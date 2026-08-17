from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from retailprintguard.common.domain import (
    DocumentLine,
    DocumentType,
    EvidenceLevel,
    NormalizedDocument,
)
from retailprintguard.correlation import ALGORITHM_VERSION, CorrelationEngine
from retailprintguard.correlation.worker import LoadedDocument, _candidate_pairs

BASE_TIME = datetime(2042, 9, 18, 19, 0, tzinfo=UTC)


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"retailprintguard-sale-correlation:{name}")


def _line(item_code: str, price: str) -> DocumentLine:
    amount = Decimal(price)
    return DocumentLine(
        sequence=1,
        item_code=item_code,
        description=f"Articolo laboratorio {item_code}",
        quantity=Decimal("1"),
        unit_price=amount,
        line_total=amount,
    )


def _document(
    name: str,
    document_type: DocumentType,
    total: str,
    *,
    minute: int,
    second: int = 0,
    progressive: str | None,
    item_code: str = "LAB-BEV",
    table_code: str | None = "LAB-25",
    commercial_reference: str | None = None,
    fiscal_suffix: str | None = None,
) -> NormalizedDocument:
    return NormalizedDocument(
        id=_id(name),
        source_device_id=(
            "fiscal_lab" if document_type is DocumentType.COMMERCIAL_DOCUMENT else "management_lab"
        ),
        source_session_id=None,
        source_job_id=f"job-{name}",
        type=document_type,
        subtype=document_type.value,
        external_document_code=progressive,
        external_document_code_suffix=fiscal_suffix,
        commercial_reference_code=commercial_reference,
        order_code=None,
        table_code=table_code,
        operator_code=None,
        terminal_code=None,
        document_timestamp=BASE_TIME + timedelta(minutes=minute, seconds=second),
        captured_at=BASE_TIME + timedelta(minutes=minute, seconds=second + 1),
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
        lines=(_line(item_code, total),),
        raw_metadata=(
            {
                "external_document_code_suffix_evidence": (
                    "RCH_STATUS_RESPONSE_SUFFIX_SEQUENCE_CONFIRMED"
                )
            }
            if fiscal_suffix is not None
            else {}
        ),
    )


def _loaded(document: NormalizedDocument) -> LoadedDocument:
    return LoadedDocument(
        value=document,
        version_id=_id(f"version-{document.id}"),
        version_record_hash=hashlib.sha256(str(document.id).encode()).hexdigest(),
        database_device_id=_id(f"device-{document.id}"),
        database_job_id=_id(f"job-{document.id}"),
        raw_payload_id=None,
    )


def test_table_sale_and_commercial_reference_form_one_explainable_episode() -> None:
    kitchen_order = _document(
        "loss-kitchen-order",
        DocumentType.KITCHEN_ORDER,
        "0.00",
        minute=0,
        second=0,
        progressive=None,
    ).model_copy(update={"gross_total": None, "net_total": None})
    prebill = _document(
        "loss-prebill",
        DocumentType.MANAGEMENT_DOCUMENT,
        "3.00",
        minute=0,
        second=7,
        progressive=None,
    )
    fiscal = _document(
        "loss-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "0.10",
        minute=0,
        second=24,
        progressive=None,
        fiscal_suffix="0042",
    )
    management_copy = _document(
        "loss-management-copy",
        DocumentType.MANAGEMENT_DOCUMENT,
        "0.10",
        minute=0,
        second=26,
        progressive=None,
        commercial_reference="FSC-LAB-0042",
    )

    transactions = CorrelationEngine().correlate(
        (management_copy, fiscal, prebill, kitchen_order)
    )

    assert len(transactions) == 1
    transaction = transactions[0]
    assert {document.id for document in transaction.documents} == {
        prebill.id,
        fiscal.id,
        management_copy.id,
        kitchen_order.id,
    }
    assert transaction.correlation is not None
    assert transaction.correlation.algorithm_version == ALGORITHM_VERSION
    assert transaction.correlation.score >= 95
    assert {
        "table_sale_sequence",
        "line_identity_overlap",
        "commercial_reference_to_observed_fiscal_suffix",
        "kitchen_to_management_baseline",
    } <= set(transaction.correlation.matched_criteria)
    assert transaction.prebill_total == Decimal("3.00")
    assert transaction.fiscal_total == Decimal("0.10")
    assert transaction.difference_amount == Decimal("2.90")
    assert fiscal.external_document_code is None
    assert fiscal.external_document_code_suffix == "0042"
    assert management_copy.commercial_reference_code == "FSC-LAB-0042"


