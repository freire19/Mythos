"""Regression tests for DEEP_BUGS wave 3 — Apify/audio null-field fixes.

- #DB22: JSON `null` fields from Apify (description/title/stats/data/items)
  must not crash `_list_actors` nor the run-status poll.
- #DB23: `audio.transcribe` must raise AudioTranscriptionError (not
  AttributeError) when a >=400 body is `{"error": null}` or non-dict.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from alpha import audio
from alpha.tools import apify_tools


# ─── fakes ───────────────────────────────────────────────────────


@dataclass
class FakeResponse:
    status_code: int = 200
    body: object = None

    def json(self):
        return self.body

    @property
    def text(self):
        return str(self.body)

    def raise_for_status(self):
        pass


@dataclass
class FakeAsyncClient:
    get_responses: list = field(default_factory=list)
    post_responses: list = field(default_factory=list)

    async def get(self, url, **kwargs):
        return self.get_responses.pop(0)

    async def post(self, url, **kwargs):
        return self.post_responses.pop(0)


def _patch_client(monkeypatch, client: FakeAsyncClient) -> None:
    async def fake_get_client(timeout: float = 30.0):
        return client

    monkeypatch.setattr(apify_tools, "_get_apify_client", fake_get_client)
    monkeypatch.setenv("APIFY_API_TOKEN", "test-token")


# ─── #DB22: _list_actors com campos null ─────────────────────────


class TestDB22ListActorsNullFields:
    def test_null_fields_do_not_crash(self, monkeypatch):
        client = FakeAsyncClient(get_responses=[
            FakeResponse(body={"data": {"items": [
                {
                    "username": None,
                    "name": None,
                    "title": None,
                    "description": None,
                    "stats": None,
                },
                {
                    "username": "apify",
                    "name": "web-scraper",
                    "title": "Web Scraper",
                    "description": "x" * 500,
                    "stats": {"totalRuns": 7},
                },
            ]}}),
        ])
        _patch_client(monkeypatch, client)

        result = asyncio.run(apify_tools._list_actors(search="scraper"))

        assert "error" not in result
        assert result["total"] == 2
        first, second = result["actors"]
        assert first["id"] == "/"
        assert first["title"] == ""
        assert first["description"] == ""
        assert first["runs"] == 0
        assert second["id"] == "apify/web-scraper"
        assert len(second["description"]) == 200
        assert second["runs"] == 7

    def test_null_items_do_not_crash(self, monkeypatch):
        client = FakeAsyncClient(get_responses=[
            FakeResponse(body={"data": {"items": None}}),
        ])
        _patch_client(monkeypatch, client)

        result = asyncio.run(apify_tools._list_actors())

        assert result == {"total": 0, "actors": []}


# ─── #DB22: poll de status com data null ─────────────────────────


class TestDB22PollNullData:
    def test_null_data_in_status_is_soft_error(self, monkeypatch):
        client = FakeAsyncClient(
            post_responses=[
                FakeResponse(body={"data": {"id": "run1", "defaultDatasetId": None}}),
            ],
            get_responses=[
                FakeResponse(body={"data": None}),
                FakeResponse(body={"data": {"status": "FAILED"}}),
            ],
        )
        _patch_client(monkeypatch, client)

        async def fake_sleep(seconds):
            pass

        monkeypatch.setattr(apify_tools.asyncio, "sleep", fake_sleep)

        result = asyncio.run(apify_tools._run_actor("apify/web-scraper"))

        # Antes do fix: AttributeError em `None.get("status")`. Agora o
        # payload malformado vira soft-retry e o proximo poll conclui.
        assert result == {"error": "Actor run FAILED", "run_id": "run1"}


# ─── #DB23: audio.transcribe com {"error": null} ─────────────────


class TestDB23AudioErrorNull:
    @pytest.mark.parametrize("body", [
        {"error": None},
        {"error": "plain string error"},
        ["not", "a", "dict"],
    ])
    def test_error_body_variants_raise_transcription_error(
        self, tmp_path, monkeypatch, body
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        clip = tmp_path / "clip.wav"
        clip.write_bytes(b"\0" * 128)

        def fake_post(url, **kwargs):
            return FakeResponse(status_code=400, body=body)

        monkeypatch.setattr(audio.httpx, "post", fake_post)

        with pytest.raises(audio.AudioTranscriptionError) as exc:
            audio.transcribe(Path(clip))
        assert "Whisper 400" in str(exc.value)
