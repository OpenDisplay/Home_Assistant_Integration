#!/usr/bin/env python3
"""Compose the HACS preview-release version stamped into manifest.json.

Extracted out of `.github/workflows/preview-release.yml` so the
version-composition logic -- the exact thing that took down a live install
when a zero-padded run number produced an invalid semver prerelease
identifier ("3.0.2-designer-v2.012") that Home Assistant's loader refuses
to load -- is testable outside a GitHub Actions run. See
`tests/test_preview_version.py` for the regression coverage (checked
against Home Assistant's own `AwesomeVersion` loader validator) and the
fix commit ("fix(ci): stop zero-padding the preview version -- HA blocks
invalid semver") for the incident report.

Usage (this is what the workflow calls -- the tested procedure IS the
shipped procedure):

    python3 scripts/preview_version.py <base_version> <branch> <run_number>

Prints the composed version to stdout, nothing else. Deliberately has NO
third-party imports: the preview-release workflow runs on a bare
`ubuntu-latest` job with no Python venv set up, and composition itself is
pure string formatting. Do NOT zero-pad `run_number` before passing it in
-- see the workflow's own comment for why.
"""

from __future__ import annotations

import sys


def compose_preview_version(base_version: str, branch: str, run_number: str) -> str:
    """Compose the preview manifest version Home Assistant will try to load.

    Mirrors `.github/workflows/preview-release.yml` exactly. Callers decide
    what string to pass as `run_number` -- this function does no padding,
    validation, or normalization of its own; it is pure composition.
    """
    return f"{base_version}-{branch}.{run_number}"


def main(argv: list[str]) -> int:
    """CLI entry point: print the composed version for the workflow to capture."""
    base_version, branch, run_number = argv
    print(compose_preview_version(base_version, branch, run_number))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
