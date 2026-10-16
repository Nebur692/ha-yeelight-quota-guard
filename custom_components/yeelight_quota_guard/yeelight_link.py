"""The bulbs the core Yeelight integration already knows.

The guard does not discover anything on its own: it watches the bulbs that are
set up in Home Assistant's Yeelight integration. Each of those config entries
stores the bulb id ("0x00000000124dd88b"), and that id is the same number the
bulb gives as its miIO device id. That is how a token is tied to a bulb
without trusting an IP address.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import YEELIGHT_DOMAIN, YEELIGHT_STUCK_REASON


@dataclass(frozen=True)
class YeelightBulb:
    did: int
    host: str | None
    name: str
    model: str | None
    entry_id: str
    device_identifier: str
    state: ConfigEntryState
    reason: str | None

    @property
    def stuck(self) -> bool:
        """The core integration is retrying with the quota-lock symptom."""
        return self.state is ConfigEntryState.SETUP_RETRY and YEELIGHT_STUCK_REASON in (self.reason or "").lower()


def parse_did(value: object) -> int | None:
    """'0x00000000124dd88b', '0x189beea6' or '307091595' → int."""
    if value is None:
        return None
    text = str(value).strip().lower()
    try:
        return int(text, 16) if text.startswith("0x") else int(text)
    except ValueError:
        return None


def _from_entry(entry: ConfigEntry) -> YeelightBulb | None:
    did = parse_did(entry.data.get("id") or entry.unique_id)
    if did is None:
        return None
    return YeelightBulb(
        did=did,
        host=entry.data.get("host"),
        name=entry.title,
        model=entry.options.get("model") or entry.data.get("detected_model"),
        entry_id=entry.entry_id,
        # Same identifier the core Yeelight entities register their device with,
        # so our entities land on the bulb's own device page.
        device_identifier=entry.unique_id or entry.entry_id,
        state=entry.state,
        reason=entry.reason,
    )


def yeelight_bulbs(hass: HomeAssistant) -> dict[int, YeelightBulb]:
    bulbs: dict[int, YeelightBulb] = {}
    for entry in hass.config_entries.async_entries(YEELIGHT_DOMAIN):
        # Disabled by the person: not to be guarded, listed or restarted.
        if entry.disabled_by is not None:
            continue
        if (bulb := _from_entry(entry)) is not None:
            bulbs[bulb.did] = bulb
    return bulbs
