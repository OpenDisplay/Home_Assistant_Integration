#!/usr/bin/env python3
"""Update the vendored npm packages the designer panel ships with.

HA custom components cannot `npm install` at runtime, so both the designer
library and its `js-yaml` dependency ship vendored inside this repo. This
script replaces the old "vendor an anonymous release blob" procedure with a
pinned, verifiable one for BOTH packages: download the exact npm tarball for
a pinned version, verify its integrity, and copy the files the panel
actually consumes into
``custom_components/opendisplay/designer/frontend/vendor/``.

The pins live in ``designer.lock.json`` next to this script's target
directory, one entry per package — that file, plus this script, is the
whole update procedure.

Usage:

    scripts/update-designer-vendor.py
        Re-download and re-verify BOTH currently pinned packages (idempotent
        upgrade path — safe to re-run any time, e.g. to recover from a
        corrupted vendor/ checkout).

    scripts/update-designer-vendor.py --pin 2.6.0
        Bump the designer's pin to a new version: fetch that version's
        metadata from the npm registry (the registry-declared sha512
        `dist.integrity`), download the tarball, verify the tarball's
        *actual* hash against that declared integrity, then write the new
        pin and vendor files. Fails loudly and changes nothing on a
        mismatch.

    scripts/update-designer-vendor.py --pin-js-yaml 4.1.1
        Same, for the `js-yaml` dependency.

Both packages fail loudly (non-zero exit) before touching `vendor/` at all
on any network, integrity, or content-shape problem — verification
(registry lookup, hash check, tarball-layout check) always completes first.
The one window this does not cover: a raw filesystem error (disk full,
permissions) partway through the final copy step, after every file has
already been verified correct, could in principle leave a partial set of
files on disk. Nothing here silently falls back to an unpinned or
unverified download.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from argparse import ArgumentParser
from base64 import b64encode
from pathlib import Path
from typing import NoReturn

REGISTRY_BASE = "https://registry.npmjs.org"

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = (
    REPO_ROOT
    / "custom_components"
    / "opendisplay"
    / "designer"
    / "frontend"
    / "vendor"
)
LOCK_FILE = VENDOR_DIR / "designer.lock.json"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class Package:
    """One npm package this script vendors, pinned+verified independently."""

    name: str  # npm package name, e.g. "@schlomo/odl-drawcustom-designer"
    lock_key: str  # this package's key inside designer.lock.json
    pin_flag: str  # the --pin-* CLI flag that bumps this package
    # tarball path (relative to the extracted "package/" dir) -> dest
    # filename in vendor/. The tarball's own top-level dir is always
    # "package" regardless of the npm package's name (npm convention).
    files: tuple[tuple[str, str], ...]

    def tarball_filename(self, version: str) -> str:
        # Scoped packages (@scope/name) publish as name-version.tgz with the
        # scope segment kept but the slash dropped, e.g.
        # "odl-drawcustom-designer-2.6.3.tgz" for "@schlomo/odl-drawcustom-designer".
        bare_name = self.name.rsplit("/", 1)[-1]
        return f"{bare_name}-{version}.tgz"


DESIGNER = Package(
    name="@schlomo/odl-drawcustom-designer",
    lock_key="designer",
    pin_flag="--pin",
    files=(
        ("odl-drawcustom-designer.js", "odl-drawcustom-designer.js"),
        ("odl-drawcustom-designer.d.ts", "odl-drawcustom-designer.d.ts"),
        ("LICENSE", "LICENSE"),
        ("NOTICE", "NOTICE"),
        ("THIRD_PARTY.md", "THIRD_PARTY.md"),
    ),
)

JS_YAML = Package(
    name="js-yaml",
    lock_key="js_yaml",
    pin_flag="--pin-js-yaml",
    # dist/js-yaml.mjs is the real npm-published ESM build -- replaces the
    # jsDelivr-rebundled (Rollup+Terser) blob the old vendoring procedure
    # dropped in by hand, which had no pin, no integrity record, and no
    # license file of its own (found in adversarial review, 2026-08-30).
    # LICENSE.js-yaml (not LICENSE -- that name is the designer library's)
    # is this package's own MIT license text, not merely described in a
    # README (THIRD_PARTY.md is the designer bundle's own auto-generated
    # notices file, covering the designer's transitive deps, not a sibling
    # dependency of this panel -- hand-editing it would misattribute
    # js-yaml as one of the designer's own bundled packages).
    files=(
        ("dist/js-yaml.mjs", "js-yaml.mjs"),
        ("LICENSE", "LICENSE.js-yaml"),
    ),
)

PACKAGES = (DESIGNER, JS_YAML)


def die(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def validate_pin(flag: str, version: str) -> None:
    if not SEMVER_RE.fullmatch(version):
        die(
            f"{flag} {version!r} doesn't look like a semver version "
            "(expected e.g. '2.6.0') — refusing to build a registry URL from it"
        )


def read_lock() -> dict[str, dict[str, str]]:
    if not LOCK_FILE.is_file():
        die(
            f"no pins found at {LOCK_FILE} — run with --pin <version> and "
            "--pin-js-yaml <version> first"
        )
    data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    for pkg in PACKAGES:
        entry = data.get(pkg.lock_key)
        if not entry:
            die(f"{LOCK_FILE} is missing a '{pkg.lock_key}' entry")
        for key in ("version", "integrity"):
            if key not in entry:
                die(f"{LOCK_FILE}['{pkg.lock_key}'] is missing required key '{key}'")
    return data


def write_lock(pins: dict[str, dict[str, str]]) -> None:
    LOCK_FILE.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")


def fetch_registry_integrity(pkg: Package, version: str) -> str:
    """Fetch npm's own declared sha512 integrity for an exact version.

    This is the one place a bump *trusts* the registry (same trust boundary
    as `npm install` itself). Every subsequent run re-verifies the tarball
    against the value pinned here, not against the registry again.
    """
    url = f"{REGISTRY_BASE}/{pkg.name}/{version}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
            meta = json.loads(resp.read())
    except urllib.error.HTTPError as err:
        die(f"npm registry lookup failed for {pkg.name}@{version}: {err}")
    except urllib.error.URLError as err:
        die(f"could not reach npm registry: {err}")
    integrity = meta.get("dist", {}).get("integrity")
    if not integrity or not integrity.startswith("sha512-"):
        die(f"registry metadata for {pkg.name}@{version} has no sha512 integrity")
    return integrity


def download_tarball(pkg: Package, version: str, dest: Path) -> Path:
    filename = pkg.tarball_filename(version)
    url = f"{REGISTRY_BASE}/{pkg.name}/-/{filename}"
    tarball_path = dest / filename
    try:
        # urlretrieve() takes no timeout of its own (it blocks on
        # socket.getdefaulttimeout(), i.e. forever, by default) — use
        # urlopen()'s explicit timeout instead, same as the registry lookup
        # above, and stream the response straight to disk.
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
            with tarball_path.open("wb") as f:
                shutil.copyfileobj(resp, f)
    except urllib.error.HTTPError as err:
        die(f"download failed for {url}: {err}")
    except urllib.error.URLError as err:
        die(f"could not reach npm registry: {err}")
    return tarball_path


def verify_integrity(tarball_path: Path, expected: str) -> None:
    algo, _, expected_b64 = expected.partition("-")
    if algo != "sha512":
        die(f"unsupported integrity algorithm '{algo}' (expected sha512)")
    digest = hashlib.sha512(tarball_path.read_bytes()).digest()
    actual_b64 = b64encode(digest).decode("ascii")
    if actual_b64 != expected_b64:
        die(
            f"integrity check FAILED for {tarball_path.name}\n"
            f"  expected: sha512-{expected_b64}\n"
            f"  actual:   sha512-{actual_b64}\n"
            "Refusing to install a tarball that does not match the pinned hash."
        )
    # Also print the sha256 of the tarball for anyone doing a manual
    # `shasum -a 256` cross-check (the vendored library's own release
    # artifacts publish a .sha256 file in this form).
    sha256 = hashlib.sha256(tarball_path.read_bytes()).hexdigest()
    print(f"integrity OK (sha512), sha256={sha256}")


def install_vendor_files(pkg: Package, tarball_path: Path, extract_dir: Path) -> None:
    with tarfile.open(tarball_path) as tar:
        tar.extractall(extract_dir, filter="data")  # noqa: S202 -- verified above
    package_dir = extract_dir / "package"
    if not package_dir.is_dir():
        die(f"unexpected tarball layout: no package/ directory in {tarball_path.name}")

    missing = [src for src, _dest in pkg.files if not (package_dir / src).is_file()]
    if missing:
        die(f"tarball is missing expected file(s): {', '.join(missing)}")

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    # The designer library's own LICENSE/NOTICE replace the old
    # LICENSE.odl-drawcustom-designer file from the v1.0.2 blob era — remove
    # that stale name so vendor/ never carries both. Once, not per file.
    stale = VENDOR_DIR / "LICENSE.odl-drawcustom-designer"
    if stale.is_file():
        stale.unlink()
    for src, dest in pkg.files:
        shutil.copy2(package_dir / src, VENDOR_DIR / dest)
        print(f"vendored {dest}")


def update_package(pkg: Package, pin: str | None, pins: dict[str, dict[str, str]]) -> None:
    if pin:
        version = pin
        validate_pin(pkg.pin_flag, version)
        integrity = fetch_registry_integrity(pkg, version)
        print(f"pinning {pkg.name}@{version}")
    else:
        pinned = pins[pkg.lock_key]
        version = pinned["version"]
        integrity = pinned["integrity"]
        print(f"re-installing pinned {pkg.name}@{version}")

    with tempfile.TemporaryDirectory(prefix="odl-designer-vendor-") as tmp:
        tmp_path = Path(tmp)
        tarball = download_tarball(pkg, version, tmp_path)
        verify_integrity(tarball, integrity)
        install_vendor_files(pkg, tarball, tmp_path)

    pins[pkg.lock_key] = {"version": version, "integrity": integrity}
    print(f"done — {pkg.name}@{version} vendored into {VENDOR_DIR}")


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pin",
        metavar="VERSION",
        help="bump the designer library's pin to this exact version (e.g. "
        "2.6.0) instead of re-installing the currently pinned one",
    )
    parser.add_argument(
        "--pin-js-yaml",
        metavar="VERSION",
        help="bump the js-yaml dependency's pin to this exact version "
        "instead of re-installing the currently pinned one",
    )
    args = parser.parse_args()

    # read_lock() requires every package already pinned -- fine for a
    # re-verify run of one package while the other has no --pin* flag; only
    # a truly empty lock file (first-ever run) needs both --pin* up front.
    pins: dict[str, dict[str, str]] = (
        read_lock() if LOCK_FILE.is_file() else {}
    )
    for pkg, pin in ((DESIGNER, args.pin), (JS_YAML, args.pin_js_yaml)):
        if not pin and pkg.lock_key not in pins:
            die(
                f"no pin found for {pkg.name} — run with {pkg.pin_flag} "
                "<version> first"
            )
        update_package(pkg, pin, pins)

    write_lock(pins)
    print(f"pins recorded in {LOCK_FILE}")


if __name__ == "__main__":
    main()
