"""Stage 1B teacher-forced continuous commit replay."""

from __future__ import annotations

from datetime import UTC, datetime

from novel_agent.domain.artifacts import RootManifest
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import (
    CommitRequest,
    CommitStatus,
    ExtractionRule,
    ObservedChangeSet,
    ValidationReport,
    ValidationStatus,
    WorldRecordKind,
)
from novel_agent.domain.ids import CommitId, ProjectId, RunId, StableId
from novel_agent.domain.memory import (
    DerivedBuildStatus,
    DerivedSnapshotLite,
    FreshnessMode,
    FreshnessRequest,
    FreshnessStatus,
    WorldRootDocument,
)
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.replay import (
    ContinuousReplayResult,
    ReplayChapterResult,
    ReplayChapterStatus,
    ReplayMaterializedRecord,
)
from novel_agent.services.commits import CommitService
from novel_agent.services.curation import Stage1Curator
from novel_agent.services.model_curation import ModelCurator
from novel_agent.services.model_validation import ModelAssistedValidator
from novel_agent.services.overlay import WorldOverlay, build_candidate_bundle
from novel_agent.services.projection import (
    DerivedProjectionService,
    DerivedSnapshotRepository,
    FreshnessGate,
    ProjectionBuilder,
    snapshot_id_for_commit,
)
from novel_agent.services.validation import Stage1Validator


class ExactReplayProjectionBuilder(ProjectionBuilder):
    """Deterministic projection builder used by non-model replay harnesses."""

    def build(self, project_id: ProjectId, source_commit: CommitId) -> DerivedSnapshotLite:
        profile = project_id.root.replace(":", ".")
        return DerivedSnapshotLite(
            snapshot_id=snapshot_id_for_commit(source_commit),
            source_commit=source_commit,
            anchor_build_id=StableId(f"anchor.{source_commit.root.removeprefix('sha256:')[:24]}"),
            anchor_index_version=f"anchor-v1-{profile}",
            grounded_index_version=f"grounded-v1-{profile}",
            embedding_profile="deterministic-test-embedding-v1",
            fusion_profile="rrf-v1",
            build_status=DerivedBuildStatus.EXACT,
            retrieval_backend_profile="scripted_smoke",
            published_at=datetime.now(UTC),
        )


