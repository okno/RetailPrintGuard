from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_db_repository import NOW, _seed_api

from retailprintguard.api.auth import TokenService
from retailprintguard.api.exports import (
    ExportCreate,
    ExportError,
    ExportService,
    canonical_json,
    utc_bounds,
)
from retailprintguard.api.main import create_app
from retailprintguard.api.schemas import DocumentView, RoleName, UserPrincipal
from retailprintguard.db import Base, create_db_engine, session_factory
from retailprintguard.db.models import (
    Document,
    DocumentVersion,
    ExportItem,
    ExportRequest,
    RawPayload,
)
from retailprintguard.db.repository import SqlAlchemyApiRepository
from retailprintguard.render.naming import archive_pdf_filename
from retailprintguard.render.pdf import render_document_pdf


@pytest.fixture
def exports(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'exports.db'}")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    ids = _seed_api(factory)
    service = ExportService(factory, tmp_path / "archive")
    actor = UserPrincipal(id=ids["user"], username="auditor", roles=(RoleName.AUDITOR,))
    yield service, factory, ids, actor
    engine.dispose()


def request_body(**kwargs):
    return ExportCreate(date_from=NOW.date(), date_to=NOW.date(), **kwargs)


def test_calendar_days_include_dst_and_end_date():
    start, end = utc_bounds(ExportCreate(date_from=date(2026, 3, 29), date_to=date(2026, 3, 29)))
    assert start == datetime(2026, 3, 28, 23, tzinfo=UTC)
    assert end - start == timedelta(hours=23)
    start, end = utc_bounds(ExportCreate(date_from=date(2026, 10, 25), date_to=date(2026, 10, 25)))
    assert end - start == timedelta(hours=25)
    with pytest.raises(ValueError):
        ExportCreate(date_from=date(2026, 2, 2), date_to=date(2026, 2, 1))


def test_snapshot_archive_checksums_and_preserved_source(exports):
    service, factory, ids, actor = exports
    result = service.create(request_body(device_ids=["pos_1"]), actor)
    assert result.total_documents == 1
    assert result.status == "queued"
    with factory() as session:
        item = session.get(ExportItem, (result.id, 1))
        original_document = DocumentView.model_validate(item.snapshot)
        original_pdf = render_document_pdf(original_document)
        assert item.snapshot["receipt_preview"] == original_document.receipt_preview
        assert item.snapshot["pdf_filename"] == original_document.pdf_filename
        assert original_document.model_dump(mode="json") == item.snapshot
        raw_count = session.scalar(select(func.count()).select_from(RawPayload))
    # Later parser/projection edits must not alter the requested export.
    with factory.begin() as session:
        version = session.scalar(select(DocumentVersion))
        version.normalized_text = "LATER INTERPRETATION"
        document = session.get(Document, ids["document"])
        document.document_timestamp = NOW + timedelta(days=1)
    assert service.run_one()
    completed, stream = service.open_archive(result.id)
    with stream, ZipFile(stream) as archive:
        manifest_bytes = archive.read("manifest.json")
        manifest = json.loads(manifest_bytes)
        entry = manifest["documents"][0]
        assert entry["document_id"] == str(ids["document"])
        assert entry["archive_sequence"] == 1
        assert entry["filename"] == "documents/" + archive_pdf_filename(original_document, 1)
        assert archive.read(entry["filename"]) == original_pdf
        assert hashlib.sha256(original_pdf).hexdigest() == entry["pdf_sha256"]
        assert (
            archive.read("manifest.sha256")
            .decode()
            .startswith(hashlib.sha256(manifest_bytes).hexdigest())
        )
        assert len(archive.namelist()) == 3
    assert completed.completed_documents == completed.total_documents == 1
    assert completed.download_url.endswith("/download")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(RawPayload)) == raw_count
        assert session.get(Document, ids["document"]) is not None
    assert not service.run_one()


@pytest.mark.parametrize("include_projections", [False, True])
def test_legacy_snapshots_and_computed_projections_do_not_change_evidence(
    exports, include_projections
):
    service, factory, _, actor = exports
    result = service.create(request_body(), actor)
    with factory.begin() as session:
        item = session.get(ExportItem, (result.id, 1))
        snapshot = dict(item.snapshot)
        expected = DocumentView.model_validate(snapshot)
        if include_projections:
            snapshot.update(receipt_preview="UNTRUSTED EXTRA", pdf_filename="../../escape.pdf")
        else:
            snapshot.pop("receipt_preview")
            snapshot.pop("pdf_filename")
        original_bytes = canonical_json(snapshot)
        checksum = hashlib.sha256(original_bytes).hexdigest()
        item.snapshot = snapshot
        item.snapshot_sha256 = checksum
        reconstructed = DocumentView.model_validate(snapshot)
        assert reconstructed.receipt_preview == expected.receipt_preview
        assert reconstructed.pdf_filename == expected.pdf_filename
        assert canonical_json(snapshot) == original_bytes
    assert service.run_one()
    _, stream = service.open_archive(result.id)
    with stream, ZipFile(stream) as archive:
        entry = json.loads(archive.read("manifest.json"))["documents"][0]
        assert entry["snapshot_sha256"] == checksum
        assert entry["filename"] == "documents/" + archive_pdf_filename(expected, 1)
        assert archive.read(entry["filename"]) == render_document_pdf(expected)
    with factory() as session:
        item = session.get(ExportItem, (result.id, 1))
        assert canonical_json(item.snapshot) == original_bytes
        assert item.snapshot_sha256 == checksum


