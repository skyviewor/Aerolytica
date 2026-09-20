"""Tests for the standalone official-account login command."""

from __future__ import annotations

import pytest

import aero.cli.main as cli_main
from aero.cli.main import OfficialLoginApp, OfficialLoginScreen, run_official_login
from aero.core.config import load_user_secrets


def test_login_command_activates_official_provider(monkeypatch, tmp_path):
    class SuccessfulLoginApp:
        def run(self):
            return True

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    monkeypatch.setenv("AERO_OFFICIAL_LLM_URL", "https://llm.test/v1")
    monkeypatch.setattr("aero.cli.main.OfficialLoginApp", SuccessfulLoginApp)

    assert run_official_login() == 0

    llm = load_user_secrets()["llm"]
    assert llm["active_provider"] == "official"
    assert llm["providers"]["official"] == {
        "api_key": "",
        "model": "auto",
        "base_url": "https://llm.test/v1",
    }


def test_login_command_reports_cancel(monkeypatch):
    class CancelledLoginApp:
        def run(self):
            return False

    monkeypatch.setattr("aero.cli.main.OfficialLoginApp", CancelledLoginApp)

    assert run_official_login() == 1


@pytest.mark.asyncio
async def test_login_command_opens_only_the_login_dialog():
    app = OfficialLoginApp()

    async with app.run_test() as pilot:
        assert isinstance(app.screen, OfficialLoginScreen)
        await pilot.press("escape")


def test_main_dispatches_login_command(monkeypatch, tmp_path):
    called: list[bool] = []
    monkeypatch.setattr("aero.cli.main.configure_debug_logging", lambda: tmp_path / "debug.log")
    monkeypatch.setattr("aero.cli.main.configure_logging", lambda **_: None)
    monkeypatch.setattr("aero.cli.main.run_official_login", lambda: called.append(True) or 0)
    monkeypatch.setattr("aero.cli.main.sys.argv", ["aero", "login"])

    cli_main.main()

    assert called == [True]
