"""Bounded download names from observed identity, never invented fiscal numbers."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

_LOCAL_TIMEZONE = ZoneInfo("Europe/Rome")
_TYPE_LABELS = {
    "ORDER": "comanda",
    "ORDER_CHANGE": "variazione-comanda",
    "KITCHEN_ORDER": "comanda-cucina",
    "PRE_BILL": "preconto",
    "MANAGEMENT_DOCUMENT": "gestionale",
    "COMMERCIAL_DOCUMENT": "documento-commerciale",
    "INVOICE": "fattura",
    "SHIFT_END_REPORT": "chiusura-turno",
    "DEVICE_RESPONSE": "risposta-dispositivo",
    "CANCELLATION": "annullo",
    "CONFORMING_COPY": "copia-conforme",
    "REFUND": "reso",
    "REPRINT": "ristampa",
    "PAYMENT": "pagamento",
    "UNKNOWN": "non-classificato",
}


def _observed(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _component(value: str, *, limit: int = 48) -> str:
    pieces: list[str] = []
    for character in unicodedata.normalize("NFKD", value):
        if unicodedata.combining(character):
            continue
        if character.isascii() and (character.isalnum() or character in "-_"):
            pieces.append(character)
        elif not character.isascii() and character.isalnum():
            # Keep non-Latin identifiers distinguishable in ASCII-only headers.
            pieces.append(f"u{ord(character):04x}")
        else:
            pieces.append("-")
    normalized = re.sub(r"-+", "-", "".join(pieces)).strip("-_")
    no_identifier_characters = not normalized
    if no_identifier_characters:
        normalized = "codice"
    if len(normalized) > limit or no_identifier_characters:
        digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:10]
        normalized = f"{normalized[:limit - 11]}-{digest}"
    return normalized


def _document_reference(document: Any) -> str:
    full = _observed(getattr(document, "external_document_code", None))
    legacy = _observed(getattr(document, "external_code", None))
    order = _observed(getattr(document, "order_code", None))
    suffix = _observed(getattr(document, "external_document_code_suffix", None))
    if full is not None:
        return "numero-" + _component(full)
    if (
        legacy is not None
        and legacy != suffix
        and getattr(document, "progressive_observation_status", None)
        not in {"SUFFIX_ONLY", "SUFFIX_ONLY_OBSERVED_IN_CAPTURE"}
    ):
        return "numero-" + _component(legacy)
    if order is not None:
        return "ordine-" + _component(order)
    if suffix is not None:
        return "suffisso-" + _component(suffix)
    return "senza-numero-fiscale"


def document_pdf_filename(document: Any) -> str:
    """Use document date, or explicitly labeled capture date, in Europe/Rome."""

    timestamp = getattr(document, "document_timestamp", None)
    prefix = ""
    if not isinstance(timestamp, datetime):
        timestamp = getattr(document, "captured_at", None)
        prefix = "acquisizione-"
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        try:
            date_label = prefix + timestamp.astimezone(_LOCAL_TIMEZONE).date().isoformat()
        except (OverflowError, ValueError):
            date_label = "senza-data"
    else:
        date_label = "senza-data"
    kind = _TYPE_LABELS.get(str(getattr(document, "type", "")), "documento")
    device = _component(str(getattr(document, "device_id", "dispositivo")), limit=32)
    return f"{date_label}_{kind}_{_document_reference(document)}_{device}.pdf"


def archive_pdf_filename(document: Any, sequence: int) -> str:
    """The persisted export sequence makes duplicate observed numbers unambiguous."""

    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 1 <= sequence <= 999999:
        raise ValueError("archive sequence must be an integer between 1 and 999999")
    return f"{sequence:06d}_{document_pdf_filename(document)}"


__all__ = ["archive_pdf_filename", "document_pdf_filename"]
