"""Testes do plan mode (alpha/plan_mode.py + integrações).

Cobre: estado, filtro de tools oferecidas ao modelo (camada 1),
hard-block via approval.is_denied (camada 2), comando /plan com
exclusão mútua do auto-accept, e saída do modo na aprovação do plano.
"""

from __future__ import annotations

import pytest

from alpha import plan_mode
from alpha.tools import get_openai_tools, get_tool, load_all_tools


@pytest.fixture(autouse=True)
def _reset_plan_mode():
    """Estado é module-level — nunca vazar entre testes."""
    plan_mode.set_active(False)
    yield
    plan_mode.set_active(False)


def _tool_names(tools: list[dict]) -> set[str]:
    return {t["function"]["name"] for t in tools}


class TestState:
    def test_default_off(self):
        assert plan_mode.is_active() is False

    def test_set_and_toggle(self):
        plan_mode.set_active(True)
        assert plan_mode.is_active() is True
        assert plan_mode.toggle() is False
        assert plan_mode.toggle() is True


class TestFilterTools:
    def test_keeps_safe_drops_destructive(self):
        load_all_tools()
        full = get_openai_tools()
        filtered = plan_mode.filter_openai_tools(full, get_tool)
        names = _tool_names(filtered)

        # Read-only essenciais continuam
        for keep in ("read_file", "search_files", "glob_files", "list_directory"):
            assert keep in names, keep
        # present_plan é a exceção destrutiva permitida
        assert "present_plan" in names
        # Destrutivas somem do schema
        for drop in (
            "write_file", "edit_file", "execute_shell", "execute_python",
            "git_operation", "search_and_replace", "delegate_task", "pre_flight",
        ):
            assert drop not in names, drop

    def test_unknown_tool_dropped_fail_closed(self):
        fake = [{"type": "function", "function": {"name": "tool_fantasma"}}]
        assert plan_mode.filter_openai_tools(fake, lambda name: None) == []


class TestDenyReason:
    def test_inactive_allows_everything(self):
        load_all_tools()
        assert plan_mode.deny_reason("write_file") is None
        assert plan_mode.deny_reason("read_file") is None

    def test_active_blocks_destructive(self):
        load_all_tools()
        plan_mode.set_active(True)
        reason = plan_mode.deny_reason("write_file")
        assert reason is not None
        assert "present_plan" in reason

    def test_active_allows_safe_and_present_plan(self):
        load_all_tools()
        plan_mode.set_active(True)
        assert plan_mode.deny_reason("read_file") is None
        assert plan_mode.deny_reason("present_plan") is None

    def test_active_blocks_unknown_tool(self):
        plan_mode.set_active(True)
        assert plan_mode.deny_reason("mcp__x__ferramenta_desconhecida") is not None

    def test_is_denied_integration(self):
        load_all_tools()
        from alpha.approval import is_denied

        plan_mode.set_active(True)
        denied, msg = is_denied("execute_shell", {"command": "rm -rf /"})
        assert denied is True
        assert "Plan mode" in msg

        denied, _ = is_denied("present_plan", {"summary": "s", "steps": []})
        assert denied is False

        plan_mode.set_active(False)
        denied, _ = is_denied("execute_shell", {"command": "ls"})
        assert denied is False


class TestPlanCommand:
    def _ctx(self):
        from alpha.cli.commands._types import ReplContext

        return ReplContext(
            messages=[{"role": "system", "content": "s"}],
            history=[],
            session_id="t",
            provider="deepseek",
            temperature=0.5,
            cfg={"model": "m"},
            system_prompt="s",
            tools=[],
            get_tool_fn=None,
            active_agent=None,
        )

    def test_toggle_on_off(self, capsys):
        from alpha.cli.commands.safety import _handle_plan

        _handle_plan(self._ctx(), ["/plan"])
        assert plan_mode.is_active() is True
        _handle_plan(self._ctx(), ["/plan"])
        assert plan_mode.is_active() is False

    def test_explicit_on_off(self):
        from alpha.cli.commands.safety import _handle_plan

        _handle_plan(self._ctx(), ["/plan", "on"])
        assert plan_mode.is_active() is True
        _handle_plan(self._ctx(), ["/plan", "off"])
        assert plan_mode.is_active() is False

    def test_plan_on_disables_auto_accept(self):
        from alpha.cli.commands.safety import _handle_plan
        from alpha.display import is_auto_accept, set_auto_accept

        set_auto_accept(True)
        try:
            _handle_plan(self._ctx(), ["/plan", "on"])
            assert plan_mode.is_active() is True
            assert is_auto_accept() is False
        finally:
            set_auto_accept(False)

    def test_accept_edits_on_disables_plan(self):
        from alpha.cli.commands.safety import _handle_accept_edits
        from alpha.display import is_auto_accept, set_auto_accept

        plan_mode.set_active(True)
        try:
            _handle_accept_edits(self._ctx(), ["/accept-edits", "on"])
            assert is_auto_accept() is True
            assert plan_mode.is_active() is False
        finally:
            set_auto_accept(False)


