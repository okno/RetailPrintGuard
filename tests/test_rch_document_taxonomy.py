from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from retailprintguard.common.domain import DocumentType
from retailprintguard.parser.rch import PARSER_VERSION, parse_rch
from retailprintguard.render.pdf import _layout_preview
from retailprintguard.render.text import receipt_preview_text

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
    assert document.parser_version == PARSER_VERSION == "1.7.3"


def test_shift_end_report_sums_all_observed_payment_methods() -> None:
    document = _management_document(
        "Report di fine turno",
        "Pagamenti",
        "                         EURO",
        "Contanti               320,00",
        "Bancomat               288,50",
        "Carta di credito       150,00",
        "Addebito in camera      50,00",
        "----------------------------",
        "Libro cassa",
        "                         EURO",
        "Contanti               999,00",
    )

    assert document.type is DocumentType.SHIFT_END_REPORT
    assert document.gross_total == document.net_total == Decimal("808.50")
    assert [(payment.method, payment.amount) for payment in document.payments] == [
        ("CONTANTI", Decimal("320.00")),
        ("BANCOMAT", Decimal("288.50")),
        ("CARTA DI CREDITO", Decimal("150.00")),
        ("ADDEBITO IN CAMERA", Decimal("50.00")),
    ]
    assert (
        document.raw_metadata["shift_end_payment_total_evidence"]
        == "RCH_PRINTED_PAYMENT_SECTION_SUM"
    )


def test_shift_end_report_sums_repeated_methods_without_counting_summary() -> None:
    document = _management_document(
        "Report di fine turno",
        "Pagamenti",
        "EURO",
        "Contanti 40,00",
        "Contanti 30,00",
        "Bancomat 51,80",
        "TOTALE PAGAMENTI 121,80",
        "----------------------------",
        "Libro cassa",
        "Contanti 500,00",
    )

    assert document.gross_total == Decimal("121.80")
    assert [payment.amount for payment in document.payments] == [
        Decimal("40.00"),
        Decimal("30.00"),
        Decimal("51.80"),
    ]


def test_shift_end_report_without_bounded_payments_keeps_existing_total() -> None:
    without_payments = _management_document(
        "Report di fine turno",
        "Scontrini 10,00",
        "Fatture 5,00",
    )
    without_end = _management_document(
        "Report di fine turno",
        "Pagamenti",
        "Contanti 70,00",
        "Bancomat 51,80",
    )

    assert without_payments.gross_total == Decimal("5.00")
    assert without_end.gross_total == Decimal("51.80")
    assert without_payments.raw_metadata["shift_end_payment_total_evidence"] is None
    assert without_end.raw_metadata["shift_end_payment_total_evidence"] is None


def test_management_document_does_not_sum_shift_end_payment_rows() -> None:
    document = _management_document(
        "Riepilogo gestionale",
        "Pagamenti",
        "Contanti 70,00",
        "Bancomat 51,80",
        "Libro cassa",
        "Contanti 500,00",
    )

    assert document.type is DocumentType.MANAGEMENT_DOCUMENT
    assert document.gross_total == Decimal("500.00")
    assert document.raw_metadata["shift_end_payment_total_evidence"] is None


def test_management_receipt_service_mode_follows_printed_order_or_table() -> None:
    dine_in = _management_document("Documento gestionale", "Tavolo: Z-99")
    takeaway = _management_document("Documento gestionale", "Ordine: 9001")
    conflicting = _management_document(
        "Documento gestionale", "Tavolo: Z-99", "Ordine: 9001"
    )
    unknown = _management_document("Documento gestionale", "Voce sintetica")

    assert dine_in.table_code == "Z-99"
    assert dine_in.raw_metadata["service_mode"] == "DINE_IN"
    assert takeaway.order_code == "9001"
    assert takeaway.raw_metadata["service_mode"] == "TAKEAWAY"
    assert conflicting.raw_metadata["service_mode"] == "UNDETERMINED"
    assert conflicting.raw_metadata["service_mode_evidence"] == (
        "CONFLICTING_ORDER_AND_TABLE_MARKERS"
    )
    assert unknown.raw_metadata["service_mode"] == "UNDETERMINED"


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


def test_pdf_renderer_keeps_printed_non_sale_title_without_extra_taxonomy_labels() -> None:
    for document_type, printed_label in (
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
            receipt_text=printed_label + "\nTesto sintetico",
        )

        lines = _layout_preview(receipt_preview_text(document)).lines

        assert lines == (printed_label, "Testo sintetico")
