from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import novel_agent.agents.controller as controller_module
import novel_agent.services.teacher_forced_benchmark_e2e as e2e_module
from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.agents import seal_agent_spec
from novel_agent.domain.changes import ValidationStatus
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.memory_write import (
    MemoryWriteBudgetUsage,
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
)
from novel_agent.domain.retrieval_routing import (
    RetrievalBackendProfile,
    SnapshotCapabilityStatus,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    BenchmarkInformationProfile,
    SourceClass,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.bootstrap_workflow import BootstrapCrossRootValidator
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner
from novel_agent.services.teacher_forced_benchmark_e2e import (
    TeacherForcedBenchmarkE2ERunner,
    TeacherForcedBenchmarkError,
    TeacherForcedControlledPause,
    _E2EContextFreezer,
    _E2EEvaluator,
    _FrozenState,
    _ProgressWriter,
    _ResponseBook,
    _TeacherForcedTransition,
    source_project,
)
from novel_agent.services.teacher_forced_scenario import TeacherForcedScenarioRunner
from tests.contract.test_memory_write_workflow_contract import _artifact
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.unit.test_stage2_scenario import GENESIS, scenario
from tests.unit.test_teacher_forced_scenario_edges import TransitionPort

ROOT = Path(__file__).parents[2]
PILOT = ROOT / "benchmarks/private/ztj_memory_pilot_v0.1"


def test_response_book_rejects_missing_and_unused_scripted_responses() -> None:
    book = _ResponseBook()
    request = TeacherForcedBenchmarkE2ERunner._request("missing", AgentMode.REPLAY)
    with pytest.raises(TeacherForcedBenchmarkError, match="has no response"):
        book.resolve(request)

    book.add(request.request_id, scenario())
    with pytest.raises(TeacherForcedBenchmarkError, match="unused responses"):
        book.assert_empty()
    assert book.resolve(request)
    book.assert_empty()


def test_resume_requires_bound_progress_manifest(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    runner = TeacherForcedBenchmarkE2ERunner()
    with pytest.raises(TeacherForcedBenchmarkError, match="no progress manifest"):
        runner.run(
            tmp_path,
            tmp_path / "output",
            bundle,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            resume=True,
        )

    project = tmp_path / "project"
    project.mkdir()
    (project / "progress_manifest.json").write_text(
        json.dumps({"last_accepted_chapter": 1}),
        encoding="utf-8",
    )
    with pytest.raises(TeacherForcedBenchmarkError, match="no last accepted commit"):
        runner.run(
            tmp_path,
            tmp_path / "other-output",
            bundle,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            resume=True,
            project_directory=project,
        )


def test_teacher_forced_utility_guards_and_descriptors(tmp_path: Path) -> None:
    runner = TeacherForcedBenchmarkE2ERunner(
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        real_hybrid_backend_provider=lambda _project, _commit: MagicMock(),
    )
    runner._real_hybrid_backend_provider = None
    with pytest.raises(TeacherForcedBenchmarkError, match="provider is not configured"):
        runner._attestation_for_commit(
            ProjectId("project.test"),
            CommitId("sha256:" + "1" * 64),
        )
    attestation = MagicMock()
    runner._real_hybrid_backend_provider = lambda _project, _commit: MagicMock(
        attestation=attestation
    )
    assert (
        runner._attestation_for_commit(
            ProjectId("project.test"),
            CommitId("sha256:" + "1" * 64),
        )
        is attestation
    )

    descriptor = runner._database_descriptor(
        "postgresql+psycopg://user:secret@db.example:5432/novel",
        tmp_path / "ignored.sqlite3",
    )
    assert descriptor == "postgresql+psycopg://db.example:5432/novel"
    assert "configured structured generation model" in runner._quality_blocker(False, True)
    assert "projection attestation" in runner._quality_blocker(True, False)
    assert ";" in runner._quality_blocker(False, False)


def test_bootstrap_loader_rejects_directory_escape(tmp_path: Path) -> None:
    source = tmp_path / "source"
    bootstrap = source / "bootstrap"
    bootstrap.mkdir(parents=True)
    (bootstrap / "bootstrap_manifest.yaml").write_text(
        """
bootstrap_id: bootstrap.escape
sources:
  - source_id: source.escape
    source_class: reference
    media_type: text/plain
    path: ../escape.txt
""".strip(),
        encoding="utf-8",
    )
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))

    with pytest.raises(TeacherForcedBenchmarkError, match="escapes its directory"):
        TeacherForcedBenchmarkE2ERunner._load_bootstrap(
            source,
            ProjectId("project.test"),
            artifacts,
        )


