"""Add versioned Stage 1 R1 relational materialization.

Revision ID: 0003_r1_materialized_view
Revises: 0002_projection_outbox
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_r1_materialized_view"
down_revision: str | None = "0002_projection_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projection_outbox",
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "projection_outbox",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "r1_record",
        sa.Column("row_id", sa.String(length=180), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("source_commit", sa.String(length=71), nullable=False),
        sa.Column("record_kind", sa.String(length=32), nullable=False),
        sa.Column("record_id", sa.String(length=128), nullable=False),
        sa.Column("predicate", sa.String(length=128), nullable=True),
        sa.Column("valid_start", sa.Integer(), nullable=True),
        sa.Column("valid_end", sa.Integer(), nullable=True),
        sa.Column("truth_class", sa.String(length=32), nullable=True),
        sa.Column("record_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_commit"], ["project_commit.commit_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("row_id"),
        sa.UniqueConstraint("source_commit", "record_kind", "record_id"),
    )
    op.create_index(op.f("ix_r1_record_project_id"), "r1_record", ["project_id"])
    op.create_index(op.f("ix_r1_record_source_commit"), "r1_record", ["source_commit"])
    op.create_table(
        "r1_record_entity",
        sa.Column("association_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("row_id", sa.String(length=180), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["row_id"], ["r1_record.row_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("association_id"),
        sa.UniqueConstraint("row_id", "entity_id", "role"),
    )
    op.create_index(op.f("ix_r1_record_entity_row_id"), "r1_record_entity", ["row_id"])
    op.create_index(op.f("ix_r1_record_entity_entity_id"), "r1_record_entity", ["entity_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_r1_record_entity_entity_id"), table_name="r1_record_entity")
    op.drop_index(op.f("ix_r1_record_entity_row_id"), table_name="r1_record_entity")
    op.drop_table("r1_record_entity")
    op.drop_index(op.f("ix_r1_record_source_commit"), table_name="r1_record")
    op.drop_index(op.f("ix_r1_record_project_id"), table_name="r1_record")
    op.drop_table("r1_record")
    op.drop_column("projection_outbox", "lease_expires_at")
    op.drop_column("projection_outbox", "claimed_by")
