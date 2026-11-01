"""One PC AutoSave transfer, from the camera's IMPORT until it hangs up.

The order of the setup commands copies Fujifilm's PC AutoSave (see docs/protocol.md). Don't reorder them without
testing on a camera.
"""

from __future__ import annotations

import logging
import socket
import struct
from collections.abc import AsyncIterator, Callable, Iterable

from fujiautosave.discovery import CameraInfo
from fujiautosave.fuji import (
    CAMERA_STATE_PC_AUTOSAVE,
    CLIENT_STATE_AUTOSAVE,
    ObjectInfo,
    Prop,
    parse_events,
    parse_object_info,
    progress_text,
)
from fujiautosave.ptp import Op, ProtocolError, PtpConnection, PtpError, ResponseError, SessionEnded, pack_string

logger = logging.getLogger(__name__)

CHUNK_SIZE = 0x100000  # what PC AutoSave asks for; the camera's maximum
STORAGE_ID = 0x10000001  # the SD card, as in every ObjectInfo
KEEPALIVE_EVERY = 20  # PC AutoSave polls the events list now and then while reading object infos


class ObjectChanged(PtpError):
    """The file at this handle is not the one listed earlier (files were added or deleted on the camera)."""


def session_guid(guid: bytes, local_address: str) -> bytes:
    """PC AutoSave's client GUID ends with its own IPv4 address, reversed. We do the same to be safe."""
    try:
        return guid[:12] + socket.inet_aton(local_address)[::-1]
    except OSError:
        return guid


class AutoSaveSession:
    def __init__(self, conn: PtpConnection, camera: CameraInfo, model: str, object_count: int):
        self._conn = conn
        self.camera = camera
        self.model = model
        self.object_count = object_count

    @classmethod
    async def open(cls, camera: CameraInfo, guid: bytes, port: int = 55740, timeout: float = 30.0) -> AutoSaveSession:
        conn = await PtpConnection.connect(camera.address, port, timeout)
        try:
            ack = await conn.initialize(session_guid(guid, conn.local_address), camera.name)
            await conn.transaction(Op.OPEN_SESSION, (1,))
            events = parse_events((await conn.transaction(Op.GET_DEVICE_PROP_VALUE, (Prop.EVENTS,))).data)
            state = events.get(Prop.CAMERA_STATE)
            if state != CAMERA_STATE_PC_AUTOSAVE:
                raise ProtocolError(f"camera is not in PC AutoSave mode (state {state})")
            await conn.transaction(Op.SET_DEVICE_PROP_VALUE, (Prop.CLIENT_STATE,),
                                   struct.pack("<H", CLIENT_STATE_AUTOSAVE))
            version = (await conn.transaction(Op.GET_DEVICE_PROP_VALUE, (Prop.AUTOSAVE_VERSION,))).data
            await conn.transaction(Op.SET_DEVICE_PROP_VALUE, (Prop.AUTOSAVE_VERSION,), version)
            await conn.transaction(Op.SET_DEVICE_PROP_VALUE, (Prop.ENABLE_CORRECT_FILE_SIZE,), struct.pack("<H", 1))
        except BaseException:
            await conn.close()
            raise
        model = camera.model or ack.model
        logger.info("session with %s (%s): %d objects", camera.name, model, events.get(Prop.OBJECT_COUNT, 0))
        return cls(conn, camera, model, events.get(Prop.OBJECT_COUNT, 0))

    @property
    def ended(self) -> bool:
        """True once the camera hung up (finished or cancelled)."""
        return self._conn.ended

    async def poll_events(self) -> dict[int, int]:
        """Read the events list. Also serves as keep-alive while the application is busy."""
        return parse_events((await self._conn.transaction(Op.GET_DEVICE_PROP_VALUE, (Prop.EVENTS,))).data)

    async def object_info(self, handle: int) -> ObjectInfo:
        return parse_object_info(handle, (await self._conn.transaction(Op.GET_OBJECT_INFO, (handle,))).data)

    async def list_objects(self, on_progress: Callable[[int, int], None] | None = None) -> list[ObjectInfo]:
        """Every file on the card. Slow on a full card: about four files per second."""
        objects = []
        async for obj in self.iter_objects():
            objects.append(obj)
            if on_progress:
                on_progress(len(objects), self.object_count)
        return objects

    async def iter_objects(self, newest_first: bool = False,
                           handles: Iterable[int] | None = None) -> AsyncIterator[ObjectInfo]:
        """The files one by one, by handle (all of them unless `handles` is given). `newest_first` starts at the end
        of the card, where new photos usually are; files shot after deleting others can fill the gaps instead."""
        if handles is None:
            handles = range(self.object_count, 0, -1) if newest_first else range(1, self.object_count + 1)
        for read, handle in enumerate(handles, start=1):
            yield await self.object_info(handle)
            if read % KEEPALIVE_EVERY == 0:
                await self.poll_events()

    async def free_space(self, storage_id: int = STORAGE_ID) -> int | None:
        """Free bytes on the card (GetStorageInfo), or None if the camera refuses it. PC AutoSave never asks."""
        try:
            data = (await self._conn.transaction(Op.GET_STORAGE_INFO, (storage_id,))).data
        except ResponseError as e:
            logger.info("camera refuses GetStorageInfo: 0x%04x", e.code)
            return None
        # StorageType, FilesystemType, AccessCapability (u16 each), MaxCapacity (u64), FreeSpaceInBytes (u64)
        return struct.unpack_from("<Q", data, 14)[0] if len(data) >= 22 else None

    async def set_progress(self, done: int, total: int, error: bool = False) -> None:
        """Text on the camera's screen, e.g. 3/5."""
        await self._conn.transaction(Op.SET_DEVICE_PROP_VALUE, (Prop.PROGRESS,),
                                     pack_string(progress_text(done, total, error)))

    async def download(self, obj: ObjectInfo, offset: int = 0) -> AsyncIterator[bytes]:
        """Stream the file from `offset` on, in chunks of up to 1 MiB."""
        current = await self.object_info(obj.handle)
        if (current.filename, current.size, current.captured) != (obj.filename, obj.size, obj.captured):
            raise ObjectChanged(f"handle {obj.handle} is now {current.filename}, expected {obj.filename}")
        position = offset
        while position < obj.size:
            length = min(CHUNK_SIZE, obj.size - position)
            data = (await self._conn.transaction(Op.GET_PARTIAL_OBJECT, (obj.handle, position, length))).data
            if not data:
                raise ProtocolError(f"camera sent no data for {obj.filename} at offset {position}")
            position += len(data)
            yield data

    async def finish(self) -> None:
        """End the session the way PC AutoSave does. Call set_progress first: done == total for success, or
        error=True. After a complete transfer the camera hangs up as soon as it sees the final command; otherwise
        it answers and we close the session ourselves."""
        if self.ended:
            return
        try:
            await self._conn.transaction(Op.SET_DEVICE_PROP_VALUE, (Prop.ENABLE_CORRECT_FILE_SIZE,),
                                         struct.pack("<H", 0))
            await self._conn.transaction(Op.CLOSE_SESSION)
            await self._conn.send_end()
        except (SessionEnded, ConnectionError):
            pass  # the camera may hang up before our data phase has been written

    async def close(self) -> None:
        await self._conn.close()

    async def __aenter__(self) -> AutoSaveSession:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
