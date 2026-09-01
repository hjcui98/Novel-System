from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import pytest
import scripts.prepare_u6e_endurance_basis as preparation
from scripts.prepare_u6e_endurance_basis import (
    _expected_chapters,
    _preflight_target_database,
    _seed_planner_continuation,
    _select_reference_inputs,
    prepare,
)
from sqlalchemy import create_engine, text

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import ChapterDocument
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import NeedFacetKind
from novel_agent.domain.planning import (
    PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
    GoalProposal,
    PlanningInquiry,
    PlanningLoopCheckpoint,
    PlanningLoopPhase,
    PlanningProblemIdentitySeed,
    PlanningProvenance,
    PlanningQuestion,
    PlanningQuestionKind,
    PlanningReference,
    PlanReview,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    ContractRef,
    ExecutionStatus,
    ReferenceAsset,
    ReferenceRootDocument,
    SourceClass,
)
from novel_agent.runtime.production_bootstrap import (
    _default_stage4_policy,
    load_production_assembly_spec,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.projection import snapshot_id_for_commit
from tests.factories import make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def _database(path: Path) -> str:
    url = f"sqlite+pysqlite:///{path}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def test_expected_chapters_requires_exactly_fifty() -> None:
    assert _expected_chapters(40, 90) == tuple(range(41, 91))
    with pytest.raises(ValueError, match="exactly fifty"):
        _expected_chapters(40, 89)
    assert _expected_chapters(44, 45, canary=True) == (45,)
    with pytest.raises(ValueError, match="one canary chapter"):
        _expected_chapters(44, 46, canary=True)


def test_reference_input_mode_can_retain_the_frozen_full_intent_set() -> None:
    version = SchemaVersion("1.0.0")
    first = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "1" * 64),
        byte_length=1,
        media_type="text/plain",
        schema_version=version,
    )
    second = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "2" * 64),
        byte_length=1,
        media_type="text/plain",
        schema_version=version,
    )
    reference = ReferenceRootDocument(
        root_hash=ArtifactId("sha256:" + "3" * 64),
        schema_version=version,
        assets=(
            ReferenceAsset(
                asset_id=StableId("reference.author"),
                source_id=StableId("source.author"),
                source_class=SourceClass.AUTHOR_INITIAL_BRIEF,
                artifact=first,
            ),
            ReferenceAsset(
                asset_id=StableId("reference.setting"),
                source_id=StableId("source.setting"),
                source_class=SourceClass.BASELINE_SETTING,
                artifact=second,
            ),
        ),
    )

    assert _select_reference_inputs(reference, mode="author_initial_brief") == (first,)
    assert _select_reference_inputs(reference, mode="all") == (first, second)
    with pytest.raises(ValueError, match="unsupported reference input mode"):
        _select_reference_inputs(reference, mode="unknown")


