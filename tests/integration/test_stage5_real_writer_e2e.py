"""Isolated Stage 5 E2E composing the real Stage3WritingLeafAdapter through the full chain."""

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
from novel_agent.adapters.runtime.isolated import (
    StrictDeterministicCandidateMaterializer,
    StrictFakePlanningLeaf,
)
from novel_agent.adapters.runtime.stage3_writer import Stage3WritingLeafAdapter
from novel_agent.agents import AgentRegistry, StructuredAgentRunner
from novel_agent.agents.candidate_observer import CandidateObservationAgent
from novel_agent.agents.editor import EditorAgent, build_editor_contract_bundle
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    AcceptanceCommand,
    AcceptanceDecision,
    ActorKind,
    AutomationMode,
    CandidateBinding,
    CandidateKind,
    CreativeRunPolicy,
    CreativeRunRequest,
    CreativeRunTerminal,
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
)
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
    ProviderModelResult,
)
from novel_agent.domain.runtime import TaskKind, TaskRecord, TaskStatus
from novel_agent.domain.stage2 import FutureIsolationAttestation
from novel_agent.ports.creative_runtime import WritingLeafPort
from novel_agent.prompts import PromptRegistry
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
from tests.fixtures.stage2_memory_benchmark import writer_context_inputs

HASH = "sha256:" + "1" * 64
PERMISSION_HASH = "sha256:" + "2" * 64
NOW = datetime(2026, 8, 10, tzinfo=UTC)
VERSION = SchemaVersion("1.0.0")
PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "novel_agent"
CANDIDATE_MEDIA_TYPE = "application/vnd.novel-agent.stage5-plan-candidate+json"


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

    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts
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
        plan_ref = _put(self._artifacts, {"goal": "enter the tower"})
        profile_ref = _put(self._artifacts, {"voice": "restrained"})
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
) -> Iterator[tuple[CreativeRuntimeService, RuntimeCommandService, CreativeRunPolicy, CommitId]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    commands = RuntimeCommandService(
        factory, RunEventLogRepository(factory), lambda _project_id: PERMISSION_HASH
    )
    policy = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )
    assembly = _Stage3WriterAssembly(artifacts)
    loop = assembly.build_loop(factory)
    writer = assembly.writing_leaf(loop=loop)
    runtime = CreativeRuntimeService(
        commands,
        RuntimeAcceptanceService(commands, commits, artifacts),
        commits,
        artifacts,
        StrictFakePlanningLeaf(artifacts),
        cast(WritingLeafPort, writer),
        lambda task: assembly.writing_request(task),
        StrictDeterministicCandidateMaterializer(commits, candidate_kind=CandidateKind.PLAN),
        StrictDeterministicCandidateMaterializer(commits, candidate_kind=CandidateKind.DRAFT),
        DerivedProjectionService(ProjectionOutboxRepository(factory), _ProjectionBuilder()),
        DerivedSnapshotRepository(factory),
        lambda policy_hash: policy
        if policy_hash == policy.policy_hash
        else (_ for _ in ()).throw(KeyError(policy_hash)),
    )
    yield runtime, commands, policy, base
    engine.dispose()


def test_real_writer_adapter_composes_through_draft_chain(
    real_writer_kernel: tuple[
        CreativeRuntimeService, RuntimeCommandService, CreativeRunPolicy, CommitId
    ],
) -> None:
    runtime, commands, policy, base = real_writer_kernel
    request = CreativeRunRequest(
        run_id=RunId("run.real-writer"),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        policy=policy,
    )
    start = runtime.start(request)
    assert start.current_task_id is not None
    waiting = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert waiting.terminal is CreativeRunTerminal.WAITING_PLAN_ACCEPTANCE
    assert waiting.current_task_id is not None
    task = commands.get_task(waiting.current_task_id)
    assert len(task.input_artifact_refs) == 1
    ref = task.input_artifact_refs[0]
    accepted = runtime.submit_acceptance(
        AcceptanceCommand(
            command_id=StableId("accept.real-writer.plan"),
            project_id=task.project_id,
            run_id=task.run_id,
            task_id=task.task_id,
            candidate=CandidateBinding(
                candidate_id=StableId("candidate.real-writer.plan"),
                kind=CandidateKind.PLAN,
                artifact_ref=ref,
                candidate_hash=ref.artifact_id.root,
                basis_commit=base,
            ),
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
    assert waiting_draft.terminal is CreativeRunTerminal.WAITING_DRAFT_ACCEPTANCE
    assert waiting_draft.current_task_id is not None
    # Submit the persisted Draft candidate acceptance.
    draft_task = commands.get_task(waiting_draft.current_task_id)
    assert len(draft_task.input_artifact_refs) == 1
    draft_ref = draft_task.input_artifact_refs[0]
    draft_accepted = runtime.submit_acceptance(
        AcceptanceCommand(
            command_id=StableId("accept.real-writer.draft"),
            project_id=draft_task.project_id,
            run_id=draft_task.run_id,
            task_id=draft_task.task_id,
            candidate=CandidateBinding(
                candidate_id=StableId("candidate.real-writer.draft"),
                kind=CandidateKind.DRAFT,
                artifact_ref=draft_ref,
                candidate_hash=draft_ref.artifact_id.root,
                basis_commit=draft_task.basis_commit,
                basis_snapshot=draft_task.basis_snapshot,
            ),
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
    assert len(committed.terminal_artifact_refs) == 1
