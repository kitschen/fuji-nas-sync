#!/usr/bin/env bash
# fuji-nas-sync installer for Raspberry Pi OS (Bookworm or later). Coexists with gopro-nas-sync.
#
#   curl -fsSL https://raw.githubusercontent.com/kitschen/fuji-nas-sync/main/install.sh | sudo bash
#   sudo ./install.sh                  # from a local checkout
#   sudo ./install.sh --reconfigure    # run the setup questions again
#   sudo ./install.sh --uninstall [--purge]
#
# Environment: FUJI_SYNC_REPO (git URL), FUJI_SYNC_REF (branch / tag), NONINTERACTIVE=1
set -euo pipefail

REPO_URL="${FUJI_SYNC_REPO:-https://github.com/kitschen/fuji-nas-sync.git}"
REPO_REF="${FUJI_SYNC_REF:-main}"
PREFIX=/opt/fuji-sync
CONF_DIR=/etc/fuji-sync
STATE_DIR=/var/lib/fuji-sync
SERVICE_USER=fuji-sync
UNIT=/etc/systemd/system/fuji-sync.service
SUDOERS=/etc/sudoers.d/fuji-sync
LAN_IF=eth0
AUTOSAVE_PORTS=(51541 51542)

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

ask() { # ask <prompt> <default> -> answer on stdout
    local answer=""
    if [[ "${NONINTERACTIVE:-0}" == 1 || ! -r /dev/tty ]]; then echo "$2"; return; fi
    read -r -p "$1 [$2]: " answer </dev/tty || true
    echo "${answer:-$2}"
}

ask_secret() {
    local answer=""
    if [[ "${NONINTERACTIVE:-0}" == 1 || ! -r /dev/tty ]]; then echo ""; return; fi
    read -r -s -p "$1: " answer </dev/tty || true
    echo >&2
    echo "$answer"
}

port_in_use() { # port_in_use <tcp|udp> <port>
    ss -H -l -n "--$1" "sport = :$2" 2>/dev/null | grep -q .
}

MODE=install PURGE=0 RECONFIGURE=0
for arg in "$@"; do
    case "$arg" in
        --uninstall) MODE=uninstall ;;
        --purge) PURGE=1 ;;
        --reconfigure) RECONFIGURE=1 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) die "Unknown option $arg" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "Please run as root (sudo)."

uninstall() {
    info "Stopping service"
    systemctl disable --now fuji-sync.service 2>/dev/null || true
    if [[ -x $PREFIX/bin/fuji-sync-mount ]]; then
        info "Removing NAS mount unit"
        "$PREFIX/bin/fuji-sync-mount" remove >/dev/null || true
    fi
    rm -f "$UNIT" "$SUDOERS" /usr/local/bin/fuji-sync
    systemctl daemon-reload
    rm -rf "$PREFIX"
    if [[ $PURGE == 1 ]]; then
        info "Removing configuration, state and service user"
        rm -rf "$CONF_DIR" "$STATE_DIR" /etc/fuji-sync-mount
        userdel "$SERVICE_USER" 2>/dev/null || true
    else
        info "Kept $CONF_DIR and $STATE_DIR (use --purge to remove them)"
    fi
    info "Uninstalled. Files on your NAS were not touched."
}

if [[ $MODE == uninstall ]]; then uninstall; exit 0; fi

# ---------------------------------------------------------------------------------------------
info "Checking system"
. /etc/os-release
case "${VERSION_CODENAME:-}" in
    bookworm|trixie) ;;
    *) warn "Tested on Raspberry Pi OS Bookworm / Trixie; detected '${PRETTY_NAME:-unknown}'." ;;
esac
command -v systemctl >/dev/null || die "systemd is required."

info "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
APT_LOCK=(-o DPkg::Lock::Timeout=600)
apt-get "${APT_LOCK[@]}" update -qq
apt-get "${APT_LOCK[@]}" install -y -qq cifs-utils nfs-common python3 curl git ca-certificates iproute2 >/dev/null

