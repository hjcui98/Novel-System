"""Trusted Editor review and bounded candidate-repair services."""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from novel_agent.agents.editor import EditorAgent
from novel_agent.agents.runner import AgentRunResult
from novel_agent.domain.editorial import (
    DraftSpan,
    EditorialConstraintSource,
    EditorialConstraintSourceKind,
    EditorialEvidenceScope,
    EditorialIssue,
    EditorialIssueDraft,
    EditorialLocation,
    EditorialRepairHistoryEntry,
    EditorialReport,
    EditorialReviewInput,
    EditorialVerdict,
    EditorReviewPayload,
    LocalRepairScope,
    RepairedDraft,
    editorial_issue_is_blocking,
)
from novel_agent.domain.generation import RewriteDirective, RewriteScope, WritingTaskContract
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.model_gateway import StructuredGenerationExhausted
from novel_agent.services.writer_cognition import candidate_surface_error

REPAIRED_TEXT_MEDIA_TYPE: Final[str] = "text/plain; charset=utf-8"
REWRITE_DIRECTIVE_MEDIA_TYPE: Final[str] = (
    "application/vnd.novel-agent.editor-rewrite-directive+json"
)
_EDITOR_CONTRACT_RETRY_SUFFIX = ".contract-retry1"
_EDITOR_CONTRACT_RETRY_FIELD = "host_contract_retry"
_ADMITTED_EDITOR_LENSES: Final[tuple[StableId, ...]] = (
    StableId("skill.editor.chapter-length"),
    StableId("skill.editor.plan-adherence-hook-payoff"),
    StableId("skill.editor.pacing-repetition"),
)
_PLAN_REVIEWER_LENSES: Final[tuple[StableId, ...]] = (
    StableId("skill.plan-review.temporal-obligation"),
    StableId("skill.plan-review.parent-scope"),
)


_EDITOR_LENS_FILES: Final[dict[str, str]] = {
    "skill.editor.chapter-length": "editor_chapter_length_v1.md",
    "skill.editor.plan-adherence-hook-payoff": "editor_plan_adherence_hook_payoff_v1.md",
    "skill.editor.pacing-repetition": "editor_pacing_repetition_v1.md",
}


