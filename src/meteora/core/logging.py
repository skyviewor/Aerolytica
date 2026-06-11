"""Structlog configuration for Meteora CLI."""

import logging
import sys
from pathlib import Path

import structlog


def _mirror_warning_to_debug_log(_logger, _method_name, event_dict):
    level = event_dict.get("level", "")
    if level not in {"warning", "error", "critical", "exception"}:
        return event_dict

    try:
        from meteora.core.debug_log import debug_log

        debug_log(
            "log.warning_or_error",
            log_level=level,
            log_event=str(event_dict.get("event", "")),
            fields={
                str(k): v
                for k, v in event_dict.items()
                if k not in {"event", "level", "timestamp"}
            },
        )
    except Exception:
        pass

    return event_dict


def configure(log_level: str = "WARNING", log_file: str | None = None):
    """Configure structlog.

    Routes structlog through Python's standard logging so level filtering works.
    """

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            _mirror_warning_to_debug_log,
            structlog.dev.ConsoleRenderer()
            if log_file is None and sys.stderr.isatty()
            else structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    level = getattr(logging, log_level.upper(), logging.WARNING)
    handlers: list[logging.Handler]
    if log_file is None:
        handlers = [logging.StreamHandler(sys.stderr)]
    else:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers = [logging.FileHandler(path, encoding="utf-8")]

    logging.basicConfig(format="%(message)s", handlers=handlers, level=level, force=True)

    for name in ["httpx", "httpcore", "xarray", "cdsapi"]:
        logging.getLogger(name).setLevel(logging.WARNING)
