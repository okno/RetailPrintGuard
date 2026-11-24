from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from retailprintguard.analysis.discounts import (
    aggregate_document_discounts,
    analyze_document_discounts,
    confirmed_quantity_contexts,
)


def _row(sequence, description, amount, **extra):
    return {
        "sequence": sequence,
        "description": description,
        "line_total": amount,
        "raw_text": description,
        **extra,
    }


def _document(label="SCONTO DIPENDENTI 30% [-30%]", discount="-3.60", total="8.40"):
    return {
        "complete": True,
        "gross_total": total,
        "lines": [
            _row(1, "Articolo sintetico", "12.00"),
            _row(2, label, discount),
            _row(3, "TOT", total),
        ],
    }


def test_explicit_employee_evidence_is_separate_from_generic_and_authorization() -> None:
    employee = analyze_document_discounts(_document())
    generic = analyze_document_discounts(_document("Sconto 30%"))
    assert employee.employee_total == Decimal("3.60")
    assert employee.generic_30_total == 0 and employee.arithmetic_reconciled
    assert employee.observations[0].basis == 12
    assert employee.observations[0].arithmetic_verified
    assert generic.employee_total == 0 and generic.generic_30_total == Decimal("3.60")
    assert generic.observations[0].kind == "generic_30"
    # No authorization or fiscal-correlation decision is manufactured here.
    assert not hasattr(employee, "authorized")


@pytest.mark.parametrize(
    "label",
    [
        "SCONTI DIPENDENTI 30,00% [-30.0%]",
        "Sconto dipendente 30.00%",
        " Sconto  dipendenti 30 % ",
        "Sconto dipendenti 30% (-30%)",
    ],
)
def test_equivalent_exact_labels_keep_the_same_rate(label) -> None:
    analysis = analyze_document_discounts(_document(label))
    assert analysis.employee_total == Decimal("3.60") and analysis.arithmetic_reconciled


@pytest.mark.parametrize(
    "label",
    [
        "SCONTO PROMO 30%",
        "SCONTO DIPENDENTI 20% nota 30%",
        "SCONTO DIPENDENTI 30% [-20%]",
        "SCONTO DIPENDENTI 30% (-20%)",
    ],
)
def test_ambiguous_labels_are_not_employee_evidence(label) -> None:
    analysis = analyze_document_discounts(_document(label))
    assert analysis.employee_total == 0
    assert analysis.other_discount_total == Decimal("3.60")


def test_numerical_reduction_alone_is_not_an_observed_employee_discount() -> None:
    document = _document()
    document["lines"].pop(1)
    result = analyze_document_discounts(document)
    assert result.observations == () and result.employee_total == 0
    assert not result.arithmetic_reconciled


def test_quantity_context_legacy_read_model_matches_reparsed_values_without_mutation() -> None:
    document = _document()
    document["lines"].insert(0, _row(0, "4x", "3.00", raw_text=" 4x 3,00"))
    before = copy.deepcopy(document)
    result = analyze_document_discounts(document)
    assert document == before
    assert result.employee_total == Decimal("3.60") and result.arithmetic_reconciled


@pytest.mark.parametrize(
    "products,subtotal,discount,total",
    [
        (("10.00", "12.00", "18.00"), "40.00", "-39.60", "0.40"),
        (("20.00", "40.00"), "60.00", "-59.40", "0.60"),
    ],
)
@pytest.mark.parametrize("subtotal_label", ("SUBTOT", "SUBTOTALE", "SUB-TOTALE", "SUBTOTAL"))
def test_printed_subtotal_is_discount_basis_without_double_counting_sales(
    products, subtotal, discount, total, subtotal_label
) -> None:
    rows = [
        _row(index, f"Articolo sintetico {index}", amount)
        for index, amount in enumerate(products, 1)
    ]
    rows.extend(
        (
            _row(len(rows) + 1, subtotal_label, subtotal),
            _row(len(rows) + 2, "Sconto 99%", discount),
            _row(len(rows) + 3, "TOT", total),
        )
    )
    analysis = analyze_document_discounts(
        {"complete": True, "gross_total": total, "lines": rows}
    )
    assert analysis.arithmetic_reconciled
    assert analysis.other_discount_total == -Decimal(discount)
    assert analysis.observations[0].basis == Decimal(subtotal)
    assert analysis.observations[0].rate_percent == Decimal("99")
    assert analysis.observations[0].arithmetic_verified


