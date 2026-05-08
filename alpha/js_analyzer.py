"""JavaScript/TypeScript vulnerability analyzer. See analyzer_base.BaseAnalyzer."""

from pathlib import Path
from .analyzer_base import BaseAnalyzer, Sev


class JSAnalyzer(BaseAnalyzer):
    language = "javascript"
    VULN_DEFS = {
        "prototype_pollution": {"severity": Sev.CRITICAL, "cwe": "CWE-1321", "desc": "Improperly controlled modification of object prototype"},
        "xss_sink":           {"severity": Sev.HIGH,     "cwe": "CWE-79",   "desc": "User input flows into XSS-sensitive DOM/HTML sink"},
        "eval_injection":     {"severity": Sev.CRITICAL, "cwe": "CWE-95",   "desc": "Dynamic code execution with user-controlled input"},
        "unsafe_deserialization": {"severity": Sev.HIGH, "cwe": "CWE-502",  "desc": "Deserialization of untrusted data"},
        "path_traversal":     {"severity": Sev.HIGH,     "cwe": "CWE-22",   "desc": "User input in file path without sanitization"},
        "nosql_injection":    {"severity": Sev.CRITICAL, "cwe": "CWE-943",  "desc": "NoSQL query injection via user-controlled operators"},
        "open_redirect":      {"severity": Sev.MEDIUM,   "cwe": "CWE-601",  "desc": "URL redirection to untrusted site"},
        "ssrf":               {"severity": Sev.HIGH,     "cwe": "CWE-918",  "desc": "Server-side request forgery via user-controlled URL"},
        "csp_bypass":         {"severity": Sev.MEDIUM,   "cwe": "CWE-1021", "desc": "CSP policy that can be bypassed"},
    }

    PATTERNS = {
        "xss_sink": [
            r'\.innerHTML\s*=', r'\.outerHTML\s*=', r'document\.write\s*\(',
            r'\.insertAdjacentHTML\s*\(', r'dangerouslySetInnerHTML',
            r'__html\s*:', r'\.html\s*\(\s*[^)\'"]',
        ],
        "prototype_pollution": [
            r'\.__proto__\s*\[', r'\.constructor\.prototype',
            r'Object\.assign\s*\(\s*\{\}\s*,', r'\.merge\s*\(\s*[^,]*,\s*req\.',
            r'\.extend\s*\(\s*[^,]*,\s*req\.', r'lodash\.merge\s*\(',
        ],
        "eval_injection": [
            r'eval\s*\(\s*[^"\']', r'new\s+Function\s*\(',
            r'vm\.runInNewContext\s*\(', r'child_process\.exec\s*\(',
        ],
        "path_traversal": [
            r'fs\.readFile\s*\(\s*[^"\']+\+', r'fs\.writeFile\s*\(\s*[^"\']+\+',
            r'path\.join\s*\(\s*[^"\']', r'path\.resolve\s*\(\s*[^"\']',
        ],
        "nosql_injection": [
            r'\$where\s*:', r'\$regex\s*:.*req\.', r'\$ne\s*:.*req\.',
            r'\.find\s*\(\s*\{[^}]*req\.', r'\.findOne\s*\(\s*\{[^}]*req\.',
        ],
        "ssrf": [
            r'fetch\s*\(\s*[^"\']+\+', r'axios\.get\s*\(\s*[^"\']+\+',
            r'http\.get\s*\(\s*[^"\']+\+',
        ],
        "unsafe_deserialization": [
            r'js-yaml\.load\s*\(', r'yaml\.load\s*\(', r'node-serialize\.unserialize',
        ],
    }

    def _file_extensions(self) -> list[str]:
        return ["js", "ts", "jsx", "tsx", "mjs", "cjs"]

    def scan_file(self, file_path: str) -> list[dict]:
        source = self._read_file(file_path)
        if source is None:
            return []
        lines = source.split("\n")
        findings = self._pattern_scan(source, file_path, self.PATTERNS, self.VULN_DEFS, lines)

        # CSP bypass detection: check for unsafe CSP directives
        for i, line in enumerate(lines, 1):
            if "Content-Security-Policy" not in line:
                continue
            if "unsafe-eval" in line:
                findings.append({"type": "csp_bypass", "file": file_path, "line": i,
                                 "code": line.strip()[:200], "severity": Sev.MEDIUM,
                                 "cwe": "CWE-1021", "detail": "CSP allows unsafe-eval"})
            if "unsafe-inline" in line:
                findings.append({"type": "csp_bypass", "file": file_path, "line": i,
                                 "code": line.strip()[:200], "severity": Sev.MEDIUM,
                                 "cwe": "CWE-1021", "detail": "CSP allows unsafe-inline"})
        return findings


# Module-level API for backward compat
def scan_js_file(file_path: str) -> list[dict]:
    return JSAnalyzer().scan_file(file_path)

def scan_js_codebase(root: str) -> dict:
    return JSAnalyzer().scan_codebase(root)
