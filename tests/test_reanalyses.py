from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_db_repository import _seed_api
from test_parser_worker_db import NOW, _database, _rch_frame

from retailprintguard.api.analyses import AnalysisCreate, AnalysisError, AnalysisService, digest
from retailprintguard.api.analysis_worker import AnalysisRunner, FrozenJobRepository
from retailprintguard.api.auth import TokenService
from retailprintguard.api.main import create_app
from retailprintguard.api.schemas import RoleName, UserPrincipal
from retailprintguard.db import Base, create_db_engine, session_factory
from retailprintguard.db.models import (
    ActiveParserVersion,
    AnalysisInput,
    AnalysisRequest,
    AnalysisResult,
    Document,
    DocumentVersion,
    ParserVersion,
    PrintJob,
    RawPayload,
    User,
)
from retailprintguard.db.repository import SqlAlchemyApiRepository
from retailprintguard.parser import rch
from retailprintguard.parser.repository import ParserRepositoryError, SqlAlchemyParserRepository
from retailprintguard.parser.worker import ParserWorker


def management_raw(title="CONTO SINTETICO"):
    lines = [
        title,
        "8x 2,00",
        "Prodotto sintetico      16,00",
        "Sconto DIPENDENTI 30%   -4,80",
        "TOT                    11,20",
    ]
    return b"".join(
        [
            _rch_frame("=o"),
            *[
                _rch_frame(f'="/({line})', sequence=str(index % 10))
                for index, line in enumerate(lines, 1)
            ],
            _rch_frame("=o"),
        ]
    )


@pytest.fixture
def analysis_case(monkeypatch):
    engine, factory, job, request, response = _database(
        parser_kind="rch_observed", request=management_raw()
    )
    user_id = uuid4()
    with factory.begin() as session:
        session.add(
            User(
                id=user_id,
                username="synthetic_auditor",
                display_name="Synthetic",
                password_hash="not-a-login-hash",
            )
        )
    # Preserve an older, intentionally activated interpretation for comparison.
    original = rch.PARSER_VERSION
    monkeypatch.setattr(rch, "PARSER_VERSION", "0.0.synthetic")
    assert ParserWorker(SqlAlchemyParserRepository(factory)).run_once().failed == 0
    monkeypatch.setattr(rch, "PARSER_VERSION", original)
    with factory.begin() as session:
        parser = session.scalar(select(ParserVersion))
        session.add(
            ActiveParserVersion(
                parser_name=parser.name,
                parser_version_id=parser.id,
                activation_reason="synthetic previous build",
            )
        )
    actor = UserPrincipal(id=user_id, username="synthetic_auditor", roles=(RoleName.AUDITOR,))
    service = AnalysisService(factory)
    yield service, AnalysisRunner(service, engine), factory, actor, job, request
    engine.dispose()


