from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from retailprintguard.common.domain import (
    DocumentLine,
    DocumentType,
    EvidenceLevel,
    NormalizedDocument,
    OrderEvent,
    OrderEventType,
    PaymentRecord,
    SourceSpan,
)
from retailprintguard.common.hashchain import verify_chain
from retailprintguard.correlation import (
    ALGORITHM_VERSION,
    CorrelationEngine,
    LineChangeType,
    apply_order_change_lines,
    compare_document_lines,
)
from retailprintguard.fraud import (
    DEFAULT_RULES,
    FraudContext,
    FraudEngine,
    WhitelistEntry,
    WhitelistScope,
    authorized_employee_discount,
    finding_chain_record,
)

BASE_TIME = datetime(2042, 5, 6, 18, 0, tzinfo=UTC)


def _id(name: str):
    return uuid5(NAMESPACE_URL, f"retailprintguard-test:{name}")


def _line(sequence: int, code: str, price: str) -> DocumentLine:
    amount = Decimal(price)
    return DocumentLine(
        sequence=sequence,
        item_code=code,
        description=f"Articolo {code}",
        quantity=Decimal("1"),
        unit_price=amount,
        line_total=amount,
    )


def _document(
    name: str,
    document_type: DocumentType,
    total: str,
    lines: tuple[DocumentLine, ...],
    *,
    minute: int,
    device: str,
    external_code: str | None,
    table_code: str = "T-7",
    external_code_suffix: str | None = None,
    commercial_reference_code: str | None = None,
    payments: tuple[PaymentRecord, ...] = (),
) -> NormalizedDocument:
    return NormalizedDocument(
        id=_id(name),
        source_device_id=device,
        source_session_id=f"session-{device}",
        source_job_id=f"job-{name}",
        type=document_type,
        subtype=document_type.value,
        external_document_code=external_code,
        external_document_code_suffix=external_code_suffix,
        commercial_reference_code=commercial_reference_code,
        order_code="ORD-80",
        table_code=table_code,
        operator_code="OP-7",
        terminal_code="TERM-1" if device.startswith("pos") else "RCH-1",
        document_timestamp=BASE_TIME + timedelta(minutes=minute),
        captured_at=BASE_TIME + timedelta(minutes=minute, seconds=1),
        gross_total=Decimal(total),
        net_total=Decimal(total),
        status="COMPLETE",
        normalized_text=f"Documento sintetico {name}",
        parser_name="synthetic",
        parser_version="1",
        parse_confidence=100,
        evidence=EvidenceLevel.CONFIRMED,
        source_manifest_sha256=hashlib.sha256(f"manifest:{name}".encode()).hexdigest(),
        source_payload_sha256=hashlib.sha256(f"payload:{name}".encode()).hexdigest(),
        source_path=f"synthetic/{name}",
        complete=True,
        lines=lines,
        payments=payments,
        raw_metadata={
            "order_reference": "REFERENCE-80",
            **(
                {
                    "external_document_code_suffix_evidence": (
                        "RCH_STATUS_RESPONSE_SUFFIX_SEQUENCE_CONFIRMED"
                    )
                }
                if external_code_suffix is not None
                else {}
            ),
        },
    )


def test_printed_management_subtotal_discount_above_90_percent_is_alerted() -> None:
    def receipt(name: str, products: tuple[str, ...], discount: str, total: str):
        basis = sum((Decimal(item) for item in products), Decimal("0.00"))
        entries = [
            DocumentLine(
                sequence=index,
                description=f"Articolo sintetico {index}",
                raw_text=f"Articolo sintetico {index}",
                quantity=Decimal("1"),
                unit_price=Decimal(amount),
                line_total=Decimal(amount),
            )
            for index, amount in enumerate(products, 1)
        ]
        for label, amount in (
            ("SUBTOT", basis),
            ("Sconto 99%", Decimal(discount)),
            ("TOT", Decimal(total)),
        ):
            entries.append(
                DocumentLine(
                    sequence=len(entries) + 1,
                    description=label,
                    raw_text=label,
                    quantity=Decimal("1"),
                    unit_price=amount,
                    line_total=amount,
                )
            )
        return _document(
            name,
            DocumentType.MANAGEMENT_DOCUMENT,
            total,
            tuple(entries),
            minute=0,
            device="rch_synthetic",
            external_code=None,
        )

    for document, expected_basis, expected_discount in (
        (
            receipt("large-subtotal", ("10.00", "12.00", "18.00"), "-39.60", "0.40"),
            "40.00",
            "39.60",
        ),
        (
            receipt("small-subtotal", ("20.00", "40.00"), "-59.40", "0.60"),
            "60.00",
            "59.40",
        ),
    ):
        assert document.discount_total is None
        transaction = CorrelationEngine().correlate((document,))[0]
        alerts = [
            item
            for item in FraudEngine().evaluate(
                FraudContext(transaction=transaction, evaluated_at=BASE_TIME + timedelta(hours=1))
            )
            if item.rule_code == "EXTREME_PRINTED_DISCOUNT"
        ]
        assert len(alerts) == 1
        assert alerts[0].severity.value == "CRITICAL"
        assert alerts[0].document_ids == (document.id,)
        assert alerts[0].evidence[0]["discount_basis"] == expected_basis
        assert alerts[0].evidence[0]["discount_amount"] == expected_discount
        assert alerts[0].evidence[0]["printed_rate_percent"] == "99"


def test_printed_discount_threshold_is_strict_and_unreconciled_rows_do_not_alert() -> None:
    def codes(
        rate: str,
        discount: str,
        subtotal: str,
        total: str,
        document_type: DocumentType = DocumentType.MANAGEMENT_DOCUMENT,
        subtotal_label: str = "SUBTOT",
    ) -> set[str]:
        document = _document(
            f"discount-boundary-{rate}-{subtotal}",
            document_type,
            total,
            (
                DocumentLine(
                    sequence=1,
                    description="Articolo sintetico",
                    line_total=Decimal("100.00"),
                ),
                DocumentLine(sequence=2, description=subtotal_label, line_total=Decimal(subtotal)),
                DocumentLine(
                    sequence=3,
                    description=f"Sconto {rate}%",
                    line_total=Decimal(discount),
                ),
                DocumentLine(sequence=4, description="TOT", line_total=Decimal(total)),
            ),
            minute=0,
            device="rch_synthetic",
            external_code=None,
        )
        transaction = CorrelationEngine().correlate((document,))[0]
        return {
            finding.rule_code
            for finding in FraudEngine().evaluate(
                FraudContext(transaction=transaction, evaluated_at=BASE_TIME + timedelta(hours=1))
            )
        }

    assert "EXTREME_PRINTED_DISCOUNT" not in codes("90", "-90.00", "100.00", "10.00")
    assert "EXTREME_PRINTED_DISCOUNT" in codes("99", "-99.00", "100.00", "1.00")
    assert "EXTREME_PRINTED_DISCOUNT" in codes(
        "99", "-99.00", "100.00", "1.00", DocumentType.COMMERCIAL_DOCUMENT
    )
    assert "EXTREME_PRINTED_DISCOUNT" in codes(
        "99", "-99.00", "100.00", "1.00", DocumentType.PRE_BILL, "SUB-TOTALE"
    )
    assert "EXTREME_PRINTED_DISCOUNT" not in codes("99", "-99.00", "101.00", "1.00")


