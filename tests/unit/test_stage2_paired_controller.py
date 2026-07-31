from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from novel_agent.domain.benchmark import TextRootDocument
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
    ChannelHit,
    FusedCandidate,
    NeedExecutionStatus,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalChannel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.retrieval_routing import (
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    ContextBudget,
    ControllerArm,
    ControllerStopReason,
    MemoryResolutionRequest,
    PairedContextComparison,
    RequiredSnapshotPolicy,
    RetrievalBudget,
    ToolPolicy,
)
from novel_agent.runtime.memory_controller import RouteBoundControllerPolicy
from novel_agent.services.memory_pipeline import ContextCompiler, EvidenceExpander
from novel_agent.services.paired_controller import PairedMemoryControllerRunner
from novel_agent.services.retrieval import InMemoryRetrievalBackend, RerankService
from novel_agent.services.retrieval_routing import DeterministicChannelPlanner

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.paired")
CONFIG = ArtifactId("sha256:" + "c" * 64)
PRIVATE = ArtifactId("sha256:" + "f" * 64)
VERSION = SchemaVersion("2.0.0")


def need(
    *,
    intent: Stage1QueryIntent = Stage1QueryIntent.CURRENT_STATE,
) -> Stage1MemoryNeed:
    return Stage1MemoryNeed(
        need_id=StableId("need.paired"),
        run_id=RunId("run.paired"),
        task_id=TaskId("task.paired"),
        base_commit=COMMIT,
        chapter_target=20,
        need_type="continuity",
        query_intent=intent,
        query_text="hero injury",
        why_needed="paired controller comparison",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
        allowed_candidate_pools=(CandidatePool.R1,),
        stop_condition="current state found",
    )


def request(
    item: Stage1MemoryNeed,
    *,
    max_calls: int = 12,
    allow_future_plan: bool = False,
) -> MemoryResolutionRequest:
    return MemoryResolutionRequest(
        request_id=StableId("request.paired"),
        run_id=item.run_id,
        task_id=item.task_id,
        project_id=ProjectId("project.paired"),
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
        task_contract="prepare chapter 20",
        initial_memory_needs=(item,),
        worldline="main",
        narrative_chapter=20,
        access_scope=(AccessScope.EVALUATOR if allow_future_plan else AccessScope.WRITER_SAFE),
        allow_future_plan=allow_future_plan,
        retrieval_budget=RetrievalBudget(
            max_rounds=3,
            max_tool_calls=max_calls,
            max_candidates=20,
        ),
        context_budget=ContextBudget(token_budget=1000),
    )


def text_root() -> TextRootDocument:
    return TextRootDocument(root_hash=CONFIG, schema_version=VERSION, chapters=())


def runner(
    item: Stage1MemoryNeed,
    unit: RetrievalUnit,
    *,
    fresh: bool = True,
) -> PairedMemoryControllerRunner:
    backend = InMemoryRetrievalBackend((unit,))
    policy = ToolPolicy(
        policy_id=StableId("policy.paired"),
        version=VERSION,
        content_hash=CONFIG,
        allowed_tools=("memory.search_exact", "memory.search_temporal"),
        max_rounds=3,
        max_tool_calls=12,
    )
    return PairedMemoryControllerRunner.from_shared_backend(
        backend=backend,
        needs=(item,),
        tool_policy=policy,
        compiler=ContextCompiler(EvidenceExpander()),
        controller_policy=RouteBoundControllerPolicy(),
        freshness_check=lambda _: fresh,
        checkpointer=InMemorySaver(),
        comparison_basis_fingerprint=CONFIG,
    )


