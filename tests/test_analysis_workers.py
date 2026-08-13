from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select

from retailprintguard.correlation.worker import (
    CorrelationWorker,
    activate_parser_version,
    load_latest_documents,
)
from retailprintguard.db import Base, create_db_engine, session_factory
from retailprintguard.db.models import (
    AnalysisWatermark,
    AuditLog,
    Device,
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
    Order,
    OrderEvent,
    OrderSnapshot,
    ParserVersion,
    Payment,
    PrintJob,
    ProxySession,
    RawPayload,
    SystemEvent,
)
from retailprintguard.db.repository import SqlAlchemyApiRepository
from retailprintguard.fraud.worker import FraudWorker

NOW = datetime(2042, 8, 12, 13, 52, tzinfo=UTC)


def _sha(value: str | bytes) -> str:
    encoded = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def _database() -> tuple[object, object]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine, session_factory(engine)


def _device(session: object, external_id: str, kind: str, port: int) -> UUID:
    identifier = uuid4()
    session.add(  # type: ignore[attr-defined]
        Device(
            id=identifier,
            external_id=external_id,
            name=external_id,
            device_type=kind,
            parser_kind="escpos" if kind == "pos" else "rch_observed",
            listen_ip=f"192.0.2.{10 + port % 10}",
            listen_port=port,
            target_ip=f"192.0.2.{20 + port % 10}",
            target_port=port,
        )
    )
    return identifier


def _document(
    session: object,
    *,
    device_id: UUID,
    parser_id: UUID,
    source: str,
    document_type: str,
    total: str,
    when: datetime,
    lines: tuple[tuple[str, str], ...],
    payment: str | None = None,
) -> UUID:
    session_id, job_id, document_id = uuid4(), uuid4(), uuid4()
    session.add(  # type: ignore[attr-defined]
        ProxySession(
            id=session_id,
            device_id=device_id,
            source_system="test",
            source_instance="workers",
            source_scope=source,
            source_session_id=f"session-{source}",
            listen_ip="192.0.2.10",
            listen_port=9100,
            target_ip="192.0.2.20",
            target_port=9100,
            started_at=when,
            ended_at=when,
            status="CLOSED",
        )
    )
    session.flush()  # type: ignore[attr-defined]
    manifest_hash = _sha(f"manifest-{source}")
    session.add(  # type: ignore[attr-defined]
        PrintJob(
            id=job_id,
            device_id=device_id,
            session_id=session_id,
            source_key=f"test:workers:{source}",
            source_system="test",
            source_instance="workers",
            source_scope=source,
            source_job_id=f"job-{source}",
            source_schema="test.v1",
            manifest_sha256=manifest_hash,
            manifest_path=f"/spool/{source}/manifest.json",
            started_at=when,
            ended_at=when,
            captured_at=when,
            status="READY",
            capture_complete=True,
            timeline_complete=True,
        )
    )
    session.flush()  # type: ignore[attr-defined]
    raw_bytes = f"raw-{source}".encode()
    raw = RawPayload(
        job_id=job_id,
        device_id=device_id,
        session_id=session_id,
        artifact_role="REQUEST_RAW",
        direction="CLIENT_TO_DEVICE",
        source_path=f"/spool/{source}/client.raw",
        source_path_sha256=_sha(f"/spool/{source}/client.raw"),
        byte_count=len(raw_bytes),
        sha256=_sha(raw_bytes),
        payload=raw_bytes,
        complete=True,
        chain_scope=f"raw:{job_id}",
        chain_sequence=1,
        previous_record_hash="0" * 64,
        record_hash=_sha(f"raw-record-{source}"),
    )
    session.add(raw)  # type: ignore[attr-defined]
    session.add(  # type: ignore[attr-defined]
        Document(
            id=document_id,
            device_id=device_id,
            session_id=session_id,
            job_id=job_id,
            source_document_key=f"document-{source}",
            document_type=document_type,
            subtype=document_type,
            external_document_code=f"DOC-{source}",
            order_code="ORDER-80",
            table_code="25-B",
            operator_code="OP-1",
            document_timestamp=when,
            captured_at=when,
        )
    )
    session.flush()  # type: ignore[attr-defined]
    version = DocumentVersion(
        document_id=document_id,
        parser_version_id=parser_id,
        raw_payload_id=raw.id,
        version_sequence=1,
        gross_total=Decimal(total),
        status="COMPLETE",
        normalized_text=f"{document_type} {total}",
        parse_confidence=100,
        evidence_level="CONFIRMED",
        source_manifest_sha256=manifest_hash,
        source_payload_sha256=raw.sha256,
        source_path=raw.source_path,
        complete=True,
        chain_scope=f"document:{job_id}",
        chain_sequence=1,
        previous_record_hash="0" * 64,
        record_hash=_sha(f"document-record-{source}"),
    )
    session.add(version)  # type: ignore[attr-defined]
    session.flush()  # type: ignore[attr-defined]
    for sequence, (description, amount) in enumerate(lines, start=1):
        session.add(  # type: ignore[attr-defined]
            DocumentLine(
                document_version_id=version.id,
                sequence=sequence,
                description=description,
                quantity=Decimal("1"),
                unit_price=Decimal(amount),
                line_total=Decimal(amount),
            )
        )
    if payment is not None:
        session.add(  # type: ignore[attr-defined]
            Payment(
                document_version_id=version.id,
                method="CASH",
                amount=Decimal(payment),
                currency="EUR",
                status="RECORDED",
                evidence_level="CONFIRMED",
                paid_at=when,
            )
        )
    return document_id


