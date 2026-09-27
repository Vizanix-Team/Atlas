"""Structured logging.

Collection runs inside GitHub Actions, where the log is the only artefact that
survives a failed job. Atlas therefore emits one JSON object per line with a
stable field set, so that a run can be reconstructed afterwards without guessing.

Per-instrument logging is suppressed by default: a run touching 60,000
instruments must not produce 60,000 log lines, and a raw venue response is never
logged at ``INFO`` because responses are large and Atlas treats them as untrusted.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName",
    }
)

# Fields promoted to the top level of every record when present in the extra dict,
# in this order, so that a human scanning a log finds them in a predictable place.
_ORDERED_CONTEXT = (
    "run_id",
    "generation_id",
    "venue",
    "operation",
    "instrument_id",
    "asset_id",
    "status",
    "error_type",
    "duration_ms",
)


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialise ``record``, promoting known context fields ahead of the rest."""
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03dZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
        for key in _ORDERED_CONTEXT:
            if key in extras:
                payload[key] = extras.pop(key)
        payload.update(extras)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class HumanFormatter(logging.Formatter):
    """Render a log record for a developer's terminal.

    Used when Atlas is not running under CI, where readability beats parseability.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialise ``record`` as an aligned single line with trailing context."""
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        suffix = " ".join(f"{k}={v}" for k, v in extras.items())
        base = f"{record.levelname:<7} {record.name:<34} {record.getMessage()}"
        line = f"{base}  {suffix}" if suffix else base
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(*, level: str | int | None = None, json_output: bool | None = None) -> None:
    """Install Atlas's root log handler.

    Args:
        level: Log level; defaults to ``ATLAS_LOG_LEVEL`` or ``INFO``.
        json_output: Force JSON or human output. Defaults to JSON when the ``CI``
            environment variable is set, because that is where logs are parsed.
    """
    resolved_level = level if level is not None else os.environ.get("ATLAS_LOG_LEVEL", "INFO")
    use_json = json_output if json_output is not None else bool(os.environ.get("CI"))

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if use_json else HumanFormatter())

    root = logging.getLogger("vizanix_atlas")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved_level)
    # Atlas owns its own logging tree; leaving propagation on would duplicate every
    # line when an embedding application has configured the root logger.
    root.propagate = False

    # httpx logs one INFO line per request, which at Atlas's request volume buries
    # everything else. Adapter-level metrics record the same information.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a logger inside the ``vizanix_atlas`` tree."""
    suffix = name.removeprefix("vizanix_atlas.")
    return logging.getLogger(f"vizanix_atlas.{suffix}")
