"""Run a real foreground manual-model image turn with non-secret wire evidence."""

import asyncio
import json
import os
import secrets
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from accept_web_conversation_v2 import (
    EnvironmentAccount,
    IsolatedSessionManager,
    ReadOnlyMcp,
    acquire_environment_tokens,
    restricted_registry,
)
from PIL import Image

from aero.application.local_session import LocalSession
from aero.core.config import AeroConfig
from aero.core.daemon import private_directory, private_write


async def main(root, model):
    root = private_directory(root.resolve())
    await acquire_environment_tokens(cli_only=True)
    token = os.environ["AERO_E2E_CLIENT_JWT"]
    async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
        response = await http.get(
            os.environ["AERO_OFFICIAL_API_URL"] + "/v1/auth/me",
            headers={"Authorization": "Bearer " + token},
        )
        response.raise_for_status()
        EnvironmentAccount.identity = response.json()["user_id"]
    os.environ["AERO_SECRETS_PATH"] = str(root / "no-credentials.yaml")
    os.chdir(root)
    config = AeroConfig()
    config.llm.provider = "official"
    config.llm.model = model
    config.mode = "execute"
    config.save(root / "aero.yaml")
    image = root / "manual-model-red.png"
    Image.new("RGB", (24, 24), "red").save(image)
    report = {"model": model, "passed": False, "wire": [], "test": "foreground_real_client"}

    async def observe(request):
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            report["wire"].append({
                "model": body["model"],
                "purpose": request.headers.get("x-relay-question-purpose"),
                "turn_id": body.get("relay_turn_id"),
                "request_key": request.headers.get("Idempotency-Key"),
            })
            private_write(root / "acceptance.json", json.dumps(report, indent=2))

    with ExitStack() as stack:
        for module in (
            "aero.core.official_account", "aero.core.official_models",
            "aero.core.agent_preferences", "aero.core.official_mcp",
            "aero.agent.official_vision_client",
        ):
            stack.enter_context(patch(f"{module}.OfficialAccountSession", EnvironmentAccount))
        stack.enter_context(patch(
            "aero.core.official_account.load_official_session",
            lambda: SimpleNamespace(is_logged_in=True),
        ))
        stack.enter_context(patch.object(EnvironmentAccount, "request_observer", observe))
        stack.enter_context(patch(
            "aero.data.vision_cache._cache_dir",
            lambda: private_directory(root / "vision-cache"),
        ))
        session = LocalSession(
            root, config, "manual-vision-" + secrets.token_hex(6),
            session_manager=IsolatedSessionManager(root / "sessions"),
        )
        session.agent.registry = restricted_registry()
        session.agent._official_mcp = ReadOnlyMcp()
        session.agent.llm._client.event_hooks["request"] = [observe]
        # Exclude unrelated automatic title generation from this targeted routing check.
        stack.enter_context(patch.object(session, "_schedule_title_generation"))
        try:
            run = session.start_run(
                "模型路由验收：只调用 analyze_image 分析图片 " + str(image)
                + "，报告主色调。禁止搜索、修改文件或其他操作。"
            )
            await asyncio.wait_for(session.wait_run(run), timeout=240)
            report["state"] = session.run_status(run)["state"]
            report["answer"] = session._session_meta.transcript[-1].get("content", "")
            wire = report["wire"]
            report["passed"] = (
                report["state"] == "completed"
                and any(item["purpose"] == "vision" for item in wire)
                and all(item["model"] == model for item in wire)
            )
        except Exception as exc:
            report["failure_type"] = type(exc).__name__
        finally:
            await session.close()
            private_write(
                root / "acceptance.json", json.dumps(report, indent=2, ensure_ascii=False)
            )
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
