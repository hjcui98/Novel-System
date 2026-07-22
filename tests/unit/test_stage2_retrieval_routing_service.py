from __future__ import annotations

import pytest

from novel_agent.domain.ids import CommitId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    ChannelHit,
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
    ResolutionTier,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.services.paired_controller import PairedMemoryControllerRunner
from novel_agent.services.retrieval_routing import (
    CounterfactualRouteEvaluator,
    DeterministicChannelPlanner,
    R0ContextSlot,
    RoutePlanValidator,
    profile_for,
)

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.routing")
ENTITY = StableId("entity.routing.hero")
CHANNELS = (
    RetrievalChannel.R1_EXACT,
    RetrievalChannel.R1_TEMPORAL,
    RetrievalChannel.ANCHOR_BM25,
    RetrievalChannel.ANCHOR_DENSE,
    RetrievalChannel.GROUNDED_BM25,
    RetrievalChannel.GROUNDED_DENSE,
    RetrievalChannel.HIERARCHY,
    RetrievalChannel.TYPED_GRAPH,
)


def capability(*, source_commit: CommitId = COMMIT) -> SnapshotCapability:
    return SnapshotCapability(
        source_commit=source_commit,
        snapshot_id=SNAPSHOT,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=CHANNELS,
        embedding_profile="BAAI/bge-m3@locked",
        graph_profile="postgresql-r1-versioned-edges-v0.1",
    )


def need(
    intent: Stage1QueryIntent,
    pools: tuple[CandidatePool, ...],
    *,
    entities: tuple[StableId, ...] = (),
    predicates: tuple[str, ...] = (),
) -> Stage1MemoryNeed:
    return Stage1MemoryNeed(
        need_id=StableId(f"need.routing.{intent.value}"),
        run_id=RunId("run.routing"),
        task_id=TaskId("task.routing"),
        base_commit=COMMIT,
        chapter_target=21,
        need_type=intent.value,
        query_intent=intent,
        query_text="查找旧誓言",
        entity_ids=entities,
        predicates=predicates,
        why_needed="routing contract test",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.ANCHOR_FIRST,
        allowed_candidate_pools=pools,
        stop_condition="bounded lookup complete",
    )


def active_channels(plan) -> set[RetrievalChannel]:  # type: ignore[no-untyped-def]
    return {
        step.channel
        for step in (
            *plan.mandatory_steps,
            *(step for group in plan.primary_groups for step in group.steps),
            *(step for fallback in plan.conditional_fallbacks for step in fallback.steps),
        )
    }


def test_semantic_route_is_anchor_first_with_gated_grounded_fallback() -> None:
    planner = DeterministicChannelPlanner()
    route = planner.plan(
        need(
            Stage1QueryIntent.SEMANTIC_HISTORY,
            (CandidatePool.ANCHOR, CandidatePool.GROUNDED),
        ),
        capability(),
    )

    assert route.resolution_tier is ResolutionTier.R2
    assert {step.channel for step in route.primary_groups[0].steps} == {
        RetrievalChannel.ANCHOR_BM25,
        RetrievalChannel.ANCHOR_DENSE,
    }
    assert {step.channel for step in route.conditional_fallbacks[0].steps} == {
        RetrievalChannel.GROUNDED_BM25,
        RetrievalChannel.GROUNDED_DENSE,
    }
    assert RetrievalChannel.TYPED_GRAPH not in active_channels(route)
    assert {item.channel for item in route.excluded_channels} >= {
        RetrievalChannel.R1_EXACT,
        RetrievalChannel.TYPED_GRAPH,
    }


def test_candidate_pool_hard_mask_removes_forbidden_fallback_channels() -> None:
    route = DeterministicChannelPlanner().plan(
        need(Stage1QueryIntent.SEMANTIC_HISTORY, (CandidatePool.ANCHOR,)),
        capability(),
    )

    assert route.conditional_fallbacks == ()
    assert {
        item.channel
        for item in route.excluded_channels
        if item.reason == "candidate_pool_forbidden"
    } == {
        RetrievalChannel.GROUNDED_BM25,
        RetrievalChannel.GROUNDED_DENSE,
    }


