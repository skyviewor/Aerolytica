"""Session persistence with AES encryption.

Stores conversation history so users can resume previous sessions.
All session data is encrypted at rest using Fernet (AES-128-CBC).
"""

from __future__ import annotations

import json
import os
import tempfile
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from aero.core.types import Message, ToolCall
from aero.data.pricing import TokenTracker

logger = structlog.get_logger()

_INDEX_FILE = "_index.json"
_REDACTED_SECRET = "[API_KEY_REDACTED]"
_SK_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_.-]{6,}\b", re.IGNORECASE)
_LABELED_SECRET_RE = re.compile(
    r"((?:api\s*key|apikey|access[_ -]?token|password|secret|密钥)\s*[:：=]\s*)"
    r"([A-Za-z0-9][A-Za-z0-9_.-]{7,})",
    re.IGNORECASE,
)
_SECRET_FIELD_NAMES = {
    "api_key",
    "apikey",
    "key",
    "access_token",
    "token",
    "password",
    "secret",
}


def _fernet_key_path() -> Path:
    return Path.home() / ".aero" / ".session_key"


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
    official_text_model: str = ""
    official_vision_model: str = ""
    mode: str = ""
    title_source: str = ""
    project_dir: str = ""
    transcript: list[dict[str, str]] = field(default_factory=list)

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
            "official_text_model": self.official_text_model,
            "official_vision_model": self.official_vision_model,
            "mode": self.mode,
            "title_source": self.title_source,
            "project_dir": self.project_dir,
            "transcript": _redact_secret_value(self.transcript),
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
            official_text_model=d.get("official_text_model", ""),
            official_vision_model=d.get("official_vision_model", ""),
            mode=d.get("mode", ""),
            title_source=d.get("title_source", ""),
            project_dir=d.get("project_dir", ""),
            transcript=_redact_secret_value(d.get("transcript", [])),
        )


class SessionManager:
    def __init__(self, storage_dir: Path | None = None):
        if storage_dir is None:
            storage_dir = Path.home() / ".aero" / "sessions"
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
        encrypted = self._encrypt(payload)
        descriptor, temporary = tempfile.mkstemp(prefix="session-", suffix=".tmp", dir=self.storage_dir)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
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

    def snapshot(self, session_id: str) -> bytes | None:
        """Return the encrypted bytes for a saved session."""
        path = self.storage_dir / f"{session_id}.json"
        return path.read_bytes() if path.is_file() else None

    def export_portable_context(self, session_id: str) -> dict | None:
        """Return sanitized, device-independent persisted context for HTTPS upload."""
        loaded = self.load(session_id)
        if loaded is None:
            return None
        messages, meta = loaded
        return {"schema": 1, "meta": meta.to_dict(),
                "messages": [_serialize_message(message) for message in messages]}

    def import_portable_context(self, session_id: str, payload: dict) -> None:
        if payload.get("schema") != 1 or not isinstance(payload.get("messages"), list):
            raise ValueError("invalid_portable_context")
        raw_messages = payload["messages"]
        if len(raw_messages) > 100_000:
            raise ValueError("portable_context_too_many_messages")
        messages = [_deserialize_message(item) for item in raw_messages]
        meta = SessionMeta.from_dict(payload.get("meta") or {})
        meta.id = session_id
        self.save(session_id, messages, meta)

    def load_snapshot(self, encrypted: bytes) -> tuple[list[Message], SessionMeta]:
        """Decode an encrypted session snapshot without changing saved sessions."""
        payload = self._decrypt(encrypted)
        messages = [_deserialize_message(d) for d in payload.get("messages", [])]
        meta = SessionMeta.from_dict(payload.get("meta", {}))
        return messages, meta

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

    def latest_session(
        self,
        project_dir: str | Path,
        *,
        include_legacy: bool = True,
    ) -> SessionMeta | None:
        """Return the latest session saved for one launch directory."""
        project = str(Path(project_dir).resolve())
        sessions = self.list_sessions()
        for meta in sessions:
            if meta.project_dir and str(Path(meta.project_dir).resolve()) == project:
                return meta
        if include_legacy:
            return next((meta for meta in sessions if not meta.project_dir), None)
        return None

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
    d = {"role": m.role, "content": _redact_secret_text(m.content)}
    if m.tool_call_id:
        d["tool_call_id"] = m.tool_call_id
    if m.tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "name": tc.name,
                "arguments": _redact_secret_value(tc.arguments),
            }
            for tc in m.tool_calls
        ]
    return d


def _deserialize_message(d: dict) -> Message:
    tool_calls = None
    if "tool_calls" in d:
        tool_calls = [
            ToolCall(
                id=tc["id"],
                name=tc["name"],
                arguments=_redact_secret_value(tc["arguments"]),
            )
            for tc in d["tool_calls"]
        ]
    return Message(
        role=d["role"],
        content=_redact_secret_text(d.get("content", "")),
        tool_call_id=d.get("tool_call_id"),
        tool_calls=tool_calls,
    )


def _redact_secret_text(value: str) -> str:
    text = str(value or "")
    text = _SK_SECRET_RE.sub(_REDACTED_SECRET, text)
    return _LABELED_SECRET_RE.sub(rf"\1{_REDACTED_SECRET}", text)


def _redact_secret_value(value, *, field_name: str = ""):
    if isinstance(value, dict):
        return {
            key: _redact_secret_value(item, field_name=str(key).lower())
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret_value(item, field_name=field_name) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secret_value(item, field_name=field_name) for item in value)
    if isinstance(value, str):
        if field_name in _SECRET_FIELD_NAMES and len(value) >= 8:
            return _REDACTED_SECRET
        return _redact_secret_text(value)
    return value
