"""When each bulb was powered on, and when it will lock if nothing restarts it."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import GuardConfigEntry
from .entity import GuardEntity, guarded_links
from .guard import quota_lock_at

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GuardConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for link in guarded_links(coordinator):
        entities.append(PoweredOnSensor(coordinator, link, "powered_on"))
        entities.append(QuotaLockSensor(coordinator, link, "quota_lock"))
    async_add_entities(entities)


class PoweredOnSensor(GuardEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> datetime | None:
        return self.bulb.powered_on if self.bulb else None


class QuotaLockSensor(GuardEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> datetime | None:
        # A bulb seen working past the limit has no lock to expect.
        if self.bulb is None or self.bulb.powered_on is None or self.bulb.immune:
            return None
        return quota_lock_at(self.bulb.powered_on)

    @property
    def extra_state_attributes(self) -> dict[str, bool | None]:
        """affected: False once seen working past the limit; None until then."""
        return {"affected": False if self.bulb and self.bulb.immune else None}
