from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from reportlab.lib.units import mm

from retailprintguard.api.schemas import (
    DocumentLineView,
    DocumentView,
    LinePriceAttributionView,
    ReceiptHeaderView,
)
from retailprintguard.render.pdf import (
    PDF_RENDERER_VERSION,
    _document_lines,
    _page_height,
    render_document_pdf,
)


def _document() -> DocumentView:
    return DocumentView(
        id=UUID("10000000-0000-4000-8000-000000000001"),
        device_id="rch_synthetic",
        job_id=UUID("20000000-0000-4000-8000-000000000002"),
        type="MANAGEMENT_DOCUMENT",
        subtype="SYNTHETIC_SETTLEMENT",
        external_document_code="9901-0043",
        external_document_code_suffix="0043",
        commercial_reference_code="9901-0042",
        progressive_observation_status="FULL_CODE_OBSERVED_IN_CAPTURE",
        external_code="LAB-0001",
        order_code="ORDER-LAB",
        table_code="TABLE-LAB",
        receipt_header=ReceiptHeaderView(
            merchant_name="LAB HOTEL",
            legal_name="SYNTHETIC HOSPITALITY S.R.L.",
            address_lines=["VIA DEL LABORATORIO 1", "00000 CITTA' TEST"],
            phone="0000000000",
            tax_code="SYNTHETIC01",
            vat_number="00000000000",
            evidence="RCH_PRINTED_HEADER",
        ),
        document_timestamp=datetime(2042, 5, 6, 10, 13, tzinfo=UTC),
        document_timestamp_precision="MINUTE",
        document_timestamp_evidence="RCH_APPLICATION_PRINTED_TEXT",
        application_timestamp=datetime(2042, 5, 6, 10, 13, tzinfo=UTC),
        application_timestamp_precision="MINUTE",
        application_timestamp_evidence="RCH_APPLICATION_PRINTED_TEXT",
        rch_footer_timestamp=datetime(2042, 5, 6, 10, 11, tzinfo=UTC),
        rch_footer_timestamp_precision="MINUTE",
        rch_footer_timestamp_evidence="RCH_FOOTER_PRINTED_TEXT",
        rch_serial_number="99SYN123456",
        rch_serial_number_evidence="RCH_PRINTED_RT_PREFIX",
        rch_clock_offset_seconds=-120,
        captured_at=datetime(2042, 5, 6, 10, 11, 12, tzinfo=UTC),
        gross_total=Decimal("5.00"),
        net_total=Decimal("5.00"),
        discount_total=Decimal("0.00"),
        tax_total=Decimal("0.45"),
        status="COMPLETE",
        normalized_text="Cover  4,00\nDrink A 1,00\x00",
        parser_name="synthetic",
        parser_version="1.2.3",
        confidence=95,
        sha256="a" * 64,
        complete=True,
        warnings=["synthetic warning"],
        lines=[
            DocumentLineView(
                sequence=1,
                description="Cover",
                quantity=Decimal("2"),
                unit_price=Decimal("2.00"),
                line_total=Decimal("4.00"),
            ),
            DocumentLineView(
                sequence=2,
                description="Drink A",
                quantity=Decimal("1"),
                unit_price=Decimal("1.00"),
                line_total=Decimal("1.00"),
            ),
        ],
        payments=[{"method": "ROOM", "amount": "5.00"}],
    )


