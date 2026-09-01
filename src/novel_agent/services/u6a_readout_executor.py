"""Execute the U6-A checkpoint lifecycle over existing readout owners.

This service owns ordering, information-boundary checks, evaluation discard and
the durable report.  The injected adapter owns the existing WCP, Writer and
evaluator implementations; this module deliberately does not duplicate them.
"""

from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from novel_agent.domain.artifacts import (
    EVALUATION_NAMESPACE_DISCARD_MEDIA_TYPE,
    ArtifactRef,
)
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId
from novel_agent.domain.u6_continuous_replay import (
    U6A_READOUT_LIFECYCLE,
    U6A_READOUT_PHASES,
    U6A_READOUT_PLAN_MEDIA_TYPE,
    U6A_READOUT_REPORT_MEDIA_TYPE,
    U6ACanaryJob,
    U6AReadoutCheckpointMetric,
    U6AReadoutItemReceipt,
    U6AReadoutPhaseResult,
    U6AReadoutPlan,
    U6AReadoutRunReport,
    U6AReadoutTask,
    U6AReadoutTrack,
    U6CheckpointBasis,
    U6CheckpointBasisManifest,
    U6ContinuousReplayReport,
)
from novel_agent.domain.v05_readout import MemoryIdentitySnapshot
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.evaluation_namespace import discard_evaluation_namespace

SCHEMA_VERSION = SchemaVersion("1.0.0")
U6AReadoutItem = U6AReadoutTask | U6ACanaryJob
U6AReadoutPhase = Literal[
    "freeze",
    "release",
    "wcp",
    "writer",
    "response_freeze",
    "evaluator_reveal",
]


class U6AReadoutExecutionError(ValueError):
    """The U6-A lifecycle cannot safely proceed to the next checkpoint."""

    def __init__(self, message: str, *, phase: str, item_id: StableId) -> None:
        super().__init__(message)
        self.phase = phase
        self.item_id = item_id


class U6AReadoutAdapter(Protocol):
    """Thin seam for the already-bound WCP/Writer/evaluator owners."""

    def execute_phase(
        self,
        *,
        phase: U6AReadoutPhase,
        item: U6AReadoutItem,
        basis: U6CheckpointBasis,
        run_id: RunId,
    ) -> U6AReadoutPhaseResult | Awaitable[U6AReadoutPhaseResult]:
        """Run one phase without advancing another checkpoint."""


@dataclass(frozen=True, slots=True)
class U6AReadoutExecutionArtifacts:
    report: U6AReadoutRunReport
    report_ref: ArtifactRef


