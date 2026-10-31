"""Listens for cameras that start PC AutoSave and hands each transfer to the application."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from fujiautosave.discovery import (
    BUSY_REPLY,
    IMPORT_REPLY,
    REGISTER_REPLY,
    SERVICE,
    CameraInfo,
    Message,
    Ports,
    read_message,
    send_notify,
)
from fujiautosave.session import AutoSaveSession

logger = logging.getLogger(__name__)

SessionHandler = Callable[[AutoSaveSession], Awaitable[None]]
RegisterHandler = Callable[[CameraInfo], Awaitable[None] | None]


def default_guid(importer: str) -> bytes:
    """Stable per destination name, so the camera sees the same client every time."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"{importer}.fujiautosave").bytes


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self, handler: Callable[[bytes, str], None]):
        self._handler = handler

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._handler(data, addr[0])


class AutoSaveServer:
    """Plays the part of Fujifilm's PC AutoSave software under the destination name `importer`.

    `on_session` runs for each transfer; the session is closed when it returns. One transfer at a time: a second
    camera calling in meanwhile is turned away and can retry. While `registration_open` is set, cameras can
    register this destination (camera menu: PC AUTO SAVE SETTING); `on_register` is called for each.
    """

    def __init__(self, importer: str, on_session: SessionHandler, on_register: RegisterHandler | None = None, *,
                 guid: bytes | None = None, host: str = "0.0.0.0", ports: Ports = Ports(),
                 registration_open: bool = False):
        self.importer = importer
        self.guid = guid or default_guid(importer)
        self.registration_open = registration_open
        self._on_session = on_session
        self._on_register = on_register
        self._host = host
        self._ports = ports
        self._transports: list[asyncio.BaseTransport] = []
        self._servers: list[asyncio.Server] = []
        self._tasks: set[asyncio.Task] = set()
        self._session_lock = asyncio.Lock()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            for port, handler in ((self._ports.register, self._on_register_discover),
                                  (self._ports.connect, self._on_connect_discover)):
                transport, _ = await loop.create_datagram_endpoint(lambda h=handler: _Datagrams(h),
                                                                   local_addr=(self._host, port))
                self._transports.append(transport)
            self._servers.append(await asyncio.start_server(self._handle_register, self._host, self._ports.register))
            self._servers.append(await asyncio.start_server(self._handle_import, self._host, self._ports.connect))
        except BaseException:
            await self.stop()
            raise
        logger.info("listening for PC AutoSave as %r", self.importer)

    async def stop(self) -> None:
        for transport in self._transports:
            transport.close()
        for server in self._servers:
            server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for server in self._servers:
            await server.wait_closed()
        await asyncio.sleep(0)  # datagram sockets close on the next loop iteration; free the ports for a restart
        self._transports.clear()
        self._servers.clear()

    async def __aenter__(self) -> AutoSaveServer:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    @property
    def busy(self) -> bool:
        return self._session_lock.locked()

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_register_discover(self, data: bytes, sender: str) -> None:
        message = self._parse_discover(data, sender)
        if message and message.target == "*" and self.registration_open:
            logger.info("camera at %s wants to register", sender)
            self._spawn(self._notify(sender))

    def _on_connect_discover(self, data: bytes, sender: str) -> None:
        message = self._parse_discover(data, sender)
        if message and message.target == self.importer:
            logger.info("camera at %s starts PC AutoSave", sender)
            self._spawn(self._notify(sender))

    @staticmethod
    def _parse_discover(data: bytes, sender: str) -> Message | None:
        try:
            message = Message.parse(data)
        except ValueError:
            logger.debug("ignoring datagram from %s: %r", sender, data[:80])
            return None
        if message.method != "DISCOVER" or message.headers.get("SERVICE") != SERVICE:
            return None
        return message

    async def _notify(self, camera_address: str) -> None:
        try:
            await send_notify(camera_address, self.importer, self._ports.notify)
        except (OSError, asyncio.TimeoutError) as e:
            logger.warning("NOTIFY to %s failed: %s", camera_address, e)

    async def _handle_register(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        address = writer.get_extra_info("peername")[0]
        try:
            message = await read_message(reader, until_header="DSCMODEL")
            if message.method != "REGISTER":
                return
            if not self.registration_open:
                writer.write(BUSY_REPLY)
                return
            camera = CameraInfo(message.headers.get("DSCNAME", ""), address, message.headers.get("DSCMODEL", ""))
            writer.write(REGISTER_REPLY)
            await writer.drain()
            logger.info("registered %s (%s) at %s", camera.name, camera.model, address)
            if self._on_register:
                result = self._on_register(camera)
                if result is not None:
                    await result
        except (OSError, asyncio.TimeoutError, ValueError) as e:
            logger.warning("REGISTER from %s failed: %s", address, e)
        finally:
            writer.close()

    async def _handle_import(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        address = writer.get_extra_info("peername")[0]
        try:
            message = await read_message(reader, until_header="DSCPORT")
            if message.method != "IMPORT":
                return
            if self.busy:
                logger.warning("turning away %s: another transfer is running", address)
                writer.write(BUSY_REPLY)
                return
            camera = CameraInfo(message.headers.get("DSCNAME", ""), address)
            writer.write(IMPORT_REPLY)
            await writer.drain()
        except (OSError, asyncio.TimeoutError, ValueError) as e:
            logger.warning("IMPORT from %s failed: %s", address, e)
            return
        finally:
            writer.close()
        async with self._session_lock:
            await self._run_session(camera)

    async def _run_session(self, camera: CameraInfo) -> None:
        try:
            session = await AutoSaveSession.open(camera, self.guid, self._ports.ptp)
        except Exception:
            logger.exception("could not open a session with %s at %s", camera.name, camera.address)
            return
        async with session:
            try:
                await self._on_session(session)
            except Exception:
                logger.exception("transfer from %s failed", camera.name)
