"""Fenced Relay worker hosting the *same* LocalSession used by the local UI.

Heartbeats and account checks never share the command/event polling task. A lost
lease cancels local work and records interruption before reconnect; it is never
interpreted as permission to replay an uncertain operation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from aero.application.chat_images import ImagePublisher, describe_images, workspace_roots
from aero.application.remote_secret import SecretKey, associated_data
from aero.core.daemon import private_write
from aero.core.official_account import OfficialLoginRequiredError
from aero.toolbox.secret_input import get_credential_spec

if TYPE_CHECKING:
    from aero.application.local_session import LocalSession


class LeaseLostError(RuntimeError):
    """Ownership is uncertain; stop side effects before any reconnect."""


LeaseLost = LeaseLostError


class AccountRevokedError(LeaseLostError):
    """The cloud login or Agent credential was revoked; do not reconnect."""


class RelayRuntimeTransport:
    """Small explicit adapter; injected HTTP client remains caller-owned."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        agent_id: str,
        token: str,
        account_access_token: Callable[[], Awaitable[str]] | None = None,
    ):
        self.http = http
        self.agent_id = agent_id
        self._token = token
        self._account_access_token = account_access_token
        self.device_id = ""
        self.lease_token: int | None = None

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if path == "runtime/acquire" and self._account_access_token:
            try:
                headers["X-Official-Access-Token"] = await self._account_access_token()
            except OfficialLoginRequiredError as exc:
                raise AccountRevokedError("客户端登录已失效，请重新登录。") from exc
        if self.lease_token is not None:
            headers.update(
                {"X-Agent-Device-Id": self.device_id, "X-Agent-Lease-Token": str(self.lease_token)}
            )
        response = await self.http.request(
            method, f"/v1/agents/{self.agent_id}/{path}", headers=headers, **kwargs
        )
        if response.status_code == 401:
            raise AccountRevokedError("客户端登录或智能体授权已下线，请重新登录。")
        image_path = path.startswith("messages/") and "/images/" in path
        if (
            response.status_code in {410, 423}
            or (response.status_code == 403 and not image_path)
            or (
                response.status_code == 409
                and path not in {"context", "context/compact", "history"}
                and not image_path
            )
        ):
            raise LeaseLost(f"runtime_rejected_{response.status_code}")
        response.raise_for_status()
        return {} if response.status_code == 204 else response.json()


