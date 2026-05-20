"""Attachment handlers: /image, /pdf, /audio.

These don't live in the ``_DISPATCH`` table because they need the
original ``user_input`` (not just ``parts``) to extract the optional
message text after the path.
"""

from __future__ import annotations

import os
from pathlib import Path

from alpha.attachments import build_user_content  # noqa: F401  — kept for back-compat
from alpha.display import C, c, print_error

from ._types import DispatchResult, ReplContext


def handle_image(
    ctx: ReplContext, user_input: str, parts: list[str]
) -> DispatchResult:
    """Resolve `/image <path> [message]` and FALL THROUGH with attachment.

    Kept out of ``_DISPATCH`` because it needs the full ``user_input`` to
    extract the optional message after the path (the dispatcher only
    passes ``parts``).
    """
    if len(parts) < 2:
        print(f"  {c(C.GRAY, 'Usage: /image <path> [optional message]')}")
        print(f"  {c(C.GRAY, 'Example: /image /tmp/screenshot.png what is wrong?')}")
        return DispatchResult.CONTINUE

    img_path_str = parts[1]
    img_path = Path(os.path.expanduser(img_path_str))
    if not img_path.is_file():
        print_error(f"Image not found: {img_path}")
        return DispatchResult.CONTINUE

    rest = user_input.split(maxsplit=2)
    msg_text = rest[2] if len(rest) >= 3 else "What's in this image?"
    print(c(C.GRAY, f"  (1 image attached: {img_path.name})"))

    ctx.user_input_override = msg_text
    ctx.image_paths_override = [img_path]
    ctx.history_record_override = f"[image: {img_path.name}] {msg_text}"
    return DispatchResult.FALL_THROUGH


def handle_pdf(
    ctx: ReplContext, user_input: str, parts: list[str]
) -> DispatchResult:
    """`/pdf <path> [optional question]` — extract text and FALL_THROUGH.

    The extracted text is prepended to the user message so any provider
    (vision-capable or not) can process it. Truncation and per-page
    framing are documented in `alpha/pdf.py`.
    """
    if len(parts) < 2:
        print(f"  {c(C.GRAY, 'Usage: /pdf <path> [optional question]')}")
        return DispatchResult.CONTINUE

    from alpha import pdf as _pdf

    pdf_path = Path(os.path.expanduser(parts[1]))
    rest = user_input.split(maxsplit=2)
    question = rest[2] if len(rest) >= 3 else "Summarize this PDF."

    try:
        extraction = _pdf.extract_text(pdf_path)
    except FileNotFoundError as e:
        print_error(str(e))
        return DispatchResult.CONTINUE
    except _pdf.PDFSupportMissingError as e:
        print_error(str(e))
        return DispatchResult.CONTINUE
    except (_pdf.PDFExtractionError, ValueError) as e:
        print_error(f"PDF: {e}")
        return DispatchResult.CONTINUE

    suffix = " (truncated)" if extraction.truncated else ""
    print(
        c(
            C.GRAY,
            f"  ({extraction.page_count} page(s), "
            f"{len(extraction.text):,} chars{suffix})",
        )
    )

    ctx.user_input_override = (
        f"[Attached PDF: {pdf_path.name}]\n"
        "--- BEGIN PDF TEXT ---\n"
        f"{extraction.text}\n"
        "--- END PDF TEXT ---\n\n"
        f"{question}"
    )
    ctx.history_record_override = f"[pdf: {pdf_path.name}] {question}"
    return DispatchResult.FALL_THROUGH


def handle_audio(
    ctx: ReplContext, user_input: str, parts: list[str]
) -> DispatchResult:
    """`/audio <path> [optional question]` — transcribe via Whisper, FALL_THROUGH.

    Same flow as /pdf: text is injected as context, all providers can
    consume it without needing audio-capable models.
    """
    if len(parts) < 2:
        print(f"  {c(C.GRAY, 'Usage: /audio <path> [optional question]')}")
        return DispatchResult.CONTINUE

    from alpha import audio as _audio

    audio_path = Path(os.path.expanduser(parts[1]))
    rest = user_input.split(maxsplit=2)
    question = rest[2] if len(rest) >= 3 else "Summarize this audio."

    print(c(C.GRAY, f"  ✾ Transcrevendo {audio_path.name}..."))
    try:
        transcription = _audio.transcribe(audio_path)
    except FileNotFoundError as e:
        print_error(str(e))
        return DispatchResult.CONTINUE
    except _audio.AudioSupportMissingError as e:
        print_error(str(e))
        return DispatchResult.CONTINUE
    except (_audio.AudioTranscriptionError, ValueError) as e:
        print_error(f"audio: {e}")
        return DispatchResult.CONTINUE

    dur = (
        f" ({transcription.duration_sec:.1f}s)"
        if transcription.duration_sec is not None
        else ""
    )
    print(
        c(
            C.GRAY,
            f"  ({len(transcription.text):,} chars transcribed{dur} "
            f"via {transcription.model})",
        )
    )

    ctx.user_input_override = (
        f"[Attached audio: {audio_path.name}]\n"
        "--- BEGIN TRANSCRIPT ---\n"
        f"{transcription.text}\n"
        "--- END TRANSCRIPT ---\n\n"
        f"{question}"
    )
    ctx.history_record_override = f"[audio: {audio_path.name}] {question}"
    return DispatchResult.FALL_THROUGH
