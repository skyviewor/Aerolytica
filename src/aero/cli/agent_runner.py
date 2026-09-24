"""Standalone remote Agent command runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

from aero.agent.loop import AgentLoop
from aero.agent.audit_snapshot import render_conversation, zip_conversation
from aero.agent.session import SessionManager, SessionMeta
from aero.application.cloud_runtime import CloudAgentRuntime, RelayRuntimeTransport
from aero.application.local_session import LocalSession
from aero.core.config import AeroConfig
from aero.core.official_account import OfficialAccountError
from aero.core.remote_agent import (
    AgentProcessLock,
    RemoteAgentClient,
    RemoteAgentError,
    renew_command_lease,
)
from aero.core.cloud_sync import CloudSyncEngine


def run_agent_cli(args: list[str]) -> int:
    """Dispatch ``aero agent`` without starting the Textual application."""
    if not args or args[0] in {"-h", "--help", "help"}:
        _print_agent_usage()
        return 0 if args else 2
    action = args[0]
    try:
        if action == "register":
            name, description = _registration_args(args[1:])
            return asyncio.run(_register(name, description))
        if action == "list":
            if len(args) != 1:
                raise ValueError("aero agent list 不接受其他参数")
            return asyncio.run(_list_agents())
        if action == "run":
            selector, cloud_memory = _run_args(args[1:])
            return asyncio.run(_run_service(selector, cloud_memory=cloud_memory))
        if action == "status":
            selector = _selector_arg(args[1:], "status")
            return asyncio.run(_show_status(selector))
        if action == "snapshot":
            selector, upload, output = _snapshot_args(args[1:])
            return asyncio.run(_export_snapshot(selector, upload=upload, output=output))
        if action == "takeover":
            selector = _selector_arg(args[1:], "takeover")
            if selector is None:
                raise ValueError("用法：aero agent takeover 智能体名称或 ID")
            return asyncio.run(_takeover(selector))
        if action == "clone":
            selector = _selector_arg(args[1:], "clone")
            if selector is None:
                raise ValueError("用法：aero agent clone 智能体名称或 ID")
            return asyncio.run(_takeover(selector, clone=True))
        if action == "resume":
            selector = _selector_arg(args[1:], "resume")
            if selector is None:
                raise ValueError("用法：aero agent resume 智能体名称或 ID")
            return asyncio.run(_resume_stopped_agent(selector))
        raise ValueError(f"未知 Agent 子命令：{action}")
    except (OfficialAccountError, RemoteAgentError, ValueError) as exc:
        print(f"错误：{exc}")
        return 1


def _registration_args(args: list[str]) -> tuple[str, str]:
    name = ""
    description = ""
    index = 0
    while index < len(args):
        flag = args[index]
        if flag not in {"--name", "--description"} or index + 1 >= len(args):
            raise ValueError('用法：aero agent register --name ocean [--description "备注"]')
        value = args[index + 1].strip()
        if not value:
            raise ValueError(f"{flag} 不能为空")
        if flag == "--name":
            name = value
        else:
            description = value
        index += 2
    if not name:
        raise ValueError('用法：aero agent register --name ocean [--description "备注"]')
    return name, description


def _selector_arg(args: list[str], command: str) -> str | None:
    if not args:
        return None
    if len(args) == 1 and args[0].strip():
        return args[0].strip()
    raise ValueError(f"用法：aero agent {command} [智能体名称或智能体 ID]")


def _run_args(args: list[str]) -> tuple[str | None, bool]:
    selector = None
    cloud_memory = False
    index = 0
    while index < len(args):
        if args[index] == "--cloud-memory" and not cloud_memory:
            cloud_memory = True
            index += 1
        elif not args[index].startswith("-") and args[index].strip():
            if selector is not None:
                raise ValueError("智能体名称或 ID 只能指定一次")
            selector = args[index].strip()
            index += 1
        else:
            raise ValueError(
                "用法：aero agent run [智能体名称或智能体 ID] [--cloud-memory]"
            )
    return selector, cloud_memory


def _snapshot_args(args: list[str]) -> tuple[str | None, bool, Path | None]:
    selector = None
    output = None
    upload = False
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--upload":
            upload = True
            index += 1
        elif item == "--output" and index + 1 < len(args):
            output = Path(args[index + 1]).expanduser()
            index += 2
        elif not item.startswith("-") and selector is None:
            selector = item
            index += 1
        else:
            raise ValueError("用法：aero agent snapshot [名称或 ID] [--output 文件.zip] [--upload]")
    return selector, upload, output


async def _export_snapshot(selector: str | None, *, upload: bool, output: Path | None) -> int:
    try:
        client = RemoteAgentClient(agent_selector=selector)
        resident = True
    except RemoteAgentError:
        client = RemoteAgentClient(select_agent=False)
        resident = False
    try:
        if resident:
            remote = await client.status()
        else:
            response = await client.session.request("GET", "/v1/agents")
            if response.status_code >= 400:
                raise RemoteAgentError("读取云端智能体列表失败。")
            matches = [item for item in response.json().get("agents", [])
                       if item.get("agent_type") == "chat" and
                       (selector is None or selector in {item.get("agent_id"), item.get("name")})]
            if len(matches) != 1:
                raise RemoteAgentError("请指定一个本机普通聊天的智能体 ID。")
            remote = matches[0]
            client.agent_id = remote["agent_id"]
            client.agent_name = remote["name"]
        session_id = str(remote.get("session_id") or _memory_session_id(client.agent_id, Path.cwd().resolve()))
        manager = SessionManager()
        if manager.load(session_id) is None:
            manager = SessionManager(Path.home() / ".aero" / "agent-memory")
        text = render_conversation(manager, session_id)
        archive = zip_conversation(text)
        destination = output or Path.cwd() / (
            f"agent-{client.agent_id}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        )
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(archive)
        print(f"审计快照已导出：{destination}（{len(archive)} 字节）")
        print("请先人工审阅 ZIP 中的 conversation.txt，脱敏无法识别所有敏感信息。")
        if upload:
            if not sys.stdin.isatty() or input("确认已审阅文件，允许上传云端？输入 YES：").strip() != "YES":
                raise RemoteAgentError("上传已取消；本地 ZIP 已保留。")
            await _upload_audit_snapshot(client, destination.name, archive)
            print("审计快照已上传云端，可在网页智能体详情中查看或下载。")
        return 0
    finally:
        await client.close()


async def _upload_audit_snapshot(client: RemoteAgentClient, filename: str, archive: bytes) -> None:
    content_type = "application/zip"
    response = await client.session.request("POST", "/v1/storage/upload-url", json={
        "filename": filename,
        "size_bytes": len(archive),
        "content_type": content_type,
    })
    if response.status_code >= 400:
        raise RemoteAgentError(f"申请云端存储失败：{response.status_code}")
    upload = response.json()
    if httpx.URL(upload["upload_url"]).scheme != "https":
        raise RemoteAgentError("云存储签名地址必须使用 HTTPS。")
    async with httpx.AsyncClient(timeout=60) as http:
        uploaded = await http.put(
            upload["upload_url"], content=archive, headers={"Content-Type": content_type}
        )
        uploaded.raise_for_status()
    completed = await client.session.request(
        "POST", f"/v1/storage/{upload['object_id']}/complete"
    )
    if completed.status_code >= 400:
        raise RemoteAgentError(f"云端文件校验失败：{completed.status_code}")
    linked = await client.session.request(
        "POST", f"/v1/agents/{client.agent_id}/audit-snapshots",
        json={"object_id": upload["object_id"]},
    )
    if linked.status_code >= 400:
        raise RemoteAgentError(f"云端快照关联失败：{linked.status_code}")


async def _takeover(selector: str, *, clone: bool = False) -> int:
    root = Path.cwd().resolve()
    if any(root.iterdir()):
        raise RemoteAgentError("请在空目录执行接管，避免云端文件覆盖本地内容。")
    client = RemoteAgentClient(select_agent=False)
    try:
        await client.session.access_token()
        response = await client.session.request("GET", "/v1/agents")
        if response.status_code >= 400:
            raise RemoteAgentError("读取云端智能体列表失败。")
        matches = [item for item in response.json().get("agents", [])
                   if selector in {item.get("agent_id"), item.get("name")}]
        if len(matches) != 1:
            raise RemoteAgentError("找不到唯一的目标智能体，请使用智能体 ID。")
        agent_id = matches[0]["agent_id"]
        if matches[0].get("agent_type") == "chat" and not clone:
            existing_id = str(matches[0].get("session_id") or "")
            if existing_id and SessionManager().load(existing_id) is not None:
                raise RemoteAgentError("本机已有同 ID 会话，请先备份或选择另一设备，避免覆盖。")
        response = await client.session.request(
            "POST", f"/v1/agents/{agent_id}/{'clone' if clone else 'takeover'}"
        )
        if response.status_code >= 400:
            raise RemoteAgentError(
                f"{'克隆' if clone else '接管'}失败：{response.status_code}"
                + ("，请确认原设备已离线。" if not clone else "。")
            )
        result = response.json()
        if matches[0].get("agent_type") == "chat" and not clone:
            context_response = await client.session.request(
                "GET", f"/v1/agents/{result['agent_id']}/context"
            )
            if context_response.status_code >= 400:
                raise RemoteAgentError("接管成功，但读取云端聊天上下文失败；请重试或联系管理员。")
            payload = context_response.json()["context"]
            payload.setdefault("meta", {})["project_dir"] = str(root)
            SessionManager().import_portable_context(result["session_id"], payload)
            print(f"聊天上下文已恢复到本机。请在当前目录运行 aero chat --continue 继续会话：{result['display_name']}")
            return 0
        client.registry.add(
            agent_id=result["agent_id"], name=result["name"], description="", token=result["token"],
        )
        engine = CloudSyncEngine(root, client.session)
        await engine.bind(client.session.data.user_id, result["project_id"])
        await engine.sync_once()
        print(f"云端项目和上下文准备就绪，正在此设备启动：{result['display_name']}")
    finally:
        await client.close()
    return await _run_service(result["agent_id"])


async def _resume_stopped_agent(selector: str) -> int:
    """Re-issue a token after an explicit web stop, without moving files."""
    client = RemoteAgentClient(select_agent=False)
    try:
        response = await client.session.request("GET", "/v1/agents")
        response.raise_for_status()
        matches = [item for item in response.json().get("agents", [])
                   if selector in {item.get("agent_id"), item.get("name")}]
        if len(matches) != 1:
            raise RemoteAgentError("找不到唯一的智能体，请使用智能体 ID。")
        target = matches[0]
        if target.get("agent_type") == "chat":
            raise RemoteAgentError("普通聊天请在客户端开启新会话。")
        response = await client.session.request(
            "POST", f"/v1/agents/{target['agent_id']}/takeover",
            json={"resume_local": True},
        )
        if response.status_code >= 400:
            raise RemoteAgentError("原设备仍在线或智能体没有可恢复会话，暂不能重新上线。")
        result = response.json()
        client.registry.add(
            agent_id=result["agent_id"], name=result["name"],
            description="", token=result["token"],
        )
        print(f"已恢复智能体授权：{result['display_name']}。请在已绑定云项目的工作区运行 aero agent run {result['agent_id']}。")
        return 0
    finally:
        await client.close()


async def _register(name: str, description: str) -> int:
    client = RemoteAgentClient(select_agent=False)
    try:
        result = await client.register(name, description=description)
        print(f"智能体已注册：{result.get('name') or name}")
        if description:
            print(f"描述：{description}")
        print(f"智能体 ID：{result.get('agent_id') or ''}")
        print("令牌已安全保存到 ~/.aero/secrets.yaml")
        print(f"运行 aero agent run {name} 开始待命。")
        return 0
    finally:
        await client.close()


async def _list_agents() -> int:
    client = RemoteAgentClient(select_agent=False)
    try:
        agents = await client.list_registered_remote_agents()
        if not agents:
            print("本地没有已注册的智能体。")
            return 0
        for agent in agents:
            last_seen = f"，最后活动：{agent['last_seen_at']}" if agent.get("last_seen_at") else ""
            print(
                f"{agent['name']}\t{agent.get('description') or '-'}\t{agent['status']}"
                f"\t{agent['agent_id']}{last_seen}"
            )
        return 0
    finally:
        await client.close()


async def _show_status(selector: str | None) -> int:
    client = RemoteAgentClient(agent_selector=selector)
    try:
        status = await client.status()
        print(f"智能体：{status.get('name') or client.agent_name or client.agent_id}")
        print(f"状态：{status.get('status') or 'unknown'}")
        if status.get("last_seen_at"):
            print(f"最后活动：{status['last_seen_at']}")
        return 0
    finally:
        await client.close()


async def _run_service(selector: str | None, *, cloud_memory: bool = False) -> int:
    """Run the fenced resident worker against the workspace's bound project."""
    config = _load_config()
    client = RemoteAgentClient(agent_selector=selector)
    client._require_registration()
    try:
        await client.session.access_token()
    except Exception:
        await client.close()
        raise
    binding_path = Path.cwd().resolve() / ".aero" / "cloud-sync" / "state.json"
    try:
        state = json.loads(binding_path.read_text())
    except (OSError, ValueError) as exc:
        await client.close()
        raise RemoteAgentError("请先将当前工作区绑定到云端项目，再启动常驻智能体。") from exc
    binding = state.get("binding") if isinstance(state, dict) else None
    project_id = binding.get("project_id") if isinstance(binding, dict) else None
    if not project_id:
        await client.close()
        raise RemoteAgentError("请先将当前工作区绑定到云端项目，再启动常驻智能体。")

    project_dir = Path.cwd().resolve()
    remote_agent = await client.status()
    session_id = str(remote_agent.get("session_id") or _memory_session_id(client.agent_id, project_dir))
    if remote_agent.get("project_id") and remote_agent["project_id"] != project_id:
        await client.close()
        raise RemoteAgentError("本地云项目与智能体绑定项目不同，不能启动。")
    process_lock = AgentProcessLock(client.agent_id)
    process_lock.acquire()
    session = LocalSession(project_dir, config, session_id=session_id)
    http = httpx.AsyncClient(
        base_url=client.session.base_url,
        timeout=httpx.Timeout(35.0, connect=10.0),
    )
    runtime = CloudAgentRuntime(
        session,
        RelayRuntimeTransport(
            http, client.agent_id, client.agent_token,
            account_access_token=client.session.access_token,
        ),
        device_id=f"cli-{project_dir.name}",
        project_id=str(project_id),
        account_id=client.session.data.user_id,
        account_identity=lambda: client.session.data.user_id,
        state_dir=Path.home() / ".aero" / "agent-runtime",
    )
    memory_info = runtime.enable_memory() if cloud_memory else None
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, request_stop)
    await runtime.start()
    print(f"远程智能体已启动：{client.agent_name or client.agent_id}")
    print("已绑定云端项目：" + str(project_id))
    if memory_info:
        print("加密云记忆已开启；请立即保存恢复密钥：" + memory_info["recovery_key"])
    print("正在等待云端指令，按 Ctrl+C 停止。")
    try:
        stop_waiter = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                [stop_waiter, runtime._task], return_when=asyncio.FIRST_COMPLETED
            )
            if runtime._task in done and runtime.state == "login_required":
                print("客户端登录或智能体授权已下线，请重新登录后重启智能体。")
                return 1
            if runtime._task in done:
                runtime._task.result()
                return 1
            return 0
        finally:
            stop_waiter.cancel()
    finally:
        if memory_info:
            with suppress(Exception):
                await runtime.export_memory()
        await runtime.stop()
        await session.close()
        await http.aclose()
        await client.close()
        process_lock.release()


