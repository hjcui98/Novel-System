"""Deterministic Controller C1+C2 observation assembly from existing tool results."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from novel_agent.domain.memory import ChannelHit, RequirementLevel, Stage1MemoryNeed
from novel_agent.domain.stage2 import ToolResult, ToolResultStatus
from novel_agent.runtime.memory_controller import ControllerStateView

C3_ADMISSION = "NOT_ADMITTED"
DEFAULT_PREVIEW_CHARS = 240


class ContextAssemblyBudgetExceeded(ValueError):
    """Protected identity plus mandatory Need/preview exceeds the input budget."""


class CompactionRoute(StrEnum):
    NONE = "none"
    TRUNCATE_CANDIDATE_TEXT = "truncate_candidate_text"
    DROP_OPTIONAL_NEED = "drop_optional_need"
    COMPACT_OLDEST_ACTIONS = "compact_oldest_actions"
    PRESERVE_MANDATORY_PREVIEW = "preserve_mandatory_preview"
    DROP_MANDATORY_PREVIEW = "drop_mandatory_preview"


@dataclass(frozen=True, slots=True)
class ControllerObservationAssembly:
    """Auditable C0+C1+C2 payload plus compaction evidence. C3 stays NOT_ADMITTED."""

    context_level: str
    available_input_tokens: int
    input_tokens: int
    preview_count: int
    truncated_preview_count: int
    zero_preview_cause: str | None
    dropped_optional_needs: int
    dropped_actions: int
    compaction_route: CompactionRoute
    payload: dict[str, object]
    c3_admission: str = C3_ADMISSION

    def telemetry(self) -> dict[str, object]:
        """Return the bounded context facts that belong in the execution receipt."""

        return {
            "context_level": self.context_level,
            "available_input_tokens": self.available_input_tokens,
            "input_tokens": self.input_tokens,
            "preview_count": self.preview_count,
            "truncated_preview_count": self.truncated_preview_count,
            "zero_preview_cause": self.zero_preview_cause,
            "dropped_optional_needs": self.dropped_optional_needs,
            "dropped_actions": self.dropped_actions,
            "compaction_route": self.compaction_route.value,
            "c3_admission": self.c3_admission,
            "budget_source": "effective_budget",
        }


class ControllerObservationAssembler:
    """Assemble Need contracts and bounded candidate previews. Retrieval is unchanged."""

    def __init__(self, *, preview_chars: int = DEFAULT_PREVIEW_CHARS) -> None:
        if preview_chars < 1:
            raise ValueError("preview character budget must be positive")
        self._preview_chars = preview_chars

    def assemble(
        self,
        state: ControllerStateView,
        *,
        available_actions: list[dict[str, object]],
        registered_action_ids: list[str],
        round_index: int,
        max_agentic_actions: int,
        available_input_tokens: int,
    ) -> ControllerObservationAssembly:
        if available_input_tokens < 1:
            raise ContextAssemblyBudgetExceeded(
                "protected Controller identity exceeds the available input budget"
            )
        needs = state["request"].initial_memory_needs
        resolved = {
            need_id
            for need_id, _tool, result in state["tool_calls"]
            if result.status is ToolResultStatus.SUCCEEDED and result.coverage > 0
        }
        need_summaries: list[dict[str, object]] = [
            self._need_contract(need, need.need_id in resolved) for need in needs
        ]
        outcomes: list[dict[str, object]] = [
            self._action_outcome(need_id.root, tool_name, result)
            for need_id, tool_name, result in state["tool_calls"]
        ]
        previews = self._previews(state, self._preview_chars)
        initial_preview_count = len(previews)
        payload: dict[str, object] = {
            "task_contract": {
                "request_id": state["request"].request_id.root,
                "base_commit": state["request"].base_commit.root,
                "snapshot_id": state["request"].snapshot_id.root,
                "narrative_chapter": state["request"].narrative_chapter,
            },
            "need_summaries": need_summaries,
            "available_actions": available_actions,
            "registered_action_ids": registered_action_ids,
            "prior_action_outcomes": outcomes,
            "candidate_previews": previews,
            "round_index": round_index,
            "max_agentic_actions": max_agentic_actions,
            "c3_admission": C3_ADMISSION,
        }
        route = CompactionRoute.NONE
        truncated = 0
        dropped_optional = 0
        dropped_actions = 0
        preview_chars = self._preview_chars
        if self._tokens(payload) > available_input_tokens:
            preview_chars = max(32, preview_chars // 4)
            previews = self._previews(state, preview_chars)
            payload["candidate_previews"] = previews
            truncated = len(previews)
            route = CompactionRoute.TRUNCATE_CANDIDATE_TEXT
        if self._tokens(payload) > available_input_tokens:
            kept_needs = [
                item
                for item, need in zip(need_summaries, needs, strict=True)
                if need.requirement is RequirementLevel.MANDATORY
            ]
            dropped_optional = len(need_summaries) - len(kept_needs)
            need_summaries = kept_needs
            payload["need_summaries"] = need_summaries
            route = CompactionRoute.DROP_OPTIONAL_NEED
        if self._tokens(payload) > available_input_tokens:
            while outcomes and self._tokens(payload) > available_input_tokens:
                outcomes.pop(0)
                dropped_actions += 1
                payload["prior_action_outcomes"] = outcomes
            route = CompactionRoute.COMPACT_OLDEST_ACTIONS
        if self._tokens(payload) > available_input_tokens:
            mandatory_ids = {
                need.need_id.root
                for need in needs
                if need.requirement is RequirementLevel.MANDATORY
            }
            preserved: list[dict[str, object]] = []
            seen_need_ids: set[str] = set()
            for preview in previews:
                need_id = preview.get("need_id")
                if not isinstance(need_id, str) or need_id not in mandatory_ids:
                    continue
                if need_id in seen_need_ids:
                    continue
                seen_need_ids.add(need_id)
                preserved.append(
                    {
                        **preview,
                        "text": str(preview.get("text", ""))[:32],
                        "truncated": True,
                    }
                )
            previews = preserved
            payload["candidate_previews"] = previews
            route = (
                CompactionRoute.PRESERVE_MANDATORY_PREVIEW
                if previews
                else CompactionRoute.DROP_MANDATORY_PREVIEW
            )
            truncated = sum(bool(item.get("truncated")) for item in previews)
        if self._tokens(payload) > available_input_tokens:
            raise ContextAssemblyBudgetExceeded(
                "protected identity and mandatory Need contract/preview exceed the input budget"
            )
        zero_preview_cause = None
        if not previews:
            zero_preview_cause = (
                "no_resolved_hits" if initial_preview_count == 0 else "compaction_removed_previews"
            )
        return ControllerObservationAssembly(
            context_level="C1+C2" if previews else "C1",
            available_input_tokens=available_input_tokens,
            input_tokens=self._tokens(payload),
            preview_count=len(previews),
            truncated_preview_count=truncated,
            zero_preview_cause=zero_preview_cause,
            dropped_optional_needs=dropped_optional,
            dropped_actions=dropped_actions,
            compaction_route=route,
            payload=payload,
        )

    def _need_contract(self, need: Stage1MemoryNeed, resolved: bool) -> dict[str, object]:
        facet_ids = tuple(facet.need_facet_id.root for facet in need.need_facets)
        if need.completion_spec is not None:
            facet_ids = tuple(item.root for item in need.completion_spec.required_need_facet_ids)
        return {
            "id": need.need_id.root,
            "intent": need.query_intent.value,
            "requirement": need.requirement.value,
            "query_text": need.query_text,
            "semantic_question": need.semantic_question,
            "entity_ids": [item.root for item in need.entity_ids],
            "required_facet_ids": list(facet_ids),
            "mandatory": need.requirement is RequirementLevel.MANDATORY,
            "resolved": resolved,
        }

    def _action_outcome(
        self,
        need_id: str,
        tool_name: str,
        result: ToolResult,
    ) -> dict[str, object]:
        return {
            "need_id": need_id,
            "tool_name": tool_name,
            "success": result.status is ToolResultStatus.SUCCEEDED,
            "candidate_count": result.channel_candidate_count or 0,
            "gain": result.new_information_gain,
            "failure_code": (
                result.failure_code.value if result.failure_code is not None else None
            ),
        }

    def _previews(self, state: ControllerStateView, preview_chars: int) -> list[dict[str, object]]:
        previews: list[dict[str, object]] = []
        for need_id, tool_name, result in state["tool_calls"]:
            if result.status is not ToolResultStatus.SUCCEEDED or not isinstance(
                result.payload, dict
            ):
                continue
            payload_hits = result.payload.get("hits")
            if not isinstance(payload_hits, list):
                continue
            for raw in payload_hits:
                if not isinstance(raw, dict):
                    continue
                hit = ChannelHit.model_validate_json(json.dumps(raw))
                text = hit.unit.text
                truncated = text[:preview_chars]
                previews.append(
                    {
                        "need_id": need_id.root,
                        "tool_name": tool_name,
                        "unit_id": hit.unit.unit_id.root,
                        "channel": hit.channel.value,
                        "rank": hit.channel_rank,
                        "chapter": hit.unit.narrative_start,
                        "predicate": hit.unit.predicate,
                        "truth_class": (
                            hit.unit.truth_class.value if hit.unit.truth_class is not None else None
                        ),
                        "text": truncated,
                        "truncated": len(text) > preview_chars,
                    }
                )
        return previews

    @staticmethod
    def _tokens(payload: dict[str, object]) -> int:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode(
            "utf-8"
        )
        return max(1, (len(encoded) + 2) // 3)


def build_c0_payload(
    state: ControllerStateView,
    *,
    available_actions: list[dict[str, object]],
    round_index: int,
) -> dict[str, object]:
    """Existing compact-off Controller payload. Retrieval output is passed through."""

    return {
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


def _hit_texts(state: ControllerStateView) -> tuple[tuple[str, str, str], ...]:
    texts: list[tuple[str, str, str]] = []
    for need_id, tool_name, result in state["tool_calls"]:
        if not isinstance(result.payload, dict):
            continue
        hits = result.payload.get("hits")
        if not isinstance(hits, list):
            continue
        for raw in hits:
            if isinstance(raw, dict):
                texts.append((need_id.root, tool_name, json.dumps(raw, sort_keys=True)))
    return tuple(texts)


@dataclass(frozen=True, slots=True)
class ControllerContextLevelComparison:
    """Deterministic C0 vs C1+C2 contrast. Does not call a model or change retrieval."""

    c0_payload: dict[str, object]
    c1c2: ControllerObservationAssembly
    retrieval_hit_texts_unchanged: bool
    c3_admission: str
    c0_token_estimate: int
    c1c2_token_estimate: int


def compare_c0_to_c1c2(
    state: ControllerStateView,
    *,
    available_actions: list[dict[str, object]],
    registered_action_ids: list[str],
    round_index: int,
    max_agentic_actions: int,
    available_input_tokens: int,
    assembler: ControllerObservationAssembler | None = None,
) -> ControllerContextLevelComparison:
    before = _hit_texts(state)
    c0 = build_c0_payload(
        state,
        available_actions=available_actions,
        round_index=round_index,
    )
    assembly = (assembler or ControllerObservationAssembler()).assemble(
        state,
        available_actions=available_actions,
        registered_action_ids=registered_action_ids,
        round_index=round_index,
        max_agentic_actions=max_agentic_actions,
        available_input_tokens=available_input_tokens,
    )
    return ControllerContextLevelComparison(
        c0_payload=c0,
        c1c2=assembly,
        retrieval_hit_texts_unchanged=before == _hit_texts(state),
        c3_admission=assembly.c3_admission,
        c0_token_estimate=ControllerObservationAssembler._tokens(c0),
        c1c2_token_estimate=assembly.input_tokens,
    )
