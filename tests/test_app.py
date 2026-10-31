"""The fuji_sync daemon against the fake camera over localhost."""

import asyncio
import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fujiautosave.fake import FakeCamera, FakeFile
from fujiautosave.ptp import Op
from fuji_sync import sync as sync_module
from fuji_sync.config import Config, ConfigStore, NasConfig, NasType
from fuji_sync.db import Database, default_name
from fuji_sync.nas import NasError, NasManager, NasStatus
from fuji_sync.sync import SyncService, destination
from fuji_sync.web import create_app

CAMERA_ID = "FUJIFILM-X-E2-TEST"


class StubNas(NasManager):
    def __init__(self) -> None:
        super().__init__("eth0", helper=Path("/nonexistent"))
        self.free_bytes: int | None = 10**12
        self.error: str | None = None

    async def preflight(self, nas: NasConfig) -> NasStatus:
        if self.error:
            raise NasError(self.error)
        return NasStatus(type="path", target_dir=str(nas.target_dir), mounted=True, writable=True,
                         free_bytes=self.free_bytes)


@pytest.fixture
def target(tmp_path: Path) -> Path:
    path = tmp_path / "nas"
    path.mkdir()
    return path


@pytest.fixture
def config(tmp_path: Path, target: Path) -> ConfigStore:
    store = ConfigStore(tmp_path / "config.toml")
    cfg = Config(nas=NasConfig(type=NasType.PATH, path=str(target)))
    cfg.camera.importer = "RPI-TEST"
    store.save(cfg)
    return store


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state.db")


@pytest.fixture
def nas() -> StubNas:
    return StubNas()


@pytest.fixture
async def service(config, db, nas, ports):
    svc = SyncService(config, db, nas, host="127.0.0.1", ports=ports)
    await svc.start()
    assert svc.listening, svc.listen_error
    yield svc
    await svc.stop()


async def autosave(service: SyncService, camera: FakeCamera):
    """One transfer from the fake camera; returns the camera's log and the finished job."""
    previous = service.last_job
    log = await camera.autosave(service.config.config.camera.importer)
    for _ in range(100):
        if service.job is None and service.last_job is not previous:
            return log, service.last_job
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


def nas_files(target: Path) -> dict[str, bytes]:
    return {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}


async def test_register_window(service, db, ports):
    async with FakeCamera([], ports=ports) as camera:
        with pytest.raises(asyncio.TimeoutError):
            await camera.register(timeout=0.5)
        service.open_registration(mark_existing=True)
        assert (await camera.register()).ok
        service.close_registration()
        with pytest.raises(asyncio.TimeoutError):
            await camera.register(timeout=0.5)
    cam = db.get_camera(CAMERA_ID)
    assert (cam.name, cam.model, cam.mark_existing, cam.enabled) == ("X-E2 TEST", "X-E2", True, True)
    assert service.registered == [{"camera_id": CAMERA_ID, "name": "X-E2 TEST", "model": "X-E2"}]


async def test_transfer_then_nothing_new(service, db, ports, files, target):
    async with FakeCamera(files, ports=ports) as camera:
        log, job = await autosave(service, camera)
        assert job.phase == "done", job.error
        assert (job.files_found, job.files_new, job.files_done) == (3, 3, 3)
        assert log.progress == ["0/3", "1/3", "2/3", "3/3"] and log.ended_by == "camera"
        assert nas_files(target) == {f"Fujifilm/X-E2 TEST/2026-10-31/{f.filename}": f.data for f in files}

        log, job = await autosave(service, camera)
        assert (job.phase, job.files_new, job.message) == ("done", 0, "No new files on X-E2 TEST")
        assert log.progress == ["0/0"]

        # A new photo, and a reused file number with another capture date
        files.append(FakeFile("DSCF2095.JPG", b"new", datetime(2026, 11, 1, 9, 0)))
        files.append(FakeFile("DSCF2090.JPG", b"reused number", datetime(2027, 5, 1, 9, 0)))
        log, job = await autosave(service, camera)
        assert job.files_done == 2 and log.progress == ["0/2", "1/2", "2/2"]
        assert nas_files(target)["Fujifilm/X-E2 TEST/2027-05-01/DSCF2090.JPG"] == b"reused number"
    assert db.list_transferred(CAMERA_ID)[1] == 5
    assert db.get_camera(CAMERA_ID).last_error is None