async def _run_legacy_service(selector: str | None, *, cloud_memory: bool = False) -> int:
    config = _load_config()
    agent = AgentLoop(config)
    client = RemoteAgentClient(agent_selector=selector)
    client._require_registration()
    process_lock = AgentProcessLock(client.agent_id)
    process_lock.acquire()
    project_dir = Path.cwd().resolve()
    memory_manager = SessionManager(Path.home() / ".aero" / "agent-memory")
    memory_session_id = _memory_session_id(client.agent_id, project_dir)
    memory_source = await _restore_agent_memory(
        client,
        agent,
        memory_manager,
        memory_session_id,
        cloud_memory=cloud_memory,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        stop_event.set()
        agent.cancel()

    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, request_stop)

    print(f"远程 Agent 已启动：{client.agent_name or client.agent_id}")
    print(f"本地记忆：已开启{f'（已从{memory_source}恢复）' if memory_source else ''}")
    print(f"云端记忆：{'已开启' if cloud_memory else '未开启'}")
    print("正在等待云端指令，按 Ctrl+C 停止。")

    async def handle_command(command: dict[str, Any]) -> None:
        await _execute_command(client, agent, command)
        await _save_agent_memory(
            client,
            agent,
            memory_manager,
            memory_session_id,
            project_dir,
            cloud_memory=cloud_memory,
        )

    try:
        await client.serve(
            handle_command,
            stop_event=stop_event,
        )
        return 0
    finally:
        with suppress(Exception):
            await _save_agent_memory(
                client,
                agent,
                memory_manager,
                memory_session_id,
                project_dir,
                cloud_memory=cloud_memory,
            )
        try:
            await client.close()
        finally:
            process_lock.release()


