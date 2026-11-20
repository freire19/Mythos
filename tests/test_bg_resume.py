"""Retomada de sub-agentes de BACKGROUND (`alpha/bg_resume.py`).

Contratos:
- o pai pré-gera o `agent_id` e passa ao filho `ALPHA_BG_TRANSCRIPT` /
  `ALPHA_BG_AGENT_ID`; o resultado do spawn carrega `agent_id`;
- o filho (`run_print`) salva o transcript ao terminar: system dropado,
  tool outputs clipados, reasoning removido, tool_calls órfãs saneadas;
- `load_transcript` rejeita id inseguro, agent_id divergente, roles
  inválidos e emparelhamento quebrado;
- `agent_message(agent_id)` sem sessão em memória revive a partir do
  arquivo: mesmo id, histórico do filho atrás do NOSSO system prompt,
  registra em `agent_sessions`, resultado marca `resumed_from=background`;
- `agent_list` mescla sessões em memória e transcripts em disco;
- id em execução/sem transcript segue `not_found`.
"""

from __future__ import annotations

import json
import os

import pytest

import alpha.tools.delegate_tools as dt
from alpha import agent_sessions, bg_resume
from alpha.config import FEATURES
from alpha.tools import load_all_tools


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    load_all_tools()
    monkeypatch.delenv("ALPHA_DELEGATED", raising=False)
    monkeypatch.delenv(bg_resume.TRANSCRIPT_ENV, raising=False)
    monkeypatch.setitem(FEATURES, "multi_agent_enabled", True)
    monkeypatch.setitem(FEATURES, "delegate_tool_enabled", True)
    agent_sessions.reset()
    yield
    agent_sessions.reset()


def _child_messages():
    return [
        {"role": "system", "content": "SYSTEM DO FILHO"},
        {"role": "user", "content": "[CWD: /x]\ninvestiga X"},
        {"role": "assistant", "content": None, "reasoning_content": "pensando...",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "A" * 5000},
        {"role": "assistant", "content": "achei X"},
    ]


# ── módulo ──────────────────────────────────────────────────────────

