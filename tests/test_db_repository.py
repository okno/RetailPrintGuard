from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from retailprintguard.api.auth import PasswordService
from retailprintguard.api.schemas import (
    AlertUpdate,
    AuditEntry,
    JobReviewRequest,
    RoleName,
)
from retailprintguard.common.domain import DocumentType
from retailprintguard.db import Base, create_db_engine, session_factory
from retailprintguard.db.models import (
    ActiveParserVersion,
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
    ImportBatch,
    ImportItem,
    Order,
    OrderEvent,
    ParserVersion,
    PrintJob,
    ProxySession,
    RawPayload,
    Role,
    SystemEvent,
    User,
    UserRole,
)
from retailprintguard.db.models import StreamChunk as StoredChunk
from retailprintguard.db.repository import (
    SqlAlchemyApiRepository,
    SqlAlchemyIngestionRepository,
)
from retailprintguard.ingestion.dto import (
    ArtifactRole,
    ArtifactSnapshot,
    Endpoint,
    NormalizedDocument,
    NormalizedEnvelope,
    SourceKind,
    StreamChunk,
    StreamDirection,
)
from retailprintguard.ingestion.repository import ImportDisposition

NOW = datetime(2042, 5, 6, 18, 0, tzinfo=UTC)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _factory():
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine, session_factory(engine)


def _seed_api(factory):
    ids = {name: uuid4() for name in ("device", "session", "job", "raw", "document")}
    ids.update({name: uuid4() for name in ("parser", "user", "rule", "rule_version")})
    ids.update(
        {name: uuid4() for name in ("correlation", "transaction", "alert", "duplicate_alert")}
    )
    with factory.begin() as session:
        session.add(
            Device(
                id=ids["device"],
                external_id="pos_1",
                name="POS uno",
                device_type="pos",
                parser_kind="escpos",
                mac_address="02:00:00:00:01:01",
                department="Bar",
                role="comande_bar",
                listen_ip="192.0.2.10",
                listen_port=9100,
                target_ip="192.0.2.20",
                target_port=9100,
            )
        )
        session.add(
            User(
                id=ids["user"],
                username="auditor",
                display_name="Auditor",
                password_hash=PasswordService().hash("correct-password"),
            )
        )
        role = Role(code="AUDITOR", name="Auditor")
        session.add(role)
        session.flush()
        session.add(UserRole(user_id=ids["user"], role_id=role.id))
        session.flush()
        session.add(
            ProxySession(
                id=ids["session"],
                device_id=ids["device"],
                source_system="test",
                source_instance="one",
                source_scope="192.0.2.20:9100",
                source_session_id="session-one",
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
                id=ids["job"],
                device_id=ids["device"],
                session_id=ids["session"],
                source_key="test:one:job-one",
                source_system="test",
                source_instance="one",
                source_scope="192.0.2.20:9100",
                source_job_id="job-one",
                source_schema="test.v1",
                manifest_sha256="a" * 64,
                manifest_path="/spool/job-one/manifest.json",
                started_at=NOW,
                captured_at=NOW,
                status="IMPORTED",
                capture_complete=True,
                timeline_complete=True,
            )
        )
        session.add(
            ParserVersion(
                id=ids["parser"],
                name="synthetic",
                version="1",
                build_sha256="b" * 64,
                protocol="test",
            )
        )
        session.flush()
        session.add(
            RawPayload(
                id=ids["raw"],
                job_id=ids["job"],
                device_id=ids["device"],
                session_id=ids["session"],
                artifact_role="REQUEST_RAW",
                direction="CLIENT_TO_DEVICE",
                payload=b"PRECONTO 100,00",
                byte_count=15,
                sha256="c" * 64,
                source_path="/spool/job-one/client.raw",
                source_path_sha256=_sha(b"/spool/job-one/client.raw"),
                complete=True,
                chain_scope=f"raw:{ids['job']}",
                chain_sequence=1,
                previous_record_hash="0" * 64,
                record_hash="d" * 64,
            )
        )
        session.add(
            Document(
                id=ids["document"],
                device_id=ids["device"],
                session_id=ids["session"],
                job_id=ids["job"],
                source_document_key="doc-one",
                document_type="PRE_BILL",
                subtype="PRECONTO",
                external_document_code="PB-0001",
                order_code="ORD-80",
                table_code="LAB-25",
                captured_at=NOW,
            )
        )
        session.flush()
        version = DocumentVersion(
            document_id=ids["document"],
            parser_version_id=ids["parser"],
            raw_payload_id=ids["raw"],
            version_sequence=1,
            gross_total=Decimal("100.00"),
            status="COMPLETE",
            normalized_text="PRECONTO ORD-80 TOTALE 100,00",
            parse_confidence=100,
            evidence_level="CONFIRMED",
            source_manifest_sha256="a" * 64,
            source_payload_sha256="c" * 64,
            source_path="/spool/job-one/client.raw",
            complete=True,
            chain_scope=f"document:{ids['job']}",
            chain_sequence=1,
            previous_record_hash="0" * 64,
            record_hash="e" * 64,
        )
        session.add(version)
        session.flush()
        session.add(
            DocumentLine(
                document_version_id=version.id,
                sequence=1,
                course_code="2",
                description="Crudo e melone",
                quantity=Decimal("1"),
                unit_price=Decimal("100.00"),
                line_total=Decimal("100.00"),
            )
        )
        session.add(
            DocumentCorrelation(
                id=ids["correlation"],
                transaction_id=ids["transaction"],
                algorithm_version="test-1",
                input_fingerprint="f" * 64,
                score=95,
                explanation="Riferimento ordine condiviso",
            )
        )
        session.flush()
        session.add(
            DocumentCorrelationMember(
                correlation_id=ids["correlation"],
                document_id=ids["document"],
                role="PRE_BILL",
                contribution_score=95,
            )
        )
        session.add(
            FraudRule(
                id=ids["rule"],
                code="PREBILL_FISCAL_AMOUNT_DROP",
                name="Calo importo",
                description="Riduzione tra preconto e fiscale",
            )
        )
        session.flush()
        session.add(
            FraudRuleVersion(
                id=ids["rule_version"],
                fraud_rule_id=ids["rule"],
                version=1,
                implementation_version="test-1",
                configuration_fingerprint="9" * 64,
                severity="HIGH",
                threshold=Decimal("20"),
                weight=Decimal("1"),
                effective_from=NOW,
            )
        )
        session.flush()
        session.add(
            FraudAlert(
                id=ids["alert"],
                fraud_rule_version_id=ids["rule_version"],
                correlation_id=ids["correlation"],
                transaction_id=ids["transaction"],
                finding_key="f" * 64,
                severity="HIGH",
                score=90,
                status="OPEN",
                description="Calo sospetto",
                explanation="100 -> 50",
                difference_amount=Decimal("50.00"),
                confidence=95,
                opened_at=NOW,
            )
        )
        session.flush()
        session.add(
            FraudAlert(
                id=ids["duplicate_alert"],
                fraud_rule_version_id=ids["rule_version"],
                correlation_id=ids["correlation"],
                transaction_id=ids["transaction"],
                finding_key="8" * 64,
                severity="HIGH",
                score=90,
                status="OPEN",
                is_canonical=False,
                duplicate_of_alert_id=ids["alert"],
                deduplicated_at=NOW + timedelta(seconds=1),
                deduplication_reason="duplicate regression fixture",
                description="Calo sospetto duplicato storico",
                explanation="100 -> 50",
                difference_amount=Decimal("50.00"),
                confidence=95,
                opened_at=NOW + timedelta(seconds=1),
            )
        )
        session.flush()
        session.add(
            FraudAlertEvidence(
                fraud_alert_id=ids["alert"],
                sequence=1,
                document_id=ids["document"],
                raw_payload_id=ids["raw"],
                evidence_type="AMOUNT_DIFF",
                summary="Differenza 50 euro",
                evidence={"difference": "50.00"},
            )
        )
        session.add(
            SystemEvent(
                service="parser",
                severity="WARNING",
                event_type="SYNTHETIC_DIAGNOSTIC",
                message="Evento diagnostico sintetico",
                device_id=ids["device"],
                session_id=ids["session"],
                job_id=ids["job"],
                correlation_id="diagnostic-request-1",
                occurred_at=NOW,
            )
        )
    return ids


