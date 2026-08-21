"""Add versioned receipt-header evidence to parsed documents.

Revision ID: a4d8c2e6f1b3
Revises: f2a6b4c8d1e3
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4d8c2e6f1b3"
down_revision: str | None = "f2a6b4c8d1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("receipt_header", sa.JSON(), nullable=True))

    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("receipt_header", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.drop_column("receipt_header")

    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_column("receipt_header")
