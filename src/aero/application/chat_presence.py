"""Cloud presence for a foreground TUI conversation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from aero.application.chat_images import ImagePublisher, describe_images
from aero.application.conversation_history import sync_error_reason, visible_history
from aero.core.official_account import (
    OfficialAccountError,
    OfficialAccountSession,
    OfficialLoginRequiredError,
)


class ChatPresence:
    def __init__(
        self, *, session_id: Callable[[], str], title: Callable[[], str],
        on_title: Callable[[str], None], on_command: Callable[[str], Awaitable[str]],
        on_login_required: Callable[[], Awaitable[None]],
        on_quota_full: Callable[[], None],
        on_sync_error: Callable[[str], None] | None = None,
        on_taken_over: Callable[[], None] | None = None,
        export_context: Callable[[], dict | None] | None = None,
        restore_context: Callable[[dict], None] | None = None,
        is_idle: Callable[[], bool] | None = None,
        image_roots: Callable[[], list[Path]] | None = None,
    ) -> None:
        self.session = OfficialAccountSession()
        self.session_id = session_id
        self.title = title
        self.on_title = on_title
        self.on_command = on_command
        self.on_login_required = on_login_required
        self.on_quota_full = on_quota_full
        self.on_sync_error = on_sync_error or (lambda _message: None)
        self.on_taken_over = on_taken_over or (lambda: None)
        self.export_context = export_context or (lambda: None)
        self.restore_context = restore_context or (lambda _payload: None)
        self.is_idle = is_idle or (lambda: True)
        self.image_roots = image_roots or (lambda: [])
        self._image_publisher: ImagePublisher | None = None
        self._image_task: asyncio.Task | None = None
        self.agent_id = ""
        self.token = ""
        self._active_session_id = ""
        self._context_version = 0
        self._context_hash = ""
        self.context_sync_enabled = False
        self._sync_blocked_version: int | None = None
        self._pending_compactions: list[dict] = []
        self._pending_path: Path | None = None
        self._pending_messages: list[dict] = []
        self._messages_path: Path | None = None
        self._message_flush_lock = asyncio.Lock()
        self._message_error_reported = False
        self._running = True
        self._prompted = False
        self._blocked_session_id = ""
        self._http = httpx.AsyncClient(base_url=self.session.base_url, timeout=35)
        self._command_task: asyncio.Task | None = None

    async def close(self) -> None:
        self._running = False
        if self._image_task is not None:
            self._image_task.cancel()
            await asyncio.gather(self._image_task, return_exceptions=True)
        if self._command_task is not None:
            self._command_task.cancel()
            await asyncio.gather(self._command_task, return_exceptions=True)
        await self._exit()
        await self._http.aclose()
        await self.session.close()

    async def _exit(self) -> None:
        if self.agent_id:
            try:
                await self.session.request("POST", f"/v1/agents/{self.agent_id}/chat-exit")
            except (httpx.HTTPError, OfficialLoginRequiredError):
                pass
            self.agent_id = ""
            self.token = ""
            self._active_session_id = ""
            self._pending_path = None
            self._pending_compactions = []
            self._messages_path = None
            self._pending_messages = []
            self._image_publisher = None

    async def run(self) -> None:
        self._image_task = asyncio.create_task(self._image_loop())
        while self._running:
            if not self.session.data.is_logged_in:
                await self._exit()
                await asyncio.sleep(3)
                continue
            try:
                await self.session.access_token()
                self._prompted = False
                current = self.session_id()
                if self._blocked_session_id == current:
                    await asyncio.sleep(3)
                    continue
                self._blocked_session_id = ""
                if not self.agent_id:
                    self._select_message_queue(current)
                    context = self.export_context() or {}
                    has_user_message = any(
                        item.get("type") == "user_message" for item in self._pending_messages
                    ) or any(
                        item.get("role") == "user" for item in context.get("messages", [])
                    )
                    if not has_user_message:
                        # Opening an empty TUI session must not create a cloud conversation.
                        await asyncio.sleep(1)
                        continue
                    response = await self.session.request("POST", "/v1/agents/chats", json={
                        "session_id": current, "title": self.title(),
                    })
                    if response.status_code == 409:
                        detail = str(response.json().get("detail") or "")
                        if "并发" not in detail:
                            self.on_quota_full()
                        await asyncio.sleep(1 if "并发" in detail else 15)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    self.agent_id, self.token = data["agent_id"], data["token"]
                    self._context_version = int(data.get("context_version") or 0)
                    self._context_hash = ""
                    session_key = hashlib.sha256(current.encode()).hexdigest()
                    self._pending_path = Path.home() / ".aero" / "context-events" / f"{session_key}.json"
                    self._load_pending()
                    self._select_message_queue(current)
                    self.context_sync_enabled = bool(data.get("context_sync_enabled"))
                    await self._restore_context()
                elif current != self._active_session_id:
                    await self._exit()
                    continue
                self._active_session_id = current
                heartbeat = await self.session.request(
                    "POST", f"/v1/agents/{self.agent_id}/chat-heartbeat",
                    json={"title": self.title()},
                )
                if heartbeat.status_code == 404:
                    self._blocked_session_id = current
                    self.on_taken_over()
                    await self._exit()
                    continue
                if heartbeat.status_code == 409:
                    # Explicit remote stop only blocks this conversation; a new
                    # local conversation may register as a new cloud agent.
                    self._blocked_session_id = current
                    await self._exit()
                    continue
                heartbeat.raise_for_status()
                data = heartbeat.json()
                was_enabled = self.context_sync_enabled
                self.context_sync_enabled = bool(data.get("context_sync_enabled"))
                if (self._sync_blocked_version is not None
                        and int(data.get("context_version") or 0) != self._sync_blocked_version):
                    self._sync_blocked_version = None
                if not was_enabled and self.context_sync_enabled:
                    self._context_version = int(data.get("context_version") or 0)
                if self.context_sync_enabled and not was_enabled:
                    self._context_hash = ""
                    await self._restore_context()
                if data.get("title_source") == "manual" and data.get("display_name") != self.title():
                    self.on_title(data["display_name"])
                manual_request_id = str(data.get("manual_sync_request_id") or "")
                if self.is_idle() and (manual_request_id or self._sync_blocked_version is None):
                    if manual_request_id:
                        try:
                            await self._sync_context(manual_request_id=manual_request_id)
                        except httpx.HTTPStatusError as exc:
                            if 400 <= exc.response.status_code < 500:
                                await self._fail_manual_sync(
                                    manual_request_id, "服务端拒绝了本次上下文同步"
                                )
                            else:
                                raise
                    elif self.context_sync_enabled:
                        await self._sync_context()
                await self._flush_messages()
                await self._poll_once()
            except OfficialLoginRequiredError:
                await self._exit()
                if not self._prompted:
                    self._prompted = True
                    await self.on_login_required()
            except (OfficialAccountError, httpx.HTTPError, ValueError, KeyError):
                await asyncio.sleep(3)

    async def _restore_context(self) -> None:
        response = await self._http.get(
            f"/v1/agents/{self.agent_id}/context",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if response.status_code == 404:
            return
        response.raise_for_status()
        data = response.json()
        self._context_version = int(data["version"])
        local = self.export_context()
        if self.is_idle() and (not local or not local.get("messages")):
            self.restore_context(data["context"])

    async def _sync_context(self, *, manual_request_id: str = "") -> None:
        if not self.context_sync_enabled and not manual_request_id:
            return
        if manual_request_id:
            await self._flush_messages()
            if self._pending_messages or not self.is_idle():
                return
            payload = self.export_context()
            if not payload:
                await self._fail_manual_sync(manual_request_id, "本地尚无可同步的会话")
                return
            response = await self._http.post(
                f"/v1/agents/{self.agent_id}/history",
                json={"manual_request_id": manual_request_id,
                      "messages": visible_history(payload)},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=120,
            )
            if response.status_code >= 400:
                await self._fail_manual_sync(
                    manual_request_id, sync_error_reason(
                        response, "聊天记录同步失败，请确认 Relay 和客户端均已更新"
                    )
                )
                return
        while self._pending_compactions:
            event = self._pending_compactions[0]
            response = await self._http.post(
                f"/v1/agents/{self.agent_id}/context/compact",
                json={**event, "expected_version": self._context_version,
                      "manual_request_id": manual_request_id},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            if response.status_code == 409:
                reason = sync_error_reason(response, "压缩归档同步被拒绝，请稍后重试")
                self.on_sync_error(reason)
                self._sync_blocked_version = self._context_version
                if manual_request_id:
                    await self._fail_manual_sync(manual_request_id, reason)
                return
            response.raise_for_status()
            result = response.json()
            if not result.get("sync_enabled", True) and not manual_request_id:
                self.context_sync_enabled = False
                return
            self._context_version = int(result["version"])
            self._pending_compactions.pop(0)
            self._save_pending()
        payload = self.export_context()
        if not payload:
            if manual_request_id:
                await self._fail_manual_sync(manual_request_id, "本地尚无可同步的会话")
            return
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if digest == self._context_hash and not manual_request_id:
            return
        response = await self._http.put(
            f"/v1/agents/{self.agent_id}/context",
            json={"context": payload, "expected_version": self._context_version,
                  "manual_request_id": manual_request_id},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if response.status_code == 409:
            reason = sync_error_reason(response, "上下文同步被拒绝，请稍后重试")
            self.on_sync_error(reason)
            self._sync_blocked_version = self._context_version
            if manual_request_id:
                await self._fail_manual_sync(manual_request_id, reason)
            return
        response.raise_for_status()
        result = response.json()
        self._context_version = int(result["version"])
        if result.get("applied", True):
            self._context_hash = digest

    async def _fail_manual_sync(self, request_id: str, reason: str) -> None:
        response = await self._http.post(
            f"/v1/agents/{self.agent_id}/context/manual-sync/{request_id}/fail",
            json={"reason": reason},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        response.raise_for_status()

    async def set_context_sync(self, enabled: bool) -> bool:
        if not self.agent_id:
            return False
        response = await self.session.request(
            "PATCH", f"/v1/agents/{self.agent_id}/context-sync",
            json={"enabled": enabled},
        )
        response.raise_for_status()
        self.context_sync_enabled = bool(response.json()["enabled"])
        self._context_version = int(response.json()["version"])
        self._sync_blocked_version = None
        self._context_hash = ""
        return True

    def queue_compaction(self, before: dict, after: dict) -> None:
        session_key = hashlib.sha256(self.session_id().encode()).hexdigest()
        target = Path.home() / ".aero" / "context-events" / f"{session_key}.json"
        if self._pending_path != target:
            self._pending_path = target
            self._load_pending()
        self._pending_compactions.append({
            "event_id": uuid.uuid4().hex,
            "before": before, "after": after,
        })
        self._save_pending()

    def _load_pending(self) -> None:
        self._pending_compactions = []
        if self._pending_path and self._pending_path.exists():
            try:
                self._pending_compactions = json.loads(self._pending_path.read_text())
            except (OSError, ValueError):
                self._pending_compactions = []

    def _save_pending(self) -> None:
        if self._pending_path is None:
            return
        from aero.core.daemon import private_write
        self._pending_path.parent.mkdir(parents=True, exist_ok=True)
        private_write(self._pending_path, json.dumps(self._pending_compactions, ensure_ascii=False))

    def _select_message_queue(self, session_id: str) -> None:
        user_id = self.session.data.user_id
        session_key = hashlib.sha256(f"{user_id}:{session_id}".encode()).hexdigest()
        path = Path.home() / ".aero" / "context-events" / f"{session_key}.messages.json"
        if self._messages_path == path:
            return
        self._messages_path = path
        if hasattr(self, "image_roots"):
            self._image_publisher = ImagePublisher(
                self._image_request, self.image_roots, path.with_suffix(".images.json")
            )
        try:
            loaded = json.loads(path.read_text()) if path.exists() else []
            self._pending_messages = loaded if isinstance(loaded, list) else []
        except (OSError, ValueError):
            self._pending_messages = []

    def _save_messages(self) -> None:
        if self._messages_path is None:
            return
        from aero.core.daemon import private_write
        self._messages_path.parent.mkdir(parents=True, exist_ok=True)
        private_write(self._messages_path, json.dumps(self._pending_messages, ensure_ascii=False))

    def queue_local_message(
        self, role: str, content: str, *, local_turn_id: str = ""
    ) -> str:
        if (role not in {"user", "assistant", "summary"} or not content.strip()
                or not self.session.data.is_logged_in or not self.session.data.user_id):
            return ""
        current = self.session_id()
        appended = False
        try:
            self._select_message_queue(current)
            self._pending_messages.append({
                "type": {"user": "user_message", "assistant": "message", "summary": "summary"}[role],
                "content": content,
                "client_event_id": uuid.uuid4().hex,
            })
            appended = True
            event = self._pending_messages[-1]
            if role == "user":
                local_turn_id = event["client_event_id"]
            if local_turn_id:
                event["local_turn_id"] = local_turn_id
            if role == "assistant":
                event = self._pending_messages[-1]
                event["images"] = self._images_for(content, event["client_event_id"])
            self._save_messages()
        except (OSError, RuntimeError):
            if appended:
                self._pending_messages.pop()
            self.on_sync_error("客户端会话消息未能保存到同步队列，请检查本地文件权限。")
            return ""
        if self.agent_id and self._active_session_id == current:
            asyncio.create_task(self._flush_messages())
        return local_turn_id

    def finish_local_turn(self, local_turn_id: str, status: str) -> None:
        if not local_turn_id:
            return
        event = {"type": "local_turn_status", "local_turn_id": local_turn_id,
                 "status": status, "client_event_id": uuid.uuid4().hex}
        self._pending_messages.append(event)
        try:
            self._save_messages()
        except OSError:
            self.on_sync_error("客户端处理状态未能保存，请检查本地文件权限。")
        if self.agent_id:
            asyncio.create_task(self._flush_messages())

    async def _flush_messages(self) -> None:
        async with self._message_flush_lock:
            while self.agent_id and self._messages_path and self._pending_messages:
                session_id = self.session_id()
                if session_id != self._active_session_id:
                    return
                path = self._messages_path
                event = self._pending_messages[0]
                try:
                    response = await self._http.post(
                        f"/v1/agents/{self.agent_id}/events",
                        json=event,
                        headers={"Authorization": f"Bearer {self.token}"},
                    )
                except httpx.HTTPError:
                    return
                if response.status_code >= 400:
                    if response.status_code not in {401, 410} and not self._message_error_reported:
                        self._message_error_reported = True
                        self.on_sync_error("客户端会话消息未能同步到网页，请检查 Relay 版本或连接。")
                    return
                self._message_error_reported = False
                if path != self._messages_path or not self._pending_messages:
                    return
                if self._pending_messages[0]["client_event_id"] != event["client_event_id"]:
                    return
                self._pending_messages.pop(0)
                try:
                    self._save_messages()
                except (OSError, RuntimeError):
                    self.on_sync_error("客户端会话同步队列未能更新，请检查本地文件权限。")
                    return

    async def _poll_once(self) -> None:
        if not self.agent_id:
            return
        if self._command_task is not None:
            if self._command_task.done():
                self._command_task = None
            else:
                await asyncio.sleep(1)
                return
        response = await self._http.get(
            f"/v1/agents/{self.agent_id}/commands/next",
            params={"wait_seconds": 10},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if response.status_code == 204:
            return
        if response.status_code in {401, 410}:
            # A revoked login or remotely stopped chat must not keep polling.
            if response.status_code == 410:
                self._blocked_session_id = self._active_session_id
            await self._exit()
            if response.status_code == 401:
                try:
                    await self.session.access_token(force_refresh=True)
                except OfficialLoginRequiredError:
                    if not self._prompted:
                        self._prompted = True
                        await self.on_login_required()
                else:
                    self._blocked_session_id = self._active_session_id
            return
        response.raise_for_status()
        command = response.json().get("command")
        if not command:
            return
        if command.get("session_id") and command["session_id"] != self._active_session_id:
            raise ValueError("cloud_command_session_mismatch")
        self._command_task = asyncio.create_task(self._execute_command(command))

    async def _execute_command(self, command: dict) -> None:
        command_id = command["command_id"]
        headers = {"Authorization": f"Bearer {self.token}"}
        renewer: asyncio.Task | None = None
        try:
            await self._http.post(
                f"/v1/agents/{self.agent_id}/commands/{command_id}/status",
                json={"status": "running"}, headers=headers,
            )
            renewer = asyncio.create_task(self._renew_command(command_id, headers))
            answer = await self.on_command(str(command.get("content") or ""))
            event_id = uuid.uuid4().hex
            response = await self._http.post(
                f"/v1/agents/{self.agent_id}/events",
                json={"type": "message", "content": answer or "已执行。",
                      "in_reply_to": command_id, "client_event_id": event_id,
                      "images": self._images_for(answer or "", event_id)},
                headers=headers,
            )
            response.raise_for_status()
        except Exception:
            await self._http.post(
                f"/v1/agents/{self.agent_id}/commands/{command_id}/status",
                json={"status": "failed"}, headers=headers,
            )
        finally:
            if renewer is not None:
                renewer.cancel()
                await asyncio.gather(renewer, return_exceptions=True)

    def _images_for(self, content: str, event_id: str) -> list[dict]:
        publisher = getattr(self, "_image_publisher", None)
        if publisher is not None:
            try:
                return publisher.describe(content, event_id)
            except (OSError, RuntimeError):
                self.on_sync_error("图片同步路径未能保存，重试时会使用当前项目目录。")
        return describe_images(content, event_id)

    async def _image_request(self, method: str, path: str, **kwargs) -> dict:
        if not self.agent_id:
            raise RuntimeError("chat_offline")
        response = await self._http.request(
            method, f"/v1/agents/{self.agent_id}/{path}",
            headers={"Authorization": f"Bearer {self.token}"}, **kwargs,
        )
        response.raise_for_status()
        return response.json()

    async def _image_loop(self) -> None:
        while self._running:
            publisher = self._image_publisher
            if publisher and self.agent_id:
                try:
                    await publisher.flush()
                except (httpx.HTTPError, OSError, ValueError, RuntimeError):
                    pass
            await asyncio.sleep(5)

    async def _renew_command(self, command_id: str, headers: dict[str, str]) -> None:
        while True:
            await asyncio.sleep(30)
            response = await self._http.post(
                f"/v1/agents/{self.agent_id}/commands/{command_id}/status",
                json={"status": "running"}, headers=headers,
            )
            response.raise_for_status()
