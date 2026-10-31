"""HTTPS installer bootstrap, run by the downloaded private Python."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

# Run the installed controller, not an old wheel's installer, when replacing an
# existing client. It understands its live process inventory and rollback.
HANDOFF = r"""
import hashlib, json, os, sys, time
from pathlib import Path
from packaging.version import Version
from aero.distribution import (
    DistributionError, assert_release_environment, canonical_manifest,
    current_environment, install_lock, install_root, smoke, verify_manifest, write_state,
)
from aero.cli.update import switch_environment
from aero.update_processes import lock_is_live

try:
    with install_lock():
        target, signed, expected, mode = map(str, sys.argv[1:])
        target = Path(target)
        current = current_environment()
        root = (install_root() / 'versions').resolve()
        if current is None or current != Path(expected) or current.parent != root:
            raise DistributionError('当前安装已变化，请重新执行安装命令。')
        if target.is_symlink() or target.resolve().parent != root:
            raise DistributionError('指定版本不属于当前安装。')
        envelope = json.loads(Path(signed).read_text())
        manifest = verify_manifest(envelope)
        expected_env = os.environ.get('AERO_ENVIRONMENT', 'production')
        assert_release_environment(manifest, expected_env)
        old = json.loads((current / 'aero-install.json').read_text())
        if old.get('managed') is not True:
            raise DistributionError('当前环境不是官方托管安装，未覆盖。')
        if old.get('runtime', {'environment': 'production'}).get('environment') != expected_env:
            raise DistributionError('当前安装与指定版本的环境不同，未跨环境切换。')
        requested = os.environ.get('AERO_VERSION')
        revision = os.environ.get('AERO_INSTALL_REVISION')
        if requested and requested != manifest['version']:
            raise DistributionError('发行清单与指定版本不符。')
        if revision and (expected_env != 'testing' or revision != manifest.get('revision')):
            raise DistributionError('发行清单与指定测试修订不符。')
        if Version(manifest['version']) < Version(old['version']):
            if not requested or os.environ.get('AERO_ALLOW_DOWNGRADE') != '1':
                raise DistributionError('降级需要 --version 和 --allow-downgrade 显式确认。')
        elif Version(manifest['version']) == Version(old['version']) and target != current:
            if (expected_env != 'testing' or not manifest.get('revision')
                    or not (revision or os.environ.get('AERO_REINSTALL') == '1')):
                raise DistributionError(
                    '同版本不同修订仅限 testing，须指定 --revision 或 --reinstall。')
        elif not (requested or os.environ.get('AERO_RELEASE_MANIFEST')):
            raise DistributionError('已有官网安装，请运行 aero update 安全升级。')
        if lock_is_live(install_root() / 'prepare-update.lock'):
            raise DistributionError('后台正在准备更新，请等准备完成后再指定版本安装。')
        job_path = install_root() / 'prepare-update.json'
        job = json.loads(job_path.read_text()) if job_path.exists() else {}
        if (job.get('status') in {'queued', 'preparing'}
                and time.time() - job.get('started_at', 0) < 30):
            raise DistributionError('更新准备任务尚未结束，请先查询更新状态。')
        if mode != 'check':
            marker = json.loads((target / 'aero-install.json').read_text())
            digest = hashlib.sha256(canonical_manifest(manifest)).hexdigest()
            if marker.get('managed') is not True or marker.get('manifest_hash') != digest:
                raise DistributionError('目标环境与签名清单不符，未切换版本。')
            smoke(target, manifest['version'])
            switch_environment(target, wait=0)
            # An explicit pin must not be undone by a formerly prepared update
            # when the user next opens chat. Retain the staged environment itself.
            (install_root() / 'pending-update.json').unlink(missing_ok=True)
            if job_path.exists():
                write_state(job_path, {'status': 'replaced_by_explicit_install'})
            print('已切换到指定版本：' + manifest['version'])
except Exception as exc:
    print(str(exc) if isinstance(exc, DistributionError)
          else '版本切换未完成，旧版本已保留；请检查安装状态。', file=sys.stderr)
    sys.exit(1)
