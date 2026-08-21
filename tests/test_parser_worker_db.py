from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select

from retailprintguard.common.domain import DocumentLine as DomainLine
from retailprintguard.common.domain import DocumentType, ReceiptHeader
from retailprintguard.correlation.worker import load_latest_documents
from retailprintguard.db import Base, create_db_engine, session_factory
from retailprintguard.db.models import (
    Device,
    Document,
    DocumentLine,
    DocumentVersion,
    PrintJob,
    ProxySession,
    RawPayload,
)
from retailprintguard.parser import escpos as escpos_parser
from retailprintguard.parser import repository as parser_repository
from retailprintguard.parser import worker as parser_worker
from retailprintguard.parser.escpos import parse_escpos
from retailprintguard.parser.repository import SqlAlchemyParserRepository
from retailprintguard.parser.worker import ParserWorker

NOW = datetime(2042, 7, 8, 12, 0, tzinfo=UTC)


class _RecordingBeeper:
    def __init__(self, events: list[str] | None = None) -> None:
        self.device_ids: list[str] = []
        self.events = events

    def enqueue(self, device_id: str, *, event_id: str | None = None) -> bool:
        del event_id
        self.device_ids.append(device_id)
        if self.events is not None:
            self.events.append("beep")
        return True

    def supports(self, _device_id: str) -> bool:
        return True


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _rch_frame(
    data: str,
    *,
    address: str = "00",
    frame_class: str = "z",
    sequence: str = "0",
) -> bytes:
    encoded = data.encode("latin-1")
    prefix = (
        b"\x02"
        + address.encode("ascii")
        + f"{len(encoded):03d}".encode("ascii")
        + frame_class.encode("ascii")
        + encoded
        + sequence.encode("ascii")
    )
    checksum = 0
    for value in prefix:
        checksum ^= value
    return prefix + f"{checksum:02X}".encode("ascii") + b"\x03"


def _database(*, parser_kind: str, request: bytes, response: bytes = b""):
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    device_id, session_id, job_id = uuid4(), uuid4(), uuid4()
    request_id, response_id = uuid4(), uuid4()
    with factory.begin() as session:
        session.add(
            Device(
                id=device_id,
                external_id="device_synthetic",
                name="Dispositivo sintetico",
                device_type="rch" if parser_kind == "rch_observed" else "pos",
                parser_kind=parser_kind,
                listen_ip="192.0.2.10",
                listen_port=9100,
                target_ip="192.0.2.20",
                target_port=9100,
            )
        )
        session.flush()
        session.add(
            ProxySession(
                id=session_id,
                device_id=device_id,
                source_system="test",
                source_instance="synthetic",
                source_scope="device_synthetic",
                source_session_id="session-synthetic",
                listen_ip="192.0.2.10",
                listen_port=9100,
                target_ip="192.0.2.20",
                target_port=9100,
                started_at=NOW,
                status="CLOSED",
            )
        )
        session.flush()
        session.add(
            PrintJob(
                id=job_id,
                device_id=device_id,
                session_id=session_id,
                source_key=f"test:synthetic:{job_id}",
                source_system="test",
                source_instance="synthetic",
                source_scope="device_synthetic",
                source_job_id="job-synthetic",
                source_schema="test.v1",
                manifest_sha256="a" * 64,
                manifest_path="/spool/synthetic/manifest.json",
                started_at=NOW,
                captured_at=NOW,
                status="COMPLETE",
                capture_complete=True,
                timeline_complete=True,
                import_status="IMPORTED",
            )
        )
        session.flush()
        session.add(
            RawPayload(
                id=request_id,
                job_id=job_id,
                device_id=device_id,
                session_id=session_id,
                artifact_role="REQUEST_RAW",
                direction="CLIENT_TO_DEVICE",
                payload=request,
                byte_count=len(request),
                sha256=_sha(request),
                source_path="/spool/synthetic/client.raw",
                source_path_sha256=_sha(b"/spool/synthetic/client.raw"),
                complete=True,
                chain_scope=f"raw:{job_id}",
                chain_sequence=1,
                previous_record_hash="0" * 64,
                record_hash="1" * 64,
            )
        )
        session.add(
            RawPayload(
                id=response_id,
                job_id=job_id,
                device_id=device_id,
                session_id=session_id,
                artifact_role="RESPONSE_RAW",
                direction="DEVICE_TO_CLIENT",
                payload=response,
                byte_count=len(response),
                sha256=_sha(response),
                source_path="/spool/synthetic/device.raw",
                source_path_sha256=_sha(b"/spool/synthetic/device.raw"),
                complete=True,
                chain_scope=f"raw:{job_id}",
                chain_sequence=2,
                previous_record_hash="1" * 64,
                record_hash="2" * 64,
            )
        )
    return engine, factory, job_id, request_id, response_id