def _seed_scenario(factory: object, *, split: bool) -> None:
    with factory.begin() as session:  # type: ignore[attr-defined]
        pos_id = _device(session, "pos_1", "pos", 9100)
        rch_id = _device(session, "rch_1", "rch", 23)
        parser = ParserVersion(
            name="test",
            version="1.0.0",
            build_sha256=_sha("parser"),
            protocol="test",
        )
        session.add(parser)
        session.flush()
        if split:
            _document(
                session,
                device_id=pos_id,
                parser_id=parser.id,
                source="prebill",
                document_type="PRE_BILL",
                total="100.00",
                when=NOW,
                lines=(("Primo", "50.00"), ("Secondo", "50.00")),
            )
            _document(
                session,
                device_id=rch_id,
                parser_id=parser.id,
                source="fiscal-1",
                document_type="COMMERCIAL_DOCUMENT",
                total="50.00",
                when=NOW + timedelta(minutes=1),
                lines=(("Primo", "50.00"),),
                payment="50.00",
            )
            _document(
                session,
                device_id=rch_id,
                parser_id=parser.id,
                source="fiscal-2",
                document_type="COMMERCIAL_DOCUMENT",
                total="50.00",
                when=NOW + timedelta(minutes=2),
                lines=(("Secondo", "50.00"),),
                payment="50.00",
            )
        else:
            _document(
                session,
                device_id=pos_id,
                parser_id=parser.id,
                source="prebill",
                document_type="PRE_BILL",
                total="100.00",
                when=NOW,
                lines=(("Coperto", "40.00"), ("Crudo", "40.00"), ("Espresso", "20.00")),
            )
            _document(
                session,
                device_id=rch_id,
                parser_id=parser.id,
                source="fiscal",
                document_type="COMMERCIAL_DOCUMENT",
                total="50.00",
                when=NOW + timedelta(minutes=1),
                lines=(("Coperto", "40.00"), ("Crudo", "10.00")),
                payment="50.00",
            )


def test_database_workers_persist_scenario_a_idempotently() -> None:
    engine, factory = _database()
    _seed_scenario(factory, split=False)
    correlation = CorrelationWorker(factory)
    first = correlation.run_once()
    second = correlation.run_once()
    assert first.correlations_inserted == 1
    assert first.orders_created == 1 and first.events_inserted >= 6
    assert second.correlations_inserted == 0
    assert second.orders_created == 0 and second.events_inserted == 0

    fraud = FraudWorker(factory)
    fraud_first = fraud.run_once()
    fraud_second = fraud.run_once()
    assert fraud_first.alerts_inserted >= 4
    assert fraud_second.alerts_inserted == 0
    with factory() as session:
        codes = set(
            session.scalars(select(FraudAlert.description).where(FraudAlert.status == "OPEN"))
        )
        assert any("Riduzione significativa" in description for description in codes)
        assert any("Articoli rimossi" in description for description in codes)
        assert any("Prezzo ridotto" in description for description in codes)
        alert_count = session.scalar(select(func.count()).select_from(FraudAlert))
        assert session.scalar(select(func.count()).select_from(FraudAlertHistory)) == alert_count
        assert session.scalar(select(func.count()).select_from(FraudAlertEvidence)) >= alert_count
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert session.scalar(select(func.count()).select_from(OrderEvent)) == session.scalar(
            select(func.count()).select_from(OrderSnapshot)
        )
        correlation_id = session.scalar(select(DocumentCorrelation.transaction_id))
        assert correlation_id is not None
    transaction = SqlAlchemyApiRepository(factory).get_transaction(correlation_id)
    assert transaction is not None
    kinds = {entry["event_kind"] for entry in transaction.timeline}
    assert {"DOCUMENT", "ORDER_EVENT", "FRAUD_ALERT"} <= kinds
    assert transaction.diff["lines"]["removed"]
    assert transaction.diff["lines"]["price_changed"]
    engine.dispose()


