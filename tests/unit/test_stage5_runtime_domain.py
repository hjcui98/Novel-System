from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    AcceptanceCommand,
    AcceptanceDecision,
    AcceptanceReceipt,
    AcceptedCandidateBinding,
    ActorKind,
    AutomationMode,
    CandidateBinding,
    CandidateKind,
    CreativeRunPolicy,
    CreativeTaskSpec,
    PlanningLoopResult,
    PlanningTerminalStatus,
    next_task_kind,
    validate_successor,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.runtime import (
    STAGE5_EVENT_SCHEMA_VERSION,
    EvaluationDecision,
    EvaluationEntry,
    FailureClass,
    RunEvent,
    RunEventType,
    TaskCreatedPayload,
    TaskKind,
    TaskRecord,
    TaskStatus,
    evaluate_task_eligibility,
    failure_policy,
)
from novel_agent.domain.stage5_manifest import (
    Stage5DevelopmentManifest,
    Stage5FeatureAdmission,
)
from novel_agent.domain.world import PlanLevel
from novel_agent.runtime.creative_assembly import validate_runtime_assembly

HASH = "sha256:" + "1" * 64
COMMIT = CommitId("sha256:" + "2" * 64)
NOW = datetime(2026, 8, 10, tzinfo=UTC)


def _artifact(digit: str = "3") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + digit * 64),
        media_type="application/json",
        byte_length=2,
        schema_version=SchemaVersion("1.0.0"),
    )


def _task(**updates: object) -> TaskRecord:
    base = TaskRecord(
        task_id=TaskId("task.plan"),
        run_id=RunId("run.stage5"),
        project_id=ProjectId("project.test"),
        kind=TaskKind.PLAN_CANDIDATE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=COMMIT,
        policy_hash=HASH,
        permission_hash=HASH,
    )
    return base.model_copy(update=updates)


def test_task_eligibility_has_one_fail_closed_definition() -> None:
    eligible = evaluate_task_eligibility(
        _task(),
        now=NOW,
        current_commit=COMMIT,
        dependency_statuses=(),
        permission_hash=HASH,
        writer_generation=0,
    )
    assert eligible.eligible and eligible.status is TaskStatus.READY

    cases = (
        (_task(status=TaskStatus.RUNNING), (), COMMIT, HASH, 0, "status_not_claimable"),
        (
            _task(current_attempt_id=StableId("attempt.current")),
            (),
            COMMIT,
            HASH,
            0,
            "attempt_active",
        ),
        (_task(paused=True), (), COMMIT, HASH, 0, "paused_or_superseded"),
        (_task(superseded=True), (), COMMIT, HASH, 0, "paused_or_superseded"),
        (_task(failure_budget=0), (), COMMIT, HASH, 0, "failure_budget_exhausted"),
        (
            _task(status=TaskStatus.BUDGET_REVIEW, failure_budget=0),
            (),
            COMMIT,
            HASH,
            0,
            "budget_extension_required",
        ),
        (
            _task(scheduled_for=NOW + timedelta(seconds=1)),
            (),
            COMMIT,
            HASH,
            0,
            "scheduled_for_future",
        ),
        (_task(), (TaskStatus.BLOCKED,), COMMIT, HASH, 0, "dependency_not_succeeded"),
        (
            _task(),
            (),
            CommitId("sha256:" + "4" * 64),
            HASH,
            0,
            "basis_changed",
        ),
        (_task(), (), COMMIT, "sha256:" + "5" * 64, 0, "permission_changed"),
        (_task(), (), COMMIT, HASH, 1, "writer_generation_changed"),
    )
    for task, dependencies, commit, permission, generation, reason in cases:
        decision = evaluate_task_eligibility(
            task,
            now=NOW,
            current_commit=commit,
            dependency_statuses=dependencies,
            permission_hash=permission,
            writer_generation=generation,
        )
        assert not decision.eligible and decision.reason_code == reason


def test_failure_policy_mapping_is_exhaustive_and_owned() -> None:
    policies = {item: failure_policy(item) for item in FailureClass}
    assert set(policies) == set(FailureClass)
    assert policies[FailureClass.PROJECTION_FAILED].retryable
    assert not policies[FailureClass.COMMIT_CONFLICT].retryable
    assert policies[FailureClass.VALIDATION_REJECTED].consumes_task_budget


