from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from retailprintguard.db import Base, create_db_engine, session_factory, transaction
from retailprintguard.db.models import (
    Device,
    Document,
    DocumentVersion,
    FraudAlert,
    LinePriceAttribution,
    ParserVersion,
    PrintJob,
    ProxySession,
    RawPayload,
)
from retailprintguard.db.types import UUIDBinary

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
    "line_price_attributions",
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
    alert_ddl = str(CreateTable(FraudAlert.__table__).compile(dialect=mysql.dialect()))
    version_ddl = str(CreateTable(DocumentVersion.__table__).compile(dialect=mysql.dialect()))
    raw_ddl = str(CreateTable(RawPayload.__table__).compile(dialect=mysql.dialect()))
    price_ddl = str(
        CreateTable(LinePriceAttribution.__table__).compile(dialect=mysql.dialect())
    )
    job_ddl = str(CreateTable(PrintJob.__table__).compile(dialect=mysql.dialect()))
    assert "BINARY(16)" in device_ddl
    assert "ENGINE=InnoDB" in device_ddl
    assert "CHARSET=utf8mb4" in device_ddl
    assert "mac_address VARCHAR(17)" in device_ddl
    assert "duplicate_of_alert_id BINARY(16)" in alert_ddl
    assert "FOREIGN KEY(duplicate_of_alert_id)" in alert_ddl
    assert "canonical_duplicate_consistency" in alert_ddl
    assert "DECIMAL(19, 4)" in version_ddl
    assert "LONGBLOB" in raw_ddl
    assert "DECIMAL(19, 4)" in price_ddl
    assert "FOREIGN KEY(target_line_id)" in price_ddl
    assert "FOREIGN KEY(source_line_id)" in price_ddl
    assert "ON DELETE RESTRICT" in price_ddl
    assert "review_state VARCHAR(32) NOT NULL" in job_ddl
    assert "analysis_excluded BOOL NOT NULL" in job_ddl
    assert "FOREIGN KEY(reviewed_by_user_id)" in job_ddl
    assert "print_jobs_review_state" in job_ddl


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


