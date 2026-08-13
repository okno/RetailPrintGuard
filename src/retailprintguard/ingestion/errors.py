"""Errors with explicit retry/quarantine semantics."""

from __future__ import annotations


class IngestionError(RuntimeError):
    """Base class for ingestion failures."""


class SourceValidationError(IngestionError):
    """The source evidence is malformed, unsupported, or fails integrity checks."""


class SourceBusyError(IngestionError):
    """A source snapshot changed during a read and should be retried."""


class RepositoryUnavailable(IngestionError):
    """The durable repository is temporarily unavailable."""


class RepositoryContractError(IngestionError):
    """The repository violated its atomic idempotency contract."""
