"""PTP over TCP as Fujifilm cameras speak it: USB-style containers after a PTP/IP-like init packet."""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 0x8F53E4F2
INIT_NAME_CHARS = 27  # fixed-size UTF-16 name field of the init request
ACK_NAME_CHARS = 20  # fixed-size UTF-16 model field of the init ack
END_OF_SESSION = b"\x08\x00\x00\x00\xff\xff\xff\xff"
MAX_PACKET = 16 * 1024 * 1024

RESPONSE_OK = 0x2001


class ContainerType(IntEnum):
    COMMAND = 1
    DATA = 2
    RESPONSE = 3
    EVENT = 4


class Op(IntEnum):
    OPEN_SESSION = 0x1002
    CLOSE_SESSION = 0x1003
    GET_OBJECT_INFO = 0x1008
    GET_DEVICE_PROP_VALUE = 0x1015
    SET_DEVICE_PROP_VALUE = 0x1016
    GET_PARTIAL_OBJECT = 0x101B


class PtpError(Exception):
    pass


class ProtocolError(PtpError):
    """The camera sent something we don't understand."""


class SessionEnded(PtpError):
    """The camera sent its end-of-session marker: the transfer is over or the user cancelled on the camera."""


class ResponseError(PtpError):
    def __init__(self, op: int, code: int):
        super().__init__(f"camera answered 0x{code:04x} to operation 0x{op:04x}")
        self.op = op
        self.code = code


@dataclass(frozen=True)
class Container:
    type: int
    code: int
    transaction_id: int
    payload: bytes = b""

    def encode(self) -> bytes:
        return struct.pack("<IHHI", 12 + len(self.payload), self.type, self.code, self.transaction_id) + self.payload

    @classmethod
    def decode(cls, packet: bytes) -> Container:
        if len(packet) < 12:
            raise ProtocolError(f"container too short: {packet.hex()}")
        length, type_, code, transaction_id = struct.unpack_from("<IHHI", packet)
        if length != len(packet):
            raise ProtocolError(f"container length {length} does not match packet length {len(packet)}")
        return cls(type_, code, transaction_id, bytes(packet[12:]))


