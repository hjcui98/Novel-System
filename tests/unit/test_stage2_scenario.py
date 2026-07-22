from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    BenchmarkInformationProfile,
    BenchmarkScenario,
    BenchmarkScenarioProfile,
    BootstrapSource,
    ContractRef,
    EvaluatorRevealReceipt,
    ExecutionStatus,
    ScenarioBuildMode,
    ScenarioRunResult,
    SourceClass,
    SourceClassification,
    SourceDestination,
)
from novel_agent.services.scenario import ScenarioStateBuilder, ScenarioStateError

VERSION = SchemaVersion("2.0.0")
PROJECT = ProjectId("project.scenario")
HASH_A = ArtifactId("sha256:" + "a" * 64)
HASH_B = ArtifactId("sha256:" + "b" * 64)
HASH_C = ArtifactId("sha256:" + "c" * 64)
GENESIS = CommitId("sha256:" + "0" * 64)
COMMIT_1 = CommitId("sha256:" + "1" * 64)
COMMIT_2 = CommitId("sha256:" + "2" * 64)
NOW = datetime(2026, 7, 21, tzinfo=UTC)


def artifact(value: ArtifactId = HASH_A) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=value,
        media_type="application/json",
        byte_length=1,
        schema_version=VERSION,
    )


def source(
    source_id: str,
    source_class: SourceClass,
    *,
    chapter: int | None = None,
    evaluator_only: bool = False,
) -> BootstrapSource:
    return BootstrapSource(
        source_id=StableId(source_id),
        source_class=source_class,
        media_type="text/plain",
        content_hash=HASH_A,
        byte_length=1,
        artifact_ref=artifact(),
        earliest_visible_chapter=chapter,
        chapter_index=chapter if source_class is SourceClass.CHAPTER_TEXT else None,
        evaluator_only=evaluator_only,
    )


def classification(item: BootstrapSource) -> SourceClassification:
    destination = (
        SourceDestination.EVALUATION
        if item.evaluator_only
        else SourceDestination.TEXT
        if item.source_class is SourceClass.CHAPTER_TEXT
        else SourceDestination.PLAN
    )
    return SourceClassification(
        source_id=item.source_id,
        source_class=item.source_class,
        allowed_destinations=(destination,),
        classification_reason="fixed fixture classification",
    )


def scenario(
    *,
    information_profile: BenchmarkInformationProfile = (
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF
    ),
) -> BenchmarkScenario:
    sources = (
        source("source.brief", SourceClass.AUTHOR_INITIAL_BRIEF),
        source("source.plan", SourceClass.AUTHOR_KNOWN_FUTURE_PLAN),
        source("source.chapter.1", SourceClass.CHAPTER_TEXT, chapter=1),
        source("source.chapter.2", SourceClass.CHAPTER_TEXT, chapter=2),
        source(
            "source.future.private",
            SourceClass.FUTURE_TEXT_PRIVATE,
            evaluator_only=True,
        ),
    )
    return BenchmarkScenario(
        scenario_id=StableId("scenario.1"),
        project_id=PROJECT,
        branch="benchmark/main",
        sources=sources,
        classifications=tuple(classification(item) for item in sources),
        profile=BenchmarkScenarioProfile(
            profile_id=StableId("profile.scenario"),
            build_mode=ScenarioBuildMode.CONTINUOUS_REPLAY,
            information_profile=information_profile,
            checkpoint_chapters=(1, 2),
            configuration_fingerprint=HASH_C,
        ),
    )


def curator_receipt(base: CommitId, chapter: int) -> AgentExecutionReceipt:
    return AgentExecutionReceipt(
        receipt_id=StableId(f"receipt.curator.{chapter}"),
        run_id=RunId("run.scenario"),
        task_id=TaskId(f"task.chapter.{chapter}"),
        agent_spec=ContractRef(
            contract_id=StableId("agent.curator.replay"),
            version=VERSION,
            content_hash=HASH_A,
        ),
        agent_type=AgentType.MEMORY_CURATOR,
        agent_mode=AgentMode.REPLAY,
        prompt_fingerprint=HASH_A,
        configuration_fingerprint=HASH_C,
        base_commit=base,
        status=ExecutionStatus.SUCCEEDED,
        started_at=NOW,
        completed_at=NOW,
        latency_ms=0,
    )


