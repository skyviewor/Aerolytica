"""Authenticated local cloud controls. Remote credentials never reach the browser."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from aero.core.config import save_llm_profile
from aero.core.device_login import DeviceLoginController
from aero.core.official_account import (
    CloudSyncClient,
    OfficialAccountError,
    OfficialAccountSession,
    OfficialLoginRequiredError,
    relay_llm_url,
)
from aero.release_environment import user_home


class LoginModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["browser", "manual"]


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


class SyncControlBody(BaseModel):
    action: Literal["enable", "disable", "run_once"]
    expected_revision: str
    interval_seconds: int | None = Field(default=None, ge=30, le=900, strict=True)


class AgentBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=500)
    project_id: str = Field(min_length=1, max_length=64)
    session_id: str | None = None


class AgentStartBody(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=128)
    update_restore: bool = False


class CloudServices:
    def __init__(self, web: Any):
        self.web = web
        self.account = OfficialAccountSession()
        self.storage = CloudSyncClient(self.account)
        self._sync = None
        self._sync_stop = asyncio.Event()
        self._sync_task: asyncio.Task | None = None
        self._manual_sync_task: asyncio.Task | None = None
        self._manual_job: dict = {}
        self._sync_control_lock = asyncio.Lock()
        self.agents: dict[str, Any] = {}
        self._account_change_lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self._login_controller: DeviceLoginController | None = None
        self._login_task: asyncio.Task | None = None

    async def cancel_login(self) -> None:
        if self._login_controller:
            await self._login_controller.cancel()
        if self._login_task and not self._login_task.done():
            self._login_task.cancel()
            try:
                await self._login_task
            except asyncio.CancelledError:
                pass

    async def start_login(self, mode: str) -> dict:
        async with self._login_lock:
            await self.cancel_login()
            controller = DeviceLoginController(self.account, commit=self._commit_login)
            self._login_controller = controller
            state = await controller.start(mode, open_browser=False)
            self._login_task = asyncio.create_task(controller.wait())
            return state

    async def _commit_login(self, data) -> bool:
        async with self._account_change_lock:
            controller = self._login_controller
            if controller is None or controller._cancelled:
                return False
            previous_user = self.account.data.user_id
            enabled = bool(self._sync and self.sync.status().get("enabled"))
            await self.pause_cloud()
            if not await controller.commit_session(data):
                if enabled:
                    self.sync.pause(False)
                    self.start_sync()
                return False
            if enabled and previous_user == data.user_id:
                self.sync.pause(False)
                self.start_sync()
            cfg = self.web.config
            cfg.llm.switch_provider("official")
            cfg.llm.model, cfg.llm.base_url = "auto", relay_llm_url()
            cfg.llm.set_active_api_key("")
            try:
                save_llm_profile("official", "", "auto", relay_llm_url())
                cfg.save(self.web.project_dir / "aero.yaml")
            except OSError:
                controller._state["message"] = "登录成功，但模型配置未能保存，请检查目录权限。"
            self.web.sync_active_sessions()
            return True

    @property
    def sync(self):
        if self._sync is None:
            from aero.core.cloud_sync import CloudSyncEngine

            self._sync = CloudSyncEngine(
                self.web.project_dir,
                self.account,
                busy=lambda: any(
                    s.metadata()["active_runs"] for s in self.web.active_sessions.values()
                ),
            )
        return self._sync

    def start_sync(self) -> None:
        if self._sync_task is None or self._sync_task.done():
            self._sync_stop = asyncio.Event()
            self._sync_task = asyncio.create_task(self.sync.run(self._sync_stop))

    def sync_control_status(self) -> dict:
        from aero.core.cloud_sync import CloudSyncEngine

        return {
            "configuration": CloudSyncEngine.inspect_scope(self.web.project_dir),
            "service_running": True,
            "automatic_task_running": bool(self._sync_task and not self._sync_task.done()),
            "transfer_in_progress": bool(self._sync and self._sync._mutex.locked()),
            "manual_job": dict(self._manual_job),
            "last_error": self._sync._last_error if self._sync else None,
        }

    async def control_sync(self, body: SyncControlBody) -> dict:
        async with self._sync_control_lock:
            return await self._control_sync(body)

    async def _control_sync(self, body: SyncControlBody) -> dict:
        from aero.core.cloud_sync import CloudSyncEngine, CloudSyncError

        current = CloudSyncEngine.inspect_scope(self.web.project_dir)
        if current["revision"] != body.expected_revision:
            raise HTTPException(409, "同步配置已变化，请重新读取并审批。")
        if body.action != "disable":
            if not current["bound"]:
                raise HTTPException(409, "工作区尚未绑定云项目，不能开启或立即同步。")
            await self.account.access_token()
            binding = (
                CloudSyncEngine._scope_reader(self.web.project_dir)._state.get("binding") or {}
            )
            if (
                binding.get("user_id") != self.account.data.user_id
                or binding.get("base_url") != self.account.base_url
            ):
                raise HTTPException(409, "当前账户或接口与同步绑定不匹配。")
        if body.action in {"enable", "disable"}:
            # Cancel our own transfers before changing persisted pause state.
            tasks = [t for t in (self._sync_task, self._manual_sync_task) if t and not t.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._manual_job.get("state") in {"queued", "running"}:
                self._manual_job["state"] = "canceled"
            try:
                await self.sync.configure_sync(
                    body.action == "enable", body.expected_revision, body.interval_seconds
                )
            except CloudSyncError as exc:
                raise HTTPException(409, "同步配置未能修改，请重新查询状态。") from exc
            if body.action == "enable":
                self.start_sync()
            return {"status": "success", **self.sync_control_status()}
        if not self._manual_sync_task or self._manual_sync_task.done():
            self._manual_job = {"job_id": uuid.uuid4().hex, "state": "queued"}
            self._manual_sync_task = asyncio.create_task(
                self._manual_sync_once(body.expected_revision)
            )
        return {"status": "queued", **self.sync_control_status()}

    async def _manual_sync_once(self, revision: str) -> None:
        from aero.core.cloud_sync import CloudSyncEngine

        deadline = asyncio.get_running_loop().time() + 300
        try:
            while asyncio.get_running_loop().time() < deadline:
                reader = CloudSyncEngine._scope_reader(self.web.project_dir)
                if reader._scope_view()["revision"] != revision:
                    self._manual_job["state"] = "configuration_changed"
                    return
                binding = reader._state.get("binding") or {}
                if (
                    binding.get("user_id") != self.account.data.user_id
                    or binding.get("base_url") != self.account.base_url
                ):
                    self._manual_job["state"] = "account_changed"
                    return
                self._manual_job["state"] = "running"
                result = await self.sync.sync_once(manual=True)
                if not result.get("deferred") and not result.get("busy"):
                    count = len(result["conflicts"])
                    self._manual_job.update(
                        state="completed_with_conflicts" if count else "completed",
                        conflict_count=count,
                    )
                    return
                self._manual_job["state"] = "queued"
                await asyncio.sleep(1)
            self._manual_job["state"] = "wait_timeout"
        except asyncio.CancelledError:
            self._manual_job["state"] = "canceled"
            raise
        except Exception:
            # Do not persist/return exception strings with credentials or signed URLs.
            self._manual_job["state"] = "failed"

    async def pause_cloud(self) -> None:
        if self._sync is not None:
            self.sync.pause(True)
        self._sync_stop.set()
        if self._manual_sync_task:
            self._manual_sync_task.cancel()
            await asyncio.gather(self._manual_sync_task, return_exceptions=True)
        if self._sync_task:
            self._sync_task.cancel()
            await asyncio.gather(self._sync_task, return_exceptions=True)
        for hosted, client in list(self.agents.values()):
            await hosted.stop()
            await client.aclose()
        self.agents.clear()

    async def close(self) -> None:
        await self.cancel_login()
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
            "credits": None,
            "settings": self.web.settings_view(),
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
    async def login(cloud=Depends(service)):
        raise HTTPException(410, "客户端已改用官网授权，不再接收账号密码。")

    @router.post("/account/device-login/start")
    async def start_login(body: LoginModeBody, cloud=Depends(service)):
        return await cloud.start_login(body.mode)

    def current_login(cloud, flow_id):
        controller = cloud._login_controller
        if controller is None or not secrets.compare_digest(controller.flow_id, flow_id):
            raise HTTPException(404, "授权请求不存在或已被替换。")
        return controller

    @router.get("/account/device-login/{flow_id}")
    async def login_status(flow_id: str, cloud=Depends(service)):
        return current_login(cloud, flow_id).public_state()

    @router.post("/account/device-login/{flow_id}/cancel")
    async def cancel_login(flow_id: str, cloud=Depends(service)):
        async with cloud._login_lock:
            controller = current_login(cloud, flow_id)
            await cloud.cancel_login()
            return controller.public_state()

    @router.post("/account/logout")
    async def logout(cloud=Depends(service)):
        async with cloud._login_lock:
            await cloud.cancel_login()
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

    @router.get("/sync/control/status")
    async def sync_control_status(cloud=Depends(service)):
        return cloud.sync_control_status()

    @router.post("/sync/control")
    async def sync_control(body: SyncControlBody, cloud=Depends(service)):
        return await cloud.control_sync(body)

    @router.put("/sync")
    async def bind(body: BindingBody, cloud=Depends(service)):
        await cloud.account.access_token()
        await cloud.sync.bind(
            user_id=cloud.account.data.user_id,
            project_id=body.project_id,
            root_directory_id=body.directory_id,
            includes=body.includes,
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
    async def start_agent(
        agent_id: str, body: AgentStartBody, request: Request, cloud=Depends(service)
    ):
        if body.update_restore:
            if (
                not cloud.web.update_paused
                or request.headers.get("origin")
                or not secrets.compare_digest(
                    request.headers.get("authorization", ""), f"Bearer {cloud.web.launch_token}"
                )
            ):
                raise HTTPException(403, "仅本机升级控制器可恢复暂停中的 Agent")
        elif cloud.web.update_paused:
            raise HTTPException(503, "客户端正在更新，请稍后重试")
        from aero.application.cloud_runtime import CloudAgentRuntime, RelayRuntimeTransport
        from aero.core.remote_agent import RemoteAgentRegistry

        agent = next(
            (item for item in RemoteAgentRegistry().list() if item.agent_id == agent_id), None
        )
        if agent is None:
            raise HTTPException(404, "本机没有该 Agent 的安全令牌")
        session = cloud.web.session(body.session_id)
        if session is None:
            raise HTTPException(404, "会话不存在")
        existing = cloud.agents.get(agent_id)
        if existing is not None:
            old_runtime, old_client = existing
            if (
                old_runtime.session.id == body.session_id
                and old_runtime.project_id == body.project_id
            ):
                return old_runtime.status()
            await old_runtime.stop()
            await old_client.aclose()
            cloud.agents.pop(agent_id, None)
        client = httpx.AsyncClient(base_url=cloud.account.base_url, timeout=35)
        hosted = CloudAgentRuntime(
            session,
            RelayRuntimeTransport(
                client,
                agent.agent_id,
                agent.token,
                account_access_token=cloud.account.access_token,
            ),
            device_id=f"web-{cloud.web.project_dir.name}",
            project_id=body.project_id,
            account_id=cloud.account.data.user_id,
            account_identity=lambda: cloud.account.data.user_id,
            state_dir=user_home() / "agent-runtime",
        )
        cloud.agents[agent_id] = (hosted, client)
        hosted.update_paused = cloud.web.update_paused
        session.update_paused = cloud.web.update_paused
        result = await hosted.start()
        if body.update_restore:
            try:
                async with asyncio.timeout(15):
                    while hosted.transport.lease_token is None:
                        if hosted._task.done():
                            raise RuntimeError("restored_agent_failed")
                        await asyncio.sleep(0.05)
            except (RuntimeError, TimeoutError) as exc:
                await hosted.stop()
                await client.aclose()
                cloud.agents.pop(agent_id, None)
                raise HTTPException(503, "恢复后的 Agent 尚未就绪") from exc
            result = hosted.status()
        return result

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
    async def send_agent_message(agent_id: str, body: dict[str, Any], cloud=Depends(service)):
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
        return await cloud.agent_json("POST", f"/{agent_id}/commands/{command_id}/cancel", json={})

    return router
