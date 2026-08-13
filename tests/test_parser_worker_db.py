from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from retailprintguard.common.domain import DocumentType
from retailprintguard.db import Base, create_db_engine, session_factory
from retailprintguard.db.models import (
    Device,
    Document,
    DocumentVersion,
    PrintJob,
    ProxySession,
    RawPayload,
)
from retailprintguard.parser.escpos import parse_escpos
from retailprintguard.parser.repository import SqlAlchemyParserRepository
from retailprintguard.parser.worker import ParserWorker

NOW = datetime(2042, 7, 8, 12, 0, tzinfo=UTC)


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
    revised = tuple(document.model_copy(update={"parser_version": "2.0.0"}) for document in parsed)
    assert repository.store_documents(str(job_id), revised) == 1

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        versions = session.scalars(
            select(DocumentVersion).order_by(DocumentVersion.version_sequence)
        ).all()
        assert [version.version_sequence for version in versions] == [1, 2]
        assert versions[1].previous_record_hash == versions[0].record_hash
        assert versions[0].chain_scope == versions[1].chain_scope
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
