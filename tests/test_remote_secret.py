"""Encrypted cloud secret entry with fake credentials and no external services."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aero.application.cloud_runtime import CloudAgentRuntime
from aero.application.events import RunEvent
from aero.application.local_session import LocalSession
from aero.application.remote_secret import SecretKey, associated_data
from aero.toolbox.secret_input import (
    CredentialSpec,
    SecretInputRequest,
    register_credential_spec,
    save_secret_from_context,
    use_secret_input_provider,
)


def encrypt(public_key, text, aad):
    public = serialization.load_der_public_key(base64.b64decode(public_key))
    key, iv = os.urandom(32), os.urandom(12)
    wrapped = public.encrypt(
        key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    )
    return {
        "key": base64.b64encode(wrapped).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(AESGCM(key).encrypt(iv, text.encode(), aad)).decode(),
    }


def test_envelope_roundtrip_bound_to_agent_command_and_request():
    key = SecretKey()
    aad = associated_data("agent", "command", "request")
    envelope = encrypt(key.public_key, "fake CDS secret", aad)
    assert "fake CDS secret" not in json.dumps(envelope)
    assert key.decrypt(envelope, aad) == "fake CDS secret"
    for other in (associated_data("other", "command", "request"), b"wrong"):
        with pytest.raises(Exception):
            key.decrypt(envelope, other)
    with pytest.raises(Exception):
        SecretKey().decrypt(envelope, aad)  # Lost keys cannot claim restoration.


@pytest.mark.asyncio
async def test_invalid_format_stays_pending_and_saved_result_is_consumed_once():
    calls = []
    register_credential_spec(
        CredentialSpec(
            "test_remote",
            "Test",
            "Instructions",
            True,
            lambda value: (
                calls.append(value) or {"status": "success" if value == "valid" else "error"}
            ),
        )
    )
    session = LocalSession.__new__(LocalSession)
    session._pending_secrets = {"local": asyncio.get_running_loop().create_future()}
    session._secret_values = {}
    assert not session.submit_remote_secret("local", "test_remote", "invalid")
    assert not session._pending_secrets["local"].done()
    assert session.submit_remote_secret("local", "test_remote", "valid")
    handle = await session._pending_secrets["local"]
    assert "valid" not in repr(session._secret_values)
    with use_secret_input_provider(AsyncMock(), session._resolve_secret):
        assert save_secret_from_context("test_remote", handle) == {"status": "success"}
        assert save_secret_from_context("test_remote", handle)["status"] == "error"
    assert calls == ["invalid", "valid"]  # No second save by the model tool.
    with pytest.raises(RuntimeError):
        session.submit_remote_secret("local", "test_remote", "valid")


@pytest.mark.asyncio
async def test_secret_future_resumes_original_waiter_without_new_task():
    register_credential_spec(
        CredentialSpec("test_resume", "Test", "Instructions", True, lambda _: {"status": "success"})
    )
    session = LocalSession.__new__(LocalSession)
    session._run_states = {}
    session._pending_secrets = {}
    session._secret_values = {}
    events = []
    session._emit_for_active_run = lambda kind, data: events.append((kind, data))
    waiter = asyncio.create_task(
        session._request_secret(SecretInputRequest("test_resume", "Test", "Instructions", True))
    )
    await asyncio.sleep(0)
    request_id = events[0][1]["request_id"]
    session.submit_remote_secret(request_id, "test_resume", "mock-secret")
    result = await waiter
    assert result["status"] == "ready"
    assert "mock-secret" not in repr(events) + repr(result)
    assert not session._pending_secrets


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [None, "canceled", "expired"])
async def test_cloud_wait_retry_and_terminal_states(tmp_path, terminal):
    scope = "test_cloud"
    register_credential_spec(
        CredentialSpec(
            scope, "Test", "Registered instructions", True, lambda _: {"status": "success"}
        )
    )
    session = SimpleNamespace(
        id="session",
        project_dir=tmp_path,
        cancel=AsyncMock(),
        submit_remote_secret=lambda *args: args[-1] == "valid",
    )
    registered, reports, envelopes, acknowledgments = [], [], {}, []
    disconnected = False

    async def request(method, path, **kwargs):
        nonlocal disconnected
        if path.endswith("/secrets"):
            body = kwargs["json"]
            registered.append(body)
            request_id = body["request_id"]
            value = "invalid" if len(registered) == 1 else "valid"
            envelopes[request_id] = encrypt(
                body["public_key"], value, associated_data("agent", "command", request_id)
            )
            return {}
        request_id = path.split("/")[-2]
        if path.endswith("/claim"):
            if terminal:
                return {"request": {"status": terminal}}
            if not disconnected:
                disconnected = True
                raise httpx.ConnectError("temporary offline")
            return {"request": {"status": "submitted"}, "envelope": envelopes[request_id]}
        if path.endswith("/ack"):
            acknowledgments.append(request_id)
            return {}
        if path.endswith("/result"):
            reports.append(kwargs["json"]["status"])
            return {}
        raise AssertionError(path)

    transport = SimpleNamespace(agent_id="agent", request=request)
    runtime = CloudAgentRuntime(
        session,
        transport,
        device_id="device",
        project_id="project",
        account_id="user",
        account_identity=lambda: "user",
        state_dir=tmp_path / "state",
    )
    runtime._assert_lease = lambda: None
    event = RunEvent(
        1, "session", "run", "secret_required", {"scope": scope, "request_id": "local"}
    )
    await runtime._secret("command", "run", event)
    if terminal:
        session.cancel.assert_awaited_once_with("run")
        assert not reports
    else:
        assert reports == ["failed", "success"]
        assert len(set(acknowledgments)) == 2
        assert registered[0]["public_key"] != registered[1]["public_key"]
        assert runtime.state == "running"
        session.cancel.assert_not_awaited()
        assert "invalid" not in json.dumps(registered)


@pytest.mark.asyncio
async def test_execute_continues_same_run_and_tool_after_safe_configuration(tmp_path):
    saved = []
    register_credential_spec(
        CredentialSpec(
            "test_execute",
            "Test",
            "Instructions",
            True,
            lambda secret: saved.append(secret) or {"status": "success"},
        )
    )
    session = LocalSession.__new__(LocalSession)
    session.id, session.project_dir = "session", tmp_path
    session._secret_values = {}
    future = asyncio.get_running_loop().create_future()
    session._pending_secrets = {"local": future}
    session.cancel = AsyncMock()
    starts, executed_tools, replies = [], [], []

    def start_run(content, *, origin):
        assert origin == "web"
        starts.append(content)
        return "original-run"

    async def events(run_id):
        assert run_id == "original-run"
        yield RunEvent(
            1,
            session.id,
            run_id,
            "secret_required",
            {
                "scope": "test_execute",
                "request_id": "local",
            },
        )
        handle = await future
        with use_secret_input_provider(AsyncMock(), session._resolve_secret):
            assert save_secret_from_context("test_execute", handle)["status"] == "success"
        executed_tools.append("original-tool")
        yield RunEvent(2, session.id, run_id, "assistant_delta", {"text": "配置成功，原任务已继续"})

    session.start_run, session.events = start_run, events
    session.run_status = lambda _: {"state": "completed"}
    registered = {}

    async def request(method, path, **kwargs):
        if path.endswith("/secrets"):
            registered.update(kwargs["json"])
        elif path.endswith("/claim"):
            return {
                "request": {"status": "submitted"},
                "envelope": encrypt(
                    registered["public_key"],
                    "simulated-secret",
                    associated_data("agent", "command", registered["request_id"]),
                ),
            }
        elif path == "events":
            replies.append(kwargs["json"])
        return {}

    runtime = CloudAgentRuntime(
        session,
        SimpleNamespace(agent_id="agent", request=request),
        device_id="device",
        project_id="project",
        account_id="user",
        account_identity=lambda: "user",
        state_dir=tmp_path / "state",
    )
    runtime._assert_lease = lambda: None
    await runtime._execute({"command_id": "command", "content": "original-task"})
    assert starts == ["original-task"]
    assert saved == ["simulated-secret"]
    assert executed_tools == ["original-tool"]
    assert "simulated-secret" not in repr(replies)
    for path in (tmp_path / "state").glob("*"):
        assert "simulated-secret" not in path.read_text()


@pytest.mark.asyncio
async def test_setup_hint_does_not_pause_or_register_secret_request(tmp_path):
    transport = SimpleNamespace(agent_id="agent", request=AsyncMock())
    session = SimpleNamespace(id="session", project_dir=tmp_path)
    runtime = CloudAgentRuntime(
        session,
        transport,
        device_id="device",
        project_id="project",
        account_id="user",
        account_identity=lambda: "user",
        state_dir=tmp_path / "state",
    )
    await runtime._secret(
        "command",
        "run",
        RunEvent(
            1,
            "session",
            "run",
            "secret_required",
            {
                "setup_required": "cds",
                "credential_request": {"scope": "cds"},
            },
        ),
    )
    transport.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreground_web_command_uses_inbox_and_never_terminal_dialog():
    from aero.application.chat_presence import ChatPresence
    from aero.cli.main import AeroApp

    saved = []
    scope = "test_foreground_web"
    register_credential_spec(
        CredentialSpec(
            scope,
            "CDS",
            "Registered instructions",
            True,
            lambda value: saved.append(value) or {"status": "success"},
        )
    )
    presence = ChatPresence.__new__(ChatPresence)
    presence.agent_id, presence.token, presence._running = "agent", "agt_token", True
    presence._active_command_id = "web-command"
    registered, called = {}, []

    async def post(url, **kwargs):
        called.append(url)
        if url.endswith("/secrets"):
            registered.update(kwargs["json"])
        if url.endswith("/claim"):
            payload = {
                "request": {"status": "submitted"},
                "envelope": encrypt(
                    registered["public_key"],
                    "simulated-web-secret",
                    associated_data("agent", "web-command", registered["request_id"]),
                ),
            }
        else:
            payload = {}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    presence._http = SimpleNamespace(post=post)
    app = SimpleNamespace(_chat_presence=presence, _secret_handles={}, push_screen_wait=AsyncMock())
    result = await AeroApp._request_cloud_secret_input(
        app, SecretInputRequest(scope, "Model title", "Model instructions", True)
    )
    assert result["status"] == "submitted"
    assert saved == ["simulated-web-secret"]
    assert registered["description"]["instructions"] == "Registered instructions"
    assert any(url.endswith("/ack") for url in called)
    assert any(url.endswith("/result") for url in called)
    app.push_screen_wait.assert_not_awaited()
    assert "simulated-web-secret" not in repr(app._secret_handles) + repr(result)
    with use_secret_input_provider(AsyncMock(), app._secret_handles.pop):
        assert save_secret_from_context(scope, result["secret_handle"]) == {"status": "success"}
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_web_credential_unavailable_never_falls_back_to_terminal():
    from aero.cli.main import AeroApp

    app = SimpleNamespace(_chat_presence=None, push_screen_wait=AsyncMock())
    result = await AeroApp._request_cloud_secret_input(app, SecretInputRequest("cds", "", "", True))
    assert result["status"] == "unavailable"
    app.push_screen_wait.assert_not_awaited()
