from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import JsonValue, ValidationError
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.agents import AgentRegistry, StructuredAgentRunner
from novel_agent.agents.candidate_observer import (
    CandidateObservationAgent,
    CandidateObservationError,
)
from novel_agent.agents.editor import EditorAgent, build_editor_contract_bundle
from novel_agent.domain.agent_context import (
    ContextDelta,
    ContextDeltaStatus,
    ContextItemKind,
    ContextLayer,
    ContextViewItem,
    SettledArtifactPayload,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.editorial import (
    CuratorObservation,
    EditorialIssueType,
    EditorialSeverity,
    EditorialVerdict,
    EditorRepairPayload,
    EditorReviewPayload,
)
from novel_agent.domain.generation import (
    AcceptedPlanBinding,
    DeclaredMemoryHint,
    MemoryHintChangeKind,
    WriterContextItem,
    WriterContextSnapshot,
    WriterMemoryRequest,
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
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
    ProviderModelResult,
)
from novel_agent.domain.runtime import RunEvent, RunEventType
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentMode,
    FutureIsolationAttestation,
    MemoryGatewayMode,
)
from novel_agent.domain.stage3_evaluation import (
    ContextScheme,
    EvaluatorDimension,
    EvaluatorScore,
    Stage3EvaluationCase,
)
from novel_agent.domain.stage3_loop_evaluation import (
    Stage3FormalManifest,
    Stage3FullChainCaseResult,
    Stage3FullChainSchemeResult,
)
from novel_agent.domain.writer_context import EvidenceFirstGap, EvidenceGapKind
from novel_agent.domain.writing_loop import (
    WritingLoopCheckpoint,
    WritingLoopPhase,
    WritingLoopResult,
    WritingLoopTerminalStatus,
)
from novel_agent.ports.model_endpoint import ModelEndpointError
from novel_agent.prompts import PromptRegistry
from novel_agent.prompts.registry import content_hash
from novel_agent.services.agent_context import (
    AgentContextProjector,
    AgentContextRuntime,
    ContextCompactor,
    ContextLimitError,
    ContextWindowPolicy,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.editorial import EditorialReviewError, EditorialService
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.evidence_first_writer_context_assembler import (
    EvidenceFirstWriterContextAssembler,
    NeedEvidenceSelection,
    SliceSelectionTrace,
)
from novel_agent.services.evidence_slice_resolver import EvidenceSliceResolver
from novel_agent.services.memory_gateway import MemoryGatewayBlockedError
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.recent_prose import RecentProseAssembler
from novel_agent.services.stage3_evaluation import load_case
from novel_agent.services.stage3_loop_evaluation import (
    PreparedFullChainRun,
    Stage3FullChainEvaluationService,
)
from novel_agent.services.task_focus import TaskFocusExtractor
from novel_agent.services.writer_candidate import (
    WriterCandidateError,
    WriterCandidateMaterializer,
)
from novel_agent.services.writer_change_reconciliation import (
    ReconciliationError,
    WriterChangeReconciliationService,
)
from novel_agent.services.writer_cognition import WriterCognitionError, WriterCognitionService
from novel_agent.services.writer_context_assembler import WriterContextAssembler
from novel_agent.services.writer_context_loop import WriterContextLoopService
from novel_agent.services.writer_reactive_memory import (
    ReactiveMemoryInputs,
    ReactiveMemoryResult,
    WriterReactiveMemoryError,
    WriterReactiveNeedAdapter,
)
from novel_agent.skills import SkillRegistry, SkillTemplate
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.fixtures.stage2_memory_benchmark import writer_context_inputs
from tests.unit.test_stage2_memory_controller import request as memory_resolution_request
from tests.unit.test_stage2_memory_gateway import gateway as deterministic_memory_gateway

VERSION = SchemaVersion("1.0.0")
PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "novel_agent"


class SequenceEndpoint(FakeModelEndpoint):
    def __init__(self, responses: tuple[str, ...]) -> None:
        super().__init__("")
        self._responses = iter(responses)

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.response_text = next(self._responses)
        return await super().generate(request)


class BoundObserverEndpoint(FakeModelEndpoint):
    def __init__(self) -> None:
        super().__init__("")

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        match = re.search(r'"draft_id":"(sha256:[0-9a-f]{64})"', request.prompt)
        assert match is not None
        self.response_text = CuratorObservation(
            draft_id=ArtifactId(match.group(1)),
        ).model_dump_json()
        return await super().generate(request)


class DiscardingRawResponses(dict[str, str]):
    def __setitem__(self, key: str, value: str) -> None:
        del key, value


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
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return artifacts.put(canonical_json_bytes(payload), "application/json", VERSION)


def _request(artifacts: ArtifactRepository, suffix: str) -> WritingLoopRequest:
    memory_task, needs, units, base_commit = writer_context_inputs()
    snapshot_id = StableId(f"snapshot.stage3-loop.{suffix}")
    assembled = WriterContextAssembler().assemble(
        task=memory_task,
        units=units,
        needs=needs,
        basis_commit_id=base_commit,
        basis_snapshot_id=snapshot_id,
        arm="A",
        writer_token_budget=20_000,
    )
    package = assembled.package
    assert package is not None
    writing_task = WritingTaskContract(
        contract_id=StableId(f"writing-contract.stage3-loop.{suffix}"),
        target_chapter=21,
        target_scenes=(StableId(f"scene.stage3-loop.{suffix}"),),
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
    task_ref = _put(artifacts, writing_task)
    plan_ref = _put(artifacts, {"goal": "enter the tower"})
    profile_ref = _put(artifacts, {"voice": "restrained"})
    package_ref = _put(artifacts, package)
    text_root = next(
        item for item in make_synthetic_bundle().text_roots if len(item.chapters) == 20
    )
    recent_prose, recent_prose_ref = RecentProseAssembler(artifacts, VERSION).assemble(
        text_root=text_root,
        base_commit=base_commit,
        snapshot_id=snapshot_id,
        target_chapter=writing_task.target_chapter,
    )
    assert hasattr(task_ref, "artifact_id")
    assert hasattr(plan_ref, "artifact_id")
    assert hasattr(profile_ref, "artifact_id")
    assert hasattr(package_ref, "artifact_id")
    return WritingLoopRequest(
        run_id=RunId(f"run.stage3-loop.{suffix}"),
        task_id=TaskId(f"task.stage3-loop.{suffix}"),
        project_id=ProjectId("project.stage3-loop"),
        base_commit=base_commit,
        snapshot_id=snapshot_id,
        writing_task=writing_task,
        writing_task_artifact=task_ref,
        accepted_plan=AcceptedPlanBinding(
            artifact=plan_ref,
            revision="accepted-v1",
            task_contract_id=writing_task.contract_id,
            base_commit=base_commit,
            snapshot_id=snapshot_id,
        ),
        project_profile_artifact=profile_ref,
        project_profile_revision="profile-v1",
        writer_context_package=package,
        writer_context_package_artifact=package_ref,
        recent_prose_context=recent_prose,
        recent_prose_context_artifact=recent_prose_ref,
        future_isolation_attestation=FutureIsolationAttestation(
            attestation_id=StableId(f"attestation.stage3-loop.{suffix}"),
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


def _editor_responses(route: EditorialVerdict, text: str) -> tuple[str, ...]:
    if route is EditorialVerdict.PASS:
        return (EditorReviewPayload(verdict=route).model_dump_json(),)
    if route is EditorialVerdict.LOCAL_REPAIR:
        quote = "opens the gate"
        review = {
            "verdict": route.value,
            "issues": [
                {
                    "issue_type": EditorialIssueType.STYLE.value,
                    "severity": EditorialSeverity.ERROR.value,
                    "description": "Clarify the entrance action.",
                    "evidence_quote": quote,
                    "occurrence": 0,
                    "repairable": True,
                    "structural": False,
                }
            ],
            "repair_instructions": ["Clarify the action without changing the beat."],
            "preserve_requirements": ["Keep the injury constraint."],
        }
        repaired = text.replace(quote, "opens it anew")
        return (
            json.dumps(review),
            EditorRepairPayload(repaired_text=repaired).model_dump_json(),
            EditorReviewPayload(verdict=EditorialVerdict.PASS).model_dump_json(),
        )
    review = {
        "verdict": route.value,
        "issues": [
            {
                "issue_type": EditorialIssueType.STRUCTURE.value,
                "severity": EditorialSeverity.ERROR.value,
                "description": "The entrance action needs a new structure.",
                "repairable": False,
                "structural": True,
            }
        ],
        "rewrite_targets": ["Rebuild the entrance around reflected moonlight."],
        "rewrite_preserve_requirements": ["Keep the injury constraint."],
    }
    return (
        json.dumps(review),
        EditorReviewPayload(verdict=EditorialVerdict.PASS).model_dump_json(),
    )


@pytest.fixture
def repositories() -> Iterator[tuple[RunEventLogRepository, RunCheckpointRepository]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    from novel_agent.adapters.postgres.database import Base, build_session_factory

    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    yield RunEventLogRepository(factory), RunCheckpointRepository(factory)
    engine.dispose()


def _loop(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
    request: WritingLoopRequest,
    route: EditorialVerdict,
    *,
    artifact_repository: ArtifactRepository | None = None,
    writer_turns: tuple[WriterTurnOutput, ...] | None = None,
    editor_responses: tuple[str, ...] | None = None,
) -> tuple[WriterContextLoopService, ModelRequest, ArtifactRepository]:
    artifacts = artifact_repository or ArtifactRepository(
        FilesystemObjectStore(tmp_path / "objects")
    )
    initial_text = (
        "Lin studies the moonlit groove and opens the gate without using her injured arm."
    )
    rewrite_text = "Lin studies first, then redirects moonlight to open the gate safely."
    selected_turns = writer_turns or (_writer_turn(initial_text),)
    writer_responses = [
        _work_plan(request).model_dump_json(),
        *(turn.model_dump_json() for turn in selected_turns),
    ]
    if route is EditorialVerdict.MAJOR_REWRITE and writer_turns is None:
        writer_responses.append(_writer_turn(rewrite_text).model_dump_json())
    writer_gateway = _gateway(SequenceEndpoint(tuple(writer_responses)), "stage3-writer")
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
        artifacts,
        skills,
        require_admission=False,
    )

    editor_bundle = build_editor_contract_bundle()
    editor_gateway = _gateway(
        SequenceEndpoint(editor_responses or _editor_responses(route, initial_text)),
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
        artifacts,
        VERSION,
    )
    observer = CandidateObservationAgent(
        _gateway(BoundObserverEndpoint(), "stage3-observer"),
        artifacts,
        require_admission=False,
    )
    projector = AgentContextProjector(lambda value: max(1, len(value) // 4))
    events, checkpoints = repositories
    runtime = AgentContextRuntime(projector, artifacts, events, checkpoints, VERSION)
    loop = WriterContextLoopService(
        projector,
        ContextCompactor(projector, artifacts, VERSION, lambda value: max(1, len(value) // 4)),
        runtime,
        cognition,
        cast(WriterReactiveNeedAdapter, object()),
        WriterCandidateMaterializer(artifacts, VERSION),
        editor,
        observer,
        WriterChangeReconciliationService(),
        artifacts,
        events,
    )
    model_request = ModelRequest(
        request_id=StableId(f"request.{request.run_id.root}"),
        run_id=request.run_id,
        task_id=request.task_id,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id=f"trace.{request.run_id.root}",
        prompt="replaced by Stage 3 services",
    )
    return loop, model_request, artifacts


def test_writer_events_and_checkpoints_are_scoped_to_the_task_stream(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "stream-objects"))
    first_request = _request(artifacts, "shared-stream")
    second_request = first_request.model_copy(
        update={"task_id": TaskId("task.stage3-loop.shared-stream.second")}
    )
    loop, _model_request, _loop_artifacts = _loop(
        tmp_path,
        repositories,
        first_request,
        EditorialVerdict.PASS,
        artifact_repository=artifacts,
    )
    events, checkpoints = repositories
    events.append(
        RunEvent(
            event_id=StableId("event.runtime.before-writer"),
            run_id=first_request.run_id,
            task_id=TaskId("task.runtime.before-writer"),
            sequence_no=1,
            event_type=RunEventType.TASK_STARTED,
            occurred_at=datetime.now(UTC),
            idempotency_identity=StableId("event.runtime.before-writer.identity"),
            payload_schema_version=VERSION,
            trace_id="runtime-before-writer",
            payload={"source": "runtime"},
        )
    )
    turn_ref = _put(artifacts, {"turn": "settled"})

    first_view = loop._append_and_apply(
        first_request,
        loop._seed(first_request),
        RunEventType.WRITER_TURN_SETTLED,
        SettledArtifactPayload(artifact_ref=turn_ref).model_dump(mode="json"),
        (turn_ref,),
        "writer-turn-0",
    )
    loop._checkpoint(first_view, "writer-turn-0")
    first_checkpoint = checkpoints.latest(
        first_request.run_id,
        logical_stage=f"stage3.context:writer:{first_request.task_id.root}",
    )
    second_view = loop._append_and_apply(
        second_request,
        loop._seed(second_request),
        RunEventType.WRITER_TURN_SETTLED,
        SettledArtifactPayload(artifact_ref=turn_ref).model_dump(mode="json"),
        (turn_ref,),
        "writer-turn-0",
    )
    loop._checkpoint(second_view, "writer-turn-0")
    resumed_first_view = loop._append_and_apply(
        first_request,
        first_view,
        RunEventType.TASK_STARTED,
        {"source": "retry"},
        (),
        "retry-boundary",
    )
    loop._checkpoint(resumed_first_view, "writer-turn-0")
    resumed_first_checkpoint = checkpoints.latest(
        first_request.run_id,
        logical_stage=f"stage3.context:writer:{first_request.task_id.root}",
    )
    second_checkpoint = checkpoints.latest(
        second_request.run_id,
        logical_stage=f"stage3.context:writer:{second_request.task_id.root}",
    )
    assert first_view.basis_event_position == 2
    assert second_view.basis_event_position == 3
    assert resumed_first_view.basis_event_position == 4
    assert first_checkpoint is not None
    assert second_checkpoint is not None
    assert resumed_first_checkpoint is not None
    assert first_checkpoint.checkpoint_id != second_checkpoint.checkpoint_id
    assert first_checkpoint.checkpoint_id != resumed_first_checkpoint.checkpoint_id


@pytest.mark.parametrize(
    "failure",
    (
        TimeoutError("model request timed out"),
        ModelEndpointError("model transport exhausted"),
    ),
)
def test_writer_loop_maps_model_runtime_failures_without_leaking(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
    failure: Exception,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "model-unavailable"))
    request = _request(artifacts, type(failure).__name__.casefold())
    loop, model_request, _loop_artifacts = _loop(
        tmp_path,
        repositories,
        request,
        EditorialVerdict.PASS,
        artifact_repository=artifacts,
    )

    class UnavailableCognition:
        async def create_work_plan(self, *_args: object) -> None:
            raise failure

    loop._cognition = cast(Any, UnavailableCognition())
    result = asyncio.run(loop.execute(request, model_request, cast(Any, object())))

    assert result.status is WritingLoopTerminalStatus.MODEL_UNAVAILABLE
    assert result.failure_detail
    assert {
        request.writing_task_artifact,
        request.accepted_plan.artifact,
        request.writer_context_package_artifact,
        request.recent_prose_context_artifact,
    }.issubset(result.artifacts)


@pytest.mark.parametrize(
    ("route", "expected_repair", "expected_rewrite"),
    (
        (EditorialVerdict.PASS, False, False),
        (EditorialVerdict.LOCAL_REPAIR, True, False),
        (EditorialVerdict.MAJOR_REWRITE, False, True),
    ),
)
def test_real_candidate_loop_closes_all_editor_routes(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
    route: EditorialVerdict,
    expected_repair: bool,
    expected_rewrite: bool,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "request-objects"))
    request = _request(artifacts, route.value.casefold())
    loop, model_request, loop_artifacts = _loop(
        tmp_path,
        repositories,
        request,
        route,
        artifact_repository=artifacts,
    )
    # The DRAFT_READY route never invokes reactive_inputs; passing the typed name documents
    # that this test covers the no-reactive branch without inventing a Memory fixture verdict.
    result = asyncio.run(
        loop.execute(request, model_request, None)  # type: ignore[arg-type]
    )

    assert result.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
    assert (result.repaired_draft is not None) is expected_repair
    assert (result.rewritten_draft is not None) is expected_rewrite
    assert result.final_text_artifact is not None
    assert loop_artifacts.read_verified(result.final_text_artifact).decode("utf-8")
    assert result.observation is not None
    assert result.reconciliation is not None
    assert not result.reconciliation.comparisons
    assert result.context_view is not None
    recent_items = tuple(
        item
        for item in result.context_view.active_memory_items
        if item.kind is ContextItemKind.RECENT_PROSE
    )
    assert tuple(item.mandatory for item in recent_items) == (True, False, False)
    assert request.recent_prose_context.previous_chapter is not None
    previous_text = artifacts.read_verified(
        request.recent_prose_context.previous_chapter.full_text_artifact
    ).decode()
    assert previous_text in recent_items[0].content
    assert result.candidate_only
    assert not result.canon_mutated
    assert not result.memory_patch_generated
    assert not result.commit_called


def test_writer_seed_uses_bounded_evidence_preview_and_renders_typed_gap(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "evidence-first"))
    request = _request(artifacts, "evidence-first")
    task, needs, _units, base_commit = writer_context_inputs()
    text_root = next(
        item for item in make_synthetic_bundle().text_roots if len(item.chapters) == 20
    )
    block = text_root.chapters[-1].scenes[0].blocks[0]
    slice_ = EvidenceSliceResolver().resolve_block(
        block,
        source_commit=base_commit,
        snapshot_id=request.snapshot_id,
        access_scope=needs[0].access_scope,
    )[0]
    assembly = EvidenceFirstWriterContextAssembler().assemble(
        task=task,
        selections=(
            NeedEvidenceSelection(
                need=needs[0],
                selections=(
                    SliceSelectionTrace(
                        slice_id=slice_.slice_id,
                        unit_id=StableId("unit.stage3.evidence-first"),
                        route_channel="r1_exact",
                        fused_rank=1,
                        selection_reason="focused Stage 3 handoff fixture",
                    ),
                ),
                slices=(slice_,),
            ),
        ),
        text_root=text_root,
        basis_commit_id=base_commit,
        basis_snapshot_id=request.snapshot_id,
        arm="A",
    )
    ledger_ref = artifacts.put(
        canonical_json_bytes(assembly.evidence_ledger.model_dump(mode="json")),
        "application/vnd.novel-agent.evidence-ledger-v2+json",
        VERSION,
    )
    assert ledger_ref == assembly.package.evidence_ledger_ref
    evidence_item = assembly.package.items[0]
    preview = slice_.text[: max(1, len(slice_.text) // 2)]
    bounded_item = evidence_item.model_copy(
        update={"raw_preview": preview, "preview_truncated": True}
    )
    gap = EvidenceFirstGap(
        gap_id=StableId("gap.stage3.evidence-first"),
        need_ids=evidence_item.need_ids,
        need_facet_ids=evidence_item.need_facet_ids,
        kind=EvidenceGapKind.BUDGET_EXCEEDED,
        reason="the remaining exact evidence is deferred to reactive Memory",
    )
    gap_item = evidence_item.model_copy(
        update={
            "item_id": StableId("item.stage3.evidence-first.gap"),
            "purpose": "resolve the remaining blocking continuity question",
            "evidence_ledger_ids": (),
            "raw_preview": "",
            "preview_truncated": False,
            "mandatory": True,
            "gap": gap,
        }
    )
    package = assembly.package.model_copy(
        update={
            "items": (bounded_item, gap_item),
            "gaps": (gap,),
            "budget_report": assembly.package.budget_report.model_copy(
                update={"item_count": 2, "evidence_item_count": 1, "gap_item_count": 1}
            ),
        }
    )
    package_ref = _put(artifacts, package)
    evidence_request = request.model_copy(
        update={
            "writer_context_package": package,
            "writer_context_package_artifact": package_ref,
        }
    )
    loop, _model_request, _ = _loop(
        tmp_path,
        repositories,
        evidence_request,
        EditorialVerdict.PASS,
        artifact_repository=artifacts,
    )

    view = loop._seed(evidence_request)

    evidence_items = tuple(
        item for item in view.active_memory_items if item.kind is ContextItemKind.EVIDENCE_HANDLE
    )
    gap_items = tuple(
        item for item in view.active_memory_items if item.kind is ContextItemKind.UNRESOLVED_NEED
    )
    assert len(evidence_items) == 1
    assert preview in evidence_items[0].content
    assert slice_.text not in evidence_items[0].content
    assert "渐进展开" in evidence_items[0].content
    assert evidence_items[0].mandatory
    assert len(gap_items) == 1
    assert gap.reason in gap_items[0].content
    assert gap_items[0].mandatory
    assert set(view.unresolved_need_ids) == set(gap.need_ids)


@pytest.mark.parametrize(
    ("route", "expected"),
    (
        (
            EditorialVerdict.LOCAL_REPAIR,
            WritingLoopTerminalStatus.REVIEW_REQUIRED_LOCAL_REPAIR_EXHAUSTED,
        ),
        (
            EditorialVerdict.MAJOR_REWRITE,
            WritingLoopTerminalStatus.REVIEW_REQUIRED_MAJOR_REWRITE_EXHAUSTED,
        ),
    ),
)
def test_loop_stops_after_one_failed_repair_or_rewrite_review(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
    route: EditorialVerdict,
    expected: WritingLoopTerminalStatus,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / f"request-{route.value}"))
    request = _request(artifacts, f"exhausted-{route.value.casefold()}")
    initial_text = (
        "Lin studies the moonlit groove and opens the gate without using her injured arm."
    )
    responses = _editor_responses(route, initial_text)
    final_review = responses[0]
    if route is EditorialVerdict.LOCAL_REPAIR:
        final_payload = json.loads(final_review)
        final_payload["issues"][0]["evidence_quote"] = "opens it anew"
        final_review = json.dumps(final_payload)
    loop, model_request, _ = _loop(
        tmp_path / route.value,
        repositories,
        request,
        route,
        artifact_repository=artifacts,
        editor_responses=(*responses[:-1], final_review),
    )
    result = asyncio.run(loop.execute(request, model_request, cast(Any, object())))
    assert result.status is expected
    assert result.final_text_artifact is not None


def test_major_rewrite_rejects_another_memory_round(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "rewrite-memory-request"))
    request = _request(artifacts, "rewrite-memory-request")
    loop, model_request, _ = _loop(
        tmp_path,
        repositories,
        request,
        EditorialVerdict.MAJOR_REWRITE,
        artifact_repository=artifacts,
        writer_turns=(_writer_turn("A complete initial draft."), _memory_turn()),
    )
    result = asyncio.run(loop.execute(request, model_request, cast(Any, object())))
    assert result.status is WritingLoopTerminalStatus.WRITER_FAILED
    assert result.failure_detail == "major rewrite cannot start another Memory round"


def test_reconciliation_mismatch_routes_to_review_required(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "reconciliation-review"))
    request = _request(artifacts, "reconciliation-review")
    hint = DeclaredMemoryHint(
        subject_hint="gate",
        change_kind=MemoryHintChangeKind.CHANGE,
        predicate_hint="state",
        value_hint="open",
        evidence_quote="opens the gate",
        confidence=0.9,
    )
    turn = _writer_turn(
        "Lin studies the moonlit groove and opens the gate without using her injured arm."
    ).model_copy(update={"declared_memory_hints": (hint,)})
    loop, model_request, _ = _loop(
        tmp_path,
        repositories,
        request,
        EditorialVerdict.PASS,
        artifact_repository=artifacts,
        writer_turns=(turn,),
    )
    result = asyncio.run(loop.execute(request, model_request, cast(Any, object())))
    assert result.status is WritingLoopTerminalStatus.REVIEW_REQUIRED
    assert result.reconciliation is not None
    assert result.reconciliation.declared_only


def test_reactive_inputs_type_is_public() -> None:
    assert ReactiveMemoryInputs.__module__ == "novel_agent.services.writer_reactive_memory"


def test_post_draft_slice_resumes_editor_and_observer_without_repeating_writer(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "post-draft-resume"))
    request = _request(artifacts, "post-draft-resume")
    initial = request.model_copy(
        update={
            "budgets": request.budgets.model_copy(
                update={"max_post_draft_model_calls": 0}
            )
        }
    )
    loop, model_request, _ = _loop(
        tmp_path,
        repositories,
        initial,
        EditorialVerdict.PASS,
        artifact_repository=artifacts,
    )
    first = asyncio.run(loop.execute(initial, model_request, cast(Any, object())))
    assert first.status is WritingLoopTerminalStatus.YIELDED
    assert first.checkpoint_ref is not None
    checkpoint = WritingLoopCheckpoint.model_validate_json(
        artifacts.read_verified(first.checkpoint_ref)
    )
    assert checkpoint.phase is WritingLoopPhase.EDITOR_PENDING

    resumed = initial.model_copy(
        update={
            "resume_checkpoint_ref": first.checkpoint_ref,
            "budgets": initial.budgets.model_copy(
                update={"max_post_draft_model_calls": 5}
            ),
        }
    )
    second = asyncio.run(loop.execute(resumed, model_request, cast(Any, object())))
    assert second.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
    assert second.initial_draft == first.initial_draft


def test_stage3_public_lazy_exports_are_resolvable() -> None:
    import novel_agent.agents as agents
    import novel_agent.services as services

    assert agents.CandidateObservationAgent is CandidateObservationAgent
    assert agents.CandidateObservationError is CandidateObservationError
    assert services.AgentContextProjector is AgentContextProjector
    assert services.AgentContextRuntime is AgentContextRuntime
    assert services.ContextCompactor is ContextCompactor
    assert services.WriterContextLoopService is WriterContextLoopService
    missing_agent = "NotAnAgent"
    missing_service = "NotAService"
    with pytest.raises(AttributeError):
        getattr(agents, missing_agent)
    with pytest.raises(AttributeError):
        getattr(services, missing_service)


def test_stage3_loop_contracts_reject_invalid_cross_boundary_state(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    request_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "contract-request"))
    request = _request(request_artifacts, "contract-negative")

    with pytest.raises(ValidationError, match="narrative end"):
        WriterContextItem(
            item_id=StableId("context-item.invalid-span"),
            category="memory",
            text="fact",
            source_commit=request.base_commit,
            snapshot_id=request.snapshot_id,
            narrative_start=2,
            narrative_end=1,
        )
    item = WriterContextItem(
        item_id=StableId("context-item.duplicate"),
        category="memory",
        text="fact",
        source_commit=request.base_commit,
        snapshot_id=request.snapshot_id,
    )
    with pytest.raises(ValidationError, match="unique"):
        WriterContextSnapshot(
            context_id=StableId("context.duplicates"),
            base_commit=request.base_commit,
            snapshot_id=request.snapshot_id,
            task_contract="task",
            items=(item, item),
        )
    with pytest.raises(ValidationError, match="Context limits"):
        WritingLoopBudgets(
            context_sequence_limit=10,
            reserved_output_tokens=5,
            context_safety_allowance_tokens=5,
            context_soft_limit_tokens=1,
        )

    request_data = request.model_dump(mode="python")
    bad_request_updates: tuple[tuple[str, dict[str, object]], ...] = (
        ("Writer mode", {"mode": AgentMode.REVIEW}),
        ("unique", {"allowed_skills": (request.allowed_skills[0],) * 2}),
        (
            "another WritingTask",
            {
                "accepted_plan": request.accepted_plan.model_copy(
                    update={"task_contract_id": StableId("writing-contract.other")}
                )
            },
        ),
        (
            "share a basis",
            {
                "accepted_plan": request.accepted_plan.model_copy(
                    update={"snapshot_id": StableId("snapshot.other")}
                )
            },
        ),
        (
            "target range",
            {"writing_task": request.writing_task.model_copy(update={"target_chapter": 999})},
        ),
    )
    for message, update in bad_request_updates:
        with pytest.raises(ValidationError, match=message):
            WritingLoopRequest.model_validate(request_data | update)

    plan = _work_plan(request)
    with pytest.raises(ValidationError, match="unique"):
        WriterWorkPlan.model_validate(
            plan.model_dump(mode="python")
            | {"selected_skill_ids": (request.allowed_skills[0],) * 2}
        )
    with pytest.raises(ValidationError, match="unselected"):
        WriterWorkPlan.model_validate(
            plan.model_dump(mode="python")
            | {"expected_skill_checkpoints": {"skill.not-selected": ("x",)}}
        )

    memory_turn = _memory_turn()
    memory_request = memory_turn.memory_requests[0]
    invalid_turn_updates: tuple[tuple[dict[str, object], str], ...] = (
        ({"draft_text": "also a draft"}, "only memory_requests"),
        ({"memory_requests": ()}, "only memory_requests"),
        ({"memory_requests": (memory_request, memory_request)}, "unique"),
    )
    for update, message in invalid_turn_updates:
        with pytest.raises(ValidationError, match=message):
            WriterTurnOutput.model_validate(memory_turn.model_dump(mode="python") | update)
    with pytest.raises(ValidationError, match="only draft_text"):
        WriterTurnOutput.model_validate(
            _writer_turn("draft").model_dump(mode="python") | {"memory_requests": (memory_request,)}
        )

    loop, model_request, _ = _loop(
        tmp_path / "contract-loop",
        repositories,
        request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
    )
    result = asyncio.run(loop.execute(request, model_request, cast(Any, object())))
    assert result.work_plan is not None
    with pytest.raises(ValidationError, match="exactly cover"):
        type(result.work_plan).model_validate(
            result.work_plan.model_dump(mode="python") | {"skill_receipts": ()}
        )

    ready = result.model_dump(mode="python")
    invalid_results: tuple[tuple[str, dict[str, object]], ...] = (
        ("complete candidate", {"initial_draft": None}),
        ("pass final review", {"editorial_reports": ()}),
        (
            "failure detail",
            {
                "status": WritingLoopTerminalStatus.EDITOR_FAILED,
                "failure_detail": None,
            },
        ),
        (
            "appear together",
            {
                "status": WritingLoopTerminalStatus.OBSERVER_FAILED,
                "failure_detail": "observer failed",
                "observation_artifact": None,
            },
        ),
        (
            "unique",
            {"model_call_records": result.model_call_records[:1] * 2},
        ),
    )
    for message, update in invalid_results:
        with pytest.raises(ValidationError, match=message):
            WritingLoopResult.model_validate(ready | update)


def test_writer_candidate_rejects_non_draft_and_invalid_parent_rules(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "candidate-errors"))
    materializer = WriterCandidateMaterializer(artifacts, VERSION)
    request = _request(artifacts, "candidate-errors")
    memory_turn = SimpleNamespace(
        output=WriterTurnOutput(
            action=WriterTurnAction.REQUEST_MEMORY,
            memory_requests=(
                WriterMemoryRequest(
                    request_id=StableId("memory.candidate-errors"),
                    question="What is Lin's current injury?",
                    purpose="continuity",
                    blocked_action="open the gate",
                    requested_evidence_type="current state",
                    scene_or_draft_checkpoint="gate",
                    risk="injury",
                ),
            ),
            work_plan_checkpoint="blocked",
        )
    )
    with pytest.raises(WriterCandidateError, match="DRAFT_READY"):
        materializer.materialize(
            request,
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, memory_turn),
            mode=request.mode,
        )
    draft_turn = SimpleNamespace(output=_writer_turn("A valid candidate draft."))
    with pytest.raises(WriterCandidateError, match="cannot have a parent"):
        materializer.materialize(
            request,
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, draft_turn),
            mode=request.mode,
            parent_draft=cast(Any, object()),
        )
    with pytest.raises(WriterCandidateError, match="requires a parent"):
        materializer.materialize(
            request,
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, draft_turn),
            mode=request.mode.MAJOR_REWRITE,
        )


