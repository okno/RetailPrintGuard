"""Bounded ESC/POS text extraction without touching authoritative RAW bytes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from retailprintguard.common.domain import (
    DocumentLine,
    DocumentType,
    EvidenceLevel,
    NormalizedDocument,
    SourceSpan,
)

PARSER_NAME = "retailprintguard-escpos"
PARSER_VERSION = "1.0.0"
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_OUTPUT_CHARS = 4_000_000
_MAX_DOCUMENTS = 1_024
_MAX_LINES = 32_768
_CODEPAGES = {0: "cp437", 2: "cp850", 16: "cp1252", 19: "cp858"}
_ALLOWED_ENCODINGS = frozenset({"cp437", "cp850", "cp858", "cp1252", "latin-1", "utf-8"})
_MONEY_RE = re.compile(r"(?<!\d)(?P<value>[+-]?\d{1,9}(?:\.\d{3})*,\d{2})(?!\d)")
_TABLE_RE = re.compile(r"\bTAVOLO\s*[:#-]?\s*(?P<value>[A-Z0-9._/-]+)", re.IGNORECASE)
_ORDER_RE = re.compile(r"\b(?:ORDINE|COMANDA)\s*[:#-]?\s*(?P<value>[A-Z0-9._/-]+)", re.IGNORECASE)
_OPERATOR_RE = re.compile(r"\bOPERATORE\s*[:#-]?\s*(?P<value>[A-Z0-9._/-]+)", re.IGNORECASE)
_DOCUMENT_RE = re.compile(
    r"\b(?:DOCUMENTO|DOC\.?|PRECONTO|COMANDA)\s*(?:N\.?|#)?\s*[:#-]?\s*(?P<value>[A-Z0-9._/-]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _TextLine:
    text: str
    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class _Segment:
    payload: bytes
    base_offset: int
    cut_observed: bool


def _money(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def _segments(payload: bytes) -> tuple[_Segment, ...]:
    result: list[_Segment] = []
    start = 0
    cursor = 0
    while cursor + 2 < len(payload) and len(result) < _MAX_DOCUMENTS - 1:
        marker = payload.find(b"\x1d\x56", cursor)
        if marker < 0 or marker + 2 >= len(payload):
            break
        mode = payload[marker + 2]
        if mode in {0, 1, 48, 49}:
            end = marker + 3
        elif mode in {65, 66} and marker + 3 < len(payload):
            end = marker + 4
        else:
            cursor = marker + 2
            continue
        result.append(_Segment(payload[start:end], start, True))
        start = end
        cursor = end
    if start < len(payload) or not result:
        result.append(_Segment(payload[start:], start, False))
    return tuple(result[:_MAX_DOCUMENTS])


def _extract_lines(
    payload: bytes,
    *,
    base_offset: int,
    default_encoding: str,
) -> tuple[tuple[_TextLine, ...], str, tuple[str, ...]]:
    if default_encoding not in _ALLOWED_ENCODINGS:
        raise ValueError(f"unsupported configured ESC/POS encoding: {default_encoding}")
    encoding = default_encoding
    buffer = bytearray()
    buffer_offset = base_offset
    lines: list[_TextLine] = []
    rendered: list[str] = []
    warnings: list[str] = []
    cursor = 0
    chars = 0

    def emit_text() -> None:
        nonlocal chars
        if not buffer:
            return
        try:
            text = bytes(buffer).decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError) as exc:
            text = "".join(
                chr(value) if 32 <= value < 127 else f"<0x{value:02X}>" for value in buffer
            )
            warnings.append(f"text_decode_error:{type(exc).__name__}:{encoding}")
        remaining = _MAX_OUTPUT_CHARS - chars
        if remaining <= 0:
            warnings.append("normalized_text_limit_exceeded")
            buffer.clear()
            return
        text = text[:remaining]
        rendered.append(text)
        chars += len(text)
        if text and len(lines) < _MAX_LINES:
            lines.append(_TextLine(text, buffer_offset, len(buffer)))
        elif text:
            warnings.append("line_limit_exceeded")
        buffer.clear()

    def control(label: str, length: int) -> None:
        nonlocal chars
        emit_text()
        if chars < _MAX_OUTPUT_CHARS:
            rendered.append(label)
            chars += len(label)
        else:
            warnings.append("normalized_text_limit_exceeded")
        del length

    while cursor < len(payload) and chars < _MAX_OUTPUT_CHARS:
        byte = payload[cursor]
        absolute = base_offset + cursor
        if byte == 0x0A:
            emit_text()
            rendered.append("\n")
            chars += 1
            cursor += 1
        elif byte == 0x0D:
            emit_text()
            cursor += 1
        elif byte == 0x09:
            if not buffer:
                buffer_offset = absolute
            buffer.extend(b"    ")
            cursor += 1
        elif byte == 0x1B and cursor + 1 < len(payload):
            command = payload[cursor + 1]
            if command == 0x40:
                control("<ESC/POS:INIT>", 2)
                cursor += 2
            elif command == 0x74 and cursor + 2 < len(payload):
                emit_text()
                code = payload[cursor + 2]
                encoding = _CODEPAGES.get(code, encoding)
                control(f"<ESC/POS:CODEPAGE:{code}:{encoding}>", 3)
                cursor += 3
            elif command in {0x21, 0x45, 0x61, 0x64, 0x4A, 0x33, 0x20} and cursor + 2 < len(
                payload
            ):
                control(f"<ESC/POS:ESC:{command:02X}:{payload[cursor + 2]}>", 3)
                cursor += 3
            elif command == 0x70 and cursor + 4 < len(payload):
                control("<ESC/POS:CASH_DRAWER>", 5)
                cursor += 5
            elif command == 0x2A and cursor + 4 < len(payload):
                width = payload[cursor + 3] + (payload[cursor + 4] << 8)
                multiplier = 3 if payload[cursor + 2] in {32, 33} else 1
                available = min(width * multiplier, max(0, len(payload) - cursor - 5))
                control(f"<ESC/POS:BIT_IMAGE:{available}>", 5 + available)
                cursor += 5 + available
            else:
                control(f"<ESC/POS:ESC:{command:02X}>", 2)
                cursor += 2
        elif byte == 0x1D and cursor + 1 < len(payload):
            command = payload[cursor + 1]
            if command == 0x56 and cursor + 2 < len(payload):
                mode = payload[cursor + 2]
                size = 4 if mode in {65, 66} and cursor + 3 < len(payload) else 3
                control("<ESC/POS:CUT>", size)
                cursor += size
            elif command == 0x76 and cursor + 7 < len(payload) and payload[cursor + 2] == 0x30:
                width = payload[cursor + 4] + (payload[cursor + 5] << 8)
                height = payload[cursor + 6] + (payload[cursor + 7] << 8)
                available = min(width * height, max(0, len(payload) - cursor - 8))
                control(f"<ESC/POS:RASTER_IMAGE:{available}>", 8 + available)
                cursor += 8 + available
            elif command == 0x6B and cursor + 2 < len(payload):
                mode = payload[cursor + 2]
                if mode >= 65 and cursor + 3 < len(payload):
                    available = min(payload[cursor + 3], max(0, len(payload) - cursor - 4))
                    size = 4 + available
                else:
                    terminator = payload.find(b"\x00", cursor + 3)
                    size = len(payload) - cursor if terminator < 0 else terminator + 1 - cursor
                    available = max(0, size - 3)
                control(f"<ESC/POS:BARCODE:{available}>", size)
                cursor += size
            elif command == 0x28 and cursor + 4 < len(payload):
                declared = payload[cursor + 3] + (payload[cursor + 4] << 8)
                available = min(declared, max(0, len(payload) - cursor - 5))
                control(f"<ESC/POS:GS_PAREN:{available}>", 5 + available)
                cursor += 5 + available
            elif command == 0x21 and cursor + 2 < len(payload):
                control(f"<ESC/POS:CHAR_SIZE:{payload[cursor + 2]}>", 3)
                cursor += 3
            else:
                control(f"<ESC/POS:GS:{command:02X}>", 2)
                cursor += 2
        elif byte == 0x10 and cursor + 2 < len(payload):
            control(f"<ESC/POS:REALTIME:{payload[cursor + 1]:02X}:{payload[cursor + 2]}>", 3)
            cursor += 3
        elif byte < 0x20 or byte == 0x7F:
            control(f"<BYTE:0x{byte:02X}>", 1)
            cursor += 1
        else:
            if not buffer:
                buffer_offset = absolute
            buffer.append(byte)
            cursor += 1
    emit_text()
    if cursor < len(payload):
        warnings.append("normalized_text_limit_exceeded")
    return tuple(lines), "".join(rendered).strip(), tuple(dict.fromkeys(warnings))


def _classify(text: str) -> tuple[DocumentType, str]:
    upper = text.upper()
    if "COPIA CONFORME" in upper:
        return DocumentType.CONFORMING_COPY, "COPIA_CONFORME_LITERAL"
    if "RISTAMPA" in upper:
        return DocumentType.REPRINT, "RISTAMPA_LITERAL"
    if "ANNULL" in upper or "STORNO" in upper:
        return DocumentType.CANCELLATION, "ANNULLAMENTO_LITERAL"
    if "PRECONTO" in upper or "PRE-CONTO" in upper:
        return DocumentType.PRE_BILL, "PRECONTO_LITERAL"
    if "COMANDA" in upper or "CUCINA" in upper:
        return DocumentType.KITCHEN_ORDER, "COMANDA_LITERAL"
    if "ORDINE" in upper:
        return DocumentType.ORDER, "ORDINE_LITERAL"
    if "DOCUMENTO GESTIONALE" in upper:
        return DocumentType.MANAGEMENT_DOCUMENT, "GESTIONALE_LITERAL"
    return DocumentType.UNKNOWN, "NESSUN_MARCATORE_CONFERMATO"


def _field(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group("value") if match else None


def _semantic_lines(lines: tuple[_TextLine, ...], base_offset: int) -> tuple[DocumentLine, ...]:
    result: list[DocumentLine] = []
    for line in lines:
        text = " ".join(line.text.split())
        if not text:
            continue
        amounts = list(_MONEY_RE.finditer(text))
        amount = _money(amounts[-1].group("value")) if amounts else None
        description = text[: amounts[-1].start()].strip(" -:;") if amounts else text
        if not description or description.upper().startswith(("TOTALE", "TOT ", "IMPORTO")):
            continue
        result.append(
            DocumentLine(
                sequence=len(result) + 1,
                description=description[:512],
                quantity=Decimal("1"),
                unit_price=amount,
                line_total=amount,
                raw_text=text,
                source=SourceSpan(
                    direction="CLIENT_TO_DEVICE",
                    offset=max(base_offset, line.offset),
                    length=line.length,
                ),
            )
        )
        if len(result) >= _MAX_LINES:
            break
    return tuple(result)


def parse_escpos(
    payload: bytes,
    *,
    device_id: str,
    session_id: str | None,
    job_id: str,
    captured_at: datetime,
    manifest_sha256: str,
    source_path: str | Path,
    encoding: str = "cp858",
) -> tuple[NormalizedDocument, ...]:
    """Return versionable documents; byte framing never depends on TCP recv calls."""

    source_hash = hashlib.sha256(payload, usedforsecurity=True).hexdigest()
    if len(payload) > _MAX_INPUT_BYTES:
        payload = payload[:_MAX_INPUT_BYTES]
        global_warning = ("parser_input_limit_exceeded",)
    else:
        global_warning = ()
    documents: list[NormalizedDocument] = []
    for segment in _segments(payload):
        lines, text, warnings = _extract_lines(
            segment.payload,
            base_offset=segment.base_offset,
            default_encoding=encoding,
        )
        if not segment.payload:
            continue
        doc_type, subtype = _classify(text)
        money_values = [_money(match.group("value")) for match in _MONEY_RE.finditer(text)]
        money_values = [value for value in money_values if value is not None]
        gross_total = money_values[-1] if money_values else None
        # The source identity deliberately excludes the parser version.  A new
        # parser release must append a DocumentVersion to the same document,
        # never manufacture a second business document from unchanged bytes.
        document_id = uuid5(
            NAMESPACE_URL,
            f"retailprintguard:escpos:{job_id}:{segment.base_offset}:{source_hash}",
        )
        documents.append(
            NormalizedDocument(
                id=document_id,
                source_device_id=device_id,
                source_session_id=session_id,
                source_job_id=job_id,
                type=doc_type,
                subtype=subtype,
                external_document_code=_field(_DOCUMENT_RE, text),
                order_code=_field(_ORDER_RE, text),
                table_code=_field(_TABLE_RE, text),
                operator_code=_field(_OPERATOR_RE, text),
                captured_at=captured_at,
                gross_total=gross_total,
                net_total=gross_total,
                status="COMPLETE" if segment.cut_observed else "PARTIAL",
                normalized_text=text,
                encoding=encoding,
                parser_name=PARSER_NAME,
                parser_version=PARSER_VERSION,
                parse_confidence=85 if doc_type is not DocumentType.UNKNOWN else 25,
                evidence=(
                    EvidenceLevel.CONFIRMED
                    if doc_type is not DocumentType.UNKNOWN
                    else EvidenceLevel.UNKNOWN
                ),
                source_manifest_sha256=manifest_sha256,
                source_payload_sha256=source_hash,
                source_path=str(source_path),
                complete=segment.cut_observed,
                warnings=tuple(dict.fromkeys((*global_warning, *warnings))),
                lines=_semantic_lines(lines, segment.base_offset),
                raw_metadata={
                    "source_start_offset": segment.base_offset,
                    "source_end_offset": segment.base_offset + len(segment.payload),
                    "cut_observed": segment.cut_observed,
                    "classification_evidence": "LITERAL_MARKER"
                    if doc_type is not DocumentType.UNKNOWN
                    else "UNKNOWN",
                },
            )
        )
    return tuple(documents)


__all__ = ["PARSER_NAME", "PARSER_VERSION", "parse_escpos"]
