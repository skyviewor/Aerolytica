"""Fenced Relay worker hosting the *same* LocalSession used by the local UI.

Heartbeats and account checks never share the command/event polling task. A lost
lease cancels local work and records interruption before reconnect; it is never
interpreted as permission to replay an uncertain operation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.fernet import Fernet

from aero.core.daemon import private_write

if TYPE_CHECKING:
    from aero.application.local_session import LocalSession


class LeaseLostError(RuntimeError):
    """Ownership is uncertain; stop side effects before any reconnect."""


LeaseLost = LeaseLostError


class RelayRuntimeTransport:
    """Small explicit adapter; injected HTTP client remains caller-owned."""

    def __init__(self, http: httpx.AsyncClient, agent_id: str, token: str):
        self.http = http
        self.agent_id = agent_id
        self._token = token
        self.device_id = ""
        self.lease_token: int | None = None

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if self.lease_token is not None:
            headers.update({"X-Agent-Device-Id": self.device_id,
                            "X-Agent-Lease-Token": str(self.lease_token)})
        response = await self.http.request(
            method, f"/v1/agents/{self.agent_id}/{path}", headers=headers, **kwargs
        )
        if response.status_code in {401, 403, 409, 410, 423}:
            raise LeaseLost(f"runtime_rejected_{response.status_code}")
        response.raise_for_status()
        return {} if response.status_code == 204 else response.json()


class PortableMemory:
    """Encrypted, explicitly sanitized conversation context and notes.

