from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from pypdf import PdfReader
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import getAscentDescent, stringWidth
from reportlab.pdfgen.canvas import Canvas

import retailprintguard.render.pdf as renderer
from retailprintguard.render.pdf import (
    PDF_RENDERER_VERSION,
    DocumentRenderError,
    render_document_pdf,
)
from retailprintguard.render.text import receipt_preview_text


def document(text: str, **changes):
    values = dict(
        receipt_text=text,
        normalized_text="UNUSED PARSER TEXT",
        receipt_header=None,
        type="COMMERCIAL_DOCUMENT",
        subtype="RCH_COMMERCIALE_INFERRED",
        device_id="SYNTHETIC DEVICE METADATA",
        warnings=["UNPRINTED WARNING"],
        lines=[{"description": "UNPRINTED STRUCTURED ITEM"}],
        payments=[{"method": "UNPRINTED PAYMENT"}],
    )
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture
def drawings(monkeypatch):
    drawn = []

    class RecordingCanvas(Canvas):
        def drawString(self, x, y, text, *args, **kwargs):
            drawn.append((self._pageNumber, x, y, text, self._fontname,
                          self._fontsize, self._pagesize))
            return super().drawString(x, y, text, *args, **kwargs)

    monkeypatch.setattr(renderer, "Canvas", RecordingCanvas)
    return drawn


def test_pdf_text_equals_web_receipt_without_added_fields_or_footer(drawings):
    preview = "  LAB HOTEL  \n\nArticolo             4,00 €  \nTotale               4,00 €\n\n"
    source = document(preview)
    pdf = render_document_pdf(source)
    assert "\n".join(row[3] for row in drawings) == receipt_preview_text(source) == preview
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1
    extracted = reader.pages[0].extract_text()
    assert extracted == "".join(line + "\n" for line in preview.split("\n") if line)
    assert "RCH_COMMERCIALE" not in extracted
    assert "UNPRINTED" not in extracted
    assert "pagina" not in extracted
    assert PDF_RENDERER_VERSION not in extracted
    assert reader.metadata.producer == PDF_RENDERER_VERSION
    assert float(reader.pages[0].mediabox.width) == pytest.approx(80 * mm, abs=0.001)
    assert float(reader.pages[0].mediabox.height) < 80 * mm
    assert render_document_pdf(source) == pdf


@pytest.mark.parametrize("evidence,title,provenance", [
    ("DEVICE_METADATA_CONFIGURED", "", ""),
    ("RCH_DGFE_EXPORT", "INTESTAZIONE DOCUMENTO (MEMORIA FISCALE DGFE)",
     "Estratta dall'export della memoria fiscale DGFE"),
])
def test_configured_web_header_is_preserved_once(drawings, evidence, title, provenance):
    source = document("  Caffè        € 1,50\n", receipt_header={
        "evidence": evidence, "merchant_name": " LAB È ", "legal_name": "Test S.r.l.",
        "address_lines": ["Via Sintetica 1", "", "Città"], "phone": "0000000000",
        "tax_code": "SYNTHETIC", "vat_number": "00000000000",
    })
    expected = (
        (title + "\n" if title else "")
        + " LAB È \nTest S.r.l.\nVia Sintetica 1\n\nCittà\n"
        "Telefono 0000000000\nC.F. SYNTHETIC\nP.IVA 00000000000"
        + ("\n\n" + provenance if provenance else "") + "\n\n  Caffè        € 1,50\n"
    )
    assert receipt_preview_text(source) == expected
    pdf = render_document_pdf(source)
    # A long provenance line may wrap, but no character or whitespace is lost.
    assert "".join(row[3] for row in drawings) == expected.replace("\n", "")
    extracted = "".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    assert extracted.count("LAB È") == 1
    assert "Caffè        € 1,50" in extracted
    assert "INTESTAZIONE DOCUMENTO (CONFIGURATA)" not in extracted
    assert "Configurata sul dispositivo" not in extracted
    assert source.receipt_header["evidence"] == evidence


