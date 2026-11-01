# fuji-nas-sync

Copy the photos and videos from a Fujifilm camera to your NAS with the camera's own *PC AutoSave* function, without
a Windows PC. A Raspberry Pi on your home network plays the part of Fujifilm's PC AutoSave software.

This repository contains:

- `fuji_sync`, the daemon: it waits for the camera, copies every photo it has not copied before to the NAS and keeps
  the list of copied files. Web UI on port 8081 (gopro-nas-sync, which can run on the same Pi, uses 8080).
- `fujiautosave`, a dependency-free Python library that speaks the PC AutoSave protocol, with a small command line
  tool to try it. The protocol is described in [docs/protocol.md](docs/protocol.md).

Tested with an X-E2 (firmware 4.11): the library has copied photos and a movie from the real camera. The daemon is
tested against a simulated camera.

## How it works

1. The camera joins your home Wi-Fi (2.4 GHz on older models) and broadcasts for its registered destination.
2. The Pi answers under its destination name (default `NAS`), the camera connects and the Pi reads the file list.
3. The camera offers every file on the card each time; the Pi copies the ones not in its list (same camera, file
   name, capture time and size) to the NAS, showing the progress on the camera.

The camera and the Pi must be in the same subnet, because the camera finds the Pi by broadcast.

Reading a file's details takes about a quarter of a second, so reading a full card (4000 photos) takes about 17
minutes. The camera adds new photos at the end of its list, also after photos were deleted, so the Pi reads from the
newest file backwards and stops at the first one it already copied (quick scan, usually under a second). It reads the
whole card on the first transfer, after *Copy again* in the History tab, every 30 days and on request (*Full scan* in
the Cameras tab). Quick scanning can be turned off in Settings.

## Install on a Raspberry Pi

```bash
curl -fsSL https://raw.githubusercontent.com/kitschen/fuji-nas-sync/main/install.sh | sudo bash
```

Or from a checkout:

```bash
git clone https://github.com/kitschen/fuji-nas-sync.git
cd fuji-nas-sync
sudo ./install.sh
```

The installer creates the `fuji-sync` service user, `/opt/fuji-sync`, `/etc/fuji-sync`, `/var/lib/fuji-sync` and the
`fuji-sync` systemd service, and asks for the web UI port, the destination name and the NAS. Then open
`http://<pi-address>:8081/`:

1. **NAS**: enter the SMB or NFS share and save; it is mounted at `/mnt/fujinas`.
2. **Cameras**: click *Register camera*, then on the camera open *PC AUTO SAVE SETTING*, connect to your Wi-Fi and
   select the destination name. If the card holds photos you already have, tick the box to only mark them as copied
   on the first transfer.
3. On the camera, start *PC AUTO SAVE* from the playback menu.

Run the installer again to update to the latest version; it keeps the settings, the NAS mount and the registered
cameras. `sudo ./install.sh --reconfigure` asks the questions again, `sudo ./install.sh --uninstall [--purge]` removes
it.

## Trying the daemon without a Pi or camera

```bash
uv sync
uv run fuji-sync --config ./dev/config.toml serve --state-dir ./dev --demo
```

`--demo` adds a simulated X-E2 on localhost and buttons in the UI to let it register and start a transfer. Set the
NAS to an existing local directory (type *Existing directory*) in the NAS tab.

## Trying the library with a real camera

The camera and the computer must be on the same network (no guest Wi-Fi, no client isolation). Close Fujifilm's
PC AutoSave first: it uses the same ports (UDP and TCP 51541 and 51542).

```bash
uv sync

# Register this computer as a destination: on the camera, open PC AUTO SAVE SETTING and register a destination
uv run fujiautosave listen --name RPI --register

# Then start PC AUTO SAVE on the camera; lists the files and copies the ones missing in ./photos
uv run fujiautosave listen --name RPI --out photos
```

On Windows, allow Python through the firewall when asked (private networks).

## Library overview

```python
from fujiautosave import AutoSaveServer, AutoSaveSession

async def on_session(session: AutoSaveSession) -> None:
    files = await session.list_objects()
    for done, info in enumerate(files):
        await session.set_progress(done, len(files))
        with open(info.filename, "wb") as f:
            async for chunk in session.download(info):
                f.write(chunk)
    await session.set_progress(len(files), len(files))
    await session.finish()

async with AutoSaveServer("RPI", on_session):
    ...  # runs until the block exits
```

The camera starts every transfer; the library listens for it. The camera does not remember which files it already
sent, so deciding what is new is up to the application.
