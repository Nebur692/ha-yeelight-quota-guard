"""The integration end to end, against fake bulbs that behave like real ones."""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.yeelight_quota_guard import coordinator as coordinator_module
from custom_components.yeelight_quota_guard.const import DOMAIN, EVENT_RESTARTED
from custom_components.yeelight_quota_guard.diagnostics import async_get_config_entry_diagnostics

from .conftest import DAY, LIMIT, TOKEN_A, TOKEN_B, add_yeelight, guard_entry

STUCK = "Could not fetch initial state; try power cycling the device"
# What the core Yeelight entry of a locked bulb looked like on 15-10-2026.
LOCKED = {"state": ConfigEntryState.SETUP_RETRY, "reason": STUCK}


async def _setup(hass: HomeAssistant, entry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _refresh(hass: HomeAssistant, entry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_entities_live_on_the_same_bulb(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=3 * DAY)
    yeelight = add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    registry = dr.async_get(hass)
    core_device = registry.async_get_or_create(
        config_entry_id=yeelight.entry_id,
        identifiers={("yeelight", yeelight.unique_id)},
        name="Yeelight Color C 0x0a",
        manufacturer="Yeelight",
    )
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)

    # One device per config entry since 2026.9; the shared identifiers are what
    # tells Home Assistant both are the same bulb.
    ours = dr.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(ours) == 1
    assert ours[0].identifiers == core_device.identifiers
    assert ours[0].name == "Salón 1"
    entities = er.async_entries_for_device(er.async_get(hass), ours[0].id)
    assert sorted(e.unique_id for e in entities) == ["10_powered_on", "10_quota_lock", "10_restart"]
    # The core integration's device is left exactly as it was.
    untouched = registry.async_get(core_device.id)
    assert (untouched.name, untouched.config_entry_id) == ("Yeelight Color C 0x0a", yeelight.entry_id)

    powered = hass.states.get("sensor.salon_1_powered_on_since")
    lock = hass.states.get("sensor.salon_1_quota_lock_expected")
    assert powered is not None and lock is not None
    assert lock.state > powered.state


async def test_a_locked_bulb_is_repaired_at_once_and_keeps_its_power(hass: HomeAssistant, bulbs) -> None:
    on = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=LIMIT + 60, power="on")
    off = await bulbs(host="127.0.0.3", did=11, token=TOKEN_B, life=LIMIT + 60, power="off")
    add_yeelight(hass, 10, "127.0.0.2", "Trastero", **LOCKED)
    add_yeelight(hass, 11, "127.0.0.3", "Salón A", **LOCKED)
    events = async_capture_events(hass, EVENT_RESTARTED)
    entry = guard_entry(hass, {10: TOKEN_A, 11: TOKEN_B})
    await _setup(hass, entry)

    assert (on.reboots, off.reboots) == (1, 1)
    assert (on.power, off.power) == ("on", "off")
    assert sorted((e.data["bulb"], e.data["reason"], e.data["result"]) for e in events) == [
        ("Salón A", "repair", "ok"),
        ("Trastero", "repair", "ok"),
    ]
    assert entry.runtime_data.data[10].life_s < 60

    # Measured, not assumed: the next poll sees fresh counters and does nothing.
    await _refresh(hass, entry)
    assert (on.reboots, off.reboots) == (1, 1)


async def test_power_is_put_back_if_the_restart_changed_it(hass: HomeAssistant, bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=LIMIT + 60, power="off", power_after_reboot="on")
    add_yeelight(hass, 10, "127.0.0.2", "Cocina", **LOCKED)
    events = async_capture_events(hass, EVENT_RESTARTED)
    await _setup(hass, guard_entry(hass, {10: TOKEN_A}))
    assert bulb.power == "off"
    assert events[0].data["result"] == "power_restored"


async def test_prevention_waits_for_the_light_to_be_off(hass: HomeAssistant, bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=46 * DAY, power="on")
    add_yeelight(hass, 10, "127.0.0.2", "Comedor")
    # Same start and end: the quiet window is the whole day, so the test does
    # not depend on the clock.
    entry = guard_entry(hass, {10: TOKEN_A}, window_start="04:00:00", window_end="04:00:00")
    await _setup(hass, entry)
    assert bulb.reboots == 0

    bulb.power = "off"
    await _refresh(hass, entry)
    assert bulb.reboots == 1
    assert bulb.power == "off"


