from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from retailprintguard.api.schemas import JobReviewRequest, RoleName, UserPrincipal
from retailprintguard.common.hashchain import canonical_json
from retailprintguard.correlation.engine import ALGORITHM_VERSION
from retailprintguard.correlation.worker import (
    CorrelationWorker,
    _candidate_batch,
    activate_parser_version,
    load_latest_documents,
)
from retailprintguard.db import Base, create_db_engine, session_factory
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
    FraudAlert,
    FraudAlertEvidence,
    FraudAlertHistory,
    FraudRule,
    FraudRuleVersion,
    LinePriceAttribution,
    Order,
    OrderEvent,
    OrderSnapshot,
    ParserVersion,
    Payment,
    PrintJob,
    ProxySession,
    RawPayload,
    SystemEvent,
    User,
)
from retailprintguard.db.repository import SqlAlchemyApiRepository
from retailprintguard.fraud.worker import FraudWorker
from retailprintguard.pricing.service import PRICE_ATTRIBUTION_ALGORITHM

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
    order_code: str = "ORDER-80",
    table_code: str = "LAB-25",
    operator_code: str = "OP-1",
    complete: bool = True,
    external_document_code: str | None = None,
    external_document_code_suffix: str | None = None,
    commercial_reference_code: str | None = None,
    auto_external_document_code: bool = True,
    raw_metadata: dict[str, object] | None = None,
) -> UUID:
    session_id, job_id, document_id = uuid4(), uuid4(), uuid4()
    effective_external_code = (
        external_document_code
        if external_document_code is not None or not auto_external_document_code
        else f"DOC-{source}"
    )
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
            external_document_code=effective_external_code,
            external_document_code_suffix=external_document_code_suffix,
            commercial_reference_code=commercial_reference_code,
            order_code=order_code,
            table_code=table_code,
            operator_code=operator_code,
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
        document_type=document_type,
        subtype=document_type,
        external_document_code=effective_external_code,
        external_document_code_suffix=external_document_code_suffix,
        commercial_reference_code=commercial_reference_code,
        order_code=order_code,
        table_code=table_code,
        operator_code=operator_code,
        document_timestamp=when,
        gross_total=Decimal(total),
        status="COMPLETE" if complete else "PARTIAL",
        normalized_text=f"{document_type} {total}",
        parse_confidence=100,
        evidence_level="CONFIRMED",
        source_manifest_sha256=manifest_hash,
        source_payload_sha256=raw.sha256,
        source_path=raw.source_path,
        complete=complete,
        raw_metadata=raw_metadata or {},
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
    assert fraud_first.alerts_inserted == 1
    assert fraud_second.alerts_inserted == 0
    with factory() as session:
        codes = set(
            session.scalars(select(FraudAlert.description).where(FraudAlert.status == "OPEN"))
        )
        assert any("Riduzione del valore di vendita" in description for description in codes)
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


def test_excluded_job_reopens_only_after_current_finding_is_reproduced() -> None:
    engine, factory = _database()
    _seed_scenario(factory, split=False)
    correlation_worker = CorrelationWorker(factory)
    fraud_worker = FraudWorker(factory)
    correlation_worker.run_once()
    fraud_worker.run_once()

    actor_id = uuid4()
    with factory.begin() as session:
        current_correlation = session.scalar(
            select(DocumentCorrelation).where(
                DocumentCorrelation.status.in_(("AUTOMATIC", "UNCORRELATED"))
            )
        )
        assert current_correlation is not None
        current_correlation_id = current_correlation.id
        original_alert = session.scalar(select(FraudAlert).where(FraudAlert.status == "OPEN"))
        assert original_alert is not None
        original_alert_id = original_alert.id
        stale = DocumentCorrelation(
            transaction_id=uuid4(),
            algorithm_version=ALGORITHM_VERSION,
            input_fingerprint=_sha("stale-correlation-for-same-job"),
            score=current_correlation.score,
            status="SUPERSEDED",
            matched_criteria=current_correlation.matched_criteria,
            unmatched_criteria=current_correlation.unmatched_criteria,
            explanation="Correlazione storica gia superata.",
        )
        session.add(stale)
        session.flush()
        for member in session.scalars(
            select(DocumentCorrelationMember).where(
                DocumentCorrelationMember.correlation_id == current_correlation.id
            )
        ):
            session.add(
                DocumentCorrelationMember(
                    correlation_id=stale.id,
                    document_id=member.document_id,
                    role=member.role,
                    contribution_score=member.contribution_score,
                    criteria=member.criteria,
                )
            )
        stale_correlation_id = stale.id
        job = session.scalar(select(PrintJob).where(PrintJob.source_scope == "prebill"))
        assert job is not None
        job.capture_complete = False
        job.status = "PARTIAL"
        session.add(
            User(
                id=actor_id,
                username="review-admin",
                display_name="Review Admin",
                password_hash="not-used-by-this-test",
            )
        )
        job_id = job.id

    repository = SqlAlchemyApiRepository(factory)
    principal = UserPrincipal(
        id=actor_id,
        username="review-admin",
        roles=(RoleName.ADMIN,),
    )
    excluded = repository.review_job(
        job_id,
        JobReviewRequest(
            action="EXCLUDE_FROM_ANALYSIS",
            reason="Capture incompleta esclusa temporaneamente dalla sola analisi.",
            confirmation_password="not-persisted-password",
        ),
        principal,
        correlation_id="exclude-current-finding",
    )
    assert excluded is not None and excluded.analysis_excluded
    reopened = repository.review_job(
        job_id,
        JobReviewRequest(
            action="REOPEN_REVIEW",
            reason="Verifica tecnica conclusa; richiedo un nuovo calcolo deterministico.",
            confirmation_password="not-persisted-password",
        ),
        principal,
        correlation_id="reopen-current-finding",
    )
    assert reopened is not None and reopened.analysis_excluded

    # The API action alone never resurrects stale analysis.
    with factory() as session:
        assert set(session.scalars(select(DocumentCorrelation.status))) == {"SUPERSEDED"}
        assert set(session.scalars(select(FraudAlert.status))) == {"JUSTIFIED"}

    correlation_worker.run_once()
    fraud_worker.run_once()
    with factory() as session:
        # REOPEN_REVIEW means "review again": an incomplete PENDING job does
        # not influence analysis until the administrator explicitly verifies it.
        correlation = session.get(DocumentCorrelation, current_correlation_id)
        alert = session.get(FraudAlert, original_alert_id)
        assert correlation is not None and correlation.status == "SUPERSEDED"
        assert alert is not None and alert.status == "JUSTIFIED"

    verified = repository.review_job(
        job_id,
        JobReviewRequest(
            action="VERIFY_USABLE",
            reason="Il contenuto semantico e completo e puo essere usato nel ricalcolo.",
            confirmation_password="not-persisted-password",
        ),
        principal,
        correlation_id="verify-current-finding",
    )
    assert verified is not None and verified.review_state == "VERIFIED_USABLE"
    correlation_worker.run_once()
    fraud_worker.run_once()
    with factory() as session:
        current = session.scalars(
            select(DocumentCorrelation).where(
                DocumentCorrelation.status.in_(("AUTOMATIC", "UNCORRELATED"))
            )
        ).all()
        assert len(current) == 1
        assert current[0].id == current_correlation_id
        stale = session.get(DocumentCorrelation, stale_correlation_id)
        assert stale is not None and stale.status == "SUPERSEDED"
        alert = session.get(FraudAlert, original_alert_id)
        assert alert is not None and alert.status == "OPEN"
        history = session.scalars(
            select(FraudAlertHistory)
            .where(FraudAlertHistory.fraud_alert_id == alert.id)
            .order_by(FraudAlertHistory.sequence)
        ).all()
        assert history[-1].event_type == "ALERT_AUTO_REOPENED_AFTER_JOB_REVIEW"
        assert history[-1].previous_record_hash == history[-2].record_hash
    engine.dispose()