def test_real_raw_reanalysis_preserves_prior_versions_and_freezes_results(analysis_case):
    service, runner, factory, actor, job, request = analysis_case
    with factory() as session:
        old = session.scalar(select(DocumentVersion))
        old_id, old_hash = old.id, old.record_hash
        raw_before = bytes(session.get(RawPayload, request).payload)
    queued = service.create(AnalysisCreate(), actor)
    assert queued.status == "queued" and queued.total_jobs == 1
    with pytest.raises(AnalysisError, match="già"):
        service.create(AnalysisCreate(), actor)
    assert runner.run_one()
    result = service.get(queued.id)
    assert result.status == "completed", result.error
    assert result.completed_jobs == result.total_jobs == 1
    assert result.completed_documents == result.total_documents == 1
    assert result.summary["employee_total"] == "4.8000"
    assert result.summary["arithmetic_verified_rows"] == 1
    with factory() as session:
        assert session.get(DocumentVersion, old_id).record_hash == old_hash
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 2
        assert bytes(session.get(RawPayload, request).payload) == raw_before
        frozen = session.scalar(select(AnalysisResult))
        assert frozen.document_version_id != old_id
        assert frozen.result["parser_version"] == rch.PARSER_VERSION
        assert (
            session.scalar(select(ActiveParserVersion)).parser_version_id != old.parser_version_id
        )
        input_item = session.scalar(select(AnalysisInput))
        assert input_item.snapshot["previous_versions"][0]["version_id"] == str(old_id)
    payload = service.download(queued.id, format="json")
    assert payload == service.download(queued.id, format="json")
    report = json.loads(payload)
    assert report["results"][0]["result"]["observations"][0]["kind"] == "employee_30"
    assert report["inputs"][0]["snapshot"]["job_id"] == str(job)
    csv_payload = service.download(queued.id, format="csv").decode("utf-8-sig")
    assert "employee_30" in csv_payload and "document_version_id" in csv_payload
    assert "document_arithmetic_reconciled" in csv_payload
    assert "arithmetic_is_not_authorization" in csv_payload
    with pytest.raises(AnalysisError, match="immutabile"):
        service.retry(queued.id)
    # A subsequent full RAW run is idempotent for the same build, with its own
    # preserved audit result. Altered current projections cannot change reports.
    second = service.create(AnalysisCreate(), actor)
    assert runner.run_one() and service.get(second.id).status == "completed"
    with factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 2
        session.scalar(select(Document)).order_code = "later-current-projection"
    assert service.download(queued.id, format="json") == payload


def test_parse_failure_keeps_active_parser_and_retry_keeps_input(analysis_case, monkeypatch):
    service, runner, factory, actor, _, _ = analysis_case
    with factory() as session:
        active = session.scalar(select(ActiveParserVersion)).parser_version_id
    queued = service.create(AnalysisCreate(), actor)
    original = FrozenJobRepository.load_job

    def fail(_self, _job):
        raise ParserRepositoryError("synthetic failure")

    monkeypatch.setattr(FrozenJobRepository, "load_job", fail)
    assert runner.run_one()
    failed = service.get(queued.id)
    assert failed.status == "failed" and failed.failed_jobs == 1
    with factory() as session:
        assert session.scalar(select(ActiveParserVersion)).parser_version_id == active
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 1
    with pytest.raises(AnalysisError, match="completamento"):
        service.download(queued.id, format="json")
    monkeypatch.setattr(FrozenJobRepository, "load_job", original)
    assert service.retry(queued.id).status == "queued"
    assert runner.run_one()
    completed = service.get(queued.id)
    assert completed.status == "completed", completed.error
    assert completed.input_metadata == queued.input_metadata
    assert len(completed.checkpoints["attempts"]) == 1


def test_raw_integrity_failure_never_activates_and_input_filters_are_rejected(analysis_case):
    service, runner, factory, actor, _, request = analysis_case
    with pytest.raises(ValueError):
        AnalysisCreate(scope="date_range")
    with pytest.raises(ValueError):
        AnalysisCreate(device_ids=["synthetic"])
    queued = service.create(AnalysisCreate(), actor)
    with factory.begin() as session:
        raw = session.get(RawPayload, request)
        raw.payload = b"tampered synthetic RAW"
    assert runner.run_one()
    assert service.get(queued.id).status == "failed"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 1


def test_restart_uses_frozen_inputs_and_excludes_later_ingestion(analysis_case):
    service, runner, factory, actor, job, _ = analysis_case
    queued = service.create(AnalysisCreate(), actor)
    with factory.begin() as session:
        original = session.get(PrintJob, job)
        values = {
            column.name: getattr(original, column.name) for column in PrintJob.__table__.columns
        }
        values.update(id=uuid4(), source_key="later-synthetic-job", source_job_id="later-synthetic")
        session.add(PrintJob(**values))
    stop = Event()
    stop.set()
    assert runner.run_one(stop=stop)
    assert service.get(queued.id).status == "running"
    assert runner.run_one()
    assert service.get(queued.id).status == "completed"
    assert service.get(queued.id).total_jobs == 1
    following = service.create(AnalysisCreate(), actor)
    assert following.total_jobs == 2
    assert following.input_metadata["jobs_without_request_raw"] == 1
    assert runner.run_one()
    assert service.get(following.id).status == "completed"
    with factory() as session:
        items = session.scalars(
            select(AnalysisInput).where(AnalysisInput.analysis_id == following.id)
        ).all()
        assert sum(item.outcome.get("skipped") == "NO_REQUEST_RAW" for item in items) == 1


