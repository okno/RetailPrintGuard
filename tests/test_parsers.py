from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

from retailprintguard.common.domain import DocumentType
from retailprintguard.parser.escpos import RasterOcrResult, parse_escpos
from retailprintguard.parser.rch import frame_stream, parse_rch

CAPTURED_AT = datetime(2042, 6, 7, 12, 0, tzinfo=UTC)
MANIFEST_HASH = hashlib.sha256(b"synthetic-manifest").hexdigest()


def _synthetic_table_raster(*, width: int = 8, strips: int = 4) -> bytes:
    band = b"\x1b*\x21" + bytes((width, 0)) + (b"\x00" * (width * 3))
    return b"\x1bJ\x30".join(band for _ in range(strips))


def _rch_frame(
    data: str,
    *,
    address: str = "00",
    frame_class: str = "z",
    sequence: str = "0",
) -> bytes:
    encoded = data.encode("latin-1")
    prefix = (
        b"\x02"
        + address.encode("ascii")
        + f"{len(encoded):03d}".encode("ascii")
        + frame_class.encode("ascii")
        + encoded
        + sequence.encode("ascii")
    )
    checksum = 0
    for value in prefix:
        checksum ^= value
    return prefix + f"{checksum:02X}".encode("ascii") + b"\x03"


def test_escpos_multiple_documents_and_controls_are_bounded_and_visible() -> None:
    first = (
        b"\x1b@PRECONTO N. PB-77\nTavolo: T-9\nOrdine: O-77\n"
        b"Piatto sintetico 100,00\nTOTALE 100,00\n\x1dV\x00"
    )
    second = b"\x1b@COMANDA N. C-78\nTavolo: T-9\nPiatto sintetico 50,00\nTOTALE 50,00\n\x1dV\x00"

    documents = parse_escpos(
        first + second,
        device_id="pos_1",
        session_id="session-synthetic",
        job_id="job-synthetic",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )

    assert [document.type for document in documents] == [
        DocumentType.PRE_BILL,
        DocumentType.KITCHEN_ORDER,
    ]
    assert all(document.complete for document in documents)
    assert documents[0].gross_total is not None
    assert documents[0].gross_total == 100
    assert documents[0].table_code == "T-9"
    assert "<ESC/POS:INIT>" in documents[0].normalized_text
    assert "<ESC/POS:CUT>" in documents[0].normalized_text
    assert documents[0].lines[0].source is not None


def test_rch_reconstructs_commercial_and_device_response_from_coalesced_stream() -> None:
    requests = b"".join(
        (
            _rch_frame("=K", sequence="0"),
            _rch_frame("=R7/$5000/*1/(VOCE SINTETICA)", sequence="1"),
            _rch_frame('="/?A/(Tavolo: T-7)', sequence="2"),
            _rch_frame('="/?A/(Ordine: O-7)', sequence="3"),
            _rch_frame("=T1/$5000", sequence="4"),
            _rch_frame("<</?s", sequence="5"),
            _rch_frame("<</?7", sequence="6"),
        )
    )
    responses = b"\x06" + _rch_frame(
        "s000000RE0042",
        address="01",
        frame_class="N",
        sequence="3",
    )

    documents = parse_rch(
        requests,
        responses,
        device_id="rch_1",
        session_id="session-rch",
        job_id="job-rch",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )

    commercial = documents[0]
    assert commercial.type is DocumentType.COMMERCIAL_DOCUMENT
    assert commercial.complete is True
    assert commercial.gross_total == 50
    assert commercial.table_code == "T-7"
    assert commercial.order_code == "O-7"
    assert commercial.lines[0].description == "VOCE SINTETICA"
    assert commercial.external_document_code is None
    assert commercial.external_document_code_suffix == "0042"
    assert commercial.raw_metadata["response_counter_suffix"] == "0042"
    assert commercial.raw_metadata["response_status_digits"] == "000000"
    assert (
        commercial.raw_metadata["external_document_code_suffix_evidence"]
        == "RCH_STATUS_RESPONSE_SUFFIX_SEQUENCE_CONFIRMED"
    )
    assert "0042" not in commercial.normalized_text
    assert documents[1].type is DocumentType.DEVICE_RESPONSE
    assert documents[1].raw_metadata["ack_count"] == 1


