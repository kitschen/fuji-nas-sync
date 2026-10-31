from datetime import datetime

from test_ptp import CAMERA, split_packets

from fujiautosave.fuji import (
    Format,
    ObjectInfo,
    Prop,
    encode_object_info,
    parse_events,
    parse_object_info,
    progress_text,
)
from fujiautosave.ptp import Container, ContainerType, Op


def camera_data(op: int) -> list[bytes]:
    containers = [Container.decode(p) for p in split_packets(CAMERA[0x44:])[:-1]]  # skip init ack and end marker
    return [c.payload for c in containers if c.type == ContainerType.DATA and c.code == op]


def test_events_from_capture():
    first, *rest = camera_data(Op.GET_DEVICE_PROP_VALUE)
    assert parse_events(first) == {Prop.CAMERA_STATE: 3, Prop.OBJECT_COUNT: 12}
    assert parse_events(rest[1]) == {}  # rest[0] is the AutoSave version


def test_object_infos_from_capture():
    infos = [parse_object_info(h, d) for h, d in enumerate(camera_data(Op.GET_OBJECT_INFO), start=1)]
    assert [i.filename for i in infos] == [
        "DSCF4492.JPG", "DSCF4493.JPG", "DSCF4498.RAF", "DSCF4556.JPG", "DSCF4557.JPG", "DSCF4560.RAF",
        "DSCF4561.JPG", "DSCF4562.JPG", "DSCF4558.JPG", "DSCF4558.RAF", "DSCF4559.JPG", "DSCF4559.RAF",
    ]
    assert infos[0] == ObjectInfo(1, "DSCF4492.JPG", 5_046_207, Format.JPEG, datetime(2016, 2, 25, 21, 10, 38),
                                  "Orientation:8", 0x10000001)
    assert infos[2].format == Format.RAF and infos[2].size == 33_465_856


def test_object_info_round_trip():
    info = ObjectInfo(5, "DSCF2094.MOV", 29_460_416, Format.MOV, datetime(2026, 10, 31, 15, 38, 10), "", 0x10000001)
    assert parse_object_info(5, encode_object_info(info)) == info


def test_progress_text():
    assert progress_text(2, 5) == "2/5"
    assert progress_text(4, 5, error=True) == "4/5:ERR(-1)"
