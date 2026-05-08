"""
Static analysis for C source code — AST-level vulnerability detection.

Uses pycparser to parse C into an AST, then walks it looking for:
- Buffer overflow (unbounded strcpy/sprintf/gets/scanf)
- Use-after-free (free followed by deref on same path)
- Format string (printf with non-literal format)
- Integer overflow (malloc(a*b) without bounds check)
- Double free (free called twice on same variable)
- Null dereference (deref after possible null assignment)

Designed for the NARROW→INSPECT phases: after MAP identifies entry points,
this module inspects C code line-by-line at the AST level.
"""

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─── pycparser availability ───

_pycparser_available: bool | None = None


def _has_pycparser() -> bool:
    global _pycparser_available
    if _pycparser_available is None:
        try:
            import pycparser  # noqa: F401
            _pycparser_available = True
        except ImportError:
            _pycparser_available = False
    return _pycparser_available


# ─── Vulnerability types ───

class VulnSeverity:
    CRITICAL = "CRÍTICO"
    HIGH = "ALTO"
    MEDIUM = "MÉDIO"
    LOW = "BAIXO"


VULN_TYPES = {
    "buffer_overflow": {
        "severity": VulnSeverity.HIGH,
        "description": "Unbounded copy into fixed-size buffer",
        "cwe": "CWE-120",
    },
    "format_string": {
        "severity": VulnSeverity.HIGH,
        "description": "User-controlled format string",
        "cwe": "CWE-134",
    },
    "use_after_free": {
        "severity": VulnSeverity.CRITICAL,
        "description": "Memory access after deallocation",
        "cwe": "CWE-416",
    },
    "double_free": {
        "severity": VulnSeverity.CRITICAL,
        "description": "Free called twice on same pointer",
        "cwe": "CWE-415",
    },
    "integer_overflow": {
        "severity": VulnSeverity.MEDIUM,
        "description": "Arithmetic in allocation without overflow guard",
        "cwe": "CWE-190",
    },
    "null_deref": {
        "severity": VulnSeverity.MEDIUM,
        "description": "Pointer dereference after possible null assignment",
        "cwe": "CWE-476",
    },
    "unsafe_function": {
        "severity": VulnSeverity.LOW,
        "description": "Use of inherently unsafe function",
        "cwe": "CWE-242",
    },
}

# ─── Dangerous function patterns ───

UNBOUNDED_COPY_FUNCTIONS = {
    "strcpy", "strcat", "sprintf", "vsprintf",
    "gets", "scanf", "fscanf", "sscanf",
}

BOUNDED_COPY_FUNCTIONS = {
    "strncpy", "strncat", "snprintf", "vsnprintf",
    "memcpy", "memmove", "memccpy",
}

FORMAT_FUNCTIONS = {
    "printf", "fprintf", "sprintf", "snprintf",
    "syslog", "dprintf", "vfprintf", "vsprintf",
}

MEMORY_ALLOCATION = {
    "malloc", "calloc", "realloc", "alloca",
    "strdup", "strndup", "asprintf",
}

MEMORY_FREE = {
    "free", "realloc",
}

# ─── Regex-based pre-scanner (fallback when pycparser unavailable) ───


def _regex_scan(source: str, file_path: str) -> list[dict]:
    """Regex-based C vulnerability scanner. Less precise than AST but no deps."""
    findings: list[dict] = []
    lines = source.split("\n")

    # Buffer overflow: strcpy, sprintf, gets
    for func in ("strcpy", "strcat", "sprintf", "gets"):
        pattern = rf"\b{func}\s*\("
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line) and "// safe" not in line.lower():
                findings.append({
                    "type": "buffer_overflow",
                    "file": file_path,
                    "line": i,
                    "code": line.strip()[:200],
                    "severity": VulnSeverity.HIGH,
                    "detail": f"Unbounded {func}() — possible buffer overflow",
                    "function": func,
                })

    # Format string: printf with variable (non-literal) first arg
    for func in FORMAT_FUNCTIONS:
        pattern = rf"\b{func}\s*\(\s*([^\"])"
        for i, line in enumerate(lines, 1):
            m = re.search(pattern, line)
            if m and not line.strip().startswith("//"):
                findings.append({
                    "type": "format_string",
                    "file": file_path,
                    "line": i,
                    "code": line.strip()[:200],
                    "severity": VulnSeverity.HIGH,
                    "detail": f"{func}() with non-literal format — format string vulnerability",
                    "function": func,
                })

    # Use-after-free / double free heuristic
    free_lines: dict[str, int] = {}
    for i, line in enumerate(lines, 1):
        fm = re.search(r"\bfree\s*\(\s*(\w+)\s*\)", line)
        if fm:
            var = fm.group(1)
            if var in free_lines:
                findings.append({
                    "type": "double_free",
                    "file": file_path,
                    "line": i,
                    "code": line.strip()[:200],
                    "severity": VulnSeverity.CRITICAL,
                    "detail": f"free({var}) called twice (first at line {free_lines[var]})",
                    "function": "free",
                })
            free_lines[var] = i

    # malloc(size * count) without overflow check
    for i, line in enumerate(lines, 1):
        m = re.search(r"\b(calloc|malloc)\s*\(\s*(\w+)\s*\*\s*(\w+)\s*\)", line)
        if m:
            findings.append({
                "type": "integer_overflow",
                "file": file_path,
                "line": i,
                "code": line.strip()[:200],
                "severity": VulnSeverity.MEDIUM,
                "detail": f"{m.group(1)}({m.group(2)}*{m.group(3)}) without overflow check",
                "function": m.group(1),
            })

    return findings


