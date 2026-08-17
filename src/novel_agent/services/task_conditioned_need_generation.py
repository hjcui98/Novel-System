"""Task/plan-conditioned, bounded memory need generation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import Field

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import AuthorPlanningContext, PlanRootDocument
from novel_agent.domain.ids import ArtifactId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    ExpectedClaimScope,
    FacetEvidenceRequirement,
    NeedCompletionSpec,
    NeedFacet,
    NeedFacetKind,
    NeedGapPolicy,
    NeedRisk,
    NeedUncertaintyPolicy,
    ObligationStatus,
    RequirementLevel,
    ResolutionPath,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.planning_memory import (
    GroundedNeedDraft,
    GroundingStatus,
    PlannerArtifactMetadata,
    PlannerFallbackStatus,
    PlannerFinalNeedManifest,
    PlannerInvocationArtifact,
    PlannerRunResult,
)
from novel_agent.domain.world import StateRecord
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    WriterContextSection,
)
from novel_agent.services.benchmark_importer import content_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.model_gateway import ModelGateway
from novel_agent.services.need_draft_grounder import NeedDraftGrounder
from novel_agent.services.need_query_compiler import NeedQueryCompiler
from novel_agent.services.need_validator import NeedValidator
from novel_agent.services.plan_conditioned_need_planner import PlanConditionedNeedPlanner
from novel_agent.services.task_focus import (
    FocusSet,
    TaskFocus,
    TaskFocusExtractor,
    TaskFocusSource,
    TaskFocusType,
)


class NeedGenerationStatus(StrEnum):
    READY = "READY"
    NEED_BUDGET_EXHAUSTED = "NEED_BUDGET_EXHAUSTED"
    NO_FOCUS = "NO_FOCUS"
    PLANNER_FALLBACK = "PLANNER_FALLBACK"


class NeedGenerationResult(DomainModel):
    task_id: StableId
    focus_set: FocusSet
    needs: tuple[Stage1MemoryNeed, ...]
    status: NeedGenerationStatus
    unexpanded_focus_ids: tuple[StableId, ...] = ()
    need_completion_spec_version: str = Field(min_length=1)
    generator_version: str = Field(min_length=1)
    planner_metadata: PlannerArtifactMetadata | None = None
    fallback_used: bool = False
    planner_fallback_reason: str | None = Field(default=None, min_length=1)
    grounding_status_counts: tuple[int, int, int] = (0, 0, 0)
    planner_artifact: PlannerInvocationArtifact | None = None
    planner_artifact_document_ref: ArtifactRef | None = None


class TaskPlanConditionedNeedGenerator:
    """Generate needs from the bounded FocusSet, never by enumerating WorldRoot."""

    profile = "task_plan_conditioned_v1"
    version = "task_plan_conditioned_need.v24"
    completion_spec_version = "need_completion_spec.v1"

    _CAPABILITY_PREDICATE_KEYWORDS: ClassVar[tuple[str, ...]] = (
        "ability",
        "boundary",
        "capability",
        "cultivation",
        "limit",
        "restriction",
        "skill",
        "境界",
        "能力",
        "限制",
        "未完成",
    )
    _KNOWLEDGE_PREDICATE_KEYWORDS: ClassVar[tuple[str, ...]] = (
        "contract",
        "disclos",
        "know",
        "marriage",
        "promise",
        "secret",
        "婚",
        "承诺",
        "知",
        "秘密",
    )
    _INTENT_BY_NEED_TYPE: ClassVar[dict[str, Stage1QueryIntent]] = {
        "current_state": Stage1QueryIntent.CURRENT_STATE,
        "capability_boundary": Stage1QueryIntent.CURRENT_STATE,
        "knowledge_boundary": Stage1QueryIntent.SEMANTIC_HISTORY,
        "relationship_emotion": Stage1QueryIntent.SEMANTIC_HISTORY,
        "long_range_callback": Stage1QueryIntent.RELATED_EVENT,
        "unresolved_obligation": Stage1QueryIntent.KNOWN_ID,
        "entity_history": Stage1QueryIntent.SEMANTIC_HISTORY,
        "plan_obligation": Stage1QueryIntent.PLAN_OBLIGATION,
    }
    _SECTION_BY_NEED_TYPE: ClassVar[dict[str, WriterContextSection]] = {
        "current_state": WriterContextSection.CURRENT_WORLD_STATE,
        "capability_boundary": WriterContextSection.CONTINUITY_CONSTRAINTS,
        "knowledge_boundary": WriterContextSection.KNOWLEDGE_AND_DISCLOSURE,
        "relationship_emotion": WriterContextSection.RELATIONSHIP_AND_EMOTION,
        "long_range_callback": WriterContextSection.LONG_RANGE_CALLBACKS,
        "unresolved_obligation": WriterContextSection.CONTINUITY_CONSTRAINTS,
        "entity_history": WriterContextSection.CAUSAL_HISTORY,
        "plan_obligation": WriterContextSection.PLAN_AND_OBLIGATIONS,
    }
    _POOLS_BY_NEED_TYPE: ClassVar[dict[str, tuple[CandidatePool, ...]]] = {
        "current_state": (
            CandidatePool.R1,
            CandidatePool.ANCHOR,
            CandidatePool.GROUNDED,
        ),
        "capability_boundary": (
            CandidatePool.R1,
            CandidatePool.ANCHOR,
            CandidatePool.GROUNDED,
        ),
        "knowledge_boundary": (CandidatePool.ANCHOR, CandidatePool.GROUNDED),
        "relationship_emotion": (
            CandidatePool.R1,
            CandidatePool.ANCHOR,
            CandidatePool.GROUNDED,
        ),
        "long_range_callback": (CandidatePool.ANCHOR, CandidatePool.GROUNDED),
        "unresolved_obligation": (
            CandidatePool.R1,
            CandidatePool.ANCHOR,
            CandidatePool.GROUNDED,
        ),
        "entity_history": (
            CandidatePool.R1,
            CandidatePool.ANCHOR,
            CandidatePool.GROUNDED,
        ),
        "plan_obligation": (CandidatePool.ANCHOR,),
    }

    def __init__(
        self,
        *,
        max_total_needs: int = 32,
        focus_extractor: TaskFocusExtractor | None = None,
        planner_gateway: ModelGateway | None = None,
        planner_artifact_writer: Callable[[bytes, str], ArtifactRef] | None = None,
        planner_max_output_tokens: int = 8192,
    ) -> None:
        if max_total_needs < 1:
            raise ValueError("max_total_needs must be positive")
        self._max_total_needs = max_total_needs
        self._focus_extractor = focus_extractor or TaskFocusExtractor()
        self._planner = (
            PlanConditionedNeedPlanner(
                gateway=planner_gateway,
                max_output_tokens=planner_max_output_tokens,
            )
            if planner_gateway is not None
            else None
        )
        self._grounder = NeedDraftGrounder()
        self._validator = NeedValidator(max_total_needs=max_total_needs)
        self._planner_artifact_writer = planner_artifact_writer
        self._last_fallback_artifact: PlannerInvocationArtifact | None = None
        self._last_fallback_artifact_ref: ArtifactRef | None = None
        self._frozen_fallback_artifact: PlannerInvocationArtifact | None = None

    def generate(
        self,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        plan: PlanRootDocument | None = None,
        planning_context: AuthorPlanningContext | None = None,
        frozen_planner_artifact: PlannerInvocationArtifact | None = None,
    ) -> tuple[Stage1MemoryNeed, ...]:
        return self.generate_with_lineage(
            task,
            world,
            plan,
            planning_context,
            frozen_planner_artifact=frozen_planner_artifact,
        ).needs

    def generate_with_lineage(
        self,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        plan: PlanRootDocument | None = None,
        planning_context: AuthorPlanningContext | None = None,
        *,
        frozen_planner_artifact: PlannerInvocationArtifact | None = None,
    ) -> NeedGenerationResult:
        self._last_fallback_artifact = None
        self._last_fallback_artifact_ref = None
        self._frozen_fallback_artifact = None
        if (
            task.information_profile
            in {
                BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                BenchmarkInformationProfile.TASK_INTENT_ONLY,
            }
            and plan is not None
            and (plan.nodes or plan.chapter_goals)
        ):
            # Ignore is unsafe here: callers could believe the plan was filtered.
            raise ValueError(
                f"{task.information_profile.value} need generation cannot receive a future PlanRoot"
            )
        planner_fallback = False
        if frozen_planner_artifact is not None and (
            task.information_profile is not BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
            or plan is None
            or not (plan.nodes or plan.chapter_goals)
        ):
            raise ValueError("frozen Planner artifact requires an APC task and PlanRoot")
        if (
            task.information_profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
            and plan is not None
            and (plan.nodes or plan.chapter_goals)
        ):
            if frozen_planner_artifact is not None:
                planned = self._replay_planner_artifact(
                    task,
                    world,
                    plan,
                    planning_context,
                    frozen_planner_artifact,
                )
                if planned is not None:
                    return planned
                planner_fallback = True
            elif self._planner is not None:
                planned = self._run_planner_chain(task, world, plan, planning_context)
                if planned is not None:
                    return planned
                planner_fallback = True
        focus_set = self._focus_extractor.extract(task, world, plan)
        if not focus_set.focuses:
            artifact = self._last_fallback_artifact
            artifact_ref = self._last_fallback_artifact_ref
            planner_metadata = artifact.metadata if artifact is not None else None
            if planner_fallback:
                _, artifact, artifact_ref, planner_metadata = self._finalize_fallback_lineage(())
            return NeedGenerationResult(
                task_id=task.task_id,
                focus_set=focus_set,
                needs=(),
                status=(
                    NeedGenerationStatus.PLANNER_FALLBACK
                    if planner_fallback
                    else NeedGenerationStatus.NO_FOCUS
                ),
                fallback_used=planner_fallback,
                planner_fallback_reason=(
                    (artifact.fallback_reason if artifact is not None else None)
                    or ("planner_chain_unavailable_or_rejected" if planner_fallback else None)
                ),
                planner_metadata=planner_metadata,
                planner_artifact=artifact,
                planner_artifact_document_ref=artifact_ref,
                need_completion_spec_version=self.completion_spec_version,
                generator_version=self.version,
            )

        entity_by_id = {entity.entity_id: entity for entity in world.entities}
        state_by_id = {state.state_id: state for state in world.states}
        relation_by_id = {relation.relation_id: relation for relation in world.relations}
        event_by_id = {event.event_id: event for event in world.events}
        obligation_by_id = {
            obligation.obligation_id: obligation for obligation in world.obligations
        }
        node_by_id = {
            **{node.plan_node_id: node for node in (plan.nodes if plan else ())},
            **{goal.goal_id: goal for goal in (plan.chapter_goals if plan else ())},
        }
        focus_by_canonical: dict[StableId, list[TaskFocus]] = {}
        for focus in focus_set.focuses:
            focus_by_canonical.setdefault(focus.canonical_id, []).append(focus)
        focused_entities = tuple(
            (index, focus)
            for index, focus in enumerate(focus_set.focuses)
            if focus.focus_type is TaskFocusType.ENTITY
        )
        state_count = {
            focus.canonical_id: sum(
                state.subject_id == focus.canonical_id for state in world.states
            )
            for _, focus in focused_entities
        }
        event_count = {
            focus.canonical_id: sum(
                focus.canonical_id in event.participant_ids for event in world.events
            )
            for _, focus in focused_entities
        }
        source_rank = {
            TaskFocusSource.TASK: 0,
            TaskFocusSource.OPEN_OBLIGATION: 1,
            TaskFocusSource.PLAN_INTENT: 2,
            TaskFocusSource.CUTOFF_FRONTIER: 3,
            TaskFocusSource.ALIAS_EXPANSION: 4,
            TaskFocusSource.ONE_HOP_RELATION: 5,
        }
        primary_entity_focus = (
            min(
                focused_entities,
                key=lambda indexed: (
                    indexed[1].source is not TaskFocusSource.TASK,
                    -state_count[indexed[1].canonical_id],
                    -event_count[indexed[1].canonical_id],
                    source_rank[indexed[1].source],
                    indexed[0],
                ),
            )[1]
            if focused_entities
            else None
        )
        target_plan_text = " ".join(
            dict.fromkeys(
                text.strip()
                for node in (plan.nodes if plan else ())
                if TaskFocusExtractor._plan_node_intersects_target(node.plan_node_id, task)
                for text in (node.title, node.summary)
                if text.strip()
            )
        )

        run_id = RunId(f"run.stage2m.{task.task_id.root}"[:128])
        task_id = TaskId(task.task_id.root)
        candidates: list[Stage1MemoryNeed] = []

        def add(
            *,
            identity: str,
            focus: TaskFocus,
            need_type: str,
            intent: Stage1QueryIntent,
            query: str,
            entity_ids: tuple[StableId, ...],
            section: WriterContextSection,
            mandatory: bool,
            priority: int,
            allow_plan: bool = False,
            pools: tuple[CandidatePool, ...] = (
                CandidatePool.ANCHOR,
                CandidatePool.GROUNDED,
            ),
            query_hints: tuple[str, ...] = (),
            predicates: tuple[str, ...] = (),
            predicates_by_facet: Mapping[NeedFacetKind, tuple[str, ...]] | None = None,
        ) -> None:
            need_id = StableId(f"need.stage2m.{identity}"[:128])
            # Entity-mention closure over this fallback Need's own text so the
            # deterministic path keeps entities that appear in the query/goal
            # but were not already bound by the focus extractor.
            closed_entities = self._closed_entity_ids_for_text(
                world,
                (query, *query_hints, target_plan_text),
            )
            entity_ids = tuple(dict.fromkeys((*entity_ids, *closed_entities)))
            facets, completion_spec = self._completion_contract(
                need_id=need_id,
                need_type=need_type,
                section=section,
                task=task,
                focus=focus,
                allow_plan=allow_plan,
                mandatory=mandatory,
                predicates_by_facet=predicates_by_facet,
            )
            candidates.append(
                Stage1MemoryNeed(
                    need_id=need_id,
                    run_id=run_id,
                    task_id=task_id,
                    base_commit=world.source_commit,
                    horizon_target=(
                        task.target_chapter_start,
                        task.target_chapter_end,
                    ),
                    need_type=need_type,
                    query_intent=intent,
                    query_text=query,
                    entity_ids=entity_ids,
                    predicates=predicates,
                    access_scope=("author_planning" if allow_plan else "writer_safe"),
                    allow_plan=allow_plan,
                    planner_may_read_plan=allow_plan,
                    retrieval_may_return_plan=allow_plan,
                    claim_may_cite_plan=allow_plan,
                    legacy_allow_plan=allow_plan,
                    why_needed=focus.reason,
                    risk_level=NeedRisk.HIGH if mandatory else NeedRisk.MEDIUM,
                    requirement=(
                        RequirementLevel.MANDATORY if mandatory else RequirementLevel.OPTIONAL
                    ),
                    preferred_resolution_path=(
                        ResolutionPath.EXACT_TEMPORAL
                        if CandidatePool.R1 in pools
                        else ResolutionPath.ANCHOR_FIRST
                    ),
                    allowed_candidate_pools=pools,
                    expected_evidence_types=("structured_record", "text_span"),
                    stop_condition=(
                        "served by cutoff-safe exact evidence slices or an explicit typed gap"
                    ),
                    purpose=focus.reason,
                    expected_section=section,
                    focus_ids=(focus.focus_id,),
                    priority=priority,
                    query_hints=tuple(dict.fromkeys((query, *query_hints))),
                    completion_criteria=(
                        "every required facet is served by cutoff-safe exact evidence slices "
                        "or a typed gap"
                    ),
                    need_facets=facets,
                    completion_spec=completion_spec,
                )
            )

        for focus in focus_set.focuses:
            if focus.focus_type is TaskFocusType.ENTITY:
                entity = entity_by_id.get(focus.canonical_id)
                if entity is None:
                    continue
                predicates = tuple(
                    dict.fromkeys(
                        state.predicate
                        for state in world.states
                        if state.subject_id == entity.entity_id
                    )
                )
                state_context = " ".join(
                    value
                    for state in world.states
                    if state.subject_id == entity.entity_id
                    for value in (state.predicate, self._query_value(state.value))
                    if value
                )[:2000]
                relation_context = " ".join(
                    value
                    for relation in world.relations
                    if entity.entity_id in {relation.subject_id, relation.object_id}
                    for value in (
                        relation.predicate,
                        entity_by_id.get(
                            (
                                relation.object_id
                                if relation.subject_id == entity.entity_id
                                else relation.subject_id
                            ),
                            entity,
                        ).internal_label,
                    )
                    if value
                )[:1000]
                event_context = " ".join(
                    dict.fromkeys(
                        event.event_type
                        for event in world.events
                        if entity.entity_id in event.participant_ids
                    )
                )[:1000]
                obligation_context = " ".join(
                    dict.fromkeys(
                        obligation.description
                        for obligation in world.obligations
                        if entity.entity_id in obligation.owner_ids
                        and obligation.status
                        in {
                            ObligationStatus.OPEN,
                            ObligationStatus.PROGRESSED,
                        }
                    )
                )[:1000]
                knowledge_state_context = self._knowledge_state_context(
                    entity.entity_id,
                    world.states,
                )
                entity_context = " ".join(
                    value
                    for value in (
                        state_context,
                        relation_context,
                        event_context,
                        obligation_context,
                    )
                    if value
                )

                query = " ".join((entity.internal_label, *predicates[:16])).strip()
                is_primary_entity = (
                    primary_entity_focus is not None
                    and focus.canonical_id == primary_entity_focus.canonical_id
                )
                # R1 predicates are an OR-set SQL filter (IN clause) combined
                # with entity ids by AND, so filling them tightens exact
                # retrieval to semantically related records without ever
                # over-constraining to an empty set.
                relation_predicates = tuple(
                    dict.fromkeys(
                        relation.predicate
                        for relation in world.relations
                        if entity.entity_id in {relation.subject_id, relation.object_id}
                    )
                )
                # A recent Event and a one-hop Relation already produce their
                # own retrieval envelopes below. Generating a generic current-
                # state Need for every participant duplicated those envelopes
                # and consumed one scheduler turn per incidental entity. Keep
                # entity-state retrieval only when the public task, an open
                # obligation, or visible plan actually names the entity (plus
                # the one primary entity selected from that public frontier).
                if is_primary_entity or focus.source in {
                    TaskFocusSource.TASK,
                    TaskFocusSource.OPEN_OBLIGATION,
                    TaskFocusSource.PLAN_INTENT,
                }:
                    add(
                        identity=f"entity.{entity.entity_id.root}.state",
                        focus=focus,
                        need_type="current_state",
                        intent=Stage1QueryIntent.CURRENT_STATE,
                        query=query or entity.internal_label,
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.CURRENT_WORLD_STATE,
                        mandatory=is_primary_entity,
                        priority=(
                            97
                            if is_primary_entity
                            else 88
                            if focus.source is TaskFocusSource.OPEN_OBLIGATION
                            else 84
                        ),
                        pools=(CandidatePool.R1, CandidatePool.ANCHOR, CandidatePool.GROUNDED),
                        predicates=tuple(predicates[:16]),
                        predicates_by_facet={
                            NeedFacetKind.CURRENT_STATE: tuple(predicates[:16]),
                        },
                    )
                if is_primary_entity:
                    label = entity.internal_label
                    add(
                        identity=f"entity.{entity.entity_id.root}.continuity",
                        focus=focus,
                        need_type="continuity_constraint",
                        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                        query=(
                            f"{label} 当前连续性 限制 条件 目标 动机 承诺 "
                            f"未解决问题 {entity_context}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.CONTINUITY_CONSTRAINTS,
                        mandatory=True,
                        priority=96,
                        pools=(
                            CandidatePool.R1,
                            CandidatePool.ANCHOR,
                            CandidatePool.GROUNDED,
                        ),
                        # Continuity binds current-state anchors of the entity;
                        # declare its state predicates so the facet evaluator
                        # can bind by predicate (2026-08-14 review P1).
                        predicates=predicates,
                        predicates_by_facet={
                            NeedFacetKind.CURRENT_STATE: tuple(predicates),
                        },
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.capability-boundary",
                        focus=focus,
                        need_type="capability_boundary",
                        intent=Stage1QueryIntent.CURRENT_STATE,
                        query=(
                            f"{label} 当前能力 能力边界 已完成 未完成 "
                            f"可用 不可用 理论 实践 限制 {state_context}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.CONTINUITY_CONSTRAINTS,
                        mandatory=False,
                        priority=94,
                        pools=(
                            CandidatePool.R1,
                            CandidatePool.ANCHOR,
                            CandidatePool.GROUNDED,
                        ),
                        predicates=self._predicates_by_keywords(
                            predicates,
                            self._CAPABILITY_PREDICATE_KEYWORDS,
                        ),
                        predicates_by_facet={
                            NeedFacetKind.CAPABILITY_STATUS: self._predicates_by_keywords(
                                predicates, self._CAPABILITY_PREDICATE_KEYWORDS
                            ),
                            NeedFacetKind.LIMITATION: self._predicates_by_keywords(
                                predicates, self._CAPABILITY_PREDICATE_KEYWORDS
                            ),
                        },
                    )
                    # Retrieval is intentionally coarser than the conclusion
                    # contract.  Before retrieval we cannot know whether the
                    # useful historical passage will be classified as a
                    # capability change, goal, destination, learning source,
                    # environment transition, or stable behaviour.  Emitting
                    # one Need per label starved every Need under the global
                    # call budget and made a wrong pre-retrieval label exclude
                    # the correct evidence route.  Keep those interpretations
                    # as bounded public reformulations on one historical
                    # envelope; the NeedFacet/support layer still decides what
                    # the evidence can actually establish.
                    add(
                        identity=f"entity.{entity.entity_id.root}.history",
                        focus=focus,
                        need_type="entity_history",
                        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                        query=(
                            f"{label} 重要历史 前因 变化 决定 结果 "
                            f"{event_context} {relation_context} {obligation_context}"
                        ),
                        query_hints=(
                            (
                                f"{label} 能力边界 前置条件 失败 尝试 学习经验 来源 "
                                f"{state_context[:500]}"
                            ),
                            (
                                f"{label} 目标 动机 承诺 去向 地点 资格 行动因果 "
                                f"{obligation_context[:300]} {event_context[:300]}"
                            ),
                            (
                                f"{label} 环境 到达 居住 组织 资源 行为习惯 决策原则 "
                                f"{relation_context[:300]}"
                            ),
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.CAUSAL_HISTORY,
                        mandatory=False,
                        priority=95,
                        pools=(
                            CandidatePool.R1,
                            CandidatePool.ANCHOR,
                            CandidatePool.GROUNDED,
                        ),
                        # History binds event/state change predicates of the
                        # entity plus relation predicates (causal chain).
                        predicates_by_facet={
                            NeedFacetKind.CAUSAL_HISTORY: tuple(
                                dict.fromkeys(
                                    (
                                        *predicates,
                                        *relation_predicates,
                                        *(
                                            event.event_type
                                            for event in world.events
                                            if entity.entity_id in event.participant_ids
                                        ),
                                    )
                                )
                            )[:16],
                        },
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.relationship",
                        focus=focus,
                        need_type="relationship_emotion",
                        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                        query=(
                            f"{label} 关系 情绪 责任 信任 冲突 选择 "
                            f"{relation_context} {target_plan_text}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.RELATIONSHIP_AND_EMOTION,
                        mandatory=False,
                        priority=95,
                        pools=(
                            CandidatePool.R1,
                            CandidatePool.ANCHOR,
                            CandidatePool.GROUNDED,
                        ),
                        predicates=tuple(relation_predicates),
                        predicates_by_facet={
                            NeedFacetKind.RELATION_STATE: tuple(relation_predicates),
                        },
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.knowledge",
                        focus=focus,
                        need_type="knowledge_boundary",
                        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                        query=(
                            f"{label} 知情边界 知道 不知道 公开 未公开 "
                            f"推测 不可断言 {obligation_context[:300]} "
                            f"{knowledge_state_context[:300]} {relation_context[:500]} "
                            f"{target_plan_text}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.KNOWLEDGE_AND_DISCLOSURE,
                        mandatory=bool(target_plan_text),
                        priority=96 if target_plan_text else 95,
                        predicates=self._predicates_by_keywords(
                            predicates,
                            self._KNOWLEDGE_PREDICATE_KEYWORDS,
                        ),
                        predicates_by_facet={
                            NeedFacetKind.KNOWLEDGE_BOUNDARY: self._predicates_by_keywords(
                                predicates, self._KNOWLEDGE_PREDICATE_KEYWORDS
                            ),
                        },
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.callback",
                        focus=focus,
                        need_type="long_range_callback",
                        intent=Stage1QueryIntent.RELATED_EVENT,
                        query=(
                            f"{label} 长线伏笔 早期建立 首次出现 来源 "
                            f"物件连续性 未解决因果 {state_context} {target_plan_text}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.LONG_RANGE_CALLBACKS,
                        mandatory=bool(target_plan_text),
                        priority=97 if target_plan_text else 95,
                        # Callbacks bind setup (event/early-establishment) and
                        # unresolved-status predicates of the entity.
                        predicates_by_facet={
                            NeedFacetKind.SETUP: tuple(
                                dict.fromkeys(
                                    (
                                        *predicates,
                                        *(
                                            event.event_type
                                            for event in world.events
                                            if entity.entity_id in event.participant_ids
                                        ),
                                    )
                                )
                            )[:16],
                            NeedFacetKind.UNRESOLVED_STATUS: tuple(predicates[:16]),
                        },
                    )
                    if target_plan_text:
                        add(
                            identity=f"entity.{entity.entity_id.root}.target-transition",
                            focus=focus,
                            need_type="target_transition_history",
                            intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                            query=(
                                f"{label} {target_plan_text} 已发生历史 前因 当前行动线 "
                                "物件来源 关系约束 动机 知情边界 最近进展 "
                                f"{relation_context} {event_context} {state_context}"
                            ),
                            entity_ids=(entity.entity_id,),
                            section=WriterContextSection.CAUSAL_HISTORY,
                            mandatory=True,
                            priority=97,
                            predicates_by_facet={
                                NeedFacetKind.CAUSAL_HISTORY: tuple(
                                    dict.fromkeys(
                                        (
                                            *predicates,
                                            *relation_predicates,
                                            *(
                                                event.event_type
                                                for event in world.events
                                                if entity.entity_id in event.participant_ids
                                            ),
                                        )
                                    )
                                )[:16],
                            },
                        )
            elif focus.focus_type is TaskFocusType.STATE:
                state = state_by_id.get(focus.canonical_id)
                if state is not None:
                    add(
                        identity=f"state.{state.state_id.root}",
                        focus=focus,
                        need_type="current_state",
                        intent=Stage1QueryIntent.CURRENT_STATE,
                        query=f"{state.predicate} {state.value}",
                        entity_ids=(state.subject_id,),
                        section=WriterContextSection.CURRENT_WORLD_STATE,
                        mandatory=True,
                        priority=95,
                        pools=(CandidatePool.R1, CandidatePool.ANCHOR),
                        predicates_by_facet={
                            NeedFacetKind.CURRENT_STATE: (state.predicate,),
                        },
                    )
            elif focus.focus_type is TaskFocusType.RELATION:
                relation = relation_by_id.get(focus.canonical_id)
                if relation is not None:
                    subject = entity_by_id.get(relation.subject_id)
                    object_ = entity_by_id.get(relation.object_id)
                    add(
                        identity=f"relation.{relation.relation_id.root}",
                        focus=focus,
                        need_type="relationship_emotion",
                        intent=Stage1QueryIntent.RELATION_CHAIN,
                        query=" ".join(
                            (
                                (
                                    subject.internal_label
                                    if subject is not None
                                    else relation.subject_id.root
                                ),
                                relation.predicate,
                                (
                                    object_.internal_label
                                    if object_ is not None
                                    else relation.object_id.root
                                ),
                            )
                        ),
                        entity_ids=(relation.subject_id, relation.object_id),
                        section=WriterContextSection.RELATIONSHIP_AND_EMOTION,
                        mandatory=False,
                        priority=65,
                        pools=(CandidatePool.R1, CandidatePool.ANCHOR, CandidatePool.GRAPH),
                        predicates_by_facet={
                            NeedFacetKind.RELATION_STATE: (relation.predicate,),
                        },
                    )
            elif focus.focus_type is TaskFocusType.EVENT:
                event = event_by_id.get(focus.canonical_id)
                if event is not None:
                    participant_labels = tuple(
                        entity_by_id[participant_id].internal_label
                        for participant_id in event.participant_ids
                        if participant_id in entity_by_id
                    )
                    effect_surfaces = tuple(
                        self._query_value(state.value)
                        for effect_id in event.effect_refs
                        for state in world.states
                        if state.state_id == effect_id
                    )
                    add(
                        identity=f"event.{event.event_id.root}",
                        focus=focus,
                        need_type="causal_history",
                        intent=Stage1QueryIntent.RELATED_EVENT,
                        query=" ".join(
                            dict.fromkeys(
                                (
                                    event.event_type,
                                    *participant_labels,
                                    *effect_surfaces,
                                )
                            )
                        ).strip(),
                        entity_ids=event.participant_ids,
                        section=WriterContextSection.CAUSAL_HISTORY,
                        mandatory=False,
                        priority=60,
                        pools=(CandidatePool.ANCHOR, CandidatePool.GRAPH, CandidatePool.GROUNDED),
                        predicates_by_facet={
                            NeedFacetKind.CAUSAL_HISTORY: (event.event_type,),
                        },
                    )
            elif focus.focus_type is TaskFocusType.OBLIGATION:
                obligation = obligation_by_id.get(focus.canonical_id)
                if obligation is not None and obligation.status in {
                    ObligationStatus.OPEN,
                    ObligationStatus.PROGRESSED,
                }:
                    owner_labels = tuple(
                        entity_by_id[owner_id].internal_label
                        for owner_id in obligation.owner_ids
                        if owner_id in entity_by_id
                    )
                    add(
                        identity=f"obligation.{obligation.obligation_id.root}",
                        focus=focus,
                        need_type="unresolved_obligation",
                        intent=Stage1QueryIntent.KNOWN_ID,
                        query=" ".join((*owner_labels, obligation.description)).strip(),
                        entity_ids=obligation.owner_ids,
                        section=WriterContextSection.CONTINUITY_CONSTRAINTS,
                        mandatory=True,
                        priority=96,
                        pools=(CandidatePool.R1, CandidatePool.ANCHOR, CandidatePool.GROUNDED),
                        # Durable obligations project to PLAN_ANCHOR units whose
                        # predicate is the obligation kind; declare it so the
                        # facet evaluator can bind commitment facets by
                        # predicate (2026-08-14 review follow-up P1).
                        predicates=(obligation.kind.value,),
                        predicates_by_facet={
                            NeedFacetKind.COMMITMENT: (obligation.kind.value,),
                            NeedFacetKind.UNRESOLVED_STATUS: (obligation.kind.value,),
                        },
                    )
            else:
                # TaskFocusType is exhaustive; the remaining variant is PLAN_INTENT.
                node = node_by_id.get(focus.canonical_id)
                if node is not None:
                    summary = getattr(node, "summary", "")
                    plan_query = summary or getattr(node, "title", focus.canonical_id.root)
                    chapter_index = getattr(node, "chapter_index", None)
                    target_relevant = TaskFocusExtractor._plan_node_intersects_target(
                        focus.canonical_id, task
                    ) or (
                        isinstance(chapter_index, int)
                        and task.target_chapter_start <= chapter_index <= task.target_chapter_end
                    )
                    add(
                        identity=f"plan.{focus.canonical_id.root}",
                        focus=focus,
                        need_type="plan_obligation",
                        intent=Stage1QueryIntent.PLAN_OBLIGATION,
                        query=plan_query,
                        entity_ids=(),
                        section=WriterContextSection.PLAN_AND_OBLIGATIONS,
                        mandatory=target_relevant,
                        priority=100 if target_relevant else 50,
                        allow_plan=True,
                        pools=(CandidatePool.ANCHOR,),
                        predicates_by_facet={
                            NeedFacetKind.PLAN_NODE: (getattr(node, "node_type", "plan_node"),),
                        },
                    )
                    for facet_index, facet in enumerate(
                        self._plan_history_facets(plan_query),
                        start=1,
                    ):
                        add(
                            identity=(
                                f"plan-history.{focus.canonical_id.root}.facet.{facet_index}"
                            ),
                            focus=focus,
                            need_type="plan_conditioned_history",
                            intent=Stage1QueryIntent.RELATED_EVENT,
                            query=facet,
                            entity_ids=(),
                            section=WriterContextSection.LONG_RANGE_CALLBACKS,
                            mandatory=target_relevant,
                            priority=98 if target_relevant else 48,
                            pools=(
                                CandidatePool.R1,
                                CandidatePool.ANCHOR,
                                CandidatePool.GROUNDED,
                            ),
                        )

        deduped: dict[
            tuple[str, tuple[StableId, ...], WriterContextSection | None, str],
            Stage1MemoryNeed,
        ] = {}
        for need in candidates:
            key = (
                need.need_type,
                need.entity_ids,
                need.expected_section,
                (
                    need.query_text
                    if need.need_type in {"plan_obligation", "plan_conditioned_history"}
                    else ""
                ),
            )
            previous = deduped.get(key)
            if previous is None:
                deduped[key] = need
                continue
            deduped[key] = previous.model_copy(
                update={
                    "focus_ids": tuple(dict.fromkeys((*previous.focus_ids, *need.focus_ids))),
                    "query_hints": tuple(dict.fromkeys((*previous.query_hints, *need.query_hints))),
                    "priority": max(previous.priority, need.priority),
                }
            )

        ordered = sorted(
            deduped.values(),
            key=lambda item: (
                item.requirement is not RequirementLevel.MANDATORY,
                -item.priority,
                item.need_id.root,
            ),
        )
        retained = tuple(ordered[: self._max_total_needs])
        if task.information_profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED:
            retained = tuple(
                need.model_copy(
                    update={
                        "access_scope": "writer_safe",
                        "allow_plan": False,
                        "planner_may_read_plan": True,
                        "retrieval_may_return_plan": False,
                        "claim_may_cite_plan": False,
                        "legacy_allow_plan": False,
                    }
                )
                for need in retained
                if need.need_type != "plan_obligation"
                and all(
                    facet.facet_kind is not NeedFacetKind.PLAN_NODE for facet in need.need_facets
                )
            )
        planner_artifact = self._last_fallback_artifact
        planner_artifact_ref = self._last_fallback_artifact_ref
        planner_metadata = planner_artifact.metadata if planner_artifact is not None else None
        if planner_fallback:
            retained, planner_artifact, planner_artifact_ref, planner_metadata = (
                self._finalize_fallback_lineage(retained)
            )
        retained_focus_ids = {focus_id for need in retained for focus_id in need.focus_ids}
        unexpanded = tuple(
            focus.focus_id
            for focus in focus_set.focuses
            if focus.focus_id not in retained_focus_ids
        )
        return NeedGenerationResult(
            task_id=task.task_id,
            focus_set=focus_set,
            needs=retained,
            status=(
                NeedGenerationStatus.PLANNER_FALLBACK
                if planner_fallback
                else NeedGenerationStatus.NEED_BUDGET_EXHAUSTED
                if len(ordered) > self._max_total_needs
                else NeedGenerationStatus.READY
            ),
            unexpanded_focus_ids=unexpanded,
            fallback_used=planner_fallback,
            planner_fallback_reason=(
                (planner_artifact.fallback_reason if planner_artifact is not None else None)
                or ("planner_chain_unavailable_or_rejected" if planner_fallback else None)
            ),
            planner_metadata=planner_metadata,
            planner_artifact=planner_artifact,
            planner_artifact_document_ref=planner_artifact_ref,
            need_completion_spec_version=self.completion_spec_version,
            generator_version=self.version,
        )

    @staticmethod
    def _grounding_status_counts(
        drafts: Sequence[GroundedNeedDraft],
    ) -> tuple[int, int, int]:
        """Count (GROUNDED, AMBIGUOUS, UNRESOLVED) entity mentions.

        Feeds the Gate 1 ``grounding_success_rate`` aggregation: success is
        the GROUNDED share of all mentions across the accepted drafts.
        """

        counts = [0, 0, 0]
        for draft in drafts:
            for mention in draft.entity_mentions:
                if mention.grounding_status is GroundingStatus.AMBIGUOUS:
                    counts[1] += 1
                elif mention.grounding_status is GroundingStatus.UNRESOLVED:
                    counts[2] += 1
                else:
                    counts[0] += 1
        return (counts[0], counts[1], counts[2])

    def _missing_goal_entities(
        self,
        *,
        context: AuthorPlanningContext,
        world: WorldRootDocument,
        accepted: tuple[GroundedNeedDraft, ...],
        target_start: int,
        target_end: int,
    ) -> tuple[tuple[int, StableId, str], ...]:
        """Goal/entity coverage postcondition.

        Every entity that is uniquely groundable in a target Plan goal's text
        must appear in at least one accepted Need that triggers that goal.
        Only World labels/aliases that uniquely resolve to one runtime entity
        are required; ambiguous or absent entities are not enforced.
        """

        from novel_agent.services.need_draft_grounder import NeedDraftGrounder

        unique_entities = NeedDraftGrounder._world_label_map(world)
        missing: list[tuple[int, StableId, str]] = []
        accepted_by_goal: dict[int, tuple[GroundedNeedDraft, ...]] = {}
        for draft in accepted:
            for chapter in draft.trigger_plan_chapters:
                accepted_by_goal.setdefault(chapter, ())
                accepted_by_goal[chapter] = (*accepted_by_goal[chapter], draft)
        for goal in context.chapter_goals:
            if not (target_start <= goal.chapter_index <= target_end):
                continue
            from novel_agent.services.need_draft_grounder import NeedDraftGrounder

            normalized_goal = NeedDraftGrounder._normalize(goal.summary)
            goal_entities = self._unique_entities_in_text(normalized_goal, unique_entities)
            goal_drafts = accepted_by_goal.get(goal.chapter_index, ())
            covered_ids = {
                entity_id
                for draft in goal_drafts
                for entity_id in (
                    mention.entity_id
                    for mention in draft.entity_mentions
                    if mention.entity_id is not None
                )
            }
            for label, entity_id in goal_entities:
                if entity_id not in covered_ids:
                    missing.append((goal.chapter_index, entity_id, label))
        return tuple(dict.fromkeys(missing))

    @staticmethod
    def _unique_entities_in_text(
        text: str,
        unique_entities: dict[str, StableId],
    ) -> tuple[tuple[str, StableId], ...]:
        """Longest-first verbatim entity labels found in one text fragment."""

        normalized_by_length = tuple(sorted(unique_entities, key=lambda item: (-len(item), item)))
        occupied: list[tuple[int, int]] = []
        found: list[tuple[str, StableId]] = []
        for normalized in normalized_by_length:
            start = 0
            while True:
                idx = text.find(normalized, start)
                if idx < 0:
                    break
                span = (idx, idx + len(normalized))
                if not any(
                    span[0] < other_end and other_start < span[1]
                    for other_start, other_end in occupied
                ):
                    occupied.append(span)
                    found.append((normalized, unique_entities[normalized]))
                start = idx + 1
        return tuple(found)

    @staticmethod
    def _closed_entity_ids_for_text(
        world: WorldRootDocument,
        texts: tuple[str, ...],
    ) -> tuple[StableId, ...]:
        """Entity ids that appear verbatim in the given text fragments.

        Longest label first; only labels uniquely bound to one runtime entity
        are returned.  Used by the deterministic fallback path.
        """

        from novel_agent.services.need_draft_grounder import NeedDraftGrounder

        unique_entities = NeedDraftGrounder._world_label_map(world)
        combined = "\n".join(text for text in texts if text)
        found = TaskPlanConditionedNeedGenerator._unique_entities_in_text(combined, unique_entities)
        return tuple(dict.fromkeys(entity for _label, entity in found))

    def _run_planner_chain(
        self,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        plan: PlanRootDocument,
        planning_context: AuthorPlanningContext | None,
    ) -> NeedGenerationResult | None:
        """LLM Planner -> Grounder -> Validator chain with deterministic fallback.

        Returns None when the chain cannot produce needs so the caller falls
        back to the deterministic template path (marked PLANNER_FALLBACK).
        """

        assert self._planner is not None
        if planning_context is None:
            raise ValueError("APC Planner requires the compiled AuthorPlanningContext")
        context = planning_context
        planner_result = self._planner.plan(
            task=task,
            world=world,
            planning_context=context,
        )
        if not planner_result.drafts or planner_result.metadata is None:
            reason = planner_result.error_category or "empty_or_unusable_planner_output"
            validated_hash = ArtifactId("sha256:" + "0" * 64)
            fallback_metadata = (
                planner_result.metadata.model_copy(
                    update={"validated_need_set_hash": validated_hash, "fallback_used": True}
                )
                if planner_result.metadata is not None
                else None
            )
            artifact = PlannerInvocationArtifact(
                planning_context=planner_result.planning_context,
                world_summary=planner_result.world_summary,
                exact_prompt=planner_result.exact_prompt,
                metadata=fallback_metadata,
                raw_response=planner_result.raw_response,
                attempts=planner_result.attempts,
                parsed_drafts=planner_result.drafts,
                validated_need_set_hash=validated_hash,
                fallback_status=PlannerFallbackStatus.PLANNER_FALLBACK,
                fallback_reason=reason,
            )
            self._last_fallback_artifact = artifact
            return None
        grounded = tuple(self._grounder.ground(draft, world) for draft in planner_result.drafts)
        focus_set = self._focus_extractor.extract(task, world, plan)
        entity_ids = tuple(
            dict.fromkeys(
                entity_id
                for draft in grounded
                for entity_id in self._grounder.grounded_entity_ids(draft)
            )
        )
        if entity_ids:  # pragma: no branch - accepted historical drafts require an anchor
            focus_set = self._focus_extractor.extend(focus_set, entity_ids)
        validation = self._validator.validate(
            drafts=grounded,
            task=task,
            world=world,
            focus_set=focus_set,
            plan=plan,
        )
        accepted = validation.accepted_drafts
        target_start, target_end = task.target_chapter_start, task.target_chapter_end
        target_goal_chapters = tuple(
            dict.fromkeys(
                goal.chapter_index
                for goal in context.chapter_goals
                if target_start <= goal.chapter_index <= target_end
            )
        )

        def _coverage_state() -> tuple[tuple[int, ...], dict[str, str]]:
            accepted_goal_chapters = tuple(
                dict.fromkeys(
                    chapter for draft in accepted for chapter in draft.trigger_plan_chapters
                )
            )
            missing = tuple(
                chapter for chapter in target_goal_chapters if chapter not in accepted_goal_chapters
            )
            return missing, self._planner_contract_findings(grounded)

        missing_goal_chapters, contract_findings = _coverage_state()
        # One bounded semantic-repair round (review-25 P0-4b + #1, review-26
        # terminal-audit): when the target-goal union is incomplete or explicit
        # labels violate the exact-canonical-label contract, issue exactly one
        # repair request naming the missing chapters and the offending labels,
        # with the canonical label map.  The repair invocation is ALWAYS
        # terminal: its attempts, prompt, raw response and error category are
        # merged into the persisted lineage even when it fails; a failed repair
        # never resumes evaluation of the first drafts and never substitutes
        # deterministic Needs (the runner fail-closes under
        # require_model_decisions).
        semantic_repair_attempted = False
        if (missing_goal_chapters or contract_findings) and accepted and self._planner is not None:
            semantic_repair_attempted = True
            repair_instruction = self._build_repair_instruction(
                missing_goal_chapters=missing_goal_chapters,
                contract_findings=contract_findings,
                canonical_labels=self._canonical_label_map(world),
            )
            repaired = self._planner.plan(
                task=task,
                world=world,
                planning_context=context,
                repair_instruction=repair_instruction,
                max_retries=0,
            )
            planner_result = self._merge_planner_attempts(planner_result, repaired)
            if repaired.drafts and repaired.metadata is not None:
                grounded = tuple(
                    self._grounder.ground(draft, world) for draft in planner_result.drafts
                )
                focus_set = self._focus_extractor.extract(task, world, plan)
                entity_ids = tuple(
                    dict.fromkeys(
                        entity_id
                        for draft in grounded
                        for entity_id in self._grounder.grounded_entity_ids(draft)
                    )
                )
                if entity_ids:  # pragma: no branch
                    focus_set = self._focus_extractor.extend(focus_set, entity_ids)
                validation = self._validator.validate(
                    drafts=grounded,
                    task=task,
                    world=world,
                    focus_set=focus_set,
                    plan=plan,
                )
                accepted = validation.accepted_drafts
                missing_goal_chapters, contract_findings = _coverage_state()
            else:
                # Typed semantic-repair fallback: the repair invocation is
                # terminal.  Persist BOTH invocations, the repair prompt/raw
                # and the real failure category; do not continue with the
                # first response's coverage/contract verdict.
                reason = repaired.error_category or "semantic_repair_no_usable_output"
                rejected_hash = ArtifactId("sha256:" + "0" * 64)
                fallback_metadata = (
                    planner_result.metadata.model_copy(
                        update={
                            "validated_need_set_hash": rejected_hash,
                            "fallback_used": True,
                        }
                    )
                    if planner_result.metadata is not None
                    else None
                )
                artifact = PlannerInvocationArtifact(
                    planning_context=planner_result.planning_context,
                    world_summary=planner_result.world_summary,
                    exact_prompt=planner_result.exact_prompt,
                    metadata=fallback_metadata,
                    raw_response=planner_result.raw_response,
                    attempts=planner_result.attempts,
                    parsed_drafts=planner_result.drafts,
                    grounded_drafts=(),
                    accepted_draft_ids=(),
                    rejected_reasons={},
                    deduplicated_draft_ids=(),
                    truncated_draft_ids=(),
                    final_need_manifests=(),
                    validated_need_set_hash=rejected_hash,
                    fallback_status=PlannerFallbackStatus.PLANNER_FALLBACK,
                    fallback_reason=reason,
                    missing_goal_chapters=missing_goal_chapters,
                    planner_contract_findings=dict(contract_findings),
                )
                self._last_fallback_artifact = artifact
                return self._terminal_planner_fallback_result(task, world, plan)
        # The early guard plus the repair merge keep a non-None metadata.
        assert planner_result.metadata is not None
        if not validation.accepted_drafts:
            rejected_hash = ArtifactId("sha256:" + "0" * 64)
            fallback_metadata = planner_result.metadata.model_copy(
                update={
                    "validated_need_set_hash": rejected_hash,
                    "fallback_used": True,
                }
            )
            artifact = PlannerInvocationArtifact(
                planning_context=planner_result.planning_context,
                world_summary=planner_result.world_summary,
                exact_prompt=planner_result.exact_prompt,
                metadata=fallback_metadata,
                raw_response=planner_result.raw_response,
                attempts=planner_result.attempts,
                parsed_drafts=planner_result.drafts,
                grounded_drafts=grounded,
                rejected_reasons=validation.rejected_reasons,
                deduplicated_draft_ids=validation.deduplicated_draft_ids,
                truncated_draft_ids=validation.truncated_draft_ids,
                validated_need_set_hash=rejected_hash,
                fallback_status=PlannerFallbackStatus.PLANNER_FALLBACK,
                fallback_reason="all_drafts_rejected",
                planner_contract_findings=dict(contract_findings),
                raw_scope_by_draft=validation.raw_scope_by_draft,
                canonical_scope_by_draft=validation.canonical_scope_by_draft,
                scope_normalization_reasons=validation.scope_normalization_reasons,
            )
            self._last_fallback_artifact = artifact
            if semantic_repair_attempted:
                return self._terminal_planner_fallback_result(task, world, plan)
            return None
        accepted = validation.accepted_drafts
        target_start, target_end = task.target_chapter_start, task.target_chapter_end
        target_goal_chapters = tuple(
            dict.fromkeys(
                goal.chapter_index
                for goal in context.chapter_goals
                if target_start <= goal.chapter_index <= target_end
            )
        )
        if missing_goal_chapters or (semantic_repair_attempted and contract_findings):
            rejected_hash = ArtifactId("sha256:" + "0" * 64)
            fallback_metadata = planner_result.metadata.model_copy(
                update={
                    "validated_need_set_hash": rejected_hash,
                    "fallback_used": True,
                }
            )
            artifact = PlannerInvocationArtifact(
                planning_context=planner_result.planning_context,
                world_summary=planner_result.world_summary,
                exact_prompt=planner_result.exact_prompt,
                metadata=fallback_metadata,
                raw_response=planner_result.raw_response,
                attempts=planner_result.attempts,
                parsed_drafts=planner_result.drafts,
                grounded_drafts=grounded,
                accepted_draft_ids=tuple(draft.draft_id for draft in accepted),
                rejected_reasons=validation.rejected_reasons,
                deduplicated_draft_ids=validation.deduplicated_draft_ids,
                truncated_draft_ids=validation.truncated_draft_ids,
                final_need_manifests=(),
                validated_need_set_hash=rejected_hash,
                fallback_status=PlannerFallbackStatus.PLANNER_FALLBACK,
                fallback_reason="insufficient_target_goal_coverage",
                missing_goal_chapters=missing_goal_chapters,
                planner_contract_findings=dict(contract_findings),
            )
            self._last_fallback_artifact = artifact
            if semantic_repair_attempted:
                return self._terminal_planner_fallback_result(task, world, plan)
            return None
        missing_goal_entities = self._missing_goal_entities(
            context=context,
            world=world,
            accepted=accepted,
            target_start=target_start,
            target_end=target_end,
        )
        if missing_goal_entities:
            rejected_hash = ArtifactId("sha256:" + "0" * 64)
            fallback_metadata = planner_result.metadata.model_copy(
                update={
                    "validated_need_set_hash": rejected_hash,
                    "fallback_used": True,
                }
            )
            artifact = PlannerInvocationArtifact(
                planning_context=planner_result.planning_context,
                world_summary=planner_result.world_summary,
                exact_prompt=planner_result.exact_prompt,
                metadata=fallback_metadata,
                raw_response=planner_result.raw_response,
                attempts=planner_result.attempts,
                parsed_drafts=planner_result.drafts,
                grounded_drafts=grounded,
                accepted_draft_ids=tuple(draft.draft_id for draft in accepted),
                rejected_reasons=validation.rejected_reasons,
                deduplicated_draft_ids=validation.deduplicated_draft_ids,
                truncated_draft_ids=validation.truncated_draft_ids,
                final_need_manifests=(),
                validated_need_set_hash=rejected_hash,
                fallback_status=PlannerFallbackStatus.PLANNER_FALLBACK,
                fallback_reason="insufficient_target_goal_entity_coverage",
                missing_goal_entities=tuple(
                    (entity_id.root, label, chapter)
                    for chapter, entity_id, label in missing_goal_entities
                ),
                planner_contract_findings=dict(contract_findings),
            )
            self._last_fallback_artifact = artifact
            if semantic_repair_attempted:
                return self._terminal_planner_fallback_result(task, world, plan)
            return None
        placeholder_hash = ArtifactId("sha256:" + "0" * 64)
        placeholder_ref = ArtifactId("sha256:" + "0" * 64)
        canonical_goals = {goal.chapter_index: goal.summary for goal in context.chapter_goals}
        provisional_needs = tuple(
            self._build_planner_need(
                task=task,
                world=world,
                draft=draft,
                need_type=validation.need_type_by_draft[draft.draft_id],
                focus_set=focus_set,
                artifact_ref=placeholder_ref,
                validated_hash=placeholder_hash,
                canonical_goals=canonical_goals,
            )
            for draft in accepted
        )
        manifests = self._final_need_manifests(
            provisional_needs,
            tuple(draft.draft_id for draft in accepted),
        )
        validated_hash = self._final_need_set_hash(manifests)
        metadata = planner_result.metadata.model_copy(
            update={"validated_need_set_hash": validated_hash, "fallback_used": False}
        )
        artifact = PlannerInvocationArtifact(
            planning_context=planner_result.planning_context,
            world_summary=planner_result.world_summary,
            exact_prompt=planner_result.exact_prompt,
            metadata=metadata,
            raw_response=planner_result.raw_response,
            attempts=planner_result.attempts,
            parsed_drafts=planner_result.drafts,
            grounded_drafts=grounded,
            accepted_draft_ids=tuple(draft.draft_id for draft in accepted),
            rejected_reasons=validation.rejected_reasons,
            deduplicated_draft_ids=validation.deduplicated_draft_ids,
            truncated_draft_ids=validation.truncated_draft_ids,
            final_need_manifests=manifests,
            validated_need_set_hash=validated_hash,
            fallback_status=PlannerFallbackStatus.PLANNER,
            planner_contract_findings=dict(contract_findings),
            raw_scope_by_draft=validation.raw_scope_by_draft,
            canonical_scope_by_draft=validation.canonical_scope_by_draft,
            scope_normalization_reasons=validation.scope_normalization_reasons,
        )
        artifact_document_ref = self._persist_planner_artifact(artifact)
        artifact_ref = (
            artifact_document_ref.artifact_id
            if artifact_document_ref is not None
            else content_id(artifact.model_dump(mode="json"))
        )
        # Grounding diagnostics cover the complete Planner output, including
        # drafts later rejected by deterministic validation.
        grounding_counts = self._grounding_status_counts(grounded)
        needs = tuple(
            need.model_copy(
                update={
                    "planner_artifact_ref": artifact_ref,
                    "validated_need_set_hash": validated_hash,
                }
            )
            for need in provisional_needs
        )
        return NeedGenerationResult(
            task_id=task.task_id,
            focus_set=focus_set,
            needs=needs,
            status=NeedGenerationStatus.READY,
            planner_metadata=metadata,
            fallback_used=False,
            grounding_status_counts=grounding_counts,
            need_completion_spec_version=self.completion_spec_version,
            generator_version=self.version,
            planner_artifact=artifact,
            planner_artifact_document_ref=artifact_document_ref,
        )

    def _terminal_planner_fallback_result(
        self,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        plan: PlanRootDocument,
    ) -> NeedGenerationResult:
        """Persist a failed semantic repair without substituting template Needs.

        A bounded repair is the terminal model decision.  Once it fails or
        remains outside the Planner contract, returning deterministic Needs
        would make the caller believe the failed model decision was usable.
        The empty final set and merged invocation artifact make that state
        explicit to the evidence-first runner.
        """

        needs, artifact, artifact_ref, metadata = self._finalize_fallback_lineage(())
        focus_set = self._focus_extractor.extract(task, world, plan)
        return NeedGenerationResult(
            task_id=task.task_id,
            focus_set=focus_set,
            needs=needs,
            status=NeedGenerationStatus.PLANNER_FALLBACK,
            unexpanded_focus_ids=tuple(focus.focus_id for focus in focus_set.focuses),
            planner_metadata=metadata,
            fallback_used=True,
            planner_fallback_reason=artifact.fallback_reason,
            grounding_status_counts=self._grounding_status_counts(artifact.grounded_drafts),
            need_completion_spec_version=self.completion_spec_version,
            generator_version=self.version,
            planner_artifact=artifact,
            planner_artifact_document_ref=artifact_ref,
        )

    def _planner_contract_findings(
        self,
        grounded: tuple[GroundedNeedDraft, ...],
    ) -> dict[str, str]:
        """Typed Planner-output-contract findings (review-25).

        The Planner prompt requires every explicit entity mention and relation
        endpoint to copy exactly one canonical WORLD label or unique alias.
        The grounder stays exact; a composite/descriptive/annotated explicit
        label that cannot resolve is a typed, retryable Planner-contract
        finding (never a fuzzy ID guess).  ``draft_id -> finding``.
        """

        findings: dict[str, str] = {}
        for draft in grounded:
            explicit_entities = tuple(
                mention for mention in draft.entity_mentions if mention.mention_source == "explicit"
            )
            for mention in explicit_entities:
                if mention.grounding_status is GroundingStatus.UNRESOLVED:
                    findings.setdefault(
                        draft.draft_id,
                        f"planner_contract_label_not_in_world:{mention.mention}",
                    )
                elif mention.grounding_status is GroundingStatus.AMBIGUOUS:
                    findings.setdefault(
                        draft.draft_id,
                        f"planner_contract_label_ambiguous:{mention.mention}",
                    )
            for relation in draft.relation_mentions:
                if relation.grounding_status is GroundingStatus.UNRESOLVED:
                    findings.setdefault(
                        draft.draft_id,
                        "planner_contract_relation_endpoint_not_in_world:"
                        f"{relation.subject_label}/{relation.object_label}",
                    )
                elif relation.grounding_status is GroundingStatus.AMBIGUOUS:
                    findings.setdefault(
                        draft.draft_id,
                        "planner_contract_relation_endpoint_ambiguous:"
                        f"{relation.subject_label}/{relation.object_label}",
                    )
        return findings

    @staticmethod
    def _canonical_label_map(
        world: WorldRootDocument,
        *,
        limit: int = 120,
    ) -> tuple[str, ...]:
        """Bounded canonical label map for the repair prompt.

        Built from ``NeedDraftGrounder._resolvable_label_map`` semantics so
        every advertised value exact-grounds (review-26): each canonical
        internal label and each uniquely resolvable alias is listed as a
        separate exact value; ambiguous aliases and annotated/composite
        display strings are never advertised.
        """

        from novel_agent.services.need_draft_grounder import NeedDraftGrounder

        resolvable = NeedDraftGrounder._resolvable_label_map(world)
        labels: list[str] = []
        seen: set[str] = set()
        for entity in world.entities:
            if not entity.internal_label.strip():
                continue
            normalized = NeedDraftGrounder._normalize(entity.internal_label)
            if normalized in resolvable and normalized not in seen:
                seen.add(normalized)
                labels.append(entity.internal_label)
            for alias in dict.fromkeys(entity.aliases):
                if not alias.strip():
                    continue
                normalized_alias = NeedDraftGrounder._normalize(alias)
                if normalized_alias in resolvable and normalized_alias not in seen:
                    seen.add(normalized_alias)
                    labels.append(alias)
            if len(labels) >= limit:
                break
        return tuple(labels)

    @staticmethod
    def _build_repair_instruction(
        *,
        missing_goal_chapters: tuple[int, ...],
        contract_findings: dict[str, str],
        canonical_labels: tuple[str, ...],
    ) -> str:
        """One bounded repair request naming exact missing chapters and
        offending explicit labels, with the canonical label map (review-25).
        """

        parts: list[str] = []
        if missing_goal_chapters:
            parts.append(
                "你遗漏了以下目标章节: "
                + "、".join(str(chapter) for chapter in missing_goal_chapters)
                + "。必须为这些章节各补充至少一条问题, 且 trigger_plan_chapters"
                " 必须包含对应章节编号。"
            )
        if contract_findings:
            offending = tuple(dict.fromkeys(contract_findings.values()))
            parts.append(
                "以下显式标签不是世界摘要中的规范实体标签或唯一别名, 不得使用: "
                + "、".join(offending)
                + "。请把每个显式 entity_mentions 标签和 relation_mentions 端点替换为"
                "下列规范标签中的某一个(原样复制), 或删除该 mention。"
            )
        if canonical_labels:
            parts.append(
                "规范标签映射(只使用这些):\n" + "\n".join(f"- {item}" for item in canonical_labels)
            )
        return "\n".join(parts)

    def _merge_planner_attempts(
        self,
        first: PlannerRunResult,
        second: PlannerRunResult,
    ) -> PlannerRunResult:
        """Merge a bounded repair invocation into the first result.

        The repair invocation is TERMINAL: its prompt, raw response, error
        category and fallback status always become the terminal ones, and its
        attempts are ALWAYS merged so a failed repair is still fully auditable
        (review-26).  Drafts become terminal only when the repair produced
        usable drafts; otherwise the merged result keeps the first drafts but
        carries the repair's failure category, and callers must not resume
        evaluation of those drafts (the chain fail-closes instead).  Metadata
        token usage aggregates across both invocations.
        """

        merged_metadata = second.metadata
        if first.metadata is not None and second.metadata is not None:
            merged_metadata = second.metadata.model_copy(
                update={
                    "input_tokens": first.metadata.input_tokens + second.metadata.input_tokens,
                    "output_tokens": first.metadata.output_tokens + second.metadata.output_tokens,
                }
            )
        return first.model_copy(
            update={
                "drafts": second.drafts or first.drafts,
                "metadata": merged_metadata or first.metadata,
                "fallback_status": second.fallback_status,
                "error_category": second.error_category,
                "exact_prompt": second.exact_prompt,
                "raw_response": second.raw_response,
                "attempts": (*first.attempts, *second.attempts),
            }
        )

    def generate_evidence_first(
        self,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        plan: PlanRootDocument,
        planning_context: AuthorPlanningContext | None,
        frozen_planner_artifact: PlannerInvocationArtifact,
    ) -> NeedGenerationResult | None:
        """Offline evidence-first Need rebuild from frozen Planner drafts.

        Re-grounds and re-validates the frozen raw drafts with the current
        deterministic rules (exact internal-label priority, unresolved lexical
        anchors preserved) and rebuilds the final Needs with a fresh
        validated identity.  No model is called; fallback artifacts keep the
        frozen deterministic template behavior (``None`` -> template path).
        """
        if frozen_planner_artifact.metadata is None:
            raise ValueError("evidence-first replay requires frozen Planner metadata")
        if frozen_planner_artifact.fallback_status is PlannerFallbackStatus.PLANNER_FALLBACK:
            return None
        if planning_context is None:
            raise ValueError("evidence-first replay requires AuthorPlanningContext")
        grounded = tuple(
            self._grounder.ground(draft, world) for draft in frozen_planner_artifact.parsed_drafts
        )
        focus_set = self._focus_extractor.extract(task, world, plan)
        entity_ids = tuple(
            dict.fromkeys(
                entity_id
                for draft in grounded
                for entity_id in self._grounder.grounded_entity_ids(draft)
            )
        )
        if entity_ids:  # pragma: no branch - accepted drafts require an anchor
            focus_set = self._focus_extractor.extend(focus_set, entity_ids)
        validation = self._validator.validate(
            drafts=grounded,
            task=task,
            world=world,
            focus_set=focus_set,
            plan=plan,
        )
        accepted = validation.accepted_drafts
        if not accepted:
            self._last_fallback_artifact = frozen_planner_artifact
            self._frozen_fallback_artifact = frozen_planner_artifact
            return None
        target_start, target_end = task.target_chapter_start, task.target_chapter_end
        target_goal_chapters = tuple(
            dict.fromkeys(
                goal.chapter_index
                for goal in planning_context.chapter_goals
                if target_start <= goal.chapter_index <= target_end
            )
        )
        accepted_goal_chapters = tuple(
            dict.fromkeys(chapter for draft in accepted for chapter in draft.trigger_plan_chapters)
        )
        missing_goal_chapters = tuple(
            chapter for chapter in target_goal_chapters if chapter not in accepted_goal_chapters
        )
        if missing_goal_chapters:
            self._last_fallback_artifact = frozen_planner_artifact
            self._frozen_fallback_artifact = frozen_planner_artifact
            return None
        placeholder = ArtifactId("sha256:" + "0" * 64)
        canonical_goals = {
            goal.chapter_index: goal.summary for goal in planning_context.chapter_goals
        }
        provisional = tuple(
            self._build_planner_need(
                task=task,
                world=world,
                draft=draft,
                need_type=validation.need_type_by_draft[draft.draft_id],
                focus_set=focus_set,
                artifact_ref=placeholder,
                validated_hash=placeholder,
                canonical_goals=canonical_goals,
            )
            for draft in accepted
        )
        manifests = self._final_need_manifests(
            provisional,
            tuple(draft.draft_id for draft in accepted),
        )
        validated_hash = self._final_need_set_hash(manifests)
        metadata = frozen_planner_artifact.metadata.model_copy(
            update={"validated_need_set_hash": validated_hash, "fallback_used": False}
        )
        refreshed = frozen_planner_artifact.model_copy(
            update={
                "metadata": metadata,
                "grounded_drafts": grounded,
                "accepted_draft_ids": tuple(draft.draft_id for draft in accepted),
                "rejected_reasons": validation.rejected_reasons,
                "deduplicated_draft_ids": validation.deduplicated_draft_ids,
                "truncated_draft_ids": validation.truncated_draft_ids,
                "final_need_manifests": manifests,
                "validated_need_set_hash": validated_hash,
                "raw_scope_by_draft": validation.raw_scope_by_draft,
                "canonical_scope_by_draft": validation.canonical_scope_by_draft,
                "scope_normalization_reasons": validation.scope_normalization_reasons,
            }
        )
        artifact_ref = self._persist_planner_artifact(refreshed)
        artifact_id = (
            artifact_ref.artifact_id
            if artifact_ref is not None
            else content_id(refreshed.model_dump(mode="json"))
        )
        needs = tuple(
            need.model_copy(
                update={
                    "planner_artifact_ref": artifact_id,
                    "validated_need_set_hash": validated_hash,
                }
            )
            for need in provisional
        )
        return NeedGenerationResult(
            task_id=task.task_id,
            focus_set=focus_set,
            needs=needs,
            status=NeedGenerationStatus.READY,
            planner_metadata=metadata,
            fallback_used=False,
            grounding_status_counts=self._grounding_status_counts(grounded),
            need_completion_spec_version=self.completion_spec_version,
            generator_version=self.version,
            planner_artifact=refreshed,
            planner_artifact_document_ref=artifact_ref,
        )

    @staticmethod
    def _final_need_manifests(
        needs: tuple[Stage1MemoryNeed, ...],
        source_draft_ids: tuple[str, ...],
    ) -> tuple[PlannerFinalNeedManifest, ...]:
        if len(needs) != len(source_draft_ids):
            raise ValueError("final Need lineage count does not match source identities")
        compiler = NeedQueryCompiler()
        manifests: list[PlannerFinalNeedManifest] = []
        for need, source_draft_id in zip(needs, source_draft_ids, strict=True):
            need_payload = need.model_dump(
                mode="json",
                exclude={
                    "planner_artifact_ref",
                    "planned_draft_id",
                    "validated_need_set_hash",
                    "completion_spec",
                },
            )
            manifests.append(
                PlannerFinalNeedManifest(
                    need_id=need.need_id,
                    source_draft_id=source_draft_id,
                    need_payload_hash=content_id(need_payload),
                    completion_contract_hash=content_id(
                        need.completion_spec.model_dump(mode="json")
                        if need.completion_spec is not None
                        else None
                    ),
                    query_bundle_hash=content_id(compiler.compile(need).model_dump(mode="json")),
                )
            )
        return tuple(manifests)

    @staticmethod
    def _final_need_set_hash(
        manifests: tuple[PlannerFinalNeedManifest, ...],
    ) -> ArtifactId:
        return content_id({"final_needs": [item.model_dump(mode="json") for item in manifests]})

    def _finalize_fallback_lineage(
        self,
        needs: tuple[Stage1MemoryNeed, ...],
    ) -> tuple[
        tuple[Stage1MemoryNeed, ...],
        PlannerInvocationArtifact,
        ArtifactRef | None,
        PlannerArtifactMetadata | None,
    ]:
        artifact = self._last_fallback_artifact
        if artifact is None:
            raise ValueError("Planner fallback is missing its invocation artifact")
        source_ids = tuple(
            f"fallback.{index:03d}.{need.need_id.root}" for index, need in enumerate(needs, start=1)
        )
        provisional = tuple(
            need.model_copy(
                update={
                    "semantic_question": need.semantic_question or need.query_text,
                    "planned_draft_id": source_id,
                }
            )
            for need, source_id in zip(needs, source_ids, strict=True)
        )
        manifests = self._final_need_manifests(provisional, source_ids)
        validated_hash = self._final_need_set_hash(manifests)
        metadata = (
            artifact.metadata.model_copy(
                update={"validated_need_set_hash": validated_hash, "fallback_used": True}
            )
            if artifact.metadata is not None
            else None
        )
        finalized = artifact.model_copy(
            update={
                "metadata": metadata,
                "validated_need_set_hash": validated_hash,
                "final_need_manifests": manifests,
            }
        )
        frozen = self._frozen_fallback_artifact
        if frozen is not None:
            if (
                frozen.final_need_manifests != manifests
                or frozen.validated_need_set_hash != validated_hash
            ):
                raise ValueError("Planner artifact replay final Need set mismatch")
            finalized = frozen
            artifact_document_ref = self._persist_planner_artifact(finalized)
        else:
            artifact_document_ref = self._persist_planner_artifact(finalized)
        artifact_id = (
            artifact_document_ref.artifact_id
            if artifact_document_ref is not None
            else content_id(finalized.model_dump(mode="json"))
        )
        lineaged = tuple(
            need.model_copy(
                update={
                    "planner_artifact_ref": artifact_id,
                    "validated_need_set_hash": validated_hash,
                }
            )
            for need in provisional
        )
        self._last_fallback_artifact = finalized
        self._last_fallback_artifact_ref = artifact_document_ref
        return lineaged, finalized, artifact_document_ref, finalized.metadata

    def _replay_planner_artifact(
        self,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        plan: PlanRootDocument,
        planning_context: AuthorPlanningContext | None,
        artifact: PlannerInvocationArtifact,
    ) -> NeedGenerationResult | None:
        """Rebuild final Needs without a model and verify every frozen identity."""

        if planning_context is None:
            raise ValueError("frozen Planner replay requires AuthorPlanningContext")
        if artifact.metadata is None:
            raise ValueError("frozen Planner replay requires model-policy metadata")
        planner = self._planner or PlanConditionedNeedPlanner()
        replay = planner.replay(
            artifact,
            task=task,
            world=world,
            planning_context=planning_context,
            planner_model=artifact.metadata.planner_model,
            planner_model_revision=artifact.metadata.planner_model_revision,
        )
        if replay.fallback_status is PlannerFallbackStatus.PLANNER_FALLBACK:
            self._last_fallback_artifact = artifact
            self._frozen_fallback_artifact = artifact
            return None

        grounded = tuple(self._grounder.ground(draft, world) for draft in replay.drafts)
        if grounded != artifact.grounded_drafts:
            raise ValueError("Planner artifact replay grounded drafts mismatch")
        focus_set = self._focus_extractor.extract(task, world, plan)
        entity_ids = tuple(
            dict.fromkeys(
                entity_id
                for draft in grounded
                for entity_id in self._grounder.grounded_entity_ids(draft)
            )
        )
        if entity_ids:  # pragma: no branch - frozen accepted drafts require an anchor
            focus_set = self._focus_extractor.extend(focus_set, entity_ids)
        validation = self._validator.validate(
            drafts=grounded,
            task=task,
            world=world,
            focus_set=focus_set,
            plan=plan,
        )
        accepted = validation.accepted_drafts
        if (
            tuple(draft.draft_id for draft in accepted) != artifact.accepted_draft_ids
            or validation.rejected_reasons != artifact.rejected_reasons
            or validation.deduplicated_draft_ids != artifact.deduplicated_draft_ids
            or validation.truncated_draft_ids != artifact.truncated_draft_ids
        ):
            raise ValueError("Planner artifact replay validation outcome mismatch")
        placeholder = ArtifactId("sha256:" + "0" * 64)
        canonical_goals = {
            goal.chapter_index: goal.summary for goal in planning_context.chapter_goals
        }
        provisional = tuple(
            self._build_planner_need(
                task=task,
                world=world,
                draft=draft,
                need_type=validation.need_type_by_draft[draft.draft_id],
                focus_set=focus_set,
                artifact_ref=placeholder,
                validated_hash=placeholder,
                canonical_goals=canonical_goals,
            )
            for draft in accepted
        )
        manifests = self._final_need_manifests(
            provisional,
            tuple(draft.draft_id for draft in accepted),
        )
        validated_hash = self._final_need_set_hash(manifests)
        if (
            manifests != artifact.final_need_manifests
            or validated_hash != artifact.validated_need_set_hash
        ):
            raise ValueError("Planner artifact replay final Need set mismatch")
        artifact_id = content_id(artifact.model_dump(mode="json"))
        artifact_document_ref = self._persist_planner_artifact(artifact)
        needs = tuple(
            need.model_copy(
                update={
                    "planner_artifact_ref": artifact_id,
                    "validated_need_set_hash": validated_hash,
                }
            )
            for need in provisional
        )
        return NeedGenerationResult(
            task_id=task.task_id,
            focus_set=focus_set,
            needs=needs,
            status=NeedGenerationStatus.READY,
            planner_metadata=artifact.metadata,
            fallback_used=False,
            grounding_status_counts=self._grounding_status_counts(grounded),
            need_completion_spec_version=self.completion_spec_version,
            generator_version=self.version,
            planner_artifact=artifact,
            planner_artifact_document_ref=artifact_document_ref,
        )

    def _persist_planner_artifact(self, artifact: PlannerInvocationArtifact) -> ArtifactRef | None:
        if self._planner_artifact_writer is None:
            return None
        return self._planner_artifact_writer(
            canonical_json_bytes(artifact.model_dump(mode="json")),
            "application/vnd.novel-agent.planner-invocation+json",
        )

    def _build_planner_need(
        self,
        *,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        draft: GroundedNeedDraft,
        need_type: str,
        focus_set: FocusSet,
        artifact_ref: ArtifactId,
        validated_hash: ArtifactId,
        canonical_goals: Mapping[int, str] | None = None,
    ) -> Stage1MemoryNeed:
        entity_ids = self._grounder.grounded_entity_ids(draft)
        sanitized = NeedValidator.sanitize_draft_id(draft.draft_id)
        need_id = StableId(f"need.stage2m.planner.{sanitized}"[:128])
        # Host-verified canonical goal binding: the model's trigger_plan_goal
        # stays an auditable explanation; the Need carries the plan's canonical
        # goal text per trigger chapter (complete binding only).
        canonical_goal_by_chapter = (
            {chapter: canonical_goals[chapter] for chapter in draft.trigger_plan_chapters}
            if canonical_goals is not None
            and all(chapter in canonical_goals for chapter in draft.trigger_plan_chapters)
            else {}
        )
        focus_by_canonical = {
            item.canonical_id: item.focus_id
            for item in focus_set.focuses
            if item.focus_type is TaskFocusType.ENTITY
        }
        focus_ids = tuple(
            dict.fromkeys(
                focus_by_canonical[entity_id]
                for entity_id in entity_ids
                if entity_id in focus_by_canonical
            )
        )
        primary_focus = next(
            (
                item
                for item in focus_set.focuses
                if item.focus_type is TaskFocusType.ENTITY and item.canonical_id in entity_ids
            ),
            None,
        )
        draft_focus = TaskFocus(
            focus_id=StableId(f"focus.planner.draft.{sanitized}"[:128]),
            focus_type=TaskFocusType.ENTITY if entity_ids else TaskFocusType.PLAN_INTENT,
            canonical_id=entity_ids[0] if entity_ids else StableId(f"plan.planner.{sanitized}"),
            source=TaskFocusSource.PLAN_INTENT,
            reason=draft.why_needed or "planner draft focus",
        )
        focus = primary_focus or draft_focus
        facet_kinds = tuple(NeedFacetKind[item] for item in draft.suggested_facets)
        intent = self._INTENT_BY_NEED_TYPE[need_type]
        section = self._SECTION_BY_NEED_TYPE[need_type]
        pools = self._POOLS_BY_NEED_TYPE[need_type]
        # Facet-level predicate binding: derive, per facet kind, the predicates
        # of the matching world-record kinds for the grounded entities, so a
        # state predicate never closes knowledge/capability facets and
        # relation/event/obligation anchors can close their own facets
        # (2026-08-14 review second follow-up P1).
        state_predicates = tuple(
            dict.fromkeys(
                state.predicate
                for state in world.states
                if state.subject_id in entity_ids and state.predicate
            )
        )[:16]
        relation_predicates = tuple(
            dict.fromkeys(
                relation.predicate
                for relation in world.relations
                if relation.predicate
                and bool(set((relation.subject_id, relation.object_id)) & set(entity_ids))
            )
        )[:16]
        event_predicates = tuple(
            dict.fromkeys(
                event.event_type
                for event in world.events
                if event.event_type and set(event.participant_ids) & set(entity_ids)
            )
        )[:16]
        obligation_predicates = tuple(
            dict.fromkeys(
                obligation.kind.value
                for obligation in world.obligations
                if set(obligation.owner_ids) & set(entity_ids)
            )
        )[:16]
        facet_predicates: dict[NeedFacetKind, tuple[str, ...]] = {
            NeedFacetKind.CURRENT_STATE: state_predicates,
            NeedFacetKind.CAPABILITY_STATUS: self._predicates_by_keywords(
                state_predicates, self._CAPABILITY_PREDICATE_KEYWORDS
            ),
            NeedFacetKind.LIMITATION: self._predicates_by_keywords(
                state_predicates, self._CAPABILITY_PREDICATE_KEYWORDS
            ),
            NeedFacetKind.KNOWLEDGE_BOUNDARY: self._predicates_by_keywords(
                state_predicates, self._KNOWLEDGE_PREDICATE_KEYWORDS
            ),
            NeedFacetKind.RELATION_STATE: relation_predicates,
            NeedFacetKind.CAUSAL_HISTORY: tuple(
                dict.fromkeys((*state_predicates, *event_predicates))
            ),
            NeedFacetKind.SETUP: tuple(dict.fromkeys((*state_predicates, *event_predicates))),
            NeedFacetKind.COMMITMENT: obligation_predicates,
            NeedFacetKind.UNRESOLVED_STATUS: obligation_predicates,
            NeedFacetKind.PLAN_NODE: (),
        }
        facets, completion_spec = self._completion_contract(
            need_id=need_id,
            need_type=need_type,
            section=section,
            task=task,
            focus=focus,
            allow_plan=False,
            mandatory=True,
            facet_kinds_override=facet_kinds,
            predicates_by_facet=facet_predicates,
        )
        need_predicates = tuple(
            dict.fromkeys(
                predicate
                for facet_kind in facet_kinds
                for predicate in facet_predicates.get(facet_kind, ())
            )
        )
        hints = tuple(
            dict.fromkeys(hint for hint in draft.query_hints if hint != draft.semantic_question)
        )
        return Stage1MemoryNeed(
            need_id=need_id,
            run_id=RunId(f"run.stage2m.{task.task_id.root}"[:128]),
            task_id=TaskId(task.task_id.root),
            base_commit=world.source_commit,
            horizon_target=(task.target_chapter_start, task.target_chapter_end),
            need_type=need_type,
            query_intent=intent,
            query_text=draft.semantic_question,
            semantic_question=draft.semantic_question,
            trigger_plan_chapters=draft.trigger_plan_chapters,
            trigger_plan_goal=draft.trigger_plan_goal,
            canonical_goal_by_chapter=canonical_goal_by_chapter,
            entity_ids=entity_ids,
            predicates=need_predicates,
            access_scope="writer_safe",
            allow_plan=False,
            planner_may_read_plan=True,
            retrieval_may_return_plan=False,
            claim_may_cite_plan=False,
            legacy_allow_plan=False,
            why_needed=draft.why_needed or focus.reason,
            risk_level=NeedRisk.HIGH,
            requirement=RequirementLevel.MANDATORY,
            preferred_resolution_path=(
                ResolutionPath.EXACT_TEMPORAL
                if CandidatePool.R1 in pools
                else ResolutionPath.ANCHOR_FIRST
            ),
            allowed_candidate_pools=pools,
            expected_evidence_types=("structured_record", "text_span"),
            stop_condition="served by cutoff-safe exact evidence slices or an explicit typed gap",
            purpose=draft.why_needed or focus.reason,
            expected_section=section,
            focus_ids=focus_ids,
            priority=90,
            query_hints=hints,
            completion_criteria=(
                "every required facet is served by cutoff-safe exact evidence slices or a typed gap"
            ),
            need_facets=facets,
            completion_spec=completion_spec,
            planner_artifact_ref=artifact_ref,
            planned_draft_id=draft.draft_id,
            validated_need_set_hash=validated_hash,
        )

    @staticmethod
    def _query_value(value: Any) -> str:
        if isinstance(value, str):
            return value[:240]
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, (list, tuple)):
            return " ".join(
                TaskPlanConditionedNeedGenerator._query_value(item) for item in value[:8]
            )[:240]
        if isinstance(value, dict):
            return " ".join(
                f"{key} {TaskPlanConditionedNeedGenerator._query_value(item)}"
                for key, item in list(value.items())[:8]
            )[:240]
        return ""

    @classmethod
    def _predicates_by_keywords(
        cls,
        predicates: tuple[str, ...],
        keywords: tuple[str, ...],
        *,
        limit: int = 16,
    ) -> tuple[str, ...]:
        """Keep only predicates whose name matches the semantic keyword set.

        R1 treats ``predicates`` as an OR-set (SQL IN) filter, so restricting
        the set never over-constrains exact retrieval to an empty result; it
        only excludes unrelated records from the exact path.
        """

        return tuple(
            dict.fromkeys(
                predicate
                for predicate in predicates
                if any(keyword in predicate.casefold() for keyword in keywords)
            )
        )[:limit]

    @classmethod
    def _knowledge_state_context(
        cls,
        entity_id: StableId,
        states: tuple[StateRecord, ...],
        *,
        limit: int = 900,
    ) -> str:
        """Keep public relationship/disclosure anchors early in knowledge queries."""

        keywords = (
            "attitude",
            "contract",
            "disclos",
            "know",
            "marriage",
            "promise",
            "relation",
            "secret",
            "student",
            "teacher",
            "信任",
            "关系",
            "婚",
            "态度",
            "承诺",
            "知",
            "秘密",
        )
        selected: list[str] = []
        for state in states:
            if state.subject_id != entity_id:
                continue
            value = cls._query_value(state.value)
            searchable = f"{state.predicate} {value}".casefold()
            if not any(keyword in searchable for keyword in keywords):
                continue
            selected.extend(item for item in (state.predicate, value) if item)
        return " ".join(dict.fromkeys(selected))[:limit]

    @classmethod
    def _completion_contract(
        cls,
        *,
        need_id: StableId,
        need_type: str,
        section: WriterContextSection,
        task: BenchmarkTaskContract,
        focus: TaskFocus,
        allow_plan: bool,
        mandatory: bool,
        facet_kinds_override: tuple[NeedFacetKind, ...] | None = None,
        predicates_by_facet: Mapping[NeedFacetKind, tuple[str, ...]] | None = None,
    ) -> tuple[tuple[NeedFacet, ...], NeedCompletionSpec]:
        facet_kinds: tuple[NeedFacetKind, ...]
        if facet_kinds_override is not None:
            facet_kinds = tuple(dict.fromkeys(facet_kinds_override))
        elif need_type == "capability_boundary":
            facet_kinds = (
                NeedFacetKind.CAPABILITY_STATUS,
                NeedFacetKind.LIMITATION,
            )
        elif need_type == "relationship_emotion":
            facet_kinds = (NeedFacetKind.RELATION_STATE,)
        elif need_type == "knowledge_boundary":
            facet_kinds = (NeedFacetKind.KNOWLEDGE_BOUNDARY,)
        elif need_type == "long_range_callback":
            facet_kinds = (NeedFacetKind.SETUP, NeedFacetKind.UNRESOLVED_STATUS)
        elif need_type == "unresolved_obligation":
            facet_kinds = (NeedFacetKind.COMMITMENT, NeedFacetKind.UNRESOLVED_STATUS)
        elif need_type == "plan_obligation":
            facet_kinds = (NeedFacetKind.PLAN_NODE,)
        elif "history" in need_type or need_type == "causal_history":
            facet_kinds = (NeedFacetKind.CAUSAL_HISTORY,)
        else:
            facet_kinds = (NeedFacetKind.CURRENT_STATE,)

        derivation_refs = tuple(
            dict.fromkeys(
                (
                    task.task_id,
                    focus.focus_id,
                    *((focus.canonical_id,) if allow_plan else ()),
                )
            )
        )
        digest = hashlib.sha256(need_id.root.encode("utf-8")).hexdigest()[:16]
        facets = tuple(
            NeedFacet(
                need_facet_id=StableId(f"need-facet.{digest}.{kind.value}"),
                need_id=need_id,
                facet_kind=kind,
                expected_claim_scope=cls._claim_scope(kind, section),
                derivation_refs=derivation_refs,
                producer="TaskPlanConditionedNeedGenerator",
                producer_version=cls.version,
                information_scope="author_plan" if allow_plan else "cutoff_safe",
            )
            for kind in facet_kinds
        )
        evidence_requirements = {
            facet.need_facet_id.root: (
                FacetEvidenceRequirement.PLAN_PROVENANCE
                if facet.facet_kind is NeedFacetKind.PLAN_NODE
                else FacetEvidenceRequirement.CUTOFF_CURRENT_SOURCE
                if facet.expected_claim_scope is ExpectedClaimScope.CURRENT
                else FacetEvidenceRequirement.DISTINCT_HISTORICAL_SOURCE
                if facet.expected_claim_scope is ExpectedClaimScope.HISTORICAL
                else FacetEvidenceRequirement.TRACEABLE_CUTOFF_SOURCE
            )
            for facet in facets
        }
        facet_ids = tuple(item.need_facet_id for item in facets)
        return facets, NeedCompletionSpec(
            need_id=need_id,
            required_need_facet_ids=facet_ids,
            irreducible_need_facet_ids=(facet_ids if mandatory or len(facet_ids) > 1 else ()),
            evidence_requirement_by_facet=evidence_requirements,
            min_distinct_evidence_sources=1,
            min_distinct_chapters=(2 if NeedFacetKind.SETUP in facet_kinds else 1),
            # Evidence-first default: completion is served by cutoff-safe exact
            # evidence slices or an explicit typed gap.  No active "one current
            # claim" gate; CURRENT_CLAIM remains an explicit opt-in for legacy
            # claim-driven paths only.
            require_current_claim=False,
            require_causal_history=NeedFacetKind.CAUSAL_HISTORY in facet_kinds,
            uncertainty_policy=NeedUncertaintyPolicy.ALLOW_GAP_ONLY,
            gap_policy=NeedGapPolicy.FAIL_MANDATORY,
            producer="TaskPlanConditionedNeedGenerator",
            producer_version=cls.version,
            # Facet-level predicate binding: each facet declares only the
            # predicates that can serve it (2026-08-14 review second P1).
            predicates_by_facet={
                facet.need_facet_id.root: tuple(
                    dict.fromkeys((predicates_by_facet or {}).get(facet.facet_kind, ()))
                )
                for facet in facets
            },
        )

    @staticmethod
    def _claim_scope(
        kind: NeedFacetKind,
        section: WriterContextSection,
    ) -> ExpectedClaimScope:
        if kind is NeedFacetKind.PLAN_NODE:
            return ExpectedClaimScope.PLANNED
        if kind is NeedFacetKind.KNOWLEDGE_BOUNDARY:
            return ExpectedClaimScope.KNOWLEDGE
        if kind in {
            NeedFacetKind.CAUSAL_HISTORY,
            NeedFacetKind.SETUP,
            NeedFacetKind.COMMITMENT,
        } or section in {
            WriterContextSection.CAUSAL_HISTORY,
            WriterContextSection.LONG_RANGE_CALLBACKS,
        }:
            return ExpectedClaimScope.HISTORICAL
        return ExpectedClaimScope.CURRENT

    @staticmethod
    def _plan_history_facets(value: str, *, limit: int = 6) -> tuple[str, ...]:
        """Split an author-visible coarse intent without adding case knowledge."""

        normalized = " ".join(value.split())
        strip_chars = " -\u2014:\uff1a[]\u3010\u3011()\uff08\uff09"
        parts = tuple(
            part.strip(strip_chars)
            for part in re.split(r"[/\u3001\uff0c,\uff1b;\u3002]", normalized)
            if part.strip(strip_chars)
        )
        return tuple(dict.fromkeys(parts))[:limit] or (normalized,)
