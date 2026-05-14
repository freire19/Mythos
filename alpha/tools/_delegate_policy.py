"""Sub-agent safety policy and blocklist (#082).

Extracted from delegate_tools.py — constants, prompt loading, and
the default approval gate used when no human callback is available.
"""

from pathlib import Path

from ..config import _PROJECT_ROOT

_SUBAGENT_PROMPT_PATH = _PROJECT_ROOT / "prompts" / "subagent.md"

# ─── Sub-agent safety policy (referenciado pelos testes em test_subagent_blocked.py) ───
#
# DESTRUCTIVE tools que sao removidas do toolset do sub-agent quando nao
# existe approval callback do parent. Cobre:
# - shell/pipeline/http/db/clipboard/install: side effects fora do workspace
# - browser_*: JS arbitrario, click/fill em sessao logada (cookie/form exfil)
#
# Nao listadas aqui (mas tambem DESTRUCTIVE):
# - write_file, edit_file, execute_python, search_and_replace, run_tests:
#   auto-aprovadas por politica geral (AUTO_APPROVE_TOOLS) — comportamento
#   intencional do system.md
# - delegate_task, delegate_parallel: bloqueadas separadamente para evitar recursao
# - present_plan: ferramenta de planejamento, nao tem efeito real
# - git_operation: gating dinamico via _auto_approve_no_callback abaixo
SUBAGENT_DESTRUCTIVE_BLOCKLIST = frozenset({
    "execute_shell", "execute_pipeline", "http_request",
    "query_database", "clipboard_read", "clipboard_write", "install_package",
    "browser_click", "browser_fill", "browser_select_option",
    "browser_press_key", "browser_execute_js",
    "apify_run_actor",
    "scan_vulnerabilities", "audit_dependencies", "fuzz_endpoint",
    "nmap_scan", "ffuf_fuzz", "banner_grab", "payload_inject",
    "traffic_capture", "port_knock", "exploit_loop",
    "check_mitigations", "generate_rop_chain", "generate_shellcode",
    "inject_payload", "analyze_binary", "run_exploit", "sandbox_test",
    "auto_exploit", "auto_exploit_multi", "tune_offset", "analyze_crash_output",
})

# Read-only git actions que sub-agents podem chamar sem callback.
# Write actions (push/merge/rebase/reset/clean/...) sao rejeitadas.
GIT_READ_ACTIONS = frozenset({
    "status", "diff", "log", "branch", "show", "blame",
    "stash_list", "remote", "tag",
})


def _auto_approve_no_callback(name: str, args: dict) -> bool:
    """Approval default quando sub-agent nao tem callback humano.

    Aprova qualquer tool por default (ja que tools perigosas estao removidas
    via SUBAGENT_DESTRUCTIVE_BLOCKLIST), exceto git_operation onde precisamos
    distinguir read de write actions.
    """
    if name == "git_operation":
        return (args or {}).get("action") in GIT_READ_ACTIONS
    return True


def _load_subagent_prompt() -> str:
    if _SUBAGENT_PROMPT_PATH.exists():
        raw = _SUBAGENT_PROMPT_PATH.read_text(encoding="utf-8")
        return _strip_control_chars(raw)
    return "You are a focused sub-agent. Complete the delegated task using your tools."


def _strip_control_chars(text: str) -> str:
    """Remove control chars que sequestrariam o prompt do sub-agent.

    Cobre NUL (\\x00), ANSI escape (\\x1b), e Unicode bidi overrides
    (RLO/LRO/RLI/LRI/PDI). Sem isso, um arquivo subagent.md modificado
    por atacante poderia esconder instrucoes via reordering visual ou
    quebrar prompts via NUL byte.
    """
    forbidden = set(chr(c) for c in range(32) if c not in (9, 10, 13))
    forbidden |= {"\x7f"}
    forbidden |= {
        "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
        "\u2066", "\u2067", "\u2068", "\u2069",
        "\u200e", "\u200f",
    }
    return "".join(c for c in text if c not in forbidden)
