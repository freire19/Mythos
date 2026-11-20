"""Regressao para DEEP_BUGS wave 3 — system_tools.

- #DB12: timeout numa tool de screenshot deve matar o subprocess e o erro
  final deve distinguir "deu timeout" de "nenhuma tool disponivel".
- #DB20: timeout em clipboard_read/clipboard_write/notify_user deve matar
  o subprocess antes de retornar o erro.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alpha.tools.system_tools import (
    _clipboard_read,
    _clipboard_write,
    _notify_user,
    _screenshot,
)


def _timeout_proc() -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


# ── #DB12: screenshot ───────────────────────────────────────────────


class TestDB12ScreenshotTimeout:
    @pytest.mark.anyio
    async def test_timeout_kills_proc_and_reports_timeout(self):
        proc = _timeout_proc()
        with (
            patch(
                "shutil.which",
                side_effect=lambda name: (
                    "/usr/bin/scrot" if name == "scrot" else None
                ),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=proc),
            ),
        ):
            result = await _screenshot(region="full")

        proc.kill.assert_called_once()
        proc.wait.assert_awaited()
        assert "error" in result
        assert "Timeout" in result["error"]
        assert "scrot" in result["error"]
        # Nao pode mentir que nenhuma tool existia
        assert "Nenhuma ferramenta" not in result["error"]

    @pytest.mark.anyio
    async def test_no_tool_available_keeps_original_message(self):
        with patch("shutil.which", return_value=None):
            result = await _screenshot(region="full")
        assert "Nenhuma ferramenta" in result["error"]

    @pytest.mark.anyio
    async def test_timeout_on_first_tool_still_tries_next(self):
        procs = [_timeout_proc(), _timeout_proc()]
        with (
            patch(
                "shutil.which",
                side_effect=lambda name: (
                    f"/usr/bin/{name}"
                    if name in ("gnome-screenshot", "scrot")
                    else None
                ),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=procs),
            ),
        ):
            result = await _screenshot(region="full")

        for proc in procs:
            proc.kill.assert_called_once()
            proc.wait.assert_awaited()
        assert "gnome-screenshot" in result["error"]
        assert "scrot" in result["error"]


# ── #DB20: clipboard / notify ───────────────────────────────────────


class TestDB20ClipboardNotifyTimeout:
    @pytest.mark.anyio
    async def test_clipboard_read_timeout_kills_proc(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        proc = _timeout_proc()
        with (
            patch("shutil.which", return_value="/usr/bin/xclip"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=proc),
            ),
        ):
            result = await _clipboard_read()

        proc.kill.assert_called_once()
        proc.wait.assert_awaited()
        assert result == {"error": "Timeout ao ler clipboard"}

    @pytest.mark.anyio
    async def test_clipboard_write_timeout_kills_proc(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("DISPLAY", ":0")
        proc = _timeout_proc()
        with (
            patch("shutil.which", return_value="/usr/bin/xclip"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=proc),
            ),
        ):
            result = await _clipboard_write("abc")

        proc.kill.assert_called_once()
        proc.wait.assert_awaited()
        assert result == {"error": "Timeout ao escrever no clipboard"}

    @pytest.mark.anyio
    async def test_notify_user_timeout_kills_proc(self):
        proc = _timeout_proc()
        with (
            patch("shutil.which", return_value="/usr/bin/notify-send"),
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=proc),
            ),
        ):
            result = await _notify_user("msg")

        proc.kill.assert_called_once()
        proc.wait.assert_awaited()
        assert result == {"error": "Timeout ao enviar notificação"}
