"""Database-backed deterministic fraud evaluation worker."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from retailprintguard.common.domain import AlertSeverity, OrderEventType
from retailprintguard.common.domain import OrderEvent as DomainOrderEvent
from retailprintguard.common.hashchain import ZERO_HASH, chained_hash
from retailprintguard.correlation.engine import ALGORITHM_VERSION, CorrelationEngine
from retailprintguard.correlation.worker import (
    LoadedDocument,
    correlation_input_fingerprint,
    load_latest_documents,
)
from retailprintguard.db.models import (
    Document,
    DocumentCorrelation,
    DocumentCorrelationMember,
    FraudAlert,
    FraudAlertEvidence,
    FraudAlertHistory,
    FraudRule,
    FraudRuleVersion,
    FraudWhitelist,
    OrderEvent,
    RawPayload,
)
from retailprintguard.fraud.engine import (
    DEFAULT_RULES,
    FraudContext,
    FraudEngine,
    OperatorPatternStats,
    RuleDefinition,
    SuppressedFinding,
    WhitelistEntry,
    WhitelistScope,
    finding_fingerprint,
)
from retailprintguard.fraud.versioning import rule_configuration_fingerprint

FRAUD_ENGINE_VERSION = "rpg-fraud-1.0.0"


@dataclass(frozen=True, slots=True)
class FraudRunReport:
    correlations_loaded: int
    transactions_evaluated: int
    findings_detected: int
    findings_suppressed: int
    alerts_inserted: int
    evidence_inserted: int
    alerts_superseded: int


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


class FraudWorker:
    """Evaluate current correlations and append alerts without changing source evidence."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        minimum_score: int = 60,
        time_window_seconds: int = 7200,
        default_amount_drop_percent: int = 20,
        order_without_fiscal_close_minutes: int = 120,
        extreme_price_change_percent: int = 70,
    ) -> None:
        self._factory = factory
        self.correlation_engine = CorrelationEngine(
            minimum_score=minimum_score,
            time_window_seconds=time_window_seconds,
        )
        self._parameter_overrides: dict[str, dict[str, Any]] = {
            "PREBILL_FISCAL_AMOUNT_DROP": {"minimum_percent": Decimal(default_amount_drop_percent)},
            "ORDER_WITHOUT_FISCAL_CLOSE": {"close_minutes": order_without_fiscal_close_minutes},
            "EXTREME_PRICE_CHANGE": {"minimum_percent": Decimal(extreme_price_change_percent)},
        }

    def run_once(self, *, max_transactions: int | None = 10_000) -> FraudRunReport:
        if max_transactions is not None and max_transactions < 1:
            raise ValueError("max_transactions must be positive")
        now = datetime.now(UTC)
        with self._factory.begin() as session:
            alerts_superseded = 0
            rule_rows = self._ensure_and_load_rules(session, now)
            rules = tuple(definition for definition, _ in rule_rows.values())
            engine = FraudEngine(rules)
            correlations = self._current_correlations(session, limit=max_transactions)
            member_ids = {
                document_id
                for correlation in correlations
                for document_id in session.scalars(
                    select(DocumentCorrelationMember.document_id).where(
                        DocumentCorrelationMember.correlation_id == correlation.id
                    )
                )
            }
            comparison_ids = set(member_ids)
            member_rows = session.execute(
                select(
                    Document.external_document_code, Document.device_id, Document.captured_at
                ).where(Document.id.in_(member_ids))
            ).all()
            for external_code, device_id, captured_at in member_rows:
                if not external_code:
                    continue
                comparison_ids.update(
                    session.scalars(
                        select(Document.id).where(
                            Document.device_id == device_id,
                            Document.external_document_code == external_code,
                            Document.captured_at >= captured_at - timedelta(days=1),
                            Document.captured_at <= captured_at + timedelta(days=1),
                        )
                    )
                )
            comparison_loaded = load_latest_documents(session, document_ids=comparison_ids)
            comparison_documents = tuple(item.value for item in comparison_loaded)
            comparison_by_id = {item.value.id: item for item in comparison_loaded}
            whitelists = self._load_whitelists(session, now)
            operator_counts, anomalous_transactions, void_counts = self._operator_statistics(
                session, correlations, comparison_by_id
            )
            evaluated = 0
            findings_detected = 0
            findings_suppressed = 0
            alerts_inserted = 0
            evidence_inserted = 0
            for correlation in correlations:
                ids = set(
                    session.scalars(
                        select(DocumentCorrelationMember.document_id).where(
                            DocumentCorrelationMember.correlation_id == correlation.id
                        )
                    )
                )
                loaded = [comparison_by_id[item] for item in ids if item in comparison_by_id]
                if len(loaded) != len(ids):
                    continue
                by_id = {item.value.id: item for item in loaded}
                current_fingerprint = correlation_input_fingerprint(
                    tuple(item.value for item in loaded), by_id
                )
                if current_fingerprint != correlation.input_fingerprint:
                    continue
                candidate_transactions = self.correlation_engine.correlate(
                    item.value for item in loaded
                )
                transaction = next(
                    (
                        candidate
                        for candidate in candidate_transactions
                        if candidate.transaction_id == correlation.transaction_id
                        and {document.id for document in candidate.documents} == ids
                    ),
                    None,
                )
                if transaction is None:
                    continue
                order_events = self._load_order_events(session, ids)
                operator = next(
                    (
                        document.operator_code
                        for document in transaction.documents
                        if document.operator_code
                    ),
                    None,
                )
                stats = None
                if operator is not None:
                    stats = OperatorPatternStats(
                        operator_code=operator,
                        transaction_count=len(operator_counts[operator]),
                        anomalous_transaction_count=len(
                            operator_counts[operator] & anomalous_transactions
                        ),
                        void_or_cancellation_count=void_counts[operator],
                    )
                evaluation = engine.evaluate_with_suppressions(
                    FraudContext(
                        transaction=transaction,
                        order_events=order_events,
                        comparison_documents=comparison_documents,
                        operator_stats=stats,
                        whitelist_entries=whitelists,
                        evaluated_at=now,
                    )
                )
                evaluated += 1
                findings_detected += len(evaluation.findings)
                findings_suppressed += len(evaluation.suppressed)
                for finding in evaluation.findings:
                    inserted, evidence_count = self._persist_finding(
                        session,
                        correlation,
                        finding,
                        rule_rows,
                        by_id,
                    )
                    alerts_inserted += int(inserted)
                    evidence_inserted += evidence_count
                for suppressed in evaluation.suppressed:
                    inserted, evidence_count = self._persist_finding(
                        session,
                        correlation,
                        suppressed.finding,
                        rule_rows,
                        by_id,
                        suppression=suppressed,
                    )
                    alerts_inserted += int(inserted)
                    evidence_inserted += evidence_count
            # Resolve stale alerts only after replacement correlations have been
            # evaluated in this transaction.  This lets us distinguish an amount
            # discrepancy genuinely fixed by late evidence (for example the second
            # half of a split payment) from an anomaly that is still present.
            alerts_superseded = self._resolve_superseded_alerts(session, now)
            return FraudRunReport(
                correlations_loaded=len(correlations),
                transactions_evaluated=evaluated,
                findings_detected=findings_detected,
                findings_suppressed=findings_suppressed,
                alerts_inserted=alerts_inserted,
                evidence_inserted=evidence_inserted,
                alerts_superseded=alerts_superseded,
            )

    @staticmethod
    def _resolve_superseded_alerts(session: Session, now: datetime) -> int:
        """Justify stale automatic alerts only after a superset correlation exists."""

        alerts = session.scalars(
            select(FraudAlert)
            .join(DocumentCorrelation, DocumentCorrelation.id == FraudAlert.correlation_id)
            .where(
                DocumentCorrelation.status == "SUPERSEDED",
                FraudAlert.status == "OPEN",
            )
            .order_by(FraudAlert.opened_at, FraudAlert.id)
            .with_for_update()
        ).all()
        resolved = 0
        for alert in alerts:
            if alert.correlation_id is None:
                continue
            old_members = set(
                session.scalars(
                    select(DocumentCorrelationMember.document_id).where(
                        DocumentCorrelationMember.correlation_id == alert.correlation_id
                    )
                )
            )
            candidates = session.scalars(
                select(DocumentCorrelation)
                .join(
                    DocumentCorrelationMember,
                    DocumentCorrelationMember.correlation_id == DocumentCorrelation.id,
                )
                .where(
                    DocumentCorrelation.id != alert.correlation_id,
                    DocumentCorrelation.status.in_(("AUTOMATIC", "UNCORRELATED")),
                    DocumentCorrelationMember.document_id.in_(old_members),
                )
                .distinct()
            ).all()
            replacements: list[DocumentCorrelation] = []
            for candidate in candidates:
                candidate_members = set(
                    session.scalars(
                        select(DocumentCorrelationMember.document_id).where(
                            DocumentCorrelationMember.correlation_id == candidate.id
                        )
                    )
                )
                if old_members < candidate_members:
                    replacements.append(candidate)
            if not replacements:
                continue

            old_rule_code = session.scalar(
                select(FraudRule.code)
                .join(
                    FraudRuleVersion,
                    FraudRuleVersion.fraud_rule_id == FraudRule.id,
                )
                .where(FraudRuleVersion.id == alert.fraud_rule_version_id)
            )
            replacement_ids = [item.id for item in replacements]
            replacement_has_same_finding = session.scalar(
                select(FraudAlert.id)
                .join(
                    FraudRuleVersion,
                    FraudRuleVersion.id == FraudAlert.fraud_rule_version_id,
                )
                .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
                .where(
                    FraudAlert.correlation_id.in_(replacement_ids),
                    FraudAlert.status.in_(("OPEN", "UNDER_REVIEW", "CONFIRMED")),
                    FraudRule.code == old_rule_code,
                )
                .limit(1)
            )
            if replacement_has_same_finding is not None:
                continue

            previous_status = alert.status
            alert.status = "JUSTIFIED"
            alert.closed_at = now
            alert.updated_at = now
            alert.closure_reason = (
                "Correlazione sostituita da un aggregato successivo contenente nuove "
                "evidenze; l'alert originario non rappresenta più lo stato corrente."
            )
            evidence_sequence = (
                session.scalar(
                    select(func.max(FraudAlertEvidence.sequence)).where(
                        FraudAlertEvidence.fraud_alert_id == alert.id
                    )
                )
                or 0
            ) + 1
            replacement_id_strings = sorted(str(item.id) for item in replacements)
            session.add(
                FraudAlertEvidence(
                    fraud_alert_id=alert.id,
                    sequence=evidence_sequence,
                    evidence_type="CORRELATION_SUPERSEDED",
                    summary="Alert giustificato dopo correlazione con evidenze tardive",
                    evidence={
                        "kind": "correlation_superseded",
                        "previous_correlation_id": str(alert.correlation_id),
                        "replacement_correlation_ids": replacement_id_strings,
                        "previous_document_ids": sorted(str(value) for value in old_members),
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
            history_sequence = 1 if latest is None else latest.sequence + 1
            previous_hash = ZERO_HASH if latest is None else latest.record_hash
            payload = {
                "alert_id": str(alert.id),
                "sequence": history_sequence,
                "actor_user_id": None,
                "event_type": "ALERT_AUTO_SUPERSEDED",
                "previous_status": previous_status,
                "new_status": "JUSTIFIED",
                "note": None,
                "reason": alert.closure_reason,
                "occurred_at": now.isoformat(),
                "previous_hash": previous_hash,
            }
            session.add(
                FraudAlertHistory(
                    fraud_alert_id=alert.id,
                    sequence=history_sequence,
                    event_type="ALERT_AUTO_SUPERSEDED",
                    previous_status=previous_status,
                    new_status="JUSTIFIED",
                    reason=alert.closure_reason,
                    occurred_at=now,
                    previous_record_hash=previous_hash,
                    record_hash=chained_hash(payload, previous_hash),
                )
            )
            resolved += 1
        return resolved

    def _ensure_and_load_rules(
        self, session: Session, now: datetime
    ) -> dict[str, tuple[RuleDefinition, FraudRuleVersion]]:
        for default in DEFAULT_RULES:
            rule = session.scalar(select(FraudRule).where(FraudRule.code == default.code))
            if rule is None:
                rule = FraudRule(
                    code=default.code,
                    name=default.code.replace("_", " ").title(),
                    description=f"Regola deterministica {default.code}",
                    enabled=default.enabled,
                )
                session.add(rule)
                session.flush()
            parameters = dict(default.parameters)
            parameters.update(self._parameter_overrides.get(default.code, {}))
            safe_parameters = _json_safe(parameters)
            desired_fingerprint = rule_configuration_fingerprint(
                implementation_version=FRAUD_ENGINE_VERSION,
                enabled=rule.enabled,
                severity=default.severity.value,
                weight=default.weight,
                configuration=safe_parameters,
            )
            latest = session.scalar(
                select(FraudRuleVersion)
                .where(FraudRuleVersion.fraud_rule_id == rule.id)
                .order_by(FraudRuleVersion.version.desc())
                .limit(1)
                .with_for_update()
            )
            if latest is None or latest.configuration_fingerprint != desired_fingerprint:
                if latest is not None and latest.effective_until is None:
                    latest.effective_until = now
                version = FraudRuleVersion(
                    fraud_rule_id=rule.id,
                    version=1 if latest is None else latest.version + 1,
                    implementation_version=FRAUD_ENGINE_VERSION,
                    configuration_fingerprint=desired_fingerprint,
                    enabled=rule.enabled,
                    severity=default.severity.value,
                    threshold=next(
                        (
                            Decimal(str(parameters[key]))
                            for key in (
                                "minimum_amount",
                                "minimum_percent",
                                "tolerance",
                                "minimum_count",
                                "maximum_count",
                            )
                            if key in parameters
                        ),
                        None,
                    ),
                    weight=default.weight,
                    configuration=safe_parameters,
                    effective_from=now,
                )
                session.add(version)
        session.flush()

        result: dict[str, tuple[RuleDefinition, FraudRuleVersion]] = {}
        rules = session.scalars(select(FraudRule).order_by(FraudRule.code)).all()
        for rule in rules:
            version = session.scalar(
                select(FraudRuleVersion)
                .where(
                    FraudRuleVersion.fraud_rule_id == rule.id,
                    FraudRuleVersion.effective_from <= now,
                    (
                        (FraudRuleVersion.effective_until.is_(None))
                        | (FraudRuleVersion.effective_until > now)
                    ),
                )
                .order_by(FraudRuleVersion.version.desc())
                .limit(1)
            )
            if version is None or rule.code not in {item.code for item in DEFAULT_RULES}:
                continue
            try:
                severity = AlertSeverity(version.severity)
            except ValueError:
                severity = AlertSeverity.MEDIUM
            result[rule.code] = (
                RuleDefinition(
                    code=rule.code,
                    version=version.version,
                    enabled=rule.enabled and version.enabled,
                    severity=severity,
                    weight=version.weight,
                    parameters=dict(version.configuration or {}),
                ),
                version,
            )
        return result

    @staticmethod
    def _current_correlations(
        session: Session, *, limit: int | None
    ) -> tuple[DocumentCorrelation, ...]:
        statement = (
            select(DocumentCorrelation)
            .where(
                DocumentCorrelation.algorithm_version == ALGORITHM_VERSION,
                DocumentCorrelation.status.in_(("AUTOMATIC", "UNCORRELATED")),
            )
            .order_by(DocumentCorrelation.created_at.desc(), DocumentCorrelation.id.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        rows = session.scalars(statement).all()
        current: dict[UUID, DocumentCorrelation] = {}
        for row in rows:
            current.setdefault(row.transaction_id, row)
        return tuple(current.values())

    @staticmethod
    def _load_order_events(
        session: Session, document_ids: set[UUID]
    ) -> tuple[DomainOrderEvent, ...]:
        rows = session.scalars(
            select(OrderEvent)
            .where(OrderEvent.source_document_id.in_(document_ids))
            .order_by(OrderEvent.occurred_at, OrderEvent.sequence, OrderEvent.id)
        ).all()
        result: list[DomainOrderEvent] = []
        for row in rows:
            try:
                event_type = OrderEventType(row.event_type)
            except ValueError:
                continue
            result.append(
                DomainOrderEvent(
                    id=row.id,
                    order_id=row.order_id,
                    type=event_type,
                    occurred_at=row.occurred_at,
                    source_document_id=row.source_document_id,
                    operator_code=row.operator_code,
                    details=row.details or {},
                    previous_hash=row.previous_record_hash,
                    record_hash=row.record_hash,
                )
            )
        return tuple(result)

    @staticmethod
    def _load_whitelists(session: Session, now: datetime) -> tuple[WhitelistEntry, ...]:
        rows = session.execute(
            select(FraudWhitelist, FraudRule.code)
            .outerjoin(FraudRule, FraudRule.id == FraudWhitelist.fraud_rule_id)
            .where(
                FraudWhitelist.active.is_(True),
                FraudWhitelist.valid_from <= now,
                (FraudWhitelist.valid_until.is_(None)) | (FraudWhitelist.valid_until > now),
            )
            .order_by(FraudWhitelist.created_at, FraudWhitelist.id)
        ).all()
        entries: list[WhitelistEntry] = []
        for row, rule_code in rows:
            try:
                scope = WhitelistScope(row.scope_type)
            except ValueError:
                continue
            entries.append(
                WhitelistEntry(
                    id=row.id,
                    rule_code=rule_code,
                    scope=scope,
                    scope_value=row.scope_value,
                    reason=row.reason,
                    valid_from=row.valid_from,
                    valid_until=row.valid_until,
                )
            )
        return tuple(entries)

    @staticmethod
    def _operator_statistics(
        session: Session,
        correlations: tuple[DocumentCorrelation, ...],
        loaded_by_id: dict[UUID, LoadedDocument],
    ) -> tuple[dict[str, set[UUID]], set[UUID], dict[str, int]]:
        counts: dict[str, set[UUID]] = defaultdict(set)
        void_counts: dict[str, int] = defaultdict(int)
        for correlation in correlations:
            ids = session.scalars(
                select(DocumentCorrelationMember.document_id).where(
                    DocumentCorrelationMember.correlation_id == correlation.id
                )
            )
            seen_void: set[UUID] = set()
            for document_id in ids:
                item = loaded_by_id.get(document_id)
                if item is None or item.value.operator_code is None:
                    continue
                operator = item.value.operator_code
                counts[operator].add(correlation.transaction_id)
                if item.value.type.value in {"CANCELLATION", "REFUND"}:
                    seen_void.add(item.value.id)
                    void_counts[operator] += 1
        transaction_ids = {correlation.transaction_id for correlation in correlations}
        anomalous = set(
            session.scalars(
                select(FraudAlert.transaction_id)
                .where(FraudAlert.transaction_id.in_(transaction_ids))
                .distinct()
            )
        )
        return counts, anomalous, void_counts

    @staticmethod
    def _persist_finding(
        session: Session,
        correlation: DocumentCorrelation,
        finding: Any,
        rule_rows: dict[str, tuple[RuleDefinition, FraudRuleVersion]],
        loaded_by_id: dict[UUID, LoadedDocument],
        *,
        suppression: SuppressedFinding | None = None,
    ) -> tuple[bool, int]:
        row = rule_rows.get(finding.rule_code)
        if row is None:
            return False, 0
        _, rule_version = row
        finding_key = finding_fingerprint(finding)
        existing = session.scalar(
            select(FraudAlert.id).where(
                FraudAlert.fraud_rule_version_id == rule_version.id,
                FraudAlert.transaction_id == finding.transaction_id,
                FraudAlert.finding_key == finding_key,
            )
        )
        if existing is not None:
            return False, 0
        documents = [
            loaded_by_id[document_id].value
            for document_id in finding.document_ids
            if document_id in loaded_by_id
        ]
        prebills = [item for item in documents if item.type.value == "PRE_BILL"]
        fiscals = [
            item for item in documents if item.type.value in {"COMMERCIAL_DOCUMENT", "REFUND"}
        ]
        original_amount = prebills[-1].gross_total if prebills else None
        final_amount = (
            sum(
                (
                    -(item.gross_total or Decimal("0"))
                    if item.type.value == "REFUND"
                    else (item.gross_total or Decimal("0"))
                )
                for item in fiscals
            )
            if fiscals
            else None
        )
        difference = (
            None
            if original_amount is None or final_amount is None
            else original_amount - final_amount
        )
        difference_percent = (
            None
            if difference is None or original_amount == 0
            else (difference / original_amount * Decimal("100")).quantize(Decimal("0.0001"))
        )
        status = "JUSTIFIED" if suppression is not None else "OPEN"
        alert = FraudAlert(
            fraud_rule_version_id=rule_version.id,
            correlation_id=correlation.id,
            transaction_id=finding.transaction_id,
            finding_key=finding_key,
            severity=finding.severity.value,
            score=finding.score,
            status=status,
            description=finding.description,
            explanation=finding.explanation,
            original_amount=original_amount,
            final_amount=final_amount,
            difference_amount=difference,
            difference_percent=difference_percent,
            confidence=finding.confidence,
            closed_at=finding.opened_at if suppression is not None else None,
            closure_reason=(
                None
                if suppression is None
                else f"Whitelist {suppression.whitelist_id}: {suppression.reason}"
            ),
            opened_at=finding.opened_at,
        )
        session.add(alert)
        session.flush()
        serialized_evidence = finding.model_dump(mode="json")["evidence"]
        for sequence, evidence in enumerate(serialized_evidence, start=1):
            candidate_id = evidence.get("document_id")
            try:
                document_id = UUID(candidate_id) if candidate_id else None
            except (TypeError, ValueError):
                document_id = None
            if document_id not in loaded_by_id:
                document_id = (
                    finding.document_ids[sequence - 1]
                    if sequence <= len(finding.document_ids)
                    and finding.document_ids[sequence - 1] in loaded_by_id
                    else None
                )
            loaded = loaded_by_id.get(document_id) if document_id is not None else None
            raw = (
                session.get(RawPayload, loaded.raw_payload_id)
                if loaded is not None and loaded.raw_payload_id is not None
                else None
            )
            session.add(
                FraudAlertEvidence(
                    fraud_alert_id=alert.id,
                    sequence=sequence,
                    document_id=document_id,
                    print_job_id=None if loaded is None else loaded.database_job_id,
                    raw_payload_id=None if raw is None else raw.id,
                    evidence_type=str(evidence.get("kind", "RULE_EVIDENCE"))[:64],
                    summary=finding.description,
                    evidence=evidence,
                    artifact_path=None if raw is None else raw.source_path,
                    artifact_sha256=None if raw is None else raw.sha256,
                )
            )
        history_payload = {
            "alert_id": str(alert.id),
            "sequence": 1,
            "actor_user_id": None,
            "event_type": "ALERT_SUPPRESSED" if suppression else "ALERT_OPENED",
            "previous_status": None,
            "new_status": status,
            "note": None,
            "reason": None if suppression is None else suppression.reason,
            "occurred_at": finding.opened_at.isoformat(),
            "previous_hash": ZERO_HASH,
        }
        session.add(
            FraudAlertHistory(
                fraud_alert_id=alert.id,
                sequence=1,
                event_type=history_payload["event_type"],
                new_status=status,
                reason=history_payload["reason"],
                occurred_at=finding.opened_at,
                previous_record_hash=ZERO_HASH,
                record_hash=chained_hash(history_payload, ZERO_HASH),
            )
        )
        return True, len(serialized_evidence)


__all__ = ["FRAUD_ENGINE_VERSION", "FraudRunReport", "FraudWorker"]