class U6AReadoutExecutor:
    """Run all planned items in checkpoint order with one discard per checkpoint."""

    def __init__(
        self,
        *,
        plan: U6AReadoutPlan,
        plan_ref: ArtifactRef,
        basis_manifest: U6CheckpointBasisManifest,
        basis_report: U6ContinuousReplayReport,
        adapter: U6AReadoutAdapter,
        artifacts: ArtifactRepository,
        run_id: RunId,
    ) -> None:
        self._plan = plan
        self._plan_ref = plan_ref
        self._basis_manifest = basis_manifest
        self._basis_report = basis_report
        self._adapter = adapter
        self._artifacts = artifacts
        self._run_id = run_id

    async def run(self) -> U6AReadoutExecutionArtifacts:
        self._validate_inputs()
        items = self._items()
        basis_by_id = {node.basis_id.root: node for node in self._basis_manifest.basis_nodes}
        grouped: dict[int, list[U6AReadoutItem]] = defaultdict(list)
        for item in items:
            grouped[item.checkpoint_chapter].append(item)

        completed: list[U6AReadoutItemReceipt] = []
        checkpoint_metrics: list[U6AReadoutCheckpointMetric] = []
        discard_count = 0
        failure: U6AReadoutExecutionError | None = None
        failure_detail: str | None = None
        for checkpoint in sorted(grouped):
            checkpoint_items = tuple(grouped[checkpoint])
            basis = basis_by_id[checkpoint_items[0].basis_id.root]
            phase_results: dict[str, list[U6AReadoutPhaseResult]] = defaultdict(list)
            try:
                for phase in cast(tuple[U6AReadoutPhase, ...], U6A_READOUT_PHASES):
                    for item in checkpoint_items:
                        result = await self._execute_phase(phase, item, basis)
                        phase_results[item_id(item).root].append(result)

                evaluation_refs = _unique_refs(
                    result.evaluation_refs
                    for results in phase_results.values()
                    for result in results
                )
                if not evaluation_refs:
                    raise U6AReadoutExecutionError(
                        "checkpoint has no evaluation artifacts to discard",
                        phase="discard",
                        item_id=item_id(checkpoint_items[0]),
                    )
                identity = _memory_identity(basis)
                discard = discard_evaluation_namespace(
                    self._artifacts,
                    run_id=self._run_id,
                    discarded_refs=evaluation_refs,
                    memory_before=identity,
                    memory_after=identity,
                    discard_identity=StableId(
                        f"evaluation-discard.{self._run_id.root}.c{checkpoint}"[:128]
                    ),
                )
                discard_payload = canonical_json_bytes(discard.model_dump(mode="json"))
                discard_ref = self._artifacts.put(
                    discard_payload,
                    EVALUATION_NAMESPACE_DISCARD_MEDIA_TYPE,
                    SCHEMA_VERSION,
                )
                discard_count += 1
                for item in checkpoint_items:
                    item_results = tuple(phase_results[item_id(item).root])
                    completed.append(
                        U6AReadoutItemReceipt(
                            item_id=item_id(item),
                            track=item_track(item),
                            checkpoint_chapter=item.checkpoint_chapter,
                            basis_id=item.basis_id,
                            run_id=self._run_id,
                            lifecycle=U6A_READOUT_LIFECYCLE,
                            completed_phases=U6A_READOUT_PHASES,
                            phase_results=item_results,
                            discarded_refs=evaluation_refs,
                            discard_receipt_ref=discard_ref,
                            memory_identity_before=identity,
                            memory_identity_after=identity,
                            control_replay_identity=self._control_identity(checkpoint),
                            future_leakage_count=sum(
                                result.future_leakage_count for result in item_results
                            ),
                            input_tokens=sum(result.input_tokens for result in item_results),
                            output_tokens=sum(result.output_tokens for result in item_results),
                            latency_ms=sum(result.latency_ms for result in item_results),
                            gap_count=sum(result.gap_count for result in item_results),
                            evidence_distance=sum(
                                result.evidence_distance for result in item_results
                            ),
                            stage_loss_count=sum(
                                result.stage_loss_count for result in item_results
                            ),
                            status="EXECUTED",
                        )
                    )
                checkpoint_metrics.append(
                    self._checkpoint_metric(checkpoint, checkpoint_items, phase_results)
                )
            except U6AReadoutExecutionError as error:
                failure = error
                failure_detail = str(error)
                completed.extend(
                    self._review_receipts(
                        checkpoint_items,
                        phase_results,
                        basis,
                    )
                )
                break
            except Exception as error:
                failure = U6AReadoutExecutionError(
                    f"{type(error).__name__}: {error}",
                    phase="adapter",
                    item_id=item_id(checkpoint_items[0]),
                )
                failure_detail = str(failure)
                completed.extend(
                    self._review_receipts(
                        checkpoint_items,
                        phase_results,
                        basis,
                    )
                )
                break

        status: Literal["COMPLETED", "REVIEW_REQUIRED"] = (
            "COMPLETED" if failure is None else "REVIEW_REQUIRED"
        )
        report = U6AReadoutRunReport(
            campaign_id=self._plan.campaign_id,
            run_id=self._run_id,
            basis_manifest_ref=self._basis_report.basis_manifest_ref,
            plan_ref=self._plan_ref,
            control_replay_identity=self._basis_report.control_replay_identity,
            task_count=len(self._plan.tasks),
            canary_job_count=len(self._plan.canary_jobs),
            expected_item_count=len(items),
            completed_item_count=sum(item.status == "EXECUTED" for item in completed),
            expected_checkpoint_count=len(grouped),
            completed_checkpoint_count=len(
                {item.checkpoint_chapter for item in completed if item.status == "EXECUTED"}
            ),
            evaluation_discard_count=discard_count,
            future_leakage_count=sum(item.future_leakage_count for item in completed),
            items=tuple(completed),
            checkpoint_metrics=tuple(checkpoint_metrics),
            status=status,
            first_failure_phase=None if failure is None else failure.phase,
            first_failure_item_id=None if failure is None else failure.item_id,
            first_failure_type=None if failure is None else type(failure).__name__,
            first_failure_detail=failure_detail,
        )
        report_ref = self._artifacts.put(
            canonical_json_bytes(report.model_dump(mode="json", by_alias=True)),
            U6A_READOUT_REPORT_MEDIA_TYPE,
            SCHEMA_VERSION,
        )
        return U6AReadoutExecutionArtifacts(report=report, report_ref=report_ref)

    def _validate_inputs(self) -> None:
        if self._plan.status != "READY":
            raise U6AReadoutExecutionError(
                "U6-A executor requires a READY readout plan",
                phase="preflight",
                item_id=StableId("u6a-plan"),
            )
        if self._basis_manifest.status.value != "FROZEN":
            raise U6AReadoutExecutionError(
                "U6-A executor requires a frozen basis manifest",
                phase="preflight",
                item_id=StableId("u6a-basis"),
            )
        if self._basis_report.status != "BASIS_FROZEN":
            raise U6AReadoutExecutionError(
                "U6-A executor cannot reuse a completed or failed basis report",
                phase="preflight",
                item_id=StableId("u6a-basis-report"),
            )
        if self._basis_report.run_id != self._run_id:
            raise U6AReadoutExecutionError(
                "readout run id does not match the frozen basis run",
                phase="preflight",
                item_id=StableId("u6a-run"),
            )
        if self._basis_report.basis_manifest_ref != self._plan.basis_manifest_ref:
            raise U6AReadoutExecutionError(
                "readout plan and basis report do not share the basis identity",
                phase="preflight",
                item_id=StableId("u6a-basis-binding"),
            )
        if self._plan_ref.media_type != U6A_READOUT_PLAN_MEDIA_TYPE:
            raise U6AReadoutExecutionError(
                "readout plan ref has the wrong media type",
                phase="preflight",
                item_id=StableId("u6a-plan-ref"),
            )
        basis_ids = [node.basis_id for node in self._basis_manifest.basis_nodes]
        if len(basis_ids) != len(set(basis_ids)):
            raise U6AReadoutExecutionError(
                "basis manifest repeats a basis identity",
                phase="preflight",
                item_id=StableId("u6a-basis-duplicates"),
            )
        lineage_by_chapter = {
            lineage.checkpoint_chapter: lineage for lineage in self._basis_report.lineage
        }
        if set(lineage_by_chapter) != {
            node.checkpoint_chapter for node in self._basis_manifest.basis_nodes
        }:
            raise U6AReadoutExecutionError(
                "basis lineage does not cover the frozen basis manifest",
                phase="preflight",
                item_id=StableId("u6a-lineage"),
            )
        for lineage in self._basis_report.lineage:
            if lineage.evaluation_namespace != "PENDING_READOUT":
                raise U6AReadoutExecutionError(
                    "basis lineage is already closed or has an invalid namespace",
                    phase="preflight",
                    item_id=StableId(f"u6a-lineage-{lineage.checkpoint_chapter}"),
                )
        items = self._items()
        ids = tuple(item_id(item) for item in items)
        if len(ids) != len(set(ids)):
            raise U6AReadoutExecutionError(
                "U6-A tasks and canary jobs must have unique item identities",
                phase="preflight",
                item_id=StableId("u6a-item-duplicates"),
            )
        basis_by_id = {node.basis_id: node for node in self._basis_manifest.basis_nodes}
        for item in items:
            basis = basis_by_id.get(item.basis_id)
            if basis is None or basis.checkpoint_chapter != item.checkpoint_chapter:
                raise U6AReadoutExecutionError(
                    "readout item is not attached to its frozen basis chapter",
                    phase="preflight",
                    item_id=item_id(item),
                )

    def _items(self) -> tuple[U6AReadoutItem, ...]:
        return (*self._plan.tasks, *self._plan.canary_jobs)

    async def _execute_phase(
        self,
        phase: U6AReadoutPhase,
        item: U6AReadoutItem,
        basis: U6CheckpointBasis,
    ) -> U6AReadoutPhaseResult:
        try:
            raw = self._adapter.execute_phase(
                phase=phase,
                item=item,
                basis=basis,
                run_id=self._run_id,
            )
            result = await raw if inspect.isawaitable(raw) else raw
        except Exception as error:
            raise U6AReadoutExecutionError(
                f"U6-A {phase} adapter failed: {type(error).__name__}: {error}",
                phase=phase,
                item_id=item_id(item),
            ) from error
        if not isinstance(result, U6AReadoutPhaseResult) or result.phase != phase:
            raise U6AReadoutExecutionError(
                "U6-A adapter returned a phase result for a different phase",
                phase=phase,
                item_id=item_id(item),
            )
        if result.future_leakage_count:
            raise U6AReadoutExecutionError(
                "U6-A readout reported future leakage",
                phase=phase,
                item_id=item_id(item),
            )
        expected_identity = _memory_identity(basis)
        if result.memory_identity is not None and result.memory_identity != expected_identity:
            raise U6AReadoutExecutionError(
                "U6-A readout changed the frozen Memory identity",
                phase=phase,
                item_id=item_id(item),
            )
        return result

    def _review_receipts(
        self,
        items: Sequence[U6AReadoutItem],
        phase_results: Mapping[str, Sequence[U6AReadoutPhaseResult]],
        basis: U6CheckpointBasis,
    ) -> tuple[U6AReadoutItemReceipt, ...]:
        identity = _memory_identity(basis)
        return tuple(
            U6AReadoutItemReceipt(
                item_id=item_id(item),
                track=item_track(item),
                checkpoint_chapter=item.checkpoint_chapter,
                basis_id=item.basis_id,
                run_id=self._run_id,
                lifecycle=U6A_READOUT_LIFECYCLE,
                completed_phases=tuple(
                    result.phase for result in phase_results.get(item_id(item).root, ())
                ),
                phase_results=tuple(phase_results.get(item_id(item).root, ())),
                control_replay_identity=self._control_identity(item.checkpoint_chapter),
                memory_identity_before=identity if phase_results.get(item_id(item).root) else None,
                future_leakage_count=sum(
                    result.future_leakage_count
                    for result in phase_results.get(item_id(item).root, ())
                ),
                input_tokens=sum(
                    result.input_tokens for result in phase_results.get(item_id(item).root, ())
                ),
                output_tokens=sum(
                    result.output_tokens for result in phase_results.get(item_id(item).root, ())
                ),
                latency_ms=sum(
                    result.latency_ms for result in phase_results.get(item_id(item).root, ())
                ),
                gap_count=sum(
                    result.gap_count for result in phase_results.get(item_id(item).root, ())
                ),
                evidence_distance=sum(
                    result.evidence_distance for result in phase_results.get(item_id(item).root, ())
                ),
                stage_loss_count=sum(
                    result.stage_loss_count for result in phase_results.get(item_id(item).root, ())
                ),
                status="REVIEW_REQUIRED",
            )
            for item in items
        )

    @staticmethod
    def _checkpoint_metric(
        checkpoint: int,
        items: Sequence[U6AReadoutItem],
        phase_results: Mapping[str, Sequence[U6AReadoutPhaseResult]],
    ) -> U6AReadoutCheckpointMetric:
        results = tuple(result for item in items for result in phase_results[item_id(item).root])
        return U6AReadoutCheckpointMetric(
            checkpoint_chapter=checkpoint,
            item_count=len(items),
            input_tokens=sum(result.input_tokens for result in results),
            output_tokens=sum(result.output_tokens for result in results),
            latency_ms=sum(result.latency_ms for result in results),
            gap_count=sum(result.gap_count for result in results),
            evidence_distance=sum(result.evidence_distance for result in results),
            stage_loss_count=sum(result.stage_loss_count for result in results),
        )

    def _control_identity(self, checkpoint: int) -> ArtifactId:
        for lineage in self._basis_report.lineage:
            if lineage.checkpoint_chapter == checkpoint:
                return lineage.control_replay_identity
        raise U6AReadoutExecutionError(
            "basis control identity is missing",
            phase="preflight",
            item_id=StableId(f"u6a-control-{checkpoint}"),
        )