class TestTranscriptFile:
    def test_path_rejects_unsafe_ids(self, tmp_path):
        for bad in ("", "../x", "a/b", "a\\b", ".hidden", "x" * 70, "é"):
            with pytest.raises(ValueError):
                bg_resume.transcript_path(tmp_path, bad)
            assert bg_resume.load_transcript(tmp_path, bad) is None
        p = bg_resume.transcript_path(tmp_path, "20261102-120000-abcd1234")
        assert p.parent == tmp_path / ".alpha" / "runs" / "bg" / "agents"

    def test_save_then_load_roundtrip_is_sanitized(self, tmp_path):
        aid = "20261102-120000-aaaa0001"
        p = bg_resume.transcript_path(tmp_path, aid)
        ok = bg_resume.save_transcript(
            p, _child_messages(), task="investiga X", status="completed",
            reply="achei X", agent="pesquisador", provider="deepseek",
        )
        assert ok and p.is_file()
        assert oct(p.stat().st_mode & 0o777) == "0o600"

        info = bg_resume.load_transcript(tmp_path, aid)
        assert info is not None
        roles = [m["role"] for m in info["messages"]]
        assert roles == ["user", "assistant", "tool", "assistant"], "system dropado"
        assert "reasoning_content" not in info["messages"][1]
        assert len(info["messages"][2]["content"]) < 2100, "tool output clipado"
        assert info["agent"] == "pesquisador" and info["provider"] == "deepseek"
        assert info["task"] == "investiga X" and info["reply_preview"] == "achei X"

    def test_save_drops_orphan_tool_calls(self, tmp_path):
        aid = "20261102-120000-aaaa0002"
        msgs = _child_messages()[:3]  # assistant com tool_calls e SEM tool result
        bg_resume.save_transcript(
            bg_resume.transcript_path(tmp_path, aid), msgs,
            task="t", status="error", reply="",
        )
        info = bg_resume.load_transcript(tmp_path, aid)
        assert info is not None
        assert all("tool_calls" not in m for m in info["messages"])

    @pytest.mark.parametrize("mutate", [
        lambda d: d.update(agent_id="outro-id"),
        lambda d: d.update(messages=[]),
        lambda d: d.update(messages=[{"role": "system", "content": "x"}]),
        lambda d: d.update(messages=[{"role": "tool", "tool_call_id": "zz", "content": "órfã"}]),
        lambda d: d.update(messages=[
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "assistant", "content": "sem o tool result"},
        ]),
        lambda d: d.update(messages="not a list"),
    ])
    def test_load_rejects_malformed(self, tmp_path, mutate):
        aid = "20261102-120000-aaaa0003"
        p = bg_resume.transcript_path(tmp_path, aid)
        bg_resume.save_transcript(p, _child_messages(), task="t", status="completed", reply="r")
        data = json.loads(p.read_text())
        mutate(data)
        p.write_text(json.dumps(data))
        assert bg_resume.load_transcript(tmp_path, aid) is None

    def test_load_rejects_garbage_and_oversize(self, tmp_path, monkeypatch):
        aid = "20261102-120000-aaaa0004"
        p = bg_resume.transcript_path(tmp_path, aid)
        p.parent.mkdir(parents=True)
        p.write_text("{not json")
        assert bg_resume.load_transcript(tmp_path, aid) is None
        bg_resume.save_transcript(p, _child_messages(), task="t", status="completed", reply="r")
        monkeypatch.setattr(bg_resume, "MAX_FILE_BYTES", 10)
        assert bg_resume.load_transcript(tmp_path, aid) is None

    def test_trim_keeps_tool_pairs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bg_resume, "MAX_MESSAGES", 3)
        aid = "20261102-120000-aaaa0005"
        p = bg_resume.transcript_path(tmp_path, aid)
        bg_resume.save_transcript(p, _child_messages(), task="t", status="completed", reply="r")
        info = bg_resume.load_transcript(tmp_path, aid)
        # Últimas 3 seriam [assistant+tool_calls, tool, assistant]; o corte
        # nunca começa numa `tool` órfã e o resultado valida.
        assert info is not None
        assert info["messages"][0]["role"] != "tool"

    def test_list_and_forget(self, tmp_path):
        assert bg_resume.list_transcripts(tmp_path) == []
        for i in range(3):
            aid = f"20261102-12000{i}-bbbb000{i}"
            bg_resume.save_transcript(
                bg_resume.transcript_path(tmp_path, aid), _child_messages(),
                task=f"tarefa {i}", status="completed", reply="ok",
            )
        (bg_resume.transcripts_dir(tmp_path) / "lixo.json").write_text("{")
        listed = bg_resume.list_transcripts(tmp_path)
        assert len(listed) == 3
        assert all(e["source"] == "background" and e["busy"] is False for e in listed)
        assert bg_resume.forget(tmp_path, listed[0]["agent_id"]) is True
        assert len(bg_resume.list_transcripts(tmp_path)) == 2
        assert bg_resume.forget(tmp_path, "../x") is False

    def test_child_target_from_env(self, monkeypatch, tmp_path):
        assert bg_resume.child_target() is None
        monkeypatch.setenv(bg_resume.TRANSCRIPT_ENV, str(tmp_path / "t.json"))
        assert bg_resume.child_target() == tmp_path / "t.json"


# ── pai: spawn publica o id ─────────────────────────────────────────

