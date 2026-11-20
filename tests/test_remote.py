"""Remote control (alpha/remote.py): server, auth, inbox, questions, mirror."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from alpha import remote


@pytest.fixture
def server():
    remote.stop()
    st = remote.start(host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{st['port']}"
    token = st["url"].split("#", 1)[1]
    yield base, token
    remote.stop()


def _post(base, path, token=None, body=None):
    req = urllib.request.Request(
        base + path, data=json.dumps(body if body is not None else {}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _read_events(base, token, n, since=0, timeout=5):
    req = urllib.request.Request(
        f"{base}/api/events?since={since}", headers={"Authorization": f"Bearer {token}"}
    )
    out = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        while len(out) < n:
            line = r.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                out.append(json.loads(line[6:]))
    return out


class TestServer:
    def test_inactive_helpers_are_noops(self):
        remote.stop()
        assert not remote.is_active()
        assert remote.pop_message() is None
        assert remote.ask("choice", "q", "", ["a"]) is None
        assert remote.turn_mirror() is None
        assert remote.clear_inbox() == 0
        remote.publish({"type": "note"})
        with remote.capture_output("/help"):
            pass
        assert remote.status() == {"active": False}

    def test_page_is_static_and_carries_no_token(self, server):
        base, token = server
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            body = r.read().decode()
            assert "default-src 'none'" in r.headers["Content-Security-Policy"]
            assert r.headers["Cache-Control"] == "no-store"
        assert "Mythos" in body and token not in body

    def test_url_keeps_token_in_fragment(self, server):
        assert "/#" in remote.status()["url"] and "?" not in remote.status()["url"]
        assert remote.status()["loopback"] is True

    def test_api_requires_token(self, server):
        base, token = server
        assert _post(base, "/api/message", None, {"text": "oi"})[0] == 401
        assert _post(base, "/api/message", "wrong", {"text": "oi"})[0] == 401
        assert _post(base, "/api/interrupt", token[:-1])[0] == 401
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/events", timeout=5)
        assert e.value.code == 401
        assert remote.pop_message() is None

    def test_message_reaches_inbox_and_wakes(self, server):
        base, token = server
        woke = threading.Event()
        remote.set_waker(woke.set)
        assert _post(base, "/api/message", token, {"text": "  rode os testes \n"}) == (200, {"ok": True})
        assert woke.is_set()
        assert remote.has_messages()
        assert remote.pop_message() == "rode os testes"
        assert remote.pop_message() is None

    def test_message_validation(self, server):
        base, token = server
        assert _post(base, "/api/message", token, {"text": "   "})[0] == 400
        assert _post(base, "/api/message", token, {"text": 5})[0] == 400
        assert _post(base, "/api/message", token, {"text": "x" * 20000})[0] == 400
        # `!cmd` runs a shell command with no approval gate: terminal only.
        code, body = _post(base, "/api/message", token, {"text": "!rm -rf x"})
        assert code == 400 and "terminal" in body["error"]
        assert remote.pop_message() is None
        # Slash commands are fine.
        assert _post(base, "/api/message", token, {"text": "/cost"})[0] == 200

    def test_events_hello_backlog_and_since(self, server):
        base, token = server
        remote.set_info(model="m1", cwd="/w")
        remote.publish_user("primeira")
        remote.publish({"type": "assistant", "text": "resposta"})
        evs = _read_events(base, token, 3)
        assert evs[0]["type"] == "hello" and evs[0]["info"] == {"model": "m1", "cwd": "/w"}
        assert [e["type"] for e in evs[1:]] == ["user", "assistant"]
        assert evs[2]["seq"] == evs[1]["seq"] + 1
        later = _read_events(base, token, 2, since=evs[1]["seq"])
        assert [e["type"] for e in later] == ["hello", "assistant"]

    def test_live_events_and_non_persistent_delta(self, server):
        base, token = server
        got = []
        t = threading.Thread(target=lambda: got.extend(_read_events(base, token, 3)))
        t.start()
        for _ in range(50):
            if remote.status()["clients"]:
                break
            time.sleep(0.02)
        remote.publish({"type": "delta", "text": "ol"}, persist=False)
        remote.publish({"type": "assistant", "text": "olá"})
        t.join(5)
        assert [e["type"] for e in got] == ["hello", "delta", "assistant"]
        assert "seq" not in got[1] and got[2]["seq"] == 1

    def test_stop_invalidates_link(self, server):
        base, token = server
        assert remote.stop() is True
        with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
            for _ in range(20):
                _post(base, "/api/message", token, {"text": "oi"})
                time.sleep(0.05)
        st = remote.start(host="127.0.0.1", port=0)
        assert st["url"].split("#", 1)[1] != token


class TestQuestions:
    def test_remote_answer_sets_event(self, server):
        base, token = server
        q = remote.ask("approval", "Bash (execute_shell)", "command: ls", ["Sim", "Não"])
        hello = _read_events(base, token, 1)[0]
        assert hello["question"]["id"] == q.id and hello["question"]["options"] == ["Sim", "Não"]
        assert _post(base, "/api/answer", token, {"id": q.id, "index": 5})[1] == {"ok": False}
        assert _post(base, "/api/answer", token, {"id": "nope", "index": 0})[1] == {"ok": False}
        assert _post(base, "/api/answer", token, {"id": q.id, "index": True})[0] == 400
        assert not q.event.is_set()
        assert _post(base, "/api/answer", token, {"id": q.id, "index": 1})[1] == {"ok": True}
        assert q.event.is_set() and q.answer == 1
        # First answer wins.
        assert _post(base, "/api/answer", token, {"id": q.id, "index": 0})[1] == {"ok": False}
        remote.close_question(q, "✗ Negado")
        evs = _read_events(base, token, 3)
        assert evs[0]["question"] is None
        assert evs[2]["type"] == "question_closed" and evs[2]["source"] == "remote"

    def test_terminal_answer_closes_question(self, server):
        base, token = server
        q = remote.ask("choice", "Qual?", "", ["a", "b"])
        remote.close_question(q, "✓ a")
        assert _post(base, "/api/answer", token, {"id": q.id, "index": 0})[1] == {"ok": False}
        evs = _read_events(base, token, 3)
        assert evs[2]["source"] == "terminal" and evs[2]["outcome"] == "✓ a"

    def test_interrupt_cancels_menu_then_signals(self, server, monkeypatch):
        base, token = server
        kills = []
        monkeypatch.setattr(remote.os, "kill", lambda pid, sig: kills.append(sig))
        monkeypatch.setattr(remote.time, "sleep", lambda s: None)
        # Idle: nothing to interrupt, no signal.
        assert _post(base, "/api/interrupt", token)[1] == {"ok": False}
        mirror = remote.turn_mirror()
        q = remote.ask("approval", "t", "", ["Sim", "Não"])
        assert _post(base, "/api/interrupt", token)[1] == {"ok": True}
        assert q.cancelled and q.event.is_set() and q.answer is None
        assert kills == [remote.signal.SIGINT]
        mirror.finish(aborted=True)
        assert _post(base, "/api/interrupt", token)[1] == {"ok": False}
        assert len(kills) == 1

    def test_approval_prompt_takes_remote_answer(self, server, monkeypatch, capsys):
        from alpha.display.renderers import prompts
        from alpha.display.select import MenuCancelled

        base, token = server
        monkeypatch.setattr(prompts, "_approve_all", False)
        monkeypatch.setattr(prompts, "can_select", lambda: True)

        def fake_menu(options, shortcuts=None, hint="", cancel=None):
            assert cancel is not None
            hello = _read_events(base, token, 1)[0]
            assert "execute_shell" in hello["question"]["title"]
            assert "rm -rf build" in hello["question"]["detail"]
            _post(base, "/api/answer", token, {"id": hello["question"]["id"], "index": 3})
            assert cancel.wait(2)
            raise MenuCancelled

        monkeypatch.setattr(prompts, "select_menu", fake_menu)
        assert prompts.print_approval_request("execute_shell", {"command": "rm -rf build"}) is False
        assert "controle remoto" in capsys.readouterr().out

        def fake_menu_yes(options, shortcuts=None, hint="", cancel=None):
            q = _read_events(base, token, 1)[0]["question"]
            _post(base, "/api/answer", token, {"id": q["id"], "index": 0})
            assert cancel.wait(2)
            raise MenuCancelled

        monkeypatch.setattr(prompts, "select_menu", fake_menu_yes)
        assert prompts.print_approval_request("execute_shell", {"command": "ls"}) is True
        # Question is closed either way.
        assert _read_events(base, token, 1)[0]["question"] is None

    def test_approval_prompt_remote_interrupt_denies(self, server, monkeypatch):
        from alpha.display.renderers import prompts
        from alpha.display.select import MenuCancelled

        monkeypatch.setattr(prompts, "_approve_all", False)
        monkeypatch.setattr(prompts, "can_select", lambda: True)

        def fake_menu(options, shortcuts=None, hint="", cancel=None):
            cancel.set()  # what hub.interrupt() does, with no answer
            raise MenuCancelled

        monkeypatch.setattr(prompts, "select_menu", fake_menu)
        assert prompts.print_approval_request("execute_shell", {"command": "ls"}) is False

    def test_ask_choice_takes_remote_answer(self, server, monkeypatch):
        import asyncio

        from alpha.display import select as sel
        from alpha.tools import interaction_tools as it

        base, token = server
        monkeypatch.setattr(sel, "can_select", lambda: True)

        def fake_menu(o_lines, cancel=None):
            q = _read_events(base, token, 1)[0]["question"]
            assert q["kind"] == "choice" and q["options"] == ["um", "dois"]
            _post(base, "/api/answer", token, {"id": q["id"], "index": 1})
            assert cancel.wait(2)
            raise sel.MenuCancelled

        monkeypatch.setattr(sel, "select_menu", fake_menu)
        res = asyncio.run(it._ask_choice("Qual?", ["um", "dois"]))
        assert res == {"ok": True, "chosen_index": 2, "chosen_value": "dois"}


class TestMirror:
    def test_turn_events(self, server):
        base, token = server
        m = remote.turn_mirror()
        for ev in (
            {"type": "token", "text": "Vou "},
            {"type": "token", "text": "ler."},
            {"type": "tool_call", "id": "c1", "name": "read_file", "args": {"path": "a.py"}},
            {"type": "tool_result", "id": "c1", "name": "read_file",
             "result": {"ok": True, "content": "x\ny\n", "total_lines": 2}},
            {"type": "tool_call", "id": "c2", "name": "execute_shell", "args": {"command": "false"}},
            {"type": "tool_result", "id": "c2", "name": "execute_shell",
             "result": {"ok": False, "error": "exit \x1b[31m1\x1b[0m"}},
            {"type": "tool_call", "id": "c3", "name": "ask_choice", "args": {}},
            {"type": "tool_result", "id": "c3", "name": "ask_choice", "result": {"ok": True}},
            {"type": "token", "text": "Pronto."},
            {"type": "done", "reply": "ignored"},
        ):
            m.feed(ev)
        m.finish(tools=1, duration=2.34)
        evs = _read_events(base, token, 9)
        kinds = [e["type"] for e in evs[1:]]
        assert kinds == ["status", "assistant", "tool_call", "tool_result",
                         "tool_call", "tool_result", "assistant", "turn_end"]
        assert evs[2]["text"] == "Vou ler."
        assert "a.py" in evs[3]["header"] and "\x1b" not in evs[3]["header"]
        assert evs[4] == {**evs[4], "id": "c1", "text": "Read 2 lines", "ok": True}
        assert evs[6]["text"] == "Error: exit 1" and evs[6]["ok"] is False
        assert evs[7]["text"] == "Pronto."
        assert evs[8]["tools"] == 1 and evs[8]["duration"] == 2.3
        assert _read_events(base, token, 1)[0]["busy"] is False

    def test_done_reply_used_when_nothing_streamed(self, server):
        base, token = server
        m = remote.turn_mirror()
        m.feed({"type": "done", "reply": "resposta inteira"})
        m.finish()
        evs = _read_events(base, token, 3)
        assert evs[2]["type"] == "assistant" and evs[2]["text"] == "resposta inteira"

    def test_mirror_never_raises(self, server):
        m = remote.turn_mirror()
        m.feed({"type": "tool_result"})
        m.feed({"type": "tool_call", "args": object()})
        m.finish()

    def test_capture_output_strips_ansi(self, server, capsys):
        base, token = server
        with remote.capture_output("/cost"):
            print("\x1b[36mTotal:\x1b[0m $0.10")
        assert "Total:" in capsys.readouterr().out
        evs = _read_events(base, token, 3)
        assert evs[1] == {**evs[1], "type": "user", "text": "/cost", "source": "command"}
        assert evs[2]["type"] == "output" and evs[2]["text"] == "Total: $0.10"

    def test_clear_command_clears_page(self, server):
        base, token = server
        with remote.capture_output("/clear"):
            print("tela limpa")
        assert _read_events(base, token, 3)[2]["type"] == "clear"

    def test_plain_keeps_last_carriage_return_frame(self):
        assert remote._plain("a\rb\x1b[2K\x07c\n") == "bc\n"


class TestReplIntegration:
    def test_read_input_returns_pending_remote_message(self, server, monkeypatch, capsys):
        from alpha import repl_input

        base, token = server
        _post(base, "/api/message", token, {"text": "do celular"})
        monkeypatch.setattr(repl_input, "_pop_scheduled", lambda: None)
        assert repl_input.read_input("> ") == ("do celular", [])
        assert "remoto" in capsys.readouterr().out

    def test_command_registered(self):
        from alpha.cli.commands import _DESCRIPTIONS, _DISPATCH, remote_cmd

        assert _DISPATCH["/remote"] is remote_cmd._handle_remote and "/remote" in _DESCRIPTIONS

    def test_command_refuses_without_interactive_terminal(self, monkeypatch, capsys):
        from alpha.cli.commands import remote_cmd
        from alpha.display import select as sel

        remote.stop()
        monkeypatch.setattr(sel, "can_select", lambda: False)
        assert remote_cmd.start_remote() is False
        assert not remote.is_active()
        assert "interativo" in capsys.readouterr().out

    def test_command_on_status_off(self, monkeypatch, capsys):
        from types import SimpleNamespace

        from alpha import repl_input
        from alpha.cli.commands import remote_cmd
        from alpha.display import select as sel

        remote.stop()
        monkeypatch.setattr(sel, "can_select", lambda: True)
        monkeypatch.setattr(repl_input, "use_simple_input", lambda: False)
        monkeypatch.setenv("ALPHA_REMOTE_PORT", "0")
        ctx = SimpleNamespace(cfg={"model": "m"}, history=[
            {"role": "user", "content": "antes"}, {"role": "assistant", "content": "ok"},
        ])
        try:
            remote_cmd._handle_remote(ctx, ["/remote", "on", "--local"])
            st = remote.status()
            assert st["active"] and st["loopback"]
            out = capsys.readouterr().out
            assert st["url"] in out and "aprova comandos" in out
            token = st["url"].split("#", 1)[1]
            evs = _read_events(f"http://127.0.0.1:{st['port']}", token, 3)
            assert [e.get("text") for e in evs[1:]] == ["antes", "ok"]
            remote_cmd._handle_remote(ctx, ["/remote", "off"])
            assert not remote.is_active()
        finally:
            remote.stop()

    def test_qr_lines_are_rectangular(self):
        lines = remote.qr_lines("http://192.168.0.10:7333/#" + "a" * 32)
        if not lines:
            pytest.skip("qrcode not installed")
        assert len({len(ln) for ln in lines}) == 1 and len(lines) >= 10

    def test_firewall_hint_scopes_rule_to_lan(self, monkeypatch, tmp_path):
        import builtins

        from alpha.cli.commands import remote_cmd

        conf = tmp_path / "ufw.conf"
        real_open = builtins.open

        def fake_open(path, *a, **k):
            return real_open(conf if path == "/etc/ufw/ufw.conf" else path, *a, **k)

        monkeypatch.setattr(builtins, "open", fake_open)
        conf.write_text("# x\nENABLED=yes\n")
        assert remote_cmd._firewall_hint("192.168.15.7", 7333) == (
            "sudo ufw allow from 192.168.15.0/24 to any port 7333 proto tcp"
        )
        conf.write_text("ENABLED=no\n")
        assert remote_cmd._firewall_hint("192.168.15.7", 7333) is None
        conf.unlink()
        assert remote_cmd._firewall_hint("192.168.15.7", 7333) is None
