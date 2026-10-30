import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_install_script_uses_uv_and_never_initializes_a_project():
    script = (ROOT / "install.sh").read_text()

    download_base = (
        'AERO_DOWNLOAD_BASE="${AERO_DOWNLOAD_BASE:-https://aero.skyviewor.com/download}"'
    )
    assert download_base in script
    assert "bootstrap/uv/$UV_VERSION/$TARGET.tar.gz" in script
    assert "bootstrap/python/$PYTHON_BUILD/$PLATFORM.tar.gz" in script
    assert '"$TEMP_DIR/bootstrap" --base' in script
    assert "aero setup" in script
    assert "uv tool install --" not in script
    assert "aero init" not in script.split("安装完成")[0]
    assert "Miniconda" not in script
    assert "conda base" not in script
    assert "curl -fk" not in script
    assert "github.com" not in script.lower()
    assert 'export AERO_NETWORK_REGION="${AERO_NETWORK_REGION:-mainland_china}"' in script
    assert "export UV_DEFAULT_INDEX=" not in script


def test_install_script_selects_each_supported_platform():
    script = (ROOT / "install.sh").read_text()
    for platform in (
        "macos-aarch64",
        "macos-x86_64",
        "linux-x86_64",
        "linux-aarch64",
    ):
        assert platform in script
    assert "--retry 3" in script
    assert "--connect-timeout 10" in script
    assert "--proto '=https'" in script
    assert '"$TEMP_DIR/$1.sha256"' in script
    assert 'sha256sum "$1"' in script
    assert 'shasum -a 256 "$1"' in script


def test_shell_hash_check_uses_downloaded_file_not_sidecar_filename(tmp_path):
    script = (ROOT / "install.sh").read_text()
    function = "check_hash() {" + script.split("check_hash() {", 1)[1].split("\nUV_DIR=", 1)[0]
    archive = tmp_path / "python.tar.gz"
    contents = b"test-private-interpreter-archive"
    archive.write_bytes(contents)
    (tmp_path / "python.tar.gz.sha256").write_text(
        hashlib.sha256(contents).hexdigest() + "  macos-aarch64.tar.gz\n"
    )
    command = "set -eu\nfail() { exit 1; }\n" + function + "\ncheck_hash python.tar.gz\n"
    environment = {**os.environ, "TEMP_DIR": str(tmp_path)}
    subprocess.run(["sh", "-c", command], check=True, env=environment)
    archive.write_bytes(b"corrupt")
    assert subprocess.run(["sh", "-c", command], env=environment).returncode != 0


def test_release_builder_produces_matching_archives_and_checksums():
    installer = (ROOT / "install.sh").read_text()
    builder = (ROOT / "scripts/release/build-download-packages.sh").read_text()

    prepare = (ROOT / "scripts/release/prepare.py").read_text()
    assert "scripts/release/prepare.py" in builder
    assert '"uv", "build"' in prepare
    assert "checksum(release / archive_name)" in prepare
    assert "manifest" in prepare
    assert "key.sign" in prepare
    assert "stable.json" in installer
