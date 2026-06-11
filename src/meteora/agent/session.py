"""Session persistence with AES encryption.

Stores conversation history so users can resume previous sessions.
All session data is encrypted at rest using Fernet (AES-128-CBC).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from meteora.core.types import Message, ToolCall
from meteora.data.pricing import TokenTracker

logger = structlog.get_logger()

_INDEX_FILE = "_index.json"


def _fernet_key_path() -> Path:
    return Path.home() / ".meteora" / ".session_key"


def _load_or_create_key() -> bytes:
    key_path = _fernet_key_path()
    if key_path.exists():
        return key_path.read_bytes()
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    os.chmod(key_path, 0o600)
    return key


def _get_fernet():
    from cryptography.fernet import Fernet

    key = _load_or_create_key()
    return Fernet(key)


@dataclass
class SessionMeta:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    message_count: int = 0
    tracker: dict = field(default_factory=dict)
    model: str = ""
    provider: str = ""
    vision_model: str = ""
    mode: str = ""
    title_source: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "tracker": self.tracker,
            "model": self.model,
            "provider": self.provider,
            "vision_model": self.vision_model,
            "mode": self.mode,
            "title_source": self.title_source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionMeta":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            created_at=d.get("created_at", 0),
            updated_at=d.get("updated_at", 0),
            message_count=d.get("message_count", 0),
            tracker=d.get("tracker", {}),
            model=d.get("model", ""),
            provider=d.get("provider", ""),
            vision_model=d.get("vision_model", ""),
            mode=d.get("mode", ""),
            title_source=d.get("title_source", ""),
        )


class SessionManager:
    def __init__(self, storage_dir: Path | None = None):
        if storage_dir is None:
            storage_dir = Path.home() / ".meteora" / "sessions"
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _encrypt(self, data: dict) -> bytes:
        fernet = _get_fernet()
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return fernet.encrypt(raw)

    def _decrypt(self, encrypted: bytes) -> dict:
        fernet = _get_fernet()
        raw = fernet.decrypt(encrypted)
        return json.loads(raw.decode("utf-8"))

    def save(
        self,
        session_id: str,
        messages: list[Message],
        meta: SessionMeta | None = None,
    ) -> None:
        if meta is None:
            meta = SessionMeta(id=session_id)
        meta.id = session_id
        meta.updated_at = time.time()
        meta.message_count = len(messages)
        path = self.storage_dir / f"{session_id}.json"
        data = [_serialize_message(m) for m in messages]
        payload = {"meta": meta.to_dict(), "messages": data}
        path.write_bytes(self._encrypt(payload))
        self._update_index(meta)

    def load(self, session_id: str) -> tuple[list[Message], SessionMeta] | None:
        path = self.storage_dir / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            payload = self._decrypt(path.read_bytes())
            messages = [_deserialize_message(d) for d in payload.get("messages", [])]
            meta = SessionMeta.from_dict(payload.get("meta", {}))
            return messages, meta
        except Exception:
            logger.exception("session.load_failed", session_id=session_id)
            return None

    def delete(self, session_id: str) -> bool:
        path = self.storage_dir / f"{session_id}.json"
        if not path.exists():
            return False
        path.unlink()
        self._remove_from_index(session_id)
        return True

    def list_sessions(self) -> list[SessionMeta]:
        index = self._read_index()
        metas = []
        for data in index.values():
            try:
                metas.append(SessionMeta.from_dict(data))
            except Exception:
                pass
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas

    def _index_path(self) -> Path:
        return self.storage_dir / _INDEX_FILE

    def _read_index(self) -> dict:
        path = self._index_path()
        if not path.exists():
            return {}
        try:
            return self._decrypt(path.read_bytes())
        except Exception:
            return {}

    def _write_index(self, index: dict) -> None:
        path = self._index_path()
        path.write_bytes(self._encrypt(index))

    def _update_index(self, meta: SessionMeta) -> None:
        index = self._read_index()
        index[meta.id] = meta.to_dict()
        self._write_index(index)

    def _remove_from_index(self, session_id: str) -> None:
        index = self._read_index()
        index.pop(session_id, None)
        self._write_index(index)


def _serialize_message(m: Message) -> dict:
    d = {"role": m.role, "content": m.content}
    if m.tool_call_id:
        d["tool_call_id"] = m.tool_call_id
    if m.tool_calls:
        d["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in m.tool_calls
        ]
    return d


def _deserialize_message(d: dict) -> Message:
    tool_calls = None
    if "tool_calls" in d:
        tool_calls = [
            ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
            for tc in d["tool_calls"]
        ]
    return Message(
        role=d["role"],
        content=d.get("content", ""),
        tool_call_id=d.get("tool_call_id"),
        tool_calls=tool_calls,
    )
