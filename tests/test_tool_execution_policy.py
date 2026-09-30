import asyncio
from types import SimpleNamespace

import pytest

from aero.agent.runtime import Runtime
from aero.agent.search_budget import RESEARCH, SearchBudget
from aero.core.config import AeroConfig, resolved_vision_config, vision_is_configured
from aero.toolbox.paths import use_workspace
from aero.toolbox.tools.files import edit_file, read_file, write_file
from aero.toolbox.tools.runtime import _has_multiple_subplots, _search_has_no_matches


async def test_file_reads_survive_async_tasks_and_path_aliases(tmp_path):
    path = tmp_path / "script.py"
    path.write_text("one")
    runtime = Runtime()
    with use_workspace(tmp_path):
        await asyncio.create_task(runtime.execute(read_file, {"file_path": "script.py"}))
        result = await asyncio.create_task(
            runtime.execute(
                edit_file, {"file_path": str(path), "old_string": "one", "new_string": "two"}
            )
        )
        assert result.result["status"] == "success"
        other = await Runtime().execute(
            edit_file, {"file_path": "script.py", "old_string": "two", "new_string": "three"}
        )
        assert other.result["status"] == "error"
        runtime.read_files.clear()
        denied = await runtime.execute(
            edit_file, {"file_path": "script.py", "old_string": "two", "new_string": "three"}
        )
        assert denied.result["status"] == "error"


async def test_only_successful_text_reads_and_writes_authorize_edit(tmp_path):
    runtime = Runtime()
    with use_workspace(tmp_path):
        await runtime.execute(read_file, {"file_path": "missing.py"})
        await runtime.execute(read_file, {"file_path": str(tmp_path)})
        binary = tmp_path / "binary"
        binary.write_bytes(b"\xff")
        await runtime.execute(read_file, {"file_path": "binary"})
        assert not runtime.read_files
        await runtime.execute(write_file, {"file_path": "new.py", "content": "one"})
        result = await runtime.execute(
            edit_file, {"file_path": "new.py", "old_string": "one", "new_string": "two"}
        )
        assert result.result["status"] == "success"


@pytest.mark.parametrize(
    "source,expected",
    [
        ("fig, ax = plt.subplots(figsize=(12, 8))", False),
        ("fig, ax = plt.subplots(1, 1, figsize=(12,8))", False),
        ("fig, axes = plt.subplots(2, 3)", True),
        ("fig, axes = plt.subplots(nrows=2, ncols=1)", True),
        ("n = 2\nfig, axes = plt.subplots(n, 1)", True),
        ("fig, axes = plt.subplots(dynamic, 3)", False),
        ("n = 2\nn = dynamic\nfig, axes = plt.subplots(n, 1)", False),
    ],
)
def test_subplot_classification(source, expected):
    assert _has_multiple_subplots(source) is expected


def test_no_matches_is_not_a_command_failure():
    result = SimpleNamespace(exit_code=1, stdout="", stderr="", error="")
    assert _search_has_no_matches('grep -rn "text" . | head -50', result)
    assert _search_has_no_matches('rg "text" .', result)
    assert not _search_has_no_matches("python script.py", result)
    assert not _search_has_no_matches("grep text . && touch file", result)
    result.stderr = "permission denied"
    assert not _search_has_no_matches("grep text .", result)


async def test_search_budget_caches_unknown_and_blocks_batches():
    budget = SearchBudget(2)
    calls = []

    async def execute():
        calls.append(1)
        return {"status": "unknown", "content": "unknown"}

    first = await budget.execute({"query": "  latest  typhoon "}, "first", execute)
    repeated = await budget.execute({"query": "latest typhoon"}, "different-id", execute)
    assert repeated["search_reused"] is True
    assert repeated["search_call_id"] == first["search_call_id"] == "first"
    outputs = await asyncio.gather(
        *[budget.execute({"query": str(i)}, str(i), execute) for i in range(5)]
    )
    assert len(calls) == 2
    assert sum(row.get("status") == "blocked" for row in outputs) == 4
    assert budget.remaining == 0
    assert RESEARCH.search("全面调研近期飓风")
    assert not RESEARCH.search("最新的台风叫什么")


async def test_search_options_are_part_of_deduplication_key():
    budget = SearchBudget(8)
    calls = []

    async def execute():
        calls.append(1)
        return {"status": "completed", "results": []}

    for domains in (["a.test"], ["b.test"], ["a.test"]):
        await budget.execute({"query": "weather", "domains": domains}, "id", execute)
    assert len(calls) == 2


@pytest.mark.parametrize("provider", ["official", "deepseek"])
def test_logged_in_unconfigured_vision_uses_relay(provider, monkeypatch):
    monkeypatch.setattr(
        "aero.core.official_account.load_official_session",
        lambda: SimpleNamespace(is_logged_in=True),
    )
    config = AeroConfig()
    config.llm.provider = provider
    assert vision_is_configured(config)
    resolved = resolved_vision_config(config)
    assert resolved.mode == resolved.provider == "official"
    assert resolved.model == "auto"


