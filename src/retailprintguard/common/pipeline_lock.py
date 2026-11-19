"""Coordinate derived-data workers without involving the proxy data plane."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

T = TypeVar("T")


class PipelineLockError(RuntimeError):
    """Safe diagnostic without SQL statements, connection details or parameters."""


class PipelineBusy(PipelineLockError):
    """Another derived-data operation currently owns the pipeline."""


@dataclass(frozen=True)
class PipelineLease:
    connection: Connection | None
    name: str
    connection_id: int | None

    def check(self) -> None:
        """Fail before another phase if the dedicated lock connection was lost."""
        if self.connection is None:
            return
        try:
            owner = self.connection.scalar(text("SELECT IS_USED_LOCK(:name)"), {"name": self.name})
        except SQLAlchemyError:
            raise PipelineLockError("Cannot verify derived-data pipeline lock") from None
        if owner != self.connection_id:
            raise PipelineLockError("Derived-data pipeline lock was lost")


def safe_worker_error(error: BaseException) -> str:
    """Service diagnostics must never serialize raw database errors or payloads."""

    return str(error) if isinstance(error, PipelineLockError) else type(error).__name__


@contextmanager
def pipeline_lock(engine: Engine, *, wait_seconds: int = 0) -> Iterator[PipelineLease | None]:
    """Hold one MariaDB advisory lock on a dedicated connection.

    A missing lease means busy, never successful execution. SQLite is used only
    by local tests and does not implement the production advisory-lock protocol.
    Callers running several phases must check the lease between bounded batches.
    """
    if not 0 <= wait_seconds <= 60:
        raise ValueError("Pipeline lock timeout must be between 0 and 60 seconds")
    if engine.dialect.name == "sqlite":
        yield PipelineLease(None, "sqlite-test", None)
        return
    if engine.dialect.name not in {"mysql", "mariadb"}:
        raise ValueError("Unsupported pipeline lock database")
    try:
        with engine.connect() as connection:
            connection = connection.execution_options(isolation_level="AUTOCOMMIT")
            database = connection.scalar(text("SELECT DATABASE()"))
            if not isinstance(database, str) or not database:
                raise PipelineLockError("Pipeline database must be selected")
            name = "rpg.pipeline.v1." + hashlib.sha256(database.encode()).hexdigest()[:40]
            try:
                acquired = connection.scalar(
                    text("SELECT GET_LOCK(:name, :timeout)"),
                    {"name": name, "timeout": wait_seconds},
                )
            except SQLAlchemyError:
                # Acquisition may have reached the server even if its reply
                # did not. Never return an uncertain owner to the pool.
                connection.invalidate()
                raise
            if acquired == 0:
                yield None
                return
            if acquired != 1:
                raise PipelineLockError("Cannot acquire derived-data pipeline lock")
            try:
                connection_id = connection.scalar(text("SELECT CONNECTION_ID()"))
                if not isinstance(connection_id, int) or connection_id <= 0:
                    raise PipelineLockError("Cannot identify derived-data pipeline lock owner")
                yield PipelineLease(connection, name, connection_id)
            finally:
                # Pooled physical connections must not retain an advisory lock
                # after uncertain release, including when the operation failed.
                try:
                    released = connection.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": name})
                    if released != 1:
                        raise PipelineLockError("Cannot confirm derived-data pipeline lock release")
                except Exception:
                    connection.invalidate()
                    raise
    except SQLAlchemyError:
        raise PipelineLockError("Derived-data pipeline database operation failed") from None


def run_pipeline_once(
    engine: Engine, operation: Callable[[], T], *, required: bool = False
) -> T | None:
    try:
        with pipeline_lock(engine) as lease:
            if lease is None:
                if required:
                    raise PipelineBusy("Una rianalisi o un altro ciclo di analisi è in corso")
                return None
            lease.check()
            result = operation()
            lease.check()
            return result
    except SQLAlchemyError:
        raise PipelineLockError("Derived-data worker database operation failed") from None
