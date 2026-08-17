"""Single legal-action registry shared by Controller prompt and ToolAdapter."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import RetrievalChannel, Stage1MemoryNeed
from novel_agent.domain.retrieval_routing import RoutePlan
from novel_agent.domain.stage2 import (
    ControllerActionPhase,
    MemoryResolutionRequest,
    RegisteredControllerAction,
    ToolPolicy,
    ToolResult,
    ToolResultStatus,
)
from novel_agent.tools.retrieval import CHANNEL_BY_TOOL, PLAN_INTENTS, POOL_BY_CHANNEL

TOOL_BY_CHANNEL = {channel: name for name, channel in CHANNEL_BY_TOOL.items()}


def _need_is_accessible(
    need: Stage1MemoryNeed,
    access_scope: str,
    allow_future_plan: bool,
) -> bool:
    """Mirror RetrievalToolAdapter access checks so legal == executable."""

    if need.access_scope != access_scope:
        return False
    return not (
        (need.query_intent in PLAN_INTENTS or need.retrieval_may_return_plan)
        and not allow_future_plan
    )


class LegalActionProvider:
    """RoutePlan-aware registry: model-visible == bindable == adapter-executable."""

    def __init__(
        self,
        *,
        tool_policy: ToolPolicy,
        route_plans: tuple[RoutePlan, ...] = (),
    ) -> None:
        plans = {plan.need_id: plan for plan in route_plans}
        if len(plans) != len(route_plans):
            raise ValueError("legal action provider route plans must have unique need ids")
        self._tool_policy = tool_policy
        self._route_plans = plans

    def available_actions(
        self,
        request: MemoryResolutionRequest,
        prior_calls: Sequence[tuple[StableId, str, ToolResult]],
        *,
        include_inactive_fallbacks: bool = False,
    ) -> tuple[RegisteredControllerAction, ...]:
        called = {(need_id, tool_name) for need_id, tool_name, _ in prior_calls}
        access_scope = request.access_scope.value
        allow_future_plan = request.allow_future_plan
        actions: list[RegisteredControllerAction] = []
        for need in request.initial_memory_needs:
            if not _need_is_accessible(need, access_scope, allow_future_plan):
                continue
            actions.extend(
                self._actions_for_need(
                    need,
                    prior_calls=tuple(item for item in prior_calls if item[0] == need.need_id),
                    called=called,
                    include_inactive_fallbacks=include_inactive_fallbacks,
                )
            )
        return tuple(actions)

    def available_action_summaries(
        self,
        request: MemoryResolutionRequest,
        prior_calls: Sequence[tuple[StableId, str, ToolResult]],
    ) -> list[dict[str, object]]:
        """Legacy prompt shape: need_id + tool_names grouped."""

        grouped: dict[str, dict[str, object]] = {}
        for action in self.available_actions(request, prior_calls):
            entry = grouped.setdefault(
                action.need_id.root,
                {
                    "need_id": action.need_id.root,
                    "query_intent": action.query_intent,
                    "requirement": action.requirement,
                    "tool_names": [],
                },
            )
            tools = entry["tool_names"]
            assert isinstance(tools, list)
            if action.tool_name not in tools:
                tools.append(action.tool_name)
        return list(grouped.values())

    def channels_by_need(
        self,
        needs: tuple[Stage1MemoryNeed, ...],
        *,
        active_only: bool = False,
        prior_calls: Sequence[tuple[StableId, str, ToolResult]] = (),
    ) -> dict[StableId, tuple[RetrievalChannel, ...]]:
        """Adapter allowlist: channels currently or potentially executable."""

        result: dict[StableId, tuple[RetrievalChannel, ...]] = {}
        for need in needs:
            if active_only:
                channels = tuple(
                    dict.fromkeys(
                        action.retrieval_channel
                        for action in self._actions_for_need(
                            need,
                            prior_calls=tuple(
                                item for item in prior_calls if item[0] == need.need_id
                            ),
                            called={(n, t) for n, t, _ in prior_calls},
                            include_inactive_fallbacks=False,
                        )
                    )
                )
            else:
                plan = self._route_plans.get(need.need_id)
                if plan is None:
                    channels = tuple(
                        channel
                        for tool_name, channel in CHANNEL_BY_TOOL.items()
                        if tool_name in self._tool_policy.allowed_tools
                        and POOL_BY_CHANNEL[channel] in need.allowed_candidate_pools
                    )
                else:
                    channels = tuple(
                        dict.fromkeys(
                            step.channel
                            for step in (
                                *plan.mandatory_steps,
                                *(step for group in plan.primary_groups for step in group.steps),
                                *(
                                    step
                                    for fallback in plan.conditional_fallbacks
                                    for step in fallback.steps
                                ),
                            )
                            if TOOL_BY_CHANNEL.get(step.channel) in self._tool_policy.allowed_tools
                            and POOL_BY_CHANNEL[step.channel] in need.allowed_candidate_pools
                        )
                    )
            if channels:
                result[need.need_id] = channels
        return result

    def assert_consistency(
        self,
        request: MemoryResolutionRequest,
        prior_calls: Sequence[tuple[StableId, str, ToolResult]] = (),
    ) -> None:
        actions = self.available_actions(request, prior_calls)
        adapter_channels = self.channels_by_need(
            request.initial_memory_needs,
            active_only=True,
            prior_calls=prior_calls,
        )
        for action in actions:
            allowed = adapter_channels.get(action.need_id, ())
            if action.retrieval_channel not in allowed:
                raise ValueError(
                    "legal action provider inconsistency: "
                    f"{action.action_id.root} not adapter-executable"
                )

    def _actions_for_need(
        self,
        need: Stage1MemoryNeed,
        *,
        prior_calls: Sequence[tuple[StableId, str, ToolResult]],
        called: set[tuple[StableId, str]],
        include_inactive_fallbacks: bool,
    ) -> list[RegisteredControllerAction]:
        plan = self._route_plans.get(need.need_id)
        if plan is None:
            return self._pool_actions(need, called)
        actions: list[RegisteredControllerAction] = []
        for step in plan.mandatory_steps:
            action = self._step_action(
                need, step.step_id, step.channel, ControllerActionPhase.MANDATORY, None, called
            )
            if action is not None:
                actions.append(action)
        for group in plan.primary_groups:
            for step in group.steps:
                action = self._step_action(
                    need, step.step_id, step.channel, ControllerActionPhase.PRIMARY, None, called
                )
                if action is not None:
                    actions.append(action)
        primary_tools = {
            TOOL_BY_CHANNEL[step.channel]
            for step in (
                *plan.mandatory_steps,
                *(step for group in plan.primary_groups for step in group.steps),
            )
            if step.channel in TOOL_BY_CHANNEL
        }
        primary_exhausted = primary_tools.issubset({tool_name for _, tool_name, _ in prior_calls})
        primary_succeeded = self._primary_succeeded(plan, prior_calls)
        for fallback in plan.conditional_fallbacks:
            applies = primary_exhausted and self._fallback_applies(
                fallback.condition, primary_succeeded
            )
            if not applies and not include_inactive_fallbacks:
                continue
            for step in fallback.steps:
                action = self._step_action(
                    need,
                    step.step_id,
                    step.channel,
                    ControllerActionPhase.FALLBACK,
                    fallback.condition,
                    called,
                )
                if action is not None:
                    actions.append(action)
        return actions

    def _pool_actions(
        self,
        need: Stage1MemoryNeed,
        called: set[tuple[StableId, str]],
    ) -> list[RegisteredControllerAction]:
        actions: list[RegisteredControllerAction] = []
        for tool_name in self._tool_policy.allowed_tools:
            channel = CHANNEL_BY_TOOL.get(tool_name)
            if channel is None:
                continue
            if POOL_BY_CHANNEL[channel] not in need.allowed_candidate_pools:
                continue
            if (need.need_id, tool_name) in called:
                continue
            actions.append(
                RegisteredControllerAction(
                    action_id=StableId(f"action.{need.need_id.root}.{tool_name}"),
                    need_id=need.need_id,
                    route_step_id=None,
                    tool_name=tool_name,
                    retrieval_channel=channel,
                    requirement=need.requirement.value,
                    phase=ControllerActionPhase.PRIMARY,
                    fallback_condition=None,
                    query_intent=need.query_intent.value,
                )
            )
        return actions

    @staticmethod
    def _action_id(need_id: StableId, step_id: StableId) -> str:
        seed = f"action.{need_id.root}.{step_id.root}"
        if len(seed) < 128:
            return seed
        digest = hashlib.sha256(seed.encode()).hexdigest()[:32]
        return f"action.{digest}"

    def _step_action(
        self,
        need: Stage1MemoryNeed,
        step_id: StableId,
        channel: RetrievalChannel,
        phase: ControllerActionPhase,
        fallback_condition: str | None,
        called: set[tuple[StableId, str]],
    ) -> RegisteredControllerAction | None:
        tool_name = TOOL_BY_CHANNEL.get(channel)
        if tool_name is None or tool_name not in self._tool_policy.allowed_tools:
            return None
        if POOL_BY_CHANNEL[channel] not in need.allowed_candidate_pools:
            return None
        if (need.need_id, tool_name) in called:
            return None
        return RegisteredControllerAction(
            action_id=StableId(self._action_id(need.need_id, step_id)),
            need_id=need.need_id,
            route_step_id=step_id,
            tool_name=tool_name,
            retrieval_channel=channel,
            requirement=need.requirement.value,
            phase=phase,
            fallback_condition=fallback_condition,
            query_intent=need.query_intent.value,
        )

    @staticmethod
    def _primary_succeeded(
        plan: RoutePlan,
        prior_calls: Sequence[tuple[StableId, str, ToolResult]],
    ) -> bool:
        primary_tools = {
            TOOL_BY_CHANNEL[step.channel]
            for step in (
                *plan.mandatory_steps,
                *(step for group in plan.primary_groups for step in group.steps),
            )
            if step.channel in TOOL_BY_CHANNEL
        }
        return any(
            tool_name in primary_tools
            and result.status is ToolResultStatus.SUCCEEDED
            and result.coverage > 0
            for _, tool_name, result in prior_calls
        )

    @staticmethod
    def _fallback_applies(condition: str, primary_succeeded: bool) -> bool:
        if condition in {
            "anchor_evidence_insufficient",
            "plan_anchor_insufficient",
            "exact_current_record_absent",
        }:
            return not primary_succeeded
        if condition == "hierarchy_scope_resolved":
            return primary_succeeded
        raise ValueError(f"unregistered route fallback condition: {condition}")
