"""V0.5 Writer readout runner identities. No Gold, future text, or answers."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import (
    EFFECTIVE_BUDGET_MEDIA_TYPE,
    PRODUCTION_ASSEMBLY_ATTESTATION_MEDIA_TYPE,
    V05_READOUT_CAMPAIGN_MANIFEST_MEDIA_TYPE,
    ArtifactRef,
    is_evaluation_artifact_media_type,
)
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, StableId
from novel_agent.domain.model_calls import (
    BudgetResolutionProfile,
    BudgetSource,
    EffectiveBudgetResult,
    ModelCallLedgerAggregate,
    ModelCostAvailability,
)
from novel_agent.domain.writer_context import BenchmarkInformationProfile


class V05ReadoutTrack(StrEnum):
    QA = "novelmem_qa"
    CONTEXT = "novelmem_context"


class V05HistoryAccess(StrEnum):
    HISTORY_ONLY = "history_only"
    AUTHOR_PLAN_CONDITIONED = "author_plan_conditioned"


_HISTORY_ACCESS_TO_PROFILE = {
    V05HistoryAccess.HISTORY_ONLY: BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    V05HistoryAccess.AUTHOR_PLAN_CONDITIONED: BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
}


class U4L0CanaryVariableLock(DomainModel):
    """One frozen U4-L0 variable vector carried by campaign evidence."""

    budget_profile: BudgetResolutionProfile
    controller_context_level: Literal["C0", "C1+C2"]
    planner_context_level: Literal["P0", "P0+P1"]
    thinking_enabled: bool
    c3_admission: Literal["NOT_ADMITTED"] = "NOT_ADMITTED"


def map_v05_history_access(
    history_access: str | V05HistoryAccess,
) -> BenchmarkInformationProfile:
    """Map V0.5 history_only to the invisible-plan production profile."""

    if isinstance(history_access, str):
        try:
            history_access = V05HistoryAccess(history_access)
        except ValueError as error:
            raise ValueError(f"unsupported V0.5 history access: {history_access}") from error
    return _HISTORY_ACCESS_TO_PROFILE[history_access]


class V05ReadoutTaskIdentity(DomainModel):
    """One unique V0.5 Writer readout identity. Public fields only."""

    task_id: StableId
    track: V05ReadoutTrack
    checkpoint_id: StableId
    checkpoint_chapter: int = Field(ge=0)
    history_access: V05HistoryAccess
    information_profile: BenchmarkInformationProfile
    plan_release: Literal["after_checkpoint_freeze"] = "after_checkpoint_freeze"
    question_release: Literal["after_checkpoint_freeze"] | None = None
    question_id: StableId | None = None
    target_chapter_start: int | None = Field(default=None, ge=1)
    target_chapter_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_identity(self) -> V05ReadoutTaskIdentity:
        if self.track is V05ReadoutTrack.QA:
            if self.question_id is None or self.question_release is None:
                raise ValueError("QA readout identity requires a question id and release timing")
            if self.target_chapter_start is not None or self.target_chapter_end is not None:
                raise ValueError("QA readout identity must not carry a target window")
        else:
            if self.question_id is not None or self.question_release is not None:
                raise ValueError("Context readout identity must not carry a QA question")
            if self.target_chapter_start is None or self.target_chapter_end is None:
                raise ValueError("Context readout identity requires a target window")
            if self.target_chapter_end < self.target_chapter_start:
                raise ValueError("Context readout target range is invalid")
            if self.target_chapter_start <= self.checkpoint_chapter:
                raise ValueError("Context readout target range must follow its checkpoint")
        expected = map_v05_history_access(self.history_access)
        if self.information_profile is not expected:
            raise ValueError("V0.5 history access does not match the production profile")
        return self


class V05ReadoutManifest(DomainModel):
    """Complete unique identity set for V0.5 Track A and Track B Writer readout."""

    benchmark_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    tasks: tuple[V05ReadoutTaskIdentity, ...]
    canary_lock: U4L0CanaryVariableLock | None = None

    @model_validator(mode="after")
    def validate_unique_tasks(self) -> V05ReadoutManifest:
        task_ids = tuple(task.task_id for task in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("V0.5 readout task identities must be unique")
        return self


class V05CampaignPhase(StrEnum):
    SEED = "seed"
    CALIBRATION = "calibration"
    FORMAL = "formal"


class V05CampaignStatus(StrEnum):
    SEED_DIAGNOSTIC_NOT_ACCEPTANCE = "seed_diagnostic_not_acceptance"
    CALIBRATION_NOT_ACCEPTANCE = "calibration_not_acceptance"
    FORMAL_CANDIDATE = "formal_candidate"


class V05ReadinessStatus(StrEnum):
    PASS = "PASS"
    PENDING = "PENDING"
    UNAVAILABLE = "UNAVAILABLE"


class V05CampaignSourceIdentity(DomainModel):
    """Source and readiness facts frozen before a Writer-readout campaign."""

    bundle_id: str = Field(min_length=1)
    bundle_version: str = Field(min_length=1)
    build_report_ref: ArtifactRef
    assembly_attestation_ref: ArtifactRef
    effective_budget_ref: ArtifactRef
    source_identity: ArtifactId
    r_bundle: V05ReadinessStatus
    r_annotation: V05ReadinessStatus
    r_judge: V05ReadinessStatus
    r_runner: V05ReadinessStatus

    @model_validator(mode="after")
    def validate_runtime_refs(self) -> V05CampaignSourceIdentity:
        if self.assembly_attestation_ref.media_type != PRODUCTION_ASSEMBLY_ATTESTATION_MEDIA_TYPE:
            raise ValueError("campaign attestation ref has the wrong media type")
        if self.effective_budget_ref.media_type != EFFECTIVE_BUDGET_MEDIA_TYPE:
            raise ValueError("campaign effective budget ref has the wrong media type")
        return self


class V05ReadoutFreezeReceipt(DomainModel):
    """Durable zero-call receipt binding the immutable campaign to U2 facts."""

    receipt_version: Literal["v05_readout_freeze_receipt.v1"] = "v05_readout_freeze_receipt.v1"
    receipt_id: StableId
    campaign_id: StableId
    ledger_identity: str = Field(min_length=1)
    ledger_before_request_count: int = Field(ge=0)
    ledger_after_request_count: int = Field(ge=0)
    manifest_ref: ArtifactRef
    attestation_ref: ArtifactRef
    effective_budget_ref: ArtifactRef
    zero_model_call: Literal[True] = True
    frozen_before_run: Literal[True] = True

    @model_validator(mode="after")
    def validate_zero_call_freeze(self) -> V05ReadoutFreezeReceipt:
        if self.ledger_before_request_count != self.ledger_after_request_count:
            raise ValueError("freeze ledger count changed during manifest creation")
        if self.ledger_before_request_count != 0:
            raise ValueError("freeze receipt requires an empty ledger before the campaign")
        if self.manifest_ref.media_type != V05_READOUT_CAMPAIGN_MANIFEST_MEDIA_TYPE:
            raise ValueError("freeze manifest ref has the wrong media type")
        if self.attestation_ref.media_type != PRODUCTION_ASSEMBLY_ATTESTATION_MEDIA_TYPE:
            raise ValueError("freeze attestation ref has the wrong media type")
        if self.effective_budget_ref.media_type != EFFECTIVE_BUDGET_MEDIA_TYPE:
            raise ValueError("freeze effective budget ref has the wrong media type")
        if self.zero_model_call is not True or self.frozen_before_run is not True:
            raise ValueError("freeze receipt must be a pre-run zero-call receipt")
        return self


class V05WriterRuntimeFreeze(DomainModel):
    """Writer request identity and fixed transport budgets."""

    endpoint: str = Field(min_length=1)
    model: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    prompt_ref: ArtifactRef
    response_schema_ref: ArtifactRef
    request_role: str = Field(min_length=1)
    temperature: float = Field(ge=0.0, le=2.0)
    seed: int | None = None
    seed_capability: Literal["fixed", "unsupported", "uncontrolled"]
    evidence_token_budget: int = Field(ge=0)
    """Writer body-output budget; provider reserve is recorded separately below."""

    output_token_budget: int = Field(ge=1)
    provider_total_output_budget: int | None = Field(default=None, ge=1)
    concurrency: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_seed_capability(self) -> V05WriterRuntimeFreeze:
        if self.seed_capability == "fixed" and self.seed is None:
            raise ValueError("fixed Writer seed capability requires a seed")
        if self.seed_capability == "unsupported" and self.seed is not None:
            raise ValueError("unsupported Writer seed capability cannot carry a seed")
        if self.prompt_ref.artifact_id == self.response_schema_ref.artifact_id:
            raise ValueError("Writer prompt and response schema must be distinct artifacts")
        return self


class V05JudgeRuntimeFreeze(DomainModel):
    """Independent scorer and judge contracts, including unavailable behavior."""

    request_role: str = Field(min_length=1)
    deterministic_scorer_version: str = Field(min_length=1)
    answer_judge_version: str = Field(min_length=1)
    evidence_support_judge_version: str = Field(min_length=1)
    unavailable_policy: Literal["pending", "unavailable", "fail_closed"]


class V05RuntimeVariableFreeze(DomainModel):
    """U4-L0 variables held constant while a readout campaign runs."""

    budget_source: BudgetSource
    budget_profile: BudgetResolutionProfile = BudgetResolutionProfile.CANARY
    context_limit: int = Field(ge=1)
    body_output_tokens: int = Field(ge=1)
    reasoning_reserve_tokens: int = Field(ge=0)
    safety_allowance_tokens: int = Field(ge=0)
    controller_level: Literal["C0", "C1+C2"]
    planner_level: Literal["P0", "P0+P1"]
    thinking_enabled: bool
    effective_budget: EffectiveBudgetResult | None = None
    provider_total_output_tokens: int | None = Field(default=None, ge=1)
    provider_reasoning_included_in_completion_tokens: bool | None = None

    @classmethod
    def from_effective_budget(
        cls,
        effective_budget: EffectiveBudgetResult,
        *,
        budget_profile: BudgetResolutionProfile = BudgetResolutionProfile.CANARY,
        controller_level: Literal["C0", "C1+C2"],
        planner_level: Literal["P0", "P0+P1"],
        thinking_enabled: bool,
        provider_reasoning_included_in_completion_tokens: bool,
    ) -> V05RuntimeVariableFreeze:
        """Build frozen variables from the gateway's one resolved budget result."""

        return cls(
            budget_source=effective_budget.budget_source,
            budget_profile=budget_profile,
            context_limit=effective_budget.context_limit,
            body_output_tokens=effective_budget.body_output_budget,
            reasoning_reserve_tokens=effective_budget.thinking_budget,
            safety_allowance_tokens=effective_budget.safety_allowance_tokens,
            controller_level=controller_level,
            planner_level=planner_level,
            thinking_enabled=thinking_enabled,
            effective_budget=effective_budget,
            provider_total_output_tokens=effective_budget.total_output_budget,
            provider_reasoning_included_in_completion_tokens=(
                provider_reasoning_included_in_completion_tokens
            ),
        )

    @model_validator(mode="after")
    def validate_capacity(self) -> V05RuntimeVariableFreeze:
        effective = self.effective_budget
        if effective is None:
            if (
                self.body_output_tokens
                + self.reasoning_reserve_tokens
                + self.safety_allowance_tokens
                > self.context_limit
            ):
                raise ValueError("frozen output and safety budgets exceed context limit")
            return self
        expected = {
            "budget_source": effective.budget_source,
            "context_limit": effective.context_limit,
            "body_output_tokens": effective.body_output_budget,
            "reasoning_reserve_tokens": effective.thinking_budget,
            "safety_allowance_tokens": effective.safety_allowance_tokens,
        }
        for field, value in expected.items():
            if getattr(self, field) != value:
                raise ValueError(
                    f"frozen runtime field {field} does not match EffectiveBudgetResult"
                )
        if self.provider_total_output_tokens != effective.total_output_budget:
            raise ValueError("provider total output reserve must match EffectiveBudgetResult")
        if self.provider_reasoning_included_in_completion_tokens is None:
            raise ValueError("provider reasoning billing policy is required")
        if self.thinking_enabled != (effective.thinking_budget > 0):
            raise ValueError("thinking flag does not match EffectiveBudgetResult")
        if (
            effective.total_output_budget + effective.safety_allowance_tokens
            > effective.context_limit
        ):
            raise ValueError("frozen output and safety budgets exceed context limit")
        return self