def test_fixed_topology_cannot_skip_acceptance_or_freshness() -> None:
    succeeded = _task(status=TaskStatus.SUCCEEDED)
    assert next_task_kind(succeeded) is TaskKind.PLAN_ACCEPTANCE
    successor = CreativeTaskSpec(
        task_id=TaskId("task.accept"),
        kind=TaskKind.PLAN_ACCEPTANCE,
        basis_commit=COMMIT,
        dependency_task_ids=(succeeded.task_id,),
    )
    validate_successor(succeeded, successor)
    with pytest.raises(ValueError, match="skips"):
        validate_successor(
            succeeded,
            successor.model_copy(update={"kind": TaskKind.PLAN_COMMIT}),
        )
    with pytest.raises(ValueError, match="basis"):
        validate_successor(
            succeeded,
            successor.model_copy(update={"basis_commit": CommitId("sha256:" + "6" * 64)}),
        )
    with pytest.raises(ValueError, match="succeeded"):
        next_task_kind(_task())
    with pytest.raises(ValueError, match="no automatic successor"):
        next_task_kind(_task(status=TaskStatus.SUCCEEDED, kind=TaskKind.MAINTENANCE))
    projection = _task(status=TaskStatus.SUCCEEDED, kind=TaskKind.PROJECTION_FRESHNESS)
    assert (
        next_task_kind(projection, after_projection=CandidateKind.PLAN) is TaskKind.DRAFT_CANDIDATE
    )
    story_projection = projection.model_copy(update={"plan_level": PlanLevel.STORY})
    assert (
        next_task_kind(story_projection, after_projection=CandidateKind.PLAN)
        is TaskKind.PLAN_CANDIDATE
    )
    volume_projection = projection.model_copy(update={"plan_level": PlanLevel.ARC_VOLUME})
    assert (
        next_task_kind(volume_projection, after_projection=CandidateKind.PLAN)
        is TaskKind.PLAN_CANDIDATE
    )
    assert (
        next_task_kind(projection, after_projection=CandidateKind.DRAFT) is TaskKind.DRAFT_CANDIDATE
    )
    with pytest.raises(ValueError, match="requires"):
        next_task_kind(projection)


def test_candidate_acceptance_and_planner_terminals_are_strict() -> None:
    artifact = _artifact()
    candidate = CandidateBinding(
        candidate_id=StableId("candidate.plan"),
        kind=CandidateKind.PLAN,
        artifact_ref=artifact,
        candidate_hash=artifact.artifact_id.root,
        basis_commit=COMMIT,
    )
    with pytest.raises(ValidationError, match="candidate hash"):
        CandidateBinding(
            candidate_id=StableId("candidate.bad"),
            kind=CandidateKind.PLAN,
            artifact_ref=artifact,
            candidate_hash="sha256:" + "7" * 64,
            basis_commit=COMMIT,
        )
    command = AcceptanceCommand(
        command_id=StableId("command.accept"),
        project_id=ProjectId("project.test"),
        run_id=RunId("run.stage5"),
        task_id=TaskId("task.accept"),
        candidate=candidate,
        acceptance_policy_hash=HASH,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        decision=AcceptanceDecision.ACCEPT,
        reason="approved",
        expected_project_commit=COMMIT,
        idempotency_identity=StableId("accept.identity"),
        issued_at=NOW,
    )
    assert command.kind == "acceptance"
    with pytest.raises(ValidationError, match="candidate basis"):
        AcceptanceCommand.model_validate(
            {
                **command.model_dump(mode="python"),
                "expected_project_commit": CommitId("sha256:" + "8" * 64),
            }
        )
    ready = PlanningLoopResult(
        result_id=StableId("planner.result"),
        run_id=RunId("run.stage5"),
        task_id=TaskId("task.plan"),
        status=PlanningTerminalStatus.PLAN_CANDIDATE_READY,
        candidate=candidate,
    )
    assert ready.candidate == candidate
    with pytest.raises(ValidationError, match="requires typed failure"):
        PlanningLoopResult(
            result_id=StableId("planner.bad"),
            run_id=RunId("run.stage5"),
            task_id=TaskId("task.plan"),
            status=PlanningTerminalStatus.BLOCKED,
        )
    with pytest.raises(ValidationError, match="only PLAN_CANDIDATE_READY"):
        PlanningLoopResult(
            result_id=StableId("planner.blocked-with-candidate"),
            run_id=RunId("run.stage5"),
            task_id=TaskId("task.plan"),
            status=PlanningTerminalStatus.BLOCKED,
            candidate=candidate,
            failure_code="blocked",
            failure_detail="blocked",
        )
    with pytest.raises(ValidationError, match="cannot carry a failure"):
        PlanningLoopResult(
            result_id=StableId("planner.ready-with-failure"),
            run_id=RunId("run.stage5"),
            task_id=TaskId("task.plan"),
            status=PlanningTerminalStatus.PLAN_CANDIDATE_READY,
            candidate=candidate,
            failure_code="impossible",
        )

    binding = AcceptedCandidateBinding(
        acceptance_id=StableId("acceptance.domain"),
        command_id=command.command_id,
        project_id=command.project_id,
        run_id=command.run_id,
        task_id=command.task_id,
        candidate=candidate,
        actor_kind=command.actor_kind,
        actor_id=command.actor_id,
        accepted_at=NOW,
        expected_project_commit=COMMIT,
    )
    receipt_data = {
        "receipt_id": StableId("receipt.domain"),
        "command_id": command.command_id,
        "idempotency_identity": command.idempotency_identity,
        "command_hash": HASH,
        "decision": AcceptanceDecision.ACCEPT,
        "candidate": candidate,
        "accepted_binding": binding,
        "reason": "accepted",
        "recorded_at": NOW,
    }
    assert AcceptanceReceipt.model_validate(receipt_data).accepted_binding == binding
    with pytest.raises(ValidationError, match="only accepted decisions"):
        AcceptanceReceipt.model_validate({**receipt_data, "decision": AcceptanceDecision.REJECT})


