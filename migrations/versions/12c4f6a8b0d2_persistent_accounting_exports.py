"""Add persistent, non-destructive accounting PDF/ZIP exports.

Revision ID: 12c4f6a8b0d2
Revises: a4d8c2e6f1b3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from retailprintguard.db.types import UTCDateTime, UUIDBinary

revision: str = "12c4f6a8b0d2"
down_revision: str | None = "a4d8c2e6f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STORAGE = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_unicode_ci")


def upgrade() -> None:
    op.create_table(
        "export_requests",
        sa.Column("id", UUIDBinary(), primary_key=True),
        sa.Column(
            "requested_by_user_id",
            UUIDBinary(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("device_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("started_at", UTCDateTime()),
        sa.Column("completed_at", UTCDateTime()),
        sa.Column("total_documents", sa.Integer(), nullable=False),
        sa.Column("completed_documents", sa.Integer(), nullable=False),
        sa.Column("archive_bytes", sa.BigInteger()),
        sa.Column("archive_sha256", sa.String(64)),
        sa.Column("renderer_version", sa.String(64), nullable=False),
        sa.Column("error", sa.String(1024)),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name=op.f("ck_export_requests_export_status"),
        ),
        **_STORAGE,
    )
    op.create_index(
        "ix_export_requests_status_created", "export_requests", ["status", "created_at"]
    )
    op.create_index("ix_export_requests_created", "export_requests", ["created_at", "id"])
    op.create_table(
        "export_items",
        sa.Column(
            "export_id",
            UUIDBinary(),
            sa.ForeignKey("export_requests.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column(
            "document_id",
            UUIDBinary(),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            UUIDBinary(),
            sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("date_basis", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "export_id", "document_id", name="uq_export_items_export_id_document_id"
        ),
        **_STORAGE,
    )


def downgrade() -> None:
    # Permit schema tests and empty fresh installations to roll back, but never
    # silently delete already-created accounting exports or request history.
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM export_requests")):
        raise RuntimeError("Export audit history exists; downgrade would discard evidence")
    op.drop_table("export_items")
    op.drop_table("export_requests")
