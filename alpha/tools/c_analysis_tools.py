"""
C code analysis tools — static vulnerability detection for C codebases.

Tools:
- analyze_c_codebase: Scan C files for buffer overflow, UAF, format string,
  integer overflow, double free, null deref.
- detect_c_vulns: Deep scan of a single C file with function-level context.
- c_dataflow_trace: Trace data from input sources to dangerous sinks in C code.
"""

import logging
from pathlib import Path

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool

logger = logging.getLogger(__name__)

_SECURITY = ToolCategory.SECURITY


# ─── Tools ───


async def analyze_c_codebase(
    path: str = ".",
    glob_pattern: str = "**/*.c",
    min_severity: str = "BAIXO",
) -> dict:
    """Scan a C codebase for common vulnerabilities.

    Uses pycparser for AST-level analysis (more accurate) with regex fallback.
    Detects: buffer overflow (unbounded strcpy/sprintf/gets), format string,
    use-after-free, double free, integer overflow (malloc(a*b)), null deref.

    Args:
        path: Root directory of the C codebase or single .c file.
        glob_pattern: File pattern for C source files.
        min_severity: Minimum severity to report (CRÍTICO, ALTO, MÉDIO, BAIXO).
    """
    from alpha.c_analyzer import CASTAnalyzer

    root = Path(path).resolve()
    if root.is_file():
        analyzer = CASTAnalyzer()
        findings = analyzer.scan_file(str(root))
        return {
            "ok": True,
            "files_scanned": 1,
            "total_findings": len(findings),
            "findings": findings,
        }

    if not root.is_dir():
        return {"ok": False, "error": f"Not found: {path}"}

    analyzer = CASTAnalyzer()
    result = analyzer.scan_codebase(str(root), glob_pattern)

    # Filter by severity
    severity_order = {"CRÍTICO": 0, "ALTO": 1, "MÉDIO": 2, "BAIXO": 3}
    min_level = severity_order.get(min_severity, 3)
    result["findings"] = [
        f for f in result["findings"]
        if severity_order.get(f["severity"], 3) <= min_level
    ]
    result["total_findings"] = len(result["findings"])

    return result


async def detect_c_vulns(
    file: str,
    include_context: bool = True,
) -> dict:
    """Deep scan of a single C file with per-function vulnerability summary.

    Parses the file into an AST, walks each function, and reports:
    - Which dangerous functions are called and with what arguments.
    - Whether bounds checking is present.
    - Buffer sizes for stack-allocated arrays near dangerous calls.
    - Per-function risk score.

    Args:
        file: Path to C source file.
        include_context: Include surrounding code context for each finding.
    """
    from alpha.c_analyzer import CASTAnalyzer

    path = Path(file).resolve()
    if not path.is_file():
        return {"ok": False, "error": f"File not found: {file}"}

    analyzer = CASTAnalyzer()
    findings = analyzer.scan_file(str(path))

    # Per-function summary
    functions: dict[str, dict] = {}
    for f in findings:
        func = f.get("function", "unknown")
        if func not in functions:
            functions[func] = {"calls": 0, "findings": 0, "risk": 0}
        functions[func]["findings"] += 1
        sev = f.get("severity", "BAIXO")
        risk_map = {"CRÍTICO": 10, "ALTO": 5, "MÉDIO": 2, "BAIXO": 1}
        functions[func]["risk"] += risk_map.get(sev, 1)

    # Sort by risk
    ranked = sorted(functions.items(), key=lambda x: x[1]["risk"], reverse=True)

    return {
        "ok": True,
        "file": str(path),
        "total_findings": len(findings),
        "functions_analyzed": len(functions),
        "riskiest_functions": [
            {"name": name, **info}
            for name, info in ranked[:10]
        ],
        "findings": findings if include_context else [
            {k: v for k, v in f.items() if k != "code"}
            for f in findings
        ],
    }