def test_planner_continuation_rebases_accepted_inquiry_without_memory_state(
    tmp_path: Path,
) -> None:
    version = SchemaVersion("1.0.0")
    source_root = tmp_path / "source-objects"
    destination_root = tmp_path / "destination-objects"
    source_artifacts = ArtifactRepository(FilesystemObjectStore(source_root))
    destination_artifacts = ArtifactRepository(FilesystemObjectStore(destination_root))
    source_commit = CommitId("sha256:" + "1" * 64)
    destination_commit = CommitId("sha256:" + "2" * 64)
    input_ref = source_artifacts.put(b"author intent", "text/plain", version)
    goal = GoalProposal(
        goal_id=StableId("goal.seed"),
        summary="preserve the seed intent",
        rationale="regression",
        provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
    )
    inquiry = PlanningInquiry(
        inquiry_id=StableId("inquiry.source"),
        project_id=ProjectId("project.source"),
        mode=AgentMode.CHAPTER_SET,
        planning_scope=("chapters:41-41",),
        horizon_start=41,
        horizon_end=41,
        author_intent_refs=(input_ref,),
        goal_proposals=(goal,),
        questions=(
            PlanningQuestion(
                question_id=StableId("question.seed"),
                kind=PlanningQuestionKind.FACT,
                question="preserve the reviewed fact",
                provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
                goal_id=goal.goal_id,
                blocking=True,
            ),
        ),
        expected_output_shape="bounded PlanProposal",
    )
    inquiry_ref = source_artifacts.put(
        canonical_json_bytes(inquiry.model_dump(mode="json")),
        "application/vnd.novel-agent.planning-inquiry+json",
        version,
    )
    now = datetime.now(UTC)
    review_receipt = AgentExecutionReceipt(
        receipt_id=StableId("receipt.source"),
        run_id=RunId("run.source"),
        task_id=TaskId("task.source"),
        agent_spec=ContractRef(
            contract_id=StableId("agent.plan-reviewer.chapter-set"),
            version=version,
            content_hash=ArtifactId("sha256:" + "a" * 64),
        ),
        agent_type=AgentType.PLAN_REVIEWER,
        agent_mode=AgentMode.CHAPTER_SET,
        prompt_fingerprint=ArtifactId("sha256:" + "a" * 64),
        configuration_fingerprint=ArtifactId("sha256:" + "a" * 64),
        base_commit=source_commit,
        status=ExecutionStatus.SUCCEEDED,
        started_at=now,
        completed_at=now,
        latency_ms=1,
    )
    review = PlanReview(
        review_id=StableId("review.source"),
        target_kind=ReviewTargetKind.INQUIRY,
        target_artifact_ref=inquiry_ref,
        decision=ReviewDecision.ACCEPT,
        receipt=review_receipt,
    )
    review_ref = source_artifacts.put(
        canonical_json_bytes(review.model_dump(mode="json")),
        "application/vnd.novel-agent.plan-review+json",
        version,
    )
    policy = _default_stage4_policy(load_production_assembly_spec())
    checkpoint = PlanningLoopCheckpoint(
        checkpoint_id=StableId("planning-checkpoint.source"),
        request_id=StableId("planning-request.source"),
        phase=PlanningLoopPhase.INQUIRY_ACCEPTED,
        base_commit=source_commit,
        snapshot_id=snapshot_id_for_commit(source_commit),
        configuration_fingerprint=policy.configuration_fingerprint,
        inquiry_ref=inquiry_ref,
        inquiry_review_ref=review_ref,
    )
    checkpoint_ref = source_artifacts.put(
        canonical_json_bytes(checkpoint.model_dump(mode="json")),
        PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
        version,
    )
    source_text_root = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "3" * 64),
        byte_length=1,
        media_type="application/json",
        schema_version=version,
    )
    problem_identity_seed = PlanningProblemIdentitySeed(
        need_id=StableId("need.seed.reviewed-fact"),
        question_id=StableId("question.seed"),
        need_query="preserve the reviewed fact",
        semantic_question="the reviewed fact is preserved",
        facet=NeedFacetKind.CURRENT_STATE,
        source_commit=source_commit,
        source_text_root=source_text_root.artifact_id,
        cutoff_chapter=40,
    )
    checkpoint = checkpoint.model_copy(
        update={
            "problem_identity_seed": problem_identity_seed,
            "pending_planner_memory_questions": (problem_identity_seed.need_query,),
        }
    )
    checkpoint_ref = source_artifacts.put(
        canonical_json_bytes(checkpoint.model_dump(mode="json")),
        PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
        version,
    )

    destination_checkpoint_ref, provenance = _seed_planner_continuation(
        source_checkpoint_path=source_root
        / "sha256"
        / checkpoint_ref.artifact_id.root[7:9]
        / checkpoint_ref.artifact_id.root[7:],
        source_object_root=source_root,
        source_artifacts=source_artifacts,
        destination_artifacts=destination_artifacts,
        input_assets=(input_ref,),
        source_commit=source_commit,
        destination_project_id=ProjectId("project.destination"),
        destination_basis=destination_commit,
        destination_snapshot=snapshot_id_for_commit(destination_commit),
        run_id=RunId("run.destination"),
        current_chapter=40,
        target_chapters=41,
        problem_identity_seed=problem_identity_seed,
        source_text_root=source_text_root,
    )

    seeded_checkpoint = PlanningLoopCheckpoint.model_validate_json(
        destination_artifacts.read_verified(destination_checkpoint_ref), strict=True
    )
    assert seeded_checkpoint.phase is PlanningLoopPhase.INQUIRY_ACCEPTED
    assert seeded_checkpoint.base_commit == destination_commit
    assert seeded_checkpoint.memory_context_ref is None
    assert seeded_checkpoint.planner_context_ref is None
    assert seeded_checkpoint.proposal_ref is None
    assert seeded_checkpoint.plan_review_ref is None
    assert seeded_checkpoint.execution_ref is None
    assert seeded_checkpoint.problem_identity_seed == problem_identity_seed
    assert seeded_checkpoint.pending_planner_memory_questions == (problem_identity_seed.need_query,)
    seeded_inquiry = PlanningInquiry.model_validate_json(
        destination_artifacts.read_verified(seeded_checkpoint.inquiry_ref), strict=True
    )
    assert seeded_inquiry.project_id == ProjectId("project.destination")
    assert seeded_inquiry.author_intent_refs == (input_ref,)
    seeded_review = PlanReview.model_validate_json(
        destination_artifacts.read_verified(seeded_checkpoint.inquiry_review_ref), strict=True
    )
    assert seeded_review.decision is ReviewDecision.ACCEPT
    assert seeded_review.target_artifact_ref == seeded_checkpoint.inquiry_ref
    assert seeded_review.receipt.status is ExecutionStatus.SKIPPED
    assert seeded_review.receipt.model_call_ids == ()
    assert provenance["mode"] == "accepted_inquiry_only"
    assert provenance["problem_identity_seed"] == problem_identity_seed.model_dump(mode="json")


