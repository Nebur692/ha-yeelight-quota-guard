"""Setup and options: every path a person can take."""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest
import requests
import requests_mock
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.yeelight_quota_guard.burburito import UNCATALOGUED
from custom_components.yeelight_quota_guard.const import CONF_BULBS, DOMAIN

from .conftest import TOKEN_A, TOKEN_B, add_yeelight, guard_entry
from .fake_xiaomi import JPEG, FakeXiaomi


@pytest.fixture
def no_setup():
    """The entry a flow creates is not set up: these tests are about the flow."""
    with patch("custom_components.yeelight_quota_guard.async_setup_entry", return_value=True) as setup:
        yield setup


async def _start(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_needs_the_yeelight_integration(hass: HomeAssistant) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_yeelight"


async def test_manual_path(hass: HomeAssistant, bulbs, no_setup) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A)
    await bulbs(host="127.0.0.3", did=11, token=TOKEN_B)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    add_yeelight(hass, 11, "127.0.0.3", "Salón 2")

    result = await _start(hass)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    assert result["step_id"] == "manual"

    # Bad format, then the other bulb's token, then the right one.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"bulb": "10", "token": "nope"})
    assert result["errors"] == {"base": "invalid_token"}
    assert result["description_placeholders"]["code"] == "027-002-007-061"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"bulb": "10", "token": TOKEN_B})
    assert result["errors"] == {"base": "wrong_token"}
    assert result["description_placeholders"]["code"] == "027-002-007-041"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"bulb": "10", "token": TOKEN_A})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "manual_more"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"bulb": "11", "token": TOKEN_B})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BULBS] == {"10": {"token": TOKEN_A}, "11": {"token": TOKEN_B}}


async def test_manual_bulb_that_does_not_answer(hass: HomeAssistant, bulbs) -> None:
    add_yeelight(hass, 10, "127.0.0.9", "Pasillo")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"bulb": "10", "token": TOKEN_A})
    assert result["errors"] == {"base": "cannot_reach_bulb"}


async def test_manual_address_taken_by_another_device(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=99, token=TOKEN_A)
    add_yeelight(hass, 10, "127.0.0.2", "Pasillo")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"bulb": "10", "token": TOKEN_A})
    assert result["errors"] == {"base": "wrong_device"}


CLOUD_FORM = {"username": "a@b.c", "password": "secret", "country": "de"}
DEVICES = [
    {"did": "10", "model": "yeelink.light.colorc", "name": "Salón A", "localip": "127.0.0.2", "token": TOKEN_A},
    {"did": "77", "model": "yeelink.light.color4", "name": "Not in HA", "token": TOKEN_B},
]


def _choices(result: dict) -> list[tuple[str, str]]:
    """The checkbox list as the screen draws it: value and label, in order."""
    selector = result["data_schema"].schema["watch"]
    return [(o["value"], o["label"]) for o in selector.config["options"]]


async def _cloud_form(hass: HomeAssistant) -> dict:
    result = await _start(hass)
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "cloud"})


async def test_cloud_path_keeps_only_bulbs_home_assistant_knows(hass: HomeAssistant, no_setup) -> None:
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    add_yeelight(hass, 11, "127.0.0.3", "Trastero")
    result = await _cloud_form(hass)
    with FakeXiaomi(devices=DEVICES):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD_FORM)
    assert result["step_id"] == "cloud_done"
    placeholders = result["description_placeholders"]
    assert {k: placeholders[k] for k in ("found", "total", "without_count", "without")} == {
        "found": "1",
        "total": "2",
        "without_count": "1",
        "without": "\n- Trastero",
    }
    assert _choices(result) == [("10", "Salón 1")]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BULBS] == {"10": {"token": TOKEN_A}}
    assert "secret" not in str(result["data"]) and "secret" not in str(result["options"])


async def test_cloud_captcha_then_email_code(hass: HomeAssistant, no_setup) -> None:
    """The sign-in Xiaomi asked for on 15-10-2026: captcha, and a code by email."""
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    result = await _cloud_form(hass)
    with FakeXiaomi(captcha="K7TQ", code_by="email", devices=DEVICES):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD_FORM)
        assert result["step_id"] == "cloud_captcha" and not result["errors"]
        assert (
            result["description_placeholders"]["image"] == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
        )

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "nope"})
        assert result["step_id"] == "cloud_captcha"
        assert result["errors"] == {"base": "captcha_wrong"}
        assert result["description_placeholders"]["code"] == "027-002-003-061"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "K7TQ"})
        assert result["step_id"] == "cloud_code" and not result["errors"]
        assert result["description_placeholders"]["address"] == "jo***@example.com"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
        assert result["step_id"] == "cloud_code"
        assert result["errors"] == {"base": "code_wrong"}
        assert result["description_placeholders"]["code"] == "027-002-003-043"
        assert result["description_placeholders"]["address"] == "jo***@example.com"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["step_id"] == "cloud_done"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"][CONF_BULBS] == {"10": {"token": TOKEN_A}}


@pytest.mark.parametrize(
    ("xiaomi", "password", "error", "code"),
    [
        ({"code_by": "email", "identity_flag": 16}, "secret", "verification", "027-002-003-021"),
        ({}, "wrong", "invalid_auth", "027-002-003-041"),
        ({"refuse_with": 70022}, "secret", "cloud_refused", "027-002-003-022"),
    ],
)
async def test_cloud_errors(hass: HomeAssistant, xiaomi, password, error, code) -> None:
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    result = await _cloud_form(hass)
    with FakeXiaomi(**xiaomi):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {**CLOUD_FORM, "password": password})
    assert result["step_id"] == "cloud"
    assert result["errors"] == {"base": error}
    assert result["description_placeholders"]["code"] == code != UNCATALOGUED
    if error == "verification":
        assert result["description_placeholders"]["url"].startswith("https://account.xiaomi.com/")


