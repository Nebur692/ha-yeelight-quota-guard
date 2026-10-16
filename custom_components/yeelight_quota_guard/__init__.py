"""Yeelight Quota Guard.

Keeps Yeelight bulbs out of the firmware bug that locks their LAN control
after 49.7 days of uptime ("Could not fetch initial state; try power cycling
the device"), by restarting them over miIO before or right after it happens.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .areas import async_copy_missing_areas, async_follow_area_changes, async_remove_unguarded
from .coordinator import GuardConfigEntry, GuardCoordinator, clear_issues, store_key
from .entity import guarded_links

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: GuardConfigEntry) -> bool:
    coordinator = GuardCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # The platforms have registered our devices by now.
    async_remove_unguarded(hass, entry.entry_id, {link.device_identifier for link in guarded_links(coordinator)})
    async_copy_missing_areas(hass, entry.entry_id)
    entry.async_on_unload(async_follow_area_changes(hass, entry.entry_id))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GuardConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: GuardConfigEntry) -> None:
    """Removed for good: leave nothing behind, neither stored data nor issues."""
    await Store(hass, 1, store_key(entry.entry_id)).async_remove()
    clear_issues(hass)