def _selected_editor_lenses(
    review_input: EditorialReviewInput,
    *,
    prior_report: EditorialReport | None = None,
    draft_length: int | None = None,
) -> tuple[StableId, ...]:
    selected: list[StableId] = []
    policy = review_input.writing_task.length_policy
    if draft_length is not None:
        span = max(1, policy.maximum_characters - policy.minimum_characters)
        near = max(1, span // 10)
        if (
            draft_length < policy.minimum_characters
            or draft_length > policy.maximum_characters
            or draft_length <= policy.minimum_characters + near
            or draft_length >= policy.maximum_characters - near
        ):
            selected.append(_ADMITTED_EDITOR_LENSES[0])
    if (
        review_input.writing_task.active_plan_obligations
        or review_input.writing_task.forbidden_reveals
    ):
        selected.append(_ADMITTED_EDITOR_LENSES[1])
    if prior_report is not None or len(review_input.writing_task.required_beats) > 2:
        selected.append(_ADMITTED_EDITOR_LENSES[2])
    return tuple(dict.fromkeys(selected))[:3]


def _editor_lens_instructions(selected: tuple[StableId, ...]) -> str:
    root = Path(__file__).parents[1] / "skills"
    parts: list[str] = []
    for skill_id in selected:
        filename = _EDITOR_LENS_FILES[skill_id.root]
        text = (root / filename).read_text(encoding="utf-8")
        parts.append(f'<ADMITTED_LENS id="{skill_id.root}">\n{text}\n</ADMITTED_LENS>')
    return "\n\n".join(parts)


_EDITOR_CONTRACT_RETRY_INSTRUCTION = (
    "The previous Editor response was rejected by the host contract. Return one complete "
    "replacement JSON object. This is a report-contract repair, not a Writer revision: do not "
    "convert missing evidence, an unknown constraint source, or a self-contradictory report "
    "into MAJOR_REWRITE or PASS. Every blocking issue must name a visible WritingTask field "
    "or Context item and a conflict_reason. Quote-scoped issues need an exact contiguous "
    "evidence_quote from the supplied Draft; do not invent or paraphrase a quote. Whole-chapter "
    "absence or structure uses evidence_scope=chapter with no invented sentence quote. Keep "
    "unresolved_needs advisory and never use them as a substitute for the required fields. A "
    "contradiction, cross-chapter event, unsupported state change, invented character, "
    "backstory, item, or mechanism, and any internal inconsistency is an issue, not an "
    "unresolved_need. PASS is invalid while any such defect remains."
)
_EDITOR_NO_CHANGE_RETRY_SUFFIX = ".no-change-retry1"
_EDITOR_NO_CHANGE_RETRY_FIELD = "host_no_change_retry"
_EDITOR_NO_CHANGE_RETRY_REPETITION_PENALTY = 1.10
_EDITOR_NO_CHANGE_RETRY_INSTRUCTION = (
    "The previous LOCAL_REPAIR response returned the Draft unchanged, which is invalid. "
    "Return one complete replacement JSON object with repaired_text changed inside the "
    "frozen allowed spans. Preserve every character outside those spans exactly, make the "
    "smallest repair that addresses the blocking issue, and do not return the original "
    "Draft again."
)
_EDITOR_REPAIR_COMPLETENESS_FIELD = "host_repair_completeness"
_EDITOR_REPAIR_COMPLETENESS_INSTRUCTION = (
    "Address every blocking issue in the supplied issues list before returning. Do not stop "
    "after repairing the first issue: resolve each issue_id in its corresponding allowed "
    "span, while preserving every character outside the frozen allowed spans. Return one "
    "complete JSON object."
)
_EDITOR_REPAIR_BOUNDARY_FIELD = "host_repair_boundary_semantics"
_EDITOR_REPAIR_BOUNDARY_INSTRUCTION = (
    "An allowed span is a replacement boundary, not a fixed-length quota. Construct the "
    "complete result as draft_text[:start] + replacement_text + draft_text[end:] for each "
    "span: replacement_text may be longer or shorter than the supplied span, including an "
    "inserted sentence or paragraph. The returned repaired_text must contain the actual "
    "replacement; self_observations do not count as a repair."
)
_EDITOR_BOUNDARY_RETRY_FIELD = "host_out_of_scope_retry"
_EDITOR_BOUNDARY_RETRY_REPETITION_PENALTY = 1.10
_EDITOR_BOUNDARY_RETRY_INSTRUCTION = (
    "The previous LOCAL_REPAIR candidate changed characters outside every frozen allowed span. "
    "Discard that candidate. Return the complete Draft with every character before and after "
    "each allowed span byte-for-byte unchanged; replace only the supplied repair_scope_text."
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
    """Run independent review and one bounded local repair with one no-change retry."""

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
        payload = _review_payload(
            review_input,
            text,
            blocks,
            admitted_lenses=_selected_editor_lenses(review_input, draft_length=len(text)),
        )
        current_request = request
        current_payload: Mapping[str, object] = payload
        for attempt in range(2):
            try:
                run = await self._editor.review(
                    current_request,
                    current_payload,
                    source_hashes=(review_input.draft.text_artifact.artifact_id,),
                    input_artifacts=(review_input.draft.text_artifact,),
                    base_commit=review_input.context.base_commit,
                )
            except (StructuredGenerationExhausted, ValidationError) as error:
                if attempt == 0:
                    current_request = _editor_contract_retry_request(request)
                    current_payload = _editor_contract_retry_payload(payload)
                    continue
                raise EditorialReviewError(
                    f"Editor REVIEW failed without a report: {error}"
                ) from error
            except (ValueError, RuntimeError) as error:
                raise EditorialReviewError(
                    f"Editor REVIEW failed without a report: {error}"
                ) from error
            try:
                return self._build_report(
                    review_input,
                    review_input.draft.draft_id,
                    text,
                    blocks,
                    run,
                )
            except (EditorialReviewError, ValidationError) as error:
                if attempt == 0:
                    current_request = _editor_contract_retry_request(request)
                    current_payload = _editor_contract_retry_payload(payload)
                    continue
                raise EditorialReviewError(
                    "Editor REVIEW output did not form a valid report"
                ) from error
            except (ValueError, RuntimeError) as error:
                raise EditorialReviewError(
                    "Editor REVIEW output did not form a valid report"
                ) from error
        raise AssertionError("Editor contract retry loop did not terminate")  # pragma: no cover

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
            admitted_lenses=_selected_editor_lenses(
                review_input,
                prior_report=repair_report,
                draft_length=len(text),
            ),
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
        current_request = request
        current_payload: Mapping[str, object] = payload
        for attempt in range(3):
            try:
                run = await self._editor.local_repair(
                    current_request,
                    current_payload,
                    source_hashes=(review_input.draft.text_artifact.artifact_id,),
                    input_artifacts=(review_input.draft.text_artifact,),
                    base_commit=review_input.context.base_commit,
                )
            except (ValidationError, ValueError, RuntimeError) as error:
                raise EditorialRepairError(
                    "Editor LOCAL_REPAIR failed without a candidate"
                ) from error

            proposed_text = run.output.repaired_text
            proposed_spans = _changed_spans(
                review_input.draft.draft_id,
                original,
                proposed_text,
            )
            repaired_text = _project_repair_to_allowed_spans(
                original,
                proposed_text,
                scope.allowed_spans,
            )
            changed_spans = _changed_spans(review_input.draft.draft_id, original, repaired_text)
            if (
                changed_spans
                and all(_span_inside(span, scope.allowed_spans) for span in changed_spans)
                and _all_required_spans_changed(changed_spans, scope.allowed_spans)
            ):
                break
            if not changed_spans:
                if proposed_spans and attempt < 2:
                    current_request = _editor_boundary_retry_request(request, attempt + 1)
                    current_payload = _editor_boundary_retry_payload(payload)
                    continue
                if not proposed_spans and attempt == 0:
                    current_request = _editor_no_change_retry_request(request)
                    current_payload = _editor_no_change_retry_payload(payload)
                    continue
                if proposed_spans:
                    raise EditorialRepairError("LOCAL_REPAIR changed text outside its frozen scope")
                raise EditorialRepairError("LOCAL_REPAIR produced no text change")
            if attempt < 2:
                current_request = _editor_boundary_retry_request(request, attempt + 1)
                current_payload = _editor_boundary_retry_payload(
                    payload,
                    require_complete=True,
                )
                continue
            raise EditorialRepairError("LOCAL_REPAIR did not safely change every frozen issue span")
        else:  # pragma: no cover - the bounded loop always returns or raises above
            raise AssertionError("Editor LOCAL_REPAIR retry loop did not terminate")
        surface_error = candidate_surface_error(
            repaired_text,
            length_policy=review_input.writing_task.length_policy,
            forbidden_reveals=review_input.writing_task.forbidden_reveals,
        )
        if surface_error is not None:
            raise EditorialRepairError(f"LOCAL_REPAIR failed surface checks: {surface_error}")
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
                "task_contract_id": review_input.writing_task.contract_id.root,
                "snapshot_id": review_input.context.snapshot_id.root,
                "context_id": review_input.context.context_id.root,
                "text_artifact": text_artifact.model_dump(mode="json"),
            }
        )
        try:
            return RepairedDraft(
                draft_id=repaired_id,
                parent_draft_id=review_input.draft.draft_id,
                repair_report_id=report.report_id,
                task_contract_id=review_input.writing_task.contract_id,
                snapshot_id=review_input.context.snapshot_id,
                context_id=review_input.context.context_id,
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
            _materialize_issue(
                report_id,
                index,
                issue,
                text,
                blocks,
                review_input=review_input,
            )
            for index, issue in enumerate(payload.issues)
        )
        repair_scope: LocalRepairScope | None = None
        rewrite_directive: RewriteDirective | None = None
        receipt = run.receipt
        verdict = payload.verdict
        if verdict is EditorialVerdict.LOCAL_REPAIR:
            blocking = tuple(
                issue
                for issue in issues
                if editorial_issue_is_blocking(
                    repairable=issue.repairable,
                    structural=issue.structural,
                    severity=issue.severity,
                )
            )
            if not blocking:
                # A model can over-route an advisory warning as LOCAL_REPAIR. It has no
                # trusted repair scope, but it is still useful to the Writer as a nonblocking
                # report. Keep the warning and its unresolved markers while normalizing the
                # routing verdict so the report can continue through the pipeline.
                verdict = EditorialVerdict.PASS
            elif any(issue.location is None for issue in blocking):
                raise EditorialReviewError(
                    "LOCAL_REPAIR requires a concrete trusted location for every blocking issue"
                )
            else:
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
        elif verdict is EditorialVerdict.MAJOR_REWRITE:
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
            verdict=verdict,
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
    admitted_lenses: tuple[StableId, ...] | None = None,
) -> Mapping[str, object]:
    lenses = admitted_lenses or ()
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
        "paragraph_stats": _paragraph_stats(text),
        "prior_issues": _prior_issues_for_payload(review_input, prior_repair_history),
        "re_review_instructions": (
            "Review the complete current candidate. Prior issues are only for checking "
            "whether they closed; also re-check paragraphs, chapter goals, capability "
            "bounds, repeated progression, and the full base checklist. Merge overlapping "
            "repair spans; keep original reports and the mapping instead of deleting findings."
            if (prior_repair_history or review_input.prior_repair_history)
            else None
        ),
        "admitted_lenses": [item.root for item in lenses],
        "lens_instructions": _editor_lens_instructions(lenses),
        "base_review_checklist": (
            "执行当前 ChapterGoal、beats、state changes、参与实体和 obligation actions",
            "检查人物/世界状态、POV/人称、叙事时间、时间锁、揭示边界及 Profile 语言/题材/风格",
            "对照上一章完整正文与最近3至5章的 goal/summary/beats，检查直接复写和结构循环",  # noqa: RUF001
            "检查模板化连接、过度解释、说明化对话、整章单段和题材漂移",
            "判断问题属于 LOCAL_REPAIR、MAJOR_REWRITE，还是 accepted Plan 需要人工 replan",  # noqa: RUF001
        ),
        "writer_surface_checks": (
            "正文非空且符合 WritingTask 长度区间",
            "无内部规划标记、章节标签、替换字符或明显非目标语言段落",
            "无近期正文长段复制，且保留换行和自然段边界",  # noqa: RUF001
        ),
    }


