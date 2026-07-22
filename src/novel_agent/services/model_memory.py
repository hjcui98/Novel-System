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
from novel_agent.services.benchmark_importer import validate_evidence_ref
from novel_agent.services.content_addressing import world_root_content_id
from novel_agent.services.model_gateway import ModelGateway


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