def test_observer_and_cognition_require_admission_and_fail_closed(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "agent-errors"))
    request = _request(artifacts, "agent-errors")
    model_request = ModelRequest(
        request_id=StableId("request.stage3.agent-errors"),
        run_id=request.run_id,
        task_id=request.task_id,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace.stage3.agent-errors",
        prompt="replaced",
    )
    gateway = _gateway(FakeModelEndpoint("{}"), "stage3-agent-errors")
    with pytest.raises(CandidateObservationError, match="admission"):
        CandidateObservationAgent(gateway, artifacts)
    with pytest.raises(WriterCognitionError, match="admission"):
        WriterCognitionService(gateway, artifacts, SkillRegistry(()))

    observer = CandidateObservationAgent(
        _gateway(
            FakeModelEndpoint(
                CuratorObservation(draft_id=ArtifactId("sha256:" + "2" * 64)).model_dump_json()
            ),
            "stage3-observer-errors",
        ),
        artifacts,
        require_admission=False,
    )
    blank = artifacts.put(b"   ", "text/plain", VERSION)
    with pytest.raises(CandidateObservationError, match="blank"):
        asyncio.run(
            observer.observe(
                ArtifactId("sha256:" + "1" * 64),
                blank,
                ArtifactId("sha256:" + "3" * 64),
                model_request,
            )
        )
    text = artifacts.put(b"candidate", "text/plain", VERSION)
    with pytest.raises(CandidateObservationError, match="another Draft"):
        asyncio.run(
            observer.observe(
                ArtifactId("sha256:" + "1" * 64),
                text,
                ArtifactId("sha256:" + "3" * 64),
                model_request,
            )
        )


