"""Teacher-forced Stage 2 E2E benchmark with real Canon commits and frozen evaluation."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import yaml

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.memory_write import (
    CommitServiceMemoryWriteAdapter,
    InformationBoundaryRegistryAdapter,
    LegacyGuardianPortAdapter,
    LegacyRiskClassifierAdapter,
    LegacyWriteGateAdapter,
    ProjectionServiceReadinessAdapter,
    RefusingCommitPort,
    RepositoryCanonicalReadAdapter,
    TeacherForcedCuratorPort,
)
from novel_agent.adapters.model import ScriptedModelEndpoint
from novel_agent.adapters.postgres.database import Base, build_engine, build_session_factory
from novel_agent.agents import (
    AgentRegistry,
    CuratorBootstrapAgent,
    CuratorRepairAgent,
    CuratorReplayAgent,
    GuardianRiskReviewAgent,
    PlannerAgent,
    StructuredAgentRunner,
    seal_agent_spec,
    seal_tool_policy,
)
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    RootKind,
    TextRootRef,
)
from novel_agent.domain.benchmark import (
    BenchmarkBundle,
    ChapterDocument,
    GoldItem,
    PlanRootDocument,
    PreludeDocument,
    TextRootDocument,
)
from novel_agent.domain.changes import (
    ChangeOperationType,
    CuratorStateRecord,
    CuratorStoryTime,
    CuratorV2EvidenceDraft,
    CuratorV2OperationDraft,
    ObservedChangeSet,
    ValidationStatus,
    WorldRecordKind,
)
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
    FreshnessMode,
    FreshnessRequest,
    FreshnessStatus,
    WorldRootDocument,
)
from novel_agent.domain.memory_benchmark import (
    ContentAddressedGoldMetricDescriptor,
    FiveSegmentReport,
    MemoryBenchmarkCaseArmReport,
    MemoryBenchmarkEvaluationReport,
)
from novel_agent.domain.memory_write import (
    ChapterRevealTrigger,
    CuratorWorldProposalInput,
    InformationBoundary,
    MemoryWriteBudget,
    MemoryWriteCommitProfile,
    MemoryWriteWorkflowRequest,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
    NarrativePosition,
    NoWorldMutationInput,
    RootUpdateIntent,
    RootUpdateKind,
    SourceProvenance,
)
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.planning_memory import PlannerInvocationArtifact
from novel_agent.domain.retrieval_routing import (
    ProjectionAttestation,
    RetrievalBackendProfile,
    RoutePlan,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentExecutionReceipt,
    AgentMode,
    AgentSpec,
    AgentType,
    ArmExecutionStatus,
    AuthorApprovalDecision,
    AuthorApprovalStatus,
    BenchmarkInformationProfile,
    BootstrapStrategy,
    ContractRef,
    ControllerMode,
    CuratorBootstrapDraft,
    CuratorReplayResult,
    EvaluatorDisposition,
    EvidenceSupportGateMode,
    ExecutionStatus,
    GuardianDecisionDraft,
    GuardianOutcome,
    PairedContextComparison,
    PairedPilotCaseResult,
    PlannerProposalDraft,
    PlanningTask,
    ProjectProfileRootDocument,
    PromptContractRef,
    ProposalProvenance,
    ProposedItem,
    PublicBenchmarkConfig,
    QualityRepairFeatureFlags,
    ReferenceAsset,
    ReferenceRootDocument,
    ScenarioChapterTransition,
    ScenarioCheckpointArtifacts,
    SkillContractRef,
    SourceClass,
    Stage2PairedPilotReport,
    ToolPermission,
    ToolPolicy,
    WorldPatchCandidate,
)
from novel_agent.domain.world import (
    Entity,
    GraphCandidatePageDraft,
    GraphCandidatePageStatus,
    PlanNode,
    StateRecord,
    StoryTime,
    TruthClass,
)
from novel_agent.domain.writer_context import EvidenceLedger
from novel_agent.ports.model_endpoint import ModelEndpointPort
from novel_agent.prompts import PromptRegistry, PromptTemplate
from novel_agent.prompts.registry import content_hash
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.benchmark_scenario_compiler import BenchmarkScenarioCompiler
from novel_agent.services.bootstrap import (
    BootstrapIngestionService,
    IngestedBootstrapSource,
    RawBootstrapSource,
)
from novel_agent.services.bootstrap_workflow import (
    BootstrapCrossRootValidator,
    BootstrapRootBuilder,
    GenesisCoordinator,
    SqlAuthorApprovalRepository,
)
from novel_agent.services.claim_support import ClaimSupportTransportConfig
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import (
    canonical_json_bytes,
    content_id,
    plan_root_content_id,
    world_root_content_id,
)
from novel_agent.services.effective_budget import DEFAULT_SEQUENCE_LIMIT
from novel_agent.services.evidence_candidates import EvidenceCandidateGenerator
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatcher
from novel_agent.services.information_boundary import InformationBoundaryPort
from novel_agent.services.memory_benchmark_contract import build_public_checkpoint_case
from novel_agent.services.memory_benchmark_diagnostics import StageLossDiagnosticBuilder
from novel_agent.services.memory_benchmark_evaluation import (
    MemoryBenchmarkEvaluator,
    ModelSemanticSupportVerifier,
)
from novel_agent.services.memory_benchmark_metric_contracts import (
    GATE_METRIC_FORMULA_HASH,
    GATE_METRIC_FORMULA_VERSION,
    GoldMetricContractBuilder,
)
from novel_agent.services.memory_benchmark_reporting import MemoryBenchmarkReporter
from novel_agent.services.memory_write_validation import Stage2ValidationV2Adapter
from novel_agent.services.memory_write_workflow import LocalMemoryWriteWorkflow
from novel_agent.services.model_curation import ModelCurator
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.model_request_admission import ModelRequestAdmissionController
from novel_agent.services.observed_text_ancestry import (
    ObservedTextAncestryProof,
    TextRootAncestryEntry,
)
from novel_agent.services.projection import (
    DerivedProjectionService,
    DerivedSnapshotRepository,
    FreshnessGate,
    ProjectionOutboxRepository,
    snapshot_id_for_commit,
)
from novel_agent.services.replay import ExactReplayProjectionBuilder
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner
from novel_agent.services.stage2_retrieval_backend import Stage2RetrievalBackendBundle
from novel_agent.services.teacher_forced_scenario import TeacherForcedScenarioRunner
from novel_agent.services.text_timeline import SequentialTextRootService
from novel_agent.skills import SkillRegistry, SkillTemplate

VERSION = SchemaVersion("1.0.0")
ZERO_HASH = ArtifactId("sha256:" + "0" * 64)
ZERO_COMMIT = CommitId("sha256:" + "0" * 64)


def _quality_repair_memory_write_budget() -> MemoryWriteBudget:
    """Allow three observations of one typed finding before quarantine.

    The proposal budget is raised above the historical default because the
    local endpoint in json_object framing can need several contract-feedback
    retries on complex chapters before producing a valid draft.  One proposal
    attempt can fan out across dynamic graph source units and semantic checks,
    so the call cap must leave the five-attempt repair corridor reachable.
    """

    return MemoryWriteBudget(
        max_curator_proposal_attempts=5,
        max_curator_proposal_rejections=5,
        same_content_hash_limit=3,
        same_finding_signature_limit=3,
        max_total_model_calls=64,
        token_budget=192_000,
        wall_clock_budget_ms=900_000,
    )


class TeacherForcedBenchmarkError(RuntimeError):
    pass


class TeacherForcedControlledPause(RuntimeError):
    """Expected workflow terminal that preserves resumable benchmark progress."""

    def __init__(self, chapter: int, result: MemoryWriteWorkflowResult) -> None:
        super().__init__(f"chapter {chapter} paused with {result.status.value}")
        self.chapter = chapter
        self.result = result


class TeacherForcedTerminalFailure(TeacherForcedBenchmarkError):
    """Non-resumable typed workflow failure with a persistable result."""

    def __init__(self, chapter: int, result: MemoryWriteWorkflowResult) -> None:
        super().__init__(
            f"chapter {chapter} memory-write workflow stopped: "
            f"status={result.status.value}; phase={result.workflow_phase.value}; "
            f"accepted={result.canonical_commit_accepted}; "
            f"codes={result.terminal_codes!r}"
        )
        self.chapter = chapter
        self.result = result


RealHybridBackendProvider = Callable[[ProjectId, CommitId], Stage2RetrievalBackendBundle]


class _ResponseBook:
    def __init__(self) -> None:
        self._responses: dict[StableId, str] = {}
        self._stage_responses: dict[str, str] = {}

    def add(self, request_id: StableId, model: Any) -> None:
        self._responses[request_id] = model.model_dump_json()

    def add_stage(self, scheduling_stage: str, model: Any) -> None:
        self._stage_responses[scheduling_stage] = model.model_dump_json()

    def resolve(self, request: ModelRequest) -> str:
        try:
            return self._responses.pop(request.request_id)
        except KeyError as error:
            stage_response = (
                self._stage_responses.get(request.scheduling_stage)
                if request.scheduling_stage is not None
                else None
            )
            if stage_response is not None:
                return stage_response
            raise TeacherForcedBenchmarkError(
                f"scripted model has no response for {request.request_id.root}"
            ) from error

    def assert_empty(self) -> None:
        if self._responses:
            raise TeacherForcedBenchmarkError("scripted model retained unused responses")


@dataclass(frozen=True, slots=True)
class _AgentHarness:
    runner: StructuredAgentRunner
    gateway: ModelGateway
    endpoint: ModelEndpointPort
    responses: _ResponseBook | None
    specs: tuple[AgentSpec, ...]
    prompt_refs: tuple[PromptContractRef, ...]
    skill_refs: tuple[SkillContractRef, ...]
    controller_spec: AgentSpec | None
    controller_request_factory: Any | None


@dataclass(frozen=True, slots=True)
class _FrozenState:
    text: TextRootDocument
    world: WorldRootDocument
    plan: PlanRootDocument
    commit: CommitId
    text_root_ref: ArtifactRef | None = None


class TeacherForcedBenchmarkE2ERunner:
    """Run teacher-forced construction with an explicitly qualified retrieval mode."""

    def __init__(
        self,
        *,
        token_budget: int = 4000,
        max_candidates: int = 20,
        max_tool_calls: int = 48,
        benchmark_arms: tuple[str, ...] = ("A", "B", "C"),
        semantic_endpoint: ModelEndpointPort | None = None,
        retrieval_backend_profile: RetrievalBackendProfile = RetrievalBackendProfile.SCRIPTED_SMOKE,
        real_hybrid_backend_provider: RealHybridBackendProvider | None = None,
        database_url: str | None = None,
        quality_repair_flags: QualityRepairFeatureFlags | None = None,
        memory_write_dry_run: bool = False,
        support_pre_proposal_trace: bool = False,
        support_max_concurrent_needs: int = 1,
        support_max_inflight_kv_tokens: int | None = None,
        support_admission_controller: ModelRequestAdmissionController | None = None,
        checkpoint_workers: int = 2,
        frozen_planner_artifact: PlannerInvocationArtifact | None = None,
        evaluator_max_concurrent_batches: int = 4,
        model_scheduling_timeout_seconds: float = 120.0,
        support_transport_config: ClaimSupportTransportConfig | None = None,
    ) -> None:
        if checkpoint_workers < 1:
            raise ValueError("checkpoint workers must be positive")
        self._semantic_endpoint = semantic_endpoint
        self._retrieval_backend_profile = retrieval_backend_profile
        self._real_hybrid_backend_provider = real_hybrid_backend_provider
        self._database_url = database_url
        self._quality_repair_flags = quality_repair_flags or QualityRepairFeatureFlags()
        self._memory_write_dry_run = memory_write_dry_run
        self._support_pre_proposal_trace = support_pre_proposal_trace
        self._support_max_concurrent_needs = support_max_concurrent_needs
        self._support_max_inflight_kv_tokens = support_max_inflight_kv_tokens
        self._support_admission_controller = support_admission_controller
        self._checkpoint_workers = checkpoint_workers
        self._frozen_planner_artifact = frozen_planner_artifact
        self._evaluator_max_concurrent_batches = evaluator_max_concurrent_batches
        self._model_scheduling_timeout_seconds = model_scheduling_timeout_seconds
        self._support_transport_config = support_transport_config or ClaimSupportTransportConfig()
        self._paired = Stage2PairedPilotRunner(
            token_budget=token_budget,
            max_candidates=max_candidates,
            max_tool_calls=max_tool_calls,
            arms=benchmark_arms,
            retrieval_backend_profile=retrieval_backend_profile,
            controller_mode=self._quality_repair_flags.controller_mode,
        )

    def run(
        self,
        source_directory: Path,
        output_directory: Path,
        bundle: BenchmarkBundle,
        *,
        information_profile: BenchmarkInformationProfile,
        stop_after_genesis: bool = False,
        max_chapter: int | None = None,
        resume: bool = False,
        project_directory: Path | None = None,
        resume_commit: CommitId | None = None,
        resume_chapter_override: int | None = None,
    ) -> dict[str, Any]:
        self._require_registered_retrieval_backend()
        resolved_project_directory = (project_directory or output_directory).resolve()
        progress_path = resolved_project_directory / "progress_manifest.json"
        if resume:
            if (resume_commit is None) != (resume_chapter_override is None):
                raise TeacherForcedBenchmarkError(
                    "explicit resume commit and chapter must be supplied together"
                )
            if resume_commit is not None and resume_chapter_override is not None:
                resume_from = resume_commit.root
                resume_chapter = resume_chapter_override
            elif not progress_path.exists():
                raise TeacherForcedBenchmarkError("resume requested but no progress manifest found")
            else:
                progress = json.loads(progress_path.read_text("utf-8"))
                resume_from = progress.get("last_accepted_commit")
                resume_chapter = progress.get("last_accepted_chapter", 0)
            if not isinstance(resume_from, str) or not resume_from:
                raise TeacherForcedBenchmarkError("progress manifest has no last accepted commit")
        else:
            if (output_directory / "flow_summary.json").exists():
                raise TeacherForcedBenchmarkError(
                    "output already contains a completed teacher-forced run"
                )
            resume_from = None
            resume_chapter = 0
        output_directory.mkdir(parents=True, exist_ok=True)
        resolved_project_directory.mkdir(parents=True, exist_ok=True)
        artifacts = ArtifactRepository(
            FilesystemObjectStore(resolved_project_directory / "objects")
        )
        database_path = resolved_project_directory / "project.sqlite3"
        database_url = self._database_url or f"sqlite:///{database_path}"
        database_descriptor = self._database_descriptor(database_url, database_path)
        engine = build_engine(database_url)
        if self._retrieval_backend_profile is RetrievalBackendProfile.SCRIPTED_SMOKE:
            Base.metadata.create_all(engine)
        session_factory = build_session_factory(engine)
        postcommit_recovery: dict[str, Any] | None = None
        try:
            project_id = source_project(bundle)
            harness = self._agent_harness(
                self._semantic_endpoint,
                quality_repair_flags=self._quality_repair_flags,
                admission_controller=self._support_admission_controller,
                scheduling_timeout_seconds=self._model_scheduling_timeout_seconds,
            )
            resume_attestation: ProjectionAttestation | None = None
            if resume:
                resume_commit_id = CommitId(cast(str, resume_from))
                commits = CommitService(session_factory)
                if resume_commit is None:
                    current_head = commits.current_commit(project_id)
                    expected_head = resume_commit_id
                    if current_head != expected_head:
                        resume_from, resume_chapter, postcommit_recovery = (
                            self._reconcile_postcommit_progress(
                                commits=commits,
                                artifacts=artifacts,
                                progress_path=progress_path,
                                expected_head=expected_head,
                                current_head=current_head,
                                expected_chapter=resume_chapter,
                            )
                        )
                        self._write_json(
                            output_directory / "postcommit_recovery.json",
                            postcommit_recovery,
                        )
                        resume_commit_id = CommitId(resume_from)
                manifest = commits.load_manifest(resume_commit_id)
                text_bytes = artifacts.read_verified(manifest.text_root)
                world_bytes = artifacts.read_verified(manifest.world_root)
                plan_bytes = artifacts.read_verified(manifest.plan_root)
                profile_bytes = artifacts.read_verified(manifest.project_profile_root)
                text = TextRootDocument.model_validate_json(text_bytes.decode("utf-8"))
                world = WorldRootDocument.model_validate_json(world_bytes.decode("utf-8"))
                plan = PlanRootDocument.model_validate_json(plan_bytes.decode("utf-8"))
                project_profile = ProjectProfileRootDocument.model_validate_json(
                    profile_bytes.decode("utf-8")
                )
                progress = json.loads(progress_path.read_text("utf-8"))
                original_genesis = progress.get("genesis_commit")
                genesis_commit_id_for_report = (
                    original_genesis
                    if isinstance(original_genesis, str) and original_genesis
                    else cast(str, resume_from)
                )
                genesis_commit = resume_commit_id
                ingested: tuple[IngestedBootstrapSource, ...] = ()
                bootstrap_bundle = BenchmarkScenarioCompiler().compile(bundle, information_profile)
                bootstrap_bundle = bootstrap_bundle.model_copy(
                    update={"sources": (), "classifications": ()}
                )
                resume_attestation = self._attestation_for_commit(project_id, genesis_commit)
            else:
                bootstrap_bundle, ingested = self._load_bootstrap(
                    source_directory,
                    project_id,
                    artifacts,
                )
                genesis, text, world, plan, project_profile = self._genesis(
                    project_id,
                    bootstrap_bundle.bundle_id,
                    ingested,
                    information_profile,
                    artifacts,
                    session_factory,
                    harness,
                )
                genesis_commit = genesis.commit_id
                genesis_commit_id_for_report = genesis_commit.root
            if stop_after_genesis:
                retrieval_attestation = self._attestation_for_commit(project_id, genesis_commit)
                retrieval_quality_eligible = retrieval_attestation.quality_eligible
                self._write_progress(
                    progress_path,
                    genesis_commit=genesis_commit_id_for_report,
                    last_chapter=0,
                    completed_chapters=[],
                )
                engine.dispose()
                return {
                    "status": "genesis_completed",
                    "bundle_id": bundle.bundle_id.root,
                    "genesis_commit": genesis_commit_id_for_report,
                    "semantic_backend": (
                        "scripted_contract_smoke"
                        if harness.responses is not None
                        else "configured_structured_generation_model"
                    ),
                    "retrieval_backend_profile": self._retrieval_backend_profile.value,
                    "retrieval_backend": self._retrieval_backend_profile.value,
                    "retrieval_attestation": retrieval_attestation.model_dump(mode="json"),
                    "retrieval_quality_eligible": retrieval_quality_eligible,
                    "semantic_quality_eligible": (
                        harness.responses is None and retrieval_quality_eligible
                    ),
                    "project_database": database_descriptor,
                    "project_directory": str(resolved_project_directory),
                }
            scenario = BenchmarkScenarioCompiler().compile(bundle, information_profile)
            chapter_sources = tuple(
                source
                for source in scenario.sources
                if source.source_class is SourceClass.CHAPTER_TEXT
            )
            non_chapter_sources = tuple(
                source
                for source in scenario.sources
                if source.source_class is not SourceClass.CHAPTER_TEXT
            )
            checkpoint_cases = scenario.checkpoint_cases
            if max_chapter is not None:
                chapter_sources = tuple(
                    source
                    for source in chapter_sources
                    if source.chapter_index is not None and source.chapter_index <= max_chapter
                )
                checkpoint_cases = tuple(
                    declaration
                    for declaration in scenario.checkpoint_cases
                    if declaration.checkpoint_chapter <= max_chapter
                )
            if resume:
                pending_sources = tuple(
                    source
                    for source in chapter_sources
                    if source.chapter_index is not None and source.chapter_index > resume_chapter
                )
                recover_checkpoint = (
                    not pending_sources
                    and max_chapter == resume_chapter
                    and any(
                        declaration.checkpoint_chapter == resume_chapter
                        for declaration in scenario.checkpoint_cases
                    )
                )
                chapter_sources = (
                    tuple(
                        source
                        for source in chapter_sources
                        if source.chapter_index == resume_chapter
                    )
                    if recover_checkpoint
                    else pending_sources
                )
                if recover_checkpoint:
                    checkpoint_cases = tuple(
                        declaration
                        for declaration in checkpoint_cases
                        if declaration.checkpoint_chapter == resume_chapter
                    )
            else:
                recover_checkpoint = False
            scenario_profile = scenario.profile
            if checkpoint_cases:
                scenario_profile = scenario.profile.model_copy(
                    update={
                        "checkpoint_chapters": tuple(
                            declaration.checkpoint_chapter for declaration in checkpoint_cases
                        )
                    }
                )
            scenario = scenario.model_copy(
                update={
                    "checkpoint_cases": checkpoint_cases,
                    "profile": scenario_profile,
                    "sources": (*bootstrap_bundle.sources, *non_chapter_sources, *chapter_sources),
                    "classifications": (
                        *(item.classification for item in ingested),
                        *scenario.classifications,
                    ),
                }
            )
            transition = _TeacherForcedTransition(
                bundle=bundle,
                information_profile=information_profile,
                artifacts=artifacts,
                commits=CommitService(session_factory),
                project_id=project_id,
                projections=DerivedProjectionService(
                    ProjectionOutboxRepository(session_factory),
                    ExactReplayProjectionBuilder(),
                    project_id=project_id,
                ),
                snapshots=DerivedSnapshotRepository(session_factory),
                harness=harness,
                current_text=text,
                current_world=world,
                current_plan=plan,
                profile_root_hash=project_profile.root_hash,
                real_hybrid_backend_provider=self._real_hybrid_backend_provider,
                recover_checkpoint_chapter=(resume_chapter if recover_checkpoint else None),
                quality_repair_flags=self._quality_repair_flags,
                memory_write_dry_run=self._memory_write_dry_run,
            )
            public_config = PublicBenchmarkConfig(
                schema_version=bundle.bundle_schema_version,
                configuration_fingerprint=self._paired.public_configuration_fingerprint(
                    bundle.bundle_schema_version.root
                ),
                expected_profiles=bundle.expected_profiles,
            )
            support_progress_events: list[dict[str, object]] = []
            support_progress_lock = threading.Lock()

            def record_support_progress(event: Mapping[str, object]) -> None:
                with support_progress_lock:
                    support_progress_events.append(
                        {
                            "sequence": len(support_progress_events) + 1,
                            "recorded_at": datetime.now(UTC).isoformat(),
                            **event,
                        }
                    )
                    progress_path = output_directory / "support_progress.json"
                    temporary_path = output_directory / "support_progress.json.tmp"
                    temporary_path.write_text(
                        json.dumps(
                            {
                                "state": "running",
                                "events": support_progress_events,
                            },
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    os.replace(temporary_path, progress_path)

            freezer = _E2EContextFreezer(
                public_config,
                transition,
                artifacts,
                self._paired,
                controller_policy_factory=harness.controller_request_factory,
                real_hybrid_backend_provider=self._real_hybrid_backend_provider,
                support_gateway=(harness.gateway if self._semantic_endpoint is not None else None),
                support_progress_writer=record_support_progress,
                support_pre_proposal_trace=self._support_pre_proposal_trace,
                support_max_concurrent_needs=self._support_max_concurrent_needs,
                support_max_inflight_kv_tokens=self._support_max_inflight_kv_tokens,
                support_admission_controller=self._support_admission_controller,
                frozen_planner_artifact=self._frozen_planner_artifact,
                support_transport_config=self._support_transport_config,
            )
            evaluator = _E2EEvaluator(
                bundle,
                information_profile,
                freezer,
                artifacts,
                self._paired,
                semantic_gateway=harness.gateway,
                model_semantic_verifier_enabled=self._semantic_endpoint is not None,
                max_concurrent_batches=self._evaluator_max_concurrent_batches,
            )
            segment_preamble = self._segment_preamble_count(progress_path) if resume else 0
            try:
                if scenario.checkpoint_cases:
                    with _ProgressWriter(progress_path) as progress_writer:
                        transition.progress_writer = progress_writer
                        scenario_result = TeacherForcedScenarioRunner(
                            transition,
                            freezer,
                            evaluator,
                            checkpoint_workers=self._checkpoint_workers,
                        ).run(scenario, genesis_commit)
                else:
                    with _ProgressWriter(progress_path) as progress_writer:
                        transition.progress_writer = progress_writer
                        scenario_result = self._run_without_checkpoints(
                            scenario, genesis_commit, transition
                        )
            except TeacherForcedControlledPause as pause:
                paused = pause.result
                pause_summary = {
                    "status": "teacher_forced_controlled_pause",
                    "bundle_id": bundle.bundle_id.root,
                    "information_profile": information_profile.value,
                    "last_revealed_chapter": transition.last_revealed_chapter,
                    "paused_chapter": pause.chapter,
                    "run_complete": False,
                    "segment_commit_count": transition.commit_count,
                    "memory_write_status_counts": transition.memory_write_status_counts,
                    "memory_write_proposal_attempts": (transition.memory_write_proposal_attempts),
                    "memory_write_proposal_rejections": (
                        transition.memory_write_proposal_rejections
                    ),
                    "memory_write_proposal_retry_counts": (
                        transition.memory_write_proposal_retry_counts
                    ),
                    "memory_write_proposal_poison_loops": (
                        transition.memory_write_proposal_poison_loops
                    ),
                    "memory_write_proposal_terminal_status": paused.status.value,
                    "memory_write_resume_checkpoint": (
                        None
                        if paused.checkpoint_ref is None
                        else paused.checkpoint_ref.model_dump(mode="json")
                    ),
                    "project_database": database_descriptor,
                    "project_directory": str(resolved_project_directory),
                }
                self._write_json(
                    output_directory / "memory_write_pause_trace.json",
                    {
                        "chapter": pause.chapter,
                        "result": paused.model_dump(mode="json"),
                    },
                )
                self._write_json(output_directory / "flow_summary.json", pause_summary)
                return pause_summary
            except TeacherForcedTerminalFailure as failure:
                terminal = failure.result
                failure_summary = {
                    "status": "teacher_forced_terminal_failure",
                    "bundle_id": bundle.bundle_id.root,
                    "information_profile": information_profile.value,
                    "last_revealed_chapter": transition.last_revealed_chapter,
                    "failed_chapter": failure.chapter,
                    "run_complete": False,
                    "segment_commit_count": transition.commit_count,
                    "memory_write_status_counts": transition.memory_write_status_counts,
                    "memory_write_proposal_attempts": (transition.memory_write_proposal_attempts),
                    "memory_write_proposal_rejections": (
                        transition.memory_write_proposal_rejections
                    ),
                    "memory_write_proposal_retry_counts": (
                        transition.memory_write_proposal_retry_counts
                    ),
                    "memory_write_proposal_poison_loops": (
                        transition.memory_write_proposal_poison_loops
                    ),
                    "memory_write_proposal_terminal_status": terminal.status.value,
                    "memory_write_resume_checkpoint": (
                        None
                        if terminal.checkpoint_ref is None
                        else terminal.checkpoint_ref.model_dump(mode="json")
                    ),
                    "terminal_codes": terminal.terminal_codes,
                    "project_database": database_descriptor,
                    "project_directory": str(resolved_project_directory),
                }
                self._write_json(
                    output_directory / "memory_write_failure_trace.json",
                    {
                        "chapter": failure.chapter,
                        "result": terminal.model_dump(mode="json"),
                    },
                )
                self._write_json(output_directory / "flow_summary.json", failure_summary)
                raise
            if (
                self._retrieval_backend_profile is RetrievalBackendProfile.REAL_HYBRID
                and not scenario_result.completed
            ):
                self._write_model(output_directory / "scenario_run.json", scenario_result)
                raise TeacherForcedBenchmarkError(
                    f"formal Stage 2M scenario lifecycle is incomplete: {scenario_result.blockers}"
                )
            (harness.responses or _ResponseBook()).assert_empty()
            if evaluator.results:
                paired_report = self._paired_report(
                    bundle,
                    information_profile,
                    evaluator.results,
                    controller_mode=self._quality_repair_flags.controller_mode,
                )
                self._write_model(output_directory / "e2e_paired_report.json", paired_report)
            else:
                paired_report = None
            formal_matrix = False
            if evaluator.stage2m_results:
                child_report_refs: list[dict[str, object]] = []
                for item in evaluator.stage2m_results:
                    report_bytes = canonical_json_bytes(item.model_dump(mode="json"))
                    report_ref = artifacts.put(
                        report_bytes,
                        "application/vnd.novel-agent.stage2m-case-arm-report+json",
                        VERSION,
                    )
                    child_report_refs.append(
                        {
                            "case_id": item.case_id.root,
                            "checkpoint": item.checkpoint_chapter,
                            "arm": item.arm,
                            "artifact_ref": report_ref.model_dump(mode="json"),
                        }
                    )
                    immutable_case_path = (
                        output_directory
                        / "reports"
                        / GATE_METRIC_FORMULA_VERSION
                        / information_profile.value
                        / item.case_id.root
                        / f"C{item.checkpoint_chapter}"
                        / item.arm
                        / f"{report_ref.artifact_id.root.removeprefix('sha256:')}.json"
                    )
                    self._write_immutable(immutable_case_path, report_bytes)
                    self._write_model(
                        output_directory
                        / f"stage2m_case_C{item.checkpoint_chapter}_{item.arm}.json",
                        item,
                    )
                report_manifest = {
                    "manifest_version": "stage2m_report_manifest.v1",
                    "bundle_hash": bundle.content_hash.root,
                    "profile": information_profile.value,
                    "formula_version": GATE_METRIC_FORMULA_VERSION,
                    "formula_hash": GATE_METRIC_FORMULA_HASH.root,
                    "children": sorted(
                        child_report_refs,
                        key=lambda child: (
                            int(cast(int, child["checkpoint"])),
                            str(child["case_id"]),
                            str(child["arm"]),
                        ),
                    ),
                }
                manifest_bytes = canonical_json_bytes(report_manifest)
                manifest_ref = artifacts.put(
                    manifest_bytes,
                    "application/vnd.novel-agent.stage2m-report-manifest+json",
                    VERSION,
                )
                self._write_immutable(
                    output_directory
                    / "report_manifests"
                    / f"{manifest_ref.artifact_id.root.removeprefix('sha256:')}.json",
                    manifest_bytes,
                )
                self._write_json(
                    output_directory / "report_index.json",
                    {
                        "latest_manifest_ref": manifest_ref.model_dump(mode="json"),
                        "formula_version": GATE_METRIC_FORMULA_VERSION,
                        "formula_hash": GATE_METRIC_FORMULA_HASH.root,
                    },
                )
                cases_by_arm = {
                    arm: tuple(item for item in evaluator.stage2m_results if item.arm == arm)
                    for arm in sorted({item.arm for item in evaluator.stage2m_results})
                }
                arm_a_cases = cases_by_arm.get("A", ())
                formal_matrix = {
                    item.checkpoint_chapter: item.case_id.root for item in arm_a_cases
                } == {
                    20: "ZTJ-P001",
                    40: "ZTJ-P002",
                    60: "ZTJ-P003",
                    80: "ZTJ-P004",
                    95: "ZTJ-P005",
                }
                if formal_matrix:
                    formal_report = MemoryBenchmarkReporter(
                        artifact_reader=artifacts.read_verified
                    ).aggregate(
                        profile=information_profile,
                        cases=arm_a_cases,
                        aggregation_manifest_ref=manifest_ref,
                    )
                    self._write_model(output_directory / "unified_report_A.json", formal_report)
                    self._write_model(output_directory / "unified_report.json", formal_report)
                for arm, arm_cases in cases_by_arm.items():
                    if arm == "A" and formal_matrix:
                        continue
                    diagnostic = MemoryBenchmarkReporter(
                        artifact_reader=artifacts.read_verified,
                        enforce_formal_contract=False,
                    ).aggregate(
                        profile=information_profile,
                        cases=arm_cases,
                        aggregation_manifest_ref=manifest_ref,
                    )
                    self._write_model(
                        output_directory / f"diagnostic_partial_report_{arm}.json",
                        diagnostic,
                    )
            self._write_model(output_directory / "scenario_run.json", scenario_result)
            if paired_report is not None:
                self._write_model(output_directory / "e2e_paired_report.json", paired_report)
            run_complete = transition.last_revealed_chapter == 95
            checkpoints = scenario_result.checkpoints
            chain_consistent = (
                all(item.future_isolation.passed for item in checkpoints)
                if checkpoints
                else transition.commit_count > 0
            )
            latest_attestation = (
                freezer.latest_attestation or transition.latest_attestation or resume_attestation
            )
            retrieval_quality_eligible = bool(
                latest_attestation is not None and latest_attestation.quality_eligible
            )
            scheduler_snapshot = (
                self._support_admission_controller.snapshot()
                if self._support_admission_controller is not None
                else None
            )
            lifecycle_statuses = self._execution_lifecycle_statuses(
                scheduler_snapshot,
                scenario_completed=scenario_result.completed,
                single_arm_result_count=sum(item.arm == "A" for item in evaluator.stage2m_results),
                checkpoint_count=len(scenario_result.checkpoints),
                generation_quality_eligible=harness.responses is None,
                retrieval_quality_eligible=retrieval_quality_eligible,
            )
            scheduling_failure_count = cast(int, lifecycle_statuses["scheduling_failure_count"])
            support_terminal_state = (
                self._support_terminal_state(
                    support_progress_events,
                    scenario_completed=scenario_result.completed,
                )
                if support_progress_events
                else "not_run"
            )
            summary: dict[str, Any] = {
                "status": (
                    "teacher_forced_real_hybrid_completed"
                    if self._retrieval_backend_profile is RetrievalBackendProfile.REAL_HYBRID
                    else "teacher_forced_contract_smoke_completed"
                ),
                "bundle_id": bundle.bundle_id.root,
                "information_profile": information_profile.value,
                "bootstrap_source_count": len(bootstrap_bundle.sources),
                "genesis_commit": genesis_commit_id_for_report,
                "genesis_author_approved": True,
                "teacher_forced_writer": True,
                "last_revealed_chapter": transition.last_revealed_chapter,
                "run_complete": run_complete,
                "segment_commit_count": transition.commit_count,
                "total_commit_count": segment_preamble + transition.commit_count,
                "segment_preamble_count": segment_preamble,
                "chapter_commit_count": transition.commit_count,
                "checkpoint_chapters": [item.last_revealed_chapter for item in checkpoints]
                if checkpoints
                else [],
                "frozen_checkpoint_inputs": [
                    {
                        "case_id": case_id.root,
                        "checkpoint_chapter": next(
                            case.history_range[1]
                            for case in bundle.case_manifests
                            if case.case_id == case_id
                        ),
                        "commit": transition.states[case_id].commit.root,
                        "comparison_ref": comparison_ref.model_dump(mode="json"),
                    }
                    for case_id, comparison_ref in sorted(
                        freezer.comparison_refs.items(), key=lambda item: item[0].root
                    )
                ],
                "checkpoint_chain_consistent": chain_consistent,
                "scenario_run_completed": scenario_result.completed,
                "checkpoint_scenario_status": cast(
                    str, lifecycle_statuses["checkpoint_scenario_status"]
                ),
                "single_arm_evaluation_status": cast(
                    str, lifecycle_statuses["single_arm_evaluation_status"]
                ),
                "paired_comparison_status": (
                    "NOT_RUN"
                    if paired_report is None or not paired_report.paired_results_count
                    else "COMPLETED"
                    if paired_report.paired_results_count == len(paired_report.cases)
                    else "PARTIAL"
                ),
                "matrix_evaluation_status": "COMPLETED" if formal_matrix else "INCOMPLETE",
                "scenario_run_blockers": scenario_result.blockers,
                "future_isolation_failure_count": sum(
                    not item.future_isolation.passed for item in checkpoints
                )
                if checkpoints
                else 0,
                "planner_agent_calls": transition.planner_calls + (0 if resume else 1),
                "curator_bootstrap_agent_calls": 0 if resume else 1,
                "curator_replay_agent_calls": transition.curator_calls,
                "guardian_agent_calls": transition.guardian_calls,
                "guardian_gate_decisions": transition.guardian_gate_decisions,
                "validator_calls": transition.validator_calls,
                "need_planner_artifact_refs": [
                    comparison.planner_artifact_ref.model_dump(mode="json")
                    for _, comparison in sorted(
                        freezer.comparisons.items(), key=lambda item: item[0].root
                    )
                    if comparison.planner_artifact_ref is not None
                ],
                "need_planner_invocation_count": sum(
                    comparison.planner_artifact_ref is not None
                    for comparison in freezer.comparisons.values()
                ),
                "need_planner_fallback_count": sum(
                    comparison.planner_fallback_used for comparison in freezer.comparisons.values()
                ),
                "need_planner_grounded_status_counts": [
                    sum(
                        comparison.grounded_status_counts[index]
                        for comparison in freezer.comparisons.values()
                    )
                    for index in range(3)
                ],
                "memory_write_status_counts": transition.memory_write_status_counts,
                "memory_write_candidate_revisions": transition.memory_write_candidate_revisions,
                "memory_write_repair_calls": transition.memory_write_repair_calls,
                "memory_write_normalization_passes": transition.memory_write_normalization_passes,
                "memory_write_guardian_reviews": transition.memory_write_guardian_reviews,
                "memory_write_context_refreshes": transition.memory_write_context_refreshes,
                "memory_write_transport_attempts": transition.memory_write_transport_attempts,
                "memory_write_tokens": transition.memory_write_tokens,
                "memory_write_proposal_attempts": transition.memory_write_proposal_attempts,
                "memory_write_proposal_rejections": transition.memory_write_proposal_rejections,
                "memory_write_proposal_retry_counts": (
                    transition.memory_write_proposal_retry_counts
                ),
                "memory_write_proposal_poison_loops": (
                    transition.memory_write_proposal_poison_loops
                ),
                "memory_write_proposal_terminal_status": (
                    transition.memory_write_proposal_terminal_status
                ),
                "memory_write_resume_checkpoint": transition.memory_write_resume_checkpoint,
                "paired_results_count": (
                    paired_report.paired_results_count if paired_report else 0
                ),
                "comparable_results_count": (
                    paired_report.comparable_results_count if paired_report else 0
                ),
                "future_leakage_count": (
                    paired_report.future_leakage_count if paired_report else 0
                ),
                "semantic_backend": (
                    "scripted_contract_smoke"
                    if harness.responses is not None
                    else "configured_structured_generation_model"
                ),
                "generation_quality_eligible": harness.responses is None,
                "retrieval_backend_profile": self._retrieval_backend_profile.value,
                "retrieval_backend": self._retrieval_backend_profile.value,
                "retrieval_attestation": (
                    latest_attestation.model_dump(mode="json")
                    if latest_attestation is not None
                    else self._scripted_smoke_attestation(
                        CommitService(session_factory).current_commit(project_id)
                    ).model_dump(mode="json")
                ),
                "retrieval_quality_eligible": retrieval_quality_eligible,
                "semantic_quality_eligible": cast(
                    bool, lifecycle_statuses["semantic_quality_eligible"]
                ),
                "curator_semantic_extraction_enabled": harness.responses is None,
                "quality_blocker": cast(str, lifecycle_statuses["quality_blocker"]),
                "quality_repair_flags": transition.quality_repair_flags.model_dump(mode="json"),
                "postcommit_recovery": postcommit_recovery,
                "project_database": database_descriptor,
                "project_directory": str(resolved_project_directory),
                "model_request_scheduler": scheduler_snapshot,
                "scheduling_failure_count": scheduling_failure_count,
                "support_terminal_state": support_terminal_state,
                "model_call_records": [
                    record.model_dump(mode="json") for record in harness.gateway.call_records
                ],
            }
            if support_progress_events:
                progress_path = output_directory / "support_progress.json"
                temporary_path = output_directory / "support_progress.json.tmp"
                temporary_path.write_text(
                    json.dumps(
                        {
                            "state": support_terminal_state,
                            "events": support_progress_events,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary_path, progress_path)
            self._write_json(output_directory / "flow_summary.json", summary)
            return summary
        finally:
            engine.dispose()

    def _require_registered_retrieval_backend(self) -> None:
        if (
            self._retrieval_backend_profile is RetrievalBackendProfile.REAL_HYBRID
            and self._real_hybrid_backend_provider is None
        ):
            raise TeacherForcedBenchmarkError(
                "real_hybrid requires the Stage 2R commit-scoped FullDerivedProjectionBuilder "
                "and CompositeRetrievalBackend provider; scripted smoke fallback is disabled"
            )

    @staticmethod
    def _segment_preamble_count(progress_path: Path) -> int:
        completed = json.loads(progress_path.read_text("utf-8")).get("completed_chapters", [])
        return len(completed) + (0 if 0 in completed else 1)

    @staticmethod
    def _database_descriptor(database_url: str, database_path: Path) -> str:
        parsed = urlparse(database_url)
        if parsed.scheme.startswith("postgresql+"):
            return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}/{parsed.path.lstrip('/')}"
        return str(database_path)

    @staticmethod
    def _support_terminal_state(
        support_progress_events: list[dict[str, object]],
        *,
        scenario_completed: bool,
    ) -> str:
        """Resolve the terminal support-progress state.

        The producer records an explicit three-state terminal event; a legacy
        event stream without one falls back to the scenario lifecycle result.
        """
        terminal_events = [
            event
            for event in support_progress_events
            if event.get("stage") == "terminal" and "state" in event
        ]
        if terminal_events:
            return str(terminal_events[-1]["state"])
        return (
            "completed"
            if scenario_completed
            and not any(event.get("status") == "failed" for event in support_progress_events)
            else "completed_with_failures"
        )

    @staticmethod
    def _execution_lifecycle_statuses(
        scheduler_snapshot: dict[str, object] | None,
        *,
        scenario_completed: bool,
        single_arm_result_count: int,
        checkpoint_count: int,
        generation_quality_eligible: bool,
        retrieval_quality_eligible: bool,
    ) -> dict[str, str | int | bool]:
        """Reconcile scheduling infrastructure failure with quality eligibility.

        A typed scheduling timeout is an execution-infrastructure failure: it
        must demote the scenario and single-arm lifecycle states, block
        semantic quality eligibility, and surface a non-empty quality blocker.
        Model output/validation outcomes remain measurable model behavior and
        do not by themselves become an infrastructure blocker.
        """
        scheduling_failure_count = (
            cast(int, scheduler_snapshot["scheduling_timeouts"])
            if scheduler_snapshot is not None
            else 0
        )
        has_scheduling_failure = scheduling_failure_count > 0
        return {
            "scheduling_failure_count": scheduling_failure_count,
            "checkpoint_scenario_status": (
                "COMPLETED_WITH_EXECUTION_FAILURE"
                if scenario_completed and has_scheduling_failure
                else "COMPLETED"
                if scenario_completed
                else "INCOMPLETE"
            ),
            "single_arm_evaluation_status": (
                "COMPLETED_WITH_EXECUTION_FAILURE"
                if has_scheduling_failure and single_arm_result_count > 0
                else "COMPLETED"
                if single_arm_result_count == checkpoint_count and checkpoint_count > 0
                else "NOT_RUN"
                if single_arm_result_count == 0
                else "PARTIAL"
            ),
            "semantic_quality_eligible": (
                generation_quality_eligible
                and retrieval_quality_eligible
                and not has_scheduling_failure
            ),
            "quality_blocker": (
                "SCHEDULING_INFRASTRUCTURE_FAILURE"
                if has_scheduling_failure
                else TeacherForcedBenchmarkE2ERunner._quality_blocker(
                    generation_quality_eligible, retrieval_quality_eligible
                )
            ),
        }

    def _scripted_smoke_attestation(self, source_commit: CommitId) -> ProjectionAttestation:
        snapshot_id = snapshot_id_for_commit(source_commit)
        return ProjectionAttestation(
            attestation_id=StableId(
                f"attestation.scripted-smoke.{source_commit.root.removeprefix('sha256:')[:24]}"
            ),
            retrieval_backend_profile=RetrievalBackendProfile.SCRIPTED_SMOKE,
            source_commit=source_commit,
            snapshot_id=snapshot_id,
            capability=SnapshotCapability(
                source_commit=source_commit,
                snapshot_id=snapshot_id,
                status=SnapshotCapabilityStatus.TEST_ONLY,
            ),
            r1_record_count=0,
            r1_entity_association_count=0,
            graph_node_count=0,
            graph_edge_count=0,
        )

    def _attestation_for_commit(
        self,
        project_id: ProjectId,
        source_commit: CommitId,
    ) -> ProjectionAttestation:
        if self._retrieval_backend_profile is RetrievalBackendProfile.SCRIPTED_SMOKE:
            return self._scripted_smoke_attestation(source_commit)
        if self._real_hybrid_backend_provider is None:  # guarded before any state mutation
            raise TeacherForcedBenchmarkError("real_hybrid backend provider is not configured")
        backend_bundle = self._real_hybrid_backend_provider(project_id, source_commit)
        return backend_bundle.attestation

    @staticmethod
    def _quality_blocker(
        generation_quality_eligible: bool,
        retrieval_quality_eligible: bool,
    ) -> str:
        blockers: list[str] = []
        if not generation_quality_eligible:
            blockers.insert(
                0,
                "replace scripted model endpoint with a configured structured generation model",
            )
        if not retrieval_quality_eligible:
            blockers.append(
                "real_hybrid capability and projection attestation are required; "
                "scripted_smoke retrieval is test-only"
            )
        return "; ".join(blockers)

    @staticmethod
    def _load_bootstrap(
        source_directory: Path,
        project_id: ProjectId,
        artifacts: ArtifactRepository,
    ) -> tuple[Any, tuple[IngestedBootstrapSource, ...]]:
        bootstrap_directory = (source_directory / "bootstrap").resolve()
        manifest = yaml.safe_load(
            (bootstrap_directory / "bootstrap_manifest.yaml").read_text("utf-8")
        )
        raw: list[RawBootstrapSource] = []
        for item in manifest["sources"]:
            path = (bootstrap_directory / item["path"]).resolve()
            if bootstrap_directory not in path.parents:
                raise TeacherForcedBenchmarkError("bootstrap source escapes its directory")
            raw.append(
                RawBootstrapSource(
                    source_id=StableId(item["source_id"]),
                    source_class=SourceClass(item["source_class"]),
                    media_type=item["media_type"],
                    data=path.read_bytes(),
                )
            )
        return BootstrapIngestionService(artifacts).ingest(
            project_id,
            StableId(manifest["bootstrap_id"]),
            tuple(raw),
            VERSION,
        )

    def _genesis(
        self,
        project_id: ProjectId,
        bootstrap_bundle_id: StableId,
        ingested: tuple[IngestedBootstrapSource, ...],
        information_profile: BenchmarkInformationProfile,
        artifacts: ArtifactRepository,
        session_factory: Any,
        harness: _AgentHarness,
    ) -> tuple[
        Any,
        TextRootDocument,
        WorldRootDocument,
        PlanRootDocument,
        ProjectProfileRootDocument,
    ]:
        visible = tuple(
            item
            for item in ingested
            if information_profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
            or item.source.source_class is not SourceClass.AUTHOR_KNOWN_FUTURE_PLAN
        )
        planner_draft = self._planner_bootstrap_draft(visible)
        planner_request = self._request("planner.bootstrap", AgentMode.PROJECT_BOOTSTRAP)
        self._script(harness, planner_request, planner_draft)
        planner_result, _ = asyncio.run(
            PlannerAgent(harness.runner, artifacts).run(
                version=VERSION,
                task=PlanningTask(
                    planning_task_id=StableId("planning.teacher-forced.bootstrap"),
                    project_id=project_id,
                    mode=AgentMode.PROJECT_BOOTSTRAP,
                    source_ids=tuple(item.source.source_id for item in visible),
                    creative_scope=("project_intent", "story", "world_design", "profile"),
                    strategy=BootstrapStrategy.NORMALIZE_ONLY,
                ),
                source_payload=self._source_payload(visible),
                source_artifacts=tuple(item.source.artifact_ref for item in visible),
                request=planner_request,
            )
        )
        baseline = tuple(
            item for item in ingested if item.source.source_class is SourceClass.BASELINE_SETTING
        )
        curator_request = self._request("curator.bootstrap", AgentMode.BOOTSTRAP)
        self._script(harness, curator_request, self._curator_bootstrap_draft(baseline))
        world_patch, _ = asyncio.run(
            CuratorBootstrapAgent(harness.runner, artifacts).run(
                version=VERSION,
                project_id=project_id,
                source_ids=tuple(item.source.source_id for item in baseline),
                source_payload=self._source_payload(baseline),
                source_artifacts=tuple(item.source.artifact_ref for item in baseline),
                request=curator_request,
            )
        )
        text = SequentialTextRootService().empty(VERSION)
        plan = self._bootstrap_plan_root(visible, information_profile)
        world = self._world_root(world_patch)
        reference = ReferenceRootDocument(
            root_hash=ZERO_HASH,
            schema_version=VERSION,
            assets=tuple(
                ReferenceAsset(
                    asset_id=StableId(f"reference.{item.source.source_id.root}"),
                    source_id=item.source.source_id,
                    source_class=item.source.source_class,
                    artifact=item.source.artifact_ref,
                )
                for item in visible
                if item.reference_candidate is not None
            ),
        )
        project_profile = self._profile_root(harness, visible)
        candidates = BootstrapRootBuilder(artifacts).build(
            project_id,
            bootstrap_bundle_id,
            text,
            plan,
            world,
            reference,
            project_profile,
            planner_result.plan_proposal,
            world_patch,
            tuple(item.classification for item in ingested),
        )
        validation = BootstrapCrossRootValidator().validate(candidates)
        if validation.status is not ValidationStatus.PASSED:
            raise TeacherForcedBenchmarkError(
                f"bootstrap validation failed: {[item.code for item in validation.findings]}"
            )
        approvals = SqlAuthorApprovalRepository(session_factory)
        coordinator = GenesisCoordinator(CommitService(session_factory), approvals)
        approval = coordinator.create_approval_request(candidates, validation)
        approvals.decide(
            AuthorApprovalDecision(
                decision_id=StableId("approval-decision.teacher-forced.simulated-author"),
                approval_request_id=approval.approval_request_id,
                project_id=project_id,
                candidate_manifest_hash=approval.candidate_manifest_hash,
                validation_report_id=approval.validation_report_id,
                status=AuthorApprovalStatus.APPROVED,
                author_id=StableId("author-role.benchmark-harness"),
                reason="simulated author approved the validated reconstructed C0 candidates",
                decided_at=datetime.now(UTC),
            )
        )
        genesis = coordinator.commit(candidates, validation, approval.approval_request_id)
        return genesis, candidates.text, candidates.world, candidates.plan, candidates.profile

    @staticmethod
    def _source_payload(items: tuple[IngestedBootstrapSource, ...]) -> str:
        return "\n\n".join(f"SOURCE={item.source.source_id.root}\n{item.parsed}" for item in items)

    @staticmethod
    def _planner_bootstrap_draft(
        items: tuple[IngestedBootstrapSource, ...],
    ) -> PlannerProposalDraft:
        proposed = tuple(
            ProposedItem(
                item_id=StableId(f"intent.bootstrap.{index}"),
                kind=item.source.source_class.value,
                payload={"summary": str(item.parsed)[:1000]},
                provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                source_ids=(item.source.source_id,),
            )
            for index, item in enumerate(items)
        )
        return PlannerProposalDraft(
            mode=AgentMode.PROJECT_BOOTSTRAP,
            strategy=BootstrapStrategy.NORMALIZE_ONLY,
            project_intent_items=proposed,
            plan_items=tuple(
                item
                for item in proposed
                if item.kind
                in {
                    SourceClass.AUTHOR_INITIAL_BRIEF.value,
                    SourceClass.AUTHOR_KNOWN_FUTURE_PLAN.value,
                }
            ),
            world_design_items=tuple(
                item for item in proposed if item.kind == SourceClass.BASELINE_SETTING.value
            ),
            profile_items=tuple(
                item for item in proposed if item.kind == SourceClass.STYLE_GUIDE.value
            ),
            coverage=1,
        )

    @staticmethod
    def _curator_bootstrap_draft(
        items: tuple[IngestedBootstrapSource, ...],
    ) -> CuratorBootstrapDraft:
        candidates: list[ProposedItem] = []
        for source in items:
            facts = re.findall(
                r"\[WORLD_FACT_AT_STORY_OPEN\]\s*([^\n]+)",
                str(source.parsed),
            )
            offset = len(candidates)
            candidates.extend(
                ProposedItem(
                    item_id=StableId(f"world.bootstrap.fact.{offset + index}"),
                    kind="baseline_state",
                    payload={"fact": fact.strip()},
                    provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                    source_ids=(source.source.source_id,),
                )
                for index, fact in enumerate(facts)
            )
        return CuratorBootstrapDraft(
            items=tuple(candidates),
            extraction_coverage=1 if candidates else 0,
            unresolved_claims=() if candidates else ("no labelled opening facts found",),
        )

    @staticmethod
    def _bootstrap_plan_root(
        items: tuple[IngestedBootstrapSource, ...],
        profile: BenchmarkInformationProfile,
    ) -> PlanRootDocument:
        if profile is BenchmarkInformationProfile.VISIBLE_AT_CUTOFF:
            provisional = PlanRootDocument(
                root_hash=ZERO_HASH,
                schema_version=VERSION,
            )
            return provisional.model_copy(update={"root_hash": plan_root_content_id(provisional)})

        nodes: list[PlanNode] = []
        for item in items:
            if item.source.source_class not in {
                SourceClass.AUTHOR_INITIAL_BRIEF,
                SourceClass.AUTHOR_KNOWN_FUTURE_PLAN,
            }:
                continue
            source_slug = item.source.source_id.root.removeprefix("bootstrap.").replace("_", "-")
            section = "intent"
            counters: dict[str, int] = {}
            for raw_line in str(item.parsed).splitlines():
                line = raw_line.strip()
                heading = re.fullmatch(r"##\s+(.+)", line)
                if heading is not None:
                    title = heading.group(1)
                    section = {
                        "核心创作目标": "goal",
                        "人物初始设想": "character",
                        "预期阅读体验": "experience",
                        "长篇方向": "direction",
                        "第一卷前 100 章的模拟节拍": "range",
                        "规划确定度": "certainty",
                    }.get(title, "intent")
                    continue
                range_item = re.match(
                    r"^-\s+`\[PLAN\s+(\d+)(?:\u2013|-)(\d+)\]`\s*(.+)$",
                    line,
                )
                if range_item is not None:
                    start, end, summary = range_item.groups()
                    node_id = StableId(f"plan.bootstrap.{source_slug}.range.{start}-{end}")
                    title = f"Coarse range {start}-{end}"
                else:
                    list_item = re.match(r"^(?:\d+\.|-)\s+(.+)$", line)
                    if list_item is None:
                        continue
                    summary = re.sub(r"[`*]", "", list_item.group(1)).strip()
                    counters[section] = counters.get(section, 0) + 1
                    node_id = StableId(
                        f"plan.bootstrap.{source_slug}.{section}.{counters[section]}"
                    )
                    title = f"{section.replace('-', ' ').title()} {counters[section]}"
                nodes.append(
                    PlanNode(
                        plan_node_id=node_id,
                        node_type="bootstrap_author_intent",
                        title=title,
                        summary=summary,
                    )
                )
        provisional = PlanRootDocument(
            root_hash=ZERO_HASH,
            schema_version=VERSION,
            nodes=tuple(nodes),
        )
        return provisional.model_copy(update={"root_hash": plan_root_content_id(provisional)})

    @staticmethod
    def _world_root(candidate: WorldPatchCandidate) -> WorldRootDocument:
        setting_id = StableId("entity.bootstrap.story-world")
        chen_id = StableId("entity.bootstrap.chen-changsheng")
        provisional = WorldRootDocument(
            root_hash=ZERO_HASH,
            schema_version=VERSION,
            source_commit=ZERO_COMMIT,
            entities=(
                Entity(entity_id=setting_id, entity_type="setting", internal_label="故事世界"),
                Entity(entity_id=chen_id, entity_type="character", internal_label="陈长生"),
            ),
            states=tuple(
                StateRecord(
                    state_id=StableId(f"state.bootstrap.fact.{index}"),
                    subject_id=(
                        chen_id if "陈长生" in str(item.payload.get("fact")) else setting_id
                    ),
                    predicate=f"opening_fact_{index + 1}",
                    value=str(item.payload.get("fact", "")),
                    valid_time=StoryTime(worldline="main", start_ordinal=0),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                )
                for index, item in enumerate(candidate.items)
            ),
        )
        return provisional.model_copy(update={"root_hash": world_root_content_id(provisional)})

    @staticmethod
    def _profile_root(
        harness: _AgentHarness,
        visible: tuple[IngestedBootstrapSource, ...],
    ) -> ProjectProfileRootDocument:
        return ProjectProfileRootDocument(
            root_hash=ZERO_HASH,
            schema_version=VERSION,
            style_profile={
                "sources": [
                    item.source.source_id.root
                    for item in visible
                    if item.source.source_class is SourceClass.STYLE_GUIDE
                ]
            },
            capability_profile={"teacher_forced_writer": True},
            agent_specs=tuple(
                ContractRef(
                    contract_id=spec.agent_id,
                    version=spec.version,
                    content_hash=spec.content_hash,
                )
                for spec in harness.specs
            ),
            prompt_contracts=harness.prompt_refs,
            skill_contracts=harness.skill_refs,
            tool_policies=tuple(
                ContractRef(
                    contract_id=spec.tool_policy.policy_id,
                    version=spec.tool_policy.version,
                    content_hash=spec.tool_policy.content_hash,
                )
                for spec in harness.specs
            ),
            model_profiles=("scripted-contract-smoke-v1",),
        )

    @staticmethod
    def _request(identity: str, mode: AgentMode) -> ModelRequest:
        suffix = identity.replace("_", "-")
        # Transport configuration for the local endpoint.  E2E agent requests
        # (bootstrap, replay curator, controller, guardian, support) keep
        # thinking disabled: with strict json_schema framing the drafts are
        # short and deterministic, keeping proposal, repair, and support calls
        # well inside the 900s transport ceiling (output ceiling 12288, decode
        # ~18 tokens/s without thinking).  The graph page extraction
        # sub-request is the single exception: it overrides this base request
        # in ModelCurator._extract_graph_page with bounded thinking (budget
        # 2048) so the model can satisfy the relation-endpoint contract before
        # emitting candidates.
        return ModelRequest(
            request_id=StableId(f"request.teacher-forced.{suffix}"),
            run_id=RunId("run.teacher-forced.e2e"),
            task_id=TaskId(f"task.teacher-forced.{suffix}"),
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.BATCH_TEST,
            trace_id=f"trace-teacher-forced-{mode.value}-{suffix}",
            prompt="replaced by StructuredAgentRunner",
            max_output_tokens=12288,
            timeout_seconds=900,
            enable_thinking=False,
            thinking_token_budget=None,
            scheduling_stage=f"agent_{suffix}",
        )

    @staticmethod
    def _script(harness: _AgentHarness, request: ModelRequest, model: Any) -> None:
        if harness.responses is not None:
            harness.responses.add(request.request_id, model)

    @staticmethod
    def build_model_harness(
        endpoint: ModelEndpointPort,
        *,
        quality_repair_flags: QualityRepairFeatureFlags | None = None,
        admission_controller: ModelRequestAdmissionController | None = None,
        scheduling_timeout_seconds: float = 120.0,
        provider_thinking_on: bool = False,
        thinking_token_budget: int = 2_048,
        call_ledger: Any | None = None,
        raw_artifacts: ArtifactRepository | None = None,
    ) -> _AgentHarness:
        """Build the shared semantic Planner/Controller harness for read-side products."""

        if thinking_token_budget < 0:
            raise ValueError("thinking token budget must be non-negative")

        return TeacherForcedBenchmarkE2ERunner._agent_harness(
            endpoint,
            quality_repair_flags=quality_repair_flags,
            admission_controller=admission_controller,
            scheduling_timeout_seconds=scheduling_timeout_seconds,
            provider_thinking_on=provider_thinking_on,
            thinking_token_budget=thinking_token_budget,
            call_ledger=call_ledger,
            raw_artifacts=raw_artifacts,
        )

    @staticmethod
    def _agent_harness(
        endpoint: ModelEndpointPort | None = None,
        *,
        quality_repair_flags: QualityRepairFeatureFlags | None = None,
        admission_controller: ModelRequestAdmissionController | None = None,
        scheduling_timeout_seconds: float = 120.0,
        provider_thinking_on: bool = False,
        thinking_token_budget: int = 2_048,
        call_ledger: Any | None = None,
        raw_artifacts: ArtifactRepository | None = None,
    ) -> _AgentHarness:
        package_root = Path(__file__).parents[1]
        prompt_directory = package_root / "prompts"
        skill_directory = package_root / "skills"
        prompt_defs = {
            "system": ("prompt.system-policy", "system_policy_v1.md"),
            "planner_bootstrap": (
                "prompt.planner-project-bootstrap",
                "planner_project_bootstrap_v1.md",
            ),
            "planner_chapter": ("prompt.planner-chapter", "planner_chapter_v1.md"),
            "curator_bootstrap": ("prompt.curator-bootstrap", "curator_bootstrap_v1.md"),
            "curator_replay": ("prompt.curator-replay", "curator_replay_v1.md"),
            "curator_repair": ("prompt.curator-repair", "curator_repair_v1.md"),
            "guardian": ("prompt.guardian-risk-review", "guardian_risk_review_v1.md"),
            "controller": (
                "prompt.memory-controller",
                "memory_controller_v1.md",
            ),
        }
        skill_defs = {
            "planner_bootstrap": (
                "skill.project-intent-modeling",
                "project_intent_modeling_v1.md",
            ),
            "planner_chapter": (
                "skill.chapter-goal-decomposition",
                "chapter_goal_decomposition_v1.md",
            ),
            "curator_bootstrap": ("skill.setting-to-world", "setting_to_world_v1.md"),
            "curator_replay": (
                "skill.memory-delta-extraction",
                "memory_delta_extraction_v1.md",
            ),
            "guardian": ("skill.memory-risk-review", "memory_risk_review_v1.md"),
            "controller_iterative": (
                "skill.iterative-retrieval",
                "iterative_retrieval_v1.md",
            ),
            "controller_evidence": (
                "skill.evidence-sufficiency",
                "evidence_sufficiency_v1.md",
            ),
            "controller_reduction": (
                "skill.context-reduction",
                "context_reduction_v1.md",
            ),
        }
        prompt_refs: dict[str, PromptContractRef] = {}
        prompt_templates: list[PromptTemplate] = []
        for key, (identity, filename) in prompt_defs.items():
            path = prompt_directory / filename
            digest = content_hash(path.read_bytes())
            prompt_ref = PromptContractRef(
                contract_id=StableId(identity),
                version=VERSION,
                content_hash=digest,
                render_fingerprint=digest,
            )
            prompt_refs[key] = prompt_ref
            prompt_templates.append(PromptTemplate(prompt_ref.contract_id, VERSION, path, digest))
        skill_refs: dict[str, SkillContractRef] = {}
        skill_templates: list[SkillTemplate] = []
        for key, (identity, filename) in skill_defs.items():
            path = skill_directory / filename
            digest = content_hash(path.read_bytes())
            skill_ref = SkillContractRef(
                contract_id=StableId(identity),
                version=VERSION,
                content_hash=digest,
            )
            skill_refs[key] = skill_ref
            skill_templates.append(SkillTemplate(skill_ref.contract_id, VERSION, path, digest))

        def spec(
            agent_type: AgentType,
            mode: AgentMode,
            task_key: str,
            skill_key: str,
            output_name: str,
        ) -> AgentSpec:
            policy = seal_tool_policy(
                ToolPolicy(
                    policy_id=StableId(f"policy.{agent_type.value}.{mode.value}.smoke"),
                    version=VERSION,
                    content_hash=ZERO_HASH,
                    allowed_tools=(),
                    permission=ToolPermission.PROPOSE,
                    max_tool_calls=0,
                )
            )
            schema_ref = ContractRef(
                contract_id=StableId(f"schema.{output_name}"),
                version=VERSION,
                content_hash=content_id({"schema": output_name}),
            )
            return seal_agent_spec(
                AgentSpec(
                    agent_id=StableId(f"agent.{agent_type.value}.{mode.value}"),
                    agent_type=agent_type,
                    mode=mode,
                    version=VERSION,
                    content_hash=ZERO_HASH,
                    input_schema=schema_ref,
                    output_schema=schema_ref,
                    system_prompt=prompt_refs["system"],
                    task_prompt=prompt_refs[task_key],
                    skills=(skill_refs[skill_key],),
                    tool_policy=policy,
                )
            )

        specs = (
            spec(
                AgentType.PLANNER,
                AgentMode.PROJECT_BOOTSTRAP,
                "planner_bootstrap",
                "planner_bootstrap",
                "planner-proposal-draft",
            ),
            spec(
                AgentType.PLANNER,
                AgentMode.CHAPTER,
                "planner_chapter",
                "planner_chapter",
                "planner-proposal-draft",
            ),
            spec(
                AgentType.MEMORY_CURATOR,
                AgentMode.BOOTSTRAP,
                "curator_bootstrap",
                "curator_bootstrap",
                "curator-bootstrap-draft",
            ),
            spec(
                AgentType.MEMORY_CURATOR,
                AgentMode.REPLAY,
                "curator_replay",
                "curator_replay",
                "chapter-change-draft",
            ),
            spec(
                AgentType.MEMORY_CURATOR,
                AgentMode.CURATOR_REPAIR,
                "curator_repair",
                "curator_replay",
                "chapter-change-draft",
            ),
            spec(
                AgentType.MEMORY_GUARDIAN,
                AgentMode.RISK_REVIEW,
                "guardian",
                "guardian",
                "guardian-decision-draft",
            ),
        )
        controller_spec_base = AgentSpec(
            agent_id=StableId("agent.memory-controller.bounded-r2"),
            agent_type=AgentType.MEMORY_CONTROLLER,
            mode=AgentMode.BOUNDED_R2,
            version=VERSION,
            content_hash=ZERO_HASH,
            input_schema=ContractRef(
                contract_id=StableId("schema.controller-policy-decision"),
                version=VERSION,
                content_hash=content_id({"schema": "controller-policy-decision"}),
            ),
            output_schema=ContractRef(
                contract_id=StableId("schema.controller-policy-decision"),
                version=VERSION,
                content_hash=content_id({"schema": "controller-policy-decision"}),
            ),
            system_prompt=prompt_refs["system"],
            task_prompt=prompt_refs["controller"],
            skills=(
                skill_refs["controller_iterative"],
                skill_refs["controller_evidence"],
                skill_refs["controller_reduction"],
            ),
            tool_policy=seal_tool_policy(
                ToolPolicy(
                    policy_id=StableId("policy.memory-controller.bounded-r2.smoke"),
                    version=VERSION,
                    content_hash=ZERO_HASH,
                    allowed_tools=(),
                    permission=ToolPermission.PROPOSE,
                    max_tool_calls=0,
                )
            ),
        )
        responses = _ResponseBook() if endpoint is None else None
        if responses is not None:
            selected_endpoint: ModelEndpointPort = ScriptedModelEndpoint(responses.resolve)
            endpoint_name = "scripted-teacher-forced"
            model_name = "scripted-contract-smoke"
            is_semantic = False
        elif endpoint is not None:
            selected_endpoint = endpoint
            endpoint_name = "local-openai-chat"
            model_name = getattr(endpoint, "model", "local-model")
            is_semantic = True
        else:  # pragma: no cover - the branches above exhaust the constructor state
            raise AssertionError("semantic endpoint resolution failed")
        endpoint_output_tokens = getattr(selected_endpoint, "max_output_tokens", None)
        named_output_limit = (
            endpoint_output_tokens
            if isinstance(endpoint_output_tokens, int) and endpoint_output_tokens >= 1
            else None
        )
        endpoint_sequence_limit = getattr(selected_endpoint, "sequence_limit", None)
        if not isinstance(endpoint_sequence_limit, int) or endpoint_sequence_limit < 1:
            endpoint_sequence_limit = DEFAULT_SEQUENCE_LIMIT
        endpoint_safety_allowance = getattr(selected_endpoint, "safety_allowance_tokens", None)
        if not isinstance(endpoint_safety_allowance, int) or endpoint_safety_allowance < 0:
            endpoint_safety_allowance = 256
        registered_endpoint = RegisteredModelEndpoint(
            role=ModelRole.BATCH_TEST,
            endpoint_name=endpoint_name,
            model_name=model_name,
            adapter=selected_endpoint,
            sequence_limit=endpoint_sequence_limit,
            output_limit=named_output_limit,
            safety_allowance_tokens=endpoint_safety_allowance,
            estimated_reasoning_reserve=thinking_token_budget,
            default_thinking=provider_thinking_on,
            reasoning_included_in_completion_tokens=False,
        )
        gateway = ModelGateway(
            (registered_endpoint,),
            # Strict schema generation and workflow-level repair own semantic
            # correction. Transport retry policy must not silently enable an
            # additional full-prompt structured-output retry.
            structured_max_retries=0,
            call_ledger=call_ledger,
            admission_controller=admission_controller,
            raw_artifacts=raw_artifacts,
            raw_artifact_schema_version=VERSION,
            scheduling_timeout_seconds=scheduling_timeout_seconds,
        )
        all_specs = (*specs,) if not is_semantic else (*specs, controller_spec_base)
        runner = StructuredAgentRunner(
            gateway,
            AgentRegistry(all_specs),
            PromptRegistry(prompt_templates),
            SkillRegistry(skill_templates),
        )

        def controller_policy_factory(
            sealed_tool_policy: ToolPolicy,
            route_plans: tuple[RoutePlan, ...] = (),
        ) -> Any:
            from novel_agent.agents.controller import StructuredControllerPolicy

            sealed_spec = seal_agent_spec(
                controller_spec_base.model_copy(update={"tool_policy": sealed_tool_policy})
            )
            if sealed_tool_policy.content_hash != sealed_spec.tool_policy.content_hash:
                raise TeacherForcedBenchmarkError("controller sealed ToolPolicy hash mismatch")
            controller_runner = StructuredAgentRunner(
                gateway,
                AgentRegistry((sealed_spec,)),
                PromptRegistry(prompt_templates),
                SkillRegistry(skill_templates),
            )

            def request_factory(state: Any, round_index: int) -> ModelRequest:
                req = state["request"]
                return ModelRequest(
                    request_id=StableId(f"request.controller.{req.request_id.root}.r{round_index}"),
                    run_id=req.run_id,
                    task_id=req.task_id,
                    model_role=ModelRole.BATCH_TEST,
                    purpose=ModelCallPurpose.BATCH_TEST,
                    trace_id=(f"trace-controller-{req.request_id.root}-r{round_index}"),
                    prompt="replaced by StructuredAgentRunner",
                    max_output_tokens=named_output_limit,
                    timeout_seconds=900,
                    enable_thinking=(None if provider_thinking_on else False),
                    thinking_token_budget=(thinking_token_budget if provider_thinking_on else None),
                    scheduling_stage="memory_controller",
                )

            policy = StructuredControllerPolicy(
                controller_runner,
                sealed_spec,
                request_factory,
                route_plans=route_plans,
                max_decision_model_calls=(
                    quality_repair_flags.max_controller_decision_model_calls
                    if quality_repair_flags is not None
                    else 2
                ),
                max_agentic_actions=(
                    quality_repair_flags.max_agentic_actions
                    if quality_repair_flags is not None
                    else 8
                ),
            )
            if policy.tool_policy_hash != sealed_tool_policy.content_hash:
                raise TeacherForcedBenchmarkError(
                    "StructuredControllerPolicy tool_policy_hash mismatch after construction"
                )
            return policy

        return _AgentHarness(
            runner=runner,
            gateway=gateway,
            endpoint=selected_endpoint,
            responses=responses,
            specs=all_specs,
            prompt_refs=tuple(prompt_refs.values()),
            skill_refs=tuple(skill_refs.values()),
            controller_spec=controller_spec_base if is_semantic else None,
            controller_request_factory=controller_policy_factory if is_semantic else None,
        )

    @staticmethod
    def _paired_report(
        bundle: BenchmarkBundle,
        profile: BenchmarkInformationProfile,
        results: tuple[PairedPilotCaseResult, ...],
        *,
        controller_mode: ControllerMode = ControllerMode.DETERMINISTIC,
    ) -> Stage2PairedPilotReport:
        if not results:
            raise TeacherForcedBenchmarkError("teacher-forced run produced no paired results")
        fingerprints = {item.comparison_basis_fingerprint for item in results}
        if len(fingerprints) != 1:
            raise TeacherForcedBenchmarkError("paired results used different comparison bases")
        fingerprint = next(iter(fingerprints))
        return Stage2PairedPilotReport(
            report_id=StableId(f"stage2-e2e.{bundle.bundle_id.root}.{profile.value}"),
            bundle_hash=bundle.content_hash,
            configuration_fingerprint=fingerprint,
            controller_mode=controller_mode,
            cases=results,
            paired_results_count=sum(
                item.agentic_execution_status is ArmExecutionStatus.COMPLETED for item in results
            ),
            comparable_results_count=sum(item.comparable for item in results),
            future_leakage_count=sum(
                item.deterministic_metrics.future_leakage_count
                + (
                    item.agentic_metrics.future_leakage_count
                    if item.agentic_metrics is not None
                    else 0
                )
                for item in results
            ),
            safety_regression_count=sum(item.safety_regression is True for item in results),
            accuracy_gain_count=sum(item.accuracy_gain is True for item in results),
            tool_call_reduction_count=sum(item.tool_call_reduction is True for item in results),
            delta_gain_count=sum(
                1
                for item in results
                if item.delta_metrics is not None
                and item.delta_metrics.gold_evidence_recall
                > item.deterministic_metrics.gold_evidence_recall
            ),
            held_out_complex_gain_proven=False,
        )

    @staticmethod
    def _write_model(path: Path, model: Any) -> None:
        path.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_immutable(path: Path, payload: bytes) -> None:
        if path.exists():
            if path.read_bytes() != payload:
                raise TeacherForcedBenchmarkError(
                    f"refusing to overwrite different benchmark evidence: {path}"
                )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    @staticmethod
    def _run_without_checkpoints(
        scenario: Any,
        genesis_commit: CommitId,
        transition: _TeacherForcedTransition,
    ) -> Any:
        from novel_agent.domain.stage2 import SourceClass
        from novel_agent.services.scenario import ScenarioStateBuilder

        builder = ScenarioStateBuilder(scenario, genesis_commit)
        current_commit = genesis_commit
        chapter_sources = sorted(
            (
                source
                for source in scenario.sources
                if source.source_class is SourceClass.CHAPTER_TEXT
            ),
            key=lambda s: s.chapter_index or 0,
        )
        for source in chapter_sources:
            result = transition.apply(source, current_commit)
            if result.freshness.status.value not in ("ready", "degraded", "overridden"):
                raise TeacherForcedBenchmarkError(
                    f"chapter {source.chapter_index} has non-continuable freshness"
                )
            builder.record_chapter(
                source_id=source.source_id,
                resulting_commit=result.resulting_commit,
                curator_receipt=result.curator_receipt,
                validation_artifact=result.validation_artifact,
                projection_snapshot_id=result.projection_snapshot_id,
            )
            current_commit = result.resulting_commit
        return builder.result()

    @staticmethod
    def _write_progress(
        path: Path,
        genesis_commit: str,
        last_chapter: int,
        completed_chapters: list[int],
    ) -> None:
        payload: dict[str, object] = {
            "genesis_commit": genesis_commit,
            "last_accepted_commit": genesis_commit,
            "last_accepted_chapter": last_chapter,
            "completed_chapters": sorted(completed_chapters),
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(payload, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, path)

    @staticmethod
    def _reconcile_postcommit_progress(
        *,
        commits: CommitService,
        artifacts: ArtifactRepository,
        progress_path: Path,
        expected_head: CommitId,
        current_head: CommitId,
        expected_chapter: int,
    ) -> tuple[str, int, dict[str, Any]]:
        """Recover only the single-commit window after Canon accepted but before projection."""

        manifest = commits.load_manifest(current_head)
        if manifest.parent_commit_ids != (expected_head,):
            raise TeacherForcedBenchmarkError(
                "database Canon head is not the direct child of the progress commit"
            )
        text = TextRootDocument.model_validate_json(artifacts.read_verified(manifest.text_root))
        recovered_chapter = expected_chapter + 1
        last_chapter = max((item.chapter_index for item in text.chapters), default=0)
        if last_chapter != recovered_chapter:
            raise TeacherForcedBenchmarkError(
                "database Canon head does not represent exactly one additional chapter"
            )
        progress = json.loads(progress_path.read_text("utf-8"))
        completed = sorted(
            {
                *(int(item) for item in progress.get("completed_chapters", [])),
                recovered_chapter,
            }
        )
        payload: dict[str, object] = {
            key: value for key, value in progress.items() if key != "workflow_pause"
        }
        payload.update(
            {
                "last_accepted_commit": current_head.root,
                "last_accepted_chapter": recovered_chapter,
                "completed_chapters": completed,
            }
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=progress_path.parent,
            prefix=f".{progress_path.name}.",
            delete=False,
            encoding="utf-8",
        ) as temporary:
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary.name, progress_path)
        recovery = {
            "status": "postcommit_progress_reconciled",
            "previous_progress_commit": expected_head.root,
            "recovered_commit": current_head.root,
            "previous_chapter": expected_chapter,
            "recovered_chapter": recovered_chapter,
            "direct_parent_verified": True,
            "text_chapter_verified": True,
            "projection_rebuild_required": True,
        }
        return current_head.root, recovered_chapter, recovery


class _TeacherForcedTransition:
    def __init__(
        self,
        *,
        bundle: BenchmarkBundle,
        information_profile: BenchmarkInformationProfile,
        artifacts: ArtifactRepository,
        commits: CommitService,
        project_id: ProjectId,
        projections: DerivedProjectionService,
        snapshots: DerivedSnapshotRepository,
        harness: _AgentHarness,
        current_text: TextRootDocument,
        current_world: WorldRootDocument,
        current_plan: PlanRootDocument,
        profile_root_hash: ArtifactId,
        real_hybrid_backend_provider: RealHybridBackendProvider | None = None,
        recover_checkpoint_chapter: int | None = None,
        quality_repair_flags: QualityRepairFeatureFlags | None = None,
        memory_write_dry_run: bool = False,
    ) -> None:
        self.bundle = bundle
        self.profile = information_profile
        self.artifacts = artifacts
        self.commits = commits
        self._project_id = project_id
        self.projections = projections
        self.snapshots = snapshots
        self.harness = harness
        self.text = current_text
        self.world = current_world
        self.plan = current_plan
        self.profile_root_hash = profile_root_hash
        self.real_hybrid_backend_provider = real_hybrid_backend_provider
        self.recover_checkpoint_chapter = recover_checkpoint_chapter
        self.quality_repair_flags = quality_repair_flags or QualityRepairFeatureFlags()
        self.memory_write_dry_run = memory_write_dry_run
        self.progress_writer: _ProgressWriter | None = None
        self.timeline = SequentialTextRootService()
        self.case_by_chapter = {case.history_range[1]: case for case in bundle.case_manifests}
        latest = max(bundle.case_manifests, key=lambda item: item.history_range[1])
        reference = next(
            item for item in bundle.text_roots if item.root_hash == latest.input_text_root
        )
        self.documents: dict[int, PreludeDocument | ChapterDocument] = {
            chapter.chapter_index: chapter for chapter in reference.chapters
        }
        if reference.prelude is not None:
            self.documents[0] = reference.prelude
        self.states: dict[StableId, _FrozenState] = {}
        self.commit_count = 0
        self.curator_calls = 0
        self.planner_calls = 0
        self.guardian_calls = 0
        self.guardian_gate_decisions = 0
        self.validator_calls = 0
        self.memory_write_repair_calls = 0
        self.memory_write_candidate_revisions = 0
        self.memory_write_normalization_passes = 0
        self.memory_write_guardian_reviews = 0
        self.memory_write_context_refreshes = 0
        self.memory_write_transport_attempts = 0
        self.memory_write_tokens = 0
        self.memory_write_proposal_attempts = 0
        self.memory_write_proposal_rejections = 0
        self.memory_write_proposal_retry_counts: dict[str, int] = {}
        self.memory_write_proposal_poison_loops = 0
        self.memory_write_proposal_terminal_status: str | None = None
        self.memory_write_resume_checkpoint: dict[str, Any] | None = None
        self.memory_write_status_counts: dict[str, int] = {}
        self.last_revealed_chapter = 0
        self.latest_attestation: ProjectionAttestation | None = None
        self._revealed_text_for_adapter: TextRootDocument | None = None
        self._active_workflow_chapter: int | None = None
        self._boundary_port = InformationBoundaryPort(
            artifact_reader=artifacts,
            trusted_policy_hashes=(
                self._workflow_policy_ref().content_hash,
                self._workflow_configuration_fingerprint(),
            ),
        )
        self._boundary_registry = InformationBoundaryRegistryAdapter(self._boundary_port, artifacts)
        self._canonical_read = RepositoryCanonicalReadAdapter(commits, artifacts)
        enforce_support_gate = (
            harness.responses is None
            and self.quality_repair_flags.evidence_support_gate
            == EvidenceSupportGateMode.ENFORCE_PRE_CANDIDATE
        )
        model_curator = ModelCurator(
            harness.gateway,
            enforce_support_gate=enforce_support_gate,
            enable_model_semantic_verifier=enforce_support_gate,
        )
        graph_curator = ModelCurator(
            harness.gateway,
            enforce_support_gate=enforce_support_gate,
            enable_model_semantic_verifier=enforce_support_gate,
        )
        self._curator_port = TeacherForcedCuratorPort(
            CuratorReplayAgent(
                model_curator,
                harness.runner,
                evidence_contract=self.quality_repair_flags.curator_evidence_contract,
            ),
            CuratorRepairAgent(
                model_curator,
                harness.runner,
                evidence_contract=self.quality_repair_flags.curator_evidence_contract,
            ),
            artifacts,
            TeacherForcedBenchmarkE2ERunner._request,
            self._script_workflow_model,
            graph_curator=graph_curator,
        )
        self._guardian_port = LegacyGuardianPortAdapter(
            GuardianRiskReviewAgent(harness.runner, artifacts),
            artifacts,
            TeacherForcedBenchmarkE2ERunner._request,
            self._script_workflow_model,
            evidence_root=lambda: self._revealed_text_for_adapter,
        )
        self._workflow = LocalMemoryWriteWorkflow(
            canonical_read=self._canonical_read,
            curator=self._curator_port,
            validator=Stage2ValidationV2Adapter(
                proposed_text_loader=lambda ref: TextRootDocument.model_validate_json(
                    artifacts.read_verified(ref),
                    strict=True,
                )
            ),
            risk_classifier=LegacyRiskClassifierAdapter(artifacts),
            write_gate=LegacyWriteGateAdapter(artifacts),
            guardian=self._guardian_port,
            commit=(
                RefusingCommitPort(canonical_commit=commits.current_commit(self._project_id))
                if self.memory_write_dry_run
                else CommitServiceMemoryWriteAdapter(commits, artifacts)
            ),
            information_boundary=self._boundary_port,
            artifacts=artifacts,
            projection=ProjectionServiceReadinessAdapter(projections, snapshots, artifacts),
        )

    def _script_workflow_model(self, request: ModelRequest, mode: AgentMode) -> None:
        """Lazily register scripted responses only when a port actually calls a model."""
        if self.harness.responses is None:
            return
        if request.scheduling_stage == "curator_graph_extraction":
            self.harness.responses.add_stage(
                request.scheduling_stage,
                GraphCandidatePageDraft(
                    status=GraphCandidatePageStatus.COMPLETE,
                    no_graph_candidate_reason="scripted smoke has no graph candidate",
                ),
            )
            return
        if mode in {AgentMode.REPLAY, AgentMode.CURATOR_REPAIR}:
            candidates = EvidenceCandidateGenerator().generate(
                self.text,
                self._active_workflow_chapter or 0,
            )
            quote = next(
                (
                    candidate.text
                    for candidate in candidates
                    if len(EvidenceCandidateGenerator._semantic_span(candidate.text)) >= 8
                ),
                "scripted contract smoke",
            )
            subject = (
                self.world.entities[0].entity_id
                if self.world.entities
                else StableId("entity.scripted-smoke")
            )
            TeacherForcedBenchmarkE2ERunner._script(
                self.harness,
                request,
                CuratorV2EvidenceDraft(
                    chapter_index=self._active_workflow_chapter or 0,
                    operations=(
                        CuratorV2OperationDraft(
                            operation=ChangeOperationType.CREATE,
                            record_kind=WorldRecordKind.STATE,
                            target_id=StableId(
                                f"state.scripted-smoke.{self._active_workflow_chapter or 0}"
                            ),
                            record=CuratorStateRecord(
                                subject_id=subject,
                                predicate="scripted_smoke",
                                value="verified",
                                valid_time=CuratorStoryTime(
                                    worldline="main",
                                    start_ordinal=self._active_workflow_chapter or 0,
                                ),
                                truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                            ),
                            evidence_quotes=(quote,),
                        ),
                    ),
                ),
            )
        elif mode is AgentMode.RISK_REVIEW:
            TeacherForcedBenchmarkE2ERunner._script(
                self.harness,
                request,
                GuardianDecisionDraft(
                    outcome=GuardianOutcome.APPROVE,
                    risk_codes=(),
                    reasons=("scripted benchmark Guardian approved validated candidate",),
                ),
            )

    def apply(self, source: Any, parent_commit: CommitId) -> ScenarioChapterTransition:
        chapter = source.chapter_index
        if chapter is None or chapter not in self.documents:
            raise TeacherForcedBenchmarkError("scenario chapter source has no document")
        if chapter == getattr(self, "recover_checkpoint_chapter", None):
            # Historical checkpoint evaluation is deliberately read-only. The
            # project's branch head may have advanced beyond this immutable
            # commit, so current-Canon equality is required only for writes.
            return self._recover_checkpoint(source, parent_commit, chapter)
        if self.commits.current_commit(source_project(self.bundle)) != parent_commit:
            raise TeacherForcedBenchmarkError("transition parent is not current Canon")

        self.text, _ = self.timeline.append(self.text, source.source_id, self.documents[chapter])
        case = self.case_by_chapter.get(chapter)

        project_id = source_project(self.bundle)
        current = self.commits.load_manifest(parent_commit)
        text_ref = self._store_root(self.text, TextRootRef)

        position = NarrativePosition(chapter_index=chapter)
        boundary = InformationBoundary(
            boundary_id=StableId(f"boundary.teacher-forced.{chapter}"),
            base_commit=parent_commit,
            reveal_position=position,
            maximum_visible_position=position,
            evaluator_sources_forbidden=True,
            policy_ref=self._workflow_policy_ref(),
        )
        source_artifact = (
            self._prelude_curator_result(source, parent_commit).observed_changes.source_artifact
            if chapter == 0
            else self.artifacts.put(
                canonical_json_bytes(self.documents[chapter].model_dump(mode="json")),
                "application/json",
                VERSION,
            )
        )
        source_artifacts = [source_artifact]
        visibility_receipts = [
            self._boundary_registry.register_visibility(
                source=source_artifact,
                boundary=boundary,
                position=position,
                access_scope=AccessScope.WRITER_SAFE,
                provenance=SourceProvenance.REVEALED_TEXT,
            )
        ]
        text_visibility = visibility_receipts[0]
        text_producer = self._boundary_registry.register_derivation(
            output=text_ref,
            inputs=(source_artifact,),
            visibility_receipts=(text_visibility,),
            boundary=boundary,
            policy=boundary.policy_ref,
            position=position,
            access_scope=AccessScope.WRITER_SAFE,
        )
        intents = [
            RootUpdateIntent(
                intent_id=StableId(f"intent.teacher-forced.text.{chapter}"),
                root_kind=RootKind.TEXT,
                update_kind=RootUpdateKind.REPLACE,
                expected_base_root=current.text_root,
                update_artifact=text_ref,
                producer_receipt=text_producer,
                builder_policy_ref=boundary.policy_ref,
            )
        ]
        curator_spec = next(
            item
            for item in self.harness.specs
            if item.agent_type is AgentType.MEMORY_CURATOR and item.mode is AgentMode.REPLAY
        )
        world_mutation = (
            NoWorldMutationInput()
            if chapter == 0
            else CuratorWorldProposalInput(
                curator_agent_spec=ContractRef(
                    contract_id=curator_spec.agent_id,
                    version=curator_spec.version,
                    content_hash=curator_spec.content_hash,
                )
            )
        )
        request = MemoryWriteWorkflowRequest(
            request_id=StableId(f"memory-write.teacher-forced.chapter.{chapter}"),
            run_id=RunId("run.teacher-forced.e2e"),
            task_id=TaskId(f"task.teacher-forced.memory-write.{chapter}"),
            project_id=project_id,
            trigger=ChapterRevealTrigger(
                chapter_id=source.source_id,
                chapter_index=chapter,
                reveal_position=position,
            ),
            commit_profile=MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC,
            base_commit=parent_commit,
            source_artifacts=tuple(source_artifacts),
            root_update_intents=tuple(intents),
            world_mutation=world_mutation,
            canonical_root_refs=current,
            information_boundary=boundary,
            source_visibility_receipts=tuple(visibility_receipts),
            access_scope=AccessScope.WRITER_SAFE,
            source_provenance=(SourceProvenance.REVEALED_TEXT,),
            configuration_fingerprint=self._workflow_configuration_fingerprint(),
            budget=_quality_repair_memory_write_budget(),
            prompt_contract_refs=self.harness.prompt_refs,
            skill_contract_refs=self.harness.skill_refs,
            tool_policy_ref=self._tool_policy_ref(curator_spec),
            repair_policy_ref=boundary.policy_ref,
            idempotency_key=StableId(f"teacher-forced.chapter.{chapter}"),
        )
        self._active_workflow_chapter = chapter
        self._revealed_text_for_adapter = self.text
        self._curator_port.set_revealed_text(self.text)
        before_validations = len(getattr(self._workflow, "_validations", {}))
        before_guardian_calls = self._guardian_port.calls
        before_curator_calls = self._curator_port.proposal_calls
        before_repair_calls = self._curator_port.repair_calls
        result = asyncio.run(self._workflow.execute(request))
        self._revealed_text_for_adapter = None
        self.curator_calls += self._curator_port.proposal_calls - before_curator_calls
        self.validator_calls += (
            len(getattr(self._workflow, "_validations", {})) - before_validations
        )
        self.guardian_calls += self._guardian_port.calls - before_guardian_calls
        self.memory_write_repair_calls += self._curator_port.repair_calls - before_repair_calls
        self._record_memory_write_outcome(chapter, result)
        resulting_commit = self._require_committed_result(chapter, result)
        committed_basis = self._canonical_read.load_verified(project_id, resulting_commit)
        manifest = cast(Any, committed_basis.root_manifest)
        freshness = cast(Any, result.freshness)
        projection_snapshot_id = cast(StableId, result.projection_snapshot_id)
        self._require_complete_canonical_state(
            chapter,
            committed_basis,
            freshness,
            projection_snapshot_id,
            manifest,
        )
        self.text = cast(TextRootDocument, committed_basis.canonical_text)
        self.world = cast(WorldRootDocument, committed_basis.canonical_world)
        self.plan = cast(PlanRootDocument, committed_basis.canonical_plan)
        self._capture_latest_attestation(project_id, resulting_commit)
        self.commit_count += 1
        self.last_revealed_chapter = max(self.last_revealed_chapter, chapter)
        self._record_progress(resulting_commit, chapter)
        checkpoint = None
        if case is not None:
            self.states[case.case_id] = _FrozenState(
                text=self.text,
                world=self.world,
                plan=self.plan,
                commit=resulting_commit,
                text_root_ref=manifest.text_root,
            )
            checkpoint = ScenarioCheckpointArtifacts(
                text_root=manifest.text_root.artifact_id,
                plan_root=manifest.plan_root.artifact_id,
                world_root=manifest.world_root.artifact_id,
                derived_snapshot_id=projection_snapshot_id,
                anchor_alias=f"anchor-{resulting_commit.root[-16:]}",
                grounded_alias=f"grounded-{resulting_commit.root[-16:]}",
                project_profile=self.profile_root_hash,
            )
        curator_receipt = (
            self._prelude_curator_result(source, parent_commit).receipt
            if chapter == 0
            else self._curator_port.last_receipt
        )
        curator_receipt = self._require_curator_receipt(curator_receipt)
        return ScenarioChapterTransition(
            source_id=source.source_id,
            parent_commit=parent_commit,
            resulting_commit=resulting_commit,
            curator_receipt=curator_receipt,
            validation_artifact=result.validation_receipt
            if result.validation_receipt is not None
            else self._missing_validation_artifact(chapter),
            projection_snapshot_id=projection_snapshot_id,
            freshness=freshness,
            checkpoint_artifacts=checkpoint,
        )

    def _record_memory_write_outcome(
        self,
        chapter: int,
        result: MemoryWriteWorkflowResult,
    ) -> None:
        usage = result.budget_usage
        self.memory_write_candidate_revisions += usage.candidate_revisions
        self.memory_write_normalization_passes += usage.normalization_passes
        self.memory_write_guardian_reviews += usage.guardian_reviews
        self.memory_write_context_refreshes += usage.context_refreshes
        self.memory_write_transport_attempts += usage.transport_attempts
        self.memory_write_tokens += usage.tokens_used
        self.memory_write_proposal_attempts += usage.curator_proposal_attempts
        self.memory_write_proposal_rejections += usage.curator_proposal_rejections
        status_key = result.status.value
        if usage.curator_proposal_rejections:
            self.memory_write_proposal_retry_counts[status_key] = (
                self.memory_write_proposal_retry_counts.get(status_key, 0)
                + max(usage.curator_proposal_attempts - 1, 0)
            )
        if "CURATOR_PROPOSAL_POISON_LOOP" in result.terminal_codes:
            self.memory_write_proposal_poison_loops += 1
        if result.status is not MemoryWriteWorkflowStatus.COMMITTED:
            self.memory_write_proposal_terminal_status = result.status.value
            self.memory_write_resume_checkpoint = (
                None
                if result.checkpoint_ref is None
                else result.checkpoint_ref.model_dump(mode="json")
            )
        self.memory_write_status_counts[status_key] = (
            self.memory_write_status_counts.get(status_key, 0) + 1
        )
        self.guardian_gate_decisions += 1
        if (
            result.status
            in {
                MemoryWriteWorkflowStatus.SUSPENDED,
                MemoryWriteWorkflowStatus.HUMAN_REQUIRED,
                MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED,
                MemoryWriteWorkflowStatus.QUARANTINED,
                MemoryWriteWorkflowStatus.REPLAN_REQUIRED,
            }
            and self.progress_writer is not None
        ):
            self.progress_writer.record_pause(chapter, result)

    @staticmethod
    def _require_committed_result(chapter: int, result: MemoryWriteWorkflowResult) -> CommitId:
        resulting_commit = result.resulting_commit
        if result.status is not MemoryWriteWorkflowStatus.COMMITTED or resulting_commit is None:
            if result.status in {
                MemoryWriteWorkflowStatus.SUSPENDED,
                MemoryWriteWorkflowStatus.HUMAN_REQUIRED,
                MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED,
                MemoryWriteWorkflowStatus.QUARANTINED,
                MemoryWriteWorkflowStatus.REPLAN_REQUIRED,
            }:
                raise TeacherForcedControlledPause(chapter, result)
            raise TeacherForcedTerminalFailure(chapter, result)
        return resulting_commit

    @staticmethod
    def _require_complete_canonical_state(
        chapter: int,
        committed_basis: Any,
        freshness: Any,
        projection_snapshot_id: StableId | None,
        manifest: Any,
    ) -> None:
        if (
            committed_basis.canonical_text is None
            or committed_basis.canonical_world is None
            or committed_basis.canonical_plan is None
            or freshness is None
            or projection_snapshot_id is None
            or manifest is None
        ):
            raise TeacherForcedBenchmarkError(
                f"chapter {chapter} workflow result did not expose complete canonical state"
            )

    def _capture_latest_attestation(
        self,
        project_id: ProjectId,
        resulting_commit: CommitId,
    ) -> None:
        if self.real_hybrid_backend_provider is not None:
            backend_bundle = self.real_hybrid_backend_provider(project_id, resulting_commit)
            self.latest_attestation = backend_bundle.attestation

    def _record_progress(self, resulting_commit: CommitId, chapter: int) -> None:
        if self.progress_writer is not None:
            self.progress_writer.record(resulting_commit.root, chapter)

    @staticmethod
    def _require_curator_receipt(curator_receipt: Any) -> AgentExecutionReceipt:
        if curator_receipt is None:
            raise TeacherForcedBenchmarkError("memory-write workflow produced no Curator receipt")
        return cast(AgentExecutionReceipt, curator_receipt)

    def _missing_validation_artifact(self, chapter: int) -> ArtifactRef:
        raise TeacherForcedBenchmarkError(
            f"chapter {chapter} workflow result did not expose a validation artifact"
        )

    def _workflow_configuration_fingerprint(self) -> ArtifactId:
        return content_id(
            {
                "workflow": "stage2w-teacher-forced-v1",
                "profile": self.profile.value,
                "specs": [spec.content_hash.root for spec in self.harness.specs],
                "prompts": [ref.content_hash.root for ref in self.harness.prompt_refs],
                "skills": [ref.content_hash.root for ref in self.harness.skill_refs],
                "quality_repair_flags": self.quality_repair_flags.model_dump(mode="json"),
            }
        )

    def _workflow_policy_ref(self) -> ContractRef:
        return ContractRef(
            contract_id=StableId("policy.memory-write.teacher-forced"),
            version=VERSION,
            content_hash=self._workflow_configuration_fingerprint(),
        )

    @staticmethod
    def _tool_policy_ref(spec: AgentSpec) -> ContractRef:
        return ContractRef(
            contract_id=spec.tool_policy.policy_id,
            version=spec.tool_policy.version,
            content_hash=spec.tool_policy.content_hash,
        )

    def _recover_checkpoint(
        self,
        source: Any,
        parent_commit: CommitId,
        chapter: int,
    ) -> ScenarioChapterTransition:
        """Resume Freeze/Evaluate after the checkpoint commit already succeeded."""
        case = self.case_by_chapter.get(chapter)
        if case is None:
            raise TeacherForcedBenchmarkError("checkpoint recovery requires a declared case")
        manifest = self.commits.load_manifest(parent_commit)
        snapshot = self.snapshots.get_for_commit(parent_commit)
        required_snapshot = snapshot_id_for_commit(parent_commit)
        freshness = FreshnessGate.evaluate(
            FreshnessRequest(
                canonical_commit=parent_commit,
                r1_basis_commit=parent_commit,
                required_snapshot_id=required_snapshot,
                actual_alias_commit=None if snapshot is None else snapshot.source_commit,
                actual_snapshot=snapshot,
                mode=FreshnessMode.BLOCK_ON_MISMATCH,
            )
        )
        if freshness.status is not FreshnessStatus.READY:
            raise TeacherForcedBenchmarkError("recovered checkpoint projection is not fresh")
        validation_ref = self.artifacts.put(
            canonical_json_bytes(
                {"checkpoint": chapter, "canonical_commit": parent_commit.root, "recovered": True}
            ),
            "application/vnd.novel-agent.checkpoint-recovery+json",
            VERSION,
        )
        spec = next(
            item
            for item in self.harness.specs
            if item.agent_type is AgentType.MEMORY_CURATOR and item.mode is AgentMode.REPLAY
        )
        now = datetime.now(UTC)
        receipt = AgentExecutionReceipt(
            receipt_id=StableId(f"agent-receipt.checkpoint-recovery.{chapter}"),
            run_id=RunId("run.teacher-forced.e2e"),
            task_id=TaskId(f"task.checkpoint-recovery.{chapter}"),
            agent_spec=ContractRef(
                contract_id=spec.agent_id,
                version=spec.version,
                content_hash=spec.content_hash,
            ),
            agent_type=AgentType.MEMORY_CURATOR,
            agent_mode=AgentMode.REPLAY,
            prompt_fingerprint=content_id(
                {"checkpoint_recovery": chapter, "commit": parent_commit.root}
            ),
            configuration_fingerprint=content_id({"spec": spec.content_hash.root}),
            base_commit=parent_commit,
            input_artifacts=(manifest.text_root, manifest.plan_root, manifest.world_root),
            output_artifacts=(validation_ref,),
            unresolved=("chapter commit already accepted; resumed checkpoint freeze only",),
            status=ExecutionStatus.SUCCEEDED,
            started_at=now,
            completed_at=now,
            latency_ms=0,
        )
        self.states[case.case_id] = _FrozenState(
            text=self.text,
            world=self.world,
            plan=self.plan,
            commit=parent_commit,
            text_root_ref=manifest.text_root,
        )
        self.last_revealed_chapter = chapter
        checkpoint = ScenarioCheckpointArtifacts(
            text_root=manifest.text_root.artifact_id,
            plan_root=manifest.plan_root.artifact_id,
            world_root=manifest.world_root.artifact_id,
            derived_snapshot_id=required_snapshot,
            anchor_alias=f"anchor-{parent_commit.root[-16:]}",
            grounded_alias=f"grounded-{parent_commit.root[-16:]}",
            project_profile=self.profile_root_hash,
        )
        return ScenarioChapterTransition(
            source_id=source.source_id,
            parent_commit=parent_commit,
            resulting_commit=parent_commit,
            curator_receipt=receipt,
            validation_artifact=validation_ref,
            projection_snapshot_id=required_snapshot,
            freshness=freshness,
            checkpoint_artifacts=checkpoint,
        )

    def _run_curator(self, chapter: int, base: CommitId) -> CuratorReplayResult:
        request = TeacherForcedBenchmarkE2ERunner._request(
            f"curator.replay.{chapter}", AgentMode.REPLAY
        )
        candidates = EvidenceCandidateGenerator().generate(self.text, chapter)
        quote = next(
            (
                candidate.text
                for candidate in candidates
                if len(EvidenceCandidateGenerator._semantic_span(candidate.text)) >= 8
            ),
            "scripted contract smoke",
        )
        subject = (
            self.world.entities[0].entity_id
            if self.world.entities
            else StableId("entity.scripted-smoke")
        )
        TeacherForcedBenchmarkE2ERunner._script(
            self.harness,
            request,
            CuratorV2EvidenceDraft(
                chapter_index=chapter,
                operations=(
                    CuratorV2OperationDraft(
                        operation=ChangeOperationType.CREATE,
                        record_kind=WorldRecordKind.STATE,
                        target_id=StableId(f"state.scripted-smoke.{chapter}"),
                        record=CuratorStateRecord(
                            subject_id=subject,
                            predicate="scripted_smoke",
                            value="verified",
                            valid_time=CuratorStoryTime(
                                worldline="main",
                                start_ordinal=chapter,
                            ),
                            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                        ),
                        evidence_quotes=(quote,),
                    ),
                ),
            ),
        )
        result, _ = asyncio.run(
            CuratorReplayAgent(
                ModelCurator(self.harness.gateway, enforce_support_gate=False),
                self.harness.runner,
            ).run(
                version=VERSION,
                text_root=self.text,
                chapter_index=chapter,
                base_commit=base,
                current_world=self.world,
                request=request,
            )
        )
        self.curator_calls += 1
        return result

    def _prelude_curator_result(self, source: Any, base: CommitId) -> CuratorReplayResult:
        source_ref = self.artifacts.put(
            canonical_json_bytes(self.documents[0].model_dump(mode="json")),
            "application/json",
            VERSION,
        )
        changes = ObservedChangeSet(
            change_set_id=StableId("changes.teacher-forced.prelude"),
            base_commit=base,
            source_artifact=source_ref,
        )
        spec = next(
            item
            for item in self.harness.specs
            if item.agent_type is AgentType.MEMORY_CURATOR and item.mode is AgentMode.REPLAY
        )
        now = datetime.now(UTC)
        unresolved = ("prelude is committed as text-only teacher-forced material",)
        receipt = AgentExecutionReceipt(
            receipt_id=StableId("agent-receipt.teacher-forced.prelude"),
            run_id=RunId("run.teacher-forced.e2e"),
            task_id=TaskId("task.teacher-forced.prelude"),
            agent_spec=ContractRef(
                contract_id=spec.agent_id,
                version=spec.version,
                content_hash=spec.content_hash,
            ),
            agent_type=AgentType.MEMORY_CURATOR,
            agent_mode=AgentMode.REPLAY,
            prompt_fingerprint=content_id({"prelude": source.source_id.root}),
            configuration_fingerprint=content_id({"spec": spec.content_hash.root}),
            base_commit=base,
            output_artifacts=(source_ref,),
            unresolved=unresolved,
            status=ExecutionStatus.SUCCEEDED,
            started_at=now,
            completed_at=now,
            latency_ms=0,
        )
        return CuratorReplayResult(
            observed_changes=changes,
            coverage=0,
            unresolved=unresolved,
            receipt=receipt,
        )

    def _store_root(self, root: Any, ref_type: Any) -> Any:
        artifact = self.artifacts.put(
            canonical_json_bytes(root.model_dump(mode="json")),
            "application/json",
            root.schema_version,
        )
        return ref_type.model_validate(artifact.model_dump())


class _E2EContextFreezer:
    def __init__(
        self,
        config: Any,
        transition: _TeacherForcedTransition,
        artifacts: ArtifactRepository,
        paired: Stage2PairedPilotRunner,
        *,
        controller_policy_factory: Any | None = None,
        real_hybrid_backend_provider: RealHybridBackendProvider | None = None,
        support_gateway: ModelGateway | None = None,
        support_progress_writer: Callable[[Mapping[str, object]], None] | None = None,
        support_pre_proposal_trace: bool = False,
        support_max_concurrent_needs: int = 1,
        support_max_inflight_kv_tokens: int | None = None,
        support_admission_controller: ModelRequestAdmissionController | None = None,
        frozen_planner_artifact: PlannerInvocationArtifact | None = None,
        support_transport_config: ClaimSupportTransportConfig | None = None,
    ) -> None:
        self.config = config
        self.transition = transition
        self.artifacts = artifacts
        self.paired = paired
        self.controller_policy_factory = controller_policy_factory
        self.real_hybrid_backend_provider = real_hybrid_backend_provider
        self.support_gateway = support_gateway
        self.support_progress_writer = support_progress_writer
        self.support_pre_proposal_trace = support_pre_proposal_trace
        self.support_max_concurrent_needs = support_max_concurrent_needs
        self.support_max_inflight_kv_tokens = support_max_inflight_kv_tokens
        self.support_admission_controller = support_admission_controller
        self.frozen_planner_artifact = frozen_planner_artifact
        self.support_transport_config = support_transport_config
        self.comparisons: dict[StableId, PairedContextComparison] = {}
        self.comparison_refs: dict[StableId, ArtifactRef] = {}
        self._latest_attestation: ProjectionAttestation | None = None

    @property
    def latest_attestation(self) -> ProjectionAttestation | None:
        return self._latest_attestation

    def freeze(self, basis: Any) -> ArtifactRef:
        case_id = basis.case_id
        case = next(
            item for item in self.transition.bundle.case_manifests if item.case_id == case_id
        )
        state = self.transition.states[case.case_id]
        author_plan = (
            next(
                root
                for root in self.transition.bundle.plan_roots
                if root.root_hash == case.input_plan_root
            )
            if self.transition.profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
            else state.plan
        )
        plan_bytes = author_plan.model_dump_json().encode("utf-8")
        plan_ref = (
            PlanRootRef(
                artifact_id=author_plan.root_hash,
                media_type="application/vnd.novel-agent.plan-root+json",
                byte_length=len(plan_bytes),
                schema_version=author_plan.schema_version,
            )
            if self.transition.profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
            else None
        )
        public = build_public_checkpoint_case(
            case_id=case.case_id,
            project_id=case.project_id,
            target_range=case.target_range,
            history_range=case.history_range,
            information_profile=self.transition.profile,
            plan_root_ref=plan_ref,
            task_intent=(
                case.task_intent
                if self.transition.profile
                in {
                    BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
                    BenchmarkInformationProfile.TASK_INTENT_ONLY,
                }
                else ""
            ),
            planning_context_ref=(
                case.planning_context_ref
                if self.transition.profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
                else None
            ),
            planning_context_hash=(
                case.planning_context_hash
                if self.transition.profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
                else None
            ),
        )
        retrieval_backend = None
        snapshot_capability = None
        reranker = None
        if self.paired.retrieval_backend_profile is RetrievalBackendProfile.REAL_HYBRID:
            if self.real_hybrid_backend_provider is None:
                raise TeacherForcedBenchmarkError(
                    "real_hybrid freeze requires a commit-scoped retrieval backend provider"
                )
            backend_bundle = self.real_hybrid_backend_provider(case.project_id, state.commit)
            attestation = backend_bundle.attestation
            if (
                attestation.source_commit != state.commit
                or not attestation.quality_eligible
                or attestation.capability.status is not SnapshotCapabilityStatus.EXACT
            ):
                raise TeacherForcedBenchmarkError(
                    "real_hybrid freeze received an incomplete or stale projection attestation"
                )
            retrieval_backend = backend_bundle.backend
            snapshot_capability = attestation.capability
            reranker = backend_bundle.reranker
            self._latest_attestation = attestation
        comparison = self.paired.resolve_state_case(
            self.config,
            public,
            self.transition.profile,
            history=state.text,
            world=state.world,
            plan=author_plan,
            base_commit=state.commit,
            controller_policy_factory=self.controller_policy_factory,
            retrieval_backend=retrieval_backend,
            snapshot_capability=snapshot_capability,
            reranker=reranker,
            support_gateway=self.support_gateway,
            support_max_concurrent_needs=self.support_max_concurrent_needs,
            support_max_inflight_kv_tokens=self.support_max_inflight_kv_tokens,
            support_admission_controller=self.support_admission_controller,
            support_transport_config=self.support_transport_config,
            planning_context=next(
                (
                    context
                    for context in self.transition.bundle.planning_contexts
                    if context.source_hash == case.planning_context_hash
                ),
                None,
            ),
            support_artifact_writer=lambda payload, media_type: self.artifacts.put(
                payload,
                media_type,
                VERSION,
            ),
            support_progress_writer=self.support_progress_writer,
            support_pre_proposal_trace=self.support_pre_proposal_trace,
            frozen_planner_artifact=self.frozen_planner_artifact,
        )
        if self.support_progress_writer is not None:
            self.support_progress_writer(
                {
                    "stage": "freeze",
                    "status": "completed",
                    "case_id": case.case_id.root,
                    "assembly_status": (
                        comparison.deterministic.assembly_status.value
                        if comparison.deterministic.assembly_status is not None
                        else "not_assembled"
                    ),
                }
            )
        comparison_ref = self.artifacts.put(
            canonical_json_bytes(comparison.model_dump(mode="json")),
            "application/vnd.novel-agent.frozen-paired-context+json",
            VERSION,
        )
        self.comparisons[case.case_id] = comparison
        self.comparison_refs[case.case_id] = comparison_ref
        return comparison_ref


class _E2EEvaluator:
    def __init__(
        self,
        bundle: BenchmarkBundle,
        profile: BenchmarkInformationProfile,
        freezer: _E2EContextFreezer,
        artifacts: ArtifactRepository,
        paired: Stage2PairedPilotRunner,
        *,
        semantic_gateway: ModelGateway | None = None,
        model_semantic_verifier_enabled: bool = False,
        max_concurrent_batches: int = 4,
    ) -> None:
        self.bundle = bundle
        self.profile = profile
        self.freezer = freezer
        self.artifacts = artifacts
        self.paired = paired
        self.semantic_gateway = semantic_gateway
        self.model_semantic_verifier_enabled = model_semantic_verifier_enabled
        self.max_concurrent_batches = max_concurrent_batches
        self._results: list[PairedPilotCaseResult] = []
        self._stage2m_results: list[MemoryBenchmarkCaseArmReport] = []
        self._results_lock = threading.Lock()

    @property
    def results(self) -> tuple[PairedPilotCaseResult, ...]:
        with self._results_lock:
            return tuple(self._results)

    @property
    def stage2m_results(self) -> tuple[MemoryBenchmarkCaseArmReport, ...]:
        with self._results_lock:
            return tuple(self._stage2m_results)

    def _build_ancestry_proof(
        self,
        case: Any,
        comparison: Any,
    ) -> tuple[
        ObservedTextAncestryProof,
        dict[ArtifactId, TextRootDocument],
    ]:
        """Build and persist the observed TextRoot ancestry proof.

        Walks the single-parent canonical commit chain from the checkpoint
        commit back to genesis.  Every manifest TextRoot is read through the
        verified artifact repository and its logical ``TextRootDocument
        .root_hash`` is collected, retaining the concrete documents for
        cross-root EvidenceRef validation.  The case-local compiled historical
        root is bound separately by the bundle content hash and is never
        treated as a commit ancestor.  Any chain/read/parse failure raises a
        typed error; the caller must not silently continue without a proof.
        """

        state = self.freezer.transition.states.get(case.case_id)
        if state is None:
            raise TeacherForcedBenchmarkError(
                f"checkpoint state missing for {case.case_id.root}; cannot build ancestry proof"
            )
        checkpoint_commit = state.commit
        commits = self.freezer.transition.commits
        if commits is None:
            raise TeacherForcedBenchmarkError(
                "commit service unavailable; cannot build ancestry proof"
            )
        entries: list[TextRootAncestryEntry] = []
        text_roots: dict[ArtifactId, TextRootDocument] = {}
        seen: set[str] = set()
        cursor: CommitId | None = checkpoint_commit
        while cursor is not None:
            if cursor.root in seen:
                raise TeacherForcedBenchmarkError(
                    "commit ancestry contains a cycle; cannot build ancestry proof"
                )
            seen.add(cursor.root)
            try:
                manifest = commits.load_manifest(cursor)
            except Exception as error:
                raise TeacherForcedBenchmarkError(
                    "commit ancestry manifest read failed: " + str(error)
                ) from error
            if len(manifest.parent_commit_ids) > 1:
                raise TeacherForcedBenchmarkError(
                    "commit ancestry is not single-parent; cannot build ancestry proof"
                )
            text_root_ref = manifest.text_root
            try:
                text_bytes = self.artifacts.read_verified(text_root_ref)
                text_document = TextRootDocument.model_validate_json(text_bytes.decode("utf-8"))
            except Exception as error:
                raise TeacherForcedBenchmarkError(
                    "cannot resolve manifest TextRoot into a logical root: " + str(error)
                ) from error
            entries.append(
                TextRootAncestryEntry(
                    commit_id=cursor,
                    text_root_ref=text_root_ref,
                    text_root_logical_hash=text_document.root_hash,
                )
            )
            text_roots[text_document.root_hash] = text_document
            parent = manifest.parent_commit_ids[0] if manifest.parent_commit_ids else None
            cursor = parent
        checkpoint_root_ref = state.text_root_ref if hasattr(state, "text_root_ref") else None
        checkpoint_root_hash = state.text.root_hash
        if checkpoint_root_ref is None:
            raise TeacherForcedBenchmarkError(
                "checkpoint state lacks its TextRoot artifact ref; cannot build ancestry proof"
            )
        compiled_case = next(
            (item for item in self.bundle.case_manifests if item.case_id == case.case_id),
            None,
        )
        if compiled_case is None:
            raise TeacherForcedBenchmarkError("case manifest missing; cannot build ancestry proof")
        compiled_text_root = next(
            (
                root
                for root in self.bundle.text_roots
                if root.root_hash == compiled_case.input_text_root
            ),
            None,
        )
        if compiled_text_root is None:
            raise TeacherForcedBenchmarkError(
                "case compiled historical TextRoot missing; cannot build ancestry proof"
            )
        text_roots[compiled_text_root.root_hash] = compiled_text_root
        proof = ObservedTextAncestryProof.build(
            benchmark_content_hash=self.bundle.content_hash,
            case_id=case.case_id,
            profile=self.profile.value,
            checkpoint_chapter=case.history_range[1],
            checkpoint_commit=checkpoint_commit,
            checkpoint_text_root_ref=checkpoint_root_ref,
            checkpoint_text_root_hash=checkpoint_root_hash,
            ancestry=tuple(entries),
            case_input_text_root_hash=compiled_text_root.root_hash,
        )
        return proof, text_roots

    @staticmethod
    def _runtime_entity_label_map(
        runtime_world: WorldRootDocument,
    ) -> tuple[dict[str, StableId], set[str]]:
        """Build label/alias -> runtime entity id from the frozen runtime World.

        A label or alias mapping to multiple runtime ids is recorded as
        ambiguous and never binds; empty labels are skipped.
        """

        label_to_ids: dict[str, list[StableId]] = {}
        for entity in runtime_world.entities:
            for label in dict.fromkeys((entity.internal_label, *entity.aliases)):
                if not label:
                    continue
                label_to_ids.setdefault(label, []).append(entity.entity_id)
        entity_id_by_label: dict[str, StableId] = {}
        ambiguous_labels: set[str] = set()
        for label, ids in label_to_ids.items():
            if len(ids) == 1:
                entity_id_by_label[label] = ids[0]
            else:
                ambiguous_labels.add(label)
        return entity_id_by_label, ambiguous_labels

    def _compute_five_segments(
        self,
        benchmark_evaluator: MemoryBenchmarkEvaluator,
        case: Any,
        comparison: Any,
        gold_items: tuple[GoldItem, ...],
        evidence_ledger: EvidenceLedger,
        per_gold: MemoryBenchmarkEvaluationReport,
        metric_descriptors: Mapping[StableId, ContentAddressedGoldMetricDescriptor],
    ) -> FiveSegmentReport:
        """Compute five-segment evaluation (incl. Plan Goal Coverage) for one arm."""
        plan = next(
            (root for root in self.bundle.plan_roots if root.root_hash == case.input_plan_root),
            None,
        )
        plan_goals = tuple(
            goal
            for goal in (plan.chapter_goals if plan is not None else ())
            if case.target_range[0] <= goal.chapter_index <= case.target_range[1]
        )
        runtime_world = self.freezer.transition.states[case.case_id].world
        entity_id_by_label, ambiguous_labels = self._runtime_entity_label_map(runtime_world)
        return benchmark_evaluator.evaluate_five_segments(
            needs=comparison.generated_needs,
            gold_need_specs=case.gold_need_specs,
            plan_goals=plan_goals,
            gold_items=gold_items,
            evidence_ledger=evidence_ledger,
            completion_accuracy=per_gold.weighted_coverage,
            per_gold_comparisons=per_gold.comparisons,
            future_leakage_count=comparison.deterministic.future_leakage_count,
            entity_id_by_label=entity_id_by_label or None,
            ambiguous_entity_labels=ambiguous_labels,
            planner_fallback_used=comparison.planner_fallback_used,
            planner_fallback_reason=comparison.planner_fallback_reason,
            planner_artifact_ref=comparison.planner_artifact_ref,
            grounded_status_counts=comparison.grounded_status_counts,
            profile=self.profile,
        )

    def score(self, freeze: Any, evaluator_sources: tuple[Any, ...]) -> EvaluatorDisposition:
        if {item.source_class for item in evaluator_sources} != {
            SourceClass.FUTURE_TEXT_PRIVATE,
            SourceClass.READ_GOLD,
        }:
            raise TeacherForcedBenchmarkError("checkpoint evaluator sources are incomplete")
        case = next(item for item in self.bundle.case_manifests if item.case_id == freeze.case_id)
        result = self.paired.score_comparison(
            case,
            self.profile,
            self.freezer.comparisons[case.case_id],
        )
        with self._results_lock:
            self._results.append(result)
        artifact = self.artifacts.put(
            canonical_json_bytes(result.model_dump(mode="json")),
            "application/vnd.novel-agent.e2e-case-score+json",
            VERSION,
        )
        score_artifacts = [artifact]
        comparison = self.freezer.comparisons[case.case_id]
        if comparison.freeze_receipt is None:
            raise TeacherForcedBenchmarkError(
                "Stage 2M evaluator requires a persisted freeze receipt"
            )
        gold_items = (
            *case.observed_use_gold,
            *case.operational_constraint_gold,
            *case.plan_obligation_gold,
        )
        evaluator_manifest_id = StableId(
            f"evaluator-manifest.{case.case_id.root}.{self.profile.value}"
        )
        applicable_gold = tuple(
            item for item in gold_items if self.profile in item.applicable_profiles
        )
        if self.profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED:
            ancestry_proof, text_roots = self._build_ancestry_proof(case, comparison)
            proof_ref = self.artifacts.put(
                canonical_json_bytes(ancestry_proof.model_dump(mode="json")),
                "application/vnd.novel-agent.observed-text-ancestry-proof+json",
                VERSION,
            )
        else:
            ancestry_proof = None
            text_roots = {}
            proof_ref = None
        evidence_matcher = GoldEvidenceMatcher(
            ancestry_proof=ancestry_proof,
            text_roots=text_roots,
        )
        metric_builder = GoldMetricContractBuilder(
            self.artifacts,
            matcher=evidence_matcher,
            ancestry_proof_ref=proof_ref,
        )
        _evaluator_manifest, evaluator_manifest_ref = metric_builder.build_manifest(
            gold_items=applicable_gold,
            evaluator_manifest_id=evaluator_manifest_id,
        )
        evaluator_manifest_hash = evaluator_manifest_ref.artifact_id
        metric_descriptors = metric_builder.build(
            gold_items=applicable_gold,
            evaluator_manifest_id=evaluator_manifest_id,
            evaluator_manifest_hash=evaluator_manifest_hash,
        )
        benchmark_evaluator = MemoryBenchmarkEvaluator(evidence_matcher=evidence_matcher)
        arm_inputs = (
            (
                comparison.deterministic.context,
                comparison.deterministic.writer_context,
                comparison.deterministic.evidence_ledger,
            ),
            (
                comparison.agentic.context,
                comparison.agentic.writer_context,
                comparison.agentic.evidence_ledger,
            ),
            (
                comparison.agentic.context,
                comparison.arm_c_writer_context,
                comparison.arm_c_evidence_ledger,
            ),
        )
        diagnostic_builder = StageLossDiagnosticBuilder(matcher=evidence_matcher)
        score_artifacts.append(evaluator_manifest_ref)
        for binding in metric_descriptors.values():
            score_artifacts.extend(
                (
                    binding.descriptor.accepted_evidence_contract_ref,
                    binding.descriptor.gold_contract_ref,
                    binding.descriptor_ref,
                )
            )
        for stage1_context, writer_context, evidence_ledger in arm_inputs:
            stage_loss_diagnostics = (
                ()
                if evidence_ledger is None
                else diagnostic_builder.build(
                    gold_items=gold_items,
                    profile=self.profile,
                    stage1_context=stage1_context,
                    writer_ledger=evidence_ledger,
                )
            )
            if (
                writer_context is not None
                and evidence_ledger is not None
                and writer_context.arm == "A"
                and writer_context.budget_report.final_status.value != "READY"
            ):
                per_gold = benchmark_evaluator.evaluate_typed_failure(
                    gold_items=gold_items,
                    profile=self.profile,
                    assembly_status=writer_context.budget_report.final_status,
                    freeze_receipt=comparison.freeze_receipt,
                    evaluator_manifest_id=evaluator_manifest_id,
                    evaluator_manifest_ref=evaluator_manifest_ref,
                    evaluator_manifest_hash=evaluator_manifest_hash,
                    gold_metric_descriptors=metric_descriptors,
                    stage_loss_diagnostics=stage_loss_diagnostics,
                )
                five_segments = self._compute_five_segments(
                    benchmark_evaluator,
                    case,
                    comparison,
                    gold_items,
                    evidence_ledger,
                    per_gold,
                    metric_descriptors,
                )
                per_gold = per_gold.model_copy(update={"five_segments": five_segments})
                ledger_ref = self.artifacts.put(
                    canonical_json_bytes(evidence_ledger.model_dump(mode="json")),
                    "application/vnd.novel-agent.evaluator-writer-evidence-ledger+json",
                    VERSION,
                )
                with self._results_lock:
                    self._stage2m_results.append(
                        MemoryBenchmarkCaseArmReport(
                            case_id=case.case_id,
                            checkpoint_chapter=case.history_range[1],
                            arm="A",
                            code_version=comparison.freeze_receipt.code_version,
                            run_config_hash=comparison.freeze_receipt.run_config_hash,
                            benchmark_contract_hash=self.bundle.content_hash,
                            matcher_version=GoldEvidenceMatcher.version,
                            writer_token_budget=(
                                writer_context.budget_report.configured_writer_token_budget
                            ),
                            evidence_ledger_token_budget=12_000,
                            assembly_status=writer_context.budget_report.final_status,
                            writer_tokens=(
                                writer_context.budget_report.actual_rendered_writer_tokens
                            ),
                            evidence_tokens=writer_context.budget_report.evidence_ledger_tokens,
                            selected_unit_count=writer_context.lineage.normalized_unit_count,
                            comparable=False,
                            writer_evidence_ledger_ref=ledger_ref,
                            evaluation=per_gold,
                        )
                    )
                score_artifacts.append(
                    self.artifacts.put(
                        canonical_json_bytes(per_gold.model_dump(mode="json")),
                        "application/vnd.novel-agent.stage2m-per-gold-score+json",
                        VERSION,
                    )
                )
                continue
            if (
                writer_context is None
                or evidence_ledger is None
                or writer_context.budget_report.final_status.value != "READY"
                or (writer_context.arm == "C" and "C_FALLBACK_TO_A" in comparison.blockers)
            ):
                continue
            semantic_judgments = None
            verifier_receipt_ref = None
            if self.model_semantic_verifier_enabled:
                if self.semantic_gateway is None:
                    raise TeacherForcedBenchmarkError(
                        "model semantic verifier requires a configured model gateway"
                    )
                benchmark_evaluator._verify_frozen_artifacts(
                    writer_context,
                    evidence_ledger,
                    comparison.freeze_receipt,
                )
                verifier = ModelSemanticSupportVerifier(
                    self.semantic_gateway,
                    evidence_matcher=evidence_matcher,
                    max_concurrent_batches=self.max_concurrent_batches,
                )
                batch, calls = asyncio.run(
                    verifier.verify(
                        gold_items=applicable_gold,
                        package=writer_context,
                        evidence_ledger=evidence_ledger,
                        request=ModelRequest(
                            request_id=StableId(
                                f"request.stage2m.semantic.{case.case_id.root}.{writer_context.arm}"
                            ),
                            run_id=RunId("run.teacher-forced.e2e"),
                            task_id=TaskId(
                                f"task.stage2m.semantic.{case.case_id.root}.{writer_context.arm}"
                            ),
                            model_role=ModelRole.BATCH_TEST,
                            purpose=ModelCallPurpose.EVALUATION,
                            trace_id=(
                                f"trace-stage2m-semantic-{case.case_id.root}-{writer_context.arm}"
                            ),
                            prompt="replaced by Stage 2M semantic verifier",
                            timeout_seconds=300,
                            enable_thinking=False,
                            scheduling_stage="evaluation",
                        ),
                    )
                )
                verifier_receipt_ref = self.artifacts.put(
                    canonical_json_bytes(
                        {
                            "verifier_version": verifier.version,
                            "writer_context_hash": content_id(
                                writer_context.model_dump(mode="json")
                            ).root,
                            "batch": batch.model_dump(mode="json"),
                            "model_calls": [call.model_dump(mode="json") for call in calls],
                            "evaluator_manifest_id": evaluator_manifest_id.root,
                            "evaluator_manifest_ref": evaluator_manifest_ref.artifact_id.root,
                            "ancestry_proof_ref": (
                                proof_ref.artifact_id.root if proof_ref is not None else None
                            ),
                            "ancestry_proof_hash": (
                                ancestry_proof.proof_hash.root
                                if ancestry_proof is not None
                                else None
                            ),
                            "matcher_version": GoldEvidenceMatcher.version,
                        }
                    ),
                    "application/vnd.novel-agent.stage2m-semantic-verifier-receipt+json",
                    VERSION,
                )
                semantic_judgments = {item.gold_id: item for item in batch.judgments}
                score_artifacts.append(verifier_receipt_ref)
            per_gold = benchmark_evaluator.evaluate(
                package=writer_context,
                evidence_ledger=evidence_ledger,
                gold_items=gold_items,
                profile=self.profile,
                freeze_receipt=comparison.freeze_receipt,
                evaluator_manifest_id=evaluator_manifest_id,
                evaluator_manifest_ref=evaluator_manifest_ref,
                evaluator_manifest_hash=evaluator_manifest_hash,
                gold_metric_descriptors=metric_descriptors,
                semantic_judgments=semantic_judgments,
                verifier_receipt_ref=verifier_receipt_ref,
                stage_loss_diagnostics=stage_loss_diagnostics,
            )
            five_segments = self._compute_five_segments(
                benchmark_evaluator,
                case,
                comparison,
                gold_items,
                evidence_ledger,
                per_gold,
                metric_descriptors,
            )
            per_gold = per_gold.model_copy(update={"five_segments": five_segments})
            ledger_ref = self.artifacts.put(
                canonical_json_bytes(evidence_ledger.model_dump(mode="json")),
                "application/vnd.novel-agent.evaluator-writer-evidence-ledger+json",
                VERSION,
            )
            with self._results_lock:
                self._stage2m_results.append(
                    MemoryBenchmarkCaseArmReport(
                        case_id=case.case_id,
                        checkpoint_chapter=case.history_range[1],
                        arm=writer_context.arm,
                        code_version=comparison.freeze_receipt.code_version,
                        run_config_hash=comparison.freeze_receipt.run_config_hash,
                        benchmark_contract_hash=self.bundle.content_hash,
                        matcher_version=GoldEvidenceMatcher.version,
                        writer_token_budget=(
                            writer_context.budget_report.configured_writer_token_budget
                        ),
                        evidence_ledger_token_budget=12_000,
                        assembly_status=writer_context.budget_report.final_status,
                        writer_tokens=(writer_context.budget_report.actual_rendered_writer_tokens),
                        evidence_tokens=writer_context.budget_report.evidence_ledger_tokens,
                        selected_unit_count=writer_context.lineage.normalized_unit_count,
                        comparable=comparison.comparable,
                        writer_evidence_ledger_ref=ledger_ref,
                        evaluation=per_gold,
                    )
                )
            score_artifacts.append(
                self.artifacts.put(
                    canonical_json_bytes(per_gold.model_dump(mode="json")),
                    "application/vnd.novel-agent.stage2m-per-gold-score+json",
                    VERSION,
                )
            )
        return EvaluatorDisposition(
            evaluator_context_destroyed=True,
            teacher_forced_resume_allowed=True,
            score_artifacts=tuple(score_artifacts),
        )


def source_project(bundle: BenchmarkBundle) -> ProjectId:
    projects = {item.project_id for item in bundle.case_manifests}
    if len(projects) != 1:
        raise TeacherForcedBenchmarkError("teacher-forced bundle must use one project")
    return projects.pop()


class _ProgressWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.genesis_commit: str | None = None
        self.completed_chapters: list[int] = []
        self.last_commit: str | None = None
        self.last_chapter: int = 0

    def open(self) -> None:
        if self.path.exists():
            existing = json.loads(self.path.read_text("utf-8"))
            self.genesis_commit = existing.get("genesis_commit")
            self.last_commit = existing.get("last_accepted_commit")
            self.last_chapter = existing.get("last_accepted_chapter", 0)
            self.completed_chapters = existing.get("completed_chapters", [])

    def close(self) -> None:
        pass

    def record(self, commit: str, chapter: int) -> None:
        self.last_commit = commit
        self.last_chapter = max(self.last_chapter, chapter)
        if chapter not in self.completed_chapters:
            self.completed_chapters.append(chapter)
        self._write()

    def record_pause(
        self,
        chapter: int,
        result: MemoryWriteWorkflowResult,
    ) -> None:
        self._write(
            workflow_pause={
                "chapter": chapter,
                "status": result.status.value,
                "checkpoint_ref": (
                    None
                    if result.checkpoint_ref is None
                    else result.checkpoint_ref.model_dump(mode="json")
                ),
                "terminal_codes": result.terminal_codes,
            }
        )

    def _write(self, *, workflow_pause: dict[str, object] | None = None) -> None:
        payload: dict[str, object] = {
            "last_accepted_commit": self.last_commit,
            "last_accepted_chapter": self.last_chapter,
            "completed_chapters": sorted(self.completed_chapters),
        }
        if self.genesis_commit:
            payload["genesis_commit"] = self.genesis_commit
        if workflow_pause is not None:
            payload["workflow_pause"] = workflow_pause
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(payload, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, self.path)

    def __enter__(self) -> _ProgressWriter:
        self.open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