def pack_string(value: str) -> bytes:
    """PTP string: u8 character count including the terminator, then UTF-16LE. Empty string is a single 0."""
    if not value:
        return b"\x00"
    encoded = (value + "\0").encode("utf-16-le")
    if len(encoded) // 2 > 255:
        raise ValueError("PTP strings are limited to 254 characters")
    return bytes([len(encoded) // 2]) + encoded


def unpack_string(buf: bytes, offset: int = 0) -> tuple[str, int]:
    """Returns the string and the offset just after it."""
    if offset >= len(buf):
        raise ProtocolError("PTP string past end of data")
    end = offset + 1 + 2 * buf[offset]
    if end > len(buf):
        raise ProtocolError("PTP string past end of data")
    return buf[offset + 1:end].decode("utf-16-le").rstrip("\0"), end


def encode_init_request(guid: bytes, name: str) -> bytes:
    """First packet of the session. Fuji's PC AutoSave puts the camera's DSCNAME into the name field."""
    if len(guid) != 16:
        raise ValueError("guid must be 16 bytes")
    field = (name[:INIT_NAME_CHARS - 1] + "\0").encode("utf-16-le").ljust(2 * INIT_NAME_CHARS, b"\0")
    body = struct.pack("<II", ContainerType.COMMAND, PROTOCOL_VERSION) + guid + field
    return struct.pack("<I", 4 + len(body)) + body


@dataclass(frozen=True)
class InitAck:
    guid: bytes
    model: str


def decode_init_ack(packet: bytes) -> InitAck:
    if len(packet) < 28:
        raise ProtocolError(f"init ack too short: {packet.hex()}")
    type_, result = struct.unpack_from("<II", packet, 4)
    if type_ != ContainerType.DATA or result != 0:
        raise ProtocolError(f"camera refused the connection (type {type_}, result {result})")
    model = packet[28:28 + 2 * ACK_NAME_CHARS].decode("utf-16-le", "replace").split("\0")[0]
    return InitAck(guid=bytes(packet[12:28]), model=model)


def encode_init_ack(guid: bytes, model: str) -> bytes:
    field = (model[:ACK_NAME_CHARS - 1] + "\0").encode("utf-16-le").ljust(2 * ACK_NAME_CHARS, b"\0")
    body = struct.pack("<II", ContainerType.DATA, 0) + guid + field
    return struct.pack("<I", 4 + len(body)) + body


async def read_packet(reader: asyncio.StreamReader, timeout: float | None) -> bytes:
    """Read one length-prefixed packet. Raises SessionEnded on the camera's end marker."""
    header = await asyncio.wait_for(reader.readexactly(4), timeout)
    (length,) = struct.unpack("<I", header)
    if length < 8 or length > MAX_PACKET:
        raise ProtocolError(f"implausible packet length {length}")
    packet = header + await asyncio.wait_for(reader.readexactly(length - 4), timeout)
    if packet == END_OF_SESSION:
        raise SessionEnded("camera ended the session")
    return packet


@dataclass(frozen=True)
class Response:
    code: int
    params: tuple[int, ...]
    data: bytes


class PtpConnection:
    """One TCP connection to the camera's PTP port. Transactions run one at a time."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout: float = 30.0):
        self._reader = reader
        self._writer = writer
        self._timeout = timeout
        self._transaction_id = 0
        self._lock = asyncio.Lock()
        self.ended = False

    @classmethod
    async def connect(cls, host: str, port: int, timeout: float = 30.0) -> PtpConnection:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        # asyncio disables Nagle. With it on, as with PC AutoSave, a data container waits for the camera to
        # acknowledge its command: the camera drops whatever arrives in the same read as the command.
        writer.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 0)
        return cls(reader, writer, timeout)

    @property
    def local_address(self) -> str:
        return self._writer.get_extra_info("sockname")[0]

    async def initialize(self, guid: bytes, name: str) -> InitAck:
        self._writer.write(encode_init_request(guid, name))
        await self._writer.drain()
        return decode_init_ack(await self._read())

    async def transaction(self, op: int, params: Sequence[int] = (), data: bytes | None = None) -> Response:
        # Once the command is out, the transaction runs to its end even if the caller is cancelled: otherwise its
        # response would be taken for the answer to the next command (e.g. the error shown after a user stop).
        task = asyncio.ensure_future(self._transaction(op, params, data))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # retrieved even if nobody waits
        return await asyncio.shield(task)

    async def _transaction(self, op: int, params: Sequence[int], data: bytes | None) -> Response:
        async with self._lock:
            self._transaction_id += 1
            tid = self._transaction_id
            self._writer.write(Container(ContainerType.COMMAND, op, tid, struct.pack(f"<{len(params)}I", *params)).encode())
            await self._writer.drain()
            if data is not None:
                # Separate write so Nagle holds it back until the camera acknowledged the command (see connect)
                self._writer.write(Container(ContainerType.DATA, op, tid, data).encode())
                await self._writer.drain()

            chunks: list[bytes] = []
            while True:
                container = Container.decode(await self._read())
                if container.type == ContainerType.EVENT:
                    logger.debug("ignoring event 0x%04x", container.code)
                    continue
                if container.transaction_id != tid:
                    raise ProtocolError(f"expected transaction {tid}, got {container.transaction_id}")
                if container.type == ContainerType.DATA:
                    chunks.append(container.payload)
                elif container.type == ContainerType.RESPONSE:
                    if container.code != RESPONSE_OK:
                        raise ResponseError(op, container.code)
                    count = len(container.payload) // 4
                    return Response(container.code, struct.unpack_from(f"<{count}I", container.payload), b"".join(chunks))
                else:
                    raise ProtocolError(f"unexpected container type {container.type}")

    async def send_end(self) -> None:
        """Our side of the end marker, as PC AutoSave sends it after CloseSession."""
        self.ended = True
        self._writer.write(END_OF_SESSION)
        await self._writer.drain()

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def _read(self) -> bytes:
        try:
            return await read_packet(self._reader, self._timeout)
        except SessionEnded:
            self.ended = True
            raise
        except asyncio.IncompleteReadError as e:
            self.ended = True
            raise SessionEnded("camera closed the connection") from e
