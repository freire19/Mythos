"""Tests for `alpha.audio` (Plano-Upgrade-v3 H3 #18).

httpx is the network boundary — we monkeypatch `httpx.post` so tests
don't touch the real Whisper endpoint. The fixture below builds a
fake `httpx.Response` so error-path tests can drive specific status
codes and JSON shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from alpha import audio


@dataclass
class FakeResponse:
    """Minimal stand-in for httpx.Response.

    Mirrors only the surface area `audio.transcribe` uses: status_code,
    .json(), .text. Implemented as a dataclass so tests can construct
    instances inline without subclassing httpx internals.
    """
    status_code: int
    body: dict | str

    def json(self):
        if isinstance(self.body, str):
            import json as _json
            return _json.loads(self.body)
        return self.body

    @property
    def text(self):
        return str(self.body)


def _write_wav(path: Path, size: int = 1024) -> Path:
    """Write a placeholder file with a .wav extension. Whisper requests
    are mocked, so the actual bytes never need to be valid audio."""
    path.write_bytes(b"\0" * size)
    return path


# ─── happy path ──────────────────────────────────────────────────


def test_transcribe_success(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio_file = _write_wav(tmp_path / "clip.wav")

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["data"] = kwargs.get("data")
        return FakeResponse(
            status_code=200,
            body={"text": "hello world transcript", "duration": 3.5},
        )

    monkeypatch.setattr(audio.httpx, "post", fake_post)

    result = audio.transcribe(audio_file)
    assert result.text == "hello world transcript"
    assert result.duration_sec == 3.5
    assert result.model == "whisper-1"

    # Confirms the auth header and endpoint are correct so a future
    # refactor doesn't silently break either.
    assert "openai.com" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["data"]["model"] == "whisper-1"


def test_transcribe_strips_whitespace(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio_file = _write_wav(tmp_path / "clip.wav")
    monkeypatch.setattr(
        audio.httpx,
        "post",
        lambda *a, **kw: FakeResponse(200, {"text": "  padded text  "}),
    )

    result = audio.transcribe(audio_file)
    assert result.text == "padded text"


# ─── pre-flight validation ───────────────────────────────────────


def test_transcribe_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(FileNotFoundError):
        audio.transcribe(tmp_path / "missing.wav")


def test_transcribe_unsupported_format(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bad = tmp_path / "doc.pdf"
    bad.write_bytes(b"\0")
    with pytest.raises(ValueError, match="unsupported audio format"):
        audio.transcribe(bad)


def test_transcribe_too_large(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(audio, "MAX_AUDIO_BYTES", 100)
    big = _write_wav(tmp_path / "big.wav", size=200)
    with pytest.raises(ValueError, match="too large"):
        audio.transcribe(big)


def test_transcribe_missing_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    audio_file = _write_wav(tmp_path / "clip.wav")
    with pytest.raises(audio.AudioSupportMissingError, match="OPENAI_API_KEY"):
        audio.transcribe(audio_file)


# ─── API error paths ─────────────────────────────────────────────


def test_transcribe_http_error_surfaces_message(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio_file = _write_wav(tmp_path / "clip.wav")
    monkeypatch.setattr(
        audio.httpx,
        "post",
        lambda *a, **kw: FakeResponse(
            400, {"error": {"message": "file too short"}}
        ),
    )

    with pytest.raises(audio.AudioTranscriptionError, match="file too short"):
        audio.transcribe(audio_file)


def test_transcribe_request_failure_wrapped(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio_file = _write_wav(tmp_path / "clip.wav")

    def boom(*a, **kw):
        raise audio.httpx.ConnectError("connection refused")

    monkeypatch.setattr(audio.httpx, "post", boom)

    with pytest.raises(audio.AudioTranscriptionError, match="connection refused"):
        audio.transcribe(audio_file)


def test_transcribe_empty_response(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio_file = _write_wav(tmp_path / "clip.wav")
    monkeypatch.setattr(
        audio.httpx, "post", lambda *a, **kw: FakeResponse(200, {"text": "   "})
    )
    with pytest.raises(audio.AudioTranscriptionError, match="empty"):
        audio.transcribe(audio_file)