def test_results_filter_before_pagination_and_checksums(analysis_case):
    service, runner, factory, actor, _, _ = analysis_case
    queued = service.create(AnalysisCreate(), actor)
    assert runner.run_one()
    rows, total = service.results(
        queued.id, limit=1, offset=0, kind="employee_30", reconciliation="verified"
    )
    assert total == 1 and rows[0].device_id == "device_synthetic"
    assert service.results(queued.id, limit=1, offset=1, kind="employee_30")[1] == 1
    assert service.results(queued.id, limit=1, offset=0, kind="generic_30")[1] == 0
    assert service.results(queued.id, limit=1, offset=0, device_id="absent")[1] == 0
    assert (
        service.results(queued.id, limit=1, offset=0, period_from=NOW + timedelta(days=1))[1] == 0
    )
    assert service.results(queued.id, limit=1, offset=0, period_to=NOW)[1] == 0
    with factory.begin() as session:
        item = session.scalar(select(AnalysisResult))
        item.result = {**item.result, "employee_total": "999"}
    with pytest.raises(AnalysisError, match="Integrità"):
        service.results(queued.id, limit=1, offset=0)
    with pytest.raises(AnalysisError, match="Integrità"):
        service.download(queued.id, format="json")


def test_retry_rejects_changed_build_and_competing_run(analysis_case, monkeypatch):
    service, runner, factory, actor, _, _ = analysis_case
    queued = service.create(AnalysisCreate(), actor)
    with factory.begin() as session:
        row = session.get(AnalysisRequest, queued.id)
        row.status, row.stage, row.active_slot = "failed", "failed", None
        row.completed_at = NOW
    other = service.create(AnalysisCreate(), actor)
    with pytest.raises(AnalysisError, match="attiva"):
        service.retry(queued.id)
    with factory.begin() as session:
        row = session.get(AnalysisRequest, other.id)
        row.status, row.stage, row.active_slot = "failed", "failed", None
    monkeypatch.setattr(rch, "PARSER_VERSION", "future.synthetic")
    with pytest.raises(AnalysisError, match="Versioni"):
        service.retry(queued.id)


def test_report_csv_neutralizes_formula_and_pins_all_observation_data(analysis_case):
    service, runner, factory, actor, _, _ = analysis_case
    queued = service.create(AnalysisCreate(), actor)
    runner.run_one()
    with factory.begin() as session:
        item = session.scalar(select(AnalysisResult))
        result = json.loads(json.dumps(item.result))
        result["observations"][0]["label"] = "   =SYNTHETIC()"
        item.result, item.result_sha256 = result, digest(result)
    payload = service.download(queued.id, format="csv").decode("utf-8-sig")
    assert "'   =SYNTHETIC()" in payload


