"""Shared pytest fixtures and configuration.

Stubs out heavy Home Assistant selector / media components that are not
needed for unit tests and may cause import errors due to version
mismatches in the test environment.
"""

from __future__ import annotations

import sys
import homeassistant.helpers.selector  # ensure it's loaded first


def _stub_ha_selector() -> None:
    """Stub out homeassistant.helpers.selector to avoid voluptuous schema errors."""
    selector_mod = sys.modules.get("homeassistant.helpers.selector")
    if selector_mod is None:
        return

    class _NumberSelectorMode:
        BOX = "box"

    class _NumberSelectorConfig:
        def __init__(self, **kwargs):
            pass

    class _NumberSelector:
        def __init__(self, config=None):
            pass

        def __call__(self, value):
            return value

    class _MediaSelectorConfig:
        def __init__(self, **kwargs):
            pass

    class _MediaSelector:
        def __init__(self, config=None):
            pass

        def __call__(self, value):
            return value

    selector_mod.NumberSelectorMode = _NumberSelectorMode
    selector_mod.NumberSelectorConfig = _NumberSelectorConfig
    selector_mod.NumberSelector = _NumberSelector
    selector_mod.MediaSelectorConfig = _MediaSelectorConfig
    selector_mod.MediaSelector = _MediaSelector


_stub_ha_selector()
