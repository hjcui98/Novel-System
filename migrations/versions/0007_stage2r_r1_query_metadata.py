"""Add Stage 2R temporal, narrative, and access fields to versioned R1 records.

Revision ID: 0007_stage2r_r1_query_metadata
Revises: 0006_stage2r_embedding_cache
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_stage2r_r1_query_metadata"
down_revision: str | None = "0006_stage2r_embedding_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("r1_record", sa.Column("worldline", sa.String(length=128), nullable=True))
    op.add_column("r1_record", sa.Column("narrative_start", sa.Integer(), nullable=True))
    op.add_column("r1_record", sa.Column("narrative_end", sa.Integer(), nullable=True))
    op.add_column(
        "r1_record",
        sa.Column(
            "access_scope",
            sa.String(length=64),
            nullable=False,
            server_default="writer_safe",
        ),
    )
    op.create_index(
        "ix_r1_record_basis_predicate_time",
        "r1_record",
        ["source_commit", "record_kind", "predicate", "valid_start", "valid_end"],
    )
    op.create_index(
        "ix_r1_record_basis_narrative",
        "r1_record",
        ["source_commit", "narrative_start", "narrative_end"],
    )


def downgrade() -> None:
    op.drop_index("ix_r1_record_basis_narrative", table_name="r1_record")
    op.drop_index("ix_r1_record_basis_predicate_time", table_name="r1_record")
    op.drop_column("r1_record", "access_scope")
    op.drop_column("r1_record", "narrative_end")
    op.drop_column("r1_record", "narrative_start")
    op.drop_column("r1_record", "worldline")
