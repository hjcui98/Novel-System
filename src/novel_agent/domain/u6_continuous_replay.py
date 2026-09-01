"""Contracts for the U6-A continuous V0.5 replay and basis freeze."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, StableId
from novel_agent.domain.v05_readout import MemoryIdentitySnapshot

U6_BASIS_MANIFEST_MEDIA_TYPE = (
    "application/vnd.novel-agent.evaluation.u6-checkpoint-basis-manifest+json"
)
U6_CONTINUOUS_REPLAY_REPORT_MEDIA_TYPE = (
    "application/vnd.novel-agent.evaluation.u6-continuous-replay-report+json"
)
U6A_READOUT_PLAN_MEDIA_TYPE = "application/vnd.novel-agent.evaluation.u6a-readout-plan+json"
U6A_READOUT_REPORT_MEDIA_TYPE = "application/vnd.novel-agent.evaluation.u6a-readout-report+json"


class U6BasisStatus(StrEnum):
    PENDING_REPLAY = "PENDING_REPLAY"
    FROZEN = "FROZEN"


class U6BasisKind(StrEnum):
    PUBLIC_DECLARED = "public_declared"
    INTERNAL_N_MINUS_1 = "internal_n_minus_1"


class U6CheckpointBasis(DomainModel):
    """One unique chapter basis with optional Track C/D fan-out jobs."""

    basis_id: StableId
    checkpoint_chapter: int = Field(ge=1, le=300)
    kind: U6BasisKind
    status: U6BasisStatus
    commit_id: CommitId | None = None
    snapshot_id: StableId | None = None
    plan_root_ref: PlanRootRef | None = None
    text_root_ref: TextRootRef | None = None
    world_root_ref: WorldRootRef | None = None
    profile_root_ref: ProjectProfileRootRef | None = None
    jobs: tuple[StableId, ...] = ()
    release_policy: Literal["internal_basis_only"] = "internal_basis_only"

    @model_validator(mode="after")
    def validate_freeze_state(self) -> U6CheckpointBasis:
        refs = (
            self.commit_id,
            self.snapshot_id,
            self.plan_root_ref,
            self.text_root_ref,
            self.world_root_ref,
            self.profile_root_ref,
        )
        if self.status is U6BasisStatus.PENDING_REPLAY and any(ref is not None for ref in refs):
            raise ValueError("pending basis cannot carry replay roots")
        if self.status is U6BasisStatus.FROZEN and any(ref is None for ref in refs):
            raise ValueError("frozen basis requires commit, snapshot, and all root refs")
        if len(self.jobs) != len(set(self.jobs)):
            raise ValueError("basis jobs must be unique")
        return self


class U6CheckpointBasisManifest(DomainModel):
    """Frozen basis manifest shared by public checkpoints and canary jobs."""

    benchmark_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    frozen_build_id: str = Field(min_length=1)
    status: U6BasisStatus
    replay_scope: Literal["0..300 sequential_once"]
    status_machine: tuple[U6BasisStatus, ...] = (
        U6BasisStatus.PENDING_REPLAY,
        U6BasisStatus.FROZEN,
    )
    status_note: str = Field(min_length=1)
    basis_nodes: tuple[U6CheckpointBasis, ...]

    @model_validator(mode="after")
    def validate_manifest(self) -> U6CheckpointBasisManifest:
        chapters = tuple(node.checkpoint_chapter for node in self.basis_nodes)
        if len(chapters) != len(set(chapters)):
            raise ValueError("checkpoint basis declarations must be unique by chapter")
        if self.status_machine != (
            U6BasisStatus.PENDING_REPLAY,
            U6BasisStatus.FROZEN,
        ):
            raise ValueError("U6 basis status machine is not the frozen protocol")
        expected = self.status
        if any(node.status is not expected for node in self.basis_nodes):
            raise ValueError("basis node status must match the manifest status")
        return self

    def validate_shape(
        self,
        *,
        public_chapters: tuple[int, ...],
        internal_chapters: tuple[int, ...],
    ) -> U6CheckpointBasisManifest:
        """Validate the benchmark's public/internal union without mutating it."""

        public = set(public_chapters)
        internal = set(internal_chapters)
        if public & internal:
            raise ValueError("public and internal basis chapter sets must be disjoint")
        expected = public | internal
        actual = {node.checkpoint_chapter for node in self.basis_nodes}
        if actual != expected:
            raise ValueError(
                "basis chapter set differs from the one-pass protocol: "
                f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
            )
        for node in self.basis_nodes:
            expected_kind = (
                U6BasisKind.PUBLIC_DECLARED
                if node.checkpoint_chapter in public
                else U6BasisKind.INTERNAL_N_MINUS_1
            )
            if node.kind is not expected_kind:
                raise ValueError(
                    f"basis C{node.checkpoint_chapter} has kind {node.kind.value}, "
                    f"expected {expected_kind.value}"
                )
        return self