def test_writer_cognition_rejects_untrusted_plan_skill_context_and_memory_output(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "cognition-errors"))
    request = _request(artifacts, "cognition-errors")

    def service(
        responses: tuple[str, ...],
        *,
        skill_path: Path = PACKAGE_ROOT / "skills" / "scene_composition_v1.md",
    ) -> tuple[WriterCognitionService, ModelGateway]:
        gateway = _gateway(SequenceEndpoint(responses), "stage3-cognition-errors")
        contract = next(
            item
            for item in WriterCognitionService.skill_contracts()
            if item.contract_id == StableId("skill.scene-composition")
        )
        registry = SkillRegistry(
            (
                SkillTemplate(
                    skill_id=contract.contract_id,
                    version=contract.version,
                    path=skill_path,
                    expected_hash=content_hash(skill_path.read_bytes()),
                ),
            )
        )
        return (
            WriterCognitionService(
                gateway,
                artifacts,
                registry,
                require_admission=False,
            ),
            gateway,
        )

    loop, model_request, _ = _loop(
        tmp_path / "seed",
        repositories,
        request,
        EditorialVerdict.PASS,
        artifact_repository=artifacts,
    )
    plan_model_request = model_request.model_copy(
        update={"request_id": StableId("request.cognition.work-plan")}
    )
    turn_model_request = model_request.model_copy(
        update={"request_id": StableId("request.cognition.writer-turn")}
    )
    view = loop._seed(request)
    policy = ContextWindowPolicy(
        sequence_limit=request.budgets.context_sequence_limit,
        reserved_output_tokens=request.budgets.reserved_output_tokens,
        safety_allowance_tokens=request.budgets.context_safety_allowance_tokens,
        soft_limit_tokens=request.budgets.context_soft_limit_tokens,
        tokenizer=request.writer_context_package.budget_report.tokenizer,
        tokenizer_version=request.writer_context_package.budget_report.tokenizer_version,
    )
    receipt = loop._compactor.provider_receipt(view, policy)
    valid_view = view.model_copy(update={"provider_validity_receipt": receipt})
    plan = _work_plan(request)

    cognition, _ = service((plan.model_dump_json(),))
    unknown_request = request.model_copy(
        update={"allowed_skills": (StableId("skill.not-registered"),)}
    )
    with pytest.raises(WriterCognitionError, match="unregistered"):
        asyncio.run(cognition.create_work_plan(unknown_request, valid_view, plan_model_request))
    with pytest.raises(WriterCognitionError, match="basis"):
        asyncio.run(
            cognition.create_work_plan(
                request,
                valid_view.model_copy(update={"task_id": TaskId("task.other")}),
                plan_model_request,
            )
        )
    with pytest.raises(WriterCognitionError, match="provider-valid"):
        asyncio.run(cognition.create_work_plan(request, view, plan_model_request))

    wrong_ref = artifacts.put(b"other-plan", "application/json", VERSION)
    wrong_lineage = plan.model_copy(update={"accepted_plan_ref": wrong_ref})
    cognition, _ = service((wrong_lineage.model_dump_json(),))
    with pytest.raises(WriterCognitionError, match="lineage"):
        asyncio.run(cognition.create_work_plan(request, valid_view, plan_model_request))

    outside = plan.model_copy(
        update={
            "selected_skill_ids": (StableId("skill.continuation"),),
            "expected_skill_checkpoints": {},
        }
    )
    cognition, _ = service((outside.model_dump_json(),))
    with pytest.raises(WriterCognitionError, match="outside"):
        asyncio.run(cognition.create_work_plan(request, valid_view, plan_model_request))

    cognition, _ = service(
        (plan.model_dump_json(),),
        skill_path=PACKAGE_ROOT / "skills" / "continuation_v1.md",
    )
    with pytest.raises(WriterCognitionError, match="hash mismatch"):
        asyncio.run(cognition.create_work_plan(request, valid_view, plan_model_request))

    too_many = WriterTurnOutput(
        action=WriterTurnAction.REQUEST_MEMORY,
        memory_requests=tuple(
            WriterMemoryRequest(
                request_id=StableId(f"memory.cognition.{index}"),
                question=f"Question {index}?",
                purpose="continuity",
                blocked_action="write scene",
                requested_evidence_type="current state",
                scene_or_draft_checkpoint="scene",
                risk="continuity",
            )
            for index in range(request.budgets.max_memory_questions + 1)
        ),
        work_plan_checkpoint="blocked",
    )
    cognition, _ = service((plan.model_dump_json(), too_many.model_dump_json()))
    plan_result = asyncio.run(cognition.create_work_plan(request, valid_view, plan_model_request))
    with pytest.raises(WriterCognitionError, match="question budget"):
        asyncio.run(cognition.take_turn(request, valid_view, plan_result, turn_model_request))

    nonvisible = WriterTurnOutput(
        action=WriterTurnAction.REQUEST_MEMORY,
        memory_requests=(
            _memory_turn()
            .memory_requests[0]
            .model_copy(update={"known_context_item_ids": (StableId("context.not-visible"),)}),
        ),
        work_plan_checkpoint="blocked",
    )
    cognition, _ = service((plan.model_dump_json(), nonvisible.model_dump_json()))
    plan_result = asyncio.run(cognition.create_work_plan(request, valid_view, plan_model_request))
    with pytest.raises(WriterCognitionError, match="non-visible"):
        asyncio.run(cognition.take_turn(request, valid_view, plan_result, turn_model_request))

    cognition, gateway = service(
        (plan.model_dump_json(), _writer_turn("A complete draft.").model_dump_json())
    )
    plan_result = asyncio.run(cognition.create_work_plan(request, valid_view, plan_model_request))
    gateway.raw_responses = DiscardingRawResponses()
    with pytest.raises(WriterCognitionError, match="raw response"):
        asyncio.run(cognition.take_turn(request, valid_view, plan_result, turn_model_request))

    changed_request = request.model_copy(
        update={"allowed_skills": (StableId("skill.continuation"),)}
    )
    with pytest.raises(WriterCognitionError, match="no longer allowed"):
        asyncio.run(
            cognition.take_turn(changed_request, valid_view, plan_result, turn_model_request)
        )

    changed_skill_cognition, _ = service(
        (_writer_turn("unused").model_dump_json(),),
        skill_path=PACKAGE_ROOT / "skills" / "continuation_v1.md",
    )
    with pytest.raises(WriterCognitionError, match="hash mismatch"):
        asyncio.run(
            changed_skill_cognition.take_turn(
                request,
                valid_view,
                plan_result,
                turn_model_request,
            )
        )


