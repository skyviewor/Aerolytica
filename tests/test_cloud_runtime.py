"""Tests for resident-agent local state that must survive a restart."""

import stat

from aero.application.cloud_runtime import CloudAgentRuntime


class _Session:
    id = "session_1"


class _Transport:
    agent_id = "agent_1"


def _runtime(tmp_path):
    return CloudAgentRuntime(
        _Session(), _Transport(), device_id="device_1", project_id="project_1",
        account_id="user_1", account_identity=lambda: "user_1", state_dir=tmp_path,
    )


def test_memory_configuration_survives_runtime_restart(tmp_path):
    first = _runtime(tmp_path)
    enabled = first.enable_memory()

    second = _runtime(tmp_path)

    assert second.status()["memory_enabled"] is True
    assert second._memory.recovery_key == enabled["recovery_key"]
    assert stat.S_IMODE(second._memory_path.stat().st_mode) == 0o600
