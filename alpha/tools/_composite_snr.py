"""search_and_replace tool — composite (#030 split)."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ._composite_helpers import _resolve_target, _run_tool
from .file_tools import _validate_path_no_symlink


async def _search_and_replace(
    path: str,
    search: str,
    replace: str,
    file_pattern: str = "**/*",
    dry_run: bool = True,
) -> dict:
    """Search and replace across multiple files."""
    if not search:
        return {"error": "search vazio não é permitido"}
    target_path, err = _resolve_target(path, default_to_workspace=False)
    if err:
        return err

    # Find files with matches
    # #DB01: cap alto explícito. O default 50 do search_files é sobre
    # LINHAS-match — sem isto uma substituição em massa (>50 hits) truncava
    # silenciosamente e reportava sucesso parcial (files_changed/total como se
    # tivesse concluído). 10000 cobre projetos reais.
    search_result = await _run_tool(
        "search_files", path=str(target_path), pattern=re.escape(search),
        max_results=10000,
    )
    if "error" in search_result:
        return search_result
    # #D037: post-filter by file_pattern (search_files doesn't accept it)
    if file_pattern != "**/*":
        results = search_result.get("results", [])
        search_result["results"] = [r for r in results if Path(r.get("file", "")).match(file_pattern)]

    results_list = search_result.get("results", [])
    if not results_list:
        return {"matches": 0, "message": f"Nenhuma ocorrencia de '{search}' encontrada"}

    # Group by file — one entry per file with matches, in discovery order.
    matched_files: list[str] = []
    _seen: set[str] = set()
    for match in results_list:
        filepath = match.get("file", match.get("path", ""))
        if filepath and filepath not in _seen:
            _seen.add(filepath)
            matched_files.append(filepath)

    if dry_run:
        files_preview = await asyncio.to_thread(
            _snr_preview_sync, matched_files, search
        )
        return {
            "dry_run": True,
            "files_affected": len(files_preview),
            "total_matches": sum(files_preview.values()),
            "files": files_preview,
            "search": search,
            "replace": replace,
            "message": "Execute com dry_run=false para aplicar as mudancas",
        }

    changed_files, errors = await asyncio.to_thread(
        _snr_apply_sync, matched_files, search, replace
    )

    return {
        "dry_run": False,
        "files_changed": len(changed_files),
        "total_replacements": sum(f["replacements"] for f in changed_files),
        "changed": changed_files,
        "errors": errors if errors else None,
        "search": search,
        "replace": replace,
    }


def _snr_preview_sync(matched_files: list[str], search: str) -> dict[str, int]:
    # #013: count REAL occurrences (mirroring the apply phase's
    # `original.count(search)`), not grep match-LINES — a line containing
    # the term N times is one grep match but N replacements, so the old
    # per-line tally undercounted the preview. Read each file and count so
    # the dry-run total equals what dry_run=false will actually do.
    files_preview: dict[str, int] = {}
    for filepath in matched_files:
        try:
            p = _validate_path_no_symlink(filepath)
        except (PermissionError, OSError):
            continue
        if not p.exists():
            continue
        try:
            files_preview[filepath] = p.read_text(errors="replace").count(search)
        except OSError:
            continue
    return files_preview


def _snr_apply_sync(
    matched_files: list[str], search: str, replace: str
) -> tuple[list[dict], list[dict]]:
    # Apply replacements: read once, replace ALL in memory, write once.
    changed_files: list[dict] = []
    errors: list[dict] = []

    for filepath in matched_files:
        try:
            p = _validate_path_no_symlink(filepath)
        except (PermissionError, OSError) as e:
            errors.append({"file": filepath, "error": str(e)})
            continue
        if not p.exists():
            errors.append({"file": filepath, "error": "Arquivo nao encontrado"})
            continue
        try:
            original = p.read_text(errors="replace")
        except OSError as e:
            errors.append({"file": filepath, "error": str(e)})
            continue

        count = original.count(search)
        if count == 0:
            continue

        updated = original.replace(search, replace)

        # SNR escreve direto (os.replace), sem passar por write/edit_file —
        # precisa do próprio checkpoint pro /rewind cobrir replaces em massa.
        from ..checkpoints import snapshot as _ckpt_snapshot
        _ckpt_snapshot(p, "search_and_replace")

        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".sr_", dir=str(p.parent))
            try:
                os.fchmod(fd, 0o644)
                os.write(fd, updated.encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp_path, p)
            tmp_path = None
        except OSError as e:
            errors.append({"file": filepath, "error": str(e)})
            continue
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        changed_files.append({"file": filepath, "replacements": count})

    return changed_files, errors


register_tool(
    ToolDefinition(
        name="search_and_replace",
        description=(
            "Search and replace text across multiple files. dry_run mode by default "
            "(shows what would change without changing it). Use dry_run=false to apply."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Root directory for the search"},
                "search": {"type": "string", "description": "Text to search for"},
                "replace": {"type": "string", "description": "Replacement text"},
                "file_pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '**/*.py'). Default: **/*",
                    "default": "**/*",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, only shows what would change. Default: true",
                    "default": True,
                },
            },
            "required": ["path", "search", "replace"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.COMPOSITE,
        executor=_search_and_replace,
    )
)