def test_parser_worker_is_idempotent_and_links_response_to_response_raw() -> None:
    request = b"".join(
        (
            _rch_frame("=K", sequence="0"),
            _rch_frame("=R7/$5000/*1/(VOCE SINTETICA)", sequence="1"),
            _rch_frame("=T1/$5000", sequence="2"),
            _rch_frame("<</?s", sequence="3"),
            _rch_frame("<</?7", sequence="4"),
        )
    )
    response = b"\x06" + _rch_frame(
        "s000000RE7001", address="01", frame_class="N", sequence="1"
    )
    engine, factory, _job_id, request_id, response_id = _database(
        parser_kind="rch_observed", request=request, response=response
    )
    repository = SqlAlchemyParserRepository(factory)
    first = ParserWorker(repository).run_once()
    second = ParserWorker(repository).run_once()
    assert (first.parsed_jobs, first.parsed_documents, first.failed) == (1, 2, 0)
    assert second.discovered == 0

    with factory() as session:
        documents = session.scalars(select(Document).order_by(Document.document_type)).all()
        assert {document.document_type for document in documents} == {
            DocumentType.COMMERCIAL_DOCUMENT.value,
            DocumentType.DEVICE_RESPONSE.value,
        }
        versions = session.scalars(select(DocumentVersion)).all()
        by_document = {version.document_id: version for version in versions}
        response_document = next(
            document
            for document in documents
            if document.document_type == DocumentType.DEVICE_RESPONSE.value
        )
        commercial_document = next(
            document
            for document in documents
            if document.document_type == DocumentType.COMMERCIAL_DOCUMENT.value
        )
        assert by_document[response_document.id].raw_payload_id == response_id
        assert by_document[response_document.id].source_path.endswith("device.raw")
        assert by_document[commercial_document.id].raw_payload_id == request_id
    engine.dispose()