def record(builder: ScenarioStateBuilder, chapter: int) -> None:
    base = GENESIS if chapter == 1 else COMMIT_1
    resulting = COMMIT_1 if chapter == 1 else COMMIT_2
    builder.record_chapter(
        source_id=StableId(f"source.chapter.{chapter}"),
        resulting_commit=resulting,
        curator_receipt=curator_receipt(base, chapter),
        validation_artifact=artifact(HASH_B),
        projection_snapshot_id=StableId(f"snapshot.{chapter}"),
    )


def checkpoint(builder: ScenarioStateBuilder, chapter: int) -> None:
    builder.checkpoint(
        case_id=StableId(f"case.{chapter}"),
        text_root=HASH_A,
        plan_root=HASH_B,
        world_root=HASH_C,
        derived_snapshot_id=StableId(f"snapshot.{chapter}"),
        anchor_alias=f"anchor-{chapter}",
        grounded_alias=f"grounded-{chapter}",
        project_profile=HASH_C,
    )


def evaluate(builder: ScenarioStateBuilder, chapter: int) -> None:
    freeze = builder.freeze_context(
        case_id=StableId(f"case.{chapter}"), context_artifact=artifact(HASH_C)
    )
    builder.reveal_to_evaluator(
        freeze_id=freeze.freeze_id,
        evaluator_source_ids=(StableId("source.future.private"),),
        evaluator_context_destroyed=True,
    )


def test_scenario_builds_chained_checkpoints_and_isolated_evaluator_lifecycle() -> None:
    builder = ScenarioStateBuilder(scenario(), GENESIS, clock=lambda: NOW)
    record(builder, 1)
    checkpoint(builder, 1)
    evaluate(builder, 1)
    record(builder, 2)
    checkpoint(builder, 2)
    evaluate(builder, 2)

    result = builder.result()

    assert result.completed is True
    assert result.blockers == ()
    assert result.chapter_receipts[1].previous_chain_hash == result.chapter_receipts[0].chain_hash
    first_basis = result.checkpoints[0]
    assert first_basis.future_isolation.passed is True
    assert StableId("source.chapter.2") not in first_basis.future_isolation.canonical_source_ids
    assert StableId("source.plan") not in first_basis.future_isolation.canonical_source_ids
    assert first_basis.future_isolation.evaluator_only_source_ids == (
        StableId("source.future.private"),
    )
    assert result.evaluator_reveals[0].canonical_write_count == 0


def test_author_plan_conditioned_profile_exposes_plan_without_promoting_private_data() -> None:
    builder = ScenarioStateBuilder(
        scenario(information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED),
        GENESIS,
    )
    record(builder, 1)
    checkpoint(builder, 1)
    basis = builder.result().checkpoints[0]

    assert StableId("source.plan") in basis.future_isolation.canonical_source_ids
    assert StableId("source.future.private") not in basis.future_isolation.canonical_source_ids