def test_rch_management_toggle_multiple_docs_and_malformed_raw_is_never_executed() -> None:
    management = b"".join(
        (
            _rch_frame("=o", sequence="0"),
            _rch_frame('="/(PRECONTO SINTETICO)', sequence="1"),
            _rch_frame('="/(Voce 10,00)', sequence="2"),
            _rch_frame("=o", sequence="3"),
            _rch_frame("=o", sequence="4"),
            _rch_frame('="/(COPIA CONFORME)', sequence="5"),
            _rch_frame("=o", sequence="6"),
        )
    )
    documents = parse_rch(
        management,
        b"",
        device_id="rch_1",
        session_id="session-management",
        job_id="job-management",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )
    assert [document.type for document in documents] == [
        DocumentType.PRE_BILL,
        DocumentType.CONFORMING_COPY,
    ]

    malformed = b"not-a-command\x02broken"
    result = frame_stream(malformed)
    assert result.frames == ()
    unknown = parse_rch(
        malformed,
        b"",
        device_id="rch_1",
        session_id="session-malformed",
        job_id="job-malformed",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )
    assert unknown[0].type is DocumentType.UNKNOWN
    assert unknown[0].warnings


def test_rch_rejects_an_unmatched_status_progressive() -> None:
    requests = b"".join(
        (
            _rch_frame("=K", sequence="0"),
            _rch_frame("=T1/$100", sequence="4"),
            _rch_frame("<</?s", sequence="5"),
            _rch_frame("<</?7", sequence="6"),
        )
    )
    unrelated_response = _rch_frame(
        "s000000RE0044",
        address="01",
        frame_class="N",
        sequence="2",
    )

    document = parse_rch(
        requests,
        unrelated_response,
        device_id="rch_synthetic",
        session_id="session-unmatched-progressive",
        job_id="job-unmatched-progressive",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )[0]

    assert document.type is DocumentType.COMMERCIAL_DOCUMENT
    assert document.external_document_code is None
    assert document.external_document_code_suffix is None
    assert document.raw_metadata["response_counter_suffix"] is None


def test_rch_management_copy_keeps_reference_without_inventing_own_progressive() -> None:
    management = b"".join(
        (
            _rch_frame("=o", sequence="0"),
            _rch_frame('="/(DOCUMENTO GESTIONALE)', sequence="1"),
            _rch_frame('="/(DOCUMENTO N. 9901-0042)', sequence="2"),
            _rch_frame("=o", sequence="3"),
        )
    )

    document = parse_rch(
        management,
        b"",
        device_id="rch_synthetic",
        session_id="session-management-reference",
        job_id="job-management-reference",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )[0]

    assert document.type is DocumentType.MANAGEMENT_DOCUMENT
    assert document.external_document_code is None
    assert document.external_document_code_suffix is None
    assert document.commercial_reference_code == "9901-0042"
    assert (
        document.raw_metadata["progressive_observation_status"]
        == "NOT_OBSERVED_IN_CAPTURE"
    )
    assert (
        document.raw_metadata["commercial_reference_code_evidence"]
        == "RCH_REQUEST_UNQUALIFIED_COMMERCIAL_DOCUMENT_NUMBER"
    )


