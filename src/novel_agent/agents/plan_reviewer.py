"""Independent inquiry and PlanProposal reviewer facade."""

from __future__ import annotations

import json
from typing import Any

from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId, bounded_stable_id
from novel_agent.domain.memory import ObligationKind, long_range_kind_requires_not_before
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.planning import (
    PlanReview,
    PlanReviewDraft,
    PlanReviewIssue,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import AgentMode, AgentType
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id

_RESOLVE_MARKERS = {"resolved", "payoff", "resolve"}


class PlanReviewerInvocationError(ValueError):
    pass


def apply_host_plan_review_constraints(
    draft: PlanReviewDraft,
    *,
    target_kind: ReviewTargetKind,
    target_payload: str,
) -> PlanReviewDraft:
    """Overlay trusted temporal/parent-scope issues onto a model Plan review."""

    if target_kind is not ReviewTargetKind.PLAN_PROPOSAL:
        return draft
    try:
        payload = json.loads(target_payload)
    except json.JSONDecodeError:
        return draft
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return draft
    extra = tuple(_host_issues_for_items(raw_items))
    if not extra:
        return draft
    issues = (*draft.issues, *extra)
    missing_window = any(
        issue.kind is ReviewIssueKind.LONG_RANGE_PAYOFF_WITHOUT_TIME_WINDOW for issue in extra
    )
    if missing_window:
        return draft.model_copy(
            update={
                "issues": issues,
                "decision": ReviewDecision.HUMAN_REQUIRED,
                "revision_instruction": None,
            }
        )
    instruction = (
        draft.revision_instruction
        or "Revise future-locked payoff and parent-scope violations; keep SETUP/PROGRESS only."
    )
    return draft.model_copy(
        update={
            "issues": issues,
            "decision": ReviewDecision.REVISE,
            "revision_instruction": instruction,
        }
    )


def _host_issues_for_items(raw_items: list[object]) -> list[PlanReviewIssue]:
    issues: list[PlanReviewIssue] = []
    by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if isinstance(raw, dict) and isinstance(raw.get("item_id"), str):
            by_id[raw["item_id"]] = raw
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        raw_item_id = raw.get("item_id")
        item_id = raw_item_id if isinstance(raw_item_id, str) else "item"
        item_payload = raw.get("payload")
        if not isinstance(item_payload, dict):
            item_payload = {}
        kind_raw = str(item_payload.get("obligation_kind") or raw.get("kind") or "")
        try:
            kind = ObligationKind(kind_raw)
        except ValueError:
            kind = None
        not_before = item_payload.get("not_before_chapter")
        not_before_chapter = not_before if isinstance(not_before, int) else None
        if (
            kind is not None
            and long_range_kind_requires_not_before(kind)
            and not_before_chapter is None
        ):
            issues.append(
                _host_issue(
                    ReviewIssueKind.LONG_RANGE_PAYOFF_WITHOUT_TIME_WINDOW,
                    "long-range PROMISE/FORESHADOWING requires not_before_chapter",
                    item_id,
                    blocking=True,
                )
            )
        if _item_resolves(raw, item_payload) and not_before_chapter is not None:
            chapter = item_payload.get("chapter_index")
            if not isinstance(chapter, int):
                chapter = item_payload.get("chapter")
            if isinstance(chapter, int) and chapter < not_before_chapter:
                issues.append(
                    _host_issue(
                        ReviewIssueKind.EARLY_RESOLUTION_OF_FUTURE_LOCKED_OBLIGATION,
                        "future-locked obligation cannot be resolved in this planning scope",
                        item_id,
                        blocking=True,
                    )
                )
        parent_id = item_payload.get("parent_id")
        if isinstance(parent_id, str) and parent_id in by_id:
            parent_payload = by_id[parent_id].get("payload")
            if not isinstance(parent_payload, dict):
                parent_payload = {}
            child_start = _optional_int(item_payload.get("chapter_start"))
            child_end = _optional_int(item_payload.get("chapter_end"))
            parent_start = _optional_int(parent_payload.get("chapter_start"))
            parent_end = _optional_int(parent_payload.get("chapter_end"))
            if (
                child_start is not None
                and child_end is not None
                and parent_start is not None
                and parent_end is not None
                and (child_start < parent_start or child_end > parent_end)
            ):
                issues.append(
                    _host_issue(
                        ReviewIssueKind.TARGET_WINDOW_OUTSIDE_PARENT_SCOPE,
                        "child plan scope exceeds parent scope",
                        item_id,
                        blocking=True,
                    )
                )
    return issues


def _item_resolves(raw: dict[str, Any], payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("obligation_status") or "").lower()
    operation = str(payload.get("operation") or "").lower()
    kind = str(raw.get("kind") or "").lower()
    return status in _RESOLVE_MARKERS or operation in _RESOLVE_MARKERS or kind in _RESOLVE_MARKERS


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _host_issue(
    kind: ReviewIssueKind,
    summary: str,
    item_id: str,
    *,
    blocking: bool,
) -> PlanReviewIssue:
    return PlanReviewIssue(
        issue_id=bounded_stable_id(f"issue.{kind.value}.{item_id}", f"issue.{kind.value}"),
        kind=kind,
        summary=summary,
        blocking=blocking,
        affected_item_ids=(StableId(item_id),) if _is_stable_id(item_id) else (),
    )


def _is_stable_id(value: str) -> bool:
    try:
        StableId(value)
    except ValueError:
        return False
    return True


class PlanReviewerAgent:
    def __init__(self, runner: StructuredAgentRunner, artifacts: ArtifactRepository) -> None:
        self._runner = runner
        self._artifacts = artifacts

    async def review(
        self,
        *,
        version: SchemaVersion,
        mode: AgentMode,
        target_kind: ReviewTargetKind,
        target_payload: str,
        target_artifact: ArtifactRef,
        trusted_source_artifacts: tuple[ArtifactRef, ...],
        request: ModelRequest,
        base_commit: CommitId | None,
    ) -> tuple[PlanReview, ArtifactRef, ModelCallRecord]:
        inputs = (*trusted_source_artifacts, target_artifact)
        prepared = self._runner.prepare(
            AgentType.PLAN_REVIEWER,
            mode,
            version.root,
            request,
            (
                f"REVIEW_TARGET_KIND={target_kind.value}\n"
                f"REVIEW_TARGET={target_payload}\n"
                "PLANNER_HIDDEN_REASONING=not_supplied"
            ),
            source_hashes=tuple(item.artifact_id for item in trusted_source_artifacts),
            input_artifacts=inputs,
            base_commit=base_commit,
        )
        execution = await self._runner.execute(prepared, PlanReviewDraft)
        draft = execution.output
        if draft.target_kind is not target_kind:
            raise PlanReviewerInvocationError("Reviewer changed the trusted target kind")
        draft = apply_host_plan_review_constraints(
            draft,
            target_kind=target_kind,
            target_payload=target_payload,
        )
        draft_artifact = self._artifacts.put(
            canonical_json_bytes(draft.model_dump(mode="json")),
            "application/vnd.novel-agent.plan-review-draft+json",
            version,
        )
        receipt = self._runner.receipt(
            prepared,
            execution.model_call,
            output_artifacts=(draft_artifact,),
            unresolved=tuple(issue.summary for issue in draft.issues if issue.blocking),
        )
        identity = content_id(
            {
                "target": target_artifact.artifact_id.root,
                "draft": draft.model_dump(mode="json"),
                "receipt": receipt.receipt_id.root,
            }
        ).root.removeprefix("sha256:")[:24]
        review = PlanReview(
            review_id=StableId(f"plan-review.{identity}"),
            target_kind=draft.target_kind,
            target_artifact_ref=target_artifact,
            decision=draft.decision,
            issues=draft.issues,
            preserve_item_ids=draft.preserve_item_ids,
            revision_instruction=draft.revision_instruction,
            memory_gap_questions=draft.memory_gap_questions,
            receipt=receipt,
        )
        review_artifact = self._artifacts.put(
            canonical_json_bytes(review.model_dump(mode="json")),
            "application/vnd.novel-agent.plan-review+json",
            version,
        )
        return review, review_artifact, execution.model_call
