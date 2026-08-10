"""Stage 2 agent harness, bootstrap, tool, and scenario contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, JsonValue, StringConstraints, model_validator

from novel_agent.domain.artifacts import ArtifactRef, PlanRootRef, RootManifest
from novel_agent.domain.base import DomainModel
from novel_agent.domain.changes import ObservedChangeSet
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import (
    CandidatePool,
    FreshnessDecision,
    RetrievalChannel,
    Stage1ContextPackage,
    Stage1MemoryNeed,
)
from novel_agent.domain.retrieval_routing import ChannelFailureCode
from novel_agent.domain.text import EvidenceRef
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile as BenchmarkInformationProfile,
)
from novel_agent.domain.writer_context import (
    BenchmarkTaskContract,
    ClaimSupportGroup,
    ClaimSupportReceipt,
    ClaimVariant,
    ContextAssemblyStatus,
    CutoffAttestation,
    EvidenceLedger,
    FreezeReceipt,
    WriterContextPackage,
)


class AgentType(StrEnum):
    PLANNER = "planner"
    MEMORY_CURATOR = "memory_curator"
    MEMORY_CONTROLLER = "memory_controller"
    MEMORY_GUARDIAN = "memory_guardian"
    WRITER = "writer"
    EDITOR = "editor"
    CANDIDATE_OBSERVER = "candidate_observer"
    PLAN_REVIEWER = "plan_reviewer"


class AgentMode(StrEnum):
    PROJECT_BOOTSTRAP = "project_bootstrap"
    STORY = "story"
    ARC_VOLUME = "arc_volume"
    CHAPTER = "chapter"
    SCENE = "scene"
    REPLAN = "replan"
    BOOTSTRAP = "bootstrap"
    REPLAY = "replay"
    CURATOR_REPAIR = "curator_repair"
    BOUNDED_R2 = "bounded_r2"
    RISK_REVIEW = "risk_review"
    DRAFT = "draft"
    CONTINUE = "continue"
    MAJOR_REWRITE = "major_rewrite"
    REVIEW = "review"
    LOCAL_REPAIR = "local_repair"
    OBSERVE = "observe"
    CHAPTER_SET = "chapter_set"


class ContractRef(DomainModel):
    contract_id: StableId
    version: SchemaVersion
    content_hash: ArtifactId


class PromptContractRef(ContractRef):
    render_fingerprint: ArtifactId


class SkillContractRef(ContractRef):
    pass


class ToolPermission(StrEnum):
    READ = "read"
    PROPOSE = "propose"


class ToolPolicy(DomainModel):
    policy_id: StableId
    version: SchemaVersion
    content_hash: ArtifactId
    allowed_tools: tuple[str, ...]
    denied_tools: tuple[str, ...] = ()
    permission: ToolPermission = ToolPermission.READ
    max_rounds: int = Field(default=1, ge=1)
    max_tool_calls: int = Field(default=1, ge=0)
    max_query_rewrites_per_need: int = Field(default=0, ge=0)
    wall_clock_budget_ms: int = Field(default=30_000, ge=1)
    token_budget: int = Field(default=8_000, ge=1)

    @model_validator(mode="after")
    def validate_tool_sets(self) -> ToolPolicy:
        if len(self.allowed_tools) != len(set(self.allowed_tools)):
            raise ValueError("allowed tool names must be unique")
        if len(self.denied_tools) != len(set(self.denied_tools)):
            raise ValueError("denied tool names must be unique")
        if set(self.allowed_tools) & set(self.denied_tools):
            raise ValueError("a tool cannot be both allowed and denied")
        return self


class AgentSpec(DomainModel):
    agent_id: StableId
    agent_type: AgentType
    mode: AgentMode
    version: SchemaVersion
    content_hash: ArtifactId
    input_schema: ContractRef
    output_schema: ContractRef
    system_prompt: PromptContractRef
    task_prompt: PromptContractRef
    skills: tuple[SkillContractRef, ...]
    tool_policy: ToolPolicy


class Stage2ConfigurationManifest(DomainModel):
    manifest_id: StableId
    schema_version: SchemaVersion
    agent_specs: tuple[AgentSpec, ...]
    prompt_contracts: tuple[PromptContractRef, ...]
    skill_contracts: tuple[SkillContractRef, ...]
    tool_policies: tuple[ToolPolicy, ...]
    schema_artifacts: tuple[ArtifactRef, ...] = ()
    configuration_fingerprint: ArtifactId

    @model_validator(mode="after")
    def validate_inventory(self) -> Stage2ConfigurationManifest:
        agent_ids = tuple(spec.agent_id for spec in self.agent_specs)
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("configuration manifest AgentSpec ids must be unique")
        prompt_keys = {
            (item.contract_id, item.version, item.content_hash) for item in self.prompt_contracts
        }
        skill_keys = {
            (item.contract_id, item.version, item.content_hash) for item in self.skill_contracts
        }
        tool_keys = {
            (item.policy_id, item.version, item.content_hash) for item in self.tool_policies
        }
        if len(prompt_keys) != len(self.prompt_contracts):
            raise ValueError("configuration manifest prompt contracts must be unique")
        if len(skill_keys) != len(self.skill_contracts):
            raise ValueError("configuration manifest skill contracts must be unique")
        if len(tool_keys) != len(self.tool_policies):
            raise ValueError("configuration manifest ToolPolicies must be unique")
        schema_ids = tuple(item.artifact_id for item in self.schema_artifacts)
        if len(schema_ids) != len(set(schema_ids)):
            raise ValueError("configuration manifest schema artifacts must be unique")
        for spec in self.agent_specs:
            required_prompts = (spec.system_prompt, spec.task_prompt)
            if any(
                (item.contract_id, item.version, item.content_hash) not in prompt_keys
                for item in required_prompts
            ):
                raise ValueError("AgentSpec references an unlisted prompt contract")
            if any(
                (item.contract_id, item.version, item.content_hash) not in skill_keys
                for item in spec.skills
            ):
                raise ValueError("AgentSpec references an unlisted skill contract")
            policy = spec.tool_policy
            if (policy.policy_id, policy.version, policy.content_hash) not in tool_keys:
                raise ValueError("AgentSpec references an unlisted ToolPolicy")
        return self


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUSPENDED = "suspended"
    REJECTED = "rejected"


class SkillExecutionReceipt(DomainModel):
    receipt_id: StableId
    run_id: RunId
    task_id: TaskId
    skill: SkillContractRef
    agent_type: AgentType
    agent_mode: AgentMode
    base_commit: CommitId | None = None
    context_manifest: ArtifactId | None = None
    input_artifacts: tuple[ArtifactRef, ...] = ()
    output_artifacts: tuple[ArtifactRef, ...] = ()
    completed_checkpoints: tuple[str, ...] = ()
    skipped_checkpoints: tuple[str, ...] = ()
    tool_call_ids: tuple[StableId, ...] = ()
    unresolved: tuple[str, ...] = ()
    escalations: tuple[str, ...] = ()
    status: ExecutionStatus
    latency_ms: int = Field(ge=0)


class AgentExecutionReceipt(DomainModel):
    receipt_id: StableId
    run_id: RunId
    task_id: TaskId
    agent_spec: ContractRef
    agent_type: AgentType
    agent_mode: AgentMode
    prompt_fingerprint: ArtifactId
    configuration_fingerprint: ArtifactId
    base_commit: CommitId | None = None
    input_artifacts: tuple[ArtifactRef, ...] = ()
    output_artifacts: tuple[ArtifactRef, ...] = ()
    skill_receipts: tuple[SkillExecutionReceipt, ...] = ()
    model_call_ids: tuple[StableId, ...] = ()
    tool_call_ids: tuple[StableId, ...] = ()
    unresolved: tuple[str, ...] = ()
    escalations: tuple[str, ...] = ()
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime
    latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_timing(self) -> AgentExecutionReceipt:
        if self.completed_at < self.started_at:
            raise ValueError("agent execution completion precedes start")
        return self


class SourceClass(StrEnum):
    AUTHOR_INITIAL_BRIEF = "author_initial_brief"
    AUTHOR_KNOWN_FUTURE_PLAN = "author_known_future_plan"
    BASELINE_SETTING = "baseline_setting"
    CHAPTER_TEXT = "chapter_text_n"
    RETROSPECTIVE_SUMMARY = "retrospective_summary"
    FUTURE_TEXT_PRIVATE = "future_text_private"
    READ_GOLD = "read_gold"
    REPLAY_GOLD = "replay_gold"
    STYLE_GUIDE = "style_guide"
    EXTERNAL_REFERENCE = "external_reference"


class SourceDestination(StrEnum):
    TEXT = "text"
    PLAN = "plan"
    WORLD = "world"
    REFERENCE = "reference"
    PROJECT_PROFILE = "project_profile"
    EVALUATION = "evaluation"


class BootstrapSource(DomainModel):
    source_id: StableId
    source_class: SourceClass
    media_type: str = Field(min_length=1)
    content_hash: ArtifactId
    byte_length: int = Field(ge=0)
    artifact_ref: ArtifactRef
    earliest_visible_chapter: int | None = Field(default=None, ge=0)
    chapter_index: int | None = Field(default=None, ge=0)
    evaluator_only: bool = False

    @model_validator(mode="after")
    def validate_visibility(self) -> BootstrapSource:
        private = {
            SourceClass.RETROSPECTIVE_SUMMARY,
            SourceClass.FUTURE_TEXT_PRIVATE,
            SourceClass.READ_GOLD,
            SourceClass.REPLAY_GOLD,
        }
        if self.source_class in private and not self.evaluator_only:
            raise ValueError("private/evaluation source must be evaluator_only")
        if self.source_class is SourceClass.CHAPTER_TEXT and (
            self.chapter_index is None or self.earliest_visible_chapter != self.chapter_index
        ):
            raise ValueError("chapter source visibility must equal its chapter index")
        return self


class SourceClassification(DomainModel):
    source_id: StableId
    source_class: SourceClass
    allowed_destinations: tuple[SourceDestination, ...]
    forbidden_destinations: tuple[SourceDestination, ...] = ()
    classification_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_destinations(self) -> SourceClassification:
        if set(self.allowed_destinations) & set(self.forbidden_destinations):
            raise ValueError("source destination cannot be both allowed and forbidden")
        if self.source_class in {
            SourceClass.RETROSPECTIVE_SUMMARY,
            SourceClass.FUTURE_TEXT_PRIVATE,
            SourceClass.READ_GOLD,
            SourceClass.REPLAY_GOLD,
        } and any(
            destination is not SourceDestination.EVALUATION
            for destination in self.allowed_destinations
        ):
            raise ValueError("evaluator-only source cannot target a canonical root")
        return self


class ProjectBootstrapBundle(DomainModel):
    bundle_id: StableId
    project_id: ProjectId
    schema_version: SchemaVersion
    sources: tuple[BootstrapSource, ...]
    bundle_hash: ArtifactId

    @model_validator(mode="after")
    def validate_sources(self) -> ProjectBootstrapBundle:
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("bootstrap source ids must be unique")
        return self


class BootstrapStrategy(StrEnum):
    NORMALIZE_ONLY = "normalize_only"
    DEVELOP_CANDIDATES = "develop_candidates"


class ProjectBootstrapRequest(DomainModel):
    request_id: StableId
    run_id: RunId
    task_id: TaskId
    project_id: ProjectId
    bundle_hash: ArtifactId
    strategy: BootstrapStrategy
    approved_source_ids: tuple[StableId, ...]


class PlanningTask(DomainModel):
    planning_task_id: StableId
    project_id: ProjectId
    mode: AgentMode
    base_commit: CommitId | None = None
    source_ids: tuple[StableId, ...] = ()
    creative_scope: tuple[str, ...] = ()
    strategy: BootstrapStrategy | None = None

    @model_validator(mode="after")
    def validate_mode_basis(self) -> PlanningTask:
        planner_modes = {
            AgentMode.PROJECT_BOOTSTRAP,
            AgentMode.STORY,
            AgentMode.ARC_VOLUME,
            AgentMode.CHAPTER,
            AgentMode.SCENE,
            AgentMode.REPLAN,
        }
        if self.mode not in planner_modes:
            raise ValueError("PlanningTask requires a Planner mode")
        if self.mode is AgentMode.PROJECT_BOOTSTRAP:
            if self.base_commit is not None or self.strategy is None:
                raise ValueError("PROJECT_BOOTSTRAP requires strategy and no base commit")
        elif self.base_commit is None or self.strategy is not None:
            raise ValueError("post-Genesis planning requires base commit and no bootstrap strategy")
        return self


class ProposalProvenance(StrEnum):
    AUTHOR_SUPPLIED = "author_supplied"
    PLANNER_PROPOSED = "planner_proposed"


class ProposedItem(DomainModel):
    item_id: StableId
    kind: str = Field(min_length=1)
    payload: dict[str, JsonValue]
    provenance: ProposalProvenance
    source_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_origin(self) -> ProposedItem:
        if self.provenance is ProposalProvenance.AUTHOR_SUPPLIED and not self.source_ids:
            raise ValueError("author-supplied proposal requires a source")
        return self


class PlanDeviationRecordCandidate(DomainModel):
    deviation_id: StableId
    summary: str = Field(min_length=1)
    affected_plan_item_ids: tuple[StableId, ...] = Field(min_length=1)
    invalidated_artifact_ids: tuple[ArtifactId, ...] = ()
    replacement_item_ids: tuple[StableId, ...] = ()


class PlannerProposalDraft(DomainModel):
    mode: AgentMode
    strategy: BootstrapStrategy | None = None
    project_intent_items: tuple[ProposedItem, ...] = ()
    plan_items: tuple[ProposedItem, ...] = ()
    world_design_items: tuple[ProposedItem, ...] = ()
    profile_items: tuple[ProposedItem, ...] = ()
    deviations: tuple[PlanDeviationRecordCandidate, ...] = ()
    alternatives: tuple[str, ...] = ()
    selection_rationale: str | None = None
    unresolved: tuple[str, ...] = ()
    coverage: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_mode_output(self) -> PlannerProposalDraft:
        planner_modes = {
            AgentMode.PROJECT_BOOTSTRAP,
            AgentMode.STORY,
            AgentMode.ARC_VOLUME,
            AgentMode.CHAPTER,
            AgentMode.SCENE,
            AgentMode.REPLAN,
        }
        if self.mode not in planner_modes:
            raise ValueError("Planner draft requires a Planner mode")
        if self.mode is AgentMode.PROJECT_BOOTSTRAP:
            if self.strategy is None:
                raise ValueError("PROJECT_BOOTSTRAP draft requires a strategy")
        elif self.strategy is not None or self.project_intent_items:
            raise ValueError("only PROJECT_BOOTSTRAP may emit bootstrap intent/strategy")
        if self.mode is AgentMode.REPLAN:
            if not self.deviations:
                raise ValueError("REPLAN draft requires an explicit deviation record")
        elif self.deviations:
            raise ValueError("only REPLAN may emit deviation records")
        if (
            self.mode is not AgentMode.PROJECT_BOOTSTRAP
            and not self.plan_items
            and not self.unresolved
        ):
            raise ValueError("planning draft requires plan items or explicit unresolved gaps")
        if self.alternatives and not self.selection_rationale:
            raise ValueError("planning alternatives require a selection rationale")
        if self.strategy is BootstrapStrategy.NORMALIZE_ONLY and any(
            item.provenance is ProposalProvenance.PLANNER_PROPOSED
            for item in (
                *self.project_intent_items,
                *self.plan_items,
                *self.world_design_items,
                *self.profile_items,
            )
        ):
            raise ValueError("NORMALIZE_ONLY cannot introduce Planner-proposed items")
        return self


class ProjectIntentModel(DomainModel):
    intent_id: StableId
    project_id: ProjectId
    strategy: BootstrapStrategy
    items: tuple[ProposedItem, ...]
    source_ids: tuple[StableId, ...]
    unresolved: tuple[str, ...] = ()
    coverage: float = Field(ge=0, le=1)


class WorldDesignProposal(DomainModel):
    proposal_id: StableId
    project_id: ProjectId
    items: tuple[ProposedItem, ...]
    unresolved: tuple[str, ...] = ()


class ProjectProfileProposal(DomainModel):
    proposal_id: StableId
    project_id: ProjectId
    items: tuple[ProposedItem, ...]
    unresolved: tuple[str, ...] = ()


class PlanProposal(DomainModel):
    proposal_id: StableId
    project_id: ProjectId
    mode: AgentMode
    strategy: BootstrapStrategy | None = None
    base_commit: CommitId | None = None
    items: tuple[ProposedItem, ...]
    unresolved: tuple[str, ...] = ()
    coverage: float = Field(ge=0, le=1)
    receipt: AgentExecutionReceipt


class PlannerExecutionResult(DomainModel):
    mode: AgentMode
    project_intent: ProjectIntentModel | None = None
    plan_proposal: PlanProposal
    world_design: WorldDesignProposal | None = None
    project_profile: ProjectProfileProposal | None = None
    deviations: tuple[PlanDeviationRecordCandidate, ...] = ()
    output_artifact: ArtifactRef
    receipt: AgentExecutionReceipt

    @model_validator(mode="after")
    def validate_planner_result(self) -> PlannerExecutionResult:
        if self.receipt.agent_type is not AgentType.PLANNER:
            raise ValueError("Planner result requires a Planner receipt")
        if self.receipt.agent_mode is not self.mode or self.plan_proposal.mode is not self.mode:
            raise ValueError("Planner result mode must match proposal and receipt")
        if self.plan_proposal.receipt != self.receipt:
            raise ValueError("PlanProposal must carry the enclosing Planner receipt")
        if self.mode is AgentMode.PROJECT_BOOTSTRAP and self.project_intent is None:
            raise ValueError("PROJECT_BOOTSTRAP result requires ProjectIntentModel")
        if self.mode is not AgentMode.PROJECT_BOOTSTRAP and self.project_intent is not None:
            raise ValueError("only PROJECT_BOOTSTRAP result may carry ProjectIntentModel")
        if self.mode is AgentMode.REPLAN and not self.deviations:
            raise ValueError("REPLAN result requires deviation records")
        return self


class WorldPatchCandidate(DomainModel):
    proposal_id: StableId
    project_id: ProjectId
    base_commit: CommitId | None = None
    items: tuple[ProposedItem, ...]
    origin_source_ids: tuple[StableId, ...]
    unresolved_claims: tuple[str, ...] = ()
    extraction_coverage: float = Field(ge=0, le=1)
    receipt: AgentExecutionReceipt


class CuratorBootstrapDraft(DomainModel):
    """Untrusted structured output produced by Memory Curator BOOTSTRAP."""

    items: tuple[ProposedItem, ...] = ()
    unresolved_claims: tuple[str, ...] = ()
    extraction_coverage: float = Field(ge=0, le=1)


class ReferenceAsset(DomainModel):
    asset_id: StableId
    source_id: StableId
    source_class: SourceClass
    artifact: ArtifactRef
    title: str | None = Field(default=None, min_length=1)


class ReferenceRootDocument(DomainModel):
    root_hash: ArtifactId
    schema_version: SchemaVersion
    assets: tuple[ReferenceAsset, ...]

    @model_validator(mode="after")
    def validate_assets(self) -> ReferenceRootDocument:
        if len({asset.asset_id for asset in self.assets}) != len(self.assets):
            raise ValueError("reference asset ids must be unique")
        if len({asset.source_id for asset in self.assets}) != len(self.assets):
            raise ValueError("reference source ids must be unique")
        return self


class ProjectProfileRootDocument(DomainModel):
    root_hash: ArtifactId
    schema_version: SchemaVersion
    style_profile: dict[str, JsonValue] = Field(default_factory=dict)
    capability_profile: dict[str, JsonValue] = Field(default_factory=dict)
    agent_specs: tuple[ContractRef, ...]
    prompt_contracts: tuple[PromptContractRef, ...]
    skill_contracts: tuple[SkillContractRef, ...]
    tool_policies: tuple[ContractRef, ...]
    model_profiles: tuple[str, ...]

    @model_validator(mode="after")
    def validate_pins(self) -> ProjectProfileRootDocument:
        pinned = (
            self.agent_specs,
            self.prompt_contracts,
            self.skill_contracts,
            self.tool_policies,
            self.model_profiles,
        )
        if any(not values for values in pinned):
            raise ValueError(
                "project profile must pin agent, prompt, skill, tool, and model versions"
            )
        for values in pinned[:-1]:
            identities = tuple((value.contract_id, value.version) for value in values)
            if len(identities) != len(set(identities)):
                raise ValueError("project profile contract pins must be unique")
        return self


class AuthorApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_CHANGES = "needs_changes"


class AuthorApprovalRequest(DomainModel):
    approval_request_id: StableId
    project_id: ProjectId
    bootstrap_bundle_id: StableId
    candidate_manifest_hash: ArtifactId
    validation_report_id: StableId
    status: AuthorApprovalStatus = AuthorApprovalStatus.PENDING
    requested_at: datetime


class AuthorApprovalDecision(DomainModel):
    decision_id: StableId
    approval_request_id: StableId
    project_id: ProjectId
    candidate_manifest_hash: ArtifactId
    validation_report_id: StableId
    status: AuthorApprovalStatus
    author_id: StableId
    reason: str = Field(min_length=1)
    decided_at: datetime

    @model_validator(mode="after")
    def validate_terminal(self) -> AuthorApprovalDecision:
        if self.status is AuthorApprovalStatus.PENDING:
            raise ValueError("author decision cannot remain pending")
        return self


class GenesisCommitReceipt(DomainModel):
    receipt_id: StableId
    project_id: ProjectId
    bootstrap_bundle_id: StableId
    candidate_manifest_hash: ArtifactId
    validation_report_id: StableId
    approval_decision_id: StableId
    commit_id: CommitId
    manifest: RootManifest
    idempotent_replay: bool = False
    committed_at: datetime


class BootstrapSourceAuditEntry(DomainModel):
    source_id: StableId
    source_class: SourceClass
    content_hash: ArtifactId
    evaluator_only: bool
    allowed_destinations: tuple[SourceDestination, ...]
    forbidden_destinations: tuple[SourceDestination, ...]


class BootstrapAuditReport(DomainModel):
    report_id: StableId
    project_id: ProjectId
    bootstrap_bundle_id: StableId
    bundle_hash: ArtifactId
    sources: tuple[BootstrapSourceAuditEntry, ...]
    planner_proposal_id: StableId
    planner_coverage: float = Field(ge=0.0, le=1.0)
    planner_unresolved: tuple[str, ...]
    curator_proposal_id: StableId
    curator_coverage: float = Field(ge=0.0, le=1.0)
    curator_unresolved: tuple[str, ...]
    candidate_manifest_hash: ArtifactId
    validation_report_id: StableId
    validation_status: str = Field(min_length=1)
    approval_request_id: StableId | None = None
    approval_status: AuthorApprovalStatus | None = None
    genesis_receipt_id: StableId | None = None
    genesis_commit_id: CommitId | None = None
    freshness: FreshnessDecision | None = None
    blockers: tuple[str, ...] = ()
    complete: bool
    configuration_fingerprint: ArtifactId
    created_at: datetime

    @model_validator(mode="after")
    def validate_completion(self) -> BootstrapAuditReport:
        if self.complete and self.blockers:
            raise ValueError("complete Bootstrap audit cannot retain blockers")
        if self.complete and (
            self.approval_status is not AuthorApprovalStatus.APPROVED
            or self.genesis_receipt_id is None
            or self.genesis_commit_id is None
            or self.freshness is None
        ):
            raise ValueError("complete Bootstrap audit requires approval, Genesis, and freshness")
        if (self.genesis_receipt_id is None) != (self.genesis_commit_id is None):
            raise ValueError("Bootstrap audit Genesis receipt and commit must appear together")
        return self


class RequiredSnapshotPolicy(StrEnum):
    EXACT = "exact"
    ALLOW_CANONICAL_DEGRADED = "allow_canonical_degraded"


class AccessScope(StrEnum):
    WRITER_SAFE = "writer_safe"
    AUTHOR_PLANNING = "author_planning"
    EVALUATOR = "evaluator"


class PublicCheckpointCase(DomainModel):
    """Checkpoint case view stripped of all Gold, future text, and evaluator data.

    This type must be the ONLY case representation passed to freeze-before-evaluation
    code paths.  Full BenchmarkCaseManifest (with Gold, private future text roots,
    etc.) is loaded only by the Evaluator AFTER context freeze is persisted.
    """

    case_id: StableId
    project_id: ProjectId
    target_range: tuple[int, int]
    history_range: tuple[int, int]
    task_contract: BenchmarkTaskContract
    plan_root_ref: PlanRootRef | None = None
    public_input_hash: ArtifactId

    @model_validator(mode="after")
    def validate_public_contract(self) -> PublicCheckpointCase:
        if self.task_contract.checkpoint_chapter != self.history_range[1]:
            raise ValueError("public task checkpoint does not match history range")
        if (
            self.task_contract.target_chapter_start,
            self.task_contract.target_chapter_end,
        ) != self.target_range:
            raise ValueError("public task target range does not match checkpoint case")
        profile = self.task_contract.information_profile
        if (
            profile
            in {
                BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                BenchmarkInformationProfile.TASK_INTENT_ONLY,
            }
            and self.plan_root_ref is not None
        ):
            raise ValueError(f"{profile.value} public case cannot expose a PlanRoot")
        if profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED:
            if self.plan_root_ref is None:
                raise ValueError("APC public case requires a verified PlanRoot")
            if (
                self.task_contract.planning_context_ref is None
                or self.task_contract.planning_context_hash is None
            ):
                raise ValueError("APC public case requires a bound planning context")
        return self


class PublicBenchmarkConfig(DomainModel):
    """Benchmark configuration stripped of all Gold content hashes.

    Must be the ONLY bundle-level type passed to freeze-before-evaluation
    code paths.  The full BenchmarkBundle (with Gold-containing content_hash)
    is loaded only by the Evaluator after context freeze.
    """

    schema_version: SchemaVersion
    configuration_fingerprint: ArtifactId
    expected_profiles: tuple[str, ...]


class RetrievalBudget(DomainModel):
    max_rounds: int = Field(default=3, ge=1)
    max_tool_calls: int = Field(default=12, ge=1)
    max_query_rewrites_per_need: int = Field(default=2, ge=0)
    max_candidates: int = Field(default=100, ge=1, le=100)
    max_anchor_expansions: int = Field(default=8, ge=0)
    max_full_chapter_reads: int = Field(default=0, ge=0)
    wall_clock_budget_ms: int = Field(default=30_000, ge=1)
    token_budget: int = Field(default=12_000, ge=1)


class ContextBudget(DomainModel):
    token_budget: int = Field(ge=1)
    mandatory_reserve_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_reserve(self) -> ContextBudget:
        if self.mandatory_reserve_tokens > self.token_budget:
            raise ValueError("mandatory reserve exceeds context budget")
        return self


class MemoryResolutionRequest(DomainModel):
    request_id: StableId
    run_id: RunId
    task_id: TaskId
    project_id: ProjectId
    base_commit: CommitId
    snapshot_id: StableId
    required_snapshot_policy: RequiredSnapshotPolicy
    task_contract: str = Field(min_length=1)
    initial_memory_needs: tuple[Stage1MemoryNeed, ...]
    worldline: str = Field(min_length=1)
    narrative_chapter: int = Field(ge=0)
    access_scope: AccessScope
    allow_future_plan: bool = False
    retrieval_budget: RetrievalBudget
    context_budget: ContextBudget

    @model_validator(mode="after")
    def validate_needs(self) -> MemoryResolutionRequest:
        for need in self.initial_memory_needs:
            if need.run_id != self.run_id or need.task_id != self.task_id:
                raise ValueError("memory need must belong to resolution run and task")
            if need.base_commit != self.base_commit:
                raise ValueError("memory need base commit mismatch")
        if self.access_scope is AccessScope.WRITER_SAFE and self.allow_future_plan:
            raise ValueError("writer-safe scope cannot access future plan")
        return self


class SelectionDecision(StrEnum):
    SELECTED = "selected"
    EXCLUDED = "excluded"


class MemorySelection(DomainModel):
    unit_id: StableId
    need_ids: tuple[StableId, ...]
    candidate_pool: CandidatePool
    decision: SelectionDecision
    reason: str = Field(min_length=1)
    mandatory: bool = False


class EvidenceLedgerEntry(DomainModel):
    unit_id: StableId
    evidence_refs: tuple[EvidenceRef, ...]
    basis_commit: CommitId
    snapshot_id: StableId
    access_scope: AccessScope


class ContextAssemblySpec(DomainModel):
    selected_unit_ids: tuple[StableId, ...] = ()
    mandatory_unit_ids: tuple[StableId, ...] = ()
    token_budget: int = Field(default=1, ge=1)
    reduction_allowed: bool = True
    selected_support_group_ids: tuple[StableId, ...] = ()
    mandatory_support_group_ids: tuple[StableId, ...] = ()
    allowed_claim_variant_ids_by_support_group: dict[str, tuple[StableId, ...]] = Field(
        default_factory=dict
    )
    mandatory_claim_variant_ids: tuple[StableId, ...] = ()
    closed_need_facet_ids: tuple[StableId, ...] = ()
    unresolved_need_facet_ids: tuple[StableId, ...] = ()
    ordered_optional_support_group_ids: tuple[StableId, ...] = ()
    writer_token_budget: int | None = Field(default=None, ge=1)
    evidence_ledger_token_budget: int = Field(default=12_000, ge=1)
    reduction_policy: str = Field(default="receipt_bound_variants_only", min_length=1)
    selection_policy_version: str = Field(default="legacy_unit_selection.v1", min_length=1)

    @model_validator(mode="after")
    def validate_mandatory(self) -> ContextAssemblySpec:
        if not set(self.mandatory_unit_ids).issubset(self.selected_unit_ids):
            raise ValueError("mandatory assembly units must be selected")
        selected_groups = set(self.selected_support_group_ids)
        if not set(self.mandatory_support_group_ids).issubset(selected_groups):
            raise ValueError("mandatory support groups must be selected")
        if not set(self.ordered_optional_support_group_ids).issubset(selected_groups):
            raise ValueError("optional support groups must be selected")
        if self.selected_support_group_ids:
            if self.writer_token_budget is None:
                raise ValueError("support-aware assembly requires a Writer token budget")
            if set(self.allowed_claim_variant_ids_by_support_group) != {
                item.root for item in self.selected_support_group_ids
            }:
                raise ValueError("support-aware assembly requires variants for every group")
            allowed_variants = {
                variant_id
                for values in self.allowed_claim_variant_ids_by_support_group.values()
                for variant_id in values
            }
            if not set(self.mandatory_claim_variant_ids).issubset(allowed_variants):
                raise ValueError("mandatory claim variants must be allowed by the spec")
            if set(self.closed_need_facet_ids).intersection(self.unresolved_need_facet_ids):
                raise ValueError("closed and unresolved Need facets must be disjoint")
        return self


class ControllerStopReason(StrEnum):
    SUFFICIENT = "sufficient"
    MANDATORY_GAP_UNRESOLVED = "mandatory_gap_unresolved"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FRESHNESS_BLOCKED = "freshness_blocked"
    ACCESS_BLOCKED = "access_blocked"
    CONFLICT_REQUIRES_REVIEW = "conflict_requires_review"
    TOOL_FAILURE = "tool_failure"
    NO_ADDITIONAL_EVIDENCE = "no_additional_evidence"


class SufficiencyReport(DomainModel):
    """Auditable stopping evidence for a bounded RoutePlan execution."""

    mandatory_gaps_closed: bool
    evidence_strength_satisfied: bool
    entity_coverage: float = Field(ge=0, le=1)
    temporal_coverage: float = Field(ge=0, le=1)
    plan_obligation_coverage: float = Field(ge=0, le=1)
    conflicting_evidence: tuple[StableId, ...] = ()
    unresolved_unknowns: tuple[str, ...] = ()
    scope_access_warnings: tuple[str, ...] = ()
    freshness_warnings: tuple[str, ...] = ()
    new_information_gain_by_round: tuple[int, ...] = ()
    recommended_fallback: str | None = None
    stop_reason: ControllerStopReason

    @model_validator(mode="after")
    def validate_stopping_claim(self) -> SufficiencyReport:
        if self.stop_reason is ControllerStopReason.SUFFICIENT and (
            not self.mandatory_gaps_closed
            or not self.evidence_strength_satisfied
            or self.unresolved_unknowns
            or self.freshness_warnings
        ):
            raise ValueError(
                "sufficient report cannot retain a mandatory, evidence, or freshness gap"
            )
        if any(value < 0 for value in self.new_information_gain_by_round):
            raise ValueError("new information gain must be non-negative")
        return self


class ControllerPolicyAction(StrEnum):
    CALL_TOOL = "call_tool"
    STOP = "stop"
    EXECUTE_PLAN = "execute_plan"


class ControllerMode(StrEnum):
    """Production vs diagnostic controller execution modes (WP0)."""

    DETERMINISTIC = "deterministic"
    STANDALONE_AGENTIC_DIAGNOSTIC = "standalone_agentic_diagnostic"
    DETERMINISTIC_PLUS_AGENTIC_DELTA = "deterministic_plus_agentic_delta"


class CuratorEvidenceContract(StrEnum):
    """Evidence selection contract version for Curator proposals (WP0/WP4)."""

    LEGACY_OFFSET_V1 = "legacy_offset_v1"
    CANDIDATE_ID_V2 = "candidate_id_v2"


class EvidenceSupportGateMode(StrEnum):
    """Semantic support gate enforcement mode (WP0/WP5)."""

    DISABLED = "disabled"
    AUDIT_ONLY = "audit_only"
    ENFORCE_PRE_CANDIDATE = "enforce_pre_candidate"


class QualityRepairFeatureFlags(DomainModel):
    """Explicit feature flags for controller/curator quality repair."""

    controller_mode: ControllerMode = ControllerMode.DETERMINISTIC
    curator_evidence_contract: CuratorEvidenceContract = CuratorEvidenceContract.CANDIDATE_ID_V2
    evidence_support_gate: EvidenceSupportGateMode = EvidenceSupportGateMode.ENFORCE_PRE_CANDIDATE
    max_controller_decision_model_calls: int = Field(default=2, ge=0, le=8)
    max_agentic_actions: int = Field(default=8, ge=0, le=32)


class ControllerActionPhase(StrEnum):
    MANDATORY = "mandatory"
    PRIMARY = "primary"
    FALLBACK = "fallback"


class RegisteredControllerAction(DomainModel):
    """One RoutePlan-legal controller action shared by prompt and adapter."""

    action_id: StableId
    need_id: StableId
    route_step_id: StableId | None = None
    tool_name: str = Field(min_length=1, max_length=64)
    retrieval_channel: RetrievalChannel
    requirement: str = Field(min_length=1, max_length=32)
    phase: ControllerActionPhase
    fallback_condition: str | None = None
    query_intent: str = Field(min_length=1, max_length=64)


class ControllerRetrievalPlanDraft(DomainModel):
    """Untrusted batch Agentic plan: opaque action IDs only (WP3)."""

    selected_action_ids: tuple[str, ...] = ()
    stop_after_action_ids: tuple[str, ...] = ()
    rationale_code: str | None = None
    stop_reason: str | None = None


class ControllerPolicyDraft(DomainModel):
    """Untrusted model proposal normalized by the Controller policy adapter.

    Routing authority remains in ``ControllerPolicyDecision``.  These fields
    deliberately accept plain strings so a formatting defect cannot bypass the
    adapter's bounded repair against the sealed ``available_actions`` registry.
    """

    action: str | None = None
    need_id: str | None = None
    tool_name: str | None = None
    stop_reason: str | None = None
    rationale_code: str | None = None
    model_call_id: str | None = None
    selected_action_ids: tuple[str, ...] = ()
    stop_after_action_ids: tuple[str, ...] = ()


class ControllerPolicyDecision(DomainModel):
    action: ControllerPolicyAction
    need_id: StableId | None = None
    tool_name: str | None = Field(default=None, min_length=1, max_length=64)
    stop_reason: ControllerStopReason | None = None
    rationale_code: str = Field(min_length=1, max_length=64)
    model_call_id: StableId | None = None
    selected_action_ids: tuple[StableId, ...] = ()
    pending_action_ids: tuple[StableId, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def normalize_action_shape(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        action = normalized.get("action")
        # Ignore mutually exclusive fields emitted by a structured model. They
        # cannot grant authority: STOP never calls a tool, and model_call_id is
        # always bound to the audited runtime call after validation.
        normalized["model_call_id"] = None
        if action == ControllerPolicyAction.STOP.value:
            normalized["need_id"] = None
            normalized["tool_name"] = None
            normalized["selected_action_ids"] = ()
            normalized["pending_action_ids"] = ()
            if normalized.get("stop_reason") is None:
                normalized["stop_reason"] = ControllerStopReason.MANDATORY_GAP_UNRESOLVED.value
        elif action == ControllerPolicyAction.CALL_TOOL.value:
            normalized["stop_reason"] = None
            normalized["selected_action_ids"] = ()
            normalized["pending_action_ids"] = ()
        elif action == ControllerPolicyAction.EXECUTE_PLAN.value:
            normalized["stop_reason"] = None
            normalized["need_id"] = None
            normalized["tool_name"] = None
        return normalized

    @model_validator(mode="after")
    def validate_action(self) -> ControllerPolicyDecision:
        if self.action is ControllerPolicyAction.CALL_TOOL:
            if self.need_id is None or self.tool_name is None or self.stop_reason is not None:
                raise ValueError("call_tool decision requires need/tool and no stop reason")
            if self.selected_action_ids or self.pending_action_ids:
                raise ValueError("call_tool decision cannot carry batch plan action ids")
        elif self.action is ControllerPolicyAction.EXECUTE_PLAN:
            if (
                self.need_id is not None
                or self.tool_name is not None
                or self.stop_reason is not None
                or not self.pending_action_ids
            ):
                raise ValueError("execute_plan requires pending action ids only")
        elif self.need_id is not None or self.tool_name is not None or self.stop_reason is None:
            raise ValueError("stop decision requires only a stop reason")
        return self


class ResolutionStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


class ContextResolutionResult(DomainModel):
    resolution_id: StableId
    request_id: StableId
    status: ResolutionStatus
    base_commit: CommitId
    snapshot_id: StableId
    normalized_needs: tuple[Stage1MemoryNeed, ...]
    memory_selection: tuple[MemorySelection, ...]
    evidence_ledger: tuple[EvidenceLedgerEntry, ...]
    conflicts: tuple[str, ...] = ()
    unresolved_gaps: tuple[str, ...] = ()
    context_assembly_spec: ContextAssemblySpec | None = None
    sufficiency_report: SufficiencyReport | None = None
    stop_reason: ControllerStopReason
    receipt: AgentExecutionReceipt

    @model_validator(mode="after")
    def validate_sufficiency(self) -> ContextResolutionResult:
        if self.stop_reason is ControllerStopReason.SUFFICIENT and self.unresolved_gaps:
            raise ValueError("sufficient resolution cannot retain unresolved gaps")
        if self.status is ResolutionStatus.READY and self.context_assembly_spec is None:
            raise ValueError("ready resolution requires an assembly specification")
        if self.status is ResolutionStatus.READY and self.sufficiency_report is None:
            raise ValueError("ready resolution requires a sufficiency report")
        return self


class ControllerArm(StrEnum):
    DETERMINISTIC = "deterministic_stage1"
    BOUNDED_R2 = "bounded_r2"


class ArmExecutionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class PairedContextArmResult(DomainModel):
    arm: ControllerArm
    execution_status: ArmExecutionStatus = ArmExecutionStatus.COMPLETED
    context: Stage1ContextPackage
    selected_unit_ids: tuple[StableId, ...]
    retrieval_call_count: int = Field(ge=0)
    calls_allocated_by_need: dict[str, int] = Field(default_factory=dict)
    stop_reason: ControllerStopReason
    comparison_basis_fingerprint: ArtifactId
    future_leakage_count: int = Field(ge=0)
    writer_context: WriterContextPackage | None = None
    evidence_ledger: EvidenceLedger | None = None
    assembly_status: ContextAssemblyStatus | None = None
    quality_eligible: bool = True
    failure_category: str | None = None
    need_generation_status: str = Field(default="completed", min_length=1)
    unexpanded_focus_ids: tuple[StableId, ...] = ()
    need_completion_spec_version: str = Field(
        default="need_completion_spec.v1",
        min_length=1,
    )
    mandatory_need_facets_total: int = Field(default=0, ge=0)
    mandatory_need_facets_closed: int = Field(default=0, ge=0)
    support_receipt_refs: tuple[ArtifactRef, ...] = ()
    selected_claim_variant_ids: tuple[StableId, ...] = ()
    context_assembly_spec_ref: ArtifactRef | None = None
    context_assembly_spec: ContextAssemblySpec | None = None
    claim_support_groups: tuple[ClaimSupportGroup, ...] = ()
    claim_variants: tuple[ClaimVariant, ...] = ()
    support_receipts: tuple[ClaimSupportReceipt, ...] = ()
    cutoff_attestations: tuple[CutoffAttestation, ...] = ()
    typed_failure_diagnostic_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_writer_artifacts(self) -> PairedContextArmResult:
        if any(value < 0 for value in self.calls_allocated_by_need.values()):
            raise ValueError("per-Need call allocations must be non-negative")
        if sum(self.calls_allocated_by_need.values()) > self.retrieval_call_count:
            raise ValueError("per-Need allocations exceed the arm retrieval call count")
        if self.mandatory_need_facets_closed > self.mandatory_need_facets_total:
            raise ValueError("closed mandatory Need facets exceed the total")
        if (self.writer_context is None) != (self.evidence_ledger is None):
            raise ValueError("writer context and evidence ledger must appear together")
        if self.writer_context is not None and self.assembly_status is None:
            raise ValueError("writer artifact requires an assembly status")
        if (
            self.quality_eligible
            and self.assembly_status is not None
            and self.assembly_status is not ContextAssemblyStatus.READY
        ):
            raise ValueError("non-READY writer context cannot be quality eligible")
        if not self.quality_eligible and self.failure_category is None:
            raise ValueError("ineligible paired arm requires a failure category")
        if self.execution_status is not ArmExecutionStatus.COMPLETED:
            if self.quality_eligible:
                raise ValueError("skipped or failed arm cannot be quality eligible")
            if self.writer_context is not None or self.evidence_ledger is not None:
                raise ValueError("skipped or failed arm cannot expose Writer artifacts")
            if self.retrieval_call_count or self.calls_allocated_by_need:
                raise ValueError("skipped or failed arm cannot report successful retrieval calls")
        if self.context_assembly_spec is not None and self.context_assembly_spec_ref is None:
            raise ValueError("embedded assembly spec requires its content-addressed ref")
        if self.support_receipts and len(self.support_receipt_refs) != len(self.support_receipts):
            raise ValueError("support receipts and their refs must appear together")
        if self.support_receipts and any(
            group.support_receipt_ref not in self.support_receipt_refs
            for group in self.claim_support_groups
        ):
            raise ValueError("support group references an unavailable receipt")
        variant_ids = {item.claim_variant_id for item in self.claim_variants}
        if self.claim_variants and not set(self.selected_claim_variant_ids).issubset(variant_ids):
            raise ValueError("selected claim variant is not embedded in the frozen arm")
        return self


class PairedContextComparison(DomainModel):
    pair_id: StableId
    request_id: StableId
    deterministic: PairedContextArmResult
    agentic: PairedContextArmResult
    comparable: bool
    blockers: tuple[str, ...] = ()
    arm_c_writer_context: WriterContextPackage | None = None
    arm_c_evidence_ledger: EvidenceLedger | None = None
    arm_c_status: ContextAssemblyStatus | None = None
    arm_c_execution_status: ArmExecutionStatus = ArmExecutionStatus.SKIPPED
    arm_c_failure_category: str | None = "NOT_RUN"
    freeze_receipt: FreezeReceipt | None = None
    # Generated needs used by the five-segment evaluation (public-safe).
    generated_needs: tuple[Stage1MemoryNeed, ...] = ()
    # Durable, replay-complete Planner invocation.  This is the document ref,
    # not merely the compact metadata hash copied onto individual Needs.
    planner_artifact_ref: ArtifactRef | None = None
    # Gate 1 planner diagnostics: whether the LLM Need Planner fell back to
    # the deterministic templates, and (GROUNDED, AMBIGUOUS, UNRESOLVED)
    # grounding counts across the accepted drafts.
    planner_fallback_used: bool = False
    planner_fallback_reason: str | None = None
    grounded_status_counts: tuple[int, int, int] = (0, 0, 0)

    @model_validator(mode="after")
    def validate_comparability(self) -> PairedContextComparison:
        if self.planner_fallback_used != (self.planner_fallback_reason is not None):
            raise ValueError("Planner fallback flag/reason must appear together")
        if self.deterministic.arm is not ControllerArm.DETERMINISTIC:
            raise ValueError("deterministic paired arm has the wrong controller type")
        if self.agentic.arm is not ControllerArm.BOUNDED_R2:
            raise ValueError("agentic paired arm has the wrong controller type")
        if (
            self.deterministic.context.base_commit != self.agentic.context.base_commit
            or self.deterministic.context.snapshot_id != self.agentic.context.snapshot_id
        ):
            raise ValueError("paired arms must share canonical and snapshot basis")
        if (
            self.deterministic.context.budget_report.token_budget
            != self.agentic.context.budget_report.token_budget
        ):
            raise ValueError("paired arms must share context token budget")
        if (
            self.deterministic.comparison_basis_fingerprint
            != self.agentic.comparison_basis_fingerprint
        ):
            raise ValueError("paired arms must share comparison configuration")
        expected_comparable = (
            not self.blockers
            and self.deterministic.quality_eligible
            and self.agentic.quality_eligible
            and not (self.deterministic.future_leakage_count or self.agentic.future_leakage_count)
        )
        if self.comparable != expected_comparable:
            raise ValueError("paired comparison flag contradicts blockers or leakage")
        if (self.arm_c_writer_context is None) != (self.arm_c_evidence_ledger is None):
            raise ValueError("Arm C Writer Context and Evidence Ledger must appear together")
        if self.arm_c_execution_status is ArmExecutionStatus.COMPLETED:
            if self.arm_c_writer_context is None or self.arm_c_status is None:
                raise ValueError("completed Arm C requires frozen Writer artifacts")
            if self.arm_c_failure_category is not None:
                raise ValueError("completed Arm C cannot carry a failure category")
        elif self.arm_c_writer_context is not None or self.arm_c_evidence_ledger is not None:
            raise ValueError("skipped or failed Arm C cannot expose Writer artifacts")
        elif self.arm_c_failure_category is None:
            raise ValueError("skipped or failed Arm C requires a typed failure category")
        return self


class PairedPilotArmMetrics(DomainModel):
    gold_evidence_recall: float = Field(ge=0.0, le=1.0)
    observed_use_coverage: float = Field(ge=0.0, le=1.0)
    operational_constraint_coverage: float = Field(ge=0.0, le=1.0)
    plan_obligation_coverage: float = Field(ge=0.0, le=1.0)
    mandatory_constraint_coverage: float = Field(ge=0.0, le=1.0)
    evidence_traceability: float = Field(ge=0.0, le=1.0)
    selected_unit_count: int = Field(ge=0)
    retrieval_call_count: int = Field(ge=0)
    future_leakage_count: int = Field(ge=0)
    stop_reason: ControllerStopReason


class PairedPilotCaseResult(DomainModel):
    case_id: StableId
    information_profile: BenchmarkInformationProfile
    checkpoint_chapter: int = Field(ge=0)
    pair_id: StableId
    request_id: StableId
    comparison_basis_fingerprint: ArtifactId
    comparable: bool
    blockers: tuple[str, ...] = ()
    deterministic_execution_status: ArmExecutionStatus = ArmExecutionStatus.COMPLETED
    agentic_execution_status: ArmExecutionStatus = ArmExecutionStatus.COMPLETED
    paired_comparison_status: str = "COMPARABLE"
    deterministic_metrics: PairedPilotArmMetrics
    agentic_metrics: PairedPilotArmMetrics | None = None
    delta_metrics: PairedPilotArmMetrics | None = None
    accuracy_gain: bool | None = None
    tool_call_reduction: bool | None = None
    safety_regression: bool | None = None

    @model_validator(mode="after")
    def validate_case_summary(self) -> PairedPilotCaseResult:
        if self.deterministic_execution_status is not ArmExecutionStatus.COMPLETED:
            raise ValueError("deterministic metrics require a completed deterministic arm")
        if self.agentic_execution_status is not ArmExecutionStatus.COMPLETED:
            if any(
                item is not None
                for item in (
                    self.agentic_metrics,
                    self.delta_metrics,
                    self.accuracy_gain,
                    self.tool_call_reduction,
                    self.safety_regression,
                )
            ):
                raise ValueError("skipped or failed Agentic arm cannot expose metrics or deltas")
            if self.comparable or self.paired_comparison_status == "COMPARABLE":
                raise ValueError("unexecuted Agentic arm cannot be a paired comparison")
            return self
        if self.agentic_metrics is None:
            raise ValueError("completed Agentic arm requires metrics")
        expected_comparable = not self.blockers and not (
            self.deterministic_metrics.future_leakage_count
            or self.agentic_metrics.future_leakage_count
        )
        if self.comparable != expected_comparable:
            raise ValueError("paired Pilot comparable flag is inconsistent")
        expected_gain = (
            self.agentic_metrics.gold_evidence_recall
            > self.deterministic_metrics.gold_evidence_recall
        )
        expected_reduction = (
            self.agentic_metrics.retrieval_call_count
            < self.deterministic_metrics.retrieval_call_count
        )
        # In delta mode the production candidate is Arm C; its safety regression
        # is measured against the deterministic floor (A).  Otherwise the
        # standalone Agentic arm (B) is compared against A.
        if self.delta_metrics is not None:
            expected_regression = (
                self.delta_metrics.future_leakage_count
                > self.deterministic_metrics.future_leakage_count
                or self.delta_metrics.mandatory_constraint_coverage
                < self.deterministic_metrics.mandatory_constraint_coverage
            )
        else:
            expected_regression = (
                self.agentic_metrics.future_leakage_count
                > self.deterministic_metrics.future_leakage_count
                or self.agentic_metrics.mandatory_constraint_coverage
                < self.deterministic_metrics.mandatory_constraint_coverage
            )
        if self.accuracy_gain != expected_gain:
            raise ValueError("paired Pilot accuracy gain flag is inconsistent")
        if self.tool_call_reduction != expected_reduction:
            raise ValueError("paired Pilot tool reduction flag is inconsistent")
        if self.safety_regression != expected_regression:
            raise ValueError("paired Pilot safety regression flag is inconsistent")
        if self.paired_comparison_status != ("COMPARABLE" if self.comparable else "NOT_COMPARABLE"):
            raise ValueError("paired comparison status is inconsistent")
        return self


class Stage2PairedPilotReport(DomainModel):
    report_id: StableId
    bundle_hash: ArtifactId
    configuration_fingerprint: ArtifactId
    controller_mode: ControllerMode = ControllerMode.DETERMINISTIC
    cases: tuple[PairedPilotCaseResult, ...]
    paired_results_count: int = Field(ge=0)
    comparable_results_count: int = Field(ge=0)
    future_leakage_count: int = Field(ge=0)
    safety_regression_count: int = Field(ge=0)
    accuracy_gain_count: int = Field(ge=0)
    tool_call_reduction_count: int = Field(ge=0)
    delta_gain_count: int = Field(default=0, ge=0)
    held_out_complex_gain_proven: bool = False

    @model_validator(mode="after")
    def validate_report_summary(self) -> Stage2PairedPilotReport:
        identities = tuple((item.case_id, item.information_profile) for item in self.cases)
        if len(identities) != len(set(identities)):
            raise ValueError("paired Pilot case/profile identities must be unique")
        expected = {
            "paired_results_count": sum(
                item.agentic_execution_status is ArmExecutionStatus.COMPLETED for item in self.cases
            ),
            "comparable_results_count": sum(item.comparable for item in self.cases),
            "future_leakage_count": sum(
                item.deterministic_metrics.future_leakage_count
                + (
                    item.agentic_metrics.future_leakage_count
                    if item.agentic_metrics is not None
                    else 0
                )
                for item in self.cases
            ),
            "safety_regression_count": sum(item.safety_regression is True for item in self.cases),
            "accuracy_gain_count": sum(item.accuracy_gain is True for item in self.cases),
            "tool_call_reduction_count": sum(
                item.tool_call_reduction is True for item in self.cases
            ),
        }
        for field, value in expected.items():
            if getattr(self, field) != value:
                raise ValueError(f"paired Pilot {field} is inconsistent")
        if self.held_out_complex_gain_proven:
            raise ValueError("Pilot current-state cases cannot prove held-out complex-query gain")
        return self


class MemoryGatewayMode(StrEnum):
    DETERMINISTIC = "deterministic"
    BOUNDED_R2 = "bounded_r2"


class MemoryGatewayPolicy(DomainModel):
    policy_id: StableId
    mode: MemoryGatewayMode
    allow_deterministic_fallback: bool = True
    promotion_evidence: ArtifactRef | None = None
    configuration_fingerprint: ArtifactId

    @model_validator(mode="after")
    def validate_promotion(self) -> MemoryGatewayPolicy:
        if self.mode is MemoryGatewayMode.BOUNDED_R2 and self.promotion_evidence is None:
            raise ValueError("BOUNDED_R2 gateway policy requires promotion evidence")
        return self


class MemoryGatewayResult(DomainModel):
    gateway_result_id: StableId
    request_id: StableId
    selected_arm: ControllerArm
    fallback_used: bool
    fallback_reason: str | None = None
    context: Stage1ContextPackage
    frozen_context_artifact: ArtifactRef
    selected_result: PairedContextArmResult
    comparison: PairedContextComparison | None = None
    promotion_evidence: ArtifactRef | None = None
    policy_id: StableId
    configuration_fingerprint: ArtifactId

    @model_validator(mode="after")
    def validate_gateway_selection(self) -> MemoryGatewayResult:
        if self.selected_arm is not self.selected_result.arm:
            raise ValueError("Memory Gateway arm differs from its selected result")
        if self.context != self.selected_result.context:
            raise ValueError("Memory Gateway context must equal its selected result")
        if self.comparison is not None:
            paired = (
                self.comparison.deterministic
                if self.selected_arm is ControllerArm.DETERMINISTIC
                else self.comparison.agentic
            )
            if paired != self.selected_result:
                raise ValueError("Memory Gateway selection differs from paired execution")
        if self.fallback_used != (self.fallback_reason is not None):
            raise ValueError("Memory Gateway fallback flag and reason must agree")
        if self.fallback_used and self.selected_arm is not ControllerArm.DETERMINISTIC:
            raise ValueError("Memory Gateway fallback must select deterministic arm")
        if self.selected_arm is ControllerArm.BOUNDED_R2 and self.promotion_evidence is None:
            raise ValueError("bounded Memory Gateway result requires promotion evidence")
        if (
            self.comparison is not None
            and self.selected_arm is ControllerArm.BOUNDED_R2
            and not self.comparison.comparable
        ):
            raise ValueError("Memory Gateway cannot select a non-comparable bounded arm")
        if self.configuration_fingerprint != self.selected_result.comparison_basis_fingerprint:
            raise ValueError("Memory Gateway configuration differs from selected execution")
        return self


class ToolFailureCode(StrEnum):
    SCOPE_MISMATCH = "TOOL_SCOPE_MISMATCH"
    BASE_COMMIT_MISMATCH = "TOOL_BASE_COMMIT_MISMATCH"
    SNAPSHOT_STALE = "TOOL_SNAPSHOT_STALE"
    ACCESS_DENIED = "TOOL_ACCESS_DENIED"
    INVALID_QUERY = "TOOL_INVALID_QUERY"
    TIMEOUT = "TOOL_TIMEOUT"
    BACKEND_UNAVAILABLE = "TOOL_BACKEND_UNAVAILABLE"
    PARTIAL_RESULT = "TOOL_PARTIAL_RESULT"
    EVIDENCE_UNRESOLVABLE = "TOOL_EVIDENCE_UNRESOLVABLE"
    BUDGET_EXCEEDED = "TOOL_BUDGET_EXCEEDED"


class ToolCallContext(DomainModel):
    tool_call_id: StableId
    run_id: RunId
    task_id: TaskId
    agent_type: AgentType
    agent_mode: AgentMode
    project_id: ProjectId
    base_commit: CommitId
    snapshot_id: StableId | None = None
    worldline: str = Field(min_length=1)
    narrative_chapter: int = Field(ge=0)
    access_scope: AccessScope
    plan_permission: bool = False
    timeout_ms: int = Field(ge=1)
    read_only: bool = True


class MemoryToolQuery(DomainModel):
    need_id: StableId
    limit: int = Field(default=20, ge=1, le=100)


class ToolResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class ToolResult(DomainModel):
    tool_call_id: StableId
    status: ToolResultStatus
    basis_commit: CommitId
    snapshot_id: StableId | None = None
    payload: JsonValue | None = None
    coverage: float = Field(default=0, ge=0, le=1)
    partial: bool = False
    warnings: tuple[str, ...] = ()
    query_variant: str | None = Field(default=None, min_length=1, max_length=128)
    backend_latency_ms: int | None = Field(default=None, ge=0)
    new_information_gain: int = Field(default=0, ge=0)
    retrieval_channel: RetrievalChannel | None = None
    channel_candidate_count: int | None = Field(default=None, ge=0)
    channel_failure_code: ChannelFailureCode | None = None
    failure_code: ToolFailureCode | None = None
    audit_ref: StableId

    @model_validator(mode="after")
    def validate_status(self) -> ToolResult:
        if self.status is ToolResultStatus.FAILED and self.failure_code is None:
            raise ValueError("failed tool result requires a failure code")
        if self.status is ToolResultStatus.SUCCEEDED and self.failure_code is not None:
            raise ValueError("successful tool result cannot carry a failure code")
        if self.status is ToolResultStatus.SUCCEEDED and self.channel_failure_code is not None:
            raise ValueError("successful tool result cannot carry a channel failure code")
        if self.channel_failure_code is not None and self.retrieval_channel is None:
            raise ValueError("channel failure requires a retrieval channel")
        if self.status is ToolResultStatus.PARTIAL and not self.partial:
            raise ValueError("partial tool result must set partial=true")
        return self


class GuardianOutcome(StrEnum):
    APPROVE = "approve"
    REVISE = "revise"
    REJECT = "reject"
    HUMAN_REVIEW = "human_review"


GuardianRiskCode = Annotated[str, StringConstraints(min_length=1, max_length=64)]
GuardianReason = Annotated[str, StringConstraints(min_length=1, max_length=240)]


class GuardianDecisionDraft(DomainModel):
    outcome: GuardianOutcome
    risk_codes: tuple[GuardianRiskCode, ...] = Field(max_length=8)
    reasons: tuple[GuardianReason, ...] = Field(min_length=1, max_length=4)
    revised_candidate: dict[str, JsonValue] | None = Field(default=None, max_length=8)

    @model_validator(mode="after")
    def validate_revision(self) -> GuardianDecisionDraft:
        if self.outcome is GuardianOutcome.REVISE and self.revised_candidate is None:
            raise ValueError("Guardian revise outcome requires a revised candidate")
        if self.outcome is not GuardianOutcome.REVISE and self.revised_candidate is not None:
            raise ValueError("only Guardian revise outcome may return a revised candidate")
        return self


class GuardianDecision(DomainModel):
    decision_id: StableId
    proposal_id: StableId
    base_commit: CommitId
    outcome: GuardianOutcome
    risk_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    revised_candidate: ArtifactRef | None = None
    receipt: AgentExecutionReceipt

    @model_validator(mode="after")
    def validate_guardian_receipt(self) -> GuardianDecision:
        if self.receipt.agent_type is not AgentType.MEMORY_GUARDIAN:
            raise ValueError("Guardian decision requires a Memory Guardian receipt")
        if self.receipt.agent_mode is not AgentMode.RISK_REVIEW:
            raise ValueError("Guardian decision requires a RISK_REVIEW receipt")
        if self.receipt.base_commit != self.base_commit:
            raise ValueError("Guardian receipt and decision must share a base commit")
        if self.outcome is GuardianOutcome.REVISE and self.revised_candidate is None:
            raise ValueError("Guardian revise decision requires a persisted candidate")
        if self.outcome is not GuardianOutcome.REVISE and self.revised_candidate is not None:
            raise ValueError("only Guardian revise decision may reference a candidate")
        return self


class CuratorReplayResult(DomainModel):
    observed_changes: ObservedChangeSet
    coverage: float = Field(ge=0, le=1)
    unresolved: tuple[str, ...] = ()
    declared_vs_observed_diff: tuple[str, ...] = ()
    receipt: AgentExecutionReceipt

    @model_validator(mode="after")
    def validate_receipt_basis(self) -> CuratorReplayResult:
        if self.receipt.agent_type is not AgentType.MEMORY_CURATOR:
            raise ValueError("Curator replay result requires a Memory Curator receipt")
        if self.receipt.agent_mode is not AgentMode.REPLAY:
            raise ValueError("Curator replay result requires a REPLAY receipt")
        if self.receipt.base_commit != self.observed_changes.base_commit:
            raise ValueError("Curator receipt and observed changes must share a base commit")
        if self.receipt.unresolved != self.unresolved:
            raise ValueError("Curator receipt must preserve unresolved candidates")
        return self


class PatchRiskLevel(StrEnum):
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"


class PatchRiskAssessment(DomainModel):
    assessment_id: StableId
    change_set_id: StableId
    base_commit: CommitId
    level: PatchRiskLevel
    risk_codes: tuple[str, ...]
    requires_guardian: bool
    requires_human_review: bool

    @model_validator(mode="after")
    def validate_routing(self) -> PatchRiskAssessment:
        if self.level is PatchRiskLevel.LOW and self.risk_codes:
            raise ValueError("low-risk patch cannot carry risk codes")
        if self.requires_human_review and not self.requires_guardian:
            raise ValueError("human risk review requires Guardian routing")
        if self.level is PatchRiskLevel.CRITICAL and not self.requires_human_review:
            raise ValueError("critical patch requires human review")
        return self


class WriteGateOutcome(StrEnum):
    ALLOW_COMMIT = "allow_commit"
    BLOCK_VALIDATION = "block_validation"
    REQUIRE_GUARDIAN = "require_guardian"
    REQUIRE_HUMAN = "require_human"
    BLOCK_GUARDIAN = "block_guardian"
    BLOCK_HUMAN = "block_human"


class PatchApprovalRequest(DomainModel):
    approval_request_id: StableId
    project_id: ProjectId
    change_set_id: StableId
    base_commit: CommitId
    risk_assessment_id: StableId
    guardian_decision_id: StableId
    status: AuthorApprovalStatus = AuthorApprovalStatus.PENDING
    requested_at: datetime


class PatchApprovalDecision(DomainModel):
    decision_id: StableId
    approval_request_id: StableId
    project_id: ProjectId
    change_set_id: StableId
    base_commit: CommitId
    risk_assessment_id: StableId
    guardian_decision_id: StableId
    status: AuthorApprovalStatus
    author_id: StableId
    reason: str = Field(min_length=1)
    decided_at: datetime

    @model_validator(mode="after")
    def validate_terminal(self) -> PatchApprovalDecision:
        if self.status is AuthorApprovalStatus.PENDING:
            raise ValueError("patch approval decision cannot remain pending")
        return self


class WriteGateDecision(DomainModel):
    decision_id: StableId
    change_set_id: StableId
    base_commit: CommitId
    outcome: WriteGateOutcome
    risk_assessment_id: StableId
    guardian_decision_id: StableId | None = None
    human_approval_decision_id: StableId | None = None
    reasons: tuple[str, ...] = ()


class ScenarioBuildMode(StrEnum):
    CONTINUOUS_REPLAY = "continuous_replay"
    INDEPENDENT_REBUILD = "independent_rebuild"


class BenchmarkScenarioProfile(DomainModel):
    profile_id: StableId
    build_mode: ScenarioBuildMode
    information_profile: BenchmarkInformationProfile
    checkpoint_chapters: tuple[int, ...] = Field(min_length=1)
    configuration_fingerprint: ArtifactId

    @model_validator(mode="after")
    def validate_checkpoints(self) -> BenchmarkScenarioProfile:
        if any(chapter < 1 for chapter in self.checkpoint_chapters):
            raise ValueError("scenario checkpoints must be positive chapter indexes")
        if self.checkpoint_chapters != tuple(sorted(set(self.checkpoint_chapters))):
            raise ValueError("scenario checkpoints must be unique and ascending")
        return self


class BenchmarkCheckpointDeclaration(DomainModel):
    case_id: StableId
    checkpoint_chapter: int = Field(ge=1)
    evaluator_source_ids: tuple[StableId, ...] = Field(min_length=1)


class BenchmarkScenario(DomainModel):
    scenario_id: StableId
    project_id: ProjectId
    branch: str = Field(min_length=1)
    sources: tuple[BootstrapSource, ...] = Field(min_length=1)
    classifications: tuple[SourceClassification, ...] = Field(min_length=1)
    profile: BenchmarkScenarioProfile
    checkpoint_cases: tuple[BenchmarkCheckpointDeclaration, ...] = ()

    @model_validator(mode="after")
    def validate_source_bindings(self) -> BenchmarkScenario:
        source_by_id = {source.source_id: source for source in self.sources}
        classification_by_id = {
            classification.source_id: classification for classification in self.classifications
        }
        if len(source_by_id) != len(self.sources):
            raise ValueError("benchmark scenario source ids must be unique")
        if len(classification_by_id) != len(self.classifications):
            raise ValueError("benchmark scenario classification ids must be unique")
        if source_by_id.keys() != classification_by_id.keys():
            raise ValueError("every scenario source requires exactly one classification")
        if any(
            source.source_class is not classification_by_id[source_id].source_class
            for source_id, source in source_by_id.items()
        ):
            raise ValueError("scenario source and classification classes must match")
        chapter_indexes = tuple(
            source.chapter_index
            for source in self.sources
            if source.source_class is SourceClass.CHAPTER_TEXT
        )
        if len(chapter_indexes) != len(set(chapter_indexes)):
            raise ValueError("scenario chapter sources must have unique chapter indexes")
        if self.checkpoint_cases:
            declared_chapters = tuple(
                declaration.checkpoint_chapter for declaration in self.checkpoint_cases
            )
            if declared_chapters != self.profile.checkpoint_chapters:
                raise ValueError("checkpoint case declarations must match profile checkpoints")
            case_ids = tuple(declaration.case_id for declaration in self.checkpoint_cases)
            if len(case_ids) != len(set(case_ids)):
                raise ValueError("checkpoint case declarations require unique case ids")
            evaluator_ids = tuple(
                source_id
                for declaration in self.checkpoint_cases
                for source_id in declaration.evaluator_source_ids
            )
            if len(evaluator_ids) != len(set(evaluator_ids)):
                raise ValueError("an evaluator source may belong to only one checkpoint")
            for declaration in self.checkpoint_cases:
                for source_id in declaration.evaluator_source_ids:
                    source = source_by_id.get(source_id)
                    if source is None or not source.evaluator_only:
                        raise ValueError(
                            "checkpoint evaluator bindings require evaluator-only sources"
                        )
                    if source.earliest_visible_chapter != declaration.checkpoint_chapter:
                        raise ValueError(
                            "checkpoint evaluator source visibility must match its checkpoint"
                        )
        return self


class FutureIsolationAttestation(DomainModel):
    attestation_id: StableId
    checkpoint_chapter: int = Field(ge=0)
    canonical_source_ids: tuple[StableId, ...]
    evaluator_only_source_ids: tuple[StableId, ...]
    overlap_source_ids: tuple[StableId, ...] = ()
    passed: bool
    configuration_fingerprint: ArtifactId

    @model_validator(mode="after")
    def validate_isolation(self) -> FutureIsolationAttestation:
        overlap = set(self.canonical_source_ids) & set(self.evaluator_only_source_ids)
        if set(self.overlap_source_ids) != overlap:
            raise ValueError("reported source overlap does not match attestation inputs")
        if self.passed == bool(overlap):
            raise ValueError("future isolation pass flag contradicts overlap")
        return self


class ChapterStateBuildReceipt(DomainModel):
    receipt_id: StableId
    project_id: ProjectId
    chapter_index: int = Field(ge=0)
    parent_commit: CommitId
    resulting_commit: CommitId
    source_id: StableId
    curator_receipt: AgentExecutionReceipt
    validation_artifact: ArtifactRef
    projection_snapshot_id: StableId
    previous_chain_hash: ArtifactId | None = None
    chain_hash: ArtifactId


class TextRootAdvanceReceipt(DomainModel):
    receipt_id: StableId
    source_id: StableId
    narrative_index: int = Field(ge=0)
    previous_text_root: ArtifactId
    resulting_text_root: ArtifactId
    document_hash: ArtifactId


class BenchmarkCheckpointBasis(DomainModel):
    case_id: StableId
    project_id: ProjectId
    branch: str = Field(min_length=1)
    canonical_commit: CommitId
    text_root: ArtifactId
    plan_root: ArtifactId
    world_root: ArtifactId
    derived_snapshot_id: StableId
    r1_basis_commit: CommitId
    anchor_alias: str = Field(min_length=1)
    grounded_alias: str = Field(min_length=1)
    project_profile: ArtifactId
    configuration_fingerprint: ArtifactId
    last_revealed_chapter: int = Field(ge=0)
    future_isolation: FutureIsolationAttestation
    state_build_receipt_chain_hash: ArtifactId

    @model_validator(mode="after")
    def validate_basis(self) -> BenchmarkCheckpointBasis:
        if self.r1_basis_commit != self.canonical_commit:
            raise ValueError("checkpoint R1 basis must equal canonical commit")
        if self.future_isolation.checkpoint_chapter != self.last_revealed_chapter:
            raise ValueError("future isolation checkpoint mismatch")
        return self


class ContextFreezeReceipt(DomainModel):
    freeze_id: StableId
    case_id: StableId
    checkpoint_chapter: int = Field(ge=1)
    canonical_commit: CommitId
    snapshot_id: StableId
    context_artifact: ArtifactRef
    configuration_fingerprint: ArtifactId
    frozen_at: datetime


class EvaluatorRevealReceipt(DomainModel):
    reveal_id: StableId
    freeze_id: StableId
    evaluator_source_ids: tuple[StableId, ...] = Field(min_length=1)
    score_artifacts: tuple[ArtifactRef, ...] = ()
    canonical_write_count: int = Field(default=0, ge=0, le=0)
    evaluator_context_destroyed: bool
    completed_at: datetime


class ScenarioRunResult(DomainModel):
    scenario_id: StableId
    project_id: ProjectId
    build_mode: ScenarioBuildMode
    chapter_receipts: tuple[ChapterStateBuildReceipt, ...]
    checkpoints: tuple[BenchmarkCheckpointBasis, ...]
    freezes: tuple[ContextFreezeReceipt, ...] = ()
    evaluator_reveals: tuple[EvaluatorRevealReceipt, ...] = ()
    completed: bool
    blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_run_result(self) -> ScenarioRunResult:
        if self.completed and self.blockers:
            raise ValueError("completed scenario run cannot retain blockers")
        if len({item.freeze_id for item in self.freezes}) != len(self.freezes):
            raise ValueError("context freeze ids must be unique")
        freeze_ids = {item.freeze_id for item in self.freezes}
        if any(item.freeze_id not in freeze_ids for item in self.evaluator_reveals):
            raise ValueError("evaluator reveal must reference a recorded context freeze")
        return self


class ScenarioCheckpointArtifacts(DomainModel):
    text_root: ArtifactId
    plan_root: ArtifactId
    world_root: ArtifactId
    derived_snapshot_id: StableId
    anchor_alias: str = Field(min_length=1)
    grounded_alias: str = Field(min_length=1)
    project_profile: ArtifactId


class ScenarioChapterTransition(DomainModel):
    source_id: StableId
    parent_commit: CommitId
    resulting_commit: CommitId
    curator_receipt: AgentExecutionReceipt
    validation_artifact: ArtifactRef
    projection_snapshot_id: StableId
    freshness: FreshnessDecision
    checkpoint_artifacts: ScenarioCheckpointArtifacts | None = None

    @model_validator(mode="after")
    def validate_basis(self) -> ScenarioChapterTransition:
        if self.curator_receipt.base_commit != self.parent_commit:
            raise ValueError("scenario transition Curator basis must equal parent commit")
        if (
            self.freshness.canonical_commit != self.resulting_commit
            or self.freshness.r1_basis_commit != self.resulting_commit
            or self.freshness.required_snapshot_id != self.projection_snapshot_id
        ):
            raise ValueError("scenario transition freshness basis is inconsistent")
        if (
            self.checkpoint_artifacts is not None
            and self.checkpoint_artifacts.derived_snapshot_id != self.projection_snapshot_id
        ):
            raise ValueError("checkpoint artifacts use another projection snapshot")
        return self


class CanonicalWriteOutcome(DomainModel):
    change_set_id: StableId
    parent_commit: CommitId
    resulting_commit: CommitId
    validation_artifact: ArtifactRef
    projection_snapshot_id: StableId
    freshness: FreshnessDecision
    checkpoint_artifacts: ScenarioCheckpointArtifacts | None = None

    @model_validator(mode="after")
    def validate_write_basis(self) -> CanonicalWriteOutcome:
        if (
            self.freshness.canonical_commit != self.resulting_commit
            or self.freshness.r1_basis_commit != self.resulting_commit
            or self.freshness.required_snapshot_id != self.projection_snapshot_id
        ):
            raise ValueError("canonical write freshness basis is inconsistent")
        return self


class ReplayWriteStatus(StrEnum):
    COMMITTED = "committed"
    SUSPENDED = "suspended"
    BLOCKED = "blocked"


class ReplayWriteResult(DomainModel):
    status: ReplayWriteStatus
    curator_result: CuratorReplayResult
    validation_report_id: StableId
    risk_assessment: PatchRiskAssessment
    guardian_decision: GuardianDecision | None = None
    approval_request: PatchApprovalRequest | None = None
    approval_decision: PatchApprovalDecision | None = None
    write_gate: WriteGateDecision
    transition: ScenarioChapterTransition | None = None

    @model_validator(mode="after")
    def validate_status(self) -> ReplayWriteResult:
        if (self.status is ReplayWriteStatus.COMMITTED) != (self.transition is not None):
            raise ValueError("committed replay write requires exactly one transition")
        if self.status is ReplayWriteStatus.COMMITTED and (
            self.write_gate.outcome is not WriteGateOutcome.ALLOW_COMMIT
        ):
            raise ValueError("committed replay write requires an allowing WriteGate")
        if self.status is ReplayWriteStatus.SUSPENDED and self.write_gate.outcome not in {
            WriteGateOutcome.REQUIRE_GUARDIAN,
            WriteGateOutcome.REQUIRE_HUMAN,
        }:
            raise ValueError("suspended replay write requires a resumable gate outcome")
        return self


class EvaluatorDisposition(DomainModel):
    evaluator_context_destroyed: bool
    teacher_forced_resume_allowed: bool
    score_artifacts: tuple[ArtifactRef, ...] = ()


class IndependentRebuildComparison(DomainModel):
    case_id: StableId
    checkpoint_chapter: int = Field(ge=1)
    checkpoint_text_root: ArtifactId
    reference_text_root: ArtifactId
    compared_chapters: int = Field(ge=0)
    mismatched_chapters: tuple[int, ...] = ()
    consistent: bool

    @model_validator(mode="after")
    def validate_consistency(self) -> IndependentRebuildComparison:
        if self.consistent == bool(self.mismatched_chapters):
            raise ValueError("independent rebuild consistency contradicts mismatches")
        return self


class IndependentRebuildReport(DomainModel):
    report_id: StableId
    bundle_hash: ArtifactId
    reference_case_id: StableId
    comparisons: tuple[IndependentRebuildComparison, ...]
    all_consistent: bool
    configuration_fingerprint: ArtifactId

    @model_validator(mode="after")
    def validate_summary(self) -> IndependentRebuildReport:
        identities = tuple(item.case_id for item in self.comparisons)
        if len(identities) != len(set(identities)):
            raise ValueError("independent rebuild case ids must be unique")
        if self.all_consistent != all(item.consistent for item in self.comparisons):
            raise ValueError("independent rebuild summary contradicts comparisons")
        return self


class FailureLedgerType(StrEnum):
    BOOTSTRAP = "bootstrap"
    CONTROLLER = "controller"
    CURATOR = "curator"


class FailureSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class FailureLedgerEntry(DomainModel):
    failure_id: StableId
    code: str = Field(min_length=1)
    severity: FailureSeverity
    message: str = Field(min_length=1)
    run_id: RunId | None = None
    task_id: TaskId | None = None
    source_artifacts: tuple[ArtifactRef, ...] = ()
    occurred_at: datetime


class FailureLedgerDocument(DomainModel):
    ledger_id: StableId
    ledger_type: FailureLedgerType
    configuration_fingerprint: ArtifactId
    entries: tuple[FailureLedgerEntry, ...]

    @model_validator(mode="after")
    def validate_unique_failures(self) -> FailureLedgerDocument:
        if len({entry.failure_id for entry in self.entries}) != len(self.entries):
            raise ValueError("failure ledger entry ids must be unique")
        return self


class FailureLedgerRef(DomainModel):
    ledger_type: FailureLedgerType
    artifact: ArtifactRef
    entry_count: int = Field(ge=0)


class Stage2GateEvidence(DomainModel):
    evidence_id: StableId
    configuration_fingerprint: ArtifactId
    real_bundle_hash: ArtifactId | None = None
    bootstrap_user_data_imported: bool | None = None
    source_traceability_passed: bool | None = None
    genesis_author_approved: bool | None = None
    contracts_versioned: bool | None = None
    controller_read_only_bounded: bool | None = None
    tool_calls_replayable: bool | None = None
    controller_checkpoint_resume_passed: bool | None = None
    future_leakage_count: int | None = Field(default=None, ge=0)
    paired_results_count: int | None = Field(default=None, ge=0)
    held_out_complex_classes_with_gain: int | None = Field(default=None, ge=0)
    agentic_gain_proven: bool | None = None
    agentic_safety_regression_count: int | None = Field(default=None, ge=0)
    curator_real_replay_passed: bool | None = None
    real_replay_chapters: int | None = Field(default=None, ge=0)
    rejected_patch_pollution_count: int | None = Field(default=None, ge=0)
    freshness_violation_count: int | None = Field(default=None, ge=0)
    evaluation_ledger_complete: bool | None = None
    checkpoint_chapters: tuple[int, ...] = ()
    checkpoint_chain_consistent: bool | None = None
    future_isolation_failure_count: int | None = Field(default=None, ge=0)
    information_profiles_separate: bool | None = None
    failure_ledgers: tuple[FailureLedgerRef, ...] = ()

    @model_validator(mode="after")
    def validate_gate_evidence(self) -> Stage2GateEvidence:
        if self.checkpoint_chapters != tuple(sorted(set(self.checkpoint_chapters))):
            raise ValueError("gate checkpoint chapters must be unique and ascending")
        ledger_types = tuple(item.ledger_type for item in self.failure_ledgers)
        if len(ledger_types) != len(set(ledger_types)):
            raise ValueError("gate failure ledger types must be unique")
        if self.agentic_gain_proven is True and (
            self.paired_results_count is None
            or self.paired_results_count < 1
            or self.held_out_complex_classes_with_gain is None
            or self.held_out_complex_classes_with_gain < 1
        ):
            raise ValueError("agentic gain requires paired held-out evidence")
        return self


class Stage2GateVerdict(StrEnum):
    PASS = "pass"
    CONDITIONAL_PASS = "conditional_pass"
    FAIL = "fail"
    INCOMPLETE = "incomplete"


class ControllerPromotionDecision(StrEnum):
    ACCEPT_BOUNDED_DEFAULT = "accept_bounded_default"
    FREEZE_DETERMINISTIC_GATEWAY = "freeze_deterministic_gateway"
    REJECT_ARCHITECTURE = "reject_architecture"
    DEFER = "defer"


class Stage2GateReport(DomainModel):
    report_id: StableId
    evidence_id: StableId
    verdict: Stage2GateVerdict
    checks: dict[str, bool]
    blockers: tuple[str, ...]
    controller_promotion: ControllerPromotionDecision
    memory_gateway_frozen: bool
    configuration_fingerprint: ArtifactId

    @model_validator(mode="after")
    def validate_gate_decision(self) -> Stage2GateReport:
        expected = {
            Stage2GateVerdict.PASS: (
                ControllerPromotionDecision.ACCEPT_BOUNDED_DEFAULT,
                True,
            ),
            Stage2GateVerdict.CONDITIONAL_PASS: (
                ControllerPromotionDecision.FREEZE_DETERMINISTIC_GATEWAY,
                True,
            ),
            Stage2GateVerdict.FAIL: (
                ControllerPromotionDecision.REJECT_ARCHITECTURE,
                False,
            ),
            Stage2GateVerdict.INCOMPLETE: (
                ControllerPromotionDecision.DEFER,
                False,
            ),
        }[self.verdict]
        if (self.controller_promotion, self.memory_gateway_frozen) != expected:
            raise ValueError("Stage 2 gate verdict contradicts promotion/freeze decision")
        return self
