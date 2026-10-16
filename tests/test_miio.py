"""The miIO client against the strict fake bulb."""

from __future__ import annotations

import pytest

from custom_components.yeelight_quota_guard.miio import (
    MiioDevice,
    MiioTimeout,
    MiioTokenError,
    MiioWrongDevice,
    parse_token,
)

from .conftest import TOKEN_A, TOKEN_B


async def test_reads_info_and_power(bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=307091595, token=TOKEN_A, life=4327918, power="on")
    device = MiioDevice("127.0.0.2", TOKEN_A, 307091595)
    info = await device.info()
    assert info["life"] >= 4327918
    assert await device.power() == "on"
    assert device.did == 307091595
    assert bulb.methods == ["miIO.info", "get_prop"]


async def test_wrong_token_is_told_apart_from_silence(bulbs) -> None:
    await bulbs(host="127.0.0.2", did=1, token=TOKEN_A)
    with pytest.raises(MiioTokenError):
        await MiioDevice("127.0.0.2", TOKEN_B, 1, timeout=0.3).info()


async def test_nobody_there(bulbs) -> None:
    with pytest.raises(MiioTimeout):
        await MiioDevice("127.0.0.9", TOKEN_A, 1, timeout=0.3).info()


async def test_never_talks_to_another_device(bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=111, token=TOKEN_A)
    with pytest.raises(MiioWrongDevice):
        await MiioDevice("127.0.0.2", TOKEN_A, 222).reboot()
    assert bulb.reboots == 0


async def test_reboot_resets_life_and_keeps_power(bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=5, token=TOKEN_A, life=4400000, power="off")
    await MiioDevice("127.0.0.2", TOKEN_A, 5).reboot()
    assert bulb.reboots == 1
    assert bulb.uptime < 5
    assert bulb.power == "off"


async def test_request_ids_stay_between_1_and_999(bulbs) -> None:
    bulb = await bulbs(host="127.0.0.2", did=5, token=TOKEN_A)
    device = MiioDevice("127.0.0.2", TOKEN_A, 5)
    device._id = 998  # noqa: SLF001
    for _ in range(3):
        await device.power()
    assert bulb.ids == [999, 1, 2]


@pytest.mark.parametrize("token", ["", "abc", "z" * 32, "0" * 31, "0" * 33])
def test_bad_tokens_are_refused(token: str) -> None:
    with pytest.raises(ValueError):
        parse_token(token)


def test_token_is_trimmed_and_case_insensitive() -> None:
    assert parse_token(f"  {TOKEN_A.upper()} ") == bytes.fromhex(TOKEN_A)
