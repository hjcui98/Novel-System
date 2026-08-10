"""Stage 1 L1 anchor construction, evidence expansion, and context compilation."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory import (
    ContextBudgetReport,
    FusedCandidate,
    RequirementLevel,
    RetrievalTrace,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1ContextPackage,
    Stage1MemoryNeed,
    WorldRootDocument,
)
from novel_agent.domain.text import (
    EvidenceRef,
    EvidenceSupportStatus,
    TextBlock,
    TextSpanRef,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.canonical_alias_registry import CanonicalAliasRegistry
from novel_agent.services.content_addressing import quote_hash


class AnchorBuilder:
    """Build typed L1 and grounded L0 units from one immutable canonical basis."""

    def build(
        self,
        world: WorldRootDocument,
        text: TextRootDocument,
        plan: PlanRootDocument | None,
        *,
        snapshot_id: StableId,
        canonical_commit: CommitId | None = None,
    ) -> tuple[RetrievalUnit, ...]:
        basis_commit = canonical_commit or world.source_commit
        labels = {entity.entity_id: entity.internal_label for entity in world.entities}
        blocks = {
            block.block_id: block
            for scene in (
                *(text.prelude.scenes if text.prelude is not None else ()),
                *(scene for chapter in text.chapters for scene in chapter.scenes),
            )
            for block in scene.blocks
        }
        units: list[RetrievalUnit] = []
        alias_registry = CanonicalAliasRegistry()
        for state in world.states:
            canonical_value = (
                alias_registry.resolve(state.predicate, state.value)
                if isinstance(state.value, str)
                else None
            )
            units.append(
                RetrievalUnit(
                    unit_id=StableId(f"anchor.{state.state_id.root}"),
                    unit_kind=RetrievalUnitKind.STATE_ANCHOR,
                    source_commit=basis_commit,
                    snapshot_id=snapshot_id,
                    source_artifact=world.root_hash,
                    text=(
                        f"{labels[state.subject_id]} {state.predicate} "
                        f"{json.dumps(state.value, ensure_ascii=False, sort_keys=True)} "
                        f"{_evidence_snippets(state.evidence_refs, text, blocks)}"
                    ),
                    entity_ids=(state.subject_id,),
                    predicate=state.predicate,
                    canonical_value_id=(
                        None if canonical_value is None else canonical_value.canonical_value_id
                    ),
                    canonicalizer_version=(
                        None if canonical_value is None else canonical_value.canonicalizer_version
                    ),
                    story_time_start=state.valid_time.start_ordinal,
                    story_time_end=state.valid_time.end_ordinal,
                    truth_class=state.truth_class,
                    evidence_refs=state.evidence_refs,
                    mandatory=state.predicate in {"alive", "injury", "location", "owns"},
                )
            )
        for event in world.events:
            participants = " ".join(labels[identity] for identity in event.participant_ids)
            units.append(
                RetrievalUnit(
                    unit_id=StableId(f"anchor.{event.event_id.root}"),
                    unit_kind=RetrievalUnitKind.EVENT_ANCHOR,
                    source_commit=basis_commit,
                    snapshot_id=snapshot_id,
                    source_artifact=world.root_hash,
                    text=(
                        f"{participants} {event.event_type} "
                        f"{_evidence_snippets(event.evidence_refs, text, blocks)}"
                    ).strip(),
                    entity_ids=event.participant_ids,
                    predicate=event.event_type,
                    story_time_start=(
                        None if event.story_time is None else event.story_time.start_ordinal
                    ),
                    story_time_end=(
                        None if event.story_time is None else event.story_time.end_ordinal
                    ),
                    truth_class=event.truth_class,
                    evidence_refs=event.evidence_refs,
                )
            )
        for relation in world.relations:
            units.append(
                RetrievalUnit(
                    unit_id=StableId(f"anchor.{relation.relation_id.root}"),
                    unit_kind=RetrievalUnitKind.RELATION_ANCHOR,
                    source_commit=basis_commit,
                    snapshot_id=snapshot_id,
                    source_artifact=world.root_hash,
                    text=(
                        f"{labels[relation.subject_id]} {relation.predicate} "
                        f"{labels[relation.object_id]} "
                        f"{_evidence_snippets(relation.evidence_refs, text, blocks)}"
                    ),
                    entity_ids=(relation.subject_id, relation.object_id),
                    predicate=relation.predicate,
                    story_time_start=relation.valid_time.start_ordinal,
                    story_time_end=relation.valid_time.end_ordinal,
                    truth_class=relation.truth_class,
                    evidence_refs=relation.evidence_refs,
                )
            )
        for obligation in world.obligations:
            units.append(
                RetrievalUnit(
                    unit_id=StableId(f"anchor.{obligation.obligation_id.root}"),
                    unit_kind=RetrievalUnitKind.PLAN_ANCHOR,
                    source_commit=basis_commit,
                    snapshot_id=snapshot_id,
                    source_artifact=world.root_hash,
                    text=(
                        f"{obligation.kind.value} {obligation.status.value} "
                        f"{obligation.description} "
                        f"{_evidence_snippets(obligation.evidence_refs, text, blocks)}"
                    ).strip(),
                    entity_ids=obligation.owner_ids,
                    predicate=obligation.kind.value,
                    access_scope="writer_safe",
                    information_label="plan",
                    evidence_refs=obligation.evidence_refs,
                    mandatory=obligation.status.value != "resolved",
                )
            )
        if plan is not None:
            for node in plan.nodes:
                units.append(
                    RetrievalUnit(
                        unit_id=StableId(f"anchor.{node.plan_node_id.root}"),
                        unit_kind=RetrievalUnitKind.ARC_ANCHOR,
                        source_commit=basis_commit,
                        snapshot_id=snapshot_id,
                        source_artifact=plan.root_hash,
                        text=f"{node.title} {node.summary}",
                        parent_unit_id=(
                            StableId(f"anchor.{node.parent_id.root}")
                            if node.parent_id is not None
                            else None
                        ),
                        parent_unit_ids=(
                            ()
                            if node.parent_id is None
                            else (StableId(f"anchor.{node.parent_id.root}"),)
                        ),
                        access_scope="author_planning",
                        information_label="plan",
                    )
                )
            for goal in plan.chapter_goals:
                units.append(
                    RetrievalUnit(
                        unit_id=StableId(f"anchor.{goal.goal_id.root}"),
                        unit_kind=RetrievalUnitKind.PLAN_ANCHOR,
                        source_commit=basis_commit,
                        snapshot_id=snapshot_id,
                        source_artifact=plan.root_hash,
                        text=f"chapter {goal.chapter_index} {goal.summary}",
                        narrative_start=goal.chapter_index,
                        narrative_end=goal.chapter_index,
                        access_scope="author_planning",
                        information_label="plan",
                        mandatory=True,
                    )
                )
        if text.prelude is not None:
            prelude_texts: list[str] = []
            prelude_evidence: list[EvidenceRef] = []
            for scene in text.prelude.scenes:
                for block in scene.blocks:
                    prelude_texts.append(block.text)
                    evidence = _block_evidence(block, text, basis_commit)
                    prelude_evidence.append(evidence)
                    units.append(
                        RetrievalUnit(
                            unit_id=StableId(f"grounded.{block.block_id.root}"),
                            unit_kind=RetrievalUnitKind.GROUNDED_BLOCK,
                            source_commit=basis_commit,
                            snapshot_id=snapshot_id,
                            source_artifact=text.root_hash,
                            text=block.text,
                            narrative_start=block.narrative_index,
                            narrative_end=block.narrative_index,
                            evidence_refs=(evidence,),
                        )
                    )
            units.append(
                RetrievalUnit(
                    unit_id=StableId(f"anchor.{text.prelude.prelude_id.root}"),
                    unit_kind=RetrievalUnitKind.CHAPTER_ANCHOR,
                    source_commit=basis_commit,
                    snapshot_id=snapshot_id,
                    source_artifact=text.root_hash,
                    text=" ".join(prelude_texts),
                    narrative_start=0,
                    narrative_end=0,
                    evidence_refs=tuple(prelude_evidence),
                )
            )
        for chapter in text.chapters:
            chapter_texts: list[str] = []
            chapter_evidence: list[EvidenceRef] = []
            for scene in chapter.scenes:
                for block in scene.blocks:
                    chapter_texts.append(block.text)
                    evidence = _block_evidence(block, text, basis_commit)
                    chapter_evidence.append(evidence)
                    units.append(
                        RetrievalUnit(
                            unit_id=StableId(f"grounded.{block.block_id.root}"),
                            unit_kind=RetrievalUnitKind.GROUNDED_BLOCK,
                            source_commit=basis_commit,
                            snapshot_id=snapshot_id,
                            source_artifact=text.root_hash,
                            text=block.text,
                            narrative_start=block.narrative_index,
                            narrative_end=block.narrative_index,
                            evidence_refs=(evidence,),
                        )
                    )
            units.append(
                RetrievalUnit(
                    unit_id=StableId(f"anchor.{chapter.chapter_id.root}"),
                    unit_kind=RetrievalUnitKind.CHAPTER_ANCHOR,
                    source_commit=basis_commit,
                    snapshot_id=snapshot_id,
                    source_artifact=text.root_hash,
                    text=f"{chapter.title or ''} {' '.join(chapter_texts)}".strip(),
                    narrative_start=chapter.chapter_index,
                    narrative_end=chapter.chapter_index,
                    evidence_refs=tuple(chapter_evidence),
                )
            )
        if len({unit.unit_id for unit in units}) != len(units):
            raise ValueError("anchor build produced duplicate retrieval unit ids")
        return tuple(_with_content_metadata(unit) for unit in units)


class EvidenceExpander:
    def expand(
        self,
        candidates: Iterable[FusedCandidate],
        text_root: TextRootDocument,
    ) -> tuple[RetrievalUnit, ...]:
        blocks = {
            block.block_id: block
            for scene in (
                *(text_root.prelude.scenes if text_root.prelude is not None else ()),
                *(scene for chapter in text_root.chapters for scene in chapter.scenes),
            )
            for block in scene.blocks
        }
        expanded: list[RetrievalUnit] = []
        seen: set[StableId] = set()
        for candidate in candidates:
            if not candidate.selected:
                continue
            if candidate.unit.unit_kind in {
                RetrievalUnitKind.GROUNDED_BLOCK,
                RetrievalUnitKind.GROUNDED_SPAN,
            }:
                # Grounded units already are L0 evidence. Expanding their own
                # EvidenceRef creates a second unit with the same prose and
                # charges the context budget twice without adding lineage.
                continue
            for evidence in candidate.unit.evidence_refs:
                selected_text = _resolve_evidence_text(evidence, blocks, label="anchor")
                unit_id = StableId(f"expanded.{evidence.evidence_id.root}")
                if unit_id in seen:
                    continue
                seen.add(unit_id)
                expanded.append(
                    RetrievalUnit(
                        unit_id=unit_id,
                        unit_kind=RetrievalUnitKind.GROUNDED_SPAN,
                        source_commit=candidate.unit.source_commit,
                        snapshot_id=candidate.unit.snapshot_id,
                        source_artifact=text_root.root_hash,
                        text=selected_text,
                        entity_ids=candidate.unit.entity_ids,
                        parent_unit_id=candidate.unit.unit_id,
                        parent_unit_ids=(candidate.unit.unit_id,),
                        expanded_from_handle=(
                            candidate.unit.compact_handle or candidate.unit.unit_id
                        ),
                        worldline=candidate.unit.worldline,
                        narrative_start=candidate.unit.narrative_start,
                        narrative_end=candidate.unit.narrative_end,
                        truth_class=candidate.unit.truth_class,
                        access_scope=candidate.unit.access_scope,
                        information_label=candidate.unit.information_label,
                        evidence_refs=(evidence,),
                        mandatory=candidate.unit.mandatory,
                    )
                )
        return tuple(_with_content_metadata(unit) for unit in expanded)


def _with_content_metadata(unit: RetrievalUnit) -> RetrievalUnit:
    """Fill v0.2 content identity without changing a stable semantic unit id."""

    source_refs = () if unit.source_artifact is None else (unit.source_artifact,)
    parent_ids = unit.parent_unit_ids
    if unit.parent_unit_id is not None and unit.parent_unit_id not in parent_ids:
        parent_ids = (*parent_ids, unit.parent_unit_id)
    return unit.model_copy(
        update={
            "source_refs": source_refs,
            "content_hash": sha256_id(unit.text.encode("utf-8")),
            "parent_unit_ids": parent_ids,
        }
    )


class ContextCompiler:
    def __init__(self, expander: EvidenceExpander) -> None:
        self._expander = expander

    def compile(
        self,
        needs_and_traces: tuple[tuple[Stage1MemoryNeed, RetrievalTrace], ...],
        text_root: TextRootDocument,
        *,
        context_id: StableId,
        base_commit: CommitId,
        snapshot_id: StableId,
        task_contract: str,
        token_budget: int,
    ) -> Stage1ContextPackage:
        if token_budget < 1:
            raise ValueError("context token budget must be positive")
        mandatory: list[RetrievalUnit] = []
        optional_groups_by_need: list[
            tuple[
                Stage1MemoryNeed,
                list[tuple[RetrievalUnit, ...]],
                list[tuple[RetrievalUnit, ...]],
                bool,
            ]
        ] = []
        optional_unit_order: list[RetrievalUnit] = []
        raw_evidence: list[RetrievalUnit] = []
        unresolved: list[str] = []
        compiled_traces: list[RetrievalTrace] = []
        for need, trace in needs_and_traces:
            if need.need_id != trace.need_id:
                raise ValueError("retrieval trace belongs to a different memory need")
            selected = tuple(candidate for candidate in trace.candidates if candidate.selected)
            if not selected:
                unresolved.append(need.query_text)
                compiled_traces.append(trace)
                optional_groups_by_need.append((need, [], [], False))
                continue
            units = tuple(candidate.unit for candidate in selected)
            self._assert_basis(units, base_commit, snapshot_id)
            candidate_groups: list[tuple[RetrievalUnit, ...]] = []
            candidate_expansion_groups: list[tuple[RetrievalUnit, ...]] = []
            expanded_items: list[RetrievalUnit] = []
            for candidate in selected:
                candidate_expanded = self._expander.expand((candidate,), text_root)
                candidate_groups.append((candidate.unit,))
                candidate_expansion_groups.append(tuple(_dedupe(candidate_expanded)))
                expanded_items.extend(candidate_expanded)
            expanded = _dedupe(expanded_items)
            self._assert_basis(expanded, base_commit, snapshot_id)
            compiled_traces.append(
                trace.model_copy(
                    update={
                        "anchors_expanded": sum(
                            candidate.unit.unit_kind
                            not in {
                                RetrievalUnitKind.GROUNDED_BLOCK,
                                RetrievalUnitKind.GROUNDED_SPAN,
                            }
                            for candidate in selected
                        ),
                        "spans_expanded": len(expanded),
                        "l0_tokens": sum(_estimate_tokens(unit.text) for unit in expanded),
                    }
                )
            )
            required_group_indexes = {
                index
                for index, group in enumerate(candidate_groups)
                if any(unit.mandatory for unit in group)
            }
            if need.requirement is RequirementLevel.MANDATORY:
                # A mandatory Need requires one best-ranked evidence group,
                # not every candidate in its top-k retrieval window. Explicit
                # mandatory units remain required wherever they rank. The
                # remaining alternatives compete fairly for bounded context.
                required_group_indexes.add(0)
            optional_groups: list[tuple[RetrievalUnit, ...]] = []
            optional_expansion_groups: list[tuple[RetrievalUnit, ...]] = []
            for index, group in enumerate(candidate_groups):
                expansion_group = candidate_expansion_groups[index]
                if index in required_group_indexes:
                    mandatory.extend(group)
                    # Required evidence remains complete and fail-closed. The
                    # compact-first policy only changes optional competition.
                    mandatory.extend(expansion_group)
                else:
                    optional_groups.append(group)
                    optional_expansion_groups.append(expansion_group)
                    optional_unit_order.extend(group)
                    optional_unit_order.extend(expansion_group)
            optional_groups_by_need.append(
                (
                    need,
                    optional_groups,
                    optional_expansion_groups,
                    bool(required_group_indexes),
                )
            )
            raw_evidence.extend(expanded)

        mandatory = _dedupe(mandatory)
        mandatory_tokens = sum(_estimate_tokens(unit.text) for unit in mandatory)
        remaining = max(0, token_budget - mandatory_tokens)
        selected_optional: list[RetrievalUnit] = []
        included_ids = {unit.unit_id for unit in mandatory}
        optional_tokens = 0
        packing_order = tuple(
            sorted(
                range(len(optional_groups_by_need)),
                key=lambda index: (-optional_groups_by_need[index][0].priority, index),
            )
        )
        next_group_index = [0] * len(optional_groups_by_need)

        def include_if_fits(
            group: tuple[RetrievalUnit, ...], *, need: Stage1MemoryNeed | None = None
        ) -> bool:
            nonlocal optional_tokens, remaining
            new_units = tuple(unit for unit in group if unit.unit_id not in included_ids)
            if not new_units:
                return bool(group)
            cost = sum(_estimate_tokens(unit.text) for unit in new_units)
            if need is not None and len(new_units) == 1:
                unit = new_units[0]
                if unit.unit_kind in {
                    RetrievalUnitKind.GROUNDED_BLOCK,
                    RetrievalUnitKind.GROUNDED_SPAN,
                } and (cost > remaining or cost > COMPACT_FULL_TEXT_TOKEN_CAP):
                    # A large grounded passage does not fit (or is too large to
                    # justify its full text) but still has exact evidence
                    # references a Writer can cite.  Represent it by a bounded
                    # excerpt instead of dropping the evidence entirely, so
                    # deep-ranked blocks survive budget competition.
                    compact = _compact_block_unit(unit, query_text=need.query_text)
                    if compact is not None and compact.unit_id in included_ids:
                        # The passage is already represented by its excerpt;
                        # never charge the full text for a duplicate.
                        return False
                    if compact is not None and compact.unit_id not in included_ids:
                        compact_cost = _estimate_tokens(compact.text)
                        if compact_cost <= remaining:
                            new_units = (compact,)
                            cost = compact_cost
            if cost > remaining:
                return False
            selected_optional.extend(new_units)
            included_ids.update(unit.unit_id for unit in new_units)
            optional_tokens += cost
            remaining -= cost
            return True

        has_context_group = [entry[3] for entry in optional_groups_by_need]

        # Give every Need that has no required group one budget opportunity
        # before adding second alternatives for already-covered Needs.
        for index in packing_order:
            _need, groups, _expansions, _has_required_group = optional_groups_by_need[index]
            if has_context_group[index] or not groups:
                continue
            has_context_group[index] = include_if_fits(groups[0], need=_need)
            next_group_index[index] = 1

        # Grounded evidence tier: deep-ranked direct passages must not be
        # starved by the tail alternatives of already-covered Needs.  Walk
        # every Need's not-yet-represented grounded groups round-robin (one
        # group per Need per round) and admit each passage as a bounded
        # compact excerpt when it is large or does not fit, otherwise in full.
        # The compact keeps the exact evidence references, so the Writer can
        # cite the passage without charging its full text to the budget.  A
        # full round without progress terminates the tier.
        grounded_offsets = [1] * len(optional_groups_by_need)
        while True:
            advanced = False
            for index in packing_order:
                _need, groups, _expansions, _has_required_group = optional_groups_by_need[index]
                offset = grounded_offsets[index]
                while offset < len(groups):
                    unit = groups[offset][0]
                    if unit.unit_id in included_ids or (
                        unit.unit_kind
                        not in {
                            RetrievalUnitKind.GROUNDED_BLOCK,
                            RetrievalUnitKind.GROUNDED_SPAN,
                        }
                    ):
                        offset += 1
                        advanced = True
                        continue
                    include_if_fits(groups[offset], need=_need)
                    offset += 1
                    advanced = True
                    break
                grounded_offsets[index] = offset
            if not advanced:
                break

        # Compact -> expand: after every uncovered Need has had a chance to
        # place its small semantic anchor, spend remaining budget on the L0
        # evidence behind those first anchors. This prevents one long passage
        # from starving later Needs while preserving cutoff-safe lineage.
        for index in packing_order:
            _need, groups, expansions, _has_required_group = optional_groups_by_need[index]
            if next_group_index[index] != 1 or not groups:
                continue
            if all(unit.unit_id in included_ids for unit in groups[0]):
                include_if_fits(expansions[0], need=_need)

        while any(
            next_group_index[index] < len(optional_groups_by_need[index][1])
            for index in packing_order
        ):
            for index in packing_order:
                _need, groups, expansions, _has_required_group = optional_groups_by_need[index]
                group_index = next_group_index[index]
                if group_index >= len(groups):
                    continue
                if include_if_fits(groups[group_index], need=_need):
                    include_if_fits(expansions[group_index], need=_need)
                next_group_index[index] += 1
        dropped_optional = tuple(
            unit.unit_id
            for unit in _dedupe(optional_unit_order)
            if unit.unit_id not in included_ids
        )
        included = (*mandatory, *selected_optional)
        needs = tuple(need for need, _trace in needs_and_traces)
        return Stage1ContextPackage(
            context_id=context_id,
            base_commit=base_commit,
            snapshot_id=snapshot_id,
            task_contract=task_contract,
            mandatory_constraints=tuple(mandatory),
            current_world_state=_kinds(included, {RetrievalUnitKind.STATE_ANCHOR}),
            active_plan_obligations=_kinds(included, {RetrievalUnitKind.PLAN_ANCHOR}),
            relevant_historical_events=_kinds(
                included,
                {RetrievalUnitKind.EVENT_ANCHOR, RetrievalUnitKind.CHAPTER_ANCHOR},
            ),
            truth_and_knowledge_boundaries=_kinds(
                included,
                {RetrievalUnitKind.FACT_ANCHOR, RetrievalUnitKind.RELATION_ANCHOR},
            ),
            raw_evidence_spans=tuple(
                unit for unit in _dedupe(raw_evidence) if unit.unit_id in included_ids
            ),
            style_or_reference_optional=tuple(
                unit
                for unit in selected_optional
                if unit.unit_kind
                in {RetrievalUnitKind.GROUNDED_BLOCK, RetrievalUnitKind.GROUNDED_SPAN}
            ),
            unresolved_gaps=tuple(unresolved),
            need_facets=tuple(facet for need in needs for facet in need.need_facets),
            need_completion_specs=tuple(
                need.completion_spec for need in needs if need.completion_spec is not None
            ),
            retrieval_traces=tuple(compiled_traces),
            budget_report=ContextBudgetReport(
                token_budget=token_budget,
                mandatory_tokens=mandatory_tokens,
                optional_tokens=optional_tokens,
                dropped_optional_unit_ids=dropped_optional,
                full_chapter_read_count=sum(
                    trace.full_chapters_read for _, trace in needs_and_traces
                ),
            ),
        )

    @staticmethod
    def _assert_basis(
        units: Iterable[RetrievalUnit],
        base_commit: CommitId,
        snapshot_id: StableId,
    ) -> None:
        if any(
            unit.source_commit != base_commit or unit.snapshot_id != snapshot_id for unit in units
        ):
            raise ValueError("retrieval unit canonical or snapshot basis mismatch")


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


# A bounded excerpt a large grounded unit may be compacted to when its full
# text would not fit the writer budget.  The excerpt keeps the unit's exact
# evidence references, so the compact representation can still be cited while
# the full passage is dropped.
COMPACT_BLOCK_EXCERPT_LIMIT = 320
_COMPACT_HEAD_BUDGET_RATIO = 0.4
# Large grounded passages are represented by bounded excerpts instead of full
# text, so deep-ranked direct evidence keeps an affordable place in a bounded
# Writer budget without starving every other Need.
COMPACT_FULL_TEXT_TOKEN_CAP = 200


def _query_terms(value: str) -> tuple[str, ...]:
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", value.casefold())
    stopwords = {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "use",
        "with",
    }
    terms: list[str] = []
    for token in tokens:
        if len(token) < 2 or token in stopwords:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 2:
            terms.extend(token[index : index + 2] for index in range(len(token) - 1))
        else:
            terms.append(token)
    return tuple(dict.fromkeys(terms))


def _compact_block_segments(
    text: str, query_text: str, *, limit: int
) -> tuple[tuple[str, int, int], ...] | None:
    """Return the retained sentences of a bounded excerpt with precise spans.

    Each segment is ``(sentence, start, end)`` with ``start``/``end`` measured
    in the source passage text, so a compact excerpt can carry exact
    model-visible support provenance instead of a whole-passage reference that
    implies unseen text is part of the support.

    The excerpt keeps the passage head in original order and then adds the
    query-term-ranked sentences that fit the remaining budget, so the writer
    keeps a coherent and relevant view of the block without its full text.
    """

    if len(text) <= limit:
        return None
    split_items = tuple(
        item
        for item in re.finditer(
            r"[^\u3002\uff01\uff1f!?;\uff1b\n]+[\u3002\uff01\uff1f!?;\uff1b]*|[\u3002\uff01\uff1f!?;\uff1b]+",
            text,
        )
        if item.group().strip()
    )
    sentences: list[tuple[str, int, int]] = []
    for item in split_items:
        raw = item.group()
        start = item.start() + len(raw) - len(raw.lstrip())
        end = item.end() - (len(raw) - len(raw.rstrip()))
        sentences.append((raw.strip(), start, end))
    if not sentences:
        return None
    terms = _query_terms(query_text)
    head_budget = int(limit * _COMPACT_HEAD_BUDGET_RATIO)
    head_indexes: list[int] = []
    head_chars = 0
    for index, (sentence, _start, _end) in enumerate(sentences):
        if head_chars + len(sentence) > head_budget:
            break
        head_indexes.append(index)
        head_chars += len(sentence)
    head_set = set(head_indexes)
    ranked = sorted(
        (
            (index, sentence)
            for index, (sentence, _start, _end) in enumerate(sentences)
            if index not in head_set
        ),
        key=lambda item: (
            -sum(term in item[1].casefold() for term in terms),
            item[0],
        ),
    )
    selected_indexes: list[int] = list(head_indexes)
    chars = head_chars
    for index, sentence in ranked:
        if chars + len(sentence) > limit:
            continue
        selected_indexes.append(index)
        chars += len(sentence)
    selected_indexes.sort()
    selected = [sentences[index] for index in selected_indexes]
    excerpt = " ".join(segment for segment, _start, _end in selected)
    while selected and len(excerpt) > limit:
        selected.pop()
        excerpt = " ".join(segment for segment, _start, _end in selected)
    if not excerpt or len(excerpt) >= len(text):
        return None
    return tuple(selected)


def _compact_block_excerpt(text: str, query_text: str, *, limit: int) -> str | None:
    """Return the bounded excerpt text for a large grounded unit, or None."""

    segments = _compact_block_segments(text, query_text, limit=limit)
    if segments is None:
        return None
    return " ".join(segment for segment, _start, _end in segments)


def _compact_block_unit(
    unit: RetrievalUnit,
    *,
    query_text: str,
    limit: int = COMPACT_BLOCK_EXCERPT_LIMIT,
) -> RetrievalUnit | None:
    """Build a bounded excerpt unit for a large grounded unit.

    The compact unit carries one precise source-span EvidenceRef per retained
    sentence (the model-visible support provenance) and records the source
    unit as its parent.  The whole-passage reference is retained only as
    parent/source lineage: unseen excerpt text never counts as semantic
    support.  The unit stays citable and traceable without charging the full
    text to the budget.
    """

    if unit.unit_kind not in {
        RetrievalUnitKind.GROUNDED_BLOCK,
        RetrievalUnitKind.GROUNDED_SPAN,
    }:
        return None
    segments = _compact_block_segments(unit.text, query_text, limit=limit)
    if segments is None:
        return None
    excerpt = " ".join(segment for segment, _start, _end in segments)
    segment_refs = tuple(
        _segment_evidence_ref(unit, segment, start, end) for segment, start, end in segments
    )
    parent_ids = tuple(dict.fromkeys((*unit.parent_unit_ids, unit.unit_id)))
    source_refs = () if unit.source_artifact is None else (unit.source_artifact,)
    return unit.model_copy(
        update={
            "unit_id": StableId(f"compact.{unit.unit_id.root}"),
            "text": excerpt,
            "evidence_refs": segment_refs,
            "source_refs": source_refs,
            "content_hash": unit.content_hash,
            "parent_unit_id": unit.unit_id,
            "parent_unit_ids": parent_ids,
        }
    )


def _segment_evidence_ref(
    unit: RetrievalUnit,
    segment: str,
    start: int,
    end: int,
) -> EvidenceRef:
    """Build a precise source-span EvidenceRef for one retained segment.

    The segment ref inherits the passage identity (root/object hash, chapter,
    scene, commit) from the unit's whole-passage reference while pinning the
    span and quote to exactly the visible sentence.  The whole-passage
    reference itself stays parent/source lineage only.
    """

    parent_ref = next(
        (
            reference
            for reference in unit.evidence_refs
            if reference.span is not None
            and reference.span.block_id == unit.unit_id.root.replace("grounded.block.", "block.", 1)
        ),
        unit.evidence_refs[0] if unit.evidence_refs else None,
    )
    if parent_ref is None:
        raise ValueError("grounded segment source requires an evidence reference")
    if parent_ref.span is None:
        raise ValueError("grounded segment source requires a precise span")
    digest = quote_hash(segment).root.removeprefix("sha256:")[:24]
    return parent_ref.model_copy(
        update={
            "evidence_id": StableId(f"evidence.segment.{unit.unit_id.root}.{start}.{digest}"),
            "quote_hash": quote_hash(segment),
            "span": parent_ref.span.model_copy(update={"start": start, "end": end}),
        }
    )


def _evidence_snippets(
    evidence_refs: tuple[EvidenceRef, ...],
    text_root: TextRootDocument,
    blocks: dict[StableId, TextBlock],
) -> str:
    snippets: list[str] = []
    for evidence in evidence_refs:
        selected = _resolve_evidence_text(evidence, blocks, label="canonical record")
        # ``root_hash`` identifies the TextRoot version that originally bound
        # this evidence. An append-only later TextRoot has a different root hash
        # while retaining the same content-addressed block and quote.
        snippets.append(selected)
    return " ".join(snippets)


def _resolve_evidence_text(
    evidence: EvidenceRef,
    blocks: dict[StableId, TextBlock],
    *,
    label: str,
) -> str:
    if evidence.span is None:
        raise ValueError(f"{label} evidence does not resolve to the supplied text root")
    block = blocks.get(evidence.span.block_id)
    if (
        block is None
        or evidence.span.end > len(block.text)
        or evidence.chapter_id != block.chapter_id
        or evidence.scene_id != block.scene_id
        or evidence.object_hash != sha256_id(block.text.encode("utf-8"))
    ):
        raise ValueError(f"{label} evidence span cannot be resolved")
    selected = block.text[evidence.span.start : evidence.span.end]
    if evidence.quote_hash != quote_hash(selected):
        raise ValueError(f"{label} evidence quote hash does not match")
    return selected


def _block_evidence(
    block: TextBlock,
    text_root: TextRootDocument,
    source_commit: CommitId,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=StableId(f"evidence.full.{block.block_id.root}"),
        root_hash=text_root.root_hash,
        object_hash=sha256_id(block.text.encode("utf-8")),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=TextSpanRef(block_id=block.block_id, start=0, end=len(block.text)),
        quote_hash=quote_hash(block.text),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=source_commit,
    )


def _dedupe(units: Iterable[RetrievalUnit]) -> list[RetrievalUnit]:
    result: list[RetrievalUnit] = []
    seen: set[StableId] = set()
    for unit in units:
        if unit.unit_id not in seen:
            seen.add(unit.unit_id)
            result.append(unit)
    return result


def _kinds(
    units: Iterable[RetrievalUnit],
    kinds: set[RetrievalUnitKind],
) -> tuple[RetrievalUnit, ...]:
    return tuple(unit for unit in units if unit.unit_kind in kinds)
