"""Passive, bounded EpsonFpMate HTTP/SOAP interpretation of captured bytes.

Only documented report fields observed in captures are promoted to canonical
documents. DirectIO payloads, receipt commands and native binary protocols remain
uninterpreted. HTTP success or a SOAP acknowledgement never proves paper delivery.
No network, database, printer command dispatch or cross-session state lives here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree as ET

from retailprintguard.common.domain import (
    DocumentLine,
    DocumentType,
    EvidenceLevel,
    NormalizedDocument,
    SourceSpan,
)
from retailprintguard.parser import opaque

PARSER_NAME = "retailprintguard-epson-observed"
PARSER_VERSION = "1.0.0"
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_BODY_BYTES = 256 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_MESSAGES = 256
MAX_XML_NODES = 2048
SOAP_NAMESPACE = "http://schemas.xmlsoap.org/soap/envelope/"
_REPORTS = {"printXReport": "X", "printZReport": "Z", "printXZReport": "XZ"}
_AMOUNT = re.compile(r"[0-9]{1,12}(?:[.,][0-9]{1,2})?\Z")


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
