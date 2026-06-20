#!/bin/bash
set -e

# FiveM FXAP Decompiler — One-click Deployment
# Usage: sudo bash deploy.sh

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err() { echo -e "${RED}[x]${NC} $1"; exit 1; }

# Check root
[[ $EUID -ne 0 ]] && err "Run as root: sudo bash deploy.sh"

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
FXSERVER_DIR="/opt/fxserver"
SERVER_DATA="/opt/fxserver/server-data"
PORT="${PORT:-8080}"

# ---- 1. Install dependencies ----
log "Installing dependencies..."
apt-get update -qq
apt-get install -y -qq gdb openjdk-21-jre-headless python3 python3-pip python3-venv unzip wget curl > /dev/null 2>&1

# ---- 2. Install FXServer ----
if [[ ! -f "$FXSERVER_DIR/run.sh" ]]; then
    log "Downloading FXServer..."
    mkdir -p "$FXSERVER_DIR"

    # Get latest recommended build
    LATEST=$(curl -s "https://changelogs-live.fivem.net/api/changelog/versions/linux/server" | head -1)
    BUILD=$(echo "$LATEST" | grep -oP '"id":\s*\K\d+' || echo "")

    if [[ -z "$BUILD" ]]; then
        # Fallback: use artifacts page
        BUILD_URL="https://runtime.fivem.net/artifacts/fivem/build_proot_linux/master/"
        warn "Using fallback FXServer download..."
        wget -q -O /tmp/fx.tar.xz "${BUILD_URL}$(curl -s "$BUILD_URL" | grep -oP 'href="\K[^"]+\.tar\.xz' | head -1)"
    else
        wget -q -O /tmp/fx.tar.xz "https://runtime.fivem.net/artifacts/fivem/build_proot_linux/master/${BUILD}-f*/fx.tar.xz" 2>/dev/null || \
        wget -q -O /tmp/fx.tar.xz "https://runtime.fivem.net/artifacts/fivem/build_proot_linux/master/"
    fi

    # Try direct download if the above didn't work
    if [[ ! -s /tmp/fx.tar.xz ]]; then
        log "Trying direct FXServer download..."
        wget -q -O /tmp/fx.tar.xz "https://runtime.fivem.net/artifacts/fivem/build_proot_linux/master/$(curl -sL 'https://runtime.fivem.net/artifacts/fivem/build_proot_linux/master/' 2>/dev/null | grep -oP 'href="\K\d+-f[0-9a-f]+/fx\.tar\.xz' | tail -1)" 2>/dev/null || true
    fi

    if [[ -s /tmp/fx.tar.xz ]]; then
        tar xf /tmp/fx.tar.xz -C "$FXSERVER_DIR"
        rm /tmp/fx.tar.xz
        log "FXServer installed to $FXSERVER_DIR"
    else
        err "FXServer download failed. Install manually: https://docs.fivem.net/docs/server-manual/setting-up-a-server/"
    fi
else
    log "FXServer already installed"
fi

# ---- 3. Set up server data ----
if [[ ! -d "$SERVER_DATA" ]]; then
    log "Downloading server data..."
    mkdir -p "$SERVER_DATA"
    cd /tmp
    wget -q -O server-data.zip "https://github.com/citizenfx/cfx-server-data/archive/refs/heads/master.zip" 2>/dev/null || \
    wget -q -O server-data.zip "https://github.com/citizenfx/cfx-server-data/archive/master.zip"
    if [[ -s server-data.zip ]]; then
        unzip -qo server-data.zip
        cp -r cfx-server-data-master/* "$SERVER_DATA/"
        rm -rf cfx-server-data-master server-data.zip
        log "Server data installed"
    else
        warn "Server data download failed — creating minimal setup"
        mkdir -p "$SERVER_DATA/resources"
    fi
fi

# ---- 4. Create minimal server.cfg if missing ----
CFG="$SERVER_DATA/server.cfg"
if [[ ! -f "$CFG" ]]; then
    cat > "$CFG" << 'SERVERCFG'
# FiveM FXAP Decompiler — Minimal Server Config
sv_hostname "FXAP Decompiler"
sv_maxclients 1
endpoint_add_tcp "0.0.0.0:30120"
endpoint_add_udp "0.0.0.0:30120"

# License key will be set dynamically per-request
set sv_licenseKey "changeme"

# Minimal resources
ensure mapmanager
ensure chat
ensure spawnmanager
ensure sessionmanager
ensure hardcap
SERVERCFG
    log "Created minimal server.cfg"
fi

# ---- 5. Create run.sh if missing ----
if [[ ! -f "$FXSERVER_DIR/run.sh" ]]; then
    cat > "$FXSERVER_DIR/run.sh" << 'RUNSH'
#!/bin/bash
cd /opt/fxserver
exec bash citizen/run.sh +exec /opt/fxserver/server-data/server.cfg "$@"
RUNSH
    chmod +x "$FXSERVER_DIR/run.sh"
fi

# ---- 6. Set up Python environment ----
log "Setting up Python environment..."
cd "$PROJECT_DIR"
python3 -m venv venv 2>/dev/null || python3 -m venv --without-pip venv
source venv/bin/activate
pip install -q --upgrade pip 2>/dev/null
pip install -q -r requirements.txt

# ---- 7. Copy unluac.jar if not present ----
if [[ ! -f "$PROJECT_DIR/unluac.jar" ]]; then
    # Try to build from source
    if [[ -d "$PROJECT_DIR/unluac" ]]; then
        log "Building unluac..."
        cd "$PROJECT_DIR/unluac/src"
        mkdir -p ../build
        javac -d ../build $(find unluac -name "*.java") 2>/dev/null
        cd ../build
        echo -e "Manifest-Version: 1.0\nMain-Class: unluac.Main" > /tmp/manifest.mf
        jar cfm "$PROJECT_DIR/unluac.jar" /tmp/manifest.mf unluac/
        log "unluac.jar built"
    else
        err "unluac.jar not found and no source to build from"
    fi
fi

# ---- 8. Create systemd service ----
log "Creating systemd service..."
cat > /etc/systemd/system/fivem-decompiler.service << EOF
[Unit]
Description=FiveM FXAP Decompiler Web Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
Environment=PATH=$PROJECT_DIR/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$PROJECT_DIR/venv/bin/uvicorn app:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable fivem-decompiler
systemctl restart fivem-decompiler

# ---- 9. Done ----
IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
log "========================================="
log " FiveM FXAP Decompiler deployed!"
log " Web UI: http://${IP}:${PORT}"
log "========================================="
log ""
log " Usage:"
log "   1. Open the URL in your browser"
log "   2. Upload an encrypted .pack.zip"
log "   3. Enter your FiveM license key"
log "   4. Wait for decompilation"
log "   5. Download the result"
log ""
log " Service: systemctl status fivem-decompiler"
log " Logs:    journalctl -u fivem-decompiler -f"