def test_commercial_reference_is_cross_field_and_does_not_need_table_or_order() -> None:
    fiscal = _document(
        "reference-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "0.10",
        minute=1,
        progressive="FSC-9902-0042",
        table_code=None,
    )
    management_copy = _document(
        "reference-copy",
        DocumentType.MANAGEMENT_DOCUMENT,
        "0.10",
        minute=2,
        progressive="MGT-9902-0101",
        table_code=None,
        commercial_reference="FSC-9902-0042",
    )

    score = CorrelationEngine().score_candidate_pair(fiscal, management_copy)
    transactions = CorrelationEngine().correlate((fiscal, management_copy))

    assert score.score == 95
    assert score.criteria[0].name == "commercial_reference_to_fiscal_progressive"
    assert len(transactions) == 1
    assert transactions[0].correlation is not None


def test_worker_candidate_block_matches_reference_to_fiscal_progressive() -> None:
    fiscal = _document(
        "candidate-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "0.10",
        minute=1,
        progressive="FSC-9903-0042",
        table_code=None,
    )
    management_copy = _document(
        "candidate-copy",
        DocumentType.MANAGEMENT_DOCUMENT,
        "0.10",
        minute=2,
        progressive="MGT-9903-0101",
        table_code=None,
        commercial_reference="FSC-9903-0042",
    )

    pairs = _candidate_pairs(
        (_loaded(fiscal), _loaded(management_copy)),
        {management_copy.id},
        7200,
    )

    assert {frozenset(pair) for pair in pairs} == {frozenset((fiscal.id, management_copy.id))}


def test_worker_candidate_block_can_discover_observed_fiscal_suffix() -> None:
    fiscal = _document(
        "candidate-suffix-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "0.10",
        minute=1,
        progressive=None,
        fiscal_suffix="0042",
        table_code=None,
    )
    management_copy = _document(
        "candidate-suffix-copy",
        DocumentType.MANAGEMENT_DOCUMENT,
        "0.10",
        minute=2,
        progressive=None,
        table_code=None,
        commercial_reference="FSC-LAB-0042",
    )

    pairs = _candidate_pairs(
        (_loaded(fiscal), _loaded(management_copy)),
        {management_copy.id},
        7200,
    )

    assert {frozenset(pair) for pair in pairs} == {frozenset((fiscal.id, management_copy.id))}
    assert len(CorrelationEngine().correlate((fiscal, management_copy))) == 2


def test_matching_numeric_suffix_alone_cannot_correlate_unrelated_documents() -> None:
    fiscal = _document(
        "suffix-only-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "0.10",
        minute=1,
        progressive=None,
        fiscal_suffix="0042",
        table_code="LAB-25",
        item_code="LAB-BEV",
    )
    unrelated_copy = _document(
        "suffix-only-copy",
        DocumentType.MANAGEMENT_DOCUMENT,
        "0.10",
        minute=2,
        progressive=None,
        commercial_reference="OTHER-LAB-0042",
        table_code="LAB-25",
        item_code="LAB-FOOD",
    )

    score = CorrelationEngine().score_candidate_pair(fiscal, unrelated_copy)

    assert score.score < 60
    assert len(CorrelationEngine().correlate((fiscal, unrelated_copy))) == 2


def test_kitchen_ticket_with_different_item_does_not_link_management_baseline() -> None:
    kitchen = _document(
        "different-kitchen-item",
        DocumentType.KITCHEN_ORDER,
        "0.00",
        minute=0,
        second=0,
        progressive=None,
        item_code="LAB-BEV",
    ).model_copy(update={"gross_total": None, "net_total": None})
    management = _document(
        "different-management-item",
        DocumentType.MANAGEMENT_DOCUMENT,
        "3.00",
        minute=0,
        second=7,
        progressive=None,
        item_code="LAB-FOOD",
    )

    assert len(CorrelationEngine().correlate((kitchen, management))) == 2


def test_kitchen_ticket_over_thirty_seconds_before_baseline_does_not_link() -> None:
    kitchen = _document(
        "late-kitchen-ticket",
        DocumentType.KITCHEN_ORDER,
        "0.00",
        minute=0,
        second=0,
        progressive=None,
    ).model_copy(update={"gross_total": None, "net_total": None})
    management = _document(
        "late-management-baseline",
        DocumentType.MANAGEMENT_DOCUMENT,
        "3.00",
        minute=0,
        second=31,
        progressive=None,
    )

    assert len(CorrelationEngine().correlate((kitchen, management))) == 2


