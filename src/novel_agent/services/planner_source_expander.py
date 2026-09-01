"""Cutoff-safe P1 L0 previews from World evidence_refs on a frozen TextRoot."""

from __future__ import annotations

from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.planning_memory import (
    PlannerPreviewStatus,
    PlannerSourceExpansion,
    PlannerSourcePreview,
)
from novel_agent.domain.text import EvidenceRef, TextBlock
from novel_agent.domain.writer_context import BenchmarkTaskContract
from novel_agent.services.content_addressing import world_root_content_id
from novel_agent.services.evidence_slice_resolver import (
    EvidenceSliceResolver,
    LiveEvidenceBasis,
    text_root_indexes,
)

DEFAULT_PREVIEW_LIMIT = 12
DEFAULT_PREVIEW_CHARS = 400


class PlannerSourceExpander:
    """Attach cutoff-safe L0 previews. Invalid refs fail closed for that preview only."""

    def __init__(
        self,
        *,
        resolver: EvidenceSliceResolver | None = None,
        preview_limit: int = DEFAULT_PREVIEW_LIMIT,
        preview_chars: int = DEFAULT_PREVIEW_CHARS,
    ) -> None:
        if preview_limit < 1 or preview_chars < 1:
            raise ValueError("P1 preview limits must be positive")
        self._resolver = resolver or EvidenceSliceResolver()
        self._preview_limit = preview_limit
        self._preview_chars = preview_chars

    def expand(
        self,
        *,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        text: TextRootDocument,
        snapshot_id: StableId,
        selected_record_ids: tuple[StableId, ...] | None = None,
        request_commit: CommitId | None = None,
        request_snapshot_id: StableId | None = None,
    ) -> PlannerSourceExpansion:
        blocks, chapter_indexes = text_root_indexes(text)
        chapters = {chapter.chapter_id: chapter.chapter_index for chapter in text.chapters}
        # WorldRoot is content-addressed at the import boundary.  A caller can
        # still hand this service an in-memory copy, however; if its source
        # commit was changed without recomputing the root identity, treating
        # its evidence as live would silently bind P1 to an unverified basis.
        # Keep the failure local to each selected preview so the P0 summary can
        # still be emitted and the caller receives the existing stale signal.
        world_identity_valid = world_root_content_id(world) == world.root_hash
        basis = LiveEvidenceBasis(
            request_commit=request_commit or world.source_commit,
            request_snapshot_id=request_snapshot_id or snapshot_id,
            checkpoint_chapter=task.checkpoint_chapter,
        )
        previews: list[PlannerSourcePreview] = []
        records = _priority_records(world, selected_record_ids)
        selection = (
            tuple(dict.fromkeys(selected_record_ids))
            if selected_record_ids is not None
            else tuple(record_id for record_id, _kind, _refs in records)
        )
        for record_id, kind, refs in records:
            if len(previews) >= self._preview_limit:
                break
            for ref in refs:
                if len(previews) >= self._preview_limit:
                    break
                previews.append(
                    self._preview(
                        world=world,
                        snapshot_id=snapshot_id,
                        record_id=record_id,
                        record_kind=kind,
                        ref=ref,
                        blocks=blocks,
                        chapters=chapters,
                        chapter_indexes=chapter_indexes,
                        basis=basis,
                        world_identity_valid=world_identity_valid,
                    )
                )
        resolved = tuple(item for item in previews if item.status is PlannerPreviewStatus.RESOLVED)
        return PlannerSourceExpansion(
            hit_record_count=len({item.record_id for item in previews}),
            resolved_count=len(resolved),
            missing_count=sum(item.status is PlannerPreviewStatus.MISSING for item in previews),
            stale_count=sum(item.status is PlannerPreviewStatus.STALE for item in previews),
            cutoff_excluded_count=sum(
                item.status is PlannerPreviewStatus.CUTOFF_EXCLUDED for item in previews
            ),
            preview_tokens=_tokens("\n".join(item.text for item in resolved)),
            selected_record_ids=selection,
            truncated_count=sum(item.truncated for item in resolved),
            previews=tuple(previews),
        )

    def _preview(
        self,
        *,
        world: WorldRootDocument,
        snapshot_id: StableId,
        record_id: StableId,
        record_kind: str,
        ref: EvidenceRef,
        blocks: dict[StableId, TextBlock],
        chapters: dict[StableId, int],
        chapter_indexes: dict[StableId, int],
        basis: LiveEvidenceBasis,
        world_identity_valid: bool,
    ) -> PlannerSourcePreview:
        preview_chapter = chapters.get(ref.chapter_id) if ref.chapter_id is not None else None
        if not world_identity_valid:
            return PlannerSourcePreview(
                record_id=record_id,
                record_kind=record_kind,
                evidence_id=ref.evidence_id,
                status=PlannerPreviewStatus.STALE,
                source_commit=world.source_commit,
                chapter_index=preview_chapter,
            )
        block = blocks.get(ref.span.block_id) if ref.span is not None else None
        live_chapter = None if block is None else chapter_indexes.get(block.chapter_id)
        decision = self._resolver.live_decision(
            basis=basis,
            unit_source_commit=world.source_commit,
            unit_snapshot_id=snapshot_id,
            evidence=ref,
            block=block,
            chapter_index=live_chapter,
        )
        if not decision.live:
            if decision.reason == "cutoff":
                status = PlannerPreviewStatus.CUTOFF_EXCLUDED
            elif decision.reason in {"source_commit_mismatch", "snapshot_mismatch"}:
                status = PlannerPreviewStatus.STALE
            else:
                status = PlannerPreviewStatus.MISSING
            return PlannerSourcePreview(
                record_id=record_id,
                record_kind=record_kind,
                evidence_id=ref.evidence_id,
                status=status,
                source_commit=world.source_commit,
                chapter_index=preview_chapter,
            )
        slices = self._resolver.resolve_live_evidence(
            basis=basis,
            unit_source_commit=world.source_commit,
            unit_snapshot_id=snapshot_id,
            evidence=ref,
            block=block,
            chapter_index=live_chapter,
            access_scope="planner_safe",
        )
        if not slices:
            return PlannerSourcePreview(
                record_id=record_id,
                record_kind=record_kind,
                evidence_id=ref.evidence_id,
                status=PlannerPreviewStatus.MISSING,
                source_commit=world.source_commit,
                chapter_index=preview_chapter,
            )
        preview_text = slices[0].text
        truncated = len(preview_text) > self._preview_chars
        return PlannerSourcePreview(
            record_id=record_id,
            record_kind=record_kind,
            evidence_id=ref.evidence_id,
            status=PlannerPreviewStatus.RESOLVED,
            source_commit=world.source_commit,
            chapter_index=preview_chapter,
            text=preview_text[: self._preview_chars],
            truncated=truncated,
        )


def _priority_records(
    world: WorldRootDocument,
    selected_record_ids: tuple[StableId, ...] | None = None,
) -> tuple[tuple[StableId, str, tuple[EvidenceRef, ...]], ...]:
    records: list[tuple[StableId, str, tuple[EvidenceRef, ...]]] = []
    for event in world.events:
        if event.evidence_refs:
            records.append((event.event_id, "event", event.evidence_refs))
    for state in world.states:
        if state.evidence_refs:
            records.append((state.state_id, "state", state.evidence_refs))
    for relation in world.relations:
        if relation.evidence_refs:
            records.append((relation.relation_id, "relation", relation.evidence_refs))
    for obligation in world.obligations:
        if obligation.evidence_refs:
            records.append((obligation.obligation_id, "obligation", obligation.evidence_refs))
    if selected_record_ids is None:
        return tuple(records)
    by_id = {record_id: (record_id, kind, refs) for record_id, kind, refs in records}
    return tuple(
        by_id[record_id] for record_id in dict.fromkeys(selected_record_ids) if record_id in by_id
    )


def _tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 2) // 3)
