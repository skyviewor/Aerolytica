"""Runtime model catalog provided by the official Aerolytica Relay."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from aero.core.official_account import (
    OfficialAccountError,
    OfficialAccountSession,
    relay_llm_url,
)

MODEL_CACHE_TTL_SECONDS = 5 * 60


@dataclass(frozen=True)
class OfficialModel:
    id: str
    display_name: str
    description: str
    capabilities: tuple[str, ...]
    recommended: bool = False
    price_multiplier: str = ""


@dataclass(frozen=True)
class OfficialModelPreferences:
    text_models: tuple[OfficialModel, ...]
    vision_models: tuple[OfficialModel, ...]
    text_default: str = "auto"
    vision_default: str = "auto"


def _fallback_model() -> OfficialModel:
    return OfficialModel(
        id="auto",
        display_name="自动路由",
        description="由官方服务自动选择模型",
        capabilities=("text", "vision", "tools"),
        recommended=True,
    )


class OfficialModelCatalog:
    """Fetch and cache the official account's server-owned model catalog."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = MODEL_CACHE_TTL_SECONDS,
    ) -> None:
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._models: tuple[OfficialModel, ...] = ()
        self._preferences: OfficialModelPreferences | None = None
        self._loaded_at = 0.0
        self._refresh_lock = asyncio.Lock()
        self.last_error: OfficialAccountError | None = None

    @property
    def has_cached_models(self) -> bool:
        return bool(self._models or self._preferences)

    @property
    def preferences(self) -> OfficialModelPreferences | None:
        return self._preferences

    async def models(self) -> tuple[OfficialModel, ...]:
        now = self._clock()
        if self._models and now - self._loaded_at < self._ttl_seconds:
            return self._models

        async with self._refresh_lock:
            now = self._clock()
            if self._models and now - self._loaded_at < self._ttl_seconds:
                return self._models
            try:
                models = await self._fetch()
            except OfficialAccountError as exc:
                self.last_error = exc
                return self._models or (_fallback_model(),)
            self._models = models
            self._loaded_at = self._clock()
            self.last_error = None
            return models

    async def _fetch(self) -> tuple[OfficialModel, ...]:
        session = OfficialAccountSession()
        try:
            response = await session.request_to(relay_llm_url(), "GET", "/models")
        finally:
            await session.close()
        if response.status_code >= 400:
            raise OfficialAccountError(
                f"官方模型列表暂时不可用（HTTP {response.status_code}）。"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OfficialAccountError("官方模型列表格式无效。") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise OfficialAccountError("官方模型列表格式无效。")

        parsed: list[OfficialModel] = []
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            capabilities = item.get("capabilities")
            if not isinstance(capabilities, list):
                capabilities = ["text"]
            parsed.append(
                OfficialModel(
                    id=model_id,
                    display_name=str(item.get("name") or item.get("display_name") or model_id),
                    description=str(item.get("description") or ""),
                    capabilities=tuple(str(value) for value in capabilities if str(value)),
                    recommended=bool(item.get("recommended", False)),
                )
            )
        if not parsed:
            raise OfficialAccountError("官方模型列表为空。")
        if not any(model.id == "auto" for model in parsed):
            parsed.insert(0, _fallback_model())
        return tuple(parsed)

    async def official_preferences(self) -> OfficialModelPreferences:
        now = self._clock()
        if self._preferences and now - self._loaded_at < self._ttl_seconds:
            return self._preferences
        async with self._refresh_lock:
            now = self._clock()
            if self._preferences and now - self._loaded_at < self._ttl_seconds:
                return self._preferences
            try:
                preferences = await self._fetch_preferences()
            except OfficialAccountError as exc:
                self.last_error = exc
                if self._preferences:
                    return self._preferences
                fallback = _fallback_model()
                return OfficialModelPreferences((fallback,), (fallback,))
            self._preferences = preferences
            self._loaded_at = self._clock()
            self.last_error = None
            return preferences

    async def _fetch_preferences(self) -> OfficialModelPreferences:
        session = OfficialAccountSession()
        try:
            response = await session.request_to(relay_llm_url(), "GET", "/model-preferences")
        finally:
            await session.close()
        if response.status_code >= 400:
            raise OfficialAccountError(f"官方模型偏好暂时不可用（HTTP {response.status_code}）。")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OfficialAccountError("官方模型偏好格式无效。") from exc
        if not isinstance(payload, dict):
            raise OfficialAccountError("官方模型偏好格式无效。")

        def parse_group(key: str) -> tuple[tuple[OfficialModel, ...], str]:
            group = payload.get(key)
            if not isinstance(group, dict):
                fallback = _fallback_model()
                return (fallback,), "auto"
            items = group.get("models") if isinstance(group.get("models"), list) else []
            pool = {
                str(model_id).strip()
                for model_id in group.get("pool", [])
                if str(model_id).strip()
            } if isinstance(group.get("pool"), list) else set()
            pool.add("auto")
            parsed = tuple(
                self._parse_model(item)
                for item in items
                if (
                    isinstance(item, dict)
                    and str(item.get("id") or "").strip() in pool
                )
            )
            if not any(model.id == "auto" for model in parsed):
                parsed = (_fallback_model(), *parsed)
            default = str(group.get("default") or "auto").strip()
            if default not in pool:
                default = "auto"
            return parsed, default

        text_models, text_default = parse_group("text")
        vision_models, vision_default = parse_group("vision")
        return OfficialModelPreferences(text_models, vision_models, text_default, vision_default)

    @staticmethod
    def _parse_model(item: dict) -> OfficialModel:
        capabilities = item.get("capabilities") if isinstance(item.get("capabilities"), list) else ["text"]
        multiplier = item.get("price_multiplier") if isinstance(item.get("price_multiplier"), dict) else {}
        return OfficialModel(
            id=str(item.get("id") or "").strip(),
            display_name=str(item.get("name") or item.get("display_name") or item.get("id") or ""),
            description=str(item.get("description") or ""),
            capabilities=tuple(str(value) for value in capabilities if str(value)),
            recommended=bool(item.get("recommended", False)),
            price_multiplier=str(multiplier.get("label") or ""),
        )
