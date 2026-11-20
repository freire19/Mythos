"""#D816 — Apify: timeout por-request no client singleton + poll com deadline real."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from alpha.tools import apify_tools


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _start_resp():
    return _FakeResponse({"data": {"id": "run1", "defaultDatasetId": "ds1"}})


def _status_resp(status):
    return _FakeResponse({"data": {"status": status}})


class _FakeClient:
    def __init__(self, get_responses):
        self.post = AsyncMock(return_value=_start_resp())
        self.get = AsyncMock(side_effect=get_responses)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "test-token")


def test_list_actors_passes_per_request_timeout():
    client = _FakeClient([_FakeResponse({"data": {"items": []}})])
    with patch.object(apify_tools, "_get_apify_client", AsyncMock(return_value=client)):
        result = asyncio.run(apify_tools._list_actors(search="x"))
    assert "error" not in result
    assert client.get.call_args.kwargs["timeout"] == 15.0


def test_run_actor_passes_per_request_timeouts():
    client = _FakeClient([_status_resp("SUCCEEDED"), _FakeResponse([{"a": 1}])])
    with (
        patch.object(apify_tools, "_get_apify_client", AsyncMock(return_value=client)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = asyncio.run(apify_tools._run_actor("apify/web-scraper"))
    assert result.get("status") == "SUCCEEDED"
    assert client.post.call_args.kwargs["timeout"] == 30.0
    for call in client.get.call_args_list:
        assert call.kwargs["timeout"] == 30.0


def test_poll_deadline_counts_round_trip_not_only_sleep():
    """GETs lentos consomem o orçamento mesmo com sleeps curtos."""
    client = _FakeClient([_status_resp("RUNNING")] * 50)
    clock = {"t": 0.0}

    def fake_monotonic():
        t = clock["t"]
        clock["t"] += 120.0  # cada checagem simula 120s de wall-clock decorridos
        return t

    with (
        patch.object(apify_tools, "_get_apify_client", AsyncMock(return_value=client)),
        patch.object(apify_tools, "time", SimpleNamespace(monotonic=fake_monotonic)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = asyncio.run(apify_tools._run_actor("apify/web-scraper"))
    assert "Timeout waiting" in result.get("error", "")
    assert result.get("run_id") == "run1"
    # start(0) → check(120, entra) → elapsed(240) → check(360, sai): 1 poll só
    assert client.get.call_count == 1


def test_get_apify_client_no_longer_fixes_first_caller_timeout():
    """O construtor do singleton não recebe mais o timeout dos callers."""
    import inspect

    src = inspect.getsource(apify_tools._run_actor) + inspect.getsource(
        apify_tools._list_actors
    )
    assert "_get_apify_client(timeout=" not in src