def _memory_turn() -> WriterTurnOutput:
    return WriterTurnOutput(
        action=WriterTurnAction.REQUEST_MEMORY,
        memory_requests=(
            WriterMemoryRequest(
                request_id=StableId("memory.loop.injury"),
                question="What is Lin's current injury?",
                purpose="continuity",
                blocked_action="open the gate",
                requested_evidence_type="current state",
                scene_or_draft_checkpoint="gate",
                risk="injury",
            ),
        ),
        work_plan_checkpoint="blocked",
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (ContextDeltaStatus.DENIED, WritingLoopTerminalStatus.MEMORY_DENIED),
        (ContextDeltaStatus.INSUFFICIENT, WritingLoopTerminalStatus.MEMORY_INSUFFICIENT),
        (
            ContextDeltaStatus.BUDGET_EXHAUSTED,
            WritingLoopTerminalStatus.MEMORY_BUDGET_EXHAUSTED,
        ),
    ),
)
def test_loop_maps_terminal_memory_outcomes(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
    status: ContextDeltaStatus,
    expected: WritingLoopTerminalStatus,
) -> None:
    request_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "request-memory"))
    request = _request(request_artifacts, status.value.casefold())
    loop, model_request, artifacts = _loop(
        tmp_path,
        repositories,
        request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
        writer_turns=(_memory_turn(),),
    )

    class TerminalReactive:
        def resolve(
            self,
            loop_request: WritingLoopRequest,
            view: object,
            *_args: object,
            **_kwargs: object,
        ) -> ReactiveMemoryResult:
            request_ref = artifacts.put(b"request", "application/json", VERSION)
            resolution_ref = artifacts.put(b"resolution", "application/json", VERSION)
            return ReactiveMemoryResult(
                delta=ContextDelta(
                    delta_id=StableId(f"delta.loop.{status.value.casefold()}"),
                    request_ref=request_ref,
                    resolution_ref=resolution_ref,
                    parent_view_revision=cast(Any, view).revision,
                    base_commit=loop_request.base_commit,
                    snapshot_id=loop_request.snapshot_id,
                    profile_ref=loop_request.project_profile_artifact,
                    plan_ref=loop_request.accepted_plan.artifact,
                    unresolved_need_ids=(StableId("memory.loop.injury"),),
                    token_impact=0,
                    status=status,
                ),
                request_fingerprint=ArtifactId("sha256:" + "7" * 64),
                needs=(),
            )

    loop._reactive = cast(Any, TerminalReactive())
    result = asyncio.run(loop.execute(request, model_request, cast(Any, object())))
    assert result.status is expected
    assert len(result.context_deltas) == 1
    assert (result.checkpoint_ref is not None) is (
        status is ContextDeltaStatus.BUDGET_EXHAUSTED
    )


