"""HTTP API and status web page."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from fujiautosave.fake import FakeCamera

from fuji_sync import __version__
from fuji_sync.config import CameraLinkConfig, ConfigStore, NasConfig
from fuji_sync.db import Database
from fuji_sync.nas import NasError, NasManager
from fuji_sync.sync import Busy, SyncService

STATIC_DIR = Path(__file__).parent / "static"


class NasUpdate(NasConfig):
    password: str | None = None  # None = keep stored password
    layout: str | None = None


class CameraUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    mark_existing: bool | None = None


class RegistrationRequest(BaseModel):
    mark_existing: bool = False


class SettingsUpdate(BaseModel):
    importer: str | None = None
    registration_minutes: int | None = None
    lan_interface: str | None = None


def create_app(config: ConfigStore, db: Database, sync: SyncService, nas: NasManager, *,
               manage_listener: bool = True, demo_camera: FakeCamera | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if manage_listener:
            await sync.start()
        if demo_camera:
            await demo_camera.start()
        try:
            yield
        finally:
            if demo_camera:
                await demo_camera.stop()
            if manage_listener:
                await sync.stop()

    app = FastAPI(title="fuji-nas-sync", version=__version__, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def require_token(request: Request) -> None:
        token = config.config.server.api_token
        if not token:
            return
        header = request.headers.get("authorization", "")
        supplied = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(401, "Missing or invalid API token")

    auth = [Depends(require_token)]

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    # Status and transfers

    @app.get("/api/status")
    async def status() -> dict:
        return {**sync.status(), "version": __version__, "auth_required": bool(config.config.server.api_token),
                "demo": demo_camera is not None}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        async def stream():
            last = None
            while not await request.is_disconnected():
                payload = json.dumps(sync.status())
                if payload != last:
                    yield f"data: {payload}\n\n"
                    last = payload
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/transfer/stop", dependencies=auth)
    async def stop_transfer() -> dict:
        if not sync.stop_transfer():
            raise HTTPException(409, "No transfer is running")
        return {"ok": True}

    # Cameras

    @app.get("/api/cameras")
    async def cameras() -> dict:
        return {"cameras": [asdict(c) for c in db.list_cameras()], "importer": config.config.camera.importer}

    @app.post("/api/cameras/register", dependencies=auth)
    async def open_registration(body: RegistrationRequest | None = None) -> dict:
        try:
            until = sync.open_registration(mark_existing=bool(body and body.mark_existing))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"until": until}

    @app.delete("/api/cameras/register", dependencies=auth)
    async def close_registration() -> dict:
        sync.close_registration()
        return {"ok": True}

    @app.patch("/api/cameras/{camera_id}", dependencies=auth)
    async def update_camera(camera_id: str, body: CameraUpdate) -> dict:
        if not db.update_camera(camera_id, name=body.name, enabled=body.enabled, mark_existing=body.mark_existing):
            raise HTTPException(404, "Unknown camera")
        return {"ok": True}

    @app.delete("/api/cameras/{camera_id}", dependencies=auth)
    async def delete_camera(camera_id: str) -> dict:
        if not db.delete_camera(camera_id):
            raise HTTPException(404, "Unknown camera")
        return {"ok": True}

    # Settings

    @app.get("/api/settings")
    async def get_settings() -> dict:
        cfg = config.config
        return {"importer": cfg.camera.importer, "registration_minutes": cfg.camera.registration_minutes,
                "lan_interface": cfg.network.lan_interface, "port": cfg.server.port}

    @app.put("/api/settings", dependencies=auth)
    async def put_settings(body: SettingsUpdate) -> dict:
        cfg = config.config.model_copy(deep=True)
        try:
            cfg.camera = CameraLinkConfig.model_validate({
                **cfg.camera.model_dump(),
                **body.model_dump(include={"importer", "registration_minutes"}, exclude_none=True),
            })
        except ValidationError as e:
            raise HTTPException(400, e.errors()[0]["msg"])
        if body.lan_interface is not None:
            cfg.network.lan_interface = body.lan_interface.strip()
            nas.lan_interface = cfg.network.lan_interface
        renamed = cfg.camera.importer != config.config.camera.importer
        if renamed and sync.busy:
            raise HTTPException(409, "Wait until the transfer has finished")
        config.save(cfg)
        if renamed:
            await sync.restart()
        return {"ok": True, "restarted": renamed, "listen_error": sync.listen_error}

    # NAS

    @app.get("/api/nas")
    async def get_nas() -> dict:
        cfg = config.config
        status = await nas.status(cfg.nas)
        return {"config": cfg.nas.model_dump(mode="json"), "layout": cfg.storage.layout, "status": status.as_dict()}

    @app.put("/api/nas", dependencies=auth)
    async def put_nas(body: NasUpdate) -> dict:
        new_nas = NasConfig.model_validate(body.model_dump(exclude={"password", "layout"}))
        try:
            result = await nas.apply(new_nas, body.password)
        except NasError as e:
            raise HTTPException(400, str(e))
        cfg = config.config.model_copy(deep=True)
        cfg.nas = new_nas
        if body.layout:
            cfg.storage.layout = body.layout
        config.save(cfg)
        return {"ok": True, "helper": result, "status": (await nas.status(new_nas)).as_dict()}

    @app.post("/api/nas/test", dependencies=auth)
    async def test_nas(body: NasUpdate | None = None) -> dict:
        target = NasConfig.model_validate(body.model_dump(exclude={"password", "layout"})) if body else config.config.nas
        return await nas.test_connection(target)

    @app.post("/api/nas/mount", dependencies=auth)
    async def mount_nas() -> dict:
        try:
            return await nas.mount()
        except NasError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/nas/unmount", dependencies=auth)
    async def unmount_nas() -> dict:
        if sync.busy:
            raise HTTPException(409, "Cannot unmount while a transfer is running")
        try:
            return await nas.unmount()
        except NasError as e:
            raise HTTPException(400, str(e))

    # Transferred files

    @app.get("/api/transferred")
    async def transferred(camera: str | None = None, search: str = "", limit: int = 200, offset: int = 0) -> dict:
        items, total = db.list_transferred(camera, search, min(limit, 1000), offset)
        return {"items": [asdict(i) for i in items], "total": total}

    @app.delete("/api/transferred", dependencies=auth)
    async def reset_transferred(camera: str | None = None) -> dict:
        return {"deleted": db.reset_transferred(camera)}

    @app.delete("/api/transferred/{file_id}", dependencies=auth)
    async def delete_transferred(file_id: int) -> dict:
        if not db.delete_transferred(file_id):
            raise HTTPException(404, "Unknown entry")
        return {"ok": True}

    # Demo camera (serve --demo)

    if demo_camera:
        demo_tasks: set[asyncio.Task] = set()

        def demo_action(coro) -> None:
            async def run() -> None:
                try:
                    await coro
                except Exception as e:  # shown in the server log; the UI sees the job (or nothing) happen
                    logging.getLogger(__name__).warning("demo camera: %r", e)

            task = asyncio.create_task(run())
            demo_tasks.add(task)
            task.add_done_callback(demo_tasks.discard)

        @app.post("/api/demo/register", dependencies=auth)
        async def demo_register() -> dict:
            demo_action(demo_camera.register())
            return {"ok": True}

        @app.post("/api/demo/autosave", dependencies=auth)
        async def demo_autosave() -> dict:
            demo_action(demo_camera.autosave(config.config.camera.importer, timeout=3600))
            return {"ok": True}

    return app
