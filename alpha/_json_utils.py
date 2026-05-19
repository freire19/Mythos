"""Shared JSON helpers.

Centralizes the "read a JSON file, tolerate failure, return a default"
pattern that was previously duplicated across `settings.py`, `history.py`,
and `skills/manager.py` with subtly different exception sets and
logging behavior.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


def load_json_file(
    path: Path | str | None,
    default: Any = None,
    *,
    logger: logging.Logger | None = None,
) -> Any:
    """Read and JSON-decode a file. Return ``default`` on any failure.

    Failures that fall back to ``default``: missing path, unreadable file,
    invalid UTF-8, malformed JSON. Pass ``logger`` to emit a warning on
    failure; omit it for callers that intentionally swallow errors (e.g.
    a corrupt state index that should rebuild silently).
    """
    if path is None:
        return default
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        if logger is not None:
            logger.warning("Failed to read %s: %s", p, e)
        return default
