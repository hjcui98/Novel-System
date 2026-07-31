"""Task/plan-conditioned, bounded memory need generation."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Any

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import PlanRootDocument
from novel_agent.domain.ids import RunId, StableId, TaskId
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
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    WriterContextSection,
)
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


class NeedGenerationResult(DomainModel):
    task_id: StableId
    focus_set: FocusSet
    needs: tuple[Stage1MemoryNeed, ...]
    status: NeedGenerationStatus
    unexpanded_focus_ids: tuple[StableId, ...] = ()
    need_completion_spec_version: str = Field(min_length=1)
    generator_version: str = Field(min_length=1)


class TaskPlanConditionedNeedGenerator:
    """Generate needs from the bounded FocusSet, never by enumerating WorldRoot."""

    profile = "task_plan_conditioned_v1"
    version = "task_plan_conditioned_need.v19"
    completion_spec_version = "need_completion_spec.v1"

    def __init__(
        self,
        *,
        max_total_needs: int = 32,
        focus_extractor: TaskFocusExtractor | None = None,
    ) -> None:
        if max_total_needs < 1:
            raise ValueError("max_total_needs must be positive")
        self._max_total_needs = max_total_needs
        self._focus_extractor = focus_extractor or TaskFocusExtractor()

    def generate(
        self,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        plan: PlanRootDocument | None = None,
    ) -> tuple[Stage1MemoryNeed, ...]:
        return self.generate_with_lineage(task, world, plan).needs

    def generate_with_lineage(
        self,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        plan: PlanRootDocument | None = None,
    ) -> NeedGenerationResult:
        if (
            task.information_profile is BenchmarkInformationProfile.VISIBLE_AT_CUTOFF
            and plan is not None
            and (plan.nodes or plan.chapter_goals)
        ):
            # Ignore is unsafe here: callers could believe the plan was filtered.
            raise ValueError("visible_at_cutoff need generation cannot receive a future PlanRoot")
        focus_set = self._focus_extractor.extract(task, world, plan)
        if not focus_set.focuses:
            return NeedGenerationResult(
                task_id=task.task_id,
                focus_set=focus_set,
                needs=(),
                status=NeedGenerationStatus.NO_FOCUS,
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
        ) -> None:
            need_id = StableId(f"need.stage2m.{identity}"[:128])
            facets, completion_spec = self._completion_contract(
                need_id=need_id,
                need_type=need_type,
                section=section,
                task=task,
                focus=focus,
                allow_plan=allow_plan,
                mandatory=mandatory,
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
                    access_scope=("author_planning" if allow_plan else "writer_safe"),
                    allow_plan=allow_plan,
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
                    stop_condition="one current claim with a minimal legal evidence set",
                    purpose=focus.reason,
                    expected_section=section,
                    focus_ids=(focus.focus_id,),
                    priority=priority,
                    query_hints=(query,),
                    completion_criteria="claim is supported by cutoff-valid evidence",
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
                add(
                    identity=f"entity.{entity.entity_id.root}.state",
                    focus=focus,
                    need_type="current_state",
                    intent=Stage1QueryIntent.CURRENT_STATE,
                    query=query or entity.internal_label,
                    entity_ids=(entity.entity_id,),
                    section=WriterContextSection.CURRENT_WORLD_STATE,
                    mandatory=is_primary_entity,
                    priority=90,
                    pools=(CandidatePool.R1, CandidatePool.ANCHOR, CandidatePool.GROUNDED),
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
                        priority=92,
                        pools=(
                            CandidatePool.R1,
                            CandidatePool.ANCHOR,
                            CandidatePool.GROUNDED,
                        ),
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
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.capability-history",
                        focus=focus,
                        need_type="capability_history",
                        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                        query=(
                            f"{label} 历史能力边界 前置条件 失败 尝试 "
                            f"变化 来源 证据 {state_context}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.CONTINUITY_CONSTRAINTS,
                        mandatory=False,
                        priority=94,
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.goal-history",
                        focus=focus,
                        need_type="goal_history",
                        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                        query=(
                            f"{label} 目标历史 原因 动机 选择 承诺 "
                            f"{obligation_context} {state_context}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.CONTINUITY_CONSTRAINTS,
                        mandatory=False,
                        priority=93,
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.destination-history",
                        focus=focus,
                        need_type="destination_history",
                        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                        query=(
                            f"{label} 去向 目的 地点 行动路线 前往 到达 "
                            f"资格 原因 {event_context} {state_context}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.CAUSAL_HISTORY,
                        mandatory=False,
                        priority=93,
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.goal",
                        focus=focus,
                        need_type="current_goal",
                        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                        query=(
                            f"{label} 当前目标 目的 优先级 坚持 只能 "
                            f"{obligation_context} {state_context}"
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
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.eligibility",
                        focus=focus,
                        need_type="eligibility_and_destination",
                        intent=Stage1QueryIntent.RELATED_EVENT,
                        query=(
                            f"{label} 条件 资格 进入 前往 地点 目的地 "
                            f"行动因果 {event_context} {state_context}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.CAUSAL_HISTORY,
                        mandatory=False,
                        priority=93,
                        pools=(
                            CandidatePool.R1,
                            CandidatePool.ANCHOR,
                            CandidatePool.GROUNDED,
                        ),
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.learning-foundation",
                        focus=focus,
                        need_type="learning_foundation",
                        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                        query=(
                            f"{label} 历史基础 知识 技能 经验 学习方法 "
                            f"长期积累 来源 {state_context}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.LONG_RANGE_CALLBACKS,
                        mandatory=False,
                        priority=93,
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.environment-resources",
                        focus=focus,
                        need_type="environment_and_resources",
                        intent=Stage1QueryIntent.CURRENT_STATE,
                        query=(
                            f"{label} 当前环境 地点 人员 组织 资源 "
                            f"可用条件 {relation_context} {state_context}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.CURRENT_WORLD_STATE,
                        mandatory=False,
                        priority=93,
                        pools=(
                            CandidatePool.R1,
                            CandidatePool.ANCHOR,
                            CandidatePool.GROUNDED,
                        ),
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.behavior-profile",
                        focus=focus,
                        need_type="behavioral_profile",
                        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                        query=(
                            f"{label} 行为习惯 决策原则 处事方式 稳定模式 变化条件 {state_context}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.CONTINUITY_CONSTRAINTS,
                        mandatory=False,
                        priority=93,
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.environment-history",
                        focus=focus,
                        need_type="environment_history",
                        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                        query=(
                            f"{label} 环境历史 到达 居住 组织变化 "
                            f"资源来源 当前形成过程 {event_context} {state_context}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.CURRENT_WORLD_STATE,
                        mandatory=False,
                        priority=93,
                        pools=(
                            CandidatePool.R1,
                            CandidatePool.ANCHOR,
                            CandidatePool.GROUNDED,
                        ),
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
                        priority=91,
                        pools=(
                            CandidatePool.R1,
                            CandidatePool.ANCHOR,
                            CandidatePool.GROUNDED,
                        ),
                    )
                    add(
                        identity=f"entity.{entity.entity_id.root}.knowledge",
                        focus=focus,
                        need_type="knowledge_boundary",
                        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
                        query=(
                            f"{label} 知情边界 知道 不知道 公开 未公开 "
                            f"推测 不可断言 {relation_context} {target_plan_text}"
                        ),
                        entity_ids=(entity.entity_id,),
                        section=WriterContextSection.KNOWLEDGE_AND_DISCLOSURE,
                        mandatory=bool(target_plan_text),
                        priority=95 if target_plan_text else 74,
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
                        priority=96 if target_plan_text else 72,
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
                    )
            elif focus.focus_type is TaskFocusType.EVENT:
                event = event_by_id.get(focus.canonical_id)
                if event is not None:
                    add(
                        identity=f"event.{event.event_id.root}",
                        focus=focus,
                        need_type="causal_history",
                        intent=Stage1QueryIntent.RELATED_EVENT,
                        query=event.event_type,
                        entity_ids=event.participant_ids,
                        section=WriterContextSection.CAUSAL_HISTORY,
                        mandatory=False,
                        priority=60,
                        pools=(CandidatePool.ANCHOR, CandidatePool.GRAPH, CandidatePool.GROUNDED),
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
                NeedGenerationStatus.NEED_BUDGET_EXHAUSTED
                if len(ordered) > self._max_total_needs
                else NeedGenerationStatus.READY
            ),
            unexpanded_focus_ids=unexpanded,
            need_completion_spec_version=self.completion_spec_version,
            generator_version=self.version,
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
    ) -> tuple[tuple[NeedFacet, ...], NeedCompletionSpec]:
        facet_kinds: tuple[NeedFacetKind, ...]
        if need_type == "capability_boundary":
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
            require_current_claim=any(
                item.expected_claim_scope is ExpectedClaimScope.CURRENT for item in facets
            ),
            require_causal_history=NeedFacetKind.CAUSAL_HISTORY in facet_kinds,
            uncertainty_policy=NeedUncertaintyPolicy.ALLOW_GAP_ONLY,
            gap_policy=NeedGapPolicy.FAIL_MANDATORY,
            producer="TaskPlanConditionedNeedGenerator",
            producer_version=cls.version,
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
