"""Deterministic PDFs of the exact text shown in the web Scontrino tab.

Only layout is applied: one embedded monospace font, preserved whitespace and
bounded receipt pages. No inferred items, titles, provenance or footer are added.
The renderer never reads or changes RAW, document versions or published exports.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from math import floor
from pathlib import Path
from threading import Lock
from typing import Any

from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import getAscentDescent, getFont, registerFont, stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from retailprintguard.render.text import receipt_preview_text

PDF_RENDERER_VERSION = "rpg-receipt-pdf-2.0.1"
_PAGE_WIDTH = 80 * mm
_MAX_PAGE_HEIGHT = 297 * mm
_MARGIN = 5 * mm
_BODY_WIDTH = _PAGE_WIDTH - 2 * _MARGIN
_MAX_FONT_SIZE = 8.4
_MIN_FONT_SIZE = 6.5
_LINE_HEIGHT_RATIO = 1.55  # The web Scontrino tab uses the same line-height.
_FONT_NAME = "RPG-DejaVuSansMono"
_FONT_LOCK = Lock()
_MAX_SOURCE_CHARACTERS = 250_000
_MAX_LAYOUT_CHARACTERS = 500_000
_MAX_RENDER_LINES = 10_000
_TAB_SIZE = 8  # CSS pre-wrap's default tab stops.


class DocumentRenderError(ValueError):
    """The complete preview cannot be represented within the rendering limits."""


@dataclass(frozen=True, slots=True)
class _ReceiptLayout:
    lines: tuple[str, ...]
    font_size: float
    line_height: float
    ascent: float
    descent: float


def _receipt_font() -> TTFont:
    with _FONT_LOCK:
        try:
            return getFont(_FONT_NAME)
        except KeyError:
            font_path = Path(__file__).with_name("fonts") / "DejaVuSansMono.ttf"
            font = TTFont(_FONT_NAME, str(font_path))
            registerFont(font)
            return font


def _layout_preview(preview: str) -> _ReceiptLayout:
    if len(preview) > _MAX_SOURCE_CHARACTERS:
        raise DocumentRenderError("receipt preview exceeds the PDF rendering safety limit")
    font = _receipt_font()
    # CRLF/CR and tabs have layout semantics in CSS pre-wrap. No other control
    # bytes are silently dropped or converted to extra printed annotations.
    for character in set(preview):
        if character in "\r\n\t":
            continue
        codepoint = ord(character)
        if codepoint < 32 or 127 <= codepoint <= 159:
            raise DocumentRenderError("receipt preview contains an unsupported control character")
        if codepoint not in font.face.charToGlyph:
            raise DocumentRenderError(
                "receipt preview contains a character absent from the PDF font"
            )
    normalized_breaks = preview.replace("\r\n", "\n").replace("\r", "\n")
    source_lines = normalized_breaks.split("\n")
    if len(source_lines) > _MAX_RENDER_LINES:
        raise DocumentRenderError("receipt preview exceeds the PDF line limit")
    expanded_lines: list[str] = []
    expanded_characters = 0
    for source in source_lines:
        expanded = source.expandtabs(_TAB_SIZE)
        expanded_characters += len(expanded)
        if expanded_characters > _MAX_LAYOUT_CHARACTERS:
            raise DocumentRenderError("receipt preview exceeds the PDF layout safety limit")
        expanded_lines.append(expanded)

    maximum_width = max(stringWidth(line, _FONT_NAME, 1) for line in expanded_lines)
    font_size = (
        min(_MAX_FONT_SIZE, max(_MIN_FONT_SIZE, _BODY_WIDTH / maximum_width))
        if maximum_width else _MAX_FONT_SIZE
    )
    fitted: list[str] = []
    for source in expanded_lines:
        # Calculate actual glyph widths once; never strip leading/trailing spaces
        # at wrapping boundaries. Joining these parts recovers the whole line.
        start = 0
        width = 0.0
        last_space = 0
        index = 0
        while index < len(source):
            character_width = font.face.charWidths[ord(source[index])] * font_size / 1000
            if width + character_width > _BODY_WIDTH + 1e-8:
                end = last_space if last_space > start else index
                if end == start:
                    raise DocumentRenderError("a glyph exceeds the PDF rendering width")
                fitted.append(source[start:end])
                start = end
                index = end
                width = 0.0
                last_space = end
                continue
            width += character_width
            index += 1
            if source[index - 1] == " ":
                last_space = index
        fitted.append(source[start:])
        if len(fitted) > _MAX_RENDER_LINES:
            raise DocumentRenderError("receipt preview exceeds the PDF line limit")
    ascent, descent = getAscentDescent(_FONT_NAME, font_size)
    return _ReceiptLayout(
        tuple(fitted), font_size, font_size * _LINE_HEIGHT_RATIO, ascent, descent
    )


def render_document_pdf(document: Any) -> bytes:
    """Render only the shared web preview, preserving its text and alignment."""
    layout = _layout_preview(receipt_preview_text(document))
    glyph_height = layout.ascent - layout.descent
    lines_per_page = 1 + floor(
        (_MAX_PAGE_HEIGHT - 2 * _MARGIN - glyph_height) / layout.line_height
    )
    stream = io.BytesIO()
    canvas = Canvas(stream, pageCompression=1, invariant=1)
    # Renderer provenance belongs to PDF properties and the export manifest,
    # never the printed receipt. Do not copy identifiers or hashes into the PDF.
    canvas.setTitle("")
    canvas.setAuthor("")
    canvas.setSubject("")
    canvas.setCreator(PDF_RENDERER_VERSION)
    canvas.setProducer(PDF_RENDERER_VERSION)
    for offset in range(0, len(layout.lines), lines_per_page):
        page_lines = layout.lines[offset:offset + lines_per_page]
        page_height = (
            2 * _MARGIN + glyph_height + (len(page_lines) - 1) * layout.line_height
        )
        canvas.setPageSize((_PAGE_WIDTH, page_height))
        canvas.setFont(_FONT_NAME, layout.font_size)
        canvas.setFillColorRGB(0, 0, 0)
        y = page_height - _MARGIN - layout.ascent
        for line in page_lines:
            canvas.drawString(_MARGIN, y, line)
            y -= layout.line_height
        canvas.showPage()
    canvas.save()
    return stream.getvalue()


__all__ = ["DocumentRenderError", "PDF_RENDERER_VERSION", "render_document_pdf"]
