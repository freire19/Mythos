"""Tool lazy-loading (`alpha.tool_lazy` + `tool_search`) — parity wave 2026-11.

Contratos:
- classificação: browser/scraping/database + nomes raros + MCP acima do
  threshold são DEFERRED; o resto não;
- `apply()` remove deferred não carregadas da lista, mantém a ordem,
  anexa UMA entrada `tool_search` cuja description indexa o que foi
  removido, e NUNCA adiciona uma tool que não estava na lista de entrada
  (não alarga escopo de perfil/plan mode/sub-agente);
- `search()` resolve `select:` e palavras-chave, marca como carregadas,
  devolve schemas completos, respeita o cap;
- flag `ALPHA_LAZY_TOOLS=off` → `apply` devolve a lista intacta;
- `get_openai_tools()` nunca anuncia a entrada estática `tool_search`;
- o loop do agente aplica por iteração (tools carregadas numa iteração
  aparecem na seguinte); `/clear` zera o set carregado.
"""

from __future__ import annotations

import pytest

from alpha import tool_lazy
from alpha.tools import ToolCategory, ToolDefinition, ToolSafety, get_openai_tools, get_tool, load_all_tools


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    load_all_tools()
    monkeypatch.delenv("ALPHA_LAZY_TOOLS", raising=False)
    monkeypatch.delenv("ALPHA_LAZY_MCP_THRESHOLD", raising=False)
    monkeypatch.setenv("ALPHA_BROWSER_TOOLS", "on")
    monkeypatch.setenv("ALPHA_APIFY_TOOLS", "on")
    tool_lazy.reset_loaded()
    yield
    tool_lazy.reset_loaded()


def _names(tools):
    return [t["function"]["name"] for t in tools]


# ─── classificação ───────────────────────────────────────────────────


class TestIsDeferred:
    @pytest.mark.parametrize("name", [
        "browser_open", "browser_click", "apify_run_actor", "list_tables",
        "query_database", "clipboard_read", "deploy_check", "install_package",
    ])
    def test_deferred(self, name):
        assert tool_lazy.is_deferred(name) is True

    @pytest.mark.parametrize("name", [
        "read_file", "edit_file", "execute_shell", "web_search", "delegate_task",
        "present_plan", "screenshot", "tool_search",
    ])
    def test_not_deferred(self, name):
        assert tool_lazy.is_deferred(name) is False

    def test_unknown_name_is_not_deferred(self):
        assert tool_lazy.is_deferred("nope_xyz") is False

    def test_mcp_threshold(self, monkeypatch):
        from alpha.tools import TOOL_REGISTRY

        async def _noop(**kw):
            return {}

        added = []
        try:
            for i in range(3):
                td = ToolDefinition(
                    name=f"mcp__srv__t{i}", description="x", parameters={"type": "object"},
                    safety=ToolSafety.DESTRUCTIVE, executor=_noop, category="mcp:srv",
                )
                TOOL_REGISTRY[td.name] = td
                added.append(td.name)
            # 3 MCP tools, threshold 8 → inline
            assert tool_lazy.is_deferred("mcp__srv__t0") is False
            monkeypatch.setenv("ALPHA_LAZY_MCP_THRESHOLD", "2")
            assert tool_lazy.is_deferred("mcp__srv__t0") is True
        finally:
            for n in added:
                TOOL_REGISTRY.pop(n, None)


# ─── apply ───────────────────────────────────────────────────────────