def test_fiscal_employee_30_percent_parenthesized_label_is_not_extreme() -> None:
    fiscal = _document(
        "synthetic-epson-employee-discount",
        DocumentType.COMMERCIAL_DOCUMENT,
        "7.00",
        (
            DocumentLine(sequence=1, description="Prodotto sintetico", line_total=Decimal("10.00")),
            DocumentLine(
                sequence=2,
                description="Sconto dipendenti 30% (-30%)",
                line_total=Decimal("-3.00"),
            ),
            DocumentLine(sequence=3, description="TOTALE", line_total=Decimal("7.00")),
        ),
        minute=0,
        device="epson_synthetic",
        external_code=None,
    ).model_copy(update={"parser_name": "retailprintguard-epson-observed"})
    transaction = CorrelationEngine().correlate((fiscal,))[0]
    findings = FraudEngine().evaluate(
        FraudContext(transaction=transaction, evaluated_at=BASE_TIME + timedelta(hours=1))
    )
    assert "EXTREME_PRINTED_DISCOUNT" not in {item.rule_code for item in findings}


def test_scenario_a_100_to_50_creates_diff_and_explainable_high_alerts() -> None:
    prebill = _document(
        "a-prebill",
        DocumentType.PRE_BILL,
        "100.00",
        (_line(1, "A", "30.00"), _line(2, "B", "40.00"), _line(3, "C", "30.00")),
        minute=0,
        device="pos_1",
        external_code="PB-0001",
    )
    fiscal = _document(
        "a-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "50.00",
        (_line(1, "A", "1.00"), _line(2, "B", "49.00")),
        minute=2,
        device="rch_1",
        external_code="DC-0001",
        payments=(
            PaymentRecord(
                method="CONTANTI",
                amount=Decimal("50.00"),
                evidence=EvidenceLevel.CONFIRMED,
            ),
        ),
    )

    transactions = CorrelationEngine().correlate((fiscal, prebill))
    assert len(transactions) == 1
    transaction = transactions[0]
    assert transaction.correlation is not None
    assert transaction.correlation.score >= 60
    assert transaction.prebill_total == Decimal("100.00")
    assert transaction.fiscal_total == Decimal("50.00")
    assert transaction.observed_final_total == Decimal("50.00")
    assert transaction.difference_amount == Decimal("50.00")
    assert transaction.difference_percent == Decimal("50.0000")
    assert {change.change_type for change in transaction.line_changes} >= {
        LineChangeType.REMOVED,
        LineChangeType.PRICE_CHANGED,
    }

    order_id = _id("order-a")
    events = (
        OrderEvent(
            id=_id("remove-event"),
            order_id=order_id,
            type=OrderEventType.ITEM_REMOVED,
            occurred_at=BASE_TIME + timedelta(minutes=1, seconds=30),
            details={"item_code": "C"},
        ),
        OrderEvent(
            id=_id("price-event"),
            order_id=order_id,
            type=OrderEventType.PRICE_CHANGED,
            occurred_at=BASE_TIME + timedelta(minutes=1, seconds=40),
            details={"item_code": "A", "before": "30.00", "after": "1.00"},
        ),
    )
    findings = FraudEngine().evaluate(
        FraudContext(
            transaction=transaction,
            order_events=events,
            evaluated_at=BASE_TIME + timedelta(hours=1),
        )
    )
    by_code = {finding.rule_code: finding for finding in findings}
    assert set(by_code) == {"MODIFICA_POST_PRECONTO"}
    amount_alert = by_code["MODIFICA_POST_PRECONTO"]
    assert amount_alert.severity.value == "HIGH"
    assert amount_alert.score >= 80
    assert amount_alert.evidence[0]["difference_amount"] == "50.00"
    assert {item["change_type"] for item in amount_alert.evidence[1:]} >= {
        "REMOVED",
        "PRICE_CHANGED",
    }

    first = finding_chain_record(amount_alert, sequence=1, previous_hash=None)
    assert verify_chain([first])


def test_lab_cash_prebill_to_ten_cent_close_is_one_loss_alert() -> None:
    """Sanitized regression for a price reduction after a cash prebill."""

    prebill = _document(
        "table-25b-prebill",
        DocumentType.MANAGEMENT_DOCUMENT,
        "3.00",
        (_line(1, "COFFEE-MILK", "3.00"),),
        minute=0,
        device="pos_bar",
        external_code=None,
        table_code="LAB-25B",
    )
    fiscal = _document(
        "table-25b-commercial",
        DocumentType.COMMERCIAL_DOCUMENT,
        "0.10",
        (_line(1, "COFFEE-MILK", "0.10"),),
        minute=2,
        device="rch_1",
        external_code=None,
        external_code_suffix="0041",
        table_code="LAB-25B",
        payments=(
            PaymentRecord(
                method="CONTANTI",
                amount=Decimal("0.10"),
                evidence=EvidenceLevel.CONFIRMED,
            ),
        ),
    )
    management_copy = _document(
        "table-25b-management-copy",
        DocumentType.MANAGEMENT_DOCUMENT,
        "0.10",
        (_line(1, "COFFEE-MILK", "0.10"),),
        minute=3,
        device="rch_1",
        external_code=None,
        commercial_reference_code="LAB-FSC-0041",
        table_code="LAB-25B",
    )

    transactions = CorrelationEngine().correlate((management_copy, fiscal, prebill))
    assert len(transactions) == 1
    transaction = transactions[0]
    assert transaction.prebill_total == Decimal("3.00")
    assert transaction.fiscal_total == Decimal("0.10")
    assert transaction.observed_final_total == Decimal("0.10")
    assert transaction.difference_amount == Decimal("2.90")
    assert transaction.difference_percent == Decimal("96.6667")

    findings = FraudEngine().evaluate(
        FraudContext(
            transaction=transaction,
            evaluated_at=BASE_TIME + timedelta(minutes=4),
        )
    )
    assert len(findings) == 1
    alert = findings[0]
    assert alert.rule_code == "MODIFICA_POST_PRECONTO"
    assert alert.severity.value == "HIGH"
    assert alert.evidence[0]["prebill_total"] == "3.00"
    assert alert.evidence[0]["observed_final_total"] == "0.10"
    assert alert.evidence[0]["difference_amount"] == "2.90"
    assert alert.evidence[0]["difference_percent"] == "96.6667"
    assert set(alert.document_ids) == {prebill.id, fiscal.id, management_copy.id}
    assert {item["change_type"] for item in alert.evidence[1:]} == {"PRICE_CHANGED"}


def test_default_profile_ignores_immaterial_post_prebill_price_correction() -> None:
    prebill = _document(
        "minor-prebill",
        DocumentType.PRE_BILL,
        "3.00",
        (_line(1, "COFFEE-MILK", "3.00"),),
        minute=0,
        device="pos_bar",
        external_code="MGMT-MINOR-1",
        table_code="LAB-MINOR",
    )
    fiscal = _document(
        "minor-commercial",
        DocumentType.COMMERCIAL_DOCUMENT,
        "2.90",
        (_line(1, "COFFEE-MILK", "2.90"),),
        minute=1,
        device="rch_1",
        external_code="COMM-MINOR-1",
        table_code="LAB-MINOR",
        payments=(
            PaymentRecord(
                method="CONTANTI",
                amount=Decimal("2.90"),
                evidence=EvidenceLevel.CONFIRMED,
            ),
        ),
    )
    transaction = CorrelationEngine().correlate((prebill, fiscal))[0]

    # The configured materiality threshold is authoritative: legacy line and
    # same-reference symptom rules must not bypass it in the default profile.
    assert (
        FraudEngine().evaluate(
            FraudContext(transaction=transaction, evaluated_at=BASE_TIME + timedelta(minutes=2))
        )
        == ()
    )


