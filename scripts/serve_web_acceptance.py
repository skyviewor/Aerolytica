"""Keep a real foreground ChatPresence and LocalSession alive for browser checks."""

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

from aero.application.chat_presence import ChatPresence
from aero.application.incoming_attachments import stage_command
from aero.application.local_session import LocalSession
from aero.application.remote_command import use_remote_command
from aero.core.config import AeroConfig
from aero.core.daemon import private_directory, private_write


async def main(root):
    root = private_directory(root.resolve())
    await acquire_environment_tokens(cli_only=True)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
        me = await http.get(os.environ["AERO_OFFICIAL_API_URL"] + "/v1/auth/me", headers={
            "Authorization": "Bearer " + os.environ["AERO_E2E_CLIENT_JWT"],
        })
        me.raise_for_status()
        EnvironmentAccount.identity = me.json()["user_id"]
    os.chdir(root)
    os.environ["AERO_SECRETS_PATH"] = str(root / "no-credentials.yaml")
    config = AeroConfig()
    config.llm.provider = "official"
    config.llm.model = "auto"
    config.save(root / "aero.yaml")
    report = {"session": "web-live-" + secrets.token_hex(6), "wire": [], "commands": []}

    def save():
        private_write(root / "acceptance.json", json.dumps(report, indent=2, ensure_ascii=False))

    async def observe(request):
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            report["wire"].append({"model": body["model"], "turn_id": body.get("relay_turn_id"),
                                   "purpose": request.headers.get("x-relay-question-purpose")})
            save()

    class PrivatePath(type(Path())):
        @classmethod
        def home(cls):
            return root / "private-home"

    with ExitStack() as stack:
        for module in (
            "aero.core.official_account", "aero.core.official_models",
            "aero.core.agent_preferences", "aero.core.official_mcp",
            "aero.agent.official_vision_client", "aero.application.chat_presence",
        ):
            stack.enter_context(patch(f"{module}.OfficialAccountSession", EnvironmentAccount))
        stack.enter_context(patch("aero.core.official_account.load_official_session",
                                  lambda: SimpleNamespace(is_logged_in=True)))
        stack.enter_context(patch.object(EnvironmentAccount, "request_observer", observe))
        stack.enter_context(patch("aero.application.chat_presence.Path", PrivatePath))
        stack.enter_context(patch("aero.data.vision_cache._cache_dir",
                                  lambda: private_directory(root / "vision-cache")))
        session = LocalSession(root, config, report["session"],
                               session_manager=IsolatedSessionManager(root / "sessions"))
        session.agent.registry = restricted_registry()
        session.agent._official_mcp = ReadOnlyMcp()
        session.agent.llm._client.event_hooks["request"] = [observe]
        stack.enter_context(patch.object(session, "_schedule_title_generation"))

        async def prompt(content):
            run = session.start_run(content, origin="web")
            await session.wait_run(run)
            if session.run_status(run)["state"] != "completed":
                raise RuntimeError("real_client_turn_failed")
            return session._session_meta.transcript[-1]["content"]

        async def remote(command):
            entry = {"command_id": command["command_id"], "model": command.get("model"),
                     "mode": command.get("mode"), "state": "running"}
            report["commands"].append(entry)
            save()
            content = await stage_command(command, presence._image_request,
                                          root / "attachments", session.id)
            with use_remote_command(command):
                answer = await prompt(content)
            entry.update(state="completed", answer=answer)
            save()
            return answer

        async def login_required():
            report["error"] = "login_required"
            save()

        # A real first turn makes this an ordinary nonempty foreground conversation.
        await prompt("这是网页验收专用会话，请只回复 OK，等待后续消息。")
        presence = ChatPresence(
            session_id=lambda: session.id, title=lambda: report["session"], on_title=lambda _: None,
            on_command=prompt, on_remote_command=remote, on_login_required=login_required,
            on_quota_full=lambda: report.update(error="quota_full"),
            export_context=lambda: session.session_manager.export_portable_context(session.id),
            is_idle=lambda: not any(not task.done() for task in session._run_tasks.values()),
            image_roots=lambda: [root],
        )
        presence.queue_local_message("user", "这是网页验收专用会话，请只回复 OK，等待后续消息。")
        presence.queue_local_message("assistant", session._session_meta.transcript[-1]["content"])
        async def request_observer(request):
            report.setdefault("http_pending", {})[request.url.path] = True
            save()

        async def response_observer(response):
            report.setdefault("http_pending", {}).pop(response.request.url.path, None)
            report["last_http"] = {"path": response.request.url.path,
                                   "status": response.status_code}
            save()
        presence._http.event_hooks["response"] = [response_observer]
        presence._http.event_hooks["request"] = [request_observer]
        task = asyncio.create_task(presence.run())
        try:
            for _ in range(3600):
                if task.done():
                    task.result()
                    break
                report["agent_id"] = presence.agent_id
                save()
                await asyncio.sleep(1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await presence.close()
            await session.close()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