def _add_correlated_document(
    session,
    ids,
    *,
    document_type: str,
    total: str | None,
    complete: bool,
    minute: int = 1,
    raw_metadata: dict[str, object] | None = None,
    line_total: str | None = None,
):
    document_id = uuid4()
    document = Document(
        id=document_id,
        device_id=ids["device"],
        session_id=ids["session"],
        job_id=ids["job"],
        source_document_key=f"doc-{document_id}",
        document_type=document_type,
        subtype=document_type,
        external_document_code=f"{document_type}-{document_id}",
        order_code="ORD-80",
        table_code="LAB-25",
        document_timestamp=NOW + timedelta(minutes=minute),
        captured_at=NOW + timedelta(minutes=minute, seconds=1),
    )
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document_id,
        parser_version_id=ids["parser"],
        raw_payload_id=ids["raw"],
        version_sequence=1,
        document_type=document_type,
        subtype=document_type,
        external_document_code=document.external_document_code,
        order_code=document.order_code,
        table_code=document.table_code,
        document_timestamp=document.document_timestamp,
        gross_total=None if total is None else Decimal(total),
        status="COMPLETE" if complete else "PARTIAL",
        normalized_text=document_type,
        parse_confidence=100,
        evidence_level="CONFIRMED",
        source_manifest_sha256="a" * 64,
        source_payload_sha256=hashlib.sha256(str(document_id).encode()).hexdigest(),
        source_path=f"/spool/{document_id}.raw",
        complete=complete,
        raw_metadata=raw_metadata or {},
        chain_scope=f"document:{document_id}",
        chain_sequence=1,
        previous_record_hash="0" * 64,
        record_hash=hashlib.sha256(f"record:{document_id}".encode()).hexdigest(),
    )
    session.add(version)
    session.flush()
    if line_total is not None:
        session.add(
            DocumentLine(
                document_version_id=version.id,
                sequence=1,
                description="Crudo e melone",
                quantity=Decimal("1"),
                unit_price=Decimal(line_total),
                line_total=Decimal(line_total),
            )
        )
    session.add(
        DocumentCorrelationMember(
            correlation_id=ids["correlation"],
            document_id=document_id,
            role=document_type,
            contribution_score=95,
        )
    )
    return document_id, version.id


def test_commercial_progressive_is_resolved_only_from_strong_current_correlation() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    strong_correlation_id = uuid4()
    with factory.begin() as session:
        fiscal_id, fiscal_version_id = _add_correlated_document(
            session,
            ids,
            document_type="COMMERCIAL_DOCUMENT",
            total="0.10",
            complete=True,
            line_total="0.10",
        )
        management_id, management_version_id = _add_correlated_document(
            session,
            ids,
            document_type="MANAGEMENT_DOCUMENT",
            total="0.10",
            complete=True,
            minute=2,
            line_total="0.10",
        )
        fiscal = session.get(Document, fiscal_id)
        fiscal_version = session.get(DocumentVersion, fiscal_version_id)
        management = session.get(Document, management_id)
        management_version = session.get(DocumentVersion, management_version_id)
        assert fiscal is not None and fiscal_version is not None
        assert management is not None and management_version is not None
        fiscal.external_document_code = None
        fiscal.external_document_code_suffix = "0042"
        fiscal_version.external_document_code = None
        fiscal_version.external_document_code_suffix = "0042"
        management.external_document_code = None
        management.commercial_reference_code = "9901-0042"
        management_version.external_document_code = None
        management_version.commercial_reference_code = "9901-0042"
        session.add(
            DocumentCorrelation(
                id=strong_correlation_id,
                transaction_id=uuid4(),
                algorithm_version="rpg-correlation-1.4.0",
                input_fingerprint=hashlib.sha256(b"strong-progressive").hexdigest(),
                score=100,
                status="AUTOMATIC",
                matched_criteria=[
                    "commercial_reference_to_observed_fiscal_suffix",
                    "table_code",
                    "line_identity_overlap",
                    "time_proximity",
                ],
                explanation="Correlazione progressivo sintetica forte",
            )
        )
        session.flush()
        session.add_all(
            [
                DocumentCorrelationMember(
                    correlation_id=strong_correlation_id,
                    document_id=fiscal_id,
                    role="FISCAL",
                    contribution_score=100,
                ),
                DocumentCorrelationMember(
                    correlation_id=strong_correlation_id,
                    document_id=management_id,
                    role="MANAGEMENT_COPY",
                    contribution_score=100,
                ),
            ]
        )

    selected = SqlAlchemyApiRepository(factory).get_document(fiscal_id)
    assert selected is not None
    assert selected.external_document_code is None
    assert selected.external_document_code_suffix == "0042"
    assert selected.resolved_external_document_code == "9901-0042"
    assert (
        selected.resolved_external_document_code_provenance
        == "CORRELATED_MANAGEMENT_REFERENCE"
    )
    assert selected.progressive_observation_status == "SUFFIX_ONLY_OBSERVED_IN_CAPTURE"
    engine.dispose()


