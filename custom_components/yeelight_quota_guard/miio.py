"""A small miIO client: the one door a quota-locked Yeelight still opens.

When a Yeelight passes 2^32 ms of uptime its LAN protocol (TCP 55443) answers
every command with "client quota exceeded". The miIO protocol (UDP 54321) is a
different stack in the same firmware and keeps working, so this is the way in
to read the uptime and to ask the bulb to restart.

Only what the guard needs: hello, encrypted request, reply. Every request
checks that the device id that answers is the bulb we meant: an IP can change
hands, and a restart must never land on the wrong device.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import struct
import time
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PORT = 54321
MAGIC = 0x2131
HEADER = struct.Struct(">HHIII")  # magic, length, unknown, device id, stamp
HELLO = bytes.fromhex("21310020" + "ff" * 28)


class MiioError(Exception):
    """Base error."""


class MiioTimeout(MiioError):
    """Nothing answered, not even the hello."""


class MiioTokenError(MiioError):
    """The hello was answered but the encrypted request was not: wrong token."""


class MiioWrongDevice(MiioError):
    """Another device answered at that address."""


class MiioDeviceError(MiioError):
    """The device answered with an error."""


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()  # noqa: S324 - the miIO protocol is defined on MD5


def parse_token(token: str) -> bytes:
    """32 hex characters → 16 bytes, or ValueError."""
    token = token.strip().lower()
    if len(token) != 32:
        raise ValueError("token must be 32 hexadecimal characters")
    return bytes.fromhex(token)


class _OneReply(asyncio.DatagramProtocol):
    def __init__(self, future: asyncio.Future[bytes]) -> None:
        self._future = future

    def datagram_received(self, data: bytes, addr: Any) -> None:
        if not self._future.done():
            self._future.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self._future.done():
            self._future.set_exception(MiioTimeout(str(exc)))


async def _exchange(host: str, packet: bytes, timeout: float, port: int) -> bytes:
    """Send one datagram and wait for one reply."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bytes] = loop.create_future()
    transport, _ = await loop.create_datagram_endpoint(lambda: _OneReply(future), remote_addr=(host, port))
    try:
        transport.sendto(packet)
        async with asyncio.timeout(timeout):
            return await future
    finally:
        transport.close()


class MiioDevice:
    """One bulb over miIO."""

    def __init__(
        self,
        host: str,
        token: str,
        expected_did: int | None = None,
        *,
        timeout: float = 4.0,
        port: int = PORT,
    ) -> None:
        self.host = host
        self._token = parse_token(token)
        self._key = _md5(self._token)
        self._iv = _md5(self._key + self._token)
        self._expected_did = expected_did
        self._timeout = timeout
        self._port = port
        # Some firmwares reset on id 0 or above 1000 and dedupe repeated ids:
        # start anywhere in 1..900 and wrap before 1000.
        self._id = random.randint(1, 900)
        self.did: int | None = None
        self._stamp: int | None = None
        self._stamp_at = 0.0

    async def hello(self) -> int:
        """Handshake; returns the device id."""
        try:
            reply = await _exchange(self.host, HELLO, self._timeout, self._port)
        except (TimeoutError, OSError) as err:
            raise MiioTimeout(f"{self.host} did not answer the miIO hello") from err
        if len(reply) < 32:
            raise MiioError(f"{self.host} sent a short hello reply")
        magic, _length, _unknown, did, stamp = HEADER.unpack_from(reply)
        if magic != MAGIC:
            raise MiioError(f"{self.host} is not speaking miIO")
        if self._expected_did is not None and did != self._expected_did:
            raise MiioWrongDevice(f"{self.host} is device {did}, expected {self._expected_did}")
        self.did, self._stamp, self._stamp_at = did, stamp, time.monotonic()
        return did

    def _encrypt(self, plain: bytes) -> bytes:
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plain) + padder.finalize()
        enc = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).encryptor()
        return enc.update(padded) + enc.finalize()

    def _decrypt(self, data: bytes) -> bytes:
        dec = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).decryptor()
        padded = dec.update(data) + dec.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()

    def _packet(self, payload: bytes) -> bytes:
        assert self.did is not None and self._stamp is not None
        encrypted = self._encrypt(payload)
        stamp = self._stamp + int(time.monotonic() - self._stamp_at)
        header = HEADER.pack(MAGIC, 32 + len(encrypted), 0, self.did, stamp)
        return header + _md5(header + self._token + encrypted) + encrypted

    def _read(self, reply: bytes, request_id: int) -> Any:
        if len(reply) <= 32:
            raise MiioTokenError(f"{self.host} sent an empty reply")
        try:
            text = self._decrypt(reply[32:]).rstrip(b"\x00").decode("utf-8", "replace")
        except ValueError as err:
            raise MiioTokenError(f"{self.host}: reply does not decrypt") from err
        # Some firmwares append garbage after the JSON.
        try:
            message = json.loads(text[: text.rfind("}") + 1])
        except ValueError as err:
            raise MiioError(f"{self.host} sent an unreadable reply") from err
        if not isinstance(message, dict):
            raise MiioError(f"{self.host} sent an unexpected reply")
        if message.get("id") != request_id:
            raise MiioError(f"{self.host} answered another request")
        if "error" in message:
            raise MiioDeviceError(str(message["error"]))
        return message.get("result")

    async def send(self, method: str, params: list[Any] | None = None, retries: int = 2) -> Any:
        """Encrypted request with retries."""
        greeted = False
        for _attempt in range(retries + 1):
            if self._stamp is None:
                await self.hello()
            greeted = True
            self._id = self._id + 1 if self._id < 999 else 1
            request_id = self._id
            payload = (
                json.dumps(
                    {"id": request_id, "method": method, "params": params or []},
                    separators=(",", ":"),
                ).encode()
                + b"\x00"
            )
            try:
                reply = await _exchange(self.host, self._packet(payload), self._timeout, self._port)
            except (TimeoutError, OSError):
                self._stamp = None  # greet again on the next try
                continue
            try:
                return self._read(reply, request_id)
            except MiioTokenError:
                self._stamp = None
                continue
        if greeted:
            raise MiioTokenError(f"{self.host} answers the hello but not the request: the token is not this bulb's")
        raise MiioTimeout(f"{self.host} did not answer")

    async def info(self) -> dict[str, Any]:
        """miIO.info: model, firmware and `life` (seconds since power-on)."""
        result = await self.send("miIO.info")
        if not isinstance(result, dict):
            raise MiioError(f"{self.host}: unexpected miIO.info reply")
        return result

    async def power(self) -> str | None:
        """'on' / 'off' from get_prop."""
        result = await self.send("get_prop", ["power"])
        return result[0] if isinstance(result, list) and result else None

    async def set_power(self, state: str) -> None:
        await self.send("set_power", [state, "sudden", 0])

    async def reboot(self) -> None:
        """Ask the bulb to restart. It keeps its on/off state."""
        result = await self.send("miIO.reboot", retries=0)
        if result != ["ok"]:
            raise MiioDeviceError(f"{self.host}: reboot answered {result!r}")
