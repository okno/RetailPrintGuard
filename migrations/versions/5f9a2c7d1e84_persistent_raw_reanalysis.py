"""Persist whole-history RAW reanalysis membership, progress and results.

Revision ID: 5f9a2c7d1e84
Revises: 3e7c9a21d4f6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from retailprintguard.db.types import UTCDateTime, UUIDBinary

revision: str = "5f9a2c7d1e84"
down_revision: str | None = "3e7c9a21d4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
_STORAGE = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_unicode_ci")


def upgrade() -> None:
    op.create_table(
        "analysis_requests",
        sa.Column("id", UUIDBinary(), primary_key=True),
        sa.Column(
            "requested_by_user_id",
            UUIDBinary(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("active_slot", sa.Integer()),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("started_at", UTCDateTime()),
        sa.Column("completed_at", UTCDateTime()),
        *(
            sa.Column(name, sa.Integer(), nullable=False)
            for name in (
                "total_jobs",
                "completed_jobs",
                "failed_jobs",
                "total_documents",
                "completed_documents",
            )
        ),
        sa.Column("parser_versions", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("input_metadata", sa.JSON(), nullable=False),
        sa.Column("checkpoints", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(none_as_null=True)),
        sa.Column("error", sa.String(1024)),
        sa.UniqueConstraint("active_slot", name="uq_analysis_requests_active_slot"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name=op.f("ck_analysis_requests_analysis_status"),
        ),
        sa.CheckConstraint(
            "(status IN ('queued','running') AND active_slot IS NOT NULL AND active_slot = 1) OR "
            "(status IN ('completed','failed') AND active_slot IS NULL)",
            name=op.f("ck_analysis_requests_analysis_active_slot"),
        ),
        **_STORAGE,
    )
    op.create_index(
        "ix_analysis_requests_status_created", "analysis_requests", ["status", "created_at"]
    )
    op.create_table(
        "analysis_inputs",
        sa.Column(
            "analysis_id",
            UUIDBinary(),
            sa.ForeignKey("analysis_requests.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "job_id",
            UUIDBinary(),
            sa.ForeignKey("print_jobs.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("outcome", sa.JSON(none_as_null=True)),
        sa.UniqueConstraint(
            "analysis_id", "sequence", name="uq_analysis_inputs_analysis_id_sequence"
        ),
        **_STORAGE,
    )
    op.create_table(
        "analysis_results",
        sa.Column(
            "analysis_id",
            UUIDBinary(),
            sa.ForeignKey("analysis_requests.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "document_id",
            UUIDBinary(),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "document_version_id",
            UUIDBinary(),
            sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "analysis_id", "sequence", name="uq_analysis_results_analysis_id_sequence"
        ),
        **_STORAGE,
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM analysis_requests")):
        raise RuntimeError("Reanalysis audit history exists; downgrade would discard evidence")
    op.drop_table("analysis_results")
    op.drop_table("analysis_inputs")
    op.drop_table("analysis_requests")
