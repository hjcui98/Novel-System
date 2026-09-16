"""Bounded source coverage and continuation for ordinary chapter curation.

Ported from the fixed blind fix ``b79a751`` (remediation plan R4.1).  One Curator
response may carry at most four operations, which is a *response* limit and never a
chapter capacity: this module slices the chapter into bounded source units, requests
continuation pages until each slice is exhausted, and aggregates the whole chapter into
one operation set before the host validates and commits it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from novel_agent.adapters.model.openai_chat import OpenAIChatOutputLengthError
from novel_agent.domain.benchmark import ChapterDocument
from novel_agent.domain.changes import (
    ChangeOperationType,
    CuratorObligationRecord,
    CuratorV2EvidenceDraft,
    CuratorV2OperationDraft,
    OrdinaryCurationPageReceipt,
    PlannedObligationObservation,
    WorldRecordKind,
)
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory import PlanObligation, WorldRootDocument
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.services.content_addressing import content_id
from novel_agent.services.model_call_ledger import bounded_model_request_id
from novel_agent.services.model_gateway import ModelGateway, ModelOutputBudgetExhausted

# One bounded attempt may pay for this many previously unanswered pages per source
# batch.  Pages settled by an earlier attempt are replayed from the durable ledger for
# free, so a retry continues past them instead of paying for the same work again.
_DEFAULT_ORDINARY_PAGE_QUOTA = 16
_PLAN_PROGRESS_RANK = {"not_observed": 0, "progressed": 1, "resolved": 2, "abandoned": 2}


class OrdinaryCurationIncomplete(ValueError):
    """Source work cannot be declared complete within the current bounded attempt."""


def _record_page_attempt(
    record: ModelCallRecord,
    *,
    calls: list[ModelCallRecord],
    billed: set[str],
    skip: set[str],
) -> int:
    key = record.request_id.root
    if key in billed or key in skip:
        return 0
    billed.add(key)
    calls.append(record)
    return record.usage.input_tokens + record.usage.output_tokens


def _bill_page_attempts(
    gateway: ModelGateway,
    page_request_id: StableId,
    *,
    calls: list[ModelCallRecord],
    billed: set[str],
    skip: set[str],
    answer: ModelCallRecord | None = None,
) -> int:
    """Append and charge every provider attempt of one page exactly once.

    Truncated attempts, schema retries and terminal failures all carry a call record
    in the durable ledger, so they are billed from there instead of from the final
    successful answer.  Attempts settled before this pass (``skip``) were already
    charged by the attempt that owned them.  The answered record is the authority for
    the page's own identity, so it is billed even when no ledger entry was visible.
    """

    total = sum(
        _record_page_attempt(record, calls=calls, billed=billed, skip=skip)
        for record in gateway.attempts_for(page_request_id.root)
    )
    if answer is not None:
        total += _record_page_attempt(answer, calls=calls, billed=billed, skip=skip)
    return total


@dataclass(frozen=True)
class SourceUnit:
    unit_id: str
    block_id: str
    text: str


def source_batches(chapter: ChapterDocument) -> tuple[tuple[SourceUnit, ...], ...]:
    batches: list[tuple[SourceUnit, ...]] = []
    pending: list[SourceUnit] = []
    size = 0
    for scene in chapter.scenes:
        for block in scene.blocks:
            start = 0
            while start < len(block.text):
                end = min(start + 2400, len(block.text))
                if end < len(block.text):
                    boundaries = list(
                        re.finditer(r"[。\uff01\uff1f.!?]\s*|\n", block.text[start + 1200 : end])
                    )
                    if boundaries:
                        end = start + 1200 + boundaries[-1].end()
                fragment = block.text[start:end]
                if pending and size + len(fragment) > 5000:
                    batches.append(tuple(pending))
                    pending, size = [], 0
                pending.append(
                    SourceUnit(
                        f"{block.block_id.root}:{start}:{start + len(fragment)}",
                        block.block_id.root,
                        fragment,
                    )
                )
                size += len(fragment)
                if end == len(block.text):
                    break
                # The next window retains local context across a split; operation deduplication
                # and the full-source grounder still own identity and exact spans.
                start = max(start + 1, end - 256)
        if pending:
            batches.append(tuple(pending))
            pending, size = [], 0
    return tuple(batches)


def world_working_view(
    world: WorldRootDocument,
    source_text: str,
    lookup_terms: tuple[str, ...] = (),
) -> dict[str, object]:
    """Select a bounded model working set; callers still validate against the full World."""
    folded = source_text.casefold()
    terms = {term.casefold() for term in lookup_terms}
    named = [
        entity
        for entity in world.entities
        if any(
            label and (label.casefold() in folded or label.casefold() in terms)
            for label in (entity.entity_id.root, entity.internal_label, *entity.aliases)
        )
    ]
    if len(named) > 80:
        raise OrdinaryCurationIncomplete("source unit mentions more than 80 entities; split it")
    entities = {item.entity_id for item in named}
    result: dict[str, object] = {"entities": [item.model_dump(mode="json") for item in named]}
    omitted: dict[str, int] = {}
    for name in ("states", "events", "relations", "obligations"):
        records = getattr(world, name)
        selected = []
        exact = []
        for item in records:
            raw = item.model_dump(mode="json", exclude={"evidence_refs"})
            ids = {
                str(value).casefold()
                for key, value in raw.items()
                if key.endswith("_id") or key == "predicate"
            }
            involved = {getattr(item, key, None) for key in ("subject_id", "object_id")}
            involved.update(getattr(item, "participant_ids", ()))
            involved.update(getattr(item, "owner_ids", ()))
            if ids & terms:
                exact.append(raw)
            elif involved & entities:
                selected.append(raw)
        if len(exact) > 128:
            raise OrdinaryCurationIncomplete(
                "exact lookup is too broad; narrow the requested identity"
            )
        # Recent records are preferred, with exact requested records never silently dropped.
        retained = [*selected[-max(0, 128 - len(exact)) :], *exact] if len(exact) < 128 else exact
        result[name] = retained
        omitted[name] = len(records) - len(retained)
    result["omitted_record_counts"] = omitted
    result["lookup_contract"] = (
        "This is a working set, not the complete World. Use world_lookup_terms for exact "
        "entity label/id, record id or predicate before creating a potentially existing fact. "
        "The host resolves lookups against the full World; final validation uses the full World."
    )
    return result


async def extract_source_batches(
    gateway: ModelGateway,
    request: ModelRequest,
    chapter: ChapterDocument,
    world: WorldRootDocument,
    planned: tuple[PlanObligation, ...],
    *,
    base_commit: CommitId,
    cumulative_token_budget: int | None = None,
    cumulative_token_budgets: tuple[int, ...] | None = None,
    cumulative_tokens_used: int = 0,
    page_quota: int | None = None,
) -> tuple[
    CuratorV2EvidenceDraft, tuple[ModelCallRecord, ...], tuple[OrdinaryCurationPageReceipt, ...]
]:
    batches = source_batches(chapter)
    if not batches:
        raise OrdinaryCurationIncomplete("chapter contains no curation source units")
    observed_world = {item.obligation_id: item for item in world.obligations}
    active = tuple(
        item
        for item in planned
        if (item.target_chapter_start is None or item.target_chapter_start <= chapter.chapter_index)
        and (
            item.obligation_id not in observed_world
            or observed_world[item.obligation_id].status.value not in {"resolved", "abandoned"}
        )
    )
    planned_by_id = {item.obligation_id: item for item in active}
    operations: dict[str, CuratorV2OperationDraft] = {}
    plan_progress: dict[StableId, PlannedObligationObservation] = {}
    calls: list[ModelCallRecord] = []
    receipts: list[OrdinaryCurationPageReceipt] = []
    unresolved: list[str] = []
    diffs: list[str] = []
    no_op_quotes: list[str] = []
    last: CuratorV2EvidenceDraft | None = None
    used = cumulative_tokens_used
    prefix, separator, rest = request.prompt.partition('<CURATOR_INPUT trusted="false">')
    if not separator:
        raise OrdinaryCurationIncomplete("ordinary Curator request lacks its source envelope")
    _discarded, separator, suffix = rest.partition("</CURATOR_INPUT>")
    if not separator:
        raise OrdinaryCurationIncomplete("ordinary Curator source envelope is incomplete")
    page_quota = _DEFAULT_ORDINARY_PAGE_QUOTA if page_quota is None else page_quota
    if page_quota < 1:
        raise ValueError("ordinary curation page quota must be positive")
    # Every attempt this pass pays for is charged once.  Attempts that were already
    # settled when the pass started belong to an earlier attempt's receipt.
    billed: set[str] = set()
    for batch_index, batch in enumerate(batches):
        source = "\n".join(unit.text for unit in batch)
        lookups: set[str] = set()
        seen_progress: set[StableId] = set()
        new_pages = 0
        page = 0
        while True:
            missing_progress = tuple(
                item for item in active if item.obligation_id not in seen_progress
            )
            directory = missing_progress[:8]
            page_request_id = (
                request.request_id
                if batch_index == page == 0
                else bounded_model_request_id(request, f".ordinary.b{batch_index}.p{page}")
            )
            # The durable ledger is the resumable cursor: a page whose attempts are
            # already settled is replayed by ``generate_structured`` without another
            # provider call and without being billed twice.
            settled_before = {
                entry.request_id.root
                for entry in gateway.call_ledger.list_for_prefix(page_request_id.root)
                if entry.call_record is not None
            }
            if not settled_before:
                if new_pages >= page_quota:
                    raise OrdinaryCurationIncomplete(
                        f"ordinary source batch {batch_index} reached its configured quota of "
                        f"{page_quota} new pages at page {page}; every settled page stays in the "
                        "model call ledger, so a later attempt resumes past them"
                    )
                new_pages += 1
            fields = {
                "BASE_COMMIT": base_commit.root,
                "WORLD": world_working_view(world, source, tuple(sorted(lookups))),
                "CHAPTER": {
                    "chapter_index": chapter.chapter_index,
                    "source_units": [unit.__dict__ for unit in batch],
                },
                "EVIDENCE_CANDIDATES": [
                    {"block_id": unit.block_id, "text": unit.text} for unit in batch
                ],
                "PLANNED_OBLIGATIONS": [
                    item.model_dump(mode="json", exclude={"evidence_refs"}) for item in directory
                ],
                "ALREADY_EXTRACTED": [item.model_dump(mode="json") for item in operations.values()],
            }
            input_payload = (
                '<CURATOR_INPUT trusted="false">\n'
                + "\n".join(
                    key
                    + "="
                    + (
                        str(value)
                        if key == "BASE_COMMIT"
                        else json.dumps(value, ensure_ascii=False)
                    )
                    for key, value in fields.items()
                )
                + "\n</CURATOR_INPUT>"
            )
            prompt = (
                prefix
                + input_payload
                + (
                    "\n<BATCH_CONTINUATION_CONTRACT>\n"
                    "Extract only the supplied source units. At most four operations is a response "
                    "limit, not chapter capacity. Never repeat ALREADY_EXTRACTED. Set "
                    "has_more=true "
                    "until all remaining changes in these units are covered; coverage=1 means this "
                    "batch is exhausted. For each PLANNED_OBLIGATIONS entry return "
                    "plan_observations "
                    "with the exact obligation_id, status "
                    "(not_observed/progressed/resolved/abandoned), "
                    "rationale and exact source evidence_quotes for any observed progress. The "
                    "directory is intent, never evidence. Do not duplicate those identities in "
                    "operations. Empty operations are allowed for a lookup request, plan-only "
                    "progress or an exhausted batch; use no-op proof only when no change remains. "
                    "Every due milestone must have resolved evidence before chapter "
                    "settlement. If it "
                    "is not fulfilled, report not_observed/progressed honestly so the "
                    "host can return "
                    "the task for repair. Preserve cross-unit facts as unresolved rather"
                    " than inventing a missing step.\n"
                    "</BATCH_CONTINUATION_CONTRACT>"
                )
                + suffix
            )
            current = request.model_copy(
                update={
                    "request_id": page_request_id,
                    "prompt": prompt,
                    "trace_id": f"{request.trace_id}:ordinary:{batch_index}:{page}",
                }
            )
            if cumulative_token_budgets is not None:
                # ``ModelCurator`` may have preflighted the whole source envelope before
                # slicing it into pages. A page is a distinct model request, so it must
                # not inherit the parent's in-process EffectiveBudgetResult. Clear only
                # the binding marker; the caller's explicit output cap remains part of the
                # request and is resolved again against this page's prompt.
                current = current.model_copy(update={"budget_source": None})
                budget, _tier = gateway.preflight_elastic_cumulative_token_budget(
                    current,
                    token_budgets=cumulative_token_budgets,
                    tokens_used=used,
                )
                current = current.model_copy(
                    update={
                        "max_output_tokens": budget.total_output_budget,
                        "budget_source": budget.budget_source,
                    }
                )
            elif cumulative_token_budget is not None:
                current = current.model_copy(update={"budget_source": None})
                budget = gateway.preflight_cumulative_token_budget(
                    current,
                    token_budget=cumulative_token_budget,
                    tokens_used=used,
                )
                current = current.model_copy(
                    update={
                        "max_output_tokens": budget.total_output_budget,
                        "budget_source": budget.budget_source,
                    }
                )
            bound = current
            try:
                draft, call = await gateway.generate_structured(current, CuratorV2EvidenceDraft)
            except (OpenAIChatOutputLengthError, ModelOutputBudgetExhausted):
                # The page's attempts are already retained; charge them before the
                # compact retry so its own preflight sees the real spend.
                used += _bill_page_attempts(
                    gateway, page_request_id, calls=calls, billed=billed, skip=settled_before
                )
                compact = current.model_copy(
                    update={
                        "request_id": bounded_model_request_id(current, ".compact"),
                        "max_output_tokens": min(current.max_output_tokens or 8192, 8192),
                        "enable_thinking": False,
                        "thinking_token_budget": None,
                        "prompt": current.prompt
                        + '\n<COMPACT_OUTPUT_RETRY trusted="true">\nThe prior response '
                        "exhausted output capacity. "
                        "Return at most two operations now and use has_more=true for the rest. "
                        "Keep exact evidence quotes and required fields; put uncertain "
                        "details in short unresolved items. "
                        "</COMPACT_OUTPUT_RETRY>",
                    }
                )
                if cumulative_token_budgets is not None:
                    # The compact retry has its own request identity as well. It must
                    # resolve the reduced output cap before ``generate_structured``.
                    compact = compact.model_copy(update={"budget_source": None})
                    budget, _tier = gateway.preflight_elastic_cumulative_token_budget(
                        compact,
                        token_budgets=cumulative_token_budgets,
                        tokens_used=used,
                    )
                    compact = compact.model_copy(
                        update={
                            "max_output_tokens": budget.total_output_budget,
                            "budget_source": budget.budget_source,
                        }
                    )
                elif cumulative_token_budget is not None:
                    compact = compact.model_copy(update={"budget_source": None})
                    budget = gateway.preflight_cumulative_token_budget(
                        compact,
                        token_budget=cumulative_token_budget,
                        tokens_used=used,
                    )
                    compact = compact.model_copy(
                        update={
                            "max_output_tokens": budget.total_output_budget,
                            "budget_source": budget.budget_source,
                        }
                    )
                bound = compact
                draft, call = await gateway.generate_structured(
                    compact, CuratorV2EvidenceDraft, json_object_framing=True
                )
            used += _bill_page_attempts(
                gateway,
                page_request_id,
                calls=calls,
                billed=billed,
                skip=settled_before,
                answer=call,
            )
            # The requested chapter is bound by the caller's typed contract check; this
            # layer only proves that every source batch was read to the end.
            before = len(operations)
            before_ranks = {
                identity: _PLAN_PROGRESS_RANK[progress.status]
                for identity, progress in plan_progress.items()
            }
            for operation in draft.operations:
                if (
                    operation.target_id in planned_by_id
                    and operation.record_kind is WorldRecordKind.OBLIGATION
                ):
                    raise OrdinaryCurationIncomplete(
                        "planned identities must use plan_observations"
                    )
                operations[content_id(operation.model_dump(mode="json")).root] = operation
            for progress in draft.plan_observations:
                if progress.obligation_id not in {item.obligation_id for item in directory}:
                    raise OrdinaryCurationIncomplete(
                        "Curator assessed an unrequested planned identity"
                    )
                if any(
                    not any(quote in unit.text for unit in batch)
                    for quote in progress.evidence_quotes
                ):
                    raise OrdinaryCurationIncomplete(
                        "plan progress quote is outside current source units"
                    )
                old = plan_progress.get(progress.obligation_id)
                rank = _PLAN_PROGRESS_RANK
                if (
                    old is not None
                    and rank[old.status] == rank[progress.status] == 2
                    and old.status != progress.status
                ):
                    raise OrdinaryCurationIncomplete(
                        "planned obligation has conflicting terminal observations"
                    )
                if old is None or rank[progress.status] > rank[old.status]:
                    plan_progress[progress.obligation_id] = progress
                seen_progress.add(progress.obligation_id)
            newly_requested = set(draft.world_lookup_terms) - lookups
            # A repeated operation, lookup or obligation observation is not progress:
            # only new material, a new query or a raised obligation rank continues.
            progressed = (
                len(operations) > before
                or bool(newly_requested)
                or any(
                    _PLAN_PROGRESS_RANK[progress.status]
                    > before_ranks.get(progress.obligation_id, -1)
                    for progress in draft.plan_observations
                )
            )
            lookups.update(draft.world_lookup_terms)
            more = (
                draft.has_more
                or len(draft.operations) == 4
                or bool(newly_requested)
                or bool(set(planned_by_id) - seen_progress)
            )
            # Page exhaustion, not the model's self-report, is this function's
            # evidence that a batch was read to the end.  A model declaring no further
            # work while reporting coverage < 1 is contradictory, but that contract
            # defect is refused downstream by the no-durable-delta support gate.
            covered = not more and draft.coverage == 1
            receipts.append(
                OrdinaryCurationPageReceipt(
                    source_unit_ids=tuple(unit.unit_id for unit in batch),
                    source_hash=content_id(source),
                    model_request_id=call.request_id,
                    operation_count=len(draft.operations),
                    has_more=more,
                    covered=covered,
                    lookup_terms=draft.world_lookup_terms,
                    budget_source=bound.budget_source,
                    output_token_budget=bound.max_output_tokens,
                )
            )
            unresolved.extend(draft.unresolved)
            diffs.extend(draft.declared_vs_observed_diff)
            no_op_quotes.extend(draft.no_op_evidence_quotes)
            last = draft
            if not more:
                break
            if not progressed:
                raise OrdinaryCurationIncomplete("Curator continuation made no progress")
            page += 1
    assert last is not None
    missing_due = [
        item.obligation_id.root
        for item in active
        if item.obligation_id.root.startswith("milestone.")
        and item.due_chapter is not None
        and item.due_chapter <= chapter.chapter_index
        and (
            item.obligation_id not in plan_progress
            or plan_progress[item.obligation_id].status != "resolved"
        )
    ]
    if missing_due:
        raise OrdinaryCurationIncomplete(
            "due plan milestones lack completion evidence: " + ", ".join(missing_due)
        )
    for identity, progress in plan_progress.items():
        if progress.status == "not_observed":
            continue
        intent = planned_by_id[identity]
        if progress.status == "resolved" and intent.is_future_locked(chapter.chapter_index):
            raise OrdinaryCurationIncomplete("planned obligation resolved before its time lock")
        raw = intent.model_dump(mode="json", exclude={"obligation_id", "evidence_refs"})
        raw["status"] = progress.status
        operation = CuratorV2OperationDraft(
            operation=ChangeOperationType.REPLACE
            if identity in observed_world
            else ChangeOperationType.CREATE,
            record_kind=WorldRecordKind.OBLIGATION,
            target_id=identity,
            record=CuratorObligationRecord.model_validate_json(json.dumps(raw)),
            evidence_quotes=progress.evidence_quotes,
        )
        operations[content_id(operation.model_dump(mode="json")).root] = operation
    # Only the host aggregates beyond the model's four-operation response schema.
    return (
        last.model_copy(
            update={
                "operations": tuple(
                    sorted(
                        operations.values(),
                        key=lambda item: item.record_kind is not WorldRecordKind.ENTITY,
                    )
                ),
                # Source coverage is proven by page exhaustion; the model's own coverage
                # self-report is carried through unchanged so the host support gate can
                # still refuse a chapter whose draft admitted it was incomplete.
                "coverage": last.coverage,
                "has_more": False,
                "world_lookup_terms": (),
                "plan_observations": tuple(plan_progress.values()),
                "unresolved": tuple(dict.fromkeys(unresolved)),
                "declared_vs_observed_diff": tuple(dict.fromkeys(diffs)),
                "no_durable_delta_reason": None if operations else last.no_durable_delta_reason,
                "no_op_evidence_quotes": ()
                if operations
                else tuple(dict.fromkeys(no_op_quotes))[:4],
            }
        ),
        tuple(calls),
        tuple(receipts),
    )
