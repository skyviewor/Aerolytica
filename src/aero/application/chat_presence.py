"""Cloud presence for a foreground TUI conversation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable

import httpx

from aero.core.official_account import (
    OfficialAccountError, OfficialAccountSession, OfficialLoginRequiredError,
)


class ChatPresence:
    def __init__(
        self, *, session_id: Callable[[], str], title: Callable[[], str],
        on_title: Callable[[str], None], on_command: Callable[[str], Awaitable[str]],
        on_login_required: Callable[[], Awaitable[None]],
        on_quota_full: Callable[[], None],
        on_taken_over: Callable[[], None] | None = None,
        export_context: Callable[[], dict | None] | None = None,
        restore_context: Callable[[dict], None] | None = None,
        is_idle: Callable[[], bool] | None = None,
    ) -> None:
        self.session = OfficialAccountSession()
        self.session_id = session_id
        self.title = title
        self.on_title = on_title
        self.on_command = on_command
        self.on_login_required = on_login_required
        self.on_quota_full = on_quota_full
        self.on_taken_over = on_taken_over or (lambda: None)
        self.export_context = export_context or (lambda: None)
        self.restore_context = restore_context or (lambda _payload: None)
        self.is_idle = is_idle or (lambda: True)
        self.agent_id = ""
        self.token = ""
        self._context_version = 0
        self._context_hash = ""
        self._running = True
        self._prompted = False
        self._blocked_session_id = ""
        self._http = httpx.AsyncClient(base_url=self.session.base_url, timeout=35)
        self._command_task: asyncio.Task | None = None

    async def close(self) -> None:
        self._running = False
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

    async def run(self) -> None:
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
                    self._context_version = 0
                    self._context_hash = ""
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
                if data.get("title_source") == "manual" and data.get("display_name") != self.title():
                    self.on_title(data["display_name"])
                if self.is_idle():
                    await self._sync_context()
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

    async def _sync_context(self) -> None:
        payload = self.export_context()
        if not payload:
            return
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if digest == self._context_hash:
            return
        response = await self._http.put(
            f"/v1/agents/{self.agent_id}/context",
            json={"context": payload, "expected_version": self._context_version},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        response.raise_for_status()
        self._context_version = int(response.json()["version"])
        self._context_hash = digest

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
            await self._http.post(
                f"/v1/agents/{self.agent_id}/events",
                json={"type": "message", "content": answer or "已执行。",
                      "in_reply_to": command_id, "client_event_id": uuid.uuid4().hex},
                headers=headers,
            )
        except Exception:
            await self._http.post(
                f"/v1/agents/{self.agent_id}/commands/{command_id}/status",
                json={"status": "failed"}, headers=headers,
            )
        finally:
            if renewer is not None:
                renewer.cancel()
                await asyncio.gather(renewer, return_exceptions=True)

    async def _renew_command(self, command_id: str, headers: dict[str, str]) -> None:
        while True:
            await asyncio.sleep(30)
            response = await self._http.post(
                f"/v1/agents/{self.agent_id}/commands/{command_id}/status",
                json={"status": "running"}, headers=headers,
            )
            response.raise_for_status()