class TestApprovalExitsPlanMode:
    def test_present_plan_approved_disables(self, monkeypatch):
        import alpha.cli.setup as setup

        plan_mode.set_active(True)
        monkeypatch.setattr(setup, "print_approval_request", lambda n, a: True)
        assert setup.approval_callback("present_plan", {"summary": "s"}) is True
        assert plan_mode.is_active() is False

    def test_present_plan_denied_keeps_mode(self, monkeypatch):
        import alpha.cli.setup as setup

        plan_mode.set_active(True)
        monkeypatch.setattr(setup, "print_approval_request", lambda n, a: False)
        assert setup.approval_callback("present_plan", {"summary": "s"}) is False
        assert plan_mode.is_active() is True

    def test_other_tool_approved_keeps_mode(self, monkeypatch):
        import alpha.cli.setup as setup

        plan_mode.set_active(True)
        monkeypatch.setattr(setup, "print_approval_request", lambda n, a: True)
        assert setup.approval_callback("read_file", {"path": "x"}) is True
        assert plan_mode.is_active() is True


class TestSideEffectSafeBlock:
    """AUDIT_V2.3 #207: tools SAFE com efeito colateral (record_memory grava
    disco; google_antigravity paga chamada externa) são bloqueadas nas duas
    camadas do plan mode."""

    def teardown_method(self):
        plan_mode.set_active(False)

    def test_deny_reason_blocks_side_effect_safe(self):
        plan_mode.set_active(True)
        assert plan_mode.deny_reason("record_memory") is not None
        assert plan_mode.deny_reason("google_antigravity") is not None

    def test_deny_reason_still_allows_pure_reads(self):
        plan_mode.set_active(True)
        from alpha.tools import load_all_tools
        load_all_tools()
        assert plan_mode.deny_reason("read_file") is None

    def test_filter_removes_side_effect_safe_from_schema(self):
        from alpha.tools import get_tool, load_all_tools
        load_all_tools()
        tools = [
            {"type": "function", "function": {"name": "record_memory"}},
            {"type": "function", "function": {"name": "google_antigravity"}},
            {"type": "function", "function": {"name": "read_file"}},
        ]
        out = plan_mode.filter_openai_tools(tools, get_tool)
        names = {t["function"]["name"] for t in out}
        assert "record_memory" not in names
        assert "google_antigravity" not in names
        assert "read_file" in names


class TestLayer3Note:
    """AUDIT_V2.3 #602: camada 3 — o prefixo PLAN_MODE_NOTE no turno do
    usuário. Testa o helper REAL de produção (`_apply_plan_mode_note`), não
    uma reconstrução da lógica (nota da review: tautologia)."""

    def teardown_method(self):
        plan_mode.set_active(False)

    def test_note_prepended_when_active(self):
        from alpha.cli.entry import _apply_plan_mode_note

        plan_mode.set_active(True)
        out = _apply_plan_mode_note("[CWD: /x]\noi")
        assert out.startswith(plan_mode.PLAN_MODE_NOTE)
        assert out.endswith("[CWD: /x]\noi")
        assert "\n\n" in out

    def test_no_note_when_inactive(self):
        from alpha.cli.entry import _apply_plan_mode_note

        plan_mode.set_active(False)
        text = "[CWD: /x]\noi"
        assert _apply_plan_mode_note(text) == text