def test_target_preflight_requires_migrated_fresh_database(tmp_path: Path) -> None:
    database_url = _database(tmp_path / "preflight.db")
    with pytest.raises(RuntimeError, match="missing required tables"):
        _preflight_target_database(
            database_url=database_url,
            project_id=ProjectId("project.u6e.preflight"),
            run_id=RunId("u6e-preflight"),
        )

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0010_model_call_ledger')"))
    engine.dispose()
    result = _preflight_target_database(
        database_url=database_url,
        project_id=ProjectId("project.u6e.preflight"),
        run_id=RunId("u6e-preflight"),
    )
    assert result["database_created"] is True
    assert result["identity_free"] is True


def test_health_preflight_accepts_empty_http_200_body(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyHealthResponse:
        status = 200

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b""

    monkeypatch.setattr(preparation, "urlopen", lambda *_args, **_kwargs: EmptyHealthResponse())
    assert preparation._probe_health("http://127.0.0.1:8005/health") == {
        "http_status": 200,
        "body_present": False,
    }


def test_prepare_continues_from_frozen_c40_without_reusing_source(tmp_path: Path) -> None:
    source_url = _database(tmp_path / "source.db")
    destination_url = _database(tmp_path / "destination.db")
    source_project = ProjectId("project.u6b.frozen.c40")
    destination_project = ProjectId("project.u6e.endurance.continuation")
    source_root = tmp_path / "source-objects"
    destination_root = tmp_path / "destination-objects"
    source_artifacts = ArtifactRepository(FilesystemObjectStore(source_root))
    bundle = make_synthetic_bundle()
    base_text = next(root for root in bundle.text_roots if len(root.chapters) == 20)
    extra_chapters = tuple(
        ChapterDocument(
            chapter_id=StableId(f"chapter.synthetic.{chapter}"),
            chapter_index=chapter,
            scenes=(),
        )
        for chapter in range(21, 41)
    )
    text = base_text.model_copy(update={"chapters": base_text.chapters + extra_chapters})
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    version = SchemaVersion("1.0.0")

    def put(value: DomainModel, media_type: str) -> ArtifactRef:
        return source_artifacts.put(
            canonical_json_bytes(value.model_dump(mode="json")), media_type, version
        )

    text_ref = put(text, "application/json")
    plan_ref = put(plan, "application/json")
    world_ref = put(world, "application/json")
    author_ref = source_artifacts.put(b"author intent", "text/plain", version)
    reference = ReferenceRootDocument(
        root_hash=ArtifactId("sha256:" + "3" * 64),
        schema_version=version,
        assets=(
            ReferenceAsset(
                asset_id=StableId("reference.author"),
                source_id=StableId("source.author"),
                source_class=SourceClass.AUTHOR_INITIAL_BRIEF,
                artifact=author_ref,
            ),
        ),
    )
    reference_ref = source_artifacts.put(
        canonical_json_bytes(reference.model_dump(mode="json")), "application/json", version
    )
    profile_ref = source_artifacts.put(b"{}", "application/json", version)
    manifest = make_manifest(source_project).model_copy(
        update={
            "text_root": TextRootRef(**text_ref.model_dump(mode="python")),
            "plan_root": PlanRootRef(**plan_ref.model_dump(mode="python")),
            "world_root": WorldRootRef(**world_ref.model_dump(mode="python")),
            "reference_root": ReferenceRootRef(**reference_ref.model_dump(mode="python")),
            "project_profile_root": ProjectProfileRootRef(**profile_ref.model_dump(mode="python")),
        }
    )
    source_engine = create_engine(source_url)
    source_commit = CommitService(build_session_factory(source_engine)).initialize_project(manifest)
    source_engine.dispose()

    receipt, request = prepare(
        source_database_url=source_url,
        source_project_id=source_project,
        source_commit=source_commit,
        source_object_root=source_root,
        destination_database_url=destination_url,
        destination_project_id=destination_project,
        destination_object_root=destination_root,
        run_id=RunId("u6e-continuation"),
        target_chapters=90,
    )

    assert receipt["schema"] == "u6e-endurance-basis.v3"
    assert receipt["source_is_current"] is True
    assert receipt["source_history_last_chapter"] == 40
    assert receipt["expected_chapters"] == list(range(41, 91))
    assert request.current_chapter == 40
    assert request.target_chapters == 90
    assert request.basis_commit != source_commit

    destination_engine = create_engine(destination_url)
    destination_commits = CommitService(build_session_factory(destination_engine))
    assert destination_commits.current_commit(destination_project).root == request.basis_commit.root
    destination_engine.dispose()
