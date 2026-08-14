"""add device metadata and preserve historical alert duplicates

Revision ID: 8d4c2a91f7b0
Revises: 29517f373309
Create Date: 2026-08-14 03:30:00.000000

The original ORDER_WITHOUT_FISCAL_CLOSE implementation included a volatile
evaluation timestamp in its finding fingerprint.  Repeated worker polls could
therefore create multiple alerts for the same rule version and transaction.
This migration never deletes or rewrites that evidence.  It retains the first
alert as the operational/canonical record and links every later record to it,
with an explicit reason and migration timestamp.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import context, op

import retailprintguard.db.types

revision: str = "8d4c2a91f7b0"
down_revision: str | Sequence[str] | None = "29517f373309"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AFFECTED_RULE = "ORDER_WITHOUT_FISCAL_CLOSE"
_REASON = "historical duplicate: volatile finding fingerprint before revision 8d4c2a91f7b0"


def _mark_historical_duplicates() -> None:
    """Link, rather than delete, known historical duplicates.

    The data step intentionally uses SQLAlchemy's portable expression layer so
    both MariaDB production migrations and SQLite migration tests bind compact
    UUID values through the same UUIDBinary type.
    """

    if context.is_offline_mode():
        # Offline SQL cannot inspect existing rows to choose the first alert.
        # Production installation and supported recovery procedures run Alembic
        # online; schema-only SQL generation therefore leaves legacy rows
        # canonical until an online migration is performed.
        return

    connection = op.get_bind()
    uuid_type = retailprintguard.db.types.UUIDBinary()
    alert = sa.table(
        "fraud_alerts",
        sa.column("id", uuid_type),
        sa.column("fraud_rule_version_id", uuid_type),
        sa.column("transaction_id", uuid_type),
        sa.column("opened_at", retailprintguard.db.types.UTCDateTime()),
        sa.column("is_canonical", sa.Boolean()),
        sa.column("duplicate_of_alert_id", uuid_type),
        sa.column("deduplicated_at", retailprintguard.db.types.UTCDateTime()),
        sa.column("deduplication_reason", sa.String(191)),
    )
    rule_version = sa.table(
        "fraud_rule_versions",
        sa.column("id", uuid_type),
        sa.column("fraud_rule_id", uuid_type),
    )
    rule = sa.table(
        "fraud_rules",
        sa.column("id", uuid_type),
        sa.column("code", sa.String(96)),
    )

    rows = connection.execute(
        sa.select(
            alert.c.id,
            alert.c.fraud_rule_version_id,
            alert.c.transaction_id,
        )
        .select_from(
            alert.join(
                rule_version,
                alert.c.fraud_rule_version_id == rule_version.c.id,
            ).join(rule, rule_version.c.fraud_rule_id == rule.c.id)
        )
        .where(rule.c.code == _AFFECTED_RULE)
        .order_by(
            alert.c.fraud_rule_version_id,
            alert.c.transaction_id,
            alert.c.opened_at,
            alert.c.id,
        )
    ).all()

    groups: dict[tuple[Any, Any], list[Any]] = defaultdict(list)
    for row in rows:
        groups[(row.fraud_rule_version_id, row.transaction_id)].append(row.id)

    migration_time = datetime.now(UTC)
    updates: list[dict[str, Any]] = []
    for alert_ids in groups.values():
        if len(alert_ids) < 2:
            continue
        canonical_id = alert_ids[0]
        updates.extend(
            {
                "_alert_id": duplicate_id,
                "_canonical_id": canonical_id,
                "_deduplicated_at": migration_time,
                "_reason": _REASON,
            }
            for duplicate_id in alert_ids[1:]
        )

    statement = (
        alert.update()
        .where(
            alert.c.id
            == sa.bindparam(
                "_alert_id",
                type_=retailprintguard.db.types.UUIDBinary(),
            )
        )
        .values(
            is_canonical=False,
            duplicate_of_alert_id=sa.bindparam(
                "_canonical_id",
                type_=retailprintguard.db.types.UUIDBinary(),
            ),
            deduplicated_at=sa.bindparam(
                "_deduplicated_at",
                type_=retailprintguard.db.types.UTCDateTime(),
            ),
            deduplication_reason=sa.bindparam("_reason", type_=sa.String(191)),
        )
    )
    for offset in range(0, len(updates), 1_000):
        connection.execute(statement, updates[offset : offset + 1_000])


def upgrade() -> None:
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("mac_address", sa.String(length=17), nullable=True))
        batch_op.add_column(sa.Column("department", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("role", sa.String(length=64), nullable=True))

    with op.batch_alter_table("fraud_alerts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_canonical",
                sa.Boolean(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "duplicate_of_alert_id",
                retailprintguard.db.types.UUIDBinary(length=16),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "deduplicated_at",
                retailprintguard.db.types.UTCDateTime(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("deduplication_reason", sa.String(length=191), nullable=True)
        )
        batch_op.create_foreign_key(
            op.f("fk_fraud_alerts_duplicate_of_alert_id_fraud_alerts"),
            "fraud_alerts",
            ["duplicate_of_alert_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            op.f("ck_fraud_alerts_canonical_duplicate_consistency"),
            "(is_canonical = 1 AND duplicate_of_alert_id IS NULL "
            "AND deduplicated_at IS NULL AND deduplication_reason IS NULL) OR "
            "(is_canonical = 0 AND duplicate_of_alert_id IS NOT NULL "
            "AND deduplicated_at IS NOT NULL AND deduplication_reason IS NOT NULL)",
        )
        batch_op.create_index(
            "ix_fraud_alerts_duplicate_of",
            ["duplicate_of_alert_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_fraud_alerts_operational_status_opened",
            ["is_canonical", "status", "opened_at"],
            unique=False,
        )

    _mark_historical_duplicates()


def downgrade() -> None:
    with op.batch_alter_table("fraud_alerts", schema=None) as batch_op:
        batch_op.drop_index("ix_fraud_alerts_operational_status_opened")
        batch_op.drop_index("ix_fraud_alerts_duplicate_of")
        batch_op.drop_constraint(
            op.f("ck_fraud_alerts_canonical_duplicate_consistency"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("fk_fraud_alerts_duplicate_of_alert_id_fraud_alerts"),
            type_="foreignkey",
        )
        batch_op.drop_column("deduplication_reason")
        batch_op.drop_column("deduplicated_at")
        batch_op.drop_column("duplicate_of_alert_id")
        batch_op.drop_column("is_canonical")

    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.drop_column("role")
        batch_op.drop_column("department")
        batch_op.drop_column("mac_address")