def test_zero_technical_line_is_ignored_but_negative_sale_line_is_not() -> None:
    zero_technical = _document(
        "zero-technical-line",
        DocumentType.PRE_BILL,
        "3.00",
        (
            _line(1, "BEVERAGE", "3.00"),
            _line(2, "RESTO", "0.00").model_copy(update={"description": "RESTO"}),
        ),
        minute=0,
        device="pos_lab",
        external_code="LAB-MGMT-ZERO",
        table_code="LAB-ZERO",
    )
    negative_sale = _document(
        "negative-sale-line",
        DocumentType.PRE_BILL,
        "2.00",
        (_line(1, "BEVERAGE", "3.00"), _line(2, "MANUAL-ADJUSTMENT", "-1.00")),
        minute=0,
        device="pos_lab",
        external_code="LAB-MGMT-NEGATIVE",
        table_code="LAB-NEGATIVE",
    )
    zero_sale = _document(
        "zero-sale-line",
        DocumentType.PRE_BILL,
        "3.00",
        (_line(1, "BEVERAGE", "3.00"), _line(2, "PROMO-ITEM", "0.00")),
        minute=0,
        device="pos_lab",
        external_code="LAB-MGMT-ZERO-SALE",
        table_code="LAB-ZERO-SALE",
    )

    zero_codes = {
        finding.rule_code
        for finding in FraudEngine().evaluate(
            FraudContext(
                transaction=CorrelationEngine().correlate((zero_technical,))[0],
                evaluated_at=BASE_TIME + timedelta(minutes=1),
            )
        )
    }
    negative_codes = {
        finding.rule_code
        for finding in FraudEngine().evaluate(
            FraudContext(
                transaction=CorrelationEngine().correlate((negative_sale,))[0],
                evaluated_at=BASE_TIME + timedelta(minutes=1),
            )
        )
    }
    zero_sale_codes = {
        finding.rule_code
        for finding in FraudEngine().evaluate(
            FraudContext(
                transaction=CorrelationEngine().correlate((zero_sale,))[0],
                evaluated_at=BASE_TIME + timedelta(minutes=1),
            )
        )
    }
    assert "NEGATIVE_OR_ZERO_VALUE_ITEM" not in zero_codes
    assert "NEGATIVE_OR_ZERO_VALUE_ITEM" in negative_codes
    assert "NEGATIVE_OR_ZERO_VALUE_ITEM" in zero_sale_codes


def test_incomplete_commercial_attempt_is_not_an_economic_closure() -> None:
    prebill = _document(
        "incomplete-prebill",
        DocumentType.PRE_BILL,
        "100.00",
        (_line(1, "A", "100.00"),),
        minute=0,
        device="pos_1",
        external_code="PB-INCOMPLETE",
    )
    incomplete_fiscal = _document(
        "incomplete-commercial",
        DocumentType.COMMERCIAL_DOCUMENT,
        "50.00",
        (_line(1, "A", "50.00"),),
        minute=1,
        device="rch_1",
        external_code="DC-INCOMPLETE",
    ).model_copy(update={"complete": False, "status": "PARTIAL"})
    transaction = CorrelationEngine().correlate((prebill, incomplete_fiscal))[0]

    codes = {
        finding.rule_code
        for finding in FraudEngine().evaluate(
            FraudContext(transaction=transaction, evaluated_at=BASE_TIME + timedelta(minutes=2))
        )
    }
    assert not codes & {
        "MODIFICA_POST_PRECONTO",
        "PREBILL_FISCAL_AMOUNT_DROP",
        "ITEM_REMOVED_AFTER_PREBILL",
        "PRICE_REDUCED_AFTER_PREBILL",
        "EXTREME_PRICE_CHANGE",
        "SAME_REFERENCE_DIFFERENT_AMOUNT",
    }


def test_total_line_mismatch_accepts_observed_global_discount_and_adjustment() -> None:
    base = _document(
        "discounted-document",
        DocumentType.ORDER,
        "90.00",
        (_line(1, "A", "100.00"),),
        minute=0,
        device="pos_1",
        external_code="ORDER-DISCOUNTED",
    )
    discounted = base.model_copy(update={"discount_total": Decimal("10.00")})
    adjusted = base.model_copy(
        update={
            "id": _id("adjusted-document"),
            "source_job_id": "job-adjusted-document",
            "external_document_code": "ORDER-ADJUSTED",
            "discount_total": None,
            "raw_metadata": {"adjustment_total": "-10.00"},
        }
    )
    unknown_adjustment = base.model_copy(
        update={
            "id": _id("unknown-adjustment-document"),
            "source_job_id": "job-unknown-adjustment-document",
            "external_document_code": "ORDER-UNKNOWN-ADJUSTMENT",
            "discount_total": None,
            "raw_metadata": {"adjustments": [{"kind": "service"}]},
        }
    )

    for document in (discounted, adjusted, unknown_adjustment):
        transaction = CorrelationEngine().correlate((document,))[0]
        codes = {
            finding.rule_code
            for finding in FraudEngine().evaluate(
                FraudContext(
                    transaction=transaction,
                    evaluated_at=BASE_TIME + timedelta(minutes=1),
                )
            )
        }
        assert "TOTAL_LINE_MISMATCH" not in codes


def test_exact_printed_employee_discount_is_excluded_but_unrelated_alert_remains() -> None:
    prebill = _document(
        "employee-discount-prebill",
        DocumentType.PRE_BILL,
        "100.00",
        (_line(1, "MEAL", "100.00"),),
        minute=0,
        device="pos_1",
        external_code="PB-EMPLOYEE",
    )
    fiscal = _document(
        "employee-discount-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "70.00",
        (_line(1, "MEAL", "100.00"),),
        minute=1,
        device="rch_1",
        external_code="DC-EMPLOYEE",
        # Deliberately inconsistent payment: authorization must cover only the
        # employee discount, never unrelated antifraud conditions.
        payments=(PaymentRecord(method="CONTANTI", amount=Decimal("69.00")),),
    ).model_copy(
        update={
            "parser_name": "retailprintguard-rch-observed",
            "parse_confidence": 90,
            "evidence": EvidenceLevel.INFERRED,
            "normalized_text": (
                "Pasto 100,00\n"
                "SCONTO DIPENDENTI 30% [-30%] -30,00\n"
                "TOTALE 70,00"
            ),
            "raw_metadata": {
                "semantic_evidence": "INFERRED_FROM_CAPTURE",
                "source_start_offset": 120,
                "source_end_offset": 360,
            },
        }
    )
    transaction = CorrelationEngine().correlate((prebill, fiscal))[0]

    authorization = authorized_employee_discount(transaction)
    assert authorization is not None
    assert authorization.gross_basis == Decimal("100.00")
    assert authorization.discounted_basis == Decimal("100.00")
    assert authorization.discount_amount == Decimal("30.00")
    assert authorization.final_amount == Decimal("70.00")
    assert authorization.observations[0]["provenance"] == (
        "RCH_CAPTURED_PRINT_LITERAL"
    )
    assert authorization.observations[0]["label"] == (
        "SCONTO DIPENDENTI 30% [-30%]"
    )

    codes = {
        finding.rule_code
        for finding in FraudEngine().evaluate(
            FraudContext(
                transaction=transaction,
                evaluated_at=BASE_TIME + timedelta(hours=1),
            )
        )
    }
    assert "MODIFICA_POST_PRECONTO" not in codes
    assert "PREBILL_FISCAL_AMOUNT_DROP" not in codes
    assert "PAYMENT_TOTAL_MISMATCH" in codes