def test_loop_maps_preflight_context_and_downstream_failures(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    request_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "request-failures"))

    basis_request = _request(request_artifacts, "basis")
    basis_loop, model_request, _ = _loop(
        tmp_path / "basis",
        repositories,
        basis_request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
    )
    wrong_model = model_request.model_copy(update={"task_id": TaskId("task.wrong")})
    assert (
        asyncio.run(basis_loop.execute(basis_request, wrong_model, cast(Any, object()))).status
        is WritingLoopTerminalStatus.BASIS_CHANGED
    )

    limit_request = _request(request_artifacts, "limit").model_copy(
        update={
            "budgets": WritingLoopBudgets(
                context_sequence_limit=100,
                reserved_output_tokens=1,
                context_safety_allowance_tokens=1,
                context_soft_limit_tokens=98,
            )
        }
    )
    limit_loop, limit_model, _ = _loop(
        tmp_path / "limit",
        repositories,
        limit_request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
    )
    assert (
        asyncio.run(limit_loop.execute(limit_request, limit_model, cast(Any, object()))).status
        is WritingLoopTerminalStatus.CONTEXT_LIMIT
    )

    class FailingCognition:
        async def create_work_plan(self, *_args: object) -> None:
            raise WriterCognitionError("writer failed")

    writer_request = _request(request_artifacts, "writer-failure")
    writer_loop, writer_model, _ = _loop(
        tmp_path / "writer",
        repositories,
        writer_request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
    )
    writer_loop._cognition = cast(Any, FailingCognition())
    assert (
        asyncio.run(writer_loop.execute(writer_request, writer_model, cast(Any, object()))).status
        is WritingLoopTerminalStatus.WRITER_FAILED
    )

    class FailingEditor:
        async def review(self, *_args: object) -> None:
            raise EditorialReviewError("editor failed")

    editor_request = _request(request_artifacts, "editor-failure")
    editor_loop, editor_model, _ = _loop(
        tmp_path / "editor",
        repositories,
        editor_request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
    )
    editor_loop._editorial = cast(Any, FailingEditor())
    assert (
        asyncio.run(editor_loop.execute(editor_request, editor_model, cast(Any, object()))).status
        is WritingLoopTerminalStatus.EDITOR_FAILED
    )

    class FailingObserver:
        async def observe(self, *_args: object) -> None:
            raise CandidateObservationError("observer failed")

    observer_request = _request(request_artifacts, "observer-failure")
    observer_loop, observer_model, _ = _loop(
        tmp_path / "observer",
        repositories,
        observer_request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
    )
    observer_loop._observer = cast(Any, FailingObserver())
    assert (
        asyncio.run(
            observer_loop.execute(observer_request, observer_model, cast(Any, object()))
        ).status
        is WritingLoopTerminalStatus.OBSERVER_FAILED
    )

    class FailingReconciliation:
        def reconcile(self, *_args: object) -> None:
            raise ReconciliationError("reconciliation failed")

    reconciliation_request = _request(request_artifacts, "reconciliation-failure")
    reconciliation_loop, reconciliation_model, _ = _loop(
        tmp_path / "reconciliation",
        repositories,
        reconciliation_request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
    )
    reconciliation_loop._reconciliation = cast(Any, FailingReconciliation())
    assert (
        asyncio.run(
            reconciliation_loop.execute(
                reconciliation_request,
                reconciliation_model,
                cast(Any, object()),
            )
        ).status
        is WritingLoopTerminalStatus.RECONCILIATION_FAILED
    )


