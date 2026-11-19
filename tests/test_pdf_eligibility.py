from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from retailprintguard.render.eligibility import (
    DOCUMENT_PDF_POLICY_VERSION,
    classify_pdf_document,
)


def document(**changes):
    return {
        "type": "MANAGEMENT_DOCUMENT",
        "subtype": "SYNTHETIC_DOCUMENT",
        "receipt_text": "Articolo sintetico    7,50\nTOT                   7,50",
        "normalized_text": "unused technical projection",
        "complete": True,
        **changes,
    }


def test_complete_business_document_needs_no_fiscal_number_date_or_job_success():
    source = document(
        type="KITCHEN_ORDER", receipt_text="Portata: 1\n2x Piatto sintetico",
        lines=[], payments=[], gross_total=None, document_timestamp=None,
        external_document_code=None, order_code=None,
        job_status="PARTIAL", review_state="PENDING", analysis_excluded=True,
    )
    before = deepcopy(source)
    result = classify_pdf_document(source)
    assert result.category == "complete"
    assert result.reason_code == "COMPLETE_PRINTABLE_DOCUMENT"
    assert source == before
    assert result == classify_pdf_document(SimpleNamespace(**source))
    assert DOCUMENT_PDF_POLICY_VERSION == "rpg-document-pdf-policy-1.0.0"


@pytest.mark.parametrize("kind,body", [
    ("PRE_BILL", "PRECONTO\nArticolo sintetico    7,50\nTOT    7,50"),
    ("MANAGEMENT_DOCUMENT", "CORRISPETTIVO NON RISCOSSO\nTOT 0,00"),
    ("ORDER_CHANGE", "Portata: 1\n-1x Articolo sintetico"),
    ("CANCELLATION", "ANNULLAMENTO COMANDA\nTavolo: TEST-1"),
    ("REFUND", "RESO\nArticolo sintetico -7,50\nTOTALE -7,50"),
    ("REPRINT", "RISTAMPA\nPortata: 1\n1x Articolo sintetico"),
    ("INVOICE", "FATTURA\nPrestazione sintetica 7,50\nTOTALE 7,50"),
    ("CONFORMING_COPY", "COPIA CONFORME\nArticolo sintetico 7,50"),
    ("PAYMENT", "PAGAMENTO CONTANTI\n7,50"),
])
def test_document_families_zero_negative_amounts_and_printed_cancellations_remain(kind, body):
    assert classify_pdf_document(document(type=kind, receipt_text=body)).category == "complete"


def test_partial_business_content_is_separate_even_when_it_contains_total_and_payment():
    for body in ("Articolo sintetico 7,50", "Articolo 7,50\nTOT 7,50\nCONTANTI 7,50"):
        result = classify_pdf_document(document(receipt_text=body, complete=False))
        assert result.category == "partial"
        assert result.reason_code == "PARTIAL_PRINTABLE_DOCUMENT"


@pytest.mark.parametrize("kind", ["MANAGEMENT_DOCUMENT", "COMMERCIAL_DOCUMENT"])
def test_configured_header_preview_and_structured_metadata_do_not_fill_an_empty_body(kind):
    source = document(
        type=kind, receipt_text="", normalized_text="", complete=True,
        receipt_header={
            "merchant_name": "SYNTHETIC HOTEL", "evidence": "DEVICE_METADATA_CONFIGURED",
        },
        receipt_preview="SYNTHETIC HOTEL\nArticolo inventato 999,99",
        lines=[{"description": "EURO", "quantity": "1"}], gross_total="999.99",
    )
    before = deepcopy(source)
    decision = classify_pdf_document(source)
    assert decision.category == "excluded"
    assert decision.reason_code == "EMPTY_BODY"
    assert source == before


@pytest.mark.parametrize("body", [
    "ON00000000\nON00000000", "ES00010000", "s000001RE0042\n0042",
    "response text resembling a receipt 7,50",
])
def test_known_response_role_is_excluded_regardless_of_success_or_print_like_text(body):
    result = classify_pdf_document(document(type="DEVICE_RESPONSE", receipt_text=body))
    assert result.category == "excluded"
    assert result.reason_code == "DEVICE_RESPONSE"