def test_exact_dgfe_line_modifier_proves_authorized_employee_discount() -> None:
    prebill = _document(
        "dgfe-employee-prebill",
        DocumentType.PRE_BILL,
        "10.00",
        (_line(1, "MEAL", "10.00"),),
        minute=0,
        device="pos_1",
        external_code="PB-DGFE-EMPLOYEE",
    )
    dgfe_line = _line(1, "MEAL", "10.00").model_copy(
        update={"discount": Decimal("3.00")}
    )
    fiscal = _document(
        "dgfe-employee-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "7.00",
        (dgfe_line,),
        minute=1,
        device="rch_1",
        external_code="0001-0007",
        payments=(PaymentRecord(method="PC", amount=Decimal("7.00")),),
    ).model_copy(
        update={
            "parser_name": "rch.xtools.dgfe.v1",
            "subtype": "RCH_DGFE_VENDITA",
            "raw_metadata": {
                "provenance": "RCH_DGFE_EXPORT",
                "source_export_kind": "DGFE_DETAIL",
                "xml_path": "synthetic/Vendita[1]",
                "fiscal_identity": {
                    "matricola_rt": "99XX0000001",
                    "numero_azzeramento": 1,
                    "numero_documento": 7,
                    "kind": "Vendita",
                },
                "subtotal_modifiers": [],
                "lines": [
                    {
                        "sequence": 1,
                        "description": "Articolo MEAL",
                        "line_total": "10.00",
                        "modifiers": [
                            {
                                "amount": "3.00",
                                "sign": "-",
                                "description": "Sconto",
                            }
                        ],
                    }
                ],
            },
        }
    )
    transaction = CorrelationEngine().correlate((prebill, fiscal))[0]

    authorization = authorized_employee_discount(transaction)
    assert authorization is not None
    assert authorization.discount_amount == Decimal("3.00")
    assert authorization.observations[0]["provenance"] == "RCH_DGFE_EXPORT"
    assert not FraudEngine().evaluate(
        FraudContext(
            transaction=transaction,
            evaluated_at=BASE_TIME + timedelta(hours=1),
        )
    )


def test_employee_discount_recognition_fails_closed_for_non_exact_or_price_change() -> None:
    prebill = _document(
        "employee-fail-prebill",
        DocumentType.PRE_BILL,
        "100.00",
        (_line(1, "MEAL", "100.00"),),
        minute=0,
        device="pos_1",
        external_code="PB-EMPLOYEE-FAIL",
    )
    exact = _document(
        "employee-fail-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "70.00",
        (_line(1, "MEAL", "100.00"),),
        minute=1,
        device="rch_1",
        external_code="DC-EMPLOYEE-FAIL",
        payments=(PaymentRecord(method="CONTANTI", amount=Decimal("70.00")),),
    ).model_copy(
        update={
            "parser_name": "retailprintguard-rch-observed",
            "parse_confidence": 90,
            "evidence": EvidenceLevel.INFERRED,
            "normalized_text": "Pasto 100,00\nSconto 30% -30,00\nTOTALE 70,00",
            "raw_metadata": {
                "semantic_evidence": "INFERRED_FROM_CAPTURE",
                "source_start_offset": 10,
                "source_end_offset": 90,
            },
        }
    )
    wrong_rate = exact.model_copy(
        update={
            "id": _id("employee-wrong-rate"),
            "source_job_id": "job-employee-wrong-rate",
            "normalized_text": "Pasto 100,00\nSconto 29% -30,00\nTOTALE 70,00",
        }
    )
    price_changed = exact.model_copy(
        update={
            "id": _id("employee-price-changed"),
            "source_job_id": "job-employee-price-changed",
            "lines": (
                _line(1, "MEAL", "70.00").model_copy(
                    update={"line_total": Decimal("100.00")}
                ),
            ),
        }
    )

    for fiscal in (wrong_rate, price_changed):
        transaction = CorrelationEngine().correlate((prebill, fiscal))[0]
        assert authorized_employee_discount(transaction) is None
        assert "MODIFICA_POST_PRECONTO" in {
            finding.rule_code
            for finding in FraudEngine().evaluate(
                FraudContext(
                    transaction=transaction,
                    evaluated_at=BASE_TIME + timedelta(hours=1),
                )
            )
        }


def _management_discount_line(
    sequence: int,
    description: str,
    amount: str,
) -> DocumentLine:
    value = Decimal(amount)
    printed = f"{description} {value:.2f}".replace(".", ",")
    return DocumentLine(
        sequence=sequence,
        description=description,
        quantity=Decimal("1"),
        unit_price=value,
        line_total=value,
        raw_text=printed,
        source=SourceSpan(
            direction="request",
            offset=sequence * 100,
            length=len(printed),
            frame_id=str(sequence),
        ),
    )


def _partial_employee_discount_documents(
    *,
    suffix_evidence: bool = True,
    fiscal_total: str = "153.10",
    copy_total: str = "153.10",
    first_discount: str = "-30.00",
    table_code: str | None = "T-7",
    discount_label: str = "SCONTO DIPENDENTI 30% [-30%]",
) -> tuple[NormalizedDocument, NormalizedDocument, NormalizedDocument]:
    base_lines = (
        DocumentLine(
            sequence=1,
            item_code="A",
            description="Articolo A",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            line_total=Decimal("100.00"),
        ),
        DocumentLine(
            sequence=2,
            item_code="B",
            description="Articolo B",
            quantity=Decimal("1"),
            unit_price=Decimal("93.00"),
            line_total=Decimal("93.00"),
        ),
        DocumentLine(
            sequence=3,
            item_code="C",
            description="Articolo C",
            quantity=Decimal("1"),
            unit_price=Decimal("18.00"),
            line_total=Decimal("18.00"),
        ),
    )
    prebill = _document(
        "partial-employee-prebill",
        DocumentType.PRE_BILL,
        "211.00",
        base_lines,
        minute=0,
        device="pos_1",
        external_code="PB-PARTIAL-EMPLOYEE",
    )
    fiscal = _document(
        "partial-employee-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        fiscal_total,
        base_lines,
        minute=1,
        device="rch_1",
        external_code=None,
        external_code_suffix="0002",
        payments=(
            PaymentRecord(method="CONTANTI", amount=Decimal(fiscal_total)),
        ),
    ).model_copy(
        update={
            "external_document_code": None,
            "table_code": table_code,
            "raw_metadata": {
                **(
                    {
                        "external_document_code_suffix_evidence": (
                            "RCH_STATUS_RESPONSE_SUFFIX_SEQUENCE_CONFIRMED"
                        )
                    }
                    if suffix_evidence
                    else {}
                )
            },
        }
    )
    copy_lines = (
        _management_discount_line(1, "Articolo A", "100.00"),
        _management_discount_line(
            2,
            discount_label,
            first_discount,
        ),
        _management_discount_line(3, "Articolo B", "93.00"),
        _management_discount_line(
            4,
            discount_label,
            "-27.90",
        ),
        _management_discount_line(5, "Articolo C", "18.00"),
        _management_discount_line(6, "TOT", copy_total),
    )
    management = _document(
        "partial-employee-management-copy",
        DocumentType.MANAGEMENT_DOCUMENT,
        copy_total,
        copy_lines,
        minute=2,
        device="rch_1",
        external_code="MGMT-0046",
        commercial_reference_code="LAB9-0002",
    ).model_copy(
        update={
            "parser_name": "retailprintguard-rch-observed",
            "table_code": table_code,
            "parse_confidence": 90,
            "evidence": EvidenceLevel.INFERRED,
            "normalized_text": "\n".join(line.raw_text or "" for line in copy_lines),
            "raw_metadata": {
                "semantic_evidence": "INFERRED_FROM_CAPTURE",
                "source_start_offset": 100,
                "source_end_offset": 700,
                "commercial_reference_code_evidence": (
                    "RCH_PRINTED_UNQUALIFIED_DOCUMENT_NUMBER"
                ),
            },
        }
    )
    return prebill, fiscal, management


