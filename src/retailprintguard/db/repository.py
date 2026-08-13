"""SQLAlchemy repositories for the API and evidence-ingestion boundaries.

All filtering is expressed with SQLAlchemy expressions (never interpolated SQL).
The API adapter deliberately exposes read models rather than ORM instances; a
request cannot retain a live database session after the repository call returns.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from retailprintguard.api.auth import PasswordService
from retailprintguard.api.repository import RawArtifact, RepositoryUnavailable
from retailprintguard.api.schemas import (
    AlertUpdate,
    AlertView,
    AuditEntry,
    DashboardView,
    DeviceView,
    DocumentLineView,
    DocumentView,
    ImportBatchView,
    JobView,
    OrderView,
    RoleName,
    RuleView,
    SearchHit,
    SessionView,
    TransactionView,
    UserPrincipal,
)
from retailprintguard.common.config import Settings
from retailprintguard.common.domain import AlertStatus, DocumentType
from retailprintguard.common.hashchain import ZERO_HASH, chained_hash
from retailprintguard.db.models import (
    AuditLog,
    Device,
    DeviceStatus,
    Document,
    DocumentCorrelation,
    DocumentCorrelationMember,
    DocumentLine,
    DocumentVersion,
    FraudAlert,
    FraudAlertEvidence,
    FraudAlertHistory,
    FraudRule,
    FraudRuleVersion,
    HashChainHead,
    ImportBatch,
    ImportItem,
    Order,
    OrderEvent,
    OrderSnapshot,
    ParserVersion,
    Payment,
    PrintJob,
    ProxySession,
    RawPayload,
    Role,
    StreamChunk,
    SystemEvent,
    User,
    UserRole,
)
from retailprintguard.db.session import create_db_engine, session_factory
from retailprintguard.fraud.versioning import rule_configuration_fingerprint
from retailprintguard.ingestion.dto import (
    ArtifactRole,
    NormalizedEnvelope,
    QuarantineRecord,
    RetryRecord,
    StreamDirection,
)
from retailprintguard.ingestion.errors import RepositoryUnavailable as IngestionUnavailable
from retailprintguard.ingestion.repository import (
    ImportDisposition,
    RepositoryImportResult,
)

MONEY_ZERO = Decimal("0.0000")
FISCAL_TYPES = {DocumentType.COMMERCIAL_DOCUMENT.value, DocumentType.REFUND.value}
SOURCE_TYPES = {
    DocumentType.ORDER.value,
    DocumentType.ORDER_CHANGE.value,
    DocumentType.KITCHEN_ORDER.value,
    DocumentType.PRE_BILL.value,
    DocumentType.MANAGEMENT_DOCUMENT.value,
}


def _page(limit: int, offset: int, *, maximum: int = 10_000) -> tuple[int, int]:
    if not 1 <= limit <= maximum or offset < 0:
        raise ValueError("invalid pagination")
    return limit, offset


def _latest_version_id() -> Any:
    return (
        select(DocumentVersion.id)
        .where(DocumentVersion.document_id == Document.id)
        .order_by(DocumentVersion.version_sequence.desc(), DocumentVersion.id.desc())
        .limit(1)
        .correlate(Document)
        .scalar_subquery()
    )


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _aware(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps entering the database must be timezone-aware")
    return value.astimezone(UTC)


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _text(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _line_key(line: DocumentLine) -> str:
    if line.item_code:
        return f"CODE:{' '.join(line.item_code.upper().split())}"
    if line.description:
        return f"DESC:{' '.join(line.description.upper().split())}"
    return f"SEQUENCE:{line.sequence}"


def _aggregate_stored_lines(lines: Sequence[DocumentLine]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for line in lines:
        key = _line_key(line)
        quantity = line.quantity if line.quantity is not None else Decimal("1")
        price = (
            line.modified_unit_price if line.modified_unit_price is not None else line.unit_price
        )
        if key not in result:
            result[key] = {
                "item_key": key,
                "description": line.description,
                "quantity": quantity,
                "unit_price": price,
                "discount": line.discount,
                "line_total": line.line_total,
            }
            continue
        result[key]["quantity"] += quantity
        if result[key]["line_total"] is not None and line.line_total is not None:
            result[key]["line_total"] += line.line_total
    return result


def _stored_line_diff(
    before: Sequence[DocumentLine], after: Sequence[DocumentLine]
) -> dict[str, list[dict[str, Any]]]:
    left, right = _aggregate_stored_lines(before), _aggregate_stored_lines(after)
    result: dict[str, list[dict[str, Any]]] = {
        "added": [],
        "removed": [],
        "quantity_changed": [],
        "price_changed": [],
        "discount_changed": [],
    }
    for key in sorted(set(left) | set(right)):
        old, new = left.get(key), right.get(key)
        if old is None:
            result["added"].append({"after": new})
        elif new is None:
            result["removed"].append({"before": old})
        else:
            common = {"item_key": key, "description": new["description"] or old["description"]}
            if old["quantity"] != new["quantity"]:
                result["quantity_changed"].append(
                    {**common, "before": old["quantity"], "after": new["quantity"]}
                )
            if old["unit_price"] != new["unit_price"]:
                result["price_changed"].append(
                    {**common, "before": old["unit_price"], "after": new["unit_price"]}
                )
            if old["discount"] != new["discount"]:
                result["discount_changed"].append(
                    {**common, "before": old["discount"], "after": new["discount"]}
                )
    return result


class SqlAlchemyApiRepository:
    """Concrete, synchronous API repository backed by MariaDB/SQLAlchemy 2."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._passwords = PasswordService()

    @classmethod
    def from_url(cls, database_url: str) -> SqlAlchemyApiRepository:
        return cls(session_factory(create_db_engine(database_url)))

    @contextmanager
    def _read(self) -> Iterator[Session]:
        session = self._factory()
        try:
            yield session
        except SQLAlchemyError as exc:
            raise RepositoryUnavailable("database query failed") from exc
        finally:
            session.close()

    @contextmanager
    def _write(self) -> Iterator[Session]:
        session = self._factory()
        try:
            with session.begin():
                yield session
        except SQLAlchemyError as exc:
            raise RepositoryUnavailable("database transaction failed") from exc
        finally:
            session.close()

    def authenticate(self, username: str, password: str) -> UserPrincipal | None:
        now = datetime.now(UTC)
        with self._write() as session:
            user = session.scalar(
                select(User).where(func.lower(User.username) == username.strip().lower())
            )
            if user is None or not user.active:
                return None
            if user.locked_until is not None and user.locked_until > now:
                return None
            if not self._passwords.verify(user.password_hash, password):
                user.failed_login_count += 1
                if user.failed_login_count >= 5:
                    user.locked_until = now + timedelta(minutes=5)
                return None
            user.failed_login_count = 0
            user.locked_until = None
            user.last_login_at = now
            role_codes = session.scalars(
                select(Role.code)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == user.id)
                .order_by(Role.code)
            ).all()
            roles: list[RoleName] = []
            for code in role_codes:
                try:
                    roles.append(RoleName(code))
                except ValueError:
                    continue
            return UserPrincipal(
                id=user.id,
                username=user.username,
                roles=tuple(roles),
                active=user.active,
            )

    def dashboard(self) -> DashboardView:
        with self._read() as session:
            documents = session.scalar(select(func.count()).select_from(Document)) or 0
            orders = session.scalar(select(func.count()).select_from(Order)) or 0
            type_counts = dict(
                session.execute(
                    select(Document.document_type, func.count()).group_by(Document.document_type)
                ).all()
            )
            open_states = ("OPEN", "UNDER_REVIEW")
            open_alerts = (
                session.scalar(
                    select(func.count())
                    .select_from(FraudAlert)
                    .where(FraudAlert.status.in_(open_states))
                )
                or 0
            )
            critical = (
                session.scalar(
                    select(func.count())
                    .select_from(FraudAlert)
                    .where(
                        FraudAlert.status.in_(open_states),
                        FraudAlert.severity == "CRITICAL",
                    )
                )
                or 0
            )
            economic = (
                session.scalar(
                    select(func.coalesce(func.sum(FraudAlert.difference_amount), 0)).where(
                        FraudAlert.status.in_(open_states)
                    )
                )
                or MONEY_ZERO
            )
            device_count = session.scalar(select(func.count()).select_from(Device)) or 0
            latest_status = self._latest_device_statuses(session)
            online = sum(status.status.upper() == "ONLINE" for status in latest_status.values())
            spool_bytes = sum(status.spool_bytes for status in latest_status.values())
            # Keep the recovery/test dialect free from MariaDB JSON functions.
            parse_errors = sum(
                bool(errors) for errors in session.scalars(select(DocumentVersion.errors)).all()
            )
            trend = [
                {"date": str(day), "count": count}
                for day, count in session.execute(
                    select(func.date(FraudAlert.opened_at), func.count())
                    .group_by(func.date(FraudAlert.opened_at))
                    .order_by(func.date(FraudAlert.opened_at).desc())
                    .limit(30)
                ).all()
            ]
            return DashboardView(
                documents=documents,
                orders=orders,
                pre_bills=type_counts.get(DocumentType.PRE_BILL.value, 0),
                management_documents=type_counts.get(DocumentType.MANAGEMENT_DOCUMENT.value, 0),
                commercial_documents=type_counts.get(DocumentType.COMMERCIAL_DOCUMENT.value, 0),
                open_alerts=open_alerts,
                critical_alerts=critical,
                economic_difference=economic,
                devices_online=online,
                devices_offline=max(0, device_count - online),
                spool_bytes=spool_bytes,
                parse_errors=parse_errors,
                alert_trend=trend,
            )

    @staticmethod
    def _latest_device_statuses(session: Session) -> dict[UUID, DeviceStatus]:
        latest = (
            select(
                DeviceStatus.device_id,
                func.max(DeviceStatus.observed_at).label("observed_at"),
            )
            .group_by(DeviceStatus.device_id)
            .subquery()
        )
        rows = session.scalars(
            select(DeviceStatus).join(
                latest,
                and_(
                    latest.c.device_id == DeviceStatus.device_id,
                    latest.c.observed_at == DeviceStatus.observed_at,
                ),
            )
        ).all()
        return {row.device_id: row for row in rows}

    def list_devices(self) -> Sequence[DeviceView]:
        with self._read() as session:
            statuses = self._latest_device_statuses(session)
            devices = session.scalars(select(Device).order_by(Device.external_id)).all()
            return tuple(
                DeviceView(
                    id=device.external_id,
                    name=device.name,
                    type=device.device_type,
                    enabled=device.enabled,
                    online=(
                        device.id in statuses and statuses[device.id].status.upper() == "ONLINE"
                    ),
                    listen_endpoint=f"{device.listen_ip}:{device.listen_port}",
                    target_endpoint=f"{device.target_ip}:{device.target_port}",
                    last_connection_at=(
                        statuses[device.id].last_connection_at if device.id in statuses else None
                    ),
                    last_print_at=(
                        statuses[device.id].last_print_at if device.id in statuses else None
                    ),
                    last_response_at=(
                        statuses[device.id].last_response_at if device.id in statuses else None
                    ),
                    spool_bytes=statuses[device.id].spool_bytes if device.id in statuses else 0,
                    pending_jobs=(
                        statuses[device.id].pending_imports if device.id in statuses else 0
                    ),
                    service_version=(
                        statuses[device.id].service_version if device.id in statuses else None
                    ),
                    last_error=statuses[device.id].error if device.id in statuses else None,
                )
                for device in devices
            )

    def list_sessions(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[SessionView], int]:
        limit, offset = _page(limit, offset)
        with self._read() as session:
            statement: Select[Any] = select(ProxySession, Device).join(Device)
            if filters.get("device_id"):
                statement = statement.where(Device.external_id == str(filters["device_id"]))
            count = (
                session.scalar(
                    select(func.count()).select_from(statement.order_by(None).subquery())
                )
                or 0
            )
            rows = session.execute(
                statement.order_by(ProxySession.started_at.desc()).limit(limit).offset(offset)
            ).all()
            return [
                SessionView(
                    id=item.id,
                    device_id=device.external_id,
                    source_endpoint=(
                        f"{item.client_ip}:{item.client_port}"
                        if item.client_ip and item.client_port is not None
                        else "sconosciuto"
                    ),
                    target_endpoint=f"{item.target_ip}:{item.target_port}",
                    opened_at=item.started_at,
                    closed_at=item.ended_at,
                    close_reason=item.status,
                    request_bytes=item.bytes_to_device,
                    response_bytes=item.bytes_to_client,
                    complete=item.capture_complete,
                )
                for item, device in rows
            ], count

    def list_jobs(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[JobView], int]:
        limit, offset = _page(limit, offset)
        with self._read() as session:
            statement: Select[Any] = select(PrintJob, Device).join(Device)
            if filters.get("device_id"):
                statement = statement.where(Device.external_id == str(filters["device_id"]))
            if filters.get("status"):
                statement = statement.where(PrintJob.status == str(filters["status"]))
            count = (
                session.scalar(
                    select(func.count()).select_from(statement.order_by(None).subquery())
                )
                or 0
            )
            rows = session.execute(
                statement.order_by(PrintJob.captured_at.desc()).limit(limit).offset(offset)
            ).all()
            result: list[JobView] = []
            for job, device in rows:
                direction_sizes = dict(
                    session.execute(
                        select(RawPayload.direction, func.sum(RawPayload.byte_count))
                        .where(RawPayload.job_id == job.id)
                        .group_by(RawPayload.direction)
                    ).all()
                )
                result.append(
                    JobView(
                        id=job.id,
                        device_id=device.external_id,
                        session_id=job.session_id,
                        external_job_id=job.source_job_id,
                        captured_at=job.captured_at,
                        status=job.status,
                        request_bytes=int(direction_sizes.get("CLIENT_TO_DEVICE", 0) or 0),
                        response_bytes=int(direction_sizes.get("DEVICE_TO_CLIENT", 0) or 0),
                        manifest_sha256=job.manifest_sha256,
                        spool_path=job.manifest_path,
                        imported_at=job.imported_at,
                        parser_status=job.import_status,
                        warnings=[str(value) for value in (job.warnings or [])],
                    )
                )
            return result, count

    def _document_view(self, session: Session, document: Document) -> DocumentView | None:
        version = session.scalar(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document.id)
            .order_by(DocumentVersion.version_sequence.desc(), DocumentVersion.id.desc())
            .limit(1)
        )
        if version is None:
            return None
        device = session.get(Device, document.device_id)
        parser = session.get(ParserVersion, version.parser_version_id)
        lines = session.scalars(
            select(DocumentLine)
            .where(DocumentLine.document_version_id == version.id)
            .order_by(DocumentLine.sequence)
        ).all()
        payments = session.scalars(
            select(Payment)
            .where(Payment.document_version_id == version.id)
            .order_by(Payment.created_at, Payment.id)
        ).all()
        correlations = session.execute(
            select(DocumentCorrelationMember, DocumentCorrelation)
            .join(
                DocumentCorrelation,
                DocumentCorrelation.id == DocumentCorrelationMember.correlation_id,
            )
            .where(DocumentCorrelationMember.document_id == document.id)
            .order_by(DocumentCorrelation.created_at.desc())
        ).all()
        return DocumentView(
            id=document.id,
            device_id=device.external_id if device else str(document.device_id),
            job_id=document.job_id,
            type=document.document_type,
            subtype=document.subtype,
            external_code=document.external_document_code,
            order_code=document.order_code,
            table_code=document.table_code,
            operator_code=document.operator_code,
            terminal_code=document.terminal_code,
            document_timestamp=document.document_timestamp,
            captured_at=document.captured_at,
            gross_total=version.gross_total,
            net_total=version.net_total,
            discount_total=version.discount_total,
            tax_total=version.tax_total,
            status=version.status,
            normalized_text=version.normalized_text,
            parser_name=parser.name if parser else "unknown",
            parser_version=parser.version if parser else "unknown",
            confidence=version.parse_confidence,
            sha256=version.source_payload_sha256,
            complete=version.complete,
            warnings=[str(value) for value in (version.warnings or [])],
            lines=[
                DocumentLineView(
                    sequence=line.sequence,
                    item_code=line.item_code,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    original_unit_price=line.original_unit_price,
                    modified_unit_price=line.modified_unit_price,
                    discount=line.discount,
                    tax_rate=line.tax_rate,
                    line_total=line.line_total,
                    state=line.line_state,
                    removed=line.removed,
                    cancelled=line.cancelled,
                    raw_text=line.raw_text,
                )
                for line in lines
            ],
            payments=[
                {
                    "id": str(payment.id),
                    "method": payment.method,
                    "amount": str(payment.amount),
                    "currency": payment.currency,
                    "status": payment.status,
                    "evidence": payment.evidence_level,
                }
                for payment in payments
            ],
            correlations=[
                {
                    "id": str(correlation.id),
                    "transaction_id": str(correlation.transaction_id),
                    "score": correlation.score,
                    "role": member.role,
                    "algorithm_version": correlation.algorithm_version,
                    "explanation": correlation.explanation,
                }
                for member, correlation in correlations
            ],
        )

    def list_documents(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[DocumentView], int]:
        limit, offset = _page(limit, offset)
        with self._read() as session:
            statement: Select[Any] = select(Document).join(Device)
            if filters.get("type"):
                statement = statement.where(Document.document_type == str(filters["type"]))
            if filters.get("device_id"):
                statement = statement.where(Device.external_id == str(filters["device_id"]))
            if filters.get("order_code"):
                statement = statement.where(Document.order_code == str(filters["order_code"]))
            count = (
                session.scalar(
                    select(func.count()).select_from(statement.order_by(None).subquery())
                )
                or 0
            )
            documents = session.scalars(
                statement.order_by(Document.captured_at.desc(), Document.id)
                .limit(limit)
                .offset(offset)
            ).all()
            return [
                view
                for document in documents
                if (view := self._document_view(session, document)) is not None
            ], count

    def get_document(self, document_id: UUID) -> DocumentView | None:
        with self._read() as session:
            document = session.get(Document, document_id)
            return None if document is None else self._document_view(session, document)

    def get_document_raw(self, document_id: UUID) -> RawArtifact | None:
        with self._read() as session:
            document = session.get(Document, document_id)
            if document is None:
                return None
            version = session.scalar(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.version_sequence.desc())
                .limit(1)
            )
            raw = session.get(RawPayload, version.raw_payload_id) if version else None
            if raw is None:
                raw = session.scalar(
                    select(RawPayload)
                    .where(
                        RawPayload.job_id == document.job_id,
                        RawPayload.direction == "CLIENT_TO_DEVICE",
                    )
                    .order_by(RawPayload.created_at, RawPayload.id)
                    .limit(1)
                )
            if raw is None:
                return None
            filename = f"{document.id}_{raw.artifact_role.lower()}.raw"
            return RawArtifact(raw.payload, filename, raw.sha256)

    def list_orders(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[OrderView], int]:
        limit, offset = _page(limit, offset)
        with self._read() as session:
            statement: Select[Any] = select(Order)
            if filters.get("table_code"):
                statement = statement.where(Order.table_code == str(filters["table_code"]))
            if filters.get("order_code"):
                statement = statement.where(Order.order_code == str(filters["order_code"]))
            count = (
                session.scalar(
                    select(func.count()).select_from(statement.order_by(None).subquery())
                )
                or 0
            )
            rows = session.scalars(
                statement.order_by(Order.opened_at.desc()).limit(limit).offset(offset)
            ).all()
            result: list[OrderView] = []
            for order in rows:
                version = (
                    session.scalar(
                        select(func.max(OrderSnapshot.sequence)).where(
                            OrderSnapshot.order_id == order.id
                        )
                    )
                    or 0
                )
                result.append(
                    OrderView(
                        id=order.id,
                        external_code=order.order_code,
                        table_code=order.table_code,
                        operator_code=order.operator_code,
                        opened_at=order.opened_at,
                        closed_at=order.closed_at,
                        status=order.status,
                        current_total=order.gross_total,
                        version=int(version),
                    )
                )
            return result, count

    def _transaction_view(
        self, session: Session, correlation: DocumentCorrelation
    ) -> TransactionView:
        documents = session.scalars(
            select(Document)
            .join(
                DocumentCorrelationMember,
                DocumentCorrelationMember.document_id == Document.id,
            )
            .where(DocumentCorrelationMember.correlation_id == correlation.id)
            .order_by(Document.captured_at, Document.id)
        ).all()
        versions: dict[UUID, DocumentVersion] = {}
        for document in documents:
            version = session.scalar(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.version_sequence.desc())
                .limit(1)
            )
            if version is not None:
                versions[document.id] = version
        prebill_documents = [
            document for document in documents if document.document_type == "PRE_BILL"
        ]
        fiscal_documents = [
            document for document in documents if document.document_type in FISCAL_TYPES
        ]
        prebill_total = (
            versions[prebill_documents[-1].id].gross_total
            if prebill_documents and prebill_documents[-1].id in versions
            else None
        )
        fiscal_total = sum(
            (
                -(versions[document.id].gross_total or MONEY_ZERO)
                if document.document_type == "REFUND"
                else (versions[document.id].gross_total or MONEY_ZERO)
            )
            for document in fiscal_documents
            if document.id in versions
        )
        initial = next(
            (
                versions[document.id].gross_total
                for document in documents
                if document.document_type in SOURCE_TYPES and document.id in versions
            ),
            None,
        )
        alert_count = (
            session.scalar(
                select(func.count())
                .select_from(FraudAlert)
                .where(FraudAlert.transaction_id == correlation.transaction_id)
            )
            or 0
        )
        order_code = next((item.order_code for item in documents if item.order_code), None)
        order = None
        if order_code:
            order = session.scalar(
                select(Order)
                .where(Order.order_code == order_code)
                .order_by(Order.opened_at.desc())
                .limit(1)
            )
        timeline: list[dict[str, Any]] = [
            {
                "event_kind": "DOCUMENT",
                "document_id": str(document.id),
                "type": document.document_type,
                "occurred_at": (document.document_timestamp or document.captured_at).isoformat(),
                "total": (
                    str(versions[document.id].gross_total)
                    if document.id in versions and versions[document.id].gross_total is not None
                    else None
                ),
            }
            for document in documents
        ]
        snapshots: dict[UUID, OrderSnapshot] = {}
        if order is not None:
            snapshots = {
                snapshot.order_event_id: snapshot
                for snapshot in session.scalars(
                    select(OrderSnapshot).where(OrderSnapshot.order_id == order.id)
                )
                if snapshot.order_event_id is not None
            }
            for event in session.scalars(
                select(OrderEvent)
                .where(OrderEvent.order_id == order.id)
                .order_by(OrderEvent.occurred_at, OrderEvent.sequence)
            ):
                snapshot = snapshots.get(event.id)
                timeline.append(
                    {
                        "event_kind": "ORDER_EVENT",
                        "event_id": str(event.id),
                        "type": event.event_type,
                        "occurred_at": event.occurred_at.isoformat(),
                        "document_id": (
                            str(event.source_document_id) if event.source_document_id else None
                        ),
                        "operator_code": event.operator_code,
                        "details": event.details or {},
                        "record_hash": event.record_hash,
                        "snapshot": (
                            None
                            if snapshot is None
                            else {
                                "id": str(snapshot.id),
                                "sequence": snapshot.sequence,
                                "gross_total": snapshot.gross_total,
                                "net_total": snapshot.net_total,
                                "discount_total": snapshot.discount_total,
                                "lines": snapshot.lines or [],
                                "record_hash": snapshot.record_hash,
                            }
                        ),
                    }
                )
        alerts = session.scalars(
            select(FraudAlert)
            .where(FraudAlert.transaction_id == correlation.transaction_id)
            .order_by(FraudAlert.opened_at, FraudAlert.id)
        ).all()
        for alert in alerts:
            timeline.append(
                {
                    "event_kind": "FRAUD_ALERT",
                    "alert_id": str(alert.id),
                    "type": "ALERT_OPENED",
                    "occurred_at": alert.opened_at.isoformat(),
                    "rule_version_id": str(alert.fraud_rule_version_id),
                    "severity": alert.severity,
                    "score": alert.score,
                    "status": alert.status,
                    "description": alert.description,
                }
            )
            for history in session.scalars(
                select(FraudAlertHistory)
                .where(FraudAlertHistory.fraud_alert_id == alert.id)
                .order_by(FraudAlertHistory.sequence)
            ):
                timeline.append(
                    {
                        "event_kind": "ALERT_HISTORY",
                        "alert_id": str(alert.id),
                        "history_id": str(history.id),
                        "type": history.event_type,
                        "occurred_at": history.occurred_at.isoformat(),
                        "previous_status": history.previous_status,
                        "new_status": history.new_status,
                        "reason": history.reason,
                        "record_hash": history.record_hash,
                    }
                )
        timeline.sort(
            key=lambda item: (
                item["occurred_at"],
                item["event_kind"],
                item.get("event_id") or item.get("document_id") or item.get("alert_id") or "",
            )
        )
        prebill_lines: list[DocumentLine] = []
        fiscal_lines: list[DocumentLine] = []
        if prebill_documents:
            prebill_version = versions.get(prebill_documents[-1].id)
            if prebill_version is not None:
                prebill_lines = list(
                    session.scalars(
                        select(DocumentLine)
                        .where(DocumentLine.document_version_id == prebill_version.id)
                        .order_by(DocumentLine.sequence)
                    )
                )
        fiscal_version_ids = [
            versions[document.id].id for document in fiscal_documents if document.id in versions
        ]
        if fiscal_version_ids:
            fiscal_lines = list(
                session.scalars(
                    select(DocumentLine)
                    .where(DocumentLine.document_version_id.in_(fiscal_version_ids))
                    .order_by(DocumentLine.document_version_id, DocumentLine.sequence)
                )
            )
        difference = None if prebill_total is None else prebill_total - fiscal_total
        return TransactionView(
            id=correlation.transaction_id,
            order_id=order.id if order else None,
            occurred_at=(
                min((document.document_timestamp or document.captured_at) for document in documents)
                if documents
                else correlation.created_at
            ),
            table_code=next((item.table_code for item in documents if item.table_code), None),
            order_code=order_code,
            operator_code=next(
                (item.operator_code for item in documents if item.operator_code), None
            ),
            initial_total=initial,
            pre_bill_total=prebill_total,
            fiscal_total=fiscal_total,
            difference=difference,
            status="ALERT" if alert_count else "CORRELATED",
            document_count=len(documents),
            alert_count=alert_count,
            correlation_confidence=correlation.score,
            timeline=timeline,
            diff={
                "pre_bill_total": str(prebill_total) if prebill_total is not None else None,
                "fiscal_total": str(fiscal_total),
                "difference": str(difference) if difference is not None else None,
                "lines": _stored_line_diff(prebill_lines, fiscal_lines),
            },
        )

    def list_transactions(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[TransactionView], int]:
        limit, offset = _page(limit, offset)
        with self._read() as session:
            # The current correlation version is unique per transaction/version. Keep
            # the newest record per transaction and apply semantic filters to views.
            correlations = session.scalars(
                select(DocumentCorrelation)
                .where(DocumentCorrelation.status != "SUPERSEDED")
                .order_by(DocumentCorrelation.created_at.desc())
            ).all()
            latest: dict[UUID, DocumentCorrelation] = {}
            for correlation in correlations:
                latest.setdefault(correlation.transaction_id, correlation)
            views = [self._transaction_view(session, item) for item in latest.values()]
            if filters.get("table_code"):
                views = [item for item in views if item.table_code == str(filters["table_code"])]
            if filters.get("operator_code"):
                views = [
                    item for item in views if item.operator_code == str(filters["operator_code"])
                ]
            if filters.get("minimum_difference") is not None:
                minimum = _as_decimal(filters["minimum_difference"]) or MONEY_ZERO
                views = [
                    item
                    for item in views
                    if item.difference is not None and abs(item.difference) >= minimum
                ]
            views.sort(key=lambda item: (item.occurred_at, str(item.id)), reverse=True)
            return views[offset : offset + limit], len(views)

    def get_transaction(self, transaction_id: UUID) -> TransactionView | None:
        with self._read() as session:
            correlation = session.scalar(
                select(DocumentCorrelation)
                .where(DocumentCorrelation.transaction_id == transaction_id)
                .order_by(DocumentCorrelation.created_at.desc())
                .limit(1)
            )
            return None if correlation is None else self._transaction_view(session, correlation)

    def _alert_view(self, session: Session, alert: FraudAlert) -> AlertView:
        version = session.get(FraudRuleVersion, alert.fraud_rule_version_id)
        rule = session.get(FraudRule, version.fraud_rule_id) if version else None
        evidence_rows = session.scalars(
            select(FraudAlertEvidence)
            .where(FraudAlertEvidence.fraud_alert_id == alert.id)
            .order_by(FraudAlertEvidence.sequence, FraudAlertEvidence.id)
        ).all()
        history_rows = session.scalars(
            select(FraudAlertHistory)
            .where(FraudAlertHistory.fraud_alert_id == alert.id)
            .order_by(FraudAlertHistory.sequence)
        ).all()
        document_ids: list[UUID] = []
        device_ids: list[str] = []
        if alert.correlation_id is not None:
            document_ids = list(
                session.scalars(
                    select(DocumentCorrelationMember.document_id).where(
                        DocumentCorrelationMember.correlation_id == alert.correlation_id
                    )
                ).all()
            )
            if document_ids:
                device_ids = list(
                    session.scalars(
                        select(Device.external_id)
                        .join(Document, Document.device_id == Device.id)
                        .where(Document.id.in_(document_ids))
                        .distinct()
                        .order_by(Device.external_id)
                    ).all()
                )
        return AlertView(
            id=alert.id,
            rule_code=rule.code if rule else "UNKNOWN",
            severity=alert.severity,
            score=alert.score,
            status=alert.status,
            opened_at=alert.opened_at,
            transaction_id=alert.transaction_id,
            device_ids=device_ids,
            document_ids=document_ids,
            description=alert.description,
            explanation=alert.explanation,
            economic_difference=alert.difference_amount,
            confidence=alert.confidence,
            assigned_to=alert.assigned_to_user_id,
            acknowledged_at=alert.taken_at,
            closed_at=alert.closed_at,
            resolution_reason=alert.closure_reason,
            evidence=[
                {
                    "id": str(row.id),
                    "type": row.evidence_type,
                    "summary": row.summary,
                    "document_id": str(row.document_id) if row.document_id else None,
                    "raw_payload_id": str(row.raw_payload_id) if row.raw_payload_id else None,
                    "artifact_path": row.artifact_path,
                    "artifact_sha256": row.artifact_sha256,
                    "details": row.evidence,
                }
                for row in evidence_rows
            ],
            history=[
                {
                    "sequence": row.sequence,
                    "event_type": row.event_type,
                    "previous_status": row.previous_status,
                    "new_status": row.new_status,
                    "note": row.note,
                    "reason": row.reason,
                    "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
                    "occurred_at": row.occurred_at.isoformat(),
                    "record_hash": row.record_hash,
                }
                for row in history_rows
            ],
        )

    def list_alerts(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[AlertView], int]:
        limit, offset = _page(limit, offset)
        with self._read() as session:
            statement: Select[Any] = select(FraudAlert).join(FraudRuleVersion).join(FraudRule)
            if filters.get("severity"):
                statement = statement.where(FraudAlert.severity == str(filters["severity"]))
            if filters.get("status"):
                statement = statement.where(FraudAlert.status == str(filters["status"]))
            if filters.get("rule"):
                statement = statement.where(FraudRule.code == str(filters["rule"]))
            if filters.get("device_id"):
                statement = (
                    statement.join(
                        DocumentCorrelationMember,
                        DocumentCorrelationMember.correlation_id == FraudAlert.correlation_id,
                    )
                    .join(Document, Document.id == DocumentCorrelationMember.document_id)
                    .join(Device, Device.id == Document.device_id)
                    .where(Device.external_id == str(filters["device_id"]))
                    .distinct()
                )
            if filters.get("operator_code"):
                statement = (
                    statement.join(
                        DocumentCorrelationMember,
                        DocumentCorrelationMember.correlation_id == FraudAlert.correlation_id,
                    )
                    .join(Document, Document.id == DocumentCorrelationMember.document_id)
                    .where(Document.operator_code == str(filters["operator_code"]))
                    .distinct()
                )
            count = (
                session.scalar(
                    select(func.count()).select_from(statement.order_by(None).subquery())
                )
                or 0
            )
            alerts = (
                session.scalars(
                    statement.order_by(FraudAlert.opened_at.desc()).limit(limit).offset(offset)
                )
                .unique()
                .all()
            )
            return [self._alert_view(session, alert) for alert in alerts], count

    def get_alert(self, alert_id: UUID) -> AlertView | None:
        with self._read() as session:
            alert = session.get(FraudAlert, alert_id)
            return None if alert is None else self._alert_view(session, alert)

    def update_alert(
        self, alert_id: UUID, update: AlertUpdate, actor: UserPrincipal
    ) -> AlertView | None:
        now = datetime.now(UTC)
        with self._write() as session:
            alert = session.scalar(
                select(FraudAlert).where(FraudAlert.id == alert_id).with_for_update()
            )
            if alert is None:
                return None
            previous_status = alert.status
            if update.status is not None:
                new_status = AlertStatus(update.status).value
                alert.status = new_status
                if new_status in {
                    AlertStatus.CLOSED.value,
                    AlertStatus.FALSE_POSITIVE.value,
                    AlertStatus.JUSTIFIED.value,
                }:
                    alert.closed_at = now
                    alert.closure_reason = update.resolution_reason or update.note
                else:
                    alert.closed_at = None
                    alert.closure_reason = None
            if update.assigned_to_me:
                alert.assigned_to_user_id = actor.id
                alert.taken_at = now
            latest = session.scalar(
                select(FraudAlertHistory)
                .where(FraudAlertHistory.fraud_alert_id == alert.id)
                .order_by(FraudAlertHistory.sequence.desc())
                .limit(1)
                .with_for_update()
            )
            sequence = 1 if latest is None else latest.sequence + 1
            previous_hash = ZERO_HASH if latest is None else latest.record_hash
            payload = {
                "alert_id": str(alert.id),
                "sequence": sequence,
                "actor_user_id": str(actor.id),
                "event_type": "ALERT_UPDATED",
                "previous_status": previous_status,
                "new_status": alert.status,
                "note": update.note,
                "reason": update.resolution_reason,
                "occurred_at": now.isoformat(),
                "previous_hash": previous_hash,
            }
            session.add(
                FraudAlertHistory(
                    fraud_alert_id=alert.id,
                    sequence=sequence,
                    actor_user_id=actor.id,
                    event_type="ALERT_UPDATED",
                    previous_status=previous_status,
                    new_status=alert.status,
                    note=update.note,
                    reason=update.resolution_reason,
                    occurred_at=now,
                    previous_record_hash=previous_hash,
                    record_hash=chained_hash(payload, previous_hash),
                )
            )
            session.flush()
            return self._alert_view(session, alert)

    def _rule_view(self, session: Session, rule: FraudRule) -> RuleView:
        version = session.scalar(
            select(FraudRuleVersion)
            .where(FraudRuleVersion.fraud_rule_id == rule.id)
            .order_by(FraudRuleVersion.version.desc())
            .limit(1)
        )
        if version is None:
            return RuleView(
                code=rule.code,
                name=rule.name,
                enabled=False,
                version=0,
                severity="UNKNOWN",
                weight=0,
            )
        return RuleView(
            code=rule.code,
            name=rule.name,
            enabled=rule.enabled and version.enabled,
            version=version.version,
            severity=version.severity,
            weight=int((version.weight * Decimal("100")).to_integral_value()),
            threshold=version.threshold,
            configuration=version.configuration or {},
        )

    def list_rules(self) -> Sequence[RuleView]:
        with self._read() as session:
            rules = session.scalars(select(FraudRule).order_by(FraudRule.code)).all()
            return tuple(self._rule_view(session, rule) for rule in rules)

    def set_rule_enabled(self, code: str, enabled: bool, actor: UserPrincipal) -> RuleView | None:
        with self._write() as session:
            rule = session.scalar(select(FraudRule).where(FraudRule.code == code).with_for_update())
            if rule is None:
                return None
            latest = session.scalar(
                select(FraudRuleVersion)
                .where(FraudRuleVersion.fraud_rule_id == rule.id)
                .order_by(FraudRuleVersion.version.desc())
                .limit(1)
                .with_for_update()
            )
            if latest is not None and (latest.enabled != enabled or rule.enabled != enabled):
                now = datetime.now(UTC)
                if latest.effective_until is None:
                    latest.effective_until = now
                fingerprint = rule_configuration_fingerprint(
                    implementation_version=latest.implementation_version,
                    enabled=enabled,
                    severity=latest.severity,
                    weight=latest.weight,
                    configuration=latest.configuration or {},
                )
                session.add(
                    FraudRuleVersion(
                        fraud_rule_id=rule.id,
                        version=latest.version + 1,
                        implementation_version=latest.implementation_version,
                        configuration_fingerprint=fingerprint,
                        enabled=enabled,
                        severity=latest.severity,
                        threshold=latest.threshold,
                        weight=latest.weight,
                        configuration=latest.configuration or {},
                        effective_from=now,
                        created_by_user_id=actor.id,
                    )
                )
            rule.enabled = enabled
            rule.updated_at = datetime.now(UTC)
            session.flush()
            return self._rule_view(session, rule)

    def list_imports(self, *, limit: int, offset: int) -> tuple[list[ImportBatchView], int]:
        limit, offset = _page(limit, offset)
        with self._read() as session:
            count = session.scalar(select(func.count()).select_from(ImportBatch)) or 0
            rows = session.scalars(
                select(ImportBatch)
                .order_by(ImportBatch.started_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            return [
                ImportBatchView(
                    id=row.id,
                    source_type=row.source_system,
                    source_root=row.source_root,
                    started_at=row.started_at,
                    completed_at=row.ended_at,
                    status=row.status,
                    discovered=row.scanned_count,
                    imported=row.imported_count,
                    duplicates=row.skipped_count,
                    failed=row.failed_count,
                    report=row.report or {},
                )
                for row in rows
            ], count

    def search(self, *, query: str, limit: int, offset: int) -> tuple[list[SearchHit], int]:
        limit, offset = _page(limit, offset, maximum=200)
        value = query.strip()
        if len(value) < 2:
            return [], 0
        escaped_value = value.replace("%", r"\%").replace("_", r"\_")
        pattern = f"%{escaped_value}%"
        with self._read() as session:
            document_ids = session.scalars(
                select(Document.id)
                .outerjoin(DocumentVersion, DocumentVersion.id == _latest_version_id())
                .where(
                    or_(
                        Document.external_document_code.ilike(pattern, escape="\\"),
                        Document.order_code.ilike(pattern, escape="\\"),
                        Document.table_code.ilike(pattern, escape="\\"),
                        Document.operator_code.ilike(pattern, escape="\\"),
                        DocumentVersion.normalized_text.ilike(pattern, escape="\\"),
                        DocumentVersion.source_payload_sha256.ilike(pattern, escape="\\"),
                        DocumentVersion.id.in_(
                            select(DocumentLine.document_version_id).where(
                                DocumentLine.description.ilike(pattern, escape="\\")
                            )
                        ),
                    )
                )
                .order_by(Document.captured_at.desc())
                .limit(500)
            ).all()
            hits: list[SearchHit] = []
            for document_id in document_ids:
                document = session.get(Document, document_id)
                if document is not None:
                    hits.append(
                        SearchHit(
                            entity_type="DOCUMENT",
                            entity_id=document.id,
                            occurred_at=document.document_timestamp or document.captured_at,
                            title=(
                                document.external_document_code
                                or document.order_code
                                or document.document_type
                            ),
                            subtitle=f"{document.document_type} · {document.table_code or '-'}",
                            highlights=[value],
                        )
                    )
            for order in session.scalars(
                select(Order)
                .where(
                    or_(
                        Order.order_code.ilike(pattern, escape="\\"),
                        Order.table_code.ilike(pattern, escape="\\"),
                        Order.operator_code.ilike(pattern, escape="\\"),
                    )
                )
                .order_by(Order.opened_at.desc())
                .limit(500)
            ).all():
                hits.append(
                    SearchHit(
                        entity_type="ORDER",
                        entity_id=order.id,
                        occurred_at=order.opened_at,
                        title=order.order_code,
                        subtitle=f"Tavolo {order.table_code or '-'}",
                        highlights=[value],
                    )
                )
            hits.sort(key=lambda item: (item.occurred_at, str(item.entity_id)), reverse=True)
            return hits[offset : offset + limit], len(hits)

    def _append_audit_session(self, session: Session, entry: AuditEntry) -> None:
        scope = "audit:global"
        head = session.scalar(
            select(HashChainHead).where(HashChainHead.scope == scope).with_for_update()
        )
        if head is None:
            head = HashChainHead(scope=scope, sequence=0, record_hash=ZERO_HASH)
            session.add(head)
            session.flush()
        sequence = head.sequence + 1
        payload = {
            "scope": scope,
            "sequence": sequence,
            "actor_id": str(entry.actor_id) if entry.actor_id else None,
            "action": entry.action,
            "entity_type": entry.entity_type,
            "entity_id": entry.entity_id,
            "correlation_id": entry.correlation_id,
            "occurred_at": entry.occurred_at.astimezone(UTC).isoformat(),
            "metadata": entry.metadata,
            "previous_hash": head.record_hash,
        }
        record_hash = chained_hash(payload, head.record_hash)
        session.add(
            AuditLog(
                chain_scope=scope,
                sequence=sequence,
                actor_user_id=entry.actor_id,
                event_type=entry.action,
                resource_type=entry.entity_type,
                resource_id=entry.entity_id,
                correlation_id=entry.correlation_id,
                occurred_at=entry.occurred_at,
                details=entry.metadata,
                previous_record_hash=head.record_hash,
                record_hash=record_hash,
            )
        )
        head.sequence = sequence
        head.record_hash = record_hash
        head.updated_at = datetime.now(UTC)

    def append_audit(self, entry: AuditEntry) -> None:
        with self._write() as session:
            self._append_audit_session(session, entry)

    def database_health(self) -> str:
        try:
            with self._read() as session:
                session.execute(text("SELECT 1"))
            return "ok"
        except RepositoryUnavailable:
            return "unavailable"


class SqlAlchemyIngestionRepository:
    """Atomic, idempotent adapter from validated envelopes to evidence tables."""

    def __init__(self, factory: sessionmaker[Session], *, spool_root: Path | None = None) -> None:
        self._factory = factory
        self._spool_root = spool_root
        self._active_import_batch: ContextVar[UUID | None] = ContextVar(
            f"rpg_import_batch_{id(self)}", default=None
        )

    def begin_import_batch(
        self, *, source_system: str, source_instance: str, source_root: Path
    ) -> str:
        """Open one durable report for an adapter scan in this worker context."""

        session = self._factory()
        try:
            with session.begin():
                batch = ImportBatch(
                    source_system=source_system[:64],
                    source_instance=source_instance[:128],
                    source_root=str(source_root),
                    status="RUNNING",
                    started_at=datetime.now(UTC),
                    report={"source_kinds": []},
                )
                session.add(batch)
                session.flush()
                batch_id = batch.id
            self._active_import_batch.set(batch_id)
            return str(batch_id)
        except SQLAlchemyError as exc:
            raise IngestionUnavailable("cannot begin import batch") from exc
        finally:
            session.close()

    def complete_import_batch(self, batch_id: str, report: dict[str, Any]) -> None:
        """Close an adapter-scan report while retaining immutable item identities."""

        try:
            parsed_id = UUID(batch_id)
        except ValueError as exc:
            raise ValueError("invalid import batch id") from exc
        session = self._factory()
        try:
            with session.begin():
                batch = session.scalar(
                    select(ImportBatch).where(ImportBatch.id == parsed_id).with_for_update()
                )
                if batch is None:
                    raise ValueError("import batch not found")
                if batch.status != "RUNNING":
                    raise ValueError("import batch is already complete")
                batch.scanned_count = int(report.get("discovered", 0))
                batch.imported_count = int(report.get("imported", 0))
                batch.skipped_count = int(report.get("duplicates", 0))
                batch.failed_count = int(report.get("quarantined", 0)) + int(
                    report.get("retry_exhausted", 0)
                )
                errors = tuple(str(item)[:4096] for item in report.get("errors", ()))
                batch.status = "COMPLETED_WITH_ERRORS" if errors else "COMPLETED"
                batch.ended_at = datetime.now(UTC)
                batch.report = {
                    **(batch.report or {}),
                    **report,
                    "errors": list(errors),
                }
        except SQLAlchemyError as exc:
            raise IngestionUnavailable("cannot complete import batch") from exc
        finally:
            if self._active_import_batch.get() == parsed_id:
                self._active_import_batch.set(None)
            session.close()

    def store_import(self, envelope: NormalizedEnvelope) -> RepositoryImportResult:
        session = self._factory()
        try:
            with session.begin():
                existing = session.scalar(
                    select(PrintJob).where(PrintJob.source_key == envelope.source_key)
                )
                if existing is not None:
                    if existing.manifest_sha256 != envelope.manifest_sha256:
                        raise ValueError("source key reused with a different manifest")
                    self._record_import_attempt(
                        session,
                        envelope,
                        existing,
                        status="DUPLICATE",
                    )
                    return RepositoryImportResult(ImportDisposition.DUPLICATE, str(existing.id))
                job = self._insert_envelope(session, envelope)
                self._record_import_attempt(session, envelope, job, status="IMPORTED")
                self._update_device_status(session, envelope, job)
                session.flush()
                return RepositoryImportResult(ImportDisposition.IMPORTED, str(job.id))
        except IntegrityError as exc:
            session.rollback()
            try:
                existing = session.scalar(
                    select(PrintJob).where(PrintJob.source_key == envelope.source_key)
                )
            except SQLAlchemyError as lookup_exc:
                raise IngestionUnavailable(
                    "database unavailable after uniqueness race"
                ) from lookup_exc
            if existing is not None and existing.manifest_sha256 == envelope.manifest_sha256:
                return RepositoryImportResult(ImportDisposition.DUPLICATE, str(existing.id))
            raise ValueError("conflicting source identity during ingestion") from exc
        except SQLAlchemyError as exc:
            raise IngestionUnavailable("database ingestion failed") from exc
        finally:
            session.close()

    def refresh_device_statuses(self) -> None:
        """Refresh control-plane spool metrics without coupling relays to MariaDB."""

        if self._spool_root is None:
            return
        session = self._factory()
        try:
            with session.begin():
                now = datetime.now(UTC)
                devices = session.scalars(select(Device).order_by(Device.external_id)).all()
                for device in devices:
                    metric_error: str | None = None
                    try:
                        snapshot = _spool_snapshot(self._spool_root / device.external_id)
                    except (OSError, RuntimeError, ValueError) as exc:
                        snapshot = {
                            "bytes": 0,
                            "ready_job_ids": set(),
                            "ready_jobs": 0,
                            "partial_jobs": 0,
                        }
                        metric_error = f"{type(exc).__name__}: {exc}"[:512]
                    ready_ids = snapshot["ready_job_ids"]
                    imported_ids: set[str] = set()
                    for offset in range(0, len(ready_ids), 500):
                        batch = tuple(sorted(ready_ids))[offset : offset + 500]
                        imported_ids.update(
                            session.scalars(
                                select(PrintJob.source_job_id).where(
                                    PrintJob.device_id == device.id,
                                    PrintJob.source_job_id.in_(batch),
                                )
                            )
                        )
                    latest = session.scalar(
                        select(DeviceStatus)
                        .where(DeviceStatus.device_id == device.id)
                        .order_by(DeviceStatus.observed_at.desc(), DeviceStatus.id.desc())
                        .limit(1)
                        .with_for_update()
                    )
                    metrics = dict(latest.metrics or {}) if latest is not None else {}
                    metrics.update(
                        {
                            "spool_scanned_at": now.isoformat(),
                            "ready_jobs": snapshot["ready_jobs"],
                            "partial_jobs": snapshot["partial_jobs"],
                        }
                    )
                    if metric_error is not None:
                        metrics["spool_metric_error"] = metric_error
                    else:
                        metrics.pop("spool_metric_error", None)
                    pending_imports = len(ready_ids - imported_ids)
                    if latest is None:
                        session.add(
                            DeviceStatus(
                                device_id=device.id,
                                observed_at=now,
                                status="UNKNOWN",
                                spool_bytes=snapshot["bytes"],
                                pending_imports=pending_imports,
                                metrics=metrics,
                                error=metric_error,
                            )
                        )
                    else:
                        latest.observed_at = now
                        latest.spool_bytes = snapshot["bytes"]
                        latest.pending_imports = pending_imports
                        latest.metrics = metrics
        except SQLAlchemyError as exc:
            raise IngestionUnavailable("cannot refresh device spool status") from exc
        finally:
            session.close()

    def _record_import_attempt(
        self,
        session: Session,
        envelope: NormalizedEnvelope,
        job: PrintJob,
        *,
        status: str,
    ) -> None:
        batch_id = self._active_import_batch.get()
        batch = session.get(ImportBatch, batch_id) if batch_id is not None else None
        if batch is None:
            batch = ImportBatch(
                source_system=envelope.source_kind.value,
                source_instance=envelope.source_instance_id,
                source_root=str(_manifest_path(envelope)),
                status="COMPLETED",
                started_at=datetime.now(UTC),
                ended_at=datetime.now(UTC),
                scanned_count=1,
                imported_count=int(status == "IMPORTED"),
                skipped_count=int(status == "DUPLICATE"),
                failed_count=0,
                report={
                    "source_key": envelope.source_key,
                    "manifest_sha256": envelope.manifest_sha256,
                    "status": status,
                },
            )
            session.add(batch)
            session.flush()
        else:
            details = dict(batch.report or {})
            source_kinds = set(details.get("source_kinds") or ())
            source_kinds.add(envelope.source_kind.value)
            details["source_kinds"] = sorted(source_kinds)
            batch.report = details
        if status == "DUPLICATE":
            return
        artifact_roles = sorted({artifact.role.value for artifact in envelope.artifacts}) or [
            "CAPTURE_MANIFEST"
        ]
        artifacts = {artifact.role.value: artifact for artifact in envelope.artifacts}
        for artifact_role in artifact_roles:
            artifact = artifacts.get(artifact_role)
            source_path = (
                str(artifact.original_path)
                if artifact is not None
                else str(_manifest_path(envelope))
            )
            session.add(
                ImportItem(
                    import_batch_id=batch.id,
                    source_system=envelope.source_kind.value,
                    source_instance=envelope.source_instance_id,
                    source_scope=job.source_scope,
                    source_job_id=envelope.source_job_id,
                    artifact_role=artifact_role,
                    original_path=source_path,
                    source_fingerprint=hashlib.sha256(
                        f"{envelope.source_key}:{artifact_role}:{source_path}".encode()
                    ).hexdigest(),
                    sha256=(artifact.sha256 if artifact is not None else envelope.manifest_sha256),
                    byte_count=artifact.size if artifact is not None else 0,
                    status=status,
                    imported_job_id=job.id,
                    imported_document_id=session.scalar(
                        select(Document.id)
                        .where(Document.job_id == job.id)
                        .order_by(Document.id)
                        .limit(1)
                    ),
                    imported_at=datetime.now(UTC),
                )
            )

    def _update_device_status(
        self,
        session: Session,
        envelope: NormalizedEnvelope,
        job: PrintJob,
    ) -> None:
        observed = envelope.closed_at or envelope.opened_at
        response_seen = any(
            chunk.direction is StreamDirection.DEVICE_TO_CLIENT for chunk in envelope.chunks
        )
        pending = (
            session.scalar(
                select(func.count())
                .select_from(PrintJob)
                .where(
                    PrintJob.device_id == job.device_id,
                    PrintJob.import_status != "IMPORTED",
                )
            )
            or 0
        )
        spool_bytes = sum(artifact.size for artifact in envelope.artifacts)
        spool_metric_error: str | None = None
        if self._spool_root is not None:
            try:
                spool_bytes = _directory_size(self._spool_root / envelope.device_id)
            except (OSError, RuntimeError) as exc:
                # Observability is deliberately best-effort: a permission race or an
                # unusually large spool must never roll back an otherwise valid import.
                spool_metric_error = type(exc).__name__
        metrics = {
            "last_job_id": str(job.id),
            "captured_bytes": sum(artifact.size for artifact in envelope.artifacts),
            "chunks": len(envelope.chunks),
            "capture_complete": envelope.complete,
            "source_kind": envelope.source_kind.value,
        }
        if spool_metric_error is not None:
            metrics["spool_metric_error"] = spool_metric_error
        session.add(
            DeviceStatus(
                device_id=job.device_id,
                observed_at=observed,
                status="ONLINE" if envelope.status not in {"FAILED", "ERROR"} else "ERROR",
                last_connection_at=envelope.opened_at,
                last_print_at=envelope.closed_at or envelope.opened_at,
                last_response_at=observed if response_seen else None,
                spool_bytes=spool_bytes,
                pending_imports=pending,
                metrics=metrics,
                error=(
                    "; ".join(envelope.warnings)[:4096]
                    if envelope.status in {"FAILED", "ERROR"} and envelope.warnings
                    else None
                ),
            )
        )

    def _insert_envelope(self, session: Session, envelope: NormalizedEnvelope) -> PrintJob:
        device = session.scalar(
            select(Device).where(Device.external_id == envelope.device_id).with_for_update()
        )
        if device is None:
            raise ValueError(f"unknown configured device: {envelope.device_id}")
        scope = f"{envelope.device_endpoint.ip}:{envelope.device_endpoint.port}"
        source_session_id = (
            envelope.source_session_id or envelope.connection_id or f"job:{envelope.source_job_id}"
        )
        proxy_endpoint = envelope.proxy_endpoint
        source_endpoint = envelope.source_endpoint
        proxy_session = session.scalar(
            select(ProxySession).where(
                ProxySession.source_system == envelope.source_kind.value,
                ProxySession.source_instance == envelope.source_instance_id,
                ProxySession.source_scope == scope,
                ProxySession.source_session_id == source_session_id,
            )
        )
        if proxy_session is None:
            proxy_session = ProxySession(
                device_id=device.id,
                source_system=envelope.source_kind.value,
                source_instance=envelope.source_instance_id,
                source_scope=scope,
                source_session_id=source_session_id,
                client_ip=source_endpoint.ip if source_endpoint else None,
                client_port=source_endpoint.port if source_endpoint else None,
                listen_ip=(proxy_endpoint.ip if proxy_endpoint else device.listen_ip),
                listen_port=(proxy_endpoint.port if proxy_endpoint else device.listen_port),
                target_ip=envelope.device_endpoint.ip,
                target_port=envelope.device_endpoint.port,
                started_at=envelope.opened_at,
                ended_at=envelope.closed_at,
                status=envelope.status,
                client_fin_received=envelope.closed_at is not None,
                device_fin_received=envelope.closed_at is not None,
                bytes_to_device=sum(
                    chunk.byte_count
                    for chunk in envelope.chunks
                    if chunk.direction is StreamDirection.CLIENT_TO_DEVICE
                ),
                bytes_to_client=sum(
                    chunk.byte_count
                    for chunk in envelope.chunks
                    if chunk.direction is StreamDirection.DEVICE_TO_CLIENT
                ),
                capture_complete=envelope.complete,
            )
            session.add(proxy_session)
            session.flush()
        job = PrintJob(
            device_id=device.id,
            session_id=proxy_session.id,
            source_key=envelope.source_key,
            source_system=envelope.source_kind.value,
            source_instance=envelope.source_instance_id,
            source_scope=scope,
            source_job_id=envelope.source_job_id,
            source_schema=envelope.source_kind.value,
            manifest_sha256=envelope.manifest_sha256,
            manifest_path=_manifest_path(envelope),
            started_at=envelope.opened_at,
            ended_at=envelope.closed_at,
            captured_at=envelope.opened_at,
            boundary_source=envelope.boundary_source,
            boundary_confidence=(
                Decimal(str(envelope.boundary_confidence))
                if envelope.boundary_confidence is not None
                else None
            ),
            status=envelope.status,
            capture_complete=envelope.complete,
            timeline_complete=bool(envelope.chunks),
            import_status="IMPORTED",
            warnings=list(envelope.warnings),
            errors=[],
            imported_at=datetime.now(UTC),
        )
        session.add(job)
        session.flush()
        raw_payloads = self._insert_artifacts(session, envelope, device, proxy_session, job)
        self._insert_chunks(session, envelope, proxy_session, job, raw_payloads)
        self._insert_documents(session, envelope, device, proxy_session, job, raw_payloads)
        return job

    @staticmethod
    def _insert_artifacts(
        session: Session,
        envelope: NormalizedEnvelope,
        device: Device,
        proxy_session: ProxySession,
        job: PrintJob,
    ) -> dict[str, list[RawPayload]]:
        previous_hash = ZERO_HASH
        by_role: dict[str, list[RawPayload]] = {}
        for sequence, artifact in enumerate(envelope.artifacts, start=1):
            direction = _artifact_direction(artifact.role)
            chain_payload = {
                "job_id": str(job.id),
                "sequence": sequence,
                "role": artifact.role.value,
                "source_path": str(artifact.original_path),
                "sha256": artifact.sha256,
                "size": artifact.size,
                "complete": artifact.complete,
                "previous_hash": previous_hash,
            }
            record_hash = chained_hash(chain_payload, previous_hash)
            raw = RawPayload(
                job_id=job.id,
                device_id=device.id,
                session_id=proxy_session.id,
                artifact_role=artifact.role.value,
                direction=direction,
                payload=artifact.content,
                byte_count=artifact.size,
                sha256=artifact.sha256,
                source_path=str(artifact.original_path),
                source_path_sha256=hashlib.sha256(
                    str(artifact.original_path).encode("utf-8", errors="surrogatepass")
                ).hexdigest(),
                complete=artifact.complete,
                chain_scope=f"raw:{job.id}",
                chain_sequence=sequence,
                previous_record_hash=previous_hash,
                record_hash=record_hash,
            )
            session.add(raw)
            by_role.setdefault(artifact.role.value, []).append(raw)
            previous_hash = record_hash
        session.flush()
        return by_role

    @staticmethod
    def _insert_chunks(
        session: Session,
        envelope: NormalizedEnvelope,
        proxy_session: ProxySession,
        job: PrintJob,
        raw_payloads: dict[str, list[RawPayload]],
    ) -> None:
        previous_hash = ZERO_HASH
        direction_sequences: dict[str, int] = {}
        for fallback_observed_sequence, chunk in enumerate(envelope.chunks, start=1):
            direction = chunk.direction.value
            fallback_direction_sequence = direction_sequences.get(direction, 0) + 1
            direction_sequence = (
                chunk.direction_sequence
                if chunk.direction_sequence is not None
                else fallback_direction_sequence
            )
            observed_sequence = (
                chunk.observed_sequence
                if chunk.observed_sequence is not None
                else fallback_observed_sequence
            )
            direction_sequences[direction] = direction_sequence
            raw = _direction_raw(raw_payloads, chunk.direction)
            payload = _chunk_payload(raw, chunk.job_offset, chunk.byte_count, chunk.sha256)
            forwarded = chunk.forward_status not in {None, "FAILED", "ERROR"}
            chain_payload = {
                "job_id": str(job.id),
                "sequence": chunk.sequence,
                "observed_sequence": observed_sequence,
                "direction": direction,
                "direction_sequence": direction_sequence,
                "offset": chunk.job_offset,
                "byte_count": chunk.byte_count,
                "sha256": chunk.sha256,
                "forwarded": forwarded,
                "previous_hash": previous_hash,
            }
            record_hash = chained_hash(chain_payload, previous_hash)
            session.add(
                StreamChunk(
                    session_id=proxy_session.id,
                    job_id=job.id,
                    raw_payload_id=raw.id if raw else None,
                    sequence=chunk.sequence,
                    observed_sequence=observed_sequence,
                    direction_sequence=direction_sequence,
                    direction=direction,
                    event_kind=chunk.event_kind,
                    direction_offset=chunk.job_offset,
                    session_offset=chunk.session_offset,
                    received_at=chunk.received_at,
                    received_unix_ns=chunk.received_unix_ns,
                    monotonic_ns=chunk.monotonic_ns,
                    forwarded_at=chunk.forwarded_at
                    or (
                        datetime.fromtimestamp(chunk.forwarded_unix_ns / 1_000_000_000, UTC)
                        if chunk.forwarded_unix_ns is not None
                        else None
                    ),
                    local_drain_at=(
                        datetime.fromtimestamp(chunk.local_write_drain_unix_ns / 1_000_000_000, UTC)
                        if chunk.local_write_drain_unix_ns is not None
                        else None
                    ),
                    byte_count=chunk.byte_count,
                    sha256=chunk.sha256,
                    payload=payload,
                    forwarded=forwarded,
                    forward_status=chunk.forward_status or "UNKNOWN",
                    forward_error=chunk.error,
                    previous_record_hash=previous_hash,
                    record_hash=record_hash,
                )
            )
            previous_hash = record_hash

    @staticmethod
    def _insert_documents(
        session: Session,
        envelope: NormalizedEnvelope,
        device: Device,
        proxy_session: ProxySession,
        job: PrintJob,
        raw_payloads: dict[str, list[RawPayload]],
    ) -> None:
        if not envelope.documents:
            return
        parser_name = envelope.source_kind.value
        parser_version_value = envelope.parser_version or "unknown"
        parser_build_sha256 = hashlib.sha256(
            f"{parser_name}:{parser_version_value}".encode()
        ).hexdigest()
        parser = session.scalar(
            select(ParserVersion).where(
                ParserVersion.name == parser_name,
                ParserVersion.version == parser_version_value,
                ParserVersion.build_sha256 == parser_build_sha256,
            )
        )
        if parser is None:
            parser = ParserVersion(
                name=parser_name,
                version=parser_version_value,
                build_sha256=parser_build_sha256,
                protocol=envelope.source_kind.value,
                configuration={},
            )
            session.add(parser)
            session.flush()
        request_raw = (raw_payloads.get(ArtifactRole.REQUEST_RAW.value) or [None])[0]
        previous_hash = ZERO_HASH
        for sequence, parsed in enumerate(envelope.documents, start=1):
            semantic = parsed.semantic
            document = Document(
                device_id=device.id,
                session_id=proxy_session.id,
                job_id=job.id,
                source_document_key=parsed.external_id,
                document_type=parsed.document_type.value,
                subtype=parsed.subtype or parsed.document_type.value,
                external_document_code=_optional_text(
                    _first(
                        semantic,
                        "external_document_code",
                        "document_code",
                        "document_number",
                        "number",
                    )
                ),
                order_code=_optional_text(
                    _first(semantic, "order_code", "order_reference", "order_number")
                ),
                table_code=_optional_text(_first(semantic, "table_code", "table", "table_number")),
                operator_code=_optional_text(
                    _first(semantic, "operator_code", "operator", "cashier")
                ),
                terminal_code=_optional_text(
                    _first(semantic, "terminal_code", "terminal", "register")
                ),
                document_timestamp=parsed.capture_time,
                captured_at=parsed.capture_time or envelope.opened_at,
            )
            session.add(document)
            session.flush()
            normalized_text = _text(
                _first(semantic, "normalized_text", "text", "clean_text"),
                default="",
            )
            source_hash = request_raw.sha256 if request_raw else envelope.manifest_sha256
            version_payload = {
                "document_id": str(document.id),
                "sequence": sequence,
                "parser": f"{parser_name}:{parser_version_value}",
                "source_sha256": source_hash,
                "semantic": semantic,
                "previous_hash": previous_hash,
            }
            record_hash = chained_hash(version_payload, previous_hash)
            version = DocumentVersion(
                document_id=document.id,
                parser_version_id=parser.id,
                raw_payload_id=request_raw.id if request_raw else None,
                version_sequence=1,
                parsed_at=datetime.now(UTC),
                gross_total=_as_decimal(_first(semantic, "gross_total", "total", "total_amount")),
                net_total=_as_decimal(_first(semantic, "net_total", "taxable_total")),
                discount_total=_as_decimal(_first(semantic, "discount_total", "discount")),
                tax_total=_as_decimal(_first(semantic, "tax_total", "vat_total", "tax")),
                payment_method=_optional_text(_first(semantic, "payment_method", "payment")),
                status="COMPLETE" if parsed.complete else "PARTIAL",
                normalized_text=normalized_text,
                encoding=_optional_text(_first(semantic, "encoding")),
                parse_confidence=_confidence(semantic, parsed.evidence),
                evidence_level=parsed.evidence,
                source_manifest_sha256=envelope.manifest_sha256,
                source_payload_sha256=source_hash,
                source_path=(request_raw.source_path if request_raw else job.manifest_path),
                complete=parsed.complete,
                warnings=list(parsed.warnings),
                errors=list(semantic.get("issues", [])),
                raw_metadata={
                    **semantic,
                    "source_start_offset": parsed.source_start_offset,
                    "source_end_offset": parsed.source_end_offset,
                    "source_frame_ids": list(parsed.source_frame_ids),
                    "timezone": parsed.timezone,
                },
                chain_scope=f"document:{job.id}",
                chain_sequence=sequence,
                previous_record_hash=previous_hash,
                record_hash=record_hash,
            )
            session.add(version)
            session.flush()
            _insert_semantic_lines(session, version, semantic)
            _insert_semantic_payments(session, version, semantic)
            previous_hash = record_hash

    def record_retry(self, retry: RetryRecord) -> None:
        self._record_system_event(
            event_type="INGESTION_RETRY",
            severity="WARNING",
            message=f"Retry import {retry.source_key}",
            details={
                "source_key": retry.source_key,
                "attempt": retry.attempt,
                "delay_seconds": retry.delay_seconds,
                "error": retry.error,
            },
        )

    def quarantine(self, record: QuarantineRecord) -> None:
        self._record_system_event(
            event_type="INGESTION_QUARANTINE",
            severity="ERROR",
            message=f"Evidence quarantined: {record.candidate_key}",
            details={
                "source_instance_id": record.source_instance_id,
                "candidate_key": record.candidate_key,
                "source_path": str(record.source_path),
                "source_kind": record.source_kind,
                "reason": record.reason,
            },
        )

    def _record_system_event(
        self, *, event_type: str, severity: str, message: str, details: dict[str, Any]
    ) -> None:
        session = self._factory()
        try:
            with session.begin():
                session.add(
                    SystemEvent(
                        service="ingestion",
                        severity=severity,
                        event_type=event_type,
                        message=message,
                        details=details,
                    )
                )
        except SQLAlchemyError as exc:
            raise IngestionUnavailable("cannot persist ingestion event") from exc
        finally:
            session.close()


def _manifest_path(envelope: NormalizedEnvelope) -> str:
    for artifact in envelope.artifacts:
        if artifact.role is ArtifactRole.CAPTURE_MANIFEST:
            return str(artifact.original_path)
    return str(envelope.metadata.get("source_path", envelope.source_key))


def _artifact_direction(role: ArtifactRole) -> str:
    if role is ArtifactRole.REQUEST_RAW:
        return StreamDirection.CLIENT_TO_DEVICE.value
    if role in {ArtifactRole.RESPONSE_RAW, ArtifactRole.RESPONSE_PREVIEW}:
        return StreamDirection.DEVICE_TO_CLIENT.value
    return "METADATA"


def _direction_raw(
    raw_payloads: dict[str, list[RawPayload]], direction: StreamDirection
) -> RawPayload | None:
    role = (
        ArtifactRole.REQUEST_RAW.value
        if direction is StreamDirection.CLIENT_TO_DEVICE
        else ArtifactRole.RESPONSE_RAW.value
    )
    candidates = raw_payloads.get(role) or []
    return candidates[0] if candidates else None


def _chunk_payload(
    raw: RawPayload | None,
    offset: int,
    byte_count: int,
    expected_sha256: str,
) -> bytes:
    if byte_count == 0:
        return b""
    if raw is None:
        raise ValueError("non-empty timeline chunk has no corresponding RAW artifact")
    if offset < 0 or byte_count < 0 or offset + byte_count > len(raw.payload):
        raise ValueError("timeline chunk range lies outside corresponding RAW artifact")
    payload = raw.payload[offset : offset + byte_count]
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("timeline chunk hash differs from corresponding RAW bytes")
    return payload


def _optional_text(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _confidence(semantic: dict[str, Any], evidence: str) -> int:
    value = _first(semantic, "parse_confidence", "confidence")
    if value is not None:
        try:
            decimal_value = Decimal(str(value))
            if Decimal("0") <= decimal_value <= Decimal("1"):
                decimal_value *= Decimal("100")
            return min(100, max(0, int(decimal_value)))
        except (InvalidOperation, TypeError, ValueError):
            pass
    normalized = evidence.upper()
    if "CONFIRMED" in normalized and "UNCONFIRMED" not in normalized:
        return 100
    if "INFERRED" in normalized:
        return 65
    return 25


def _insert_semantic_lines(
    session: Session, version: DocumentVersion, semantic: dict[str, Any]
) -> None:
    raw_lines = semantic.get("lines") or semantic.get("items") or []
    if not isinstance(raw_lines, list):
        return
    for sequence, value in enumerate(raw_lines, start=1):
        if not isinstance(value, dict):
            continue
        session.add(
            DocumentLine(
                document_version_id=version.id,
                sequence=int(value.get("sequence", sequence)),
                item_code=_optional_text(_first(value, "item_code", "code", "sku")),
                description=_optional_text(_first(value, "description", "name", "text")),
                quantity=_as_decimal(value.get("quantity")),
                unit_price=_as_decimal(_first(value, "unit_price", "price")),
                original_unit_price=_as_decimal(value.get("original_unit_price")),
                modified_unit_price=_as_decimal(value.get("modified_unit_price")),
                discount=_as_decimal(value.get("discount")),
                surcharge=_as_decimal(value.get("surcharge")),
                tax_rate=_as_decimal(_first(value, "tax_rate", "vat_rate")),
                line_total=_as_decimal(_first(value, "line_total", "total")),
                line_state=_optional_text(_first(value, "state", "status")),
                cancelled=bool(value.get("cancelled", False)),
                removed=bool(value.get("removed", False)),
                raw_text=_optional_text(value.get("raw_text")),
                source_direction=_optional_text(value.get("source_direction")),
                source_offset=_integer_or_none(value.get("source_offset")),
                source_length=_integer_or_none(value.get("source_length")),
                source_frame_id=_optional_text(value.get("source_frame_id")),
            )
        )


def _insert_semantic_payments(
    session: Session, version: DocumentVersion, semantic: dict[str, Any]
) -> None:
    raw_payments = semantic.get("payments") or []
    if isinstance(raw_payments, dict):
        raw_payments = [raw_payments]
    if not isinstance(raw_payments, list):
        return
    for value in raw_payments:
        if not isinstance(value, dict) or _as_decimal(value.get("amount")) is None:
            continue
        session.add(
            Payment(
                document_version_id=version.id,
                external_payment_code=_optional_text(value.get("code")),
                method=_optional_text(_first(value, "method", "type")),
                amount=_as_decimal(value.get("amount")),
                currency=_text(value.get("currency"), "EUR"),
                status=_text(value.get("status"), "RECORDED"),
                evidence_level=_text(value.get("evidence"), "UNKNOWN"),
                paid_at=_payment_time(value.get("paid_at")),
            )
        )


def _payment_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value, datetime.now(UTC))
    try:
        return _aware(datetime.fromisoformat(str(value)), datetime.now(UTC))
    except ValueError:
        return None


def _integer_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _directory_size(root: Path, *, maximum_entries: int = 1_000_000) -> int:
    """Measure a spool subtree without following symlinks."""

    if not root.is_dir() or root.is_symlink():
        return 0
    total = 0
    pending = [root]
    visited = 0
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    visited += 1
                    if visited > maximum_entries:
                        raise RuntimeError("spool metric scan exceeds bounded entry limit")
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except FileNotFoundError:
            continue
    return total


def _spool_snapshot(root: Path, *, maximum_entries: int = 1_000_000) -> dict[str, Any]:
    """Return bounded canonical spool metrics without following links."""

    if not root.is_dir() or root.is_symlink():
        return {"bytes": 0, "ready_job_ids": set(), "ready_jobs": 0, "partial_jobs": 0}
    total = 0
    ready_job_ids: set[str] = set()
    ready_jobs = 0
    partial_jobs = 0
    pending = [root]
    visited = 0
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    visited += 1
                    if visited > maximum_entries:
                        raise RuntimeError("spool metric scan exceeds bounded entry limit")
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.endswith(".partial"):
                            partial_jobs += 1
                        pending.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    total += stat.st_size
                    if entry.name != ".ready":
                        continue
                    ready_jobs += 1
                    if stat.st_size > 65_536:
                        raise ValueError("canonical .ready marker exceeds 64 KiB")
                    with Path(entry.path).open("rb") as marker_file:
                        marker = json.loads(marker_file.read().decode("utf-8"))
                    job_id = marker.get("job_id") if isinstance(marker, dict) else None
                    if isinstance(job_id, str) and 0 < len(job_id) <= 191:
                        ready_job_ids.add(job_id)
        except FileNotFoundError:
            continue
    return {
        "bytes": total,
        "ready_job_ids": ready_job_ids,
        "ready_jobs": ready_jobs,
        "partial_jobs": partial_jobs,
    }


def _sync_devices(factory: sessionmaker[Session], settings: Settings) -> None:
    session = factory()
    try:
        with session.begin():
            for configured in settings.devices:
                device = session.scalar(
                    select(Device).where(Device.external_id == configured.id).with_for_update()
                )
                values = {
                    "name": configured.name,
                    "device_type": configured.type.value,
                    "parser_kind": configured.parser,
                    "enabled": configured.enabled,
                    "bidirectional": configured.bidirectional,
                    "listen_ip": str(configured.listen_ip),
                    "listen_port": configured.listen_port,
                    "target_ip": str(configured.target_ip),
                    "target_port": configured.target_port,
                }
                if device is None:
                    session.add(Device(external_id=configured.id, **values))
                else:
                    for key, value in values.items():
                        setattr(device, key, value)
    except SQLAlchemyError as exc:
        raise IngestionUnavailable("cannot synchronize configured devices") from exc
    finally:
        session.close()


def create_api_repository(settings: Settings) -> SqlAlchemyApiRepository:
    return SqlAlchemyApiRepository.from_url(settings.database_url().get_secret_value())


def create_ingestion_repository(settings: Settings) -> SqlAlchemyIngestionRepository:
    factory = session_factory(create_db_engine(settings.database_url().get_secret_value()))
    _sync_devices(factory, settings)
    return SqlAlchemyIngestionRepository(factory, spool_root=settings.spool_root)


__all__ = [
    "SqlAlchemyApiRepository",
    "SqlAlchemyIngestionRepository",
    "create_api_repository",
    "create_ingestion_repository",
]