class U6CheckpointLineage(DomainModel):
    """Durable identity comparison recorded after a checkpoint namespace closes."""

    basis_id: StableId
    checkpoint_chapter: int = Field(ge=1, le=300)
    commit_id: CommitId
    snapshot_id: StableId
    plan_root_ref: PlanRootRef
    text_root_ref: TextRootRef
    world_root_ref: WorldRootRef
    profile_root_ref: ProjectProfileRootRef
    index_lineage_ref: ArtifactRef
    memory_identity_before: ArtifactId
    memory_identity_after: ArtifactId
    control_replay_identity: ArtifactId
    evaluation_namespace: Literal["PENDING_READOUT", "DISCARDED"]
    identity_match: bool

    @model_validator(mode="after")
    def validate_identity(self) -> U6CheckpointLineage:
        if self.memory_identity_before != self.memory_identity_after:
            raise ValueError("evaluation discard changed the durable memory identity")
        if not self.identity_match:
            raise ValueError("checkpoint identity does not match the control replay")
        return self


class U6ContinuousReplayReport(DomainModel):
    """Report that distinguishes frozen basis preparation from readout completion."""

    report_schema: Literal["u6-continuous-replay-report.v1"] = Field(
        default="u6-continuous-replay-report.v1",
        alias="schema",
    )
    campaign_id: StableId
    run_id: RunId
    project_id: ProjectId
    benchmark_id: str = Field(min_length=1)
    benchmark_version: str = Field(min_length=1)
    basis_manifest_ref: ArtifactRef
    chapters_declared: int = Field(ge=0)
    chapters_ingested: int = Field(ge=0)
    ingest_passes: int = Field(ge=0)
    public_basis_count: int = Field(ge=0)
    internal_basis_count: int = Field(ge=0)
    basis_count: int = Field(ge=0)
    canary_job_count: int = Field(ge=0)
    expected_readout_task_count: int = Field(ge=0)
    completed_readout_task_count: int = Field(ge=0)
    evaluation_discard_count: int = Field(ge=0)
    future_leakage_count: int = Field(ge=0)
    duplicate_checkpoint_declarations: int = Field(ge=0)
    control_replay_identity: ArtifactId
    lineage: tuple[U6CheckpointLineage, ...]
    readout_report_ref: ArtifactRef | None = None
    status: Literal["BASIS_FROZEN", "REVIEW_REQUIRED", "COMPLETED"]

    @model_validator(mode="after")
    def validate_report(self) -> U6ContinuousReplayReport:
        if self.chapters_ingested > self.chapters_declared:
            raise ValueError("ingested chapter count exceeds declared stream")
        if self.basis_count != len(self.lineage):
            raise ValueError("basis count does not match checkpoint lineage")
        if self.basis_count != self.public_basis_count + self.internal_basis_count:
            raise ValueError("basis count does not match public/internal counts")
        if self.ingest_passes > 1:
            raise ValueError("U6-A only permits one sequential ingest pass")
        if self.future_leakage_count != 0:
            raise ValueError("U6-A report cannot carry future leakage")
        if self.duplicate_checkpoint_declarations != 0:
            raise ValueError("U6-A report cannot carry duplicate checkpoint declarations")
        if self.status == "COMPLETED":
            if self.readout_report_ref is None:
                raise ValueError("completed U6-A report requires a readout report")
            if self.completed_readout_task_count != self.expected_readout_task_count:
                raise ValueError("completed U6-A report has incomplete readout tasks")
        return self


