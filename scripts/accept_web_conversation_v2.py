"""Real-client acceptance; credentials exist only in the process environment.

Creates dedicated cloud artifacts; never deletes account data or changes preferences.
Uses existing CLI/web JWTs or explicitly requested environment-only test-account login.
No refresh tokens, passwords, or JWTs are printed or written to disk.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import secrets
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from PIL import Image

from aero.agent.session import SessionManager
from aero.application.cloud_runtime import CloudAgentRuntime, RelayRuntimeTransport
from aero.application.local_session import LocalSession
from aero.core.config import AeroConfig
from aero.core.daemon import private_directory, private_write
from aero.core.debug_log import configure_debug_logging
from aero.core.official_account import OfficialAccountSession, OfficialLoginRequiredError
from aero.core.official_mcp import OfficialMcpClient
from aero.toolbox.registry import ToolRegistry, get_registry


class AcceptanceError(RuntimeError):
    """Allowlisted diagnostic identifier; never embeds upstream text or credentials."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def restricted_registry():
    # Import real implementations before filtering: LocalSession does not load them.
    from aero.toolbox.tools.files import read_file
    from aero.toolbox.tools.vision import analyze_image

    registry = ToolRegistry()
    for name, function in (("read_file", read_file), ("analyze_image", analyze_image)):
        spec = get_registry().get(name)
        if spec is None or spec.function is not function:
            raise AcceptanceError("required_real_tool_missing")
        registry.register(spec)
    return registry


def safe_failure_code(exc):
    if isinstance(exc, AcceptanceError):
        return exc.code
    if isinstance(exc, TimeoutError):
        return "acceptance_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_status_error"
    if isinstance(exc, httpx.RequestError):
        return "http_transport_error"
    return "unexpected_exception"


class EnvironmentAccount(OfficialAccountSession):
    """Only credential loading is replaced; real authenticated client requests remain intact."""

    identity = ""
    request_observer = None

    def __init__(self, *, base_url=None, client=None):
        self.base_url = (base_url or os.environ["AERO_OFFICIAL_API_URL"]).rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=60, trust_env=False)
        self._owns_client = client is None

    @property
    def data(self):
        return SimpleNamespace(user_id=self.identity, is_logged_in=True)

    async def access_token(self, *, force_refresh=False):
        if force_refresh:
            raise OfficialLoginRequiredError("Expired acceptance JWT; obtain a new token.")
        return os.environ["AERO_E2E_CLIENT_JWT"]

    async def request_to(self, base_url, method, path, *, retry_unauthorized=False, **kwargs):
        observer = type(self).request_observer
        if observer is not None and observer not in self._client.event_hooks["request"]:
            self._client.event_hooks["request"].append(observer)
        return await super().request_to(base_url, method, path, retry_unauthorized=False, **kwargs)


class IsolatedSessionManager(SessionManager):
    """Session storage uses a process-only encryption key, never the user's home directory."""

    def __init__(self, path):
        super().__init__(path)
        self.cipher = Fernet(Fernet.generate_key())

    def _encrypt(self, data):
        return self.cipher.encrypt(json.dumps(data, ensure_ascii=False).encode())

    def _decrypt(self, encrypted):
        return json.loads(self.cipher.decrypt(encrypted))


class ReadOnlyMcp(OfficialMcpClient):
    async def list_tools(self):
        await super().list_tools()  # Real discovery, but none permitted by acceptance policy.
        return []


async def account_json(http, method, path, token, **kwargs):
    response = await http.request(
        method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
    )
    if response.status_code >= 400:
        # Do not print response bodies, JWTs, signed URLs, or account details.
        raise AcceptanceError("account_api_http_failure")
    return response.json()


