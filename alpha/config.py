"""Configuration hub for Alpha Code.

Central source of truth for everything that controls agent behavior:

- `_PROVIDERS` — registry of LLM providers (DeepSeek, OpenAI, Anthropic,
  Grok, Gemini, Ollama). Each entry has base_url, model, API key env var,
  api_format ("openai" | "anthropic"), supports_tools flag, low_temperature.
- `LIMITS` — runtime caps (max_iterations, llm_timeout, token budgets).
- `FEATURES` — feature flags (multi_agent_enabled, delegate_tool_enabled,
  etc.) consumed via getters so env overrides take effect mid-session.
- `TOOL_TIMEOUTS` / `TOOL_TIMEOUT_CAPS` (#D003) — three-layer timeout
  system: per-category default, per-category hard cap, executor-level
  default+slow tools. Tuning happens here, never inline.
- `RETRY` — backoff config for LLM streaming retry loop.
- `LOOP_DETECTION` — thresholds for the agent's loop detector (#085).
- `LOG_TRUNCATION` — display truncation limits (#D010).

Project root discovery (`_PROJECT_ROOT`) is computed at import time by
walking up from this file. .env loading happens at module load with
override semantics (project .env wins over ~/.alpha/.env).

Runtime env vars are read via getters (`get_subagent_policy()`,
`get_subagent_allow()`, etc.) rather than cached at import — AUDIT_V1.2
#014 showed cached values miss runtime mutations.

What's NOT here:
- Provider implementations → `alpha.providers/`
- Workspace/path config → `alpha.tools.workspace`
- Hook config → `.alpha/settings.json` via `alpha.settings`
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# #DM072: nome público para os consumidores cross-module (settings, wizard,
# agents/skills registries); o nome antigo segue válido por retrocompat.
PROJECT_ROOT = _PROJECT_ROOT


def _dotenv_search_paths() -> list[Path]:
    """Return .env paths in load order (later wins via override=True).

    A pipx-installed user typically has no _PROJECT_ROOT/.env (it lives in
    site-packages); ~/.alpha/.env is the recommended location for them.
    """
    return [
        _PROJECT_ROOT / ".env",        # dev install (git clone + pip install -e .)
        Path.home() / ".alpha" / ".env",  # per-user global (pipx default)
        Path.cwd() / ".env",           # project-local override (closest to invocation)
    ]


# #D306: a `.env` planted in an untrusted CWD must not override the provider
# base_url (→ the real api_key would be sent to the attacker's host) nor Alpha's
# security flags. When loading a project-local `.env` that is NOT the trusted
# project-root / ~/.alpha file, drop any key whose NAME is security-sensitive.
# (`load_dotenv(override=False)` is NOT enough: the common case is the var being
# absent — the user relies on the default — so the malicious value would still
# be set.)
_UNTRUSTED_ENV_BLOCK_RE = re.compile(r"(?:_BASE_URL$|_API_KEY$|^ALPHA_)", re.IGNORECASE)


def _trusted_env_paths() -> set[Path]:
    """.env locations trusted to set anything (the user's own): project-root
    (`<repo>/.env`, present only on a dev/editable install) and `~/.alpha/.env`."""
    out: set[Path] = set()
    for p in (_PROJECT_ROOT / ".env", Path.home() / ".alpha" / ".env"):
        try:
            out.add(p.resolve())
        except OSError:
            pass
    return out


def _load_untrusted_env(path: Path) -> None:
    """Load a project-local `.env` from an untrusted CWD, dropping keys whose
    name is security-sensitive (#D306)."""
    from dotenv import dotenv_values

    blocked: list[str] = []
    for key, val in dotenv_values(path).items():
        if val is None:
            continue
        if _UNTRUSTED_ENV_BLOCK_RE.search(key):
            blocked.append(key)
            continue
        os.environ[key] = val
    if blocked:
        logging.getLogger(__name__).warning(
            "Ignorando %d var(s) sensível(is) de %s (config de projeto não-"
            "confiável, #D306): %s. Mova para ~/.alpha/.env se forem legítimas.",
            len(blocked), path, ", ".join(sorted(blocked)),
        )


def _load_env_files() -> list[Path]:
    """Load every existing .env in the discovery chain. Returns paths loaded.

    Trusted files (`<project_root>/.env`, `~/.alpha/.env`) load fully. A
    project-local CWD `.env` that is NOT one of those loads with
    security-sensitive keys stripped (#D306)."""
    loaded: list[Path] = []
    trusted = _trusted_env_paths()
    for path in _dotenv_search_paths():
        if not path.is_file():
            continue
        try:
            is_trusted = path.resolve() in trusted
        except OSError:
            is_trusted = False
        if is_trusted:
            load_dotenv(path, override=True)
        else:
            _load_untrusted_env(path)
        loaded.append(path)
    return loaded


# 12-factor: env vars are the source of truth; .env files only fill gaps.
_load_env_files()

# ─── Defaults ───

DEFAULT_PROVIDER = os.getenv("ALPHA_PROVIDER", "deepseek")

# #097 V1.1: limites de execucao agrupados em LIMITS para inspecao
# uniforme. Chaves duplicadas no module-level (`MAX_ITERATIONS` etc)
# ficam como aliases com retro-compat. Sub-agent limit tambem entra
# aqui em vez de viver isolado em FEATURES.
def _env_int_clamped(name: str, default: int, *, min_value: int = 1) -> int:
    """Parse an int env var with safe fallback + clamp.

    Returns ``default`` on missing/empty/non-numeric/out-of-range input.
    CODE_REVIEW_FASE1 #3: raw ``int(os.getenv(...))`` at module-load
    crashed the CLI on a single typo (``ALPHA_MAX_ITERATIONS=abc``) or
    silently degraded the agent loop on ``0``/``-5``. Clamp keeps the
    process running with a logged warning.
    """
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        import logging
        logging.getLogger(__name__).warning(
            "%s=%r is not an integer — using default %d", name, raw, default,
        )
        return default
    if v < min_value:
        import logging
        logging.getLogger(__name__).warning(
            "%s=%d is below minimum %d — clamping to %d", name, v, min_value, min_value,
        )
        return min_value
    return v


def _env_float_clamped(name: str, default: float, *, min_value: float = 1.0) -> float:
    """Parse a float env var with safe fallback + clamp. Rejects nan/inf
    (CODE_REVIEW_FASE1 #3 — nan made the wall-clock guard silently
    inert; inf crashed downstream ``int(MAX_TURN_SECONDS)`` calls)."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        import logging
        logging.getLogger(__name__).warning(
            "%s=%r is not a number — using default %s", name, raw, default,
        )
        return default
    import math
    if not math.isfinite(v):
        import logging
        logging.getLogger(__name__).warning(
            "%s=%r is not finite — using default %s", name, raw, default,
        )
        return default
    if v < min_value:
        import logging
        logging.getLogger(__name__).warning(
            "%s=%s is below minimum %s — clamping to %s", name, v, min_value, min_value,
        )
        return min_value
    return v


LIMITS = {
    # Iteration cap per turn. Restored 30 → 100 (the Mythos / pre-V1.8
    # value). The V1.8 "tighter cap + [CONTINUE:] auto-continue" redesign
    # assumed the model would emit a `[CONTINUE: ...]` marker to chain many
    # short turns — but real providers (e.g. DeepSeek) rarely do, so a model
    # that legitimately needs >30 tool calls got guillotined mid-task and
    # never produced a summary. 100 iterations in ONE continuous turn lets
    # long tasks (audits, multi-file refactors) run to natural completion,
    # the way they did before. The cap is a safety net, not a chopping block:
    # if it IS hit, the loop now forces a final summary (see run_agent) so the
    # user always gets an answer instead of an empty turn or a silent restart.
    "max_iterations": _env_int_clamped("ALPHA_MAX_ITERATIONS", 100),
    # Wall-clock budget per turn (seconds). Raised to 1h so long but
    # productive turns (audits with delegate_parallel, slow providers) finish
    # in one go instead of being guillotined by the old 5-/20-min caps; when
    # exceeded the loop forces one final no-tools summary (run_agent) so the
    # user always gets a response. Honest scope: this is a HIGH CEILING, not a
    # tight safety net — a stuck-but-not-looping model can run up to
    # min(max_iterations × per-call time, this) before the cap fires, and a
    # mid-call Ctrl+C resolves only after the in-flight LLM call's `llm_timeout`.
    # Loop detection (not this cap) is what catches actual spinning. Lower
    # ALPHA_MAX_TURN_SECONDS if you want a tighter bound on time-to-control.
    "max_turn_seconds": _env_float_clamped("ALPHA_MAX_TURN_SECONDS", 3600.0),
    "tool_result_max_chars": 12_000,
    "llm_timeout": 300,             # seconds per LLM call
    "max_messages": 500,            # hard cap antes de needs_compression
}

MAX_ITERATIONS = LIMITS["max_iterations"]
MAX_TURN_SECONDS = LIMITS["max_turn_seconds"]
TOOL_RESULT_MAX_CHARS = LIMITS["tool_result_max_chars"]
LLM_TIMEOUT = LIMITS["llm_timeout"]

# #P004: connection pool limits compartilhados por todos os clientes
# HTTP do LLM. Conservador: agent loop tipico nao paraleliza alem de
# 3-5 LLM calls (delegate_parallel), e providers upstream rate-limitam
# bem antes de 20 keepalive.
HTTPX_LIMITS_LLM = {
    "max_keepalive_connections": 5,
    "max_connections": 20,
}

# Retry config centralizado (#DM036). LLM e HTTP usam backoff exponencial
# com jitter, mas com parametros diferentes (LLM calls sao mais caras e
# toleram retry mais agressivo; HTTP safe-methods podem retentar erros
# transientes sem duplicar efeito).
RETRY = {
    "llm": {
        "max_retries": 3,
        "initial_backoff": 1.0,
        "max_backoff": 30.0,
        "backoff_multiplier": 2.0,
        "retryable_status_codes": frozenset({429, 500, 502, 503, 504}),
    },
    "http": {
        "max_retries": 2,
        "initial_backoff": 0.5,
        "max_backoff": 8.0,  # #032: explicit cap on the exponential backoff
        "safe_methods": frozenset({"GET", "HEAD", "OPTIONS"}),
    },
}

# Loop detection (#085 V1.1): consts agrupadas em um dict para inspecao
# uniforme (ex: ALPHA_LOOP_DETECT_DISABLE=1 pode levantar todos os
# thresholds; testes podem mockar uma chave so). Lidas em alpha/agent.py.
LOOP_DETECTION = {
    "max_repeat_calls": 3,        # exact same call N times → loop
    "similar_repeat_calls": 5,    # similar calls threshold (false-positive guard)
    "similarity_threshold": 0.92, # fuzzy match threshold
    "cycle_window": 20,           # look-back window for cycle detection
    "stale_window": 6,            # last N tool calls com no new info → stale
    "min_iter": 3,                # iter <= N nao rodam detection (exploration)
    "min_calls": 6,               # gate loop detection by call count, not iter (#018)
}

# ─── Provider configs ───

_PROVIDERS = {
    "deepseek": {
        "base_url": os.getenv("DEEPSEEK_API_BASE_URL", "https://api.deepseek.com"),
        "api_key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_MODEL",
        "default_model": "deepseek-v4-pro",
        # Vision: disponivel APENAS no chat web (chat.deepseek.com) como
        # beta fechado (Image Recognition Mode, Apr 2026). A API REST
        # NAO aceita image_url blocks — retorna HTTP 400.
        "supports_vision": False,
    },
    "openai": {
        "base_url": os.getenv("OPENAI_API_BASE_URL", "https://api.openai.com/v1"),
        "api_key_env": "OPENAI_API_KEY",
        "model_env": "OPENAI_MODEL",
        "default_model": "gpt-4o",
        "supports_vision": True,
    },
    "anthropic": {
        "base_url": os.getenv("ANTHROPIC_API_BASE_URL", "https://api.anthropic.com/v1"),
        "api_key_env": "ANTHROPIC_API_KEY",
        "model_env": "ANTHROPIC_MODEL",
        "default_model": "claude-sonnet-4-6",
        "api_format": "anthropic",
        "supports_vision": True,
    },
    "google": {
        "base_url": os.getenv(
            "GEMINI_API_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        "api_key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "default_model": "gemini-3.1-pro-preview-customtools",  # base 3.1-pro filtra as proprias tool calls como MALFORMED; a variante -customtools e a oficial pra function calling.
        "supports_vision": True,
        "vision_format": "openai",
    },
    "antigravity": {
        "base_url": os.getenv(
            "GEMINI_API_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        "api_key_env": "GEMINI_API_KEY",
        "model_env": "ANTIGRAVITY_MODEL",
        "default_model": "antigravity-preview-05-2026",
        "supports_vision": True,
        "vision_format": "openai",
        "supports_tools": False,
    },
    "grok": {
        "base_url": os.getenv("GROK_API_BASE_URL", "https://api.x.ai/v1"),
        "api_key_env": "GROK_API_KEY",
        "model_env": "GROK_MODEL",
        "default_model": "grok-4-1-fast-reasoning",
        "supports_vision": False,
    },
    "ollama": {
        "base_url": os.getenv("OLLAMA_API_BASE_URL", "http://localhost:11434/v1"),
        "api_key_env": None,
        "model_env": "OLLAMA_MODEL",
        "default_model": "qwen-heavy-abliterated:32b",
        "low_temperature": True,
        "supports_vision": False,
    },
    "gemma-12b": {
        "base_url": os.getenv("OLLAMA_API_BASE_URL", "http://localhost:11434/v1"),
        "api_key_env": None,
        "model_env": "OLLAMA_GEMMA_12B_MODEL",
        "default_model": "gemma3:12b",
        "supports_tools": False,
        "low_temperature": True,
        "supports_vision": False,
    },
    "gemma-27b": {
        "base_url": os.getenv("OLLAMA_API_BASE_URL", "http://localhost:11434/v1"),
        "api_key_env": None,
        "model_env": "OLLAMA_GEMMA_27B_MODEL",
        "default_model": "gemma3:27b",
        "supports_tools": False,
        "low_temperature": True,
        "supports_vision": False,
    },
}


# ── Provider specs + per-function routing (parity wave 2026-11, item 8) ──
#
# A provider "spec" is either a plain provider name ("anthropic") or
# "<provider>:<model>" ("anthropic:claude-haiku-4-5") — the latter runs the
# same adapter with a model override. Specs let one function of the agent
# (context compression, sub-agents) use a cheaper/faster model than the main
# loop without touching the provider table.

_ROUTING_ENV = {
    "compress": "ALPHA_COMPRESS_PROVIDER",
    "subagent": "ALPHA_SUBAGENT_PROVIDER",
}


def split_provider_spec(spec: str) -> tuple[str, str | None]:
    """'anthropic:claude-haiku-4-5' → ('anthropic', 'claude-haiku-4-5');
    'anthropic' → ('anthropic', None)."""
    spec = (spec or "").strip()
    if ":" not in spec:
        return spec, None
    base, model = spec.split(":", 1)
    base, model = base.strip(), model.strip()
    return base, (model or None)


def base_provider(spec: str) -> str:
    """Provider name without a model override — the key for everything that
    is keyed by provider (rate limits, context limits, vision support)."""
    return split_provider_spec(spec)[0]


def route_provider(function: str, fallback: str) -> str:
    """Provider spec to use for `function` ("compress" | "subagent").

    Reads `ALPHA_<FUNCTION>_PROVIDER`; unset/empty → `fallback` (the active
    provider). An unknown base provider is logged and ignored so a typo
    can't take the main loop down mid-session."""
    env = _ROUTING_ENV.get(function)
    raw = os.environ.get(env, "").strip() if env else ""
    if not raw:
        return fallback
    base = base_provider(raw)
    if base not in _PROVIDERS:
        import logging
        logging.getLogger(__name__).warning(
            "%s=%r: provider %r desconhecido — usando %r", env, raw, base, fallback,
        )
        return fallback
    return raw


def get_provider_config(provider: str) -> dict:
    """
    Return config for a provider or provider spec (`name` or `name:model`).

    Returns dict with keys: base_url, api_key, model.
    Raises RuntimeError if API key is missing (except ollama).
    """
    provider, model_override = split_provider_spec(provider)
    cfg = _PROVIDERS.get(provider)
    if not cfg:
        raise RuntimeError(
            f"Provider desconhecido: {provider}. Disponíveis: {list(_PROVIDERS)}"
        )

    base_url = cfg["base_url"]

    if cfg["api_key_env"]:
        api_key = os.getenv(cfg["api_key_env"], "")
        if not api_key:
            raise RuntimeError(
                f"API key not set for {provider}. "
                f"Set the environment variable {cfg['api_key_env']}"
            )
    else:
        api_key = "ollama"

    model = model_override or os.getenv(cfg.get("model_env", ""), cfg["default_model"])
    supports_tools = cfg.get("supports_tools", True)
    api_format = cfg.get("api_format", "openai")
    supports_vision = cfg.get("supports_vision", False)
    vision_format = cfg.get("vision_format", "openai")
    extra_payload = _build_extra_payload(provider, model)

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "supports_tools": supports_tools,
        "api_format": api_format,
        "supports_vision": supports_vision,
        "vision_format": vision_format,
        "extra_payload": extra_payload,
    }


def _build_extra_payload(provider: str, model: str) -> dict:
    """Provider-specific payload extras (thinking mode, reasoning effort).

    DeepSeek V4 (Pro/Flash) accepts `thinking` and `reasoning_effort` in the
    OpenAI-compat payload. For agentic CLIs DeepSeek recommends `effort=max`
    (their docs list Claude Code / OpenCode as examples). Older v3 models
    (`deepseek-chat`, `deepseek-reasoner`) reject these fields, so we gate
    on the model name containing `v4`.

    Env overrides:
      - DEEPSEEK_THINKING:        "enabled" (default) | "disabled"
      - DEEPSEEK_REASONING_EFFORT: "max" (default) | "high" | "medium" | "low"

    #D105: os valores de env sao validados contra um whitelist e caem no
    # default quando invalidos. Sem isso, um typo (`enable`, `maximum`) era
    # passado cru ao payload e a API rejeitava com HTTP 400 em TODA tentativa
    # de retry — quebrando o provider por uma variavel de ambiente mal escrita.
    """
    if provider == "deepseek" and "v4" in model.lower():
        thinking = _validated_env(
            "DEEPSEEK_THINKING", "enabled", {"enabled", "disabled"}
        )
        effort = _validated_env(
            "DEEPSEEK_REASONING_EFFORT", "max", {"max", "high", "medium", "low"}
        )
        extras: dict = {"thinking": {"type": thinking}}
        if thinking == "enabled":
            extras["reasoning_effort"] = effort
        return extras
    return {}


def _validated_env(name: str, default: str, allowed: set[str]) -> str:
    """Read an env override, lowercased, falling back to `default` (with a
    warning) when the value is outside `allowed`. Prevents a typo'd env var
    from being passed verbatim into the provider payload (#D105)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val not in allowed:
        import logging
        logging.getLogger(__name__).warning(
            "%s=%r invalido (esperado um de %s); usando default %r",
            name, raw, sorted(allowed), default,
        )
        return default
    return val


def get_available_providers() -> list[dict]:
    """List all providers with their availability status."""
    result = []
    for name, cfg in _PROVIDERS.items():
        available = True
        if cfg["api_key_env"]:
            available = bool(os.getenv(cfg["api_key_env"], ""))
        model = os.getenv(cfg.get("model_env", ""), cfg["default_model"])
        result.append({
            "id": name,
            "model": model,
            "available": available,
            "supports_tools": cfg.get("supports_tools", True),
        })
    return result


def load_system_prompt() -> str:
    """Load the system prompt from alpha/prompts/system.md (bundled)."""
    from ._resources import package_data
    try:
        return package_data("prompts", "system.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        return (
            "You are ALPHA, an autonomous terminal agent. "
            "Execute tasks directly using your tools. Be concise and effective."
        )


# ─── Agent Workspace ───
# Fonte canonical: `alpha.tools.workspace.AGENT_WORKSPACE` (Path resolvida
# com forbidden-system-dir guard). A versao string-vazia que vivia aqui
# era bug latente (#D021-BUGS) — qualquer `from .config import AGENT_WORKSPACE`
# pegava uma string falsy enquanto `from .tools.workspace import ...` pegava
# o Path real. Importe direto de `alpha.tools.workspace`.

# ─── Feature Flags (used by tool modules) ───
# Toda chave aqui DEVE ser lida em algum lugar. Flags orfas viram codigo
# morto que confunde quem tenta desabilitar comportamento via env e nao
# ve efeito (#DL016).
def _subagent_budget_seconds() -> float:
    """Wall-clock budget of ONE in-process sub-agent (`ALPHA_SUBAGENT_MAX_SECONDS`).

    Default 120s, floor 30s. Raise it for slow/thinking providers — with
    DeepSeek all three `/review --deep` reviewers timed out at 120s."""
    return float(_env_int_clamped("ALPHA_SUBAGENT_MAX_SECONDS", 120, min_value=30))


FEATURES: dict = {
    # Master switch para o sistema de sub-agents. Quando False, `delegate_task`
    # e `delegate_parallel` retornam erro mesmo que `delegate_tool_enabled=True`.
    # Lido em `delegate_tools._delegate_task` / `_delegate_parallel`.
    "multi_agent_enabled": True,
    # Gate fino sobre as tools de delegate (independente do master switch).
    "delegate_tool_enabled": True,
    "max_parallel_agents": 3,
    "subagent_max_iterations": 15,
    # Wall-clock budget for ONE sub-agent (seconds). Independent from
    # `max_turn_seconds` (parent cap). Tighter — sub-agents should do
    # focused work; if they spin for >2 min we want the parent to know
    # quickly so it can recover or report the gap. CODE_REVIEW_FASE1 #7.
    # `ALPHA_SUBAGENT_MAX_SECONDS` raises it for slow/thinking providers
    # (seen with DeepSeek on `/review --deep`: 3 reviewers timed out at 120s).
    "subagent_max_turn_seconds": _subagent_budget_seconds(),
    # Cap total de tarefas em delegate_parallel — `max_parallel_agents`
    # controla concorrencia, nao total. Sem cap, o modelo pode submeter
    # array de 100 tasks * 15 iteracoes = 1500 chamadas LLM silenciosas.
    "max_delegate_total_tasks": 10,
    # subagent_policy / subagent_extra_block / subagent_allow: nao mais aqui.
    # AUDIT_V1.2 #014: o codigo antigo lia `os.environ.get(...)` no IMPORT do
    # modulo, congelando a flag. Mudancas runtime em os.environ (ex: hook
    # configurando policy, teste com monkeypatch) nao surtiam efeito ate
    # reload. Vide getters abaixo (`get_subagent_policy()` etc) — `delegate_tools`
    # consulta a env a cada call em vez de cache stale.
}


# AUDIT_V1.2 #014: getters runtime para flags dependentes de env. Ler env a
# cada call e barato (poucas chamadas por turn) e elimina a categoria de
# bug "mudou env mas nada mudou ate restart". Mantemos a leitura confinada
# a estes 3 helpers em vez de espalhar `os.environ.get(...)` pelo codebase.

def get_subagent_policy() -> str:
    """Politica de bloqueio do sub-agent (strict | relaxed).

    `strict` (default): bloqueia destructive tools quando nao ha approval
    callback do parent. `relaxed`: confia no sub-agent — so delegate_*
    bloqueado (anti-recursao). Lido a cada call para refletir mudancas
    em runtime (hooks, testes, scripts).
    """
    return os.environ.get("ALPHA_SUBAGENT_POLICY", "strict")


def get_subagent_extra_block() -> frozenset[str]:
    """Tools extras a bloquear no sub-agent alem de SUBAGENT_DESTRUCTIVE_BLOCKLIST.

    Lido de `ALPHA_SUBAGENT_EXTRA_BLOCK` (comma-separated). Ex: bloquear
    write_file e edit_file em sub-agents — `ALPHA_SUBAGENT_EXTRA_BLOCK=write_file,edit_file`.
    """
    raw = os.environ.get("ALPHA_SUBAGENT_EXTRA_BLOCK", "")
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def get_subagent_allow() -> frozenset[str]:
    """Tools que o sub-agent pode usar mesmo estando na blocklist default.

    Lido de `ALPHA_SUBAGENT_ALLOW`. Sobrepoe o default — usar com cuidado.
    `delegate_*` continua bloqueado mesmo se o usuario incluir aqui (anti-recursao
    e invariante, nao policy).
    """
    raw = os.environ.get("ALPHA_SUBAGENT_ALLOW", "")
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


# ─── Tool-family gating (prompt-surface reduction) ───
#
# Advertising tools that can't run just inflates the prompt (and the model's
# choice space) every turn. Browser (21 tools) needs Playwright; Apify (2
# tools) needs an API token — without those, the family is dead weight. These
# tri-state flags default to "auto" (register iff the dependency/config is
# present) and can be forced on/off. Read at call time so runtime env changes
# take effect. The tools stay in the registry either way — this only controls
# what `get_openai_tools()` advertises to the model.

# #DM061: vocabulário canônico de env-boolean — ÚNICA fonte da verdade.
# Antes cada módulo redefinia o próprio set (jsonlogs sem "on"/strip,
# llm_anthropic sem "off", checkpoints só o lado falsy), então a grafia
# aceita variava flag a flag. Novos flags devem usar `env_bool`.
_ENV_TRUE = ("1", "true", "yes", "on")
_ENV_FALSE = ("0", "false", "no", "off")


def env_bool(env_var: str, default: bool = False) -> bool:
    """Parse an env var as a boolean using the canonical vocabulary.

    '1'/'true'/'yes'/'on' → True; '0'/'false'/'no'/'off' → False
    (case-insensitive, whitespace-stripped). Unset — or any other
    value — returns `default`.
    """
    raw = os.environ.get(env_var, "").strip().lower()
    if raw in _ENV_TRUE:
        return True
    if raw in _ENV_FALSE:
        return False
    return default


def _tristate_flag(env_var: str, *, auto_default: bool) -> bool:
    """Resolve an auto/on/off feature flag.

    unset or 'auto' → `auto_default`; '1'/'true'/'yes'/'on' → True;
    '0'/'false'/'no'/'off' → False.
    """
    return env_bool(env_var, default=auto_default)


def lazy_tools_enabled() -> bool:
    """Tool lazy-loading (`alpha.tool_lazy`): heavy/rare tool families are
    dropped from the prompt and fetched via `tool_search`. Auto = on.
    `ALPHA_LAZY_TOOLS=off` restores the full-schema prompt."""
    return _tristate_flag("ALPHA_LAZY_TOOLS", auto_default=True)


def notify_mode() -> str:
    """Desktop notifications for long REPL turns / approval prompts
    (`alpha.notify`): `ALPHA_NOTIFY=auto|on|off`, default auto (on when
    stdout is a TTY and a backend exists)."""
    return _validated_env("ALPHA_NOTIFY", "auto", {"auto", "on", "off"})


def notify_after_seconds() -> int:
    """Only turns at least this long notify (`ALPHA_NOTIFY_AFTER`, default
    20s; 0 = every turn)."""
    return _env_int_clamped("ALPHA_NOTIFY_AFTER", 20, min_value=0)


def lazy_mcp_threshold() -> int:
    """MCP tools are deferred only when the registry holds MORE than this
    many of them (`ALPHA_LAZY_MCP_THRESHOLD`, default 8) — a small server
    stays inline, a big one is searched."""
    return _env_int_clamped("ALPHA_LAZY_MCP_THRESHOLD", 8, min_value=0)


_SHOW_THINKING_MODES = {"compact", "full", "off"}


def show_thinking_mode() -> str:
    """How the REPL renders the model's reasoning (extended thinking /
    `reasoning_content`) — `ALPHA_SHOW_THINKING`:

    - ``compact`` (default): dim block capped at a few lines + `(+N linhas)`.
    - ``full``: the whole reasoning, dim.
    - ``off``: hidden (still round-tripped to the provider).
    """
    return _validated_env("ALPHA_SHOW_THINKING", "compact", _SHOW_THINKING_MODES)


def browser_tools_enabled() -> bool:
    """Whether the browser tool family should be advertised to the model.

    Auto: on only when Playwright is importable (the tools error without it).
    Override with `ALPHA_BROWSER_TOOLS=on|off`.
    """
    from importlib.util import find_spec
    return _tristate_flag(
        "ALPHA_BROWSER_TOOLS",
        auto_default=find_spec("playwright") is not None,
    )


def vision_tool_results_enabled(provider: str) -> bool:
    """Whether tool results carrying `_image_path` (e.g. browser_screenshot)
    should be attached to the conversation as vision messages (Plano-Browser
    Etapa 1).

    Auto: on only when the active provider declares `supports_vision` —
    sending image blocks to a non-vision provider (deepseek, grok, ollama)
    is a guaranteed HTTP 400. Override with `ALPHA_VISION_TOOL_RESULTS=on`
    (e.g. a custom ollama vision model whose config flag says False) or
    `=off` to keep results text-only even on vision providers.
    """
    # #DM061: usa os sets canônicos. Não delega ao `env_bool(default=...)`
    # porque o default (supports_vision) deve ser resolvido LAZY — com o
    # override setado, `get_provider_config` nem é chamado.
    raw = os.environ.get("ALPHA_VISION_TOOL_RESULTS", "").strip().lower()
    if raw in _ENV_TRUE:
        return True
    if raw in _ENV_FALSE:
        return False
    try:
        return bool(get_provider_config(provider).get("supports_vision", False))
    except RuntimeError:
        # Provider desconhecido ou API key ausente — fail-closed (sem visão).
        return False


def apify_tools_enabled() -> bool:
    """Whether the Apify scraping tools should be advertised to the model.

    Auto: on only when `APIFY_API_TOKEN` is set (the tools error without it).
    Override with `ALPHA_APIFY_TOOLS=on|off`.
    """
    return _tristate_flag(
        "ALPHA_APIFY_TOOLS",
        auto_default=bool(os.environ.get("APIFY_API_TOKEN", "").strip()),
    )


# ─── Tool Timeouts (seconds) ───
#
# #D003 (V1.0 MAINT): tres camadas de timeout, todas centralizadas aqui:
#
# 1. `TOOL_TIMEOUTS` (per-category default) — usado quando o caller nao
#    especifica `timeout=` na chamada da tool. Categoria vem do registry.
#
# 2. `TOOL_TIMEOUT_CAPS` (per-category hard cap) — limite maximo mesmo que
#    o caller peca timeout maior. Aplicado via `min(timeout, cap)` nas
#    tools. Antes era hardcoded literal em 4 modulos divergentes
#    (shell_tools=300, code_tools=60, network_tools=60, pipeline_tools=120).
#
# 3. `TOOL_EXECUTION_TIMEOUT` / `SLOW_TOOL_TIMEOUT` (executor-level) — para
#    tools que nao tem categoria conhecida (composite, agent, etc.) ou
#    para o gate global do executor. Antes estavam em `executor.py`.
#
# Ajustar timeout = mudar UM lugar.

TOOL_TIMEOUTS: dict = {
    "shell": 180,
    "code": 60,
    "git": 30,
    "network": 30,
    "pipeline": 240,
    "database": 30,
    "browser": 180,
}

TOOL_TIMEOUT_CAPS: dict = {
    "shell": 300,
    "code": 60,
    "network": 60,
    "pipeline": 120,
    "database": 60,
    "browser": 300,
    "git": 60,
}

# Executor-level timeouts (tools sem categoria, gate global). Importados
# por `executor.py` — manter compatibilidade com tests que ainda lem do
# executor (ele re-exporta).
TOOL_EXECUTION_TIMEOUT = 120  # default para tools sem categoria
SLOW_TOOL_TIMEOUT = 300       # delegate_*, run_tests, deploy_check, etc.

# ─── Browser tool policies ───
# Comma-separated env vars. By default, an empty allowlist still permits
# qualquer dominio (compatibilidade) — definir ALPHA_BROWSER_REQUIRE_ALLOWLIST=1
# torna a allowlist obrigatoria (allowlist vazia => bloqueia tudo).
BROWSER_DOMAIN_ALLOWLIST: list[str] = [
    d.strip().lower()
    for d in os.getenv("ALPHA_BROWSER_ALLOWLIST", "").split(",")
    if d.strip()
]
BROWSER_DOMAIN_BLOCKLIST: list[str] = [
    d.strip().lower()
    for d in os.getenv("ALPHA_BROWSER_BLOCKLIST", "").split(",")
    if d.strip()
]
BROWSER_REQUIRE_ALLOWLIST: bool = os.getenv(
    "ALPHA_BROWSER_REQUIRE_ALLOWLIST", ""
).strip().lower() in ("1", "true", "yes", "on")

if not BROWSER_DOMAIN_ALLOWLIST and not BROWSER_REQUIRE_ALLOWLIST:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "ALPHA_BROWSER_ALLOWLIST esta vazia (browser tools aceitam qualquer "
        "dominio). Defina ALPHA_BROWSER_ALLOWLIST=dominio1,dominio2 ou "
        "ALPHA_BROWSER_REQUIRE_ALLOWLIST=1 para reduzir vetor de exfil."
    )