def unit(*, source_artifact: ArtifactId = CONFIG) -> RetrievalUnit:
    return RetrievalUnit(
        unit_id=StableId("anchor.hero.injury"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        source_artifact=source_artifact,
        text="hero injury remains",
        mandatory=True,
    )


def test_paired_runner_uses_shared_basis_and_budgets_for_both_arms() -> None:
    item = need()
    result = runner(item, unit()).run(request(item), text_root(), thread_id="paired-success")

    assert result.comparable is True
    assert result.blockers == ()
    assert result.deterministic.arm is ControllerArm.DETERMINISTIC
    assert result.agentic.arm is ControllerArm.BOUNDED_R2
    assert result.deterministic.context.base_commit == result.agentic.context.base_commit
    assert result.deterministic.selected_unit_ids == result.agentic.selected_unit_ids
    assert result.deterministic.retrieval_call_count == 2
    assert result.agentic.retrieval_call_count == 1
    assert result.agentic.stop_reason is ControllerStopReason.SUFFICIENT


def test_paired_runner_enforces_budget_and_freshness_without_hidden_backend_calls() -> None:
    item = need()
    budgeted = runner(item, unit()).run(
        request(item, max_calls=1), text_root(), thread_id="paired-budget"
    )
    assert budgeted.deterministic.retrieval_call_count == 1
    assert budgeted.deterministic.stop_reason is ControllerStopReason.BUDGET_EXHAUSTED
    stale = runner(item, unit(), fresh=False).run(
        request(item), text_root(), thread_id="paired-stale"
    )
    assert stale.deterministic.retrieval_call_count == 0
    assert stale.agentic.retrieval_call_count == 0
    assert stale.deterministic.stop_reason is ControllerStopReason.FRESHNESS_BLOCKED
    assert stale.agentic.stop_reason is ControllerStopReason.FRESHNESS_BLOCKED


def test_paired_runner_fails_closed_to_deterministic_context_on_agentic_timeout() -> None:
    item = need()
    paired = runner(item, unit())
    paired._controller.resolve = MagicMock(side_effect=TimeoutError)  # type: ignore[method-assign]

    result = paired.run(request(item), text_root(), thread_id="paired-timeout")

    assert result.comparable is False
    assert result.blockers == ("agentic_controller_timeout",)
    assert result.agentic.arm is ControllerArm.BOUNDED_R2
    assert result.agentic.context == result.deterministic.context
    assert result.agentic.selected_unit_ids == result.deterministic.selected_unit_ids
    assert result.agentic.retrieval_call_count == 12
    assert result.agentic.stop_reason is ControllerStopReason.TOOL_FAILURE


def test_compare_marks_deterministic_quality_ineligible_as_blocker() -> None:
    item = need()
    base = runner(item, unit())
    comparison = base.run(
        request(item),
        text_root(),
        thread_id="paired-deterministic-ineligible",
    )
    deterministic = comparison.deterministic.model_copy(
        update={
            "quality_eligible": False,
            "failure_category": "CONTEXT_NOT_READY",
        }
    )
    result = base.compare(request(item), deterministic, comparison.agentic)
    assert "deterministic_arm_quality_ineligible" in result.blockers


def test_paired_runner_applies_plan_permission_to_deterministic_and_agentic_arms() -> None:
    item = need(intent=Stage1QueryIntent.PLAN_NODE)
    result = runner(item, unit()).run(request(item), text_root(), thread_id="paired-plan-block")
    assert result.deterministic.selected_unit_ids == ()
    assert result.agentic.selected_unit_ids == ()
    assert result.deterministic.stop_reason is ControllerStopReason.MANDATORY_GAP_UNRESOLVED


def test_paired_runner_marks_any_evaluator_artifact_leak_as_non_comparable() -> None:
    item = need()
    result = runner(item, unit(source_artifact=PRIVATE)).run(
        request(item),
        text_root(),
        thread_id="paired-leak",
        evaluator_only_artifacts=(PRIVATE,),
    )
    assert result.comparable is False
    assert result.deterministic.future_leakage_count == 1
    assert result.agentic.future_leakage_count == 1
    assert len(result.blockers) == 2
    trace = result.agentic.context.retrieval_traces[0]
    rejected = trace.candidates[0].model_copy(
        update={"selected": False, "rejection_reason": "test rejection"}
    )
    rejected_context = result.agentic.context.model_copy(
        update={"retrieval_traces": (trace.model_copy(update={"candidates": (rejected,)}),)}
    )
    assert PairedMemoryControllerRunner._leakage_count(rejected_context, (PRIVATE,)) == 0


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("deterministic_arm", "wrong controller"),
        ("agentic_arm", "wrong controller"),
        ("basis", "canonical and snapshot"),
        ("budget", "token budget"),
        ("fingerprint", "comparison configuration"),
        ("comparable", "contradicts"),
    ),
)
def test_paired_comparison_contract_rejects_unfair_or_contradictory_results(
    mutation: str,
    message: str,
) -> None:
    item = need()
    result = runner(item, unit()).run(
        request(item), text_root(), thread_id=f"paired-invalid-{mutation}"
    )
    payload = result.model_dump()
    if mutation == "deterministic_arm":
        payload["deterministic"]["arm"] = ControllerArm.BOUNDED_R2
    elif mutation == "agentic_arm":
        payload["agentic"]["arm"] = ControllerArm.DETERMINISTIC
    elif mutation == "basis":
        payload["agentic"]["context"]["snapshot_id"] = StableId("snapshot.other")
    elif mutation == "budget":
        payload["agentic"]["context"]["budget_report"]["token_budget"] = 999
    elif mutation == "fingerprint":
        payload["agentic"]["comparison_basis_fingerprint"] = PRIVATE
    else:
        payload["comparable"] = False
    with pytest.raises(ValidationError, match=message):
        PairedContextComparison.model_validate(payload)