def test_worker_persists_pos_change_as_residual_quantity_not_removal() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        pos_id = _device(session, "pos_2", "pos", 9100)
        parser = ParserVersion(
            name="synthetic-pos",
            version="1.0.0",
            build_sha256=_sha("synthetic-pos-parser"),
            protocol="escpos",
        )
        session.add(parser)
        session.flush()
        initial_id = _document(
            session,
            device_id=pos_id,
            parser_id=parser.id,
            source="pos-quantity-two",
            document_type="KITCHEN_ORDER",
            total="16.00",
            when=NOW,
            lines=(("Pietanza mista", "8.00"),),
        )
        change_id = _document(
            session,
            device_id=pos_id,
            parser_id=parser.id,
            source="pos-minus-one",
            document_type="ORDER_CHANGE",
            total="-8.00",
            when=NOW + timedelta(seconds=20),
            lines=(("Pietanza mista", "-8.00"),),
        )
        session.flush()
        initial_version = session.scalar(
            select(DocumentVersion.id).where(DocumentVersion.document_id == initial_id)
        )
        change_version = session.scalar(
            select(DocumentVersion.id).where(DocumentVersion.document_id == change_id)
        )
        session.execute(
            update(DocumentLine)
            .where(DocumentLine.document_version_id == initial_version)
            .values(quantity=Decimal("2"), line_total=Decimal("16.00"))
        )
        session.execute(
            update(DocumentLine)
            .where(DocumentLine.document_version_id == change_version)
            .values(quantity=Decimal("-1"), line_total=Decimal("-8.00"))
        )

    report = CorrelationWorker(factory).run_once()
    assert report.correlations_inserted == 1
    with factory() as session:
        correlation = session.scalar(select(DocumentCorrelation))
        assert correlation is not None
        assert "SAME_TABLE_CHANGE_SEQUENCE" in correlation.matched_criteria
        events = session.scalars(select(OrderEvent).order_by(OrderEvent.sequence)).all()
        quantity_events = [event for event in events if event.event_type == "QUANTITY_CHANGED"]
        assert len(quantity_events) == 1
        assert Decimal(quantity_events[0].details["before_quantity"]) == Decimal("2")
        assert Decimal(quantity_events[0].details["after_quantity"]) == Decimal("1")
        assert all(event.event_type != "ITEM_REMOVED" for event in events)
        snapshot = session.scalar(
            select(OrderSnapshot).where(OrderSnapshot.order_event_id == quantity_events[0].id)
        )
        assert snapshot is not None
        assert Decimal(snapshot.lines[0]["quantity"]) == Decimal("1")
    engine.dispose()


def test_worker_groups_cross_department_dispatch_without_fake_line_changes() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        devices = (
            _device(session, "pos_1", "pos", 9100),
            _device(session, "pos_2", "pos", 9101),
            _device(session, "pos_3", "pos", 9102),
        )
        parser = ParserVersion(
            name="synthetic-pos-dispatch",
            version="1.0.0",
            build_sha256=_sha("synthetic-pos-dispatch-parser"),
            protocol="escpos",
        )
        session.add(parser)
        session.flush()
        for index, (device_id, description) in enumerate(
            zip(devices, ("Bibita", "Pietanza", "Pizza"), strict=True)
        ):
            _document(
                session,
                device_id=device_id,
                parser_id=parser.id,
                source=f"department-dispatch-{index}",
                document_type="KITCHEN_ORDER",
                total="0.00",
                when=NOW + timedelta(seconds=index * 10),
                lines=((description, "0.00"),),
                order_code="ORDER-DISPATCH",
                table_code="LAB-20",
            )

    report = CorrelationWorker(factory).run_once()
    assert report.correlations_inserted == 1
    with factory() as session:
        correlation = session.scalar(select(DocumentCorrelation))
        assert correlation is not None
        assert "CROSS_DEPARTMENT_DISPATCH" in correlation.matched_criteria
        assert session.scalar(select(func.count()).select_from(DocumentCorrelationMember)) == 3
        event_types = set(session.scalars(select(OrderEvent.event_type)))
        assert "ITEM_ADDED" not in event_types
        assert "ITEM_REMOVED" not in event_types
    engine.dispose()


