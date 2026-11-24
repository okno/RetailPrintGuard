from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from retailprintguard.analysis.discounts import (
    aggregate_document_discounts,
    analyze_document_discounts,
    confirmed_quantity_contexts,
)
from retailprintguard.api.schemas import DocumentLineView, DocumentView
from retailprintguard.correlation.engine import CorrelationEngine
from retailprintguard.fraud.engine import FraudContext, FraudEngine
from retailprintguard.parser.epson import parse_epson


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


def _epson_soap_sale(*, rate: int, multi_item: bool = False):
    """Build the two-sided protocol evidence used by the production parser."""

    def http(body: str, *, response: bool = False) -> bytes:
        encoded = body.encode("utf-8")
        first = "HTTP/1.1 200 OK" if response else "POST /cgi-bin/fpmate.cgi HTTP/1.1"
        return (
            f"{first}\r\nContent-Type: text/xml; charset=utf-8\r\n"
            f"Content-Length: {len(encoded)}\r\n\r\n"
        ).encode() + encoded

    items = (
        '<printRecItem description="Margherita sintetica A" quantity="1" unitPrice="8,00"/>'
        '<printRecItem description="Margherita sintetica B" quantity="1" unitPrice="8,00"/>'
        if multi_item else
        '<printRecItem description="The freddo sintetico" quantity="1" unitPrice="4,00"/>'
    )
    gross = Decimal("16.00") if multi_item else Decimal("4.00")
    discount = (gross * Decimal(rate) / 100).quantize(Decimal("0.01"))
    total = gross - discount
    amount = str(discount).replace(".", ",")
    payment = str(total).replace(".", ",")
    request = http(
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        '<printerFiscalReceipt><beginFiscalReceipt/>'
        f'{items}<printRecSubtotalAdjustment description="Sconto dipendenti {rate}% '
        f'(-{rate}%)" adjustmentType="1" amount="{amount}"/>'
        f'<printRecTotal description="Contanti" payment="{payment}" paymentType="0"/>'
        '<printRecMessage message="Tavolo: TEST-01"/>'
        '<endFiscalReceipt/></printerFiscalReceipt></s:Body></s:Envelope>'
    )
    response = http(
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        '<response success="true" code="" status="2"><addInfo>'
        '<elementList>lastCommand,printerStatus,fiscalReceiptNumber,'
        'fiscalReceiptAmount,fiscalReceiptDate,fiscalReceiptTime,zRepNumber</elementList>'
        '<lastCommand>74</lastCommand><printerStatus>20010</printerStatus>'
        '<fiscalReceiptNumber>1</fiscalReceiptNumber>'
        f'<fiscalReceiptAmount>{payment}</fiscalReceiptAmount>'
        '<fiscalReceiptDate>24/11/2026</fiscalReceiptDate>'
        '<fiscalReceiptTime>09:24</fiscalReceiptTime><zRepNumber>91</zRepNumber>'
        '</addInfo></response></s:Body></s:Envelope>',
        response=True,
    )
    receipt = parse_epson(
        request, response, device_id="epson_synthetic", session_id="synthetic-session",
        job_id=f"synthetic-{rate}-{multi_item}",
        captured_at=datetime(2026, 11, 24, 8, 24, tzinfo=UTC),
        manifest_sha256="a" * 64, source_path="/synthetic/client.raw",
        response_source_path="/synthetic/device.raw",
    )[0]
    assert receipt.complete
    return receipt


def test_confirmed_epson_soap_employee_discount_reconciles_without_printed_total_row() -> None:
    receipt = _epson_soap_sale(rate=30)
    assert all(line.description != "TOTALE" for line in receipt.lines)
    analysis = analyze_document_discounts(receipt)
    assert analysis.employee_total == Decimal("1.20")
    assert analysis.arithmetic_reconciled and analysis.reasons == ()
    assert analysis.observations[0].basis == Decimal("4.00")


