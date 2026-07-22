"""Create Stage 0 commit, event log, and checkpoint tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_stage0_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("current_commit_id", sa.String(length=71), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("project_id"),
    )
    op.create_table(
        "project_commit",
        sa.Column("commit_id", sa.String(length=71), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("base_commit_id", sa.String(length=71), nullable=True),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.project_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("commit_id"),
    )
    op.create_index("ix_project_commit_project_id", "project_commit", ["project_id"])
    op.create_table(
        "commit_receipt",
        sa.Column("receipt_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.project_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("receipt_id"),
        sa.UniqueConstraint("project_id", "idempotency_key"),
    )
    op.create_table(
        "run_stream",
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("last_sequence_no", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "run_event",
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=True),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_identity", sa.String(length=128), nullable=False),
        sa.Column("payload_schema_version", sa.String(length=32), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("event_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["run_stream.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("run_id", "idempotency_identity"),
        sa.UniqueConstraint("run_id", "sequence_no"),
    )
    op.create_index("ix_run_event_run_id", "run_event", ["run_id"])
    op.create_table(
        "run_checkpoint",
        sa.Column("checkpoint_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("event_position", sa.Integer(), nullable=False),
        sa.Column("checkpoint_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["run_stream.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("checkpoint_id"),
        sa.UniqueConstraint("run_id", "event_position"),
    )
    op.create_index("ix_run_checkpoint_run_id", "run_checkpoint", ["run_id"])
    op.create_table(
        "evaluation_entry",
        sa.Column("evaluation_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=71), nullable=False),
        sa.Column("entry_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("evaluation_id"),
    )
    op.create_index("ix_evaluation_entry_run_id", "evaluation_entry", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_evaluation_entry_run_id", table_name="evaluation_entry")
    op.drop_table("evaluation_entry")
    op.drop_index("ix_run_checkpoint_run_id", table_name="run_checkpoint")
    op.drop_table("run_checkpoint")
    op.drop_index("ix_run_event_run_id", table_name="run_event")
    op.drop_table("run_event")
    op.drop_table("run_stream")
    op.drop_table("commit_receipt")
    op.drop_index("ix_project_commit_project_id", table_name="project_commit")
    op.drop_table("project_commit")
    op.drop_table("project")
