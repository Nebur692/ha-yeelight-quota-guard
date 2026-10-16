"""Our device for each bulb: in the right area, and gone when the bulb is no longer guarded.

Since 2026.9 every config entry has its own device, so ours starts with no
area and Home Assistant would ask for it bulb by bulb. The Yeelight device
already knows where the bulb is: copy it when ours has none, and follow it when
it moves, unless the person gave ours a different area on purpose.
"""

from __future__ import annotations

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import YEELIGHT_DOMAIN
from .yeelight_link import yeelight_bulbs


def _pairs(hass: HomeAssistant, entry_id: str) -> list[tuple[dr.DeviceEntry, dr.DeviceEntry]]:
    """(ours, Yeelight's) for every bulb that has both devices."""
    registry = dr.async_get(hass)
    ours = {
        ident: device
        for device in dr.async_entries_for_config_entry(registry, entry_id)
        for ident in device.identifiers
        if ident[0] == YEELIGHT_DOMAIN
    }
    pairs = []
    for link in yeelight_bulbs(hass).values():
        mine = ours.get((YEELIGHT_DOMAIN, link.device_identifier))
        if mine is None:
            continue
        for theirs in dr.async_entries_for_config_entry(registry, link.entry_id):
            if (YEELIGHT_DOMAIN, link.device_identifier) in theirs.identifiers:
                pairs.append((mine, theirs))
                break
    return pairs


@callback
def async_copy_missing_areas(hass: HomeAssistant, entry_id: str) -> int:
    """Give each of our devices with no area the area of its Yeelight device."""
    registry = dr.async_get(hass)
    copied = 0
    for mine, theirs in _pairs(hass, entry_id):
        if mine.area_id is None and theirs.area_id is not None:
            registry.async_update_device(mine.id, area_id=theirs.area_id)
            copied += 1
    return copied


@callback
def async_follow_area_changes(hass: HomeAssistant, entry_id: str) -> CALLBACK_TYPE:
    """When a Yeelight device moves, move ours too, if it was in the same place."""

    @callback
    def _moved(event: Event[dr.EventDeviceRegistryUpdatedData]) -> None:
        if event.data["action"] != "update" or "area_id" not in event.data["changes"]:
            return
        registry = dr.async_get(hass)
        for mine, theirs in _pairs(hass, entry_id):
            if theirs.id != event.data["device_id"]:
                continue
            old_area = event.data["changes"]["area_id"]
            # Ours was left where theirs used to be, or never had one: follow.
            # An area the person chose for ours on purpose is kept.
            if mine.area_id in (old_area, None) and mine.area_id != theirs.area_id:
                registry.async_update_device(mine.id, area_id=theirs.area_id)

    return hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, _moved)


@callback
def async_remove_unguarded(hass: HomeAssistant, entry_id: str, guarded: set[str]) -> int:
    """Drop our devices (and their entities) for bulbs no longer guarded: their
    Yeelight entry was disabled or removed, or their token taken out."""
    registry = dr.async_get(hass)
    removed = 0
    for device in dr.async_entries_for_config_entry(registry, entry_id):
        if not any(ident[0] == YEELIGHT_DOMAIN and ident[1] in guarded for ident in device.identifiers):
            registry.async_remove_device(device.id)
            removed += 1
    return removed
