"""Pure, deterministic and re-runnable POS/RCH correlation engine."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from retailprintguard.common.domain import (
    CorrelationResult,
    DocumentLine,
    DocumentType,
    NormalizedDocument,
    PaymentRecord,
)

ALGORITHM_VERSION = "rpg-correlation-1.1.0"
ZERO = Decimal("0.0000")
HUNDRED = Decimal("100")

_SOURCE_TYPES = {
    DocumentType.ORDER,
    DocumentType.ORDER_CHANGE,
    DocumentType.KITCHEN_ORDER,
    DocumentType.PRE_BILL,
    DocumentType.MANAGEMENT_DOCUMENT,
}
_FISCAL_TYPES = {
    DocumentType.COMMERCIAL_DOCUMENT,
    DocumentType.REFUND,
}
_ECONOMIC_CLOSE_TYPES = {
    *_FISCAL_TYPES,
    DocumentType.MANAGEMENT_DOCUMENT,
}
_FOLLOWUP_TYPES = {
    *_FISCAL_TYPES,
    DocumentType.CONFORMING_COPY,
    DocumentType.PAYMENT,
    DocumentType.REPRINT,
    DocumentType.CANCELLATION,
    DocumentType.DEVICE_RESPONSE,
}
_FISCAL_SIBLING_TYPES = {
    DocumentType.COMMERCIAL_DOCUMENT,
    DocumentType.REFUND,
    DocumentType.PAYMENT,
}


class LineChangeType(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    QUANTITY_CHANGED = "QUANTITY_CHANGED"
    PRICE_CHANGED = "PRICE_CHANGED"
    DISCOUNT_CHANGED = "DISCOUNT_CHANGED"
    UNCHANGED = "UNCHANGED"


class LineChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_key: str
    description: str | None
    change_type: LineChangeType
    before_quantity: Decimal | None = None
    after_quantity: Decimal | None = None
    before_unit_price: Decimal | None = None
    after_unit_price: Decimal | None = None
    before_discount: Decimal | None = None
    after_discount: Decimal | None = None
    before_total: Decimal | None = None
    after_total: Decimal | None = None
    before_source: dict[str, Any] | None = None
    after_source: dict[str, Any] | None = None


class TimelineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    document_type: DocumentType
    occurred_at: datetime
    gross_total: Decimal | None
    source_device_id: str
    source_job_id: str


class CorrelatedTransaction(BaseModel):
    """A re-creatable transaction view, not a destructive document merge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_id: UUID
    correlation: CorrelationResult | None
    documents: tuple[NormalizedDocument, ...]
    prebill_total: Decimal | None
    fiscal_total: Decimal
    observed_final_total: Decimal
    payment_total: Decimal
    difference_amount: Decimal | None
    difference_percent: Decimal | None
    split_payment: bool
    line_changes: tuple[LineChange, ...]
    timeline: tuple[TimelineEntry, ...]

    @model_validator(mode="after")
    def has_documents(self) -> CorrelatedTransaction:
        if not self.documents:
            raise ValueError("a transaction must contain at least one document")
        return self


class _Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    weight: int
    matched: bool
    detail: str


class _PairScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    left_id: UUID
    right_id: UUID
    score: int
    criteria: tuple[_Criterion, ...]


def _normalise(value: str | None) -> str | None:
    if value is None:
        return None
    folded = unicodedata.normalize("NFKD", value)
    result = " ".join(
        "".join(char for char in folded if not unicodedata.combining(char)).upper().split()
    )
    return result or None


def _document_time(document: NormalizedDocument) -> datetime:
    return document.document_timestamp or document.captured_at


def _total(document: NormalizedDocument) -> Decimal | None:
    return document.gross_total if document.gross_total is not None else document.net_total


def _line_key(line: DocumentLine, ordinal: int) -> str:
    code = _normalise(line.item_code)
    if code:
        return f"CODE:{code}"
    description = _normalise(line.description)
    if description:
        return f"DESC:{description}"
    return f"POSITION:{ordinal}"


