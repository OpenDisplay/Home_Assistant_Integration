#!/usr/bin/env python3
"""Compose HACS preview-build metadata stamped into manifest.json.

Extracted out of `.github/workflows/preview-release.yml` so this logic --
the exact thing that took down a live install when a zero-padded run
number produced an invalid semver prerelease identifier
("3.0.2-designer-v2.012") that Home Assistant's loader refuses to load --
is testable outside a GitHub Actions run. See `tests/test_preview_version.py`
for the regression coverage (checked against Home Assistant's own
`AwesomeVersion` loader validator) and the fix commit ("fix(ci): stop
zero-padding the preview version -- HA blocks invalid semver") for the
incident report.

Two independent concerns live here, both stamped into manifest.json IN THE
CHECKOUT ONLY (never committed) by the workflow -- see that file's own
comments for why that reaches the installed integration but not anything
HACS reads live from the git tree (hacs.json, or manifest.json read via
GitHub's API before install):

`version` subcommand
    Composes the preview `version` field. Any git branch name is a real,
    reachable input here (the workflow fires on any branch in a fork, not
    just a fixed prefix) -- `sanitize_branch_for_version` maps it onto
    something Home Assistant's semver check accepts, the same way the
    zero-padding bug is prevented for `run_number`.

`name` subcommand
    Composes the preview `name` field: the tracked integration name plus
    a `(fork: <owner>/<repo>)` suffix built from `github.repository`, so a
    fork's build self-identifies on Home Assistant's Devices & Services
    page without anyone hand-editing anything. Not subject to HA's version
    validator -- `name` has no format constraint -- so no sanitization
    needed here.

Usage (this is what the workflow calls -- the tested procedure IS the
shipped procedure):

    python3 scripts/preview_version.py version <base_version> <branch> <run_number>
    python3 scripts/preview_version.py name <base_name> <repository>

Each prints its composed value to stdout, nothing else. Deliberately has NO
third-party imports: the preview-release workflow runs on a bare
`ubuntu-latest` job with no Python venv set up, and both compositions are
pure string manipulation.
"""

from __future__ import annotations

import re
import sys

# A sanitized branch token is capped at this length before it's used in a
# semver identifier. Semver itself has no length limit; this is a
# deliberate, documented cosmetic cap so a very long branch name doesn't
# produce an unwieldy tag/version string in GitHub's release list and
# HACS's UI. Chosen generously (well past any branch name a person types
# by hand) rather than derived from a hard technical constraint.
MAX_BRANCH_TOKEN_LENGTH = 40

# Fallback token when a branch name sanitizes to nothing at all (e.g. it
# was made entirely of separator characters like "///" or "..."). Must
# itself already be a valid, non-numeric semver identifier so it needs no
# further handling.
_EMPTY_BRANCH_FALLBACK = "branch"

_UNSAFE_CHARS_RE = re.compile(r"[^0-9A-Za-z-]")
_REPEATED_HYPHENS_RE = re.compile(r"-+")


def sanitize_branch_for_version(branch: str) -> str:
    """Map an arbitrary git branch name onto a safe semver identifier segment.

    Home Assistant's loader validates the composed preview version with
    `AwesomeVersion(..., ensure_strategy=[...])` (see the module docstring
    of `tests/test_preview_version.py` for the exact check, read out of
    the installed `homeassistant` package). A semver identifier may only
    contain `[0-9A-Za-z-]`, must not be empty, and -- if it consists
    entirely of digits -- must not have a leading zero. A raw git branch
    name is not constrained by any of that: it can contain "/", ".", "_",
    uppercase, unicode, start with a digit, or (accidentally) BE all
    digits with a leading zero (the padding bug in a different disguise).
    This function is the single place that gap is closed, so every branch
    a person can actually push composes an HA-loadable version -- feeding
    it unsanitized is exactly how the zero-padding incident happened.

    Deterministic and total: every input (including "" or a name that is
    only separators) produces a valid, non-empty, non-numeric-leading
    identifier of bounded length.
    """
    # 1. Anything that isn't alphanumeric or "-" becomes "-": this single
    #    rule handles "/", ".", "_", whitespace, and any other punctuation
    #    or unicode a branch name could contain.
    token = _UNSAFE_CHARS_RE.sub("-", branch)
    # 2. Collapse runs of "-" (from step 1, or already in the branch name)
    #    into one, then drop leading/trailing "-" -- a semver identifier
    #    allows internal hyphens freely, but this keeps the result
    #    readable and guarantees no leading/trailing separator survives
    #    truncation weirdly later.
    token = _REPEATED_HYPHENS_RE.sub("-", token).strip("-")
    # 3. A branch made entirely of separators (e.g. "///", "...", "___")
    #    sanitizes to "" at this point -- semver forbids an empty
    #    identifier, so fall back to a fixed, already-safe placeholder.
    if not token:
        token = _EMPTY_BRANCH_FALLBACK
    # 4. A leading digit risks landing on semver's "no leading zero on an
    #    all-numeric identifier" rule (branch "007" -> invalid; branch "42"
    #    happens to be fine today but is fragile to depend on). Prefixing
    #    with a letter sidesteps the numeric-identifier rule entirely,
    #    unconditionally, rather than special-casing "starts with 0 AND is
    #    all-digits AND has length > 1" -- fewer ways to get it wrong.
    if token[0].isdigit():
        token = f"b{token}"
    # 5. Bound the length (see MAX_BRANCH_TOKEN_LENGTH above), then repeat
    #    the trailing-hyphen strip in case truncation landed mid-run --
    #    and fall back again in the (currently unreachable, but cheap to
    #    guard) case that stripping empties it out.
    token = token[:MAX_BRANCH_TOKEN_LENGTH].rstrip("-")
    if not token:
        token = _EMPTY_BRANCH_FALLBACK
    return token


def compose_preview_version(base_version: str, branch: str, run_number: str) -> str:
    """Compose the preview manifest `version` Home Assistant will try to load.

    Mirrors `.github/workflows/preview-release.yml` exactly. `branch` is
    sanitized (see `sanitize_branch_for_version`); callers still decide
    what string to pass as `run_number` -- this function does no padding
    of it (see the workflow's own comment for why not).
    """
    return f"{base_version}-{sanitize_branch_for_version(branch)}.{run_number}"


def compose_preview_name(base_name: str, repository: str) -> str:
    """Compose the preview manifest `name`: base integration name + fork identity.

    `repository` is `github.repository` (`<owner>/<repo>`) -- generic for
    any fork, no hand-edited repo description required. Reaches the user
    on Home Assistant's Devices & Services page (manifest `name`, baked
    into the installed zip) -- see the module docstring for what this
    cannot reach (hacs.json's list-card label, read live from the git
    tree, never from a CI-only stamp).
    """
    return f"{base_name} (fork: {repository})"


def main(argv: list[str]) -> int:
    """CLI entry point: print the composed value for the workflow to capture."""
    if not argv:
        print("usage: preview_version.py {version|name} ...", file=sys.stderr)
        return 2
    command, *rest = argv
    if command == "version":
        base_version, branch, run_number = rest
        print(compose_preview_version(base_version, branch, run_number))
    elif command == "name":
        base_name, repository = rest
        print(compose_preview_name(base_name, repository))
    else:
        print(f"usage: preview_version.py {{version|name}} ... (got {command!r})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
