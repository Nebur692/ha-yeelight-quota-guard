"""One question over the Yeelight LAN protocol: is this bulb locked or not?

The same question Home Assistant's Yeelight integration asks, on TCP 55443. A
locked bulb answers {"error": {"code": -1, "message": "client quota exceeded"}}
(measured on 16 bulbs on 15-10-2026); a working one answers with a result.
Anything else - refused, silent, closed - proves nothing: some models (colorc)
take a single LAN connection and Home Assistant already holds it.

Asked only of bulbs past 2^32 ms, so it costs nothing to the rest.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from enum import StrEnum

PORT = 55443
TIMEOUT = 3.0
QUOTA_MESSAGE = "client quota exceeded"


class LanAnswer(StrEnum):
    WORKS = "works"
    LOCKED = "locked"
    UNKNOWN = "unknown"


async def probe(host: str, port: int = PORT) -> LanAnswer:
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), TIMEOUT)
        writer.write(b'{"id":1,"method":"get_prop","params":["power"]}\r\n')
        await writer.drain()
        # A bulb may push a notification first; the answer is the line with our id.
        async with asyncio.timeout(TIMEOUT):
            while line := await reader.readline():
                answer = _read(line)
                if answer is not None:
                    return answer
    except (OSError, TimeoutError, ValueError):
        # ValueError: a line longer than the stream limit. Proves nothing either.
        pass
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
    return LanAnswer.UNKNOWN


def _read(line: bytes) -> LanAnswer | None:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    # Notifications carry "method"; our answer does not (and should carry our id).
    if not isinstance(message, dict) or "method" in message or message.get("id", 1) != 1:
        return None
    if "result" in message:
        return LanAnswer.WORKS
    error = message.get("error")
    if isinstance(error, dict) and QUOTA_MESSAGE in str(error.get("message", "")).lower():
        return LanAnswer.LOCKED
    return LanAnswer.UNKNOWN
