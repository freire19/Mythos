"""Test that `ocr_images` honors the caller-supplied `timeout` parameter.

Regression for the dead-parameter bug surfaced by code-review on commit
20dfbbd / a56a51b: the LoopAwareClient was built with a hardcoded 60s
timeout via `httpx.Timeout(60.0)` in the build lambda. Without passing
`timeout=` to `client.post(...)`, the caller's argument was silently
dropped.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

import alpha.ocr as ocr_mod


def _make_dummy_image(tmp_path: Path) -> Path:
    """Write a minimal valid PNG (1x1, transparent) for `_load_image_b64`."""
    # 67-byte minimal PNG, signature + IHDR + IDAT (empty) + IEND
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    p = tmp_path / "img.png"
    p.write_bytes(png)
    return p


@pytest.fixture
def reset_ocr_client():
    """Clear LoopAwareClient state so each test gets a fresh transport."""
    yield
    try:
        # LoopAwareClient internals — same pattern other tests use for
        # _shared_llm_client in alpha.llm.
        ocr_mod._ocr_client._client = None
        ocr_mod._ocr_client._loop = None
    except AttributeError:
        pass


def test_timeout_arg_is_applied_per_request(tmp_path, reset_ocr_client, monkeypatch):
    """Caller-supplied `timeout` must reach `client.post(...)` as a
    per-request override, not be silently discarded in favor of the
    client-level 60s default."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # `request.extensions['timeout']` holds the resolved Timeout dict
        # for THIS request (httpx merges client default + per-request
        # override). Capture it so the test can assert on the connect/
        # read/write/pool timeouts the call actually used.
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    transport = httpx.MockTransport(handler)
    # Swap the LoopAwareClient's build to inject our MockTransport.
    monkeypatch.setattr(
        ocr_mod._ocr_client,
        "_build",
        lambda: httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(60.0),  # mirror prod default
        ),
    )

    img = _make_dummy_image(tmp_path)
    result = asyncio.run(
        ocr_mod.ocr_images([img], api_key="test-key", timeout=12.5)
    )

    assert result == "ok"
    assert captured["timeout"] is not None, "request.extensions['timeout'] not captured"
    # The caller passed 12.5; all four timeout categories should reflect that
    # (httpx.Timeout(12.5) sets connect=read=write=pool=12.5).
    for category in ("connect", "read", "write", "pool"):
        assert captured["timeout"][category] == 12.5, (
            f"{category} timeout was {captured['timeout'][category]}, "
            f"expected 12.5 (caller-supplied) — the parameter is being ignored"
        )


def test_different_callers_get_different_timeouts(tmp_path, reset_ocr_client, monkeypatch):
    """Two sequential calls with different `timeout` values must each see
    their own value applied — regression for the original singleton bug
    where the first caller's timeout was captured forever."""
    observed: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.extensions["timeout"]["read"])
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        ocr_mod._ocr_client,
        "_build",
        lambda: httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(60.0)),
    )

    img = _make_dummy_image(tmp_path)
    asyncio.run(ocr_mod.ocr_images([img], api_key="k", timeout=5.0))
    asyncio.run(ocr_mod.ocr_images([img], api_key="k", timeout=42.0))

    assert observed == [5.0, 42.0], (
        f"Expected per-call timeouts [5.0, 42.0], got {observed} — "
        "the second call's timeout was overridden by the first (singleton "
        "capturing the timeout argument)"
    )