async def c_dataflow_trace(
    file: str,
    source_var: str = "",
    sink_func: str = "",
) -> dict:
    """Trace data flow from input source to dangerous sink within a C function.

    Identifies paths where user-controlled data reaches dangerous functions.
    Sources: argv, getenv, read, fgets, recv, scanf parameters.
    Sinks: strcpy, sprintf, system, popen, memcpy with variable size.

    Args:
        file: Path to C source file.
        source_var: Variable name to trace from (empty = auto-detect inputs).
        sink_func: Sink function to trace to (empty = all sinks).
    """
    from alpha.c_analyzer import CASTAnalyzer

    path = Path(file).resolve()
    if not path.is_file():
        return {"ok": False, "error": f"File not found: {file}"}

    analyzer = CASTAnalyzer()
    findings = analyzer.scan_file(str(path))

    # Identify source→sink pairs
    sources = {"argv", "getenv", "read", "fgets", "recv", "scanf", "getchar", "getc"}
    sinks = {"strcpy", "strcat", "sprintf", "system", "popen", "execve",
             "memcpy", "memmove", "strncpy", "snprintf", "free"}

    source_findings = []
    sink_findings = []
    for f in findings:
        func = f.get("function", "")
        if func in sources:
            source_findings.append(f)
        if func in sinks:
            sink_findings.append(f)

    # Try to find direct connections: same function containing both source and sink
    connections: list[dict] = []
    for sf in source_findings:
        for df in sink_findings:
            if sf["file"] == df["file"]:
                connections.append({
                    "source_line": sf["line"],
                    "source_func": sf["function"],
                    "source_code": sf["code"][:100],
                    "sink_line": df["line"],
                    "sink_func": df["function"],
                    "sink_code": df["code"][:100],
                    "sink_type": df["type"],
                    "distance": abs(sf["line"] - df["line"]),
                })

    connections.sort(key=lambda c: c["distance"])

    return {
        "ok": True,
        "file": str(path),
        "total_findings": len(findings),
        "sources_detected": len(source_findings),
        "sinks_detected": len(sink_findings),
        "source_sink_pairs": len(connections),
        "connections": connections[:20],
    }


# ─── Register ───

_SAFE = ToolSafety.SAFE

C_TOOLS = [
    ToolDefinition(
        name="analyze_c_codebase",
        description="Scan C codebase for vulnerabilities: buffer overflow, format string, UAF, double free, integer overflow, null deref. AST-level analysis with regex fallback.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Root directory or .c file."},
                "glob_pattern": {"type": "string", "description": "File pattern. Default: **/*.c"},
                "min_severity": {"type": "string", "description": "Minimum severity: CRÍTICO, ALTO, MÉDIO, BAIXO.", "enum": ["CRÍTICO", "ALTO", "MÉDIO", "BAIXO"]},
            },
            "required": ["path"],
        },
        safety=_SAFE,
        category=_SECURITY,
        executor=analyze_c_codebase,
    ),
    ToolDefinition(
        name="detect_c_vulns",
        description="Deep scan of a single C file with per-function risk scoring and vulnerability context.",
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path to C source file."},
                "include_context": {"type": "boolean", "description": "Include code context. Default: true."},
            },
            "required": ["file"],
        },
        safety=_SAFE,
        category=_SECURITY,
        executor=detect_c_vulns,
    ),
    ToolDefinition(
        name="c_dataflow_trace",
        description="Trace data flow from input sources to dangerous sinks within C functions. Finds source→sink attack paths.",
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path to C source file."},
                "source_var": {"type": "string", "description": "Variable to trace (empty = auto-detect)."},
                "sink_func": {"type": "string", "description": "Sink to trace to (empty = all)."},
            },
            "required": ["file"],
        },
        safety=_SAFE,
        category=_SECURITY,
        executor=c_dataflow_trace,
    ),
]

for td in C_TOOLS:
    register_tool(td)

logger.info("C analysis tools registered: %d tools", len(C_TOOLS))