def test_rch_observed_quantity_management_totals_and_error_status_regression() -> None:
    requests = b"".join(
        (
            _rch_frame("=k", sequence="0"),
            _rch_frame("=K", sequence="1"),
            _rch_frame("=R3/$200/*2/(Coperto)", sequence="2"),
            _rch_frame("=R2/$100/*1/(Bevanda sintetica)", sequence="3"),
            _rch_frame("=R4/$000/*1/(Voce ridotta)", sequence="4"),
        )
    )
    responses = b"".join(
        (
            b"\x06" + _rch_frame("ON00000000", address="01", frame_class="N", sequence="8"),
            b"\x06" + _rch_frame("ON00000000", address="01", frame_class="N", sequence="9"),
            b"\x06" + _rch_frame("ON00000000", address="01", frame_class="N", sequence="0"),
            b"\x06" + _rch_frame("ON00000000", address="01", frame_class="N", sequence="1"),
            b"\x06" + _rch_frame("ES00010000", address="01", frame_class="N", sequence="2"),
        )
    )
    documents = parse_rch(
        requests,
        responses,
        device_id="rch_synthetic",
        session_id="session-quantity",
        job_id="job-quantity",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )

    cancellation, commercial, response = documents
    assert cancellation.type is DocumentType.CANCELLATION
    assert cancellation.raw_metadata["observed_command"] == "=k"
    assert commercial.lines[0].quantity == 2
    assert commercial.lines[0].unit_price == 2
    assert commercial.lines[0].line_total == 4
    assert response.status == "ERROR"
    assert response.complete is True
    assert response.raw_metadata["device_error_codes"] == ["00010000"]
    assert "device_error_status:00010000" in response.warnings

    management = b"".join(
        (
            _rch_frame("=o", sequence="0"),
            _rch_frame('="/(PRECONTO SINTETICO)', sequence="1"),
            _rch_frame('="/(TOT                                      2,00)/*2', sequence="2"),
            _rch_frame('="/(Contanti                                 2,00)', sequence="3"),
            _rch_frame('="/(A 10% 10%                  1,82            0,18)', sequence="4"),
            _rch_frame('="/(TOT                        1,82            0,18)', sequence="5"),
            _rch_frame('="/(Tavolo: LAB-9)/*2', sequence="6"),
            _rch_frame('="/(Ordine: ORD-LAB)/*2', sequence="7"),
            _rch_frame('="/(01\\01\\42    12:34                  N. 9999-0042)', sequence="8"),
            _rch_frame("=o", sequence="0"),
        )
    )
    management_document = parse_rch(
        management,
        b"",
        device_id="rch_synthetic",
        session_id="session-management-total",
        job_id="job-management-total",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )[0]
    assert management_document.gross_total == 2
    assert management_document.tax_total == Decimal("0.18")
    assert management_document.table_code == "LAB-9"
    assert management_document.order_code == "ORD-LAB"
    assert management_document.external_document_code is None
    assert management_document.commercial_reference_code == "9999-0042"
    assert management_document.payments[0].method == "CONTANTI"
    assert management_document.payments[0].amount == 2


def test_escpos_legacy_cut_marks_document_complete_and_visible() -> None:
    documents = parse_escpos(
        b"\x1b@COMANDA SINTETICA\nVoce 7,50\nTOTALE 7,50\n\x1bm",
        device_id="pos_synthetic",
        session_id="session-legacy-cut",
        job_id="job-legacy-cut",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )

    assert len(documents) == 1
    assert documents[0].type is DocumentType.KITCHEN_ORDER
    assert documents[0].complete is True
    assert documents[0].status == "COMPLETE"
    assert "<ESC/POS:CUT>" in documents[0].normalized_text

    change = parse_escpos(
        b"\x1b@-1x Voce sintetica\nCoperti: 2\n\x1bm\x1bp\x00\x07\x79\x10\x04\x01",
        device_id="pos_synthetic",
        session_id="session-order-change",
        job_id="job-order-change",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
    )
    assert len(change) == 1
    assert change[0].type is DocumentType.ORDER_CHANGE
    assert change[0].complete is True


