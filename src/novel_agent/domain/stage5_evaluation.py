"""Formal evidence contract for the isolated Stage 5 kernel."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.creative_runtime import CreativeRunResult
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, SchemaVersion
from novel_agent.domain.model_calls import ModelCallLedgerAggregate, ModelCostAvailability
from novel_agent.domain.runtime import EffectReceipt, RunEvent, TaskAttempt, TaskRecord


class IsolatedKernelStatus(StrEnum):
    IMPLEMENTED = "ISOLATED_KERNEL_IMPLEMENTED"
    PASS = "ISOLATED_KERNEL_PASS"
    FAILED = "ISOLATED_KERNEL_FAILED"


class Stage5ScenarioEvidence(DomainModel):
    scenario_id: str = Field(min_length=1, max_length=128)
    passed: bool
    evidence_hash: ArtifactId
    reason: str = Field(min_length=1, max_length=512)


class Stage5IsolatedKernelReport(DomainModel):
    schema_version: SchemaVersion = SchemaVersion("1.0.0")
    status: IsolatedKernelStatus
    executable_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    manifest_fingerprint: ArtifactId
    scenarios: tuple[Stage5ScenarioEvidence, ...]
    real_stage4_adapter: Literal[False] = False
    creative_product_gate: Literal["NOT_RUN"] = "NOT_RUN"
    production_activation: Literal["BLOCKED"] = "BLOCKED"


class Stage5RuntimeAuditReport(DomainModel):
    """Read-only report derived from durable truth, never a mutable runtime state."""

    schema_version: SchemaVersion = SchemaVersion("1.0.0")
    status: Literal["ISOLATED_KERNEL_IMPLEMENTED"] = "ISOLATED_KERNEL_IMPLEMENTED"
    run_id: RunId
    generated_at: datetime
    manifest_fingerprint: ArtifactId
    executable_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    tasks: tuple[TaskRecord, ...]
    attempts: tuple[TaskAttempt, ...]
    retry_owners: dict[str, str]
    effects: tuple[EffectReceipt, ...]
    events: tuple[RunEvent, ...]
    model_request_count: int = Field(ge=0)
    model_cost_usd: Decimal | None = Field(default=None, ge=Decimal("0"))
    model_cost_availability: ModelCostAvailability = ModelCostAvailability.UNKNOWN
    model_call_aggregates: tuple[ModelCallLedgerAggregate, ...] = ()
    skill_hashes: tuple[str, ...] = ()
    active_feature_flags: tuple[str, ...] = ()
    deferred_feature_flags: tuple[str, ...]
    zero_direct_canon_bypass: Literal[True] = True
    real_stage4_adapter: Literal[False] = False
    production_activation: Literal["BLOCKED"] = "BLOCKED"


class VerticalRunStatus(StrEnum):
    COMPLETED = "completed"
    WAITING = "waiting"
    RECOVERY_PENDING = "recovery_pending"
    BLOCKED = "blocked"
    YIELDED = "yielded"


class Stage5VerticalRunReport(DomainModel):
    """Frozen orchestration evidence from one real multi-chapter runtime execution."""

    schema_version: SchemaVersion = SchemaVersion("1.0.0")
    run_id: RunId
    project_id: ProjectId
    current_chapter: int = Field(ge=0)
    target_chapter: int = Field(ge=1)
    status: VerticalRunStatus
    final_commit: CommitId
    completed_chapters: tuple[int, ...] = ()
    dispatch_slices: int = Field(default=0, ge=0)
    runtime_results: tuple[CreativeRunResult, ...]
    tasks: tuple[TaskRecord, ...]
    outputs_frozen: bool

    @property
    def generated_chapter_count(self) -> int:
        return len(self.completed_chapters)


__all__ = [
    "IsolatedKernelStatus",
    "Stage5IsolatedKernelReport",
    "Stage5RuntimeAuditReport",
    "Stage5ScenarioEvidence",
    "Stage5VerticalRunReport",
    "VerticalRunStatus",
]