def test_paired_runner_rejects_duplicate_routes_and_invalid_comparison_inputs() -> None:
    item = need()
    base = runner(item, unit())
    assert base.comparison_basis_fingerprint == CONFIG
    semantic = item.model_copy(
        update={
            "query_intent": Stage1QueryIntent.RELATED_EVENT,
            "allowed_candidate_pools": (
                CandidatePool.ANCHOR,
                CandidatePool.GROUNDED,
            ),
        }
    )
    capability = SnapshotCapability(
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=(
            RetrievalChannel.ANCHOR_BM25,
            RetrievalChannel.ANCHOR_DENSE,
            RetrievalChannel.GROUNDED_BM25,
            RetrievalChannel.GROUNDED_DENSE,
        ),
    )
    plan = DeterministicChannelPlanner().plan(semantic, capability)
    with pytest.raises(ValueError, match="unique memory need ids"):
        PairedMemoryControllerRunner(
            base._backend,
            base._controller,
            base._compiler,
            CONFIG,
            lambda _: True,
            (plan, plan),
        )

    comparison = base.run(
        request(item),
        text_root(),
        thread_id="paired-invalid-compare-inputs",
    )
    with pytest.raises(ValueError, match="deterministic input"):
        base.compare(
            request(item),
            comparison.agentic,
            comparison.agentic,
        )
    with pytest.raises(ValueError, match="agentic input"):
        base.compare(
            request(item),
            comparison.deterministic,
            comparison.deterministic,
        )
    with pytest.raises(ValueError, match="comparison basis"):
        base.compare(
            request(item),
            comparison.deterministic.model_copy(update={"comparison_basis_fingerprint": PRIVATE}),
            comparison.agentic,
        )


