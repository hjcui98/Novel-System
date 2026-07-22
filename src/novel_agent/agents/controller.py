"""Structured model-backed policy adapter for the bounded Memory Controller graph."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from novel_agent.agents.runner import AgentRunResult, StructuredAgentRunner
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentSpec,
    AgentType,
    ContractRef,
    ControllerPolicyDecision,
)
from novel_agent.runtime.memory_controller import ControllerStateView
from novel_agent.services.content_addressing import canonical_json_bytes

ControllerRequestFactory = Callable[[ControllerStateView, int], ModelRequest]


class StructuredControllerPolicy:
    """Use a registered AgentSpec for decisions while the graph retains all authority."""

    def __init__(
        self,
        runner: StructuredAgentRunner,
        spec: AgentSpec,
        request_factory: ControllerRequestFactory,
    ) -> None:
        if spec.agent_type is not AgentType.MEMORY_CONTROLLER:
            raise ValueError("structured Controller policy requires a Memory Controller AgentSpec")
        if spec.mode is not AgentMode.BOUNDED_R2:
            raise ValueError("structured Controller policy requires BOUNDED_R2 mode")
        self._runner = runner
        self._spec = spec
        self._request_factory = request_factory
        self._receipts: dict[StableId, AgentExecutionReceipt] = {}

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
    def decision_receipts(self) -> tuple[AgentExecutionReceipt, ...]:
        return tuple(self._receipts.values())

    def decision_receipt(self, model_call_id: StableId) -> AgentExecutionReceipt | None:
        return self._receipts.get(model_call_id)

    def decide(self, state: ControllerStateView) -> ControllerPolicyDecision:
        round_index = len(state["tool_calls"]) + 1
        request = self._request_factory(state, round_index)
        payload = canonical_json_bytes(
            {
                "resolution_request": state["request"].model_dump(mode="json"),
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
        ).decode("utf-8")

        async def execute() -> AgentRunResult[ControllerPolicyDecision]:
            return await self._runner.run(
                AgentType.MEMORY_CONTROLLER,
                AgentMode.BOUNDED_R2,
                self._spec.version.root,
                request,
                payload,
                ControllerPolicyDecision,
                base_commit=state["request"].base_commit,
            )

        result = asyncio.run(execute())
        self._receipts[result.model_call.request_id] = result.receipt
        return result.output.model_copy(update={"model_call_id": result.model_call.request_id})
