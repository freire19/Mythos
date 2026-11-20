"""
OCR via Google Gemini — extrai texto de imagens usando o endpoint
OpenAI-compatible do Gemini. Usado como fallback quando o provider
principal nao suporta visao (ex: DeepSeek).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path

import httpx

from ._http_singleton import LoopAwareClient
from ._security_log import sanitize_for_log

logger = logging.getLogger(__name__)

# LoopAwareClient — reusa httpx.AsyncClient entre chamadas OCR e
# rebuilda automaticamente quando o event loop troca (asyncio.run).
# Substitui singleton manual que ignorava timeout e crashava em
# single-shot mode (#029 / code-review).
_ocr_client = LoopAwareClient(
    name="ocr",
    build=lambda: httpx.AsyncClient(
        timeout=httpx.Timeout(60.0),
        limits=httpx.Limits(max_keepalive_connections=2, max_connections=10),
    ),
)


GEMINI_OCR_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
# Override via env to trade cost vs quality:
#   gemini-3.5-flash               — fast + multimodal (text/image/video/audio/PDF), good OCR default
#   gemini-3.1-pro-preview         — best vision, slow/expensive
#   gemini-3.1-flash-lite-preview  — mid quality
GEMINI_OCR_MODEL = os.getenv("GEMINI_OCR_MODEL", "gemini-3.5-flash")
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_EXT_TO_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _read_image(path: Path) -> tuple[bytes, str] | None:
    if not path.is_file():
        logger.warning("OCR skipped — file not found: %s", path)
        return None
    try:
        size = path.stat().st_size
    except OSError as e:
        logger.warning("OCR skipped — stat failed for %s: %s", path, e)
        return None
    if size > MAX_IMAGE_BYTES:
        logger.warning("OCR skipped — %s exceeds %d bytes", path, MAX_IMAGE_BYTES)
        return None
    try:
        data = path.read_bytes()
    except OSError as e:
        logger.warning("OCR skipped — read failed for %s: %s", path, e)
        return None
    media = _EXT_TO_MEDIA.get(path.suffix.lower(), "image/png")
    return data, media


_OCR_PROMPT = (
    "Extract ALL text visible in this image. "
    "Include any code, error messages, logs, file paths, numbers, "
    "labels, UI elements, terminal output — everything that is text. "
    "If it's a screenshot, describe the layout briefly then list all text. "
    "Return ONLY the extracted content, no conversational filler."
)

# Model that LOOKS at tool-produced screenshots (phone, desktop, browser) on
# behalf of a main model without vision. Needs layout understanding, not just
# text extraction. Override with GEMINI_VISION_MODEL.
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-4-hydrogen")

_DESCRIBE_PROMPT = (
    "You are the eyes of a coding agent that cannot see images. Describe this "
    "screenshot so it can act on it: which app/screen it is, the layout from "
    "top to bottom, every visible text verbatim, each button/field/tab with "
    "its approximate center in pixels as (x, y), and anything that looks "
    "wrong (error messages, overlapping or cut-off elements, blank areas, "
    "loading spinners). The image is {size}. Be precise and concise; no filler."
)


async def describe_images(
    image_paths: list[Path],
    api_key: str,
    question: str = "",
    model: str | None = None,
    timeout: float = 60.0,
) -> str:
    """Ask Gemini to describe screenshots for a model without vision.

    `question` (optional) is what the agent wants checked; it is appended to
    the standing description prompt. Returns "" when nothing could be read or
    the call fails.
    """
    sizes = []
    for path in image_paths:
        loaded = _read_image(path)
        if loaded and loaded[0][:8] == b"\x89PNG\r\n\x1a\n" and len(loaded[0]) >= 24:
            w = int.from_bytes(loaded[0][16:20], "big")
            h = int.from_bytes(loaded[0][20:24], "big")
            sizes.append(f"{w}x{h} pixels")
    prompt = _DESCRIBE_PROMPT.format(size=", ".join(sizes) or "of unknown size")
    if question.strip():
        prompt += f"\n\nThe agent specifically wants to know: {question.strip()}"
    return await ocr_images(
        image_paths, api_key, model=model or GEMINI_VISION_MODEL,
        timeout=timeout, prompt=prompt, max_tokens=4000,
    )


def describe_images_sync(
    image_paths: list[Path],
    api_key: str,
    question: str = "",
    model: str | None = None,
    timeout: float = 60.0,
) -> str:
    """Synchronous wrapper (call from a worker thread, not from a running loop)."""
    return asyncio.run(
        describe_images(image_paths, api_key, question=question, model=model, timeout=timeout)
    )


async def ocr_images(
    image_paths: list[Path],
    api_key: str,
    model: str = GEMINI_OCR_MODEL,
    timeout: float = 60.0,
    prompt: str = _OCR_PROMPT,
    max_tokens: int = 2000,
) -> str:
    """Send images to Gemini and return extracted text.

    Returns empty string if no images could be read or OCR fails.
    """
    content_blocks: list[dict] = [{"type": "text", "text": prompt}]

    for path in image_paths:
        loaded = _read_image(path)
        if loaded is None:
            continue
        data, media_type = loaded
        b64 = base64.b64encode(data).decode("ascii")
        content_blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{b64}"},
            }
        )

    if len(content_blocks) <= 1:  # only the text prompt, no images loaded
        return ""

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content_blocks}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        client = await _ocr_client.get()
        # Per-request timeout override — the shared LoopAwareClient was
        # built with a default 60s, but each caller can specify its own
        # via the `timeout` parameter. httpx honors per-request timeout
        # over the client-level default.
        resp = await client.post(
            f"{GEMINI_OCR_BASE}/chat/completions",
            json=payload,
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )
        if resp.status_code >= 400:
            # #D132: sanitize the error body before logging — Gemini error
            # bodies can echo request fragments / keys.
            logger.error(
                "Gemini OCR HTTP %d: %s",
                resp.status_code,
                sanitize_for_log(resp.text, max_chars=500),
            )
            return ""
        data = resp.json()
        # #D106: a resposta pode trazer content=null (safety block, resposta
        # vazia). Coagir para "" honra o contrato `-> str` — sem isso, retornar
        # None quebra qualquer `.strip()`/concatenacao no caller.
        return data["choices"][0]["message"]["content"] or ""
    except httpx.TimeoutException:
        logger.error("Gemini OCR timeout (%.0fs)", timeout)
        return ""
    except Exception as e:
        # #D106: exc_info preserva o frame onde o parse/index falhou.
        logger.error("Gemini OCR failed: %s", e, exc_info=True)
        return ""


def ocr_images_sync(
    image_paths: list[Path],
    api_key: str,
    model: str = GEMINI_OCR_MODEL,
    timeout: float = 60.0,
) -> str:
    """Synchronous wrapper for REPL context."""
    return asyncio.run(ocr_images(image_paths, api_key, model=model, timeout=timeout))
