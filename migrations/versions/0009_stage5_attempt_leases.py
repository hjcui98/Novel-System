"""Add Stage 5 worker Attempt leases.

Revision ID: 0009_stage5_attempt_leases
Revises: 0008_stage5_runtime_kernel
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_stage5_attempt_leases"
down_revision: str | None = "0008_stage5_runtime_kernel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runtime_task_attempt",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runtime_task_attempt",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "UPDATE runtime_task_attempt "
            "SET heartbeat_at = claimed_at, "
            "lease_expires_at = datetime(claimed_at, '+1 second')"
        )
        with op.batch_alter_table("runtime_task_attempt") as batch:
            batch.alter_column("heartbeat_at", nullable=False)
            batch.alter_column("lease_expires_at", nullable=False)
    else:
        op.execute(
            "UPDATE runtime_task_attempt "
            "SET heartbeat_at = claimed_at, lease_expires_at = claimed_at + INTERVAL '1 second'"
        )
        op.alter_column("runtime_task_attempt", "heartbeat_at", nullable=False)
        op.alter_column("runtime_task_attempt", "lease_expires_at", nullable=False)
    op.create_index(
        "ix_runtime_task_attempt_lease",
        "runtime_task_attempt",
        ["lease_expires_at", "ended_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_task_attempt_lease", table_name="runtime_task_attempt")
    op.drop_column("runtime_task_attempt", "lease_expires_at")
    op.drop_column("runtime_task_attempt", "heartbeat_at")
