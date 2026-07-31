"""Checkpointable bounded Stage 2 Memory Controller over typed read-only tools."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    CandidatePool,
    ChannelHit,
    FusedCandidate,
    NeedExecutionStatus,
    NeedFacetKind,
    NeedRisk,
    RequirementLevel,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalTrace,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1ContextPackage,
    Stage1MemoryNeed,
)
from novel_agent.domain.retrieval_routing import RoutePlan
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    ContextAssemblySpec,
    ContextResolutionResult,
    ContractRef,
    ControllerPolicyAction,
    ControllerPolicyDecision,
    ControllerStopReason,
    EvidenceLedgerEntry,
    ExecutionStatus,
    MemoryResolutionRequest,
    MemorySelection,
    ResolutionStatus,
    SelectionDecision,
    SufficiencyReport,
    ToolCallContext,
    ToolFailureCode,
    ToolPolicy,
    ToolResult,
    ToolResultStatus,
)
from novel_agent.prompts.registry import content_hash
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.controller_legal_actions import LegalActionProvider
from novel_agent.services.memory_pipeline import ContextCompiler
from novel_agent.services.retrieval import ROUTES, FusionService, RerankService
from novel_agent.tools.contracts import ControllerBudgetState, ToolBinding, ToolInvocation
from novel_agent.tools.retrieval import CHANNEL_BY_TOOL, PLAN_INTENTS, POOL_BY_CHANNEL

TOOL_BY_CHANNEL = {channel: name for name, channel in CHANNEL_BY_TOOL.items()}


class ControllerGraphState(TypedDict, total=False):
    request: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    policy_decisions: list[dict[str, Any]]
    pending_tool: str
    pending_need_id: str
    pending_action_ids: list[str]
    stopped: bool
    stop_reason: str
    decision_model_calls: int
    terminal_failure: str | None


class ControllerStateView(TypedDict):
    request: MemoryResolutionRequest
    tool_calls: tuple[tuple[StableId, str, ToolResult], ...]


class ControllerPolicy(Protocol):
    @property
    def contract_ref(self) -> ContractRef: ...

    @property
    def prompt_fingerprint(self) -> ArtifactId: ...

    @property
    def tool_policy_hash(self) -> ArtifactId | None: ...

    def decide(self, state: ControllerStateView) -> ControllerPolicyDecision: ...

    def decision_receipt(self, model_call_id: StableId) -> AgentExecutionReceipt | None: ...


class RouteBoundControllerPolicy:
    """Safe baseline policy: registered route order, no query invention, no duplicate calls."""

    def __init__(self, route_plans: tuple[RoutePlan, ...] = ()) -> None:
        plans = {plan.need_id: plan for plan in route_plans}
        if len(plans) != len(route_plans):
            raise ValueError("controller route plans must have unique memory need ids")
        self._route_plans = plans

    @property
    def contract_ref(self) -> ContractRef:
        fingerprint = content_hash(b"route-bound-controller-policy-v2")
        return ContractRef(
            contract_id=StableId("controller-policy.route-bound"),
            version=SchemaVersion("2.0.0"),
            content_hash=fingerprint,
        )

    @property
    def prompt_fingerprint(self) -> ArtifactId:
        return content_hash(b"route-bound-controller-policy-v2")

    @property
    def tool_policy_hash(self) -> ArtifactId | None:
        return None

    def decide(self, state: ControllerStateView) -> ControllerPolicyDecision:
        request = state["request"]
        calls = state["tool_calls"]
        if self._route_plans:
            return self._decide_registered_route_plans(request, calls)
        successful = {
            need_id
            for need_id, _, result in calls
            if result.status is ToolResultStatus.SUCCEEDED and result.coverage > 0
        }
        called = {(need_id, tool_name) for need_id, tool_name, _ in calls}
        access_scope = request.access_scope.value
        allow_future_plan = request.allow_future_plan
        mandatory_missing = tuple(
            need
            for need in request.initial_memory_needs
            if need.requirement is RequirementLevel.MANDATORY
            and need.need_id not in successful
            and self._need_is_accessible(need, access_scope, allow_future_plan)
        )
        optional_missing = tuple(
            need
            for need in request.initial_memory_needs
            if need.need_id not in successful
            and self._need_is_accessible(need, access_scope, allow_future_plan)
        )
        if not mandatory_missing:
            return ControllerPolicyDecision(
                action=ControllerPolicyAction.STOP,
                stop_reason=ControllerStopReason.SUFFICIENT,
                rationale_code="MANDATORY_NEEDS_SATISFIED",
            )
        max_calls = min(
            request.retrieval_budget.max_tool_calls,
            request.retrieval_budget.max_rounds * max(1, len(request.initial_memory_needs)),
        )
        if len(calls) >= max_calls:
            return ControllerPolicyDecision(
                action=ControllerPolicyAction.STOP,
                stop_reason=ControllerStopReason.BUDGET_EXHAUSTED,
                rationale_code="REQUEST_CALL_BUDGET_EXHAUSTED",
            )
        for need in (*mandatory_missing, *optional_missing):
            channels = self._channels_for_need(need)
            for channel in channels:
                if POOL_BY_CHANNEL[channel] not in need.allowed_candidate_pools:
                    continue
                tool_name = TOOL_BY_CHANNEL[channel]
                if (need.need_id, tool_name) not in called:
                    return ControllerPolicyDecision(
                        action=ControllerPolicyAction.CALL_TOOL,
                        need_id=need.need_id,
                        tool_name=tool_name,
                        rationale_code="NEXT_REGISTERED_ROUTE",
                    )
        return ControllerPolicyDecision(
            action=ControllerPolicyAction.STOP,
            stop_reason=ControllerStopReason.MANDATORY_GAP_UNRESOLVED,
            rationale_code="REGISTERED_ROUTES_EXHAUSTED",
        )

    def _decide_registered_route_plans(
        self,
        request: MemoryResolutionRequest,
        calls: tuple[tuple[StableId, str, ToolResult], ...],
    ) -> ControllerPolicyDecision:
        """Advance each Need through its registered plan without broad fallback calls."""

        max_calls = min(
            request.retrieval_budget.max_tool_calls,
            request.retrieval_budget.max_rounds * max(1, len(request.initial_memory_needs)),
        )
        if len(calls) >= max_calls:
            return ControllerPolicyDecision(
                action=ControllerPolicyAction.STOP,
                stop_reason=ControllerStopReason.BUDGET_EXHAUSTED,
                rationale_code="REQUEST_CALL_BUDGET_EXHAUSTED",
            )
        unresolved = False
        fallback_exhausted_without_gain = False
        access_scope = request.access_scope.value
        allow_future_plan = request.allow_future_plan
        actual_priority_needs = tuple(
            need
            for need in request.initial_memory_needs
            if (need.requirement is RequirementLevel.MANDATORY or need.risk_level is NeedRisk.HIGH)
            and self._need_is_accessible(need, access_scope, allow_future_plan)
        )
        next_actions: list[tuple[Stage1MemoryNeed, str, int]] = []
        for need in actual_priority_needs:
            plan = self._route_plans.get(need.need_id)
            if plan is None:
                raise ValueError(
                    "route-bound controller has no plan for a mandatory/high-risk priority "
                    "memory need"
                )
            need_calls = tuple(item for item in calls if item[0] == need.need_id)
            next_tool = self._next_registered_tool(plan, need_calls, need=need)
            if next_tool is not None:
                next_actions.append((need, next_tool, len(need_calls)))
            elif need.requirement is RequirementLevel.MANDATORY and not any(
                result.status is ToolResultStatus.SUCCEEDED and result.coverage > 0
                for _, _, result in need_calls
            ):
                unresolved = True
                fallback_tools = {
                    TOOL_BY_CHANNEL[step.channel]
                    for fallback in plan.conditional_fallbacks
                    for step in fallback.steps
                }
                fallback_results = tuple(
                    result for _, tool_name, result in need_calls if tool_name in fallback_tools
                )
                fallback_exhausted_without_gain = fallback_exhausted_without_gain or (
                    bool(fallback_results)
                    and all(result.new_information_gain == 0 for result in fallback_results)
                )
        if next_actions:
            # Deficit round-robin: no priority Need receives its N+1 call while
            # another actual priority Need has received fewer calls.  Stable
            # public risk/priority/Need ID fields break ties deterministically.
            risk_order = {NeedRisk.HIGH: 0, NeedRisk.MEDIUM: 1, NeedRisk.LOW: 2}
            need, next_tool, _ = min(
                next_actions,
                key=lambda item: (
                    item[2],
                    0 if item[0].requirement is RequirementLevel.MANDATORY else 1,
                    risk_order[item[0].risk_level],
                    -item[0].priority,
                    item[0].need_id.root,
                ),
            )
            return ControllerPolicyDecision(
                action=ControllerPolicyAction.CALL_TOOL,
                need_id=need.need_id,
                tool_name=next_tool,
                rationale_code="MAX_MIN_REGISTERED_ROUTE_STEP",
            )
        if unresolved:
            return ControllerPolicyDecision(
                action=ControllerPolicyAction.STOP,
                stop_reason=(
                    ControllerStopReason.NO_ADDITIONAL_EVIDENCE
                    if fallback_exhausted_without_gain
                    else ControllerStopReason.MANDATORY_GAP_UNRESOLVED
                ),
                rationale_code=(
                    "REGISTERED_FALLBACK_ADDED_NO_INFORMATION"
                    if fallback_exhausted_without_gain
                    else "REGISTERED_ROUTE_EXHAUSTED_WITHOUT_EVIDENCE"
                ),
            )
        return ControllerPolicyDecision(
            action=ControllerPolicyAction.STOP,
            stop_reason=ControllerStopReason.SUFFICIENT,
            rationale_code="MANDATORY_REGISTERED_ROUTES_COMPLETED",
        )

    @staticmethod
    def _next_registered_tool(
        plan: RoutePlan,
        calls: tuple[tuple[StableId, str, ToolResult], ...],
        *,
        need: Stage1MemoryNeed | None = None,
    ) -> str | None:
        called_tools = {tool_name for _, tool_name, _ in calls}
        primary_steps = (
            *plan.mandatory_steps,
            *(step for group in plan.primary_groups for step in group.steps),
        )
        for step in primary_steps:
            tool_name = TOOL_BY_CHANNEL[step.channel]
            if tool_name not in called_tools:
                return tool_name
        primary_channels = {step.channel for step in primary_steps}
        primary_succeeded = any(
            TOOL_BY_CHANNEL.get(channel) == tool_name
            and result.status is ToolResultStatus.SUCCEEDED
            and result.coverage > 0
            for _, tool_name, result in calls
            for channel in primary_channels
        )
        primary_sufficient = (
            RouteBoundControllerPolicy._public_primary_sufficient(need, calls)
            if need is not None and primary_succeeded
            else primary_succeeded
        )
        for fallback in plan.conditional_fallbacks:
            if not RouteBoundControllerPolicy._fallback_applies(
                fallback.condition, primary_sufficient
            ):
                continue
            for step in fallback.steps:
                tool_name = TOOL_BY_CHANNEL[step.channel]
                if tool_name not in called_tools:
                    return tool_name
        return None

    @staticmethod
    def _public_primary_sufficient(
        need: Stage1MemoryNeed,
        calls: tuple[tuple[StableId, str, ToolResult], ...],
    ) -> bool:
        """Conservative public-facet signal used only to decide legal fallback."""

        units: list[RetrievalUnit] = []
        for _need_id, _tool_name, result in calls:
            if result.status is not ToolResultStatus.SUCCEEDED or not isinstance(
                result.payload,
                dict,
            ):
                continue
            raw_hits = result.payload.get("hits")
            if not isinstance(raw_hits, list):
                continue
            for raw_hit in raw_hits:
                try:
                    units.append(ChannelHit.model_validate_json(json.dumps(raw_hit)).unit)
                except (TypeError, ValueError):
                    continue
        if not units:
            return False
        if need.completion_spec is None:
            return True
        text = " ".join(item.text.casefold() for item in units)
        limitation_terms = ("cannot", "unable", "limit", "cost", "限制", "无法", "不能", "代价")
        unresolved_terms = (
            "unresolved",
            "pending",
            "remain",
            "promise",
            "oath",
            "尚",
            "未",
            "仍",
            "等待",
            "承诺",
            "誓",
            "义务",
            "必须",
            "需要",
        )
        historical_kinds = {
            RetrievalUnitKind.EVENT_ANCHOR,
            RetrievalUnitKind.SCENE_ANCHOR,
            RetrievalUnitKind.CHAPTER_ANCHOR,
            RetrievalUnitKind.GROUNDED_BLOCK,
            RetrievalUnitKind.GROUNDED_SPAN,
        }
        plan_kinds = {
            RetrievalUnitKind.PLAN_ANCHOR,
            RetrievalUnitKind.ARC_ANCHOR,
        }
        covered: set[StableId] = set()
        for facet in need.need_facets:
            matches = (
                (
                    facet.facet_kind
                    in {
                        NeedFacetKind.CURRENT_STATE,
                        NeedFacetKind.RELATION_STATE,
                        NeedFacetKind.CAPABILITY_STATUS,
                        NeedFacetKind.KNOWLEDGE_BOUNDARY,
                    }
                    and any(unit.unit_kind not in historical_kinds | plan_kinds for unit in units)
                )
                or (
                    facet.facet_kind is NeedFacetKind.LIMITATION
                    and any(term in text for term in limitation_terms)
                )
                or (
                    facet.facet_kind in {NeedFacetKind.CAUSAL_HISTORY, NeedFacetKind.SETUP}
                    and any(unit.unit_kind in historical_kinds for unit in units)
                )
                or (
                    facet.facet_kind in {NeedFacetKind.UNRESOLVED_STATUS, NeedFacetKind.COMMITMENT}
                    and any(term in text for term in unresolved_terms)
                )
                or (
                    facet.facet_kind is NeedFacetKind.PLAN_NODE
                    and any(unit.unit_kind in plan_kinds for unit in units)
                )
            )
            if matches:
                covered.add(facet.need_facet_id)
        if not set(need.completion_spec.required_need_facet_ids).issubset(covered):
            return False
        terms = tuple(
            dict.fromkeys(
                token
                for token in re.findall(
                    r"[a-z0-9_]+|[\u4e00-\u9fff]{2}",
                    need.query_text.casefold(),
                )
                if len(token) >= 2
            )
        )
        return not terms or sum(term in text for term in terms) / len(terms) >= 0.25

    @staticmethod
    def _fallback_applies(condition: str, primary_succeeded: bool) -> bool:
        if condition in {"anchor_evidence_insufficient", "plan_anchor_insufficient"}:
            return not primary_succeeded
        if condition == "hierarchy_scope_resolved":
            return primary_succeeded
        raise ValueError(f"unregistered route fallback condition: {condition}")

    def _channels_for_need(self, need: Stage1MemoryNeed) -> tuple[RetrievalChannel, ...]:
        plan = self._route_plans.get(need.need_id)
        if plan is None:
            route = ROUTES[need.query_intent]
            return (*route.channels, *route.fallback_channels)
        return tuple(
            dict.fromkeys(
                step.channel
                for step in (
                    *plan.mandatory_steps,
                    *(step for group in plan.primary_groups for step in group.steps),
                    *(step for fallback in plan.conditional_fallbacks for step in fallback.steps),
                )
            )
        )

    @staticmethod
    def _need_is_accessible(
        need: Stage1MemoryNeed,
        access_scope: str,
        allow_future_plan: bool,
    ) -> bool:
        """Mirror RetrievalToolAdapter access checks so policy skips denied needs."""

        if need.access_scope != access_scope:
            return False
        return not (
            (need.query_intent in PLAN_INTENTS or need.allow_plan) and not allow_future_plan
        )

    def decision_receipt(self, model_call_id: StableId) -> AgentExecutionReceipt | None:
        return None


class BoundedControllerRun(TypedDict):
    resolution: ContextResolutionResult
    context: Stage1ContextPackage
    tool_results: tuple[ToolResult, ...]
    decision_receipts: tuple[AgentExecutionReceipt, ...]


class BoundedMemoryController:
    def __init__(
        self,
        binding: ToolBinding,
        tool_policy: ToolPolicy,
        context_compiler: ContextCompiler,
        policy: ControllerPolicy,
        freshness_check: Callable[[MemoryResolutionRequest], bool],
        checkpointer: Any,
        *,
        route_plans: tuple[RoutePlan, ...] = (),
        reranker: RerankService | None = None,
    ) -> None:
        policy_tool_hash = getattr(policy, "tool_policy_hash", None)
        if policy_tool_hash is not None and policy_tool_hash != tool_policy.content_hash:
            raise ValueError("Controller policy and ToolPolicy fingerprints differ")
        self._binding = binding
        self._tool_policy = tool_policy
        self._compiler = context_compiler
        self._policy = policy
        fallback_policy = RouteBoundControllerPolicy()
        self._policy_contract_ref = getattr(
            policy,
            "contract_ref",
            fallback_policy.contract_ref,
        )
        self._policy_prompt_fingerprint = getattr(
            policy,
            "prompt_fingerprint",
            fallback_policy.prompt_fingerprint,
        )
        self._route_plans = {plan.need_id: plan for plan in route_plans}
        self._reranker = reranker
        if len(self._route_plans) != len(route_plans):
            raise ValueError("bounded controller route plans must have unique memory need ids")
        self._freshness_check = freshness_check
        self._budgets: dict[str, ControllerBudgetState] = {}
        self._legal_actions = LegalActionProvider(
            tool_policy=tool_policy,
            route_plans=route_plans,
        )
        self._max_decision_model_calls = int(getattr(policy, "max_decision_model_calls", 2) or 2)
        builder = StateGraph(ControllerGraphState)
        builder.add_node("decide", self._decide)
        builder.add_node("execute_tool", self._execute_tool)
        builder.add_edge(START, "decide")
        builder.add_conditional_edges(
            "decide",
            self._route_after_decision,
            {"execute": "execute_tool", "finish": END},
        )
        builder.add_edge("execute_tool", "decide")
        self.graph = builder.compile(checkpointer=checkpointer)

    def resolve(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        *,
        thread_id: str,
    ) -> BoundedControllerRun:
        if request.retrieval_budget.max_tool_calls > self._tool_policy.max_tool_calls:
            raise ValueError("request tool budget exceeds registered ToolPolicy")
        if request.retrieval_budget.max_rounds > self._tool_policy.max_rounds:
            raise ValueError("request round budget exceeds registered ToolPolicy")
        if not self._freshness_check(request):
            return self._finalize_without_tools(
                request, text_root, ControllerStopReason.FRESHNESS_BLOCKED
            )
        self._legal_actions.assert_consistency(request, ())
        max_tools = self._trusted_call_limit(request)
        self._budgets[request.request_id.root] = ControllerBudgetState.from_policy(
            self._tool_policy,
            max_tool_calls=max_tools,
            max_decision_model_calls=self._max_decision_model_calls,
            wall_clock_budget_ms=min(
                self._tool_policy.wall_clock_budget_ms,
                request.retrieval_budget.wall_clock_budget_ms,
            ),
        )
        initial = ControllerGraphState(
            request=request.model_dump(mode="json"),
            tool_calls=[],
            policy_decisions=[],
            pending_action_ids=[],
            stopped=False,
            decision_model_calls=0,
        )
        try:
            state = cast(
                ControllerGraphState,
                self.graph.invoke(initial, {"configurable": {"thread_id": thread_id}}),
            )
        finally:
            self._budgets.pop(request.request_id.root, None)
        return self._finalize(request, text_root, state)

    def _decide(self, state: ControllerGraphState) -> ControllerGraphState:
        request = self._request_from_state(state)
        calls = self._parse_calls(state.get("tool_calls", []))
        budget = self._budgets[request.request_id.root]
        pending_action_ids = list(state.get("pending_action_ids") or [])

        if budget.terminal_failure is not None:
            stop_reason = budget.stop_reason_for_terminal()
            decision = ControllerPolicyDecision(
                action=ControllerPolicyAction.STOP,
                stop_reason=stop_reason,
                rationale_code="TERMINAL_TOOL_FAILURE",
            )
            return {
                "policy_decisions": [
                    *state.get("policy_decisions", []),
                    decision.model_dump(mode="json"),
                ],
                "stopped": True,
                "stop_reason": stop_reason.value,
                "terminal_failure": budget.terminal_failure,
                "pending_action_ids": [],
            }

        if budget.wall_clock_exhausted() or not budget.can_invoke_tool():
            decision = ControllerPolicyDecision(
                action=ControllerPolicyAction.STOP,
                stop_reason=ControllerStopReason.BUDGET_EXHAUSTED,
                rationale_code="TRUSTED_GRAPH_WALL_CLOCK_OR_TOOL_BUDGET_EXHAUSTED",
            )
            budget.mark_terminal(ControllerStopReason.BUDGET_EXHAUSTED.value)
            return {
                "policy_decisions": [
                    *state.get("policy_decisions", []),
                    decision.model_dump(mode="json"),
                ],
                "stopped": True,
                "stop_reason": ControllerStopReason.BUDGET_EXHAUSTED.value,
                "pending_action_ids": [],
            }

        if len(calls) >= self._trusted_call_limit(request):
            decision = ControllerPolicyDecision(
                action=ControllerPolicyAction.STOP,
                stop_reason=ControllerStopReason.BUDGET_EXHAUSTED,
                rationale_code="TRUSTED_GRAPH_CALL_BUDGET_EXHAUSTED",
            )
            return {
                "policy_decisions": [
                    *state.get("policy_decisions", []),
                    decision.model_dump(mode="json"),
                ],
                "stopped": True,
                "stop_reason": ControllerStopReason.BUDGET_EXHAUSTED.value,
                "pending_action_ids": [],
            }

        # Drain batch plan actions without another model call.
        if pending_action_ids:
            action_id = StableId(pending_action_ids[0])
            legal = {
                item.action_id: item
                for item in self._legal_actions.available_actions(request, calls)
            }
            registered = legal.get(action_id)
            if registered is None:
                decision = ControllerPolicyDecision(
                    action=ControllerPolicyAction.STOP,
                    stop_reason=ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
                    rationale_code="BATCH_ACTION_NO_LONGER_LEGAL",
                )
                return {
                    "policy_decisions": [
                        *state.get("policy_decisions", []),
                        decision.model_dump(mode="json"),
                    ],
                    "stopped": True,
                    "stop_reason": ControllerStopReason.NO_ADDITIONAL_EVIDENCE.value,
                    "pending_action_ids": [],
                }
            decision = ControllerPolicyDecision(
                action=ControllerPolicyAction.CALL_TOOL,
                need_id=registered.need_id,
                tool_name=registered.tool_name,
                rationale_code="BATCH_PLAN_ACTION",
            )
            return {
                "policy_decisions": [
                    *state.get("policy_decisions", []),
                    decision.model_dump(mode="json"),
                ],
                "pending_need_id": registered.need_id.root,
                "pending_tool": registered.tool_name,
                "pending_action_ids": pending_action_ids[1:],
                "stopped": False,
            }

        uses_model = hasattr(self._policy, "decision_receipts") or hasattr(
            self._policy, "max_decision_model_calls"
        )
        if uses_model and not budget.can_decide():
            decision = ControllerPolicyDecision(
                action=ControllerPolicyAction.STOP,
                stop_reason=ControllerStopReason.BUDGET_EXHAUSTED,
                rationale_code="DECISION_MODEL_BUDGET_EXHAUSTED",
            )
            budget.mark_terminal(ControllerStopReason.BUDGET_EXHAUSTED.value)
            return {
                "policy_decisions": [
                    *state.get("policy_decisions", []),
                    decision.model_dump(mode="json"),
                ],
                "stopped": True,
                "stop_reason": ControllerStopReason.BUDGET_EXHAUSTED.value,
                "pending_action_ids": [],
            }

        if uses_model:
            budget.record_decision_call()

        raw_decision = self._policy.decide({"request": request, "tool_calls": calls})
        if isinstance(raw_decision, ControllerPolicyDecision):
            decision = raw_decision
        elif isinstance(raw_decision, ControllerStopReason):
            decision = ControllerPolicyDecision(
                action=ControllerPolicyAction.STOP,
                stop_reason=raw_decision,
                rationale_code="LEGACY_POLICY_STOP",
            )
        else:
            need_id, tool_name = raw_decision
            decision = ControllerPolicyDecision(
                action=ControllerPolicyAction.CALL_TOOL,
                need_id=need_id,
                tool_name=tool_name,
                rationale_code="LEGACY_POLICY_TOOL_CALL",
            )

        # Model latency may exhaust wall clock before tool execution.
        if budget.wall_clock_exhausted():
            budget.mark_terminal(ControllerStopReason.BUDGET_EXHAUSTED.value)
            decision = ControllerPolicyDecision(
                action=ControllerPolicyAction.STOP,
                stop_reason=ControllerStopReason.BUDGET_EXHAUSTED,
                rationale_code="DEADLINE_AFTER_MODEL_DECISION",
            )
            return {
                "policy_decisions": [
                    *state.get("policy_decisions", []),
                    decision.model_dump(mode="json"),
                ],
                "stopped": True,
                "stop_reason": ControllerStopReason.BUDGET_EXHAUSTED.value,
                "decision_model_calls": budget.decision_model_calls_used,
                "pending_action_ids": [],
            }

        decisions = [
            *state.get("policy_decisions", []),
            decision.model_dump(mode="json"),
        ]
        if decision.action is ControllerPolicyAction.STOP:
            stop_reason = cast(ControllerStopReason, decision.stop_reason)
            return {
                "policy_decisions": decisions,
                "stopped": True,
                "stop_reason": stop_reason.value,
                "decision_model_calls": budget.decision_model_calls_used,
                "pending_action_ids": [],
            }
        if decision.action is ControllerPolicyAction.EXECUTE_PLAN:
            queued = [item.root for item in decision.pending_action_ids]
            if not queued:
                stop = ControllerPolicyDecision(
                    action=ControllerPolicyAction.STOP,
                    stop_reason=ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
                    rationale_code="EMPTY_BATCH_PLAN",
                )
                return {
                    "policy_decisions": [*decisions, stop.model_dump(mode="json")],
                    "stopped": True,
                    "stop_reason": ControllerStopReason.NO_ADDITIONAL_EVIDENCE.value,
                    "decision_model_calls": budget.decision_model_calls_used,
                    "pending_action_ids": [],
                }
            # Bind the first action immediately so execute_tool has pending_need_id
            # and pending_tool.  Remaining actions drain via the pending_action_ids
            # branch at the top of _decide without another model call.
            legal = {
                item.action_id: item
                for item in self._legal_actions.available_actions(request, calls)
            }
            first_registered = legal.get(StableId(queued[0]))
            if first_registered is None:
                stop = ControllerPolicyDecision(
                    action=ControllerPolicyAction.STOP,
                    stop_reason=ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
                    rationale_code="BATCH_FIRST_ACTION_NO_LONGER_LEGAL",
                )
                return {
                    "policy_decisions": [*decisions, stop.model_dump(mode="json")],
                    "stopped": True,
                    "stop_reason": ControllerStopReason.NO_ADDITIONAL_EVIDENCE.value,
                    "decision_model_calls": budget.decision_model_calls_used,
                    "pending_action_ids": [],
                }
            batch_decision = ControllerPolicyDecision(
                action=ControllerPolicyAction.CALL_TOOL,
                need_id=first_registered.need_id,
                tool_name=first_registered.tool_name,
                rationale_code="BATCH_PLAN_ACTION",
            )
            return {
                "policy_decisions": [*decisions, batch_decision.model_dump(mode="json")],
                "pending_need_id": first_registered.need_id.root,
                "pending_tool": first_registered.tool_name,
                "pending_action_ids": queued[1:],
                "stopped": False,
                "decision_model_calls": budget.decision_model_calls_used,
            }
        need_id = cast(StableId, decision.need_id)
        tool_name = cast(str, decision.tool_name)
        known_need_ids = {need.need_id for need in request.initial_memory_needs}
        if need_id not in known_need_ids:
            return {
                "policy_decisions": decisions,
                "stopped": True,
                "stop_reason": ControllerStopReason.ACCESS_BLOCKED.value,
                "decision_model_calls": budget.decision_model_calls_used,
            }
        if tool_name not in self._tool_policy.allowed_tools or tool_name not in CHANNEL_BY_TOOL:
            return {
                "policy_decisions": decisions,
                "stopped": True,
                "stop_reason": ControllerStopReason.ACCESS_BLOCKED.value,
                "decision_model_calls": budget.decision_model_calls_used,
            }
        if any(
            called_need_id == need_id and called_tool == tool_name
            for called_need_id, called_tool, _ in calls
        ):
            return {
                "policy_decisions": decisions,
                "stopped": True,
                "stop_reason": ControllerStopReason.NO_ADDITIONAL_EVIDENCE.value,
                "decision_model_calls": budget.decision_model_calls_used,
            }
        legal_pairs = {
            (item.need_id, item.tool_name)
            for item in self._legal_actions.available_actions(request, calls)
        }
        if (need_id, tool_name) not in legal_pairs:
            return {
                "policy_decisions": decisions,
                "stopped": True,
                "stop_reason": ControllerStopReason.ACCESS_BLOCKED.value,
                "decision_model_calls": budget.decision_model_calls_used,
            }
        return {
            "policy_decisions": decisions,
            "pending_need_id": need_id.root,
            "pending_tool": tool_name,
            "pending_action_ids": [],
            "stopped": False,
            "decision_model_calls": budget.decision_model_calls_used,
        }

    def _trusted_call_limit(self, request: MemoryResolutionRequest) -> int:
        round_limit = min(
            request.retrieval_budget.max_rounds,
            self._tool_policy.max_rounds,
        ) * max(1, len(request.initial_memory_needs))
        return min(
            request.retrieval_budget.max_tool_calls,
            self._tool_policy.max_tool_calls,
            round_limit,
        )

    @staticmethod
    def _route_after_decision(state: ControllerGraphState) -> Literal["execute", "finish"]:
        return "finish" if state.get("stopped", False) else "execute"

    def _execute_tool(self, state: ControllerGraphState) -> ControllerGraphState:
        request = self._request_from_state(state)
        calls = state.get("tool_calls", [])
        call_index = len(calls) + 1
        controller_budget = self._budgets[request.request_id.root]
        if controller_budget.wall_clock_exhausted() or not controller_budget.can_invoke_tool():
            controller_budget.mark_terminal(ControllerStopReason.BUDGET_EXHAUSTED.value)
            failed = ToolResult(
                tool_call_id=StableId(f"tool-call.{request.request_id.root}.{call_index}"),
                status=ToolResultStatus.FAILED,
                basis_commit=request.base_commit,
                snapshot_id=request.snapshot_id,
                failure_code=ToolFailureCode.BUDGET_EXCEEDED,
                audit_ref=StableId(f"tool-call.{request.request_id.root}.{call_index}"),
            )
            return {
                "tool_calls": [
                    *calls,
                    {
                        "need_id": state["pending_need_id"],
                        "tool_name": state["pending_tool"],
                        "result": failed.model_dump(mode="json"),
                    },
                ],
                "terminal_failure": controller_budget.terminal_failure,
            }
        remaining_ms = max(1, int(controller_budget.remaining_wall_clock_ms()))
        context = ToolCallContext(
            tool_call_id=StableId(f"tool-call.{request.request_id.root}.{call_index}"),
            run_id=request.run_id,
            task_id=request.task_id,
            agent_type=AgentType.MEMORY_CONTROLLER,
            agent_mode=AgentMode.BOUNDED_R2,
            project_id=request.project_id,
            base_commit=request.base_commit,
            snapshot_id=request.snapshot_id,
            worldline=request.worldline,
            narrative_chapter=request.narrative_chapter,
            access_scope=request.access_scope,
            plan_permission=request.allow_future_plan,
            timeout_ms=min(self._tool_policy.wall_clock_budget_ms, remaining_ms, 30_000),
        )
        budget = controller_budget.sync_tool_budget()

        async def invoke() -> ToolResult:
            return await self._binding.invoke(
                ToolInvocation(
                    state["pending_tool"],
                    {
                        "need_id": state["pending_need_id"],
                        "limit": request.retrieval_budget.max_candidates,
                    },
                ),
                context,
                budget,
            )

        result = asyncio.run(invoke())
        gain = self._new_information_gain(calls, result)
        result = result.model_copy(update={"new_information_gain": gain})
        backend_executed = result.status is ToolResultStatus.SUCCEEDED
        controller_budget.note_tool_result(result, backend_executed=backend_executed)
        return {
            "tool_calls": [
                *calls,
                {
                    "need_id": state["pending_need_id"],
                    "tool_name": state["pending_tool"],
                    "result": result.model_dump(mode="json"),
                },
            ],
            "terminal_failure": controller_budget.terminal_failure,
        }

    @staticmethod
    def _new_information_gain(
        prior_calls: list[dict[str, Any]],
        result: ToolResult,
    ) -> int:
        if result.status is not ToolResultStatus.SUCCEEDED:
            return 0
        prior_ids = {
            unit_id
            for call in prior_calls
            for unit_id in BoundedMemoryController._result_unit_ids(
                ToolResult.model_validate_json(json.dumps(call["result"]))
            )
        }
        return len(set(BoundedMemoryController._result_unit_ids(result)) - prior_ids)

    @staticmethod
    def _result_unit_ids(result: ToolResult) -> tuple[StableId, ...]:
        if result.status is not ToolResultStatus.SUCCEEDED or not isinstance(result.payload, dict):
            return ()
        raw_hits = result.payload.get("hits")
        if not isinstance(raw_hits, list):
            return ()
        ids: list[StableId] = []
        for raw in raw_hits:
            if not isinstance(raw, dict):
                continue
            try:
                ids.append(ChannelHit.model_validate(raw).unit.unit_id)
            except (TypeError, ValueError):
                continue
        return tuple(ids)

    def _finalize(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        state: ControllerGraphState,
    ) -> BoundedControllerRun:
        calls = self._parse_calls(state.get("tool_calls", []))
        stop_reason = ControllerStopReason(state["stop_reason"])
        traces = self._build_traces(
            request.initial_memory_needs,
            calls,
            candidate_limit=request.retrieval_budget.max_candidates,
        )
        unresolved = tuple(
            need.query_text
            for need, trace in zip(request.initial_memory_needs, traces, strict=True)
            if need.requirement is RequirementLevel.MANDATORY and not trace.candidates
        )
        if stop_reason is ControllerStopReason.SUFFICIENT and unresolved:
            stop_reason = ControllerStopReason.MANDATORY_GAP_UNRESOLVED
        selected = tuple(
            candidate.unit
            for trace in traces
            for candidate in trace.candidates
            if candidate.selected
        )
        selected = tuple({unit.unit_id: unit for unit in selected}.values())
        decisions = tuple(
            ControllerPolicyDecision.model_validate(item, strict=False).model_copy(
                update={
                    "model_call_id": (
                        StableId(item["model_call_id"])
                        if item.get("model_call_id") is not None
                        else None
                    )
                }
            )
            for item in state.get("policy_decisions", [])
        )
        receipt = self._receipt(request, calls, decisions)
        ready = stop_reason is ControllerStopReason.SUFFICIENT
        assembly = ContextAssemblySpec(
            selected_unit_ids=tuple(unit.unit_id for unit in selected),
            mandatory_unit_ids=tuple(unit.unit_id for unit in selected if unit.mandatory),
            token_budget=request.context_budget.token_budget,
        )
        context = self._compiler.compile(
            tuple(zip(request.initial_memory_needs, traces, strict=True)),
            text_root,
            context_id=StableId(f"context.{request.request_id.root}"),
            base_commit=request.base_commit,
            snapshot_id=request.snapshot_id,
            task_contract=request.task_contract,
            token_budget=request.context_budget.token_budget,
        )
        if context.budget_report.mandatory_tokens > request.context_budget.token_budget:
            ready = False
            stop_reason = ControllerStopReason.BUDGET_EXHAUSTED
            unresolved = (*unresolved, "mandatory context exceeds token budget")
        evidence_strength_satisfied = all(
            unit.evidence_refs
            or unit.source_artifact is not None
            or unit.truth_class is not None
            or unit.information_label == "plan"
            or bool(unit.text.strip())
            for unit in selected
        )
        if ready and not evidence_strength_satisfied:
            ready = False
            stop_reason = ControllerStopReason.NO_ADDITIONAL_EVIDENCE
            unresolved = (*unresolved, "selected candidates lack qualifying evidence")
        sufficiency = self._sufficiency_report(
            request,
            traces,
            selected,
            calls,
            unresolved,
            evidence_strength_satisfied=evidence_strength_satisfied,
            stop_reason=stop_reason,
        )
        resolution = ContextResolutionResult(
            resolution_id=StableId(f"resolution.{request.request_id.root}"),
            request_id=request.request_id,
            status=ResolutionStatus.READY if ready else ResolutionStatus.PARTIAL,
            base_commit=request.base_commit,
            snapshot_id=request.snapshot_id,
            normalized_needs=request.initial_memory_needs,
            memory_selection=tuple(
                MemorySelection(
                    unit_id=unit.unit_id,
                    need_ids=tuple(
                        trace.need_id
                        for trace in traces
                        if any(
                            candidate.unit.unit_id == unit.unit_id for candidate in trace.candidates
                        )
                    ),
                    candidate_pool=self._pool_for_unit(unit.unit_kind.value),
                    decision=SelectionDecision.SELECTED,
                    reason="selected by bounded route policy",
                    mandatory=unit.mandatory,
                )
                for unit in selected
            ),
            evidence_ledger=tuple(
                EvidenceLedgerEntry(
                    unit_id=unit.unit_id,
                    evidence_refs=unit.evidence_refs,
                    basis_commit=request.base_commit,
                    snapshot_id=request.snapshot_id,
                    access_scope=request.access_scope,
                )
                for unit in selected
            ),
            unresolved_gaps=unresolved,
            context_assembly_spec=assembly if ready else None,
            sufficiency_report=sufficiency,
            stop_reason=stop_reason,
            receipt=receipt,
        )
        decision_receipts: list[AgentExecutionReceipt] = []
        for policy_decision in decisions:
            if policy_decision.model_call_id is None:
                continue
            decision_receipt = self._policy.decision_receipt(policy_decision.model_call_id)
            if decision_receipt is not None:
                decision_receipts.append(decision_receipt)
        return {
            "resolution": resolution,
            "context": context,
            "tool_results": tuple(result for _, _, result in calls),
            "decision_receipts": tuple(decision_receipts),
        }

    def _sufficiency_report(
        self,
        request: MemoryResolutionRequest,
        traces: tuple[RetrievalTrace, ...],
        selected: tuple[RetrievalUnit, ...],
        calls: tuple[tuple[StableId, str, ToolResult], ...],
        unresolved: tuple[str, ...],
        *,
        evidence_strength_satisfied: bool,
        stop_reason: ControllerStopReason,
    ) -> SufficiencyReport:
        selected_entities = {entity for unit in selected for entity in unit.entity_ids}
        requested_entities = {
            entity for need in request.initial_memory_needs for entity in need.entity_ids
        }
        temporal_needs = tuple(
            need for need in request.initial_memory_needs if need.time_scope is not None
        )
        plan_needs = tuple(
            need for need in request.initial_memory_needs if need.query_intent in PLAN_INTENTS
        )
        resolved = {
            trace.need_id for trace in traces if any(item.selected for item in trace.candidates)
        }
        recommended_fallback = next(
            (
                fallback.condition
                for need in request.initial_memory_needs
                if need.need_id not in resolved
                for plan in (self._route_plans.get(need.need_id),)
                if plan is not None
                for fallback in plan.conditional_fallbacks
            ),
            None,
        )
        truth_groups: dict[tuple[tuple[StableId, ...], str | None], list[RetrievalUnit]] = {}
        for unit in selected:
            key = (tuple(sorted(unit.entity_ids, key=lambda item: item.root)), unit.predicate)
            truth_groups.setdefault(key, []).append(unit)
        conflicting_evidence = tuple(
            sorted(
                {
                    unit.unit_id
                    for units in truth_groups.values()
                    if len({unit.truth_class for unit in units if unit.truth_class is not None}) > 1
                    for unit in units
                },
                key=lambda item: item.root,
            )
        )
        return SufficiencyReport(
            mandatory_gaps_closed=not unresolved,
            evidence_strength_satisfied=evidence_strength_satisfied,
            entity_coverage=(
                len(requested_entities & selected_entities) / len(requested_entities)
                if requested_entities
                else 1.0
            ),
            temporal_coverage=(
                sum(need.need_id in resolved for need in temporal_needs) / len(temporal_needs)
                if temporal_needs
                else 1.0
            ),
            plan_obligation_coverage=(
                sum(need.need_id in resolved for need in plan_needs) / len(plan_needs)
                if plan_needs
                else 1.0
            ),
            conflicting_evidence=conflicting_evidence,
            unresolved_unknowns=unresolved,
            scope_access_warnings=(
                ("runtime access policy blocked retrieval",)
                if stop_reason is ControllerStopReason.ACCESS_BLOCKED
                else ()
            ),
            freshness_warnings=(
                ("snapshot freshness gate blocked retrieval",)
                if stop_reason is ControllerStopReason.FRESHNESS_BLOCKED
                else ()
            ),
            new_information_gain_by_round=tuple(
                result.new_information_gain for _, _, result in calls
            ),
            recommended_fallback=recommended_fallback,
            stop_reason=stop_reason,
        )

    def _finalize_without_tools(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        reason: ControllerStopReason,
    ) -> BoundedControllerRun:
        state = ControllerGraphState(
            tool_calls=[],
            policy_decisions=[],
            stopped=True,
            stop_reason=reason.value,
        )
        return self._finalize(request, text_root, state)

    @staticmethod
    def _parse_calls(
        calls: list[dict[str, Any]],
    ) -> tuple[tuple[StableId, str, ToolResult], ...]:
        return tuple(
            (
                StableId(item["need_id"]),
                str(item["tool_name"]),
                ToolResult.model_validate_json(json.dumps(item["result"])),
            )
            for item in calls
        )

    def _build_traces(
        self,
        needs: tuple[Stage1MemoryNeed, ...],
        calls: tuple[tuple[StableId, str, ToolResult], ...],
        *,
        candidate_limit: int = 20,
    ) -> tuple[RetrievalTrace, ...]:
        traces: list[RetrievalTrace] = []
        for need in needs:
            need_calls = tuple(item for item in calls if item[0] == need.need_id)
            hits: list[ChannelHit] = []
            channels: list[RetrievalChannel] = []
            for _, tool_name, result in need_calls:
                channel = CHANNEL_BY_TOOL[tool_name]
                channels.append(channel)
                if result.status is not ToolResultStatus.SUCCEEDED or not isinstance(
                    result.payload, dict
                ):
                    continue
                payload_hits = result.payload.get("hits")
                if isinstance(payload_hits, list):
                    hits.extend(
                        ChannelHit.model_validate_json(json.dumps(hit)) for hit in payload_hits
                    )
            channel_results = {
                channel: tuple(hit for hit in hits if hit.channel is channel)
                for channel in dict.fromkeys(channels)
            }
            candidates, fusion_applied, rerank_applied, rerank_failure = self._route_candidates(
                need,
                self._route_plans.get(need.need_id),
                channel_results,
                candidate_limit=candidate_limit,
            )
            traces.append(
                RetrievalTrace(
                    need_id=need.need_id,
                    intent=need.query_intent,
                    allowed_channels=tuple(channels),
                    channel_candidate_counts={
                        channel: sum(hit.channel is channel for hit in hits) for channel in channels
                    },
                    candidates=candidates,
                    fusion_applied=fusion_applied,
                    rerank_applied=rerank_applied,
                    channel_failures=(
                        {RetrievalChannel.RERANK: rerank_failure}
                        if rerank_failure is not None
                        else {}
                    ),
                    stop_reason=(
                        RetrievalStopReason.EXACT_SATISFIED
                        if candidates
                        else RetrievalStopReason.CANDIDATES_EXHAUSTED
                    ),
                    need_execution_status=(
                        NeedExecutionStatus.EXECUTED_WITH_CANDIDATES
                        if candidates
                        else NeedExecutionStatus.EXECUTED_EMPTY
                        if need_calls
                        else NeedExecutionStatus.NOT_EXECUTED_BUDGET_EXHAUSTED
                    ),
                    calls_allocated=len(need_calls),
                    required_need_facet_ids=(
                        need.completion_spec.required_need_facet_ids
                        if need.completion_spec is not None
                        else ()
                    ),
                    irreducible_need_facet_ids=(
                        need.completion_spec.irreducible_need_facet_ids
                        if need.completion_spec is not None
                        else ()
                    ),
                )
            )
        return tuple(traces)

    def _route_candidates(
        self,
        need: Stage1MemoryNeed,
        plan: RoutePlan | None,
        channel_results: dict[RetrievalChannel, tuple[ChannelHit, ...]],
        *,
        candidate_limit: int,
    ) -> tuple[tuple[FusedCandidate, ...], bool, bool, str | None]:
        """Fuse only the groups declared by the route; no cross-pool RRF."""

        if plan is None:
            direct_candidates = self._direct_candidates(channel_results, candidate_limit)
            return direct_candidates, False, False, None
        consumed: set[RetrievalChannel] = set()
        stages: list[tuple[dict[RetrievalChannel, tuple[ChannelHit, ...]], bool]] = []
        mandatory = {
            step.channel: channel_results[step.channel]
            for step in plan.mandatory_steps
            if step.channel in channel_results
        }
        if mandatory:
            stages.append((mandatory, False))
            consumed.update(mandatory)
        for group in plan.primary_groups:
            results = {
                step.channel: channel_results[step.channel]
                for step in group.steps
                if step.channel in channel_results
            }
            if results:
                stages.append((results, group.fusion_profile is not None))
                consumed.update(results)
        for fallback in plan.conditional_fallbacks:
            results = {
                step.channel: channel_results[step.channel]
                for step in fallback.steps
                if step.channel in channel_results
            }
            if results:
                stages.append((results, fallback.fusion_profile is not None))
                consumed.update(results)
        remaining = {
            channel: hits for channel, hits in channel_results.items() if channel not in consumed
        }
        if remaining:
            stages.append((remaining, False))
        candidates: tuple[FusedCandidate, ...] = ()
        fusion_applied = False
        for results, should_fuse in stages:
            stage_candidates = (
                FusionService().fuse(results, limit=candidate_limit)
                if should_fuse and len(results) > 1
                else self._direct_candidates(results, candidate_limit)
            )
            fusion_applied = fusion_applied or (should_fuse and len(results) > 1)
            candidates = self._merge_candidates(
                candidates,
                stage_candidates,
                candidate_limit,
            )
        rerank_applied = False
        rerank_failure: str | None = None
        if fusion_applied and self._reranker is not None:
            try:
                candidates = self._reranker.rerank(need, candidates, limit=candidate_limit)
                rerank_applied = True
            except Exception as error:
                rerank_failure = f"reranker_degraded:{type(error).__name__}"
        return candidates, fusion_applied, rerank_applied, rerank_failure

    @staticmethod
    def _direct_candidates(
        channel_results: dict[RetrievalChannel, tuple[ChannelHit, ...]],
        candidate_limit: int,
    ) -> tuple[FusedCandidate, ...]:
        unique: dict[StableId, ChannelHit] = {}
        for hits in channel_results.values():
            for hit in hits:
                unique.setdefault(hit.unit.unit_id, hit)
        return tuple(
            FusedCandidate(
                unit=hit.unit,
                fused_rank=rank,
                rrf_score=1.0 / rank,
                channel_hits=(hit,),
                selected=rank <= candidate_limit or hit.unit.mandatory,
                rejection_reason=(
                    None if rank <= candidate_limit or hit.unit.mandatory else "route_result_limit"
                ),
            )
            for rank, hit in enumerate(unique.values(), start=1)
        )

    @staticmethod
    def _merge_candidates(
        current: tuple[FusedCandidate, ...],
        incoming: tuple[FusedCandidate, ...],
        candidate_limit: int,
    ) -> tuple[FusedCandidate, ...]:
        unique: dict[StableId, FusedCandidate] = {}
        for candidate in (*current, *incoming):
            unique.setdefault(candidate.unit.unit_id, candidate)
        return tuple(
            candidate.model_copy(
                update={
                    "fused_rank": rank,
                    "selected": rank <= candidate_limit or candidate.unit.mandatory,
                    "rejection_reason": (
                        None
                        if rank <= candidate_limit or candidate.unit.mandatory
                        else "route_result_limit"
                    ),
                }
            )
            for rank, candidate in enumerate(unique.values(), start=1)
        )

    def _receipt(
        self,
        request: MemoryResolutionRequest,
        calls: tuple[tuple[StableId, str, ToolResult], ...],
        decisions: tuple[ControllerPolicyDecision, ...],
    ) -> AgentExecutionReceipt:
        now = datetime.now(UTC)
        configuration = content_hash(
            canonical_json_bytes(
                {
                    "retrieval_budget": request.retrieval_budget.model_dump(mode="json"),
                    "context_budget": request.context_budget.model_dump(mode="json"),
                    "need_completion_contracts": [
                        {
                            "need_id": need.need_id.root,
                            "facets": [facet.model_dump(mode="json") for facet in need.need_facets],
                            "completion_spec": (
                                None
                                if need.completion_spec is None
                                else need.completion_spec.model_dump(mode="json")
                            ),
                        }
                        for need in request.initial_memory_needs
                    ],
                    "tool_policy": self._tool_policy.model_dump(mode="json"),
                    "controller_policy": self._policy_contract_ref.model_dump(mode="json"),
                }
            )
        )
        return AgentExecutionReceipt(
            receipt_id=StableId(f"controller-receipt.{request.request_id.root}"),
            run_id=request.run_id,
            task_id=request.task_id,
            agent_spec=self._policy_contract_ref,
            agent_type=AgentType.MEMORY_CONTROLLER,
            agent_mode=AgentMode.BOUNDED_R2,
            prompt_fingerprint=self._policy_prompt_fingerprint,
            configuration_fingerprint=configuration,
            model_call_ids=tuple(
                decision.model_call_id
                for decision in decisions
                if decision.model_call_id is not None
            ),
            tool_call_ids=tuple(result.tool_call_id for _, _, result in calls),
            status=ExecutionStatus.SUCCEEDED,
            started_at=now,
            completed_at=now,
            latency_ms=0,
        )

    @staticmethod
    def _pool_for_unit(unit_kind: str) -> CandidatePool:
        if unit_kind.startswith("grounded"):
            return CandidatePool.GROUNDED
        if unit_kind.endswith("anchor"):
            return CandidatePool.ANCHOR
        return CandidatePool.R1

    @staticmethod
    def _request_from_state(state: ControllerGraphState) -> MemoryResolutionRequest:
        return MemoryResolutionRequest.model_validate_json(json.dumps(state["request"]))
