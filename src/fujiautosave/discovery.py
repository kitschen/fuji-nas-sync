"""The HTTP-like handshake before a PC AutoSave session: DISCOVER, NOTIFY, REGISTER and IMPORT.

The messages only look like HTTP. The camera's requests end without an empty line, so they are read until the
expected header arrived or the camera goes quiet, never with an HTTP library.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SERVICE = "PCAUTOSAVE/1.0"
FOLDER = "guest"


@dataclass(frozen=True)
class Ports:
    register: int = 51542  # UDP DISCOVER * and TCP REGISTER, both towards the destination
    connect: int = 51541  # UDP DISCOVER <name> and TCP IMPORT, both towards the destination
    notify: int = 51540  # TCP NOTIFY, towards the camera
    ptp: int = 55740  # TCP PTP session, towards the camera


@dataclass(frozen=True)
class CameraInfo:
    name: str  # DSCNAME, e.g. FUJIFILM-X-E2-BD35; the only stable identity the camera gives
    address: str
    model: str = ""  # DSCMODEL, only sent while registering


@dataclass(frozen=True)
class Message:
    method: str  # DISCOVER, NOTIFY, REGISTER, IMPORT, or HTTP/1.1 for a response
    target: str  # request target, or the status code of a response
    headers: dict[str, str] = field(default_factory=dict)  # names upper-cased

    @classmethod
    def parse(cls, data: bytes) -> Message:
        lines = data.decode("latin-1").replace("\r\n", "\n").split("\n")
        start = lines[0].split(" ")
        if len(start) < 2:
            raise ValueError(f"not a PC AutoSave message: {data[:80]!r}")
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().upper()] = value.strip()
        return cls(start[0], start[1], headers)

    @property
    def ok(self) -> bool:
        return self.method.startswith("HTTP/") and self.target == "200"


def notify_message(camera_address: str, importer: str, notify_port: int = 51540) -> bytes:
    return f"NOTIFY * HTTP/1.1\r\nHOST: {camera_address}:{notify_port}\r\nIMPORTER: {importer}\r\n\r\n".encode()


REGISTER_REPLY = f"HTTP/1.1 200 OK\r\nFOLDER: {FOLDER}\r\nServiceName: {SERVICE}\r\n\r\n".encode()
IMPORT_REPLY = b"HTTP/1.1 200 OK\r\n\r\n"
BUSY_REPLY = b"HTTP/1.1 503 Service Unavailable\r\n\r\n"


def discover_message(target: str, camera_address: str, broadcast: str = "255.255.255.255") -> bytes:
    """What the camera broadcasts; used by the fake camera."""
    return (f"DISCOVER {target} HTTP/1.1\r\nMX:3\r\nHOST:{broadcast}:\r\nDSCADDR:{camera_address}\r\n"
            f"SERVICE:{SERVICE}\r\n").encode()


async def read_message(reader: asyncio.StreamReader, until_header: str | None = None, single_line: bool = False,
                       idle: float = 1.0, timeout: float = 10.0) -> Message:
    """Read one message: until an empty line, `until_header` has arrived, the first line if `single_line`, EOF,
    or `idle` seconds of silence."""
    buf = b""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(reader.read(4096), min(remaining, idle) if buf else remaining)
        except asyncio.TimeoutError:
            if buf:
                break
            raise
        if not chunk:
            break
        buf += chunk
        if b"\r\n\r\n" in buf or (single_line and b"\r\n" in buf):
            break
        if until_header and buf.endswith(b"\r\n") and until_header.upper() in Message.parse(buf).headers:
            break
    if not buf:
        raise ConnectionError("connection closed before a message arrived")
    return Message.parse(buf)


async def send_notify(camera_address: str, importer: str, notify_port: int = 51540, timeout: float = 5.0) -> None:
    """Answer a DISCOVER: tell the camera who we are. It then connects back for REGISTER or IMPORT."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(camera_address, notify_port), timeout)
    try:
        writer.write(notify_message(camera_address, importer, notify_port))
        await writer.drain()
        reply = await read_message(reader, single_line=True, timeout=timeout)
        if not reply.ok:
            raise ConnectionError(f"camera refused NOTIFY: {reply.method} {reply.target}")
    finally:
        writer.close()
