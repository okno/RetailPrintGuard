from retailprintguard.render.text import receipt_text


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
