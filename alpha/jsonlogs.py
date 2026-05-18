"""
JSON-lines structured logging (Plano-Upgrade-v3 §5 — observability).

Opt-in via env: `ALPHA_JSON_LOGS=1` writes one JSON object per log record
to `~/.alpha/logs/alpha-YYYYMMDD.log`. Designed for grep-friendly post-hoc
analysis (`jq`, `lnav`) and to feed dashboards later. The plain-text
default stays untouched — this only activates when explicitly enabled.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path


class JsonFormatter(logging.Formatter):
    """logging.Formatter that emits one JSON object per record.

    Required fields: ts, level, logger, msg.
    Optional: extra fields passed via `logger.x("...", extra={"key": ...})`
    flow through as top-level keys. Exceptions are flattened to `exc`.
    """

    _RESERVED = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_installed = False


def maybe_install() -> Path | None:
    """If `ALPHA_JSON_LOGS=1`, attach a JsonFormatter file handler and
    return the log path. Otherwise no-op and return None.

    Idempotent: subsequent calls are no-ops."""
    global _installed
    if _installed:
        return None
    if os.environ.get("ALPHA_JSON_LOGS", "").lower() not in ("1", "true", "yes"):
        return None
    from .settings import alpha_user_dir
    log_dir = alpha_user_dir("logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Read-only home — fail open: silent fallback to default logging.
        return None
    log_path = log_dir / time.strftime("alpha-%Y%m%d.log")
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    # Attach to the `alpha` package logger so we don't capture every
    # third-party library that ever calls logging.getLogger().
    root = logging.getLogger("alpha")
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    _installed = True
    return log_path
