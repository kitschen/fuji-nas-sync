"""Transfer service: listens for cameras starting PC AutoSave and copies their new files to the NAS.

The camera starts every transfer, so there is no "sync now": the service keeps the PC AutoSave listener
running and turns each camera connection into a job that the web UI can follow and stop.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fujiautosave import AutoSaveServer, AutoSaveSession, CameraInfo, ObjectInfo, Ports, SessionEnded
from fujiautosave.server import default_guid

from fuji_sync.config import ConfigStore
from fuji_sync.db import Camera, Database, FileKey
from fuji_sync.nas import NasError, NasManager

logger = logging.getLogger(__name__)


class Busy(RuntimeError):
    pass


@dataclass
class FileProgress:
    filename: str
    nas_path: str
    bytes_done: int = 0
    bytes_total: int | None = None
    started_at: float = field(default_factory=time.time)
    resumed_from: int = 0

    @property
    def speed(self) -> float:
        elapsed = time.time() - self.started_at
        return (self.bytes_done - self.resumed_from) / elapsed if elapsed > 0 else 0.0


STEP_LABELS = {
    "nas": "Check NAS",
    "list": "Read file list",
    "mark": "Mark files as copied",
    "transfer": "Transfer files",
    "skip": "Camera disabled",
    "finish": "End session on the camera",
}


@dataclass
class Step:
    key: str
    label: str
    detail: str = ""
    state: str = "running"  # running | done | failed | stopped
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


@dataclass
class Job:
    id: str
    camera_id: str
    camera_name: str
    address: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    phase: str = "starting"
    message: str = ""
    list_done: int = 0
    list_total: int = 0
    files_found: int = 0
    files_new: int = 0
    files_done: int = 0
    bytes_done: int = 0
    queue_index: int = 0
    current: FileProgress | None = None
    steps: list[Step] = field(default_factory=list)
    error: str | None = None

    def step(self, key: str, detail: str = "") -> None:
        """Enter step `key` (finishing the running one), or update the running step's detail."""
        running = self._running()
        if running is None or running.key != key:
            if running is not None:
                running.state, running.finished_at = "done", time.time()
            self.steps.append(Step(key=key, label=STEP_LABELS.get(key, key)))
            running = self.steps[-1]
        if detail:
            running.detail = self.message = detail

    def end_step(self, error: BaseException | None = None) -> None:
        if (running := self._running()) is None:
            return
        running.finished_at = time.time()
        if error is None:
            running.state = "done"
        elif isinstance(error, asyncio.CancelledError):
            running.state = "stopped"
        else:
            running.state, running.error = "failed", str(error) or repr(error)

    def _running(self) -> Step | None:
        return self.steps[-1] if self.steps and self.steps[-1].state == "running" else None

    def snapshot(self) -> dict:
        data = asdict(self)
        if self.current:
            data["current"]["speed"] = self.current.speed
            remaining = (self.current.bytes_total or 0) - self.current.bytes_done
            data["current"]["eta"] = remaining / self.current.speed if self.current.speed and remaining > 0 else None
        now = time.time()
        for step in data["steps"]:
            step["elapsed"] = (step["finished_at"] or now) - step["started_at"]
        return data


_SAFE = re.compile(r"[^A-Za-z0-9._ ()\-]+")


def safe_name(value: str) -> str:
    return _SAFE.sub("_", value).strip(" .") or "camera"


def file_key(obj: ObjectInfo) -> FileKey:
    return obj.filename, obj.captured.isoformat() if obj.captured else "", obj.size


def destination(target_dir: Path, layout: str, camera: Camera, obj: ObjectInfo) -> Path:
    captured = obj.captured
    rel = layout.format(
        camera=safe_name(camera.name),
        date=captured.strftime("%Y-%m-%d") if captured else "unknown-date",
        year=captured.strftime("%Y") if captured else "unknown-year",
        month=captured.strftime("%m") if captured else "unknown-month",
    )
    parts = [safe_name(p) for p in Path(rel).parts if p not in ("", ".", "..", "/", "\\")]
    return target_dir.joinpath(*parts, safe_name(obj.filename))


