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
    PaymentRecord,
)
from retailprintguard.correlation import CorrelationEngine
from retailprintguard.correlation.worker import LoadedDocument, _candidate_pairs

BASE_TIME = datetime(2044, 3, 9, 11, 30, tzinfo=UTC)


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"retailprintguard-dgfe-correlation:{name}")


def _line(code: str, amount: str) -> DocumentLine:
    value = Decimal(amount)
    return DocumentLine(
        sequence=1,
        item_code=code,
        description=f"Articolo sintetico {code}",
        quantity=Decimal("1"),
        unit_price=value,
        line_total=value,
    )


def _document(
    name: str,
    total: str,
    *,
    minute: int,
    progressive: str | None,
    suffix: str | None = None,
    dgfe: bool = False,
    device: str = "rch_lab",
    item: str = "LAB-A",
    payment: str | None = None,
) -> NormalizedDocument:
    amount = Decimal(total)
    raw_metadata: dict[str, object] = {}
    if dgfe:
        raw_metadata = {
            "provenance": "RCH_DGFE_EXPORT",
            "fiscal_identity": {
                "matricola_rt": "RTLAB0000001",
                "numero_azzeramento": 41,
                "numero_documento": 7,
                "kind": "VENDITA",
            },
        }
    elif suffix is not None:
        raw_metadata = {
            "external_document_code_suffix_evidence": (
                "RCH_STATUS_RESPONSE_SUFFIX_SEQUENCE_CONFIRMED"
            )
        }
    payments = (
        ()
        if payment is None
        else (
            PaymentRecord(
                method="CASH",
                amount=Decimal(payment),
                currency="EUR",
                evidence=EvidenceLevel.CONFIRMED,
            ),
        )
    )
    return NormalizedDocument(
        id=_id(name),
        source_device_id=device,
        source_session_id=f"session-{name}",
        source_job_id=f"job-{name}",
        type=DocumentType.COMMERCIAL_DOCUMENT,
        subtype="RCH_DGFE_VENDITA" if dgfe else "RCH_COMMERCIAL",
        external_document_code=progressive,
        external_document_code_suffix=suffix,
        order_code="ORDER-LAB-1",
        document_timestamp=BASE_TIME + timedelta(minutes=minute),
        captured_at=BASE_TIME + timedelta(days=10 if dgfe else 0, minutes=minute),
        gross_total=amount,
        net_total=amount,
        status="COMPLETE",
        normalized_text=f"Documento fiscale sintetico {name}",
        parser_name="rch.xtools.dgfe.v1" if dgfe else "retailprintguard-rch-observed",
        parser_version="1",
        parse_confidence=100,
        evidence=EvidenceLevel.CONFIRMED,
        source_manifest_sha256=hashlib.sha256(f"manifest:{name}".encode()).hexdigest(),
        source_payload_sha256=hashlib.sha256(f"payload:{name}".encode()).hexdigest(),
        source_path=f"synthetic/{name}",
        complete=True,
        lines=(_line(item, total),),
        payments=payments,
        rch_serial_number="RTLAB0000001" if dgfe else None,
        raw_metadata=raw_metadata,
    )


def _loaded(document: NormalizedDocument) -> LoadedDocument:
    return LoadedDocument(
        value=document,
        version_id=_id(f"version-{document.id}"),
        version_record_hash=hashlib.sha256(str(document.id).encode()).hexdigest(),
        database_device_id=_id(f"device-{document.source_device_id}"),
        database_job_id=_id(f"job-{document.id}"),
        raw_payload_id=None,
    )


def test_stream_and_dgfe_same_identity_count_economics_once() -> None:
    stream = _document(
        "same-stream",
        "12.00",
        minute=0,
        progressive="0041-0007",
        payment="12.00",
    )
    dgfe = _document(
        "same-dgfe",
        "12.00",
        minute=1,
        progressive="0041-0007",
        payment="12.00",
        dgfe=True,
    )

    transactions = CorrelationEngine().correlate((stream, dgfe))

    assert len(transactions) == 1
    transaction = transactions[0]
    assert {document.id for document in transaction.documents} == {stream.id, dgfe.id}
    assert len(transaction.timeline) == 2
    assert transaction.fiscal_total == Decimal("12.00")
    assert transaction.payment_total == Decimal("12.00")
    assert transaction.split_payment is False
    assert transaction.fiscal_total_document_ids == (dgfe.id,)
    assert transaction.fiscal_payment_document_ids == (dgfe.id,)
    assert transaction.fiscal_line_document_ids == (dgfe.id,)
    assert transaction.correlation is not None
    assert "dgfe_fiscal_identity" in transaction.correlation.matched_criteria
    assert "dgfe_evidence_consistent" in transaction.correlation.matched_criteria