def test_semantic_harness_builds_structured_controller_policy() -> None:
    endpoint = FakeModelEndpoint("{}")
    harness = TeacherForcedBenchmarkE2ERunner._agent_harness(endpoint)

    assert harness.responses is None
    assert harness.endpoint is endpoint
    assert harness.controller_spec is not None
    assert harness.controller_request_factory is not None
    policy = harness.controller_request_factory(harness.controller_spec.tool_policy)
    assert policy.tool_policy_hash == harness.controller_spec.tool_policy.content_hash
    state_request = MagicMock(
        request_id=StableId("request.controller.fixture"),
        run_id=RunId("run.controller.fixture"),
        task_id=TaskId("task.controller.fixture"),
    )
    generated = policy._request_factory({"request": state_request}, 2)
    assert generated.request_id.root.endswith(".r2")
    TeacherForcedBenchmarkE2ERunner._script(
        harness,
        TeacherForcedBenchmarkE2ERunner._request("semantic", AgentMode.REPLAY),
        scenario(),
    )


def test_controller_policy_factory_rejects_hash_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TeacherForcedBenchmarkE2ERunner._agent_harness(FakeModelEndpoint("{}"))
    assert harness.controller_spec is not None
    assert harness.controller_request_factory is not None
    original_seal = seal_agent_spec

    def mismatched_spec(spec: Any) -> Any:
        sealed = original_seal(spec)
        return sealed.model_copy(
            update={
                "tool_policy": sealed.tool_policy.model_copy(
                    update={"content_hash": ArtifactId("sha256:" + "9" * 64)}
                )
            }
        )

    monkeypatch.setattr(e2e_module, "seal_agent_spec", mismatched_spec)
    with pytest.raises(TeacherForcedBenchmarkError, match="sealed ToolPolicy hash"):
        harness.controller_request_factory(harness.controller_spec.tool_policy)

    monkeypatch.setattr(e2e_module, "seal_agent_spec", original_seal)

    class MismatchedPolicy:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.tool_policy_hash = ArtifactId("sha256:" + "8" * 64)

    monkeypatch.setattr(controller_module, "StructuredControllerPolicy", MismatchedPolicy)
    with pytest.raises(TeacherForcedBenchmarkError, match="after construction"):
        harness.controller_request_factory(harness.controller_spec.tool_policy)


def test_genesis_fails_closed_on_cross_root_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    failed = MagicMock(
        status=ValidationStatus.FAILED,
        findings=(MagicMock(code="fixture_failure"),),
    )
    monkeypatch.setattr(
        BootstrapCrossRootValidator,
        "validate",
        lambda _self, _candidates: failed,
    )

    with pytest.raises(TeacherForcedBenchmarkError, match="bootstrap validation failed"):
        TeacherForcedBenchmarkE2ERunner().run(
            PILOT,
            tmp_path / "invalid-genesis",
            bundle,
            information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        )


