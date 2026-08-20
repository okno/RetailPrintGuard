"""Preserve application and RCH footer identity independently.

Revision ID: f2a6b4c8d1e3
Revises: e8a1c5d3f7b2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import retailprintguard.db.types

revision: str = "f2a6b4c8d1e3"
down_revision: str | None = "e8a1c5d3f7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "application_timestamp",
                retailprintguard.db.types.UTCDateTime(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "rch_footer_timestamp",
                retailprintguard.db.types.UTCDateTime(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("rch_serial_number", sa.String(length=64), nullable=True)
        )

    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "application_timestamp",
                retailprintguard.db.types.UTCDateTime(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "rch_footer_timestamp",
                retailprintguard.db.types.UTCDateTime(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("rch_serial_number", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.drop_column("rch_serial_number")
        batch_op.drop_column("rch_footer_timestamp")
        batch_op.drop_column("application_timestamp")

    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_column("rch_serial_number")
        batch_op.drop_column("rch_footer_timestamp")
        batch_op.drop_column("application_timestamp")