def test_only_the_observed_command_subtype_is_excluded_not_all_cancellations():
    source = document(
        type="CANCELLATION", subtype="RCH_DOCUMENT_CANCEL_COMMAND_OBSERVED",
        receipt_text="RCH cancellation command observed",
    )
    assert classify_pdf_document(source).reason_code == "TECHNICAL_CANCEL_COMMAND"
    source.update(subtype="ANNULLAMENTO_LITERAL", receipt_text="ANNULLAMENTO\nTavolo: TEST-1")
    assert classify_pdf_document(source).category == "complete"


@pytest.mark.parametrize("body", [
    "<ESC/POS:INIT><BYTE:0x01><ESC/POS:CUT>",
    "\x00\x01\x1b\x7f", "----\n=====\n....\n****", " \n \t ",
])
def test_control_or_separator_only_stream_is_not_a_receipt(body):
    result = classify_pdf_document(document(receipt_text=body, normalized_text=body))
    assert result.category == "excluded"
    assert result.reason_code in {"EMPTY_BODY", "CONTROL_ONLY_BODY"}


@pytest.mark.parametrize("body", [
    "Scontrino: SF 0099-0001 06\\05\\42, 12:00",
    " Scontrino: SF 0099-0001 06/05/2042, 12:00:01 \n---",
    "DOC. GESTIONALE N. 0099-0001\n06-05-2042 12:00\nRT 99SYN123456",
    "Rif. documento commerciale N. 0099-0001",
])
def test_reference_footer_only_is_excluded_even_with_spurious_structured_rows(body):
    source = document(
        receipt_text=body, lines=[{"description": body, "quantity": "1"}],
        external_document_code="0099-0001",
    )
    result = classify_pdf_document(source)
    assert result.category == "excluded"
    assert result.reason_code == "FOOTER_ONLY"


@pytest.mark.parametrize("body", [
    "Scontrino: SF 0099-0001 06/05/42, 12:00\nArticolo sintetico 0,00",
    "Scontrino speciale 7,50\nTOT 7,50",
    "ON00000000 cocktail 7,50\nTOT 7,50",
    "Articolo sintetico 7,50\nDOC. GESTIONALE N. 0099-0001",
])
def test_receipt_containing_footer_or_status_like_item_is_not_excluded(body):
    assert classify_pdf_document(document(receipt_text=body)).category == "complete"


@pytest.mark.parametrize("kind", ["SHIFT_END_REPORT", "MANAGEMENT_DOCUMENT"])
def test_empty_report_template_is_distinguished_from_zero_activity_report(kind):
    body = "Report di fine turno\n   Pagamenti\nEURO\n------\nLibro cassa\nEURO"
    source = document(type=kind, receipt_text=body)
    result = classify_pdf_document(source)
    assert result.category == "excluded"
    assert result.reason_code == "EMPTY_REPORT_TEMPLATE"
    for observed_line in ("Contanti 0,00", "Nessun movimento registrato", "Scontrini 0"):
        source["receipt_text"] = body + "\n" + observed_line
        assert classify_pdf_document(source).category == "complete"


def test_unknown_protocol_is_not_promoted_to_a_fiscal_document_from_text_alone():
    source = document(type="UNKNOWN", subtype="UNRECOGNIZED", receipt_text="Articolo 7,50")
    result = classify_pdf_document(source)
    assert result.category == "excluded"
    assert result.reason_code == "UNCLASSIFIED_DOCUMENT"


def test_parser_annotations_and_ocr_wrappers_do_not_hide_real_content_or_change_source():
    source = document(
        type="KITCHEN_ORDER", receipt_text=None,
        normalized_text="<ESC/POS:INIT><OCR:TEST>Portata: 1</OCR:TEST>\n2x Articolo sintetico",
    )
    before = deepcopy(source)
    assert classify_pdf_document(source).category == "complete"
    assert source == before


def test_inspection_does_not_silently_truncate_a_later_business_line():
    body = "\n" * 250_001 + "Articolo sintetico 7,50"
    assert classify_pdf_document(document(receipt_text=body)).category == "complete"


def test_malformed_repeated_ocr_openers_keep_later_business_text():
    body = "<OCR:TEST>" * 30_000 + "2x Articolo sintetico"
    assert classify_pdf_document(document(type="KITCHEN_ORDER", receipt_text=body)).category == (
        "complete"
    )
    assert classify_pdf_document(document(receipt_text="<OCR:TEST>" * 30_000)).reason_code == (
        "CONTROL_ONLY_BODY"
    )