async def test_young_bulbs_are_left_alone(hass: HomeAssistant, bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=44 * DAY, power="off")
    add_yeelight(hass, 10, "127.0.0.2", "Pasillo")
    await _setup(hass, guard_entry(hass, {10: TOKEN_A}, window_start="04:00:00", window_end="04:00:00"))
    assert bulb.reboots == 0


async def test_a_restart_that_does_not_take_is_reported_once_and_not_looped(
    hass: HomeAssistant, bulbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator_module, "RESTART_WAIT", timedelta(seconds=1))
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=LIMIT + 60, reboot_works=False)
    add_yeelight(hass, 10, "127.0.0.2", "Recibidor", **LOCKED)
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "restart_failed_10")
    assert issue is not None
    assert issue.translation_placeholders["code"] == "027-001-145-123"
    assert issue.translation_placeholders["bulb"] == "Recibidor"
    for _ in range(3):
        await _refresh(hass, entry)
    assert bulb.reboots == 1


async def test_stuck_bulbs_without_token_raise_one_issue(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=DAY)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    add_yeelight(hass, 11, "127.0.0.3", "Cuadro B", ConfigEntryState.SETUP_RETRY, STUCK)
    add_yeelight(hass, 12, "127.0.0.4", "Cuadro A", ConfigEntryState.SETUP_RETRY, STUCK)
    add_yeelight(hass, 13, "127.0.0.5", "Offline", ConfigEntryState.SETUP_RETRY, "Timed out")
    await _setup(hass, guard_entry(hass, {10: TOKEN_A}))

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "missing_token")
    assert issue is not None
    # Sorted, and only the quota-lock symptom: a bulb that is simply offline is not listed.
    assert issue.translation_placeholders["bulbs"] == "Cuadro A, Cuadro B"
    assert issue.translation_placeholders["code"] == "027-001-145-100"


async def test_no_issue_when_every_stuck_bulb_has_a_token(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=DAY)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1", ConfigEntryState.SETUP_RETRY, STUCK)
    with patch.object(hass.config_entries, "async_reload", AsyncMock()):
        await _setup(hass, guard_entry(hass, {10: TOKEN_A}))
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missing_token") is None


async def test_the_core_entry_is_reloaded_after_a_repair(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=LIMIT + 60)
    stuck = add_yeelight(hass, 10, "127.0.0.2", "Salón 4", ConfigEntryState.SETUP_RETRY, STUCK)
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload:
        await _setup(hass, guard_entry(hass, {10: TOKEN_A}))
    reload.assert_awaited_once_with(stuck.entry_id)


async def test_a_rejected_token_is_reported_after_three_polls(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=DAY)
    add_yeelight(hass, 10, "127.0.0.2", "Mesilla")
    entry = guard_entry(hass, {10: TOKEN_B})
    await _setup(hass, entry)
    registry = ir.async_get(hass)
    await _refresh(hass, entry)
    assert registry.async_get_issue(DOMAIN, "token_rejected_10") is None  # two misses can be Wi-Fi
    await _refresh(hass, entry)
    issue = registry.async_get_issue(DOMAIN, "token_rejected_10")
    assert issue is not None
    assert issue.translation_placeholders["code"] == "027-001-145-041"


async def test_another_device_at_the_address_is_never_restarted(hass: HomeAssistant, bulbs) -> None:
    other = await bulbs(host="127.0.0.2", did=999, token=TOKEN_A, life=LIMIT + 60)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    assert other.reboots == 0
    assert entry.runtime_data.data[10].reachable is False


async def test_restart_button(hass: HomeAssistant, bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=DAY, power="on")
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    await hass.services.async_call("button", "press", {"entity_id": "button.salon_1_restart"}, blocking=True)
    assert bulb.reboots == 1 and bulb.power == "on"

    bulb.stop()  # gone from the network
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call("button", "press", {"entity_id": "button.salon_1_restart"}, blocking=True)
    assert err.value.translation_key == "restart_failed"
    assert err.value.translation_placeholders["code"] == "027-001-080-120"


async def test_diagnostics_never_show_a_token(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=DAY)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert TOKEN_A not in str(diagnostics)
    assert diagnostics["bulbs"]["bulbs"]["10"]["token"] == "**REDACTED**"
    assert diagnostics["states"]["10"]["reachable"] == "True"


async def test_unload(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=DAY)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def _bulb_with_area(hass: HomeAssistant, bulbs, area_id: str | None):
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=3 * DAY)
    yeelight = add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    registry = dr.async_get(hass)
    core_device = registry.async_get_or_create(
        config_entry_id=yeelight.entry_id, identifiers={("yeelight", yeelight.unique_id)}, name="Yeelight 0x0a"
    )
    if area_id:
        registry.async_update_device(core_device.id, area_id=area_id)
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    (ours,) = dr.async_entries_for_config_entry(registry, entry.entry_id)
    return registry, core_device, ours


