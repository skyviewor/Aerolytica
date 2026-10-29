"""Read/send the original resident conversation; never create an executor."""

from __future__ import annotations

import asyncio
import json
import re
import uuid

import httpx
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import ListView, Static, TextArea

from aero.cli.main import (
    AeroApp,
    ChatMarkdown,
    ChatTextArea,
    ChatTuiApp,
    SelectScreen,
    _display_model_name,
    _load_saved_theme,
    _resolve_theme_name,
)
from aero.core.official_account import (
    OfficialAccountError,
    OfficialAccountSession,
    OfficialLoginRequiredError,
)
from aero.core.official_models import OfficialModelCatalog
from aero.core.remote_agent import RemoteAgentError, RemoteAgentRegistry
from aero.version import title


def chat_agent_args(args: list[str]) -> tuple[str | None, list[str]]:
    remaining = list(args)
    if "--agent" not in remaining:
        return None, remaining
    index = remaining.index("--agent")
    if remaining.count("--agent") != 1 or index + 1 == len(remaining):
        raise ValueError("用法：aero chat --agent 智能体名称或 ID")
    selector = remaining[index + 1]
    if not selector or selector.startswith("-"):
        raise ValueError("--agent 后必须指定智能体名称或 ID")
    del remaining[index : index + 2]
    if any(arg in remaining for arg in ("--service", "--attach", "--continue", "-c")):
        raise ValueError("--agent 不能与 --service、--attach 或 --continue 混用")
    return selector, remaining


class ResidentConversation:
    def __init__(self, selector: str, *, account=None, stream_client=None):
        agent = RemoteAgentRegistry().resolve(selector)
        self.agent_id = agent.agent_id
        self.name = agent.name
        self.account = account or OfficialAccountSession()
        self.stream_client = stream_client or httpx.AsyncClient(
            base_url=self.account.base_url,
            timeout=httpx.Timeout(40, connect=10),
        )
        self.cursor: str | None = None
        self.agent: dict = {}

    async def request(self, method: str, suffix: str, **kwargs) -> dict:
        response = await self.account.request(
            method,
            f"/v1/agents/{self.agent_id}/{suffix}",
            **kwargs,
        )
        if response.is_error:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise RemoteAgentError(str(detail or f"请求失败（HTTP {response.status_code}）"))
        return response.json()

    async def snapshot(self, *, full_history=False) -> tuple[list[dict], list[dict]]:
        response = await self.account.request("GET", "/v1/agents")
        response.raise_for_status()
        listing = response.json()
        agent = next(
            (row for row in listing.get("agents", []) if row["agent_id"] == self.agent_id), None
        )
        if agent is None:
            raise RemoteAgentError("当前账号下找不到该 Agent；请检查登录账号或是否已删除。")
        if agent.get("agent_type") == "chat":
            raise RemoteAgentError("该 Agent 仍是普通聊天，请先转为常驻后再接入。")
        self.agent = agent
        if self.cursor is None:
            self.cursor = listing.get("event_cursor") or "0"
        pages = []
        before = None
        seen = set()
        while True:
            params = {"limit": 200}
            if before:
                params["before"] = before
            result = await self.request("GET", "messages", params=params)
            pages.append(result.get("messages", []))
            before = result.get("next_cursor")
            if not full_history or not before or before in seen:
                break
            seen.add(before)
        approvals = await self.request("GET", "approvals")
        return [row for page in reversed(pages) for row in page], approvals.get("approvals", [])

    async def events(self):
        token = await self.account.access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
            "Last-Event-ID": self.cursor or "0",
        }
        async with self.stream_client.stream(
            "GET", "/v1/agents/events", headers=headers
        ) as response:
            response.raise_for_status()
            event, identifier, lines = "message", None, []
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("id:"):
                    identifier = line[3:].strip()
                elif line.startswith("data:"):
                    lines.append(line[5:].lstrip())
                elif not line and lines:
                    data = json.loads("\n".join(lines))
                    yield event, data
                    if identifier:
                        self.cursor = identifier
                    event, identifier, lines = "message", None, []

    async def close(self):
        # Closing a viewer never calls stop, chat-exit, runtime/release or cancel.
        await self.stream_client.aclose()
        await self.account.close()