def test_receipt_columns_and_blank_line_geometry_are_preserved(drawings):
    preview = (
        "\n" + "Articolo".ljust(40) + "    4,00" + "\n\n\n"
        + "Totale".ljust(40) + "    4,00\n"
    )
    render_document_pdf(document(preview))
    assert [row[3] for row in drawings] == preview.split("\n")
    assert len({row[4:6] for row in drawings}) == 1
    assert 6.5 <= drawings[0][5] < 8.4  # 48 columns fit without wrapping.
    for first, second in zip(drawings, drawings[1:], strict=False):
        assert first[2] - second[2] == pytest.approx(first[5] * 1.55)
    for _, x, y, text, font, size, (width, height) in drawings:
        ascent, descent = getAscentDescent(font, size)
        assert x == pytest.approx(5 * mm)
        assert x + stringWidth(text, font, size) <= width - 5 * mm + 1e-7
        assert y + ascent <= height - 5 * mm + 1e-7
        assert y + descent >= 5 * mm - 1e-7


def test_long_lines_wrap_without_discarding_spaces_and_every_glyph_stays_inside(drawings):
    preview = "  " + "é€W" * 115 + "     " + "word " * 40 + "  "
    pdf = render_document_pdf(document(preview))
    assert len(drawings) > 3
    assert "".join(row[3] for row in drawings) == preview
    assert drawings[0][3].startswith("  ")
    assert drawings[-1][3].endswith("  ")
    for _, x, y, text, font, size, (width, height) in drawings:
        ascent, descent = getAscentDescent(font, size)
        assert x + stringWidth(text, font, size) <= width - 5 * mm + 1e-7
        assert y + ascent <= height - 5 * mm + 1e-7
        assert y + descent >= 5 * mm - 1e-7
    extracted = "".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    assert extracted.replace("\n", "") == preview


def test_unicode_is_embedded_extractable_and_not_replaced():
    preview = "Caffè àèéìòù ÀÈÉÌÒÙ €\nCrème brûlée – Straße\nαβ Ω Ж © £"
    pdf = render_document_pdf(document(preview))
    reader = PdfReader(io.BytesIO(pdf))
    assert reader.pages[0].extract_text() == preview + "\n"
    fonts = reader.pages[0]["/Resources"]["/Font"].get_object().values()
    embedded = [font.get_object() for font in fonts if "/FontDescriptor" in font.get_object()]
    assert embedded
    assert all("/FontFile2" in font["/FontDescriptor"] for font in embedded)
    assert all("/ToUnicode" in font for font in embedded)


def test_pagination_keeps_every_line_and_has_no_page_numbers_or_continuation_title(drawings):
    preview = "\n".join(f"Riga sintetica {index:04d}      {index},00" for index in range(250))
    pdf = render_document_pdf(document(preview))
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) > 1
    assert [row[3] for row in drawings] == preview.split("\n")
    assert "".join(page.extract_text() for page in reader.pages) == preview + "\n"
    assert all(0 < float(page.mediabox.height) <= 297 * mm for page in reader.pages)
    assert all(page.extract_text().strip() for page in reader.pages)


def test_tabs_and_line_endings_follow_browser_pre_wrap_layout(drawings):
    preview = "A\tB\r\n\rC\tD\n"
    source = document(preview)
    render_document_pdf(source)
    assert receipt_preview_text(source) == preview
    assert [row[3] for row in drawings] == ["A       B", "", "C       D", ""]


@pytest.mark.parametrize("preview", ["bad\x00text", "bad\x1btext", "bad\x7ftext", "\U0010ffff"])
def test_unrepresentable_text_fails_instead_of_silently_losing_characters(preview):
    with pytest.raises(DocumentRenderError, match="control character|absent"):
        render_document_pdf(document(preview))


@pytest.mark.parametrize("preview,match", [
    ("W" * 250_001, "safety limit"),
    ("\n" * 10_001, "line limit"),
    ("\t" * 100_000, "layout safety limit"),
])
def test_input_and_expansion_limits_fail_without_truncation(preview, match):
    with pytest.raises(DocumentRenderError, match=match):
        render_document_pdf(document(preview))
