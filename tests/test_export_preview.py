from __future__ import annotations

import hashlib
import io
import json
import struct
from datetime import timedelta
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool
from test_db_repository import NOW, _seed_api
from test_export_selection import _add_document

from retailprintguard.api.auth import TokenService
from retailprintguard.api.exports import ExportCreate, ExportError, ExportService, canonical_json
from retailprintguard.api.main import create_app
from retailprintguard.api.schemas import RoleName, UserPrincipal
from retailprintguard.db import Base, session_factory
from retailprintguard.db.models import (
    AuditLog,
    Device,
    Document,
    DocumentVersion,
    ExportItem,
    ExportRequest,
)
from retailprintguard.db.repository import SqlAlchemyApiRepository
from retailprintguard.render.pdf import PDF_RENDERER_VERSION


@pytest.fixture
def exports(tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    ids = _seed_api(factory)
    service = ExportService(factory, tmp_path / "archive")
    actor = UserPrincipal(id=ids["user"], username="auditor", roles=(RoleName.AUDITOR,))
    yield service, factory, ids, actor
    engine.dispose()


def test_zip_directory_is_bounded_before_zipfile_loads_index(tmp_path):
    path = tmp_path / "index.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("small", b"test")
    with path.open("rb") as stream:
        ExportService._check_zip_index(stream)
        assert stream.tell() == 0
    payload = bytearray(path.read_bytes())
    end_record = payload.rfind(b"PK\x05\x06")
    struct.pack_into("<L", payload, end_record + 12, 16 * 1024 * 1024 + 1)
    path.write_bytes(payload)
    with path.open("rb") as stream, pytest.raises(ExportError, match="Indice ZIP"):
        ExportService._check_zip_index(stream)


def test_latest_ten_skips_invalid_and_partial_pins_order_versions_and_capture_basis(
    exports, monkeypatch
):
    service, factory, ids, actor = exports
    valid = []
    for index in range(13):
        document_id = _add_document(
            factory,
            ids,
            "KITCHEN_ORDER",
            f"COMANDA\n{index + 1} x articolo sintetico",
            timestamp=NOW - timedelta(days=index + 1),
        )
        captured = NOW + timedelta(minutes=min(index, 11))
        with factory.begin() as session:
            session.get(Document, document_id).captured_at = captured
        valid.append((captured, document_id))
    for kind, body, complete in (
        ("DEVICE_RESPONSE", "ON00000000", True),
        ("COMMERCIAL_DOCUMENT", "", True),
        ("KITCHEN_ORDER", "COMANDA\n1 x articolo parziale", False),
    ):
        key = _add_document(factory, ids, kind, body, complete=complete)
        with factory.begin() as session:
            session.get(Document, key).captured_at = NOW + timedelta(hours=1)
    monkeypatch.setattr("retailprintguard.api.exports.BATCH_SIZE", 3)
    result = service.create(ExportCreate(scope="latest_10"), actor)
    expected = [
        key for _, key in sorted(valid, key=lambda pair: (-pair[0].timestamp(), pair[1]))[:10]
    ]
    assert result.scope == "latest_10" and result.document_scope == "complete"
    assert result.total_documents == 10 and result.date_from is result.date_to is None
    assert "ultimi_10_completi" in result.archive_filename
    metadata = result.selection_metadata
    assert metadata["requested_limit"] == 10 and metadata["selection_basis"] == "captured_at"
    assert metadata["selection_order"] == "captured_at_desc_id_asc"
    assert metadata["scanned_documents"] == 13 and metadata["excluded_documents"] == 3
    assert metadata["candidate_documents"] == 17 and metadata["unscanned_documents"] == 4
    with factory() as session:
        items = session.scalars(
            select(ExportItem)
            .where(ExportItem.export_id == result.id)
            .order_by(ExportItem.sequence)
        ).all()
        assert [item.document_id for item in items] == expected
        frozen = [item.snapshot_sha256 for item in items]
    with factory.begin() as session:
        for version in session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id.in_(expected))
        ):
            version.normalized_text = "LATER INTERPRETATION"
    assert service.run_one()
    page = service.archive_documents(result.id, limit=3, offset=3)
    assert page.total == 10 and [item.document_id for item in page.items] == expected[3:6]
    assert not page.legacy and page.renderer_version == PDF_RENDERER_VERSION
    with factory() as session:
        assert [
            session.get(ExportItem, (result.id, index + 1)).snapshot_sha256 for index in range(10)
        ] == frozen


