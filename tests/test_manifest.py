"""Tests for the requirements declared in manifest.json."""

from importlib.metadata import requires
import json
from pathlib import Path

from packaging.requirements import Requirement

import custom_components.opendisplay as integration


def _manifest_requirements() -> dict[str, Requirement]:
    """Return the manifest's requirements keyed by package name."""
    manifest = Path(integration.__file__).with_name("manifest.json")
    parsed = map(Requirement, json.loads(manifest.read_text())["requirements"])
    return {requirement.name: requirement for requirement in parsed}


def test_manifest_detects_a_downgraded_dithering_library() -> None:
    """A too-old epaper-dithering is reinstalled before the integration imports.

    Home Assistant only checks requirements the manifest names, never their
    transitive dependencies. py-opendisplay imports DbsParams at module scope, so
    without this entry an older copy stops the integration from loading at all,
    and nothing repairs it because py-opendisplay itself still looks satisfied.
    """
    declared = _manifest_requirements()["epaper-dithering"]

    library_pin = next(
        requirement
        for requirement in map(Requirement, requires("py-opendisplay") or [])
        if requirement.name == "epaper-dithering"
    )
    pinned_version = next(iter(library_pin.specifier)).version

    assert declared.specifier.contains(pinned_version)
    # The last release without DbsParams.
    assert not declared.specifier.contains("6.0.0")