async def test_mark_existing_copies_nothing_the_first_time(service, db, ports, files, target):
    db.register_camera(CAMERA_ID, "X-E2", mark_existing=True)
    async with FakeCamera(files, ports=ports) as camera:
        log, job = await autosave(service, camera)
        assert job.phase == "done" and log.progress == ["0/0"]
        assert nas_files(target) == {}
        assert db.get_camera(CAMERA_ID).mark_existing is False
        assert all(f.nas_path == "" for f in db.list_transferred(CAMERA_ID)[0])

        files.append(FakeFile("DSCF2095.JPG", b"new", datetime(2026, 11, 1, 9, 0)))
        log, job = await autosave(service, camera)
        assert job.files_done == 1 and list(nas_files(target)) == ["Fujifilm/X-E2 TEST/2026-11-01/DSCF2095.JPG"]


async def test_unknown_camera_is_added_and_disabled_camera_copies_nothing(service, db, ports, files, target):
    async with FakeCamera(files, ports=ports, name="FUJIFILM-X-T2-0001", model="X-T2") as camera:
        await autosave(service, camera)
        cam = db.get_camera("FUJIFILM-X-T2-0001")
        assert cam is not None and cam.model == "X-T2"
        db.reset_transferred()
        db.update_camera(cam.camera_id, enabled=False)
        log, job = await autosave(service, camera)
        assert job.files_done == 0 and log.progress == ["0/0"] and log.ended_by == "camera"


async def test_nas_error_is_shown_on_the_camera(service, db, nas, ports, files, target):
    nas.error = "NAS share not mounted"
    async with FakeCamera(files, ports=ports) as camera:
        log, job = await autosave(service, camera)
    assert job.phase == "failed" and job.error == "NAS share not mounted"
    assert log.progress == ["0/0:ERR(-1)"] and log.ended_by == "client"
    assert db.get_camera(CAMERA_ID).last_error == "NAS share not mounted"


async def test_not_enough_space(service, nas, ports, files, target):
    nas.free_bytes = 1000
    async with FakeCamera(files, ports=ports) as camera:
        log, job = await autosave(service, camera)
    assert job.phase == "failed" and "Not enough space" in job.error
    assert nas_files(target) == {}


async def test_stop_then_resume(service, db, ports, files, target, monkeypatch):
    class StopAfterFirstChunk(sync_module.FileProgress):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if name == "bytes_done" and self.filename == "DSCF2094.MOV" and value >= 0x100000:
                service.stop_transfer()

    monkeypatch.setattr(sync_module, "FileProgress", StopAfterFirstChunk)
    async with FakeCamera(files, ports=ports) as camera:
        log, job = await autosave(service, camera)
        assert job.phase == "stopped" and job.files_done == 2
        assert log.progress[-1].endswith(":ERR(-1)") and log.ended_by == "client"
        part = target / "Fujifilm/X-E2 TEST/2026-10-31/DSCF2094.MOV.part"
        assert part.stat().st_size == 0x100000

        monkeypatch.setattr(sync_module, "FileProgress", sync_module.FileProgress.__mro__[1])
        log, job = await autosave(service, camera)
        assert job.phase == "done" and job.files_done == 1
        reads = [p for op, p in log.requests if op == Op.GET_PARTIAL_OBJECT]
        assert reads[0][1] == 0x100000  # resumed after the part already on the NAS
    assert nas_files(target)["Fujifilm/X-E2 TEST/2026-10-31/DSCF2094.MOV"] == files[2].data
    assert not part.exists()


async def test_camera_cancel(service, ports, files):
    async with FakeCamera(files, ports=ports, cancel_after_requests=9) as camera:
        log, job = await autosave(service, camera)
    assert job.phase == "ended" and "camera ended the transfer" in job.error and log.ended_by == "cancel"


def test_destination_layout(db):
    from fujiautosave import ObjectInfo

    cam = db.register_camera(CAMERA_ID, "X-E2")
    db.update_camera(CAMERA_ID, name="My/Fuji")
    cam = db.get_camera(CAMERA_ID)
    obj = ObjectInfo(1, "DSCF0001.JPG", 1, 0x3801, datetime(2026, 1, 2, 3, 4, 5))
    assert destination(Path("/nas"), "{camera}/{year}/{month}", cam, obj) == Path("/nas/My_Fuji/2026/01/DSCF0001.JPG")
    assert destination(Path("/nas"), "../{date}", cam, obj) == Path("/nas/2026-01-02/DSCF0001.JPG")
    undated = ObjectInfo(1, "DSCF0001.JPG", 1, 0x3801, None)
    assert destination(Path("/nas"), "{date}", cam, undated) == Path("/nas/unknown-date/DSCF0001.JPG")


