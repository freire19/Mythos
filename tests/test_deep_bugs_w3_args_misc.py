"""Regression tests for DEEP_BUGS wave 3 — args misc.

- #DB17: TokenBucket.acquire com rate 0 dividia por zero; rpm<=0 no env
  deve cair no default e rate<=0 no bucket vira "ilimitado".
- #DB19: timeout/max_chars stringificados crashavam fora do try em
  execute_shell / execute_python / web_fetch; agora retornam {"error": ...}.
"""

from __future__ import annotations

import asyncio


from alpha import _rate_limiter
from alpha._rate_limiter import TokenBucket, _parse_limit


class TestDB17RateZero:
    def test_parse_limit_rejects_zero_rpm(self, monkeypatch):
        monkeypatch.setenv("ALPHA_RATE_LIMIT_DEEPSEEK", "0")
        assert _parse_limit("deepseek") == _rate_limiter._DEFAULT_LIMITS["deepseek"]

    def test_parse_limit_rejects_negative_rpm(self, monkeypatch):
        monkeypatch.setenv("ALPHA_RATE_LIMIT_FOO", "-5,2")
        assert _parse_limit("foo") == _rate_limiter._DEFAULT_LIMITS["_default"]

    def test_parse_limit_valid_rpm_still_works(self, monkeypatch):
        monkeypatch.setenv("ALPHA_RATE_LIMIT_FOO", "120,4")
        assert _parse_limit("foo") == (2.0, 4)

    def test_acquire_with_rate_zero_does_not_raise(self):
        # Bucket construído diretamente com rate 0 (bypassa _parse_limit):
        # deve tratar como ilimitado, nunca ZeroDivisionError.
        bucket = TokenBucket(rate=0.0, burst=1)

        async def _drain():
            for _ in range(5):
                await bucket.acquire()

        asyncio.run(_drain())


class TestDB19StringifiedNumericArgs:
    def test_execute_shell_string_timeout_returns_error(self):
        from alpha.tools.shell_tools import _execute_shell

        result = asyncio.run(_execute_shell("echo hi", timeout="abc"))
        assert "error" in result
        assert "timeout" in result["error"]

    def test_execute_shell_numeric_string_timeout_is_coerced(self):
        from alpha.tools.shell_tools import _execute_shell

        result = asyncio.run(_execute_shell("echo hi", timeout="30"))
        assert result.get("exit_code") == 0

    def test_execute_python_string_timeout_returns_error(self):
        from alpha.tools.code_tools import _execute_python

        result = asyncio.run(_execute_python("print(1)", timeout="20.5"))
        assert "error" in result
        assert "timeout" in result["error"]

    def test_web_fetch_string_max_chars_returns_error(self):
        from alpha.tools.search_tools import _web_fetch

        result = asyncio.run(_web_fetch("http://example.com", max_chars="abc"))
        assert "error" in result
        assert "max_chars" in result["error"]

    def test_web_fetch_numeric_string_max_chars_is_coerced(self, monkeypatch):
        from alpha import web_search
        from alpha.tools.search_tools import _web_fetch

        seen: dict = {}

        async def fake_extract(url, max_chars=0):
            seen["max_chars"] = max_chars
            return "conteudo"

        monkeypatch.setattr(web_search, "extract_page_content", fake_extract)
        result = asyncio.run(_web_fetch("http://example.com", max_chars="500"))
        assert result.get("content") == "conteudo"
        assert seen["max_chars"] == 500
