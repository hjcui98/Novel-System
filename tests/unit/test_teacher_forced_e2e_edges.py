from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import novel_agent.agents.controller as controller_module
import novel_agent.services.teacher_forced_benchmark_e2e as e2e_module
from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.adapters.postgres.database import Base, build_engine, build_session_factory
from novel_agent.agents import seal_agent_spec
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.changes import ValidationStatus
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory_benchmark import ContextAssemblyStatus
from novel_agent.domain.memory_write import (
    MemoryWriteBudgetUsage,
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
)
from novel_agent.domain.model_calls import ModelRole
from novel_agent.domain.retrieval_routing import (
    RetrievalBackendProfile,
    SnapshotCapabilityStatus,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    BenchmarkInformationProfile,
    ControllerMode,
    ScenarioRunResult,
    SourceClass,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.bootstrap_workflow import BootstrapCrossRootValidator
from novel_agent.services.commits import CommitService, ProjectNotFoundError
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_evaluation import (
    ModelSemanticSupportVerifier,
    SemanticGoldJudgment,
    SemanticSupport,
    SemanticVerificationBatch,
)
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner
from novel_agent.services.teacher_forced_benchmark_e2e import (
    TeacherForcedBenchmarkE2ERunner,
    TeacherForcedBenchmarkError,
    TeacherForcedControlledPause,
    TeacherForcedTerminalFailure,
    _E2EContextFreezer,
    _E2EEvaluator,
    _FrozenState,
    _ProgressWriter,
    _quality_repair_memory_write_budget,
    _ResponseBook,
    _TeacherForcedTransition,
    source_project,
)
from novel_agent.services.teacher_forced_scenario import TeacherForcedScenarioRunner
from tests.contract.test_memory_write_workflow_contract import _artifact
from tests.factories import make_commit_request, make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.fixtures.stage2_memory_benchmark import resolved_public_comparison
from tests.unit.test_stage2_scenario import GENESIS, scenario
from tests.unit.test_teacher_forced_scenario_edges import TransitionPort

ROOT = Path(__file__).parents[2]
PILOT = ROOT / "benchmarks/private/ztj_memory_pilot_v0.1"


def test_controller_prompt_prefers_registered_batch_plan() -> None:
    prompt = (ROOT / "src/novel_agent/prompts/memory_controller_v1.md").read_text()

    assert '"action":"execute_plan"' in prompt
    assert "`selected_action_ids`" in prompt
    assert "up to `max_agentic_actions`" in prompt


def test_quality_repair_memory_write_budget_allows_progressive_feedback_retries() -> None:
    budget = _quality_repair_memory_write_budget()

    assert budget.max_curator_proposal_attempts == 5
    assert budget.max_curator_proposal_rejections == 5
    assert budget.max_total_model_calls == 64
    assert budget.token_budget == 192_000
    assert budget.wall_clock_budget_ms == 900_000
    assert budget.same_content_hash_limit == 3
    assert budget.same_finding_signature_limit == 3


def test_teacher_forced_model_request_leaves_time_for_narrow_verifier() -> None:
    request = TeacherForcedBenchmarkE2ERunner._request("curator", AgentMode.REPLAY)

    assert request.timeout_seconds == 600
    assert request.max_output_tokens == 12288
    assert request.enable_thinking is False
    assert request.thinking_token_budget is None

    bootstrap = TeacherForcedBenchmarkE2ERunner._request(
        "planner.bootstrap", AgentMode.PROJECT_BOOTSTRAP
    )
    assert bootstrap.enable_thinking is False
    assert bootstrap.thinking_token_budget is None


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
    with pytest.raises(ValueError, match="checkpoint workers"):
        TeacherForcedBenchmarkE2ERunner(checkpoint_workers=0)
    with pytest.raises(
        TeacherForcedBenchmarkError,
        match="explicit resume commit and chapter must be supplied together",
    ):
        runner.run(
            tmp_path,
            tmp_path / "mismatched-explicit-resume",
            bundle,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            resume=True,
            resume_commit=CommitId("sha256:" + "1" * 64),
        )
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

    with pytest.raises(ProjectNotFoundError):
        runner.run(
            tmp_path,
            tmp_path / "explicit-output",
            bundle,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            resume=True,
            resume_commit=CommitId("sha256:" + "2" * 64),
            resume_chapter_override=1,
            project_directory=tmp_path / "explicit-project",
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


def test_real_hybrid_persists_and_rejects_incomplete_scenario_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    database_url = f"sqlite:///{tmp_path / 'formal.sqlite3'}"
    engine = build_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()

    class IncompleteScenarioRunner:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def run(self, compiled: Any, _genesis: CommitId) -> ScenarioRunResult:
            return ScenarioRunResult(
                scenario_id=compiled.scenario_id,
                project_id=compiled.project_id,
                build_mode=compiled.profile.build_mode,
                chapter_receipts=(),
                checkpoints=(),
                completed=False,
                blockers=("test lifecycle incomplete",),
            )

    monkeypatch.setattr(e2e_module, "TeacherForcedScenarioRunner", IncompleteScenarioRunner)
    output = tmp_path / "formal-output"
    runner = TeacherForcedBenchmarkE2ERunner(
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        real_hybrid_backend_provider=lambda _project, _commit: MagicMock(),
        database_url=database_url,
    )

    with pytest.raises(TeacherForcedBenchmarkError, match="scenario lifecycle is incomplete"):
        runner.run(
            PILOT,
            output,
            bundle,
            information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        )

    scenario_payload = json.loads((output / "scenario_run.json").read_text("utf-8"))
    assert scenario_payload["completed"] is False
    assert scenario_payload["blockers"] == ["test lifecycle incomplete"]


def test_postcommit_progress_reconciliation_is_single_commit_and_atomic(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    text = bundle.text_roots[0]
    recovered_chapter = max(item.chapter_index for item in text.chapters)
    expected = CommitId("sha256:" + "1" * 64)
    current = CommitId("sha256:" + "2" * 64)
    progress_path = tmp_path / "progress_manifest.json"
    progress_path.write_text(
        json.dumps(
            {
                "genesis_commit": expected.root,
                "last_accepted_commit": expected.root,
                "last_accepted_chapter": recovered_chapter - 1,
                "completed_chapters": [recovered_chapter - 1],
                "workflow_pause": {"status": "interrupted"},
            }
        ),
        encoding="utf-8",
    )
    manifest = MagicMock(parent_commit_ids=(expected,), text_root=MagicMock())
    commits = MagicMock()
    commits.load_manifest.return_value = manifest
    artifacts = MagicMock()
    artifacts.read_verified.return_value = text.model_dump_json().encode()

    commit, chapter, recovery = TeacherForcedBenchmarkE2ERunner._reconcile_postcommit_progress(
        commits=commits,
        artifacts=artifacts,
        progress_path=progress_path,
        expected_head=expected,
        current_head=current,
        expected_chapter=recovered_chapter - 1,
    )

    persisted = json.loads(progress_path.read_text("utf-8"))
    assert (commit, chapter) == (current.root, recovered_chapter)
    assert recovery["direct_parent_verified"] is True
    assert persisted["last_accepted_commit"] == current.root
    assert persisted["completed_chapters"][-1] == recovered_chapter
    assert "workflow_pause" not in persisted

    commits.load_manifest.return_value = MagicMock(
        parent_commit_ids=(CommitId("sha256:" + "3" * 64),),
        text_root=MagicMock(),
    )
    with pytest.raises(TeacherForcedBenchmarkError, match="direct child"):
        TeacherForcedBenchmarkE2ERunner._reconcile_postcommit_progress(
            commits=commits,
            artifacts=artifacts,
            progress_path=progress_path,
            expected_head=expected,
            current_head=current,
            expected_chapter=recovered_chapter - 1,
        )

    commits.load_manifest.return_value = manifest
    with pytest.raises(TeacherForcedBenchmarkError, match="exactly one additional chapter"):
        TeacherForcedBenchmarkE2ERunner._reconcile_postcommit_progress(
            commits=commits,
            artifacts=artifacts,
            progress_path=progress_path,
            expected_head=expected,
            current_head=current,
            expected_chapter=recovered_chapter - 2,
        )


def test_runner_reconciles_real_postcommit_window_before_loading_roots(
    tmp_path: Path,
) -> None:
    bundle = make_synthetic_bundle()
    project_id = bundle.case_manifests[0].project_id
    project = tmp_path / "recovery-project"
    project.mkdir()
    repository = ArtifactRepository(FilesystemObjectStore(project / "objects"))

    def store(payload: bytes, media_type: str) -> Any:
        return repository.put(payload, media_type, bundle.bundle_schema_version)

    text = bundle.text_roots[0]
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    text_ref = TextRootRef.model_validate(
        store(text.model_dump_json().encode(), "application/vnd.test.text+json").model_dump()
    )
    world_ref = WorldRootRef.model_validate(
        store(world.model_dump_json().encode(), "application/vnd.test.world+json").model_dump()
    )
    plan_ref = PlanRootRef.model_validate(
        store(plan.model_dump_json().encode(), "application/vnd.test.plan+json").model_dump()
    )
    reference_ref = ReferenceRootRef.model_validate(
        store(b'{"reference":true}', "application/vnd.test.reference+json").model_dump()
    )
    profile_ref = ProjectProfileRootRef.model_validate(
        store(b'{"profile":true}', "application/vnd.test.profile+json").model_dump()
    )
    roots = {
        "text_root": text_ref,
        "world_root": world_ref,
        "plan_root": plan_ref,
        "reference_root": reference_ref,
        "project_profile_root": profile_ref,
    }
    engine = build_engine(f"sqlite:///{project / 'project.sqlite3'}")
    Base.metadata.create_all(engine)
    commits = CommitService(build_session_factory(engine))
    genesis_manifest = make_manifest(project_id).model_copy(update=roots)
    genesis = commits.initialize_project(genesis_manifest)
    child_manifest = genesis_manifest.model_copy(update={"parent_commit_ids": (genesis,)})
    request = make_commit_request(
        genesis,
        project_id=project_id,
        idempotency_key="postcommit.recovery",
    )
    request = request.model_copy(
        update={"bundle": request.bundle.model_copy(update={"proposed_roots": child_manifest})}
    )
    accepted = commits.commit(request)
    assert accepted.commit_id is not None
    last_chapter = max(item.chapter_index for item in text.chapters)
    (project / "progress_manifest.json").write_text(
        json.dumps(
            {
                "genesis_commit": genesis.root,
                "last_accepted_commit": genesis.root,
                "last_accepted_chapter": last_chapter - 1,
                "completed_chapters": list(range(1, last_chapter)),
            }
        ),
        encoding="utf-8",
    )
    engine.dispose()

    output = tmp_path / "recovery-output"
    with pytest.raises(ValueError, match="ProjectProfileRootDocument"):
        TeacherForcedBenchmarkE2ERunner().run(
            tmp_path,
            output,
            bundle,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            resume=True,
            project_directory=project,
        )

    recovery = json.loads((output / "postcommit_recovery.json").read_text("utf-8"))
    assert recovery["recovered_commit"] == accepted.commit_id.root


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
    cast(Any, endpoint).max_retries = 1
    harness = TeacherForcedBenchmarkE2ERunner._agent_harness(endpoint)

    assert harness.responses is None
    assert harness.endpoint is endpoint
    assert harness.controller_spec is not None
    assert harness.controller_request_factory is not None
    assert dict(harness.gateway.endpoint_policy_identity(ModelRole.BATCH_TEST)) == {
        "endpoint_name": "local-openai-chat",
        "registered_model": "local-model",
        "adapter_model": "local-model",
        "adapter_revision": "unknown",
        "adapter_max_retries": "1",
        "structured_max_retries": "0",
    }
    policy = harness.controller_request_factory(harness.controller_spec.tool_policy)
    assert policy.tool_policy_hash == harness.controller_spec.tool_policy.content_hash
    state_request = MagicMock(
        request_id=StableId("request.controller.fixture"),
        run_id=RunId("run.controller.fixture"),
        task_id=TaskId("task.controller.fixture"),
    )
    generated = policy._request_factory({"request": state_request}, 2)
    assert generated.request_id.root.endswith(".r2")
    assert generated.timeout_seconds == 60
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


def test_paired_report_preserves_delta_controller_mode() -> None:
    bundle = make_synthetic_bundle()
    case = (
        Stage2PairedPilotRunner(controller_mode=ControllerMode.DETERMINISTIC_PLUS_AGENTIC_DELTA)
        .run(bundle)
        .cases[0]
    )

    report = TeacherForcedBenchmarkE2ERunner._paired_report(
        bundle,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        (case,),
        controller_mode=ControllerMode.DETERMINISTIC_PLUS_AGENTIC_DELTA,
    )

    assert report.controller_mode is ControllerMode.DETERMINISTIC_PLUS_AGENTIC_DELTA
    assert report.cases[0].delta_metrics is not None


def test_checkpointless_wrapper_rejects_noncontinuable_freshness() -> None:
    with pytest.raises(TeacherForcedBenchmarkError, match="non-continuable freshness"):
        TeacherForcedBenchmarkE2ERunner._run_without_checkpoints(
            scenario(),
            GENESIS,
            TransitionPort("freshness"),  # type: ignore[arg-type]
        )


def test_transition_helpers_reject_invalid_sources(
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


def test_runner_persists_terminal_failure_summary_before_reraising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    terminal = MemoryWriteWorkflowResult(
        request_id=StableId("request.terminal.failure"),
        status=MemoryWriteWorkflowStatus.FATAL,
        workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        canonical_commit_accepted=False,
        base_commit=GENESIS,
        checkpoint_ref=_artifact("6"),
        budget_usage=MemoryWriteBudgetUsage(
            curator_proposal_attempts=1,
            curator_proposal_rejections=1,
        ),
        terminal_codes=("CURATOR_PROPOSAL_INFORMATION_BOUNDARY",),
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise TeacherForcedTerminalFailure(21, terminal)

    monkeypatch.setattr(TeacherForcedScenarioRunner, "run", fail)
    output = tmp_path / "terminal-failure"
    with pytest.raises(
        TeacherForcedTerminalFailure,
        match="CURATOR_PROPOSAL_INFORMATION_BOUNDARY",
    ):
        TeacherForcedBenchmarkE2ERunner().run(
            PILOT,
            output,
            bundle,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        )

    summary = json.loads((output / "flow_summary.json").read_text("utf-8"))
    assert summary["status"] == "teacher_forced_terminal_failure"
    assert summary["failed_chapter"] == 21
    assert summary["memory_write_proposal_terminal_status"] == "fatal"
    assert summary["terminal_codes"] == ["CURATOR_PROPOSAL_INFORMATION_BOUNDARY"]
    trace = json.loads((output / "memory_write_failure_trace.json").read_text("utf-8"))
    assert trace["chapter"] == 21
    assert trace["result"]["canonical_commit_accepted"] is False


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


def _evaluator_sources() -> tuple[MagicMock, MagicMock]:
    return (
        MagicMock(source_class=SourceClass.FUTURE_TEXT_PRIVATE),
        MagicMock(source_class=SourceClass.READ_GOLD),
    )


def test_evaluator_requires_persisted_freeze_receipt(tmp_path: Path) -> None:
    bundle, private_case, _public, runner, comparison = resolved_public_comparison()
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    freezer = MagicMock(
        comparisons={private_case.case_id: comparison.model_copy(update={"freeze_receipt": None})}
    )
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        freezer,
        artifacts,
        runner,
    )
    with pytest.raises(TeacherForcedBenchmarkError, match="persisted freeze receipt"):
        evaluator.score(MagicMock(case_id=private_case.case_id), _evaluator_sources())


def test_evaluator_skips_unready_and_fallback_arms(tmp_path: Path) -> None:
    bundle, private_case, _public, runner, comparison = resolved_public_comparison()
    agentic = comparison.agentic.model_copy(
        update={
            "writer_context": None,
            "evidence_ledger": None,
            "assembly_status": None,
            "quality_eligible": False,
            "failure_category": "NOT_RUN",
        }
    )
    comparison = comparison.model_copy(
        update={
            "agentic": agentic,
            "blockers": ("C_FALLBACK_TO_A",),
            "comparable": False,
        }
    )
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        MagicMock(comparisons={private_case.case_id: comparison}),
        ArtifactRepository(FilesystemObjectStore(tmp_path / "objects")),
        runner,
    )
    evaluator.score(MagicMock(case_id=private_case.case_id), _evaluator_sources())
    assert len(evaluator.stage2m_results) == 1
    assert evaluator.stage2m_results[0].arm == "A"


def test_evaluator_persists_typed_failure_for_unready_arm_a(tmp_path: Path) -> None:
    bundle, private_case, _public, runner, comparison = resolved_public_comparison()
    score_result = runner.score_comparison(
        private_case,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        comparison,
    )
    context = comparison.deterministic.writer_context
    assert context is not None
    failed_status = ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
    failed_context = context.model_copy(
        update={
            "budget_report": context.budget_report.model_copy(
                update={"final_status": failed_status}
            )
        }
    )
    deterministic = comparison.deterministic.model_copy(
        update={
            "writer_context": failed_context,
            "assembly_status": failed_status,
            "quality_eligible": False,
        }
    )
    comparison = comparison.model_copy(update={"deterministic": deterministic, "comparable": False})
    scoring_runner = MagicMock()
    scoring_runner.score_comparison.return_value = score_result
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        MagicMock(comparisons={private_case.case_id: comparison}),
        ArtifactRepository(FilesystemObjectStore(tmp_path / "objects")),
        scoring_runner,
    )

    evaluator.score(MagicMock(case_id=private_case.case_id), _evaluator_sources())

    result = evaluator.stage2m_results[0]
    assert result.arm == "A"
    assert result.assembly_status is failed_status
    assert result.comparable is False
    assert all(item.status.value == "MISS" for item in result.evaluation.comparisons)


def test_model_semantic_evaluator_requires_gateway(tmp_path: Path) -> None:
    bundle, private_case, _public, runner, comparison = resolved_public_comparison()
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        MagicMock(comparisons={private_case.case_id: comparison}),
        ArtifactRepository(FilesystemObjectStore(tmp_path / "objects")),
        runner,
        model_semantic_verifier_enabled=True,
    )
    with pytest.raises(TeacherForcedBenchmarkError, match="configured model gateway"):
        evaluator.score(MagicMock(case_id=private_case.case_id), _evaluator_sources())


def test_model_semantic_evaluator_persists_receipt_per_arm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, private_case, _public, runner, comparison = resolved_public_comparison()

    async def fake_verify(
        _self: ModelSemanticSupportVerifier,
        *,
        gold_items: tuple[Any, ...],
        **_kwargs: Any,
    ) -> tuple[SemanticVerificationBatch, tuple[MagicMock, ...]]:
        batch = SemanticVerificationBatch(
            judgments=tuple(
                SemanticGoldJudgment(
                    gold_id=gold.gold_id,
                    all_claims_support=SemanticSupport.NONE,
                    traceable_claims_support=SemanticSupport.NONE,
                    all_context_item_ids=(),
                    traceable_context_item_ids=(),
                    explanation="not expressed by the frozen claims",
                )
                for gold in gold_items
            )
        )
        call = MagicMock()
        call.model_dump.return_value = {"model_call": "fixture"}
        return batch, (call,)

    monkeypatch.setattr(ModelSemanticSupportVerifier, "verify", fake_verify)
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        MagicMock(comparisons={private_case.case_id: comparison}),
        ArtifactRepository(FilesystemObjectStore(tmp_path / "objects")),
        runner,
        semantic_gateway=MagicMock(),
        model_semantic_verifier_enabled=True,
    )
    evaluator.score(MagicMock(case_id=private_case.case_id), _evaluator_sources())
    assert len(evaluator.stage2m_results) == 3
    assert all(
        item.verifier_receipt_ref is not None
        for result in evaluator.stage2m_results
        for item in result.evaluation.comparisons
    )


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


