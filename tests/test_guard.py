"""The restart policy, case by case."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.yeelight_quota_guard.guard import (
    MIN_GAP_BETWEEN_RESTARTS,
    QUOTA_LIMIT_S,
    Reason,
    Settings,
    Snapshot,
    decide,
    in_window,
    quota_lock_at,
    stable_boot_time,
)

DAY = 86400
NIGHT = datetime(2026, 10, 16, 3, 30, tzinfo=UTC)  # inside the default 03:00-05:00
NOON = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
SETTINGS = Settings()


def test_the_limit_is_two_to_the_32_milliseconds() -> None:
    # 49 days, 17 h, 2 min, 47.296 s: what the 16 real bulbs hit on 15-10-2026.
    assert QUOTA_LIMIT_S == 4294967.296
    assert timedelta(seconds=QUOTA_LIMIT_S) == timedelta(days=49, hours=17, minutes=2, seconds=47.296)


@pytest.mark.parametrize(
    ("life", "power", "now", "locked", "expected"),
    [
        (None, "off", NIGHT, False, None),  # never read
        (0, "off", NIGHT, False, None),  # just powered on
        (44 * DAY, "off", NIGHT, False, None),  # below the prevention threshold
        (45 * DAY, "off", NIGHT, False, Reason.PREVENT),  # exactly the threshold
        (45 * DAY, "on", NIGHT, False, None),  # on: wait for it to be off
        (45 * DAY, None, NIGHT, False, None),  # power unknown counts as not off
        (45 * DAY, "off", NOON, False, None),  # outside the window
        (int(QUOTA_LIMIT_S), "on", NOON, True, None),  # one second short of the lock
        (int(QUOTA_LIMIT_S) + 1, "on", NOON, True, Reason.REPAIR),  # locked: any hour, on or off
        (60 * DAY, "off", NIGHT, True, Reason.REPAIR),  # repair wins over prevent
        # 15-10-2026: past the limit but NOT locked (strips, color2 never lock).
        # Nine of those were restarted at noon for nothing. Only the night,
        # off, prevention may touch them.
        (int(QUOTA_LIMIT_S) + 1, "on", NOON, False, None),
        (60 * DAY, "off", NOON, False, None),
        (60 * DAY, "off", NIGHT, False, Reason.PREVENT),
    ],
)
def test_decide(life, power, now, locked, expected) -> None:
    assert decide(Snapshot(life, power, locked=locked), SETTINGS, now) is expected


def test_repair_off_falls_back_to_prevention_in_the_window() -> None:
    settings = Settings(repair=False)
    assert decide(Snapshot(60 * DAY, "on"), settings, NOON) is None
    assert decide(Snapshot(60 * DAY, "off"), settings, NIGHT) is Reason.PREVENT


def test_prevent_off_only_repairs() -> None:
    settings = Settings(prevent=False)
    assert decide(Snapshot(46 * DAY, "off"), settings, NIGHT) is None
    assert decide(Snapshot(60 * DAY, "off", locked=True), settings, NIGHT) is Reason.REPAIR


def test_only_when_off_disabled_restarts_a_light_that_is_on() -> None:
    settings = Settings(only_when_off=False)
    assert decide(Snapshot(46 * DAY, "on"), settings, NIGHT) is Reason.PREVENT


def test_no_restart_loop() -> None:
    locked = Snapshot(60 * DAY, "off", last_restart=NOON - MIN_GAP_BETWEEN_RESTARTS + timedelta(minutes=1), locked=True)
    assert decide(locked, SETTINGS, NOON) is None
    later = Snapshot(60 * DAY, "off", last_restart=NOON - MIN_GAP_BETWEEN_RESTARTS, locked=True)
    assert decide(later, SETTINGS, NOON) is Reason.REPAIR


@pytest.mark.parametrize(
    ("now", "start", "end", "inside"),
    [
        (time(3, 0), time(3, 0), time(5, 0), True),  # start included
        (time(5, 0), time(3, 0), time(5, 0), False),  # end excluded
        (time(2, 59), time(3, 0), time(5, 0), False),
        (time(23, 30), time(23, 0), time(2, 0), True),  # across midnight
        (time(1, 0), time(23, 0), time(2, 0), True),
        (time(12, 0), time(23, 0), time(2, 0), False),
        (time(12, 0), time(4, 0), time(4, 0), True),  # same start and end: all day
    ],
)
def test_in_window(now, start, end, inside) -> None:
    assert in_window(now, start, end) is inside


def test_boot_time_ignores_jitter_and_follows_a_real_restart() -> None:
    first = datetime(2026, 8, 26, 18, 10, 5, 123456, tzinfo=UTC)
    kept = stable_boot_time(None, first)
    assert kept == first.replace(microsecond=0)
    assert stable_boot_time(kept, first + timedelta(seconds=2)) == kept
    restarted = first + timedelta(days=3)
    assert stable_boot_time(kept, restarted) == restarted.replace(microsecond=0)


def test_quota_lock_time() -> None:
    boot = datetime(2026, 8, 26, 18, 10, tzinfo=UTC)
    # The 16 bulbs that booted then were found locked on 15-10-2026 around noon.
    assert quota_lock_at(boot) == datetime(2026, 10, 15, 11, 12, 47, 296000, tzinfo=UTC)


def test_a_bulb_known_not_to_lock_is_not_prevented() -> None:
    immune = Snapshot(60 * DAY, "off", immune=True)
    assert decide(immune, SETTINGS, NIGHT) is None
    # Evidence of a lock still wins: what was learnt does not hide a real one.
    assert decide(Snapshot(60 * DAY, "off", locked=True, immune=True), SETTINGS, NOON) is Reason.REPAIR
