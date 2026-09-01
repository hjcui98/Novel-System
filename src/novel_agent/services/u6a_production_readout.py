"""Production adapter for the U6-A Writer readout lifecycle.

The adapter reuses the frozen U4-S corpus, the evidence-first WCP owner and the
production Writer readout bindings.  It only reads the explicitly public
canary inputs; hidden target text, Gold and evaluator annotations never enter
this path.  C-ROLL is the one canary that enables the existing model-driven
Stage-2M Planner owner; D-SHORT and free-run remain Writer readout diagnostics
until their separate Stage-3 case adapter is admitted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import (
    AuthorPlanningContext,
    ChapterGoal,
    PlanRootDocument,
    VisibleOutlineNode,
)
from novel_agent.domain.ids import ArtifactId, ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import ModelCallLedgerEntry, ModelCallLedgerStatus
from novel_agent.domain.u6_continuous_replay import (
    U6ACanaryJob,
    U6AReadoutPhaseResult,
    U6AReadoutTask,
    U6AReadoutTrack,
    U6CheckpointBasis,
)
from novel_agent.domain.v05_readout import (
    MemoryIdentitySnapshot,
    V05HistoryAccess,
    V05ReadoutCampaignManifest,
    V05ReadoutTaskIdentity,
    V05ReadoutTrack,
)
from novel_agent.domain.world import PlanNode
from novel_agent.domain.writer_context import BenchmarkInformationProfile, FreezeReceipt
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.benchmark_importer import content_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.evidence_first_checkpoint_runner import (
    EvidenceFirstCheckpointRunner,
)
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.model_gateway import ModelGateway
from novel_agent.services.u4s_seed_readout import (
    U4SCheckpointInput,
    U4SPublicCorpus,
    as_run_request_id,
)
from novel_agent.services.writer_context_readout import (
    CONTEXT_READOUT_STAGE,
    QA_READOUT_STAGE,
    WriterContextReadoutRequest,
    bind_production_context_readout,
    bind_production_qa_readout,
    readout_model_request_id,
)
from novel_agent.services.writer_judge import WriterJudgeService

SCHEMA_VERSION = SchemaVersion("1.0.0")
U6A_FREEZE_MARKER_MEDIA_TYPE = "application/vnd.novel-agent.evaluation.u6a-freeze-marker+json"
U6A_RELEASE_MARKER_MEDIA_TYPE = "application/vnd.novel-agent.evaluation.u6a-release-marker+json"
U6A_PACKAGE_MEDIA_TYPE = "application/vnd.novel-agent.writer-context-package-v2+json"
U6A_LEDGER_MEDIA_TYPE = "application/vnd.novel-agent.evidence-ledger-v2+json"

U6AItem = U6AReadoutTask | U6ACanaryJob


class U6AProductionReadoutError(ValueError):
    """The production U6-A adapter cannot satisfy a typed lifecycle phase."""


@dataclass(frozen=True, slots=True)
class U6AProductionReadoutConfig:
    manifest: V05ReadoutCampaignManifest
    corpus: U4SPublicCorpus
    basis_artifacts: ArtifactRepository
    artifacts: ArtifactRepository
    gateway: ModelGateway
    bundle_root: Path
    project_id: ProjectId
    run_id: RunId


@dataclass(slots=True)
class _ItemState:
    task_input: U4SCheckpointInput | None = None
    freeze_ref: ArtifactRef | None = None
    package_ref: ArtifactRef | None = None
    ledger_ref: ArtifactRef | None = None
    freeze_receipt: FreezeReceipt | None = None
    response_ref: ArtifactRef | None = None
    record_ref: ArtifactRef | None = None
    raw_ref: ArtifactRef | None = None
    judge_refs: tuple[ArtifactRef, ...] = ()


class U6AProductionReadoutAdapter:
    """Bind each U6-A phase to one existing production owner."""

    def __init__(self, config: U6AProductionReadoutConfig) -> None:
        self._config = config
        self._source_tasks = {task.task_id: task for task in config.manifest.readout_manifest.tasks}
        self._states: dict[StableId, _ItemState] = {}

    async def execute_phase(
        self,
        *,
        phase: str,
        item: U6AItem,
        basis: U6CheckpointBasis,
        run_id: RunId,
    ) -> U6AReadoutPhaseResult:
        if run_id != self._config.run_id:
            raise U6AProductionReadoutError("U6-A adapter run identity differs from its freeze")
        state = self._states.setdefault(_item_id(item), _ItemState())
        if phase == "freeze":
            return self._freeze(item, basis, state)
        if phase == "release":
            return self._release(item, basis, state, run_id)
        if phase == "wcp":
            # EvidenceFirstCheckpointRunner is a synchronous owner whose
            # model Planner path uses asyncio.run().  Keep the U6 lifecycle
            # sequential, but run this blocking phase outside the executor's
            # event loop so the existing owner can await its gateway safely.
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="u6a-wcp") as executor:
                result = await loop.run_in_executor(executor, self._wcp, item, basis, state)
            return result
        if phase == "writer":
            return await self._writer(item, basis, state, run_id)
        if phase == "response_freeze":
            return self._response_freeze(item, basis, state)
        if phase == "evaluator_reveal":
            return self._evaluator_reveal(item, basis, state, run_id)
        raise U6AProductionReadoutError(f"unsupported U6-A phase: {phase}")

    def _freeze(
        self,
        item: U6AItem,
        basis: U6CheckpointBasis,
        state: _ItemState,
    ) -> U6AReadoutPhaseResult:
        payload = {
            "schema": "u6a-freeze-marker.v1",
            "item_id": _item_id(item).root,
            "track": _item_track(item).value,
            "checkpoint_chapter": basis.checkpoint_chapter,
            "basis_id": basis.basis_id.root,
            "commit_id": None if basis.commit_id is None else basis.commit_id.root,
            "snapshot_id": None if basis.snapshot_id is None else basis.snapshot_id.root,
        }
        ref = self._put_json(payload, U6A_FREEZE_MARKER_MEDIA_TYPE)
        state.freeze_ref = ref
        return self._phase(
            "freeze",
            (ref,),
            (ref,),
            _memory_identity(basis),
        )

    def _release(
        self,
        item: U6AItem,
        basis: U6CheckpointBasis,
        state: _ItemState,
        run_id: RunId,
    ) -> U6AReadoutPhaseResult:
        if state.freeze_ref is None:
            raise U6AProductionReadoutError("release occurred before freeze")
        task_input = self._build_task_input(item, basis, run_id)
        state.task_input = task_input
        payload = {
            "schema": "u6a-release-marker.v1",
            "item_id": _item_id(item).root,
            "task_id": task_input.task.task_id.root,
            "checkpoint_chapter": task_input.identity.checkpoint_chapter,
            "target_range": [
                task_input.task.target_chapter_start,
                task_input.task.target_chapter_end,
            ],
            "information_profile": task_input.task.information_profile.value,
            "basis_commit": task_input.basis_commit.root,
            "snapshot_id": task_input.snapshot_id.root,
        }
        ref = self._put_json(payload, U6A_RELEASE_MARKER_MEDIA_TYPE)
        return self._phase(
            "release",
            (ref,),
            (ref,),
            _memory_identity(basis),
        )

    def _wcp(
        self,
        item: U6AItem,
        basis: U6CheckpointBasis,
        state: _ItemState,
    ) -> U6AReadoutPhaseResult:
        task_input = state.task_input
        if task_input is None:
            raise U6AProductionReadoutError("WCP occurred before release")
        before = {
            entry.request_id
            for entry in self._config.gateway.call_ledger.list_for_run(self._config.run_id)
        }
        canary_planner = isinstance(item, U6ACanaryJob) and item.track is U6AReadoutTrack.C_ROLL
        runner = EvidenceFirstCheckpointRunner(
            writer_token_budget=self._config.manifest.runtime.body_output_tokens,
            evidence_ledger_token_budget=self._config.manifest.writer.evidence_token_budget,
            planner_gateway=self._config.gateway if canary_planner else None,
            planner_model_decisions=canary_planner,
            controller_model_decisions=False,
            semantic_judge_model_decisions=False,
            artifact_writer=self._write_planner_artifact if canary_planner else None,
            planner_max_output_tokens=min(8_192, self._config.manifest.runtime.body_output_tokens),
            planner_max_input_tokens=self._config.manifest.runtime.context_limit,
            thinking_enabled=self._config.manifest.runtime.thinking_enabled,
        )
        result = runner.run(
            case_id=self._config.project_id,
            task=task_input.task,
            world=task_input.world,
            text=task_input.text,
            plan=task_input.plan,
            base_commit=task_input.basis_commit,
            snapshot_id=task_input.snapshot_id,
            planning_context=task_input.planning_context,
            frozen_planner_artifact=None if canary_planner else task_input.planner_artifact,
            frozen_needs=(task_input.need,),
            backend_bundle=task_input.backend_bundle,
            fingerprint=content_id(
                {
                    "campaign": self._config.manifest.campaign_id.root,
                    "item": _item_id(item).root,
                    "basis": task_input.basis_commit.root,
                }
            ),
            run_id=as_run_request_id(task_input.identity.task_id),
            model_run_id=self._config.run_id,
        )
        if result.future_leakage_count:
            raise U6AProductionReadoutError("WCP returned future leakage")
        package_ref = self._put_json(
            result.assembly.package.model_dump(mode="json"), U6A_PACKAGE_MEDIA_TYPE
        )
        ledger_ref = self._put_json(
            result.assembly.evidence_ledger.model_dump(mode="json"), U6A_LEDGER_MEDIA_TYPE
        )
        if ledger_ref != result.assembly.package.evidence_ledger_ref:
            raise U6AProductionReadoutError("WCP package does not bind its persisted ledger")
        receipt = self._freeze_receipt(task_input, package_ref, ledger_ref)
        freeze_ref = self._put_json(
            receipt.model_dump(mode="json"),
            "application/vnd.novel-agent.evaluation.v05-readout-freeze-receipt+json",
        )
        state.package_ref = package_ref
        state.ledger_ref = ledger_ref
        state.freeze_receipt = receipt
        state.freeze_ref = freeze_ref
        entries = self._new_entries(before)
        return self._phase(
            "wcp",
            (package_ref, ledger_ref, freeze_ref),
            (freeze_ref,),
            _memory_identity(basis),
            input_tokens=_input_tokens(entries),
            output_tokens=_output_tokens(entries),
            latency_ms=_latency_ms(entries),
            gap_count=len(result.assembly.package.gaps),
            evidence_distance=sum(len(selection.selections) for selection in result.selections),
            stage_loss_count=len(result.assembly.package.unclosed_mandatory_need_facets),
        )

    async def _writer(
        self,
        item: U6AItem,
        basis: U6CheckpointBasis,
        state: _ItemState,
        run_id: RunId,
    ) -> U6AReadoutPhaseResult:
        task_input = state.task_input
        package = state.package_ref
        receipt = state.freeze_receipt
        if task_input is None or package is None or receipt is None:
            raise U6AProductionReadoutError("Writer occurred before WCP freeze")
        identity = task_input.identity
        request = WriterContextReadoutRequest(
            task_contract=task_input.task,
            writer_context=self._load_package(package),
            freeze_receipt=receipt,
            case_id=StableId(f"case.u6a.{_item_id(item).root}"[:128]),
            question_id=identity.question_id,
            question_text=task_input.question_text,
            track=(
                V05ReadoutTrack.QA.value
                if identity.track is V05ReadoutTrack.QA
                else V05ReadoutTrack.CONTEXT.value
            ),
        )
        before = {
            entry.request_id
            for entry in self._config.gateway.call_ledger.list_for_run(self._config.run_id)
        }
        probe: Any
        writer: Any
        if identity.track is V05ReadoutTrack.QA:
            probe, writer = bind_production_qa_readout(
                self._config.gateway,
                self._config.artifacts,
                run_id=run_id,
                max_output_tokens=self._config.manifest.writer.output_token_budget,
                timeout_seconds=60.0,
                enable_thinking=self._config.manifest.runtime.thinking_enabled,
            )
        else:
            probe, writer = bind_production_context_readout(
                self._config.gateway,
                self._config.artifacts,
                run_id=run_id,
                max_output_tokens=self._config.manifest.writer.output_token_budget,
                timeout_seconds=60.0,
                enable_thinking=self._config.manifest.runtime.thinking_enabled,
            )
        await probe.arun(request)
        response_ref = writer.last_response_ref
        record_ref = writer.last_record_ref
        if response_ref is None or record_ref is None:
            raise U6AProductionReadoutError("Writer returned no durable readout artifacts")
        readout_stage = (
            QA_READOUT_STAGE if identity.track is V05ReadoutTrack.QA else CONTEXT_READOUT_STAGE
        )
        request_id = readout_model_request_id(
            run_id=run_id,
            task_id=task_input.task.task_id.root,
            stage=readout_stage,
        )
        entry = self._config.gateway.call_ledger.load(request_id)
        if entry is None or entry.status is not ModelCallLedgerStatus.COMPLETED:
            raise U6AProductionReadoutError("Writer response has no completed ledger entry")
        raw_ref = entry.raw_artifact_ref
        if raw_ref is None:
            raise U6AProductionReadoutError("Writer response has no raw response artifact")
        state.response_ref = response_ref
        state.record_ref = record_ref
        state.raw_ref = raw_ref
        entries = self._new_entries(before)
        return self._phase(
            "writer",
            (response_ref, record_ref, raw_ref),
            (response_ref, record_ref),
            _memory_identity(basis),
            input_tokens=_input_tokens(entries),
            output_tokens=_output_tokens(entries),
            latency_ms=_latency_ms(entries),
        )

    def _response_freeze(
        self,
        item: U6AItem,
        basis: U6CheckpointBasis,
        state: _ItemState,
    ) -> U6AReadoutPhaseResult:
        if state.response_ref is None or state.record_ref is None:
            raise U6AProductionReadoutError("response freeze occurred before Writer response")
        return self._phase(
            "response_freeze",
            (state.response_ref, state.record_ref),
            (state.response_ref, state.record_ref),
            _memory_identity(basis),
        )

    def _evaluator_reveal(
        self,
        item: U6AItem,
        basis: U6CheckpointBasis,
        state: _ItemState,
        run_id: RunId,
    ) -> U6AReadoutPhaseResult:
        if state.response_ref is None or state.freeze_receipt is None:
            raise U6AProductionReadoutError("evaluator reveal occurred before response freeze")
        judge_task_id = (
            state.task_input.task.task_id if state.task_input is not None else _item_id(item)
        )
        pair = WriterJudgeService(self._config.artifacts).pending_pair(
            run_id=run_id,
            task_id=judge_task_id,
            freeze_receipt_id=state.freeze_receipt.receipt_id,
            response_ref=state.response_ref,
        )
        refs = (
            WriterJudgeService(self._config.artifacts).persist(pair.answer_judge),
            WriterJudgeService(self._config.artifacts).persist(pair.evidence_support_judge),
        )
        state.judge_refs = refs
        return self._phase(
            "evaluator_reveal",
            refs,
            refs,
            _memory_identity(basis),
        )

    def _build_task_input(
        self,
        item: U6AItem,
        basis: U6CheckpointBasis,
        run_id: RunId,
    ) -> U4SCheckpointInput:
        if isinstance(item, U6AReadoutTask):
            identity = self._source_tasks.get(item.source_task_id)
            if identity is None:
                raise U6AProductionReadoutError(
                    f"public source task is missing: {item.source_task_id.root}"
                )
            return self._config.corpus.checkpoint_input_for_frozen_basis(
                identity,
                run_id=run_id,
                basis=basis,
                basis_artifacts=self._config.basis_artifacts,
            )

        target_start, target_end, objective = self._canary_public_case(item)
        base_identity = V05ReadoutTaskIdentity(
            task_id=StableId(f"u6a.canary.task.{item.job_id.root}"[:128]),
            track=V05ReadoutTrack.CONTEXT,
            checkpoint_id=StableId(f"u6a.canary.checkpoint.{item.job_id.root}"[:128]),
            checkpoint_chapter=item.checkpoint_chapter,
            history_access=V05HistoryAccess.HISTORY_ONLY,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            target_chapter_start=target_start,
            target_chapter_end=target_end,
        )
        released = self._config.corpus.checkpoint_input_for_frozen_basis(
            base_identity,
            run_id=run_id,
            basis=basis,
            basis_artifacts=self._config.basis_artifacts,
        )
        if item.track is U6AReadoutTrack.C_ROLL:
            task, plan, context = self._canary_planner_projection(
                item,
                target_start,
                target_end,
                objective,
            )
        else:
            task = build_safe_task_contract(
                case_id=base_identity.task_id,
                checkpoint_chapter=item.checkpoint_chapter,
                target_range=(target_start, target_end),
                information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            ).model_copy(update={"task_id": base_identity.task_id})
            plan = released.plan
            context = released.planning_context
        need = released.need.model_copy(
            update={
                "task_id": TaskId(base_identity.task_id.root),
                "query_text": objective or released.need.query_text,
                "why_needed": objective or released.need.why_needed,
                "chapter_target": target_start,
                "horizon_target": (target_start, target_end),
            }
        )
        return released.__class__(
            identity=base_identity.model_copy(
                update={
                    "information_profile": task.information_profile,
                    "history_access": (
                        V05HistoryAccess.AUTHOR_PLAN_CONDITIONED
                        if task.information_profile
                        is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
                        else V05HistoryAccess.HISTORY_ONLY
                    ),
                }
            ),
            task=task,
            planning_context=context,
            plan=plan,
            text=released.text,
            world=released.world,
            basis_commit=released.basis_commit,
            snapshot_id=released.snapshot_id,
            question_text=None,
            need=need,
            planner_artifact=released.planner_artifact,
            backend_bundle=released.backend_bundle,
            memory_identity=released.memory_identity,
        )

    def _canary_planner_projection(
        self,
        item: U6ACanaryJob,
        target_start: int,
        target_end: int,
        objective: str,
    ) -> tuple[Any, PlanRootDocument, AuthorPlanningContext]:
        payload = self._canary_payload(item)
        stage = payload.get("stage") if isinstance(payload.get("stage"), dict) else {}
        if not isinstance(stage, dict):
            stage = {}
        stage_id = str(stage.get("stage_id", f"U6A-{item.job_id.root}"))
        stage_objective = str(stage.get("objective", objective)).strip() or objective
        outline_id = StableId(f"u6a.plan.{item.job_id.root}.stage"[:128])
        raw_goal_items = stage.get("chapter_goals")
        goal_items: dict[str, Any] = (
            {str(key): value for key, value in raw_goal_items.items()}
            if isinstance(raw_goal_items, dict)
            else {}
        )
        goals = tuple(
            ChapterGoal(
                goal_id=StableId(f"u6a.goal.{item.job_id.root}.{chapter}"[:128]),
                chapter_index=int(chapter),
                summary=str(summary),
            )
            for chapter, summary in sorted(goal_items.items(), key=lambda pair: int(pair[0]))
            if target_start <= int(chapter) <= target_end
        )
        plan_node = PlanNode(
            plan_node_id=outline_id,
            node_type="stage",
            title=stage_id,
            summary=stage_objective,
        )
        plan = PlanRootDocument(
            root_hash=ArtifactId("sha256:" + "0" * 64),
            schema_version=SCHEMA_VERSION,
            nodes=(plan_node,),
            chapter_goals=goals,
        )
        plan = plan.model_copy(update={"root_hash": content_id(plan.model_dump(mode="json"))})
        context_payload = {
            "item": item.job_id.root,
            "objective": stage_objective,
            "target_range": (target_start, target_end),
            "goals": [goal.model_dump(mode="json") for goal in goals],
        }
        context = AuthorPlanningContext(
            profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            task_intent=stage_objective,
            target_range=(target_start, target_end),
            visible_outline_nodes=(
                VisibleOutlineNode(
                    node_id=outline_id,
                    title=stage_id,
                    summary=stage_objective,
                ),
            ),
            chapter_goals=goals,
            source_hash=content_id(context_payload),
            planner_may_read_plan=True,
        )
        task = build_safe_task_contract(
            case_id=StableId(f"u6a.canary.task.{item.job_id.root}"[:128]),
            checkpoint_chapter=item.checkpoint_chapter,
            target_range=(target_start, target_end),
            information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            task_intent=stage_objective,
            planning_context_ref=content_id(context.model_dump(mode="json")),
            planning_context_hash=context.source_hash,
        ).model_copy(update={"task_id": StableId(f"u6a.canary.task.{item.job_id.root}"[:128])})
        return task, plan, context

    def _canary_public_case(self, item: U6ACanaryJob) -> tuple[int, int, str]:
        payload = self._canary_payload(item)
        if item.track is U6AReadoutTrack.FREE_RUN:
            chapters = payload.get("chapters")
            if not isinstance(chapters, list) or not chapters:
                raise U6AProductionReadoutError("free-run public case has no chapter horizon")
            start, end = int(chapters[0]), int(chapters[-1])
            return start, end, str(payload.get("continuity", ""))
        horizon = payload.get("horizon")
        if (
            not isinstance(horizon, list) or len(horizon) != 2
        ) and item.track is U6AReadoutTrack.D_SHORT:
            projection = self._read_public_dshort_projection(item)
            horizon = projection.get("horizon")
        if not isinstance(horizon, list) or len(horizon) != 2:
            raise U6AProductionReadoutError("canary public case has no two-point horizon")
        objective = str(payload.get("window_objective", "")).strip()
        if not objective and item.track is U6AReadoutTrack.D_SHORT:
            projection = self._read_public_dshort_projection(item)
            stage = projection.get("stage")
            objective = str(stage.get("objective", "")).strip() if isinstance(stage, dict) else ""
        return int(horizon[0]), int(horizon[1]), objective

    def _canary_payload(self, item: U6ACanaryJob) -> dict[str, Any]:
        if item.track is U6AReadoutTrack.C_ROLL:
            path = (
                self._config.bundle_root
                / "private"
                / "plan_tasks"
                / "CROLL"
                / f"ZTJ-CROLL-{item.job_id.root.removeprefix('croll-')}.json"
            )
        elif item.track is U6AReadoutTrack.D_SHORT:
            path = (
                self._config.bundle_root
                / "private"
                / "write_tasks"
                / "DSHORT"
                / f"ZTJ-DSHORT-{item.job_id.root.removeprefix('dshort-')}"
                / "public"
                / "case.json"
            )
        elif item.track is U6AReadoutTrack.FREE_RUN:
            path = (
                self._config.bundle_root
                / "private"
                / "write_tasks"
                / "FREERUN"
                / f"ZTJ-FREERUN-{item.job_id.root.removeprefix('freerun-')}.json"
            )
        else:
            raise U6AProductionReadoutError(f"unsupported canary track: {item.track.value}")
        if not path.is_file():
            raise U6AProductionReadoutError(f"public canary input is missing: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise U6AProductionReadoutError(f"public canary input is not an object: {path}")
        return payload

    def _read_public_dshort_projection(self, item: U6ACanaryJob) -> dict[str, Any]:
        path = (
            self._config.bundle_root
            / "private"
            / "write_tasks"
            / "DSHORT"
            / f"ZTJ-DSHORT-{item.job_id.root.removeprefix('dshort-')}"
            / "public"
            / "accepted_plan_projection.json"
        )
        if not path.is_file():
            raise U6AProductionReadoutError(f"public D-SHORT plan is missing: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise U6AProductionReadoutError("public D-SHORT plan is not an object")
        return payload

    def _load_package(self, ref: ArtifactRef) -> Any:
        from novel_agent.domain.writer_context import WriterContextPackageV2

        return WriterContextPackageV2.model_validate_json(
            self._config.artifacts.read_verified(ref), strict=True
        )

    def _freeze_receipt(
        self,
        task_input: U4SCheckpointInput,
        package_ref: ArtifactRef,
        ledger_ref: ArtifactRef,
    ) -> FreezeReceipt:
        identity = task_input.identity
        return FreezeReceipt(
            receipt_id=StableId(f"freeze.u6a.{identity.task_id.root}"[:128]),
            public_input_hash=content_id(
                {
                    "campaign_id": self._config.manifest.campaign_id.root,
                    "task_id": identity.task_id.root,
                    "checkpoint": identity.checkpoint_chapter,
                    "track": identity.track.value,
                    "basis_commit": task_input.basis_commit.root,
                }
            ),
            code_version="u6a_production_readout.v1",
            run_config_hash=content_id(
                {
                    "writer": self._config.manifest.writer.model_dump(mode="json"),
                    "runtime": self._config.manifest.runtime.model_dump(mode="json"),
                }
            ),
            arm_artifact_hashes={
                "A": package_ref.artifact_id,
                "B": ledger_ref.artifact_id,
                "C": task_input.text.root_hash,
            },
            frozen_before_reveal=True,
        )

    def _write_planner_artifact(self, payload: bytes, media_type: str) -> ArtifactRef:
        return self._config.artifacts.put(payload, media_type, SCHEMA_VERSION)

    def _new_entries(self, before: set[StableId]) -> tuple[ModelCallLedgerEntry, ...]:
        return tuple(
            entry
            for entry in self._config.gateway.call_ledger.list_for_run(self._config.run_id)
            if entry.request_id not in before
        )

    def _put_json(self, payload: Mapping[str, Any], media_type: str) -> ArtifactRef:
        return self._config.artifacts.put(canonical_json_bytes(payload), media_type, SCHEMA_VERSION)

    @staticmethod
    def _phase(
        phase: str,
        artifact_refs: tuple[ArtifactRef, ...],
        evaluation_refs: tuple[ArtifactRef, ...],
        memory_identity: MemoryIdentitySnapshot,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        gap_count: int = 0,
        evidence_distance: int = 0,
        stage_loss_count: int = 0,
    ) -> U6AReadoutPhaseResult:
        return U6AReadoutPhaseResult(
            phase=cast(Any, phase),
            artifact_refs=artifact_refs,
            evaluation_refs=evaluation_refs,
            memory_identity=memory_identity,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            gap_count=gap_count,
            evidence_distance=evidence_distance,
            stage_loss_count=stage_loss_count,
        )


def _item_id(item: U6AItem) -> StableId:
    return item.task_id if isinstance(item, U6AReadoutTask) else item.job_id


def _item_track(item: U6AItem) -> U6AReadoutTrack:
    if isinstance(item, U6AReadoutTask):
        return U6AReadoutTrack(item.track)
    return item.track


def _memory_identity(basis: U6CheckpointBasis) -> MemoryIdentitySnapshot:
    if (
        basis.commit_id is None
        or basis.text_root_ref is None
        or basis.world_root_ref is None
        or basis.plan_root_ref is None
        or basis.profile_root_ref is None
    ):
        raise U6AProductionReadoutError("U6-A basis is missing a frozen memory root")
    return MemoryIdentitySnapshot(
        commit_id=basis.commit_id,
        text_root=basis.text_root_ref.artifact_id,
        world_root=basis.world_root_ref.artifact_id,
        plan_root=basis.plan_root_ref.artifact_id,
        profile_root=basis.profile_root_ref.artifact_id,
    )


def _input_tokens(entries: tuple[ModelCallLedgerEntry, ...]) -> int:
    return sum(
        entry.call_record.usage.input_tokens for entry in entries if entry.call_record is not None
    )


def _output_tokens(entries: tuple[ModelCallLedgerEntry, ...]) -> int:
    return sum(
        entry.call_record.usage.output_tokens for entry in entries if entry.call_record is not None
    )


def _latency_ms(entries: tuple[ModelCallLedgerEntry, ...]) -> int:
    return sum(entry.call_record.latency_ms for entry in entries if entry.call_record is not None)


__all__ = [
    "U6AProductionReadoutAdapter",
    "U6AProductionReadoutConfig",
    "U6AProductionReadoutError",
]