def item_id(item: U6AReadoutItem) -> StableId:
    return item.task_id if isinstance(item, U6AReadoutTask) else item.job_id


def item_track(item: U6AReadoutItem) -> U6AReadoutTrack:
    if isinstance(item, U6AReadoutTask):
        return U6AReadoutTrack(item.track)
    return item.track


def _unique_refs(groups: Iterable[Iterable[ArtifactRef]]) -> tuple[ArtifactRef, ...]:
    refs: dict[tuple[str, str], ArtifactRef] = {}
    for group in groups:
        for ref in group:
            refs.setdefault((ref.artifact_id.root, ref.media_type), ref)
    return tuple(refs.values())


def _memory_identity(basis: U6CheckpointBasis) -> MemoryIdentitySnapshot:
    if (
        basis.commit_id is None
        or basis.plan_root_ref is None
        or basis.text_root_ref is None
        or basis.world_root_ref is None
        or basis.profile_root_ref is None
    ):
        raise U6AReadoutExecutionError(
            "readout item basis is missing a frozen root",
            phase="freeze",
            item_id=basis.basis_id,
        )
    return MemoryIdentitySnapshot(
        commit_id=basis.commit_id,
        text_root=basis.text_root_ref.artifact_id,
        world_root=basis.world_root_ref.artifact_id,
        plan_root=basis.plan_root_ref.artifact_id,
        profile_root=basis.profile_root_ref.artifact_id,
    )