U6A_READOUT_LIFECYCLE = (
    "freeze",
    "release",
    "wcp",
    "writer",
    "response_freeze",
    "evaluator_reveal",
    "discard",
)


class U6AReadoutTrack(StrEnum):
    QA = "novelmem_qa"
    CONTEXT = "novelmem_context"
    C_ROLL = "c_roll"
    D_SHORT = "d_short"
    FREE_RUN = "free_run"


class U6AReadoutTask(DomainModel):
    """One public QA/Context task released against one frozen basis."""

    task_id: StableId
    track: Literal["novelmem_qa", "novelmem_context"]
    checkpoint_chapter: int = Field(ge=1, le=300)
    basis_id: StableId
    source_task_id: StableId
    future_visibility: Literal["evaluator_only"] = "evaluator_only"
    lifecycle: tuple[str, ...] = U6A_READOUT_LIFECYCLE
    status: Literal["PLANNED", "EXECUTED", "REVIEW_REQUIRED"] = "PLANNED"

    @model_validator(mode="after")
    def validate_lifecycle(self) -> U6AReadoutTask:
        if self.lifecycle != U6A_READOUT_LIFECYCLE:
            raise ValueError("U6-A readout task lifecycle is incomplete or reordered")
        return self


class U6ACanaryJob(DomainModel):
    """One Track C/D or free-run job attached to an existing N-1 basis."""

    job_id: StableId
    track: U6AReadoutTrack
    checkpoint_chapter: int = Field(ge=1, le=300)
    basis_id: StableId
    future_visibility: Literal["evaluator_only"] = "evaluator_only"
    release_policy: Literal["after_basis_freeze"] = "after_basis_freeze"
    lifecycle: tuple[str, ...] = U6A_READOUT_LIFECYCLE
    status: Literal["PLANNED", "EXECUTED", "REVIEW_REQUIRED"] = "PLANNED"

    @model_validator(mode="after")
    def validate_lifecycle(self) -> U6ACanaryJob:
        if self.lifecycle != U6A_READOUT_LIFECYCLE:
            raise ValueError("U6-A canary lifecycle is incomplete or reordered")
        return self


class U6AReadoutPlan(DomainModel):
    """One immutable task/job plan consumed by the future real readout runner."""

    plan_schema: Literal["u6a-readout-plan.v1"] = Field(
        default="u6a-readout-plan.v1",
        alias="schema",
    )
    campaign_id: StableId
    basis_manifest_ref: ArtifactRef
    source_readout_manifest_ref: ArtifactRef
    tasks: tuple[U6AReadoutTask, ...]
    canary_jobs: tuple[U6ACanaryJob, ...]
    qa_task_count: int = Field(ge=0)
    context_task_count: int = Field(ge=0)
    canary_job_count: int = Field(ge=0)
    status: Literal["READY", "EXECUTED", "REVIEW_REQUIRED"]

    @model_validator(mode="after")
    def validate_plan(self) -> U6AReadoutPlan:
        task_ids = tuple(task.task_id for task in self.tasks)
        job_ids = tuple(job.job_id for job in self.canary_jobs)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("U6-A readout task identities must be unique")
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("U6-A canary job identities must be unique")
        if self.qa_task_count != sum(task.track == U6AReadoutTrack.QA.value for task in self.tasks):
            raise ValueError("U6-A QA count does not match task identities")
        if self.context_task_count != sum(
            task.track == U6AReadoutTrack.CONTEXT.value for task in self.tasks
        ):
            raise ValueError("U6-A Context count does not match task identities")
        if self.canary_job_count != len(self.canary_jobs):
            raise ValueError("U6-A canary count does not match job identities")
        return self


U6A_READOUT_PHASES = U6A_READOUT_LIFECYCLE[:-1]


