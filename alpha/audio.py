"""Audio transcription via OpenAI Whisper (Plano-Upgrade-v3 H3 #18).

Why Whisper as the only path: it's accessible from any provider account
the user might already have (the OpenAI API key isn't specific to chat),
the model is mature, and the alternatives (Deepgram, AssemblyAI) would
add extra accounts. If a different transcription backend gets demand,
swap the implementation here without touching call sites.

Audio is transcribed to text before injection — no provider currently
takes audio inline reliably across models, and text-only fits Alpha's
universal-multimodal-by-text philosophy (see `pdf.py`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx

# Whisper's hard limit is 25 MB. Keeping the same cap here so the user
# gets a clean error rather than a 413 from the API.
MAX_AUDIO_BYTES = 25 * 1024 * 1024

# Whisper accepts these — we list them explicitly so the REPL can reject
# obviously wrong inputs (e.g. a .pdf typo'd into /audio) up front.
SUPPORTED_FORMATS = frozenset({
    ".mp3", ".mp4", ".mpeg", ".mpga",
    ".m4a", ".wav", ".webm", ".flac", ".ogg",
})


class AudioSupportMissingError(RuntimeError):
    """`OPENAI_API_KEY` env var is unset. Whisper requires it."""


class AudioTranscriptionError(RuntimeError):
    """Whisper returned an error or the file couldn't be sent."""


@dataclass
class Transcription:
    text: str
    model: str
    duration_sec: float | None = None


def transcribe(path: Path, *, model: str = "whisper-1", timeout: float = 120.0) -> Transcription:
    """Send `path` to the Whisper transcription endpoint and return the text.

    Raises:
      FileNotFoundError: path missing.
      ValueError: file too big or wrong extension.
      AudioSupportMissingError: OPENAI_API_KEY env var unset.
      AudioTranscriptionError: API call failed for any reason.
    """
    if not path.is_file():
        raise FileNotFoundError(f"audio not found: {path}")

    if path.suffix.lower() not in SUPPORTED_FORMATS:
        raise ValueError(
            f"unsupported audio format: {path.suffix} "
            f"(Whisper accepts: {', '.join(sorted(SUPPORTED_FORMATS))})"
        )

    size = path.stat().st_size
    if size > MAX_AUDIO_BYTES:
        raise ValueError(
            f"audio too large: {size:,} bytes "
            f"(Whisper hard limit: {MAX_AUDIO_BYTES:,})"
        )

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise AudioSupportMissingError(
            "OPENAI_API_KEY is required for /audio (Whisper transcription). "
            "Set it in .env or your environment."
        )

    try:
        with path.open("rb") as fh:
            response = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (path.name, fh, "application/octet-stream")},
                data={"model": model, "response_format": "json"},
                timeout=timeout,
            )
    except (httpx.HTTPError, OSError) as e:
        raise AudioTranscriptionError(f"transcription request failed: {e}") from e

    if response.status_code >= 400:
        # OpenAI returns structured errors; surface the message field
        # so the user sees "invalid_request_error: file too short" etc.
        try:
            err = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            err = response.text
        raise AudioTranscriptionError(
            f"Whisper {response.status_code}: {err}"
        )

    try:
        body = response.json()
    except ValueError as e:
        raise AudioTranscriptionError(f"invalid JSON from Whisper: {e}") from e

    text = (body.get("text") or "").strip()
    if not text:
        raise AudioTranscriptionError("Whisper returned empty transcription")

    return Transcription(
        text=text,
        model=model,
        duration_sec=body.get("duration"),
    )
