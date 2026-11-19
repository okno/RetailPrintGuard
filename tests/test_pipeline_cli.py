from __future__ import annotations

import importlib
import logging
import os
from types import SimpleNamespace

import pytest
from test_pipeline_lock import FakeEngine, _database_error

from retailprintguard.parser.worker import ParserRunReport


class OneIterationStop:
    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self.stopped = True
        return True


@pytest.fixture(params=["parser", "correlation", "fraud"])
def worker_cli(request, monkeypatch):
    service = request.param
    module = importlib.import_module(f"retailprintguard.{service}.main")
    engine = FakeEngine()
    stop = OneIterationStop()
    calls = []
    runtime_stops = []
    control = {"failure": False}
    settings = SimpleNamespace(
        timezone="Europe/Rome",
        devices=(),
        spool_root="synthetic-spool",
        database_url=lambda: SimpleNamespace(get_secret_value=lambda: "synthetic-unused-db-url"),
        correlation=SimpleNamespace(minimum_score=80, time_window_seconds=300),
        ingestion=SimpleNamespace(scan_interval_seconds=1),
        fraud=SimpleNamespace(
            default_amount_drop_percent=20,
            order_without_fiscal_close_minutes=30,
            extreme_price_change_percent=50,
        ),
    )

    class Worker:
        def __init__(self, *_, **options):
            self.options = options

        def run_once(self, **options):
            calls.append(options)
            if control["failure"]:
                raise _database_error()
            return ParserRunReport(discovered=1, parsed_jobs=1, parsed_documents=1)

    monkeypatch.setattr(module, "load_settings", lambda _: settings)
    monkeypatch.setattr(module, "create_db_engine", lambda _: engine)
    monkeypatch.setattr(module, "session_factory", lambda _: object())
    monkeypatch.setattr(module, "Event", lambda: stop)
    monkeypatch.setattr(module.signal, "signal", lambda *_: None)
    monkeypatch.setattr(module, f"{service.title()}Worker", Worker)
    monkeypatch.setattr(
        module,
        "configure_structured_logging",
        lambda _: SimpleNamespace(stop=lambda: runtime_stops.append(True)),
    )
    if service == "parser":
        monkeypatch.setattr(
            module.PosBeeperConfiguration,
            "from_environment",
            lambda: SimpleNamespace(enabled=False),
        )
    original_umask = os.umask(0o027)
    os.umask(original_umask)
    yield SimpleNamespace(
        service=service,
        module=module,
        engine=engine,
        stop=stop,
        calls=calls,
        control=control,
        runtime_stops=runtime_stops,
    )
    os.umask(original_umask)


def _run(case, *arguments):
    function = case.module.cli if case.service == "parser" else case.module.run_cli
    return function(["--config", "synthetic-config.yaml", *arguments])


def test_once_executes_under_lease_and_always_disposes_engine(worker_cli, capsys):
    case = worker_cli
    assert _run(case, "--once", "--json") == 0
    assert len(case.calls) == 1 and case.engine.disposed
    assert case.engine.server.owners == {}
    queries = [query for query, _ in case.engine.connections[0].commands]
    assert sum("IS_USED_LOCK" in query for query in queries) == 2
    assert '"parsed_jobs": 1' in capsys.readouterr().out


def test_once_busy_is_failure_without_running_or_printing_success(worker_cli, capsys):
    case = worker_cli
    case.engine.acquire_result = 0
    assert _run(case, "--once", "--json") == 2
    captured = capsys.readouterr()
    assert case.calls == [] and captured.out == ""
    assert "in corso" in captured.err and case.engine.disposed


@pytest.mark.parametrize("json_logs", [False, True])
def test_loop_busy_waits_without_running_or_success_report(worker_cli, json_logs, capsys):
    case = worker_cli
    case.engine.acquire_result = 0
    assert _run(case, *(["--json-logs"] if json_logs else [])) == 0
    assert case.calls == [] and len(case.stop.waits) == 1 and case.engine.disposed
    assert capsys.readouterr().out == ""
    assert case.runtime_stops == ([True] if json_logs else [])