async def test_our_device_takes_the_yeelight_area(hass: HomeAssistant, bulbs) -> None:
    """15-10-2026: with 40 bulbs, Home Assistant asked for each one's area."""
    area = ar.async_get(hass).async_create("Salón")
    _, _, ours = await _bulb_with_area(hass, bulbs, area.id)
    assert ours.area_id == area.id


async def test_no_area_in_yeelight_leaves_ours_empty(hass: HomeAssistant, bulbs) -> None:
    _, _, ours = await _bulb_with_area(hass, bulbs, None)
    assert ours.area_id is None


async def test_moving_the_yeelight_bulb_moves_ours(hass: HomeAssistant, bulbs) -> None:
    areas = ar.async_get(hass)
    salon, cocina = areas.async_create("Salón"), areas.async_create("Cocina")
    registry, core_device, ours = await _bulb_with_area(hass, bulbs, salon.id)
    registry.async_update_device(core_device.id, area_id=cocina.id)
    await hass.async_block_till_done()
    assert registry.async_get(ours.id).area_id == cocina.id


async def test_an_area_chosen_for_ours_on_purpose_is_kept(hass: HomeAssistant, bulbs) -> None:
    areas = ar.async_get(hass)
    salon, cocina, garaje = areas.async_create("Salón"), areas.async_create("Cocina"), areas.async_create("Garaje")
    registry, core_device, ours = await _bulb_with_area(hass, bulbs, salon.id)
    registry.async_update_device(ours.id, area_id=garaje.id)
    registry.async_update_device(core_device.id, area_id=cocina.id)
    await hass.async_block_till_done()
    assert registry.async_get(ours.id).area_id == garaje.id


async def test_a_restart_fills_an_area_that_was_left_empty(hass: HomeAssistant, bulbs) -> None:
    """His 40 devices were created before this existed: the next start fills them."""
    area = ar.async_get(hass).async_create("Salón")
    registry, core_device, ours = await _bulb_with_area(hass, bulbs, None)
    registry.async_update_device(core_device.id, area_id=area.id)
    registry.async_update_device(ours.id, area_id=None)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert registry.async_get(ours.id).area_id == area.id


def _core_light(hass: HomeAssistant, yeelight, state: str) -> str:
    entity = er.async_get(hass).async_get_or_create(
        "light", "yeelight", yeelight.unique_id, config_entry=yeelight, suggested_object_id="core"
    )
    hass.states.async_set(entity.entity_id, state)
    return entity.entity_id


async def test_past_the_limit_but_working_is_left_alone(hass: HomeAssistant, bulbs) -> None:
    """15-10-2026: nine strips and lamps past 49.7 days, working fine, were restarted for nothing."""
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=60 * DAY, power="on", affected=False)
    yeelight = add_yeelight(hass, 10, "127.0.0.2", "Led pasillo")
    _core_light(hass, yeelight, "on")
    entry = guard_entry(hass, {10: TOKEN_A})
    # On, so night-time prevention cannot fire either, whatever the clock says.
    await _setup(hass, entry)
    for _ in range(3):
        await _refresh(hass, entry)
    assert bulb.reboots == 0


async def test_light_unavailable_two_polls_in_a_row_counts_as_locked(hass: HomeAssistant, bulbs) -> None:
    # A colorc that keeps its one LAN connection for Home Assistant: the LAN
    # question proves nothing, only the unavailable light is left.
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=LIMIT + 60, power="on", lan="silent")
    yeelight = add_yeelight(hass, 10, "127.0.0.2", "Salón 3")
    light = _core_light(hass, yeelight, "unavailable")
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    assert bulb.reboots == 0  # one poll could be a Wi-Fi blip

    hass.states.async_set(light, "on")
    await _refresh(hass, entry)
    hass.states.async_set(light, "unavailable")
    await _refresh(hass, entry)
    assert bulb.reboots == 0  # the count starts again after it came back

    await _refresh(hass, entry)
    assert bulb.reboots == 1
    assert bulb.power == "on"