class TestApply:
    def test_removes_deferred_and_appends_index(self):
        tools = get_openai_tools()
        out = tool_lazy.apply(tools, get_tool)
        names = _names(out)
        assert "browser_open" not in names
        assert "list_tables" not in names
        assert "read_file" in names and "execute_shell" in names
        assert names[-1] == "tool_search"
        assert names.count("tool_search") == 1
        desc = out[-1]["function"]["description"]
        assert "- browser_open:" in desc
        assert "- list_tables:" in desc
        assert len(out) < len(tools)
        # ordem relativa preservada
        kept = [n for n in _names(tools) if n in names]
        assert kept == names[:-1]

    def test_input_not_mutated(self):
        tools = get_openai_tools()
        before = _names(tools)
        tool_lazy.apply(tools, get_tool)
        assert _names(tools) == before

    def test_loaded_tools_come_back_next_apply(self):
        tools = get_openai_tools()
        tool_lazy.mark_loaded(["browser_open"])
        out = tool_lazy.apply(tools, get_tool)
        names = _names(out)
        assert "browser_open" in names
        assert "browser_click" not in names
        # índice não lista mais a carregada
        assert "- browser_open:" not in out[-1]["function"]["description"]

    def test_never_widens_scope(self):
        """Tool carregada via search mas AUSENTE da lista de entrada (perfil
        restrito / plan mode / blocklist) continua ausente."""
        tools = [t for t in get_openai_tools() if t["function"]["name"] != "browser_click"]
        tool_lazy.mark_loaded(["browser_click"])
        out = tool_lazy.apply(tools, get_tool)
        assert "browser_click" not in _names(out)

    def test_off_flag_returns_input_unchanged(self, monkeypatch):
        monkeypatch.setenv("ALPHA_LAZY_TOOLS", "off")
        tools = get_openai_tools()
        assert tool_lazy.apply(tools, get_tool) is tools

    def test_no_deferred_in_list_returns_unchanged(self):
        tools = [t for t in get_openai_tools() if t["function"]["name"] in ("read_file", "edit_file")]
        assert tool_lazy.apply(tools, get_tool) is tools

    def test_all_deferred_loaded_still_has_no_search_when_index_empty(self):
        """Tudo carregado → ainda anexa tool_search (índice '(none)') para o
        modelo não ver a tool sumir no meio da sessão."""
        tools = [t for t in get_openai_tools() if t["function"]["name"] in ("read_file", "list_tables")]
        tool_lazy.mark_loaded(["list_tables"])
        out = tool_lazy.apply(tools, get_tool)
        assert set(_names(out)) == {"read_file", "list_tables", "tool_search"}
        assert _names(out)[-1] == "tool_search"
        assert "(none)" in out[-1]["function"]["description"]

    def test_empty_list(self):
        assert tool_lazy.apply([], get_tool) == []

    def test_stale_tool_search_entry_is_replaced(self):
        tools = get_openai_tools() + [
            {"type": "function", "function": {"name": "tool_search", "description": "velha"}}
        ]
        out = tool_lazy.apply(tools, get_tool)
        assert _names(out).count("tool_search") == 1
        assert out[-1]["function"]["description"] != "velha"


# ─── search ──────────────────────────────────────────────────────────


class TestSearch:
    def test_select_exact_names(self):
        r = tool_lazy.search("select:browser_open, list_tables")
        assert r["ok"] is True
        assert r["loaded"] == ["browser_open", "list_tables"]
        assert [t["function"]["name"] for t in r["tools"]] == ["browser_open", "list_tables"]
        assert "parameters" in r["tools"][0]["function"]
        assert tool_lazy.loaded_names() == {"browser_open", "list_tables"}

    def test_select_reports_not_found_for_non_deferred(self):
        r = tool_lazy.search("select:read_file,browser_open")
        assert r["loaded"] == ["browser_open"]
        assert r["not_found"] == ["read_file"]

    def test_keyword_ranking_prefers_name_hits(self):
        r = tool_lazy.search("browser click", max_results=3)
        assert r["loaded"][0] == "browser_click"
        assert len(r["loaded"]) <= 3

    def test_keyword_category_ranks_whole_family(self):
        # Observado em smoke real (DeepSeek, 2026-11-02): "database SQL SQLite
        # query tables" devolvia browser_query e deixava describe_table de
        # fora. A categoria entra no haystack e plural é dobrado.
        r = tool_lazy.search("database SQL SQLite query tables")
        loaded = r["loaded"]
        assert {"query_database", "list_tables", "describe_table"} <= set(loaded)
        db = [n for n in loaded if get_tool(n).category == ToolCategory.DATABASE]
        assert loaded[: len(db)] == db, "tools de banco vêm antes de qualquer browser_*"

    def test_keyword_no_match(self):
        r = tool_lazy.search("zzzz qqqq")
        assert r["ok"] is True
        assert r["loaded"] == [] and r["tools"] == []
        assert "Nenhuma" in r["hint"]

    def test_cap(self):
        r = tool_lazy.search("browser", max_results=500)
        assert len(r["loaded"]) == tool_lazy.MAX_SEARCH_RESULTS

    def test_empty_query(self):
        r = tool_lazy.search("   ")
        assert r["ok"] is False

    def test_respects_family_flags(self, monkeypatch):
        monkeypatch.setenv("ALPHA_BROWSER_TOOLS", "off")
        r = tool_lazy.search("select:browser_open")
        assert r["loaded"] == [] and r["not_found"] == ["browser_open"]


