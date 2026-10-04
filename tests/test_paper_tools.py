"""Integration checks for paper-version tools and slash commands."""

import pytest

from aero.cli.main import AeroApp
from aero.data.modes import is_tool_allowed
from aero.toolbox.paths import use_workspace
from aero.toolbox.registry import get_registry


@pytest.mark.asyncio
async def test_paper_tools_share_project_local_history(tmp_path):
    registry = get_registry()

    with use_workspace(tmp_path):
        initialized = await registry.get("initialize_paper_versioning").function()
        paper = tmp_path / "paper" / "main.md"
        paper.write_text("# Paper\n\nRevised result.\n", encoding="utf-8")
        saved = await registry.get("save_paper_version").function(title="Revise result")
        versions = await registry.get("list_paper_versions").function()
        difference = await registry.get("diff_paper_version").function(
            version_id=initialized["head"]
        )

    assert initialized["success"] is True
    assert saved["version"]["title"] == "Revise result"
    assert versions["count"] == 2
    assert difference["changed"] is True
    assert "+Revised result." in difference["unified_diff"]
    assert registry.get("restore_paper_version").requires_confirmation is True


def test_paper_write_tools_are_execute_only():
    for tool_name in (
        "initialize_paper_versioning",
        "save_paper_version",
        "restore_paper_version",
    ):
        assert is_tool_allowed(tool_name, "execute") is True
        assert is_tool_allowed(tool_name, "plan") is False
        assert is_tool_allowed(tool_name, "qa") is False

    for tool_name in (
        "paper_version_status",
        "list_paper_versions",
        "diff_paper_version",
    ):
        assert is_tool_allowed(tool_name, "plan") is True
        assert is_tool_allowed(tool_name, "qa") is True


@pytest.mark.asyncio
async def test_paper_slash_commands_initialize_save_list_and_diff(tmp_path):
    app = AeroApp.__new__(AeroApp)
    app._project_dir = tmp_path
    shown: list[tuple[str, dict]] = []
    app._show_checkpoint_message = lambda message, **kwargs: shown.append((message, kwargs))

    await app._handle_paper_command("/paper init")
    paper = tmp_path / "paper" / "main.md"
    paper.write_text("# Paper\n\nRevised result.\n", encoding="utf-8")
    await app._handle_paper_command("/paper save 修改结果段")
    await app._handle_paper_command("/paper versions")
    await app._handle_paper_command("/paper diff")

    rendered = "\n".join(message for message, _ in shown)
    assert "论文版本管理已启用" in rendered
    assert "修改结果段" in rendered
    assert "论文版本历史" in rendered
    assert "论文正文与所选版本一致" in rendered
    assert all(options == {"force_scroll": True} for _, options in shown)
