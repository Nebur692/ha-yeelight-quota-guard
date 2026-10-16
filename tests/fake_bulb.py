"""A fake Yeelight that speaks miIO as strictly as a real one.

Measured on real bulbs (colorc, color4, panel1, ceiling15, strip2, color2):
- the hello reply carries the device id and a stamp;
- a request with a bad checksum (wrong token) gets no reply at all;
- miIO.info returns {"life": seconds powered, "fw_ver", "model", ...};
- miIO.reboot answers ["ok"], goes silent for a few seconds and comes back
  with life near 0 and the same on/off state.

And the LAN protocol (TCP 55443) that Home Assistant uses, measured on
15-10-2026: get_prop answers {"id":1,"result":["on"]}; past 2^32 ms an
affected model answers {"id":1,"error":{"code":-1,"message":"client quota
exceeded"}}, while strips and color2 keep answering normally; a colorc may
take the connection and say nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import time
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HEADER = struct.Struct(">HHIII")


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()  # noqa: S324


@dataclass
class FakeBulb:
    host: str
    did: int
    token: str
    life: int = 1000
    power: str = "off"
    model: str = "yeelink.light.colorc"
    # What a restart does. The real ones keep their power state.
    power_after_reboot: str | None = None
    reboot_works: bool = True
    silent_after_reboot: float = 0.2
    port: int = 54321
    # Locks past 2^32 ms (colorc, panel1, ceiling15, color4) or not (strips, color2).
    affected: bool = True
    # "answer", or "silent": accepts the connection and never replies.
    lan: str = "answer"
    lan_port: int = 55443
    firmware: str = "2.0.8_0016"
    lan_questions: int = 0
    # Restarts without answering the order (it went down before replying).
    reboot_reply: bool = True
    # Power answers to give, one per get_prop, before falling back to `power`.
    power_sequence: list[str] = field(default_factory=list)
    report_life: bool = True
    ids: list[int] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    reboots: int = 0

    def __post_init__(self) -> None:
        self._token = bytes.fromhex(self.token)
        self._key = _md5(self._token)
        self._iv = _md5(self._key + self._token)
        self._boot = time.monotonic() - self.life
        self._silent_until = 0.0
        self._transport: asyncio.DatagramTransport | None = None
        self._lan_server: asyncio.Server | None = None

    # ---- life ----------------------------------------------------------

    @property
    def uptime(self) -> int:
        return int(time.monotonic() - self._boot)

    # ---- network -------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self), local_addr=(self.host, self.port)
        )
        self._lan_server = await asyncio.start_server(self._lan, self.host, self.lan_port)

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
        if self._lan_server is not None:
            self._lan_server.close()

    @property
    def locked(self) -> bool:
        return self.affected and self.uptime * 1000 >= 2**32

    async def _lan(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            self.lan_questions += 1
            request = json.loads(line)
            assert request["method"] == "get_prop", request
            if self.lan == "silent":
                await reader.read()  # says nothing until the other side hangs up
                return
            # A notification first, as real bulbs push them at any time.
            writer.write(b'{"method":"props","params":{"bright":"50"}}\r\n')
            if self.locked:
                reply = {"id": request["id"], "error": {"code": -1, "message": "client quota exceeded"}}
            else:
                reply = {"id": request["id"], "result": [self.power]}
            writer.write(json.dumps(reply).encode() + b"\r\n")
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    def _encrypt(self, plain: bytes) -> bytes:
        padder = padding.PKCS7(128).padder()
        data = padder.update(plain) + padder.finalize()
        enc = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).encryptor()
        return enc.update(data) + enc.finalize()

    def _decrypt(self, data: bytes) -> bytes:
        dec = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).decryptor()
        padded = dec.update(data) + dec.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()

    def handle(self, data: bytes) -> bytes | None:
        if time.monotonic() < self._silent_until:
            return None
        magic, length, _unknown, did, _stamp = HEADER.unpack_from(data)
        assert magic == 0x2131 and length == len(data)
        stamp = int(time.monotonic()) & 0xFFFFFFFF
        if length == 32:  # hello
            return HEADER.pack(0x2131, 32, 0, self.did, stamp) + b"\x00" * 16
        if did != self.did:
            return None
        encrypted = data[32:]
        if _md5(data[:16] + self._token + encrypted) != data[16:32]:
            return None  # wrong token: a real bulb stays silent
        request = json.loads(self._decrypt(encrypted).rstrip(b"\x00"))
        request_id = request["id"]
        assert 0 < request_id < 1000, request_id
        self.ids.append(request_id)
        self.methods.append(request["method"])
        result = self._result(request["method"], request.get("params") or [])
        if request["method"] == "miIO.reboot" and not self.reboot_reply:
            return None
        body = json.dumps({"id": request_id, "result": result}).encode()
        reply_enc = self._encrypt(body)
        header = HEADER.pack(0x2131, 32 + len(reply_enc), 0, self.did, stamp)
        return header + _md5(header + self._token + reply_enc) + reply_enc

    def _result(self, method: str, params: list) -> object:
        if method == "miIO.info":
            info = {"fw_ver": self.firmware, "model": self.model}
            return {**info, "life": self.uptime} if self.report_life else info
        if method == "get_prop":
            if self.power_sequence:
                self.power = self.power_sequence.pop(0)
            return [self.power if p == "power" else "" for p in params]
        if method == "set_power":
            self.power = params[0]
            return ["ok"]
        if method == "miIO.reboot":
            self.reboots += 1
            if self.reboot_works:
                self._boot = time.monotonic() + self.silent_after_reboot
                self._silent_until = time.monotonic() + self.silent_after_reboot
                if self.power_after_reboot is not None:
                    self.power = self.power_after_reboot
            return ["ok"]
        return {"code": -1, "message": "method not supported"}


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, bulb: FakeBulb) -> None:
        self.bulb = bulb
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        reply = self.bulb.handle(data)
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, addr)
