from __future__ import annotations

from fastapi.testclient import TestClient

from aero.agent.llm_client import StreamEvent
from aero.agent.session import SessionManager
from aero.core.types import Message
from aero.server.app import create_app


def _client(tmp_path, monkeypatch):
    storage = tmp_path / "sessions"
    monkeypatch.setattr(
        "aero.server.app.SessionManager",
        lambda: SessionManager(storage),
    )
    app, runtime = create_app(tmp_path, launch_token="test-token")
    client = TestClient(app)
    client.cookies.set("aero_access", "test-token")
    return client, runtime


def test_web_requires_launch_cookie(tmp_path):
    app, _ = create_app(tmp_path, launch_token="test-token")
    client = TestClient(app)
    assert client.get("/api/v1/bootstrap").status_code == 401
    assert client.get("/?token=test-token", follow_redirects=False).status_code == 303


def test_artifact_route_rejects_escape_and_serves_text(tmp_path, monkeypatch):
    source = tmp_path / "notes.md"
    source.write_text("hello", encoding="utf-8")
    client, _ = _client(tmp_path, monkeypatch)

    tree = client.get("/api/v1/workspace/tree?path=.")
    assert tree.status_code == 200
    artifact_id = next(
        item["artifact_id"] for item in tree.json()["items"] if item["name"] == "notes.md"
    )
    assert client.get(f"/api/v1/artifacts/{artifact_id}/text").json() == {"text": "hello"}
    assert client.get("/api/v1/workspace/tree?path=../../").status_code == 400


def test_run_stream_emits_structured_events(tmp_path, monkeypatch):
    client, runtime = _client(tmp_path, monkeypatch)
    created = client.post("/api/v1/sessions")
    session_id = created.json()["id"]
    session = runtime.active_sessions[session_id]

    async def fake_run(_prompt: str):
        yield StreamEvent(type="text", content="hello")
        yield StreamEvent(type="done")

    session.agent.run_stream = fake_run
    run = client.post(f"/api/v1/sessions/{session_id}/runs", json={"prompt": "hi"}).json()
    response = client.get(f"/api/v1/runs/{run['run_id']}/events?session_id={session_id}")
    assert response.status_code == 200
    assert "assistant_delta" in response.text
    assert "run_completed" in response.text


def test_missing_vision_setup_does_not_wait_for_a_secret(tmp_path, monkeypatch):
    client, runtime = _client(tmp_path, monkeypatch)
    session_id = client.post("/api/v1/sessions").json()["id"]
    session = runtime.active_sessions[session_id]

    async def fake_run(_prompt: str):
        yield StreamEvent(
            type="status",
            content='{"setup_required":"vision","credential_request":{}}',
        )
        yield StreamEvent(type="text", content="无法分析图片，但任务可以继续。")
        yield StreamEvent(type="done")

    session.agent.run_stream = fake_run
    run = client.post(f"/api/v1/sessions/{session_id}/runs", json={"prompt": "看图"}).json()
    response = client.get(f"/api/v1/runs/{run['run_id']}/events?session_id={session_id}")

    assert response.status_code == 200
    assert "vision_setup_required" in response.text
    assert "secret_required" not in response.text
    assert "run_completed" in response.text


def test_run_generates_and_persists_automatic_session_title(tmp_path, monkeypatch):
    client, runtime = _client(tmp_path, monkeypatch)
    runtime.config.llm.set_active_api_key("sk-title-test")
    created = client.post("/api/v1/sessions")
    session_id = created.json()["id"]
    session = runtime.active_sessions[session_id]
    session.agent.llm.relay_turn_id = "turn-title-test"
    title_clients = []

    async def fake_run(prompt: str):
        session.agent.messages.append(Message(role="user", content=prompt))
        yield StreamEvent(type="text", content="完成研究任务")
        yield StreamEvent(type="done")

    class FakeTitleClient:
        def __init__(self, _config):
            self.relay_turn_id = ""
            title_clients.append(self)

        async def chat(self, _messages):
            return "华北气温研究"

        async def close(self):
            return None

    session.agent.run_stream = fake_run
    monkeypatch.setattr("aero.application.local_session.LLMClient", FakeTitleClient)
    run = client.post(f"/api/v1/sessions/{session_id}/runs", json={"prompt": "分析华北气温"}).json()
    response = client.get(f"/api/v1/runs/{run['run_id']}/events?session_id={session_id}")

    assert response.status_code == 200
    assert "session_title_updated" in response.text
    assert runtime.active_sessions[session_id].metadata()["name"] == "华北气温研究"
    assert runtime.sessions.load(session_id)[1].title_source == "auto"
    assert title_clients[0].relay_turn_id == "session-title:turn-title-test"


def test_settings_never_returns_secret(tmp_path, monkeypatch):
    client, runtime = _client(tmp_path, monkeypatch)
    runtime.config.llm.set_active_api_key("sk-test-secret")
    settings = client.get("/api/v1/settings").json()
    assert settings["llm_configured"] is True
    assert "sk-test-secret" not in repr(settings)


def test_session_can_be_deleted(tmp_path, monkeypatch):
    client, runtime = _client(tmp_path, monkeypatch)
    session_id = client.post("/api/v1/sessions").json()["id"]

    response = client.delete(f"/api/v1/sessions/{session_id}")

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert runtime.session(session_id) is None


def test_primary_setup_tests_before_persisting(tmp_path, monkeypatch):
    client, runtime = _client(tmp_path, monkeypatch)
    session_id = client.post("/api/v1/sessions").json()["id"]
    calls = []

    class FakeClient:
        def __init__(self, config):
            calls.append(config)

        async def chat(self, _messages):
            return "OK"

        async def close(self):
            return None

    monkeypatch.setattr("aero.server.app.LLMClient", FakeClient)
    monkeypatch.setattr("aero.server.app.save_llm_profile", lambda *args: None)
    response = client.post(
        "/api/v1/settings/setup",
        json={
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-web-test",
        },
    )
    assert response.status_code == 200
    assert calls[0].model == "deepseek-v4-flash"
    assert calls[0].max_tokens == 1
    assert runtime.config.llm.active_api_key() == "sk-web-test"
    assert runtime.active_sessions[session_id].agent.llm.config.api_key == "sk-web-test"
    assert response.json()["llm_configured"] is True