@pytest.mark.parametrize("once", [False, True])
@pytest.mark.parametrize("json_logs", [False, True])
def test_worker_database_failure_never_logs_parameters_or_secrets(
    worker_cli,
    once,
    json_logs,
    capsys,
    caplog,
):
    case = worker_cli
    case.control["failure"] = True
    caplog.set_level(logging.ERROR)
    args = (["--once"] if once else []) + (["--json-logs"] if json_logs else [])
    assert _run(case, *args) == (2 if once else 0)
    captured = capsys.readouterr()
    diagnostic = captured.out + captured.err + caplog.text
    diagnostic += " ".join(str(getattr(record, "error", "")) for record in caplog.records)
    assert "synthetic-sensitive" not in diagnostic
    assert "synthetic-secret" not in diagnostic
    assert "synthetic_private_payload" not in diagnostic
    assert case.engine.disposed and case.engine.server.owners == {}
    assert len(case.calls) == 1
    assert case.runtime_stops == ([True] if json_logs else [])


def test_connection_failure_is_safe_before_worker_runs(worker_cli, monkeypatch, capsys):
    case = worker_cli

    def fail_connect():
        raise _database_error()

    monkeypatch.setattr(case.engine, "connect", fail_connect)
    assert _run(case, "--once") == 2
    diagnostic = capsys.readouterr()
    assert "synthetic-secret" not in diagnostic.err and diagnostic.out == ""
    assert case.calls == [] and case.engine.disposed


def test_reparse_never_starts_configured_beeper(worker_cli, monkeypatch):
    case = worker_cli
    if case.service != "parser":
        return
    monkeypatch.setattr(
        case.module.PosBeeperConfiguration,
        "from_environment",
        lambda: SimpleNamespace(enabled=True),
    )

    def forbidden(*_, **__):
        pytest.fail("Historical replay started a printer notification")

    monkeypatch.setattr(case.module, "PosBeeperDispatcher", forbidden)
    monkeypatch.setattr(case.module, "PosSpoolBeeperWatcher", forbidden)
    assert _run(case, "--once", "--reparse-all") == 0
    assert case.calls == [{"limit": 100, "reparse": True}]


def test_parser_reparse_can_target_one_configured_device(worker_cli, capsys):
    case = worker_cli
    if case.service != "parser":
        return
    settings = case.module.load_settings("synthetic-config.yaml")
    settings.devices = (SimpleNamespace(id="epson_synthetic"),)
    assert _run(case, "--once", "--reparse-all", "--device-id", "epson_synthetic") == 0
    assert case.calls == [{"limit": 100, "reparse": True, "device_id": "epson_synthetic"}]
    for arguments in (
        ("--once", "--device-id", "epson_synthetic"),
        ("--reparse-all", "--device-id", "epson_synthetic"),
        ("--once", "--reparse-all", "--device-id", "unknown_device"),
        ("--once", "--reparse-all", "--device-id", "../unsafe"),
    ):
        assert _run(case, *arguments) == 2
    assert len(case.calls) == 1
    assert capsys.readouterr().err.count("ERROR: ValueError") == 4


def test_parser_activation_obeys_same_busy_lock(worker_cli, monkeypatch):
    case = worker_cli
    if case.service != "correlation":
        return
    case.engine.acquire_result = 0
    activations = []
    monkeypatch.setattr(
        case.module, "activate_parser_version", lambda *a, **k: activations.append(k)
    )
    assert (
        _run(
            case,
            "--once",
            "--activate-parser",
            "synthetic",
            "--parser-version",
            "1.0",
            "--build-sha256",
            "0" * 64,
            "--activation-reason",
            "synthetic",
        )
        == 2
    )
    assert activations == [] and case.calls == [] and case.engine.disposed
