"""Go vulnerability analyzer. See analyzer_base.BaseAnalyzer."""

import re
from .analyzer_base import BaseAnalyzer, Sev


class GoAnalyzer(BaseAnalyzer):
    language = "go"
    VULN_DEFS = {
        "goroutine_race":     {"severity": Sev.HIGH,    "cwe": "CWE-362", "desc": "Shared variable access in goroutine without synchronization"},
        "nil_deref":          {"severity": Sev.MEDIUM,  "cwe": "CWE-476", "desc": "Potential nil pointer dereference"},
        "unsafe_pointer":     {"severity": Sev.CRITICAL,"cwe": "CWE-823", "desc": "Use of unsafe.Pointer — bypasses Go type safety"},
        "slice_bounds":       {"severity": Sev.HIGH,    "cwe": "CWE-129", "desc": "Slice/array access without bounds check"},
        "defer_leak":         {"severity": Sev.MEDIUM,  "cwe": "CWE-404", "desc": "Resource not closed — defer missing or in loop"},
        "hardcoded_secret":   {"severity": Sev.HIGH,    "cwe": "CWE-798", "desc": "Hardcoded credential, token, or key"},
        "command_injection":  {"severity": Sev.CRITICAL,"cwe": "CWE-78",  "desc": "OS command injection via user input"},
        "weak_random":        {"severity": Sev.MEDIUM,  "cwe": "CWE-338", "desc": "Weak PRNG (math/rand) in security context"},
        "tls_skip_verify":    {"severity": Sev.HIGH,    "cwe": "CWE-295", "desc": "TLS certificate verification disabled"},
    }

    PATTERNS = {
        "goroutine_race": [
            r'go\s+func\s*\(\)\s*\{.*\b(\w+)\s*[+\-]=',
            r'go\s+\w+\(.*&?\w+\b',
        ],
        "unsafe_pointer": [r'unsafe\.Pointer\s*\(', r'import\s+"unsafe"'],
        "command_injection": [
            r'exec\.Command\s*\(\s*[^"\']+.*\+',
            r'exec\.Command\s*\(\s*[^"\']+.*Sprintf',
        ],
        "hardcoded_secret": [
            r'(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*"[^"]{8,}"',
            r'(?i)(password|secret|token)\s*=\s*`[^`]{8,}`',
        ],
        "weak_random": [r'math/rand', r'rand\.Intn?\s*\(', r'rand\.Read\s*\('],
        "tls_skip_verify": [r'InsecureSkipVerify\s*:\s*true'],
        "nil_deref": [r'if\s+err\s*!=\s*nil\s*\{[^}]*\}\s*\n\s*\w+\.\w+'],
        "defer_leak": [r'os\.Open\s*\([^)]+\)(?!.*defer.*Close)'],
        "slice_bounds": [r'\[(\w+)\s*:\s*(\w+)\]', r'\.Slice\s*\(\s*\w+'],
    }

    def _file_extensions(self) -> list[str]:
        return ["go"]

    def scan_file(self, file_path: str) -> list[dict]:
        source = self._read_file(file_path)
        if source is None:
            return []
        lines = source.split("\n")

        # Filter weak_random: only flag when used in security-sensitive context
        patterns_to_use = dict(self.PATTERNS)
        if "weak_random" in patterns_to_use and not self._is_security_context(source):
            del patterns_to_use["weak_random"]

        findings = self._pattern_scan(source, file_path, patterns_to_use, self.VULN_DEFS, lines)
        return findings

    @staticmethod
    def _is_security_context(source: str) -> bool:
        return bool(re.search(r'(?i)(token|session|password|auth|jwt|csrftoken)', source))


# Module-level API for backward compat
def scan_go_file(file_path: str) -> list[dict]:
    return GoAnalyzer().scan_file(file_path)

def scan_go_codebase(root: str) -> dict:
    return GoAnalyzer().scan_codebase(root)