def test_escpos_pos_ticket_recovers_table_quantity_course_and_wrapped_item() -> None:
    payload = (
        b"\x1b@Operatore: 07/06/42  14:00\n"
        + _synthetic_table_raster()
        + b"\nPortata: 1\n--------------------------\n"
        b"2x Pietanza m\n    ista\nCoperti: 1\n\x1bm"
    )
    original_hash = hashlib.sha256(payload).hexdigest()
    seen: list[tuple[int, int, int]] = []

    def ocr(image: object) -> RasterOcrResult:
        seen.append((image.width, image.height, image.strip_count))  # type: ignore[attr-defined]
        return RasterOcrResult("Tavolo: LAB-22", 96.5, (0, 0, 8, 96))

    document = parse_escpos(
        payload,
        device_id="pos_synthetic",
        session_id="session-pos-ticket",
        job_id="job-pos-ticket",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
        ocr_engine=ocr,
    )[0]

    assert hashlib.sha256(payload).hexdigest() == original_hash
    assert seen == [(8, 96, 4)]
    assert document.type is DocumentType.KITCHEN_ORDER
    assert document.table_code == "LAB-22"
    assert document.operator_code is None
    assert document.document_timestamp == CAPTURED_AT
    assert document.gross_total is None
    assert document.raw_metadata["covers"] == 1
    assert document.raw_metadata["table_code_evidence"] == "ESC_POS_RASTER_OCR_INFERRED"
    assert "<OCR:ESC_STAR:96.50>Tavolo: LAB-22</OCR:ESC_STAR>" in document.normalized_text
    assert len(document.lines) == 1
    assert document.lines[0].description == "Pietanza mista"
    assert document.lines[0].quantity == 2
    assert document.lines[0].course_code == "1"
    assert document.lines[0].state == "ACTIVE"
    assert document.lines[0].raw_text == "2x Pietanza m\n    ista"
    assert document.lines[0].source is not None
    assert document.lines[0].source.offset >= 0
    assert document.lines[0].source.offset + document.lines[0].source.length <= len(payload)


def test_escpos_quantity_decrease_is_a_signed_delta_not_an_invented_removal() -> None:
    document = parse_escpos(
        b"\x1b@Portata: 1\n-1x Pietanza \n    mista\nCoperti: 1\n\x1bm",
        device_id="pos_synthetic",
        session_id="session-pos-change",
        job_id="job-pos-change",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
        ocr_engine=lambda _image: None,
    )[0]

    assert document.type is DocumentType.ORDER_CHANGE
    assert len(document.lines) == 1
    assert document.lines[0].description == "Pietanza mista"
    assert document.lines[0].quantity == -1
    assert document.lines[0].state == "QUANTITY_DECREASE"
    assert document.lines[0].removed is False


def test_escpos_wrap_rules_and_multiple_courses_preserve_business_lines_only() -> None:
    document = parse_escpos(
        (
            b"\x1b@Operatore: OP-LAB 07/06/42 14:00\n"
            b"Portata: 1\n--------------------------\n"
            b"1x Dessert\n    agli Agrum\n    i\n"
            b"1x Bevanda 33\n    cl\n"
            b"Portata: 2\n--------------------------\n"
            b"1x Acqua natu\n    rale grand\n    e\n"
            b"Coperti: 2\n\x1bm"
        ),
        device_id="pos_synthetic",
        session_id="session-pos-courses",
        job_id="job-pos-courses",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
        ocr_engine=lambda _image: None,
    )[0]

    assert document.operator_code == "OP-LAB"
    assert document.document_timestamp == CAPTURED_AT
    assert document.raw_metadata["covers"] == 2
    assert [line.description for line in document.lines] == [
        "Dessert agli Agrumi",
        "Bevanda 33 cl",
        "Acqua naturale grande",
    ]
    assert [line.course_code for line in document.lines] == ["1", "1", "2"]
    assert all(line.quantity == 1 for line in document.lines)


def test_escpos_raster_ocr_failure_and_low_confidence_are_non_fatal() -> None:
    payload = b"\x1b@" + _synthetic_table_raster() + b"\nPortata: 1\n1x Voce\n\x1bm"

    low = parse_escpos(
        payload,
        device_id="pos_synthetic",
        session_id="session-low-ocr",
        job_id="job-low-ocr",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
        ocr_engine=lambda _image: RasterOcrResult("Tavolo: LAB-LOW", 42.0),
    )[0]

    def failing_ocr(_image: object) -> RasterOcrResult:
        raise RuntimeError("synthetic OCR failure")

    failed = parse_escpos(
        payload,
        device_id="pos_synthetic",
        session_id="session-failed-ocr",
        job_id="job-failed-ocr",
        captured_at=CAPTURED_AT,
        manifest_sha256=MANIFEST_HASH,
        source_path="synthetic/client.raw",
        ocr_engine=failing_ocr,
    )[0]

    assert low.table_code is None
    assert "raster_table_ocr_below_confidence_threshold" in low.warnings
    assert low.complete is True
    assert failed.table_code is None
    assert "raster_ocr_backend_error" in failed.warnings
    assert failed.complete is True