def test_sqlalchemy_api_repository_read_models_workflow_and_audit_chain() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    repository = SqlAlchemyApiRepository(factory)

    principal = repository.authenticate("AUDITOR", "correct-password")
    assert principal is not None and principal.roles == (RoleName.AUDITOR,)
    assert repository.authenticate("auditor", "bad-password") is None
    rule_view = repository.set_rule_enabled("PREBILL_FISCAL_AMOUNT_DROP", False, principal)
    assert rule_view is not None and rule_view.enabled is False and rule_view.version == 2
    dashboard = repository.dashboard()
    assert dashboard.open_alerts == 1
    assert dashboard.economic_difference == Decimal("50.0000")
    device = repository.list_devices()[0]
    assert (device.id, device.mac_address, device.department, device.role) == (
        "pos_1",
        "02:00:00:00:01:01",
        "Bar",
        "comande_bar",
    )
    documents, total = repository.list_documents(
        limit=20, offset=0, filters={"order_code": "ORD-80"}
    )
    assert total == 1 and documents[0].lines[0].description == "Crudo e melone"
    assert documents[0].lines[0].course_code == "2"
    assert repository.get_document_raw(ids["document"]).content == b"PRECONTO 100,00"
    hits, hit_count = repository.search(query="melone", limit=10, offset=0)
    assert hit_count == 1 and hits[0].entity_id == ids["document"]
    transactions, transaction_count = repository.list_transactions(
        limit=20,
        offset=0,
        filters={"from": NOW - timedelta(minutes=1), "to": NOW + timedelta(minutes=1)},
    )
    assert transaction_count == 1 and transactions[0].id == ids["transaction"]
    _, future_transaction_count = repository.list_transactions(
        limit=20,
        offset=0,
        filters={"from": NOW + timedelta(days=1)},
    )
    assert future_transaction_count == 0
    alerts, alert_count = repository.list_alerts(limit=20, offset=0, filters={})
    assert alert_count == 1 and [alert.id for alert in alerts] == [ids["alert"]]
    assert repository.get_alert(ids["duplicate_alert"]) is not None
    diagnostics = repository.diagnostics()
    assert diagnostics.database == "ok"
    assert diagnostics.parser_errors == 0 and diagnostics.incomplete_jobs == 0
    assert diagnostics.recent_events[0].correlation_id == "diagnostic-request-1"

    updated = repository.update_alert(
        ids["alert"],
        AlertUpdate(status="UNDER_REVIEW", assigned_to_me=True, note="verifica"),
        principal,
    )
    assert updated is not None and updated.status == "UNDER_REVIEW"
    assert updated.assigned_to == principal.id and len(updated.history) == 1

    for sequence in range(2):
        repository.append_audit(
            AuditEntry(
                actor_id=principal.id,
                action="TEST_ACTION",
                entity_type="document",
                entity_id=str(ids["document"]),
                correlation_id=f"request-{sequence}",
                occurred_at=NOW + timedelta(seconds=sequence),
                metadata={"sequence": sequence},
            )
        )
    with factory() as session:
        history = session.scalars(
            select(FraudAlertHistory).order_by(FraudAlertHistory.sequence)
        ).all()
        audits = session.scalars(select(AuditLog).order_by(AuditLog.sequence)).all()
        rule_versions = session.scalars(
            select(FraudRuleVersion).order_by(FraudRuleVersion.version)
        ).all()
        assert history[0].previous_record_hash == "0" * 64
        assert [version.enabled for version in rule_versions] == [True, False]
        assert rule_versions[0].effective_until is not None
        assert rule_versions[1].created_by_user_id == principal.id
        assert [row.sequence for row in audits] == [1, 2]
        assert audits[1].previous_record_hash == audits[0].record_hash
    engine.dispose()


def test_alert_filters_combine_device_with_active_and_legacy_operator_semantics() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    shadow_parser_id = uuid4()
    with factory.begin() as session:
        document = session.get(Document, ids["document"])
        active_version = session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == ids["document"],
                DocumentVersion.version_sequence == 1,
            )
        )
        assert document is not None and active_version is not None
        # The mutable projection and a newer shadow parse must not affect the
        # operator filter while parser version 1 remains active.
        document.operator_code = "MUTABLE-SHADOW"
        active_version.operator_code = "ACTIVE-42"
        session.add(
            ParserVersion(
                id=shadow_parser_id,
                name="synthetic",
                version="2",
                build_sha256="1" * 64,
                protocol="test",
            )
        )
        session.add(
            ActiveParserVersion(
                parser_name="synthetic",
                parser_version_id=ids["parser"],
                activation_reason="keep validated operator semantics active",
            )
        )
        session.flush()
        session.add(
            DocumentVersion(
                document_id=ids["document"],
                parser_version_id=shadow_parser_id,
                raw_payload_id=ids["raw"],
                version_sequence=2,
                document_type="PRE_BILL",
                operator_code="SHADOW-99",
                gross_total=Decimal("100.00"),
                status="COMPLETE",
                normalized_text="SHADOW OPERATOR",
                parse_confidence=90,
                evidence_level="INFERRED",
                source_manifest_sha256="a" * 64,
                source_payload_sha256="1" * 64,
                source_path="/spool/job-one/client.raw",
                complete=True,
                chain_scope=f"document:{ids['job']}:operator-shadow",
                chain_sequence=1,
                previous_record_hash="0" * 64,
                record_hash="1" * 64,
            )
        )
        legacy_document_id, legacy_version_id = _add_correlated_document(
            session,
            ids,
            document_type="KITCHEN_ORDER",
            total=None,
            complete=True,
            minute=2,
        )
        legacy_document = session.get(Document, legacy_document_id)
        legacy_version = session.get(DocumentVersion, legacy_version_id)
        assert legacy_document is not None and legacy_version is not None
        legacy_document.operator_code = "LEGACY-7"
        for field in (
            "document_type",
            "subtype",
            "external_document_code",
            "order_code",
            "table_code",
            "operator_code",
            "terminal_code",
            "document_timestamp",
        ):
            setattr(legacy_version, field, None)

    repository = SqlAlchemyApiRepository(factory)
    active_alerts, active_count = repository.list_alerts(
        limit=10,
        offset=0,
        filters={"device_id": "pos_1", "operator_code": "ACTIVE-42"},
    )
    legacy_alerts, legacy_count = repository.list_alerts(
        limit=10,
        offset=0,
        filters={"device_id": "pos_1", "operator_code": "LEGACY-7"},
    )
    for leaked_operator in ("SHADOW-99", "MUTABLE-SHADOW"):
        leaked, leaked_count = repository.list_alerts(
            limit=10,
            offset=0,
            filters={"device_id": "pos_1", "operator_code": leaked_operator},
        )
        assert leaked_count == 0 and leaked == []
    assert active_count == 1 and [alert.id for alert in active_alerts] == [ids["alert"]]
    assert legacy_count == 1 and [alert.id for alert in legacy_alerts] == [ids["alert"]]
    engine.dispose()


def test_incomplete_job_review_is_audited_and_preserves_evidence() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    with factory.begin() as session:
        job = session.get(PrintJob, ids["job"])
        assert job is not None
        job.capture_complete = False
        job.status = "PARTIAL"
    repository = SqlAlchemyApiRepository(factory)
    principal = repository.authenticate("auditor", "correct-password")
    assert principal is not None

    reviewed = repository.review_job(
        ids["job"],
        JobReviewRequest(
            action="VERIFY_USABLE",
            reason="Documento semanticamente completo nonostante il tail tecnico.",
            confirmation_password="not-persisted-password",
        ),
        principal,
        correlation_id="review-request-1",
    )
    assert reviewed is not None
    assert reviewed.review_state == "VERIFIED_USABLE"
    assert reviewed.analysis_excluded is False
    assert repository.diagnostics().incomplete_jobs == 0

    excluded = repository.review_job(
        ids["job"],
        JobReviewRequest(
            action="EXCLUDE_FROM_ANALYSIS",
            reason="Evidenza tecnicamente incompleta esclusa dalla sola analisi.",
            confirmation_password="not-persisted-password",
        ),
        principal,
        correlation_id="review-request-2",
    )
    assert excluded is not None and excluded.analysis_excluded is True

    reopened = repository.review_job(
        ids["job"],
        JobReviewRequest(
            action="REOPEN_REVIEW",
            reason=(
                "Il job deve essere rivalutato dagli engine correnti "
                "senza riesumare lo storico."
            ),
            confirmation_password="not-persisted-password",
        ),
        principal,
        correlation_id="review-request-3",
    )
    assert reopened is not None
    assert reopened.review_state == "PENDING"
    assert reopened.analysis_excluded is True
    with factory() as session:
        job = session.get(PrintJob, ids["job"])
        assert job is not None and job.manifest_sha256 == "a" * 64
        raw_count = session.scalar(
            select(func.count()).select_from(RawPayload).where(RawPayload.job_id == ids["job"])
        )
        alert = session.get(FraudAlert, ids["alert"])
        assert alert is not None and alert.status == "JUSTIFIED"
        assert alert.closure_reason is not None and "esclusa" in alert.closure_reason
        correlation = session.get(DocumentCorrelation, ids["correlation"])
        assert correlation is not None and correlation.status == "SUPERSEDED"
        audit_actions = session.scalars(
            select(AuditLog.event_type).order_by(AuditLog.sequence)
        ).all()
    assert raw_count == 1
    assert audit_actions == [
        "JOB_REVIEW_VERIFY_USABLE",
        "JOB_REVIEW_EXCLUDE_FROM_ANALYSIS",
        "JOB_REVIEW_REOPEN_REVIEW",
    ]
    engine.dispose()