class ContinuousReplayRunner:
    def __init__(
        self,
        *,
        commit_service: CommitService,
        projection_service: DerivedProjectionService,
        snapshot_repository: DerivedSnapshotRepository,
    ) -> None:
        self._commits = commit_service
        self._projections = projection_service
        self._snapshots = snapshot_repository

    def run(
        self,
        *,
        replay_id: StableId,
        project_id: ProjectId,
        run_id: RunId,
        initial_manifest: RootManifest,
        initial_world: WorldRootDocument,
        chapters: TextRootDocument,
        rules_by_chapter: dict[int, tuple[ExtractionRule, ...]],
    ) -> ContinuousReplayResult:
        current_commit = self._commits.current_commit(project_id)
        current_manifest = initial_manifest
        current_world = initial_world
        results: list[ReplayChapterResult] = []
        for chapter_index in sorted(rules_by_chapter):
            changes = Stage1Curator().extract(
                chapters,
                chapter_index,
                current_commit,
                rules_by_chapter[chapter_index],
            )
            chapter_result, current_manifest, current_world, current_commit = self._process_chapter(
                replay_id=replay_id,
                project_id=project_id,
                run_id=run_id,
                chapter_index=chapter_index,
                current_manifest=current_manifest,
                current_world=current_world,
                current_commit=current_commit,
                chapters=chapters,
                changes=changes,
            )
            results.append(chapter_result)
            if chapter_result.status is not ReplayChapterStatus.COMMITTED:
                break
        return self._finish(replay_id, project_id, results)

    async def run_model_assisted(
        self,
        *,
        replay_id: StableId,
        project_id: ProjectId,
        run_id: RunId,
        initial_manifest: RootManifest,
        initial_world: WorldRootDocument,
        chapters: TextRootDocument,
        chapter_indexes: tuple[int, ...],
        curator: ModelCurator,
        requests_by_chapter: dict[int, ModelRequest],
        validator: ModelAssistedValidator | None = None,
        validation_requests_by_chapter: dict[int, ModelRequest] | None = None,
    ) -> ContinuousReplayResult:
        if len(chapter_indexes) != len(set(chapter_indexes)):
            raise ValueError("model replay chapter indexes must be unique")
        if validator is None and validation_requests_by_chapter:
            raise ValueError("model validation requests require a model validator")
        current_commit = self._commits.current_commit(project_id)
        current_manifest = initial_manifest
        current_world = initial_world
        results: list[ReplayChapterResult] = []
        model_calls: list[ModelCallRecord] = []
        for chapter_index in sorted(chapter_indexes):
            request = requests_by_chapter.get(chapter_index)
            if request is None:
                raise ValueError(f"model replay request is missing for chapter {chapter_index}")
            changes, call = await curator.extract(
                chapters,
                chapter_index,
                current_commit,
                current_world,
                request,
            )
            model_calls.append(call)
            validation_override = None
            if validator is not None:
                validation_request = (validation_requests_by_chapter or {}).get(chapter_index)
                if validation_request is None:
                    raise ValueError(
                        f"model validation request is missing for chapter {chapter_index}"
                    )
                proposed_world = WorldOverlay().apply(
                    current_world, changes, canonical_commit=current_commit
                )
                candidate = build_candidate_bundle(
                    project_id=project_id,
                    run_id=run_id,
                    current_manifest=current_manifest,
                    changes=changes,
                    proposed_world=proposed_world,
                )
                validation_override, validation_call = await validator.validate(
                    candidate,
                    current_world,
                    proposed_world,
                    chapters,
                    validation_request,
                    canonical_commit=current_commit,
                )
                if validation_call is not None:
                    model_calls.append(validation_call)
            chapter_result, current_manifest, current_world, current_commit = self._process_chapter(
                replay_id=replay_id,
                project_id=project_id,
                run_id=run_id,
                chapter_index=chapter_index,
                current_manifest=current_manifest,
                current_world=current_world,
                current_commit=current_commit,
                chapters=chapters,
                changes=changes,
                validation_override=validation_override,
            )
            results.append(chapter_result)
            if chapter_result.status is not ReplayChapterStatus.COMMITTED:
                break
        return self._finish(replay_id, project_id, results, tuple(model_calls))

    def _process_chapter(
        self,
        *,
        replay_id: StableId,
        project_id: ProjectId,
        run_id: RunId,
        chapter_index: int,
        current_manifest: RootManifest,
        current_world: WorldRootDocument,
        current_commit: CommitId,
        chapters: TextRootDocument,
        changes: ObservedChangeSet,
        validation_override: ValidationReport | None = None,
    ) -> tuple[ReplayChapterResult, RootManifest, WorldRootDocument, CommitId]:
        proposed_world = WorldOverlay().apply(
            current_world, changes, canonical_commit=current_commit
        )
        candidate = build_candidate_bundle(
            project_id=project_id,
            run_id=run_id,
            current_manifest=current_manifest,
            changes=changes,
            proposed_world=proposed_world,
        )
        validation = validation_override or Stage1Validator().validate(
            candidate,
            current_world,
            proposed_world,
            chapters,
            canonical_commit=current_commit,
        )
        if validation.status is not ValidationStatus.PASSED:
            return (
                ReplayChapterResult(
                    chapter_index=chapter_index,
                    base_commit=current_commit,
                    status=ReplayChapterStatus.BLOCKED_BY_VALIDATION,
                    validation_report=validation,
                    observed_changes=changes,
                ),
                current_manifest,
                current_world,
                current_commit,
            )
        commit_result = self._commits.commit(
            CommitRequest(
                request_id=StableId(f"request.{replay_id.root}.{chapter_index}"),
                project_id=project_id,
                base_commit=current_commit,
                idempotency_key=StableId(f"replay.{replay_id.root}.{chapter_index}"),
                bundle=candidate,
                validation_report=validation,
            )
        )
        if commit_result.status is not CommitStatus.ACCEPTED or commit_result.commit_id is None:
            raise RuntimeError("validated replay commit was not accepted")
        self._projections.process_all()
        new_commit = commit_result.commit_id
        snapshot = self._snapshots.get_for_commit(new_commit)
        required_snapshot_id = snapshot_id_for_commit(new_commit)
        freshness = FreshnessGate.evaluate(
            FreshnessRequest(
                canonical_commit=new_commit,
                r1_basis_commit=new_commit,
                required_snapshot_id=required_snapshot_id,
                actual_alias_commit=None if snapshot is None else snapshot.source_commit,
                actual_snapshot=snapshot,
                mode=FreshnessMode.BLOCK_ON_MISMATCH,
            )
        )
        status = (
            ReplayChapterStatus.COMMITTED
            if freshness.status is FreshnessStatus.READY
            else ReplayChapterStatus.BLOCKED_BY_FRESHNESS
        )
        return (
            ReplayChapterResult(
                chapter_index=chapter_index,
                base_commit=current_commit,
                status=status,
                validation_report=validation,
                observed_changes=changes,
                commit_id=new_commit,
                snapshot_id=required_snapshot_id,
                freshness=freshness,
                materialized_records=self._materialized_records(proposed_world),
            ),
            commit_result.manifest or current_manifest,
            proposed_world,
            new_commit,
        )

    @staticmethod
    def _finish(
        replay_id: StableId,
        project_id: ProjectId,
        results: list[ReplayChapterResult],
        model_calls: tuple[ModelCallRecord, ...] = (),
    ) -> ContinuousReplayResult:
        committed = sum(item.status is ReplayChapterStatus.COMMITTED for item in results)
        blocked = len(results) - committed
        return ContinuousReplayResult(
            replay_id=replay_id,
            project_id=project_id,
            chapter_results=tuple(results),
            committed_chapters=committed,
            blocked_chapters=blocked,
            silent_canonical_pollution_count=0,
            silent_stale_snapshot_reads=0,
            model_calls=model_calls,
        )

    @staticmethod
    def _materialized_records(
        world: WorldRootDocument,
    ) -> tuple[ReplayMaterializedRecord, ...]:
        records = [
            *(
                ReplayMaterializedRecord(
                    record_kind=WorldRecordKind.ENTITY,
                    target_id=value.entity_id,
                    record=value.model_dump(mode="json"),
                )
                for value in world.entities
            ),
            *(
                ReplayMaterializedRecord(
                    record_kind=WorldRecordKind.EVENT,
                    target_id=value.event_id,
                    record=value.model_dump(mode="json"),
                )
                for value in world.events
            ),
            *(
                ReplayMaterializedRecord(
                    record_kind=WorldRecordKind.STATE,
                    target_id=value.state_id,
                    record=value.model_dump(mode="json"),
                )
                for value in world.states
            ),
            *(
                ReplayMaterializedRecord(
                    record_kind=WorldRecordKind.RELATION,
                    target_id=value.relation_id,
                    record=value.model_dump(mode="json"),
                )
                for value in world.relations
            ),
            *(
                ReplayMaterializedRecord(
                    record_kind=WorldRecordKind.OBLIGATION,
                    target_id=value.obligation_id,
                    record=value.model_dump(mode="json"),
                )
                for value in world.obligations
            ),
        ]
        return tuple(
            sorted(
                records,
                key=lambda record: (record.record_kind.value, record.target_id.root),
            )
        )