def test_explicit_vision_is_preserved_and_logout_disables_default(monkeypatch):
    monkeypatch.setattr(
        "aero.core.official_account.load_official_session",
        lambda: SimpleNamespace(is_logged_in=True),
    )
    config = AeroConfig()
    config.vision.mode = "separate"
    config.vision.api_key = "test-key"
    assert resolved_vision_config(config).provider == "bailian"
    config.vision.mode = "official"
    config.vision.model = "specific"
    assert resolved_vision_config(config).model == "specific"
    config.vision.mode = "unconfigured"
    config.vision.api_key = ""
    monkeypatch.setattr(
        "aero.core.official_account.load_official_session",
        lambda: SimpleNamespace(is_logged_in=False),
    )
    assert not vision_is_configured(config)
    assert resolved_vision_config(config) is None


@pytest.mark.parametrize("prompt,limit", [("最新台风叫什么", 2), ("调研并比较台风", 8)])
async def test_streaming_search_batch_enforces_limit_and_removes_tool(prompt, limit):
    import json

    from aero.agent.llm_client import StreamEvent
    from aero.agent.loop import AgentLoop
    from aero.core.types import ToolCall

    config = AeroConfig()
    config.llm.provider = "official"
    config.web_search.enabled = True
    agent = AgentLoop(config)
    tool = {"type": "function", "function": {"name": "web_search", "parameters": {}}}
    calls = []
    offered = []

    class Mcp:
        async def list_tools(self):
            return [tool]

        async def call(self, name, args, **kwargs):
            calls.append(kwargs["call_id"])
            return {
                "status": "success",
                "source": "bailian",
                "credits": 44,
                "results": [
                    {"title": "Weather", "url": "https://example.com", "summary": "Evidence"}
                ],
                "content": "Evidence",
            }

        async def close(self):
            pass

    async def model(messages, tools):
        offered.append(tools)
        if len(offered) == 1:
            for i in range(10):
                yield StreamEvent(
                    type="tool_call",
                    tool_call=ToolCall(id=str(i), name="web_search", arguments={"query": str(i)}),
                )
        else:
            yield StreamEvent(type="text", content="Answer")
        yield StreamEvent(type="done")

    agent._official_mcp = Mcp()
    agent.llm.chat_with_tools_stream = model
    try:
        _ = [event async for event in agent.run_stream(prompt)]
        assert len(calls) == limit
        assert all(t["function"]["name"] != "web_search" for t in offered[1])
        payload = json.loads(next(m.content for m in agent.messages if m.role == "tool"))
        assert payload["source"] == "bailian"
        assert payload["results"][0]["summary"] == "Evidence"
        agent.runtime.read_files.add("retained")
        agent._start_search_turn("继续")
        assert "retained" in agent.runtime.read_files
        assert agent.search_budget.remaining == 2
        agent.messages = agent.messages[:1]
        agent._start_search_turn("新对话")
        assert not agent.runtime.read_files
    finally:
        await agent.close()


@pytest.mark.parametrize(
    "status,error_type",
    [
        (200, None),
        (401, "authentication"),
        (402, "insufficient_credits"),
        (503, "route_unavailable"),
    ],
)
async def test_default_official_vision_request_and_errors(
    tmp_path, monkeypatch, status, error_type
):
    import httpx

    from aero.agent.official_vision_client import OfficialVisionClient
    from aero.agent.vision_client import VisionAnalysisError

    monkeypatch.setattr(
        "aero.core.official_account.load_official_session",
        lambda: SimpleNamespace(is_logged_in=True),
    )
    received = []

    class Account:
        async def request_to(self, base, method, path, **kwargs):
            received.append(kwargs["json"])
            return httpx.Response(
                status,
                json={
                    "error": {"message": "Details"},
                    "choices": [{"message": {"content": "Image analysis"}}],
                    "usage": {"prompt_tokens": 10},
                },
            )

        async def close(self):
            pass

    monkeypatch.setattr("aero.agent.official_vision_client.OfficialAccountSession", Account)
    image = tmp_path / "image.png"
    image.write_bytes(b"mock image")
    config = AeroConfig()
    config.llm.provider = "deepseek"
    client = OfficialVisionClient(resolved_vision_config(config))
    if error_type:
        with pytest.raises(VisionAnalysisError) as raised:
            await client.analyze([str(image)], "Inspect")
        assert raised.value.error_type == error_type
    else:
        assert await client.analyze([str(image)], "Inspect") == "Image analysis"
    assert received[0]["model"] == "auto"
    assert received[0]["messages"][0]["content"][1]["type"] == "image_url"
    await client.close()


def test_saved_official_vision_mode_loads(monkeypatch):
    from aero.core.config import apply_user_secrets

    monkeypatch.setattr(
        "aero.core.config.load_user_secrets",
        lambda: {"vision": {"mode": "official", "provider": "official", "model": "auto"}},
    )
    config = apply_user_secrets(AeroConfig())
    assert config.vision.mode == "official"
