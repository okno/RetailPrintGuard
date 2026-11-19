from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

from retailprintguard.common.pipeline_lock import (
    PipelineBusy,
    PipelineLockError,
    pipeline_lock,
    run_pipeline_once,
    safe_worker_error,
)


def _database_error():
    return OperationalError(
        "SELECT synthetic_private_payload",
        {"secret": "synthetic-sensitive-value"},
        RuntimeError("synthetic-secret-connection-details"),
    )


class FakeServer:
    def __init__(self):
        self.owners = {}
        self.next_id = 20


class FakeConnection:
    def __init__(self, engine):
        self.engine = engine
        self.server = engine.server
        self.identifier = self.server.next_id
        self.server.next_id += 1
        self.closed = False
        self.invalidated = False
        self.commands = []

    def execution_options(self, **options):
        assert options == {"isolation_level": "AUTOCOMMIT"}
        if self.engine.options_error:
            raise _database_error()
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def invalidate(self):
        self.invalidated = True
        self.server.owners = {
            name: owner for name, owner in self.server.owners.items() if owner != self.identifier
        }

    def scalar(self, statement, parameters=None):
        command = str(statement)
        parameters = parameters or {}
        self.commands.append((command, parameters))
        if command == "SELECT DATABASE()":
            return self.engine.database
        if command.startswith("SELECT GET_LOCK"):
            name = parameters["name"]
            if self.engine.acquire_error:
                self.server.owners[name] = self.identifier
                raise _database_error()  # Reply lost after acquisition reached the server.
            if self.engine.acquire_result != 1:
                return self.engine.acquire_result
            if name in self.server.owners:
                return 0
            self.server.owners[name] = self.identifier
            return 1
        if command == "SELECT CONNECTION_ID()":
            return (
                self.engine.connection_id
                if self.engine.connection_id is not None
                else self.identifier
            )
        if command.startswith("SELECT IS_USED_LOCK"):
            if self.engine.check_error:
                raise _database_error()
            return self.server.owners.get(parameters["name"])
        if command.startswith("SELECT RELEASE_LOCK"):
            if self.engine.release_error:
                raise _database_error()
            if self.engine.release_result != 1:
                return self.engine.release_result
            owner = self.server.owners.get(parameters["name"])
            if owner == self.identifier:
                del self.server.owners[parameters["name"]]
                return 1
            return 0
        raise AssertionError("Unexpected lock query")


class FakeEngine:
    def __init__(self, database="synthetic_a", *, server=None, dialect="mariadb"):
        self.database = database
        self.server = server or FakeServer()
        self.dialect = SimpleNamespace(name=dialect)
        self.connections = []
        self.acquire_result = self.release_result = 1
        self.acquire_error = self.check_error = self.release_error = False
        self.options_error = False
        self.connection_id = None
        self.disposed = False

    def connect(self):
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    def dispose(self):
        self.disposed = True


def test_same_database_contends_across_engines_but_other_database_is_independent():
    server = FakeServer()
    first = FakeEngine(server=server)
    same = FakeEngine(server=server)
    other = FakeEngine("synthetic_b", server=server)
    with pipeline_lock(first) as owner:
        assert owner is not None and len(owner.name) <= 64
        assert "synthetic_a" not in owner.name
        with pipeline_lock(same) as busy:
            assert busy is None
        with pipeline_lock(other) as independent:
            assert independent is not None and independent.name != owner.name
            independent.check()
        owner.check()
    assert server.owners == {}
    assert all(
        connection.closed for engine in (first, same, other) for connection in engine.connections
    )


@pytest.mark.parametrize("required", [False, True])
def test_busy_never_invokes_operation(required):
    engine = FakeEngine()
    engine.acquire_result = 0
    calls = []
    if required:
        with pytest.raises(PipelineBusy):
            run_pipeline_once(engine, lambda: calls.append(True), required=True)
    else:
        assert run_pipeline_once(engine, lambda: calls.append(True)) is None
    assert calls == [] and engine.connections[0].closed
    assert not any("RELEASE_LOCK" in query for query, _ in engine.connections[0].commands)


