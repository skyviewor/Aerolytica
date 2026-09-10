"""A single local web conversation and its Agent lifecycle."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from aero.agent.checkpoint_context import use_checkpoint_creator
from aero.agent.llm_client import LLMClient, LLMConfig
from aero.agent.loop import AgentLoop
from aero.agent.session import SessionManager, SessionMeta
from aero.agent.subagent import (
    SubAgentManager,
    use_subagent_canceller,
    use_subagent_launcher,
    use_subagent_status_provider,
)
from aero.application.events import RunEvent, RunState
from aero.application.session_titles import (
    fallback_session_title,
    normalize_session_title,
    session_title_prompt,
)
from aero.checkpoints import CheckpointManager
from aero.core.config import AeroConfig
from aero.core.types import Message
from aero.data.plans import use_session_id
from aero.toolbox.file_access import use_read_files
from aero.toolbox.paths import use_workspace
from aero.toolbox.secret_input import use_secret_input_provider

_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_.-]{6,}\b", re.IGNORECASE)
_EVENT_BUFFER_SIZE = 2_000


def _safe_text(value: Any) -> str:
    return _SECRET_RE.sub("[API_KEY_REDACTED]", str(value or ""))


def _message_view(message: Message) -> dict[str, Any]:
    return {"role": message.role, "content": _safe_text(message.content)}


class LocalSession:
    """Own one AgentLoop and expose a concurrency-safe run interface."""

    def __init__(
        self,
        project_dir: Path,
        config: AeroConfig,
        session_id: str | None = None,
        *,
        session_manager: SessionManager | None = None,
    ) -> None:
        self.project_dir = project_dir.resolve()
        self.config = config
        self.id = session_id or uuid.uuid4().hex[:12]
        self.session_manager = session_manager or SessionManager()
        self.agent = AgentLoop(config)
        self.subagents = SubAgentManager()
        self.lock = asyncio.Lock()
        # Hosting installs a fenced cloud project guard; foreground and background
        # callers still use this same object and the same local project OS lock.
        self.execution_guard = None
        self._events: dict[str, deque[RunEvent]] = {}
        self._event_counters: dict[str, int] = {}
        self._event_waiters: dict[str, asyncio.Condition] = {}
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        self._run_states: dict[str, RunState] = {}
        self._run_errors: dict[str, str] = {}
        self._title_tasks: dict[str, asyncio.Task[None]] = {}
        self._title_pending: dict[str, bool] = {}
        self._pending_secrets: dict[str, asyncio.Future[str]] = {}
        self._secret_values: dict[str, str] = {}
        self._session_meta = SessionMeta(
            id=self.id,
            project_dir=str(self.project_dir),
            model=config.llm.model,
            provider=config.llm.provider,
            vision_model=config.vision.model,
            mode=config.mode,
        )
        self._load_saved_session()

    def _load_saved_session(self) -> None:
        loaded = self.session_manager.load(self.id)
        if loaded is None:
            return
        messages, meta = loaded
        self.agent.messages = messages
        self._session_meta = meta

    def messages_view(self) -> list[dict[str, Any]]:
        return [
            _message_view(message)
            for message in self.agent.messages
            if message.role in {"user", "assistant"}
        ]

    def restore_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace conversation context after an explicit cloud-memory restore."""
        if self.metadata()["active_runs"]:
            raise RuntimeError("cannot_restore_active_session")
        restored: list[Message] = []
        for item in messages:
            role = str(item.get("role", ""))
            content = str(item.get("content", ""))
            if role in {"user", "assistant"} and content:
                restored.append(Message(role=role, content=_safe_text(content)))
        self.agent.messages = restored
        self._save()

    def metadata(self) -> dict[str, Any]:
        active_runs = [
            run_id
            for run_id, state in self._run_states.items()
            if state
            in {
                RunState.QUEUED,
                RunState.RUNNING,
                RunState.WAITING_CONFIRMATION,
                RunState.WAITING_SECRET,
                RunState.CANCELLING,
            }
        ]
        return {
            "id": self.id,
            "name": self._session_meta.name or "新会话",
            "created_at": self._session_meta.created_at,
            "updated_at": self._session_meta.updated_at,
            "message_count": len(self.agent.messages),
            "model": self.config.llm.model,
            "provider": self.config.llm.provider,
            "mode": self.config.mode,
            "title_source": self._session_meta.title_source,
            "active_runs": active_runs,
            "messages": self.messages_view(),
        }

    def _save(self) -> None:
        if not self._session_meta.name and any(
            message.role == "user" and message.content.strip() for message in self.agent.messages
        ):
            self._session_meta.name = fallback_session_title(self.agent.messages) or self.id
            self._session_meta.title_source = "pending"
        self._session_meta.project_dir = str(self.project_dir)
        self._session_meta.model = self.config.llm.model
        self._session_meta.provider = self.config.llm.provider
        self._session_meta.vision_model = self.config.vision.model
        self._session_meta.mode = self.config.mode
        self.session_manager.save(self.id, self.agent.messages, self._session_meta)

    def _notify(self, run_id: str) -> None:
        condition = self._condition(run_id)

        async def notify() -> None:
            async with condition:
                condition.notify_all()

        asyncio.create_task(notify())

    def update_config(self, config: AeroConfig) -> None:
        """Apply workspace settings to a session that was created earlier."""
        self.config = config.model_copy(deep=True)
        self.agent.config = self.config
        self.agent.llm.config.provider = self.config.llm.provider
        self.agent.llm.config.model = self.config.llm.model
        self.agent.llm.config.base_url = self.config.llm.base_url
        self.agent.llm.config.reasoning_effort = self.config.llm.reasoning_effort
        self.agent.llm.config.api_key = self.config.llm.active_api_key()

    def _condition(self, run_id: str) -> asyncio.Condition:
        return self._event_waiters.setdefault(run_id, asyncio.Condition())

    def _emit(self, run_id: str, event_type: str, data: dict[str, Any] | None = None) -> RunEvent:
        event_id = self._event_counters.get(run_id, 0) + 1
        event = RunEvent(
            id=event_id,
            session_id=self.id,
            run_id=run_id,
            type=event_type,
            data=data or {},
        )
        self._event_counters[run_id] = event_id
        self._events.setdefault(run_id, deque(maxlen=_EVENT_BUFFER_SIZE)).append(event)
        self._notify(run_id)
        return event

    async def events(self, run_id: str, after_id: int = 0) -> AsyncIterator[RunEvent]:
        """Yield buffered and future events for one run."""
        condition = self._condition(run_id)
        cursor = after_id
        while True:
            buffered = [event for event in self._events.get(run_id, ()) if event.id > cursor]
            for event in buffered:
                cursor = event.id
                yield event
            if (
                self._run_states.get(run_id)
                in {
                    RunState.CANCELLED,
                    RunState.COMPLETED,
                    RunState.FAILED,
                }
                and not buffered
                and not self._title_pending.get(run_id, False)
            ):
                return
            async with condition:
                await condition.wait_for(
                    lambda: (
                        self._event_counters.get(run_id, 0) > cursor
                        or self._run_states.get(run_id)
                        in {
                            RunState.CANCELLED,
                            RunState.COMPLETED,
                            RunState.FAILED,
                        }
                    )
                )

    def run_status(self, run_id: str) -> dict[str, Any]:
        state = self._run_states.get(run_id)
        if state is None:
            return {"status": "not_found", "run_id": run_id}
        return {
            "run_id": run_id,
            "session_id": self.id,
            "state": state.value,
            "error": self._run_errors.get(run_id, ""),
            "last_event_id": self._event_counters.get(run_id, 0),
        }

    def start_run(self, prompt: str) -> str:
        if any(
            state
            in {
                RunState.QUEUED,
                RunState.RUNNING,
                RunState.WAITING_CONFIRMATION,
                RunState.WAITING_SECRET,
                RunState.CANCELLING,
            }
            for state in self._run_states.values()
        ):
            raise RuntimeError("session_busy")
        run_id = f"run_{secrets.token_urlsafe(10)}"
        self._run_states[run_id] = RunState.QUEUED
        self._emit(
            run_id, "run_started", {"state": RunState.QUEUED.value, "prompt": _safe_text(prompt)}
        )
        task = asyncio.create_task(self._run(run_id, prompt))
        self._run_tasks[run_id] = task
        return run_id

    @asynccontextmanager
    async def _project_lock(self):
        from aero.core.daemon import WorkspaceLock

        lock = WorkspaceLock(self.project_dir / ".aero" / "runtime" / "execution.lock")
        while True:
            try:
                lock.acquire()
                break
            except BlockingIOError:
                await asyncio.sleep(0.1)
        try:
            yield
        finally:
            lock.release()

    async def _run(self, run_id: str, prompt: str) -> None:
        try:
            async with self._project_lock():
                async with AsyncExitStack() as stack:
                    if self.execution_guard is not None:
                        await stack.enter_async_context(self.execution_guard(run_id))
                    await self._run_in_project(run_id, prompt)
        except asyncio.CancelledError:
            if self._run_states.get(run_id) != RunState.CANCELLED:
                self._run_states[run_id] = RunState.CANCELLED
                self._emit(run_id, "run_cancelled", {"state": RunState.CANCELLED.value})
            raise
        except Exception as exc:
            self._run_states[run_id] = RunState.FAILED
            self._run_errors[run_id] = _safe_text(exc)
            self._emit(run_id, "error", {"message": _safe_text(exc)})
            self._emit(run_id, "run_completed", {"state": RunState.FAILED.value})
        finally:
            self._run_tasks.pop(run_id, None)

    async def wait_run(self, run_id: str) -> None:
        """Wait until execution has actually quiesced, including cancellation."""
        task = self._run_tasks.get(run_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _run_in_project(self, run_id: str, prompt: str) -> None:
        self._run_states[run_id] = RunState.RUNNING
        self._emit(run_id, "run_state", {"state": RunState.RUNNING.value})
        workspace = self.project_dir
        active_experiment = None
        try:
            from aero.experiments import ExperimentManager

            active_experiment = ExperimentManager(self.project_dir).active()
            if active_experiment:
                workspace = ExperimentManager(self.project_dir).workspace_path(active_experiment)
        except Exception:
            active_experiment = None

        async with self.lock:
            async with AsyncExitStack() as stack:
                stack.enter_context(use_workspace(self.project_dir, workspace))
                stack.enter_context(use_session_id(self.id))
                stack.enter_context(use_read_files())
                stack.enter_context(use_subagent_launcher(self._launch_subagent))
                stack.enter_context(use_subagent_status_provider(self._query_subagents))
                stack.enter_context(use_subagent_canceller(self._cancel_subagent))
                stack.enter_context(
                    use_secret_input_provider(self._request_secret, self._resolve_secret)
                )
                stack.enter_context(use_checkpoint_creator(self._create_checkpoint))
                try:
                    response_text = ""
                    async for event in self.agent.run_stream(prompt):
                        if event.type == "text":
                            response_text += event.content
                            self._emit(
                                run_id, "assistant_delta", {"text": _safe_text(event.content)}
                            )
                        elif event.type == "status":
                            status = _safe_text(event.content)
                            if status.startswith("{"):
                                try:
                                    payload = json.loads(status)
                                except json.JSONDecodeError:
                                    payload = {}
                                if payload.get("setup_required"):
                                    if payload.get("setup_required") == "vision":
                                        # Vision setup is optional.  It must not enter the
                                        # generic secret-input state: no pending secret future
                                        # exists for this tool, and doing so leaves the Web run
                                        # apparently stuck forever.
                                        request = payload.get("credential_request")
                                        message = "当前任务需要视觉模型，但尚未配置视觉能力。"
                                        if isinstance(request, dict):
                                            message = str(request.get("message") or message)
                                        self._emit(
                                            run_id,
                                            "vision_setup_required",
                                            {"message": message, "setup_required": "vision"},
                                        )
                                    else:
                                        self._run_states[run_id] = RunState.WAITING_SECRET
                                        self._emit(run_id, "secret_required", payload)
                                    continue
                            self._emit(run_id, "tool_progress", {"text": status})
                        elif event.type == "confirm":
                            self._run_states[run_id] = RunState.WAITING_CONFIRMATION
                            try:
                                payload = json.loads(event.content)
                            except json.JSONDecodeError:
                                payload = {"message": _safe_text(event.content)}
                            self._emit(run_id, "confirmation_required", payload)
                            choice = await self._wait_for_confirmation(run_id)
                            if (
                                payload.get("tool") == "propose_execution"
                                and choice in {"allow", "always"}
                                and self.config.mode == "plan"
                            ):
                                self.config.mode = "execute"
                                self.agent.config.mode = "execute"
                                self.agent.reset_system_prompt(self.config.language)
                            self._run_states[run_id] = RunState.RUNNING
                            self._emit(run_id, "run_state", {"state": RunState.RUNNING.value})
                        elif event.type == "content_blocked":
                            self._emit(
                                run_id, "content_blocked", {"text": _safe_text(event.content)}
                            )
                    self._run_states[run_id] = RunState.COMPLETED
                    self._save()
                    self._schedule_title_generation(run_id)
                    self._emit(
                        run_id,
                        "run_completed",
                        {
                            "state": RunState.COMPLETED.value,
                            "title_pending": self._title_pending.get(run_id, False),
                        },
                    )
                except asyncio.CancelledError:
                    self._run_states[run_id] = RunState.CANCELLED
                    self._emit(run_id, "run_cancelled", {"state": RunState.CANCELLED.value})
                    raise
                except Exception as exc:
                    self._run_states[run_id] = RunState.FAILED
                    self._run_errors[run_id] = _safe_text(exc)
                    self._emit(run_id, "error", {"message": _safe_text(exc)})
                    self._emit(run_id, "run_completed", {"state": RunState.FAILED.value})

    def _schedule_title_generation(self, run_id: str) -> None:
        if (
            self._session_meta.title_source != "pending"
            or run_id in self._title_tasks
            or not self.config.llm.active_api_key()
        ):
            return
        self._title_pending[run_id] = True
        task = asyncio.create_task(self._generate_title(run_id))
        self._title_tasks[run_id] = task

    async def _generate_title(self, run_id: str) -> None:
        try:
            if not self.config.llm.active_api_key():
                return
            client = LLMClient(
                LLMConfig(
                    provider=self.config.llm.provider,
                    model=self.config.llm.model,
                    reasoning_effort=self.config.llm.reasoning_effort,
                    api_key=self.config.llm.active_api_key(),
                    base_url=self.config.llm.base_url,
                )
            )
            try:
                title = await client.chat(
                    [
                        Message(
                            role="user",
                            content=session_title_prompt(self.agent.messages, self.config.language),
                        )
                    ]
                )
            finally:
                await client.close()
            title = normalize_session_title(title)
            if not title or self._session_meta.title_source == "manual":
                return
            self._session_meta.name = title
            self._session_meta.title_source = "auto"
            self._save()
            self._emit(run_id, "session_title_updated", {"name": title, "title_source": "auto"})
        except asyncio.CancelledError:
            raise
        except Exception:
            # Title generation is best-effort; the deterministic fallback remains.
            self._emit(run_id, "session_title_failed", {})
            return
        finally:
            self._title_pending[run_id] = False
            self._title_tasks.pop(run_id, None)
            self._notify(run_id)

    async def _wait_for_confirmation(self, run_id: str) -> str:
        while self.agent.confirm_future is None:
            if self._run_states.get(run_id) == RunState.CANCELLING:
                raise asyncio.CancelledError
            await asyncio.sleep(0.01)
        future = self.agent.confirm_future
        if future is None:
            raise RuntimeError("confirmation_not_pending")
        return str(await future)

    def confirm(self, run_id: str, choice: str) -> None:
        if choice not in {"allow", "always", "deny"}:
            raise ValueError("invalid_confirmation")
        future = self.agent.confirm_future
        if future is None or self._run_states.get(run_id) != RunState.WAITING_CONFIRMATION:
            raise RuntimeError("confirmation_not_pending")
        future.set_result({"allow": "allow", "always": "always", "deny": "deny"}[choice])
        self._emit(run_id, "confirmation_resolved", {"choice": choice})

    async def cancel(self, run_id: str) -> None:
        state = self._run_states.get(run_id)
        if state is None:
            raise KeyError(run_id)
        task = self._run_tasks.get(run_id)
        if task is None or task.done():
            self._run_states[run_id] = RunState.CANCELLED
            self._emit(run_id, "run_cancelled", {"state": RunState.CANCELLED.value})
            return
        self._run_states[run_id] = RunState.CANCELLING
        self.agent.cancel()
        task.cancel()
        self._emit(run_id, "run_state", {"state": RunState.CANCELLING.value})

    async def _request_secret(self, request: Any) -> dict[str, Any]:
        request_id = secrets.token_urlsafe(12)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_secrets[request_id] = future
        self._emit_for_active_run(
            "secret_required",
            {
                "request_id": request_id,
                "scope": request.scope,
                "title": request.title,
                "instructions": request.instructions,
                "multiline": request.multiline,
            },
        )
        try:
            handle = await future
            return {"status": "ready", "secret_handle": handle}
        finally:
            self._pending_secrets.pop(request_id, None)

    def submit_secret(self, request_id: str, secret: str) -> None:
        future = self._pending_secrets.get(request_id)
        if future is None or future.done():
            raise RuntimeError("secret_not_pending")
        handle = secrets.token_urlsafe(18)
        self._secret_values[handle] = secret
        future.set_result(handle)

    def _resolve_secret(self, handle: str) -> str | None:
        return self._secret_values.pop(handle, None)

    def _emit_for_active_run(self, event_type: str, data: dict[str, Any]) -> None:
        active = next(
            (
                run_id
                for run_id, state in self._run_states.items()
                if state
                in {
                    RunState.RUNNING,
                    RunState.WAITING_SECRET,
                    RunState.WAITING_CONFIRMATION,
                }
            ),
            None,
        )
        if active:
            self._run_states[active] = RunState.WAITING_SECRET
            self._emit(active, event_type, data)

    def rename(self, name: str) -> None:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("empty_name")
        self._session_meta.name = cleaned[:120]
        self._session_meta.title_source = "manual"
        self._save()

    def _create_checkpoint(self, name: str) -> dict[str, Any]:
        manager = CheckpointManager(self.project_dir)
        self._save()
        snapshot = self.session_manager.snapshot(self.id)
        metadata = manager.create(
            name=name or "Web 会话检查点",
            session_id=self.id,
            session_snapshot=snapshot,
            model={"provider": self.config.llm.provider, "model": self.config.llm.model},
            kind="agent",
        )
        return {"success": True, **metadata}

    def _query_subagents(self, task_id: str | None = None) -> dict[str, Any]:
        return self.subagents.snapshot(task_id)

    def _cancel_subagent(self, task_id: str) -> dict[str, Any]:
        task = self.subagents.get(task_id)
        if task is None:
            return {"status": "not_found", "message": f"未找到后台任务 #{task_id}"}
        self.subagents.cancel(task_id)
        return {"status": "cancelled", "task_id": task_id}

    def _launch_subagent(
        self, title: str, task: str, success_criteria: str, context_summary: str
    ) -> dict[str, Any]:
        subtask = self.subagents.create(
            title=title,
            description=task,
            success_criteria=success_criteria,
            context_summary=context_summary,
        )
        asyncio.create_task(self._run_subagent(subtask.id, task))
        self._emit_for_active_run("subagent_updated", self.subagents.snapshot(subtask.id))
        return {"status": "started", "task_id": subtask.id, "title": subtask.title}

    async def _run_subagent(self, task_id: str, prompt: str) -> None:
        task = self.subagents.get(task_id)
        if task is None:
            return
        sub_agent = AgentLoop(self.config)
        task.agent = sub_agent
        response = ""
        try:
            async for event in sub_agent.run_stream(prompt):
                if event.type == "text":
                    response += event.content
                elif event.type == "status":
                    self.subagents.update(task_id, _safe_text(event.content))
                    self._emit_for_active_run("subagent_updated", self.subagents.snapshot(task_id))
            self.subagents.finish(task_id, result_summary=response)
        except asyncio.CancelledError:
            self.subagents.cancel(task_id)
        except Exception as exc:
            self.subagents.fail(task_id, _safe_text(exc))
        self._emit_for_active_run("subagent_updated", self.subagents.snapshot(task_id))

    async def close(self) -> None:
        for task in self._run_tasks.values():
            if not task.done():
                task.cancel()
        for task in self._title_tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(
            *self._run_tasks.values(), *self._title_tasks.values(), return_exceptions=True
        )
        await self.agent.close()
