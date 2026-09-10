"""Textual client for the workspace resident Web runtime.

This deliberately contains only presentation and transport code.  The
resident service owns sessions and AgentLoop instances, so opening this TUI
cannot create a second executor for the same workspace.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

import httpx
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Static


class AttachedChatApp(App[None]):
    """Small TUI that attaches to the daemon-backed session API."""

    CSS = """
    Screen { layout: vertical; }
    #transcript { height: 1fr; padding: 1 2; scrollbar-size: 1 1; }
    #composer { height: auto; padding: 0 2 1 2; }
    #status { height: 1; padding: 0 2; color: $text-muted; }
    .message { height: auto; margin: 0 0 1 0; }
    .user { color: $accent; }
    .assistant { color: $text; }
    .system { color: $warning; }
    """

    BINDINGS = [("ctrl+q", "quit", "退出")]

    def __init__(self, base_url: str, token: str, session_id: str | None = None) -> None:
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session_id = session_id
        self.run_id: str | None = None
        self._event_cursor = 0
        self._assistant_line: Static | None = None
        self._assistant_text = ""
        self._booted = False
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            cookies={"aero_access": token},
            timeout=httpx.Timeout(35.0, connect=3.0),
            trust_env=False,
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="transcript")
        yield Static("正在连接本机常驻服务…", id="status")
        with Vertical(id="composer"):
            yield Input(placeholder="输入消息，Enter 发送；/approve、/deny、/cancel 控制当前任务")
        yield Footer()

    async def on_mount(self) -> None:
        self.run_worker(self._bootstrap(), exclusive=True)

    async def on_unmount(self) -> None:
        await self._http.aclose()

    def _write(self, text: str, role: str = "system") -> None:
        self.query_one("#transcript", VerticalScroll).mount(
            Static(text, classes=f"message {role}")
        )
        self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    async def _bootstrap(self) -> None:
        try:
            response = await self._http.get("/api/v1/sessions")
            response.raise_for_status()
            sessions = response.json().get("sessions", [])
            if self.session_id is None:
                self.session_id = sessions[0]["id"] if sessions else None
            if self.session_id is None:
                response = await self._http.post("/api/v1/sessions")
                response.raise_for_status()
                self.session_id = response.json()["id"]
            response = await self._http.get(f"/api/v1/sessions/{self.session_id}")
            response.raise_for_status()
            for message in response.json().get("messages", []):
                role = message.get("role")
                if role in {"user", "assistant"}:
                    self._write(str(message.get("content", "")), role)
            active = response.json().get("active_runs", [])
            if active:
                self.run_id = active[-1]
                self._write("已接管运行中的任务。", "system")
                await self._watch_run(self.run_id)
            else:
                self._status(f"已连接 · 会话 {self.session_id}")
            self._booted = True
            self.query_one(Input).focus()
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            self._status("连接失败")
            self._write(f"无法连接本机常驻服务：{exc}", "system")

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text or not self._booted:
            return
        if text in {"/approve", "/deny"}:
            self.run_worker(self._confirm(text[1:]), exclusive=True)
            return
        if text == "/cancel":
            self.run_worker(self._cancel(), exclusive=True)
            return
        if self.run_id is not None:
            self._write("当前会话已有运行中的任务，请等待完成或输入 /cancel。", "system")
            return
        self._write(text, "user")
        self.run_worker(self._start(text), exclusive=True)

    async def _start(self, text: str) -> None:
        assert self.session_id is not None
        try:
            response = await self._http.post(
                f"/api/v1/sessions/{self.session_id}/runs", json={"prompt": text}
            )
            response.raise_for_status()
            self.run_id = response.json()["run_id"]
            self._event_cursor = 0
            await self._watch_run(self.run_id)
        except httpx.HTTPError as exc:
            self._write(f"启动任务失败：{exc}", "system")
            self.run_id = None

    async def _watch_run(self, run_id: str) -> None:
        assert self.session_id is not None
        self._assistant_line = None
        self._assistant_text = ""
        self._status("任务运行中…")
        try:
            headers = {"Accept": "text/event-stream"}
            async with self._http.stream(
                "GET", f"/api/v1/runs/{run_id}/events",
                params={"session_id": self.session_id, "after": self._event_cursor},
                headers=headers,
            ) as response:
                response.raise_for_status()
                event_name = "message"
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif not line and data_lines:
                        with suppress(json.JSONDecodeError):
                            payload = json.loads("\n".join(data_lines))
                            self._consume_event(event_name, payload)
                        event_name, data_lines = "message", []
        except httpx.HTTPError as exc:
            self._write(f"任务事件连接中断：{exc}", "system")
        finally:
            with suppress(httpx.HTTPError):
                status = await self._http.get(
                    f"/api/v1/runs/{run_id}", params={"session_id": self.session_id}
                )
                if status.is_success:
                    state = status.json().get("state", "unknown")
                    self._status(f"任务状态：{state}")
            self.run_id = None
            self._assistant_line = None

    def _consume_event(self, event_name: str, payload: dict[str, Any]) -> None:
        self._event_cursor = max(self._event_cursor, int(payload.get("id", 0)))
        data = payload.get("data", {})
        if event_name == "assistant_delta":
            if self._assistant_line is None:
                self._assistant_line = Static("", classes="message assistant")
                self.query_one("#transcript", VerticalScroll).mount(self._assistant_line)
            self._assistant_text += str(data.get("text", ""))
            self._assistant_line.update(self._assistant_text)
        elif event_name == "confirmation_required":
            self._write(
                f"需要确认：{data.get('message') or data.get('tool') or '允许此操作吗？'}\n"
                "输入 /approve 批准，/deny 拒绝。",
                "system",
            )
        elif event_name == "secret_required":
            self._write("需要输入敏感信息，请改用本机 Web 界面完成。", "system")
        elif event_name == "error":
            self._write(str(data.get("message", "任务失败")), "system")
        elif event_name == "run_state":
            self._status(f"任务状态：{data.get('state', 'running')}")

    async def _confirm(self, choice: str) -> None:
        if self.run_id is None or self.session_id is None:
            self._write("当前没有等待确认的任务。", "system")
            return
        response = await self._http.post(
            f"/api/v1/runs/{self.run_id}/confirmation",
            params={"session_id": self.session_id},
            json={"choice": "allow" if choice == "approve" else "deny"},
        )
        if response.is_error:
            self._write("确认操作失败，请查看 Web 界面中的任务状态。", "system")

    async def _cancel(self) -> None:
        if self.run_id is None or self.session_id is None:
            self._write("当前没有运行中的任务。", "system")
            return
        response = await self._http.delete(
            f"/api/v1/runs/{self.run_id}", params={"session_id": self.session_id}
        )
        self._status("正在取消任务…" if response.is_success else "取消任务失败")
