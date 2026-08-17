"""Preserve observed suffixes and references separately from document identity.

Revision ID: e8a1c5d3f7b2
Revises: d7f9a3b2c1e4
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8a1c5d3f7b2"
down_revision: str | None = "d7f9a3b2c1e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("external_document_code_suffix", sa.String(length=16), nullable=True)
        )
        batch_op.add_column(
            sa.Column("commercial_reference_code", sa.String(length=128), nullable=True)
        )
        batch_op.create_index(
            "ix_documents_external_code_suffix",
            ["external_document_code_suffix"],
            unique=False,
        )
        batch_op.create_index(
            "ix_documents_commercial_reference",
            ["commercial_reference_code"],
            unique=False,
        )

    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("external_document_code_suffix", sa.String(length=16), nullable=True)
        )
        batch_op.add_column(
            sa.Column("commercial_reference_code", sa.String(length=128), nullable=True)
        )
        batch_op.create_index(
            "ix_document_versions_external_suffix",
            ["external_document_code_suffix", "document_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_document_versions_commercial_reference",
            ["commercial_reference_code", "document_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.drop_index("ix_document_versions_commercial_reference")
        batch_op.drop_index("ix_document_versions_external_suffix")
        batch_op.drop_column("commercial_reference_code")
        batch_op.drop_column("external_document_code_suffix")

    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_index("ix_documents_commercial_reference")
        batch_op.drop_index("ix_documents_external_code_suffix")
        batch_op.drop_column("commercial_reference_code")
        batch_op.drop_column("external_document_code_suffix")