def test_partial_item_employee_discount_is_proven_by_strong_management_copy() -> None:
    prebill, fiscal, management = _partial_employee_discount_documents()
    transaction = CorrelationEngine().correlate((management, fiscal, prebill))[0]

    authorization = authorized_employee_discount(transaction)
    assert authorization is not None
    assert authorization.gross_basis == Decimal("211.00")
    assert authorization.discounted_basis == Decimal("193.00")
    assert authorization.discount_amount == Decimal("57.90")
    assert authorization.final_amount == Decimal("153.10")
    assert management.id in authorization.document_ids
    assert fiscal.id in authorization.document_ids
    assert {item["paired_item_sequence"] for item in authorization.observations} == {
        1,
        3,
    }


def test_parenthesized_employee_30_literal_keeps_existing_authorization_checks() -> None:
    prebill, fiscal, management = _partial_employee_discount_documents(
        discount_label="SCONTO DIPENDENTI 30% (-30%)"
    )
    transaction = CorrelationEngine().correlate((management, fiscal, prebill))[0]
    authorization = authorized_employee_discount(transaction)
    assert authorization is not None
    assert authorization.discount_amount == Decimal("57.90")
    assert "EXTREME_PRINTED_DISCOUNT" not in {
        finding.rule_code
        for finding in FraudEngine().evaluate(
            FraudContext(transaction=transaction, evaluated_at=BASE_TIME + timedelta(hours=1))
        )
    }


def test_partial_employee_discount_accepts_live_identity_with_both_tables_absent() -> None:
    prebill, fiscal, management = _partial_employee_discount_documents(
        table_code=None
    )
    transaction = CorrelationEngine().correlate((management, fiscal, prebill))[0]

    authorization = authorized_employee_discount(transaction)
    assert authorization is not None
    assert authorization.discounted_basis == Decimal("193.00")
    assert authorization.discount_amount == Decimal("57.90")
    assert authorization.observations[0]["fiscal_identity_resolution"] == (
        "SEQUENCE_CONFIRMED_SUFFIX"
    )
    assert not {
        "MODIFICA_POST_PRECONTO",
        "NEGATIVE_OR_ZERO_VALUE_ITEM",
        "TOTAL_LINE_MISMATCH",
    } & {
        finding.rule_code
        for finding in FraudEngine().evaluate(
            FraudContext(
                transaction=transaction,
                evaluated_at=BASE_TIME + timedelta(hours=1),
            )
        )
    }


def test_management_employee_discount_fails_closed_without_pair_or_strong_identity() -> None:
    cases = (
        _partial_employee_discount_documents(first_discount="-29.99"),
        _partial_employee_discount_documents(suffix_evidence=False),
        _partial_employee_discount_documents(fiscal_total="152.00"),
    )
    for prebill, fiscal, management in cases:
        transaction = CorrelationEngine().correlate((management, fiscal, prebill))[0]
        assert authorized_employee_discount(transaction) is None
        assert "MODIFICA_POST_PRECONTO" in {
            finding.rule_code
            for finding in FraudEngine().evaluate(
                FraudContext(
                    transaction=transaction,
                    evaluated_at=BASE_TIME + timedelta(hours=1),
                )
            )
        }


def test_scenario_b_split_100_into_two_50_has_no_amount_drop_false_positive() -> None:
    prebill = _document(
        "b-prebill",
        DocumentType.PRE_BILL,
        "100.00",
        (_line(1, "A", "50.00"), _line(2, "B", "50.00")),
        minute=0,
        device="pos_1",
        external_code="PB-0002",
    )
    first = _document(
        "b-fiscal-one",
        DocumentType.COMMERCIAL_DOCUMENT,
        "50.00",
        (_line(1, "A", "50.00"),),
        minute=2,
        device="rch_1",
        external_code="DC-0002",
        payments=(PaymentRecord(method="CARTA", amount=Decimal("50.00")),),
    )
    second = _document(
        "b-fiscal-two",
        DocumentType.COMMERCIAL_DOCUMENT,
        "50.00",
        (_line(1, "B", "50.00"),),
        minute=3,
        device="rch_1",
        external_code="DC-0003",
        payments=(PaymentRecord(method="CONTANTI", amount=Decimal("50.00")),),
    )

    engine = CorrelationEngine()
    transaction = engine.correlate((second, prebill, first))[0]
    rerun = engine.correlate((prebill, first, second))[0]
    assert transaction.transaction_id == rerun.transaction_id
    assert transaction.correlation is not None
    assert len(transaction.documents) == 3
    assert transaction.split_payment is True
    assert transaction.fiscal_total == Decimal("100.00")
    assert transaction.observed_final_total == Decimal("100.00")
    assert transaction.payment_total == Decimal("100.00")
    assert transaction.difference_amount == Decimal("0.00")
    assert all(
        change.change_type is LineChangeType.UNCHANGED for change in transaction.line_changes
    )

    codes = {
        finding.rule_code
        for finding in FraudEngine().evaluate(
            FraudContext(transaction=transaction, evaluated_at=BASE_TIME + timedelta(hours=1))
        )
    }
    assert "PREBILL_FISCAL_AMOUNT_DROP" not in codes
    assert "SAME_REFERENCE_DIFFERENT_AMOUNT" not in codes
    assert "PAYMENT_TOTAL_MISMATCH" not in codes
    assert "ITEM_REMOVED_AFTER_PREBILL" not in codes


def test_synthetic_post_prebill_change_35_to_non_fiscal_close_5_is_quantified() -> None:
    prebill = _document(
        "post-prebill-prebill",
        DocumentType.PRE_BILL,
        "35.00",
        (
            _line(1, "COVER", "4.00"),
            _line(2, "DRINK-A", "8.00"),
            _line(3, "FOOD-A", "8.00"),
            _line(4, "FOOD-B", "7.00"),
            _line(5, "FOOD-C", "8.00"),
        ),
        minute=0,
        device="pos_synthetic",
        external_code="PB-LAB-1",
    )
    non_fiscal_close = _document(
        "post-prebill-room-close",
        DocumentType.MANAGEMENT_DOCUMENT,
        "5.00",
        (
            _line(1, "COVER", "4.00"),
            _line(2, "DRINK-A", "1.00"),
            _line(3, "FOOD-A", "0.00"),
            _line(4, "FOOD-B", "0.00"),
            _line(5, "FOOD-C", "0.00"),
        ),
        minute=20,
        device="rch_synthetic",
        external_code="MG-LAB-2",
    ).model_copy(
        update={
            "subtype": "CORRISPETTIVO_NON_RISCOSSO",
            "normalized_text": "Conto: Camera LAB\nCORRISPETTIVO NON RISCOSSO\nTOT 5,00",
            "raw_metadata": {
                "order_reference": "REFERENCE-80",
                "economic_close": True,
                "settlement_kind": "ROOM_CHARGE",
            },
        }
    )
    cancellation = _document(
        "post-prebill-cancelled-attempt",
        DocumentType.CANCELLATION,
        "5.00",
        (),
        minute=18,
        device="rch_synthetic",
        external_code="CN-LAB-1",
    )
    incomplete_fiscal_attempt = _document(
        "post-prebill-incomplete-fiscal-attempt",
        DocumentType.COMMERCIAL_DOCUMENT,
        "5.00",
        (_line(1, "COVER", "4.00"), _line(2, "DRINK-A", "1.00")),
        minute=17,
        device="rch_synthetic",
        external_code="TRY-LAB-1",
    ).model_copy(update={"complete": False, "status": "PARTIAL"})

    transaction = CorrelationEngine().correlate(
        (cancellation, incomplete_fiscal_attempt, non_fiscal_close, prebill)
    )[0]
    assert transaction.correlation is not None
    assert transaction.prebill_total == Decimal("35.00")
    assert transaction.fiscal_total == Decimal("0")
    assert transaction.observed_final_total == Decimal("5.00")
    assert transaction.difference_amount == Decimal("30.00")
    assert transaction.difference_percent == Decimal("85.7143")
    assert {change.change_type for change in transaction.line_changes} >= {
        LineChangeType.PRICE_CHANGED,
    }
    codes = {
        finding.rule_code
        for finding in FraudEngine().evaluate(
            FraudContext(transaction=transaction, evaluated_at=BASE_TIME + timedelta(hours=1))
        )
    }
    assert "MODIFICA_POST_PRECONTO" in codes
    assert "ORDER_WITHOUT_FISCAL_CLOSE" not in codes


