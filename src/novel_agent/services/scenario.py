"""Trusted Stage 2 benchmark scenario state and evaluator-isolation lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    BenchmarkCheckpointBasis,
    BenchmarkInformationProfile,
    BenchmarkScenario,
    ChapterStateBuildReceipt,
    ContextFreezeReceipt,
    EvaluatorRevealReceipt,
    FutureIsolationAttestation,
    ScenarioRunResult,
    SourceClass,
)
from novel_agent.services.content_addressing import content_id


class ScenarioStateError(ValueError):
    """Raised when scenario ordering, authority, or isolation invariants are violated."""


def utc_now() -> datetime:
    return datetime.now(UTC)


class ScenarioStateBuilder:
    """Builds a receipt chain while keeping evaluator-only sources outside Canon."""

    def __init__(
        self,
        scenario: BenchmarkScenario,
        genesis_commit: CommitId,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._scenario = scenario
        self._current_commit = genesis_commit
        self._clock = clock
        self._receipts: list[ChapterStateBuildReceipt] = []
        self._checkpoints: list[BenchmarkCheckpointBasis] = []
        self._freezes: list[ContextFreezeReceipt] = []
        self._reveals: list[EvaluatorRevealReceipt] = []
        self._sources = {source.source_id: source for source in scenario.sources}
        self._chapter_sources = tuple(
            sorted(
                (
                    source
                    for source in scenario.sources
                    if source.source_class is SourceClass.CHAPTER_TEXT
                ),
                key=lambda source: source.chapter_index or 0,
            )
        )

    def record_chapter(
        self,
        *,
        source_id: StableId,
        resulting_commit: CommitId,
        curator_receipt: AgentExecutionReceipt,
        validation_artifact: ArtifactRef,
        projection_snapshot_id: StableId,
    ) -> ChapterStateBuildReceipt:
        source = self._sources.get(source_id)
        if source is None or source.source_class is not SourceClass.CHAPTER_TEXT:
            raise ScenarioStateError("chapter reveal requires a registered chapter source")
        if source.evaluator_only:
            raise ScenarioStateError("evaluator-only source cannot enter chapter state build")
        position = len(self._receipts)
        if position >= len(self._chapter_sources) or self._chapter_sources[position] != source:
            raise ScenarioStateError("chapter sources must be revealed in strict scenario order")
        if curator_receipt.base_commit != self._current_commit:
            raise ScenarioStateError("curator receipt base does not match current canonical commit")
        previous_hash = self._receipts[-1].chain_hash if self._receipts else None
        chapter_index = cast(int, source.chapter_index)
        chain_hash = content_id(
            {
                "scenario_id": self._scenario.scenario_id.root,
                "project_id": self._scenario.project_id.root,
                "chapter_index": chapter_index,
                "parent_commit": self._current_commit.root,
                "resulting_commit": resulting_commit.root,
                "source_id": source_id.root,
                "curator_receipt_id": curator_receipt.receipt_id.root,
                "validation_artifact": validation_artifact.model_dump(mode="json"),
                "projection_snapshot_id": projection_snapshot_id.root,
                "previous_chain_hash": previous_hash.root if previous_hash else None,
            }
        )
        receipt = ChapterStateBuildReceipt(
            receipt_id=StableId(f"chapter-state.{self._scenario.scenario_id.root}.{chapter_index}"),
            project_id=self._scenario.project_id,
            chapter_index=chapter_index,
            parent_commit=self._current_commit,
            resulting_commit=resulting_commit,
            source_id=source_id,
            curator_receipt=curator_receipt,
            validation_artifact=validation_artifact,
            projection_snapshot_id=projection_snapshot_id,
            previous_chain_hash=previous_hash,
            chain_hash=chain_hash,
        )
        self._receipts.append(receipt)
        self._current_commit = resulting_commit
        return receipt

    def checkpoint(
        self,
        *,
        case_id: StableId,
        text_root: ArtifactId,
        plan_root: ArtifactId,
        world_root: ArtifactId,
        derived_snapshot_id: StableId,
        anchor_alias: str,
        grounded_alias: str,
        project_profile: ArtifactId,
    ) -> BenchmarkCheckpointBasis:
        if not self._receipts:
            raise ScenarioStateError("checkpoint requires at least one revealed chapter")
        chapter = self._receipts[-1].chapter_index
        if chapter not in self._scenario.profile.checkpoint_chapters:
            raise ScenarioStateError("current chapter is not a declared scenario checkpoint")
        if any(item.last_revealed_chapter == chapter for item in self._checkpoints):
            raise ScenarioStateError("scenario checkpoint is already frozen")
        canonical_ids = self._canonical_source_ids(chapter)
        evaluator_ids = tuple(
            source.source_id for source in self._scenario.sources if source.evaluator_only
        )
        overlap = tuple(sorted(set(canonical_ids) & set(evaluator_ids), key=lambda item: item.root))
        attestation = FutureIsolationAttestation(
            attestation_id=StableId(f"future-isolation.{case_id.root}.{chapter}"),
            checkpoint_chapter=chapter,
            canonical_source_ids=canonical_ids,
            evaluator_only_source_ids=evaluator_ids,
            overlap_source_ids=overlap,
            passed=not overlap,
            configuration_fingerprint=self._scenario.profile.configuration_fingerprint,
        )
        basis = BenchmarkCheckpointBasis(
            case_id=case_id,
            project_id=self._scenario.project_id,
            branch=self._scenario.branch,
            canonical_commit=self._current_commit,
            text_root=text_root,
            plan_root=plan_root,
            world_root=world_root,
            derived_snapshot_id=derived_snapshot_id,
            r1_basis_commit=self._current_commit,
            anchor_alias=anchor_alias,
            grounded_alias=grounded_alias,
            project_profile=project_profile,
            configuration_fingerprint=self._scenario.profile.configuration_fingerprint,
            last_revealed_chapter=chapter,
            future_isolation=attestation,
            state_build_receipt_chain_hash=self._receipts[-1].chain_hash,
        )
        self._checkpoints.append(basis)
        return basis

    def freeze_context(
        self,
        *,
        case_id: StableId,
        context_artifact: ArtifactRef,
    ) -> ContextFreezeReceipt:
        basis = next(
            (item for item in reversed(self._checkpoints) if item.case_id == case_id),
            None,
        )
        if basis is None:
            raise ScenarioStateError("context can only freeze against a recorded checkpoint")
        if any(item.case_id == case_id for item in self._freezes):
            raise ScenarioStateError("case context is already frozen")
        receipt = ContextFreezeReceipt(
            freeze_id=StableId(f"context-freeze.{case_id.root}.{basis.last_revealed_chapter}"),
            case_id=case_id,
            checkpoint_chapter=basis.last_revealed_chapter,
            canonical_commit=basis.canonical_commit,
            snapshot_id=basis.derived_snapshot_id,
            context_artifact=context_artifact,
            configuration_fingerprint=basis.configuration_fingerprint,
            frozen_at=self._clock(),
        )
        self._freezes.append(receipt)
        return receipt

    def reveal_to_evaluator(
        self,
        *,
        freeze_id: StableId,
        evaluator_source_ids: tuple[StableId, ...],
        evaluator_context_destroyed: bool,
        score_artifacts: tuple[ArtifactRef, ...] = (),
    ) -> EvaluatorRevealReceipt:
        if not any(item.freeze_id == freeze_id for item in self._freezes):
            raise ScenarioStateError("evaluator reveal requires a prior context freeze")
        if any(item.freeze_id == freeze_id for item in self._reveals):
            raise ScenarioStateError("evaluator sources were already revealed for this freeze")
        if not evaluator_source_ids:
            raise ScenarioStateError("evaluator reveal requires at least one private source")
        if any(
            source_id not in self._sources or not self._sources[source_id].evaluator_only
            for source_id in evaluator_source_ids
        ):
            raise ScenarioStateError("evaluator reveal may contain evaluator-only sources only")
        if not evaluator_context_destroyed:
            raise ScenarioStateError("evaluator working context must be destroyed after scoring")
        receipt = EvaluatorRevealReceipt(
            reveal_id=StableId(f"evaluator-reveal.{freeze_id.root}"),
            freeze_id=freeze_id,
            evaluator_source_ids=evaluator_source_ids,
            score_artifacts=score_artifacts,
            evaluator_context_destroyed=True,
            completed_at=self._clock(),
        )
        self._reveals.append(receipt)
        return receipt

    def result(self) -> ScenarioRunResult:
        expected = set(self._scenario.profile.checkpoint_chapters)
        actual = {item.last_revealed_chapter for item in self._checkpoints}
        freeze_ids = {item.freeze_id for item in self._freezes}
        revealed_freezes = {item.freeze_id for item in self._reveals}
        blockers: list[str] = []
        if actual != expected:
            blockers.append("not all declared checkpoints were built")
        if freeze_ids != revealed_freezes or len(freeze_ids) != len(expected):
            blockers.append("checkpoint freeze/evaluator lifecycle is incomplete")
        return ScenarioRunResult(
            scenario_id=self._scenario.scenario_id,
            project_id=self._scenario.project_id,
            build_mode=self._scenario.profile.build_mode,
            chapter_receipts=tuple(self._receipts),
            checkpoints=tuple(self._checkpoints),
            freezes=tuple(self._freezes),
            evaluator_reveals=tuple(self._reveals),
            completed=not blockers,
            blockers=tuple(blockers),
        )

    def _canonical_source_ids(self, chapter: int) -> tuple[StableId, ...]:
        allow_plan = (
            self._scenario.profile.information_profile
            is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
        )
        visible: list[StableId] = []
        for source in self._scenario.sources:
            if source.evaluator_only:
                continue
            if source.source_class is SourceClass.AUTHOR_KNOWN_FUTURE_PLAN and not allow_plan:
                continue
            if (
                source.earliest_visible_chapter is not None
                and source.earliest_visible_chapter > chapter
            ):
                continue
            visible.append(source.source_id)
        return tuple(visible)
