"""Isolated experiment workspaces for bounded research attempts."""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

EXPERIMENT_DIRECTORIES = (
    "scripts",
    "figures",
    "plans",
    "outputs",
    "reports",
    "data",
    "literature",
    "tmp",
)


class ExperimentError(RuntimeError):
    """Raised when an experiment operation cannot be completed."""


class ExperimentManager:
    """Create and manage project-local experiment workspaces."""

    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir).resolve()
        self.aero_dir = self.project_dir / ".aero"
        self.metadata_dir = self.aero_dir / "experiments"
        self.state_path = self.aero_dir / "experiment-state.json"
        self.workspaces_dir = self.project_dir / "experiments"

    def create(
        self,
        name: str,
        *,
        base_checkpoint: str | None = None,
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ExperimentError("实验名称不能为空。")
        state = self._read_state()
        experiment_id = self._new_id()
        workspace = self._new_workspace(name, experiment_id)
        for directory in EXPERIMENT_DIRECTORIES:
            (workspace / directory).mkdir(parents=True, exist_ok=True)

        now = time.time()
        metadata: dict[str, Any] = {
            "version": 1,
            "id": experiment_id,
            "name": name,
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "workspace": workspace.relative_to(self.project_dir).as_posix(),
            "base_checkpoint": base_checkpoint,
            "parent_experiment_id": state.get("active_experiment"),
            "report": None,
        }
        self._write_metadata(metadata)
        state["active_experiment"] = experiment_id
        self._write_state(state)
        return metadata

    def list(self) -> list[dict[str, Any]]:
        if not self.metadata_dir.exists():
            return []
        experiments: list[dict[str, Any]] = []
        for path in self.metadata_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("id"):
                experiments.append(data)
        experiments.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
        return experiments

    def load(self, experiment: str | None) -> dict[str, Any] | None:
        if not experiment:
            return None
        exact = self.metadata_dir / f"{experiment}.json"
        if exact.is_file():
            return json.loads(exact.read_text(encoding="utf-8"))
        matches = [
            item
            for item in self.list()
            if item["id"].startswith(experiment) or item.get("name") == experiment
        ]
        if len(matches) > 1:
            raise ExperimentError(f"实验名称不唯一，请使用实验 ID：{experiment}")
        return matches[0] if matches else None

    def active(self) -> dict[str, Any] | None:
        return self.load(self._read_state().get("active_experiment"))

    def switch(self, experiment: str) -> dict[str, Any]:
        metadata = self.load(experiment)
        if metadata is None:
            raise ExperimentError(f"找不到实验：{experiment}")
        state = self._read_state()
        state["active_experiment"] = metadata["id"]
        self._write_state(state)
        metadata["updated_at"] = time.time()
        self._write_metadata(metadata)
        return metadata

    def leave(self) -> None:
        """Return to the main project workspace."""
        state = self._read_state()
        state["active_experiment"] = None
        self._write_state(state)

    def complete(self, experiment: str, report: str) -> dict[str, Any]:
        metadata = self.load(experiment)
        if metadata is None:
            raise ExperimentError(f"找不到实验：{experiment}")
        workspace = self.workspace_path(metadata)
        report_path = workspace / "reports" / "final-report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report.rstrip() + "\n", encoding="utf-8")

        now = time.time()
        metadata["status"] = "completed"
        metadata["completed_at"] = now
        metadata["updated_at"] = now
        metadata["report"] = report_path.relative_to(self.project_dir).as_posix()
        self._write_metadata(metadata)

        return metadata

    def delete(self, experiment: str) -> dict[str, Any]:
        """Permanently remove one experiment workspace and its metadata."""
        metadata = self.load(experiment)
        if metadata is None:
            raise ExperimentError(f"找不到实验：{experiment}")
        workspace = self.workspace_path(metadata)
        if workspace.is_symlink():
            workspace.unlink()
        elif workspace.exists():
            shutil.rmtree(workspace)
        metadata_path = self.metadata_dir / f"{metadata['id']}.json"
        if metadata_path.exists():
            metadata_path.unlink()

        state = self._read_state()
        if state.get("active_experiment") == metadata["id"]:
            parent_id = metadata.get("parent_experiment_id")
            state["active_experiment"] = parent_id if self.load(parent_id) else None
            self._write_state(state)
        return metadata

    def clear(self) -> list[dict[str, Any]]:
        """Permanently remove every experiment and reset active state."""
        experiments = self.list()
        for metadata in experiments:
            workspace = self.workspace_path(metadata)
            if workspace.is_symlink():
                workspace.unlink()
            elif workspace.exists():
                shutil.rmtree(workspace)
        if self.metadata_dir.exists():
            shutil.rmtree(self.metadata_dir)
        self._write_state({"active_experiment": None})
        if self.workspaces_dir.exists() and not any(self.workspaces_dir.iterdir()):
            self.workspaces_dir.rmdir()
        return experiments

    def workspace_path(self, experiment: dict[str, Any] | str) -> Path:
        metadata = self.load(experiment) if isinstance(experiment, str) else experiment
        if metadata is None:
            raise ExperimentError(f"找不到实验：{experiment}")
        path = (self.project_dir / metadata["workspace"]).resolve()
        try:
            path.relative_to(self.workspaces_dir.resolve())
        except ValueError as exc:
            raise ExperimentError("实验工作区路径不安全。") from exc
        return path

    def artifacts(self, experiment: dict[str, Any] | str) -> list[str]:
        workspace = self.workspace_path(experiment)
        artifacts: list[str] = []
        for path in sorted(workspace.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(workspace).as_posix()
            if ".aero" in path.relative_to(workspace).parts:
                continue
            if relative == "reports/final-report.md":
                continue
            artifacts.append(relative)
        return artifacts

    def _new_workspace(self, name: str, experiment_id: str) -> Path:
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        slug = _experiment_slug(name) or "experiment"
        suffix = experiment_id.rsplit("-", 1)[-1]
        return self.workspaces_dir / f"{slug}-{suffix}"

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"active_experiment": None}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"active_experiment": None}
        except (OSError, ValueError):
            return {"active_experiment": None}

    def _write_state(self, state: dict[str, Any]) -> None:
        self._write_json(self.state_path, state)

    def _write_metadata(self, metadata: dict[str, Any]) -> None:
        self._write_json(self.metadata_dir / f"{metadata['id']}.json", metadata)

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _new_id() -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        return f"exp-{stamp}-{uuid.uuid4().hex[:8]}"


def _experiment_slug(name: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", name.strip(), flags=re.UNICODE)
    return slug.strip("-_")[:40]