def _source_dict(line: DocumentLine) -> dict[str, Any] | None:
    if line.source is None:
        return None
    return line.source.model_dump(mode="json")


def _effective_unit_price(line: DocumentLine) -> Decimal | None:
    if line.modified_unit_price is not None:
        return line.modified_unit_price
    return line.unit_price


def _aggregate_lines(lines: Iterable[DocumentLine]) -> dict[str, dict[str, Any]]:
    aggregated: dict[str, dict[str, Any]] = {}
    for ordinal, line in enumerate(lines):
        key = _line_key(line, ordinal)
        quantity = line.quantity if line.quantity is not None else Decimal("1")
        existing = aggregated.get(key)
        if existing is None:
            aggregated[key] = {
                "description": line.description,
                "quantity": quantity,
                "unit_price": _effective_unit_price(line),
                "discount": line.discount,
                "total": line.line_total,
                "source": _source_dict(line),
            }
            continue
        existing["quantity"] += quantity
        if existing["total"] is not None and line.line_total is not None:
            existing["total"] += line.line_total
        elif line.line_total is not None:
            existing["total"] = line.line_total
        candidate_price = _effective_unit_price(line)
        if existing["unit_price"] != candidate_price:
            existing["unit_price"] = candidate_price
    return aggregated


def compare_document_lines(
    before: Iterable[DocumentLine], after: Iterable[DocumentLine]
) -> tuple[LineChange, ...]:
    """Return a stable semantic diff between two line collections."""

    left = _aggregate_lines(before)
    right = _aggregate_lines(after)
    result: list[LineChange] = []
    for key in sorted(set(left) | set(right)):
        old = left.get(key)
        new = right.get(key)
        if old is None:
            change = LineChangeType.ADDED
        elif new is None:
            change = LineChangeType.REMOVED
        elif old["quantity"] != new["quantity"]:
            change = LineChangeType.QUANTITY_CHANGED
        elif old["unit_price"] != new["unit_price"]:
            change = LineChangeType.PRICE_CHANGED
        elif old["discount"] != new["discount"]:
            change = LineChangeType.DISCOUNT_CHANGED
        else:
            change = LineChangeType.UNCHANGED
        result.append(
            LineChange(
                item_key=key,
                description=(new or old)["description"],
                change_type=change,
                before_quantity=None if old is None else old["quantity"],
                after_quantity=None if new is None else new["quantity"],
                before_unit_price=None if old is None else old["unit_price"],
                after_unit_price=None if new is None else new["unit_price"],
                before_discount=None if old is None else old["discount"],
                after_discount=None if new is None else new["discount"],
                before_total=None if old is None else old["total"],
                after_total=None if new is None else new["total"],
                before_source=None if old is None else old["source"],
                after_source=None if new is None else new["source"],
            )
        )
    return tuple(result)


def _line_similarity(left: NormalizedDocument, right: NormalizedDocument) -> Decimal | None:
    left_keys = set(_aggregate_lines(left.lines))
    right_keys = set(_aggregate_lines(right.lines))
    if not left_keys or not right_keys:
        return None
    return Decimal(len(left_keys & right_keys)) / Decimal(len(left_keys | right_keys))


def _metadata_references(document: NormalizedDocument) -> set[str]:
    keys = {
        "reference",
        "order_reference",
        "document_reference",
        "transaction_reference",
        "work_session",
    }
    references: set[str] = set()
    for value in (document.order_code, document.external_document_code):
        if value is not None and (normalised := _normalise(value)) is not None:
            references.add(normalised)
    for key in keys:
        value = document.raw_metadata.get(key)
        if value is not None and (normalised := _normalise(str(value))) is not None:
            references.add(normalised)
    return references


