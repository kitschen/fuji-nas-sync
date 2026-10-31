"""Command line entry point: `fuji-sync serve | status`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path

from fuji_sync import __version__
from fuji_sync.config import DEFAULT_CONFIG_PATH, DEFAULT_STATE_DIR, ConfigStore


def _demo_camera():
    """A simulated X-E2 on localhost with a few photos, to try the UI without a camera."""
    import os
    from datetime import datetime, timedelta

    from fujiautosave.fake import FakeCamera, FakeFile
    from fujiautosave.fuji import Format

    start = datetime.now().replace(microsecond=0) - timedelta(days=1)
    files = [FakeFile(f"DSCF{1000 + i:04d}.JPG", os.urandom(3_000_000 + 50_000 * i), start + timedelta(minutes=i))
             for i in range(6)]
    files.append(FakeFile("DSCF1006.RAF", os.urandom(8_000_000), start + timedelta(minutes=6), Format.RAF))
    files.append(FakeFile("DSCF1007.MOV", os.urandom(20_000_000), start + timedelta(minutes=7), Format.MOV))
    return FakeCamera(files, name="FUJIFILM-X-E2-DEMO", host="127.0.0.1")


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    from fuji_sync.db import Database
    from fuji_sync.nas import NasManager
    from fuji_sync.sync import SyncService
    from fuji_sync.web import create_app

    config = ConfigStore(Path(args.config))
    db = Database(Path(args.state_dir) / "state.db")
    nas = NasManager(config.config.network.lan_interface)
    sync = SyncService(config, db, nas)
    app = create_app(config, db, sync, nas, demo_camera=_demo_camera() if args.demo else None)
    uvicorn.run(app, host=args.host or config.config.server.host, port=args.port or config.config.server.port,
                log_level=args.log_level.lower())


def _local_request(args: argparse.Namespace, method: str, path: str) -> dict:
    config = ConfigStore(Path(args.config)).config
    url = f"http://127.0.0.1:{args.port or config.server.port}{path}"
    req = urllib.request.Request(url, method=method)
    if config.server.api_token:
        req.add_header("Authorization", f"Bearer {config.server.api_token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fuji-sync", description="Receive Fujifilm PC AutoSave transfers on a NAS")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="config file path")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the daemon (camera listener + web UI + API)")
    p.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--demo", action="store_true", help="add a simulated camera on localhost (for trying the UI)")

    p = sub.add_parser("status", help="show daemon status")
    p.add_argument("--port", type=int)

    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "serve":
        serve(args)
    elif args.command == "status":
        print(json.dumps(_local_request(args, "GET", "/api/status"), indent=2))
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
