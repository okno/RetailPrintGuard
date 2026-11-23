from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from retailprintguard.analysis.discounts import analyze_document_discounts
from retailprintguard.parser.rch import PARSER_VERSION, parse_rch


def _frame(text: str, index: int) -> bytes:
    data = text.encode("latin-1")
    prefix = b"\x0200" + f"{len(data):03d}".encode() + b"z" + data + str(index % 10).encode()
    checksum = 0
    for value in prefix:
        checksum ^= value
    return prefix + f"{checksum:02X}".encode() + b"\x03"


def _parse(lines: list[str]):
    commands = ["=o", *(f'="/({line})' for line in lines), "=o"]
    frames = [_frame(command, index) for index, command in enumerate(commands)]
    raw = b"".join(frames)
    document = parse_rch(
        raw,
        b"",
        device_id="rch_synthetic",
        job_id="quantity-context-synthetic",
        session_id="quantity-context-synthetic",
        captured_at=datetime(2042, 1, 2, tzinfo=UTC),
        manifest_sha256=hashlib.sha256(b"synthetic-manifest").hexdigest(),
        source_path="synthetic/client.raw",
    )[0]
    return document, raw, frames


def test_quantity_explanation_preserves_both_raw_spans_without_double_counting() -> None:
    text = [
        "DOCUMENTO GESTIONALE",
        "    4x 3,00",
        "Articolo sintetico 12,00 A",
        "SCONTO DIPENDENTI 30% [-30%] -3,60",
        "TOT 8,40",
    ]
    document, raw, frames = _parse(text)
    detail, product = document.lines[1:3]

    assert document.parser_version == PARSER_VERSION == "1.7.2"
    assert document.normalized_text == "\n".join(text)
    assert detail.raw_text == text[1] and detail.state == "QUANTITY_CONTEXT"
    assert detail.quantity is detail.unit_price is detail.line_total is None
    assert product.quantity == 4 and product.unit_price == 3 and product.line_total == 12
    assert detail.source is not None and product.source is not None
    assert raw[detail.source.offset : detail.source.offset + detail.source.length] == frames[2]
    assert raw[product.source.offset : product.source.offset + product.source.length] == frames[3]
    observation = document.raw_metadata["quantity_context_observations"][0]
    assert observation["context_source"] == detail.source.model_dump(mode="json")
    assert observation["product_source"] == product.source.model_dump(mode="json")
    assert observation["quantity"] == "4" and observation["unit_price"] == "3.00"
    discount = analyze_document_discounts(document)
    assert discount.arithmetic_reconciled and discount.employee_total == Decimal("3.60")


@pytest.mark.parametrize(
    "prefix,following,intervening",
    [
        ("4x 3,00", "Articolo sintetico 13,00", []),
        ("4x 3,00", "Articolo sintetico 12,00", [""]),
        ("4x 3,00", "TOT 12,00", []),
        ("4x 3,00", "Carta di credito 12,00", []),
        ("4x 3,00", "SCONTO 12,00", []),
        ("4x 3,00", "12,00", []),
        ("0x 3,00", "Articolo sintetico 0,00", []),
        ("-4x 3,00", "Articolo sintetico -12,00", []),
        ("Codice 4x 3,00", "Articolo sintetico 12,00", []),
    ],
)
def test_ambiguous_quantity_rows_are_not_reinterpreted(prefix, following, intervening) -> None:
    text = ["DOCUMENTO GESTIONALE", prefix, *intervening, following, "TOT 10,00"]
    document, _, _ = _parse(text)
    assert document.normalized_text == "\n".join(text)
    assert document.lines[1].state is None
    assert document.lines[1].line_total == 3
    assert document.raw_metadata["quantity_context_observations"] == []


def test_fractional_quantity_and_unpaired_last_row_remain_auditable() -> None:
    document, _, _ = _parse(
        [
            "DOCUMENTO GESTIONALE",
            "1,5x 2,00",
            "Articolo sintetico 3,00",
            "2x 7,00",
        ]
    )
    assert document.lines[2].quantity == Decimal("1.5")
    assert document.lines[2].unit_price == Decimal("2")
    assert document.lines[-1].state is None and document.lines[-1].line_total == 7


def test_multiple_explanations_keep_original_sequence_and_amounts() -> None:
    document, _, _ = _parse(
        [
            "DOCUMENTO GESTIONALE",
            "2x 3,00",
            "Articolo sintetico A 6,00",
            "3x 4,00",
            "Articolo sintetico B 12,00",
            "TOT 18,00",
        ]
    )
    assert [line.sequence for line in document.lines] == list(range(1, 7))
    assert [line.quantity for line in (document.lines[2], document.lines[4])] == [2, 3]
    assert len(document.raw_metadata["quantity_context_observations"]) == 2


def test_parser_build_identity_covers_shared_quantity_implementation(tmp_path, monkeypatch) -> None:
    from retailprintguard.analysis import discounts
    from retailprintguard.parser import rch, repository

    before = repository._parser_build_sha256(rch.PARSER_NAME)
    synthetic = tmp_path / "synthetic_quantity_implementation.py"
    synthetic.write_text("# Synthetic changed helper implementation\n")
    monkeypatch.setattr(discounts, "__file__", str(synthetic))
    after = repository._parser_build_sha256(rch.PARSER_NAME)
    assert before != after
