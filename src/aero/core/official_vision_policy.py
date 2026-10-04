"""Keep manual text and vision selections unified when the model supports vision."""

from __future__ import annotations

from aero.agent.vision_client import VisionAnalysisError
from aero.core.official_models import OfficialModelCatalog

_catalog = OfficialModelCatalog()


async def select_official_vision_model(primary_model: str) -> str:
    selected = primary_model.strip()
    if selected in {"", "auto", "default"}:
        return "auto"
    models = await _catalog.models()
    model = next((item for item in models if item.id == selected), None)
    if model is None:
        # A failed catalog fetch must not silently change the user's selected model.
        raise VisionAnalysisError(
            "无法确认所选模型的视觉能力，请刷新官方模型列表后重试。",
            error_type="model_capability_unavailable",
        )
    return selected if "vision" in model.capabilities else "auto"
