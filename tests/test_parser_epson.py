from __future__ import annotations

import hashlib
import socket
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from retailprintguard.common.domain import DocumentType, EvidenceLevel
from retailprintguard.parser import epson
from retailprintguard.render.eligibility import classify_pdf_document
from retailprintguard.render.text import receipt_preview_text

NOW = datetime(2042, 1, 2, tzinfo=UTC)


def _soap(xml: str) -> bytes:
    return (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<s:Body>{xml}</s:Body></s:Envelope>"
    ).encode()


def _http(body: bytes, *, response: bool = False, status: int = 200) -> bytes:
    first = f"HTTP/1.1 {status} Test" if response else "POST /cgi-bin/fpmate.cgi HTTP/1.1"
    return (
        f"{first}\r\nContent-Type: text/xml; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body


def _request(command: str = '<printXZReport operator="1"/>') -> bytes:
    return _http(_soap(f"<printerFiscalReport>{command}</printerFiscalReport>"))


def _response(fields: str = "", *, success: str = "true", status: int = 200) -> bytes:
    return _http(_soap(
        f'<response success="{success}" code="" status="2"><addInfo>'
        f"{fields}</addInfo></response>"
    ), response=True, status=status)


REPORT_FIELDS = (
    "<lastCommand>74</lastCommand><printerStatus>20010</printerStatus>"
    "<zRepNumber>42</zRepNumber><dailyAmount>123,45</dailyAmount>"
)


def _parse(request: bytes, response: bytes = b""):
    return epson.parse_epson(
        request, response, device_id="epson_synthetic", session_id="session-synthetic",
        job_id="job-synthetic", captured_at=NOW, manifest_sha256="a" * 64,
        source_path="/synthetic/client.raw", response_source_path="/synthetic/device.raw",
    )


def test_report_uses_canonical_model_and_both_raw_directions_without_network(monkeypatch):
    def forbidden_socket(*args, **kwargs):
        raise AssertionError("offline parser must not open any socket")

    monkeypatch.setattr(socket, "socket", forbidden_socket)
    request, response = _request(), _response(REPORT_FIELDS)
    report, reverse = _parse(request, response)
    assert report.type is DocumentType.SHIFT_END_REPORT
    assert report.subtype == "EPSON_XZ_REPORT_OBSERVED"
    assert report.gross_total == Decimal("123.45")
    assert report.external_document_code == "42"
    assert report.complete is True
    assert report.evidence is EvidenceLevel.INFERRED
    assert report.status == "ACKNOWLEDGED"
    assert report.document_timestamp is None  # capture time is not a fiscal date
    assert report.receipt_header is None
    assert report.payments == ()
    assert report.rch_footer_timestamp is report.rch_serial_number is None
    assert report.raw_metadata["physical_delivery"] == "UNKNOWN"
    assert report.raw_metadata["total_scope"] == "DAILY_CUMULATIVE_NOT_SALE"
    assert report.source_payload_sha256 == hashlib.sha256(request).hexdigest()
    span = report.lines[0].source
    assert span.direction == "DEVICE_TO_CLIENT"
    assert response[span.offset:span.offset + span.length] == b"<dailyAmount>123,45</dailyAmount>"
    assert reverse.type is DocumentType.DEVICE_RESPONSE
    assert reverse.source_payload_sha256 == hashlib.sha256(response).hexdigest()
    assert reverse.source_path.endswith("device.raw")
    assert classify_pdf_document(reverse).category == "excluded"
    assert classify_pdf_document(report).category == "complete"
    assert "123.45" in receipt_preview_text(report)
    assert "DATI SOAP" in receipt_preview_text(report)
    assert _parse(request, response) == (report, reverse)


@pytest.mark.parametrize("name", ["printXReport", "printZReport", "printXZReport"])
def test_documented_report_types_and_decimal_separator(name):
    report = _parse(
        _request(f'<{name} operator="1"/>'),
        _response(REPORT_FIELDS.replace("123,45", "0.00")),
    )[0]
    assert report.type is DocumentType.SHIFT_END_REPORT
    assert report.gross_total == 0
    assert report.complete
    assert (report.external_document_code is None) == (name == "printXReport")


@pytest.mark.parametrize("response", [
    b"", _response(REPORT_FIELDS, success="false"),
    _response(REPORT_FIELDS, status=500), _response(""),
    _response(REPORT_FIELDS.replace("123,45", "NaN")),
    _response(REPORT_FIELDS.replace("123,45", "1.234,56")),
    _response(REPORT_FIELDS.replace("123,45", "-10.00")),
    _response(REPORT_FIELDS.replace("42</zRepNumber>", "</zRepNumber>")),
    _response(REPORT_FIELDS + "<dailyAmount>999</dailyAmount>"),
])
def test_missing_failed_ambiguous_or_invalid_report_never_invents_total(response):
    report = _parse(_request(), response)[0]
    assert report.type is DocumentType.SHIFT_END_REPORT
    assert report.complete is False
    assert report.gross_total is None
    assert report.external_document_code is None
    assert report.lines == report.payments == ()
    assert report.document_timestamp is None


def test_aggregated_http_messages_have_absolute_raw_offsets_and_stable_ids():
    request = _request()
    response = _response(REPORT_FIELDS)
    records = _parse(request * 2, response * 2)
    first, second, reverse = records
    assert first.id != second.id != reverse.id
    assert second.raw_metadata["raw_offset"] == len(request)
    span = second.lines[0].source
    assert span.offset > len(response)
    assert (response * 2)[span.offset:span.offset + span.length].startswith(b"<dailyAmount>")
    assert first.gross_total == second.gross_total == Decimal("123.45")


def test_missing_one_response_prevents_cross_pairing_all_requests():
    records = _parse(_request() * 2, _response(REPORT_FIELDS))
    assert all(record.gross_total is None for record in records)
    assert all(record.complete is False for record in records)
    assert all(not record.raw_metadata["response_paired"] for record in records[:-1])


def test_observed_close_delimited_soap_response_requires_complete_unambiguous_xml():
    body = _soap('<response success="true" code="" status="2"><addInfo>'
                 + REPORT_FIELDS + '</addInfo></response>')
    response = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + body
    report = _parse(_request(), response)[0]
    assert report.complete
    assert report.gross_total == Decimal("123.45")
    for invalid in (response[:-10], response + b"HTTP/1.1 200 OK\r\n\r\n", response * 2):
        record = _parse(_request(), invalid)[0]
        assert not record.complete and record.gross_total is None
    no_delimiter = response.replace(b"Connection: close", b"Connection: keep-alive")
    assert all(record.type is DocumentType.UNKNOWN for record in _parse(_request(), no_delimiter))


@pytest.mark.parametrize("command,fields", [
    ('<openDrawer operator="1"/>', "<lastCommand>50</lastCommand>"),
    ('<directIO command="3016" data="01SYNTHETIC CONFIGURATION"/>',
     "<responseCommand>3016</responseCommand><responseData>01</responseData>"),
    ('<directIO command="4002" data="01SYNTHETIC 12345"/>',
     "<responseCommand>4002</responseCommand><responseData>01</responseData>"),
    ('<directIO command="9999" data="unknown"/>', "<responseCommand>4002</responseCommand>"),
])
def test_technical_commands_and_configuration_never_become_receipts(command, fields):
    request = _http(_soap(f"<printerCommand>{command}</printerCommand>"))
    record, reverse = _parse(request, _response(fields))
    assert record.type is DocumentType.UNKNOWN
    assert record.normalized_text == ""
    assert record.receipt_header is None
    assert record.gross_total is record.external_document_code is None
    assert record.lines == record.payments == ()
    assert not record.complete
    assert classify_pdf_document(record).category == "excluded"
    assert reverse.type is DocumentType.DEVICE_RESPONSE
    if 'command="9999"' in command:
        assert record.status == "UNCONFIRMED"


@pytest.mark.parametrize("payload", [
    b"\x02native-protocol\x03", b"TLS-uninterpreted", b"<printerFiscalReceipt/>",
    _request()[:-1],
    _request().replace(b"Content-Length:", b"Transfer-Encoding: chunked\r\nContent-Length:"),
    _request().replace(b"Content-Length:", b"Content-Length: 0\r\nContent-Length:"),
    _http(_soap("<printerFiscalReport><printXZReport/></printerFiscalReport>").replace(
        b"<s:Body>", b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///synthetic/secret">]><s:Body>',
    )),
    _http(_soap("<x>" * 30 + "</x>" * 30)),
    _http(_soap("<x/>" * (epson.MAX_XML_NODES + 1))),
    _http(_soap("<printerFiscalReport/>").replace(
        b"http://schemas.xmlsoap.org/soap/envelope/", b"urn:synthetic:wrong-namespace",
    )),
    _http(_soap("<printerFiscalReport/>").decode().encode("utf-16")),
    _http(_soap("<printerFiscalReport><printXZReport/></printerFiscalReport>"))
    + b"INCOMPLETE NEXT MESSAGE",
])
def test_hostile_or_unimplemented_streams_remain_opaque(payload):
    records = _parse(payload, _response(REPORT_FIELDS))
    assert records
    assert all(record.type is DocumentType.UNKNOWN for record in records)
    assert all(record.gross_total is None and not record.complete for record in records)
    assert records[0].source_payload_sha256 == hashlib.sha256(payload).hexdigest()


def test_unobserved_sale_commands_are_not_inferred_from_report_or_response():
    request = _http(_soap(
        '<printerFiscalReceipt><beginFiscalReceipt operator="1"/>'
        '<printRecItem description="SYNTHETIC" quantity="1" unitPrice="10.00"/>'
        '<printRecTotal payment="10.00"/><endFiscalReceipt operator="1"/>'
        '</printerFiscalReceipt>'
    ))
    record = _parse(request, _response(REPORT_FIELDS))[0]
    assert record.type is DocumentType.UNKNOWN
    assert record.subtype == "EPSON_UNSUPPORTED_COMMAND"
    assert record.gross_total is None
    assert record.lines == record.payments == ()
    assert not record.complete


def test_response_attributes_cannot_impersonate_report_fields():
    body = _soap('<response success="true" code="" dailyAmount="123.45" zRepNumber="42">'
                 '<addInfo/></response>')
    report = _parse(_request(), _http(body, response=True))[0]
    assert not report.complete
    assert report.gross_total is None


def test_limits_and_empty_streams_are_safe(monkeypatch):
    assert _parse(b"") == ()
    monkeypatch.setattr(epson, "MAX_MESSAGES", 1)
    assert _parse(_request() * 2)[0].type is DocumentType.UNKNOWN
    monkeypatch.setattr(epson, "MAX_STREAM_BYTES", 1)
    assert _parse(_request())[0].type is DocumentType.UNKNOWN


def test_worker_routes_existing_epson_config_and_stores_idempotent_report():
    from sqlalchemy import func, select
    from test_parser_worker_db import _database

    from retailprintguard.db.models import Document, DocumentVersion, ParserVersion
    from retailprintguard.parser.repository import SqlAlchemyParserRepository
    from retailprintguard.parser.worker import ParserWorker

    engine, factory, _, _, _ = _database(
        parser_kind="opaque_raw", request=_request(), response=_response(REPORT_FIELDS),
    )
    try:
        repository = SqlAlchemyParserRepository(factory)
        report = ParserWorker(repository).run_once()
        assert (report.parsed_jobs, report.parsed_documents, report.failed) == (1, 2, 0)
        assert repository.pending_jobs(limit=10, reparse=True) == ()
        assert ParserWorker(repository).run_once().discovered == 0
        with factory() as session:
            assert set(session.scalars(select(Document.document_type))) == {
                "SHIFT_END_REPORT", "DEVICE_RESPONSE",
            }
            assert session.scalar(select(func.count(DocumentVersion.id))) == 2
            assert set(session.scalars(select(ParserVersion.name))) == {epson.PARSER_NAME}
    finally:
        engine.dispose()


def test_build_identity_includes_opaque_fallback(tmp_path, monkeypatch):
    from retailprintguard.parser.repository import _parser_build_sha256

    before = _parser_build_sha256(epson.PARSER_NAME)
    changed_fallback = tmp_path / "synthetic-fallback.py"
    changed_fallback.write_text("# synthetic fallback identity change\n")
    monkeypatch.setattr(epson.opaque, "__file__", str(changed_fallback))
    assert _parser_build_sha256(epson.PARSER_NAME) != before


def test_device_filtered_reparse_preserves_other_device_interpretations():
    from uuid import uuid4

    from sqlalchemy import func, select
    from test_parser_worker_db import _database

    from retailprintguard.db.models import Device, Document, PrintJob, ProxySession, RawPayload
    from retailprintguard.parser.repository import SqlAlchemyParserRepository
    from retailprintguard.parser.worker import ParserWorker

    request = _request()
    engine, factory, first_job, _, _ = _database(
        parser_kind="opaque_raw", request=request, response=_response(REPORT_FIELDS),
    )
    other_device, other_session, other_job = uuid4(), uuid4(), uuid4()
    with factory.begin() as session:
        session.add(Device(
            id=other_device, external_id="epson_other", name="Other synthetic Epson",
            device_type="epson", parser_kind="opaque_raw", listen_ip="192.0.2.30",
            listen_port=9100, target_ip="192.0.2.40", target_port=9100,
        ))
        session.add(ProxySession(
            id=other_session, device_id=other_device, source_system="test",
            source_instance="synthetic", source_scope="epson_other",
            source_session_id="other-session", listen_ip="192.0.2.30", listen_port=9100,
            target_ip="192.0.2.40", target_port=9100, started_at=NOW, status="CLOSED",
        ))
        session.flush()
        session.add(PrintJob(
            id=other_job, device_id=other_device, session_id=other_session,
            source_key=f"test:synthetic:{other_job}", source_system="test",
            source_instance="synthetic", source_scope="epson_other",
            source_job_id="other-job", source_schema="test.v1",
            manifest_sha256="b" * 64, manifest_path="/synthetic/other/manifest.json",
            started_at=NOW, captured_at=NOW, status="COMPLETE",
            capture_complete=True, timeline_complete=True, import_status="IMPORTED",
        ))
        session.flush()
        session.add(RawPayload(
            id=uuid4(), job_id=other_job, device_id=other_device,
            session_id=other_session, artifact_role="REQUEST_RAW",
            direction="CLIENT_TO_DEVICE", payload=request, byte_count=len(request),
            sha256=hashlib.sha256(request).hexdigest(),
            source_path="/synthetic/other/client.raw",
            source_path_sha256=hashlib.sha256(b"/synthetic/other/client.raw").hexdigest(),
            complete=True, chain_scope=f"raw:{other_job}", chain_sequence=1,
            previous_record_hash="0" * 64, record_hash="1" * 64,
        ))
    try:
        repository = SqlAlchemyParserRepository(factory)
        assert repository.pending_jobs(
            limit=10, reparse=True, device_id="device_synthetic",
        ) == (str(first_job),)
        assert repository.pending_jobs(
            limit=10, reparse=True, device_id="epson_other",
        ) == (str(other_job),)
        report = ParserWorker(repository).run_once(
            limit=10, reparse=True, device_id="device_synthetic",
        )
        assert (report.parsed_jobs, report.failed) == (1, 0)
        with factory() as session:
            assert session.scalar(
                select(func.count(Document.id)).where(Document.job_id == other_job)
            ) == 0
            assert session.get(PrintJob, other_job).import_status == "IMPORTED"
        assert repository.pending_jobs(
            limit=10, reparse=True, device_id="device_synthetic",
        ) == ()
        assert repository.pending_jobs(
            limit=10, reparse=True, device_id="epson_other",
        ) == (str(other_job),)
        with pytest.raises(ValueError):
            repository.pending_jobs(limit=10, device_id="device_synthetic")
        with pytest.raises(ValueError):
            ParserWorker(repository).run_once(device_id="device_synthetic")
    finally:
        engine.dispose()