def test_scenario_rejects_out_of_order_or_untrusted_state_build_inputs() -> None:
    builder = ScenarioStateBuilder(scenario(), GENESIS)
    with pytest.raises(ScenarioStateError, match="strict scenario order"):
        record(builder, 2)
    with pytest.raises(ScenarioStateError, match="registered chapter source"):
        builder.record_chapter(
            source_id=StableId("source.future.private"),
            resulting_commit=COMMIT_1,
            curator_receipt=curator_receipt(GENESIS, 1),
            validation_artifact=artifact(),
            projection_snapshot_id=StableId("snapshot.1"),
        )
    with pytest.raises(ScenarioStateError, match="curator receipt base"):
        builder.record_chapter(
            source_id=StableId("source.chapter.1"),
            resulting_commit=COMMIT_1,
            curator_receipt=curator_receipt(COMMIT_1, 1),
            validation_artifact=artifact(),
            projection_snapshot_id=StableId("snapshot.1"),
        )
    private_chapter = source(
        "source.chapter.private", SourceClass.CHAPTER_TEXT, chapter=3, evaluator_only=True
    )
    private_scenario = BenchmarkScenario(
        scenario_id=StableId("scenario.private-chapter"),
        project_id=PROJECT,
        branch="benchmark/private",
        sources=(private_chapter,),
        classifications=(classification(private_chapter),),
        profile=BenchmarkScenarioProfile(
            profile_id=StableId("profile.private-chapter"),
            build_mode=ScenarioBuildMode.CONTINUOUS_REPLAY,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            checkpoint_chapters=(3,),
            configuration_fingerprint=HASH_C,
        ),
    )
    private_builder = ScenarioStateBuilder(private_scenario, GENESIS)
    with pytest.raises(ScenarioStateError, match="evaluator-only source"):
        private_builder.record_chapter(
            source_id=private_chapter.source_id,
            resulting_commit=COMMIT_1,
            curator_receipt=curator_receipt(GENESIS, 3),
            validation_artifact=artifact(),
            projection_snapshot_id=StableId("snapshot.3"),
        )


def test_scenario_requires_checkpoint_then_freeze_then_private_reveal() -> None:
    builder = ScenarioStateBuilder(scenario(), GENESIS)
    with pytest.raises(ScenarioStateError, match="at least one"):
        checkpoint(builder, 1)
    with pytest.raises(ScenarioStateError, match="recorded checkpoint"):
        builder.freeze_context(case_id=StableId("case.1"), context_artifact=artifact())
    with pytest.raises(ScenarioStateError, match="prior context freeze"):
        builder.reveal_to_evaluator(
            freeze_id=StableId("missing.freeze"),
            evaluator_source_ids=(StableId("source.future.private"),),
            evaluator_context_destroyed=True,
        )
    record(builder, 1)
    checkpoint(builder, 1)
    with pytest.raises(ScenarioStateError, match="already frozen"):
        checkpoint(builder, 1)
    freeze = builder.freeze_context(case_id=StableId("case.1"), context_artifact=artifact())
    with pytest.raises(ScenarioStateError, match="already frozen"):
        builder.freeze_context(case_id=StableId("case.1"), context_artifact=artifact())
    with pytest.raises(ScenarioStateError, match="at least one private"):
        builder.reveal_to_evaluator(
            freeze_id=freeze.freeze_id,
            evaluator_source_ids=(),
            evaluator_context_destroyed=True,
        )
    with pytest.raises(ScenarioStateError, match="evaluator-only"):
        builder.reveal_to_evaluator(
            freeze_id=freeze.freeze_id,
            evaluator_source_ids=(StableId("source.brief"),),
            evaluator_context_destroyed=True,
        )
    with pytest.raises(ScenarioStateError, match="must be destroyed"):
        builder.reveal_to_evaluator(
            freeze_id=freeze.freeze_id,
            evaluator_source_ids=(StableId("source.future.private"),),
            evaluator_context_destroyed=False,
        )
    builder.reveal_to_evaluator(
        freeze_id=freeze.freeze_id,
        evaluator_source_ids=(StableId("source.future.private"),),
        evaluator_context_destroyed=True,
    )
    with pytest.raises(ScenarioStateError, match="already revealed"):
        builder.reveal_to_evaluator(
            freeze_id=freeze.freeze_id,
            evaluator_source_ids=(StableId("source.future.private"),),
            evaluator_context_destroyed=True,
        )


