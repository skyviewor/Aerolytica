"""Tests for background debug logging."""

import json

import structlog

from meteora.core.debug_log import configure_debug_logging, debug_exception, debug_log
from meteora.core.logging import configure as configure_logging


def test_debug_log_writes_jsonl(tmp_path):
    path = tmp_path / "debug.log"

    configure_debug_logging(path=path)
    debug_log("test.event", value=1, unsafe=object())

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    configured = json.loads(lines[0])
    event = json.loads(lines[1])

    assert configured["event"] == "debug_log.configured"
    assert event["event"] == "test.event"
    assert event["value"] == 1
    assert isinstance(event["unsafe"], str)


def test_debug_log_can_be_disabled(tmp_path):
    path = tmp_path / "debug.log"

    configure_debug_logging(enabled=False, path=path)
    debug_log("test.disabled")

    assert not path.exists()


def test_debug_exception_writes_traceback(tmp_path):
    path = tmp_path / "debug.log"

    configure_debug_logging(path=path)
    try:
        raise ValueError("bad value")
    except ValueError as exc:
        debug_exception("test.exception", exc, extra="field")

    event = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["event"] == "test.exception"
    assert event["exception_type"] == "ValueError"
    assert event["error"] == "bad value"
    assert "ValueError: bad value" in event["traceback"]
    assert event["extra"] == "field"


def test_structlog_warning_writes_file_and_debug_log(tmp_path):
    debug_path = tmp_path / "debug.log"
    standard_path = tmp_path / "meteora.log"

    configure_debug_logging(path=debug_path)
    configure_logging(log_file=str(standard_path))

    structlog.get_logger().warning("test.warning", detail="copyable")

    assert "test.warning" in standard_path.read_text(encoding="utf-8")
    events = [
        json.loads(line)
        for line in debug_path.read_text(encoding="utf-8").splitlines()
    ]
    mirrored = [event for event in events if event["event"] == "log.warning_or_error"]
    assert mirrored[-1]["log_level"] == "warning"
    assert mirrored[-1]["log_event"] == "test.warning"
    assert mirrored[-1]["fields"]["detail"] == "copyable"
