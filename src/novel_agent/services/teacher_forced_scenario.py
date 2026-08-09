"""Port-driven continuous replay orchestration with strict evaluator isolation."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import ClassVar, Protocol

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory import FreshnessStatus
from novel_agent.domain.stage2 import (
    BenchmarkCheckpointBasis,
    BenchmarkScenario,
    BootstrapSource,
    ContextFreezeReceipt,
    EvaluatorDisposition,
    ScenarioChapterTransition,
    ScenarioRunResult,
    SourceClass,
)
from novel_agent.services.scenario import ScenarioStateBuilder, ScenarioStateError


class ChapterStateTransitionPort(Protocol):
    def apply(
        self,
        source: BootstrapSource,
        parent_commit: CommitId,
    ) -> ScenarioChapterTransition: ...


class ContextFreezePort(Protocol):
    def freeze(self, basis: BenchmarkCheckpointBasis) -> ArtifactRef: ...


class EvaluatorPort(Protocol):
    def score(
        self,
        freeze: ContextFreezeReceipt,
        evaluator_sources: tuple[BootstrapSource, ...],
    ) -> EvaluatorDisposition: ...


class TeacherForcedScenarioRunner:
    """Own ordering only; all mutation, compilation, and scoring remain injected services.

    Checkpoint corridors (freeze -> score -> reveal) run on a bounded worker
    pool while the chapter replay continues: the replay pipeline no longer
    stalls on LLM-heavy corridor work.  Concurrency changes scheduling only;
    every corridor still executes its own dependency chain
    (freeze before score before reveal) and corridor results are collected in
    checkpoint order at the end of the run.  Builder mutations are serialized
    by a lock so concurrent reveals cannot corrupt the receipt chain.
    """

    _CONTINUABLE_FRESHNESS: ClassVar[set[FreshnessStatus]] = {
        FreshnessStatus.READY,
        FreshnessStatus.DEGRADED,
        FreshnessStatus.OVERRIDDEN,
    }

    def __init__(
        self,
        transition: ChapterStateTransitionPort,
        context_freezer: ContextFreezePort,
        evaluator: EvaluatorPort,
        *,
        checkpoint_workers: int = 2,
    ) -> None:
        if checkpoint_workers < 1:
            raise ValueError("checkpoint workers must be positive")
        self._transition = transition
        self._context_freezer = context_freezer
        self._evaluator = evaluator
        self._checkpoint_workers = checkpoint_workers

    def run(
        self,
        scenario: BenchmarkScenario,
        genesis_commit: CommitId,
    ) -> ScenarioRunResult:
        if not scenario.checkpoint_cases:
            raise ScenarioStateError(
                "teacher-forced scenario requires checkpoint case declarations"
            )
        declaration_by_chapter = {
            declaration.checkpoint_chapter: declaration for declaration in scenario.checkpoint_cases
        }
        chapter_sources = tuple(
            sorted(
                (
                    source
                    for source in scenario.sources
                    if source.source_class is SourceClass.CHAPTER_TEXT
                ),
                key=lambda source: source.chapter_index or 0,
            )
        )
        builder = ScenarioStateBuilder(scenario, genesis_commit)
        source_by_id = {item.source_id: item for item in scenario.sources}
        builder_lock = threading.Lock()
        current_commit = genesis_commit
        pending: list[tuple[int, Future[EvaluatorDisposition]]] = []
        with ThreadPoolExecutor(
            max_workers=self._checkpoint_workers,
            thread_name_prefix="checkpoint-corridor",
        ) as executor:
            for source in chapter_sources:
                transition = self._transition.apply(source, current_commit)
                if transition.source_id != source.source_id:
                    raise ScenarioStateError("chapter transition returned another source")
                if transition.parent_commit != current_commit:
                    raise ScenarioStateError("chapter transition returned a stale parent commit")
                if transition.freshness.status not in self._CONTINUABLE_FRESHNESS:
                    raise ScenarioStateError(
                        f"chapter transition cannot continue with freshness "
                        f"{transition.freshness.status.value}"
                    )
                with builder_lock:
                    builder.record_chapter(
                        source_id=source.source_id,
                        resulting_commit=transition.resulting_commit,
                        curator_receipt=transition.curator_receipt,
                        validation_artifact=transition.validation_artifact,
                        projection_snapshot_id=transition.projection_snapshot_id,
                    )
                current_commit = transition.resulting_commit
                chapter = source.chapter_index
                if chapter not in declaration_by_chapter:
                    if transition.checkpoint_artifacts is not None:
                        raise ScenarioStateError(
                            "non-checkpoint chapter returned checkpoint artifacts"
                        )
                    continue
                checkpoint = transition.checkpoint_artifacts
                if checkpoint is None:
                    raise ScenarioStateError("checkpoint chapter lacks checkpoint artifacts")
                declaration = declaration_by_chapter[chapter]
                case_id = declaration.case_id
                with builder_lock:
                    basis = builder.checkpoint(
                        case_id=case_id,
                        text_root=checkpoint.text_root,
                        plan_root=checkpoint.plan_root,
                        world_root=checkpoint.world_root,
                        derived_snapshot_id=checkpoint.derived_snapshot_id,
                        anchor_alias=checkpoint.anchor_alias,
                        grounded_alias=checkpoint.grounded_alias,
                        project_profile=checkpoint.project_profile,
                    )
                evaluator_sources = tuple(
                    source_by_id[source_id] for source_id in declaration.evaluator_source_ids
                )
                pending.append(
                    (
                        chapter,
                        executor.submit(
                            self._run_checkpoint_corridor,
                            builder=builder,
                            builder_lock=builder_lock,
                            basis=basis,
                            evaluator_sources=evaluator_sources,
                            case_id=case_id,
                        ),
                    )
                )
            for chapter, future in sorted(pending):
                try:
                    disposition = future.result()
                except ScenarioStateError:
                    raise
                except Exception as error:
                    raise ScenarioStateError(f"checkpoint {chapter} corridor failed") from error
                if not disposition.evaluator_context_destroyed:
                    raise ScenarioStateError(
                        f"checkpoint {chapter} evaluator context was not destroyed after scoring"
                    )
        return builder.result()

    def _run_checkpoint_corridor(
        self,
        *,
        builder: ScenarioStateBuilder,
        builder_lock: threading.Lock,
        basis: BenchmarkCheckpointBasis,
        evaluator_sources: tuple[BootstrapSource, ...],
        case_id: StableId,
    ) -> EvaluatorDisposition:
        """One checkpoint corridor: freeze -> score -> reveal (single worker)."""

        freeze_ref = self._context_freezer.freeze(basis)
        with builder_lock:
            freeze = builder.freeze_context(
                case_id=case_id,
                context_artifact=freeze_ref,
            )
        disposition = self._evaluator.score(freeze, evaluator_sources)
        with builder_lock:
            builder.reveal_to_evaluator(
                freeze_id=freeze.freeze_id,
                evaluator_source_ids=tuple(item.source_id for item in evaluator_sources),
                evaluator_context_destroyed=True,
                score_artifacts=disposition.score_artifacts,
            )
        if not disposition.teacher_forced_resume_allowed:
            raise ScenarioStateError("evaluator rejected teacher-forced resume")
        return disposition
