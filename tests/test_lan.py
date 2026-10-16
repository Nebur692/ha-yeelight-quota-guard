"""The LAN question, against a fake bulb that answers like the real ones did on 15-10-2026."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.yeelight_quota_guard.lan import LanAnswer, _read, probe

from .conftest import LIMIT, TOKEN_A


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b'{"id":1,"result":["on"]}\r\n', LanAnswer.WORKS),  # Led pasillo, measured
        (b'{"id":1,"error":{"code":-1,"message":"client quota exceeded"}}\r\n', LanAnswer.LOCKED),
        (b'{"error":{"code":-1,"message":"client quota exceeded"}}\r\n', LanAnswer.LOCKED),  # no id
        (b'{"id":1,"error":{"code":-1,"message":"method not supported"}}\r\n', LanAnswer.UNKNOWN),
        (b'{"method":"props","params":{"power":"on"}}\r\n', None),  # a notification, not our answer
        (b'{"id":7,"result":["on"]}\r\n', None),  # someone else's answer
        (b"not json\r\n", None),
    ],
)
def test_read(line, expected) -> None:
    assert _read(line) is expected


async def test_probe_against_bulbs(bulbs) -> None:
    working = await bulbs(host="127.0.0.2", did=10, token=TOKEN_A, life=LIMIT + 60, affected=False)
    locked = await bulbs(host="127.0.0.3", did=11, token=TOKEN_A, life=LIMIT + 60)
    young = await bulbs(host="127.0.0.4", did=12, token=TOKEN_A, life=60)
    silent = await bulbs(host="127.0.0.5", did=13, token=TOKEN_A, life=LIMIT + 60, lan="silent")
    assert await probe(working.host) is LanAnswer.WORKS
    assert await probe(locked.host) is LanAnswer.LOCKED
    assert await probe(young.host) is LanAnswer.WORKS
    assert await probe(silent.host) is LanAnswer.UNKNOWN
    assert await probe("127.0.0.9") is LanAnswer.UNKNOWN  # nothing listening


async def test_probe_gives_up_in_time(bulbs, monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.yeelight_quota_guard import lan

    monkeypatch.setattr(lan, "TIMEOUT", 0.3)
    silent = await bulbs(host="127.0.0.5", did=13, token=TOKEN_A, life=LIMIT + 60, lan="silent")
    async with asyncio.timeout(2):
        assert await lan.probe(silent.host) is LanAnswer.UNKNOWN