def test_unmatched_printed_subtotal_does_not_authorize_discount_basis() -> None:
    rows = [
        _row(1, "Articolo sintetico", "60.00"),
        _row(2, "SUBTOT", "61.00"),
        _row(3, "Sconto 99%", "-60.39"),
        _row(4, "TOT", "0.61"),
    ]
    analysis = analyze_document_discounts(
        {"complete": True, "gross_total": "0.61", "lines": rows}
    )
    assert not analysis.arithmetic_reconciled
    assert "subtotal_not_reconciled" in analysis.reasons
    assert not analysis.observations[0].arithmetic_verified


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda d: d.update(complete=False), "document_incomplete"),
        (lambda d: d.update(gross_total="8.41"), "document_total_not_reconciled"),
        (lambda d: d["lines"][1].update(line_total="-3.50"), "discount_basis_unverified"),
        (lambda d: d["lines"].insert(1, _row(8, "", None)), "discount_basis_unverified"),
        (lambda d: d["lines"].pop(), "printed_total_missing"),
    ],
)
def test_unreconciled_observations_remain_visible_with_explicit_reason(mutation, reason) -> None:
    document = _document()
    mutation(document)
    result = analyze_document_discounts(document)
    assert result.employee_total > 0 and not result.arithmetic_reconciled
    assert reason in result.reasons


def test_positive_or_after_total_discount_does_not_become_negative_sale() -> None:
    positive = analyze_document_discounts(_document(discount="3.60"))
    assert positive.employee_total == 0
    assert "discount_without_negative_amount" in positive.reasons
    document = _document()
    document["lines"].append(_row(4, "SCONTO DIPENDENTI 30%", "-2.52"))
    result = analyze_document_discounts(document)
    assert result.employee_total == Decimal("3.60")
    assert "discount_after_total" in result.reasons and not result.arithmetic_reconciled


def test_limits_nonfinite_numbers_and_removed_rows_fail_closed() -> None:
    document = _document()
    document["lines"][1]["line_total"] = "NaN"
    result = analyze_document_discounts(document)
    assert result.employee_total == 0 and not result.arithmetic_reconciled
    document = _document()
    document["lines"][1]["removed"] = True
    assert "inactive_sale_row" in analyze_document_discounts(document).reasons
    document = {"lines": [{}] * 32_769, "complete": True}
    assert analyze_document_discounts(document).reasons == ("line_limit",)


def test_aggregation_counts_document_identity_not_equal_amounts_or_rows() -> None:
    first = {**_document(), "id": "synthetic-a"}
    second = {**_document(), "id": "synthetic-b"}
    generic = {**_document("SCONTO 30%"), "id": "synthetic-c"}
    partial = {**_document(), "id": "synthetic-partial", "complete": False}
    result = aggregate_document_discounts([first, copy.deepcopy(first), second, generic, partial])
    assert result.observed_employee_discount_total == Decimal("10.80")
    assert result.observed_employee_discount_count == 3
    assert result.observed_generic_30_discount_total == Decimal("3.60")
    assert result.observed_generic_30_discount_count == 1
    assert result.arithmetic_reconciled_employee_discount_total == Decimal("7.20")
    assert result.arithmetic_reconciled_employee_discount_count == 2


def test_aggregation_rejects_conflicting_interpretations_of_same_document() -> None:
    first = {**_document(), "id": "synthetic-a"}
    other = {**_document(discount="-3.50"), "id": "synthetic-a"}
    with pytest.raises(ValueError, match="Conflicting selected discount interpretations"):
        aggregate_document_discounts([first, other])


def test_long_labels_and_extreme_numbers_cannot_become_truncated_employee_evidence() -> None:
    document = _document("SCONTO DIPENDENTI 30%" + " " * 2_048 + "PROMO")
    result = analyze_document_discounts(document)
    assert result.employee_total == 0 and "line_text_limit" in result.reasons
    for value in ("1e99999999", "1e-99999999", "9" * 129):
        document = _document()
        document["lines"][1]["line_total"] = value
        assert analyze_document_discounts(document).employee_total == 0
    huge = "2x " + "999." * 20 + "999,00"
    assert (
        confirmed_quantity_contexts(
            [
                _row(1, "2x", "1.00", raw_text=huge),
                _row(2, "Articolo sintetico", "2.00"),
            ]
        )
        == ()
    )
