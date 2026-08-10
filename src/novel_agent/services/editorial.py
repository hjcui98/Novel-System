"""Trusted Editor review and bounded candidate-repair services."""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ValidationError

from novel_agent.agents.editor import EditorAgent
from novel_agent.agents.runner import AgentRunResult
from novel_agent.domain.editorial import (
    DraftSpan,
    EditorialIssue,
    EditorialIssueDraft,
    EditorialLocation,
    EditorialRepairHistoryEntry,
    EditorialReport,
    EditorialReviewInput,
    EditorialSeverity,
    EditorialVerdict,
    EditorReviewPayload,
    LocalRepairScope,
    RepairedDraft,
)
from novel_agent.domain.generation import RewriteDirective, RewriteScope
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id

REPAIRED_TEXT_MEDIA_TYPE: Final[str] = "text/plain; charset=utf-8"
REWRITE_DIRECTIVE_MEDIA_TYPE: Final[str] = (
    "application/vnd.novel-agent.editor-rewrite-directive+json"
)


class EditorialServiceError(ValueError):
    """Base class for fail-closed Editor service errors."""


class EditorialReviewError(EditorialServiceError):
    """The Editor could not produce a trusted EditorialReport."""


class EditorialRepairError(EditorialServiceError):
    """The requested bounded local repair could not produce a safe candidate."""


@dataclass(frozen=True, slots=True)
class _DraftBlock:
    block_id: StableId
    start: int
    end: int
    text: str


