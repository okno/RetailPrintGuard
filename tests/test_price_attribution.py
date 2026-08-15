from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from retailprintguard.pricing.service import (
    PriceLineEvidence,
    derive_document_attributions,
)

NOW = datetime(2042, 8, 15, 12, 0, tzinfo=UTC)
CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000001")
TARGET_DOCUMENT_ID = UUID("00000000-0000-0000-0000-000000000002")
TARGET_VERSION_ID = UUID("00000000-0000-0000-0000-000000000003")
SOURCE_DOCUMENT_ID = UUID("00000000-0000-0000-0000-000000000004")
SOURCE_VERSION_ID = UUID("00000000-0000-0000-0000-000000000005")


def _line(
    number: int,
    *,
    target: bool = False,
    item_code: str | None = None,
    description: str | None = "Margherita",
    quantity: str | None = "1",
    unit_price: str | None = None,
    line_total: str | None = None,
) -> PriceLineEvidence:
    return PriceLineEvidence(
        line_id=UUID(f"00000000-0000-0000-0000-{number:012d}"),
        document_id=TARGET_DOCUMENT_ID if target else SOURCE_DOCUMENT_ID,
        document_version_id=TARGET_VERSION_ID if target else SOURCE_VERSION_ID,
        sequence=number,
        item_code=item_code,
        description=description,
        quantity=None if quantity is None else Decimal(quantity),
        unit_price=None if unit_price is None else Decimal(unit_price),
        line_total=None if line_total is None else Decimal(line_total),
        observed_at=NOW if target else NOW + timedelta(minutes=5),
    )


def _derive(
    target: PriceLineEvidence,
    *sources: PriceLineEvidence,
):
    return derive_document_attributions(
        correlation_id=CORRELATION_ID,
        target=target,
        source_kind="PREBILL",
        source_document_id=SOURCE_DOCUMENT_ID,
        source_document_version_id=SOURCE_VERSION_ID,
        source_lines=tuple(sources),
    )


def test_exact_item_code_has_priority_over_description_and_preserves_provenance() -> None:
    target = _line(10, target=True, item_code="PIZZA-1")
    matching_code = _line(
        20,
        item_code="PIZZA-1",
        description="Descrizione cambiata",
        unit_price="8.50",
        line_total="8.50",
    )
    matching_description_only = _line(
        21,
        item_code="PIZZA-2",
        description="  MARGHERITA  ",
        unit_price="9.50",
        line_total="9.50",
    )

    result = _derive(target, matching_description_only, matching_code)

    assert len(result) == 1
    assert result[0].source_line_id == matching_code.line_id
    assert result[0].match_basis == "ITEM_CODE_EXACT"
    assert result[0].status == "RESOLVED"
    assert result[0].observed_unit_price == Decimal("8.5000")
    assert result[0].criteria["temporal_relation"] == "SOURCE_AFTER_TARGET"


def test_normalized_description_and_observed_line_total_derive_unit_price() -> None:
    target = _line(10, target=True, description="Caffè   lungo", quantity="2")
    source = _line(
        20,
        description="CAFFÈ LUNGO",
        quantity="2",
        unit_price=None,
        line_total="5.00",
    )

    result = _derive(target, source)

    assert len(result) == 1
    assert result[0].match_basis == "DESCRIPTION_NORMALIZED_EXACT"
    assert result[0].observed_unit_price == Decimal("2.5000")
    assert result[0].observed_line_total == Decimal("5.0000")
    assert result[0].criteria["unit_price_origin"] == "DERIVED_FROM_OBSERVED_LINE_TOTAL"


def test_agreeing_sources_are_all_retained_without_arbitrary_selection() -> None:
    target = _line(10, target=True)
    first = _line(20, unit_price="7.00", line_total="7.00")
    second = _line(21, unit_price="7.00", line_total="7.00")

    result = _derive(target, second, first)

    assert [item.source_line_id for item in result] == [first.line_id, second.line_id]
    assert {item.status for item in result} == {"AGREED"}
    assert {item.observed_unit_price for item in result} == {Decimal("7.0000")}
    assert all(item.ambiguity_group is None for item in result)


def test_conflicting_sources_are_explicitly_ambiguous() -> None:
    target = _line(10, target=True)
    first = _line(20, unit_price="7.00", line_total="7.00")
    second = _line(21, unit_price="8.00", line_total="8.00")

    result = _derive(target, first, second)

    assert len(result) == 2
    assert {item.status for item in result} == {"AMBIGUOUS"}
    assert len({item.ambiguity_group for item in result}) == 1
    assert all(item.ambiguity_group is not None for item in result)
    assert all(item.confidence <= Decimal("0.4900") for item in result)


def test_quantity_conflict_is_not_silently_matched() -> None:
    target = _line(10, target=True, quantity="2")
    source = _line(20, quantity="1", unit_price="7.00", line_total="7.00")

    assert _derive(target, source) == ()


def test_line_total_without_quantity_is_retained_but_not_promoted_to_unit_price() -> None:
    target = _line(10, target=True, quantity=None)
    source = _line(20, quantity=None, unit_price=None, line_total="12.00")

    result = _derive(target, source)

    assert len(result) == 1
    assert result[0].observed_unit_price is None
    assert result[0].observed_line_total == Decimal("12.0000")
    assert result[0].criteria["unit_price_origin"] == "UNAVAILABLE"
