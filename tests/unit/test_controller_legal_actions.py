"""WP2: RoutePlan single legal-action registry."""

from __future__ import annotations

import pytest

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
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalChannel,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.retrieval_routing import (
    ConditionalFallback,
    EvidenceExpansionPolicy,
    InformationDomain,
    ResolutionTier,
    RouteExecution,
    RoutePlan,
    RouteStep,
    RouteStepGroup,
    RouteStopPolicy,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    ContextBudget,
    ControllerActionPhase,
    MemoryResolutionRequest,
    RequiredSnapshotPolicy,
    RetrievalBudget,
    ToolPolicy,
    ToolResult,
    ToolResultStatus,
)
from novel_agent.services.controller_legal_actions import TOOL_BY_CHANNEL, LegalActionProvider

COMMIT = CommitId("sha256:" + "a" * 64)


def _need(
    need_id: str,
    intent: Stage1QueryIntent,
    pools: tuple[CandidatePool, ...] = (CandidatePool.R1,),
) -> Stage1MemoryNeed:
    is_plan = intent in {
        Stage1QueryIntent.PLAN_NODE,
        Stage1QueryIntent.PLAN_OBLIGATION,
        Stage1QueryIntent.GLOBAL_ARC,
    }
    return Stage1MemoryNeed(
        need_id=StableId(need_id),
        run_id=RunId("run.legal"),
        task_id=TaskId("task.legal"),
        base_commit=COMMIT,
        chapter_target=1,
        need_type="state",
        query_intent=intent,
        query_text="query",
        why_needed="test",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
        allowed_candidate_pools=pools,
        stop_condition="done",
        allow_plan=is_plan,
        access_scope="author_planning" if is_plan else "writer_safe",
    )


def _policy(tools: tuple[str, ...]) -> ToolPolicy:
    return ToolPolicy(
        policy_id=StableId("tool-policy.legal"),
        version=SchemaVersion("1.0.0"),
        content_hash=ArtifactId("sha256:" + "b" * 64),
        allowed_tools=tools,
        max_rounds=2,
        max_tool_calls=12,
    )


def _plan(
    need_id: str,
    channels: tuple[RetrievalChannel, ...],
    *,
    intent: Stage1QueryIntent = Stage1QueryIntent.CURRENT_STATE,
    tier: ResolutionTier = ResolutionTier.R1,
    fallbacks: tuple[ConditionalFallback, ...] = (),
) -> RoutePlan:
    steps = tuple(
        RouteStep(
            step_id=StableId(f"step.{channel.value}"),
            channel=channel,
            candidate_pool=(
                CandidatePool.R1
                if channel in {RetrievalChannel.R1_EXACT, RetrievalChannel.R1_TEMPORAL}
                else CandidatePool.GRAPH
                if channel is RetrievalChannel.TYPED_GRAPH
                else CandidatePool.ANCHOR
            ),
            query_template="q",
            mandatory=True,
        )
        for channel in channels
    )
    return RoutePlan(
        route_plan_id=StableId(f"route.{need_id}"),
        profile_id=StableId("profile.legal"),
        need_id=StableId(need_id),
        base_commit=COMMIT,
        snapshot_id=StableId("snap.1"),
        resolution_tier=tier,
        domains=(InformationDomain.WORLD_SEMANTIC,),
        normalized_intent=intent,
        routing_features_hash=ArtifactId("sha256:" + "c" * 64),
        mandatory_steps=steps,
        conditional_fallbacks=fallbacks,
        evidence_policy=EvidenceExpansionPolicy(required_strength="exact"),
        stop_policy=RouteStopPolicy(stop_when="mandatory_closed"),
        policy_version=SchemaVersion("1.0.0"),
    )


def _request(
    needs: tuple[Stage1MemoryNeed, ...],
    *,
    allow_future_plan: bool = False,
    access_scope: AccessScope = AccessScope.WRITER_SAFE,
) -> MemoryResolutionRequest:
    return MemoryResolutionRequest(
        request_id=StableId("req.legal"),
        run_id=RunId("run.legal"),
        task_id=TaskId("task.legal"),
        project_id=ProjectId("project.legal"),
        base_commit=COMMIT,
        snapshot_id=StableId("snap.1"),
        worldline="main",
        narrative_chapter=1,
        access_scope=access_scope,
        required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
        task_contract="test",
        initial_memory_needs=needs,
        retrieval_budget=RetrievalBudget(),
        context_budget=ContextBudget(token_budget=1000),
        allow_future_plan=allow_future_plan,
    )