class EditorialService:
    """Run independent review and exactly one bounded local repair."""

    def __init__(
        self,
        editor: EditorAgent,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion,
    ) -> None:
        self._editor = editor
        self._artifacts = artifacts
        self._schema_version = schema_version

    async def review(
        self,
        review_input: EditorialReviewInput,
        request: ModelRequest,
    ) -> EditorialReport:
        """Produce one read-only report; no candidate text is written or modified."""

        text = self._read_draft_text(review_input)
        blocks = _draft_blocks(review_input.draft.draft_id, text)
        payload = _review_payload(review_input, text, blocks)
        try:
            run = await self._editor.review(
                request,
                payload,
                source_hashes=(review_input.draft.text_artifact.artifact_id,),
                input_artifacts=(review_input.draft.text_artifact,),
                base_commit=review_input.context.base_commit,
            )
        except (ValidationError, ValueError, RuntimeError) as error:
            raise EditorialReviewError("Editor REVIEW failed without a report") from error
        try:
            return self._build_report(
                review_input,
                review_input.draft.draft_id,
                text,
                blocks,
                run,
            )
        except (ValidationError, ValueError, RuntimeError) as error:
            raise EditorialReviewError(
                "Editor REVIEW output did not form a valid report"
            ) from error

    async def review_repaired(
        self,
        review_input: EditorialReviewInput,
        repair_report: EditorialReport,
        repaired: RepairedDraft,
        request: ModelRequest,
    ) -> EditorialReport:
        """Review the repaired candidate once before it can leave the local-repair path."""

        _validate_repair_target(review_input, repair_report)
        if repaired.parent_draft_id != review_input.draft.draft_id:
            raise EditorialReviewError("repaired candidate belongs to another Draft")
        if repaired.repair_report_id != repair_report.report_id:
            raise EditorialReviewError("repaired candidate belongs to another repair report")
        try:
            text = self._artifacts.read_verified(repaired.text_artifact).decode("utf-8")
        except Exception as error:
            raise EditorialReviewError("Editor could not read the repaired candidate") from error
        if not text.strip():
            raise EditorialReviewError("Editor cannot review a blank repaired candidate")
        blocks = _draft_blocks(repaired.draft_id, text)
        history = (
            *review_input.prior_repair_history,
            EditorialRepairHistoryEntry(
                report_id=repair_report.report_id,
                draft_id=review_input.draft.draft_id,
                verdict=repair_report.verdict,
                repaired_draft_id=repaired.draft_id,
            ),
        )
        payload = _review_payload(
            review_input,
            text,
            blocks,
            draft_id=repaired.draft_id,
            prior_repair_history=history,
        )
        try:
            run = await self._editor.review(
                request,
                payload,
                source_hashes=(repaired.text_artifact.artifact_id,),
                input_artifacts=(repaired.text_artifact,),
                base_commit=review_input.context.base_commit,
            )
            return self._build_report(
                review_input,
                repaired.draft_id,
                text,
                blocks,
                run,
            )
        except (ValidationError, ValueError, RuntimeError) as error:
            raise EditorialReviewError(
                "Editor repaired-candidate REVIEW did not form a valid report"
            ) from error

    async def repair(
        self,
        review_input: EditorialReviewInput,
        report: EditorialReport,
        request: ModelRequest,
    ) -> RepairedDraft:
        """Apply one frozen LOCAL_REPAIR report and return a new candidate Draft."""

        _validate_repair_target(review_input, report)
        scope = report.repair_scope
        if scope is None:  # pragma: no cover - protected by EditorialReport validation
            raise EditorialRepairError("LOCAL_REPAIR report has no repair scope")
        original = self._read_draft_text(review_input)
        blocks = _draft_blocks(review_input.draft.draft_id, original)
        payload = _repair_payload(review_input, report, original, blocks)
        try:
            run = await self._editor.local_repair(
                request,
                payload,
                source_hashes=(review_input.draft.text_artifact.artifact_id,),
                input_artifacts=(review_input.draft.text_artifact,),
                base_commit=review_input.context.base_commit,
            )
        except (ValidationError, ValueError, RuntimeError) as error:
            raise EditorialRepairError("Editor LOCAL_REPAIR failed without a candidate") from error

        repaired_text = run.output.repaired_text
        changed_spans = _changed_spans(review_input.draft.draft_id, original, repaired_text)
        if not changed_spans:
            raise EditorialRepairError("LOCAL_REPAIR produced no text change")
        if any(not _span_inside(span, scope.allowed_spans) for span in changed_spans):
            raise EditorialRepairError("LOCAL_REPAIR changed text outside its frozen scope")
        try:
            text_artifact = self._artifacts.put(
                repaired_text.encode("utf-8"),
                REPAIRED_TEXT_MEDIA_TYPE,
                self._schema_version,
            )
        except Exception as error:
            raise EditorialRepairError("LOCAL_REPAIR candidate artifact write failed") from error

        receipt = run.receipt.model_copy(update={"output_artifacts": (text_artifact,)})
        repaired_id = content_id(
            {
                "kind": "editor-local-repair-v1",
                "parent_draft_id": review_input.draft.draft_id.root,
                "repair_report_id": report.report_id.root,
                "text_artifact": text_artifact.model_dump(mode="json"),
            }
        )
        try:
            return RepairedDraft(
                draft_id=repaired_id,
                parent_draft_id=review_input.draft.draft_id,
                repair_report_id=report.report_id,
                text_artifact=text_artifact,
                changed_spans=changed_spans,
                editor_receipt=receipt,
                model_call_record=run.model_call,
                created_at=run.model_call.completed_at,
            )
        except (ValidationError, ValueError) as error:
            raise EditorialRepairError("LOCAL_REPAIR candidate lineage is invalid") from error

    def _read_draft_text(self, review_input: EditorialReviewInput) -> str:
        artifact = review_input.draft.text_artifact
        try:
            text = self._artifacts.read_verified(artifact).decode("utf-8")
        except Exception as error:
            raise EditorialReviewError("Editor could not read the candidate Draft") from error
        if not text.strip():
            raise EditorialReviewError("Editor cannot review a blank candidate Draft")
        return text

    def _build_report(
        self,
        review_input: EditorialReviewInput,
        draft_id: ArtifactId,
        text: str,
        blocks: tuple[_DraftBlock, ...],
        run: AgentRunResult[EditorReviewPayload],
    ) -> EditorialReport:
        # The concrete type is kept local to avoid making the public AgentRunResult part of the
        # service contract; the runner has already validated the output and receipt.
        payload = run.output
        report_id = _stable_id(
            "editorial-report",
            {
                "draft_id": draft_id.root,
                "request_id": run.model_call.request_id.root,
                "payload": payload.model_dump(mode="json"),
            },
        )
        issues = tuple(
            _materialize_issue(report_id, index, issue, text, blocks)
            for index, issue in enumerate(payload.issues)
        )
        repair_scope: LocalRepairScope | None = None
        rewrite_directive: RewriteDirective | None = None
        receipt = run.receipt
        if payload.verdict is EditorialVerdict.LOCAL_REPAIR:
            blocking = tuple(
                issue
                for issue in issues
                if issue.repairable
                or issue.structural
                or issue.severity in {EditorialSeverity.ERROR, EditorialSeverity.CRITICAL}
            )
            if not blocking or any(issue.location is None for issue in blocking):
                raise EditorialReviewError(
                    "LOCAL_REPAIR requires a concrete trusted location for every blocking issue"
                )
            repair_scope = LocalRepairScope(
                issue_ids=tuple(issue.issue_id for issue in blocking),
                allowed_spans=tuple(
                    DraftSpan(
                        block_id=issue.location.block_id,
                        start=issue.location.start or 0,
                        end=issue.location.end or 0,
                    )
                    for issue in blocking
                    if issue.location is not None
                ),
                instructions=payload.repair_instructions,
                preserve_requirements=payload.preserve_requirements,
            )
        elif payload.verdict is EditorialVerdict.MAJOR_REWRITE:
            directive_payload = {
                "parent_draft_id": draft_id.root,
                "scope": RewriteScope.MAJOR_REWRITE.value,
                "instructions": payload.rewrite_targets,
                "preserve_requirements": payload.rewrite_preserve_requirements,
                "planner_replan_required": payload.planner_replan_required,
            }
            directive_artifact = self._artifacts.put(
                canonical_json_bytes(directive_payload),
                REWRITE_DIRECTIVE_MEDIA_TYPE,
                self._schema_version,
            )
            directive = RewriteDirective(
                directive_id=_stable_id("rewrite-directive", directive_payload),
                parent_draft_id=draft_id,
                scope=RewriteScope.MAJOR_REWRITE,
                directive_artifact=directive_artifact,
                instructions=payload.rewrite_targets,
                preserve_requirements=payload.rewrite_preserve_requirements,
            )
            rewrite_directive = directive
            receipt = receipt.model_copy(update={"output_artifacts": (directive_artifact,)})
        return EditorialReport(
            report_id=report_id,
            draft_id=draft_id,
            task_contract_id=review_input.writing_task.contract_id,
            context_id=review_input.context.context_id,
            base_commit=review_input.context.base_commit,
            verdict=payload.verdict,
            issues=issues,
            repair_scope=repair_scope,
            rewrite_directive=rewrite_directive,
            planner_replan_required=payload.planner_replan_required,
            unresolved_needs=payload.unresolved_needs,
            receipt=receipt,
            model_call_record=run.model_call,
            created_at=run.model_call.completed_at,
        )


