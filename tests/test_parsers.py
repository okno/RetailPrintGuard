from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from retailprintguard.common.domain import DocumentType
from retailprintguard.parser.escpos import parse_escpos
from retailprintguard.parser.rch import frame_stream, parse_rch

CAPTURED_AT = datetime(2042, 6, 7, 12, 0, tzinfo=UTC)
MANIFEST_HASH = hashlib.sha256(b"synthetic-manifest").hexdigest()


def _rch_frame(
    data: str,
    *,
    address: str = "00",
    frame_class: str = "z",
    sequence: str = "0",
) -> bytes:
    encoded = data.encode("latin-1")
    prefix = (
        b"\x02"
        + address.encode("ascii")
        + f"{len(encoded):03d}".encode("ascii")
        + frame_class.encode("ascii")
        + encoded
        + sequence.encode("ascii")
    )
    checksum = 0
    for value in prefix:
        checksum ^= value
    return prefix + f"{checksum:02X}".encode("ascii") + b"\x03"


def test_escpos_multiple_documents_and_controls_are_bounded_and_visible() -> None:
    first = (
        b"\x1b@PRECONTO N. PB-77\nTavolo: T-9\nOrdine: O-77\n"
        b"Piatto sintetico 100,00\nTOTALE 100,00\n\x1dV\x00"
    )
    second = b"\x1b@COMANDA N. C-78\nTavolo: T-9\nPiatto sintetico 50,00\nTOTALE 50,00\n\x1dV\x00"

    documents = parse_escpos(
        first + second,
        device_id="pos_1",
        session_id="session-synthetic",
        job_id="job-synthetic",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )

    assert [document.type for document in documents] == [
        DocumentType.PRE_BILL,
        DocumentType.KITCHEN_ORDER,
    ]
    assert all(document.complete for document in documents)
    assert documents[0].gross_total is not None
    assert documents[0].gross_total == 100
    assert documents[0].table_code == "T-9"
    assert "<ESC/POS:INIT>" in documents[0].normalized_text
    assert "<ESC/POS:CUT>" in documents[0].normalized_text
    assert documents[0].lines[0].source is not None


def test_rch_reconstructs_commercial_and_device_response_from_coalesced_stream() -> None:
    requests = b"".join(
        (
            _rch_frame("=K", sequence="0"),
            _rch_frame("=R7/$5000/*1/(VOCE SINTETICA)", sequence="1"),
            _rch_frame('="/?A/(Tavolo: T-7)', sequence="2"),
            _rch_frame('="/?A/(Ordine: O-7)', sequence="3"),
            _rch_frame("=T1/$5000", sequence="4"),
            _rch_frame("<</?s", sequence="5"),
            _rch_frame("<</?7", sequence="6"),
        )
    )
    responses = b"\x06" + _rch_frame(
        "s000000RE7001",
        address="01",
        frame_class="N",
        sequence="3",
    )

    documents = parse_rch(
        requests,
        responses,
        device_id="rch_1",
        session_id="session-rch",
        job_id="job-rch",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )

    commercial = documents[0]
    assert commercial.type is DocumentType.COMMERCIAL_DOCUMENT
    assert commercial.complete is True
    assert commercial.gross_total == 50
    assert commercial.table_code == "T-7"
    assert commercial.order_code == "O-7"
    assert commercial.lines[0].description == "VOCE SINTETICA"
    assert commercial.raw_metadata["response_counter_suffix"] == "7001"
    assert documents[1].type is DocumentType.DEVICE_RESPONSE
    assert documents[1].raw_metadata["ack_count"] == 1


def test_rch_management_toggle_multiple_docs_and_malformed_raw_is_never_executed() -> None:
    management = b"".join(
        (
            _rch_frame("=o", sequence="0"),
            _rch_frame('="/(PRECONTO SINTETICO)', sequence="1"),
            _rch_frame('="/(Voce 10,00)', sequence="2"),
            _rch_frame("=o", sequence="3"),
            _rch_frame("=o", sequence="4"),
            _rch_frame('="/(COPIA CONFORME)', sequence="5"),
            _rch_frame("=o", sequence="6"),
        )
    )
    documents = parse_rch(
        management,
        b"",
        device_id="rch_1",
        session_id="session-management",
        job_id="job-management",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )
    assert [document.type for document in documents] == [
        DocumentType.PRE_BILL,
        DocumentType.CONFORMING_COPY,
    ]

    malformed = b"not-a-command\x02broken"
    result = frame_stream(malformed)
    assert result.frames == ()
    unknown = parse_rch(
        malformed,
        b"",
        device_id="rch_1",
        session_id="session-malformed",
        job_id="job-malformed",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )
    assert unknown[0].type is DocumentType.UNKNOWN
    assert unknown[0].warnings