if ! ip -4 -o addr show "$LAN_IF" 2>/dev/null | grep -q inet; then
    warn "${LAN_IF} has no IPv4 address. The camera and the NAS must reach the Pi on your home network;"
    warn "if the Pi uses Wi-Fi instead, clear the network interface in the web UI (Settings)."
fi
if ! systemctl is-active --quiet fuji-sync.service; then
    for port in "${AUTOSAVE_PORTS[@]}"; do
        if port_in_use udp "$port" || port_in_use tcp "$port"; then
            warn "Port $port is already in use; another PC AutoSave receiver may be running on this Pi."
        fi
    done
fi

# ---------------------------------------------------------------------------------------------
info "Creating service user and directories"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -m 0755 -o root -g root "$PREFIX" "$PREFIX/bin"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONF_DIR" "$STATE_DIR"

info "Fetching fuji-nas-sync"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/pyproject.toml" && -d "$SCRIPT_DIR/packaging" ]]; then
    rm -rf "$PREFIX/src"
    mkdir -p "$PREFIX/src"
    tar -C "$SCRIPT_DIR" --exclude=.venv --exclude=.git -cf - . | tar -C "$PREFIX/src" --no-same-owner -xf -
    chown -R root:root "$PREFIX/src"
elif [[ -d "$PREFIX/src/.git" ]]; then
    git -C "$PREFIX/src" fetch --depth 1 origin "$REPO_REF"
    git -C "$PREFIX/src" reset --hard FETCH_HEAD
else
    rm -rf "$PREFIX/src"
    git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$PREFIX/src"
fi
SRC="$PREFIX/src"

info "Installing uv and Python environment"
if [[ ! -x $PREFIX/bin/uv ]]; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$PREFIX/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
export UV_PYTHON_INSTALL_DIR="$PREFIX/python" UV_CACHE_DIR="$PREFIX/.uv-cache"
"$PREFIX/bin/uv" venv --quiet --allow-existing --python ">=3.11,<3.14" "$PREFIX/venv"
"$PREFIX/bin/uv" pip install --quiet --python "$PREFIX/venv/bin/python" --reinstall-package fuji-nas-sync "$SRC"

info "Installing mount helper, sudoers entry and service"
install -m 0755 -o root -g root "$SRC/packaging/fuji-sync-mount" "$PREFIX/bin/fuji-sync-mount"
install -m 0440 -o root -g root "$SRC/packaging/sudoers-fuji-sync" "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null || { rm -f "$SUDOERS.tmp"; die "sudoers validation failed"; }
mv "$SUDOERS.tmp" "$SUDOERS"
install -m 0644 -o root -g root "$SRC/packaging/fuji-sync.service" "$UNIT"
ln -sf "$PREFIX/venv/bin/fuji-sync" /usr/local/bin/fuji-sync
systemctl daemon-reload

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    info "Opening the PC AutoSave ports in ufw"
    for port in "${AUTOSAVE_PORTS[@]}"; do ufw allow "$port" >/dev/null; done
fi