def test_four_completed_archives_remain_downloadable_across_renderer_upgrade(
    exports, monkeypatch
):
    service, factory, _, actor = exports
    originals = {}
    for _ in range(4):
        request = service.create(request_body(), actor)
        assert service.run_one()
        view, stream = service.open_archive(request.id)
        with stream:
            originals[request.id] = (stream.read(), view.archive_sha256)
    with factory.begin() as session:
        for request_id in originals:
            session.get(ExportRequest, request_id).renderer_version = "1.4.1"

    def forbidden_render(_):
        pytest.fail("Completed archives must never be regenerated")

    monkeypatch.setattr("retailprintguard.api.exports.render_document_pdf", forbidden_render)
    history, total = service.history(limit=10, offset=0)
    assert total == 4
    assert {item.id for item in history} == set(originals)
    for request_id, (original, checksum) in originals.items():
        view, stream = service.open_archive(request_id)
        with stream:
            assert stream.read() == original
        assert view.status == "completed"
        assert view.archive_sha256 == checksum == hashlib.sha256(original).hexdigest()
    assert not service.run_one()


def test_timestamp_precedence_and_fallback(exports):
    service, factory, ids, actor = exports
    with factory.begin() as session:
        doc = session.get(Document, ids["document"])
        doc.document_timestamp = NOW + timedelta(days=1)
    empty = service.create(request_body(), actor)
    assert empty.total_documents == 0
    selected = service.create(
        ExportCreate(
            date_from=NOW.date() + timedelta(days=1), date_to=NOW.date() + timedelta(days=1)
        ),
        actor,
    )
    assert selected.total_documents == 1
    with factory() as session:
        assert session.get(ExportItem, (selected.id, 1)).date_basis == "document_timestamp"
    assert service.run_one()
    view, stream = service.open_archive(empty.id)
    with stream, ZipFile(stream) as archive:
        assert json.loads(archive.read("manifest.json"))["documents"] == []
    assert view.total_documents == 0


def test_restart_after_publication_is_idempotent(exports):
    service, factory, _, actor = exports
    result = service.create(request_body(), actor)
    assert service.run_one()
    original = service.get(result.id)
    with factory.begin() as session:
        # Simulate publication followed by a process crash before status commit.
        stored = session.get(ExportRequest, result.id)
        stored.status = "running"
        stored.archive_sha256 = None
        stored.completed_at = None
    service.recover()
    assert service.get(result.id).status == "queued"
    assert service.run_one()
    final = service.get(result.id)
    assert final.status == "completed"
    assert final.archive_sha256 == original.archive_sha256
    assert final.archive_bytes == original.archive_bytes


def test_crash_during_build_recovers_from_frozen_snapshot(exports, monkeypatch):
    service, _, _, actor = exports
    result = service.create(request_body(), actor)
    original = service._build
    monkeypatch.setattr(service, "_build", lambda _: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        service.run_one()
    assert service.get(result.id).status == "running"
    service.recover()
    monkeypatch.setattr(service, "_build", original)
    assert service.run_one()
    assert service.get(result.id).status == "completed"


def test_renderer_failure_never_publishes_partial_archive(exports, monkeypatch):
    service, _, _, actor = exports
    result = service.create(request_body(), actor)

    def broken(_):
        raise ValueError("sensitive source/path not for user")

    monkeypatch.setattr("retailprintguard.api.exports.render_document_pdf", broken)
    assert service.run_one()
    failed = service.get(result.id)
    assert failed.status == "failed"
    assert "ValueError" in failed.error
    assert "sensitive" not in failed.error
    assert not (service.directory / f"{result.id}.zip").exists()
    with pytest.raises(ExportError, match="non ancora"):
        service.open_archive(result.id)


def test_corrupt_snapshot_and_archive_fail_closed(exports):
    service, factory, _, actor = exports
    first = service.create(request_body(), actor)
    with factory.begin() as session:
        session.get(ExportItem, (first.id, 1)).snapshot_sha256 = "0" * 64
    assert service.run_one()
    assert service.get(first.id).status == "failed"
    second = service.create(request_body(), actor)
    assert service.run_one()
    path = service.directory / f"{second.id}.zip"
    path.chmod(0o640)
    path.write_bytes(b"corrupt")
    with pytest.raises(ExportError, match="Integrità"):
        service.open_archive(second.id)


def test_limits_unknown_devices_and_membership_rollback(exports, monkeypatch):
    service, factory, _, actor = exports
    with pytest.raises(ExportError, match="non esistono"):
        service.create(request_body(device_ids=["missing"]), actor)
    monkeypatch.setattr("retailprintguard.api.exports.MAX_DOCUMENTS", 0)
    with pytest.raises(ExportError, match="limite"):
        service.create(request_body(), actor)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExportRequest)) == 0
    monkeypatch.setattr("retailprintguard.api.exports.MAX_DOCUMENTS", 100)
    for _ in range(3):
        service.create(request_body(), actor)
    with pytest.raises(ExportError, match="Tre richieste"):
        service.create(request_body(), actor)


