"""
Go static vulnerability analyzer.

Detects: goroutine race conditions, nil pointer dereference, unsafe pointer
usage, slice bounds issues, defer close leaks, hardcoded secrets, crypto
weaknesses (weak rand, broken hash), and command injection.
"""

import re
from collections import defaultdict
from pathlib import Path

# ─── Vulnerability definitions ───

VULN_DEFS = {
    "goroutine_race": {
        "severity": "ALTO", "cwe": "CWE-362",
        "desc": "Shared variable access in goroutine without synchronization",
    },
    "nil_deref": {
        "severity": "MÉDIO", "cwe": "CWE-476",
        "desc": "Potential nil pointer dereference",
    },
    "unsafe_pointer": {
        "severity": "CRÍTICO", "cwe": "CWE-823",
        "desc": "Use of unsafe.Pointer — bypasses Go type safety",
    },
    "slice_bounds": {
        "severity": "ALTO", "cwe": "CWE-129",
        "desc": "Slice/array access without bounds check",
    },
    "defer_leak": {
        "severity": "MÉDIO", "cwe": "CWE-404",
        "desc": "Resource not closed — defer missing or in loop",
    },
    "hardcoded_secret": {
        "severity": "ALTO", "cwe": "CWE-798",
        "desc": "Hardcoded credential, token, or key",
    },
    "command_injection": {
        "severity": "CRÍTICO", "cwe": "CWE-78",
        "desc": "OS command injection via user input",
    },
    "weak_random": {
        "severity": "MÉDIO", "cwe": "CWE-338",
        "desc": "Weak PRNG (math/rand) used for security-sensitive context",
    },
    "tls_skip_verify": {
        "severity": "ALTO", "cwe": "CWE-295",
        "desc": "TLS certificate verification disabled",
    },
}

# ─── Dangerous patterns ───

PATTERNS = {
    "goroutine_race": [
        r'go\s+func\s*\(\)\s*\{.*\b(\w+)\s*[+\-]=',  # closure captures var
        r'go\s+\w+\(.*&?\w+\b',  # passing pointer to goroutine
        r'var\s+\w+\s+\*?\w+\s*\n.*go\s+',  # shared var before goroutine
    ],
    "unsafe_pointer": [
        r'unsafe\.Pointer\s*\(',
        r'unsafe\.Sizeof\s*\(',
        r'unsafe\.Offsetof\s*\(',
        r'import\s+"unsafe"',
    ],
    "command_injection": [
        r'exec\.Command\s*\(\s*[^"\']+.*\+',  # dynamic command
        r'exec\.Command\s*\(\s*[^"\']+.*Sprintf',  # fmt.Sprintf in command
        r'os\.Exec\s*\(',
        r'syscall\.Exec\s*\(',
    ],
    "hardcoded_secret": [
        r'(?i)(password|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*"[^"]{8,}"',
        r'(?i)(password|secret|token|api[_-]?key)\s*=\s*`[^`]{8,}`',
    ],
    "weak_random": [
        r'math/rand',
        r'rand\.Intn?\s*\(',
        r'rand\.Float(32|64)?\s*\(',
        r'rand\.Read\s*\(',
    ],
    "tls_skip_verify": [
        r'InsecureSkipVerify\s*:\s*true',
        r'TLSClientConfig.*InsecureSkipVerify',
    ],
    "nil_deref": [
        r'if\s+err\s*!=\s*nil\s*\{[^}]*\}\s*\n\s*\w+\.\w+',  # deref after error ignoring
        r'\.\w+\s*\(\)\s*\n.*if.*==\s*nil',  # method call before nil check
    ],
    "defer_leak": [
        r'os\.Open\s*\([^)]+\)(?!.*defer.*Close)',  # open without close
        r'for\s+.*\{[^}]*os\.Open',  # open in loop without defer
    ],
    "slice_bounds": [
        r'\[(\w+)\s*:\s*(\w+)\]',  # slice with variables
        r'\.Slice\s*\(\s*\w+',  # unsafe.Slice without bounds
    ],
}

# ─── Scanner ───


def scan_go_file(file_path: str) -> list[dict]:
    """Scan a Go source file for vulnerabilities."""
    findings: list[dict] = []
    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return findings

    lines = source.split("\n")

    for vuln_type, patterns in PATTERNS.items():
        for pattern in patterns:
            for i, line in enumerate(lines, 1):
                if re.search(pattern, line):
                    stripped = line.strip()
                    if stripped.startswith("//"):
                        continue

                    # Special handling: weak_random only flags if crypto usage exists
                    if vuln_type == "weak_random":
                        if "crypto/rand" in source:
                            pass  # Has both — flag the math/rand usage
                        elif re.search(r'(?i)(token|session|password|auth|jwt|csrftoken)', source):
                            pass  # Security context detected
                        else:
                            continue  # Not security-sensitive, skip

                    findings.append({
                        "type": vuln_type,
                        "file": file_path,
                        "line": i,
                        "code": stripped[:200],
                        "severity": VULN_DEFS[vuln_type]["severity"],
                        "cwe": VULN_DEFS[vuln_type]["cwe"],
                        "detail": VULN_DEFS[vuln_type]["desc"],
                    })
                    break

    return findings


def scan_go_codebase(root: str, glob_pattern: str = "**/*.go") -> dict:
    """Scan a Go codebase for vulnerabilities."""
    base = Path(root)
    all_findings: list[dict] = []
    files_scanned = 0

    for file_path in sorted(base.glob(glob_pattern)):
        if any(p.startswith(".") for p in file_path.parts):
            continue
        if "test" in file_path.parts or file_path.name.endswith("_test.go"):
            continue
        findings = scan_go_file(str(file_path))
        all_findings.extend(findings)
        files_scanned += 1

    by_type = defaultdict(int)
    by_severity = defaultdict(int)
    for f in all_findings:
        by_type[f["type"]] += 1
        by_severity[f["severity"]] += 1

    return {
        "ok": True,
        "language": "go",
        "files_scanned": files_scanned,
        "total_findings": len(all_findings),
        "by_type": dict(by_type),
        "by_severity": dict(by_severity),
        "findings": all_findings,
    }
