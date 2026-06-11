"""Tests for user instructions data layer and system prompt injection."""

import tempfile
from pathlib import Path

import pytest

from meteora.data.instructions import (
    GLOBAL_INSTRUCTIONS_PATH,
    append_instruction,
    clear_instructions,
    load_instructions,
    write_instructions,
    _render_section,
    _resolve_scope_path,
)
from meteora.agent.system_prompt import (
    _instruction_section,
    build_system_prompt,
)


class TestRenderSection:
    def test_empty_returns_empty(self):
        assert _render_section("", "") == ""

    def test_global_only(self):
        result = _render_section("- use celsius", "")
        assert "全局偏好" in result
        assert "use celsius" in result
        assert "项目要求" not in result

    def test_both(self):
        result = _render_section("- use celsius", "- era5 only")
        assert "全局偏好" in result
        assert "项目要求" in result

    def test_truncates_long_content(self):
        long_text = "x" * 3000
        result = _render_section(long_text, "")
        assert len(result) < 3000


class TestAppendInstruction:
    def test_appends_to_new_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = append_instruction("use celsius", scope="project", project_dir=tmpdir)
            content = path.read_text()
            assert "- use celsius" in content

    def test_appends_multiple(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            append_instruction("use celsius", scope="project", project_dir=tmpdir)
            append_instruction("prefer viridis", scope="project", project_dir=tmpdir)
            content = load_instructions(project_dir=tmpdir)
            assert "use celsius" in content
            assert "prefer viridis" in content

    def test_appends_to_global(self):
        append_instruction("always use celsius", scope="global")
        content = load_instructions()
        assert "always use celsius" in content
        clear_instructions(scope="global")

    def test_raises_on_empty_instruction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError):
                append_instruction("", scope="project", project_dir=tmpdir)
            with pytest.raises(ValueError):
                append_instruction("   ", scope="project", project_dir=tmpdir)


class TestClearInstructions:
    def test_clears_project_instructions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            append_instruction("test instruction", scope="project", project_dir=tmpdir)
            op = clear_instructions(scope="project", project_dir=tmpdir)
            assert not op.exists()

    def test_clear_nonexistent_is_noop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            op = clear_instructions(scope="project", project_dir=tmpdir)


class TestResolveScopePath:
    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError, match="unknown scope"):
            _resolve_scope_path("invalid")

    def test_global_path(self):
        path = _resolve_scope_path("global")
        assert path == GLOBAL_INSTRUCTIONS_PATH


class TestInstructionSection:
    def test_empty_returns_empty(self):
        assert _instruction_section("", "zh") == ""
        assert _instruction_section("", "en") == ""

    def test_zh_includes_header(self):
        result = _instruction_section("- test instruction", "zh")
        assert "用户指令" in result
        assert "test instruction" in result

    def test_en_includes_header(self):
        result = _instruction_section("- test instruction", "en")
        assert "User Instructions" in result
        assert "test instruction" in result


class TestBuildSystemPromptWithInstructions:
    def test_includes_instructions_when_provided(self):
        from meteora.core.config import MeteoraConfig

        config = MeteoraConfig()
        result = build_system_prompt(
            config, "zh", instructions_context="### 全局偏好\n- test",
        )
        assert "用户指令" in result
        assert "test" in result

    def test_excludes_instruction_section_when_empty(self):
        from meteora.core.config import MeteoraConfig

        config = MeteoraConfig()
        result = build_system_prompt(config, "zh", instructions_context="")
        header_occurrences = result.count("## 用户指令")
        assert header_occurrences <= 2  # may appear in behavioral rules
