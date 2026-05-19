"""Shared loader for `.alpha/settings.json` and friends.

Resolves config files in this priority order (first match wins):
  1. ./.alpha/<file>           — project-local override
  2. <project_root>/.alpha/<file>  — bundled with the install
  3. ~/.alpha/<file>           — user-global

Two helpers:
  * `find_config_file(name)`  — returns the resolved Path, or None
  * `read_json(path, default)` — load JSON, log + return default on failure
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ._json_utils import load_json_file
from .config import _PROJECT_ROOT

logger = logging.getLogger(__name__)


def alpha_user_dir(subdir: str = "") -> Path:
    """Resolve a per-user `~/.alpha/<subdir>/` path.

    Single source of truth for the user-global Alpha directory layout so
    `jsonlogs`, `memory`, `agents`, `skills`, etc. don't each spell out
    the literal `Path.home() / ".alpha" / ...`. Pass `""` to get the
    root."""
    base = Path.home() / ".alpha"
    return base / subdir if subdir else base


def alpha_config_paths(filename: str) -> list[Path]:
    """Candidate locations for an `.alpha/<filename>` config file."""
    return [
        Path.cwd() / ".alpha" / filename,
        _PROJECT_ROOT / ".alpha" / filename,
        alpha_user_dir(filename),
    ]


def find_config_file(filename: str) -> Path | None:
    for path in alpha_config_paths(filename):
        if path.is_file():
            return path
    return None


def read_json(path: Path | None, default: Any = None) -> Any:
    """Read a JSON file. Returns `default` if the path is None, missing, or invalid."""
    return load_json_file(path, default, logger=logger)
