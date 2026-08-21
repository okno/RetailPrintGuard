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

_OCR_BLOCK = re.compile(r"<OCR:[^>]+>(.*?)</OCR:[^>]+>", re.DOTALL)
_TECHNICAL_TOKEN = re.compile(r"<(?:ESC/POS|BYTE):[^>]*>")
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
        "DEVICE_METADATA_CONFIGURED": (
            "Configurata sul dispositivo (non osservata nel flusso)"
        ),
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
    lines.append(
        "Provenienza intestazione: "
        + receipt_header_evidence_label(_header_field(header, "evidence"))
    )
    return lines


def rch_identity_text_lines(document: Any) -> list[str]:
    """Return truthful, separately sourced RCH timing and identity fields.

    Missing printed values are never substituted with the server capture time.
    A configured serial may be displayed, but its provenance explicitly states
    that it was not observed in the captured wire stream.
    """

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
    source = _OCR_BLOCK.sub(lambda match: match.group(1), source)
    source = _TECHNICAL_TOKEN.sub("", source)
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
    configured_header = (
        receipt_header_text_lines(document)
        if evidence == "DEVICE_METADATA_CONFIGURED"
        else []
    )
    trailing_provenance = []
    if evidence != "DEVICE_METADATA_CONFIGURED":
        trailing_provenance = [
            "",
            "PROVENIENZA INTESTAZIONE",
            receipt_header_evidence_label(evidence),
        ]
    lines = [
        "RETAILPRINTGUARD - VISTA DERIVATA",
        "Il RAW immutabile resta autoritativo.",
        "",
        *configured_header,
        *(('',) if configured_header else ()),
        "TESTO DOCUMENTO",
        body or "Nessun testo documento disponibile.",
        *trailing_provenance,
    ]
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "NOT_OBSERVED_IN_FLOW",
    "RECEIPT_HEADER_NOT_AVAILABLE",
    "document_text_export",
    "rch_identity_text_lines",
    "receipt_header_evidence_label",
    "receipt_header_text_lines",
    "receipt_text",
]
