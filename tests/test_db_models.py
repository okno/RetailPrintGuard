from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from retailprintguard.db import Base, create_db_engine, session_factory, transaction
from retailprintguard.db.models import (
    Device,
    Document,
    DocumentVersion,
    ParserVersion,
    PrintJob,
    ProxySession,
    RawPayload,
)

REQUIRED_TABLES = {
    "active_parser_versions",
    "analysis_watermarks",
    "devices",
    "device_status",
    "proxy_sessions",
    "stream_chunks",
    "print_jobs",
    "raw_payloads",
    "documents",
    "document_versions",
    "document_lines",
    "orders",
    "order_events",
    "order_snapshots",
    "payments",
    "document_correlations",
    "fraud_rules",
    "fraud_alerts",
    "fraud_alert_evidence",
    "users",
    "roles",
    "user_roles",
    "audit_log",
    "system_events",
    "import_batches",
    "parser_versions",
}


def _sha(character: str) -> str:
    return character * 64


def test_complete_normalized_schema_and_mariadb_storage_contract() -> None:
    assert set(Base.metadata.tables) >= REQUIRED_TABLES
    assert len(Base.metadata.tables) >= len(REQUIRED_TABLES)

    device_ddl = str(CreateTable(Device.__table__).compile(dialect=mysql.dialect()))
    version_ddl = str(CreateTable(DocumentVersion.__table__).compile(dialect=mysql.dialect()))
    raw_ddl = str(CreateTable(RawPayload.__table__).compile(dialect=mysql.dialect()))
    assert "BINARY(16)" in device_ddl
    assert "ENGINE=InnoDB" in device_ddl
    assert "CHARSET=utf8mb4" in device_ddl
    assert "DECIMAL(19, 4)" in version_ddl
    assert "LONGBLOB" in raw_ddl


def test_uuid_decimal_utc_and_parser_version_history_round_trip() -> None:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    now = datetime(2042, 5, 6, 12, 30, tzinfo=UTC)
    device_id, session_id, job_id = uuid4(), uuid4(), uuid4()
    document_id, parser_one_id, parser_two_id = uuid4(), uuid4(), uuid4()

    with transaction(factory) as session:
        session.add(
            Device(
                id=device_id,
                external_id="pos_1",
                name="POS sintetica",
                device_type="pos",
                parser_kind="escpos",
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
                source_system="printproxy",
                source_instance="test",
                source_scope="printer-a",
                source_session_id="session-a",
                listen_ip="192.0.2.10",
                listen_port=9100,
                target_ip="192.0.2.20",
                target_port=9100,
                started_at=now,
                status="CLOSED",
            )
        )
        session.flush()
        session.add(
            PrintJob(
                id=job_id,
                device_id=device_id,
                session_id=session_id,
                source_key="printproxy:test:printer-a:job-a",
                source_system="printproxy",
                source_instance="test",
                source_scope="printer-a",
                source_job_id="job-a",
                source_schema="printproxy.state.v2",
                manifest_sha256=_sha("a"),
                manifest_path="synthetic/job-a.json",
                started_at=now,
                ended_at=now + timedelta(seconds=1),
                captured_at=now,
                status="READY",
                capture_complete=True,
                timeline_complete=False,
            )
        )
        session.flush()
        session.add_all(
            [
                ParserVersion(
                    id=parser_one_id,
                    name="escpos",
                    version="1.0.0",
                    build_sha256=_sha("b"),
                    protocol="escpos",
                ),
                ParserVersion(
                    id=parser_two_id,
                    name="escpos",
                    version="2.0.0",
                    build_sha256=_sha("c"),
                    protocol="escpos",
                ),
            ]
        )
        session.flush()
        session.add(
            Document(
                id=document_id,
                device_id=device_id,
                session_id=session_id,
                job_id=job_id,
                source_document_key="candidate-1",
                document_type="PRE_BILL",
                subtype="PRECONTO",
                captured_at=now,
            )
        )
        session.flush()
        session.add_all(
            [
                DocumentVersion(
                    document_id=document_id,
                    parser_version_id=parser_one_id,
                    version_sequence=1,
                    gross_total=Decimal("100.1234"),
                    status="COMPLETE",
                    normalized_text="prima versione",
                    parse_confidence=80,
                    evidence_level="INFERRED",
                    source_manifest_sha256=_sha("a"),
                    source_payload_sha256=_sha("d"),
                    source_path="synthetic/a.raw",
                    complete=True,
                    chain_scope=f"document:{job_id}",
                    chain_sequence=1,
                    previous_record_hash=_sha("0"),
                    record_hash=_sha("e"),
                ),
                DocumentVersion(
                    document_id=document_id,
                    parser_version_id=parser_two_id,
                    version_sequence=2,
                    gross_total=Decimal("100.1234"),
                    status="COMPLETE",
                    normalized_text="seconda versione",
                    parse_confidence=95,
                    evidence_level="CONFIRMED",
                    source_manifest_sha256=_sha("a"),
                    source_payload_sha256=_sha("d"),
                    source_path="synthetic/a.raw",
                    complete=True,
                    chain_scope=f"document:{job_id}",
                    chain_sequence=2,
                    previous_record_hash=_sha("e"),
                    record_hash=_sha("f"),
                ),
            ]
        )

    with factory() as session:
        stored_device = session.get(Device, device_id)
        versions = session.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version_sequence)
        ).all()
        assert stored_device is not None and stored_device.id == device_id
        assert len(versions) == 2
        assert versions[0].gross_total == Decimal("100.1234")
        assert versions[0].parsed_at.tzinfo is UTC
        assert versions[1].normalized_text == "seconda versione"


def test_initial_alembic_upgrade_and_downgrade(tmp_path: Path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "migration.sqlite"
    configuration = Config(root / "alembic.ini")
    configuration.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    monkeypatch.delenv("RPG_DATABASE_URL", raising=False)

    application_logger = logging.getLogger("retailprintguard.proxy")
    application_logger.disabled = False
    command.upgrade(configuration, "head")
    assert application_logger.disabled is False
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    assert set(inspect(engine).get_table_names()) >= REQUIRED_TABLES
    with engine.connect() as connection:
        # SQLite reflects BINARY(16) with NUMERIC affinity; compare structure
        # here and verify MariaDB physical types separately from compiled DDL.
        context = MigrationContext.configure(connection, opts={"compare_type": False})
        assert compare_metadata(context, Base.metadata) == []
    engine.dispose()

    command.downgrade(configuration, "base")
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    assert not (REQUIRED_TABLES & set(inspect(engine).get_table_names()))
    engine.dispose()
