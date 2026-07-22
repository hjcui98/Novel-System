from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import CommitRow, DerivedSnapshotRow, ProjectionOutboxRow
from novel_agent.domain.changes import (
    ChangeOperationType,
    CommitResult,
    CommitStatus,
    CuratorEventRecord,
    ExtractionRule,
    ModelValidationDraft,
    ObservedChangeSet,
    ValidationReport,
    WorldRecordKind,
)
from novel_agent.domain.ids import ProjectId, RunId, StableId
from novel_agent.domain.replay import ReplayChapterResult, ReplayChapterStatus
from novel_agent.domain.world import NarrativeOrder, TruthClass
from novel_agent.services.commits import CommitService
from novel_agent.services.curation import Stage1Curator
from novel_agent.services.model_curation import ModelCurator
from novel_agent.services.model_validation import ModelAssistedValidator
from novel_agent.services.projection import (
    DerivedProjectionService,
    DerivedSnapshotRepository,
    ProjectionOutboxRepository,
)
from novel_agent.services.replay import (
    ContinuousReplayRunner,
    ExactReplayProjectionBuilder,
)
from novel_agent.services.replay_evaluation import ReplayEvaluator
from tests.factories import make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.unit.test_model_curation import _draft, _gateway, _request
from tests.unit.test_model_validation import (
    _finding as _validation_finding,
)
from tests.unit.test_model_validation import (
    _gateway as _validation_gateway,
)


@pytest.fixture
def replay_database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine, build_session_factory(engine)
    engine.dispose()


def _rules() -> dict[int, tuple[ExtractionRule, ...]]:
    entity = "entity.synthetic.lin-che"
    return {
        21: (
            ExtractionRule(
                rule_id=StableId("rule.reaffirm.promise"),
                phrase="重申旧誓言",
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.EVENT,
                target_id=StableId("event.synthetic.promise-reaffirmed"),
                record={
                    "event_id": "event.synthetic.promise-reaffirmed",
                    "event_type": "promise_reaffirmed",
                    "participant_ids": [entity],
                    "effect_refs": [],
                    "evidence_refs": [],
                    "truth_class": "accepted_world_fact",
                },
            ),
        ),
        22: (
            ExtractionRule(
                rule_id=StableId("rule.injury.persists"),
                phrase="受伤仍未痊愈",
                operation=ChangeOperationType.REPLACE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.synthetic.injury"),
                record={
                    "state_id": "state.synthetic.injury",
                    "subject_id": entity,
                    "predicate": "injury",
                    "value": "not_healed",
                    "valid_time": {"worldline": "main", "start_ordinal": 22},
                    "evidence_refs": [],
                    "truth_class": "accepted_world_fact",
                },
            ),
        ),
        23: (
            ExtractionRule(
                rule_id=StableId("rule.resolve.north-tower"),
                phrase="进入北塔",
                operation=ChangeOperationType.REPLACE,
                record_kind=WorldRecordKind.OBLIGATION,
                target_id=StableId("obligation.synthetic.north-tower"),
                record={
                    "obligation_id": "obligation.synthetic.north-tower",
                    "kind": "objective",
                    "description": "林澈需要进入北塔。",
                    "status": "resolved",
                    "owner_ids": [entity],
                    "due_chapter": 23,
                    "evidence_refs": [],
                },
            ),
        ),
    }


def _runner(
    factory: sessionmaker[Session],
    *,
    commits: CommitService | None = None,
    projections: DerivedProjectionService | None = None,
) -> tuple[ContinuousReplayRunner, CommitService]:
    commit_service = commits or CommitService(factory)
    outbox = ProjectionOutboxRepository(factory)
    projection_service = projections or DerivedProjectionService(
        outbox, ExactReplayProjectionBuilder()
    )
    return (
        ContinuousReplayRunner(
            commit_service=commit_service,
            projection_service=projection_service,
            snapshot_repository=DerivedSnapshotRepository(factory),
        ),
        commit_service,
    )


