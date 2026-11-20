"""Shared loader for `.alpha/settings.json` and friends.

Resolves config files in this priority order (first match wins):
  1. ./.alpha/<file>           — project-local override
  2. <project_root>/.alpha/<file>  — bundled with the install
  3. ~/.alpha/<file>           — user-global

Two helpers:
  * `find_config_file(name)`  — returns the resolved Path, or None
  * `read_json(path, default)` — load JSON, log + return default on failure

Also home of `atomic_write_text` (#DM066) — the single durable
temp-file + `os.replace` writer shared by history/checkpoints/wizard/skills.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from ._json_utils import JSON_INVALID, load_json_file
from .config import PROJECT_ROOT

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
        PROJECT_ROOT / ".alpha" / filename,
        alpha_user_dir(filename),
    ]


def find_config_file(filename: str) -> Path | None:
    for path in alpha_config_paths(filename):
        if path.is_file():
            return path
    return None


_TRUST_PROJECT_ENV = "ALPHA_TRUST_PROJECT_CONFIG"


def project_config_trusted() -> bool:
    """Whether project-scoped `.alpha` config that EXECUTES commands is honored.

    Fail-closed by default (#D301/#D305): a `.alpha/settings.json` (hooks) or
    `.alpha/mcp.json` (stdio servers) planted in an untrusted repo must NOT run
    shell / spawn processes just because `alpha` was launched in that directory.
    Only `~/.alpha` config is trusted unless the user opts in by setting
    ``ALPHA_TRUST_PROJECT_CONFIG`` to a truthy value (``1``/``true``/``yes``/``on``).
    """
    return os.environ.get(_TRUST_PROJECT_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def is_user_scoped_config(path: Path | None) -> bool:
    """True only for the user-global `~/.alpha/<file>` (the trusted location).

    Project-scoped configs (`./.alpha/` or `<project_root>/.alpha/`) return
    False — command-executing config from those requires
    `project_config_trusted()` to be honored (#D301/#D305).
    """
    if path is None:
        return False
    try:
        return path.resolve().parent == alpha_user_dir().resolve()
    except OSError:
        return False


def atomic_write_text(
    path: Path | str,
    data: str,
    *,
    fsync: bool = True,
    mode: int = 0o600,
) -> None:
    """Atomically and durably write `data` (UTF-8) to `path`.

    #DM066: quatro módulos (history, checkpoints, wizard/env, skills)
    reimplementavam "temp + os.replace" com garantias divergentes — só a
    history fazia fsync+chmod. Este helper (modelado na
    `history._atomic_write`, a mais completa) é a fonte única:

    - temp via `mkstemp` no MESMO diretório (mesmo filesystem → rename
      atômico; nome único com O_EXCL — um `.tmp` de nome fixo poderia ser
      um symlink pré-plantado);
    - `fchmod(mode)` no fd (não depende de umask; default 0o600 pois os
      consumidores carregam sessões/env/índices potencialmente sensíveis);
    - `flush()+fsync()` antes do replace (#D808 — desligável via
      `fsync=False` para escritas onde a durabilidade não compensa);
    - `os.replace` promove; um crash só deixa um `.tmp` órfão, nunca o
      alvo corrompido. Se o alvo for um symlink, ele é SUBSTITUÍDO, não
      seguido (#D318); quem precisa RECUSAR symlink checa antes (history).
    - cleanup do temp em qualquer exceção (inclusive KeyboardInterrupt).
    """
    path = Path(path)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_json(path: Path | None, default: Any = None) -> Any:
    """Read a JSON file. Returns `default` if the path is None, missing, or invalid."""
    return load_json_file(path, default, logger=logger)


def read_json_or_invalid(path: Path | None, default: Any = None) -> Any:
    """Like `read_json`, but a file that EXISTS and cannot be parsed returns the
    `JSON_INVALID` sentinel instead of `default` (#D807). Lets security-sensitive
    callers (sandbox, hooks) fail closed on a corrupt settings.json instead of
    treating it as absent. Compare with ``is JSON_INVALID``."""
    return load_json_file(path, default, logger=logger, invalid_default=JSON_INVALID)