def test_get_transaction_prefers_current_correlation_then_latest_historical_fallback() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    historical_correlation_id = uuid4()
    with factory.begin() as session:
        current = session.get(DocumentCorrelation, ids["correlation"])
        assert current is not None
        current.status = "AUTOMATIC"
        current.score = 88
        current.created_at = NOW
        session.add(
            DocumentCorrelation(
                id=historical_correlation_id,
                transaction_id=ids["transaction"],
                algorithm_version="test-history",
                input_fingerprint="2" * 64,
                score=1,
                status="SUPERSEDED",
                explanation="Newer historical projection must not shadow current state",
                created_at=NOW + timedelta(minutes=1),
            )
        )
        session.flush()
        session.add(
            DocumentCorrelationMember(
                correlation_id=historical_correlation_id,
                document_id=ids["document"],
                role="PRE_BILL",
                contribution_score=1,
            )
        )

    repository = SqlAlchemyApiRepository(factory)
    current_view = repository.get_transaction(ids["transaction"])
    assert current_view is not None
    assert current_view.correlation_confidence == 88
    assert current_view.document_count == 1

    with factory.begin() as session:
        current = session.get(DocumentCorrelation, ids["correlation"])
        assert current is not None
        current.status = "SUPERSEDED"
    historical_view = repository.get_transaction(ids["transaction"])
    assert historical_view is not None
    assert historical_view.correlation_confidence == 1
    assert historical_view.document_count == 1
    engine.dispose()


def test_archived_alerts_do_not_keep_transaction_or_dashboard_operational() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    repository = SqlAlchemyApiRepository(factory)
    before = repository.get_transaction(ids["transaction"])
    assert before is not None and before.status == "ALERT" and before.alert_count == 1
    with factory.begin() as session:
        alert = session.get(FraudAlert, ids["alert"])
        assert alert is not None
        alert.status = "FALSE_POSITIVE"
        alert.closed_at = NOW + timedelta(minutes=1)
        correlation = session.get(DocumentCorrelation, ids["correlation"])
        assert correlation is not None
        correlation.status = "SUPERSEDED"
    after = repository.get_transaction(ids["transaction"])
    assert after is not None and after.status == "CORRELATED" and after.alert_count == 0
    dashboard = repository.dashboard()
    assert dashboard.operational_alerts == 0
    assert dashboard.operational_economic_difference == Decimal("0.0000")
    assert dashboard.false_positive_alerts == 1
    operational, operational_count = repository.list_alerts(
        limit=10, offset=0, filters={"view": "operational"}
    )
    archived, archived_count = repository.list_alerts(
        limit=10,
        offset=0,
        filters={
            "view": "archive",
            "from": NOW - timedelta(minutes=1),
            "to": NOW + timedelta(minutes=1),
        },
    )
    contradictory, contradictory_count = repository.list_alerts(
        limit=10,
        offset=0,
        filters={"view": "operational", "status": "FALSE_POSITIVE"},
    )
    all_alerts, all_count = repository.list_alerts(
        limit=10, offset=0, filters={"view": "all"}
    )
    assert operational_count == 0 and operational == []
    assert archived_count == 1 and archived[0].id == ids["alert"]
    assert contradictory_count == 0 and contradictory == []
    assert all_count == 1 and all_alerts[0].id == ids["alert"]
    engine.dispose()


def test_dashboard_economic_cards_use_sale_occurrence_primary_rule_and_one_episode() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    primary_rule_id, primary_version_id = uuid4(), uuid4()
    with factory.begin() as session:
        legacy_alert = session.get(FraudAlert, ids["alert"])
        assert legacy_alert is not None
        legacy_alert.opened_at = NOW + timedelta(days=10)
        session.add(
            FraudRule(
                id=primary_rule_id,
                code="MODIFICA_POST_PRECONTO",
                name="Riduzione operativa",
                description="Riduzione economica primaria",
            )
        )
        session.flush()
        session.add(
            FraudRuleVersion(
                id=primary_version_id,
                fraud_rule_id=primary_rule_id,
                version=1,
                implementation_version="test-primary",
                configuration_fingerprint="1" * 64,
                severity="HIGH",
                weight=Decimal("1"),
                effective_from=NOW,
            )
        )
        session.flush()
        session.add(
            FraudAlert(
                fraud_rule_version_id=primary_version_id,
                correlation_id=ids["correlation"],
                transaction_id=ids["transaction"],
                finding_key="2" * 64,
                severity="HIGH",
                score=90,
                status="OPEN",
                description="Riduzione primaria",
                explanation="100 -> 60",
                difference_amount=Decimal("40.00"),
                confidence=95,
                opened_at=NOW + timedelta(days=11),
            )
        )

    repository = SqlAlchemyApiRepository(factory)
    dashboard = repository.dashboard(
        filters={"from": NOW - timedelta(minutes=1), "to": NOW + timedelta(minutes=2)}
    )
    assert dashboard.economic_reduction_episodes == 1
    assert dashboard.operational_economic_difference == Decimal("40.0000")
    assert dashboard.operational_alerts == 2
    drilldown_alerts, drilldown_count = repository.list_alerts(
        limit=10,
        offset=0,
        filters={
            "view": "operational",
            "from": NOW - timedelta(minutes=1),
            "to": NOW + timedelta(minutes=2),
        },
    )
    assert drilldown_count == 2 and len(drilldown_alerts) == 2
    outside_sale_period = repository.dashboard(
        filters={"from": NOW + timedelta(days=9), "to": NOW + timedelta(days=12)}
    )
    assert outside_sale_period.economic_reduction_episodes == 0
    assert outside_sale_period.operational_alerts == 0
    outside_drilldown, outside_drilldown_count = repository.list_alerts(
        limit=10,
        offset=0,
        filters={
            "view": "operational",
            "from": NOW + timedelta(days=9),
            "to": NOW + timedelta(days=12),
        },
    )
    assert outside_drilldown_count == 0 and outside_drilldown == []
    engine.dispose()


def test_dashboard_does_not_treat_non_economic_alert_difference_as_revenue_loss() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    with factory.begin() as session:
        rule = session.get(FraudRule, ids["rule"])
        assert rule is not None
        rule.code = "DUPLICATE_DOCUMENT"

    dashboard = SqlAlchemyApiRepository(factory).dashboard()
    assert dashboard.operational_alerts == 1
    assert dashboard.economic_reduction_episodes == 0
    assert dashboard.operational_economic_difference == Decimal("0.0000")
    engine.dispose()


