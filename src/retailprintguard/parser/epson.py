"""Passive, bounded EpsonFpMate HTTP/SOAP interpretation of captured bytes.

Only documented report fields and reconciled commercial receipts observed in
captures are promoted to canonical documents. DirectIO payloads and native binary
protocols remain uninterpreted. A SOAP acknowledgement never proves paper delivery.
No network, database, printer command dispatch or cross-session state lives here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from retailprintguard.common.domain import (
    DocumentLine,
    DocumentType,
    EvidenceLevel,
    NormalizedDocument,
    PaymentRecord,
    SourceSpan,
)
from retailprintguard.common.service_mode import classify_service_mode
from retailprintguard.parser import opaque

PARSER_NAME = "retailprintguard-epson-observed"
PARSER_VERSION = "1.1.0"
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_BODY_BYTES = 256 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_MESSAGES = 256
MAX_XML_NODES = 2048
SOAP_NAMESPACE = "http://schemas.xmlsoap.org/soap/envelope/"
_REPORTS = {"printXReport": "X", "printZReport": "Z", "printXZReport": "XZ"}
_AMOUNT = re.compile(r"[0-9]{1,12}(?:[.,][0-9]{1,2})?\Z")
_QUANTITY = re.compile(r"[0-9]{1,9}(?:[.,][0-9]{1,4})?\Z")
_RECEIPT_NUMBER = re.compile(r"[0-9]{1,12}\Z")
_RECEIPT_DATE = re.compile(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})\Z")
_RECEIPT_TIME = re.compile(r"([0-9]{1,2}):([0-9]{1,2})\Z")
_TABLE_MESSAGE = re.compile(r"\s*Tavolo\s*:\s*([A-Za-z0-9][A-Za-z0-9/-]{0,31})\s*\Z", re.I)
_ORDER_MESSAGE = re.compile(r"\s*Ordine\s*:\s*([A-Za-z0-9][A-Za-z0-9/-]{0,31})\s*\Z", re.I)
_RECEIPT_COMMANDS = frozenset({
    "beginFiscalReceipt", "endFiscalReceipt", "printRecItem",
    "printRecSubtotalAdjustment", "printRecTotal", "printRecMessage",
    "openDrawer", "displayText",
})
MAX_RECEIPT_COMMANDS = 256
MAX_MODEL_MONEY = Decimal("999999999999999.9999")


class _Unsupported(ValueError):
    """Keep the original evidence opaque when framing or semantics is ambiguous."""


@dataclass(frozen=True)
class _Message:
    offset: int
    end: int
    body_offset: int
    body: bytes
    first_line: str
    status: int | None


def parser_runtime_fingerprint() -> str:
    return hashlib.sha256(Path(opaque.__file__).read_bytes()).hexdigest()


def _messages(raw: bytes, *, response: bool) -> tuple[_Message, ...]:
    if len(raw) > MAX_STREAM_BYTES:
        raise _Unsupported("stream_limit")
    cursor = 0
    result: list[_Message] = []
    while cursor < len(raw):
        if len(result) >= MAX_MESSAGES:
            raise _Unsupported("message_limit")
        header_end = raw.find(b"\r\n\r\n", cursor, cursor + MAX_HEADER_BYTES)
        if header_end < 0:
            raise _Unsupported("http_header_incomplete")
        try:
            lines = raw[cursor:header_end].decode("ascii").split("\r\n")
        except UnicodeDecodeError as exc:
            raise _Unsupported("http_header_encoding") from exc
        first = lines[0]
        status = None
        if response:
            match = re.fullmatch(r"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?", first)
            if match is None:
                raise _Unsupported("http_response_line")
            status = int(match[1])
        elif re.fullmatch(r"(?:GET|POST) /[^\s]* HTTP/1\.[01]", first) is None:
            raise _Unsupported("http_request_line")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            key, separator, value = line.partition(":")
            if not separator or re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", key) is None:
                raise _Unsupported("http_header_invalid")
            key = key.lower()
            if key in headers:
                raise _Unsupported("http_duplicate_header")
            headers[key] = value.strip()
        if (
            "transfer-encoding" in headers
            or headers.get("content-encoding", "identity") != "identity"
        ):
            raise _Unsupported("http_encoding_unsupported")
        body_offset = header_end + 4
        length = headers.get("content-length", "0" if first.startswith("GET ") else "")
        if response and not length and headers.get("connection", "").lower() == "close":
            # Observed Epson firmware delimits SOAP replies by connection close.
            # Consume only the final reverse stream; XML must still be complete.
            size = len(raw) - body_offset
        elif re.fullmatch(r"[0-9]{1,8}", length):
            size = int(length)
        else:
            raise _Unsupported("http_length_required")
        end = body_offset + size
        if size > MAX_BODY_BYTES or end > len(raw):
            raise _Unsupported("http_body_limit_or_incomplete")
        result.append(_Message(cursor, end, body_offset, raw[body_offset:end], first, status))
        cursor = end
    return tuple(result)


def _soap(body: bytes) -> ET.Element:
    # Decode first so UTF-16 or alternate encodings cannot bypass DTD rejection.
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _Unsupported("xml_encoding") from exc
    if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        raise _Unsupported("xml_declarations_forbidden")
    try:
        root = ET.fromstring(text)  # noqa: S314 - bounded UTF-8; DTD/entities rejected above
    except ET.ParseError as exc:
        raise _Unsupported("xml_invalid") from exc
    stack = [(root, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_XML_NODES or depth > 24 or len(node.attrib) > 32:
            raise _Unsupported("xml_structure_limit")
        if any(len(value) > 4096 for value in node.attrib.values()):
            raise _Unsupported("xml_attribute_limit")
        stack.extend((child, depth + 1) for child in node)
    if root.tag != f"{{{SOAP_NAMESPACE}}}Envelope":
        raise _Unsupported("soap_envelope_required")
    bodies = root.findall(f"{{{SOAP_NAMESPACE}}}Body")
    if len(bodies) != 1 or len(bodies[0]) != 1:
        raise _Unsupported("soap_body_ambiguous")
    return bodies[0][0]


def _response(message: _Message | None) -> tuple[dict[str, str], bool]:
    if message is None:
        return {}, False
    try:
        root = _soap(message.body)
    except _Unsupported:
        return {}, False
    if (
        root.tag != "response" or root.get("success") not in {"true", "false"}
        or set(root.attrib) - {"success", "code", "status"}
        or len(root) != 1
    ):
        return {}, False
    fields = dict(root.attrib)
    for info in root:
        if info.tag != "addInfo":
            return {}, False
        for child in info:
            if len(child) or child.tag in fields:
                return {}, False
            fields[child.tag] = (child.text or "").strip()
    acknowledged = (
        message.status == 200 and fields["success"] == "true" and fields.get("code", "") == ""
    )
    return fields, acknowledged


def _field_span(message: _Message, name: str) -> SourceSpan:
    match = re.search(
        rb"<" + name.encode("ascii") + rb"\s*>[^<]*</" + name.encode("ascii") + rb"\s*>",
        message.body,
    )
    offset, length = (match.start(), len(match[0])) if match else (0, len(message.body))
    return SourceSpan(
        direction="DEVICE_TO_CLIENT", offset=message.body_offset + offset, length=length,
    )


def _money(value: str | None, *, quantity: bool = False) -> Decimal:
    pattern = _QUANTITY if quantity else _AMOUNT
    if value is None or pattern.fullmatch(value) is None:
        raise _Unsupported("receipt_invalid_decimal")
    return Decimal(value.replace(",", "."))


def _bounded_money(value: Decimal) -> Decimal:
    """Keep computed SOAP values within the canonical Money model's range."""

    if not value.is_finite() or abs(value) > MAX_MODEL_MONEY:
        raise _Unsupported("receipt_amount_model_limit")
    return value


