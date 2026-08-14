"""Deterministic, explainable document correlation."""

from retailprintguard.correlation.engine import (
    ALGORITHM_VERSION,
    CorrelatedTransaction,
    CorrelationEngine,
    LineChange,
    LineChangeType,
    TimelineEntry,
    apply_order_change_lines,
    compare_document_lines,
)

__all__ = [
    "ALGORITHM_VERSION",
    "CorrelatedTransaction",
    "CorrelationEngine",
    "LineChange",
    "LineChangeType",
    "TimelineEntry",
    "apply_order_change_lines",
    "compare_document_lines",
]
