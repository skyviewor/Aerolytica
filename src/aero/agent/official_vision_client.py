"""Official Relay-backed visual model client."""

from __future__ import annotations

import base64
import mimetypes

from aero.core.config import VisionConfig
from aero.core.official_account import OfficialAccountSession, relay_llm_url


class OfficialVisionClient:
    def __init__(self, config: VisionConfig):
        self._config = config
        self._session = OfficialAccountSession()
        self.last_usage: dict | None = None

    async def close(self) -> None:
        await self._session.close()

    async def analyze(self, image_paths: list[str], prompt: str, detail: str = "high") -> str:
        parts: list[dict] = [{"type": "text", "text": prompt}]
        for path in image_paths:
            mime_type, _ = mimetypes.guess_type(path)
            mime_type = mime_type or "image/png"
            with open(path, "rb") as handle:
                raw = handle.read()
            parts.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}",
                    "detail": detail,
                },
            })
        response = await self._session.request_to(
            relay_llm_url(),
            "POST",
            "/chat/completions",
            json={
                "model": self._config.model or "default",
                "messages": [{"role": "user", "content": parts}],
                "stream": False,
            },
        )
        if response.status_code >= 400:
            try:
                detail_text = response.json().get("error", {}).get("message", response.text)
            except (ValueError, AttributeError):
                detail_text = response.text
            raise RuntimeError(f"官方视觉模型请求失败（HTTP {response.status_code}）：{detail_text}")
        payload = response.json()
        self.last_usage = payload.get("usage") if isinstance(payload, dict) else None
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("官方视觉模型返回了空结果")
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, list):
            content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        return str(content or "")