def test_epson_api_projection_reconciles_without_exposing_raw_metadata() -> None:
    receipt = _epson_soap_sale(rate=30)
    view = DocumentView(
        id=receipt.id, device_id=receipt.source_device_id, job_id=uuid4(),
        type=receipt.type.value, subtype=receipt.subtype,
        external_document_code_suffix=receipt.external_document_code_suffix,
        captured_at=receipt.captured_at, document_timestamp=receipt.document_timestamp,
        document_timestamp_evidence=receipt.raw_metadata["document_timestamp_evidence"],
        gross_total=receipt.gross_total, net_total=receipt.net_total,
        discount_total=receipt.discount_total, status=receipt.status,
        normalized_text=receipt.normalized_text, parser_name=receipt.parser_name,
        parser_version=receipt.parser_version, confidence=receipt.parse_confidence,
        sha256=receipt.source_payload_sha256, complete=receipt.complete,
        lines=[DocumentLineView(**line.model_dump(exclude={"source"})) for line in receipt.lines],
        payments=[payment.model_dump(mode="json") for payment in receipt.payments],
    )
    assert not hasattr(view, "raw_metadata")
    result = analyze_document_discounts(view)
    assert result == analyze_document_discounts(receipt)
    assert not analyze_document_discounts(
        view.model_copy(update={"payments": []})
    ).arithmetic_reconciled


def test_confirmed_epson_multi_item_99_percent_discount_generates_critical_alert() -> None:
    receipt = _epson_soap_sale(rate=99, multi_item=True)
    analysis = analyze_document_discounts(receipt)
    assert analysis.arithmetic_reconciled and analysis.observations[0].basis == Decimal("16.00")
    transaction = CorrelationEngine().correlate((receipt,))[0]
    findings = FraudEngine().evaluate(FraudContext(
        transaction=transaction,
        evaluated_at=receipt.captured_at + timedelta(hours=1),
    ))
    extreme = [item for item in findings if item.rule_code == "EXTREME_PRINTED_DISCOUNT"]
    assert len(extreme) == 1 and extreme[0].severity.value == "CRITICAL"
    assert extreme[0].evidence[0]["discount_basis"] == "16.00"
    assert extreme[0].evidence[0]["discount_amount"] == "15.84"


@pytest.mark.parametrize("tamper", [
    "incomplete", "wrong_parser", "unacknowledged", "unpaired", "bad_fiscal_span",
    "wrong_subtotal", "wrong_total", "wrong_payment", "missing_line_source",
    "wrong_modifier", "wrong_line_total",
])
def test_epson_response_total_requires_provenance_and_independent_arithmetic(tamper) -> None:
    receipt = _epson_soap_sale(rate=99, multi_item=True)
    metadata = copy.deepcopy(receipt.raw_metadata)
    if tamper == "incomplete":
        receipt = receipt.model_copy(update={"complete": False})
    elif tamper == "wrong_parser":
        receipt = receipt.model_copy(update={"parser_name": "synthetic-other-parser"})
    elif tamper == "unacknowledged":
        metadata["command_acknowledged"] = False
    elif tamper == "unpaired":
        metadata["response_paired"] = False
    elif tamper == "bad_fiscal_span":
        metadata["fiscal_total_source_span"]["direction"] = "CLIENT_TO_DEVICE"
    elif tamper == "wrong_subtotal":
        metadata["observed_item_subtotal"] = "17.00"
    elif tamper == "wrong_total":
        receipt = receipt.model_copy(update={"gross_total": Decimal("0.17")})
    elif tamper == "wrong_payment":
        receipt = receipt.model_copy(update={"payments": ()})
    elif tamper == "missing_line_source":
        lines = list(receipt.lines)
        lines[0] = lines[0].model_copy(update={"source": None})
        receipt = receipt.model_copy(update={"lines": tuple(lines)})
    elif tamper == "wrong_modifier":
        metadata["subtotal_modifiers"] = []
    elif tamper == "wrong_line_total":
        lines = list(receipt.lines)
        lines[0] = lines[0].model_copy(update={"line_total": Decimal("7.00")})
        receipt = receipt.model_copy(update={"lines": tuple(lines)})
    receipt = receipt.model_copy(update={"raw_metadata": metadata})
    analysis = analyze_document_discounts(receipt)
    assert not analysis.arithmetic_reconciled
    assert "printed_total_missing" in analysis.reasons


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