def test_spawn_hands_transcript_path_to_child(monkeypatch, tmp_path):
    captured = {}

    def fake_start(command, cwd, env=None):
        captured.update(command=command, cwd=cwd, env=env)
        return {"task_id": 9, "pid": 1, "log": "/tmp/t.log"}

    import alpha.bg_tasks as bg
    monkeypatch.setattr(bg, "start", fake_start)

    r = dt._spawn_background_subagent("pesquise X", parent_workspace=str(tmp_path))
    assert r["ok"] and r["agent_id"]
    aid = r["agent_id"]
    assert captured["env"][bg_resume.AGENT_ID_ENV] == aid
    assert captured["env"][bg_resume.TRANSCRIPT_ENV] == str(bg_resume.transcript_path(tmp_path, aid))
    assert captured["cwd"] == str(tmp_path)
    assert f"agent_message(agent_id='{aid}'" in r["message"]


# ── filho: run_print salva ao terminar ──────────────────────────────

def test_run_print_child_saves_transcript(monkeypatch, tmp_path):
    import alpha.cli.entry as entry
    from alpha import sdk
    from alpha.sdk import TaskResult

    target = bg_resume.transcript_path(tmp_path, "20261102-130000-cccc0001")
    monkeypatch.setenv(bg_resume.TRANSCRIPT_ENV, str(target))
    monkeypatch.setenv("ALPHA_AGENT", "pesquisador")
    monkeypatch.setattr(
        entry, "_setup_agent_session",
        lambda p, t: (None, p, t, "sys", lambda n: None, []),
    )
    monkeypatch.setattr(
        sdk, "run_task_sync",
        lambda *a, **k: TaskResult(reply="achei X", turns=1, messages=_child_messages()),
    )
    with pytest.raises(SystemExit) as exc:
        entry.run_print("deepseek", 0.5, "investiga X", output_format="text")
    assert exc.value.code == 0
    info = bg_resume.load_transcript(tmp_path, "20261102-130000-cccc0001")
    assert info is not None
    assert info["task"] == "investiga X" and info["agent"] == "pesquisador"
    assert info["provider"] == "deepseek" and info["status"] == "completed"
    assert [m["role"] for m in info["messages"]][0] == "user"


def test_run_print_without_env_writes_nothing(monkeypatch, tmp_path):
    import alpha.cli.entry as entry
    from alpha import sdk
    from alpha.sdk import TaskResult

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        entry, "_setup_agent_session",
        lambda p, t: (None, p, t, "sys", lambda n: None, []),
    )
    monkeypatch.setattr(sdk, "run_task_sync", lambda *a, **k: TaskResult(reply="ok", turns=1))
    with pytest.raises(SystemExit):
        entry.run_print("deepseek", 0.5, "x", output_format="text")
    assert not (tmp_path / ".alpha" / "runs" / "bg" / "agents").exists()


# ── pai: agent_message revive ───────────────────────────────────────

def _fake_run_agent(calls: list[dict], reply: str = "e Y também"):
    async def fake(messages, user_message, **kw):
        calls.append({"messages": list(messages), "user_message": user_message, **kw})
        messages.append({"role": "assistant", "content": reply})
        yield {"type": "token", "text": reply}
        yield {"type": "done", "reply": reply}
    return fake


def _write_bg_transcript(ws, aid="20261102-140000-dddd0001", agent=""):
    bg_resume.save_transcript(
        bg_resume.transcript_path(ws, aid), _child_messages(),
        task="investiga X", status="completed", reply="achei X", agent=agent,
    )
    return aid


