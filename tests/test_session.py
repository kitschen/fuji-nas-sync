"""End to end against the fake camera over localhost: handshake, session setup, listing, download, ending."""

import asyncio

import pytest
from test_ptp import CLIENT, split_packets

from fujiautosave import AutoSaveServer, AutoSaveSession, CameraInfo, ObjectChanged, SessionEnded
from fujiautosave.fake import FakeCamera
from fujiautosave.fuji import Prop
from fujiautosave.ptp import Container, Op

IMPORTER = "RPI-TEST"


def server(ports, on_session=None, on_register=None, **kwargs) -> AutoSaveServer:
    async def nothing(session):
        pass

    return AutoSaveServer(IMPORTER, on_session or nothing, on_register, host="127.0.0.1", ports=ports, **kwargs)


async def run_autosave(ports, files, on_session, **camera_args):
    """One transfer from the fake camera; returns its log once on_session has returned too."""
    handled = asyncio.Event()

    async def tracked(session):
        try:
            await on_session(session)
        finally:
            handled.set()

    async with server(ports, tracked), FakeCamera(files, ports=ports, **camera_args) as camera:
        log = await camera.autosave(IMPORTER)
        await asyncio.wait_for(handled.wait(), 5)
    return log


async def copy_all(session: AutoSaveSession) -> dict[str, bytes]:
    objects = await session.list_objects()
    received = {}
    for done, obj in enumerate(objects):
        await session.set_progress(done, len(objects))
        received[obj.filename] = b"".join([chunk async for chunk in session.download(obj)])
    await session.set_progress(len(objects), len(objects))
    await session.finish()
    return received


async def test_register(ports):
    registered: list[CameraInfo] = []
    async with server(ports, on_register=registered.append, registration_open=True), \
            FakeCamera([], ports=ports) as camera:
        reply = await camera.register()
    assert reply.ok
    assert reply.headers == {"FOLDER": "guest", "SERVICENAME": "PCAUTOSAVE/1.0"}
    assert registered == [CameraInfo("FUJIFILM-X-E2-TEST", "127.0.0.1", "X-E2")]


async def test_no_answer_while_registration_closed(ports):
    async with server(ports), FakeCamera([], ports=ports) as camera:
        with pytest.raises(asyncio.TimeoutError):
            await camera.register(timeout=0.5)


async def test_no_answer_to_other_destination_name(ports):
    async with server(ports), FakeCamera([], ports=ports) as camera:
        with pytest.raises(asyncio.TimeoutError):
            await camera.autosave("SOMEONE-ELSE", timeout=0.5)


async def test_transfer(ports, files):
    received = {}

    async def on_session(session):
        assert session.camera.name == "FUJIFILM-X-E2-TEST"
        assert session.model == "X-E2"
        assert session.object_count == 3
        received.update(await copy_all(session))
        assert session.ended

    log = await run_autosave(ports, files, on_session)

    assert received == {f.filename: f.data for f in files}
    assert log.client_name == "FUJIFILM-X-E2-TEST"
    assert log.progress == ["0/3", "1/3", "2/3", "3/3"]
    assert log.ended_by == "camera"
    chunks = [p for op, p in log.requests if op == Op.GET_PARTIAL_OBJECT and p[0] == 3]
    assert [c[1:] for c in chunks] == [(0, 0x100000), (0x100000, 0x100000), (0x200000, 2_500_000 - 0x200000)]


async def test_session_setup_matches_pc_autosave(ports, files):
    """Our opening sequence equals the captured one, minus PC AutoSave's extra event polls."""
    captured = []
    for packet in split_packets(CLIENT)[1:]:
        c = Container.decode(packet)
        if c.type == 1:
            captured.append((c.code, tuple(int.from_bytes(c.payload[i:i + 4], "little")
                                           for i in range(0, len(c.payload), 4))))
    polls = (Op.GET_DEVICE_PROP_VALUE, (Prop.EVENTS,))
    expected = [captured[0], captured[1]] + [r for r in captured[2:] if r != polls]

    log = await run_autosave(ports, files, copy_all)
    assert log.requests[:6] == expected[:6]
    assert log.requests[6] == (Op.GET_OBJECT_INFO, (1,))


async def test_resume_from_offset(ports, files):
    received = []

    async def on_session(session):
        objects = await session.list_objects()
        received.append(b"".join([c async for c in session.download(objects[0], offset=1_000_000)]))
        await session.set_progress(1, 1)
        await session.finish()

    await run_autosave(ports, files, on_session)
    assert received == [files[0].data[1_000_000:]]


async def test_error_ending(ports, files):
    async def on_session(session):
        await session.set_progress(1, 3, error=True)
        await session.finish()

    log = await run_autosave(ports, files, on_session)
    assert log.progress == ["1/3:ERR(-1)"]
    assert log.requests[-2:] == [(Op.SET_DEVICE_PROP_VALUE, (Prop.ENABLE_CORRECT_FILE_SIZE,)),
                                 (Op.CLOSE_SESSION, ())]
    assert log.ended_by == "client"


async def test_cancel_on_camera(ports, files):
    errors = []

    async def on_session(session):
        try:
            await session.list_objects()
        except SessionEnded as e:
            errors.append(e)
            assert session.ended
            await session.finish()  # no-op once the camera hung up

    log = await run_autosave(ports, files, on_session, cancel_after_requests=7)
    assert len(errors) == 1
    assert log.ended_by == "cancel"


async def test_object_changed(ports, files):
    errors = []

    async def on_session(session):
        objects = await session.list_objects()
        files.reverse()  # files deleted / added on the camera: handles now point elsewhere
        try:
            async for _ in session.download(objects[0]):
                pass
        except ObjectChanged as e:
            errors.append(e)
        await session.set_progress(0, 1, error=True)
        await session.finish()

    await run_autosave(ports, files, on_session)
    assert len(errors) == 1