def test_database_workers_aggregate_legitimate_split_without_amount_drop() -> None:
    engine, factory = _database()
    _seed_scenario(factory, split=True)
    correlation = CorrelationWorker(factory)
    report = correlation.run_once()
    assert report.correlations_inserted == 1
    with factory() as session:
        stored = session.scalar(select(DocumentCorrelation))
        assert stored is not None and stored.score >= 60

    fraud = FraudWorker(factory)
    fraud.run_once()
    with factory() as session:
        descriptions = set(session.scalars(select(FraudAlert.description)))
        assert not any("Riduzione significativa" in item for item in descriptions)
        assert not any("Stesso riferimento con importi differenti" in item for item in descriptions)
    engine.dispose()


def test_late_split_payment_supersedes_and_justifies_stale_amount_drop() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        pos_id = _device(session, "pos_1", "pos", 9100)
        rch_id = _device(session, "rch_1", "rch", 23)
        parser = ParserVersion(
            name="test",
            version="1.0.0",
            build_sha256=_sha("parser"),
            protocol="test",
        )
        session.add(parser)
        session.flush()
        _document(
            session,
            device_id=pos_id,
            parser_id=parser.id,
            source="prebill",
            document_type="PRE_BILL",
            total="100.00",
            when=NOW,
            lines=(("Primo", "50.00"), ("Secondo", "50.00")),
        )
        _document(
            session,
            device_id=rch_id,
            parser_id=parser.id,
            source="fiscal-first",
            document_type="COMMERCIAL_DOCUMENT",
            total="50.00",
            when=NOW + timedelta(minutes=1),
            lines=(("Primo", "50.00"),),
            payment="50.00",
        )
    correlation = CorrelationWorker(factory)
    correlation.run_once()
    fraud = FraudWorker(factory)
    first = fraud.run_once()
    assert first.alerts_inserted >= 1
    with factory() as session:
        stale = session.scalar(
            select(FraudAlert).where(FraudAlert.description.contains("Riduzione significativa"))
        )
        assert stale is not None and stale.status == "OPEN"
        stale_id = stale.id

    with factory.begin() as session:
        parser = session.scalar(select(ParserVersion))
        rch = session.scalar(select(Device).where(Device.external_id == "rch_1"))
        assert parser is not None and rch is not None
        _document(
            session,
            device_id=rch.id,
            parser_id=parser.id,
            source="fiscal-late-second",
            document_type="COMMERCIAL_DOCUMENT",
            total="50.00",
            when=NOW + timedelta(minutes=2),
            lines=(("Secondo", "50.00"),),
            payment="50.00",
        )
    correlation.run_once()
    second = fraud.run_once()
    assert second.alerts_superseded >= 1
    with factory() as session:
        stale = session.get(FraudAlert, stale_id)
        assert stale is not None and stale.status == "JUSTIFIED"
        assert stale.closed_at is not None
        history = session.scalars(
            select(FraudAlertHistory)
            .where(FraudAlertHistory.fraud_alert_id == stale_id)
            .order_by(FraudAlertHistory.sequence)
        ).all()
        assert history[-1].event_type == "ALERT_AUTO_SUPERSEDED"
        assert history[-1].previous_record_hash == history[-2].record_hash
        assert (
            session.scalar(
                select(FraudAlertEvidence).where(
                    FraudAlertEvidence.fraud_alert_id == stale_id,
                    FraudAlertEvidence.evidence_type == "CORRELATION_SUPERSEDED",
                )
            )
            is not None
        )
        assert (
            session.scalar(
                select(FraudAlert).where(
                    FraudAlert.status == "OPEN",
                    FraudAlert.description.contains("Riduzione significativa"),
                )
            )
            is None
        )
    transactions, total = SqlAlchemyApiRepository(factory).list_transactions(
        limit=10, offset=0, filters={}
    )
    assert total == 1
    assert transactions[0].document_count == 3
    engine.dispose()


