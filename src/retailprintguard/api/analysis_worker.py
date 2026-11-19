"""Database-only reanalysis pipeline; no replay, printer connection or beeper."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, func, select

from retailprintguard.analysis.discounts import (
    DISCOUNT_ANALYSIS_VERSION,
)
from retailprintguard.api.analyses import (
    AnalysisError,
    AnalysisService,
    digest,
    json_safe,
    parser_identities,
    pipeline_configuration,
)
from retailprintguard.api.discounts import analyze_sale_document_discounts
from retailprintguard.common.config import Settings, load_settings
from retailprintguard.common.logging import configure_structured_logging
from retailprintguard.common.pipeline_lock import PipelineLease, pipeline_lock
from retailprintguard.correlation.worker import (
    CorrelationWorker,
    _document_query,
    activate_parser_version,
)
from retailprintguard.db.models import (
    AnalysisInput,
    AnalysisRequest,
    AnalysisResult,
    AnalysisWatermark,
    Document,
    DocumentVersion,
    ParserVersion,
    RawPayload,
    SystemEvent,
)
from retailprintguard.db.session import create_db_engine, session_factory
from retailprintguard.fraud.worker import FraudWorker
from retailprintguard.parser.repository import ParserRepositoryError, SqlAlchemyParserRepository
from retailprintguard.parser.worker import ParserWorker


class FrozenJobRepository(SqlAlchemyParserRepository):
    """Restrict the existing parser to a single hash-verified frozen acquisition."""

    def __init__(self, factory: Any, item: AnalysisInput) -> None:
        super().__init__(factory)
        self.item = item

    def pending_jobs(self, *, limit: int, reparse: bool = False) -> tuple[str, ...]:
        if not reparse or limit != 1:
            raise ValueError("historical reanalysis requires explicit single-job reparse")
        return (str(self.item.job_id),)

    def load_job(self, job_id: str) -> dict[str, Any] | None:
        if (
            job_id != str(self.item.job_id)
            or digest(self.item.snapshot) != self.item.snapshot_sha256
        ):
            raise ParserRepositoryError("frozen input identity differs")
        source = super().load_job(job_id)
        if source is None:
            raise ParserRepositoryError("frozen job is unavailable")
        snapshot = self.item.snapshot
        for name in (
            "job_id",
            "source_job_id",
            "session_id",
            "device_id",
            "parser_kind",
            "manifest_sha256",
        ):
            if source[name] != snapshot[name]:
                raise ParserRepositoryError("frozen source metadata differs")
        if source["captured_at"].isoformat() != snapshot["captured_at"]:
            raise ParserRepositoryError("frozen capture timestamp differs")
        with self._factory() as session:
            payloads = [
                {"id": str(identifier), "role": role, "sha256": sha, "source_path": path}
                for identifier, role, sha, path in session.execute(
                    select(
                        RawPayload.id,
                        RawPayload.artifact_role,
                        RawPayload.sha256,
                        RawPayload.source_path,
                    )
                    .where(RawPayload.job_id == self.item.job_id)
                    .order_by(RawPayload.id)
                )
            ]
        if payloads != snapshot["raw_payloads"]:
            raise ParserRepositoryError("frozen RAW membership differs")
        return source

    def store_documents(self, job_id: str, documents: Any) -> int:
        # A crash after the existing empty-result transaction must not append
        # the same no-document event again on restart.
        if not documents:
            identity = parser_identities()[self.item.snapshot["parser_kind"]]
            with self._factory() as session:
                already_empty = session.scalar(
                    select(SystemEvent.id)
                    .where(
                        SystemEvent.job_id == self.item.job_id,
                        SystemEvent.service == "parser",
                        SystemEvent.event_type == "PARSER_NO_DOCUMENTS",
                        SystemEvent.correlation_id == identity["build_sha256"],
                    )
                    .limit(1)
                )
            if already_empty is not None:
                return 0
        return super().store_documents(job_id, documents)


class AnalysisRunner:
    def __init__(
        self, service: AnalysisService, engine: Engine, settings: Settings | None = None
    ) -> None:
        self.service, self.engine, self.settings = service, engine, settings
        self.factory = service.factory

    def _checkpoint(self, identifier: UUID, stage: str, **values: Any) -> None:
        with self.factory.begin() as session:
            row = session.get(AnalysisRequest, identifier)
            row.stage = stage
            row.checkpoints = {**row.checkpoints, **json_safe(values)}

    def _view(self, identifier: UUID) -> Any:
        value = self.service.get(identifier)
        if value is None:
            raise AnalysisError("Richiesta non disponibile")
        return value

    def run_one(self, *, stop: Event | None = None) -> bool:
        with pipeline_lock(self.engine, wait_seconds=5) as lease:
            if lease is None:
                return False
            lease.check()
            with self.factory.begin() as session:
                row = session.scalar(
                    select(AnalysisRequest)
                    .where(AnalysisRequest.status.in_(("queued", "running")))
                    .order_by(AnalysisRequest.created_at, AnalysisRequest.id)
                    .with_for_update()
                    .limit(1)
                )
                if row is None:
                    return False
                identifier = row.id
                row.status = "running"
                row.started_at = row.started_at or datetime.now(UTC)
            try:
                self._execute(identifier, lease, stop or Event())
            except Exception as exc:
                # Database errors can embed SQL/parameters. Persist only a safe
                # classification unless it is one of our explicit diagnostics.
                message = (
                    str(exc)
                    if isinstance(exc, AnalysisError)
                    else (f"Fase interrotta ({type(exc).__name__}); input e progressi conservati")
                )
                with self.factory.begin() as session:
                    row = session.get(AnalysisRequest, identifier)
                    row.checkpoints = {**row.checkpoints, "failed_stage": row.stage}
                    row.status, row.stage, row.active_slot = "failed", "failed", None
                    row.error, row.completed_at = message[:1024], datetime.now(UTC)
            return True

    def _execute(self, identifier: UUID, lease: PipelineLease, stop: Event) -> None:
        view = self._view(identifier)
        if (
            view.parser_versions != parser_identities()
            or view.policy_version != DISCOUNT_ANALYSIS_VERSION
        ):
            raise AnalysisError("Versioni cambiate: creare una nuova rianalisi dello storico")
        if view.input_metadata.get("pipeline_configuration") != pipeline_configuration(
            self.settings
        ):
            raise AnalysisError("Configurazione cambiata: creare una nuova rianalisi")
        checksum = hashlib.sha256()
        with self.factory() as session:
            inputs = session.scalars(
                select(AnalysisInput)
                .where(AnalysisInput.analysis_id == identifier)
                .order_by(AnalysisInput.sequence)
            ).all()
            if len(inputs) != view.total_jobs:
                raise AnalysisError("Appartenenza degli input non verificata")
            for item in inputs:
                if digest(item.snapshot) != item.snapshot_sha256:
                    raise AnalysisError("Integrità dello snapshot degli input non verificata")
                checksum.update(bytes.fromhex(item.snapshot_sha256))
        if checksum.hexdigest() != view.input_metadata["input_sha256"]:
            raise AnalysisError("Impronta degli input non verificata")
        if not view.checkpoints.get("parsing_complete"):
            self._checkpoint(identifier, "parsing")
            self._parse(identifier, lease, stop)
            if stop.is_set():
                return
            view = self._view(identifier)
            if view.failed_jobs:
                raise AnalysisError(
                    f"{view.failed_jobs} acquisizioni non rielaborate; nessun parser attivato"
                )
            self._checkpoint(identifier, "parsing", parsing_complete=True)
        lease.check()
        if stop.is_set():
            return
        self._activate(identifier, lease)
        if not self._view(identifier).checkpoints.get("correlation_complete"):
            self._correlate(identifier, lease, stop)
            if stop.is_set():
                return
        lease.check()
        if not self._view(identifier).checkpoints.get("fraud_complete"):
            self._checkpoint(identifier, "fraud")
            options = {}
            if self.settings:
                options = {
                    "minimum_score": self.settings.correlation.minimum_score,
                    "time_window_seconds": self.settings.correlation.time_window_seconds,
                    "default_amount_drop_percent": self.settings.fraud.default_amount_drop_percent,
                    "order_without_fiscal_close_minutes": (
                        self.settings.fraud.order_without_fiscal_close_minutes
                    ),
                    "extreme_price_change_percent": (
                        self.settings.fraud.extreme_price_change_percent
                    ),
                }
            report = FraudWorker(self.factory, **options).run_once(max_transactions=None)
            lease.check()
            self._checkpoint(identifier, "fraud", fraud_complete=True, fraud_report=asdict(report))
        if stop.is_set():
            return
        self._discounts(identifier, lease, stop)
        if stop.is_set():
            return
        lease.check()
        self._complete(identifier)

    def _parse(self, identifier: UUID, lease: PipelineLease, stop: Event) -> None:
        with self.factory() as session:
            ids = list(
                session.scalars(
                    select(AnalysisInput.job_id)
                    .where(
                        AnalysisInput.analysis_id == identifier,
                        AnalysisInput.status != "completed",
                    )
                    .order_by(AnalysisInput.sequence)
                )
            )
        for job_id in ids:
            if stop.is_set():
                return
            lease.check()
            with self.factory() as session:
                item = session.get(AnalysisInput, (identifier, job_id))
                session.expunge(item)
            if digest(item.snapshot) != item.snapshot_sha256:
                raise AnalysisError("Integrità dello snapshot degli input non verificata")
            has_raw = any(value["role"] == "REQUEST_RAW" for value in item.snapshot["raw_payloads"])
            if not has_raw:
                outcome = {
                    "skipped": "NO_REQUEST_RAW",
                    "versions": [
                        value
                        for value in item.snapshot["previous_versions"]
                        if value["version_id"] is not None
                    ],
                }
                successful = True
            else:
                repository = FrozenJobRepository(self.factory, item)
                report = ParserWorker(
                    repository,
                    timezone_name=(self.settings.timezone if self.settings else "Europe/Rome"),
                    beeper=None,
                ).run_once(limit=1, reparse=True)
                successful = report.failed == 0 and report.parsed_jobs == 1
                outcome = {"parser_report": asdict(report), "versions": []}
                if successful:
                    identity = self._view(identifier).parser_versions[item.snapshot["parser_kind"]]
                    with self.factory() as session:
                        versions = session.execute(
                            select(Document.id, DocumentVersion.id)
                            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                            .join(
                                ParserVersion, ParserVersion.id == DocumentVersion.parser_version_id
                            )
                            .where(
                                Document.job_id == job_id,
                                ParserVersion.name == identity["name"],
                                ParserVersion.version == identity["version"],
                                ParserVersion.build_sha256 == identity["build_sha256"],
                            )
                            .order_by(Document.id)
                        ).all()
                    outcome["versions"] = [
                        {"document_id": str(doc), "version_id": str(version)}
                        for doc, version in versions
                    ]
            lease.check()
            with self.factory.begin() as session:
                stored = session.get(AnalysisInput, (identifier, job_id))
                stored.status = "completed" if successful else "failed"
                stored.outcome = outcome
                session.flush()
                row = session.get(AnalysisRequest, identifier)
                counts = dict(
                    session.execute(
                        select(AnalysisInput.status, func.count())
                        .where(AnalysisInput.analysis_id == identifier)
                        .group_by(AnalysisInput.status)
                    ).all()
                )
                row.completed_jobs = counts.get("completed", 0)
                row.failed_jobs = counts.get("failed", 0)

    def _activate(self, identifier: UUID, lease: PipelineLease) -> None:
        view = self._view(identifier)
        if view.checkpoints.get("activation_complete"):
            return
        with self.factory() as session:
            kinds = {
                item.snapshot["parser_kind"]
                for item in session.scalars(
                    select(AnalysisInput).where(AnalysisInput.analysis_id == identifier)
                )
                if item.outcome and item.outcome.get("versions") and not item.outcome.get("skipped")
            }
            actor_id = session.get(AnalysisRequest, identifier).requested_by_user_id
        activated = {}
        for kind in sorted(kinds):
            lease.check()
            identity = view.parser_versions[kind]
            activated[kind] = str(
                activate_parser_version(
                    self.factory,
                    parser_name=identity["name"],
                    parser_version=identity["version"],
                    build_sha256=identity["build_sha256"],
                    reason=f"Whole-history analysis {identifier}",
                    actor_user_id=actor_id,
                )
            )
        # Re-evaluate the full normalized history even if all RAW jobs are empty.
        with self.factory.begin() as session:
            watermark = session.get(AnalysisWatermark, "correlation")
            if watermark is not None:
                session.delete(watermark)
        self._checkpoint(
            identifier, "correlation", activation_complete=True, activated_parsers=activated
        )

    def _correlate(self, identifier: UUID, lease: PipelineLease, stop: Event) -> None:
        self._checkpoint(identifier, "correlation")
        options = {}
        if self.settings:
            options = {
                "minimum_score": self.settings.correlation.minimum_score,
                "time_window_seconds": self.settings.correlation.time_window_seconds,
                "timezone": self.settings.timezone,
            }
        if stop.is_set():
            return
        lease.check()
        with self.factory.begin() as session:
            # A historical pass must not be starved by non-sale lookback seeds
            # which intentionally never receive a sale-correlation membership.
            # Read the entire eligible cursor set, then use an explicit limit
            # large enough for all seeds. Never treat a stable cursor as proof.
            source = (
                _document_query()
                .order_by(None)
                .with_only_columns(DocumentVersion.parsed_at, DocumentVersion.id)
            )
            expected = session.execute(source).all()
            if len(expected) > 500_000:
                raise AnalysisError(
                    "Storico correlazioni oltre il limite; nessun risultato troncato"
                )
            expected_cursor = max(expected) if expected else None
            watermark = session.get(AnalysisWatermark, "correlation")
            if watermark is not None:
                session.delete(watermark)
        report = CorrelationWorker(self.factory, **options).run_once(
            max_documents=max(1, len(expected) + 1)
        )
        lease.check()
        with self.factory() as session:
            cursor = session.get(AnalysisWatermark, "correlation")
            actual_cursor = (cursor.cursor_timestamp, cursor.cursor_id) if cursor else None
        if actual_cursor != expected_cursor or report.documents_loaded < len(expected):
            raise AnalysisError("Copertura della correlazione storica non verificata")
        self._checkpoint(
            identifier,
            "correlation",
            correlation_report=asdict(report),
            correlation_complete=True,
            correlation_source_documents=len(expected),
            correlation_final_cursor=[str(value) for value in expected_cursor]
            if expected_cursor
            else None,
        )

    def _discounts(self, identifier: UUID, lease: PipelineLease, stop: Event) -> None:
        self._checkpoint(identifier, "discounts")
        with self.factory() as session:
            membership = []
            for item in session.scalars(
                select(AnalysisInput).where(AnalysisInput.analysis_id == identifier)
            ):
                for version in (item.outcome or {}).get("versions", []):
                    membership.append(
                        (
                            UUID(version["document_id"]),
                            UUID(version["version_id"]),
                            bool(item.snapshot["analysis_excluded"]),
                            not bool(item.outcome.get("skipped")),
                        )
                    )
            existing = set(
                session.scalars(
                    select(AnalysisResult.document_id).where(
                        AnalysisResult.analysis_id == identifier
                    )
                )
            )
        membership.sort(key=lambda value: str(value[0]))
        if len({values[0] for values in membership}) != len(membership):
            raise AnalysisError("Più versioni per uno stesso documento nel parser selezionato")
        with self.factory.begin() as session:
            session.get(AnalysisRequest, identifier).total_documents = len(membership)
        pending = [
            (sequence, values)
            for sequence, values in enumerate(membership, 1)
            if values[0] not in existing
        ]
        for offset in range(0, len(pending), 50):
            if stop.is_set():
                return
            lease.check()
            batch = pending[offset : offset + 50]
            with self.factory.begin() as session:
                documents = session.scalars(
                    select(Document).where(Document.id.in_([values[0] for _, values in batch]))
                ).all()
                views = self.service.api._document_views(
                    session, documents, version_ids=[values[1] for _, values in batch]
                )
                by_id = {view.id: view for view in views}
                if len(by_id) != len(batch):
                    raise AnalysisError("Interpretazione congelata non disponibile")
                for sequence, (document_id, version_id, excluded, reprocessed) in batch:
                    lease.check()
                    view = by_id[document_id]
                    analysis = analyze_sale_document_discounts(view)
                    result = json_safe(
                        {
                            "analysis_id": str(identifier),
                            "document_id": str(document_id),
                            "document_version_id": str(version_id),
                            "device_id": view.device_id,
                            "captured_at": view.captured_at.isoformat(),
                            "document_timestamp": view.document_timestamp.isoformat()
                            if view.document_timestamp
                            else None,
                            "document_type": view.type,
                            "complete": view.complete,
                            "analysis_excluded": excluded,
                            "reprocessed_from_raw": reprocessed,
                            "reprocessing_note": None if reprocessed else "NO_REQUEST_RAW",
                            "parser_name": view.parser_name,
                            "parser_version": view.parser_version,
                            **asdict(analysis),
                        }
                    )
                    snapshot = view.model_dump(mode="json")
                    session.add(
                        AnalysisResult(
                            analysis_id=identifier,
                            document_id=document_id,
                            document_version_id=version_id,
                            sequence=sequence,
                            snapshot=snapshot,
                            snapshot_sha256=digest(snapshot),
                            result=result,
                            result_sha256=digest(result),
                        )
                    )
                session.flush()
                row = session.get(AnalysisRequest, identifier)
                row.completed_documents = (
                    session.scalar(
                        select(func.count())
                        .select_from(AnalysisResult)
                        .where(AnalysisResult.analysis_id == identifier)
                    )
                    or 0
                )

    def _complete(self, identifier: UUID) -> None:
        totals = {
            key: Decimal("0")
            for key in (
                "employee_total",
                "generic_30_total",
                "other_discount_total",
                "employee_arithmetic_verified_total",
                "generic_30_arithmetic_verified_total",
            )
        }
        counts = {
            key: 0
            for key in (
                "observed_rows",
                "documents_with_discounts",
                "arithmetic_verified_rows",
                "arithmetic_unverified_rows",
                "excluded_documents_with_discounts",
                "documents_without_raw_reprocessing",
            )
        }
        output_hash = hashlib.sha256()
        with self.factory.begin() as session:
            previous_documents = set()
            output_documents = set()
            for source in session.scalars(
                select(AnalysisInput).where(AnalysisInput.analysis_id == identifier)
            ):
                previous_documents.update(
                    item["document_id"] for item in source.snapshot["previous_versions"]
                )
                output_documents.update(
                    item["document_id"] for item in (source.outcome or {}).get("versions", [])
                )
            for item in session.scalars(
                select(AnalysisResult)
                .where(AnalysisResult.analysis_id == identifier)
                .order_by(AnalysisResult.sequence)
            ):
                if (
                    digest(item.result) != item.result_sha256
                    or digest(item.snapshot) != item.snapshot_sha256
                ):
                    raise AnalysisError("Integrità dei risultati non verificata")
                output_hash.update(bytes.fromhex(item.result_sha256))
                result = item.result
                observations = result["observations"]
                counts["documents_without_raw_reprocessing"] += int(
                    not result["reprocessed_from_raw"]
                )
                counts["documents_with_discounts"] += int(bool(observations))
                counts["excluded_documents_with_discounts"] += int(
                    bool(observations) and result["analysis_excluded"]
                )
                for key in ("employee_total", "generic_30_total", "other_discount_total"):
                    totals[key] += Decimal(result[key])
                for observation in observations:
                    verified = observation["arithmetic_verified"]
                    counts["observed_rows"] += 1
                    counts[
                        "arithmetic_verified_rows" if verified else "arithmetic_unverified_rows"
                    ] += 1
                    if (
                        verified
                        and result["arithmetic_reconciled"]
                        and observation["kind"] in {"employee_30", "generic_30"}
                    ):
                        key = (
                            "employee_arithmetic_verified_total"
                            if observation["kind"] == "employee_30"
                            else "generic_30_arithmetic_verified_total"
                        )
                        totals[key] += Decimal(observation["amount"])
            row = session.get(AnalysisRequest, identifier)
            if (
                row.failed_jobs
                or row.completed_jobs != row.total_jobs
                or row.completed_documents != row.total_documents
            ):
                raise AnalysisError("Rianalisi incompleta; report non pubblicato")
            row.summary = json_safe(
                {
                    **totals,
                    **counts,
                    "result_sha256": output_hash.hexdigest(),
                    "totals_basis": "observed_document_lines",
                    "authorization_basis": "not_evaluated_by_discount_arithmetic",
                    "previous_documents_without_current_output": len(
                        previous_documents - output_documents
                    ),
                    "jobs_without_request_raw": row.input_metadata["jobs_without_request_raw"],
                }
            )
            row.status, row.stage, row.active_slot = "completed", "completed", None
            row.completed_at, row.error = datetime.now(UTC), None


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RetailPrintGuard persistent RAW reanalysis worker"
    )
    parser.add_argument(
        "--config", default=os.environ.get("RPG_CONFIG", "/etc/retailprintguard/config.yaml")
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    runtime = configure_structured_logging("analysis")
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    engine = None
    try:
        settings = load_settings(args.config)
        engine = create_db_engine(
            settings.database_url().get_secret_value(), pool_size=4, max_overflow=1
        )
        runner = AnalysisRunner(
            AnalysisService(session_factory(engine), settings), engine, settings
        )
        while not stop.is_set():
            try:
                processed = runner.run_one(stop=stop)
            except Exception:
                logging.getLogger(__name__).error("analysis_worker_database_unavailable")
                if args.once:
                    return 1
                stop.wait(5)
                continue
            if args.once:
                break
            if not processed:
                stop.wait(2)
    finally:
        if engine is not None:
            engine.dispose()
        runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
