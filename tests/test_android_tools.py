"""android_* tools (adb mocked) + the Gemini fallback that lets a text-only
model 'see' tool screenshots + ask_choice never timing out."""

from __future__ import annotations

import asyncio
import struct
import zlib

import pytest

from alpha import executor
from alpha.tools import android_tools as at


def _png(w: int, h: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + b"\x00\x00\x00" * w))
        + chunk(b"IEND", b"")
    )


UI_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">'
    '<node text="" class="android.widget.FrameLayout" package="com.ex.app" bounds="[0,0][904,2316]">'
    '<node text="Entrar" resource-id="com.ex.app:id/btn_login" class="android.widget.Button" '
    'package="com.ex.app" clickable="true" enabled="true" bounds="[80,1000][824,1130]"/>'
    '<node text="hunter2" resource-id="com.ex.app:id/pwd" class="android.widget.EditText" '
    'package="com.ex.app" clickable="true" password="true" bounds="[80,780][824,900]"/>'
    '<node text="Erro: senha incorreta" class="android.widget.TextView" package="com.ex.app" '
    'clickable="false" bounds="[80,1200][824,1260]"/>'
    "</node></hierarchy>"
)


@pytest.fixture
def adb(monkeypatch):
    """Fake adb: `calls` records argv; `replies` maps a substring of the
    joined argv to (code, stdout, stderr)."""
    calls: list[list[str]] = []
    replies: dict[str, tuple[int, bytes, bytes]] = {}

    async def fake_run(argv, timeout=30.0):
        calls.append(list(argv))
        joined = " ".join(argv)
        for needle, reply in replies.items():
            if needle in joined:
                return reply
        return (0, b"", b"")

    monkeypatch.setattr(at, "_run", fake_run)
    monkeypatch.setattr(at.shutil, "which", lambda name: "/usr/bin/adb")
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)
    return calls, replies


def run(coro):
    return asyncio.run(coro)


class TestDevices:
    def test_lists_device_with_displays(self, adb):
        _, replies = adb
        replies["devices -l"] = (0, b"List of devices attached\nR5CT1 device usb:1-2 model:SM_F936B device:q4q\n", b"")
        replies["ro.build.version.release"] = (0, b"16\n", b"")
        replies["wm size"] = (0, b"Physical size: 1812x2176\n", b"")
        replies["--display-id"] = (0, b"Display 4630946 (HWC display 0)\nDisplay 4630947 (HWC display 1)\n", b"")
        r = run(at._android_devices())
        assert r["ok"] and r["devices"][0]["model"] == "SM_F936B"
        assert r["devices"][0]["android"] == "16"
        assert r["devices"][0]["displays"] == ["4630946", "4630947"]

    def test_no_device_explains_usb_debugging(self, adb):
        _, replies = adb
        replies["devices -l"] = (0, b"List of devices attached\n\n", b"")
        r = run(at._android_devices())
        assert r["ok"] is False and r["category"] == "no_device"
        assert "Depuração USB" in r["error"]

    def test_missing_adb(self, adb, monkeypatch):
        monkeypatch.setattr(at.shutil, "which", lambda name: None)
        assert "adb não encontrado" in run(at._android_devices())["error"]


class TestScreenshot:
    def test_plain_capture(self, adb):
        _, replies = adb
        replies["screencap"] = (0, _png(904, 2316), b"")
        r = run(at._android_screenshot(question="tem erro na tela?"))
        assert r["ok"] and (r["width"], r["height"]) == (904, 2316)
        assert r["_image_path"].endswith(".png")
        assert r["_image_question"] == "tem erro na tela?"

    def test_foldable_falls_back_to_display_ids(self, adb):
        calls, replies = adb
        replies["screencap -p -d 22"] = (0, _png(904, 2316), b"")
        replies["screencap -p -d 11"] = (0, b"", b"")
        replies["--display-id"] = (0, b"Display 11 (HWC display 0)\nDisplay 22 (HWC display 1)\n", b"")
        replies["screencap -p"] = (0, b"[Warning] Multiple displays were found, but no display id was specified!", b"")
        r = run(at._android_screenshot())
        assert r["ok"] and r["display"] == "22" and r["other_displays"] == ["11"]

    def test_folded_phone_captures_the_lit_display_not_the_first(self, adb):
        """Both panels answer screencap; the OFF one is a black image."""
        calls, replies = adb
        replies["screencap -p -d 11"] = (0, _png(1812, 2176), b"")
        replies["screencap -p -d 22"] = (0, _png(904, 2316), b"")
        replies["--display-id"] = (0, b"Display 11 (HWC display 0)\nDisplay 22 (HWC display 3)\n", b"")
        replies["dumpsys display"] = (0, (
            b'  DisplayDeviceInfo{"Tela integrada": uniqueId="local:11", 1812 x 2176}\n'
            b"    mPhysicalDisplayId=11\n    mActiveModeId=3\n    mSupportedRefreshRates=[120.0, 60.0]\n    mState=OFF\n"
            b'  DisplayDeviceInfo{"Tela integrada": uniqueId="local:22", 904 x 2316}\n'
            b"    mPhysicalDisplayId=22\n    mActiveModeId=8\n    mState=ON\n"
        ), b"")
        # Android 16: a plain screencap silently returns display 0 (the dark one).
        replies["screencap -p"] = (0, _png(1812, 2176), b"")
        r = run(at._android_screenshot())
        assert r["display"] == "22" and (r["width"], r["height"]) == (904, 2316)
        assert r["other_displays"] == ["11"]

    def test_rejects_bad_display(self, adb):
        assert run(at._android_screenshot(display="1; rm -rf /"))["category"] == "invalid_args"


