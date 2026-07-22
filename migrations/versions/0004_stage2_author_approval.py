"""Add durable Stage 2 author approval checkpoints.

Revision ID: 0004_stage2_author_approval
Revises: 0003_r1_materialized_view
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_stage2_author_approval"
down_revision: str | None = "0003_r1_materialized_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "author_approval",
        sa.Column("approval_request_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("decision_json", sa.JSON(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("approval_request_id"),
    )
    op.create_index(op.f("ix_author_approval_project_id"), "author_approval", ["project_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_author_approval_project_id"), table_name="author_approval")
    op.drop_table("author_approval")