def _authoritative_payments(
    documents: Iterable[NormalizedDocument],
    fiscal_documents: Iterable[NormalizedDocument],
) -> tuple[PaymentRecord, ...]:
    """Avoid counting the same payment echoed by prebill and fiscal parsers.

    Fiscal documents are authoritative. Explicit PAYMENT documents are used
    only when no fiscal parser exposed payment records; the remaining source
    documents are a final compatibility fallback.
    """

    documents = tuple(documents)
    fiscal_payments = tuple(
        payment for document in fiscal_documents for payment in document.payments
    )
    if fiscal_payments:
        return fiscal_payments
    explicit_payments = tuple(
        payment
        for document in documents
        if document.type is DocumentType.PAYMENT
        for payment in document.payments
    )
    if explicit_payments:
        return explicit_payments
    return tuple(payment for document in documents for payment in document.payments)


def _compatible(left: NormalizedDocument, right: NormalizedDocument) -> bool:
    types = {left.type, right.type}
    if types <= _FISCAL_TYPES:
        return False
    if left.type in _SOURCE_TYPES and right.type in _SOURCE_TYPES:
        return True
    if (left.type in _SOURCE_TYPES and right.type in _FOLLOWUP_TYPES) or (
        right.type in _SOURCE_TYPES and left.type in _FOLLOWUP_TYPES
    ):
        return True
    return DocumentType.CONFORMING_COPY in types and bool(types & _FISCAL_TYPES)


