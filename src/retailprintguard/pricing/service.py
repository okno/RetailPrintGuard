"""Conservative price attribution for non-monetary POS lines.

The service consumes only already persisted control-plane interpretations.  It
never mutates parsed lines or RAW evidence and has no dependency on the proxy
data path.  An attribution is a versioned inference, not a replacement for an
observed POS value.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from retailprintguard.common.domain import DocumentType, NormalizedDocument
from retailprintguard.common.hashchain import canonical_json
from retailprintguard.db.models import DocumentLine, LinePriceAttribution

PRICE_ATTRIBUTION_ALGORITHM = "line-price-attribution/1.0.0"
_MONEY_QUANTUM = Decimal("0.0001")
_TARGET_TYPES = {
    DocumentType.ORDER,
    DocumentType.ORDER_CHANGE,
    DocumentType.KITCHEN_ORDER,
}
_SOURCE_KINDS = {
    DocumentType.PRE_BILL: "PREBILL",
    DocumentType.MANAGEMENT_DOCUMENT: "MANAGEMENT",
    DocumentType.COMMERCIAL_DOCUMENT: "FISCAL",
}


class _LoadedDocument(Protocol):
    value: NormalizedDocument
    version_id: UUID


@dataclass(frozen=True, slots=True)
class PriceLineEvidence:
    line_id: UUID
    document_id: UUID
    document_version_id: UUID
    sequence: int
    item_code: str | None
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    line_total: Decimal | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class DerivedPriceAttribution:
    id: UUID
    correlation_id: UUID
    target_document_id: UUID
    target_document_version_id: UUID
    target_line_id: UUID
    source_document_id: UUID
    source_document_version_id: UUID
    source_line_id: UUID
    observed_unit_price: Decimal | None
    observed_line_total: Decimal | None
    target_quantity: Decimal | None
    source_quantity: Decimal | None
    source_kind: Literal["PREBILL", "MANAGEMENT", "FISCAL"]
    match_basis: Literal["ITEM_CODE_EXACT", "DESCRIPTION_NORMALIZED_EXACT"]
    algorithm_version: str
    confidence: Decimal
    status: Literal["RESOLVED", "AGREED", "AMBIGUOUS"]
    criteria: dict[str, Any]
    ambiguity_group: str | None
    attribution_fingerprint: str
    source_observed_at: datetime


def _normalise_code(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _normalise_description(value: str | None) -> str | None:
    if value is None:
        return None
    normalised = unicodedata.normalize("NFKC", value).casefold()
    normalised = re.sub(r"\s+", " ", normalised).strip()
    return normalised or None


def _normalise_money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _effective_unit_price(line: PriceLineEvidence) -> tuple[Decimal | None, str]:
    if line.unit_price is not None:
        return _normalise_money(line.unit_price), "OBSERVED_UNIT_PRICE"
    if line.line_total is None or line.quantity in {None, Decimal("0")}:
        return None, "UNAVAILABLE"
    return (
        _normalise_money(line.line_total / abs(line.quantity)),
        "DERIVED_FROM_OBSERVED_LINE_TOTAL",
    )


def _quantity_compatibility(
    target: PriceLineEvidence, source: PriceLineEvidence
) -> tuple[bool, str]:
    if target.quantity is None or source.quantity is None:
        return True, "UNKNOWN"
    if abs(target.quantity) == abs(source.quantity):
        return True, "ABSOLUTE_EXACT"
    return False, "CONFLICT"


def _price_signature(line: PriceLineEvidence) -> tuple[Decimal | None, Decimal | None]:
    unit_price, _ = _effective_unit_price(line)
    return unit_price, _normalise_money(line.line_total)


def _has_observed_amount(line: PriceLineEvidence) -> bool:
    return line.unit_price is not None or line.line_total is not None


def _candidates(
    target: PriceLineEvidence,
    sources: tuple[PriceLineEvidence, ...],
) -> tuple[
    Literal["ITEM_CODE_EXACT", "DESCRIPTION_NORMALIZED_EXACT"] | None,
    tuple[PriceLineEvidence, ...],
]:
    target_code = _normalise_code(target.item_code)
    if target_code is not None:
        by_code = tuple(
            source
            for source in sources
            if _normalise_code(source.item_code) == target_code
            and _quantity_compatibility(target, source)[0]
            and _has_observed_amount(source)
        )
        if by_code:
            return "ITEM_CODE_EXACT", by_code

    target_description = _normalise_description(target.description)
    if target_description is None:
        return None, ()
    by_description = tuple(
        source
        for source in sources
        if _normalise_description(source.description) == target_description
        and _quantity_compatibility(target, source)[0]
        and _has_observed_amount(source)
    )
    if by_description:
        return "DESCRIPTION_NORMALIZED_EXACT", by_description
    return None, ()


def _sources_agree(candidates: tuple[PriceLineEvidence, ...]) -> bool:
    signatures = tuple(_price_signature(candidate) for candidate in candidates)
    unit_prices = {unit for unit, _total in signatures if unit is not None}
    line_totals = {total for _unit, total in signatures if total is not None}
    all_units_comparable = all(unit is not None for unit, _total in signatures)
    all_totals_comparable = all(total is not None for _unit, total in signatures)
    if all_units_comparable:
        return len(unit_prices) == 1 and len(line_totals) <= 1
    if all_totals_comparable:
        return len(line_totals) == 1 and len(unit_prices) <= 1
    return False


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def derive_document_attributions(
    *,
    correlation_id: UUID,
    target: PriceLineEvidence,
    source_kind: Literal["PREBILL", "MANAGEMENT", "FISCAL"],
    source_document_id: UUID,
    source_document_version_id: UUID,
    source_lines: tuple[PriceLineEvidence, ...],
    algorithm_version: str = PRICE_ATTRIBUTION_ALGORITHM,
) -> tuple[DerivedPriceAttribution, ...]:
    """Match one POS line against one source-document version.

    Candidate selection is intentionally exact.  Item code wins over
    description, quantities must not conflict, and a conflict between monetary
    candidates is surfaced rather than resolved heuristically.
    """

    basis, candidates = _candidates(target, source_lines)
    if basis is None or not candidates:
        return ()
    candidates = tuple(sorted(candidates, key=lambda item: (item.sequence, str(item.line_id))))
    agreement = len(candidates) > 1 and _sources_agree(candidates)
    status: Literal["RESOLVED", "AGREED", "AMBIGUOUS"] = (
        "RESOLVED" if len(candidates) == 1 else "AGREED" if agreement else "AMBIGUOUS"
    )
    ambiguity_group = (
        _fingerprint(
            {
                "algorithm_version": algorithm_version,
                "correlation_id": str(correlation_id),
                "target_line_id": str(target.line_id),
                "source_document_version_id": str(source_document_version_id),
                "candidate_line_ids": [str(candidate.line_id) for candidate in candidates],
            }
        )
        if status == "AMBIGUOUS"
        else None
    )
    signatures = [
        {
            "unit_price": None if unit is None else str(unit),
            "line_total": None if total is None else str(total),
        }
        for unit, total in (_price_signature(candidate) for candidate in candidates)
    ]
    result: list[DerivedPriceAttribution] = []
    for source in candidates:
        unit_price, unit_origin = _effective_unit_price(source)
        quantity_compatible, quantity_basis = _quantity_compatibility(target, source)
        if not quantity_compatible:  # defensive: candidates already enforce this
            continue
        confidence = Decimal("0.9800") if basis == "ITEM_CODE_EXACT" else Decimal("0.9000")
        if quantity_basis == "UNKNOWN":
            confidence -= Decimal("0.1000")
        if unit_origin == "DERIVED_FROM_OBSERVED_LINE_TOTAL":
            confidence -= Decimal("0.0300")
        elif unit_origin == "UNAVAILABLE":
            confidence -= Decimal("0.1500")
        if status == "AGREED":
            confidence = min(Decimal("0.9900"), confidence + Decimal("0.0100"))
        elif status == "AMBIGUOUS":
            confidence = min(confidence, Decimal("0.4900"))
        delta_seconds = int((source.observed_at - target.observed_at).total_seconds())
        identity = {
            "algorithm_version": algorithm_version,
            "correlation_id": str(correlation_id),
            "target_line_id": str(target.line_id),
            "source_line_id": str(source.line_id),
        }
        attribution_fingerprint = _fingerprint(identity)
        result.append(
            DerivedPriceAttribution(
                id=uuid5(NAMESPACE_URL, f"retailprintguard:{attribution_fingerprint}"),
                correlation_id=correlation_id,
                target_document_id=target.document_id,
                target_document_version_id=target.document_version_id,
                target_line_id=target.line_id,
                source_document_id=source_document_id,
                source_document_version_id=source_document_version_id,
                source_line_id=source.line_id,
                observed_unit_price=unit_price,
                observed_line_total=_normalise_money(source.line_total),
                target_quantity=target.quantity,
                source_quantity=source.quantity,
                source_kind=source_kind,
                match_basis=basis,
                algorithm_version=algorithm_version,
                confidence=confidence,
                status=status,
                criteria={
                    "candidate_count": len(candidates),
                    "candidate_price_signatures": signatures,
                    "item_code_priority": basis == "ITEM_CODE_EXACT",
                    "quantity_compatibility": quantity_basis,
                    "unit_price_origin": unit_origin,
                    "temporal_relation": (
                        "SOURCE_AFTER_TARGET"
                        if delta_seconds > 0
                        else "SOURCE_BEFORE_TARGET"
                        if delta_seconds < 0
                        else "SIMULTANEOUS"
                    ),
                    "temporal_distance_seconds": abs(delta_seconds),
                },
                ambiguity_group=ambiguity_group,
                attribution_fingerprint=attribution_fingerprint,
                source_observed_at=source.observed_at,
            )
        )
    return tuple(result)


def _evidence_line(
    line: DocumentLine,
    *,
    document: NormalizedDocument,
    version_id: UUID,
) -> PriceLineEvidence:
    return PriceLineEvidence(
        line_id=line.id,
        document_id=document.id,
        document_version_id=version_id,
        sequence=line.sequence,
        item_code=line.item_code,
        description=line.description,
        quantity=line.quantity,
        unit_price=line.unit_price,
        line_total=line.line_total,
        observed_at=document.document_timestamp or document.captured_at,
    )


def persist_transaction_price_attributions(
    session: Session,
    *,
    correlation_id: UUID,
    documents: tuple[NormalizedDocument, ...],
    version_ids: dict[UUID, UUID],
) -> int:
    """Append missing attributions for one persisted sale correlation.

    Idempotency is enforced both by a pre-insert lookup and by database unique
    constraints.  Reprocessing with a newer algorithm version appends new rows
    and leaves prior interpretations available for audit.
    """

    targets = tuple(document for document in documents if document.type in _TARGET_TYPES)
    # An incomplete monetary document is useful evidence of capture/parsing
    # degradation, but it is not authoritative enough to price a POS line.
    # Omitting it here prevents both RESOLVED rows and downstream display
    # projections from silently promoting partial evidence to a fact.
    sources = tuple(
        document
        for document in documents
        if document.type in _SOURCE_KINDS and document.complete
    )
    if not targets or not sources:
        return 0
    relevant_version_ids = {
        version_ids[document.id] for document in (*targets, *sources) if document.id in version_ids
    }
    if not relevant_version_ids:
        return 0
    stored_lines = session.scalars(
        select(DocumentLine)
        .where(DocumentLine.document_version_id.in_(relevant_version_ids))
        .order_by(DocumentLine.document_version_id, DocumentLine.sequence, DocumentLine.id)
    ).all()
    lines_by_version: dict[UUID, list[DocumentLine]] = {}
    for line in stored_lines:
        lines_by_version.setdefault(line.document_version_id, []).append(line)

    derived: list[DerivedPriceAttribution] = []
    for target_document in targets:
        target_version_id = version_ids.get(target_document.id)
        if target_version_id is None:
            continue
        target_lines = lines_by_version.get(target_version_id, [])
        # An observed POS unit price remains authoritative and does not need a
        # derived replacement.  Lines with only a total can still receive a
        # sourced unit-price attribution.
        target_evidence = tuple(
            _evidence_line(line, document=target_document, version_id=target_version_id)
            for line in target_lines
            if line.unit_price is None
        )
        for source_document in sources:
            source_version_id = version_ids.get(source_document.id)
            if source_version_id is None:
                continue
            source_evidence = tuple(
                _evidence_line(line, document=source_document, version_id=source_version_id)
                for line in lines_by_version.get(source_version_id, [])
                if not line.cancelled
                and not line.removed
                and (line.unit_price is not None or line.line_total is not None)
            )
            if not source_evidence:
                continue
            source_kind = _SOURCE_KINDS[source_document.type]
            for target_line in target_evidence:
                derived.extend(
                    derive_document_attributions(
                        correlation_id=correlation_id,
                        target=target_line,
                        source_kind=source_kind,  # type: ignore[arg-type]
                        source_document_id=source_document.id,
                        source_document_version_id=source_version_id,
                        source_lines=source_evidence,
                    )
                )

    fingerprints = {item.attribution_fingerprint for item in derived}
    existing = (
        set(
            session.scalars(
                select(LinePriceAttribution.attribution_fingerprint).where(
                    LinePriceAttribution.attribution_fingerprint.in_(fingerprints)
                )
            )
        )
        if fingerprints
        else set()
    )
    inserted = 0
    for item in derived:
        if item.attribution_fingerprint in existing:
            continue
        session.add(LinePriceAttribution(**asdict(item)))
        existing.add(item.attribution_fingerprint)
        inserted += 1
    if inserted:
        session.flush()
    return inserted


__all__ = [
    "PRICE_ATTRIBUTION_ALGORITHM",
    "DerivedPriceAttribution",
    "PriceLineEvidence",
    "derive_document_attributions",
    "persist_transaction_price_attributions",
]
