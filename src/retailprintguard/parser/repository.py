"""Transactional, append-only parser repository."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from retailprintguard.common.config import Settings
from retailprintguard.common.domain import NormalizedDocument
from retailprintguard.common.hashchain import ZERO_HASH, chained_hash
from retailprintguard.db.models import (
    Device,
    Document,
    DocumentLine,
    DocumentVersion,
    ParserVersion,
    Payment,
    PrintJob,
    ProxySession,
    RawPayload,
    SystemEvent,
)
from retailprintguard.db.session import create_db_engine, session_factory
from retailprintguard.parser import escpos as escpos_parser
from retailprintguard.parser import rch as rch_parser


class ParserRepositoryError(RuntimeError):
    """A parser transaction could not complete."""


class SqlAlchemyParserRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    @classmethod
    def from_url(cls, database_url: str) -> SqlAlchemyParserRepository:
        return cls(session_factory(create_db_engine(database_url)))

    def pending_jobs(self, *, limit: int, reparse: bool = False) -> tuple[str, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError("parser batch limit must be between 1 and 10000")
        session = self._factory()
        try:
            esc_hash = _parser_build_sha256(escpos_parser.PARSER_NAME)
            rch_hash = _parser_build_sha256(rch_parser.PARSER_NAME)
            statuses = (
                ("IMPORTED", "PARSE_RETRY", "PARSED", "PARSE_EMPTY", "PARSE_FAILED")
                if reparse
                else ("IMPORTED", "PARSE_RETRY")
            )
            document_exists = (
                select(Document.id).where(Document.job_id == PrintJob.id).exists()
            )
            raw_exists = (
                select(RawPayload.id)
                .where(
                    RawPayload.job_id == PrintJob.id,
                    RawPayload.artifact_role == "REQUEST_RAW",
                )
                .exists()
            )
            esc_version_exists = (
                select(DocumentVersion.id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(ParserVersion, ParserVersion.id == DocumentVersion.parser_version_id)
                .where(
                    Document.job_id == PrintJob.id,
                    ParserVersion.name == escpos_parser.PARSER_NAME,
                    ParserVersion.build_sha256 == esc_hash,
                )
                .exists()
            )
            rch_version_exists = (
                select(DocumentVersion.id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(ParserVersion, ParserVersion.id == DocumentVersion.parser_version_id)
                .where(
                    Document.job_id == PrintJob.id,
                    ParserVersion.name == rch_parser.PARSER_NAME,
                    ParserVersion.build_sha256 == rch_hash,
                )
                .exists()
            )
            empty_exists = (
                select(SystemEvent.id)
                .where(
                    SystemEvent.job_id == PrintJob.id,
                    SystemEvent.service == "parser",
                    SystemEvent.event_type == "PARSER_NO_DOCUMENTS",
                    SystemEvent.correlation_id.in_((esc_hash, rch_hash)),
                )
                .exists()
            )
            current_processed = or_(
                and_(Device.parser_kind == "escpos", esc_version_exists),
                and_(Device.parser_kind == "rch_observed", rch_version_exists),
                empty_exists,
            )
            statement = (
                select(PrintJob.id, Device.parser_kind)
                .join(Device, Device.id == PrintJob.device_id)
                .where(PrintJob.import_status.in_(statuses), raw_exists)
                .order_by(PrintJob.captured_at, PrintJob.id)
            )
            statement = statement.where(
                ~current_processed if reparse else ~document_exists
            ).limit(limit if reparse else min(10_000, limit * 8))
            candidates = session.execute(statement).all()
            rows = [value for value, _parser_kind in candidates]
            retry_rows = session.execute(
                select(
                    SystemEvent.job_id,
                    func.count(SystemEvent.id),
                    func.max(SystemEvent.occurred_at),
                )
                .where(
                    SystemEvent.job_id.in_(rows),
                    SystemEvent.service == "parser",
                    SystemEvent.event_type == "PARSER_FAILED",
                    SystemEvent.correlation_id.in_((esc_hash, rch_hash)),
                )
                .group_by(SystemEvent.job_id)
            ).all()
            retry_state = {
                job: (int(attempts), last_failure)
                for job, attempts, last_failure in retry_rows
            }
            now = datetime.now(UTC)
            selected: list[str] = []
            for value in rows:
                attempts, last_failure = retry_state.get(value, (0, None))
                # An explicit historical reparse is an operator recovery action:
                # it must be able to retry a quarantined PARSE_FAILED job now.
                # Automatic polling still observes the exponential backoff.
                if last_failure is not None and not reparse:
                    delay = min(3600, 5 * (2 ** min(max(0, attempts - 1), 10)))
                    if now < last_failure + timedelta(seconds=delay):
                        continue
                selected.append(str(value))
                if len(selected) == limit:
                    break
            return tuple(selected)
        except SQLAlchemyError as exc:
            raise ParserRepositoryError("cannot query pending parser jobs") from exc
        finally:
            session.close()

    def load_job(self, job_id: str) -> dict[str, Any] | None:
        session = self._factory()
        try:
            job = session.get(PrintJob, job_id)
            if job is None:
                return None
            device = session.get(Device, job.device_id)
            proxy_session = session.get(ProxySession, job.session_id)
            if device is None or proxy_session is None:
                raise ParserRepositoryError("parser job has missing device/session evidence")
            payloads = session.scalars(
                select(RawPayload)
                .where(RawPayload.job_id == job.id)
                .order_by(RawPayload.chain_sequence)
            ).all()
            by_role = {payload.artifact_role: payload for payload in payloads}
            request = by_role.get("REQUEST_RAW")
            response = by_role.get("RESPONSE_RAW")
            if request is None:
                raise ParserRepositoryError("parser job has no authoritative request RAW")
            if hashlib.sha256(request.payload).hexdigest() != request.sha256:
                raise ParserRepositoryError("request RAW hash differs from imported evidence")
            if (
                response is not None
                and hashlib.sha256(response.payload).hexdigest() != response.sha256
            ):
                raise ParserRepositoryError("response RAW hash differs from imported evidence")
            return {
                "job_id": str(job.id),
                "source_job_id": job.source_job_id,
                "session_id": proxy_session.source_session_id,
                "device_id": device.external_id,
                "parser_kind": device.parser_kind.value
                if hasattr(device.parser_kind, "value")
                else str(device.parser_kind),
                "captured_at": job.captured_at,
                "manifest_sha256": job.manifest_sha256,
                "request_path": request.source_path,
                "response_path": response.source_path if response is not None else None,
                "request": bytes(request.payload),
                "response": b"" if response is None else bytes(response.payload),
                "request_payload_id": request.id,
                "import_status": job.import_status,
            }
        except SQLAlchemyError as exc:
            raise ParserRepositoryError("cannot load parser job") from exc
        finally:
            session.close()

    def store_documents(
        self,
        job_id: str,
        documents: tuple[NormalizedDocument, ...],
    ) -> int:
        session = self._factory()
        try:
            with session.begin():
                job = session.get(PrintJob, job_id)
                if job is None:
                    raise ParserRepositoryError("parser job disappeared")
                device = session.get(Device, job.device_id)
                if device is None:
                    raise ParserRepositoryError("parser device disappeared")
                request_raw = session.scalar(
                    select(RawPayload).where(
                        RawPayload.job_id == job.id,
                        RawPayload.artifact_role == "REQUEST_RAW",
                    )
                )
                inserted = 0
                raw_payloads = session.scalars(
                    select(RawPayload).where(RawPayload.job_id == job.id)
                ).all()
                raw_by_sha = {payload.sha256: payload for payload in raw_payloads}
                for parsed in documents:
                    source_key = str(parsed.id)
                    document = session.scalar(
                        select(Document).where(
                            Document.job_id == job.id,
                            Document.source_document_key == source_key,
                        )
                    )
                    if document is None:
                        document = Document(
                            device_id=job.device_id,
                            session_id=job.session_id,
                            job_id=job.id,
                            source_document_key=source_key,
                            document_type=parsed.type.value,
                            subtype=parsed.subtype,
                            external_document_code=parsed.external_document_code,
                            external_document_code_suffix=(
                                parsed.external_document_code_suffix
                            ),
                            commercial_reference_code=parsed.commercial_reference_code,
                            order_code=parsed.order_code,
                            table_code=parsed.table_code,
                            operator_code=parsed.operator_code,
                            terminal_code=parsed.terminal_code,
                            document_timestamp=parsed.document_timestamp,
                            captured_at=parsed.captured_at,
                        )
                        session.add(document)
                        session.flush()
                    parser_hash = _parser_build_sha256(parsed.parser_name)
                    parser = session.scalar(
                        select(ParserVersion).where(
                            ParserVersion.name == parsed.parser_name,
                            ParserVersion.version == parsed.parser_version,
                            ParserVersion.build_sha256 == parser_hash,
                        )
                    )
                    if parser is None:
                        parser = ParserVersion(
                            name=parsed.parser_name,
                            version=parsed.parser_version,
                            build_sha256=parser_hash,
                            protocol=device.parser_kind.value
                            if hasattr(device.parser_kind, "value")
                            else str(device.parser_kind),
                            configuration={"evidence_policy": "bounded-sidecar"},
                        )
                        session.add(parser)
                        session.flush()
                    existing = session.scalar(
                        select(DocumentVersion).where(
                            DocumentVersion.document_id == document.id,
                            DocumentVersion.parser_version_id == parser.id,
                            DocumentVersion.source_payload_sha256 == parsed.source_payload_sha256,
                        )
                    )
                    if existing is not None:
                        continue
                    chain_scope = f"document:{document.id}"
                    previous_hash = (
                        session.scalar(
                            select(DocumentVersion.record_hash)
                            .where(DocumentVersion.chain_scope == chain_scope)
                            .order_by(DocumentVersion.chain_sequence.desc())
                            .limit(1)
                        )
                        or ZERO_HASH
                    )
                    next_chain = (
                        int(
                            session.scalar(
                                select(func.max(DocumentVersion.chain_sequence)).where(
                                    DocumentVersion.chain_scope == chain_scope
                                )
                            )
                            or 0
                        )
                        + 1
                    )
                    version_sequence = (
                        int(
                            session.scalar(
                                select(func.max(DocumentVersion.version_sequence)).where(
                                    DocumentVersion.document_id == document.id
                                )
                            )
                            or 0
                        )
                        + 1
                    )
                    version_payload = {
                        "document_id": str(document.id),
                        "version_sequence": version_sequence,
                        "parser": f"{parsed.parser_name}:{parsed.parser_version}",
                        "source_payload_sha256": parsed.source_payload_sha256,
                        "normalized": parsed.model_dump(mode="json"),
                        "previous_hash": previous_hash,
                    }
                    record_hash = chained_hash(version_payload, previous_hash)
                    source_raw = raw_by_sha.get(parsed.source_payload_sha256) or request_raw
                    version = DocumentVersion(
                        document_id=document.id,
                        parser_version_id=parser.id,
                        raw_payload_id=source_raw.id if source_raw is not None else None,
                        version_sequence=version_sequence,
                        document_type=parsed.type.value,
                        subtype=parsed.subtype,
                        external_document_code=parsed.external_document_code,
                        external_document_code_suffix=(
                            parsed.external_document_code_suffix
                        ),
                        commercial_reference_code=parsed.commercial_reference_code,
                        order_code=parsed.order_code,
                        table_code=parsed.table_code,
                        operator_code=parsed.operator_code,
                        terminal_code=parsed.terminal_code,
                        document_timestamp=parsed.document_timestamp,
                        gross_total=parsed.gross_total,
                        net_total=parsed.net_total,
                        discount_total=parsed.discount_total,
                        tax_total=parsed.tax_total,
                        payment_method=(parsed.payments[0].method if parsed.payments else None),
                        status=parsed.status,
                        normalized_text=parsed.normalized_text,
                        encoding=parsed.encoding,
                        parse_confidence=parsed.parse_confidence,
                        evidence_level=parsed.evidence.value,
                        source_manifest_sha256=parsed.source_manifest_sha256,
                        source_payload_sha256=parsed.source_payload_sha256,
                        source_path=parsed.source_path,
                        complete=parsed.complete,
                        warnings=list(parsed.warnings),
                        errors=[],
                        raw_metadata=parsed.raw_metadata,
                        chain_scope=chain_scope,
                        chain_sequence=next_chain,
                        previous_record_hash=previous_hash,
                        record_hash=record_hash,
                    )
                    session.add(version)
                    session.flush()
                    # ``documents`` is a convenient current read projection.
                    # Historical parser semantics remain append-only in
                    # ``document_versions`` and are never overwritten here.
                    document.document_type = parsed.type.value
                    document.subtype = parsed.subtype
                    document.external_document_code = parsed.external_document_code
                    document.external_document_code_suffix = (
                        parsed.external_document_code_suffix
                    )
                    document.commercial_reference_code = parsed.commercial_reference_code
                    document.order_code = parsed.order_code
                    document.table_code = parsed.table_code
                    document.operator_code = parsed.operator_code
                    document.terminal_code = parsed.terminal_code
                    document.document_timestamp = parsed.document_timestamp
                    for line in parsed.lines:
                        source = line.source
                        session.add(
                            DocumentLine(
                                document_version_id=version.id,
                                sequence=line.sequence,
                                course_code=line.course_code,
                                item_code=line.item_code,
                                description=line.description,
                                quantity=line.quantity,
                                unit_price=line.unit_price,
                                original_unit_price=line.original_unit_price,
                                modified_unit_price=line.modified_unit_price,
                                discount=line.discount,
                                surcharge=line.surcharge,
                                tax_rate=line.tax_rate,
                                line_total=line.line_total,
                                line_state=line.state,
                                cancelled=line.cancelled,
                                removed=line.removed,
                                raw_text=line.raw_text,
                                source_direction=source.direction if source else None,
                                source_offset=source.offset if source else None,
                                source_length=source.length if source else None,
                                source_frame_id=source.frame_id if source else None,
                            )
                        )
                    for payment in parsed.payments:
                        session.add(
                            Payment(
                                document_version_id=version.id,
                                method=payment.method,
                                amount=payment.amount,
                                currency=payment.currency,
                                status="RECORDED",
                                evidence_level=payment.evidence.value,
                                paid_at=parsed.document_timestamp,
                            )
                        )
                    inserted += 1
                job.import_status = "PARSED" if documents else "PARSE_EMPTY"
                if not documents:
                    session.add(
                        SystemEvent(
                            service="parser",
                            device_id=job.device_id,
                            session_id=job.session_id,
                            job_id=job.id,
                            severity="WARNING",
                            event_type="PARSER_NO_DOCUMENTS",
                            correlation_id=_parser_build_sha256(
                                escpos_parser.PARSER_NAME
                                if str(device.parser_kind) == "escpos"
                                else rch_parser.PARSER_NAME
                            ),
                            message="Nessun documento semanticamente riconosciuto; RAW conservato",
                            details={},
                        )
                    )
                return inserted
        except IntegrityError as exc:
            raise ParserRepositoryError("parser output conflicts with existing evidence") from exc
        except SQLAlchemyError as exc:
            raise ParserRepositoryError("cannot store parsed documents") from exc
        finally:
            session.close()

    def record_failure(self, job_id: str, error: str) -> None:
        session = self._factory()
        try:
            with session.begin():
                job = session.get(PrintJob, job_id)
                if job is not None:
                    device = session.get(Device, job.device_id)
                    parser_name = (
                        escpos_parser.PARSER_NAME
                        if device is not None and str(device.parser_kind) == "escpos"
                        else rch_parser.PARSER_NAME
                    )
                    build_hash = _parser_build_sha256(parser_name)
                    attempts = int(
                        session.scalar(
                            select(func.count(SystemEvent.id)).where(
                                SystemEvent.job_id == job.id,
                                SystemEvent.service == "parser",
                                SystemEvent.event_type == "PARSER_FAILED",
                                SystemEvent.correlation_id == build_hash,
                            )
                        )
                        or 0
                    ) + 1
                    job.import_status = "PARSE_FAILED" if attempts >= 8 else "PARSE_RETRY"
                    session.add(
                        SystemEvent(
                            service="parser",
                            device_id=job.device_id,
                            session_id=job.session_id,
                            job_id=job.id,
                            severity="ERROR",
                            event_type="PARSER_FAILED",
                            correlation_id=build_hash,
                            message="Errore parser; il RAW originale resta disponibile",
                            details={
                                "error": error[:4096],
                                "attempt": attempts,
                                "retry_scheduled": attempts < 8,
                            },
                        )
                    )
        except SQLAlchemyError as exc:
            raise ParserRepositoryError("cannot record parser failure") from exc
        finally:
            session.close()


def create_parser_repository(settings: Settings) -> SqlAlchemyParserRepository:
    return SqlAlchemyParserRepository.from_url(settings.database_url().get_secret_value())


def _parser_build_sha256(parser_name: str) -> str:
    modules = {
        escpos_parser.PARSER_NAME: escpos_parser,
        rch_parser.PARSER_NAME: rch_parser,
    }
    module = modules.get(parser_name)
    if module is None or module.__file__ is None:
        raise ParserRepositoryError(f"unknown parser implementation: {parser_name}")
    implementation = Path(module.__file__).read_bytes()
    runtime_fingerprint: Any | None = None
    for attribute in (
        "PARSER_RUNTIME_FINGERPRINT",
        "RUNTIME_FINGERPRINT",
        "parser_runtime_fingerprint",
    ):
        if hasattr(module, attribute):
            runtime_fingerprint = getattr(module, attribute)
            break
    if callable(runtime_fingerprint):
        runtime_fingerprint = runtime_fingerprint()
    if runtime_fingerprint is None:
        # Preserve the historical identity for parsers without runtime assets.
        return hashlib.sha256(implementation, usedforsecurity=True).hexdigest()
    if isinstance(runtime_fingerprint, str):
        encoded_fingerprint = runtime_fingerprint.encode("utf-8")
    elif isinstance(runtime_fingerprint, bytes):
        encoded_fingerprint = runtime_fingerprint
    else:
        raise ParserRepositoryError("parser runtime fingerprint must be str or bytes")
    identity = b"retailprintguard-parser-build-v2\x00" + implementation
    identity += b"\x00runtime-fingerprint\x00" + encoded_fingerprint
    return hashlib.sha256(identity, usedforsecurity=True).hexdigest()


__all__ = [
    "ParserRepositoryError",
    "SqlAlchemyParserRepository",
    "create_parser_repository",
]