def test_support_terminal_state_uses_explicit_terminal_event() -> None:
    events: list[dict[str, object]] = [
        {"stage": "proposal", "status": "completed"},
        {"stage": "terminal", "state": "completed_with_failures"},
    ]
    assert (
        TeacherForcedBenchmarkE2ERunner._support_terminal_state(events, scenario_completed=True)
        == "completed_with_failures"
    )


def test_support_terminal_state_legacy_fallback() -> None:
    runner = TeacherForcedBenchmarkE2ERunner
    assert runner._support_terminal_state([], scenario_completed=True) == "completed"
    assert runner._support_terminal_state([], scenario_completed=False) == "completed_with_failures"
    assert (
        runner._support_terminal_state(
            [{"stage": "proposal", "status": "failed"}],
            scenario_completed=True,
        )
        == "completed_with_failures"
    )


def test_execution_lifecycle_statuses_reconcile_scheduling_failure() -> None:
    statuses = TeacherForcedBenchmarkE2ERunner._execution_lifecycle_statuses(
        {"scheduling_timeouts": 3},
        scenario_completed=True,
        single_arm_result_count=1,
        checkpoint_count=1,
        generation_quality_eligible=True,
        retrieval_quality_eligible=True,
    )
    assert statuses["scheduling_failure_count"] == 3
    assert statuses["checkpoint_scenario_status"] == "COMPLETED_WITH_EXECUTION_FAILURE"
    assert statuses["single_arm_evaluation_status"] == "COMPLETED_WITH_EXECUTION_FAILURE"
    assert statuses["semantic_quality_eligible"] is False
    assert statuses["quality_blocker"] == "SCHEDULING_INFRASTRUCTURE_FAILURE"