def test_dgfe_mismatch_stays_linked_and_is_explicit() -> None:
    stream = _document(
        "mismatch-stream",
        "10.00",
        minute=0,
        progressive="0041-0007",
        payment="10.00",
        item="LAB-STREAM",
    )
    dgfe = _document(
        "mismatch-dgfe",
        "9.00",
        minute=1,
        progressive="0041-0007",
        payment="9.00",
        item="LAB-DGFE",
        dgfe=True,
    )

    transaction = CorrelationEngine().correlate((stream, dgfe))[0]

    assert transaction.correlation is not None
    assert transaction.fiscal_total == Decimal("9.00")
    assert transaction.payment_total == Decimal("9.00")
    assert "dgfe_evidence_mismatch" in transaction.correlation.matched_criteria
    assert {
        "dgfe_total_consistency",
        "dgfe_payment_consistency",
        "dgfe_line_consistency",
    } <= set(transaction.correlation.unmatched_criteria)
    assert transaction.fiscal_evidence_mismatches == (
        "mismatch DGFE/stream rilevato su: totale, pagamenti, righe",
    )


def test_dgfe_full_identity_links_sequence_confirmed_stream_suffix() -> None:
    stream = _document(
        "suffix-stream",
        "4.50",
        minute=0,
        progressive=None,
        suffix="0007",
    )
    dgfe = _document(
        "suffix-dgfe",
        "4.50",
        minute=2,
        progressive="0041-0007",
        dgfe=True,
    )
    loaded = (_loaded(stream), _loaded(dgfe))

    pairs = _candidate_pairs(loaded, {dgfe.id}, window_seconds=7200)
    transaction = CorrelationEngine().correlate_candidates(
        (stream, dgfe), pairs
    )[0]

    assert pairs == {(stream.id, dgfe.id)} or pairs == {(dgfe.id, stream.id)}
    assert transaction.correlation is not None
    assert "dgfe_fiscal_identity" in transaction.correlation.matched_criteria
    assert transaction.fiscal_total == Decimal("4.50")


def test_same_suffix_does_not_link_wrong_device_or_distant_sale() -> None:
    stream = _document(
        "unrelated-stream",
        "4.50",
        minute=0,
        progressive=None,
        suffix="0007",
    )
    wrong_device = _document(
        "wrong-device-dgfe",
        "4.50",
        minute=1,
        progressive="0041-0007",
        dgfe=True,
        device="rch_other",
    ).model_copy(update={"order_code": "ORDER-LAB-OTHER"})
    late = _document(
        "late-dgfe",
        "4.50",
        minute=6,
        progressive="0041-0007",
        dgfe=True,
    ).model_copy(update={"order_code": "ORDER-LAB-LATE"})

    assert len(CorrelationEngine().correlate((stream, wrong_device))) == 2
    assert len(CorrelationEngine().correlate((stream, late))) == 2


def test_real_split_fiscal_document_is_still_added_once_per_identity() -> None:
    stream = _document(
        "split-stream",
        "12.00",
        minute=0,
        progressive="0041-0007",
    )
    dgfe = _document(
        "split-dgfe",
        "12.00",
        minute=1,
        progressive="0041-0007",
        dgfe=True,
    )
    second_fiscal = _document(
        "split-second",
        "3.00",
        minute=2,
        progressive="0041-0008",
        item="LAB-B",
    )

    transaction = CorrelationEngine().correlate((stream, dgfe, second_fiscal))[0]

    assert transaction.fiscal_total == Decimal("15.00")
    assert transaction.split_payment is True
    assert set(transaction.fiscal_total_document_ids) == {dgfe.id, second_fiscal.id}
