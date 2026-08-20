from datetime import UTC, datetime
from types import SimpleNamespace

from retailprintguard.render.text import rch_identity_text_lines, receipt_text


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
