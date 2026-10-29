"""Verified, consent-driven updates with idle-only local process handoff."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import sys
import time
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from packaging.version import InvalidVersion, Version

from aero.distribution import (
    CHECK_INTERVAL,
    DistributionError,
    activate,
    assert_release_environment,
    current_environment,
    default_download_base,
    fetch_release,
    install_lock,
    install_root,
    managed_install,
    smoke,
    stage_release,
    verify_manifest,
    write_state,
)
from aero.release_environment import environment_name
from aero.update_processes import live_records, lock_is_live
from aero.version import get_version


def check_update(*, force: bool = False) -> dict | None:
    """Cache failures too: a bad network must not trigger a check on every chat."""
    path = install_root() / "update-check.json"
    try:
        cache = json.loads(path.read_text())
    except (OSError, ValueError):
        cache = {}
    base = os.environ.get("AERO_DOWNLOAD_BASE")
    if not base and managed_install():
        marker = json.loads((Path(sys.prefix) / "aero-install.json").read_text())
        base = marker.get("base_url")
    base = base or default_download_base()
    fresh = 0 <= time.time() - cache.get("checked_at", 0) < CHECK_INTERVAL
    if not force and fresh and cache.get("base") == base:
        envelope = cache.get("release")
        if not envelope:
            return None
        verify_manifest(envelope)
        assert_release_environment(envelope["manifest"], environment_name())
    else:
        envelope = None
        try:
            envelope = fetch_release(base)
            assert_release_environment(envelope["manifest"], environment_name())
        except DistributionError:
            envelope = None
            raise
        finally:
            write_state(
                path,
                {
                    "checked_at": time.time(),
                    "base": base,
                    "release": envelope,
                    "skipped": cache.get("skipped", ""),
                },
            )
    manifest = verify_manifest(envelope)
    try:
        newer = Version(manifest["version"]) > Version(get_version())
    except InvalidVersion as exc:
        raise DistributionError("当前版本无法识别，请重新安装正式发行版。") from exc
    if not newer or (not force and cache.get("skipped") == manifest["version"]):
        return None
    return envelope


def skip_version(version: str) -> None:
    path = install_root() / "update-check.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    write_state(path, {**state, "skipped": version})


def external_update_command() -> str:
    try:
        package = distribution("aerolytica")
        direct = json.loads(package.read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            return "当前为源码开发安装，请更新仓库并运行 uv sync；不会自动修改开发环境。"
    except (PackageNotFoundError, ValueError):
        pass
    if "pipx" in Path(sys.prefix).parts:
        return "pipx upgrade aerolytica"
    if "uv" in Path(sys.prefix).parts and "tools" in Path(sys.prefix).parts:
        return "uv tool upgrade aerolytica"
    return shlex.join([sys.executable, "-m", "pip", "install", "--upgrade", "aerolytica"])


def control(record: dict, action: str) -> dict:
    details = record["control"]
    if record["kind"] == "agent":
        from aero.cli.agent_background import control_request

        response = json.loads(control_request(details["agent_id"], action))
    elif record["kind"] == "daemon":
        import httpx

        from aero.core.daemon import DaemonManager

        manager = DaemonManager(details["project_dir"], Path(details["state_dir"]))
        state = manager._read()
        if not state:
            raise DistributionError("本地服务状态无法验证。")
        method = "GET" if action == "status" else "POST"
        result = httpx.request(
            method,
            f"http://127.0.0.1:{state['port']}/api/v1/daemon/{action}",
            headers={"Authorization": f"Bearer {state['token']}"},
            timeout=3,
            trust_env=False,
        )
        result.raise_for_status()
        response = result.json()
    else:
        raise DistributionError("此进程不支持安全升级，请先关闭。")
    if action != "update-stop" and response.get("protocol") != 1:
        raise DistributionError("旧版进程不支持升级握手，请先停止后再更新。")
    return response


def _restart(record: dict, environment: Path) -> None:
    interpreter = str(environment / "bin/python")
    details = record["control"]
    if record["kind"] == "agent":
        from aero.cli.agent_background import launch_background_agent

        launch_background_agent(details["agent_id"], Path(record["cwd"]), interpreter=interpreter)
    else:
        from aero.core.daemon import DaemonManager

        manager = DaemonManager(details["project_dir"], Path(details["state_dir"]))
        manager.start(port=details.get("port", 0), interpreter=interpreter)
        if record.get("hosted_agents"):
            import httpx

            info = manager.connection_info()
            for agent in record["hosted_agents"]:
                result = httpx.post(
                    info["base_url"] + f"/api/v1/cloud/agents/{agent['agent_id']}/start",
                    headers={"Authorization": f"Bearer {info['token']}"},
                    json={
                        "project_id": agent["project_id"],
                        "session_id": agent["session_id"],
                        "update_restore": True,
                    },
                    timeout=20,
                    trust_env=False,
                )
                result.raise_for_status()
                if not result.json().get("update_paused"):
                    raise DistributionError("恢复后的 Agent 未保持暂停状态。")


def _stop_idle(record: dict) -> None:
    result = control(record, "update-stop")
    if not result.get("stopping"):
        raise DistributionError("任务尚未空闲，未停止 Agent。")
    path = install_root() / "processes" / f"{record['pid']}.lock"
    deadline = time.monotonic() + 15
    while lock_is_live(path):
        if time.monotonic() > deadline:
            raise DistributionError("进程尚未正常退出；未强制杀进程。")
        time.sleep(0.1)


def switch_environment(
    environment: Path, *, exclude_pid: int | None = None, wait: float = 60
) -> None:
    old = current_environment()
    if old is None:
        raise DistributionError("没有可恢复的当前版本，请使用官方安装脚本。")
    barrier = install_root() / "updating.json"
    if barrier.exists():
        raise DistributionError("上次更新未完成，请运行 aero update --rollback 恢复。")
    token = secrets.token_urlsafe(32)
    journal = {"token": token, "old": str(old), "new": str(environment), "processes": []}
    write_state(barrier, journal)
    paused, stopped, restarted = [], [], []
    activated = False
    restored = False
    resumed = False
    previous_token = os.environ.get("AERO_UPDATE_RESTART_TOKEN")
    try:
        records = live_records(exclude_pid=exclude_pid)
        journal["processes"] = records
        write_state(barrier, journal)
        for record in records:
            if (
                Path(record["environment"]).resolve().parent
                != (install_root() / "versions").resolve()
            ):
                raise DistributionError("检测到其他安装的后台进程，请先手动停止。")
            status = control(record, "drain")
            if record["kind"] == "daemon":
                record["hosted_agents"] = status.get("hosted_agents", [])
                record["control"]["port"] = status["port"]
            paused.append(record)
            write_state(barrier, journal)
        deadline = time.monotonic() + wait
        while any(control(record, "status").get("update_busy", True) for record in paused):
            if time.monotonic() >= deadline:
                raise DistributionError("任务仍在运行或等待审批；已恢复接单，空闲后再更新。")
            time.sleep(0.5)
        for record in paused:
            _stop_idle(record)
            stopped.append(record)
        activate(environment)
        activated = True
        os.environ["AERO_UPDATE_RESTART_TOKEN"] = token
        for record in stopped:
            restarted.append(record)
            _restart(record, environment)
    except Exception as exc:
        # New workers start paused. They cannot claim work before all restarts
        # succeed, so rollback never has to cancel a newly accepted cloud task.
        for record in restarted:
            replacement = next(
                (
                    r
                    for r in live_records(exclude_pid=exclude_pid)
                    if r["control"] == record["control"]
                ),
                None,
            )
            if replacement:
                control(replacement, "drain")
                _stop_idle(replacement)
        if activated:
            activate(old)
        os.environ["AERO_UPDATE_RESTART_TOKEN"] = token
        for record in stopped:
            _restart(record, old)
        for record in paused:
            control(record, "resume")
        restored = True
        reason = str(exc) if isinstance(exc, DistributionError) else "启动或控制握手失败"
        raise DistributionError("更新未完成，已保留/恢复旧版本；" + reason) from exc
    finally:
        if previous_token is None:
            os.environ.pop("AERO_UPDATE_RESTART_TOKEN", None)
        else:
            os.environ["AERO_UPDATE_RESTART_TOKEN"] = previous_token
        # If recovery itself failed, leave its private journal for explicit
        # rollback rather than pretending all processes were restored.
        if restored:
            barrier.unlink(missing_ok=True)
    try:
        for record in stopped:
            control(record, "resume")
        resumed = True
    finally:
        if resumed:
            barrier.unlink(missing_ok=True)


def perform_update(envelope: dict, *, exclude_pid: int | None = None, wait: float = 60) -> None:
    if not managed_install():
        raise DistributionError("此安装由外部工具管理，请执行：\n" + external_update_command())
    with install_lock():
        marker = json.loads((Path(sys.prefix) / "aero-install.json").read_text())
        uv = Path(marker["uv"])
        if not uv.is_file() or not os.access(uv, os.X_OK):
            raise DistributionError("私有安装工具缺失，请重新运行官方安装脚本。")
        environment = stage_release(envelope, uv, marker["base_url"])
        switch_environment(environment, exclude_pid=exclude_pid, wait=wait)


def _smoke_owned_environment(environment: Path) -> None:
    if environment.resolve().parent != (install_root() / "versions").resolve():
        raise DistributionError("恢复目标不属于本安装，未执行环境中的程序。")
    marker = json.loads((environment / "aero-install.json").read_text())
    if marker.get("managed") is not True:
        raise DistributionError("恢复目标缺少托管安装记录。")
    smoke(environment, marker["version"])


def rollback(*, wait: float = 60) -> None:
    if not managed_install():
        raise DistributionError("仅官方托管安装支持自动回滚。")
    with install_lock():
        barrier = install_root() / "updating.json"
        if barrier.exists():
            journal = json.loads(barrier.read_text())
            old = Path(journal["old"])
            _smoke_owned_environment(old)
            os.environ["AERO_UPDATE_RESTART_TOKEN"] = journal["token"]
            try:
                # Never stop an unexpectedly active worker during recovery.
                records = live_records()
                for record in records:
                    control(record, "drain")
                if any(control(r, "status").get("update_busy", True) for r in records):
                    for record in records:
                        control(record, "resume")
                    raise DistributionError("仍有任务运行，请等任务结束后再回滚。")
                for record in records:
                    _stop_idle(record)
                activate(old)
                for record in journal["processes"]:
                    _restart(record, old)
                    control(record, "resume")
                barrier.unlink()
            finally:
                os.environ.pop("AERO_UPDATE_RESTART_TOKEN", None)
        else:
            previous = install_root() / "previous"
            if not previous.is_symlink():
                raise DistributionError("没有可回滚的上一版本。")
            target = previous.resolve()
            _smoke_owned_environment(target)
            switch_environment(target, wait=wait)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aero update")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true")
    group.add_argument("--rollback", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--wait", type=float, default=60, help="等待后台空闲的秒数（默认 60）")
    args = parser.parse_args(argv)
    if not 0 <= args.wait <= 3600:
        parser.error("--wait 必须在 0–3600 秒之间")
    try:
        if args.rollback:
            if not args.yes and (
                not sys.stdin.isatty() or input("回滚到上一版本？[y/N] ").lower() != "y"
            ):
                return 0
            rollback(wait=args.wait)
            print("已回滚到上一版本。")
            return 0
        release = check_update(force=True)
        if release is None:
            print(f"当前版本 {get_version()}，没有更新的稳定版。")
            return 0
        version = release["manifest"]["version"]
        print(f"发现新版本 {version}（当前 {get_version()}）。")
        if not managed_install():
            print(external_update_command())
            return 0 if args.check else 2
        if args.check:
            return 0
        if not args.yes and (
            not sys.stdin.isatty() or input("空闲后更新并重启后台服务？[y/N] ").lower() != "y"
        ):
            print("未执行更新。")
            return 0
        perform_update(release, wait=args.wait)
        print(f"已更新到 {version}，后台服务已恢复。")
        return 0
    except Exception as exc:
        # Avoid logging signed HTTP request details or local bearer capabilities.
        message = (
            str(exc)
            if isinstance(exc, DistributionError)
            else "操作失败，请检查网络或运行状态后重试。"
        )
        print("更新失败：" + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
