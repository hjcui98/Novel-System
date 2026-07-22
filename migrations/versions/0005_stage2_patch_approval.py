"""Add durable Stage 2 patch approval checkpoints.

Revision ID: 0005_stage2_patch_approval
Revises: 0004_stage2_author_approval
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_stage2_patch_approval"
down_revision: str | None = "0004_stage2_author_approval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "patch_approval",
        sa.Column("approval_request_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("change_set_id", sa.String(length=128), nullable=False),
        sa.Column("base_commit", sa.String(length=71), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("decision_json", sa.JSON(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("approval_request_id"),
    )
    op.create_index(op.f("ix_patch_approval_project_id"), "patch_approval", ["project_id"])
    op.create_index(op.f("ix_patch_approval_change_set_id"), "patch_approval", ["change_set_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_patch_approval_change_set_id"), table_name="patch_approval")
    op.drop_index(op.f("ix_patch_approval_project_id"), table_name="patch_approval")
    op.drop_table("patch_approval")
