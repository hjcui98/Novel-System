"""Stage 4 adapter over the Stage 3-owned event-derived Context Runtime."""

from __future__ import annotations

from novel_agent.domain.agent_context import (
    AgentContextView,
    ContextCompactedPayload,
    ContextConsumer,
    ContextDelta,
    ContextDeltaAppliedPayload,
    ContextDeltaStatus,
    ContextItemKind,
    ContextLayer,
    ContextPressureDetectedPayload,
    ContextViewItem,
    SettledArtifactPayload,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.planning import (
    PlannerContextItem,
    PlannerContextPackage,
    PlannerContextProjection,
    PlannerContextSection,
)
from novel_agent.domain.runtime import RunEventType
from novel_agent.ports.planning_context import PlannerContextRuntimeFailure
from novel_agent.services.agent_context import (
    CONTEXT_VIEW_MEDIA_TYPE,
    AgentContextProjector,
    AgentContextRuntime,
    ContextCompactor,
    ContextLimitError,
    ContextWindowPolicy,
    render_context,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id

PLANNER_COMPACTION_RECEIPT_MEDIA_TYPE = (
    "application/vnd.novel-agent.planner-context-compaction-receipt+json"
)


class PlannerContextRuntimeError(PlannerContextRuntimeFailure):
    """The shared Context Runtime cannot serve this Planner stream."""


class SharedPlannerContextRuntime:
    """Translate Planner seeds/deltas while reusing the single shared Runtime owner."""

    def __init__(
        self,
        *,
        projector: AgentContextProjector,
        runtime: AgentContextRuntime,
        compactor: ContextCompactor,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion,
        policy: ContextWindowPolicy,
    ) -> None:
        self._projector = projector
        self._runtime = runtime
        self._compactor = compactor
        self._artifacts = artifacts
        self._schema_version = schema_version
        self._policy = policy

    def start(
        self,
        *,
        run_id: RunId,
        task_id: TaskId,
        seed: PlannerContextPackage,
        seed_ref: ArtifactRef,
    ) -> PlannerContextProjection:
        if (
            self._runtime.restore_latest(
                run_id,
                task_id=task_id,
                consumer=ContextConsumer.PLANNER,
            )
            is not None
        ):
            raise PlannerContextRuntimeError("Planner Context stream already exists")
        protected, memory = self._split_items(seed.items)
        view = self._projector.seed(
            run_id=run_id,
            task_id=task_id,
            consumer=ContextConsumer.PLANNER,
            base_commit=seed.base_commit,
            snapshot_id=seed.snapshot_id,
            profile_ref=seed.profile_ref,
            plan_ref=seed.reviewed_inquiry_ref,
            information_scope="planner_safe",
            seed_package_ref=seed_ref,
            protected_items=protected,
            active_memory_items=memory,
        )
        view = self._runtime.append_and_apply(
            view,
            event_type=RunEventType.CONTEXT_VIEW_STARTED,
            payload=SettledArtifactPayload(artifact_ref=seed_ref).model_dump(mode="json"),
            artifact_refs=(seed_ref,),
            label="context-view-started",
            trace_namespace="stage4",
        )
        return self._settle(view, "start")

    def append_delta(
        self,
        *,
        run_id: RunId,
        task_id: TaskId,
        delta_ref: ArtifactRef,
    ) -> PlannerContextProjection:
        view = self._view(run_id, task_id)
        package = PlannerContextPackage.model_validate_json(
            self._artifacts.read_verified(delta_ref)
        )
        if (
            package.base_commit != view.base_commit
            or package.snapshot_id != view.snapshot_id
            or package.profile_ref != view.profile_ref
            or package.reviewed_inquiry_ref != view.plan_ref
        ):
            raise PlannerContextRuntimeError("Planner Context delta basis changed")
        if view.base_commit is None or package.stage1_context_ref is None:
            raise PlannerContextRuntimeError("bootstrap Planner Context cannot append Memory")
        _protected, incoming = self._split_items(package.items)
        existing = {item.item_id: item for item in view.active_memory_items}
        for item in incoming:
            if item.item_id in existing and existing[item.item_id] != item:
                raise PlannerContextRuntimeError("Planner Context item identity changed")
        known_ids = {*existing, *view.compacted_item_ids}
        additions = tuple(item for item in incoming if item.item_id not in known_ids)
        if not additions:
            return self._projection(view)
        request_ref = package.stage1_context_ref
        delta_digest = content_id((run_id.root, delta_ref.artifact_id.root)).root[-48:]
        delta = ContextDelta(
            delta_id=StableId(f"context-delta.stage4.{delta_digest}"),
            request_ref=request_ref,
            resolution_ref=delta_ref,
            parent_view_revision=view.revision,
            base_commit=view.base_commit,
            snapshot_id=view.snapshot_id,
            profile_ref=view.profile_ref,
            plan_ref=view.plan_ref,
            added_memory_items=additions,
            evidence_refs=(delta_ref,),
            token_impact=sum(item.token_count for item in additions),
            information_scope="planner_safe",
            status=ContextDeltaStatus.RESOLVED,
        )
        view = self._runtime.append_and_apply(
            view,
            event_type=RunEventType.CONTEXT_DELTA_APPLIED,
            payload=ContextDeltaAppliedPayload(delta=delta).model_dump(mode="json"),
            artifact_refs=(request_ref, delta_ref),
            label=f"context-delta-{view.revision}",
            trace_namespace="stage4",
        )
        return self._settle(view, f"delta-{view.revision}")

    def project(self, *, run_id: RunId, task_id: TaskId) -> PlannerContextProjection:
        return self._projection(self._view(run_id, task_id))

    def _settle(self, view: AgentContextView, label: str) -> PlannerContextProjection:
        compaction_ref = None
        suspension_reason = None
        pressure = self._compactor.pressure(view, self._policy)
        try:
            if pressure.soft_exceeded:
                view = self._runtime.append_and_apply(
                    view,
                    event_type=RunEventType.CONTEXT_PRESSURE_DETECTED,
                    payload=ContextPressureDetectedPayload(pressure=pressure).model_dump(
                        mode="json"
                    ),
                    artifact_refs=(),
                    label=f"context-pressure-{view.revision}-{view.generation}",
                    trace_namespace="stage4",
                )
                _prepared, receipt = self._compactor.compact(
                    view,
                    self._policy,
                    hard=pressure.hard_exceeded,
                )
                if receipt is not None:
                    compaction_ref = self._artifacts.put(
                        canonical_json_bytes(receipt.model_dump(mode="json")),
                        PLANNER_COMPACTION_RECEIPT_MEDIA_TYPE,
                        self._schema_version,
                    )
                    view = self._runtime.append_and_apply(
                        view,
                        event_type=RunEventType.CONTEXT_COMPACTED,
                        payload=ContextCompactedPayload(receipt=receipt).model_dump(mode="json"),
                        artifact_refs=tuple(
                            ref
                            for ref in (
                                receipt.summary_artifact,
                                receipt.detail_artifact,
                                compaction_ref,
                            )
                            if ref is not None
                        ),
                        label=f"context-compacted-{receipt.published_generation}",
                        trace_namespace="stage4",
                    )
            provider = self._compactor.provider_receipt(view, self._policy)
            if not provider.provider_valid:
                suspension_reason = "PROVIDER_CONTEXT_INVALID"
            view = view.model_copy(update={"provider_validity_receipt": provider})
        except ContextLimitError:
            suspension_reason = "CONTEXT_HARD_LIMIT"
        checkpoint = self._runtime.checkpoint(
            view,
            StableId(
                f"checkpoint.stage4.{view.run_id.root}.{view.task_id.root}."
                f"{label}.{view.basis_event_position}"[:128]
            ),
        )
        return self._projection(
            view,
            view_ref=checkpoint.state_artifact_ref,
            compaction_ref=compaction_ref,
            suspension_reason=suspension_reason,
        )

    def _view(self, run_id: RunId, task_id: TaskId) -> AgentContextView:
        view = self._runtime.restore_latest(
            run_id,
            task_id=task_id,
            consumer=ContextConsumer.PLANNER,
        )
        if view is None or view.task_id != task_id or view.consumer is not ContextConsumer.PLANNER:
            raise PlannerContextRuntimeError("Planner Context stream is unavailable")
        return view

    def _projection(
        self,
        view: AgentContextView,
        *,
        view_ref: ArtifactRef | None = None,
        compaction_ref: ArtifactRef | None = None,
        suspension_reason: str | None = None,
    ) -> PlannerContextProjection:
        if view_ref is None:
            view_ref = self._artifacts.put(
                canonical_json_bytes(view.model_dump(mode="json")),
                CONTEXT_VIEW_MEDIA_TYPE,
                self._schema_version,
            )
        items = (
            *view.protected_items,
            *view.active_memory_items,
            *view.working_items,
            *view.compacted_prefix_items,
            *view.recent_settled_tail,
        )
        if suspension_reason is None and (
            view.provider_validity_receipt is not None
            and not view.provider_validity_receipt.provider_valid
        ):
            suspension_reason = "PROVIDER_CONTEXT_INVALID"
        return PlannerContextProjection(
            run_id=view.run_id,
            task_id=view.task_id,
            seed_ref=view.seed_package_ref,
            view_ref=view_ref,
            generation=view.generation,
            basis_event_position=view.basis_event_position,
            rendered_context=render_context(view),
            token_count=view.token_report.get("rendered", 0),
            exposed_context_item_ids=tuple(item.item_id for item in items),
            compaction_receipt_ref=compaction_ref,
            suspended=suspension_reason is not None,
            suspension_reason=suspension_reason,
        )

    @classmethod
    def _split_items(
        cls,
        items: tuple[PlannerContextItem, ...],
    ) -> tuple[tuple[ContextViewItem, ...], tuple[ContextViewItem, ...]]:
        converted = tuple(cls._item(item) for item in items)
        return (
            tuple(item for item in converted if item.layer is ContextLayer.PROTECTED),
            tuple(item for item in converted if item.layer is ContextLayer.MEMORY),
        )

    @staticmethod
    def _item(item: PlannerContextItem) -> ContextViewItem:
        kinds = {
            PlannerContextSection.AUTHOR_INTENT: ContextItemKind.AUTHOR_INTENT,
            PlannerContextSection.ACCEPTED_PLAN: ContextItemKind.ACCEPTED_PLAN,
            PlannerContextSection.WORKING_PROPOSAL: ContextItemKind.GOAL_PROPOSAL,
            PlannerContextSection.UNRESOLVED: ContextItemKind.UNRESOLVED_NEED,
        }
        return ContextViewItem(
            item_id=item.context_item_id,
            layer=ContextLayer.PROTECTED if item.protected else ContextLayer.MEMORY,
            kind=kinds.get(item.section, ContextItemKind.MEMORY_CLAIM),
            content=item.text,
            token_count=item.token_count,
            source_artifact_refs=tuple(
                dict.fromkeys((*item.source_artifact_refs, *item.graph_path_receipt_refs))
            ),
            atomic_group_id=item.compact_handle,
            mandatory=item.mandatory or item.protected,
            information_scope="planner_safe",
            instruction_boundary=item.protected,
            pending_effect=item.section
            in {PlannerContextSection.WORKING_PROPOSAL, PlannerContextSection.UNRESOLVED},
        )


__all__ = [
    "PLANNER_COMPACTION_RECEIPT_MEDIA_TYPE",
    "PlannerContextRuntimeError",
    "SharedPlannerContextRuntime",
]