class V05CampaignExecutionFreeze(DomainModel):
    """Repetition, resource, namespace, and stop policy fixed before run."""

    repetitions: int = Field(ge=1)
    max_model_calls: int = Field(ge=1)
    max_wall_time_seconds: int = Field(ge=1)
    output_namespace: str = Field(min_length=1)
    object_namespace: str = Field(min_length=1)
    database_namespace: str = Field(min_length=1)
    stop_conditions: tuple[str, ...] = Field(min_length=1)


class V05CampaignThreshold(DomainModel):
    metric: str = Field(min_length=1)
    operator: Literal[">=", "<=", "=="]
    value: float


class V05CampaignReportFreeze(DomainModel):
    """Report dimensions and any thresholds registered before candidate runs."""

    dimensions: tuple[str, ...] = Field(min_length=1)
    threshold_policy: Literal["diagnostic_only", "pre_registered"]
    thresholds: tuple[V05CampaignThreshold, ...] = ()
    canary_lock: U4L0CanaryVariableLock | None = None

    @model_validator(mode="after")
    def validate_threshold_policy(self) -> V05CampaignReportFreeze:
        if self.threshold_policy == "pre_registered" and not self.thresholds:
            raise ValueError("pre_registered reporting requires frozen thresholds")
        if self.threshold_policy == "diagnostic_only" and self.thresholds:
            raise ValueError("diagnostic-only reporting cannot carry pass thresholds")
        return self


