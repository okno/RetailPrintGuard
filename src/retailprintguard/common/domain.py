"""Canonical, protocol-neutral evidence and document models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DocumentType(StrEnum):
    ORDER = "ORDER"
    ORDER_CHANGE = "ORDER_CHANGE"
    KITCHEN_ORDER = "KITCHEN_ORDER"
    PRE_BILL = "PRE_BILL"
    MANAGEMENT_DOCUMENT = "MANAGEMENT_DOCUMENT"
    COMMERCIAL_DOCUMENT = "COMMERCIAL_DOCUMENT"
    SHIFT_END_REPORT = "SHIFT_END_REPORT"
    INVOICE = "INVOICE"
    CONFORMING_COPY = "CONFORMING_COPY"
    CANCELLATION = "CANCELLATION"
    REFUND = "REFUND"
    REPRINT = "REPRINT"
    PAYMENT = "PAYMENT"
    DEVICE_RESPONSE = "DEVICE_RESPONSE"
    UNKNOWN = "UNKNOWN"


NON_SALE_DOCUMENT_TYPES: frozenset[DocumentType] = frozenset(
    {
        DocumentType.SHIFT_END_REPORT,
        DocumentType.INVOICE,
    }
)


class OrderEventType(StrEnum):
    ORDER_CREATED = "ORDER_CREATED"
    ITEM_ADDED = "ITEM_ADDED"
    ITEM_REMOVED = "ITEM_REMOVED"
    QUANTITY_CHANGED = "QUANTITY_CHANGED"
    PRICE_CHANGED = "PRICE_CHANGED"
    DISCOUNT_APPLIED = "DISCOUNT_APPLIED"
    DISCOUNT_REMOVED = "DISCOUNT_REMOVED"
    ORDER_VOIDED = "ORDER_VOIDED"
    TABLE_CHANGED = "TABLE_CHANGED"
    ORDER_SPLIT = "ORDER_SPLIT"
    ORDER_MERGED = "ORDER_MERGED"
    PRE_BILL_PRINTED = "PRE_BILL_PRINTED"
    FISCAL_DOCUMENT_ISSUED = "FISCAL_DOCUMENT_ISSUED"
    PAYMENT_RECORDED = "PAYMENT_RECORDED"
    COPY_PRINTED = "COPY_PRINTED"
    ORDER_CLOSED = "ORDER_CLOSED"


class AlertStatus(StrEnum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    CONFIRMED = "CONFIRMED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    JUSTIFIED = "JUSTIFIED"
    CLOSED = "CLOSED"


class AlertSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EvidenceLevel(StrEnum):
    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


Money = Annotated[Decimal, Field(max_digits=19, decimal_places=4)]


class SourceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    direction: str
    offset: Annotated[int, Field(ge=0)]
    length: Annotated[int, Field(ge=0)]
    frame_id: str | None = None


class DocumentLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: Annotated[int, Field(ge=0)]
    course_code: str | None = None
    item_code: str | None = None
    description: str | None = None
    quantity: Money | None = None
    unit_price: Money | None = None
    original_unit_price: Money | None = None
    modified_unit_price: Money | None = None
    discount: Money | None = None
    surcharge: Money | None = None
    tax_rate: Money | None = None
    line_total: Money | None = None
    state: str | None = None
    cancelled: bool = False
    removed: bool = False
    raw_text: str | None = None
    source: SourceSpan | None = None


class PaymentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str | None = None
    amount: Money
    currency: str = "EUR"
    evidence: EvidenceLevel = EvidenceLevel.UNKNOWN


class ReceiptHeader(BaseModel):
    """Versioned merchant heading observed on, or configured for, an RCH print.

    The parser only creates ``RCH_PRINTED_HEADER`` values from text present in
    the captured stream.  ``DEVICE_METADATA_CONFIGURED`` is reserved for the
    API read-model fallback and must never be presented as captured evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    merchant_name: Annotated[str, Field(min_length=1, max_length=191)] | None = None
    legal_name: Annotated[str, Field(min_length=1, max_length=191)] | None = None
    address_lines: tuple[
        Annotated[str, Field(min_length=1, max_length=191)], ...
    ] = Field(default=(), max_length=8)
    phone: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    tax_code: Annotated[str, Field(min_length=1, max_length=32)] | None = None
    vat_number: Annotated[str, Field(min_length=1, max_length=32)] | None = None
    evidence: Literal["RCH_PRINTED_HEADER", "DEVICE_METADATA_CONFIGURED"]

    @field_validator(
        "merchant_name",
        "legal_name",
        "phone",
        "tax_code",
        "vat_number",
        mode="before",
    )
    @classmethod
    def strip_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("address_lines", mode="before")
    @classmethod
    def normalize_address_lines(cls, value: Any) -> Any:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("address_lines must be a list or tuple")
        return tuple(line.strip() if isinstance(line, str) else line for line in value)

    @model_validator(mode="after")
    def at_least_one_observed_field(self) -> ReceiptHeader:
        if not any(
            (
                self.merchant_name,
                self.legal_name,
                self.address_lines,
                self.phone,
                self.tax_code,
                self.vat_number,
            )
        ):
            raise ValueError("receipt header must contain at least one field")
        return self


class NormalizedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    source_device_id: str
    source_session_id: str | None = None
    source_job_id: str
    type: DocumentType
    subtype: str
    external_document_code: str | None = None
    # Some RCH status responses expose only the final counter.  It must never
    # be promoted to a full document number without separately observed proof.
    external_document_code_suffix: str | None = None
    # A conforming/management print can have its own progressive while
    # referring to a distinct commercial document.  Keeping the two values
    # separate prevents the reference from replacing the immutable identity
    # of the document being parsed.
    commercial_reference_code: str | None = None
    order_code: str | None = None
    table_code: str | None = None
    operator_code: str | None = None
    terminal_code: str | None = None
    receipt_header: ReceiptHeader | None = None
    # Keep the application-authored business time separate from the fiscal
    # printer clock.  On RCH output both can be visible on the same document
    # and a clock skew must remain auditable rather than being silently
    # collapsed into one timestamp.
    application_timestamp: datetime | None = None
    rch_footer_timestamp: datetime | None = None
    rch_serial_number: str | None = None
    document_timestamp: datetime | None = None
    captured_at: datetime
    gross_total: Money | None = None
    net_total: Money | None = None
    discount_total: Money | None = None
    tax_total: Money | None = None
    status: str
    normalized_text: str
    encoding: str | None = None
    parser_name: str
    parser_version: str
    parse_confidence: Annotated[int, Field(ge=0, le=100)]
    evidence: EvidenceLevel
    source_manifest_sha256: str
    source_payload_sha256: str
    source_path: str
    complete: bool
    warnings: tuple[str, ...] = ()
    lines: tuple[DocumentLine, ...] = ()
    payments: tuple[PaymentRecord, ...] = ()
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "captured_at",
        "application_timestamp",
        "rch_footer_timestamp",
        "document_timestamp",
    )
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @field_validator("source_manifest_sha256", "source_payload_sha256")
    @classmethod
    def sha256_hex(cls, value: str) -> str:
        normalized = value.lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("expected a SHA-256 hexadecimal digest")
        return normalized


class OrderEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    order_id: UUID
    type: OrderEventType
    occurred_at: datetime
    source_document_id: UUID | None = None
    operator_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    previous_hash: str | None = None
    record_hash: str | None = None

    @field_validator("occurred_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)


class CorrelationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_id: UUID = Field(default_factory=uuid4)
    document_ids: tuple[UUID, ...]
    score: Annotated[int, Field(ge=0, le=100)]
    algorithm_version: str
    matched_criteria: tuple[str, ...]
    unmatched_criteria: tuple[str, ...]
    explanation: str

    @model_validator(mode="after")
    def at_least_two_documents(self) -> CorrelationResult:
        if len(set(self.document_ids)) < 2:
            raise ValueError("a correlation requires at least two distinct documents")
        return self


class FraudFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_code: str
    rule_version: int
    severity: AlertSeverity
    score: Annotated[int, Field(ge=0, le=100)]
    transaction_id: UUID
    document_ids: tuple[UUID, ...]
    description: str
    explanation: str
    evidence: tuple[dict[str, Any], ...]
    confidence: Annotated[int, Field(ge=0, le=100)]
    opened_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