def test_reparse_appends_version_to_stable_document_identity() -> None:
    payload = b"PRECONTO N. PB-X\nTOTALE 10,00\n\x1dV\x00"
    engine, factory, job_id, _request_id, _response_id = _database(
        parser_kind="escpos", request=payload
    )
    repository = SqlAlchemyParserRepository(factory)
    parsed = parse_escpos(
        payload,
        device_id="device_synthetic",
        session_id="session-synthetic",
        job_id="job-synthetic",
        captured_at=NOW,
        manifest_sha256="a" * 64,
        source_path="/spool/synthetic/client.raw",
    )
    assert repository.store_documents(str(job_id), parsed) == 1
    revised = tuple(
        document.model_copy(
            update={
                "parser_version": "2.0.0",
                "type": DocumentType.ORDER_CHANGE,
                "subtype": "ORDER_CHANGE_REPARSED",
                "external_document_code": "DOC-V2",
                "external_document_code_suffix": "0042",
                "commercial_reference_code": "FISCAL-V1",
                "order_code": "ORDER-V2",
                "table_code": "TABLE-V2",
                "operator_code": "OP-V2",
                "terminal_code": "TERM-V2",
                "receipt_header": ReceiptHeader(
                    merchant_name="SYNTHETIC HOTEL",
                    legal_name="EXAMPLE LABS S.R.L.",
                    address_lines=("VIA ESEMPIO 4",),
                    vat_number="SYNTHVAT01",
                    evidence="RCH_PRINTED_HEADER",
                ),
                "application_timestamp": NOW,
                "rch_footer_timestamp": NOW - timedelta(minutes=2),
                "rch_serial_number": "99LAB123456",
                "document_timestamp": NOW,
                "lines": (
                    DomainLine(
                        sequence=1,
                        course_code="2",
                        description="Crudo e melone",
                        quantity=Decimal("-1"),
                        state="QUANTITY_DECREASE",
                        removed=False,
                    ),
                ),
            }
        )
        for document in parsed
    )
    assert repository.store_documents(str(job_id), revised) == 1

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        versions = session.scalars(
            select(DocumentVersion).order_by(DocumentVersion.version_sequence)
        ).all()
        assert [version.version_sequence for version in versions] == [1, 2]
        assert versions[1].previous_record_hash == versions[0].record_hash
        assert versions[0].chain_scope == versions[1].chain_scope
        assert versions[0].document_type == DocumentType.PRE_BILL.value
        assert versions[1].document_type == DocumentType.ORDER_CHANGE.value
        assert versions[1].subtype == "ORDER_CHANGE_REPARSED"
        assert versions[1].external_document_code == "DOC-V2"
        assert versions[1].external_document_code_suffix == "0042"
        assert versions[1].commercial_reference_code == "FISCAL-V1"
        assert versions[1].order_code == "ORDER-V2"
        assert versions[1].table_code == "TABLE-V2"
        assert versions[1].operator_code == "OP-V2"
        assert versions[1].terminal_code == "TERM-V2"
        assert versions[1].receipt_header == {
            "schema_version": 1,
            "merchant_name": "SYNTHETIC HOTEL",
            "legal_name": "EXAMPLE LABS S.R.L.",
            "address_lines": ["VIA ESEMPIO 4"],
            "phone": None,
            "tax_code": None,
            "vat_number": "SYNTHVAT01",
            "evidence": "RCH_PRINTED_HEADER",
        }
        assert versions[1].application_timestamp == NOW
        assert versions[1].rch_footer_timestamp == NOW - timedelta(minutes=2)
        assert versions[1].rch_serial_number == "99LAB123456"
        projection = session.scalar(select(Document))
        assert projection is not None
        assert projection.document_type == DocumentType.ORDER_CHANGE.value
        assert projection.external_document_code == "DOC-V2"
        assert projection.external_document_code_suffix == "0042"
        assert projection.commercial_reference_code == "FISCAL-V1"
        assert projection.table_code == "TABLE-V2"
        assert projection.receipt_header == versions[1].receipt_header
        assert projection.application_timestamp == NOW
        assert projection.rch_footer_timestamp == NOW - timedelta(minutes=2)
        assert projection.rch_serial_number == "99LAB123456"
        revised_line = session.scalar(
            select(DocumentLine).where(DocumentLine.document_version_id == versions[1].id)
        )
        assert revised_line is not None
        assert revised_line.course_code == "2"
        assert revised_line.quantity == Decimal("-1.0000")
        assert revised_line.line_state == "QUANTITY_DECREASE"
        assert revised_line.removed is False
        selected = load_latest_documents(session, document_ids={projection.id})
        assert len(selected) == 1
        assert selected[0].value.lines[0].course_code == "2"
        assert selected[0].value.lines[0].quantity == Decimal("-1.0000")
    assert repository.pending_jobs(limit=10) == ()
    assert repository.pending_jobs(limit=10, reparse=True) == ()
    engine.dispose()


def test_parser_failures_back_off_and_eventually_require_explicit_retry() -> None:
    engine, factory, job_id, _request_id, _response_id = _database(
        parser_kind="unsupported", request=b"synthetic"
    )
    repository = SqlAlchemyParserRepository(factory)
    first = ParserWorker(repository).run_once()
    assert first.failed == 1
    assert repository.pending_jobs(limit=10) == ()
    for attempt in range(2, 9):
        repository.record_failure(str(job_id), f"synthetic failure {attempt}")
    with factory() as session:
        job = session.get(PrintJob, job_id)
        assert job is not None and job.import_status == "PARSE_FAILED"
    assert repository.pending_jobs(limit=10) == ()
    assert repository.pending_jobs(limit=10, reparse=True) == (str(job_id),)
    engine.dispose()


