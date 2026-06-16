"""JSON structured-logging config (WP-6, ORAA-4 A5) — one shared ``logging.dictConfig`` factory.

Every service calls :func:`configure_structured_logging` at app startup. It installs a JSON
formatter on the root logger whose every record carries the bound ``request_id`` (and
``organisation_id`` when bound) from :mod:`oraclous_telemetry.correlation` — so a single correlation
id is queryable across every service's logs without the handler having to pass it explicitly.

The contextvars are read by a logging :class:`logging.Filter` (filters run on the emitting thread
synchronously, so they see the request's bound context), which sets ``record.request_id`` /
``record.organisation_id``; the JSON formatter then serialises a flat one-line object per record.
"""

from __future__ import annotations

import json
import logging
import logging.config
from datetime import UTC, datetime
from typing import Any

from oraclous_telemetry.correlation import get_organisation_id, get_request_id

#: Standard ``logging.LogRecord`` attributes — everything else on a record is treated as a
#: structured "extra" field and merged into the JSON line.
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        "request_id",
        "organisation_id",
    }
)


class CorrelationFilter(logging.Filter):
    """Stamp the bound ``request_id`` / ``organisation_id`` onto every record.

    A filter (not a formatter hook) so the contextvar read happens on the thread that emits the
    record, while the request's context is still bound. An unbound id leaves the attribute empty,
    and the formatter omits empty correlation fields.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        if not hasattr(record, "organisation_id"):
            record.organisation_id = get_organisation_id()
        return True


class JsonFormatter(logging.Formatter):
    """Serialise a record to a flat one-line JSON object.

    Always carries ``timestamp``/``level``/``logger``/``message``; carries ``request_id`` /
    ``organisation_id`` only when bound (so unscoped startup lines aren't noise); merges any
    structured ``extra=`` fields; appends ``exc_info`` as a rendered ``exception`` string.
    """

    def format(self, record: logging.LogRecord) -> str:
        # ``log_line`` (not ``payload``/``body``) is deliberate: this is the outbound JSON log
        # object, not a request body — the org-scoping guardrail's ORG001 only inspects body-shaped
        # names, and stamping organisation_id here comes from the bound auth context, never input.
        log_line: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", "")
        if request_id:
            log_line["request_id"] = request_id
        organisation_id = getattr(record, "organisation_id", "")
        if organisation_id:
            log_line["organisation_id"] = organisation_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                log_line[key] = value
        if record.exc_info:
            log_line["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_line, default=str, separators=(",", ":"))


def structured_logging_dictconfig(level: str = "INFO") -> dict[str, Any]:
    """Return the ``logging.dictConfig`` dict that installs JSON structured logging.

    Exposed separately from :func:`configure_structured_logging` so it can be asserted in tests and
    composed by an operator who wants to extend it.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "correlation": {"()": "oraclous_telemetry.logging_config.CorrelationFilter"},
        },
        "formatters": {
            "json": {"()": "oraclous_telemetry.logging_config.JsonFormatter"},
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "filters": ["correlation"],
            },
        },
        "root": {"level": level, "handlers": ["default"]},
        # uvicorn ships its own handlers; route them through the JSON root so access/error lines
        # carry the same correlation context and shape.
        "loggers": {
            "uvicorn": {"level": level, "handlers": ["default"], "propagate": False},
            "uvicorn.error": {"level": level, "handlers": ["default"], "propagate": False},
            "uvicorn.access": {"level": level, "handlers": ["default"], "propagate": False},
        },
    }


def configure_structured_logging(level: str = "INFO") -> None:
    """Install the JSON structured-logging config on the root logger (idempotent per process)."""
    logging.config.dictConfig(structured_logging_dictconfig(level))