def test_yaml_threshold_changes_append_rule_versions_and_allow_a_b_a() -> None:
    engine, factory = _database()
    FraudWorker(factory, default_amount_drop_percent=20).run_once()
    FraudWorker(factory, default_amount_drop_percent=60).run_once()
    FraudWorker(factory, default_amount_drop_percent=20).run_once()
    with factory() as session:
        rule = session.scalar(
            select(FraudRule).where(FraudRule.code == "PREBILL_FISCAL_AMOUNT_DROP")
        )
        assert rule is not None
        versions = session.scalars(
            select(FraudRuleVersion)
            .where(FraudRuleVersion.fraud_rule_id == rule.id)
            .order_by(FraudRuleVersion.version)
        ).all()
        assert [row.version for row in versions] == [1, 2, 3]
        assert [row.configuration["minimum_percent"] for row in versions] == ["20", "60", "20"]
        assert versions[0].configuration_fingerprint == versions[2].configuration_fingerprint
        assert versions[0].effective_until is not None
        assert versions[1].effective_until is not None
        assert versions[2].effective_until is None
    engine.dispose()


def test_correlation_watermark_reprocesses_late_evidence_and_blocks_unrelated_rows() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        pos_id = _device(session, "pos_1", "pos", 9100)
        rch_id = _device(session, "rch_1", "rch", 23)
        parser = ParserVersion(
            name="test",
            version="1.0.0",
            build_sha256=_sha("parser"),
            protocol="test",
        )
        session.add(parser)
        session.flush()
        _document(
            session,
            device_id=pos_id,
            parser_id=parser.id,
            source="prebill",
            document_type="PRE_BILL",
            total="100.00",
            when=NOW,
            lines=(("Primo", "100.00"),),
        )
        # Same time window but no shared indexed business key.
        unrelated = _document(
            session,
            device_id=rch_id,
            parser_id=parser.id,
            source="unrelated",
            document_type="COMMERCIAL_DOCUMENT",
            total="12.00",
            when=NOW + timedelta(minutes=1),
            lines=(("Altro", "12.00"),),
        )
        stored = session.get(Document, unrelated)
        assert stored is not None
        stored.order_code = "ORDER-UNRELATED"
        stored.table_code = "TABLE-UNRELATED"

    worker = CorrelationWorker(factory)
    first = worker.run_once(max_documents=1)
    assert first.documents_loaded == 1
    with factory() as session:
        watermark = session.get(AnalysisWatermark, "correlation")
        assert watermark is not None and watermark.processed_count == 1

    # A later ingestion has a new parsed_at but an earlier business timestamp:
    # it must still enter through the watermark lookback and join the old prebill.
    with factory.begin() as session:
        parser = session.scalar(select(ParserVersion))
        rch = session.scalar(select(Device).where(Device.external_id == "rch_1"))
        assert parser is not None and rch is not None
        late_id = _document(
            session,
            device_id=rch.id,
            parser_id=parser.id,
            source="late-fiscal",
            document_type="COMMERCIAL_DOCUMENT",
            total="50.00",
            when=NOW + timedelta(seconds=30),
            lines=(("Primo", "50.00"),),
            payment="50.00",
        )
        late = session.get(Document, late_id)
        assert late is not None
        late.external_document_code = "DOC-LATE"

    second = worker.run_once(max_documents=10)
    assert second.correlations_inserted >= 1
    with factory() as session:
        active = session.scalars(
            select(DocumentCorrelation).where(DocumentCorrelation.status == "AUTOMATIC")
        ).all()
        member_sets = [
            set(
                session.scalars(
                    select(DocumentCorrelationMember.document_id).where(
                        DocumentCorrelationMember.correlation_id == correlation.id
                    )
                )
            )
            for correlation in active
        ]
        assert any(late_id in members and unrelated not in members for members in member_sets)
    engine.dispose()


