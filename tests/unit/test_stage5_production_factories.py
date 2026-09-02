from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.runtime.stage3_writer import (
    ProductionWritingRequestFactory,
    Stage2MWriterContextInvocation,
    WritingRequestPolicy,
)
from novel_agent.adapters.runtime.stage4_planner import (
    ProductionStage4InvocationFactory,
    Stage4InvocationPolicy,
)
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.benchmark import ChapterGoal, TextRootDocument
from novel_agent.domain.creative_runtime import PlanningLoopRequest
from novel_agent.domain.generation import WritingLengthPolicy, WritingLoopBudgets
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRole
from novel_agent.domain.planning import PlanningBudgets
from novel_agent.domain.runtime import TaskKind, TaskRecord, TaskStatus
from novel_agent.domain.stage2 import (
    AgentMode,
    ContextBudget,
    ContractRef,
    ProjectProfileRootDocument,
    PromptContractRef,
    RetrievalBudget,
    SkillContractRef,
)
from novel_agent.domain.writer_context import ContextAssemblyStatus, WriterContextPackageV2
from novel_agent.domain.writing_loop import WRITING_LOOP_CHECKPOINT_MEDIA_TYPE
from novel_agent.runtime.production_components import (
    ProductionCuratorModelRequestFactory,
    ProductionReactiveMemoryInputsFactory,
    ProductionWriterModelRequestFactory,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, plan_root_content_id
from novel_agent.services.evidence_first_writer_context_assembler import (
    EvidenceFirstAssemblyResult,
    EvidenceFirstWriterContextAssembler,
    NeedEvidenceSelection,
    SliceSelectionTrace,
)
from novel_agent.services.evidence_slice_resolver import EvidenceSliceResolver
from novel_agent.services.recent_prose import RecentProseAssembler
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
) -> tuple[ArtifactRepository, CommitService, CommitId, TextRootDocument]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    commits = CommitService(build_session_factory(engine))
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    bundle = make_synthetic_bundle()
    text = next(item for item in bundle.text_roots if len(item.chapters) == 20)
    world = bundle.world_roots[0]
    original_plan = bundle.plan_roots[0]
    goal = ChapterGoal(
        goal_id=StableId("plan.chapter.21"),
        chapter_index=21,
        summary="Enter the tower while protecting the injured arm.",
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
    return artifacts, commits, base, text


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
    artifacts, commits, base, _text = _canonical(tmp_path)
    snapshot = StableId("snapshot.chapter.20")
    run_id = RunId("run.production-writer")
    old_checkpoint_ref = artifacts.put(
        b'{"checkpoint":"old"}', WRITING_LOOP_CHECKPOINT_MEDIA_TYPE, VERSION
    )
    unrelated_ref = artifacts.put(b"terminal", "application/json", VERSION)
    checkpoint_ref = artifacts.put(
        b'{"checkpoint":"latest"}', WRITING_LOOP_CHECKPOINT_MEDIA_TYPE, VERSION
    )
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
        terminal_artifact_refs=(old_checkpoint_ref, unrelated_ref, checkpoint_ref),
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
                ),
            ),
            text_root=invocation.text,
            basis_commit_id=invocation.base_commit,
            basis_snapshot_id=invocation.snapshot_id,
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
    )(task)

    assert request.writing_task.target_chapter == 21
    assert request.writing_task.chapter_goal.startswith("Enter the tower")
    assert request.writing_task.pov == "Lin"
    assert isinstance(request.writer_context_package, WriterContextPackageV2)
    assert request.recent_prose_context.previous_chapter is not None
    assert request.recent_prose_context.previous_chapter.chapter_index == 20
    assert request.future_isolation_attestation.evaluator_only_source_ids == ()
    assert request.resume_checkpoint_ref == checkpoint_ref
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


def test_production_writing_factory_accepts_multiple_goals_for_one_chapter(
    tmp_path: Path,
) -> None:
    artifacts, commits, base, _text = _canonical(
        tmp_path,
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
    run_id = RunId("run.production-writer-multi-goal")
    task = TaskRecord(
        task_id=TaskId("task.production-writer-multi-goal"),
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
                            unit_id=StableId("unit.production-writer-multi-goal"),
                            route_channel="r1_exact",
                            fused_rank=1,
                            selection_reason="production factory multi-goal evidence",
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
        return result

    request = ProductionWritingRequestFactory(
        commits=commits,
        artifacts=artifacts,
        recent_prose=RecentProseAssembler(artifacts, VERSION),
        writer_context=stage2m,
        policy=_writing_policy(),
        schema_version=VERSION,
    )(task)

    assert request.writing_task.chapter_goal == (
        "Enter the tower while protecting the injured arm.；"
        "Keep the injured arm out of the inner ward."
    )
    assert request.writing_task.active_plan_obligations == (StableId("obligation.arm"),)
    assert "Keep the injured arm out of the inner ward." in request.writing_task.required_beats


def test_production_writing_factory_preserves_semantic_gaps_and_rejects_complete_rewrite(
    tmp_path: Path,
) -> None:
    artifacts, commits, base, _text = _canonical(tmp_path)
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
    )
    request = factory(task)
    assert isinstance(request.writer_context_package, WriterContextPackageV2)
    assert request.writer_context_package.semantic_status == "INCOMPLETE"
    assert request.writer_context_package.usable_with_gaps is True
    assert request.writer_context_package.unclosed_mandatory_need_facets == (unclosed,)

    def not_ready(
        invocation: Stage2MWriterContextInvocation,
    ) -> EvidenceFirstAssemblyResult:
        result = stage2m(invocation)
        package = result.package.model_copy(
            update={"assembly_status": ContextAssemblyStatus.EVIDENCE_INSUFFICIENT.value}
        )
        return result.model_copy(
            update={"status": ContextAssemblyStatus.EVIDENCE_INSUFFICIENT, "package": package}
        )

    waiting = ProductionWritingRequestFactory(
        commits=commits,
        artifacts=artifacts,
        recent_prose=RecentProseAssembler(artifacts, VERSION),
        writer_context=not_ready,
        policy=_writing_policy(),
        schema_version=VERSION,
    )(task)
    assert isinstance(waiting.writer_context_package, WriterContextPackageV2)
    assert waiting.writer_context_package.assembly_status == (
        ContextAssemblyStatus.EVIDENCE_INSUFFICIENT.value
    )

    def complete_rewrite(
        invocation: Stage2MWriterContextInvocation,
    ) -> EvidenceFirstAssemblyResult:
        result = stage2m(invocation)
        rewritten = result.package.model_copy(
            update={
                "semantic_status": "COMPLETE",
                "usable_with_gaps": True,
                "unclosed_mandatory_need_facets": (unclosed,),
            }
        )
        return result.model_copy(update={"package": rewritten, "semantic_status": "COMPLETE"})

    with pytest.raises(ValueError, match="semantic incompleteness"):
        ProductionWritingRequestFactory(
            commits=commits,
            artifacts=artifacts,
            recent_prose=RecentProseAssembler(artifacts, VERSION),
            writer_context=complete_rewrite,
            policy=_writing_policy(),
            schema_version=VERSION,
        )(task)


def test_production_stage4_factory_builds_chapter_set_horizon_from_runtime_task(
    tmp_path: Path,
) -> None:
    artifacts, commits, base, _text = _canonical(tmp_path)
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
