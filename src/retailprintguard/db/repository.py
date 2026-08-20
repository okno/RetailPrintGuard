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

from sqlalchemy import Select, and_, case, func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, aliased, sessionmaker

from retailprintguard.api.auth import PasswordService
from retailprintguard.api.repository import RawArtifact, RepositoryUnavailable
from retailprintguard.api.schemas import (
    AlertUpdate,
    AlertView,
    AuditEntry,
    DashboardView,
    DeviceView,
    DiagnosticsView,
    DocumentLineView,
    DocumentView,
    ImportBatchView,
    JobReviewRequest,
    JobView,
    LinePriceAttributionView,
    OrderView,
    RoleName,
    RuleView,
    SearchHit,
    SessionView,
    SystemEventView,
    TransactionView,
    UserPrincipal,
)
from retailprintguard.common.config import Settings
from retailprintguard.common.domain import AlertStatus, DocumentType
from retailprintguard.common.hashchain import ZERO_HASH, chained_hash
from retailprintguard.db.models import (
    ActiveParserVersion,
    AnalysisWatermark,
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
    LinePriceAttribution,
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
from retailprintguard.pricing.service import PRICE_ATTRIBUTION_ALGORITHM
from retailprintguard.render.text import receipt_text

MONEY_ZERO = Decimal("0.0000")
DEVICE_ACTIVITY_STALE_AFTER = timedelta(minutes=10)
DEFAULT_SPOOL_METRIC_STALE_AFTER = timedelta(minutes=10)
DEFAULT_SPOOL_WARNING_BYTES = 1_073_741_824
SALE_FISCAL_TYPES = {DocumentType.COMMERCIAL_DOCUMENT.value}
OPERATIONAL_ALERT_STATES = ("OPEN", "UNDER_REVIEW", "CONFIRMED")
ARCHIVED_ALERT_STATES = ("FALSE_POSITIVE", "JUSTIFIED", "CLOSED")
PRIMARY_ECONOMIC_REDUCTION_RULE = "MODIFICA_POST_PRECONTO"
COMPAT_ECONOMIC_REDUCTION_RULE = "PREBILL_FISCAL_AMOUNT_DROP"
ECONOMIC_REDUCTION_RULE_CODES = (
    PRIMARY_ECONOMIC_REDUCTION_RULE,
    COMPAT_ECONOMIC_REDUCTION_RULE,
)
SOURCE_TYPES = {
    DocumentType.ORDER.value,
    DocumentType.ORDER_CHANGE.value,
    DocumentType.KITCHEN_ORDER.value,
    DocumentType.PRE_BILL.value,
    DocumentType.MANAGEMENT_DOCUMENT.value,
}

_VERSION_SEMANTIC_FIELDS = (
    "document_type",
    "subtype",
    "external_document_code",
    "external_document_code_suffix",
    "commercial_reference_code",
    "order_code",
    "table_code",
    "operator_code",
    "terminal_code",
    "document_timestamp",
)


def _page(limit: int, offset: int, *, maximum: int = 10_000) -> tuple[int, int]:
    if not 1 <= limit <= maximum or offset < 0:
        raise ValueError("invalid pagination")
    return limit, offset


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


def _version_value(version: DocumentVersion, document: Document, field: str) -> Any:
    """Read immutable semantics, falling back only for a wholly legacy row."""

    if all(getattr(version, name) is None for name in _VERSION_SEMANTIC_FIELDS):
        return getattr(document, field)
    return getattr(version, field)


def _version_column(version_column: Any, legacy_column: Any) -> Any:
    """SQL equivalent of :func:`_version_value` without cross-version leakage."""

    return case(
        (
            and_(
                *(
                    getattr(DocumentVersion, name).is_(None)
                    for name in _VERSION_SEMANTIC_FIELDS
                )
            ),
            legacy_column,
        ),
        else_=version_column,
    )


def _progressive_observation_status(
    version: DocumentVersion,
    document: Document,
) -> str:
    metadata = version.raw_metadata if isinstance(version.raw_metadata, dict) else {}
    explicit = metadata.get("progressive_observation_status")
    if explicit in {
        "FULL_CODE_OBSERVED_IN_CAPTURE",
        "SUFFIX_ONLY_OBSERVED_IN_CAPTURE",
        "NOT_OBSERVED_IN_CAPTURE",
        "NOT_APPLICABLE",
    }:
        return str(explicit)
    if _version_value(version, document, "external_document_code"):
        return "FULL_CODE_OBSERVED_IN_CAPTURE"
    if _version_value(version, document, "external_document_code_suffix"):
        return "SUFFIX_ONLY_OBSERVED_IN_CAPTURE"
    if _version_value(version, document, "document_type") in {
        DocumentType.PRE_BILL.value,
        DocumentType.MANAGEMENT_DOCUMENT.value,
        DocumentType.CONFORMING_COPY.value,
        DocumentType.COMMERCIAL_DOCUMENT.value,
    }:
        return "NOT_OBSERVED_IN_CAPTURE"
    return "NOT_APPLICABLE"


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _as_nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


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


def _latest_transaction_occurrences() -> Any:
    """Return one sale occurrence for each current transaction projection.

    The occurrence is derived from the immutable document evidence belonging to
    the latest non-superseded correlation.  It intentionally does not use the
    time at which a fraud worker happened to open or re-evaluate an alert.
    """

    ranked = (
        select(
            DocumentCorrelation.id.label("correlation_id"),
            DocumentCorrelation.transaction_id.label("transaction_id"),
            func.row_number()
            .over(
                partition_by=DocumentCorrelation.transaction_id,
                order_by=(
                    DocumentCorrelation.created_at.desc(),
                    DocumentCorrelation.id.desc(),
                ),
            )
            .label("row_number"),
        )
        .where(DocumentCorrelation.status != "SUPERSEDED")
        .subquery()
    )
    return (
        select(
            ranked.c.correlation_id,
            ranked.c.transaction_id,
            func.min(
                func.coalesce(
                    _version_column(
                        DocumentVersion.document_timestamp,
                        Document.document_timestamp,
                    ),
                    Document.captured_at,
                )
            ).label("occurred_at"),
        )
        .select_from(ranked)
        .join(
            DocumentCorrelationMember,
            DocumentCorrelationMember.correlation_id == ranked.c.correlation_id,
        )
        .join(Document, Document.id == DocumentCorrelationMember.document_id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(
            ranked.c.row_number == 1,
            DocumentVersion.id == _latest_version_id(),
        )
        .group_by(ranked.c.correlation_id, ranked.c.transaction_id)
        .subquery()
    )


def _historical_correlation_occurrences() -> Any:
    """Return immutable occurrence timestamps for every correlation revision."""

    return (
        select(
            DocumentCorrelationMember.correlation_id.label("correlation_id"),
            func.min(
                func.coalesce(
                    _version_column(
                        DocumentVersion.document_timestamp,
                        Document.document_timestamp,
                    ),
                    Document.captured_at,
                )
            ).label("occurred_at"),
        )
        .join(Document, Document.id == DocumentCorrelationMember.document_id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(DocumentVersion.id == _latest_version_id())
        .group_by(DocumentCorrelationMember.correlation_id)
        .subquery()
    )


def _alert_has_device(device_external_id: str) -> Any:
    """Build an isolated membership predicate for an alert's correlated devices."""

    member = aliased(DocumentCorrelationMember, name="alert_device_member")
    document = aliased(Document, name="alert_device_document")
    device = aliased(Device, name="alert_device")
    return (
        select(1)
        .select_from(member)
        .join(document, document.id == member.document_id)
        .join(device, device.id == document.device_id)
        .where(
            member.correlation_id == FraudAlert.correlation_id,
            device.external_id == device_external_id,
        )
        .correlate(FraudAlert)
        .exists()
    )


def _alert_has_operator(operator_code: str) -> Any:
    """Match an operator from the active immutable parser version.

    The mutable ``documents`` projection is consulted only when the selected
    version is a wholly legacy row, mirroring :func:`_version_value` without
    allowing values produced by an inactive shadow parser to leak into filters.
    All aliases are local to the ``EXISTS`` predicate so it can be combined with
    the independent device predicate without duplicate JOIN names.
    """

    member = aliased(DocumentCorrelationMember, name="alert_operator_member")
    document = aliased(Document, name="alert_operator_document")
    version = aliased(DocumentVersion, name="alert_operator_version")
    candidate = aliased(DocumentVersion, name="alert_operator_candidate")
    parser = aliased(ParserVersion, name="alert_operator_parser")
    active = aliased(ActiveParserVersion, name="alert_operator_active_parser")
    selected_version_id = (
        select(candidate.id)
        .join(parser, parser.id == candidate.parser_version_id)
        .outerjoin(active, active.parser_name == parser.name)
        .where(candidate.document_id == document.id)
        .order_by(
            (active.parser_version_id == candidate.parser_version_id).desc(),
            candidate.version_sequence.desc(),
            candidate.id.desc(),
        )
        .limit(1)
        .correlate(document)
        .scalar_subquery()
    )
    effective_operator = case(
        (
            and_(
                *(
                    getattr(version, field).is_(None)
                    for field in _VERSION_SEMANTIC_FIELDS
                )
            ),
            document.operator_code,
        ),
        else_=version.operator_code,
    )
    return (
        select(1)
        .select_from(member)
        .join(document, document.id == member.document_id)
        .join(
            version,
            and_(
                version.document_id == document.id,
                version.id == selected_version_id,
            ),
        )
        .where(
            member.correlation_id == FraudAlert.correlation_id,
            effective_operator == operator_code,
        )
        .correlate(FraudAlert)
        .exists()
    )


class SqlAlchemyApiRepository:
    """Concrete, synchronous API repository backed by MariaDB/SQLAlchemy 2."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        spool_metric_stale_after: timedelta = DEFAULT_SPOOL_METRIC_STALE_AFTER,
        spool_warning_bytes: int = DEFAULT_SPOOL_WARNING_BYTES,
    ) -> None:
        if spool_metric_stale_after <= timedelta(0):
            raise ValueError("spool metric freshness window must be positive")
        if spool_warning_bytes <= 0:
            raise ValueError("spool warning threshold must be positive")
        self._factory = factory
        self._passwords = PasswordService()
        self._spool_metric_stale_after = spool_metric_stale_after
        self._spool_warning_bytes = spool_warning_bytes

    @classmethod
    def from_url(
        cls,
        database_url: str,
        *,
        spool_metric_stale_after: timedelta = DEFAULT_SPOOL_METRIC_STALE_AFTER,
        spool_warning_bytes: int = DEFAULT_SPOOL_WARNING_BYTES,
    ) -> SqlAlchemyApiRepository:
        return cls(
            session_factory(create_db_engine(database_url)),
            spool_metric_stale_after=spool_metric_stale_after,
            spool_warning_bytes=spool_warning_bytes,
        )

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

    def dashboard(self, *, filters: dict[str, Any] | None = None) -> DashboardView:
        filters = filters or {}
        with self._read() as session:
            document_period = []
            occurrence = _latest_transaction_occurrences()
            archive_occurrence = _historical_correlation_occurrences()
            episode_period = []
            archive_period = []
            if filters.get("from") is not None:
                document_period.append(Document.captured_at >= filters["from"])
                episode_period.append(occurrence.c.occurred_at >= filters["from"])
                archive_period.append(archive_occurrence.c.occurred_at >= filters["from"])
            if filters.get("to") is not None:
                document_period.append(Document.captured_at < filters["to"])
                episode_period.append(occurrence.c.occurred_at < filters["to"])
                archive_period.append(archive_occurrence.c.occurred_at < filters["to"])
            semantic_type = _version_column(
                DocumentVersion.document_type,
                Document.document_type,
            ).label("document_type")
            selected_documents = (
                select(semantic_type)
                .select_from(DocumentVersion)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(DocumentVersion.id == _latest_version_id(), *document_period)
                .subquery()
            )
            type_counts = dict(
                session.execute(
                    select(selected_documents.c.document_type, func.count()).group_by(
                        selected_documents.c.document_type
                    )
                ).all()
            )
            documents = sum(
                count
                for document_type, count in type_counts.items()
                if document_type != DocumentType.DEVICE_RESPONSE.value
            )
            orders = sum(
                type_counts.get(document_type.value, 0)
                for document_type in (
                    DocumentType.ORDER,
                    DocumentType.ORDER_CHANGE,
                    DocumentType.KITCHEN_ORDER,
                )
            )
            open_alerts = (
                session.scalar(
                    select(func.count())
                    .select_from(FraudAlert)
                    .join(
                        occurrence,
                        occurrence.c.transaction_id == FraudAlert.transaction_id,
                    )
                    .where(
                        FraudAlert.is_canonical.is_(True),
                        FraudAlert.status.in_(OPERATIONAL_ALERT_STATES),
                        *episode_period,
                    )
                )
                or 0
            )
            critical = (
                session.scalar(
                    select(func.count())
                    .select_from(FraudAlert)
                    .join(
                        occurrence,
                        occurrence.c.transaction_id == FraudAlert.transaction_id,
                    )
                    .where(
                        FraudAlert.is_canonical.is_(True),
                        FraudAlert.status.in_(OPERATIONAL_ALERT_STATES),
                        FraudAlert.severity == "CRITICAL",
                        *episode_period,
                    )
                )
                or 0
            )
            economic_candidates = (
                select(
                    FraudAlert.transaction_id.label("transaction_id"),
                    func.max(
                        case(
                            (
                                FraudRule.code == PRIMARY_ECONOMIC_REDUCTION_RULE,
                                FraudAlert.difference_amount,
                            ),
                            else_=None,
                        )
                    ).label("primary_difference"),
                    func.max(
                        case(
                            (
                                FraudRule.code == COMPAT_ECONOMIC_REDUCTION_RULE,
                                FraudAlert.difference_amount,
                            ),
                            else_=None,
                        )
                    ).label("compat_difference"),
                )
                .select_from(FraudAlert)
                .join(
                    FraudRuleVersion,
                    FraudRuleVersion.id == FraudAlert.fraud_rule_version_id,
                )
                .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
                .join(
                    occurrence,
                    occurrence.c.transaction_id == FraudAlert.transaction_id,
                )
                .where(
                    FraudAlert.is_canonical.is_(True),
                    FraudAlert.status.in_(OPERATIONAL_ALERT_STATES),
                    FraudAlert.transaction_id.is_not(None),
                    FraudAlert.difference_amount > MONEY_ZERO,
                    FraudRule.code.in_(ECONOMIC_REDUCTION_RULE_CODES),
                    *episode_period,
                )
                .group_by(FraudAlert.transaction_id)
                .subquery()
            )
            economic_difference = func.coalesce(
                economic_candidates.c.primary_difference,
                economic_candidates.c.compat_difference,
            )
            economic_episodes = (
                select(
                    economic_candidates.c.transaction_id,
                    economic_difference.label("difference_amount"),
                )
                .where(economic_difference > MONEY_ZERO)
                .subquery()
            )
            economic_episode_count = (
                session.scalar(select(func.count()).select_from(economic_episodes)) or 0
            )
            economic = (
                session.scalar(
                    select(func.coalesce(func.sum(economic_episodes.c.difference_amount), 0))
                )
                or MONEY_ZERO
            )
            false_positives = (
                session.scalar(
                    select(func.count())
                    .select_from(FraudAlert)
                    .join(
                        archive_occurrence,
                        archive_occurrence.c.correlation_id == FraudAlert.correlation_id,
                    )
                    .where(
                        FraudAlert.is_canonical.is_(True),
                        FraudAlert.status == "FALSE_POSITIVE",
                        *archive_period,
                    )
                )
                or 0
            )
            justified = (
                session.scalar(
                    select(func.count())
                    .select_from(FraudAlert)
                    .join(
                        archive_occurrence,
                        archive_occurrence.c.correlation_id == FraudAlert.correlation_id,
                    )
                    .where(
                        FraudAlert.is_canonical.is_(True),
                        FraudAlert.status == "JUSTIFIED",
                        *archive_period,
                    )
                )
                or 0
            )
            incomplete_period = []
            if filters.get("from") is not None:
                incomplete_period.append(PrintJob.captured_at >= filters["from"])
            if filters.get("to") is not None:
                incomplete_period.append(PrintJob.captured_at < filters["to"])
            incomplete_jobs = (
                session.scalar(
                    select(func.count())
                    .select_from(PrintJob)
                    .where(
                        self._incomplete_job_predicate(),
                        PrintJob.review_state == "PENDING",
                        *incomplete_period,
                    )
                )
                or 0
            )
            device_count = session.scalar(select(func.count()).select_from(Device)) or 0
            latest_status = self._latest_device_statuses(session)
            now = datetime.now(UTC)
            online = sum(
                self._is_device_online(status, now=now) for status in latest_status.values()
            )
            spool_bytes = sum(status.spool_bytes for status in latest_status.values())
            dialect_name = session.get_bind().dialect.name
            json_length = (
                func.json_length(DocumentVersion.errors)
                if dialect_name in {"mysql", "mariadb"}
                else func.json_array_length(DocumentVersion.errors)
            )
            parse_errors = (
                session.scalar(
                    select(func.count())
                    .select_from(DocumentVersion)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(
                        DocumentVersion.id == _latest_version_id(),
                        func.coalesce(json_length, 0) > 0,
                        *document_period,
                    )
                )
                or 0
            )
            trend = [
                {"date": str(day), "count": count}
                for day, count in session.execute(
                    select(func.date(occurrence.c.occurred_at), func.count())
                    .select_from(FraudAlert)
                    .join(
                        occurrence,
                        occurrence.c.transaction_id == FraudAlert.transaction_id,
                    )
                    .where(
                        FraudAlert.is_canonical.is_(True),
                        FraudAlert.status.in_(OPERATIONAL_ALERT_STATES),
                        *episode_period,
                    )
                    .group_by(func.date(occurrence.c.occurred_at))
                    .order_by(func.date(occurrence.c.occurred_at).desc())
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
                operational_alerts=open_alerts,
                economic_reduction_episodes=economic_episode_count,
                operational_economic_difference=economic,
                incomplete_jobs=incomplete_jobs,
                false_positive_alerts=false_positives,
                justified_alerts=justified,
            )

    def diagnostics(self) -> DiagnosticsView:
        with self._read() as session:
            dialect_name = session.get_bind().dialect.name
            json_length = (
                func.json_length(DocumentVersion.errors)
                if dialect_name in {"mysql", "mariadb"}
                else func.json_array_length(DocumentVersion.errors)
            )
            parser_errors = (
                session.scalar(
                    select(func.count())
                    .select_from(DocumentVersion)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(
                        DocumentVersion.id == _latest_version_id(),
                        func.coalesce(json_length, 0) > 0,
                    )
                )
                or 0
            )
            parser_errors += (
                session.scalar(
                    select(func.count())
                    .select_from(PrintJob)
                    .where(PrintJob.import_status.in_(("PARSE_RETRY", "PARSE_FAILED")))
                )
                or 0
            )
            incomplete_jobs = (
                session.scalar(
                    select(func.count())
                    .select_from(PrintJob)
                    .where(
                        or_(
                            self._incomplete_job_predicate(),
                            PrintJob.import_status.in_(("PARSE_RETRY", "PARSE_FAILED")),
                        ),
                        PrintJob.review_state == "PENDING",
                    )
                )
                or 0
            )
            events = session.scalars(
                select(SystemEvent)
                .order_by(SystemEvent.occurred_at.desc(), SystemEvent.id.desc())
                .limit(100)
            ).all()
            return DiagnosticsView(
                generated_at=datetime.now(UTC),
                spool=self._spool_health(session, now=datetime.now(UTC)),
                parser_errors=parser_errors,
                incomplete_jobs=incomplete_jobs,
                recent_events=[
                    SystemEventView(
                        id=event.id,
                        service=event.service,
                        severity=event.severity,
                        event_type=event.event_type,
                        message=event.message,
                        device_id=event.device_id,
                        session_id=event.session_id,
                        job_id=event.job_id,
                        correlation_id=event.correlation_id,
                        occurred_at=event.occurred_at,
                        error=event.error,
                    )
                    for event in events
                ],
            )

    def _spool_health(self, session: Session, *, now: datetime) -> str:
        enabled_device_ids = set(
            session.scalars(select(Device.id).where(Device.enabled.is_(True))).all()
        )
        if not enabled_device_ids:
            return "unknown"

        statuses = self._latest_device_statuses(session)
        total_bytes = 0
        has_partial_jobs = False
        for device_id in enabled_device_ids:
            status = statuses.get(device_id)
            if status is None:
                return "unknown"
            metrics = status.metrics if isinstance(status.metrics, dict) else {}
            if metrics.get("spool_metric_error"):
                return "degraded"
            scanned_value = metrics.get("spool_scanned_at")
            if not isinstance(scanned_value, str):
                return "unknown"
            try:
                scanned_at = datetime.fromisoformat(scanned_value)
            except ValueError:
                return "unknown"
            if scanned_at.tzinfo is None:
                return "unknown"
            if scanned_at.astimezone(UTC) < now - self._spool_metric_stale_after:
                return "unknown"
            total_bytes += max(0, status.spool_bytes)
            try:
                has_partial_jobs = has_partial_jobs or int(metrics.get("partial_jobs", 0)) > 0
            except (TypeError, ValueError):
                return "unknown"

        if has_partial_jobs or total_bytes >= self._spool_warning_bytes:
            return "degraded"
        return "ok"

    def spool_health(self) -> str:
        try:
            with self._read() as session:
                return self._spool_health(session, now=datetime.now(UTC))
        except RepositoryUnavailable:
            return "unknown"

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

    @staticmethod
    def _is_device_online(status: DeviceStatus, *, now: datetime) -> bool:
        return bool(
            status.status.upper() == "ONLINE"
            and status.last_connection_at is not None
            and status.last_connection_at >= now - DEVICE_ACTIVITY_STALE_AFTER
        )

    def list_devices(self) -> Sequence[DeviceView]:
        with self._read() as session:
            statuses = self._latest_device_statuses(session)
            devices = session.scalars(select(Device).order_by(Device.external_id)).all()
            now = datetime.now(UTC)
            return tuple(
                DeviceView(
                    id=device.external_id,
                    name=device.name,
                    type=device.device_type,
                    mac_address=device.mac_address,
                    department=device.department,
                    role=device.role,
                    enabled=device.enabled,
                    online=(
                        device.id in statuses
                        and self._is_device_online(statuses[device.id], now=now)
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

    @staticmethod
    def _incomplete_job_predicate() -> Any:
        return or_(
            PrintJob.capture_complete.is_(False),
            PrintJob.timeline_complete.is_(False),
            PrintJob.status.in_(("FAILED", "INCOMPLETE", "MALFORMED")),
            PrintJob.import_status.in_(
                ("FAILED", "QUARANTINED", "PARSE_RETRY", "PARSE_FAILED")
            ),
        )

    @staticmethod
    def _job_view(
        job: PrintJob, device: Device, direction_sizes: dict[str, int]
    ) -> JobView:
        return JobView(
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
            review_state=job.review_state,
            analysis_excluded=job.analysis_excluded,
            reviewed_at=job.reviewed_at,
            reviewed_by=job.reviewed_by_user_id,
            review_reason=job.review_reason,
        )

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
            incomplete = self._incomplete_job_predicate()
            if filters.get("incomplete") is True:
                statement = statement.where(incomplete)
            elif filters.get("incomplete") is False:
                statement = statement.where(~incomplete)
            if filters.get("from") is not None:
                statement = statement.where(PrintJob.captured_at >= filters["from"])
            if filters.get("to") is not None:
                statement = statement.where(PrintJob.captured_at < filters["to"])
            if filters.get("review_state"):
                statement = statement.where(
                    PrintJob.review_state == str(filters["review_state"])
                )
            count = (
                session.scalar(
                    select(func.count()).select_from(statement.order_by(None).subquery())
                )
                or 0
            )
            rows = session.execute(
                statement.order_by(PrintJob.captured_at.desc()).limit(limit).offset(offset)
            ).all()
            job_ids = [job.id for job, _ in rows]
            sizes: dict[UUID, dict[str, int]] = {job_id: {} for job_id in job_ids}
            if job_ids:
                for job_id, direction, byte_count in session.execute(
                    select(
                        RawPayload.job_id,
                        RawPayload.direction,
                        func.sum(RawPayload.byte_count),
                    )
                    .where(RawPayload.job_id.in_(job_ids))
                    .group_by(RawPayload.job_id, RawPayload.direction)
                ):
                    sizes[job_id][direction] = int(byte_count or 0)
            result: list[JobView] = []
            for job, device in rows:
                direction_sizes = sizes[job.id]
                result.append(self._job_view(job, device, direction_sizes))
            return result, count

    def review_job(
        self,
        job_id: UUID,
        review: JobReviewRequest,
        actor: UserPrincipal,
        *,
        correlation_id: str,
    ) -> JobView | None:
        state_by_action = {
            "VERIFY_USABLE": ("VERIFIED_USABLE", False),
            "EXCLUDE_FROM_ANALYSIS": ("EXCLUDED", True),
            # Reopening means "needs a new decision", not implicit trust.  Keep
            # incomplete evidence out of analysis until VERIFY_USABLE explicitly
            # clears the exclusion.
            "REOPEN_REVIEW": ("PENDING", True),
        }
        now = datetime.now(UTC)
        with self._write() as session:
            row = session.execute(
                select(PrintJob, Device)
                .join(Device)
                .where(PrintJob.id == job_id)
                .with_for_update()
            ).one_or_none()
            if row is None:
                return None
            job, device = row
            is_incomplete = bool(
                not job.capture_complete
                or not job.timeline_complete
                or job.status in {"FAILED", "INCOMPLETE", "MALFORMED"}
                or job.import_status
                in {"FAILED", "QUARANTINED", "PARSE_RETRY", "PARSE_FAILED"}
            )
            if not is_incomplete:
                raise ValueError("only technically incomplete jobs can be reviewed")
            target_state, excluded = state_by_action[review.action]
            previous_state = job.review_state
            previous_excluded = job.analysis_excluded
            job.review_state = target_state
            job.analysis_excluded = excluded
            job.reviewed_at = now
            job.reviewed_by_user_id = actor.id
            job.review_reason = review.reason
            watermark = session.get(AnalysisWatermark, "correlation")
            if watermark is not None:
                session.delete(watermark)
            if excluded:
                self._justify_alerts_for_excluded_job(
                    session,
                    job=job,
                    actor=actor,
                    reason=review.reason,
                    occurred_at=now,
                )
            # Re-inclusion deliberately does not resurrect historical correlations or
            # alerts here.  Clearing the correlation watermark schedules a fresh,
            # deterministic replay; only a finding that still exists in that replay
            # may become operational again.  This prevents an old correlation that
            # had already been superseded for another reason from being revived.
            self._append_audit_session(
                session,
                AuditEntry(
                    actor_id=actor.id,
                    action=f"JOB_REVIEW_{review.action}",
                    entity_type="print_job",
                    entity_id=str(job.id),
                    correlation_id=correlation_id,
                    occurred_at=now,
                    metadata={
                        "previous_review_state": previous_state,
                        "new_review_state": target_state,
                        "previous_analysis_excluded": previous_excluded,
                        "analysis_excluded": excluded,
                        "reason": review.reason,
                    },
                ),
            )
            direction_sizes = {
                direction: int(byte_count or 0)
                for direction, byte_count in session.execute(
                    select(RawPayload.direction, func.sum(RawPayload.byte_count))
                    .where(RawPayload.job_id == job.id)
                    .group_by(RawPayload.direction)
                )
            }
            session.flush()
            return self._job_view(job, device, direction_sizes)

    @staticmethod
    def _justify_alerts_for_excluded_job(
        session: Session,
        *,
        job: PrintJob,
        actor: UserPrincipal,
        reason: str,
        occurred_at: datetime,
    ) -> None:
        document_ids = set(
            session.scalars(select(Document.id).where(Document.job_id == job.id))
        )
        if not document_ids:
            return
        correlation_ids = set(
            session.scalars(
                select(DocumentCorrelationMember.correlation_id).where(
                    DocumentCorrelationMember.document_id.in_(document_ids)
                )
            )
        )
        if not correlation_ids:
            return
        correlations = session.scalars(
            select(DocumentCorrelation)
            .where(
                DocumentCorrelation.id.in_(correlation_ids),
                DocumentCorrelation.status.in_(("AUTOMATIC", "UNCORRELATED")),
            )
            .with_for_update()
        ).all()
        for correlation in correlations:
            correlation.status = "SUPERSEDED"
        alerts = session.scalars(
            select(FraudAlert)
            .where(
                FraudAlert.correlation_id.in_(correlation_ids),
                FraudAlert.is_canonical.is_(True),
                FraudAlert.status.in_(OPERATIONAL_ALERT_STATES),
            )
            .order_by(FraudAlert.opened_at, FraudAlert.id)
            .with_for_update()
        ).all()
        for alert in alerts:
            previous_status = alert.status
            alert.status = "JUSTIFIED"
            alert.closed_at = occurred_at
            alert.updated_at = occurred_at
            alert.closure_reason = (
                "Evidenza tecnica esclusa dall'analisi da un amministratore: " + reason
            )
            evidence_sequence = (
                session.scalar(
                    select(func.max(FraudAlertEvidence.sequence)).where(
                        FraudAlertEvidence.fraud_alert_id == alert.id
                    )
                )
                or 0
            ) + 1
            session.add(
                FraudAlertEvidence(
                    fraud_alert_id=alert.id,
                    sequence=evidence_sequence,
                    print_job_id=job.id,
                    evidence_type="JOB_EXCLUDED_FROM_ANALYSIS",
                    summary="Alert giustificato dopo esclusione auditata del job incompleto",
                    evidence={
                        "kind": "job_excluded_from_analysis",
                        "job_id": str(job.id),
                        "reason": reason,
                    },
                )
            )
            latest = session.scalar(
                select(FraudAlertHistory)
                .where(FraudAlertHistory.fraud_alert_id == alert.id)
                .order_by(FraudAlertHistory.sequence.desc())
                .limit(1)
                .with_for_update()
            )
            sequence = 1 if latest is None else latest.sequence + 1
            previous_hash = ZERO_HASH if latest is None else latest.record_hash
            history_payload = {
                "alert_id": str(alert.id),
                "sequence": sequence,
                "actor_user_id": str(actor.id),
                "event_type": "ALERT_JOB_EXCLUDED",
                "previous_status": previous_status,
                "new_status": "JUSTIFIED",
                "note": None,
                "reason": alert.closure_reason,
                "occurred_at": occurred_at.isoformat(),
                "previous_hash": previous_hash,
            }
            session.add(
                FraudAlertHistory(
                    fraud_alert_id=alert.id,
                    sequence=sequence,
                    actor_user_id=actor.id,
                    event_type="ALERT_JOB_EXCLUDED",
                    previous_status=previous_status,
                    new_status="JUSTIFIED",
                    reason=alert.closure_reason,
                    occurred_at=occurred_at,
                    previous_record_hash=previous_hash,
                    record_hash=chained_hash(history_payload, previous_hash),
                )
            )

    def _document_views(
        self, session: Session, documents: Sequence[Document]
    ) -> list[DocumentView]:
        if not documents:
            return []
        document_ids = [document.id for document in documents]
        versions = session.scalars(
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                Document.id.in_(document_ids),
                DocumentVersion.id == _latest_version_id(),
            )
        ).all()
        version_by_document = {version.document_id: version for version in versions}
        devices = {
            device.id: device
            for device in session.scalars(
                select(Device).where(Device.id.in_({document.device_id for document in documents}))
            )
        }
        parsers = {
            parser.id: parser
            for parser in session.scalars(
                select(ParserVersion).where(
                    ParserVersion.id.in_({version.parser_version_id for version in versions})
                )
            )
        }
        version_ids = [version.id for version in versions]
        lines_by_version: dict[UUID, list[DocumentLine]] = {}
        price_attributions_by_line: dict[UUID, list[LinePriceAttribution]] = {}
        payments_by_version: dict[UUID, list[Payment]] = {}
        for line in session.scalars(
            select(DocumentLine)
            .where(DocumentLine.document_version_id.in_(version_ids))
            .order_by(DocumentLine.document_version_id, DocumentLine.sequence)
        ):
            lines_by_version.setdefault(line.document_version_id, []).append(line)
        line_ids = {
            line.id for version_lines in lines_by_version.values() for line in version_lines
        }
        if line_ids:
            for attribution in session.scalars(
                select(LinePriceAttribution)
                .join(
                    DocumentCorrelation,
                    DocumentCorrelation.id == LinePriceAttribution.correlation_id,
                )
                .where(
                    LinePriceAttribution.target_line_id.in_(line_ids),
                    LinePriceAttribution.algorithm_version
                    == PRICE_ATTRIBUTION_ALGORITHM,
                    DocumentCorrelation.status.in_(("AUTOMATIC", "UNCORRELATED")),
                )
                .order_by(
                    LinePriceAttribution.target_line_id,
                    LinePriceAttribution.source_observed_at,
                    LinePriceAttribution.source_kind,
                    LinePriceAttribution.id,
                )
            ):
                price_attributions_by_line.setdefault(
                    attribution.target_line_id, []
                ).append(attribution)
        for payment in session.scalars(
            select(Payment)
            .where(Payment.document_version_id.in_(version_ids))
            .order_by(Payment.document_version_id, Payment.created_at, Payment.id)
        ):
            payments_by_version.setdefault(payment.document_version_id, []).append(payment)
        correlations_by_document: dict[
            UUID, list[tuple[DocumentCorrelationMember, DocumentCorrelation]]
        ] = {}
        for member, correlation in session.execute(
            select(DocumentCorrelationMember, DocumentCorrelation)
            .join(
                DocumentCorrelation,
                DocumentCorrelation.id == DocumentCorrelationMember.correlation_id,
            )
            .where(DocumentCorrelationMember.document_id.in_(document_ids))
            .order_by(
                DocumentCorrelationMember.document_id,
                DocumentCorrelation.created_at.desc(),
            )
        ):
            correlations_by_document.setdefault(member.document_id, []).append(
                (member, correlation)
            )
        required_suffix_criteria = {
            "commercial_reference_to_observed_fiscal_suffix",
            "table_code",
            "line_identity_overlap",
            "time_proximity",
        }
        resolving_correlation_ids = {
            correlation.id
            for correlations in correlations_by_document.values()
            for _member, correlation in correlations
            if correlation.status == "AUTOMATIC"
            and correlation.algorithm_version == "rpg-correlation-1.4.0"
            and correlation.score == 100
            and required_suffix_criteria.issubset(
                {str(value) for value in (correlation.matched_criteria or [])}
            )
        }
        related_ids_by_correlation: dict[UUID, set[UUID]] = {}
        if resolving_correlation_ids:
            for member in session.scalars(
                select(DocumentCorrelationMember).where(
                    DocumentCorrelationMember.correlation_id.in_(
                        resolving_correlation_ids
                    )
                )
            ):
                related_ids_by_correlation.setdefault(member.correlation_id, set()).add(
                    member.document_id
                )
        related_document_ids = {
            document_id
            for values in related_ids_by_correlation.values()
            for document_id in values
        }
        related_versions: dict[UUID, tuple[Document, DocumentVersion]] = {}
        if related_document_ids:
            for related_document, related_version in session.execute(
                select(Document, DocumentVersion)
                .join(
                    DocumentVersion,
                    DocumentVersion.document_id == Document.id,
                )
                .where(
                    Document.id.in_(related_document_ids),
                    DocumentVersion.id == _latest_version_id(),
                )
            ):
                related_versions[related_document.id] = (
                    related_document,
                    related_version,
                )

        def resolved_external_code(
            document: Document,
            version: DocumentVersion,
        ) -> tuple[str | None, str | None]:
            if (
                _version_value(version, document, "document_type")
                != DocumentType.COMMERCIAL_DOCUMENT.value
                or _version_value(version, document, "external_document_code")
            ):
                return None, None
            suffix = _version_value(
                version, document, "external_document_code_suffix"
            )
            if not suffix:
                return None, None
            candidates: set[str] = set()
            for _member, correlation in correlations_by_document.get(document.id, []):
                if correlation.id not in resolving_correlation_ids:
                    continue
                for related_id in related_ids_by_correlation.get(correlation.id, set()):
                    if related_id == document.id:
                        continue
                    related = related_versions.get(related_id)
                    if related is None:
                        continue
                    related_document, related_version = related
                    if _version_value(
                        related_version, related_document, "document_type"
                    ) not in {
                        DocumentType.MANAGEMENT_DOCUMENT.value,
                        DocumentType.CONFORMING_COPY.value,
                    }:
                        continue
                    reference = _version_value(
                        related_version,
                        related_document,
                        "commercial_reference_code",
                    )
                    if (
                        reference
                        and reference.replace("/", "-").rsplit("-", 1)[-1]
                        == str(suffix)
                    ):
                        candidates.add(str(reference))
            if len(candidates) != 1:
                return None, None
            return candidates.pop(), "CORRELATED_MANAGEMENT_REFERENCE"

        result: list[DocumentView] = []
        for document in documents:
            version = version_by_document.get(document.id)
            if version is None:
                continue
            device = devices.get(document.device_id)
            parser = parsers.get(version.parser_version_id)
            lines = lines_by_version.get(version.id, [])
            payments = payments_by_version.get(version.id, [])
            correlations = correlations_by_document.get(document.id, [])
            resolved_code, resolved_code_provenance = resolved_external_code(
                document, version
            )
            result.append(
                DocumentView(
                    id=document.id,
                    device_id=device.external_id if device else str(document.device_id),
                    job_id=document.job_id,
                    type=_version_value(version, document, "document_type"),
                    subtype=_version_value(version, document, "subtype"),
                    external_document_code=_version_value(
                        version, document, "external_document_code"
                    ),
                    external_document_code_suffix=_version_value(
                        version, document, "external_document_code_suffix"
                    ),
                    resolved_external_document_code=resolved_code,
                    resolved_external_document_code_provenance=(
                        resolved_code_provenance
                    ),
                    commercial_reference_code=_version_value(
                        version, document, "commercial_reference_code"
                    ),
                    progressive_observation_status=_progressive_observation_status(
                        version, document
                    ),
                    external_code=_version_value(
                        version, document, "external_document_code"
                    ),
                    order_code=_version_value(version, document, "order_code"),
                    table_code=_version_value(version, document, "table_code"),
                    operator_code=_version_value(version, document, "operator_code"),
                    terminal_code=_version_value(version, document, "terminal_code"),
                    covers=_as_nonnegative_int(
                        (version.raw_metadata or {}).get("covers")
                        if isinstance(version.raw_metadata, dict)
                        else None
                    ),
                    document_timestamp=_version_value(
                        version, document, "document_timestamp"
                    ),
                    document_timestamp_precision=(
                        str((version.raw_metadata or {}).get("document_timestamp_precision"))
                        if isinstance(version.raw_metadata, dict)
                        and (version.raw_metadata or {}).get("document_timestamp_precision")
                        else None
                    ),
                    document_timestamp_evidence=(
                        str((version.raw_metadata or {}).get("document_timestamp_evidence"))
                        if isinstance(version.raw_metadata, dict)
                        and (version.raw_metadata or {}).get("document_timestamp_evidence")
                        else None
                    ),
                    captured_at=document.captured_at,
                    gross_total=version.gross_total,
                    net_total=version.net_total,
                    discount_total=version.discount_total,
                    tax_total=version.tax_total,
                    status=version.status,
                    normalized_text=version.normalized_text,
                    receipt_text=receipt_text(version.normalized_text),
                    parser_name=parser.name if parser else "unknown",
                    parser_version=parser.version if parser else "unknown",
                    confidence=version.parse_confidence,
                    sha256=version.source_payload_sha256,
                    complete=version.complete,
                    warnings=[str(value) for value in (version.warnings or [])],
                    lines=[
                        DocumentLineView(
                            id=line.id,
                            sequence=line.sequence,
                            course_code=line.course_code,
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
                            **self._line_price_projection(
                                price_attributions_by_line.get(line.id, [])
                            ),
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
            )
        return result

    @staticmethod
    def _line_price_projection(
        attributions: Sequence[LinePriceAttribution],
    ) -> dict[str, Any]:
        views = [
            LinePriceAttributionView(
                id=item.id,
                correlation_id=item.correlation_id,
                source_document_id=item.source_document_id,
                source_document_version_id=item.source_document_version_id,
                source_line_id=item.source_line_id,
                source_kind=item.source_kind,
                observed_unit_price=item.observed_unit_price,
                observed_line_total=item.observed_line_total,
                confidence=item.confidence,
                status=item.status,
                match_basis=item.match_basis,
                algorithm_version=item.algorithm_version,
                criteria=item.criteria or {},
                source_observed_at=item.source_observed_at,
            )
            for item in attributions
        ]
        # The display projection is deliberately separate from the observed
        # POS unit_price.  A value is promoted only when every usable source
        # agrees.  Price changes across prebill/management/fiscal evidence are
        # retained as provenance and surfaced as a conflict, never hidden by
        # choosing the latest document.
        eligible = [
            item
            for item in attributions
            if item.status in {"RESOLVED", "AGREED"}
            and item.observed_unit_price is not None
        ]
        observed_unit_prices = {
            item.observed_unit_price
            for item in attributions
            if item.observed_unit_price is not None
        }
        observed_line_totals = {
            item.observed_line_total
            for item in attributions
            if item.observed_line_total is not None
        }
        conflicting_sources = (
            any(
                item.status == "AMBIGUOUS"
                and (
                    item.observed_unit_price is not None
                    or item.observed_line_total is not None
                )
                for item in attributions
            )
            or len(observed_unit_prices) > 1
            or len(observed_line_totals) > 1
        )
        priorities = {"FISCAL": 3, "MANAGEMENT": 2, "PREBILL": 1}
        selected = (
            None
            if conflicting_sources
            else max(
                eligible,
                key=lambda item: (
                    priorities.get(item.source_kind, 0),
                    item.source_observed_at,
                    str(item.id),
                ),
                default=None,
            )
        )
        return {
            "derived_unit_price": (
                None if selected is None else selected.observed_unit_price
            ),
            "derived_price_source": (
                "CONFLICTING_SOURCES"
                if conflicting_sources
                else None
                if selected is None
                else selected.source_kind
            ),
            "price_attributions": views,
        }

    def _document_view(self, session: Session, document: Document) -> DocumentView | None:
        views = self._document_views(session, [document])
        return views[0] if views else None

    def list_documents(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[DocumentView], int]:
        limit, offset = _page(limit, offset)
        with self._read() as session:
            semantic_type = _version_column(
                DocumentVersion.document_type,
                Document.document_type,
            )
            semantic_order_code = _version_column(
                DocumentVersion.order_code,
                Document.order_code,
            )
            semantic_external_code = _version_column(
                DocumentVersion.external_document_code,
                Document.external_document_code,
            )
            semantic_external_suffix = _version_column(
                DocumentVersion.external_document_code_suffix,
                Document.external_document_code_suffix,
            )
            semantic_commercial_reference = _version_column(
                DocumentVersion.commercial_reference_code,
                Document.commercial_reference_code,
            )
            statement: Select[Any] = (
                select(Document)
                .join(Device)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .where(DocumentVersion.id == _latest_version_id())
            )
            if filters.get("type"):
                statement = statement.where(semantic_type == str(filters["type"]))
            elif filters.get("exclude_type"):
                statement = statement.where(semantic_type != str(filters["exclude_type"]))
            if filters.get("device_id"):
                statement = statement.where(Device.external_id == str(filters["device_id"]))
            if filters.get("order_code"):
                statement = statement.where(
                    semantic_order_code == str(filters["order_code"])
                )
            if filters.get("external_document_code"):
                statement = statement.where(
                    semantic_external_code == str(filters["external_document_code"])
                )
            if filters.get("external_document_code_suffix"):
                statement = statement.where(
                    semantic_external_suffix
                    == str(filters["external_document_code_suffix"])
                )
            if filters.get("commercial_reference_code"):
                statement = statement.where(
                    semantic_commercial_reference
                    == str(filters["commercial_reference_code"])
                )
            if filters.get("from") is not None:
                statement = statement.where(Document.captured_at >= filters["from"])
            if filters.get("to") is not None:
                statement = statement.where(Document.captured_at < filters["to"])
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
            return self._document_views(session, documents), count

    def get_document(self, document_id: UUID) -> DocumentView | None:
        with self._read() as session:
            document = session.get(Document, document_id)
            return None if document is None else self._document_view(session, document)

    @staticmethod
    def _raw_direction(direction: str) -> str:
        try:
            return {
                "request": "CLIENT_TO_DEVICE",
                "response": "DEVICE_TO_CLIENT",
            }[direction]
        except KeyError as exc:
            raise ValueError("direction must be request or response") from exc

    @staticmethod
    def _raw_artifact(raw: RawPayload, filename_prefix: str) -> RawArtifact:
        return RawArtifact(
            raw.payload,
            f"{filename_prefix}_{raw.artifact_role.lower()}.raw",
            raw.sha256,
        )

    def get_document_raw(
        self, document_id: UUID, *, direction: str = "request"
    ) -> RawArtifact | None:
        stored_direction = self._raw_direction(direction)
        with self._read() as session:
            document = session.get(Document, document_id)
            if document is None:
                return None
            version = session.scalar(
                select(DocumentVersion)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    Document.id == document.id,
                    DocumentVersion.id == _latest_version_id(),
                )
            )
            raw = session.get(RawPayload, version.raw_payload_id) if version else None
            if raw is not None and raw.direction != stored_direction:
                raw = None
            if raw is None:
                raw = session.scalar(
                    select(RawPayload)
                    .where(
                        RawPayload.job_id == document.job_id,
                        RawPayload.direction == stored_direction,
                    )
                    .order_by(RawPayload.created_at, RawPayload.id)
                    .limit(1)
                )
            if raw is None:
                return None
            return self._raw_artifact(raw, str(document.id))

    def get_job_raw(self, job_id: UUID, *, direction: str) -> RawArtifact | None:
        stored_direction = self._raw_direction(direction)
        with self._read() as session:
            if session.get(PrintJob, job_id) is None:
                return None
            raw = session.scalar(
                select(RawPayload)
                .where(RawPayload.job_id == job_id, RawPayload.direction == stored_direction)
                .order_by(RawPayload.created_at, RawPayload.id)
                .limit(1)
            )
            return None if raw is None else self._raw_artifact(raw, str(job_id))

    def get_session_raw(self, session_id: UUID, *, direction: str) -> RawArtifact | None:
        stored_direction = self._raw_direction(direction)
        with self._read() as session:
            if session.get(ProxySession, session_id) is None:
                return None
            chunks = session.scalars(
                select(StreamChunk)
                .where(
                    StreamChunk.session_id == session_id,
                    StreamChunk.direction == stored_direction,
                    StreamChunk.event_kind == "data",
                )
                .order_by(StreamChunk.direction_offset, StreamChunk.direction_sequence)
            ).all()
            if not chunks:
                return None
            expected_offset = 0
            payload_parts: list[bytes] = []
            for chunk in chunks:
                if chunk.direction_offset != expected_offset:
                    return None
                if len(chunk.payload) != chunk.byte_count:
                    return None
                if hashlib.sha256(chunk.payload).hexdigest() != chunk.sha256:
                    return None
                payload_parts.append(chunk.payload)
                expected_offset += chunk.byte_count
            payload = b"".join(payload_parts)
            digest = hashlib.sha256(payload).hexdigest()
            return RawArtifact(
                payload,
                f"{session_id}_{direction}.raw",
                digest,
            )

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
            versions = {
                order_id: int(sequence or 0)
                for order_id, sequence in session.execute(
                    select(OrderSnapshot.order_id, func.max(OrderSnapshot.sequence))
                    .where(OrderSnapshot.order_id.in_([order.id for order in rows]))
                    .group_by(OrderSnapshot.order_id)
                ).all()
            }
            result: list[OrderView] = []
            for order in rows:
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
                        version=versions.get(order.id, 0),
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
        versions = {
            version.document_id: version
            for version in session.scalars(
                select(DocumentVersion)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    Document.id.in_([document.id for document in documents]),
                    DocumentVersion.id == _latest_version_id(),
                )
            )
        }

        def semantics(document: Document, field: str) -> Any:
            version = versions.get(document.id)
            if version is None:
                return getattr(document, field)
            return _version_value(version, document, field)

        def document_time(document: Document) -> datetime:
            return semantics(document, "document_timestamp") or document.captured_at

        prebill_documents = [
            document
            for document in documents
            if semantics(document, "document_type") == DocumentType.PRE_BILL.value
        ]
        commercial_documents = [
            document
            for document in documents
            if semantics(document, "document_type") in SALE_FISCAL_TYPES
        ]
        fiscal_documents = [
            document
            for document in commercial_documents
            if document.id in versions
            and versions[document.id].complete
            and versions[document.id].gross_total is not None
        ]
        fiscal_documents.sort(key=lambda item: (document_time(item), item.captured_at, item.id))
        economic_closures = [
            document
            for document in documents
            if semantics(document, "document_type") == DocumentType.MANAGEMENT_DOCUMENT.value
            and document.id in versions
            and versions[document.id].gross_total is not None
            and isinstance(versions[document.id].raw_metadata, dict)
            and (
                bool(versions[document.id].raw_metadata.get("economic_close"))
                or versions[document.id].raw_metadata.get("settlement_kind") == "ROOM_CHARGE"
            )
        ]
        cancellation_documents = [
            document
            for document in documents
            if semantics(document, "document_type") == DocumentType.CANCELLATION.value
        ]
        first_fiscal = fiscal_documents[0] if fiscal_documents else None
        first_fiscal_time = document_time(first_fiscal) if first_fiscal is not None else None
        prebill_candidates = [
            document
            for document in prebill_documents
            if document.id in versions
            and (
                first_fiscal_time is None
                or document_time(document) <= first_fiscal_time
            )
        ]
        prebill_candidates.sort(
            key=lambda item: (document_time(item), item.captured_at, item.id)
        )
        prebill_document = prebill_candidates[-1] if prebill_candidates else None
        baseline_basis = "OBSERVED_PRE_BILL" if prebill_document is not None else None

        # RCH management streams do not always carry the PRE_BILL literal.  The
        # correlation engine records a dedicated, explainable sale-sequence
        # criterion when a complete non-economic management document precedes
        # the fiscal close on the same table with overlapping line identity.
        # Reuse that persisted evidence here rather than guessing from a table
        # number or a total.  A post-fiscal management copy (which carries a
        # commercial reference) and a room/economic close are explicitly
        # ineligible as the comparison baseline.
        matched_criteria = {
            str(value) for value in (correlation.matched_criteria or [])
        }

        def stored_line_keys(document: Document) -> set[str]:
            version = versions.get(document.id)
            if version is None:
                return set()
            return {
                _line_key(line)
                for line in session.scalars(
                    select(DocumentLine).where(
                        DocumentLine.document_version_id == version.id
                    )
                )
            }

        if (
            prebill_document is None
            and first_fiscal is not None
            and {"table_sale_sequence", "line_identity_overlap"}.issubset(
                matched_criteria
            )
        ):
            fiscal_table = semantics(first_fiscal, "table_code")
            normalized_fiscal_table = (
                " ".join(str(fiscal_table).upper().split())
                if fiscal_table is not None
                else None
            )
            fiscal_line_keys = stored_line_keys(first_fiscal)
            management_candidates = []
            for document in documents:
                version = versions.get(document.id)
                if (
                    version is None
                    or semantics(document, "document_type")
                    != DocumentType.MANAGEMENT_DOCUMENT.value
                    or not version.complete
                    or version.gross_total is None
                    or semantics(document, "commercial_reference_code") is not None
                    or not isinstance(version.raw_metadata, dict)
                    or bool(version.raw_metadata.get("economic_close"))
                    or version.raw_metadata.get("settlement_kind") == "ROOM_CHARGE"
                    or document_time(document) > first_fiscal_time
                ):
                    continue
                table = semantics(document, "table_code")
                normalized_table = (
                    " ".join(str(table).upper().split()) if table is not None else None
                )
                if (
                    normalized_fiscal_table is not None
                    and normalized_table == normalized_fiscal_table
                    and bool(fiscal_line_keys & stored_line_keys(document))
                ):
                    management_candidates.append(document)
            management_candidates.sort(
                key=lambda item: (document_time(item), item.captured_at, item.id)
            )
            if management_candidates:
                prebill_document = management_candidates[-1]
                baseline_basis = "CORRELATED_MANAGEMENT_PRE_FISCAL"

        prebill_total = (
            versions[prebill_document.id].gross_total
            if prebill_document is not None and prebill_document.id in versions
            else None
        )
        fiscal_total = sum(
            (versions[document.id].gross_total or MONEY_ZERO)
            for document in fiscal_documents
        )
        management_total = sum(
            (versions[document.id].gross_total or MONEY_ZERO)
            for document in economic_closures
        )
        if fiscal_documents:
            observed_final_total: Decimal | None = fiscal_total
            final_documents = fiscal_documents
            settlement_basis = "FISCAL"
        elif economic_closures:
            observed_final_total = management_total
            final_documents = economic_closures
            settlement_basis = "ECONOMIC_MANAGEMENT_CLOSE"
        elif cancellation_documents:
            observed_final_total = MONEY_ZERO
            final_documents = cancellation_documents
            settlement_basis = "CANCELLATION"
        else:
            observed_final_total = None
            final_documents = []
            settlement_basis = "PARTIAL_FISCAL" if commercial_documents else "NONE"
        initial = next(
            (
                versions[document.id].gross_total
                for document in documents
                if semantics(document, "document_type") in SOURCE_TYPES
                and document.id in versions
                and versions[document.id].gross_total is not None
            ),
            None,
        )
        alert_count = (
            session.scalar(
                select(func.count())
                .select_from(FraudAlert)
                .where(
                    FraudAlert.is_canonical.is_(True),
                    FraudAlert.transaction_id == correlation.transaction_id,
                    FraudAlert.status.in_(OPERATIONAL_ALERT_STATES),
                )
            )
            or 0
        )
        order_code = next(
            (
                value
                for item in documents
                if (value := semantics(item, "order_code"))
            ),
            None,
        )
        order = session.scalar(
            select(Order)
            .join(OrderEvent, OrderEvent.order_id == Order.id)
            .where(OrderEvent.source_document_id.in_([document.id for document in documents]))
            .order_by(OrderEvent.occurred_at.desc(), Order.id.desc())
            .limit(1)
        )
        if order is None and order_code:
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
                "type": semantics(document, "document_type"),
                "occurred_at": (
                    document_time(document)
                ).isoformat(),
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
            .where(
                FraudAlert.is_canonical.is_(True),
                FraudAlert.transaction_id == correlation.transaction_id,
            )
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
        if prebill_document is not None:
            prebill_version = versions.get(prebill_document.id)
            if prebill_version is not None:
                prebill_lines = list(
                    session.scalars(
                        select(DocumentLine)
                        .where(DocumentLine.document_version_id == prebill_version.id)
                        .order_by(DocumentLine.sequence)
                    )
                )
        final_version_ids = [
            versions[document.id].id for document in final_documents if document.id in versions
        ]
        if final_version_ids:
            fiscal_lines = list(
                session.scalars(
                    select(DocumentLine)
                    .where(DocumentLine.document_version_id.in_(final_version_ids))
                    .order_by(DocumentLine.document_version_id, DocumentLine.sequence)
                )
            )
        difference = (
            None
            if prebill_total is None or observed_final_total is None
            else prebill_total - observed_final_total
        )
        return TransactionView(
            id=correlation.transaction_id,
            order_id=order.id if order else None,
            occurred_at=(
                min(
                    semantics(document, "document_timestamp") or document.captured_at
                    for document in documents
                )
                if documents
                else correlation.created_at
            ),
            table_code=next(
                (
                    value
                    for item in documents
                    if (value := semantics(item, "table_code"))
                ),
                None,
            ),
            order_code=order_code,
            operator_code=next(
                (
                    value
                    for item in documents
                    if (value := semantics(item, "operator_code"))
                ),
                None,
            ),
            initial_total=initial,
            pre_bill_total=prebill_total,
            fiscal_total=observed_final_total,
            difference=difference,
            status="ALERT" if alert_count else "CORRELATED",
            document_count=len(documents),
            alert_count=alert_count,
            correlation_confidence=correlation.score,
            timeline=timeline,
            diff={
                "pre_bill_total": str(prebill_total) if prebill_total is not None else None,
                "baseline_basis": baseline_basis,
                "baseline_document_id": (
                    str(prebill_document.id) if prebill_document is not None else None
                ),
                "baseline_document_type": (
                    semantics(prebill_document, "document_type")
                    if prebill_document is not None
                    else None
                ),
                "fiscal_total": str(fiscal_total) if fiscal_documents else None,
                "observed_final_total": (
                    str(observed_final_total) if observed_final_total is not None else None
                ),
                "settlement_basis": settlement_basis,
                "difference": str(difference) if difference is not None else None,
                "lines": _stored_line_diff(prebill_lines, fiscal_lines),
            },
        )

    def list_transactions(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[TransactionView], int]:
        limit, offset = _page(limit, offset)
        with self._read() as session:
            occurrence = _latest_transaction_occurrences()
            statement: Select[Any] = (
                select(DocumentCorrelation)
                .join(
                    occurrence,
                    occurrence.c.correlation_id == DocumentCorrelation.id,
                )
            )
            document_filters = []
            if filters.get("table_code"):
                document_filters.append(
                    _version_column(DocumentVersion.table_code, Document.table_code)
                    == str(filters["table_code"])
                )
            if filters.get("order_code"):
                document_filters.append(
                    _version_column(DocumentVersion.order_code, Document.order_code)
                    == str(filters["order_code"])
                )
            if filters.get("operator_code"):
                document_filters.append(
                    _version_column(DocumentVersion.operator_code, Document.operator_code)
                    == str(filters["operator_code"])
                )
            if document_filters:
                statement = statement.where(
                    DocumentCorrelation.id.in_(
                        select(DocumentCorrelationMember.correlation_id)
                        .join(Document, Document.id == DocumentCorrelationMember.document_id)
                        .join(
                            DocumentVersion,
                            DocumentVersion.document_id == Document.id,
                        )
                        .where(DocumentVersion.id == _latest_version_id())
                        .where(*document_filters)
                    )
                )
            if filters.get("operational_economic_only"):
                economic_transactions = (
                    select(FraudAlert.transaction_id)
                    .select_from(FraudAlert)
                    .join(
                        FraudRuleVersion,
                        FraudRuleVersion.id == FraudAlert.fraud_rule_version_id,
                    )
                    .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
                    .where(
                        FraudAlert.is_canonical.is_(True),
                        FraudAlert.status.in_(OPERATIONAL_ALERT_STATES),
                        FraudAlert.difference_amount > MONEY_ZERO,
                        FraudRule.code.in_(ECONOMIC_REDUCTION_RULE_CODES),
                    )
                    .group_by(FraudAlert.transaction_id)
                )
                statement = statement.where(
                    DocumentCorrelation.transaction_id.in_(economic_transactions)
                )
            statement = statement.order_by(
                occurrence.c.occurred_at.desc(), DocumentCorrelation.id.desc()
            )
            if filters.get("from") is not None:
                statement = statement.where(occurrence.c.occurred_at >= filters["from"])
            if filters.get("to") is not None:
                statement = statement.where(occurrence.c.occurred_at < filters["to"])
            minimum_value = filters.get("minimum_difference")
            reduction_only = bool(filters.get("reduction_only"))
            if minimum_value is None and not reduction_only:
                count = (
                    session.scalar(
                        select(func.count()).select_from(statement.order_by(None).subquery())
                    )
                    or 0
                )
                correlations = session.scalars(
                    statement.limit(limit).offset(offset)
                ).all()
                return [self._transaction_view(session, item) for item in correlations], count

            # Difference depends on the latest versions and split fiscal documents.
            # Keep its exact domain semantics, but only pay the full materialisation
            # cost when that uncommon derived-value filter is explicitly requested.
            correlations = session.scalars(
                statement
            ).all()
            minimum = _as_decimal(minimum_value) or MONEY_ZERO
            views = [
                view
                for correlation in correlations
                if (view := self._transaction_view(session, correlation)).difference is not None
                and (
                    view.difference >= minimum
                    if reduction_only
                    else abs(view.difference) >= minimum
                )
                and (not reduction_only or view.difference > MONEY_ZERO)
            ]
            return views[offset : offset + limit], len(views)

    def get_transaction(self, transaction_id: UUID) -> TransactionView | None:
        with self._read() as session:
            correlation = session.scalar(
                select(DocumentCorrelation)
                .where(
                    DocumentCorrelation.transaction_id == transaction_id,
                    DocumentCorrelation.status.in_(("AUTOMATIC", "UNCORRELATED")),
                )
                .order_by(
                    DocumentCorrelation.created_at.desc(),
                    DocumentCorrelation.id.desc(),
                )
                .limit(1)
            )
            if correlation is None:
                correlation = session.scalar(
                    select(DocumentCorrelation)
                    .where(
                        DocumentCorrelation.transaction_id == transaction_id,
                        DocumentCorrelation.status == "SUPERSEDED",
                    )
                    .order_by(
                        DocumentCorrelation.created_at.desc(),
                        DocumentCorrelation.id.desc(),
                    )
                    .limit(1)
                )
            return None if correlation is None else self._transaction_view(session, correlation)

    def _alert_views(
        self, session: Session, alerts: Sequence[FraudAlert], *, include_details: bool
    ) -> list[AlertView]:
        if not alerts:
            return []
        alert_ids = [alert.id for alert in alerts]
        version_ids = {alert.fraud_rule_version_id for alert in alerts}
        versions = {
            version.id: version
            for version in session.scalars(
                select(FraudRuleVersion).where(FraudRuleVersion.id.in_(version_ids))
            )
        }
        rules = {
            rule.id: rule
            for rule in session.scalars(
                select(FraudRule).where(
                    FraudRule.id.in_({version.fraud_rule_id for version in versions.values()})
                )
            )
        }
        correlation_ids = {
            alert.correlation_id for alert in alerts if alert.correlation_id is not None
        }
        documents_by_correlation: dict[UUID, list[UUID]] = {}
        devices_by_correlation: dict[UUID, set[str]] = {}
        if correlation_ids:
            for correlation_id, document_id, device_external_id in session.execute(
                select(
                    DocumentCorrelationMember.correlation_id,
                    Document.id,
                    Device.external_id,
                )
                .join(Document, Document.id == DocumentCorrelationMember.document_id)
                .join(Device, Device.id == Document.device_id)
                .where(DocumentCorrelationMember.correlation_id.in_(correlation_ids))
                .order_by(DocumentCorrelationMember.correlation_id, Document.id)
            ):
                documents_by_correlation.setdefault(correlation_id, []).append(document_id)
                devices_by_correlation.setdefault(correlation_id, set()).add(device_external_id)
        evidence_by_alert: dict[UUID, list[FraudAlertEvidence]] = {}
        history_by_alert: dict[UUID, list[FraudAlertHistory]] = {}
        if include_details:
            for row in session.scalars(
                select(FraudAlertEvidence)
                .where(FraudAlertEvidence.fraud_alert_id.in_(alert_ids))
                .order_by(
                    FraudAlertEvidence.fraud_alert_id,
                    FraudAlertEvidence.sequence,
                    FraudAlertEvidence.id,
                )
            ):
                evidence_by_alert.setdefault(row.fraud_alert_id, []).append(row)
            for row in session.scalars(
                select(FraudAlertHistory)
                .where(FraudAlertHistory.fraud_alert_id.in_(alert_ids))
                .order_by(FraudAlertHistory.fraud_alert_id, FraudAlertHistory.sequence)
            ):
                history_by_alert.setdefault(row.fraud_alert_id, []).append(row)
        result: list[AlertView] = []
        for alert in alerts:
            version = versions.get(alert.fraud_rule_version_id)
            rule = rules.get(version.fraud_rule_id) if version else None
            evidence_rows = evidence_by_alert.get(alert.id, [])
            history_rows = history_by_alert.get(alert.id, [])
            result.append(
                AlertView(
                    id=alert.id,
                    rule_code=rule.code if rule else "UNKNOWN",
                    severity=alert.severity,
                    score=alert.score,
                    status=alert.status,
                    opened_at=alert.opened_at,
                    transaction_id=alert.transaction_id,
                    device_ids=sorted(devices_by_correlation.get(alert.correlation_id, set())),
                    document_ids=documents_by_correlation.get(alert.correlation_id, []),
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
                            "raw_payload_id": (
                                str(row.raw_payload_id) if row.raw_payload_id else None
                            ),
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
                            "actor_user_id": (
                                str(row.actor_user_id) if row.actor_user_id else None
                            ),
                            "occurred_at": row.occurred_at.isoformat(),
                            "record_hash": row.record_hash,
                        }
                        for row in history_rows
                    ],
                )
            )
        return result

    def _alert_view(self, session: Session, alert: FraudAlert) -> AlertView:
        return self._alert_views(session, [alert], include_details=True)[0]

    def list_alerts(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[AlertView], int]:
        limit, offset = _page(limit, offset)
        with self._read() as session:
            statement: Select[Any] = (
                select(FraudAlert)
                .join(FraudRuleVersion)
                .join(FraudRule)
                .where(FraudAlert.is_canonical.is_(True))
            )
            if filters.get("severity"):
                statement = statement.where(FraudAlert.severity == str(filters["severity"]))
            if filters.get("status"):
                statement = statement.where(FraudAlert.status == str(filters["status"]))
            if filters.get("view") == "operational":
                statement = statement.where(
                    FraudAlert.status.in_(OPERATIONAL_ALERT_STATES)
                )
            elif filters.get("view") == "archive":
                statement = statement.where(FraudAlert.status.in_(ARCHIVED_ALERT_STATES))
            if filters.get("from") is not None or filters.get("to") is not None:
                if filters.get("view") == "operational":
                    alert_occurrence = _latest_transaction_occurrences()
                    statement = statement.join(
                        alert_occurrence,
                        alert_occurrence.c.transaction_id == FraudAlert.transaction_id,
                    )
                else:
                    alert_occurrence = _historical_correlation_occurrences()
                    statement = statement.join(
                        alert_occurrence,
                        alert_occurrence.c.correlation_id == FraudAlert.correlation_id,
                    )
                if filters.get("from") is not None:
                    statement = statement.where(
                        alert_occurrence.c.occurred_at >= filters["from"]
                    )
                if filters.get("to") is not None:
                    statement = statement.where(
                        alert_occurrence.c.occurred_at < filters["to"]
                    )
            if filters.get("rule"):
                statement = statement.where(FraudRule.code == str(filters["rule"]))
            if filters.get("device_id"):
                statement = statement.where(
                    _alert_has_device(str(filters["device_id"]))
                )
            if filters.get("operator_code"):
                statement = statement.where(
                    _alert_has_operator(str(filters["operator_code"]))
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
            return self._alert_views(session, alerts, include_details=False), count

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
            # Older releases persisted one COMPLETED row for every duplicate-only
            # polling cycle. Keep those rows available for forensic SQL, but omit the
            # operational noise from the UI and its pagination count.
            meaningful = or_(
                ImportBatch.imported_count > 0,
                ImportBatch.failed_count > 0,
                ImportBatch.status != "COMPLETED",
                ImportBatch.scanned_count != ImportBatch.skipped_count,
            )
            count = (
                session.scalar(
                    select(func.count(ImportBatch.id)).where(meaningful)
                )
                or 0
            )
            rows = session.scalars(
                select(ImportBatch)
                .where(meaningful)
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

    def search(
        self,
        *,
        query: str,
        limit: int,
        offset: int,
        filters: dict[str, Any] | None = None,
    ) -> tuple[list[SearchHit], int]:
        limit, offset = _page(limit, offset, maximum=200)
        filters = filters or {}
        value = query.strip()
        if len(value) < 2:
            return [], 0
        escaped_value = value.replace("%", r"\%").replace("_", r"\_")
        pattern = f"%{escaped_value}%"
        amount = _as_decimal(value.replace(",", "."))
        with self._read() as session:
            document_period = []
            order_period = []
            device_period = []
            if filters.get("from") is not None:
                document_period.append(Document.captured_at >= filters["from"])
                order_period.append(Order.opened_at >= filters["from"])
                device_period.append(Device.created_at >= filters["from"])
            if filters.get("to") is not None:
                document_period.append(Document.captured_at < filters["to"])
                order_period.append(Order.opened_at < filters["to"])
                device_period.append(Device.created_at < filters["to"])
            document_predicates = [
                _version_column(
                    DocumentVersion.external_document_code,
                    Document.external_document_code,
                ).ilike(pattern, escape="\\"),
                _version_column(
                    DocumentVersion.external_document_code_suffix,
                    Document.external_document_code_suffix,
                ).ilike(pattern, escape="\\"),
                _version_column(
                    DocumentVersion.commercial_reference_code,
                    Document.commercial_reference_code,
                ).ilike(pattern, escape="\\"),
                _version_column(DocumentVersion.order_code, Document.order_code).ilike(
                    pattern, escape="\\"
                ),
                _version_column(DocumentVersion.table_code, Document.table_code).ilike(
                    pattern, escape="\\"
                ),
                _version_column(DocumentVersion.operator_code, Document.operator_code).ilike(
                    pattern, escape="\\"
                ),
                Device.external_id.ilike(pattern, escape="\\"),
                Device.name.ilike(pattern, escape="\\"),
                DocumentVersion.normalized_text.ilike(pattern, escape="\\"),
                DocumentVersion.source_payload_sha256.ilike(pattern, escape="\\"),
                DocumentVersion.id.in_(
                    select(DocumentLine.document_version_id).where(
                        DocumentLine.description.ilike(pattern, escape="\\")
                    )
                ),
            ]
            if amount is not None:
                document_predicates.append(DocumentVersion.gross_total == amount)
            document_rows = session.execute(
                select(Document, DocumentVersion)
                .join(Device, Device.id == Document.device_id)
                .join(DocumentVersion, DocumentVersion.id == _latest_version_id())
                .where(or_(*document_predicates), *document_period)
                .order_by(Document.captured_at.desc())
                .limit(500)
            ).all()
            hits: list[SearchHit] = [
                SearchHit(
                    entity_type="DOCUMENT",
                    entity_id=document.id,
                    occurred_at=(
                        _version_value(version, document, "document_timestamp")
                        or document.captured_at
                    ),
                    title=(
                        _version_value(version, document, "external_document_code")
                        or _version_value(version, document, "order_code")
                        or _version_value(version, document, "document_type")
                    ),
                    subtitle=(
                        f"{_version_value(version, document, 'document_type')} · "
                        f"{_version_value(version, document, 'table_code') or '-'}"
                    ),
                    highlights=[value],
                )
                for document, version in document_rows
            ]
            for device in session.scalars(
                select(Device)
                .where(
                    or_(
                        Device.external_id.ilike(pattern, escape="\\"),
                        Device.name.ilike(pattern, escape="\\"),
                        Device.mac_address.ilike(pattern, escape="\\"),
                        Device.department.ilike(pattern, escape="\\"),
                        Device.role.ilike(pattern, escape="\\"),
                    ),
                    *device_period,
                )
                .order_by(Device.external_id)
                .limit(100)
            ):
                hits.append(
                    SearchHit(
                        entity_type="DEVICE",
                        entity_id=device.id,
                        occurred_at=device.created_at,
                        title=device.name,
                        subtitle=(
                            f"{device.external_id} · "
                            f"{device.department or device.device_type}"
                        ),
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
                    ),
                    *order_period,
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
                discovered = int(report.get("discovered", 0))
                imported = int(report.get("imported", 0))
                duplicates = int(report.get("duplicates", 0))
                failed = int(report.get("quarantined", 0)) + int(
                    report.get("retry_exhausted", 0)
                )
                errors = tuple(str(item)[:4096] for item in report.get("errors", ()))
                if imported == 0 and failed == 0 and not errors and discovered == duplicates:
                    # A periodic scan that observes no new evidence is operational noise,
                    # not an immutable import event. Duplicate attempts do not create
                    # ImportItem rows, so deleting this still-RUNNING shell is FK-safe and
                    # prevents an idle worker from growing import_batches without bound.
                    session.delete(batch)
                    return
                batch.scanned_count = discovered
                batch.imported_count = imported
                batch.skipped_count = duplicates
                batch.failed_count = failed
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
                external_document_code_suffix=_optional_text(
                    _first(
                        semantic,
                        "external_document_code_suffix",
                        "document_number_suffix",
                    )
                ),
                commercial_reference_code=_optional_text(
                    _first(
                        semantic,
                        "commercial_reference_code",
                        "commercial_document_reference",
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
                document_type=document.document_type,
                subtype=document.subtype,
                external_document_code=document.external_document_code,
                external_document_code_suffix=document.external_document_code_suffix,
                commercial_reference_code=document.commercial_reference_code,
                order_code=document.order_code,
                table_code=document.table_code,
                operator_code=document.operator_code,
                terminal_code=document.terminal_code,
                document_timestamp=document.document_timestamp,
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
                    "mac_address": configured.mac_address,
                    "department": configured.department,
                    "role": configured.role,
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
    freshness_seconds = max(300.0, settings.ingestion.scan_interval_seconds * 3)
    return SqlAlchemyApiRepository.from_url(
        settings.database_url().get_secret_value(),
        spool_metric_stale_after=timedelta(seconds=freshness_seconds),
        spool_warning_bytes=settings.ingestion.spool_warning_bytes,
    )


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
