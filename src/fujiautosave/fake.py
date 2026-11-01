"""A fake camera that behaves like the X-E2 in the captures, for tests and for developing without a camera."""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass, field
from datetime import datetime

from fujiautosave.discovery import IMPORT_REPLY, Message, Ports, discover_message, read_message
from fujiautosave.fuji import (
    CAMERA_STATE_PC_AUTOSAVE,
    Format,
    ObjectInfo,
    Prop,
    encode_events,
    encode_object_info,
)
from fujiautosave.ptp import (
    END_OF_SESSION,
    PROTOCOL_VERSION,
    RESPONSE_OK,
    Container,
    ContainerType,
    Op,
    SessionEnded,
    encode_init_ack,
    read_packet,
    unpack_string,
)

logger = logging.getLogger(__name__)

INVALID_OBJECT_HANDLE = 0x2009
OPERATION_NOT_SUPPORTED = 0x2005
DEVICE_PROP_NOT_SUPPORTED = 0x200A
CAMERA_GUID = bytes.fromhex("0870b0610a8b4593b2e79357dd36e050")


@dataclass
class FakeFile:
    filename: str
    data: bytes
    captured: datetime
    format: int = Format.JPEG


@dataclass
class FakeSessionLog:
    client_name: str = ""
    requests: list[tuple[int, tuple[int, ...]]] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)
    ended_by: str = ""  # "camera" (complete), "client" (end marker after CloseSession), "cancel", "disconnect"


