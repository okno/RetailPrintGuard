"""Durable, bounded accounting exports from frozen document views.

The ZIP contains derived PDFs and a checksummed manifest, never substitutes for
fiscal originals or immutable RAW. Existing documents, interpretations and RAW
are never changed. All authorized auditors share the export request history.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import zlib
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from retailprintguard.api.schemas import DocumentView, UserPrincipal
from retailprintguard.db.models import (
    Device,
    Document,
    DocumentVersion,
    ExportItem,
    ExportRequest,
    User,
)
from retailprintguard.db.repository import (
    SqlAlchemyApiRepository,
    _latest_version_id,
    _version_column,
    document_pdf_decision,
)
from retailprintguard.render.eligibility import DOCUMENT_PDF_POLICY_VERSION
from retailprintguard.render.naming import archive_pdf_filename
from retailprintguard.render.pdf import PDF_RENDERER_VERSION, render_document_pdf

TIMEZONE = "Europe/Rome"
MAX_DOCUMENTS = 10_000
MAX_SCAN_DOCUMENTS = 100_000
MAX_EXCLUSION_RECORDS = 10_000
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_ITEM_BYTES = 4 * 1024 * 1024
MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_ZIP_INDEX_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MIN_FREE_BYTES = 256 * 1024 * 1024
BATCH_SIZE = 100


class ExportError(ValueError):
    """A safe, actionable error that may be returned to the user."""


class ExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["all_history", "date_range", "latest_10"] = "date_range"
    document_scope: Literal["complete", "partial"] = "complete"
    date_from: date | None = Field(default=None, ge=date(1900, 1, 1))
    date_to: date | None = None
    device_ids: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_range(self) -> ExportCreate:
        if self.scope in {"all_history", "latest_10"}:
            if self.date_from is not None or self.date_to is not None:
                raise ValueError("Questa selezione non richiede estremi di data")
            if self.scope == "latest_10" and self.document_scope != "complete":
                raise ValueError("Gli ultimi dieci includono soltanto documenti completi")
        else:
            if self.date_from is None or self.date_to is None:
                raise ValueError("L'intervallo richiede entrambe le date")
            if self.date_to < self.date_from:
                raise ValueError(
                    "La data finale deve essere uguale o successiva alla data iniziale"
                )
            if self.date_to == date.max:
                raise ValueError("Data finale fuori intervallo")
        if any(not value or len(value) > 64 for value in self.device_ids):
            raise ValueError("Identificativo dispositivo non valido")
        self.device_ids = sorted(set(self.device_ids))
        return self


class ExportView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    status: Literal["queued", "running", "completed", "failed"]
    date_from: date | None
    date_to: date | None
    timezone: str
    device_ids: list[str]
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    total_documents: int
    completed_documents: int
    archive_bytes: int | None
    archive_sha256: str | None
    error: str | None
    download_url: str | None = None
    scope: Literal["all_history", "date_range", "latest_10"] = "date_range"
    document_scope: Literal["complete", "partial", "legacy_all"] = "legacy_all"
    selection_metadata: dict[str, Any] | None = None
    archive_filename: str = ""


class ExportDocumentView(BaseModel):
    archive_sequence: int
    document_id: UUID
    document_version_id: UUID
    device_id: str
    type: str
    filename: str
    pdf_bytes: int
    pdf_sha256: str
    document_timestamp: datetime | None
    captured_at: datetime
    complete: bool
    preview_url: str


class ExportDocumentPage(BaseModel):
    export_id: UUID
    items: list[ExportDocumentView]
    total: int
    limit: int
    offset: int
    legacy: bool
    renderer_version: str


def utc_bounds(body: ExportCreate) -> tuple[datetime, datetime]:
    if body.date_from is None or body.date_to is None:
        raise ValueError("date_range requires calendar bounds")
    local = ZoneInfo(TIMEZONE)
    return (
        datetime.combine(body.date_from, time.min, local).astimezone(UTC),
        datetime.combine(body.date_to + timedelta(days=1), time.min, local).astimezone(UTC),
    )


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def file_checksum(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _rome_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    try:
        return value.astimezone(ZoneInfo(TIMEZONE))
    except (OverflowError, ValueError) as exc:
        raise ExportError(
            "Data non rappresentabile nel fuso Europe/Rome; richiesta non pubblicata"
        ) from exc


def zip_timestamp(value: datetime) -> tuple[int, int, int, int, int, int]:
    """ZIP stores local wall time, whole two-second units, and years 1980–2107."""
    local = _rome_datetime(value)
    if local.year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    if local.year > 2107:
        return (2107, 12, 31, 23, 59, 58)
    return (local.year, local.month, local.day, local.hour, local.minute, local.second // 2 * 2)


def _view(row: ExportRequest) -> ExportView:
    view = ExportView.model_validate(row)
    metadata = row.selection_metadata
    if isinstance(metadata, dict) and metadata.get("schema_version") == 1:
        view.scope = metadata["scope"]
        view.document_scope = metadata["document_scope"]
        # The bounded excluded-ID audit belongs to the manifest and storage, not
        # every paginated history response.
        view.selection_metadata = {
            key: value for key, value in metadata.items() if key != "exclusion_records"
        }
        first, last = metadata.get("coverage_from"), metadata.get("coverage_to")
        coverage = (
            f"copertura_{date.fromisoformat(first)}_{date.fromisoformat(last)}"
            if first and last else "vuoto"
        )
        category = "completi" if view.document_scope == "complete" else "parziali"
        if view.scope == "latest_10":
            category = f"ultimi_{view.total_documents}_completi"
        view.archive_filename = f"RetailPrintGuard_{category}_{coverage}_{row.id}.zip"
    else:
        view.archive_filename = f"RetailPrintGuard_archivio_precedente_{row.id}.zip"
    if view.status == "completed":
        view.download_url = f"/api/v1/exports/{view.id}/download"
    return view


class ExportService:
    def __init__(self, factory: sessionmaker[Session], archive_root: Path) -> None:
        self.factory = factory
        self.directory = Path(
            os.environ.get("RPG_EXPORT_ROOT", str(Path(archive_root) / "exports"))
        )
        if not self.directory.is_absolute():
            raise ValueError("RPG_EXPORT_ROOT must be an absolute path")
        self.api = SqlAlchemyApiRepository(factory)

    def create(self, body: ExportCreate, actor: UserPrincipal) -> ExportView:
        """Pin membership, version, display metadata and derived values atomically.

        Batches bound memory; explicit limits reject an entire oversized request,
        never truncate its documents. REPEATABLE READ pins MariaDB source reads.
        """
        with self.api._write() as session:
            if session.get_bind().dialect.name in {"mysql", "mariadb"}:
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            user = session.scalar(select(User).where(User.id == actor.id).with_for_update())
            if user is None or not user.active:
                raise ExportError("Utente non disponibile")
            pending = (
                session.scalar(
                    select(func.count())
                    .select_from(ExportRequest)
                    .where(
                        ExportRequest.requested_by_user_id == actor.id,
                        ExportRequest.status.in_(("queued", "running")),
                    )
                )
                or 0
            )
            if pending >= 3:
                raise ExportError(
                    "Tre richieste sono già in lavorazione; attendere il completamento"
                )
            if body.device_ids:
                found = set(
                    session.scalars(
                        select(Device.external_id).where(Device.external_id.in_(body.device_ids))
                    )
                )
                if found != set(body.device_ids):
                    raise ExportError("Uno o più dispositivi non esistono")
            semantic_time = _version_column(
                DocumentVersion.document_timestamp, Document.document_timestamp
            )
            effective_time = func.coalesce(semantic_time, Document.captured_at)
            statement = (
                select(
                    Document,
                    DocumentVersion.id,
                    _version_column(DocumentVersion.document_type, Document.document_type),
                    _version_column(DocumentVersion.subtype, Document.subtype),
                    DocumentVersion.normalized_text,
                    DocumentVersion.complete,
                )
                .join(Device, Device.id == Document.device_id)
                .outerjoin(DocumentVersion, DocumentVersion.id == _latest_version_id())
                .order_by(*(
                    (Document.captured_at.desc(), Document.id)
                    if body.scope == "latest_10" else (Document.id,)
                ))
            )
            if body.scope == "date_range":
                lower, upper = utc_bounds(body)
                statement = statement.where(effective_time >= lower, effective_time < upper)
            if body.device_ids:
                statement = statement.where(Device.external_id.in_(body.device_ids))
            count = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            if count > MAX_SCAN_DOCUMENTS and body.scope != "latest_10":
                raise ExportError(
                    f"La richiesta esamina {count} record: limite {MAX_SCAN_DOCUMENTS}. "
                    "Suddividere l'intervallo di date o selezionare un dispositivo."
                )
            row = ExportRequest(
                id=uuid4(),
                requested_by_user_id=actor.id,
                requested_by=actor.username,
                date_from=body.date_from,
                date_to=body.date_to,
                timezone=TIMEZONE,
                device_ids=body.device_ids,
                status="queued",
                created_at=datetime.now(UTC),
                total_documents=0,
                completed_documents=0,
                renderer_version=PDF_RENDERER_VERSION,
            )
            session.add(row)
            session.flush()
            snapshot_bytes = 0
            selected_count = 0
            category_counts: Counter[str] = Counter(complete=0, partial=0, excluded=0)
            exclusion_counts: Counter[str] = Counter()
            exclusion_records: list[dict[str, Any]] = []
            coverage: list[date] = []
            document_date_count = 0
            capture_date_count = 0
            scanned_count = 0
            capture_coverage: list[datetime] = []
            for offset in range(0, min(count, MAX_SCAN_DOCUMENTS), BATCH_SIZE):
                pairs = session.execute(
                    statement.limit(min(BATCH_SIZE, MAX_SCAN_DOCUMENTS - offset)).offset(offset)
                ).all()
                selected = []
                for document, version_id, kind, subtype, text, complete in pairs:
                    scanned_count += 1
                    decision = (
                        document_pdf_decision(kind, subtype, text, complete)
                        if version_id is not None else None
                    )
                    category = decision.category if decision else "excluded"
                    category_counts[category] += 1
                    if category == body.document_scope:
                        selected.append((document, version_id))
                        if body.scope == "latest_10" and selected_count + len(selected) == 10:
                            break
                        continue
                    reason = (
                        "NO_ACTIVE_VERSION" if decision is None else decision.reason_code
                        if category == "excluded" else "OTHER_DOCUMENT_SCOPE"
                    )
                    exclusion_counts[reason] += 1
                    if len(exclusion_records) < MAX_EXCLUSION_RECORDS:
                        exclusion_records.append({
                            "document_id": str(document.id),
                            "document_version_id": str(version_id) if version_id else None,
                            "category": category,
                            "reason_code": reason,
                        })
                versions = {document.id: version for document, version in selected}
                views = self.api._document_views(
                    session,
                    [document for document, _ in selected],
                    version_ids=list(versions.values()),
                ) if selected else []
                if len(views) != len(selected):
                    raise ExportError("Snapshot documenti incompleto; richiesta annullata")
                for document in views:
                    if document.pdf_category != body.document_scope:
                        raise ExportError("Selezione documento incoerente; richiesta annullata")
                    selected_count += 1
                    if selected_count > MAX_DOCUMENTS:
                        raise ExportError(
                            f"Superato il limite di {MAX_DOCUMENTS} PDF; "
                            "suddividere l'intervallo o scegliere un dispositivo"
                        )
                    snapshot = document.model_dump(mode="json")
                    payload = canonical_json(snapshot)
                    snapshot_bytes += len(payload)
                    if len(payload) > MAX_ITEM_BYTES or snapshot_bytes > MAX_SNAPSHOT_BYTES:
                        raise ExportError(
                            "Limite dimensione snapshot superato; ridurre l'intervallo"
                        )
                    session.add(
                        ExportItem(
                            export_id=row.id,
                            sequence=selected_count,
                            document_id=document.id,
                            document_version_id=versions[document.id],
                            snapshot=snapshot,
                            snapshot_sha256=hashlib.sha256(payload).hexdigest(),
                            date_basis="document_timestamp"
                            if document.document_timestamp
                            else "captured_at",
                        )
                    )
                    timestamp = document.document_timestamp or document.captured_at
                    coverage.append(_rome_datetime(timestamp).date())
                    capture_coverage.append(document.captured_at)
                    document_date_count += int(document.document_timestamp is not None)
                    capture_date_count += int(document.document_timestamp is None)
                session.flush()
                session.expunge_all()
                if body.scope == "latest_10" and selected_count == 10:
                    break
            if body.scope == "latest_10":
                if selected_count < 10 and scanned_count < count:
                    raise ExportError(
                        "Limite di ricerca raggiunto; selezionare un dispositivo "
                        "per individuare gli ultimi documenti completi"
                    )
                if selected_count == 0:
                    raise ExportError(
                        "Nessun documento completo valido disponibile per l'esportazione"
                    )
            row.total_documents = selected_count
            row.selection_metadata = {
                "schema_version": 1,
                "policy_version": DOCUMENT_PDF_POLICY_VERSION,
                "scope": body.scope,
                "document_scope": body.document_scope,
                "scanned_documents": scanned_count,
                "selected_documents": selected_count,
                "excluded_documents": scanned_count - selected_count,
                "category_counts": dict(category_counts),
                "exclusion_counts": dict(exclusion_counts),
                "exclusion_records": exclusion_records,
                "exclusion_records_truncated": (
                    scanned_count - selected_count - len(exclusion_records)
                ),
                "coverage_from": min(coverage).isoformat() if coverage else None,
                "coverage_to": max(coverage).isoformat() if coverage else None,
                "document_date_count": document_date_count,
                "capture_date_count": capture_date_count,
            }
            if body.scope == "latest_10":
                row.selection_metadata = {
                    **row.selection_metadata,
                    "requested_limit": 10,
                    "candidate_documents": count,
                    "unscanned_documents": count - scanned_count,
                    "selection_basis": "captured_at",
                    "selection_order": "captured_at_desc_id_asc",
                    "ordering": "captured_at_desc_document_id_asc",
                    "selection_capture_from": min(capture_coverage).isoformat(),
                    "selection_capture_to": max(capture_coverage).isoformat(),
                    "coverage_basis": "document_timestamp_or_captured_at",
                }
            session.add(row)
            session.flush()
            return _view(row)

    def history(self, *, limit: int, offset: int) -> tuple[list[ExportView], int]:
        with self.api._read() as session:
            total = session.scalar(select(func.count()).select_from(ExportRequest)) or 0
            rows = session.scalars(
                select(ExportRequest)
                .order_by(ExportRequest.created_at.desc(), ExportRequest.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_view(row) for row in rows], total

    def get(self, export_id: UUID) -> ExportView | None:
        with self.api._read() as session:
            row = session.get(ExportRequest, export_id)
            return _view(row) if row else None

    def open_archive(self, export_id: UUID) -> tuple[ExportView, BinaryIO]:
        view = self.get(export_id)
        if view is None:
            raise FileNotFoundError("Richiesta non trovata")
        if view.status != "completed":
            raise ExportError("Archivio non ancora disponibile")
        if view.archive_bytes is None or not 0 < view.archive_bytes <= MAX_ARCHIVE_BYTES:
            raise ExportError("Dimensione archivio non valida")
        path = self.directory / f"{export_id}.zip"
        if path.is_symlink() or not path.is_file():
            raise ExportError("Archivio non disponibile; verificare lo storage")
        stream = path.open("rb")
        try:
            if os.fstat(stream.fileno()).st_size != view.archive_bytes:
                raise ExportError("Integrità archivio non verificata")
            if file_checksum(stream) != view.archive_sha256:
                raise ExportError("Integrità archivio non verificata")
            stream.seek(0)
            return view, stream
        except Exception:
            stream.close()
            raise

    @staticmethod
    def _check_zip_index(stream: BinaryIO) -> None:
        # ZipFile loads the complete central directory in memory. Check its
        # declared size and count before constructing it. The writer's 1 GiB /
        # 10,000-document limits never require ZIP64 or multi-disk archives.
        size = os.fstat(stream.fileno()).st_size
        tail_size = min(size, 65_557)
        stream.seek(size - tail_size)
        tail = stream.read(tail_size)
        position = tail.rfind(b"PK\x05\x06")
        if position < 0 or len(tail) - position < 22:
            raise ExportError("Indice ZIP non disponibile")
        _, disk, directory_disk, disk_count, count, directory_size, offset, comment = struct.unpack(
            "<4s4H2LH", tail[position:position + 22]
        )
        if (
            disk != 0 or directory_disk != 0 or disk_count != count
            or count > MAX_DOCUMENTS + 2 or directory_size > MAX_ZIP_INDEX_BYTES
            or position + 22 + comment != len(tail)
            or offset + directory_size != size - tail_size + position
        ):
            raise ExportError("Indice ZIP fuori dai limiti supportati")
        stream.seek(0)

    @staticmethod
    def _read_member(archive: ZipFile, name: str, maximum: int) -> bytes:
        info = archive.getinfo(name)
        if not 0 <= info.file_size <= maximum:
            raise ExportError("Dimensione elemento archivio non valida")
        with archive.open(info) as member:
            payload = member.read(maximum + 1)
        if len(payload) != info.file_size or len(payload) > maximum:
            raise ExportError("Integrità elemento archivio non verificata")
        return payload

    @staticmethod
    def _manifest_documents(
        archive: ZipFile, view: ExportView
    ) -> tuple[list[ExportDocumentView], bool, str]:
        """Validate names and bounded metadata; never extract members to disk."""
        entries = archive.infolist()
        if len(entries) > MAX_DOCUMENTS + 2 or len(entries) != len({i.filename for i in entries}):
            raise ExportError("Indice archivio non valido")
        for entry in entries:
            if (
                entry.flag_bits & 1
                or entry.compress_type not in {ZIP_DEFLATED, ZIP_STORED}
                or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}
            ):
                raise ExportError("Formato elemento archivio non valido")
        payload = ExportService._read_member(archive, "manifest.json", MAX_MANIFEST_BYTES)
        signature = ExportService._read_member(archive, "manifest.sha256", 256)
        if signature != (hashlib.sha256(payload).hexdigest() + "  manifest.json\n").encode("ascii"):
            raise ExportError("Integrità manifest non verificata")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result = dict(pairs)
            if len(result) != len(pairs):
                raise ExportError("Manifest ambiguo")
            return result

        manifest = json.loads(payload, object_pairs_hook=unique_object)
        if not isinstance(manifest, dict) or manifest.get("schema") not in {
            "retailprintguard.accounting-export.v1", "retailprintguard.accounting-export.v2",
        } or manifest.get("export_id") != str(view.id):
            raise ExportError("Identità manifest non verificata")
        records = manifest.get("documents")
        if not isinstance(records, list) or len(records) != view.total_documents:
            raise ExportError("Elenco documenti archivio incompleto")
        documents = []
        expected_names = {"manifest.json", "manifest.sha256"}
        for sequence, record in enumerate(records, 1):
            if not isinstance(record, dict) or type(record.get("archive_sequence")) is not int:
                raise ExportError("Sequenza documento archivio non valida")
            filename = record.get("filename")
            if (
                record["archive_sequence"] != sequence
                or not isinstance(filename, str) or len(filename) > 512
                or "\\" in filename or any(ord(char) < 32 or ord(char) == 127 for char in filename)
                or PurePosixPath(filename).parts != ("documents", PurePosixPath(filename).name)
                or filename != "documents/" + PurePosixPath(filename).name
                or not filename.endswith(".pdf") or filename in expected_names
            ):
                raise ExportError("Nome o sequenza documento archivio non validi")
            size, checksum = record.get("pdf_bytes"), record.get("pdf_sha256")
            if (
                type(size) is not int or not 0 < size <= MAX_PDF_BYTES
                or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                or archive.getinfo(filename).file_size != size
            ):
                raise ExportError("Metadati PDF non validi")
            expected_names.add(filename)
            documents.append(ExportDocumentView(
                **{key: record[key] for key in (
                    "archive_sequence", "document_id", "document_version_id", "device_id", "type",
                    "pdf_bytes", "pdf_sha256", "document_timestamp", "captured_at", "complete",
                )},
                filename=PurePosixPath(filename).name,
                preview_url=f"/api/v1/exports/{view.id}/documents/{sequence}/pdf",
            ))
        if {entry.filename for entry in entries} != expected_names:
            raise ExportError("Indice archivio diverso dal manifest")
        renderer = manifest.get("renderer_version")
        if not isinstance(renderer, str) or not renderer or len(renderer) > 128:
            raise ExportError("Versione renderer archivio non valida")
        return documents, manifest["schema"].endswith(".v1"), renderer

    def archive_documents(self, export_id: UUID, *, limit: int, offset: int) -> ExportDocumentPage:
        if not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
            raise ExportError("Paginazione archivio non valida")
        view, stream = self.open_archive(export_id)
        try:
            with stream:
                self._check_zip_index(stream)
                with ZipFile(stream) as archive:
                    documents, legacy, renderer = self._manifest_documents(archive, view)
            return ExportDocumentPage(
                export_id=export_id, items=documents[offset:offset + limit], total=len(documents),
                limit=limit, offset=offset, legacy=legacy, renderer_version=renderer,
            )
        except (
            BadZipFile, KeyError, ValueError, TypeError, RecursionError, OSError, zlib.error
        ) as exc:
            if isinstance(exc, ExportError):
                raise
            raise ExportError("Archivio non leggibile o manifest non valido") from None

    def archive_pdf(
        self, export_id: UUID, sequence: int
    ) -> tuple[ExportDocumentView, bytes]:
        if not 1 <= sequence <= MAX_DOCUMENTS:
            raise FileNotFoundError("PDF non trovato nell'archivio")
        view, stream = self.open_archive(export_id)
        try:
            with stream:
                self._check_zip_index(stream)
                with ZipFile(stream) as archive:
                    documents, _, _ = self._manifest_documents(archive, view)
                    if sequence > len(documents):
                        raise FileNotFoundError("PDF non trovato nell'archivio")
                    document = documents[sequence - 1]
                    payload = self._read_member(
                        archive, "documents/" + document.filename, MAX_PDF_BYTES
                    )
                    if (
                        hashlib.sha256(payload).hexdigest() != document.pdf_sha256
                        or not payload.startswith(b"%PDF-")
                    ):
                        raise ExportError("Integrità PDF non verificata")
            return document, payload
        except FileNotFoundError:
            raise
        except (
            BadZipFile, KeyError, ValueError, TypeError, RecursionError, OSError, zlib.error
        ) as exc:
            if isinstance(exc, ExportError):
                raise
            raise ExportError("Archivio non leggibile o manifest non valido") from None

    def recover(self) -> None:
        """Called only after acquiring the exclusive local worker lock."""
        with self.api._write() as session:
            session.execute(
                update(ExportRequest)
                .where(ExportRequest.status == "running")
                .values(
                    status="queued",
                    completed_documents=0,
                )
            )

    def run_one(self) -> bool:
        """One job, one PDF in memory, restartable frozen snapshot; exclusive worker only."""
        with self.api._write() as session:
            row = session.scalar(
                select(ExportRequest)
                .where(ExportRequest.status == "queued")
                .order_by(ExportRequest.created_at, ExportRequest.id)
                .limit(1)
                .with_for_update()
            )
            if row is None:
                return False
            row.status = "running"
            row.started_at = row.started_at or datetime.now(UTC)
            row.completed_documents = 0
            export_id = row.id
        try:
            self._build(row)
        except Exception as exc:
            # Do not expose paths, payloads or DB credentials in persisted errors.
            message = (
                str(exc)
                if isinstance(exc, ExportError)
                else (
                    f"Esportazione interrotta ({type(exc).__name__}); "
                    "nessun archivio parziale disponibile."
                )
            )
            with self.api._write() as session:
                stored = session.get(ExportRequest, export_id)
                stored.status = "failed"
                stored.error = message[:1024]
                stored.completed_at = datetime.now(UTC)
        return True

    def _build(self, row: ExportRequest) -> None:
        if row.renderer_version != PDF_RENDERER_VERSION:
            raise ExportError("Versione renderer cambiata; creare una nuova richiesta")
        metadata = row.selection_metadata
        if metadata is not None and metadata.get("policy_version") != DOCUMENT_PDF_POLICY_VERSION:
            raise ExportError("Criteri di selezione cambiati; creare una nuova richiesta")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        if self.directory.is_symlink():
            raise ExportError("Percorso archivio non valido")
        if shutil.disk_usage(self.directory).free < MIN_FREE_BYTES:
            raise ExportError("Spazio disco insufficiente per l'esportazione")
        partial = self.directory / f"{row.id}.partial"
        final = self.directory / f"{row.id}.zip"
        if partial.is_symlink() or final.is_symlink():
            raise ExportError("Percorso archivio non valido")
        if partial.exists():
            partial.unlink()  # only this request's unpublished derived temporary file
        entries: list[dict[str, Any]] = []
        manifest = {
            "schema": "retailprintguard.accounting-export.v2"
            if metadata is not None else "retailprintguard.accounting-export.v1",
            "export_id": str(row.id),
            "requested_by": row.requested_by,
            "created_at": row.created_at.isoformat(),
            "date_from": row.date_from.isoformat() if row.date_from else None,
            "date_to": row.date_to.isoformat() if row.date_to else None,
            "timezone": row.timezone,
            "device_ids": row.device_ids,
            "date_policy": (
                "document_timestamp; fallback captured_at; inclusive local calendar dates"
            ),
            "scope": (
                f"printable {metadata['document_scope']} documents selected by versioned policy"
                if metadata is not None else
                "all persisted documents, including technical and excluded records"
            ),
            "renderer_version": row.renderer_version,
            "source_hash_policy": "stored parser references; RAW is not bundled or rehashed",
            "notice": "Derived PDFs; immutable RAW remains authoritative. Not fiscal originals.",
            "documents": entries,
        }
        if metadata is not None:
            manifest["selection_metadata"] = metadata
            if metadata.get("scope") == "latest_10":
                manifest["date_policy"] = (
                    "latest complete printable documents by captured_at descending, "
                    "document id ascending; "
                    "coverage and PDF dates use document_timestamp with captured_at fallback"
                )
            manifest["zip_timestamp_policy"] = {
                "timezone": TIMEZONE,
                "pdf_basis": "document_timestamp; fallback captured_at",
                "manifest_basis": "request_created_at",
                "precision": "two seconds, rounded down",
                "out_of_range": "clamped to ZIP range 1980-01-01 through 2107-12-31",
            }
        failed_document: UUID | None = None
        try:
            with ZipFile(partial, "w", compression=ZIP_DEFLATED, allowZip64=True) as archive:
                for sequence in range(1, row.total_documents + 1):
                    with self.api._read() as session:
                        item = session.get(ExportItem, (row.id, sequence))
                        if item is None:
                            raise ExportError("Snapshot incompleto; archivio non pubblicato")
                    failed_document = item.document_id
                    if (
                        hashlib.sha256(canonical_json(item.snapshot)).hexdigest()
                        != item.snapshot_sha256
                    ):
                        raise ExportError(
                            "Integrità snapshot non verificata; archivio non pubblicato"
                        )
                    document = DocumentView.model_validate(item.snapshot)
                    if metadata is not None and document.pdf_category != metadata["document_scope"]:
                        raise ExportError("Documento fuori dalla selezione congelata")
                    payload = render_document_pdf(document)
                    if len(payload) > MAX_PDF_BYTES:
                        raise ExportError("Limite dimensione PDF superato; archivio non pubblicato")
                    filename = (
                        f"documents/{item.sequence:06d}_{document.pdf_filename}"
                        if metadata is not None else
                        f"documents/{archive_pdf_filename(document, item.sequence)}"
                    )
                    source_timestamp = document.document_timestamp or document.captured_at
                    if source_timestamp.tzinfo is None:
                        source_timestamp = source_timestamp.replace(tzinfo=UTC)
                    self._zip_write(
                        archive, filename, payload,
                        timestamp=source_timestamp if metadata is not None else None,
                    )
                    entries.append(
                        {
                            "archive_sequence": item.sequence,
                            "document_id": str(document.id),
                            "document_version_id": str(item.document_version_id),
                            "device_id": document.device_id,
                            "type": document.type,
                            "external_document_code": document.external_document_code,
                            "external_document_code_suffix": document.external_document_code_suffix,
                            "order_code": document.order_code,
                            "document_timestamp": document.document_timestamp.isoformat()
                            if document.document_timestamp
                            else None,
                            "captured_at": document.captured_at.isoformat(),
                            "date_basis": item.date_basis,
                            "source_sha256": document.sha256,
                            "snapshot_sha256": item.snapshot_sha256,
                            "filename": filename,
                            "pdf_sha256": hashlib.sha256(payload).hexdigest(),
                            "pdf_bytes": len(payload),
                            "complete": document.complete,
                            "warnings": document.warnings,
                        }
                    )
                    if metadata is not None:
                        local = _rome_datetime(source_timestamp)
                        entries[-1]["zip_timestamp_clamped"] = not 1980 <= local.year <= 2107
                    if partial.stat().st_size > MAX_ARCHIVE_BYTES:
                        raise ExportError("Limite archivio 1 GiB superato; ridurre l'intervallo")
                    if sequence % 10 == 0 or sequence == row.total_documents:
                        if shutil.disk_usage(self.directory).free < MIN_FREE_BYTES:
                            raise ExportError("Spazio disco insufficiente; archivio non pubblicato")
                        with self.api._write() as session:
                            session.execute(
                                update(ExportRequest)
                                .where(ExportRequest.id == row.id)
                                .values(completed_documents=sequence)
                            )
                manifest_bytes = canonical_json(manifest)
                self._zip_write(
                    archive, "manifest.json", manifest_bytes,
                    timestamp=row.created_at if metadata is not None else None,
                )
                self._zip_write(
                    archive,
                    "manifest.sha256",
                    (hashlib.sha256(manifest_bytes).hexdigest() + "  manifest.json\n").encode(
                        "ascii"
                    ),
                    timestamp=row.created_at if metadata is not None else None,
                )
            if partial.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ExportError("Limite archivio 1 GiB superato; ridurre l'intervallo")
            with partial.open("rb") as stream:
                os.fsync(stream.fileno())
                checksum = file_checksum(stream)
            # Publish exclusively. Crash between publication and DB commit is
            # recovered by deterministic rebuild; never replace an existing ZIP.
            if final.exists():
                with final.open("rb") as stream:
                    if file_checksum(stream) != checksum:
                        raise ExportError(
                            "Archivio preesistente diverso; revisione manuale richiesta"
                        )
            else:
                os.link(partial, final)
                final.chmod(0o440)
                if os.name == "posix":
                    directory_fd = os.open(self.directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            with self.api._write() as session:
                session.execute(
                    update(ExportRequest)
                    .where(ExportRequest.id == row.id)
                    .values(
                        status="completed",
                        completed_at=datetime.now(UTC),
                        error=None,
                        archive_bytes=final.stat().st_size,
                        archive_sha256=checksum,
                        completed_documents=row.total_documents,
                    )
                )
        except Exception as exc:
            if failed_document is not None:
                message = str(exc) if isinstance(exc, ExportError) else type(exc).__name__
                raise ExportError(f"Documento {failed_document}: {message}") from exc
            raise
        finally:
            if partial.is_file() and not partial.is_symlink():
                partial.unlink()

    @staticmethod
    def _zip_write(
        archive: ZipFile, filename: str, payload: bytes, *, timestamp: datetime | None = None
    ) -> None:
        info = ZipInfo(
            filename,
            date_time=zip_timestamp(timestamp) if timestamp is not None else (1980, 1, 1, 0, 0, 0),
        )
        info.compress_type = ZIP_DEFLATED
        info.external_attr = 0o440 << 16
        archive.writestr(info, payload)