def _memory_session_id(agent_id: str, project_dir: Path) -> str:
    identity = f"{agent_id}\0{project_dir}".encode("utf-8")
    return f"remote-{hashlib.sha256(identity).hexdigest()[:24]}"


async def _restore_agent_memory(
    client: RemoteAgentClient,
    agent: AgentLoop,
    manager: SessionManager,
    session_id: str,
    *,
    cloud_memory: bool,
) -> str:
    loaded = manager.load(session_id)
    source = "本地记忆" if loaded else ""
    if loaded is None and cloud_memory:
        try:
            encrypted = await client.download_memory(session_id)
            if encrypted:
                loaded = manager.load_snapshot(encrypted)
                manager.save(session_id, loaded[0], loaded[1])
                source = "云端备份"
        except Exception as exc:
            print(f"警告：云端记忆恢复失败，将使用新的本地上下文：{exc}")
    if loaded is not None:
        messages, _ = loaded
        if messages:
            agent.messages = messages
            agent.reset_system_prompt(agent.config.language)
    return source


async def _save_agent_memory(
    client: RemoteAgentClient,
    agent: AgentLoop,
    manager: SessionManager,
    session_id: str,
    project_dir: Path,
    *,
    cloud_memory: bool,
) -> None:
    meta = SessionMeta(
        id=session_id,
        name=f"远程 Agent · {client.agent_name or client.agent_id}",
        model=agent.config.llm.model,
        provider=agent.config.llm.provider,
        mode=agent.config.mode,
        title_source="remote_agent",
        project_dir=str(project_dir),
    )
    manager.save(session_id, agent.messages, meta)
    if not cloud_memory:
        return
    encrypted = manager.snapshot(session_id)
    if encrypted is None:
        return
    try:
        await client.upload_memory(session_id, encrypted)
    except Exception as exc:
        print(f"警告：本地记忆已保存，但云端同步失败：{exc}")