def test_all_required_rules_are_versioned_and_registered() -> None:
    expected = {
        "PREBILL_FISCAL_AMOUNT_DROP",
        "MODIFICA_POST_PRECONTO",
        "ITEM_REMOVED_AFTER_PREBILL",
        "PRICE_REDUCED_AFTER_PREBILL",
        "EXTREME_PRICE_CHANGE",
        "EXTREME_PRINTED_DISCOUNT",
        "SAME_REFERENCE_DIFFERENT_AMOUNT",
        "ORDER_WITHOUT_FISCAL_CLOSE",
        "FISCAL_WITHOUT_SOURCE_ORDER",
        "EXCESSIVE_VOID_OR_CANCELLATION",
        "REPRINT_OR_COPY_ANOMALY",
        "DOCUMENT_SEQUENCE_GAP",
        "DUPLICATE_DOCUMENT",
        "LATE_ORDER_MODIFICATION",
        "NEGATIVE_OR_ZERO_VALUE_ITEM",
        "TOTAL_LINE_MISMATCH",
        "PAYMENT_TOTAL_MISMATCH",
        "UNUSUAL_OPERATOR_PATTERN",
    }
    assert {rule.code for rule in DEFAULT_RULES} == expected
    assert all(rule.version >= 1 and rule.enabled for rule in DEFAULT_RULES)


def test_whitelist_suppresses_only_the_matching_rule_and_keeps_evidence() -> None:
    prebill = _document(
        "w-prebill",
        DocumentType.PRE_BILL,
        "100.00",
        (_line(1, "A", "100.00"),),
        minute=0,
        device="pos_1",
        external_code="PB-0009",
    )
    fiscal = _document(
        "w-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "50.00",
        (_line(1, "A", "50.00"),),
        minute=2,
        device="rch_1",
        external_code="DC-0009",
        payments=(PaymentRecord(method="CONTANTI", amount=Decimal("50.00")),),
    )
    transaction = CorrelationEngine().correlate((prebill, fiscal))[0]
    entry = WhitelistEntry(
        rule_code="MODIFICA_POST_PRECONTO",
        scope=WhitelistScope.TRANSACTION,
        scope_value=str(transaction.transaction_id),
        reason="Conto separato verificato dall'auditor",
        valid_from=BASE_TIME,
        valid_until=BASE_TIME + timedelta(days=1),
    )
    evaluation = FraudEngine().evaluate_with_suppressions(
        FraudContext(
            transaction=transaction,
            whitelist_entries=(entry,),
            evaluated_at=BASE_TIME + timedelta(hours=1),
        )
    )
    assert not evaluation.findings
    assert len(evaluation.suppressed) == 1
    assert evaluation.suppressed[0].finding.rule_code == "MODIFICA_POST_PRECONTO"
    assert evaluation.suppressed[0].reason == entry.reason


def test_device_response_is_correlated_by_exact_duplex_job_without_business_codes() -> None:
    request = _document(
        "response-request",
        DocumentType.COMMERCIAL_DOCUMENT,
        "5.00",
        (_line(1, "Caffè", "5.00"),),
        minute=0,
        device="rch_1",
        external_code="DC-0005",
    )
    response = _document(
        "response-frame",
        DocumentType.DEVICE_RESPONSE,
        "0.00",
        (),
        minute=0,
        device="rch_1",
        external_code="RSP-0005",
    ).model_copy(
        update={
            "source_job_id": request.source_job_id,
            "source_session_id": request.source_session_id,
            "external_document_code": None,
            "order_code": None,
            "table_code": None,
            "operator_code": None,
            "raw_metadata": {"response_status": "OK"},
        }
    )

    transactions = CorrelationEngine().correlate((request, response))

    assert len(transactions) == 1
    assert {item.type for item in transactions[0].documents} == {
        DocumentType.COMMERCIAL_DOCUMENT,
        DocumentType.DEVICE_RESPONSE,
    }
    correlation = transactions[0].correlation
    assert correlation is not None
    assert "response_job_context" in correlation.matched_criteria


def test_persistent_rch_session_cannot_link_response_from_another_job() -> None:
    request = _document(
        "response-other-request",
        DocumentType.COMMERCIAL_DOCUMENT,
        "5.00",
        (_line(1, "COFFEE", "5.00"),),
        minute=0,
        device="rch_1",
        external_code="DC-0100",
    )
    response = _document(
        "response-other-job",
        DocumentType.DEVICE_RESPONSE,
        "0.00",
        (),
        minute=0,
        device="rch_1",
        external_code="RSP-0101",
    ).model_copy(
        update={
            "source_session_id": request.source_session_id,
            "external_document_code": None,
            "order_code": None,
            "table_code": None,
            "operator_code": None,
            "raw_metadata": {},
        }
    )

    transactions = CorrelationEngine().correlate((request, response))

    assert len(transactions) == 2
    assert all(item.correlation is None for item in transactions)


def test_conflicting_order_codes_are_a_hard_correlation_gate() -> None:
    prebill = _document(
        "conflict-prebill",
        DocumentType.PRE_BILL,
        "50.00",
        (_line(1, "A", "50.00"),),
        minute=0,
        device="pos_1",
        external_code="PB-CONFLICT",
    )
    fiscal = _document(
        "conflict-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "50.00",
        (_line(1, "A", "50.00"),),
        minute=1,
        device="rch_1",
        external_code="DC-CONFLICT",
    ).model_copy(update={"order_code": "OTHER-ORDER"})

    transactions = CorrelationEngine().correlate((prebill, fiscal))

    assert len(transactions) == 2
    score = CorrelationEngine().score_candidate_pair(prebill, fiscal)
    assert score.score == 0
    assert score.criteria[0].name == "identity_compatibility"


