"""Fail-closed validators that the quality gate still reports as uncovered."""

from __future__ import annotations

from typing import Any, cast

import pytest

from novel_agent.adapters.runtime.materializers import PlanCandidateMaterializer
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CandidateBinding,
    CandidateKind,
    CreativeRunPolicy,
    CreativeRunRequest,
    ExtendBudgetCommand,
)
from novel_agent.domain.generation import RecentProseContext
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.model_calls import BudgetSource, EffectiveBudgetResult
from novel_agent.domain.runtime import TaskKind, TaskPurpose, TaskRecord, TaskStatus
from novel_agent.domain.stage2 import ProposalProvenance, ProposedItem
from novel_agent.domain.stage5_evaluation import Stage5VerticalRunReport, VerticalRunStatus
from novel_agent.domain.stage5_manifest import Stage5DevelopmentManifest, Stage5FeatureAdmission

HASH = "sha256:" + "1" * 64
COMMIT = CommitId("sha256:" + "a" * 64)


def _ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "a" * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )


def test_creative_run_policy_lookahead_requires_parallelism() -> None:
    with pytest.raises(ValueError, match="Planner lookahead requires runtime parallelism 2"):
        CreativeRunPolicy(
            automation_mode=AutomationMode.MANUAL,
            policy_hash=HASH,
            permission_hash=HASH,
            enable_planner_lookahead=True,
            runtime_parallelism=1,
        )


def test_creative_run_request_rejects_non_increasing_target() -> None:
    policy = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=HASH,
    )
    with pytest.raises(ValueError, match="target chapter must follow"):
        CreativeRunRequest(
            run_id=RunId("run.range"),
            project_id=ProjectId("project.range"),
            basis_commit=COMMIT,
            policy=policy,
            current_chapter=5,
            target_chapters=5,
        )


def test_candidate_binding_lookahead_and_draft_impact_guards() -> None:
    ref = _ref()
    with pytest.raises(ValueError, match="lookahead candidate requires"):
        CandidateBinding(
            candidate_id=StableId("candidate.lookahead"),
            kind=CandidateKind.DRAFT,
            artifact_ref=ref,
            candidate_hash=ref.artifact_id.root,
            basis_commit=COMMIT,
            planning_purpose=TaskPurpose.LOOKAHEAD,
        )
    with pytest.raises(ValueError, match="future-Plan impact belongs only to a Draft"):
        CandidateBinding(
            candidate_id=StableId("candidate.plan-impact"),
            kind=CandidateKind.PLAN,
            artifact_ref=ref,
            candidate_hash=ref.artifact_id.root,
            basis_commit=COMMIT,
            affects_future_plan=True,
        )


def test_extend_budget_requires_an_extension() -> None:
    with pytest.raises(ValueError, match="must add a retry or Planner Memory tranche"):
        ExtendBudgetCommand(
            command_id=StableId("cmd.extend"),
            run_id=RunId("run.extend"),
            task_id=TaskId("task.extend"),
            actor_id="operator",
            reason="no-op",
        )


def test_effective_budget_result_identity_guards() -> None:
    with pytest.raises(ValueError, match="reserved sequence tokens must equal"):
        EffectiveBudgetResult(
            budget_source=BudgetSource.EXPLICIT_REQUEST,
            context_limit=10_000,
            estimated_input_tokens=100,
            body_output_budget=50,
            thinking_budget=0,
            total_output_budget=50,
            safety_allowance_tokens=10,
            reserved_sequence_tokens=1,
            available_input_tokens=9_940,
        )
    with pytest.raises(ValueError, match="available input tokens contradict"):
        EffectiveBudgetResult(
            budget_source=BudgetSource.EXPLICIT_REQUEST,
            context_limit=10_000,
            estimated_input_tokens=100,
            body_output_budget=50,
            thinking_budget=0,
            total_output_budget=50,
            safety_allowance_tokens=10,
            reserved_sequence_tokens=160,
            available_input_tokens=1,
        )


def test_task_record_horizon_and_purpose_guards() -> None:
    with pytest.raises(ValueError, match="horizon bounds must appear together"):
        TaskRecord(
            task_id=TaskId("task.horizon"),
            run_id=RunId("run.horizon"),
            project_id=ProjectId("project.horizon"),
            kind=TaskKind.PLAN_CANDIDATE,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=COMMIT,
            policy_hash=HASH,
            permission_hash=HASH,
            horizon_start=21,
        )
    with pytest.raises(ValueError, match="lookahead requires a future Plan horizon"):
        TaskRecord(
            task_id=TaskId("task.lookahead"),
            run_id=RunId("run.lookahead"),
            project_id=ProjectId("project.lookahead"),
            kind=TaskKind.DRAFT_CANDIDATE,
            purpose=TaskPurpose.LOOKAHEAD,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=COMMIT,
            policy_hash=HASH,
            permission_hash=HASH,
            horizon_start=21,
            horizon_end=25,
            protected_chapter_index=20,
        )
    with pytest.raises(ValueError, match="future-Plan impact belongs only"):
        TaskRecord(
            task_id=TaskId("task.impact"),
            run_id=RunId("run.impact"),
            project_id=ProjectId("project.impact"),
            kind=TaskKind.PLAN_CANDIDATE,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=COMMIT,
            policy_hash=HASH,
            permission_hash=HASH,
            affects_future_plan=True,
        )


