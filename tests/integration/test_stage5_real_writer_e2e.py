"""Focused offline Stage 3/4/5 closure through trusted canonical-root materializers."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.runtime.materializers import (
    DraftCandidateMaterializer,
    PlanCandidateMaterializer,
)
from novel_agent.adapters.runtime.stage3_writer import Stage3WritingLeafAdapter
from novel_agent.adapters.runtime.stage4_planner import (
    Stage4PlanningInvocation,
    Stage4PlanningLeafAdapter,
)
from novel_agent.agents import AgentRegistry, StructuredAgentRunner
from novel_agent.agents.candidate_observer import CandidateObservationAgent
from novel_agent.agents.editor import EditorAgent, build_editor_contract_bundle
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.creative_runtime import (
    AcceptanceCommand,
    AcceptanceDecision,
    ActorKind,
    AutomationMode,
    CandidateBinding,
    CreativeRunPolicy,
    CreativeRunRequest,
    CreativeRunTerminal,
    PlanningLoopRequest,
)
from novel_agent.domain.editorial import (
    CuratorObservation,
    EditorialVerdict,
    EditorReviewPayload,
)
from novel_agent.domain.generation import (
    AcceptedPlanBinding,
    WriterTurnAction,
    WriterTurnOutput,
    WriterWorkPlan,
    WritingLengthPolicy,
    WritingLoopBudgets,
    WritingLoopRequest,
    WritingTaskContract,
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
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
    ProviderModelResult,
)
from novel_agent.domain.planning import (
    PlanningLoopEventReceipt,
    PlanningLoopPhase,
    PlanReview,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.planning import (
    PlanningLoopRequest as Stage4PlanningLoopRequest,
)
from novel_agent.domain.planning import (
    PlanningLoopResult as Stage4PlanningLoopResult,
)
from novel_agent.domain.planning import (
    PlanningLoopTerminal as Stage4PlanningLoopTerminal,
)
from novel_agent.domain.runtime import TaskKind, TaskRecord, TaskStatus
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    ContractRef,
    ExecutionStatus,
    FutureIsolationAttestation,
    PlannerExecutionResult,
    PlanningTask,
    PlanProposal,
    ProposalProvenance,
    ProposedItem,
)
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.domain.writing_loop import WritingLoopResult
from novel_agent.ports.creative_runtime import WritingLeafPort
from novel_agent.prompts import PromptRegistry
from novel_agent.runtime.creative_assembly import validate_runtime_assembly
from novel_agent.services.agent_context import (
    AgentContextProjector,
    AgentContextRuntime,
    ContextCompactor,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.creative_runtime import CreativeRuntimeService
from novel_agent.services.editorial import EditorialService
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.planning_context_loop import PlanningContextLoopService
from novel_agent.services.projection import (
    DerivedProjectionService,
    DerivedSnapshotRepository,
    ProjectionOutboxRepository,
)
from novel_agent.services.runtime_acceptance import RuntimeAcceptanceService
from novel_agent.services.runtime_commands import RuntimeCommandService
from novel_agent.services.writer_candidate import WriterCandidateMaterializer
from novel_agent.services.writer_change_reconciliation import WriterChangeReconciliationService
from novel_agent.services.writer_cognition import WriterCognitionService
from novel_agent.services.writer_context_assembler import WriterContextAssembler
from novel_agent.services.writer_context_loop import WriterContextLoopService
from novel_agent.services.writer_reactive_memory import (
    ReactiveMemoryInputs,
    WriterReactiveNeedAdapter,
)
from novel_agent.skills import SkillRegistry, SkillTemplate
from tests.factories import make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.fixtures.stage2_memory_benchmark import writer_context_inputs

HASH = "sha256:" + "1" * 64
PERMISSION_HASH = "sha256:" + "2" * 64
NOW = datetime(2026, 8, 10, tzinfo=UTC)
VERSION = SchemaVersion("1.0.0")
PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "novel_agent"
PLAN_PROPOSAL_MEDIA_TYPE = "application/vnd.novel-agent.plan-proposal+json"
PLAN_REVIEW_MEDIA_TYPE = "application/vnd.novel-agent.plan-review+json"
PLANNING_EVENT_MEDIA_TYPE = "application/vnd.novel-agent.planning-loop-event+json"
PLANNER_EXECUTION_MEDIA_TYPE = "application/vnd.novel-agent.planner-execution-result+json"


class _SequenceEndpoint(FakeModelEndpoint):
    def __init__(self, responses: tuple[str, ...]) -> None:
        super().__init__("")
        self._responses = iter(responses)

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.response_text = next(self._responses)
        return await super().generate(request)


class _BoundObserverEndpoint(FakeModelEndpoint):
    def __init__(self) -> None:
        super().__init__("")

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        match = re.search(r'"draft_id":"(sha256:[0-9a-f]{64})"', request.prompt)
        assert match is not None
        self.response_text = CuratorObservation(
            draft_id=ArtifactId(match.group(1)),
        ).model_dump_json()
        return await super().generate(request)


class _WriterEndpoint(FakeModelEndpoint):
    def __init__(self, assembly: _Stage3WriterAssembly) -> None:
        super().__init__("")
        self._assembly = assembly
        self._pending_tasks: list[str] = []

    def prepare_task(self, task_id_root: str) -> None:
        self._pending_tasks.append(task_id_root)

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        task_id_root = request.task_id.root
        if self._pending_tasks:
            self.response_text = self._assembly._work_plan_response(task_id_root)
            self._pending_tasks.pop()
        else:
            self.response_text = _writer_turn(
                "Lin studies the moonlit groove and opens the gate without using her injured arm."
            ).model_dump_json()
        return await super().generate(request)


def _gateway(endpoint: FakeModelEndpoint, name: str) -> ModelGateway:
    return ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name=name,
                model_name=name,
                adapter=endpoint,
            ),
        ),
        forbid_external_calls=True,
    )


def _put(artifacts: ArtifactRepository, value: object) -> ArtifactRef:
    from novel_agent.services.content_addressing import canonical_json_bytes

    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return artifacts.put(canonical_json_bytes(payload), "application/json", VERSION)


def _canonical_manifest(artifacts: ArtifactRepository) -> RootManifest:
    from novel_agent.services.content_addressing import canonical_json_bytes

    bundle = make_synthetic_bundle()
    text = next(item for item in bundle.text_roots if len(item.chapters) == 20)
    plan = bundle.plan_roots[0]
    world = bundle.world_roots[0]
    text_artifact = artifacts.put(
        canonical_json_bytes(text.model_dump(mode="json")),
        "application/vnd.novel-agent.text-root+json",
        text.schema_version,
    )
    plan_artifact = artifacts.put(
        canonical_json_bytes(plan.model_dump(mode="json")),
        "application/vnd.novel-agent.plan-root+json",
        plan.schema_version,
    )
    world_artifact = artifacts.put(
        canonical_json_bytes(world.model_dump(mode="json")),
        "application/vnd.novel-agent.world-root+json",
        world.schema_version,
    )
    profile_artifact = _put(artifacts, {"voice": "restrained"})
    reference_artifact = _put(artifacts, {"references": []})
    return make_manifest().model_copy(
        update={
            "text_root": TextRootRef(**text_artifact.model_dump(mode="python")),
            "plan_root": PlanRootRef(**plan_artifact.model_dump(mode="python")),
            "world_root": WorldRootRef(**world_artifact.model_dump(mode="python")),
            "project_profile_root": ProjectProfileRootRef(
                **profile_artifact.model_dump(mode="python")
            ),
            "reference_root": ReferenceRootRef(
                **reference_artifact.model_dump(mode="python")
            ),
        }
    )


def _receipt(
    agent_type: AgentType,
    mode: AgentMode,
    run_id: RunId,
    task_id: TaskId,
    base_commit: CommitId,
) -> AgentExecutionReceipt:
    return AgentExecutionReceipt(
        receipt_id=StableId(f"receipt.{agent_type.value}.{task_id.root}"[:128]),
        run_id=run_id,
        task_id=task_id,
        agent_spec=ContractRef(
            contract_id=StableId(f"agent.{agent_type.value}"),
            version=VERSION,
            content_hash=ArtifactId(HASH),
        ),
        agent_type=agent_type,
        agent_mode=mode,
        prompt_fingerprint=ArtifactId(HASH),
        configuration_fingerprint=ArtifactId(HASH),
        base_commit=base_commit,
        status=ExecutionStatus.SUCCEEDED,
        started_at=NOW,
        completed_at=NOW,
        latency_ms=1,
    )


class _MaterializableStage4Loop:
    """Offline deterministic terminal behind the real Stage 4 public adapter."""

    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts

    async def run(self, **kwargs: object) -> Stage4PlanningLoopResult:
        from novel_agent.services.content_addressing import canonical_json_bytes

        request = cast(Stage4PlanningLoopRequest, kwargs["request"])
        assert request.task.base_commit is not None
        planner_receipt = _receipt(
            AgentType.PLANNER,
            AgentMode.CHAPTER,
            request.run_id,
            request.task_id,
            request.task.base_commit,
        )
        proposal = PlanProposal(
            proposal_id=StableId(f"proposal.{request.task_id.root}"[:128]),
            project_id=request.project_id,
            mode=AgentMode.CHAPTER,
            base_commit=request.task.base_commit,
            items=(
                ProposedItem(
                    item_id=StableId("plan.chapter.21"),
                    kind="chapter",
                    payload={
                        "title": "Enter the tower",
                        "summary": "Lin enters the tower without violating the injury constraint.",
                        "chapter_index": 21,
                    },
                    provenance=ProposalProvenance.PLANNER_PROPOSED,
                ),
            ),
            coverage=1.0,
            receipt=planner_receipt,
        )
        proposal_ref = self._artifacts.put(
            canonical_json_bytes(proposal.model_dump(mode="json")),
            PLAN_PROPOSAL_MEDIA_TYPE,
            VERSION,
        )
        reviewer_receipt = _receipt(
            AgentType.PLAN_REVIEWER,
            AgentMode.CHAPTER,
            request.run_id,
            request.task_id,
            request.task.base_commit,
        ).model_copy(
            update={"input_artifacts": (proposal_ref,)}
        )
        review = PlanReview(
            review_id=StableId(f"review.{request.task_id.root}"[:128]),
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            target_artifact_ref=proposal_ref,
            decision=ReviewDecision.ACCEPT,
            receipt=reviewer_receipt,
        )
        review_ref = self._artifacts.put(
            canonical_json_bytes(review.model_dump(mode="json")),
            PLAN_REVIEW_MEDIA_TYPE,
            VERSION,
        )
        raw_output = _put(self._artifacts, {"plan": "accepted"})
        execution = PlannerExecutionResult(
            mode=AgentMode.CHAPTER,
            plan_proposal=proposal,
            output_artifact=raw_output,
            receipt=planner_receipt,
        )
        execution_ref = self._artifacts.put(
            canonical_json_bytes(execution.model_dump(mode="json")),
            PLANNER_EXECUTION_MEDIA_TYPE,
            VERSION,
        )
        event = PlanningLoopEventReceipt(
            event_id=StableId(f"event.{request.task_id.root}"[:128]),
            request_id=StableId(f"request.{request.task_id.root}"[:128]),
            phase=PlanningLoopPhase.PLAN_REVIEWED,
            event_kind="plan.review_settled",
            artifact_refs=(proposal_ref, review_ref, execution_ref),
        )
        event_ref = self._artifacts.put(
            canonical_json_bytes(event.model_dump(mode="json")),
            PLANNING_EVENT_MEDIA_TYPE,
            VERSION,
        )
        return Stage4PlanningLoopResult(
            request_id=request.request_id,
            terminal=Stage4PlanningLoopTerminal.PLAN_CANDIDATE_READY,
            proposal=proposal,
            plan_review_ref=review_ref,
            event_artifacts=(event_ref,),
        )


def _planning_leaf(
    artifacts: ArtifactRepository, commits: CommitService
) -> Stage4PlanningLeafAdapter:
    def invocation(request: PlanningLoopRequest) -> Stage4PlanningInvocation:
        assert request.basis_snapshot is not None
        manifest = commits.load_manifest(request.basis_commit)
        task = PlanningTask(
            planning_task_id=StableId(request.task_id.root),
            project_id=request.project_id,
            mode=AgentMode.CHAPTER,
            base_commit=request.basis_commit,
            source_ids=(),
        )
        detailed = Stage4PlanningLoopRequest.model_construct(
            request_id=StableId(f"request.{request.task_id.root}"[:128]),
            run_id=request.run_id,
            task_id=request.task_id,
            project_id=request.project_id,
            task=task,
            author_intent_artifacts=(),
            accepted_plan_ref=manifest.plan_root,
            accepted_world_ref=manifest.world_root,
            accepted_text_ref=manifest.text_root,
            project_profile_ref=manifest.project_profile_root,
            snapshot_id=request.basis_snapshot,
        )
        return Stage4PlanningInvocation(
            request=detailed,
            model_request=lambda _phase, _mode, _attempt: cast(ModelRequest, object()),
        )

    return Stage4PlanningLeafAdapter(
        cast(PlanningContextLoopService, _MaterializableStage4Loop(artifacts)),
        artifacts,
        invocation,
        schema_version=VERSION,
    )


def _work_plan(request: WritingLoopRequest) -> WriterWorkPlan:
    return WriterWorkPlan(
        work_plan_id=StableId(f"work-plan.{request.task_id.root}"),
        writing_task_ref=request.writing_task_artifact,
        accepted_plan_ref=request.accepted_plan.artifact,
        writer_context_ref=request.writer_context_package_artifact,
        scene_beat_order=("Observe the gate.", "Redirect moonlight."),
        participating_characters=("Lin",),
        character_current_states=("Lin's left arm remains injured.",),
        pov_boundary="Only Lin's current knowledge may be narrated.",
        reader_disclosure_boundary="Keep the tower's final secret hidden.",
        must_keep=request.writing_task.mandatory_constraints,
        must_avoid=request.writing_task.forbidden_reveals,
        selected_skill_ids=request.allowed_skills,
        expected_skill_checkpoints={"skill.scene-composition": ("gate opens",)},
    )


def _writer_turn(text: str) -> WriterTurnOutput:
    return WriterTurnOutput(
        action=WriterTurnAction.DRAFT_READY,
        draft_text=text,
        unresolved_questions=("The guard beyond the gate remains unknown.",),
        self_observations=("The injured-arm constraint is preserved.",),
        work_plan_checkpoint="gate opens",
    )


class _Stage3WriterAssembly:
    """Own the real WriterContextLoopService and Stage3WritingLeafAdapter wiring."""

    def __init__(self, artifacts: ArtifactRepository, commits: CommitService) -> None:
        self._artifacts = artifacts
        self._commits = commits
        self._memory_task, self._needs, self._units, self._fixture_base = writer_context_inputs()
        self._work_plan_responses: dict[str, str] = {}

    def writing_leaf(
        self,
        *,
        loop: WriterContextLoopService,
    ) -> Stage3WritingLeafAdapter:
        def model_request_factory(request: WritingLoopRequest) -> ModelRequest:
            return ModelRequest(
                request_id=StableId(f"request.{request.task_id.root}"),
                run_id=request.run_id,
                task_id=request.task_id,
                model_role=ModelRole.BATCH_TEST,
                purpose=ModelCallPurpose.BATCH_TEST,
                trace_id=f"trace.{request.run_id.root}",
                prompt="replaced by Stage 3 services",
            )

        def reactive_inputs_factory(request: WritingLoopRequest) -> ReactiveMemoryInputs:
            return cast(ReactiveMemoryInputs, None)

        return Stage3WritingLeafAdapter(
            loop,
            model_request_factory,
            reactive_inputs_factory,
        )

    def writing_request(self, task: TaskRecord) -> WritingLoopRequest:
        assert task.basis_snapshot is not None
        assembled = WriterContextAssembler().assemble(
            task=self._memory_task,
            units=self._units,
            needs=self._needs,
            basis_commit_id=task.basis_commit,
            basis_snapshot_id=task.basis_snapshot,
            arm="A",
            writer_token_budget=20_000,
        )
        package = assembled.package
        assert package is not None
        writing_task = WritingTaskContract(
            contract_id=StableId(f"writing-contract.{task.task_id.root}"),
            target_chapter=21,
            target_scenes=(StableId(f"scene.{task.task_id.root}"),),
            pov="Lin",
            narrative_person="third person limited",
            chapter_goal="Enter the tower without violating the injury constraint.",
            required_beats=("Observe the gate.", "Redirect moonlight."),
            mandatory_constraints=("Do not force the gate with the injured arm.",),
            forbidden_reveals=("Do not reveal the tower's final secret.",),
            length_policy=WritingLengthPolicy(
                minimum_characters=20,
                target_characters=100,
                maximum_characters=500,
            ),
        )
        task_ref = _put(self._artifacts, writing_task)
        manifest = self._commits.load_manifest(task.basis_commit)
        plan_ref = ArtifactRef.model_validate(
            manifest.plan_root.model_dump(mode="python", exclude={"root_kind"})
        )
        profile_ref = ArtifactRef.model_validate(
            manifest.project_profile_root.model_dump(mode="python", exclude={"root_kind"})
        )
        package_ref = _put(self._artifacts, package)
        request = WritingLoopRequest(
            run_id=task.run_id,
            task_id=task.task_id,
            project_id=task.project_id,
            base_commit=task.basis_commit,
            snapshot_id=task.basis_snapshot,
            writing_task=writing_task,
            writing_task_artifact=task_ref,
            accepted_plan=AcceptedPlanBinding(
                artifact=plan_ref,
                revision="accepted-v1",
                task_contract_id=writing_task.contract_id,
                base_commit=task.basis_commit,
                snapshot_id=task.basis_snapshot,
            ),
            project_profile_artifact=profile_ref,
            project_profile_revision="profile-v1",
            writer_context_package=package,
            writer_context_package_artifact=package_ref,
            future_isolation_attestation=FutureIsolationAttestation(
                attestation_id=StableId(f"attestation.{task.task_id.root}"),
                checkpoint_chapter=20,
                canonical_source_ids=(StableId("source.stage3.visible"),),
                evaluator_only_source_ids=(StableId("source.stage3.future"),),
                passed=True,
                configuration_fingerprint=ArtifactId("sha256:" + "f" * 64),
            ),
            allowed_skills=(StableId("skill.scene-composition"),),
            budgets=WritingLoopBudgets(
                context_sequence_limit=100_000,
                reserved_output_tokens=2_000,
                context_safety_allowance_tokens=1_000,
                context_soft_limit_tokens=90_000,
            ),
            writer_configuration_fingerprint=ArtifactId("sha256:" + "e" * 64),
            model_configuration_fingerprint=ArtifactId("sha256:" + "d" * 64),
        )
        self._work_plan_responses[task.task_id.root] = _work_plan(request).model_dump_json()
        endpoint = getattr(self, "_writer_endpoint", None)
        if endpoint is not None:
            endpoint.prepare_task(task.task_id.root)
        return request

    def _work_plan_response(self, task_id_root: str) -> str:
        response = self._work_plan_responses.pop(task_id_root, None)
        assert response is not None, f"work plan not prepared for {task_id_root}"
        return response

    def _writer_turn_response(self, text: str) -> str:
        from tests.integration.test_stage5_real_writer_e2e import _writer_turn as _turn

        return _turn(text).model_dump_json()

    def build_loop(self, factory: sessionmaker[Session]) -> WriterContextLoopService:
        from novel_agent.adapters.postgres.database import Base as _Base

        writer_engine = create_engine("sqlite+pysqlite:///:memory:")
        _Base.metadata.create_all(writer_engine)
        writer_factory = build_session_factory(writer_engine)
        writer_endpoint = _WriterEndpoint(self)
        self._writer_endpoint = writer_endpoint
        writer_gateway = _gateway(writer_endpoint, "stage3-writer")
        contracts = WriterCognitionService.skill_contracts()
        skill_paths = {
            "skill.scene-composition": PACKAGE_ROOT / "skills" / "scene_composition_v1.md",
        }
        skills = SkillRegistry(
            SkillTemplate(
                skill_id=contract.contract_id,
                version=contract.version,
                path=skill_paths[contract.contract_id.root],
                expected_hash=contract.content_hash,
            )
            for contract in contracts
            if contract.contract_id.root in skill_paths
        )
        cognition = WriterCognitionService(
            writer_gateway,
            self._artifacts,
            skills,
            require_admission=False,
        )
        editor_bundle = build_editor_contract_bundle()
        editor_gateway = _gateway(
            _SequenceEndpoint(
                (EditorReviewPayload(verdict=EditorialVerdict.PASS).model_dump_json(),)
            ),
            "stage3-editor",
        )
        editor = EditorialService(
            EditorAgent(
                StructuredAgentRunner(
                    editor_gateway,
                    AgentRegistry(editor_bundle.agent_specs),
                    PromptRegistry(editor_bundle.prompt_templates),
                    SkillRegistry(editor_bundle.skill_templates),
                )
            ),
            self._artifacts,
            VERSION,
        )
        observer = CandidateObservationAgent(
            _gateway(_BoundObserverEndpoint(), "stage3-observer"),
            self._artifacts,
            require_admission=False,
        )
        projector = AgentContextProjector(lambda value: max(1, len(value) // 4))
        events = RunEventLogRepository(writer_factory)
        checkpoints = RunCheckpointRepository(writer_factory)
        runtime = AgentContextRuntime(projector, self._artifacts, events, checkpoints, VERSION)
        return WriterContextLoopService(
            projector,
            ContextCompactor(
                projector, self._artifacts, VERSION, lambda value: max(1, len(value) // 4)
            ),
            runtime,
            cognition,
            cast(WriterReactiveNeedAdapter, None),
            WriterCandidateMaterializer(self._artifacts, VERSION),
            editor,
            observer,
            WriterChangeReconciliationService(),
            self._artifacts,
            events,
        )


class _ProjectionBuilder:
    def build(self, project_id: ProjectId, source_commit: CommitId) -> DerivedSnapshotLite:
        suffix = source_commit.root.removeprefix("sha256:")
        return DerivedSnapshotLite(
            snapshot_id=StableId(f"snapshot.{suffix}"),
            source_commit=source_commit,
            anchor_build_id=StableId(f"anchor.{suffix[:24]}"),
            anchor_index_version="anchor-v1",
            grounded_index_version="grounded-v1",
            embedding_profile="offline-v1",
            fusion_profile="rrf-v1",
            build_status=DerivedBuildStatus.EXACT,
            published_at=NOW,
        )


@pytest.fixture
def real_writer_kernel(
    tmp_path: Path,
) -> Iterator[
    tuple[
        CreativeRuntimeService,
        RuntimeCommandService,
        CreativeRunPolicy,
        CommitId,
        ArtifactRepository,
        CommitService,
    ]
]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    commits = CommitService(factory)
    base = commits.initialize_project(_canonical_manifest(artifacts))
    commands = RuntimeCommandService(
        factory, RunEventLogRepository(factory), lambda _project_id: PERMISSION_HASH
    )
    policy = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )
    assembly = _Stage3WriterAssembly(artifacts, commits)
    loop = assembly.build_loop(factory)
    writer = assembly.writing_leaf(loop=loop)
    planner = _planning_leaf(artifacts, commits)
    plan_materializer = PlanCandidateMaterializer(
        artifacts, commits, schema_version=VERSION
    )
    draft_materializer = DraftCandidateMaterializer(
        artifacts, commits, schema_version=VERSION
    )
    validate_runtime_assembly(
        load_stage5_manifest(PACKAGE_ROOT / "runtime" / "stage5_development_manifest.json"),
        planner=planner,
        writer=writer,
        plan_materializer=plan_materializer,
        draft_materializer=draft_materializer,
        production=True,
    )
    runtime = CreativeRuntimeService(
        commands,
        RuntimeAcceptanceService(commands, commits, artifacts),
        commits,
        artifacts,
        planner,
        cast(WritingLeafPort, writer),
        lambda task: assembly.writing_request(task),
        plan_materializer,
        draft_materializer,
        DerivedProjectionService(ProjectionOutboxRepository(factory), _ProjectionBuilder()),
        DerivedSnapshotRepository(factory),
        lambda policy_hash: policy
        if policy_hash == policy.policy_hash
        else (_ for _ in ()).throw(KeyError(policy_hash)),
    )
    yield runtime, commands, policy, base, artifacts, commits
    engine.dispose()


def test_real_writer_adapter_composes_through_draft_chain(
    real_writer_kernel: tuple[
        CreativeRuntimeService,
        RuntimeCommandService,
        CreativeRunPolicy,
        CommitId,
        ArtifactRepository,
        CommitService,
    ],
) -> None:
    runtime, commands, policy, base, artifacts, commits = real_writer_kernel
    request = CreativeRunRequest(
        run_id=RunId("run.real-writer"),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        basis_snapshot=StableId("snapshot.initial"),
        policy=policy,
    )
    start = runtime.start(request)
    assert start.current_task_id is not None
    waiting = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert waiting.terminal is CreativeRunTerminal.WAITING_PLAN_ACCEPTANCE
    assert waiting.current_task_id is not None
    task = commands.get_task(waiting.current_task_id)
    assert len(task.input_artifact_refs) == 1
    assert task.candidate_binding_ref is not None
    plan_candidate = CandidateBinding.model_validate_json(
        artifacts.read_verified(task.candidate_binding_ref)
    )
    accepted = runtime.submit_acceptance(
        AcceptanceCommand(
            command_id=StableId("accept.real-writer.plan"),
            project_id=task.project_id,
            run_id=task.run_id,
            task_id=task.task_id,
            candidate=plan_candidate,
            acceptance_policy_hash=HASH,
            actor_kind=ActorKind.AUTHOR,
            actor_id="author",
            decision=AcceptanceDecision.ACCEPT,
            reason="approved",
            expected_project_commit=base,
            idempotency_identity=StableId("accept.real-writer.plan.identity"),
            issued_at=NOW,
        ),
        policy=policy,
    )
    assert accepted.current_task_id is not None
    projection = asyncio.run(runtime.advance(accepted.current_task_id, worker_id="commit"))
    assert projection.current_task_id is not None
    draft = asyncio.run(runtime.advance(projection.current_task_id, worker_id="projection"))
    assert draft.current_task_id is not None
    # The real Stage3WriterLeafAdapter drives the draft candidate through the real loop.
    waiting_draft = asyncio.run(runtime.advance(draft.current_task_id, worker_id="writer.real"))
    failed_task = commands.get_task(draft.current_task_id)
    diagnostic = next(
        (
            WritingLoopResult.model_validate_json(artifacts.read_verified(ref))
            for ref in failed_task.terminal_artifact_refs
            if ref.media_type
            == "application/vnd.novel-agent.writing-loop-result+json"
        ),
        None,
    )
    assert waiting_draft.terminal is CreativeRunTerminal.WAITING_DRAFT_ACCEPTANCE, diagnostic
    assert waiting_draft.current_task_id is not None
    # Submit the persisted Draft candidate acceptance.
    draft_task = commands.get_task(waiting_draft.current_task_id)
    assert len(draft_task.input_artifact_refs) == 1
    assert draft_task.candidate_binding_ref is not None
    draft_candidate = CandidateBinding.model_validate_json(
        artifacts.read_verified(draft_task.candidate_binding_ref)
    )
    draft_accepted = runtime.submit_acceptance(
        AcceptanceCommand(
            command_id=StableId("accept.real-writer.draft"),
            project_id=draft_task.project_id,
            run_id=draft_task.run_id,
            task_id=draft_task.task_id,
            candidate=draft_candidate,
            acceptance_policy_hash=HASH,
            actor_kind=ActorKind.AUTHOR,
            actor_id="author",
            decision=AcceptanceDecision.ACCEPT,
            reason="approved",
            expected_project_commit=draft_task.basis_commit,
            idempotency_identity=StableId("accept.real-writer.draft.identity"),
            issued_at=NOW,
        ),
        policy=policy,
    )
    assert draft_accepted.current_task_id is not None
    draft_commit_task = commands.get_task(draft_accepted.current_task_id)
    assert draft_commit_task.kind is TaskKind.DRAFT_COMMIT
    # Advance the Draft Commit, then Projection/Freshness to exact readiness.
    after_commit = asyncio.run(
        runtime.advance(draft_accepted.current_task_id, worker_id="commit.draft")
    )
    assert after_commit.current_task_id is not None
    freshness = asyncio.run(
        runtime.advance(after_commit.current_task_id, worker_id="projection.draft")
    )
    # Exact freshness publishes the committed snapshot, so the run advances to a
    # terminal COMPLETED when target_chapters == 1.
    assert freshness.terminal is CreativeRunTerminal.COMPLETED
    assert freshness.current_commit == after_commit.current_commit
    committed = commands.get_task(draft_commit_task.task_id)
    assert committed.status is TaskStatus.SUCCEEDED
    assert len(committed.terminal_artifact_refs) == 5
    assert freshness.current_commit is not None
    manifest = commits.load_manifest(freshness.current_commit)
    plan = PlanRootDocument.model_validate_json(artifacts.read_verified(manifest.plan_root))
    text = TextRootDocument.model_validate_json(artifacts.read_verified(manifest.text_root))
    assert any(item.plan_node_id == StableId("plan.chapter.21") for item in plan.nodes)
    assert text.chapters[-1].chapter_index == 21
    assert "opens the gate" in text.chapters[-1].scenes[0].blocks[0].text
