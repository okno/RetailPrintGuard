"""Read models for observed discounts and their document-level evidence."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from retailprintguard.analysis.discounts import (
    DISCOUNT_ANALYSIS_VERSION,
    DiscountAnalysis,
    analyze_document_discounts,
)
from retailprintguard.render.eligibility import classify_pdf_document

DISCOUNT_SALE_DOCUMENT_TYPES = frozenset({
    "PRE_BILL", "MANAGEMENT_DOCUMENT", "COMMERCIAL_DOCUMENT",
    "ORDER", "ORDER_CHANGE", "KITCHEN_ORDER",
})


def analyze_sale_document_discounts(document: Any) -> DiscountAnalysis:
    """Use the same printable-sale scope for live and frozen reporting.

    Report summaries, protocol responses and copies are evidence, but their
    labelled amounts must not be counted as additional sale discounts. Readable
    partial sales remain observed; the arithmetic helper marks them for review.
    """
    kind = document.get("type") if isinstance(document, dict) else getattr(document, "type", None)
    reason = (
        "document_type_not_sale" if kind not in DISCOUNT_SALE_DOCUMENT_TYPES
        else "document_not_printable"
        if classify_pdf_document(document).category == "excluded"
        else None
    )
    if reason is None:
        return analyze_document_discounts(document)
    return DiscountAnalysis(
        observations=(), employee_total=Decimal("0"), generic_30_total=Decimal("0"),
        other_discount_total=Decimal("0"), arithmetic_reconciled=False, reasons=(reason,),
    )


class DiscountObservationView(BaseModel):
    sequence: int
    label: str
    amount: Decimal
    rate_percent: Decimal | None
    kind: Literal["employee_30", "generic_30", "other"]
    basis: Decimal | None
    arithmetic_verified: bool
    source_text: str


class DiscountAnalysisView(BaseModel):
    observations: list[DiscountObservationView]
    employee_total: Decimal
    generic_30_total: Decimal
    other_discount_total: Decimal
    arithmetic_reconciled: bool
    reasons: list[str]
    policy_version: str


class DiscountSummary(BaseModel):
    observed_employee_discount_total: Decimal = Decimal("0")
    observed_employee_discount_count: int = 0
    observed_generic_30_discount_total: Decimal = Decimal("0")
    observed_generic_30_discount_count: int = 0
    arithmetic_reconciled_employee_discount_total: Decimal = Decimal("0")
    arithmetic_reconciled_employee_discount_count: int = 0


class DiscountDocumentView(BaseModel):
    document_id: UUID
    document_version_id: UUID
    device_id: str
    device_name: str
    type: str
    complete: bool
    captured_at: datetime
    document_timestamp: datetime | None
    external_document_code: str | None
    commercial_reference_code: str | None
    order_code: str | None
    pdf_category: str
    discounts: DiscountAnalysisView


class DiscountPage(BaseModel):
    items: list[DiscountDocumentView] = Field(default_factory=list)
    total: int = 0
    limit: int
    offset: int
    summary: DiscountSummary = Field(default_factory=DiscountSummary)
    date_basis: Literal["captured_at"] = "captured_at"
    counting_basis: Literal["document_identity"] = "document_identity"
    policy_version: str = DISCOUNT_ANALYSIS_VERSION
