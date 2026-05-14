"""
Multi-language code analysis tools — auto-detect language and scan.

Tools:
- analyze_codebase: Auto-detect language and scan entire codebase.
- detect_vulns_multi: Deep scan single file with language auto-detection.
- trace_dataflow_multi: Cross-language dataflow tracing.
"""

import logging
from pathlib import Path

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool

logger = logging.getLogger(__name__)
_SECURITY = ToolCategory.SECURITY

# ─── Language detection ───

_EXT_TO_LANG = {
    ".c": "c", ".h": "c",
    ".py": "python", ".pyx": "python", ".pxd": "python",
    ".js": "javascript", ".ts": "javascript", ".jsx": "javascript", ".tsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
}


def _detect_language(path: str) -> str:
    """Detect programming language from file extension or directory contents."""
    p = Path(path)
    if p.is_file():
        return _EXT_TO_LANG.get(p.suffix.lower(), "unknown")
    # Sample the directory
    extensions: dict[str, int] = {}
    for f in p.glob("**/*"):
        if f.is_file():
            ext = f.suffix.lower()
            if ext in _EXT_TO_LANG:
                extensions[_EXT_TO_LANG[ext]] = extensions.get(_EXT_TO_LANG[ext], 0) + 1
    if extensions:
        return max(extensions, key=extensions.get)
    return "unknown"


# ─── Tools ───


async def analyze_codebase(path: str = ".", language: str = "auto",
                           min_severity: str = "BAIXO") -> dict:
    """Auto-detect language and scan entire codebase for vulnerabilities.

    Supports: Python, C, JavaScript/TypeScript, Go, Rust.
    Falls back to generic pattern scanning for unknown languages.

    Args:
        path: Root directory of the codebase.
        language: Force language ('python', 'c', 'javascript', 'go', 'rust')
                  or 'auto' to detect.
        min_severity: Minimum severity to report.
    """
    root = Path(path).resolve()
    if not root.is_dir():
        return {"ok": False, "error": f"Not a directory: {path}"}

    lang = language if language != "auto" else _detect_language(str(root))

    # Cache analyzer classes to avoid repeated lazy imports (#085)
    _ANALYZER_CACHE: dict[str, type] = {}

    if lang == "c":
        cls = _ANALYZER_CACHE.get("c")
        if cls is None:
            from alpha.c_analyzer import CASTAnalyzer
            cls = CASTAnalyzer
            _ANALYZER_CACHE["c"] = cls
        result = cls().scan_codebase(str(root))
    elif lang == "javascript":
        cls = _ANALYZER_CACHE.get("js")
        if cls is None:
            from alpha.js_analyzer import JSAnalyzer
            cls = JSAnalyzer
            _ANALYZER_CACHE["js"] = cls
        result = cls().scan_codebase(str(root))
    elif lang == "go":
        cls = _ANALYZER_CACHE.get("go")
        if cls is None:
            from alpha.go_analyzer import GoAnalyzer
            cls = GoAnalyzer
            _ANALYZER_CACHE["go"] = cls
        result = cls().scan_codebase(str(root))
    elif lang == "rust":
        cls = _ANALYZER_CACHE.get("rust")
        if cls is None:
            from alpha.rust_analyzer import RustAnalyzer
            cls = RustAnalyzer
            _ANALYZER_CACHE["rust"] = cls
        result = cls().scan_codebase(str(root))
    elif lang == "python":
        # Use depgraph for Python — entry points + sinks
        from alpha.depgraph import DependencyGraph
        dg = DependencyGraph()
        graph_result = dg.build(str(root))
        entries = dg.find_entry_points()
        sinks = dg.find_dangerous_sinks()
        result = {
            "ok": True, "language": "python",
            "files_scanned": graph_result.get("files_parsed", 0),
            "entry_points": len(entries),
            "dangerous_sinks": len(sinks),
            "findings": [
                {"type": "entry_point", "file": e["file"], "line": 0,
                 "severity": "MÉDIO", "detail": f"Entry point (score={e['score']})"}
                for e in entries[:20]
            ] + [
                {"type": f"dangerous_sink_{s['sink_type']}", "file": s["file"],
                 "line": s["line"], "severity": "ALTO",
                 "detail": f"{s['caller']} -> {s['callee']}"}
                for s in sinks[:30]
            ],
        }
    else:
        return {"ok": False, "error": f"Unsupported language: {lang}"}

    # Filter by severity using shared constant
    from alpha.analyzer_base import Sev
    result["findings"] = Sev.filter(result.get("findings", []), min_severity)
    result["total_findings"] = len(result.get("findings", []))
    result["language"] = lang

    return result