async def test_locked_while_home_assistant_still_shows_it_is_repaired(hass: HomeAssistant, bulbs) -> None:
    """The lock seen from the bulb itself, before Home Assistant notices anything."""
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=LIMIT + 60, power="on")
    yeelight = add_yeelight(hass, 10, "127.0.0.2", "Cocina 1")
    _core_light(hass, yeelight, "on")
    await _setup(hass, guard_entry(hass, {10: TOKEN_A}))
    assert bulb.reboots == 1
    assert bulb.power == "on"


async def test_young_bulbs_are_not_asked_over_lan(hass: HomeAssistant, bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=10 * DAY)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    await _setup(hass, guard_entry(hass, {10: TOKEN_A}))
    assert bulb.lan_questions == 0


async def test_working_past_the_limit_is_learnt_and_remembered(hass: HomeAssistant, bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=60 * DAY, power="off", affected=False)
    add_yeelight(hass, 10, "127.0.0.2", "Led pasillo")
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    assert entry.runtime_data.data[10].immune
    lock = hass.states.get("sensor.led_pasillo_quota_lock_expected")
    assert lock.state == "unknown"
    assert lock.attributes["affected"] is False

    # Remembered across a restart of the integration, and nothing to prevent at night.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data._immune == {"10": "2.0.8_0016"}  # noqa: SLF001
    snapshot = entry.runtime_data.data[10]
    assert snapshot.immune
    assert bulb.reboots == 0


async def test_just_past_the_limit_is_not_enough_to_learn(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=LIMIT + 60, power="off", affected=False)
    add_yeelight(hass, 10, "127.0.0.2", "Led pasillo")
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    assert not entry.runtime_data.data[10].immune


async def test_a_firmware_update_is_learnt_again(hass: HomeAssistant, bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=60 * DAY, affected=False)
    add_yeelight(hass, 10, "127.0.0.2", "Led pasillo")
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    assert entry.runtime_data.data[10].immune
    bulb.firmware = "2.0.9_0001"
    bulb.life = 3 * DAY
    bulb._boot = bulb._boot + 57 * DAY  # noqa: SLF001 - it restarted after the update
    await _refresh(hass, entry)
    assert not entry.runtime_data.data[10].immune
    assert hass.states.get("sensor.led_pasillo_quota_lock_expected").state != "unknown"


async def test_a_silent_bulb_past_the_limit_is_neither_learnt_nor_restarted(hass: HomeAssistant, bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=60 * DAY, power="on", lan="silent")
    yeelight = add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    _core_light(hass, yeelight, "on")
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    assert bulb.lan_questions == 1
    assert not entry.runtime_data.data[10].immune
    assert bulb.reboots == 0


async def test_a_disabled_yeelight_bulb_is_left_out(hass: HomeAssistant, bulbs) -> None:
    """Led isla, 15-10-2026: disabled in Home Assistant and off the Wi-Fi."""
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=3 * DAY)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    isla = add_yeelight(hass, 11, "127.0.0.3", "Led isla")
    entry = guard_entry(hass, {10: TOKEN_A, 11: TOKEN_B})
    await _setup(hass, entry)
    registry = dr.async_get(hass)
    assert [d.name for d in dr.async_entries_for_config_entry(registry, entry.entry_id)] == ["Salón 1", "Led isla"]

    assert await hass.config_entries.async_set_disabled_by(isla.entry_id, ConfigEntryDisabler.USER)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [d.name for d in dr.async_entries_for_config_entry(registry, entry.entry_id)] == ["Salón 1"]
    assert hass.states.get("sensor.led_isla_powered_on_since") is None
    assert 11 not in entry.runtime_data.data


# ---- found in the review of 15-10-2026 ---------------------------------


async def test_one_bulb_answering_nonsense_does_not_take_the_others_down(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=3 * DAY)
    await bulbs(host="127.0.0.3", did=11, token=TOKEN_B, life=3 * DAY)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    add_yeelight(hass, 11, "127.0.0.3", "Salón 2")
    real_info = coordinator_module.MiioDevice.info

    async def info(self):
        if self.host == "127.0.0.3":
            raise AttributeError("'list' object has no attribute 'get'")
        return await real_info(self)

    entry = guard_entry(hass, {10: TOKEN_A, 11: TOKEN_B})
    with patch.object(coordinator_module.MiioDevice, "info", info):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data[10].reachable
    assert not entry.runtime_data.data[11].reachable


async def test_a_restart_whose_reply_is_lost_still_counts(hass: HomeAssistant, bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=LIMIT + 60, power="on", reboot_reply=False)
    add_yeelight(hass, 10, "127.0.0.2", "Cocina 1", **LOCKED)
    events = async_capture_events(hass, EVENT_RESTARTED)
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    assert bulb.reboots == 1
    assert [e.data["result"] for e in events] == ["ok"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, "restart_failed_10") is None


