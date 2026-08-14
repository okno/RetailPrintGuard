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
from retailprintguard.api.schemas import AlertUpdate, AuditEntry, RoleName
from retailprintguard.common.domain import DocumentType
from retailprintguard.db import Base, create_db_engine, session_factory
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
    ImportBatch,
    ImportItem,
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
                table_code="25-B",
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