def test_conforming_copy_attaches_to_only_one_parent_and_cannot_bridge_sales() -> None:
    first = _document(
        "copy-first-sale",
        DocumentType.COMMERCIAL_DOCUMENT,
        "20.00",
        (_line(1, "A", "20.00"),),
        minute=0,
        device="rch_1",
        external_code="DC-COPY-1",
    ).model_copy(update={"order_code": "SALE-1"})
    second = _document(
        "copy-second-sale",
        DocumentType.COMMERCIAL_DOCUMENT,
        "20.00",
        (_line(1, "A", "20.00"),),
        minute=1,
        device="rch_1",
        external_code="DC-COPY-2",
    ).model_copy(update={"order_code": "SALE-2"})
    copy = _document(
        "copy-child",
        DocumentType.CONFORMING_COPY,
        "20.00",
        (_line(1, "A", "20.00"),),
        minute=2,
        device="rch_1",
        external_code="COPY-1",
    ).model_copy(update={"order_code": None})

    transactions = CorrelationEngine().correlate((first, second, copy))

    assert sorted(len(item.documents) for item in transactions) == [1, 2]
    assert sum(
        DocumentType.CONFORMING_COPY in {document.type for document in item.documents}
        for item in transactions
    ) == 1


def test_refund_is_a_post_close_adjustment_not_a_sale_reduction() -> None:
    prebill = _document(
        "refund-prebill",
        DocumentType.PRE_BILL,
        "100.00",
        (_line(1, "A", "100.00"),),
        minute=0,
        device="pos_1",
        external_code="PB-REFUND",
    )
    fiscal = _document(
        "refund-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "100.00",
        (_line(1, "A", "100.00"),),
        minute=1,
        device="rch_1",
        external_code="DC-REFUND",
    )
    refund = _document(
        "refund-adjustment",
        DocumentType.REFUND,
        "50.00",
        (_line(1, "A", "50.00"),),
        minute=5,
        device="rch_1",
        external_code="RF-REFUND",
    )

    transaction = CorrelationEngine().correlate((prebill, fiscal, refund))[0]

    assert len(transaction.documents) == 3
    assert transaction.fiscal_total == Decimal("100.00")
    assert transaction.observed_final_total == Decimal("100.00")
    assert transaction.difference_amount == Decimal("0.00")
    codes = {
        finding.rule_code
        for finding in FraudEngine().evaluate(
            FraudContext(transaction=transaction, evaluated_at=BASE_TIME + timedelta(hours=1))
        )
    }
    assert "MODIFICA_POST_PRECONTO" not in codes


def test_reused_order_code_after_close_starts_a_new_episode() -> None:
    prebill = _document(
        "reuse-prebill",
        DocumentType.PRE_BILL,
        "20.00",
        (_line(1, "A", "20.00"),),
        minute=0,
        device="pos_1",
        external_code="PB-REUSE",
    )
    fiscal = _document(
        "reuse-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "20.00",
        (_line(1, "A", "20.00"),),
        minute=1,
        device="rch_1",
        external_code="DC-REUSE",
    )
    next_sale = _document(
        "reuse-next-sale",
        DocumentType.KITCHEN_ORDER,
        "0.00",
        (_line(1, "B", "8.00"),),
        minute=5,
        device="pos_2",
        external_code="KO-REUSE",
    ).model_copy(update={"gross_total": None, "net_total": None})

    transactions = CorrelationEngine().correlate((prebill, fiscal, next_sale))

    assert sorted(len(item.documents) for item in transactions) == [1, 2]
    closed = next(
        item
        for item in transactions
        if DocumentType.COMMERCIAL_DOCUMENT in {document.type for document in item.documents}
    )
    assert {document.type for document in closed.documents} == {
        DocumentType.PRE_BILL,
        DocumentType.COMMERCIAL_DOCUMENT,
    }


def test_global_duplicate_finding_is_emitted_once_and_never_on_unrelated_transaction() -> None:
    duplicate_one = _document(
        "duplicate-one",
        DocumentType.COMMERCIAL_DOCUMENT,
        "10.00",
        (_line(1, "A", "10.00"),),
        minute=0,
        device="rch_1",
        external_code="DC-DUPLICATE",
    ).model_copy(update={"order_code": None, "table_code": None, "raw_metadata": {}})
    duplicate_two = _document(
        "duplicate-two",
        DocumentType.COMMERCIAL_DOCUMENT,
        "10.00",
        (_line(1, "A", "10.00"),),
        minute=1,
        device="rch_1",
        external_code="DC-DUPLICATE",
    ).model_copy(update={"order_code": None, "table_code": None, "raw_metadata": {}})
    unrelated = _document(
        "duplicate-unrelated",
        DocumentType.PRE_BILL,
        "7.00",
        (_line(1, "B", "7.00"),),
        minute=2,
        device="pos_1",
        external_code="PB-UNRELATED",
    ).model_copy(update={"order_code": None, "table_code": None, "raw_metadata": {}})
    engine = FraudEngine()
    transactions = {
        document.id: CorrelationEngine().correlate((document,))[0]
        for document in (duplicate_one, duplicate_two, unrelated)
    }

    duplicate_alert_count = 0
    for document in (duplicate_one, duplicate_two):
        findings = engine.evaluate(
            FraudContext(
                transaction=transactions[document.id],
                comparison_documents=(duplicate_one, duplicate_two, unrelated),
                evaluated_at=BASE_TIME + timedelta(hours=1),
            )
        )
        duplicate_alert_count += sum(
            finding.rule_code == "DUPLICATE_DOCUMENT" for finding in findings
        )
    unrelated_codes = {
        finding.rule_code
        for finding in engine.evaluate(
            FraudContext(
                transaction=transactions[unrelated.id],
                comparison_documents=(duplicate_one, duplicate_two, unrelated),
                evaluated_at=BASE_TIME + timedelta(hours=1),
            )
        )
    }
    assert duplicate_alert_count == 1
    assert "DUPLICATE_DOCUMENT" not in unrelated_codes

    responses = tuple(
        document.model_copy(update={"type": DocumentType.DEVICE_RESPONSE})
        for document in (duplicate_one, duplicate_two)
    )
    for response in responses:
        response_codes = {
            finding.rule_code
            for finding in engine.evaluate(
                FraudContext(
                    transaction=CorrelationEngine().correlate((response,))[0],
                    comparison_documents=responses,
                    evaluated_at=BASE_TIME + timedelta(hours=1),
                )
            )
        }
        assert "DUPLICATE_DOCUMENT" not in response_codes


def _pos_evidence(
    name: str,
    document_type: DocumentType,
    *,
    device: str,
    table: str,
    second: int,
    lines: tuple[DocumentLine, ...],
) -> NormalizedDocument:
    document = _document(
        name,
        document_type,
        "0.00",
        lines,
        minute=0,
        device=device,
        external_code=f"IGNORED-{name}",
    )
    return document.model_copy(
        update={
            "source_session_id": f"session-{name}",
            "external_document_code": None,
            "order_code": None,
            "table_code": table,
            "operator_code": None,
            "terminal_code": None,
            "document_timestamp": BASE_TIME + timedelta(seconds=second),
            "captured_at": BASE_TIME + timedelta(seconds=second, milliseconds=10),
            "gross_total": None,
            "net_total": None,
            "raw_metadata": {},
        }
    )


