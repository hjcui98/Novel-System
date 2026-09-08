"""Independent inquiry and PlanProposal reviewer facade."""

from __future__ import annotations

import json
import re
from typing import Any

from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId, bounded_stable_id
from novel_agent.domain.memory import ObligationKind, long_range_kind_requires_not_before
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.planning import (
    PlannerContextPackage,
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
_HISTORY_NEED_KINDS = frozenset(
    {
        "causal_history",
        "knowledge_origin",
        "relationship_origin",
        "setup_evidence",
        "object_origin",
    }
)


class PlanReviewerInvocationError(ValueError):
    pass


def apply_host_plan_review_constraints(
    draft: PlanReviewDraft,
    *,
    target_kind: ReviewTargetKind,
    target_payload: str,
    mode: AgentMode = AgentMode.ARC_VOLUME,
    expected_volume_count: int | None = None,
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
    extra = tuple(
        _host_issues_for_items(
            raw_items,
            mode=mode,
            expected_volume_count=(
                expected_volume_count
                if expected_volume_count is not None
                else _expected_volume_count(payload)
            ),
        )
    )
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


def _host_issues_for_items(
    raw_items: list[object], *, mode: AgentMode, expected_volume_count: int | None = None
) -> list[PlanReviewIssue]:
    issues: list[PlanReviewIssue] = []
    by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if isinstance(raw, dict) and isinstance(raw.get("item_id"), str):
            by_id[raw["item_id"]] = raw
    volume_items = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        raw_item_id = raw.get("item_id")
        item_id = raw_item_id if isinstance(raw_item_id, str) else "item"
        item_payload = raw.get("payload")
        if not isinstance(item_payload, dict):
            item_payload = {}
        kind_raw = str(item_payload.get("obligation_kind") or raw.get("kind") or "")
        level_raw = str(item_payload.get("plan_level") or raw.get("kind") or "").lower()
        if level_raw in {"arc_volume", "volume", "volume_scope", "volume_arc", "arc"}:
            volume_items += 1
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
            if not isinstance(chapter, int):
                chapter = item_payload.get("target_chapter_start")
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
        if mode is AgentMode.CHAPTER_SET and _is_chapter_item(raw, item_payload):
            _append_history_need_issues(issues, item_payload, item_id)
    if (
        mode is AgentMode.ARC_VOLUME
        and expected_volume_count is not None
        and volume_items != expected_volume_count
    ):
        issues.append(
            _host_issue(
                ReviewIssueKind.COVERAGE,
                f"expected {expected_volume_count} arc volumes but received {volume_items}",
                "volume-coverage",
                blocking=True,
            )
        )
    return issues


def _is_chapter_item(raw: dict[str, Any], payload: dict[str, Any]) -> bool:
    level = str(payload.get("plan_level") or raw.get("plan_level") or "").lower()
    kind = str(raw.get("kind") or "").lower()
    return (
        level in {"chapter", "chapter_goal"}
        or "chapter" in kind
        or isinstance(payload.get("chapter_index"), int)
        or isinstance(payload.get("chapter"), int)
    )


def _append_history_need_issues(
    issues: list[PlanReviewIssue], payload: dict[str, Any], item_id: str
) -> None:
    declared = payload.get("history_needs")
    if declared is None:
        return
    if not isinstance(declared, list):
        issues.append(
            _host_issue(
                ReviewIssueKind.COVERAGE,
                "HISTORY_NEEDS_INVALID: history_needs must be a list",
                item_id,
                blocking=True,
            )
        )
        return
    if len(declared) > 3:
        issues.append(
            _host_issue(
                ReviewIssueKind.COVERAGE,
                f"HISTORY_NEEDS_LIMIT: expected at most 3, received {len(declared)}",
                item_id,
                blocking=True,
            )
        )
    seen: set[tuple[str, str]] = set()
    for index, need in enumerate(declared):
        if not isinstance(need, dict):
            issues.append(
                _host_issue(
                    ReviewIssueKind.COVERAGE,
                    f"HISTORY_NEEDS_INVALID: item {index} must be an object",
                    item_id,
                    blocking=True,
                )
            )
            continue
        kind = need.get("kind")
        query = need.get("query")
        if not isinstance(kind, str) or kind not in _HISTORY_NEED_KINDS:
            issues.append(
                _host_issue(
                    ReviewIssueKind.COVERAGE,
                    f"HISTORY_NEEDS_INVALID: item {index} has unsupported kind",
                    item_id,
                    blocking=True,
                )
            )
        if not isinstance(query, str) or not query.strip():
            issues.append(
                _host_issue(
                    ReviewIssueKind.COVERAGE,
                    f"HISTORY_NEEDS_INVALID: item {index} requires a non-empty query",
                    item_id,
                    blocking=True,
                )
            )
        if isinstance(kind, str) and isinstance(query, str) and query.strip():
            key = (kind, query.strip())
            if key in seen:
                issues.append(
                    _host_issue(
                        ReviewIssueKind.COVERAGE,
                        "HISTORY_NEEDS_INVALID: duplicate (kind, query)",
                        item_id,
                        blocking=True,
                    )
                )
            seen.add(key)


def _expected_volume_count(payload: dict[str, Any]) -> int | None:
    candidates: list[object] = [
        payload.get("expected_volume_count"),
        payload.get("volume_count"),
    ]
    for raw in payload.get("items", ()):
        if isinstance(raw, dict) and isinstance(raw.get("payload"), dict):
            candidates.extend(
                (
                    raw["payload"].get("expected_volume_count"),
                    raw["payload"].get("volume_count"),
                )
            )
    for candidate in candidates:
        if type(candidate) is int and candidate > 0:
            return candidate
    return None


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
        review_context = self._review_context_data(trusted_source_artifacts)
        context_block = (
            '<REVIEW_CONTEXT_DATA instruction_authority="none">\n'
            f"{review_context or '(no additional context data)'}\n"
            "</REVIEW_CONTEXT_DATA>"
        )
        prepared = self._runner.prepare(
            AgentType.PLAN_REVIEWER,
            mode,
            version.root,
            request,
            (
                f"REVIEW_TARGET_KIND={target_kind.value}\n"
                f"{context_block}\n"
                f"<REVIEW_TARGET_DATA>\n{target_payload}\n</REVIEW_TARGET_DATA>\n"
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
            mode=mode,
            target_kind=target_kind,
            target_payload=target_payload,
            expected_volume_count=_expected_volume_count_from_context(review_context),
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

    def _review_context_data(self, refs: tuple[ArtifactRef, ...]) -> str:
        rendered: list[str] = []
        source_text: list[str] = []
        for ref in refs:
            try:
                raw = self._artifacts.read_verified(ref)
                if ref.media_type == "application/vnd.novel-agent.planner-context-package+json":
                    package = PlannerContextPackage.model_validate_json(raw, strict=True)
                    rendered.append(package.rendered_context)
                elif ref.media_type.startswith("text/"):
                    source_text.append(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise PlanReviewerInvocationError("Reviewer context artifact is invalid") from error
        return "\n\n".join(
            dict.fromkeys(
                part
                for part in (*rendered, *source_text)
                if part.strip()
            )
        )


def _expected_volume_count_from_context(context: str) -> int | None:
    """Read only the trusted Profile/constraint value used by host coverage checks."""

    for key in ("expected_volume_count", "volume_count"):
        match = re.search(rf'"{re.escape(key)}"\s*:\s*(\d+)', context)
        if match is not None:
            value = int(match.group(1))
            if value > 0:
                return value
    return None