async def detect_vulns_multi(file: str, language: str = "auto") -> dict:
    """Deep scan a single file with language auto-detection.

    Args:
        file: Path to source file.
        language: Force language or 'auto'.
    """
    path = Path(file).resolve()
    if not path.is_file():
        return {"ok": False, "error": f"File not found: {file}"}

    lang = language if language != "auto" else _detect_language(str(path))

    if lang == "c":
        from alpha.c_analyzer import CASTAnalyzer
        a = CASTAnalyzer()
        findings = a.scan_file(str(path))
    elif lang == "javascript":
        from alpha.js_analyzer import JSAnalyzer
        findings = JSAnalyzer().scan_file(str(path))
    elif lang == "go":
        from alpha.go_analyzer import GoAnalyzer
        findings = GoAnalyzer().scan_file(str(path))
    elif lang == "rust":
        from alpha.rust_analyzer import RustAnalyzer
        findings = RustAnalyzer().scan_file(str(path))
    else:
        return {"ok": False, "error": f"Unsupported language: {lang}"}

    return {
        "ok": True,
        "file": str(path),
        "language": lang,
        "total_findings": len(findings),
        "findings": findings,
    }


async def auto_exploit_multi(target: str, language: str = "auto",
                             max_rounds: int = 15, timeout: float = 10.0) -> dict:
    """Run autonomous exploit feedback loop with language auto-detection.

    For C binaries: uses binary exploitation (check_mitigations, shellcode, sandbox).
    For interpreted languages: generates PoC code instead.

    Args:
        target: Path to binary (C) or source file (interpreted).
        language: Force language or 'auto'.
        max_rounds: Maximum exploit rounds.
        timeout: Per-round timeout.
    """
    path = Path(target).resolve()
    lang = language if language != "auto" else _detect_language(str(path))

    if lang == "c":
        from alpha.exploit_feedback import ExploitFeedbackLoop
        loop = ExploitFeedbackLoop(str(path))
        session = await loop.run(max_rounds=max_rounds, timeout=timeout)
        return {
            "ok": True, "language": "c",
            "success": session.success,
            "offset_found": session.offset_found,
            "rounds": len(session.rounds),
        }

    # Interpreted languages: generate PoC script
    if not path.is_file():
        return {"ok": False, "error": f"File not found: {target}"}

    source = path.read_text(encoding="utf-8", errors="replace")

    if lang == "python":
        from alpha.depgraph import DependencyGraph
        dg = DependencyGraph()
        dg.build(str(path.parent))
        sinks = dg.find_dangerous_sinks()
        return {
            "ok": True, "language": "python",
            "mode": "static_analysis_poc",
            "dangerous_sinks_found": len(sinks),
            "top_sinks": [
                {"file": s["file"], "line": s["line"], "sink": s["callee"], "type": s["sink_type"]}
                for s in sinks[:10]
            ],
            "note": "For interpreted languages, review sinks and craft PoC manually",
        }

    if lang == "javascript":
        from alpha.js_analyzer import JSAnalyzer
        findings = JSAnalyzer().scan_file(str(path))
        return {
            "ok": True, "language": "javascript",
            "mode": "static_analysis_poc",
            "findings": len(findings),
            "top_findings": findings[:10],
            "note": "Review XSS/prototype pollution/eval sinks for exploitability",
        }

    return {"ok": False, "error": f"Exploit not supported for language: {lang}"}


# ─── Register ───

_SAFE = ToolSafety.SAFE
_DESTRUCTIVE = ToolSafety.DESTRUCTIVE

MULTI_TOOLS = [
    ToolDefinition(
        name="analyze_codebase",
        description="Auto-detect language and scan codebase for vulnerabilities. Supports Python, C, JavaScript, Go, Rust.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Codebase root directory."},
                "language": {"type": "string", "description": "Force language or 'auto'.", "enum": ["auto", "python", "c", "javascript", "go", "rust"]},
                "min_severity": {"type": "string", "description": "Minimum severity.", "enum": ["CRÍTICO", "ALTO", "MÉDIO", "BAIXO"]},
            },
            "required": [],
        },
        safety=_SAFE, category=_SECURITY, executor=analyze_codebase,
    ),
    ToolDefinition(
        name="detect_vulns_multi",
        description="Deep scan a single file with language auto-detection. Supports C, JavaScript, Go, Rust.",
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path to source file."},
                "language": {"type": "string", "description": "Force language or 'auto'.", "enum": ["auto", "c", "javascript", "go", "rust"]},
            },
            "required": ["file"],
        },
        safety=_SAFE, category=_SECURITY, executor=detect_vulns_multi,
    ),
    ToolDefinition(
        name="auto_exploit_multi",
        description="Autonomous exploit with language auto-detection. Binary exploitation for C, static PoC for Python/JS.",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Path to binary or source file."},
                "language": {"type": "string", "description": "Force language or 'auto'.", "enum": ["auto", "c", "python", "javascript"]},
                "max_rounds": {"type": "integer", "description": "Maximum exploit rounds."},
                "timeout": {"type": "number", "description": "Per-round timeout."},
            },
            "required": ["target"],
        },
        safety=_DESTRUCTIVE, category=_SECURITY, executor=auto_exploit_multi,
    ),
]

for td in MULTI_TOOLS:
    register_tool(td)

logger.info("Multi-language tools registered: %d tools", len(MULTI_TOOLS))
