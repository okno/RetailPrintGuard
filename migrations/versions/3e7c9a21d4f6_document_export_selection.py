"""Record document export selection and permit unbounded history requests.

Revision ID: 3e7c9a21d4f6
Revises: 12c4f6a8b0d2

Existing requests, frozen snapshots and published ZIPs remain unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3e7c9a21d4f6"
down_revision: str | None = "12c4f6a8b0d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("export_requests") as batch:
        batch.alter_column("date_from", existing_type=sa.Date(), nullable=True)
        batch.alter_column("date_to", existing_type=sa.Date(), nullable=True)
        batch.add_column(sa.Column("selection_metadata", sa.JSON(none_as_null=True)))


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text(
            "SELECT COUNT(*) FROM export_requests WHERE selection_metadata IS NOT NULL "
            "OR date_from IS NULL OR date_to IS NULL"
        )
    )
    if count:
        raise RuntimeError("Export selection history exists; downgrade would discard evidence")
    with op.batch_alter_table("export_requests") as batch:
        batch.drop_column("selection_metadata")
        batch.alter_column("date_from", existing_type=sa.Date(), nullable=False)
        batch.alter_column("date_to", existing_type=sa.Date(), nullable=False)