def test_loop_maps_turn_memory_materialization_and_repair_failures(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    request_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "layer-failures"))

    class FailTurn:
        def __init__(self, delegate: object) -> None:
            self._delegate = delegate

        async def create_work_plan(self, *args: object) -> object:
            return await cast(Any, self._delegate).create_work_plan(*args)

        async def take_turn(self, *_args: object) -> None:
            raise WriterCognitionError("turn failed")

    turn_request = _request(request_artifacts, "turn-failed")
    turn_loop, turn_model, _ = _loop(
        tmp_path / "turn-failed",
        repositories,
        turn_request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
    )
    turn_loop._cognition = cast(Any, FailTurn(turn_loop._cognition))
    assert (
        asyncio.run(turn_loop.execute(turn_request, turn_model, cast(Any, object()))).status
        is WritingLoopTerminalStatus.WRITER_FAILED
    )

    context_request = _request(request_artifacts, "turn-context-limit")
    context_loop, context_model, _ = _loop(
        tmp_path / "turn-context-limit",
        repositories,
        context_request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
    )
    real_ensure = context_loop._ensure_dispatch
    dispatch_count = 0

    def fail_second_dispatch(*args: object) -> object:
        nonlocal dispatch_count
        dispatch_count += 1
        if dispatch_count == 2:
            raise ContextLimitError("turn dispatch overflow")
        return real_ensure(*cast(Any, args))

    cast(Any, context_loop)._ensure_dispatch = fail_second_dispatch
    assert (
        asyncio.run(
            context_loop.execute(context_request, context_model, cast(Any, object()))
        ).status
        is WritingLoopTerminalStatus.CONTEXT_LIMIT
    )

    exhausted_request = _request(request_artifacts, "memory-exhausted").model_copy(
        update={
            "budgets": _request(request_artifacts, "memory-budget-source").budgets.model_copy(
                update={"max_reactive_memory_rounds": 0}
            )
        }
    )
    exhausted_loop, exhausted_model, _ = _loop(
        tmp_path / "memory-exhausted",
        repositories,
        exhausted_request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
        writer_turns=(_memory_turn(),),
    )
    assert (
        asyncio.run(
            exhausted_loop.execute(exhausted_request, exhausted_model, cast(Any, object()))
        ).status
        is WritingLoopTerminalStatus.MEMORY_INSUFFICIENT
    )

    class FailReactive:
        def resolve(self, *_args: object, **_kwargs: object) -> None:
            raise WriterReactiveMemoryError("reactive failed")

    reactive_request = _request(request_artifacts, "reactive-failed")
    reactive_loop, reactive_model, _ = _loop(
        tmp_path / "reactive-failed",
        repositories,
        reactive_request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
        writer_turns=(_memory_turn(),),
    )
    reactive_loop._reactive = cast(Any, FailReactive())
    assert (
        asyncio.run(
            reactive_loop.execute(reactive_request, reactive_model, cast(Any, object()))
        ).status
        is WritingLoopTerminalStatus.MEMORY_DENIED
    )

    class FailMaterializer:
        def materialize(self, *_args: object, **_kwargs: object) -> None:
            raise WriterCandidateError("materialization failed")

    material_request = _request(request_artifacts, "material-failed")
    material_loop, material_model, _ = _loop(
        tmp_path / "material-failed",
        repositories,
        material_request,
        EditorialVerdict.PASS,
        artifact_repository=request_artifacts,
    )
    material_loop._materializer = cast(Any, FailMaterializer())
    assert (
        asyncio.run(
            material_loop.execute(material_request, material_model, cast(Any, object()))
        ).status
        is WritingLoopTerminalStatus.WRITER_FAILED
    )

    repair_request = _request(request_artifacts, "repair-failed")
    initial_text = (
        "Lin studies the moonlit groove and opens the gate without using her injured arm."
    )
    repair_responses = _editor_responses(EditorialVerdict.LOCAL_REPAIR, initial_text)
    repair_loop, repair_model, _ = _loop(
        tmp_path / "repair-failed",
        repositories,
        repair_request,
        EditorialVerdict.LOCAL_REPAIR,
        artifact_repository=request_artifacts,
        editor_responses=(*repair_responses[:-1], "{}"),
    )
    assert (
        asyncio.run(repair_loop.execute(repair_request, repair_model, cast(Any, object()))).status
        is WritingLoopTerminalStatus.EDITOR_FAILED
    )


def test_loop_publishes_compaction_rejects_invalid_provider_and_replays_event(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "dispatch-request"))
    request = _request(artifacts, "dispatch")
    loop, _model_request, _ = _loop(
        tmp_path,
        repositories,
        request,
        EditorialVerdict.PASS,
        artifact_repository=artifacts,
    )
    seed = loop._seed(request)
    generous = ContextWindowPolicy(
        sequence_limit=100_000,
        reserved_output_tokens=0,
        safety_allowance_tokens=0,
        soft_limit_tokens=99_000,
        tokenizer="test",
        tokenizer_version="v1",
    )
    rendered = loop._compactor.pressure(seed, generous).rendered_input_tokens
    compacting_policy = ContextWindowPolicy(
        sequence_limit=rendered + 100,
        reserved_output_tokens=0,
        safety_allowance_tokens=0,
        soft_limit_tokens=rendered - 1,
        tokenizer="test",
        tokenizer_version="v1",
    )
    compacted, receipt = loop._ensure_dispatch(request, seed, compacting_policy)
    assert receipt is not None
    assert compacted.generation == 1

    original = loop._compactor

    class InvalidProviderCompactor:
        def pressure(self, view: object, policy: object) -> object:
            return original.pressure(cast(Any, view), cast(Any, policy))

        def provider_receipt(self, view: object, policy: object) -> object:
            valid = original.provider_receipt(cast(Any, view), cast(Any, policy))
            return valid.model_copy(
                update={"provider_valid": False, "rendered_input_tokens": 100_001}
            )

    loop._compactor = cast(Any, InvalidProviderCompactor())
    with pytest.raises(ContextLimitError, match="dispatch Gate"):
        loop._ensure_dispatch(request, seed, generous)
    loop._compactor = original

    replay_request = request.model_copy(update={"run_id": RunId("run.stage3-loop.replay")})
    replay_seed = loop._seed(replay_request)
    payload: JsonValue = {"done": True}
    first = loop._append_and_apply(
        replay_request,
        replay_seed,
        RunEventType.TASK_COMPLETED,
        payload,
        (),
        "replay",
    )
    replayed = loop._append_and_apply(
        replay_request,
        replay_seed,
        RunEventType.TASK_COMPLETED,
        payload,
        (),
        "replay",
    )
    assert replayed == first

    no_op_request = request.model_copy(update={"run_id": RunId("run.stage3-loop.soft-no-op")})
    no_op_seed = loop._seed(no_op_request).model_copy(update={"active_memory_items": ()})
    no_op_tokens = original.pressure(no_op_seed, generous).rendered_input_tokens
    no_op_policy = ContextWindowPolicy(
        sequence_limit=no_op_tokens + 100,
        reserved_output_tokens=0,
        safety_allowance_tokens=0,
        soft_limit_tokens=no_op_tokens - 1,
        tokenizer="test",
        tokenizer_version="v1",
    )
    unchanged, no_receipt = loop._ensure_dispatch(no_op_request, no_op_seed, no_op_policy)
    assert no_receipt is None
    assert unchanged.provider_validity_receipt is not None


@pytest.mark.parametrize("route", (EditorialVerdict.PASS, EditorialVerdict.MAJOR_REWRITE))
def test_loop_retains_compaction_receipts_across_writer_dispatches(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
    route: EditorialVerdict,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / f"receipt-{route.value}"))
    request = _request(artifacts, f"receipt-{route.value.casefold()}")
    loop, model_request, _ = _loop(
        tmp_path / route.value,
        repositories,
        request,
        route,
        artifact_repository=artifacts,
    )
    seed = loop._seed(request)
    broad = ContextWindowPolicy(
        sequence_limit=100_000,
        reserved_output_tokens=0,
        safety_allowance_tokens=0,
        soft_limit_tokens=99_000,
        tokenizer="test",
        tokenizer_version="v1",
    )
    rendered = loop._compactor.pressure(seed, broad).rendered_input_tokens
    compacting = ContextWindowPolicy(
        sequence_limit=rendered + 100,
        reserved_output_tokens=0,
        safety_allowance_tokens=0,
        soft_limit_tokens=rendered - 1,
        tokenizer="test",
        tokenizer_version="v1",
    )
    _compacted, receipt = loop._compactor.compact(seed, compacting, hard=False)
    assert receipt is not None
    real_compactor = loop._compactor

    def dispatch_with_receipt(
        _request_value: WritingLoopRequest,
        view: object,
        policy: ContextWindowPolicy,
    ) -> tuple[object, object]:
        typed_view = cast(Any, view)
        provider = real_compactor.provider_receipt(typed_view, policy)
        return typed_view.model_copy(update={"provider_validity_receipt": provider}), receipt

    cast(Any, loop)._ensure_dispatch = dispatch_with_receipt
    result = asyncio.run(loop.execute(request, model_request, cast(Any, object())))
    assert result.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
    expected_minimum = 3 if route is EditorialVerdict.MAJOR_REWRITE else 2
    assert len(result.compaction_receipts) >= expected_minimum


