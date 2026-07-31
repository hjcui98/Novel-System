"""Audited model-assisted Stage 1 world construction and MemoryNeed generation."""

from __future__ import annotations

import json
from collections.abc import Iterator

from novel_agent.domain.benchmark import (
    BenchmarkCaseManifest,
    PlanRootDocument,
    TextRootDocument,
    WorldConstructionDraft,
)
from novel_agent.domain.ids import ArtifactId, CommitId
from novel_agent.domain.memory import HorizonNeedSet, Stage1MemoryNeed, WorldRootDocument
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.text import EvidenceRef
from novel_agent.domain.writer_context import BenchmarkTaskContract
from novel_agent.services.benchmark_importer import validate_evidence_ref
from novel_agent.services.content_addressing import world_root_content_id
from novel_agent.services.model_gateway import ModelGateway
from novel_agent.services.task_focus import FocusSet, TaskFocusType


class ModelMemoryContractError(ValueError):
    pass


class ModelMemoryConstructor:
    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway

    async def construct(
        self,
        history: TextRootDocument,
        case: BenchmarkCaseManifest,
        base_commit: CommitId,
        request: ModelRequest,
    ) -> tuple[WorldRootDocument, ModelCallRecord]:
        safe_request = request.model_copy(update={"prompt": self._world_prompt(history, case)})
        draft, call = await self._gateway.generate_structured(safe_request, WorldConstructionDraft)
        provisional = WorldRootDocument(
            root_hash=ArtifactId("sha256:" + "0" * 64),
            schema_version=history.schema_version,
            source_commit=base_commit,
            entities=draft.entities,
            events=draft.events,
            states=draft.states,
            relations=draft.relations,
            obligations=draft.obligations,
        )
        for evidence in self._evidence(provisional):
            validate_evidence_ref(evidence, history)
        world = provisional.model_copy(update={"root_hash": world_root_content_id(provisional)})
        return world, call

    @staticmethod
    def _world_prompt(history: TextRootDocument, case: BenchmarkCaseManifest) -> str:
        public_case = {
            "case_id": case.case_id.root,
            "history_range": case.history_range,
            "target_range": case.target_range,
            "chapter_goal_ids": [item.root for item in case.chapter_goal_ids],
        }
        return (
            "Construct WorldConstructionDraft JSON from history only. "
            "Bind every non-entity record to exact EvidenceRef; preserve assertion/rumor/dream "
            "truth classes and never infer from future text.\n"
            f"CASE={json.dumps(public_case, ensure_ascii=False, sort_keys=True)}\n"
            f"HISTORY={history.model_dump_json()}"
        )

    @staticmethod
    def _evidence(world: WorldRootDocument) -> Iterator[EvidenceRef]:
        for event in world.events:
            yield from event.evidence_refs
        for state in world.states:
            yield from state.evidence_refs
        for relation in world.relations:
            yield from relation.evidence_refs
        for obligation in world.obligations:
            yield from obligation.evidence_refs