class TestUiDump:
    def test_nodes_have_centers_and_hide_password_text(self, adb):
        _, replies = adb
        replies["uiautomator dump"] = (0, UI_XML.encode() + b"UI hierchary dumped to: /dev/tty\n", b"")
        r = run(at._android_ui_dump())
        assert r["package"] == "com.ex.app" and r["count"] == 3
        btn = next(n for n in r["nodes"] if n.get("id") == "btn_login")
        assert (btn["x"], btn["y"], btn["text"]) == (452, 1065, "Entrar")
        pwd = next(n for n in r["nodes"] if n.get("id") == "pwd")
        assert "text" not in pwd and "password" in pwd["flags"]
        assert "hunter2" not in str(r)

    def test_only_interactive(self, adb):
        _, replies = adb
        replies["uiautomator dump"] = (0, UI_XML.encode(), b"")
        assert run(at._android_ui_dump(only_interactive=True))["count"] == 2

    def test_no_hierarchy(self, adb):
        _, replies = adb
        replies["uiautomator dump"] = (0, b"ERROR: could not get idle state.", b"")
        assert run(at._android_ui_dump())["ok"] is False


class TestInput:
    def test_tap(self, adb):
        calls, _ = adb
        assert run(at._android_input("tap", x=452, y=1065))["ok"]
        assert calls[-1] == ["adb", "shell", "input", "tap", "452", "1065"]

    def test_text_is_quoted_for_the_device_shell(self, adb):
        calls, _ = adb
        run(at._android_input("text", text="a b; rm -rf /sdcard $(id) 100%"))
        arg = calls[-1][-1]
        assert arg.startswith("'") and arg.endswith("'")
        assert " " not in arg and "%s" in arg and "100%%" in arg

    def test_text_rejects_accents_and_newlines(self, adb):
        calls, _ = adb
        assert run(at._android_input("text", text="ação"))["category"] == "invalid_args"
        assert run(at._android_input("text", text="a\nb"))["category"] == "invalid_args"
        assert calls == []

    def test_key_whitelist(self, adb):
        calls, _ = adb
        assert run(at._android_input("key", key="back"))["ok"]
        assert calls[-1][-1] == "KEYCODE_BACK"
        assert run(at._android_input("key", key="power"))["category"] == "invalid_args"

    def test_serial_is_validated_and_passed(self, adb):
        calls, _ = adb
        run(at._android_input("tap", x=1, y=1, serial="R5CT1"))
        assert calls[-1][:3] == ["adb", "-s", "R5CT1"]
        assert run(at._android_input("tap", x=1, y=1, serial="a b"))["category"] == "invalid_args"

    def test_bad_coordinates(self, adb):
        assert run(at._android_input("tap", x="abc", y=1))["category"] == "invalid_args"
        assert run(at._android_input("swipe", x=1, y=1))["category"] == "invalid_args"


class TestApp:
    def test_install_requires_apk_inside_workspace(self, adb, tmp_path, monkeypatch):
        calls, replies = adb
        from alpha.tools import workspace

        monkeypatch.setattr(workspace, "AGENT_WORKSPACE", tmp_path)
        apk = tmp_path / "app.apk"
        apk.write_bytes(b"PK")
        replies["install -r"] = (0, b"Performing Streamed Install\nSuccess\n", b"")
        r = run(at._android_app("install", apk_path=str(apk)))
        assert r["ok"], r
        assert calls[-1][1:3] == ["install", "-r"]
        assert run(at._android_app("install", apk_path="/etc/passwd"))["ok"] is False

    def test_launch_validates_package(self, adb):
        calls, _ = adb
        assert run(at._android_app("launch", package="com.ex.app; reboot"))["category"] == "invalid_args"
        assert calls == []
        assert run(at._android_app("launch", package="com.ex.app"))["ok"]

    def test_no_destructive_actions(self, adb):
        assert run(at._android_app("uninstall", package="com.ex.app"))["category"] == "invalid_args"
        assert run(at._android_app("clear", package="com.ex.app"))["category"] == "invalid_args"


class TestLogcat:
    def test_filters_by_pid_and_grep(self, adb):
        calls, replies = adb
        replies["pidof"] = (0, b"4242\n", b"")
        replies["logcat"] = (0, b"11-20 E/App: boom NullPointer\n11-20 W/App: slow frame\n", b"")
        r = run(at._android_logcat(package="com.ex.app", grep="nullpointer"))
        assert r["count"] == 1 and "boom" in r["lines"][0]
        assert "--pid" in calls[-1] and "4242" in calls[-1]

    def test_app_not_running(self, adb):
        _, replies = adb
        replies["pidof"] = (1, b"", b"")
        r = run(at._android_logcat(package="com.ex.app"))
        assert r["ok"] and r["running"] is False