def test_transaction_reduction_drilldown_requires_closure_and_operational_economic_alert() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    repository = SqlAlchemyApiRepository(factory)

    open_transaction = repository.get_transaction(ids["transaction"])
    assert open_transaction is not None
    assert open_transaction.fiscal_total is None
    assert open_transaction.difference is None
    assert open_transaction.diff["settlement_basis"] == "NONE"
    filtered, count = repository.list_transactions(
        limit=10,
        offset=0,
        filters={
            "operational_economic_only": True,
            "reduction_only": True,
            "minimum_difference": Decimal("0.01"),
        },
    )
    assert count == 0 and filtered == []

    with factory.begin() as session:
        _, fiscal_version_id = _add_correlated_document(
            session,
            ids,
            document_type="COMMERCIAL_DOCUMENT",
            total="50.00",
            complete=True,
            line_total="50.00",
        )
    filtered, count = repository.list_transactions(
        limit=10,
        offset=0,
        filters={
            "operational_economic_only": True,
            "reduction_only": True,
            "minimum_difference": Decimal("0.01"),
        },
    )
    assert count == 1
    assert filtered[0].id == ids["transaction"]
    assert filtered[0].difference == Decimal("50.0000")

    with factory.begin() as session:
        fiscal_version = session.get(DocumentVersion, fiscal_version_id)
        assert fiscal_version is not None
        fiscal_version.gross_total = Decimal("120.00")
    filtered, count = repository.list_transactions(
        limit=10,
        offset=0,
        filters={
            "operational_economic_only": True,
            "reduction_only": True,
            "minimum_difference": Decimal("0.01"),
        },
    )
    assert count == 0 and filtered == []

    with factory.begin() as session:
        alert = session.get(FraudAlert, ids["alert"])
        assert alert is not None
        alert.status = "FALSE_POSITIVE"
    filtered, count = repository.list_transactions(
        limit=10,
        offset=0,
        filters={"operational_economic_only": True, "reduction_only": True},
    )
    assert count == 0 and filtered == []
    engine.dispose()


def test_transaction_uses_correlated_management_pre_fiscal_baseline() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    with factory.begin() as session:
        correlation = session.get(DocumentCorrelation, ids["correlation"])
        management = session.get(Document, ids["document"])
        management_version = session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == ids["document"]
            )
        )
        assert management_version is not None
        management_line = session.scalar(
            select(DocumentLine).where(
                DocumentLine.document_version_id == management_version.id
            )
        )
        assert correlation is not None
        assert management is not None
        assert management_line is not None
        correlation.algorithm_version = "rpg-correlation-1.4.0"
        correlation.status = "AUTOMATIC"
        correlation.score = 100
        correlation.matched_criteria = [
            "table_sale_sequence",
            "line_identity_overlap",
            "time_proximity",
        ]
        management.document_type = DocumentType.MANAGEMENT_DOCUMENT.value
        management.subtype = "GESTIONALE_PRE_FISCALE"
        management.external_document_code = "MGT-LAB-0014"
        management.commercial_reference_code = None
        management_version.gross_total = Decimal("3.00")
        management_version.complete = True
        management_version.status = "COMPLETE"
        management_version.raw_metadata = {}
        management_line.description = "Prodotto laboratorio"
        management_line.unit_price = Decimal("3.00")
        management_line.line_total = Decimal("3.00")

        fiscal_id, _fiscal_version_id = _add_correlated_document(
            session,
            ids,
            document_type="COMMERCIAL_DOCUMENT",
            total="0.10",
            complete=True,
            minute=1,
            line_total="0.10",
        )
        session.flush()
        fiscal = session.get(Document, fiscal_id)
        assert fiscal is not None
        fiscal.external_document_code = None
        fiscal.external_document_code_suffix = "0042"
        fiscal_version = session.scalar(
            select(DocumentVersion).where(DocumentVersion.document_id == fiscal_id)
        )
        assert fiscal_version is not None
        fiscal_version.external_document_code = None
        fiscal_version.external_document_code_suffix = "0042"
        fiscal_line = session.scalar(
            select(DocumentLine).where(
                DocumentLine.document_version_id == fiscal_version.id
            )
        )
        assert fiscal_line is not None
        fiscal_line.description = "Prodotto laboratorio"

        copy_id, copy_version_id = _add_correlated_document(
            session,
            ids,
            document_type="MANAGEMENT_DOCUMENT",
            total="0.10",
            complete=True,
            minute=2,
            line_total="0.10",
        )
        management_copy = session.get(Document, copy_id)
        management_copy_version = session.get(DocumentVersion, copy_version_id)
        assert management_copy is not None and management_copy_version is not None
        management_copy.commercial_reference_code = "FSC-LAB-0042"
        management_copy_version.commercial_reference_code = "FSC-LAB-0042"

    repository = SqlAlchemyApiRepository(factory)
    transaction = repository.get_transaction(ids["transaction"])
    assert transaction is not None
    assert transaction.pre_bill_total == Decimal("3.00")
    assert transaction.fiscal_total == Decimal("0.10")
    assert transaction.difference == Decimal("2.90")
    assert transaction.diff["baseline_basis"] == "CORRELATED_MANAGEMENT_PRE_FISCAL"
    assert transaction.diff["baseline_document_id"] == str(ids["document"])
    assert transaction.diff["baseline_document_type"] == "MANAGEMENT_DOCUMENT"
    assert transaction.diff["lines"]["removed"] == []
    assert len(transaction.diff["lines"]["price_changed"]) == 1

    dashboard_rows, count = repository.list_transactions(
        limit=10,
        offset=0,
        filters={
            "operational_economic_only": True,
            "reduction_only": True,
            "minimum_difference": Decimal("0.01"),
        },
    )
    assert count == 1
    assert dashboard_rows[0].pre_bill_total == Decimal("3.00")
    assert dashboard_rows[0].fiscal_total == Decimal("0.10")
    assert dashboard_rows[0].difference == Decimal("2.90")
    engine.dispose()


def test_transaction_does_not_infer_management_baseline_without_line_evidence() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    with factory.begin() as session:
        correlation = session.get(DocumentCorrelation, ids["correlation"])
        management = session.get(Document, ids["document"])
        management_version = session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == ids["document"]
            )
        )
        assert correlation is not None
        assert management is not None and management_version is not None
        correlation.algorithm_version = "rpg-correlation-1.4.0"
        correlation.status = "AUTOMATIC"
        correlation.matched_criteria = ["table_sale_sequence", "time_proximity"]
        management.document_type = DocumentType.MANAGEMENT_DOCUMENT.value
        management_version.gross_total = Decimal("3.00")
        management_version.complete = True
        management_version.raw_metadata = {}
        _add_correlated_document(
            session,
            ids,
            document_type="COMMERCIAL_DOCUMENT",
            total="0.10",
            complete=True,
            minute=1,
            line_total="0.10",
        )

    transaction = SqlAlchemyApiRepository(factory).get_transaction(ids["transaction"])
    assert transaction is not None
    assert transaction.pre_bill_total is None
    assert transaction.fiscal_total == Decimal("0.10")
    assert transaction.difference is None
    assert transaction.diff["baseline_basis"] is None
    engine.dispose()