async def _execute_command(
    client: RemoteAgentClient,
    agent: AgentLoop,
    command: dict[str, Any],
) -> None:
    command_id = str(command.get("command_id") or "")
    content = str(command.get("content") or "").strip()
    if not command_id or not content:
        return
    print(f"收到命令 {command_id}：{content[:80]}")
    await client.update_command_status(command_id, "running")
    await client.send_progress("已收到指令，正在处理…", command_id=command_id)
    lease_stop = asyncio.Event()
    lease_task = asyncio.create_task(
        renew_command_lease(client, command_id, lease_stop),
        name=f"remote-agent-lease-{command_id}",
    )
    response_text = ""
    try:
        async for event in agent.run_stream(content):
            if event.type == "status" and event.content:
                await client.send_progress(str(event.content), command_id=command_id)
            elif event.type == "text":
                response_text += event.content or ""
            elif event.type == "confirm":
                await client.send_progress(
                    "该操作需要本地确认，远程请求暂未执行。",
                    command_id=command_id,
                )
                if agent.confirm_future is not None and not agent.confirm_future.done():
                    agent.confirm_future.set_result("deny")
            elif event.type == "content_blocked":
                await client.send_error(
                    event.content or "请求被拦截。", command_id=command_id
                )
                return
        await client.send_message(
            response_text.strip() or "指令已处理，但没有可返回的文本结果。",
            command_id=command_id,
        )
        print(f"命令已完成：{command_id}")
    except Exception as exc:
        message = f"本地 Agent 执行失败：{exc}"
        with suppress(Exception):
            await client.send_error(message, command_id=command_id)
        print(message)
    finally:
        lease_stop.set()
        lease_task.cancel()
        with suppress(asyncio.CancelledError):
            await lease_task


def _load_config() -> AeroConfig:
    config_path = Path.cwd() / "aero.yaml"
    return AeroConfig.load(config_path) if config_path.exists() else AeroConfig.create_default()


def _print_agent_usage() -> None:
    print(
        """远程 Agent 命令：
  aero agent register --name ocean [--description \"备注\"]  注册并保存 Agent 凭据
  aero agent list                              列出本地 Agent 及在线状态
  aero agent run [Agent 名称或 Agent ID] [--cloud-memory]  前台常驻并等待云端指令
  aero agent snapshot [名称或 ID] [--output 文件.zip] [--upload]  导出可读审计快照
  aero agent takeover [名称或 ID]  在空目录接管已离线智能体及云端项目
  aero agent clone [名称或 ID]     在空目录克隆智能体和云端项目
  aero agent resume [名称或 ID]    网页下线后恢复原工作区的智能体授权
  aero agent status [Agent 名称或 Agent ID]  查询指定 Agent 状态"""
    )
