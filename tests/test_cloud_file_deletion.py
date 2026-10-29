"""Cloud deletion is bounded, versioned, verified and always explicitly approved."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from aero.agent.loop import AgentLoop
from aero.application.approval_details import approval_payload
from aero.core.cloud_files import CloudFiles
from aero.core.config import AeroConfig
from aero.core.types import ToolCall
from aero.data.modes import is_tool_allowed
from aero.toolbox.tools import cloud


@pytest.fixture
def wire(monkeypatch):
    manifest = [
        {
            "file_id": "f1",
            "relative_path": "会话附件/agent_x/figure.png",
            "expected_version": 3,
            "size_bytes": 120,
        }
    ]
    state = SimpleNamespace(version=3, failed_id=None, transport_fail=False)
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            assert request.url.path == "/v1/storage/v2/snapshot"
            assert request.url.params["project_id"] == "p"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "entity_type": "file",
                            "data": {
                                "file_id": item["file_id"],
                                "relative_path": item["relative_path"],
                                "size_bytes": item["size_bytes"],
                                "version": state.version,
                                "project_id": "p",
                                "status": "active",
                            },
                        }
                        for item in manifest
                    ],
                    "has_more": False,
                },
            )
        assert request.method == "DELETE"
        assert request.url.params["expected_version"] == "3"
        if state.transport_fail:
            raise httpx.ReadTimeout("private signed url")
        if request.url.path.endswith(state.failed_id or "never"):
            return httpx.Response(409)
        return httpx.Response(200, json={"status": "deleted"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = SimpleNamespace(
        access_token=AsyncMock(return_value="test-only-token"),
        close=AsyncMock(),
        request=lambda method, path, **kw: http.request(method, "https://relay.test" + path, **kw),
    )
    monkeypatch.setattr(cloud, "OfficialAccountSession", lambda: session)
    return manifest, state, calls, http, CloudFiles(session)


async def test_delete_exact_manifest_and_version_no_directory(wire):
    manifest, _, calls, http, files = wire
    async with http:
        result = await files.delete("p", manifest)
    assert result["status"] == "success" and result["deleted"] == manifest
    deletes = [r for r in calls if r.method == "DELETE"]
    assert [r.url.path for r in deletes] == ["/v1/storage/v2/files/f1"]


@pytest.mark.parametrize(
    "change",
    [
        {"relative_path": "other.png"},
        {"expected_version": 2},
        {"size_bytes": 121},
        {"file_id": "missing"},
        {"relative_path": "*"},
        {"expected_version": True},
    ],
)
async def test_forged_or_stale_manifest_cannot_delete(wire, change):
    manifest, _, calls, http, files = wire
    async with http:
        with pytest.raises(ValueError):
            await files.delete("p", [{**manifest[0], **change}])
    assert not [r for r in calls if r.method == "DELETE"]


@pytest.mark.parametrize("count", [0, 6])
async def test_empty_or_unbounded_selection_is_rejected(wire, count):
    manifest, _, calls, http, files = wire
    async with http:
        with pytest.raises(ValueError):
            await files.delete("p", manifest * count)
    assert not calls


@pytest.mark.parametrize("failure", ["conflict", "transport"])
async def test_partial_failure_stops_without_retry(wire, failure):
    manifest, state, calls, http, files = wire
    manifest.extend([{**manifest[0], "file_id": "f2"}, {**manifest[0], "file_id": "f3"}])
    state.failed_id = "f2"
    state.transport_fail = failure == "transport"
    async with http:
        result = await files.delete("p", manifest)
    deletes = [r for r in calls if r.method == "DELETE"]
    assert len(deletes) == (1 if state.transport_fail else 2)
    assert result["status"] == ("error" if state.transport_fail else "partial")
    assert result["not_attempted"] and "private" not in str(result)


@pytest.mark.parametrize("choice", ["deny", "allow", "always", "unexpected"])
async def test_stream_approval_cannot_be_bypassed_by_session_allowlist(wire, choice):
    import aero.toolbox.builtin_tools  # noqa: F401

    manifest, _, calls, http, _ = wire
    config = AeroConfig.create_default()
    config.llm.api_key = "test-key"
    loop = AgentLoop(config)
    loop.always_allow.add("delete_cloud_files")
    count = 0
    async with http:
        for index in range(2):
            call = ToolCall(
                id=f"c{index}",
                name="delete_cloud_files",
                arguments={"project_id": "p", "files": manifest},
            )
            async for event in loop._execute_one_tool_stream(call):
                if event.type == "confirm":
                    payload = json.loads(event.content)
                    assert payload["args"]["files"] == manifest
                    count += 1
                    loop.confirm_future.set_result(choice)
    await loop.llm.close()
    assert count == 2
    assert len([r for r in calls if r.method == "DELETE"]) == (
        2 if choice in {"allow", "always"} else 0
    )


async def test_change_while_waiting_for_approval_blocks_execution(wire):
    import aero.toolbox.builtin_tools  # noqa: F401

    manifest, state, calls, http, _ = wire
    config = AeroConfig.create_default()
    config.llm.api_key = "test-key"
    loop = AgentLoop(config)
    async with http:
        call = ToolCall(
            id="c", name="delete_cloud_files", arguments={"project_id": "p", "files": manifest}
        )
        async for event in loop._execute_one_tool_stream(call):
            if event.type == "confirm":
                state.version = 4
                loop.confirm_future.set_result("allow")
    await loop.llm.close()
    assert not [r for r in calls if r.method == "DELETE"]


def test_modes_and_approval_message_show_exact_cloud_targets():
    from rich.text import Text

    from aero.cli.main import AeroApp

    args = {
        "project_id": "p",
        "files": [
            {
                "file_id": "f1",
                "relative_path": "dir/figure.png",
                "expected_version": 3,
                "size_bytes": 120,
            }
        ],
    }
    payload = approval_payload({"tool": "delete_cloud_files", "args": args})
    assert "1 个云端文件" in payload["title"]
    assert not payload["details_truncated"]
    message = AeroApp._build_confirm_message(
        SimpleNamespace(config=SimpleNamespace(language="zh")),
        "delete_cloud_files",
        args,
    )
    assert "dir/figure.png" in message and "f1" in message and "120" in message
    assert "后续删除仍须重新审批" in message and "旧对话" in message
    args["files"][0]["relative_path"] = "dir/[red]figure.png[/red]"
    literal = AeroApp._build_confirm_message(
        SimpleNamespace(config=SimpleNamespace(language="zh")), "delete_cloud_files", args,
    )
    assert args["files"][0]["relative_path"] in Text.from_markup(literal).plain
    for mode in ("qa", "plan"):
        assert not is_tool_allowed("delete_cloud_files", mode)


@pytest.mark.parametrize("choice", ["deny", "allow"])
async def test_nonstream_requires_approval_and_displays_all_targets(wire, choice, capsys):
    import aero.toolbox.builtin_tools  # noqa: F401

    manifest, _, calls, http, _ = wire
    config = AeroConfig.create_default()
    config.llm.api_key = "test-key"
    loop = AgentLoop(config)
    loop.always_allow.add("delete_cloud_files")
    loop._read_terminal_confirmation = AsyncMock(return_value=choice)
    loop.llm.chat = AsyncMock(return_value="done")
    async with http:
        await loop._handle_tool_calls(
            [
                ToolCall(
                    id="c",
                    name="delete_cloud_files",
                    arguments={"project_id": "p", "files": manifest},
                )
            ],
            "",
        )
    await loop.llm.close()
    text = capsys.readouterr().out
    assert manifest[0]["relative_path"] in text and "120" in text
    assert "一直允许" not in text
    assert bool([r for r in calls if r.method == "DELETE"]) == (choice == "allow")


async def test_same_response_multiple_deletion_calls_approved_separately(wire):
    import aero.toolbox.builtin_tools  # noqa: F401

    manifest, _, calls, http, _ = wire
    config = AeroConfig.create_default()
    config.llm.api_key = "test-key"
    loop = AgentLoop(config)
    tool_calls = [
        ToolCall(
            id=f"c{i}", name="delete_cloud_files", arguments={"project_id": "p", "files": manifest}
        )
        for i in range(2)
    ]
    count = 0
    async with http:
        async for event in loop._execute_tool_calls_stream(tool_calls):
            if event.type == "confirm":
                count += 1
                loop.confirm_future.set_result("deny")
    await loop.llm.close()
    assert count == 2 and not [r for r in calls if r.method == "DELETE"]
