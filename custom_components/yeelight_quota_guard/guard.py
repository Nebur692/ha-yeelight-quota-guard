"""When to restart a bulb. Pure functions, no Home Assistant, no network.

The firmware bug: the timer that refills a Yeelight's LAN command quota stops
re-arming once the bulb has been powered for 2^32 ms (49 days, 17 h, 2 min,
47 s). From then on every LAN command, even a read, gets "client quota
exceeded" until the bulb restarts. Yeelight confirmed it on their forum in
2022 and never shipped a fix.

Two reasons to restart:
- repair: the bulb is already past the limit. Restart now, whatever the time:
  on the affected firmwares it no longer obeys Home Assistant anyway.
- prevent: the bulb is getting close (default 45 days). Restart inside a quiet
  window, and by default only while it is off, so nobody notices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum

QUOTA_LIMIT_S = 2**32 / 1000  # 4294967.296 s
QUOTA_LIMIT = timedelta(seconds=QUOTA_LIMIT_S)
# Working this long past the limit means the bug does not affect the bulb: the
# lock comes at 2^32 ms exactly, not a little later.
IMMUNE_AFTER_S = QUOTA_LIMIT_S + 10 * 60
# A restart that did not take, or a bulb that keeps hitting the limit, must not
# turn into a restart loop.
MIN_GAP_BETWEEN_RESTARTS = timedelta(hours=6)


class Reason(StrEnum):
    REPAIR = "repair"
    PREVENT = "prevent"
    MANUAL = "manual"


@dataclass(frozen=True)
class Settings:
    repair: bool = True
    prevent: bool = True
    prevent_after_days: int = 45
    window_start: time = time(3, 0)
    window_end: time = time(5, 0)
    only_when_off: bool = True


@dataclass(frozen=True)
class Snapshot:
    """What the last poll saw of one bulb."""

    life_s: int | None
    power: str | None
    last_restart: datetime | None = None
    # Evidence that the core Yeelight integration can no longer talk to it.
    # Past 2^32 ms alone is not enough: several models (strips, color2) pass
    # it without locking, and restarting those would be for nothing.
    locked: bool = False
    # Seen working past the limit: nothing to prevent.
    immune: bool = False


def in_window(now: time, start: time, end: time) -> bool:
    """Half-open [start, end). Crosses midnight when end <= start; start == end
    means the whole day."""
    if start == end:
        return True
    if start < end:
        return start <= now < end
    return now >= start or now < end


def decide(snapshot: Snapshot, settings: Settings, now: datetime) -> Reason | None:
    """The reason to restart this bulb now, or None."""
    if snapshot.life_s is None:
        return None
    if snapshot.last_restart is not None and now - snapshot.last_restart < MIN_GAP_BETWEEN_RESTARTS:
        return None
    if settings.repair and snapshot.life_s >= QUOTA_LIMIT_S and snapshot.locked:
        return Reason.REPAIR
    if (
        settings.prevent
        and not snapshot.immune
        and snapshot.life_s >= settings.prevent_after_days * 86400
        and in_window(now.time(), settings.window_start, settings.window_end)
        and (not settings.only_when_off or snapshot.power == "off")
    ):
        return Reason.PREVENT
    return None


def powered_on_at(now: datetime, life_s: int) -> datetime:
    return now - timedelta(seconds=life_s)


def stable_boot_time(previous: datetime | None, computed: datetime) -> datetime:
    """Keep the previous value unless the bulb really restarted.

    now - life jitters by a second or two between polls; publishing that would
    write a new sensor state every poll for nothing."""
    if previous is not None and abs((computed - previous).total_seconds()) < 120:
        return previous
    return computed.replace(microsecond=0)


def quota_lock_at(boot: datetime) -> datetime:
    return boot + QUOTA_LIMIT