@pytest.mark.asyncio
async def test_agent_message_revives_background_run(monkeypatch, tmp_path):
    import alpha.agent as agent_mod
    from alpha.agents.workspace import active_workspace

    aid = _write_bg_transcript(tmp_path)
    token = active_workspace.set(str(tmp_path))
    calls: list[dict] = []
    monkeypatch.setattr(agent_mod, "run_agent", _fake_run_agent(calls))
    try:
        r = await dt._agent_message(aid, "e sobre Y?")
    finally:
        active_workspace.reset(token)

    assert r["status"] == "completed", r
    assert r["agent_id"] == aid and r["resumed_from"] == "background"
    assert r["resumable"] is True
    sent = calls[0]["messages"]
    assert sent[0]["role"] == "system" and "SYSTEM DO FILHO" not in sent[0]["content"]
    assert sent[1]["content"].endswith("investiga X")
    assert sent[-1] == {"role": "user", "content": "e sobre Y?"}
    assert any(m.get("content") == "achei X" for m in sent)
    # Scratch vazio é removido ao fim do run (contrato normal); o path é o do id.
    assert r["scratch_dir"] == str(tmp_path / ".alpha" / "runs" / aid)
    assert not os.path.isdir(r["scratch_dir"])

    # Segunda mensagem: já em memória, sem passar pelo arquivo.
    calls.clear()
    monkeypatch.setattr(bg_resume, "load_transcript", lambda *a, **k: pytest.fail("não deveria reler"))
    token = active_workspace.set(str(tmp_path))
    try:
        r2 = await dt._agent_message(aid, "mais um")
    finally:
        active_workspace.reset(token)
    assert r2["status"] == "completed" and "resumed_from" not in r2
    assert sum(1 for m in calls[0]["messages"] if m["role"] == "user") == 3


@pytest.mark.asyncio
async def test_revive_with_unknown_profile_errors(monkeypatch, tmp_path):
    from alpha.agents.workspace import active_workspace

    aid = _write_bg_transcript(tmp_path, agent="perfil-que-nao-existe")
    token = active_workspace.set(str(tmp_path))
    try:
        r = await dt._agent_message(aid, "oi")
    finally:
        active_workspace.reset(token)
    assert r["ok"] is False and r["category"] == "invalid_args"


@pytest.mark.asyncio
async def test_no_transcript_is_not_found(tmp_path):
    from alpha.agents.workspace import active_workspace

    token = active_workspace.set(str(tmp_path))
    try:
        r = await dt._agent_message("20261102-150000-eeee0001", "oi")
    finally:
        active_workspace.reset(token)
    assert r["ok"] is False and r["category"] == "not_found"
    assert "background" in r["error"]


@pytest.mark.asyncio
async def test_agent_list_merges_memory_and_disk(monkeypatch, tmp_path):
    import alpha.agent as agent_mod
    from alpha.agents.workspace import active_workspace

    on_disk = _write_bg_transcript(tmp_path, aid="20261102-160000-ffff0001")
    calls: list[dict] = []
    monkeypatch.setattr(agent_mod, "run_agent", _fake_run_agent(calls))
    first = await dt._run_subagent("in-process", parent_workspace=str(tmp_path))

    token = active_workspace.set(str(tmp_path))
    try:
        listed = await dt._agent_list()
        # Reviver move o id do disco para a memória — sem duplicar na lista.
        await dt._agent_message(on_disk, "continua")
        listed_after = await dt._agent_list()
    finally:
        active_workspace.reset(token)

    ids = {a["agent_id"]: a for a in listed["agents"]}
    assert set(ids) == {first["agent_id"], on_disk}
    assert ids[on_disk]["source"] == "background"
    assert "source" not in ids[first["agent_id"]]
    after = [a["agent_id"] for a in listed_after["agents"]]
    assert sorted(after) == sorted({first["agent_id"], on_disk})


# ── inbox: mensagens para um filho AINDA RODANDO ────────────────────

