"""Portable SQLAlchemy types used by the evidence database."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BINARY, DateTime
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UUIDBinary(TypeDecorator[UUID]):
    """Store UUID values as compact ``BINARY(16)`` values.

    The Python-facing value is always :class:`uuid.UUID`.  This keeps indexes
    compact on MariaDB while retaining a useful representation in SQLite tests.
    """

    impl = BINARY(16)
    cache_ok = True

    def process_bind_param(
        self, value: UUID | str | bytes | None, dialect: Dialect
    ) -> bytes | None:
        del dialect
        if value is None:
            return None
        if isinstance(value, bytes):
            if len(value) != 16:
                raise ValueError("binary UUID values must contain exactly 16 bytes")
            return value
        return (value if isinstance(value, UUID) else UUID(str(value))).bytes

    def process_result_value(self, value: Any, dialect: Dialect) -> UUID | None:
        del dialect
        if value is None:
            return None
        if isinstance(value, UUID):
            return value
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, bytes):
            return UUID(bytes=value)
        return UUID(str(value))


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC as timezone-free ``DATETIME(6)`` and return aware values.

    MariaDB ``DATETIME`` does not retain an offset.  Rejecting naive input and
    normalising to UTC at the boundary makes that limitation explicit.
    """

    impl = DateTime(timezone=False)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):  # type: ignore[no-untyped-def]
        if dialect.name in {"mysql", "mariadb"}:
            return dialect.type_descriptor(MYSQL_DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=False))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("database timestamps must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