def test_execution_lifecycle_statuses_keep_model_outcome_out_of_blocker() -> None:
    statuses = TeacherForcedBenchmarkE2ERunner._execution_lifecycle_statuses(
        {"scheduling_timeouts": 0},
        scenario_completed=True,
        single_arm_result_count=1,
        checkpoint_count=1,
        generation_quality_eligible=True,
        retrieval_quality_eligible=True,
    )
    assert statuses["checkpoint_scenario_status"] == "COMPLETED"
    assert statuses["single_arm_evaluation_status"] == "COMPLETED"
    assert statuses["semantic_quality_eligible"] is True
    assert statuses["quality_blocker"] == ""


def test_execution_lifecycle_statuses_keep_partial_and_absent_states_typed() -> None:
    statuses = TeacherForcedBenchmarkE2ERunner._execution_lifecycle_statuses(
        {"scheduling_timeouts": 0},
        scenario_completed=False,
        single_arm_result_count=0,
        checkpoint_count=1,
        generation_quality_eligible=False,
        retrieval_quality_eligible=False,
    )
    assert statuses["checkpoint_scenario_status"] == "INCOMPLETE"
    assert statuses["single_arm_evaluation_status"] == "NOT_RUN"
    assert statuses["semantic_quality_eligible"] is False
    assert statuses["quality_blocker"]