def test_worker_persists_missing_pos_prices_by_source_kind_idempotently() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        pos_id = _device(session, "pos_1", "pos", 9100)
        rch_id = _device(session, "rch_1", "rch", 23)
        parser = ParserVersion(
            name="synthetic-price-attribution",
            version="1.0.0",
            build_sha256=_sha("synthetic-price-attribution-parser"),
            protocol="synthetic",
        )
        session.add(parser)
        session.flush()
        target_id = _document(
            session,
            device_id=pos_id,
            parser_id=parser.id,
            source="price-target-kitchen",
            document_type="KITCHEN_ORDER",
            total="0.00",
            when=NOW,
            lines=(("Margherita", "0.00"),),
            order_code="ORDER-PRICE",
            table_code="LAB-PRICE",
        )
        _document(
            session,
            device_id=rch_id,
            parser_id=parser.id,
            source="price-source-prebill",
            document_type="PRE_BILL",
            total="10.00",
            when=NOW + timedelta(minutes=1),
            lines=(("Margherita", "10.00"),),
            order_code="ORDER-PRICE",
            table_code="LAB-PRICE",
        )
        _document(
            session,
            device_id=rch_id,
            parser_id=parser.id,
            source="price-source-management",
            document_type="MANAGEMENT_DOCUMENT",
            total="10.00",
            when=NOW + timedelta(minutes=2),
            lines=(("Margherita", "10.00"),),
            order_code="ORDER-PRICE",
            table_code="LAB-PRICE",
        )
        _document(
            session,
            device_id=rch_id,
            parser_id=parser.id,
            source="price-source-fiscal",
            document_type="COMMERCIAL_DOCUMENT",
            total="8.00",
            when=NOW + timedelta(minutes=3),
            lines=(("Margherita", "8.00"),),
            order_code="ORDER-PRICE",
            table_code="LAB-PRICE",
        )
        for offset, document_type in enumerate(("CONFORMING_COPY", "REPRINT"), start=4):
            _document(
                session,
                device_id=rch_id,
                parser_id=parser.id,
                source=f"excluded-price-source-{document_type.lower()}",
                document_type=document_type,
                total="99.00",
                when=NOW + timedelta(minutes=offset),
                lines=(("Margherita", "99.00"),),
                order_code="ORDER-PRICE",
                table_code="LAB-PRICE",
            )
        session.flush()
        target_version_id = session.scalar(
            select(DocumentVersion.id).where(DocumentVersion.document_id == target_id)
        )
        session.execute(
            update(DocumentLine)
            .where(DocumentLine.document_version_id == target_version_id)
            .values(unit_price=None, line_total=None)
        )

    worker = CorrelationWorker(factory)
    first = worker.run_once()
    second = worker.run_once()

    assert first.price_attributions_inserted == 3
    assert second.price_attributions_inserted == 0
    with factory() as session:
        attributions = session.scalars(
            select(LinePriceAttribution).order_by(LinePriceAttribution.source_kind)
        ).all()
        assert {item.source_kind for item in attributions} == {
            "PREBILL",
            "MANAGEMENT",
            "FISCAL",
        }
        assert {item.status for item in attributions} == {"RESOLVED"}
        assert {item.match_basis for item in attributions} == {
            "DESCRIPTION_NORMALIZED_EXACT"
        }
        assert {item.observed_unit_price for item in attributions} == {
            Decimal("8.0000"),
            Decimal("10.0000"),
        }
        attribution = attributions[0]
        target_line = session.scalar(
            select(DocumentLine).where(DocumentLine.id == attribution.target_line_id)
        )
        assert target_line is not None and target_line.unit_price is None
    document = SqlAlchemyApiRepository(factory).get_document(target_id)
    assert document is not None and len(document.lines) == 1
    assert document.lines[0].unit_price is None
    assert document.lines[0].derived_unit_price is None
    assert document.lines[0].derived_price_source == "CONFLICTING_SOURCES"
    assert len(document.lines[0].price_attributions) == 3
    engine.dispose()


def test_worker_does_not_price_pos_line_from_incomplete_monetary_source() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        pos_id = _device(session, "pos_1", "pos", 9100)
        rch_id = _device(session, "rch_1", "rch", 23)
        parser = ParserVersion(
            name="synthetic-incomplete-price-source",
            version="1.0.0",
            build_sha256=_sha("synthetic-incomplete-price-source-parser"),
            protocol="synthetic",
        )
        session.add(parser)
        session.flush()
        target_id = _document(
            session,
            device_id=pos_id,
            parser_id=parser.id,
            source="incomplete-price-target",
            document_type="KITCHEN_ORDER",
            total="0.00",
            when=NOW,
            lines=(("Voce parziale", "0.00"),),
            order_code="ORDER-INCOMPLETE-PRICE",
            table_code="LAB-INCOMPLETE-PRICE",
        )
        source_id = _document(
            session,
            device_id=rch_id,
            parser_id=parser.id,
            source="incomplete-price-source",
            document_type="PRE_BILL",
            total="10.00",
            when=NOW + timedelta(minutes=1),
            lines=(("Voce parziale", "10.00"),),
            order_code="ORDER-INCOMPLETE-PRICE",
            table_code="LAB-INCOMPLETE-PRICE",
        )
        session.flush()
        target_version_id = session.scalar(
            select(DocumentVersion.id).where(DocumentVersion.document_id == target_id)
        )
        source_version = session.scalar(
            select(DocumentVersion).where(DocumentVersion.document_id == source_id)
        )
        assert target_version_id is not None and source_version is not None
        session.execute(
            update(DocumentLine)
            .where(DocumentLine.document_version_id == target_version_id)
            .values(unit_price=None, line_total=None)
        )
        source_version.complete = False
        source_version.status = "PARTIAL"

    report = CorrelationWorker(factory).run_once()

    assert report.correlations_inserted == 1
    assert report.price_attributions_inserted == 0
    with factory() as session:
        correlation = session.scalar(
            select(DocumentCorrelation).where(DocumentCorrelation.status == "AUTOMATIC")
        )
        assert correlation is not None
        assert session.scalar(
            select(func.count())
            .select_from(DocumentCorrelationMember)
            .where(DocumentCorrelationMember.correlation_id == correlation.id)
        ) == 2
        assert session.scalar(select(func.count()).select_from(LinePriceAttribution)) == 0
    document = SqlAlchemyApiRepository(factory).get_document(target_id)
    assert document is not None and len(document.lines) == 1
    assert document.lines[0].unit_price is None
    assert document.lines[0].derived_unit_price is None
    assert document.lines[0].derived_price_source is None
    assert document.lines[0].price_attributions == []
    engine.dispose()


def test_order_without_fiscal_close_remains_idempotent_across_worker_polls() -> None:
    engine, factory = _database()
    _seed_scenario(factory, split=False)
    with factory.begin() as session:
        # Leave only the source/pre-bill side of the transaction and make it old
        # enough for ORDER_WITHOUT_FISCAL_CLOSE.  Re-evaluation time must not be
        # part of the stable finding identity.
        fiscal_ids = select(Document.id).where(Document.document_type == "COMMERCIAL_DOCUMENT")
        session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.document_id.in_(fiscal_ids))
            .values(document_type="UNKNOWN")
        )
        session.execute(
            update(Document)
            .where(Document.document_type == "COMMERCIAL_DOCUMENT")
            .values(document_type="UNKNOWN")
        )
        session.execute(
            update(DocumentVersion).values(document_timestamp=datetime(2000, 1, 1, tzinfo=UTC))
        )
        session.execute(
            update(Document).values(
                captured_at=datetime(2000, 1, 1, tzinfo=UTC),
                document_timestamp=datetime(2000, 1, 1, tzinfo=UTC),
            )
        )

    correlation = CorrelationWorker(factory)
    correlation.run_once()
    worker = FraudWorker(factory, order_without_fiscal_close_minutes=1)
    first = worker.run_once()
    second = worker.run_once()

    assert first.alerts_inserted >= 1
    assert second.alerts_inserted == 0
    with factory() as session:
        matching = session.scalars(
            select(FraudAlert)
            .join(FraudRuleVersion, FraudRuleVersion.id == FraudAlert.fraud_rule_version_id)
            .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
            .where(FraudRule.code == "ORDER_WITHOUT_FISCAL_CLOSE")
        ).all()
        assert len(matching) == 1
    engine.dispose()


