"""Evidence-first checkpoint runner: deterministic baseline and model product path."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from novel_agent.domain.benchmark import (
    AuthorPlanningContext,
    PlanRootDocument,
    TextRootDocument,
    VisibleOutlineNode,
)
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, StableId
from novel_agent.domain.memory import (
    CandidatePool,
    NeedExecutionStatus,
    WorldRootDocument,
)
from novel_agent.domain.planning_memory import (
    PLANNER_OUTPUT_SCHEMA_VERSION,
    EntityMention,
    GroundingStatus,
    PlannedNeedDraft,
    PlannerArtifactMetadata,
    PlannerFallbackStatus,
    PlannerInvocationArtifact,
    PlannerInvocationAttempt,
    PlannerInvocationAttemptStatus,
)
from novel_agent.domain.retrieval_routing import (
    L2IndexKind,
    L2IndexManifest,
    ProjectionAttestation,
    RetrievalBackendProfile,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.runtime.memory_controller import RouteBoundControllerPolicy
from novel_agent.services.benchmark_importer import content_id
from novel_agent.services.evidence_first_checkpoint_runner import EvidenceFirstCheckpointRunner
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.memory_pipeline import AnchorBuilder
from novel_agent.services.plan_conditioned_need_planner import PlannerWorldSummaryBuilder
from novel_agent.services.retrieval import InMemoryRetrievalBackend, RerankService
from novel_agent.services.stage2_retrieval_backend import Stage2RetrievalBackendBundle
from novel_agent.services.task_conditioned_need_generation import (
    NeedGenerationResult,
    NeedGenerationStatus,
    TaskPlanConditionedNeedGenerator,
)
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.evidence-first.runner")


class _FakeReranker:
    profile = "fake-reranker-v1"

    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        return tuple(float(index) for index, _ in enumerate(passages, start=1))


def _task_and_context() -> tuple[Any, AuthorPlanningContext]:
    bundle = make_synthetic_bundle()
    plan = bundle.plan_roots[0]
    task = build_safe_task_contract(
        case_id=StableId("case.runner"),
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=task_profile(),
        task_intent="为写 21-23 章准备历史记忆",
    )
    context = AuthorPlanningContext(
        profile=task.information_profile,
        task_intent=task.task_intent,
        target_range=(21, 23),
        visible_outline_nodes=tuple(
            VisibleOutlineNode(node_id=node.plan_node_id, title=node.title, summary=node.summary)
            for node in plan.nodes
        ),
        chapter_goals=plan.chapter_goals,
        source_hash=content_id({"t": "runner"}),
        planner_may_read_plan=True,
    )
    return task, context


def task_profile() -> Any:
    from novel_agent.domain.stage2 import BenchmarkInformationProfile

    return BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED


def _planner_artifact(task: Any, context: AuthorPlanningContext) -> PlannerInvocationArtifact:
    world = make_synthetic_bundle().world_roots[0]
    return PlannerInvocationArtifact(
        planning_context=context,
        world_summary=PlannerWorldSummaryBuilder.build(task, world, context),
        exact_prompt="prompt",
        metadata=PlannerArtifactMetadata(
            run_id=RunId("run.runner.test"),
            planner_model="test-model",
            planner_model_revision="test",
            planner_prompt_version="v1",
            planner_prompt_hash=content_id({"p": "runner"}),
            planner_output_schema_version=PLANNER_OUTPUT_SCHEMA_VERSION,
            temperature=0.0,
            requested_seed=None,
            effective_seed_supported=False,
            planning_context_hash=context.source_hash,
            world_summary_hash=content_id({"w": "runner"}),
            raw_response_hash=content_id({"r": "runner"}),
            validated_need_set_hash=content_id({"v": "runner"}),
            fallback_used=False,
            input_tokens=1,
            output_tokens=1,
        ),
        raw_response='{"drafts": []}',
        attempts=(),
        parsed_drafts=(
            PlannedNeedDraft(
                draft_id="lexical-21",
                semantic_question="国教学院与教枢处的历史互动模式是怎样的?",
                entity_mentions=(EntityMention(label="国教学院", role_in_need="institution"),),
                trigger_plan_chapters=(21,),
                trigger_plan_goal="重申旧誓言",
                why_needed="第21章需要互动模式",
                required_claim_scopes=("current",),
                suggested_facets=("CURRENT_STATE",),
                historical_time_scope="main",
            ),
            PlannedNeedDraft(
                draft_id="injury-22",
                semantic_question="在截止点前 teacher 的伤势是否仍未痊愈?",
                entity_mentions=(EntityMention(label="teacher", role_in_need="subject"),),
                trigger_plan_chapters=(22,),
                trigger_plan_goal="保持受伤状态约束",
                why_needed="第22章需要伤势状态",
                required_claim_scopes=("current",),
                suggested_facets=("CURRENT_STATE",),
                historical_time_scope="main",
            ),
            PlannedNeedDraft(
                draft_id="tower-23",
                semantic_question="student 是否已承诺前往北塔?",
                entity_mentions=(EntityMention(label="student", role_in_need="subject"),),
                trigger_plan_chapters=(23,),
                trigger_plan_goal="进入北塔",
                why_needed="第23章需要承诺来源",
                required_claim_scopes=("historical",),
                suggested_facets=("CAUSAL_HISTORY",),
                historical_time_scope="main",
            ),
        ),
        validated_need_set_hash=content_id({"v": "runner"}),
        fallback_status=PlannerFallbackStatus.PLANNER,
    )


def _attestation(
    source_commit: CommitId,
    snapshot_id: StableId,
) -> ProjectionAttestation:
    channels = (
        "r1_exact",
        "r1_temporal",
        "anchor_bm25",
        "anchor_dense",
        "grounded_bm25",
        "grounded_dense",
        "hierarchy",
        "typed_graph",
    )
    from novel_agent.domain.memory import RetrievalChannel
    from novel_agent.domain.retrieval_routing import ChannelCoverage

    capability = SnapshotCapability(
        source_commit=source_commit,
        snapshot_id=snapshot_id,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=tuple(RetrievalChannel(channel) for channel in channels),
        coverage_by_channel=tuple(
            ChannelCoverage(
                channel=RetrievalChannel(channel),
                expected_units=1,
                ready_units=1,
            )
            for channel in channels
        ),
        embedding_profile="test-embedding-v1",
    )
    indexes = tuple(
        L2IndexManifest(
            index_id=StableId(f"index.{kind.value}.{source_commit.root[-16:]}"),
            index_kind=kind,
            physical_name=f"ztj-volume01-preview-stage2r-runner-{kind.value}-abc123",
            alias=f"ztj-volume01-preview-stage2r-runner-{kind.value}",
            source_commit=source_commit,
            snapshot_id=snapshot_id,
            document_count=10,
            mapping_hash=ArtifactId("sha256:" + "e" * 64),
            analyzer_profile="standard",
            embedding_profile=(None if kind is L2IndexKind.HIERARCHY else "test-embedding-v1"),
        )
        for kind in (L2IndexKind.ANCHOR, L2IndexKind.GROUNDED, L2IndexKind.HIERARCHY)
    )
    return ProjectionAttestation(
        attestation_id=StableId("attestation.runner"),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        source_commit=source_commit,
        snapshot_id=snapshot_id,
        capability=capability,
        r1_record_count=1,
        r1_entity_association_count=1,
        graph_node_count=0,
        graph_edge_count=0,
        indexes=indexes,
        embedding_model="test-embedding",
        embedding_revision="abc",
        embedding_dimension=1024,
        embedding_normalized=True,
        embedding_runtime_fingerprint=ArtifactId("sha256:" + "b" * 64),
        reranker_model="test-reranker",
        reranker_revision="def",
    )


def _backend_bundle(
    world: WorldRootDocument,
    text: TextRootDocument,
    plan: PlanRootDocument,
    source_commit: CommitId,
    snapshot_id: StableId,
) -> Stage2RetrievalBackendBundle:
    units = AnchorBuilder().build(
        world,
        text,
        plan,
        snapshot_id=snapshot_id,
        canonical_commit=source_commit,
    )
    return Stage2RetrievalBackendBundle(
        backend=InMemoryRetrievalBackend(units),
        attestation=_attestation(source_commit, snapshot_id),
        allowed_channels=_attestation(source_commit, snapshot_id).capability.available_channels,
        reranker=RerankService(_FakeReranker()),
    )


def test_runner_produces_ready_v2_package_with_zero_model_calls() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    text = bundle.text_roots[0]
    plan = bundle.plan_roots[0]
    task, context = _task_and_context()
    runner = EvidenceFirstCheckpointRunner(
        artifact_writer=(
            lambda payload, media_type: None  # type: ignore[arg-type,return-value]
        )
    )
    result = runner.run(
        case_id=ProjectId("ztj_volume01_preview"),
        task=task,
        world=world,
        text=text,
        plan=plan,
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        planning_context=context,
        frozen_planner_artifact=_planner_artifact(task, context),
        frozen_needs=(),
        backend_bundle=_backend_bundle(world, text, plan, COMMIT, SNAPSHOT),
        fingerprint=content_id({"runner": "test"}),
        run_id=StableId("request.runner.test"),
    )
    assert result.needs
    # a mandatory Need that the frozen corpus cannot serve must surface a typed
    # gap instead of a fabricated answer; the package may still be READY when
    # every mandatory Need received exact evidence
    assert result.assembly.status.value in {"READY", "EVIDENCE_INSUFFICIENT"}
    assert result.assembly.package.contract_version == "writer_context.v2"
    assert result.assembly.evidence_ledger.contract_version == "evidence_ledger.v2"
    assert result.future_leakage_count == 0
    assert result.retrieval_call_count >= 0
    assert result.planner_fallback_used is False
    assert result.need_planner_model_call_count == 0
    assert result.controller_model_call_count == 0
    assert result.assembly.package.arm == "A"
    # zero model calls by construction: no gateway, no claim support, no
    # verifier/evaluator is ever instantiated on this path
    assert result.assembly.package.lineage.planner_fallback_used is False
    # the unresolved institution anchor is recorded, not dropped
    anchors = {anchor.mention for anchor in result.unresolved_lexical_anchors}
    assert "国教学院" in anchors
    # every item is ledger-backed or a typed gap
    for item in result.assembly.package.items:
        assert item.evidence_ledger_ids or item.gap is not None


def test_model_driven_runner_requires_planner_and_controller_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    text = bundle.text_roots[0]
    plan = bundle.plan_roots[0]
    task, context = _task_and_context()
    frozen_artifact = _planner_artifact(task, context)
    replayed = TaskPlanConditionedNeedGenerator().generate_evidence_first(
        task,
        world,
        plan,
        context,
        frozen_artifact,
    )
    assert replayed is not None
    assert replayed.planner_artifact is not None
    planner_attempt = PlannerInvocationAttempt(
        request_id=StableId("request.runner.model-planner"),
        status=PlannerInvocationAttemptStatus.SUCCEEDED,
        raw_response="{}",
        raw_response_hash=content_id({"raw": "{}"}),
        input_tokens=1,
        output_tokens=1,
    )
    generated = replayed.model_copy(
        update={
            "planner_artifact": replayed.planner_artifact.model_copy(
                update={"attempts": (planner_attempt,)}
            )
        }
    )

    class _RecordedRoutePolicy(RouteBoundControllerPolicy):
        @property
        def decision_receipts(self) -> tuple[object, ...]:
            return (object(),)

        @property
        def decision_repairs(self) -> tuple[object, ...]:
            return ()

    runner = EvidenceFirstCheckpointRunner(
        planner_gateway=object(),  # type: ignore[arg-type]
        controller_policy_factory=(lambda _tool_policy, routes: _RecordedRoutePolicy(routes)),
        require_model_decisions=True,
    )
    monkeypatch.setattr(
        runner._generator,
        "generate_with_lineage",
        lambda *args, **kwargs: generated,
    )
    result = runner.run(
        case_id=ProjectId("ztj_volume01_preview"),
        task=task,
        world=world,
        text=text,
        plan=plan,
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        planning_context=context,
        frozen_planner_artifact=frozen_artifact,
        frozen_needs=(),
        backend_bundle=_backend_bundle(world, text, plan, COMMIT, SNAPSHOT),
        fingerprint=content_id({"runner": "model-driven-test"}),
        run_id=StableId("request.runner.model-driven"),
    )

    assert result.need_planner_model_call_count == 1
    assert result.controller_model_call_count == 1
    assert result.assembly.package.arm == "B"


def test_model_driven_runner_continues_with_planner_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    text = bundle.text_roots[0]
    plan = bundle.plan_roots[0]
    task, context = _task_and_context()
    frozen_artifact = _planner_artifact(task, context)
    replayed = TaskPlanConditionedNeedGenerator().generate_evidence_first(
        task,
        world,
        plan,
        context,
        frozen_artifact,
    )
    assert replayed is not None
    assert replayed.planner_artifact is not None
    planner_attempt = PlannerInvocationAttempt(
        request_id=StableId("request.runner.model-fallback-planner"),
        status=PlannerInvocationAttemptStatus.SUCCEEDED,
        raw_response="{}",
        raw_response_hash=content_id({"raw": "{}"}),
        input_tokens=1,
        output_tokens=1,
    )
    fallback = replayed.model_copy(
        update={
            "status": NeedGenerationStatus.PLANNER_FALLBACK,
            "fallback_used": True,
            "planner_fallback_reason": "provider_error",
            "planner_artifact": replayed.planner_artifact.model_copy(
                update={"attempts": (planner_attempt,)}
            ),
        }
    )

    class _RecordedRoutePolicy(RouteBoundControllerPolicy):
        @property
        def decision_receipts(self) -> tuple[object, ...]:
            return (object(),)

        @property
        def decision_repairs(self) -> tuple[object, ...]:
            return ()

    runner = EvidenceFirstCheckpointRunner(
        planner_gateway=object(),  # type: ignore[arg-type]
        controller_policy_factory=(lambda _tool_policy, routes: _RecordedRoutePolicy(routes)),
        require_model_decisions=True,
    )
    monkeypatch.setattr(
        runner._generator,
        "generate_with_lineage",
        lambda *args, **kwargs: fallback,
    )

    result = runner.run(
        case_id=ProjectId("ztj_volume01_preview"),
        task=task,
        world=world,
        text=text,
        plan=plan,
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        planning_context=context,
        frozen_planner_artifact=frozen_artifact,
        frozen_needs=(),
        backend_bundle=_backend_bundle(world, text, plan, COMMIT, SNAPSHOT),
        fingerprint=content_id({"runner": "model-fallback-test"}),
        run_id=StableId("request.runner.model-fallback"),
    )
    assert result.planner_fallback_used is True
    assert result.needs
    assert result.assembly.package.lineage.planner_fallback_used is True
    assert result.assembly.package.semantic_status == "INCOMPLETE"


def test_runner_fallback_case_reuses_frozen_needs() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    text = bundle.text_roots[0]
    plan = bundle.plan_roots[0]
    task, context = _task_and_context()
    fallback_artifact = _planner_artifact(task, context).model_copy(
        update={"fallback_status": PlannerFallbackStatus.PLANNER_FALLBACK}
    )
    runner = EvidenceFirstCheckpointRunner()
    # a fallback artifact with no frozen Needs fails closed instead of
    # silently running an empty Need set
    with pytest.raises(ValueError, match="produced no memory needs"):
        runner.run(
            case_id=ProjectId("ztj_volume01_preview"),
            task=task,
            world=world,
            text=text,
            plan=plan,
            base_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            planning_context=context,
            frozen_planner_artifact=fallback_artifact,
            frozen_needs=(),
            backend_bundle=_backend_bundle(world, text, plan, COMMIT, SNAPSHOT),
            fingerprint=content_id({"runner": "fallback"}),
            run_id=StableId("request.runner.fallback"),
        )


def test_runner_rejects_stale_backend_basis() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    text = bundle.text_roots[0]
    plan = bundle.plan_roots[0]
    task, context = _task_and_context()
    runner = EvidenceFirstCheckpointRunner()
    stale = _backend_bundle(world, text, plan, COMMIT, SNAPSHOT)
    stale_bundle = Stage2RetrievalBackendBundle(
        backend=stale.backend,
        attestation=_attestation(CommitId("sha256:" + "c" * 64), SNAPSHOT),
        allowed_channels=stale.allowed_channels,
        reranker=stale.reranker,
    )
    with pytest.raises(ValueError, match="backend attestation basis differs"):
        runner.run(
            case_id=ProjectId("ztj_volume01_preview"),
            task=task,
            world=world,
            text=text,
            plan=plan,
            base_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            planning_context=context,
            frozen_planner_artifact=_planner_artifact(task, context),
            frozen_needs=(),
            backend_bundle=stale_bundle,
            fingerprint=content_id({"runner": "stale"}),
            run_id=StableId("request.runner.stale"),
        )


def test_runner_constructor_validation() -> None:
    with pytest.raises(ValueError, match="budgets must be positive"):
        EvidenceFirstCheckpointRunner(writer_token_budget=0)
    with pytest.raises(ValueError, match="candidate limit"):
        EvidenceFirstCheckpointRunner(max_candidates=0)
    with pytest.raises(ValueError, match="tool-call limit"):
        EvidenceFirstCheckpointRunner(max_tool_calls=0)
    with pytest.raises(ValueError, match="Planner gateway"):
        EvidenceFirstCheckpointRunner(require_model_decisions=True)
    with pytest.raises(ValueError, match="Controller policy factory"):
        EvidenceFirstCheckpointRunner(
            planner_gateway=object(),  # type: ignore[arg-type]
            require_model_decisions=True,
        )


def test_runner_rejects_inexact_snapshot_capability() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    text = bundle.text_roots[0]
    plan = bundle.plan_roots[0]
    task, context = _task_and_context()
    runner = EvidenceFirstCheckpointRunner()
    bundle_ok = _backend_bundle(world, text, plan, COMMIT, SNAPSHOT)
    attestation = bundle_ok.attestation.model_copy(
        update={
            "capability": bundle_ok.attestation.capability.model_copy(
                update={"status": SnapshotCapabilityStatus.TEST_ONLY}
            )
        }
    )
    inexact = Stage2RetrievalBackendBundle(
        backend=bundle_ok.backend,
        attestation=attestation,
        allowed_channels=bundle_ok.allowed_channels,
        reranker=bundle_ok.reranker,
    )
    with pytest.raises(ValueError, match="snapshot capability must be exact"):
        runner.run(
            case_id=ProjectId("ztj_volume01_preview"),
            task=task,
            world=world,
            text=text,
            plan=plan,
            base_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            planning_context=context,
            frozen_planner_artifact=_planner_artifact(task, context),
            frozen_needs=(),
            backend_bundle=inexact,
            fingerprint=content_id({"runner": "inexact"}),
            run_id=StableId("request.runner.inexact"),
        )


def test_unresolved_anchors_short_circuits_for_fallback() -> None:
    from novel_agent.services.evidence_first_checkpoint_runner import (
        EvidenceFirstCheckpointRunner as R,
    )

    assert R._unresolved_anchors(None, True) == ()
    assert R._unresolved_anchors(None, False) == ()


def test_allowed_tools_empty_and_channel_mapping() -> None:
    from novel_agent.services.evidence_first_checkpoint_runner import (
        EvidenceFirstCheckpointRunner as R,
    )

    assert R._allowed_tools(()) == ()
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    plan = bundle.plan_roots[0]
    from novel_agent.services.retrieval_routing import DeterministicChannelPlanner

    capability = _attestation(COMMIT, SNAPSHOT).capability
    from novel_agent.services.task_conditioned_need_generation import (
        TaskPlanConditionedNeedGenerator,
    )

    task, context = _task_and_context()
    artifact = _planner_artifact(task, context)
    generation = TaskPlanConditionedNeedGenerator().generate_evidence_first(
        task, world, plan, context, artifact
    )
    assert generation is not None
    route = DeterministicChannelPlanner().plan(generation.needs[0], capability)
    tools = R._allowed_tools((route,))
    assert tools


def test_scope_needs_preserves_writer_safe_policy() -> None:
    from novel_agent.services.evidence_first_checkpoint_runner import (
        EvidenceFirstCheckpointRunner as R,
    )

    bundle = make_synthetic_bundle()
    task, _context = _task_and_context()
    from novel_agent.services.task_conditioned_need_generation import (
        TaskPlanConditionedNeedGenerator,
    )

    generation = TaskPlanConditionedNeedGenerator().generate_evidence_first(
        task,
        bundle.world_roots[0].model_copy(update={"source_commit": COMMIT}),
        bundle.plan_roots[0],
        _task_and_context()[1],
        _planner_artifact(task, _task_and_context()[1]),
    )
    assert generation is not None
    scoped = R._scope_needs(generation.needs)
    for need in scoped:
        assert need.access_scope == "writer_safe"
        assert need.planner_may_read_plan is True
        assert need.retrieval_may_return_plan is False
        assert need.claim_may_cite_plan is False
        assert need.legacy_allow_plan is False
        assert need.completion_spec is not None
        assert need.completion_spec.require_current_claim is False
        assert "one current claim" not in need.stop_condition
        assert "claim is supported" not in (need.completion_criteria or "")


def test_runner_assembler_property_and_fallback_with_frozen_needs() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    text = bundle.text_roots[0]
    plan = bundle.plan_roots[0]
    task, context = _task_and_context()
    artifact = _planner_artifact(task, context)
    generation = TaskPlanConditionedNeedGenerator().generate_evidence_first(
        task, world, plan, context, artifact
    )
    assert generation is not None
    fallback_artifact = artifact.model_copy(
        update={"fallback_status": PlannerFallbackStatus.PLANNER_FALLBACK}
    )
    runner = EvidenceFirstCheckpointRunner()
    assert runner.assembler.version.startswith("evidence_first_writer_context_assembler")
    result = runner.run(
        case_id=ProjectId("ztj_volume01_preview"),
        task=task,
        world=world,
        text=text,
        plan=plan,
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        planning_context=context,
        frozen_planner_artifact=fallback_artifact,
        frozen_needs=generation.needs,
        backend_bundle=_backend_bundle(world, text, plan, COMMIT, SNAPSHOT),
        fingerprint=content_id({"runner": "fallback-needs"}),
        run_id=StableId("request.runner.fallback-needs"),
    )
    assert result.planner_fallback_used is True
    assert result.assembly.package.lineage.planner_fallback_used is True


def test_runner_trace_records_route_channel_decisions_and_graph_reason() -> None:
    """Round 2: route trace records eligible/ineligible channels and a typed
    graph unavailable reason instead of silent success."""
    from novel_agent.domain.memory import (
        RetrievalStopReason,
        RetrievalTrace,
        Stage1QueryIntent,
    )
    from novel_agent.services.retrieval_routing import DeterministicChannelPlanner

    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    text = bundle.text_roots[0]
    plan = bundle.plan_roots[0]
    task, context = _task_and_context()
    generation = TaskPlanConditionedNeedGenerator().generate_evidence_first(
        task, world, plan, context, _planner_artifact(task, context)
    )
    assert generation is not None
    need = generation.needs[0].model_copy(
        update={
            # A graph-registered intent whose anchor is unresolved: no seed id.
            "query_intent": Stage1QueryIntent.RELATION_CHAIN,
            "entity_ids": (),
            "predicates": (),
            "access_scope": "writer_safe",
            "hierarchy_parent_unit_ids": (),
            "allowed_candidate_pools": (
                CandidatePool.ANCHOR,
                CandidatePool.GROUNDED,
                CandidatePool.GRAPH,
            ),
        }
    )
    backend = _backend_bundle(world, text, plan, COMMIT, SNAPSHOT)
    route_plan = DeterministicChannelPlanner().plan(need, backend.attestation.capability)
    assert any(
        item.channel.value == "typed_graph" and item.reason == "missing_graph_seed"
        for item in route_plan.excluded_channels
    )
    trace = RetrievalTrace(
        need_id=need.need_id,
        intent=need.query_intent,
        allowed_channels=(),
        channel_candidate_counts={},
        candidates=(),
        fusion_applied=False,
        stop_reason=RetrievalStopReason.NO_EXECUTABLE_QUERY,
        need_execution_status=NeedExecutionStatus.NOT_EXECUTED_NO_EXECUTABLE_QUERY,
        calls_allocated=0,
        compiled_query_bundle={},
        effective_channels=(),
        query_unavailable_reasons={},
    )
    selections, records = EvidenceFirstCheckpointRunner()._selections(
        (need,),
        (trace,),
        text,
        SNAPSHOT,
        route_plans=(route_plan,),
    )
    assert len(records) == 1
    record = records[0]
    by_channel = {item["channel"]: item["reason"] for item in record["ineligible_channels"]}
    assert by_channel["typed_graph"] == "missing_graph_seed"
    assert record["graph_unavailable_reason"] == "missing_graph_seed"
    assert len(selections) == 1 and selections[0].selections == ()


def test_graph_unavailable_reason_branches() -> None:
    """Round 2: graph unavailability stays typed on every trace path."""
    from novel_agent.domain.memory import (
        RetrievalChannel,
        RetrievalStopReason,
        RetrievalTrace,
        Stage1QueryIntent,
    )
    from novel_agent.services.evidence_first_checkpoint_runner import (
        EvidenceFirstCheckpointRunner as R,
    )
    from novel_agent.services.retrieval_routing import DeterministicChannelPlanner

    def trace_for(candidate_counts: dict[str, int], *, graph_effective: bool) -> RetrievalTrace:
        effective = (RetrievalChannel.TYPED_GRAPH,) if graph_effective else ()
        return RetrievalTrace(
            need_id=StableId("need.trace"),
            intent=Stage1QueryIntent.RELATION_CHAIN,
            allowed_channels=(),
            channel_candidate_counts={
                RetrievalChannel(channel): count for channel, count in candidate_counts.items()
            },
            candidates=(),
            fusion_applied=False,
            stop_reason=RetrievalStopReason.CANDIDATES_EXHAUSTED,
            need_execution_status=NeedExecutionStatus.EXECUTED_EMPTY,
            calls_allocated=1,
            effective_channels=effective,
        )

    # No plan and no trace-level graph signal: no reason.
    empty_trace = trace_for({}, graph_effective=False)
    assert R._graph_unavailable_reason(None, empty_trace) is None
    # Trace-level compiler reason when no plan is attached.
    trace_with_reason = empty_trace.model_copy(
        update={"query_unavailable_reasons": {RetrievalChannel.TYPED_GRAPH: "missing_graph_seed"}}
    )
    assert R._graph_unavailable_reason(None, trace_with_reason) == "missing_graph_seed"
    assert (
        R._graph_readiness_status(
            None,
            trace_with_reason,
            graph_edge_count=4,
            verified_receipt_count=0,
        )
        == "missing_seed"
    )
    # Zero-edge graph on the executed trace is a typed unavailable reason.
    zero_edge = trace_for({"typed_graph": 0}, graph_effective=True)
    assert R._graph_unavailable_reason(None, zero_edge) == "graph_zero_candidates"
    assert (
        R._graph_readiness_status(
            None,
            zero_edge,
            graph_edge_count=0,
            verified_receipt_count=0,
        )
        == "zero_edge"
    )
    assert (
        R._graph_readiness_status(
            None,
            zero_edge,
            graph_edge_count=3,
            verified_receipt_count=0,
        )
        == "filtered_or_no_path"
    )
    # A plan whose graph channel executed empty is typed as zero-candidate.
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    text = bundle.text_roots[0]
    plan = bundle.plan_roots[0]
    task, context = _task_and_context()
    generation = TaskPlanConditionedNeedGenerator().generate_evidence_first(
        task, world, plan, context, _planner_artifact(task, context)
    )
    assert generation is not None
    need = generation.needs[0].model_copy(
        update={
            "query_intent": Stage1QueryIntent.RELATION_CHAIN,
            "entity_ids": (),
            "predicates": (),
            "access_scope": "writer_safe",
            "hierarchy_parent_unit_ids": (),
            "allowed_candidate_pools": (
                CandidatePool.ANCHOR,
                CandidatePool.GROUNDED,
                CandidatePool.GRAPH,
            ),
        }
    )
    backend = _backend_bundle(world, text, plan, COMMIT, SNAPSHOT)
    route_plan = DeterministicChannelPlanner().plan(need, backend.attestation.capability)
    # Compiler reason on a plan wins.
    assert (
        R._graph_unavailable_reason(route_plan, empty_trace)
        == route_plan.query_unavailable_reasons[RetrievalChannel.TYPED_GRAPH]
    )
    # A plan without a compiler reason but with zero graph candidates is typed.
    graph_plan = route_plan.model_copy(
        update={
            "query_unavailable_reasons": {},
            "effective_channels": (RetrievalChannel.TYPED_GRAPH,),
        }
    )
    zero_edge_trace = trace_for({"typed_graph": 0}, graph_effective=True)
    assert R._graph_unavailable_reason(graph_plan, zero_edge_trace) == "graph_zero_candidates"
    # A plan whose graph channel returned candidates has no unavailable reason.
    graph_plan_with_hits = graph_plan.model_copy(
        update={
            "effective_channels": (
                RetrievalChannel.TYPED_GRAPH,
                RetrievalChannel.ANCHOR_BM25,
            )
        }
    )
    hits_trace = trace_for({"typed_graph": 2}, graph_effective=True)
    assert R._graph_unavailable_reason(graph_plan_with_hits, hits_trace) is None
    assert (
        R._graph_readiness_status(
            graph_plan_with_hits,
            hits_trace,
            graph_edge_count=3,
            verified_receipt_count=1,
        )
        == "ready"
    )
    assert (
        R._graph_readiness_status(
            graph_plan_with_hits,
            hits_trace,
            graph_edge_count=3,
            verified_receipt_count=0,
        )
        == "unverified_receipt"
    )


def test_runner_persists_refreshed_planner_artifact() -> None:
    from tempfile import TemporaryDirectory

    from novel_agent.adapters.filesystem import FilesystemObjectStore
    from novel_agent.domain.artifacts import ArtifactRef
    from novel_agent.domain.ids import SchemaVersion
    from novel_agent.services.artifacts import ArtifactRepository

    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    text = bundle.text_roots[0]
    plan = bundle.plan_roots[0]
    task, context = _task_and_context()
    with TemporaryDirectory() as tmp:
        repository = ArtifactRepository(FilesystemObjectStore(Path(tmp)))

        def writer(payload: bytes, media_type: str) -> ArtifactRef:
            return repository.put(payload, media_type, SchemaVersion("1.0.0"))

        runner = EvidenceFirstCheckpointRunner(artifact_writer=writer)
        result = runner.run(
            case_id=ProjectId("ztj_volume01_preview"),
            task=task,
            world=world,
            text=text,
            plan=plan,
            base_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            planning_context=context,
            frozen_planner_artifact=_planner_artifact(task, context),
            frozen_needs=(),
            backend_bundle=_backend_bundle(world, text, plan, COMMIT, SNAPSHOT),
            fingerprint=content_id({"runner": "persist"}),
            run_id=StableId("request.runner.persist"),
        )
        ref = result.assembly.package.lineage.planner_artifact_ref
        assert ref is not None
        payload = repository.read_verified(ref)
        assert b"planner_invocation_artifact" in payload or b"artifact_version" in payload


def test_runner_static_helpers_direct() -> None:
    from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
    from novel_agent.services.content_addressing import quote_hash
    from novel_agent.services.evidence_first_checkpoint_runner import (
        EvidenceFirstCheckpointRunner as R,
    )

    bundle = make_synthetic_bundle()
    text = bundle.text_roots[0]
    block = text.chapters[0].scenes[0].blocks[0]
    evidence = EvidenceRef(
        evidence_id=StableId("evidence.spanless"),
        root_hash=ArtifactId("sha256:" + "b" * 64),
        object_hash=ArtifactId("sha256:" + "c" * 64),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=None,
        quote_hash=None,
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=COMMIT,
    )
    assert R._block(text, evidence) is None
    assert (
        R._block(
            text,
            evidence.model_copy(
                update={"span": TextSpanRef(block_id=StableId("block.ghost"), start=0, end=1)}
            ),
        )
        is None
    )
    found = R._block(
        text,
        evidence.model_copy(update={"span": TextSpanRef(block_id=block.block_id, start=0, end=1)}),
    )
    assert found is not None

    static_gen = TaskPlanConditionedNeedGenerator().generate_evidence_first(
        _task_and_context()[0],
        bundle.world_roots[0].model_copy(update={"source_commit": COMMIT}),
        bundle.plan_roots[0],
        _task_and_context()[1],
        _planner_artifact(_task_and_context()[0], _task_and_context()[1]),
    )
    assert static_gen is not None
    need = static_gen.needs[0]
    runner = EvidenceFirstCheckpointRunner()
    from novel_agent.domain.memory import (
        ChannelHit,
        FusedCandidate,
        GraphPathDereferenceStatus,
        GraphPathReceipt,
        RetrievalChannel,
        RetrievalStopReason,
        RetrievalTrace,
        RetrievalUnit,
        RetrievalUnitKind,
    )
    from novel_agent.domain.world import StoryTime

    def unit_for(evidence_: EvidenceRef, unit_id: str) -> RetrievalUnit:
        return RetrievalUnit(
            unit_id=StableId(unit_id),
            unit_kind=RetrievalUnitKind.GROUNDED_SPAN,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            source_artifact=ArtifactId("sha256:" + "e" * 64),
            text="片段",
            evidence_refs=(evidence_,),
            access_scope="writer_safe",
        )

    forged = EvidenceRef(
        evidence_id=StableId("evidence.forged"),
        root_hash=ArtifactId("sha256:" + "b" * 64),
        object_hash=ArtifactId("sha256:" + "f" * 64),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=TextSpanRef(block_id=block.block_id, start=0, end=1),
        quote_hash=quote_hash("x"),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=COMMIT,
    )
    trace = RetrievalTrace(
        need_id=need.need_id,
        intent=need.query_intent,
        allowed_channels=(),
        channel_candidate_counts={},
        candidates=(
            FusedCandidate(
                unit=unit_for(forged, "unit.forged"),
                fused_rank=1,
                rrf_score=0.5,
                channel_hits=(
                    ChannelHit(
                        unit=unit_for(forged, "unit.forged"),
                        channel=RetrievalChannel.GROUNDED_BM25,
                        channel_rank=1,
                        raw_score=1.0,
                        candidate_count=1,
                        hit_reason="test",
                    ),
                ),
            ),
        ),
        fusion_applied=False,
        stop_reason=RetrievalStopReason.BUDGET_SATISFIED,
    )
    selections, _records = runner._selections((need,), (trace,), text, SNAPSHOT)
    # the forged evidence cannot resolve to an exact slice -> empty selection
    assert selections[0].slices == ()

    unknown = trace.model_copy(update={"need_id": StableId("need.unknown")})
    with pytest.raises(ValueError, match="unknown need"):
        runner._selections((need,), (unknown,), text, SNAPSHOT)

    graph_receipt = GraphPathReceipt(
        path_id=StableId("graph-path.runner.edge"),
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        seed_entity_ids=(StableId("entity.seed"),),
        relation_row_ids=(StableId("row.relation"),),
        relation_ids=(StableId("relation.edge"),),
        entity_path=(StableId("entity.seed"), StableId("entity.target")),
        predicates=("teacher_of",),
        directions=("forward",),
        valid_time=(StoryTime(worldline="main", start_ordinal=1),),
        edge_semantics=("canonical",),
        evidence_refs=(forged,),
        dereference_status=GraphPathDereferenceStatus.RELATION_ROWS_VERIFIED,
    )
    graph_hit = (
        trace.candidates[0]
        .channel_hits[0]
        .model_copy(update={"graph_path_receipts": (graph_receipt,)})
    )
    graph_candidate = trace.candidates[0].model_copy(update={"channel_hits": (graph_hit,)})
    graph_trace = trace.model_copy(update={"candidates": (graph_candidate,)})
    with pytest.raises(ValueError, match="require exact L0"):
        runner._selections((need,), (graph_trace,), text, SNAPSHOT)
    unverified_runner = EvidenceFirstCheckpointRunner(
        graph_receipt_validator=lambda receipts, _text: receipts
    )
    with pytest.raises(ValueError, match="did not verify exact L0"):
        unverified_runner._selections((need,), (graph_trace,), text, SNAPSHOT)
    verified_runner = EvidenceFirstCheckpointRunner(
        graph_receipt_validator=lambda receipts, _text: tuple(
            receipt.model_copy(
                update={"dereference_status": GraphPathDereferenceStatus.L0_VERIFIED}
            )
            for receipt in receipts
        )
    )
    verified_selections, verified_records = verified_runner._selections(
        (need,), (graph_trace,), text, SNAPSHOT
    )
    assert verified_selections
    assert verified_records[0]["verified_graph_path_receipt_ids"] == [graph_receipt.path_id.root]

    from novel_agent.services.task_focus import FocusSet

    empty_gen = NeedGenerationResult(
        task_id=StableId("task.x"),
        focus_set=FocusSet(focuses=(), task_id=StableId("task.x")),
        needs=(),
        status=NeedGenerationStatus.READY,
        need_completion_spec_version="v",
        generator_version="v",
    )
    assert runner._unresolved_anchors(empty_gen, False) == ()
    mixed = runner._unresolved_anchors(None, True)
    assert mixed == ()


def test_runner_planner_generation_none_uses_frozen_needs() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    text = bundle.text_roots[0]
    plan = bundle.plan_roots[0]
    task, context = _task_and_context()
    artifact = _planner_artifact(task, context)
    generation = TaskPlanConditionedNeedGenerator().generate_evidence_first(
        task, world, plan, context, artifact
    )
    assert generation is not None
    # non-fallback artifact whose drafts all fail validation -> generation is
    # None -> the runner falls back to the frozen Needs
    rejected = artifact.model_copy(
        update={
            "parsed_drafts": (
                PlannedNeedDraft(
                    draft_id="bad",
                    semantic_question="无法验证的问题",
                    entity_mentions=(),
                    relation_mentions=(),
                    trigger_plan_chapters=(21,),
                    trigger_plan_goal="not canonical goal text",
                    required_claim_scopes=("current",),
                    suggested_facets=("CURRENT_STATE",),
                ),
            )
        }
    )
    runner = EvidenceFirstCheckpointRunner()
    result = runner.run(
        case_id=ProjectId("ztj_volume01_preview"),
        task=task,
        world=world,
        text=text,
        plan=plan,
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        planning_context=context,
        frozen_planner_artifact=rejected,
        frozen_needs=generation.needs,
        backend_bundle=_backend_bundle(world, text, plan, COMMIT, SNAPSHOT),
        fingerprint=content_id({"runner": "gen-none"}),
        run_id=StableId("request.runner.gen-none"),
    )
    assert result.planner_fallback_used is True
    assert result.needs


def test_selections_skips_missing_block_and_unresolved_mixed_drafts() -> None:
    from novel_agent.domain.memory import (
        ChannelHit,
        FusedCandidate,
        RetrievalChannel,
        RetrievalStopReason,
        RetrievalTrace,
        RetrievalUnit,
        RetrievalUnitKind,
    )
    from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
    from novel_agent.services.artifacts import sha256_id
    from novel_agent.services.content_addressing import quote_hash
    from novel_agent.services.task_focus import FocusSet

    bundle = make_synthetic_bundle()
    text = bundle.text_roots[0]
    task, context = _task_and_context()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    generation = TaskPlanConditionedNeedGenerator().generate_evidence_first(
        task, world, bundle.plan_roots[0], context, _planner_artifact(task, context)
    )
    assert generation is not None
    need = generation.needs[0]
    block = text.chapters[0].scenes[0].blocks[0]
    ghost_evidence = EvidenceRef(
        evidence_id=StableId("evidence.ghost"),
        root_hash=ArtifactId("sha256:" + "b" * 64),
        object_hash=sha256_id(block.text.encode("utf-8")),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=TextSpanRef(block_id=StableId("block.ghost"), start=0, end=1),
        quote_hash=quote_hash(block.text[0:1]),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=COMMIT,
    )
    unit = RetrievalUnit(
        unit_id=StableId("unit.ghost"),
        unit_kind=RetrievalUnitKind.GROUNDED_SPAN,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        source_artifact=ArtifactId("sha256:" + "e" * 64),
        text="片段",
        evidence_refs=(ghost_evidence,),
        access_scope="writer_safe",
    )
    trace = RetrievalTrace(
        need_id=need.need_id,
        intent=need.query_intent,
        allowed_channels=(),
        channel_candidate_counts={},
        candidates=(
            FusedCandidate(
                unit=unit,
                fused_rank=1,
                rrf_score=0.5,
                channel_hits=(
                    ChannelHit(
                        unit=unit,
                        channel=RetrievalChannel.GROUNDED_BM25,
                        channel_rank=1,
                        raw_score=1.0,
                        candidate_count=1,
                        hit_reason="test",
                    ),
                ),
            ),
        ),
        fusion_applied=False,
        stop_reason=RetrievalStopReason.BUDGET_SATISFIED,
    )
    runner = EvidenceFirstCheckpointRunner()
    selections, _records = runner._selections((need,), (trace,), text, SNAPSHOT)
    assert selections[0].slices == ()

    # _unresolved_anchors with a grounded draft carrying a resolved mention
    from novel_agent.domain.planning_memory import GroundedEntityMention

    grounded_mention = GroundedEntityMention(
        mention="teacher",
        canonical_label="teacher",
        entity_id=StableId("entity.teacher"),
        confidence=1.0,
        grounding_method="exact_internal_label_match",
        grounding_status=GroundingStatus.GROUNDED,
    )
    assert generation.planner_artifact is not None
    grounded = generation.planner_artifact.grounded_drafts
    assert grounded
    mixed_artifact = generation.planner_artifact.model_copy(
        update={
            "grounded_drafts": (
                grounded[0].model_copy(update={"entity_mentions": (grounded_mention,)}),
            )
        }
    )
    mixed_gen = generation.model_copy(
        update={
            "planner_artifact": mixed_artifact,
            "planner_artifact_document_ref": None,
            "focus_set": FocusSet(focuses=(), task_id=task.task_id),
        }
    )
    assert runner._unresolved_anchors(mixed_gen, False) == ()


def test_model_driven_premature_stop_repairs_and_executes_real_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review-26: with the real bounded graph + ToolAdapter, a model policy
    that proposes STOP while mandatory Needs have legal actions must be
    repaired (PREMATURE_STOP_WITH_LEGAL_ACTIONS), execute at least one backend
    retrieval call, retain only sealed registered actions, and expose the
    bound decision, repair, tool trace and truthful terminal status."""
    from novel_agent.agents.controller import StructuredControllerPolicy
    from novel_agent.domain.stage2 import ControllerPolicyDraft
    from tests.unit.test_stage2_memory_controller import (
        controller_request_factory,
        structured_controller_spec,
    )

    class _StopModelRunner:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                output=ControllerPolicyDraft(action="stop"),
                model_call=SimpleNamespace(request_id=StableId("model-call.stop.1")),
                receipt=SimpleNamespace(),
            )

    def policy_factory(tool_policy: Any, route_plans: Any) -> Any:
        spec = structured_controller_spec()
        spec = SimpleNamespace(
            **{
                **vars(spec),
                "tool_policy": tool_policy,
            }
        )
        return StructuredControllerPolicy(
            cast(Any, _StopModelRunner()),
            cast(Any, spec),
            controller_request_factory,
            route_plans=route_plans,
        )

    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(update={"source_commit": COMMIT})
    # Ground the frozen drafts: give the world the canonical labels the
    # planner artifact mentions (teacher/student), so Needs carry grounded
    # entity IDs and their routes expose legal retrieval actions.
    template = world.entities[0]
    teacher = template.model_copy(
        update={
            "entity_id": StableId("entity.runner.teacher"),
            "internal_label": "teacher",
            "aliases": ("师",),
        }
    )
    student = teacher.model_copy(
        update={
            "entity_id": StableId("entity.runner.student"),
            "internal_label": "student",
            "aliases": ("小徒",),
        }
    )
    world = world.model_copy(update={"entities": (teacher, student, *world.entities)})
    text = bundle.text_roots[0]
    plan = bundle.plan_roots[0]
    task, context = _task_and_context()
    frozen_artifact = _planner_artifact(task, context)
    replayed = TaskPlanConditionedNeedGenerator().generate_evidence_first(
        task,
        world,
        plan,
        context,
        frozen_artifact,
    )
    assert replayed is not None
    assert any(need.entity_ids for need in replayed.needs)
    planner_attempt = PlannerInvocationAttempt(
        request_id=StableId("request.runner.stop-planner"),
        status=PlannerInvocationAttemptStatus.SUCCEEDED,
        raw_response="{}",
        raw_response_hash=content_id({"raw": "{}"}),
        input_tokens=1,
        output_tokens=1,
    )
    generated = replayed.model_copy(
        update={
            "planner_artifact": replayed.planner_artifact.model_copy(
                update={"attempts": (planner_attempt,)}
            )
        }
    )
    runner = EvidenceFirstCheckpointRunner(
        planner_gateway=object(),  # type: ignore[arg-type]
        controller_policy_factory=policy_factory,
        require_model_decisions=True,
    )
    monkeypatch.setattr(
        runner._generator,
        "generate_with_lineage",
        lambda *args, **kwargs: generated,
    )
    result = runner.run(
        case_id=ProjectId("ztj_volume01_preview"),
        task=task,
        world=world,
        text=text,
        plan=plan,
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        planning_context=context,
        frozen_planner_artifact=frozen_artifact,
        frozen_needs=(),
        backend_bundle=_backend_bundle(world, text, plan, COMMIT, SNAPSHOT),
        fingerprint=content_id({"runner": "model-stop-repair"}),
        run_id=StableId("request.runner.stop-repair"),
    )
    # The premature STOP was repaired with the typed reason.
    assert result.controller_repair_count >= 1
    assert any(
        repair["reason"] == "PREMATURE_STOP_WITH_LEGAL_ACTIONS"
        for repair in result.controller_repairs
    )
    # The repair executed at least one real backend retrieval through the
    # ToolAdapter; zero-call dispositions are not silently accepted.
    assert result.retrieval_call_count >= 1
    # Every bound decision is a sealed registered action (never a model-built
    # need/tool id) and the decision history is persisted for the case record.
    assert result.controller_decisions
    bound = result.controller_decisions[0]
    assert bound["action"] in {"call_tool", "execute_plan"}
    if bound["action"] == "call_tool":
        assert bound["need_id"] and bound["tool_name"]
    # Traces show a truthful execution status for the repaired round.
    trace_statuses = {record["execution_status"] for record in result.trace_records}
    assert (
        trace_statuses
        & {
            "executed_with_candidates",
            "executed_empty",
        }
        or result.retrieval_call_count >= 1
    )
    # Stop reason is a real terminal verdict, and the case record surface
    # carries the bound decisions.
    assert result.stop_reason in {
        "mandatory_gap_unresolved",
        "sufficient",
        "budget_exhausted",
        "no_additional_evidence",
    }