def test_immutable_evidence_writer_rejects_conflicting_existing_bytes(tmp_path: Path) -> None:
    path = tmp_path / "immutable.json"
    TeacherForcedBenchmarkE2ERunner._write_immutable(path, b"first")
    TeacherForcedBenchmarkE2ERunner._write_immutable(path, b"first")
    with pytest.raises(TeacherForcedBenchmarkError, match="refusing to overwrite different"):
        TeacherForcedBenchmarkE2ERunner._write_immutable(path, b"second")


def test_ancestry_proof_fails_closed_without_state(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "proof-objects"))
    paired = MagicMock()
    transition = MagicMock(
        bundle=bundle,
        states={},
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        commits=MagicMock(),
    )
    freezer = _E2EContextFreezer(MagicMock(), transition, artifacts, paired)
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        freezer,
        artifacts,
        paired,
    )
    comparison = MagicMock(freeze_receipt=MagicMock(code_version="v1"))
    with pytest.raises(TeacherForcedBenchmarkError, match="checkpoint state missing"):
        evaluator._build_ancestry_proof(case, comparison)


def test_ancestry_proof_fails_closed_without_commit_service(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    world = bundle.world_roots[0]
    state = _FrozenState(
        text=bundle.text_roots[0],
        world=world,
        plan=bundle.plan_roots[0],
        commit=world.source_commit,
        text_root_ref=TextRootRef(
            artifact_id=bundle.text_roots[0].root_hash,
            media_type="application/json",
            byte_length=1,
            schema_version=SchemaVersion("1.0.0"),
        ),
    )
    transition = MagicMock(
        bundle=bundle,
        states={case.case_id: state},
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        commits=None,
    )
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "proof-objects-4"))
    paired = MagicMock()
    freezer = _E2EContextFreezer(MagicMock(), transition, artifacts, paired)
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        freezer,
        artifacts,
        paired,
    )
    comparison = MagicMock(freeze_receipt=MagicMock(code_version="v1"))
    with pytest.raises(TeacherForcedBenchmarkError, match="commit service unavailable"):
        evaluator._build_ancestry_proof(case, comparison)


