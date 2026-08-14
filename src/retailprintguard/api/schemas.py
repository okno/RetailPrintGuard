"""Public API schemas kept independent from persistence models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


class RoleName(StrEnum):
    ADMIN = "ADMIN"
    AUDITOR = "AUDITOR"
    OPERATOR = "OPERATOR"
    READ_ONLY = "READ_ONLY"


class UserPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    username: str
    roles: tuple[RoleName, ...]
    active: bool = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserPrincipal


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=1024)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class DeviceView(BaseModel):
    id: str
    name: str
    type: str
    mac_address: str | None = None
    department: str | None = None
    role: str | None = None
    enabled: bool
    online: bool
    listen_endpoint: str
    target_endpoint: str
    last_connection_at: datetime | None = None
    last_print_at: datetime | None = None
    last_response_at: datetime | None = None
    spool_bytes: int = 0
    pending_jobs: int = 0
    service_version: str | None = None
    last_error: str | None = None


class DashboardView(BaseModel):
    documents: int = 0
    orders: int = 0
    pre_bills: int = 0
    management_documents: int = 0
    commercial_documents: int = 0
    open_alerts: int = 0
    critical_alerts: int = 0
    economic_difference: Decimal = Decimal("0")
    devices_online: int = 0
    devices_offline: int = 0
    spool_bytes: int = 0
    parse_errors: int = 0
    alert_trend: list[dict[str, Any]] = Field(default_factory=list)
    anomaly_concentration: list[dict[str, Any]] = Field(default_factory=list)


class SessionView(BaseModel):
    id: UUID
    device_id: str
    source_endpoint: str
    target_endpoint: str
    opened_at: datetime
    closed_at: datetime | None = None
    close_reason: str | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    complete: bool = False


class JobView(BaseModel):
    id: UUID
    device_id: str
    session_id: UUID | None = None
    external_job_id: str
    captured_at: datetime
    status: str
    request_bytes: int
    response_bytes: int
    manifest_sha256: str
    spool_path: str
    imported_at: datetime | None = None
    parser_status: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SystemEventView(BaseModel):
    id: UUID
    service: str
    severity: str
    event_type: str
    message: str
    device_id: UUID | None = None
    session_id: UUID | None = None
    job_id: UUID | None = None
    correlation_id: str | None = None
    occurred_at: datetime
    error: str | None = None


class DiagnosticsView(BaseModel):
    generated_at: datetime
    database: str = "ok"
    spool: str = "unknown"
    parser_errors: int = 0
    incomplete_jobs: int = 0
    recent_events: list[SystemEventView] = Field(default_factory=list)


class DocumentLineView(BaseModel):
    sequence: int
    item_code: str | None = None
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    original_unit_price: Decimal | None = None
    modified_unit_price: Decimal | None = None
    discount: Decimal | None = None
    tax_rate: Decimal | None = None
    line_total: Decimal | None = None
    state: str | None = None
    removed: bool = False
    cancelled: bool = False
    raw_text: str | None = None


class DocumentView(BaseModel):
    id: UUID
    device_id: str
    job_id: UUID
    type: str
    subtype: str
    external_code: str | None = None
    order_code: str | None = None
    table_code: str | None = None
    operator_code: str | None = None
    terminal_code: str | None = None
    document_timestamp: datetime | None = None
    captured_at: datetime
    gross_total: Decimal | None = None
    net_total: Decimal | None = None
    discount_total: Decimal | None = None
    tax_total: Decimal | None = None
    status: str
    normalized_text: str
    parser_name: str
    parser_version: str
    confidence: int
    sha256: str
    complete: bool
    warnings: list[str] = Field(default_factory=list)
    lines: list[DocumentLineView] = Field(default_factory=list)
    payments: list[dict[str, Any]] = Field(default_factory=list)
    correlations: list[dict[str, Any]] = Field(default_factory=list)


class OrderView(BaseModel):
    id: UUID
    external_code: str | None = None
    table_code: str | None = None
    operator_code: str | None = None
    opened_at: datetime
    closed_at: datetime | None = None
    status: str
    current_total: Decimal | None = None
    version: int


class TransactionView(BaseModel):
    id: UUID
    order_id: UUID | None = None
    occurred_at: datetime
    table_code: str | None = None
    order_code: str | None = None
    operator_code: str | None = None
    initial_total: Decimal | None = None
    pre_bill_total: Decimal | None = None
    fiscal_total: Decimal | None = None
    difference: Decimal | None = None
    status: str
    document_count: int
    alert_count: int
    correlation_confidence: int
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    diff: dict[str, Any] = Field(default_factory=dict)


class AlertView(BaseModel):
    id: UUID
    rule_code: str
    severity: str
    score: int
    status: str
    opened_at: datetime
    transaction_id: UUID | None = None
    device_ids: list[str] = Field(default_factory=list)
    document_ids: list[UUID] = Field(default_factory=list)
    description: str
    explanation: str
    economic_difference: Decimal | None = None
    confidence: int
    assigned_to: UUID | None = None
    acknowledged_at: datetime | None = None
    closed_at: datetime | None = None
    resolution_reason: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)


class AlertUpdate(BaseModel):
    status: str | None = None
    assigned_to_me: bool = False
    note: str | None = Field(default=None, max_length=4000)
    resolution_reason: str | None = Field(default=None, max_length=4000)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str | None) -> str | None:
        allowed = {
            "OPEN",
            "UNDER_REVIEW",
            "CONFIRMED",
            "FALSE_POSITIVE",
            "JUSTIFIED",
            "CLOSED",
        }
        if value is not None and value not in allowed:
            raise ValueError(f"status must be one of: {', '.join(sorted(allowed))}")
        return value


class RuleView(BaseModel):
    code: str
    name: str
    enabled: bool
    version: int
    severity: str
    weight: int
    threshold: Decimal | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)


class ImportBatchView(BaseModel):
    id: UUID
    source_type: str
    source_root: str
    started_at: datetime
    completed_at: datetime | None = None
    status: str
    discovered: int = 0
    imported: int = 0
    duplicates: int = 0
    failed: int = 0
    report: dict[str, Any] = Field(default_factory=dict)


class SearchHit(BaseModel):
    entity_type: str
    entity_id: UUID
    occurred_at: datetime
    title: str
    subtitle: str | None = None
    highlights: list[str] = Field(default_factory=list)


class HealthView(BaseModel):
    status: str
    version: str
    database: str
    spool: str
    timestamp: datetime


class AuditEntry(BaseModel):
    actor_id: UUID | None
    action: str
    entity_type: str
    entity_id: str | None = None
    correlation_id: str
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def no_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden = {"password", "token", "secret", "authorization"}
        if forbidden.intersection(key.lower() for key in value):
            raise ValueError("audit metadata cannot contain secrets")
        return value