class CorrelationEngine:
    def __init__(self, *, minimum_score: int = 60, time_window_seconds: int = 7200) -> None:
        if not 0 <= minimum_score <= 100:
            raise ValueError("minimum_score must be between 0 and 100")
        if time_window_seconds <= 0:
            raise ValueError("time_window_seconds must be positive")
        self.minimum_score = minimum_score
        self.time_window_seconds = time_window_seconds

    def score_pair(self, left: NormalizedDocument, right: NormalizedDocument) -> _PairScore:
        criteria: list[_Criterion] = []

        def exact(name: str, weight: int, first: str | None, second: str | None) -> None:
            a, b = _normalise(first), _normalise(second)
            matched = a is not None and a == b
            detail = "valore esatto" if matched else "assente o differente"
            criteria.append(_Criterion(name=name, weight=weight, matched=matched, detail=detail))

        exact("order_code", 35, left.order_code, right.order_code)
        exact(
            "external_document_code",
            15,
            left.external_document_code,
            right.external_document_code,
        )
        exact("table_code", 12, left.table_code, right.table_code)
        exact("operator_code", 5, left.operator_code, right.operator_code)
        exact("terminal_code", 4, left.terminal_code, right.terminal_code)
        exact("source_session", 5, left.source_session_id, right.source_session_id)

        references = _metadata_references(left) & _metadata_references(right)
        criteria.append(
            _Criterion(
                name="embedded_reference",
                weight=20,
                matched=bool(references),
                detail="riferimento comune" if references else "nessun riferimento comune",
            )
        )

        delta_seconds = abs((_document_time(right) - _document_time(left)).total_seconds())
        within_window = delta_seconds <= self.time_window_seconds
        if delta_seconds <= 120:
            time_weight = 12
        elif delta_seconds <= 900:
            time_weight = 8
        elif within_window:
            time_weight = 4
        else:
            time_weight = 0
        criteria.append(
            _Criterion(
                name="time_proximity",
                weight=time_weight,
                matched=within_window,
                detail=f"distanza {int(delta_seconds)} secondi",
            )
        )
        same_date = _document_time(left).date() == _document_time(right).date()
        criteria.append(
            _Criterion(
                name="business_date",
                weight=3,
                matched=same_date,
                detail="stessa data" if same_date else "data differente",
            )
        )

        left_total, right_total = _total(left), _total(right)
        totals_equal = left_total is not None and left_total == right_total
        plausible_partial = (
            left_total is not None
            and right_total is not None
            and min(left_total, right_total) >= ZERO
            and max(left_total, right_total) > ZERO
            and min(left_total, right_total) < max(left_total, right_total)
        )
        criteria.append(
            _Criterion(
                name="amount_relationship",
                weight=8 if totals_equal else (3 if plausible_partial else 0),
                matched=totals_equal or plausible_partial,
                detail=(
                    "totali uguali"
                    if totals_equal
                    else "importo parziale plausibile"
                    if plausible_partial
                    else "totali non confrontabili"
                ),
            )
        )

        similarity = _line_similarity(left, right)
        line_weight = (
            0 if similarity is None else int((similarity * Decimal(12)).to_integral_value())
        )
        criteria.append(
            _Criterion(
                name="line_similarity",
                weight=line_weight,
                matched=similarity is not None and similarity > ZERO,
                detail=(
                    "righe non disponibili"
                    if similarity is None
                    else f"similarità {similarity.quantize(Decimal('0.01'))}"
                ),
            )
        )
        compatible = _compatible(left, right)
        criteria.append(
            _Criterion(
                name="document_sequence",
                weight=8,
                matched=compatible,
                detail="tipi compatibili" if compatible else "tipi non collegabili",
            )
        )
        same_device = left.source_device_id == right.source_device_id
        criteria.append(
            _Criterion(
                name="device_context",
                weight=2,
                matched=same_device,
                detail="stesso dispositivo" if same_device else "dispositivi differenti",
            )
        )
        score = min(100, sum(item.weight for item in criteria if item.matched))
        if not compatible or not within_window:
            score = 0
        return _PairScore(
            left_id=left.id,
            right_id=right.id,
            score=score,
            criteria=tuple(criteria),
        )

    def _fiscal_siblings(
        self, left: NormalizedDocument, right: NormalizedDocument
    ) -> _PairScore | None:
        if left.type not in _FISCAL_SIBLING_TYPES or right.type not in _FISCAL_SIBLING_TYPES:
            return None
        delta_seconds = abs((_document_time(right) - _document_time(left)).total_seconds())
        if delta_seconds > self.time_window_seconds:
            return None
        references = _metadata_references(left) & _metadata_references(right)
        order_match = _normalise(left.order_code) is not None and _normalise(
            left.order_code
        ) == _normalise(right.order_code)
        table_match = _normalise(left.table_code) is not None and _normalise(
            left.table_code
        ) == _normalise(right.table_code)
        session_match = _normalise(left.source_session_id) is not None and _normalise(
            left.source_session_id
        ) == _normalise(right.source_session_id)
        if not (references or order_match or (table_match and session_match)):
            return None
        criteria = (
            _Criterion(
                name="split_fiscal_reference",
                weight=70,
                matched=True,
                detail="documenti fiscali parziali con riferimento ordine comune",
            ),
            _Criterion(
                name="time_proximity",
                weight=10,
                matched=True,
                detail=f"distanza {int(delta_seconds)} secondi",
            ),
        )
        return _PairScore(
            left_id=left.id,
            right_id=right.id,
            score=80,
            criteria=criteria,
        )

    def _device_response_pair(
        self, left: NormalizedDocument, right: NormalizedDocument
    ) -> _PairScore | None:
        """Link an RCH response only to its observed request/session context."""

        if DocumentType.DEVICE_RESPONSE not in {left.type, right.type}:
            return None
        delta_seconds = abs((_document_time(right) - _document_time(left)).total_seconds())
        if delta_seconds > self.time_window_seconds:
            return None
        same_job = bool(left.source_job_id) and left.source_job_id == right.source_job_id
        same_session = (
            bool(left.source_session_id)
            and left.source_session_id == right.source_session_id
            and left.source_device_id == right.source_device_id
        )
        if not (same_job or same_session):
            return None
        criteria = (
            _Criterion(
                name="response_job_context",
                weight=80,
                matched=same_job,
                detail="stesso job duplex" if same_job else "job differente",
            ),
            _Criterion(
                name="response_session_context",
                weight=65,
                matched=same_session,
                detail=(
                    "stessa sessione e dispositivo"
                    if same_session
                    else "sessione o dispositivo differente"
                ),
            ),
            _Criterion(
                name="time_proximity",
                weight=10,
                matched=True,
                detail=f"distanza {int(delta_seconds)} secondi",
            ),
        )
        return _PairScore(
            left_id=left.id,
            right_id=right.id,
            score=min(100, sum(item.weight for item in criteria if item.matched)),
            criteria=criteria,
        )

    def score_candidate_pair(
        self, left: NormalizedDocument, right: NormalizedDocument
    ) -> _PairScore:
        """Score one already-blocked pair, including protocol-specific links."""

        return (
            self._device_response_pair(left, right)
            or self._fiscal_siblings(left, right)
            or self.score_pair(left, right)
        )

    def correlate(
        self, documents: Iterable[NormalizedDocument]
    ) -> tuple[CorrelatedTransaction, ...]:
        ordered = sorted(
            documents,
            key=lambda item: (_document_time(item), str(item.id)),
        )
        pairs = (
            (left.id, right.id)
            for index, left in enumerate(ordered)
            for right in ordered[index + 1 :]
        )
        return self._correlate_pairs(ordered, pairs)

    def correlate_candidates(
        self,
        documents: Iterable[NormalizedDocument],
        candidate_pairs: Iterable[tuple[UUID, UUID]],
    ) -> tuple[CorrelatedTransaction, ...]:
        """Correlate only pre-blocked candidate pairs.

        The database worker uses indexed business keys and a bounded time
        window to produce the pair set.  This preserves the exact scoring
        semantics without the quadratic all-pairs scan used for small in-memory
        batches and tests.
        """

        ordered = sorted(
            documents,
            key=lambda item: (_document_time(item), str(item.id)),
        )
        return self._correlate_pairs(ordered, candidate_pairs)

    def _correlate_pairs(
        self,
        ordered: list[NormalizedDocument],
        candidate_pairs: Iterable[tuple[UUID, UUID]],
    ) -> tuple[CorrelatedTransaction, ...]:
        if not ordered:
            return ()
        by_id = {document.id: document for document in ordered}
        parent = {document.id: document.id for document in ordered}
        edges: dict[frozenset[UUID], _PairScore] = {}

        def find(value: UUID) -> UUID:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def union(left_id: UUID, right_id: UUID) -> None:
            left_root, right_root = find(left_id), find(right_id)
            if left_root != right_root:
                first, second = sorted((left_root, right_root), key=str)
                parent[second] = first

        unique_pairs = {
            frozenset((left_id, right_id))
            for left_id, right_id in candidate_pairs
            if left_id != right_id and left_id in by_id and right_id in by_id
        }
        for pair in sorted(unique_pairs, key=lambda item: sorted(str(value) for value in item)):
            left_id, right_id = sorted(pair, key=str)
            score = self.score_candidate_pair(by_id[left_id], by_id[right_id])
            edges[pair] = score
            if score.score >= self.minimum_score:
                union(left_id, right_id)

        groups: dict[UUID, list[NormalizedDocument]] = defaultdict(list)
        for document in ordered:
            groups[find(document.id)].append(document)

        transactions = [
            self._build_transaction(group, edges)
            for _, group in sorted(groups.items(), key=lambda item: str(item[0]))
        ]
        transactions.sort(key=lambda item: (item.timeline[0].occurred_at, str(item.transaction_id)))
        return tuple(transactions)

    def _build_transaction(
        self,
        documents: list[NormalizedDocument],
        edges: dict[frozenset[UUID], _PairScore],
    ) -> CorrelatedTransaction:
        documents.sort(key=lambda item: (_document_time(item), str(item.id)))
        deterministic_name = f"{ALGORITHM_VERSION}:" + ":".join(
            sorted(str(document.id) for document in documents)
        )
        transaction_id = uuid5(NAMESPACE_URL, deterministic_name)
        correlation: CorrelationResult | None = None
        if len(documents) > 1:
            relevant = [
                edges[frozenset((left.id, right.id))]
                for index, left in enumerate(documents)
                for right in documents[index + 1 :]
                if frozenset((left.id, right.id)) in edges
                and edges[frozenset((left.id, right.id))].score >= self.minimum_score
            ]
            per_document: list[int] = []
            for document in documents:
                linked = [
                    edge.score for edge in relevant if document.id in {edge.left_id, edge.right_id}
                ]
                per_document.append(max(linked, default=0))
            score = min(per_document)
            matched = sorted(
                {
                    criterion.name
                    for edge in relevant
                    for criterion in edge.criteria
                    if criterion.matched
                }
            )
            unmatched = sorted(
                {
                    criterion.name
                    for edge in relevant
                    for criterion in edge.criteria
                    if not criterion.matched
                }
                - set(matched)
            )
            correlation = CorrelationResult(
                transaction_id=transaction_id,
                document_ids=tuple(document.id for document in documents),
                score=score,
                algorithm_version=ALGORITHM_VERSION,
                matched_criteria=tuple(matched),
                unmatched_criteria=tuple(unmatched),
                explanation=(
                    f"{len(documents)} documenti collegati con punteggio minimo {score}; "
                    f"criteri: {', '.join(matched)}"
                ),
            )

        prebills = [document for document in documents if document.type is DocumentType.PRE_BILL]
        prebill = prebills[-1] if prebills else None
        prebill_total = None if prebill is None else _total(prebill)
        fiscal_documents = [document for document in documents if document.type in _FISCAL_TYPES]
        valid_fiscal_documents = [
            document
            for document in fiscal_documents
            if document.complete and _total(document) is not None
        ]
        fiscal_total = sum(
            (
                -(_total(document) or ZERO)
                if document.type is DocumentType.REFUND
                else (_total(document) or ZERO)
            )
            for document in valid_fiscal_documents
        )
        economic_closures = [
            document
            for document in documents
            if document.type in _ECONOMIC_CLOSE_TYPES
            and document is not prebill
            and _total(document) is not None
            and (
                document in valid_fiscal_documents
                or bool(document.raw_metadata.get("economic_close"))
            )
        ]
        comparison_total = (
            fiscal_total
            if valid_fiscal_documents
            else sum((_total(document) or ZERO) for document in economic_closures)
        )
        payment_total = sum(
            payment.amount
            for payment in _authoritative_payments(documents, valid_fiscal_documents)
        )
        difference = None if prebill_total is None else prebill_total - comparison_total
        difference_percent = (
            None
            if prebill_total is None or prebill_total == ZERO
            else (difference / prebill_total * HUNDRED).quantize(Decimal("0.0001"))
        )
        final_lines = tuple(line for document in economic_closures for line in document.lines)
        line_changes = (
            compare_document_lines(prebill.lines, final_lines)
            if prebill is not None and prebill.lines and final_lines
            else ()
        )
        timeline = tuple(
            TimelineEntry(
                document_id=document.id,
                document_type=document.type,
                occurred_at=_document_time(document),
                gross_total=_total(document),
                source_device_id=document.source_device_id,
                source_job_id=document.source_job_id,
            )
            for document in documents
        )
        return CorrelatedTransaction(
            transaction_id=transaction_id,
            correlation=correlation,
            documents=tuple(documents),
            prebill_total=prebill_total,
            fiscal_total=fiscal_total,
            observed_final_total=comparison_total,
            payment_total=payment_total,
            difference_amount=difference,
            difference_percent=difference_percent,
            split_payment=len(valid_fiscal_documents) > 1 and fiscal_total > ZERO,
            line_changes=line_changes,
            timeline=timeline,
        )


def numeric_suffix(value: str | None) -> tuple[str, int] | None:
    """Return a stable prefix/number pair for sequence-gap analysis."""

    if not value or (match := re.fullmatch(r"(.*?)(\d+)", value.strip())) is None:
        return None
    return match.group(1), int(match.group(2))


__all__ = [
    "ALGORITHM_VERSION",
    "CorrelatedTransaction",
    "CorrelationEngine",
    "LineChange",
    "LineChangeType",
    "TimelineEntry",
    "compare_document_lines",
    "numeric_suffix",
]
