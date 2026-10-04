import pytest

from aero.core.remote_agent import RemoteAgentError, RemoteAgentRegistry


@pytest.mark.parametrize("name", ["mytest2", "ocean_data2", "a_2", "agent123"])
def test_digit_names_are_registered_and_resolved(tmp_path, monkeypatch, name):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    registry = RemoteAgentRegistry()
    assert registry.ensure_name_available(name) == name
    agent = registry.add(
        agent_id="isolated_agent",
        name=name,
        token="isolated_token",
        description="",
    )
    assert registry.resolve(name) == agent
    with pytest.raises(RemoteAgentError, match="已存在"):
        registry.ensure_name_available(name)


@pytest.mark.parametrize("name", ["2agent", "Agent2", "agent-2", "agent 2", "a__2", "a_"])
def test_invalid_names_are_still_rejected(tmp_path, monkeypatch, name):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    with pytest.raises(RemoteAgentError, match="小写字母"):
        RemoteAgentRegistry().ensure_name_available(name)