def test_parser_build_identity_includes_optional_runtime_fingerprint(monkeypatch) -> None:
    monkeypatch.setattr(
        escpos_parser,
        "PARSER_RUNTIME_FINGERPRINT",
        "ocr-runtime-a",
        raising=False,
    )
    first = parser_repository._parser_build_sha256(escpos_parser.PARSER_NAME)
    monkeypatch.setattr(
        escpos_parser,
        "PARSER_RUNTIME_FINGERPRINT",
        lambda: b"ocr-runtime-b",
    )
    second = parser_repository._parser_build_sha256(escpos_parser.PARSER_NAME)
    assert first != second
    assert len(first) == len(second) == 64


def test_new_complete_pos_command_queues_before_full_parse_and_store(monkeypatch) -> None:
    payload = (
        b"\x1b@COMANDA N. C-78\nTavolo: T-9\n"
        b"Piatto sintetico 50,00\nTOTALE 50,00\n\x1dV\x00"
    )
    engine, factory, _job_id, _request_id, _response_id = _database(
        parser_kind="escpos", request=payload
    )
    events: list[str] = []
    beeper = _RecordingBeeper(events)
    repository = SqlAlchemyParserRepository(factory)
    parse = parser_worker.parse_escpos

    def tracked_parse(*args, **kwargs):
        events.append("parse")
        return parse(*args, **kwargs)

    monkeypatch.setattr(parser_worker, "parse_escpos", tracked_parse)

    first = ParserWorker(repository, beeper=beeper).run_once()
    second = ParserWorker(repository, beeper=beeper).run_once()

    assert (first.parsed_jobs, first.parsed_documents, first.failed) == (1, 1, 0)
    assert second.discovered == 0
    assert beeper.device_ids == ["device_synthetic"]
    assert events == ["beep", "parse"]
    engine.dispose()


def test_pos_prebill_and_explicit_reparse_never_queue_beeper() -> None:
    prebill = b"\x1b@PRECONTO N. PB-1\nTOTALE 10,00\n\x1dV\x00"
    engine, factory, _job_id, _request_id, _response_id = _database(
        parser_kind="escpos", request=prebill
    )
    beeper = _RecordingBeeper()
    ParserWorker(SqlAlchemyParserRepository(factory), beeper=beeper).run_once()
    assert beeper.device_ids == []
    engine.dispose()

    incomplete_command = b"\x1b@COMANDA N. C-PARTIAL\nPiatto 10,00\n"
    engine, factory, _job_id, _request_id, _response_id = _database(
        parser_kind="escpos", request=incomplete_command
    )
    ParserWorker(SqlAlchemyParserRepository(factory), beeper=beeper).run_once()
    assert beeper.device_ids == []
    engine.dispose()

    command = b"\x1b@COMANDA N. C-79\nPiatto 10,00\n\x1dV\x00"
    engine, factory, _job_id, _request_id, _response_id = _database(
        parser_kind="escpos", request=command
    )
    ParserWorker(SqlAlchemyParserRepository(factory), beeper=beeper).run_once(reparse=True)
    assert beeper.device_ids == []
    engine.dispose()

    retry = b"\x1b@COMANDA N. C-RETRY\nPiatto 10,00\n\x1dV\x00"
    engine, factory, job_id, _request_id, _response_id = _database(
        parser_kind="escpos", request=retry
    )
    with factory.begin() as session:
        session.get(PrintJob, job_id).import_status = "PARSE_RETRY"
    ParserWorker(SqlAlchemyParserRepository(factory), beeper=beeper).run_once()
    assert beeper.device_ids == []
    engine.dispose()


def test_complete_rch_command_never_queues_pos_beeper() -> None:
    request = b"".join(
        (
            _rch_frame("=o", sequence="0"),
            _rch_frame('="/(COMANDA N. RCH-1)', sequence="1"),
            _rch_frame("=o", sequence="2"),
        )
    )
    engine, factory, _job_id, _request_id, _response_id = _database(
        parser_kind="rch_observed", request=request
    )
    beeper = _RecordingBeeper()
    report = ParserWorker(SqlAlchemyParserRepository(factory), beeper=beeper).run_once()

    assert (report.parsed_jobs, report.parsed_documents, report.failed) == (1, 1, 0)
    assert beeper.device_ids == []
    engine.dispose()
