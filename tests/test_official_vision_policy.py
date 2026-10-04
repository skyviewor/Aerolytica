from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aero.application.turn_context import current_primary_model, use_primary_model
from aero.core import official_vision_policy as policy
from aero.core.config import AeroConfig, VisionConfig
from aero.toolbox.tools import vision


@pytest.mark.parametrize(
    "selected,capabilities,expected",
    [
        ("glm-5.3-flash", ("text", "vision"), "glm-5.3-flash"),
        ("text-only", ("text", "tools"), "auto"),
        ("auto", (), "auto"),
        ("default", (), "auto"),
    ],
)
async def test_manual_multimodal_model_wins_over_independent_vision_setting(
    monkeypatch, selected, capabilities, expected
):
    catalog = SimpleNamespace(
        models=AsyncMock(return_value=[SimpleNamespace(id=selected, capabilities=capabilities)])
    )
    monkeypatch.setattr(policy, "_catalog", catalog)
    assert await policy.select_official_vision_model(selected) == expected
    if selected in {"auto", "default"}:
        catalog.models.assert_not_awaited()


async def test_missing_capability_does_not_silently_switch_models(monkeypatch):
    monkeypatch.setattr(policy, "_catalog", SimpleNamespace(models=AsyncMock(return_value=[])))
    with pytest.raises(RuntimeError, match="无法确认"):
        await policy.select_official_vision_model("manual-model")


async def test_tool_uses_active_session_not_stale_workspace_and_separates_cache(
    monkeypatch, tmp_path
):
    config = AeroConfig()
    config.llm.provider = "official"
    config.llm.model = "workspace-model"
    resolved = VisionConfig(mode="official", provider="official", model="old-vision")
    used_models = []

    class Client:
        last_usage = None

        async def analyze(self, *args):
            used_models.append(resolved.model)
            return "red"

        close = AsyncMock()

    monkeypatch.setattr(vision, "_ensure_vision_client", lambda: (Client(), config, resolved))
    monkeypatch.setattr(vision, "vision_is_configured", lambda config: True)
    select = AsyncMock(return_value="glm-5.3-flash")
    monkeypatch.setattr(policy, "select_official_vision_model", select)

    def cache_get(paths, prompt, model, ttl):
        return "wrong" if model == "old-vision" else None

    monkeypatch.setattr("aero.data.vision_cache.get", cache_get)
    monkeypatch.setattr("aero.data.vision_cache.put", lambda *args: None)
    image = tmp_path / "image.png"
    image.write_bytes(b"test")
    with use_primary_model("official", "glm-5.3-flash"):
        result = await vision.analyze_image([str(image)], "color?")
    select.assert_awaited_once_with("glm-5.3-flash")
    assert used_models == ["glm-5.3-flash"]
    assert result["model"] == "glm-5.3-flash"
    assert result["analysis"] == "red"
    assert current_primary_model() is None


def test_primary_selection_scope_restores_after_error():
    with use_primary_model("official", "outer"):
        with pytest.raises(ValueError):
            with use_primary_model("official", "inner"):
                raise ValueError("failed")
        assert current_primary_model() == ("official", "outer")
    assert current_primary_model() is None


async def test_official_turn_does_not_use_saved_byok_visual_profile(monkeypatch):
    from aero.agent.official_vision_client import OfficialVisionClient

    saved = AeroConfig()
    saved.vision.mode = "separate"
    saved.vision.provider = "bailian"
    saved.vision.model = "qwen3.7-plus"
    monkeypatch.setattr(vision, "find_config", lambda: saved)
    with use_primary_model("official", "glm-5.3-flash"):
        client, effective, resolved = vision._ensure_vision_client()
    try:
        assert isinstance(client, OfficialVisionClient)
        assert effective.vision.mode == "official"
        assert resolved.provider == "official"
        assert saved.vision.mode == "separate"
        assert saved.vision.model == "qwen3.7-plus"
    finally:
        await client.close()


def test_byok_turn_keeps_saved_visual_profile(monkeypatch):
    from aero.agent.vision_client import VisionClient

    saved = AeroConfig()
    saved.vision.mode = "separate"
    saved.vision.provider = "bailian"
    saved.vision.model = "qwen3.7-plus"
    saved.vision.api_key = "test-only-key"
    monkeypatch.setattr(vision, "find_config", lambda: saved)
    monkeypatch.setattr(vision, "resolved_vision_config", lambda config: config.vision)
    with use_primary_model("bailian", "text-model"):
        client, effective, resolved = vision._ensure_vision_client()
    assert isinstance(client, VisionClient)
    assert effective is saved
    assert resolved.model == "qwen3.7-plus"