def test_modes_only_enable_pinned_auto_acceptance() -> None:
    manual = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=HASH,
    )
    assert not manual.auto_accept_plan
    with pytest.raises(ValidationError, match="only auto"):
        manual.model_copy(update={"auto_accept_plan": True}).model_validate(
            {**manual.model_dump(), "auto_accept_plan": True}
        )
    automatic = CreativeRunPolicy(
        automation_mode=AutomationMode.AUTO,
        policy_hash=HASH,
        permission_hash=HASH,
        auto_accept_plan=True,
        auto_accept_draft=True,
    )
    assert automatic.auto_accept_draft

    elastic = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=HASH,
        max_task_attempts=21,
        max_tasks_per_advance=11,
        planning_horizon=25,
        lookahead_horizon=13,
    )
    assert elastic.planning_horizon == 25


def test_commit_task_from_acceptance_rejects_unsettled_and_mismatched_receipts() -> None:
    from novel_agent.domain.creative_runtime import commit_task_from_acceptance

    artifact = _artifact("4")
    candidate = CandidateBinding(
        candidate_id=StableId("candidate.commit-task"),
        kind=CandidateKind.PLAN,
        artifact_ref=artifact,
        candidate_hash=artifact.artifact_id.root,
        basis_commit=COMMIT,
    )
    command = AcceptanceCommand(
        command_id=StableId("command.commit-task"),
        project_id=ProjectId("project.test"),
        run_id=RunId("run.stage5"),
        task_id=TaskId("task.accept.commit"),
        candidate=candidate,
        acceptance_policy_hash=HASH,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        decision=AcceptanceDecision.ACCEPT,
        reason="approved",
        expected_project_commit=COMMIT,
        idempotency_identity=StableId("accept.commit-task.identity"),
        issued_at=NOW,
    )
    binding = AcceptedCandidateBinding(
        acceptance_id=StableId("acceptance.commit-task"),
        command_id=command.command_id,
        project_id=command.project_id,
        run_id=command.run_id,
        task_id=command.task_id,
        candidate=candidate,
        actor_kind=command.actor_kind,
        actor_id=command.actor_id,
        accepted_at=NOW,
        expected_project_commit=COMMIT,
    )
    receipt = AcceptanceReceipt(
        receipt_id=StableId("receipt.commit-task"),
        command_id=command.command_id,
        idempotency_identity=command.idempotency_identity,
        command_hash=HASH,
        decision=AcceptanceDecision.ACCEPT,
        candidate=candidate,
        accepted_binding=binding,
        reason="accepted",
        recorded_at=NOW,
    )
    settled = _task(
        task_id=TaskId("task.accept.commit"),
        kind=TaskKind.PLAN_ACCEPTANCE,
        status=TaskStatus.SUCCEEDED,
        failure_budget=0,
        retry_tranche_size=7,
    )
    commit_task = commit_task_from_acceptance(settled, receipt)
    assert commit_task.kind is TaskKind.PLAN_COMMIT
    assert commit_task.failure_budget == 7
    long_commit_task = commit_task_from_acceptance(
        settled.model_copy(
            update={
                "task_id": TaskId("t" * 128),
                "run_id": RunId("r" * 128),
            }
        ),
        receipt,
    )
    assert long_commit_task.task_id.root == f"commit.{candidate.candidate_hash}"
    assert len(long_commit_task.task_id.root) <= 128
    with pytest.raises(ValueError, match="only an accepted, settled"):
        commit_task_from_acceptance(
            _task(task_id=TaskId("task.accept.commit"), kind=TaskKind.PLAN_ACCEPTANCE),
            receipt,
        )
    with pytest.raises(ValueError, match="fixed topology basis"):
        commit_task_from_acceptance(
            settled,
            receipt.model_copy(
                update={
                    "candidate": candidate.model_copy(
                        update={"basis_commit": CommitId("sha256:" + "5" * 64)}
                    )
                }
            ),
        )
    with pytest.raises(ValueError, match="fixed topology basis"):
        commit_task_from_acceptance(
            _task(
                task_id=TaskId("task.accept.commit"),
                kind=TaskKind.DRAFT_ACCEPTANCE,
                status=TaskStatus.SUCCEEDED,
            ),
            receipt,
        )


