"""Stage signed releases out of process; activation requires a human restart."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from aero.core.file_lock import lock_nonblocking, nofollow_flag
from aero.distribution import (
    DistributionError,
    assert_release_environment,
    canonical_manifest,
    current_environment,
    install_lock,
    install_root,
    managed_install,
    private_dir,
    smoke,
    stage_release,
    verify_manifest,
    write_state,
)
from aero.release_environment import environment_name
from aero.version import get_version


def _read(name: str) -> dict:
    try:
        value = json.loads((install_root() / name).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def _job_lock():
    root = private_dir(install_root())
    fd = os.open(root / "prepare-update.lock", os.O_CREAT | os.O_RDWR | nofollow_flag(), 0o600)
    try:
        lock_nonblocking(fd)
        yield
    finally:
        os.close(fd)


def _pending() -> tuple[dict, Path] | None:
    record = _read("pending-update.json")
    if not record:
        return None
    manifest = verify_manifest(record.get("release", {}))
    assert_release_environment(manifest, environment_name())
    target = Path(record["environment"]).resolve()
    root = (install_root() / "versions").resolve()
    if target.parent != root or Path(record["environment"]).is_symlink():
        raise DistributionError("待更新环境不属于当前安装。")
    marker = json.loads((target / "aero-install.json").read_text())
    digest = hashlib.sha256(canonical_manifest(manifest)).hexdigest()
    if marker.get("manifest_hash") != digest or marker.get("managed") is not True:
        raise DistributionError("待更新环境与签名清单不符，未切换版本。")
    return record, target


def update_status() -> dict:
    """No network polling, URLs, tokens, or process control in this read path."""
    result = {"running_version": get_version(), "managed": managed_install(), "status": "idle"}
    if not result["managed"]:
        from aero.cli.update import external_update_command

        return {**result, "status": "external", "message": external_update_command()}
    pending = _pending()
    if pending and pending[1] != current_environment():
        manifest = pending[0]["release"]["manifest"]
        return {
            **result,
            "status": "ready",
            "version": manifest["version"],
            "revision": manifest.get("revision"),
            "message": (
                "更新已准备，当前版本未切换。请点击重启并应用、执行 /restart，"
                "或关闭后重新进入 chat。"
            ),
        }
    job = _read("prepare-update.json")
    if job.get("status") in {"queued", "preparing"}:
        from aero.update_processes import lock_is_live

        active = lock_is_live(install_root() / "prepare-update.lock")
        if active or time.time() - job.get("started_at", 0) < 30:
            return {
                **result,
                "status": "preparing",
                "version": job.get("version"),
                "message": "正在后台准备并验证更新，当前对话可以继续；不必持续查询。",
            }
        return {
            **result,
            "status": "interrupted",
            "message": "准备进程已结束，未切换版本；可重新明确请求准备。",
        }
    if job.get("status") == "error":
        return {**result, "status": "error", "message": job.get("message", "更新未准备完成。")}
    return result


def prepare_update(*, reinstall: bool = False) -> dict:
    """Call only after the user requests preparation; never applies a release."""
    if type(reinstall) is not bool:
        raise DistributionError("reinstall 必须为布尔值。")
    status = update_status()
    if status["status"] in {"external", "preparing", "ready"}:
        return status
    from aero.cli.update import check_update

    release = check_update(force=True, reinstall=reinstall)
    if release is None:
        return {**status, "status": "current", "message": "没有可准备的新发行版。"}
    try:
        with _job_lock():
            pending = _pending()
            job = _read("prepare-update.json")
            if pending or (
                job.get("status") in {"queued", "preparing"}
                and time.time() - job.get("started_at", 0) < 30
            ):
                return update_status()
            job_id = uuid.uuid4().hex
            write_state(
                install_root() / "prepare-update.json",
                {
                    "job_id": job_id,
                    "release": release,
                    "status": "queued",
                    "started_at": time.time(),
                    "version": release["manifest"]["version"],
                },
            )
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "aero.cli.pending_update", job_id],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                write_state(
                    install_root() / "prepare-update.json",
                    {
                        "status": "error",
                        "message": "无法启动更新准备进程，旧版本未改变。",
                    },
                )
                raise DistributionError("无法启动更新准备进程，旧版本未改变。") from exc
    except BlockingIOError:
        pass  # Another preparer won; return its state instead of another job.
    return update_status()


def run_preparer(job_id: str) -> None:
    try:
        with _job_lock():
            job = _read("prepare-update.json")
            if job.get("job_id") != job_id or not managed_install():
                return
            try:
                write_state(install_root() / "prepare-update.json", {**job, "status": "preparing"})
                with install_lock():
                    marker = json.loads((Path(sys.prefix) / "aero-install.json").read_text())
                    uv = Path(marker["uv"])
                    if not uv.is_file() or not os.access(uv, os.X_OK):
                        raise DistributionError("私有安装工具缺失，请重新运行官方安装脚本。")
                    target = stage_release(job["release"], uv, marker["base_url"])
                    write_state(
                        install_root() / "pending-update.json",
                        {
                            "release": job["release"],
                            "environment": str(target),
                            "prepared_at": time.time(),
                        },
                    )
                write_state(install_root() / "prepare-update.json", {**job, "status": "ready"})
            except Exception as exc:
                write_state(
                    install_root() / "prepare-update.json",
                    {
                        "status": "error",
                        "message": str(exc)
                        if isinstance(exc, DistributionError)
                        else "准备或校验失败，旧版本未改变；可稍后重试。",
                    },
                )
    except BlockingIOError:
        return


def apply_pending(*, exclude_pid: int | None = None) -> bool:
    """Only the local human restart/startup paths may call this, never tools."""
    if not managed_install():
        return False
    if not (install_root() / "pending-update.json").exists():
        return False
    from aero.cli.update import switch_environment

    with install_lock():
        pending = _pending()
        if not pending:
            return False
        record, target = pending
        if target != current_environment():
            from packaging.version import Version

            if Version(record["release"]["manifest"]["version"]) < Version(get_version()):
                raise DistributionError("待更新版本低于当前运行版本，拒绝自动降级。")
            smoke(target, record["release"]["manifest"]["version"])
            # Do not wait or kill busy peers. The existing handoff rolls back
            # and resumes old workers if any handshake/restart fails.
            switch_environment(target, exclude_pid=exclude_pid, wait=0)
        (install_root() / "pending-update.json").unlink(missing_ok=True)
        return True


if __name__ == "__main__":
    if len(sys.argv) == 2:
        from aero.release_environment import configure_runtime

        configure_runtime()
        run_preparer(sys.argv[1])
