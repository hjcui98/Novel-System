"""Deterministic validation of grounded planner drafts."""

from __future__ import annotations

import re

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import PlanRootDocument
from novel_agent.domain.memory import ExpectedClaimScope, NeedFacetKind, WorldRootDocument
from novel_agent.domain.planning_memory import (
    GroundedNeedDraft,
    GroundingStatus,
)
from novel_agent.domain.writer_context import BenchmarkTaskContract
from novel_agent.services.task_focus import FocusSet

_VALID_WORLDLINES = frozenset({"main", ""})
_FACET_BY_PLANNER_VALUE = {item.name: item for item in NeedFacetKind}
_SCOPE_BY_PLANNER_VALUE = {item.value: item for item in ExpectedClaimScope}
_EXPECTED_SCOPE_BY_FACET: dict[NeedFacetKind, ExpectedClaimScope] = {
    NeedFacetKind.CURRENT_STATE: ExpectedClaimScope.CURRENT,
    NeedFacetKind.RELATION_STATE: ExpectedClaimScope.CURRENT,
    NeedFacetKind.CAPABILITY_STATUS: ExpectedClaimScope.CURRENT,
    NeedFacetKind.LIMITATION: ExpectedClaimScope.CURRENT,
    NeedFacetKind.KNOWLEDGE_BOUNDARY: ExpectedClaimScope.KNOWLEDGE,
    NeedFacetKind.CAUSAL_HISTORY: ExpectedClaimScope.HISTORICAL,
    NeedFacetKind.SETUP: ExpectedClaimScope.HISTORICAL,
    NeedFacetKind.UNRESOLVED_STATUS: ExpectedClaimScope.CURRENT,
    NeedFacetKind.COMMITMENT: ExpectedClaimScope.HISTORICAL,
    NeedFacetKind.PLAN_NODE: ExpectedClaimScope.PLANNED,
}
_NEED_TYPE_BY_FACETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("plan_obligation", ("PLAN_NODE",)),
    ("knowledge_boundary", ("KNOWLEDGE_BOUNDARY",)),
    ("capability_boundary", ("CAPABILITY_STATUS", "LIMITATION")),
    ("relationship_emotion", ("RELATION_STATE",)),
    ("unresolved_obligation", ("COMMITMENT", "UNRESOLVED_STATUS")),
    ("long_range_callback", ("SETUP", "UNRESOLVED_STATUS")),
    ("entity_history", ("CAUSAL_HISTORY",)),
    ("current_state", ("CURRENT_STATE",)),
)


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[\s\u3000]+", " ", text).strip().casefold().split())


class NeedValidationResult(DomainModel):
    """Accepted drafts plus typed rejection/dedup/truncation accounting."""

    accepted_drafts: tuple[GroundedNeedDraft, ...]
    need_type_by_draft: dict[str, str] = Field(default_factory=dict)
    rejected_draft_ids: tuple[str, ...] = ()
    rejected_reasons: dict[str, str] = Field(default_factory=dict)
    deduplicated_draft_ids: tuple[str, ...] = ()
    truncated_draft_ids: tuple[str, ...] = ()
    grounded_entity_count: int = Field(ge=0)
    ambiguous_entity_count: int = Field(ge=0)
    unresolved_entity_count: int = Field(ge=0)
    raw_scope_by_draft: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    canonical_scope_by_draft: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    scope_normalization_reasons: dict[str, str] = Field(default_factory=dict)