def test_operation_exception_still_releases_owner_and_connection():
    engine = FakeEngine()

    def fail():
        raise ValueError("synthetic operation failure")

    with pytest.raises(ValueError, match="synthetic operation failure"):
        run_pipeline_once(engine, fail)
    assert engine.server.owners == {} and engine.connections[0].closed
    assert run_pipeline_once(engine, lambda: 42) == 42


def test_lease_loss_during_operation_cannot_return_success():
    engine = FakeEngine()

    def lose_connection():
        engine.connections[-1].invalidate()
        return "must not be reported as success"

    with pytest.raises(PipelineLockError):
        run_pipeline_once(engine, lose_connection)
    assert engine.server.owners == {} and engine.connections[0].invalidated


@pytest.mark.parametrize("failure", ["acquire_error", "check_error", "release_error"])
def test_database_failure_is_sanitized_and_uncertain_owner_is_not_pooled(failure):
    engine = FakeEngine()
    setattr(engine, failure, True)
    with pytest.raises(PipelineLockError) as error:
        run_pipeline_once(engine, lambda: 42)
    assert "synthetic-sensitive" not in str(error.value)
    assert "synthetic-secret" not in str(error.value)
    assert "SELECT" not in str(error.value)
    assert engine.server.owners == {} and engine.connections[0].closed
    if failure in {"acquire_error", "release_error"}:
        assert engine.connections[0].invalidated


@pytest.mark.parametrize("result", [0, None])
def test_unconfirmed_release_invalidates_connection_and_reports_failure(result):
    engine = FakeEngine()
    engine.release_result = result
    with pytest.raises(PipelineLockError, match="release"):
        run_pipeline_once(engine, lambda: 42)
    assert engine.connections[0].invalidated and engine.server.owners == {}


def test_failed_connection_identity_releases_the_acquired_lock():
    engine = FakeEngine()
    engine.connection_id = "not-an-integer"
    with pytest.raises(PipelineLockError, match="identify"):
        run_pipeline_once(engine, lambda: 42)
    assert engine.server.owners == {}


def test_connection_is_closed_if_autocommit_configuration_fails():
    engine = FakeEngine()
    engine.options_error = True
    with pytest.raises(PipelineLockError):
        run_pipeline_once(engine, lambda: pytest.fail("operation must not execute"))
    assert engine.connections[0].closed and engine.connections[0].commands == []


@pytest.mark.parametrize("wait", [-1, 61])
def test_invalid_timeout_is_rejected_before_connecting(wait):
    engine = FakeEngine()
    with pytest.raises(ValueError), pipeline_lock(engine, wait_seconds=wait):
        pytest.fail("invalid timeout entered operation")
    assert engine.connections == []


@pytest.mark.parametrize("database", [None, ""])
def test_missing_database_cannot_acquire_a_server_global_pipeline(database):
    engine = FakeEngine(database)
    with pytest.raises(PipelineLockError, match="selected"):
        run_pipeline_once(engine, lambda: 42)
    assert not any("GET_LOCK" in query for query, _ in engine.connections[0].commands)


def test_sqlite_smoke_runs_operation_without_mysql_queries_and_sanitizes_failure():
    engine = create_engine("sqlite:///:memory:")
    statements = []
    event.listen(engine, "before_cursor_execute", lambda _c, _u, sql, *_: statements.append(sql))
    try:

        def operation():
            with engine.begin() as connection:
                return connection.scalar(text("SELECT 42"))

        assert run_pipeline_once(engine, operation, required=True) == 42
        assert statements == ["SELECT 42"]
        with pytest.raises(PipelineLockError) as error:
            run_pipeline_once(engine, lambda: (_ for _ in ()).throw(_database_error()))
        assert "synthetic-sensitive" not in str(error.value)
    finally:
        engine.dispose()


def test_unknown_database_protocol_fails_before_operation_and_error_formatter_redacts():
    with pytest.raises(ValueError, match="Unsupported"):
        run_pipeline_once(FakeEngine(dialect="unknown"), lambda: pytest.fail("executed"))
    assert safe_worker_error(_database_error()) == "OperationalError"
    assert safe_worker_error(PipelineBusy("already running")) == "already running"