class SyncService:
    def __init__(self, config: ConfigStore, db: Database, nas: NasManager, *, host: str = "0.0.0.0",
                 ports: Ports = Ports()) -> None:
        self.config = config
        self.db = db
        self.nas = nas
        self._host = host
        self._ports = ports
        self._server: AutoSaveServer | None = None
        self.listen_error: str | None = None
        self._task: asyncio.Task | None = None
        self.job: Job | None = None
        self.last_job: Job | None = None
        self.registration_until: float | None = None
        self._registration_mark_existing = False
        self._registration_timer: asyncio.TimerHandle | None = None
        self.registered: list[dict] = []  # cameras registered during the current / last window

    # Listener

    async def start(self) -> None:
        importer = self.config.config.camera.importer
        server = AutoSaveServer(importer, self._on_session, self._on_register, guid=default_guid(importer),
                                host=self._host, ports=self._ports)
        try:
            await server.start()
        except OSError as e:
            busy = getattr(e, "winerror", None) == 10048 or e.errno in (errno.EADDRINUSE, errno.EACCES)
            self.listen_error = (
                f"Cannot listen on the PC AutoSave ports {self._ports.connect}/{self._ports.register}: {e}."
                + (" Is another PC AutoSave receiver running on this machine?" if busy else "")
            )
            logger.error(self.listen_error)
            return
        self.listen_error = None
        self._server = server

    async def stop(self) -> None:
        self.close_registration()
        if self._server:
            await self._server.stop()
            self._server = None

    async def restart(self) -> None:
        """Apply a changed destination name. Refused while a transfer is running."""
        if self.busy:
            raise Busy("A transfer is running")
        await self.stop()
        await self.start()

    @property
    def listening(self) -> bool:
        return self._server is not None

    @property
    def busy(self) -> bool:
        return self.job is not None

    def status(self) -> dict:
        now = time.time()
        return {
            "busy": self.busy,
            "listening": self.listening,
            "listen_error": self.listen_error,
            "importer": self.config.config.camera.importer,
            "registration": {
                "open": self.registration_until is not None,
                "seconds_left": max(0.0, self.registration_until - now) if self.registration_until else 0,
                "mark_existing": self._registration_mark_existing,
                "registered": self.registered,
            },
            "job": self.job.snapshot() if self.job else None,
            "last_job": self.last_job.snapshot() if self.last_job else None,
        }

    # Registration

    def open_registration(self, mark_existing: bool = False, minutes: float | None = None) -> float:
        if not self._server:
            raise RuntimeError(self.listen_error or "The PC AutoSave listener is not running")
        minutes = minutes or self.config.config.camera.registration_minutes
        self.close_registration()
        self._server.registration_open = True
        self._registration_mark_existing = mark_existing
        self.registration_until = time.time() + minutes * 60
        self.registered = []
        self._registration_timer = asyncio.get_running_loop().call_later(minutes * 60, self.close_registration)
        logger.info("registration open for %.0f minute(s)", minutes)
        return self.registration_until

    def close_registration(self) -> None:
        if self._registration_timer:
            self._registration_timer.cancel()
            self._registration_timer = None
        if self._server:
            self._server.registration_open = False
        self.registration_until = None

    def _on_register(self, info: CameraInfo) -> None:
        camera = self.db.register_camera(info.name, info.model, self._registration_mark_existing)
        self.registered.append({"camera_id": camera.camera_id, "name": camera.name, "model": camera.model})

    # Transfers

    def stop_transfer(self) -> bool:
        """Cancel the running transfer. Copied files stay recorded; the partial file is resumed next time."""
        if self.job is None or self._task is None or self._task.done():
            return False
        self.job.message = "Stopping..."
        self._task.cancel()
        return True

    async def _on_session(self, session: AutoSaveSession) -> None:
        if self.job is not None:  # the library already allows only one session; be defensive
            raise Busy("A transfer is already running")
        camera = self.db.register_camera(session.camera.name, session.model)
        self.db.record_camera_seen(camera.camera_id)
        job = self.job = Job(id=uuid.uuid4().hex[:12], camera_id=camera.camera_id, camera_name=camera.name,
                             address=session.camera.address)
        self._task = asyncio.current_task()
        logger.info("transfer from %s (%s) at %s", camera.name, camera.camera_id, session.camera.address)
        try:
            await self._run(job, session, camera)
            job.end_step()
            job.phase = "done"
        except asyncio.CancelledError as e:
            job.end_step(e)
            job.phase = "stopped"
            job.message = f"Stopped by user after {job.files_done} file(s). The next transfer continues."
            self.db.record_camera_sync(camera.camera_id, "Stopped by user")
            await asyncio.shield(self._end_with_error(session))
            raise
        except SessionEnded as e:
            job.end_step(e)
            job.phase = "ended"
            job.error = (f"The camera ended the transfer after {job.files_done} of {job.files_new} file(s) "
                         "(cancelled on the camera, or the connection was lost).")
        except Exception as e:
            logger.exception("transfer from %s failed", camera.name)
            job.end_step(e)
            job.phase = "failed"
            job.error = str(e) or repr(e)
            await self._end_with_error(session)
        finally:
            job.current = None
            job.finished_at = time.time()
            if job.phase != "stopped":
                self.db.record_camera_sync(camera.camera_id, job.error)
            self.last_job, self.job, self._task = job, None, None

    @staticmethod
    async def _end_with_error(session: AutoSaveSession) -> None:
        """Show the error on the camera and end the session, as PC AutoSave does."""
        if session.ended:
            return
        try:
            await asyncio.wait_for(session.set_progress(0, 0, error=True), 10)
            await asyncio.wait_for(session.finish(), 10)
        except Exception as e:
            logger.warning("could not end the session cleanly: %r", e)

    async def _finish(self, job: Job, session: AutoSaveSession, done: int, total: int) -> None:
        job.phase = "finishing"
        job.step("finish", f"Showing {done}/{total} on the camera")
        await session.set_progress(done, total)
        await session.finish()

    async def _run(self, job: Job, session: AutoSaveSession, camera: Camera) -> None:
        cfg = self.config.config
        if not camera.enabled:
            job.step("skip", f"{camera.name} is disabled; nothing is copied")
            await self._finish(job, session, 0, 0)
            job.message = f"{camera.name} is disabled; nothing was copied."
            return

        job.phase = "preflight"
        job.step("nas", f"Checking {cfg.nas.target_dir}...")
        nas_status = await self.nas.preflight(cfg.nas)
        if nas_status.free_bytes is not None:
            job.step("nas", f"{cfg.nas.target_dir} is writable, {nas_status.free_bytes / 1024**3:.1f} GB free")

        job.phase = "listing"
        job.list_total = session.object_count
        job.step("list", f"Reading {session.object_count} file(s) from the camera...")

        def listed(done: int, total: int) -> None:
            job.list_done, job.list_total = done, total
            job.step("list", f"Reading file list: {done} of {total}")

        objects = await session.list_objects(listed)
        done_keys = self.db.transferred_keys(camera.camera_id)
        new = [o for o in objects if file_key(o) not in done_keys]
        new.sort(key=lambda o: (o.captured is None, o.captured or 0, o.filename))
        job.files_found, job.files_new = len(objects), len(new)
        job.step("list", f"{len(objects)} file(s) on the camera, {len(new)} new")

        if camera.mark_existing:
            job.step("mark", f"Marking {len(new)} file(s) as copied without copying them")
            self.db.add_transferred_many(camera.camera_id, [file_key(o) for o in new])
            self.db.update_camera(camera.camera_id, mark_existing=False)
            await self._finish(job, session, 0, 0)
            job.message = (f"Marked {len(new)} file(s) on {camera.name} as copied. "
                           "From now on only new photos are transferred.")
            return

        needed = sum(o.size for o in new)
        if nas_status.free_bytes is not None and needed > nas_status.free_bytes:
            raise NasError(f"Not enough space on the NAS: need {needed / 1024**3:.1f} GB, "
                           f"{nas_status.free_bytes / 1024**3:.1f} GB free")

        job.phase = "transferring"
        if new:
            job.step("transfer", f"{len(new)} new file(s)")
        for index, obj in enumerate(new, start=1):
            job.queue_index = index
            await session.set_progress(index - 1, len(new))
            dest = destination(cfg.nas.target_dir, cfg.storage.layout, camera, obj)
            job.step("transfer", f"File {index} of {len(new)}: {obj.filename}")
            await self._transfer(job, session, obj, dest)
            filename, captured, size = file_key(obj)
            self.db.add_transferred(camera.camera_id, filename, captured, size, str(dest))
            job.files_done += 1
            job.bytes_done += obj.size
        job.current = None
        await self._finish(job, session, len(new), len(new))
        job.message = f"Transferred {job.files_done} file(s) from {camera.name}" if new else \
            f"No new files on {camera.name}"

    async def _transfer(self, job: Job, session: AutoSaveSession, obj: ObjectInfo, dest: Path) -> None:
        """Copy into `<dest>.part`, resuming a part left by an interrupted transfer, then rename."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        offset = part.stat().st_size if part.exists() else 0
        if offset > obj.size:
            offset = 0
        progress = job.current = FileProgress(obj.filename, str(dest), offset, obj.size, resumed_from=offset)
        with part.open("r+b" if offset else "wb") as fp:
            fp.seek(offset)
            fp.truncate()
            try:
                async for chunk in session.download(obj, offset):
                    await asyncio.to_thread(fp.write, chunk)
                    progress.bytes_done += len(chunk)
            finally:
                await asyncio.to_thread(fp.flush)
                await asyncio.to_thread(os.fsync, fp.fileno())
        if progress.bytes_done != obj.size:
            raise IOError(f"{obj.filename}: got {progress.bytes_done} of {obj.size} bytes")
        os.replace(part, dest)
