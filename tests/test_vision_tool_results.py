"""Plano-Browser Etapa 1 — screenshots multimodais.

Cobre o caminho completo: tool retorna `_image_path` → executor anexa a
imagem como mensagem user de visão (gated por provider) → adapter Anthropic
faz merge de user turns consecutivos mantendo tool_result primeiro.
"""

import base64

import pytest

import alpha.config as config_mod
from alpha.executor import (
    _append_vision_attachments,
    _collect_result_images,
    execute_tool_calls,
)
from alpha.llm_anthropic import _convert_messages

# 1x1 PNG válido (67 bytes).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
    "YGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
)


def _write_png(tmp_path, name="shot.png"):
    p = tmp_path / name
    p.write_bytes(_PNG)
    return p


class _Tool:
    """ToolDefinition mínima para o executor."""

    def __init__(self, fn, parameters=None):
        self.executor = fn
        self.parameters = parameters or {"type": "object", "properties": {}}
        self.safety = None  # -> safety_str "safe"


def _no_approval(_name, _args):
    return False


async def _drain(gen):
    return [e async for e in gen]


# ── _collect_result_images ──


class TestCollectResultImages:
    def test_single_path(self):
        assert _collect_result_images({"_image_path": "/a.png"}) == ["/a.png"]

    def test_list_of_paths(self):
        result = {"_image_paths": ["/a.png", "/b.png", 3, ""]}
        assert _collect_result_images(result) == ["/a.png", "/b.png"]

    def test_none_and_non_dict(self):
        assert _collect_result_images(None) == []
        assert _collect_result_images("x") == []
        assert _collect_result_images({"saved_to": "/a.png"}) == []


# ── _append_vision_attachments ──


class TestAppendVisionAttachments:
    def test_appends_openai_vision_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            config_mod, "vision_tool_results_enabled", lambda p: True
        )
        png = _write_png(tmp_path)
        messages = []
        _append_vision_attachments(
            messages, [("browser_screenshot", [str(png)])], "anthropic"
        )
        assert len(messages) == 1
        msg = messages[0]
        assert msg["role"] == "user"
        types = [b["type"] for b in msg["content"]]
        assert types == ["text", "image_url"]
        assert "browser_screenshot" in msg["content"][0]["text"]
        assert msg["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )

    def test_no_provider_is_noop(self, tmp_path):
        png = _write_png(tmp_path)
        messages = []
        _append_vision_attachments(messages, [("t", [str(png)])], None)
        assert messages == []

    def test_vision_disabled_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            config_mod, "vision_tool_results_enabled", lambda p: False
        )
        png = _write_png(tmp_path)
        messages = []
        _append_vision_attachments(messages, [("t", [str(png)])], "deepseek")
        assert messages == []

    def test_missing_image_skipped_entirely(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            config_mod, "vision_tool_results_enabled", lambda p: True
        )
        messages = []
        _append_vision_attachments(
            messages, [("t", [str(tmp_path / "nope.png")])], "anthropic"
        )
        # Sem imagem legível não pode sobrar mensagem só de texto.
        assert messages == []


# ── executor end-to-end (sequencial e paralelo) ──


