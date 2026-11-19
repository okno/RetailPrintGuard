from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from retailprintguard.common.domain import DocumentType, EvidenceLevel
from retailprintguard.parser.opaque import parse_opaque


@pytest.mark.parametrize(
    "payload",
    [
        b"\x02=K\x03\nTOTALE 10,00\n\x1dV\x00",
        b'<printerFiscalReceipt><printRecTotal payment="10.00"/></printerFiscalReceipt>',
        bytes(range(256)) * 512,
    ],
)
def test_opaque_stream_never_infers_fiscal_semantics(payload: bytes) -> None:
    arguments = dict(
        device_id="epson_synthetic",
        session_id="session-synthetic",
        job_id="job-synthetic",
        captured_at=datetime(2042, 1, 1, tzinfo=UTC),
        manifest_sha256="a" * 64,
        source_path="/synthetic/client.raw",
        response_source_path="/synthetic/device.raw",
    )
    records = parse_opaque(payload, payload, **arguments)
    assert len(records) == 2
    assert records == parse_opaque(payload, payload, **arguments)
    assert records[0].id != records[1].id
    for direction, record in zip(("CLIENT_TO_DEVICE", "DEVICE_TO_CLIENT"), records, strict=True):
        assert record.type is DocumentType.UNKNOWN
        assert record.evidence is EvidenceLevel.UNKNOWN
        assert record.gross_total is None
        assert record.external_document_code is None
        assert record.lines == record.payments == ()
        assert record.normalized_text == ""
        assert record.parse_confidence == 0
        assert record.complete is False
        assert record.source_payload_sha256 == hashlib.sha256(payload).hexdigest()
        assert record.raw_metadata["direction"] == direction
        assert record.raw_metadata["raw_offset"] == 0
        assert record.raw_metadata["raw_length"] == len(payload)
    assert records[0].source_path.endswith("client.raw")
    assert records[1].source_path.endswith("device.raw")


def test_empty_opaque_capture_has_no_invented_document() -> None:
    assert parse_opaque(
        b"",
        device_id="epson_synthetic",
        session_id=None,
        job_id="empty-synthetic",
        captured_at=datetime(2042, 1, 1, tzinfo=UTC),
        manifest_sha256="a" * 64,
        source_path="/synthetic/client.raw",
    ) == ()