def _inputs(commit_service: CommitService):  # type: ignore[no-untyped-def]
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    future = next(root for root in bundle.text_roots if len(root.chapters) == 3)
    manifest = make_manifest().model_copy(update={"project_id": ProjectId("project.synthetic")})
    commit_service.initialize_project(manifest)
    return manifest, world, future


def test_synthetic_21_to_23_replay_commits_with_exact_freshness(
    replay_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = replay_database
    runner, commits = _runner(factory)
    manifest, world, future = _inputs(commits)

    result = runner.run(
        replay_id=StableId("replay.synthetic.21-23"),
        project_id=ProjectId("project.synthetic"),
        run_id=RunId("run.synthetic.replay"),
        initial_manifest=manifest,
        initial_world=world,
        chapters=future,
        rules_by_chapter=_rules(),
    )

    assert result.committed_chapters == 3
    assert result.blocked_chapters == 0
    assert result.silent_canonical_pollution_count == 0
    assert result.silent_stale_snapshot_reads == 0
    assert [item.status for item in result.chapter_results] == [
        ReplayChapterStatus.COMMITTED,
        ReplayChapterStatus.COMMITTED,
        ReplayChapterStatus.COMMITTED,
    ]
    assert all(item.freshness is not None for item in result.chapter_results)
    metrics = ReplayEvaluator().evaluate(make_synthetic_bundle().replay_manifests[0], result)
    assert metrics.state_delta_f1 == 1.0
    assert metrics.event_extraction_f1 == 1.0
    assert metrics.plan_obligation_update_f1 == 1.0
    assert metrics.evidence_binding_accuracy == 1.0
    assert metrics.false_world_fact_promotion_rate == 0.0
    assert metrics.invalid_state_overwrite_rate == 0.0
    assert metrics.current_state_accuracy_by_chapter == {22: 1.0, 23: 1.0}
    assert metrics.cumulative_state_drift == (0.0, 0.0)
    assert metrics.wrong_vital_or_injury_state_count == 0
    assert metrics.wrong_obligation_debt_count == 0
    assert metrics.orphan_evidence_ref_count == 0
    assert metrics.manual_repair_commit_count == 0
    assert all(item.materialized_records for item in result.chapter_results)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CommitRow)) == 4
        assert session.scalar(select(func.count()).select_from(ProjectionOutboxRow)) == 4
        assert session.scalar(select(func.count()).select_from(DerivedSnapshotRow)) == 4
        assert {row.status for row in session.scalars(select(ProjectionOutboxRow)).all()} == {
            "completed"
        }


class _NoopProjectionService(DerivedProjectionService):
    def process_all(self, *, max_items: int = 1000) -> int:
        return 0


def test_replay_blocks_explicitly_when_snapshot_is_stale_or_missing(
    replay_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = replay_database
    outbox = ProjectionOutboxRepository(factory)
    commits = CommitService(factory)
    projections = _NoopProjectionService(outbox, ExactReplayProjectionBuilder())
    runner, _ = _runner(factory, commits=commits, projections=projections)
    manifest, world, future = _inputs(commits)

    result = runner.run(
        replay_id=StableId("replay.blocked.freshness"),
        project_id=ProjectId("project.synthetic"),
        run_id=RunId("run.blocked.freshness"),
        initial_manifest=manifest,
        initial_world=world,
        chapters=future,
        rules_by_chapter={21: _rules()[21]},
    )

    chapter = result.chapter_results[0]
    assert chapter.status is ReplayChapterStatus.BLOCKED_BY_FRESHNESS
    assert chapter.commit_id is not None
    assert result.committed_chapters == 0 and result.blocked_chapters == 1


def test_replay_blocks_invalid_truth_promotion_before_commit(
    replay_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = replay_database
    runner, commits = _runner(factory)
    manifest, world, future = _inputs(commits)
    rule = _rules()[21][0].model_copy(update={"phrase": "林澈"})
    record = dict(rule.record)
    record["truth_class"] = "accepted_world_fact"
    rule = rule.model_copy(update={"record": record})
    marker_block = (
        future.chapters[0].scenes[0].blocks[0].model_copy(update={"text": "据说林澈重申旧誓言。"})
    )
    scene = future.chapters[0].scenes[0].model_copy(update={"blocks": (marker_block,)})
    chapter = future.chapters[0].model_copy(update={"scenes": (scene,)})
    from novel_agent.domain.ids import ArtifactId
    from novel_agent.services.benchmark_importer import text_root_content_id

    provisional = future.model_copy(
        update={"root_hash": ArtifactId("sha256:" + "0" * 64), "chapters": (chapter,)}
    )
    root = provisional.model_copy(update={"root_hash": text_root_content_id(provisional)})
    promoted = rule.model_copy(update={"phrase": "据说林澈"})

    result = runner.run(
        replay_id=StableId("replay.blocked.validation"),
        project_id=ProjectId("project.synthetic"),
        run_id=RunId("run.blocked.validation"),
        initial_manifest=manifest,
        initial_world=world,
        chapters=root,
        rules_by_chapter={21: (promoted,)},
    )
    assert result.chapter_results[0].status is ReplayChapterStatus.BLOCKED_BY_VALIDATION
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CommitRow)) == 1


