"""Stage 1 L1 anchor construction, evidence expansion, and context compilation."""

from __future__ import annotations

import json
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
        optional: list[RetrievalUnit] = []
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
                continue
            units = tuple(candidate.unit for candidate in selected)
            self._assert_basis(units, base_commit, snapshot_id)
            expanded = self._expander.expand(selected, text_root)
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
            if need.requirement is RequirementLevel.MANDATORY:
                mandatory.extend(units)
                mandatory.extend(expanded)
            else:
                optional.extend(units)
                optional.extend(expanded)
            raw_evidence.extend(expanded)

        mandatory = _dedupe(mandatory)
        optional = [unit for unit in _dedupe(optional) if unit not in mandatory]
        mandatory_tokens = sum(_estimate_tokens(unit.text) for unit in mandatory)
        remaining = max(0, token_budget - mandatory_tokens)
        selected_optional: list[RetrievalUnit] = []
        dropped_optional: list[StableId] = []
        optional_tokens = 0
        for unit in optional:
            cost = _estimate_tokens(unit.text)
            if cost <= remaining:
                selected_optional.append(unit)
                optional_tokens += cost
                remaining -= cost
            else:
                dropped_optional.append(unit.unit_id)
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
            raw_evidence_spans=tuple(unit for unit in _dedupe(raw_evidence) if unit in included),
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
                dropped_optional_unit_ids=tuple(dropped_optional),
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
