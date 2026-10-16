"""Shared base: our entities live on the bulb's own Yeelight device."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import YEELIGHT_DOMAIN
from .coordinator import BulbState, GuardCoordinator
from .yeelight_link import YeelightBulb


class GuardEntity(CoordinatorEntity[GuardCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: GuardCoordinator, link: YeelightBulb, key: str) -> None:
        super().__init__(coordinator)
        self.did = link.did
        self._attr_translation_key = key
        self._attr_unique_id = f"{link.did}_{key}"
        # Since 2026.9 every config entry has its own device; sharing the core
        # Yeelight identifiers is how Home Assistant knows both are the same
        # bulb, and nothing the core integration wrote is touched.
        self._attr_device_info = DeviceInfo(
            identifiers={(YEELIGHT_DOMAIN, link.device_identifier)},
            name=link.name,
            manufacturer="Yeelight",
        )

    @property
    def bulb(self) -> BulbState | None:
        return (self.coordinator.data or {}).get(self.did)

    @property
    def available(self) -> bool:
        return super().available and self.bulb is not None and self.bulb.reachable


def guarded_links(coordinator: GuardCoordinator) -> list[YeelightBulb]:
    """Bulbs with a token whose Yeelight entry still exists and is enabled."""
    links = coordinator.links()
    return [links[did] for did in coordinator.tokens if did in links]
