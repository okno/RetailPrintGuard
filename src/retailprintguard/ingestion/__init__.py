"""Read-only evidence ingestion for supported proxy spool formats."""

from retailprintguard.ingestion.dto import NormalizedEnvelope, SourceKind
from retailprintguard.ingestion.repository import ImportDisposition, IngestionRepository

__all__ = [
    "ImportDisposition",
    "IngestionRepository",
    "NormalizedEnvelope",
    "SourceKind",
]