def test_real_profile_skips_smoke_schema_creation_before_bundle_guard(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    cases = (
        bundle.case_manifests[0],
        bundle.case_manifests[0].model_copy(update={"project_id": ProjectId("project.other")}),
    )
    invalid = bundle.model_copy(update={"case_manifests": cases})
    runner = TeacherForcedBenchmarkE2ERunner(
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        real_hybrid_backend_provider=lambda _project, _commit: MagicMock(),
        database_url=f"sqlite:///{tmp_path / 'real.sqlite3'}",
    )

    with pytest.raises(TeacherForcedBenchmarkError, match="must use one project"):
        runner.run(
            tmp_path,
            tmp_path / "real-output",
            invalid,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        )


def test_paired_report_requires_results_on_one_comparison_basis() -> None:
    bundle = make_synthetic_bundle()
    case = Stage2PairedPilotRunner().run(bundle).cases[0]
    with pytest.raises(TeacherForcedBenchmarkError, match="no paired results"):
        TeacherForcedBenchmarkE2ERunner._paired_report(
            bundle,
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            (),
        )
    other = case.model_copy(
        update={
            "information_profile": BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            "comparison_basis_fingerprint": bundle.content_hash,
        }
    )
    with pytest.raises(TeacherForcedBenchmarkError, match="different comparison bases"):
        TeacherForcedBenchmarkE2ERunner._paired_report(
            bundle,
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            (case, other),
        )


def test_checkpointless_wrapper_rejects_noncontinuable_freshness() -> None:
    with pytest.raises(TeacherForcedBenchmarkError, match="non-continuable freshness"):
        TeacherForcedBenchmarkE2ERunner._run_without_checkpoints(
            scenario(),
            GENESIS,
            TransitionPort("freshness"),  # type: ignore[arg-type]
        )


def test_transition_helpers_reject_invalid_sources_and_plan_bindings(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    transition = object.__new__(_TeacherForcedTransition)
    transition.bundle = bundle
    transition.documents = {}
    with pytest.raises(TeacherForcedBenchmarkError, match="has no document"):
        transition.apply(MagicMock(chapter_index=None), GENESIS)

    transition.documents = {1: MagicMock()}
    transition.commits = MagicMock()
    transition.commits.current_commit.return_value = CommitId("sha256:" + "9" * 64)
    with pytest.raises(TeacherForcedBenchmarkError, match="not current Canon"):
        transition.apply(MagicMock(chapter_index=1), GENESIS)

    case = bundle.case_manifests[0]
    transition.bundle = bundle
    with pytest.raises(TeacherForcedBenchmarkError, match="has no author PlanRoot"):
        transition._bind_checkpoint_author_plan(case.model_copy(update={"input_plan_root": None}))
    with pytest.raises(TeacherForcedBenchmarkError, match="missing from the bundle"):
        transition._bind_checkpoint_author_plan(
            case.model_copy(update={"input_plan_root": ArtifactId("sha256:" + "9" * 64)})
        )
    with pytest.raises(TeacherForcedBenchmarkError, match="does not cover target range"):
        transition._bind_checkpoint_author_plan(case.model_copy(update={"target_range": (90, 91)}))
    with pytest.raises(TeacherForcedBenchmarkError, match="validation artifact"):
        transition._missing_validation_artifact(1)
    transition.case_by_chapter = {}
    with pytest.raises(TeacherForcedBenchmarkError, match="requires a declared case"):
        transition._recover_checkpoint(MagicMock(), GENESIS, 1)

    transition.case_by_chapter = {1: case}
    transition.commits = MagicMock()
    transition.commits.load_manifest.return_value = MagicMock()
    transition.snapshots = MagicMock()
    transition.snapshots.get_for_commit.return_value = None
    with pytest.raises(TeacherForcedBenchmarkError, match="projection is not fresh"):
        transition._recover_checkpoint(MagicMock(), GENESIS, 1)


def test_transition_commit_result_guards_and_optional_sinks() -> None:
    failed = MagicMock(
        status=MemoryWriteWorkflowStatus.FATAL,
        resulting_commit=None,
        workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        canonical_commit_accepted=False,
        terminal_codes=("fixture",),
    )
    with pytest.raises(TeacherForcedBenchmarkError, match="workflow stopped"):
        _TeacherForcedTransition._require_committed_result(1, failed)
    controlled = MagicMock(
        status=MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED,
        resulting_commit=None,
    )
    with pytest.raises(TeacherForcedControlledPause):
        _TeacherForcedTransition._require_committed_result(8, controlled)
    dry_run = MagicMock(
        status=MemoryWriteWorkflowStatus.SUSPENDED,
        resulting_commit=None,
    )
    with pytest.raises(TeacherForcedControlledPause):
        _TeacherForcedTransition._require_committed_result(21, dry_run)
    with pytest.raises(TeacherForcedBenchmarkError, match="complete canonical state"):
        _TeacherForcedTransition._require_complete_canonical_state(
            1,
            MagicMock(canonical_text=None, canonical_world=None, canonical_plan=None),
            None,
            None,
            None,
        )
    with pytest.raises(TeacherForcedBenchmarkError, match="no Curator receipt"):
        _TeacherForcedTransition._require_curator_receipt(None)

    transition = object.__new__(_TeacherForcedTransition)
    transition.real_hybrid_backend_provider = lambda _project, _commit: MagicMock(
        attestation="attestation"
    )
    transition.latest_attestation = None
    transition._capture_latest_attestation(
        ProjectId("project.test"),
        CommitId("sha256:" + "1" * 64),
    )
    assert transition.latest_attestation == "attestation"
    writer = MagicMock()
    transition.progress_writer = writer
    transition._record_progress(CommitId("sha256:" + "1" * 64), 1)
    writer.record.assert_called_once()
    transition.progress_writer = None
    transition._record_progress(CommitId("sha256:" + "1" * 64), 1)


def test_progress_writer_records_pause_without_completing_chapter(tmp_path: Path) -> None:
    path = tmp_path / "progress_manifest.json"
    writer = _ProgressWriter(path)
    writer.record(CommitId("sha256:" + "1" * 64).root, 7)
    paused = MagicMock(
        status=MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED,
        checkpoint_ref=None,
        terminal_codes=("CURATOR_PROPOSAL_BUDGET_EXHAUSTED",),
    )

    writer.record_pause(8, paused)

    manifest = json.loads(path.read_text("utf-8"))
    assert manifest["completed_chapters"] == [7]
    assert manifest["last_accepted_chapter"] == 7
    assert manifest["workflow_pause"]["chapter"] == 8


def test_runner_persists_controlled_pause_summary_and_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    paused = MemoryWriteWorkflowResult(
        request_id=StableId("request.controlled.pause"),
        status=MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED,
        workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        canonical_commit_accepted=False,
        base_commit=GENESIS,
        checkpoint_ref=_artifact("7"),
        budget_usage=MemoryWriteBudgetUsage(
            curator_proposal_attempts=2,
            curator_proposal_rejections=2,
        ),
        terminal_codes=(
            "CURATOR_PROPOSAL_BUDGET_EXHAUSTED",
            "CURATOR_PROPOSAL_POISON_LOOP",
        ),
    )

    def pause(*_args: object, **_kwargs: object) -> None:
        raise TeacherForcedControlledPause(8, paused)

    monkeypatch.setattr(TeacherForcedScenarioRunner, "run", pause)
    output = tmp_path / "controlled-pause"
    summary = TeacherForcedBenchmarkE2ERunner().run(
        PILOT,
        output,
        bundle,
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )

    assert summary["status"] == "teacher_forced_controlled_pause"
    assert summary["paused_chapter"] == 8
    assert summary["memory_write_proposal_terminal_status"] == "budget_exhausted"
    assert summary["memory_write_resume_checkpoint"] is not None
    trace = json.loads((output / "memory_write_pause_trace.json").read_text("utf-8"))
    assert trace["chapter"] == 8


def test_transition_records_proposal_metrics_for_pause_and_nonpause() -> None:
    transition = object.__new__(_TeacherForcedTransition)
    for name in (
        "memory_write_candidate_revisions",
        "memory_write_normalization_passes",
        "memory_write_guardian_reviews",
        "memory_write_context_refreshes",
        "memory_write_transport_attempts",
        "memory_write_tokens",
        "memory_write_proposal_attempts",
        "memory_write_proposal_rejections",
        "memory_write_proposal_poison_loops",
        "guardian_gate_decisions",
    ):
        setattr(transition, name, 0)
    transition.memory_write_proposal_retry_counts = {}
    transition.memory_write_status_counts = {}
    transition.memory_write_proposal_terminal_status = None
    transition.memory_write_resume_checkpoint = None
    transition.progress_writer = MagicMock()
    paused = MemoryWriteWorkflowResult(
        request_id=StableId("request.metrics.pause"),
        status=MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED,
        workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        canonical_commit_accepted=False,
        base_commit=GENESIS,
        checkpoint_ref=_artifact("8"),
        budget_usage=MemoryWriteBudgetUsage(
            curator_proposal_attempts=3,
            curator_proposal_rejections=2,
            candidate_revisions=1,
            normalization_passes=1,
            guardian_reviews=1,
            context_refreshes=1,
            transport_attempts=2,
            tokens_used=10,
        ),
        terminal_codes=("CURATOR_PROPOSAL_POISON_LOOP",),
    )
    transition._record_memory_write_outcome(8, paused)

    assert transition.memory_write_proposal_retry_counts == {"budget_exhausted": 2}
    assert transition.memory_write_proposal_poison_loops == 1
    assert transition.memory_write_resume_checkpoint is not None
    transition.progress_writer.record_pause.assert_called_once_with(8, paused)

    transition.progress_writer = None
    fatal = MemoryWriteWorkflowResult(
        request_id=StableId("request.metrics.fatal"),
        status=MemoryWriteWorkflowStatus.FATAL,
        workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        canonical_commit_accepted=False,
        base_commit=GENESIS,
    )
    transition._record_memory_write_outcome(9, fatal)
    assert transition.memory_write_resume_checkpoint is None

    committed = MagicMock(
        status=MemoryWriteWorkflowStatus.COMMITTED,
        budget_usage=MemoryWriteBudgetUsage(),
        terminal_codes=(),
    )
    transition._record_memory_write_outcome(10, committed)
    assert transition.memory_write_status_counts["committed"] == 1


def test_transition_constructor_handles_text_root_without_prelude(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    transition = _TeacherForcedTransition(
        bundle=bundle,
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        artifacts=ArtifactRepository(FilesystemObjectStore(tmp_path / "transition-objects")),
        commits=MagicMock(),
        project_id=ProjectId("project.test"),
        projections=MagicMock(),
        snapshots=MagicMock(),
        harness=TeacherForcedBenchmarkE2ERunner._agent_harness(),
        current_text=bundle.text_roots[0],
        current_world=bundle.world_roots[0],
        current_plan=bundle.plan_roots[0],
        profile_root_hash=ArtifactId("sha256:" + "7" * 64),
    )

    assert 0 not in transition.documents


def test_transition_runs_scripted_curator_and_scripts_risk_review(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    transition = object.__new__(_TeacherForcedTransition)
    transition.harness = TeacherForcedBenchmarkE2ERunner._agent_harness()
    transition.text = bundle.text_roots[1]
    transition.world = bundle.world_roots[0]
    transition.curator_calls = 0

    result = transition._run_curator(23, transition.world.source_commit)

    assert result.receipt.base_commit == transition.world.source_commit
    assert transition.curator_calls == 1
    transition._active_workflow_chapter = 23
    request = TeacherForcedBenchmarkE2ERunner._request("risk", AgentMode.RISK_REVIEW)
    transition._script_workflow_model(request, AgentMode.RISK_REVIEW)
    assert transition.harness.responses is not None
    assert transition.harness.responses.resolve(request)

    transition.harness = TeacherForcedBenchmarkE2ERunner._agent_harness(FakeModelEndpoint("{}"))
    transition._script_workflow_model(request, AgentMode.RISK_REVIEW)
    transition.harness = TeacherForcedBenchmarkE2ERunner._agent_harness()
    transition._script_workflow_model(request, AgentMode.CHAPTER)


def test_real_hybrid_freezer_requires_fresh_attestation(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    state = _FrozenState(
        text=bundle.text_roots[0],
        world=bundle.world_roots[0],
        plan=bundle.plan_roots[0],
        commit=bundle.world_roots[0].source_commit,
    )
    transition = MagicMock(
        bundle=bundle,
        states={case.case_id: state},
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "freeze-objects"))
    paired = MagicMock()
    paired.retrieval_backend_profile = RetrievalBackendProfile.REAL_HYBRID
    basis = MagicMock(case_id=case.case_id)
    freezer = _E2EContextFreezer(
        MagicMock(),
        transition,
        artifacts,
        paired,
    )
    with pytest.raises(TeacherForcedBenchmarkError, match="requires a commit-scoped"):
        freezer.freeze(basis)

    stale = MagicMock(
        source_commit=CommitId("sha256:" + "9" * 64),
        quality_eligible=False,
    )
    stale.capability.status = MagicMock()
    freezer = _E2EContextFreezer(
        MagicMock(),
        transition,
        artifacts,
        paired,
        real_hybrid_backend_provider=lambda _project, _commit: MagicMock(attestation=stale),
    )
    with pytest.raises(TeacherForcedBenchmarkError, match="incomplete or stale"):
        freezer.freeze(basis)

    exact = MagicMock(source_commit=state.commit, quality_eligible=True)
    exact.capability.status = SnapshotCapabilityStatus.EXACT
    comparison = MagicMock()
    comparison.model_dump.return_value = {"comparison": "fixture"}
    paired.resolve_state_case.return_value = comparison
    freezer = _E2EContextFreezer(
        MagicMock(),
        transition,
        artifacts,
        paired,
        real_hybrid_backend_provider=lambda _project, _commit: MagicMock(
            attestation=exact,
            backend=MagicMock(),
            reranker=MagicMock(),
        ),
    )

    assert freezer.freeze(basis)
    assert freezer.latest_attestation is exact


def test_evaluator_requires_both_private_source_classes() -> None:
    evaluator = _E2EEvaluator(
        make_synthetic_bundle(),
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        MagicMock(),
        MagicMock(),
        Stage2PairedPilotRunner(),
    )
    future = MagicMock(source_class=SourceClass.FUTURE_TEXT_PRIVATE)

    with pytest.raises(TeacherForcedBenchmarkError, match="sources are incomplete"):
        evaluator.score(MagicMock(), (future,))


def test_source_project_requires_one_project() -> None:
    bundle = make_synthetic_bundle()
    cases = (
        bundle.case_manifests[0],
        bundle.case_manifests[0].model_copy(update={"project_id": ProjectId("project.other")}),
    )
    with pytest.raises(TeacherForcedBenchmarkError, match="must use one project"):
        source_project(bundle.model_copy(update={"case_manifests": cases}))


def test_progress_writer_reloads_and_deduplicates_chapters(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"
    writer = _ProgressWriter(path)
    writer.genesis_commit = "sha256:" + "0" * 64
    writer.record("sha256:" + "1" * 64, 1)
    writer.record("sha256:" + "1" * 64, 1)

    with _ProgressWriter(path) as restored:
        assert restored.genesis_commit == writer.genesis_commit
        assert restored.last_chapter == 1
        assert restored.completed_chapters == [1]