async def acquire_environment_tokens(*, cli_only=False):
    """Fill missing JWTs without persistent sessions, cookie reuse, or secret output."""
    kinds = (("cli", "AERO_E2E_CLIENT_JWT"),)
    if not cli_only:
        kinds += (("web", "AERO_E2E_WEB_JWT"),)
    if all(os.environ.get(name) for _, name in kinds):
        return
    account = os.environ.get("AERO_E2E_TEST_ACCOUNT") or os.environ.get("AERO_E2E_TEST_PHONE")
    password = os.environ.get("AERO_E2E_TEST_PASSWORD")
    if not account or not password:
        raise RuntimeError("Protected test-account credentials were not supplied")
    for kind, variable in kinds:
        if os.environ.get(variable):
            continue
        headers = {"X-Aero-Client": kind, "Cache-Control": "no-store"}
        body = {"account": account, "password": password, "remember_me": False}
        if kind == "cli":
            headers.update({"X-Aero-Device": "acceptance-isolated", "X-Aero-OS": "acceptance"})
        else:
            headers["X-Relay-Web-Auth"] = "1"
            ticket = os.environ.get("AERO_E2E_CAPTCHA_TICKET")
            randstr = os.environ.get("AERO_E2E_CAPTCHA_RANDSTR")
            if ticket and randstr:
                body.update(ticket=ticket, randstr=randstr)
        # Separate clients ensure CLI cookies do not carry over into the web login.
        async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as http:
            response = await http.post(
                os.environ["AERO_OFFICIAL_API_URL"].rstrip("/") + "/v1/auth/login",
                headers=headers,
                json=body,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Acceptance {kind} login failed: HTTP {response.status_code}")
            token = response.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("Login did not return an access token")
            os.environ[variable] = token


async def upload_attachment(http, agent_id, web_token, filename, content_type, data):
    prefix = f"/v1/agents/{agent_id}/attachments"
    ticket = await account_json(
        http,
        "POST",
        prefix,
        web_token,
        json={
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(data),
        },
    )
    url = httpx.URL(ticket["upload_url"])
    if url.scheme != "https" or url.userinfo:
        raise RuntimeError("Upload URL is not credential-free HTTPS")
    async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as oss:
        response = await oss.put(url, content=data, headers={"Content-Type": content_type})
        response.raise_for_status()
    await account_json(
        http, "POST", f"{prefix}/{ticket['attachment_id']}/complete", web_token, json={}
    )
    return ticket["attachment_id"]


async def wait_until(check, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(1)
    raise TimeoutError("Acceptance condition did not complete before its deadline")


async def linger_runtime(runtime, seconds, observe):
    """Keep the same restricted runtime alive; never acquire browser credentials."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if runtime._task.done():
            runtime._task.result()
            raise RuntimeError("Client runtime stopped during browser handoff")
        await observe()
        await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
    await observe()


def validate_command_wire(sent, command_id, model, mode, *, require_vision=False):
    """Main routing follows command settings; tool vision follows its separate config."""
    main = [item for item in sent if item["purpose"] == "main"]
    vision = [item for item in sent if item["purpose"] == "vision"]
    if (
        not main
        or (require_vision and not vision)
        or any(
            item["turn_id"] != command_id
            or item["header_turn_id"] != command_id
            or not item["idempotency_key"]
            or item["purpose"] not in {"main", "vision", "title", "compact"}
            for item in sent
        )
    ):
        raise AcceptanceError("wire_turn_purpose_identity_mismatch")
    if any(item["model"] != model or item["mode"] != mode for item in main):
        raise AcceptanceError("wire_main_model_mode_mismatch")
    if any(
        item["model"] != item["expected_vision_model"] or item["mode"] != mode for item in vision
    ):
        raise AcceptanceError("wire_vision_model_mode_mismatch")


async def accept(args):
    root = args.workspace.resolve()
    if not root.is_dir() or args.workspace.is_symlink() or any(root.iterdir()):
        raise RuntimeError("Workspace must be an existing empty dedicated directory")
    private_directory(root)
    configure_debug_logging(enabled=False, path=root / "debug-disabled.log")
    os.environ["AERO_SECRETS_PATH"] = str(root / "no-credentials.yaml")
    os.chdir(root)
    api_url = os.environ["AERO_OFFICIAL_API_URL"]
    for value in (api_url, os.environ["AERO_OFFICIAL_LLM_URL"]):
        url = httpx.URL(value)
        if url.scheme != "https" or url.userinfo or url.query or url.fragment:
            raise RuntimeError("API origins must be credential-free HTTPS")
    if args.login_from_env:
        await acquire_environment_tokens(cli_only=args.http_use_cli_bearer)
    client_token = os.environ["AERO_E2E_CLIENT_JWT"]
    web_token = client_token if args.http_use_cli_bearer else os.environ["AERO_E2E_WEB_JWT"]
    prefix = "accept-v2-" + secrets.token_hex(6)
    report = {
        "run": prefix,
        "workspace": str(root),
        "questions": [],
        "passed": False,
        "phase": "preflight",
        "http_assertion_auth": "cli_bearer" if args.http_use_cli_bearer else "web_bearer",
        "browser_login_tested": False,
    }
    report_path = root / "acceptance.json"
    runtime = None
    session = None
    with ExitStack() as stack:
        # These are credential adapters, not mocked inference, tools, downloads, or execution.
        for module in (
            "aero.core.official_account",
            "aero.core.official_models",
            "aero.core.agent_preferences",
            "aero.core.official_mcp",
            "aero.agent.official_vision_client",
        ):
            stack.enter_context(patch(f"{module}.OfficialAccountSession", EnvironmentAccount))
        stack.enter_context(
            patch(
                "aero.core.official_account.load_official_session",
                lambda: SimpleNamespace(is_logged_in=True),
            )
        )
        cache = private_directory(root / "vision-cache")
        stack.enter_context(patch("aero.data.vision_cache._cache_dir", lambda: cache))
        async with httpx.AsyncClient(base_url=api_url, timeout=60, trust_env=False) as http:
            async def record_runtime_response(response):
                if response.request.url.path.endswith("/runtime/acquire"):
                    await response.aread()
                    report["runtime_acquire"] = {"status": response.status_code}
                    if response.status_code >= 400:
                        report["runtime_acquire"]["detail"] = response.json().get("detail")
                    private_write(report_path, json.dumps(report, indent=2))

            http.event_hooks["response"] = [record_runtime_response]
            try:
                client_me = await account_json(http, "GET", "/v1/auth/me", client_token)
                web_me = await account_json(http, "GET", "/v1/auth/me", web_token)
                if client_me["user_id"] != web_me["user_id"]:
                    raise RuntimeError("CLI and web tokens must belong to the same account")
                EnvironmentAccount.identity = client_me["user_id"]
                prefs = await account_json(http, "GET", "/v1/agents/preferences", client_token)
                if prefs["max_rounds"] is not None and prefs["max_rounds"] < 4:
                    raise RuntimeError(
                        "Acceptance needs max_rounds >= 4; no preference changes made"
                    )
                if prefs.get("context_sync_configurable") is not False:
                    raise RuntimeError("Forced-sync deployment is not active")
                report["phase"] = "create_dedicated_project"
                project = await account_json(
                    http, "POST", "/v1/storage/projects", client_token, json={"name": prefix}
                )
                report["project_id"] = project["project_id"]
                report["phase"] = "create_dedicated_chat"
                registered = await account_json(
                    http,
                    "POST",
                    "/v1/agents/chats",
                    client_token,
                    json={"session_id": prefix, "title": prefix},
                )
                agent_id = registered["agent_id"]
                report.update(agent_id=agent_id, session_id=prefix)
                private_write(report_path, json.dumps(report, indent=2))
                config = AeroConfig()
                config.llm.provider = "official"
                config.llm.model = "auto"
                config.save(root / "aero.yaml")
                session = LocalSession(
                    root, config, prefix, session_manager=IsolatedSessionManager(root / "sessions")
                )
                # Only real read/vision tools are offered; no financial or destructive tools.
                session.agent.registry = restricted_registry()
                report["enabled_tools"] = sorted(
                    spec.name for spec in session.agent.registry.list_all()
                )
                session.agent._official_mcp = ReadOnlyMcp()
                wire = []

                async def record_request(request):
                    if request.url.path.endswith("/chat/completions"):
                        from aero.core.official_vision_policy import select_official_vision_model

                        body = json.loads(request.content)
                        wire.append(
                            {
                                "turn_id": body.get("relay_turn_id"),
                                "header_turn_id": request.headers.get("x-relay-turn-id"),
                                "purpose": request.headers.get("x-relay-question-purpose"),
                                "idempotency_key": request.headers.get("Idempotency-Key"),
                                "body_fingerprint": hashlib.sha256(request.content).hexdigest(),
                                "model": body["model"],
                                "tool_names": [
                                    tool["function"]["name"] for tool in body.get("tools", [])
                                ],
                                "mode": config.mode,
                                "expected_vision_model": (
                                    await select_official_vision_model(config.llm.model)
                                    if request.headers.get("x-relay-question-purpose") == "vision"
                                    else None
                                ),
                            }
                        )

                session.agent.llm._client.event_hooks["request"] = [record_request]
                stack.enter_context(
                    patch.object(EnvironmentAccount, "request_observer", record_request)
                )
                transport = RelayRuntimeTransport(
                    http, agent_id, registered["token"], session_token
                )
                original_transport_request = transport.request

                async def observe_transport(method, path, **kwargs):
                    report["runtime_last_request"] = {"method": method, "path": path}
                    private_write(report_path, json.dumps(report, indent=2))
                    try:
                        return await original_transport_request(method, path, **kwargs)
                    except Exception as exc:
                        report["runtime_last_error"] = {"type": type(exc).__name__}
                        if isinstance(exc, httpx.HTTPStatusError):
                            report["runtime_last_error"]["status"] = exc.response.status_code
                        private_write(report_path, json.dumps(report, indent=2))
                        raise

                transport.request = observe_transport
                runtime = CloudAgentRuntime(
                    session,
                    transport,
                    device_id=prefix,
                    project_id=project["project_id"],
                    account_id=EnvironmentAccount.identity,
                    account_identity=lambda: EnvironmentAccount.identity,
                    state_dir=root / "runtime",
                )
                await runtime.start()
                report["phase"] = "acquire_runtime"

                async def online():
                    if runtime._task.done():
                        runtime._task.result()
                        raise AcceptanceError("runtime_stopped_before_checks")
                    return runtime.state == "online"

                await wait_until(online, args.timeout)
                if args.serve_only:
                    report["phase"] = "browser_ready"
                    private_write(report_path, json.dumps(report, indent=2))
                    print(json.dumps({"agent_id": agent_id, "session": prefix, "state": "online"}), flush=True)
                    async def observe_browser():
                        report["runtime_state"] = runtime.state
                        report["observed_wire"] = wire
                        private_write(report_path, json.dumps(report, indent=2))
                    await linger_runtime(runtime, args.linger_seconds, observe_browser)
                    return
                # A genuine foreground turn seeds the same LocalSession before web delivery.
                marker = "continuity-" + secrets.token_hex(5)
                report["phase"] = "foreground_seed"
                seed = session.start_run(f"Remember the continuity marker {marker}. Reply only OK.")
                await session.wait_run(seed)
                if session.run_status(seed)["state"] != "completed":
                    raise AcceptanceError("foreground_seed_failed")
                text_secret = "text-check-" + secrets.token_hex(5)
                report["phase"] = "upload_small_attachments"
                text_id = await upload_attachment(
                    http,
                    agent_id,
                    web_token,
                    "notes.txt",
                    "text/plain",
                    f"Acceptance file marker: {text_secret}".encode(),
                )
                image = io.BytesIO()
                Image.new("RGB", (24, 24), "red").save(image, format="PNG")
                image_id = await upload_attachment(
                    http, agent_id, web_token, "red.png", "image/png", image.getvalue()
                )
                for mode, model, attachment_ids in (
                    ("plan", "auto", [text_id]),
                    ("execute", args.manual_model, [image_id]),
                    ("qa", "auto", []),
                ):
                    report["phase"] = f"web_{mode}"
                    count = len(session.agent.messages)
                    prompt = (
                        "Acceptance check only. Do not search, change files or request secrets. "
                        "Repeat the remembered continuity marker. "
                    )
                    if mode == "plan":
                        prompt += "Read the attached text with read_file; repeat its file marker."
                    elif mode == "execute":
                        prompt += (
                            "Use analyze_image on the attached PNG; report its dominant color."
                        )
                    else:
                        prompt += "Repeat the text-file marker from the previous turn."
                    result = await account_json(
                        http,
                        "POST",
                        f"/v1/agents/{agent_id}/messages",
                        web_token,
                        json={
                            "client_message_id": prefix + "-" + mode,
                            "content": prompt,
                            "mode": mode,
                            "model": model,
                            "attachments": attachment_ids,
                        },
                    )
                    message_id = result["message"]["message_id"]

                    async def completed():
                        messages = await account_json(
                            http, "GET", f"/v1/agents/{agent_id}/messages", web_token
                        )
                        row = next(m for m in messages["messages"] if m["message_id"] == message_id)
                        state = row["delivery_status"]
                        if state in {"failed", "canceled", "interrupted"}:
                            raise AcceptanceError("remote_command_" + state)
                        return row if state == "completed" else None

                    row = await wait_until(completed, args.timeout)
                    command_id = row["command_id"]
                    sent = [item for item in wire if item["turn_id"] == command_id]
                    validate_command_wire(
                        sent, command_id, model, mode, require_vision=mode == "execute"
                    )
                    identities = {}
                    for item in wire:
                        key = item["idempotency_key"]
                        fingerprint = (item["turn_id"], item["body_fingerprint"])
                        if key in identities and identities[key] != fingerprint:
                            raise AcceptanceError("idempotency_key_body_turn_reused")
                        identities[key] = fingerprint
                    new_messages = session.agent.messages[count:]
                    if not any(
                        m.role == "user" and m.message_id == command_id for m in new_messages
                    ):
                        raise AcceptanceError("command_missing_from_local_context")
                    answer = "\n".join(m.content for m in new_messages if m.role == "assistant")
                    if marker not in answer or (
                        mode in {"plan", "qa"} and text_secret not in answer
                    ):
                        raise AcceptanceError("context_file_marker_missing")
                    if mode == "execute" and not any(
                        call.name == "analyze_image"
                        for m in new_messages
                        for call in m.tool_calls or []
                    ):
                        raise AcceptanceError("image_vision_tool_not_called")
                    if mode == "execute" and "red" not in answer.lower() and "红" not in answer:
                        raise AcceptanceError("image_color_not_identified")
                    if (config.mode, config.llm.model) != ("execute", "auto"):
                        raise AcceptanceError("command_overrides_not_restored")

                    async def fees_ready():
                        usage = await account_json(
                            http,
                            "GET",
                            f"/v1/agents/{agent_id}/messages/{message_id}/usage",
                            web_token,
                        )
                        return (
                            usage
                            if (
                                usage.get("request_count", 0) > 0
                                and usage.get("cost_status") == "settled"
                                and usage.get("pending_request_count") == 0
                            )
                            else None
                        )

                    fees = await wait_until(fees_ready, args.timeout)
                    if fees["turn_id"] != command_id or fees["cost_milli"] < 0:
                        raise AcceptanceError("question_fee_association_mismatch")
                    report["questions"].append(
                        {
                            "mode": mode,
                            "model": model,
                            "message_id": message_id,
                            "command_id": command_id,
                            "usage": fees,
                            "wire": sent,
                        }
                    )
                    private_write(report_path, json.dumps(report, indent=2))
                await runtime._sync_context()
                saved = await account_json(http, "GET", f"/v1/agents/{agent_id}/context", web_token)
                if marker not in json.dumps(saved["context"]):
                    raise AcceptanceError("cloud_context_marker_missing")
                report["scripted_checks_passed"] = True
                if args.linger_seconds:
                    report["phase"] = "browser_handoff"
                    report["linger_seconds"] = args.linger_seconds
                    report["handoff_observations"] = []
                    start = len(session.agent.messages)

                    async def observe_handoff():
                        # Record identifiers/wire only, never user text or browser JWTs.
                        report["handoff_observations"] = [
                            {
                                "turn_id": message.message_id,
                                "wire": [
                                    item for item in wire if item["turn_id"] == message.message_id
                                ],
                            }
                            for message in session.agent.messages[start:]
                            if message.role == "user"
                        ]
                        private_write(report_path, json.dumps(report, indent=2))

                    await observe_handoff()
                    print(
                        json.dumps(
                            {
                                "phase": "browser_handoff",
                                "agent_id": agent_id,
                                "session_id": prefix,
                                "linger_seconds": args.linger_seconds,
                                "report_path": str(report_path),
                                "browser_login_tested": False,
                            }
                        ),
                        flush=True,
                    )
                    await linger_runtime(runtime, args.linger_seconds, observe_handoff)
                report["passed"] = True
                report["phase"] = "complete"
            except Exception as exc:
                report["failure_type"] = type(exc).__name__
                report["failure_code"] = safe_failure_code(exc)
                report["observed_wire"] = wire if "wire" in locals() else []
                raise
            finally:
                if runtime:
                    await runtime.stop()
                if session:
                    await session.close()
                private_write(report_path, json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


async def session_token():
    return os.environ["AERO_E2E_CLIENT_JWT"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--manual-model", required=True, help="Available official main-agent model ID"
    )
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--allow-model-charges", action="store_true")
    parser.add_argument("--serve-only", action="store_true", help="Keep real runtime for browser acceptance")
    parser.add_argument(
        "--http-use-cli-bearer",
        action="store_true",
        help="Use CLI JWT for HTTP assertions; does not test browser login or bypass CAPTCHA",
    )
    parser.add_argument(
        "--linger-seconds",
        type=int,
        default=0,
        help="Keep the restricted runtime alive after checks for manual browser commands",
    )
    parser.add_argument(
        "--login-from-env",
        action="store_true",
        help="Acquire missing CLI/web JWTs from protected test-account environment",
    )
    args = parser.parse_args()
    if not 0 <= args.linger_seconds <= 3600:
        parser.error("--linger-seconds must be between 0 and 3600")
    if not args.allow_model_charges:
        parser.error("This real-client check spends model credits; pass --allow-model-charges")
    required = (
        "AERO_OFFICIAL_API_URL",
        "AERO_OFFICIAL_LLM_URL",
    )
    if any(not os.environ.get(name) for name in required):
        parser.error("Provide API/LLM URLs through the protected environment")
    jwt_names = ("AERO_E2E_CLIENT_JWT",)
    if not args.http_use_cli_bearer:
        jwt_names += ("AERO_E2E_WEB_JWT",)
    missing_jwts = any(not os.environ.get(name) for name in jwt_names)
    if missing_jwts and not args.login_from_env:
        parser.error("Provide CLI/web JWTs or use --login-from-env with protected test credentials")
    try:
        asyncio.run(accept(args))
    except Exception as exc:
        print(
            f"Acceptance FAILED ({safe_failure_code(exc)}; {type(exc).__name__}); "
            "inspect the non-secret acceptance.json report."
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
