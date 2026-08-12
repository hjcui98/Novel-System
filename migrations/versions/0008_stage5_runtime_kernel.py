"""Add Stage 5 isolated runtime projections.

Revision ID: 0008_stage5_runtime_kernel
Revises: 0007_stage2r_r1_query_metadata
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_stage5_runtime_kernel"
down_revision: str | None = "0007_stage2r_r1_query_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_task_projection",
        sa.Column("task_id", sa.String(length=128), primary_key=True),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("current_attempt_id", sa.String(length=128), nullable=True),
        sa.Column("basis_commit", sa.String(length=71), nullable=False),
        sa.Column("basis_snapshot", sa.String(length=128), nullable=True),
        sa.Column("policy_hash", sa.String(length=71), nullable=False),
        sa.Column("permission_hash", sa.String(length=71), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("task_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["run_stream.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["project.project_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_runtime_task_projection_run_id", "runtime_task_projection", ["run_id"])
    op.create_index(
        "ix_runtime_task_claim",
        "runtime_task_projection",
        ["project_id", "status", "priority", "scheduled_for"],
    )
    op.create_table(
        "runtime_task_attempt",
        sa.Column("attempt_id", sa.String(length=128), primary_key=True),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("claim_digest", sa.String(length=71), nullable=False),
        sa.Column("fence_generation", sa.Integer(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("failure_class", sa.String(length=64), nullable=True),
        sa.Column("attempt_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["runtime_task_projection.task_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("task_id", "attempt_no"),
    )
    op.create_index("ix_runtime_task_attempt_task_id", "runtime_task_attempt", ["task_id"])
    op.create_table(
        "runtime_effect_projection",
        sa.Column("effect_identity", sa.String(length=128), primary_key=True),
        sa.Column("request_identity", sa.String(length=128), nullable=False, unique=True),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_request_id", sa.String(length=256), nullable=True),
        sa.Column("result_ref_json", sa.JSON(), nullable=True),
        sa.Column("effect_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["runtime_task_projection.task_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["runtime_task_attempt.attempt_id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_runtime_effect_projection_run_id", "runtime_effect_projection", ["run_id"])
    op.create_table(
        "project_writer_claim",
        sa.Column("project_id", sa.String(length=128), primary_key=True),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_id", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.project_id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("project_writer_claim")
    op.drop_index("ix_runtime_effect_projection_run_id", table_name="runtime_effect_projection")
    op.drop_table("runtime_effect_projection")
    op.drop_index("ix_runtime_task_attempt_task_id", table_name="runtime_task_attempt")
    op.drop_table("runtime_task_attempt")
    op.drop_index("ix_runtime_task_claim", table_name="runtime_task_projection")
    op.drop_index("ix_runtime_task_projection_run_id", table_name="runtime_task_projection")
    op.drop_table("runtime_task_projection")
