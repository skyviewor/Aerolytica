"""Sync scope changes are local-only, revisioned, bounded and individually approved."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aero.agent.loop import AgentLoop
from aero.application.approval_details import approval_payload
from aero.core.cloud_sync import ALLOWED_ROOTS, CloudSyncEngine, CloudSyncError
from aero.core.config import AeroConfig
from aero.core.types import ToolCall
from aero.data.modes import is_tool_allowed
from aero.toolbox.paths import use_workspace
from aero.toolbox.registry import get_registry
from aero.toolbox.tools import cloud_sync


@pytest.fixture
def session(monkeypatch):
    session = SimpleNamespace(
        data=SimpleNamespace(user_id="u"),
        base_url="https://relay.test",
        access_token=AsyncMock(),
        close=AsyncMock(),
        request=AsyncMock(side_effect=AssertionError("no network permitted")),
    )
    monkeypatch.setattr(cloud_sync, "OfficialAccountSession", lambda: session)
    return session


def bound_engine(root, session, paused=True):
    engine = CloudSyncEngine(root, session)
    with engine._locked():
        engine._state["binding"] = {
            "user_id": "u",
            "project_id": "p",
            "root_directory_id": None,
            "local_root": str(root),
            "base_url": session.base_url,
            "includes": ["papers", "scripts"],
        }
        engine._paused = paused
        engine._save()
    return engine


async def test_read_is_nonmutating_offline_and_reports_hard_exclusions(tmp_path):
    with use_workspace(tmp_path):
        result = await cloud_sync.get_local_sync_directories()
    assert result["status"] == "success"
    scope = result["result"]
    assert scope["includes"] == sorted(ALLOWED_ROOTS) and not scope["bound"]
    assert "data" in scope["excluded_components"] and "tmp" in scope["excluded_components"]
    assert not (tmp_path / ".aero").exists()


@pytest.mark.parametrize(
    "path",
    [
        "data",
        "tmp",
        "scripts/tmp",
        ".git",
        "../outside",
        "/absolute",
        "credentials",
        "reports/child",
        "*.txt",
        "a.key",
    ],
)
def test_non_overridable_restrictions(tmp_path, path):
    revision = CloudSyncEngine.inspect_scope(tmp_path)["revision"]
    with pytest.raises(CloudSyncError):
        CloudSyncEngine.preview_scope_at(tmp_path, [path], revision)
    assert not (tmp_path / ".aero").exists()


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_scope_cannot_target_links_or_files(tmp_path, kind):
    if kind == "symlink":
        (tmp_path / "reports").symlink_to(tmp_path, target_is_directory=True)
    else:
        (tmp_path / "reports").write_text("keep")
    scope = CloudSyncEngine.inspect_scope(tmp_path)
    with pytest.raises(CloudSyncError):
        CloudSyncEngine.preview_scope_at(tmp_path, ["reports"], scope["revision"])


async def test_update_preserves_binding_pause_and_files_but_resets_out_of_scope_tracking(
    tmp_path,
    session,
):
    engine = bound_engine(tmp_path, session)
    (tmp_path / "scripts").mkdir()
    target = tmp_path / "scripts/keep.py"
    target.write_text("keep")
    with engine._locked():
        engine._state["baseline"] = {"scripts/keep.py": {}, "papers/keep.md": {}}
        engine._state["conflicts"] = {"scripts/keep.py": {}, "papers/keep.md": {}}
        engine._state["remote_entities"] = {"file:x": {}}
        engine._state["remote_cursor"] = "old-cursor"
        engine._save()
    old = engine.inspect_scope(tmp_path)
    preview = engine.preview_scope_at(tmp_path, ["papers", "figures/**"], old["revision"])
    assert preview["added"] == ["figures"] and preview["removed"] == ["scripts"]
    result = await engine.update_scope(preview["includes"], old["revision"])
    assert result["includes"] == ["figures", "papers"] and result["paused"]
    assert result["project_id"] == "p" and result["directory_id"] is None
    assert result["revision"] != old["revision"]
    assert target.read_text() == "keep"
    assert set(engine._state["baseline"]) == {"papers/keep.md"}
    assert set(engine._state["conflicts"]) == {"papers/keep.md"}
    assert not engine._state["remote_entities"] and engine._state["remote_cursor"] is None
    session.request.assert_not_awaited()


@pytest.mark.parametrize("problem", ["revision", "account", "pending", "lock"])
async def test_conflicts_do_not_change_configuration(tmp_path, session, problem):
    engine = bound_engine(tmp_path, session)
    scope = engine.inspect_scope(tmp_path)
    if problem == "pending":
        with engine._locked():
            engine._state["pending"] = {"papers/a": {"stage": "upload"}}
            engine._save()
    if problem == "account":
        session.data.user_id = "other"
    revision = "stale" if problem == "revision" else scope["revision"]
    path = tmp_path / ".aero/cloud-sync/state.json"
    before = path.read_bytes()
    if problem == "lock":
        other = CloudSyncEngine(tmp_path, session)
        with other._locked(), pytest.raises(CloudSyncError):
            await engine.update_scope(["figures"], revision)
    else:
        with pytest.raises(CloudSyncError):
            await engine.update_scope(["figures"], revision)
    assert path.read_bytes() == before


async def test_running_engine_reloads_approved_scope_and_pause(tmp_path, session):
    running = bound_engine(tmp_path, session, paused=True)
    other = CloudSyncEngine(tmp_path, session)
    scope = other.inspect_scope(tmp_path)
    await other.update_scope(["figures"], scope["revision"])
    # Simulate a stale in-memory process; its next lock must reload both settings.
    running._paused = False
    with running._locked():
        assert running._state["binding"]["includes"] == ["figures"]
        assert running._paused
    running.pause(False)
    assert not running.inspect_scope(tmp_path)["paused"]


async def test_unbound_choice_becomes_default_on_later_explicit_bind(tmp_path, session):
    engine = CloudSyncEngine(tmp_path, session)
    scope = engine.inspect_scope(tmp_path)
    result = await engine.update_scope(["figures"], scope["revision"])
    assert not result["bound"]
    engine._api = AsyncMock(return_value={"version": 2})
    engine._remote = AsyncMock(return_value={})
    await engine.bind("u", "p")
    assert engine.status()["includes"] == ["figures"]


async def test_approved_scope_controls_scanner_without_expanding_hard_exclusions(tmp_path, session):
    engine = bound_engine(tmp_path, session)
    for directory in ("figures", "papers", "figures/tmp"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    (tmp_path / "figures/plot.png").write_bytes(b"image")
    (tmp_path / "figures/tmp/private.png").write_bytes(b"excluded")
    (tmp_path / "papers/keep.md").write_text("out of scope")
    scope = engine.inspect_scope(tmp_path)
    await engine.update_scope(["figures"], scope["revision"])
    assert set(engine._local()) == {"figures", "figures/plot.png"}
    session.request.assert_not_awaited()


@pytest.mark.parametrize("choice", ["deny", "allow", "always", "unknown"])
async def test_agent_scope_change_always_requires_approval(tmp_path, session, choice):
    import aero.toolbox.builtin_tools  # noqa: F401

    cfg = AeroConfig.create_default()
    cfg.llm.api_key = "test-only-key"
    loop = AgentLoop(cfg)
    loop.always_allow.add("set_local_sync_directories")
    count = 0
    with use_workspace(tmp_path):
        scope = CloudSyncEngine.inspect_scope(tmp_path)
        call = ToolCall(
            id="scope",
            name="set_local_sync_directories",
            arguments={"includes": ["figures"], "expected_revision": scope["revision"]},
        )
        async for event in loop._execute_one_tool_stream(call):
            if event.type == "confirm":
                count += 1
                args = json.loads(event.content)["args"]
                assert args["added"] == ["figures"]
                assert args["removed"] == sorted(ALLOWED_ROOTS)
                assert args["previous_includes"] == scope["includes"]
                loop.confirm_future.set_result(choice)
    await loop.llm.close()
    assert count == 1
    result = CloudSyncEngine.inspect_scope(tmp_path)
    if choice in {"allow", "always"}:
        assert result["includes"] == ["figures"] and not result["bound"]
    else:
        assert not (tmp_path / ".aero").exists()
    session.request.assert_not_awaited()


async def test_approval_race_blocks_change(tmp_path, session):
    import aero.toolbox.builtin_tools  # noqa: F401

    cfg = AeroConfig.create_default()
    cfg.llm.api_key = "test-only-key"
    loop = AgentLoop(cfg)
    with use_workspace(tmp_path):
        scope = CloudSyncEngine.inspect_scope(tmp_path)
        call = ToolCall(
            id="scope",
            name="set_local_sync_directories",
            arguments={
                "includes": ["figures"],
                "expected_revision": scope["revision"],
            },
        )
        async for event in loop._execute_one_tool_stream(call):
            if event.type == "confirm":
                await CloudSyncEngine(tmp_path, session).update_scope(
                    ["reports"], scope["revision"]
                )
                loop.confirm_future.set_result("allow")
    await loop.llm.close()
    assert CloudSyncEngine.inspect_scope(tmp_path)["includes"] == ["reports"]


def test_tools_modes_and_ui_details(tmp_path):
    from aero.cli.main import AeroApp

    read = get_registry().get("get_local_sync_directories")
    write = get_registry().get("set_local_sync_directories")
    assert read and not read.requires_confirmation and write.requires_confirmation
    for mode in ("plan", "qa"):
        assert is_tool_allowed(read.name, mode) and not is_tool_allowed(write.name, mode)
    scope = CloudSyncEngine.inspect_scope(tmp_path)
    args = CloudSyncEngine.preview_scope_at(tmp_path, ["figures"], scope["revision"])
    payload = approval_payload({"tool": write.name, "args": args})
    assert "新增目录" in str(payload) and "移除目录" in str(payload)
    assert "不启用或立即运行" in payload["description"]
    text = AeroApp._build_confirm_message(
        SimpleNamespace(config=SimpleNamespace(language="zh")),
        write.name,
        args,
    )
    assert "figures" in text and "原同步目录" in text
