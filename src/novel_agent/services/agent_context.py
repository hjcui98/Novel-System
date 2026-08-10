"""Single event-derived Context View projector, compactor, and recovery owner."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from novel_agent.domain.agent_context import (
    AgentContextView,
    ContextCompactedPayload,
    ContextCompactionReceipt,
    ContextConsumer,
    ContextDelta,
    ContextDeltaAppliedPayload,
    ContextItemKind,
    ContextLayer,
    ContextPressure,
    ContextViewItem,
    ProviderValidityReceipt,
    WriterWorkPlanSettledPayload,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.runtime import ResumabilityStatus, RunCheckpoint, RunEvent, RunEventType
from novel_agent.domain.writer_context import WriterContextPackage
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository

TokenCounter = Callable[[str], int]
SummaryFunction = Callable[[str], str]
CONTEXT_VIEW_MEDIA_TYPE = "application/vnd.novel-agent.agent-context-view+json"
CONTEXT_SUMMARY_MEDIA_TYPE = "application/vnd.novel-agent.context-summary+json"
CONTEXT_DETAIL_MEDIA_TYPE = "application/vnd.novel-agent.context-compaction-detail+json"


class ContextProjectionError(ValueError):
    """An event or delta cannot be applied to the current Context View."""


class ContextLimitError(RuntimeError):
    """The provider hard limit cannot be closed without violating safe-cut rules."""


def _view_hash(view: AgentContextView) -> ArtifactId:
    payload = view.model_dump(
        mode="json",
        exclude={"context_hash", "provider_validity_receipt"},
    )
    return content_id(payload)


def _with_hash(view: AgentContextView) -> AgentContextView:
    return view.model_copy(update={"context_hash": _view_hash(view)})


def render_context(view: AgentContextView) -> str:
    """Render layers deterministically without promoting runtime data to instructions."""

    ordered = (
        *view.protected_items,
        *view.active_memory_items,
        *view.working_items,
        *view.compacted_prefix_items,
        *view.recent_settled_tail,
    )
    return "\n\n".join(
        f'<CONTEXT_ITEM layer="{item.layer.value}" kind="{item.kind.value}">\n'
        f"{item.content}\n</CONTEXT_ITEM>"
        for item in ordered
    )


class AgentContextProjector:
    """Build a Writer/Planner Context View from Seed plus typed RunEvents."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._count_tokens = token_counter

    def seed_writer(
        self,
        *,
        run_id: RunId,
        task_id: TaskId,
        package: WriterContextPackage,
        seed_package_ref: ArtifactRef,
        profile_ref: ArtifactRef,
        plan_ref: ArtifactRef,
        protected_items: tuple[ContextViewItem, ...],
    ) -> AgentContextView:
        if any(item.layer is not ContextLayer.PROTECTED for item in protected_items):
            raise ContextProjectionError("seed protected items must use the protected layer")
        sections = (
            package.continuity_constraints,
            package.current_world_state,
            package.relationship_and_emotion,
            package.causal_history,
            package.knowledge_and_disclosure,
            package.plan_and_obligations,
            package.long_range_callbacks,
        )
        memory_items = tuple(
            ContextViewItem(
                item_id=item.context_item_id,
                layer=ContextLayer.MEMORY,
                kind=ContextItemKind.MEMORY_CLAIM,
                content=item.claim,
                token_count=max(1, self._count_tokens(item.claim)),
                source_artifact_refs=(
                    (item.support_receipt_ref,) if item.support_receipt_ref is not None else ()
                ),
                supersedes_item_ids=item.supersedes_item_ids,
                mandatory=item.mandatory,
                information_scope="writer_safe",
            )
            for section in sections
            for item in section
        )
        unresolved = tuple(need_id for gap in package.gaps for need_id in gap.need_ids)
        empty_hash = ArtifactId("sha256:" + "0" * 64)
        view = AgentContextView(
            run_id=run_id,
            task_id=task_id,
            consumer=ContextConsumer.WRITER,
            revision=0,
            generation=0,
            basis_event_position=0,
            base_commit=package.basis_commit_id,
            snapshot_id=package.basis_snapshot_id,
            profile_ref=profile_ref,
            plan_ref=plan_ref,
            information_scope="writer_safe",
            seed_package_ref=seed_package_ref,
            protected_items=protected_items,
            active_memory_items=memory_items,
            unresolved_need_ids=tuple(dict.fromkeys(unresolved)),
            token_report={},
            context_hash=empty_hash,
        )
        return self.refresh_tokens(_with_hash(view))

    def apply_delta(self, view: AgentContextView, delta: ContextDelta) -> AgentContextView:
        if delta.parent_view_revision != view.revision:
            raise ContextProjectionError("ContextDelta parent revision is stale")
        if (
            delta.base_commit != view.base_commit
            or delta.snapshot_id != view.snapshot_id
            or delta.profile_ref != view.profile_ref
            or delta.plan_ref != view.plan_ref
        ):
            raise ContextProjectionError("ContextDelta basis differs from the current View")
        superseded = set(delta.superseded_item_ids)
        current = tuple(item for item in view.active_memory_items if item.item_id not in superseded)
        existing = {item.item_id for item in current}
        if any(item.item_id in existing for item in delta.added_memory_items):
            raise ContextProjectionError("ContextDelta collides with an active memory item")
        unresolved = tuple(
            item for item in view.unresolved_need_ids if item not in set(delta.resolved_need_ids)
        )
        unresolved = tuple(dict.fromkeys((*unresolved, *delta.unresolved_need_ids)))
        updated = view.model_copy(
            update={
                "revision": view.revision + 1,
                "active_memory_items": (*current, *delta.added_memory_items),
                "unresolved_need_ids": unresolved,
                "provider_validity_receipt": None,
            }
        )
        return self.refresh_tokens(_with_hash(updated))

    def put_working_item(
        self,
        view: AgentContextView,
        item: ContextViewItem,
        *,
        replace_kind: ContextItemKind | None = None,
    ) -> AgentContextView:
        if item.layer is not ContextLayer.WORKING:
            raise ContextProjectionError("working Context item has the wrong layer")
        current = (
            tuple(entry for entry in view.working_items if entry.kind is not replace_kind)
            if replace_kind is not None
            else view.working_items
        )
        if item.item_id in {entry.item_id for entry in current}:
            raise ContextProjectionError("working Context item id already exists")
        updated = view.model_copy(
            update={
                "revision": view.revision + 1,
                "working_items": (*current, item),
                "provider_validity_receipt": None,
            }
        )
        return self.refresh_tokens(_with_hash(updated))

    def apply_event(self, view: AgentContextView, event: RunEvent) -> AgentContextView:
        if event.run_id != view.run_id or (
            event.task_id is not None and event.task_id != view.task_id
        ):
            raise ContextProjectionError("RunEvent belongs to another Context View")
        if event.sequence_no != view.basis_event_position + 1:
            raise ContextProjectionError("RunEvent is not the next event in sequence")
        updated = view
        if event.event_type is RunEventType.CONTEXT_DELTA_APPLIED:
            delta_payload = ContextDeltaAppliedPayload.model_validate(event.payload, strict=False)
            updated = self.apply_delta(view, delta_payload.delta)
        elif event.event_type is RunEventType.WRITER_WORK_PLAN_SETTLED:
            work_plan_payload = WriterWorkPlanSettledPayload.model_validate(
                event.payload,
                strict=False,
            )
            current = tuple(
                item for item in view.working_items if item.kind is not ContextItemKind.WORK_PLAN
            )
            updated = view.model_copy(
                update={
                    "revision": view.revision + 1,
                    "working_items": (*current, work_plan_payload.working_item),
                    "provider_validity_receipt": None,
                }
            )
        elif event.event_type is RunEventType.CONTEXT_COMPACTED:
            compaction_payload = ContextCompactedPayload.model_validate(
                event.payload,
                strict=False,
            )
            updated = self.apply_compaction(view, compaction_payload.receipt)
        updated = updated.model_copy(update={"basis_event_position": event.sequence_no})
        return self.refresh_tokens(_with_hash(updated))

    def apply_compaction(
        self,
        view: AgentContextView,
        receipt: ContextCompactionReceipt,
    ) -> AgentContextView:
        if (
            receipt.run_id != view.run_id
            or receipt.parent_view_revision != view.revision
            or receipt.parent_generation != view.generation
            or receipt.basis_event_position != view.basis_event_position
            or receipt.input_context_hash != view.context_hash
        ):
            raise ContextProjectionError("compaction receipt failed CAS or basis validation")
        removed = set(receipt.removed_item_ids)
        updated = view.model_copy(
            update={
                "revision": view.revision + 1,
                "generation": receipt.published_generation,
                "active_memory_items": tuple(
                    item for item in view.active_memory_items if item.item_id not in removed
                ),
                "working_items": tuple(
                    item for item in view.working_items if item.item_id not in removed
                ),
                "recent_settled_tail": tuple(
                    item for item in view.recent_settled_tail if item.item_id not in removed
                ),
                "compacted_prefix_items": receipt.compacted_items,
                "compacted_prefix_ref": receipt.summary_artifact,
                "covered_event_range": receipt.covered_event_range,
                "kept_boundary": receipt.kept_boundary,
                "provider_validity_receipt": None,
            }
        )
        candidate = self.refresh_tokens(_with_hash(updated))
        if candidate.context_hash != receipt.output_context_hash:
            raise ContextProjectionError("compaction output hash differs from receipt")
        return candidate

    def full_replay(
        self,
        seed: AgentContextView,
        events: Iterable[RunEvent],
    ) -> AgentContextView:
        view = seed
        for event in events:
            view = self.apply_event(view, event)
        return view

    def refresh_tokens(self, view: AgentContextView) -> AgentContextView:
        rendered = render_context(view)
        report = {
            "protected": sum(item.token_count for item in view.protected_items),
            "memory": sum(item.token_count for item in view.active_memory_items),
            "working": sum(item.token_count for item in view.working_items),
            "recent_settled": sum(item.token_count for item in view.recent_settled_tail),
            "compacted_prefix": sum(item.token_count for item in view.compacted_prefix_items),
            "rendered": max(0, self._count_tokens(rendered)),
        }
        return view.model_copy(update={"token_report": report})


