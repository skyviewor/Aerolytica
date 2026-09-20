"""Tests for rendering Relay-owned official model picker rows."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from aero.cli.main import _official_model_options
from aero.core.official_models import OfficialModel


def test_official_model_picker_uses_server_metadata():
    options = _official_model_options(
        (
            OfficialModel(
                id="glm-5.2",
                display_name="GLM 5.2",
                description="高质量文本模型",
                capabilities=("text", "tools"),
                recommended=True,
            ),
        )
    )

    assert len(options) == 1
    value, prompt = options[0]
    assert value == "glm-5.2"
    output = StringIO()
    Console(file=output, width=100).print(prompt)
    rendered = output.getvalue()
    assert "GLM 5.2" in rendered
    assert "高质量文本模型" in rendered
    assert "能力：text、tools" in rendered
    assert "推荐" in rendered