def test_post_prebill_economic_close_persists_the_observed_35_to_5_delta() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        pos_id = _device(session, "pos_synthetic", "pos", 9100)
        rch_id = _device(session, "rch_synthetic", "rch", 23)
        parser = ParserVersion(
            name="test",
            version="1.0.0",
            build_sha256=_sha("synthetic-parser"),
            protocol="test",
        )
        session.add(parser)
        session.flush()
        _document(
            session,
            device_id=pos_id,
            parser_id=parser.id,
            source="synthetic-prebill-35",
            document_type="PRE_BILL",
            total="35.00",
            when=NOW,
            lines=(
                ("Cover", "4.00"),
                ("Drink A", "8.00"),
                ("Food A", "8.00"),
                ("Food B", "7.00"),
                ("Food C", "8.00"),
            ),
        )
        close_id = _document(
            session,
            device_id=rch_id,
            parser_id=parser.id,
            source="synthetic-room-close-5",
            document_type="MANAGEMENT_DOCUMENT",
            total="5.00",
            when=NOW + timedelta(minutes=20),
            lines=(
                ("Cover", "4.00"),
                ("Drink A", "1.00"),
                ("Food A", "0.00"),
                ("Food B", "0.00"),
                ("Food C", "0.00"),
            ),
        )
        close_version = session.scalar(
            select(DocumentVersion).where(DocumentVersion.document_id == close_id)
        )
        assert close_version is not None
        close_version.commercial_reference_code = "LAB-COMM-9901-0041"
        close_version.raw_metadata = {
            "economic_close": True,
            "settlement_kind": "ROOM_CHARGE",
        }

    correlation = CorrelationWorker(factory)
    assert correlation.run_once().correlations_inserted == 1
    fraud = FraudWorker(factory)
    first = fraud.run_once()
    with factory.begin() as session:
        primary_version = session.scalar(
            select(FraudRuleVersion)
            .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
            .where(FraudRule.code == "MODIFICA_POST_PRECONTO")
        )
        assert primary_version is not None
        primary_version.configuration_fingerprint = _sha("simulated-previous-rule-version")
    second = fraud.run_once()
    assert first.alerts_inserted >= 1
    assert second.alerts_inserted == 0
    assert second.evidence_inserted == 0

    with factory() as session:
        alert = session.scalar(
            select(FraudAlert)
            .join(FraudRuleVersion, FraudRuleVersion.id == FraudAlert.fraud_rule_version_id)
            .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
            .where(FraudRule.code == "MODIFICA_POST_PRECONTO")
        )
        assert alert is not None
        assert alert.original_amount == Decimal("35.0000")
        assert alert.final_amount == Decimal("5.0000")
        assert alert.difference_amount == Decimal("30.0000")
        assert alert.difference_percent == Decimal("85.7143")
        evidence = session.scalar(
            select(FraudAlertEvidence).where(
                FraudAlertEvidence.fraud_alert_id == alert.id,
                FraudAlertEvidence.evidence_type == "post_prebill_economic_outcome",
            )
        )
        assert evidence is not None
        assert Decimal(evidence.evidence["observed_final_total"]) == Decimal("5.00")
        references = session.scalars(
            select(FraudAlertEvidence)
            .where(
                FraudAlertEvidence.fraud_alert_id == alert.id,
                FraudAlertEvidence.evidence_type == "CORRELATED_DOCUMENT_REFERENCE",
            )
            .order_by(FraudAlertEvidence.sequence)
        ).all()
        assert len(references) == 2
        assert {item.evidence["role"] for item in references} == {
            "PRE_BILL",
            "NON_FISCAL_CLOSE",
        }
        assert {item.evidence["external_document_code"] for item in references} == {
            "DOC-synthetic-prebill-35",
            "DOC-synthetic-room-close-5",
        }
        assert all("commercial_reference_code" in item.evidence for item in references)
        management_reference = next(
            item for item in references if item.evidence["role"] == "NON_FISCAL_CLOSE"
        )
        assert management_reference.evidence["commercial_reference_code"] == "LAB-COMM-9901-0041"
        assert all(item.document_id is not None for item in references)
        assert all(item.raw_payload_id is not None for item in references)
        assert all(item.artifact_path for item in references)
        assert all(item.artifact_sha256 for item in references)
    engine.dispose()


