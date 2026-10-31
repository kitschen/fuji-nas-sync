"""Command line tool to try the library against a real camera."""

from __future__ import annotations

import argparse
import asyncio
import errno
import logging
import os
from pathlib import Path

from fujiautosave.discovery import CameraInfo
from fujiautosave.fuji import Format
from fujiautosave.ptp import SessionEnded
from fujiautosave.server import AutoSaveServer
from fujiautosave.session import AutoSaveSession


def _format_name(fmt: int) -> str:
    try:
        return Format(fmt).name
    except ValueError:
        return f"0x{fmt:04x}"


async def _copy_missing(session: AutoSaveSession, objects: list, out: Path) -> None:
    target = out / session.camera.name
    target.mkdir(parents=True, exist_ok=True)
    missing = [o for o in objects if not ((target / o.filename).exists()
                                          and (target / o.filename).stat().st_size == o.size)]
    print(f"{len(missing)} of {len(objects)} files to copy into {target}")
    for done, obj in enumerate(missing):
        await session.set_progress(done, len(missing))
        part = target / (obj.filename + ".part")
        offset = part.stat().st_size if part.exists() else 0
        with open(part, "ab") as f:
            async for chunk in session.download(obj, offset):
                f.write(chunk)
                print(f"\r  {obj.filename}: {f.tell() * 100 // obj.size:3d}%", end="", flush=True)
        os.replace(part, target / obj.filename)
        print()
    await session.set_progress(len(missing), len(missing))


def _make_handler(args: argparse.Namespace):
    async def on_session(session: AutoSaveSession) -> None:
        print(f"{session.camera.name} ({session.model}) at {session.camera.address}: {session.object_count} files")
        try:
            objects = await session.list_objects(
                lambda i, n: print(f"\r  reading file list {i}/{n}", end="", flush=True))
            print()
            for o in objects:
                print(f"  {o.handle:5d}  {o.filename:14s} {_format_name(o.format):5s} {o.size:>11,d}  {o.captured}")
            if args.out:
                await _copy_missing(session, objects, Path(args.out))
            else:
                await session.set_progress(0, 0)
            await session.finish()
            print("done")
        except SessionEnded:
            print("\ncamera ended the session (cancelled on the camera?)")
        except Exception:
            if not session.ended:
                await session.set_progress(0, 0, error=True)
                await session.finish()
            raise

    return on_session


def _on_register(camera: CameraInfo) -> None:
    print(f"registered {camera.name} ({camera.model}) at {camera.address}")


async def _listen(args: argparse.Namespace) -> None:
    async with AutoSaveServer(args.name, _make_handler(args), _on_register, registration_open=args.register):
        print(f"waiting for cameras as {args.name!r}" + (" (registration open)" if args.register else "")
              + "; Ctrl+C to stop")
        await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser(prog="fujiautosave", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    listen = sub.add_parser("listen", help="wait for cameras starting PC AutoSave")
    listen.add_argument("--name", required=True, help="destination name shown on the camera")
    listen.add_argument("--register", action="store_true", help="let cameras register this destination")
    listen.add_argument("--out", help="copy files missing in this directory (default: only list them)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(_listen(args))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        if getattr(e, "winerror", None) != 10048 and e.errno not in (errno.EADDRINUSE, errno.EACCES):
            raise
        raise SystemExit(f"cannot listen on the PC AutoSave ports: {e}\nIs Fujifilm's PC AutoSave running? "
                         "On Windows, stop it with: Stop-Service FFPCAutoSave (as administrator)") from None


if __name__ == "__main__":
    main()
