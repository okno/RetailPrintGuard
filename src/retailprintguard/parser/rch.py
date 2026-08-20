"""Capture-observed RCH framing with conservative, inferred document semantics."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from retailprintguard.common.domain import (
    DocumentLine,
    DocumentType,
    EvidenceLevel,
    NormalizedDocument,
    PaymentRecord,
    SourceSpan,
)

PARSER_NAME = "retailprintguard-rch-observed"
PARSER_VERSION = "1.3.0"
_STX = 0x02
_ETX = 0x03
_ACK = 0x06
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_FRAMES = 8_192
_MAX_ISSUES = 1_024
_MAX_DOCUMENTS = 256
_MAX_LINES = 32_768
_ITEM_RE = re.compile(
    r"^=R(?P<code>[^/]+)/\$(?P<amount>[+-]?\d+)(?:/\*(?P<quantity>[+-]?\d+))?/\((?P<description>.*)\)$"
)
_TOTAL_RE = re.compile(r"^=T(?P<code>[^/]+)/\$(?P<amount>[+-]?\d+)$")
_COMMERCIAL_TEXT_RE = re.compile(r'^="/\?A/\((?P<text>.*)\)(?:/\*(?P<style>\d+))?$')
_MANAGEMENT_TEXT_RE = re.compile(r'^="/\((?P<text>.*)\)(?:/\*(?P<style>\d+))?$')
_COUNTER_RE = re.compile(r"^s(?P<status_digits>\d{6})RE(?P<counter>\d{4})$")
_ORDER_RE = re.compile(r"^\s*(?:ORDINE|ORDER)\s*:\s*(?P<value>.*?)\s*$", re.IGNORECASE)
_TABLE_RE = re.compile(r"^\s*TAVOLO\s*:\s*(?P<value>.*?)\s*$", re.IGNORECASE)
_DOCUMENT_CODE_RE = re.compile(
    r"\b(?:DOC(?:UMENTO)?\.?\s*(?:GESTIONALE)?\s*N\.?|N\.?)\s*"
    r"(?P<value>[A-Z0-9]+(?:[-/][A-Z0-9]+)+)\b",
    re.IGNORECASE,
)
_MANAGEMENT_DOCUMENT_CODE_RE = re.compile(
    r"\bDOC(?:UMENTO)?\.?\s*GESTIONALE\s*N\.?\s*"
    r"(?P<value>[A-Z0-9]+(?:[-/][A-Z0-9]+)+)\b",
    re.IGNORECASE,
)
_COMMERCIAL_REFERENCE_CODE_RE = re.compile(
    r"\b(?:RIF(?:ERIMENTO)?\.?\s*(?:AL\s+)?(?:DOCUMENTO\s+COMMERCIALE|DOCUMENTO)"
    r"|DOCUMENTO\s+COMMERCIALE(?:\s+DI\s+RIFERIMENTO)?"
    r"|COPIA\s+CONFORME(?:\s+DEL)?\s+DOCUMENTO)\s*N\.?\s*"
    r"(?P<value>[A-Z0-9]+(?:[-/][A-Z0-9]+)+)\b",
    re.IGNORECASE,
)
_PRINTED_MONEY_RE = re.compile(r"(?<!\d)(?P<value>[+-]?\d{1,9}(?:\.\d{3})*,\d{2})(?!\d)")
_PRINTED_TOTAL_RE = re.compile(
    r"^\s*(?:TOT|TOTALE(?:\s+COMPLESSIVO)?)\b(?P<tail>.*)$",
    re.IGNORECASE,
)
_PAYMENT_RE = re.compile(
    r"^\s*(?P<method>CONTANTI|CARTA|BANCOMAT|ASSEGNO|PAGAMENTO)\b(?P<tail>.*)$",
    re.IGNORECASE,
)
_ERROR_RESPONSE_RE = re.compile(r"^ES(?P<code>\d{8})$")
_SUCCESS_RESPONSE_RE = re.compile(r"^(?:ON\d{8}|s\d{6}RE\d{4}|\d{4})$")
_PRINTED_TIMESTAMP_RE = re.compile(
    r"(?<!\d)(?P<day>\d{2})[/\\.\-](?P<month>\d{2})[/\\.\-]"
    r"(?P<year>\d{2}|\d{4})\s+(?P<hour>\d{2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2}))?(?!\d)"
)


@dataclass(frozen=True, slots=True)
class Frame:
    frame_id: int
    offset: int
    raw: bytes
    address: str
    frame_class: str
    data: bytes
    sequence: str
    bcc_valid: bool

    @property
    def text(self) -> str:
        return self.data.decode("latin-1")


@dataclass(frozen=True, slots=True)
class FramedStream:
    frames: tuple[Frame, ...]
    ack_count: int
    issues: tuple[str, ...]
    truncated: bool


@dataclass(slots=True)
class _Draft:
    kind: str
    ordinal: int
    start_offset: int
    lines: list[DocumentLine] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    item_code: str | None = None
    order_code: str | None = None
    table_code: str | None = None
    external_document_code: str | None = None
    external_document_code_suffix: str | None = None
    commercial_reference_code: str | None = None
    external_document_code_evidence: str | None = None
    external_document_code_suffix_evidence: str | None = None
    commercial_reference_code_evidence: str | None = None
    response_status_digits: str | None = None
    total: Decimal | None = None
    tax_total: Decimal | None = None
    payments: list[PaymentRecord] = field(default_factory=list)
    total_code: str | None = None
    frame_ids: list[int] = field(default_factory=list)
    end_offset: int = 0
    complete: bool = False
    warnings: list[str] = field(default_factory=list)


def _bcc(prefix: bytes) -> int:
    result = 0
    for value in prefix:
        result ^= value
    return result


def frame_stream(payload: bytes) -> FramedStream:
    """Frame arbitrary TCP segmentation after reassembly; retain bounded diagnostics."""

    frames: list[Frame] = []
    issues: list[str] = []
    ack_count = 0
    cursor = 0
    truncated = len(payload) > _MAX_INPUT_BYTES
    view = payload[:_MAX_INPUT_BYTES]
    while cursor < len(view):
        byte = view[cursor]
        if byte == _ACK:
            ack_count += 1
            cursor += 1
            continue
        if byte != _STX:
            next_control = min(
                (
                    position
                    for position in (view.find(b"\x02", cursor + 1), view.find(b"\x06", cursor + 1))
                    if position >= 0
                ),
                default=len(view),
            )
            if len(issues) < _MAX_ISSUES:
                issues.append(f"unframed_bytes:{cursor}:{next_control - cursor}")
            cursor = next_control
            continue
        if cursor + 6 > len(view):
            if len(issues) < _MAX_ISSUES:
                issues.append(f"truncated_header:{cursor}:{len(view) - cursor}")
            break
        address_raw = view[cursor + 1 : cursor + 3]
        length_raw = view[cursor + 3 : cursor + 6]
        if not address_raw.isdigit() or not length_raw.isdigit():
            if len(issues) < _MAX_ISSUES:
                issues.append(f"malformed_header:{cursor}")
            cursor += 1
            continue
        data_length = int(length_raw)
        total = data_length + 11
        if cursor + total > len(view):
            if len(issues) < _MAX_ISSUES:
                issues.append(f"truncated_frame:{cursor}:{len(view) - cursor}")
            break
        raw = view[cursor : cursor + total]
        if raw[-1] != _ETX:
            if len(issues) < _MAX_ISSUES:
                issues.append(f"missing_etx:{cursor}")
            cursor += 1
            continue
        try:
            expected = int(raw[-3:-1].decode("ascii"), 16)
            address = address_raw.decode("ascii")
            frame_class = bytes((raw[6],)).decode("latin-1")
            sequence = bytes((raw[-4],)).decode("latin-1")
        except (UnicodeDecodeError, ValueError):
            if len(issues) < _MAX_ISSUES:
                issues.append(f"malformed_trailer:{cursor}")
            cursor += total
            continue
        valid = _bcc(raw[:-3]) == expected
        if not valid and len(issues) < _MAX_ISSUES:
            issues.append(f"bcc_mismatch:{cursor}")
        if len(frames) < _MAX_FRAMES:
            frames.append(
                Frame(
                    frame_id=len(frames) + 1,
                    offset=cursor,
                    raw=raw,
                    address=address,
                    frame_class=frame_class,
                    data=raw[7 : 7 + data_length],
                    sequence=sequence,
                    bcc_valid=valid,
                )
            )
        else:
            truncated = True
        cursor += total
    if len(issues) >= _MAX_ISSUES:
        issues[-1] = "issue_limit_exceeded"
    if len(payload) > len(view):
        issues.append("parser_input_limit_exceeded")
    return FramedStream(tuple(frames), ack_count, tuple(issues), truncated)


def _cents(value: str) -> Decimal:
    return (Decimal(value) / Decimal("100")).quantize(Decimal("0.01"))


def _printed_money(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def _document_code(value: str) -> str:
    """Return the printer spelling in a stable separator/case form."""

    return value.strip().upper().replace("/", "-")


def _printed_timestamp(
    text: str,
    *,
    captured_at: datetime,
    timezone_name: str,
) -> tuple[datetime | None, str | None, tuple[str, ...]]:
    """Return a timestamp only when date and time are visible in captured text."""

    match = _PRINTED_TIMESTAMP_RE.search(text)
    if match is None:
        return None, None, ()
    warnings: list[str] = []
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return None, None, ("document_timezone_unknown",)
    try:
        year = int(match.group("year"))
        if year < 100:
            captured_year = captured_at.astimezone(zone).year
            century = (captured_year // 100) * 100
            year = min(
                (century - 100 + year, century + year, century + 100 + year),
                key=lambda candidate: abs(candidate - captured_year),
            )
        naive = datetime(
            year,
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second") or 0),
        )
    except ValueError:
        return None, None, ("document_timestamp_invalid",)
    candidates: list[datetime] = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        roundtrip = aware.astimezone(UTC).astimezone(zone)
        if roundtrip.replace(tzinfo=None) == naive:
            candidates.append(aware)
    if not candidates:
        warnings.append("document_local_time_nonexistent")
        return None, None, tuple(warnings)
    timestamp = min(
        candidates,
        key=lambda item: abs((item - captured_at.astimezone(zone)).total_seconds()),
    )
    precision = "SECOND" if match.group("second") is not None else "MINUTE"
    return timestamp, precision, tuple(warnings)


def _source(frame: Frame) -> SourceSpan:
    return SourceSpan(
        direction="CLIENT_TO_DEVICE",
        offset=frame.offset,
        length=len(frame.raw),
        frame_id=str(frame.frame_id),
    )


def _append_management_line(draft: _Draft, frame: Frame, text: str) -> None:
    if len(draft.lines) >= _MAX_LINES:
        if "line_limit_exceeded" not in draft.warnings:
            draft.warnings.append("line_limit_exceeded")
        return
    amounts = list(_PRINTED_MONEY_RE.finditer(text))
    value = _printed_money(amounts[-1].group("value")) if amounts else None
    description = text[: amounts[-1].start()].strip(" -:;") if amounts else text.strip()
    draft.lines.append(
        DocumentLine(
            sequence=len(draft.lines) + 1,
            description=description or None,
            quantity=Decimal("1") if description else None,
            unit_price=value,
            line_total=value,
            raw_text=text,
            source=_source(frame),
        )
    )

    if match := _ORDER_RE.fullmatch(text):
        draft.order_code = match.group("value") or None
    if match := _TABLE_RE.fullmatch(text):
        draft.table_code = match.group("value") or None
    if match := _COMMERCIAL_REFERENCE_CODE_RE.search(text):
        draft.commercial_reference_code = _document_code(match.group("value"))
        draft.commercial_reference_code_evidence = "RCH_PRINTED_COMMERCIAL_REFERENCE"
    elif match := _MANAGEMENT_DOCUMENT_CODE_RE.search(text):
        value = _document_code(match.group("value"))
        if (
            draft.external_document_code is not None
            and draft.external_document_code != value
            and draft.commercial_reference_code is None
        ):
            # A conforming management print contains the commercial number
            # before its own ``DOC. GESTIONALE N.`` footer.  Preserve both;
            # never overwrite the referenced commercial identity.
            draft.commercial_reference_code = draft.external_document_code
            draft.commercial_reference_code_evidence = (
                "RCH_PRINTED_UNQUALIFIED_DOCUMENT_BEFORE_MANAGEMENT_FOOTER"
            )
        draft.external_document_code = value
        draft.external_document_code_evidence = "RCH_PRINTED_MANAGEMENT_FOOTER"
    elif match := _DOCUMENT_CODE_RE.search(text):
        # In observed management-copy request frames an unqualified ``N.`` is
        # the referenced commercial document.  The management print's own
        # ``DOC. GESTIONALE N.`` footer is generated inside the printer and is
        # absent from the captured request/response, so it must not be
        # invented or assigned from this value.
        value = _document_code(match.group("value"))
        draft.commercial_reference_code = value
        draft.commercial_reference_code_evidence = (
            "RCH_REQUEST_UNQUALIFIED_COMMERCIAL_DOCUMENT_NUMBER"
        )

    if match := _PRINTED_TOTAL_RE.match(text):
        total_values = [
            _printed_money(amount.group("value"))
            for amount in _PRINTED_MONEY_RE.finditer(match.group("tail"))
        ]
        total_values = [amount for amount in total_values if amount is not None]
        if total_values:
            # The first amount following the printed TOTAL label is the
            # document total.  A later amount can be the VAT component.
            if draft.total is None:
                draft.total = total_values[0]
            if len(total_values) > 1:
                draft.tax_total = total_values[-1]

    if match := _PAYMENT_RE.match(text):
        values = [
            _printed_money(amount.group("value"))
            for amount in _PRINTED_MONEY_RE.finditer(match.group("tail"))
        ]
        if values and values[-1] is not None:
            draft.payments.append(
                PaymentRecord(
                    method=match.group("method").upper(),
                    amount=values[-1],
                    evidence=EvidenceLevel.INFERRED,
                )
            )


def _append_commercial_item(draft: _Draft, frame: Frame, match: re.Match[str]) -> None:
    if len(draft.lines) >= _MAX_LINES:
        if "line_limit_exceeded" not in draft.warnings:
            draft.warnings.append("line_limit_exceeded")
        return
    quantity = Decimal(match.group("quantity") or "1")
    # Captures and printed output confirm that ``/$amount/*quantity`` carries
    # a unit amount followed by quantity.  The previous implementation
    # treated amount as a line total and divided it again, under-reporting
    # multi-quantity lines.
    unit = _cents(match.group("amount"))
    total = (unit * quantity).quantize(Decimal("0.01"))
    draft.lines.append(
        DocumentLine(
            sequence=len(draft.lines) + 1,
            item_code=match.group("code"),
            description=match.group("description"),
            quantity=quantity,
            unit_price=unit,
            line_total=total,
            raw_text=frame.text,
            source=_source(frame),
        )
    )


def _draft_document(
    draft: _Draft,
    *,
    device_id: str,
    session_id: str | None,
    job_id: str,
    captured_at: datetime,
    manifest_sha256: str,
    source_hash: str,
    source_path: str,
    response_counter: str | None,
    timezone_name: str,
) -> NormalizedDocument:
    text = "\n".join(draft.texts).strip()
    document_timestamp, timestamp_precision, timestamp_warnings = _printed_timestamp(
        text,
        captured_at=captured_at,
        timezone_name=timezone_name,
    )
    if draft.kind == "commercial":
        document_type = DocumentType.COMMERCIAL_DOCUMENT
        subtype = "RCH_COMMERCIALE_INFERRED"
        confidence = 72
        payments = (
            (
                PaymentRecord(
                    method=None,
                    amount=draft.total,
                    evidence=EvidenceLevel.INFERRED,
                ),
            )
            if draft.total is not None
            else ()
        )
    else:
        upper = text.upper()
        if "COPIA CONFORME" in upper:
            document_type = DocumentType.CONFORMING_COPY
            subtype = "COPIA_CONFORME_LITERAL"
            confidence = 90
        elif "PRECONTO" in upper:
            document_type = DocumentType.PRE_BILL
            subtype = "PRECONTO_LITERAL"
            confidence = 90
        elif "COMANDA" in upper:
            document_type = DocumentType.KITCHEN_ORDER
            subtype = "COMANDA_LITERAL"
            confidence = 90
        elif "CORRISPETTIVO NON RISCOSSO" in upper:
            document_type = DocumentType.MANAGEMENT_DOCUMENT
            subtype = "CORRISPETTIVO_NON_RISCOSSO_LITERAL"
            confidence = 90
        else:
            document_type = DocumentType.MANAGEMENT_DOCUMENT
            subtype = "RCH_GESTIONALE_INFERRED"
            confidence = 70
        if draft.total is None:
            values = [
                _printed_money(match.group("value"))
                for match in _PRINTED_MONEY_RE.finditer(text)
            ]
            draft.total = next((value for value in reversed(values) if value is not None), None)
        payments = tuple(draft.payments)
    identifier = uuid5(
        NAMESPACE_URL,
        f"retailprintguard:rch:{job_id}:{draft.kind}:{draft.start_offset}:{source_hash}",
    )
    warnings = tuple(dict.fromkeys((*draft.warnings, *timestamp_warnings)))
    return NormalizedDocument(
        id=identifier,
        source_device_id=device_id,
        source_session_id=session_id,
        source_job_id=job_id,
        type=document_type,
        subtype=subtype,
        external_document_code=draft.external_document_code,
        external_document_code_suffix=draft.external_document_code_suffix,
        commercial_reference_code=draft.commercial_reference_code,
        order_code=draft.order_code,
        table_code=draft.table_code,
        document_timestamp=document_timestamp,
        captured_at=captured_at,
        gross_total=draft.total,
        net_total=draft.total,
        tax_total=draft.tax_total,
        status="COMPLETE" if draft.complete else "PARTIAL",
        normalized_text=text,
        encoding="latin-1-lossless-byte-view",
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        parse_confidence=confidence if draft.complete else max(20, confidence - 20),
        evidence=EvidenceLevel.INFERRED,
        source_manifest_sha256=manifest_sha256,
        source_payload_sha256=source_hash,
        source_path=source_path,
        complete=draft.complete,
        warnings=warnings,
        lines=tuple(draft.lines),
        payments=payments,
        raw_metadata={
            "source_start_offset": draft.start_offset,
            "source_end_offset": draft.end_offset,
            "source_frame_ids": draft.frame_ids,
            "total_command_code": draft.total_code,
            "response_counter_suffix": response_counter,
            "response_status_digits": draft.response_status_digits,
            "external_document_code_evidence": draft.external_document_code_evidence,
            "external_document_code_suffix_evidence": (
                draft.external_document_code_suffix_evidence
            ),
            "commercial_reference_code_evidence": (
                draft.commercial_reference_code_evidence
            ),
            "document_timestamp_evidence": (
                "RCH_PRINTED_TEXT" if document_timestamp is not None else None
            ),
            "document_timestamp_precision": timestamp_precision,
            "progressive_observation_status": (
                "FULL_CODE_OBSERVED_IN_CAPTURE"
                if draft.external_document_code
                else "SUFFIX_ONLY_OBSERVED_IN_CAPTURE"
                if draft.external_document_code_suffix
                else "NOT_OBSERVED_IN_CAPTURE"
            ),
            "semantic_evidence": "INFERRED_FROM_CAPTURE",
            "economic_close": (
                draft.kind == "management"
                and "CORRISPETTIVO NON RISCOSSO" in text.upper()
            ),
            "settlement_kind": (
                "ROOM_CHARGE"
                if draft.kind == "management"
                and "CORRISPETTIVO NON RISCOSSO" in text.upper()
                and "CONTO: CAMERA" in text.upper()
                else None
            ),
        },
    )


def _cancellation_command_document(
    frame: Frame,
    *,
    device_id: str,
    session_id: str | None,
    job_id: str,
    captured_at: datetime,
    manifest_sha256: str,
    source_hash: str,
    source_path: str,
) -> NormalizedDocument:
    return NormalizedDocument(
        id=uuid5(
            NAMESPACE_URL,
            f"retailprintguard:rch:{job_id}:cancel-command:{frame.offset}:{source_hash}",
        ),
        source_device_id=device_id,
        source_session_id=session_id,
        source_job_id=job_id,
        type=DocumentType.CANCELLATION,
        subtype="RCH_DOCUMENT_CANCEL_COMMAND_OBSERVED",
        captured_at=captured_at,
        status="COMPLETE",
        normalized_text="RCH cancellation command observed",
        encoding="latin-1-lossless-byte-view",
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        parse_confidence=80,
        evidence=EvidenceLevel.INFERRED,
        source_manifest_sha256=manifest_sha256,
        source_payload_sha256=source_hash,
        source_path=source_path,
        complete=True,
        raw_metadata={
            "source_start_offset": frame.offset,
            "source_end_offset": frame.offset + len(frame.raw),
            "source_frame_ids": [frame.frame_id],
            "observed_command": "=k",
            "semantic_evidence": "INFERRED_FROM_CAPTURE_AND_PRINTED_OUTPUT",
        },
    )


def _response_document(
    response: FramedStream,
    *,
    response_raw: bytes,
    device_id: str,
    session_id: str | None,
    job_id: str,
    captured_at: datetime,
    manifest_sha256: str,
    source_path: str,
) -> NormalizedDocument | None:
    if not response_raw:
        return None
    valid = [
        frame
        for frame in response.frames
        if frame.address == "01" and frame.frame_class == "N" and frame.bcc_valid
    ]
    text = "\n".join(frame.text for frame in valid)
    errors = [
        match.group("code")
        for frame in valid
        if (match := _ERROR_RESPONSE_RE.fullmatch(frame.text)) is not None
    ]
    unknown = [
        frame.text
        for frame in valid
        if _ERROR_RESPONSE_RE.fullmatch(frame.text) is None
        and _SUCCESS_RESPONSE_RE.fullmatch(frame.text) is None
    ]
    semantic_warnings = tuple(
        [*(f"device_error_status:{code}" for code in errors)]
        + (["unclassified_response"] if unknown else [])
    )
    digest = hashlib.sha256(response_raw, usedforsecurity=True).hexdigest()
    return NormalizedDocument(
        id=uuid5(NAMESPACE_URL, f"retailprintguard:rch:{job_id}:response:{digest}"),
        source_device_id=device_id,
        source_session_id=session_id,
        source_job_id=job_id,
        type=DocumentType.DEVICE_RESPONSE,
        subtype="RCH_RESPONSE_STREAM_CONFIRMED",
        captured_at=captured_at,
        status=(
            "ERROR"
            if errors
            else "COMPLETE"
            if not response.issues and not response.truncated and not unknown
            else "PARTIAL"
        ),
        normalized_text=text,
        encoding="latin-1-lossless-byte-view",
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        parse_confidence=100 if valid else 20,
        evidence=EvidenceLevel.CONFIRMED if valid else EvidenceLevel.UNKNOWN,
        source_manifest_sha256=manifest_sha256,
        source_payload_sha256=digest,
        source_path=source_path,
        complete=not response.issues and not response.truncated,
        warnings=tuple((*response.issues, *semantic_warnings)),
        lines=tuple(
            DocumentLine(
                sequence=index,
                description=frame.text,
                raw_text=frame.text,
                source=SourceSpan(
                    direction="DEVICE_TO_CLIENT",
                    offset=frame.offset,
                    length=len(frame.raw),
                    frame_id=str(frame.frame_id),
                ),
            )
            for index, frame in enumerate(valid, 1)
        ),
        raw_metadata={
            "ack_count": response.ack_count,
            "valid_response_frames": len(valid),
            "framing_issues": list(response.issues),
            "device_error_codes": errors,
            "unclassified_response_frames": unknown,
        },
    )


def parse_rch(
    request_raw: bytes,
    response_raw: bytes,
    *,
    device_id: str,
    session_id: str | None,
    job_id: str,
    captured_at: datetime,
    manifest_sha256: str,
    source_path: str | Path,
    response_source_path: str | Path | None = None,
    timezone_name: str = "Europe/Rome",
) -> tuple[NormalizedDocument, ...]:
    """Parse capture-confirmed frames and label all business roles as inferred."""

    request = frame_stream(request_raw)
    response = frame_stream(response_raw)
    source_hash = hashlib.sha256(
        request_raw + b"\x00RPG-DIRECTION\x00" + response_raw,
        usedforsecurity=True,
    ).hexdigest()
    response_candidates = [
        (
            index,
            frame,
            match.group("status_digits"),
            match.group("counter"),
        )
        for index, frame in enumerate(response.frames)
        if frame.address == "01"
        and frame.frame_class == "N"
        and frame.bcc_valid
        and (match := _COUNTER_RE.fullmatch(frame.text)) is not None
    ]
    used_responses: set[int] = set()
    status_by_document_start: dict[int, dict[str, str]] = {}
    commercial_start: int | None = None
    total_seen = False
    for frame in request.frames:
        if frame.address != "00" or frame.frame_class != "z" or not frame.bcc_valid:
            continue
        if frame.text == "=K":
            commercial_start = frame.frame_id
            total_seen = False
            continue
        if commercial_start is None:
            continue
        if _TOTAL_RE.fullmatch(frame.text):
            total_seen = True
            continue
        # Only the observed post-total status query can provide a document
        # suffix.  Live capture confirms that the six status digits can all be
        # zero while the physically printed document has a non-zero prefix;
        # therefore only ``CCCC`` is retained as evidence.  Sequence
        # correlation prevents a pre-document or unrelated response from
        # being silently attached to the receipt.
        if total_seen and frame.text == "<</?s" and frame.sequence.isdigit():
            expected = str((int(frame.sequence) + 8) % 10)
            for index, candidate, status_digits, counter in response_candidates:
                if index not in used_responses and candidate.sequence == expected:
                    used_responses.add(index)
                    status_by_document_start[commercial_start] = {
                        "status_digits": status_digits,
                        "counter": counter,
                    }
                    break
        if re.fullmatch(r"<</\?\d", frame.text):
            commercial_start = None
            total_seen = False
    documents: list[NormalizedDocument] = []
    for frame in request.frames:
        if (
            frame.address == "00"
            and frame.frame_class == "z"
            and frame.bcc_valid
            and frame.text == "=k"
            and len(documents) < _MAX_DOCUMENTS
        ):
            documents.append(
                _cancellation_command_document(
                    frame,
                    device_id=device_id,
                    session_id=session_id,
                    job_id=job_id,
                    captured_at=captured_at,
                    manifest_sha256=manifest_sha256,
                    source_hash=source_hash,
                    source_path=str(source_path),
                )
            )
    active: _Draft | None = None

    def finish(*, complete: bool, warning: str | None = None) -> None:
        nonlocal active
        if active is None or len(documents) >= _MAX_DOCUMENTS:
            active = None
            return
        active.complete = complete
        if warning:
            active.warnings.append(warning)
        active.warnings.extend(request.issues)
        documents.append(
            _draft_document(
                active,
                device_id=device_id,
                session_id=session_id,
                job_id=job_id,
                captured_at=captured_at,
                manifest_sha256=manifest_sha256,
                source_hash=source_hash,
                source_path=str(source_path),
                response_counter=(
                    status_by_document_start.get(active.frame_ids[0], {}).get("counter")
                    if active.frame_ids
                    else None
                ),
                timezone_name=timezone_name,
            )
        )
        active = None

    for frame in request.frames:
        if frame.address != "00" or frame.frame_class != "z" or not frame.bcc_valid:
            continue
        text = frame.text
        if text == "=o":
            if active is not None and active.kind == "management":
                active.frame_ids.append(frame.frame_id)
                active.end_offset = frame.offset + len(frame.raw)
                finish(complete=True)
            else:
                if active is not None:
                    finish(complete=False, warning="document_interrupted_by_management_open")
                active = _Draft(
                    "management",
                    len(documents) + 1,
                    frame.offset,
                    frame_ids=[frame.frame_id],
                    end_offset=frame.offset + len(frame.raw),
                )
            continue
        if text == "=K":
            if active is not None:
                finish(complete=False, warning="document_interrupted_by_commercial_open")
            observed_status = status_by_document_start.get(frame.frame_id)
            active = _Draft(
                "commercial",
                len(documents) + 1,
                frame.offset,
                external_document_code_suffix=(
                    observed_status.get("counter") if observed_status else None
                ),
                external_document_code_suffix_evidence=(
                    "RCH_STATUS_RESPONSE_SUFFIX_SEQUENCE_CONFIRMED"
                    if observed_status
                    else None
                ),
                response_status_digits=(
                    observed_status.get("status_digits") if observed_status else None
                ),
                frame_ids=[frame.frame_id],
                end_offset=frame.offset + len(frame.raw),
            )
            continue
        if active is None:
            continue
        active.frame_ids.append(frame.frame_id)
        active.end_offset = frame.offset + len(frame.raw)
        if active.kind == "management":
            if match := _MANAGEMENT_TEXT_RE.fullmatch(text):
                visible = match.group("text")
                active.texts.append(visible)
                _append_management_line(active, frame, visible)
            continue
        if match := _ITEM_RE.fullmatch(text):
            _append_commercial_item(active, frame, match)
            active.texts.append(f"{match.group('description')} {_cents(match.group('amount')):.2f}")
        elif match := _COMMERCIAL_TEXT_RE.fullmatch(text):
            visible = match.group("text")
            active.texts.append(visible)
            if order_match := _ORDER_RE.fullmatch(visible):
                active.order_code = order_match.group("value") or None
            if table_match := _TABLE_RE.fullmatch(visible):
                active.table_code = table_match.group("value") or None
        elif match := _TOTAL_RE.fullmatch(text):
            active.total = _cents(match.group("amount"))
            active.total_code = match.group("code")
            active.texts.append(f"TOTALE {active.total:.2f}")
        elif re.fullmatch(r"<</\?\d", text) and active.total is not None:
            finish(complete=True)
    if active is not None:
        finish(complete=False, warning="document_close_not_observed")

    if not documents and request_raw and request.issues:
        digest = hashlib.sha256(request_raw, usedforsecurity=True).hexdigest()
        documents.append(
            NormalizedDocument(
                id=uuid5(
                    NAMESPACE_URL, f"retailprintguard:rch:{job_id}:unknown:{digest}"
                ),
                source_device_id=device_id,
                source_session_id=session_id,
                source_job_id=job_id,
                type=DocumentType.UNKNOWN,
                subtype="RCH_MALFORMED_OR_UNRECOGNIZED",
                captured_at=captured_at,
                status="PARTIAL",
                normalized_text="",
                encoding="latin-1-lossless-byte-view",
                parser_name=PARSER_NAME,
                parser_version=PARSER_VERSION,
                parse_confidence=0,
                evidence=EvidenceLevel.UNKNOWN,
                source_manifest_sha256=manifest_sha256,
                source_payload_sha256=digest,
                source_path=str(source_path),
                complete=False,
                warnings=request.issues,
                raw_metadata={"framing_issues": list(request.issues)},
            )
        )
    response_document = _response_document(
        response,
        response_raw=response_raw,
        device_id=device_id,
        session_id=session_id,
        job_id=job_id,
        captured_at=captured_at,
        manifest_sha256=manifest_sha256,
        source_path=str(response_source_path or source_path),
    )
    if response_document is not None and len(documents) < _MAX_DOCUMENTS:
        documents.append(response_document)
    return tuple(documents)


__all__ = ["Frame", "FramedStream", "PARSER_NAME", "PARSER_VERSION", "frame_stream", "parse_rch"]
