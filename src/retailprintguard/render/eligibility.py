"""Versioned PDF selection without changing document interpretations or text.

Completeness is the parser's observed document boundary, never certification of
fiscal emission. Known protocol records and empty/layout-only bodies are kept as
evidence but omitted from receipt archives. Readable partial documents form their
own category. Dates, identifiers, amounts and job status are not prerequisites.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

DOCUMENT_PDF_POLICY_VERSION = "rpg-document-pdf-policy-1.0.0"

_DOCUMENT_TYPES = frozenset({
    "ORDER", "ORDER_CHANGE", "KITCHEN_ORDER", "PRE_BILL", "MANAGEMENT_DOCUMENT",
    "COMMERCIAL_DOCUMENT", "SHIFT_END_REPORT", "INVOICE", "CONFORMING_COPY",
    "CANCELLATION", "REFUND", "REPRINT", "PAYMENT",
})
_FOOTER_DOCUMENT_TYPES = frozenset({
    "MANAGEMENT_DOCUMENT", "COMMERCIAL_DOCUMENT", "PRE_BILL", "CONFORMING_COPY",
})
_SEPARATOR = re.compile(r"[\s\-_=.*]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DATE = r"\d{2}[/\\.\-]\d{2}[/\\.\-](?:\d{4}|\d{2})"
_DATE_TIME = _DATE + r"(?:,?\s+\d{2}:\d{2}(?::\d{2})?)?"
_CODE = r"[A-Z0-9]+(?:[-/][A-Z0-9]+)+"
_REFERENCE = re.compile(
    r"(?:SCONTRINO\s*:\s*SF\s*" + _CODE + r"(?:\s+" + _DATE_TIME + r")?"
    r"|DOC(?:UMENTO)?\.?\s+GESTIONALE\s+N[.°º]?\s*" + _CODE
    + r"|DOCUMENTO\s+COMMERCIALE\s+N[.°º]?\s*" + _CODE
    + r"|RIF(?:ERIMENTO)?\.?\s+(?:AL\s+)?DOCUMENTO(?:\s+COMMERCIALE)?"
    r"\s+N[.°º]?\s*" + _CODE + r")",
    re.IGNORECASE,
)
_FOOTER_TIMESTAMP = re.compile(_DATE_TIME)
_FOOTER_SERIAL = re.compile(r"RT\s*[:#-]?\s*[A-Z0-9]{8,32}", re.IGNORECASE)
_EMPTY_REPORT_HEADINGS = frozenset({
    "REPORT DI FINE TURNO", "PAGAMENTI", "LIBRO CASSA", "EURO",
})


@dataclass(frozen=True, slots=True)
class PdfEligibilityDecision:
    category: Literal["complete", "partial", "excluded"]
    reason_code: str


def _field(document: Any, name: str) -> Any:
    return document.get(name) if isinstance(document, dict) else getattr(document, name, None)


def _inspection_text(source: str) -> str:
    # A single forward scan removes only explicit parser annotations, retaining
    # OCR payload text. Unlike matching paired OCR tags, malformed/repeated
    # opening tags cannot cause quadratic rescans. This copy is never rendered.
    fragments: list[str] = []
    cursor = 0
    while (start := source.find("<", cursor)) >= 0:
        end = source.find(">", start + 1)
        if end < 0:
            break
        fragments.append(source[cursor:start])
        token = source[start + 1:end]
        if not token.startswith(("ESC/POS:", "BYTE:", "OCR:", "/OCR:")):
            fragments.append(source[start:end + 1])
        cursor = end + 1
    fragments.append(source[cursor:])
    return _CONTROL.sub("", "".join(fragments))


def _footer_only(kind: str, lines: tuple[str, ...]) -> bool:
    # Require an explicit document-reference anchor. Generic words, timestamps,
    # SKU-like strings or an item containing "Scontrino" are not enough.
    if kind not in _FOOTER_DOCUMENT_TYPES or not any(_REFERENCE.fullmatch(line) for line in lines):
        return False
    return all(
        _REFERENCE.fullmatch(line)
        or _FOOTER_TIMESTAMP.fullmatch(line)
        or _FOOTER_SERIAL.fullmatch(line)
        or _SEPARATOR.fullmatch(line)
        for line in lines
    )


def classify_pdf_document(document: Any) -> PdfEligibilityDecision:
    """Classify the original body; never count configured headers as content.

    This function has no side effects and does not read computed preview fields,
    database state, current time, filesystem or external services. Even apparently
    structured metadata rows cannot override an empty or exclusively technical
    body: RCH creates description/quantity rows for headings and footers too.
    """
    kind = str(_field(document, "type") or "")
    subtype = str(_field(document, "subtype") or "")
    if kind == "DEVICE_RESPONSE":
        return PdfEligibilityDecision("excluded", "DEVICE_RESPONSE")
    if kind == "CANCELLATION" and subtype == "RCH_DOCUMENT_CANCEL_COMMAND_OBSERVED":
        return PdfEligibilityDecision("excluded", "TECHNICAL_CANCEL_COMMAND")

    source = _field(document, "receipt_text") or _field(document, "normalized_text") or ""
    if not source.strip():
        return PdfEligibilityDecision("excluded", "EMPTY_BODY")
    # Inspect the entire supplied body, with no silent prefix truncation.
    printable = _inspection_text(source)
    lines = tuple(" ".join(line.split()) for line in printable.splitlines() if line.strip())
    if not lines or all(_SEPARATOR.fullmatch(line) for line in lines):
        return PdfEligibilityDecision("excluded", "CONTROL_ONLY_BODY")
    if kind not in _DOCUMENT_TYPES:
        return PdfEligibilityDecision("excluded", "UNCLASSIFIED_DOCUMENT")
    if _footer_only(kind, lines):
        return PdfEligibilityDecision("excluded", "FOOTER_ONLY")
    if (
        # Older RCH interpretations kept this explicit printed report title
        # under MANAGEMENT_DOCUMENT; require the same exact empty template.
        kind in {"SHIFT_END_REPORT", "MANAGEMENT_DOCUMENT"}
        and "REPORT DI FINE TURNO" in {line.upper() for line in lines}
        and all(
            line.upper() in _EMPTY_REPORT_HEADINGS or _SEPARATOR.fullmatch(line)
            for line in lines
        )
    ):
        return PdfEligibilityDecision("excluded", "EMPTY_REPORT_TEMPLATE")
    if _field(document, "complete") is True:
        return PdfEligibilityDecision("complete", "COMPLETE_PRINTABLE_DOCUMENT")
    return PdfEligibilityDecision("partial", "PARTIAL_PRINTABLE_DOCUMENT")


__all__ = [
    "DOCUMENT_PDF_POLICY_VERSION", "PdfEligibilityDecision", "classify_pdf_document",
]