def test_api_roles_creation_progress_report_and_timezone_validation(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'analysis-api.db'}")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    ids = _seed_api(factory)
    with factory.begin() as session:
        for raw in session.scalars(select(RawPayload)):
            raw.sha256 = hashlib.sha256(raw.payload).hexdigest()
    service = AnalysisService(factory)
    secret = b"synthetic-analysis-test-secret-000000000000000000000"
    client = TestClient(
        create_app(
            repository=SqlAlchemyApiRepository(factory), jwt_secret=secret, analysis_service=service
        )
    )

    def headers(role):
        actor = UserPrincipal(id=ids["user"], username="auditor", roles=(role,))
        return {"Authorization": "Bearer " + TokenService(secret, lifetime_minutes=30).issue(actor)}

    # inspect the established token service contract, without calling login.
    auditor = headers(RoleName.AUDITOR)
    for role in (RoleName.OPERATOR, RoleName.READ_ONLY):
        assert (
            client.post(
                "/api/v1/analyses", json={"scope": "all_history"}, headers=headers(role)
            ).status_code
            == 403
        )
    assert (
        client.post("/api/v1/analyses", json={"scope": "date_range"}, headers=auditor).status_code
        == 422
    )
    response = client.post("/api/v1/analyses", json={"scope": "all_history"}, headers=auditor)
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert client.get("/api/v1/analyses", headers=auditor).json()["total"] == 1
    assert (
        client.get(f"/api/v1/analyses/{identifier}", headers=auditor).json()["status"] == "queued"
    )
    assert client.post(f"/api/v1/analyses/{identifier}/retry", headers=auditor).status_code == 202
    assert client.get(f"/api/v1/analyses/{identifier}/download", headers=auditor).status_code == 409
    assert (
        client.get(
            f"/api/v1/analyses/{identifier}/results?from=2042-01-01", headers=auditor
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/analyses/{identifier}/results?kind=invalid", headers=auditor
        ).status_code
        == 422
    )
    assert client.get(f"/api/v1/analyses/{uuid4()}", headers=auditor).status_code == 404
    assert AnalysisRunner(service, engine).run_one()
    assert service.get(UUID(identifier)).status == "completed", service.get(UUID(identifier)).error
    downloaded = client.get(f"/api/v1/analyses/{identifier}/download", headers=auditor)
    assert downloaded.status_code == 200
    assert downloaded.headers["x-checksum-sha256"] == hashlib.sha256(downloaded.content).hexdigest()
    assert downloaded.json()["analysis"]["id"] == identifier
    assert f"{identifier}.json" in downloaded.headers["content-disposition"]
    assert (
        client.get(
            f"/api/v1/analyses/{identifier}/download?format=csv", headers=auditor
        ).status_code
        == 200
    )
    engine.dispose()


def test_full_correlation_covers_sale_after_more_than_one_batch_of_technical_rows(analysis_case):
    from retailprintguard.common.pipeline_lock import pipeline_lock

    service, runner, factory, actor, _, _ = analysis_case
    with factory.begin() as session:
        original = session.scalar(select(Document))
        version = session.scalar(select(DocumentVersion))
        for index in range(1001):
            document_id = uuid4()
            document_values = {
                column.name: getattr(original, column.name) for column in Document.__table__.columns
            }
            document_values.update(
                id=document_id,
                source_document_key=f"technical-{index}",
                document_type="SHIFT_END_REPORT",
            )
            session.add(Document(**document_values))
            session.flush()
            version_values = {
                column.name: getattr(version, column.name)
                for column in DocumentVersion.__table__.columns
            }
            version_values.update(
                id=uuid4(),
                document_id=document_id,
                document_type="SHIFT_END_REPORT",
                normalized_text="Report di fine turno",
                chain_scope=f"document:{document_id}",
                parsed_at=version.parsed_at - timedelta(seconds=1),
            )
            session.add(DocumentVersion(**version_values))
    queued = service.create(AnalysisCreate(), actor)
    with pipeline_lock(runner.engine) as lease:
        runner._correlate(queued.id, lease, Event())
    view = service.get(queued.id)
    assert view.checkpoints["correlation_complete"]
    assert view.checkpoints["correlation_source_documents"] == 1002
    assert view.checkpoints["correlation_report"]["documents_loaded"] == 1002
    assert view.checkpoints["correlation_report"]["transactions_evaluated"] >= 1


def test_empty_history_completes_and_deleted_input_never_silently_truncates(analysis_case):
    service, runner, factory, actor, _, _ = analysis_case
    queued = service.create(AnalysisCreate(), actor)
    with factory.begin() as session:
        item = session.scalar(select(AnalysisInput))
        session.delete(item)
    assert runner.run_one()
    assert service.get(queued.id).status == "failed"
    assert "Appartenenza" in service.get(queued.id).error


