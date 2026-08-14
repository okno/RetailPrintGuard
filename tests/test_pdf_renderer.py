from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from retailprintguard.api.schemas import DocumentLineView, DocumentView
from retailprintguard.render.pdf import PDF_RENDERER_VERSION, render_document_pdf


def _document() -> DocumentView:
    return DocumentView(
        id=UUID("10000000-0000-4000-8000-000000000001"),
        device_id="rch_synthetic",
        job_id=UUID("20000000-0000-4000-8000-000000000002"),
        type="MANAGEMENT_DOCUMENT",
        subtype="SYNTHETIC_SETTLEMENT",
        external_code="LAB-0001",
        order_code="ORDER-LAB",
        table_code="TABLE-LAB",
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
    first = render_document_pdf(_document())
    second = render_document_pdf(_document())

    assert first == second
    assert first.startswith(b"%PDF-")
    assert first.endswith(b"%%EOF\n")
    assert 1_000 < len(first) < 100_000
    assert PDF_RENDERER_VERSION.encode() in first
