"""Diagnostics download, with every token and address redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN
from .coordinator import GuardConfigEntry

TO_REDACT = {CONF_TOKEN, "host"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: GuardConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "options": dict(entry.options),
        "bulbs": async_redact_data(dict(entry.data), TO_REDACT),
        "states": async_redact_data(
            {
                str(did): {k: str(v) if v is not None else None for k, v in asdict(state).items()}
                for did, state in coordinator.snapshot().items()
            },
            TO_REDACT,
        ),
    }