def test_analysis_service_has_no_printer_or_beeper_environment():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    unit = (root / "systemd/retailprintguard-analysis.service").read_text()
    assert "IPAddressDeny=any" in unit and "IPAddressAllow=localhost" in unit
    assert "parser.env" not in unit
    assert "User=retailprintguard-worker" in unit
    assert "CapabilityBoundingSet=\n" in unit


def test_no_raw_keeps_previous_interpretation_explicit_without_claiming_reprocessing(analysis_case):
    service, runner, factory, actor, job_id, request_id = analysis_case
    # Model a historical job whose only recorded artifact is a response. Its
    # previous interpretation stays available, with an explicit provenance flag.
    with factory.begin() as session:
        session.get(RawPayload, request_id).artifact_role = "LEGACY_ARTIFACT"
    queued = service.create(AnalysisCreate(), actor)
    assert runner.run_one()
    view = service.get(queued.id)
    assert view.status == "completed", view.error
    assert view.summary["documents_without_raw_reprocessing"] == 1
    assert view.summary["jobs_without_request_raw"] == 1
    rows, count = service.results(queued.id, limit=10, offset=0)
    assert count == 1 and not rows[0].reprocessed_from_raw
    assert rows[0].reprocessing_note == "NO_REQUEST_RAW"
    assert rows[0].parser_version == "0.0.synthetic"
    assert view.checkpoints["activated_parsers"] == {}
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 1
        assert (
            session.get(AnalysisInput, (queued.id, job_id)).outcome["skipped"] == "NO_REQUEST_RAW"
        )


@pytest.mark.parametrize("changed", ["captured_at", "source_job_id", "source_path"])
def test_frozen_source_metadata_changes_are_rejected(analysis_case, changed):
    service, runner, factory, actor, job_id, request_id = analysis_case
    queued = service.create(AnalysisCreate(), actor)
    with factory.begin() as session:
        if changed == "captured_at":
            session.get(PrintJob, job_id).captured_at += timedelta(seconds=1)
        elif changed == "source_job_id":
            session.get(PrintJob, job_id).source_job_id = "different-synthetic-id"
        else:
            session.get(RawPayload, request_id).source_path = "/synthetic/changed.raw"
    assert runner.run_one()
    assert service.get(queued.id).status == "failed"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 1


def test_empty_history_completes_without_activating_any_parser():
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    actor = UserPrincipal(id=uuid4(), username="empty_auditor", roles=(RoleName.AUDITOR,))
    with factory.begin() as session:
        session.add(
            User(
                id=actor.id,
                username=actor.username,
                display_name="Synthetic",
                password_hash="not-a-login-hash",
            )
        )
    service = AnalysisService(factory)
    queued = service.create(AnalysisCreate(), actor)
    assert AnalysisRunner(service, engine).run_one()
    view = service.get(queued.id)
    assert view.status == "completed", view.error
    assert view.total_jobs == view.completed_jobs == view.total_documents == 0
    assert view.checkpoints["activated_parsers"] == {}
    assert json.loads(service.download(queued.id, format="json"))["results"] == []
    engine.dispose()


def test_shift_report_discount_lines_do_not_inflate_observed_sale_totals(analysis_case):
    service, runner, factory, actor, _, request_id = analysis_case
    with factory.begin() as session:
        raw = session.get(RawPayload, request_id)
        raw.payload = management_raw("Report di fine turno")
        raw.sha256 = hashlib.sha256(raw.payload).hexdigest()
        raw.byte_count = len(raw.payload)
    queued = service.create(AnalysisCreate(), actor)
    assert runner.run_one()
    view = service.get(queued.id)
    assert view.status == "completed", view.error
    assert view.summary["employee_total"] == "0"
    rows, count = service.results(queued.id, limit=100, offset=0)
    assert count == 1 and rows[0].document_type == "SHIFT_END_REPORT"
    assert rows[0].reasons == ["document_type_not_sale"]
    assert rows[0].observations == []
    assert service.results(queued.id, limit=100, offset=0, with_discounts=True)[1] == 0
    assert service.results(queued.id, limit=100, offset=0, kind="employee_30")[1] == 0
