"""Loader for `.alpha/mcp.json` — the MCP server configuration file.

Schema (Claude-Code compatible subset):

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": {"FOO": "bar"},
      "disabled": false
    },
    "linear": {
      "type": "http",
      "url": "https://mcp.linear.app/mcp",
      "headers": {"Authorization": "Bearer ${LINEAR_MCP_TOKEN}"}
    }
  }
}
```

Entradas com `url` usam o transporte Streamable HTTP (`http_client.py`);
`type` é opcional (aceito para compat com Claude Code). `headers` suporta
expansão `${VAR}` — o lugar certo para tokens (nunca na URL).

Environment-variable expansion: any string in `args`/`env` of the form
`${VAR}` is expanded from the parent process environment. Missing vars
fall through unchanged so the user sees what's wrong instead of a silent
empty value.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..secret_env import is_secret_env
from ..settings import find_config_file as _find_alpha_config
from ..settings import is_user_scoped_config, project_config_trusted, read_json

logger = logging.getLogger(__name__)

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


# AUDIT_V1.10 #014 + V2.3 #101: detecta secrets expandidos em argv/url.
# Decisão por NOME e por VALOR (connection string `user:senha@`, PEM) via
# heurística compartilhada — `${DATABASE_URL}` num arg é recusado em vez
# de expandido para /proc/PID/cmdline.
def _is_secret_ref(var: str) -> bool:
    return is_secret_env(var, os.environ.get(var))


@dataclass
class MCPServerConfig:
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    disabled: bool = False
    # Transporte Streamable HTTP: quando `url` está setada, `command`/`args`/
    # `env` são ignorados e a conexão vai via http_client.MCPHttpClient.
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # OAuth (http only): optional pre-registered client_id; `auth: "none"`
    # disables the OAuth path even without a static Authorization header.
    client_id: str = ""
    auth: str = ""

    @property
    def is_http(self) -> bool:
        return bool(self.url)


def _expand(value: str) -> str:
    def repl(m: re.Match) -> str:
        var = m.group(1)
        return os.environ.get(var, m.group(0))

    return _ENV_VAR_PATTERN.sub(repl, value)


def find_config_file() -> Path | None:
    return _find_alpha_config("mcp.json")


def load_mcp_config(path: Path | None = None) -> list[MCPServerConfig]:
    """Parse the MCP config file. Returns [] if no config is present."""
    if path is None:
        path = find_config_file()
    raw = read_json(path, default=None)
    if not isinstance(raw, dict):
        return []

    servers_block = raw.get("mcpServers") or raw.get("servers") or {}
    if not isinstance(servers_block, dict):
        logger.warning("MCP config at %s: 'mcpServers' must be an object", path)
        return []

    # #D305: connecting a server from project config runs attacker-controlled
    # behaviour at startup — a stdio server spawns `command` (Popen), and an
    # http server sends `${VAR}`-expanded headers (which may hold secrets) to
    # the config's url. A `.alpha/mcp.json` planted in an untrusted repo must NOT
    # do either just because `alpha` was launched there. Fail-closed — only
    # `~/.alpha/mcp.json` (or an explicit opt-in) may spawn/connect project
    # servers.
    project_trusted = is_user_scoped_config(path) or project_config_trusted()

    out: list[MCPServerConfig] = []
    for name, spec in servers_block.items():
        if not isinstance(spec, dict):
            logger.warning("MCP server '%s': entry must be an object, skipping", name)
            continue

        # ── Entrada HTTP (Streamable HTTP) ──
        url = spec.get("url")
        if isinstance(url, str) and url:
            if not project_trusted:
                logger.warning(
                    "MCP server '%s': http endpoint ignored — project-scoped %s "
                    "is untrusted (#D305, fail-closed). Set "
                    "ALPHA_TRUST_PROJECT_CONFIG=1 to opt in, or move it to "
                    "~/.alpha/mcp.json.",
                    name, path,
                )
                continue
            # Secrets pertencem a `headers`, nunca à URL: a URL vai para
            # logs e mensagens de erro. Recusamos ${VAR} de nome secreto.
            secret_var = next(
                (
                    var
                    for var in _ENV_VAR_PATTERN.findall(url)
                    if _is_secret_ref(var)
                ),
                None,
            )
            if secret_var is not None:
                logger.error(
                    "MCP server '%s': url referencia env var de nome secreto "
                    "${%s} — servidor NÃO iniciado (URLs aparecem em logs). "
                    "Mova o secret para 'headers'.",
                    name, secret_var,
                )
                continue
            headers_raw = spec.get("headers")
            if not isinstance(headers_raw, dict):
                if headers_raw is not None:
                    logger.warning(
                        "MCP server '%s': 'headers' must be an object, ignoring",
                        name,
                    )
                headers_raw = {}
            headers = {str(k): _expand(str(v)) for k, v in headers_raw.items()}
            out.append(
                MCPServerConfig(
                    name=name,
                    url=_expand(url),
                    headers=headers,
                    disabled=bool(spec.get("disabled", False)),
                    client_id=str(spec.get("client_id") or ""),
                    auth=str(spec.get("auth") or "").lower(),
                )
            )
            continue
        if (spec.get("type") or "").lower() in ("http", "sse", "streamable-http"):
            logger.warning(
                "MCP server '%s': type http sem 'url', skipping", name
            )
            continue

        # ── Entrada stdio ──
        command = spec.get("command")
        if not isinstance(command, str) or not command:
            logger.warning("MCP server '%s': missing 'command', skipping", name)
            continue

        if not project_trusted:
            logger.warning(
                "MCP server '%s': stdio command ignored — project-scoped %s is "
                "untrusted (#D305, fail-closed). Set ALPHA_TRUST_PROJECT_CONFIG=1 "
                "to opt in, or move the server to ~/.alpha/mcp.json.",
                name, path,
            )
            continue

        # AUDIT_V1.10 #014 / #013: args expandidos vão para /proc/PID/cmdline
        # (visível a qualquer processo). Secrets devem ir em `env`, não em
        # `args`. Antes só avisávamos — o secret já vazava. Agora RECUSAMOS o
        # servidor quando um arg referencia um env var de NOME secreto
        # (`${OPENAI_API_KEY}`, `${MY_TOKEN}`, …) — keying no NOME da var, não
        # no valor, então `tokens.json` (literal) ou `${HOME}/x` (não-secreto)
        # seguem funcionando normalmente.
        raw_args = spec.get("args", []) or []
        secret_ref = None
        for i, raw in enumerate(raw_args):
            for var in _ENV_VAR_PATTERN.findall(str(raw)):
                if _is_secret_ref(var):
                    secret_ref = (i, var)
                    break
            if secret_ref:
                break
        if secret_ref is not None:
            logger.error(
                "MCP server '%s': arg[%d] referencia env var de nome secreto "
                "${%s} — servidor NÃO iniciado (args vão para /proc/PID/cmdline, "
                "visível a qualquer processo). Mova o secret para 'env'.",
                name, secret_ref[0], secret_ref[1],
            )
            continue
        args = [_expand(str(a)) for a in raw_args]
        env_raw = spec.get("env")
        if not isinstance(env_raw, dict):
            if env_raw is not None:
                logger.warning(
                    "MCP server '%s': 'env' must be an object, ignoring", name
                )
            env_raw = {}
        env = {str(k): _expand(str(v)) for k, v in env_raw.items()}
        disabled = bool(spec.get("disabled", False))

        out.append(
            MCPServerConfig(
                name=name,
                command=command,
                args=args,
                env=env,
                disabled=disabled,
            )
        )
    return out