class NeedValidator:
    """Time bounds, factualization, grounding, dedup, and budget gates.

    The validator never relaxes a planner draft; it rejects or deduplicates
    and lets the orchestrator build the final ``Stage1MemoryNeed`` with the
    canonical completion contracts.
    """

    version = "need_validator.v2"

    def __init__(self, *, max_total_needs: int = 32) -> None:
        if max_total_needs < 1:
            raise ValueError("max_total_needs must be positive")
        self._max_total_needs = max_total_needs
        self._raw_scope_by_draft: dict[str, tuple[str, ...]] = {}
        self._canonical_scope_by_draft: dict[str, tuple[str, ...]] = {}
        self._scope_normalization_reasons: dict[str, str] = {}

    @classmethod
    def need_type_for_facets(
        cls,
        suggested_facets: tuple[str, ...],
    ) -> str:
        facets = set(suggested_facets)
        for need_type, required in _NEED_TYPE_BY_FACETS:
            if any(facet in facets for facet in required):
                return need_type
        return "current_state"

    @classmethod
    def sanitize_draft_id(cls, draft_id: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_.-]", "_", draft_id)
        return sanitized[:96] or "draft"

    def validate(
        self,
        *,
        drafts: tuple[GroundedNeedDraft, ...],
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        focus_set: FocusSet,
        plan: PlanRootDocument | None = None,
        goal_bindings: dict[int, str] | None = None,
    ) -> NeedValidationResult:
        del focus_set
        self._raw_scope_by_draft = {}
        self._canonical_scope_by_draft = {}
        self._scope_normalization_reasons = {}
        target_start = task.target_chapter_start
        target_end = task.target_chapter_end
        accepted: list[GroundedNeedDraft] = []
        need_type_by_draft: dict[str, str] = {}
        rejected_ids: list[str] = []
        rejected_reasons: dict[str, str] = {}
        deduplicated_ids: list[str] = []
        seen: set[tuple[str, tuple[str, ...], str]] = set()
        canonical_goals = goal_bindings or (
            {goal.chapter_index: _normalize(goal.summary) for goal in plan.chapter_goals}
            if plan is not None
            else {}
        )
        world_entity_ids = {entity.entity_id for entity in world.entities}
        for draft in drafts:
            unknown_facets = tuple(
                facet for facet in draft.suggested_facets if facet not in _FACET_BY_PLANNER_VALUE
            )
            unknown_scopes = tuple(
                scope
                for scope in draft.required_claim_scopes
                if scope not in _SCOPE_BY_PLANNER_VALUE
            )
            if unknown_facets or unknown_scopes or not draft.suggested_facets:
                rejected_ids.append(draft.draft_id)
                rejected_reasons[draft.draft_id] = "unknown_or_empty_scope_facet"
                continue
            facet_kinds = tuple(_FACET_BY_PLANNER_VALUE[item] for item in draft.suggested_facets)
            raw_scope_values = tuple(
                _SCOPE_BY_PLANNER_VALUE[item] for item in draft.required_claim_scopes
            )
            expected_scopes = {_EXPECTED_SCOPE_BY_FACET[item] for item in facet_kinds}
            if (
                NeedFacetKind.PLAN_NODE in facet_kinds
                or ExpectedClaimScope.PLANNED in raw_scope_values
            ):
                rejected_ids.append(draft.draft_id)
                rejected_reasons[draft.draft_id] = "plan_scope_not_historical_memory"
                continue
            canonical_scopes = tuple(sorted(expected_scopes, key=lambda item: item.value))
            scope_reason: str | None = None
            if not raw_scope_values:
                scope_reason = "missing_scope_canonicalized_from_facets"
            elif set(raw_scope_values) != expected_scopes:
                scope_reason = "mismatched_scope_canonicalized_from_facets"
            if scope_reason is not None:
                self._scope_normalization_reasons[draft.draft_id] = scope_reason
            need_type = self.need_type_for_facets(draft.suggested_facets)
            if draft.trigger_plan_chapters and any(
                chapter < target_start or chapter > target_end
                for chapter in draft.trigger_plan_chapters
            ):
                rejected_ids.append(draft.draft_id)
                rejected_reasons[draft.draft_id] = "out_of_range_chapters"
                continue
            if not draft.trigger_plan_chapters:
                rejected_ids.append(draft.draft_id)
                rejected_reasons[draft.draft_id] = "missing_trigger_goal_binding"
                continue
            normalized_trigger_goal = _normalize(draft.trigger_plan_goal)
            if not normalized_trigger_goal or any(
                canonical_goals.get(chapter) != normalized_trigger_goal
                for chapter in draft.trigger_plan_chapters
            ):
                rejected_ids.append(draft.draft_id)
                rejected_reasons[draft.draft_id] = "trigger_goal_mismatch"
                continue
            if draft.historical_time_scope not in _VALID_WORLDLINES:
                rejected_ids.append(draft.draft_id)
                rejected_reasons[draft.draft_id] = "unknown_time_scope"
                continue
            normalized_question = _normalize(draft.semantic_question)
            if (
                normalized_question == normalized_trigger_goal
                or normalized_trigger_goal in normalized_question
                or self._looks_future_factualized(draft.semantic_question)
            ):
                rejected_ids.append(draft.draft_id)
                rejected_reasons[draft.draft_id] = "plan_goal_as_fact"
                continue
            entity_ids = tuple(
                dict.fromkeys(
                    mention.entity_id
                    for mention in draft.entity_mentions
                    if mention.grounding_status is GroundingStatus.GROUNDED
                    and mention.entity_id is not None
                )
            )
            entity_ids = tuple(item for item in entity_ids if item in world_entity_ids)
            has_anchoring_mention = bool(draft.entity_mentions or draft.relation_mentions)
            if not has_anchoring_mention:
                # A draft with no mention at all has nothing to anchor its
                # public question to.  A draft whose mentions are unresolved
                # lexical anchors (for example a legitimate institution that
                # has no runtime entity id yet) is kept: the mention stays in
                # the semantic/lexical query text and only the exact/graph
                # routes fail closed on the missing id.
                rejected_ids.append(draft.draft_id)
                rejected_reasons[draft.draft_id] = "no_anchoring_mention"
                continue
            key = (
                _normalize(draft.semantic_question),
                tuple(entity.root for entity in entity_ids),
                need_type,
            )
            if key in seen:
                deduplicated_ids.append(draft.draft_id)
                continue
            seen.add(key)
            if len(accepted) >= self._max_total_needs:
                break
            self._raw_scope_by_draft[draft.draft_id] = tuple(
                item.value for item in raw_scope_values
            )
            self._canonical_scope_by_draft[draft.draft_id] = tuple(
                item.value for item in canonical_scopes
            )
            accepted.append(draft)
            need_type_by_draft[draft.draft_id] = need_type

        accepted_ids = {draft.draft_id for draft in accepted}
        truncated = tuple(
            draft.draft_id
            for draft in drafts
            if draft.draft_id not in accepted_ids
            and draft.draft_id not in rejected_ids
            and draft.draft_id not in deduplicated_ids
        )
        return NeedValidationResult(
            accepted_drafts=tuple(accepted),
            need_type_by_draft=need_type_by_draft,
            rejected_draft_ids=tuple(rejected_ids),
            rejected_reasons=rejected_reasons,
            deduplicated_draft_ids=tuple(deduplicated_ids),
            truncated_draft_ids=truncated,
            grounded_entity_count=sum(
                mention.grounding_status is GroundingStatus.GROUNDED
                for draft in drafts
                for mention in draft.entity_mentions
            ),
            ambiguous_entity_count=sum(
                mention.grounding_status is GroundingStatus.AMBIGUOUS
                for draft in drafts
                for mention in draft.entity_mentions
            ),
            unresolved_entity_count=sum(
                mention.grounding_status is GroundingStatus.UNRESOLVED
                for draft in drafts
                for mention in draft.entity_mentions
            ),
            raw_scope_by_draft=dict(self._raw_scope_by_draft),
            canonical_scope_by_draft=dict(self._canonical_scope_by_draft),
            scope_normalization_reasons=dict(self._scope_normalization_reasons),
        )

    @staticmethod
    def _looks_future_factualized(question: str) -> bool:
        normalized = _normalize(question)
        future_markers = ("将会", "会在", "计划中", "目标是", "准备在", "未来会")
        history_markers = (
            "历史",
            "此前",
            "曾",
            "当前",
            "是否",
            "什么",
            "如何",
            "哪些",
            "为何",
            "关系",
            "状态",
            "知",
            "承诺",
            "能力",
            "限制",
            "伏笔",
        )
        return any(marker in normalized for marker in future_markers) or not any(
            marker in normalized for marker in history_markers
        )


__all__ = ["NeedValidationResult", "NeedValidator"]
