"""Add audited review projection for incomplete jobs.

Revision ID: d7f9a3b2c1e4
Revises: c4e8d2f1a6b9
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from retailprintguard.db.types import UTCDateTime, UUIDBinary

revision: str = "d7f9a3b2c1e4"
down_revision: str | None = "c4e8d2f1a6b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("print_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "review_state",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ))
        batch_op.add_column(sa.Column(
            "analysis_excluded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ))
        batch_op.add_column(sa.Column("reviewed_at", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("reviewed_by_user_id", UUIDBinary(), nullable=True))
        batch_op.add_column(sa.Column("review_reason", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_print_jobs_reviewed_by_user_id_users"),
            "users",
            ["reviewed_by_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            op.f("ck_print_jobs_review_state"),
            "review_state IN ('PENDING', 'VERIFIED_USABLE', 'EXCLUDED')",
        )
        batch_op.create_index(
            "ix_print_jobs_review_state",
            ["review_state", "captured_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("print_jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_print_jobs_review_state")
        batch_op.drop_constraint(op.f("ck_print_jobs_review_state"), type_="check")
        batch_op.drop_constraint(
            op.f("fk_print_jobs_reviewed_by_user_id_users"),
            type_="foreignkey",
        )
        batch_op.drop_column("review_reason")
        batch_op.drop_column("reviewed_by_user_id")
        batch_op.drop_column("reviewed_at")
        batch_op.drop_column("analysis_excluded")
        batch_op.drop_column("review_state")