def test_plan_node_exact_only_excludes_temporal() -> None:
    need = _need("need.plan", Stage1QueryIntent.PLAN_NODE, (CandidatePool.R1,))
    plan = _plan(
        "need.plan",
        (RetrievalChannel.R1_EXACT,),
        intent=Stage1QueryIntent.PLAN_NODE,
    )
    provider = LegalActionProvider(
        tool_policy=_policy(("memory.search_exact", "memory.search_temporal")),
        route_plans=(plan,),
    )
    actions = provider.available_actions(
        _request(
            (need,),
            allow_future_plan=True,
            access_scope=AccessScope.AUTHOR_PLANNING,
        ),
        (),
    )
    tools = {item.tool_name for item in actions}
    assert tools == {"memory.search_exact"}
    assert "memory.search_temporal" not in tools


def test_prompt_actions_match_adapter_channels() -> None:
    need = _need(
        "need.event",
        Stage1QueryIntent.RELATED_EVENT,
        (CandidatePool.R1, CandidatePool.ANCHOR),
    )
    plan = _plan(
        "need.event",
        (RetrievalChannel.R1_EXACT, RetrievalChannel.ANCHOR_BM25),
        intent=Stage1QueryIntent.RELATED_EVENT,
    )
    tools = (
        "memory.search_exact",
        "memory.search_anchor_bm25",
        "memory.search_temporal",
    )
    provider = LegalActionProvider(tool_policy=_policy(tools), route_plans=(plan,))
    request = _request((need,))
    provider.assert_consistency(request, ())
    actions = provider.available_actions(request, ())
    channels = provider.channels_by_need((need,), active_only=True)
    for action in actions:
        assert action.retrieval_channel in channels[need.need_id]
        assert TOOL_BY_CHANNEL[action.retrieval_channel] == action.tool_name


def test_fallback_hidden_until_condition() -> None:
    need = _need(
        "need.fb",
        Stage1QueryIntent.CURRENT_STATE,
        (CandidatePool.R1, CandidatePool.GRAPH),
    )
    primary = RouteStep(
        step_id=StableId("step.exact"),
        channel=RetrievalChannel.R1_EXACT,
        candidate_pool=CandidatePool.R1,
        query_template="q",
        mandatory=True,
    )
    fallback = ConditionalFallback(
        fallback_id=StableId("fb.1"),
        condition="anchor_evidence_insufficient",
        steps=(
            RouteStep(
                step_id=StableId("step.graph"),
                channel=RetrievalChannel.TYPED_GRAPH,
                candidate_pool=CandidatePool.GRAPH,
                query_template="g",
            ),
        ),
    )
    plan = _plan(
        "need.fb",
        (),
        tier=ResolutionTier.R2,
        fallbacks=(fallback,),
    )
    plan = plan.model_copy(update={"mandatory_steps": (primary,)})
    provider = LegalActionProvider(
        tool_policy=_policy(("memory.search_exact", "memory.search_graph")),
        route_plans=(plan,),
    )
    request = _request((need,))
    before = {item.tool_name for item in provider.available_actions(request, ())}
    assert before == {"memory.search_exact"}
    prior = (
        (
            need.need_id,
            "memory.search_exact",
            ToolResult(
                tool_call_id=StableId("t.1"),
                status=ToolResultStatus.SUCCEEDED,
                basis_commit=COMMIT,
                coverage=0,
                audit_ref=StableId("a.1"),
            ),
        ),
    )
    after = {item.tool_name for item in provider.available_actions(request, prior)}
    assert after == {"memory.search_graph"}


def test_need_is_accessible_returns_false_for_access_scope_mismatch() -> None:
    from novel_agent.services.controller_legal_actions import _need_is_accessible

    need = _need("need.scope", Stage1QueryIntent.CURRENT_STATE)
    assert not _need_is_accessible(need, "author_planning", allow_future_plan=True)