class TestExecutorVisionInjection:
    @pytest.mark.asyncio
    async def test_sequential_appends_after_tool_msg(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            config_mod, "vision_tool_results_enabled", lambda p: True
        )
        png = _write_png(tmp_path)

        async def shot():
            return {"saved_to": str(png), "_image_path": str(png)}

        tools = {"browser_screenshot": _Tool(shot)}
        messages = []
        events = await _drain(execute_tool_calls(
            [{"id": "c1", "name": "browser_screenshot", "arguments": "{}"}],
            messages,
            needs_approval_fn=_no_approval,
            get_tool_fn=tools.get,
            provider="anthropic",
        ))
        assert any(e["type"] == "tool_result" for e in events)
        assert [m["role"] for m in messages] == ["tool", "user"]
        blocks = messages[1]["content"]
        assert blocks[-1]["type"] == "image_url"
        # O JSON textual do tool result não deve conter o path duplicado
        # via _image_path (chave _ é ocultada).
        assert "_image_path" not in messages[0]["content"]

    @pytest.mark.asyncio
    async def test_sequential_no_vision_provider(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            config_mod, "vision_tool_results_enabled", lambda p: False
        )
        png = _write_png(tmp_path)

        async def shot():
            return {"saved_to": str(png), "_image_path": str(png)}

        tools = {"browser_screenshot": _Tool(shot)}
        messages = []
        await _drain(execute_tool_calls(
            [{"id": "c1", "name": "browser_screenshot", "arguments": "{}"}],
            messages,
            needs_approval_fn=_no_approval,
            get_tool_fn=tools.get,
            provider="deepseek",
        ))
        assert [m["role"] for m in messages] == ["tool"]

    @pytest.mark.asyncio
    async def test_parallel_batch_images_after_all_tool_msgs(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            config_mod, "vision_tool_results_enabled", lambda p: True
        )
        png1 = _write_png(tmp_path, "a.png")
        png2 = _write_png(tmp_path, "b.png")

        async def shot_a():
            return {"saved_to": str(png1), "_image_path": str(png1)}

        async def shot_b():
            return {"saved_to": str(png2), "_image_path": str(png2)}

        async def plain():
            return {"ok": True}

        tools = {
            "shot_a": _Tool(shot_a),
            "shot_b": _Tool(shot_b),
            "plain": _Tool(plain),
        }
        messages = []
        await _drain(execute_tool_calls(
            [
                {"id": "c1", "name": "shot_a", "arguments": "{}"},
                {"id": "c2", "name": "plain", "arguments": "{}"},
                {"id": "c3", "name": "shot_b", "arguments": "{}"},
            ],
            messages,
            needs_approval_fn=_no_approval,
            get_tool_fn=tools.get,
            provider="anthropic",
        ))
        roles = [m["role"] for m in messages]
        # Todas as tool messages primeiro, depois as mensagens de visão.
        assert roles == ["tool", "tool", "tool", "user", "user"]

    @pytest.mark.asyncio
    async def test_no_provider_param_keeps_old_wire(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            config_mod, "vision_tool_results_enabled", lambda p: True
        )
        png = _write_png(tmp_path)

        async def shot():
            return {"saved_to": str(png), "_image_path": str(png)}

        tools = {"browser_screenshot": _Tool(shot)}
        messages = []
        await _drain(execute_tool_calls(
            [{"id": "c1", "name": "browser_screenshot", "arguments": "{}"}],
            messages,
            needs_approval_fn=_no_approval,
            get_tool_fn=tools.get,
        ))
        assert [m["role"] for m in messages] == ["tool"]


