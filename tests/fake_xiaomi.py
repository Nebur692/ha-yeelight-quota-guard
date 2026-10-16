"""A fake Xiaomi account server, as strict as the real one about what it checks.

It answers the same URLs, with the same "&&&START&&&" JSON, the same codes
(70016 wrong password, 87001 captcha) and the same cookies (ick for the
captcha, identity_session for the email/SMS code), and refuses anything sent
without them. Shapes taken from a real sign-in on 15-10-2026.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from typing import Any
from urllib.parse import parse_qs

import requests_mock
from micloud import miutils
from urllib3 import HTTPResponse
from urllib3._collections import HTTPHeaderDict

ACCOUNT = "https://account.xiaomi.com"
LOCATION = "https://sts.api.io.mi.com/sts?d=abc"
SERVICE = "https://sts.api.io.mi.com/sts/done"
CAPTCHA_URL = "/pass/getCode?icodeType=login&0.4118"
AUTH_START = f"{ACCOUNT}/fe/service/identity/authStart?sid=xiaomiio&_locale=es_ES"
JPEG = b"\xff\xd8\xff\xe0fake-captcha"
SSECURITY = "c3NlY3VyaXR5LXZhbHVl"


def _body(payload: dict[str, Any]) -> str:
    return "&&&START&&&" + json.dumps(payload)


def _raw(request, status: int, headers: list[tuple[str, str]], body: bytes = b""):
    raw_headers = HTTPHeaderDict()
    for name, value in headers:
        raw_headers.add(name, value)
    raw = HTTPResponse(body=io.BytesIO(body), headers=raw_headers, status=status, preload_content=False)
    return requests_mock.create_response(request, raw=raw)


class FakeXiaomi:
    def __init__(
        self,
        password: str = "secret",  # noqa: S107
        captcha: str | None = None,
        code_by: str | None = None,
        code: str = "123456",
        refuse_with: int | None = None,
        identity_flag: int | None = None,
        devices: list[dict[str, Any]] | None = None,
    ) -> None:
        self.password = password
        self.captcha = captcha  # characters in the picture; None: no captcha asked
        self.code_by = code_by  # "email", "phone" or None: no second step
        self.code = code
        self.refuse_with = refuse_with  # an unknown Xiaomi code at the password step
        self.identity_flag = identity_flag
        self.devices: list[dict[str, Any]] | None = devices or []
        self.captchas_served = 0
        self.codes_sent = 0
        self.signed_in_as: dict[str, Any] | None = None
        self._ick = ""
        self._mock = requests_mock.Mocker()

    def __enter__(self) -> FakeXiaomi:
        m = self._mock.__enter__()
        m.get(
            f"{ACCOUNT}/pass/serviceLogin",
            text=_body(
                {
                    "code": 70016,
                    "_sign": "SIGN",
                    "sid": "xiaomiio",
                    "qs": "%3Fsid",
                    "callback": "https://sts.api.io.mi.com/sts",
                }
            ),
        )
        m.post(f"{ACCOUNT}/pass/serviceLoginAuth2", text=self._password_step)
        m.get(f"{ACCOUNT}/pass/getCode", content=self._captcha_image)
        m.get(f"{ACCOUNT}/identity/list", text=self._identity_list)
        for kind in ("Email", "Phone"):
            m.get(f"{ACCOUNT}/identity/auth/verify{kind}", text=self._verify_info)
            m.post(f"{ACCOUNT}/identity/auth/send{kind}Ticket", text=self._send_ticket)
            m.post(f"{ACCOUNT}/identity/auth/verify{kind}", text=self._verify)
        m.add_matcher(self._sts)
        m.post(re.compile(r"https://([a-z0-9]+\.)?api\.io\.mi\.com/app/home/device_list"), text=self._device_list)
        return self

    def __exit__(self, *exc: object) -> None:
        self._mock.__exit__(*exc)

    # -- the password step -------------------------------------------------
    def _password_step(self, request, context) -> str:
        form = dict(p.split("=", 1) for p in request.text.split("&"))
        assert form["_sign"] == "SIGN" and form["sid"] == "xiaomiio" and form["_json"] == "true"
        if self.captcha is not None:
            sent = form.get("captCode")
            if (
                sent is None
                or sent != self.captcha
                or request._request.headers.get("Cookie", "").find(f"ick={self._ick}") < 0
            ):
                return _body({"code": 87001, "result": "error", "desc": "验证码输入错误", "captchaUrl": CAPTCHA_URL})
        if form["hash"] != hashlib.md5(self.password.encode()).hexdigest().upper():  # noqa: S324
            return _body({"code": 70016, "result": "error", "desc": "密码错误"})
        if self.refuse_with is not None:
            return _body({"code": self.refuse_with, "result": "error", "desc": "?"})
        if self.code_by:
            return _body({"code": 0, "result": "ok", "notificationUrl": AUTH_START, "location": ""})
        return _body({"code": 0, "result": "ok", "userId": 42, "ssecurity": SSECURITY, "location": LOCATION})

    def _captcha_image(self, request, context) -> bytes:
        self.captchas_served += 1
        self._ick = f"ICK{self.captchas_served}"
        context.headers["Set-Cookie"] = f"ick={self._ick}; Domain=.xiaomi.com; Path=/"
        return JPEG

    # -- the email / SMS code ---------------------------------------------
    def _flag(self) -> int:
        if self.identity_flag is not None:
            return self.identity_flag
        return 4 if self.code_by == "phone" else 8

    def _kind(self) -> str:
        return "Phone" if self._flag() == 4 else "Email"

    def _has_identity(self, request) -> bool:
        return "identity_session=IDS" in request._request.headers.get("Cookie", "")

    def _identity_list(self, request, context) -> str:
        context.headers["Set-Cookie"] = "identity_session=IDS; Domain=.xiaomi.com; Path=/"
        return _body({"code": 2, "flag": self._flag()})

    def _verify_info(self, request, context) -> str:
        if not self._has_identity(request) or not request.url.split("?")[0].endswith(self._kind()):
            return _body({"code": 70002})
        return _body(
            {"code": 0, f"masked{self._kind()}": "jo***@example.com" if self._kind() == "Email" else "+34***12"}
        )

    def _send_ticket(self, request, context) -> str:
        if not self._has_identity(request):
            return _body({"code": 70002})
        self.codes_sent += 1
        return _body({"code": 0})

    def _verify(self, request, context) -> str:
        if not self._has_identity(request) or request.qs.get("ticket") != [self.code]:
            return _body({"code": 70014, "desc": "验证码错误"})
        return _body({"code": 0, "location": LOCATION})

    # -- signed in ---------------------------------------------------------
    def _sts(self, request):
        # Like the real one (15-10-2026), userId and serviceToken come once per
        # domain: two Set-Cookie headers with the same name. Needs a raw
        # response, as a headers dict cannot repeat a key.
        if request.url == LOCATION:
            # The first hop names the user and carries ssecurity, then redirects.
            return _raw(
                request,
                302,
                [
                    ("Location", SERVICE),
                    ("Set-Cookie", "userId=42; Domain=.mi.com; Path=/"),
                    ("Set-Cookie", "userId=42; Domain=sts.api.io.mi.com; Path=/"),
                    ("extension-pragma", json.dumps({"ssecurity": SSECURITY})),
                ],
            )
        if request.url == SERVICE:
            return _raw(
                request,
                200,
                [
                    ("Set-Cookie", "serviceToken=STOKEN; Domain=.mi.com; Path=/"),
                    ("Set-Cookie", "serviceToken=STOKEN; Domain=sts.api.io.mi.com; Path=/"),
                ],
                b"ok",
            )
        return None

    def _device_list(self, request, context) -> str:
        """Signed and RC4-encrypted both ways, keyed by ssecurity and the nonce."""
        cookies = request._request.headers.get("Cookie", "")
        if "serviceToken=STOKEN" not in cookies or "userId=42" not in cookies:
            context.status_code = 401
            return "{}"
        form = {k: v[0] for k, v in parse_qs(request.text).items()}
        key = miutils.signed_nonce(SSECURITY, form["_nonce"])
        assert json.loads(miutils.decrypt_rc4(key, form["data"]))["getVirtualModel"] is True
        host = request.netloc.split(".")[0]
        self.signed_in_as = {"country": "cn" if host == "api" else host}
        result: dict[str, Any] = {"list": self.devices} if self.devices is not None else {}
        return miutils.encrypt_rc4(key, json.dumps({"code": 0, "message": "ok", "result": result}))
