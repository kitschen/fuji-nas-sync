"""NAS target management: mounting via the root helper, Ethernet route checks and pre-transfer checks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fuji_sync.config import NasConfig, NasType

logger = logging.getLogger(__name__)

HELPER = Path(os.environ.get("FUJI_SYNC_MOUNT_HELPER", "/opt/fuji-sync/bin/fuji-sync-mount"))
PORTS = {NasType.SMB: 445, NasType.NFS: 2049}


class NasError(RuntimeError):
    pass


@dataclass
class RouteInfo:
    address: str
    device: str | None
    ok: bool
    message: str


@dataclass
class NasStatus:
    type: str
    target_dir: str
    mounted: bool
    writable: bool = False
    free_bytes: int | None = None
    route: RouteInfo | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def resolve(host: str) -> str:
    try:
        return socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)[0][4][0]
    except socket.gaierror as e:
        raise NasError(f"Cannot resolve NAS host {host!r}: {e}") from e


def parse_route_get(output: str) -> str | None:
    """Return the outgoing device from `ip -j route get <addr>` output."""
    try:
        routes = json.loads(output)
    except json.JSONDecodeError:
        return None
    return routes[0].get("dev") if routes else None


def check_route(host: str, lan_interface: str) -> RouteInfo:
    address = resolve(host)
    if not lan_interface:
        return RouteInfo(address, None, True, f"NAS address {address} (interface not checked).")
    try:
        out = subprocess.run(["ip", "-j", "route", "get", address], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return RouteInfo(address, None, False, f"Could not query routing table: {e}")
    device = parse_route_get(out)
    if device == lan_interface:
        return RouteInfo(address, device, True, f"Traffic to {address} uses {device}.")
    return RouteInfo(address, device, False,
                     f"Traffic to {address} would use {device or 'no interface'} instead of {lan_interface}. "
                     f"Make sure {lan_interface} is connected, or clear the interface setting.")


def _probe_writable(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".fuji-sync-probe-{uuid.uuid4().hex}"
    probe.write_bytes(b"ok")
    probe.unlink()


class NasManager:
    def __init__(self, lan_interface: str, helper: Path = HELPER) -> None:
        self.lan_interface = lan_interface
        self.helper = helper

    async def _helper(self, command: str, payload: dict | None = None) -> dict:
        def run() -> dict:
            proc = subprocess.run(
                ["sudo", "-n", str(self.helper), command],
                input=json.dumps(payload) if payload is not None else None,
                capture_output=True, text=True, timeout=120,
            )
            try:
                result = json.loads(proc.stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                raise NasError(f"Mount helper failed: {proc.stderr.strip() or proc.stdout.strip()}")
            if "error" in result:
                raise NasError(result["error"])
            return result

        return await asyncio.to_thread(run)

    async def apply(self, nas: NasConfig, password: str | None) -> dict:
        """Write the mount unit for SMB / NFS and mount it. For `path` only validate the directory."""
        if nas.type is NasType.PATH:
            await self._helper_if_configured("remove")
            if not nas.path or not Path(nas.path).is_dir():
                raise NasError(f"Directory {nas.path!r} does not exist")
            return {"mounted": True}
        route = await asyncio.to_thread(check_route, nas.server, self.lan_interface)
        if not route.ok:
            raise NasError(route.message)
        payload = {
            "type": nas.type.value, "server": nas.server, "share": nas.share, "version": nas.version,
            "username": nas.username, "domain": nas.domain, "password": password,
        }
        return await self._helper("apply", payload)

    async def _helper_if_configured(self, command: str) -> None:
        if self.helper.exists():
            try:
                await self._helper(command)
            except NasError as e:
                logger.warning("Mount helper %s failed: %s", command, e)

    async def mount(self) -> dict:
        return await self._helper("mount")

    async def unmount(self) -> dict:
        return await self._helper("unmount")

    async def test_connection(self, nas: NasConfig) -> dict:
        """Check name resolution, Ethernet routing and that the NAS port answers, without mounting."""
        if nas.type is NasType.PATH:
            status = await self.status(nas)
            return {"ok": status.writable, "messages": status.errors or ["Directory is writable."]}
        if not nas.server:
            return {"ok": False, "messages": ["No server configured."]}
        try:
            route = await asyncio.to_thread(check_route, nas.server, self.lan_interface)
        except NasError as e:
            return {"ok": False, "messages": [str(e)]}
        messages = [route.message]
        if not route.ok:
            return {"ok": False, "messages": messages}
        port = PORTS[nas.type]
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(route.address, port), timeout=5)
            writer.close()
            messages.append(f"{nas.type.value.upper()} port {port} is reachable.")
            return {"ok": True, "messages": messages}
        except (OSError, asyncio.TimeoutError) as e:
            messages.append(f"Cannot reach port {port} on {route.address}: {e!r}")
            return {"ok": False, "messages": messages}

    async def status(self, nas: NasConfig) -> NasStatus:
        return await asyncio.to_thread(self._status, nas)

    def _status(self, nas: NasConfig) -> NasStatus:
        if nas.type is NasType.PATH and not nas.path:
            return NasStatus(type=nas.type.value, target_dir="", mounted=False,
                             errors=["No NAS configured yet. Set it up in the NAS tab."])
        root = nas.mount_root
        mounted = root.is_dir() if nas.type is NasType.PATH else os.path.ismount(root)
        status = NasStatus(type=nas.type.value, target_dir=str(nas.target_dir), mounted=mounted)
        if nas.type is not NasType.PATH:
            try:
                status.route = check_route(nas.server, self.lan_interface) if nas.server else None
            except NasError as e:
                status.errors.append(str(e))
            if status.route and not status.route.ok:
                status.errors.append(status.route.message)
        if not mounted:
            status.errors.append(f"{root} is not mounted." if nas.type is not NasType.PATH else f"{root} does not exist.")
            return status
        try:
            _probe_writable(nas.target_dir)
            status.writable = True
            status.free_bytes = shutil.disk_usage(nas.target_dir).free
        except OSError as e:
            status.errors.append(f"Cannot write to {nas.target_dir}: {e}")
        return status

    async def preflight(self, nas: NasConfig) -> NasStatus:
        """Called when a camera connects, before listing its files. Tries to (re)mount once if needed."""
        status = await self.status(nas)
        if not status.mounted and nas.type is not NasType.PATH:
            try:
                await self.mount()
            except NasError as e:
                raise NasError(f"NAS share not mounted and mounting failed: {e}") from e
            status = await self.status(nas)
        if status.errors or not status.writable:
            raise NasError("; ".join(status.errors) or "NAS target is not writable")
        return status
