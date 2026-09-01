"""Persist provider-call sent/raw evidence for U3 recovery."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_model_call_ledger"
down_revision: str | None = "0009_stage5_attempt_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_call_ledger",
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_id", sa.String(length=128), nullable=True),
        sa.Column("request_hash", sa.String(length=71), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("logical_phase", sa.String(length=256), nullable=False),
        sa.Column("effective_budget_json", sa.JSON(), nullable=False),
        sa.Column("reasoning_included_in_completion_tokens", sa.Boolean(), nullable=False),
        sa.Column("provider_request_id", sa.String(length=256), nullable=True),
        sa.Column("provider_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_response_hash", sa.String(length=71), nullable=True),
        sa.Column("raw_artifact_json", sa.JSON(), nullable=True),
        sa.Column("call_record_json", sa.JSON(), nullable=True),
        sa.Column("validation_error", sa.String(length=4096), nullable=True),
        sa.Column("transport_error_type", sa.String(length=240), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index("ix_model_call_ledger_run_id", "model_call_ledger", ["run_id"])
    op.create_index("ix_model_call_ledger_task_id", "model_call_ledger", ["task_id"])
    op.create_index("ix_model_call_ledger_attempt_id", "model_call_ledger", ["attempt_id"])


def downgrade() -> None:
    op.drop_index("ix_model_call_ledger_attempt_id", table_name="model_call_ledger")
    op.drop_index("ix_model_call_ledger_task_id", table_name="model_call_ledger")
    op.drop_index("ix_model_call_ledger_run_id", table_name="model_call_ledger")
    op.drop_table("model_call_ledger")
