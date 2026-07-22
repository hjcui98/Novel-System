"""Checkpointable bounded Stage 2 Memory Controller over typed read-only tools."""

from __future__ import annotations

import asyncio
import json
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
    RequirementLevel,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalTrace,
    Stage1ContextPackage,
    Stage1MemoryNeed,
)
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
    ToolCallContext,
    ToolPolicy,
    ToolResult,
    ToolResultStatus,
)
from novel_agent.prompts.registry import content_hash
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.memory_pipeline import ContextCompiler
from novel_agent.services.retrieval import ROUTES
from novel_agent.tools.contracts import ToolBinding, ToolBudget, ToolInvocation
from novel_agent.tools.retrieval import CHANNEL_BY_TOOL, POOL_BY_CHANNEL

TOOL_BY_CHANNEL = {channel: name for name, channel in CHANNEL_BY_TOOL.items()}


class ControllerGraphState(TypedDict, total=False):
    request: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    policy_decisions: list[dict[str, Any]]
    pending_tool: str
    pending_need_id: str
    stopped: bool
    stop_reason: str


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
        successful = {
            need_id
            for need_id, _, result in calls
            if result.status is ToolResultStatus.SUCCEEDED and result.coverage > 0
        }
        called = {(need_id, tool_name) for need_id, tool_name, _ in calls}
        mandatory_missing = tuple(
            need
            for need in request.initial_memory_needs
            if need.requirement is RequirementLevel.MANDATORY and need.need_id not in successful
        )
        optional_missing = tuple(
            need for need in request.initial_memory_needs if need.need_id not in successful
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
            route = ROUTES[need.query_intent]
            channels = (*route.channels, *route.fallback_channels)
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
        self._freshness_check = freshness_check
        self._budgets: dict[str, ToolBudget] = {}
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
        self._budgets[request.request_id.root] = ToolBudget.from_policy(self._tool_policy)
        initial = ControllerGraphState(
            request=request.model_dump(mode="json"),
            tool_calls=[],
            policy_decisions=[],
            stopped=False,
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
            }
        need_id = cast(StableId, decision.need_id)
        tool_name = cast(str, decision.tool_name)
        known_need_ids = {need.need_id for need in request.initial_memory_needs}
        if need_id not in known_need_ids:
            return {
                "policy_decisions": decisions,
                "stopped": True,
                "stop_reason": ControllerStopReason.ACCESS_BLOCKED.value,
            }
        if tool_name not in self._tool_policy.allowed_tools or tool_name not in CHANNEL_BY_TOOL:
            return {
                "policy_decisions": decisions,
                "stopped": True,
                "stop_reason": ControllerStopReason.ACCESS_BLOCKED.value,
            }
        if any(
            called_need_id == need_id and called_tool == tool_name
            for called_need_id, called_tool, _ in calls
        ):
            return {
                "policy_decisions": decisions,
                "stopped": True,
                "stop_reason": ControllerStopReason.NO_ADDITIONAL_EVIDENCE.value,
            }
        return {
            "policy_decisions": decisions,
            "pending_need_id": need_id.root,
            "pending_tool": tool_name,
            "stopped": False,
        }

    @staticmethod
    def _route_after_decision(state: ControllerGraphState) -> Literal["execute", "finish"]:
        return "finish" if state.get("stopped", False) else "execute"

    def _execute_tool(self, state: ControllerGraphState) -> ControllerGraphState:
        request = self._request_from_state(state)
        calls = state.get("tool_calls", [])
        call_index = len(calls) + 1
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
            timeout_ms=min(self._tool_policy.wall_clock_budget_ms, 30_000),
        )
        budget = self._budgets[request.request_id.root]

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
        return {
            "tool_calls": [
                *calls,
                {
                    "need_id": state["pending_need_id"],
                    "tool_name": state["pending_tool"],
                    "result": result.model_dump(mode="json"),
                },
            ]
        }

    def _finalize(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        state: ControllerGraphState,
    ) -> BoundedControllerRun:
        calls = self._parse_calls(state.get("tool_calls", []))
        stop_reason = ControllerStopReason(state["stop_reason"])
        traces = self._build_traces(request.initial_memory_needs, calls)
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
            ControllerPolicyDecision.model_validate(item, strict=False)
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
            unique = {hit.unit.unit_id: hit for hit in hits}
            candidates = tuple(
                FusedCandidate(
                    unit=hit.unit,
                    fused_rank=rank,
                    rrf_score=1.0 / rank,
                    channel_hits=(hit,),
                )
                for rank, hit in enumerate(unique.values(), start=1)
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
                    fusion_applied=False,
                    stop_reason=(
                        RetrievalStopReason.EXACT_SATISFIED
                        if candidates
                        else RetrievalStopReason.CANDIDATES_EXHAUSTED
                    ),
                )
            )
        return tuple(traces)

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