def finalize_u6a_basis_report(
    basis_report: U6ContinuousReplayReport,
    execution: U6AReadoutExecutionArtifacts,
) -> U6ContinuousReplayReport:
    """Close basis lineage only after every planned U6-A item completed."""

    if execution.report.status != "COMPLETED":
        raise U6AReadoutExecutionError(
            "cannot finalize a review-required U6-A readout",
            phase="finalize",
            item_id=StableId("u6a-report"),
        )
    if execution.report.task_count != basis_report.expected_readout_task_count:
        raise U6AReadoutExecutionError(
            "U6-A public task count does not match the frozen basis report",
            phase="finalize",
            item_id=StableId("u6a-task-count"),
        )
    lineage = tuple(
        item.model_copy(update={"evaluation_namespace": "DISCARDED"})
        for item in basis_report.lineage
    )
    return basis_report.model_copy(
        update={
            "completed_readout_task_count": execution.report.task_count,
            "evaluation_discard_count": execution.report.evaluation_discard_count,
            "lineage": lineage,
            "readout_report_ref": execution.report_ref,
            "status": "COMPLETED",
        }
    )


__all__ = [
    "U6AReadoutAdapter",
    "U6AReadoutExecutionArtifacts",
    "U6AReadoutExecutionError",
    "U6AReadoutExecutor",
    "finalize_u6a_basis_report",
]