"""


def handoff(current: Path, target: Path, manifest: Path, *, check: bool) -> None:
    result = subprocess.run(
        [
            str(current / "bin/python"),
            "-c",
            HANDOFF,
            str(target),
            str(manifest),
            str(current),
            "check" if check else "apply",
        ],
        check=False,
    )
    if result.returncode:
        raise SystemExit(result.returncode)


def selected_interpreter(module, manifest: dict, base: str) -> Path:
    """Historical releases may pin a different Python patch than today's script."""
    item = manifest["platforms"][module.target_platform()]
    prefix = (
        module.install_root()
        / "python"
        / (f"{manifest['python']}-{module.target_platform()}-{item['sha256'][:16]}")
    )
    with module.install_lock():
        module.private_dir(prefix.parent)
        if prefix.is_symlink():
            raise module.DistributionError("历史发行版的私有解释器目录不能是符号链接。")
        if not prefix.exists():
            with tempfile.TemporaryDirectory(
                prefix=".historical-python-", dir=prefix.parent
            ) as path:
                archive = Path(path) / "python.tar.gz"
                staging = Path(path) / "tree"
                staging.mkdir()
                module.download(
                    module.artifact_url(base, item["path"]), archive, sha256=item["sha256"]
                )
                with tarfile.open(archive) as bundle:
                    bundle.extractall(staging, filter="data")
                os.rename(staging, prefix)
    return prefix / "python/bin/python3.12"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--interpreter-ready", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    replacement = None
    with tempfile.TemporaryDirectory(prefix="aero-bootstrap-") as directory:
        module_path = Path(directory) / "distribution.py"
        with zipfile.ZipFile(args.wheel) as wheel:
            module_path.write_bytes(wheel.read("aero/distribution.py"))
        spec = importlib.util.spec_from_file_location("aero_install_distribution", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            envelope = json.loads(args.manifest.read_text())
            manifest = module.validate_manifest(envelope["manifest"])
            expected_env = os.environ.get("AERO_ENVIRONMENT", "production")
            module.assert_release_environment(manifest, expected_env)
            if os.environ.get("AERO_VERSION") and manifest["version"] != os.environ["AERO_VERSION"]:
                raise module.DistributionError("发行清单与指定版本不符。")
            revision = os.environ.get("AERO_INSTALL_REVISION")
            if revision and (expected_env != "testing" or revision != manifest.get("revision")):
                raise module.DistributionError("发行清单与指定测试修订不符。")
            if module.interpreter_version() != manifest["python"]:
                if args.interpreter_ready:
                    raise module.DistributionError("历史发行版解释器版本校验失败。")
                interpreter = selected_interpreter(module, manifest, args.base)
                result = subprocess.run(
                    [
                        str(interpreter),
                        str(Path(__file__).resolve()),
                        *sys.argv[1:],
                        "--interpreter-ready",
                    ],
                    check=False,
                )
                raise SystemExit(result.returncode)
            name = manifest["version"] + (
                "-" + manifest["revision"] if manifest.get("revision") else ""
            )
            target = module.install_root() / "versions" / name
            current = module.current_environment()
            if current is not None:
                if current.parent != (module.install_root() / "versions").resolve():
                    raise module.DistributionError("当前版本不属于本安装，未执行其他环境中的程序。")
                marker = json.loads((current / "aero-install.json").read_text())
                if marker.get("managed") is not True:
                    raise module.DistributionError("当前环境不是官方托管安装，未覆盖。")
                if current != target.resolve():
                    # Authorization is checked before downloading/installing a wheel.
                    handoff(current, target, args.manifest, check=True)
                    replacement = current
            with module.install_lock():
                if module.current_environment() != current:
                    raise module.DistributionError("当前安装已变化，请重新执行安装命令。")
                environment = module.stage_release(envelope, args.uv, args.base, bootstrap=True)
                if replacement is None:
                    launcher = module.install_launcher(
                        Path(os.environ.get("AERO_BIN_DIR", "~/.local/bin")).expanduser()
                    )
                    module.activate(environment)
                    print(f"客户端已安装：{launcher}")
        except module.DistributionError as exc:
            print(f"安装失败：{exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    if replacement is not None:
        # Recheck the exact selected signed manifest after staging, not stable:
        # stable may already have advanced to another version or revision.
        handoff(replacement, environment, args.manifest, check=False)


if __name__ == "__main__":
    main()