def _editor_contract_retry_request(request: ModelRequest) -> ModelRequest:
    suffix = _EDITOR_CONTRACT_RETRY_SUFFIX
    request_id = request.request_id.root[: 128 - len(suffix)] + suffix
    return request.model_copy(update={"request_id": StableId(request_id)})


def _editor_contract_retry_payload(
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    return {
        **payload,
        _EDITOR_CONTRACT_RETRY_FIELD: _EDITOR_CONTRACT_RETRY_INSTRUCTION,
    }


def _editor_no_change_retry_request(request: ModelRequest) -> ModelRequest:
    suffix = _EDITOR_NO_CHANGE_RETRY_SUFFIX
    request_id = request.request_id.root[: 128 - len(suffix)] + suffix
    return request.model_copy(
        update={
            "request_id": StableId(request_id),
            "repetition_penalty": _EDITOR_NO_CHANGE_RETRY_REPETITION_PENALTY,
        }
    )


def _editor_no_change_retry_payload(
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    return {
        **payload,
        _EDITOR_NO_CHANGE_RETRY_FIELD: _EDITOR_NO_CHANGE_RETRY_INSTRUCTION,
    }


def _editor_boundary_retry_request(request: ModelRequest, attempt: int) -> ModelRequest:
    suffix = f".boundary-retry{attempt}"
    request_id = request.request_id.root[: 128 - len(suffix)] + suffix
    return request.model_copy(
        update={
            "request_id": StableId(request_id),
            "repetition_penalty": _EDITOR_BOUNDARY_RETRY_REPETITION_PENALTY,
        }
    )


def _editor_boundary_retry_payload(
    payload: Mapping[str, object],
    *,
    require_complete: bool = False,
) -> Mapping[str, object]:
    retry: dict[str, object] = {
        **payload,
        _EDITOR_BOUNDARY_RETRY_FIELD: _EDITOR_BOUNDARY_RETRY_INSTRUCTION,
    }
    if require_complete:
        retry[_EDITOR_REPAIR_COMPLETENESS_FIELD] = (
            _EDITOR_REPAIR_COMPLETENESS_INSTRUCTION
            + " The previous candidate left at least one allowed issue span unchanged; "
            "this retry must make an actual repair inside every listed allowed span."
        )
    return retry


def _repair_payload(
    review_input: EditorialReviewInput,
    report: EditorialReport,
    text: str,
    blocks: tuple[_DraftBlock, ...],
) -> Mapping[str, object]:
    scope = report.repair_scope
    if scope is None:  # pragma: no cover - protected by EditorialReport validation
        raise EditorialRepairError("LOCAL_REPAIR report has no repair scope")
    return {
        "draft_id": review_input.draft.draft_id.root,
        "repair_scope": scope,
        _EDITOR_REPAIR_COMPLETENESS_FIELD: _EDITOR_REPAIR_COMPLETENESS_INSTRUCTION,
        _EDITOR_REPAIR_BOUNDARY_FIELD: _EDITOR_REPAIR_BOUNDARY_INSTRUCTION,
        "repair_scope_text": tuple(
            {
                "block_id": span.block_id.root if span.block_id is not None else None,
                "start": span.start,
                "end": span.end,
                "text": text[span.start : span.end],
            }
            for span in scope.allowed_spans
        ),
        "issues": report.issues,
        "writing_task": review_input.writing_task.model_dump(mode="json"),
        "context_summary": _context_summary(review_input),
        "draft_blocks": tuple(
            {"block_id": block.block_id.root, "text": block.text} for block in blocks
        ),
        "draft_text": text,
        "base_review_checklist": (
            "执行当前 ChapterGoal、beats、state changes、参与实体和 obligation actions",
            "检查人物/世界状态、POV/人称、叙事时间、时间锁、揭示边界及 Profile 语言/题材/风格",
            "对照上一章完整正文与最近3至5章的 goal/summary/beats，检查直接复写和结构循环",  # noqa: RUF001
            "检查模板化连接、过度解释、说明化对话、整章单段和题材漂移",
            "判断问题属于 LOCAL_REPAIR、MAJOR_REWRITE，还是 accepted Plan 需要人工 replan",  # noqa: RUF001
        ),
        "writer_surface_checks": (
            "正文非空且符合 WritingTask 长度区间",
            "无内部规划标记、章节标签、替换字符或明显非目标语言段落",
            "无近期正文长段复制，且保留换行和自然段边界",  # noqa: RUF001
        ),
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


def _paragraph_stats(text: str) -> dict[str, int]:
    paragraphs = tuple(part for part in text.split("\n\n") if part.strip())
    return {
        "character_count": len(text),
        "paragraph_count": len(paragraphs),
        "newline_count": text.count("\n"),
    }


def _prior_issues_for_payload(
    review_input: EditorialReviewInput,
    prior_repair_history: tuple[EditorialRepairHistoryEntry, ...] | None,
) -> tuple[dict[str, object], ...]:
    history = (
        review_input.prior_repair_history if prior_repair_history is None else prior_repair_history
    )
    return tuple(
        {
            "report_id": entry.report_id.root,
            "draft_id": entry.draft_id.root,
            "verdict": entry.verdict.value,
            "repaired_draft_id": (
                None if entry.repaired_draft_id is None else entry.repaired_draft_id.root
            ),
        }
        for entry in history
    )


_WRITING_TASK_CONSTRAINT_FIELDS = frozenset(
    {
        "pov",
        "narrative_person",
        "chapter_goal",
        "scene_goals",
        "required_beats",
        "active_plan_obligations",
        "mandatory_constraints",
        "forbidden_reveals",
        "preserve_requirements",
        "style_requirements",
        "participating_entity_ids",
        "obligation_actions",
        "blocking_gaps",
        "length_policy",
    }
)


def _writing_task_field_has_requirement(task: WritingTaskContract, field_name: str) -> bool:
    if field_name not in _WRITING_TASK_CONSTRAINT_FIELDS:
        return False
    value = getattr(task, field_name)
    if field_name == "length_policy":
        return value is not None
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, tuple):
        return any(str(item).strip() for item in value)
    return False


def _visible_context_requirements(review_input: EditorialReviewInput) -> dict[str, str]:
    requirements: dict[str, str] = {}
    for item in _context_summary(review_input):
        item_id = item.get("item_id")
        text = item.get("text")
        if isinstance(item_id, str) and isinstance(text, str) and text.strip():
            requirements[item_id] = text
    return requirements


def _validate_constraint_source(
    issue: EditorialIssueDraft,
    review_input: EditorialReviewInput,
) -> EditorialConstraintSource | None:
    source = issue.constraint_source
    blocking = editorial_issue_is_blocking(
        repairable=issue.repairable,
        structural=issue.structural,
        severity=issue.severity,
    )
    if source is None:
        if blocking:
            raise EditorialReviewError("blocking Editor issue is missing a constraint source")
        return None
    if source.kind is EditorialConstraintSourceKind.CHAPTER_SCOPE:
        raise EditorialReviewError(
            "Editor constraint source cannot be chapter_scope; "
            "name a WritingTask field or visible Context item"
        )
    if source.kind is EditorialConstraintSourceKind.WRITING_TASK_FIELD:
        field_name = source.field_name
        if field_name is None or not _writing_task_field_has_requirement(
            review_input.writing_task, field_name
        ):
            raise EditorialReviewError(
                "Editor constraint source does not name an effective WritingTask requirement"
            )
    elif source.kind is EditorialConstraintSourceKind.CONTEXT_ITEM:
        item_id = None if source.context_item_id is None else source.context_item_id.root
        if item_id is None or item_id not in _visible_context_requirements(review_input):
            raise EditorialReviewError(
                "Editor constraint source is not a visible non-empty Context item"
            )
    return source


def _materialize_issue(
    report_id: StableId,
    index: int,
    issue: EditorialIssueDraft,
    text: str,
    blocks: tuple[_DraftBlock, ...],
    *,
    review_input: EditorialReviewInput,
) -> EditorialIssue:
    location: EditorialLocation | None = None
    constraint_source = _validate_constraint_source(issue, review_input)
    if issue.evidence_scope is EditorialEvidenceScope.CHAPTER:
        location = EditorialLocation(
            start=0,
            end=len(text),
        )
    elif issue.evidence_quote is not None:
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
    elif editorial_issue_is_blocking(
        repairable=issue.repairable,
        structural=issue.structural,
        severity=issue.severity,
    ):
        raise EditorialReviewError("blocking Editor issue is missing Draft evidence")
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
        constraint_source=constraint_source,
        evidence_scope=issue.evidence_scope,
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


def _project_repair_to_allowed_spans(
    original: str,
    proposed: str,
    allowed_spans: Iterable[DraftSpan],
) -> str:
    """Apply only model-proposed edits wholly contained by a frozen repair span."""

    allowed = tuple(allowed_spans)
    matcher = difflib.SequenceMatcher(a=original, b=proposed, autojunk=False)
    replacements: list[tuple[int, int, str]] = []
    for tag, start, end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if any(span.start <= start <= end <= span.end for span in allowed):
            replacements.append((start, end, proposed[new_start:new_end]))
    projected = original
    for start, end, replacement in reversed(replacements):
        projected = projected[:start] + replacement + projected[end:]
    return projected


def _block_at(blocks: tuple[_DraftBlock, ...], offset: int) -> _DraftBlock | None:
    return next((block for block in blocks if block.start <= offset <= block.end), None)


def _span_inside(span: DraftSpan, allowed: Iterable[DraftSpan]) -> bool:
    return any(
        allowed_span.start <= span.start <= span.end <= allowed_span.end for allowed_span in allowed
    )


def _all_required_spans_changed(
    changed: Iterable[DraftSpan],
    allowed: Iterable[DraftSpan],
) -> bool:
    """Require observable repair work for every independently reviewed issue span."""

    changed_spans = tuple(changed)
    return all(
        any(
            (
                change.start <= target.start <= change.end
                if target.start == target.end
                else change.start < target.end and target.start < change.end
            )
            for change in changed_spans
        )
        for target in allowed
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
