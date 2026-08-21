from datetime import UTC, datetime
from types import SimpleNamespace

from retailprintguard.render.text import (
    RECEIPT_HEADER_NOT_AVAILABLE,
    document_text_export,
    rch_identity_text_lines,
    receipt_header_text_lines,
    receipt_text,
)


def test_receipt_text_keeps_printable_and_ocr_text_but_hides_parser_tokens() -> None:
    normalized = (
        "<ESC/POS:INIT><ESC/POS:CHAR_SIZE:17>Operatore: 14/08/42  19:25\n"
        "<OCR:ESC_STAR:96.50>Tavolo: LAB-22</OCR:ESC_STAR>\n"
        "<ESC/POS:ESC:45:1>Portata: 1\n"
        "<BYTE:0x01>2x Insalata mista\n\n\n<ESC/POS:CUT>"
    )

    result = receipt_text(normalized)

    assert result == (
        "Operatore: 14/08/42  19:25\n"
        "Tavolo: LAB-22\n"
        "Portata: 1\n"
        "2x Insalata mista"
    )
    assert "ESC/POS" not in result
    assert "<OCR:" not in result


def test_receipt_text_preserves_receipt_columns_and_bounds_input() -> None:
    normalized = "EURO\nArticolo                 4,00 A\nTOT                      4,00"

    assert receipt_text(normalized) == normalized
    assert receipt_text(normalized, maximum_characters=4) == "EURO"


def test_rch_identity_text_keeps_printed_clocks_capture_and_serial_separate() -> None:
    synthetic = SimpleNamespace(
        application_timestamp=datetime(2042, 8, 20, 12, 13, tzinfo=UTC),
        application_timestamp_precision="MINUTE",
        application_timestamp_evidence="RCH_APPLICATION_PRINTED_TEXT",
        captured_at=datetime(2042, 8, 20, 12, 13, 7, tzinfo=UTC),
        rch_footer_timestamp=datetime(2042, 8, 20, 12, 11, tzinfo=UTC),
        rch_footer_timestamp_precision="MINUTE",
        rch_footer_timestamp_evidence="RCH_FOOTER_PRINTED_TEXT",
        rch_clock_offset_seconds=-120,
        rch_serial_number="99SYN123456",
        rch_serial_number_evidence="RCH_PRINTED_RT_PREFIX",
    )

    text = "\n".join(rch_identity_text_lines(synthetic))

    assert "Ora applicativa RCH: 20/08/2042 14:13" in text
    assert "Acquisizione server: 20/08/2042 14:13:07" in text
    assert "Ora footer RCH: 20/08/2042 14:11" in text
    assert "-120 s (footer RCH indietro di 2 min" in text
    assert "Seriale RCH: 99SYN123456" in text
    assert "prefisso RT stampato dalla RCH" in text


def test_rch_identity_text_never_replaces_unobserved_wire_values() -> None:
    synthetic = SimpleNamespace(
        application_timestamp=None,
        captured_at=datetime(2042, 8, 20, 12, 13, 7, tzinfo=UTC),
        rch_footer_timestamp=None,
        rch_clock_offset_seconds=None,
        rch_serial_number="99CFG123456",
        rch_serial_number_evidence="DEVICE_METADATA_CONFIGURED",
    )

    text = "\n".join(rch_identity_text_lines(synthetic))

    assert "Ora applicativa RCH: Non osservato nel flusso" in text
    assert "Ora footer RCH: Non osservato nel flusso" in text
    assert "uno o entrambi gli orari non sono stati osservati nel flusso" in text
    assert "Seriale RCH: 99CFG123456" in text
    assert "metadato dispositivo configurato (non osservato nel flusso)" in text
    assert "Ora footer RCH: 20/08/2042 14:13:07" not in text


def test_receipt_header_text_preserves_observed_fields_and_provenance() -> None:
    synthetic = SimpleNamespace(
        receipt_header=SimpleNamespace(
            schema_version=1,
            merchant_name="LAB HOTEL",
            legal_name="SYNTHETIC HOSPITALITY S.R.L.",
            address_lines=["VIA DEL LABORATORIO 1", "00000 CITTA' TEST"],
            phone="0000000000",
            tax_code="SYNTHETIC01",
            vat_number="00000000000",
            evidence="RCH_PRINTED_HEADER",
        )
    )

    text = "\n".join(receipt_header_text_lines(synthetic))

    assert "INTESTAZIONE DOCUMENTO" in text
    assert "Insegna: LAB HOTEL" in text
    assert "Ragione sociale: SYNTHETIC HOSPITALITY S.R.L." in text
    assert "Partita IVA: 00000000000" in text
    assert "Osservata nel blocco iniziale stampato dalla RCH" in text
    assert "Configurata sul dispositivo" not in text


def test_receipt_header_text_distinguishes_configured_and_missing_headers() -> None:
    configured = SimpleNamespace(
        receipt_header={
            "schema_version": 1,
            "merchant_name": "LAB CONFIGURATO",
            "legal_name": None,
            "address_lines": [],
            "phone": None,
            "tax_code": None,
            "vat_number": None,
            "evidence": "DEVICE_METADATA_CONFIGURED",
        }
    )
    configured_text = "\n".join(receipt_header_text_lines(configured))
    missing_text = "\n".join(
        receipt_header_text_lines(SimpleNamespace(receipt_header=None))
    )

    assert "Configurata sul dispositivo (non osservata nel flusso)" in configured_text
    assert "stampato dalla RCH" not in configured_text
    assert RECEIPT_HEADER_NOT_AVAILABLE in missing_text


def test_document_text_export_adds_structured_header_without_parser_tokens() -> None:
    document = SimpleNamespace(
        receipt_header={
            "schema_version": 1,
            "merchant_name": "LAB HOTEL",
            "legal_name": None,
            "address_lines": ["VIA DEL LABORATORIO 1"],
            "phone": None,
            "tax_code": None,
            "vat_number": "00000000000",
            "evidence": "RCH_PRINTED_HEADER",
        },
        receipt_text=None,
        normalized_text=(
            "<ESC/POS:INIT>LAB HOTEL\nVIA DEL LABORATORIO 1\n"
            "Articolo sintetico  4,20\n<ESC/POS:CUT>"
        ),
    )

    text = document_text_export(document)

    assert text.startswith("RETAILPRINTGUARD - VISTA DERIVATA\n")
    assert text.count("LAB HOTEL") == 1
    assert "Insegna: LAB HOTEL" not in text
    assert "PROVENIENZA INTESTAZIONE\nOsservata" in text
    assert "TESTO DOCUMENTO\nLAB HOTEL" in text
    assert "ESC/POS" not in text
    assert text.endswith("\n")


def test_document_text_export_prepends_only_configured_header() -> None:
    document = SimpleNamespace(
        receipt_header={
            "schema_version": 1,
            "merchant_name": "LAB CONFIGURATO",
            "legal_name": None,
            "address_lines": ["VIA CONFIGURATA 1"],
            "phone": None,
            "tax_code": None,
            "vat_number": None,
            "evidence": "DEVICE_METADATA_CONFIGURED",
        },
        receipt_text="Articolo sintetico  7,25",
        normalized_text="ignored",
    )

    text = document_text_export(document)

    assert text.index("Insegna: LAB CONFIGURATO") < text.index("TESTO DOCUMENTO")
    assert "Configurata sul dispositivo (non osservata nel flusso)" in text
    assert "PROVENIENZA INTESTAZIONE" not in text