class V05RepresentativeTaskCoverage(DomainModel):
    """Preselected task ids proving the required U4-S representative coverage."""

    early_checkpoint_task_id: StableId
    mid_checkpoint_task_id: StableId
    late_checkpoint_task_id: StableId
    legacy_five_chapter_window_task_id: StableId
    twenty_chapter_window_task_id: StableId
    unanswerable_qa_task_id: StableId
    multi_hop_qa_task_id: StableId

    def task_ids(self) -> tuple[StableId, ...]:
        return (
            self.early_checkpoint_task_id,
            self.mid_checkpoint_task_id,
            self.late_checkpoint_task_id,
            self.legacy_five_chapter_window_task_id,
            self.twenty_chapter_window_task_id,
            self.unanswerable_qa_task_id,
            self.multi_hop_qa_task_id,
        )


class V05ReadoutCampaignManifest(DomainModel):
    """Immutable U4-S0 campaign contract wrapping the public readout identities."""

    manifest_version: Literal["v05_readout_campaign_manifest.v1"] = (
        "v05_readout_campaign_manifest.v1"
    )
    campaign_id: StableId
    benchmark_id: str = Field(min_length=1)
    benchmark_version: str = Field(min_length=1)
    phase: V05CampaignPhase
    status: V05CampaignStatus
    readout_manifest: V05ReadoutManifest
    source: V05CampaignSourceIdentity
    writer: V05WriterRuntimeFreeze
    judges: V05JudgeRuntimeFreeze
    runtime: V05RuntimeVariableFreeze
    execution: V05CampaignExecutionFreeze
    report: V05CampaignReportFreeze
    canary_lock: U4L0CanaryVariableLock | None = None
    representative_task_ids: tuple[StableId, ...] = ()
    representative_task_coverage: V05RepresentativeTaskCoverage | None = None
    frozen_before_run: Literal[True] = True

    @model_validator(mode="after")
    def validate_campaign_identity(self) -> V05ReadoutCampaignManifest:
        if self.benchmark_id != self.readout_manifest.benchmark_id:
            raise ValueError("campaign benchmark id does not match readout identities")
        if self.benchmark_version != self.readout_manifest.version:
            raise ValueError("campaign benchmark version does not match readout identities")
        if self.source.bundle_id != self.benchmark_id:
            raise ValueError("campaign source bundle id does not match benchmark")
        if self.source.bundle_version != self.benchmark_version:
            raise ValueError("campaign source bundle version does not match benchmark")
        expected_status = {
            V05CampaignPhase.SEED: V05CampaignStatus.SEED_DIAGNOSTIC_NOT_ACCEPTANCE,
            V05CampaignPhase.CALIBRATION: V05CampaignStatus.CALIBRATION_NOT_ACCEPTANCE,
            V05CampaignPhase.FORMAL: V05CampaignStatus.FORMAL_CANDIDATE,
        }[self.phase]
        if self.status is not expected_status:
            raise ValueError("campaign phase and status are inconsistent")
        if self.writer.request_role == self.judges.request_role:
            raise ValueError("Writer and judge requests must use independent logical roles")
        if self.runtime.effective_budget is None:
            raise ValueError("campaign runtime must reference one EffectiveBudgetResult")
        if self.canary_lock is None:
            raise ValueError("campaign manifest requires the frozen canary lock")
        if self.readout_manifest.canary_lock != self.canary_lock:
            raise ValueError("readout and campaign canary locks must be identical")
        if self.canary_lock.budget_profile is not self.runtime.budget_profile:
            raise ValueError("campaign canary budget profile does not match runtime")
        if self.canary_lock.controller_context_level != self.runtime.controller_level:
            raise ValueError("campaign canary Controller level does not match runtime")
        if self.canary_lock.planner_context_level != self.runtime.planner_level:
            raise ValueError("campaign canary Planner level does not match runtime")
        if self.canary_lock.thinking_enabled != self.runtime.thinking_enabled:
            raise ValueError("campaign canary thinking flag does not match runtime")
        if self.report.canary_lock != self.canary_lock:
            raise ValueError("campaign report canary lock differs from manifest lock")
        if self.writer.output_token_budget != self.runtime.effective_budget.body_output_budget:
            raise ValueError("Writer output budget must match the frozen runtime body budget")
        if self.writer.provider_total_output_budget != (
            self.runtime.effective_budget.total_output_budget
        ):
            raise ValueError("Writer provider reserve must match the frozen total output budget")
        task_ids = {task.task_id for task in self.readout_manifest.tasks}
        if any(task_id not in task_ids for task_id in self.representative_task_ids):
            raise ValueError("representative campaign task is absent from readout identities")
        if len(self.representative_task_ids) != len(set(self.representative_task_ids)):
            raise ValueError("representative campaign tasks must be unique")
        coverage = self.representative_task_coverage
        if coverage is None:
            raise ValueError("representative task coverage must be frozen before the campaign")
        coverage_ids = coverage.task_ids()
        if len(set(coverage_ids)) != len(coverage_ids):
            raise ValueError("representative task coverage ids must be unique")
        if any(task_id not in task_ids for task_id in coverage_ids):
            raise ValueError("representative task coverage references an unknown task")
        if any(task_id not in self.representative_task_ids for task_id in coverage_ids):
            raise ValueError("representative task ids must include every coverage role")
        tasks_by_id = {task.task_id: task for task in self.readout_manifest.tasks}
        early = tasks_by_id[coverage.early_checkpoint_task_id]
        middle = tasks_by_id[coverage.mid_checkpoint_task_id]
        late = tasks_by_id[coverage.late_checkpoint_task_id]
        if not (early.checkpoint_chapter < middle.checkpoint_chapter < late.checkpoint_chapter):
            raise ValueError("representative checkpoints must cover early, middle, and late order")
        legacy = tasks_by_id[coverage.legacy_five_chapter_window_task_id]
        long_window = tasks_by_id[coverage.twenty_chapter_window_task_id]
        if legacy.target_chapter_start is None or legacy.target_chapter_end is None:
            raise ValueError("legacy representative must be a Context window")
        if legacy.target_chapter_end - legacy.target_chapter_start + 1 != 5:
            raise ValueError("legacy representative must cover a five-chapter window")
        if long_window.target_chapter_start is None or long_window.target_chapter_end is None:
            raise ValueError("long-window representative must be a Context window")
        if long_window.target_chapter_end - long_window.target_chapter_start + 1 != 20:
            raise ValueError("long-window representative must cover a twenty-chapter window")
        for label, task_id in (
            ("unanswerable", coverage.unanswerable_qa_task_id),
            ("multi-hop", coverage.multi_hop_qa_task_id),
        ):
            task = tasks_by_id[task_id]
            if task.question_id is None:
                raise ValueError(f"{label} representative must be a QA task")
        if self.phase is V05CampaignPhase.FORMAL and any(
            status is not V05ReadinessStatus.PASS
            for status in (
                self.source.r_bundle,
                self.source.r_annotation,
                self.source.r_judge,
                self.source.r_runner,
            )
        ):
            raise ValueError("formal campaign requires all readiness statuses to be PASS")
        if (
            self.phase is V05CampaignPhase.FORMAL
            and self.report.threshold_policy != "pre_registered"
        ):
            raise ValueError("formal campaign requires pre-registered report thresholds")
        return self


