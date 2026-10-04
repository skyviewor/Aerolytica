"""Acceptance login uses correct session kinds without printing/persisting credentials."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "accept_web_conversation_v2.py"
    spec = importlib.util.spec_from_file_location("acceptance_login_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_login_uses_headers_and_only_keeps_access_jwts(monkeypatch, tmp_path, capsys):
    module = load_script()
    for name in ("AERO_E2E_CLIENT_JWT", "AERO_E2E_WEB_JWT"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("AERO_OFFICIAL_API_URL", "https://relay.test")
    monkeypatch.setenv("AERO_E2E_TEST_PHONE", "13800000000")
    monkeypatch.setenv("AERO_E2E_TEST_PASSWORD", "protected-test-password")
    monkeypatch.setenv("AERO_E2E_CAPTCHA_TICKET", "protected-test-ticket")
    monkeypatch.setenv("AERO_E2E_CAPTCHA_RANDSTR", "protected-test-randstr")
    monkeypatch.chdir(tmp_path)
    requests = []
    original = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        kind = request.headers["X-Aero-Client"]
        return httpx.Response(
            200,
            json={"access_token": kind + "-test-jwt", "refresh_token": "never-kept-refresh"},
            headers={"Set-Cookie": "test-cookie=private; Path=/"},
        )

    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    await module.acquire_environment_tokens()
    assert module.os.environ["AERO_E2E_CLIENT_JWT"] == "cli-test-jwt"
    assert module.os.environ["AERO_E2E_WEB_JWT"] == "web-test-jwt"
    assert len(requests) == 2
    assert "X-Relay-Web-Auth" not in requests[0].headers
    assert requests[1].headers["X-Relay-Web-Auth"] == "1"
    assert all("Cookie" not in request.headers for request in requests)
    assert all("client_kind" not in json.loads(request.content) for request in requests)
    assert json.loads(requests[1].content)["ticket"] == "protected-test-ticket"
    assert not list(tmp_path.iterdir())
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_existing_tokens_require_no_password_or_login(monkeypatch):
    module = load_script()
    monkeypatch.setenv("AERO_E2E_CLIENT_JWT", "existing-client")
    monkeypatch.setenv("AERO_E2E_WEB_JWT", "existing-web")
    for name in ("AERO_E2E_TEST_ACCOUNT", "AERO_E2E_TEST_PHONE", "AERO_E2E_TEST_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    await module.acquire_environment_tokens()


@pytest.mark.asyncio
async def test_cli_only_login_never_attempts_web_captcha(monkeypatch):
    module = load_script()
    monkeypatch.setenv("AERO_E2E_CLIENT_JWT", "")
    monkeypatch.delenv("AERO_E2E_WEB_JWT", raising=False)
    monkeypatch.setenv("AERO_OFFICIAL_API_URL", "https://relay.test")
    monkeypatch.setenv("AERO_E2E_TEST_PHONE", "13800000000")
    monkeypatch.setenv("AERO_E2E_TEST_PASSWORD", "protected-test-password")
    requests = []
    original = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        assert request.headers["X-Aero-Client"] == "cli"
        assert "X-Relay-Web-Auth" not in request.headers
        return httpx.Response(200, json={"access_token": "cli-test-jwt"})

    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    await module.acquire_environment_tokens(cli_only=True)
    assert len(requests) == 1
    assert "AERO_E2E_WEB_JWT" not in module.os.environ


@pytest.mark.asyncio
async def test_login_failure_does_not_echo_secret_response(monkeypatch, capsys):
    module = load_script()
    monkeypatch.delenv("AERO_E2E_CLIENT_JWT", raising=False)
    monkeypatch.setenv("AERO_E2E_WEB_JWT", "existing-web")
    monkeypatch.setenv("AERO_OFFICIAL_API_URL", "https://relay.test")
    monkeypatch.setenv("AERO_E2E_TEST_ACCOUNT", "test@example.test")
    monkeypatch.setenv("AERO_E2E_TEST_PASSWORD", "protected-test-password")
    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original(
            **kwargs,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(401, json={"detail": "protected-test-password"})
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="HTTP 401") as error:
        await module.acquire_environment_tokens()
    assert "protected-test-password" not in str(error.value)
    assert capsys.readouterr().out == ""
