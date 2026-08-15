"""add append-only line price attribution provenance

Revision ID: c4e8d2f1a6b9
Revises: b7631f2a9c4e
Create Date: 2026-08-15 10:30:00.000000

Prices inferred for POS lines remain separate from immutable parsed evidence.
Every attribution points to the exact target and monetary source lines, their
document versions, and the correlation episode in which the match was made.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import retailprintguard.db.types

revision: str = "c4e8d2f1a6b9"
down_revision: str | Sequence[str] | None = "b7631f2a9c4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "line_price_attributions",
        sa.Column(
            "id",
            retailprintguard.db.types.UUIDBinary(length=16),
            nullable=False,
        ),
        sa.Column(
            "correlation_id",
            retailprintguard.db.types.UUIDBinary(length=16),
            nullable=False,
        ),
        sa.Column(
            "target_document_id",
            retailprintguard.db.types.UUIDBinary(length=16),
            nullable=False,
        ),
        sa.Column(
            "target_document_version_id",
            retailprintguard.db.types.UUIDBinary(length=16),
            nullable=False,
        ),
        sa.Column(
            "target_line_id",
            retailprintguard.db.types.UUIDBinary(length=16),
            nullable=False,
        ),
        sa.Column(
            "source_document_id",
            retailprintguard.db.types.UUIDBinary(length=16),
            nullable=False,
        ),
        sa.Column(
            "source_document_version_id",
            retailprintguard.db.types.UUIDBinary(length=16),
            nullable=False,
        ),
        sa.Column(
            "source_line_id",
            retailprintguard.db.types.UUIDBinary(length=16),
            nullable=False,
        ),
        sa.Column("observed_unit_price", sa.Numeric(precision=19, scale=4), nullable=True),
        sa.Column("observed_line_total", sa.Numeric(precision=19, scale=4), nullable=True),
        sa.Column("target_quantity", sa.Numeric(precision=19, scale=4), nullable=True),
        sa.Column("source_quantity", sa.Numeric(precision=19, scale=4), nullable=True),
        sa.Column("source_kind", sa.String(length=24), nullable=False),
        sa.Column("match_basis", sa.String(length=32), nullable=False),
        sa.Column("algorithm_version", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("ambiguity_group", sa.String(length=64), nullable=True),
        sa.Column("attribution_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "source_observed_at",
            retailprintguard.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            retailprintguard.db.types.UTCDateTime(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_line_price_attributions_line_price_attr_confidence"),
        ),
        sa.CheckConstraint(
            "observed_unit_price IS NOT NULL OR observed_line_total IS NOT NULL",
            name=op.f("ck_line_price_attributions_line_price_attr_has_amount"),
        ),
        sa.CheckConstraint(
            "source_kind IN ('PREBILL', 'MANAGEMENT', 'FISCAL')",
            name=op.f("ck_line_price_attributions_line_price_attr_source_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('RESOLVED', 'AGREED', 'AMBIGUOUS')",
            name=op.f("ck_line_price_attributions_line_price_attr_status"),
        ),
        sa.ForeignKeyConstraint(
            ["correlation_id"],
            ["document_correlations.id"],
            name=op.f(
                "fk_line_price_attributions_correlation_id_document_correlations"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["documents.id"],
            name=op.f("fk_line_price_attributions_source_document_id_documents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_version_id"],
            ["document_versions.id"],
            name=op.f(
                "fk_line_price_attributions_source_document_version_id_document_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_line_id"],
            ["document_lines.id"],
            name=op.f("fk_line_price_attributions_source_line_id_document_lines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_document_id"],
            ["documents.id"],
            name=op.f("fk_line_price_attributions_target_document_id_documents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_document_version_id"],
            ["document_versions.id"],
            name=op.f(
                "fk_line_price_attributions_target_document_version_id_document_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_line_id"],
            ["document_lines.id"],
            name=op.f("fk_line_price_attributions_target_line_id_document_lines"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_line_price_attributions")),
        sa.UniqueConstraint(
            "attribution_fingerprint",
            name="uq_line_price_attr_fingerprint",
        ),
        sa.UniqueConstraint(
            "correlation_id",
            "target_line_id",
            "source_line_id",
            "algorithm_version",
            name="uq_line_price_attr_identity",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_line_price_attr_correlation_status",
        "line_price_attributions",
        ["correlation_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_line_price_attr_source_document",
        "line_price_attributions",
        ["source_document_id", "source_document_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_line_price_attr_target_created",
        "line_price_attributions",
        ["target_line_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_line_price_attr_target_created",
        table_name="line_price_attributions",
    )
    op.drop_index(
        "ix_line_price_attr_source_document",
        table_name="line_price_attributions",
    )
    op.drop_index(
        "ix_line_price_attr_correlation_status",
        table_name="line_price_attributions",
    )
    op.drop_table("line_price_attributions")
