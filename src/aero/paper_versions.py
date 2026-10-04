"""Single-document version history for a Markdown paper."""

from __future__ import annotations

import difflib
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PaperVersionError(RuntimeError):
    """Raised when the paper version history cannot complete an operation."""


@dataclass(frozen=True)
class PaperDiff:
    version_id: str
    document: str
    changed: bool
    added_lines: int
    removed_lines: int
    unified_diff: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "document": self.document,
            "changed": self.changed,
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
            "unified_diff": self.unified_diff,
        }


class PaperVersionManager:
    """Track exact snapshots of the project's fixed ``paper/main.md`` document."""

    DOCUMENT = "paper/main.md"

    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir).resolve()
        self.document_root = self.project_dir / "paper"
        self.paper_dir = self.project_dir / ".aero" / "paper"
        self.versions_dir = self.paper_dir / "versions"
        self.state_path = self.paper_dir / "state.json"

    def initialize(self) -> dict[str, Any]:
        path = self._safe_document_path(self.DOCUMENT)
        state = self._read_state()
        configured = state.get("document")
        if configured and configured != self.DOCUMENT:
            raise PaperVersionError(
                f"检测到旧版论文绑定 {configured}；当前版本只允许使用 {self.DOCUMENT}。"
            )
        document_created = False
        if path.exists() and not path.is_file():
            raise PaperVersionError(f"论文正文路径不是文件：{self.DOCUMENT}")
        if not path.exists():
            self._write_bytes(path, b"")
            document_created = True
        state.update({"version": 1, "document": self.DOCUMENT, "head": state.get("head")})
        self._write_json(self.state_path, state)
        if state.get("head"):
            return {
                "initialized": True,
                "created": False,
                "document_created": document_created,
                "document": self.DOCUMENT,
                "head": state["head"],
            }
        version = self.save("初始版本", kind="initial")
        return {
            "initialized": True,
            "created": True,
            "document_created": document_created,
            "document": self.DOCUMENT,
            "head": version["id"],
            "version": version,
        }

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        document = state.get("document")
        if not document:
            return {"initialized": False}
        path = self._safe_document_path(document)
        head = self.load(state.get("head"))
        exists = path.is_file()
        current_hash = self._hash(path.read_bytes()) if exists else ""
        changed = head is None or current_hash != head.get("sha256")
        return {
            "initialized": True,
            "document": document,
            "exists": exists,
            "head": head,
            "changed": changed,
            "version_count": len(self.list()),
        }

    def save(self, title: str = "", *, kind: str = "manual") -> dict[str, Any]:
        state = self._require_state()
        path = self._safe_document_path(state["document"])
        if not path.is_file():
            raise PaperVersionError(f"论文正文不存在：{state['document']}")
        content = path.read_bytes()
        digest = self._hash(content)
        head = self.load(state.get("head"))
        if head is not None and head.get("sha256") == digest:
            return {**head, "created": False}

        created_at = time.time()
        version_id = self._new_id()
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        snapshot_name = f"{version_id}.md"
        metadata = {
            "version": 1,
            "id": version_id,
            "title": title.strip()
            or time.strftime("论文版本 %Y-%m-%d %H:%M", time.localtime(created_at)),
            "kind": kind,
            "created_at": created_at,
            "document": state["document"],
            "parent_id": state.get("head"),
            "sha256": digest,
            "size": len(content),
            "snapshot": snapshot_name,
        }
        self._write_bytes(self.versions_dir / snapshot_name, content)
        self._write_json(self.versions_dir / f"{version_id}.json", metadata)
        state["head"] = version_id
        self._write_json(self.state_path, state)
        return {**metadata, "created": True}

    def list(self, *, include_safety: bool = True) -> list[dict[str, Any]]:
        if not self.versions_dir.is_dir():
            return []
        versions = []
        for path in self.versions_dir.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if not include_safety and item.get("kind") == "pre-restore":
                continue
            versions.append(item)
        versions.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        return versions

    def load(self, version: str | None) -> dict[str, Any] | None:
        if not version:
            return None
        exact = self.versions_dir / f"{version}.json"
        if exact.is_file():
            return json.loads(exact.read_text(encoding="utf-8"))
        matches = [
            item
            for item in self.list()
            if item["id"].startswith(version) or item.get("title") == version
        ]
        if len(matches) > 1:
            raise PaperVersionError(f"论文版本不唯一，请使用更完整的 ID：{version}")
        return matches[0] if matches else None

    def diff(self, version: str | None = None) -> PaperDiff:
        state = self._require_state()
        target = self.load(version or state.get("head"))
        if target is None:
            raise PaperVersionError(f"找不到论文版本：{version or '当前版本'}")
        path = self._safe_document_path(state["document"])
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        saved = self._snapshot_path(target).read_text(encoding="utf-8")
        saved_lines = saved.splitlines(keepends=True)
        current_lines = current.splitlines(keepends=True)
        unified = "".join(
            difflib.unified_diff(
                saved_lines,
                current_lines,
                fromfile=f"{state['document']}@{target['id']}",
                tofile=f"{state['document']}@working",
            )
        )
        added, removed = self._change_counts(unified)
        return PaperDiff(
            version_id=target["id"],
            document=state["document"],
            changed=bool(unified),
            added_lines=added,
            removed_lines=removed,
            unified_diff=unified,
        )

    def restore(self, version: str) -> dict[str, Any]:
        state = self._require_state()
        target = self.load(version)
        if target is None:
            raise PaperVersionError(f"找不到论文版本：{version}")
        protection = None
        current_diff = self.diff()
        if current_diff.changed:
            protection = self.save(
                f"恢复前保护：{target['title']}",
                kind="pre-restore",
            )
        content = self._snapshot_path(target).read_bytes()
        document_path = self._safe_document_path(state["document"])
        self._write_bytes(document_path, content)
        state["head"] = target["id"]
        self._write_json(self.state_path, state)
        return {
            "restored": target,
            "protection_version": protection,
            "document": state["document"],
        }

    def _require_state(self) -> dict[str, Any]:
        state = self._read_state()
        if not state.get("document"):
            raise PaperVersionError("尚未启用论文版本控制。请先指定一份 Markdown 正文。")
        return state

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _safe_document_path(self, document: str | Path) -> Path:
        value = Path(document).expanduser()
        if value.is_absolute():
            path = value.resolve()
        elif value.parts and value.parts[0] == "paper":
            path = (self.project_dir / value).resolve()
        else:
            path = (self.document_root / value).resolve()
        expected = (self.project_dir / self.DOCUMENT).resolve()
        if path != expected:
            raise PaperVersionError(f"论文版本控制只允许使用 {self.DOCUMENT}。")
        return path

    def _snapshot_path(self, metadata: dict[str, Any]) -> Path:
        path = (self.versions_dir / str(metadata.get("snapshot", ""))).resolve()
        try:
            path.relative_to(self.versions_dir.resolve())
        except ValueError as exc:
            raise PaperVersionError("论文版本快照路径不安全。") from exc
        if not path.is_file():
            raise PaperVersionError(f"论文版本快照不存在：{metadata.get('id', '')}")
        return path

    def _short_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.project_dir).as_posix()
        except ValueError:
            return str(path)

    @staticmethod
    def _change_counts(unified: str) -> tuple[int, int]:
        added = sum(
            1
            for line in unified.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        removed = sum(
            1
            for line in unified.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )
        return added, removed

    @staticmethod
    def _hash(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        PaperVersionManager._write_bytes(path, payload.encode("utf-8"))

    @staticmethod
    def _write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            temporary.write_bytes(content)
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _new_id() -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        return f"paper-{stamp}-{uuid.uuid4().hex[:8]}"