def test_evaluation_model_audit_is_all_or_nothing() -> None:
    from decimal import Decimal

    from novel_agent.domain.model_calls import ModelRole

    base = {
        "evaluation_id": StableId("evaluation.stage5"),
        "run_id": RunId("run.stage5"),
        "evaluator": "deterministic",
        "evaluator_version": "1",
        "rubric_version": "1",
        "decision": EvaluationDecision.INFORMATIONAL,
        "created_at": NOW,
    }
    assert EvaluationEntry.model_validate(base).model_role is None
    with pytest.raises(ValidationError, match="requires model_role"):
        EvaluationEntry.model_validate({**base, "model_endpoint": "endpoint"})
    with pytest.raises(ValidationError, match="requires complete"):
        EvaluationEntry.model_validate({**base, "model_role": ModelRole.IMPLEMENTATION})
    assert (
        EvaluationEntry.model_validate(
            {
                **base,
                "model_role": ModelRole.IMPLEMENTATION,
                "model_endpoint": "endpoint",
                "model_version": "v1",
                "model_cost_usd": Decimal("0"),
                "model_latency_ms": 0,
            }
        ).model_endpoint
        == "endpoint"
    )


def test_stage5_event_payload_registry_fails_closed() -> None:
    task = _task()
    payload = TaskCreatedPayload(task=task).model_dump(mode="json")
    event = RunEvent(
        event_id=StableId("event.task-created"),
        run_id=task.run_id,
        task_id=task.task_id,
        sequence_no=1,
        event_type=RunEventType.RUNTIME_TASK_CREATED,
        occurred_at=NOW,
        idempotency_identity=StableId("identity.task-created"),
        payload_schema_version=STAGE5_EVENT_SCHEMA_VERSION,
        trace_id="trace",
        payload=payload,
    )
    assert event.payload == payload
    with pytest.raises(ValidationError, match="unknown Stage 5"):
        event.model_copy(update={"payload_schema_version": SchemaVersion("2.0.0")}).model_validate(
            {**event.model_dump(mode="python"), "payload_schema_version": SchemaVersion("2.0.0")}
        )


def test_manifest_and_assembly_are_fail_closed() -> None:
    manifest = Stage5DevelopmentManifest(
        runtime_contract_version=SchemaVersion("1.0.0"),
        stage2_base_commit="a" * 40,
        stage2_schema_fingerprint=HASH,
        stage3_commit="b" * 40,
        stage3_contract_fingerprint=HASH,
        stage4_port_fingerprint=HASH,
        commit_projection_contract_version=SchemaVersion("1.0.0"),
        commit_projection_fingerprint=HASH,
        artifact_runtime_fingerprint=HASH,
        configuration_fingerprint=HASH,
        model_admission_fingerprint=HASH,
        skill_registry_fingerprint=HASH,
        projection_contract_fingerprint=HASH,
        feature_admission=Stage5FeatureAdmission(),
    )
    with pytest.raises(ValidationError, match="cannot be admitted"):
        Stage5DevelopmentManifest.model_validate(
            {
                **manifest.model_dump(mode="python"),
                "feature_admission": {"scheduled_fire": True},
            }
        )

    class Component:
        def __init__(self, fixture: bool) -> None:
            self.is_fixture = fixture

    validate_runtime_assembly(
        manifest,
        planner=Component(True),
        writer=Component(False),
        plan_materializer=Component(True),
        draft_materializer=Component(True),
        production=False,
    )
    with pytest.raises(RuntimeError, match="real Stage 4"):
        validate_runtime_assembly(
            manifest,
            planner=Component(False),
            writer=Component(False),
            plan_materializer=Component(False),
            draft_materializer=Component(False),
            production=True,
        )
    admitted = manifest.model_copy(
        update={
            "feature_admission": manifest.feature_admission.model_copy(
                update={"real_stage4_adapter": True}
            )
        }
    )
    with pytest.raises(RuntimeError, match="rejects fixture"):
        validate_runtime_assembly(
            admitted,
            planner=Component(False),
            writer=Component(False),
            plan_materializer=Component(True),
            draft_materializer=Component(False),
            production=True,
        )
    with pytest.raises(RuntimeError, match="strict fake Planner"):
        validate_runtime_assembly(
            manifest,
            planner=Component(False),
            writer=Component(False),
            plan_materializer=Component(True),
            draft_materializer=Component(True),
            production=False,
        )
    with pytest.raises(RuntimeError, match="real Stage 3"):
        validate_runtime_assembly(
            manifest,
            planner=Component(True),
            writer=Component(True),
            plan_materializer=Component(True),
            draft_materializer=Component(True),
            production=False,
        )
