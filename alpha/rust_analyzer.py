"""Rust vulnerability analyzer. See analyzer_base.BaseAnalyzer."""

from .analyzer_base import BaseAnalyzer, Sev


class RustAnalyzer(BaseAnalyzer):
    language = "rust"
    VULN_DEFS = {
        "unsafe_block":          {"severity": Sev.HIGH,     "cwe": "CWE-242", "desc": "Unsafe block — bypasses Rust safety guarantees"},
        "raw_pointer_deref":     {"severity": Sev.CRITICAL, "cwe": "CWE-822", "desc": "Dereference of raw pointer — no bounds/lifetime checks"},
        "mem_forget_leak":       {"severity": Sev.MEDIUM,   "cwe": "CWE-404", "desc": "mem::forget prevents Drop — resource leak or lock poisoning"},
        "send_sync_violation":   {"severity": Sev.HIGH,     "cwe": "CWE-362", "desc": "Unsafe Send/Sync impl — possible data race"},
        "integer_overflow":      {"severity": Sev.MEDIUM,   "cwe": "CWE-190", "desc": "Arithmetic in unsafe context without overflow check"},
        "panic_unwind_safety":   {"severity": Sev.MEDIUM,   "cwe": "CWE-248", "desc": "Potential panic across FFI boundary — undefined behavior"},
        "unbounded_allocation":  {"severity": Sev.MEDIUM,   "cwe": "CWE-770", "desc": "Allocation with user-controlled size without limit"},
        "hardcoded_secret":      {"severity": Sev.HIGH,     "cwe": "CWE-798", "desc": "Hardcoded credential or key in source"},
        "weak_crypto":           {"severity": Sev.HIGH,     "cwe": "CWE-327", "desc": "Use of broken or weak cryptographic primitive"},
        "transmute_abuse":       {"severity": Sev.HIGH,     "cwe": "CWE-843", "desc": "Type confusion via mem::transmute"},
    }

    PATTERNS = {
        "unsafe_block": [r'\bunsafe\s*\{', r'\bunsafe\s+fn\b', r'\bunsafe\s+impl\b', r'\bunsafe\s+trait\b'],
        "raw_pointer_deref": [r'\*\s*(const|mut)\s+\w+\b', r'\.read\s*\(\s*\)', r'\.offset\s*\(\s*\w+', r'\.add\s*\(\s*\w+'],
        "mem_forget_leak": [r'(std::)?mem::forget\s*\(', r'ManuallyDrop', r'\.leak\s*\(\s*\)'],
        "transmute_abuse": [r'(std::)?mem::transmute\s*\('],
        "send_sync_violation": [r'unsafe\s+impl\s+Send\s+for', r'unsafe\s+impl\s+Sync\s+for'],
        "panic_unwind_safety": [r'extern\s+"C"\s+fn.*\n.*panic', r'catch_unwind.*unsafe'],
        "unbounded_allocation": [r'Vec::with_capacity\s*\(\s*\w+\s*\*\s*\w+'],
        "hardcoded_secret": [
            r'(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*"[^"]{8,}"',
            r'(?i)const\s+(PASSWORD|SECRET|TOKEN|API_KEY)\s*[:=]\s*"[^"]+"',
        ],
        "weak_crypto": [r'md5::', r'sha1::', r'[Mm][Dd]5', r'RC4', r'DES'],
    }

    def _file_extensions(self) -> list[str]:
        return ["rs"]

    def scan_file(self, file_path: str) -> list[dict]:
        source = self._read_file(file_path)
        if source is None:
            return []
        lines = source.split("\n")
        findings = self._pattern_scan(source, file_path, self.PATTERNS, self.VULN_DEFS, lines)

        # High unsafe block density warning
        unsafe_count = sum(1 for l in lines if "unsafe" in l and "{" in l)
        if unsafe_count > 10:
            findings.append({
                "type": "unsafe_block", "file": file_path, "line": 1,
                "code": f"{unsafe_count} unsafe blocks in file",
                "severity": Sev.HIGH, "cwe": "CWE-242",
                "detail": "High density of unsafe blocks — audit required",
            })
        return findings


# Module-level API for backward compat
def scan_rust_file(file_path: str) -> list[dict]:
    return RustAnalyzer().scan_file(file_path)

def scan_rust_codebase(root: str) -> dict:
    return RustAnalyzer().scan_codebase(root)
