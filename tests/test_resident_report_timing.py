from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aero.application.cloud_runtime import RESIDENT_REPORT_PROMPT, CloudAgentRuntime
from aero.application.request_timing import RequestTimer, start_execution_timer, timed_request


def worker(messages):
    runtime = CloudAgentRuntime.__new__(CloudAgentRuntime)
    runtime.session = SimpleNamespace(
        id="report-session",
        session_manager=SimpleNamespace(export_portable_context=Mock(return_value=None)),
        messages_view=Mock(return_value=messages),
        start_run=Mock(return_value="run-report"),
        wait_run=AsyncMock(),
        run_status=Mock(return_value={"state": "completed"}),
        cancel=AsyncMock(),
    )
    runtime._flush_conversation_events = AsyncMock()
    runtime.transport = SimpleNamespace(request=AsyncMock())
    runtime._poll = AsyncMock()
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [[], [{"role": "system", "content": "initial config"}]])
async def test_empty_resident_reports_through_real_run_entry(history):
    runtime = worker(history)
    await runtime._report_and_poll()
    runtime.session.start_run.assert_called_once_with(RESIDENT_REPORT_PROMPT, origin="client")
    runtime.session.wait_run.assert_awaited_once_with("run-report")
    runtime._flush_conversation_events.assert_awaited_once()
    runtime._poll.assert_awaited_once()


@pytest.mark.asyncio
async def test_compacted_context_keeps_original_user_history_and_does_not_report():
    runtime = worker([])
    runtime.session.session_manager.export_portable_context.return_value = {
        "meta": {"transcript": [{"role": "user", "content": "earlier question"}]},
    }
    await runtime._report_and_poll()
    runtime.session.start_run.assert_not_called()


@pytest.mark.asyncio
async def test_existing_user_message_even_without_answer_skips_report():
    runtime = worker([{"role": "user", "content": "pending question"}])
    await runtime._report_and_poll()
    runtime.session.start_run.assert_not_called()
    runtime.transport.request.assert_awaited_once_with(
        "POST",
        "history",
        json={
            "hosted_bootstrap": True,
            "messages": [{"role": "user", "content": "pending question"}],
        },
        timeout=120,
    )
    runtime._poll.assert_awaited_once()


@pytest.mark.asyncio
async def test_active_foreground_wins_instead_of_extra_report():
    runtime = worker([])
    runtime.session.start_run.side_effect = RuntimeError("session_busy")
    await runtime._report_and_poll()
    runtime.session.wait_run.assert_not_awaited()
    runtime._poll.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_report_is_visible_and_does_not_block_polling():
    runtime = worker([])
    runtime.session.run_status.return_value = {"state": "failed"}
    await runtime._report_and_poll()
    assert "报到失败" in runtime.context_sync_error
    runtime._poll.assert_awaited_once()


def test_monotonic_timer_starts_on_execution_and_freezes(monkeypatch):
    clock = iter([10, 11, 12])
    monkeypatch.setattr("aero.application.request_timing.time.monotonic", lambda: next(clock))
    with timed_request() as timer:
        assert timer.payload() == {}
        start_execution_timer()
        start_execution_timer()  # No reset on same-turn retry.
        assert timer.payload() == {"execution_ms": 1000}
        timer.stop()
    assert timer.payload() == {"execution_ms": 2000}
    assert RequestTimer().payload() == {}


def test_error_also_freezes_timer(monkeypatch):
    clock = iter([10, 10.25])
    monkeypatch.setattr("aero.application.request_timing.time.monotonic", lambda: next(clock))
    with pytest.raises(ValueError), timed_request() as timer:
        start_execution_timer()
        raise ValueError("failed request")
    assert timer.payload() == {"execution_ms": 250}