def test_cash_price_reduction_persists_one_alert_loss_and_all_document_references() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        pos_id = _device(session, "pos_lab", "pos", 9100)
        rch_id = _device(session, "rch_lab", "rch", 23)
        parser = ParserVersion(
            name="test",
            version="1.0.0",
            build_sha256=_sha("lab-price-reduction-parser"),
            protocol="test",
        )
        session.add(parser)
        session.flush()
        prebill_id = _document(
            session,
            device_id=pos_id,
            parser_id=parser.id,
            source="lab-prebill-three-euro",
            document_type="MANAGEMENT_DOCUMENT",
            total="3.00",
            when=NOW,
            lines=(("Synthetic beverage", "3.00"),),
            order_code="LAB-ORDER-25B",
            table_code="LAB-25B",
            external_document_code=None,
            auto_external_document_code=False,
        )
        fiscal_id = _document(
            session,
            device_id=rch_id,
            parser_id=parser.id,
            source="lab-commercial-ten-cent",
            document_type="COMMERCIAL_DOCUMENT",
            total="0.10",
            when=NOW + timedelta(minutes=2),
            lines=(("Synthetic beverage", "0.10"),),
            payment="0.10",
            order_code="LAB-ORDER-25B",
            table_code="LAB-25B",
            external_document_code=None,
            external_document_code_suffix="0041",
            auto_external_document_code=False,
            raw_metadata={
                "external_document_code_suffix_evidence": (
                    "RCH_STATUS_RESPONSE_SUFFIX_SEQUENCE_CONFIRMED"
                )
            },
        )
        management_id = _document(
            session,
            device_id=rch_id,
            parser_id=parser.id,
            source="lab-management-copy-ten-cent",
            document_type="MANAGEMENT_DOCUMENT",
            total="0.10",
            when=NOW + timedelta(minutes=3),
            lines=(("Synthetic beverage", "0.10"),),
            order_code="LAB-ORDER-25B",
            table_code="LAB-25B",
            external_document_code=None,
            commercial_reference_code="LAB-FSC-0041",
            auto_external_document_code=False,
        )

    correlation = CorrelationWorker(factory)
    assert correlation.run_once().correlations_inserted == 1
    fraud = FraudWorker(factory)
    first = fraud.run_once()
    second = fraud.run_once()
    assert first.alerts_inserted == 1
    assert second.alerts_inserted == 0
    assert second.evidence_inserted == 0

    with factory() as session:
        alerts = session.execute(
            select(FraudAlert, FraudRule.code)
            .join(FraudRuleVersion, FraudRuleVersion.id == FraudAlert.fraud_rule_version_id)
            .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
        ).all()
        assert len(alerts) == 1
        alert, rule_code = alerts[0]
        assert rule_code == "MODIFICA_POST_PRECONTO"
        assert alert.status == "OPEN"
        assert alert.severity == "HIGH"
        assert alert.original_amount == Decimal("3.0000")
        assert alert.final_amount == Decimal("0.1000")
        assert alert.difference_amount == Decimal("2.9000")
        assert alert.difference_percent == Decimal("96.6667")

        references = session.scalars(
            select(FraudAlertEvidence)
            .where(
                FraudAlertEvidence.fraud_alert_id == alert.id,
                FraudAlertEvidence.evidence_type == "CORRELATED_DOCUMENT_REFERENCE",
            )
            .order_by(FraudAlertEvidence.sequence)
        ).all()
        assert {item.document_id for item in references} == {
            prebill_id,
            fiscal_id,
            management_id,
        }
        by_role = {item.evidence["role"]: item.evidence for item in references}
        assert by_role["MANAGEMENT_PREBILL"]["external_document_code"] is None
        assert by_role["COMMERCIAL_CLOSE"]["external_document_code"] is None
        assert by_role["COMMERCIAL_CLOSE"]["external_document_code_suffix"] == "0041"
        assert by_role["MANAGEMENT_COPY"]["external_document_code"] is None
        assert (
            by_role["MANAGEMENT_COPY"]["commercial_reference_code"]
            == "LAB-FSC-0041"
        )
        assert all(item.artifact_path and item.artifact_sha256 for item in references)

    # Historical 1.1 workers treated the printed management copy as a new POS
    # action.  Verify that the upgrade closes that auxiliary false positive
    # without deleting its evidence.
    with factory.begin() as session:
        late_version = session.scalar(
            select(FraudRuleVersion)
            .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
            .where(FraudRule.code == "LATE_ORDER_MODIFICATION")
        )
        correlation_row = session.scalar(select(DocumentCorrelation))
        late_event = session.scalar(
            select(OrderEvent).where(OrderEvent.source_document_id == management_id)
        )
        assert late_version is not None and correlation_row is not None and late_event is not None
        late_version.implementation_version = "rpg-fraud-1.1.0"
        old_late_alert = FraudAlert(
            fraud_rule_version_id=late_version.id,
            correlation_id=correlation_row.id,
            transaction_id=correlation_row.transaction_id,
            finding_key=_sha("old-management-output-late-alert"),
            severity="HIGH",
            score=75,
            status="OPEN",
            description="Output gestionale interpretato come modifica tardiva",
            explanation="Fixture sintetica",
            confidence=90,
            opened_at=NOW + timedelta(minutes=3),
        )
        session.add(old_late_alert)
        session.flush()
        session.add(
            FraudAlertEvidence(
                fraud_alert_id=old_late_alert.id,
                sequence=1,
                document_id=management_id,
                evidence_type="late_order_event",
                summary="Evento sintetico derivato dall'output gestionale",
                evidence={
                    "kind": "late_order_event",
                    "event_id": str(late_event.id),
                },
            )
        )
        session.flush()
        assert FraudWorker._reclassify_known_false_positives(session, NOW) == 1
        assert old_late_alert.status == "FALSE_POSITIVE"
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
        event_types = set(session.scalars(select(OrderEvent.event_type)))
        assert "ITEM_REMOVED" not in event_types
        assert "PRICE_CHANGED" not in event_types
    engine.dispose()


def test_compat_amount_alert_persists_only_complete_fiscal_aggregate() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        pos_id = _device(session, "pos_1", "pos", 9100)
        rch_id = _device(session, "rch_1", "rch", 23)
        parser = ParserVersion(
            name="test",
            version="1.0.0",
            build_sha256=_sha("parser-compat-partial"),
            protocol="test",
        )
        session.add(parser)
        session.flush()
        prebill_id = _document(
            session,
            device_id=pos_id,
            parser_id=parser.id,
            source="compat-prebill",
            document_type="PRE_BILL",
            total="100.00",
            when=NOW,
            lines=(("Conto", "100.00"),),
        )
        session.flush()
        prebill_version = session.scalar(
            select(DocumentVersion).where(DocumentVersion.document_id == prebill_id)
        )
        assert prebill_version is not None
        prebill_version.gross_total = None
        prebill_version.net_total = Decimal("100.00")
        _document(
            session,
            device_id=rch_id,
            parser_id=parser.id,
            source="compat-fiscal-complete",
            document_type="COMMERCIAL_DOCUMENT",
            total="50.00",
            when=NOW + timedelta(minutes=1),
            lines=(("Conto", "50.00"),),
        )
        _document(
            session,
            device_id=rch_id,
            parser_id=parser.id,
            source="compat-fiscal-partial",
            document_type="COMMERCIAL_DOCUMENT",
            total="20.00",
            when=NOW + timedelta(minutes=2),
            lines=(("Tentativo", "20.00"),),
            complete=False,
        )

    worker = FraudWorker(factory)
    worker.run_once()
    with factory.begin() as session:
        primary = session.scalar(
            select(FraudRule).where(FraudRule.code == "MODIFICA_POST_PRECONTO")
        )
        assert primary is not None
        primary.enabled = False
    CorrelationWorker(factory).run_once()
    worker.run_once()

    with factory() as session:
        alert = session.scalar(
            select(FraudAlert)
            .join(FraudRuleVersion, FraudRuleVersion.id == FraudAlert.fraud_rule_version_id)
            .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
            .where(FraudRule.code == "PREBILL_FISCAL_AMOUNT_DROP")
        )
        assert alert is not None
        assert alert.original_amount == Decimal("100.0000")
        assert alert.final_amount == Decimal("50.0000")
        assert alert.difference_amount == Decimal("50.0000")
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
            select(FraudAlert).where(
                FraudAlert.description.contains("Riduzione del valore di vendita")
            )
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
                    FraudAlert.description.contains("Riduzione del valore di vendita"),
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


