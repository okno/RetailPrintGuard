from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

from retailprintguard.render.naming import archive_pdf_filename, document_pdf_filename


def _document(**changes):
    return SimpleNamespace(
        **{
            "device_id": "pos_1", "type": "PRE_BILL",
            "document_timestamp": datetime(2042, 7, 8, 23, 30, tzinfo=UTC),
            "captured_at": datetime(2042, 7, 10, 12, tzinfo=UTC),
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("changes", "reference"),
    [
        ({"external_document_code": "0001/0023", "order_code": "9"}, "numero-0001-0023"),
        ({"external_document_code": "0"}, "numero-0"),
        ({"external_document_code": "0000"}, "numero-0000"),
        ({"external_document_code": " ", "external_code": "0012"}, "numero-0012"),
        ({"order_code": "0"}, "ordine-0"),
        ({"order_code": "0042", "external_document_code_suffix": "23"}, "ordine-0042"),
        ({"external_document_code_suffix": "0023"}, "suffisso-0023"),
        (
            {"external_code": "0023", "external_document_code_suffix": "0023"},
            "suffisso-0023",
        ),
        (
            {
                "external_code": "inferred-code", "external_document_code_suffix": "0023",
                "progressive_observation_status": "SUFFIX_ONLY_OBSERVED_IN_CAPTURE",
            },
            "suffisso-0023",
        ),
        ({"resolved_external_document_code": "inferred-0001"}, "senza-numero-fiscale"),
        ({}, "senza-numero-fiscale"),
    ],
)
def test_names_use_observed_references_without_inventing_fiscal_numbers(changes, reference):
    assert document_pdf_filename(_document(**changes)) == (
        f"2042-07-09_preconto_{reference}_pos_1.pdf"
    )


def test_document_date_wins_and_capture_fallback_is_explicit_in_rome():
    assert document_pdf_filename(_document()).startswith("2042-07-09_")
    assert document_pdf_filename(_document(document_timestamp=None)).startswith(
        "acquisizione-2042-07-10_"
    )
    # SQLite snapshots may contain naive UTC values; UTC+2 crosses midnight in Rome.
    assert document_pdf_filename(
        _document(document_timestamp=datetime(2042, 7, 8, 23, 30))
    ).startswith("2042-07-09_")
    assert document_pdf_filename(
        _document(
            document_timestamp=datetime(2042, 7, 9, 1, 30, tzinfo=timezone(timedelta(hours=2)))
        )
    ).startswith("2042-07-09_")
    assert document_pdf_filename(
        _document(document_timestamp=None, captured_at=None)
    ).startswith("senza-data_")


@pytest.mark.parametrize(
    "code", ["../../CON", '\\x/../a\r\n";filename=evil', "A:" * 300, "è-東京", "..."]
)
def test_names_are_bounded_ascii_basenames_on_windows_and_posix(code):
    filename = document_pdf_filename(_document(external_document_code=code))
    assert filename.isascii()
    assert len(filename) <= 191
    assert re.fullmatch(r"[A-Za-z0-9_-]+\.pdf", filename)
    assert PurePosixPath(filename).name == filename
    assert PureWindowsPath(filename).name == filename
    assert ".." not in filename


def test_non_ascii_references_remain_distinguishable():
    assert "numero-caffe_" in document_pdf_filename(_document(external_document_code="caffè"))
    assert document_pdf_filename(_document(external_document_code="東京")) != (
        document_pdf_filename(_document(external_document_code="大阪"))
    )
    assert document_pdf_filename(_document(external_document_code="x" * 100 + "a")) != (
        document_pdf_filename(_document(external_document_code="x" * 100 + "b"))
    )


def test_archive_sequence_disambiguates_duplicate_observed_codes():
    document = _document(external_document_code="0007")
    first = archive_pdf_filename(document, 1)
    second = archive_pdf_filename(document, 2)
    assert first == "000001_" + document_pdf_filename(document)
    assert second == "000002_" + document_pdf_filename(document)
    assert first != second


@pytest.mark.parametrize("sequence", [0, -1, True, "1", 1.5, 1000000])
def test_archive_sequence_must_be_an_explicit_positive_integer(sequence):
    with pytest.raises(ValueError, match="archive sequence"):
        archive_pdf_filename(_document(), sequence)