def _command_span(message: _Message, tag: str, cursor: int) -> tuple[SourceSpan, int]:
    """Locate a command opening tag, retaining its absolute request RAW offset."""

    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", tag) is None:
        raise _Unsupported("receipt_command_namespace_unsupported")
    match = re.search(rb"<" + tag.encode() + rb"(?=[\s/>])", message.body[cursor:])
    if match is None:
        raise _Unsupported("receipt_command_span_missing")
    start = cursor + match.start()
    quote: int | None = None
    for end in range(start + len(tag) + 1, len(message.body)):
        char = message.body[end]
        if char in (ord('"'), ord("'")):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        elif char == ord(">") and quote is None:
            return (
                SourceSpan(
                    direction="CLIENT_TO_DEVICE",
                    offset=message.body_offset + start,
                    length=end + 1 - start,
                ),
                end + 1,
            )
    raise _Unsupported("receipt_command_span_incomplete")


def _fiscal_receipt_document(
    request: _Message,
    response: _Message | None,
    root: ET.Element,
    fields: dict[str, str],
    acknowledged: bool,
    metadata: dict[str, object],
    *,
    device_id: str,
    session_id: str | None,
    job_id: str,
    captured_at: datetime,
    manifest_sha256: str,
    source_hash: str,
    source_path: str | Path,
    request_index: int,
    timezone_name: str,
) -> NormalizedDocument:
    """Interpret an observed, reconciled Epson commercial sale from SOAP.

    The response is the source for the fiscal total and daily progressive;
    item and discount rows come from the exact request command spans.  Neither
    an HTTP 200 nor a photo is treated as proof of physical paper delivery.
    """

    required = {
        "lastCommand", "fiscalReceiptNumber", "fiscalReceiptAmount",
        "fiscalReceiptDate", "fiscalReceiptTime", "zRepNumber",
    }
    listed = {name.strip() for name in fields.get("elementList", "").split(",")}
    if (
        response is None or not acknowledged or fields.get("lastCommand") != "74"
        or not required <= fields.keys() or not required <= listed
    ):
        raise _Unsupported("receipt_response_not_confirmed")
    receipt_number = fields["fiscalReceiptNumber"]
    z_number = fields["zRepNumber"]
    if (
        _RECEIPT_NUMBER.fullmatch(receipt_number) is None
        or _RECEIPT_NUMBER.fullmatch(z_number) is None
        or int(receipt_number) <= 0
    ):
        raise _Unsupported("receipt_fiscal_identity_invalid")
    total = _bounded_money(_money(fields["fiscalReceiptAmount"]))
    if total <= 0:
        # Epson documents zero for automatic voids; this adapter handles sales.
        raise _Unsupported("receipt_zero_or_void_total")
    date_match = _RECEIPT_DATE.fullmatch(fields["fiscalReceiptDate"])
    time_match = _RECEIPT_TIME.fullmatch(fields["fiscalReceiptTime"])
    if date_match is None or time_match is None:
        raise _Unsupported("receipt_fiscal_datetime_invalid")
    try:
        local_time = datetime(
            int(date_match[3]), int(date_match[2]), int(date_match[1]),
            int(time_match[1]), int(time_match[2]),
        )
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise _Unsupported("receipt_fiscal_datetime_invalid") from exc
    fold_zero = local_time.replace(tzinfo=zone, fold=0)
    fold_one = local_time.replace(tzinfo=zone, fold=1)
    fold_zero_valid = (
        fold_zero.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == local_time
    )
    fold_one_valid = (
        fold_one.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == local_time
    )
    clock_warning = None
    if fold_zero_valid and fold_one_valid and fold_zero.utcoffset() != fold_one.utcoffset():
        fiscal_time = None
        clock_warning = "receipt_fiscal_datetime_ambiguous_dst"
    elif not fold_zero_valid and not fold_one_valid:
        fiscal_time = None
        clock_warning = "receipt_fiscal_datetime_nonexistent_dst"
    else:
        fiscal_time = fold_zero if fold_zero_valid else fold_one

    commands = list(root)
    if len(commands) > MAX_RECEIPT_COMMANDS or any(
        child.tag not in _RECEIPT_COMMANDS
        or len(child) != 0
        or (child.text or "").strip()
        for child in commands
    ):
        raise _Unsupported("receipt_command_semantics_unsupported")
    names = [child.tag for child in commands]
    if names.count("beginFiscalReceipt") != 1 or names.count("endFiscalReceipt") != 1:
        raise _Unsupported("receipt_boundaries_ambiguous")
    begin = names.index("beginFiscalReceipt")
    end = names.index("endFiscalReceipt")
    if begin >= end or "printRecItem" not in names[begin + 1:end]:
        raise _Unsupported("receipt_sale_boundary_missing")

    command_spans: list[SourceSpan] = []
    cursor = 0
    for child in commands:
        span, cursor = _command_span(request, child.tag, cursor)
        command_spans.append(span)
    economic_lines: list[DocumentLine] = []
    payments: list[PaymentRecord] = []
    modifiers: list[dict[str, str]] = []
    table_codes: set[str] = set()
    order_codes: set[str] = set()
    subtotal = Decimal("0.00")
    discount_total = Decimal("0.00")
    surcharge_total = Decimal("0.00")
    tendered = Decimal("0.00")
    zero_payment = False
    for index, (child, span) in enumerate(zip(commands, command_spans, strict=True)):
        tag = child.tag
        if tag == "printRecMessage":
            message = (child.get("message") or "").strip()
            if match := _TABLE_MESSAGE.fullmatch(message):
                table_codes.add(match[1])
            if match := _ORDER_MESSAGE.fullmatch(message):
                order_codes.add(match[1])
            continue
        if tag not in {"printRecItem", "printRecSubtotalAdjustment", "printRecTotal"}:
            continue
        if not begin < index < end:
            raise _Unsupported("receipt_command_outside_boundaries")
        if tag == "printRecItem":
            if payments:
                raise _Unsupported("receipt_item_after_payment")
            description = (child.get("description") or "").strip()
            quantity = _money(child.get("quantity"), quantity=True)
            unit = _money(child.get("unitPrice"))
            if not description or len(description) > 191 or quantity <= 0:
                raise _Unsupported("receipt_item_invalid")
            line_total = _bounded_money((quantity * unit).quantize(Decimal("0.01")))
            subtotal = _bounded_money(subtotal + line_total)
            economic_lines.append(DocumentLine(
                sequence=len(economic_lines) + 1,
                description=description, quantity=quantity, unit_price=unit,
                line_total=line_total, state="SALE_ITEM", raw_text=description,
                source=span,
            ))
        elif tag == "printRecSubtotalAdjustment":
            if payments:
                raise _Unsupported("receipt_adjustment_after_payment")
            adjustment_type = child.get("adjustmentType")
            sign = {"1": "-", "2": "-", "6": "+", "7": "+"}.get(
                adjustment_type or ""
            )
            amount = _money(child.get("amount"))
            description = (child.get("description") or "").strip()
            if sign is None or amount <= 0 or not description or len(description) > 191:
                raise _Unsupported("receipt_adjustment_invalid")
            if sign == "-":
                discount_total = _bounded_money(discount_total + amount)
            else:
                surcharge_total = _bounded_money(surcharge_total + amount)
            modifiers.append({
                "sign": sign, "description": description, "amount": str(amount),
            })
            economic_lines.append(DocumentLine(
                sequence=len(economic_lines) + 1, description=description,
                discount=amount if sign == "-" else None,
                surcharge=amount if sign == "+" else None,
                line_total=-amount if sign == "-" else amount,
                state="SUBTOTAL_DISCOUNT" if sign == "-" else "SUBTOTAL_SURCHARGE",
                raw_text=description, source=span,
            ))
        else:
            amount = _money(child.get("payment"))
            payment_type = child.get("paymentType")
            if payment_type not in {"0", "1", "2", "3", "4", "5", "6"}:
                raise _Unsupported("receipt_payment_type_unsupported")
            if amount == 0:
                if payments or zero_payment:
                    raise _Unsupported("receipt_zero_payment_ambiguous")
                zero_payment = True
                amount = total
            method = (child.get("description") or "").strip()
            if not method or len(method) > 128:
                method = "CONTANTI" if payment_type == "0" else f"EPSON TIPO {payment_type}"
            payments.append(PaymentRecord(
                method=method.upper(), amount=amount, evidence=EvidenceLevel.INFERRED,
            ))
            tendered = _bounded_money(tendered + amount)
    if not payments:
        raise _Unsupported("receipt_payment_missing")
    if len(table_codes) > 1 or len(order_codes) > 1:
        raise _Unsupported("receipt_service_marker_conflict")
    table_code = next(iter(table_codes), None)
    order_code = next(iter(order_codes), None)
    service_mode = classify_service_mode(
        order_code=order_code,
        table_code=table_code,
        table_evidence="EPSON_SOAP_PRINT_REC_MESSAGE_TABLE",
    )
    arithmetic_reconciled = _bounded_money(
        (subtotal - discount_total + surcharge_total).quantize(Decimal("0.01"))
    ) == total
    payment_reconciled = tendered.quantize(Decimal("0.01")) == total
    complete = arithmetic_reconciled and payment_reconciled
    warnings = [
        "acknowledgement_does_not_prove_physical_delivery",
        "receipt_text_reconstructed_not_original_print_layout",
    ]
    if not arithmetic_reconciled:
        warnings.append("receipt_item_adjustment_total_mismatch")
    if not payment_reconciled:
        warnings.append("receipt_payment_total_mismatch_or_change")
    if clock_warning:
        warnings.append(clock_warning)
    text_lines = ["DOCUMENTO COMMERCIALE EPSON (DATI SOAP)"]
    for line in economic_lines:
        if line.state == "SALE_ITEM":
            text_lines.append(
                f"{line.description}  {line.quantity} × "
                f"{line.unit_price:.2f} = {line.line_total:.2f}"
            )
        else:
            text_lines.append(f"{line.description}  {line.line_total:+.2f}")
    text_lines.append(f"Totale documento (risposta Epson): {total:.2f}")
    for payment in payments:
        text_lines.append(f"Pagamento {payment.method}: {payment.amount:.2f}")
    text_lines.append(
        "Data fiscale (risposta Epson): "
        f"{fields['fiscalReceiptDate']} {fields['fiscalReceiptTime']}"
    )
    text_lines.append(
        f"Progressivo giornaliero (risposta Epson): {receipt_number}; Z: {z_number}"
    )
    if table_code:
        text_lines.append(f"Tavolo: {table_code}")
    if order_code:
        text_lines.append(f"Ordine: {order_code}")
    metadata.update({
        "fiscal_receipt_number": receipt_number,
        "z_report_number": z_number,
        "epson_serial_number": fields.get("serialNumber") or None,
        "fiscal_identity_evidence": "EPSON_SOAP_RESPONSE_FIELDS",
        "fiscal_total_source_span": _field_span(
            response, "fiscalReceiptAmount"
        ).model_dump(mode="json"),
        "document_timestamp_evidence": (
            "EPSON_SOAP_RESPONSE_PRINTER_CLOCK" if fiscal_time is not None
            else "EPSON_SOAP_LOCAL_CLOCK_DST_UNRESOLVED"
        ),
        "document_timestamp_precision": "minute",
        "text_projection": "RECONSTRUCTED_FROM_SOAP_NOT_PRINTED_TEXT",
        "subtotal_modifiers": modifiers,
        "observed_item_subtotal": str(subtotal),
        "arithmetic_reconciled": arithmetic_reconciled,
        "payment_reconciled": payment_reconciled,
        "service_mode": service_mode.mode,
        "service_mode_evidence": service_mode.evidence,
        "zero_payment_interpreted_from_response": zero_payment,
    })
    return NormalizedDocument(
        id=uuid5(
            NAMESPACE_URL,
            f"retailprintguard:epson:{device_id}:{job_id}:request:{request_index}",
        ),
        source_device_id=device_id, source_session_id=session_id,
        source_job_id=job_id, type=DocumentType.COMMERCIAL_DOCUMENT,
        subtype="EPSON_COMMERCIAL_SALE_SOAP_OBSERVED",
        external_document_code_suffix=receipt_number,
        order_code=order_code, table_code=table_code,
        document_timestamp=fiscal_time, captured_at=captured_at,
        gross_total=total, net_total=total, discount_total=discount_total,
        status="ACKNOWLEDGED" if complete else "PARTIAL",
        normalized_text="\n".join(text_lines), encoding="utf-8",
        parser_name=PARSER_NAME, parser_version=PARSER_VERSION,
        parse_confidence=95 if complete else 55,
        evidence=EvidenceLevel.INFERRED,
        source_manifest_sha256=manifest_sha256,
        source_payload_sha256=source_hash, source_path=str(source_path),
        complete=complete, warnings=tuple(warnings),
        lines=tuple(economic_lines), payments=tuple(payments),
        raw_metadata=metadata,
    )


