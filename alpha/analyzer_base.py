"""
Shared base for language-specific vulnerability analyzers.

Eliminates the copy-paste across go_analyzer, js_analyzer, rust_analyzer.
Each analyzer now only defines its patterns + VULN_DEFS; scanning, severity
counting, and file iteration are handled here.
"""

import re
from collections import defaultdict
from pathlib import Path

# ─── Severity constants (replace raw strings) ───

class Sev:
    CRITICAL = "CRÍTICO"
    HIGH = "ALTO"
    MEDIUM = "MÉDIO"
    LOW = "BAIXO"

    _order = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}

    @classmethod
    def filter(cls, findings: list[dict], min_sev: str) -> list[dict]:
        min_level = cls._order.get(min_sev, 3)
        return [f for f in findings if cls._order.get(f.get("severity", cls.LOW), 3) <= min_level]


# ─── Vulnerability definition ───

VulnDef = dict  # {"severity": str, "cwe": str, "desc": str}


class BaseAnalyzer:
    """Base class for language analyzers.

    Subclasses override:
        language: str               — e.g. "go", "javascript"
        VULN_DEFS: dict[str, VulnDef]
        PATTERNS: dict[str, list[str]]
        _scan_file(self, path: str) -> list[dict]
    """

    language: str = "unknown"
    VULN_DEFS: dict[str, VulnDef] = {}
    PATTERNS: dict[str, list[str]] = {}

    def scan_file(self, file_path: str) -> list[dict]:
        """Override in subclass. Returns list of finding dicts."""
        raise NotImplementedError

    def scan_codebase(self, root: str) -> dict:
        """Scan all source files in a directory. Uses shared iteration logic."""
        base = Path(root)
        all_findings: list[dict] = []
        files_scanned = 0

        for ext in self._file_extensions():
            for file_path in sorted(base.glob(f"**/*.{ext}")):
                if self._skip_file(file_path):
                    continue
                try:
                    findings = self.scan_file(str(file_path))
                    all_findings.extend(findings)
                    files_scanned += 1
                except Exception:
                    continue

        by_type: dict[str, int] = defaultdict(int)
        by_severity: dict[str, int] = defaultdict(int)
        for f in all_findings:
            by_type[f["type"]] += 1
            by_severity[f["severity"]] += 1

        return {
            "ok": True,
            "language": self.language,
            "files_scanned": files_scanned,
            "total_findings": len(all_findings),
            "by_type": dict(by_type),
            "by_severity": dict(by_severity),
            "findings": all_findings,
        }

    def _file_extensions(self) -> list[str]:
        """Override to specify file extensions for this language."""
        return []

    def _skip_file(self, file_path: Path) -> bool:
        """Shared skip logic: hidden dirs, test files, vendor dirs."""
        parts = file_path.parts
        if any(p.startswith(".") for p in parts):
            return True
        if any(p in ("test", "tests", "node_modules", "vendor", "target") for p in parts):
            return True
        name = file_path.name
        if name.startswith("test_") or name.endswith("_test.go") or name.endswith("_test.rs"):
            return True
        if ".test." in name:
            return True
        return False

    @staticmethod
    def _read_file(path: str) -> str | None:
        """Read file content sans TOCTOU — operate and handle error."""
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

    @staticmethod
    def _pattern_scan(
        source: str, file_path: str, patterns: dict[str, list[str]],
        vuln_defs: dict[str, VulnDef], lines: list[str] | None = None,
    ) -> list[dict]:
        """Shared pattern-based scanner. Returns findings for matched patterns.

        Args:
            source: Full source text.
            file_path: Path for reporting.
            patterns: {vuln_type: [regex_patterns]}.
            vuln_defs: {vuln_type: {severity, cwe, desc}}.
            lines: Pre-split lines (avoids re-splitting).
        """
        if lines is None:
            lines = source.split("\n")
        findings: list[dict] = []

        for vuln_type, regexes in patterns.items():
            vdef = vuln_defs.get(vuln_type, {})
            for regex in regexes:
                for i, line in enumerate(lines, 1):
                    if not re.search(regex, line):
                        continue
                    stripped = line.strip()
                    if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*") or stripped.startswith("///"):
                        continue
                    findings.append({
                        "type": vuln_type,
                        "file": file_path,
                        "line": i,
                        "code": stripped[:200],
                        "severity": vdef.get("severity", Sev.LOW),
                        "cwe": vdef.get("cwe", ""),
                        "detail": vdef.get("desc", ""),
                    })
                    break  # One finding per pattern per file
        return findings
