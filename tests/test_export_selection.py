from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, func, inspect, null, select, update
from test_db_repository import NOW, _seed_api
from test_exports import exports as exports

from retailprintguard.api.exports import ExportCreate, ExportError, canonical_json, zip_timestamp
from retailprintguard.db import create_db_engine, session_factory
from retailprintguard.db.models import (
    Document,
    DocumentVersion,
    ExportItem,
    ExportRequest,
    PrintJob,
    RawPayload,
)
from retailprintguard.db.repository import SqlAlchemyApiRepository, _version_column, _version_value
from retailprintguard.render.eligibility import DOCUMENT_PDF_POLICY_VERSION


def _add_document(factory, ids, kind, body, *, subtype="SYNTHETIC", complete=True, timestamp=None):
    document_id = uuid4()
    with factory.begin() as session:
        source = session.scalar(select(DocumentVersion).where(
            DocumentVersion.document_id == ids["document"]
        ))
        copied = {
            column.name: getattr(source, column.name)
            for column in source.__table__.columns
            if column.name not in {"id", "document_id"}
        }
        document = Document(
            id=document_id, device_id=ids["device"], session_id=ids["session"], job_id=ids["job"],
            source_document_key=str(document_id), document_type=kind, subtype=subtype,
            captured_at=NOW, document_timestamp=timestamp,
        )
        session.add(document)
        session.flush()
        copied.update(
            document_type=kind, subtype=subtype, normalized_text=body, complete=complete,
            document_timestamp=timestamp, gross_total=None, net_total=None, tax_total=None,
            chain_scope=f"document:{document_id}", chain_sequence=1,
        )
        session.add(DocumentVersion(id=uuid4(), document_id=document_id, **copied))
    return document_id


def _selection_corpus(factory, ids):
    complete = {ids["document"]}
    partial = set()
    excluded = set()
    cases = [
        ("DEVICE_RESPONSE", "ON00000000", "RESPONSE", True, "excluded"),
        ("CANCELLATION", "RCH cancellation command observed",
         "RCH_DOCUMENT_CANCEL_COMMAND_OBSERVED", True, "excluded"),
        ("COMMERCIAL_DOCUMENT", "", "RCH_COMMERCIALE_INFERRED", False, "excluded"),
        ("KITCHEN_ORDER", "<ESC/POS:CUT>", "COMANDA_LITERAL", True, "excluded"),
        ("UNKNOWN", "Unclassified captured bytes", "SYNTHETIC", True, "excluded"),
        ("MANAGEMENT_DOCUMENT", "Scontrino: SF0001-0002 01/01/2042 12:30",
         "RCH_GESTIONALE_INFERRED", True, "excluded"),
        ("SHIFT_END_REPORT", "Report di fine turno\nPagamenti\nLibro cassa\nEURO\n----",
         "RCH_REPORT_FINE_TURNO_LITERAL", True, "excluded"),
        ("SHIFT_END_REPORT", "Report di fine turno\nContanti 0,00", "SYNTHETIC", True, "complete"),
        ("CANCELLATION", "ANNULLAMENTO\n1 x articolo", "ANNULLAMENTO_LITERAL", True, "complete"),
        ("KITCHEN_ORDER", "COMANDA\n1 x articolo", "COMANDA_LITERAL", True, "complete"),
        ("KITCHEN_ORDER", "COMANDA\n1 x articolo", "COMANDA_LITERAL", False, "partial"),
        ("COMMERCIAL_DOCUMENT", "Articolo 1,00", "RCH_COMMERCIALE_INFERRED", False, "partial"),
    ]
    for kind, body, subtype, closed, category in cases:
        key = _add_document(
            factory, ids, kind, body, subtype=subtype, complete=closed,
            timestamp=(
                NOW - timedelta(days=2)
                if kind == "SHIFT_END_REPORT" and category == "complete" else None
            ),
        )
        {"complete": complete, "partial": partial, "excluded": excluded}[category].add(key)
    with factory.begin() as session:
        # Complete, readable documents can occur in a partial capture. Review
        # PENDING is the normal default, not an instruction to omit their PDFs.
        job = session.get(PrintJob, ids["job"])
        job.status = "PARTIAL"
        job.capture_complete = False
        job.review_state = "PENDING"
    return complete, partial, excluded