def test_api_roles_history_download_and_validation(exports):
    service, factory, _, actor = exports
    secret = b"export-test-secret-at-least-thirty-two-bytes"
    app = create_app(
        repository=SqlAlchemyApiRepository(factory), jwt_secret=secret, export_service=service
    )
    tokens = TokenService(secret, lifetime_minutes=30)
    headers = {"Authorization": f"Bearer {tokens.issue(actor)}"}
    readonly = actor.model_copy(update={"roles": (RoleName.READ_ONLY,)})
    denied = {"Authorization": f"Bearer {tokens.issue(readonly)}"}
    body = {"date_from": NOW.date().isoformat(), "date_to": NOW.date().isoformat()}
    with TestClient(app) as client:
        assert client.get("/api/v1/exports").status_code == 401
        assert client.get("/api/v1/exports", headers=denied).status_code == 403
        assert client.post("/api/v1/exports", headers=denied, json=body).status_code == 403
        response = client.post("/api/v1/exports", headers=headers, json=body)
        assert response.status_code == 202, response.text
        export_id = response.json()["id"]
        assert (
            client.get(f"/api/v1/exports/{export_id}/download", headers=headers).status_code == 409
        )
        assert service.run_one()
        page = client.get("/api/v1/exports?limit=1&offset=0", headers=headers).json()
        assert page["total"] == 1
        assert page["items"][0]["status"] == "completed"
        assert page["items"][0]["requested_by"] == "auditor"
        assert client.get(f"/api/v1/exports/{export_id}", headers=denied).status_code == 403
        download = client.get(f"/api/v1/exports/{export_id}/download", headers=headers)
        assert download.status_code == 200
        assert download.content.startswith(b"PK")
        assert hashlib.sha256(download.content).hexdigest() == download.headers["X-Checksum-SHA256"]
        assert client.get("/api/v1/exports?limit=1000", headers=headers).status_code == 422
        assert client.get("/api/v1/exports/not-a-uuid", headers=headers).status_code == 422
        assert UUID(export_id)


def test_snapshot_spans_batches_without_omission(exports, monkeypatch):
    service, factory, ids, actor = exports
    from uuid import uuid4

    with factory.begin() as session:
        source = session.scalar(select(DocumentVersion))
        document = Document(
            id=uuid4(),
            device_id=ids["device"],
            session_id=ids["session"],
            job_id=ids["job"],
            source_document_key="second",
            document_type="PRE_BILL",
            subtype="PRECONTO",
            captured_at=NOW,
            external_document_code=session.get(Document, ids["document"]).external_document_code,
        )
        session.add(document)
        session.flush()
        copied = {
            column.name: getattr(source, column.name)
            for column in source.__table__.columns
            if column.name not in {"id", "document_id", "chain_sequence"}
        }
        session.add(
            DocumentVersion(id=uuid4(), document_id=document.id, chain_sequence=2, **copied)
        )
    monkeypatch.setattr("retailprintguard.api.exports.BATCH_SIZE", 1)
    result = service.create(request_body(), actor)
    assert result.total_documents == 2, result.selection_metadata
    assert service.run_one()
    _, stream = service.open_archive(result.id)
    with stream, ZipFile(stream) as archive:
        entries = json.loads(archive.read("manifest.json"))["documents"]
        assert len(entries) == 2
        assert len({entry["document_id"] for entry in entries}) == 2
        assert [entry["archive_sequence"] for entry in entries] == [1, 2]
        assert entries[0]["filename"].startswith("documents/000001_")
        assert entries[1]["filename"].startswith("documents/000002_")
        assert entries[0]["filename"].split("_", 1)[1] == (
            entries[1]["filename"].split("_", 1)[1]
        )
        assert len(archive.namelist()) == 4


def test_nonlegacy_timestamp_is_selected_without_projection_leakage(exports):
    service, factory, ids, actor = exports
    with factory.begin() as session:
        document = session.get(Document, ids["document"])
        document.document_timestamp = NOW - timedelta(days=10)
        version = session.scalar(select(DocumentVersion))
        version.document_type = "PRE_BILL"
        version.subtype = "PRECONTO"
        version.document_timestamp = NOW
    result = service.create(request_body(), actor)
    assert result.total_documents == 1
    with factory() as session:
        snapshot = session.get(ExportItem, (result.id, 1)).snapshot
        assert datetime.fromisoformat(snapshot["document_timestamp"]) == NOW
