"""Search and replace — composite tool for bulk file modifications."""

import logging
import re

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ._composite_helpers import _run_tool, _violation
from .file_tools import _validate_path_no_symlink
from .path_helpers import _atomic_write, _validate_path
from .workspace import AGENT_WORKSPACE

logger = logging.getLogger(__name__)


async def _search_and_replace(
    path: str,
    search: str,
    replace: str,
    file_pattern: str = "**/*",
    dry_run: bool = True,
) -> dict:
    """Search and replace across multiple files."""
    try:
        target_path = _validate_path(path)
    except PermissionError as e:
        return _violation(str(e))

    # Find files with matches
    search_result = await _run_tool("search_files", path=str(target_path), pattern=re.escape(search))
    if "error" in search_result:
        return search_result

    results_list = search_result.get("results", [])
    if not results_list:
        return {"matches": 0, "message": f"Nenhuma ocorrencia de '{search}' encontrada"}

    # Group by file
    files_to_change = {}
    for match in results_list:
        filepath = match.get("file", match.get("path", ""))
        if filepath:
            if filepath not in files_to_change:
                files_to_change[filepath] = 0
            files_to_change[filepath] += 1

    if dry_run:
        return {
            "dry_run": True,
            "files_affected": len(files_to_change),
            "total_matches": sum(files_to_change.values()),
            "files": files_to_change,
            "search": search,
            "replace": replace,
            "message": "Execute com dry_run=false para aplicar as mudancas",
        }

    changed_files = []
    errors = []

    for filepath in files_to_change:
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
        try:
            _atomic_write(p, updated.encode("utf-8"))
        except OSError as e:
            errors.append({"file": filepath, "error": str(e)})
            continue
        changed_files.append({"file": filepath, "replacements": count})

    return {
        "dry_run": False,
        "files_changed": len(changed_files),
        "total_replacements": sum(f["replacements"] for f in changed_files),
        "changed": changed_files,
        "errors": errors if errors else None,
        "search": search,
        "replace": replace,
    }


register_tool(
    ToolDefinition(
        name="search_and_replace",
        description=(
            "Buscar e substituir texto em multiplos arquivos. "
            "Modo dry_run por padrao (mostra o que seria alterado sem alterar). "
            "Use dry_run=false para aplicar."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Diretorio raiz para busca",
                },
                "search": {
                    "type": "string",
                    "description": "Texto a buscar",
                },
                "replace": {
                    "type": "string",
                    "description": "Texto de substituicao",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Padrao glob para filtrar arquivos (ex: '**/*.py'). Padrao: **/*",
                    "default": "**/*",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Se true, apenas mostra o que seria alterado. Padrao: true",
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