@pytest.mark.parametrize("scope", ["complete", "partial"])
def test_export_separates_complete_partial_and_protocol_across_batches(exports, monkeypatch, scope):
    service, factory, ids, actor = exports
    complete, partial, excluded = _selection_corpus(factory, ids)
    expected = complete if scope == "complete" else partial
    monkeypatch.setattr("retailprintguard.api.exports.BATCH_SIZE", 1)
    with factory() as session:
        source_count = session.scalar(select(func.count()).select_from(DocumentVersion))
        raw_before = session.scalar(select(RawPayload.sha256))
    result = service.create(ExportCreate(scope="all_history", document_scope=scope), actor)
    assert result.date_from is None and result.date_to is None
    assert result.scope == "all_history" and result.document_scope == scope
    assert result.total_documents == len(expected)
    metadata = result.selection_metadata
    assert metadata["policy_version"] == DOCUMENT_PDF_POLICY_VERSION
    assert metadata["category_counts"] == {"complete": 4, "partial": 2, "excluded": 7}
    assert metadata["scanned_documents"] == len(complete | partial | excluded)
    assert metadata["excluded_documents"] == 13 - len(expected)
    assert metadata["exclusion_counts"]["OTHER_DOCUMENT_SCOPE"] == (2 if scope == "complete" else 4)
    assert "exclusion_records" not in metadata
    assert "1900" not in result.archive_filename
    assert ("completi" if scope == "complete" else "parziali") in result.archive_filename
    assert metadata["coverage_to"] == NOW.date().isoformat()
    assert metadata["document_date_count"] == (1 if scope == "complete" else 0)
    assert metadata["capture_date_count"] == len(expected) - metadata["document_date_count"]
    with factory() as session:
        items = session.scalars(select(ExportItem).where(ExportItem.export_id == result.id)
                                .order_by(ExportItem.sequence)).all()
        assert {item.document_id for item in items} == expected
        assert [item.sequence for item in items] == list(range(1, len(expected) + 1))
    assert service.run_one()
    _, stream = service.open_archive(result.id)
    with stream, ZipFile(stream) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema"] == "retailprintguard.accounting-export.v2"
        assert len(manifest["selection_metadata"]["exclusion_records"]) == 13 - len(expected)
        assert {entry["document_id"] for entry in manifest["documents"]} == {
            str(key) for key in expected
        }
        assert all(entry["filename"].endswith("_parziale.pdf") == (scope == "partial")
                   for entry in manifest["documents"])
        for entry in manifest["documents"]:
            timestamp = datetime.fromisoformat(entry["document_timestamp"] or entry["captured_at"])
            assert archive.getinfo(entry["filename"]).date_time == zip_timestamp(timestamp)
            assert entry["zip_timestamp_clamped"] is False
        created = datetime.fromisoformat(manifest["created_at"])
        assert archive.getinfo("manifest.json").date_time == zip_timestamp(created)
        assert archive.getinfo("manifest.sha256").date_time == zip_timestamp(created)
        assert manifest["zip_timestamp_policy"]["timezone"] == "Europe/Rome"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == source_count
        assert session.scalar(select(RawPayload.sha256)) == raw_before


def test_list_policy_counts_and_pagination_only_hydrate_selected_page(exports, monkeypatch):
    _, factory, ids, _ = exports
    complete, partial, excluded = _selection_corpus(factory, ids)
    repository = SqlAlchemyApiRepository(factory)
    original = repository._document_views
    hydrated = []

    def observe(session, documents, **kwargs):
        hydrated.append(len(documents))
        return original(session, documents, **kwargs)

    monkeypatch.setattr(repository, "_document_views", observe)
    seen = set()
    for offset in range(len(complete)):
        items, total = repository.list_documents(
            limit=1, offset=offset, filters={"document_scope": "complete"}
        )
        assert total == len(complete) and len(items) == 1
        assert items[0].pdf_category == "complete"
        seen.add(items[0].id)
    assert seen == complete
    assert hydrated == [1] * len(complete)
    items, total = repository.list_documents(
        limit=20, offset=0, filters={"document_scope": "partial"}
    )
    assert total == len(partial) and {item.id for item in items} == partial
    items, total = repository.list_documents(
        limit=20, offset=0, filters={"document_scope": "all", "include_technical": True}
    )
    assert total == len(complete | partial | excluded)
    assert {item.id for item in items} == complete | partial | excluded


