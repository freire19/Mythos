---
name: security-audit
description: Full-spectrum security audit of a codebase — SAST, dependency CVEs, misconfigurations, and manual code review.
metadata:
  {
    "alpha": {
      "emoji": "🔍",
      "requires": { "tools": ["scan_vulnerabilities", "audit_dependencies", "check_misconfigurations"] },
    },
  }
---

# Security Audit

Comprehensive security audit pipeline for any codebase. Uses Mythos's
built-in security tools plus manual code review.

## When to use

- "audit this project for vulnerabilities"
- "find security issues in this codebase"
- "run a security scan"
- "check for CVEs in my dependencies"
- "penetration test this application"
- "what's wrong with this code security-wise?"

## Pipeline

Run these steps in order:

### Phase 1 — Recon (2-3 min)

```
project_overview()
glob_files for source files
search_files for auth/input/crypto patterns
```

### Phase 2 — Automated scans (5-10 min)

```
scan_vulnerabilities(scanner="semgrep")   # SAST
audit_dependencies()                       # CVE check
check_misconfigurations()                  # config audit
```

### Phase 3 — Manual review (10-20 min)

Focus on these hotspots:
1. Authentication/authorization logic
2. Input validation (every request param, header, body)
3. SQL queries, ORM calls — injection surface
4. File operations (upload, download, path construction)
5. Cryptographic operations (key generation, storage, algorithm choice)
6. Session management and token handling
7. Error handlers and exception disclosure
8. Logging of sensitive data

### Phase 4 — Report

Use the destructive audit format:
- File path + line numbers
- Real code snippet
- Concrete attack vector
- Fix code (real code)
- Severity: CRÍTICO | ALTO | MÉDIO | BAIXO
- CWE mapping

End with:
- "O que NÃO auditei" section
- Plano de ação (Sprint imediato / Próximo sprint / Backlog)

## Tool selection matrix

| Language | SAST | Deps | Config |
|----------|------|------|--------|
| Python | bandit + semgrep | pip-audit | check_misconfigurations |
| JavaScript/TS | semgrep | npm audit | check_misconfigurations |
| Go | semgrep | govulncheck (via shell) | check_misconfigurations |
| Rust | semgrep | cargo-audit (via shell) | check_misconfigurations |
| Java/Kotlin | semgrep | mvn/grade deps (via shell) | check_misconfigurations |