def test_latest_fewer_than_ten_and_empty_device_are_explicit(exports):
    service, factory, _, actor = exports
    result = service.create(ExportCreate(scope="latest_10", device_ids=["pos_1"]), actor)
    assert result.total_documents == 1 and "ultimi_1_completi" in result.archive_filename
    with factory.begin() as session:
        session.add(
            Device(
                external_id="empty",
                name="Synthetic empty device",
                device_type="pos",
                parser_kind="escpos",
                listen_ip="192.0.2.210",
                listen_port=9100,
                target_ip="192.0.2.211",
                target_port=9100,
            )
        )
    with pytest.raises(ExportError, match="Nessun documento"):
        service.create(ExportCreate(scope="latest_10", device_ids=["empty"]), actor)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExportRequest)) == 1
    for parameters in (
        {"date_from": NOW.date(), "date_to": NOW.date()},
        {"document_scope": "partial"},
    ):
        with pytest.raises(ValueError):
            ExportCreate(scope="latest_10", **parameters)


def test_latest_scan_limit_never_silently_omits_newer_candidates(exports, monkeypatch):
    service, factory, ids, actor = exports
    for _ in range(3):
        key = _add_document(factory, ids, "DEVICE_RESPONSE", "ON00000000")
        with factory.begin() as session:
            session.get(Document, key).captured_at = NOW + timedelta(hours=1)
    monkeypatch.setattr("retailprintguard.api.exports.MAX_SCAN_DOCUMENTS", 2)
    with pytest.raises(ExportError, match="Limite di ricerca"):
        service.create(ExportCreate(scope="latest_10"), actor)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ExportRequest)) == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_preview_reads_original_archive_bytes_without_rerender_or_extraction(
    exports, monkeypatch, legacy
):
    service, factory, ids, actor = exports
    request = service.create(ExportCreate(scope="latest_10"), actor)
    if legacy:
        with factory.begin() as session:
            session.get(ExportRequest, request.id).selection_metadata = None
    assert service.run_one()
    path = service.directory / f"{request.id}.zip"
    archive_before = path.read_bytes()
    with ZipFile(io.BytesIO(archive_before)) as archive:
        record = json.loads(archive.read("manifest.json"))["documents"][0]
        expected = archive.read(record["filename"])
    with factory.begin() as session:
        session.get(Document, ids["document"]).external_document_code = "LATER-CODE"
    monkeypatch.setattr(
        "retailprintguard.api.exports.render_document_pdf", lambda _: pytest.fail("rerender")
    )
    files_before = set(service.directory.rglob("*"))
    page = service.archive_documents(request.id, limit=1, offset=0)
    item, payload = service.archive_pdf(request.id, 1)
    assert payload == expected and item.pdf_sha256 == hashlib.sha256(expected).hexdigest()
    assert page.legacy == legacy and page.items == [item]
    assert item.preview_url.endswith(f"/{request.id}/documents/1/pdf")
    assert "/" not in item.filename
    assert service.archive_documents(request.id, limit=1, offset=100).items == []
    with pytest.raises(FileNotFoundError):
        service.archive_pdf(request.id, 2)
    assert path.read_bytes() == archive_before and set(service.directory.rglob("*")) == files_before


def _rewrite_synthetic_archive(
    service, factory, request_id, change, *, bad_signature=False, extra=None
):
    path = service.directory / f"{request_id}.zip"
    with ZipFile(path) as archive:
        members = {entry.filename: archive.read(entry) for entry in archive.infolist()}
    manifest = json.loads(members.pop("manifest.json"))
    members.pop("manifest.sha256")
    change(manifest, members)
    encoded = canonical_json(manifest)
    members["manifest.json"] = encoded
    members["manifest.sha256"] = (
        ("0" * 64 if bad_signature else hashlib.sha256(encoded).hexdigest()) + "  manifest.json\n"
    ).encode()
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
        if extra:
            archive.writestr(*extra)
    path.unlink()  # Synthetic temporary test archive only.
    path.write_bytes(output.getvalue())
    with factory.begin() as session:
        row = session.get(ExportRequest, request_id)
        row.archive_bytes = len(output.getvalue())
        row.archive_sha256 = hashlib.sha256(output.getvalue()).hexdigest()


