"""add skipped status and feed timestamp to ingestion runs

Revision ID: 531276c5463b
Revises: 9b30bba10c0e
Create Date: 2026-11-06

Hand-written: Alembic autogenerate does not detect CHECK constraint changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "531276c5463b"
down_revision: str | None = "9b30bba10c0e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingestion_runs", sa.Column("feed_timestamp", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_constraint("ck_ingestion_runs_status", "ingestion_runs", type_="check")
    op.create_check_constraint(
        "ck_ingestion_runs_status",
        "ingestion_runs",
        "status IN ('running', 'success', 'failed', 'skipped')",
    )


def downgrade() -> None:
    # Skipped runs never wrote positions, so deleting them breaks no foreign keys.
    op.execute("DELETE FROM ingestion_runs WHERE status = 'skipped'")
    op.drop_constraint("ck_ingestion_runs_status", "ingestion_runs", type_="check")
    op.create_check_constraint(
        "ck_ingestion_runs_status",
        "ingestion_runs",
        "status IN ('running', 'success', 'failed')",
    )
    op.drop_column("ingestion_runs", "feed_timestamp")