def test_same_membership_recorrelation_closes_obsolete_alert() -> None:
    engine, factory = _database()
    _seed_scenario(factory, split=False)
    CorrelationWorker(factory).run_once()
    FraudWorker(factory).run_once()
    with factory.begin() as session:
        old = session.scalar(
            select(DocumentCorrelation).where(
                DocumentCorrelation.status.in_(("AUTOMATIC", "UNCORRELATED"))
            )
        )
        alert = session.scalar(select(FraudAlert).where(FraudAlert.status == "OPEN"))
        assert old is not None and alert is not None
        old.status = "SUPERSEDED"
        replacement = DocumentCorrelation(
            transaction_id=old.transaction_id,
            algorithm_version=ALGORITHM_VERSION,
            input_fingerprint=_sha("same-members-new-parser-version"),
            score=old.score,
            status="AUTOMATIC",
            matched_criteria=old.matched_criteria,
            unmatched_criteria=old.unmatched_criteria,
            explanation="Nuova interpretazione parser sugli stessi documenti.",
        )
        session.add(replacement)
        session.flush()
        members = session.scalars(
            select(DocumentCorrelationMember).where(
                DocumentCorrelationMember.correlation_id == old.id
            )
        ).all()
        for member in members:
            session.add(
                DocumentCorrelationMember(
                    correlation_id=replacement.id,
                    document_id=member.document_id,
                    role=member.role,
                    contribution_score=member.contribution_score,
                    criteria=member.criteria,
                )
            )
        session.flush()
        resolved = FraudWorker._resolve_superseded_alerts(session, NOW)
        assert resolved == 1
        alert_id = alert.id

    with factory() as session:
        alert = session.get(FraudAlert, alert_id)
        assert alert is not None
        assert alert.status == "JUSTIFIED"
        assert alert.closed_at is not None
        history = session.scalars(
            select(FraudAlertHistory)
            .where(FraudAlertHistory.fraud_alert_id == alert_id)
            .order_by(FraudAlertHistory.sequence)
        ).all()
        assert history[-1].event_type == "ALERT_AUTO_SUPERSEDED"
        assert history[-1].previous_record_hash == history[-2].record_hash
    engine.dispose()


def test_old_operator_self_amplification_alert_is_reclassified_idempotently() -> None:
    engine, factory = _database()
    FraudWorker(factory).run_once()
    with factory.begin() as session:
        version = session.scalar(
            select(FraudRuleVersion)
            .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
            .where(FraudRule.code == "UNUSUAL_OPERATOR_PATTERN")
        )
        assert version is not None
        version.implementation_version = "rpg-fraud-1.0.0"
        alert = FraudAlert(
            fraud_rule_version_id=version.id,
            transaction_id=uuid4(),
            finding_key=_sha("old-operator-alert"),
            severity="MEDIUM",
            score=75,
            status="OPEN",
            description="Vecchio pattern operatore auto-amplificato",
            explanation="Fixture sintetica",
            confidence=80,
            opened_at=NOW,
        )
        session.add(alert)
        session.flush()
        alert_id = alert.id
        assert FraudWorker._reclassify_known_false_positives(session, NOW) == 1
        assert FraudWorker._reclassify_known_false_positives(session, NOW) == 0

    with factory() as session:
        alert = session.get(FraudAlert, alert_id)
        assert alert is not None and alert.status == "FALSE_POSITIVE"
        evidence = session.scalar(
            select(FraudAlertEvidence).where(
                FraudAlertEvidence.fraud_alert_id == alert_id,
                FraudAlertEvidence.evidence_type == "ENGINE_FALSE_POSITIVE",
            )
        )
        assert evidence is not None
        history = session.scalar(
            select(FraudAlertHistory).where(FraudAlertHistory.fraud_alert_id == alert_id)
        )
        assert history is not None
        assert history.event_type == "ALERT_AUTO_FALSE_POSITIVE"
    engine.dispose()


def test_old_auxiliary_amount_alert_is_reclassified_under_canonical_loss_rule() -> None:
    engine, factory = _database()
    FraudWorker(factory).run_once()
    with factory.begin() as session:
        version = session.scalar(
            select(FraudRuleVersion)
            .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
            .where(FraudRule.code == "PREBILL_FISCAL_AMOUNT_DROP")
        )
        assert version is not None
        version.implementation_version = "rpg-fraud-1.1.0"
        alert = FraudAlert(
            fraud_rule_version_id=version.id,
            transaction_id=uuid4(),
            finding_key=_sha("old-auxiliary-amount-alert"),
            severity="HIGH",
            score=90,
            status="OPEN",
            description="Sintomo economico ausiliario",
            explanation="Fixture sintetica",
            original_amount=Decimal("3.00"),
            final_amount=Decimal("0.10"),
            difference_amount=Decimal("2.90"),
            confidence=90,
            opened_at=NOW,
        )
        session.add(alert)
        session.flush()
        alert_id = alert.id
        assert FraudWorker._reclassify_known_false_positives(session, NOW) == 1
        assert FraudWorker._reclassify_known_false_positives(session, NOW) == 0

    with factory() as session:
        alert = session.get(FraudAlert, alert_id)
        assert alert is not None and alert.status == "FALSE_POSITIVE"
        evidence = session.scalar(
            select(FraudAlertEvidence).where(
                FraudAlertEvidence.fraud_alert_id == alert_id,
                FraudAlertEvidence.evidence_type == "ENGINE_FALSE_POSITIVE",
            )
        )
        assert evidence is not None
        assert evidence.evidence["defect"] == "auxiliary_post_prebill_alert_noise"
        assert evidence.evidence["canonical_rule_code"] == "MODIFICA_POST_PRECONTO"
    engine.dispose()


def test_old_zero_value_footer_alert_is_reclassified_but_sale_item_is_preserved() -> None:
    engine, factory = _database()
    FraudWorker(factory).run_once()
    with factory.begin() as session:
        version = session.scalar(
            select(FraudRuleVersion)
            .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
            .where(FraudRule.code == "NEGATIVE_OR_ZERO_VALUE_ITEM")
        )
        assert version is not None
        version.implementation_version = "rpg-fraud-1.1.0"
        alert_ids: dict[str, UUID] = {}
        for label, description in (("technical", "RESTO"), ("sale", "PROMO ITEM")):
            alert = FraudAlert(
                fraud_rule_version_id=version.id,
                transaction_id=uuid4(),
                finding_key=_sha(f"old-zero-{label}-alert"),
                severity="HIGH",
                score=70,
                status="OPEN",
                description="Riga a valore zero",
                explanation="Fixture sintetica",
                confidence=90,
                opened_at=NOW,
            )
            session.add(alert)
            session.flush()
            alert_ids[label] = alert.id
            session.add(
                FraudAlertEvidence(
                    fraud_alert_id=alert.id,
                    sequence=1,
                    evidence_type="non_positive_item",
                    summary="Riga sintetica",
                    evidence={
                        "kind": "non_positive_item",
                        "description": description,
                        "unit_price": "0.00",
                        "line_total": "0.00",
                    },
                )
            )
        session.flush()
        assert FraudWorker._reclassify_known_false_positives(session, NOW) == 1

    with factory() as session:
        technical = session.get(FraudAlert, alert_ids["technical"])
        sale = session.get(FraudAlert, alert_ids["sale"])
        assert technical is not None and technical.status == "FALSE_POSITIVE"
        assert sale is not None and sale.status == "OPEN"
    engine.dispose()


