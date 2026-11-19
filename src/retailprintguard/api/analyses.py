"""Persistent whole-history reanalysis requests and immutable result snapshots."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from retailprintguard.analysis.discounts import DISCOUNT_ANALYSIS_VERSION
from retailprintguard.api.exports import canonical_json
from retailprintguard.api.schemas import UserPrincipal
from retailprintguard.common.config import CorrelationConfig, FraudConfig, Settings
from retailprintguard.correlation.engine import ALGORITHM_VERSION
from retailprintguard.db.models import (
    ActiveParserVersion,
    AnalysisInput,
    AnalysisRequest,
    AnalysisResult,
    Device,
    Document,
    PrintJob,
    ProxySession,
    RawPayload,
    User,
)
from retailprintguard.db.repository import SqlAlchemyApiRepository, _latest_version_id
from retailprintguard.fraud.worker import FRAUD_ENGINE_VERSION
from retailprintguard.parser.repository import PARSERS_BY_KIND, _parser_build_sha256

MAX_HISTORY_JOBS = 500_000


class AnalysisError(RuntimeError):
    """A safe, user-facing reanalysis error."""


class AnalysisCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["all_history"] = "all_history"


class AnalysisView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    status: Literal["queued", "running", "completed", "failed"]
    stage: str
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    total_jobs: int
    completed_jobs: int
    failed_jobs: int
    total_documents: int
    completed_documents: int
    parser_versions: dict[str, Any]
    policy_version: str
    summary: dict[str, Any] | None
    error: str | None
    scope: Literal["all_history"] = "all_history"
    input_metadata: dict[str, Any]
    checkpoints: dict[str, Any]


class DiscountObservationView(BaseModel):
    sequence: int
    label: str
    amount: Decimal
    rate_percent: Decimal | None
    kind: Literal["employee_30", "generic_30", "other"]
    basis: Decimal | None
    arithmetic_verified: bool
    source_text: str


class AnalysisResultView(BaseModel):
    analysis_id: UUID
    document_id: UUID
    document_version_id: UUID
    device_id: str
    captured_at: datetime
    document_timestamp: datetime | None
    document_type: str
    complete: bool
    observations: list[DiscountObservationView] = Field(default_factory=list)
    employee_total: Decimal
    generic_30_total: Decimal
    other_discount_total: Decimal
    arithmetic_reconciled: bool
    reasons: list[str]
    policy_version: str
    analysis_excluded: bool = False
    reprocessed_from_raw: bool = True
    reprocessing_note: str | None = None
    parser_name: str
    parser_version: str


def parser_identities() -> dict[str, Any]:
    return {
        kind: {
            "name": module.PARSER_NAME,
            "version": module.PARSER_VERSION,
            "build_sha256": _parser_build_sha256(module.PARSER_NAME),
        }
        for kind, module in sorted(PARSERS_BY_KIND.items())
    }


def pipeline_configuration(settings: Settings | None) -> dict[str, Any]:
    return {
        "timezone": settings.timezone if settings else "Europe/Rome",
        "correlation": (settings.correlation if settings else CorrelationConfig()).model_dump(
            mode="json"
        ),
        "fraud": (settings.fraud if settings else FraudConfig()).model_dump(mode="json"),
        "correlation_version": ALGORITHM_VERSION,
        "fraud_version": FRAUD_ENGINE_VERSION,
    }


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _safe_csv(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


class AnalysisService:
    def __init__(self, factory: sessionmaker[Session], settings: Settings | None = None) -> None:
        self.factory = factory
        self.settings = settings
        self.api = SqlAlchemyApiRepository(factory)

    def create(self, body: AnalysisCreate, actor: UserPrincipal) -> AnalysisView:
        del body  # The only supported scope is validated before any transaction.
        identities = parser_identities()
        try:
            with self.factory.begin() as session:
                if session.get_bind().dialect.name in {"mysql", "mariadb"}:
                    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                user = session.get(User, actor.id)
                if user is None or not user.active:
                    raise AnalysisError("Utente non disponibile")
                if session.scalar(
                    select(AnalysisRequest.id).where(AnalysisRequest.active_slot == 1)
                ):
                    raise AnalysisError("Una rianalisi è già in coda o in esecuzione")
                count = session.scalar(select(func.count()).select_from(PrintJob)) or 0
                if count > MAX_HISTORY_JOBS:
                    raise AnalysisError("Storico oltre il limite operativo; nessun input troncato")
                row = AnalysisRequest(
                    id=uuid4(),
                    requested_by_user_id=actor.id,
                    requested_by=actor.username,
                    status="queued",
                    active_slot=1,
                    stage="queued",
                    created_at=datetime.now(UTC),
                    total_jobs=count,
                    completed_jobs=0,
                    failed_jobs=0,
                    total_documents=0,
                    completed_documents=0,
                    parser_versions=identities,
                    policy_version=DISCOUNT_ANALYSIS_VERSION,
                    input_metadata={},
                    checkpoints={},
                )
                session.add(row)
                session.flush()  # UNIQUE active_slot serializes concurrent creators globally.
                input_hash = hashlib.sha256()
                without_raw = 0
                for offset in range(0, count, 100):
                    jobs = session.execute(
                        select(
                            PrintJob,
                            Device.external_id,
                            Device.parser_kind,
                            ProxySession.source_session_id,
                        )
                        .join(Device, Device.id == PrintJob.device_id)
                        .join(ProxySession, ProxySession.id == PrintJob.session_id)
                        .order_by(PrintJob.id)
                        .limit(100)
                        .offset(offset)
                    ).all()
                    raw_by_job: dict[UUID, list[Any]] = defaultdict(list)
                    versions_by_job: dict[UUID, list[Any]] = defaultdict(list)
                    job_ids = [job.id for job, _, _, _ in jobs]
                    for job_id, payload_id, role, checksum, source_path in session.execute(
                        select(
                            RawPayload.job_id,
                            RawPayload.id,
                            RawPayload.artifact_role,
                            RawPayload.sha256,
                            RawPayload.source_path,
                        )
                        .where(RawPayload.job_id.in_(job_ids))
                        .order_by(RawPayload.id)
                    ):
                        raw_by_job[job_id].append((payload_id, role, checksum, source_path))
                    for job_id, document_id, version_id in session.execute(
                        select(Document.job_id, Document.id, _latest_version_id())
                        .where(Document.job_id.in_(job_ids))
                        .order_by(Document.id)
                    ):
                        versions_by_job[job_id].append((document_id, version_id))
                    for index, (job, device_id, parser_kind, source_session_id) in enumerate(
                        jobs, offset + 1
                    ):
                        payloads = raw_by_job[job.id]
                        before = versions_by_job[job.id]
                        snapshot = {
                            "job_id": str(job.id),
                            "source_job_id": job.source_job_id,
                            "session_id": source_session_id,
                            "device_id": device_id,
                            "parser_kind": str(parser_kind),
                            "manifest_sha256": job.manifest_sha256,
                            "captured_at": job.captured_at.isoformat(),
                            "analysis_excluded": job.analysis_excluded,
                            "review_state": job.review_state,
                            "raw_payloads": [
                                {
                                    "id": str(identifier),
                                    "role": role,
                                    "sha256": sha,
                                    "source_path": path,
                                }
                                for identifier, role, sha, path in payloads
                            ],
                            "previous_versions": [
                                {
                                    "document_id": str(doc),
                                    "version_id": str(version) if version else None,
                                }
                                for doc, version in before
                            ],
                        }
                        checksum = digest(snapshot)
                        input_hash.update(bytes.fromhex(checksum))
                        has_request = any(role == "REQUEST_RAW" for _, role, _, _ in payloads)
                        without_raw += int(not has_request)
                        session.add(
                            AnalysisInput(
                                analysis_id=row.id,
                                job_id=job.id,
                                sequence=index,
                                snapshot=snapshot,
                                snapshot_sha256=checksum,
                                status="pending",
                            )
                        )
                    session.flush()
                row.input_metadata = {
                    "pipeline_configuration": pipeline_configuration(self.settings),
                    "schema_version": 1,
                    "scope": "all_history",
                    "input_sha256": input_hash.hexdigest(),
                    "jobs_without_request_raw": without_raw,
                    "active_parsers_before": {
                        name: str(identifier)
                        for name, identifier in session.execute(
                            select(
                                ActiveParserVersion.parser_name,
                                ActiveParserVersion.parser_version_id,
                            ).order_by(ActiveParserVersion.parser_name)
                        )
                    },
                    "date_filter_basis": "captured_at",
                    "totals_basis": "observed_document_lines",
                }
                session.flush()
                return AnalysisView.model_validate(row)
        except IntegrityError as exc:
            raise AnalysisError("Una rianalisi è già in coda o in esecuzione") from exc

    def history(self, *, limit: int, offset: int) -> tuple[list[AnalysisView], int]:
        with self.factory() as session:
            total = session.scalar(select(func.count()).select_from(AnalysisRequest)) or 0
            rows = session.scalars(
                select(AnalysisRequest)
                .order_by(AnalysisRequest.created_at.desc(), AnalysisRequest.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            return [AnalysisView.model_validate(row) for row in rows], total

    def get(self, identifier: UUID) -> AnalysisView | None:
        with self.factory() as session:
            row = session.get(AnalysisRequest, identifier)
            return AnalysisView.model_validate(row) if row is not None else None

    def retry(self, identifier: UUID) -> AnalysisView:
        try:
            with self.factory.begin() as session:
                row = session.scalar(
                    select(AnalysisRequest)
                    .where(AnalysisRequest.id == identifier)
                    .with_for_update()
                )
                if row is None:
                    raise FileNotFoundError(identifier)
                if row.status in {"queued", "running"}:
                    return AnalysisView.model_validate(row)
                if row.status != "failed":
                    raise AnalysisError("Una rianalisi completata è immutabile")
                if (
                    row.parser_versions != parser_identities()
                    or row.policy_version != DISCOUNT_ANALYSIS_VERSION
                ):
                    raise AnalysisError(
                        "Versioni cambiate: creare una nuova rianalisi dello storico"
                    )
                if row.input_metadata.get("pipeline_configuration") != pipeline_configuration(
                    self.settings
                ):
                    raise AnalysisError("Configurazione cambiata: creare una nuova rianalisi")
                history = list((row.checkpoints or {}).get("attempts", []))
                history.append({"failed_at": row.completed_at.isoformat(), "error": row.error})
                row.checkpoints = {**(row.checkpoints or {}), "attempts": history}
                row.status, row.stage, row.active_slot = "queued", "queued", 1
                row.error, row.completed_at = None, None
                session.flush()
                return AnalysisView.model_validate(row)
        except IntegrityError as exc:
            raise AnalysisError("Un'altra rianalisi è già attiva") from exc

    def results(
        self,
        identifier: UUID,
        *,
        limit: int,
        offset: int,
        period_from: datetime | None = None,
        period_to: datetime | None = None,
        device_id: str | None = None,
        kind: str | None = None,
        reconciliation: str | None = None,
        with_discounts: bool = False,
    ) -> tuple[list[AnalysisResultView], int]:
        # Frozen JSON is deliberately portable across MariaDB/SQLite. It contains
        # only compact discount results, never hydration of RAW or receipt lines.
        with self.factory() as session:
            if session.get(AnalysisRequest, identifier) is None:
                raise FileNotFoundError(identifier)
            rows = session.execute(
                select(AnalysisResult.result, AnalysisResult.result_sha256)
                .where(AnalysisResult.analysis_id == identifier)
                .order_by(AnalysisResult.sequence)
            )
            selected: list[AnalysisResultView] = []
            count = 0
            for payload, checksum in rows:
                if digest(payload) != checksum:
                    raise AnalysisError("Integrità del risultato non verificata")
                value = AnalysisResultView.model_validate(payload)
                if with_discounts and not value.observations:
                    continue
                if device_id and value.device_id != device_id:
                    continue
                if period_from and value.captured_at < period_from:
                    continue
                if period_to and value.captured_at >= period_to:
                    continue
                if kind and not any(item.kind == kind for item in value.observations):
                    continue
                if reconciliation and value.arithmetic_reconciled != (reconciliation == "verified"):
                    continue
                if offset <= count < offset + limit:
                    selected.append(value)
                count += 1
            return selected, count

    def download(self, identifier: UUID, *, format: str) -> bytes:
        with self.factory() as session:
            row = session.get(AnalysisRequest, identifier)
            if row is None:
                raise FileNotFoundError(identifier)
            if row.status != "completed":
                raise AnalysisError("Il report sarà disponibile al completamento")
            report = AnalysisView.model_validate(row).model_dump(mode="json")
            inputs = []
            for item in session.scalars(
                select(AnalysisInput)
                .where(AnalysisInput.analysis_id == identifier)
                .order_by(AnalysisInput.sequence)
            ):
                if digest(item.snapshot) != item.snapshot_sha256:
                    raise AnalysisError("Integrità degli input non verificata")
                inputs.append(
                    {
                        "snapshot": item.snapshot,
                        "sha256": item.snapshot_sha256,
                        "status": item.status,
                        "outcome": item.outcome,
                    }
                )
            results = []
            for item in session.scalars(
                select(AnalysisResult)
                .where(AnalysisResult.analysis_id == identifier)
                .order_by(AnalysisResult.sequence)
            ):
                if (
                    digest(item.snapshot) != item.snapshot_sha256
                    or digest(item.result) != item.result_sha256
                ):
                    raise AnalysisError("Integrità dello snapshot non verificata")
                results.append(
                    {
                        "snapshot": item.snapshot,
                        "snapshot_sha256": item.snapshot_sha256,
                        "result": item.result,
                        "result_sha256": item.result_sha256,
                    }
                )
        if format == "json":
            return canonical_json(
                {"schema_version": 1, "analysis": report, "inputs": inputs, "results": results}
            )
        if format != "csv":
            raise AnalysisError("Formato report non supportato")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "analysis_id",
                "document_id",
                "document_version_id",
                "device_id",
                "captured_at",
                "document_timestamp",
                "kind",
                "sequence",
                "label",
                "amount",
                "rate_percent",
                "basis",
                "arithmetic_verified",
                "analysis_excluded",
                "policy_version",
                "document_arithmetic_reconciled",
                "document_complete",
                "document_type",
                "reasons",
                "reprocessed_from_raw",
                "reprocessing_note",
                "source_text",
                "semantics",
            ]
        )
        for item in results:
            result = item["result"]
            for observation in result["observations"]:
                writer.writerow(
                    [
                        _safe_csv(value)
                        for value in (
                            str(identifier),
                            result["document_id"],
                            result["document_version_id"],
                            result["device_id"],
                            result["captured_at"],
                            result["document_timestamp"],
                            observation["kind"],
                            observation["sequence"],
                            observation["label"],
                            observation["amount"],
                            observation["rate_percent"],
                            observation["basis"],
                            observation["arithmetic_verified"],
                            result["analysis_excluded"],
                            row.policy_version,
                            result["arithmetic_reconciled"],
                            result["complete"],
                            result["document_type"],
                            ";".join(result["reasons"]),
                            result["reprocessed_from_raw"],
                            result["reprocessing_note"],
                            observation["source_text"],
                            "observed_discount_line; arithmetic_is_not_authorization",
                        )
                    ]
                )
        return output.getvalue().encode("utf-8-sig")


def json_safe(value: Any) -> Any:
    """Canonical, lossless Decimal encoding for persisted observations."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))
