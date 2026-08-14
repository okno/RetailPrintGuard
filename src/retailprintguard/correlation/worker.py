"""Database-backed correlation and order-timeline worker.

The worker never participates in the proxy data path.  It consumes immutable
document versions already committed by ingestion and writes derived records in
one transaction.  Stable identifiers and unique constraints make a repeated
run idempotent while retaining results produced from newer parser versions.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from retailprintguard.common.domain import (
    DocumentLine as DomainLine,
)
from retailprintguard.common.domain import (
    DocumentType,
    EvidenceLevel,
    NormalizedDocument,
    OrderEventType,
    PaymentRecord,
    SourceSpan,
)
from retailprintguard.common.hashchain import ZERO_HASH, canonical_json, chained_hash
from retailprintguard.correlation.engine import (
    ALGORITHM_VERSION,
    CorrelatedTransaction,
    CorrelationEngine,
    LineChange,
    LineChangeType,
    apply_order_change_lines,
    compare_document_lines,
)
from retailprintguard.db.models import (
    ActiveParserVersion,
    AnalysisWatermark,
    AuditLog,
    Device,
    Document,
    DocumentCorrelation,
    DocumentCorrelationMember,
    DocumentLine,
    DocumentVersion,
    HashChainHead,
    Order,
    OrderEvent,
    OrderSnapshot,
    ParserVersion,
    Payment,
    PrintJob,
    ProxySession,
    SystemEvent,
)

_SOURCE_TYPES = {
    DocumentType.ORDER,
    DocumentType.ORDER_CHANGE,
    DocumentType.KITCHEN_ORDER,
    DocumentType.PRE_BILL,
    DocumentType.MANAGEMENT_DOCUMENT,
}
_FISCAL_TYPES = {DocumentType.COMMERCIAL_DOCUMENT, DocumentType.REFUND}


@dataclass(frozen=True, slots=True)
class LoadedDocument:
    value: NormalizedDocument
    version_id: UUID
    version_record_hash: str
    database_device_id: UUID
    database_job_id: UUID
    raw_payload_id: UUID | None


@dataclass(frozen=True, slots=True)
class CorrelationRunReport:
    documents_loaded: int
    transactions_evaluated: int
    correlations_inserted: int
    orders_created: int
    events_inserted: int
    snapshots_inserted: int


def _document_time(document: NormalizedDocument) -> datetime:
    return document.document_timestamp or document.captured_at


def _document_type(value: str) -> DocumentType:
    try:
        return DocumentType(value)
    except ValueError:
        return DocumentType.UNKNOWN


def _evidence(value: str) -> EvidenceLevel:
    try:
        return EvidenceLevel(value)
    except ValueError:
        return EvidenceLevel.UNKNOWN


def _versioned_semantic(
    version: DocumentVersion,
    legacy_projection: Document,
    attribute: str,
) -> Any:
    """Read immutable version data, falling back only for pre-migration rows."""

    version_value = getattr(version, attribute)
    return version_value if version_value is not None else getattr(legacy_projection, attribute)


def activate_parser_version(
    factory: sessionmaker[Session],
    *,
    parser_name: str,
    parser_version: str,
    build_sha256: str,
    reason: str,
    rewind: bool = True,
    actor_user_id: UUID | None = None,
) -> UUID:
    """Atomically select an installed parser build and optionally rewind analysis.

    Selection is by the exact immutable build identity, rather than by install
    order.  Consequently an intentional software rollback keeps using the older
    build until another explicit activation occurs.
    """

    parser_name = parser_name.strip()
    parser_version = parser_version.strip()
    reason = reason.strip()
    digest = build_sha256.strip().lower()
    if not parser_name or not parser_version or not reason:
        raise ValueError("parser name, version and activation reason are required")
    if len(parser_name) > 128 or len(parser_version) > 64 or len(reason) > 191:
        raise ValueError("parser name, version or activation reason exceeds its storage limit")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("parser build sha256 must contain exactly 64 hexadecimal characters")

    with factory.begin() as session:
        selected = session.scalar(
            select(ParserVersion).where(
                ParserVersion.name == parser_name,
                ParserVersion.version == parser_version,
                ParserVersion.build_sha256 == digest,
            )
        )
        if selected is None:
            raise ValueError("requested parser build is not installed")
        pointer = session.scalar(
            select(ActiveParserVersion)
            .where(ActiveParserVersion.parser_name == parser_name)
            .with_for_update()
        )
        previous_version_id = pointer.parser_version_id if pointer is not None else None
        activated_at = datetime.now(UTC)
        if pointer is None:
            session.add(
                ActiveParserVersion(
                    parser_name=parser_name,
                    parser_version_id=selected.id,
                    activated_at=activated_at,
                    activation_reason=reason,
                )
            )
        else:
            pointer.parser_version_id = selected.id
            pointer.activated_at = activated_at
            pointer.activation_reason = reason
        watermark = session.get(AnalysisWatermark, "correlation")
        if watermark is not None:
            if rewind:
                session.delete(watermark)
            else:
                active_rows = [
                    {"name": name, "parser_version_id": str(version_id)}
                    for name, version_id in session.execute(
                        select(
                            ActiveParserVersion.parser_name,
                            ActiveParserVersion.parser_version_id,
                        ).order_by(ActiveParserVersion.parser_name)
                    )
                ]
                metadata = dict(watermark.metadata_json or {})
                metadata["parser_activation_fingerprint"] = hashlib.sha256(
                    canonical_json(active_rows)
                ).hexdigest()
                metadata["parser_activation_no_rewind"] = {
                    "parser_name": parser_name,
                    "parser_version_id": str(selected.id),
                    "activated_at": activated_at.isoformat(),
                }
                watermark.metadata_json = metadata
                watermark.updated_at = activated_at
        details = {
            "parser_name": parser_name,
            "parser_version": parser_version,
            "build_sha256": digest,
            "previous_parser_version_id": (
                str(previous_version_id) if previous_version_id is not None else None
            ),
            "rewind": rewind,
            "reason": reason,
        }
        session.add(
            SystemEvent(
                service="correlation",
                severity="INFO",
                event_type="PARSER_ACTIVATED",
                message=f"Parser {parser_name} {parser_version} activated",
                occurred_at=activated_at,
                details=details,
            )
        )
        _append_parser_activation_audit(
            session,
            parser_version_id=selected.id,
            actor_user_id=actor_user_id,
            occurred_at=activated_at,
            details=details,
        )
        session.flush()
        return selected.id


def _append_parser_activation_audit(
    session: Session,
    *,
    parser_version_id: UUID,
    actor_user_id: UUID | None,
    occurred_at: datetime,
    details: dict[str, Any],
) -> None:
    scope = "audit:global"
    head = session.scalar(
        select(HashChainHead).where(HashChainHead.scope == scope).with_for_update()
    )
    if head is None:
        head = HashChainHead(scope=scope, sequence=0, record_hash=ZERO_HASH)
        session.add(head)
        session.flush()
    sequence = head.sequence + 1
    correlation_id = f"parser-activation:{parser_version_id}"
    payload = {
        "scope": scope,
        "sequence": sequence,
        "actor_id": str(actor_user_id) if actor_user_id else None,
        "action": "PARSER_ACTIVATED",
        "entity_type": "PARSER_VERSION",
        "entity_id": str(parser_version_id),
        "correlation_id": correlation_id,
        "occurred_at": occurred_at.isoformat(),
        "metadata": details,
        "previous_hash": head.record_hash,
    }
    record_hash = chained_hash(payload, head.record_hash)
    session.add(
        AuditLog(
            chain_scope=scope,
            sequence=sequence,
            actor_user_id=actor_user_id,
            event_type="PARSER_ACTIVATED",
            resource_type="PARSER_VERSION",
            resource_id=str(parser_version_id),
            correlation_id=correlation_id,
            occurred_at=occurred_at,
            details=details,
            previous_record_hash=head.record_hash,
            record_hash=record_hash,
        )
    )
    head.sequence = sequence
    head.record_hash = record_hash
    head.updated_at = occurred_at


def _latest_version_id() -> Any:
    return (
        select(DocumentVersion.id)
        .join(ParserVersion, ParserVersion.id == DocumentVersion.parser_version_id)
        .outerjoin(
            ActiveParserVersion,
            ActiveParserVersion.parser_name == ParserVersion.name,
        )
        .where(DocumentVersion.document_id == Document.id)
        .order_by(
            (ActiveParserVersion.parser_version_id == DocumentVersion.parser_version_id).desc(),
            DocumentVersion.version_sequence.desc(),
            DocumentVersion.id.desc(),
        )
        .limit(1)
        .correlate(Document)
        .scalar_subquery()
    )


def _document_query(document_ids: set[UUID] | None = None) -> Select[Any]:
    statement = (
        select(Document, DocumentVersion, Device, ProxySession, PrintJob, ParserVersion)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Device, Device.id == Document.device_id)
        .join(ProxySession, ProxySession.id == Document.session_id)
        .join(PrintJob, PrintJob.id == Document.job_id)
        .join(ParserVersion, ParserVersion.id == DocumentVersion.parser_version_id)
        .where(DocumentVersion.id == _latest_version_id())
        .order_by(Document.captured_at.desc(), Document.id.desc())
    )
    if document_ids is not None:
        if not document_ids:
            return statement.where(False)
        statement = statement.where(Document.id.in_(document_ids))
    return statement


def load_latest_documents(
    session: Session,
    *,
    limit: int | None = None,
    document_ids: set[UUID] | None = None,
) -> tuple[LoadedDocument, ...]:
    """Load protocol-neutral domain objects from the latest parser version."""

    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    statement = _document_query(document_ids)
    if limit is not None:
        statement = statement.limit(limit)
    rows = session.execute(statement).all()
    loaded: list[LoadedDocument] = []
    for document, version, device, proxy_session, job, parser in rows:
        stored_lines = session.scalars(
            select(DocumentLine)
            .where(DocumentLine.document_version_id == version.id)
            .order_by(DocumentLine.sequence, DocumentLine.id)
        ).all()
        lines: list[DomainLine] = []
        for line in stored_lines:
            source = None
            if (
                line.source_direction is not None
                and line.source_offset is not None
                and line.source_length is not None
            ):
                source = SourceSpan(
                    direction=line.source_direction,
                    offset=line.source_offset,
                    length=line.source_length,
                    frame_id=line.source_frame_id,
                )
            lines.append(
                DomainLine(
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
                    state=line.line_state,
                    cancelled=line.cancelled,
                    removed=line.removed,
                    raw_text=line.raw_text,
                    source=source,
                )
            )
        stored_payments = session.scalars(
            select(Payment)
            .where(Payment.document_version_id == version.id)
            .order_by(Payment.created_at, Payment.id)
        ).all()
        payments = tuple(
            PaymentRecord(
                method=payment.method,
                amount=payment.amount,
                currency=payment.currency,
                evidence=_evidence(payment.evidence_level),
            )
            for payment in stored_payments
        )
        metadata = version.raw_metadata if isinstance(version.raw_metadata, dict) else {}
        warnings = tuple(str(item) for item in (version.warnings or []))
        value = NormalizedDocument(
            id=document.id,
            source_device_id=device.external_id,
            source_session_id=proxy_session.source_session_id,
            source_job_id=job.source_job_id,
            type=_document_type(_versioned_semantic(version, document, "document_type")),
            subtype=_versioned_semantic(version, document, "subtype") or "UNKNOWN",
            external_document_code=_versioned_semantic(version, document, "external_document_code"),
            order_code=_versioned_semantic(version, document, "order_code"),
            table_code=_versioned_semantic(version, document, "table_code"),
            operator_code=_versioned_semantic(version, document, "operator_code"),
            terminal_code=_versioned_semantic(version, document, "terminal_code"),
            document_timestamp=_versioned_semantic(version, document, "document_timestamp"),
            captured_at=document.captured_at,
            gross_total=version.gross_total,
            net_total=version.net_total,
            discount_total=version.discount_total,
            tax_total=version.tax_total,
            status=version.status,
            normalized_text=version.normalized_text,
            encoding=version.encoding,
            parser_name=parser.name,
            parser_version=parser.version,
            parse_confidence=version.parse_confidence,
            evidence=_evidence(version.evidence_level),
            source_manifest_sha256=version.source_manifest_sha256,
            source_payload_sha256=version.source_payload_sha256,
            source_path=version.source_path,
            complete=version.complete,
            warnings=warnings,
            lines=tuple(lines),
            payments=payments,
            raw_metadata=metadata,
        )
        loaded.append(
            LoadedDocument(
                value=value,
                version_id=version.id,
                version_record_hash=version.record_hash,
                database_device_id=document.device_id,
                database_job_id=document.job_id,
                raw_payload_id=version.raw_payload_id,
            )
        )
    loaded.sort(key=lambda item: (_document_time(item.value), str(item.value.id)))
    return tuple(loaded)


def _candidate_batch(
    session: Session,
    *,
    limit: int,
    lookback_seconds: int,
) -> tuple[tuple[LoadedDocument, ...], set[UUID]]:
    """Return new/late seeds plus an indexed, bounded candidate neighbourhood."""

    watermark = session.scalar(
        select(AnalysisWatermark)
        .where(AnalysisWatermark.service == "correlation")
        .with_for_update()
    )
    active_parsers = [
        {"name": name, "parser_version_id": str(version_id)}
        for name, version_id in session.execute(
            select(
                ActiveParserVersion.parser_name,
                ActiveParserVersion.parser_version_id,
            ).order_by(ActiveParserVersion.parser_name)
        )
    ]
    activation_fingerprint = hashlib.sha256(canonical_json(active_parsers)).hexdigest()
    activation_changed = (
        watermark is not None
        and (watermark.metadata_json or {}).get("parser_activation_fingerprint")
        != activation_fingerprint
    )
    cursor = (
        None
        if watermark is None or activation_changed
        else watermark.cursor_timestamp - timedelta(seconds=lookback_seconds)
    )
    seed_statement = _document_query().order_by(None)
    if cursor is not None:
        seed_statement = seed_statement.where(
            DocumentVersion.parsed_at >= cursor,
            or_(
                DocumentVersion.parsed_at > watermark.cursor_timestamp,
                and_(
                    DocumentVersion.parsed_at == watermark.cursor_timestamp,
                    DocumentVersion.id > watermark.cursor_id,
                ),
                ~Document.id.in_(
                    select(DocumentCorrelationMember.document_id)
                    .join(
                        DocumentCorrelation,
                        DocumentCorrelation.id == DocumentCorrelationMember.correlation_id,
                    )
                    .where(
                        DocumentCorrelation.algorithm_version == ALGORITHM_VERSION,
                        DocumentCorrelation.status.in_(("AUTOMATIC", "UNCORRELATED")),
                    )
                ),
            ),
        )
    seed_statement = seed_statement.order_by(DocumentVersion.parsed_at, DocumentVersion.id).limit(
        limit
    )
    seed_rows = session.execute(seed_statement).all()
    if not seed_rows:
        return (), set()
    seed_ids = {row[0].id for row in seed_rows}
    loaded_seeds = load_latest_documents(session, document_ids=seed_ids)
    earliest = min(_document_time(item.value) for item in loaded_seeds)
    latest = max(_document_time(item.value) for item in loaded_seeds)
    window = timedelta(seconds=lookback_seconds)
    order_codes = {item.value.order_code for item in loaded_seeds if item.value.order_code}
    external_codes = {
        item.value.external_document_code
        for item in loaded_seeds
        if item.value.external_document_code
    }
    table_codes = {item.value.table_code for item in loaded_seeds if item.value.table_code}
    session_ids = {
        item.value.source_session_id for item in loaded_seeds if item.value.source_session_id
    }
    source_job_ids = {item.value.source_job_id for item in loaded_seeds if item.value.source_job_id}
    strong_blocks = []
    if order_codes:
        strong_blocks.append(
            or_(
                DocumentVersion.order_code.in_(order_codes),
                and_(
                    DocumentVersion.order_code.is_(None),
                    Document.order_code.in_(order_codes),
                ),
            )
        )
    if external_codes:
        strong_blocks.append(
            or_(
                DocumentVersion.external_document_code.in_(external_codes),
                and_(
                    DocumentVersion.external_document_code.is_(None),
                    Document.external_document_code.in_(external_codes),
                ),
            )
        )
    if table_codes:
        strong_blocks.append(
            or_(
                DocumentVersion.table_code.in_(table_codes),
                and_(
                    DocumentVersion.table_code.is_(None),
                    Document.table_code.in_(table_codes),
                ),
            )
        )
    if session_ids:
        strong_blocks.append(ProxySession.source_session_id.in_(session_ids))
    if source_job_ids:
        strong_blocks.append(PrintJob.source_job_id.in_(source_job_ids))
    candidate_statement = _document_query().where(
        Document.captured_at >= earliest - window,
        Document.captured_at <= latest + window,
        or_(Document.id.in_(seed_ids), *strong_blocks),
    )
    candidate_ids = {row[0].id for row in session.execute(candidate_statement)}
    loaded = load_latest_documents(session, document_ids=candidate_ids)
    latest_seed_row = max(seed_rows, key=lambda row: (row[1].parsed_at, str(row[1].id)))
    latest_version: DocumentVersion = latest_seed_row[1]
    if watermark is None:
        session.add(
            AnalysisWatermark(
                service="correlation",
                cursor_timestamp=latest_version.parsed_at,
                cursor_id=latest_version.id,
                processed_count=len(seed_ids),
                metadata_json={
                    "lookback_seconds": lookback_seconds,
                    "parser_activation_fingerprint": activation_fingerprint,
                },
            )
        )
    elif activation_changed or (latest_version.parsed_at, str(latest_version.id)) > (
        watermark.cursor_timestamp,
        str(watermark.cursor_id),
    ):
        watermark.cursor_timestamp = latest_version.parsed_at
        watermark.cursor_id = latest_version.id
        watermark.processed_count += len(seed_ids)
        watermark.metadata_json = {
            "lookback_seconds": lookback_seconds,
            "parser_activation_fingerprint": activation_fingerprint,
        }
    return loaded, seed_ids


def _candidate_pairs(
    loaded: tuple[LoadedDocument, ...], seed_ids: set[UUID], window_seconds: int
) -> set[tuple[UUID, UUID]]:
    """Block in memory on exact keys after the indexed SQL neighbourhood query."""

    result: set[tuple[UUID, UUID]] = set()
    blocks: dict[tuple[str, str], list[NormalizedDocument]] = defaultdict(list)
    for item in loaded:
        document = item.value
        for name, value in (
            ("order", document.order_code),
            ("external", document.external_document_code),
            ("table", document.table_code),
            ("session", document.source_session_id),
            ("job", document.source_job_id),
        ):
            if value:
                blocks[(name, value)].append(document)
    for documents in blocks.values():
        for index, left in enumerate(documents):
            for right in documents[index + 1 :]:
                if left.id not in seed_ids and right.id not in seed_ids:
                    continue
                if (
                    abs((_document_time(right) - _document_time(left)).total_seconds())
                    <= window_seconds
                ):
                    result.add((left.id, right.id))
    return result


def correlation_input_fingerprint(
    documents: tuple[NormalizedDocument, ...],
    loaded_by_id: dict[UUID, LoadedDocument],
) -> str:
    evidence = [
        {
            "document_id": str(document.id),
            "version_id": str(loaded_by_id[document.id].version_id),
            "record_hash": loaded_by_id[document.id].version_record_hash,
        }
        for document in sorted(documents, key=lambda item: str(item.id))
    ]
    return hashlib.sha256(canonical_json(evidence)).hexdigest()


def _member_role(document: NormalizedDocument) -> str:
    if document.type in _SOURCE_TYPES:
        return "SOURCE"
    if document.type in _FISCAL_TYPES:
        return "FISCAL"
    if document.type is DocumentType.PAYMENT:
        return "PAYMENT"
    if document.type is DocumentType.DEVICE_RESPONSE:
        return "RESPONSE"
    return "AUXILIARY"


def _line_change_event(change: LineChange) -> OrderEventType | None:
    return {
        LineChangeType.ADDED: OrderEventType.ITEM_ADDED,
        LineChangeType.REMOVED: OrderEventType.ITEM_REMOVED,
        LineChangeType.QUANTITY_CHANGED: OrderEventType.QUANTITY_CHANGED,
        LineChangeType.PRICE_CHANGED: OrderEventType.PRICE_CHANGED,
        LineChangeType.DISCOUNT_CHANGED: (
            OrderEventType.DISCOUNT_REMOVED
            if (change.after_discount or Decimal("0")) <= Decimal("0")
            else OrderEventType.DISCOUNT_APPLIED
        ),
    }.get(change.change_type)


class CorrelationWorker:
    """Derive correlations and append-only order evidence from normalized data."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        minimum_score: int = 60,
        time_window_seconds: int = 7200,
        timezone: str = "Europe/Rome",
    ) -> None:
        self._factory = factory
        self.engine = CorrelationEngine(
            minimum_score=minimum_score,
            time_window_seconds=time_window_seconds,
        )
        self.time_window_seconds = time_window_seconds
        self.timezone = ZoneInfo(timezone)

    def run_once(self, *, max_documents: int | None = 10_000) -> CorrelationRunReport:
        with self._factory.begin() as session:
            if max_documents is None:
                raise ValueError("max_documents must be bounded")
            loaded, seed_ids = _candidate_batch(
                session,
                limit=max_documents,
                lookback_seconds=self.time_window_seconds,
            )
            by_id = {item.value.id: item for item in loaded}
            transactions = self.engine.correlate_candidates(
                (item.value for item in loaded),
                _candidate_pairs(loaded, seed_ids, self.time_window_seconds),
            )
            correlations_inserted = 0
            orders_created = 0
            events_inserted = 0
            snapshots_inserted = 0
            for transaction in transactions:
                if not ({document.id for document in transaction.documents} & seed_ids):
                    continue
                correlation, inserted = self._persist_correlation(session, transaction, by_id)
                correlations_inserted += int(inserted)
                order, created = self._upsert_order(session, transaction, by_id)
                orders_created += int(created)
                inserted_events, inserted_snapshots = self._append_order_evidence(
                    session,
                    order,
                    transaction,
                    by_id,
                )
                events_inserted += inserted_events
                snapshots_inserted += inserted_snapshots
                # The value is used by the fraud worker; touching it here makes
                # the ordering dependency explicit without coupling workers.
                del correlation
            return CorrelationRunReport(
                documents_loaded=len(loaded),
                transactions_evaluated=len(transactions),
                correlations_inserted=correlations_inserted,
                orders_created=orders_created,
                events_inserted=events_inserted,
                snapshots_inserted=snapshots_inserted,
            )

    def _persist_correlation(
        self,
        session: Session,
        transaction: CorrelatedTransaction,
        loaded_by_id: dict[UUID, LoadedDocument],
    ) -> tuple[DocumentCorrelation, bool]:
        fingerprint = correlation_input_fingerprint(transaction.documents, loaded_by_id)
        existing = session.scalar(
            select(DocumentCorrelation).where(
                DocumentCorrelation.transaction_id == transaction.transaction_id,
                DocumentCorrelation.algorithm_version == ALGORITHM_VERSION,
                DocumentCorrelation.input_fingerprint == fingerprint,
            )
        )
        if existing is not None:
            return existing, False

        document_ids = {document.id for document in transaction.documents}
        previous = session.scalars(
            select(DocumentCorrelation)
            .join(
                DocumentCorrelationMember,
                DocumentCorrelationMember.correlation_id == DocumentCorrelation.id,
            )
            .where(
                DocumentCorrelation.algorithm_version == ALGORITHM_VERSION,
                DocumentCorrelationMember.document_id.in_(document_ids),
                DocumentCorrelation.status.in_(("AUTOMATIC", "UNCORRELATED")),
            )
            .distinct()
        ).all()
        for old in previous:
            old_members = set(
                session.scalars(
                    select(DocumentCorrelationMember.document_id).where(
                        DocumentCorrelationMember.correlation_id == old.id
                    )
                )
            )
            if old_members <= document_ids:
                old.status = "SUPERSEDED"

        result = transaction.correlation
        correlation = DocumentCorrelation(
            transaction_id=transaction.transaction_id,
            algorithm_version=ALGORITHM_VERSION,
            input_fingerprint=fingerprint,
            score=0 if result is None else result.score,
            status="UNCORRELATED" if result is None else "AUTOMATIC",
            matched_criteria=[] if result is None else list(result.matched_criteria),
            unmatched_criteria=[] if result is None else list(result.unmatched_criteria),
            explanation=(
                "Documento non correlato ad altre evidenze nella finestra configurata."
                if result is None
                else result.explanation
            ),
        )
        session.add(correlation)
        session.flush()
        for document in transaction.documents:
            scores = [
                self.engine.score_candidate_pair(document, other)
                for other in transaction.documents
                if other.id != document.id
            ]
            best = max(scores, key=lambda item: item.score, default=None)
            criteria = [] if best is None else [item.name for item in best.criteria if item.matched]
            session.add(
                DocumentCorrelationMember(
                    correlation_id=correlation.id,
                    document_id=document.id,
                    role=_member_role(document),
                    contribution_score=0 if best is None else best.score,
                    criteria=criteria,
                )
            )
        return correlation, True

    def _upsert_order(
        self,
        session: Session,
        transaction: CorrelatedTransaction,
        loaded_by_id: dict[UUID, LoadedDocument],
    ) -> tuple[Order, bool]:
        documents = transaction.documents
        sources = [document for document in documents if document.type in _SOURCE_TYPES]
        primary = min(
            sources or list(documents), key=lambda item: (_document_time(item), str(item.id))
        )
        linked_order = session.scalar(
            select(Order)
            .join(OrderEvent, OrderEvent.order_id == Order.id)
            .where(OrderEvent.source_document_id.in_([document.id for document in documents]))
            .limit(1)
        )
        order_code = next(
            (document.order_code for document in (*sources, *documents) if document.order_code),
            f"AUTO:{primary.id}",
        )
        business_date = _document_time(primary).astimezone(self.timezone).date()
        device_id = loaded_by_id[primary.id].database_device_id
        order = linked_order or session.scalar(
            select(Order).where(
                Order.source_device_id == device_id,
                Order.business_date == business_date,
                Order.order_code == order_code,
            )
        )
        created = order is None
        if order is None:
            order = Order(
                source_device_id=device_id,
                business_date=business_date,
                order_code=order_code,
                status="OPEN",
                opened_at=min(_document_time(document) for document in documents),
            )
            session.add(order)
            session.flush()
        elif order.order_code.startswith("AUTO:") and not order_code.startswith("AUTO:"):
            collision = session.scalar(
                select(Order.id).where(
                    Order.source_device_id == device_id,
                    Order.business_date == business_date,
                    Order.order_code == order_code,
                    Order.id != order.id,
                )
            )
            if collision is None:
                order.order_code = order_code

        fiscals = [document for document in documents if document.type in _FISCAL_TYPES]
        cancellation = any(document.type is DocumentType.CANCELLATION for document in documents)
        order.status = "CLOSED" if fiscals else "VOIDED" if cancellation else "OPEN"
        order.table_code = next(
            (document.table_code for document in documents if document.table_code), None
        )
        order.operator_code = next(
            (document.operator_code for document in documents if document.operator_code), None
        )
        order.terminal_code = next(
            (document.terminal_code for document in documents if document.terminal_code), None
        )
        order.gross_total = (
            transaction.fiscal_total
            if fiscals
            else transaction.prebill_total
            or next(
                (
                    document.gross_total
                    for document in reversed(documents)
                    if document.gross_total is not None
                ),
                None,
            )
        )
        latest = max(documents, key=lambda item: (_document_time(item), str(item.id)))
        order.net_total = latest.net_total
        order.discount_total = latest.discount_total
        order.closed_at = max((_document_time(item) for item in fiscals), default=None)
        version_ids = [loaded_by_id[document.id].version_id for document in documents]
        for payment in session.scalars(
            select(Payment).where(Payment.document_version_id.in_(version_ids))
        ):
            if payment.order_id is None:
                payment.order_id = order.id
        return order, created

    def _desired_events(
        self,
        transaction: CorrelatedTransaction,
        loaded_by_id: dict[UUID, LoadedDocument],
    ) -> list[
        tuple[
            datetime,
            OrderEventType,
            NormalizedDocument,
            UUID,
            dict[str, Any],
            tuple[DomainLine, ...],
        ]
    ]:
        documents = list(transaction.documents)
        sources = [document for document in documents if document.type in _SOURCE_TYPES]
        desired: list[
            tuple[
                datetime,
                OrderEventType,
                NormalizedDocument,
                UUID,
                dict[str, Any],
                tuple[DomainLine, ...],
            ]
        ] = []
        if sources:
            first = min(sources, key=lambda item: (_document_time(item), str(item.id)))
            desired.append(
                (
                    _document_time(first),
                    OrderEventType.ORDER_CREATED,
                    first,
                    loaded_by_id[first.id].version_id,
                    {"document_type": first.type.value, "total": str(first.gross_total)},
                    first.lines,
                )
            )
        for document in documents:
            event_type = {
                DocumentType.PRE_BILL: OrderEventType.PRE_BILL_PRINTED,
                DocumentType.COMMERCIAL_DOCUMENT: OrderEventType.FISCAL_DOCUMENT_ISSUED,
                DocumentType.PAYMENT: OrderEventType.PAYMENT_RECORDED,
                DocumentType.CONFORMING_COPY: OrderEventType.COPY_PRINTED,
                DocumentType.REPRINT: OrderEventType.COPY_PRINTED,
                DocumentType.CANCELLATION: OrderEventType.ORDER_VOIDED,
            }.get(document.type)
            if event_type is not None:
                desired.append(
                    (
                        _document_time(document),
                        event_type,
                        document,
                        loaded_by_id[document.id].version_id,
                        {
                            "document_type": document.type.value,
                            "external_document_code": document.external_document_code,
                            "total": str(document.gross_total),
                        },
                        document.lines,
                    )
                )
            if document.payments and document.type is not DocumentType.PAYMENT:
                desired.append(
                    (
                        _document_time(document),
                        OrderEventType.PAYMENT_RECORDED,
                        document,
                        loaded_by_id[document.id].version_id,
                        {
                            "payments": [
                                payment.model_dump(mode="json") for payment in document.payments
                            ]
                        },
                        document.lines,
                    )
                )

        ordered_sources = sorted(sources, key=lambda item: (_document_time(item), str(item.id)))
        comparisons: list[
            tuple[tuple[DomainLine, ...], tuple[DomainLine, ...], NormalizedDocument]
        ] = []

        # Department tickets are partial views of one dispatch, not successive
        # snapshots.  Comparing BAR lines with CUCINA lines would manufacture
        # removals.  Keep an independent state per device/table and apply only
        # explicit ORDER_CHANGE deltas to that state.
        pos_state: dict[tuple[str, str], tuple[DomainLine, ...]] = {}
        non_pos_sources: list[NormalizedDocument] = []
        for document in ordered_sources:
            table = document.table_code.strip().upper() if document.table_code else ""
            state_key = (document.source_device_id, table)
            if document.type is DocumentType.KITCHEN_ORDER:
                if table:
                    pos_state[state_key] = document.lines
                continue
            if document.type is DocumentType.ORDER_CHANGE:
                before_lines = pos_state.get(state_key) if table else None
                if before_lines is not None:
                    after_lines = apply_order_change_lines(before_lines, document.lines)
                    comparisons.append((before_lines, after_lines, document))
                    pos_state[state_key] = after_lines
                continue
            non_pos_sources.append(document)
        comparisons.extend(
            (before.lines, after.lines, after)
            for before, after in zip(non_pos_sources, non_pos_sources[1:], strict=False)
        )
        prebill = next(
            (
                document
                for document in reversed(documents)
                if document.type is DocumentType.PRE_BILL
            ),
            None,
        )
        fiscal = next(
            (document for document in documents if document.type in _FISCAL_TYPES),
            None,
        )
        if prebill is not None and fiscal is not None:
            comparisons.append((prebill.lines, fiscal.lines, fiscal))
        seen_changes: set[str] = set()
        for before_lines, after_lines, after in comparisons:
            for change in compare_document_lines(before_lines, after_lines):
                event_type = _line_change_event(change)
                if event_type is None:
                    continue
                details = change.model_dump(mode="json")
                identity = hashlib.sha256(canonical_json(details)).hexdigest()
                if identity in seen_changes:
                    continue
                seen_changes.add(identity)
                desired.append(
                    (
                        _document_time(after),
                        event_type,
                        after,
                        loaded_by_id[after.id].version_id,
                        details,
                        after_lines,
                    )
                )
        if any(document.type in _FISCAL_TYPES for document in documents):
            final = max(
                (document for document in documents if document.type in _FISCAL_TYPES),
                key=lambda item: (_document_time(item), str(item.id)),
            )
            desired.append(
                (
                    _document_time(final),
                    OrderEventType.ORDER_CLOSED,
                    final,
                    loaded_by_id[final.id].version_id,
                    {"fiscal_total": str(transaction.fiscal_total)},
                    final.lines,
                )
            )
        desired.sort(key=lambda item: (item[0], item[1].value, str(item[2].id)))
        return desired

    def _append_order_evidence(
        self,
        session: Session,
        order: Order,
        transaction: CorrelatedTransaction,
        loaded_by_id: dict[UUID, LoadedDocument],
    ) -> tuple[int, int]:
        session.scalar(select(Order).where(Order.id == order.id).with_for_update())
        latest_event = session.scalar(
            select(OrderEvent)
            .where(OrderEvent.order_id == order.id)
            .order_by(OrderEvent.sequence.desc())
            .limit(1)
            .with_for_update()
        )
        event_sequence = 0 if latest_event is None else latest_event.sequence
        event_hash = ZERO_HASH if latest_event is None else latest_event.record_hash
        latest_snapshot = session.scalar(
            select(OrderSnapshot)
            .where(OrderSnapshot.order_id == order.id)
            .order_by(OrderSnapshot.sequence.desc())
            .limit(1)
            .with_for_update()
        )
        snapshot_sequence = 0 if latest_snapshot is None else latest_snapshot.sequence
        snapshot_hash = ZERO_HASH if latest_snapshot is None else latest_snapshot.record_hash
        events_inserted = 0
        snapshots_inserted = 0
        for (
            occurred_at,
            event_type,
            document,
            version_id,
            details,
            snapshot_lines,
        ) in self._desired_events(transaction, loaded_by_id):
            detail_digest = hashlib.sha256(canonical_json(details)).hexdigest()
            event_id = uuid5(
                NAMESPACE_URL,
                f"retailprintguard:event:{order.id}:{version_id}:{event_type.value}:{detail_digest}",
            )
            if session.get(OrderEvent, event_id) is not None:
                continue
            event_sequence += 1
            payload = {
                "id": str(event_id),
                "order_id": str(order.id),
                "source_document_id": str(document.id),
                "sequence": event_sequence,
                "event_type": event_type.value,
                "occurred_at": occurred_at.astimezone(UTC).isoformat(),
                "operator_code": document.operator_code,
                "details": details,
                "previous_hash": event_hash,
            }
            record_hash = chained_hash(payload, event_hash)
            event = OrderEvent(
                id=event_id,
                order_id=order.id,
                source_document_id=document.id,
                sequence=event_sequence,
                event_type=event_type.value,
                occurred_at=occurred_at,
                operator_code=document.operator_code,
                details=details,
                previous_record_hash=event_hash,
                record_hash=record_hash,
            )
            session.add(event)
            event_hash = record_hash
            events_inserted += 1

            snapshot_id = uuid5(NAMESPACE_URL, f"retailprintguard:snapshot:{event_id}")
            if session.get(OrderSnapshot, snapshot_id) is not None:
                continue
            snapshot_sequence += 1
            line_state = [line.model_dump(mode="json") for line in snapshot_lines]
            snapshot_payload = {
                "id": str(snapshot_id),
                "order_id": str(order.id),
                "order_event_id": str(event_id),
                "sequence": snapshot_sequence,
                "captured_at": occurred_at.astimezone(UTC).isoformat(),
                "gross_total": str(document.gross_total),
                "net_total": str(document.net_total),
                "discount_total": str(document.discount_total),
                "lines": line_state,
                "previous_hash": snapshot_hash,
            }
            snapshot_record_hash = chained_hash(snapshot_payload, snapshot_hash)
            session.add(
                OrderSnapshot(
                    id=snapshot_id,
                    order_id=order.id,
                    order_event_id=event_id,
                    sequence=snapshot_sequence,
                    captured_at=occurred_at,
                    gross_total=document.gross_total,
                    net_total=document.net_total,
                    discount_total=document.discount_total,
                    lines=line_state,
                    previous_record_hash=snapshot_hash,
                    record_hash=snapshot_record_hash,
                )
            )
            snapshot_hash = snapshot_record_hash
            snapshots_inserted += 1
        return events_inserted, snapshots_inserted


__all__ = [
    "CorrelationRunReport",
    "CorrelationWorker",
    "LoadedDocument",
    "activate_parser_version",
    "correlation_input_fingerprint",
    "load_latest_documents",
]
