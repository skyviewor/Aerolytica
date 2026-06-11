#!/usr/bin/env bash
set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()  { echo -e "  ${CYAN}[*]${NC} $*"; }
ok()    { echo -e "  ${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "  ${YELLOW}[!]${NC} $*"; }
err()   { echo -e "  ${RED}[✗]${NC} $*"; }
banner(){ echo -e "${BOLD}${CYAN}$*${NC}"; }

# ── Config ──────────────────────────────────────────────────────────────
METOORA_REPO="${METOORA_REPO:-https://github.com/skyviewor/meteora.git}"
MINICONDA_DIR="${MINICONDA_DIR:-$HOME/miniconda3}"


banner ""
banner "  Meteora — 气象科研 AI Agent IDE"
banner "  一键安装脚本"
banner ""

# ── Detect OS / arch ────────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Darwin)
        CONDA_OS="MacOSX"
        case "$ARCH" in
            arm64|aarch64) CONDA_ARCH="arm64" ;;
            x86_64)        CONDA_ARCH="x86_64" ;;
            *) err "不支持的 macOS 架构: $ARCH"; exit 1 ;;
        esac
        ;;
    Linux)
        CONDA_OS="Linux"
        case "$ARCH" in
            aarch64) CONDA_ARCH="aarch64" ;;
            x86_64)  CONDA_ARCH="x86_64" ;;
            *) err "不支持的 Linux 架构: $ARCH"; exit 1 ;;
        esac
        ;;
    *)
        err "不支持的操作系统: $OS"
        exit 1
        ;;
esac
info "检测到: ${CONDA_OS} / ${ARCH}"

# ── Step 1: Ensure conda ────────────────────────────────────────────────
banner ""
banner "  Step 1/3: 检查 conda / Miniconda"
banner ""

CONDA_BIN=""
if command -v conda &>/dev/null; then
    CONDA_BIN="$(command -v conda)"
    ok "已找到 conda: $CONDA_BIN"
elif [ -f "$MINICONDA_DIR/bin/conda" ]; then
    CONDA_BIN="$MINICONDA_DIR/bin/conda"
    ok "已找到 Miniconda: $CONDA_BIN"
else
    warn "未找到 conda。开始安装 Miniconda..."

    MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-${CONDA_OS}-${CONDA_ARCH}.sh"
    MINICONDA_MIRROR="https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-latest-${CONDA_OS}-${CONDA_ARCH}.sh"
    info "下载: $MINICONDA_URL"
    INSTALLER="/tmp/miniconda-$$.sh"
    curl -fkL --progress-bar --connect-timeout 10 --max-time 120 "$MINICONDA_URL" -o "$INSTALLER" 2>&1 || {
        warn "主源下载失败，尝试清华镜像..."
        curl -fkL --progress-bar "$MINICONDA_MIRROR" -o "$INSTALLER" || {
            err "Miniconda 下载失败，请手动安装 conda 后重试。"
            rm -f "$INSTALLER"
            exit 1
        }
    }
    echo ""
    bash "$INSTALLER" -b -p "$MINICONDA_DIR" || {
        err "Miniconda 安装失败"
        rm -f "$INSTALLER"
        exit 1
    }
    rm -f "$INSTALLER"
    CONDA_BIN="$MINICONDA_DIR/bin/conda"
    if [ ! -f "$CONDA_BIN" ]; then
        err "Miniconda 安装完成但未找到 conda: $CONDA_BIN"
        exit 1
    fi
    ok "Miniconda 已安装: $MINICONDA_DIR"
fi

# ── Step 2: Install meteora ─────────────────────────────────────────────
banner ""
banner "  Step 2/3: 安装 Meteora"
banner ""

TMP_DIR="/tmp/meteora-$$"
rm -rf "$TMP_DIR"
info "克隆仓库: $METOORA_REPO"
git clone --progress "$METOORA_REPO" "$TMP_DIR" 2>&1 || {
    err "仓库克隆失败。请检查网络连接。"
    rm -rf "$TMP_DIR"
    exit 1
}

info "pip install..."
"$CONDA_BIN" run -n base python -m pip install --progress-bar on "$TMP_DIR" 2>&1 || {
    err "pip install 失败。请检查网络和 conda 环境。"
    rm -rf "$TMP_DIR"
    exit 1
}

rm -rf "$TMP_DIR"
ok "Meteora 安装完成"

# ── Step 3: meteora init ─────────────────────────────────────────────────
banner ""
banner "  Step 3/3: 初始化 Meteora 运行时 (meteora init)"
banner ""

info "创建/更新 meteora-agent conda 环境..."
"$CONDA_BIN" run -n base python -m meteora.cli.main init || {
    err "meteora init 失败"
    exit 1
}
ok "meteora init 完成"

# ── Done ─────────────────────────────────────────────────────────────────
banner ""
banner "  安装完成！"
banner ""
echo -e "  启动对话:  ${BOLD}cd <工作目录> && meteora init && meteora chat${NC}"
echo ""
echo -e "  请将 conda 加入 PATH (如尚未加入):"
CONDA_BIN_DIR="$(dirname "$CONDA_BIN")"
echo -e "    ${BOLD}export PATH=\"${CONDA_BIN_DIR}:\$PATH\"${NC}"
echo ""
