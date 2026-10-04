"""Official Relay-backed visual model client."""

from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path

import httpx

from aero.agent.vision_client import VisionAnalysisError
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
        async with httpx.AsyncClient(timeout=120) as uploads:
            for path in image_paths:
                mime_type = mimetypes.guess_type(path)[0] or "image/png"
                image = Path(path)
                ticket = await self._session.request_to(
                    relay_llm_url(),
                    "POST",
                    "/vision/upload-url",
                    json={"content_type": mime_type, "size_bytes": image.stat().st_size},
                )
                if ticket.status_code >= 400:
                    raise VisionAnalysisError(
                        f"无法获取图片直传地址（HTTP {ticket.status_code}）：{ticket.text}",
                        error_type="image_upload",
                        status_code=ticket.status_code,
                    )
                signed = ticket.json()

                async def chunks():
                    with image.open("rb") as handle:
                        while chunk := handle.read(256 * 1024):
                            yield chunk

                try:
                    uploaded = await uploads.put(
                        signed["upload_url"],
                        content=chunks(),
                        headers={**signed["headers"], "Content-Length": str(image.stat().st_size)},
                    )
                    uploaded.raise_for_status()
                except httpx.HTTPError as exc:
                    raise VisionAnalysisError(
                        "图片直传 OSS 失败，请检查网络连接。",
                        error_type="image_upload",
                    ) from exc
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": signed["image_url"], "detail": detail},
                    }
                )
        from aero.application.turn_context import current_turn_id

        turn_id = current_turn_id() or uuid.uuid4().hex
        response = await self._session.request_to(
            relay_llm_url(),
            "POST",
            "/chat/completions",
            timeout=120,
            headers={
                "x-relay-turn-id": turn_id,
                "x-relay-question-purpose": "vision",
                "Idempotency-Key": uuid.uuid4().hex,
            },
            json={
                "model": self._config.model or "default",
                "messages": [{"role": "user", "content": parts}],
                "stream": False,
                "relay_turn_id": turn_id,
            },
        )
        if response.status_code >= 400:
            try:
                detail_text = response.json().get("error", {}).get("message", response.text)
            except (ValueError, AttributeError):
                detail_text = response.text
            error_type, message = {
                401: ("authentication", "官方登录已失效，请重新登录。"),
                402: ("insufficient_credits", "官方账户额度不足。"),
                503: ("route_unavailable", "Relay 视觉路由当前不可用。"),
            }.get(response.status_code, ("upstream_error", "官方视觉模型请求失败。"))
            raise VisionAnalysisError(
                f"{message}{detail_text}",
                error_type=error_type,
                status_code=response.status_code,
            )
        payload = response.json()
        self.last_usage = payload.get("usage") if isinstance(payload, dict) else None
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("官方视觉模型返回了空结果")
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        return str(content or "")
