"""Every restart in the logbook, on the bulb's own device page.

The event alone is invisible unless someone listens for it: on 06-11-2026 a
user looked for it in the logbook, found nothing, and could not tell whether
the guard had restarted his bulb (it had)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_RESTARTED

_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "repair": "was restarted because it had stopped answering Home Assistant",
        "prevent": "was restarted before reaching the 49.7-day lock",
        "manual": "was restarted with its Restart button",
        "ok": "it is back",
        "power_restored": "it is back, and its light was put back as it was",
        "power_changed": "it is back, but its light could not be put back as it was",
        "power_unknown": "it is back, but it did not say whether its light is on",
        "no_answer": "did not answer, so it was not restarted",
        "not_back": "it did not come back; look at Settings → Repairs",
    },
    "es": {
        "repair": "se reinició porque había dejado de contestar a Home Assistant",
        "prevent": "se reinició antes de llegar al bloqueo de los 49,7 días",
        "manual": "se reinició con su botón Reiniciar",
        "ok": "ya ha vuelto",
        "power_restored": "ya ha vuelto, y su luz se dejó como estaba",
        "power_changed": "ya ha vuelto, pero su luz no se pudo dejar como estaba",
        "power_unknown": "ya ha vuelto, pero no dijo si su luz está encendida",
        "no_answer": "no contestó, así que no se reinició",
        "not_back": "no ha vuelto; mira en Ajustes → Reparaciones",
    },
}


def restart_message(language: str, reason: str, result: str) -> str:
    texts = _TEXTS.get((language or "en").split("-")[0], _TEXTS["en"])
    what = texts.get(reason, reason)
    how = texts.get(result, result)
    # "no_answer": the order never went out, so "was restarted" would be false.
    # The logbook puts the bulb's name first: "Lamp15 did not answer, …".
    if result == "no_answer":
        return how
    return f"{what}: {how}"


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None],
) -> None:
    @callback
    def describe(event: Event) -> dict[str, Any]:
        data = event.data
        return {
            LOGBOOK_ENTRY_NAME: data.get("bulb") or "Yeelight",
            LOGBOOK_ENTRY_MESSAGE: restart_message(
                hass.config.language, str(data.get("reason", "")), str(data.get("result", ""))
            ),
        }

    async_describe_event(DOMAIN, EVENT_RESTARTED, describe)