def test_alert_duplicate_migration_preserves_and_links_historical_rows(
    tmp_path: Path, monkeypatch
) -> None:
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "alert-deduplication.sqlite"
    configuration = Config(root / "alembic.ini")
    configuration.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    monkeypatch.delenv("RPG_DATABASE_URL", raising=False)
    command.upgrade(configuration, "29517f373309")

    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    metadata = Base.metadata.__class__()
    fraud_rules = Base.metadata.tables["fraud_rules"].to_metadata(metadata)
    fraud_rule_versions = Base.metadata.tables["fraud_rule_versions"].to_metadata(metadata)
    legacy_alerts = FraudAlert.__table__.to_metadata(metadata)
    for column_name in (
        "is_canonical",
        "duplicate_of_alert_id",
        "deduplicated_at",
        "deduplication_reason",
    ):
        legacy_alerts._columns.remove(legacy_alerts.c[column_name])
    target_rule_id, other_rule_id = uuid4(), uuid4()
    target_version_id, other_version_id = uuid4(), uuid4()
    transaction_id, singleton_transaction_id, other_transaction_id = uuid4(), uuid4(), uuid4()
    canonical_id, duplicate_id = uuid4(), uuid4()
    singleton_id, other_one_id, other_two_id = uuid4(), uuid4(), uuid4()
    now = datetime(2042, 5, 6, 12, 30, tzinfo=UTC)

    with engine.begin() as connection:
        connection.execute(
            fraud_rules.insert(),
            [
                {
                    "id": target_rule_id,
                    "code": "ORDER_WITHOUT_FISCAL_CLOSE",
                    "name": "Regola sintetica",
                    "description": "Dati sintetici per test migrazione",
                    "enabled": True,
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "id": other_rule_id,
                    "code": "SYNTHETIC_UNAFFECTED_RULE",
                    "name": "Regola non interessata",
                    "description": "Dati sintetici per test migrazione",
                    "enabled": True,
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )
        connection.execute(
            fraud_rule_versions.insert(),
            [
                {
                    "id": target_version_id,
                    "fraud_rule_id": target_rule_id,
                    "version": 1,
                    "implementation_version": "test",
                    "configuration_fingerprint": _sha("1"),
                    "enabled": True,
                    "severity": "HIGH",
                    "threshold": None,
                    "weight": Decimal("1.0000"),
                    "configuration": {},
                    "effective_from": now,
                    "effective_until": None,
                    "created_by_user_id": None,
                    "created_at": now,
                },
                {
                    "id": other_version_id,
                    "fraud_rule_id": other_rule_id,
                    "version": 1,
                    "implementation_version": "test",
                    "configuration_fingerprint": _sha("2"),
                    "enabled": True,
                    "severity": "LOW",
                    "threshold": None,
                    "weight": Decimal("1.0000"),
                    "configuration": {},
                    "effective_from": now,
                    "effective_until": None,
                    "created_by_user_id": None,
                    "created_at": now,
                },
            ],
        )

        def alert_values(
            alert_id,
            version_id,
            alert_transaction_id,
            finding_character: str,
            opened_at: datetime,
        ) -> dict[str, object]:
            return {
                "id": alert_id,
                "fraud_rule_version_id": version_id,
                "correlation_id": None,
                "transaction_id": alert_transaction_id,
                "finding_key": _sha(finding_character),
                "severity": "HIGH",
                "score": 80,
                "status": "OPEN",
                "description": "Alert sintetico",
                "explanation": "Evidenza sintetica",
                "original_amount": None,
                "final_amount": None,
                "difference_amount": None,
                "difference_percent": None,
                "confidence": 80,
                "assigned_to_user_id": None,
                "taken_at": None,
                "closed_at": None,
                "closure_reason": None,
                "opened_at": opened_at,
                "updated_at": opened_at,
            }

        connection.execute(
            legacy_alerts.insert(),
            [
                alert_values(canonical_id, target_version_id, transaction_id, "a", now),
                alert_values(
                    duplicate_id,
                    target_version_id,
                    transaction_id,
                    "b",
                    now + timedelta(seconds=1),
                ),
                alert_values(
                    singleton_id,
                    target_version_id,
                    singleton_transaction_id,
                    "c",
                    now,
                ),
                alert_values(other_one_id, other_version_id, other_transaction_id, "d", now),
                alert_values(
                    other_two_id,
                    other_version_id,
                    other_transaction_id,
                    "e",
                    now + timedelta(seconds=1),
                ),
            ],
        )
    engine.dispose()

    command.upgrade(configuration, "head")
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    factory = session_factory(engine)
    with factory() as session:
        canonical = session.get(FraudAlert, canonical_id)
        duplicate = session.get(FraudAlert, duplicate_id)
        singleton = session.get(FraudAlert, singleton_id)
        unaffected = [session.get(FraudAlert, other_one_id), session.get(FraudAlert, other_two_id)]

        assert canonical is not None and canonical.is_canonical is True
        assert canonical.duplicate_of_alert_id is None
        assert duplicate is not None and duplicate.is_canonical is False
        assert duplicate.duplicate_of_alert_id == canonical_id
        assert duplicate.deduplicated_at is not None
        assert duplicate.deduplication_reason is not None
        assert "historical duplicate" in duplicate.deduplication_reason
        assert singleton is not None and singleton.is_canonical is True
        assert all(alert is not None and alert.is_canonical for alert in unaffected)

    assert {
        "ix_fraud_alerts_operational_status_opened",
        "ix_fraud_alerts_duplicate_of",
    } <= {index["name"] for index in inspect(engine).get_indexes("fraud_alerts")}
    engine.dispose()

    command.downgrade(configuration, "29517f373309")
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    assert "is_canonical" not in {
        column["name"] for column in inspect(engine).get_columns("fraud_alerts")
    }
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM fraud_alerts").scalar_one() == 5
    engine.dispose()

    command.downgrade(configuration, "base")
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    assert not (REQUIRED_TABLES & set(inspect(engine).get_table_names()))
    engine.dispose()


def test_document_version_semantics_migration_backfills_legacy_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "version-semantics.sqlite"
    configuration = Config(root / "alembic.ini")
    configuration.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    monkeypatch.delenv("RPG_DATABASE_URL", raising=False)
    command.upgrade(configuration, "8d4c2a91f7b0")

    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    metadata = MetaData()
    devices = Table("devices", metadata, autoload_with=engine)
    sessions = Table("proxy_sessions", metadata, autoload_with=engine)
    jobs = Table("print_jobs", metadata, autoload_with=engine)
    parsers = Table("parser_versions", metadata, autoload_with=engine)
    documents = Table("documents", metadata, autoload_with=engine)
    versions = Table("document_versions", metadata, autoload_with=engine)
    for table, columns in (
        (devices, ("id",)),
        (sessions, ("id", "device_id")),
        (jobs, ("id", "device_id", "session_id")),
        (parsers, ("id",)),
        (documents, ("id", "device_id", "session_id", "job_id")),
        (
            versions,
            ("id", "document_id", "parser_version_id", "raw_payload_id", "parse_run_id"),
        ),
    ):
        for column_name in columns:
            table.c[column_name].type = UUIDBinary()
    now = datetime(2042, 5, 6, 12, 30)
    device_id, session_id, job_id = uuid4(), uuid4(), uuid4()
    parser_id, document_id, version_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            devices.insert(),
            {
                "id": device_id,
                "external_id": "pos_legacy",
                "name": "POS legacy",
                "device_type": "pos",
                "parser_kind": "escpos",
                "enabled": True,
                "bidirectional": False,
                "listen_ip": "192.0.2.10",
                "listen_port": 9100,
                "target_ip": "192.0.2.20",
                "target_port": 9100,
                "non_sensitive_config": {},
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            sessions.insert(),
            {
                "id": session_id,
                "device_id": device_id,
                "source_system": "test",
                "source_instance": "migration",
                "source_scope": "legacy",
                "source_session_id": "legacy-session",
                "listen_ip": "192.0.2.10",
                "listen_port": 9100,
                "target_ip": "192.0.2.20",
                "target_port": 9100,
                "started_at": now,
                "status": "CLOSED",
                "client_fin_received": True,
                "device_fin_received": True,
                "bytes_to_device": 10,
                "bytes_to_client": 0,
                "capture_complete": True,
            },
        )
        connection.execute(
            jobs.insert(),
            {
                "id": job_id,
                "device_id": device_id,
                "session_id": session_id,
                "source_key": "test:migration:legacy:job",
                "source_system": "test",
                "source_instance": "migration",
                "source_scope": "legacy",
                "source_job_id": "legacy-job",
                "source_schema": "legacy.v1",
                "manifest_sha256": _sha("a"),
                "manifest_path": "/legacy/manifest.json",
                "started_at": now,
                "captured_at": now,
                "status": "COMPLETE",
                "capture_complete": True,
                "timeline_complete": True,
                "import_status": "PARSED",
                "warnings": [],
                "errors": [],
            },
        )
        connection.execute(
            parsers.insert(),
            {
                "id": parser_id,
                "name": "legacy-parser",
                "version": "1.0.0",
                "build_sha256": _sha("b"),
                "protocol": "escpos",
                "configuration": {},
                "installed_at": now,
            },
        )
        connection.execute(
            documents.insert(),
            {
                "id": document_id,
                "device_id": device_id,
                "session_id": session_id,
                "job_id": job_id,
                "source_document_key": "legacy-document",
                "document_type": "KITCHEN_ORDER",
                "subtype": "TICKET_POS_INFERRED",
                "external_document_code": "DOC-LEGACY",
                "order_code": "ORDER-LEGACY",
                "table_code": "LAB-25",
                "operator_code": "OP-LEGACY",
                "terminal_code": "TERM-LEGACY",
                "document_timestamp": now,
                "captured_at": now,
                "created_at": now,
            },
        )
        connection.execute(
            versions.insert(),
            {
                "id": version_id,
                "document_id": document_id,
                "parser_version_id": parser_id,
                "parse_run_id": uuid4(),
                "version_sequence": 1,
                "parsed_at": now,
                "status": "COMPLETE",
                "normalized_text": "legacy",
                "parse_confidence": 80,
                "evidence_level": "INFERRED",
                "source_manifest_sha256": _sha("a"),
                "source_payload_sha256": _sha("c"),
                "source_path": "/legacy/client.raw",
                "complete": True,
                "warnings": [],
                "errors": [],
                "raw_metadata": {},
                "chain_scope": f"document:{document_id}",
                "chain_sequence": 1,
                "previous_record_hash": _sha("0"),
                "record_hash": _sha("d"),
            },
        )
    engine.dispose()

    command.upgrade(configuration, "head")
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    factory = session_factory(engine)
    with factory() as session:
        version = session.get(DocumentVersion, version_id)
        assert version is not None
        assert version.document_type == "KITCHEN_ORDER"
        assert version.subtype == "TICKET_POS_INFERRED"
        assert version.external_document_code == "DOC-LEGACY"
        assert version.order_code == "ORDER-LEGACY"
        assert version.table_code == "LAB-25"
        assert version.operator_code == "OP-LEGACY"
        assert version.terminal_code == "TERM-LEGACY"
        assert version.document_timestamp == datetime(2042, 5, 6, 12, 30, tzinfo=UTC)
    assert {
        "ix_document_versions_order_document",
        "ix_document_versions_external_document",
        "ix_document_versions_table_document",
    } <= {index["name"] for index in inspect(engine).get_indexes("document_versions")}
    assert "course_code" in {
        column["name"] for column in inspect(engine).get_columns("document_lines")
    }
    engine.dispose()


def test_mariadb_offline_migration_ddl_is_renderable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del tmp_path
    root = Path(__file__).resolve().parents[1]
    output = StringIO()
    configuration = Config(root / "alembic.ini", output_buffer=output)
    configuration.set_main_option(
        "sqlalchemy.url",
        "mysql+pymysql://synthetic:synthetic@127.0.0.1/synthetic",
    )
    monkeypatch.delenv("RPG_DATABASE_URL", raising=False)

    command.upgrade(configuration, "head", sql=True)

    ddl = output.getvalue()
    assert "ALTER TABLE devices ADD COLUMN mac_address VARCHAR(17)" in ddl
    assert "ALTER TABLE fraud_alerts ADD COLUMN is_canonical BOOL" in ddl
    assert "FOREIGN KEY(duplicate_of_alert_id) REFERENCES fraud_alerts (id)" in ddl
    assert "ix_fraud_alerts_operational_status_opened" in ddl
    assert "ALTER TABLE document_versions ADD COLUMN document_type VARCHAR(48)" in ddl
    assert "UPDATE document_versions SET" in ddl
    assert "ix_document_versions_order_document" in ddl
    assert "ALTER TABLE document_lines ADD COLUMN course_code VARCHAR(64)" in ddl
    assert "CREATE TABLE line_price_attributions" in ddl
    assert "uq_line_price_attr_identity" in ddl
