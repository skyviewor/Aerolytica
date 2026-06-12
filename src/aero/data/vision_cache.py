"""Content-hash based cache for vision model analysis results."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def _cache_dir() -> Path:
    d = Path.home() / ".cache" / "aero" / "vision"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file_content_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(64 * 1024):
            h.update(chunk)
    return h.hexdigest()


def cache_key(image_paths: list[str], prompt: str, model: str) -> str:
    parts = []
    for p in image_paths:
        parts.append(_file_content_hash(p))
    parts.append(prompt)
    parts.append(model)
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode()).hexdigest()


def _cache_path(key: str) -> Path:
    return _cache_dir() / f"{key}.json"


def get(image_paths: list[str], prompt: str, model: str, ttl_hours: int) -> str | None:
    key = cache_key(image_paths, prompt, model)
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    cached_at = data.get("cached_at", 0)
    if time.time() - cached_at > ttl_hours * 3600:
        return None
    return data.get("analysis")


def put(image_paths: list[str], prompt: str, model: str, analysis: str) -> None:
    key = cache_key(image_paths, prompt, model)
    data = {
        "analysis": analysis,
        "model": model,
        "cached_at": time.time(),
        "image_paths": image_paths,
        "prompt": prompt,
    }
    _cache_path(key).write_text(json.dumps(data, ensure_ascii=False))
