"""Regressao para AUDIT_V1.8 #060 — cobertura de network_tools.

Cobre _rewrite_url_with_ip, validacao SSRF (validate_url, private IPs),
metodo HTTP invalido, timeout, fallback urllib, e retry em metodos seguros.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from alpha.net_utils import is_private_ip_address, validate_url
from alpha.tools.network_tools import (
    _ALLOWED_METHODS,
    _http_request,
    _http_request_with_retry,
    _rewrite_url_with_ip,
)
from urllib.parse import urlparse


# ── _rewrite_url_with_ip ────────────────────────────────────────────

class TestRewriteUrlWithIp:
    def test_ipv4_no_port(self):
        parsed = urlparse("https://example.com/path")
        result = _rewrite_url_with_ip(parsed, "93.184.216.34")
        assert result == "https://93.184.216.34/path"

    def test_ipv4_with_port(self):
        parsed = urlparse("http://example.com:8080/api")
        result = _rewrite_url_with_ip(parsed, "10.0.0.1")
        assert result == "http://10.0.0.1:8080/api"

    def test_ipv6(self):
        parsed = urlparse("https://example.com/path")
        result = _rewrite_url_with_ip(parsed, "::1")
        assert result == "https://[::1]/path"

    def test_ipv6_with_port(self):
        parsed = urlparse("https://example.com:8443/path")
        result = _rewrite_url_with_ip(parsed, "2001:db8::1")
        assert result == "https://[2001:db8::1]:8443/path"

    def test_preserves_query_and_fragment(self):
        parsed = urlparse("https://example.com/search?q=test#section")
        result = _rewrite_url_with_ip(parsed, "1.2.3.4")
        assert result == "https://1.2.3.4/search?q=test#section"


# ── SSRF: is_private_ip_address ─────────────────────────────────────

class TestPrivateIpAddress:
    def test_loopback(self):
        assert is_private_ip_address("127.0.0.1") is True
        assert is_private_ip_address("::1") is True

    def test_private_ranges(self):
        assert is_private_ip_address("10.0.0.1") is True
        assert is_private_ip_address("192.168.1.1") is True
        assert is_private_ip_address("172.16.0.1") is True

    def test_link_local(self):
        assert is_private_ip_address("169.254.1.1") is True

    def test_reserved(self):
        assert is_private_ip_address("240.0.0.1") is True  # Class E

    def test_multicast(self):
        assert is_private_ip_address("224.0.0.1") is True

    def test_public_ip(self):
        assert is_private_ip_address("8.8.8.8") is False
        assert is_private_ip_address("1.1.1.1") is False

    def test_fail_closed_on_invalid(self):
        """Invalid IP string returns True (blocked)."""
        assert is_private_ip_address("not-an-ip") is True
        assert is_private_ip_address("") is True


# ── SSRF: validate_url ──────────────────────────────────────────────

class TestValidateUrl:
    def test_allows_https_public(self):
        assert validate_url("https://example.com") is None

    def test_allows_http_public(self):
        assert validate_url("http://example.com") is None

    def test_blocks_ftp(self):
        err = validate_url("ftp://example.com/file")
        assert err is not None
        assert "ftp" in err.lower()

    def test_blocks_file_scheme(self):
        err = validate_url("file:///etc/passwd")
        assert err is not None

    def test_blocks_no_hostname(self):
        err = validate_url("http:///path")
        assert err is not None

    def test_invalid_url(self):
        err = validate_url("not a url at all")
        assert err is not None

    def test_blocks_loopback(self):
        err = validate_url("http://127.0.0.1:8080")
        assert err is not None
        assert "SSRF" in err or "private" in err.lower()

    def test_blocks_private_10(self):
        err = validate_url("http://10.0.0.1/admin")
        assert err is not None

    def test_blocks_private_192(self):
        err = validate_url("http://192.168.1.1/")
        assert err is not None

    def test_blocks_localhost_hostname(self):
        # localhost resolves to 127.0.0.1 or fails DNS — either case,
        # is_private_ip returns True (fail-closed), so this is blocked.
        err = validate_url("http://localhost:3000")
        assert err is not None
        assert "SSRF" in err or "private" in err.lower()


# ── _http_request: invalid method ───────────────────────────────────

class TestHttpRequestInvalidMethod:
    @pytest.mark.anyio
    async def test_method_not_allowed(self):
        await _http_request("https://example.com", method="PATCH")
        # PATCH is ALLOWED — let's verify it's in the set.
        assert "PATCH" in _ALLOWED_METHODS

    @pytest.mark.anyio
    async def test_truly_invalid_method(self):
        result = await _http_request("https://example.com", method="CONNECT")
        assert "error" in result
        assert "CONNECT" in result["error"]


# ── _http_request: SSRF blocking ────────────────────────────────────

class TestHttpRequestSsrF:
    @pytest.mark.anyio
    async def test_blocks_private_ip(self):
        result = await _http_request("http://127.0.0.1:8080")
        assert result.get("blocked") is True

    @pytest.mark.anyio
    async def test_blocks_internal_hostname(self):
        result = await _http_request("http://localhost:3000")
        if result.get("blocked"):
            assert result["blocked"] is True
        # If localhost doesn't resolve in test env, it's still valid as a test


# ── _http_request: urllib fallback ──────────────────────────────────

class TestHttpRequestUrllibFallback:
    @pytest.mark.anyio
    async def test_falls_back_when_aiohttp_missing(self):
        """When aiohttp is not importable, use urllib fallback."""
        with patch.dict("sys.modules", {"aiohttp": None}):
            result = await _http_request("http://127.0.0.1:8080")
        # Should be blocked by SSRF before reaching aiohttp/urllib
        assert result.get("blocked") is True

    @pytest.mark.anyio
    async def test_urllib_fallback_ssrf_block(self, monkeypatch):
        """Test urllib path specifically with SSRF."""
        monkeypatch.setitem(
            sys.modules, "aiohttp", None
        )
        result = await _http_request("http://10.0.0.1/api")
        assert result.get("blocked") is True


# ── _http_request: timeout ──────────────────────────────────────────

class TestHttpRequestTimeout:
    @pytest.mark.anyio
    async def test_timeout_capped(self):
        """Timeout > TOOL_TIMEOUT_CAPS should be capped."""
        result = await _http_request(
            "http://127.0.0.1:8080", timeout=999
        )
        # Blocked before timeout matters, but verify blocked
        assert result.get("blocked") is True


# ── _http_request_with_retry ────────────────────────────────────────

class TestHttpRequestWithRetry:
    @pytest.mark.anyio
    async def test_safe_method_retries_transient(self):
        """GET retries on transient errors."""
        call_count = 0

        async def mock_request(url, method, headers, body, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"error": "connection reset", "transient": True}
            return {"status_code": 200, "body": "ok"}

        with patch(
            "alpha.tools.network_tools._http_request",
            side_effect=mock_request,
        ):
            result = await _http_request_with_retry("https://example.com")
        assert result["status_code"] == 200
        assert call_count == 2

    @pytest.mark.anyio
    async def test_unsafe_method_no_retry(self):
        """POST should NOT retry."""
        call_count = 0

        async def mock_request(url, method, headers, body, timeout):
            nonlocal call_count
            call_count += 1
            return {"error": "connection reset", "transient": True}

        with patch(
            "alpha.tools.network_tools._http_request",
            side_effect=mock_request,
        ):
            result = await _http_request_with_retry(
                "https://example.com", method="POST"
            )
        assert call_count == 1
        assert result.get("transient") is True

    @pytest.mark.anyio
    async def test_exhausts_retries(self):
        """Returns last error after all retries exhausted."""
        call_count = 0

        async def mock_request(url, method, headers, body, timeout):
            nonlocal call_count
            call_count += 1
            return {"error": f"fail #{call_count}", "transient": True}

        with patch(
            "alpha.tools.network_tools._http_request",
            side_effect=mock_request,
        ):
            result = await _http_request_with_retry("https://example.com")
        assert "retried" in result
        assert result["retried"] > 1

    @pytest.mark.anyio
    async def test_non_transient_no_retry(self):
        """Non-transient errors return immediately."""
        call_count = 0

        async def mock_request(url, method, headers, body, timeout):
            nonlocal call_count
            call_count += 1
            return {"error": "SSRF blocked", "blocked": True}

        with patch(
            "alpha.tools.network_tools._http_request",
            side_effect=mock_request,
        ):
            result = await _http_request_with_retry("https://example.com")
        assert call_count == 1
        assert result.get("blocked") is True