class WriterJudgeKind(StrEnum):
    ANSWER = "answer_judge"
    EVIDENCE_SUPPORT = "evidence_support_judge"


class WriterJudgeAvailability(StrEnum):
    AVAILABLE = "available"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"


class BenchmarkFailureLayer(StrEnum):
    TRANSPORT = "transport"
    RAW = "raw"
    PARSE = "parse"
    PACKAGE = "package"
    WRITER_ANSWER = "writer-answer"
    ANSWER_JUDGE = "answer-judge"
    EVIDENCE_JUDGE = "evidence-judge"
    NONE = "none"


class WriterJudgeReceipt(DomainModel):
    """One Answer or Evidence-Support Judge phase. Pending is not a zero score."""

    receipt_id: StableId
    kind: WriterJudgeKind
    availability: WriterJudgeAvailability
    logical_phase: str = Field(min_length=1)
    run_id: RunId
    task_id: StableId
    freeze_receipt_id: StableId
    response_ref: ArtifactRef
    input_artifact_ref: ArtifactRef | None = None
    output_artifact_ref: ArtifactRef | None = None
    model_request_id: StableId | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_pending_score(self) -> WriterJudgeReceipt:
        expected_phase = (
            "benchmark.answer_judge"
            if self.kind is WriterJudgeKind.ANSWER
            else "benchmark.evidence_support_judge"
        )
        if self.logical_phase != expected_phase:
            raise ValueError("judge receipt phase does not match its kind")
        pending = self.availability in {
            WriterJudgeAvailability.PENDING,
            WriterJudgeAvailability.UNAVAILABLE,
        }
        if pending and self.score is not None:
            raise ValueError("pending or unavailable judge cannot carry a score")
        if pending and self.output_artifact_ref is not None:
            raise ValueError("pending or unavailable judge cannot claim an output artifact")
        if pending and self.model_request_id is not None:
            raise ValueError("pending or unavailable judge cannot claim a model request")
        if self.availability is WriterJudgeAvailability.AVAILABLE and self.score is None:
            raise ValueError("available judge requires a score")
        if self.availability is WriterJudgeAvailability.AVAILABLE and (
            self.output_artifact_ref is None or self.input_artifact_ref is None
        ):
            raise ValueError("available judge requires input and output artifacts")
        return self


