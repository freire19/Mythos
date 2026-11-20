"""Coverage para `subagent_policy` configuravel via runtime getters.

Cobre:
- `get_subagent_policy()` / `get_subagent_extra_block()` / `get_subagent_allow()`
  exportados de `alpha.config`
- Defaults preservam comportamento antigo (strict + nada extra)
- AUDIT_V1.2 #014: getters leem env A CADA CALL (nao cache de import-time)
  — `monkeypatch.setenv` pos-import deve afetar comportamento
- `delegate_tools._run_subagent` usa os getters em vez de FEATURES dict
- `delegate_*` sempre bloqueado (anti-recursao) mesmo com policy=relaxed
  ou allow incluindo delegate_task
"""

from __future__ import annotations

import pytest


class TestGettersDefaults:
    """Valores default quando env nao esta setada."""

    def test_default_policy_is_strict(self, monkeypatch):
        monkeypatch.delenv("ALPHA_SUBAGENT_POLICY", raising=False)
        from alpha.config import get_subagent_policy
        assert get_subagent_policy() == "strict"

    def test_default_extra_block_empty(self, monkeypatch):
        monkeypatch.delenv("ALPHA_SUBAGENT_EXTRA_BLOCK", raising=False)
        from alpha.config import get_subagent_extra_block
        assert get_subagent_extra_block() == frozenset()

    def test_default_allow_empty(self, monkeypatch):
        monkeypatch.delenv("ALPHA_SUBAGENT_ALLOW", raising=False)
        from alpha.config import get_subagent_allow
        assert get_subagent_allow() == frozenset()


class TestRuntimeEnvOverrides:
    """AUDIT_V1.2 #014: getters refletem mudanca runtime em os.environ.

    Antes do fix, `FEATURES["subagent_policy"]` era resolvido no IMPORT do
    modulo, congelando a flag. Hooks/scripts/testes que setassem env apos
    o import nao surtiam efeito ate reload — confusao operacional + bug
    silencioso. Agora cada call le env de novo.
    """

    def test_policy_changes_reflect_immediately(self, monkeypatch):
        from alpha.config import get_subagent_policy

        monkeypatch.setenv("ALPHA_SUBAGENT_POLICY", "relaxed")
        assert get_subagent_policy() == "relaxed"

        monkeypatch.setenv("ALPHA_SUBAGENT_POLICY", "strict")
        assert get_subagent_policy() == "strict"

    def test_extra_block_csv_parsing(self, monkeypatch):
        from alpha.config import get_subagent_extra_block

        monkeypatch.setenv(
            "ALPHA_SUBAGENT_EXTRA_BLOCK",
            "write_file, edit_file ,  http_request",
        )
        assert get_subagent_extra_block() == {
            "write_file", "edit_file", "http_request",
        }

    def test_extra_block_empty_yields_empty_frozenset(self, monkeypatch):
        from alpha.config import get_subagent_extra_block

        monkeypatch.setenv("ALPHA_SUBAGENT_EXTRA_BLOCK", "")
        assert get_subagent_extra_block() == frozenset()

    def test_allow_runtime_change(self, monkeypatch):
        from alpha.config import get_subagent_allow

        # Sem env: vazio.
        monkeypatch.delenv("ALPHA_SUBAGENT_ALLOW", raising=False)
        assert get_subagent_allow() == frozenset()

        # Set: returns parsed.
        monkeypatch.setenv("ALPHA_SUBAGENT_ALLOW", "execute_shell,http_request")
        assert get_subagent_allow() == {"execute_shell", "http_request"}

        # Unset: back to empty (NOT cached).
        monkeypatch.delenv("ALPHA_SUBAGENT_ALLOW", raising=False)
        assert get_subagent_allow() == frozenset()


