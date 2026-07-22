"""Add content-addressed embeddings for rebuildable Stage 2R L2 indexes.

Revision ID: 0006_stage2r_embedding_cache
Revises: 0005_stage2_patch_approval
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_stage2r_embedding_cache"
down_revision: str | None = "0005_stage2_patch_approval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "embedding_cache",
        sa.Column("cache_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("content_hash", sa.String(length=71), nullable=False),
        sa.Column("embedding_profile", sa.String(length=1024), nullable=False),
        sa.Column("input_profile", sa.String(length=128), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("vector_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("cache_id"),
        sa.UniqueConstraint("content_hash", "embedding_profile", "input_profile"),
    )
    op.create_index(op.f("ix_embedding_cache_content_hash"), "embedding_cache", ["content_hash"])


def downgrade() -> None:
    op.drop_index(op.f("ix_embedding_cache_content_hash"), table_name="embedding_cache")
    op.drop_table("embedding_cache")
