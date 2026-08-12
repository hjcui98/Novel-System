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
from novel_agent.domain.world import StoryTime
from novel_agent.services.paired_controller import PairedMemoryControllerRunner
from novel_agent.services.retrieval_routing import (
    CounterfactualRouteEvaluator,
    DeterministicChannelPlanner,
    DomainRouter,
    R0ContextSlot,
    RoutePlanValidator,
    TierDecision,
    TierRouter,
    _pool,
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


def test_current_state_route_is_intersected_with_registered_query_channels() -> None:
    planner = DeterministicChannelPlanner()
    current = planner.plan(
        need(
            Stage1QueryIntent.CURRENT_STATE,
            (CandidatePool.R1, CandidatePool.ANCHOR),
            entities=(ENTITY,),
        ),
        capability(),
    )

    assert current.resolution_tier is ResolutionTier.R1
    assert active_channels(current) == {
        RetrievalChannel.R1_EXACT,
        RetrievalChannel.R1_TEMPORAL,
        RetrievalChannel.ANCHOR_BM25,
        RetrievalChannel.ANCHOR_DENSE,
    }
    assert len(current.conditional_fallbacks) == 1


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


def test_deterministic_pair_arm_executes_only_registered_primary_and_fallback_channels() -> None:
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

    assert backend.calls == [
        RetrievalChannel.ANCHOR_BM25,
        RetrievalChannel.ANCHOR_DENSE,
        RetrievalChannel.GROUNDED_BM25,
        RetrievalChannel.GROUNDED_DENSE,
    ]
    assert trace.allowed_channels == tuple(backend.calls)
    assert trace.fusion_applied is True
    assert trace.fallback_used is True
    assert trace.fallback_reason == "anchor_evidence_insufficient"


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


def test_tier_and_domain_routers_cover_fail_closed_match_conditions() -> None:
    router = TierRouter()
    exact_need = need(
        Stage1QueryIntent.CURRENT_STATE,
        (CandidatePool.R1,),
        entities=(ENTITY,),
        predicates=("location",),
    )
    base_unit = RetrievalUnit(
        unit_id=StableId("unit.slot.base"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        text="slot",
        entity_ids=(ENTITY,),
        predicate="location",
        worldline="main",
    )
    slots = (
        R0ContextSlot(base_unit, conflicted=True),
        R0ContextSlot(
            base_unit.model_copy(update={"source_commit": CommitId("sha256:" + "b" * 64)})
        ),
        R0ContextSlot(base_unit.model_copy(update={"entity_ids": ()})),
        R0ContextSlot(base_unit.model_copy(update={"predicate": "status"})),
        R0ContextSlot(base_unit),
    )
    decision = router.decide(exact_need, capability(), slots=slots)
    assert decision.tier is ResolutionTier.R0
    entity_only = exact_need.model_copy(update={"predicates": ()})
    assert router.decide(entity_only, capability()).tier is ResolutionTier.R1

    timed = exact_need.model_copy(
        update={"time_scope": StoryTime(worldline="other", start_ordinal=1)}
    )
    assert router.decide(timed, capability(), slots=(slots[-1],)).tier is not ResolutionTier.R0
    partial = capability().model_copy(update={"status": SnapshotCapabilityStatus.PARTIAL})
    assert router.decide(exact_need, partial).tier is ResolutionTier.R2

    assert (
        router.decide(
            need(
                Stage1QueryIntent.MANDATORY_CONSTRAINT,
                (CandidatePool.R1,),
                predicates=("status",),
            ),
            capability(),
        ).tier
        is ResolutionTier.R1
    )
    assert (
        router.decide(
            need(
                Stage1QueryIntent.PLAN_NODE,
                (CandidatePool.R1,),
                entities=(ENTITY,),
            ),
            capability(),
        ).tier
        is ResolutionTier.R1
    )

    domains = DomainRouter()
    assert domains.domains_for(exact_need, ResolutionTier.R0)
    for intent in (
        Stage1QueryIntent.PLAN_OBLIGATION,
        Stage1QueryIntent.EXACT_QUOTE,
        Stage1QueryIntent.STYLE_VOICE,
        Stage1QueryIntent.RELATED_EVENT,
    ):
        assert domains.domains_for(
            need(intent, (CandidatePool.ANCHOR, CandidatePool.GROUNDED)),
            ResolutionTier.R2,
        )


@pytest.mark.parametrize("intent", tuple(Stage1QueryIntent))
def test_every_registered_r2_intent_builds_a_frozen_profile(
    intent: Stage1QueryIntent,
) -> None:
    profile = profile_for(intent, ResolutionTier.R2)
    assert profile.query_intent is intent


def test_planner_and_validator_reject_impossible_tiers_and_authority_expansion() -> None:
    semantic = need(Stage1QueryIntent.SEMANTIC_HISTORY, (CandidatePool.ANCHOR,))

    class FixedTier:
        def __init__(self, decision: TierDecision) -> None:
            self.decision = decision

        def decide(self, *_args, **_kwargs) -> TierDecision:  # type: ignore[no-untyped-def]
            return self.decision

    with pytest.raises(ValueError, match="same-basis context slot"):
        DeterministicChannelPlanner(
            tier_router=FixedTier(TierDecision(ResolutionTier.R0, "forced"))  # type: ignore[arg-type]
        ).plan(semantic, capability())
    with pytest.raises(ValueError, match="no certified mandatory"):
        DeterministicChannelPlanner(
            tier_router=FixedTier(TierDecision(ResolutionTier.R1, "forced"))  # type: ignore[arg-type]
        ).plan(semantic, capability())

    planner = DeterministicChannelPlanner()
    route = planner.plan(semantic, capability())
    profile = profile_for(semantic.query_intent, ResolutionTier.R2)
    validator = RoutePlanValidator()
    mutations = (
        (
            route.model_copy(update={"need_id": StableId("need.other")}),
            profile,
            "basis",
        ),
        (
            route.model_copy(update={"snapshot_id": StableId("snapshot.other")}),
            profile,
            "snapshot",
        ),
        (
            route.model_copy(update={"resolution_tier": ResolutionTier.R1}),
            profile,
            "tier differs",
        ),
        (
            route.model_copy(update={"normalized_intent": Stage1QueryIntent.RELATED_EVENT}),
            profile,
            "intent differs",
        ),
        (
            route,
            profile.model_copy(update={"allowed_channels": ()}),
            "registered profile",
        ),
    )
    for invalid, registered, message in mutations:
        with pytest.raises(ValueError, match=message):
            validator.validate(invalid, semantic, capability(), registered)

    assert planner._mask_group(route.primary_groups[0], set()) is None
    absent = route.model_copy(update={"excluded_channels": ()})
    absent_capability = capability().model_copy(update={"available_channels": ()})
    with pytest.raises(ValueError, match="absent from snapshot capability"):
        validator.validate(absent, semantic, absent_capability, profile)

    exact_need = need(
        Stage1QueryIntent.KNOWN_ID,
        (CandidatePool.R1,),
        entities=(ENTITY,),
    )
    r1 = planner.plan(exact_need, capability())
    anchor_step = (
        profile_for(Stage1QueryIntent.SEMANTIC_HISTORY, ResolutionTier.R2)
        .primary_groups[0]
        .steps[0]
    )
    invalid_r1 = r1.model_copy(
        update={
            "mandatory_steps": (*r1.mandatory_steps, anchor_step),
            "excluded_channels": tuple(
                item
                for item in r1.excluded_channels
                if item.channel is not RetrievalChannel.ANCHOR_BM25
            ),
        }
    )
    expanded_r1_profile = profile_for(Stage1QueryIntent.KNOWN_ID, ResolutionTier.R1).model_copy(
        update={
            "allowed_channels": (
                RetrievalChannel.R1_EXACT,
                RetrievalChannel.ANCHOR_BM25,
            )
        }
    )
    with pytest.raises(ValueError, match="R1 route may only"):
        validator.validate(invalid_r1, exact_need, capability(), expanded_r1_profile)

    slot = R0ContextSlot(
        RetrievalUnit(
            unit_id=StableId("unit.r0.validator"),
            unit_kind=RetrievalUnitKind.STATE_ANCHOR,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            text="local",
            entity_ids=(ENTITY,),
        )
    )
    r0 = planner.plan(exact_need, capability(), slots=(slot,))
    invalid_r0 = r0.model_copy(
        update={
            "mandatory_steps": (*r0.mandatory_steps, r1.mandatory_steps[0]),
            "excluded_channels": tuple(
                item
                for item in r0.excluded_channels
                if item.channel is not RetrievalChannel.R1_EXACT
            ),
        }
    )
    expanded_r0_profile = profile_for(Stage1QueryIntent.KNOWN_ID, ResolutionTier.R0).model_copy(
        update={
            "allowed_channels": (
                RetrievalChannel.R0,
                RetrievalChannel.R1_EXACT,
            )
        }
    )
    with pytest.raises(ValueError, match="R0 route may only"):
        validator.validate(invalid_r0, exact_need, capability(), expanded_r0_profile)

    assert profile_for(Stage1QueryIntent.CURRENT_STATE, ResolutionTier.R1).mandatory_steps
    assert _pool(RetrievalChannel.R0) is CandidatePool.R0


def test_counterfactual_evaluator_rejects_all_invalid_authority_shapes() -> None:
    memory_need = need(
        Stage1QueryIntent.SEMANTIC_HISTORY,
        (CandidatePool.ANCHOR, CandidatePool.GROUNDED),
    ).model_copy(update={"access_scope": "evaluator"})
    route = DeterministicChannelPlanner().plan(memory_need, capability())
    evaluator = CounterfactualRouteEvaluator()
    backend = _RecordedBackend()
    cases = (
        (
            route.model_copy(update={"need_id": StableId("need.other")}),
            (RetrievalChannel.HIERARCHY,),
            1,
            "basis",
        ),
        (route, (), 1, "non-empty and unique"),
        (
            route,
            (RetrievalChannel.HIERARCHY, RetrievalChannel.HIERARCHY),
            1,
            "non-empty and unique",
        ),
        (
            route,
            (RetrievalChannel.ANCHOR_BM25,),
            1,
            "absent from the production",
        ),
        (route, (RetrievalChannel.HIERARCHY,), 0, "limit must be positive"),
    )
    for candidate, channels, limit, message in cases:
        with pytest.raises(ValueError, match=message):
            evaluator.evaluate(
                candidate,
                memory_need,
                backend,
                added_channels=channels,
                limit=limit,
            )