def test_ancestry_proof_fails_closed_on_broken_chain(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    world = bundle.world_roots[0]
    state = _FrozenState(
        text=bundle.text_roots[0],
        world=world,
        plan=bundle.plan_roots[0],
        commit=world.source_commit,
        text_root_ref=TextRootRef(
            artifact_id=bundle.text_roots[0].root_hash,
            media_type="application/json",
            byte_length=1,
            schema_version=SchemaVersion("1.0.0"),
        ),
    )
    commits = MagicMock()
    commits.load_manifest.side_effect = RuntimeError("chain broken")
    transition = MagicMock(
        bundle=bundle,
        states={case.case_id: state},
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        commits=commits,
    )
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "proof-objects-2"))
    paired = MagicMock()
    freezer = _E2EContextFreezer(MagicMock(), transition, artifacts, paired)
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        freezer,
        artifacts,
        paired,
    )
    comparison = MagicMock(freeze_receipt=MagicMock(code_version="v1"))
    with pytest.raises(TeacherForcedBenchmarkError):
        evaluator._build_ancestry_proof(case, comparison)


def test_ancestry_proof_typed_failure_on_multi_parent_chain(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    world = bundle.world_roots[0]
    state = _FrozenState(
        text=bundle.text_roots[0],
        world=world,
        plan=bundle.plan_roots[0],
        commit=world.source_commit,
        text_root_ref=TextRootRef(
            artifact_id=bundle.text_roots[0].root_hash,
            media_type="application/json",
            byte_length=1,
            schema_version=SchemaVersion("1.0.0"),
        ),
    )
    manifest = MagicMock(
        text_root=MagicMock(artifact_id=bundle.text_roots[0].root_hash),
        parent_commit_ids=(CommitId("sha256:" + "e" * 64), CommitId("sha256:" + "f" * 64)),
    )
    commits = MagicMock()
    commits.load_manifest.return_value = manifest
    transition = MagicMock(
        bundle=bundle,
        states={case.case_id: state},
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        commits=commits,
    )
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "proof-objects-5"))
    paired = MagicMock()
    freezer = _E2EContextFreezer(MagicMock(), transition, artifacts, paired)
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        freezer,
        artifacts,
        paired,
    )
    comparison = MagicMock(freeze_receipt=MagicMock(code_version="v1"))
    with pytest.raises(TeacherForcedBenchmarkError, match="single-parent"):
        evaluator._build_ancestry_proof(case, comparison)