class _EmptyBackend:
    def search(
        self,
        memory_need: Stage1MemoryNeed,
        channel: RetrievalChannel,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        return ()


class _HitBackend(_EmptyBackend):
    def search(
        self,
        memory_need: Stage1MemoryNeed,
        channel: RetrievalChannel,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        return (
            ChannelHit(
                unit=unit(),
                channel=channel,
                channel_rank=1,
                raw_score=1.0,
                candidate_count=1,
                hit_reason="hit",
            ),
        )


class _RecordingEmptyBackend(_EmptyBackend):
    def __init__(self) -> None:
        self.calls: list[tuple[StableId, RetrievalChannel]] = []

    def search(
        self,
        memory_need: Stage1MemoryNeed,
        channel: RetrievalChannel,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        self.calls.append((memory_need.need_id, channel))
        return ()


def test_registered_routes_allocate_first_call_max_min_and_type_budget_miss() -> None:
    first = need(intent=Stage1QueryIntent.RELATED_EVENT).model_copy(
        update={
            "need_id": StableId("need.fair.first"),
            "allowed_candidate_pools": (
                CandidatePool.ANCHOR,
                CandidatePool.GROUNDED,
            ),
        }
    )
    second = first.model_copy(
        update={
            "need_id": StableId("need.fair.second"),
            "query_text": "second critical callback",
        }
    )
    capability = SnapshotCapability(
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=(
            RetrievalChannel.ANCHOR_BM25,
            RetrievalChannel.ANCHOR_DENSE,
            RetrievalChannel.GROUNDED_BM25,
            RetrievalChannel.GROUNDED_DENSE,
        ),
    )
    plans = tuple(DeterministicChannelPlanner().plan(item, capability) for item in (first, second))
    backend = _RecordingEmptyBackend()
    paired = PairedMemoryControllerRunner(
        backend,
        MagicMock(),
        ContextCompiler(EvidenceExpander()),
        CONFIG,
        lambda _: True,
        plans,
    )
    fair_request = request(first, max_calls=2).model_copy(
        update={"initial_memory_needs": (first, second)}
    )
    result = paired.run_deterministic(fair_request, text_root())

    assert [need_id for need_id, _ in backend.calls] == [
        first.need_id,
        second.need_id,
    ]
    assert result.calls_allocated_by_need == {
        first.need_id.root: 1,
        second.need_id.root: 1,
    }
    assert {trace.need_execution_status for trace in result.context.retrieval_traces} == {
        NeedExecutionStatus.EXECUTED_EMPTY
    }

    backend = _RecordingEmptyBackend()
    paired = PairedMemoryControllerRunner(
        backend,
        MagicMock(),
        ContextCompiler(EvidenceExpander()),
        CONFIG,
        lambda _: True,
        plans,
    )
    exhausted = paired.run_deterministic(
        fair_request.model_copy(
            update={
                "retrieval_budget": fair_request.retrieval_budget.model_copy(
                    update={"max_tool_calls": 1}
                )
            }
        ),
        text_root(),
    )
    assert exhausted.context.retrieval_traces[0].need_execution_status is (
        NeedExecutionStatus.EXECUTED_EMPTY
    )
    assert exhausted.context.retrieval_traces[1].need_execution_status is (
        NeedExecutionStatus.NOT_EXECUTED_BUDGET_EXHAUSTED
    )


def test_global_48_call_budget_serves_each_actual_need_before_any_second_call() -> None:
    template = need(intent=Stage1QueryIntent.RELATED_EVENT).model_copy(
        update={
            "allowed_candidate_pools": (
                CandidatePool.ANCHOR,
                CandidatePool.GROUNDED,
            )
        }
    )
    needs = tuple(
        template.model_copy(
            update={
                "need_id": StableId(f"need.fair.global.{index:02d}"),
                "query_text": f"critical callback {index}",
            }
        )
        for index in range(49)
    )
    capability = SnapshotCapability(
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=(
            RetrievalChannel.ANCHOR_BM25,
            RetrievalChannel.ANCHOR_DENSE,
            RetrievalChannel.GROUNDED_BM25,
            RetrievalChannel.GROUNDED_DENSE,
        ),
    )
    plans = tuple(DeterministicChannelPlanner().plan(item, capability) for item in needs)
    backend = _RecordingEmptyBackend()
    paired = PairedMemoryControllerRunner(
        backend,
        MagicMock(),
        ContextCompiler(EvidenceExpander()),
        CONFIG,
        lambda _: True,
        plans,
    )
    fair_request = request(needs[0], max_calls=48).model_copy(
        update={"initial_memory_needs": needs}
    )

    result = paired.run_deterministic(fair_request, text_root())

    assert len(backend.calls) == 48
    assert len({need_id for need_id, _channel in backend.calls}) == 48
    assert max(result.calls_allocated_by_need.values()) == 1
    assert (
        result.context.retrieval_traces[-1].need_execution_status
        is NeedExecutionStatus.NOT_EXECUTED_BUDGET_EXHAUSTED
    )


def test_max_min_first_round_does_not_starve_optional_need() -> None:
    mandatory = need(intent=Stage1QueryIntent.RELATED_EVENT).model_copy(
        update={
            "need_id": StableId("need.fair.mandatory"),
            "allowed_candidate_pools": (
                CandidatePool.ANCHOR,
                CandidatePool.GROUNDED,
            ),
        }
    )
    optional = mandatory.model_copy(
        update={
            "need_id": StableId("need.fair.optional"),
            "query_text": "optional but task-relevant history",
            "requirement": RequirementLevel.OPTIONAL,
            "risk_level": NeedRisk.MEDIUM,
        }
    )
    capability = SnapshotCapability(
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=(
            RetrievalChannel.ANCHOR_BM25,
            RetrievalChannel.ANCHOR_DENSE,
            RetrievalChannel.GROUNDED_BM25,
            RetrievalChannel.GROUNDED_DENSE,
        ),
    )
    plans = tuple(
        DeterministicChannelPlanner().plan(item, capability) for item in (mandatory, optional)
    )
    backend = _RecordingEmptyBackend()
    paired = PairedMemoryControllerRunner(
        backend,
        MagicMock(),
        ContextCompiler(EvidenceExpander()),
        CONFIG,
        lambda _: True,
        plans,
    )
    fair_request = request(mandatory, max_calls=2).model_copy(
        update={"initial_memory_needs": (mandatory, optional)}
    )

    result = paired.run_deterministic(fair_request, text_root())

    assert [need_id for need_id, _channel in backend.calls] == [
        mandatory.need_id,
        optional.need_id,
    ]
    assert result.calls_allocated_by_need == {
        mandatory.need_id.root: 1,
        optional.need_id.root: 1,
    }


class _Reranker:
    profile = "paired-test"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        if self.fail:
            raise RuntimeError("unavailable")
        return tuple(1.0 for _ in passages)


def test_route_plan_execution_covers_fallback_exhaustion_and_repeated_channels() -> None:
    item = need(intent=Stage1QueryIntent.RELATED_EVENT).model_copy(
        update={
            "allowed_candidate_pools": (
                CandidatePool.ANCHOR,
                CandidatePool.GROUNDED,
            )
        }
    )
    capability = SnapshotCapability(
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=(
            RetrievalChannel.ANCHOR_BM25,
            RetrievalChannel.ANCHOR_DENSE,
            RetrievalChannel.GROUNDED_BM25,
            RetrievalChannel.GROUNDED_DENSE,
        ),
    )
    plan = DeterministicChannelPlanner().plan(item, capability)
    trace = PairedMemoryControllerRunner._retrieve_route_plan(
        _EmptyBackend(),
        item,
        plan,
        per_channel_limit=2,
    )
    assert trace.fallback_used is True
    assert trace.stop_reason.value == "fallback_exhausted"
    successful = PairedMemoryControllerRunner._retrieve_route_plan(
        _HitBackend(),
        item,
        plan,
        per_channel_limit=2,
        reranker=RerankService(_Reranker()),
    )
    assert successful.fallback_used is True
    assert successful.fallback_reason == "anchor_evidence_insufficient"
    assert successful.rerank_applied is True
    degraded = PairedMemoryControllerRunner._retrieve_route_plan(
        _HitBackend(),
        item,
        plan,
        per_channel_limit=2,
        reranker=RerankService(_Reranker(fail=True)),
    )
    assert degraded.rerank_applied is False
    assert degraded.channel_failures[RetrievalChannel.RERANK] == ("reranker_degraded:RuntimeError")

    duplicate_stage = plan.model_copy(
        update={
            "mandatory_steps": (plan.primary_groups[0].steps[0],),
        }
    )
    repeated = PairedMemoryControllerRunner._retrieve_route_plan(
        _EmptyBackend(),
        item,
        duplicate_stage,
        per_channel_limit=2,
    )
    assert repeated.allowed_channels.count(RetrievalChannel.ANCHOR_BM25) == 1
    skipped_fallback = PairedMemoryControllerRunner._retrieve_route_plan(
        _HitBackend(),
        item,
        plan.model_copy(
            update={
                "conditional_fallbacks": tuple(
                    fallback.model_copy(update={"condition": "plan_anchor_insufficient"})
                    for fallback in plan.conditional_fallbacks
                )
            }
        ),
        per_channel_limit=2,
    )
    assert skipped_fallback.fallback_used is False

    with pytest.raises(ValueError, match="does not belong"):
        PairedMemoryControllerRunner._retrieve_route_plan(
            _EmptyBackend(),
            item,
            plan.model_copy(update={"need_id": StableId("need.other")}),
            per_channel_limit=2,
        )
    assert PairedMemoryControllerRunner._fallback_applies(
        "hierarchy_scope_resolved",
        (
            PairedMemoryControllerRunner._direct_candidates(
                {
                    RetrievalChannel.R1_EXACT: (
                        ChannelHit(
                            unit=unit(),
                            channel=RetrievalChannel.R1_EXACT,
                            channel_rank=1,
                            raw_score=1.0,
                            candidate_count=1,
                            hit_reason="candidate",
                        ),
                    )
                },
                limit=1,
            )[0],
        ),
    )
    semantic_need = item.model_copy(
        update={"query_text": "hero distant callback unresolved secret"}
    )
    assert PairedMemoryControllerRunner._fallback_applies(
        "anchor_evidence_insufficient",
        PairedMemoryControllerRunner._direct_candidates(
            {
                RetrievalChannel.ANCHOR_BM25: (
                    ChannelHit(
                        unit=unit().model_copy(update={"text": "hero current state"}),
                        channel=RetrievalChannel.ANCHOR_BM25,
                        channel_rank=1,
                        raw_score=1.0,
                        candidate_count=1,
                        hit_reason="candidate",
                    ),
                )
            },
            limit=1,
        ),
        need=semantic_need,
    )
    selected = PairedMemoryControllerRunner._direct_candidates(
        {
            RetrievalChannel.ANCHOR_BM25: (
                ChannelHit(
                    unit=unit(),
                    channel=RetrievalChannel.ANCHOR_BM25,
                    channel_rank=1,
                    raw_score=1.0,
                    candidate_count=1,
                    hit_reason="candidate",
                ),
            )
        },
        limit=1,
    )
    assert not PairedMemoryControllerRunner._fallback_applies(
        "anchor_evidence_insufficient",
        selected,
    )
    assert not PairedMemoryControllerRunner._fallback_applies(
        "anchor_evidence_insufficient",
        selected,
        need=semantic_need.model_copy(update={"query_text": "x"}),
    )
    assert not PairedMemoryControllerRunner._fallback_applies(
        "plan_anchor_insufficient",
        selected,
    )
    assert PairedMemoryControllerRunner._fallback_applies("plan_anchor_insufficient", ())
    with pytest.raises(ValueError, match="unregistered deterministic fallback"):
        PairedMemoryControllerRunner._fallback_applies("unknown", ())


def test_direct_candidate_and_merge_keep_mandatory_units_beyond_limit() -> None:
    first = unit()
    second = first.model_copy(
        update={
            "unit_id": StableId("anchor.second"),
            "mandatory": False,
        }
    )
    hits = (
        ChannelHit(
            unit=first,
            channel=RetrievalChannel.R1_EXACT,
            channel_rank=1,
            raw_score=1.0,
            candidate_count=2,
            hit_reason="first",
        ),
        ChannelHit(
            unit=second,
            channel=RetrievalChannel.R1_TEMPORAL,
            channel_rank=2,
            raw_score=0.5,
            candidate_count=2,
            hit_reason="second",
        ),
    )
    candidates = PairedMemoryControllerRunner._direct_candidates(
        {
            RetrievalChannel.R1_EXACT: hits,
            RetrievalChannel.R1_TEMPORAL: hits,
        },
        limit=1,
    )
    assert len(candidates) == 2
    assert candidates[1].selected is False
    merged = PairedMemoryControllerRunner._merge_candidates(
        (),
        (
            candidates[1],
            candidates[0].model_copy(update={"unit": first.model_copy(update={"mandatory": True})}),
        ),
        limit=1,
    )
    assert merged[1].selected is True


def test_fallback_merge_reserves_bounded_incoming_capacity() -> None:
    source = unit().model_copy(update={"mandatory": False})

    def candidate(prefix: str, index: int) -> FusedCandidate:
        item = source.model_copy(update={"unit_id": StableId(f"{prefix}.{index}")})
        hit = ChannelHit(
            unit=item,
            channel=RetrievalChannel.ANCHOR_BM25,
            channel_rank=index,
            raw_score=1.0 / index,
            candidate_count=4,
            hit_reason="candidate",
        )
        return FusedCandidate(
            unit=item,
            fused_rank=index,
            rrf_score=1.0 / index,
            channel_hits=(hit,),
        )

    merged = PairedMemoryControllerRunner._merge_candidates(
        tuple(candidate("anchor", index) for index in range(1, 5)),
        tuple(candidate("grounded", index) for index in range(1, 5)),
        limit=4,
        reserve_incoming=True,
    )

    assert [item.unit.unit_id.root for item in merged if item.selected] == [
        "anchor.1",
        "grounded.1",
        "anchor.2",
        "grounded.2",
    ]
    existing_longer = PairedMemoryControllerRunner._merge_candidates(
        tuple(candidate("anchor-long", index) for index in range(1, 4)),
        (candidate("grounded-short", 1),),
        limit=4,
        reserve_incoming=True,
    )
    incoming_longer = PairedMemoryControllerRunner._merge_candidates(
        (candidate("anchor-short", 1),),
        tuple(candidate("grounded-long", index) for index in range(1, 4)),
        limit=4,
        reserve_incoming=True,
    )
    assert len(existing_longer) == 4
    assert len(incoming_longer) == 4


def test_fair_registered_routes_block_plan_scope_and_reject_route_identity_drift() -> None:
    plan_need = need(intent=Stage1QueryIntent.PLAN_NODE)
    capability = SnapshotCapability(
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=tuple(
            channel for channel in RetrievalChannel if channel is not RetrievalChannel.RERANK
        ),
    )
    plan = DeterministicChannelPlanner().plan(plan_need, capability)
    paired = PairedMemoryControllerRunner(
        _EmptyBackend(),
        MagicMock(),
        ContextCompiler(EvidenceExpander()),
        CONFIG,
        lambda _: True,
        (plan,),
    )
    blocked = paired.run_deterministic(request(plan_need), text_root())
    assert blocked.context.retrieval_traces[0].need_execution_status is (
        NeedExecutionStatus.NOT_EXECUTED_SCOPE_BLOCKED
    )
    assert blocked.retrieval_call_count == 0

    missing_route_need = need().model_copy(update={"need_id": StableId("need.route.missing")})
    with pytest.raises(ValueError, match="no RoutePlan"):
        paired.run_deterministic(request(missing_route_need), text_root())

    wrong_basis = PairedMemoryControllerRunner(
        _EmptyBackend(),
        MagicMock(),
        ContextCompiler(EvidenceExpander()),
        CONFIG,
        lambda _: True,
        (plan.model_copy(update={"base_commit": CommitId("sha256:" + "9" * 64)}),),
    )
    with pytest.raises(ValueError, match="basis differs"):
        wrong_basis.run_deterministic(
            request(plan_need, allow_future_plan=True),
            text_root(),
        )


def test_fair_trace_assembly_covers_fallback_and_reranker_outcomes() -> None:
    item = need(intent=Stage1QueryIntent.RELATED_EVENT).model_copy(
        update={
            "allowed_candidate_pools": (
                CandidatePool.ANCHOR,
                CandidatePool.GROUNDED,
            )
        }
    )
    capability = SnapshotCapability(
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=(
            RetrievalChannel.ANCHOR_BM25,
            RetrievalChannel.ANCHOR_DENSE,
            RetrievalChannel.GROUNDED_BM25,
            RetrievalChannel.GROUNDED_DENSE,
        ),
    )
    plan = DeterministicChannelPlanner().plan(item, capability)
    fair_runner = PairedMemoryControllerRunner(
        _EmptyBackend(),
        MagicMock(),
        ContextCompiler(EvidenceExpander()),
        CONFIG,
        lambda _: True,
        (plan,),
    )
    fair_result = fair_runner.run_deterministic(
        request(item, max_calls=12),
        text_root(),
    )
    assert fair_result.context.retrieval_traces[0].fallback_used is True
    non_applicable_fallback_plan = plan.model_copy(
        update={
            "conditional_fallbacks": tuple(
                fallback.model_copy(update={"condition": "hierarchy_scope_resolved"})
                for fallback in plan.conditional_fallbacks
            )
        }
    )
    non_applicable_runner = PairedMemoryControllerRunner(
        _EmptyBackend(),
        MagicMock(),
        ContextCompiler(EvidenceExpander()),
        CONFIG,
        lambda _: True,
        (non_applicable_fallback_plan,),
    )
    non_applicable = non_applicable_runner.run_deterministic(
        request(item, max_calls=12),
        text_root(),
    )
    assert non_applicable.context.retrieval_traces[0].fallback_used is False
    assert PairedMemoryControllerRunner._semantic_query_terms("林澈受伤")

    hit_backend = _HitBackend()
    primary_results = {
        step.channel: hit_backend.search(item, step.channel, 2)
        for group in plan.primary_groups
        for step in group.steps
    }
    successful = PairedMemoryControllerRunner._assemble_route_trace(
        item,
        plan,
        primary_results,
        per_channel_limit=2,
        reranker=RerankService(_Reranker()),
    )
    assert successful.fusion_applied is True
    assert successful.rerank_applied is True
    degraded = PairedMemoryControllerRunner._assemble_route_trace(
        item,
        plan,
        primary_results,
        per_channel_limit=2,
        reranker=RerankService(_Reranker(fail=True)),
    )
    assert degraded.channel_failures[RetrievalChannel.RERANK] == ("reranker_degraded:RuntimeError")

    fallback_channel = plan.conditional_fallbacks[0].steps[0].channel
    fallback = PairedMemoryControllerRunner._assemble_route_trace(
        item,
        plan,
        {fallback_channel: ()},
        per_channel_limit=2,
        reranker=None,
    )
    assert fallback.fallback_used is True
    assert fallback.fallback_reason == plan.conditional_fallbacks[0].condition


def test_legacy_fairness_marks_later_need_unexecuted_after_global_budget() -> None:
    first = need()
    second = first.model_copy(update={"need_id": StableId("need.legacy.second")})
    legacy = runner(first, unit())
    legacy_request = request(first, max_calls=1).model_copy(
        update={"initial_memory_needs": (first, second)}
    )
    result = legacy.run_deterministic(legacy_request, text_root())
    assert result.context.retrieval_traces[1].need_execution_status is (
        NeedExecutionStatus.NOT_EXECUTED_BUDGET_EXHAUSTED
    )
