from types import SimpleNamespace
from unittest.mock import Mock

from aero.cli import main as cli


def test_cloud_presence_quota_full_does_not_display_toast(monkeypatch):
    log = Mock()
    monkeypatch.setattr(cli, "debug_log", log)
    app = SimpleNamespace(notify=Mock(), _set_footer_status=Mock())
    for _ in range(3):
        cli.AeroApp._handle_chat_presence_quota_full(app)
    app.notify.assert_not_called()
    app._set_footer_status.assert_not_called()
    assert log.call_count == 3
    log.assert_called_with("chat_presence.quota_full")
