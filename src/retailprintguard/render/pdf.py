"""Deterministic receipt-style PDF rendering from normalized documents.

The renderer is control-plane only.  It never reads or mutates the capture
spool: the PDF is a derived, human-readable view and the immutable RAW remains
the authoritative evidence.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import floor
from typing import Any
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

PDF_RENDERER_VERSION = "rpg-receipt-pdf-1.1.0"
_ROME = ZoneInfo("Europe/Rome")
_PAGE_WIDTH = 80 * mm
_MAX_PAGE_HEIGHT = 297 * mm
_MIN_PAGE_HEIGHT = 80 * mm
_MARGIN = 5 * mm
_FOOTER_HEIGHT = 8 * mm
_BODY_WIDTH = _PAGE_WIDTH - 2 * _MARGIN
_MAX_SOURCE_CHARACTERS = 250_000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_KITCHEN_TYPES = {"ORDER", "ORDER_CHANGE", "KITCHEN_ORDER"}
_UNSET = object()


class DocumentRenderError(ValueError):
    """Raised when an unbounded or invalid normalized view cannot be rendered safely."""


@dataclass(frozen=True, slots=True)
class _RenderLine:
    text: str = ""
    style: str = "body"


def _escape_controls(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub(lambda match: f"<0x{ord(match.group(0)):02X}>", value)


def _money(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{value.quantize(Decimal('0.01')):.2f}".replace(".", ",") + " EUR"


def _safe_text(value: Any, *, fallback: str = "-") -> str:
    if value is None:
        return fallback
    rendered = _escape_controls(str(value)).strip()
    return rendered or fallback


def _quantity(value: Decimal | None) -> str:
    if value is None:
        return "1"
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
        if decimal == decimal.to_integral_value():
            return str(int(decimal))
        return format(decimal.normalize(), "f").replace(".", ",")
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return _safe_text(value, fallback="1")


def _font(style: str) -> tuple[str, float]:
    if style == "title":
        return "Helvetica-Bold", 15
    if style == "table":
        return "Helvetica-Bold", 14
    if style == "course":
        return "Helvetica-Bold", 10
    if style in {"label", "total", "warning"}:
        return "Courier-Bold", 8.6
    if style == "subtitle":
        return "Helvetica-Oblique", 7
    if style == "meta":
        return "Helvetica", 5.8
    return "Courier", 8.4


def _advance(style: str) -> float:
    if style == "title":
        return 6.2 * mm
    if style == "table":
        return 6 * mm
    if style == "course":
        return 5.2 * mm
    if style == "rule":
        # Leave enough distance for the following bold heading; otherwise the
        # separator cuts through PORTATA on narrow thermal-receipt layouts.
        return 4.5 * mm
    if style == "meta":
        return 3 * mm
    if style == "subtitle":
        return 3.5 * mm
    return 4.25 * mm


def _maximum_characters(style: str) -> int:
    font_name, font_size = _font(style)
    sample_width = max(stringWidth("M", font_name, font_size), 1)
    return max(8, floor(_BODY_WIDTH / sample_width))


def _wrap(value: str, style: str = "body") -> Iterable[str]:
    """Wrap on visible word boundaries and never cross the receipt margins."""

    width = _maximum_characters(style)
    for source in value.split("\n") or [""]:
        remaining = source.rstrip()
        if not remaining:
            yield ""
            continue
        while len(remaining) > width:
            split_at = remaining.rfind(" ", 0, width + 1)
            if split_at < max(1, width // 3):
                split_at = width
            yield remaining[:split_at].rstrip()
            remaining = remaining[split_at:].lstrip()
        yield remaining


def _append_wrapped(lines: list[_RenderLine], value: str, style: str = "body") -> None:
    lines.extend(_RenderLine(part, style) for part in _wrap(value, style))


def _price_annotations(item: Any) -> list[str]:
    observed = getattr(item, "unit_price", None)
    derived = getattr(item, "derived_unit_price", None)
    source = getattr(item, "derived_price_source", None)
    attributions = list(getattr(item, "price_attributions", None) or [])
    price = observed if observed is not None else derived
    if price is None:
        if source == "CONFLICTING_SOURCES":
            candidates = sorted(
                {
                    (
                        str(getattr(attribution, "source_kind", "UNKNOWN")),
                        Decimal(str(attribution.observed_unit_price)),
                    )
                    for attribution in attributions
                    if getattr(attribution, "observed_unit_price", None) is not None
                },
                key=lambda value: (value[0], value[1]),
            )
            detail = " / ".join(
                f"{_safe_text(kind)} {_money(amount)}" for kind, amount in candidates
            )
            return [
                "Prezzi derivati in conflitto"
                + (f": {detail}" if detail else "")
            ]
        return []
    if observed is not None:
        return [f"Prezzo {_money(price)}"]

    labels = [f"Prezzo derivato {_money(price)}"]
    confidence = None
    candidates = [
        attribution
        for attribution in attributions
        if getattr(attribution, "source_kind", None) == source
        and getattr(attribution, "observed_unit_price", None) == derived
        and getattr(attribution, "status", None) in {"RESOLVED", "AGREED"}
    ]
    confidences = [
        getattr(attribution, "confidence", None)
        for attribution in candidates
        if getattr(attribution, "confidence", None) is not None
    ]
    if confidences:
        confidence = max(confidences)
    if confidence is not None:
        try:
            percent = Decimal(str(confidence))
            if percent <= 1:
                percent *= 100
            confidence_label = f"confidenza {percent.quantize(Decimal('1'))}%"
        except (InvalidOperation, TypeError, ValueError):
            confidence_label = None
    else:
        confidence_label = None
    provenance = " · ".join(
        value
        for value in (
            f"Fonte {_safe_text(source)}" if source else None,
            confidence_label,
        )
        if value
    )
    if provenance:
        labels.append(provenance)
    return labels


def _append_items(lines: list[_RenderLine], document: Any, *, kitchen: bool) -> bool:
    items = list(document.lines or [])
    if not items:
        return False
    current_course: str | None | object = _UNSET
    for item in items:
        course = _safe_text(getattr(item, "course_code", None), fallback="") or None
        if course != current_course:
            if current_course is not _UNSET:
                lines.append(_RenderLine(style="subtitle"))
            if course:
                lines.append(_RenderLine(f"PORTATA {course}", "course"))
            elif current_course is _UNSET and kitchen:
                lines.append(_RenderLine("ARTICOLI", "course"))
            current_course = course
        state = ""
        if getattr(item, "removed", False):
            state = " [RIMOSSO]"
        elif getattr(item, "cancelled", False):
            state = " [ANNULLATO]"
        description = _safe_text(
            getattr(item, "description", None) or getattr(item, "raw_text", None),
            fallback="[senza descrizione]",
        )
        style = "warning" if state else "body"
        _append_wrapped(
            lines,
            f"{_quantity(getattr(item, 'quantity', None))}x {description}{state}",
            style,
        )
        for price in _price_annotations(item):
            _append_wrapped(lines, f"  {price}", "subtitle")
    return True


def _metadata_footer(lines: list[_RenderLine], document: Any) -> None:
    lines.append(_RenderLine(style="rule"))
    short_id = str(document.id).split("-")[0]
    short_hash = str(document.sha256)[:12]
    lines.append(_RenderLine(f"Evidenza {short_id} | SHA-256 {short_hash}...", "meta"))
    lines.append(
        _RenderLine(
            f"{document.parser_name} {document.parser_version} | {PDF_RENDERER_VERSION}",
            "meta",
        )
    )
    lines.append(_RenderLine("Vista derivata; il RAW immutabile resta autoritativo.", "meta"))


def _kitchen_lines(document: Any, normalized: str) -> list[_RenderLine]:
    title = "MODIFICA COMANDA" if document.type == "ORDER_CHANGE" else "COMANDA"
    timestamp = document.document_timestamp or document.captured_at
    local_timestamp = timestamp.astimezone(_ROME).strftime("%d/%m/%Y %H:%M:%S")
    lines = [_RenderLine(title, "title")]
    if document.table_code:
        lines.append(_RenderLine(f"TAVOLO {document.table_code}", "table"))
    lines.append(_RenderLine(local_timestamp, "subtitle"))
    if document.operator_code:
        lines.append(_RenderLine(f"Operatore: {_safe_text(document.operator_code)}"))
    if getattr(document, "covers", None) is not None:
        lines.append(_RenderLine(f"Coperti: {int(document.covers)}", "label"))
    if document.order_code:
        lines.append(_RenderLine(f"Ordine: {_safe_text(document.order_code)}", "subtitle"))
    lines.append(_RenderLine(style="rule"))
    has_items = _append_items(lines, document, kitchen=True)
    if not has_items:
        _append_wrapped(lines, normalized or "[testo comanda non disponibile]")
    if document.gross_total is not None:
        lines.append(_RenderLine(style="rule"))
        lines.append(_RenderLine(f"TOTALE ATTRIBUITO  {_money(document.gross_total)}", "total"))
    if document.warnings:
        lines.append(_RenderLine(style="rule"))
        lines.append(_RenderLine("AVVISI DI LETTURA", "label"))
        for warning in document.warnings:
            _append_wrapped(lines, f"- {_safe_text(warning)}", "subtitle")
    _metadata_footer(lines, document)
    return lines


def _generic_lines(document: Any, normalized: str) -> list[_RenderLine]:
    timestamp = document.document_timestamp or document.captured_at
    local_timestamp = timestamp.astimezone(_ROME).strftime("%d/%m/%Y %H:%M:%S")
    title = _safe_text(document.subtype or document.type).replace("_", " ")
    lines: list[_RenderLine] = [
        _RenderLine(title, "title"),
        _RenderLine(_safe_text(document.type).replace("_", " "), "subtitle"),
        _RenderLine(style="rule"),
        _RenderLine(f"Data: {local_timestamp}"),
        _RenderLine(f"Dispositivo: {_safe_text(document.device_id)}"),
    ]
    progressive_status = {
        "FULL_CODE_OBSERVED_IN_CAPTURE": "progressivo completo osservato nel flusso",
        "SUFFIX_ONLY_OBSERVED_IN_CAPTURE": (
            "solo suffisso osservato; non e' un codice completo"
        ),
        "NOT_OBSERVED_IN_CAPTURE": (
            "progressivo proprio generato dalla RCH, non presente nel flusso osservato"
        ),
    }.get(getattr(document, "progressive_observation_status", None))
    resolved_code = getattr(document, "resolved_external_document_code", None)
    resolved_provenance = getattr(
        document, "resolved_external_document_code_provenance", None
    )
    resolved_display = (
        f"{_safe_text(resolved_code)} (da riferimento gestionale correlato)"
        if resolved_code
        and resolved_provenance == "CORRELATED_MANAGEMENT_REFERENCE"
        else resolved_code
    )
    for label, value in (
        (
            "Documento",
            getattr(document, "external_document_code", None)
            or document.external_code,
        ),
        ("Documento risolto", resolved_display),
        (
            "Suffisso progressivo RCH",
            getattr(document, "external_document_code_suffix", None),
        ),
        ("Osservabilita' progressivo", progressive_status),
        (
            "Rif. commerciale",
            getattr(document, "commercial_reference_code", None),
        ),
        ("Ordine", document.order_code),
        ("Tavolo", document.table_code),
        ("Operatore", document.operator_code),
        ("Terminale", document.terminal_code),
    ):
        if value:
            lines.append(_RenderLine(f"{label}: {_safe_text(value)}"))
    lines.append(_RenderLine(style="rule"))
    has_items = _append_items(lines, document, kitchen=False)
    if not has_items and normalized:
        _append_wrapped(lines, normalized)
    totals = [
        ("TOTALE LORDO", document.gross_total),
        ("TOTALE NETTO", document.net_total),
        ("SCONTI", document.discount_total),
        ("IMPOSTE", document.tax_total),
    ]
    if any(value is not None for _, value in totals):
        lines.append(_RenderLine(style="rule"))
        for label, value in totals:
            if value is not None:
                lines.append(_RenderLine(f"{label:<17}{_money(value)}", "total"))
    if document.payments:
        lines.append(_RenderLine("PAGAMENTI", "course"))
        for payment in document.payments:
            method = _safe_text(payment.get("method"), fallback="NON SPECIFICATO")
            amount_value = payment.get("amount")
            try:
                amount = None if amount_value is None else Decimal(str(amount_value))
            except (InvalidOperation, TypeError, ValueError):
                amount = None
            _append_wrapped(lines, f"{method}: {_money(amount)}")
    if document.warnings:
        lines.append(_RenderLine(style="rule"))
        lines.append(_RenderLine("AVVISI DI PARSING", "label"))
        for warning in document.warnings:
            _append_wrapped(lines, f"- {_safe_text(warning)}", "subtitle")
    _metadata_footer(lines, document)
    return lines


def _document_lines(document: Any) -> list[_RenderLine]:
    normalized = _safe_text(
        getattr(document, "receipt_text", None) or document.normalized_text,
        fallback="",
    )
    if len(normalized) > _MAX_SOURCE_CHARACTERS:
        raise DocumentRenderError("normalized document exceeds the PDF rendering safety limit")
    if str(document.type) in _KITCHEN_TYPES:
        return _kitchen_lines(document, normalized)
    return _generic_lines(document, normalized)


def _page_height(lines: list[_RenderLine]) -> float:
    requested = 2 * _MARGIN + _FOOTER_HEIGHT + sum(_advance(line.style) for line in lines)
    return max(_MIN_PAGE_HEIGHT, min(_MAX_PAGE_HEIGHT, requested))


def render_document_pdf(document: Any) -> bytes:
    """Render a normalized API document into a deterministic, bounded PDF."""

    render_lines = _document_lines(document)
    page_height = _page_height(render_lines)
    stream = io.BytesIO()
    canvas = Canvas(
        stream,
        pagesize=(_PAGE_WIDTH, page_height),
        pageCompression=1,
        invariant=1,
    )
    canvas.setTitle(f"RetailPrintGuard {document.type} {document.id}")
    canvas.setSubject("Derived receipt view; immutable RAW remains authoritative")
    canvas.setAuthor("RetailPrintGuard")
    canvas.setCreator(PDF_RENDERER_VERSION)
    canvas.setProducer(PDF_RENDERER_VERSION)
    canvas.setKeywords(
        f"document-id={document.id}; source-sha256={document.sha256}; "
        f"renderer={PDF_RENDERER_VERSION}"
    )

    page_number = 1
    y = page_height - _MARGIN

    def finish_page() -> None:
        canvas.setFillColor(colors.HexColor("#4A5568"))
        canvas.setFont("Helvetica", 5.5)
        canvas.drawRightString(_PAGE_WIDTH - _MARGIN, 2.5 * mm, f"pagina {page_number}")

    def draw(line: _RenderLine) -> None:
        nonlocal y
        advance = _advance(line.style)
        if line.style == "rule":
            canvas.setStrokeColor(colors.HexColor("#718096"))
            canvas.setLineWidth(0.45)
            canvas.line(_MARGIN, y - 0.8 * mm, _PAGE_WIDTH - _MARGIN, y - 0.8 * mm)
            y -= advance
            return
        font_name, font_size = _font(line.style)
        canvas.setFont(font_name, font_size)
        canvas.setFillColor(
            colors.HexColor("#9B2C2C")
            if line.style == "warning"
            else colors.HexColor("#4A5568")
            if line.style in {"subtitle", "meta"}
            else colors.HexColor("#111827")
        )
        if line.style in {"title", "table"}:
            width = stringWidth(line.text, font_name, font_size)
            x = max(_MARGIN, (_PAGE_WIDTH - width) / 2)
        else:
            x = _MARGIN
        canvas.drawString(x, y, line.text)
        y -= advance

    for line in render_lines:
        if y - _advance(line.style) < _FOOTER_HEIGHT:
            finish_page()
            canvas.showPage()
            page_number += 1
            y = page_height - _MARGIN
            continuation = (
                "COMANDA - CONTINUA"
                if str(document.type) in _KITCHEN_TYPES
                else "DOCUMENTO - CONTINUA"
            )
            draw(_RenderLine(continuation, "label"))
            draw(_RenderLine(style="rule"))
        draw(line)

    finish_page()
    canvas.save()
    return stream.getvalue()


__all__ = ["DocumentRenderError", "PDF_RENDERER_VERSION", "render_document_pdf"]