def test_reused_table_cannot_chain_kitchen_baselines_beyond_episode_width() -> None:
    first_kitchen = _document(
        "opening-chain-kitchen-one",
        DocumentType.KITCHEN_ORDER,
        "0.00",
        minute=0,
        second=0,
        progressive=None,
    ).model_copy(update={"gross_total": None, "net_total": None})
    first_management = _document(
        "opening-chain-management-one",
        DocumentType.MANAGEMENT_DOCUMENT,
        "3.00",
        minute=0,
        second=29,
        progressive=None,
    )
    second_kitchen = _document(
        "opening-chain-kitchen-two",
        DocumentType.KITCHEN_ORDER,
        "0.00",
        minute=0,
        second=28,
        progressive=None,
    ).model_copy(update={"gross_total": None, "net_total": None})
    second_management = _document(
        "opening-chain-management-two",
        DocumentType.MANAGEMENT_DOCUMENT,
        "3.00",
        minute=0,
        second=57,
        progressive=None,
    )

    transactions = CorrelationEngine().correlate(
        (first_kitchen, first_management, second_kitchen, second_management)
    )

    assert len(transactions) >= 2
    assert all(
        (
            max(entry.occurred_at for entry in transaction.timeline)
            - min(entry.occurred_at for entry in transaction.timeline)
        ).total_seconds()
        <= 30
        for transaction in transactions
    )
    assert all(
        not {
            first_kitchen.id,
            second_management.id,
        }.issubset({document.id for document in transaction.documents})
        for transaction in transactions
    )


def test_same_table_with_different_item_does_not_correlate() -> None:
    prebill = _document(
        "different-item-prebill",
        DocumentType.PRE_BILL,
        "3.00",
        minute=0,
        progressive="MGT-9904-0100",
        item_code="LAB-BEV",
    )
    fiscal = _document(
        "different-item-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "4.00",
        minute=2,
        progressive="FSC-9904-0042",
        item_code="LAB-FOOD",
    )

    assert len(CorrelationEngine().correlate((prebill, fiscal))) == 2


def test_same_table_and_item_outside_narrow_sale_window_do_not_correlate() -> None:
    prebill = _document(
        "late-prebill",
        DocumentType.PRE_BILL,
        "3.00",
        minute=0,
        progressive="MGT-9905-0100",
    )
    later_fiscal = _document(
        "late-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "0.10",
        minute=10,
        progressive="FSC-9905-0042",
    )

    assert len(CorrelationEngine().correlate((prebill, later_fiscal))) == 2


def test_reused_table_creates_two_episodes_without_single_link_bridge() -> None:
    first_prebill = _document(
        "first-prebill",
        DocumentType.PRE_BILL,
        "3.00",
        minute=0,
        progressive="MGT-9906-0100",
    )
    first_fiscal = _document(
        "first-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "0.10",
        minute=2,
        progressive="FSC-9906-0042",
    )
    second_prebill = _document(
        "second-prebill",
        DocumentType.PRE_BILL,
        "3.00",
        minute=10,
        progressive="MGT-9906-0101",
    )
    second_fiscal = _document(
        "second-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "3.00",
        minute=12,
        progressive="FSC-9906-0043",
    )

    transactions = CorrelationEngine().correlate(
        (first_prebill, first_fiscal, second_prebill, second_fiscal)
    )

    assert len(transactions) == 2
    assert {frozenset(document.id for document in item.documents) for item in transactions} == {
        frozenset((first_prebill.id, first_fiscal.id)),
        frozenset((second_prebill.id, second_fiscal.id)),
    }


def test_equal_own_progressive_across_namespaces_is_not_a_match() -> None:
    prebill = _document(
        "namespace-prebill",
        DocumentType.PRE_BILL,
        "3.00",
        minute=0,
        progressive="SAME-TEXT-0042",
        table_code=None,
    )
    fiscal = _document(
        "namespace-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "3.00",
        minute=1,
        progressive="SAME-TEXT-0042",
        table_code=None,
    )

    score = CorrelationEngine().score_candidate_pair(prebill, fiscal)
    progressive = next(
        criterion
        for criterion in score.criteria
        if criterion.name == "own_progressive_same_namespace"
    )

    assert progressive.matched is False
    assert len(CorrelationEngine().correlate((prebill, fiscal))) == 2


def test_economic_management_close_is_never_used_as_prebill_fallback() -> None:
    management_close = _document(
        "economic-management-close",
        DocumentType.MANAGEMENT_DOCUMENT,
        "3.00",
        minute=0,
        progressive="MGT-9907-0100",
    ).model_copy(update={"raw_metadata": {"economic_close": True}})
    fiscal = _document(
        "economic-close-fiscal",
        DocumentType.COMMERCIAL_DOCUMENT,
        "0.10",
        minute=2,
        progressive="FSC-9907-0042",
    )

    transactions = CorrelationEngine().correlate((management_close, fiscal))

    assert len(transactions) == 2
    assert all(transaction.prebill_total is None for transaction in transactions)