def test_legal_action_provider_rejects_duplicate_route_plan_need_ids() -> None:
    plan = _plan("need.dup", (RetrievalChannel.R1_EXACT,))
    with pytest.raises(ValueError, match="unique need ids"):
        LegalActionProvider(
            tool_policy=_policy(("memory.search_exact",)),
            route_plans=(plan, plan),
        )


def test_channels_by_need_without_active_only_uses_pool_channels_for_no_plan() -> None:
    need = _need(
        "need.pool",
        Stage1QueryIntent.CURRENT_STATE,
        (CandidatePool.R1, CandidatePool.ANCHOR),
    )
    provider = LegalActionProvider(
        tool_policy=_policy(("memory.search_exact", "memory.search_anchor_bm25")),
    )
    channels = provider.channels_by_need((need,), active_only=False)
    assert RetrievalChannel.R1_EXACT in channels[need.need_id]
    assert RetrievalChannel.ANCHOR_BM25 in channels[need.need_id]


def test_channels_by_need_without_active_only_uses_plan_steps() -> None:
    need = _need("need.plan-channels", Stage1QueryIntent.RELATED_EVENT, (CandidatePool.R1,))
    plan = _plan("need.plan-channels", (RetrievalChannel.R1_EXACT,))
    provider = LegalActionProvider(
        tool_policy=_policy(("memory.search_exact",)),
        route_plans=(plan,),
    )
    channels = provider.channels_by_need((need,), active_only=False)
    assert channels[need.need_id] == (RetrievalChannel.R1_EXACT,)


def test_assert_consistency_raises_when_action_channel_not_adapter_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    need = _need("need.inconsistent", Stage1QueryIntent.CURRENT_STATE)
    plan = _plan("need.inconsistent", (RetrievalChannel.R1_EXACT,))
    provider = LegalActionProvider(
        tool_policy=_policy(("memory.search_exact",)),
        route_plans=(plan,),
    )
    request = _request((need,))
    monkeypatch.setattr(provider, "channels_by_need", lambda *args, **kwargs: {})
    with pytest.raises(ValueError, match="inconsistency"):
        provider.assert_consistency(request, ())


def test_actions_for_need_includes_primary_group_steps() -> None:
    need = _need(
        "need.primary",
        Stage1QueryIntent.RELATED_EVENT,
        (CandidatePool.R1,),
    )
    step = RouteStep(
        step_id=StableId("step.exact"),
        channel=RetrievalChannel.R1_EXACT,
        candidate_pool=CandidatePool.R1,
        query_template="q",
        mandatory=True,
    )
    group_step = RouteStep(
        step_id=StableId("step.temporal"),
        channel=RetrievalChannel.R1_TEMPORAL,
        candidate_pool=CandidatePool.R1,
        query_template="q",
    )
    group = RouteStepGroup(
        group_id=StableId("group.1"),
        execution=RouteExecution.PARALLEL,
        steps=(group_step,),
    )
    plan = RoutePlan(
        route_plan_id=StableId("route.primary"),
        profile_id=StableId("profile.legal"),
        need_id=StableId("need.primary"),
        base_commit=COMMIT,
        snapshot_id=StableId("snap.1"),
        resolution_tier=ResolutionTier.R1,
        domains=(InformationDomain.WORLD_SEMANTIC,),
        normalized_intent=Stage1QueryIntent.RELATED_EVENT,
        routing_features_hash=ArtifactId("sha256:" + "c" * 64),
        mandatory_steps=(step,),
        primary_groups=(group,),
        evidence_policy=EvidenceExpansionPolicy(required_strength="exact"),
        stop_policy=RouteStopPolicy(stop_when="mandatory_closed"),
        policy_version=SchemaVersion("1.0.0"),
    )
    provider = LegalActionProvider(
        tool_policy=_policy(("memory.search_exact", "memory.search_temporal")),
        route_plans=(plan,),
    )
    request = _request((need,))
    actions = provider.available_actions(request, ())
    tools = {item.tool_name for item in actions}
    assert tools == {"memory.search_exact", "memory.search_temporal"}


def test_step_action_returns_none_when_tool_not_in_policy() -> None:
    need = _need("need.no-policy", Stage1QueryIntent.CURRENT_STATE)
    provider = LegalActionProvider(
        tool_policy=_policy(("memory.search_exact",)),
    )
    result = provider._step_action(
        need,
        StableId("step.graph"),
        RetrievalChannel.TYPED_GRAPH,
        ControllerActionPhase.PRIMARY,
        None,
        set(),
    )
    assert result is None


