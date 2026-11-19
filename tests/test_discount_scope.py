from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from retailprintguard.api.discounts import (
    DISCOUNT_SALE_DOCUMENT_TYPES,
    analyze_sale_document_discounts,
)


def _sale():
    return {
        "type": "MANAGEMENT_DOCUMENT", "complete": True,
        "normalized_text": "Articolo 10,00\nSconto dipendenti 30% -3,00\nTOT 7,00",
        "gross_total": "7.00",
        "lines": [
            {"sequence": 1, "description": "Articolo", "line_total": "10.00"},
            {"sequence": 2, "description": "Sconto dipendenti 30%", "line_total": "-3.00"},
            {"sequence": 3, "description": "TOT", "line_total": "7.00"},
        ],
    }


@pytest.mark.parametrize("kind", sorted(DISCOUNT_SALE_DOCUMENT_TYPES))
def test_sale_scope_keeps_explicit_discount_without_identity_requirements(kind):
    document = {**_sale(), "type": kind}
    before = deepcopy(document)
    result = analyze_sale_document_discounts(document)
    assert result.employee_total == Decimal("3.00") and result.arithmetic_reconciled
    assert document == before


@pytest.mark.parametrize("kind", [
    "SHIFT_END_REPORT", "DEVICE_RESPONSE", "UNKNOWN", "CANCELLATION",
    "REFUND", "CONFORMING_COPY", "REPRINT", "PAYMENT", "INVOICE", None,
])
def test_non_sale_summary_cannot_manufacture_another_observed_discount(kind):
    result = analyze_sale_document_discounts({**_sale(), "type": kind})
    assert not result.observations
    assert result.employee_total == result.generic_30_total == result.other_discount_total == 0
    assert not result.arithmetic_reconciled and result.reasons == ("document_type_not_sale",)


@pytest.mark.parametrize("as_object", [False, True])
def test_printable_scope_uses_original_receipt_projection_and_ignores_configured_header(as_object):
    document = {
        **_sale(), "receipt_text": "Scontrino: SF 9999-9999",
        "receipt_preview": "Configured header\nArticolo 10,00\nTOT 7,00",
    }
    result = analyze_sale_document_discounts(SimpleNamespace(**document) if as_object else document)
    assert not result.observations and result.reasons == ("document_not_printable",)


def test_readable_partial_is_observed_without_claiming_complete_quadrature():
    result = analyze_sale_document_discounts({**_sale(), "complete": False})
    assert result.employee_total == Decimal("3.00")
    assert not result.arithmetic_reconciled