def test_runtime_entity_label_map_unique_alias_and_ambiguous(tmp_path: Path) -> None:
    from novel_agent.domain.memory import WorldRootDocument

    bundle = make_synthetic_bundle()
    oracle = bundle.world_roots[0]
    entity = oracle.entities[0]
    twin = entity.model_copy(update={"entity_id": StableId("entity.runtime.twin"), "aliases": ()})
    aliased = entity.model_copy(
        update={"entity_id": StableId("entity.runtime.aliased"), "aliases": ("别名",)}
    )
    runtime_world = WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "5" * 64),
        schema_version=oracle.schema_version,
        source_commit=oracle.source_commit,
        entities=(entity, twin, aliased),
    )
    by_label, ambiguous = _E2EEvaluator._runtime_entity_label_map(runtime_world)
    assert entity.internal_label in ambiguous
    assert by_label.get("别名") == StableId("entity.runtime.aliased")


def test_runtime_entity_label_map_skips_blank_labels(tmp_path: Path) -> None:
    from novel_agent.domain.memory import WorldRootDocument

    bundle = make_synthetic_bundle()
    oracle = bundle.world_roots[0]
    entity = oracle.entities[0]
    blank = entity.model_copy(
        update={
            "entity_id": StableId("entity.runtime.blank"),
            "internal_label": "",
            "aliases": ("",),
        }
    )
    runtime_world = WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "4" * 64),
        schema_version=oracle.schema_version,
        source_commit=oracle.source_commit,
        entities=(blank,),
    )
    by_label, ambiguous = _E2EEvaluator._runtime_entity_label_map(runtime_world)
    assert by_label == {}
    assert ambiguous == set()


