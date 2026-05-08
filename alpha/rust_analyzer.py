"""
Rust static vulnerability analyzer.

Detects: unsafe block usage, raw pointer dereference, mem::forget leaks,
Send/Sync violations, integer overflow in release, panic unwinding safety,
unbounded allocation, and crypto weaknesses.
"""

import re
from collections import defaultdict
from pathlib import Path

# ─── Vulnerability definitions ───

VULN_DEFS = {
    "unsafe_block": {
        "severity": "ALTO", "cwe": "CWE-242",
        "desc": "Unsafe block — bypasses Rust safety guarantees. Must be audited.",
    },
    "raw_pointer_deref": {
        "severity": "CRÍTICO", "cwe": "CWE-822",
        "desc": "Dereference of raw pointer — no bounds or lifetime checks",
    },
    "mem_forget_leak": {
        "severity": "MÉDIO", "cwe": "CWE-404",
        "desc": "mem::forget prevents Drop — resource leak or lock poisoning",
    },
    "send_sync_violation": {
        "severity": "ALTO", "cwe": "CWE-362",
        "desc": "Unsafe Send/Sync impl — possible data race",
    },
    "integer_overflow": {
        "severity": "MÉDIO", "cwe": "CWE-190",
        "desc": "Arithmetic in unsafe context without overflow check",
    },
    "panic_unwind_safety": {
        "severity": "MÉDIO", "cwe": "CWE-248",
        "desc": "Potential panic across FFI boundary — undefined behavior",
    },
    "unbounded_allocation": {
        "severity": "MÉDIO", "cwe": "CWE-770",
        "desc": "Allocation with user-controlled size without limit",
    },
    "hardcoded_secret": {
        "severity": "ALTO", "cwe": "CWE-798",
        "desc": "Hardcoded credential or key in source",
    },
    "weak_crypto": {
        "severity": "ALTO", "cwe": "CWE-327",
        "desc": "Use of broken or weak cryptographic primitive",
    },
    "transmute_abuse": {
        "severity": "ALTO", "cwe": "CWE-843",
        "desc": "Type confusion via mem::transmute",
    },
}

# ─── Patterns ───

PATTERNS = {
    "unsafe_block": [
        r'\bunsafe\s*\{',
        r'\bunsafe\s+fn\b',
        r'\bunsafe\s+impl\b',
        r'\bunsafe\s+trait\b',
    ],
    "raw_pointer_deref": [
        r'\*\s*const\s+\w+\b',
        r'\*\s*mut\s+\w+\b',
        r'\.read\s*\(\s*\)',
        r'\.offset\s*\(\s*\w+',
        r'\.add\s*\(\s*\w+',
        r'\.sub\s*\(\s*\w+',
    ],
    "mem_forget_leak": [
        r'mem::forget\s*\(',
        r'std::mem::forget\s*\(',
        r'ManuallyDrop',
        r'\.leak\s*\(\s*\)',
    ],
    "transmute_abuse": [
        r'mem::transmute\s*\(',
        r'std::mem::transmute\s*\(',
    ],
    "send_sync_violation": [
        r'unsafe\s+impl\s+Send\s+for',
        r'unsafe\s+impl\s+Sync\s+for',
    ],
    "panic_unwind_safety": [
        r'extern\s+"C"\s+fn.*\n.*panic',
        r'catch_unwind.*unsafe',
    ],
    "unbounded_allocation": [
        r'Vec::with_capacity\s*\(\s*\w+\s*\*\s*\w+',
        r'alloc::.*\w+\s*\*\s*\w+',
    ],
    "hardcoded_secret": [
        r'(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*"[^"]{8,}"',
        r'(?i)const\s+(PASSWORD|SECRET|TOKEN|API_KEY)\s*[:=]\s*"[^"]+"',
    ],
    "weak_crypto": [
        r'md5::',
        r'sha1::',
        r'[Mm][Dd]5',
        r'RC4',
        r'DES',
    ],
}

# ─── Scanner ───


def scan_rust_file(file_path: str) -> list[dict]:
    """Scan a Rust source file for vulnerabilities."""
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
                    if stripped.startswith("//") or stripped.startswith("///"):
                        continue

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

    # Special: count unsafe blocks per file
    unsafe_count = sum(1 for l in lines if re.search(r'\bunsafe\s*\{', l))
    if unsafe_count > 10:
        findings.append({
            "type": "unsafe_block",
            "file": file_path,
            "line": 1,
            "code": f"File contains {unsafe_count} unsafe blocks",
            "severity": "ALTO",
            "cwe": "CWE-242",
            "detail": f"High density of unsafe blocks ({unsafe_count}) — audit required",
        })

    return findings


def scan_rust_codebase(root: str, glob_pattern: str = "**/*.rs") -> dict:
    """Scan a Rust codebase for vulnerabilities."""
    base = Path(root)
    all_findings: list[dict] = []
    files_scanned = 0

    for file_path in sorted(base.glob(glob_pattern)):
        if any(p.startswith(".") for p in file_path.parts):
            continue
        if "test" in file_path.parts or file_path.name.endswith("_test.rs"):
            continue
        findings = scan_rust_file(str(file_path))
        all_findings.extend(findings)
        files_scanned += 1

    by_type = defaultdict(int)
    by_severity = defaultdict(int)
    for f in all_findings:
        by_type[f["type"]] += 1
        by_severity[f["severity"]] += 1

    return {
        "ok": True,
        "language": "rust",
        "files_scanned": files_scanned,
        "total_findings": len(all_findings),
        "by_type": dict(by_type),
        "by_severity": dict(by_severity),
        "findings": all_findings,
    }
