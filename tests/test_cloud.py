"""Reading tokens from the Xiaomi cloud: filtering and every answer the Xiaomi sign-in can give."""

from __future__ import annotations

import pytest
import requests
import requests_mock

from custom_components.yeelight_quota_guard import cloud

from .fake_xiaomi import JPEG, FakeXiaomi

TOKEN = "0123456789abcdef0123456789abcdef"


def test_only_yeelight_lights_with_a_token_are_kept() -> None:
    devices = [
        {
            "did": "307091595",
            "model": "yeelink.light.color4",
            "name": "Trastero",
            "localip": "10.0.0.9",
            "token": TOKEN,
        },
        {
            "did": "410482680",
            "model": "yeelink.light.colorc",
            "name": "Salón A",
            "localip": "",
            "token": TOKEN.upper(),
        },
        {"did": "1", "model": "zhimi.airpurifier.ma2", "name": "Purifier", "token": TOKEN},
        {"did": "blt.3.abc", "model": "yeelink.light.ble1", "name": "BLE", "token": TOKEN},
        {"did": "2", "model": "yeelink.light.mono1", "name": "No token", "token": ""},
    ]
    bulbs = cloud.yeelight_tokens(devices)
    assert sorted(bulbs) == [307091595, 410482680]
    assert bulbs[410482680].host is None
    assert bulbs[410482680].token == TOKEN


DEVICES = [{"did": "10", "model": "yeelink.light.colorc", "name": "Salón A", "token": TOKEN}]


def test_password_alone_signs_in_and_reads_the_devices() -> None:
    with FakeXiaomi(devices=DEVICES) as xiaomi:
        login = cloud.XiaomiLogin("a@b.c", "secret")
        assert login.start() == cloud.LoginStep(signed_in=True)
        assert login.devices("de") == DEVICES
    assert xiaomi.signed_in_as == {"country": "de"}


def test_wrong_password() -> None:
    with FakeXiaomi(), pytest.raises(cloud.CloudAuthError):
        cloud.XiaomiLogin("a@b.c", "nope").start()


def test_captcha_wrong_then_right() -> None:
    """What happened on 15-10-2026: Xiaomi asked for a captcha, not a new password."""
    with FakeXiaomi(captcha="K7TQ", devices=DEVICES) as xiaomi:
        login = cloud.XiaomiLogin("a@b.c", "secret")
        step = login.start()
        assert step.captcha == JPEG and not step.captcha_was_wrong
        step = login.captcha("XXXX")
        assert step.captcha == JPEG and step.captcha_was_wrong
        assert login.captcha(" K7TQ ") == cloud.LoginStep(signed_in=True)
        assert login.devices("de") == DEVICES
    assert xiaomi.captchas_served == 2


def test_captcha_then_wrong_password() -> None:
    with FakeXiaomi(captcha="K7TQ"), pytest.raises(cloud.CloudAuthError):
        login = cloud.XiaomiLogin("a@b.c", "nope")
        login.start()
        login.captcha("K7TQ")


@pytest.mark.parametrize(("by", "address"), [("email", "jo***@example.com"), ("phone", "+34***12")])
def test_code_by_email_or_sms(by, address) -> None:
    with FakeXiaomi(code_by=by, devices=DEVICES) as xiaomi:
        login = cloud.XiaomiLogin("a@b.c", "secret")
        assert login.start() == cloud.LoginStep(code_sent_to=address)
        assert xiaomi.codes_sent == 1
        with pytest.raises(cloud.CloudCodeError):
            login.verify("000000")
        assert login.verify("123456") == cloud.LoginStep(signed_in=True)
        assert login.devices("de") == DEVICES


def test_captcha_then_code() -> None:
    with FakeXiaomi(captcha="K7TQ", code_by="email", devices=DEVICES):
        login = cloud.XiaomiLogin("a@b.c", "secret")
        assert login.start().captcha == JPEG
        assert login.captcha("K7TQ").code_sent_to == "jo***@example.com"
        assert login.verify("123456").signed_in


def test_no_code_on_offer_falls_back_to_the_link() -> None:
    with FakeXiaomi(code_by="email", identity_flag=16), pytest.raises(cloud.CloudVerificationRequired) as err:
        cloud.XiaomiLogin("a@b.c", "secret").start()
    assert err.value.url.startswith("https://account.xiaomi.com/fe/service/identity/authStart")


def test_unknown_refusal_keeps_xiaomis_code() -> None:
    with FakeXiaomi(refuse_with=70022), pytest.raises(cloud.CloudUnexpected) as err:
        cloud.XiaomiLogin("a@b.c", "secret").start()
    assert err.value.xiaomi_code == 70022
    assert not isinstance(err.value, cloud.CloudAuthError)


def test_network_down_is_a_cloud_error() -> None:
    with requests_mock.Mocker() as m, pytest.raises(cloud.CloudError):
        m.get(requests_mock.ANY, exc=requests.ConnectionError("down"))
        cloud.XiaomiLogin("a@b.c", "secret").start()


def test_devices_before_signing_in() -> None:
    with pytest.raises(cloud.CloudError):
        cloud.XiaomiLogin("a@b.c", "secret").devices("de")


def test_no_device_list_is_a_cloud_error() -> None:
    with FakeXiaomi() as xiaomi:
        xiaomi.devices = None  # type: ignore[assignment]
        login = cloud.XiaomiLogin("a@b.c", "secret")
        login.start()
        with pytest.raises(cloud.CloudError):
            login.devices("de")


def test_password_is_forgotten_once_signed_in() -> None:
    with FakeXiaomi():
        login = cloud.XiaomiLogin("a@b.c", "secret")
        login.start()
    assert "secret" not in str(vars(login))


def test_a_token_that_is_not_hexadecimal_is_left_out() -> None:
    devices = [{"did": "10", "model": "yeelink.light.colorc", "name": "x", "token": "z" * 32}]
    assert cloud.yeelight_tokens(devices) == {}


def test_the_device_list_is_read_with_our_own_session() -> None:
    """micloud wrote the service token and every device token to the root logger, with no timeout."""
    with FakeXiaomi(devices=DEVICES) as xiaomi:
        login = cloud.XiaomiLogin("a@b.c", "secret")
        login.start()
        assert login.devices("us") == DEVICES
    assert xiaomi.signed_in_as == {"country": "us"}