def test_transaction_prefers_room_close_over_incomplete_fiscal_and_uses_room_lines() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    with factory.begin() as session:
        _add_correlated_document(
            session,
            ids,
            document_type="COMMERCIAL_DOCUMENT",
            total="25.00",
            complete=False,
            minute=1,
            line_total="25.00",
        )
        _add_correlated_document(
            session,
            ids,
            document_type="MANAGEMENT_DOCUMENT",
            total="50.00",
            complete=True,
            minute=2,
            raw_metadata={"settlement_kind": "ROOM_CHARGE", "economic_close": True},
            line_total="50.00",
        )

    transaction = SqlAlchemyApiRepository(factory).get_transaction(ids["transaction"])
    assert transaction is not None
    assert transaction.fiscal_total == Decimal("50.0000")
    assert transaction.difference == Decimal("50.0000")
    assert transaction.diff["fiscal_total"] is None
    assert transaction.diff["settlement_basis"] == "ECONOMIC_MANAGEMENT_CLOSE"
    assert transaction.diff["lines"]["removed"] == []
    assert len(transaction.diff["lines"]["price_changed"]) == 1
    engine.dispose()


def test_transaction_order_is_selected_from_member_event_not_reused_global_code() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    linked_order_id, newer_order_id = uuid4(), uuid4()
    with factory.begin() as session:
        session.add_all(
            [
                Order(
                    id=linked_order_id,
                    source_device_id=ids["device"],
                    business_date=NOW.date(),
                    order_code="ORD-80",
                    status="OPEN",
                    opened_at=NOW,
                ),
                Order(
                    id=newer_order_id,
                    source_device_id=ids["device"],
                    business_date=(NOW + timedelta(days=1)).date(),
                    order_code="ORD-80",
                    status="OPEN",
                    opened_at=NOW + timedelta(days=1),
                ),
            ]
        )
        session.flush()
        session.add(
            OrderEvent(
                order_id=linked_order_id,
                source_document_id=ids["document"],
                sequence=1,
                event_type="PRE_BILL_PRINTED",
                occurred_at=NOW,
                details={},
                previous_record_hash="0" * 64,
                record_hash="3" * 64,
            )
        )

    transaction = SqlAlchemyApiRepository(factory).get_transaction(ids["transaction"])
    assert transaction is not None and transaction.order_id == linked_order_id
    engine.dispose()


def test_partial_version_semantics_do_not_mix_with_legacy_projection() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    with factory.begin() as session:
        version = session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == ids["document"]
            )
        )
        assert version is not None
        # A row with any immutable semantic value is not a pre-migration row.
        # Its remaining NULL values must stay NULL instead of being filled from
        # the mutable Document projection.
        version.external_document_code = "VERSION-ONLY"

    repository = SqlAlchemyApiRepository(factory)
    documents, document_count = repository.list_documents(
        limit=20,
        offset=0,
        filters={"type": "PRE_BILL"},
    )
    assert document_count == 0
    assert documents == []
    leaked, leaked_count = repository.search(
        query="PB-0001",
        limit=20,
        offset=0,
    )
    assert leaked_count == 0
    assert leaked == []
    assert repository.dashboard().pre_bills == 0
    engine.dispose()


def test_document_views_honor_active_parser_and_exclude_technical_responses() -> None:
    engine, factory = _factory()
    ids = _seed_api(factory)
    newer_parser_id = uuid4()
    response_document_id = uuid4()
    with factory.begin() as session:
        document = session.get(Document, ids["document"])
        assert document is not None
        active_version = session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == ids["document"],
                DocumentVersion.version_sequence == 1,
            )
        )
        assert active_version is not None
        active_version.document_type = "PRE_BILL"
        active_version.subtype = "PRECONTO"
        active_version.external_document_code = "PB-0001"
        active_version.external_document_code_suffix = "0001"
        active_version.commercial_reference_code = "COMM-0007"
        active_version.order_code = None
        active_version.table_code = "LAB-25"
        active_version.raw_metadata = {
            "document_timestamp_precision": "MINUTE",
            "document_timestamp_evidence": "ESC_POS_PRINTED_OPERATOR_LINE",
        }
        document.document_type = "KITCHEN_ORDER"
        document.subtype = "SHADOW_NEW"
        document.commercial_reference_code = "SHADOW-REF"
        document.order_code = "SHADOW-NEW"
        newer_parser = ParserVersion(
            id=newer_parser_id,
            name="synthetic",
            version="2",
            build_sha256="7" * 64,
            protocol="test",
        )
        session.add(newer_parser)
        session.add(
            ActiveParserVersion(
                parser_name="synthetic",
                parser_version_id=ids["parser"],
                activation_reason="keep validated version active",
            )
        )
        session.flush()
        session.add(
            DocumentVersion(
                document_id=ids["document"],
                parser_version_id=newer_parser_id,
                raw_payload_id=ids["raw"],
                version_sequence=2,
                document_type="KITCHEN_ORDER",
                subtype="SHADOW_NEW",
                order_code="SHADOW-NEW",
                gross_total=Decimal("9.00"),
                status="COMPLETE",
                normalized_text="SHADOW PARSER OUTPUT",
                parse_confidence=90,
                evidence_level="INFERRED",
                source_manifest_sha256="a" * 64,
                source_payload_sha256="7" * 64,
                source_path="/spool/job-one/client.raw",
                complete=True,
                chain_scope=f"document:{ids['job']}:shadow",
                chain_sequence=1,
                previous_record_hash="0" * 64,
                record_hash="7" * 64,
            )
        )
        session.add(
            Document(
                id=response_document_id,
                device_id=ids["device"],
                session_id=ids["session"],
                job_id=ids["job"],
                source_document_key="response-one",
                document_type="DEVICE_RESPONSE",
                subtype="ACK",
                captured_at=NOW + timedelta(seconds=1),
            )
        )
        session.flush()
        session.add(
            DocumentVersion(
                document_id=response_document_id,
                parser_version_id=ids["parser"],
                raw_payload_id=ids["raw"],
                version_sequence=1,
                document_type="DEVICE_RESPONSE",
                subtype="ACK",
                status="COMPLETE",
                normalized_text="ACK",
                parse_confidence=100,
                evidence_level="CONFIRMED",
                source_manifest_sha256="a" * 64,
                source_payload_sha256="8" * 64,
                source_path="/spool/job-one/device.raw",
                complete=True,
                chain_scope=f"document:{ids['job']}:response",
                chain_sequence=1,
                previous_record_hash="0" * 64,
                record_hash="8" * 64,
            )
        )

    repository = SqlAlchemyApiRepository(factory)
    selected = repository.get_document(ids["document"])
    assert selected is not None
    assert (selected.type, selected.subtype, selected.order_code) == (
        "PRE_BILL",
        "PRECONTO",
        None,
    )
    assert selected.normalized_text == "PRECONTO ORD-80 TOTALE 100,00"
    assert selected.receipt_text == "PRECONTO ORD-80 TOTALE 100,00"
    assert selected.external_document_code == "PB-0001"
    assert selected.external_document_code_suffix == "0001"
    assert selected.external_code == "PB-0001"
    assert selected.commercial_reference_code == "COMM-0007"
    assert selected.document_timestamp_precision == "MINUTE"
    assert selected.document_timestamp_evidence == "ESC_POS_PRINTED_OPERATOR_LINE"
    assert selected.progressive_observation_status == "FULL_CODE_OBSERVED_IN_CAPTURE"
    by_reference, reference_total = repository.list_documents(
        limit=20,
        offset=0,
        filters={"commercial_reference_code": "COMM-0007"},
    )
    assert reference_total == 1
    assert [item.id for item in by_reference] == [ids["document"]]
    search_hits, search_total = repository.search(
        query="COMM-0007",
        limit=20,
        offset=0,
    )
    assert search_total == 1
    assert [item.entity_id for item in search_hits] == [ids["document"]]
    leaked, leaked_total = repository.list_documents(
        limit=20,
        offset=0,
        filters={"order_code": "SHADOW-NEW"},
    )
    assert leaked_total == 0
    assert leaked == []
    search_leak, search_leak_total = repository.search(
        query="SHADOW-NEW",
        limit=20,
        offset=0,
    )
    assert search_leak_total == 0
    assert search_leak == []
    transaction_leak, transaction_leak_total = repository.list_transactions(
        limit=20,
        offset=0,
        filters={"order_code": "SHADOW-NEW"},
    )
    assert transaction_leak_total == 0
    assert transaction_leak == []

    primary, primary_total = repository.list_documents(
        limit=20,
        offset=0,
        filters={"exclude_type": "DEVICE_RESPONSE"},
    )
    assert primary_total == 1
    assert [item.id for item in primary] == [ids["document"]]
    technical, technical_total = repository.list_documents(
        limit=20,
        offset=0,
        filters={"type": "DEVICE_RESPONSE"},
    )
    assert technical_total == 1
    assert [item.id for item in technical] == [response_document_id]
    dashboard = repository.dashboard()
    assert (dashboard.documents, dashboard.orders, dashboard.pre_bills) == (1, 0, 1)

    with factory.begin() as session:
        job = session.get(PrintJob, ids["job"])
        assert job is not None
        job.import_status = "PARSE_FAILED"
    assert repository.diagnostics().parser_errors == 1
    engine.dispose()