class _RejectingCommitService(CommitService):
    def commit(self, request):  # type: ignore[no-untyped-def]
        return CommitResult(
            request_id=request.request_id,
            status=CommitStatus.CONFLICTED,
            reason="forced conflict",
        )


def test_replay_surfaces_unexpected_commit_conflict(
    replay_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = replay_database
    commits = _RejectingCommitService(factory)
    runner, _ = _runner(factory, commits=commits)
    manifest, world, future = _inputs(commits)
    with pytest.raises(RuntimeError, match="not accepted"):
        runner.run(
            replay_id=StableId("replay.commit.conflict"),
            project_id=ProjectId("project.synthetic"),
            run_id=RunId("run.commit.conflict"),
            initial_manifest=manifest,
            initial_world=world,
            chapters=future,
            rules_by_chapter={21: _rules()[21]},
        )


def test_replay_result_contract_and_projection_batch_limit(
    replay_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = replay_database
    outbox = ProjectionOutboxRepository(factory)
    projections = DerivedProjectionService(outbox, ExactReplayProjectionBuilder())
    with pytest.raises(ValueError, match="positive"):
        projections.process_all(max_items=0)
    with pytest.raises(ValidationError, match="post-commit"):
        ReplayChapterResult(
            chapter_index=1,
            base_commit=make_synthetic_bundle().world_roots[0].source_commit,
            status=ReplayChapterStatus.COMMITTED,
            validation_report=_dummy_report(),
            observed_changes=_dummy_changes(),
        )
    with pytest.raises(ValidationError, match="validation-blocked"):
        ReplayChapterResult(
            chapter_index=1,
            base_commit=make_synthetic_bundle().world_roots[0].source_commit,
            status=ReplayChapterStatus.BLOCKED_BY_VALIDATION,
            validation_report=_dummy_report(),
            observed_changes=_dummy_changes(),
            commit_id=make_synthetic_bundle().world_roots[0].source_commit,
        )

    world = make_synthetic_bundle().world_roots[0]
    malformed = _dummy_changes().operations
    operation = _rules()[21][0]
    curated = (
        Stage1Curator()
        .extract(
            next(root for root in make_synthetic_bundle().text_roots if len(root.chapters) == 3),
            21,
            world.source_commit,
            (operation,),
        )
        .operations[0]
    )
    assert malformed == ()
    assert (
        ReplayEvaluator._operation_key(21, curated.model_copy(update={"payload": "invalid"}))
        is None
    )
    assert (
        ReplayEvaluator._operation_key(
            21, curated.model_copy(update={"payload": {"record_type": 7}})
        )
        is None
    )
    assert ReplayEvaluator._truth(curated.model_copy(update={"payload": "invalid"})) is None


def test_replay_evaluator_rejects_correct_target_with_wrong_record_value(
    replay_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = replay_database
    runner, commits = _runner(factory)
    manifest, world, future = _inputs(commits)
    wrong_rule = _rules()[22][0]
    wrong_record = dict(wrong_rule.record)
    wrong_record["value"] = "fully_healed"
    wrong_rule = wrong_rule.model_copy(update={"record": wrong_record})

    result = runner.run(
        replay_id=StableId("replay.synthetic.wrong-value"),
        project_id=ProjectId("project.synthetic"),
        run_id=RunId("run.synthetic.wrong-value"),
        initial_manifest=manifest,
        initial_world=world,
        chapters=future,
        rules_by_chapter={22: (wrong_rule,)},
    )

    gold = (
        make_synthetic_bundle()
        .replay_manifests[0]
        .model_copy(
            update={"gold_changes": (make_synthetic_bundle().replay_manifests[0].gold_changes[1],)}
        )
    )
    metrics = ReplayEvaluator().evaluate(gold, result)
    assert metrics.state_delta_precision == 0.0
    assert metrics.state_delta_recall == 0.0
    assert metrics.state_delta_f1 == 0.0
    assert metrics.missed_critical_change_rate == 1.0
    assert metrics.false_world_fact_promotion_rate == 1.0
    assert metrics.invalid_state_overwrite_rate == 1.0
    assert metrics.current_state_accuracy_by_chapter == {22: 0.0, 23: 0.0}
    assert metrics.cumulative_state_drift == (1.0, 1.0)
    assert metrics.wrong_vital_or_injury_state_count == 2
    assert metrics.wrong_obligation_debt_count == 1


def test_replay_record_match_supports_nested_annotation_subsets() -> None:
    assert ReplayEvaluator._contains_expected(
        {"outer": {"value": 7, "ignored": True}, "items": [1, 2]},
        {"outer": {"value": 7}, "items": [1, 2]},
    )
    assert not ReplayEvaluator._contains_expected(
        {"outer": {"value": 8}, "items": [1]},
        {"outer": {"value": 7}, "items": [1, 2]},
    )
    bundle = make_synthetic_bundle()
    gold = bundle.replay_manifests[0].gold_changes[0]
    world = bundle.world_roots[0]
    future = next(root for root in bundle.text_roots if len(root.chapters) == 3)
    operation = (
        Stage1Curator().extract(future, 21, world.source_commit, (_rules()[21][0],)).operations[0]
    )
    assert not ReplayEvaluator._record_matches(
        operation.model_copy(update={"payload": "invalid"}), gold
    )
    assert not ReplayEvaluator._record_matches(
        operation.model_copy(update={"payload": {"record": "invalid"}}), gold
    )


def test_replay_long_horizon_ledger_tracks_repairs_orphans_and_pollution_depth(
    replay_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = replay_database
    runner, commits = _runner(factory)
    manifest, world, future = _inputs(commits)
    result = runner.run(
        replay_id=StableId("replay.synthetic.ledger"),
        project_id=ProjectId("project.synthetic"),
        run_id=RunId("run.synthetic.ledger"),
        initial_manifest=manifest,
        initial_world=world,
        chapters=future,
        rules_by_chapter=_rules(),
    )
    first = result.chapter_results[0]
    operation = first.observed_changes.operations[0]
    spanless = operation.evidence_refs[0].model_copy(update={"span": None})
    changed_operation = operation.model_copy(update={"evidence_refs": (spanless,)})
    changed_first = first.model_copy(
        update={
            "manual_repair": True,
            "observed_changes": first.observed_changes.model_copy(
                update={"operations": (changed_operation,)}
            ),
        }
    )
    ledger_result = result.model_copy(
        update={
            "chapter_results": (changed_first, *result.chapter_results[1:]),
            "silent_canonical_pollution_count": 1,
            "first_pollution_chapter": 22,
        }
    )
    replay_manifest = make_synthetic_bundle().replay_manifests[0]
    metrics = ReplayEvaluator().evaluate(replay_manifest, ledger_result)
    assert metrics.orphan_evidence_ref_count == 1
    assert metrics.manual_repair_commit_count == 1
    assert metrics.first_pollution_chapter == 22
    assert metrics.pollution_propagation_depth == 2

    no_checkpoints = replay_manifest.model_copy(update={"state_checkpoints": ()})
    metrics = ReplayEvaluator().evaluate(no_checkpoints, result)
    assert metrics.current_state_accuracy_by_chapter == {}
    assert metrics.cumulative_state_drift == ()
    assert metrics.wrong_item_ownership_count is None

    future_pollution = result.model_copy(update={"first_pollution_chapter": 99})
    metrics = ReplayEvaluator().evaluate(replay_manifest, future_pollution)
    assert metrics.pollution_propagation_depth is None


def test_model_assisted_replay_reuses_commit_projection_and_freshness_pipeline(
    replay_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = replay_database
    runner, commits = _runner(factory)
    manifest, world, future = _inputs(commits)
    gateway, _ = _gateway(_draft())
    validator = ModelAssistedValidator(_validation_gateway(ModelValidationDraft())[0])
    result = asyncio.run(
        runner.run_model_assisted(
            replay_id=StableId("replay.model.23"),
            project_id=ProjectId("project.synthetic"),
            run_id=RunId("run.model.23"),
            initial_manifest=manifest,
            initial_world=world,
            chapters=future,
            chapter_indexes=(23,),
            curator=ModelCurator(gateway),
            requests_by_chapter={23: _request()},
            validator=validator,
            validation_requests_by_chapter={23: _request()},
        )
    )
    assert result.committed_chapters == 1
    assert result.blocked_chapters == 0
    assert len(result.model_calls) == 2
    assert result.model_calls[0].request_id == _request().request_id


def test_model_assisted_replay_rejects_duplicate_chapters_missing_requests_and_stale_projection(
    replay_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = replay_database
    runner, commits = _runner(factory)
    manifest, world, future = _inputs(commits)
    curator = ModelCurator(_gateway(_draft())[0])
    with pytest.raises(ValueError, match="unique"):
        asyncio.run(
            runner.run_model_assisted(
                replay_id=StableId("replay.model.invalid"),
                project_id=ProjectId("project.synthetic"),
                run_id=RunId("run.model.invalid"),
                initial_manifest=manifest,
                initial_world=world,
                chapters=future,
                chapter_indexes=(23, 23),
                curator=curator,
                requests_by_chapter={23: _request()},
            )
        )
    with pytest.raises(ValueError, match="missing"):
        asyncio.run(
            runner.run_model_assisted(
                replay_id=StableId("replay.model.invalid"),
                project_id=ProjectId("project.synthetic"),
                run_id=RunId("run.model.invalid"),
                initial_manifest=manifest,
                initial_world=world,
                chapters=future,
                chapter_indexes=(23,),
                curator=curator,
                requests_by_chapter={},
            )
        )

    with pytest.raises(ValueError, match="require a model validator"):
        asyncio.run(
            runner.run_model_assisted(
                replay_id=StableId("replay.model.invalid-validator"),
                project_id=ProjectId("project.synthetic"),
                run_id=RunId("run.model.invalid-validator"),
                initial_manifest=manifest,
                initial_world=world,
                chapters=future,
                chapter_indexes=(23,),
                curator=curator,
                requests_by_chapter={23: _request()},
                validation_requests_by_chapter={23: _request()},
            )
        )
    validator = ModelAssistedValidator(_validation_gateway(ModelValidationDraft())[0])
    with pytest.raises(ValueError, match="validation request is missing"):
        asyncio.run(
            runner.run_model_assisted(
                replay_id=StableId("replay.model.missing-validator-request"),
                project_id=ProjectId("project.synthetic"),
                run_id=RunId("run.model.missing-validator-request"),
                initial_manifest=manifest,
                initial_world=world,
                chapters=future,
                chapter_indexes=(23,),
                curator=curator,
                requests_by_chapter={23: _request()},
                validator=validator,
            )
        )

    base_commit = commits.current_commit(ProjectId("project.synthetic"))
    preview_changes, _ = asyncio.run(
        ModelCurator(_gateway(_draft())[0]).extract(
            future,
            23,
            base_commit,
            world,
            _request(),
        )
    )
    model_evidence = preview_changes.operations[0].evidence_refs[0]
    warning = _validation_finding().model_copy(update={"evidence_refs": (model_evidence,)})
    warning_validator = ModelAssistedValidator(
        _validation_gateway(ModelValidationDraft(findings=(warning,)))[0]
    )
    review = asyncio.run(
        runner.run_model_assisted(
            replay_id=StableId("replay.model.review"),
            project_id=ProjectId("project.synthetic"),
            run_id=RunId("run.model.review"),
            initial_manifest=manifest,
            initial_world=world,
            chapters=future,
            chapter_indexes=(23,),
            curator=ModelCurator(_gateway(_draft())[0]),
            requests_by_chapter={23: _request()},
            validator=warning_validator,
            validation_requests_by_chapter={23: _request()},
        )
    )
    assert review.chapter_results[0].status is ReplayChapterStatus.BLOCKED_BY_VALIDATION
    assert len(review.model_calls) == 2

    bad_operation = (
        _draft()
        .operations[0]
        .model_copy(
            update={
                "operation": ChangeOperationType.CREATE,
                "record_kind": WorldRecordKind.EVENT,
                "target_id": StableId("event.synthetic.bad-order"),
                "record": CuratorEventRecord(
                    event_type="bad_order",
                    participant_ids=(StableId("entity.synthetic.lin-che"),),
                    narrative_order=NarrativeOrder(chapter_index=22),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
            }
        )
    )
    bad_draft = _draft().model_copy(update={"operations": (bad_operation,)})
    clean_validation_gateway, clean_validation_endpoint = _validation_gateway(
        ModelValidationDraft()
    )
    deterministic_block = asyncio.run(
        runner.run_model_assisted(
            replay_id=StableId("replay.model.deterministic-block"),
            project_id=ProjectId("project.synthetic"),
            run_id=RunId("run.model.deterministic-block"),
            initial_manifest=manifest,
            initial_world=world,
            chapters=future,
            chapter_indexes=(23,),
            curator=ModelCurator(_gateway(bad_draft)[0]),
            requests_by_chapter={23: _request()},
            validator=ModelAssistedValidator(clean_validation_gateway),
            validation_requests_by_chapter={23: _request()},
        )
    )
    assert (
        deterministic_block.chapter_results[0].status is ReplayChapterStatus.BLOCKED_BY_VALIDATION
    )
    assert len(deterministic_block.model_calls) == 1
    assert clean_validation_endpoint.requests == []

    outbox = ProjectionOutboxRepository(factory)
    stale_runner, _ = _runner(
        factory,
        commits=commits,
        projections=_NoopProjectionService(outbox, ExactReplayProjectionBuilder()),
    )
    stale = asyncio.run(
        stale_runner.run_model_assisted(
            replay_id=StableId("replay.model.stale"),
            project_id=ProjectId("project.synthetic"),
            run_id=RunId("run.model.stale"),
            initial_manifest=manifest,
            initial_world=world,
            chapters=future,
            chapter_indexes=(23,),
            curator=curator,
            requests_by_chapter={23: _request()},
        )
    )
    assert stale.blocked_chapters == 1
    assert stale.chapter_results[0].status is ReplayChapterStatus.BLOCKED_BY_FRESHNESS


def _dummy_report() -> ValidationReport:
    from datetime import UTC, datetime

    from novel_agent.domain.changes import ValidationStatus
    from novel_agent.domain.ids import SchemaVersion

    return ValidationReport(
        report_id=StableId("validation.dummy"),
        bundle_id=StableId("bundle.dummy"),
        status=ValidationStatus.PASSED,
        schema_version=SchemaVersion("0.1.0"),
        validated_at=datetime(2026, 7, 21, tzinfo=UTC),
    )


def _dummy_changes() -> ObservedChangeSet:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    future = next(root for root in bundle.text_roots if len(root.chapters) == 3)
    return Stage1Curator().extract(future, 21, world.source_commit, ())
