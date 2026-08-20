from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from retailprintguard.common.domain import DocumentType
from retailprintguard.parser.rch import PARSER_VERSION, parse_rch
from retailprintguard.render.pdf import _generic_lines

CAPTURED_AT = datetime(2042, 8, 20, 20, 18, 30, tzinfo=UTC)
MANIFEST_HASH = hashlib.sha256(b"synthetic-taxonomy-manifest").hexdigest()


def _rch_frame(data: str, *, sequence: str = "0") -> bytes:
    encoded = data.encode("latin-1")
    prefix = b"\x02" + b"00" + f"{len(encoded):03d}".encode() + b"z" + encoded
    prefix += sequence.encode()
    checksum = 0
    for value in prefix:
        checksum ^= value
    return prefix + f"{checksum:02X}".encode() + b"\x03"


def _management_document(*lines: str):
    payload = b"".join(
        (
            _rch_frame("=o", sequence="0"),
            *(
                _rch_frame(f'="/({line})', sequence=str(index % 10))
                for index, line in enumerate(lines, start=1)
            ),
            _rch_frame("=o", sequence="0"),
        )
    )
    return parse_rch(
        payload,
        b"",
        device_id="rch_taxonomy",
        session_id="session-taxonomy",
        job_id="job-taxonomy-" + hashlib.sha256(payload).hexdigest()[:12],
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )[0]


def test_shift_end_report_signature_wins_over_aggregate_invoice_row() -> None:
    document = _management_document(
        "Report di fine turno",
        "                         EURO",
        "Scontrini              123,45",
        "Fatture                 42,35",
        "Conti chiusi            81,10",
        "TOT                    246,90",
        "Pagamenti",
        "Contanti               109,80",
    )

    assert document.type is DocumentType.SHIFT_END_REPORT
    assert document.subtype == "RCH_REPORT_FINE_TURNO_LITERAL"
    assert document.parse_confidence == 98
    assert document.parser_version == PARSER_VERSION == "1.5.0"


def test_invoice_requires_a_strong_singular_document_signature() -> None:
    invoice = _management_document(
        "STRUTTURA SINTETICA",
        "FATTURA N. FT-0042",
        "Servizio sintetico      12,00",
        "TOTALE                  12,00",
    )
    electronic_invoice = _management_document(
        "STRUTTURA SINTETICA",
        "FATTURA ELETTRONICA",
        "Numero fattura: 2026-77",
    )
    aggregate_only = _management_document(
        "Riepilogo giornaliero",
        "Fatture                 42,35",
        "TOT                     42,35",
    )

    assert invoice.type is DocumentType.INVOICE
    assert invoice.subtype == "RCH_FATTURA_LITERAL"
    assert electronic_invoice.type is DocumentType.INVOICE
    assert aggregate_only.type is DocumentType.MANAGEMENT_DOCUMENT
    assert aggregate_only.subtype == "RCH_GESTIONALE_INFERRED"


def test_commercial_wire_is_not_reclassified_by_printed_invoice_wording() -> None:
    payload = b"".join(
        (
            _rch_frame("=K", sequence="0"),
            _rch_frame('="/?A/(FATTURA)', sequence="1"),
            _rch_frame("=T1/$100", sequence="2"),
            _rch_frame("<</?7", sequence="3"),
        )
    )
    document = parse_rch(
        payload,
        b"",
        device_id="rch_taxonomy",
        session_id="session-commercial-taxonomy",
        job_id="job-commercial-taxonomy",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )[0]

    assert document.type is DocumentType.COMMERCIAL_DOCUMENT


def test_pdf_renderer_uses_operator_labels_for_non_sale_document_types() -> None:
    for document_type, expected_label in (
        ("SHIFT_END_REPORT", "REPORT DI FINE TURNO"),
        ("INVOICE", "FATTURA"),
    ):
        document = SimpleNamespace(
            id=uuid4(),
            type=document_type,
            subtype="INTERNAL_SUBTYPE",
            device_id="synthetic_device",
            parser_name="synthetic-parser",
            parser_version="1.0",
            document_timestamp=None,
            captured_at=CAPTURED_AT,
            external_document_code=None,
            external_code=None,
            external_document_code_suffix=None,
            commercial_reference_code=None,
            order_code=None,
            table_code=None,
            operator_code=None,
            terminal_code=None,
            lines=(),
            gross_total=None,
            net_total=None,
            discount_total=None,
            tax_total=None,
            payments=(),
            warnings=(),
            sha256=hashlib.sha256(document_type.encode()).hexdigest(),
        )

        lines = _generic_lines(document, "testo sintetico")

        assert lines[0].text == expected_label
        assert lines[1].text == expected_label