def test_resolved_reactive_delta_rebuilds_context_and_resumes_writer(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "resolved-reactive"))
    request = _request(artifacts, "resolved-reactive")
    loop, model_request, _ = _loop(
        tmp_path,
        repositories,
        request,
        EditorialVerdict.PASS,
        artifact_repository=artifacts,
        writer_turns=(_memory_turn(), _writer_turn("A resumed complete candidate draft.")),
    )

    class ResolvedReactive:
        def resolve(
            self, loop_request: WritingLoopRequest, view: object, *_args: object, **_kwargs: object
        ) -> ReactiveMemoryResult:
            typed_view = cast(Any, view)
            request_ref = artifacts.put(b"request", "application/json", VERSION)
            resolution_ref = artifacts.put(b"resolution", "application/json", VERSION)
            evidence_ref = artifacts.put(b"evidence", "application/json", VERSION)
            item = ContextViewItem(
                item_id=StableId("context-memory.resolved-reactive"),
                layer=ContextLayer.MEMORY,
                kind=ContextItemKind.MEMORY_CLAIM,
                content="Lin's arm remains injured.",
                token_count=6,
                source_artifact_refs=(evidence_ref,),
                information_scope="writer_safe",
            )
            return ReactiveMemoryResult(
                delta=ContextDelta(
                    delta_id=StableId("context-delta.resolved-reactive"),
                    request_ref=request_ref,
                    resolution_ref=resolution_ref,
                    parent_view_revision=typed_view.revision,
                    base_commit=loop_request.base_commit,
                    snapshot_id=loop_request.snapshot_id,
                    profile_ref=loop_request.project_profile_artifact,
                    plan_ref=loop_request.accepted_plan.artifact,
                    added_memory_items=(item,),
                    resolved_need_ids=(StableId("need.resolved-reactive"),),
                    evidence_refs=(evidence_ref,),
                    token_impact=item.token_count,
                    status=ContextDeltaStatus.RESOLVED,
                ),
                request_fingerprint=ArtifactId("sha256:" + "6" * 64),
                needs=(),
            )

    loop._reactive = cast(Any, ResolvedReactive())
    result = asyncio.run(loop.execute(request, model_request, cast(Any, object())))
    assert result.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
    assert result.context_deltas[0].parent_view_revision > 0


def test_reactive_writer_yields_and_resumes_without_repeating_settled_model_work(
    tmp_path: Path,
    repositories: tuple[RunEventLogRepository, RunCheckpointRepository],
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "reactive-resume"))
    request = _request(artifacts, "reactive-resume")
    loop, model_request, _ = _loop(
        tmp_path,
        repositories,
        request,
        EditorialVerdict.PASS,
        artifact_repository=artifacts,
        writer_turns=(
            _memory_turn(),
            _memory_turn(),
            _writer_turn("Lin uses the newly recalled detail and completes the chapter."),
        ),
    )

    class CountingCognition:
        def __init__(self, delegate: object) -> None:
            self.delegate = cast(Any, delegate)
            self.work_plan_calls = 0
            self.writer_turn_calls = 0

        async def create_work_plan(self, *args: object, **kwargs: object) -> object:
            self.work_plan_calls += 1
            return await self.delegate.create_work_plan(*args, **kwargs)

        async def take_turn(self, *args: object, **kwargs: object) -> object:
            self.writer_turn_calls += 1
            return await self.delegate.take_turn(*args, **kwargs)

    class TwoResolvedMemoryRounds:
        def __init__(self) -> None:
            self.calls = 0

        def resolve(
            self,
            loop_request: WritingLoopRequest,
            view: object,
            *_args: object,
            **_kwargs: object,
        ) -> ReactiveMemoryResult:
            self.calls += 1
            typed_view = cast(Any, view)
            suffix = str(self.calls)
            request_ref = artifacts.put(
                f"request-{suffix}".encode(), "application/json", VERSION
            )
            resolution_ref = artifacts.put(
                f"resolution-{suffix}".encode(), "application/json", VERSION
            )
            evidence_ref = artifacts.put(
                f"evidence-{suffix}".encode(), "application/json", VERSION
            )
            item = ContextViewItem(
                item_id=StableId(f"context-memory.reactive-resume.{suffix}"),
                layer=ContextLayer.MEMORY,
                kind=ContextItemKind.MEMORY_CLAIM,
                content=f"Resolved continuity fact {suffix}.",
                token_count=5,
                source_artifact_refs=(evidence_ref,),
                information_scope="writer_safe",
            )
            return ReactiveMemoryResult(
                delta=ContextDelta(
                    delta_id=StableId(f"context-delta.reactive-resume.{suffix}"),
                    request_ref=request_ref,
                    resolution_ref=resolution_ref,
                    parent_view_revision=typed_view.revision,
                    base_commit=loop_request.base_commit,
                    snapshot_id=loop_request.snapshot_id,
                    profile_ref=loop_request.project_profile_artifact,
                    plan_ref=loop_request.accepted_plan.artifact,
                    added_memory_items=(item,),
                    resolved_need_ids=(StableId(f"need.reactive-resume.{suffix}"),),
                    evidence_refs=(evidence_ref,),
                    token_impact=item.token_count,
                    status=ContextDeltaStatus.RESOLVED,
                ),
                request_fingerprint=ArtifactId("sha256:" + f"{self.calls:064x}"),
                needs=(),
            )

    cognition = CountingCognition(loop._cognition)
    memory = TwoResolvedMemoryRounds()
    loop._cognition = cast(Any, cognition)
    loop._reactive = cast(Any, memory)

    first = asyncio.run(loop.execute(request, model_request, cast(Any, object())))

    assert first.status is WritingLoopTerminalStatus.YIELDED
    assert first.checkpoint_ref is not None
    checkpoint = WritingLoopCheckpoint.model_validate_json(
        artifacts.read_verified(first.checkpoint_ref)
    )
    assert checkpoint.memory_rounds == 1
    assert checkpoint.writer_turns == 2
    assert cognition.work_plan_calls == 1
    assert cognition.writer_turn_calls == 2

    resumed_request = request.model_copy(
        update={"resume_checkpoint_ref": first.checkpoint_ref}
    )
    resumed = asyncio.run(
        loop.execute(resumed_request, model_request, cast(Any, object()))
    )

    assert resumed.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
    assert cognition.work_plan_calls == 1
    assert cognition.writer_turn_calls == 3
    assert memory.calls == 2