The random recovery key is portable across machines and must be kept by the user.
This class deliberately does not derive keys from a hostname, machine ID or token.
"""

    def __init__(self, recovery_key: str | None = None):
        self.recovery_key = recovery_key or Fernet.generate_key().decode()
        self._cipher = Fernet(self.recovery_key.encode())
        self.memory_key = hashlib.sha256(self.recovery_key.encode()).hexdigest()

    def encrypt(
        self, notes: list[str], *, session_id: str, project_id: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> bytes:
        if not all(isinstance(note, str) for note in notes):
            raise ValueError("memory_requires_explicit_text_notes")
        # Reject known secret formats; callers must never pass raw conversation or config.
        for note in [*notes, *[str(item.get("content", "")) for item in (messages or [])]]:
            if re.search(
                r"(?i)(?:\b(?:sk-|agt_)[\w.-]{6,}|bearer\s+\S+|"
                r"(?:api[_ -]?key|password|secret|token)\s*[:=]\s*\S+|"
                r"-----BEGIN .*PRIVATE KEY-----)", note
            ):
                raise ValueError("memory_may_contain_secret")
        safe_messages = []
        for item in messages or []:
            role = str(item.get("role", ""))
            content = str(item.get("content", ""))
            if role in {"user", "assistant"} and content:
                safe_messages.append({"role": role, "content": content[:100_000]})
        payload = {"schema": 1, "notes": notes, "messages": safe_messages,
                  "session_id": session_id, "project_id": project_id}
        raw = json.dumps(payload, ensure_ascii=False).encode()
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("memory_snapshot_too_large")
        return self._cipher.encrypt(raw)

    def decrypt(self, encrypted: bytes) -> dict[str, Any]:
        payload = json.loads(self._cipher.decrypt(encrypted))
        if (payload.get("schema") != 1 or not isinstance(payload.get("notes"), list)
                or not isinstance(payload.get("messages", []), list)):
            raise ValueError("invalid_memory_snapshot")
        return payload


class CloudAgentRuntime:
    """Host one bound conversation; foreground clients attach to ``session``.

    ``account_identity`` must read the *current persisted* official user ID, not a
    cached login object. Empty/different identities stop execution immediately.
    ``state_dir`` holds only a private command journal, never prompts or secrets.
    """

    def __init__(
        self, session: LocalSession, transport: RelayRuntimeTransport, *,
        device_id: str, project_id: str, account_id: str,
        account_identity: Callable[[], str], state_dir: Path,
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
        self._memory_path = state_dir / f"{key}.memory.json"
        self._journal: dict[str, str] = {}
        if self._journal_path.exists():
            self._journal = json.loads(self._journal_path.read_text())
        self.state = "stopped"
        self.active_run_id: str | None = None
        self.command_id: str | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._deadline = 0.0
        self._heartbeat_seconds = 30.0
        self._memory: PortableMemory | None = None
        self.memory_version = 0
        self._foreground_execution: str | None = None
        self._load_memory_state()

    def _load_memory_state(self) -> None:
        """Restore only local memory configuration; the encrypted snapshot stays remote."""
        try:
            if self._memory_path.is_symlink() or not self._memory_path.exists():
                return
            metadata = self._memory_path.stat()
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ValueError("unsafe_memory_state_permissions")
            state = json.loads(self._memory_path.read_text())
            recovery_key = str(state.get("recovery_key") or "")
            version = int(state.get("version") or 0)
            if len(recovery_key) < 16 or version < 0:
                return
            self._memory = PortableMemory(recovery_key)
            self.memory_version = version
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._memory = None
            self.memory_version = 0

    def _save_memory_state(self) -> None:
        private_write(self._memory_path, json.dumps({
            "recovery_key": self._memory.recovery_key if self._memory else "",
            "version": self.memory_version,
        }))

    def status(self) -> dict[str, Any]:
        return {"agent_id": self.transport.agent_id, "session_id": self.session.id,
                "project_id": self.project_id, "state": self.state,
                "active_run_id": self.active_run_id, "command_id": self.command_id,
                "memory_enabled": self._memory is not None,
                "memory_version": self.memory_version}

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
        result = await self.transport.request("POST", "runtime/acquire", json={
            "device_id": self.device_id, "project_id": self.project_id,
            "session_id": self.session.id,
        })
        if result["session_id"] != self.session.id:
            raise LeaseLost("session_binding_mismatch")
        self.transport.lease_token = int(result["lease_token"])
        self._deadline = started + min(float(result.get("ttl_seconds", 120)), 120)
        self._heartbeat_seconds = min(float(result.get("heartbeat_seconds", 30)), 30)
        self.session.execution_guard = self.project_execution
        self.state = "online"

    async def _heartbeats(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self._heartbeat_seconds)
            self._assert_lease()
            started = time.monotonic()
            result = await self.transport.request("POST", "runtime/heartbeat", json={})
            self._deadline = started + min(float(result.get("ttl_seconds", 120)), 120)
            if self.command_id in result.get("cancel_requested_command_ids", []):
                if self.active_run_id:
                    await self.session.cancel(self.active_run_id)

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
        result = await self.transport.request("POST", "runtime/project/acquire",
                                              json={"execution_id": run_id})
        self._foreground_execution = run_id
        try:
            yield
        finally:
            self._foreground_execution = None
            with suppress(httpx.HTTPError, LeaseLost):
                await self.transport.request("POST", "runtime/project/release", json={
                    "execution_id": run_id,
                    "project_lease_token": result["project_lease_token"],
                })

    async def _approval(self, command_id: str, run_id: str, event: Any) -> None:
        # Only operation identifiers are cloud-visible, not tool arguments/secrets.
        request_id = f"{run_id}:{event.id}"[:64]
        await self.transport.request("POST", f"commands/{command_id}/approvals", json={
            "request_id": request_id,
            "payload": {"tool": str(event.data.get("tool", "operation"))[:120],
                        "message": "Local operation requires approval", "run_id": run_id},
        })
        self.state = "waiting_confirmation"
        while not self._stop.is_set():
            self._assert_lease()
            result = await self.transport.request("GET", f"commands/{command_id}/approvals")
            if result.get("cancel_requested"):
                await self.session.cancel(run_id)
                return
            approval = next((a for a in result.get("approvals", [])
                             if a.get("request_id") == request_id), {})
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

    async def _execute(self, command: dict[str, Any]) -> None:
        command_id = str(command["command_id"])
        if command_id in self._journal:
            # Even 'completed' could have lost its remote acknowledgment: no replay.
            await self.transport.request("POST", f"commands/{command_id}/status", json={
                "status": "interrupted", "error": "local_command_already_attempted",
            })
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
                elif event.type == "text":
                    response_parts.append(event.content or "")
                elif event.type == "status" and event.content:
                    await self.transport.request("POST", "events", json={
                        "type": "progress", "content": str(event.content)[:20_000],
                        "in_reply_to": command_id,
                        "client_event_id": f"progress-{uuid.uuid4().hex}",
                    })
                elif event.type == "secret_required":
                    # Secrets may only be entered via authenticated local UI.
                    self.state = "waiting_secret"
            state = self.session.run_status(self.active_run_id).get("state", "failed")
            final = {"completed": "completed", "cancelled": "canceled"}.get(state, "failed")
            self._record(command_id, final)
            if final == "completed":
                await self.transport.request("POST", "events", json={
                    "type": "message",
                    "content": (
                        "".join(response_parts).strip()
                        or "指令已处理，但没有可返回的文本结果。"
                    ),
                    "in_reply_to": command_id,
                    "client_event_id": f"result-{uuid.uuid4().hex}",
                })
            elif final == "failed":
                await self.transport.request("POST", "events", json={
                    "type": "error", "content": "本机智能体执行失败。",
                    "in_reply_to": command_id,
                    "client_event_id": f"error-{uuid.uuid4().hex}",
                })
            else:
                await self.transport.request("POST", f"commands/{command_id}/status",
                                              json={"status": final})
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
                    tasks = [asyncio.create_task(fn()) for fn in
                             (self._heartbeats, self._watch_identity, self._poll)]
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    backoff = 1
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
            if self.state != "account_changed":
                self.state = "stopped"

    async def stop(self) -> None:
        self._stop.set()
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.active_run_id:
            await self.session.cancel(self.active_run_id)
        self.state = "stopped"

    def enable_memory(self, recovery_key: str | None = None) -> dict[str, Any]:
        self._memory = PortableMemory(recovery_key)
        self.memory_version = 0
        self._save_memory_state()
        return {"enabled": True, "recovery_key": self._memory.recovery_key,
                "memory_key": self._memory.memory_key, "version": self.memory_version}

    async def export_memory(self, notes: list[str] | None = None) -> dict[str, Any]:
        self._assert_lease()
        if self._memory is None:
            raise RuntimeError("memory_not_enabled")
        encrypted = self._memory.encrypt(
            notes or [], session_id=self.session.id, project_id=self.project_id,
            messages=self.session.messages_view(),
        )
        result = await self.transport.request("PUT", "memory",
            params={"memory_key": self._memory.memory_key}, json={
                "snapshot": base64.b64encode(encrypted).decode(),
                "checksum": hashlib.sha256(encrypted).hexdigest(),
                "expected_version": self.memory_version,
        })
        self.memory_version = int(result["version"])
        self._save_memory_state()
        return {"version": self.memory_version, "memory_key": self._memory.memory_key}

    async def import_memory(self, recovery_key: str) -> dict[str, Any]:
        self._identity_check()
        memory = PortableMemory(recovery_key)
        result = await self.transport.request("GET", "memory",
                                              params={"memory_key": memory.memory_key})
        encrypted = base64.b64decode(result["snapshot"], validate=True)
        if hashlib.sha256(encrypted).hexdigest() != result["checksum"]:
            raise ValueError("memory_checksum_mismatch")
        payload = memory.decrypt(encrypted)
        self._memory = memory
        self.memory_version = int(result["version"])
        self._save_memory_state()
        self.session.restore_messages(payload.get("messages", []))
        # Restored content is context only; it never auto-executes a task.
        return {**payload, "version": self.memory_version}