async def test_a_prevention_is_called_off_if_the_light_came_on_meanwhile(hass: HomeAssistant, bulbs) -> None:
    # Off at the poll, on by the time its turn comes.
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=46 * DAY, power_sequence=["off", "on"])
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    entry = guard_entry(hass, {10: TOKEN_A}, window_start="00:00:00", window_end="00:00:00")  # all day
    await _setup(hass, entry)
    assert bulb.reboots == 0


async def test_a_prevention_goes_ahead_when_nothing_changed(hass: HomeAssistant, bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=46 * DAY, power="off")
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    await _setup(hass, guard_entry(hass, {10: TOKEN_A}, window_start="00:00:00", window_end="00:00:00"))
    assert bulb.reboots == 1
    assert bulb.power == "off"


async def test_the_failed_restart_issue_goes_when_it_was_power_cycled(
    hass: HomeAssistant, bulbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coordinator_module, "RESTART_WAIT", timedelta(seconds=1))
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=LIMIT + 60, reboot_works=False)
    add_yeelight(hass, 10, "127.0.0.2", "Recibidor", **LOCKED)
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "restart_failed_10") is not None

    bulb._boot = time.monotonic()  # noqa: SLF001 - switched off and on at the wall
    await _refresh(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "restart_failed_10") is None


async def test_the_last_restart_survives_a_reload(hass: HomeAssistant, bulbs, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bulb that ignores the order is not ordered again at every start."""
    monkeypatch.setattr(coordinator_module, "RESTART_WAIT", timedelta(seconds=1))
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=LIMIT + 60, reboot_works=False)
    add_yeelight(hass, 10, "127.0.0.2", "Recibidor", **LOCKED)
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    assert bulb.reboots == 1
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert bulb.reboots == 1
    assert entry.runtime_data.data[10].last_restart is not None


async def test_no_answer_at_all_is_not_counted_as_a_restart(hass: HomeAssistant, bulbs) -> None:
    add_yeelight(hass, 10, "127.0.0.9", "Pasillo")  # nothing at that address
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    entry.runtime_data._states[10] = coordinator_module.BulbState(did=10, name="Pasillo", host="127.0.0.9")  # noqa: SLF001
    with pytest.raises(coordinator_module.RestartError):
        await entry.runtime_data.async_restart(10, coordinator_module.Reason.MANUAL)
    assert entry.runtime_data.data[10].last_restart is None


async def test_no_uptime_reported_means_nothing_to_decide(hass: HomeAssistant, bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=LIMIT + 60, report_life=False)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1", **LOCKED)
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    assert entry.runtime_data.data[10].life_s is None
    assert hass.states.get("sensor.salon_1_powered_on_since").state == "unknown"
    assert bulb.reboots == 0


async def test_issues_are_not_rewritten_every_poll(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=DAY)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    add_yeelight(hass, 11, "127.0.0.3", "Salón 2", **LOCKED)
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    first = ir.async_get(hass).async_get_issue(DOMAIN, "missing_token")
    for _ in range(5):
        await _refresh(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missing_token") is first


async def test_a_bulb_enabled_again_gets_its_entities(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=DAY)
    isla = add_yeelight(hass, 10, "127.0.0.2", "Led isla")
    assert await hass.config_entries.async_set_disabled_by(isla.entry_id, ConfigEntryDisabler.USER)
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    assert hass.states.get("button.led_isla_restart") is None

    await hass.config_entries.async_set_disabled_by(isla.entry_id, None)
    await _refresh(hass, entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("button.led_isla_restart") is not None


async def test_removing_the_integration_leaves_nothing_behind(hass: HomeAssistant, bulbs, hass_storage) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=60 * DAY, affected=False)
    add_yeelight(hass, 10, "127.0.0.2", "Led pasillo")
    add_yeelight(hass, 11, "127.0.0.3", "Salón 2", **LOCKED)
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    key = f"{DOMAIN}.{entry.entry_id}"
    assert key in hass_storage
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missing_token") is not None

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
    assert [i for d, i in ir.async_get(hass).issues if d == DOMAIN] == []


async def test_diagnostics_hide_tokens_and_addresses(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=DAY)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    entry = guard_entry(hass, {10: TOKEN_A})
    await _setup(hass, entry)
    text = str(await async_get_config_entry_diagnostics(hass, entry))
    assert TOKEN_A not in text and "127.0.0.2" not in text
