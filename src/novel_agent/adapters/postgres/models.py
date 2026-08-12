"""Stage 0 relational persistence model."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from novel_agent.adapters.postgres.database import Base


class ProjectRow(Base):
    __tablename__ = "project"

    project_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    current_commit_id: Mapped[str | None] = mapped_column(String(71), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CommitRow(Base):
    __tablename__ = "project_commit"

    commit_id: Mapped[str] = mapped_column(String(71), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    base_commit_id: Mapped[str | None] = mapped_column(String(71), nullable=True)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CommitReceiptRow(Base):
    __tablename__ = "commit_receipt"
    __table_args__ = (UniqueConstraint("project_id", "idempotency_key"),)

    receipt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.project_id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunStreamRow(Base):
    __tablename__ = "run_stream"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunEventRow(Base):
    __tablename__ = "run_event"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no"),
        UniqueConstraint("run_id", "idempotency_identity"),
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("run_stream.run_id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class RunCheckpointRow(Base):
    __tablename__ = "run_checkpoint"
    __table_args__ = (UniqueConstraint("run_id", "event_position"),)

    checkpoint_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("run_stream.run_id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_position: Mapped[int] = mapped_column(Integer, nullable=False)
    checkpoint_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeTaskProjectionRow(Base):
    __tablename__ = "runtime_task_projection"
    __table_args__ = (
        Index(
            "ix_runtime_task_claim",
            "project_id",
            "status",
            "priority",
            "scheduled_for",
        ),
    )

    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("run_stream.run_id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.project_id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    current_attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    basis_commit: Mapped[str] = mapped_column(String(71), nullable=False)
    basis_snapshot: Mapped[str | None] = mapped_column(String(128), nullable=True)
    policy_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    permission_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    task_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeTaskAttemptRow(Base):
    __tablename__ = "runtime_task_attempt"
    __table_args__ = (UniqueConstraint("task_id", "attempt_no"),)

    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("runtime_task_projection.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    claim_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    fence_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class RuntimeEffectProjectionRow(Base):
    __tablename__ = "runtime_effect_projection"
    __table_args__ = (UniqueConstraint("request_identity"),)

    effect_identity: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("runtime_task_projection.task_id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("runtime_task_attempt.attempt_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    result_ref_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    effect_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ProjectWriterClaimRow(Base):
    __tablename__ = "project_writer_claim"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.project_id", ondelete="CASCADE"), primary_key=True
    )
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EvaluationEntryRow(Base):
    __tablename__ = "evaluation_entry"

    evaluation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    config_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    entry_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectionOutboxRow(Base):
    __tablename__ = "projection_outbox"

    outbox_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_commit: Mapped[str] = mapped_column(
        ForeignKey("project_commit.commit_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DerivedSnapshotRow(Base):
    __tablename__ = "derived_snapshot"

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_commit: Mapped[str] = mapped_column(
        ForeignKey("project_commit.commit_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    build_status: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmbeddingCacheRow(Base):
    """Content-addressed, rebuildable vector cache for derived L2 indexes."""

    __tablename__ = "embedding_cache"
    __table_args__ = (UniqueConstraint("content_hash", "embedding_profile", "input_profile"),)

    cache_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_hash: Mapped[str] = mapped_column(String(71), nullable=False, index=True)
    embedding_profile: Mapped[str] = mapped_column(String(1024), nullable=False)
    input_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_json: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthorApprovalRow(Base):
    __tablename__ = "author_approval"

    approval_request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PatchApprovalRow(Base):
    __tablename__ = "patch_approval"

    approval_request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    change_set_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    base_commit: Mapped[str] = mapped_column(String(71), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class R1RecordRow(Base):
    __tablename__ = "r1_record"
    __table_args__ = (UniqueConstraint("source_commit", "record_kind", "record_id"),)

    row_id: Mapped[str] = mapped_column(String(180), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_commit: Mapped[str] = mapped_column(
        ForeignKey("project_commit.commit_id", ondelete="CASCADE"), nullable=False, index=True
    )
    record_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    predicate: Mapped[str | None] = mapped_column(String(128), nullable=True)
    valid_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worldline: Mapped[str | None] = mapped_column(String(128), nullable=True)
    narrative_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    narrative_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    access_scope: Mapped[str] = mapped_column(String(64), nullable=False, default="writer_safe")
    truth_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class R1RecordEntityRow(Base):
    __tablename__ = "r1_record_entity"
    __table_args__ = (UniqueConstraint("row_id", "entity_id", "role"),)

    association_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    row_id: Mapped[str] = mapped_column(
        ForeignKey("r1_record.row_id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
