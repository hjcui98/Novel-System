from __future__ import annotations

from dataclasses import dataclass

import pytest

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.memory import FreshnessDecision, FreshnessStatus
from novel_agent.domain.stage2 import (
    BenchmarkCheckpointBasis,
    BenchmarkCheckpointDeclaration,
    BenchmarkScenario,
    BootstrapSource,
    ContextFreezeReceipt,
    EvaluatorDisposition,
    ScenarioChapterTransition,
    ScenarioCheckpointArtifacts,
    ScenarioRunResult,
    SourceClass,
)
from novel_agent.services.scenario import ScenarioStateError
from novel_agent.services.teacher_forced_scenario import TeacherForcedScenarioRunner
from tests.unit.test_stage2_scenario import (
    COMMIT_1,
    COMMIT_2,
    GENESIS,
    HASH_A,
    HASH_B,
    HASH_C,
    artifact,
    curator_receipt,
    scenario,
)


def declared_scenario() -> BenchmarkScenario:
    base = scenario()
    return base.model_copy(
        update={
            "profile": base.profile.model_copy(update={"checkpoint_chapters": (1,)}),
            "checkpoint_cases": (
                BenchmarkCheckpointDeclaration(
                    case_id=StableId("case.1"),
                    checkpoint_chapter=1,
                    evaluator_source_ids=(StableId("source.future.private"),),
                ),
            ),
        }
    )


def checkpoint(snapshot: StableId) -> ScenarioCheckpointArtifacts:
    return ScenarioCheckpointArtifacts(
        text_root=HASH_A,
        plan_root=HASH_B,
        world_root=HASH_C,
        derived_snapshot_id=snapshot,
        anchor_alias="anchor",
        grounded_alias="grounded",
        project_profile=HASH_C,
    )


@dataclass
class TransitionPort:
    mutation: str | None = None

    def apply(
        self,
        source: BootstrapSource,
        parent_commit: CommitId,
    ) -> ScenarioChapterTransition:
        source_id = source.source_id
        chapter = source.chapter_index
        assert chapter is not None
        resulting = COMMIT_1 if chapter == 1 else COMMIT_2
        snapshot = StableId(f"snapshot.{chapter}")
        result = ScenarioChapterTransition(
            source_id=source_id,
            parent_commit=parent_commit,
            resulting_commit=resulting,
            curator_receipt=curator_receipt(parent_commit, chapter),
            validation_artifact=artifact(HASH_B),
            projection_snapshot_id=snapshot,
            freshness=FreshnessDecision(
                status=FreshnessStatus.READY,
                canonical_commit=resulting,
                r1_basis_commit=resulting,
                required_snapshot_id=snapshot,
                reason="fixture is current",
            ),
            checkpoint_artifacts=checkpoint(snapshot) if chapter == 1 else None,
        )
        if chapter != 1:
            if self.mutation == "non_checkpoint_artifacts":
                return result.model_copy(update={"checkpoint_artifacts": checkpoint(snapshot)})
            return result
        if self.mutation == "source":
            return result.model_copy(update={"source_id": StableId("source.chapter.other")})
        if self.mutation == "parent":
            return result.model_copy(update={"parent_commit": COMMIT_2})
        if self.mutation == "freshness":
            freshness = result.freshness.model_copy(update={"status": FreshnessStatus.BLOCKED})
            return result.model_copy(update={"freshness": freshness})
        if self.mutation == "missing_checkpoint":
            return result.model_copy(update={"checkpoint_artifacts": None})
        return result


class Freezer:
    def freeze(self, basis: BenchmarkCheckpointBasis) -> ArtifactRef:
        assert basis.case_id == StableId("case.1")
        return artifact(ArtifactId("sha256:" + "d" * 64))


@dataclass
class Evaluator:
    destroyed: bool = True
    resume: bool = True

    def score(
        self,
        freeze: ContextFreezeReceipt,
        evaluator_sources: tuple[BootstrapSource, ...],
    ) -> EvaluatorDisposition:
        assert freeze.case_id == StableId("case.1")
        assert evaluator_sources[0].source_class is SourceClass.FUTURE_TEXT_PRIVATE
        return EvaluatorDisposition(
            evaluator_context_destroyed=self.destroyed,
            teacher_forced_resume_allowed=self.resume,
        )


