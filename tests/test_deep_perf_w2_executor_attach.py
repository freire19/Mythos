"""DEEP_PERFORMANCE wave 2 — #DP037 (truncagem de campos nao-string) e
#DP041 (vision attachments offloaded pra thread).

Foco: comportamento preservado + propriedade de performance quando barato.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from alpha.executor import (
    TOOL_RESULT_MAX_CHARS,
    _append_vision_attachments,
    _clip_preview_value,
    _format_result,
)


# ─── #DP037 — _format_result nao serializa campos nao-string gigantes ────


class TestDP037NonStringClipping:
    def test_small_result_unchanged(self):
        result = {"ok": True, "items": [1, 2, 3], "content": "hello"}
        out = _format_result(result, "test")
        parsed = json.loads(out)
        assert parsed["ok"] is True
        assert parsed["items"] == [1, 2, 3]
        assert parsed["content"] == "hello"

    def test_truncated_preview_keeps_small_nonstring_fields(self):
        result = {
            "content": "x" * (TOOL_RESULT_MAX_CHARS + 5000),
            "ok": True,
            "count": 42,
            "items": ["a", "b"],
        }
        parsed = json.loads(_format_result(result, "test"))
        assert parsed["truncated"] is True
        assert parsed["preview"]["ok"] is True
        assert parsed["preview"]["count"] == 42
        assert parsed["preview"]["items"] == ["a", "b"]

    def test_truncated_preview_replaces_big_nonstring_with_placeholder(self):
        big_list = ["y" * 100 for _ in range(2000)]  # ~200K chars estimados
        result = {
            "content": "x" * (TOOL_RESULT_MAX_CHARS + 5000),
            "rows": big_list,
        }
        out = _format_result(result, "test")
        parsed = json.loads(out)
        assert parsed["truncated"] is True
        rows_preview = parsed["preview"]["rows"]
        assert isinstance(rows_preview, str)
        assert "omitido" in rows_preview
        assert "list" in rows_preview
        # A saida inteira cabe no limite — nada oversized foi serializado.
        assert len(out) <= TOOL_RESULT_MAX_CHARS

    def test_string_fields_still_clipped_head_tail(self):
        result = {"content": "a" * (TOOL_RESULT_MAX_CHARS + 5000)}
        parsed = json.loads(_format_result(result, "test"))
        assert "chars omitted" in parsed["preview"]["content"]
        assert len(parsed["preview"]["content"]) <= 1100

    def test_big_nonstring_field_never_fully_serialized(self):
        """Propriedade de performance: json.dumps nunca ve o dict gigante."""
        big_nested = {f"k{i}": "v" * 200 for i in range(5000)}  # ~1MB
        result = {
            "content": "x" * (TOOL_RESULT_MAX_CHARS + 5000),
            "data": big_nested,
        }
        real_dumps = json.dumps
        seen_sizes = []

        def spy(obj, **kw):
            s = real_dumps(obj, **kw)
            seen_sizes.append(len(s))
            return s

        with patch("alpha.executor.json.dumps", side_effect=spy):
            out = _format_result(result, "test")
        parsed = json.loads(out)
        assert parsed["truncated"] is True
        # Nenhuma serializacao intermediaria materializou o campo de ~1MB.
        assert all(s < 100_000 for s in seen_sizes)

    def test_clip_preview_value_placeholder_shape(self):
        v = ["z" * 500] * 100
        out = _clip_preview_value(v)
        assert isinstance(out, str)
        assert out.startswith("<list omitido")
        assert out.endswith("chars est.>")

    def test_clip_preview_value_passthrough_small(self):
        assert _clip_preview_value({"a": 1}) == {"a": 1}
        assert _clip_preview_value(42) == 42
        assert _clip_preview_value(None) is None


# ─── #DP041 — vision attachments via asyncio.to_thread ───────────────────


def _make_png(tmp_path: Path) -> Path:
    p = tmp_path / "shot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return p


class TestDP041VisionOffload:
    def test_append_vision_attachments_sync_behavior(self, tmp_path):
        """A funcao continua sync e mutando messages in-place (contrato
        exigido pelo offload via to_thread)."""
        png = _make_png(tmp_path)
        messages = [{"role": "user", "content": "hi"}]
        with patch("alpha.config.vision_tool_results_enabled", return_value=True):
            _append_vision_attachments(
                messages, [("browser_screenshot", [str(png)])], "openai"
            )
        assert len(messages) == 2
        assert messages[1]["role"] == "user"
        blocks = messages[1]["content"]
        assert isinstance(blocks, list)
        assert any(b.get("type") == "image_url" for b in blocks)

    def test_noop_without_pending_or_provider(self):
        messages = [{"role": "user", "content": "hi"}]
        _append_vision_attachments(messages, [], "openai")
        _append_vision_attachments(messages, [("t", ["/x.png"])], None)
        assert messages == [{"role": "user", "content": "hi"}]

    @pytest.mark.asyncio
    async def test_sequential_path_offloads_to_thread(self, tmp_path):
        """O call site sequencial roda o attach fora do event loop."""
        import alpha.executor as ex

        png = _make_png(tmp_path)
        calls = []
        real_to_thread = ex.asyncio.to_thread

        async def spy_to_thread(fn, *a, **kw):
            calls.append(fn)
            return await real_to_thread(fn, *a, **kw)

        async def fake_exec(tool_def, tool_name, args):
            return {"ok": True, "_image_path": str(png)}

        def get_tool(name):
            class T:
                safety = type("S", (), {"value": "safe"})()
                parameters = {"properties": {}}
                category = "test"
            return T()

        messages = []
        with patch.object(ex.asyncio, "to_thread", spy_to_thread), \
             patch.object(ex, "_execute_single_tool", fake_exec), \
             patch("alpha.config.vision_tool_results_enabled", return_value=True):
            events = []
            async for ev in ex._execute_sequential(
                [{"id": "1", "name": "browser_screenshot", "arguments": "{}"}],
                messages,
                needs_approval_fn=lambda n, a: False,
                get_tool_fn=get_tool,
                provider="openai",
            ):
                events.append(ev)

        assert ex._append_vision_attachments in calls
        # Comportamento preservado: tool_result emitido + vision msg anexada.
        assert any(e["type"] == "tool_result" for e in events)
        assert any(
            isinstance(m.get("content"), list)
            and any(b.get("type") == "image_url" for b in m["content"])
            for m in messages if m.get("role") == "user"
        )

    @pytest.mark.asyncio
    async def test_sequential_path_no_thread_without_images(self):
        """Sem imagens pendentes, nada e mandado pro thread pool."""
        import alpha.executor as ex

        calls = []
        real_to_thread = ex.asyncio.to_thread

        async def spy_to_thread(fn, *a, **kw):
            calls.append(fn)
            return await real_to_thread(fn, *a, **kw)

        async def fake_exec(tool_def, tool_name, args):
            return {"ok": True}

        def get_tool(name):
            class T:
                safety = type("S", (), {"value": "safe"})()
                parameters = {"properties": {}}
                category = "test"
            return T()

        messages = []
        with patch.object(ex.asyncio, "to_thread", spy_to_thread), \
             patch.object(ex, "_execute_single_tool", fake_exec):
            async for _ in ex._execute_sequential(
                [{"id": "1", "name": "read_file", "arguments": "{}"}],
                messages,
                needs_approval_fn=lambda n, a: False,
                get_tool_fn=get_tool,
                provider="openai",
            ):
                pass

        assert ex._append_vision_attachments not in calls