@pytest.mark.parametrize("header", [None, JSON.NULL, {"merchant_name": "LAB"}])
def test_legacy_semantic_fallback_agrees_for_sql_null_json_null_and_real_header(exports, header):
    service, factory, ids, actor = exports
    with factory.begin() as session:
        session.execute(update(DocumentVersion).where(
            DocumentVersion.document_id == ids["document"]
        ).values(receipt_header=null() if header is None else header))
    with factory() as session:
        document = session.get(Document, ids["document"])
        version = session.scalar(select(DocumentVersion))
        sql_kind = session.scalar(select(
            _version_column(DocumentVersion.document_type, Document.document_type)
        ).select_from(Document).join(DocumentVersion))
        python_kind = _version_value(version, document, "document_type")
        assert sql_kind == python_kind
        assert sql_kind == (None if isinstance(header, dict) else "PRE_BILL")
    result = service.create(ExportCreate(scope="all_history"), actor)
    assert result.total_documents == (0 if isinstance(header, dict) else 1)


def test_scope_validation_and_empty_archive_coverage(exports):
    service, _, _, actor = exports
    for values in ({}, {"date_from": NOW.date()}, {"scope": "all_history", "date_to": NOW.date()}):
        with pytest.raises(ValueError):
            ExportCreate(**values)
    with pytest.raises(ValueError):
        ExportCreate(scope="all_history", document_scope="excluded")
    result = service.create(ExportCreate(scope="all_history", document_scope="partial"), actor)
    assert result.total_documents == 0
    assert result.selection_metadata["coverage_from"] is None
    assert result.selection_metadata["coverage_to"] is None
    assert "vuoto" in result.archive_filename
    assert service.run_one()
    _, stream = service.open_archive(result.id)
    with stream, ZipFile(stream) as archive:
        assert not [name for name in archive.namelist() if name.endswith(".pdf")]


def test_policy_version_change_does_not_silently_reselect_queued_request(exports, monkeypatch):
    service, _, _, actor = exports
    result = service.create(ExportCreate(scope="all_history"), actor)
    monkeypatch.setattr("retailprintguard.api.exports.DOCUMENT_PDF_POLICY_VERSION", "future-policy")
    assert service.run_one()
    assert service.get(result.id).status == "failed"
    assert "Criteri di selezione cambiati" in service.get(result.id).error
    with pytest.raises(ExportError):
        service.open_archive(result.id)


def test_limits_apply_to_selected_documents_and_exclusion_audit_is_bounded(exports, monkeypatch):
    service, factory, ids, actor = exports
    for _ in range(2):
        _add_document(factory, ids, "DEVICE_RESPONSE", "ON00000000")
    monkeypatch.setattr("retailprintguard.api.exports.MAX_DOCUMENTS", 1)
    monkeypatch.setattr("retailprintguard.api.exports.MAX_EXCLUSION_RECORDS", 1)
    result = service.create(ExportCreate(scope="all_history"), actor)
    assert result.total_documents == 1
    assert result.selection_metadata["excluded_documents"] == 2
    assert result.selection_metadata["exclusion_counts"] == {"DEVICE_RESPONSE": 2}
    assert result.selection_metadata["exclusion_records_truncated"] == 1
    with factory() as session:
        metadata = session.get(ExportRequest, result.id).selection_metadata
        assert len(metadata["exclusion_records"]) == 1
    monkeypatch.setattr("retailprintguard.api.exports.MAX_SCAN_DOCUMENTS", 2)
    with pytest.raises(ExportError, match="esamina 3 record"):
        service.create(ExportCreate(scope="all_history"), actor)


def test_legacy_manifest_recovery_never_applies_new_selection_policy(exports, monkeypatch):
    service, factory, _, actor = exports
    result = service.create(ExportCreate(date_from=NOW.date(), date_to=NOW.date()), actor)
    with factory.begin() as session:
        row = session.get(ExportRequest, result.id)
        row.selection_metadata = None
        row.date_from = date(1900, 1, 1)
    assert service.run_one()
    original, stream = service.open_archive(result.id)
    with stream:
        original_bytes = stream.read()
    assert original.document_scope == "legacy_all"
    assert original.selection_metadata is None
    assert "1900" not in original.archive_filename
    with ZipFile(service.directory / f"{result.id}.zip") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema"] == "retailprintguard.accounting-export.v1"
        assert manifest["date_from"] == "1900-01-01"
        assert "selection_metadata" not in manifest
        assert "zip_timestamp_policy" not in manifest
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())
    with factory.begin() as session:
        row = session.get(ExportRequest, result.id)
        row.status = "running"
        row.archive_sha256 = None
        row.completed_at = None
    monkeypatch.setattr("retailprintguard.api.exports.DOCUMENT_PDF_POLICY_VERSION", "future-policy")
    service.recover()
    assert service.run_one()
    rebuilt, stream = service.open_archive(result.id)
    with stream:
        assert stream.read() == original_bytes
    assert rebuilt.archive_sha256 == original.archive_sha256