class ResidentChatInput(ChatTextArea):
    """Normal chat editor, submitting only to the resident transport."""

    def action_submit(self) -> None:
        if self.app._cmd_list.styles.display != "none":
            self.app._execute_selected_command()
            return
        self.app.submit()


class ResidentChatApp(ChatTuiApp):
    # Reuse the normal TUI presentation, not its local executor lifecycle.
    CHAT_INPUT_CLASS = ResidentChatInput
    ENABLE_COMMAND_PALETTE = False
    # Share menu filtering, rendering, highlighting and placement with normal chat.
    _populate_command_list = AeroApp._populate_command_list
    _sync_command_selection = AeroApp._sync_command_selection
    _command_item_text = AeroApp._command_item_text
    _command_candidates = AeroApp._command_candidates
    _command_list_height = AeroApp._command_list_height
    _resize_command_list = AeroApp._resize_command_list
    _position_command_list = AeroApp._position_command_list
    BINDINGS = [
        ("ctrl+q", "quit", "关闭界面（Agent 继续运行）"),
        Binding("ctrl+y", "copy_last_reply", "复制回复", priority=True),
        Binding("tab", "cycle_mode", "切换模式", show=False, priority=True),
    ]

    def __init__(self, selector: str, *, conversation=None):
        super().__init__()
        self.conversation = conversation or ResidentConversation(selector)
        self._rows: dict[str, Static | ChatMarkdown] = {}
        self._messages: dict[str, dict] = {}
        self._approvals: dict[str, dict] = {}
        self._booted = False
        self._sending = False
        self._model = "auto"
        self._resolved_model = ""
        self._mode = "execute"
        self._last_reply_text = ""
        self._cmd_list: ListView | None = None
        self._filtered_commands: list[tuple[str, str]] = []
        self._commands_list = [
            ("/update", "检查客户端版本更新"),
            ("/model", "选择后续请求使用的模型"),
            ("/mode", "选择执行、问答或规划模式"),
            ("/copy", "复制最后一条回复"),
            ("/theme", "切换当前界面主题"),
            ("/approve", "批准待处理操作，可指定审批 ID"),
            ("/deny", "拒绝待处理操作，可指定审批 ID"),
            ("/cancel", "取消当前任务"),
            ("/help", "查看此接入模式支持的命令"),
            ("/quit", "关闭界面，后台 Agent 继续运行"),
        ]
        self._secondary_commands_list = [
            ("/model auto", "自动选择模型"),
            ("/mode execute", "执行模式"),
            ("/mode qa", "问答模式"),
            ("/mode plan", "规划模式"),
            ("/theme dark", "深色主题"),
            ("/theme light", "浅色主题"),
        ]
        saved_theme = _load_saved_theme()
        theme = _resolve_theme_name(saved_theme, self.available_themes) if saved_theme else None
        if theme:
            self.theme = theme

    def on_mount(self):
        self._cmd_list = self.query_one("#command-list", ListView)
        self._cmd_list.styles.display = "none"
        self._refresh_composer()
        self.query_one("#footer-status", Static).update("正在读取常驻 Agent 原对话…")
        self.query_one(ResidentChatInput).focus()
        self.query_one("#chat-area").mount(
            Static(f"{title()} · {self.conversation.name}", classes="welcome-line")
        )
        self.run_worker(self._watch(), group="events")
        from aero.cli.update_ui import start_update_checks

        start_update_checks(self)

    def _set_input_focus_style(self, active: bool) -> None:
        self.query_one("#input-box").set_class(active, "input-active")

    def _hide_command_list(self, *, focus_input: bool = True) -> None:
        AeroApp._hide_command_list(self, focus_input=focus_input)

    @on(TextArea.Changed, "#user-input")
    async def on_input_changed(self, event: TextArea.Changed) -> None:
        value = event.text_area.text.strip()
        if value.startswith("/"):
            await self._populate_command_list(value)
        else:
            self._hide_command_list(focus_input=False)

    @on(ListView.Selected, "#command-list")
    def select_command(self, event: ListView.Selected) -> None:
        event.stop()
        self._execute_selected_command()

    def _execute_selected_command(self) -> None:
        if self._cmd_list.index is None or not self._filtered_commands:
            return
        command = self._filtered_commands[self._cmd_list.index][0]
        self.query_one(ResidentChatInput).load_text(command)
        self._hide_command_list()
        self.submit()

    def _choose_model(self, selected: str | None) -> None:
        if selected is not None:
            self._model = selected
            self._refresh_composer()

    def _choose_mode(self, selected: str | None) -> None:
        if selected is not None:
            self._mode = selected
            self._refresh_composer()

    def _choose_theme(self, selected: str | None) -> None:
        if selected is not None:
            self.theme = selected

    def _scroll_chat(self, *, up: bool) -> None:
        self.query_one("#chat-area", VerticalScroll).scroll_relative(
            y=-5 if up else 5,
            animate=False,
            force=True,
            immediate=True,
        )

    def action_copy_last_reply(self) -> None:
        if self._last_reply_text:
            self.copy_to_clipboard(self._last_reply_text)
            self.notify("已复制最后一条回复")

    def _refresh_composer(self) -> None:
        labels = {"execute": "执行", "qa": "问答", "plan": "规划"}
        box = self.query_one("#input-box")
        for mode in labels:
            box.set_class(self._mode == mode, f"mode-{mode}")
        self.query_one("#mode-label", Static).update(labels[self._mode])
        model = "自动" if self._model == "auto" else self._model
        if self._model in {"auto", "default"} and self._resolved_model:
            model += f" · 实际：{_display_model_name(self._resolved_model)}"
        self.query_one("#input-meta", Static).update(
            f"模型：{model} · Enter 发送 · Shift+Enter 换行"
        )

    def action_cycle_mode(self) -> None:
        modes = ["execute", "qa", "plan"]
        self._mode = modes[(modes.index(self._mode) + 1) % len(modes)]
        self._refresh_composer()

    async def on_unmount(self):
        await self.conversation.close()

    def _write(self, key: str, text: str, role="system"):
        viewport = self.query_one("#chat-area", VerticalScroll)
        follow = viewport.max_scroll_y - viewport.scroll_y < 3
        if key in self._rows:
            self._rows[key].update(text)
        else:
            label, label_class = {
                "user": ("你", "user-label"),
                "assistant": ("Aero", "agent-label"),
            }.get(role, ("状态", "agent-label"))
            body = (
                ChatMarkdown(text, classes="message-body agent-content")
                if role == "assistant"
                else Static(
                    text,
                    classes="message-body user-content"
                    if role == "user"
                    else "message-body agent-content",
                    markup=False,
                )
            )
            row = Horizontal(
                Static(label, classes=f"message-label {label_class}"),
                body,
                classes="message-row",
            )
            self._rows[key] = body
            viewport.mount(row)
        if follow:
            self.call_after_refresh(viewport.scroll_end, animate=False)

    def _status(self, connection="已连接"):
        agent = self.conversation.agent
        state = {"online": "在线待命", "busy": "工作中", "offline": "离线"}.get(
            agent.get("status"), "状态未知"
        )
        if connection != "已连接":
            state = "状态未知"
        self.query_one("#footer-status", Static).update(
            f"{self.conversation.name} · {state} · {connection} · 关闭界面不停止 Agent"
        )

    def _message(self, message: dict):
        identifier = message["message_id"]
        self._messages[identifier] = message
        role = "user" if message.get("sender") == "user" else "assistant"
        content = str(message.get("content") or "")
        if message.get("message_type") == "progress":
            meta = message.get("metadata") or {}
            turn = meta.get("in_reply_to") or meta.get("local_turn_id") or "legacy"
            task = re.match(r"下载进度#(\d+)", content)
            key = f"progress:{turn}:{task[1] if task else 'step'}"
            self._write(key, f"运行进度：{content}")
        else:
            self._write(identifier, content, role)
            if role == "assistant":
                self._last_reply_text = content
                model = (message.get("metadata") or {}).get("resolved_model")
                if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", model):
                    self._resolved_model = model
                    self._refresh_composer()

    def _approval(self, approval: dict):
        identifier = approval["approval_id"]
        self._approvals[identifier] = approval
        if approval.get("status") != "pending":
            if f"approval:{identifier}" in self._rows:
                self._write(f"approval:{identifier}", f"审批 {identifier}：{approval['status']}")
            return
        from aero.application.approval_details import approval_payload

        payload = approval.get("payload") or {}
        if "details" not in payload:
            payload = approval_payload(payload)
        details = "\n".join(
            f"{item['label']}：{item['value']}" for item in payload.get("details", [])
        )
        self._write(
            f"approval:{identifier}",
            f"{payload.get('title', '操作审批')}（{identifier}）\n"
            f"{payload.get('description', '')}\n{details}\n"
            f"/approve {identifier} 批准；/deny {identifier} 拒绝",
        )

    def _event(self, event: str, data: dict):
        if data.get("agent_id") != self.conversation.agent_id:
            return
        if event in {"agent.message", "agent.message_updated", "agent.progress"}:
            self._message(data["message"])
        elif event == "agent.command_status":
            message = self._messages.get(data.get("message_id"))
            if message:
                message["delivery_status"] = data.get("delivery_status") or data.get("status")
        elif event == "agent.status":
            self.conversation.agent["status"] = data.get("status")
            self._status()
        elif event == "agent.title":
            self.title = f"{title()} · {data.get('name') or self.conversation.name}"
        elif event == "agent.approval":
            self._approval(data["approval"])
        elif event in {"agent.secret_required", "agent.input_required"}:
            self._write(
                f"interaction:{event}", "任务需要补充输入或凭据，请在网页的同一 Agent 对话中完成。"
            )

    async def _watch(self):
        delay = 1
        first = True
        while True:
            try:
                messages, approvals = await self.conversation.snapshot(full_history=first)
                for message in messages:
                    self._message(message)
                for approval in approvals:
                    self._approval(approval)
                self._booted = True
                self.title = f"{title()} · {self.conversation.name}"
                self._status()
                if "connection-error" in self._rows:
                    self._rows["connection-error"].update("")
                first = False
                async for event, data in self.conversation.events():
                    self._event(event, data)
                    if (
                        data.get("agent_id") == self.conversation.agent_id
                        and event == "agent.history_synced"
                    ):
                        history, _ = await self.conversation.snapshot(full_history=True)
                        for message in history:
                            self._message(message)
                    delay = 1
                self._booted = False
                self._status("重连中，在线状态待确认")
            except OfficialLoginRequiredError:
                self._booted = False
                self._status("登录失效")
                self._write("connection-error", "请关闭界面，执行 aero login 后重新打开。")
                return
            except (
                httpx.HTTPError,
                RemoteAgentError,
                OfficialAccountError,
                ValueError,
                KeyError,
            ) as exc:
                self._booted = False
                self._status("重连中，在线状态待确认")
                self._write("connection-error", f"连接暂不可用：{exc}")
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)

    def submit(self):
        if getattr(self, "_updating", False):
            self.notify("客户端正在更新，请稍候。")
            return
        editor = self.query_one(ResidentChatInput)
        text = editor.text.strip()
        if text == "/update":
            from aero.cli.update_ui import offer_update

            editor.load_text("")
            self.run_worker(offer_update(self, explicit=True), exclusive=False)
            return
        if text == "/quit":
            self.exit()
            return
        local = (
            text.split()[0] in {"/model", "/mode", "/theme", "/copy", "/help"} if text else False
        )
        if not text or (not self._booted and not local) or self._sending:
            return
        editor.clear()
        self._sending = True
        self.run_worker(self._send(text), group="actions")

    async def _send(self, text: str):
        try:
            parts = text.split()
            if text == "/help":
                self._write(
                    "help",
                    "\n".join(f"{cmd}：{desc}" for cmd, desc in self._commands_list)
                    + "\n此界面连接已有常驻 Agent，不运行第二个本地执行器。",
                )
            elif text == "/copy":
                self.action_copy_last_reply()
            elif text == "/model":
                models = await OfficialModelCatalog().models()
                self.push_screen(
                    SelectScreen(
                        "选择模型",
                        [(model.id, model.display_name) for model in models],
                        self._model,
                    ),
                    self._choose_model,
                )
            elif text == "/mode":
                self.push_screen(
                    SelectScreen(
                        "选择模式",
                        [
                            ("execute", "执行"),
                            ("qa", "问答"),
                            ("plan", "规划"),
                        ],
                        self._mode,
                    ),
                    self._choose_mode,
                )
            elif text == "/theme":
                self.push_screen(
                    SelectScreen(
                        "选择主题", [(name, name) for name in self.available_themes], self.theme
                    ),
                    self._choose_theme,
                )
            elif parts[0] == "/theme" and len(parts) == 2:
                theme = _resolve_theme_name(parts[1], self.available_themes)
                if theme is None:
                    raise ValueError("未知主题，请使用 /theme 选择。")
                self.theme = theme
            elif parts[0] in {"/approve", "/deny"}:
                pending = [
                    item for item in self._approvals.values() if item.get("status") == "pending"
                ]
                if len(parts) > 2:
                    raise ValueError("用法：/approve [审批 ID] 或 /deny [审批 ID]")
                chosen = (
                    next((item for item in pending if item["approval_id"] == parts[1]), None)
                    if len(parts) == 2
                    else (pending[0] if len(pending) == 1 else None)
                )
                if chosen is None:
                    raise ValueError("请指定唯一的待处理审批 ID。")
                result = await self.conversation.request(
                    "POST",
                    f"approvals/{chosen['approval_id']}/resolve",
                    json={"decision": "approved" if parts[0] == "/approve" else "denied"},
                )
                self._approval(result["approval"])
            elif text == "/cancel":
                pending = [
                    item
                    for item in self._messages.values()
                    if item.get("sender") == "user"
                    and item.get("delivery_status") in {"queued", "running"}
                ]
                if len(pending) != 1 or not pending[0].get("command_id"):
                    raise ValueError("没有唯一的运行中任务，无法取消。")
                await self.conversation.request(
                    "POST", f"commands/{pending[0]['command_id']}/cancel"
                )
            elif parts[0] == "/model" and len(parts) == 2:
                self._model = parts[1]
                self._refresh_composer()
                self._write("model", f"后续请求模型：{self._model}")
            elif parts[0] == "/mode" and len(parts) == 2 and parts[1] in {"execute", "qa", "plan"}:
                self._mode = parts[1]
                self._refresh_composer()
                self._write("mode", f"后续请求模式：{self._mode}")
            elif text.startswith("/"):
                raise ValueError("此接入模式不支持该命令，输入 /help 查看可用命令。")
            else:
                result = await self.conversation.request(
                    "POST",
                    "messages",
                    json={
                        "content": text,
                        "client_message_id": f"local_view_{uuid.uuid4().hex}",
                        "model": self._model,
                        "mode": self._mode,
                    },
                )
                self._message(result["message"])
        except (httpx.HTTPError, RemoteAgentError, OfficialAccountError, ValueError) as exc:
            self._write("action-error", f"操作未确认成功：{exc}。不会自动重发，请先查看对话。")
        finally:
            self._sending = False