class U6AReadoutPhaseResult(DomainModel):
    """One adapter-owned, frozen result from a pre-discard U6-A phase."""

    phase: Literal[
        "freeze",
        "release",
        "wcp",
        "writer",
        "response_freeze",
        "evaluator_reveal",
    ]
    artifact_refs: tuple[ArtifactRef, ...] = ()
    evaluation_refs: tuple[ArtifactRef, ...] = ()
    memory_identity: MemoryIdentitySnapshot | None = None
    future_leakage_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    gap_count: int = Field(default=0, ge=0)
    evidence_distance: int = Field(default=0, ge=0)
    stage_loss_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_phase_result(self) -> U6AReadoutPhaseResult:
        ref_keys = {(ref.artifact_id, ref.media_type) for ref in self.artifact_refs}
        evaluation_keys = {(ref.artifact_id, ref.media_type) for ref in self.evaluation_refs}
        if len(ref_keys) != len(self.artifact_refs):
            raise ValueError("U6-A phase artifact refs must be unique")
        if not evaluation_keys.issubset(ref_keys):
            raise ValueError("U6-A evaluation refs must be a subset of phase artifact refs")
        return self


class U6AReadoutItemReceipt(DomainModel):
    """Durable per-item evidence after one checkpoint lifecycle completes."""

    item_id: StableId
    track: U6AReadoutTrack
    checkpoint_chapter: int = Field(ge=1, le=300)
    basis_id: StableId
    run_id: RunId
    lifecycle: tuple[str, ...] = U6A_READOUT_LIFECYCLE
    completed_phases: tuple[str, ...] = ()
    phase_results: tuple[U6AReadoutPhaseResult, ...] = ()
    discarded_refs: tuple[ArtifactRef, ...] = ()
    discard_receipt_ref: ArtifactRef | None = None
    memory_identity_before: MemoryIdentitySnapshot | None = None
    memory_identity_after: MemoryIdentitySnapshot | None = None
    control_replay_identity: ArtifactId
    future_leakage_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    gap_count: int = Field(default=0, ge=0)
    evidence_distance: int = Field(default=0, ge=0)
    stage_loss_count: int = Field(default=0, ge=0)
    status: Literal["EXECUTED", "REVIEW_REQUIRED"]

    @model_validator(mode="after")
    def validate_item_receipt(self) -> U6AReadoutItemReceipt:
        if self.lifecycle != U6A_READOUT_LIFECYCLE:
            raise ValueError("U6-A item lifecycle is incomplete or reordered")
        if self.completed_phases != tuple(
            phase for phase in U6A_READOUT_PHASES if phase in self.completed_phases
        ):
            raise ValueError("U6-A completed phases are not an ordered prefix")
        result_phases = tuple(result.phase for result in self.phase_results)
        if result_phases != self.completed_phases:
            raise ValueError("U6-A phase results do not match completed phases")
        if self.status == "EXECUTED":
            if self.completed_phases != U6A_READOUT_PHASES:
                raise ValueError("executed U6-A item is missing a readout phase")
            if self.discard_receipt_ref is None:
                raise ValueError("executed U6-A item requires a discard receipt")
            if self.memory_identity_before != self.memory_identity_after:
                raise ValueError("U6-A discard changed the item Memory identity")
        if self.discard_receipt_ref is None and self.discarded_refs:
            raise ValueError("U6-A discarded refs require a discard receipt")
        if self.future_leakage_count != sum(
            result.future_leakage_count for result in self.phase_results
        ):
            raise ValueError("U6-A item future leakage is not phase-derived")
        metric_fields = (
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "gap_count",
            "evidence_distance",
            "stage_loss_count",
        )
        for field in metric_fields:
            expected = sum(getattr(result, field) for result in self.phase_results)
            if getattr(self, field) != expected:
                raise ValueError(f"U6-A item {field} is not phase-derived")
        return self


