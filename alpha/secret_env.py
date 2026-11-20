"""Heurística compartilhada de detecção de credenciais em env vars.

AUDIT_V2.3 #101: a regex por-nome usada em `hooks.py` (#D118) e
`mcp/config.py` (#014) deixava passar connection strings
(`DATABASE_URL`, `REDIS_URL`, `MONGODB_URI`), chaves "bare"
(`STRIPE_KEY`, `PRIVATE_KEY`, `SIGNING_KEY`, `AWS_ACCESS_KEY_ID`)
e PATs (`GH_PAT`). Este módulo centraliza a decisão em
`is_secret_env(name, value)`:

- `_SECRET_NAME_RE` — fragmentos de NOME que indicam credencial.
  Deliberadamente NÃO inclui match genérico de `_url`/`_uri`
  (capturaria `BASE_URL`/`SERVER_URL`/`DOCS_URL` legítimas); cobre
  apenas os nomes de connection string conhecidos.
- `_SECRET_VALUE_RE` — formatos de VALOR inequívocos: credencial
  embutida em URL (`scheme://user:senha@host`) e chave privada PEM.
  Pega `${MINHA_VAR}` cujo valor é um DSN mesmo com nome inocente.

Nota de escopo: `alpha/tools/safe_env.py` (subprocessos de shell) usa
uma lista MAIS agressiva (remove qualquer `*_URL`) — trade-off aceitável
lá, onde over-removal não quebra config; aqui quebraria hooks e faria
`mcp/config.py` recusar `${SERVER_URL}` válidas.
"""

from __future__ import annotations

import re

_SECRET_NAME_RE = re.compile(
    r"(?:api[_-]?key|secret|token|password|passwd|credential|auth"
    r"|_key|database_url|db_url|redis_url|mongo(?:db)?_ur[il]"
    r"|connection_string|_dsn\b|_pat\b)",
    re.IGNORECASE,
)

_SECRET_VALUE_RE = re.compile(
    r"://[^/\s@]*:[^/\s@]+@"  # scheme://user:senha@host (user opcional)
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----",
)


def is_secret_env(name: str, value: str | None = None) -> bool:
    """True se a env var (por nome OU valor) aparenta carregar credencial."""
    if _SECRET_NAME_RE.search(name):
        return True
    if value and _SECRET_VALUE_RE.search(value):
        return True
    return False