def _envelope(*, source_key: str, manifest_sha256: str) -> NormalizedEnvelope:
    payload = b"synthetic raw"
    artifact = ArtifactSnapshot(
        role=ArtifactRole.REQUEST_RAW,
        original_path=Path("/spool/test/client.raw"),
        sha256=_sha(payload),
        size=len(payload),
        content=payload,
    )
    chunk = StreamChunk(
        sequence=1,
        direction=StreamDirection.CLIENT_TO_DEVICE,
        received_at=NOW,
        received_unix_ns=int(NOW.timestamp() * 1_000_000_000),
        forwarded_unix_ns=int((NOW + timedelta(milliseconds=1)).timestamp() * 1_000_000_000),
        local_write_drain_unix_ns=None,
        monotonic_ns=1,
        job_offset=0,
        session_offset=0,
        byte_count=len(payload),
        sha256=_sha(payload),
        local_write_drain_completed=True,
        forward_status="FORWARDED",
        error=None,
        observed_sequence=0,
        direction_sequence=0,
    )
    document = NormalizedDocument(
        external_id="candidate-1",
        document_type=DocumentType.COMMERCIAL_DOCUMENT,
        subtype="COMMERCIALE",
        complete=True,
        evidence="CONFIRMED",
        capture_time=NOW,
        timezone="Europe/Rome",
        source_start_offset=0,
        source_end_offset=len(payload),
        source_frame_ids=(1,),
        semantic={
            "external_document_code": "0400-0001",
            "order_code": "ORD-80",
            "gross_total": "50.00",
            "normalized_text": "DOCUMENTO COMMERCIALE 50,00",
            "lines": [
                {
                    "sequence": 1,
                    "description": "Espresso",
                    "quantity": "1",
                    "unit_price": "50.00",
                    "line_total": "50.00",
                }
            ],
            "payments": [{"method": "CONTANTI", "amount": "50.00"}],
        },
    )
    return NormalizedEnvelope(
        source_key=source_key,
        source_kind=SourceKind.COMMERCIAL_RCH_PARSED_V1,
        source_instance_id="rch-primary",
        device_id="rch_1",
        source_job_id="job-1",
        source_session_id="session-1",
        connection_id="connection-1",
        opened_at=NOW,
        closed_at=NOW + timedelta(seconds=1),
        source_endpoint=Endpoint("192.0.2.30", 50000),
        proxy_endpoint=Endpoint("192.0.2.10", 23),
        device_endpoint=Endpoint("192.0.2.20", 23),
        status="COMPLETE",
        complete=True,
        boundary_source="protocol",
        boundary_confidence=1.0,
        delivery_evidence="LOCAL_DRAIN",
        manifest_sha256=manifest_sha256,
        parser_version="1.0.0",
        artifacts=(artifact,),
        chunks=(chunk,),
        documents=(document,),
        metadata={},
    )


def test_sqlalchemy_ingestion_is_atomic_idempotent_and_keeps_decimal_raw_timeline() -> None:
    engine, factory = _factory()
    with factory.begin() as session:
        session.add(
            Device(
                external_id="rch_1",
                name="RCH",
                device_type="rch",
                parser_kind="rch_observed",
                listen_ip="192.0.2.10",
                listen_port=23,
                target_ip="192.0.2.20",
                target_port=23,
            )
        )
    repository = SqlAlchemyIngestionRepository(factory)
    envelope = _envelope(source_key="rch:test:job-1", manifest_sha256="a" * 64)
    first = repository.store_import(envelope)
    second = repository.store_import(envelope)
    assert first.disposition is ImportDisposition.IMPORTED
    assert second.disposition is ImportDisposition.DUPLICATE

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(PrintJob)) == 1
        assert session.scalar(select(func.count()).select_from(RawPayload)) == 1
        assert session.scalar(select(func.count()).select_from(StoredChunk)) == 1
        assert session.scalar(select(func.count()).select_from(ImportBatch)) == 2
        assert session.scalar(select(func.count()).select_from(ImportItem)) == 1
        batch_statuses = session.scalars(
            select(ImportBatch.report).order_by(ImportBatch.started_at, ImportBatch.id)
        ).all()
        assert {report["status"] for report in batch_statuses} == {"IMPORTED", "DUPLICATE"}
        device_status = session.scalar(
            select(DeviceStatus).order_by(DeviceStatus.observed_at.desc())
        )
        assert device_status is not None and device_status.status == "ONLINE"
        assert device_status.spool_bytes == len(b"synthetic raw")
        assert device_status.metrics["chunks"] == 1
        version = session.scalar(select(DocumentVersion))
        assert version is not None and version.gross_total == Decimal("50.0000")
        assert session.scalar(select(func.count()).select_from(DocumentLine)) == 1
        chunk = session.scalar(select(StoredChunk))
        assert chunk is not None and chunk.forwarded is True
        assert chunk.observed_sequence == 0 and chunk.direction_sequence == 0
        assert chunk.payload == b"synthetic raw" and chunk.raw_payload_id is not None
        assert chunk.previous_record_hash == "0" * 64

    conflicting = _envelope(source_key="rch:test:job-1", manifest_sha256="b" * 64)
    with pytest.raises(ValueError, match="different manifest"):
        repository.store_import(conflicting)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(PrintJob)) == 1
    engine.dispose()


