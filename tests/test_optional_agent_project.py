from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aero.application.cloud_runtime import CloudAgentRuntime
from aero.cli.agent_runner import _workspace_project_id
from aero.core.remote_agent import RemoteAgentError


def test_workspace_without_cloud_binding_can_start(tmp_path):
    assert _workspace_project_id(tmp_path) is None


@pytest.mark.parametrize(
    "state,expected",
    [
        ("{}", None),
        ('{"binding": {"project_id": "existing"}}', "existing"),
    ],
)
def test_optional_workspace_binding(tmp_path, state, expected):
    path = tmp_path / ".aero" / "cloud-sync" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(state)
    assert _workspace_project_id(tmp_path) == expected


def test_corrupt_binding_does_not_silently_disable_project_safety(tmp_path):
    path = tmp_path / ".aero" / "cloud-sync" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text("not-json")
    with pytest.raises(RemoteAgentError):
        _workspace_project_id(tmp_path)


async def test_unbound_report_keeps_agent_lease_guard_without_project_requests():
    worker = CloudAgentRuntime.__new__(CloudAgentRuntime)
    worker.project_id = None
    worker.command_id = None
    worker._assert_lease = Mock()
    worker.transport = SimpleNamespace(request=AsyncMock())

    async def events(_run_id):
        if False:
            yield

    worker.session = SimpleNamespace(events=events)
    worker._queue_foreground_messages = Mock()
    async with worker.project_execution("report-run"):
        assert worker._foreground_execution == "report-run"
    worker._assert_lease.assert_called_once()
    worker.transport.request.assert_not_awaited()
    worker._queue_foreground_messages.assert_called_once_with("report-run", None)
