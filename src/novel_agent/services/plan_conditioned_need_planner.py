"""LLM Need Planner: backward-chained semantic need drafts from plan + world."""

from __future__ import annotations

import asyncio
import json
import re
from typing import ClassVar, cast

from novel_agent.domain.benchmark import AuthorPlanningContext
from novel_agent.domain.ids import ArtifactId, RunId, StableId, TaskId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.planning_memory import (
    PLANNER_OUTPUT_SCHEMA_VERSION,
    EntityMention,
    PlannedNeedDraft,
    PlannerArtifactMetadata,
    PlannerEntitySummary,
    PlannerEventSummary,
    PlannerFallbackStatus,
    PlannerInvocationArtifact,
    PlannerInvocationAttempt,
    PlannerInvocationAttemptStatus,
    PlannerObligationSummary,
    PlannerRelationSummary,
    PlannerRunResult,
    PlannerStateSummary,
    PlannerTargetStateCoverage,
    PlannerWorldSummary,
    RelationMention,
)
from novel_agent.domain.world import StateRecord
from novel_agent.domain.writer_context import BenchmarkTaskContract
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.benchmark_importer import content_id
from novel_agent.services.model_gateway import ModelGateway


class PlannerWorldSummaryBuilder:
    """Deterministic, bounded world projection for the LLM Planner."""

    version = "planner_world_summary_builder.v2"

    _MAX_ENTITIES = 48
    _MAX_STATES = 64
    _MAX_RECENT_EVENTS = 12
    _MAX_RELATIONS = 48
    _MAX_OBLIGATIONS = 32

    @classmethod
    def build(
        cls,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        context: AuthorPlanningContext,
    ) -> PlannerWorldSummary:
        label_by_id = {entity.entity_id: entity.internal_label for entity in world.entities}
        plan_text = " ".join(
            (
                context.task_intent,
                *(
                    text
                    for node in context.visible_outline_nodes
                    for text in (node.title, node.summary)
                ),
                *(goal.summary for goal in context.chapter_goals),
            )
        ).casefold()

        def relevance(*texts: str) -> int:
            return sum(
                bool(text.strip()) and text.strip().casefold() in plan_text for text in texts
            )

        ranked_entities = tuple(
            entity
            for _, entity in sorted(
                enumerate(world.entities),
                key=lambda indexed: (
                    -relevance(indexed[1].internal_label, *indexed[1].aliases),
                    indexed[0],
                    indexed[1].entity_id.root,
                ),
            )
        )
        ranked_states = tuple(
            state
            for _, state in sorted(
                enumerate(world.states),
                key=lambda indexed: (
                    -relevance(
                        label_by_id.get(indexed[1].subject_id, indexed[1].subject_id.root),
                        indexed[1].predicate,
                        json.dumps(indexed[1].value, ensure_ascii=False, default=str),
                    ),
                    indexed[0],
                    indexed[1].state_id.root,
                ),
            )
        )
        target_entities, target_coverages = cls._target_entities_and_state_coverage(
            world,
            label_by_id,
            plan_text,
            ranked_states,
        )
        selected_states, truncated_state_count = cls._target_aware_state_selection(
            target_entities,
            ranked_states,
        )
        legal_events = tuple(
            event
            for event in world.events
            if (chapter := cls._event_chapter(event)) < 0 or chapter <= task.checkpoint_chapter
        )
        recent_events = tuple(
            event
            for _, event in sorted(
                enumerate(legal_events),
                key=lambda indexed: (
                    cls._event_chapter(indexed[1]),
                    relevance(
                        indexed[1].event_type,
                        *(label_by_id.get(item, item.root) for item in indexed[1].participant_ids),
                    ),
                    indexed[0],
                ),
                reverse=True,
            )[: cls._MAX_RECENT_EVENTS]
        )
        eligible_obligations = tuple(
            obligation
            for obligation in world.obligations
            if obligation.status.value in {"open", "progressed"}
        )
        ranked_obligations = tuple(
            obligation
            for _, obligation in sorted(
                enumerate(eligible_obligations),
                key=lambda indexed: (
                    -relevance(
                        indexed[1].description,
                        *(label_by_id.get(item, item.root) for item in indexed[1].owner_ids),
                    ),
                    indexed[0],
                    indexed[1].obligation_id.root,
                ),
            )
        )
        ranked_relations = tuple(
            relation
            for _, relation in sorted(
                enumerate(world.relations),
                key=lambda indexed: (
                    -relevance(
                        label_by_id.get(indexed[1].subject_id, indexed[1].subject_id.root),
                        indexed[1].predicate,
                        label_by_id.get(indexed[1].object_id, indexed[1].object_id.root),
                    ),
                    indexed[0],
                    indexed[1].relation_id.root,
                ),
            )
        )
        plan_intent = " ".join(
            dict.fromkeys(
                text
                for node in context.visible_outline_nodes
                for text in (node.title, node.summary)
                if text.strip()
            )
        )
        goals = " ".join(
            dict.fromkeys(goal.summary for goal in context.chapter_goals if goal.summary.strip())
        )
        plan_intent = " ".join(dict.fromkeys((plan_intent, goals))).strip()
        return PlannerWorldSummary(
            checkpoint_chapter=task.checkpoint_chapter,
            target_range=(task.target_chapter_start, task.target_chapter_end),
            task_intent=context.task_intent,
            plan_intent=plan_intent[:2000],
            entities=tuple(
                PlannerEntitySummary(
                    label=entity.internal_label,
                    aliases=entity.aliases[:4],
                    entity_type=entity.entity_type,
                )
                for entity in ranked_entities[: cls._MAX_ENTITIES]
            ),
            states=tuple(
                PlannerStateSummary(
                    subject_label=label_by_id.get(state.subject_id, state.subject_id.root),
                    predicate=state.predicate,
                    value=json.dumps(
                        state.value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )[:240],
                )
                for state in selected_states
            ),
            open_obligations=tuple(
                PlannerObligationSummary(
                    description=obligation.description[:240],
                    owner_labels=tuple(
                        dict.fromkeys(
                            label_by_id.get(owner_id, owner_id.root)
                            for owner_id in obligation.owner_ids
                        )
                    ),
                    status=obligation.status.value,
                )
                for obligation in ranked_obligations[: cls._MAX_OBLIGATIONS]
            ),
            recent_events=tuple(
                PlannerEventSummary(
                    event_type=event.event_type,
                    participant_labels=tuple(
                        dict.fromkeys(
                            label_by_id.get(participant_id, participant_id.root)
                            for participant_id in event.participant_ids
                        )
                    ),
                    chapter=(lambda chapter: chapter if chapter >= 1 else None)(
                        cls._event_chapter(event)
                    ),
                )
                for event in recent_events
            ),
            key_relations=tuple(
                PlannerRelationSummary(
                    subject_label=label_by_id.get(relation.subject_id, relation.subject_id.root),
                    predicate=relation.predicate,
                    object_label=label_by_id.get(relation.object_id, relation.object_id.root),
                )
                for relation in ranked_relations[: cls._MAX_RELATIONS]
            ),
            target_state_coverage=target_coverages,
            entity_count=len(world.entities),
            state_count=len(world.states),
            event_count=len(world.events),
            relation_count=len(world.relations),
            obligation_count=len(world.obligations),
            truncated_entity_count=max(0, len(world.entities) - cls._MAX_ENTITIES),
            truncated_state_count=truncated_state_count,
            truncated_event_count=max(0, len(legal_events) - cls._MAX_RECENT_EVENTS),
            truncated_relation_count=max(0, len(world.relations) - cls._MAX_RELATIONS),
            truncated_obligation_count=max(0, len(eligible_obligations) - cls._MAX_OBLIGATIONS),
        )

    @classmethod
    def _target_entities_and_state_coverage(
        cls,
        world: WorldRootDocument,
        label_by_id: dict[StableId, str],
        plan_text: str,
        ranked_states: tuple[StateRecord, ...],
    ) -> tuple[tuple[StableId, ...], tuple[PlannerTargetStateCoverage, ...]]:
        """Extract the visible target entities and their per-target state counts.

        Only labels that uniquely resolve to one runtime entity are treated as
        targets; ambiguous or absent labels cannot anchor a target guarantee.
        Each target entity is ordered by the number of its label hits in the
        public task/Plan text so the most directly relevant entities are
        protected first within the fixed state budget.
        """
        from novel_agent.services.need_draft_grounder import NeedDraftGrounder

        resolvable = NeedDraftGrounder._resolvable_label_map(world)
        hits: dict[StableId, int] = {}
        entity_order: dict[StableId, int] = {}
        for index, entity in enumerate(world.entities):
            entity_order[entity.entity_id] = index
        for normalized, entity_id in resolvable.items():
            count = plan_text.count(normalized)
            if count > 0:
                hits[entity_id] = hits.get(entity_id, 0) + count
        targets = tuple(
            entity_id
            for entity_id, _count in sorted(
                hits.items(),
                key=lambda item: (-item[1], entity_order[item[0]], item[0].root),
            )
        )
        per_target_states: dict[StableId, list[StateRecord]] = {}
        for state in ranked_states:
            per_target_states.setdefault(state.subject_id, []).append(state)
        per_target_budget = max(1, cls._MAX_STATES // len(targets)) if targets else 0
        coverages = tuple(
            PlannerTargetStateCoverage(
                label=label_by_id.get(entity_id, entity_id.root),
                available=len(per_target_states.get(entity_id, ())),
                selected=min(per_target_budget, len(per_target_states.get(entity_id, ()))),
                truncated=max(
                    0,
                    len(per_target_states.get(entity_id, ())) - per_target_budget,
                ),
            )
            for entity_id in targets
        )
        return targets, coverages

    @classmethod
    def _target_aware_state_selection(
        cls,
        target_entities: tuple[StableId, ...],
        ranked_states: tuple[StateRecord, ...],
    ) -> tuple[tuple[StateRecord, ...], int]:
        """Select states so every target entity is represented, then fill up.

        Within the same total ``_MAX_STATES`` budget, each target entity
        receives up to ``_MAX_STATES // len(targets)`` of its own states (in
        stable relevance order); the remaining slots are filled with the
        globally ranked states that were not already selected.  States that
        are not selected are counted as truncated.
        """
        selected_ids: set[StableId] = set()
        selected: list[StateRecord] = []
        per_target_budget = (
            max(1, cls._MAX_STATES // len(target_entities)) if target_entities else 0
        )
        if target_entities:
            for entity_id in target_entities:
                if (
                    len(selected) >= cls._MAX_STATES
                ):  # pragma: no branch - targets are disjoint and capped per-target
                    break  # pragma: no cover - unreachable under the per-target cap
                taken = 0
                for state in ranked_states:
                    if state.subject_id != entity_id:
                        continue
                    if (
                        state.state_id in selected_ids
                    ):  # pragma: no branch - per-target state lists are disjoint
                        continue  # pragma: no cover - disjoint per-target state lists
                    selected.append(state)
                    selected_ids.add(state.state_id)
                    taken += 1
                    if taken >= per_target_budget or len(selected) >= cls._MAX_STATES:
                        break
        for state in ranked_states:
            if len(selected) >= cls._MAX_STATES:
                break
            if state.state_id in selected_ids:
                continue
            selected.append(state)
            selected_ids.add(state.state_id)
        return tuple(selected), max(0, len(ranked_states) - len(selected))

    @staticmethod
    def _event_chapter(event: object) -> int:
        narrative_order = getattr(event, "narrative_order", None)
        chapter = getattr(narrative_order, "chapter_index", None)
        if isinstance(chapter, int):
            return chapter
        chapters: list[int] = []
        for evidence in getattr(event, "evidence_refs", ()):
            match = re.search(r"(?:^|[._:-])(\d+)$", evidence.chapter_id.root)
            if match is not None:
                chapters.append(int(match.group(1)))
        return max(chapters, default=-1)


class PlanConditionedNeedPlanner:
    """LLM Planner: plan + task intent + world summary -> semantic drafts.

    The LLM never emits graph ids; ``PlannedNeedDraft`` carries only
    natural-language mentions.  Every invocation records full run lineage in
    ``PlannerArtifactMetadata`` so a changed model or prompt yields a new run
    identity.  When the gateway is absent or the model output cannot be
    parsed, the run falls back (``PLANNER_FALLBACK``) to the deterministic
    template generator.
    """

    version = "plan_conditioned_need_planner.v1"
    prompt_version = "plan_conditioned_need_planner_prompt.v1"
    output_schema_version = PLANNER_OUTPUT_SCHEMA_VERSION
    max_drafts_default = 24

    _FACET_VALUES: ClassVar[tuple[str, ...]] = (
        "CURRENT_STATE",
        "CAUSAL_HISTORY",
        "RELATION_STATE",
        "KNOWLEDGE_BOUNDARY",
        "CAPABILITY_STATUS",
        "LIMITATION",
        "SETUP",
        "UNRESOLVED_STATUS",
        "COMMITMENT",
        "PLAN_NODE",
    )
    _SCOPE_VALUES: ClassVar[tuple[str, ...]] = (
        "current",
        "historical",
        "knowledge",
        "planned",
    )

    def __init__(
        self,
        *,
        gateway: ModelGateway | None = None,
        model_role: ModelRole = ModelRole.BATCH_TEST,
        temperature: float = 0.0,
        max_drafts: int = 24,
        max_retries: int = 1,
        max_output_tokens: int = 8192,
    ) -> None:
        if max_drafts < 1:
            raise ValueError("planner max drafts must be positive")
        if max_retries < 0 or max_retries > 2:
            raise ValueError("planner retries must be between zero and two")
        if not 0 <= temperature <= 2:
            raise ValueError("planner temperature must be between zero and two")
        if max_output_tokens < 512 or max_output_tokens > 32768:
            raise ValueError("planner max output tokens must be between 512 and 32768")
        self._gateway = gateway
        self._model_role = model_role
        self._temperature = temperature
        self._max_drafts = max_drafts
        self._max_retries = max_retries
        self._max_output_tokens = max_output_tokens

    def plan(
        self,
        *,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        planning_context: AuthorPlanningContext,
        run_id: RunId | None = None,
        planner_model: str = "",
        planner_model_revision: str = "",
        repair_instruction: str | None = None,
        max_retries: int | None = None,
    ) -> PlannerRunResult:
        attempt_retries = self._max_retries if max_retries is None else max_retries
        if attempt_retries < 0 or attempt_retries > 2:
            raise ValueError("planner retries override must be between zero and two")
        summary = PlannerWorldSummaryBuilder.build(task, world, planning_context)
        world_summary_hash = content_id(summary.model_dump(mode="json"))
        required_goal_chapters = tuple(
            dict.fromkeys(
                goal.chapter_index
                for goal in planning_context.chapter_goals
                if task.target_chapter_start <= goal.chapter_index <= task.target_chapter_end
            )
        )
        prompt = self._build_prompt(
            planning_context,
            summary,
            required_goal_chapters=required_goal_chapters,
            repair_instruction=repair_instruction,
        )
        prompt_hash = content_id({"prompt_version": self.prompt_version, "prompt": prompt})
        if self._gateway is None:
            return PlannerRunResult(
                drafts=(),
                fallback_status=PlannerFallbackStatus.PLANNER_FALLBACK,
                error_category="no_gateway",
                planning_context=planning_context,
                world_summary=summary,
                exact_prompt=prompt,
            )
        resolved_run_id = run_id or RunId(f"run.stage2m.{task.task_id.root}"[:128])
        task_id = TaskId(task.task_id.root)
        endpoint_policy = dict(self._gateway.endpoint_policy_identity(self._model_role))
        fallback_model = planner_model or endpoint_policy["registered_model"]
        fallback_revision = planner_model_revision or endpoint_policy["adapter_revision"]
        prompt_digest = sha256_id(prompt.encode("utf-8")).root.removeprefix("sha256:")[:24]
        last_category: str | None = None
        raw_text: str | None = None
        observed_model = fallback_model
        observed_revision = fallback_revision
        input_tokens = 0
        output_tokens = 0
        attempts: list[PlannerInvocationAttempt] = []
        for attempt_index in range(attempt_retries + 1):
            request = ModelRequest(
                request_id=StableId(f"need-planner.{prompt_digest}.attempt{attempt_index + 1}"),
                run_id=resolved_run_id,
                task_id=task_id,
                model_role=self._model_role,
                purpose=ModelCallPurpose.BATCH_TEST,
                trace_id=(
                    f"stage2m-need-planner:{task.task_id.root}:{prompt_digest}:"
                    f"attempt{attempt_index + 1}"
                ),
                prompt=prompt,
                max_output_tokens=self._max_output_tokens,
                timeout_seconds=420.0,
                enable_thinking=False,
                scheduling_stage="need_planner",
            )
            attempt_raw = ""
            attempt_input_tokens = 0
            attempt_output_tokens = 0
            usage_recorded = False
            try:
                result = asyncio.run(self._gateway.generate_text(request))
                raw_text = result.text
                attempt_raw = result.text
                observed_model = planner_model or result.call_record.model
                observed_revision = planner_model_revision or result.call_record.model_version
                attempt_input_tokens = result.call_record.usage.input_tokens
                attempt_output_tokens = result.call_record.usage.output_tokens
                input_tokens += attempt_input_tokens
                output_tokens += attempt_output_tokens
                usage_recorded = True
                drafts = self._parse_drafts(raw_text)
                if not drafts:
                    last_category = "empty_drafts"
                    attempts.append(
                        PlannerInvocationAttempt(
                            request_id=request.request_id,
                            status=PlannerInvocationAttemptStatus.EMPTY_DRAFTS,
                            raw_response=attempt_raw,
                            raw_response_hash=content_id({"raw": attempt_raw}),
                            input_tokens=attempt_input_tokens,
                            output_tokens=attempt_output_tokens,
                        )
                    )
                    continue
                attempts.append(
                    PlannerInvocationAttempt(
                        request_id=request.request_id,
                        status=PlannerInvocationAttemptStatus.SUCCEEDED,
                        raw_response=attempt_raw,
                        raw_response_hash=content_id({"raw": attempt_raw}),
                        input_tokens=attempt_input_tokens,
                        output_tokens=attempt_output_tokens,
                    )
                )
                metadata = PlannerArtifactMetadata(
                    run_id=resolved_run_id,
                    planner_model=observed_model,
                    planner_model_revision=observed_revision,
                    planner_prompt_version=self.prompt_version,
                    planner_prompt_hash=prompt_hash,
                    planner_output_schema_version=self.output_schema_version,
                    temperature=self._temperature,
                    requested_seed=None,
                    effective_seed_supported=False,
                    planning_context_hash=planning_context.source_hash,
                    world_summary_hash=world_summary_hash,
                    raw_response_hash=content_id({"raw": raw_text}),
                    validated_need_set_hash=ArtifactId("sha256:" + "0" * 64),
                    fallback_used=False,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                return PlannerRunResult(
                    drafts=drafts,
                    metadata=metadata,
                    fallback_status=PlannerFallbackStatus.PLANNER,
                    planning_context=planning_context,
                    world_summary=summary,
                    exact_prompt=prompt,
                    raw_response=raw_text,
                    attempts=tuple(attempts),
                )
            except Exception as error:
                last_category = type(error).__name__
                if not usage_recorded:
                    error_raw = getattr(error, "raw_content", None)
                    attempt_raw = error_raw if isinstance(error_raw, str) else ""
                    error_input_tokens = getattr(error, "input_tokens", None)
                    error_output_tokens = getattr(error, "output_tokens", None)
                    attempt_input_tokens = (
                        error_input_tokens
                        if isinstance(error_input_tokens, int) and error_input_tokens >= 0
                        else 0
                    )
                    attempt_output_tokens = (
                        error_output_tokens
                        if isinstance(error_output_tokens, int) and error_output_tokens >= 0
                        else 0
                    )
                    input_tokens += attempt_input_tokens
                    output_tokens += attempt_output_tokens
                    raw_text = attempt_raw
                attempts.append(
                    PlannerInvocationAttempt(
                        request_id=request.request_id,
                        status=PlannerInvocationAttemptStatus.ERROR,
                        raw_response=attempt_raw,
                        raw_response_hash=content_id({"raw": attempt_raw}),
                        input_tokens=attempt_input_tokens,
                        output_tokens=attempt_output_tokens,
                        error_category=last_category,
                    )
                )
        fallback_reason = last_category or "unknown"
        validated_hash = content_id({"fallback": fallback_reason, "drafts": []})
        metadata = PlannerArtifactMetadata(
            run_id=resolved_run_id,
            planner_model=observed_model,
            planner_model_revision=observed_revision,
            planner_prompt_version=self.prompt_version,
            planner_prompt_hash=prompt_hash,
            planner_output_schema_version=self.output_schema_version,
            temperature=self._temperature,
            requested_seed=None,
            effective_seed_supported=False,
            planning_context_hash=planning_context.source_hash,
            world_summary_hash=world_summary_hash,
            raw_response_hash=content_id({"raw": raw_text or ""}),
            validated_need_set_hash=validated_hash,
            fallback_used=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return PlannerRunResult(
            drafts=(),
            metadata=metadata,
            fallback_status=PlannerFallbackStatus.PLANNER_FALLBACK,
            error_category=fallback_reason,
            planning_context=planning_context,
            world_summary=summary,
            exact_prompt=prompt,
            raw_response=raw_text or "",
            attempts=tuple(attempts),
        )

    def replay(
        self,
        artifact: PlannerInvocationArtifact,
        *,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        planning_context: AuthorPlanningContext,
        planner_model: str,
        planner_model_revision: str,
    ) -> PlannerRunResult:
        """Replay only when every semantic/model basis hash matches exactly."""

        summary = PlannerWorldSummaryBuilder.build(task, world, planning_context)
        required_goal_chapters = tuple(
            dict.fromkeys(
                goal.chapter_index
                for goal in planning_context.chapter_goals
                if task.target_chapter_start <= goal.chapter_index <= task.target_chapter_end
            )
        )
        prompt = self._build_prompt(
            planning_context,
            summary,
            required_goal_chapters=required_goal_chapters,
        )
        metadata = artifact.metadata
        checks = {
            "planning_context": artifact.planning_context == planning_context,
            "world_summary": artifact.world_summary == summary,
            "prompt": artifact.exact_prompt == prompt,
            "validated_set": (
                metadata is not None
                and metadata.validated_need_set_hash == artifact.validated_need_set_hash
            ),
            "prompt_version": (
                metadata is not None and metadata.planner_prompt_version == self.prompt_version
            ),
            "schema_version": (
                metadata is not None
                and metadata.planner_output_schema_version == self.output_schema_version
            ),
            "model": metadata is not None and metadata.planner_model == planner_model,
            "model_revision": (
                metadata is not None and metadata.planner_model_revision == planner_model_revision
            ),
            "temperature": metadata is not None and metadata.temperature == self._temperature,
        }
        mismatches = tuple(name for name, matched in checks.items() if not matched)
        if mismatches:
            raise ValueError("Planner artifact replay basis mismatch: " + ",".join(mismatches))
        return PlannerRunResult(
            drafts=artifact.parsed_drafts,
            metadata=metadata,
            fallback_status=artifact.fallback_status,
            error_category=artifact.fallback_reason,
            planning_context=planning_context,
            world_summary=summary,
            exact_prompt=prompt,
            raw_response=artifact.raw_response,
            attempts=artifact.attempts,
        )

    def _build_prompt(
        self,
        context: AuthorPlanningContext,
        summary: PlannerWorldSummary,
        *,
        required_goal_chapters: tuple[int, ...] = (),
        repair_instruction: str | None = None,
    ) -> str:
        outline = "\n".join(f"- {node.summary}" for node in context.visible_outline_nodes)
        goals = "\n".join(
            f"- 第{goal.chapter_index}章: {goal.summary}" for goal in context.chapter_goals
        )
        entities = "\n".join(
            (
                f"- {entity.label}"
                f"{('(别名: ' + '、'.join(entity.aliases) + ')') if entity.aliases else ''}"
                f"{(' [' + entity.entity_type + ']') if entity.entity_type else ''}"
            )
            for entity in summary.entities
        )
        states = "\n".join(
            f"- {state.subject_label} {state.predicate}: {state.value}" for state in summary.states
        )
        obligations = "\n".join(
            (
                f"- {obligation.description}"
                + (
                    f"(归属: {'、'.join(obligation.owner_labels)})"
                    if obligation.owner_labels
                    else ""
                )
            )
            for obligation in summary.open_obligations
        )
        events = "\n".join(
            (
                f"- 第{event.chapter}章 {event.event_type}"
                + (
                    f"(参与者: {'、'.join(event.participant_labels)})"
                    if event.participant_labels
                    else ""
                )
            )
            for event in summary.recent_events
        )
        relations = "\n".join(
            f"- {relation.subject_label} {relation.predicate} {relation.object_label}"
            for relation in summary.key_relations
        )
        return (
            "你是长篇小说的创作记忆规划器。写作助手将在不读取未来正文的前提下, "
            "为作者的目标章节输出必须从历史中恢复的记忆问题。\n"
            "任务: 从目标章节计划反向推导(backward chaining), 列出写作这些章节前"
            "必须从历史记忆中重装的具体问题。\n"
            "约束:\n"
            "- 只能规划历史记忆问题; 不得把目标计划或未来正文当作已发生事实。\n"
            "- 严禁照抄章节目标。每个 semantic_question 必须是一个具体的, "
            "可以由截止点之前历史回答的问题, 通常询问某个实体的历史状态, 关系, "
            "情绪, 能力, 知情边界, 承诺或伏笔来源。\n"
            "- 每个问题必须引用世界摘要中至少一个实体标签(entity_mentions)或关系"
            "(relation_mentions); 纯计划义务类问题除外。\n"
            "- 如果某个章节目标需要的历史事实在世界摘要中完全不存在, 跳过该目标, "
            "不要生成无锚点的问题。\n"
            "- 实体与关系只能用世界摘要中给出的自然语言标签提及; 绝不输出任何 ID。\n"
            "- 每个显式 entity_mentions 标签和 relation_mentions 端点必须原样复制"
            "世界摘要中的一个规范实体标签或唯一别名; 不得使用复合、描述性或带注解的"
            "标签(例如地点描述或带括号别名), 否则该 mention 会被判定为无效。\n"
            "- 每个问题必须说明它支撑的目标章节(trigger_plan_chapters, 取自目标章节计划)。\n"
            "- suggested_facets 只能从这些值中选取: " + "、".join(self._FACET_VALUES) + "。\n"
            "- required_claim_scopes 只能从这些值中选取: " + "、".join(self._SCOPE_VALUES) + "。\n"
            "- query_hints 可选: 1-3 条用于检索的自然语言改写, 不得含 ID。\n"
            f"- 最多输出 {self._max_drafts} 条。\n"
            "- 输出必须简洁: semantic_question 不超过 60 字, why_needed 不超过 40 字, "
            "query_hints 每条不超过 30 字; 禁止解释性前缀或后缀。\n"
            "- 必须覆盖全部目标章节: 你输出的所有 draft 的 trigger_plan_chapters 并集"
            "必须覆盖以下每一个目标章节编号; 缺少任意一章都是失败。\n"
            f"  目标章节编号: {('、'.join(str(c) for c in required_goal_chapters)) or '(无)'}\n"
            '输出必须是单个 JSON 对象: {"drafts": [{"draft_id": "...", '
            '"semantic_question": "...", "entity_mentions": '
            '[{"label": "...", "role_in_need": "..."}], "relation_mentions": '
            '[{"subject_label": "...", "relation_label": "...", "object_label": "..."}], '
            '"trigger_plan_chapters": [N], "trigger_plan_goal": "...", '
            '"why_needed": "...", "required_claim_scopes": ["..."], '
            '"suggested_facets": ["..."], "historical_time_scope": "main", '
            '"query_hints": ["..."]}], '
            '"meta": {"rationale": "..."}}。不要输出任何其他文本。\n'
            f"目标章节范围: 第{summary.target_range[0]}-{summary.target_range[1]}章;"
            f"截止点: 第{summary.checkpoint_chapter}章。\n"
            "任务意图: \n"
            f"{context.task_intent}\n"
            "可见大纲: \n"
            f"{outline or '(无)'}\n"
            "目标章节计划: \n"
            f"{goals or '(无)'}\n"
            "世界摘要(仅截止点前历史;条目已截断): \n"
            f"显式实体({len(summary.entities)}/{summary.entity_count}):\n"
            f"{entities or '(无)'}\n"
            f"当前状态面({len(summary.states)}/{summary.state_count}):\n"
            f"{states or '(无)'}\n"
            f"未决义务:\n{obligations or '(无)'}\n"
            f"近期事件(最近 {len(summary.recent_events)} 条):\n{events or '(无)'}\n"
            f"关键关系:\n{relations or '(无)'}\n"
            + (
                "\n【修复要求】(一次有界修复):\n"
                "返回完整替代 drafts 批次, 不是增量补丁。保留仍然合格且必要的原条目, "
                "并在同一个 drafts 数组中修正以下问题; 返回结果本身必须继续覆盖全部目标章节。\n"
                f"{repair_instruction}\n"
                if repair_instruction
                else ""
            )
        )

    @classmethod
    def _parse_drafts(cls, raw: str) -> tuple[PlannedNeedDraft, ...]:
        payload = cls._extract_json_payload(raw)
        raw_drafts = payload.get("drafts")
        if not isinstance(raw_drafts, list):
            raise ValueError("planner output requires a drafts list")
        drafts: list[PlannedNeedDraft] = []
        for raw_draft in raw_drafts:
            if not isinstance(raw_draft, dict):
                raise ValueError("planner draft must be an object")
            draft_id = str(raw_draft.get("draft_id", "")).strip()
            question = str(raw_draft.get("semantic_question", "")).strip()
            if not draft_id or not question:
                continue
            chapters = tuple(
                int(chapter)
                for chapter in raw_draft.get("trigger_plan_chapters", ())
                if isinstance(chapter, int) and not isinstance(chapter, bool)
            )
            drafts.append(
                PlannedNeedDraft(
                    draft_id=cls.sanitize_draft_id(draft_id),
                    semantic_question=question,
                    entity_mentions=tuple(
                        EntityMention(
                            label=str(mention.get("label", "")).strip(),
                            role_in_need=str(mention.get("role_in_need", "")).strip(),
                        )
                        for mention in raw_draft.get("entity_mentions", ())
                        if isinstance(mention, dict) and str(mention.get("label", "")).strip()
                    ),
                    relation_mentions=tuple(
                        RelationMention(
                            subject_label=str(mention.get("subject_label", "")).strip(),
                            relation_label=str(mention.get("relation_label", "")).strip(),
                            object_label=str(mention.get("object_label", "")).strip(),
                        )
                        for mention in raw_draft.get("relation_mentions", ())
                        if isinstance(mention, dict)
                        and str(mention.get("subject_label", "")).strip()
                        and str(mention.get("relation_label", "")).strip()
                        and str(mention.get("object_label", "")).strip()
                    ),
                    trigger_plan_chapters=tuple(chapters),
                    trigger_plan_goal=str(raw_draft.get("trigger_plan_goal", "")).strip(),
                    why_needed=str(raw_draft.get("why_needed", "")).strip(),
                    required_claim_scopes=tuple(
                        str(scope).strip()
                        for scope in raw_draft.get("required_claim_scopes", ())
                        if str(scope).strip()
                    ),
                    suggested_facets=tuple(
                        str(facet).strip().upper()
                        for facet in raw_draft.get("suggested_facets", ())
                        if str(facet).strip()
                    ),
                    historical_time_scope=str(
                        raw_draft.get("historical_time_scope", "main")
                    ).strip(),
                    query_hints=tuple(
                        str(hint).strip()
                        for hint in raw_draft.get("query_hints", ())
                        if str(hint).strip()
                    ),
                )
            )
        return tuple(drafts)

    @classmethod
    def sanitize_draft_id(cls, draft_id: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_.-]", "_", draft_id)
        return sanitized[:96] or "draft"

    @staticmethod
    def _extract_json_payload(raw: str) -> dict[str, object]:
        text = raw.strip()
        start = text.find("{")
        if start < 0:
            raise ValueError("planner output contains no JSON object")
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    payload = json.loads(candidate)
                    return cast(dict[str, object], payload)
        raise ValueError("planner output JSON object is unterminated")


__all__ = ["PlanConditionedNeedPlanner", "PlannerWorldSummaryBuilder"]
