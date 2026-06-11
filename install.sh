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
METOORA_REPO="${METOORA_REPO:-ssh://git.skyviewor.team:2224/products/meteora.git}"
MINICONDA_DIR="${MINICONDA_DIR:-$HOME/miniconda3}"
METOORA_DIR="${METOORA_DIR:-$HOME/meteora}"
ENV_NAME="meteora-agent"
PYTHON_MIN="3.12"

banner ""
banner "  Meteora — 气象科研 AI Agent IDE"
banner "  一键安装脚本"
banner ""

# ── Detect OS / arch ────────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Darwin)
        OS_LABEL="macOS"
        case "$ARCH" in
            arm64|aarch64) CONDA_ARCH="arm64" ;;
            x86_64)        CONDA_ARCH="x86_64" ;;
            *) err "不支持的 macOS 架构: $ARCH"; exit 1 ;;
        esac
        ;;
    Linux)
        OS_LABEL="Linux"
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
info "检测到: ${OS_LABEL} / ${ARCH}"

# ── Step 1: Ensure Python 3.12+ ────────────────────────────────────────
banner ""
banner "  Step 1/4: 检查 Python ${PYTHON_MIN}+"
banner ""

find_python312() {
    for candidate in python3.12 python3.13 python3; do
        if command -v "$candidate" &>/dev/null; then
            local ver
            ver=$("$candidate" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null || echo "(0,0)")
            local major minor
            major=$(echo "$ver" | cut -d, -f1 | tr -dc '0-9')
            minor=$(echo "$ver" | cut -d, -f2 | tr -dc '0-9')
            if [ "${major:-0}" -ge 3 ] && [ "${minor:-0}" -ge 12 ]; then
                echo "$candidate"
                return
            fi
        fi
    done
    echo ""
}

PYTHON_BIN="$(find_python312)"

if [ -n "$PYTHON_BIN" ]; then
    PY_VER="$($PYTHON_BIN --version 2>&1)"
    ok "已找到 Python: $PY_VER ($PYTHON_BIN)"
else
    warn "未找到 Python ${PYTHON_MIN}+，开始安装..."

    if [ "$OS" = "Darwin" ]; then
        if command -v brew &>/dev/null; then
            info "通过 Homebrew 安装 Python..."
            brew install python@3.12
        else
            info "正在安装 Homebrew..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            if [ -f /opt/homebrew/bin/brew ]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            elif [ -f /usr/local/bin/brew ]; then
                eval "$(/usr/local/bin/brew shellenv)"
            fi
            brew install python@3.12
        fi
        PYTHON_BIN="$(find_python312)"
        if [ -z "$PYTHON_BIN" ]; then
            PYTHON_BIN="/opt/homebrew/bin/python3.12"
        fi
    elif [ "$OS" = "Linux" ]; then
        if command -v apt-get &>/dev/null; then
            info "通过 apt 安装 Python..."
            sudo apt-get update -qq
            sudo apt-get install -y -qq python3.12 python3.12-venv python3-pip
            PYTHON_BIN="python3.12"
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y python3.12
            PYTHON_BIN="python3.12"
        else
            err "无法自动安装 Python，请手动安装 Python ${PYTHON_MIN}+"
            exit 1
        fi
    fi

    if [ -z "$(find_python312)" ]; then
        err "Python 安装失败，请手动安装 Python ${PYTHON_MIN}+"
        exit 1
    fi
    ok "Python 安装完成: $($PYTHON_BIN --version 2>&1)"
fi

# ── Step 2: pip install meteora ────────────────────────────────────────
banner ""
banner "  Step 2/4: 安装 Meteora"
banner ""

if [ -f "$METOORA_DIR/pyproject.toml" ]; then
    info "已有本地副本: $METOORA_DIR"
    read -r -p "  是否重新克隆？[y/N] " reclone
    if [ "$reclone" = "y" ] || [ "$reclone" = "Y" ]; then
        rm -rf "$METOORA_DIR"
    fi
fi

if [ ! -d "$METOORA_DIR" ]; then
    info "克隆仓库: $METOORA_REPO"
    git clone "$METOORA_REPO" "$METOORA_DIR" || {
        err "仓库克隆失败。请确保 SSH key 已配置，或设置 METOORA_REPO 环境变量为其他地址。"
        exit 1
    }
fi

info "pip install: $METOORA_DIR"
"$PYTHON_BIN" -m pip install --quiet -e "$METOORA_DIR" 2>&1 || {
    err "pip install 失败。请检查网络和 Python 环境。"
    exit 1
}
ok "Meteora 安装完成"

# ── Step 3: Ensure conda ────────────────────────────────────────────────
banner ""
banner "  Step 3/4: 检查 conda / Miniconda"
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

    MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-${OS_LABEL}-${CONDA_ARCH}.sh"
    info "下载: $MINICONDA_URL"
    INSTALLER="$(mktemp /tmp/miniconda.XXXXXX.sh)"
    curl -fsSL "$MINICONDA_URL" -o "$INSTALLER" || {
        err "Miniconda 下载失败"
        rm -f "$INSTALLER"
        exit 1
    }
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

# ── Step 4: meteora init (creates meteora-agent env) ───────────────────
banner ""
banner "  Step 4/4: 初始化 Meteora 运行时 (meteora init)"
banner ""

info "创建/更新 meteora-agent conda 环境..."
"$PYTHON_BIN" -m meteora.cli.main init || {
    err "meteora init 失败"
    exit 1
}
ok "meteora init 完成"

# ── Add conda bin to PATH hint ──────────────────────────────────────────
banner ""
banner "  安装完成！"
banner ""
echo -e "  启动对话:  ${BOLD}cd <工作目录> && meteora init && meteora chat${NC}"
echo ""
echo -e "  如提示 conda 命令未找到，请在 shell 配置中追加:"
echo -e "    ${BOLD}export PATH=\"${MINICONDA_DIR}/bin:\$PATH\"${NC}"
echo ""
