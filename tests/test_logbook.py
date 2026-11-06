"""Restarts in the real logbook, as a person sees them on the bulb's device page."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockModule, mock_integration
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.yeelight_quota_guard.guard import Reason
from custom_components.yeelight_quota_guard.logbook import _TEXTS, restart_message

from .conftest import DAY, TOKEN_A, add_yeelight, guard_entry


@pytest.fixture(autouse=True)
def mock_recorder_before_hass(async_setup_recorder_instance) -> None:
    """The logbook reads the recorder, whose database must exist before hass."""


# Every result the coordinator records, and every reason.
RESULTS = {"ok", "power_restored", "power_changed", "power_unknown", "no_answer", "not_back"}


def test_every_reason_and_result_has_a_text_in_every_language() -> None:
    wanted = RESULTS | {str(r) for r in Reason}
    for language, texts in _TEXTS.items():
        assert set(texts) == wanted, language


@pytest.mark.parametrize(
    ("language", "reason", "result", "expected"),
    [
        ("en", "repair", "ok", "was restarted because it had stopped answering Home Assistant: it is back"),
        ("es", "prevent", "ok", "se reinició antes de llegar al bloqueo de los 49,7 días: ya ha vuelto"),
        (
            "es-ES",
            "manual",
            "not_back",
            "se reinició con su botón Reiniciar: no ha vuelto; mira en Ajustes → Reparaciones",
        ),
        ("en", "repair", "no_answer", "did not answer, so it was not restarted"),
        ("fr", "manual", "ok", "was restarted with its Restart button: it is back"),
    ],
)
def test_restart_message(language: str, reason: str, result: str, expected: str) -> None:
    assert restart_message(language, reason, result) == expected


async def test_a_button_restart_shows_on_the_bulb_page(
    recorder_mock, hass: HomeAssistant, hass_ws_client, bulbs
) -> None:
    start = dt_util.utcnow()
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=DAY, power="on")
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    # The frontend is only there for the logbook's panel and needs the built UI,
    # which the test image does not carry; the logbook itself is the real one.
    mock_integration(hass, MockModule("frontend"))
    with patch("homeassistant.components.logbook.frontend.async_register_built_in_panel"):
        assert await async_setup_component(hass, "logbook", {})
    entry = guard_entry(hass, {10: TOKEN_A})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    await hass.services.async_call("button", "press", {"entity_id": "button.salon_1_restart"}, blocking=True)
    await async_wait_recording_done(hass)

    # The device page that holds the Restart button.
    device_id = er.async_get(hass).async_get("button.salon_1_restart").device_id
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "logbook/get_events", "start_time": start.isoformat(), "device_ids": [device_id]}
    )
    reply = await client.receive_json()
    assert reply["success"], reply
    entries = [e for e in reply["result"] if e.get("domain") == "yeelight_quota_guard"]
    assert len(entries) == 1, reply["result"]
    assert entries[0]["name"] == "Salón 1"
    assert entries[0]["message"] == "was restarted with its Restart button: it is back"