class FakeCamera:
    def __init__(self, files: list[FakeFile], *, name: str = "FUJIFILM-X-E2-TEST", model: str = "X-E2",
                 host: str = "127.0.0.1", ports: Ports = Ports(), cancel_after_requests: int | None = None):
        self.files = files
        self.name = name
        self.model = model
        self.cancel_after_requests = cancel_after_requests
        self.sessions: list[FakeSessionLog] = []
        self._host = host
        self._ports = ports
        self._servers: list[asyncio.Server] = []
        self._notifies: asyncio.Queue[Message] = asyncio.Queue()
        self._session_done = asyncio.Event()

    async def start(self) -> None:
        self._servers.append(await asyncio.start_server(self._handle_notify, self._host, self._ports.notify))
        self._servers.append(await asyncio.start_server(self._handle_ptp, self._host, self._ports.ptp))

    async def stop(self) -> None:
        for server in self._servers:
            server.close()
            await server.wait_closed()
        self._servers.clear()

    async def __aenter__(self) -> FakeCamera:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def register(self, destination: str = "127.0.0.1", timeout: float = 5.0) -> Message:
        """Camera menu PC AUTO SAVE SETTING: find a destination and register with it. Returns its answer."""
        notify = await self._discover("*", destination, self._ports.register, timeout)
        logger.info("fake camera: registering with %s", notify.headers.get("IMPORTER"))
        return await self._request(destination, self._ports.register,
                                   f"REGISTER * HTTP/1.1\r\nHOST:{destination}:{self._ports.register}\r\n"
                                   f"DSCNAME:{self.name}\r\nDSCMODEL:{self.model}\r\n", timeout)

    async def autosave(self, importer: str, destination: str = "127.0.0.1", timeout: float = 10.0) -> FakeSessionLog:
        """Camera menu PC AUTO SAVE: call the registered destination and serve files until it is done."""
        await self._discover(importer, destination, self._ports.connect, timeout)
        self._session_done.clear()
        reply = await self._request(destination, self._ports.connect,
                                    f"IMPORT /guest HTTP/1.1\r\nHOST:{destination}:{self._ports.connect}\r\n"
                                    f"DSCNAME:{self.name}\r\nDSCPORT: \r\n", timeout)
        if not reply.ok:
            raise ConnectionError(f"destination refused IMPORT: {reply.target}")
        await asyncio.wait_for(self._session_done.wait(), timeout)
        return self.sessions[-1]

    async def _discover(self, target: str, destination: str, port: int, timeout: float) -> Message:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=(destination, port))
        try:
            transport.sendto(discover_message(target, self._host))
        finally:
            transport.close()
        return await asyncio.wait_for(self._notifies.get(), timeout)

    async def _request(self, destination: str, port: int, text: str, timeout: float) -> Message:
        reader, writer = await asyncio.open_connection(destination, port)
        try:
            writer.write(text.encode())
            await writer.drain()
            return await read_message(reader, timeout=timeout)
        finally:
            writer.close()

    async def _handle_notify(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            message = await read_message(reader)
            if message.method == "NOTIFY":
                writer.write(b"HTTP/1.1 200 OK\r\n")
                await writer.drain()
                await self._notifies.put(message)
        finally:
            writer.close()

    async def _handle_ptp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log = FakeSessionLog()
        self.sessions.append(log)
        events_read = False
        try:
            init = await read_packet(reader, 10)
            if struct.unpack_from("<II", init, 4) != (ContainerType.COMMAND, PROTOCOL_VERSION):
                raise ConnectionError(f"bad init request {init.hex()}")
            log.client_name = init[28:].decode("utf-16-le").split("\0")[0]
            writer.write(encode_init_ack(CAMERA_GUID, self.model))
            while True:
                command = Container.decode(await read_packet(reader, 30))
                params = struct.unpack_from(f"<{len(command.payload) // 4}I", command.payload)
                log.requests.append((command.code, params))
                if self.cancel_after_requests is not None and len(log.requests) > self.cancel_after_requests:
                    log.ended_by = "cancel"
                    writer.write(END_OF_SESSION)
                    return
                if command.code == Op.SET_DEVICE_PROP_VALUE and params[0] == Prop.ENABLE_CORRECT_FILE_SIZE \
                        and self._complete(log):
                    log.ended_by = "camera"  # like the X-E2: hang up without waiting for the data phase
                    writer.write(END_OF_SESSION)
                    return
                data = None
                if command.code == Op.SET_DEVICE_PROP_VALUE:
                    data = Container.decode(await read_packet(reader, 30)).payload
                if command.code == Op.GET_DEVICE_PROP_VALUE and params[0] == Prop.EVENTS:
                    code, payload = RESPONSE_OK, encode_events(
                        {} if events_read else {Prop.CAMERA_STATE: CAMERA_STATE_PC_AUTOSAVE,
                                                Prop.OBJECT_COUNT: len(self.files)})
                    events_read = True
                else:
                    code, payload = self._execute(command.code, params, data, log)
                tid = command.transaction_id
                if payload is not None:
                    writer.write(Container(ContainerType.DATA, command.code, tid, payload).encode())
                writer.write(Container(ContainerType.RESPONSE, code, tid).encode())
                await writer.drain()
        except SessionEnded:
            log.ended_by = "client"
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            log.ended_by = log.ended_by or "disconnect"
        finally:
            writer.close()
            self._session_done.set()

    @staticmethod
    def _complete(log: FakeSessionLog) -> bool:
        if not log.progress or "ERR" in log.progress[-1]:
            return False
        done, total = log.progress[-1].split("/")
        return done == total

    def _execute(self, op: int, params: tuple[int, ...], data: bytes | None,
                 log: FakeSessionLog) -> tuple[int, bytes | None]:
        if op in (Op.OPEN_SESSION, Op.CLOSE_SESSION):
            return RESPONSE_OK, None
        if op == Op.GET_DEVICE_PROP_VALUE:
            if params[0] == Prop.AUTOSAVE_VERSION:
                return RESPONSE_OK, struct.pack("<I", 1)
            return DEVICE_PROP_NOT_SUPPORTED, None
        if op == Op.SET_DEVICE_PROP_VALUE:
            if params[0] == Prop.PROGRESS:
                log.progress.append(unpack_string(data)[0])
            return RESPONSE_OK, None
        if op in (Op.GET_OBJECT_INFO, Op.GET_PARTIAL_OBJECT):
            if not 1 <= params[0] <= len(self.files):
                return INVALID_OBJECT_HANDLE, None
            f = self.files[params[0] - 1]
            if op == Op.GET_OBJECT_INFO:
                info = ObjectInfo(params[0], f.filename, len(f.data), f.format, f.captured,
                                  "" if f.format == Format.MOV else "Orientation:1", 0x10000001)
                return RESPONSE_OK, encode_object_info(info)
            _, offset, length = params
            return RESPONSE_OK, f.data[offset:offset + length]
        return OPERATION_NOT_SUPPORTED, None