def test_global_duplicate_outside_correlation_is_reclassified_without_deletion() -> None:
    engine, factory = _database()
    _seed_scenario(factory, split=False)
    CorrelationWorker(factory).run_once()
    FraudWorker(factory).run_once()
    outside_ids = (uuid4(), uuid4())
    with factory.begin() as session:
        correlation = session.scalar(
            select(DocumentCorrelation).where(
                DocumentCorrelation.status.in_(("AUTOMATIC", "UNCORRELATED"))
            )
        )
        version = session.scalar(
            select(FraudRuleVersion)
            .join(FraudRule, FraudRule.id == FraudRuleVersion.fraud_rule_id)
            .where(FraudRule.code == "DUPLICATE_DOCUMENT")
        )
        assert correlation is not None and version is not None
        version.implementation_version = "rpg-fraud-1.0.0"
        alert = FraudAlert(
            fraud_rule_version_id=version.id,
            correlation_id=correlation.id,
            transaction_id=correlation.transaction_id,
            finding_key=_sha("global-duplicate-outside-correlation"),
            severity="HIGH",
            score=90,
            status="OPEN",
            description="Duplicato globale assegnato a transazione estranea",
            explanation="Fixture sintetica",
            confidence=90,
            opened_at=NOW,
        )
        session.add(alert)
        session.flush()
        session.add(
            FraudAlertEvidence(
                fraud_alert_id=alert.id,
                sequence=1,
                evidence_type="duplicate_document",
                summary="Documenti esterni alla correlazione",
                evidence={
                    "kind": "duplicate_document",
                    "document_ids": [str(item) for item in outside_ids],
                },
            )
        )
        session.flush()
        alert_id = alert.id
        assert FraudWorker._reclassify_known_false_positives(session, NOW) == 1

    with factory() as session:
        alert = session.get(FraudAlert, alert_id)
        assert alert is not None and alert.status == "FALSE_POSITIVE"
        assert session.scalar(
            select(func.count())
            .select_from(FraudAlertEvidence)
            .where(FraudAlertEvidence.fraud_alert_id == alert_id)
        ) == 2
        assert session.scalar(
            select(FraudAlertHistory.event_type).where(
                FraudAlertHistory.fraud_alert_id == alert_id
            )
        ) == "ALERT_AUTO_FALSE_POSITIVE"
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
        post_prebill_rule = session.scalar(
            select(FraudRule).where(FraudRule.code == "MODIFICA_POST_PRECONTO")
        )
        assert post_prebill_rule is not None
        post_prebill_versions = session.scalars(
            select(FraudRuleVersion)
            .where(FraudRuleVersion.fraud_rule_id == post_prebill_rule.id)
            .order_by(FraudRuleVersion.version)
        ).all()
        assert [row.configuration["minimum_percent"] for row in post_prebill_versions] == [
            "20",
            "60",
            "20",
        ]
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
        stored_version = session.scalar(
            select(DocumentVersion).where(DocumentVersion.document_id == unrelated)
        )
        assert stored_version is not None
        stored_version.order_code = "ORDER-UNRELATED"
        stored_version.table_code = "TABLE-UNRELATED"

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
        late_version = session.scalar(
            select(DocumentVersion).where(DocumentVersion.document_id == late_id)
        )
        assert late_version is not None
        late_version.external_document_code = "DOC-LATE"

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
        # A nullable field in the selected immutable version is meaningful: it
        # must not inherit the value later written to the mutable projection.
        first.order_code = None
        session.add(
            DocumentVersion(
                document_id=document_id,
                parser_version_id=new.id,
                raw_payload_id=first.raw_payload_id,
                version_sequence=2,
                document_type="ORDER_CHANGE",
                subtype="ORDER_CHANGE_REPARSED",
                external_document_code="DOC-rollback-v2",
                order_code="ORDER-V2",
                table_code="TABLE-V2",
                operator_code="OP-V2",
                terminal_code="TERM-V2",
                document_timestamp=NOW + timedelta(seconds=30),
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
        projected = session.get(Document, document_id)
        assert projected is not None
        projected.document_type = "ORDER_CHANGE"
        projected.subtype = "ORDER_CHANGE_REPARSED"
        projected.external_document_code = "DOC-rollback-v2"
        projected.order_code = "ORDER-V2"
        projected.table_code = "TABLE-V2"
        projected.operator_code = "OP-V2"
        projected.terminal_code = "TERM-V2"
        projected.document_timestamp = NOW + timedelta(seconds=30)
    with factory() as session:
        loaded = load_latest_documents(session, document_ids={document_id})
        assert len(loaded) == 1
        assert loaded[0].value.parser_version == "2.0.0"
        assert loaded[0].value.gross_total == Decimal("1.0000")
        assert loaded[0].value.type.value == "ORDER_CHANGE"
        assert loaded[0].value.table_code == "TABLE-V2"
        assert loaded[0].value.operator_code == "OP-V2"
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
        assert loaded[0].value.type.value == "PRE_BILL"
        assert loaded[0].value.order_code is None
        assert loaded[0].value.table_code == "LAB-25"
        assert loaded[0].value.operator_code == "OP-1"
    engine.dispose()


def test_wholly_legacy_version_uses_document_projection_row_wide() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        device_id = _device(session, "pos_1", "pos", 9100)
        parser = ParserVersion(
            name="legacy-import",
            version="0",
            build_sha256=_sha("legacy-import-build"),
            protocol="escpos",
        )
        session.add(parser)
        session.flush()
        document_id = _document(
            session,
            device_id=device_id,
            parser_id=parser.id,
            source="legacy-row",
            document_type="PRE_BILL",
            total="12.00",
            when=NOW,
            lines=(("Legacy", "12.00"),),
            order_code="LEGACY-ORDER",
            table_code="LEGACY-TABLE",
            operator_code="LEGACY-OPERATOR",
        )
        version = session.scalar(
            select(DocumentVersion).where(DocumentVersion.document_id == document_id)
        )
        assert version is not None
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
            setattr(version, field, None)

    with factory() as session:
        loaded = load_latest_documents(session, document_ids={document_id})
        assert len(loaded) == 1
        value = loaded[0].value
        assert value.type.value == "PRE_BILL"
        assert value.order_code == "LEGACY-ORDER"
        assert value.table_code == "LEGACY-TABLE"
        assert value.operator_code == "LEGACY-OPERATOR"
        assert value.document_timestamp == NOW
    engine.dispose()


def test_candidate_blocking_uses_semantics_from_selected_parser_version() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        device_id = _device(session, "pos_1", "pos", 9100)
        old = ParserVersion(
            name="escpos",
            version="1.0.0",
            build_sha256=_sha("candidate-old-build"),
            protocol="escpos",
        )
        new = ParserVersion(
            name="escpos",
            version="2.0.0",
            build_sha256=_sha("candidate-new-build"),
            protocol="escpos",
        )
        session.add_all((old, new))
        session.flush()
        seed_id = _document(
            session,
            device_id=device_id,
            parser_id=old.id,
            source="candidate-seed",
            document_type="PRE_BILL",
            total="20.00",
            when=NOW,
            lines=(("Seed", "20.00"),),
            order_code="ORDER-OLD",
            table_code="TABLE-OLD",
            operator_code="OP-OLD",
        )
        old_candidate_id = _document(
            session,
            device_id=device_id,
            parser_id=old.id,
            source="candidate-old",
            document_type="COMMERCIAL_DOCUMENT",
            total="20.00",
            when=NOW,
            lines=(("Old", "20.00"),),
            order_code="ORDER-OLD",
            table_code="TABLE-OLD",
            operator_code="OP-OLD",
        )
        new_candidate_id = _document(
            session,
            device_id=device_id,
            parser_id=new.id,
            source="candidate-new",
            document_type="COMMERCIAL_DOCUMENT",
            total="20.00",
            when=NOW,
            lines=(("New", "20.00"),),
            order_code="ORDER-NEW",
            table_code="TABLE-NEW",
            operator_code="OP-NEW",
        )
        first = session.scalar(
            select(DocumentVersion).where(DocumentVersion.document_id == seed_id)
        )
        assert first is not None
        first.order_code = None
        second = DocumentVersion(
            document_id=seed_id,
            parser_version_id=new.id,
            raw_payload_id=first.raw_payload_id,
            version_sequence=2,
            document_type="ORDER_CHANGE",
            subtype="ORDER_CHANGE_REPARSED",
            external_document_code="DOC-candidate-seed-v2",
            order_code="ORDER-NEW",
            table_code="TABLE-NEW",
            operator_code="OP-NEW",
            terminal_code="TERM-NEW",
            document_timestamp=NOW,
            gross_total=Decimal("20.00"),
            status="COMPLETE",
            normalized_text="new selected semantics",
            parse_confidence=100,
            evidence_level="CONFIRMED",
            source_manifest_sha256=first.source_manifest_sha256,
            source_payload_sha256=first.source_payload_sha256,
            source_path=first.source_path,
            complete=True,
            chain_scope=first.chain_scope,
            chain_sequence=2,
            previous_record_hash=first.record_hash,
            record_hash=_sha("candidate-seed-v2-record"),
            parsed_at=NOW + timedelta(minutes=10),
        )
        session.add(second)
        # Deliberately make the legacy/current projection point at v2.  A v1
        # activation must still use v1 fields for loading and SQL blocking.
        projected = session.get(Document, seed_id)
        assert projected is not None
        projected.document_type = "ORDER_CHANGE"
        projected.subtype = "ORDER_CHANGE_REPARSED"
        projected.external_document_code = "DOC-candidate-seed-v2"
        projected.order_code = "ORDER-NEW"
        projected.table_code = "TABLE-NEW"
        projected.operator_code = "OP-NEW"
        projected.terminal_code = "TERM-NEW"
        shadow_only_candidate = session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == new_candidate_id
            )
        )
        assert shadow_only_candidate is not None
        shadow_only_candidate.table_code = None
        shadow_candidate_projection = session.get(Document, new_candidate_id)
        assert shadow_candidate_projection is not None
        shadow_candidate_projection.table_code = "TABLE-OLD"
        session.add(
            ActiveParserVersion(
                parser_name="escpos",
                parser_version_id=old.id,
                activation_reason="candidate test v1",
            )
        )
        for version in session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id != seed_id)
        ):
            version.parsed_at = NOW - timedelta(minutes=10)
        first.parsed_at = NOW + timedelta(minutes=10)

    def install_watermark(session, parser_id: UUID) -> None:
        fingerprint = hashlib.sha256(
            canonical_json([{"name": "escpos", "parser_version_id": str(parser_id)}])
        ).hexdigest()
        session.add(
            AnalysisWatermark(
                service="correlation",
                cursor_timestamp=NOW,
                cursor_id=uuid4(),
                processed_count=0,
                metadata_json={
                    "parser_activation_fingerprint": fingerprint,
                    "correlation_algorithm_version": ALGORITHM_VERSION,
                    "price_attribution_algorithm_version": PRICE_ATTRIBUTION_ALGORITHM,
                },
            )
        )

    with factory.begin() as session:
        old_id = session.scalar(select(ParserVersion.id).where(ParserVersion.version == "1.0.0"))
        assert old_id is not None
        install_watermark(session, old_id)
        session.flush()
        loaded, seeds = _candidate_batch(session, limit=10, lookback_seconds=60)
        assert seeds == {seed_id}
        assert {item.value.id for item in loaded} == {seed_id, old_candidate_id}
        assert next(item.value for item in loaded if item.value.id == seed_id).table_code == (
            "TABLE-OLD"
        )
        assert next(item.value for item in loaded if item.value.id == seed_id).order_code is None

    with factory.begin() as session:
        session.delete(session.get(AnalysisWatermark, "correlation"))
        pointer = session.get(ActiveParserVersion, "escpos")
        new_id = session.scalar(select(ParserVersion.id).where(ParserVersion.version == "2.0.0"))
        assert pointer is not None and new_id is not None
        pointer.parser_version_id = new_id
        install_watermark(session, new_id)
        session.flush()
        loaded, seeds = _candidate_batch(session, limit=10, lookback_seconds=60)
        assert seeds == {seed_id}
        assert {item.value.id for item in loaded} == {seed_id, new_candidate_id}
        selected = next(item.value for item in loaded if item.value.id == seed_id)
        assert selected.type.value == "ORDER_CHANGE"
        assert selected.table_code == "TABLE-NEW"
        assert selected.operator_code == "OP-NEW"
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