def test_step_action_returns_none_when_pool_not_allowed() -> None:
    need = _need(
        "need.no-pool",
        Stage1QueryIntent.CURRENT_STATE,
        (CandidatePool.R1,),
    )
    provider = LegalActionProvider(
        tool_policy=_policy(("memory.search_exact", "memory.search_anchor_bm25")),
    )
    result = provider._step_action(
        need,
        StableId("step.anchor"),
        RetrievalChannel.ANCHOR_BM25,
        ControllerActionPhase.PRIMARY,
        None,
        set(),
    )
    assert result is None


def test_fallback_applies_hierarchy_scope_resolved_and_rejects_unknown_condition() -> None:
    assert LegalActionProvider._fallback_applies("hierarchy_scope_resolved", True) is True
    assert LegalActionProvider._fallback_applies("hierarchy_scope_resolved", False) is False
    with pytest.raises(ValueError, match="unregistered route fallback"):
        LegalActionProvider._fallback_applies("totally-unknown-condition", False)


def test_available_action_summaries_deduplicates_duplicate_tool_names() -> None:
    """Branch 94->82: same tool_name from mandatory + primary group -> deduplicated."""
    need = _need("need.dup-tool", Stage1QueryIntent.RELATED_EVENT, (CandidatePool.R1,))
    step = RouteStep(
        step_id=StableId("step.exact"),
        channel=RetrievalChannel.R1_EXACT,
        candidate_pool=CandidatePool.R1,
        query_template="q",
        mandatory=True,
    )
    # Primary group with the SAME channel as the mandatory step
    group = RouteStepGroup(
        group_id=StableId("group.dup"),
        execution=RouteExecution.PARALLEL,
        steps=(
            RouteStep(
                step_id=StableId("step.exact2"),
                channel=RetrievalChannel.R1_EXACT,
                candidate_pool=CandidatePool.R1,
                query_template="q",
            ),
        ),
    )
    plan = RoutePlan(
        route_plan_id=StableId("route.dup"),
        profile_id=StableId("profile.legal"),
        need_id=StableId("need.dup-tool"),
        base_commit=COMMIT,
        snapshot_id=StableId("snap.1"),
        resolution_tier=ResolutionTier.R1,
        domains=(InformationDomain.WORLD_SEMANTIC,),
        normalized_intent=Stage1QueryIntent.RELATED_EVENT,
        routing_features_hash=ArtifactId("sha256:" + "c" * 64),
        mandatory_steps=(step,),
        primary_groups=(group,),
        evidence_policy=EvidenceExpansionPolicy(required_strength="exact"),
        stop_policy=RouteStopPolicy(stop_when="mandatory_closed"),
        policy_version=SchemaVersion("1.0.0"),
    )
    provider = LegalActionProvider(
        tool_policy=_policy(("memory.search_exact",)),
        route_plans=(plan,),
    )
    request = _request((need,))
    summaries = provider.available_action_summaries(request, ())
    # Only one tool_name entry despite two actions with the same tool
    assert len(summaries) == 1
    assert summaries[0]["tool_names"] == ["memory.search_exact"]


def test_channels_by_need_returns_empty_for_unmatched_pools() -> None:
    """Branch 149->108: need with pools that don't match any policy tool -> no channels."""
    need = _need(
        "need.no-channels",
        Stage1QueryIntent.CURRENT_STATE,
        (CandidatePool.GRAPH,),
    )
    provider = LegalActionProvider(
        tool_policy=_policy(("memory.search_exact", "memory.search_temporal")),
    )
    # Need only allows GRAPH pool, but policy only has R1 tools -> no channels
    channels = provider.channels_by_need((need,), active_only=False)
    assert need.need_id not in channels


