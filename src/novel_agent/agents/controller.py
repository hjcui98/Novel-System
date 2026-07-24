"""Structured model-backed policy adapter for the bounded Memory Controller graph."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from novel_agent.agents.runner import AgentRunResult, StructuredAgentRunner
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.retrieval_routing import RoutePlan
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentSpec,
    AgentType,
    ContractRef,
    ControllerPolicyAction,
    ControllerPolicyDecision,
    ControllerPolicyDraft,
    ControllerStopReason,
    RegisteredControllerAction,
    ToolPolicy,
)
from novel_agent.runtime.memory_controller import ControllerStateView
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.controller_legal_actions import LegalActionProvider

ControllerRequestFactory = Callable[[ControllerStateView, int], ModelRequest]


@dataclass(frozen=True, slots=True)
class ControllerDecisionRepair:
    """Auditable record of a bounded model-decision repair."""

    request_id: StableId
    reason: str
    selected_need_id: StableId | None
    selected_tool_name: str | None


class StructuredControllerPolicy:
    """Use a registered AgentSpec for decisions while the graph retains all authority."""

    def __init__(
        self,
        runner: StructuredAgentRunner,
        spec: AgentSpec,
        request_factory: ControllerRequestFactory,
        *,
        route_plans: tuple[RoutePlan, ...] = (),
        legal_actions: LegalActionProvider | None = None,
        max_decision_model_calls: int = 2,
        max_agentic_actions: int = 8,
        compact_prompt: bool = True,
    ) -> None:
        if spec.agent_type is not AgentType.MEMORY_CONTROLLER:
            raise ValueError("structured Controller policy requires a Memory Controller AgentSpec")
        if spec.mode is not AgentMode.BOUNDED_R2:
            raise ValueError("structured Controller policy requires BOUNDED_R2 mode")
        self._runner = runner
        self._spec = spec
        self._request_factory = request_factory
        self._receipts: dict[StableId, AgentExecutionReceipt] = {}
        self._repairs: list[ControllerDecisionRepair] = []
        self._legal_actions = legal_actions or LegalActionProvider(
            tool_policy=spec.tool_policy,
            route_plans=route_plans,
        )
        self.max_decision_model_calls = max_decision_model_calls
        self._max_agentic_actions = max_agentic_actions
        self._compact_prompt = compact_prompt

    @property
    def contract_ref(self) -> ContractRef:
        return ContractRef(
            contract_id=self._spec.agent_id,
            version=self._spec.version,
            content_hash=self._spec.content_hash,
        )

    @property
    def prompt_fingerprint(self) -> ArtifactId:
        return self._spec.system_prompt.render_fingerprint

    @property
    def tool_policy_hash(self) -> ArtifactId | None:
        return self._spec.tool_policy.content_hash

    @property
    def tool_policy(self) -> ToolPolicy:
        return self._spec.tool_policy

    @property
    def decision_receipts(self) -> tuple[AgentExecutionReceipt, ...]:
        return tuple(self._receipts.values())

    def decision_receipt(self, model_call_id: StableId) -> AgentExecutionReceipt | None:
        return self._receipts.get(model_call_id)

    @property
    def decision_repairs(self) -> tuple[ControllerDecisionRepair, ...]:
        return tuple(self._repairs)

    def decide(self, state: ControllerStateView) -> ControllerPolicyDecision:
        round_index = len(state["tool_calls"]) + 1
        request = self._request_factory(state, round_index)
        available_actions = self._available_actions(state)
        registered = self._legal_actions.available_actions(
            state["request"],
            state["tool_calls"],
        )
        action_by_id = {item.action_id.root: item for item in registered}
        payload_obj: dict[str, object]
        if self._compact_prompt:
            payload_obj = {
                "task_contract": {
                    "request_id": state["request"].request_id.root,
                    "base_commit": state["request"].base_commit.root,
                    "snapshot_id": state["request"].snapshot_id.root,
                    "narrative_chapter": state["request"].narrative_chapter,
                },
                "need_summaries": [
                    {
                        "id": need.need_id.root,
                        "intent": need.query_intent.value,
                        "requirement": need.requirement.value,
                        "resolved": any(
                            need_id == need.need_id
                            and result.status.value == "succeeded"
                            and result.coverage > 0
                            for need_id, _, result in state["tool_calls"]
                        ),
                    }
                    for need in state["request"].initial_memory_needs
                ],
                "available_actions": available_actions,
                "registered_action_ids": [item.action_id.root for item in registered],
                "prior_action_outcomes": [
                    {
                        "need_id": need_id.root,
                        "tool_name": tool_name,
                        "success": result.status.value == "succeeded",
                        "candidate_count": result.channel_candidate_count or 0,
                        "gain": result.new_information_gain,
                        "failure_code": (
                            result.failure_code.value if result.failure_code is not None else None
                        ),
                    }
                    for need_id, tool_name, result in state["tool_calls"]
                ],
                "round_index": round_index,
                "max_agentic_actions": self._max_agentic_actions,
            }
        else:
            payload_obj = {
                "resolution_request": state["request"].model_dump(mode="json"),
                "available_actions": available_actions,
                "prior_tool_results": [
                    {
                        "need_id": need_id.root,
                        "tool_name": tool_name,
                        "result": result.model_dump(mode="json"),
                    }
                    for need_id, tool_name, result in state["tool_calls"]
                ],
                "round_index": round_index,
            }
        payload = canonical_json_bytes(payload_obj).decode("utf-8")

        async def execute() -> AgentRunResult[ControllerPolicyDraft]:
            return await self._runner.run(
                AgentType.MEMORY_CONTROLLER,
                AgentMode.BOUNDED_R2,
                self._spec.version.root,
                request,
                payload,
                ControllerPolicyDraft,
                base_commit=state["request"].base_commit,
            )

        try:
            result = asyncio.run(execute())
        except ValidationError:
            # Non-JSON or a type-level schema failure has already exhausted the
            # gateway's audited retries.  Keep the bounded graph live using
            # only a sealed legal action; never construct a Need/tool name.
            decision = self._first_legal_decision(
                available_actions,
                rationale_code="SCHEMA_RETRY_EXHAUSTED",
            )
            self._record_repair(request.request_id, "SCHEMA_RETRY_EXHAUSTED", decision)
            return decision
        self._receipts[result.model_call.request_id] = result.receipt
        decision, repair_reason = self._bind_draft(
            result.output,
            available_actions,
            action_by_id=action_by_id,
        )
        decision = decision.model_copy(update={"model_call_id": result.model_call.request_id})
        if repair_reason is not None:
            self._record_repair(result.model_call.request_id, repair_reason, decision)
        return decision

    @classmethod
    def _bind_draft(
        cls,
        draft: ControllerPolicyDraft,
        available_actions: list[dict[str, object]],
        *,
        action_by_id: Mapping[str, RegisteredControllerAction] | None = None,
    ) -> tuple[ControllerPolicyDecision, str | None]:
        legal_pairs = cls._legal_pairs(available_actions)
        registry = action_by_id or {}

        # Batch plan: only opaque registered action IDs.
        if draft.selected_action_ids:
            pending: list[StableId] = []
            for raw_id in draft.selected_action_ids:
                item = registry.get(raw_id)
                if item is None:
                    continue
                pending.append(item.action_id)
                if len(pending) >= 8:
                    break
            if pending:
                return (
                    ControllerPolicyDecision(
                        action=ControllerPolicyAction.EXECUTE_PLAN,
                        selected_action_ids=tuple(pending),
                        pending_action_ids=tuple(pending),
                        rationale_code=cls._safe_rationale(
                            draft.rationale_code,
                            "MODEL_BATCH_PLAN",
                        ),
                    ),
                    None,
                )

        if draft.action == ControllerPolicyAction.STOP.value:
            if draft.stop_reason is None:
                stop_reason = ControllerStopReason.MANDATORY_GAP_UNRESOLVED
            else:
                try:
                    stop_reason = ControllerStopReason(draft.stop_reason)
                except ValueError:
                    stop_reason = ControllerStopReason.MANDATORY_GAP_UNRESOLVED
            rationale = cls._safe_rationale(draft.rationale_code, "MODEL_STOP")
            return (
                ControllerPolicyDecision(
                    action=ControllerPolicyAction.STOP,
                    stop_reason=stop_reason,
                    rationale_code=rationale,
                ),
                None,
            )

        if draft.action == ControllerPolicyAction.CALL_TOOL.value:
            exact = next(
                (
                    pair
                    for pair in legal_pairs
                    if pair[0].root == draft.need_id and pair[1] == draft.tool_name
                ),
                None,
            )
            if exact is not None:
                return (
                    ControllerPolicyDecision(
                        action=ControllerPolicyAction.CALL_TOOL,
                        need_id=exact[0],
                        tool_name=exact[1],
                        rationale_code=cls._safe_rationale(
                            draft.rationale_code,
                            "MODEL_LEGAL_ACTION",
                        ),
                    ),
                    None,
                )

            compatible = [
                pair
                for pair in legal_pairs
                if (draft.need_id is None or pair[0].root == draft.need_id)
                and (draft.tool_name is None or pair[1] == draft.tool_name)
            ]
            if len(compatible) == 1:
                decision = cls._call_decision(compatible[0], "BOUND_UNIQUE_LEGAL_ACTION")
                return decision, "INFERRED_UNIQUE_LEGAL_ACTION"

            decision = cls._first_legal_decision(
                available_actions,
                rationale_code="BOUND_FIRST_LEGAL_ACTION",
            )
            return decision, "BOUND_FIRST_LEGAL_ACTION"

        decision = cls._first_legal_decision(
            available_actions,
            rationale_code="BOUND_MISSING_ACTION",
        )
        return decision, "MISSING_OR_UNKNOWN_ACTION"

    @staticmethod
    def _safe_rationale(value: str | None, fallback: str) -> str:
        if value is not None and 1 <= len(value) <= 64:
            return value
        return fallback

    @staticmethod
    def _legal_pairs(
        available_actions: list[dict[str, object]],
    ) -> list[tuple[StableId, str]]:
        pairs: list[tuple[StableId, str]] = []
        for action in available_actions:
            need_id = action["need_id"]
            tool_names = action["tool_names"]
            if not isinstance(need_id, str) or not isinstance(tool_names, list):
                raise AssertionError("trusted available action has an invalid shape")
            for tool_name in tool_names:
                if not isinstance(tool_name, str):
                    raise AssertionError("trusted available tool name is not a string")
                pairs.append((StableId(need_id), tool_name))
        return pairs

    @classmethod
    def _first_legal_decision(
        cls,
        available_actions: list[dict[str, object]],
        *,
        rationale_code: str,
    ) -> ControllerPolicyDecision:
        legal_pairs = cls._legal_pairs(available_actions)
        if not legal_pairs:
            return ControllerPolicyDecision(
                action=ControllerPolicyAction.STOP,
                stop_reason=ControllerStopReason.MANDATORY_GAP_UNRESOLVED,
                rationale_code="NO_LEGAL_ACTION_AVAILABLE",
            )
        return cls._call_decision(legal_pairs[0], rationale_code)

    @staticmethod
    def _call_decision(
        pair: tuple[StableId, str],
        rationale_code: str,
    ) -> ControllerPolicyDecision:
        return ControllerPolicyDecision(
            action=ControllerPolicyAction.CALL_TOOL,
            need_id=pair[0],
            tool_name=pair[1],
            rationale_code=rationale_code,
        )

    def _record_repair(
        self,
        request_id: StableId,
        reason: str,
        decision: ControllerPolicyDecision,
    ) -> None:
        self._repairs.append(
            ControllerDecisionRepair(
                request_id=request_id,
                reason=reason,
                selected_need_id=decision.need_id,
                selected_tool_name=decision.tool_name,
            )
        )

    def _available_actions(self, state: ControllerStateView) -> list[dict[str, object]]:
        """Expose the exact structured decisions the model may legally choose.

        Uses LegalActionProvider so prompt actions match ToolAdapter scope.
        """

        return self._legal_actions.available_action_summaries(
            state["request"],
            state["tool_calls"],
        )
