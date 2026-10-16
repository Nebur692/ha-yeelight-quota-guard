"""Fetch the bulbs' miIO tokens from the Xiaomi cloud, once, at setup.

The sign-in is done here, step by step, because Xiaomi rarely lets a new
sign-in through with just the password: it asks for a captcha, then for a
code sent by email or SMS. micloud (the library Home Assistant's own Xiaomi
Miio integration uses) treats both as a wrong password, so it is only used for
the device list, once signed in.

The password lives in memory for the length of the setup and is never stored:
only the tokens are kept.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any

import requests
from micloud import miutils

from .yeelight_link import parse_did

_LOGGER = logging.getLogger(__name__)

ACCOUNT = "https://account.xiaomi.com"
SDK_VERSION = "4.2.29"
TIMEOUT = 20
FLAG_PHONE = 4
FLAG_EMAIL = 8
WRONG_PASSWORD = 70016
WRONG_CAPTCHA = 87001
DEVICE_LIST_QUERY = '{"getVirtualModel":true,"getHuamiDevices":1,"get_split_device":false,"support_smart_home":true}'


class CloudError(Exception):
    """Could not reach the Xiaomi cloud or read the device list."""


class CloudAuthError(CloudError):
    """Wrong account or password."""


class CloudCodeError(CloudError):
    """The code sent by email or SMS was wrong or has expired."""


class CloudUnexpected(CloudError):
    """Xiaomi said no in a way this sign-in does not know."""

    def __init__(self, step: str, answer: dict[str, Any]) -> None:
        self.xiaomi_code = answer.get("code")
        super().__init__(f"Xiaomi refused the sign-in at {step}: code {self.xiaomi_code}, {answer.get('desc')}")


class CloudVerificationRequired(CloudError):
    """Xiaomi wants this sign-in confirmed through a link first."""

    def __init__(self, url: str) -> None:
        super().__init__("Xiaomi asks to verify this sign-in")
        self.url = url


@dataclass(frozen=True)
class CloudBulb:
    did: int
    name: str
    model: str
    host: str | None
    token: str


@dataclass(frozen=True)
class LoginStep:
    """What the sign-in needs next. Exactly one of the three is set."""

    signed_in: bool = False
    captcha: bytes | None = None  # image to read, then call captcha()
    captcha_was_wrong: bool = False
    code_sent_to: str | None = None  # masked address, then call verify()


def _cookie(*jars: Any, name: str) -> str | None:
    """A cookie's value. Xiaomi sets some (userId, serviceToken) once per domain,
    and requests' jar.get() refuses to choose between them."""
    for jar in jars:
        for cookie in jar:
            if cookie.name == name and cookie.value:
                return cookie.value
    return None


def _parse(response: requests.Response) -> dict[str, Any]:
    try:
        return json.loads(response.text.replace("&&&START&&&", ""))
    except ValueError as err:
        raise CloudError(f"Unreadable answer from {response.url}") from err


