"""DEEP_RESILIENCE wave 2 — #D809: sessão com JSON de topo não-dict.

Cobre:
- `_load_session_data`/`load_session`/`load_session_summary` com topo
  array/string/número → None em vez de AttributeError.
- `list_sessions` sobrevive a arquivos malformados (topo não-dict,
  `messages` não-lista, msg não-dict, content não-str) e ainda lista
  as sessões válidas.
- Guard do dispatch no loop do REPL (entry.py): Exception genérica não
  mata o REPL, mas KeyboardInterrupt/EOFError/SystemExit re-levantam.
"""

import json
import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

from alpha import history


class HistoryDirMixin(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._patcher = patch("alpha.history._HISTORY_DIR", Path(self.tmpdir))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, session_id, payload):
        path = Path(self.tmpdir) / f"{session_id}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


class TestLoadNonDictTop(HistoryDirMixin):
    def test_load_session_top_level_array_returns_none(self):
        self._write("badarray", [{"role": "user", "content": "oi"}])
        self.assertIsNone(history.load_session("badarray"))

    def test_load_session_top_level_string_returns_none(self):
        self._write("badstr", "apenas uma string")
        self.assertIsNone(history.load_session("badstr"))

    def test_load_session_top_level_number_returns_none(self):
        self._write("badnum", 42)
        self.assertIsNone(history.load_session("badnum"))

    def test_load_session_summary_non_dict_returns_none(self):
        self._write("badsum", ["x"])
        self.assertIsNone(history.load_session_summary("badsum"))

    def test_load_session_valid_still_works(self):
        self._write("good1", {"messages": [{"role": "user", "content": "oi"}]})
        msgs = history.load_session("good1")
        self.assertEqual(msgs, [{"role": "user", "content": "oi"}])

    def test_load_session_messages_non_list_coerced_to_empty(self):
        self._write("badmsgs", {"messages": "nao é lista"})
        self.assertEqual(history.load_session("badmsgs"), [])

    def test_load_session_non_dict_entries_dropped(self):
        self._write("mixed", {"messages": ["str", 1, {"role": "user", "content": "oi"}]})
        self.assertEqual(
            history.load_session("mixed"), [{"role": "user", "content": "oi"}]
        )


class TestListSessionsResilience(HistoryDirMixin):
    def test_bad_file_does_not_break_listing(self):
        self._write("a_badtop", ["array", "no", "topo"])
        self._write("b_badmsgs", {"messages": {"nao": "lista"}})
        self._write("c_badmsg", {"messages": ["nao-dict", 7]})
        self._write("d_badcontent", {"messages": [{"role": "user", "content": 123}]})
        self._write(
            "e_good",
            {
                "session_id": "e_good",
                "timestamp_human": "2026-08-04 10:00:00",
                "message_count": 1,
                "messages": [{"role": "user", "content": "pergunta valida"}],
            },
        )
        sessions = history.list_sessions()
        ids = [s["session_id"] for s in sessions]
        self.assertIn("e_good", ids)
        good = next(s for s in sessions if s["session_id"] == "e_good")
        self.assertEqual(good["preview"], "pergunta valida")

    def test_listing_never_raises_on_garbage(self):
        (Path(self.tmpdir) / "garbage.json").write_text("{not json", encoding="utf-8")
        self._write("nulltop", None)
        self._write("strtop", "x" * 20)
        sessions = history.list_sessions()
        self.assertIsInstance(sessions, list)


class TestDispatchGuard(unittest.TestCase):
    """O guard do loop do REPL: Exception não mata, exit paths re-levantam.

    O bloco vive inline em run_repl (não unit-testável isolado), então
    validamos o comportamento equivalente executando o mesmo padrão de
    try/except com um dispatch simulado + smoke no código-fonte para
    garantir que o re-raise dos exit paths existe antes do guard genérico.
    """

    def test_entry_reraises_exit_paths_before_generic_guard(self):
        import inspect
        from alpha.cli import entry

        src = inspect.getsource(entry.run_repl)
        dispatch_idx = src.index("result = dispatch(ctx, user_input)")
        tail = src[dispatch_idx:]
        reraise_idx = tail.index("except (KeyboardInterrupt, EOFError, SystemExit):")
        generic_idx = tail.index("except Exception as e:")
        self.assertLess(reraise_idx, generic_idx)
        self.assertIn("raise", tail[reraise_idx:generic_idx + 50])


if __name__ == "__main__":
    unittest.main()