def test_default_name():
    assert default_name("FUJIFILM-X-E2-BD35", "X-E2") == "X-E2 BD35"
    assert default_name("FUJIFILM-X-E2-BD35", "") == "X-E2-BD35"
    assert default_name("Camera", "X-T5") == "X-T5"


# Web API


@pytest.fixture
def client(config, db, nas, ports):
    svc = SyncService(config, db, nas, host="127.0.0.1", ports=ports)
    with TestClient(create_app(config, db, svc, nas)) as c:
        yield c


def test_status_and_registration_endpoints(client):
    assert client.get("/").status_code == 200
    status = client.get("/api/status").json()
    assert status["listening"] and status["importer"] == "RPI-TEST" and not status["registration"]["open"]
    assert client.post("/api/cameras/register", json={"mark_existing": True}).status_code == 200
    reg = client.get("/api/status").json()["registration"]
    assert reg["open"] and reg["mark_existing"] and 290 < reg["seconds_left"] <= 300
    assert client.delete("/api/cameras/register").status_code == 200
    assert not client.get("/api/status").json()["registration"]["open"]
    assert client.post("/api/transfer/stop").status_code == 409


def test_camera_and_history_endpoints(client, db):
    db.register_camera(CAMERA_ID, "X-E2")
    db.add_transferred(CAMERA_ID, "DSCF0001.JPG", "2026-01-01T00:00:00", 10, "/nas/a")
    db.add_transferred(CAMERA_ID, "DSCF0002.JPG", "2026-01-01T00:00:01", 10, "/nas/b")
    cams = client.get("/api/cameras").json()["cameras"]
    assert cams[0]["name"] == "X-E2 TEST"
    assert client.patch(f"/api/cameras/{CAMERA_ID}", json={"name": "Street", "mark_existing": True}).status_code == 200
    assert db.get_camera(CAMERA_ID).name == "Street" and db.get_camera(CAMERA_ID).mark_existing
    items = client.get("/api/transferred", params={"search": "0002"}).json()["items"]
    assert [i["filename"] for i in items] == ["DSCF0002.JPG"]
    assert client.delete(f"/api/transferred/{items[0]['id']}").status_code == 200
    assert client.delete("/api/transferred").json()["deleted"] == 1
    assert client.delete(f"/api/cameras/{CAMERA_ID}").status_code == 200
    assert client.patch(f"/api/cameras/{CAMERA_ID}", json={"name": "x"}).status_code == 404


def test_settings_rename_restarts_listener(client, config):
    assert client.put("/api/settings", json={"importer": "has space"}).status_code == 400
    assert client.put("/api/settings", json={"importer": ""}).status_code == 400
    r = client.put("/api/settings", json={"importer": "FUJI-NAS", "lan_interface": ""}).json()
    assert r["restarted"] and r["listen_error"] is None
    assert config.config.camera.importer == "FUJI-NAS" and config.config.network.lan_interface == ""
    assert client.get("/api/status").json()["importer"] == "FUJI-NAS"
    assert client.get("/api/settings").json()["port"] == 8081


def test_token_required_for_changes(client, config):
    cfg = config.config.model_copy(deep=True)
    cfg.server.api_token = "abc"
    config.save(cfg)
    assert client.post("/api/cameras/register").status_code == 401
    assert client.post("/api/cameras/register", headers={"Authorization": "Bearer abc"}).status_code == 200
    assert client.get("/api/cameras").status_code == 200


def test_port_in_use_is_reported(config, db, nas, ports):
    import socket

    blocker = socket.socket(type=socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", ports.connect))
    try:
        svc = SyncService(config, db, nas, host="127.0.0.1", ports=ports)
        with TestClient(create_app(config, db, svc, nas)) as c:
            status = c.get("/api/status").json()
        assert not status["listening"] and "Cannot listen" in status["listen_error"]
    finally:
        blocker.close()