def test_stage5_manifest_adapter_admission_guards() -> None:
    base = {
        "runtime_contract_version": SchemaVersion("1.0.0"),
        "stage2_base_commit": "a" * 40,
        "stage2_schema_fingerprint": HASH,
        "stage3_commit": "b" * 40,
        "stage3_contract_fingerprint": HASH,
        "stage4_port_fingerprint": HASH,
        "commit_projection_contract_version": SchemaVersion("1.0.0"),
        "commit_projection_fingerprint": HASH,
        "artifact_runtime_fingerprint": HASH,
        "configuration_fingerprint": HASH,
        "model_admission_fingerprint": HASH,
        "skill_registry_fingerprint": HASH,
        "projection_contract_fingerprint": HASH,
    }
    construct_manifest = cast(Any, Stage5DevelopmentManifest.model_construct)
    deferred = construct_manifest(
        **base,
        stage4_implementation_status="DEFERRED",
        feature_admission=Stage5FeatureAdmission(real_stage4_adapter=True),
    )
    with pytest.raises(ValueError, match="deferred Stage 4 adapter cannot be admitted"):
        deferred.validate_isolated_kernel()
    integrated = construct_manifest(
        **base,
        stage4_implementation_status="INTEGRATED",
        feature_admission=Stage5FeatureAdmission(),
    )
    with pytest.raises(ValueError, match="integrated Stage 4 adapter must be admitted"):
        integrated.validate_isolated_kernel()


def test_vertical_run_report_generated_chapter_count() -> None:
    report = Stage5VerticalRunReport(
        run_id=RunId("run.vertical"),
        project_id=ProjectId("project.vertical"),
        current_chapter=2,
        target_chapter=3,
        status=VerticalRunStatus.COMPLETED,
        final_commit=COMMIT,
        completed_chapters=(1, 2),
        runtime_results=(),
        tasks=(),
        outputs_frozen=True,
    )
    assert report.generated_chapter_count == 2


def test_recent_prose_checkpoint_zero_rejects_chapters() -> None:
    from novel_agent.domain.generation import RecentChapterProse

    chapter = RecentChapterProse(
        chapter_id=StableId("chapter.1"),
        chapter_index=1,
        title="One",
        full_text_artifact=_ref(),
        full_text_characters=4,
        compact_trail="trail",
    )
    with pytest.raises(ValueError, match="chapter-zero"):
        RecentProseContext(
            context_id=StableId("recent.zero"),
            base_commit=COMMIT,
            snapshot_id=StableId("snapshot.zero"),
            checkpoint_chapter=0,
            previous_chapter=chapter,
        )


def test_plan_item_mapping_guards() -> None:
    item = ProposedItem(
        item_id=StableId("item.plan"),
        kind="goal",
        payload={"summary": "ok", "title": "ok"},
        provenance=ProposalProvenance.PLANNER_PROPOSED,
    )
    with pytest.raises(Exception, match="must be a string list"):
        PlanCandidateMaterializer._ids(["not", 1], "obligation_ids")
    assert PlanCandidateMaterializer._ids(None, "obligation_ids") == ()
    empty_summary = item.model_copy(update={"payload": {"summary": " ", "title": "ok"}})
    with pytest.raises(Exception, match="non-empty summary"):
        PlanCandidateMaterializer._node(empty_summary)
    bad_title = item.model_copy(update={"payload": {"summary": "ok", "title": " "}})
    with pytest.raises(Exception, match="title must be a non-empty string"):
        PlanCandidateMaterializer._node(bad_title)
    bad_parent = item.model_copy(
        update={"payload": {"summary": "ok", "title": "ok", "parent_id": 1}}
    )
    with pytest.raises(Exception, match="parent_id must be a string"):
        PlanCandidateMaterializer._node(bad_parent)
    assert PlanCandidateMaterializer._chapter_goal(item) is None
    with pytest.raises(Exception, match="chapter_index must be an integer"):
        PlanCandidateMaterializer._chapter_goal(
            item.model_copy(update={"payload": {"chapter_index": True, "summary": "ok"}})
        )
    with pytest.raises(Exception, match="Chapter goal requires a non-empty summary"):
        PlanCandidateMaterializer._chapter_goal(
            item.model_copy(update={"payload": {"chapter_index": 21, "summary": " "}})
        )
    live = item.model_copy(
        update={
            "kind": "chapter_goal",
            "payload": {
                "chapter": 21,
                "goal": "enter the academy",
                "end_state": "arrived",
                "obligations": ["Goal 2: knowledge vs weakness"],
            },
        }
    )
    node = PlanCandidateMaterializer._node(live)
    assert node.summary == "enter the academy"
    assert node.title == "enter the academy"
    goal = PlanCandidateMaterializer._chapter_goal(live)
    assert goal is not None
    assert goal.chapter_index == 21
    assert goal.summary == "enter the academy"
    assert goal.obligation_ids == ()
    with pytest.raises(Exception, match="chapter_index must be an integer"):
        PlanCandidateMaterializer._chapter_goal(
            item.model_copy(update={"payload": {"chapter": True, "goal": "ok"}})
        )