def test_active_parser_pointer_honours_rollback_instead_of_latest_sequence() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        device_id = _device(session, "pos_1", "pos", 9100)
        old = ParserVersion(
            name="escpos",
            version="1.0.0",
            build_sha256=_sha("old-build"),
            protocol="escpos",
        )
        new = ParserVersion(
            name="escpos",
            version="2.0.0",
            build_sha256=_sha("new-build"),
            protocol="escpos",
        )
        session.add_all((old, new))
        session.flush()
        document_id = _document(
            session,
            device_id=device_id,
            parser_id=old.id,
            source="rollback",
            document_type="PRE_BILL",
            total="100.00",
            when=NOW,
            lines=(("Primo", "100.00"),),
        )
        first = session.scalar(
            select(DocumentVersion).where(DocumentVersion.document_id == document_id)
        )
        assert first is not None
        session.add(
            DocumentVersion(
                document_id=document_id,
                parser_version_id=new.id,
                raw_payload_id=first.raw_payload_id,
                version_sequence=2,
                gross_total=Decimal("1.00"),
                status="COMPLETE",
                normalized_text="new build result",
                parse_confidence=100,
                evidence_level="CONFIRMED",
                source_manifest_sha256=first.source_manifest_sha256,
                source_payload_sha256=first.source_payload_sha256,
                source_path=first.source_path,
                complete=True,
                chain_scope=first.chain_scope,
                chain_sequence=2,
                previous_record_hash=first.record_hash,
                record_hash=_sha("new-version-record"),
            )
        )
    with factory() as session:
        loaded = load_latest_documents(session, document_ids={document_id})
        assert len(loaded) == 1
        assert loaded[0].value.parser_version == "2.0.0"
        assert loaded[0].value.gross_total == Decimal("1.0000")
    with factory.begin() as session:
        session.add(
            AnalysisWatermark(
                service="correlation",
                cursor_timestamp=NOW,
                cursor_id=uuid4(),
                processed_count=1,
                metadata_json={"parser_activation_fingerprint": "old"},
            )
        )
    activated = activate_parser_version(
        factory,
        parser_name="escpos",
        parser_version="1.0.0",
        build_sha256=_sha("old-build"),
        reason="rollback after parser regression",
    )
    with factory() as session:
        assert activated == session.scalar(
            select(ParserVersion.id).where(ParserVersion.build_sha256 == _sha("old-build"))
        )
        assert session.get(AnalysisWatermark, "correlation") is None
        audit = session.scalar(select(AuditLog).where(AuditLog.event_type == "PARSER_ACTIVATED"))
        assert audit is not None
        assert audit.details["reason"] == "rollback after parser regression"
        assert audit.record_hash != "0" * 64
        assert (
            session.scalar(select(SystemEvent).where(SystemEvent.event_type == "PARSER_ACTIVATED"))
            is not None
        )
        loaded = load_latest_documents(session, document_ids={document_id})
        assert len(loaded) == 1
        assert loaded[0].value.parser_version == "1.0.0"
        assert loaded[0].value.gross_total == Decimal("100.0000")
    engine.dispose()


def test_parser_activation_without_rewind_preserves_and_rekeys_watermark() -> None:
    engine, factory = _database()
    cursor_id = uuid4()
    with factory.begin() as session:
        parser = ParserVersion(
            name="rch_rt_xml7",
            version="3.2.1",
            build_sha256=_sha("rch-parser-build"),
            protocol="rch_rt_xml7",
        )
        session.add(parser)
        session.add(
            AnalysisWatermark(
                service="correlation",
                cursor_timestamp=NOW,
                cursor_id=cursor_id,
                processed_count=41,
                metadata_json={"parser_activation_fingerprint": "previous"},
            )
        )

    activate_parser_version(
        factory,
        parser_name="rch_rt_xml7",
        parser_version="3.2.1",
        build_sha256=_sha("rch-parser-build"),
        reason="verified production rollout",
        rewind=False,
    )

    with factory() as session:
        watermark = session.get(AnalysisWatermark, "correlation")
        assert watermark is not None
        assert watermark.cursor_id == cursor_id
        assert watermark.processed_count == 41
        assert watermark.metadata_json["parser_activation_fingerprint"] != "previous"
        assert watermark.metadata_json["parser_activation_no_rewind"]["parser_name"] == (
            "rch_rt_xml7"
        )
    engine.dispose()
