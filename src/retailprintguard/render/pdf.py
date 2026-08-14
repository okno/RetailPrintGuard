"""Deterministic receipt-style PDF rendering from normalized documents.

This module never reads the capture spool and is deliberately outside the proxy
dependency graph.  Its output is a derived view: the immutable RAW payload and
its checksum remain the authoritative evidence.
"""

from __future__ import annotations

import io
import re
import textwrap
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

PDF_RENDERER_VERSION = "rpg-receipt-pdf-1.0.0"
_ROME = ZoneInfo("Europe/Rome")
_PAGE_WIDTH = 80 * mm
_PAGE_HEIGHT = 297 * mm
_MARGIN = 6 * mm
_BODY_WIDTH = _PAGE_WIDTH - 2 * _MARGIN
_LINE_HEIGHT = 4.2 * mm
_MAX_SOURCE_CHARACTERS = 250_000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class DocumentRenderError(ValueError):
    """Raised when an unbounded or invalid normalized view cannot be rendered safely."""


@dataclass(frozen=True, slots=True)
class _RenderLine:
    text: str
    style: str = "body"


def _escape_controls(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub(lambda match: f"<0x{ord(match.group(0)):02X}>", value)


def _money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value.quantize(Decimal('0.01')):.2f}".replace(".", ",") + " EUR"


def _safe_text(value: Any, *, fallback: str = "—") -> str:
    if value is None:
        return fallback
    rendered = _escape_controls(str(value)).strip()
    return rendered or fallback


def _wrap(value: str, width: int = 42) -> Iterable[str]:
    for source_line in value.split("\n") or [""]:
        wrapped = textwrap.wrap(
            source_line,
            width=width,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=False,
        )
        yield from (wrapped or [""])


def _document_lines(document: Any) -> list[_RenderLine]:
    normalized = _safe_text(document.normalized_text, fallback="")
    if len(normalized) > _MAX_SOURCE_CHARACTERS:
        raise DocumentRenderError("normalized document exceeds the PDF rendering safety limit")

    timestamp = document.document_timestamp or document.captured_at
    local_timestamp = timestamp.astimezone(_ROME).strftime("%d/%m/%Y %H:%M:%S %Z")
    lines: list[_RenderLine] = [
        _RenderLine("RETAILPRINTGUARD", "title"),
        _RenderLine("DERIVATO DOCUMENTALE - NON RAW", "subtitle"),
        _RenderLine("=" * 42, "rule"),
        _RenderLine(f"Tipo: {_safe_text(document.type)}", "label"),
        _RenderLine(f"Sottotipo: {_safe_text(document.subtype)}"),
        _RenderLine(f"Stato: {_safe_text(document.status)}"),
        _RenderLine(f"Dispositivo: {_safe_text(document.device_id)}"),
        _RenderLine(f"Data: {local_timestamp}"),
    ]
    for label, value in (
        ("Documento", document.external_code),
        ("Ordine", document.order_code),
        ("Tavolo", document.table_code),
        ("Operatore", document.operator_code),
        ("Terminale", document.terminal_code),
    ):
        if value:
            lines.append(_RenderLine(f"{label}: {_safe_text(value)}"))

    lines.extend((_RenderLine("-" * 42, "rule"), _RenderLine("RIGHE", "label")))
    if document.lines:
        for item in document.lines:
            quantity = _safe_text(item.quantity, fallback="?")
            description = _safe_text(item.description, fallback="[senza descrizione]")
            operation = ""
            if item.removed:
                operation = " [RIMOSSO]"
            elif item.cancelled:
                operation = " [ANNULLATO]"
            for wrapped in _wrap(f"{quantity} x {description}{operation}", 42):
                lines.append(_RenderLine(wrapped))
            price = _money(item.unit_price)
            total = _money(item.line_total)
            lines.append(_RenderLine(f"  unitario {price}  riga {total}"))
    else:
        lines.append(_RenderLine("[nessuna riga strutturata]"))

    lines.extend(
        (
            _RenderLine("-" * 42, "rule"),
            _RenderLine(f"TOTALE LORDO     {_money(document.gross_total)}", "total"),
            _RenderLine(f"TOTALE NETTO     {_money(document.net_total)}", "total"),
            _RenderLine(f"SCONTI           {_money(document.discount_total)}"),
            _RenderLine(f"IMPOSTE          {_money(document.tax_total)}"),
        )
    )
    if document.payments:
        lines.append(_RenderLine("PAGAMENTI", "label"))
        for payment in document.payments:
            method = _safe_text(payment.get("method"), fallback="NON SPECIFICATO")
            amount_value = payment.get("amount")
            amount = None if amount_value is None else Decimal(str(amount_value))
            lines.append(_RenderLine(f"{method}: {_money(amount)}"))

    if normalized:
        lines.extend(
            (
                _RenderLine("-" * 42, "rule"),
                _RenderLine("TESTO NORMALIZZATO", "label"),
            )
        )
        lines.extend(_RenderLine(value) for value in _wrap(normalized, 42))

    if document.warnings:
        lines.extend(
            (
                _RenderLine("-" * 42, "rule"),
                _RenderLine("AVVISI DI PARSING", "label"),
            )
        )
        for warning in document.warnings:
            lines.extend(_RenderLine(value) for value in _wrap(f"- {_safe_text(warning)}", 42))

    lines.append(_RenderLine("=" * 42, "rule"))
    for value in (
        f"ID: {document.id}",
        f"SHA-256: {document.sha256}",
        f"Parser: {document.parser_name} {document.parser_version}",
        f"Renderer: {PDF_RENDERER_VERSION}",
    ):
        lines.extend(_RenderLine(part) for part in _wrap(value, 42))
    lines.append(_RenderLine("Il RAW immutabile resta l'evidenza primaria.", "subtitle"))
    return lines