class ModelMemoryNeedGenerator:
    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway

    async def generate(
        self,
        world: WorldRootDocument,
        plan: PlanRootDocument | None,
        case: BenchmarkCaseManifest,
        request: ModelRequest,
    ) -> tuple[HorizonNeedSet, ModelCallRecord]:
        safe_request = request.model_copy(
            update={"prompt": self._need_prompt(world, plan, case, request)}
        )
        generated, call = await self._gateway.generate_structured(safe_request, HorizonNeedSet)
        if (generated.horizon_start, generated.horizon_end) != case.target_range:
            raise ModelMemoryContractError("generated need horizon differs from benchmark target")
        needs = self._all_needs(generated)
        if len({need.need_id for need in needs}) != len(needs):
            raise ModelMemoryContractError("generated MemoryNeed ids are not unique")
        for need in needs:
            self._validate_need(need, world, case, request)
        return generated, call

    async def generate_for_public_task(
        self,
        *,
        task: BenchmarkTaskContract,
        focus_set: FocusSet,
        world: WorldRootDocument,
        plan: PlanRootDocument | None,
        request: ModelRequest,
    ) -> tuple[HorizonNeedSet, ModelCallRecord]:
        """Optional Stage 2M model arm over a bounded, public canonical view."""

        if task.task_id.root != request.task_id.root:
            raise ModelMemoryContractError("public task and model request task ids differ")
        prompt = self._bounded_need_prompt(task, focus_set, world, plan, request)
        generated, call = await self._gateway.generate_structured(
            request.model_copy(update={"prompt": prompt}),
            HorizonNeedSet,
        )
        target = (task.target_chapter_start, task.target_chapter_end)
        if (generated.horizon_start, generated.horizon_end) != target:
            raise ModelMemoryContractError("generated need horizon differs from public task")
        needs = self._all_needs(generated)
        if len(needs) > 32:
            raise ModelMemoryContractError("generated public MemoryNeed set exceeds 32 needs")
        if len({need.need_id for need in needs}) != len(needs):
            raise ModelMemoryContractError("generated MemoryNeed ids are not unique")
        allowed_focus_ids = {focus.focus_id for focus in focus_set.focuses}
        for need in needs:
            if not need.focus_ids or not set(need.focus_ids).issubset(allowed_focus_ids):
                raise ModelMemoryContractError(
                    "generated public MemoryNeed lacks a legal bounded focus"
                )
            if need.horizon_target != target:
                raise ModelMemoryContractError("generated MemoryNeed horizon is outside target")
            if (
                need.run_id != request.run_id
                or need.task_id != request.task_id
                or need.base_commit != world.source_commit
            ):
                raise ModelMemoryContractError("generated MemoryNeed audit identity mismatch")
        return generated, call

    @staticmethod
    def _need_prompt(
        world: WorldRootDocument,
        plan: PlanRootDocument | None,
        case: BenchmarkCaseManifest,
        request: ModelRequest,
    ) -> str:
        public_target = {
            "case_id": case.case_id.root,
            "target_range": case.target_range,
            "chapter_goal_ids": [item.root for item in case.chapter_goal_ids],
            "run_id": request.run_id.root,
            "task_id": request.task_id.root,
            "base_commit": world.source_commit.root,
        }
        return (
            "Generate HorizonNeedSet JSON using only canonical WorldRoot and public PlanRoot. "
            "Every need must use the supplied run/task/base identities and a bounded registered "
            "query intent. Do not use evaluator Gold or future text.\n"
            f"TARGET={json.dumps(public_target, ensure_ascii=False, sort_keys=True)}\n"
            f"WORLD={world.model_dump_json()}\n"
            f"PLAN={None if plan is None else plan.model_dump_json()}"
        )

    @staticmethod
    def _bounded_need_prompt(
        task: BenchmarkTaskContract,
        focus_set: FocusSet,
        world: WorldRootDocument,
        plan: PlanRootDocument | None,
        request: ModelRequest,
    ) -> str:
        canonical_ids = {focus.canonical_id for focus in focus_set.focuses}
        focused_entity_ids = {
            focus.canonical_id
            for focus in focus_set.focuses
            if focus.focus_type is TaskFocusType.ENTITY
        }
        bounded_world = {
            "entities": [
                item.model_dump(mode="json")
                for item in world.entities
                if item.entity_id in focused_entity_ids
            ],
            "states": [
                item.model_dump(mode="json")
                for item in world.states
                if item.state_id in canonical_ids or item.subject_id in focused_entity_ids
            ][:32],
            "relations": [
                item.model_dump(mode="json")
                for item in world.relations
                if item.relation_id in canonical_ids
            ][:16],
            "events": [
                item.model_dump(mode="json")
                for item in world.events
                if item.event_id in canonical_ids
            ][:8],
            "obligations": [
                item.model_dump(mode="json")
                for item in world.obligations
                if item.obligation_id in canonical_ids
            ][:16],
        }
        plan_view = None
        if plan is not None:
            plan_view = {
                "nodes": [
                    item.model_dump(mode="json")
                    for item in plan.nodes
                    if item.plan_node_id in canonical_ids
                ][:16],
                "chapter_goals": [
                    item.model_dump(mode="json")
                    for item in plan.chapter_goals
                    if item.goal_id in canonical_ids
                ][:8],
            }
        audit = {
            "run_id": request.run_id.root,
            "task_id": request.task_id.root,
            "base_commit": world.source_commit.root,
        }
        return (
            "Generate HorizonNeedSet JSON from the safe public task and bounded focus view. "
            "Every need must cite one or more supplied focus_ids. Never infer evaluator Gold, "
            "target prose, target_plan, preparation, or any record outside this view.\n"
            f"TASK={task.model_dump_json()}\n"
            f"FOCUSES={focus_set.model_dump_json()}\n"
            f"WORLD_VIEW={json.dumps(bounded_world, ensure_ascii=False, sort_keys=True)}\n"
            f"PLAN_VIEW={json.dumps(plan_view, ensure_ascii=False, sort_keys=True)}\n"
            f"AUDIT={json.dumps(audit, sort_keys=True)}"
        )

    @staticmethod
    def _all_needs(generated: HorizonNeedSet) -> tuple[Stage1MemoryNeed, ...]:
        return (
            *generated.shared_constraints,
            *generated.chapter_needs,
            *generated.progressive_needs,
            *generated.volume_obligations,
        )

    @staticmethod
    def _validate_need(
        need: Stage1MemoryNeed,
        world: WorldRootDocument,
        case: BenchmarkCaseManifest,
        request: ModelRequest,
    ) -> None:
        if (
            need.run_id != request.run_id
            or need.task_id != request.task_id
            or need.base_commit != world.source_commit
        ):
            raise ModelMemoryContractError("generated MemoryNeed audit identity mismatch")
        if need.chapter_target is not None and not (
            case.target_range[0] <= need.chapter_target <= case.target_range[1]
        ):
            raise ModelMemoryContractError("generated MemoryNeed chapter is outside target")
        if need.horizon_target is not None and need.horizon_target != case.target_range:
            raise ModelMemoryContractError("generated MemoryNeed horizon is outside target")
