from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from retailprintguard.common.domain import (
    CorrelationResult,
    DocumentLine,
    DocumentType,
    EvidenceLevel,
    NormalizedDocument,
)
from retailprintguard.common.hashchain import ZERO_HASH, chained_hash, verify_chain


def _digest(char: str) -> str:
    return char * 64


def test_normalized_document_preserves_decimal_money_and_source_evidence() -> None:
    document = NormalizedDocument(
        source_device_id="pos_1",
        source_job_id="synthetic-job",
        type=DocumentType.PRE_BILL,
        subtype="PRECONTO",
        captured_at=datetime.now(UTC),
        gross_total=Decimal("100.00"),
        status="COMPLETE",
        normalized_text="PRECONTO SINTETICO",
        parser_name="synthetic",
        parser_version="1",
        parse_confidence=100,
        evidence=EvidenceLevel.CONFIRMED,
        source_manifest_sha256=_digest("a"),
        source_payload_sha256=_digest("b"),
        source_path="synthetic/job",
        complete=True,
        lines=(
            DocumentLine(
                sequence=1,
                description="ARTICOLO SINTETICO",
                quantity=Decimal("2"),
                unit_price=Decimal("50.00"),
                line_total=Decimal("100.00"),
            ),
        ),
    )

    assert document.gross_total == Decimal("100.00")
    assert document.lines[0].line_total == Decimal("100.00")


def test_document_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        NormalizedDocument(
            source_device_id="pos_1",
            source_job_id="job",
            type=DocumentType.UNKNOWN,
            subtype="UNKNOWN",
            captured_at=datetime(2042, 1, 1),
            status="PARTIAL",
            normalized_text="",
            parser_name="none",
            parser_version="1",
            parse_confidence=0,
            evidence=EvidenceLevel.UNKNOWN,
            source_manifest_sha256=_digest("a"),
            source_payload_sha256=_digest("b"),
            source_path="job",
            complete=False,
        )


def test_correlation_requires_distinct_documents() -> None:
    document_id = uuid4()
    with pytest.raises(ValidationError, match="two distinct"):
        CorrelationResult(
            document_ids=(document_id, document_id),
            score=100,
            algorithm_version="1",
            matched_criteria=("same_order",),
            unmatched_criteria=(),
            explanation="synthetic",
        )


def test_hash_chain_detects_mutation() -> None:
    first_payload = {"event": "CREATED", "previous_hash": ZERO_HASH}
    first = {**first_payload, "record_hash": chained_hash(first_payload, None)}
    second_payload = {"event": "UPDATED", "previous_hash": first["record_hash"]}
    second = {**second_payload, "record_hash": chained_hash(second_payload, first["record_hash"])}

    assert verify_chain([first, second])
    assert not verify_chain([first, {**second, "event": "TAMPERED"}])
