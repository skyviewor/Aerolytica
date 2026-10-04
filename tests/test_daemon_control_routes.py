from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from aero.core.daemon import DaemonManager, run_daemon


def test_daemon_control_precedes_spa_fallback(tmp_path, monkeypatch):
    app = FastAPI()

    @app.get("/{path:path}")
    async def fallback(path: str):
        return HTMLResponse("SPA fallback")

    runtime = SimpleNamespace(
        active_sessions={}, cloud=SimpleNamespace(agents={}), update_paused=False
    )
    monkeypatch.setattr("aero.server.app.create_app", lambda *args, **kwargs: (app, runtime))
    manager = DaemonManager(tmp_path, tmp_path / "daemon-state")
    manager.state_dir.mkdir()

    class Server:
        def __init__(self, config):
            self.should_exit = False

        def run(self, sockets):
            state = manager._read()
            with TestClient(app) as client:
                assert client.get("/api/v1/daemon/status").status_code == 401
                headers = {"Authorization": f"Bearer {state['token']}"}
                result = client.get("/api/v1/daemon/status", headers=headers)
                assert result.json()["instance_id"] == state["instance_id"]
                assert result.json()["protocol"] == 1
                assert not result.json()["update_busy"]
                assert client.post("/api/v1/daemon/update-stop", headers=headers).status_code == 409
                assert client.post("/api/v1/daemon/drain", headers=headers).json()["update_paused"]
                assert not client.post("/api/v1/daemon/resume", headers=headers).json()[
                    "update_paused"
                ]
                assert client.get("/other-page").text == "SPA fallback"
                denied = client.post(
                    "/api/v1/daemon/stop",
                    headers={**headers, "Origin": "http://127.0.0.1"},
                )
                assert denied.status_code == 403
                assert not self.should_exit
                assert client.post("/api/v1/daemon/stop", headers=headers).json() == {
                    "stopping": True
                }
                assert self.should_exit

    monkeypatch.setattr("uvicorn.Server", Server)
    monkeypatch.setattr("uvicorn.Config", lambda *args, **kwargs: SimpleNamespace())
    run_daemon(manager)
    assert not manager.state_path.exists()