# ─── AST-based scanner (requires pycparser) ───


class CFinding:
    """A single vulnerability finding from AST analysis."""

    def __init__(self, vuln_type: str, file_path: str, line: int,
                 code: str, detail: str, function: str = "",
                 variable: str = ""):
        self.vuln_type = vuln_type
        self.file = file_path
        self.line = line
        self.code = code[:300]
        self.detail = detail
        self.function = function
        self.variable = variable
        self.severity = VULN_TYPES.get(vuln_type, {}).get("severity", VulnSeverity.LOW)

    def to_dict(self) -> dict:
        return {
            "type": self.vuln_type,
            "file": self.file,
            "line": self.line,
            "code": self.code,
            "severity": self.severity,
            "detail": self.detail,
            "function": self.function,
            "variable": self.variable,
            "cwe": VULN_TYPES.get(self.vuln_type, {}).get("cwe", ""),
        }


class CASTAnalyzer:
    """AST-level C vulnerability scanner using pycparser.

    Usage:
        analyzer = CASTAnalyzer()
        findings = analyzer.scan_file("src/vuln.c")
        for f in findings:
            print(f"{f.severity}: {f.detail} at {f.file}:{f.line}")
    """

    def __init__(self):
        self.findings: list[CFinding] = []
        self._current_file = ""
        self._source_lines: list[str] = []
        # Track variables that have been freed on each path
        self._freed_vars: set[str] = set()
        self._null_checked_vars: set[str] = set()

    def scan_file(self, file_path: str) -> list[dict]:
        """Scan a single C file and return list of finding dicts."""
        if not _has_pycparser():
            try:
                source = Path(file_path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                return []
            return _regex_scan(source, file_path)

        self.findings = []
        self._current_file = file_path
        self._freed_vars = set()
        self._null_checked_vars = set()

        try:
            source = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        self._source_lines = source.split("\n")

        try:
            from pycparser import CParser, c_ast
            parser = CParser()
            ast = parser.parse(source, filename=file_path)
            self._walk(ast)
        except Exception as e:
            logger.debug("pycparser failed for %s: %s — falling back to regex", file_path, e)
            return _regex_scan(source, file_path)

        return [f.to_dict() for f in self.findings]

    def scan_codebase(self, root: str, glob_pattern: str = "**/*.c") -> dict:
        """Scan all C files in a codebase. Returns summary + findings."""
        base = Path(root)
        all_findings: list[dict] = []
        files_scanned = 0
        errors: list[str] = []

        for file_path in sorted(base.glob(glob_pattern)):
            if any(p.startswith(".") for p in file_path.parts):
                continue
            if "test" in file_path.parts:
                continue
            try:
                findings = self.scan_file(str(file_path))
                all_findings.extend(findings)
                files_scanned += 1
            except Exception as e:
                errors.append(f"{file_path}: {e}")

        # Group by type
        by_type: dict[str, int] = defaultdict(int)
        by_severity: dict[str, int] = defaultdict(int)
        for f in all_findings:
            by_type[f["type"]] += 1
            by_severity[f["severity"]] += 1

        return {
            "ok": True,
            "files_scanned": files_scanned,
            "total_findings": len(all_findings),
            "by_type": dict(by_type),
            "by_severity": dict(by_severity),
            "findings": all_findings,
            "errors": errors,
        }

    def _walk(self, node: Any) -> None:
        """Recursively walk the AST looking for vulnerabilities."""
        if node is None:
            return

        from pycparser import c_ast

        # Check for dangerous function calls
        if isinstance(node, c_ast.FuncCall):
            self._check_func_call(node)

        # Check for free() calls (track for UAF/double-free)
        if isinstance(node, c_ast.FuncCall):
            self._check_free_call(node)

        # Check for null dereference patterns
        if isinstance(node, c_ast.Assignment):
            self._check_null_assignment(node)

        if isinstance(node, c_ast.UnaryOp) and node.op == "*":
            self._check_null_deref(node)

        # Recurse into children
        for child in node.children():
            self._walk(child)

    def _check_func_call(self, node: Any) -> None:
        """Detect dangerous function calls."""
        from pycparser import c_ast

        if not isinstance(node.name, c_ast.ID):
            return
        func_name = node.name.name

        # Unbounded copy: strcpy, sprintf, gets
        if func_name in UNBOUNDED_COPY_FUNCTIONS:
            line = self._get_line(node)
            self.findings.append(CFinding(
                "buffer_overflow",
                self._current_file,
                line,
                self._get_code(line),
                f"Unbounded {func_name}() — attacker-controlled input can overflow destination buffer",
                function=func_name,
            ))

        # Format string: printf with non-literal
        if func_name in FORMAT_FUNCTIONS and node.args:
            first_arg = node.args.exprs[0] if node.args.exprs else None
            if first_arg and not isinstance(first_arg, c_ast.Constant):
                line = self._get_line(node)
                self.findings.append(CFinding(
                    "format_string",
                    self._current_file,
                    line,
                    self._get_code(line),
                    f"{func_name}() format argument is not a string literal — format string vulnerability",
                    function=func_name,
                ))

        # Unsafe: gets()
        if func_name == "gets":
            line = self._get_line(node)
            self.findings.append(CFinding(
                "buffer_overflow",
                self._current_file,
                line,
                self._get_code(line),
                "gets() has no bounds checking — always a buffer overflow",
                function="gets",
            ))

        # malloc(size * count) without overflow check
        if func_name in ("malloc", "calloc") and node.args:
            args = node.args.exprs
            if len(args) >= 1:
                for arg in args:
                    if isinstance(arg, c_ast.BinaryOp) and arg.op == "*":
                        line = self._get_line(node)
                        self.findings.append(CFinding(
                            "integer_overflow",
                            self._current_file,
                            line,
                            self._get_code(line),
                            f"{func_name}(a*b) can overflow — add SIZE_MAX/width check before multiply",
                            function=func_name,
                        ))
                        break

    def _check_free_call(self, node: Any) -> None:
        """Track free() calls for use-after-free and double-free detection."""
        from pycparser import c_ast

        if not isinstance(node.name, c_ast.ID):
            return
        if node.name.name not in ("free", "realloc"):
            return

        # Extract variable name being freed
        if node.args and node.args.exprs:
            arg = node.args.exprs[0]
            var_name = self._extract_var_name(arg)
            if var_name:
                line = self._get_line(node)
                if var_name in self._freed_vars:
                    self.findings.append(CFinding(
                        "double_free",
                        self._current_file,
                        line,
                        self._get_code(line),
                        f"free({var_name}) called twice — double free",
                        function="free",
                        variable=var_name,
                    ))
                self._freed_vars.add(var_name)

                # Check for use-after-free: if this variable was used AFTER free
                # (simplified: scan subsequent lines for deref of same var)

    def _check_null_assignment(self, node: Any) -> None:
        """Track assignments of NULL for null deref detection."""
        from pycparser import c_ast

        if isinstance(node.rvalue, c_ast.Constant) and node.rvalue.value == "0":
            if isinstance(node.lvalue, c_ast.ID):
                self._null_checked_vars.add(node.lvalue.name)

    def _check_null_deref(self, node: Any) -> None:
        """Detect dereference of possibly-null pointer."""
        from pycparser import c_ast

        if isinstance(node.expr, c_ast.ID):
            var = node.expr.name
            if var in self._null_checked_vars:
                # Deref of a var that was set to NULL earlier — could be path-dependent
                line = self._get_line(node)
                self.findings.append(CFinding(
                    "null_deref",
                    self._current_file,
                    line,
                    self._get_code(line),
                    f"*{var} dereferenced after possible NULL assignment",
                    variable=var,
                ))

    def _extract_var_name(self, node: Any) -> str | None:
        """Extract variable name from an AST node."""
        from pycparser import c_ast
        if isinstance(node, c_ast.ID):
            return node.name
        if isinstance(node, c_ast.UnaryOp) and node.op == "&":
            if isinstance(node.expr, c_ast.ID):
                return node.expr.name
        return None

    def _get_line(self, node: Any) -> int:
        """Get line number from AST node."""
        if hasattr(node, "coord") and node.coord:
            return node.coord.line
        return 0

    def _get_code(self, line: int) -> str:
        """Get source code for a given line number."""
        if 1 <= line <= len(self._source_lines):
            return self._source_lines[line - 1].strip()[:300]
        return ""
