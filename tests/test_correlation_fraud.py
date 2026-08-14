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
)
from retailprintguard.common.hashchain import verify_chain
from retailprintguard.correlation import CorrelationEngine, LineChangeType
from retailprintguard.fraud import (
    DEFAULT_RULES,
    FraudContext,
    FraudEngine,
    WhitelistEntry,
    WhitelistScope,
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
    external_code: str,
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
        order_code="ORD-80",
        table_code="25-B",
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
        raw_metadata={"order_reference": "REFERENCE-80"},
    )


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
    expected = {
        "PREBILL_FISCAL_AMOUNT_DROP",
        "ITEM_REMOVED_AFTER_PREBILL",
        "PRICE_REDUCED_AFTER_PREBILL",
        "EXTREME_PRICE_CHANGE",
        "SAME_REFERENCE_DIFFERENT_AMOUNT",
        "LATE_ORDER_MODIFICATION",
    }
    assert expected <= set(by_code)
    amount_alert = by_code["PREBILL_FISCAL_AMOUNT_DROP"]
    assert amount_alert.severity.value == "HIGH"
    assert amount_alert.score >= 80
    assert amount_alert.evidence[0]["difference_amount"] == "50.00"

    first = finding_chain_record(amount_alert, sequence=1, previous_hash=None)
    second = finding_chain_record(
        by_code["ITEM_REMOVED_AFTER_PREBILL"],
        sequence=2,
        previous_hash=first["record_hash"],
    )
    assert verify_chain([first, second])


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


def test_all_sixteen_required_rules_are_versioned_and_registered() -> None:
    expected = {
        "PREBILL_FISCAL_AMOUNT_DROP",
        "MODIFICA_POST_PRECONTO",
        "ITEM_REMOVED_AFTER_PREBILL",
        "PRICE_REDUCED_AFTER_PREBILL",
        "EXTREME_PRICE_CHANGE",
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
        rule_code="PREBILL_FISCAL_AMOUNT_DROP",
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
    assert "PREBILL_FISCAL_AMOUNT_DROP" not in {
        finding.rule_code for finding in evaluation.findings
    }
    assert len(evaluation.suppressed) == 1
    assert evaluation.suppressed[0].finding.rule_code == "PREBILL_FISCAL_AMOUNT_DROP"
    assert evaluation.suppressed[0].reason == entry.reason
    assert "PRICE_REDUCED_AFTER_PREBILL" in {finding.rule_code for finding in evaluation.findings}


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
