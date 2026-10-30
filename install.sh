#!/usr/bin/env sh
# Install only the CLI. Interactive onboarding is explicitly aero setup.
set -eu
umask 077
AERO_ENVIRONMENT="${AERO_ENVIRONMENT:-production}"
AERO_DOWNLOAD_BASE="${AERO_DOWNLOAD_BASE:-https://aero.skyviewor.com/download}"
case "$AERO_ENVIRONMENT" in
    production) DEFAULT_AERO_HOME="$HOME/.aero"; AERO_COMMAND=aero ;;
    testing) DEFAULT_AERO_HOME="$HOME/.aero-testing"; AERO_COMMAND=aero-testing ;;
    *) printf '不支持的发行环境\n' >&2; exit 1 ;;
esac
AERO_HOME="${AERO_HOME:-$DEFAULT_AERO_HOME}"
AERO_INSTALL_ROOT="${AERO_INSTALL_ROOT:-$AERO_HOME/client}"
AERO_BIN_DIR="${AERO_BIN_DIR:-$HOME/.local/bin}"
export AERO_INSTALL_ROOT AERO_BIN_DIR AERO_HOME AERO_ENVIRONMENT
UV_VERSION="0.12.23"
PYTHON_VERSION="3.12.12"
PYTHON_BUILD="20251014"
fail() { printf '\n安装失败：%s\n' "$*" >&2; exit 1; }
case "$AERO_INSTALL_ROOT" in /*) ;; *) fail "AERO_INSTALL_ROOT 必须是绝对路径" ;; esac
[ ! -L "$AERO_INSTALL_ROOT" ] || fail "安装目录不能是符号链接"
mkdir -p "$AERO_INSTALL_ROOT"
chmod 700 "$AERO_INSTALL_ROOT"
case "$AERO_DOWNLOAD_BASE" in https://*) ;; *) fail "下载源必须使用 HTTPS" ;; esac
command -v curl >/dev/null 2>&1 || fail "需要 curl；无需预装 Python 或 Conda。"
case "$(uname -s):$(uname -m)" in
    Darwin:arm64|Darwin:aarch64) TARGET=aarch64-apple-darwin; PLATFORM=macos-aarch64 ;;
    Darwin:x86_64) TARGET=x86_64-apple-darwin; PLATFORM=macos-x86_64 ;;
    Linux:arm64|Linux:aarch64) TARGET=aarch64-unknown-linux-gnu; PLATFORM=linux-aarch64 ;;
    Linux:x86_64) TARGET=x86_64-unknown-linux-gnu; PLATFORM=linux-x86_64 ;;
    *) fail "目前仅支持 macOS/Linux 的 x86_64 和 ARM64。" ;;
esac
if [ "$(uname -s)" = Linux ]; then
    getconf GNU_LIBC_VERSION >/dev/null 2>&1 || fail "目前不支持 musl/Alpine。"
fi
case "${AERO_NETWORK_REGION:-${TZ:-$(readlink /etc/localtime 2>/dev/null || true)}}" in
    cn|china|mainland|mainland_china|*Asia/Shanghai|*Asia/Chongqing|*Asia/Harbin)
        # Pass the region, not an implicit index override: the verified installer
        # can then fail over between mirrors while respecting explicit UV_* settings.
        export AERO_NETWORK_REGION="${AERO_NETWORK_REGION:-mainland_china}" ;;
esac
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/aero-install.XXXXXX")"
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM
fetch() {
    curl -fLsS --retry 3 --connect-timeout 10 --max-time 600 \
        --proto '=https' --proto-redir '=https' "$1" -o "$2"
}
check_hash() {
    EXPECTED_HASH="$(awk 'NR == 1 {print $1}' "$TEMP_DIR/$1.sha256")"
    case "$EXPECTED_HASH" in ''|*[!0-9a-f]*) fail "安装校验值格式错误" ;; esac
    [ "${#EXPECTED_HASH}" -eq 64 ] || fail "安装校验值长度错误"
    if command -v sha256sum >/dev/null 2>&1; then
        ACTUAL_HASH="$(cd "$TEMP_DIR" && sha256sum "$1" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        ACTUAL_HASH="$(cd "$TEMP_DIR" && shasum -a 256 "$1" | awk '{print $1}')"
    else fail "缺少 sha256sum/shasum，无法验证安装文件。"; fi
    [ "$ACTUAL_HASH" = "$EXPECTED_HASH" ] || fail "安装文件校验失败：$1"
}
UV_DIR="$AERO_INSTALL_ROOT/bootstrap/uv-$UV_VERSION-$PLATFORM"
if [ ! -x "$UV_DIR/uv" ]; then
    printf '准备私有安装工具…\n'
    fetch "$AERO_DOWNLOAD_BASE/bootstrap/uv/$UV_VERSION/$TARGET.tar.gz" "$TEMP_DIR/uv.tar.gz"
    fetch "$AERO_DOWNLOAD_BASE/bootstrap/uv/$UV_VERSION/$TARGET.tar.gz.sha256" "$TEMP_DIR/uv.tar.gz.sha256"
    check_hash uv.tar.gz
    [ ! -e "$UV_DIR" ] || fail "私有安装工具目录不完整，请检查 $UV_DIR"
    mkdir -p "$AERO_INSTALL_ROOT/bootstrap"
    tar -xzf "$TEMP_DIR/uv.tar.gz" -C "$TEMP_DIR"
    mv "$TEMP_DIR/uv-$TARGET" "$UV_DIR"
    chmod 700 "$UV_DIR/uv"
fi
PYTHON_DIR="$AERO_INSTALL_ROOT/python/$PYTHON_VERSION-$PLATFORM"
PYTHON="$PYTHON_DIR/python/bin/python3.12"
if [ ! -x "$PYTHON" ]; then
    printf '准备私有 Python（不会修改系统 Python）…\n'
    fetch "$AERO_DOWNLOAD_BASE/bootstrap/python/$PYTHON_BUILD/$PLATFORM.tar.gz" "$TEMP_DIR/python.tar.gz"
    fetch "$AERO_DOWNLOAD_BASE/bootstrap/python/$PYTHON_BUILD/$PLATFORM.tar.gz.sha256" "$TEMP_DIR/python.tar.gz.sha256"
    check_hash python.tar.gz
    [ ! -e "$PYTHON_DIR" ] || fail "私有 Python 目录不完整，请检查 $PYTHON_DIR"
    mkdir -p "$AERO_INSTALL_ROOT/python" "$TEMP_DIR/python-stage"
    tar -xzf "$TEMP_DIR/python.tar.gz" -C "$TEMP_DIR/python-stage"
    mv "$TEMP_DIR/python-stage" "$PYTHON_DIR"
fi
unset PYTHONHOME PYTHONPATH
fetch "$AERO_DOWNLOAD_BASE/${AERO_RELEASE_MANIFEST:-stable.json}" "$TEMP_DIR/stable.json"
"$PYTHON" - "$TEMP_DIR/stable.json" "$TEMP_DIR" <<'PY'
import json, re, sys
from pathlib import Path
root = Path(sys.argv[2])
manifest = json.loads(Path(sys.argv[1]).read_text())["manifest"]
if manifest.get("distribution") != "aerolytica" or manifest.get("schema") != 1:
    raise SystemExit("发行清单格式错误")
for name in ("wheel", "bootstrap"):
    item = manifest[name]
    path = item["path"]
    if not re.fullmatch(r"[A-Za-z0-9_./+\-]+", path) or path.startswith("/") or ".." in path.split("/"):
        raise SystemExit("发行路径不安全")
    if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
        raise SystemExit("发行校验值错误")
    (root / (name + ".path")).write_text(path)
    (root / (name + ".sha256")).write_text(item["sha256"] + "  " + name + "\n")
PY
for ITEM in wheel bootstrap; do
    ITEM_PATH="$(cat "$TEMP_DIR/$ITEM.path")"
    fetch "$AERO_DOWNLOAD_BASE/$ITEM_PATH" "$TEMP_DIR/$ITEM"
    check_hash "$ITEM"
done
printf '安装并验证客户端发行版…\n'
"$PYTHON" "$TEMP_DIR/bootstrap" --base "$AERO_DOWNLOAD_BASE" \
    --uv "$UV_DIR/uv" --manifest "$TEMP_DIR/stable.json" --wheel "$TEMP_DIR/wheel"
PROFILE_LINE="$("$PYTHON" -c 'import os,shlex; print("export PATH=" + shlex.quote(os.environ["AERO_BIN_DIR"]) + ":\"$PATH\" # Aerolytica")')"
case "${SHELL:-}" in */zsh) PROFILE="$HOME/.zshrc" ;; *) PROFILE="$HOME/.bashrc" ;; esac
if [ -L "$PROFILE" ]; then
    printf '未修改符号链接形式的 shell 配置：%s\n' "$PROFILE"
elif ! grep -F "$PROFILE_LINE" "$PROFILE" >/dev/null 2>&1; then
    printf '\n%s\n' "$PROFILE_LINE" >> "$PROFILE"
fi
printf '\n安装完成。当前终端先执行：\n  %s\n' "$PROFILE_LINE"
printf '\n然后执行：\n  %s setup\n\n进入研究目录后：\n  %s init\n  %s chat\n' \
    "$AERO_COMMAND" "$AERO_COMMAND" "$AERO_COMMAND"
