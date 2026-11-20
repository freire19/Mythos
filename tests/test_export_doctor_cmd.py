"""`/export` e `/doctor`.

Contratos:
- /export renderiza user/assistant/tool_calls/tool results (clipados, fence
  seguro), tira o prefixo [CWD], omite o system, não sobrescreve sem
  --force, aceita diretório como alvo;
- /doctor nunca levanta: cada check isolado vira aviso; nunca imprime o
  valor de uma chave; status refletem presença de bins e da chave;
- ambos no catálogo e no dispatch.
"""

from __future__ import annotations

import os

import pytest

from alpha.cli.commands import DispatchResult, _DESCRIPTIONS, _DISPATCH, doctor_cmd, export_cmd
from alpha.cli.commands._types import ReplContext


def _ctx(messages=None) -> ReplContext:
    return ReplContext(
        messages=messages if messages is not None else [{"role": "system", "content": "sys"}],
        history=[], session_id="20261103-120000", provider="deepseek", temperature=0.2,
        cfg={"model": "deepseek-v4-pro"}, system_prompt="sys", tools=[], get_tool_fn=None,
        active_agent=None,
    )


_CONVO = [
    {"role": "system", "content": "SECRET SYSTEM PROMPT"},
    {"role": "user", "content": "[CWD: /home/x/proj]\nlista os arquivos"},
    {"role": "assistant", "content": "Vou listar.", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "list_directory", "arguments": '{"path": "."}'}},
    ]},
    {"role": "tool", "tool_call_id": "c1", "content": "a.py\n```\nb.py\n" + "z" * 2000},
    {"role": "assistant", "content": "Dois arquivos: a.py e b.py‮."},
    {"role": "user", "content": [{"type": "text", "text": "e essa imagem?"},
                                 {"type": "image_url", "image_url": {"url": "data:..."}}]},
]


class TestExport:
    def test_render(self):
        md = export_cmd.render_markdown(_ctx(list(_CONVO)), now=0)
        assert md.startswith("# Alpha session 20261103-120000")
        assert "SECRET SYSTEM PROMPT" not in md
        assert "## User\n\nlista os arquivos" in md and "[CWD:" not in md
        assert "**→ `list_directory`** `{\"path\": \".\"}`" in md
        assert "<summary>← <code>list_directory</code> result</summary>" in md
        assert "chars omitted]" in md  # 2000-char result clipped
        assert "````\na.py\n```\nb.py" in md  # fence longer than the inner ```
        assert "‮" not in md
        assert "_[1 image(s) attached]_" in md
        assert "provider `deepseek` · model `deepseek-v4-pro` · 5 messages" in md

    def test_writes_default_file(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert export_cmd._handle_export(_ctx(list(_CONVO)), ["/export"]) is DispatchResult.CONTINUE
        files = list(tmp_path.glob("alpha-session-*.md"))
        assert len(files) == 1 and "## User" in files[0].read_text()
        assert "/export" in capsys.readouterr().out

    def test_explicit_path_dir_and_force(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "out" / "chat.md"
        export_cmd._handle_export(_ctx(list(_CONVO)), ["/export", str(target)])
        assert target.exists()
        target.write_text("keep")
        export_cmd._handle_export(_ctx(list(_CONVO)), ["/export", str(target)])
        assert target.read_text() == "keep" and "já existe" in capsys.readouterr().out
        export_cmd._handle_export(_ctx(list(_CONVO)), ["/export", str(target), "--force"])
        assert target.read_text().startswith("# Alpha session")
        d = tmp_path / "dir"
        d.mkdir()
        export_cmd._handle_export(_ctx(list(_CONVO)), ["/export", str(d)])
        assert list(d.glob("alpha-session-*.md"))

    def test_empty_and_usage(self, capsys):
        export_cmd._handle_export(_ctx(), ["/export"])
        assert "Nada para exportar" in capsys.readouterr().out
        export_cmd._handle_export(_ctx(list(_CONVO)), ["/export", "a", "b"])
        assert "Usage" in capsys.readouterr().out


class TestDoctor:
    def test_runs_and_summarizes(self, monkeypatch, capsys):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-super-secret")
        monkeypatch.setattr(doctor_cmd.shutil, "which", lambda n: "/usr/bin/" + n if n in ("git",) else None)
        assert doctor_cmd._handle_doctor(_ctx(), ["/doctor"]) is DispatchResult.CONTINUE
        out = capsys.readouterr().out
        assert "sk-super-secret" not in out
        assert "DEEPSEEK_API_KEY definida" in out
        assert "GitHub CLI" in out  # gh missing → hint
        assert "verificações" in out

    def test_missing_key_is_warning(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        results = {r[1]: r for r in doctor_cmd.run_checks(_ctx())}
        assert results["Provider"][0] == "warn" and "NÃO definida" in results["Provider"][2]
        assert results["Python"][0] == "ok"
        assert results["git"][0] in ("ok", "warn")

    def test_broken_check_becomes_warning(self, monkeypatch):
        def boom():
            raise RuntimeError("kaput")

        monkeypatch.setattr(doctor_cmd, "_sandbox", boom)
        results = doctor_cmd.run_checks(_ctx())
        bad = [r for r in results if "kaput" in r[2]]
        assert bad and bad[0][0] == "warn"

    def test_settings_invalid_json(self, tmp_path, monkeypatch):
        (tmp_path / ".alpha").mkdir()
        (tmp_path / ".alpha" / "settings.json").write_text("{nope")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("alpha.settings.find_config_file", lambda name: tmp_path / ".alpha" / name if name == "settings.json" else None)
        status, _, detail = doctor_cmd._settings()
        assert status == "warn" and "inválido" in detail

    def test_in_catalogue_and_dispatch(self):
        assert _DISPATCH["/export"] is export_cmd._handle_export and "/export" in _DESCRIPTIONS
        assert _DISPATCH["/doctor"] is doctor_cmd._handle_doctor and "/doctor" in _DESCRIPTIONS


@pytest.mark.skipif(os.name != "posix", reason="posix paths")
def test_export_relpath_message(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    export_cmd._handle_export(_ctx(list(_CONVO)), ["/export", "notes.md"])
    assert "notes.md (" in capsys.readouterr().out
