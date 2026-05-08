---
name: red-team-ops
description: Full red team operation — recon, weaponize, deliver, exploit, escalate, persist, exfiltrate. Complete attack lifecycle.
metadata:
  {
    "alpha": {
      "emoji": "⚔️",
      "requires": { "tools": ["nmap_scan", "ffuf_fuzz", "banner_grab", "payload_inject", "exploit_loop", "traffic_capture"] },
    },
  }
---

# Red Team Operations

Complete offensive operation lifecycle. Use this skill for any
full-scope attack engagement.

## When to use

- "pentest this target"
- "red team engagement on X"
- "attack this server"
- "break into this application"
- "full security assessment of X"
- "can you hack this?"

## Kill Chain

### Phase 1 — Reconnaissance

```
nmap_scan(target, scan_type="quick")           # Quick port scan first
nmap_scan(target, scan_type="full")            # Deep scan if needed
banner_grab on each open port                  # Service fingerprinting
ffuf_fuzz for directories, APIs, configs       # Web content discovery
web_search for each service + version + "CVE"  # Known exploit research
```

Output: attack surface map with services, versions, and known vulns.

### Phase 2 — Weaponization

For each confirmed vulnerability vector:
- SQLi: prepare UNION/boolean/time-based payload chains
- XSS: prepare reflected/stored/DOM payloads with exfiltration
- CMDi: prepare command injection payloads (;id → reverse shell)
- SSTI: prepare template injection with MRO escalation
- LFI: prepare path traversal with log poisoning for RCE

### Phase 3 — Delivery

```
exploit_loop(target_url, vector="sqli")     # Automated SQLi exploitation
payload_inject(target_url, vector="xss")     # Manual XSS probing
payload_inject(target_url, vector="cmdi")    # Command injection attempts
```

For each vector, run the exploitation loop until success or exhaustion.
Document every round.

### Phase 4 — Exploitation

Once initial access is achieved:
- SQLi → extract databases, tables, password hashes
- XSS → session theft, keylogging, browser exploitation
- CMDi → escalate to reverse shell
- LFI → source code extraction, config file reading

### Phase 5 — Privilege Escalation

```
execute_shell("sudo -l")                     # Sudo permissions
execute_shell("find / -perm -4000 2>/dev/null")  # SUID binaries
execute_shell("uname -a && cat /etc/os-release") # Kernel version
check writable cron, systemd timers, /etc/passwd
```

### Phase 6 — Persistence

```
write_file for: SSH authorized_keys, cron jobs, systemd services
execute_shell for: useradd, reverse shell listeners
```

### Phase 7 — Exfiltration

```
traffic_capture for credential harvesting
execute_shell for: tar + netcat data exfil
Compress and extract: databases, configs, source code, secrets
```

## Report Format

```markdown
# Red Team Report: <target>

## Executive Summary
- Access achieved: yes/no
- Highest privilege: user/root
- Critical findings: <count>

## Attack Timeline

### Recon
| Tool | Target | Result |

### Exploitation
| Round | Payload | Response | Adapted |

### Post-Exploitation
| Action | Result |

## Findings
| ID | Vuln | Severity | CWE | Impact |

## Remediation
| Finding | Fix | Priority |
```