class XiaomiLogin:
    """One sign-in, possibly over several form steps. Blocking: run in the executor."""

    def __init__(self, username: str, password: str, session: requests.Session | None = None) -> None:
        self._username = username
        self._password = password
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = (
            f"Android-7.1.1-1.0.0-ONEPLUS A3010-136-{secrets.token_hex(8).upper()} APP/xiaomi.smarthome APPV/62830"
        )
        self._session.cookies.set("sdkVersion", SDK_VERSION, domain=".xiaomi.com")
        self._session.cookies.set("deviceId", secrets.token_hex(8), domain=".xiaomi.com")
        self._ick: str | None = None
        self._identity: str | None = None
        self._flag: int | None = None
        self._user_id: str | None = None
        self._ssecurity: str | None = None
        self._service_token: str | None = None

    @property
    def username(self) -> str:
        return self._username

    @property
    def signed_in(self) -> bool:
        return bool(self._service_token)

    def start(self, captcha: str | None = None) -> LoginStep:
        try:
            return self._password_step(captcha)
        except requests.RequestException as err:
            raise CloudError(str(err)) from err

    def captcha(self, code: str) -> LoginStep:
        """The captcha belongs to the password step or to sending the email/SMS code."""
        try:
            if self._identity is not None:
                return self._send_code(code.strip())
            return self._password_step(code.strip())
        except requests.RequestException as err:
            raise CloudError(str(err)) from err

    def verify(self, code: str) -> LoginStep:
        try:
            return self._verify(code.strip())
        except requests.RequestException as err:
            raise CloudError(str(err)) from err

    def devices(self, country: str) -> list[dict[str, Any]]:
        """The account's device list, tokens included.

        The same signed, RC4-encrypted request micloud makes, done here for two
        reasons: micloud sets no timeout (a silent server hangs the wizard), and
        it writes the service token and every device token to the root logger
        at debug level. Only micloud's pure helpers are used.
        """
        if not self._service_token or not self._ssecurity:
            raise CloudError("Not signed in")
        url = f"https://{'' if country == 'cn' else country + '.'}api.io.mi.com/app/home/device_list"
        params = {"data": DEVICE_LIST_QUERY}
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self._session.headers["User-Agent"],
                "Accept-Encoding": "identity",
                "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
                "Content-Type": "application/x-www-form-urlencoded",
                "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
            }
        )
        session.cookies.update(
            {
                "userId": str(self._user_id),
                "yetAnotherServiceToken": self._service_token,
                "serviceToken": self._service_token,
                "locale": "en_US",
                "channel": "MI_APP_STORE",
            }
        )
        try:
            nonce = miutils.gen_nonce()
            signed = miutils.signed_nonce(self._ssecurity, nonce)
            form = miutils.generate_enc_params(url, "POST", signed, nonce, params, self._ssecurity)
            response = session.post(url, data=form, timeout=TIMEOUT)
            if response.status_code != 200:
                raise CloudError(f"The device list answered HTTP {response.status_code}")
            text = miutils.decrypt_rc4(miutils.signed_nonce(self._ssecurity, form["_nonce"]), response.text)
            devices = json.loads(text)["result"]["list"]
        except requests.RequestException as err:
            raise CloudError(str(err)) from err
        except (ValueError, KeyError, TypeError) as err:
            raise CloudError("The Xiaomi cloud did not return a readable device list") from err
        if not isinstance(devices, list):
            raise CloudError("The Xiaomi cloud did not return a device list")
        return devices

    def _password_step(self, captcha: str | None) -> LoginStep:
        first = _parse(
            self._session.get(
                f"{ACCOUNT}/pass/serviceLogin", params={"sid": "xiaomiio", "_json": "true"}, timeout=TIMEOUT
            )
        )
        form = {
            "_json": "true",
            "sid": first.get("sid", "xiaomiio"),
            "callback": first.get("callback", "https://sts.api.io.mi.com/sts"),
            "qs": first.get("qs", "%3Fsid%3Dxiaomiio%26_json%3Dtrue"),
            "_sign": first.get("_sign", ""),
            "user": self._username,
            "hash": hashlib.md5(self._password.encode()).hexdigest().upper(),  # noqa: S324
        }
        cookies = {}
        if captcha:
            form["captCode"] = captcha
            cookies["ick"] = self._ick or ""
        answer = _parse(
            self._session.post(f"{ACCOUNT}/pass/serviceLoginAuth2", data=form, cookies=cookies, timeout=TIMEOUT)
        )
        if answer.get("captchaUrl"):
            return LoginStep(captcha=self._captcha_image(answer["captchaUrl"]), captcha_was_wrong=bool(captcha))
        if answer.get("code") == WRONG_PASSWORD:
            raise CloudAuthError("Wrong account or password")
        if answer.get("notificationUrl") and not answer.get("ssecurity"):
            return self._start_identity(answer["notificationUrl"])
        if answer.get("code") != 0 or not answer.get("ssecurity") or not answer.get("location"):
            raise CloudUnexpected("password", answer)
        return self._finish(answer)

    def _captcha_image(self, path: str) -> bytes:
        response = self._session.get(ACCOUNT + path, timeout=TIMEOUT)
        self._ick = _cookie(response.cookies, self._session.cookies, name="ick")
        return response.content

    def _start_identity(self, url: str) -> LoginStep:
        if "/fe/service/identity/authStart" not in url:
            raise CloudVerificationRequired(url)
        response = self._session.get(url.replace("/fe/service/identity/authStart", "/identity/list"), timeout=TIMEOUT)
        answer = _parse(response)
        flag = answer.get("flag")
        identity = _cookie(response.cookies, name="identity_session")
        if answer.get("code") != 2 or flag not in (FLAG_EMAIL, FLAG_PHONE) or not identity:
            # Neither email nor SMS on offer: the link is the only way left.
            raise CloudVerificationRequired(url)
        self._flag, self._identity = flag, identity
        return self._send_code(None)

    @property
    def _kind(self) -> str:
        return "Phone" if self._flag == FLAG_PHONE else "Email"

    def _send_code(self, captcha: str | None) -> LoginStep:
        cookies = {"identity_session": self._identity or ""}
        info = _parse(
            self._session.get(
                f"{ACCOUNT}/identity/auth/verify{self._kind}",
                params={"_flag": self._flag, "_json": "true"},
                cookies=cookies,
                timeout=TIMEOUT,
            )
        )
        if info.get("code") != 0:
            raise CloudUnexpected("verify-info", info)
        form = {"retry": "0", "icode": captcha or "", "_json": "true"}
        if captcha:
            cookies = {**cookies, "ick": self._ick or ""}
        sent = _parse(
            self._session.post(
                f"{ACCOUNT}/identity/auth/send{self._kind}Ticket", data=form, cookies=cookies, timeout=TIMEOUT
            )
        )
        if sent.get("captchaUrl"):
            return LoginStep(captcha=self._captcha_image(sent["captchaUrl"]), captcha_was_wrong=bool(captcha))
        if sent.get("code") != 0:
            raise CloudUnexpected("send-code", sent)
        return LoginStep(code_sent_to=str(info.get(f"masked{self._kind}") or ""))

    def _verify(self, ticket: str) -> LoginStep:
        if self._identity is None:
            raise CloudError("No code was asked for")
        answer = _parse(
            self._session.post(
                f"{ACCOUNT}/identity/auth/verify{self._kind}",
                params={"_flag": self._flag, "ticket": ticket, "trust": "false", "_json": "true"},
                cookies={"identity_session": self._identity},
                timeout=TIMEOUT,
            )
        )
        if answer.get("code") != 0 or not answer.get("location"):
            _LOGGER.debug("Xiaomi did not accept the code: %s", answer.get("code"))
            raise CloudCodeError("Wrong or expired code")
        return self._finish(answer)

    def _finish(self, answer: dict[str, Any]) -> LoginStep:
        """Follow the sign-in location to the service token (and, after a code, to ssecurity)."""
        found: dict[str, Any] = {k: answer[k] for k in ("userId", "ssecurity") if answer.get(k)}
        response = self._session.get(answer["location"], timeout=TIMEOUT)
        for hop in [*response.history, response]:
            if user_id := _cookie(hop.cookies, name="userId"):
                found.setdefault("userId", user_id)
            if pragma := hop.headers.get("extension-pragma"):
                with contextlib.suppress(ValueError):
                    found.update({k: v for k, v in json.loads(pragma).items() if k == "ssecurity"})
        token = _cookie(response.cookies, self._session.cookies, name="serviceToken")
        if not token or not found.get("ssecurity") or not found.get("userId"):
            raise CloudUnexpected("location", {"code": response.status_code, "desc": "no service token"})
        self._user_id = str(found["userId"])
        self._ssecurity = str(found["ssecurity"])
        self._service_token = token
        self._password = ""
        return LoginStep(signed_in=True)


def _is_token(token: str) -> bool:
    if len(token) != 32:
        return False
    try:
        bytes.fromhex(token)
    except ValueError:
        return False
    return True


def yeelight_tokens(devices: list[dict[str, Any]]) -> dict[int, CloudBulb]:
    """Keep the Yeelight lights that come with a usable token."""
    bulbs: dict[int, CloudBulb] = {}
    for device in devices:
        model = str(device.get("model") or "")
        token = str(device.get("token") or "")
        did = parse_did(device.get("did"))
        if not model.startswith("yeelink.light.") or did is None or not _is_token(token):
            continue
        bulbs[did] = CloudBulb(
            did=did,
            name=str(device.get("name") or model),
            model=model,
            host=device.get("localip") or None,
            token=token.lower(),
        )
    return bulbs