def _review_payload(
    review_input: EditorialReviewInput,
    text: str,
    blocks: tuple[_DraftBlock, ...],
    *,
    draft_id: ArtifactId | None = None,
    prior_repair_history: tuple[EditorialRepairHistoryEntry, ...] | None = None,
) -> Mapping[str, object]:
    return {
        "draft_id": (draft_id or review_input.draft.draft_id).root,
        "writing_task": review_input.writing_task.model_dump(mode="json"),
        "context_summary": _context_summary(review_input),
        "prior_repair_history": (
            review_input.prior_repair_history
            if prior_repair_history is None
            else prior_repair_history
        ),
        "draft_blocks": tuple(
            {"block_id": block.block_id.root, "text": block.text} for block in blocks
        ),
        "draft_text": text,
    }


def _repair_payload(
    review_input: EditorialReviewInput,
    report: EditorialReport,
    text: str,
    blocks: tuple[_DraftBlock, ...],
) -> Mapping[str, object]:
    return {
        "draft_id": review_input.draft.draft_id.root,
        "repair_scope": report.repair_scope,
        "issues": report.issues,
        "writing_task": review_input.writing_task.model_dump(mode="json"),
        "context_summary": _context_summary(review_input),
        "draft_blocks": tuple(
            {"block_id": block.block_id.root, "text": block.text} for block in blocks
        ),
        "draft_text": text,
    }


def _context_summary(review_input: EditorialReviewInput) -> tuple[dict[str, object], ...]:
    context = review_input.context
    items = getattr(context, "items", None)
    if items is None:
        items = tuple(
            item
            for section in (
                "mandatory_constraints",
                "current_world_state",
                "active_plan_obligations",
                "relevant_historical_events",
                "truth_and_knowledge_boundaries",
                "raw_evidence_spans",
                "style_or_reference_optional",
            )
            for item in getattr(context, section, ())
        )
    return tuple(_context_item_summary(item) for item in items)


def _context_item_summary(item: object) -> dict[str, object]:
    if isinstance(item, BaseModel):
        raw = item.model_dump(mode="json")
    elif isinstance(item, Mapping):
        raw = {str(key): value for key, value in item.items()}
    else:
        return {"text": str(item)}
    item_id = raw.get("item_id", raw.get("unit_id"))
    if isinstance(item_id, str):
        item_id = item_id.removeprefix("stable:")
    entity_ids = raw.get("entity_ids", ())
    if not isinstance(entity_ids, (tuple, list)):
        entity_ids = ()
    return {
        "item_id": item_id,
        "category": raw.get("category", raw.get("unit_kind", "context")),
        "text": raw.get("text", ""),
        "entity_ids": tuple(str(entity_id) for entity_id in entity_ids),
        "predicate": raw.get("predicate"),
        "truth_class": raw.get("truth_class"),
        "support_status": raw.get("support_status"),
        "mandatory": raw.get("mandatory", False),
    }


