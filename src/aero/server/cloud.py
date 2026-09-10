"""Authenticated local cloud controls. Remote credentials never reach the browser."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from aero.core.config import save_llm_profile
from aero.core.official_account import (
    CloudSyncClient,
    OfficialAccountError,
    OfficialAccountSession,
    OfficialLoginRequiredError,
    relay_llm_url,
)


class LoginBody(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1000)


class NameBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class BindingBody(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    directory_id: str | None = None
    includes: list[str] = Field(
        default_factory=lambda: ["papers", "literature", "scripts", "plans"]
    )
    enabled: bool = True


class ResolutionBody(BaseModel):
    path: str
    choice: Literal["local", "cloud", "both"]


class AgentBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=500)
    project_id: str = Field(min_length=1, max_length=64)
    session_id: str | None = None


class AgentStartBody(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=128)


class MemoryImportBody(BaseModel):
    recovery_key: str = Field(min_length=16, max_length=512)


class CloudServices:
    def __init__(self, web: Any):
        self.web = web
        self.account = OfficialAccountSession()
        self.storage = CloudSyncClient(self.account)
        self._sync = None
        self._sync_stop = asyncio.Event()
        self._sync_task: asyncio.Task | None = None
        self.agents: dict[str, Any] = {}
        self._account_change_lock = asyncio.Lock()

    @property
    def sync(self):
        if self._sync is None:
            from aero.core.cloud_sync import CloudSyncEngine

            self._sync = CloudSyncEngine(
                self.web.project_dir, self.account,
                busy=lambda: any(s.metadata()["active_runs"]
                                 for s in self.web.active_sessions.values()),
            )
        return self._sync

    def start_sync(self) -> None:
        if self._sync_task is None or self._sync_task.done():
            self._sync_stop = asyncio.Event()
            self._sync_task = asyncio.create_task(self.sync.run(self._sync_stop))

    async def pause_cloud(self) -> None:
        if self._sync is not None:
            self.sync.pause(True)
        self._sync_stop.set()
        if self._sync_task:
            self._sync_task.cancel()
            await asyncio.gather(self._sync_task, return_exceptions=True)
        for hosted, client in list(self.agents.values()):
            await hosted.stop()
            await client.aclose()
        self.agents.clear()

    async def close(self) -> None:
        await self.pause_cloud()
        await self.account.close()

    async def list_agents(self) -> list[dict[str, Any]]:
        from aero.core.remote_agent import RemoteAgentClient

        client = RemoteAgentClient(session=self.account, select_agent=False)
        try:
            return await client.list_registered_remote_agents()
        finally:
            await client.close()

    async def register_agent(
        self, name: str, description: str, project_id: str, session_id: str | None = None
    ) -> dict[str, Any]:
        from aero.core.remote_agent import RemoteAgentClient

        client = RemoteAgentClient(session=self.account, select_agent=False)
        try:
            result = await client.register(
                name, description=description, project_id=project_id, session_id=session_id
            )
            return {key: value for key, value in result.items() if key != "token"}
        finally:
            await client.close()

    async def agent_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Call a user-facing Agent endpoint with the local account JWT."""
        response = await self.account.request(method, "/v1/agents" + path, **kwargs)
        if response.status_code >= 400:
            raise OfficialAccountError("智能体操作失败，请刷新状态后重试。")
        return response.json() if response.content else {}

    def hosted_agent(self, agent_id: str):
        pair = self.agents.get(agent_id)
        if pair is None:
            raise OfficialAccountError("请先在当前窗口托管该智能体会话。")
        return pair[0]

    async def account_view(self) -> dict[str, Any]:
        data = self.account.data
        result = {
            "state": self.account.state,
            "user": {"user_id": data.user_id, "email": data.email} if data.is_logged_in else None,
            "credits": None, "settings": self.web.settings_view(),
        }
        if data.is_logged_in:
            try:
                sync_status = self.sync.status()
            except Exception:
                sync_status = {}
            binding = sync_status.get("binding")
            if (
                binding
                and binding.get("user_id") == data.user_id
                and binding.get("base_url") == self.account.base_url
                and not sync_status.get("paused")
            ):
                self.start_sync()
            try:
                result["credits"] = await self.account.credits()
            except OfficialAccountError as exc:
                result["error"] = str(exc)
                result["state"] = self.account.state
        return result