# ---------------------------------------------------------------------------------------------
CONFIG="$CONF_DIR/config.toml"
NAS_JSON=""
if [[ ! -f "$CONFIG" || $RECONFIGURE == 1 ]]; then
    info "Setup (press Enter to accept defaults; everything can be changed later in the web UI)"
    PORT=$(ask "Web UI / API port (gopro-nas-sync uses 8080)" 8081)
    if port_in_use tcp "$PORT" && ! systemctl is-active --quiet fuji-sync.service; then
        warn "Port $PORT is already in use."
    fi
    IMPORTER=$(ask "Destination name shown on the camera (no spaces)" NAS)
    TOKEN=""
    if [[ "$(ask "Protect the API with a token? (y/n)" n)" =~ ^[Yy] ]]; then
        TOKEN=$("$PREFIX/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')
    fi
    NAS_TYPE=$(ask "NAS type: smb, nfs, path (existing directory) or skip" skip)
    export NAS_TYPE
    case "$NAS_TYPE" in
        smb|nfs)
            export NAS_SERVER NAS_SHARE NAS_VERSION NAS_USER NAS_PASSWORD
            NAS_SERVER=$(ask "NAS server (hostname or IP)" "")
            if [[ $NAS_TYPE == smb ]]; then
                NAS_SHARE=$(ask "Share name (optionally share/sub/dir)" "")
                NAS_VERSION=$(ask "SMB version (empty = auto)" "")
                NAS_USER=$(ask "SMB username" "")
                NAS_PASSWORD=$(ask_secret "SMB password")
            else
                NAS_SHARE=$(ask "NFS export path" "")
                NAS_VERSION=$(ask "NFS version (empty = auto)" "")
                NAS_USER="" NAS_PASSWORD=""
            fi
            NAS_JSON=$("$PREFIX/venv/bin/python" -c '
import json, os
e = os.environ
print(json.dumps({"type": e["NAS_TYPE"], "server": e["NAS_SERVER"], "share": e["NAS_SHARE"],
                  "version": e["NAS_VERSION"], "username": e["NAS_USER"], "password": e["NAS_PASSWORD"] or None}))')
            ;;
        path)
            export NAS_PATH
            NAS_PATH=$(ask "Existing directory (already mounted)" /mnt/nas)
            ;;
    esac
    export PORT TOKEN CONFIG IMPORTER
    "$PREFIX/venv/bin/python" - <<'PY'
import os
from pathlib import Path
from fuji_sync.config import Config, ConfigStore, NasConfig, NasType
store = ConfigStore(Path(os.environ["CONFIG"]))
cfg = store.config if store.path.exists() else Config()
cfg.server.port = int(os.environ["PORT"])
cfg.server.api_token = os.environ.get("TOKEN", "")
cfg.camera.importer = os.environ["IMPORTER"]
if os.environ.get("NAS_TYPE") == "path":
    cfg.nas = NasConfig(type=NasType.PATH, path=os.environ["NAS_PATH"])
store.save(Config.model_validate(cfg.model_dump()))
PY
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG"
    chmod 0640 "$CONFIG"
fi

info "Starting service"
systemctl enable fuji-sync.service >/dev/null
systemctl restart fuji-sync.service

read -r PORT TOKEN < <("$PREFIX/venv/bin/python" -c "
from pathlib import Path; from fuji_sync.config import ConfigStore
c = ConfigStore(Path('$CONFIG')).config
print(c.server.port, c.server.api_token)")

for _ in $(seq 1 30); do
    curl -fs "http://127.0.0.1:$PORT/api/status" >/dev/null && break
    sleep 1
done
curl -fs "http://127.0.0.1:$PORT/api/status" >/dev/null || die "Service did not start. See: journalctl -u fuji-sync -e"
if curl -fs "http://127.0.0.1:$PORT/api/status" | grep -q '"listening": *false'; then
    warn "The service runs but cannot listen for cameras; see the web UI or: journalctl -u fuji-sync -e"
fi

if [[ -n "$NAS_JSON" ]]; then
    info "Configuring and mounting the NAS share"
    AUTH=()
    [[ -n "$TOKEN" ]] && AUTH=(-H "Authorization: Bearer $TOKEN")
    if ! curl -fsS -X PUT "${AUTH[@]}" -H "Content-Type: application/json" -d "$NAS_JSON" \
        "http://127.0.0.1:$PORT/api/nas" >/dev/null; then
        warn "NAS setup failed; open the web UI (NAS tab) to fix the settings."
    fi
fi

IP=$(ip -4 -o addr show "$LAN_IF" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
echo
info "fuji-nas-sync is running: http://${IP:-<pi-address>}:$PORT/"
[[ -n "$TOKEN" ]] && info "API token: $TOKEN  (also stored in $CONFIG)"
echo "    Register your camera in the web UI (Cameras tab), then start PC AUTO SAVE on the camera."
