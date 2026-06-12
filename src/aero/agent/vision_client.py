"""Vision model client for image analysis via OpenAI-compatible API."""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass

import httpx

from aero.core.config import VisionConfig

MAX_IMAGE_SIZE = 20 * 1024 * 1024
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class VisionAnalysisError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str = "vision_error",
        status_code: int | None = None,
        response_excerpt: str = "",
    ):
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code
        self.response_excerpt = response_excerpt


@dataclass
class _VisionModelConfig:
    provider: str
    model: str
    api_key: str
    base_url: str

    @property
    def endpoint(self) -> str:
        if self.base_url:
            base_url = self.base_url.rstrip("/")
            if base_url.endswith("/v1"):
                return base_url + "/chat/completions"
            return base_url + "/v1/chat/completions"
        if self.provider == "bailian":
            return "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        if self.provider == "openai":
            return "https://api.openai.com/v1/chat/completions"
        if self.provider == "deepseek":
            return "https://api.deepseek.com/v1/chat/completions"
        return "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


class VisionClient:
    def __init__(self, config: VisionConfig):
        self._config = _VisionModelConfig(
            provider=config.provider,
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
        )
        self._client = httpx.AsyncClient(timeout=120)
        self.last_usage: dict | None = None

    async def close(self):
        await self._client.aclose()

    async def analyze(
        self,
        image_paths: list[str],
        prompt: str,
        detail: str = "high",
    ) -> str:
        content_parts: list[dict] = [{"type": "text", "text": prompt}]

        for path in image_paths:
            mime_type, _ = mimetypes.guess_type(path)
            if not mime_type:
                mime_type = "image/png"

            with open(path, "rb") as f:
                raw = f.read()

            if len(raw) > MAX_IMAGE_SIZE:
                raise ValueError(
                    f"图片 {path} 大小 {len(raw) / 1024 / 1024:.1f}MB 超过限制 "
                    f"({MAX_IMAGE_SIZE / 1024 / 1024:.0f}MB)"
                )

            encoded = base64.b64encode(raw).decode("ascii")
            data_uri = f"data:{mime_type};base64,{encoded}"

            content_parts.append({
                "type": "image_url",
                "image_url": {"url": data_uri, "detail": detail},
            })

        body = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": content_parts}],
        }
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._client.post(
                self._config.endpoint,
                json=body,
                headers=headers,
                timeout=120,
            )
        except httpx.TimeoutException as e:
            raise VisionAnalysisError(
                "视觉模型请求超时，请稍后重试或降低图片数量/分辨率。",
                error_type="timeout",
            ) from e
        except httpx.RequestError as e:
            raise VisionAnalysisError(
                f"无法连接视觉模型服务：{e.__class__.__name__}: {e}",
                error_type="network_error",
            ) from e

        excerpt = _response_excerpt(resp)
        if resp.status_code == 401:
            raise VisionAnalysisError(
                "视觉模型 API Key 无效，请检查 API Key 是否正确。",
                error_type="auth_error",
                status_code=resp.status_code,
                response_excerpt=excerpt,
            )
        if resp.status_code == 429:
            raise VisionAnalysisError(
                "视觉模型请求过于频繁，请稍后重试。",
                error_type="rate_limited",
                status_code=resp.status_code,
                response_excerpt=excerpt,
            )
        if resp.status_code >= 500:
            raise VisionAnalysisError(
                f"视觉模型服务暂时不可用（HTTP {resp.status_code}），请稍后重试。",
                error_type="server_error",
                status_code=resp.status_code,
                response_excerpt=excerpt,
            )
        if resp.status_code >= 400:
            raise VisionAnalysisError(
                f"视觉模型请求失败（HTTP {resp.status_code}）。",
                error_type="http_error",
                status_code=resp.status_code,
                response_excerpt=excerpt,
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise VisionAnalysisError(
                "视觉模型返回的不是合法 JSON。",
                error_type="bad_response",
                status_code=resp.status_code,
                response_excerpt=excerpt,
            ) from e

        self.last_usage = data.get("usage")

        if "choices" not in data or not data["choices"]:
            raise VisionAnalysisError(
                "视觉模型返回了空结果。",
                error_type="empty_choices",
                status_code=resp.status_code,
                response_excerpt=excerpt,
            )

        content = data["choices"][0].get("message", {}).get("content", "")
        if not content:
            raise VisionAnalysisError(
                "视觉模型返回了空内容。",
                error_type="empty_content",
                status_code=resp.status_code,
                response_excerpt=excerpt,
            )

        return content


def _response_excerpt(resp: httpx.Response, limit: int = 1200) -> str:
    text = resp.text.strip()
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit] + "..."
    return text