class TestInbox:
    def test_post_and_drain_roundtrip(self, tmp_path):
        aid = "20261102-170000-aaaa1001"
        p = bg_resume.post_message(tmp_path, aid, "primeira\x00\x1b[31m")
        bg_resume.post_message(tmp_path, aid, "segunda " + "x" * 10_000)
        assert oct(p.stat().st_mode & 0o777) == "0o600"
        assert bg_resume.pending_count(tmp_path, aid) == 2
        got = bg_resume.drain_inbox(p)
        assert got[0].startswith("primeira") and "\x00" not in got[0] and "\x1b" not in got[0]
        assert got[1].startswith("segunda ")
        assert len(got[1]) == bg_resume.INBOX_MESSAGE_CAP
        assert not p.exists() and bg_resume.drain_inbox(p) == []
        assert not list(p.parent.glob("*.read")), "arquivo temporário removido"

    def test_drain_skips_garbage_lines_and_caps(self, tmp_path, monkeypatch):
        aid = "20261102-170000-aaaa1002"
        p = bg_resume.inbox_path(tmp_path, aid)
        p.parent.mkdir(parents=True)
        p.write_text('{"text": "ok1"}\nnão é json\n{"text": 5}\n{"text": "ok2"}\n{"text": "ok3"}\n')
        monkeypatch.setattr(bg_resume, "INBOX_MAX_PENDING", 2)
        assert bg_resume.drain_inbox(p) == ["ok2", "ok3"]

    def test_unsafe_id_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            bg_resume.post_message(tmp_path, "../evil", "x")
        assert bg_resume.pending_count(tmp_path, "../evil") == 0

    def test_note_format(self):
        note = bg_resume.format_inbox_note(["a", "b"])
        assert note.startswith("[ALPHA SYSTEM NOTE] 2 new message(s) from the parent agent")
        assert "--- message 1 ---\na" in note and "--- message 2 ---\nb" in note


def _fake_stream_sequence(replies):
    """stream_chat_with_tools fake: cada chamada devolve a próxima resposta."""
    calls = []

    async def fake(messages, tools, temperature, **_kw):
        calls.append(list(messages))
        content = replies[min(len(calls) - 1, len(replies) - 1)]
        yield {"type": "final", "content": content, "tool_calls": [], "error": None}

    fake.calls = calls
    return fake


@pytest.mark.asyncio
async def test_child_loop_drains_inbox_at_top_and_before_stopping(monkeypatch, tmp_path):
    import alpha.agent as agent_mod

    aid = "20261102-170000-aaaa2001"
    inbox = bg_resume.inbox_path(tmp_path, aid)
    monkeypatch.setenv(bg_resume.INBOX_ENV, str(inbox))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    bg_resume.post_message(tmp_path, aid, "antes de começar")

    fake = _fake_stream_sequence(["resposta 1", "resposta 2"])
    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake)

    # Quando o modelo "termina" a 1ª vez, uma mensagem nova já está lá.
    orig_drain = bg_resume.drain_inbox
    state = {"posted_late": False}

    def drain_with_late_post(path):
        got = orig_drain(path)
        if not state["posted_late"] and got == ["antes de começar"]:
            state["posted_late"] = True
            bg_resume.post_message(tmp_path, aid, "chegou tarde")
        return got

    monkeypatch.setattr(bg_resume, "drain_inbox", drain_with_late_post)

    messages = [{"role": "user", "content": "tarefa"}]
    events = []
    async for ev in agent_mod.run_agent(messages=messages, user_message="tarefa", provider="deepseek"):
        events.append(ev)

    reasons = [e.get("reason") for e in events if e.get("type") == "anomaly"]
    assert reasons.count("bg_inbox_message") == 2
    assert len(fake.calls) == 2, "mensagem tardia fez o loop dar mais uma volta"
    notes = [m for m in messages if m["role"] == "user" and "from the parent agent" in m["content"]]
    assert len(notes) == 2 and "antes de começar" in notes[0]["content"]
    assert "chegou tarde" in notes[1]["content"]
    # A resposta do modelo entrou no histórico ANTES da nota tardia.
    idx = messages.index(notes[1])
    assert messages[idx - 1] == {"role": "assistant", "content": "resposta 1"}
    done = [e for e in events if e.get("type") == "done"][-1]
    assert "resposta 1" in done["reply"] and "resposta 2" in done["reply"]
    assert not inbox.exists()