class TestRegistration:
    def test_safety_and_lazy_category(self):
        from alpha import tool_lazy
        from alpha.tools import get_tool
        from alpha.tools._delegate_core import SUBAGENT_DESTRUCTIVE_BLOCKLIST

        for name in ("android_devices", "android_screenshot", "android_ui_dump", "android_logcat"):
            assert get_tool(name).safety.value == "safe"
        for name in ("android_input", "android_app"):
            assert get_tool(name).safety.value == "destructive"
            assert name in SUBAGENT_DESTRUCTIVE_BLOCKLIST
        assert tool_lazy.is_deferred("android_input", get_tool("android_input"))


class TestVisionFallback:
    def test_blind_provider_gets_gemini_description(self, monkeypatch, tmp_path):
        from alpha import ocr

        img = tmp_path / "s.png"
        img.write_bytes(_png(10, 10))
        seen = {}

        def fake_describe(paths, api_key, question="", **kw):
            seen["q"] = question
            return "Tela de login. Botao ENTRAR em (452, 1065)."

        monkeypatch.setattr(ocr, "describe_images_sync", fake_describe)
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.delenv("ALPHA_VISION_FALLBACK", raising=False)
        result = {"_image_path": str(img), "_image_question": "tem erro?"}
        paths = executor._collect_result_images(result)
        messages: list[dict] = []
        executor._append_vision_attachments(messages, [("android_screenshot", paths)], "deepseek")
        assert seen["q"] == "tem erro?"
        assert len(messages) == 1 and messages[0]["role"] == "user"
        assert "ENTRAR em (452, 1065)" in messages[0]["content"]
        assert executor._image_questions == {}

    def test_without_key_the_model_is_told_nobody_looked(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("ALPHA_VISION_FALLBACK", raising=False)
        messages: list[dict] = []
        executor._append_vision_attachments(messages, [("screenshot", ["/x.png"])], "deepseek")
        assert "cannot see images" in messages[0]["content"]

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("ALPHA_VISION_FALLBACK", "off")
        messages: list[dict] = []
        executor._append_vision_attachments(messages, [("screenshot", ["/x.png"])], "deepseek")
        assert messages == []


class TestAskChoiceNeverTimesOut:
    def test_user_wait_tool_ignores_the_tool_timeout(self, monkeypatch):
        from alpha import _turn_budget as tb

        monkeypatch.setattr(executor, "TOOL_EXECUTION_TIMEOUT", 0.05)

        class _Tool:
            @staticmethod
            async def executor(**kw):
                await asyncio.sleep(0.3)
                return {"ok": True, "chosen_index": 1}

        async def go():
            before = tb.user_wait_total()
            r = await executor._execute_single_tool(_Tool, "ask_choice", {})
            return r, tb.user_wait_total() - before

        result, waited = asyncio.run(go())
        assert result == {"ok": True, "chosen_index": 1}
        assert waited >= 0.3


class TestLoopDetectorOnDeviceFlows:
    """observe → act → observe is how a phone/browser is driven; it is not a loop."""

    DUMP = 'android_ui_dump:{"only_interactive": true}'

    @staticmethod
    def _tap(x):
        return f'android_input:{{"action": "tap", "x": {x}, "y": 150}}'

    def _detect(self, calls, batch=1):
        from alpha.agent.loop import _detect_loop

        return _detect_loop(calls[-batch:], calls, [], [])

    def test_same_observation_between_different_actions_is_fine(self):
        calls = [
            'android_devices:{}', 'android_app:{"action": "launch", "package": "com.x.y"}',
            self.DUMP, self._tap(841), self.DUMP,
            'android_input:{"action": "text", "text": "legiao urbana"}',
            'android_input:{"action": "key", "key": "enter"}', self.DUMP,
        ]
        assert self._detect(calls) is None

    def test_same_action_between_observations_is_fine(self):
        calls = ['android_devices:{}']
        for _ in range(4):  # "next video" four times, looking in between
            calls += [self.DUMP, self._tap(452)]
        assert self._detect(calls) is None

    def test_observing_three_times_with_nothing_in_between_is_a_loop(self):
        calls = ['android_devices:{}', self._tap(1), 'read_file:{"path": "a"}',
                 self.DUMP, self.DUMP, self.DUMP]
        assert "exact repeat" in (self._detect(calls) or "")

    def test_endless_alternation_hits_the_backstop(self):
        calls = []
        for _ in range(12):
            calls += [self.DUMP, self._tap(452)]
        assert "(device)" in (self._detect(calls) or "")

    def test_non_device_repeats_are_unchanged(self):
        calls = ['read_file:{"path": "a"}'] * 3 + ['glob_files:{}'] * 3
        assert "exact repeat" in (self._detect(calls) or "")
