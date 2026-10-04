"""Workspace checkpoints backed by an isolated, project-local Git object store."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

MAX_EXACT_FILE_SIZE = 50 * 1024 * 1024
_CONTROLLED_DIRS = {"figures", "plans", "scripts"}
_REFERENCE_DIRS = {"data", "literature"}
_CONTROLLED_SUFFIXES = {
    ".cfg",
    ".csv",
    ".ini",
    ".ipynb",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".pdf",
    ".png",
    ".py",
    ".r",
    ".rst",
    ".sh",
    ".svg",
    ".tex",
    ".toml",
    ".tsv",
    ".txt",
    ".webp",
    ".yaml",
    ".yml",
}
_CONTROLLED_NAMES = {".gitattributes", ".gitignore", "Makefile"}
_SENSITIVE_NAMES = {".cdsapirc", ".env", ".netrc", "keys.json", "secrets.yaml", "secrets.yml"}
_SKIP_DIRS = {
    ".aero",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "experiments",
}


class CheckpointError(RuntimeError):
    """Raised when a checkpoint cannot be created or restored safely."""


@dataclass(frozen=True)
class CheckpointDiff:
    modified: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    references_changed: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return any((self.modified, self.missing, self.added, self.references_changed))

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "modified": list(self.modified),
            "missing": list(self.missing),
            "added": list(self.added),
            "references_changed": list(self.references_changed),
        }


class CheckpointManager:
    """Create, compare, and restore checkpoints without touching the user's Git repo."""

    def __init__(
        self,
        project_dir: str | Path,
        *,
        git_binary: str = "git",
        experiment_id: str | None = None,
    ):
        self.project_dir = Path(project_dir).resolve()
        self.aero_dir = self.project_dir / ".aero"
        self.checkpoints_dir = self.aero_dir / "checkpoints"
        self.history_dir = self.aero_dir / "history.git"
        self.state_path = self.aero_dir / "checkpoint-state.json"
        self.git_binary = git_binary
        self.experiment_id = experiment_id

    def create(
        self,
        name: str = "",
        *,
        session_id: str | None = None,
        session_snapshot: bytes | None = None,
        model: dict[str, Any] | None = None,
        tool_ledger: list[dict[str, Any]] | None = None,
        kind: str = "manual",
        related_experiment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        state = self._read_state()
        checkpoint_id = self._new_id()
        created_at = time.time()
        files = self.scan_workspace()
        exact_paths = [item["path"] for item in files if item["restore"] == "exact"]
        parent_id = state.get("current_checkpoint")
        commit = None
        exact_restore = False
        git_error = ""
        if self._git_available():
            try:
                self._init_history()
                commit = self._commit_files(exact_paths, checkpoint_id)
                exact_restore = True
            except Exception as exc:
                git_error = str(exc)
        else:
            git_error = "Git is unavailable; controlled files were recorded but not snapshotted."

        checkpoint_dir = self.checkpoints_dir / checkpoint_id
        checkpoint_dir.mkdir(parents=True, exist_ok=False)
        session_file = None
        if session_snapshot:
            session_file = "session.enc"
            (checkpoint_dir / session_file).write_bytes(session_snapshot)
            os.chmod(checkpoint_dir / session_file, 0o600)

        metadata: dict[str, Any] = {
            "version": 1,
            "id": checkpoint_id,
            "name": name.strip()
            or time.strftime("检查点 %Y-%m-%d %H:%M", time.localtime(created_at)),
            "kind": kind,
            "created_at": created_at,
            "parent_id": parent_id,
            "scope": "experiment" if self.experiment_id else "main",
            "experiment": self.experiment_id or "main",
            "experiment_id": self.experiment_id,
            "related_experiment": related_experiment,
            "session_id": session_id,
            "session_file": session_file,
            "model": model or {},
            "files": files,
            "tool_ledger": tool_ledger or [],
            "commit": commit,
            "exact_restore": exact_restore,
            "git_error": git_error,
        }
        self._write_json(checkpoint_dir / "metadata.json", metadata)
        state["current_checkpoint"] = checkpoint_id
        state.setdefault("experiment", "main")
        self._write_state(state)
        return metadata

    def list(self) -> list[dict[str, Any]]:
        if not self.checkpoints_dir.exists():
            return []
        checkpoints = []
        for path in self.checkpoints_dir.iterdir():
            metadata_path = path / "metadata.json"
            if not metadata_path.is_file():
                continue
            try:
                checkpoints.append(json.loads(metadata_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        checkpoints.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        return checkpoints

    def load(self, checkpoint: str | None) -> dict[str, Any] | None:
        if not checkpoint:
            return None
        exact = self.checkpoints_dir / checkpoint / "metadata.json"
        if exact.is_file():
            return json.loads(exact.read_text(encoding="utf-8"))
        matches = [
            item
            for item in self.list()
            if item["id"].startswith(checkpoint) or item.get("name") == checkpoint
        ]
        if len(matches) > 1:
            raise CheckpointError(f"检查点名称不唯一，请使用更完整的 ID：{checkpoint}")
        return matches[0] if matches else None

    def session_snapshot(self, checkpoint: str) -> bytes | None:
        metadata = self.load(checkpoint)
        if metadata is None or not metadata.get("session_file"):
            return None
        path = self.checkpoints_dir / metadata["id"] / metadata["session_file"]
        return path.read_bytes() if path.is_file() else None

    def delete(self, checkpoint: str) -> dict[str, Any]:
        """Delete one checkpoint record and repair the metadata graph."""
        metadata = self.load(checkpoint)
        if metadata is None:
            raise CheckpointError(f"找不到检查点：{checkpoint}")

        checkpoint_id = metadata["id"]
        parent_id = self._nearest_existing_parent(metadata.get("parent_id"), checkpoint_id)
        for child in self.list():
            if child.get("id") == checkpoint_id or child.get("parent_id") != checkpoint_id:
                continue
            child["parent_id"] = parent_id
            self._write_json(
                self.checkpoints_dir / child["id"] / "metadata.json",
                child,
            )

        state = self._read_state()
        if state.get("current_checkpoint") == checkpoint_id:
            state["current_checkpoint"] = parent_id
        if state.get("experiment_base") == checkpoint_id:
            state["experiment_base"] = parent_id

        checkpoint_dir = self.checkpoints_dir / checkpoint_id
        if checkpoint_dir.exists():
            shutil.rmtree(checkpoint_dir)
        self._write_state(state)

        if self.history_dir.exists() and self._git_available():
            self._ensure_checkpoint_refs()
            self._delete_checkpoint_ref(checkpoint_id)
            self._prune_history()
        return metadata

    def rename(self, checkpoint: str, name: str) -> dict[str, Any]:
        """Rename a checkpoint without changing its snapshot or graph identity."""
        name = name.strip()
        if not name:
            raise CheckpointError("检查点名称不能为空。")
        metadata = self.load(checkpoint)
        if metadata is None:
            raise CheckpointError(f"找不到检查点：{checkpoint}")
        metadata["name"] = name
        self._write_json(
            self.checkpoints_dir / metadata["id"] / "metadata.json",
            metadata,
        )
        return metadata

    def clear(self) -> list[dict[str, Any]]:
        """Remove every checkpoint snapshot and reset checkpoint pointers."""
        checkpoints = self.list()
        if self.checkpoints_dir.exists():
            shutil.rmtree(self.checkpoints_dir)
        if self.history_dir.exists():
            shutil.rmtree(self.history_dir)

        state = self._read_state()
        state["current_checkpoint"] = None
        state["experiment_base"] = None
        self._write_state(state)
        return checkpoints

    def diff(self, checkpoint: str) -> CheckpointDiff:
        metadata = self.load(checkpoint)
        if metadata is None:
            raise CheckpointError(f"找不到检查点：{checkpoint}")
        target = {
            item["path"]: item
            for item in metadata.get("files", [])
            if self._path_in_scope(item.get("path", ""))
        }
        current = {item["path"]: item for item in self.scan_workspace()}
        modified: list[str] = []
        missing: list[str] = []
        added: list[str] = []
        references_changed: list[str] = []

        for path, expected in target.items():
            actual = current.get(path)
            if actual is None:
                missing.append(path)
            elif actual.get("fingerprint") != expected.get("fingerprint"):
                if expected.get("restore") == "exact":
                    modified.append(path)
                else:
                    references_changed.append(path)
        for path, actual in current.items():
            if path not in target and actual.get("restore") == "exact":
                added.append(path)
        return CheckpointDiff(
            modified=tuple(sorted(modified)),
            missing=tuple(sorted(missing)),
            added=tuple(sorted(added)),
            references_changed=tuple(sorted(references_changed)),
        )

    def restore(self, checkpoint: str) -> dict[str, Any]:
        metadata = self.load(checkpoint)
        if metadata is None:
            raise CheckpointError(f"找不到检查点：{checkpoint}")
        if not metadata.get("exact_restore") or not metadata.get("commit"):
            raise CheckpointError("该检查点没有可用的文件快照，只保存了数据清单。")

        target_exact = {
            item["path"]
            for item in metadata.get("files", [])
            if item.get("restore") == "exact" and self._path_in_scope(item.get("path", ""))
        }
        current_exact = {
            item["path"] for item in self.scan_workspace() if item.get("restore") == "exact"
        }
        for relative in sorted(current_exact - target_exact):
            path = self._safe_project_path(relative)
            if path.is_file() or path.is_symlink():
                path.unlink()

        for relative in sorted(target_exact):
            content = self._git_show(metadata["commit"], relative)
            path = self._safe_project_path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        state = self._read_state()
        state["current_checkpoint"] = metadata["id"]
        state["experiment"] = f"恢复自「{metadata['name']}」"
        self._write_state(state)
        return metadata

    def start_experiment(self, name: str) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise CheckpointError("实验分支名称不能为空。")
        state = self._read_state()
        state["experiment"] = name
        state["experiment_base"] = state.get("current_checkpoint")
        self._write_state(state)
        return state

    def current_checkpoint_id(self) -> str | None:
        """Return the current checkpoint identifier, if any."""
        return self._read_state().get("current_checkpoint")

    def scan_workspace(self) -> list[dict[str, Any]]:
        files = []
        if not self.project_dir.exists():
            return files
        for root, dirnames, filenames in os.walk(self.project_dir):
            dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS)
            root_path = Path(root)
            for filename in sorted(filenames):
                path = root_path / filename
                if filename in _SENSITIVE_NAMES or path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(self.project_dir).as_posix()
                try:
                    size = path.stat().st_size
                    restore = self._restore_mode(relative, path, size)
                    files.append(
                        {
                            "path": relative,
                            "size": size,
                            "mtime_ns": path.stat().st_mtime_ns,
                            "restore": restore,
                            "fingerprint": self._fingerprint(path, full=restore == "exact"),
                        }
                    )
                except OSError:
                    continue
        return files

    def _restore_mode(self, relative: str, path: Path, size: int) -> str:
        first = relative.split("/", 1)[0]
        if first in _REFERENCE_DIRS or size > MAX_EXACT_FILE_SIZE:
            return "reference"
        if (
            first in _CONTROLLED_DIRS
            or path.name in _CONTROLLED_NAMES
            or path.suffix.lower() in _CONTROLLED_SUFFIXES
        ):
            return "exact"
        return "reference"

    def _path_in_scope(self, relative: str) -> bool:
        """Keep legacy main checkpoints from crossing into experiment workspaces."""
        if self.experiment_id is not None:
            return True
        return relative.split("/", 1)[0] != "experiments"

    @staticmethod
    def _fingerprint(path: Path, *, full: bool) -> str:
        digest = hashlib.sha256()
        size = path.stat().st_size
        if full or size <= 2 * 1024 * 1024:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return f"sha256:{digest.hexdigest()}"
        block = 1024 * 1024
        with path.open("rb") as handle:
            digest.update(handle.read(block))
            handle.seek(max(0, size - block))
            digest.update(handle.read(block))
        return f"partial-sha256:{size}:{digest.hexdigest()}"

    def _git_available(self) -> bool:
        try:
            result = subprocess.run(
                [self.git_binary, "--version"], capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _init_history(self) -> None:
        if (self.history_dir / "HEAD").exists():
            return
        self.aero_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [self.git_binary, "init", "--bare", str(self.history_dir)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise CheckpointError(result.stderr.strip() or "无法初始化私有文件历史。")

    def _commit_files(self, paths: list[str], checkpoint_id: str) -> str:
        index_path = self.aero_dir / f"index-{checkpoint_id}"
        env = dict(os.environ)
        env.update(
            {
                "GIT_DIR": str(self.history_dir),
                "GIT_WORK_TREE": str(self.project_dir),
                "GIT_INDEX_FILE": str(index_path),
                "GIT_AUTHOR_NAME": "Aero Checkpoints",
                "GIT_AUTHOR_EMAIL": "checkpoints@aero.local",
                "GIT_COMMITTER_NAME": "Aero Checkpoints",
                "GIT_COMMITTER_EMAIL": "checkpoints@aero.local",
            }
        )
        try:
            self._run_git(["read-tree", "--empty"], env=env)
            for chunk in _chunks(paths, 100):
                self._run_git(["add", "--", *chunk], env=env)
            tree = self._run_git(["write-tree"], env=env).strip()
            args = ["commit-tree", tree, "-m", f"checkpoint {checkpoint_id}"]
            commit = self._run_git(args, env=env).strip()
            self._run_git(
                ["update-ref", f"refs/checkpoints/{checkpoint_id}", commit],
                env=env,
            )
            return commit
        finally:
            index_path.unlink(missing_ok=True)

    def _ensure_checkpoint_refs(self) -> None:
        env = self._git_env()
        for item in self.list():
            commit = item.get("commit")
            checkpoint_id = item.get("id")
            if commit and checkpoint_id:
                try:
                    self._run_git(
                        ["update-ref", f"refs/checkpoints/{checkpoint_id}", commit],
                        env=env,
                    )
                except CheckpointError:
                    continue

    def _delete_checkpoint_ref(self, checkpoint_id: str) -> None:
        self._run_git(
            ["update-ref", "-d", f"refs/checkpoints/{checkpoint_id}"],
            env=self._git_env(),
        )

    def _prune_history(self) -> None:
        try:
            self._run_git(["reflog", "expire", "--expire=now", "--all"], env=self._git_env())
            self._run_git(["gc", "--prune=now"], env=self._git_env())
        except CheckpointError:
            # Logical deletion has already completed; space reclamation can be retried later.
            pass

    def _git_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "GIT_DIR": str(self.history_dir),
                "GIT_WORK_TREE": str(self.project_dir),
                "GIT_AUTHOR_NAME": "Aero Checkpoints",
                "GIT_AUTHOR_EMAIL": "checkpoints@aero.local",
                "GIT_COMMITTER_NAME": "Aero Checkpoints",
                "GIT_COMMITTER_EMAIL": "checkpoints@aero.local",
            }
        )
        return env

    def _nearest_existing_parent(self, parent_id: str | None, deleted_id: str) -> str | None:
        seen = {deleted_id}
        current = parent_id
        while current and current not in seen:
            seen.add(current)
            metadata = self.load(current)
            if metadata is not None and metadata.get("id") != deleted_id:
                return metadata["id"]
            current = metadata.get("parent_id") if metadata else None
        return None

    def _git_show(self, commit: str, relative: str) -> bytes:
        result = subprocess.run(
            [self.git_binary, f"--git-dir={self.history_dir}", "show", f"{commit}:{relative}"],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise CheckpointError(
                result.stderr.decode("utf-8", errors="replace").strip()
                or f"无法恢复文件：{relative}"
            )
        return result.stdout

    def _run_git(self, args: list[str], *, env: dict[str, str]) -> str:
        result = subprocess.run(
            [self.git_binary, *args],
            cwd=self.project_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or f"私有文件历史操作失败：{' '.join(args)}"
            raise CheckpointError(detail)
        return result.stdout

    def _safe_project_path(self, relative: str) -> Path:
        path = (self.project_dir / relative).resolve()
        try:
            path.relative_to(self.project_dir)
        except ValueError as exc:
            raise CheckpointError(f"检查点包含不安全路径：{relative}") from exc
        return path

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"experiment": "main", "current_checkpoint": None}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {"experiment": "main", "current_checkpoint": None}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.aero_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(self.state_path, state)

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _new_id() -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