def test_reactive_memory_is_bounded_deduplicated_and_gateway_owned(tmp_path: Path) -> None:
    base_memory_request = _memory_turn().memory_requests[0]
    for evidence, scope, facet in (
        ("causal history", "historical", "CAUSAL_HISTORY"),
        ("knowledge disclosure", "knowledge", "KNOWLEDGE_BOUNDARY"),
        ("relationship emotion", "current", "RELATION_STATE"),
        ("current object state", "current", "CURRENT_STATE"),
    ):
        draft = WriterReactiveNeedAdapter._draft(
            0,
            base_memory_request.model_copy(update={"requested_evidence_type": evidence}),
            21,
            "enter tower",
        )
        assert draft.required_claim_scopes == (scope,)
        assert draft.suggested_facets == (facet,)

    class BlockedGateway:
        calls = 0

        def resolve(self, *_args: object, **_kwargs: object) -> None:
            self.calls += 1
            raise MemoryGatewayBlockedError("policy denied")

    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "reactive-objects"))
    request = _request(artifacts, "reactive")
    projector = AgentContextProjector(lambda value: max(1, len(value) // 4))
    view = projector.seed_writer(
        run_id=request.run_id,
        task_id=request.task_id,
        package=request.writer_context_package,
        seed_package_ref=request.writer_context_package_artifact,
        profile_ref=request.project_profile_artifact,
        plan_ref=request.accepted_plan.artifact,
        protected_items=(
            ContextViewItem(
                item_id=StableId("context-protected.reactive-task"),
                layer=ContextLayer.PROTECTED,
                kind=ContextItemKind.WRITING_TASK,
                content=request.writing_task.model_dump_json(),
                token_count=10,
                mandatory=True,
                information_scope="writer_safe",
            ),
        ),
    )
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    focus = TaskFocusExtractor().extract(
        request.writer_context_package.task_contract,
        world,
        plan,
    )
    template = memory_resolution_request().model_copy(
        update={
            "base_commit": request.base_commit,
            "snapshot_id": request.snapshot_id,
            "access_scope": AccessScope.WRITER_SAFE,
            "allow_future_plan": False,
        }
    )
    inputs = ReactiveMemoryInputs(
        task=request.writer_context_package.task_contract,
        world=world,
        plan=plan,
        focus_set=focus,
        text_root=bundle.text_roots[0],
        resolution_template=template,
        thread_id="stage3-reactive",
    )
    question = WriterMemoryRequest(
        request_id=StableId("writer-memory.reactive.injury"),
        question="林澈的伤势现在是否仍未痊愈?",
        purpose="preserve current-state continuity",
        blocked_action="describe Lin opening the gate",
        requested_evidence_type="current state",
        scene_or_draft_checkpoint="before opening the gate",
        risk="injury continuity",
        mandatory_suggestion=True,
        anchor_labels=("林澈",),
    )
    blocked = BlockedGateway()
    adapter = WriterReactiveNeedAdapter(
        cast(Any, blocked),
        artifacts,
        lambda value: max(1, len(value) // 4),
        schema_version=VERSION,
    )

    denied = adapter.resolve(request, view, (question,), inputs)
    assert denied.delta.status.value == "DENIED"
    assert blocked.calls == 1
    duplicate = adapter.resolve(
        request,
        view,
        (question,),
        inputs,
        seen_fingerprints=frozenset({denied.request_fingerprint}),
    )
    assert duplicate.delta.status.value == "INSUFFICIENT"
    assert blocked.calls == 1

    class RejectAllNeeds:
        def validate(self, **_kwargs: object) -> object:
            return SimpleNamespace(accepted_drafts=(), need_type_by_draft={})

    rejected_adapter = WriterReactiveNeedAdapter(
        cast(Any, blocked),
        artifacts,
        lambda value: max(1, len(value) // 4),
        schema_version=VERSION,
    )
    rejected_adapter._validator = cast(Any, RejectAllNeeds())
    rejected = rejected_adapter.resolve(
        request,
        view,
        (question.model_copy(update={"question": "Is the wound still current?"}),),
        inputs,
    )
    assert rejected.delta.status is ContextDeltaStatus.INSUFFICIENT

    real_compiler = adapter._compiler

    class NoEligibleChannel:
        def compile(self, need: object) -> object:
            return real_compiler.compile(cast(Any, need))

        def eligible_channels(
            self, *_args: object
        ) -> tuple[tuple[object, ...], tuple[object, ...]]:
            return (), ()

    no_channel_adapter = WriterReactiveNeedAdapter(
        cast(Any, blocked),
        artifacts,
        lambda value: max(1, len(value) // 4),
        schema_version=VERSION,
    )
    no_channel_adapter._compiler = cast(Any, NoEligibleChannel())
    with pytest.raises(WriterReactiveMemoryError, match="no executable"):
        no_channel_adapter.resolve(
            request,
            view,
            (question.model_copy(update={"question": question.question + "?"}),),
            inputs,
        )

    with pytest.raises(WriterReactiveMemoryError, match="requires"):
        adapter.resolve(request, view, (), inputs)
    with pytest.raises(WriterReactiveMemoryError, match="budget"):
        adapter.resolve(
            request,
            view,
            (question,) * (request.budgets.max_memory_questions + 1),
            inputs,
        )
    with pytest.raises(WriterReactiveMemoryError, match="basis"):
        adapter.resolve(
            request,
            view.model_copy(update={"task_id": TaskId("task.other")}),
            (question,),
            inputs,
        )
    no_goal = inputs.__class__(
        task=inputs.task,
        world=inputs.world,
        plan=inputs.plan.model_copy(update={"chapter_goals": ()}),
        focus_set=inputs.focus_set,
        text_root=inputs.text_root,
        resolution_template=inputs.resolution_template,
        thread_id=inputs.thread_id,
    )
    assert adapter.resolve(request, view, (question,), no_goal).delta.status.value == "DENIED"

    memory_gateway, _ = deterministic_memory_gateway(
        tmp_path / "actual-gateway",
        MemoryGatewayMode.DETERMINISTIC,
    )
    actual = WriterReactiveNeedAdapter(
        memory_gateway,
        artifacts,
        lambda value: max(1, len(value) // 4),
        schema_version=VERSION,
    ).resolve(request, view, (question,), inputs)
    assert actual.needs
    assert actual.delta.evidence_refs
    assert actual.delta.status in {
        ContextDeltaStatus.RESOLVED,
        ContextDeltaStatus.PARTIAL,
        ContextDeltaStatus.INSUFFICIENT,
    }


def test_formal_evaluation_runs_all_three_real_candidate_chains(tmp_path: Path) -> None:
    case = load_case(
        Path(__file__).parents[1]
        / "fixtures"
        / "stage3_evaluation"
        / "cases"
        / "enter_tower"
        / "case.json"
    )
    engines: list[object] = []

    class Factory:
        def __init__(self) -> None:
            self.calls: list[ContextScheme] = []

        def prepare(
            self,
            selected_case: Stage3EvaluationCase,
            scheme: ContextScheme,
        ) -> PreparedFullChainRun:
            self.calls.append(scheme)
            engine = create_engine("sqlite+pysqlite:///:memory:")
            from novel_agent.adapters.postgres.database import Base, build_session_factory

            Base.metadata.create_all(engine)
            engines.append(engine)
            factory = build_session_factory(engine)
            repositories = (
                RunEventLogRepository(factory),
                RunCheckpointRepository(factory),
            )
            request_artifacts = ArtifactRepository(
                FilesystemObjectStore(tmp_path / f"request-{scheme.value}")
            )
            request = _request(
                request_artifacts,
                f"{selected_case.case_id.root}.{scheme.value}",
            )
            loop, model_request, artifacts = _loop(
                tmp_path / f"loop-{scheme.value}",
                repositories,
                request,
                EditorialVerdict.PASS,
                artifact_repository=request_artifacts,
            )
            return PreparedFullChainRun(
                loop=loop,
                request=request,
                model_request=model_request,
                reactive_inputs=cast(Any, object()),
                artifacts=artifacts,
            )

    class Evaluator:
        def __init__(self) -> None:
            self.calls: list[ContextScheme] = []

        async def evaluate(
            self,
            selected_case: Stage3EvaluationCase,
            scheme: ContextScheme,
            final_text: str,
        ) -> tuple[EvaluatorScore, ...]:
            assert final_text
            self.calls.append(scheme)
            return (
                EvaluatorScore(
                    case_id=selected_case.case_id,
                    scheme=scheme,
                    dimension=EvaluatorDimension.PLAN_FOLLOWING,
                    score=1.0,
                    rationale="candidate was frozen before evaluator dispatch",
                ),
            )

    manifest_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "manifest-objects"))
    rubric = manifest_artifacts.put(b"rubric", "text/plain", VERSION)
    threshold = manifest_artifacts.put(b"threshold", "application/json", VERSION)
    case_ref = manifest_artifacts.put(b"case", "application/json", VERSION)
    manifest = Stage3FormalManifest(
        manifest_id=StableId("manifest.stage3.full-chain"),
        git_commit="test-commit",
        source_fingerprint=ArtifactId("sha256:" + "1" * 64),
        stage2_base_commit="stage2-test-commit",
        stage2_configuration_fingerprint=ArtifactId("sha256:" + "2" * 64),
        memory_gateway_policy_identity="memory-policy-v1",
        writer_model_identity="writer-test",
        editor_model_identity="editor-test",
        observer_model_identity="observer-test",
        evaluator_model_identity="evaluator-test",
        rubric_artifact=rubric,
        threshold_artifact=threshold,
        case_artifacts=(case_ref,),
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    factory = Factory()
    evaluator = Evaluator()
    report = asyncio.run(
        Stage3FullChainEvaluationService().run(
            (case,),
            manifest,
            factory,
            evaluator,
        )
    )

    assert factory.calls == list(ContextScheme)
    assert evaluator.calls == list(ContextScheme)
    assert len(report.cases) == 1
    assert {item.scheme for item in report.cases[0].schemes} == set(ContextScheme)
    assert all(
        item.loop_result.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
        for item in report.cases[0].schemes
    )
    assert all(len(item.loop_result.model_call_records) == 4 for item in report.cases[0].schemes)
    assert all(
        item.input_tokens
        == sum(call.usage.input_tokens for call in item.loop_result.model_call_records)
        and item.output_tokens
        == sum(call.usage.output_tokens for call in item.loop_result.model_call_records)
        for item in report.cases[0].schemes
    )
    assert report.semantic_pass_issued is False
    scheme = report.cases[0].schemes[0]
    with pytest.raises(ValidationError, match="run id"):
        Stage3FullChainSchemeResult.model_validate(
            scheme.model_dump(mode="python")
            | {
                "loop_result": scheme.loop_result.model_copy(
                    update={"run_id": RunId("run.unrelated")}
                )
            }
        )
    with pytest.raises(ValidationError, match="another case"):
        Stage3FullChainCaseResult(
            case_id=StableId("case.other"),
            schemes=report.cases[0].schemes,
        )
    with pytest.raises(ValidationError, match="all three"):
        Stage3FullChainCaseResult(
            case_id=case.case_id,
            schemes=(scheme, scheme, scheme),
        )

    class FailedLoop:
        def __init__(self, result: WritingLoopResult) -> None:
            self._result = result

        async def execute(self, *_args: object) -> WritingLoopResult:
            return self._result

    class FailedFactory:
        def prepare(
            self, selected_case: Stage3EvaluationCase, scheme: ContextScheme
        ) -> PreparedFullChainRun:
            prepared = Factory().prepare(selected_case, scheme)
            final_ref = (
                prepared.artifacts.put(b"failed candidate", "text/plain", VERSION)
                if scheme is ContextScheme.SIMPLE_RETRIEVAL
                else None
            )
            failed = WritingLoopResult(
                result_id=StableId(f"result.failed.{scheme.value}"),
                run_id=prepared.request.run_id,
                task_id=prepared.request.task_id,
                status=WritingLoopTerminalStatus.EDITOR_FAILED,
                final_text_artifact=final_ref,
                failure_detail="editor failed",
            )
            return PreparedFullChainRun(
                loop=cast(Any, FailedLoop(failed)),
                request=prepared.request,
                model_request=prepared.model_request,
                reactive_inputs=prepared.reactive_inputs,
                artifacts=prepared.artifacts,
            )

    failed_evaluator = Evaluator()
    failed_report = asyncio.run(
        Stage3FullChainEvaluationService().run(
            (case,),
            manifest,
            FailedFactory(),
            failed_evaluator,
        )
    )
    assert not failed_evaluator.calls
    assert all(item.deterministic_rules is None for item in failed_report.cases[0].schemes)
    for engine in engines:
        cast(Any, engine).dispose()