def test_device_status_refresh_reports_spool_bytes_partial_and_pending_jobs(
    tmp_path: Path,
) -> None:
    engine, factory = _factory()
    with factory.begin() as session:
        session.add(
            Device(
                external_id="rch_1",
                name="RCH",
                device_type="rch",
                parser_kind="rch_observed",
                listen_ip="192.0.2.10",
                listen_port=23,
                target_ip="192.0.2.20",
                target_port=23,
            )
        )
    spool = tmp_path / "spool"
    imported = spool / "rch_1" / "2042-08-12" / "imported"
    pending = spool / "rch_1" / "2042-08-12" / "pending"
    partial = spool / "rch_1" / "2042-08-12" / "crashed.partial"
    for directory in (imported, pending, partial):
        directory.mkdir(parents=True)
    (imported / ".ready").write_text(json.dumps({"job_id": "job-1"}), encoding="utf-8")
    (pending / ".ready").write_text(json.dumps({"job_id": "job-not-imported"}), encoding="utf-8")
    (imported / "client.raw").write_bytes(b"one")
    (pending / "client.raw").write_bytes(b"two")
    (partial / "client.raw").write_bytes(b"partial")
    repository = SqlAlchemyIngestionRepository(factory, spool_root=spool)
    repository.store_import(_envelope(source_key="rch:test:job-1", manifest_sha256="a" * 64))

    repository.refresh_device_statuses()

    with factory() as session:
        status = session.scalar(select(DeviceStatus).order_by(DeviceStatus.observed_at.desc()))
        assert status is not None
        assert status.pending_imports == 1
        assert status.metrics["ready_jobs"] == 2
        assert status.metrics["partial_jobs"] == 1
        assert status.spool_bytes == sum(
            path.stat().st_size for path in spool.rglob("*") if path.is_file()
        )
    engine.dispose()


def test_api_spool_health_uses_fresh_persisted_device_metrics() -> None:
    engine, factory = _factory()
    now = datetime.now(UTC)
    with factory.begin() as session:
        device = Device(
            external_id="pos_health",
            name="POS health",
            device_type="pos",
            parser_kind="escpos",
            listen_ip="192.0.2.30",
            listen_port=9100,
            target_ip="192.0.2.40",
            target_port=9100,
        )
        session.add(device)
        session.flush()
        device_id = device.id

    repository = SqlAlchemyApiRepository(
        factory,
        spool_metric_stale_after=timedelta(minutes=5),
        spool_warning_bytes=1_000,
    )
    assert repository.spool_health() == "unknown"

    with factory.begin() as session:
        session.add(
            DeviceStatus(
                device_id=device_id,
                observed_at=now,
                status="ONLINE",
                spool_bytes=120,
                metrics={"spool_scanned_at": now.isoformat(), "partial_jobs": 0},
            )
        )
    assert repository.spool_health() == "ok"
    assert repository.diagnostics().spool == "ok"

    with factory.begin() as session:
        session.add(
            DeviceStatus(
                device_id=device_id,
                observed_at=now + timedelta(seconds=1),
                status="ONLINE",
                spool_bytes=120,
                metrics={
                    "spool_scanned_at": (now - timedelta(minutes=6)).isoformat(),
                    "partial_jobs": 0,
                },
            )
        )
    assert repository.spool_health() == "unknown"

    with factory.begin() as session:
        session.add(
            DeviceStatus(
                device_id=device_id,
                observed_at=now + timedelta(seconds=2),
                status="ONLINE",
                spool_bytes=120,
                metrics={
                    "spool_scanned_at": now.isoformat(),
                    "spool_metric_error": "PermissionError",
                    "partial_jobs": 0,
                },
            )
        )
    assert repository.spool_health() == "degraded"

    with factory.begin() as session:
        session.add(
            DeviceStatus(
                device_id=device_id,
                observed_at=now + timedelta(seconds=3),
                status="ONLINE",
                spool_bytes=1_000,
                metrics={"spool_scanned_at": now.isoformat(), "partial_jobs": 0},
            )
        )
    assert repository.spool_health() == "degraded"

    with factory.begin() as session:
        session.add(
            DeviceStatus(
                device_id=device_id,
                observed_at=now + timedelta(seconds=4),
                status="ONLINE",
                spool_bytes=120,
                metrics={"spool_scanned_at": now.isoformat(), "partial_jobs": 1},
            )
        )
    assert repository.spool_health() == "degraded"
    engine.dispose()


def test_ingestion_scan_batch_aggregates_import_and_duplicate_report(tmp_path: Path) -> None:
    engine, factory = _factory()
    with factory.begin() as session:
        session.add(
            Device(
                external_id="rch_1",
                name="RCH",
                device_type="rch",
                parser_kind="rch_observed",
                listen_ip="192.0.2.10",
                listen_port=23,
                target_ip="192.0.2.20",
                target_port=23,
            )
        )
    repository = SqlAlchemyIngestionRepository(factory, spool_root=tmp_path)
    batch_id = repository.begin_import_batch(
        source_system="RCHCaptureV1Adapter",
        source_instance="rch-primary",
        source_root=tmp_path,
    )
    envelope = _envelope(source_key="rch:batch:job-1", manifest_sha256="c" * 64)
    assert repository.store_import(envelope).disposition is ImportDisposition.IMPORTED
    assert repository.store_import(envelope).disposition is ImportDisposition.DUPLICATE
    repository.complete_import_batch(
        batch_id,
        {
            "discovered": 2,
            "imported": 1,
            "duplicates": 1,
            "quarantined": 0,
            "retry_exhausted": 0,
            "source_busy": 0,
            "errors": [],
        },
    )

    with factory() as session:
        batch = session.scalar(select(ImportBatch))
        assert batch is not None
        assert batch.status == "COMPLETED"
        assert (batch.scanned_count, batch.imported_count, batch.skipped_count) == (2, 1, 1)
        assert batch.report["source_kinds"] == ["commercialrchproxy.pharsed.v1"]
        assert session.scalar(select(func.count()).select_from(ImportItem)) == 1
    engine.dispose()


def test_duplicate_only_scans_do_not_grow_import_batch_history(tmp_path: Path) -> None:
    engine, factory = _factory()
    with factory.begin() as session:
        session.add(
            Device(
                external_id="rch_1",
                name="RCH",
                device_type="rch",
                parser_kind="rch_observed",
                listen_ip="192.0.2.10",
                listen_port=23,
                target_ip="192.0.2.20",
                target_port=23,
            )
        )
    repository = SqlAlchemyIngestionRepository(factory, spool_root=tmp_path)
    envelope = _envelope(source_key="rch:batch:stable", manifest_sha256="c" * 64)

    imported_batch = repository.begin_import_batch(
        source_system="RCHCaptureV1Adapter",
        source_instance="rch-primary",
        source_root=tmp_path,
    )
    assert repository.store_import(envelope).disposition is ImportDisposition.IMPORTED
    repository.complete_import_batch(
        imported_batch,
        {
            "discovered": 1,
            "imported": 1,
            "duplicates": 0,
            "quarantined": 0,
            "retry_exhausted": 0,
            "errors": [],
        },
    )

    for _ in range(3):
        duplicate_batch = repository.begin_import_batch(
            source_system="RCHCaptureV1Adapter",
            source_instance="rch-primary",
            source_root=tmp_path,
        )
        assert repository.store_import(envelope).disposition is ImportDisposition.DUPLICATE
        repository.complete_import_batch(
            duplicate_batch,
            {
                "discovered": 1,
                "imported": 0,
                "duplicates": 1,
                "quarantined": 0,
                "retry_exhausted": 0,
                "errors": [],
            },
        )

    with factory() as session:
        batches = session.scalars(select(ImportBatch)).all()
        assert len(batches) == 1
        assert batches[0].imported_count == 1
        assert session.scalar(select(func.count()).select_from(ImportItem)) == 1
    engine.dispose()