class U6AReadoutCheckpointMetric(DomainModel):
    """Trend row for one frozen checkpoint after its evaluation namespace closes."""

    checkpoint_chapter: int = Field(ge=1, le=300)
    item_count: int = Field(ge=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    evidence_distance: int = Field(ge=0)
    stage_loss_count: int = Field(ge=0)


class U6AReadoutRunReport(DomainModel):
    """Reconstructible U6-A lifecycle report, separate from basis preparation."""

    report_schema: Literal["u6a-readout-report.v1"] = Field(
        default="u6a-readout-report.v1",
        alias="schema",
    )
    campaign_id: StableId
    run_id: RunId
    basis_manifest_ref: ArtifactRef
    plan_ref: ArtifactRef
    control_replay_identity: ArtifactId
    task_count: int = Field(ge=0)
    canary_job_count: int = Field(ge=0)
    expected_item_count: int = Field(ge=0)
    completed_item_count: int = Field(ge=0)
    expected_checkpoint_count: int = Field(ge=0)
    completed_checkpoint_count: int = Field(ge=0)
    evaluation_discard_count: int = Field(ge=0)
    future_leakage_count: int = Field(ge=0)
    items: tuple[U6AReadoutItemReceipt, ...] = ()
    checkpoint_metrics: tuple[U6AReadoutCheckpointMetric, ...] = ()
    status: Literal["COMPLETED", "REVIEW_REQUIRED"]
    first_failure_phase: str | None = None
    first_failure_item_id: StableId | None = None
    first_failure_type: str | None = None
    # Keep the first typed failure actionable without persisting prompts or
    # provider response bodies.  Historical reports may omit this optional
    # field; new review-required reports populate it from the boundary error.
    first_failure_detail: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def validate_run_report(self) -> U6AReadoutRunReport:
        item_ids = tuple(item.item_id for item in self.items)
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("U6-A report item identities must be unique")
        if self.expected_item_count != self.task_count + self.canary_job_count:
            raise ValueError("U6-A expected item count does not match task and canary counts")
        executed_items = tuple(item for item in self.items if item.status == "EXECUTED")
        if self.completed_item_count != len(executed_items):
            raise ValueError("U6-A completed item count does not match item receipts")
        if self.future_leakage_count != sum(item.future_leakage_count for item in self.items):
            raise ValueError("U6-A report future leakage is not item-derived")
        completed_checkpoints = {item.checkpoint_chapter for item in executed_items}
        if self.completed_checkpoint_count != len(completed_checkpoints):
            raise ValueError("U6-A completed checkpoint count does not match item receipts")
        metric_chapters = tuple(metric.checkpoint_chapter for metric in self.checkpoint_metrics)
        if len(metric_chapters) != len(set(metric_chapters)):
            raise ValueError("U6-A checkpoint metric chapters must be unique")
        if set(metric_chapters) != completed_checkpoints:
            raise ValueError("U6-A checkpoint metrics do not match completed item chapters")
        if self.status == "COMPLETED":
            if self.completed_item_count != self.expected_item_count:
                raise ValueError("completed U6-A report has incomplete item coverage")
            if self.completed_checkpoint_count != self.expected_checkpoint_count:
                raise ValueError("completed U6-A report has incomplete checkpoint coverage")
            if self.evaluation_discard_count != self.expected_checkpoint_count:
                raise ValueError("completed U6-A report has incomplete discard coverage")
            if self.first_failure_phase is not None or self.first_failure_item_id is not None:
                raise ValueError("completed U6-A report cannot carry a failure")
        else:
            if self.first_failure_phase is None or self.first_failure_item_id is None:
                raise ValueError("review-required U6-A report needs a typed failure")
        return self


__all__ = [
    "U6A_READOUT_LIFECYCLE",
    "U6A_READOUT_PHASES",
    "U6A_READOUT_PLAN_MEDIA_TYPE",
    "U6A_READOUT_REPORT_MEDIA_TYPE",
    "U6_BASIS_MANIFEST_MEDIA_TYPE",
    "U6_CONTINUOUS_REPLAY_REPORT_MEDIA_TYPE",
    "U6ACanaryJob",
    "U6AReadoutCheckpointMetric",
    "U6AReadoutItemReceipt",
    "U6AReadoutPhaseResult",
    "U6AReadoutPlan",
    "U6AReadoutRunReport",
    "U6AReadoutTask",
    "U6AReadoutTrack",
    "U6BasisKind",
    "U6BasisStatus",
    "U6CheckpointBasis",
    "U6CheckpointBasisManifest",
    "U6CheckpointLineage",
    "U6ContinuousReplayReport",
]