@dataclass(frozen=True, slots=True)
class ContextWindowPolicy:
    sequence_limit: int
    reserved_output_tokens: int
    safety_allowance_tokens: int
    soft_limit_tokens: int
    tokenizer: str
    tokenizer_version: str

    def __post_init__(self) -> None:
        hard = self.sequence_limit - self.reserved_output_tokens - self.safety_allowance_tokens
        if self.sequence_limit < 1 or hard < 1:
            raise ValueError("Context window leaves no provider input capacity")
        if self.soft_limit_tokens < 1 or self.soft_limit_tokens > hard:
            raise ValueError("Context soft limit must fit provider input capacity")

    @property
    def hard_limit_tokens(self) -> int:
        return self.sequence_limit - self.reserved_output_tokens - self.safety_allowance_tokens


class ContextCompactor:
    """Apply the fixed reduction order while preserving atomic groups and protected data."""

    def __init__(
        self,
        projector: AgentContextProjector,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion,
        token_counter: TokenCounter,
        *,
        summary_function: SummaryFunction | None = None,
    ) -> None:
        self._projector = projector
        self._artifacts = artifacts
        self._schema_version = schema_version
        self._count_tokens = token_counter
        self._summary = summary_function

    def pressure(self, view: AgentContextView, policy: ContextWindowPolicy) -> ContextPressure:
        tokens = self._count_tokens(render_context(view))
        return ContextPressure(
            rendered_input_tokens=tokens,
            available_input_tokens=policy.hard_limit_tokens,
            soft_limit_tokens=policy.soft_limit_tokens,
            hard_limit_tokens=policy.hard_limit_tokens,
            soft_exceeded=tokens > policy.soft_limit_tokens,
            hard_exceeded=tokens > policy.hard_limit_tokens,
        )

    def provider_receipt(
        self,
        view: AgentContextView,
        policy: ContextWindowPolicy,
    ) -> ProviderValidityReceipt:
        rendered_tokens = self._count_tokens(render_context(view))
        atomic_valid = self._atomic_groups_valid(view)
        return ProviderValidityReceipt(
            receipt_id=StableId(f"provider-validity.{view.run_id.root}.{view.revision}"),
            tokenizer=policy.tokenizer,
            tokenizer_version=policy.tokenizer_version,
            sequence_limit=policy.sequence_limit,
            reserved_output_tokens=policy.reserved_output_tokens,
            safety_allowance_tokens=policy.safety_allowance_tokens,
            rendered_input_tokens=rendered_tokens,
            available_input_tokens=policy.hard_limit_tokens,
            atomic_groups_valid=atomic_valid,
            provider_valid=atomic_valid and rendered_tokens <= policy.hard_limit_tokens,
            context_hash=view.context_hash,
        )

    def compact(
        self,
        view: AgentContextView,
        policy: ContextWindowPolicy,
        *,
        hard: bool,
    ) -> tuple[AgentContextView, ContextCompactionReceipt | None]:
        pressure = self.pressure(view, policy)
        target = policy.hard_limit_tokens if hard else policy.soft_limit_tokens
        if pressure.rendered_input_tokens <= target:
            return self._with_provider_receipt(view, policy), None

        groups = self._removable_groups(view)
        # Keep the last safe group available for the provenance-bound summary pass.
        # Otherwise the deterministic removal loop consumes every summarizable item
        # and makes the documented final reduction layer unreachable.
        extractive_groups = groups if self._summary is None else groups[:-1]
        removed: list[ContextViewItem] = []
        candidate = view
        for group in extractive_groups:
            removed.extend(group)
            candidate = self._without(candidate, {item.item_id for item in group})
            if self._count_tokens(render_context(candidate)) <= target:
                break

        if self._count_tokens(render_context(candidate)) > target and self._summary is not None:
            remaining_groups = self._removable_groups(candidate)
            if remaining_groups:
                summary_source = tuple(item for group in remaining_groups for item in group)
                summary_text = self._summary("\n\n".join(item.content for item in summary_source))
                if summary_text.strip():
                    removed.extend(summary_source)
                    candidate = self._without(
                        candidate,
                        {item.item_id for item in summary_source},
                    )
                    summary_item = ContextViewItem(
                        item_id=StableId(
                            f"context-summary.{view.run_id.root}.{view.generation + 1}"
                        ),
                        layer=ContextLayer.COMPACTED_PREFIX,
                        kind=ContextItemKind.RUNTIME_SUMMARY,
                        content=summary_text,
                        token_count=max(1, self._count_tokens(summary_text)),
                        information_scope="runtime",
                    )
                    candidate = candidate.model_copy(
                        update={"compacted_prefix_items": (summary_item,)}
                    )

        if self._count_tokens(render_context(candidate)) > target:
            if hard:
                raise ContextLimitError("no safe compaction closes the provider context limit")
            return view, None
        detail_payload = canonical_json_bytes(
            {"removed_items": [item.model_dump(mode="json") for item in removed]}
        )
        detail_ref = self._artifacts.put(
            detail_payload,
            CONTEXT_DETAIL_MEDIA_TYPE,
            self._schema_version,
        )
        compacted_items = candidate.compacted_prefix_items
        summary_ref = self._artifacts.put(
            canonical_json_bytes(
                {"items": [item.model_dump(mode="json") for item in compacted_items]}
            ),
            CONTEXT_SUMMARY_MEDIA_TYPE,
            self._schema_version,
        )
        ranges = tuple(
            item.source_event_range for item in removed if item.source_event_range is not None
        )
        covered = (
            (min(item[0] for item in ranges), max(item[1] for item in ranges))
            if ranges
            else (1, max(1, view.basis_event_position))
        )
        prepared = candidate.model_copy(
            update={
                "revision": view.revision + 1,
                "generation": view.generation + 1,
                "compacted_prefix_ref": summary_ref,
                "covered_event_range": covered,
                "kept_boundary": covered[1],
                "provider_validity_receipt": None,
            }
        )
        prepared = self._projector.refresh_tokens(_with_hash(prepared))
        receipt = ContextCompactionReceipt(
            receipt_id=StableId(f"compaction.{view.run_id.root}.{view.generation + 1}"),
            run_id=view.run_id,
            parent_view_revision=view.revision,
            parent_generation=view.generation,
            basis_event_position=max(1, view.basis_event_position),
            covered_event_range=covered,
            kept_boundary=covered[1],
            removed_item_ids=tuple(dict.fromkeys(item.item_id for item in removed)),
            compacted_items=compacted_items,
            summary_artifact=summary_ref,
            detail_artifact=detail_ref,
            input_context_hash=view.context_hash,
            output_context_hash=prepared.context_hash,
            deterministic=self._summary is None,
            safe_cut=True,
            published_generation=view.generation + 1,
        )
        return self._with_provider_receipt(prepared, policy), receipt

    def _without(
        self,
        view: AgentContextView,
        removed: set[StableId],
    ) -> AgentContextView:
        return view.model_copy(
            update={
                "active_memory_items": tuple(
                    item for item in view.active_memory_items if item.item_id not in removed
                ),
                "working_items": tuple(
                    item for item in view.working_items if item.item_id not in removed
                ),
                "recent_settled_tail": tuple(
                    item for item in view.recent_settled_tail if item.item_id not in removed
                ),
                "provider_validity_receipt": None,
            }
        )

    @staticmethod
    def _removable_groups(view: AgentContextView) -> tuple[tuple[ContextViewItem, ...], ...]:
        candidates = (
            *view.recent_settled_tail,
            *view.working_items,
            *view.active_memory_items,
        )
        groups: dict[str, list[ContextViewItem]] = {}
        order: list[str] = []
        for item in candidates:
            key = (
                f"group:{item.atomic_group_id.root}"
                if item.atomic_group_id is not None
                else f"item:{item.item_id.root}"
            )
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(item)
        return tuple(
            tuple(groups[key])
            for key in order
            if not any(item.mandatory or item.pending_effect for item in groups[key])
        )

    @staticmethod
    def _atomic_groups_valid(view: AgentContextView) -> bool:
        items = (
            *view.active_memory_items,
            *view.working_items,
            *view.recent_settled_tail,
            *view.compacted_prefix_items,
        )
        positions: dict[StableId, list[int]] = {}
        for index, item in enumerate(items):
            if item.atomic_group_id is not None:
                positions.setdefault(item.atomic_group_id, []).append(index)
        return all(
            indexes == list(range(indexes[0], indexes[-1] + 1)) for indexes in positions.values()
        )

    def _with_provider_receipt(
        self,
        view: AgentContextView,
        policy: ContextWindowPolicy,
    ) -> AgentContextView:
        receipt = self.provider_receipt(view, policy)
        return view.model_copy(update={"provider_validity_receipt": receipt})