class TestResolveBlocklistBehavior:
    """`_resolve_subagent_blocklist` compoe a blocklist a partir dos getters
    runtime (policy/extra_block/allow). Em vez de grepar o source, exercitamos
    a funcao com env controlada e checamos o set resultante — qualquer
    regressao (cache import-time, override de allow furando anti-recursao, etc)
    falha o teste.
    """

    def test_reads_policy_getter_at_call_time(self, monkeypatch):
        """strict (default, sem callback) bloqueia destrutivas; relaxed nao —
        prova que o getter de policy e lido a cada call, nao congelado."""
        from alpha.tools import delegate_tools
        from alpha.tools.delegate_tools import SUBAGENT_DESTRUCTIVE_BLOCKLIST

        monkeypatch.setenv("ALPHA_SUBAGENT_POLICY", "strict")
        monkeypatch.delenv("ALPHA_SUBAGENT_EXTRA_BLOCK", raising=False)
        monkeypatch.delenv("ALPHA_SUBAGENT_ALLOW", raising=False)
        strict = delegate_tools._resolve_subagent_blocklist(None)
        assert SUBAGENT_DESTRUCTIVE_BLOCKLIST <= strict

        monkeypatch.setenv("ALPHA_SUBAGENT_POLICY", "relaxed")
        relaxed = delegate_tools._resolve_subagent_blocklist(None)
        assert not (SUBAGENT_DESTRUCTIVE_BLOCKLIST <= relaxed)

    def test_reads_extra_block_getter_at_call_time(self, monkeypatch):
        """Um nome em ALPHA_SUBAGENT_EXTRA_BLOCK aparece na blocklist —
        e some quando a env muda, sem reload."""
        from alpha.tools import delegate_tools

        monkeypatch.setenv("ALPHA_SUBAGENT_POLICY", "relaxed")
        monkeypatch.delenv("ALPHA_SUBAGENT_ALLOW", raising=False)

        monkeypatch.setenv("ALPHA_SUBAGENT_EXTRA_BLOCK", "read_file")
        assert "read_file" in delegate_tools._resolve_subagent_blocklist(None)

        monkeypatch.delenv("ALPHA_SUBAGENT_EXTRA_BLOCK", raising=False)
        assert "read_file" not in delegate_tools._resolve_subagent_blocklist(None)

    def test_reads_allow_getter_at_call_time(self, monkeypatch):
        """ALPHA_SUBAGENT_ALLOW remove uma destrutiva da blocklist (strict),
        provando que o getter de allow tambem e lido em runtime."""
        from alpha.tools import delegate_tools

        monkeypatch.setenv("ALPHA_SUBAGENT_POLICY", "strict")
        monkeypatch.delenv("ALPHA_SUBAGENT_EXTRA_BLOCK", raising=False)

        monkeypatch.delenv("ALPHA_SUBAGENT_ALLOW", raising=False)
        assert "execute_shell" in delegate_tools._resolve_subagent_blocklist(None)

        monkeypatch.setenv("ALPHA_SUBAGENT_ALLOW", "execute_shell")
        assert "execute_shell" not in delegate_tools._resolve_subagent_blocklist(None)

    def test_relaxed_policy_skips_destructive_blocklist(self, monkeypatch):
        from alpha.tools import delegate_tools
        from alpha.tools.delegate_tools import SUBAGENT_DESTRUCTIVE_BLOCKLIST

        monkeypatch.setenv("ALPHA_SUBAGENT_POLICY", "relaxed")
        monkeypatch.delenv("ALPHA_SUBAGENT_EXTRA_BLOCK", raising=False)
        monkeypatch.delenv("ALPHA_SUBAGENT_ALLOW", raising=False)

        blocked = delegate_tools._resolve_subagent_blocklist(None)
        # Nenhuma destrutiva sobra, mas anti-recursao continua.
        assert SUBAGENT_DESTRUCTIVE_BLOCKLIST.isdisjoint(blocked)
        assert {"delegate_task", "delegate_parallel"} <= blocked

    def test_delegate_invariant_survives_allow_override(self, monkeypatch):
        """Mesmo com allow incluindo delegate_*, anti-recursao permanece —
        o override NAO pode habilitar recursao entre agentes."""
        from alpha.tools import delegate_tools

        monkeypatch.setenv("ALPHA_SUBAGENT_POLICY", "relaxed")
        monkeypatch.delenv("ALPHA_SUBAGENT_EXTRA_BLOCK", raising=False)
        monkeypatch.setenv(
            "ALPHA_SUBAGENT_ALLOW", "delegate_task,delegate_parallel,delegate_consensus"
        )

        blocked = delegate_tools._resolve_subagent_blocklist(None)
        assert {"delegate_task", "delegate_parallel"} <= blocked

    def test_strict_with_parent_callback_does_not_block_destructive(
        self, monkeypatch
    ):
        """strict + callback presente: o callback decide por tool, entao a
        blocklist NAO inclui as destrutivas (so anti-recursao)."""
        from alpha.tools import delegate_tools
        from alpha.tools.delegate_tools import SUBAGENT_DESTRUCTIVE_BLOCKLIST

        monkeypatch.setenv("ALPHA_SUBAGENT_POLICY", "strict")
        monkeypatch.delenv("ALPHA_SUBAGENT_EXTRA_BLOCK", raising=False)
        monkeypatch.delenv("ALPHA_SUBAGENT_ALLOW", raising=False)

        blocked = delegate_tools._resolve_subagent_blocklist(lambda *a, **kw: True)
        assert SUBAGENT_DESTRUCTIVE_BLOCKLIST.isdisjoint(blocked)
        assert {"delegate_task", "delegate_parallel"} <= blocked


@pytest.mark.parametrize(
    "policy,parent_callback,expect_destructive_blocked",
    [
        ("strict", None, True),
        ("strict", lambda *a, **kw: True, False),
        ("relaxed", None, False),
        ("relaxed", lambda *a, **kw: True, False),
    ],
)
def test_blocklist_logic_combinations(
    policy, parent_callback, expect_destructive_blocked
):
    """Reproduz a logica de bloqueio fora da funcao para validar a tabela:
    - strict + sem callback: bloqueia destrutivas
    - strict + callback: nao bloqueia (callback decide por tool)
    - relaxed + qualquer: nao bloqueia (so anti-recursao)
    """
    from alpha.tools.delegate_tools import SUBAGENT_DESTRUCTIVE_BLOCKLIST

    blocked = {"delegate_task", "delegate_parallel"}
    if parent_callback is None and policy != "relaxed":
        blocked = blocked | SUBAGENT_DESTRUCTIVE_BLOCKLIST

    has_destructive = "execute_shell" in blocked
    assert has_destructive == expect_destructive_blocked