@pytest.mark.parametrize(
    "mutation", ["manifest_hash", "pdf_hash", "path", "identity", "extra", "sequence"]
)
def test_preview_rejects_internal_corruption_even_with_matching_outer_zip_hash(exports, mutation):
    service, factory, _, actor = exports
    request = service.create(ExportCreate(scope="latest_10"), actor)
    assert service.run_one()

    def change(manifest, members):
        record = manifest["documents"][0]
        if mutation == "pdf_hash":
            record["pdf_sha256"] = "0" * 64
        elif mutation == "path":
            members["../escape.pdf"] = members.pop(record["filename"])
            record["filename"] = "../escape.pdf"
        elif mutation == "identity":
            manifest["export_id"] = str(uuid4())
        elif mutation == "sequence":
            record["archive_sequence"] = 2

    _rewrite_synthetic_archive(
        service,
        factory,
        request.id,
        change,
        bad_signature=mutation == "manifest_hash",
        extra=("unlisted.pdf", b"%PDF-test") if mutation == "extra" else None,
    )
    with pytest.raises(ExportError):
        service.archive_pdf(request.id, 1)
    assert not (service.directory.parent / "escape.pdf").exists()


@pytest.mark.parametrize("limit_name", ["MAX_MANIFEST_BYTES", "MAX_PDF_BYTES"])
def test_preview_checks_declared_sizes_before_decompression(exports, monkeypatch, limit_name):
    service, _, _, actor = exports
    request = service.create(ExportCreate(scope="latest_10"), actor)
    assert service.run_one()
    monkeypatch.setattr("retailprintguard.api.exports." + limit_name, 1)
    with pytest.raises(ExportError):
        service.archive_documents(request.id, limit=10, offset=0)


def test_preview_api_auth_status_inline_bytes_and_audit(exports):
    service, factory, _, actor = exports
    secret = b"synthetic-export-preview-token-signing-key"
    app = create_app(
        repository=SqlAlchemyApiRepository(factory), jwt_secret=secret, export_service=service
    )
    tokens = TokenService(secret, lifetime_minutes=30)
    headers = {"Authorization": f"Bearer {tokens.issue(actor)}"}
    readonly = actor.model_copy(update={"roles": (RoleName.READ_ONLY,)})
    denied = {"Authorization": f"Bearer {tokens.issue(readonly)}"}
    with TestClient(app) as client:
        created = client.post("/api/v1/exports", json={"scope": "latest_10"}, headers=headers)
        assert created.status_code == 202, created.text
        export_id = created.json()["id"]
        listing = f"/api/v1/exports/{export_id}/documents"
        preview = listing + "/1/pdf"
        for url in (listing, preview):
            assert client.get(url).status_code == 401
            assert client.get(url, headers=denied).status_code == 403
            assert client.get(url, headers=headers).status_code == 409
        assert service.run_one()
        page = client.get(listing, headers=headers)
        assert page.status_code == 200 and page.json()["total"] == 1
        response = client.get(preview, headers=headers)
        assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.headers["cache-control"] == "no-store"
        assert hashlib.sha256(response.content).hexdigest() == page.json()["items"][0]["pdf_sha256"]
        assert response.headers["x-checksum-sha256"] == page.json()["items"][0]["pdf_sha256"]
        assert client.get(listing + "/0/pdf", headers=headers).status_code == 404
        assert client.get(listing + "/2/pdf", headers=headers).status_code == 404
        assert client.get(listing + "?limit=101", headers=headers).status_code == 422
        assert (
            client.get(f"/api/v1/exports/{uuid4()}/documents", headers=headers).status_code == 404
        )
    with factory() as session:
        actions = set(session.scalars(select(AuditLog.event_type)))
        assert {"EXPORT_CONTENTS_VIEWED", "EXPORT_PDF_PREVIEWED"} <= actions
