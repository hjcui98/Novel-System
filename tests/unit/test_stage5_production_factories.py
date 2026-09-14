from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import DerivedSnapshotRow
from novel_agent.adapters.runtime.isolated import StrictDeterministicCandidateMaterializer
from novel_agent.adapters.runtime.stage3_writer import (
    ProductionWritingRequestFactory,
    Stage2MWriterContextInvocation,
    WritingRequestPolicy,
)
from novel_agent.adapters.runtime.stage4_planner import (
    ProductionStage4InvocationFactory,
    Stage4InvocationPolicy,
    Stage4PlanningLeafAdapter,
)
from novel_agent.domain.artifacts import (
    MODEL_RAW_RESPONSE_MEDIA_TYPE,
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.benchmark import ChapterGoal, TextRootDocument
from novel_agent.domain.creative_runtime import (
    AcceptanceCommand,
    AcceptanceDecision,
    ActorKind,
    AutomationMode,
    CandidateKind,
    CreativeRunPolicy,
    CreativeRunRequest,
    OperatorReviewEvidence,
    OperatorReviewFinding,
    PlanningLoopRequest,
    PlanningTerminalStatus,
    RuntimeModelReplayEvidence,
    RuntimeModelReplayResponse,
)
from novel_agent.domain.generation import (
    WritingLengthPolicy,
    WritingLoopBudgets,
    WritingLoopRequest,
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
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.planning import (
    PlanningBudgets,
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
from novel_agent.domain.retrieval_decision import (
    HistoryRetrievalRequirement,
    RetrievalExecutionStatus,
)
from novel_agent.domain.runtime import TaskKind, TaskRecord, TaskStatus
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    ContextBudget,
    ContractRef,
    ExecutionStatus,
    PlanProposal,
    ProjectProfileRootDocument,
    PromptContractRef,
    ProposalProvenance,
    ProposedItem,
    RetrievalBudget,
    SkillContractRef,
)
from novel_agent.domain.world import PlanLevel
from novel_agent.domain.writer_context import (
    ContextAssemblyStatus,
    NeedEvidenceSemanticStatus,
    NeedFacetSemanticReceipt,
    WriterContextPackageV2,
)
from novel_agent.domain.writer_readiness import (
    WriterContextInputNotReady,
    WriterReadinessReasonCode,
)
from novel_agent.ports.creative_runtime import WritingLeafPort
from novel_agent.runtime.production_components import (
    ProductionCuratorModelRequestFactory,
    ProductionReactiveMemoryInputsFactory,
    ProductionWriterModelRequestFactory,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, plan_root_content_id
from novel_agent.services.creative_runtime import CreativeRuntimeService
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.evidence_first_writer_context_assembler import (
    EvidenceFirstAssemblyResult,
    EvidenceFirstWriterContextAssembler,
    NeedEvidenceSelection,
    SliceSelectionTrace,
)
from novel_agent.services.evidence_slice_resolver import EvidenceSliceResolver
from novel_agent.services.planning_context_loop import PlanningContextLoopService
from novel_agent.services.projection import DerivedProjectionService, DerivedSnapshotRepository
from novel_agent.services.recent_prose import RecentProseAssembler
from novel_agent.services.runtime_acceptance import RuntimeAcceptanceService
from novel_agent.services.runtime_commands import RuntimeCommandService
from tests.factories import make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.fixtures.stage2_memory_benchmark import writer_context_inputs

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "1" * 64)


def _put(artifacts: ArtifactRepository, value: object, media_type: str) -> ArtifactRef:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return artifacts.put(canonical_json_bytes(payload), media_type, VERSION)


def _profile() -> ProjectProfileRootDocument:
    contract = ContractRef(contract_id=StableId("agent.writer"), version=VERSION, content_hash=HASH)
    return ProjectProfileRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        style_profile={
            "pov": "Lin",
            "narrative_person": "third person limited",
            "forbidden_reveals": ["Do not reveal the tower core."],
        },
        agent_specs=(contract,),
        prompt_contracts=(
            PromptContractRef(**contract.model_dump(mode="python"), render_fingerprint=HASH),
        ),
        skill_contracts=(SkillContractRef(**contract.model_dump(mode="python")),),
        tool_policies=(contract,),
        model_profiles=("offline-v1",),
    )


def _canonical(
    tmp_path: Path,
    *,
    extra_goals: tuple[ChapterGoal, ...] = (),
    publish_snapshot: bool = False,
) -> tuple[
    ArtifactRepository,
    CommitService,
    CommitId,
    TextRootDocument,
    DerivedSnapshotRepository,
]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    commits = CommitService(session_factory)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    bundle = make_synthetic_bundle()
    text = next(item for item in bundle.text_roots if len(item.chapters) == 20)
    world = bundle.world_roots[0]
    original_plan = bundle.plan_roots[0]
    goal = ChapterGoal(
        goal_id=StableId("plan.chapter.21"),
        chapter_index=21,
        summary="Enter the tower while protecting the injured arm.",
        obligation_ids=tuple(item.obligation_id for item in world.obligations),
        payload={
            "history_retrieval": {
                "requirement": "REQUIRED",
                "needs": [
                    {
                        "kind": "causal_history",
                        "query": "what happened before the tower approach",
                        "why_needed": "continuity",
                    }
                ],
            }
        },
    )
    provisional = original_plan.model_copy(
        update={
            "root_hash": ArtifactId("sha256:" + "0" * 64),
            "chapter_goals": (goal, *extra_goals),
        }
    )
    plan = provisional.model_copy(update={"root_hash": plan_root_content_id(provisional)})
    text_ref = _put(artifacts, text, "application/vnd.novel-agent.text-root+json")
    world_ref = _put(artifacts, world, "application/vnd.novel-agent.world-root+json")
    plan_ref = _put(artifacts, plan, "application/vnd.novel-agent.plan-root+json")
    profile = _profile()
    profile_ref = _put(artifacts, profile, "application/vnd.novel-agent.project-profile-root+json")
    manifest = make_manifest().model_copy(
        update={
            "text_root": TextRootRef(**text_ref.model_dump(mode="python")),
            "world_root": WorldRootRef(**world_ref.model_dump(mode="python")),
            "plan_root": PlanRootRef(**plan_ref.model_dump(mode="python")),
            "project_profile_root": ProjectProfileRootRef(**profile_ref.model_dump(mode="python")),
        }
    )
    base = commits.initialize_project(manifest)
    if publish_snapshot:
        published = DerivedSnapshotLite(
            snapshot_id=StableId("snapshot.chapter.20"),
            source_commit=base,
            anchor_build_id=StableId("anchor.production-factory"),
            anchor_index_version="anchor.v1",
            grounded_index_version="grounded.v1",
            embedding_profile="embedding.v1",
            fusion_profile="fusion.v1",
            build_status=DerivedBuildStatus.EXACT,
            published_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        with session_factory() as session:
            session.add(
                DerivedSnapshotRow(
                    snapshot_id=published.snapshot_id.root,
                    project_id=manifest.project_id.root,
                    source_commit=base.root,
                    build_status=DerivedBuildStatus.EXACT.value,
                    snapshot_json=published.model_dump(mode="json"),
                    published_at=published.published_at,
                )
            )
            session.commit()
    return artifacts, commits, base, text, DerivedSnapshotRepository(session_factory)


def test_production_curator_factory_uses_settlement_transport_timeout() -> None:
    factory = ProductionCuratorModelRequestFactory(
        run_id=RunId("factory-curator-run"),
        task_id=TaskId("factory-curator-task"),
        max_output_tokens=8_000,
        timeout_seconds=60.0,
    )

    request = factory("curator.replay.47.proposal-1", AgentMode.REPLAY)

    assert request.max_output_tokens == 8_000
    assert request.timeout_seconds == 60.0


def test_production_curator_factory_scopes_request_identity_by_run() -> None:
    first_factory = ProductionCuratorModelRequestFactory(
        run_id=RunId("factory-curator-run-1"),
        task_id=TaskId("factory-curator-task"),
        max_output_tokens=8_000,
        timeout_seconds=60.0,
    )
    second_factory = ProductionCuratorModelRequestFactory(
        run_id=RunId("factory-curator-run-2"),
        task_id=TaskId("factory-curator-task"),
        max_output_tokens=8_000,
        timeout_seconds=60.0,
    )

    first = first_factory("guardian.21", AgentMode.RISK_REVIEW)
    second = second_factory("guardian.21", AgentMode.RISK_REVIEW)

    assert first.request_id != second.request_id
    assert "factory-curator-run-1" in first.request_id.root
    assert "factory-curator-run-2" in second.request_id.root


def test_production_curator_factory_fails_closed_when_run_and_task_are_maximal() -> None:
    factory = ProductionCuratorModelRequestFactory(
        run_id=RunId("r" * 128),
        task_id=TaskId("t" * 128),
        max_output_tokens=8_000,
        timeout_seconds=60.0,
    )

    with pytest.raises(ValueError, match="stable identity is too long"):
        factory("guardian.21", AgentMode.RISK_REVIEW)


def _writing_policy() -> WritingRequestPolicy:
    return WritingRequestPolicy(
        pov="fallback POV",
        narrative_person="fallback person",
        length_policy=WritingLengthPolicy(
            minimum_characters=500,
            target_characters=1_500,
            maximum_characters=3_000,
        ),
        allowed_skills=(StableId("skill.scene-composition"),),
        budgets=WritingLoopBudgets(
            context_sequence_limit=32_000,
            reserved_output_tokens=4_000,
            context_safety_allowance_tokens=1_000,
            context_soft_limit_tokens=24_000,
        ),
        writer_configuration_fingerprint=HASH,
        model_configuration_fingerprint=HASH,
        future_isolation_configuration_fingerprint=HASH,
    )


def test_production_writing_factory_builds_v2_request_from_exact_commit(
    tmp_path: Path,
) -> None:
    artifacts, commits, base, _text, snapshots = _canonical(tmp_path, publish_snapshot=True)
    snapshot = StableId("snapshot.chapter.20")
    run_id = RunId("run.production-writer")
    unrelated_ref = artifacts.put(b"terminal", "application/json", VERSION)
    advisory_ref = artifacts.put(
        b"quarantine-advisory",
        "application/vnd.novel-agent.quarantine-package+json",
        VERSION,
    )
    task = TaskRecord(
        task_id=TaskId("task.production-writer"),
        run_id=run_id,
        project_id=ProjectId("project.test"),
        kind=TaskKind.DRAFT_CANDIDATE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        basis_snapshot=snapshot,
        policy_hash=HASH.root,
        permission_hash=HASH.root,
        chapter_index=21,
        target_chapters=25,
        current_attempt_id=StableId("attempt.production-writer"),
        input_artifact_refs=(advisory_ref,),
        terminal_artifact_refs=(unrelated_ref,),
    )

    def stage2m(invocation: Stage2MWriterContextInvocation) -> EvidenceFirstAssemblyResult:
        assert invocation.advisory_artifact_refs == (advisory_ref,)
        _fixture_task, needs, _units, _fixture_base = writer_context_inputs()
        block = invocation.text.chapters[-1].scenes[0].blocks[0]
        need = needs[0].model_copy(
            update={
                "run_id": run_id,
                "task_id": invocation.task.task_id,
                "base_commit": invocation.base_commit,
                "horizon_target": (21, 21),
            }
        )
        slice_ = EvidenceSliceResolver().resolve_block(
            block,
            source_commit=invocation.base_commit,
            snapshot_id=invocation.snapshot_id,
            access_scope=need.access_scope,
        )[0]
        result = EvidenceFirstWriterContextAssembler().assemble(
            task=invocation.task,
            selections=(
                NeedEvidenceSelection(
                    need=need,
                    selections=(
                        SliceSelectionTrace(
                            slice_id=slice_.slice_id,
                            unit_id=StableId("unit.production-writer"),
                            route_channel="r1_exact",
                            fused_rank=1,
                            selection_reason="production factory focused evidence",
                        ),
                    ),
                    slices=(slice_,),
                    semantic_receipts=tuple(
                        NeedFacetSemanticReceipt(
                            need_id=need.need_id,
                            need_facet_id=facet.need_facet_id,
                            facet_kind=facet.facet_kind.value,
                            status=NeedEvidenceSemanticStatus.SUPPORTED,
                            mandatory=True,
                            evaluated_slice_ids=(slice_.slice_id,),
                            supporting_slice_ids=(slice_.slice_id,),
                            judge_version="test",
                        )
                        for facet in need.need_facets
                    ),
                ),
            ),
            text_root=invocation.text,
            basis_commit_id=invocation.base_commit,
            basis_snapshot_id=invocation.snapshot_id,
            gateway_context_artifact=invocation.planning_context_ref,
            frozen_evidence_selections_artifact=invocation.plan_root_ref,
            retrieval_requirement=HistoryRetrievalRequirement.REQUIRED,
            retrieval_status=RetrievalExecutionStatus.EXECUTED,
            plan_root_ref=invocation.plan_root_ref,
            plan_revision=invocation.plan_revision,
            chapter_goal_ids=invocation.chapter_goal_ids,
            planning_context_ref=invocation.planning_context_ref,
        )
        assert result.status is ContextAssemblyStatus.READY
        return result

    request = ProductionWritingRequestFactory(
        commits=commits,
        artifacts=artifacts,
        recent_prose=RecentProseAssembler(artifacts, VERSION),
        writer_context=stage2m,
        policy=_writing_policy(),
        schema_version=VERSION,
        snapshots=snapshots,
    )(task)

    assert request.writing_task.target_chapter == 21
    assert request.writing_task.chapter_goal.startswith("Enter the tower")
    assert request.writing_task.pov == "Lin"
    assert isinstance(request.writer_context_package, WriterContextPackageV2)
    assert request.recent_prose_context.previous_chapter is not None
    assert request.recent_prose_context.previous_chapter.chapter_index == 20
    assert request.future_isolation_attestation.evaluator_only_source_ids == ()
    assert request.resume_checkpoint_ref is None
    assert request.attempt_id == task.current_attempt_id
    model_request = ProductionWriterModelRequestFactory(
        role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        max_output_tokens=8_000,
        timeout_seconds=120.0,
    )(request)
    assert model_request.attempt_id == task.current_attempt_id
    assert task.current_attempt_id is not None
    assert task.current_attempt_id.root in model_request.request_id.root
    retry_request = request.model_copy(
        update={"attempt_id": StableId("attempt.production-writer.2")}
    )
    retry_model_request = ProductionWriterModelRequestFactory(
        role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        max_output_tokens=8_000,
        timeout_seconds=120.0,
    )(retry_request)
    assert retry_model_request.request_id != model_request.request_id
    assert retry_model_request.attempt_id == retry_request.attempt_id

    maximal_scope_request = request.model_copy(
        update={
            "run_id": RunId("r" * 128),
            "task_id": TaskId("t" * 128),
            "attempt_id": None,
        }
    )
    with pytest.raises(ValueError, match="stable identity is too long"):
        ProductionWriterModelRequestFactory(
            role=ModelRole.IMPLEMENTATION,
            purpose=ModelCallPurpose.DEVELOPMENT,
            max_output_tokens=8_000,
            timeout_seconds=120.0,
        )(maximal_scope_request)

    reactive = ProductionReactiveMemoryInputsFactory(commits, artifacts)(request)
    assert reactive.world.source_commit == request.base_commit
    assert reactive.resolution_template.base_commit == request.base_commit
    assert reactive.resolution_template.snapshot_id == request.snapshot_id


def test_production_writing_factory_rejects_multiple_goals_for_one_chapter(
    tmp_path: Path,
) -> None:
    artifacts, commits, base, _text, snapshots = _canonical(
        tmp_path,
        publish_snapshot=True,
        extra_goals=(
            ChapterGoal(
                goal_id=StableId("plan.chapter.21.candidate"),
                chapter_index=21,
                summary="Enter the tower while protecting the injured arm.",
            ),
            ChapterGoal(
                goal_id=StableId("plan.chapter.21.second"),
                chapter_index=21,
                summary="Keep the injured arm out of the inner ward.",
                obligation_ids=(StableId("obligation.arm"),),
            ),
        ),
    )
    snapshot = StableId("snapshot.chapter.20")
    task = TaskRecord(
        task_id=TaskId("task.production-writer-multi-goal"),
        run_id=RunId("run.production-writer-multi-goal"),
        project_id=ProjectId("project.test"),
        kind=TaskKind.DRAFT_CANDIDATE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        basis_snapshot=snapshot,
        policy_hash=HASH.root,
        permission_hash=HASH.root,
        chapter_index=21,
        target_chapters=25,
    )
    with pytest.raises(ValueError, match="at most one active chapter goal"):
        ProductionWritingRequestFactory(
            commits=commits,
            artifacts=artifacts,
            recent_prose=RecentProseAssembler(artifacts, VERSION),
            writer_context=cast(
                Callable[[Stage2MWriterContextInvocation], EvidenceFirstAssemblyResult], object()
            ),
            policy=_writing_policy(),
            schema_version=VERSION,
            snapshots=snapshots,
        )(task)


def test_production_writing_factory_preserves_semantic_gaps_and_rejects_complete_rewrite(
    tmp_path: Path,
) -> None:
    artifacts, commits, base, _text, snapshots = _canonical(tmp_path, publish_snapshot=True)
    snapshot = StableId("snapshot.chapter.20")
    run_id = RunId("run.production-writer-gaps")
    task = TaskRecord(
        task_id=TaskId("task.production-writer-gaps"),
        run_id=run_id,
        project_id=ProjectId("project.test"),
        kind=TaskKind.DRAFT_CANDIDATE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        basis_snapshot=snapshot,
        policy_hash=HASH.root,
        permission_hash=HASH.root,
        chapter_index=21,
        target_chapters=25,
    )
    unclosed = StableId("facet.unclosed.continuity")

    def stage2m(invocation: Stage2MWriterContextInvocation) -> EvidenceFirstAssemblyResult:
        _fixture_task, needs, _units, _fixture_base = writer_context_inputs()
        block = invocation.text.chapters[-1].scenes[0].blocks[0]
        need = needs[0].model_copy(
            update={
                "run_id": run_id,
                "task_id": invocation.task.task_id,
                "base_commit": invocation.base_commit,
                "horizon_target": (21, 21),
            }
        )
        slice_ = EvidenceSliceResolver().resolve_block(
            block,
            source_commit=invocation.base_commit,
            snapshot_id=invocation.snapshot_id,
            access_scope=need.access_scope,
        )[0]
        result = EvidenceFirstWriterContextAssembler().assemble(
            task=invocation.task,
            selections=(
                NeedEvidenceSelection(
                    need=need,
                    selections=(
                        SliceSelectionTrace(
                            slice_id=slice_.slice_id,
                            unit_id=StableId("unit.production-writer-gaps"),
                            route_channel="r1_exact",
                            fused_rank=1,
                            selection_reason="production factory semantic-gap evidence",
                        ),
                    ),
                    slices=(slice_,),
                ),
            ),
            text_root=invocation.text,
            basis_commit_id=invocation.base_commit,
            basis_snapshot_id=invocation.snapshot_id,
        )
        assert result.status is ContextAssemblyStatus.READY
        gapped = result.package.model_copy(
            update={
                "semantic_status": "INCOMPLETE",
                "usable_with_gaps": True,
                "unclosed_mandatory_need_facets": (unclosed,),
            }
        )
        return result.model_copy(
            update={
                "package": gapped,
                "semantic_status": "INCOMPLETE",
                "usable_with_gaps": True,
                "unclosed_mandatory_need_facets": (unclosed,),
            }
        )

    factory = ProductionWritingRequestFactory(
        commits=commits,
        artifacts=artifacts,
        recent_prose=RecentProseAssembler(artifacts, VERSION),
        writer_context=stage2m,
        policy=_writing_policy(),
        schema_version=VERSION,
        snapshots=snapshots,
    )
    with pytest.raises(WriterContextInputNotReady) as error:
        factory(task)
    assert WriterReadinessReasonCode.MANDATORY_FACET_INCOMPLETE in (
        error.value.decision.reason_codes
    )


def test_production_stage4_factory_builds_chapter_set_horizon_from_runtime_task(
    tmp_path: Path,
) -> None:
    artifacts, commits, base, _text, _snapshots = _canonical(tmp_path)
    author_ref = artifacts.put(b"coarse author outline", "text/plain", VERSION)
    old_checkpoint_ref = artifacts.put(
        b'{"checkpoint":"old"}',
        "application/vnd.novel-agent.planning-loop-checkpoint+json",
        VERSION,
    )
    unrelated_ref = artifacts.put(b"terminal", "application/json", VERSION)
    checkpoint_ref = artifacts.put(
        b'{"checkpoint":"latest"}',
        "application/vnd.novel-agent.planning-loop-checkpoint+json",
        VERSION,
    )
    request = PlanningLoopRequest(
        run_id=RunId("run.production-planner"),
        task_id=TaskId("task.production-planner"),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        basis_snapshot=StableId("snapshot.chapter.20"),
        input_artifact_refs=(author_ref,),
        continuation_artifact_refs=(old_checkpoint_ref, unrelated_ref, checkpoint_ref),
        planner_memory_budget_extensions=2,
        chapter_index=20,
        horizon_start=21,
        horizon_end=25,
    )
    policy = Stage4InvocationPolicy(
        budgets=PlanningBudgets(
            retrieval=RetrievalBudget(max_full_chapter_reads=1),
            context=ContextBudget(token_budget=8_000),
        ),
        configuration_fingerprint=HASH,
        model_fingerprint=HASH,
        model_max_output_tokens=9_000,
    )
    factory = ProductionStage4InvocationFactory(
        commits=commits,
        artifacts=artifacts,
        policy=policy,
        model_request_namespace="resume-c6ccf194e344449f",
    )
    invocation = factory(request)

    assert invocation.request.task.mode is AgentMode.CHAPTER_SET
    assert invocation.request.horizon_start == 21
    assert invocation.request.horizon_end == 25
    assert invocation.request.author_intent_artifacts == (author_ref,)
    assert invocation.world is not None
    assert invocation.text_root is not None
    assert invocation.resume_checkpoint_ref == checkpoint_ref
    base_retrieval = policy.budgets.retrieval
    effective_retrieval = invocation.request.budgets.retrieval
    assert effective_retrieval.max_rounds == base_retrieval.max_rounds * 3
    assert effective_retrieval.max_tool_calls == base_retrieval.max_tool_calls * 3
    assert effective_retrieval.max_anchor_expansions == base_retrieval.max_anchor_expansions * 3
    assert effective_retrieval.max_full_chapter_reads == base_retrieval.max_full_chapter_reads * 3
    assert effective_retrieval.wall_clock_budget_ms == base_retrieval.wall_clock_budget_ms * 3
    assert effective_retrieval.token_budget == base_retrieval.token_budget * 3
    assert effective_retrieval.max_candidates == base_retrieval.max_candidates
    assert (
        effective_retrieval.max_query_rewrites_per_need
        == base_retrieval.max_query_rewrites_per_need
    )
    model_request = invocation.model_request("plan", AgentMode.CHAPTER_SET, 1)
    assert model_request.task_id == request.task_id
    assert model_request.request_id.root.endswith(".resume-c6ccf194e344449f.plan.1")
    assert model_request.max_output_tokens == policy.model_max_output_tokens
    assert model_request.enable_thinking is False

    retry_request_a = request.model_copy(update={"attempt_id": StableId("attempt.factory-a")})
    retry_request_b = request.model_copy(update={"attempt_id": StableId("attempt.factory-b")})
    retry_factory = ProductionStage4InvocationFactory(
        commits=commits,
        artifacts=artifacts,
        policy=policy,
    )
    retry_id_a = retry_factory(retry_request_a).model_request("plan", AgentMode.CHAPTER_SET, 1)
    retry_id_b = retry_factory(retry_request_b).model_request("plan", AgentMode.CHAPTER_SET, 1)
    assert retry_id_a.request_id != retry_id_b.request_id
    assert retry_id_a.attempt_id == retry_request_a.attempt_id
    assert retry_id_b.attempt_id == retry_request_b.attempt_id


def test_production_stage4_factory_rebinds_replay_to_its_logical_phase(
    tmp_path: Path,
) -> None:
    artifacts, commits, base, _text, _snapshots = _canonical(tmp_path)
    raw_ref = artifacts.put(b"raw model response", MODEL_RAW_RESPONSE_MEDIA_TYPE, VERSION)
    replay = RuntimeModelReplayEvidence(
        run_id=RunId("run.production-replay"),
        task_id=TaskId("task.production-replay"),
        source_attempt_id=StableId("attempt.previous"),
        responses=(
            RuntimeModelReplayResponse(
                request_id=StableId("model.production-replay.plan-revision.1"),
                source_attempt_id=StableId("attempt.previous"),
                request_hash=HASH.root,
                logical_phase="plan_revision",
                raw_artifact_ref=raw_ref,
            ),
        ),
    )
    replay_ref = _put(
        artifacts,
        replay,
        "application/vnd.novel-agent.runtime-model-replay-evidence+json",
    )
    request = PlanningLoopRequest(
        run_id=replay.run_id,
        task_id=replay.task_id,
        project_id=ProjectId("project.test"),
        basis_commit=base,
        basis_snapshot=StableId("snapshot.production-replay"),
        continuation_artifact_refs=(replay_ref,),
        attempt_id=StableId("attempt.recovered"),
        chapter_index=20,
        horizon_start=21,
        horizon_end=25,
    )
    policy = Stage4InvocationPolicy(
        budgets=PlanningBudgets(
            retrieval=RetrievalBudget(max_full_chapter_reads=1),
            context=ContextBudget(token_budget=8_000),
        ),
        configuration_fingerprint=HASH,
        model_fingerprint=HASH,
    )
    invocation = ProductionStage4InvocationFactory(
        commits=commits,
        artifacts=artifacts,
        policy=policy,
    )(request)

    with pytest.raises(ValueError, match="no response for Stage 4 logical phase"):
        invocation.model_request("plan_review", AgentMode.CHAPTER_SET, 1)
    rebound = invocation.model_request("plan_revision", AgentMode.CHAPTER_SET, 1)
    assert rebound.request_id == StableId("model.production-replay.plan-revision.1")
    assert rebound.attempt_id == StableId("attempt.previous")


def test_production_stage4_leaf_consumes_replay_at_the_bound_phase(
    tmp_path: Path,
) -> None:
    """The public Stage 4 leaf must hand the retained response to its phase."""

    artifacts, commits, base, _text, _snapshots = _canonical(tmp_path)
    raw_ref = artifacts.put(b"raw model response", MODEL_RAW_RESPONSE_MEDIA_TYPE, VERSION)
    replay = RuntimeModelReplayEvidence(
        run_id=RunId("run.production-leaf-replay"),
        task_id=TaskId("task.production-leaf-replay"),
        source_attempt_id=StableId("attempt.previous"),
        responses=(
            RuntimeModelReplayResponse(
                request_id=StableId("model.production-leaf-replay.plan-revision.1"),
                source_attempt_id=StableId("attempt.previous"),
                request_hash=HASH.root,
                logical_phase="plan_revision",
                raw_artifact_ref=raw_ref,
            ),
        ),
    )
    replay_ref = _put(
        artifacts,
        replay,
        "application/vnd.novel-agent.runtime-model-replay-evidence+json",
    )
    request = PlanningLoopRequest(
        run_id=replay.run_id,
        task_id=replay.task_id,
        project_id=ProjectId("project.test"),
        basis_commit=base,
        basis_snapshot=StableId("snapshot.production-leaf-replay"),
        continuation_artifact_refs=(replay_ref,),
        attempt_id=StableId("attempt.recovered"),
        chapter_index=20,
        horizon_start=21,
        horizon_end=25,
    )
    policy = Stage4InvocationPolicy(
        budgets=PlanningBudgets(
            retrieval=RetrievalBudget(max_full_chapter_reads=1),
            context=ContextBudget(token_budget=8_000),
        ),
        configuration_fingerprint=HASH,
        model_fingerprint=HASH,
    )
    invocation_factory = ProductionStage4InvocationFactory(
        commits=commits,
        artifacts=artifacts,
        policy=policy,
    )
    invocation = invocation_factory(request)
    checkpoint_ref = artifacts.put(
        b'{"checkpoint":true}',
        "application/vnd.novel-agent.planning-loop-checkpoint+json",
        VERSION,
    )
    captured: list[ModelRequest] = []

    class _Stage4Loop:
        async def run(self, **kwargs: object) -> Stage4PlanningLoopResult:
            detailed = cast(Stage4PlanningLoopRequest, kwargs["request"])
            request_factory = cast(
                Callable[[str, AgentMode, int], ModelRequest],
                kwargs["model_request"],
            )
            model_request = request_factory("plan_revision", detailed.task.mode, 1)
            captured.append(model_request)
            return Stage4PlanningLoopResult(
                request_id=detailed.request_id,
                terminal=Stage4PlanningLoopTerminal.YIELDED,
                event_artifacts=(checkpoint_ref,),
                diagnostic_codes=("PLAN_REVISION_SLICE_EXHAUSTED",),
            )

    adapter = Stage4PlanningLeafAdapter(
        cast(PlanningContextLoopService, _Stage4Loop()),
        artifacts,
        lambda _request: invocation,
        schema_version=VERSION,
    )

    result = asyncio.run(adapter.run(request))

    assert result.status is PlanningTerminalStatus.YIELDED
    assert captured[0].request_id == StableId("model.production-leaf-replay.plan-revision.1")
    assert captured[0].attempt_id == StableId("attempt.previous")
    assert captured[0].scheduling_stage == "plan_revision"
    assert invocation.replay_completion_check is not None
    invocation.replay_completion_check()

    second_response = RuntimeModelReplayResponse(
        request_id=StableId("model.production-leaf-replay.plan-review.1"),
        source_attempt_id=StableId("attempt.previous"),
        request_hash=HASH.root,
        logical_phase="plan_review",
        raw_artifact_ref=raw_ref,
    )
    two_response_ref = _put(
        artifacts,
        replay.model_copy(update={"responses": (*replay.responses, second_response)}),
        "application/vnd.novel-agent.runtime-model-replay-evidence+json",
    )
    two_response_invocation = invocation_factory(
        request.model_copy(update={"continuation_artifact_refs": (two_response_ref,)})
    )
    two_response_invocation.model_request("plan_revision", AgentMode.CHAPTER_SET, 1)
    assert two_response_invocation.replay_completion_check is not None
    with pytest.raises(ValueError, match="unconsumed response"):
        two_response_invocation.replay_completion_check()
    adapter_invocation = invocation_factory(
        request.model_copy(update={"continuation_artifact_refs": (two_response_ref,)})
    )

    class _EarlyCandidateStage4Loop:
        async def run(self, **kwargs: object) -> Stage4PlanningLoopResult:
            detailed = cast(Stage4PlanningLoopRequest, kwargs["request"])
            request_factory = cast(
                Callable[[str, AgentMode, int], ModelRequest],
                kwargs["model_request"],
            )
            request_factory("plan_revision", detailed.task.mode, 1)
            return Stage4PlanningLoopResult.model_construct(
                request_id=detailed.request_id,
                terminal=Stage4PlanningLoopTerminal.HUMAN_REQUIRED,
                proposal=PlanProposal.model_construct(),
            )

    early_candidate_adapter = Stage4PlanningLeafAdapter(
        cast(PlanningContextLoopService, _EarlyCandidateStage4Loop()),
        artifacts,
        lambda _request: adapter_invocation,
        schema_version=VERSION,
    )
    with pytest.raises(ValueError, match="unconsumed response"):
        asyncio.run(
            early_candidate_adapter.run(
                request.model_copy(update={"continuation_artifact_refs": (two_response_ref,)})
            )
        )


def test_production_stage4_factory_story_keeps_author_brief(tmp_path: Path) -> None:
    artifacts, commits, base, _text, _snapshots = _canonical(tmp_path)
    author_ref = artifacts.put(b"full author brief", "text/plain", VERSION)
    request = PlanningLoopRequest(
        run_id=RunId("run.production-story"),
        task_id=TaskId("task.production-story"),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        basis_snapshot=StableId("snapshot.chapter.20"),
        input_artifact_refs=(author_ref,),
        chapter_index=20,
        plan_level=PlanLevel.STORY,
    )
    policy = Stage4InvocationPolicy(
        budgets=PlanningBudgets(
            retrieval=RetrievalBudget(max_full_chapter_reads=1),
            context=ContextBudget(token_budget=8_000),
        ),
        configuration_fingerprint=HASH,
        model_fingerprint=HASH,
    )
    invocation = ProductionStage4InvocationFactory(
        commits=commits,
        artifacts=artifacts,
        policy=policy,
    )(request)

    assert invocation.request.task.mode is AgentMode.STORY
    assert invocation.request.horizon_start is None
    assert invocation.request.horizon_end is None
    assert invocation.request.author_intent_artifacts == (author_ref,)


def test_production_stage4_factory_separates_revision_lineage_from_author_sources(
    tmp_path: Path,
) -> None:
    artifacts, commits, base, _text, _snapshots = _canonical(tmp_path)
    author_ref = artifacts.put(b"full author brief", "text/plain", VERSION)
    candidate_ref = artifacts.put(
        b"candidate",
        "application/vnd.novel-agent.plan-proposal+json",
        VERSION,
    )
    review_ref = artifacts.put(
        b"operator review",
        "application/vnd.novel-agent.operator-plan-review+json",
        VERSION,
    )
    directive_ref = artifacts.put(
        b'{"kind":"operator_revision"}',
        "application/vnd.novel-agent.operator-revision-directive+json",
        VERSION,
    )
    rebind_ref = artifacts.put(
        b"rebind evidence",
        "application/vnd.novel-agent.runtime-rebind-evidence+json",
        VERSION,
    )
    request = PlanningLoopRequest(
        run_id=RunId("run.production-revision-lineage"),
        task_id=TaskId("task.production-revision-lineage"),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        basis_snapshot=StableId("snapshot.chapter.20"),
        input_artifact_refs=(author_ref, candidate_ref, review_ref, directive_ref, rebind_ref),
        chapter_index=20,
        plan_level=PlanLevel.STORY,
    )
    policy = Stage4InvocationPolicy(
        budgets=PlanningBudgets(
            retrieval=RetrievalBudget(max_full_chapter_reads=1),
            context=ContextBudget(token_budget=8_000),
        ),
        configuration_fingerprint=HASH,
        model_fingerprint=HASH,
    )

    invocation = ProductionStage4InvocationFactory(
        commits=commits,
        artifacts=artifacts,
        policy=policy,
    )(request)

    assert invocation.request.author_intent_artifacts == (author_ref,)
    assert invocation.request.revision_artifact_refs == (directive_ref,)
    assert invocation.request.revision_parent_proposal_ref == candidate_ref
    assert invocation.request.revision_review_artifact_refs == (review_ref,)
    assert invocation.request.task.source_ids == (
        StableId(f"source.author-intent.{author_ref.artifact_id.root[-24:]}"),
    )


def test_production_stage4_factory_reaches_public_leaf_materialization(tmp_path: Path) -> None:
    """The production request boundary must feed the public candidate materializer."""

    artifacts, commits, base, _text, _snapshots = _canonical(tmp_path)
    author_ref = artifacts.put(b"full author brief", "text/plain", VERSION)
    candidate_ref = artifacts.put(
        b"candidate",
        "application/vnd.novel-agent.plan-proposal+json",
        VERSION,
    )
    review_ref = artifacts.put(
        b"operator review",
        "application/vnd.novel-agent.operator-plan-review+json",
        VERSION,
    )
    directive_ref = artifacts.put(
        b'{"kind":"operator_revision","target_ids":["plan.story"]}',
        "application/vnd.novel-agent.operator-revision-directive+json",
        VERSION,
    )
    request = PlanningLoopRequest(
        run_id=RunId("run.production-public-materialization"),
        task_id=TaskId("task.production-public-materialization"),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        basis_snapshot=StableId("snapshot.chapter.20"),
        input_artifact_refs=(author_ref, candidate_ref, review_ref, directive_ref),
        chapter_index=20,
        plan_level=PlanLevel.STORY,
    )
    policy = Stage4InvocationPolicy(
        budgets=PlanningBudgets(
            retrieval=RetrievalBudget(max_full_chapter_reads=1),
            context=ContextBudget(token_budget=8_000),
        ),
        configuration_fingerprint=HASH,
        model_fingerprint=HASH,
    )
    invocation_factory = ProductionStage4InvocationFactory(
        commits=commits,
        artifacts=artifacts,
        policy=policy,
    )
    captured: list[Stage4PlanningLoopRequest] = []

    class _PublicStage4Loop:
        async def run(self, **kwargs: object) -> Stage4PlanningLoopResult:
            detailed = cast(Stage4PlanningLoopRequest, kwargs["request"])
            captured.append(detailed)
            receipt = AgentExecutionReceipt(
                receipt_id=StableId("receipt.public-stage4"),
                run_id=request.run_id,
                task_id=request.task_id,
                agent_spec=ContractRef(
                    contract_id=StableId("agent.planner"),
                    version=VERSION,
                    content_hash=HASH,
                ),
                agent_type=AgentType.PLANNER,
                agent_mode=AgentMode.STORY,
                prompt_fingerprint=HASH,
                configuration_fingerprint=HASH,
                base_commit=base,
                status=ExecutionStatus.SUCCEEDED,
                started_at=datetime(2026, 9, 14, tzinfo=UTC),
                completed_at=datetime(2026, 9, 14, tzinfo=UTC),
                latency_ms=1,
            )
            proposal = PlanProposal(
                proposal_id=StableId("proposal.public-stage4"),
                project_id=request.project_id,
                mode=AgentMode.STORY,
                base_commit=base,
                items=(
                    ProposedItem(
                        item_id=StableId("plan.story.public"),
                        kind="story",
                        payload={"summary": "Keep the tower entry within the author brief."},
                        provenance=ProposalProvenance.PLANNER_PROPOSED,
                    ),
                ),
                coverage=1.0,
                receipt=receipt,
            )
            plan_review_ref = artifacts.put(
                b'{"decision":"accept"}',
                "application/vnd.novel-agent.plan-review+json",
                VERSION,
            )
            return Stage4PlanningLoopResult(
                request_id=detailed.request_id,
                terminal=Stage4PlanningLoopTerminal.PLAN_CANDIDATE_READY,
                proposal=proposal,
                plan_review_ref=plan_review_ref,
            )

    adapter = Stage4PlanningLeafAdapter(
        cast(PlanningContextLoopService, _PublicStage4Loop()),
        artifacts,
        invocation_factory,
        schema_version=VERSION,
    )

    result = asyncio.run(adapter.run(request))

    assert result.status is PlanningTerminalStatus.PLAN_CANDIDATE_READY
    assert result.candidate is not None
    detailed = captured[0]
    assert detailed.author_intent_artifacts == (author_ref,)
    assert detailed.revision_artifact_refs == (directive_ref,)
    assert detailed.revision_parent_proposal_ref == candidate_ref
    assert detailed.revision_review_artifact_refs == (review_ref,)
    materialized = PlanProposal.model_validate_json(
        artifacts.read_verified(result.candidate.artifact_ref), strict=True
    )
    assert materialized.proposal_id == StableId("proposal.public-stage4")
    assert materialized.base_commit == base
    assert result.candidate.basis_snapshot == request.basis_snapshot
    assert any(
        ref.media_type == "application/vnd.novel-agent.plan-review+json"
        for ref in result.artifact_refs
    )


def test_runtime_revision_successor_uses_production_stage4_public_leaf(tmp_path: Path) -> None:
    """A reviewed rejection must reach the production Stage 4 leaf on generation two."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    commits = CommitService(session_factory)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "runtime-objects"))
    bundle = make_synthetic_bundle()
    text = next(item for item in bundle.text_roots if len(item.chapters) == 20)
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    text_ref = _put(artifacts, text, "application/vnd.novel-agent.text-root+json")
    world_ref = _put(artifacts, world, "application/vnd.novel-agent.world-root+json")
    plan_ref = _put(artifacts, plan, "application/vnd.novel-agent.plan-root+json")
    profile_ref = _put(
        artifacts,
        _profile(),
        "application/vnd.novel-agent.project-profile-root+json",
    )
    base = commits.initialize_project(
        make_manifest().model_copy(
            update={
                "text_root": TextRootRef(**text_ref.model_dump(mode="python")),
                "world_root": WorldRootRef(**world_ref.model_dump(mode="python")),
                "plan_root": PlanRootRef(**plan_ref.model_dump(mode="python")),
                "project_profile_root": ProjectProfileRootRef(
                    **profile_ref.model_dump(mode="python")
                ),
            }
        )
    )
    author_ref = artifacts.put(b"full author brief", "text/plain", VERSION)
    policy = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH.root,
        permission_hash=HASH.root,
        max_task_attempts=2,
    )
    events = RunEventLogRepository(session_factory)
    commands = RuntimeCommandService(session_factory, events, lambda _project_id: HASH.root)
    acceptance = RuntimeAcceptanceService(commands, commits, artifacts)
    loop_requests: list[Stage4PlanningLoopRequest] = []

    class _RuntimeStage4Loop:
        async def run(self, **kwargs: object) -> Stage4PlanningLoopResult:
            detailed = cast(Stage4PlanningLoopRequest, kwargs["request"])
            loop_requests.append(detailed)
            assert detailed.task.base_commit is not None
            receipt = AgentExecutionReceipt(
                receipt_id=StableId(f"receipt.runtime-stage4.{len(loop_requests)}"),
                run_id=detailed.run_id,
                task_id=detailed.task_id,
                agent_spec=ContractRef(
                    contract_id=StableId("agent.planner"),
                    version=VERSION,
                    content_hash=HASH,
                ),
                agent_type=AgentType.PLANNER,
                agent_mode=detailed.task.mode,
                prompt_fingerprint=HASH,
                configuration_fingerprint=HASH,
                base_commit=detailed.task.base_commit,
                status=ExecutionStatus.SUCCEEDED,
                started_at=datetime(2026, 9, 14, tzinfo=UTC),
                completed_at=datetime(2026, 9, 14, tzinfo=UTC),
                latency_ms=1,
            )
            proposal = PlanProposal(
                proposal_id=StableId(f"proposal.runtime-stage4.{len(loop_requests)}"),
                project_id=detailed.project_id,
                mode=detailed.task.mode,
                base_commit=detailed.task.base_commit,
                items=(
                    ProposedItem(
                        item_id=StableId("plan.story.runtime"),
                        kind="story",
                        payload={"summary": "Keep the revised story within the author brief."},
                        provenance=ProposalProvenance.PLANNER_PROPOSED,
                    ),
                ),
                coverage=1.0,
                receipt=receipt,
            )
            review_ref = artifacts.put(
                b'{"issues":[]}',
                "application/vnd.novel-agent.plan-review+json",
                VERSION,
            )
            return Stage4PlanningLoopResult(
                request_id=detailed.request_id,
                terminal=Stage4PlanningLoopTerminal.PLAN_CANDIDATE_READY,
                proposal=proposal,
                plan_review_ref=review_ref,
            )

    invocation_factory = ProductionStage4InvocationFactory(
        commits=commits,
        artifacts=artifacts,
        policy=Stage4InvocationPolicy(
            budgets=PlanningBudgets(
                retrieval=RetrievalBudget(max_full_chapter_reads=1),
                context=ContextBudget(token_budget=8_000),
            ),
            configuration_fingerprint=HASH,
            model_fingerprint=HASH,
        ),
    )
    planner = Stage4PlanningLeafAdapter(
        cast(PlanningContextLoopService, _RuntimeStage4Loop()),
        artifacts,
        invocation_factory,
        schema_version=VERSION,
    )

    def resolve_policy(policy_hash: str) -> CreativeRunPolicy:
        if policy_hash != policy.policy_hash:
            raise KeyError(policy_hash)
        return policy

    runtime = CreativeRuntimeService(
        commands,
        acceptance,
        commits,
        artifacts,
        planner,
        cast(WritingLeafPort, object()),
        lambda _task: cast(WritingLoopRequest, object()),
        StrictDeterministicCandidateMaterializer(commits, candidate_kind=CandidateKind.PLAN),
        StrictDeterministicCandidateMaterializer(commits, candidate_kind=CandidateKind.DRAFT),
        cast(DerivedProjectionService, object()),
        cast(DerivedSnapshotRepository, object()),
        resolve_policy,
    )
    start = runtime.start(
        CreativeRunRequest(
            run_id=RunId("run.stage4-runtime-successor"),
            project_id=ProjectId("project.test"),
            basis_commit=base,
            basis_snapshot=StableId("snapshot.chapter.20"),
            policy=policy,
            input_artifact_refs=(author_ref,),
            current_chapter=20,
            target_chapters=25,
            plan_level=PlanLevel.STORY,
        )
    )
    assert start.current_task_id is not None
    first_waiting = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner.1"))
    assert first_waiting.terminal.value == "WAITING_PLAN_ACCEPTANCE"
    assert first_waiting.current_task_id is not None
    first_acceptance = commands.get_task(first_waiting.current_task_id)
    first_candidate = runtime._candidate_for_task(first_acceptance)
    operator_review = OperatorReviewEvidence(
        review_id=StableId("operator-review.runtime-successor"),
        target_artifact_ref=first_candidate.artifact_ref,
        reviewer_id="operator.runtime",
        reason="the candidate needs one bounded revision",
        issues=(
            OperatorReviewFinding(
                issue_id=StableId("operator-finding.runtime-successor"),
                kind="unresolved_scope_missing",
                summary="scope is missing",
                affected_item_ids=(StableId("plan.story.runtime"),),
                field_path="unresolved.affected_chapters",
                expected="chapters 1-10",
            ),
        ),
    )
    review_ref = artifacts.put(
        canonical_json_bytes(operator_review.model_dump(mode="json")),
        "application/vnd.novel-agent.operator-plan-review+json",
        VERSION,
    )
    runtime.submit_acceptance(
        AcceptanceCommand(
            command_id=StableId("reject.runtime-successor"),
            project_id=first_acceptance.project_id,
            run_id=first_acceptance.run_id,
            task_id=first_acceptance.task_id,
            candidate=first_candidate,
            acceptance_policy_hash=policy.policy_hash,
            actor_kind=ActorKind.OPERATOR,
            actor_id="operator.runtime",
            decision=AcceptanceDecision.REJECT,
            reason="scope needs revision",
            expected_project_commit=base,
            idempotency_identity=StableId("reject.runtime-successor.identity"),
            issued_at=datetime(2026, 9, 14, tzinfo=UTC),
            review_artifact_refs=(review_ref,),
        ),
        policy=policy,
    )
    revised_task = commands.get_task(TaskId("run.stage4-runtime-successor.plan.story.g1"))
    assert revised_task.input_artifact_refs[:3] == (
        author_ref,
        first_candidate.artifact_ref,
        review_ref,
    )
    assert revised_task.input_artifact_refs[3].media_type == (
        "application/vnd.novel-agent.operator-revision-directive+json"
    )
    second_waiting = asyncio.run(runtime.advance(revised_task.task_id, worker_id="planner.2"))

    assert second_waiting.terminal.value == "WAITING_PLAN_ACCEPTANCE"
    assert len(loop_requests) == 2
    second_request = loop_requests[1]
    assert second_request.author_intent_artifacts == (author_ref,)
    assert second_request.revision_parent_proposal_ref == first_candidate.artifact_ref
    assert second_request.revision_review_artifact_refs == (review_ref,)
    assert len(second_request.revision_artifact_refs) == 1
    assert second_waiting.current_task_id is not None
    second_acceptance = commands.get_task(second_waiting.current_task_id)
    second_candidate = runtime._candidate_for_task(second_acceptance)
    PlanProposal.model_validate_json(
        artifacts.read_verified(second_candidate.artifact_ref), strict=True
    )

    second_operator_review = OperatorReviewEvidence(
        review_id=StableId("operator-review.runtime-successor.second"),
        target_artifact_ref=second_candidate.artifact_ref,
        reviewer_id="operator.runtime",
        reason="the revised candidate still needs one bounded revision",
        issues=(
            OperatorReviewFinding(
                issue_id=StableId("operator-finding.runtime-successor.second"),
                kind="unresolved_scope_missing",
                summary="scope remains missing",
                affected_item_ids=(StableId("plan.story.runtime"),),
                field_path="unresolved.affected_chapters",
                expected="chapters 1-10",
            ),
        ),
    )
    second_review_ref = artifacts.put(
        canonical_json_bytes(second_operator_review.model_dump(mode="json")),
        "application/vnd.novel-agent.operator-plan-review+json",
        VERSION,
    )
    runtime.submit_acceptance(
        AcceptanceCommand(
            command_id=StableId("reject.runtime-successor.second"),
            project_id=second_acceptance.project_id,
            run_id=second_acceptance.run_id,
            task_id=second_acceptance.task_id,
            candidate=second_candidate,
            acceptance_policy_hash=policy.policy_hash,
            actor_kind=ActorKind.OPERATOR,
            actor_id="operator.runtime",
            decision=AcceptanceDecision.REJECT,
            reason="scope still needs revision",
            expected_project_commit=base,
            idempotency_identity=StableId("reject.runtime-successor.second.identity"),
            issued_at=datetime(2026, 9, 14, tzinfo=UTC),
            review_artifact_refs=(second_review_ref,),
        ),
        policy=policy,
    )
    third_task = commands.get_task(TaskId("run.stage4-runtime-successor.plan.story.g2"))
    assert third_task.input_artifact_refs == (
        author_ref,
        second_candidate.artifact_ref,
        second_review_ref,
        third_task.input_artifact_refs[3],
    )
    assert third_task.input_artifact_refs[3].media_type == (
        "application/vnd.novel-agent.operator-revision-directive+json"
    )
    assert first_candidate.artifact_ref not in third_task.input_artifact_refs
    assert review_ref not in third_task.input_artifact_refs
    third_waiting = asyncio.run(runtime.advance(third_task.task_id, worker_id="planner.3"))

    assert third_waiting.terminal.value == "WAITING_PLAN_ACCEPTANCE"
    assert len(loop_requests) == 3
    third_request = loop_requests[2]
    assert third_request.author_intent_artifacts == (author_ref,)
    assert third_request.revision_parent_proposal_ref == second_candidate.artifact_ref
    assert third_request.revision_review_artifact_refs == (second_review_ref,)
    assert len(third_request.revision_artifact_refs) == 1
