"""Background sub-agent task state and launch bridge."""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from meteora.core.debug_log import debug_log

SubAgentLauncher = Callable[[str, str, str, str], dict]
SubAgentStatusProvider = Callable[[str | None], dict]
SubAgentCanceller = Callable[[str], dict]

_SUBAGENT_LAUNCHER: contextvars.ContextVar[SubAgentLauncher | None] = (
    contextvars.ContextVar("meteora_subagent_launcher", default=None)
)
_SUBAGENT_STATUS_PROVIDER: contextvars.ContextVar[SubAgentStatusProvider | None] = (
    contextvars.ContextVar("meteora_subagent_status_provider", default=None)
)
_SUBAGENT_CANCELLER: contextvars.ContextVar[SubAgentCanceller | None] = (
    contextvars.ContextVar("meteora_subagent_canceller", default=None)
)

_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


@dataclass
class SubAgentTask:
    id: str
    title: str
    description: str
    success_criteria: str
    context_summary: str
    status: str = "running"
    latest_status: str = "已开始后台任务"
    result_summary: str = ""
    error: str = ""
    artifacts: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    agent: Any | None = field(default=None, repr=False, compare=False)
    confirm_payload: str = ""

    @property
    def active(self) -> bool:
        return self.status in {"running", "paused"}


class SubAgentManager:
    """In-memory registry for background sub-agent tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, SubAgentTask] = {}
        self._counter = 0

    def create(
        self,
        *,
        title: str,
        description: str,
        success_criteria: str = "",
        context_summary: str = "",
    ) -> SubAgentTask:
        self._counter += 1
        task = SubAgentTask(
            id=str(self._counter),
            title=title.strip() or f"后台任务 {self._counter}",
            description=description.strip(),
            success_criteria=success_criteria.strip(),
            context_summary=context_summary.strip(),
        )
        self._tasks[task.id] = task
        debug_log("subagent.created", task_id=task.id, title=task.title)
        return task

    def get(self, task_id: str) -> SubAgentTask | None:
        return self._tasks.get(task_id)

    def list(self) -> list[SubAgentTask]:
        return list(self._tasks.values())

    def active(self) -> list[SubAgentTask]:
        return [task for task in self._tasks.values() if task.active]

    def snapshot(self, task_id: str | None = None) -> dict:
        if task_id:
            task = self._tasks.get(task_id)
            if task is None:
                return {
                    "status": "not_found",
                    "message": f"未找到后台任务 #{task_id}。",
                    "tasks": [],
                }
            tasks = [task]
        else:
            tasks = self.list()
        return {
            "status": "success",
            "total": len(tasks),
            "active_count": sum(1 for task in tasks if task.active),
            "tasks": [_task_to_dict(task) for task in tasks],
        }

    def update(self, task_id: str, latest_status: str, *, status: str = "running") -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = status
        task.latest_status = latest_status.strip() or task.latest_status
        debug_log("subagent.updated", task_id=task.id, status=task.status)

    def pause(self, task_id: str, confirm_payload: str) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = "paused"
        task.latest_status = "等待确认"
        task.confirm_payload = confirm_payload
        debug_log("subagent.paused", task_id=task.id)

    def finish(
        self,
        task_id: str,
        *,
        result_summary: str = "",
        artifacts: list[str] | None = None,
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = "completed"
        task.latest_status = "已完成"
        task.result_summary = result_summary.strip()
        task.artifacts = artifacts or []
        task.completed_at = time.time()
        debug_log("subagent.finished", task_id=task.id)

    def fail(self, task_id: str, error: str) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = "failed"
        task.latest_status = "执行失败"
        task.error = error
        task.completed_at = time.time()
        debug_log("subagent.failed", task_id=task.id, error=error[:120])

    def cancel(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = "cancelled"
        task.latest_status = "已取消"
        task.completed_at = time.time()
        if task.agent is not None:
            task.agent.cancel()
        debug_log("subagent.cancelled", task_id=task.id)

    def footer_text(self, frame: int) -> str:
        active = self.active()
        if not active:
            return ""
        task = active[(frame // 12) % len(active)]
        spinner = _SPINNER[frame % len(_SPINNER)]
        label = task.latest_status if task.status != "paused" else "等待确认"
        return f"{spinner} #{task.id} {label}"


@contextmanager
def use_subagent_launcher(launcher: SubAgentLauncher):
    token = _SUBAGENT_LAUNCHER.set(launcher)
    try:
        yield
    finally:
        _SUBAGENT_LAUNCHER.reset(token)


@contextmanager
def use_subagent_status_provider(provider: SubAgentStatusProvider):
    token = _SUBAGENT_STATUS_PROVIDER.set(provider)
    try:
        yield
    finally:
        _SUBAGENT_STATUS_PROVIDER.reset(token)


@contextmanager
def use_subagent_canceller(canceller: SubAgentCanceller):
    token = _SUBAGENT_CANCELLER.set(canceller)
    try:
        yield
    finally:
        _SUBAGENT_CANCELLER.reset(token)


def launch_subagent_from_context(
    title: str,
    task: str,
    success_criteria: str,
    context_summary: str,
) -> dict:
    launcher = _SUBAGENT_LAUNCHER.get()
    if launcher is None:
        return {
            "status": "unavailable",
            "message": "当前运行环境不支持后台子任务。",
        }
    return launcher(title, task, success_criteria, context_summary)


def query_subagents_from_context(task_id: str | None = None) -> dict:
    provider = _SUBAGENT_STATUS_PROVIDER.get()
    if provider is None:
        return {
            "status": "unavailable",
            "message": "当前运行环境不支持查询后台任务。",
            "tasks": [],
        }
    return provider(task_id)


def cancel_subagent_from_context(task_id: str) -> dict:
    canceller = _SUBAGENT_CANCELLER.get()
    if canceller is None:
        return {
            "status": "unavailable",
            "message": "当前运行环境不支持取消后台任务。",
        }
    return canceller(task_id)


def _task_to_dict(task: SubAgentTask) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "latest_status": task.latest_status,
        "result_summary": task.result_summary,
        "error": task.error,
        "artifacts": task.artifacts,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "requires_confirmation": task.status == "paused",
    }