def test_ancestry_proof_typed_failure_on_cycle(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    world = bundle.world_roots[0]
    text = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "proof-objects-6"))
    text_ref = artifacts.put(
        canonical_json_bytes(text.model_dump(mode="json")),
        "application/vnd.novel-agent.text-root+json",
        SchemaVersion("1.0.0"),
    )
    state = _FrozenState(
        text=text,
        world=world,
        plan=bundle.plan_roots[0],
        commit=world.source_commit,
        text_root_ref=text_ref,
    )
    manifest = MagicMock(
        text_root=text_ref,
        parent_commit_ids=(world.source_commit,),
    )
    commits = MagicMock()
    commits.load_manifest.return_value = manifest
    transition = MagicMock(
        bundle=bundle,
        states={case.case_id: state},
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        commits=commits,
    )
    paired = MagicMock()
    freezer = _E2EContextFreezer(MagicMock(), transition, artifacts, paired)
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        freezer,
        artifacts,
        paired,
    )
    comparison = MagicMock(freeze_receipt=MagicMock(code_version="v1"))
    with pytest.raises(TeacherForcedBenchmarkError, match="cycle"):
        evaluator._build_ancestry_proof(case, comparison)


def test_ancestry_proof_typed_failure_on_missing_text_root_ref(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    world = bundle.world_roots[0]
    text = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "proof-objects-7"))
    text_ref = artifacts.put(
        canonical_json_bytes(text.model_dump(mode="json")),
        "application/vnd.novel-agent.text-root+json",
        SchemaVersion("1.0.0"),
    )
    state = _FrozenState(
        text=text,
        world=world,
        plan=bundle.plan_roots[0],
        commit=world.source_commit,
        text_root_ref=None,
    )
    manifest = MagicMock(
        text_root=text_ref,
        parent_commit_ids=(),
    )
    commits = MagicMock()
    commits.load_manifest.return_value = manifest
    transition = MagicMock(
        bundle=bundle,
        states={case.case_id: state},
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        commits=commits,
    )
    paired = MagicMock()
    freezer = _E2EContextFreezer(MagicMock(), transition, artifacts, paired)
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        freezer,
        artifacts,
        paired,
    )
    comparison = MagicMock(freeze_receipt=MagicMock(code_version="v1"))
    with pytest.raises(TeacherForcedBenchmarkError, match="lacks its TextRoot artifact ref"):
        evaluator._build_ancestry_proof(case, comparison)


