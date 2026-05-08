"""
JavaScript/TypeScript static vulnerability analyzer.

Detects: prototype pollution, XSS sinks, eval injection, unsafe deserialization,
CSP bypass patterns, path traversal in Node.js, NoSQL injection, and more.
"""

import re
from collections import defaultdict
from pathlib import Path

# ─── Vulnerability definitions ───

VULN_DEFS = {
    "prototype_pollution": {
        "severity": "CRÍTICO",
        "cwe": "CWE-1321",
        "desc": "Improperly controlled modification of object prototype",
    },
    "xss_sink": {
        "severity": "ALTO",
        "cwe": "CWE-79",
        "desc": "User input flows into XSS-sensitive DOM/HTML sink",
    },
    "eval_injection": {
        "severity": "CRÍTICO",
        "cwe": "CWE-95",
        "desc": "Dynamic code execution with user-controlled input",
    },
    "unsafe_deserialization": {
        "severity": "ALTO",
        "cwe": "CWE-502",
        "desc": "Deserialization of untrusted data",
    },
    "path_traversal": {
        "severity": "ALTO",
        "cwe": "CWE-22",
        "desc": "User input in file path without sanitization",
    },
    "nosql_injection": {
        "severity": "CRÍTICO",
        "cwe": "CWE-943",
        "desc": "NoSQL query injection via user-controlled operators",
    },
    "open_redirect": {
        "severity": "MÉDIO",
        "cwe": "CWE-601",
        "desc": "URL redirection to untrusted site",
    },
    "ssrf": {
        "severity": "ALTO",
        "cwe": "CWE-918",
        "desc": "Server-side request forgery via user-controlled URL",
    },
    "csp_bypass": {
        "severity": "MÉDIO",
        "cwe": "CWE-1021",
        "desc": "CSP policy that can be bypassed",
    },
}

# ─── Dangerous patterns ───

# XSS sinks: DOM/HTML injection points
XSS_SINKS = [
    r'\.innerHTML\s*=',
    r'\.outerHTML\s*=',
    r'document\.write\s*\(',
    r'document\.writeln\s*\(',
    r'\.insertAdjacentHTML\s*\(',
    r'eval\s*\(',
    r'setTimeout\s*\(\s*[\w.]+',
    r'setInterval\s*\(\s*[\w.]+',
    r'new\s+Function\s*\(',
    r'dangerouslySetInnerHTML',
    r'__html\s*:',
    r'\.html\s*\(\s*[^)\'"]',
    r'\.append\s*\(\s*[^)\'"]',
]

# Prototype pollution
PROTO_POLLUTION = [
    r'\.__proto__\s*\[',
    r'\.constructor\.prototype',
    r'Object\.assign\s*\(\s*\{\}\s*,',
    r'\.merge\s*\(\s*[^,]*,\s*req\.',
    r'\.extend\s*\(\s*[^,]*,\s*req\.',
    r'lodash\.merge\s*\(',
    r'\.defaultsDeep\s*\(',
    r'JSON\.parse\s*\([^)]*\)\s*\.',
]

# Eval/code injection
EVAL_PATTERNS = [
    r'eval\s*\(\s*[^"\']',
    r'eval\s*\(\s*.*\+',
    r'eval\s*\(\s*.*concat',
    r'eval\s*\(\s*.*template',
    r'eval\s*\(\s*.*req\.',
    r'new\s+Function\s*\(\s*[^"\']',
    r'vm\.runInNewContext\s*\(',
    r'child_process\.exec\s*\(\s*[^"\']',
    r'child_process\.spawn\s*\(\s*[^"\']',
]

# Path traversal (Node.js)
PATH_TRAVERSAL = [
    r'fs\.readFile\s*\(\s*[^"\']+\+',
    r'fs\.writeFile\s*\(\s*[^"\']+\+',
    r'fs\.createReadStream\s*\(\s*[^"\']+\+',
    r'path\.join\s*\(\s*[^"\']',
    r'path\.resolve\s*\(\s*[^"\']',
]

# NoSQL injection (MongoDB)
NOSQL_INJECTION = [
    r'\$where\s*:',
    r'\$regex\s*:',
    r'\$ne\s*:',
    r'\$gt\s*:.*req\.',
    r'\$expr\s*:',
    r'\.find\s*\(\s*\{[^}]*req\.',
    r'\.findOne\s*\(\s*\{[^}]*req\.',
    r'\.updateMany\s*\(\s*\{[^}]*req\.',
]