def test_receipt_pdf_is_deterministic_bounded_and_identifies_the_renderer() -> None:
    text = "\n".join(line.text for line in _document_lines(_document()))
    compact_text = " ".join(text.split())
    assert "Documento: 9901-0043" in text
    assert "Suffisso progressivo RCH: 0043" in text
    assert "progressivo completo osservato nel flusso" in compact_text
    assert "Rif. commerciale: 9901-0042" in text
    assert "Ora applicativa RCH: 06/05/2042 12:13" in compact_text
    assert "Acquisizione server: 06/05/2042 12:11:12" in compact_text
    assert "Ora footer RCH: 06/05/2042 12:11" in compact_text
    assert "-120 s (footer RCH indietro di 2 min" in compact_text
    assert "Seriale RCH: 99SYN123456" in compact_text
    assert "prefisso RT stampato dalla RCH" in compact_text
    assert "INTESTAZIONE DOCUMENTO" in text
    assert "Insegna: LAB HOTEL" in text
    assert "Partita IVA: 00000000000" in text
    assert "Osservata nel blocco iniziale stampato dalla RCH" in compact_text

    suffix_only = _document().model_copy(
        update={
            "external_document_code": None,
            "external_code": None,
            "external_document_code_suffix": "0042",
            "resolved_external_document_code": "9901-0042",
            "resolved_external_document_code_provenance": (
                "CORRELATED_MANAGEMENT_REFERENCE"
            ),
            "progressive_observation_status": "SUFFIX_ONLY_OBSERVED_IN_CAPTURE",
        }
    )
    suffix_text = "\n".join(line.text for line in _document_lines(suffix_only))
    suffix_compact_text = " ".join(suffix_text.split())
    assert "Suffisso progressivo RCH: 0042" in suffix_text
    assert "non e' un codice completo" in suffix_compact_text
    assert "Documento risolto: 9901-0042 (da riferimento gestionale correlato)" in (
        suffix_compact_text
    )

    own_not_observed = suffix_only.model_copy(
        update={
            "external_document_code_suffix": None,
            "progressive_observation_status": "NOT_OBSERVED_IN_CAPTURE",
        }
    )
    unavailable_text = "\n".join(
        line.text for line in _document_lines(own_not_observed)
    )
    assert "progressivo proprio generato dalla RCH" in " ".join(
        unavailable_text.split()
    )

    wire_identity_not_observed = own_not_observed.model_copy(
        update={
            "application_timestamp": None,
            "application_timestamp_precision": None,
            "application_timestamp_evidence": None,
            "rch_footer_timestamp": None,
            "rch_footer_timestamp_precision": None,
            "rch_footer_timestamp_evidence": None,
            "rch_serial_number": None,
            "rch_serial_number_evidence": None,
            "rch_clock_offset_seconds": None,
        }
    )
    missing_identity_text = " ".join(
        line.text for line in _document_lines(wire_identity_not_observed)
    )
    assert "Ora applicativa RCH: Non osservato nel flusso" in missing_identity_text
    assert "Ora footer RCH: Non osservato nel flusso" in missing_identity_text
    assert "Seriale RCH: Non osservato nel flusso" in missing_identity_text
    assert "Ora footer RCH: 06/05/2042 12:11:12" not in missing_identity_text

    first = render_document_pdf(_document())
    second = render_document_pdf(_document())

    assert first == second
    assert first.startswith(b"%PDF-")
    assert first.endswith(b"%%EOF\n")
    assert 1_000 < len(first) < 100_000
    assert PDF_RENDERER_VERSION.encode() in first


def test_pdf_header_provenance_and_document_type_titles_are_explicit() -> None:
    configured = _document().model_copy(
        update={
            "receipt_header": ReceiptHeaderView(
                merchant_name="LAB CONFIGURATO",
                address_lines=[],
                evidence="DEVICE_METADATA_CONFIGURED",
            )
        }
    )
    configured_text = " ".join(line.text for line in _document_lines(configured))
    assert "Configurata sul dispositivo (non osservata nel flusso)" in configured_text
    assert "Osservata nel blocco iniziale" not in configured_text
    assert configured_text.count("INTESTAZIONE DOCUMENTO") == 1

    missing = _document().model_copy(update={"receipt_header": None})
    missing_text = " ".join(line.text for line in _document_lines(missing))
    assert (
        "Provenienza intestazione: Non osservata nel flusso e non configurata"
        in missing_text
    )

    observed_unstructured = _document().model_copy(
        update={
            "lines": [],
            "normalized_text": (
                "LAB HOTEL\nSYNTHETIC HOSPITALITY S.R.L.\n"
                "Articolo sintetico  5,00"
            ),
            "receipt_text": None,
        }
    )
    observed_text = "\n".join(
        line.text for line in _document_lines(observed_unstructured)
    )
    assert observed_text.count("LAB HOTEL") == 1
    assert "Insegna: LAB HOTEL" not in observed_text
    assert "Provenienza intestazione: Osservata" in observed_text

    shift_report = configured.model_copy(
        update={"type": "SHIFT_END_REPORT", "subtype": "RCH_REPORT_FINE_TURNO_LITERAL"}
    )
    invoice = configured.model_copy(
        update={"type": "INVOICE", "subtype": "RCH_INVOICE_STRONG_LITERAL"}
    )
    assert _document_lines(shift_report)[0].text == "REPORT DI FINE TURNO"
    assert _document_lines(invoice)[0].text == "FATTURA"