def _materialize_issue(
    report_id: StableId,
    index: int,
    issue: EditorialIssueDraft,
    text: str,
    blocks: tuple[_DraftBlock, ...],
) -> EditorialIssue:
    location: EditorialLocation | None = None
    if issue.evidence_quote is not None:
        resolved = _resolve_quote(text, blocks, issue.evidence_quote, issue.occurrence)
        if resolved is None:
            raise EditorialReviewError("Editor issue evidence quote is absent from the Draft")
        block, start, end = resolved
        if issue.block_hint is not None and issue.block_hint != block.block_id.root:
            raise EditorialReviewError("Editor issue block hint does not match the Draft")
        location = EditorialLocation(
            block_id=block.block_id,
            start=start,
            end=end,
            evidence_quote=issue.evidence_quote,
            occurrence=issue.occurrence,
        )
    elif issue.block_hint is not None:
        raise EditorialReviewError("Editor issue block hint requires an evidence quote")
    return EditorialIssue(
        issue_id=_stable_id(
            "editorial-issue",
            {"report_id": report_id.root, "index": index, "issue": issue.model_dump(mode="json")},
        ),
        issue_type=issue.issue_type,
        severity=issue.severity,
        description=issue.description,
        location=location,
        repairable=issue.repairable,
        structural=issue.structural,
    )


def _resolve_quote(
    text: str,
    blocks: tuple[_DraftBlock, ...],
    quote: str,
    occurrence: int,
) -> tuple[_DraftBlock, int, int] | None:
    start = -1
    for _ in range(occurrence + 1):
        start = text.find(quote, start + 1)
        if start < 0:
            return None
    end = start + len(quote)
    block = next(
        (item for item in blocks if item.start <= start and end <= item.end),
        None,
    )
    return None if block is None else (block, start, end)


def _draft_blocks(draft_id: ArtifactId, text: str) -> tuple[_DraftBlock, ...]:
    digest = draft_id.root.removeprefix("sha256:")[:32]
    blocks: list[_DraftBlock] = []
    cursor = 0
    for index, part in enumerate(text.split("\n\n")):
        start = cursor
        end = start + len(part)
        blocks.append(
            _DraftBlock(
                block_id=StableId(f"draft-block.{digest}.{index}"),
                start=start,
                end=end,
                text=part,
            )
        )
        cursor = end + 2
    return tuple(blocks)


def _changed_spans(draft_id: ArtifactId, original: str, repaired: str) -> tuple[DraftSpan, ...]:
    blocks = _draft_blocks(draft_id, original)
    matcher = difflib.SequenceMatcher(a=original, b=repaired, autojunk=False)
    raw: list[DraftSpan] = []
    for tag, start, end, _new_start, _new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        block = _block_at(blocks, start)
        raw.append(
            DraftSpan(
                block_id=block.block_id if block is not None else None,
                start=start,
                end=end,
            )
        )
    merged: list[DraftSpan] = []
    for span in raw:
        if merged and span.start <= merged[-1].end:
            prior = merged[-1]
            merged[-1] = DraftSpan(
                block_id=prior.block_id,
                start=prior.start,
                end=max(prior.end, span.end),
            )
        else:
            merged.append(span)
    return tuple(merged)


def _block_at(blocks: tuple[_DraftBlock, ...], offset: int) -> _DraftBlock | None:
    return next((block for block in blocks if block.start <= offset <= block.end), None)


def _span_inside(span: DraftSpan, allowed: Iterable[DraftSpan]) -> bool:
    return any(
        allowed_span.start <= span.start <= span.end <= allowed_span.end for allowed_span in allowed
    )


def _validate_repair_target(review_input: EditorialReviewInput, report: EditorialReport) -> None:
    if report.verdict is not EditorialVerdict.LOCAL_REPAIR:
        raise EditorialRepairError("only a frozen LOCAL_REPAIR report can be repaired")
    if report.draft_id != review_input.draft.draft_id:
        raise EditorialRepairError("repair report belongs to another Draft")
    if report.task_contract_id != review_input.writing_task.contract_id:
        raise EditorialRepairError("repair report belongs to another WritingTaskContract")
    if report.context_id != review_input.context.context_id:
        raise EditorialRepairError("repair report belongs to another Context snapshot")
    if report.base_commit != review_input.context.base_commit:
        raise EditorialRepairError("repair report belongs to another base commit")


def _stable_id(prefix: str, value: object) -> StableId:
    digest = content_id(value).root.removeprefix("sha256:")
    return StableId(f"{prefix}.{digest}")


__all__ = [
    "REPAIRED_TEXT_MEDIA_TYPE",
    "REWRITE_DIRECTIVE_MEDIA_TYPE",
    "EditorialRepairError",
    "EditorialReviewError",
    "EditorialService",
    "EditorialServiceError",
]