def cloud_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/api/v1/cloud")

    async def service(web=Depends(require_auth)):
        try:
            yield web.cloud
        except OfficialLoginRequiredError as exc:
            raise HTTPException(401, str(exc)) from exc
        except (OfficialAccountError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except httpx.HTTPError as exc:
            # Signed URLs and credentials can occur in httpx exception strings.
            raise HTTPException(502, "云服务连接失败，请查看状态后重试。") from exc

    @router.get("/account")
    async def account(cloud=Depends(service)):
        return await cloud.account_view()

    @router.post("/account/login")
    async def login(body: LoginBody, cloud=Depends(service)):
        async with cloud._account_change_lock:
            previous_user = cloud.account.data.user_id
            previous_sync_enabled = False
            if cloud._sync is not None:
                previous_sync_enabled = bool(cloud.sync.status().get("enabled"))
            await cloud.pause_cloud()
            await cloud.account.login(body.email, body.password)
            if previous_sync_enabled and previous_user == cloud.account.data.user_id:
                cloud.sync.pause(False)
                cloud.start_sync()
            cfg = cloud.web.config
            cfg.llm.switch_provider("official")
            cfg.llm.model = "auto"
            cfg.llm.base_url = relay_llm_url()
            cfg.llm.set_active_api_key("")
            save_llm_profile("official", "", "auto", relay_llm_url())
            cfg.save(cloud.web.project_dir / "aero.yaml")
            cloud.web.sync_active_sessions()
        return await cloud.account_view()

    @router.post("/account/logout")
    async def logout(cloud=Depends(service)):
        async with cloud._account_change_lock:
            await cloud.pause_cloud()
            await cloud.account.logout()
        return await cloud.account_view()

    @router.get("/projects")
    async def projects(cloud=Depends(service)):
        return {"items": await cloud.storage.projects()}

    @router.post("/projects")
    async def create_project(body: NameBody, cloud=Depends(service)):
        return await cloud.storage.json("POST", "/projects", json=body.model_dump())

    @router.get("/projects/{project_id}/directories")
    async def directories(project_id: str, cloud=Depends(service)):
        return {"items": await cloud.storage.directories(project_id)}

    @router.get("/sync")
    async def sync_status(cloud=Depends(service)):
        return cloud.sync.status()

    @router.put("/sync")
    async def bind(body: BindingBody, cloud=Depends(service)):
        await cloud.account.access_token()
        await cloud.sync.bind(
            user_id=cloud.account.data.user_id, project_id=body.project_id,
            root_directory_id=body.directory_id, includes=body.includes,
        )
        cloud.sync.pause(not body.enabled)
        if body.enabled:
            cloud.start_sync()
        return cloud.sync.status()

    @router.post("/sync/run")
    async def sync_once(cloud=Depends(service)):
        await cloud.sync.sync_once()
        return cloud.sync.status()

    @router.post("/sync/pause")
    async def pause(cloud=Depends(service)):
        cloud.sync.pause(True)
        return cloud.sync.status()

    @router.post("/sync/resolve")
    async def resolve(body: ResolutionBody, cloud=Depends(service)):
        await cloud.sync.resolve(body.path, body.choice)
        return cloud.sync.status()

    @router.get("/agents")
    async def agents(cloud=Depends(service)):
        return {"items": await cloud.list_agents()}

    @router.post("/agents")
    async def register_agent(body: AgentBody, cloud=Depends(service)):
        return await cloud.register_agent(
            body.name, body.description, body.project_id, body.session_id
        )

    @router.post("/agents/{agent_id}/start")
    async def start_agent(agent_id: str, body: AgentStartBody, cloud=Depends(service)):
        from aero.application.cloud_runtime import CloudAgentRuntime, RelayRuntimeTransport
        from aero.core.remote_agent import RemoteAgentRegistry

        agent = next((item for item in RemoteAgentRegistry().list()
                      if item.agent_id == agent_id), None)
        if agent is None:
            raise HTTPException(404, "本机没有该 Agent 的安全令牌")
        session = cloud.web.session(body.session_id)
        if session is None:
            raise HTTPException(404, "会话不存在")
        existing = cloud.agents.get(agent_id)
        if existing is not None:
            old_runtime, old_client = existing
            if (old_runtime.session.id == body.session_id
                    and old_runtime.project_id == body.project_id):
                return old_runtime.status()
            await old_runtime.stop()
            await old_client.aclose()
            cloud.agents.pop(agent_id, None)
        client = httpx.AsyncClient(base_url=cloud.account.base_url, timeout=35)
        hosted = CloudAgentRuntime(
            session, RelayRuntimeTransport(client, agent.agent_id, agent.token),
            device_id=f"web-{cloud.web.project_dir.name}", project_id=body.project_id,
            account_id=cloud.account.data.user_id,
            account_identity=lambda: cloud.account.data.user_id,
            state_dir=Path.home() / ".aero" / "agent-runtime",
        )
        cloud.agents[agent_id] = (hosted, client)
        return await hosted.start()

    @router.post("/agents/{agent_id}/stop")
    async def stop_agent(agent_id: str, cloud=Depends(service)):
        pair = cloud.agents.pop(agent_id, None)
        if pair is not None:
            hosted, client = pair
            await hosted.stop()
            await client.aclose()
        return {"stopped": True}

    @router.get("/agents/{agent_id}/messages")
    async def agent_messages(agent_id: str, cloud=Depends(service)):
        return await cloud.agent_json("GET", f"/{agent_id}/messages")

    @router.post("/agents/{agent_id}/messages")
    async def send_agent_message(
        agent_id: str, body: dict[str, Any], cloud=Depends(service)
    ):
        return await cloud.agent_json("POST", f"/{agent_id}/messages", json=body)

    @router.get("/agents/{agent_id}/approvals")
    async def agent_approvals(agent_id: str, cloud=Depends(service)):
        return await cloud.agent_json("GET", f"/{agent_id}/approvals")

    @router.post("/agents/{agent_id}/approvals/{approval_id}/resolve")
    async def resolve_agent_approval(
        agent_id: str, approval_id: str, body: dict[str, Any], cloud=Depends(service)
    ):
        return await cloud.agent_json(
            "POST", f"/{agent_id}/approvals/{approval_id}/resolve", json=body
        )

    @router.post("/agents/{agent_id}/commands/{command_id}/cancel")
    async def cancel_agent_command(agent_id: str, command_id: str, cloud=Depends(service)):
        return await cloud.agent_json(
            "POST", f"/{agent_id}/commands/{command_id}/cancel", json={}
        )

    @router.post("/agents/{agent_id}/memory/enable")
    async def enable_agent_memory(agent_id: str, cloud=Depends(service)):
        return cloud.hosted_agent(agent_id).enable_memory()

    @router.post("/agents/{agent_id}/memory/export")
    async def export_agent_memory(agent_id: str, cloud=Depends(service)):
        return await cloud.hosted_agent(agent_id).export_memory()

    @router.post("/agents/{agent_id}/memory/import")
    async def import_agent_memory(
        agent_id: str, body: MemoryImportBody, cloud=Depends(service)
    ):
        return await cloud.hosted_agent(agent_id).import_memory(body.recovery_key)

    return router
