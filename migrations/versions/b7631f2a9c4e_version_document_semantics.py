"""version document semantics and POS course attribution

Revision ID: b7631f2a9c4e
Revises: 8d4c2a91f7b0
Create Date: 2026-08-14 14:30:00.000000

Parser-derived identity fields must travel with each immutable document
version.  ``documents`` remains a current read projection for legacy API
consumers.  Existing rows are backfilled from that projection without
changing, deleting, or renumbering evidence versions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import retailprintguard.db.types

revision: str = "b7631f2a9c4e"
down_revision: str | Sequence[str] | None = "8d4c2a91f7b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_version_semantics() -> None:
    """Emit one portable correlated UPDATE for online and offline upgrades."""

    documents = sa.table(
        "documents",
        sa.column("id", retailprintguard.db.types.UUIDBinary()),
        sa.column("document_type", sa.String(48)),
        sa.column("subtype", sa.String(128)),
        sa.column("external_document_code", sa.String(128)),
        sa.column("order_code", sa.String(128)),
        sa.column("table_code", sa.String(128)),
        sa.column("operator_code", sa.String(128)),
        sa.column("terminal_code", sa.String(128)),
        sa.column("document_timestamp", retailprintguard.db.types.UTCDateTime()),
    )
    versions = sa.table(
        "document_versions",
        sa.column("document_id", retailprintguard.db.types.UUIDBinary()),
        sa.column("document_type", sa.String(48)),
        sa.column("subtype", sa.String(128)),
        sa.column("external_document_code", sa.String(128)),
        sa.column("order_code", sa.String(128)),
        sa.column("table_code", sa.String(128)),
        sa.column("operator_code", sa.String(128)),
        sa.column("terminal_code", sa.String(128)),
        sa.column("document_timestamp", retailprintguard.db.types.UTCDateTime()),
    )

    def from_document(column: sa.ColumnElement[object]) -> sa.ScalarSelect[object]:
        return (
            sa.select(column)
            .where(documents.c.id == versions.c.document_id)
            .correlate(versions)
            .scalar_subquery()
        )

    op.execute(
        versions.update().values(
            document_type=from_document(documents.c.document_type),
            subtype=from_document(documents.c.subtype),
            external_document_code=from_document(documents.c.external_document_code),
            order_code=from_document(documents.c.order_code),
            table_code=from_document(documents.c.table_code),
            operator_code=from_document(documents.c.operator_code),
            terminal_code=from_document(documents.c.terminal_code),
            document_timestamp=from_document(documents.c.document_timestamp),
        )
    )


def upgrade() -> None:
    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("document_type", sa.String(length=48), nullable=True))
        batch_op.add_column(sa.Column("subtype", sa.String(length=128), nullable=True))
        batch_op.add_column(
            sa.Column("external_document_code", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(sa.Column("order_code", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("table_code", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("operator_code", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("terminal_code", sa.String(length=128), nullable=True))
        batch_op.add_column(
            sa.Column(
                "document_timestamp",
                retailprintguard.db.types.UTCDateTime(),
                nullable=True,
            )
        )

    _backfill_version_semantics()

    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_document_versions_order_document",
            ["order_code", "document_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_document_versions_external_document",
            ["external_document_code", "document_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_document_versions_table_document",
            ["table_code", "document_id"],
            unique=False,
        )

    with op.batch_alter_table("document_lines", schema=None) as batch_op:
        batch_op.add_column(sa.Column("course_code", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("document_lines", schema=None) as batch_op:
        batch_op.drop_column("course_code")

    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.drop_index("ix_document_versions_table_document")
        batch_op.drop_index("ix_document_versions_external_document")
        batch_op.drop_index("ix_document_versions_order_document")
        batch_op.drop_column("document_timestamp")
        batch_op.drop_column("terminal_code")
        batch_op.drop_column("operator_code")
        batch_op.drop_column("table_code")
        batch_op.drop_column("order_code")
        batch_op.drop_column("external_document_code")
        batch_op.drop_column("subtype")
        batch_op.drop_column("document_type")
