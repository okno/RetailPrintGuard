"""Database engine and transaction helpers.

Only control-plane services should receive the database URL.  Proxy processes
must not import or depend on this module.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker


def create_db_engine(
    database_url: str | URL,
    *,
    echo: bool = False,
    pool_size: int = 10,
    max_overflow: int = 20,
) -> Engine:
    """Create a production MariaDB engine or a SQLite-compatible test engine."""

    url = make_url(database_url)
    backend = url.get_backend_name()
    common: dict[str, Any] = {"echo": echo, "future": True}
    if backend == "sqlite":
        common["connect_args"] = {"check_same_thread": False}
        engine = create_engine(url, **common)

        @event.listens_for(engine, "connect")
        def _sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        return engine

    if backend not in {"mysql", "mariadb"}:
        raise ValueError("RetailPrintGuard supports MariaDB/MySQL and SQLite test databases")

    common.update(
        {
            "pool_pre_ping": True,
            "pool_recycle": 1800,
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "isolation_level": "READ COMMITTED",
        }
    )
    engine = create_engine(url, **common)

    @event.listens_for(engine, "connect")
    def _mariadb_utc(dbapi_connection: Any, connection_record: Any) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET time_zone = '+00:00'")
        finally:
            cursor.close()

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, autoflush=False, expire_on_commit=False)


@contextmanager
def transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit exactly once or roll back the complete unit of work."""

    session = factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
