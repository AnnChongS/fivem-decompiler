#!/bin/bash
# FiveM FXAP Server-Side Decryptor — One-Click Deploy
# Requires: x86_64 Linux, root, 4GB+ RAM
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

# --- Pre-checks ---
[[ $(uname -m) == x86_64 ]] || fail "需要 x86_64 架构 (当前: $(uname -m))"
[[ $EUID -eq 0 ]] || fail "请用 root 运行"
FREE_MB=$(free -m | awk '/Mem:/ {print $2}')
[[ $FREE_MB -ge 3500 ]] || warn "内存 ${FREE_MB}MB < 3500MB，FXServer 可能 OOM"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/fxdecrypt"
FXSERVER_DIR="/opt/fxserver"

echo "=========================================="
echo " FiveM FXAP Decryptor — 部署"
echo " 安装目录: $INSTALL_DIR"
echo "=========================================="

# --- 1. System packages ---
echo "[1/7] 安装系统依赖..."
apt-get update -qq
apt-get install -y -qq gdb python3 python3-venv python3-pip unzip procps > /dev/null 2>&1
ok "系统依赖"

# --- 2. Java 21 ---
echo "[2/7] 检查 Java..."
JAVA21="/opt/jdk-21+35-jre/bin/java"
if [[ ! -x "$JAVA21" ]]; then
    echo "    下载 Adoptium JRE 21..."
    cd /opt
    curl -sL 'https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jre/hotspot/normal/eclipse?project=jdk' -o temurin21.tar.gz
    tar xzf temurin21.tar.gz
    rm temurin21.tar.gz
    ok "Java 21 安装完成"
else
    ok "Java 21 已存在"
fi

# Find actual java binary (dir name may vary)
JAVA21=$(find /opt -maxdepth 2 -name java -path '*/bin/java' 2>/dev/null | head -1)
[[ -n "$JAVA21" ]] || fail "找不到 Java 21"
echo "    Java: $JAVA21"

# --- 3. FXServer ---
echo "[3/7] 检查 FXServer..."
if [[ ! -f "$FXSERVER_DIR/run.sh" ]]; then
    echo "    下载 FXServer Build 25770..."
    mkdir -p "$FXSERVER_DIR"
    cd "$FXSERVER_DIR"
    curl -sL "https://runtime.fivem.net/artifacts/fivem/build_proot_linux/25770-524354328e47e4e34837863c10e0bb7e4e3e4e3e/fx.tar.xz" -o fx.tar.xz
    tar xf fx.tar.xz
    rm fx.tar.xz
    ok "FXServer 安装完成"
else
    ok "FXServer 已存在"
fi

# --- 4. unluac.jar ---
echo "[4/7] 检查 unluac..."
if [[ ! -f /tmp/unluac.jar ]]; then
    cp "$SCRIPT_DIR/unluac.jar" /tmp/unluac.jar
    ok "unluac.jar 复制完成"
else
    ok "unluac.jar 已存在"
fi

# --- 5. Python venv ---
echo "[5/7] 配置 Python 环境..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/app.py" "$INSTALL_DIR/app.py"

if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    python3 -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install -q flask
    ok "Python venv 创建完成"
else
    ok "Python venv 已存在"
fi

# Create upload/results dirs
mkdir -p "$INSTALL_DIR/uploads" "$INSTALL_DIR/results"

# --- 6. ASLR off ---
echo "[6/7] 关闭 ASLR..."
echo 0 > /proc/sys/kernel/randomize_va_space
ok "ASLR 已关闭"

# --- 7. Systemd service ---
echo "[7/7] 配置 systemd 服务..."
cat > /etc/systemd/system/fxdecrypt.service << EOF
[Unit]
Description=FXAP Server-Side Lua Decryptor Web Service
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/app.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable fxdecrypt.service
systemctl restart fxdecrypt.service
ok "systemd 服务已启动"

# --- Done ---
IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo ""
echo "=========================================="
echo -e " ${GREEN}✅ 部署完成！${NC}"
echo ""
echo " 打开: http://${IP}:8080"
echo ""
echo " 用法: 上传加密 ZIP + CFX License Key"
echo "       → 自动解密 server/shared 脚本"
echo "       → 下载解密后的 ZIP"
echo ""
echo " 服务管理:"
echo "   systemctl status fxdecrypt"
echo "   systemctl restart fxdecrypt"
echo "   journalctl -u fxdecrypt -f"
echo "=========================================="
