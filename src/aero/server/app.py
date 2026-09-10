"""FastAPI application for the local Aerolytica web workbench."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.staticfiles import StaticFiles

from aero.agent.llm_client import LLMClient, LLMConfig
from aero.agent.session import SessionManager
from aero.application.artifacts import ArtifactAccessError, ArtifactService
from aero.application.local_session import LocalSession
from aero.core.config import (
    AeroConfig,
    save_ads_credentials,
    save_cds_credentials,
    save_earthdata_token,
    save_llm_profile,
    save_vision_api_key,
    save_web_search_api_key,
    vision_is_configured,
)
from aero.core.llm_providers import BUILTIN_LLM_PROVIDERS
from aero.core.types import Message


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)


class ConfirmationRequest(BaseModel):
    choice: str


class SecretRequest(BaseModel):
    request_id: str
    secret: str = Field(min_length=1, max_length=100_000)


class RenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SettingsPatch(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    reasoning_effort: str | None = None
    language: str | None = None
    mode: str | None = None
    max_tool_rounds: int | None = Field(default=None, ge=1, le=10_000)
    vision: dict[str, Any] | None = None
    web_search: dict[str, Any] | None = None


class SecretPatch(BaseModel):
    scope: str
    value: str = Field(min_length=1, max_length=100_000)


class ExperimentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_checkpoint: str | None = None


class CheckpointRequest(BaseModel):
    name: str = Field(default="", max_length=120)


class PaperSaveRequest(BaseModel):
    title: str = Field(default="", max_length=120)


class MemoRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    evidence: str = Field(default="", max_length=100_000)
    tags: list[str] = Field(default_factory=list, max_length=12)


class SetupRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=160)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(min_length=1, max_length=100_000)
    reasoning_effort: str = ""
    language: str = "zh"
    mode: str = "execute"
    max_tool_rounds: int = Field(default=999, ge=1, le=10_000)


class WebRuntime:
    def __init__(self, project_dir: Path, launch_token: str):
        self.project_dir = project_dir.resolve()
        self.launch_token = launch_token
        # Import the built-in tool aggregator explicitly; the Web server does
        # not rely on Textual's import side effects to populate the registry.
        from aero.toolbox import builtin_tools  # noqa: F401

        self.config = self._load_config()
        self.sessions = SessionManager()
        self.artifacts = ArtifactService(self.project_dir)
        self.active_sessions: dict[str, LocalSession] = {}
        from aero.server.cloud import CloudServices

        self.cloud = CloudServices(self)

    def _load_config(self) -> AeroConfig:
        path = self.project_dir / "aero.yaml"
        return AeroConfig.load(path) if path.exists() else AeroConfig.create_default()

    def session(self, session_id: str) -> LocalSession | None:
        if session_id in self.active_sessions:
            return self.active_sessions[session_id]
        meta = next((item for item in self.sessions.list_sessions() if item.id == session_id), None)
        if meta is None or (
            meta.project_dir and Path(meta.project_dir).resolve() != self.project_dir
        ):
            return None
        session = LocalSession(
            self.project_dir,
            self.config.model_copy(deep=True),
            session_id,
            session_manager=self.sessions,
        )
        self.active_sessions[session_id] = session
        return session

    def create_session(self) -> LocalSession:
        session = LocalSession(
            self.project_dir, self.config.model_copy(deep=True), session_manager=self.sessions
        )
        self.active_sessions[session.id] = session
        session._save()
        return session

    def list_sessions(self) -> list[dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        for meta in self.sessions.list_sessions():
            if meta.project_dir and Path(meta.project_dir).resolve() != self.project_dir:
                continue
            session = self.active_sessions.get(meta.id)
            values[meta.id] = (
                session.metadata()
                if session
                else {
                    "id": meta.id,
                    "name": meta.name or "新会话",
                    "created_at": meta.created_at,
                    "updated_at": meta.updated_at,
                    "message_count": meta.message_count,
                    "model": meta.model,
                    "provider": meta.provider,
                    "mode": meta.mode,
                    "title_source": meta.title_source,
                    "active_runs": [],
                }
            )
        return sorted(values.values(), key=lambda item: item.get("updated_at", 0), reverse=True)

    def settings_view(self) -> dict[str, Any]:
        cfg = self.config
        return {
            "provider": cfg.llm.provider,
            "model": cfg.llm.model,
            "base_url": cfg.llm.base_url,
            "reasoning_effort": cfg.llm.reasoning_effort,
            "language": cfg.language,
            "mode": cfg.mode,
            "max_tool_rounds": cfg.max_tool_rounds,
            "llm_configured": bool(cfg.llm.active_api_key()) or (
                cfg.llm.provider == "official" and self.cloud.account.data.is_logged_in
            ),
            "vision": {
                "mode": cfg.vision.mode,
                "provider": cfg.vision.provider,
                "model": cfg.vision.model,
                "base_url": cfg.vision.base_url,
                "configured": vision_is_configured(cfg),
            },
            "web_search": {
                "enabled": cfg.web_search.enabled,
                "provider": cfg.web_search.provider,
                "model": cfg.web_search.model,
                "base_url": cfg.web_search.base_url,
                "configured": bool(cfg.web_search.api_key),
            },
        }

    def providers_view(self) -> list[dict[str, Any]]:
        return [
            {
                "id": preset.id,
                "name": preset.name,
                "base_url": preset.base_url,
                "default_model": preset.default_model,
                "models": list(preset.models),
                "api_key_url": preset.api_key_url,
                "api_key_hint": preset.api_key_hint,
            }
            for preset in BUILTIN_LLM_PROVIDERS.values()
        ]

    def sync_active_sessions(self) -> None:
        for session in self.active_sessions.values():
            session.update_config(self.config)

    async def close(self) -> None:
        await self.cloud.close()
        await asyncio.gather(
            *(session.close() for session in self.active_sessions.values()), return_exceptions=True
        )


def create_app(
    project_dir: Path | str, *, launch_token: str | None = None
) -> tuple[FastAPI, WebRuntime]:
    token = launch_token or secrets.token_urlsafe(32)
    runtime = WebRuntime(Path(project_dir), token)
    app = FastAPI(title="Aerolytica Web", docs_url=None, redoc_url=None)
    app.state.web_runtime = runtime

    def require_auth(request: Request) -> WebRuntime:
        if request.cookies.get("aero_access") != runtime.launch_token:
            raise HTTPException(status_code=401, detail="本地 Web 会话未授权")
        origin = request.headers.get("origin")
        if origin and origin not in {f"http://{request.headers.get('host')}", "null"}:
            raise HTTPException(status_code=403, detail="Origin 不被允许")
        return runtime

    def get_session(session_id: str, web: WebRuntime = Depends(require_auth)) -> LocalSession:
        session = web.session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return session

    from aero.server.cloud import cloud_router

    app.include_router(cloud_router(require_auth))

    @app.get("/")
    async def index(request: Request):
        static_dir = Path(__file__).with_name("static")
        index_path = static_dir / "index.html"
        if not index_path.exists():
            return JSONResponse({"message": "Web UI 资源尚未构建"}, status_code=503)
        if request.query_params.get("token") == runtime.launch_token:
            response = RedirectResponse(url="/", status_code=303)
            response.set_cookie(
                "aero_access", runtime.launch_token, httponly=True, samesite="strict", path="/"
            )
            return response
        return FileResponse(index_path)

    @app.get("/api/v1/bootstrap")
    async def bootstrap(web: WebRuntime = Depends(require_auth)):
        return {
            "project_dir": str(web.project_dir),
            "project_name": web.project_dir.name,
            "settings": web.settings_view(),
            "providers": web.providers_view(),
            "sessions": web.list_sessions(),
            "capabilities": {
                "uploads": True,
                "experiments": True,
                "checkpoints": True,
                "paper": True,
            },
        }

    @app.get("/api/v1/sessions")
    async def list_sessions(web: WebRuntime = Depends(require_auth)):
        return {"sessions": web.list_sessions()}

    @app.post("/api/v1/sessions")
    async def create_session(web: WebRuntime = Depends(require_auth)):
        return web.create_session().metadata()

    @app.get("/api/v1/sessions/{session_id}")
    async def read_session(session: LocalSession = Depends(get_session)):
        return session.metadata()

    @app.patch("/api/v1/sessions/{session_id}")
    async def rename_session(body: RenameRequest, session: LocalSession = Depends(get_session)):
        session.rename(body.name)
        return session.metadata()

    @app.delete("/api/v1/sessions/{session_id}")
    async def delete_session(session_id: str, web: WebRuntime = Depends(require_auth)):
        session = web.session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        await session.close()
        web.active_sessions.pop(session_id, None)
        web.sessions.delete(session_id)
        return {"deleted": True, "session_id": session_id}

    @app.post("/api/v1/sessions/{session_id}/runs")
    async def start_run(body: RunRequest, session: LocalSession = Depends(get_session)):
        try:
            run_id = session.start_run(body.prompt)
        except RuntimeError as exc:
            if str(exc) == "session_busy":
                raise HTTPException(status_code=409, detail="当前会话已有运行中的任务") from exc
            raise
        return session.run_status(run_id)

    @app.get("/api/v1/runs/{run_id}")
    async def run_status(
        run_id: str, session_id: str, session: LocalSession = Depends(get_session)
    ):
        status = session.run_status(run_id)
        if status["status"] == "not_found":
            raise HTTPException(status_code=404, detail="运行不存在")
        return status

    @app.get("/api/v1/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        session_id: str,
        request: Request,
        session: LocalSession = Depends(get_session),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        if session.run_status(run_id).get("status") == "not_found":
            raise HTTPException(status_code=404, detail="运行不存在")
        cursor = int(last_event_id or request.query_params.get("after", "0") or 0)

        async def stream():
            async for event in session.events(run_id, cursor):
                yield {
                    "id": str(event.id),
                    "event": event.type,
                    "data": json.dumps(event.to_dict(), ensure_ascii=False),
                }

        return EventSourceResponse(
            stream(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    @app.post("/api/v1/runs/{run_id}/confirmation")
    async def confirm_run(
        body: ConfirmationRequest,
        run_id: str,
        session_id: str,
        session: LocalSession = Depends(get_session),
    ):
        try:
            session.confirm(run_id, body.choice)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return session.run_status(run_id)

    @app.post("/api/v1/runs/{run_id}/secret")
    async def submit_secret(
        body: SecretRequest,
        run_id: str,
        session_id: str,
        session: LocalSession = Depends(get_session),
    ):
        try:
            session.submit_secret(body.request_id, body.secret)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"accepted": True}

    @app.delete("/api/v1/runs/{run_id}")
    async def cancel_run(
        run_id: str, session_id: str, session: LocalSession = Depends(get_session)
    ):
        await session.cancel(run_id)
        return session.run_status(run_id)

    @app.get("/api/v1/settings")
    async def get_settings(web: WebRuntime = Depends(require_auth)):
        return web.settings_view()

    @app.patch("/api/v1/settings")
    async def patch_settings(body: SettingsPatch, web: WebRuntime = Depends(require_auth)):
        cfg = web.config
        values = body.model_dump(exclude_none=True)
        for key in ("language", "mode", "reasoning_effort", "max_tool_rounds"):
            if key in values:
                setattr(cfg, key, values[key])
        if body.provider:
            cfg.llm.switch_provider(body.provider)
        if body.model is not None:
            cfg.llm.model = body.model
        if body.base_url is not None:
            cfg.llm.base_url = body.base_url
        if body.vision:
            for key, value in body.vision.items():
                if key in {"mode", "provider", "model", "base_url", "cache_ttl_hours"}:
                    setattr(cfg.vision, key, value)
        if body.web_search:
            for key, value in body.web_search.items():
                if key in {"enabled", "provider", "model", "base_url"}:
                    setattr(cfg.web_search, key, value)
        cfg.save(web.project_dir / "aero.yaml")
        web.sync_active_sessions()
        return web.settings_view()

    @app.post("/api/v1/settings/setup")
    async def setup_primary_model(body: SetupRequest, web: WebRuntime = Depends(require_auth)):
        """Test and persist the primary model exactly like the TUI wizard."""
        values = body.model_dump()
        if not values["base_url"].startswith(("https://", "http://")):
            raise HTTPException(status_code=400, detail="接口地址必须以 http:// 或 https:// 开头。")
        try:
            client = LLMClient(
                LLMConfig(
                    provider=values["provider"],
                    model=values["model"],
                    api_key=values["api_key"],
                    base_url=values["base_url"],
                    max_tokens=1,
                )
            )
            try:
                await client.chat([Message(role="user", content="Reply with OK.")])
            finally:
                await client.close()
        except Exception as exc:
            detail = str(exc).replace(values["api_key"], "[API_KEY]").replace("\n", " ").strip()
            raise HTTPException(
                status_code=400,
                detail=(
                    f"连通性测试失败：{detail[:240] or '无法连接模型服务。'} "
                    "请检查 API Key、接口地址和模型 ID。"
                ),
            ) from exc

        cfg = web.config
        cfg.llm.switch_provider(values["provider"])
        cfg.llm.model = values["model"]
        cfg.llm.base_url = values["base_url"]
        cfg.llm.set_active_api_key(values["api_key"])
        cfg.language = values["language"]
        cfg.mode = values["mode"]
        cfg.max_tool_rounds = values["max_tool_rounds"]
        cfg.llm.reasoning_effort = values["reasoning_effort"]
        save_llm_profile(values["provider"], values["api_key"], values["model"], values["base_url"])
        cfg.save(web.project_dir / "aero.yaml")
        web.sync_active_sessions()
        return web.settings_view()

    @app.put("/api/v1/settings/secret")
    async def patch_secret(body: SecretPatch, web: WebRuntime = Depends(require_auth)):
        scope = body.scope
        value = body.value
        cfg = web.config
        if scope == "llm":
            cfg.llm.set_active_api_key(value)
            save_llm_profile(cfg.llm.provider, value, cfg.llm.model, cfg.llm.base_url)
        elif scope == "vision":
            cfg.vision.api_key = value
            save_vision_api_key(
                value,
                cfg.vision.base_url,
                provider=cfg.vision.provider,
                model=cfg.vision.model,
                mode=cfg.vision.mode,
            )
        elif scope == "web_search":
            cfg.web_search.api_key = value
            save_web_search_api_key(
                value,
                cfg.web_search.base_url,
                provider=cfg.web_search.provider,
                model=cfg.web_search.model,
            )
        elif scope == "cds":
            cfg.credentials.cds.key = value
            save_cds_credentials(cfg.credentials.cds.url, value)
        elif scope == "ads":
            cfg.credentials.ads.key = value
            save_ads_credentials(cfg.credentials.ads.url, value)
        elif scope == "earthdata":
            cfg.credentials.earthdata.token = value
            save_earthdata_token(value)
        else:
            raise HTTPException(status_code=400, detail="未知的凭据用途")
        web.sync_active_sessions()
        return {"saved": True, "scope": scope}

    @app.get("/api/v1/workspace/tree")
    async def workspace_tree(path: str = ".", web: WebRuntime = Depends(require_auth)):
        try:
            return {"path": path, "items": web.artifacts.tree(path)}
        except ArtifactAccessError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/artifacts/{artifact_id}/metadata")
    async def artifact_metadata(artifact_id: str, web: WebRuntime = Depends(require_auth)):
        try:
            return web.artifacts.metadata(artifact_id)
        except ArtifactAccessError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/artifacts/{artifact_id}/text")
    async def artifact_text(artifact_id: str, web: WebRuntime = Depends(require_auth)):
        try:
            return {"text": web.artifacts.text(artifact_id)}
        except ArtifactAccessError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/artifacts/{artifact_id}")
    async def artifact_file(artifact_id: str, web: WebRuntime = Depends(require_auth)):
        try:
            path = web.artifacts.path(artifact_id)
            metadata = web.artifacts.metadata(artifact_id)
        except ArtifactAccessError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type=metadata["media_type"],
            filename=path.name,
            content_disposition_type="inline",
        )

    @app.post("/api/v1/uploads")
    async def upload_file(file: UploadFile = File(...), web: WebRuntime = Depends(require_auth)):
        safe_name = Path(file.filename or "upload.bin").name
        target_dir = web.project_dir / web.config.output.data_dir / "uploads"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / safe_name
        if target.exists():
            target = target_dir / f"{target.stem}-{secrets.token_hex(4)}{target.suffix}"
        size = 0
        with target.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    target.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="单个上传文件不能超过 100 MiB")
                output.write(chunk)
        relative = target.relative_to(web.project_dir).as_posix()
        return {"path": relative, "artifact_id": web.artifacts.encode_id(relative), "size": size}

    @app.get("/api/v1/experiments")
    async def list_experiments(web: WebRuntime = Depends(require_auth)):
        from aero.experiments import ExperimentManager

        manager = ExperimentManager(web.project_dir)
        return {"active": manager.active(), "experiments": manager.list()}

    @app.post("/api/v1/experiments")
    async def create_experiment(body: ExperimentRequest, web: WebRuntime = Depends(require_auth)):
        from aero.experiments import ExperimentError, ExperimentManager

        try:
            return ExperimentManager(web.project_dir).create(
                body.name, base_checkpoint=body.base_checkpoint
            )
        except ExperimentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/experiments/{experiment_id}/switch")
    async def switch_experiment(experiment_id: str, web: WebRuntime = Depends(require_auth)):
        from aero.experiments import ExperimentError, ExperimentManager

        try:
            return ExperimentManager(web.project_dir).switch(experiment_id)
        except ExperimentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/v1/experiments/{experiment_id}")
    async def delete_experiment(experiment_id: str, web: WebRuntime = Depends(require_auth)):
        from aero.experiments import ExperimentError, ExperimentManager

        try:
            return ExperimentManager(web.project_dir).delete(experiment_id)
        except ExperimentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/checkpoints")
    async def list_checkpoints(web: WebRuntime = Depends(require_auth)):
        from aero.checkpoints import CheckpointManager

        return {"checkpoints": CheckpointManager(web.project_dir).list()}

    @app.post("/api/v1/checkpoints")
    async def create_checkpoint(body: CheckpointRequest, web: WebRuntime = Depends(require_auth)):
        from aero.checkpoints import CheckpointError, CheckpointManager

        try:
            return CheckpointManager(web.project_dir).create(name=body.name, kind="manual")
        except CheckpointError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/checkpoints/{checkpoint_id}/diff")
    async def checkpoint_diff(checkpoint_id: str, web: WebRuntime = Depends(require_auth)):
        from aero.checkpoints import CheckpointError, CheckpointManager

        try:
            return CheckpointManager(web.project_dir).diff(checkpoint_id).to_dict()
        except CheckpointError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/v1/checkpoints/{checkpoint_id}")
    async def delete_checkpoint(checkpoint_id: str, web: WebRuntime = Depends(require_auth)):
        from aero.checkpoints import CheckpointError, CheckpointManager

        try:
            return CheckpointManager(web.project_dir).delete(checkpoint_id)
        except CheckpointError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/paper")
    async def paper_status(web: WebRuntime = Depends(require_auth)):
        from aero.paper_versions import PaperVersionManager

        manager = PaperVersionManager(web.project_dir)
        return {"status": manager.status(), "versions": manager.list()}

    @app.post("/api/v1/paper/initialize")
    async def initialize_paper(web: WebRuntime = Depends(require_auth)):
        from aero.paper_versions import PaperVersionError, PaperVersionManager

        try:
            return PaperVersionManager(web.project_dir).initialize()
        except PaperVersionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/paper/save")
    async def save_paper(body: PaperSaveRequest, web: WebRuntime = Depends(require_auth)):
        from aero.paper_versions import PaperVersionError, PaperVersionManager

        try:
            return PaperVersionManager(web.project_dir).save(body.title)
        except PaperVersionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/paper/diff")
    async def paper_diff(web: WebRuntime = Depends(require_auth)):
        from aero.paper_versions import PaperVersionError, PaperVersionManager

        try:
            return PaperVersionManager(web.project_dir).diff().to_dict()
        except PaperVersionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/memos")
    async def list_memos(web: WebRuntime = Depends(require_auth)):
        from aero.data.memos import MemoStore

        return {"memos": MemoStore(web.project_dir).list()}

    @app.post("/api/v1/memos")
    async def create_memo(body: MemoRequest, web: WebRuntime = Depends(require_auth)):
        from aero.data.memos import MemoError, MemoStore

        try:
            memo, created = MemoStore(web.project_dir).add(
                title=body.title,
                content=body.content,
                evidence=body.evidence,
                tags=body.tags,
            )
            return {"memo": memo, "created": created}
        except MemoError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/sessions/{session_id}/subagents")
    async def list_subagents(session: LocalSession = Depends(get_session)):
        return session.subagents.snapshot()

    static_dir = Path(__file__).with_name("static")
    assets_dir = static_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir, follow_symlink=False), name="assets")

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        candidate = static_dir / path
        if candidate.is_file() and static_dir in candidate.resolve().parents:
            return FileResponse(candidate)
        index_path = static_dir / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        return JSONResponse({"message": "Web UI 资源尚未构建"}, status_code=503)

    return app, runtime