class AgentContextRuntime:
    """Persist settled Views and restore from checkpoint plus subsequent RunEvents."""

    def __init__(
        self,
        projector: AgentContextProjector,
        artifacts: ArtifactRepository,
        events: RunEventLogRepository,
        checkpoints: RunCheckpointRepository,
        schema_version: SchemaVersion,
    ) -> None:
        self._projector = projector
        self._artifacts = artifacts
        self._events = events
        self._checkpoints = checkpoints
        self._schema_version = schema_version

    def checkpoint(self, view: AgentContextView, checkpoint_id: StableId) -> RunCheckpoint:
        if view.basis_event_position < 1:
            raise ContextProjectionError("Context View must settle an event before checkpointing")
        ref = self._artifacts.put(
            canonical_json_bytes(view.model_dump(mode="json")),
            CONTEXT_VIEW_MEDIA_TYPE,
            self._schema_version,
        )
        checkpoint = RunCheckpoint(
            checkpoint_id=checkpoint_id,
            run_id=view.run_id,
            event_position=view.basis_event_position,
            logical_stage="stage3.context",
            state_artifact_ref=ref,
            resumability_status=ResumabilityStatus.RESUMABLE,
        )
        return self._checkpoints.save(checkpoint)

    def restore(self, run_id: RunId, seed: AgentContextView) -> AgentContextView:
        checkpoint = self._checkpoints.latest(run_id)
        view = seed
        after = 0
        if checkpoint is not None and checkpoint.logical_stage == "stage3.context":
            raw = self._artifacts.read_verified(checkpoint.state_artifact_ref)
            view = AgentContextView.model_validate_json(raw)
            after = checkpoint.event_position
        events = self._events.replay(run_id, after_sequence=after)
        return self._projector.full_replay(view, events)


__all__ = [
    "CONTEXT_DETAIL_MEDIA_TYPE",
    "CONTEXT_SUMMARY_MEDIA_TYPE",
    "CONTEXT_VIEW_MEDIA_TYPE",
    "AgentContextProjector",
    "AgentContextRuntime",
    "ContextCompactor",
    "ContextLimitError",
    "ContextProjectionError",
    "ContextWindowPolicy",
    "render_context",
]