def test_correlation_algorithm_change_rewinds_control_plane_watermark() -> None:
    engine, factory = _database()
    with factory.begin() as session:
        device_id = _device(session, "pos_1", "pos", 9100)
        parser = ParserVersion(
            name="synthetic-watermark",
            version="1.0.0",
            build_sha256=_sha("synthetic-watermark-parser"),
            protocol="synthetic",
        )
        session.add(parser)
        session.flush()
        document_id = _document(
            session,
            device_id=device_id,
            parser_id=parser.id,
            source="algorithm-rewind",
            document_type="KITCHEN_ORDER",
            total="0.00",
            when=NOW,
            lines=(("Voce sintetica", "0.00"),),
        )
        session.add(
            AnalysisWatermark(
                service="correlation",
                cursor_timestamp=NOW + timedelta(days=1),
                cursor_id=uuid4(),
                processed_count=10,
                metadata_json={
                    "parser_activation_fingerprint": hashlib.sha256(
                        canonical_json([])
                    ).hexdigest(),
                    "correlation_algorithm_version": "rpg-correlation-obsolete",
                    "price_attribution_algorithm_version": PRICE_ATTRIBUTION_ALGORITHM,
                },
            )
        )

    with factory.begin() as session:
        loaded, seeds = _candidate_batch(session, limit=10, lookback_seconds=60)
        assert seeds == {document_id}
        assert {item.value.id for item in loaded} == {document_id}
        watermark = session.get(AnalysisWatermark, "correlation")
        assert watermark is not None
        assert watermark.metadata_json["correlation_algorithm_version"] == ALGORITHM_VERSION
    engine.dispose()
