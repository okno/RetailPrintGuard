"""Deterministic, explainable document correlation."""

from retailprintguard.correlation.engine import (
    ALGORITHM_VERSION,
    CorrelatedTransaction,
    CorrelationEngine,
    LineChange,
    LineChangeType,
    TimelineEntry,
    compare_document_lines,
)

__all__ = [
    "ALGORITHM_VERSION",
    "CorrelatedTransaction",
    "CorrelationEngine",
    "LineChange",
    "LineChangeType",
    "TimelineEntry",
    "compare_document_lines",
]
