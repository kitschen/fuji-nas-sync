"""Fujifilm device properties and object formats used by PC AutoSave, and their encodings."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from fujiautosave.ptp import ProtocolError, pack_string, unpack_string


class Prop(IntEnum):
    EVENTS = 0xD212  # list of (property, value) pairs that changed since the last read
    OBJECT_COUNT = 0xD222
    ENABLE_CORRECT_FILE_SIZE = 0xD227  # 1 while transferring, 0 to end the session
    PROGRESS = 0xD228  # text the camera shows, e.g. "2/5" or "2/5:ERR(-1)"
    CAMERA_STATE = 0xDF00
    CLIENT_STATE = 0xDF01
    AUTOSAVE_VERSION = 0xDF23


CAMERA_STATE_PC_AUTOSAVE = 3
CLIENT_STATE_AUTOSAVE = 3


class Format(IntEnum):
    MOV = 0x300D
    JPEG = 0x3801
    RAF = 0xB103


def parse_events(data: bytes) -> dict[int, int]:
    if len(data) < 2:
        raise ProtocolError(f"events list too short: {data.hex()}")
    (count,) = struct.unpack_from("<H", data)
    if len(data) < 2 + 6 * count:
        raise ProtocolError(f"events list truncated: {data.hex()}")
    return dict(struct.unpack_from("<HI", data, 2 + 6 * i) for i in range(count))


def encode_events(events: dict[int, int]) -> bytes:
    return struct.pack("<H", len(events)) + b"".join(struct.pack("<HI", k, v) for k, v in events.items())


def progress_text(done: int, total: int, error: bool = False) -> str:
    return f"{done}/{total}" + (":ERR(-1)" if error else "")


DATE_FORMAT = "%Y%m%dT%H%M%S"


@dataclass(frozen=True)
class ObjectInfo:
    handle: int  # position on the card for this session only; it changes when files are added or deleted
    filename: str
    size: int
    format: int
    captured: datetime | None  # camera local time
    keywords: str = ""
    storage_id: int = 0


# StorageID, ObjectFormat, ProtectionStatus, ObjectCompressedSize, then thumbnail and image fields we skip
_HEADER = struct.Struct("<IHHI")
_STRINGS_OFFSET = 52


def parse_object_info(handle: int, data: bytes) -> ObjectInfo:
    if len(data) < _STRINGS_OFFSET + 1:
        raise ProtocolError(f"object info too short: {data.hex()}")
    storage_id, fmt, _protection, size = _HEADER.unpack_from(data)
    filename, offset = unpack_string(data, _STRINGS_OFFSET)
    captured_text, offset = unpack_string(data, offset)
    _modified, offset = unpack_string(data, offset)
    keywords, offset = unpack_string(data, offset) if offset < len(data) else ("", offset)
    try:
        captured = datetime.strptime(captured_text[:15], DATE_FORMAT) if captured_text else None
    except ValueError:
        captured = None
    return ObjectInfo(handle, filename, size, fmt, captured, keywords, storage_id)


def encode_object_info(info: ObjectInfo) -> bytes:
    header = _HEADER.pack(info.storage_id, info.format, 0, info.size)
    captured = info.captured.strftime(DATE_FORMAT) if info.captured else ""
    return (header.ljust(_STRINGS_OFFSET, b"\0") + pack_string(info.filename) + pack_string(captured)
            + pack_string("") + pack_string(info.keywords))