async def test_cloud_unreachable(hass: HomeAssistant) -> None:
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    result = await _cloud_form(hass)
    with requests_mock.Mocker() as m:
        m.get(requests_mock.ANY, exc=requests.ConnectionError("down"))
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD_FORM)
    assert result["errors"] == {"base": "cannot_connect"}
    assert result["description_placeholders"]["code"] == "027-002-003-001"


async def test_cloud_with_no_matching_bulb(hass: HomeAssistant) -> None:
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    result = await _cloud_form(hass)
    with FakeXiaomi(devices=DEVICES[1:]):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD_FORM)
    assert result["errors"] == {"base": "no_bulbs"}


async def test_only_one_instance(hass: HomeAssistant) -> None:
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    guard_entry(hass, {})
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_settings_and_manual_token(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    entry = guard_entry(hass, {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "repair": True,
            "prevent": False,
            "prevent_after_days": 40.0,
            "window_start": "02:00:00",
            "window_end": "04:00:00",
            "only_when_off": False,
            "scan_minutes": 30.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["prevent_after_days"] == 40 and entry.options["scan_minutes"] == 30

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "manual"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"bulb": "10", "token": TOKEN_A})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_BULBS] == {"10": {"token": TOKEN_A}}
    assert entry.options["prevent_after_days"] == 40  # adding a token kept the settings
    # Review, 15-10-2026: the new bulb got no button or sensors until Home
    # Assistant restarted, because only an options change reloads by itself.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("button.salon_1_restart") is not None


async def test_options_cloud_with_captcha(hass: HomeAssistant, bulbs) -> None:
    await bulbs(host="127.0.0.2", did=10, token=TOKEN_A)
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    entry = guard_entry(hass, {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    with FakeXiaomi(captcha="K7TQ", devices=DEVICES):
        result = await hass.config_entries.options.async_configure(result["flow_id"], CLOUD_FORM)
        assert result["step_id"] == "cloud_captcha"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"code": "K7TQ"})
    assert result["step_id"] == "cloud_done"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert entry.data[CONF_BULBS] == {"10": {"token": TOKEN_A}}

    # Running it again with every bulb unticked is refused, not saved as "guard none".
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    with FakeXiaomi(devices=DEVICES):
        result = await hass.config_entries.options.async_configure(result["flow_id"], CLOUD_FORM)
    added = await hass.config_entries.options.async_configure(result["flow_id"], {"watch": []})
    assert added["errors"] == {"base": "nothing_chosen"}


async def test_cloud_failure_nobody_expected_still_has_a_message(hass: HomeAssistant) -> None:
    """15-10-2026: a crash in the last step showed "Unknown error occurred" and no code."""
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    result = await _cloud_form(hass)
    with patch(
        "custom_components.yeelight_quota_guard.config_flow.XiaomiLogin.start", side_effect=RuntimeError("boom")
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD_FORM)
    assert result["errors"] == {"base": "cloud_refused"}
    assert result["description_placeholders"]["code"] == "027-002-003-022"


async def test_cloud_done_untick_and_none_ticked(hass: HomeAssistant, no_setup) -> None:
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    add_yeelight(hass, 11, "127.0.0.3", "Cocina")
    devices = [*DEVICES, {"did": "11", "model": "yeelink.light.color4", "name": "x", "token": TOKEN_B}]
    result = await _cloud_form(hass)
    with FakeXiaomi(devices=devices):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD_FORM)
    assert _choices(result) == [("11", "Cocina"), ("10", "Salón 1")]
    assert result["description_placeholders"]["without_count"] == "0"
    assert result["description_placeholders"]["without"] == ""

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"watch": []})
    assert result["errors"] == {"base": "nothing_chosen"}
    assert result["description_placeholders"]["code"] == "027-002-007-100"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"watch": ["11"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BULBS] == {"11": {"token": TOKEN_B}}


async def test_cloud_done_scales_to_many_bulbs(hass: HomeAssistant, no_setup) -> None:
    """60 bulbs: one line each, alphabetical, all ticked by default."""
    devices = []
    for n in range(60):
        add_yeelight(hass, 100 + n, f"127.0.1.{n}", f"Bombilla {n:02d}")
        devices.append({"did": str(100 + n), "model": "yeelink.light.colorc", "name": "x", "token": TOKEN_A})
    result = await _cloud_form(hass)
    with FakeXiaomi(devices=devices):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD_FORM)
    choices = _choices(result)
    assert len(choices) == 60
    assert [label for _, label in choices] == sorted(label for _, label in choices)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert len(result["data"][CONF_BULBS]) == 60


async def test_another_server_after_no_bulbs_does_not_sign_in_again(hass: HomeAssistant, no_setup) -> None:
    add_yeelight(hass, 10, "127.0.0.2", "Salón 1")
    result = await _cloud_form(hass)
    with FakeXiaomi(captcha="K7TQ", devices=DEVICES) as xiaomi:
        xiaomi.devices = []
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD_FORM)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "K7TQ"})
        assert result["errors"] == {"base": "no_bulbs"}
        xiaomi.devices = DEVICES
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {**CLOUD_FORM, "country": "us"})
    assert result["step_id"] == "cloud_done"
    assert xiaomi.captchas_served == 1  # no second captcha
    assert xiaomi.signed_in_as == {"country": "us"}
