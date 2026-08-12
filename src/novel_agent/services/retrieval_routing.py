"""Deterministic Stage 2R tier, domain, and channel route planning."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    CandidatePool,
    RetrievalChannel,
    RetrievalUnit,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.retrieval_routing import (
    ConditionalFallback,
    CounterfactualRouteRecord,
    EvidenceExpansionPolicy,
    ExcludedChannel,
    GraphTraversalPolicy,
    InformationDomain,
    ResolutionTier,
    RetrievalRoutingFeatures,
    RouteExecution,
    RoutePlan,
    RouteProfile,
    RouteStep,
    RouteStepGroup,
    RouteStopPolicy,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.need_query_compiler import NeedQueryCompiler
from novel_agent.services.retrieval import ROUTES, FusionService, RetrievalBackend

ROUTE_POLICY_VERSION = SchemaVersion("2.2.0")
ROUTE_PROFILE_VERSION = SchemaVersion("2.2.0")


@dataclass(frozen=True, slots=True)
class R0ContextSlot:
    """A current-context fact that may be reused only for the same retrieval basis."""

    unit: RetrievalUnit
    access_scope: str = "writer_safe"
    conflicted: bool = False
    stale: bool = False


@dataclass(frozen=True, slots=True)
class TierDecision:
    tier: ResolutionTier
    reason: str
    matched_slot_id: StableId | None = None


class TierRouter:
    """Resolve R0/R1 eligibility mechanically before an R2 controller can act."""

    def decide(
        self,
        need: Stage1MemoryNeed,
        capability: SnapshotCapability,
        *,
        slots: tuple[R0ContextSlot, ...] = (),
        access_scope: str = "writer_safe",
    ) -> TierDecision:
        for slot in slots:
            if self._slot_matches(need, capability, slot, access_scope):
                return TierDecision(ResolutionTier.R0, "same_basis_context_slot", slot.unit.unit_id)
        if self._r1_eligible(need, capability):
            return TierDecision(ResolutionTier.R1, "registered_exact_contract")
        return TierDecision(ResolutionTier.R2, "semantic_or_incomplete_exact_contract")

    @staticmethod
    def _slot_matches(
        need: Stage1MemoryNeed,
        capability: SnapshotCapability,
        slot: R0ContextSlot,
        access_scope: str,
    ) -> bool:
        unit = slot.unit
        if slot.conflicted or slot.stale or slot.access_scope != access_scope:
            return False
        if unit.source_commit != need.base_commit or unit.snapshot_id != capability.snapshot_id:
            return False
        if need.entity_ids and not set(need.entity_ids).issubset(unit.entity_ids):
            return False
        if need.predicates and unit.predicate not in need.predicates:
            return False
        return need.time_scope is None or unit.worldline == need.time_scope.worldline

    @staticmethod
    def _r1_eligible(need: Stage1MemoryNeed, capability: SnapshotCapability) -> bool:
        if capability.status is not SnapshotCapabilityStatus.EXACT:
            return False
        available = set(capability.available_channels)
        exact = RetrievalChannel.R1_EXACT in available
        temporal = RetrievalChannel.R1_TEMPORAL in available
        if need.query_intent is Stage1QueryIntent.KNOWN_ID:
            return exact and bool(need.entity_ids)
        if need.query_intent is Stage1QueryIntent.CURRENT_STATE:
            # An exact canonical entity id is sufficient to retrieve its bounded
            # current-state record set. Requiring a predicate made task-derived
            # entity frontier needs fall through to an empty R2 registration.
            return exact and temporal and bool(need.entity_ids)
        if need.query_intent is Stage1QueryIntent.MANDATORY_CONSTRAINT:
            return exact and temporal and bool(need.entity_ids or need.predicates)
        if need.query_intent is Stage1QueryIntent.PLAN_NODE:
            return exact and bool(need.predicates or need.entity_ids)
        return False


class DomainRouter:
    """Map normalized intents to separated information domains, never mixed top-k pools."""

    def domains_for(
        self, need: Stage1MemoryNeed, tier: ResolutionTier
    ) -> tuple[InformationDomain, ...]:
        if tier is ResolutionTier.R0:
            return (InformationDomain.WORKING,)
        intent = need.query_intent
        if intent in {Stage1QueryIntent.PLAN_NODE, Stage1QueryIntent.PLAN_OBLIGATION}:
            return (InformationDomain.PLAN_INTENT,)
        if intent in {Stage1QueryIntent.EXACT_QUOTE, Stage1QueryIntent.RARE_PHRASE}:
            return (InformationDomain.TEXTUAL_EVIDENCE,)
        if intent in {Stage1QueryIntent.STYLE_VOICE, Stage1QueryIntent.DIALOGUE_SAMPLE}:
            return (InformationDomain.TEXTUAL_EVIDENCE,)
        if intent in {
            Stage1QueryIntent.SEMANTIC_HISTORY,
            Stage1QueryIntent.RELATED_EVENT,
            Stage1QueryIntent.ANCHOR_INSUFFICIENT,
        }:
            return (InformationDomain.WORLD_SEMANTIC, InformationDomain.TEXTUAL_EVIDENCE)
        return (InformationDomain.WORLD_SEMANTIC,)


class DeterministicChannelPlanner:
    """Build a capability-masked RoutePlan from versioned route templates."""

    def __init__(
        self,
        *,
        tier_router: TierRouter | None = None,
        domain_router: DomainRouter | None = None,
    ) -> None:
        self._tier_router = tier_router or TierRouter()
        self._domain_router = domain_router or DomainRouter()

    def features_for(
        self,
        need: Stage1MemoryNeed,
        capability: SnapshotCapability,
        *,
        access_scope: str = "writer_safe",
    ) -> RetrievalRoutingFeatures:
        return RetrievalRoutingFeatures(
            query_intent=need.query_intent,
            information_domains=self._domain_router.domains_for(need, ResolutionTier.R2),
            exact_id_count=len(need.entity_ids),
            resolved_entity_count=len(need.entity_ids),
            predicate_count=len(need.predicates),
            lexical_specificity=(
                1.0
                if need.query_intent
                in {Stage1QueryIntent.EXACT_QUOTE, Stage1QueryIntent.RARE_PHRASE}
                else 0.0
            ),
            quoted_phrase_length=(
                len(need.query_text) if need.query_intent is Stage1QueryIntent.EXACT_QUOTE else 0
            ),
            semantic_openness=(
                1.0
                if need.query_intent
                in {
                    Stage1QueryIntent.SEMANTIC_HISTORY,
                    Stage1QueryIntent.RELATED_EVENT,
                    Stage1QueryIntent.CHARACTER_ARC,
                }
                else 0.0
            ),
            temporal_scope_kind="bounded" if need.time_scope is not None else "unspecified",
            temporal_complexity="point" if need.time_scope is not None else "none",
            relation_hops_requested=(
                2
                if need.query_intent
                in {Stage1QueryIntent.CAUSAL_MULTI_HOP, Stage1QueryIntent.RELATION_CHAIN}
                else 0
            ),
            hierarchy_scope=(
                "chapter"
                if need.query_intent is Stage1QueryIntent.CHAPTER_THREAD
                else "global"
                if need.query_intent
                in {Stage1QueryIntent.GLOBAL_ARC, Stage1QueryIntent.CHARACTER_ARC}
                else "unspecified"
            ),
            continuous_prose_required=need.query_intent
            in {Stage1QueryIntent.STYLE_VOICE, Stage1QueryIntent.DIALOGUE_SAMPLE},
            evidence_strength_required="text_supported",
            mandatory=need.requirement.value == "mandatory",
            risk=need.risk_level,
            access_sensitivity=access_scope,
            latency_budget_ms=2_000,
            token_budget=2_000,
            snapshot_capabilities=capability.available_channels,
        )

    def plan(
        self,
        need: Stage1MemoryNeed,
        capability: SnapshotCapability,
        *,
        features: RetrievalRoutingFeatures | None = None,
        slots: tuple[R0ContextSlot, ...] = (),
        access_scope: str = "writer_safe",
    ) -> RoutePlan:
        if capability.source_commit != need.base_commit:
            raise ValueError("routing capability basis does not match memory need")
        chosen_features = features or self.features_for(need, capability, access_scope=access_scope)
        decision = self._tier_router.decide(
            need, capability, slots=slots, access_scope=access_scope
        )
        profile = profile_for(need.query_intent, decision.tier)
        query_bundle = NeedQueryCompiler().compile(need)
        registered_route = ROUTES[need.query_intent]
        registered_channels = tuple(
            dict.fromkeys((*registered_route.channels, *registered_route.fallback_channels))
        )
        query_channels, query_unavailable = NeedQueryCompiler.eligible_channels(
            need, query_bundle, registered_channels
        )
        capability_channels = set(capability.available_channels)
        available = {
            channel
            for channel in capability_channels
            if _pool(channel) in need.allowed_candidate_pools
            and channel in registered_channels
            and channel in query_channels
        }
        if decision.tier is ResolutionTier.R0:
            available.add(RetrievalChannel.R0)
        excluded = self._excluded(
            profile,
            capability_channels,
            available,
            registered_channels=set(registered_channels),
            query_unavailable=query_unavailable,
        )
        mandatory = tuple(step for step in profile.mandatory_steps if step.channel in available)
        groups = tuple(
            group
            for group in (self._mask_group(group, available) for group in profile.primary_groups)
            if group is not None
        )
        fallbacks = tuple(
            fallback
            for fallback in (
                self._mask_fallback(fallback, available)
                for fallback in profile.conditional_fallbacks
            )
            if fallback is not None
        )
        if decision.tier is ResolutionTier.R0 and decision.matched_slot_id is None:
            raise ValueError("R0 route requires a same-basis context slot")
        if decision.tier is ResolutionTier.R1 and not mandatory:
            raise ValueError("R1 route has no certified mandatory exact step")
        features_hash = ArtifactId(
            "sha256:"
            + hashlib.sha256(
                canonical_json_bytes(chosen_features.model_dump(mode="json"))
            ).hexdigest()
        )
        identity = hashlib.sha256(
            (
                f"{profile.profile_id.root}\0{need.need_id.root}\0{need.base_commit.root}\0"
                f"{capability.snapshot_id.root}\0{features_hash.root}\0"
                f"{hashlib.sha256(canonical_json_bytes(query_bundle.model_dump(mode='json'))).hexdigest()}"
            ).encode()
        ).hexdigest()
        plan = RoutePlan(
            route_plan_id=StableId(f"route.{identity[:48]}"),
            profile_id=profile.profile_id,
            need_id=need.need_id,
            base_commit=need.base_commit,
            snapshot_id=capability.snapshot_id,
            resolution_tier=decision.tier,
            domains=self._domain_router.domains_for(need, decision.tier),
            normalized_intent=need.query_intent,
            routing_features_hash=features_hash,
            mandatory_steps=mandatory,
            primary_groups=groups,
            conditional_fallbacks=fallbacks,
            graph_policy=(
                profile.graph_policy
                if RetrievalChannel.TYPED_GRAPH in available
                and (
                    chosen_features.resolved_entity_count > 0 or decision.tier is ResolutionTier.R2
                )
                else None
            ),
            evidence_policy=profile.evidence_policy,
            stop_policy=profile.stop_policy,
            excluded_channels=excluded,
            compiled_query_bundle=query_bundle,
            effective_channels=tuple(
                dict.fromkeys(
                    step.channel
                    for step in (
                        *mandatory,
                        *(step for group in groups for step in group.steps),
                        *(step for fallback in fallbacks for step in fallback.steps),
                    )
                )
            ),
            query_unavailable_reasons=query_unavailable,
            policy_version=ROUTE_POLICY_VERSION,
        )
        RoutePlanValidator().validate(plan, need, capability, profile)
        return plan

    @staticmethod
    def _mask_group(
        group: RouteStepGroup, available: set[RetrievalChannel]
    ) -> RouteStepGroup | None:
        steps = tuple(step for step in group.steps if step.channel in available)
        if not steps:
            return None
        return group.model_copy(update={"steps": steps})

    @staticmethod
    def _mask_fallback(
        fallback: ConditionalFallback, available: set[RetrievalChannel]
    ) -> ConditionalFallback | None:
        steps = tuple(step for step in fallback.steps if step.channel in available)
        if not steps:
            return None
        return fallback.model_copy(
            update={
                "steps": steps,
                "fusion_profile": fallback.fusion_profile if len(steps) > 1 else None,
            }
        )

    @staticmethod
    def _excluded(
        profile: RouteProfile,
        capability_channels: set[RetrievalChannel],
        available: set[RetrievalChannel],
        *,
        registered_channels: set[RetrievalChannel],
        query_unavailable: dict[RetrievalChannel, str],
    ) -> tuple[ExcludedChannel, ...]:
        planned = {
            step.channel
            for step in (
                *profile.mandatory_steps,
                *(step for group in profile.primary_groups for step in group.steps),
                *(step for fallback in profile.conditional_fallbacks for step in fallback.steps),
            )
        }
        excluded = [
            ExcludedChannel(channel=channel, reason="snapshot_capability_missing")
            for channel in sorted(
                planned - capability_channels - {RetrievalChannel.R0},
                key=lambda channel: channel.value,
            )
        ]
        excluded.extend(
            ExcludedChannel(channel=channel, reason="not_selected_by_registered_route")
            for channel in sorted(
                planned & capability_channels - registered_channels,
                key=lambda channel: channel.value,
            )
        )
        excluded.extend(
            ExcludedChannel(channel=channel, reason=query_unavailable[channel])
            for channel in sorted(
                planned & capability_channels & set(query_unavailable),
                key=lambda channel: channel.value,
            )
        )
        excluded.extend(
            ExcludedChannel(channel=channel, reason="candidate_pool_forbidden")
            for channel in sorted(
                planned
                & capability_channels
                & registered_channels - set(query_unavailable) - available,
                key=lambda channel: channel.value,
            )
        )
        excluded.extend(
            ExcludedChannel(channel=channel, reason="not_selected_by_registered_route")
            for channel in sorted(
                capability_channels - planned,
                key=lambda channel: channel.value,
            )
        )
        return tuple(excluded)


class RoutePlanValidator:
    """Reject routes that expand a runtime capability or bypass registered policy."""

    def validate(
        self,
        plan: RoutePlan,
        need: Stage1MemoryNeed,
        capability: SnapshotCapability,
        profile: RouteProfile,
    ) -> None:
        if plan.need_id != need.need_id or plan.base_commit != need.base_commit:
            raise ValueError("route plan basis does not match memory need")
        if plan.snapshot_id != capability.snapshot_id:
            raise ValueError("route plan snapshot does not match runtime capability")
        if plan.resolution_tier is not profile.resolution_tier:
            raise ValueError("route plan tier differs from registered profile")
        if plan.normalized_intent is not profile.query_intent:
            raise ValueError("route plan intent differs from registered profile")
        active = _active_channels(plan)
        excluded = {item.channel for item in plan.excluded_channels}
        available = set(capability.available_channels)
        if plan.resolution_tier is ResolutionTier.R0:
            available.add(RetrievalChannel.R0)
        if not active.issubset(available):
            raise ValueError("route plan exposes a channel absent from snapshot capability")
        if active & excluded:
            raise ValueError("route plan exposes an excluded channel")
        if not active.issubset(set(profile.allowed_channels)):
            raise ValueError("route plan exposes a channel absent from registered profile")
        if plan.resolution_tier is ResolutionTier.R0 and active != {RetrievalChannel.R0}:
            raise ValueError("R0 route may only expose the context-local channel")
        if plan.resolution_tier is ResolutionTier.R1:
            registered_r1_channels = {
                RetrievalChannel.R1_EXACT,
                RetrievalChannel.R1_TEMPORAL,
                *(
                    step.channel
                    for fallback in plan.conditional_fallbacks
                    for step in fallback.steps
                ),
            }
            if any(channel not in registered_r1_channels for channel in active):
                raise ValueError("R1 route may only expose registered exact/temporal channels")


class CounterfactualRouteEvaluator:
    """Evaluator-only all-channel ablation; never mutates a production RoutePlan."""

    def evaluate(
        self,
        plan: RoutePlan,
        need: Stage1MemoryNeed,
        backend: RetrievalBackend,
        *,
        added_channels: tuple[RetrievalChannel, ...],
        limit: int = 20,
    ) -> CounterfactualRouteRecord:
        if need.access_scope != "evaluator":
            raise ValueError("counterfactual route evaluation requires evaluator access")
        if plan.need_id != need.need_id or plan.base_commit != need.base_commit:
            raise ValueError("counterfactual route basis does not match memory need")
        if not added_channels or len(added_channels) != len(set(added_channels)):
            raise ValueError("counterfactual channels must be non-empty and unique")
        if set(added_channels) & _active_channels(plan):
            raise ValueError("counterfactual channels must be absent from the production route")
        if limit < 1:
            raise ValueError("counterfactual candidate limit must be positive")
        results = {channel: backend.search(need, channel, limit) for channel in added_channels}
        candidates = FusionService().fuse(results, limit=limit)
        return CounterfactualRouteRecord(
            record_id=StableId(f"counterfactual.{plan.route_plan_id.root}"),
            route_plan_id=plan.route_plan_id,
            need_id=need.need_id,
            base_commit=need.base_commit,
            snapshot_id=plan.snapshot_id,
            added_channels=added_channels,
            candidate_count=len(candidates),
            selected_count=sum(candidate.selected for candidate in candidates),
            evaluator_only=True,
        )


def profile_for(intent: Stage1QueryIntent, tier: ResolutionTier) -> RouteProfile:
    """Return the immutable v0.1 route registration for one normalized intent/tier."""

    profile_id = StableId(f"route-profile.stage2r.{tier.value}.{intent.value}")
    evidence = EvidenceExpansionPolicy(required_strength="text_supported")
    stop = RouteStopPolicy(stop_when="mandatory_gaps_closed_and_evidence_sufficient")
    if tier is ResolutionTier.R0:
        step = _step(intent, RetrievalChannel.R0, CandidatePool.R0, mandatory=True)
        return RouteProfile(
            profile_id=profile_id,
            version=ROUTE_PROFILE_VERSION,
            query_intent=intent,
            resolution_tier=tier,
            allowed_channels=(RetrievalChannel.R0,),
            mandatory_steps=(step,),
            evidence_policy=evidence,
            stop_policy=stop,
        )
    if tier is ResolutionTier.R1:
        channels = _r1_channels(intent)
        steps = tuple(
            _step(intent, channel, CandidatePool.R1, mandatory=True) for channel in channels
        )
        fallbacks: tuple[ConditionalFallback, ...] = (
            (
                _fallback(
                    intent,
                    "exact_current_record_absent",
                    (RetrievalChannel.ANCHOR_BM25, RetrievalChannel.ANCHOR_DENSE),
                ),
            )
            if intent is Stage1QueryIntent.CURRENT_STATE
            else ()
        )
        allowed_channels = tuple(
            dict.fromkeys(
                (
                    *channels,
                    *(step.channel for fallback in fallbacks for step in fallback.steps),
                )
            )
        )
        return RouteProfile(
            profile_id=profile_id,
            version=ROUTE_PROFILE_VERSION,
            query_intent=intent,
            resolution_tier=tier,
            allowed_channels=allowed_channels,
            mandatory_steps=steps,
            conditional_fallbacks=fallbacks,
            evidence_policy=evidence,
            stop_policy=stop,
        )
    allowed, groups, fallbacks, graph = _r2_registration(intent)
    return RouteProfile(
        profile_id=profile_id,
        version=ROUTE_PROFILE_VERSION,
        query_intent=intent,
        resolution_tier=ResolutionTier.R2,
        allowed_channels=allowed,
        primary_groups=groups,
        conditional_fallbacks=fallbacks,
        graph_policy=graph,
        evidence_policy=evidence,
        stop_policy=stop,
    )


def _r1_channels(intent: Stage1QueryIntent) -> tuple[RetrievalChannel, ...]:
    if intent in {Stage1QueryIntent.CURRENT_STATE, Stage1QueryIntent.MANDATORY_CONSTRAINT}:
        return (RetrievalChannel.R1_EXACT, RetrievalChannel.R1_TEMPORAL)
    return (RetrievalChannel.R1_EXACT,)


def _r2_registration(
    intent: Stage1QueryIntent,
) -> tuple[
    tuple[RetrievalChannel, ...],
    tuple[RouteStepGroup, ...],
    tuple[ConditionalFallback, ...],
    GraphTraversalPolicy | None,
]:
    anchor = _parallel_group(intent, (RetrievalChannel.ANCHOR_BM25, RetrievalChannel.ANCHOR_DENSE))
    grounded = _fallback(
        intent,
        "anchor_evidence_insufficient",
        (RetrievalChannel.GROUNDED_BM25, RetrievalChannel.GROUNDED_DENSE),
    )
    if intent in {Stage1QueryIntent.SEMANTIC_HISTORY, Stage1QueryIntent.RELATED_EVENT}:
        return (
            (
                RetrievalChannel.ANCHOR_BM25,
                RetrievalChannel.ANCHOR_DENSE,
                RetrievalChannel.GROUNDED_BM25,
                RetrievalChannel.GROUNDED_DENSE,
            ),
            (anchor,),
            (grounded,),
            None,
        )
    if intent in {
        Stage1QueryIntent.CURRENT_STATE,
        Stage1QueryIntent.KNOWN_ID,
        Stage1QueryIntent.MANDATORY_CONSTRAINT,
    }:
        # A current/known-id Need whose exact entity id is absent (for example
        # an unresolved lexical institution anchor) keeps its public
        # semantic/lexical query executable through the Anchor and Grounded
        # BM25+dense routes; only the id-dependent exact/graph routes are
        # closed.  The query text is never dropped because a mention lacks a
        # runtime id.
        return (
            (
                RetrievalChannel.ANCHOR_BM25,
                RetrievalChannel.ANCHOR_DENSE,
                RetrievalChannel.GROUNDED_BM25,
                RetrievalChannel.GROUNDED_DENSE,
            ),
            (anchor,),
            (grounded,),
            None,
        )
    if intent in {Stage1QueryIntent.EXACT_QUOTE, Stage1QueryIntent.RARE_PHRASE}:
        step = _step(intent, RetrievalChannel.GROUNDED_BM25, CandidatePool.GROUNDED)
        return ((RetrievalChannel.GROUNDED_BM25,), (_serial_group(intent, (step,)),), (), None)
    if intent in {
        Stage1QueryIntent.STYLE_VOICE,
        Stage1QueryIntent.DIALOGUE_SAMPLE,
        Stage1QueryIntent.ANCHOR_INSUFFICIENT,
    }:
        group = _parallel_group(
            intent, (RetrievalChannel.GROUNDED_BM25, RetrievalChannel.GROUNDED_DENSE)
        )
        return (
            (RetrievalChannel.GROUNDED_BM25, RetrievalChannel.GROUNDED_DENSE),
            (group,),
            (),
            None,
        )
    if intent in {Stage1QueryIntent.RELATION_CHAIN, Stage1QueryIntent.CAUSAL_MULTI_HOP}:
        group = _parallel_group(
            intent,
            (
                RetrievalChannel.TYPED_GRAPH,
                RetrievalChannel.ANCHOR_BM25,
                RetrievalChannel.ANCHOR_DENSE,
            ),
        )
        return (
            (
                RetrievalChannel.TYPED_GRAPH,
                RetrievalChannel.ANCHOR_BM25,
                RetrievalChannel.ANCHOR_DENSE,
            ),
            (group,),
            (),
            GraphTraversalPolicy(),
        )
    if intent in {
        Stage1QueryIntent.GLOBAL_ARC,
        Stage1QueryIntent.CHAPTER_THREAD,
        Stage1QueryIntent.CHARACTER_ARC,
    }:
        hierarchy = _step(intent, RetrievalChannel.HIERARCHY, CandidatePool.HIERARCHY)
        return (
            (
                RetrievalChannel.HIERARCHY,
                RetrievalChannel.ANCHOR_BM25,
                RetrievalChannel.ANCHOR_DENSE,
            ),
            (_serial_group(intent, (hierarchy,)),),
            (
                _fallback(
                    intent,
                    "hierarchy_scope_resolved",
                    (RetrievalChannel.ANCHOR_BM25, RetrievalChannel.ANCHOR_DENSE),
                ),
            ),
            GraphTraversalPolicy() if intent is Stage1QueryIntent.CHARACTER_ARC else None,
        )
    if intent is Stage1QueryIntent.PLAN_OBLIGATION:
        return (
            (
                RetrievalChannel.ANCHOR_BM25,
                RetrievalChannel.ANCHOR_DENSE,
                RetrievalChannel.HIERARCHY,
            ),
            (anchor,),
            (_fallback(intent, "plan_anchor_insufficient", (RetrievalChannel.HIERARCHY,)),),
            None,
        )
    # Exact intents with insufficient identifiers enter an R2 clarification path, not broad search.
    return ((), (), (), None)


def _step(
    intent: Stage1QueryIntent,
    channel: RetrievalChannel,
    pool: CandidatePool,
    *,
    mandatory: bool = False,
) -> RouteStep:
    return RouteStep(
        step_id=StableId(f"route-step.stage2r.{intent.value}.{channel.value}"),
        channel=channel,
        candidate_pool=pool,
        query_template=f"{intent.value}:{channel.value}:v0.1",
        mandatory=mandatory,
    )


def _parallel_group(
    intent: Stage1QueryIntent,
    channels: tuple[RetrievalChannel, ...],
) -> RouteStepGroup:
    return RouteStepGroup(
        group_id=StableId(f"route-group.stage2r.{intent.value}.{channels[0].value}"),
        execution=RouteExecution.PARALLEL,
        steps=tuple(_step(intent, channel, _pool(channel)) for channel in channels),
        fusion_profile="application-rrf-v1",
    )


def _serial_group(intent: Stage1QueryIntent, steps: tuple[RouteStep, ...]) -> RouteStepGroup:
    return RouteStepGroup(
        group_id=StableId(f"route-group.stage2r.{intent.value}.{steps[0].channel.value}"),
        execution=RouteExecution.SERIAL,
        steps=steps,
    )


def _fallback(
    intent: Stage1QueryIntent,
    condition: str,
    channels: tuple[RetrievalChannel, ...],
) -> ConditionalFallback:
    return ConditionalFallback(
        fallback_id=StableId(f"route-fallback.stage2r.{intent.value}.{condition}"),
        condition=condition,
        steps=tuple(_step(intent, channel, _pool(channel)) for channel in channels),
        fusion_profile="application-rrf-v1" if len(channels) > 1 else None,
    )


def _pool(channel: RetrievalChannel) -> CandidatePool:
    if channel in {RetrievalChannel.R1_EXACT, RetrievalChannel.R1_TEMPORAL}:
        return CandidatePool.R1
    if channel is RetrievalChannel.TYPED_GRAPH:
        return CandidatePool.GRAPH
    if channel is RetrievalChannel.HIERARCHY:
        return CandidatePool.HIERARCHY
    if channel in {RetrievalChannel.GROUNDED_BM25, RetrievalChannel.GROUNDED_DENSE}:
        return CandidatePool.GROUNDED
    if channel is RetrievalChannel.R0:
        return CandidatePool.R0
    return CandidatePool.ANCHOR


def _active_channels(plan: RoutePlan) -> set[RetrievalChannel]:
    return {
        step.channel
        for step in (
            *plan.mandatory_steps,
            *(step for group in plan.primary_groups for step in group.steps),
            *(step for fallback in plan.conditional_fallbacks for step in fallback.steps),
        )
    }