def _kitchen_document() -> DocumentView:
    source_document_id = UUID("30000000-0000-4000-8000-000000000003")
    return DocumentView(
        id=UUID("40000000-0000-4000-8000-000000000004"),
        device_id="pos_cucina",
        job_id=UUID("50000000-0000-4000-8000-000000000005"),
        type="KITCHEN_ORDER",
        subtype="COMANDA",
        order_code="ORD-22",
        table_code="22-B",
        operator_code="MARCO",
        covers=2,
        captured_at=datetime(2042, 5, 6, 10, 11, 12, tzinfo=UTC),
        status="COMPLETE",
        normalized_text="Portata: 1\n2x Insalata mista\nCoperti: 2",
        receipt_text="Portata: 1\n2x Insalata mista\nCoperti: 2",
        parser_name="escpos",
        parser_version="2.0",
        confidence=98,
        sha256="b" * 64,
        complete=True,
        lines=[
            DocumentLineView(
                id=UUID("60000000-0000-4000-8000-000000000006"),
                sequence=1,
                course_code="1",
                description="Insalata mista con patate e peperoni",
                quantity=Decimal("2.0000"),
                derived_unit_price=Decimal("4.50"),
                derived_price_source="PREBILL",
                price_attributions=[
                    LinePriceAttributionView(
                        id=UUID("70000000-0000-4000-8000-000000000007"),
                        correlation_id=UUID("80000000-0000-4000-8000-000000000008"),
                        source_document_id=source_document_id,
                        source_document_version_id=UUID(
                            "90000000-0000-4000-8000-000000000009"
                        ),
                        source_line_id=UUID("a0000000-0000-4000-8000-00000000000a"),
                        source_kind="PREBILL",
                        observed_unit_price=Decimal("4.50"),
                        observed_line_total=Decimal("9.00"),
                        confidence=Decimal("96.00"),
                        status="RESOLVED",
                        match_basis="DESCRIPTION_QUANTITY",
                        algorithm_version="test",
                        source_observed_at=datetime(2042, 5, 6, 10, 20, tzinfo=UTC),
                    )
                ],
            ),
            DocumentLineView(
                sequence=2,
                course_code="2",
                description="Crudo e melone",
                quantity=Decimal("1.0000"),
            ),
        ],
    )


def test_kitchen_pdf_is_compact_course_aware_and_does_not_duplicate_receipt_text() -> None:
    document = _kitchen_document()
    rendered_lines = _document_lines(document)
    text = "\n".join(line.text for line in rendered_lines)
    compact_text = " ".join(text.split())

    assert "TAVOLO 22-B" in text
    assert "Coperti: 2" in text
    assert "PORTATA 1" in text and "PORTATA 2" in text
    assert "2x Insalata mista" in text
    assert "2.0000x" not in text
    assert text.count("Insalata mista") == 1
    assert "TESTO DOCUMENTO" not in text
    assert "Prezzo derivato 4,50 EUR" in compact_text
    assert "Fonte PREBILL" in compact_text
    assert "confidenza 96%" in compact_text
    assert _page_height(rendered_lines) < 180 * mm
    assert render_document_pdf(document).startswith(b"%PDF-")


def test_kitchen_pdf_discloses_conflicting_derived_prices() -> None:
    document = _kitchen_document()
    source = document.lines[0].price_attributions[0]
    conflicting_line = document.lines[1].model_copy(
        update={
            "derived_unit_price": None,
            "derived_price_source": "CONFLICTING_SOURCES",
            "price_attributions": [
                source.model_copy(
                    update={
                        "id": UUID("b0000000-0000-4000-8000-00000000000b"),
                        "source_kind": "PREBILL",
                        "observed_unit_price": Decimal("10.00"),
                    }
                ),
                source.model_copy(
                    update={
                        "id": UUID("c0000000-0000-4000-8000-00000000000c"),
                        "source_kind": "FISCAL",
                        "observed_unit_price": Decimal("8.00"),
                    }
                ),
            ],
        }
    )
    document = document.model_copy(
        update={"lines": [document.lines[0], conflicting_line]}
    )
    text = " ".join(line.text for line in _document_lines(document))
    assert "Prezzi derivati in conflitto" in text
    assert "FISCAL 8,00 EUR" in text
    assert "PREBILL 10,00 EUR" in text
