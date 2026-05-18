"""Tests for `alpha.pdf` (Plano-Upgrade-v3 H3 #18).

We generate a tiny PDF on the fly using pypdf's writer so the test
suite stays hermetic — no checked-in binary fixtures, no external
dependencies beyond `pypdf` (already required by [multimodal]).
"""

from __future__ import annotations

import sys

import pytest

from alpha import pdf


pypdf = pytest.importorskip("pypdf")


def _make_pdf(tmp_path, pages: list[str]):
    """Create a multi-page PDF at tmp_path/test.pdf and return its Path.

    Uses pypdf's PdfWriter + a minimal page builder via add_blank_page;
    then we attach an annotation containing the text so extract_text()
    finds something to surface. This is enough to exercise the parsing
    path without depending on reportlab/fpdf.
    """
    # The easiest way to get genuine text into a pypdf-built PDF is to
    # construct a tiny PDF byte sequence by hand. This builds a valid
    # 1-page PDF for each entry in `pages` and merges them.
    from pypdf import PdfWriter

    writer = PdfWriter()
    for text in pages:
        # Generate a minimal PDF with this text via pypdf's helpers.
        page_pdf = _build_text_pdf(text, tmp_path)
        reader = pypdf.PdfReader(str(page_pdf))
        writer.add_page(reader.pages[0])

    out = tmp_path / "test.pdf"
    with out.open("wb") as fh:
        writer.write(fh)
    return out


def _build_text_pdf(text: str, tmp_path):
    """Construct a single-page PDF containing `text` using a raw PDF
    template. This avoids pulling in reportlab/fpdf as test deps."""
    # Escape parentheses (PDF string literal delimiter).
    safe = text.replace("(", r"\(").replace(")", r"\)")
    body = (
        "%PDF-1.4\n"
        "1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        "2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
        "3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
        "/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        f"4 0 obj<</Length {44 + len(safe)}>>stream\n"
        "BT /F1 12 Tf 10 100 Td (" + safe + ") Tj ET\n"
        "endstream endobj\n"
        "5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        "xref\n0 6\n0000000000 65535 f\n"
        "trailer<</Size 6/Root 1 0 R>>\nstartxref\n0\n%%EOF\n"
    )
    path = tmp_path / f"page_{abs(hash(text))}.pdf"
    path.write_bytes(body.encode("latin-1"))
    return path


def test_extract_text_returns_page_count(tmp_path):
    pdf_path = _make_pdf(tmp_path, ["Hello world page one", "Second page contents"])
    result = pdf.extract_text(pdf_path)
    assert result.page_count == 2
    assert "Hello world" in result.text
    assert "Second page" in result.text
    assert result.truncated is False


def test_extract_text_includes_page_separators(tmp_path):
    pdf_path = _make_pdf(tmp_path, ["First page", "Second page"])
    result = pdf.extract_text(pdf_path)
    assert "--- Page 1 ---" in result.text
    assert "--- Page 2 ---" in result.text


def test_extract_text_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        pdf.extract_text(tmp_path / "nope.pdf")


def test_extract_text_too_large(tmp_path, monkeypatch):
    # Cap the limit absurdly low so any valid PDF blows past it.
    monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 10)
    pdf_path = _make_pdf(tmp_path, ["x"])
    with pytest.raises(ValueError, match="too large"):
        pdf.extract_text(pdf_path)


def test_extract_text_corrupt_pdf_raises_extraction_error(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_text("not a pdf, just some text", encoding="utf-8")
    with pytest.raises(pdf.PDFExtractionError):
        pdf.extract_text(bad)


def test_extract_text_truncates_at_max_chars(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "MAX_CHARS", 50)
    pdf_path = _make_pdf(
        tmp_path,
        [
            "Page one text that is somewhat long",
            "Page two text continues even further",
        ],
    )
    result = pdf.extract_text(pdf_path)
    assert result.truncated is True
    assert len(result.text) <= 50 + 5  # +5 for the trailing ellipsis padding


def test_pdf_support_missing_when_pypdf_uninstalled(tmp_path, monkeypatch):
    pdf_path = _make_pdf(tmp_path, ["text"])
    # Drop pypdf from sys.modules and block re-import so the inner
    # `import pypdf` inside extract_text raises.
    monkeypatch.delitem(sys.modules, "pypdf", raising=False)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("pypdf not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(pdf.PDFSupportMissingError, match="pypdf"):
        pdf.extract_text(pdf_path)