class TestVisionPruning:
    def _vision_msg(self, tag):
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": f"[ALPHA SYSTEM NOTE] Image produced by tool 'browser_screenshot' ({tag})"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }

    def test_old_screenshots_pruned_on_new_attach(self, tmp_path, monkeypatch):
        from alpha.executor import _append_vision_attachments

        monkeypatch.setattr(
            config_mod, "vision_tool_results_enabled", lambda p: True
        )
        monkeypatch.setenv("ALPHA_VISION_KEEP_IMAGES", "2")
        png = _write_png(tmp_path)

        messages = [
            {"role": "user", "content": "abra o site"},
            self._vision_msg("s1"),
            self._vision_msg("s2"),
            self._vision_msg("s3"),
        ]
        _append_vision_attachments(
            messages, [("browser_screenshot", [str(png)])], "anthropic"
        )
        # Após o append: s1 e s2 viraram texto; s3 + novo mantêm imagem.
        def has_image(m):
            return isinstance(m.get("content"), list) and any(
                b.get("type") == "image_url" for b in m["content"]
            )

        with_image = [m for m in messages if has_image(m)]
        assert len(with_image) == 2
        assert isinstance(messages[1]["content"], str)
        assert "screenshot removed" in messages[1]["content"]
        assert isinstance(messages[2]["content"], str)
        # A mensagem do usuário original fica intacta.
        assert messages[0]["content"] == "abra o site"

    def test_user_pasted_images_never_pruned(self, tmp_path, monkeypatch):
        from alpha.executor import _append_vision_attachments

        monkeypatch.setattr(
            config_mod, "vision_tool_results_enabled", lambda p: True
        )
        monkeypatch.setenv("ALPHA_VISION_KEEP_IMAGES", "1")
        png = _write_png(tmp_path)

        pasted = {
            "role": "user",
            "content": [
                {"type": "text", "text": "olha esse print que EU colei"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
            ],
        }
        messages = [pasted, self._vision_msg("s1")]
        _append_vision_attachments(
            messages, [("browser_screenshot", [str(png)])], "anthropic"
        )
        # A imagem colada pelo usuário sobrevive; só a do tool foi podada.
        assert any(b.get("type") == "image_url" for b in messages[0]["content"])
        assert isinstance(messages[1]["content"], str)

    def test_prune_disabled_with_zero(self, tmp_path, monkeypatch):
        from alpha.executor import _append_vision_attachments

        monkeypatch.setattr(
            config_mod, "vision_tool_results_enabled", lambda p: True
        )
        monkeypatch.setenv("ALPHA_VISION_KEEP_IMAGES", "0")
        png = _write_png(tmp_path)

        messages = [self._vision_msg("s1"), self._vision_msg("s2")]
        _append_vision_attachments(
            messages, [("browser_screenshot", [str(png)])], "anthropic"
        )
        imgs = [
            m for m in messages
            if isinstance(m.get("content"), list)
            and any(b.get("type") == "image_url" for b in m["content"])
        ]
        assert len(imgs) == 3  # nada podado


# ── config.vision_tool_results_enabled ──


class TestVisionFlag:
    def test_auto_follows_provider_flag(self, monkeypatch):
        monkeypatch.delenv("ALPHA_VISION_TOOL_RESULTS", raising=False)
        monkeypatch.setattr(
            config_mod, "get_provider_config",
            lambda p: {"supports_vision": p == "anthropic"},
        )
        assert config_mod.vision_tool_results_enabled("anthropic") is True
        assert config_mod.vision_tool_results_enabled("deepseek") is False

    def test_env_off_wins(self, monkeypatch):
        monkeypatch.setenv("ALPHA_VISION_TOOL_RESULTS", "off")
        monkeypatch.setattr(
            config_mod, "get_provider_config",
            lambda p: {"supports_vision": True},
        )
        assert config_mod.vision_tool_results_enabled("anthropic") is False

    def test_env_on_forces(self, monkeypatch):
        monkeypatch.setenv("ALPHA_VISION_TOOL_RESULTS", "1")
        monkeypatch.setattr(
            config_mod, "get_provider_config",
            lambda p: {"supports_vision": False},
        )
        assert config_mod.vision_tool_results_enabled("ollama") is True

    def test_unknown_provider_fail_closed(self, monkeypatch):
        monkeypatch.delenv("ALPHA_VISION_TOOL_RESULTS", raising=False)

        def boom(_p):
            raise RuntimeError("unknown provider")

        monkeypatch.setattr(config_mod, "get_provider_config", boom)
        assert config_mod.vision_tool_results_enabled("nope") is False


# ── adapter Anthropic: merge de user turns consecutivos ──


class TestAnthropicUserMerge:
    def test_tool_then_vision_user_becomes_single_turn(self):
        vision_blocks = [
            {"type": "text", "text": "[ALPHA SYSTEM NOTE] screenshot"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            },
        ]
        openai_msgs = [
            {"role": "user", "content": "abra o site"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "browser_screenshot", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "<tool_result>{}</tool_result>"},
            {"role": "user", "content": vision_blocks},
        ]
        _system, converted = _convert_messages(openai_msgs)
        roles = [m["role"] for m in converted]
        assert roles == ["user", "assistant", "user"]
        merged = converted[-1]["content"]
        # tool_result primeiro (exigência da API), depois text + image.
        assert merged[0]["type"] == "tool_result"
        assert merged[0]["tool_use_id"] == "c1"
        assert [b["type"] for b in merged[1:]] == ["text", "image"]
        assert merged[2]["source"]["type"] == "base64"

    def test_consecutive_plain_user_messages_merge(self):
        _system, converted = _convert_messages([
            {"role": "user", "content": "primeira"},
            {"role": "user", "content": "segunda"},
        ])
        assert len(converted) == 1
        texts = [b["text"] for b in converted[0]["content"]]
        assert texts == ["primeira", "segunda"]

    def test_normal_alternation_untouched(self):
        _system, converted = _convert_messages([
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "olá"},
            {"role": "user", "content": "tudo bem?"},
        ])
        assert [m["role"] for m in converted] == ["user", "assistant", "user"]
        assert converted[0]["content"] == "oi"


# ── browser_screenshot expõe _image_path ──


class TestScreenshotResult:
    @pytest.mark.asyncio
    async def test_result_carries_image_path(self, tmp_path, monkeypatch):
        from alpha.tools import browser_tools as bt
        from alpha.tools import workspace as ws_mod

        class FakePage:
            url = "https://example.com"
            viewport_size = {"width": 1280, "height": 800}

            async def screenshot(self, path, full_page=False):
                _write_png(tmp_path)  # conteúdo irrelevante
                import pathlib
                pathlib.Path(path).write_bytes(_PNG)

        async def fake_require_page():
            return FakePage(), None

        monkeypatch.setattr(bt, "_require_page", fake_require_page)
        monkeypatch.setattr(ws_mod, "AGENT_WORKSPACE", str(tmp_path))

        result = await bt._browser_screenshot(save_to="shot.png")
        assert "error" not in result
        assert result["_image_path"] == result["saved_to"]
        assert result["viewport"] == {"width": 1280, "height": 800}
