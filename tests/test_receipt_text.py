from datetime import UTC, datetime
from types import SimpleNamespace

from retailprintguard.render.text import (
    RECEIPT_HEADER_NOT_AVAILABLE,
    document_text_export,
    rch_identity_text_lines,
    receipt_header_text_lines,
    receipt_preview_text,
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


def test_web_preview_keeps_whitespace_and_uses_exact_truthy_fallback_order() -> None:
    preview = "  Caffè          4,00 €  \n\n\n"
    document = SimpleNamespace(receipt_text=preview, normalized_text="not used")
    assert receipt_preview_text(document) == preview
    document.receipt_text = " \n "
    assert receipt_preview_text(document) == " \n "
    document.receipt_text = ""
    assert receipt_preview_text(document) == "not used"
    document.normalized_text = ""
    assert receipt_preview_text(document) == "Nessun testo documento disponibile."


def test_web_preview_does_not_duplicate_header_already_observed_in_body() -> None:
    source = " LAB OSSERVATO \n\nArticolo   1,00"
    for evidence in ("RCH_PRINTED_HEADER", "UNKNOWN", None):
        document = SimpleNamespace(receipt_text=source, receipt_header={
            "evidence": evidence, "merchant_name": "LAB OSSERVATO", "address_lines": []
        })
        assert receipt_preview_text(document) == source


def test_web_preview_configured_header_does_not_trim_or_drop_blank_addresses() -> None:
    document = SimpleNamespace(receipt_text=" body \n", receipt_header={
        "evidence": "DEVICE_METADATA_CONFIGURED", "merchant_name": "  Merchant  ",
        "legal_name": " ", "address_lines": [" First ", "", " Third  "],
        "phone": " 123 ", "tax_code": "", "vat_number": " 456 ",
    })
    assert receipt_preview_text(document) == (
        "  Merchant  \n \n First \n\n Third  \n"
        "Telefono  123 \nP.IVA  456 \n\n body \n"
    )


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

    assert "Configurata sul dispositivo (non osservata nel flusso)" not in configured_text
    assert "LAB CONFIGURATO" in configured_text
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
    assert "Configurata sul dispositivo (non osservata nel flusso)" not in text
    assert "PROVENIENZA INTESTAZIONE" not in text


def test_document_text_export_projects_dgfe_header_with_distinct_provenance() -> None:
    document = SimpleNamespace(
        receipt_header={
            "schema_version": 1,
            "merchant_name": "LAB MEMORIA",
            "legal_name": None,
            "address_lines": ["VIA SINTETICA 42"],
            "phone": None,
            "tax_code": None,
            "vat_number": "99999999999",
            "evidence": "RCH_DGFE_EXPORT",
        },
        receipt_text="Articolo sintetico  1,23",
        normalized_text="ignored",
    )

    text = document_text_export(document)

    assert text.count("LAB MEMORIA") == 1
    assert text.index("Insegna: LAB MEMORIA") < text.index("TESTO DOCUMENTO")
    assert "Estratta dall'export della memoria fiscale DGFE" in text
    assert "osservata nel flusso" not in text.lower()


def test_ocr_projection_matches_legacy_regex_for_bounded_adversarial_inputs() -> None:
    """Compatibility oracle includes malformed nesting and mismatched labels."""
    import itertools
    import random
    import re

    from retailprintguard.render.text import _unwrap_ocr_blocks

    legacy = re.compile(r"<OCR:[^>]+>(.*?)</OCR:[^>]+>", re.DOTALL)
    tokens = (
        "a", "\n", "€", "<OCR:x>", "<OCR:y>", "</OCR:x>", "</OCR:y>",
        "<OCR:>", "</OCR:>", "<OCR:", "</OCR:", ">",
    )
    cases = ["".join(parts) for parts in itertools.product(tokens, repeat=3)]
    generator = random.Random(2042)  # noqa: S311 - reproducible compatibility corpus
    cases.extend(
        "".join(generator.choices(tokens, k=generator.randrange(1, 30)))
        for _ in range(2000)
    )
    for source in cases:
        expected = legacy.sub(lambda match: match.group(1), source)
        assert _unwrap_ocr_blocks(source) == expected, repr(source)


def test_ocr_projection_preserves_unmatched_tags_without_dropping_later_text() -> None:
    # This malformed body previously caused quadratic retries of every opener.
    source = "<OCR:x>" * 30_000 + "\n2x Articolo sintetico €"
    assert receipt_text(source) == source
    # An unmatched closing tag and an empty label have never been stripped.
    assert receipt_text("<OCR:>test</OCR:x>") == "<OCR:>test</OCR:x>"
    assert receipt_text("<OCR:x>test</OCR:>") == "<OCR:x>test</OCR:>"


def test_ocr_projection_keeps_legacy_nonrecursive_and_mismatched_label_semantics() -> None:
    source = "before<OCR:outer>A<OCR:inner>B</OCR:other>C</OCR:outer>after"
    assert receipt_text(source) == "beforeA<OCR:inner>BC</OCR:outer>after"
    assert receipt_text("<OCR:a>one\n\ntwo</OCR:b>\nthree") == "one\n\ntwo\nthree"


def test_technical_projection_matches_legacy_regex_for_bounded_adversarial_inputs() -> None:
    import itertools
    import random
    import re

    from retailprintguard.render.text import _strip_technical_tokens

    legacy = re.compile(r"<(?:ESC/POS|BYTE):[^>]*>")
    tokens = (
        "x", "€", "\n", "<BYTE:", "<ESC/POS:", "<BYTE:>", "<ESC/POS:>",
        "<BYTE:x>", "<ESC/POS:x>", "<byte:x>", "<OCR:x>", "</OCR:x>", ">", "<",
    )
    cases = ["".join(parts) for parts in itertools.product(tokens, repeat=3)]
    generator = random.Random(2042)  # noqa: S311 - reproducible compatibility corpus
    cases.extend(
        "".join(generator.choices(tokens, k=generator.randrange(1, 30)))
        for _ in range(2000)
    )
    for source in cases:
        assert _strip_technical_tokens(source) == legacy.sub("", source), repr(source)


def test_unclosed_technical_tokens_keep_later_text_without_quadratic_retries() -> None:
    for token in ("<BYTE:", "<ESC/POS:"):
        source = token * 25_000 + "\nArticolo sintetico €"
        assert receipt_text(source) == source
    assert receipt_text("before<BYTE:>after") == "beforeafter"
    assert receipt_text("before<ESC/POS:<BYTE:nested>>after") == "before>after"
    assert receipt_text("<byte:literal>\n<ESC/POS:complete>text") == "<byte:literal>\ntext"