class WriterJudgePair(DomainModel):
    """Answer and Evidence-Support Judge receipts for one frozen Writer answer."""

    task_id: StableId
    answer_judge: WriterJudgeReceipt
    evidence_support_judge: WriterJudgeReceipt

    @model_validator(mode="after")
    def validate_pair(self) -> WriterJudgePair:
        if self.answer_judge.kind is not WriterJudgeKind.ANSWER:
            raise ValueError("answer judge receipt has the wrong kind")
        if self.evidence_support_judge.kind is not WriterJudgeKind.EVIDENCE_SUPPORT:
            raise ValueError("evidence-support judge receipt has the wrong kind")
        if (
            self.answer_judge.task_id != self.task_id
            or self.evidence_support_judge.task_id != self.task_id
        ):
            raise ValueError("judge pair task ids must match")
        return self


class V05FakeCampaignTaskReceipt(DomainModel):
    """One frozen fake-Writer readout after thin evaluator adaptation."""

    identity: V05ReadoutTaskIdentity
    freeze_receipt_id: StableId
    model_request_id: StableId
    response_ref: ArtifactRef
    record_ref: ArtifactRef
    raw_artifact_ref: ArtifactRef | None = None
    judges: WriterJudgePair
    canary_lock: U4L0CanaryVariableLock | None = None
    evaluator_adapted: Literal[True] = True
    gold_revealed: Literal[False] = False