# SSRF
SSRF_PATTERNS = [
    r'fetch\s*\(\s*[^"\']+\+',
    r'axios\.get\s*\(\s*[^"\']+\+',
    r'request\s*\(\s*[^"\']+\+',
    r'http\.get\s*\(\s*[^"\']+\+',
    r'got\s*\(\s*[^"\']+\+',
    r'node-fetch\s*\(\s*[^"\']+\+',
]

# Unsafe deserialization
DESERIALIZE = [
    r'js-yaml\.load\s*\(',
    r'yaml\.load\s*\(',
    r'\.load\s*\(\s*[^,]+,\s*[^,]*\)',
    r'node-serialize\.unserialize',
    r'serialize-javascript.*eval',
    r'JSON\.parse\s*\([^)]*\)\.filter',
]

ALL_PATTERNS = {
    "xss_sink": XSS_SINKS,
    "prototype_pollution": PROTO_POLLUTION,
    "eval_injection": EVAL_PATTERNS,
    "path_traversal": PATH_TRAVERSAL,
    "nosql_injection": NOSQL_INJECTION,
    "ssrf": SSRF_PATTERNS,
    "unsafe_deserialization": DESERIALIZE,
}

# ─── Scanner ───


def scan_js_file(file_path: str) -> list[dict]:
    """Scan a JavaScript/TypeScript file for vulnerabilities."""
    findings: list[dict] = []
    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return findings

    lines = source.split("\n")

    # Pattern-based detection
    for vuln_type, patterns in ALL_PATTERNS.items():
        for pattern in patterns:
            for i, line in enumerate(lines, 1):
                if re.search(pattern, line, re.IGNORECASE):
                    # Skip comments and test files
                    stripped = line.strip()
                    if stripped.startswith("//") or stripped.startswith("/*"):
                        continue
                    if stripped.startswith("*"):
                        continue

                    findings.append({
                        "type": vuln_type,
                        "file": file_path,
                        "line": i,
                        "code": stripped[:200],
                        "severity": VULN_DEFS[vuln_type]["severity"],
                        "cwe": VULN_DEFS[vuln_type]["cwe"],
                        "detail": VULN_DEFS[vuln_type]["desc"],
                        "pattern": pattern[:60],
                    })
                    break  # One finding per pattern per file

    # CSP bypass detection: look for unsafe CSP directives
    for i, line in enumerate(lines, 1):
        if "Content-Security-Policy" in line:
            if "unsafe-eval" in line:
                findings.append({
                    "type": "csp_bypass",
                    "file": file_path, "line": i, "code": line.strip()[:200],
                    "severity": "MÉDIO", "cwe": "CWE-1021",
                    "detail": "CSP allows unsafe-eval — enables code injection",
                })
            if "unsafe-inline" in line:
                findings.append({
                    "type": "csp_bypass",
                    "file": file_path, "line": i, "code": line.strip()[:200],
                    "severity": "MÉDIO", "cwe": "CWE-1021",
                    "detail": "CSP allows unsafe-inline — enables XSS",
                })

    return findings


def scan_js_codebase(root: str, glob_pattern: str = "**/*.{js,ts,jsx,tsx,mjs,cjs}") -> dict:
    """Scan a JavaScript/TypeScript codebase for vulnerabilities."""
    from pathlib import Path
    base = Path(root)
    all_findings: list[dict] = []
    files_scanned = 0

    for ext in ["js", "ts", "jsx", "tsx", "mjs", "cjs"]:
        for file_path in sorted(base.glob(f"**/*.{ext}")):
            if any(p.startswith(".") for p in file_path.parts):
                continue
            if "test" in file_path.parts or file_path.name.endswith(".test.js"):
                continue
            if "node_modules" in file_path.parts:
                continue
            findings = scan_js_file(str(file_path))
            all_findings.extend(findings)
            files_scanned += 1

    by_type = defaultdict(int)
    by_severity = defaultdict(int)
    for f in all_findings:
        by_type[f["type"]] += 1
        by_severity[f["severity"]] += 1

    return {
        "ok": True,
        "language": "javascript",
        "files_scanned": files_scanned,
        "total_findings": len(all_findings),
        "by_type": dict(by_type),
        "by_severity": dict(by_severity),
        "findings": all_findings,
    }