def parse_epson(
    request_raw: bytes,
    response_raw: bytes = b"",
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
    common = dict(
        device_id=device_id, session_id=session_id, job_id=job_id, captured_at=captured_at,
        manifest_sha256=manifest_sha256, source_path=source_path,
        response_source_path=response_source_path,
    )

    def fallback(reason: str) -> tuple[NormalizedDocument, ...]:
        return tuple(
            document.model_copy(update={
                "parser_name": PARSER_NAME, "parser_version": PARSER_VERSION,
                "warnings": (*document.warnings, reason),
            })
            for document in opaque.parse_opaque(request_raw, response_raw, **common)
        )

    if not request_raw:
        return fallback("epson_request_not_observed")
    try:
        requests = _messages(request_raw, response=False)
        responses = _messages(response_raw, response=True)
    except _Unsupported as exc:
        return fallback(str(exc))
    # Never shift a success response onto the wrong request when captures have gaps.
    paired = len(requests) == len(responses)
    records: list[NormalizedDocument] = []
    for index, request in enumerate(requests):
        response = responses[index] if paired else None
        fields, acknowledged = _response(response)
        try:
            method, target, _ = request.first_line.split(" ")
            if method != "POST" or target.split("?", 1)[0] != "/cgi-bin/fpmate.cgi":
                raise _Unsupported("not_fpmate_request")
            root = _soap(request.body)
        except _Unsupported as exc:
            return fallback(str(exc))
        commands = [child.tag for child in root]
        metadata: dict[str, object] = {
            "direction": "CLIENT_TO_DEVICE", "raw_offset": request.offset,
            "raw_length": request.end - request.offset, "protocol": "EPSON_FPMATE_SOAP_HTTP",
            "request_root": root.tag, "commands": commands,
            "response_paired": paired, "command_acknowledged": acknowledged,
            "physical_delivery": "UNKNOWN", "document_boundaries": "HTTP_SOAP_MESSAGE",
        }
        if response is not None:
            metadata["response_span"] = {
                "direction": "DEVICE_TO_CLIENT", "offset": response.offset,
                "length": response.end - response.offset,
            }
        kind, subtype = DocumentType.UNKNOWN, "EPSON_TECHNICAL_COMMAND"
        text = ""
        complete = False
        total = None
        code = None
        lines: tuple[DocumentLine, ...] = ()
        warnings = ["acknowledgement_does_not_prove_physical_delivery"]
        if root.tag == "printerFiscalReceipt":
            try:
                receipt = _fiscal_receipt_document(
                    request, response, root, fields, acknowledged, metadata,
                    device_id=device_id, session_id=session_id, job_id=job_id,
                    captured_at=captured_at, manifest_sha256=manifest_sha256,
                    source_hash=hashlib.sha256(request_raw).hexdigest(),
                    source_path=source_path, request_index=index,
                    timezone_name=timezone_name,
                )
            except _Unsupported as exc:
                warnings.append(str(exc))
            except (InvalidOperation, OverflowError, ValidationError):
                # A malformed or oversized sale cannot abort the parser worker.
                warnings.append("receipt_numeric_or_model_validation_failed")
            else:
                records.append(receipt)
                continue
        report_commands = [child for child in root if child.tag in _REPORTS]
        if root.tag == "printerFiscalReport" and len(report_commands) == 1 and len(root) == 1:
            report = report_commands[0]
            report_kind = _REPORTS[report.tag]
            kind, subtype = DocumentType.SHIFT_END_REPORT, f"EPSON_{report_kind}_REPORT_OBSERVED"
            daily = fields.get("dailyAmount", "")
            z_number = fields.get("zRepNumber", "")
            valid_number = re.fullmatch(r"[0-9]{1,12}", z_number) is not None
            if acknowledged and _AMOUNT.fullmatch(daily) and valid_number:
                total = Decimal(daily.replace(",", "."))
                code = z_number if report_kind != "X" else None
                complete = True
                assert response is not None  # noqa: S101 - acknowledged requires a response
                lines = (DocumentLine(
                    sequence=1, description="Totale giornaliero (risposta Epson)",
                    line_total=total, state="DAILY_REPORT_TOTAL", source=_field_span(
                        response, "dailyAmount",
                    ),
                ),)
            metadata.update({
                "report_kind": report_kind, "z_report_number": z_number or None,
                "total_scope": "DAILY_CUMULATIVE_NOT_SALE",
                "text_projection": "RECONSTRUCTED_FROM_SOAP_NOT_PRINTED_TEXT",
            })
            text = f"RAPPORTO FISCALE EPSON {report_kind} (DATI SOAP)\n"
            if complete:
                text += f"Progressivo Z: {z_number}\nTotale giornaliero: {total:.2f}\n"
            else:
                text += "Esito o dati del rapporto non confermati\n"
                warnings.append("report_response_missing_failed_or_invalid")
            warnings.append("report_text_reconstructed_not_original_print_layout")
        elif root.tag == "printerCommand" and len(root) == 1 and commands == ["directIO"]:
            command = root[0].get("command", "")
            metadata["direct_io_command"] = command
            metadata["direct_io_semantics"] = "UNKNOWN"
            if fields.get("responseCommand") != command:
                metadata["command_acknowledged"] = False
                warnings.append("direct_io_response_command_missing_or_mismatched")
        elif root.tag != "printerCommand" or commands != ["openDrawer"]:
            subtype = "EPSON_UNSUPPORTED_COMMAND"
            warnings.append("epson_command_semantics_not_implemented")
        records.append(NormalizedDocument(
            id=uuid5(NAMESPACE_URL, f"retailprintguard:epson:{device_id}:{job_id}:request:{index}"),
            source_device_id=device_id, source_session_id=session_id, source_job_id=job_id,
            type=kind, subtype=subtype, external_document_code=code,
            captured_at=captured_at, gross_total=total,
            status="ACKNOWLEDGED" if metadata["command_acknowledged"] else "UNCONFIRMED",
            normalized_text=text, encoding="utf-8", parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION, parse_confidence=95 if complete else 0,
            evidence=EvidenceLevel.INFERRED if complete else EvidenceLevel.UNKNOWN,
            source_manifest_sha256=manifest_sha256,
            source_payload_sha256=hashlib.sha256(request_raw).hexdigest(),
            source_path=str(source_path), complete=complete, warnings=tuple(warnings),
            lines=lines, raw_metadata=metadata,
        ))
    # Retain the entire reverse stream, including unpaired/non-SOAP HTTP responses.
    if response_raw:
        response_record = opaque.parse_opaque(
            b"", response_raw, **common,
        )[0]
        records.append(response_record.model_copy(update={
            "type": DocumentType.DEVICE_RESPONSE, "subtype": "EPSON_HTTP_RESPONSE_STREAM",
            "parser_name": PARSER_NAME, "parser_version": PARSER_VERSION,
        }))
    return tuple(records)


__all__ = ["PARSER_NAME", "PARSER_VERSION", "parse_epson"]
