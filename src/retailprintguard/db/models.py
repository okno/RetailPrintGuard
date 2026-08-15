"""Normalised SQLAlchemy 2 model for RetailPrintGuard.

Source evidence and every derived interpretation are deliberately separate.
Deleting an interpretation must never cascade into captured RAW evidence.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DECIMAL, LONGBLOB
from sqlalchemy.orm import Mapped, mapped_column

from retailprintguard.db.base import Base, utc_now
from retailprintguard.db.types import UTCDateTime, UUIDBinary

MONEY = Numeric(19, 4).with_variant(DECIMAL(19, 4), "mysql").with_variant(DECIMAL(19, 4), "mariadb")
SHA256 = String(64)
RAW_BINARY = LargeBinary().with_variant(LONGBLOB(), "mysql").with_variant(LONGBLOB(), "mariadb")


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("external_id"),
        UniqueConstraint("listen_ip", "listen_port"),
        UniqueConstraint("target_ip", "target_port"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    mac_address: Mapped[str | None] = mapped_column(String(17))
    department: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[str | None] = mapped_column(String(64))
    device_type: Mapped[str] = mapped_column(String(16), nullable=False)
    parser_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    bidirectional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    listen_ip: Mapped[str] = mapped_column(String(45), nullable=False)
    listen_port: Mapped[int] = mapped_column(Integer, nullable=False)
    target_ip: Mapped[str] = mapped_column(String(45), nullable=False)
    target_port: Mapped[int] = mapped_column(Integer, nullable=False)
    non_sensitive_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class DeviceStatus(Base):
    __tablename__ = "device_status"
    __table_args__ = (Index("ix_device_status_device_observed", "device_id", "observed_at"),)

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    device_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )
    observed_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_connection_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    last_print_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    last_response_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    spool_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    pending_imports: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    service_version: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ProxySession(Base):
    __tablename__ = "proxy_sessions"
    __table_args__ = (
        UniqueConstraint("source_system", "source_instance", "source_scope", "source_session_id"),
        Index("ix_proxy_sessions_device_started", "device_id", "started_at"),
        Index("ix_proxy_sessions_source_session", "source_session_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    device_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_instance: Mapped[str] = mapped_column(String(128), nullable=False)
    source_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    source_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(45))
    client_port: Mapped[int | None] = mapped_column(Integer)
    listen_ip: Mapped[str] = mapped_column(String(45), nullable=False)
    listen_port: Mapped[int] = mapped_column(Integer, nullable=False)
    target_ip: Mapped[str] = mapped_column(String(45), nullable=False)
    target_port: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[Any] = mapped_column(UTCDateTime(), nullable=False)
    ended_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    client_fin_received: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    device_fin_received: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bytes_to_device: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    bytes_to_client: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    capture_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class PrintJob(Base):
    __tablename__ = "print_jobs"
    __table_args__ = (
        UniqueConstraint("source_key"),
        UniqueConstraint("source_system", "source_instance", "source_scope", "source_job_id"),
        Index("ix_print_jobs_device_started", "device_id", "started_at"),
        Index("ix_print_jobs_import_status", "import_status", "captured_at"),
        Index("ix_print_jobs_review_state", "review_state", "captured_at"),
        CheckConstraint(
            "review_state IN ('PENDING', 'VERIFIED_USABLE', 'EXCLUDED')",
            name="review_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    device_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )
    session_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("proxy_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_instance: Mapped[str] = mapped_column(String(128), nullable=False)
    source_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    source_job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_schema: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(SHA256, nullable=False)
    manifest_path: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[Any] = mapped_column(UTCDateTime(), nullable=False)
    ended_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    captured_at: Mapped[Any] = mapped_column(UTCDateTime(), nullable=False)
    boundary_source: Mapped[str | None] = mapped_column(String(64))
    boundary_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    capture_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    timeline_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    import_status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    errors: Mapped[list[Any]] = mapped_column(JSON, default=list)
    imported_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    review_state: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    analysis_excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewed_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("users.id", ondelete="RESTRICT")
    )
    review_reason: Mapped[str | None] = mapped_column(Text)


class RawPayload(Base):
    __tablename__ = "raw_payloads"
    __table_args__ = (
        UniqueConstraint("job_id", "artifact_role", "source_path_sha256"),
        UniqueConstraint("chain_scope", "chain_sequence"),
        Index("ix_raw_payloads_sha256", "sha256"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("print_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    device_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )
    session_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("proxy_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    artifact_role: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[bytes] = mapped_column(RAW_BINARY, nullable=False)
    byte_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(SHA256, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_path_sha256: Mapped[str] = mapped_column(SHA256, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    chain_scope: Mapped[str] = mapped_column(String(191), nullable=False)
    chain_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    previous_record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)
    record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class StreamChunk(Base):
    __tablename__ = "stream_chunks"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence"),
        UniqueConstraint("session_id", "observed_sequence"),
        UniqueConstraint("session_id", "direction", "direction_offset"),
        Index("ix_stream_chunks_job_sequence", "job_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("proxy_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    job_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("print_jobs.id", ondelete="RESTRICT")
    )
    raw_payload_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("raw_payloads.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="data")
    direction_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at: Mapped[Any] = mapped_column(UTCDateTime(), nullable=False)
    received_unix_ns: Mapped[int | None] = mapped_column(BigInteger)
    monotonic_ns: Mapped[int | None] = mapped_column(BigInteger)
    forwarded_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    local_drain_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    byte_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(SHA256, nullable=False)
    payload: Mapped[bytes] = mapped_column(RAW_BINARY, nullable=False)
    forwarded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    forward_status: Mapped[str] = mapped_column(String(64), nullable=False)
    forward_error: Mapped[str | None] = mapped_column(Text)
    previous_record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)
    record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)


class ParserVersion(Base):
    __tablename__ = "parser_versions"
    __table_args__ = (UniqueConstraint("name", "version", "build_sha256"),)

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    build_sha256: Mapped[str] = mapped_column(SHA256, nullable=False)
    protocol: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    installed_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class ActiveParserVersion(Base):
    """Explicit parser activation pointer, including intentional rollbacks."""

    __tablename__ = "active_parser_versions"

    parser_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    parser_version_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("parser_versions.id", ondelete="RESTRICT"), nullable=False
    )
    activated_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    activation_reason: Mapped[str] = mapped_column(String(191), nullable=False)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("job_id", "source_document_key"),
        Index("ix_documents_order_captured", "order_code", "captured_at"),
        Index("ix_documents_external_code", "external_document_code"),
        Index("ix_documents_table_captured", "table_code", "captured_at"),
        Index("ix_documents_type_captured", "document_type", "captured_at"),
        Index("ix_documents_captured_cursor", "captured_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    device_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )
    session_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("proxy_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    job_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("print_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    source_document_key: Mapped[str] = mapped_column(String(191), nullable=False)
    document_type: Mapped[str] = mapped_column(String(48), nullable=False)
    subtype: Mapped[str] = mapped_column(String(128), nullable=False)
    external_document_code: Mapped[str | None] = mapped_column(String(128))
    order_code: Mapped[str | None] = mapped_column(String(128))
    table_code: Mapped[str | None] = mapped_column(String(128))
    operator_code: Mapped[str | None] = mapped_column(String(128))
    terminal_code: Mapped[str | None] = mapped_column(String(128))
    document_timestamp: Mapped[Any | None] = mapped_column(UTCDateTime())
    captured_at: Mapped[Any] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_sequence"),
        UniqueConstraint("document_id", "parser_version_id", "source_payload_sha256"),
        UniqueConstraint("chain_scope", "chain_sequence"),
        Index("ix_document_versions_parser", "parser_version_id", "parsed_at"),
        Index("ix_document_versions_parsed_cursor", "parsed_at", "id"),
        Index("ix_document_versions_order_document", "order_code", "document_id"),
        Index("ix_document_versions_external_document", "external_document_code", "document_id"),
        Index("ix_document_versions_table_document", "table_code", "document_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    parser_version_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("parser_versions.id", ondelete="RESTRICT"), nullable=False
    )
    raw_payload_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("raw_payloads.id", ondelete="RESTRICT")
    )
    parse_run_id: Mapped[UUID] = mapped_column(UUIDBinary(), nullable=False, default=uuid4)
    version_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    parsed_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    # Semantic identity is parser output and therefore belongs to the immutable
    # version, not only to the mutable ``documents`` read projection.  These
    # columns remain nullable so databases created by older releases and rows
    # imported by legacy tooling can be read through the documented fallback.
    document_type: Mapped[str | None] = mapped_column(String(48))
    subtype: Mapped[str | None] = mapped_column(String(128))
    external_document_code: Mapped[str | None] = mapped_column(String(128))
    order_code: Mapped[str | None] = mapped_column(String(128))
    table_code: Mapped[str | None] = mapped_column(String(128))
    operator_code: Mapped[str | None] = mapped_column(String(128))
    terminal_code: Mapped[str | None] = mapped_column(String(128))
    document_timestamp: Mapped[Any | None] = mapped_column(UTCDateTime())
    gross_total: Mapped[Decimal | None] = mapped_column(MONEY)
    net_total: Mapped[Decimal | None] = mapped_column(MONEY)
    discount_total: Mapped[Decimal | None] = mapped_column(MONEY)
    tax_total: Mapped[Decimal | None] = mapped_column(MONEY)
    payment_method: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    encoding: Mapped[str | None] = mapped_column(String(64))
    parse_confidence: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    evidence_level: Mapped[str] = mapped_column(String(32), nullable=False)
    source_manifest_sha256: Mapped[str] = mapped_column(SHA256, nullable=False)
    source_payload_sha256: Mapped[str] = mapped_column(SHA256, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    errors: Mapped[list[Any]] = mapped_column(JSON, default=list)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    chain_scope: Mapped[str] = mapped_column(String(191), nullable=False)
    chain_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    previous_record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)
    record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)


class DocumentLine(Base):
    __tablename__ = "document_lines"
    __table_args__ = (
        UniqueConstraint("document_version_id", "sequence"),
        Index("ix_document_lines_description", "description"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    document_version_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    course_code: Mapped[str | None] = mapped_column(String(64))
    item_code: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(String(512))
    quantity: Mapped[Decimal | None] = mapped_column(MONEY)
    unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    original_unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    modified_unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    discount: Mapped[Decimal | None] = mapped_column(MONEY)
    surcharge: Mapped[Decimal | None] = mapped_column(MONEY)
    tax_rate: Mapped[Decimal | None] = mapped_column(MONEY)
    line_total: Mapped[Decimal | None] = mapped_column(MONEY)
    line_state: Mapped[str | None] = mapped_column(String(64))
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    removed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text)
    source_direction: Mapped[str | None] = mapped_column(String(32))
    source_offset: Mapped[int | None] = mapped_column(BigInteger)
    source_length: Mapped[int | None] = mapped_column(BigInteger)
    source_frame_id: Mapped[str | None] = mapped_column(String(128))


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("source_device_id", "business_date", "order_code"),
        Index("ix_orders_table_opened", "table_code", "opened_at"),
        Index("ix_orders_operator_opened", "operator_code", "opened_at"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    source_device_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("devices.id", ondelete="RESTRICT")
    )
    parent_order_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("orders.id", ondelete="RESTRICT")
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    order_code: Mapped[str] = mapped_column(String(128), nullable=False)
    table_code: Mapped[str | None] = mapped_column(String(128))
    operator_code: Mapped[str | None] = mapped_column(String(128))
    terminal_code: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    opened_at: Mapped[Any] = mapped_column(UTCDateTime(), nullable=False)
    closed_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    gross_total: Mapped[Decimal | None] = mapped_column(MONEY)
    net_total: Mapped[Decimal | None] = mapped_column(MONEY)
    discount_total: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class OrderEvent(Base):
    __tablename__ = "order_events"
    __table_args__ = (
        UniqueConstraint("order_id", "sequence"),
        Index("ix_order_events_order_occurred", "order_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    source_document_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("documents.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[Any] = mapped_column(UTCDateTime(), nullable=False)
    operator_code: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    previous_record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)
    record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)


class OrderSnapshot(Base):
    __tablename__ = "order_snapshots"
    __table_args__ = (UniqueConstraint("order_id", "sequence"),)

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    order_event_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("order_events.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_at: Mapped[Any] = mapped_column(UTCDateTime(), nullable=False)
    gross_total: Mapped[Decimal | None] = mapped_column(MONEY)
    net_total: Mapped[Decimal | None] = mapped_column(MONEY)
    discount_total: Mapped[Decimal | None] = mapped_column(MONEY)
    lines: Mapped[list[Any]] = mapped_column(JSON, default=list)
    previous_record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)
    record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(
            "document_version_id IS NOT NULL OR order_id IS NOT NULL",
            name="payment_has_owner",
        ),
        Index("ix_payments_order_paid", "order_id", "paid_at"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    order_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("orders.id", ondelete="RESTRICT")
    )
    document_version_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("document_versions.id", ondelete="RESTRICT")
    )
    external_payment_code: Mapped[str | None] = mapped_column(String(128))
    method: Mapped[str | None] = mapped_column(String(128))
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    evidence_level: Mapped[str] = mapped_column(String(32), nullable=False)
    paid_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class DocumentCorrelation(Base):
    __tablename__ = "document_correlations"
    __table_args__ = (
        UniqueConstraint(
            "transaction_id",
            "algorithm_version",
            "input_fingerprint",
            name="uq_corr_tx_algorithm_input",
        ),
        Index("ix_document_correlations_score", "score", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    transaction_id: Mapped[UUID] = mapped_column(UUIDBinary(), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(SHA256, nullable=False)
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="AUTOMATIC")
    matched_criteria: Mapped[list[Any]] = mapped_column(JSON, default=list)
    unmatched_criteria: Mapped[list[Any]] = mapped_column(JSON, default=list)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("users.id", ondelete="RESTRICT")
    )
    review_decision: Mapped[str | None] = mapped_column(String(64))
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class DocumentCorrelationMember(Base):
    __tablename__ = "document_correlation_members"

    correlation_id: Mapped[UUID] = mapped_column(
        UUIDBinary(),
        ForeignKey("document_correlations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    document_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("documents.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(48), nullable=False)
    contribution_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    criteria: Mapped[list[Any]] = mapped_column(JSON, default=list)


class LinePriceAttribution(Base):
    """Append-only price provenance for a POS line.

    The referenced :class:`DocumentLine` is never modified.  A row records one
    monetary source considered by one versioned algorithm inside one persisted
    correlation.  Conflicting candidates are retained as ``AMBIGUOUS`` rows so
    downstream consumers cannot mistake an arbitrary choice for an observed
    fact.
    """

    __tablename__ = "line_price_attributions"
    __table_args__ = (
        UniqueConstraint("attribution_fingerprint", name="uq_line_price_attr_fingerprint"),
        UniqueConstraint(
            "correlation_id",
            "target_line_id",
            "source_line_id",
            "algorithm_version",
            name="uq_line_price_attr_identity",
        ),
        CheckConstraint(
            "source_kind IN ('PREBILL', 'MANAGEMENT', 'FISCAL')",
            name="line_price_attr_source_kind",
        ),
        CheckConstraint(
            "status IN ('RESOLVED', 'AGREED', 'AMBIGUOUS')",
            name="line_price_attr_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="line_price_attr_confidence",
        ),
        CheckConstraint(
            "observed_unit_price IS NOT NULL OR observed_line_total IS NOT NULL",
            name="line_price_attr_has_amount",
        ),
        Index(
            "ix_line_price_attr_target_created",
            "target_line_id",
            "created_at",
        ),
        Index(
            "ix_line_price_attr_correlation_status",
            "correlation_id",
            "status",
        ),
        Index(
            "ix_line_price_attr_source_document",
            "source_document_id",
            "source_document_version_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    correlation_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("document_correlations.id", ondelete="RESTRICT"), nullable=False
    )
    target_document_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    target_document_version_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=False
    )
    target_line_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("document_lines.id", ondelete="RESTRICT"), nullable=False
    )
    source_document_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    source_document_version_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=False
    )
    source_line_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("document_lines.id", ondelete="RESTRICT"), nullable=False
    )
    observed_unit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    observed_line_total: Mapped[Decimal | None] = mapped_column(MONEY)
    target_quantity: Mapped[Decimal | None] = mapped_column(MONEY)
    source_quantity: Mapped[Decimal | None] = mapped_column(MONEY)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    match_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    criteria: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    ambiguity_group: Mapped[str | None] = mapped_column(SHA256)
    attribution_fingerprint: Mapped[str] = mapped_column(SHA256, nullable=False)
    source_observed_at: Mapped[Any] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class FraudRule(Base):
    __tablename__ = "fraud_rules"

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(191), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class FraudRuleVersion(Base):
    __tablename__ = "fraud_rule_versions"
    __table_args__ = (
        UniqueConstraint("fraud_rule_id", "version"),
        Index(
            "ix_fraud_rule_version_config",
            "fraud_rule_id",
            "configuration_fingerprint",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    fraud_rule_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("fraud_rules.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    implementation_version: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_fingerprint: Mapped[str] = mapped_column(SHA256, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    threshold: Mapped[Decimal | None] = mapped_column(MONEY)
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    effective_from: Mapped[Any] = mapped_column(UTCDateTime(), nullable=False)
    effective_until: Mapped[Any | None] = mapped_column(UTCDateTime())
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class FraudWhitelist(Base):
    """Documented, reversible suppression policy; captured evidence is untouched."""

    __tablename__ = "fraud_whitelists"
    __table_args__ = (
        UniqueConstraint("entry_key"),
        Index("ix_fraud_whitelists_active_window", "active", "valid_from", "valid_until"),
        Index("ix_fraud_whitelists_scope", "scope_type", "scope_value"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    entry_key: Mapped[str] = mapped_column(SHA256, nullable=False)
    fraud_rule_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("fraud_rules.id", ondelete="RESTRICT")
    )
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_value: Mapped[str] = mapped_column(String(512), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    valid_from: Mapped[Any] = mapped_column(UTCDateTime(), nullable=False)
    valid_until: Mapped[Any | None] = mapped_column(UTCDateTime())
    created_by_user_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class FraudAlert(Base):
    __tablename__ = "fraud_alerts"
    __table_args__ = (
        UniqueConstraint("fraud_rule_version_id", "transaction_id", "finding_key"),
        Index("ix_fraud_alerts_status_opened", "status", "opened_at"),
        Index("ix_fraud_alerts_severity_opened", "severity", "opened_at"),
        Index(
            "ix_fraud_alerts_operational_status_opened",
            "is_canonical",
            "status",
            "opened_at",
        ),
        Index("ix_fraud_alerts_duplicate_of", "duplicate_of_alert_id"),
        CheckConstraint(
            "(is_canonical = 1 AND duplicate_of_alert_id IS NULL "
            "AND deduplicated_at IS NULL AND deduplication_reason IS NULL) OR "
            "(is_canonical = 0 AND duplicate_of_alert_id IS NOT NULL "
            "AND deduplicated_at IS NOT NULL AND deduplication_reason IS NOT NULL)",
            name="canonical_duplicate_consistency",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    fraud_rule_version_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("fraud_rule_versions.id", ondelete="RESTRICT"), nullable=False
    )
    correlation_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("document_correlations.id", ondelete="RESTRICT")
    )
    transaction_id: Mapped[UUID] = mapped_column(UUIDBinary(), nullable=False)
    finding_key: Mapped[str] = mapped_column(SHA256, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="OPEN")
    is_canonical: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    duplicate_of_alert_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("fraud_alerts.id", ondelete="RESTRICT")
    )
    deduplicated_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    deduplication_reason: Mapped[str | None] = mapped_column(String(191))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    original_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    final_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    difference_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    difference_percent: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    confidence: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    assigned_to_user_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("users.id", ondelete="RESTRICT")
    )
    taken_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    closed_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    closure_reason: Mapped[str | None] = mapped_column(Text)
    opened_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class FraudAlertEvidence(Base):
    __tablename__ = "fraud_alert_evidence"
    __table_args__ = (Index("ix_fraud_alert_evidence_alert", "fraud_alert_id", "sequence"),)

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    fraud_alert_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("fraud_alerts.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("documents.id", ondelete="RESTRICT")
    )
    print_job_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("print_jobs.id", ondelete="RESTRICT")
    )
    raw_payload_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("raw_payloads.id", ondelete="RESTRICT")
    )
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    artifact_sha256: Mapped[str | None] = mapped_column(SHA256)
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class FraudAlertHistory(Base):
    __tablename__ = "fraud_alert_history"
    __table_args__ = (UniqueConstraint("fraud_alert_id", "sequence"),)

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    fraud_alert_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("fraud_alerts.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("users.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    previous_record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)
    record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(191), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[Any | None] = mapped_column(UTCDateTime())
    last_login_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    password_changed_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    created_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    granted_by_user_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("users.id", ondelete="RESTRICT")
    )
    granted_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        UniqueConstraint("chain_scope", "sequence"),
        Index("ix_audit_log_occurred", "occurred_at"),
        Index("ix_audit_log_resource", "resource_type", "resource_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    chain_scope: Mapped[str] = mapped_column(String(191), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("users.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(96), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(191))
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    source_ip: Mapped[str | None] = mapped_column(String(45))
    occurred_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    previous_record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)
    record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)


class SystemEvent(Base):
    __tablename__ = "system_events"
    __table_args__ = (
        Index("ix_system_events_service_occurred", "service", "occurred_at"),
        Index("ix_system_events_device_occurred", "device_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    service: Mapped[str] = mapped_column(String(96), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    device_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("devices.id", ondelete="RESTRICT")
    )
    session_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("proxy_sessions.id", ondelete="RESTRICT")
    )
    job_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("print_jobs.id", ondelete="RESTRICT")
    )
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    stack_trace: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (Index("ix_import_batches_status_started", "status", "started_at"),)

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_instance: Mapped[str] = mapped_column(String(128), nullable=False)
    source_root: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by_user_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("users.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    ended_at: Mapped[Any | None] = mapped_column(UTCDateTime())
    scanned_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    imported_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ImportItem(Base):
    __tablename__ = "import_items"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "source_instance",
            "source_scope",
            "source_job_id",
            "artifact_role",
        ),
        UniqueConstraint("source_instance", "source_scope", "source_event_id"),
        Index("ix_import_items_batch_status", "import_batch_id", "status"),
        Index("ix_import_items_sha256", "sha256"),
    )

    id: Mapped[UUID] = mapped_column(UUIDBinary(), primary_key=True, default=uuid4)
    import_batch_id: Mapped[UUID] = mapped_column(
        UUIDBinary(), ForeignKey("import_batches.id", ondelete="RESTRICT"), nullable=False
    )
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_instance: Mapped[str] = mapped_column(String(128), nullable=False)
    source_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    source_job_id: Mapped[str] = mapped_column(String(191), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(191))
    artifact_role: Mapped[str] = mapped_column(String(64), nullable=False)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(SHA256, nullable=False)
    sha256: Mapped[str] = mapped_column(SHA256, nullable=False)
    byte_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    imported_job_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("print_jobs.id", ondelete="RESTRICT")
    )
    imported_document_id: Mapped[UUID | None] = mapped_column(
        UUIDBinary(), ForeignKey("documents.id", ondelete="RESTRICT")
    )
    attempted_at: Mapped[Any] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    imported_at: Mapped[Any | None] = mapped_column(UTCDateTime())


class HashChainHead(Base):
    """Serialisation point for append-only chains.

    Writers lock one row with ``SELECT ... FOR UPDATE``, increment ``sequence``
    and then append the corresponding immutable record in the same transaction.
    """

    __tablename__ = "hash_chain_heads"

    scope: Mapped[str] = mapped_column(String(191), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    record_hash: Mapped[str] = mapped_column(SHA256, nullable=False)
    updated_at: Mapped[Any] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class AnalysisWatermark(Base):
    """Durable cursor for bounded, restart-safe derived-data workers."""

    __tablename__ = "analysis_watermarks"

    service: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor_timestamp: Mapped[Any] = mapped_column(UTCDateTime(), nullable=False)
    cursor_id: Mapped[UUID] = mapped_column(UUIDBinary(), nullable=False)
    processed_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[Any] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


# Apply the same physical storage contract to every MariaDB table, including
# tables that define their own constraints and indexes above.
for _table in Base.metadata.tables.values():
    _table.dialect_options["mysql"]["engine"] = "InnoDB"
    _table.dialect_options["mysql"]["charset"] = "utf8mb4"
    _table.dialect_options["mysql"]["collate"] = "utf8mb4_unicode_ci"


__all__ = [
    "ActiveParserVersion",
    "AnalysisWatermark",
    "AuditLog",
    "Device",
    "DeviceStatus",
    "Document",
    "DocumentCorrelation",
    "DocumentCorrelationMember",
    "DocumentLine",
    "DocumentVersion",
    "FraudAlert",
    "FraudAlertEvidence",
    "FraudAlertHistory",
    "FraudRule",
    "FraudRuleVersion",
    "FraudWhitelist",
    "HashChainHead",
    "ImportBatch",
    "ImportItem",
    "LinePriceAttribution",
    "Order",
    "OrderEvent",
    "OrderSnapshot",
    "ParserVersion",
    "Payment",
    "PrintJob",
    "ProxySession",
    "RawPayload",
    "Role",
    "StreamChunk",
    "SystemEvent",
    "User",
    "UserRole",
]