@pytest.mark.parametrize("year, expected", [(1900, (1980, 1, 1, 0, 0, 0)),
                                           (2200, (2107, 12, 31, 23, 59, 58))])
def test_zip_year_limits_are_declared_without_changing_document_dates(exports, year, expected):
    service, factory, ids, actor = exports
    timestamp = datetime(year, 7, 8, 12, 30, 59, tzinfo=UTC)
    with factory.begin() as session:
        session.get(Document, ids["document"]).document_timestamp = timestamp
    result = service.create(ExportCreate(scope="all_history"), actor)
    assert result.selection_metadata["coverage_from"] == f"{year}-07-08"
    assert service.run_one()
    _, stream = service.open_archive(result.id)
    with stream, ZipFile(stream) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        entry = manifest["documents"][0]
        assert entry["document_timestamp"] == timestamp.isoformat()
        assert f"{year}-07-08" in entry["filename"]
        assert entry["zip_timestamp_clamped"] is True
        assert archive.getinfo(entry["filename"]).date_time == expected


def test_zip_timestamp_uses_rome_and_rounds_seconds_deterministically():
    assert zip_timestamp(datetime(2042, 7, 8, 23, 59, 59, 9000, tzinfo=UTC)) == (
        2042, 7, 9, 1, 59, 58
    )
    assert zip_timestamp(datetime(2042, 1, 8, 23, 59, 59)) == (2042, 1, 9, 0, 59, 58)


def test_unrepresentable_local_date_fails_explicitly_and_rolls_back_request(exports):
    service, factory, ids, actor = exports
    timestamp = datetime.max.replace(tzinfo=UTC)
    with pytest.raises(ExportError, match="Data non rappresentabile"):
        zip_timestamp(timestamp)
    with factory.begin() as session:
        session.get(Document, ids["document"]).document_timestamp = timestamp
    with pytest.raises(ExportError, match="Data non rappresentabile"):
        service.create(ExportCreate(scope="all_history"), actor)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExportRequest)) == 0
        assert session.scalar(select(func.count()).select_from(ExportItem)) == 0


def test_selection_migration_preserves_legacy_snapshots_and_guards_downgrade(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "selection-migration.sqlite"
    configuration = Config(root / "alembic.ini")
    configuration.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    monkeypatch.delenv("RPG_DATABASE_URL", raising=False)
    command.upgrade(configuration, "12c4f6a8b0d2")
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    factory = session_factory(engine)
    ids = _seed_api(factory)
    request_id = uuid4()
    snapshot = {"synthetic_legacy_evidence": "unchanged", "captured_at": NOW.isoformat()}
    checksum = hashlib.sha256(canonical_json(snapshot)).hexdigest()
    archive = tmp_path / "legacy.zip"
    with ZipFile(archive, "w") as output:
        output.writestr("legacy.pdf", b"synthetic previous derivative")
    archive_bytes = archive.read_bytes()
    with factory.begin() as session:
        version_id = session.scalar(select(DocumentVersion.id))
        session.execute(ExportRequest.__table__.insert().values(
            id=request_id, requested_by_user_id=ids["user"], requested_by="auditor",
            date_from=date(1900, 1, 1), date_to=NOW.date(), timezone="Europe/Rome", device_ids=[],
            status="completed", created_at=NOW, total_documents=1, completed_documents=1,
            renderer_version="2.0.0", archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            archive_bytes=len(archive_bytes),
        ))
        session.add(ExportItem(
            export_id=request_id, sequence=1, document_id=ids["document"],
            document_version_id=version_id, snapshot=snapshot, snapshot_sha256=checksum,
            date_basis="captured_at",
        ))
    engine.dispose()
    command.upgrade(configuration, "head")
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    factory = session_factory(engine)
    columns = {column["name"]: column for column in inspect(engine).get_columns("export_requests")}
    assert columns["date_from"]["nullable"] and columns["date_to"]["nullable"]
    with factory() as session:
        row = session.get(ExportRequest, request_id)
        assert row.selection_metadata is None and row.date_from == date(1900, 1, 1)
        assert row.archive_sha256 == hashlib.sha256(archive_bytes).hexdigest()
        item = session.get(ExportItem, (request_id, 1))
        assert item.snapshot == snapshot and item.snapshot_sha256 == checksum
    assert archive.read_bytes() == archive_bytes
    with factory.begin() as session:
        row = session.get(ExportRequest, request_id)
        row.selection_metadata = {"synthetic_new_selection": True}
    engine.dispose()
    with pytest.raises(RuntimeError, match="selection history"):
        command.downgrade(configuration, "12c4f6a8b0d2")