def test_scenario_rejects_checkpoint_at_an_undeclared_chapter() -> None:
    base = scenario()
    narrowed = base.model_copy(
        update={"profile": base.profile.model_copy(update={"checkpoint_chapters": (2,)})}
    )
    builder = ScenarioStateBuilder(narrowed, GENESIS)
    record(builder, 1)
    with pytest.raises(ScenarioStateError, match="not a declared"):
        checkpoint(builder, 1)


def test_scenario_reports_incomplete_lifecycle_and_contracts_reject_invalid_shapes() -> None:
    builder = ScenarioStateBuilder(scenario(), GENESIS)
    record(builder, 1)
    partial = builder.result()
    assert partial.completed is False
    assert len(partial.blockers) == 2

    with pytest.raises(ValidationError, match="ascending"):
        scenario().profile.model_copy(update={"checkpoint_chapters": (2, 1)}).model_validate(
            scenario().profile.model_dump() | {"checkpoint_chapters": (2, 1)}
        )
    with pytest.raises(ValidationError, match="positive"):
        BenchmarkScenarioProfile.model_validate(
            scenario().profile.model_dump() | {"checkpoint_chapters": (0,)}
        )
    base = scenario()
    with pytest.raises(ValidationError, match="source ids must be unique"):
        BenchmarkScenario.model_validate(
            base.model_dump() | {"sources": (*base.sources, base.sources[0])}
        )
    with pytest.raises(ValidationError, match="classification ids must be unique"):
        BenchmarkScenario.model_validate(
            base.model_dump()
            | {"classifications": (*base.classifications, base.classifications[0])}
        )
    with pytest.raises(ValidationError, match="exactly one classification"):
        BenchmarkScenario.model_validate(
            base.model_dump() | {"classifications": base.classifications[:-1]}
        )
    with pytest.raises(ValidationError, match="classes must match"):
        wrong = base.classifications[0].model_copy(
            update={"source_class": SourceClass.BASELINE_SETTING}
        )
        BenchmarkScenario.model_validate(
            base.model_dump() | {"classifications": (wrong, *base.classifications[1:])}
        )
    duplicate_chapter = source("source.chapter.duplicate", SourceClass.CHAPTER_TEXT, chapter=1)
    with pytest.raises(ValidationError, match="unique chapter indexes"):
        BenchmarkScenario.model_validate(
            base.model_dump()
            | {
                "sources": (*base.sources, duplicate_chapter),
                "classifications": (
                    *base.classifications,
                    classification(duplicate_chapter),
                ),
            }
        )
    with pytest.raises(ValidationError, match="completed scenario"):
        ScenarioRunResult(
            scenario_id=base.scenario_id,
            project_id=base.project_id,
            build_mode=base.profile.build_mode,
            chapter_receipts=(),
            checkpoints=(),
            completed=True,
            blockers=("blocked",),
        )
    with pytest.raises(ValidationError, match="recorded context freeze"):
        ScenarioRunResult(
            scenario_id=base.scenario_id,
            project_id=base.project_id,
            build_mode=base.profile.build_mode,
            chapter_receipts=(),
            checkpoints=(),
            evaluator_reveals=(
                EvaluatorRevealReceipt(
                    reveal_id=StableId("reveal.orphan"),
                    freeze_id=StableId("freeze.orphan"),
                    evaluator_source_ids=(StableId("source.future.private"),),
                    evaluator_context_destroyed=True,
                    completed_at=NOW,
                ),
            ),
            completed=False,
        )
    complete_builder = ScenarioStateBuilder(base, GENESIS, clock=lambda: NOW)
    record(complete_builder, 1)
    checkpoint(complete_builder, 1)
    freeze = complete_builder.freeze_context(
        case_id=StableId("case.1"), context_artifact=artifact()
    )
    with pytest.raises(ValidationError, match="freeze ids must be unique"):
        ScenarioRunResult(
            scenario_id=base.scenario_id,
            project_id=base.project_id,
            build_mode=base.profile.build_mode,
            chapter_receipts=(),
            checkpoints=(),
            freezes=(freeze, freeze),
            completed=False,
        )
