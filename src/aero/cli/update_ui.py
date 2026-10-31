"""Update consent uses the existing input-aligned approval dialog."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from aero.distribution import (
    CHECK_INTERVAL,
    DistributionError,
    install_root,
    managed_install,
    write_state,
)


def idle(app) -> bool:
    from textual.css.query import NoMatches

    if hasattr(app, "query_one"):
        try:
            if app.query_one("#user-input").text:
                return False
        except NoMatches:
            return False
    conversation = getattr(app, "conversation", None)
    if conversation is not None and (
        not getattr(app, "_booted", False)
        or conversation.agent.get("status") != "online"
        or any(row.get("status") == "pending" for row in app._approvals.values())
    ):
        return False
    worker = getattr(app, "_agent_worker", None)
    children = getattr(app, "_subagent_workers", {})
    return (
        not getattr(app, "_updating", False)
        and (worker is None or not worker.is_running)
        and not getattr(app, "_sending", False)
        and not any(getattr(child, "is_running", False) for child in children.values())
        and len(app.screen_stack) == 1
    )


def start_update_checks(app) -> None:
    if managed_install():
        app.set_interval(5, lambda: refresh_pending(app))
        app.run_worker(refresh_pending(app), exclusive=False, group="pending-update")
    # Editable source has no signing keys, and must never contact the release
    # endpoint in development or during UI tests.
    from aero import distribution

    try:
        keys = json.loads(Path(distribution.__file__).with_name("release_keys.json").read_text())
    except (OSError, ValueError):
        return
    if keys:
        app.run_worker(offer_update(app), exclusive=False, group="release-check")


async def offer_update(app, *, explicit: bool = False) -> None:
    from aero.cli.main import ConfirmScreen
    from aero.cli.pending_update import prepare_update, update_status
    from aero.cli.update import check_update, external_update_command, skip_version
    from aero.version import get_version

    if explicit and not idle(app):
        app.notify("请等当前任务和审批完成后再更新。", severity="warning")
        return
    if managed_install():
        try:
            state = await asyncio.to_thread(update_status)
            if state["status"] in {"preparing", "ready"}:
                if explicit:
                    app.notify(state["message"], timeout=10)
                await refresh_pending(app)
                return
        except (DistributionError, OSError, ValueError):
            pass
    try:
        release = await asyncio.to_thread(check_update, force=explicit)
    except Exception as exc:
        if explicit:
            app.notify(
                str(exc)
                if isinstance(exc, DistributionError)
                else "暂时无法检查更新，请稍后重试。",
                severity="warning",
            )
        return
    if release is None:
        if explicit:
            app.notify(f"当前版本 {get_version()}，没有更新的稳定版。")
        return
    state_path = install_root() / "update-prompt.json"
    if not explicit:
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
        elapsed = time.time() - state.get("prompted_at", 0)
        if state.get("version") == release["manifest"]["version"] and 0 <= elapsed < CHECK_INTERVAL:
            return
        while not idle(app):
            await asyncio.sleep(3)
    version = release["manifest"]["version"]
    write_state(state_path, {"prompted_at": time.time(), "version": version})
    if not managed_install():
        if explicit:
            app.notify(f"新版本 {version}；{external_update_command()}", timeout=15)
        else:
            app.notify(f"发现新版本 {version}，执行 /update 查看升级方法。", timeout=8)
        return
    notes = str(release["manifest"].get("notes") or "")[:2000]
    message = (
        f"当前版本：{get_version()} → {version}\n"
        "将在后台独立环境安装并校验，当前对话和 Agent 继续运行，不切换版本。\n"
        "准备好后由你手动点击「重启并应用」或执行 /restart。\n"
        "不会修改项目文件、账号配置或科学计算环境。\n" + notes
    )

    async def apply() -> None:
        if not idle(app):
            app.notify("任务状态已变化，请等任务结束后再更新。")
            return
        footer = getattr(app, "_set_footer_status", None)
        if footer:
            footer("正在准备客户端更新…")
        app.notify("正在准备新版本，请稍候；当前版本仍保留。", timeout=8)
        try:
            state = await asyncio.to_thread(prepare_update)
            app.notify(state.get("message", "更新正在后台准备。"), timeout=10)
            await refresh_pending(app)
        except Exception as exc:
            app.notify(
                str(exc)
                if isinstance(exc, DistributionError)
                else "升级失败，未确认完成；请执行 aero update --rollback 检查恢复。",
                severity="error",
                timeout=15,
            )
            if footer:
                footer("升级未完成；当前对话可以继续。")
            return

    def resolved(choice: str) -> None:
        if choice == "allow":
            app.run_worker(apply(), exclusive=False, group="release-install")
        elif choice == "deny":
            skip_version(version)

    app.push_screen(
        ConfirmScreen(
            message,
            title="客户端版本更新",
            allow_label="准备更新",
            always_label="稍后",
            deny_label="跳过此版本",
        ),
        resolved,
    )


async def refresh_pending(app) -> None:
    """A local-file check only; one notification per state transition."""
    from textual.css.query import NoMatches
    from textual.widgets import Button

    from aero.cli.pending_update import update_status

    try:
        state = await asyncio.to_thread(update_status)
        try:
            app.query_one("#restart-update", Button).display = state["status"] == "ready"
        except NoMatches:
            pass
        key = (state["status"], state.get("version"), state.get("revision"))
        if key != getattr(app, "_last_update_state", None):
            app._last_update_state = key
            if state["status"] in {"ready", "error", "interrupted"}:
                app.notify(state["message"], timeout=12)
    except (DistributionError, OSError, ValueError, KeyError):
        return


async def request_restart(app) -> None:
    """Invoked by the local user's button or slash command, never an Agent tool."""
    from aero.cli.pending_update import apply_pending, update_status

    if not idle(app):
        app.notify("请先完成当前任务和审批，再重启应用更新。", severity="warning")
        return
    try:
        state = await asyncio.to_thread(update_status)
        if state["status"] != "ready":
            app.notify(state.get("message", "没有准备好的更新。"))
            return
        app._updating = True
        save = getattr(app, "_save_session_on_exit", None)
        if save:
            save()
        await asyncio.to_thread(apply_pending, exclude_pid=os.getpid())
        app._restart_for_update = True
        getattr(app, "_do_cleanup_exit", app.exit)()
    except Exception as exc:
        app._updating = False
        app.notify(
            str(exc)
            if isinstance(exc, DistributionError)
            else "重启应用未完成，保留当前版本；请检查更新状态。",
            severity="warning",
            timeout=15,
        )
