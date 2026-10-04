"""Main and tool vision routing have independent acceptance assertions."""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.test_acceptance_login import load_script


def test_fresh_process_initializes_only_real_read_and_vision_tools():
    module = load_script()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.util
import sys
spec = importlib.util.spec_from_file_location('acceptance', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
registry = module.restricted_registry()
assert sorted(tool.name for tool in registry.list_all()) == ['analyze_image', 'read_file']
from aero.toolbox.tools.files import read_file
from aero.toolbox.tools.vision import analyze_image
assert registry.get('read_file').function is read_file
assert registry.get('analyze_image').function is analyze_image
assert len(registry.list_functions()) == 2
""",
            module.__file__,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_failure_codes_never_echo_arbitrary_exception_text():
    module = load_script()
    assert module.safe_failure_code(RuntimeError("JWT secret response")) == "unexpected_exception"
    assert (
        module.safe_failure_code(module.AcceptanceError("context_file_marker_missing"))
        == "context_file_marker_missing"
    )


@pytest.mark.asyncio
async def test_linger_keeps_same_runtime_and_observes_until_deadline(monkeypatch):
    module = load_script()
    elapsed = [0]
    runtime = SimpleNamespace(_task=Mock())
    runtime._task.done.return_value = False
    observe = AsyncMock()

    async def sleep(seconds):
        elapsed[0] += seconds

    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    await module.linger_runtime(runtime, 3, observe)
    assert elapsed[0] == 3
    assert observe.await_count == 4


@pytest.mark.asyncio
async def test_linger_reports_stopped_runtime():
    module = load_script()
    runtime = SimpleNamespace(_task=Mock())
    runtime._task.done.return_value = True
    with pytest.raises(RuntimeError, match="stopped"):
        await module.linger_runtime(runtime, 1, AsyncMock())


def row(purpose, model="selected", mode="execute"):
    return {
        "turn_id": "command",
        "header_turn_id": "command",
        "idempotency_key": "key-" + purpose,
        "purpose": purpose,
        "model": model,
        "mode": mode,
        "expected_vision_model": "account-vision",
    }


def test_vision_can_differ_from_main_and_auxiliary_models_are_excluded():
    module = load_script()
    module.validate_command_wire(
        [
            row("main"),
            row("vision", "account-vision"),
            row("title", "auto"),
            row("compact", "auto"),
        ],
        "command",
        "selected",
        "execute",
        require_vision=True,
    )


@pytest.mark.parametrize(
    "rows,require_vision",
    [
        ([row("vision", "account-vision")], False),
        ([row("main", "wrong")], False),
        ([row("main", mode="plan")], False),
        ([row("main"), row("vision", "selected")], True),
        ([row("main")], True),
        ([row("main"), {**row("vision", "account-vision"), "header_turn_id": "wrong"}], True),
    ],
)
def test_wire_rejects_missing_main_or_incorrect_routing(rows, require_vision):
    with pytest.raises(RuntimeError):
        load_script().validate_command_wire(
            rows, "command", "selected", "execute", require_vision=require_vision
        )