@pytest.mark.asyncio
async def test_inbox_drained_even_with_bg_notifications_off(monkeypatch, tmp_path):
    # Regressão do smoke real: o filho roda em modo print, e sdk.run_task
    # passa bg_notifications=False — a inbox NÃO pode depender dessa flag.
    import alpha.agent as agent_mod

    aid = "20261102-170000-aaaa2002"
    monkeypatch.setenv(bg_resume.INBOX_ENV, str(bg_resume.inbox_path(tmp_path, aid)))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    bg_resume.post_message(tmp_path, aid, "instrução do pai")
    fake = _fake_stream_sequence(["ok"])
    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake)
    messages = [{"role": "user", "content": "x"}]
    events = [e async for e in agent_mod.run_agent(
        messages=messages, user_message="x", provider="deepseek", bg_notifications=False,
    )]
    assert any(e.get("reason") == "bg_inbox_message" for e in events)
    assert "instrução do pai" in fake.calls[0][-1]["content"]


@pytest.mark.asyncio
async def test_loop_without_inbox_env_is_untouched(monkeypatch):
    import alpha.agent as agent_mod

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    fake = _fake_stream_sequence(["ok"])
    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake)
    messages = [{"role": "user", "content": "x"}]
    events = [e async for e in agent_mod.run_agent(messages=messages, user_message="x", provider="deepseek")]
    assert not any(e.get("reason") == "bg_inbox_message" for e in events)
    assert len(fake.calls) == 1


class _FakeTask:
    def __init__(self, running):
        self.running = running


@pytest.mark.asyncio
async def test_agent_message_to_running_child_goes_to_inbox(monkeypatch, tmp_path):
    import alpha.bg_tasks as bg

    captured = {}

    def fake_start(command, cwd, env=None):
        captured.update(env=env)
        return {"task_id": 42, "pid": 1, "log": "/tmp/t.log"}

    monkeypatch.setattr(bg, "start", fake_start)
    monkeypatch.setattr(dt, "_bg_agent_tasks", {})
    r = dt._spawn_background_subagent("tarefa longa", parent_workspace=str(tmp_path))
    aid = r["agent_id"]
    assert captured["env"][bg_resume.INBOX_ENV] == str(bg_resume.inbox_path(tmp_path, aid))

    monkeypatch.setattr(bg, "get", lambda tid: _FakeTask(running=True) if tid == 42 else None)
    q = await dt._agent_message(aid, "foque só no módulo X")
    assert q["ok"] and q["status"] == "queued" and q["task_id"] == 42 and q["pending"] == 1
    assert bg_resume.drain_inbox(bg_resume.inbox_path(tmp_path, aid)) == ["foque só no módulo X"]

    listed = await dt._agent_list()
    running = [a for a in listed["agents"] if a["agent_id"] == aid]
    assert running and running[0]["last_status"] == "running" and running[0]["busy"] is True

    # Filho terminou sem transcript (ex.: crash) → not_found, não inbox.
    monkeypatch.setattr(bg, "get", lambda tid: _FakeTask(running=False))
    r2 = await dt._agent_message(aid, "ainda aí?")
    assert r2["ok"] is False and r2["category"] == "not_found"


@pytest.mark.asyncio
async def test_revive_delivers_unread_inbox_once(monkeypatch, tmp_path):
    import alpha.agent as agent_mod
    from alpha.agents.workspace import active_workspace

    aid = _write_bg_transcript(tmp_path, aid="20261102-180000-aaaa3001")
    bg_resume.post_message(tmp_path, aid, "ficou sem ler")
    calls: list[dict] = []
    monkeypatch.setattr(agent_mod, "run_agent", _fake_run_agent(calls))

    token = active_workspace.set(str(tmp_path))
    try:
        r = await dt._agent_message(aid, "e agora?")
        r2 = await dt._agent_message(aid, "mais")
    finally:
        active_workspace.reset(token)

    assert r["resumed_from"] == "background" and r["unread_inbox_delivered"] == 1
    last_user = calls[0]["messages"][-1]["content"]
    assert "ficou sem ler" in last_user and last_user.endswith("e agora?")
    assert "unread_inbox_delivered" not in r2
    assert calls[1]["messages"][-1] == {"role": "user", "content": "mais"}
    assert not bg_resume.inbox_path(tmp_path, aid).exists()
