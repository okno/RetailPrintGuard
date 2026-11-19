"""Recipient identity must never be promoted to the merchant's fiscal header."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from retailprintguard.parser.rch import parse_rch


def _frame(text: str, index: int) -> bytes:
    data = text.encode("latin-1")
    prefix = b"\x0200" + f"{len(data):03d}".encode() + b"z" + data + str(index % 10).encode()
    checksum = 0
    for value in prefix:
        checksum ^= value
    return prefix + f"{checksum:02X}".encode() + b"\x03"


def _parse(lines: list[str]):
    commands = ["=o", *(f'=\"/({line})' for line in lines), "=o"]
    frames = [_frame(command, index) for index, command in enumerate(commands)]
    raw = b"".join(frames)
    document = parse_rch(
        raw,
        b"",
        device_id="rch_synthetic",
        job_id="recipient-header-synthetic",
        session_id="recipient-header-synthetic",
        captured_at=datetime(2042, 1, 2, tzinfo=UTC),
        manifest_sha256=hashlib.sha256(b"synthetic-recipient-manifest").hexdigest(),
        source_path="synthetic/client.raw",
    )[0]
    # The established document normalization trims only the outer whitespace;
    # receipt-header extraction must not otherwise rewrite the printed body.
    assert document.normalized_text == "\n".join(lines).strip()
    return document, raw, frames


@pytest.mark.parametrize(
    "label",
    [
        "Destinatario:",
        "  destinatario :  ",
        "DESTINATARIO",
        "Cliente:",
        "CLIENTE",
        "Cliente: SOCIETA RICEVENTE S.R.L.",
        "Dati cliente:",
        "DATI DEL CLIENTE",
    ],
)
def test_recipient_only_header_retains_text_and_every_original_raw_span(label: str) -> None:
    lines = [
        label,
        "SOCIETA RICEVENTE S.R.L.",
        "VIA ESEMPIO 17",
        "00100 CITTA SINTETICA",
        "P.IVA SYNTHDEST01",
        "Voce sintetica 1,00",
        "TOT 1,00",
    ]
    document, raw, frames = _parse(lines)

    assert document.receipt_header is None
    assert document.raw_metadata["receipt_header_evidence"] is None
    assert document.raw_metadata["receipt_header_line_indexes"] == []
    assert document.raw_metadata["receipt_header_source_spans"] == []
    # No recipient row is removed as a supposed merchant heading. Its original
    # bytes and source positions remain inspectable, including label spacing.
    for index, text in enumerate(lines[:5]):
        row = next(line for line in document.lines if line.raw_text == text)
        assert row.source is not None
        assert row.source.offset == sum(map(len, frames[: index + 1]))
        assert raw[row.source.offset : row.source.offset + row.source.length] == frames[index + 1]


def test_verified_merchant_before_recipient_keeps_only_its_own_identity_and_spans() -> None:
    merchant = ["CAFFE SINTETICO", "ESERCENTE S.R.L.", "VIA LAB 3", "P.IVA SYNTHMER01"]
    recipient = ["Destinatario:", "RICEVENTE S.R.L.", "VIA TEST 19", "P.IVA SYNTHDEST01"]
    document, raw, frames = _parse([*merchant, *recipient, "Voce sintetica 1,00", "TOT 1,00"])

    header = document.receipt_header
    assert header is not None
    assert header.merchant_name == merchant[0]
    assert header.legal_name == merchant[1]
    assert header.address_lines == (merchant[2],)
    assert header.vat_number == "SYNTHMER01"
    assert document.raw_metadata["receipt_header_line_indexes"] == [0, 1, 2, 3]
    spans = document.raw_metadata["receipt_header_source_spans"]
    assert len(spans) == len(merchant)
    for index, span in enumerate(spans):
        assert raw[span["offset"] : span["offset"] + span["length"]] == frames[index + 1]
    assert {line.raw_text for line in document.lines} >= set(recipient)
    assert not set(merchant) & {line.raw_text for line in document.lines}


def test_recipient_tax_identity_cannot_corroborate_an_unverified_merchant_name() -> None:
    document, _, _ = _parse(
        ["CAFFE SINTETICO", "Cliente:", "RICEVENTE S.R.L.", "P.IVA SYNTHDEST01", "TOT 1,00"]
    )
    assert document.receipt_header is None
    assert document.raw_metadata["receipt_header_line_indexes"] == []


@pytest.mark.parametrize("merchant", ["IL DESTINATARIO", "CLIENTE S.R.L.", "DESTINATARIO LAB"])
def test_words_inside_a_merchant_name_are_not_recipient_labels(merchant: str) -> None:
    document, _, _ = _parse([merchant, "VIA LAB 3", "P.IVA SYNTHMER01", "TOT 1,00"])
    assert document.receipt_header is not None
    assert document.receipt_header.vat_number == "SYNTHMER01"
    assert merchant in {
        document.receipt_header.merchant_name,
        document.receipt_header.legal_name,
    }