def runner(
    mutation: str | None = None,
    *,
    destroyed: bool = True,
    resume: bool = True,
) -> TeacherForcedScenarioRunner:
    return TeacherForcedScenarioRunner(
        TransitionPort(mutation),
        Freezer(),
        Evaluator(destroyed=destroyed, resume=resume),
    )


def test_runner_completes_declared_checkpoint_and_continues_replay() -> None:
    result = runner().run(declared_scenario(), GENESIS)

    assert result.completed is True
    assert len(result.chapter_receipts) == 2
    assert len(result.checkpoints) == len(result.freezes) == len(result.evaluator_reveals) == 1


def test_runner_requires_checkpoint_declarations() -> None:
    with pytest.raises(ScenarioStateError, match="requires checkpoint case"):
        runner().run(scenario(), GENESIS)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("source", "another source"),
        ("parent", "stale parent"),
        ("freshness", "cannot continue"),
        ("missing_checkpoint", "lacks checkpoint artifacts"),
        ("non_checkpoint_artifacts", "non-checkpoint chapter"),
    ],
)
def test_runner_rejects_invalid_chapter_transition(
    mutation: str,
    message: str,
) -> None:
    with pytest.raises(ScenarioStateError, match=message):
        runner(mutation).run(declared_scenario(), GENESIS)


def test_runner_requires_evaluator_context_destruction() -> None:
    with pytest.raises(ScenarioStateError, match="context was not destroyed"):
        runner(destroyed=False).run(declared_scenario(), GENESIS)


def test_runner_requires_evaluator_resume_permission() -> None:
    with pytest.raises(ScenarioStateError, match="rejected teacher-forced resume"):
        runner(resume=False).run(declared_scenario(), GENESIS)


def test_runner_rejects_invalid_worker_count() -> None:
    with pytest.raises(ValueError, match="checkpoint workers"):
        TeacherForcedScenarioRunner(
            TransitionPort(),
            Freezer(),
            Evaluator(),
            checkpoint_workers=0,
        )


def test_runner_runs_corridor_concurrently_with_replay() -> None:
    import threading
    import time

    release = threading.Event()
    corridor_entered = threading.Event()

    class BlockingFreezer(Freezer):
        def freeze(self, basis: BenchmarkCheckpointBasis) -> ArtifactRef:
            corridor_entered.set()
            release.wait(timeout=10)
            return super().freeze(basis)

    replay_advanced: list[bool] = []

    class TrackingTransition(TransitionPort):
        def apply(
            self,
            source: BootstrapSource,
            parent_commit: CommitId,
        ) -> ScenarioChapterTransition:
            result = super().apply(source, parent_commit)
            if source.chapter_index != 1:
                replay_advanced.append(corridor_entered.is_set())
            return result

    result = TeacherForcedScenarioRunner(
        TrackingTransition(),
        BlockingFreezer(),
        Evaluator(),
        checkpoint_workers=1,
    )
    outcome: list[ScenarioRunResult | BaseException] = []

    def run_in_thread() -> None:
        try:
            outcome.append(result.run(declared_scenario(), GENESIS))
        except BaseException as error:  # pragma: no cover - assertion path
            outcome.append(error)

    thread = threading.Thread(target=run_in_thread)
    thread.start()
    assert corridor_entered.wait(timeout=10), "corridor must start while replay continues"
    # While the corridor is blocked, the replay must already have advanced to
    # the second chapter.
    deadline = time.monotonic() + 10
    while not replay_advanced and time.monotonic() < deadline:
        time.sleep(0.01)
    assert replay_advanced == [True], "replay must continue past the checkpoint"
    release.set()
    thread.join(timeout=15)
    assert len(outcome) == 1
    assert isinstance(outcome[0], ScenarioRunResult)
    assert outcome[0].completed is True
    assert len(outcome[0].freezes) == 1
    assert len(outcome[0].evaluator_reveals) == 1


def test_runner_propagates_corridor_failures_in_checkpoint_order() -> None:
    class FailingEvaluator(Evaluator):
        def score(
            self,
            freeze: ContextFreezeReceipt,
            evaluator_sources: tuple[BootstrapSource, ...],
        ) -> EvaluatorDisposition:
            raise RuntimeError("corridor exploded")

    with pytest.raises(ScenarioStateError, match="corridor failed"):
        TeacherForcedScenarioRunner(
            TransitionPort(),
            Freezer(),
            FailingEvaluator(),
        ).run(declared_scenario(), GENESIS)
