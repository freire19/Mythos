"""PDF text extraction for REPL attachments (Plano-Upgrade-v3 H3 #18).

Why text extraction over native PDF blocks: only Anthropic supports
`document` blocks today, and even there the file caps are aggressive.
Extracted text works across every provider Alpha targets, doesn't
multiply token cost by base64 encoding, and the extraction lives in
one place we can swap later if model support catches up.

`pypdf` is an optional dependency (`pip install -e ".[multimodal]"`).
When missing, `extract_text` raises `PDFSupportMissingError` so the
REPL can show a clear pip-install hint instead of a stack trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MAX_PDF_BYTES = 20 * 1024 * 1024  # 20 MB — bigger than images, still bounded


class PDFSupportMissingError(RuntimeError):
    """`pypdf` isn't installed. Surfaces a copy-paste pip command."""


class PDFExtractionError(RuntimeError):
    """The PDF was read but parsing failed (encrypted / corrupt / etc)."""


@dataclass
class PDFExtraction:
    text: str
    page_count: int
    truncated: bool  # True when output was clipped to fit MAX_CHARS


# Loose cap so a 200-page PDF doesn't dominate the entire context window.
# At ~1500 chars/page average, this is roughly 30 pages of dense text or
# a much larger range of sparser pages.
MAX_CHARS = 50_000


def extract_text(path: Path) -> PDFExtraction:
    """Read a PDF and return its concatenated text content.

    Raises:
      FileNotFoundError: path doesn't exist or isn't a regular file.
      ValueError: file exceeds MAX_PDF_BYTES.
      PDFSupportMissingError: pypdf isn't installed.
      PDFExtractionError: parsing failed (encrypted / corrupt PDF).
    """
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")

    size = path.stat().st_size
    if size > MAX_PDF_BYTES:
        raise ValueError(
            f"PDF too large: {size:,} bytes (limit {MAX_PDF_BYTES:,})"
        )

    try:
        import pypdf
    except ImportError as e:
        raise PDFSupportMissingError(
            "pypdf is required for PDF attachments. "
            'Install with: pip install -e ".[multimodal]"  (or pip install pypdf)'
        ) from e

    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as e:
        # pypdf raises a variety of internal exceptions (PdfReadError,
        # DependencyError for encrypted PDFs, etc); the user just needs
        # "this PDF is unreadable" — the specific class doesn't help.
        raise PDFExtractionError(f"could not parse PDF: {e}") from e

    if getattr(reader, "is_encrypted", False):
        raise PDFExtractionError(
            "PDF is encrypted (password-protected); decrypt it before attaching."
        )

    chunks: list[str] = []
    total_chars = 0
    truncated = False

    for page_num, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            # A single broken page shouldn't kill the whole extraction —
            # PDFs in the wild routinely have one mangled page from a
            # font issue. Note the gap so the LLM knows what's missing.
            text = f"[page {page_num}: extraction failed]"

        text = text.strip()
        if not text:
            continue

        chunk = f"--- Page {page_num} ---\n{text}"
        if total_chars + len(chunk) > MAX_CHARS:
            remaining = MAX_CHARS - total_chars
            if remaining > 200:
                chunks.append(chunk[:remaining] + "…")
            truncated = True
            break

        chunks.append(chunk)
        total_chars += len(chunk)

    return PDFExtraction(
        text="\n\n".join(chunks),
        page_count=len(reader.pages),
        truncated=truncated,
    )
