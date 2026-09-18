"""Thin Stage 4 inquiry -> Memory -> Plan -> independent review orchestration."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import TypeVar, cast

from pydantic import BaseModel, JsonValue

from novel_agent.agents.plan_reviewer import PlanReviewerAgent, PlanReviewerInvocationError
from novel_agent.agents.planner import (
    PlannerAgent,
    PlannerInvocationError,
    question_boundary_semantics,
)
from novel_agent.agents.runner import AgentExecutionError
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.creative_runtime import (
    OPERATOR_PLAN_REVIEW_MEDIA_TYPE,
    OperatorReviewEvidence,
)
from novel_agent.domain.ids import SchemaVersion, StableId, bounded_stable_id
from novel_agent.domain.memory import (
    OBLIGATION_OWNER_SEMANTICS,
    ContextBudgetReport,
    FacetClosureStatus,
    RetrievalTrace,
    Stage1ContextPackage,
    Stage1MemoryNeed,
    WorldRootDocument,
)
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.plan_composition import (
    PlanCompositionError,
    blocking_issue_identity,
    build_composition_proof,
    compose_scoped_revision,
    issue_identity_seed,
    operator_revision_scope,
    out_of_scope_items,
    progress_against,
    revision_scope,
)
from novel_agent.domain.plan_composition import (
    seed_identity as _seed_identity,
)
from novel_agent.domain.planning import (
    PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
    PlannerContextPackage,
    PlanningInquiry,
    PlanningLoopCheckpoint,
    PlanningLoopEventReceipt,
    PlanningLoopPhase,
    PlanningLoopRequest,
    PlanningLoopResult,
    PlanningLoopTerminal,
    PlanningProblemIdentitySeed,
    PlanningProvenance,
    PlanningQuestion,
    PlanningQuestionKind,
    PlanningReference,
    PlanningTurnAction,
    PlanReview,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
)
from novel_agent.domain.planning_gap import TrustedQuestionIntent
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentMode,
    ControllerStopReason,
    MemoryGatewayResult,
    MemoryResolutionRequest,
    PlannerExecutionResult,
    PlanProposal,
    PlanUnresolvedIssue,
    PlanUnresolvedOperation,
    PlanUnresolvedOperationRecord,
    RequiredSnapshotPolicy,
)
from novel_agent.ports.model_endpoint import ModelEndpointError
from novel_agent.ports.planning_context import (
    PlannerContextRuntimeFailure,
    PlannerContextRuntimePort,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.loop_round_progress import planner_round_progress
from novel_agent.services.memory_gateway import MemoryGateway, MemoryGatewayBlockedError
from novel_agent.services.model_gateway import (
    ModelCallCumulativeBudgetExceeded,
    ModelCallForbiddenError,
    ModelOutputBudgetExhausted,
    ModelRoutingError,
    StructuredGenerationExhausted,
)
from novel_agent.services.planner_context_assembler import (
    PlannerContextAssembler,
    PlannerContextAssemblyError,
)
from novel_agent.services.planning_inquiry_need_generation import (
    PlanningInquiryConditionedNeedGenerator,
    PlanningInquiryNeedError,
)
from novel_agent.services.retrieval import ROUTES

ModelRequestFactory = Callable[[str, AgentMode, int], ModelRequest]
ModelT = TypeVar("ModelT", bound=BaseModel)

CONTROLLED_REVISION_EVIDENCE_MEDIA_TYPE = (
    "application/vnd.novel-agent.controlled-revision-evidence+json"
)
_ARC_RESPONSIBILITY_CONTEXT_FIELDS = (
    "protagonist_arc",
    "supporting_arc",
    "faction_arc",
    "obligation_plan",
)
_MAX_CONTROLLED_REVISION_ENTITIES = 128
_MAX_CONTROLLED_REVISION_STATES = 64
_MAX_CONTROLLED_REVISION_RELATIONS = 64


def _planner_memory_question_chunk_size(request: PlanningLoopRequest) -> int:
    """Keep one planner-memory tranche from starving its final Need.

    The production legacy controller has one bounded retrieval call budget for
    the whole Need set. A registered route may consume every primary and
    conditional-fallback channel before it can close, so fan-out must be
    carried as pending questions across planner-memory rounds instead of
    letting the first batch exhaust the shared tranche.
    """

    retrieval = request.budgets.retrieval
    max_registered_route_steps = max(
        len(route.channels) + len(route.fallback_channels) for route in ROUTES.values()
    )
    per_need_call_budget = max(retrieval.max_rounds, max_registered_route_steps)
    return max(1, retrieval.max_tool_calls // max(1, per_need_call_budget))


_PLANNER_MEMORY_QUESTION_MARKS = ("【", "】", "《", "》", "「", "」", "[", "]")


def _canonical_planner_memory_question(question: str) -> str:
    """Strip cosmetic name marks so a rephrase is not a new Memory problem.

    After a SUPPORTED reprompt the Planner may wrap the same names in 【】.
    Those marks must not mint a new question set, or the loop aborts with
    ``PLANNER_MEMORY_NO_PROGRESS`` instead of the bounded PLAN_READY fallback.
    """

    stripped = question.casefold().strip()
    for mark in _PLANNER_MEMORY_QUESTION_MARKS:
        stripped = stripped.replace(mark, "")
    return " ".join(stripped.split())


def _planner_memory_question_id(inquiry_id: StableId, question: str) -> StableId:
    return StableId(
        "planner-memory."
        + content_id(
            {
                "inquiry": inquiry_id.root,
                "question": question,
            }
        ).root[-48:]
    )


def _planner_memory_questions(
    inquiry: PlanningInquiry,
    questions: tuple[str, ...],
    problem_identity_seed: PlanningProblemIdentitySeed | None = None,
) -> tuple[PlanningQuestion, ...]:
    if problem_identity_seed is not None:
        seeded = tuple(
            question
            for question in (*inquiry.assumptions, *inquiry.questions)
            if question.question_id == problem_identity_seed.question_id
        )
        if len(seeded) != 1 or seeded[0].question.strip() != problem_identity_seed.need_query:
            raise PlanningInquiryNeedError(
                "problem identity seed question is not present in the Planner inquiry"
            )
        # A pre-registered problem identity is an execution boundary.  The
        # model may ask a narrower follow-up question after the initial Memory
        # turn, but that follow-up must remain the same durable problem for
        # U8-C split comparability.  Reuse the reviewed question identity
        # instead of minting a planner-memory id from model text.
        return (seeded[0].model_copy(update={"blocking": True}),)
    # The model spent a turn asking Memory for prior evidence.  That is a
    # history request at every planning level, so it can never be relabelled as
    # future design to escape an evidence check.
    purpose, expectation = question_boundary_semantics(
        mode=inquiry.mode,
        intent=TrustedQuestionIntent.REQUESTED_MEMORY_EVIDENCE,
        kind=PlanningQuestionKind.FACT,
        blocking=True,
    )
    return tuple(
        PlanningQuestion(
            question_id=_planner_memory_question_id(inquiry.inquiry_id, question),
            kind=PlanningQuestionKind.FACT,
            question=question,
            provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
            goal_id=inquiry.goal_proposals[0].goal_id,
            blocking=True,
            question_purpose=purpose,
            dependency_expectation=expectation,
        )
        for question in questions
    )


def _requested_planner_memory_question_ids(
    questions: tuple[str, ...],
    inquiry: PlanningInquiry,
    problem_identity_seed: PlanningProblemIdentitySeed | None,
) -> tuple[StableId, ...]:
    """Bind a mixed model Memory request back to the pre-registered problem.

    A seeded inquiry may still cause the model to repeat the durable question
    alongside unrelated follow-ups.  The durable question is already handled
    by the first retrieval tranche, so mapping that request to the seed lets
    the existing bounded supported-memory reprompt author a plan.  Unrelated
    questions remain ordinary planner-memory requests and therefore fail closed
    instead of being silently merged into the experiment identity.
    """

    if problem_identity_seed is not None and any(
        question.strip() == problem_identity_seed.need_query for question in questions
    ):
        return (problem_identity_seed.question_id,)
    return tuple(
        _planner_memory_question_id(inquiry.inquiry_id, question) for question in questions
    )


def _supported_memory_reprompt_payload(
    rendered_context: str,
    supported_questions: tuple[str, ...],
) -> str:
    details = " | ".join(supported_questions)
    return (
        f"{rendered_context}\n\n"
        "PLANNER_MEMORY_STATUS=SUPPORTED. The Planner Context above already contains "
        "supported mandatory evidence for every Memory question from the preceding turn. "
        "Use that existing evidence and return PLAN_READY; do not request those supported "
        "facts again. SUPPORTED_MEMORY_QUESTIONS="
        f"{details}"
    )


def _rejected_memory_reprompt_payload(
    rendered_context: str,
    rejected_questions: tuple[tuple[str, str], ...],
) -> str:
    details = " | ".join(f"{question} [{reason}]" for question, reason in rejected_questions)
    return (
        f"{rendered_context}\n\n"
        "PLANNER_MEMORY_STATUS=UNEXECUTABLE_REQUESTS. The preceding Memory requests listed "
        "below could not be grounded or compiled from the exact WORLD_ENTITY_LABELS. Use the "
        "existing Planner Context and return PLAN_READY; do not repeat these requests or "
        "invent aliases. REJECTED_MEMORY_QUESTIONS="
        f"{details}"
    )


def _unsupported_memory_reprompt_payload(
    rendered_context: str,
    unresolved_questions: Sequence[Sequence[object]],
) -> str:
    details = " | ".join(
        f"{question} [{', '.join(facets)}]"
        for _question_id, question, facets in _memory_gap_parts(unresolved_questions)
    )
    return (
        f"{rendered_context}\n\n"
        "PLANNER_MEMORY_STATUS=UNSUPPORTED_CONTENT. The preceding Memory request has "
        "mandatory facets without direct cutoff-valid evidence. Keep those facets "
        "unresolved; do not infer a reaction, causal event, or relationship that the "
        "evidence does not state. This is the final bounded content-recovery turn: use "
        "the supported Planner Context and return PLAN_READY with explicit unresolved "
        "gaps; do not issue new memory_questions, infer unsupported facts, or repeat "
        "the same request. "
        "UNSUPPORTED_MEMORY_QUESTIONS="
        f"{details}"
    )


def _unsupported_memory_gap_markers(
    unresolved_questions: Sequence[Sequence[object]],
) -> tuple[str, ...]:
    return tuple(
        f"Planner Memory remains unresolved for {question} ({', '.join(facets)})."
        for _question_id, question, facets in _memory_gap_parts(unresolved_questions)
    )


def _memory_gap_parts(
    unresolved_questions: Sequence[Sequence[object]],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """Normalize legacy ``(question, facets)`` and identified gap tuples."""

    parts: list[tuple[str, str, tuple[str, ...]]] = []
    for raw in unresolved_questions:
        if len(raw) == 3 and all(isinstance(value, str) for value in raw[:2]):
            question_id, question, facets_raw = raw
        elif len(raw) == 2 and isinstance(raw[0], str):
            question = raw[0]
            facets_raw = raw[1]
            question_id = (
                "memory-gap."
                + content_id({"question": question, "facets": facets_raw}).root.removeprefix(
                    "sha256:"
                )[:24]
            )
        else:
            raise ValueError("unsupported Memory gap detail must identify a question")
        if not isinstance(question_id, str) or not isinstance(question, str):
            raise ValueError("unsupported Memory gap detail has invalid question identity")
        if not isinstance(facets_raw, (tuple, list)) or not all(
            isinstance(facet, str) for facet in facets_raw
        ):
            raise ValueError("unsupported Memory gap detail has invalid facets")
        parts.append((question_id, question, tuple(facets_raw)))
    return tuple(parts)


def _has_evidence_bound_unsupported_gap(
    context: Stage1ContextPackage,
    text_root: TextRootDocument,
) -> bool:
    """Return true only for an unsupported mandatory facet with source candidates.

    A pure graph-zero/L0 fallback remains read-only. A selected source
    candidate, however, is the evidence-bound shape that the U8-B maintenance
    handoff is allowed to repair; it must not be converted into a writable Plan
    candidate by the bounded unresolved-content fallback. Before chapter one,
    the canonical TextRoot contains no chapter source for Curator to extract.
    World/Plan anchors may still be retrieved at Genesis, but those candidates
    cannot justify a chapter-zero maintenance write and must use the existing
    read-only unsupported-content fallback.
    """

    if not text_root.chapters:
        return False
    return any(
        bool(trace.candidates)
        and any(
            receipt.mandatory and receipt.status is not FacetClosureStatus.SUPPORTED
            for receipt in trace.facet_receipts
        )
        for trace in context.retrieval_traces
    )


_CHAPTER_RANGE_RE = re.compile(r"^\s*(?P<start>\d+)\s*[-~至到]\s*(?P<end>\d+)\s*$")


def _chapter_window_from_plan_item(item: object) -> tuple[int, ...]:
    """Read an explicit unresolved-item chapter window, never an inferred horizon.

    Story and ARC_VOLUME requests intentionally have no execution horizon.  A plan
    item may still carry a source-bound ``chapter_range``; that declaration is the
    only valid full-scope binding for a retained Memory gap.  Unknown or malformed
    values remain empty so the host gate can stop rather than invent a range.
    """

    if not isinstance(item, dict):
        return ()
    raw = item.get("chapter_range")
    if (
        isinstance(raw, (list, tuple))
        and len(raw) == 2
        and all(type(value) is int for value in raw)
    ):
        start, end = raw
    elif isinstance(raw, str):
        match = _CHAPTER_RANGE_RE.fullmatch(raw)
        if match is None:
            return ()
        start, end = int(match.group("start")), int(match.group("end"))
    else:
        start = item.get("chapter_start")
        end = item.get("chapter_end")
        if type(start) is not int or type(end) is not int:
            return ()
    if start < 1 or end < start:
        return ()
    return tuple(range(start, end + 1))


def _memory_question_stem(question: str) -> str:
    normalized = _canonical_planner_memory_question(question)
    normalized = re.sub(r"(?:是什么|为何|为什么)[?\uFF1F]?$", "", normalized).strip()
    return normalized


def _proposal_memory_gap_bindings(
    proposal: PlanProposal,
    unresolved_questions: Sequence[Sequence[object]],
) -> tuple[dict[str, tuple[int, ...]], dict[str, tuple[StableId, ...]]]:
    """Bind Memory gaps to explicit Planner unresolved items in the same proposal.

    The binding is text/source evidence, not a model assertion: only an exact
    question stem contained in one unresolved item's title/summary is accepted, and
    that item must carry a parseable chapter window.  Distinct questions remain
    distinct even when their windows happen to overlap.
    """

    details = _memory_gap_parts(unresolved_questions)
    candidate_rows: list[tuple[str, tuple[int, ...], tuple[StableId, ...]]] = []
    unresolved_by_summary = {
        issue.summary: issue for issue in proposal.unresolved if issue.summary.strip()
    }
    for item in proposal.items:
        payload = item.payload
        title = payload.get("title")
        description = payload.get("description")
        searchable = " ".join(
            value.strip()
            for value in (title, description)
            if isinstance(value, str) and value.strip()
        )
        if not searchable:
            continue
        window = _chapter_window_from_plan_item(payload)
        if not window:
            continue
        source_ids: tuple[StableId, ...] = tuple(item.source_ids)
        issue = unresolved_by_summary.get(str(title)) if isinstance(title, str) else None
        if issue is not None:
            source_ids = tuple(dict.fromkeys((*source_ids, *issue.source_ids)))
        candidate_rows.append((searchable, window, source_ids))

    scopes: dict[str, tuple[int, ...]] = {}
    sources: dict[str, tuple[StableId, ...]] = {}
    for _question_id, question, _facets in details:
        stem = _memory_question_stem(question)
        matches = [row for row in candidate_rows if stem and stem in row[0]]
        if len(matches) != 1:
            continue
        _searchable, window, source_ids = matches[0]
        scopes[question] = window
        if source_ids:
            sources[question] = source_ids
    return scopes, sources


def _retain_unsupported_memory_gaps(
    result: PlannerExecutionResult,
    unresolved_questions: Sequence[Sequence[object]],
    *,
    affected_chapters: tuple[int, ...] = (),
    scope_by_question: Mapping[str, tuple[int, ...]] | None = None,
    source_ids_by_question: Mapping[str, tuple[StableId, ...]] | None = None,
    source_artifact_refs: tuple[ArtifactRef, ...] = (),
) -> PlannerExecutionResult:
    """Carry an unsupported but relevant Memory gap without treating it as a fact."""

    details = _memory_gap_parts(unresolved_questions)
    markers = _unsupported_memory_gap_markers(unresolved_questions)
    if not markers:
        return result
    derived_scopes, derived_sources = _proposal_memory_gap_bindings(
        result.plan_proposal,
        unresolved_questions,
    )
    effective_scopes = {**derived_scopes, **(scope_by_question or {})}
    effective_sources = {**derived_sources, **(source_ids_by_question or {})}
    existing = {issue.summary for issue in result.plan_proposal.unresolved}
    existing_sources = {
        source.root for issue in result.plan_proposal.unresolved for source in issue.source_ids
    }
    added = tuple(
        PlanUnresolvedIssue(
            issue_id=bounded_stable_id(f"plan-issue.memory-gap.{question_id}"),
            summary=marker,
            blocking=False,
            resolution_owner="MEMORY",
            affected_chapters=(
                effective_scopes.get(question, affected_chapters)
                if effective_scopes
                else affected_chapters
            ),
            source_ids=tuple(
                dict.fromkeys(
                    (
                        StableId(question_id),
                        *(effective_sources.get(question, ())),
                    )
                )
            ),
            source_artifact_refs=source_artifact_refs,
            forbidden_assumptions=("不得把该未决记忆缺口当作已证实事实",),
        )
        for (question_id, question, _facets), marker in zip(details, markers, strict=True)
        if marker not in existing and question_id not in existing_sources
    )
    operations = tuple(
        PlanUnresolvedOperationRecord(
            operation=PlanUnresolvedOperation.ADD,
            issue_id=issue.issue_id,
            kind=issue.kind,
            summary=issue.summary,
            affected_chapters=issue.affected_chapters,
            blocking=issue.blocking,
            resolution_owner=issue.resolution_owner,
            allowed_assumptions=issue.allowed_assumptions,
            forbidden_assumptions=issue.forbidden_assumptions,
            source_ids=issue.source_ids,
            source_artifact_refs=issue.source_artifact_refs,
        )
        for issue in added
    )
    proposal = result.plan_proposal.model_copy(
        update={
            "unresolved": (*result.plan_proposal.unresolved, *added),
            "unresolved_operations": (
                *result.plan_proposal.unresolved_operations,
                *operations,
            ),
        }
    )
    return result.model_copy(update={"plan_proposal": proposal})


def _reviewer_memory_gap_advisory(
    review: PlanReview,
    *,
    proposal_ref: ArtifactRef,
    source_review_ref: ArtifactRef,
    markers: tuple[str, ...],
) -> PlanReview:
    """Carry an evidence-free but relevant reviewer gap as an explicit advisory."""

    receipt = review.receipt.model_copy(
        update={
            "receipt_id": StableId(f"{review.receipt.receipt_id.root}.memory-advisory"[:128]),
            "input_artifacts": tuple(
                dict.fromkeys((*review.receipt.input_artifacts, source_review_ref, proposal_ref))
            ),
            "unresolved": tuple(dict.fromkeys((*review.receipt.unresolved, *markers))),
        }
    )
    identity = content_id(
        {
            "source_review": source_review_ref.artifact_id.root,
            "proposal": proposal_ref.artifact_id.root,
            "markers": markers,
        }
    ).root.removeprefix("sha256:")[:24]
    return review.model_copy(
        update={
            "review_id": StableId(f"plan-review.memory-advisory.{identity}"[:128]),
            "target_artifact_ref": proposal_ref,
            "decision": ReviewDecision.ACCEPT,
            "issues": tuple(
                issue.model_copy(update={"blocking": False})
                if issue.kind is ReviewIssueKind.MEMORY_GAP
                else issue
                for issue in review.issues
            ),
            "revision_instruction": None,
            "receipt": receipt,
        }
    )


def _unsupported_memory_question_details(
    selected_question_ids: tuple[StableId, ...],
    needs: tuple[Stage1MemoryNeed, ...],
    traces: tuple[RetrievalTrace, ...],
    question_by_id: dict[StableId, str],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    by_need = {trace.need_id: trace for trace in traces}
    grouped: dict[StableId, list[Stage1MemoryNeed]] = {}
    for need in needs:
        if need.planned_draft_id:
            grouped.setdefault(StableId(need.planned_draft_id), []).append(need)
    if grouped:
        question_needs = tuple(
            (question_id, tuple(grouped[question_id]))
            for question_id in selected_question_ids
            if question_id in grouped
        )
    elif len(selected_question_ids) == len(needs):
        question_needs = tuple(
            (question_id, (need,))
            for question_id, need in zip(selected_question_ids, needs, strict=True)
        )
    else:
        question_needs = ()
    details: list[tuple[str, str, tuple[str, ...]]] = []
    for question_id, question_needs_for_id in question_needs:
        unsupported_facets = tuple(
            sorted(
                {
                    facet_kind
                    for need in question_needs_for_id
                    if need.requirement.value == "mandatory"
                    for facet_kind in _unsupported_mandatory_facet_kinds(
                        need, by_need.get(need.need_id)
                    )
                }
            )
        )
        if unsupported_facets:
            details.append(
                (
                    question_id.root,
                    question_by_id.get(question_id, question_id.root),
                    unsupported_facets,
                )
            )
    return tuple(details)


def _unsupported_mandatory_facet_kinds(
    need: Stage1MemoryNeed,
    trace: RetrievalTrace | None,
) -> tuple[str, ...]:
    required = (
        set(need.completion_spec.required_need_facet_ids)
        if need.completion_spec is not None
        else {facet.need_facet_id for facet in need.need_facets}
    )
    if not required:
        return ()
    facet_by_id = {facet.need_facet_id: facet for facet in need.need_facets}
    receipt_by_id = (
        {}
        if trace is None
        else {receipt.need_facet_id: receipt for receipt in trace.facet_receipts}
    )
    closed = () if trace is None else trace.closed_need_facet_ids
    unresolved: list[str] = []
    for facet_id in required:
        facet = facet_by_id.get(facet_id)
        receipt = receipt_by_id.get(facet_id)
        if (
            facet is None
            or receipt is None
            or receipt.status is not FacetClosureStatus.SUPPORTED
            or facet_id not in closed
        ):
            unresolved.append("unresolved" if facet is None else facet.facet_kind.value)
    return tuple(sorted(set(unresolved)))


def handled_question_ids_for_supported_needs(
    selected_question_ids: tuple[StableId, ...],
    needs: tuple[Stage1MemoryNeed, ...],
    traces: tuple[RetrievalTrace, ...],
) -> tuple[StableId, ...]:
    """Return question ids whose mandatory facet receipts are all SUPPORTED.

    ``UNSUPPORTED`` / ``UNRESOLVED`` stay pending. Tests and the production
    loop share this owner so a false handled mark cannot later produce
    ``PLANNER_MEMORY_NO_PROGRESS``.
    """

    by_need = {trace.need_id: trace for trace in traces}
    grouped: dict[StableId, list[Stage1MemoryNeed]] = {}
    for need in needs:
        if need.planned_draft_id:
            grouped.setdefault(StableId(need.planned_draft_id), []).append(need)
    if grouped:
        handled = [
            question_id
            for question_id in selected_question_ids
            if grouped.get(question_id)
            and all(
                mandatory_facet_receipts_supported(need, by_need.get(need.need_id))
                for need in grouped[question_id]
            )
        ]
        return tuple(handled)
    if len(selected_question_ids) != len(needs):
        return ()
    handled = [
        question_id
        for question_id, need in zip(selected_question_ids, needs, strict=True)
        if mandatory_facet_receipts_supported(need, by_need.get(need.need_id))
    ]
    return tuple(handled)


def mandatory_facet_receipts_supported(
    need: Stage1MemoryNeed,
    trace: RetrievalTrace | None,
) -> bool:
    required = (
        set(need.completion_spec.required_need_facet_ids)
        if need.completion_spec is not None
        else {facet.need_facet_id for facet in need.need_facets}
    )
    if not required:
        return True
    if trace is None:
        return False
    supported = {
        receipt.need_facet_id
        for receipt in trace.facet_receipts
        if receipt.status is FacetClosureStatus.SUPPORTED
    }
    return required.issubset(supported) and required.issubset(set(trace.closed_need_facet_ids))


_PARTIAL_MEMORY_BUDGET_GAP = (
    "Memory retrieval budget exhausted; remaining facets are unresolved and must not be inferred."
)


def _blocking_signature(
    review: PlanReview,
    attempted_identity_history: tuple[tuple[str, ...], ...] = (),
) -> tuple[str, ...]:
    """The recorded frontier of a review's blocking findings.

    The frontier keeps identity and value apart, because the two comparisons need
    different data: a partial repair has to look like the *same* problem narrowed,
    and a rephrase has to look like *no* progress.  An empty finding set has no
    frontier at all -- a legacy ``REVISE`` with no issues and a prose instruction is
    judged by whether the candidate content changed.
    """

    return issue_identity_seed(review, attempted_identity_history)


def _same_finding_identity(previous: tuple[str, ...], review: PlanReview) -> bool:
    """Whether this review answers the recorded frontier with no progress at all.

    The same problem stated the same way is not progress; the same problem with a
    genuinely narrowed condition is.  Both questions are answered against the
    recorded frontier, which is why identity and value are stored together.
    """

    made_progress = progress_against(previous, review)
    return not made_progress


class PlanningContextLoopService:
    """One bounded product loop; retrieval, projection, and review keep their owners."""

    version = "planning_context_loop.v1"

    def __init__(
        self,
        *,
        planner: PlannerAgent,
        reviewer: PlanReviewerAgent,
        need_generator: PlanningInquiryConditionedNeedGenerator,
        memory_gateway: MemoryGateway,
        context_assembler: PlannerContextAssembler,
        context_runtime: PlannerContextRuntimePort,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion,
    ) -> None:
        self._planner = planner
        self._reviewer = reviewer
        self._needs = need_generator
        self._memory = memory_gateway
        self._assembler = context_assembler
        self._context_runtime = context_runtime
        self._artifacts = artifacts
        self._schema_version = schema_version

    def _partial_memory_context(
        self, result: MemoryGatewayResult
    ) -> tuple[Stage1ContextPackage, ArtifactRef] | None:
        """Keep useful bounded Memory output while preserving an explicit gap marker.

        A deterministic retrieval tranche can exhaust its call budget after producing
        usable candidates for earlier Needs.  That is a partial context, not a reason
        to discard every returned entry.  An entirely empty tranche remains fail-closed.
        """

        if result.selected_result.stop_reason is not ControllerStopReason.BUDGET_EXHAUSTED:
            return None
        if not any(trace.candidates for trace in result.context.retrieval_traces):
            return None
        context = result.context
        if _PARTIAL_MEMORY_BUDGET_GAP not in context.unresolved_gaps:
            context = context.model_copy(
                update={
                    "unresolved_gaps": (
                        *context.unresolved_gaps,
                        _PARTIAL_MEMORY_BUDGET_GAP,
                    )
                }
            )
            reference = self._artifacts.put(
                canonical_json_bytes(context.model_dump(mode="json")),
                "application/vnd.novel-agent.context-package+json",
                self._schema_version,
            )
        else:
            reference = result.frozen_context_artifact
        return context, reference

    async def run(
        self,
        *,
        request: PlanningLoopRequest,
        model_request: ModelRequestFactory,
        world: WorldRootDocument | None = None,
        text_root: TextRootDocument | None = None,
        resume_checkpoint_ref: ArtifactRef | None = None,
    ) -> PlanningLoopResult:
        event_refs: list[ArtifactRef] = []
        try:
            return await self._run(
                request=request,
                model_request=model_request,
                world=world,
                text_root=text_root,
                resume_checkpoint_ref=resume_checkpoint_ref,
                event_refs=event_refs,
            )
        except PlanReviewerInvocationError as error:
            if error.review_draft_ref is not None:
                event_refs.append(error.review_draft_ref)
            diagnostic = (
                "PLAN_REVIEW_MECHANICAL_PRECHECK"
                if error.mechanical_preflight
                else "REVIEWER_CONTRACT_FAILURE"
            )
            return self._terminal(
                request,
                PlanningLoopTerminal.REVIEW_REQUIRED,
                event_refs,
                diagnostics=(diagnostic, str(error)[:512]),
            )
        except (PlannerInvocationError, AgentExecutionError):
            return self._terminal(
                request,
                PlanningLoopTerminal.BLOCKED,
                event_refs,
                diagnostics=("PLANNER_CONTRACT_FAILURE",),
            )
        except StructuredGenerationExhausted:
            return self._terminal(
                request,
                PlanningLoopTerminal.BLOCKED,
                event_refs,
                diagnostics=("PLANNER_STRUCTURED_OUTPUT_REJECTED",),
            )
        except (ModelOutputBudgetExhausted, ModelCallCumulativeBudgetExceeded) as error:
            return self._terminal(
                request,
                PlanningLoopTerminal.CONTEXT_LIMIT,
                event_refs,
                diagnostics=(
                    "MODEL_OUTPUT_BUDGET_EXHAUSTED"
                    if isinstance(error, ModelOutputBudgetExhausted)
                    else "MODEL_CUMULATIVE_BUDGET_EXHAUSTED",
                    str(error)[:240],
                ),
            )
        except (
            ModelRoutingError,
            ModelCallForbiddenError,
            ModelEndpointError,
            TimeoutError,
        ) as error:
            return self._terminal(
                request,
                PlanningLoopTerminal.MODEL_UNAVAILABLE,
                event_refs,
                diagnostics=(
                    "MODEL_RUNTIME_UNAVAILABLE",
                    f"{type(error).__name__}: {error}"[:240],
                ),
            )
        except PlannerContextRuntimeFailure as error:
            return self._terminal(
                request,
                PlanningLoopTerminal.SUSPENDED,
                event_refs,
                diagnostics=("CONTEXT_RUNTIME_FAILURE", str(error)[:240]),
            )

    async def _run(
        self,
        *,
        request: PlanningLoopRequest,
        model_request: ModelRequestFactory,
        world: WorldRootDocument | None = None,
        text_root: TextRootDocument | None = None,
        resume_checkpoint_ref: ArtifactRef | None = None,
        event_refs: list[ArtifactRef],
    ) -> PlanningLoopResult:
        visible_author_artifacts = self._visible_author_intent_artifacts(request)
        revision_parent: PlanProposal | None = None
        revision_review: OperatorReviewEvidence | None = None
        revision_parent_ref = request.revision_parent_proposal_ref
        revision_review_ref = next(iter(request.revision_review_artifact_refs), None)
        if revision_parent_ref is not None:
            if revision_parent_ref.media_type != "application/vnd.novel-agent.plan-proposal+json":
                return self._terminal(
                    request,
                    PlanningLoopTerminal.REVIEW_REQUIRED,
                    event_refs,
                    diagnostics=("REVISION_PARENT_WRONG_MEDIA_TYPE",),
                )
            if (
                revision_review_ref is None
                or revision_review_ref.media_type != OPERATOR_PLAN_REVIEW_MEDIA_TYPE
            ):
                return self._terminal(
                    request,
                    PlanningLoopTerminal.REVIEW_REQUIRED,
                    event_refs,
                    diagnostics=("REVISION_REVIEW_MISSING_OR_WRONG_MEDIA_TYPE",),
                )
            revision_parent = self._read(revision_parent_ref, PlanProposal)
            revision_review = self._read(revision_review_ref, OperatorReviewEvidence)
            if (
                revision_parent.project_id != request.project_id
                or revision_parent.mode is not request.task.mode
                or revision_parent.base_commit != request.task.base_commit
            ):
                return self._terminal(
                    request,
                    PlanningLoopTerminal.BASIS_CHANGED,
                    event_refs,
                    diagnostics=("REVISION_PARENT_BASIS_MISMATCH",),
                )
            if revision_review.target_artifact_ref != revision_parent_ref:
                return self._terminal(
                    request,
                    PlanningLoopTerminal.REVIEW_REQUIRED,
                    event_refs,
                    diagnostics=("REVISION_REVIEW_TARGET_MISMATCH",),
                )
        if (
            request.task.mode in {AgentMode.STORY, AgentMode.ARC_VOLUME, AgentMode.CHAPTER_SET}
            and request.task.source_ids
            and not visible_author_artifacts
        ):
            return self._terminal(
                request,
                PlanningLoopTerminal.REVIEW_REQUIRED,
                event_refs,
                diagnostics=("AUTHOR_AUTHORITY_NOT_VISIBLE",),
            )
        source_payload = self._source_payload(visible_author_artifacts)
        revision_parent_id = None if revision_parent is None else revision_parent.proposal_id
        active_revision_artifact_refs = request.revision_artifact_refs
        active_revision_review_artifact_refs = request.revision_review_artifact_refs
        active_revision_parent_ref = revision_parent_ref
        controlled_revision_evidence_ref: ArtifactRef | None = None
        controlled_revision_evidence_text: str | None = None

        def build_planner_source_payload(rendered_context: str) -> str:
            payload = self._planner_source_payload(
                rendered_context,
                visible_author_artifacts,
                active_revision_artifact_refs,
                revision_review_artifacts=active_revision_review_artifact_refs,
                revision_parent=revision_parent,
                revision_review=revision_review,
            )
            if (
                revision_parent is not None
                and world is not None
                and controlled_revision_evidence_text is None
            ):
                payload += f"\n\n{self._world_entity_label_payload(world)}"
            if controlled_revision_evidence_text is not None:
                payload += (
                    '\n\n<CONTROLLED_REVISION_EVIDENCE authority="accepted-world-parent">\n'
                    + controlled_revision_evidence_text
                    + "\n</CONTROLLED_REVISION_EVIDENCE>"
                )
            return payload

        def planner_trusted_context_artifacts(
            *refs: ArtifactRef,
        ) -> tuple[ArtifactRef, ...]:
            return tuple(
                dict.fromkeys(
                    (
                        *refs,
                        *active_revision_artifact_refs,
                        *active_revision_review_artifact_refs,
                        *(
                            (active_revision_parent_ref,)
                            if active_revision_parent_ref is not None
                            else ()
                        ),
                        *(
                            (controlled_revision_evidence_ref,)
                            if controlled_revision_evidence_ref is not None
                            else ()
                        ),
                    )
                )
            )

        if request.task.mode is AgentMode.PROJECT_BOOTSTRAP:
            if world is not None or text_root is not None:
                return self._terminal(
                    request,
                    PlanningLoopTerminal.BASIS_CHANGED,
                    event_refs,
                    diagnostics=("BOOTSTRAP_RECEIVED_PROJECT_MEMORY",),
                )
        elif world is None or text_root is None or world.source_commit != request.task.base_commit:
            return self._terminal(
                request,
                PlanningLoopTerminal.BASIS_CHANGED,
                event_refs,
                diagnostics=("POST_GENESIS_BASIS_MISMATCH",),
            )
        # A history Need has two deadlines: it must end before its own target
        # chapter and it must not read a chapter the project has not committed.
        # Only the first is visible in a model response, so the host supplies
        # the second from the frozen TextRoot it already validated.
        committed_text_cutoff = (
            text_root.chapters[-1].chapter_index
            if text_root is not None and text_root.chapters
            else 0
        )
        if revision_parent is not None and revision_review is not None and world is not None:
            controlled_revision_evidence = self._controlled_revision_evidence_payload(
                world,
                revision_parent,
                revision_review,
            )
            controlled_revision_evidence_bytes = canonical_json_bytes(controlled_revision_evidence)
            controlled_revision_evidence_ref = self._artifacts.put(
                controlled_revision_evidence_bytes,
                CONTROLLED_REVISION_EVIDENCE_MEDIA_TYPE,
                self._schema_version,
            )
            controlled_revision_evidence_text = controlled_revision_evidence_bytes.decode("utf-8")
        if world is not None:
            source_payload = f"{source_payload}\n\n{self._world_entity_label_payload(world)}"
        if revision_parent is not None:
            source_payload += (
                "\n\nCONTROLLED_REVISION_INQUIRY=true\n"
                "This inquiry is only a bounded control repair. Return no Memory questions "
                "for parent-plan fields; keep questions and assumptions empty when the "
                "operator scope already identifies the repair."
            )
        event_refs.append(self._event(request, PlanningLoopPhase.PREFLIGHT, "preflight.passed"))
        if controlled_revision_evidence_ref is not None:
            event_refs.append(
                self._event(
                    request,
                    PlanningLoopPhase.PREFLIGHT,
                    "controlled_revision.evidence_projected",
                    (controlled_revision_evidence_ref,),
                )
            )

        checkpoint = (
            None
            if resume_checkpoint_ref is None
            else self._read(resume_checkpoint_ref, PlanningLoopCheckpoint)
        )
        problem_identity_seed = None if checkpoint is None else checkpoint.problem_identity_seed
        reviewer_context_refs = list(() if checkpoint is None else checkpoint.reviewer_context_refs)
        planner_memory_context_refs = list(
            () if checkpoint is None else checkpoint.planner_memory_context_refs
        )
        plan_revisions = 0 if checkpoint is None else checkpoint.plan_revisions_used
        reviewer_memory_rounds = 0 if checkpoint is None else checkpoint.reviewer_memory_rounds_used
        planner_memory_rounds = 0 if checkpoint is None else checkpoint.planner_memory_rounds_used
        handled_memory_reviews = set(
            () if checkpoint is None else checkpoint.reviewer_memory_review_ids
        )
        handled_memory_questions = set(
            () if checkpoint is None else checkpoint.handled_memory_question_ids
        )
        deferred_memory_questions = set(
            () if checkpoint is None else checkpoint.deferred_memory_question_ids
        )
        model_calls_used = 0 if checkpoint is None else checkpoint.model_calls_used
        model_input_tokens_used = 0 if checkpoint is None else checkpoint.model_input_tokens_used
        model_output_tokens_used = 0 if checkpoint is None else checkpoint.model_output_tokens_used
        model_reasoning_tokens_used = (
            0 if checkpoint is None else checkpoint.model_reasoning_tokens_used
        )
        pending_planner_memory_questions = (
            () if checkpoint is None else checkpoint.pending_planner_memory_questions
        )
        slice_model_tokens_used = 0
        handled_memory_reprompted = False
        unsupported_memory_reprompted = False
        rejected_memory_questions: dict[str, tuple[str, str]] = {}
        unsupported_memory_questions: dict[str, tuple[str, str, tuple[str, ...]]] = {}

        def planner_skill_allowlist(
            *, include_alternative: bool = False
        ) -> tuple[StableId, ...] | None:
            """Return the deployment allowlist for the current Planner turn.

            An empty value is retained for non-production fixture callers.  A
            populated production policy is a real loading boundary: the mode
            Skill and planning-inquiry Skill must both be present, while the
            alternative-comparison Skill is only enabled for REPLAN or an
            actual revision turn.
            """

            if not request.allowed_skill_ids:
                return None
            from novel_agent.agents.planner import planner_skill_ids_for_mode

            policy_ids = set(request.allowed_skill_ids)
            mode_ids = planner_skill_ids_for_mode(request.task.mode)
            required = {
                StableId("skill.planning-inquiry"),
                StableId(f"skill.planner.{request.task.mode.value}"),
            }
            if not required.issubset(policy_ids):
                missing = sorted(item.root for item in required - policy_ids)
                raise ValueError(
                    "production Planner Skill policy is incomplete: " + ", ".join(missing)
                )
            alternative = StableId("skill.alternative-comparison")
            return tuple(
                item
                for item in mode_ids
                if item in policy_ids and (include_alternative or item != alternative)
            )

        def record_model_call(call: object) -> None:
            nonlocal model_calls_used
            nonlocal model_input_tokens_used
            nonlocal model_output_tokens_used
            nonlocal model_reasoning_tokens_used
            nonlocal slice_model_tokens_used
            records_for = getattr(self._planner, "model_calls_for", None)
            records = records_for(call) if callable(records_for) else (call,)
            for record in records:
                usage = getattr(record, "usage", None)
                if usage is None:
                    continue
                model_calls_used += 1
                model_input_tokens_used += int(usage.input_tokens)
                model_output_tokens_used += int(usage.output_tokens)
                model_reasoning_tokens_used += int(usage.reasoning_tokens)
                slice_model_tokens_used += int(
                    usage.input_tokens + usage.output_tokens + usage.reasoning_tokens
                )

        def token_slice_exhausted() -> bool:
            return slice_model_tokens_used >= request.budgets.model_token_budget

        def progress_updates() -> dict[str, object]:
            return {
                "planner_memory_rounds_used": planner_memory_rounds,
                "planner_memory_context_refs": tuple(planner_memory_context_refs),
                "handled_memory_question_ids": self._ordered_ids(handled_memory_questions),
                "deferred_memory_question_ids": self._ordered_ids(deferred_memory_questions),
                "pending_planner_memory_questions": pending_planner_memory_questions,
                "model_calls_used": model_calls_used,
                "model_input_tokens_used": model_input_tokens_used,
                "model_output_tokens_used": model_output_tokens_used,
                "model_reasoning_tokens_used": model_reasoning_tokens_used,
            }

        if checkpoint is not None and (
            checkpoint.request_id != request.request_id
            or checkpoint.base_commit != request.task.base_commit
            or checkpoint.snapshot_id != request.snapshot_id
            or checkpoint.configuration_fingerprint != request.configuration_fingerprint
        ):
            return self._terminal(
                request,
                PlanningLoopTerminal.BASIS_CHANGED,
                event_refs,
                diagnostics=("RESUME_CHECKPOINT_BASIS_MISMATCH",),
            )
        if problem_identity_seed is not None and (
            request.accepted_text_ref is None
            or problem_identity_seed.source_text_root != request.accepted_text_ref.artifact_id
            or request.horizon_start is None
            or problem_identity_seed.cutoff_chapter != request.horizon_start - 1
        ):
            return self._terminal(
                request,
                PlanningLoopTerminal.BASIS_CHANGED,
                event_refs,
                diagnostics=("PROBLEM_IDENTITY_SEED_BASIS_MISMATCH",),
            )

        inquiry: PlanningInquiry
        inquiry_ref: ArtifactRef
        inquiry_review: PlanReview
        inquiry_review_ref: ArtifactRef
        inquiry_revisions = 0 if checkpoint is None else checkpoint.inquiry_revisions_used
        inquiry_revisions_this_slice = 0
        if checkpoint is not None and checkpoint.inquiry_ref and checkpoint.inquiry_review_ref:
            inquiry_ref = checkpoint.inquiry_ref
            inquiry_review_ref = checkpoint.inquiry_review_ref
            inquiry = self._read(inquiry_ref, PlanningInquiry)
            inquiry_review = self._read(inquiry_review_ref, PlanReview)
        else:
            inquiry, inquiry_ref, _receipt, _call = await self._planner.propose_inquiry(
                version=self._schema_version,
                task=request.task,
                source_payload=source_payload,
                source_artifacts=visible_author_artifacts,
                request=model_request("inquiry", request.task.mode, 1),
                horizon_start=request.horizon_start,
                horizon_end=request.horizon_end,
                explicit_overrides=request.explicit_author_overrides,
                allowed_skill_ids=planner_skill_allowlist(
                    include_alternative=request.task.mode is AgentMode.REPLAN
                ),
            )
            record_model_call(_call)
            inquiry_review, inquiry_review_ref, _call = await self._reviewer.review(
                version=self._schema_version,
                mode=request.task.mode,
                target_kind=ReviewTargetKind.INQUIRY,
                target_payload=inquiry.model_dump_json(),
                target_artifact=inquiry_ref,
                trusted_source_artifacts=visible_author_artifacts,
                request=model_request("inquiry_review", request.task.mode, 1),
                base_commit=request.task.base_commit,
            )
            record_model_call(_call)

        while inquiry_review.decision is ReviewDecision.REVISE:
            if request.budgets.inquiry_revisions == 0:
                return self._terminal(
                    request,
                    PlanningLoopTerminal.INQUIRY_REVIEW_REQUIRED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    diagnostics=("INQUIRY_REVISION_DISABLED",),
                )
            if inquiry_revisions_this_slice >= request.budgets.inquiry_revisions:
                event_refs.append(
                    self._checkpoint(
                        request,
                        PlanningLoopPhase.INQUIRY_REVIEWED,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        inquiry_revisions_used=inquiry_revisions,
                        problem_identity_seed=problem_identity_seed,
                        **progress_updates(),
                    )
                )
                return self._terminal(
                    request,
                    PlanningLoopTerminal.YIELDED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    diagnostics=("INQUIRY_REVISION_SLICE_EXHAUSTED",),
                )
            parent_inquiry = inquiry
            instruction = inquiry_review.revision_instruction or "bounded inquiry revision"
            inquiry_revisions += 1
            inquiry_revisions_this_slice += 1
            inquiry_generation = inquiry_revisions + 1
            inquiry, inquiry_ref, _receipt, _call = await self._planner.propose_inquiry(
                version=self._schema_version,
                task=request.task,
                source_payload=(
                    f"{source_payload}\nREVIEW_REVISION={instruction}\n"
                    f"REVIEW={inquiry_review.model_dump_json()}\n"
                    f"PARENT_INQUIRY={parent_inquiry.model_dump_json()}"
                ),
                source_artifacts=visible_author_artifacts,
                request=model_request("inquiry_revision", request.task.mode, inquiry_generation),
                horizon_start=request.horizon_start,
                horizon_end=request.horizon_end,
                explicit_overrides=request.explicit_author_overrides,
                parent_inquiry_id=parent_inquiry.inquiry_id,
                generation=inquiry_generation,
                allowed_skill_ids=planner_skill_allowlist(
                    include_alternative=request.task.mode is AgentMode.REPLAN
                ),
            )
            record_model_call(_call)
            if self._same_inquiry_content(parent_inquiry, inquiry):
                return self._terminal(
                    request,
                    PlanningLoopTerminal.INQUIRY_REVIEW_REQUIRED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    diagnostics=("INQUIRY_REVISION_NO_PROGRESS",),
                )
            inquiry_review, inquiry_review_ref, _call = await self._reviewer.review(
                version=self._schema_version,
                mode=request.task.mode,
                target_kind=ReviewTargetKind.INQUIRY,
                target_payload=inquiry.model_dump_json(),
                target_artifact=inquiry_ref,
                trusted_source_artifacts=visible_author_artifacts,
                request=model_request("inquiry_rereview", request.task.mode, inquiry_generation),
                base_commit=request.task.base_commit,
            )
            record_model_call(_call)
        if inquiry_review.decision is not ReviewDecision.ACCEPT:
            terminal = (
                PlanningLoopTerminal.HUMAN_REQUIRED
                if inquiry_review.decision is ReviewDecision.HUMAN_REQUIRED
                else PlanningLoopTerminal.INQUIRY_REVIEW_REQUIRED
            )
            return self._terminal(
                request,
                terminal,
                event_refs,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
            )
        event_refs.append(
            self._event(
                request,
                PlanningLoopPhase.INQUIRY_ACCEPTED,
                "inquiry.review_settled",
                (inquiry_ref, inquiry_review_ref),
            )
        )
        event_refs.append(
            self._checkpoint(
                request,
                PlanningLoopPhase.INQUIRY_ACCEPTED,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                inquiry_revisions_used=inquiry_revisions,
                problem_identity_seed=problem_identity_seed,
                **progress_updates(),
            )
        )

        stage1_context: Stage1ContextPackage | None = None
        memory_context_ref: ArtifactRef | None = None
        if request.task.mode is not AgentMode.PROJECT_BOOTSTRAP:
            assert world is not None and text_root is not None
            if checkpoint is not None and checkpoint.memory_context_ref is not None:
                memory_context_ref = checkpoint.memory_context_ref
                stage1_context = self._read(memory_context_ref, Stage1ContextPackage)
            else:
                try:
                    need_generation = self._needs.generate(
                        inquiry=inquiry,
                        inquiry_ref=inquiry_ref,
                        review=inquiry_review,
                        review_ref=inquiry_review_ref,
                        world=world,
                        run_id=request.run_id,
                        task_id=request.task_id,
                        problem_identity_seed=problem_identity_seed,
                    )
                except PlanningInquiryNeedError:
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.INQUIRY_INVALID,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                    )
                controlled_revision_without_memory = (
                    revision_parent is not None and not need_generation.needs
                )
                if not need_generation.needs and not controlled_revision_without_memory:
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.MEMORY_INSUFFICIENT,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        diagnostics=("NO_VALID_PLANNER_MEMORY_NEEDS",),
                    )
                if controlled_revision_without_memory:
                    assert request.task.base_commit is not None
                    assert request.snapshot_id is not None
                    assert revision_parent_id is not None
                    stage1_context = Stage1ContextPackage(
                        context_id=StableId(
                            "context.controlled-revision."
                            + content_id(
                                {
                                    "request": request.request_id.root,
                                    "inquiry": inquiry.inquiry_id.root,
                                    "parent": revision_parent_id.root,
                                }
                            ).root[-48:]
                        ),
                        base_commit=request.task.base_commit,
                        snapshot_id=request.snapshot_id,
                        task_contract="stage4:controlled_revision:no_memory",
                        budget_report=ContextBudgetReport(
                            token_budget=request.budgets.context.token_budget,
                            mandatory_tokens=0,
                            optional_tokens=0,
                            full_chapter_read_count=0,
                        ),
                    )
                    memory_context_ref = self._artifacts.put(
                        canonical_json_bytes(stage1_context.model_dump(mode="json")),
                        "application/vnd.novel-agent.stage1-context+json",
                        self._schema_version,
                    )
                    event_refs.append(
                        self._event(
                            request,
                            PlanningLoopPhase.MEMORY_RESOLVED,
                            "memory.skipped_controlled_revision",
                            (memory_context_ref,),
                        )
                    )
                else:
                    deferred_memory_questions.update(
                        getattr(need_generation, "deferred_question_ids", ())
                    )
                    try:
                        memory_result = await self._resolve_memory(
                            request=request,
                            needs=need_generation.needs,
                            text_root=text_root,
                            suffix="inquiry",
                        )
                        resolved_context = memory_result.context
                        resolved_context_ref = memory_result.frozen_context_artifact
                    except MemoryGatewayBlockedError:
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.MEMORY_INSUFFICIENT,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            diagnostics=("MEMORY_GATEWAY_BLOCKED",),
                        )
                    stop_reason = memory_result.selected_result.stop_reason
                    partial_memory = self._partial_memory_context(memory_result)
                    if stop_reason is ControllerStopReason.BUDGET_EXHAUSTED:
                        if partial_memory is None:
                            event_refs.append(
                                self._checkpoint(
                                    request,
                                    PlanningLoopPhase.INQUIRY_ACCEPTED,
                                    inquiry_ref=inquiry_ref,
                                    inquiry_review_ref=inquiry_review_ref,
                                    inquiry_revisions_used=inquiry_revisions,
                                    problem_identity_seed=problem_identity_seed,
                                    **progress_updates(),
                                )
                            )
                            return self._terminal(
                                request,
                                PlanningLoopTerminal.YIELDED,
                                event_refs,
                                inquiry_ref=inquiry_ref,
                                inquiry_review_ref=inquiry_review_ref,
                                diagnostics=("INQUIRY_MEMORY_BUDGET_EXHAUSTED",),
                            )
                        resolved_context, resolved_context_ref = partial_memory
                        stop_reason = ControllerStopReason.NO_ADDITIONAL_EVIDENCE
                    mandatory_total = memory_result.selected_result.mandatory_need_facets_total
                    mandatory_closed = memory_result.selected_result.mandatory_need_facets_closed
                    if mandatory_closed < mandatory_total and partial_memory is None:
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.MEMORY_INSUFFICIENT,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            diagnostics=("MANDATORY_MEMORY_FACETS_UNRESOLVED",),
                        )
                    if stop_reason not in {
                        ControllerStopReason.SUFFICIENT,
                        ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
                    }:
                        terminal = (
                            PlanningLoopTerminal.PLAN_CONFLICT
                            if stop_reason is ControllerStopReason.CONFLICT_REQUIRES_REVIEW
                            else PlanningLoopTerminal.MEMORY_INSUFFICIENT
                        )
                        return self._terminal(
                            request,
                            terminal,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            diagnostics=(stop_reason.value,),
                        )
                    handled_memory_questions.update(
                        handled_question_ids_for_supported_needs(
                            tuple(getattr(need_generation, "selected_question_ids", ())),
                            need_generation.needs,
                            memory_result.context.retrieval_traces,
                        )
                    )
                    stage1_context = resolved_context
                    memory_context_ref = resolved_context_ref
                    event_refs.append(
                        self._event(
                            request,
                            PlanningLoopPhase.MEMORY_RESOLVED,
                            "memory.resolved",
                            (memory_context_ref,),
                        )
                    )

        try:
            if checkpoint is not None and checkpoint.planner_context_ref is not None:
                planner_context_ref = checkpoint.planner_context_ref
                planner_context = self._read_planner_context(planner_context_ref)
                for reviewer_context_ref in reviewer_context_refs:
                    planner_context = self._merge_context_metadata(
                        planner_context,
                        self._read_planner_context(reviewer_context_ref),
                    )
                for planner_memory_context_ref in planner_memory_context_refs:
                    planner_context = self._merge_context_metadata(
                        planner_context,
                        self._read_planner_context(planner_memory_context_ref),
                    )
                projection = self._context_runtime.project(
                    run_id=request.run_id,
                    task_id=request.task_id,
                )
            else:
                planner_context, planner_context_ref = self._assembler.assemble(
                    request=request,
                    inquiry=inquiry,
                    inquiry_ref=inquiry_ref,
                    stage1_context=stage1_context,
                    stage1_context_ref=memory_context_ref,
                )
                projection = self._context_runtime.start(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    seed=planner_context,
                    seed_ref=planner_context_ref,
                )
        except PlannerContextAssemblyError as error:
            return self._terminal(
                request,
                PlanningLoopTerminal.CONTEXT_LIMIT,
                event_refs,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                memory_context_ref=memory_context_ref,
                diagnostics=("PLANNER_CONTEXT_ASSEMBLY_ERROR", str(error)[:240]),
            )
        if projection.suspended:
            return self._terminal(
                request,
                PlanningLoopTerminal.SUSPENDED,
                event_refs,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                memory_context_ref=memory_context_ref,
                planner_context_ref=planner_context_ref,
                diagnostics=(projection.suspension_reason or "CONTEXT_RUNTIME_SUSPENDED",),
            )
        event_refs.append(
            self._event(
                request,
                PlanningLoopPhase.CONTEXT_READY,
                "context.view_ready",
                (planner_context_ref, projection.view_ref),
            )
        )
        event_refs.append(
            self._checkpoint(
                request,
                PlanningLoopPhase.CONTEXT_READY,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                memory_context_ref=memory_context_ref,
                planner_context_ref=planner_context_ref,
                inquiry_revisions_used=inquiry_revisions,
                plan_revisions_used=plan_revisions,
                reviewer_memory_rounds_used=reviewer_memory_rounds,
                reviewer_memory_review_ids=self._ordered_ids(handled_memory_reviews),
                reviewer_context_refs=tuple(reviewer_context_refs),
                problem_identity_seed=problem_identity_seed,
                **progress_updates(),
            )
        )

        if token_slice_exhausted():
            return self._terminal(
                request,
                PlanningLoopTerminal.YIELDED,
                event_refs,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                memory_context_ref=memory_context_ref,
                planner_context_ref=planner_context_ref,
                diagnostics=("MODEL_TOKEN_SLICE_EXHAUSTED",),
            )

        plan_revisions_this_slice = 0
        # An identical blocking finding set means the revision changed nothing the host
        # can check, so the loop stops instead of paying for the same revision again.
        # The basis comes from the checkpoint the last slice settled, so a resumed
        # worker compares against the same problem instead of resetting it.
        previous_blocking_signature: tuple[str, ...] | None = (
            None
            if checkpoint is None or not checkpoint.plan_blocking_signature
            else checkpoint.plan_blocking_signature
        )
        # Which problems this run has already spent a revision on.  Without it a
        # revision that swings between two states looks like progress on every lap,
        # because each lap presents a problem the previous frontier does not name.
        attempted_identity: tuple[tuple[str, ...], ...] = (
            ()
            if previous_blocking_signature is None
            else (tuple(sorted(_seed_identity(previous_blocking_signature))),)
        )

        def recorded_signature(review: PlanReview) -> tuple[str, ...]:
            """The frontier to persist after this review settles.

            A review that made progress is added to the attempted history, because
            the revision it asks for is about to be paid for; a review that made none
            leaves the history alone, since nothing new was attempted.
            """

            nonlocal attempted_identity
            if progress_against(previous_blocking_signature or (), review):
                # Monotone: what the new frontier replaces stays in the history, or a
                # later return to it would look like a problem nobody had tried.
                attempted_identity = (
                    *attempted_identity,
                    tuple(sorted(blocking_issue_identity(review))),
                    tuple(sorted(_seed_identity(previous_blocking_signature or ()))),
                )
            return _blocking_signature(review, attempted_identity)

        # The review that raised those findings is still unanswered: the checkpoint
        # means "a revision is pending", so its own basis must not stop the slice
        # before that revision is attempted.
        basis_review_id: str | None = None
        out_of_scope: tuple[str, ...] = ()
        # True only when the checkpoint's own review is the pending one; a checkpoint
        # without a settled review re-reviews the proposal, and that fresh review is a
        # new problem statement to compare against the recorded basis.
        resumed_review = False
        reviewer_memory_this_slice = 0
        planner_memory_this_slice = 0
        planner_execution_lineage_refs: tuple[ArtifactRef, ...] = ()
        if (
            checkpoint is not None
            and checkpoint.proposal_ref is not None
            and checkpoint.plan_review_ref is not None
            and checkpoint.execution_ref is not None
        ):
            proposal_ref = checkpoint.proposal_ref
            plan_review_ref = checkpoint.plan_review_ref
            execution_ref = checkpoint.execution_ref
            proposal = self._read(proposal_ref, PlanProposal)
            plan_review = self._read(plan_review_ref, PlanReview)
            resumed_review = True
            planner_execution_lineage_refs = (proposal_ref, plan_review_ref, execution_ref)
        elif (
            checkpoint is not None
            and checkpoint.proposal_ref is not None
            and checkpoint.execution_ref is not None
        ):
            proposal_ref = checkpoint.proposal_ref
            execution_ref = checkpoint.execution_ref
            proposal = self._read(proposal_ref, PlanProposal)
            plan_review, plan_review_ref, _call = await self._reviewer.review(
                version=self._schema_version,
                mode=request.task.mode,
                target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                target_payload=proposal.model_dump_json(),
                target_artifact=proposal_ref,
                trusted_source_artifacts=(
                    *visible_author_artifacts,
                    planner_context_ref,
                    projection.view_ref,
                    *(
                        (controlled_revision_evidence_ref,)
                        if controlled_revision_evidence_ref is not None
                        else ()
                    ),
                ),
                request=model_request("plan_review", request.task.mode, 1),
                base_commit=request.task.base_commit,
            )
            record_model_call(_call)
            planner_execution_lineage_refs = (proposal_ref, plan_review_ref, execution_ref)
        else:
            if deferred_memory_questions and not pending_planner_memory_questions:
                by_id = {
                    item.question_id: item.question
                    for item in (*inquiry.assumptions, *inquiry.questions)
                }
                pending_planner_memory_questions = tuple(
                    by_id[item]
                    for item in self._ordered_ids(deferred_memory_questions)
                    if item in by_id
                )
            result = None
            run_turn = getattr(self._planner, "run_turn", None)
            while result is None:
                if pending_planner_memory_questions:
                    if (
                        request.budgets.planner_memory_rounds == 0
                        or planner_memory_this_slice >= request.budgets.planner_memory_rounds
                    ):
                        event_refs.append(
                            self._checkpoint(
                                request,
                                PlanningLoopPhase.PLANNER_MEMORY_PENDING,
                                inquiry_ref=inquiry_ref,
                                inquiry_review_ref=inquiry_review_ref,
                                memory_context_ref=memory_context_ref,
                                planner_context_ref=planner_context_ref,
                                inquiry_revisions_used=inquiry_revisions,
                                plan_revisions_used=plan_revisions,
                                reviewer_memory_rounds_used=reviewer_memory_rounds,
                                reviewer_memory_review_ids=self._ordered_ids(
                                    handled_memory_reviews
                                ),
                                reviewer_context_refs=tuple(reviewer_context_refs),
                                problem_identity_seed=problem_identity_seed,
                                **progress_updates(),
                            )
                        )
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.YIELDED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("PLANNER_MEMORY_SLICE_EXHAUSTED",),
                        )
                    assert world is not None and text_root is not None
                    all_planner_questions = _planner_memory_questions(
                        inquiry,
                        pending_planner_memory_questions,
                        problem_identity_seed,
                    )
                    question_chunk_size = _planner_memory_question_chunk_size(request)
                    planner_questions = all_planner_questions[:question_chunk_size]
                    deferred_for_capacity = all_planner_questions[question_chunk_size:]
                    if all(
                        item.question_id in handled_memory_questions for item in planner_questions
                    ):
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.REVIEW_REQUIRED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("PLANNER_MEMORY_NO_PROGRESS",),
                        )
                    planner_inquiry = inquiry.model_copy(
                        update={"assumptions": (), "questions": planner_questions}
                    )
                    planner_inquiry_ref = self._artifacts.put(
                        canonical_json_bytes(planner_inquiry.model_dump(mode="json")),
                        "application/vnd.novel-agent.planning-inquiry+json",
                        self._schema_version,
                    )
                    (
                        planner_memory_review,
                        planner_memory_review_ref,
                        _call,
                    ) = await self._reviewer.review(
                        version=self._schema_version,
                        mode=request.task.mode,
                        target_kind=ReviewTargetKind.INQUIRY,
                        target_payload=planner_inquiry.model_dump_json(),
                        target_artifact=planner_inquiry_ref,
                        trusted_source_artifacts=(
                            *visible_author_artifacts,
                            planner_context_ref,
                            projection.view_ref,
                        ),
                        request=model_request(
                            "planner_memory_review",
                            request.task.mode,
                            planner_memory_rounds + 1,
                        ),
                        base_commit=request.task.base_commit,
                    )
                    record_model_call(_call)
                    if planner_memory_review.decision is not ReviewDecision.ACCEPT and not (
                        planner_memory_review.decision is ReviewDecision.REVISE
                        and not any(issue.blocking for issue in planner_memory_review.issues)
                    ):
                        # A REVISE without blocking issues is a bounded advisory: the
                        # reviewer judged the questions already answerable from
                        # accepted context, so the Need generator rejects them and the
                        # reprompt plans without them instead of parking the task.
                        return self._terminal(
                            request,
                            (
                                PlanningLoopTerminal.HUMAN_REQUIRED
                                if planner_memory_review.decision is ReviewDecision.HUMAN_REQUIRED
                                else PlanningLoopTerminal.REVIEW_REQUIRED
                            ),
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("PLANNER_MEMORY_REVIEW_NOT_ACCEPTED",),
                        )
                    planner_generation = self._needs.generate(
                        inquiry=planner_inquiry,
                        inquiry_ref=planner_inquiry_ref,
                        review=planner_memory_review,
                        review_ref=planner_memory_review_ref,
                        world=world,
                        run_id=request.run_id,
                        task_id=request.task_id,
                        exclude_question_ids=self._ordered_ids(handled_memory_questions),
                        problem_identity_seed=problem_identity_seed,
                    )
                    for item in planner_questions:
                        reason = planner_generation.rejection_reasons.get(item.question_id.root)
                        if reason is not None:
                            rejected_memory_questions[item.question_id.root] = (
                                item.question,
                                reason,
                            )
                    if not planner_generation.needs:
                        if (
                            run_turn is not None
                            and planner_generation.rejected_question_ids
                            and not handled_memory_reprompted
                        ):
                            handled_memory_reprompted = True
                            retry_turn, result, _call = await run_turn(
                                version=self._schema_version,
                                task=request.task,
                                source_payload=_rejected_memory_reprompt_payload(
                                    build_planner_source_payload(projection.rendered_context),
                                    tuple(rejected_memory_questions.values()),
                                ),
                                source_artifacts=visible_author_artifacts,
                                trusted_context_artifacts=planner_trusted_context_artifacts(
                                    planner_context_ref,
                                    projection.view_ref,
                                    *planner_memory_context_refs,
                                ),
                                parent_proposal_id=revision_parent_id,
                                committed_text_cutoff=committed_text_cutoff,
                                reviewed_inquiry_ref=inquiry_ref,
                                memory_need_ids=planner_context.need_ids,
                                evidence_refs=planner_context.evidence_refs,
                                graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                                request=model_request(
                                    "plan_turn_rejected_reprompt",
                                    request.task.mode,
                                    planner_memory_rounds + 2,
                                ),
                                allowed_skill_ids=planner_skill_allowlist(
                                    include_alternative=(
                                        request.task.mode is AgentMode.REPLAN or plan_revisions > 0
                                    )
                                ),
                            )
                            record_model_call(_call)
                            if retry_turn.action is PlanningTurnAction.REQUEST_MEMORY:
                                return self._terminal(
                                    request,
                                    PlanningLoopTerminal.REVIEW_REQUIRED,
                                    event_refs,
                                    inquiry_ref=inquiry_ref,
                                    inquiry_review_ref=inquiry_review_ref,
                                    memory_context_ref=memory_context_ref,
                                    planner_context_ref=planner_context_ref,
                                    diagnostics=("PLANNER_MEMORY_NO_PROGRESS",),
                                )
                            assert result is not None
                            break
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.REVIEW_REQUIRED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("NO_VALID_PLANNER_TURN_MEMORY_NEEDS",),
                        )
                    planner_memory = await self._resolve_memory(
                        request=request,
                        needs=planner_generation.needs,
                        text_root=text_root,
                        suffix=f"planner-{planner_memory_rounds + 1}",
                    )
                    planner_memory_context = planner_memory.context
                    planner_memory_context_ref = planner_memory.frozen_context_artifact
                    planner_memory_stop_reason = planner_memory.selected_result.stop_reason
                    partial_planner_memory = self._partial_memory_context(planner_memory)
                    if planner_memory_stop_reason is ControllerStopReason.BUDGET_EXHAUSTED:
                        if partial_planner_memory is None:
                            event_refs.append(
                                self._checkpoint(
                                    request,
                                    PlanningLoopPhase.PLANNER_MEMORY_PENDING,
                                    inquiry_ref=inquiry_ref,
                                    inquiry_review_ref=inquiry_review_ref,
                                    memory_context_ref=memory_context_ref,
                                    planner_context_ref=planner_context_ref,
                                    inquiry_revisions_used=inquiry_revisions,
                                    problem_identity_seed=problem_identity_seed,
                                    **progress_updates(),
                                )
                            )
                            return self._terminal(
                                request,
                                PlanningLoopTerminal.YIELDED,
                                event_refs,
                                inquiry_ref=inquiry_ref,
                                inquiry_review_ref=inquiry_review_ref,
                                memory_context_ref=memory_context_ref,
                                planner_context_ref=planner_context_ref,
                                diagnostics=("PLANNER_MEMORY_BUDGET_EXHAUSTED",),
                            )
                        planner_memory_context, planner_memory_context_ref = partial_planner_memory
                        planner_memory_stop_reason = ControllerStopReason.NO_ADDITIONAL_EVIDENCE
                    question_by_id = {item.question_id: item.question for item in planner_questions}
                    unsupported_details = _unsupported_memory_question_details(
                        planner_generation.selected_question_ids,
                        planner_generation.needs,
                        planner_memory.context.retrieval_traces,
                        question_by_id,
                    )
                    content_gap_stop = (
                        planner_memory_stop_reason is ControllerStopReason.MANDATORY_GAP_UNRESOLVED
                        and bool(unsupported_details)
                    )
                    if (
                        planner_memory_stop_reason
                        not in {
                            ControllerStopReason.SUFFICIENT,
                            ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
                        }
                        and not content_gap_stop
                    ):
                        return self._terminal(
                            request,
                            (
                                PlanningLoopTerminal.PLAN_CONFLICT
                                if planner_memory_stop_reason
                                is ControllerStopReason.CONFLICT_REQUIRES_REVIEW
                                else PlanningLoopTerminal.REVIEW_REQUIRED
                            ),
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=planner_memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=(planner_memory_stop_reason.value,),
                        )
                    planner_delta, planner_delta_ref = self._assembler.assemble(
                        request=request,
                        inquiry=planner_inquiry,
                        inquiry_ref=inquiry_ref,
                        stage1_context=planner_memory_context,
                        stage1_context_ref=planner_memory_context_ref,
                    )
                    projection = self._context_runtime.append_delta(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        delta_ref=planner_delta_ref,
                    )
                    if projection.suspended:
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.SUSPENDED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=planner_memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=(
                                projection.suspension_reason or "PLANNER_MEMORY_CONTEXT_SUSPENDED",
                            ),
                        )
                    planner_context = self._merge_context_metadata(planner_context, planner_delta)
                    planner_memory_context_refs.append(planner_delta_ref)
                    planner_memory_rounds += 1
                    planner_memory_this_slice += 1
                    handled_memory_questions.update(
                        handled_question_ids_for_supported_needs(
                            planner_generation.selected_question_ids,
                            planner_generation.needs,
                            planner_memory.context.retrieval_traces,
                        )
                    )
                    unsupported_memory_questions.update(
                        {
                            question_id: (question_id, question, facets)
                            for question_id, question, facets in unsupported_details
                        }
                    )
                    deferred_memory_questions = {item.question_id for item in deferred_for_capacity}
                    deferred_memory_questions.update(planner_generation.deferred_question_ids)
                    deferred_by_id = {item.question_id: item.question for item in planner_questions}
                    pending_planner_memory_questions = tuple(
                        item.question for item in deferred_for_capacity
                    ) + tuple(
                        deferred_by_id[item]
                        for item in self._ordered_ids(set(planner_generation.deferred_question_ids))
                        if item in deferred_by_id
                    )
                    if pending_planner_memory_questions:
                        continue
                    if unsupported_memory_questions:
                        if _has_evidence_bound_unsupported_gap(
                            planner_memory.context,
                            text_root,
                        ):
                            return self._terminal(
                                request,
                                PlanningLoopTerminal.REVIEW_REQUIRED,
                                event_refs,
                                inquiry_ref=inquiry_ref,
                                inquiry_review_ref=inquiry_review_ref,
                                memory_context_ref=planner_memory_context_ref,
                                planner_context_ref=planner_context_ref,
                                diagnostics=("PLANNER_MEMORY_FACETS_UNRESOLVED",),
                            )
                        if unsupported_memory_reprompted:
                            return self._terminal(
                                request,
                                PlanningLoopTerminal.REVIEW_REQUIRED,
                                event_refs,
                                inquiry_ref=inquiry_ref,
                                inquiry_review_ref=inquiry_review_ref,
                                memory_context_ref=planner_memory_context_ref,
                                planner_context_ref=planner_context_ref,
                                diagnostics=("PLANNER_MEMORY_FACETS_UNRESOLVED",),
                            )
                        if run_turn is not None:
                            unsupported_memory_reprompted = True
                            unsupported_details_for_fallback = tuple(
                                unsupported_memory_questions.values()
                            )
                            retry_turn, result, _call = await run_turn(
                                version=self._schema_version,
                                task=request.task,
                                source_payload=_unsupported_memory_reprompt_payload(
                                    build_planner_source_payload(projection.rendered_context),
                                    tuple(unsupported_memory_questions.values()),
                                ),
                                source_artifacts=visible_author_artifacts,
                                trusted_context_artifacts=planner_trusted_context_artifacts(
                                    planner_context_ref,
                                    projection.view_ref,
                                    *planner_memory_context_refs,
                                ),
                                parent_proposal_id=revision_parent_id,
                                committed_text_cutoff=committed_text_cutoff,
                                reviewed_inquiry_ref=inquiry_ref,
                                memory_need_ids=planner_context.need_ids,
                                evidence_refs=planner_context.evidence_refs,
                                graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                                request=model_request(
                                    "plan_turn_unsupported_reprompt",
                                    request.task.mode,
                                    planner_memory_rounds + 2,
                                ),
                                allowed_skill_ids=planner_skill_allowlist(
                                    include_alternative=(
                                        request.task.mode is AgentMode.REPLAN or plan_revisions > 0
                                    )
                                ),
                            )
                            record_model_call(_call)
                            unsupported_question_texts = {
                                _canonical_planner_memory_question(question)
                                for _question_id, question, _facets in (
                                    unsupported_memory_questions.values()
                                )
                            }
                            unsupported_memory_questions.clear()
                            if retry_turn.action is PlanningTurnAction.REQUEST_MEMORY:
                                repeated_question = any(
                                    _canonical_planner_memory_question(question)
                                    in unsupported_question_texts
                                    for question in retry_turn.memory_questions
                                )
                                if repeated_question:
                                    result, _call = await self._planner.run(
                                        version=self._schema_version,
                                        task=request.task,
                                        source_payload=(
                                            _unsupported_memory_reprompt_payload(
                                                build_planner_source_payload(
                                                    projection.rendered_context
                                                ),
                                                unsupported_details_for_fallback,
                                            )
                                            + "\nPLANNER_MEMORY_FALLBACK=The same unsupported "
                                            "request was repeated. Return PLAN_READY now with "
                                            "the supported Memory entries and explicit unresolved "
                                            "markers; do not issue another REQUEST_MEMORY action."
                                        ),
                                        source_artifacts=visible_author_artifacts,
                                        trusted_context_artifacts=planner_trusted_context_artifacts(
                                            planner_context_ref,
                                            projection.view_ref,
                                            *planner_memory_context_refs,
                                        ),
                                        parent_proposal_id=revision_parent_id,
                                        committed_text_cutoff=committed_text_cutoff,
                                        reviewed_inquiry_ref=inquiry_ref,
                                        memory_need_ids=planner_context.need_ids,
                                        evidence_refs=planner_context.evidence_refs,
                                        graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                                        request=model_request(
                                            "plan_after_unsupported_memory_no_progress",
                                            request.task.mode,
                                            planner_memory_rounds + 3,
                                        ),
                                        allowed_skill_ids=planner_skill_allowlist(
                                            include_alternative=(
                                                request.task.mode is AgentMode.REPLAN
                                                or plan_revisions > 0
                                            )
                                        ),
                                    )
                                    record_model_call(_call)
                                    result = _retain_unsupported_memory_gaps(
                                        result,
                                        unsupported_details_for_fallback,
                                        affected_chapters=(
                                            tuple(
                                                range(
                                                    request.horizon_start,
                                                    request.horizon_end + 1,
                                                )
                                            )
                                            if request.horizon_start is not None
                                            and request.horizon_end is not None
                                            else ()
                                        ),
                                        source_artifact_refs=tuple(
                                            dict.fromkeys(
                                                (
                                                    planner_context_ref,
                                                    projection.view_ref,
                                                    *planner_memory_context_refs,
                                                )
                                            )
                                        ),
                                    )
                                    break
                                pending_planner_memory_questions = retry_turn.memory_questions
                                continue
                            assert result is not None
                            # A PLAN_READY response after an unsupported mandatory
                            # facet is still not evidence that the facet was resolved.
                            # Retain the host-owned gap on this direct-ready branch as
                            # well as on the repeated-request fallback; otherwise a
                            # compliant Planner can silently drop the gap simply by
                            # returning PLAN_READY on the first bounded reprompt.
                            result = _retain_unsupported_memory_gaps(
                                result,
                                unsupported_details_for_fallback,
                                affected_chapters=(
                                    tuple(range(request.horizon_start, request.horizon_end + 1))
                                    if request.horizon_start is not None
                                    and request.horizon_end is not None
                                    else ()
                                ),
                                source_artifact_refs=tuple(
                                    dict.fromkeys(
                                        (
                                            planner_context_ref,
                                            projection.view_ref,
                                            *planner_memory_context_refs,
                                        )
                                    )
                                ),
                            )
                            break
                    # Slice yield is a resume boundary, not a post-memory abort of plan_turn.
                if run_turn is None:
                    planner_source_payload = build_planner_source_payload(
                        projection.rendered_context
                    )
                    if rejected_memory_questions:
                        planner_source_payload = _rejected_memory_reprompt_payload(
                            planner_source_payload,
                            tuple(rejected_memory_questions.values()),
                        )
                        rejected_memory_questions.clear()
                    if unsupported_memory_questions:
                        planner_source_payload = _unsupported_memory_reprompt_payload(
                            planner_source_payload,
                            tuple(unsupported_memory_questions.values()),
                        )
                        unsupported_memory_questions.clear()
                    result, _call = await self._planner.run(
                        version=self._schema_version,
                        task=request.task,
                        source_payload=planner_source_payload,
                        source_artifacts=visible_author_artifacts,
                        trusted_context_artifacts=planner_trusted_context_artifacts(
                            planner_context_ref, projection.view_ref
                        ),
                        reviewed_inquiry_ref=inquiry_ref,
                        memory_need_ids=planner_context.need_ids,
                        evidence_refs=planner_context.evidence_refs,
                        graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                        parent_proposal_id=revision_parent_id,
                        committed_text_cutoff=committed_text_cutoff,
                        request=model_request(
                            "plan_revision" if revision_parent is not None else "plan",
                            request.task.mode,
                            1,
                        ),
                        allowed_skill_ids=planner_skill_allowlist(
                            include_alternative=(
                                request.task.mode is AgentMode.REPLAN or plan_revisions > 0
                            )
                        ),
                    )
                    break
                planner_source_payload = build_planner_source_payload(projection.rendered_context)
                if rejected_memory_questions:
                    planner_source_payload = _rejected_memory_reprompt_payload(
                        planner_source_payload,
                        tuple(rejected_memory_questions.values()),
                    )
                    rejected_memory_questions.clear()
                if unsupported_memory_questions:
                    planner_source_payload = _unsupported_memory_reprompt_payload(
                        planner_source_payload,
                        tuple(unsupported_memory_questions.values()),
                    )
                    unsupported_memory_questions.clear()
                turn, result, _call = await run_turn(
                    version=self._schema_version,
                    task=request.task,
                    source_payload=planner_source_payload,
                    source_artifacts=visible_author_artifacts,
                    trusted_context_artifacts=planner_trusted_context_artifacts(
                        planner_context_ref,
                        projection.view_ref,
                        *planner_memory_context_refs,
                    ),
                    parent_proposal_id=revision_parent_id,
                    committed_text_cutoff=committed_text_cutoff,
                    reviewed_inquiry_ref=inquiry_ref,
                    memory_need_ids=planner_context.need_ids,
                    evidence_refs=planner_context.evidence_refs,
                    graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                    request=model_request(
                        "plan_turn", request.task.mode, planner_memory_rounds + 1
                    ),
                    allowed_skill_ids=planner_skill_allowlist(
                        include_alternative=(
                            request.task.mode is AgentMode.REPLAN or plan_revisions > 0
                        )
                    ),
                )
                record_model_call(_call)
                if turn.action is PlanningTurnAction.REQUEST_MEMORY:
                    if revision_parent is not None:
                        retry_turn, result, _call = await run_turn(
                            version=self._schema_version,
                            task=request.task,
                            source_payload=(
                                planner_source_payload
                                + "\n\nCONTROLLED_REVISION_MEMORY_FORBIDDEN=true\n"
                                "Use CONTROLLED_REVISION_EVIDENCE as the factual basis for the "
                                "authorized fields. For owner_ids, owner means the continuing "
                                "narrative subject and retrieval anchor, not a grantor, teacher, "
                                "information holder, location, or incidental participant. The "
                                "same canonical subject may correctly own many obligations. "
                                "For each obligation, apply explicit_subject_rule and copy the "
                                "provided explicitly_named_subject_ids when its parent arc "
                                "confirms the named subject's transition; questions asking "
                                "whether that rule applies are contract decisions, not Memory. "
                                "For any authorized *.serves repair, copy one exact "
                                "obligation_id from accepted_world_obligations whose description "
                                "and window match that stage; never retain a legacy lock.* handle. "
                                "Author PLAN_READY now without requesting historical Memory or "
                                "adding unresolved items."
                            ),
                            source_artifacts=visible_author_artifacts,
                            trusted_context_artifacts=planner_trusted_context_artifacts(
                                planner_context_ref,
                                projection.view_ref,
                                *planner_memory_context_refs,
                            ),
                            parent_proposal_id=revision_parent_id,
                            committed_text_cutoff=committed_text_cutoff,
                            reviewed_inquiry_ref=inquiry_ref,
                            memory_need_ids=planner_context.need_ids,
                            evidence_refs=planner_context.evidence_refs,
                            graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                            request=model_request(
                                "plan_controlled_revision_memory_forbidden",
                                request.task.mode,
                                planner_memory_rounds + 2,
                            ),
                            allowed_skill_ids=planner_skill_allowlist(
                                include_alternative=(
                                    request.task.mode is AgentMode.REPLAN or plan_revisions > 0
                                )
                            ),
                        )
                        record_model_call(_call)
                        if retry_turn.action is PlanningTurnAction.REQUEST_MEMORY:
                            return self._terminal(
                                request,
                                PlanningLoopTerminal.REVIEW_REQUIRED,
                                event_refs,
                                inquiry_ref=inquiry_ref,
                                inquiry_review_ref=inquiry_review_ref,
                                memory_context_ref=memory_context_ref,
                                planner_context_ref=planner_context_ref,
                                diagnostics=("CONTROLLED_REVISION_MEMORY_REQUESTED",),
                            )
                        assert result is not None
                        break
                    if request.task.mode is AgentMode.PROJECT_BOOTSTRAP:
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.REVIEW_REQUIRED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            planner_context_ref=planner_context_ref,
                            diagnostics=("BOOTSTRAP_PLANNER_MEMORY_FORBIDDEN",),
                        )
                    requested_question_ids = _requested_planner_memory_question_ids(
                        turn.memory_questions,
                        inquiry,
                        problem_identity_seed,
                    )
                    if all(
                        question_id in handled_memory_questions
                        for question_id in requested_question_ids
                    ):
                        if handled_memory_reprompted:
                            return self._terminal(
                                request,
                                PlanningLoopTerminal.REVIEW_REQUIRED,
                                event_refs,
                                inquiry_ref=inquiry_ref,
                                inquiry_review_ref=inquiry_review_ref,
                                memory_context_ref=memory_context_ref,
                                planner_context_ref=planner_context_ref,
                                diagnostics=("PLANNER_MEMORY_NO_PROGRESS",),
                            )
                        handled_memory_reprompted = True
                        retry_turn, result, _call = await run_turn(
                            version=self._schema_version,
                            task=request.task,
                            source_payload=_supported_memory_reprompt_payload(
                                build_planner_source_payload(projection.rendered_context),
                                tuple(turn.memory_questions),
                            ),
                            source_artifacts=visible_author_artifacts,
                            trusted_context_artifacts=planner_trusted_context_artifacts(
                                planner_context_ref,
                                projection.view_ref,
                                *planner_memory_context_refs,
                            ),
                            parent_proposal_id=revision_parent_id,
                            committed_text_cutoff=committed_text_cutoff,
                            reviewed_inquiry_ref=inquiry_ref,
                            memory_need_ids=planner_context.need_ids,
                            evidence_refs=planner_context.evidence_refs,
                            graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                            request=model_request(
                                "plan_turn_supported_reprompt",
                                request.task.mode,
                                planner_memory_rounds + 2,
                            ),
                            allowed_skill_ids=planner_skill_allowlist(
                                include_alternative=(
                                    request.task.mode is AgentMode.REPLAN or plan_revisions > 0
                                )
                            ),
                        )
                        record_model_call(_call)
                        if retry_turn.action is PlanningTurnAction.REQUEST_MEMORY:
                            supported_questions = {
                                _canonical_planner_memory_question(question)
                                for question in turn.memory_questions
                            }
                            repeated_questions = {
                                _canonical_planner_memory_question(question)
                                for question in retry_turn.memory_questions
                            }
                            if repeated_questions != supported_questions:
                                return self._terminal(
                                    request,
                                    PlanningLoopTerminal.REVIEW_REQUIRED,
                                    event_refs,
                                    inquiry_ref=inquiry_ref,
                                    inquiry_review_ref=inquiry_review_ref,
                                    memory_context_ref=memory_context_ref,
                                    planner_context_ref=planner_context_ref,
                                    diagnostics=("PLANNER_MEMORY_NO_PROGRESS",),
                                )
                            result, _call = await self._planner.run(
                                version=self._schema_version,
                                task=request.task,
                                source_payload=(
                                    _supported_memory_reprompt_payload(
                                        build_planner_source_payload(projection.rendered_context),
                                        tuple(turn.memory_questions),
                                    )
                                    + "\nPLANNER_MEMORY_FALLBACK=The requested facts are already "
                                    "supported. Author PLAN_READY now; do not emit another "
                                    "REQUEST_MEMORY action."
                                ),
                                source_artifacts=visible_author_artifacts,
                                trusted_context_artifacts=planner_trusted_context_artifacts(
                                    planner_context_ref,
                                    projection.view_ref,
                                    *planner_memory_context_refs,
                                ),
                                parent_proposal_id=revision_parent_id,
                                committed_text_cutoff=committed_text_cutoff,
                                reviewed_inquiry_ref=inquiry_ref,
                                memory_need_ids=planner_context.need_ids,
                                evidence_refs=planner_context.evidence_refs,
                                graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                                request=model_request(
                                    "plan_after_supported_memory_no_progress",
                                    request.task.mode,
                                    planner_memory_rounds + 3,
                                ),
                                allowed_skill_ids=planner_skill_allowlist(
                                    include_alternative=(
                                        request.task.mode is AgentMode.REPLAN or plan_revisions > 0
                                    )
                                ),
                            )
                            record_model_call(_call)
                        assert result is not None
                        break
                    pending_planner_memory_questions = turn.memory_questions
                    continue
                assert result is not None
            proposal = result.plan_proposal
            proposal_ref = self._persist_proposal(proposal)
            execution_ref = self._artifacts.put(
                canonical_json_bytes(result.model_dump(mode="json")),
                "application/vnd.novel-agent.planner-execution-result+json",
                self._schema_version,
            )
            planner_execution_lineage_refs = (proposal_ref, execution_ref)
            if (
                revision_parent is not None
                and revision_parent_ref is not None
                and revision_review is not None
                and revision_review_ref is not None
            ):
                scope = operator_revision_scope(revision_review)
                try:
                    composed = compose_scoped_revision(revision_parent, proposal, scope)
                except PlanCompositionError as error:
                    event_refs.append(
                        self._event(
                            request,
                            PlanningLoopPhase.PLAN_REVIEWED,
                            "plan.revision_scope_rejected",
                            (
                                revision_parent_ref,
                                proposal_ref,
                                revision_review_ref,
                                execution_ref,
                            ),
                        )
                    )
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.REVIEW_REQUIRED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        diagnostics=("REVISION_SCOPE_REJECTED", str(error)[:240]),
                    )
                raw_proposal = proposal
                raw_proposal_ref = proposal_ref
                raw_execution_ref = execution_ref
                out_of_scope = out_of_scope_items(revision_parent, raw_proposal, scope)
                proposal = composed
                proposal_ref = self._persist_proposal(proposal)
                proof = build_composition_proof(
                    parent_ref=revision_parent_ref,
                    raw_execution_ref=raw_execution_ref,
                    review_ref=revision_review_ref,
                    scope=scope,
                    composed=proposal,
                    out_of_scope=out_of_scope,
                )
                proof_ref = self._artifacts.put(
                    canonical_json_bytes(proof.model_dump(mode="json")),
                    "application/vnd.novel-agent.plan-composition-proof+json",
                    self._schema_version,
                )
                execution_ref = self._artifacts.put(
                    canonical_json_bytes(
                        result.model_copy(
                            update={
                                "plan_proposal": proposal,
                                "composition_proof": proof_ref,
                                "raw_plan_proposal": raw_proposal,
                            }
                        ).model_dump(mode="json")
                    ),
                    "application/vnd.novel-agent.planner-execution-result+json",
                    self._schema_version,
                )
                planner_execution_lineage_refs = (
                    revision_parent_ref,
                    raw_proposal_ref,
                    proposal_ref,
                    revision_review_ref,
                    raw_execution_ref,
                    proof_ref,
                    execution_ref,
                )
                if self._same_proposal_content(revision_parent, proposal):
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        diagnostics=("PLAN_REVISION_NO_PROGRESS",),
                    )
                # The host review has been consumed into this composed parent.  Later
                # model Reviewer revisions must use their own review scope, not repeat
                # the operator directive as a competing prompt.
                revision_parent = None
                revision_parent_id = None
                active_revision_artifact_refs = ()
                active_revision_review_artifact_refs = ()
                active_revision_parent_ref = None
            # First review of a proposal authored this invocation is in-flight work.
            # A citation failure is repaired against this exact candidate once, as in
            # the bounded D0 path.  The first provider call is still real usage and
            # must be recorded even though the reviewer raises before returning a
            # PlanReview receipt.
            try:
                plan_review, plan_review_ref, _call = await self._reviewer.review(
                    version=self._schema_version,
                    mode=request.task.mode,
                    target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                    target_payload=proposal.model_dump_json(),
                    target_artifact=proposal_ref,
                    trusted_source_artifacts=(
                        *visible_author_artifacts,
                        planner_context_ref,
                        projection.view_ref,
                        *(
                            (controlled_revision_evidence_ref,)
                            if controlled_revision_evidence_ref is not None
                            else ()
                        ),
                    ),
                    request=model_request("plan_review", request.task.mode, 1),
                    base_commit=request.task.base_commit,
                )
            except PlanReviewerInvocationError as error:
                if error.model_call is not None:
                    record_model_call(error.model_call)
                if error.review_draft_ref is not None:
                    event_refs.append(error.review_draft_ref)
                if not error.citation_repairable:
                    raise
                # This is a same-candidate Reviewer repair, not a Planner retry.  The
                # feedback contains only host verification results and preserved valid
                # findings; it grants no write authority and cannot change the target.
                try:
                    plan_review, plan_review_ref, _call = await self._reviewer.review(
                        version=self._schema_version,
                        mode=request.task.mode,
                        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                        target_payload=proposal.model_dump_json(),
                        target_artifact=proposal_ref,
                        trusted_source_artifacts=(
                            *visible_author_artifacts,
                            planner_context_ref,
                            projection.view_ref,
                            *(
                                (controlled_revision_evidence_ref,)
                                if controlled_revision_evidence_ref is not None
                                else ()
                            ),
                        ),
                        request=model_request("plan_review_citation_repair", request.task.mode, 2),
                        base_commit=request.task.base_commit,
                        review_feedback=str(error),
                    )
                except PlanReviewerInvocationError as repair_error:
                    if repair_error.model_call is not None:
                        record_model_call(repair_error.model_call)
                    if repair_error.review_draft_ref is not None:
                        event_refs.append(repair_error.review_draft_ref)
                    raise
            record_model_call(_call)

        if previous_blocking_signature is not None and resumed_review:
            # Resumed from a settled review: that review is the one still awaiting an
            # answer, so its own findings are the basis rather than a repeat of it.
            basis_review_id = plan_review.review_id.root

        def revision_repeats_pending_problem(review: PlanReview) -> bool:
            """True when this review answers the last revision with the same problem.

            The identity is what decides, not the wording: an identical identity with
            an unchanged unmet condition is the same problem coming back, while a
            *narrowed* statement of that same identity is real progress and must not
            be stopped.  Identity is also what makes oscillation visible -- returning
            to an earlier mistake restores an earlier identity rather than buying a
            new attempt under a new description.

            ``basis_review_id`` is the review whose findings the pending revision is
            answering; comparing that review with itself would stop a resume before it
            attempted the revision the checkpoint demands.
            """

            nonlocal previous_blocking_signature, basis_review_id
            if (
                previous_blocking_signature is not None
                and review.review_id.root != basis_review_id
                and _same_finding_identity(previous_blocking_signature, review)
            ):
                return True
            previous_blocking_signature = recorded_signature(review) or None
            basis_review_id = review.review_id.root
            return False

        def plan_revision_no_progress() -> PlanningLoopResult:
            # Record the settlement frontier before stopping: the runtime decides where
            # a retry resumes from the artifacts, and the rejected problem identity has
            # to stay with the review that raised it.
            event_refs.append(
                self._checkpoint(
                    request,
                    PlanningLoopPhase.PLAN_REVIEWED,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    memory_context_ref=memory_context_ref,
                    planner_context_ref=planner_context_ref,
                    proposal_ref=proposal_ref,
                    plan_review_ref=plan_review_ref,
                    execution_ref=execution_ref,
                    plan_blocking_signature=recorded_signature(plan_review),
                    inquiry_revisions_used=inquiry_revisions,
                    plan_revisions_used=plan_revisions,
                    reviewer_memory_rounds_used=reviewer_memory_rounds,
                    reviewer_memory_review_ids=self._ordered_ids(handled_memory_reviews),
                    reviewer_context_refs=tuple(reviewer_context_refs),
                    problem_identity_seed=problem_identity_seed,
                    **progress_updates(),
                )
            )
            return self._terminal(
                request,
                PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                event_refs,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                memory_context_ref=memory_context_ref,
                planner_context_ref=planner_context_ref,
                proposal=proposal,
                plan_review_ref=plan_review_ref,
                diagnostics=("PLAN_REVISION_NO_PROGRESS",),
            )

        advisory_diagnostics: tuple[str, ...] = ()
        while plan_review.decision is ReviewDecision.REVISE:
            if request.budgets.plan_revisions == 0:
                return self._terminal(
                    request,
                    PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    memory_context_ref=memory_context_ref,
                    planner_context_ref=planner_context_ref,
                    proposal=proposal,
                    plan_review_ref=plan_review_ref,
                    diagnostics=("PLAN_REVISION_DISABLED",),
                )
            if plan_revisions_this_slice >= request.budgets.plan_revisions:
                event_refs.append(
                    self._checkpoint(
                        request,
                        PlanningLoopPhase.PLAN_REVIEWED,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal_ref=proposal_ref,
                        plan_review_ref=plan_review_ref,
                        execution_ref=execution_ref,
                        plan_blocking_signature=recorded_signature(plan_review),
                        inquiry_revisions_used=inquiry_revisions,
                        plan_revisions_used=plan_revisions,
                        reviewer_memory_rounds_used=reviewer_memory_rounds,
                        reviewer_memory_review_ids=self._ordered_ids(handled_memory_reviews),
                        reviewer_context_refs=tuple(reviewer_context_refs),
                        problem_identity_seed=problem_identity_seed,
                        **progress_updates(),
                    )
                )
                return self._terminal(
                    request,
                    PlanningLoopTerminal.YIELDED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    memory_context_ref=memory_context_ref,
                    planner_context_ref=planner_context_ref,
                    proposal=proposal,
                    plan_review_ref=plan_review_ref,
                    diagnostics=("PLAN_REVISION_SLICE_EXHAUSTED",),
                )

            if (
                plan_review.memory_gap_questions
                and plan_review.review_id not in handled_memory_reviews
                and request.task.mode is not AgentMode.PROJECT_BOOTSTRAP
            ):
                if request.budgets.reviewer_memory_rounds == 0:
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=("REVIEWER_MEMORY_DISABLED",),
                    )
                if reviewer_memory_this_slice >= request.budgets.reviewer_memory_rounds:
                    event_refs.append(
                        self._checkpoint(
                            request,
                            PlanningLoopPhase.PLAN_REVIEWED,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            proposal_ref=proposal_ref,
                            plan_review_ref=plan_review_ref,
                            execution_ref=execution_ref,
                            plan_blocking_signature=recorded_signature(plan_review),
                            inquiry_revisions_used=inquiry_revisions,
                            plan_revisions_used=plan_revisions,
                            reviewer_memory_rounds_used=reviewer_memory_rounds,
                            reviewer_memory_review_ids=self._ordered_ids(handled_memory_reviews),
                            reviewer_context_refs=tuple(reviewer_context_refs),
                            problem_identity_seed=problem_identity_seed,
                            **progress_updates(),
                        )
                    )
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.YIELDED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=("REVIEWER_MEMORY_SLICE_EXHAUSTED",),
                    )
                assert world is not None and text_root is not None
                # An independent reviewer asserted a fact the candidate needs.
                # It stays a hard historical prerequisite: the loop must either
                # resolve it or revise the plan, never silently downgrade it.
                gap_purpose, gap_expectation = question_boundary_semantics(
                    mode=request.task.mode,
                    intent=TrustedQuestionIntent.REVIEWER_GAP_QUESTION,
                    kind=PlanningQuestionKind.FACT,
                    blocking=True,
                )
                gap_questions = tuple(
                    PlanningQuestion(
                        question_id=StableId(
                            f"reviewer-gap.{index}.{plan_review.review_id.root}"[:128]
                        ),
                        kind=PlanningQuestionKind.FACT,
                        question=question,
                        provenance=PlanningReference(
                            provenance=PlanningProvenance.REVIEWER_DERIVED,
                            artifact_refs=(plan_review_ref,),
                        ),
                        goal_id=inquiry.goal_proposals[0].goal_id,
                        blocking=True,
                        question_purpose=gap_purpose,
                        dependency_expectation=gap_expectation,
                    )
                    for index, question in enumerate(plan_review.memory_gap_questions)
                )
                reviewer_inquiry = inquiry.model_copy(
                    update={"questions": (*inquiry.questions, *gap_questions)}
                )
                try:
                    gap_generation = self._needs.generate(
                        inquiry=reviewer_inquiry,
                        inquiry_ref=inquiry_ref,
                        review=plan_review,
                        review_ref=plan_review_ref,
                        world=world,
                        run_id=request.run_id,
                        task_id=request.task_id,
                        reviewer_bound=True,
                    )
                    if not gap_generation.needs:
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            proposal=proposal,
                            plan_review_ref=plan_review_ref,
                            diagnostics=("NO_VALID_REVIEWER_MEMORY_NEEDS",),
                        )
                    gap_memory = await self._resolve_memory(
                        request=request,
                        needs=gap_generation.needs,
                        text_root=text_root,
                        suffix=f"reviewer-{reviewer_memory_rounds + 1}",
                    )
                    gap_memory_context = gap_memory.context
                    gap_memory_context_ref = gap_memory.frozen_context_artifact
                except (PlanningInquiryNeedError, MemoryGatewayBlockedError):
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=("REVIEWER_MEMORY_BLOCKED",),
                    )
                stop_reason = gap_memory.selected_result.stop_reason
                partial_gap_memory = self._partial_memory_context(gap_memory)
                if stop_reason is ControllerStopReason.BUDGET_EXHAUSTED:
                    if partial_gap_memory is None:
                        event_refs.append(
                            self._checkpoint(
                                request,
                                PlanningLoopPhase.PLAN_REVIEWED,
                                inquiry_ref=inquiry_ref,
                                inquiry_review_ref=inquiry_review_ref,
                                memory_context_ref=memory_context_ref,
                                planner_context_ref=planner_context_ref,
                                proposal_ref=proposal_ref,
                                plan_review_ref=plan_review_ref,
                                execution_ref=execution_ref,
                                plan_blocking_signature=recorded_signature(plan_review),
                                inquiry_revisions_used=inquiry_revisions,
                                plan_revisions_used=plan_revisions,
                                reviewer_memory_rounds_used=reviewer_memory_rounds,
                                reviewer_memory_review_ids=self._ordered_ids(
                                    handled_memory_reviews
                                ),
                                reviewer_context_refs=tuple(reviewer_context_refs),
                                problem_identity_seed=problem_identity_seed,
                                **progress_updates(),
                            )
                        )
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.YIELDED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            proposal=proposal,
                            plan_review_ref=plan_review_ref,
                            diagnostics=("REVIEWER_MEMORY_BUDGET_EXHAUSTED",),
                        )
                    gap_memory_context, gap_memory_context_ref = partial_gap_memory
                    stop_reason = ControllerStopReason.NO_ADDITIONAL_EVIDENCE
                gap_trace_by_need = {
                    trace.need_id: trace for trace in gap_memory.context.retrieval_traces
                }
                unsupported_mandatory_facets = any(
                    not mandatory_facet_receipts_supported(
                        need, gap_trace_by_need.get(need.need_id)
                    )
                    for need in gap_generation.needs
                    if need.requirement.value == "mandatory"
                )
                reviewer_memory_unresolved = bool(
                    plan_review.memory_gap_questions
                    and (
                        stop_reason is ControllerStopReason.NO_ADDITIONAL_EVIDENCE
                        or (unsupported_mandatory_facets and partial_gap_memory is None)
                    )
                )
                if reviewer_memory_unresolved:
                    if any(
                        issue.blocking and issue.kind is not ReviewIssueKind.MEMORY_GAP
                        for issue in plan_review.issues
                    ):
                        return self._terminal(
                            request,
                            PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                            event_refs,
                            inquiry_ref=inquiry_ref,
                            inquiry_review_ref=inquiry_review_ref,
                            memory_context_ref=memory_context_ref,
                            planner_context_ref=planner_context_ref,
                            proposal=proposal,
                            plan_review_ref=plan_review_ref,
                            diagnostics=("REVIEWER_MANDATORY_MEMORY_FACETS_UNRESOLVED",),
                        )
                    unresolved_details = tuple(
                        (
                            "reviewer-memory."
                            + content_id(
                                {
                                    "proposal": proposal.proposal_id.root,
                                    "question": question,
                                }
                            ).root.removeprefix("sha256:")[:24],
                            question,
                            ("reviewer_memory_gap",),
                        )
                        for question in plan_review.memory_gap_questions
                    )
                    markers = _unsupported_memory_gap_markers(unresolved_details)
                    assert execution_ref is not None
                    execution = _retain_unsupported_memory_gaps(
                        self._read(execution_ref, PlannerExecutionResult),
                        unresolved_details,
                        affected_chapters=(
                            tuple(range(request.horizon_start, request.horizon_end + 1))
                            if request.horizon_start is not None and request.horizon_end is not None
                            else ()
                        ),
                        source_artifact_refs=tuple(
                            dict.fromkeys(
                                (
                                    plan_review_ref,
                                    proposal_ref,
                                    planner_context_ref,
                                    projection.view_ref,
                                )
                            )
                        ),
                    )
                    proposal = execution.plan_proposal
                    proposal_ref = self._persist_proposal(proposal)
                    execution_ref = self._artifacts.put(
                        canonical_json_bytes(execution.model_dump(mode="json")),
                        "application/vnd.novel-agent.planner-execution-result+json",
                        self._schema_version,
                    )
                    plan_review = _reviewer_memory_gap_advisory(
                        plan_review,
                        proposal_ref=proposal_ref,
                        source_review_ref=plan_review_ref,
                        markers=markers,
                    )
                    plan_review_ref = self._artifacts.put(
                        canonical_json_bytes(plan_review.model_dump(mode="json")),
                        "application/vnd.novel-agent.plan-review+json",
                        self._schema_version,
                    )
                    planner_execution_lineage_refs = (
                        proposal_ref,
                        plan_review_ref,
                        execution_ref,
                    )
                    advisory_diagnostics = ("REVIEWER_MEMORY_UNRESOLVED_ADVISORY",)
                    break
                if stop_reason not in {
                    ControllerStopReason.SUFFICIENT,
                    ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
                }:
                    terminal = (
                        PlanningLoopTerminal.PLAN_CONFLICT
                        if stop_reason is ControllerStopReason.CONFLICT_REQUIRES_REVIEW
                        else PlanningLoopTerminal.REVIEW_REVISION_REQUIRED
                    )
                    return self._terminal(
                        request,
                        terminal,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=gap_memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=(stop_reason.value,),
                    )
                try:
                    gap_context, gap_context_ref = self._assembler.assemble(
                        request=request,
                        inquiry=reviewer_inquiry,
                        inquiry_ref=inquiry_ref,
                        stage1_context=gap_memory_context,
                        stage1_context_ref=gap_memory_context_ref,
                    )
                    projection = self._context_runtime.append_delta(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        delta_ref=gap_context_ref,
                    )
                except (PlannerContextAssemblyError, PlannerContextRuntimeFailure):
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.SUSPENDED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=gap_memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=("REVIEWER_CONTEXT_SUSPENDED",),
                    )
                if projection.suspended:
                    return self._terminal(
                        request,
                        PlanningLoopTerminal.SUSPENDED,
                        event_refs,
                        inquiry_ref=inquiry_ref,
                        inquiry_review_ref=inquiry_review_ref,
                        memory_context_ref=gap_memory_context_ref,
                        planner_context_ref=planner_context_ref,
                        proposal=proposal,
                        plan_review_ref=plan_review_ref,
                        diagnostics=(projection.suspension_reason or "REVIEWER_CONTEXT_SUSPENDED",),
                    )
                planner_context = self._merge_context_metadata(planner_context, gap_context)
                reviewer_memory_rounds += 1
                reviewer_memory_this_slice += 1
                handled_memory_reviews.add(plan_review.review_id)
                reviewer_context_refs.append(gap_context_ref)

            # A revision that reproduces exactly the blocking findings the host already
            # rejected is not progress.  Requiring it again would keep spending real
            # model calls across slices, because the per-slice revision allowance
            # resets while the findings do not change.
            if revision_repeats_pending_problem(plan_review):
                return plan_revision_no_progress()
            parent_proposal = proposal
            parent_proposal_ref = proposal_ref
            instruction = plan_review.revision_instruction or "bounded Plan revision"
            if out_of_scope:
                # The previous revision moved items nobody asked about; name them so
                # this revision restores them instead of rewriting the whole plan.
                instruction = (
                    instruction
                    + " 上一轮修订改动了未被点名的条目，必须恢复原样: "  # noqa: RUF001
                    + ", ".join(out_of_scope)
                )
            plan_revisions += 1
            plan_revisions_this_slice += 1
            attempt = plan_revisions + 1
            revised, _call = await self._planner.run(
                version=self._schema_version,
                task=request.task,
                source_payload=(
                    build_planner_source_payload(projection.rendered_context)
                    + f"\nREVIEW_REVISION={instruction}\n"
                    "REVISION_SCOPE=只修改 REVIEW 点名条目/字段；其余条目的 payload 必须与 "  # noqa: RUF001
                    "PARENT_PROPOSAL 逐字一致，不得重写整份计划。\n"  # noqa: RUF001
                    f"PARENT_CANDIDATE_HASH={parent_proposal.proposal_id.root}\n"
                    f"REVIEW={plan_review.model_dump_json()}\n"
                    f"PARENT_PROPOSAL={parent_proposal.model_dump_json()}"
                ),
                source_artifacts=visible_author_artifacts,
                trusted_context_artifacts=planner_trusted_context_artifacts(
                    planner_context_ref,
                    projection.view_ref,
                    plan_review_ref,
                    *reviewer_context_refs,
                ),
                reviewed_inquiry_ref=inquiry_ref,
                memory_need_ids=planner_context.need_ids,
                evidence_refs=planner_context.evidence_refs,
                graph_path_receipt_refs=planner_context.graph_path_receipt_refs,
                parent_proposal_id=parent_proposal.proposal_id,
                committed_text_cutoff=committed_text_cutoff,
                request=model_request("plan_revision", request.task.mode, attempt),
                allowed_skill_ids=planner_skill_allowlist(include_alternative=True),
            )
            record_model_call(_call)
            raw_proposal = revised.plan_proposal
            # The scope comes from the verified blocking findings and nothing else.
            # A review with no usable finding authorises no change at all, which is
            # why the composition returns the parent unchanged instead of the model's
            # whole-plan rewrite.
            scope = revision_scope(plan_review)
            proposal = compose_scoped_revision(parent_proposal, raw_proposal, scope)
            proposal_ref = self._persist_proposal(proposal)
            raw_execution_ref = self._artifacts.put(
                canonical_json_bytes(revised.model_dump(mode="json")),
                "application/vnd.novel-agent.planner-execution-result+json",
                self._schema_version,
            )
            out_of_scope = out_of_scope_items(parent_proposal, raw_proposal, scope)
            # The composed candidate is not the model's output, so it gets its own
            # execution record and a proof tying it to the parent, the raw execution
            # and the review.  Without this the formal materializer finds no execution
            # whose proposal matches the composed candidate and refuses a candidate
            # that review has already accepted.
            proof = build_composition_proof(
                parent_ref=parent_proposal_ref,
                raw_execution_ref=raw_execution_ref,
                review_ref=plan_review_ref,
                scope=scope,
                composed=proposal,
                out_of_scope=out_of_scope,
            )
            proof_ref = self._artifacts.put(
                canonical_json_bytes(proof.model_dump(mode="json")),
                "application/vnd.novel-agent.plan-composition-proof+json",
                self._schema_version,
            )
            execution_ref = self._artifacts.put(
                canonical_json_bytes(
                    revised.model_copy(
                        update={
                            "plan_proposal": proposal,
                            "composition_proof": proof_ref,
                            "raw_plan_proposal": raw_proposal,
                        }
                    ).model_dump(mode="json")
                ),
                "application/vnd.novel-agent.planner-execution-result+json",
                self._schema_version,
            )
            planner_execution_lineage_refs = (
                parent_proposal_ref,
                proposal_ref,
                plan_review_ref,
                raw_execution_ref,
                proof_ref,
                execution_ref,
            )
            if out_of_scope:
                event_refs.append(
                    self._event(
                        request,
                        PlanningLoopPhase.PLAN_REVIEWED,
                        "plan.revision_out_of_scope: " + ", ".join(out_of_scope),
                        (proposal_ref,),
                    )
                )
            if self._same_proposal_content(parent_proposal, proposal):
                return self._terminal(
                    request,
                    PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
                    event_refs,
                    inquiry_ref=inquiry_ref,
                    inquiry_review_ref=inquiry_review_ref,
                    memory_context_ref=memory_context_ref,
                    planner_context_ref=planner_context_ref,
                    proposal=proposal,
                    plan_review_ref=plan_review_ref,
                    diagnostics=("PLAN_REVISION_NO_PROGRESS",),
                )
            plan_review, plan_review_ref, _call = await self._reviewer.review(
                version=self._schema_version,
                mode=request.task.mode,
                target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                target_payload=proposal.model_dump_json(),
                target_artifact=proposal_ref,
                trusted_source_artifacts=(
                    *visible_author_artifacts,
                    planner_context_ref,
                    projection.view_ref,
                    *(
                        (controlled_revision_evidence_ref,)
                        if controlled_revision_evidence_ref is not None
                        else ()
                    ),
                ),
                request=model_request("plan_rereview", request.task.mode, attempt),
                base_commit=request.task.base_commit,
            )
            record_model_call(_call)
            # The slice budget is checked at the top of this loop, so a repeat has to
            # be recognised here or the next slice would start it over.
            if revision_repeats_pending_problem(plan_review):
                return plan_revision_no_progress()
        event_refs.append(
            self._event(
                request,
                PlanningLoopPhase.PLAN_REVIEWED,
                "plan.review_settled",
                planner_execution_lineage_refs,
            )
        )
        event_refs.append(
            self._checkpoint(
                request,
                PlanningLoopPhase.PLAN_REVIEWED,
                inquiry_ref=inquiry_ref,
                inquiry_review_ref=inquiry_review_ref,
                memory_context_ref=memory_context_ref,
                planner_context_ref=planner_context_ref,
                proposal_ref=proposal_ref,
                plan_review_ref=plan_review_ref,
                execution_ref=execution_ref,
                plan_blocking_signature=recorded_signature(plan_review),
                inquiry_revisions_used=inquiry_revisions,
                plan_revisions_used=plan_revisions,
                reviewer_memory_rounds_used=reviewer_memory_rounds,
                reviewer_memory_review_ids=self._ordered_ids(handled_memory_reviews),
                reviewer_context_refs=tuple(reviewer_context_refs),
                problem_identity_seed=problem_identity_seed,
                **progress_updates(),
            )
        )
        if plan_review.decision is ReviewDecision.HUMAN_REQUIRED:
            terminal = PlanningLoopTerminal.HUMAN_REQUIRED
        elif plan_review.decision is not ReviewDecision.ACCEPT:
            terminal = PlanningLoopTerminal.REVIEW_REVISION_REQUIRED
        else:
            terminal = PlanningLoopTerminal.PLAN_CANDIDATE_READY
        return self._terminal(
            request,
            terminal,
            event_refs,
            inquiry_ref=inquiry_ref,
            inquiry_review_ref=inquiry_review_ref,
            memory_context_ref=memory_context_ref,
            planner_context_ref=planner_context_ref,
            proposal=proposal,
            plan_review_ref=plan_review_ref,
            diagnostics=advisory_diagnostics,
        )

    async def _resolve_memory(
        self,
        *,
        request: PlanningLoopRequest,
        needs: tuple[Stage1MemoryNeed, ...],
        text_root: TextRootDocument,
        suffix: str,
    ) -> MemoryGatewayResult:
        typed_needs = needs
        assert request.task.base_commit is not None and request.snapshot_id is not None
        identity = content_id(
            {
                "request": request.request_id.root,
                "suffix": suffix,
                "needs": tuple(item.need_id.root for item in typed_needs),
            }
        ).root.removeprefix("sha256:")[:24]
        memory_request = MemoryResolutionRequest(
            request_id=StableId(f"memory-resolution.stage4.{identity}"),
            run_id=request.run_id,
            task_id=request.task_id,
            project_id=request.project_id,
            base_commit=request.task.base_commit,
            snapshot_id=request.snapshot_id,
            required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
            task_contract=f"stage4:{request.task.mode.value}:{suffix}",
            initial_memory_needs=typed_needs,
            worldline="main",
            narrative_chapter=request.horizon_end or request.horizon_start or 0,
            access_scope=AccessScope.AUTHOR_PLANNING,
            allow_future_plan=any(item.retrieval_may_return_plan for item in typed_needs),
            retrieval_budget=request.budgets.retrieval,
            context_budget=request.budgets.context,
        )
        if isinstance(self._memory, MemoryGateway):
            return await self._memory.resolve_async(
                memory_request,
                text_root,
                thread_id=request.run_id.root,
            )
        return self._memory.resolve(
            memory_request,
            text_root,
            thread_id=request.run_id.root,
        )

    def _persist_proposal(self, proposal: PlanProposal) -> ArtifactRef:
        return self._artifacts.put(
            canonical_json_bytes(proposal.model_dump(mode="json")),
            "application/vnd.novel-agent.plan-proposal+json",
            self._schema_version,
        )

    @staticmethod
    def _visible_author_intent_artifacts(
        request: PlanningLoopRequest,
    ) -> tuple[ArtifactRef, ...]:
        return request.author_intent_artifacts

    def _source_payload(self, artifacts: tuple[ArtifactRef, ...]) -> str:
        return "\n\n".join(self._source_parts(artifacts))

    def _planner_source_payload(
        self,
        rendered_context: str,
        author_artifacts: tuple[ArtifactRef, ...],
        revision_artifacts: tuple[ArtifactRef, ...] = (),
        *,
        revision_review_artifacts: tuple[ArtifactRef, ...] = (),
        revision_parent: PlanProposal | None = None,
        revision_review: OperatorReviewEvidence | None = None,
    ) -> str:
        """Keep the complete author authority in every Planner model prompt.

        The Context Runtime projection is a compact view and source artifacts are
        otherwise only recorded as lineage.  Planner calls therefore append the
        verified author text when the projection does not already contain every
        complete source artifact.
        """

        author_parts = self._source_parts(author_artifacts)
        payload = rendered_context
        if (
            revision_parent is None
            and author_parts
            and not all(part in rendered_context for part in author_parts)
        ):
            authority = "\n\n".join(author_parts)
            payload = f"{payload}\n\n<AUTHOR_AUTHORITY_TEXT>\n{authority}\n</AUTHOR_AUTHORITY_TEXT>"
        feedback_artifacts = tuple(
            ref
            for ref in revision_artifacts
            if ref.media_type == "application/vnd.novel-agent.plan-review-draft+json"
        )
        directive_parts = self._source_parts(
            tuple(ref for ref in revision_artifacts if ref not in feedback_artifacts)
        )
        if directive_parts:
            directives = "\n\n".join(directive_parts)
            payload = (
                f"{payload}\n\n<CONTROLLED_REVISION_DIRECTIVES>\n{directives}"
                "\n</CONTROLLED_REVISION_DIRECTIVES>"
            )
        feedback_parts = self._source_parts(feedback_artifacts)
        if feedback_parts:
            payload = (
                f"{payload}\n\n<HOST_MECHANICAL_RECOVERY_FEEDBACK "
                'authority="host-validation" narrative_authority="none">\n'
                "The previous candidate failed these deterministic host checks. "
                "Return a complete replacement candidate and repair every named field. "
                "This feedback grants no story facts and no waiver. A chapter after "
                "chapter 1 may use NOT_REQUIRED only when trusted context contains an "
                "exact host-issued approval receipt and waiver_ref; never invent or "
                "extend a first-chapter waiver. For chapter 1, either omit "
                "history_retrieval so the host supplies it, or use exactly "
                "requirement=NOT_REQUIRED, reason_code=first_chapter, and "
                "waiver_ref=waiver.history.first_chapter.\n"
                + "\n\n".join(feedback_parts)
                + "\n</HOST_MECHANICAL_RECOVERY_FEEDBACK>"
            )
        review_parts = self._source_parts(revision_review_artifacts)
        if review_parts:
            payload = (
                f'{payload}\n\n<HOST_REVISION_REVIEW authority="control">\n'
                "宿主审查工件只定义本次修订边界；它不是作者事实、Memory 证据或模型 Reviewer "  # noqa: RUF001
                "结论。严格按其中的稳定 unresolved issue_id 生成 MODIFY，禁止为同一问题生成新的 "  # noqa: RUF001
                "ADD 身份；最终候选仍会接受独立 Reviewer。\n"  # noqa: RUF001
                + "\n\n".join(review_parts)
                + "\n</HOST_REVISION_REVIEW>"
            )
        if revision_parent is not None:
            payload += self._revision_parent_payload(revision_parent, revision_review)
        return payload

    @staticmethod
    def _revision_parent_payload(
        parent: PlanProposal,
        review: OperatorReviewEvidence | None = None,
    ) -> str:
        """Expose the bounded parent baseline needed for a complete revision output.

        The parent is control data, not a second author source.  The host restores every
        out-of-scope byte, so the model only needs stable issue identities and the exact
        operator scope.  Repeating the complete parent proposal here made a small repair
        exceed the provider context budget before planning began.
        """

        unresolved = tuple(
            {
                "issue_id": issue.issue_id.root,
                "operation": issue.operation.value,
                "kind": issue.kind.value,
                "summary": issue.summary,
                "affected_chapters": issue.affected_chapters,
                "blocking": issue.blocking,
            }
            for issue in parent.unresolved
        )
        scope = None if review is None else operator_revision_scope(review).model_dump(mode="json")
        scope_json = canonical_json_bytes(scope).decode("utf-8")
        return (
            '\n\n<REVISION_PARENT_IDENTITY authority="control">\n'
            f"parent_proposal_id={parent.proposal_id.root}\n"
            "以下是宿主组合的父候选 unresolved 身份表，不是可自由改写的作者输入。"  # noqa: RUF001
            "模型输出不得填写 issue_id（该字段由宿主生成）；需要修复的既有 issue 必须"  # noqa: RUF001
            "输出 operation=MODIFY，并把 parent_issue_id 原样填写为表中的既有 issue_id；"  # noqa: RUF001
            "不得把既有 issue 改写成 ADD 或另造同义 ID。"
            "父候选中未被宿主点名的条目由宿主按原字节恢复，模型可以省略它们。\n"  # noqa: RUF001
            f"UNRESOLVED_IDENTITIES={unresolved}\n"
            "</REVISION_PARENT_IDENTITY>"
            '\n\n<REVISION_PARENT_SCOPE authority="control">\n'
            "只输出 CONTROLLED_REVISION_DIRECTIVES 授权的 item/字段；其他字段和 unresolved "  # noqa: RUF001
            "由宿主从父候选恢复。不要为读取父候选字段发起 REQUEST_MEMORY。\n"
            f"REVISION_SCOPE_JSON={scope_json}\n"
            "</REVISION_PARENT_SCOPE>"
        )

    def _source_parts(self, artifacts: tuple[ArtifactRef, ...]) -> tuple[str, ...]:
        parts: list[str] = []
        for artifact in artifacts:
            try:
                parts.append(self._artifacts.read_verified(artifact).decode("utf-8"))
            except UnicodeDecodeError as error:
                raise ValueError("Planner author source is not UTF-8") from error
        return tuple(parts)

    @staticmethod
    def _world_entity_label_payload(world: WorldRootDocument) -> str:
        catalogue = tuple(
            {
                "entity_id": entity.entity_id.root,
                "entity_type": entity.entity_type,
                "internal_label": entity.internal_label,
                "aliases": entity.aliases,
            }
            for entity in world.entities
        )
        return "WORLD_ENTITY_LABELS_JSON=" + canonical_json_bytes(catalogue).decode("utf-8")

    @staticmethod
    def _controlled_revision_evidence_payload(
        world: WorldRootDocument,
        parent: PlanProposal,
        review: OperatorReviewEvidence,
    ) -> dict[str, object]:
        """Project bounded owner evidence without opening historical Memory.

        A revision scope says which bytes may change; it does not establish the
        replacement values.  This projection keeps the accepted World identity map
        and the authorised parent's neighbouring ARC semantics together in one
        content-addressed artifact.  It is deliberately compact and deterministic so
        a resumed checkpoint sees the same evidence and the Planner cannot confuse an
        obligation owner with its grantor or an information holder.
        """

        scope = operator_revision_scope(review)
        target_ids = {target.item_id for target in scope.targets}

        def explicitly_named_subject_ids(summary: object) -> tuple[str, ...]:
            if not isinstance(summary, str):
                return ()
            folded = summary.casefold()
            return tuple(
                entity.entity_id.root
                for entity in world.entities
                if any(
                    label.strip() and label.casefold() in folded
                    for label in (entity.internal_label, *entity.aliases)
                )
            )

        item_context: tuple[dict[str, object], ...] = tuple(
            {
                "item_id": item.item_id.root,
                "kind": item.kind,
                "responsibility_context": {
                    field: (
                        tuple(
                            {
                                "index": index,
                                "kind": entry.get("kind"),
                                "summary": entry.get("summary"),
                                "explicitly_named_subject_ids": explicitly_named_subject_ids(
                                    entry.get("summary")
                                ),
                            }
                            for index, entry in enumerate(value)
                            if isinstance(entry, dict)
                        )
                        if field == "obligation_plan" and isinstance(value, list)
                        else value
                    )
                    for field in _ARC_RESPONSIBILITY_CONTEXT_FIELDS
                    if (value := item.payload.get(field)) is not None
                },
            }
            for item in parent.items
            if item.item_id in target_ids
        )
        semantic_text = canonical_json_bytes(item_context).decode("utf-8").casefold()
        relevant_entity_ids = {
            entity.entity_id
            for entity in world.entities
            if any(
                label.strip() and label.casefold() in semantic_text
                for label in (entity.internal_label, *entity.aliases)
            )
        }
        matching_entities = tuple(
            entity for entity in world.entities if entity.entity_id in relevant_entity_ids
        )
        selected_entities = (matching_entities if matching_entities else world.entities)[
            :_MAX_CONTROLLED_REVISION_ENTITIES
        ]
        entity_catalogue = tuple(
            {
                "entity_id": entity.entity_id.root,
                "entity_type": entity.entity_type,
                "internal_label": entity.internal_label,
                "aliases": entity.aliases,
                "identity_invariants": entity.identity_invariants[:4],
            }
            for entity in selected_entities
        )
        relevant_states = tuple(
            {
                "subject_id": state.subject_id.root,
                "predicate": state.predicate,
                "value": state.value,
                "truth_class": state.truth_class.value,
            }
            for state in world.states
            if state.subject_id in relevant_entity_ids
        )[:_MAX_CONTROLLED_REVISION_STATES]
        relevant_relations = tuple(
            {
                "subject_id": relation.subject_id.root,
                "predicate": relation.predicate,
                "object_id": relation.object_id.root,
                "truth_class": relation.truth_class.value,
            }
            for relation in world.relations
            if relation.subject_id in relevant_entity_ids
            or relation.object_id in relevant_entity_ids
        )[:_MAX_CONTROLLED_REVISION_RELATIONS]
        return {
            "evidence_version": "controlled-revision-evidence.v2",
            "source_commit": world.source_commit.root,
            "source_world_root": world.root_hash.root,
            "parent_proposal_id": parent.proposal_id.root,
            "revision_scope": scope.model_dump(mode="json"),
            "owner_ids_semantics": OBLIGATION_OWNER_SEMANTICS,
            "owner_role_rules": (
                "Do not diversify owner_ids for variety. Bind every obligation to its "
                "actual continuing narrative subject; repeated owners are valid."
            ),
            "explicit_subject_rule": (
                "When an obligation summary explicitly names a canonical subject and the "
                "parent protagonist/supporting/faction arc confirms that subject undergoes "
                "the promised transition, explicitly_named_subject_ids is sufficient owner "
                "evidence. Do not add an enabling teacher, relative, crafter, organization, "
                "or information holder unless that entity's own continuing state is part of "
                "the obligation. This is a contract decision, not a Memory question."
            ),
            "world_entities": entity_catalogue,
            "world_entity_catalogue_complete": len(selected_entities) == len(world.entities),
            "world_entity_count": len(world.entities),
            "world_entity_selection": (
                "entities whose canonical label or alias occurs in the authorized parent ARC "
                "responsibility context"
            ),
            "relevant_world_states": relevant_states,
            "relevant_world_states_truncated": len(relevant_states)
            < sum(state.subject_id in relevant_entity_ids for state in world.states),
            "relevant_world_relations": relevant_relations,
            "relevant_world_relations_truncated": len(relevant_relations)
            < sum(
                relation.subject_id in relevant_entity_ids
                or relation.object_id in relevant_entity_ids
                for relation in world.relations
            ),
            "accepted_world_obligations": tuple(
                {
                    "obligation_id": obligation.obligation_id.root,
                    "description": obligation.description,
                    "kind": obligation.kind.value,
                    "not_before_chapter": obligation.not_before_chapter,
                    "due_chapter": obligation.due_chapter,
                    "target_chapter_start": obligation.target_chapter_start,
                    "target_chapter_end": obligation.target_chapter_end,
                }
                for obligation in world.obligations
            ),
            "authorized_parent_item_context": item_context,
        }

    @staticmethod
    def _same_inquiry_content(left: PlanningInquiry, right: PlanningInquiry) -> bool:
        """Detect a reviewer loop that changed lineage but made no semantic progress."""

        def semantic(inquiry: PlanningInquiry) -> dict[str, object]:
            goal_positions = {
                goal.goal_id: position for position, goal in enumerate(inquiry.goal_proposals)
            }

            def question_semantic(question: PlanningQuestion) -> dict[str, object]:
                return {
                    "question": question.model_dump(
                        mode="json", exclude={"question_id", "goal_id"}
                    ),
                    "goal_position": goal_positions.get(question.goal_id),
                }

            return {
                "inquiry": inquiry.model_dump(
                    mode="json",
                    exclude={
                        "inquiry_id",
                        "parent_inquiry_id",
                        "generation",
                        "goal_proposals",
                        "assumptions",
                        "questions",
                    },
                ),
                "goals": tuple(
                    goal.model_dump(mode="json", exclude={"goal_id"})
                    for goal in inquiry.goal_proposals
                ),
                "assumptions": tuple(
                    question_semantic(question) for question in inquiry.assumptions
                ),
                "questions": tuple(question_semantic(question) for question in inquiry.questions),
            }

        return semantic(left) == semantic(right)

    @staticmethod
    def _same_proposal_content(left: PlanProposal, right: PlanProposal) -> bool:
        """Stop repeated review cycles when only execution lineage changed."""

        def semantic(proposal: PlanProposal) -> dict[str, object]:
            return {
                "proposal": proposal.model_dump(
                    mode="json",
                    exclude={
                        "proposal_id",
                        "receipt",
                        "parent_proposal_id",
                        "reviewer_receipt_ref",
                        "items",
                    },
                ),
                "items": tuple(
                    item.model_dump(mode="json", exclude={"item_id"}) for item in proposal.items
                ),
            }

        return semantic(left) == semantic(right)

    @staticmethod
    def _ordered_ids(values: set[StableId]) -> tuple[StableId, ...]:
        return tuple(sorted(values, key=lambda item: item.root))

    @staticmethod
    def _merge_context_metadata(
        current: PlannerContextPackage,
        added: PlannerContextPackage,
    ) -> PlannerContextPackage:
        return current.model_copy(
            update={
                "need_ids": tuple(dict.fromkeys((*current.need_ids, *added.need_ids))),
                "retrieval_unit_ids": tuple(
                    dict.fromkeys((*current.retrieval_unit_ids, *added.retrieval_unit_ids))
                ),
                "evidence_refs": tuple(
                    dict.fromkeys((*current.evidence_refs, *added.evidence_refs))
                ),
                "graph_path_receipt_refs": tuple(
                    dict.fromkeys(
                        (*current.graph_path_receipt_refs, *added.graph_path_receipt_refs)
                    )
                ),
                "expansion_receipt_refs": tuple(
                    dict.fromkeys((*current.expansion_receipt_refs, *added.expansion_receipt_refs))
                ),
            }
        )

    def _event(
        self,
        request: PlanningLoopRequest,
        phase: PlanningLoopPhase,
        event_kind: str,
        refs: tuple[ArtifactRef, ...] = (),
        *,
        payload: dict[str, object] | None = None,
    ) -> ArtifactRef:
        event_payload = cast(dict[str, JsonValue], payload or {})
        identity = content_id(
            {
                "request": request.request_id.root,
                "phase": phase.value,
                "kind": event_kind,
                "refs": tuple(item.artifact_id.root for item in refs),
                "payload": event_payload,
            }
        ).root.removeprefix("sha256:")[:24]
        event = PlanningLoopEventReceipt(
            event_id=StableId(f"planning-event.{identity}"),
            request_id=request.request_id,
            phase=phase,
            event_kind=event_kind,
            artifact_refs=refs,
            payload=event_payload,
        )
        return self._artifacts.put(
            canonical_json_bytes(event.model_dump(mode="json")),
            "application/vnd.novel-agent.planning-loop-event+json",
            self._schema_version,
        )

    def _checkpoint(
        self,
        request: PlanningLoopRequest,
        phase: PlanningLoopPhase,
        **updates: object,
    ) -> ArtifactRef:
        draft = PlanningLoopCheckpoint.model_validate(
            {
                "checkpoint_id": StableId("planning-checkpoint.pending"),
                "request_id": request.request_id,
                "phase": phase,
                "base_commit": request.task.base_commit,
                "snapshot_id": request.snapshot_id,
                "configuration_fingerprint": request.configuration_fingerprint,
                **updates,
            }
        )
        checkpoint_id = content_id(
            draft.model_dump(mode="json", exclude={"checkpoint_id"})
        ).root.removeprefix("sha256:")[:24]
        checkpoint = draft.model_copy(
            update={"checkpoint_id": StableId(f"planning-checkpoint.{checkpoint_id}")}
        )
        return self._artifacts.put(
            canonical_json_bytes(checkpoint.model_dump(mode="json")),
            PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
            self._schema_version,
        )

    def _terminal(
        self,
        request: PlanningLoopRequest,
        terminal: PlanningLoopTerminal,
        event_refs: list[ArtifactRef],
        *,
        inquiry_ref: ArtifactRef | None = None,
        inquiry_review_ref: ArtifactRef | None = None,
        memory_context_ref: ArtifactRef | None = None,
        planner_context_ref: ArtifactRef | None = None,
        proposal: PlanProposal | None = None,
        plan_review_ref: ArtifactRef | None = None,
        diagnostics: tuple[str, ...] = (),
    ) -> PlanningLoopResult:
        event_refs.append(
            self._event(
                request,
                PlanningLoopPhase.TERMINAL,
                f"terminal.{terminal.value}",
                tuple(
                    item
                    for item in (
                        inquiry_ref,
                        inquiry_review_ref,
                        memory_context_ref,
                        planner_context_ref,
                        plan_review_ref,
                    )
                    if item is not None
                ),
                payload={"diagnostic_codes": list(diagnostics)} if diagnostics else None,
            )
        )
        return PlanningLoopResult(
            request_id=request.request_id,
            terminal=terminal,
            inquiry_ref=inquiry_ref,
            inquiry_review_ref=inquiry_review_ref,
            memory_context_ref=memory_context_ref,
            planner_context_ref=planner_context_ref,
            proposal=proposal,
            plan_review_ref=plan_review_ref,
            event_artifacts=tuple(event_refs),
            diagnostic_codes=diagnostics,
            degraded=terminal is PlanningLoopTerminal.DEGRADED_NOT_PROMOTABLE,
            round_progress=planner_round_progress(
                terminal,
                basis_commit=request.task.base_commit,
                diagnostics=diagnostics,
                remaining_work=diagnostics,
                artifact_ref=plan_review_ref or inquiry_ref,
                input_candidate_ref=inquiry_ref,
            ),
        )

    def _read(self, artifact: ArtifactRef, model: type[ModelT]) -> ModelT:
        raw = self._artifacts.read_verified(artifact)
        # JSON arrays are the wire representation of tuple fields in the frozen
        # domain artifacts.  Keep strict field/domain validation after JSON
        # decoding, while allowing Pydantic's JSON boundary to materialize tuples.
        return model.model_validate_json(raw, strict=False)

    def _read_planner_context(self, artifact: ArtifactRef) -> PlannerContextPackage:
        return self._read(artifact, PlannerContextPackage)