def _font(style: str) -> tuple[str, float]:
    if style == "title":
        return "Helvetica-Bold", 13
    if style in {"label", "total"}:
        return "Courier-Bold", 8.5
    if style == "subtitle":
        return "Helvetica-Oblique", 7
    return "Courier", 8


def render_document_pdf(document: Any) -> bytes:
    """Render a normalized API document into a deterministic, bounded PDF."""

    render_lines = _document_lines(document)
    stream = io.BytesIO()
    canvas = Canvas(
        stream,
        pagesize=(_PAGE_WIDTH, _PAGE_HEIGHT),
        pageCompression=1,
        invariant=1,
    )
    canvas.setTitle(f"RetailPrintGuard {document.type} {document.id}")
    canvas.setSubject("Derived receipt view; immutable RAW remains authoritative")
    canvas.setAuthor("RetailPrintGuard")
    canvas.setCreator(PDF_RENDERER_VERSION)
    canvas.setProducer(PDF_RENDERER_VERSION)

    page_number = 1
    y = _PAGE_HEIGHT - _MARGIN

    def new_page() -> None:
        nonlocal page_number, y
        canvas.setFont("Helvetica", 6)
        canvas.drawRightString(_PAGE_WIDTH - _MARGIN, 3 * mm, f"pagina {page_number}")
        canvas.showPage()
        page_number += 1
        y = _PAGE_HEIGHT - _MARGIN

    for line in render_lines:
        if y < 10 * mm:
            new_page()
        font_name, font_size = _font(line.style)
        canvas.setFont(font_name, font_size)
        text = line.text
        if line.style in {"title", "subtitle"}:
            width = stringWidth(text, font_name, font_size)
            x = max(_MARGIN, (_PAGE_WIDTH - width) / 2)
        else:
            x = _MARGIN
        canvas.drawString(x, y, text)
        y -= _LINE_HEIGHT

    canvas.setFont("Helvetica", 6)
    canvas.drawRightString(_PAGE_WIDTH - _MARGIN, 3 * mm, f"pagina {page_number}")
    canvas.save()
    return stream.getvalue()


__all__ = ["DocumentRenderError", "PDF_RENDERER_VERSION", "render_document_pdf"]
