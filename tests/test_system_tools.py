"""Regressao para AUDIT_V1.8 #056 — cobertura de system_tools.

Garante que _detect_display_server, _clipboard_read, _clipboard_write,
_screenshot e _notify_user estejam cobertos com happy paths + timeouts +
returncodes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alpha.tools.system_tools import (
    _clipboard_read,
    _clipboard_write,
    _detect_display_server,
    _notify_user,
    _screenshot,
)


# ── _detect_display_server ──────────────────────────────────────────

class TestDetectDisplayServer:
    def test_wayland(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        assert _detect_display_server() == "wayland"

    def test_x11_no_wayland(self, monkeypatch):
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.setenv("DISPLAY", ":0")
        assert _detect_display_server() == "x11"

    def test_unknown(self, monkeypatch):
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        assert _detect_display_server() == "unknown"

    def test_wayland_overrides_display(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.setenv("DISPLAY", ":0")
        assert _detect_display_server() == "wayland"


# ── _clipboard_read ─────────────────────────────────────────────────

class TestClipboardRead:
    @pytest.mark.anyio
    async def test_no_display_server(self, monkeypatch):
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        result = await _clipboard_read()
        assert "error" in result
        assert "display" in result["error"].lower()

    @pytest.mark.anyio
    async def test_tool_not_found(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        with patch("shutil.which", return_value=None):
            result = await _clipboard_read()
        assert "error" in result
        assert "não encontrado" in result["error"]

    @pytest.mark.anyio
    async def test_returncode_nonzero(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"clipboard error")
        )
        with (
            patch("shutil.which", return_value="/usr/bin/xclip"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=mock_proc),
            ),
        ):
            result = await _clipboard_read()
        assert "error" in result

    @pytest.mark.anyio
    async def test_success(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(b"clipboard content", b"")
        )
        with (
            patch("shutil.which", return_value="/usr/bin/xclip"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=mock_proc),
            ),
        ):
            result = await _clipboard_read()
        assert result["content"] == "clipboard content"
        assert result["length"] == 17
        assert result["truncated"] is False

    @pytest.mark.anyio
    async def test_truncation(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        long_content = "x" * 15000
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(long_content.encode(), b"")
        )
        with (
            patch("shutil.which", return_value="/usr/bin/xclip"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=mock_proc),
            ),
        ):
            result = await _clipboard_read()
        assert len(result["content"]) == 10000
        assert result["truncated"] is True

    @pytest.mark.anyio
    async def test_timeout(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(
            side_effect=asyncio.TimeoutError
        )
        with (
            patch("shutil.which", return_value="/usr/bin/xclip"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=mock_proc),
            ),
        ):
            result = await _clipboard_read()
        assert "error" in result
        assert "Timeout" in result["error"]

    @pytest.mark.anyio
    async def test_generic_exception(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        with (
            patch("shutil.which", return_value="/usr/bin/xclip"),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=OSError("broken pipe"),
            ),
        ):
            result = await _clipboard_read()
        assert "error" in result
        assert "broken pipe" in result["error"]

    @pytest.mark.anyio
    async def test_wayland_path(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(b"wayland clipboard", b"")
        )
        with (
            patch("shutil.which", return_value="/usr/bin/wl-paste"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=mock_proc),
            ),
        ):
            result = await _clipboard_read()
        assert result["content"] == "wayland clipboard"


# ── _clipboard_write ────────────────────────────────────────────────

class TestClipboardWrite:
    @pytest.mark.anyio
    async def test_no_display_server(self, monkeypatch):
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        result = await _clipboard_write("test")
        assert "error" in result

    @pytest.mark.anyio
    async def test_tool_not_found(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        with patch("shutil.which", return_value=None):
            result = await _clipboard_write("test")
        assert "error" in result

    @pytest.mark.anyio
    async def test_success(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        with (
            patch("shutil.which", return_value="/usr/bin/xclip"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=mock_proc),
            ),
        ):
            result = await _clipboard_write("hello world")
        assert result["success"] is True
        assert result["length"] == 11

    @pytest.mark.anyio
    async def test_returncode_nonzero(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"write error")
        )
        with (
            patch("shutil.which", return_value="/usr/bin/xclip"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=mock_proc),
            ),
        ):
            result = await _clipboard_write("test")
        assert "error" in result

    @pytest.mark.anyio
    async def test_timeout(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(
            side_effect=asyncio.TimeoutError
        )
        with (
            patch("shutil.which", return_value="/usr/bin/xclip"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=mock_proc),
            ),
        ):
            result = await _clipboard_write("test")
        assert "Timeout" in result["error"]

    @pytest.mark.anyio
    async def test_generic_exception(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        with (
            patch("shutil.which", return_value="/usr/bin/xclip"),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=RuntimeError("unexpected"),
            ),
        ):
            result = await _clipboard_write("test")
        assert "error" in result


# ── _screenshot ─────────────────────────────────────────────────────

class TestScreenshot:
    @pytest.mark.anyio
    async def test_no_tools_available(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        with patch("shutil.which", return_value=None):
            result = await _screenshot()
        assert "error" in result
        assert "Nenhuma ferramenta" in result["error"]

    @pytest.mark.anyio
    async def test_full_screenshot_tool_fails_then_succeeds(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        fail_proc = MagicMock()
        fail_proc.returncode = 1
        fail_proc.communicate = AsyncMock(return_value=(b"", b"fail"))
        good_proc = MagicMock()
        good_proc.returncode = 0
        good_proc.communicate = AsyncMock(return_value=(b"", b""))
        create_exec = AsyncMock(side_effect=[fail_proc, good_proc])

        with (
            patch(
                "shutil.which",
                side_effect=lambda name: (
                    "/usr/bin/gnome-screenshot"
                    if name == "gnome-screenshot"
                    else "/usr/bin/scrot"
                    if name == "scrot"
                    else None
                ),
            ),
            patch("asyncio.create_subprocess_exec", create_exec),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.stat") as mock_stat,
        ):
            mock_stat.return_value.st_size = 12345
            result = await _screenshot()
        assert result["tool_used"] == "scrot"
        assert result["size_bytes"] == 12345
        assert result["region"] == "full"
        assert result["_image_path"] == result["path"]

    @pytest.mark.anyio
    async def test_active_window_with_window_id(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        xdo_proc = MagicMock()
        xdo_proc.returncode = 0
        xdo_proc.communicate = AsyncMock(
            return_value=(b"12345\n", b"")
        )
        fail_proc = MagicMock()
        fail_proc.returncode = 1
        fail_proc.communicate = AsyncMock(return_value=(b"", b""))
        good_proc = MagicMock()
        good_proc.returncode = 0
        good_proc.communicate = AsyncMock(return_value=(b"", b""))
        create_exec = AsyncMock(
            side_effect=[xdo_proc, fail_proc, good_proc]
        )

        with (
            patch(
                "shutil.which",
                side_effect=lambda name: (
                    "/usr/bin/xdotool"
                    if name == "xdotool"
                    else "/usr/bin/gnome-screenshot"
                    if name == "gnome-screenshot"
                    else "/usr/bin/scrot"
                    if name == "scrot"
                    else None
                ),
            ),
            patch("asyncio.create_subprocess_exec", create_exec),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.stat") as mock_stat,
        ):
            mock_stat.return_value.st_size = 9999
            result = await _screenshot(region="active_window")
        assert result["region"] == "active_window"
        assert result["size_bytes"] == 9999

    @pytest.mark.anyio
    async def test_xdotool_fails_but_screenshot_works(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        xdo_proc = MagicMock()
        xdo_proc.returncode = 1
        xdo_proc.communicate = AsyncMock(return_value=(b"", b"err"))
        good_proc = MagicMock()
        good_proc.returncode = 0
        good_proc.communicate = AsyncMock(return_value=(b"", b""))
        create_exec = AsyncMock(
            side_effect=[xdo_proc, good_proc]
        )

        with (
            patch(
                "shutil.which",
                side_effect=lambda name: (
                    "/usr/bin/xdotool"
                    if name == "xdotool"
                    else "/usr/bin/gnome-screenshot"
                    if name == "gnome-screenshot"
                    else None
                ),
            ),
            patch("asyncio.create_subprocess_exec", create_exec),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.stat") as mock_stat,
        ):
            mock_stat.return_value.st_size = 500
            result = await _screenshot(region="active_window")
        assert result["tool_used"] == "gnome-screenshot"


# ── _notify_user ────────────────────────────────────────────────────

class TestNotifyUser:
    @pytest.mark.anyio
    async def test_voice_channel(self):
        result = await _notify_user("hello", channel="voice")
        assert result["channel"] == "voice"
        assert result["message"] == "hello"

    @pytest.mark.anyio
    async def test_unknown_channel(self):
        result = await _notify_user("hi", channel="sms")
        assert "error" in result
        assert "Canal" in result["error"]

    @pytest.mark.anyio
    async def test_desktop_notify_send_not_found(self):
        with patch("shutil.which", return_value=None):
            result = await _notify_user("test")
        assert "error" in result
        assert "notify-send" in result["error"]

    @pytest.mark.anyio
    async def test_desktop_success(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        with (
            patch("shutil.which", return_value="/usr/bin/notify-send"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=mock_proc),
            ),
        ):
            result = await _notify_user("done", title="Test")
        assert result["sent"] is True
        assert result["channel"] == "desktop"

    @pytest.mark.anyio
    async def test_desktop_returncode_nonzero(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"notify error")
        )
        with (
            patch("shutil.which", return_value="/usr/bin/notify-send"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=mock_proc),
            ),
        ):
            result = await _notify_user("test")
        assert "error" in result

    @pytest.mark.anyio
    async def test_desktop_timeout(self):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(
            side_effect=asyncio.TimeoutError
        )
        with (
            patch("shutil.which", return_value="/usr/bin/notify-send"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=mock_proc),
            ),
        ):
            result = await _notify_user("test")
        assert "error" in result

    @pytest.mark.anyio
    async def test_desktop_generic_exception(self):
        with (
            patch("shutil.which", return_value="/usr/bin/notify-send"),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=OSError("dbus dead"),
            ),
        ):
            result = await _notify_user("test")
        assert "error" in result
        assert "dbus dead" in result["error"]

    @pytest.mark.anyio
    async def test_urgency_mapping(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        create_exec = AsyncMock(return_value=mock_proc)

        with (
            patch("shutil.which", return_value="/usr/bin/notify-send"),
            patch("asyncio.create_subprocess_exec", create_exec),
        ):
            result = await _notify_user("urgent msg", urgency="high")
        assert result["sent"] is True
        call_args = create_exec.call_args[0]
        assert "--urgency" in call_args
        urg_idx = call_args.index("--urgency")
        assert call_args[urg_idx + 1] == "critical"
