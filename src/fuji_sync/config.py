"""Daemon configuration stored in a TOML file (default /etc/fuji-sync/config.toml)."""

from __future__ import annotations

import os
import threading
import tomllib
from enum import Enum
from pathlib import Path

import tomli_w
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path(os.environ.get("FUJI_SYNC_CONFIG", "/etc/fuji-sync/config.toml"))
DEFAULT_STATE_DIR = Path(os.environ.get("FUJI_SYNC_STATE_DIR", "/var/lib/fuji-sync"))

# Fixed mount point managed by the root helper (systemd unit name: mnt-fujinas.mount)
MANAGED_MOUNT_POINT = Path("/mnt/fujinas")


class NasType(str, Enum):
    SMB = "smb"
    NFS = "nfs"
    PATH = "path"


class NasConfig(BaseModel):
    type: NasType = NasType.PATH
    # smb / nfs
    server: str = ""
    share: str = ""  # SMB share name or NFS export path
    version: str = ""  # SMB "3.0" etc. / NFS "4.1" etc. Empty = client default
    username: str = ""  # SMB only; password lives in the root-owned credentials file
    domain: str = ""  # SMB only
    # type == path
    path: str = ""
    # Sub directory inside the share / path where photos are written
    subdir: str = "Fujifilm"

    @property
    def mount_root(self) -> Path:
        return Path(self.path) if self.type is NasType.PATH else MANAGED_MOUNT_POINT

    @property
    def target_dir(self) -> Path:
        return self.mount_root / self.subdir if self.subdir else self.mount_root


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8081  # gopro-sync uses 8080 on the same Pi
    api_token: str = ""  # empty = no auth


class CameraLinkConfig(BaseModel):
    # Destination name shown on the camera (PC AUTO SAVE SETTING). Changing it means registering again.
    importer: str = "NAS"
    registration_minutes: int = 5

    @field_validator("importer")
    @classmethod
    def _check_importer(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 26 or not value.isascii() or " " in value:
            raise ValueError("destination name: 1 to 26 ASCII characters, no spaces")
        return value


class NetworkConfig(BaseModel):
    # The NAS must be reached through this interface; empty = don't check
    lan_interface: str = "eth0"


class StorageConfig(BaseModel):
    # Placeholders: {camera} {date} {year} {month}
    layout: str = "{camera}/{date}"


class ScanConfig(BaseModel):
    # Read the newest files first and stop at already copied ones, instead of the whole card (4 files per second)
    quick: bool = True
    stop_after: int = Field(1, ge=1)  # already copied files in a row that end a quick scan
    full_every_days: int = Field(30, ge=0)  # read the whole card now and then anyway; 0 = only when needed


class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    camera: CameraLinkConfig = Field(default_factory=CameraLinkConfig)
    scan: ScanConfig = Field(default_factory=ScanConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    nas: NasConfig = Field(default_factory=NasConfig)


class ConfigStore:
    """Thread-safe load/save of the config file."""

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.config = self._load()

    def _load(self) -> Config:
        if not self.path.exists():
            return Config()
        with self.path.open("rb") as fp:
            return Config.model_validate(tomllib.load(fp))

    def save(self, config: Config) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(tomli_w.dumps(config.model_dump(mode="json")).encode())
            os.replace(tmp, self.path)
            self.config = config