def test_registered_known_id_uses_r1_and_same_basis_slot_uses_r0() -> None:
    planner = DeterministicChannelPlanner()
    exact_need = need(
        Stage1QueryIntent.KNOWN_ID,
        (CandidatePool.R1,),
        entities=(ENTITY,),
    )
    exact = planner.plan(exact_need, capability())
    assert exact.resolution_tier is ResolutionTier.R1
    assert active_channels(exact) == {RetrievalChannel.R1_EXACT}

    slot = R0ContextSlot(
        RetrievalUnit(
            unit_id=StableId("unit.routing.slot"),
            unit_kind=RetrievalUnitKind.STATE_ANCHOR,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            text="主角当前状态",
            entity_ids=(ENTITY,),
        )
    )
    local = planner.plan(exact_need, capability(), slots=(slot,))
    assert local.resolution_tier is ResolutionTier.R0
    assert active_channels(local) == {RetrievalChannel.R0}


def test_router_fails_closed_for_basis_mismatch_and_validator_rejects_policy_expansion() -> None:
    planner = DeterministicChannelPlanner()
    semantic = need(Stage1QueryIntent.SEMANTIC_HISTORY, (CandidatePool.ANCHOR,))
    with pytest.raises(ValueError, match="basis"):
        planner.plan(semantic, capability(source_commit=CommitId("sha256:" + "b" * 64)))

    route = planner.plan(semantic, capability())
    expanded = route.model_copy(
        update={
            "mandatory_steps": (
                *route.mandatory_steps,
                profile_for(Stage1QueryIntent.KNOWN_ID, ResolutionTier.R1).mandatory_steps[0],
            )
        }
    )
    with pytest.raises(ValueError, match="excluded channel"):
        RoutePlanValidator().validate(
            expanded,
            semantic,
            capability(),
            profile_for(Stage1QueryIntent.SEMANTIC_HISTORY, ResolutionTier.R2),
        )


class _RecordedBackend:
    def __init__(self) -> None:
        self.calls: list[RetrievalChannel] = []

    def search(
        self,
        memory_need: Stage1MemoryNeed,
        channel: RetrievalChannel,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        self.calls.append(channel)
        unit = RetrievalUnit(
            unit_id=StableId(f"unit.routing.{channel.value}"),
            unit_kind=RetrievalUnitKind.EVENT_ANCHOR,
            source_commit=memory_need.base_commit,
            snapshot_id=SNAPSHOT,
            text=f"retrieved through {channel.value}",
        )
        return (
            ChannelHit(
                unit=unit,
                channel=channel,
                channel_rank=1,
                raw_score=1.0,
                candidate_count=1,
                hit_reason="recorded-route-test",
            ),
        )


def test_deterministic_pair_arm_executes_registered_route_without_fallback_broadcast() -> None:
    memory_need = need(
        Stage1QueryIntent.SEMANTIC_HISTORY,
        (CandidatePool.ANCHOR, CandidatePool.GROUNDED),
    )
    route = DeterministicChannelPlanner().plan(memory_need, capability())
    backend = _RecordedBackend()

    trace = PairedMemoryControllerRunner._retrieve_route_plan(
        backend,
        memory_need,
        route,
        per_channel_limit=5,
    )

    assert backend.calls == [RetrievalChannel.ANCHOR_BM25, RetrievalChannel.ANCHOR_DENSE]
    assert trace.allowed_channels == tuple(backend.calls)
    assert trace.fusion_applied is True
    assert trace.fallback_used is False


def test_counterfactual_route_ablation_is_evaluator_only_and_separate() -> None:
    production_need = need(
        Stage1QueryIntent.SEMANTIC_HISTORY,
        (CandidatePool.ANCHOR, CandidatePool.GROUNDED),
    )
    route = DeterministicChannelPlanner().plan(production_need, capability())
    evaluator = CounterfactualRouteEvaluator()
    backend = _RecordedBackend()

    with pytest.raises(ValueError, match="evaluator access"):
        evaluator.evaluate(
            route,
            production_need,
            backend,
            added_channels=(RetrievalChannel.HIERARCHY,),
        )
    record = evaluator.evaluate(
        route,
        production_need.model_copy(update={"access_scope": "evaluator"}),
        backend,
        added_channels=(RetrievalChannel.HIERARCHY,),
    )

    assert record.evaluator_only is True
    assert record.added_channels == (RetrievalChannel.HIERARCHY,)
    assert record.candidate_count == record.selected_count == 1
    assert RetrievalChannel.HIERARCHY not in {
        step.channel for group in route.primary_groups for step in group.steps
    }
