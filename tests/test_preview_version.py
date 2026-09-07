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
import subprocess

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


def assert_valid_git_tag(tag: str) -> None:
    """Assert `tag` is a legal git ref name -- via git's own validator, not eyeballing.

    The release step creates a tag from the composed version
    (`v${PREVIEW_VERSION}`); a name git rejects (contains "..", ends with
    ".lock", etc.) would fail the release step rather than the install.
    """
    result = subprocess.run(
        ["git", "check-ref-format", f"refs/tags/{tag}"],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"{tag!r} is not a valid git tag: {result.stderr.decode()}"
    )


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


# --- Branch-name sanitization (2026-09-03 round 2: the trigger now fires on
# ANY branch in a fork, not just `designer-*`) ------------------------------
#
# RED-FIRST: before `sanitize_branch_for_version` existed, this exact
# assertion --
#
#     version = preview_version.compose_preview_version("3.0.2", "feat/foo", "7")
#     assert_ha_loadable(version)
#
# -- failed with:
#
#     awesomeversion.exceptions.AwesomeVersionStrategyException: Strategy
#     unknown does not match ['CalVer', 'SemVer', 'SimpleVer', 'BuildVer',
#     'PEP 440'] for 3.0.2-feat/foo.7
#
# i.e. an unsanitized "/" reproduces the exact incident class (HA blocks
# the whole manifest, every service disappears) via a different route than
# the zero-padded run number. `sanitize_branch_for_version` closes that gap;
# the parametrized test below is what turned that failure green.
@pytest.mark.parametrize(
    ("branch", "expected_token"),
    [
        ("designer-v2", "designer-v2"),  # unchanged -- keeps this push's own numbering
        ("feat/foo", "feat-foo"),  # the incident-reproducing case: a single slash
        ("fix/bar/baz", "fix-bar-baz"),  # multiple slashes
        ("feature_underscores", "feature-underscores"),
        ("MixedCase", "MixedCase"),  # semver identifiers allow uppercase -- left alone
        ("dots.in.name", "dots-in-name"),
        ("2fast", "b2fast"),  # leading digit
        ("007", "b007"),  # purely digits, leading zero -- the leading-zero trap again
        (
            "42",
            "b42",
        ),  # purely digits, no leading zero -- still prefixed for uniformity
        ("--weird--", "weird"),  # leading/trailing hyphen runs stripped
        ("foo-", "foo"),  # single trailing hyphen
        ("-foo", "foo"),  # single leading hyphen
        ("///", "branch"),  # sanitizes to nothing -- separators only
        ("...", "branch"),
        ("___", "branch"),
        ("", "branch"),  # pathological empty input
        ("a" * 200, "a" * preview_version.MAX_BRANCH_TOKEN_LENGTH),  # length cap
        (
            "1" + "a" * 45,
            "b" + ("1" + "a" * 45)[: preview_version.MAX_BRANCH_TOKEN_LENGTH - 1],
        ),  # leading digit AND over length
    ],
)
def test_sanitize_branch_for_version(branch, expected_token):
    """The sanitizer's exact output for every case in the test matrix."""
    assert preview_version.sanitize_branch_for_version(branch) == expected_token


@pytest.mark.parametrize(
    ("base_version", "branch", "run_number"),
    [
        ("3.0.2", "designer-v2", "14"),
        ("3.0.2", "feat/foo", "7"),
        ("3.0.2", "fix/bar/baz", "7"),
        ("3.0.2", "feature_underscores", "7"),
        ("3.0.2", "MixedCase", "7"),
        ("3.0.2", "dots.in.name", "7"),
        ("3.0.2", "2fast", "7"),
        ("3.0.2", "007", "7"),
        ("3.0.2", "--weird--", "7"),
        ("3.0.2", "foo-", "7"),
        ("3.0.2", "///", "7"),
        ("3.0.2", "...", "7"),
        ("3.0.2", "", "7"),
        ("3.0.2", "a" * 200, "7"),
        ("3.0.2", "1" + "a" * 45, "7"),
    ],
)
def test_compose_preview_version_sanitizes_unsafe_branch_names(
    base_version, branch, run_number
):
    """Every branch a person can actually push composes an HA-loadable version."""
    version = preview_version.compose_preview_version(base_version, branch, run_number)
    assert_ha_loadable(version)  # must not raise
    assert_valid_git_tag(f"v{version}")  # the release step's tag must be legal too


def test_designer_v2_composition_is_unchanged_by_sanitization():
    """The branch actually in use keeps producing exactly what it produces today.

    This push's own release number (v3.0.2-designer-v2.N) must stay in
    sequence -- sanitization must be a no-op for a branch name that was
    already safe.
    """
    assert (
        preview_version.compose_preview_version("3.0.2", "designer-v2", "14")
        == "3.0.2-designer-v2.14"
    )


# --- Fork identity (`name`) composition -------------------------------------


def test_compose_preview_name_appends_fork_identity():
    """The composed `name` reads as the tracked name plus a generic fork suffix."""
    assert (
        preview_version.compose_preview_name(
            "OpenDisplay", "schlomo/OD_Home_Assistant_Integration"
        )
        == "OpenDisplay (fork: schlomo/OD_Home_Assistant_Integration)"
    )


def test_compose_preview_name_uses_whatever_base_name_is_tracked():
    """Not hardcoded to "OpenDisplay" -- tracks manifest.json's own name field."""
    assert (
        preview_version.compose_preview_name("Something Else", "acme/fork")
        == "Something Else (fork: acme/fork)"
    )
