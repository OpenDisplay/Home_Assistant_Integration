"""Regression coverage for the preview-release version-stamp incident (2026-09-03).

`.github/workflows/preview-release.yml` stamps a preview build's
`manifest.json` version as `${base_version}-${branch}.${run_number}`. A
change zero-padded `run_number` to three digits (`printf '%03d'`),
producing `3.0.2-designer-v2.012`. A leading zero makes that prerelease
numeric identifier invalid semver, and Home Assistant's loader rejects the
whole manifest -- not just the version string:

    The custom integration 'opendisplay' does not have a valid version key
    (3.0.2-designer-v2.012) in the manifest file and was blocked from
    loading.

The integration then does not load AT ALL: every service it provides
vanishes (`Action opendisplay.drawcustom not found`) and every automation
calling it fails. This happened on the maintainer's live instance for
~40 minutes. Fixed in commit e47f712 by reverting the padding; THIS file is
the regression test that fix commit shipped without.

These tests exercise `scripts/preview_version.compose_preview_version` --
the extracted, workflow-shared composition step (see that module's
docstring; the workflow now calls it directly, so this is not a
parallel test-only reimplementation) -- against Home Assistant's OWN
version validator, not a hand-rolled regex.

`homeassistant/loader.py` (`Integration.resolve_from_root`) does this,
verbatim (confirmed by reading the installed package in this repo's own
`.venv`, `homeassistant/loader.py` around the "does not have a valid
version key" log line)::

    try:
        AwesomeVersion(
            integration.version,
            ensure_strategy=[
                AwesomeVersionStrategy.CALVER,
                AwesomeVersionStrategy.SEMVER,
                AwesomeVersionStrategy.SIMPLEVER,
                AwesomeVersionStrategy.BUILDVER,
                AwesomeVersionStrategy.PEP440,
            ],
        )
    except AwesomeVersionException:
        _LOGGER.error(
            "The custom integration '%s' does not have a valid version key"
            " (%s) in the manifest file and was blocked from loading. ..."
        )
        return None

`assert_ha_loadable` below reproduces that exact call.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from awesomeversion import (
    AwesomeVersion,
    AwesomeVersionException,
    AwesomeVersionStrategy,
)
import pytest

# Home Assistant's own accepted strategies for a custom integration's
# manifest version -- copied verbatim from homeassistant/loader.py so this
# test fails a bad version exactly the way HA's real loader does, rather
# than approximating it with a regex of our own invention.
_HA_STRATEGIES = [
    AwesomeVersionStrategy.CALVER,
    AwesomeVersionStrategy.SEMVER,
    AwesomeVersionStrategy.SIMPLEVER,
    AwesomeVersionStrategy.BUILDVER,
    AwesomeVersionStrategy.PEP440,
]


def _load_preview_version_module():
    """Import scripts/preview_version.py by path (scripts/ is not a package)."""
    path = Path(__file__).resolve().parent.parent / "scripts" / "preview_version.py"
    spec = importlib.util.spec_from_file_location("preview_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preview_version = _load_preview_version_module()


def assert_ha_loadable(version: str) -> None:
    """Assert HA's loader would accept `version` (raises otherwise).

    This is `homeassistant.loader.Integration.resolve_from_root`'s own
    check, called the same way it calls it.
    """
    AwesomeVersion(version, ensure_strategy=_HA_STRATEGIES)


@pytest.mark.parametrize(
    ("base_version", "branch", "run_number"),
    [
        ("3.0.2", "designer-v2", "13"),  # the actual last-known-good build
        ("3.0.2", "designer-v2", "1"),  # smallest realistic run number
        ("3.0.2", "designer-v2", "104"),  # three digits, unpadded -- not the bug
        ("1.2.3-rc.1", "designer-v2", "5"),  # base version already has a prerelease
        ("3.0.2", "designer-v3-feature", "7"),  # a hyphenated branch suffix
    ],
)
def test_compose_preview_version_is_ha_loadable(base_version, branch, run_number):
    """Plain (unpadded) run numbers always compose an HA-loadable version."""
    version = preview_version.compose_preview_version(base_version, branch, run_number)
    assert_ha_loadable(version)  # must not raise


def test_compose_preview_version_rejects_zero_padded_run_number():
    """RED: this is the incident. `printf '%03d' 12` -> "012" breaks HA's loader.

    "012" is exactly what the reverted workflow code
    (`run_number="$(printf '%03d' "${{ github.run_number }}")"`, removed in
    commit e47f712) produced for run 12. Composing with it MUST produce a
    version string Home Assistant's own check rejects -- if this assertion
    ever passes, the incident has recurred.
    """
    padded_version = preview_version.compose_preview_version(
        "3.0.2", "designer-v2", "012"
    )
    assert padded_version == "3.0.2-designer-v2.012"
    with pytest.raises(AwesomeVersionException):
        assert_ha_loadable(padded_version)


def test_compose_preview_version_matches_incident_report():
    """The exact string from the maintainer's error log is reproduced and rejected."""
    version = preview_version.compose_preview_version("3.0.2", "designer-v2", "012")
    assert version == "3.0.2-designer-v2.012"
    with pytest.raises(AwesomeVersionException):
        assert_ha_loadable(version)


def test_branch_name_with_a_slash_is_not_ha_loadable_but_is_unreachable():
    """Document a second, DIFFERENT invalid-version shape -- not this incident.

    A branch containing "/" (e.g. "feature/designer-v2") also composes a
    version HA's loader rejects (the "/" is not a valid semver build/
    prerelease character). Unlike the zero-padding bug this is NOT
    currently reachable: `preview-release.yml`'s trigger is
    `branches: ['designer-*']`, and GitHub's branch-filter glob does not
    match `/` with `*`, so `github.ref_name` can never contain one here.
    Recorded so a future change to the trigger (e.g. widening it) doesn't
    silently reintroduce an unloadable-manifest bug of this same shape.
    """
    version = preview_version.compose_preview_version(
        "3.0.2", "feature/designer-v2", "7"
    )
    with pytest.raises(AwesomeVersionException):
        assert_ha_loadable(version)
