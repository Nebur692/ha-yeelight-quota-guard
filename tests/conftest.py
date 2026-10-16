"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from unittest.mock import AsyncMock

import pytest
import pytest_socket
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_integration

from custom_components.yeelight_quota_guard import coordinator as coordinator_module
from custom_components.yeelight_quota_guard.const import (
    CONF_BULBS,
    CONF_TOKEN,
    DEFAULT_OPTIONS,
    DOMAIN,
)

from .fake_bulb import FakeBulb

TOKEN_A = "0123456789abcdef0123456789abcdef"
TOKEN_B = "fedcba9876543210fedcba9876543210"
DAY = 86400
LIMIT = 4294968  # first whole second past 2^32 ms


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture(autouse=True)
def fast_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restart waits measured in tenths of a second, same logic."""
    monkeypatch.setattr(coordinator_module, "RESTART_POLL_SECONDS", 0.1)


@pytest.fixture
async def bulbs(socket_enabled: None) -> AsyncIterator[Callable[..., FakeBulb]]:
    """Fake bulbs on loopback addresses. Only these tests may open sockets."""
    # One loopback address per bulb, like one IP per bulb at home. The harness
    # resets this list to 127.0.0.1 before every test.
    pytest_socket.socket_allow_hosts([f"127.0.0.{n}" for n in range(1, 10)])
    started: list[FakeBulb] = []

    async def make(**kwargs) -> FakeBulb:
        bulb = FakeBulb(**kwargs)
        await bulb.start()
        started.append(bulb)
        return bulb

    yield make
    for bulb in started:
        bulb.stop()


def add_yeelight(
    hass: HomeAssistant,
    did: int,
    host: str,
    title: str,
    state: ConfigEntryState = ConfigEntryState.LOADED,
    reason: str | None = None,
) -> MockConfigEntry:
    """A core Yeelight config entry as the real integration stores it.

    The core integration itself is replaced by an empty one: its entries are
    only data for the guard, and the real one would need its library."""
    if "yeelight" not in hass.data.get("_yqg_mocked", set()):
        mock_integration(
            hass,
            MockModule(
                "yeelight",
                async_setup_entry=AsyncMock(return_value=True),
                async_unload_entry=AsyncMock(return_value=True),
            ),
        )
        hass.data.setdefault("_yqg_mocked", set()).add("yeelight")
    unique = f"0x{did:016x}"
    entry = MockConfigEntry(
        domain="yeelight",
        title=title,
        unique_id=unique,
        data={"host": host, "id": unique, "detected_model": "colorc"},
        options={"model": "colorc"},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, state, reason)
    return entry


def guard_entry(hass: HomeAssistant, tokens: dict[int, str], **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Yeelight Quota Guard",
        data={CONF_BULBS: {str(did): {CONF_TOKEN: token} for did, token in tokens.items()}},
        options={**DEFAULT_OPTIONS, **options},
    )
    entry.add_to_hass(hass)
    return entry
