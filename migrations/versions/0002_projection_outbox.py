"""Add Stage 1 derived projection outbox and snapshot tables.

Revision ID: 0002_projection_outbox
Revises: 0001_stage0_core
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_projection_outbox"
down_revision: str | None = "0001_stage0_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projection_outbox",
        sa.Column("outbox_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("source_commit", sa.String(length=71), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.String(length=2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_commit"], ["project_commit.commit_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("outbox_id"),
        sa.UniqueConstraint("source_commit"),
    )
    op.create_index(
        op.f("ix_projection_outbox_project_id"),
        "projection_outbox",
        ["project_id"],
        unique=False,
    )
    op.create_table(
        "derived_snapshot",
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("source_commit", sa.String(length=71), nullable=False),
        sa.Column("build_status", sa.String(length=32), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["project.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_commit"], ["project_commit.commit_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint("source_commit"),
    )
    op.create_index(
        op.f("ix_derived_snapshot_project_id"),
        "derived_snapshot",
        ["project_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_derived_snapshot_project_id"), table_name="derived_snapshot")
    op.drop_table("derived_snapshot")
    op.drop_index(op.f("ix_projection_outbox_project_id"), table_name="projection_outbox")
    op.drop_table("projection_outbox")
