"""Tests for OpenDisplay options flow."""

from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import pytest

from custom_components.opendisplay.config_flow import OpenDisplayOptionsFlow
from custom_components.opendisplay.const import (
    CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES,
    MAX_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES,
)
from custom_components.opendisplay.deep_sleep import (
    availability_window_seconds,
    deep_sleep_timeout_margin_minutes,
)


def test_availability_window_uses_timeout_margin_minutes() -> None:
    """Availability window should be deep sleep plus configured margin."""
    assert availability_window_seconds(120, 7) == 540


def test_timeout_margin_options_are_clamped() -> None:
    """Stored options should be normalized before runtime use."""
    assert (
        deep_sleep_timeout_margin_minutes(
            {CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES: -1}
        )
        == 0
    )
    assert (
        deep_sleep_timeout_margin_minutes(
            {CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES: 9999}
        )
        == MAX_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES
    )


@pytest.mark.asyncio
async def test_options_flow_accepts_timeout_margin() -> None:
    """Options flow should store a valid timeout margin."""
    flow = OpenDisplayOptionsFlow()

    with patch.object(
        OpenDisplayOptionsFlow,
        "config_entry",
        new_callable=PropertyMock,
        return_value=SimpleNamespace(options={}),
    ):
        result = await flow.async_step_init(
            {CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES: 12}
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES] == 12


@pytest.mark.asyncio
async def test_options_flow_rejects_out_of_range_timeout_margin() -> None:
    """Options flow should reject values outside 0..1440 minutes."""
    flow = OpenDisplayOptionsFlow()

    with patch.object(
        OpenDisplayOptionsFlow,
        "config_entry",
        new_callable=PropertyMock,
        return_value=SimpleNamespace(options={}),
    ):
        result = await flow.async_step_init(
            {CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES: 24 * 60 + 1}
        )

    assert result["type"] == "form"
    assert result["errors"][CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES] == (
        "invalid_timeout_margin"
    )


def test_options_flow_automatically_reloads_entry() -> None:
    """Options flow should let Home Assistant reload the entry after changes."""
    assert OpenDisplayOptionsFlow.automatic_reload is True