class V05FakeCampaignReceipt(DomainModel):
    """Complete fake-Writer campaign over unique V0.5 Track A/B identities."""

    receipt_version: Literal["v05_fake_campaign_receipt.v1"] = "v05_fake_campaign_receipt.v1"
    campaign_id: StableId
    run_id: RunId
    freeze_receipt_id: StableId
    campaign_manifest_id: StableId | None = None
    campaign_manifest_ref: ArtifactRef | None = None
    canary_lock: U4L0CanaryVariableLock | None = None
    qa_count: int = Field(ge=0)
    context_count: int = Field(ge=0)
    tasks: tuple[V05FakeCampaignTaskReceipt, ...]

    @model_validator(mode="after")
    def validate_campaign(self) -> V05FakeCampaignReceipt:
        if (self.campaign_manifest_id is None) != (self.campaign_manifest_ref is None):
            raise ValueError("campaign manifest id and ref must be provided together")
        if self.campaign_manifest_id is not None and self.canary_lock is None:
            raise ValueError("campaign receipt requires the frozen canary lock")
        if (
            self.campaign_manifest_ref is not None
            and self.campaign_manifest_ref.media_type != V05_READOUT_CAMPAIGN_MANIFEST_MEDIA_TYPE
        ):
            raise ValueError("campaign manifest ref has the wrong media type")
        task_ids = tuple(task.identity.task_id for task in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("campaign task identities must be unique")
        qa = tuple(task for task in self.tasks if task.identity.track is V05ReadoutTrack.QA)
        context = tuple(
            task for task in self.tasks if task.identity.track is V05ReadoutTrack.CONTEXT
        )
        if len(qa) != self.qa_count or len(context) != self.context_count:
            raise ValueError("campaign counts do not match task identities")
        if any(task.identity.checkpoint_chapter == 100 for task in qa):
            raise ValueError("C100 must not carry a QA readout identity")
        if any(task.identity.checkpoint_chapter == 300 for task in context):
            raise ValueError("C300 must not carry a Context readout identity")
        if any(not task.evaluator_adapted or task.gold_revealed for task in self.tasks):
            raise ValueError("campaign must freeze and adapt without revealing Gold")
        freeze_ids = {task.freeze_receipt_id for task in self.tasks}
        if freeze_ids and freeze_ids != {self.freeze_receipt_id}:
            raise ValueError("campaign tasks must share the freeze receipt")
        if self.canary_lock is not None and any(
            task.canary_lock != self.canary_lock for task in self.tasks
        ):
            raise ValueError("campaign task receipts must share the campaign canary lock")
        return self


class MemoryIdentitySnapshot(DomainModel):
    """Durable Memory/Commit identity used to prove evaluation discard is a no-op."""

    commit_id: CommitId
    text_root: ArtifactId
    world_root: ArtifactId
    plan_root: ArtifactId
    profile_root: ArtifactId


class EvaluationNamespaceDiscardReceipt(DomainModel):
    """Logical close of evaluation artifacts. Does not mutate Memory or Canon."""

    receipt_id: StableId
    run_id: RunId
    discarded_refs: tuple[ArtifactRef, ...]
    memory_identity_before: MemoryIdentitySnapshot
    memory_identity_after: MemoryIdentitySnapshot

    @model_validator(mode="after")
    def validate_discard(self) -> EvaluationNamespaceDiscardReceipt:
        if self.memory_identity_before != self.memory_identity_after:
            raise ValueError("discarding evaluation artifacts must not change Memory identity")
        if not self.discarded_refs:
            raise ValueError("discard receipt requires at least one evaluation artifact")
        if any(
            not is_evaluation_artifact_media_type(ref.media_type) for ref in self.discarded_refs
        ):
            raise ValueError("discard receipt can only close evaluation-namespace artifacts")
        return self


class DurableEvidenceReport(DomainModel):
    """Usage and lineage rebuilt from ledger, freeze receipts, and evaluation artifacts."""

    run_id: RunId
    freeze_receipt_id: StableId
    phase_aggregates: tuple[ModelCallLedgerAggregate, ...]
    profile_namespaces: tuple[str, ...]
    writer_context_item_count: int = Field(ge=0)
    writer_used_item_count: int = Field(ge=0)
    cited_evidence_count: int = Field(ge=0)
    gold_hit_count: int | None = Field(default=None, ge=0)
    first_failure_layer: BenchmarkFailureLayer
    answer_judge_availability: WriterJudgeAvailability
    evidence_judge_availability: WriterJudgeAvailability
    cost_availability: ModelCostAvailability

    @model_validator(mode="after")
    def validate_report(self) -> DurableEvidenceReport:
        if len(self.profile_namespaces) != len(set(self.profile_namespaces)):
            raise ValueError("profile namespaces must be unique")
        pending_evidence = self.evidence_judge_availability in {
            WriterJudgeAvailability.PENDING,
            WriterJudgeAvailability.UNAVAILABLE,
        }
        if pending_evidence and self.gold_hit_count is not None:
            raise ValueError("pending evidence judge cannot report gold hits")
        if (
            self.evidence_judge_availability is WriterJudgeAvailability.AVAILABLE
            and self.gold_hit_count is None
        ):
            raise ValueError("available evidence judge requires a gold-hit count")
        return self


class U4SSeedTaskReport(DomainModel):
    """Public, evaluator-free receipt for one real U4-S Writer readout."""

    task_id: StableId
    track: V05ReadoutTrack
    checkpoint_chapter: int = Field(ge=0)
    information_profile: BenchmarkInformationProfile
    basis_commit_id: CommitId
    snapshot_id: StableId
    package_ref: ArtifactRef | None = None
    evidence_ledger_ref: ArtifactRef | None = None
    freeze_receipt_id: StableId
    writer_status: Literal["SCHEMA_VALID", "TYPED_FAILURE"]
    first_failure_layer: BenchmarkFailureLayer
    package_status: str = Field(min_length=1)
    semantic_status: str = Field(min_length=1)
    future_leakage_count: int = Field(ge=0)
    response_ref: ArtifactRef | None = None
    readout_record_ref: ArtifactRef | None = None
    raw_response_ref: ArtifactRef | None = None
    judge_receipt_refs: tuple[ArtifactRef, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> U4SSeedTaskReport:
        if self.writer_status == "SCHEMA_VALID":
            if self.first_failure_layer is not BenchmarkFailureLayer.NONE:
                raise ValueError("schema-valid Writer task cannot carry a failure layer")
            if self.response_ref is None or self.readout_record_ref is None:
                raise ValueError("schema-valid Writer task requires response and record refs")
        elif self.first_failure_layer is BenchmarkFailureLayer.NONE:
            raise ValueError("typed Writer failure requires a failure layer")
        return self


class U4SSeedReadoutReport(DomainModel):
    """Durable Track A/B seed evidence without answers, Gold, or future text."""

    report_schema: Literal["u4s-seed-readout-report.v1"] = Field(
        default="u4s-seed-readout-report.v1",
        alias="schema",
    )
    campaign_id: StableId
    mode: Literal["representative", "full"]
    run_id: RunId
    task_count: int = Field(ge=0)
    qa_count: int = Field(ge=0)
    context_count: int = Field(ge=0)
    chapters_ingested_once: int = Field(ge=0)
    checkpoint_count: int = Field(ge=0)
    tasks: tuple[U4SSeedTaskReport, ...]
    first_failure_layer: BenchmarkFailureLayer
    discard_receipt_ref: ArtifactRef | None = None
    status: Literal["COMPLETED", "REVIEW_REQUIRED"]

    @model_validator(mode="after")
    def validate_counts(self) -> U4SSeedReadoutReport:
        if self.task_count != len(self.tasks):
            raise ValueError("U4-S report task count does not match task receipts")
        if self.qa_count != sum(task.track is V05ReadoutTrack.QA for task in self.tasks):
            raise ValueError("U4-S report QA count does not match task receipts")
        if self.context_count != sum(task.track is V05ReadoutTrack.CONTEXT for task in self.tasks):
            raise ValueError("U4-S report Context count does not match task receipts")
        task_ids = tuple(task.task_id for task in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("U4-S report task identities must be unique")
        failures = tuple(
            task.first_failure_layer
            for task in self.tasks
            if task.first_failure_layer is not BenchmarkFailureLayer.NONE
        )
        expected = failures[0] if failures else BenchmarkFailureLayer.NONE
        if self.first_failure_layer is not expected:
            raise ValueError("U4-S report first failure layer is not task-derived")
        if self.status == "COMPLETED" and failures:
            raise ValueError("completed U4-S report cannot contain a typed failure")
        if self.status == "REVIEW_REQUIRED" and not failures:
            raise ValueError("review-required U4-S report needs a typed failure")
        return self