# ─── registry / tool entry ──────────────────────────────────────────


class TestRegistryEntry:
    def test_static_tool_search_never_advertised(self):
        assert "tool_search" not in _names(get_openai_tools())

    def test_registered_safe_for_dispatch(self):
        td = get_tool("tool_search")
        assert td is not None
        assert td.safety == ToolSafety.SAFE
        assert td.category == ToolCategory.GENERAL

    def test_auto_approved_without_callback(self):
        """Sem callback (sub-agente/headless deny) a tool ainda roda: só
        carrega schemas, zero efeito colateral."""
        from alpha.approval import needs_approval

        assert needs_approval("tool_search", {"query": "select:browser_open"}) is False

    @pytest.mark.asyncio
    async def test_executor_runs_through_registry(self):
        td = get_tool("tool_search")
        r = await td.executor(query="select:browser_open")
        assert r["loaded"] == ["browser_open"]

    def test_plan_mode_keeps_tool_search(self):
        from alpha import plan_mode

        tools = tool_lazy.apply(get_openai_tools(), get_tool)
        out = plan_mode.filter_openai_tools(tools, get_tool)
        assert "tool_search" in _names(out)


# ─── loop do agente ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_agent_applies_per_iteration(monkeypatch):
    """1ª iteração: modelo vê tool_search e não vê browser_open. Chama
    tool_search → 2ª iteração já carrega browser_open na lista."""
    import alpha.agent as agent_mod

    seen: list[list[str]] = []
    calls = {"n": 0}

    async def fake_stream(messages, tools, temperature, **_kw):
        seen.append(_names(tools))
        calls["n"] += 1
        if calls["n"] == 1:
            yield {
                "type": "final", "content": "", "error": None,
                "tool_calls": [{"id": "c1", "name": "tool_search",
                                "arguments": '{"query": "select:browser_open"}'}],
            }
        else:
            yield {"type": "final", "content": "fim", "tool_calls": [], "error": None}

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

    events = [
        e async for e in agent_mod.run_agent(
            messages=[{"role": "user", "content": "abra o browser"}],
            user_message="abra o browser", provider="deepseek",
            get_tool_fn=get_tool, tools=get_openai_tools(),
        )
    ]
    assert len(seen) == 2
    assert "tool_search" in seen[0] and "browser_open" not in seen[0]
    assert "browser_open" in seen[1]
    results = [e for e in events if e["type"] == "tool_result"]
    assert results and results[0]["result"]["loaded"] == ["browser_open"]
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_run_agent_off_flag_passes_full_list(monkeypatch):
    import alpha.agent as agent_mod

    monkeypatch.setenv("ALPHA_LAZY_TOOLS", "off")
    seen = []

    async def fake_stream(messages, tools, temperature, **_kw):
        seen.append(_names(tools))
        yield {"type": "final", "content": "ok", "tool_calls": [], "error": None}

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)
    async for _ in agent_mod.run_agent(
        messages=[{"role": "user", "content": "oi"}], user_message="oi",
        provider="deepseek", get_tool_fn=get_tool, tools=get_openai_tools(),
    ):
        pass
    assert "browser_open" in seen[0] and "tool_search" not in seen[0]


def test_clear_resets_loaded(monkeypatch):
    from alpha.cli.commands import session as session_cmd

    tool_lazy.mark_loaded(["browser_open"])
    monkeypatch.setattr(session_cmd.os, "system", lambda *_: 0)
    monkeypatch.setattr(session_cmd, "print_banner", lambda *a, **k: None)

    class _Ctx:
        history = []
        messages = [{"role": "system", "content": "s"}]
        system_prompt = "s"
        session_id = "x"
        provider = "deepseek"
        cfg = {"model": "m"}

    session_cmd._handle_clear(_Ctx(), ["/clear"])
    assert tool_lazy.loaded_names() == frozenset()
