"""Record when a completed model response has been consumed by its logical request."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_model_response_consumption"
down_revision: str | None = "0010_model_call_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_call_ledger",
        sa.Column("response_consumed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_call_ledger", "response_consumed_at")
