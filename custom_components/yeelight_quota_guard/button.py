"""A restart button on each bulb, over miIO: works even while it is locked."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import burburito
from .const import DOMAIN
from .coordinator import GuardConfigEntry, RestartError
from .entity import GuardEntity, guarded_links
from .guard import Reason

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GuardConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(RestartButton(coordinator, link, "restart") for link in guarded_links(coordinator))


class RestartButton(GuardEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_restart(self.did, Reason.MANUAL)
        except RestartError as err:
            footer = burburito.footer(err.key, self.hass.config.language)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="restart_failed",
                translation_placeholders={
                    "bulb": self.bulb.name if self.bulb else str(self.did),
                    **footer,
                },
            ) from err
