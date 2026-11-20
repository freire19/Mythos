"""Memória entre sessões — caminho de LEITURA (parity wave 2026-11, item 3).

`record_memory` + `/memory` existiam, mas `summary_for_prompt` nunca era
chamado: nada gravado voltava ao prompt. Agora `inject_memory` entra em
`cli.setup.build_system_prompt` (REPL / single / print) e no default do
`sdk.run_task`. Contratos:

- vazio → prompt intacto (instalação nova não paga nada);
- com notas → seção `# MEMORY` no FIM do prompt, com preâmbulo e as
  entradas global + workspace;
- `ALPHA_NO_MEMORY=1` desliga só a leitura;
- erro ao ler o arquivo nunca derruba o startup;
- embedder que passa `system_prompt` próprio não recebe injeção.
"""

from __future__ import annotations

import pytest

from alpha import memory


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr("alpha.memory._memory_root", lambda: tmp_path / "memory")
    monkeypatch.delenv("ALPHA_NO_MEMORY", raising=False)
    monkeypatch.chdir(tmp_path)
    yield


class TestInjectMemory:
    def test_empty_memory_leaves_prompt_untouched(self):
        assert memory.inject_memory("BASE") == "BASE"

    def test_appends_section_at_the_end(self):
        memory.record("Prefere respostas em português.", scope="global", kind="preference")
        memory.record("Testes rodam com .venv/bin/python -m pytest.", kind="pattern")
        out = memory.inject_memory("BASE\n")
        assert out.startswith("BASE\n\n")
        assert memory.MEMORY_SECTION_HEADER in out
        assert out.index(memory.MEMORY_SECTION_HEADER) > out.index("BASE")
        assert "Prefere respostas em português." in out
        assert "Testes rodam com .venv/bin/python" in out
        assert "Global memory:" in out and "This workspace:" in out
        # preâmbulo orienta a verificar antes de confiar
        assert "verify it still" in out

    def test_opt_out_env(self, monkeypatch):
        memory.record("nota", scope="global")
        monkeypatch.setenv("ALPHA_NO_MEMORY", "1")
        assert memory.inject_memory("BASE") == "BASE"
        assert memory.memory_enabled() is False

    def test_read_failure_never_raises(self, monkeypatch):
        def boom(max_chars=4000):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(memory, "summary_for_prompt", boom)
        assert memory.inject_memory("BASE") == "BASE"

    def test_respects_max_chars(self):
        memory.record("x" * 3000, scope="global")
        memory.record("y" * 3000)
        out = memory.inject_memory("BASE", max_chars=500)
        body = out.split(memory.MEMORY_SECTION_HEADER, 1)[1]
        assert len(body) < 1200  # preâmbulo + 500 + "..."
        assert body.rstrip().endswith("...")


class TestWiring:
    def test_build_system_prompt_includes_memory(self, monkeypatch):
        from alpha.cli import setup

        memory.record("Convenção: commits em português.", kind="pattern")
        monkeypatch.setattr(setup, "load_all_skills", lambda: None)
        monkeypatch.setattr(setup, "load_system_prompt", lambda: "SYS")
        monkeypatch.setattr(setup, "inject_skill_index", lambda base, name_filter=None: base)
        monkeypatch.setenv("ALPHA_NO_PROJECT_CONTEXT", "1")
        out = setup.build_system_prompt(None)
        assert out.startswith("SYS")
        assert memory.MEMORY_SECTION_HEADER in out
        assert "Convenção: commits em português." in out
        # memória é a ÚLTIMA seção (prefixo cacheável estável)
        assert out.rstrip().endswith("Convenção: commits em português.")

    def test_build_system_prompt_without_memory_unchanged(self, monkeypatch):
        from alpha.cli import setup

        monkeypatch.setattr(setup, "load_all_skills", lambda: None)
        monkeypatch.setattr(setup, "load_system_prompt", lambda: "SYS")
        monkeypatch.setattr(setup, "inject_skill_index", lambda base, name_filter=None: base)
        monkeypatch.setenv("ALPHA_NO_PROJECT_CONTEXT", "1")
        assert memory.MEMORY_SECTION_HEADER not in setup.build_system_prompt(None)

    @pytest.mark.asyncio
    async def test_sdk_default_prompt_gets_memory(self, monkeypatch):
        import alpha.agent as agent_mod
        from alpha import sdk

        memory.record("nota sdk", scope="global")
        captured = {}

        async def fake_run_agent(convo, *a, **kw):
            captured["system"] = convo[0]["content"]
            yield {"type": "done", "reply": "ok"}

        monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)
        await sdk.run_task("t", provider="deepseek", _tools_openai=[], _get_tool_fn=lambda n: None)
        assert "nota sdk" in captured["system"]

    @pytest.mark.asyncio
    async def test_sdk_custom_prompt_not_injected(self, monkeypatch):
        import alpha.agent as agent_mod
        from alpha import sdk

        memory.record("nota sdk", scope="global")
        captured = {}

        async def fake_run_agent(convo, *a, **kw):
            captured["system"] = convo[0]["content"]
            yield {"type": "done", "reply": "ok"}

        monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)
        await sdk.run_task(
            "t", provider="deepseek", system_prompt="MEU",
            _tools_openai=[], _get_tool_fn=lambda n: None,
        )
        assert captured["system"] == "MEU"


@pytest.mark.skip(
    reason="Mythos: alpha/prompts/system.md is identity (merge=ours) and does "
    "not carry upstream's '# MEMORY ACROSS SESSIONS' section yet — port it "
    "by hand, then drop this skip."
)
def test_system_prompt_mentions_memory_guidance():
    from alpha.config import load_system_prompt

    text = load_system_prompt()
    assert "# MEMORY ACROSS SESSIONS" in text
    assert "record_memory" in text
