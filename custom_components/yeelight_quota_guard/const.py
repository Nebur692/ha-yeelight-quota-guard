"""Constants."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "yeelight_quota_guard"
YEELIGHT_DOMAIN: Final = "yeelight"

CONF_BULBS: Final = "bulbs"
CONF_TOKEN: Final = "token"
CONF_DID: Final = "did"
CONF_COUNTRY: Final = "country"

OPT_REPAIR: Final = "repair"
OPT_PREVENT: Final = "prevent"
OPT_PREVENT_AFTER_DAYS: Final = "prevent_after_days"
OPT_WINDOW_START: Final = "window_start"
OPT_WINDOW_END: Final = "window_end"
OPT_ONLY_WHEN_OFF: Final = "only_when_off"
OPT_SCAN_MINUTES: Final = "scan_minutes"

DEFAULT_SCAN_MINUTES: Final = 15
DEFAULT_OPTIONS: Final = {
    OPT_REPAIR: True,
    OPT_PREVENT: True,
    OPT_PREVENT_AFTER_DAYS: 45,
    OPT_WINDOW_START: "03:00:00",
    OPT_WINDOW_END: "05:00:00",
    OPT_ONLY_WHEN_OFF: True,
    OPT_SCAN_MINUTES: DEFAULT_SCAN_MINUTES,
}

# Xiaomi cloud regions, as the Mi Home app calls them.
COUNTRIES: Final = ["cn", "de", "i2", "ru", "sg", "us"]

# The core Yeelight integration's setup error for a bulb that does not answer
# its first LAN read: exactly what a quota-locked bulb causes.
YEELIGHT_STUCK_REASON: Final = "power cycling"

# How long a restarted bulb gets to come back before we call it failed.
RESTART_WAIT: Final = timedelta(seconds=90)
RESTART_POLL_SECONDS: Final = 5.0
# A token that fails this many polls in a row is reported; one miss is Wi-Fi.
TOKEN_FAILURES_BEFORE_ISSUE: Final = 3

EVENT_RESTARTED: Final = f"{DOMAIN}_restarted"
