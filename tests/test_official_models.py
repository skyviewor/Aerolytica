"""Tests for the official Relay model catalog cache."""

from __future__ import annotations

import httpx
import pytest

import aero.core.official_models as official_models


class FakeSession:
    calls: list[tuple[str, str, str]] = []
    response: httpx.Response | None = None

    async def request_to(self, base_url: str, method: str, path: str):
        self.calls.append((base_url, method, path))
        assert self.response is not None
        return self.response

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_official_model_catalog_caches_and_refreshes_after_ttl(monkeypatch):
    now = [100.0]
    FakeSession.calls = []
    FakeSession.response = httpx.Response(
        200,
        json={
            "object": "list",
            "data": [
                {
                    "id": "auto",
                    "name": "自动路由",
                    "description": "官方自动选择",
                    "capabilities": ["text", "vision"],
                    "recommended": True,
                },
                {
                    "id": "glm-5.2",
                    "name": "GLM 5.2",
                    "description": "高质量文本模型",
                    "capabilities": ["text", "tools"],
                    "recommended": False,
                },
            ],
        },
    )
    monkeypatch.setattr(official_models, "OfficialAccountSession", FakeSession)
    catalog = official_models.OfficialModelCatalog(clock=lambda: now[0])

    first = await catalog.models()
    now[0] += 60
    second = await catalog.models()
    now[0] += 241
    third = await catalog.models()

    assert first == second == third
    assert len(FakeSession.calls) == 2
    assert FakeSession.calls[0][1:] == ("GET", "/models")
    assert first[1].id == "glm-5.2"
    assert first[1].capabilities == ("text", "tools")


@pytest.mark.asyncio
async def test_official_model_catalog_falls_back_to_auto_when_unavailable(monkeypatch):
    class UnavailableSession:
        async def request_to(self, *args, **kwargs):
            raise official_models.OfficialAccountError("offline")

        async def close(self):
            return None

    monkeypatch.setattr(official_models, "OfficialAccountSession", UnavailableSession)
    catalog = official_models.OfficialModelCatalog()

    models = await catalog.models()

    assert [model.id for model in models] == ["auto"]
    assert catalog.last_error is not None


@pytest.mark.asyncio
async def test_official_model_catalog_keeps_stale_cache_when_refresh_fails(monkeypatch):
    now = [100.0]
    FakeSession.calls = []
    FakeSession.response = httpx.Response(
        200,
        json={
            "object": "list",
            "data": [{"id": "auto", "name": "自动路由", "capabilities": ["text"]}],
        },
    )
    monkeypatch.setattr(official_models, "OfficialAccountSession", FakeSession)
    catalog = official_models.OfficialModelCatalog(clock=lambda: now[0])
    cached = await catalog.models()

    class FailingSession:
        async def request_to(self, *args, **kwargs):
            raise official_models.OfficialAccountError("offline")

        async def close(self):
            return None

    monkeypatch.setattr(official_models, "OfficialAccountSession", FailingSession)
    now[0] += official_models.MODEL_CACHE_TTL_SECONDS + 1
    stale = await catalog.models()

    assert stale == cached
    assert catalog.last_error is not None


@pytest.mark.asyncio
async def test_official_preferences_only_expose_models_in_user_pool(monkeypatch):
    FakeSession.calls = []
    FakeSession.response = httpx.Response(
        200,
        json={
            "text": {
                "pool": ["auto"],
                "default": "glm-5.2",
                "models": [
                    {"id": "auto", "name": "自动路由", "capabilities": ["text"]},
                    {"id": "glm-5.2", "name": "GLM 5.2", "capabilities": ["text"]},
                ],
            },
            "vision": {
                "pool": ["auto", "qwen3.8-max"],
                "default": "qwen3.8-max",
                "models": [
                    {"id": "auto", "name": "自动路由", "capabilities": ["vision"]},
                    {"id": "qwen3.8-max", "name": "通义千问 3.8 Max", "capabilities": ["vision"]},
                    {"id": "glm-5.2", "name": "GLM 5.2", "capabilities": ["text"]},
                ],
            },
        },
    )
    monkeypatch.setattr(official_models, "OfficialAccountSession", FakeSession)
    catalog = official_models.OfficialModelCatalog()

    preferences = await catalog.official_preferences()

    assert [model.id for model in preferences.text_models] == ["auto"]
    assert preferences.text_default == "auto"
    assert [model.id for model in preferences.vision_models] == ["auto", "qwen3.8-max"]
    assert preferences.vision_default == "qwen3.8-max"
