"""Human-readable receipt projection derived from normalized parser text.

The normalized text remains available as technical evidence.  This module only
removes explicit parser annotations from the presentation copy used by the web
application and PDF renderer; it never reads or changes the immutable RAW.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROME = ZoneInfo("Europe/Rome")
NOT_OBSERVED_IN_FLOW = "Non osservato nel flusso"
RECEIPT_HEADER_NOT_AVAILABLE = "Non osservata nel flusso e non configurata"
_MAX_HEADER_VALUE_CHARACTERS = 240
_MAX_HEADER_ADDRESS_LINES = 8


def _printed_timestamp(value: Any, precision: str | None) -> str:
    if value is None:
        return NOT_OBSERVED_IN_FLOW
    if not isinstance(value, datetime) or value.tzinfo is None:
        return "Timestamp non valido"
    pattern = "%d/%m/%Y %H:%M" if precision == "MINUTE" else "%d/%m/%Y %H:%M:%S"
    return value.astimezone(_ROME).strftime(pattern)


def _timestamp_evidence(value: Any) -> str:
    labels = {
        "RCH_APPLICATION_PRINTED_TEXT": "testo applicativo stampato dalla RCH",
        "RCH_FOOTER_PRINTED_TEXT": "footer stampato dalla RCH",
        "RCH_DGFE_EXPORT": "data/ora fiscale estratta dall'export DGFE",
        "RCH_FISCAL_SUMMARY_EXPORT": (
            "data/ora estratta dall'export della memoria fiscale riepilogo"
        ),
        "ESC_POS_PRINTED_OPERATOR_LINE": "riga operatore stampata dal gestionale POS",
        "DEVICE_METADATA_CONFIGURED": (
            "metadato dispositivo configurato (non osservato nel flusso)"
        ),
    }
    if value is None:
        return NOT_OBSERVED_IN_FLOW
    return labels.get(str(value), f"evidenza dichiarata: {value}")


def _serial_evidence(value: Any) -> str:
    labels = {
        "RCH_PRINTED_RT_PREFIX": "prefisso RT stampato dalla RCH",
        "RCH_PRINTED_BARE_SERIAL_AFTER_FOOTER": (
            "seriale stampato dopo il footer RCH"
        ),
        "RCH_DGFE_EXPORT": "matricola RT estratta dall'export DGFE",
        "RCH_FISCAL_SUMMARY_EXPORT": (
            "matricola RT estratta dall'export della memoria fiscale riepilogo"
        ),
        "DEVICE_METADATA_CONFIGURED": (
            "metadato dispositivo configurato (non osservato nel flusso)"
        ),
    }
    if value is None:
        return NOT_OBSERVED_IN_FLOW
    return labels.get(str(value), f"evidenza dichiarata: {value}")


def _clock_offset(value: Any) -> str:
    if value is None:
        return (
            "Non calcolabile: uno o entrambi gli orari non sono stati "
            "osservati nel flusso"
        )
    try:
        seconds = int(value)
    except (TypeError, ValueError, OverflowError):
        return "Scarto non valido"
    if seconds == 0:
        return "0 s (orologi allineati al minuto/secondo osservato)"
    magnitude = abs(seconds)
    minutes, remainder = divmod(magnitude, 60)
    duration = " ".join(
        part
        for part in (
            f"{minutes} min" if minutes else "",
            f"{remainder} s" if remainder else "",
        )
        if part
    )
    relation = "indietro" if seconds < 0 else "avanti"
    return (
        f"{seconds:+d} s (footer RCH {relation} di {duration} "
        "rispetto all'ora applicativa)"
    )


def _header_field(header: Any, name: str) -> Any:
    if isinstance(header, dict):
        return header.get(name)
    return getattr(header, name, None)


def _header_value(value: Any) -> str | None:
    if value is None:
        return None
    rendered = _CONTROL_CHARACTER.sub("", str(value)).strip()
    if not rendered:
        return None
    return rendered[:_MAX_HEADER_VALUE_CHARACTERS]


def receipt_header_evidence_label(value: Any) -> str:
    labels = {
        "RCH_PRINTED_HEADER": "Osservata nel blocco iniziale stampato dalla RCH",
        "RCH_DGFE_EXPORT": "Estratta dall'export della memoria fiscale DGFE",
        "DEVICE_METADATA_CONFIGURED": "",
    }
    if value is None:
        return RECEIPT_HEADER_NOT_AVAILABLE
    return labels.get(str(value), f"Provenienza non riconosciuta: {_header_value(value)}")


def receipt_header_text_lines(document: Any) -> list[str]:
    """Return a bounded header projection with explicit evidence provenance."""

    header = getattr(document, "receipt_header", None)
    lines = ["INTESTAZIONE DOCUMENTO"]
    if header is None:
        lines.append(f"Stato: {RECEIPT_HEADER_NOT_AVAILABLE}")
        return lines

    for label, name in (
        ("Insegna", "merchant_name"),
        ("Ragione sociale", "legal_name"),
    ):
        value = _header_value(_header_field(header, name))
        if value:
            lines.append(f"{label}: {value}")

    address_lines = _header_field(header, "address_lines")
    if isinstance(address_lines, (list, tuple)):
        for index, address in enumerate(address_lines[:_MAX_HEADER_ADDRESS_LINES]):
            value = _header_value(address)
            if value:
                label = "Indirizzo" if index == 0 else f"Indirizzo {index + 1}"
                lines.append(f"{label}: {value}")

    for label, name in (
        ("Telefono", "phone"),
        ("Codice fiscale", "tax_code"),
        ("Partita IVA", "vat_number"),
    ):
        value = _header_value(_header_field(header, name))
        if value:
            lines.append(f"{label}: {value}")
    provenance = receipt_header_evidence_label(_header_field(header, "evidence"))
    if provenance:
        lines.append("Provenienza intestazione: " + provenance)
    return lines


def rch_identity_text_lines(document: Any) -> list[str]:
    """Return truthful, separately sourced RCH timing and identity fields.

    Missing printed values are never substituted with the server capture time.
    A configured serial may be displayed, but its provenance explicitly states
    that it was not observed in the captured wire stream.
    """

    document_timestamp = getattr(document, "document_timestamp", None)
    application = getattr(document, "application_timestamp", None)
    footer = getattr(document, "rch_footer_timestamp", None)
    serial = getattr(document, "rch_serial_number", None)
    captured = getattr(document, "captured_at", None)
    captured_text = _printed_timestamp(captured, "SECOND")
    if captured is None:
        captured_text = "Non disponibile"
    serial_text = (
        NOT_OBSERVED_IN_FLOW
        if serial is None
        else _CONTROL_CHARACTER.sub("", str(serial)).strip() or NOT_OBSERVED_IN_FLOW
    )
    return [
        "Ora documento fiscale: "
        + _printed_timestamp(
            document_timestamp,
            getattr(document, "document_timestamp_precision", None),
        ),
        "  Provenienza: "
        + (
            _timestamp_evidence(
                getattr(document, "document_timestamp_evidence", None)
            )
            if document_timestamp is not None
            else NOT_OBSERVED_IN_FLOW
        ),
        "Ora applicativa RCH: "
        + _printed_timestamp(
            application, getattr(document, "application_timestamp_precision", None)
        ),
        "  Provenienza: "
        + (
            _timestamp_evidence(
                getattr(document, "application_timestamp_evidence", None)
            )
            if application is not None
            else NOT_OBSERVED_IN_FLOW
        ),
        f"Acquisizione server: {captured_text}",
        "  Provenienza: timestamp registrato dal server; "
        "non e' un orario stampato dalla RCH",
        "Ora footer RCH: "
        + _printed_timestamp(
            footer, getattr(document, "rch_footer_timestamp_precision", None)
        ),
        "  Provenienza: "
        + (
            _timestamp_evidence(
                getattr(document, "rch_footer_timestamp_evidence", None)
            )
            if footer is not None
            else NOT_OBSERVED_IN_FLOW
        ),
        "Scarto orologio (footer - applicativa): "
        + _clock_offset(getattr(document, "rch_clock_offset_seconds", None)),
        f"Seriale RCH: {serial_text}",
        "  Provenienza: "
        + (
            _serial_evidence(getattr(document, "rch_serial_number_evidence", None))
            if serial is not None
            else NOT_OBSERVED_IN_FLOW
        ),
    ]


def _unwrap_ocr_blocks(source: str) -> str:
    """Preserve the legacy non-greedy OCR projection with a forward-only scan.

    Tags need nonempty labels, but labels need not match: both properties retain
    the original regex semantics, including malformed or nested annotations.
    With no closing tag, leave the remaining source intact instead of retrying
    every nested opener against the same suffix.
    """
    fragments: list[str] = []
    cursor = search_from = 0
    while (start := source.find("<OCR:", search_from)) >= 0:
        opening_end = source.find(">", start + 5)
        if opening_end < 0:
            break
        if opening_end == start + 5:
            search_from = opening_end + 1
            continue
        closing_start = source.find("</OCR:", opening_end + 1)
        while closing_start >= 0:
            closing_end = source.find(">", closing_start + 6)
            if closing_end < 0:
                closing_start = -1
                break
            if closing_end > closing_start + 6:
                break
            closing_start = source.find("</OCR:", closing_end + 1)
        if closing_start < 0:
            break
        fragments.append(source[cursor:start])
        fragments.append(source[opening_end + 1:closing_start])
        cursor = search_from = closing_end + 1
    fragments.append(source[cursor:])
    return "".join(fragments)


def _strip_technical_tokens(source: str) -> str:
    """Remove complete legacy ESC/POS/BYTE annotations without suffix rescans.

    Empty labels and nested token-like text retain the regex's first-">"
    semantics. An unterminated annotation remains printable, as before.
    """
    fragments: list[str] = []
    cursor = search_from = 0
    while (start := source.find("<", search_from)) >= 0:
        if not source.startswith(("<ESC/POS:", "<BYTE:"), start):
            search_from = start + 1
            continue
        end = source.find(">", start + 1)
        if end < 0:
            break
        fragments.append(source[cursor:start])
        cursor = search_from = end + 1
    fragments.append(source[cursor:])
    return "".join(fragments)


def receipt_text(normalized_text: str, *, maximum_characters: int = 250_000) -> str:
    """Return a bounded, readable projection while preserving printable text.

    OCR payload text is kept, whereas the surrounding OCR provenance tags and
    ESC/POS control annotations are removed.  At most one consecutive blank
    line is retained so a command-heavy ticket does not become an empty page.
    """

    if maximum_characters < 1:
        raise ValueError("maximum_characters must be positive")
    source = normalized_text[:maximum_characters]
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    source = _unwrap_ocr_blocks(source)
    source = _strip_technical_tokens(source)
    source = _CONTROL_CHARACTER.sub("", source)

    output: list[str] = []
    previous_blank = True
    for value in source.split("\n"):
        line = value.rstrip()
        blank = not line.strip()
        if blank and previous_blank:
            continue
        output.append("" if blank else line)
        previous_blank = blank
    while output and not output[-1]:
        output.pop()
    return "\n".join(output).strip("\n")


def document_text_export(document: Any) -> str:
    """Build the audited TXT view without changing the normalized/RAW evidence."""

    source = getattr(document, "receipt_text", None)
    if source is None:
        source = getattr(document, "normalized_text", "")
    body = receipt_text(str(source or ""))
    header = getattr(document, "receipt_header", None)
    evidence = _header_field(header, "evidence") if header is not None else None
    projected_header = (
        receipt_header_text_lines(document)
        if evidence in {"DEVICE_METADATA_CONFIGURED", "RCH_DGFE_EXPORT"}
        else []
    )
    trailing_provenance = []
    if evidence not in {"DEVICE_METADATA_CONFIGURED", "RCH_DGFE_EXPORT"}:
        trailing_provenance = [
            "",
            "PROVENIENZA INTESTAZIONE",
            receipt_header_evidence_label(evidence),
        ]
    lines = [
        "RETAILPRINTGUARD - VISTA DERIVATA",
        "Il RAW immutabile resta autoritativo.",
        "",
        *projected_header,
        *(('',) if projected_header else ()),
        "TESTO DOCUMENTO",
        body or "Nessun testo documento disponibile.",
        *trailing_provenance,
    ]
    return "\n".join(lines).rstrip() + "\n"


def receipt_preview_text(document: Any) -> str:
    """Return exactly the web Scontrino projection, without normalizing its text.

    This mirrors configuredReceiptHeaderText and the receipt/normalized/fallback
    selection in DocumentDetailPage. Existing TXT evidence formatting deliberately
    remains separate. In particular, whitespace-only values, columns, empty
    address lines and trailing newlines are significant here.
    """
    body = (
        _header_field(document, "receipt_text")
        or _header_field(document, "normalized_text")
        or "Nessun testo documento disponibile."
    )
    header = _header_field(document, "receipt_header")
    evidence = _header_field(header, "evidence")
    if evidence not in {"DEVICE_METADATA_CONFIGURED", "RCH_DGFE_EXPORT"}:
        return body
    lines = (
        ["INTESTAZIONE DOCUMENTO (MEMORIA FISCALE DGFE)"]
        if evidence == "RCH_DGFE_EXPORT" else []
    )
    for name in ("merchant_name", "legal_name"):
        value = _header_field(header, name)
        if value:
            lines.append(value)
    lines.extend(_header_field(header, "address_lines") or [])
    for prefix, name in (("Telefono ", "phone"), ("C.F. ", "tax_code"), ("P.IVA ", "vat_number")):
        value = _header_field(header, name)
        if value:
            lines.append(prefix + value)
    provenance = receipt_header_evidence_label(evidence)
    if provenance:
        lines.extend(("", provenance))
    return "\n".join(lines) + "\n\n" + body


__all__ = [
    "NOT_OBSERVED_IN_FLOW",
    "RECEIPT_HEADER_NOT_AVAILABLE",
    "document_text_export",
    "rch_identity_text_lines",
    "receipt_header_evidence_label",
    "receipt_header_text_lines",
    "receipt_preview_text",
    "receipt_text",
]