def test_cross_department_pos_dispatch_uses_narrow_explainable_table_window() -> None:
    bar = _pos_evidence(
        "dispatch-bar",
        DocumentType.KITCHEN_ORDER,
        device="pos_1",
        table="LAB-20",
        second=0,
        lines=(_line(1, "BAR", "4.00"),),
    )
    kitchen = _pos_evidence(
        "dispatch-kitchen",
        DocumentType.KITCHEN_ORDER,
        device="pos_2",
        table="lab-20",
        second=12,
        lines=(_line(1, "FOOD", "9.00"),),
    )
    pizzeria = _pos_evidence(
        "dispatch-pizzeria",
        DocumentType.KITCHEN_ORDER,
        device="pos_3",
        table="LAB-20",
        second=24,
        lines=(_line(1, "PIZZA", "8.00"),),
    )

    transactions = CorrelationEngine().correlate((pizzeria, bar, kitchen))

    assert len(transactions) == 1
    correlation = transactions[0].correlation
    assert correlation is not None
    assert correlation.algorithm_version == ALGORITHM_VERSION
    assert "CROSS_DEPARTMENT_DISPATCH" in correlation.matched_criteria
    assert "CROSS_DEPARTMENT_DISPATCH" in correlation.explanation

    reused_later = _pos_evidence(
        "dispatch-reused-table",
        DocumentType.KITCHEN_ORDER,
        device="pos_2",
        table="LAB-20",
        second=600,
        lines=(_line(1, "OTHER", "3.00"),),
    )
    separated = CorrelationEngine().correlate((bar, reused_later))
    assert len(separated) == 2
    assert all(transaction.correlation is None for transaction in separated)

    same_device = kitchen.model_copy(
        update={"id": _id("dispatch-same-device"), "source_device_id": "pos_1"}
    )
    not_cross_department = CorrelationEngine().correlate((bar, same_device))
    assert len(not_cross_department) == 2

    chained_later = _pos_evidence(
        "dispatch-transitive-chain",
        DocumentType.KITCHEN_ORDER,
        device="pos_1",
        table="LAB-20",
        second=50,
        lines=(_line(1, "DESSERT", "6.00"),),
    )
    bounded = CorrelationEngine().correlate((bar, kitchen, pizzeria, chained_later))
    assert sorted(len(item.documents) for item in bounded) == [1, 3]


def test_same_table_change_sequence_applies_signed_quantity_as_delta() -> None:
    initial_line = DocumentLine(
        sequence=1,
        item_code="SALAD",
        description="Pietanza mista",
        quantity=Decimal("2"),
        unit_price=Decimal("8.00"),
        line_total=Decimal("16.00"),
    )
    decrement = DocumentLine(
        sequence=1,
        item_code="SALAD",
        description="Pietanza mista",
        quantity=Decimal("-1"),
        unit_price=Decimal("8.00"),
        line_total=Decimal("-8.00"),
        raw_text="-1x Pietanza mista",
    )
    kitchen = _pos_evidence(
        "change-initial",
        DocumentType.KITCHEN_ORDER,
        device="pos_2",
        table="LAB-22",
        second=0,
        lines=(initial_line,),
    )
    change = _pos_evidence(
        "change-minus-one",
        DocumentType.ORDER_CHANGE,
        device="pos_2",
        table="LAB-22",
        second=20,
        lines=(decrement,),
    )

    transaction = CorrelationEngine().correlate((change, kitchen))[0]
    assert transaction.correlation is not None
    assert "SAME_TABLE_CHANGE_SEQUENCE" in transaction.correlation.matched_criteria

    effective = apply_order_change_lines(kitchen.lines, change.lines)
    assert len(effective) == 1
    assert effective[0].quantity == Decimal("1")
    changes = compare_document_lines(kitchen.lines, effective)
    assert len(changes) == 1
    assert changes[0].change_type is LineChangeType.QUANTITY_CHANGED
    assert changes[0].before_quantity == Decimal("2")
    assert changes[0].after_quantity == Decimal("1")
    assert all(item.change_type is not LineChangeType.REMOVED for item in changes)

    wrong_device = change.model_copy(
        update={"id": _id("change-wrong-device"), "source_device_id": "pos_1"}
    )
    late_change = change.model_copy(
        update={
            "id": _id("change-too-late"),
            "document_timestamp": BASE_TIME + timedelta(seconds=301),
            "captured_at": BASE_TIME + timedelta(seconds=301, milliseconds=10),
        }
    )
    unrelated_item_change = change.model_copy(
        update={
            "id": _id("change-unrelated-item"),
            "lines": (
                DocumentLine(
                    sequence=1,
                    item_code="PIZZA",
                    description="Pizza margherita",
                    quantity=Decimal("-1"),
                    raw_text="-1x Pizza margherita",
                ),
            ),
        }
    )
    for incompatible in (wrong_device, late_change, unrelated_item_change):
        unlinked = CorrelationEngine().correlate((kitchen, incompatible))
        assert len(unlinked) == 2
        assert all(item.correlation is None for item in unlinked)

    codes = {
        finding.rule_code
        for finding in FraudEngine().evaluate(
            FraudContext(
                transaction=transaction,
                evaluated_at=BASE_TIME + timedelta(hours=3),
            )
        )
    }
    assert "NEGATIVE_OR_ZERO_VALUE_ITEM" not in codes
    assert "TOTAL_LINE_MISMATCH" not in codes
    assert "ORDER_WITHOUT_FISCAL_CLOSE" not in codes


def test_cancellation_document_and_void_event_are_counted_once() -> None:
    prebill = _document(
        "void-prebill",
        DocumentType.PRE_BILL,
        "20.00",
        (_line(1, "A", "20.00"),),
        minute=0,
        device="pos_1",
        external_code="PB-VOID",
    )
    first = _document(
        "void-first",
        DocumentType.CANCELLATION,
        "0.00",
        (),
        minute=1,
        device="rch_1",
        external_code="VOID-1",
    )
    second = _document(
        "void-second",
        DocumentType.CANCELLATION,
        "0.00",
        (),
        minute=2,
        device="rch_1",
        external_code="VOID-2",
    )
    transaction = CorrelationEngine().correlate((prebill, first, second))[0]
    events = tuple(
        OrderEvent(
            id=_id(f"event-{document.id}"),
            order_id=_id("void-order"),
            type=OrderEventType.ORDER_VOIDED,
            occurred_at=document.document_timestamp,
            source_document_id=document.id,
        )
        for document in (first, second)
    )
    codes = {
        finding.rule_code
        for finding in FraudEngine().evaluate(
            FraudContext(
                transaction=transaction,
                order_events=events,
                evaluated_at=BASE_TIME + timedelta(hours=1),
            )
        )
    }
    assert "EXCESSIVE_VOID_OR_CANCELLATION" not in codes


def test_late_modification_requires_an_event_after_fiscal_close() -> None:
    prebill = _document(
        "late-prebill",
        DocumentType.PRE_BILL,
        "20.00",
        (_line(1, "A", "20.00"),),
        minute=0,
        device="pos_1",
        external_code="PB-LATE",
    )
    fiscal = _document(
        "late-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "20.00",
        (_line(1, "A", "20.00"),),
        minute=10,
        device="rch_1",
        external_code="DC-LATE",
    )
    transaction = CorrelationEngine().correlate((prebill, fiscal))[0]
    before = OrderEvent(
        id=_id("late-before"),
        order_id=_id("late-order"),
        type=OrderEventType.PRICE_CHANGED,
        occurred_at=BASE_TIME + timedelta(minutes=9),
        source_document_id=prebill.id,
    )
    after = before.model_copy(
        update={
            "id": _id("late-after"),
            "occurred_at": BASE_TIME + timedelta(minutes=11),
        }
    )

    before_codes = {
        item.rule_code
        for item in FraudEngine().evaluate(
            FraudContext(
                transaction=transaction,
                order_events=(before,),
                evaluated_at=BASE_TIME + timedelta(hours=1),
            )
        )
    }
    after_codes = {
        item.rule_code
        for item in FraudEngine().evaluate(
            FraudContext(
                transaction=transaction,
                order_events=(after,),
                evaluated_at=BASE_TIME + timedelta(hours=1),
            )
        )
    }
    assert "LATE_ORDER_MODIFICATION" not in before_codes
    assert "LATE_ORDER_MODIFICATION" in after_codes