def test_primary_group_step_not_in_policy_is_skipped() -> None:
    """Branch 195->191: primary group step whose tool is not in policy -> action is None."""
    need = _need(
        "need.skip-primary",
        Stage1QueryIntent.RELATED_EVENT,
        (CandidatePool.R1, CandidatePool.GRAPH),
    )
    mandatory = RouteStep(
        step_id=StableId("step.exact"),
        channel=RetrievalChannel.R1_EXACT,
        candidate_pool=CandidatePool.R1,
        query_template="q",
        mandatory=True,
    )
    # Primary group step uses GRAPH channel, but policy only has exact
    group = RouteStepGroup(
        group_id=StableId("group.skip"),
        execution=RouteExecution.PARALLEL,
        steps=(
            RouteStep(
                step_id=StableId("step.graph"),
                channel=RetrievalChannel.TYPED_GRAPH,
                candidate_pool=CandidatePool.GRAPH,
                query_template="q",
            ),
        ),
    )
    plan = RoutePlan(
        route_plan_id=StableId("route.skip-primary"),
        profile_id=StableId("profile.legal"),
        need_id=StableId("need.skip-primary"),
        base_commit=COMMIT,
        snapshot_id=StableId("snap.1"),
        resolution_tier=ResolutionTier.R1,
        domains=(InformationDomain.WORLD_SEMANTIC,),
        normalized_intent=Stage1QueryIntent.RELATED_EVENT,
        routing_features_hash=ArtifactId("sha256:" + "c" * 64),
        mandatory_steps=(mandatory,),
        primary_groups=(group,),
        evidence_policy=EvidenceExpansionPolicy(required_strength="exact"),
        stop_policy=RouteStopPolicy(stop_when="mandatory_closed"),
        policy_version=SchemaVersion("1.0.0"),
    )
    provider = LegalActionProvider(
        tool_policy=_policy(("memory.search_exact",)),
        route_plans=(plan,),
    )
    request = _request((need,))
    actions = provider.available_actions(request, ())
    tools = {item.tool_name for item in actions}
    # Only exact is available; graph step was skipped (tool not in policy)
    assert tools == {"memory.search_exact"}


def test_fallback_step_not_in_policy_is_skipped() -> None:
    """Branch 224->215: fallback step whose tool is not in policy -> action is None."""
    need = _need(
        "need.skip-fallback",
        Stage1QueryIntent.CURRENT_STATE,
        (CandidatePool.R1, CandidatePool.GRAPH),
    )
    primary = RouteStep(
        step_id=StableId("step.exact"),
        channel=RetrievalChannel.R1_EXACT,
        candidate_pool=CandidatePool.R1,
        query_template="q",
        mandatory=True,
    )
    # Fallback uses GRAPH channel, but policy only has exact
    fallback = ConditionalFallback(
        fallback_id=StableId("fb.skip"),
        condition="anchor_evidence_insufficient",
        steps=(
            RouteStep(
                step_id=StableId("step.graph"),
                channel=RetrievalChannel.TYPED_GRAPH,
                candidate_pool=CandidatePool.GRAPH,
                query_template="q",
            ),
        ),
    )
    plan = RoutePlan(
        route_plan_id=StableId("route.skip-fallback"),
        profile_id=StableId("profile.legal"),
        need_id=StableId("need.skip-fallback"),
        base_commit=COMMIT,
        snapshot_id=StableId("snap.1"),
        resolution_tier=ResolutionTier.R2,
        domains=(InformationDomain.WORLD_SEMANTIC,),
        normalized_intent=Stage1QueryIntent.CURRENT_STATE,
        routing_features_hash=ArtifactId("sha256:" + "c" * 64),
        mandatory_steps=(primary,),
        conditional_fallbacks=(fallback,),
        evidence_policy=EvidenceExpansionPolicy(required_strength="exact"),
        stop_policy=RouteStopPolicy(stop_when="mandatory_closed"),
        policy_version=SchemaVersion("1.0.0"),
    )
    provider = LegalActionProvider(
        tool_policy=_policy(("memory.search_exact",)),
        route_plans=(plan,),
    )
    request = _request((need,))
    # Call exact first to exhaust primary and trigger fallback condition
    prior = (
        (
            need.need_id,
            "memory.search_exact",
            ToolResult(
                tool_call_id=StableId("t.1"),
                status=ToolResultStatus.SUCCEEDED,
                basis_commit=COMMIT,
                coverage=0,
                audit_ref=StableId("a.1"),
            ),
        ),
    )
    actions = provider.available_actions(request, prior)
    tools = {item.tool_name for item in actions}
    # Fallback graph step was skipped (tool not in policy); no actions remain
    assert tools == set()