def test_ancestry_proof_typed_failure_on_missing_compiled_root(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    world = bundle.world_roots[0]
    state = _FrozenState(
        text=bundle.text_roots[0],
        world=world,
        plan=bundle.plan_roots[0],
        commit=world.source_commit,
        text_root_ref=TextRootRef(
            artifact_id=bundle.text_roots[0].root_hash,
            media_type="application/json",
            byte_length=1,
            schema_version=SchemaVersion("1.0.0"),
        ),
    )
    text = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "proof-objects-8"))
    text_ref = artifacts.put(
        canonical_json_bytes(text.model_dump(mode="json")),
        "application/vnd.novel-agent.text-root+json",
        SchemaVersion("1.0.0"),
    )
    state = _FrozenState(
        text=text,
        world=world,
        plan=bundle.plan_roots[0],
        commit=world.source_commit,
        text_root_ref=text_ref,
    )
    manifest = MagicMock(
        text_root=text_ref,
        parent_commit_ids=(),
    )
    commits = MagicMock()
    commits.load_manifest.return_value = manifest
    case_missing = case.model_copy(update={"input_text_root": ArtifactId("sha256:" + "9" * 64)})
    bundle_missing = bundle.model_copy(
        update={
            "case_manifests": tuple(
                case_missing if item.case_id == case.case_id else item
                for item in bundle.case_manifests
            )
        }
    )
    transition = MagicMock(
        bundle=bundle_missing,
        states={case_missing.case_id: state},
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        commits=commits,
    )
    paired = MagicMock()
    freezer = _E2EContextFreezer(MagicMock(), transition, artifacts, paired)
    evaluator = _E2EEvaluator(
        bundle_missing,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        freezer,
        artifacts,
        paired,
    )
    comparison = MagicMock(freeze_receipt=MagicMock(code_version="v1"))
    with pytest.raises(TeacherForcedBenchmarkError, match="compiled historical TextRoot missing"):
        evaluator._build_ancestry_proof(case_missing, comparison)


def test_ancestry_proof_typed_failure_on_unreadable_manifest_root(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    world = bundle.world_roots[0]
    text = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "proof-objects-9"))
    broken_ref = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "0" * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )
    state = _FrozenState(
        text=text,
        world=world,
        plan=bundle.plan_roots[0],
        commit=world.source_commit,
        text_root_ref=broken_ref,
    )
    manifest = MagicMock(
        text_root=broken_ref,
        parent_commit_ids=(),
    )
    commits = MagicMock()
    commits.load_manifest.return_value = manifest
    transition = MagicMock(
        bundle=bundle,
        states={case.case_id: state},
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        commits=commits,
    )
    paired = MagicMock()
    freezer = _E2EContextFreezer(MagicMock(), transition, artifacts, paired)
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        freezer,
        artifacts,
        paired,
    )
    comparison = MagicMock(freeze_receipt=MagicMock(code_version="v1"))
    with pytest.raises(TeacherForcedBenchmarkError, match="cannot resolve manifest TextRoot"):
        evaluator._build_ancestry_proof(case, comparison)


def test_ancestry_proof_typed_failure_on_missing_case_manifest(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    world = bundle.world_roots[0]
    text = bundle.text_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "proof-objects-10"))
    text_ref = artifacts.put(
        canonical_json_bytes(text.model_dump(mode="json")),
        "application/vnd.novel-agent.text-root+json",
        SchemaVersion("1.0.0"),
    )
    state = _FrozenState(
        text=text,
        world=world,
        plan=bundle.plan_roots[0],
        commit=world.source_commit,
        text_root_ref=text_ref,
    )
    manifest = MagicMock(
        text_root=text_ref,
        parent_commit_ids=(),
    )
    commits = MagicMock()
    commits.load_manifest.return_value = manifest
    ghost_case = case.model_copy(update={"case_id": StableId("case.ZTJ-GHOST")})
    transition = MagicMock(
        bundle=bundle,
        states={ghost_case.case_id: state},
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        commits=commits,
    )
    paired = MagicMock()
    freezer = _E2EContextFreezer(MagicMock(), transition, artifacts, paired)
    evaluator = _E2EEvaluator(
        bundle,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        freezer,
        artifacts,
        paired,
    )
    comparison = MagicMock(freeze_receipt=MagicMock(code_version="v1"))
    with pytest.raises(TeacherForcedBenchmarkError, match="case manifest missing"):
        evaluator._build_ancestry_proof(ghost_case, comparison)