class CloudAgentRuntime:
    """Host one bound conversation; foreground clients attach to ``session``.

    ``account_identity`` must read the *current persisted* official user ID, not a
    cached login object. Empty/different identities stop execution immediately.
    ``state_dir`` holds only a private command journal, never prompts or secrets.
    """

    def __init__(
        self,
        session: LocalSession,
        transport: RelayRuntimeTransport,
        *,
        device_id: str,
        project_id: str,
        account_id: str,
        account_identity: Callable[[], str],
        state_dir: Path,
    ):
        self.session = session
        self.transport = transport
        self.device_id = device_id
        self.project_id = project_id
        self.account_id = account_id
        self.account_identity = account_identity
        self.state_dir = state_dir
        key = hashlib.sha256(transport.agent_id.encode()).hexdigest()
        self._journal_path = state_dir / f"{key}.json"
        self._context_events_path = state_dir / f"{key}.context-events.json"
        self.images = ImagePublisher(
            self._image_request,
            lambda: workspace_roots(session.project_dir),
            state_dir / f"{key}.images.json",
        )
        self._conversation_events_path = state_dir / f"{key}.messages.json"
        self._conversation_flush_lock = asyncio.Lock()
        try:
            self._conversation_events = json.loads(self._conversation_events_path.read_text())
        except (OSError, ValueError):
            self._conversation_events = []
        self._pending_compactions: list[dict] = []
        if self._context_events_path.exists():
            try:
                self._pending_compactions = json.loads(self._context_events_path.read_text())
            except (OSError, ValueError):
                pass
        self._journal: dict[str, str] = {}
        if self._journal_path.exists():
            self._journal = json.loads(self._journal_path.read_text())
        self.state = "stopped"
        self.active_run_id: str | None = None
        self.command_id: str | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._deadline = 0.0
        self._heartbeat_seconds = 15.0
        self.context_version = 0
        self.context_sync_enabled = False
        self.context_sync_error = ""
        self._context_hash = ""
        self._foreground_execution: str | None = None
        self.session.on_compaction = self._queue_compaction

    async def _image_request(self, method: str, path: str, **kwargs) -> dict:
        self._assert_lease()
        return await self.transport.request(method, path, **kwargs)

    async def _sync_images(self) -> None:
        while not self._stop.is_set():
            try:
                self._assert_lease()
                await self._flush_conversation_events()
                await self.images.flush()
            except (httpx.HTTPError, OSError, ValueError, RuntimeError):
                pass
            await asyncio.sleep(5)

    async def _flush_conversation_events(self) -> None:
        async with self._conversation_flush_lock:
            while self._conversation_events:
                await self.transport.request("POST", "events", json=self._conversation_events[0])
                self._conversation_events.pop(0)
                private_write(self._conversation_events_path, json.dumps(self._conversation_events))

    def _queue_foreground_messages(self, run_id: str, status: str | None = None) -> None:
        state = self.session.run_status(run_id).get("state")
        status = status or {"completed": "completed", "cancelled": "canceled"}.get(state, "failed")
        turn_id = hashlib.sha256(f"{run_id}:0".encode()).hexdigest()
        from aero.application.conversation_history import visible_history

        saved = self.session.session_manager.export_portable_context(self.session.id)
        messages = visible_history(saved) if saved else self.session.messages_view()
        start = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "user"),
            len(messages),
        )
        for index, message in enumerate(messages[start:]):
            if index == 0 or state != "completed":
                continue  # The user message was queued when execution started.
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            event_id = hashlib.sha256(f"{run_id}:{index}".encode()).hexdigest()
            role = "user_message" if message["role"] == "user" else "message"
            attachments = self.images.describe(content, event_id) if role == "message" else []
            self._conversation_events.append(
                {
                    "type": role,
                    "content": content,
                    "client_event_id": event_id,
                    "images": attachments,
                    "local_turn_id": turn_id,
                }
            )
        self._conversation_events.append(
            {
                "type": "local_turn_status",
                "local_turn_id": turn_id,
                "status": status,
                "client_event_id": f"end-{run_id}",
            }
        )
        private_write(self._conversation_events_path, json.dumps(self._conversation_events))

    async def _queue_compaction(self, before: dict, after: dict) -> None:
        self._pending_compactions.append(
            {"event_id": uuid.uuid4().hex, "before": before, "after": after}
        )
        private_write(
            self._context_events_path, json.dumps(self._pending_compactions, ensure_ascii=False)
        )

    def status(self) -> dict[str, Any]:
        return {
            "agent_id": self.transport.agent_id,
            "session_id": self.session.id,
            "project_id": self.project_id,
            "state": self.state,
            "active_run_id": self.active_run_id,
            "command_id": self.command_id,
            "context_sync_enabled": self.context_sync_enabled,
            "context_sync_error": self.context_sync_error,
        }

    async def start(self) -> dict[str, Any]:
        if self._task and not self._task.done():
            return self.status()
        self._stop.clear()
        self.state = "connecting"
        self._task = asyncio.create_task(self.run())
        return self.status()

    def _identity_check(self) -> None:
        if not self.account_id or self.account_identity() != self.account_id:
            self.state = "account_changed"
            self._stop.set()
            raise LeaseLost("account_changed")

    def _record(self, command: str, state: str) -> None:
        self._journal[command] = state
        private_write(self._journal_path, json.dumps(self._journal))

    def _assert_lease(self) -> None:
        self._identity_check()
        if self.transport.lease_token is None or time.monotonic() >= self._deadline:
            raise LeaseLost("lease_expired")

    async def _acquire(self) -> None:
        self._identity_check()
        self.transport.device_id = self.device_id
        self.transport.lease_token = None
        started = time.monotonic()
        result = await self.transport.request(
            "POST",
            "runtime/acquire",
            json={
                "device_id": self.device_id,
                "project_id": self.project_id,
                "session_id": self.session.id,
            },
        )
        if result["session_id"] != self.session.id:
            raise LeaseLost("session_binding_mismatch")
        self.transport.lease_token = int(result["lease_token"])
        self._deadline = started + min(float(result.get("ttl_seconds", 120)), 120)
        self._heartbeat_seconds = min(float(result.get("heartbeat_seconds", 15)), 15)
        self.session.execution_guard = self.project_execution
        self.state = "online"
        self.context_sync_enabled = bool(result.get("context_sync_enabled"))
        self.context_version = int(result.get("context_version") or 0)
        try:
            saved = await self.transport.request("GET", "context")
            self.context_version = int(saved["version"])
            local = self.session.session_manager.export_portable_context(self.session.id)
            if local is None or not local.get("messages"):
                self.session.restore_portable_context(saved["context"])
            self.context_sync_error = ""
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                self.context_sync_error = f"云端上下文读取失败（{exc.response.status_code}）"
        except (ValueError, KeyError) as exc:
            self.context_sync_error = f"云端上下文格式错误：{type(exc).__name__}"

    async def _sync_context(self, *, manual_request_id: str = "") -> None:
        from aero.application.conversation_history import sync_error_reason, visible_history

        if not self.context_sync_enabled and not manual_request_id:
            return
        if (
            self.session.metadata().get("active_runs")
            or self._foreground_execution
            or self.command_id
        ):
            return
        if manual_request_id:
            if self._foreground_execution or self.command_id:
                return
            try:
                await self._flush_conversation_events()
            except (httpx.HTTPError, LeaseLost):
                return
            if (
                self.session.metadata().get("active_runs")
                or self._foreground_execution
                or self.command_id
            ):
                return
            payload = self.session.session_manager.export_portable_context(self.session.id)
            if payload is None:
                await self._fail_manual_sync(manual_request_id, "本地尚无可同步的会话")
                return
            try:
                await self.transport.request(
                    "POST",
                    "history",
                    json={
                        "manual_request_id": manual_request_id,
                        "messages": visible_history(payload),
                    },
                    timeout=120,
                )
            except (httpx.HTTPError, LeaseLost) as exc:
                await self._fail_manual_sync(
                    manual_request_id,
                    sync_error_reason(
                        exc.response, "聊天记录同步失败，请确认 Relay 和客户端均已更新"
                    )
                    if isinstance(exc, httpx.HTTPStatusError)
                    else "聊天记录上传失败，请稍后重试",
                )
                return
        try:
            while self._pending_compactions:
                result = await self.transport.request(
                    "POST",
                    "context/compact",
                    json={
                        **self._pending_compactions[0],
                        "expected_version": self.context_version,
                        "manual_request_id": manual_request_id,
                    },
                )
                if not result.get("sync_enabled", True) and not manual_request_id:
                    self.context_sync_enabled = False
                    return
                self.context_version = int(result["version"])
                self._pending_compactions.pop(0)
                private_write(
                    self._context_events_path,
                    json.dumps(self._pending_compactions, ensure_ascii=False),
                )
        except (httpx.HTTPError, LeaseLost) as exc:
            self.context_sync_error = f"云端上下文轮转失败：{type(exc).__name__}"
            if (
                manual_request_id
                and isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code < 500
            ):
                await self._fail_manual_sync(
                    manual_request_id, sync_error_reason(exc.response, "上下文归档同步失败")
                )
            return
        payload = self.session.session_manager.export_portable_context(self.session.id)
        if payload is None:
            if manual_request_id:
                await self._fail_manual_sync(manual_request_id, "本地尚无可同步的会话")
            return
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        try:
            result = await self.transport.request(
                "PUT",
                "context",
                json={
                    "context": payload,
                    "expected_version": self.context_version,
                    "manual_request_id": manual_request_id,
                    "reconcile": True,
                },
            )
            self.context_version = int(result["version"])
            merged = result.get("context")
            current = self.session.session_manager.export_portable_context(self.session.id)
            if (
                merged is not None
                and merged != payload
                and current == payload
                and not self.session.metadata().get("active_runs")
                and not self._foreground_execution
                and not self.command_id
            ):
                self.session.restore_portable_context(merged)
            if result.get("applied", True):
                self._context_hash = digest
            self.context_sync_error = ""
        except (httpx.HTTPError, LeaseLost) as exc:
            self.context_sync_error = f"云端上下文同步失败：{type(exc).__name__}"
            if (
                manual_request_id
                and isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code < 500
            ):
                await self._fail_manual_sync(
                    manual_request_id, sync_error_reason(exc.response, "上下文同步失败")
                )

    async def _fail_manual_sync(self, request_id: str, reason: str) -> None:
        try:
            await self.transport.request(
                "POST",
                f"context/manual-sync/{request_id}/fail",
                json={"reason": reason},
            )
        except (httpx.HTTPError, LeaseLost):
            pass

    async def _heartbeats(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self._heartbeat_seconds)
            self._assert_lease()
            started = time.monotonic()
            metadata = self.session.metadata()
            title = (
                metadata.get("name") if metadata.get("title_source") in {"auto", "manual"} else ""
            )
            result = await self.transport.request(
                "POST", "runtime/heartbeat", json={"title": title}
            )
            if bool(result.get("context_sync_enabled")) and not self.context_sync_enabled:
                self._context_hash = ""
                self.context_version = int(result.get("context_version") or 0)
            self.context_sync_enabled = bool(result.get("context_sync_enabled"))
            self._deadline = started + min(float(result.get("ttl_seconds", 120)), 120)
            if result.get("title_source") == "manual":
                cloud_title = str(result.get("display_name") or "").strip()
                if cloud_title and cloud_title != metadata.get("name"):
                    self.session.rename(cloud_title)
            if self.command_id in result.get("cancel_requested_command_ids", []):
                if self.active_run_id:
                    await self.session.cancel(self.active_run_id)
            if not self.session.metadata().get("active_runs"):
                await self._sync_context(
                    manual_request_id=str(result.get("manual_sync_request_id") or "")
                )

    async def _watch_identity(self) -> None:
        while not self._stop.is_set():
            self._assert_lease()
            await asyncio.sleep(0.5)

    @asynccontextmanager
    async def project_execution(self, run_id: str):
        """Used by LocalSession for foreground runs as well as cloud commands."""
        self._assert_lease()
        # Claiming the command already acquired the backend project slot.
        if self.command_id and run_id == self.active_run_id:
            yield
            return
        result = await self.transport.request(
            "POST", "runtime/project/acquire", json={"execution_id": run_id}
        )
        self._foreground_execution = run_id
        terminal_status = None
        try:
            async for event in self.session.events(run_id):
                if event.type == "run_started":
                    turn_id = hashlib.sha256(f"{run_id}:0".encode()).hexdigest()
                    self._conversation_events.append(
                        {
                            "type": "user_message",
                            "content": event.data["prompt"],
                            "client_event_id": turn_id,
                            "local_turn_id": turn_id,
                        }
                    )
                    with suppress(OSError):
                        private_write(
                            self._conversation_events_path, json.dumps(self._conversation_events)
                        )
                    break
            yield
        except asyncio.CancelledError:
            terminal_status = "canceled"
            raise
        except BaseException:
            terminal_status = "failed"
            raise
        finally:
            with suppress(OSError, ValueError, RuntimeError):
                self._queue_foreground_messages(run_id, terminal_status)
            self._foreground_execution = None
            with suppress(httpx.HTTPError, LeaseLost):
                await self.transport.request(
                    "POST",
                    "runtime/project/release",
                    json={
                        "execution_id": run_id,
                        "project_lease_token": result["project_lease_token"],
                    },
                )

    async def _approval(self, command_id: str, run_id: str, event: Any) -> None:
        # Only operation identifiers are cloud-visible, not tool arguments/secrets.
        request_id = f"{run_id}:{event.id}"[:64]
        await self.transport.request(
            "POST",
            f"commands/{command_id}/approvals",
            json={
                "request_id": request_id,
                "payload": {
                    "tool": str(event.data.get("tool", "operation"))[:120],
                    "message": "Local operation requires approval",
                    "run_id": run_id,
                },
            },
        )
        self.state = "waiting_confirmation"
        while not self._stop.is_set():
            self._assert_lease()
            result = await self.transport.request("GET", f"commands/{command_id}/approvals")
            if result.get("cancel_requested"):
                await self.session.cancel(run_id)
                return
            approval = next(
                (a for a in result.get("approvals", []) if a.get("request_id") == request_id), {}
            )
            if approval.get("status") in {"approved", "denied", "canceled"}:
                choice = "allow" if approval["status"] == "approved" else "deny"
                # LocalSession confirmation future may be installed after the event.
                for _ in range(100):
                    try:
                        self.session.confirm(run_id, choice)
                        self.state = "running"
                        return
                    except RuntimeError:
                        await asyncio.sleep(0.01)
                raise RuntimeError("confirmation_not_pending")
            await asyncio.sleep(1)

    async def _secret(self, command_id: str, run_id: str, event: Any) -> None:
        """Pause this run while a separately encrypted, authenticated inbox is serviced."""
        # A setup hint is not an actual pending input request. Only the provider
        # creates a request ID/future; the model still needs to call that tool.
        if not event.data.get("request_id") or not event.data.get("scope"):
            return
        local_id = str(event.data["request_id"])
        scope = str(event.data["scope"])
        spec = get_credential_spec(scope)
        if spec is None:
            await self.session.cancel(run_id)
            return
        self.state = "waiting_secret"
        description = {
            "scope": scope,
            "title": spec.title,
            "instructions": spec.instructions,
            "multiline": spec.multiline,
        }
        while not self._stop.is_set():
            request_id = uuid.uuid4().hex
            key = SecretKey()  # Never persisted; each format retry gets a new key and ID.
            path = f"commands/{command_id}/secrets/{request_id}"
            deadline = time.monotonic() + 900

            async def send(method: str, endpoint: str, **kwargs: Any) -> dict:
                while time.monotonic() < deadline and not self._stop.is_set():
                    self._assert_lease()
                    try:
                        return await self.transport.request(method, endpoint, **kwargs)
                    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                        if (
                            isinstance(exc, httpx.HTTPStatusError)
                            and exc.response.status_code < 500
                        ):
                            raise
                        await asyncio.sleep(1)
                raise TimeoutError("secret_request_expired")

            try:
                await send(
                    "POST",
                    f"commands/{command_id}/secrets",
                    json={
                        "request_id": request_id,
                        "public_key": key.public_key,
                        "description": description,
                    },
                )
                while time.monotonic() < deadline and not self._stop.is_set():
                    result = await send("POST", f"{path}/claim")
                    status = result["request"]["status"]
                    if result.get("cancel_requested") or status in {"canceled", "expired"}:
                        await self.session.cancel(run_id)
                        return
                    envelope = result.get("envelope")
                    if envelope:
                        secret = None
                        try:
                            secret = key.decrypt(
                                envelope,
                                associated_data(self.transport.agent_id, command_id, request_id),
                            )
                        except Exception:
                            pass  # Never expose plaintext or crypto errors to events/logs.
                        await send("POST", f"{path}/ack")
                        saved = secret is not None and self.session.submit_remote_secret(
                            local_id, scope, secret
                        )
                        secret = None
                        envelope = None
                        await send(
                            "POST",
                            f"{path}/result",
                            json={"status": "success" if saved else "failed"},
                        )
                        if saved:
                            self.state = "running"
                            return
                        break  # Invalid input: new inbox, original pending future stays alive.
                    await asyncio.sleep(1)
                else:
                    await send("POST", f"{path}/result", json={"status": "canceled"})
                    await self.session.cancel(run_id)
                    return
            except TimeoutError:
                await self.session.cancel(run_id)
                return

    async def _execute(self, command: dict[str, Any]) -> None:
        command_id = str(command["command_id"])
        if command_id in self._journal:
            # Even 'completed' could have lost its remote acknowledgment: no replay.
            await self.transport.request(
                "POST",
                f"commands/{command_id}/status",
                json={
                    "status": "interrupted",
                    "error": "local_command_already_attempted",
                },
            )
            return
        if command.get("session_id", self.session.id) != self.session.id:
            raise LeaseLost("command_session_mismatch")
        self._assert_lease()
        self._record(command_id, "started")  # durable BEFORE any side effect
        self.command_id = command_id
        self.state = "running"
        try:
            self.active_run_id = self.session.start_run(str(command.get("content", "")))
            response_parts: list[str] = []
            async for event in self.session.events(self.active_run_id):
                self._assert_lease()
                if event.type == "confirmation_required":
                    await self._approval(command_id, self.active_run_id, event)
                elif event.type in {"text", "assistant_delta"}:
                    response_parts.append(
                        getattr(event, "content", None) or event.data.get("text", "")
                    )
                elif event.type in {"status", "tool_progress"}:
                    content = getattr(event, "content", None) or event.data.get("text", "")
                    if not content:
                        continue
                    await self.transport.request(
                        "POST",
                        "events",
                        json={
                            "type": "progress",
                            "content": str(content)[:20_000],
                            "in_reply_to": command_id,
                            "client_event_id": f"progress-{uuid.uuid4().hex}",
                        },
                    )
                elif event.type == "secret_required":
                    await self._secret(command_id, self.active_run_id, event)
            state = self.session.run_status(self.active_run_id).get("state", "failed")
            final = {"completed": "completed", "cancelled": "canceled"}.get(state, "failed")
            self._record(command_id, final)
            if final == "completed":
                content = "".join(response_parts).strip() or "指令已处理，但没有可返回的文本结果。"
                event_id = f"result-{uuid.uuid4().hex}"
                try:
                    attachments = self.images.describe(content, event_id)
                except OSError:
                    attachments = describe_images(content, event_id)
                await self.transport.request(
                    "POST",
                    "events",
                    json={
                        "type": "message",
                        "content": content,
                        "images": attachments,
                        "in_reply_to": command_id,
                        "client_event_id": event_id,
                    },
                )
            elif final == "failed":
                await self.transport.request(
                    "POST",
                    "events",
                    json={
                        "type": "error",
                        "content": "本机智能体执行失败。",
                        "in_reply_to": command_id,
                        "client_event_id": f"error-{uuid.uuid4().hex}",
                    },
                )
            else:
                await self.transport.request(
                    "POST", f"commands/{command_id}/status", json={"status": final}
                )
        except BaseException:
            self._record(command_id, "interrupted")
            if self.active_run_id:
                await self.session.cancel(self.active_run_id)
            raise
        finally:
            self.command_id = None
            self.active_run_id = None

    async def _poll(self) -> None:
        while not self._stop.is_set():
            self._assert_lease()
            # Do not claim another command while a foreground task is active.
            if self.session.metadata().get("active_runs"):
                await asyncio.sleep(0.2)
                continue
            # Keep the existing Relay long-poll alive instead of waking once per
            # second. Heartbeats run independently, so this does not delay lease
            # renewal or account-change detection.
            result = await self.transport.request(
                "GET", "commands/next", params={"wait_seconds": 25}
            )
            if result.get("command"):
                await self._execute(result["command"])
                self.state = "online"
            else:
                await asyncio.sleep(1)

    async def run(self) -> None:
        backoff = 1.0
        try:
            while not self._stop.is_set():
                tasks: list[asyncio.Task] = []
                try:
                    self.state = "connecting"
                    await self._acquire()
                    tasks = [
                        asyncio.create_task(fn())
                        for fn in (
                            self._heartbeats,
                            self._watch_identity,
                            self._poll,
                            self._sync_images,
                        )
                    ]
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    backoff = 1
                except AccountRevokedError:
                    self.state = "login_required"
                    self._stop.set()
                except (httpx.HTTPError, LeaseLost):
                    if self.state != "account_changed":
                        self.state = "interrupted"
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    if self._foreground_execution:
                        await self.session.cancel(self._foreground_execution)
                    with suppress(httpx.HTTPError, LeaseLost):
                        if self.transport.lease_token is not None:
                            await self.transport.request("POST", "runtime/release", json={})
                    self.transport.lease_token = None
                if not self._stop.is_set():
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    except TimeoutError:
                        pass
                    backoff = min(backoff * 2, 30)
        finally:
            if self.state not in {"account_changed", "login_required"}:
                self.state = "stopped"

    async def stop(self) -> None:
        self._stop.set()
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.active_run_id:
            await self.session.cancel(self.active_run_id)
        self.state = "stopped"
