"""State-level fail-closed branches for the Stage 2W workflow."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from novel_agent.domain.artifacts import RootKind
from novel_agent.domain.changes import ChangeOperation, ChangeOperationType
from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.memory_write import (
    BlockingScope,
    CandidateProducerKind,
    FindingRetryability,
    HumanApprovalDecision,
    HumanDecisionKind,
    MemoryWriteCandidatePayload,
    MemoryWriteState,
    MemoryWriteWorkflowStatus,
    NormalizationResult,
    NormalizationStatus,
    ProjectionReadinessResult,
    ProjectionReadinessStatus,
    RepairAction,
    RepairDirective,
    RepairScope,
    ValidationDisposition,
    ValidationFindingCategory,
    ValidationFindingV2,
    ValidationSeverity,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    GuardianDecision,
    GuardianOutcome,
    WriteGateOutcome,
)
from novel_agent.ports.memory_write import (
    CuratorRepairRejectedError,
    CuratorRepairResult,
    GuardianReviewResult,
    MemoryWriteCommitResult,
    MemoryWriteCommitStatus,
)
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.information_boundary import InformationBoundaryViolation
from novel_agent.services.memory_write_workflow import (
    MemoryWriteWorkflowError,
    _as_guardian_result,
    _validate_human_decision,
    _WorkflowData,
)
from tests.contract.test_memory_write_workflow_contract import BASE, _artifact
from tests.contract.test_stage2_contract import agent_receipt
from tests.unit.test_memory_write_workflow import _workflow_and_data
from tests.unit.test_teacher_forced_memory_write_adapters import _commit_request


def _step(data: _WorkflowData, workflow: Any) -> Any:
    return asyncio.run(workflow._step(data))


@pytest.mark.parametrize(
    ("state", "configure", "code"),
    (
        (
            MemoryWriteState.CURATE_ATTEMPT_PREPARE,
            lambda workflow, data: setattr(workflow, "_curator", None),
            "CURATOR_PORT_UNAVAILABLE",
        ),
        (
            MemoryWriteState.NORMALIZE,
            lambda workflow, data: setattr(workflow, "_normalizer", None),
            "NORMALIZER_PORT_UNAVAILABLE",
        ),
        (
            MemoryWriteState.MATERIALIZE,
            lambda workflow, data: setattr(workflow, "_root_updates", None),
            "ROOT_UPDATE_PORT_UNAVAILABLE",
        ),
    ),
)
def test_optional_state_ports_fail_closed(
    state: MemoryWriteState,
    configure: Any,
    code: str,
) -> None:
    workflow, data = _workflow_and_data()
    data.state = state
    configure(workflow, data)

    result = _step(data, workflow)

    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert code in result.terminal_codes


def test_materializer_exception_is_a_typed_fatal() -> None:
    workflow, data = _workflow_and_data()
    data.state = MemoryWriteState.MATERIALIZE

    class Broken:
        def materialize_atomic_bundle(self, **_: object) -> object:
            raise OSError("broken")

    workflow._root_updates = Broken()
    result = _step(data, workflow)
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert any("MATERIALIZATION_FAILED" in code for code in result.terminal_codes)


@pytest.mark.parametrize(
    ("state", "usage_field", "budget_field"),
    (
        (
            MemoryWriteState.CURATE_ATTEMPT_PREPARE,
            "total_model_calls",
            "max_total_model_calls",
        ),
        (MemoryWriteState.NORMALIZE, "normalization_passes", "max_normalization_passes"),
        (MemoryWriteState.REFRESH_SOURCE_CONTEXT, "context_refreshes", "max_context_refreshes"),
        (MemoryWriteState.CURATOR_REPAIR, "candidate_revisions", "max_candidate_revisions"),
        (MemoryWriteState.GUARDIAN, "total_model_calls", "max_total_model_calls"),
    ),
)
def test_state_budgets_stop_before_external_effects(
    state: MemoryWriteState,
    usage_field: str,
    budget_field: str,
) -> None:
    workflow, data = _workflow_and_data()
    data.state = state
    if state in {
        MemoryWriteState.CURATE_ATTEMPT_PREPARE,
        MemoryWriteState.CURATOR_REPAIR,
    }:
        workflow._curator = object()
    if state is MemoryWriteState.GUARDIAN:
        workflow._guardian = object()
    budget_value = getattr(data.request.budget, budget_field)
    data.usage = data.usage.model_copy(update={usage_field: budget_value})

    result = _step(data, workflow)

    assert result.status is MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED


@pytest.mark.parametrize(
    ("action", "expected_state"),
    (
        (RepairAction.DETERMINISTIC_REPAIR, MemoryWriteState.NORMALIZE),
        (RepairAction.CURATOR_REPAIR, MemoryWriteState.CURATOR_REPAIR),
        (RepairAction.GUARDIAN_REVIEW, MemoryWriteState.GUARDIAN),
        (RepairAction.RETRY_AFTER_SOURCE_CONTEXT_REFRESH, MemoryWriteState.REFRESH_SOURCE_CONTEXT),
        (RepairAction.HUMAN, MemoryWriteState.HUMAN_SUSPEND),
        (RepairAction.QUARANTINE_OPERATION, MemoryWriteState.QUARANTINE),
        (RepairAction.STOP_BUDGET_EXHAUSTED, MemoryWriteState.BUDGET_STOP),
        (RepairAction.REPLAN, MemoryWriteState.STOP),
        (RepairAction.STOP_FATAL, MemoryWriteState.STOP),
    ),
)
def test_repair_policy_action_mapping_is_total(
    action: RepairAction,
    expected_state: MemoryWriteState,
) -> None:
    workflow, data = _workflow_and_data()
    data.state = MemoryWriteState.REPAIR_POLICY
    directive = RepairDirective(
        directive_id=StableId(f"directive.{action.value}"),
        action=action,
        reason_codes=("test",) if action is RepairAction.STOP_BUDGET_EXHAUSTED else (),
    )
    workflow._repair_policy = type(
        "Policy",
        (),
        {"decide": lambda self, context: directive},
    )()

    assert _step(data, workflow) is None
    assert data.state is expected_state
    assert data.action_receipts[-1].action is action


def test_source_refresh_success_rechecks_boundary_and_returns_to_policy() -> None:
    workflow, data = _workflow_and_data()
    data.state = MemoryWriteState.REFRESH_SOURCE_CONTEXT
    data.basis = workflow._canonical_read.load_verified(
        data.request.project_id,
        data.request.base_commit,
    )
    data.directive = RepairDirective(
        directive_id=StableId("directive.refresh-source-context"),
        action=RepairAction.RETRY_AFTER_SOURCE_CONTEXT_REFRESH,
    )
    boundary: Any = workflow._boundary
    calls_before = boundary.calls

    assert _step(data, workflow) is None
    assert data.state is MemoryWriteState.REPAIR_POLICY
    assert data.directive is None
    assert boundary.calls == calls_before + 1


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        ({"candidate": None}, "PRECOMMIT_INCOMPLETE"),
        (
            {
                "validation": lambda data: data.validation.model_copy(
                    update={"disposition": ValidationDisposition.NON_REPAIRABLE}
                )
            },
            "PRECOMMIT_VALIDATION_NOT_PASS",
        ),
        ({"bundle": None}, "PRECOMMIT_BUNDLE_MISSING"),
        ({"risk": None}, "PRECOMMIT_RISK_MISSING"),
    ),
)
def test_precommit_requires_every_bound_input(mutation: dict[str, Any], code: str) -> None:
    workflow, data = _workflow_and_data()
    for field, value in mutation.items():
        setattr(data, field, value(data) if callable(value) else value)

    result = workflow._prepare_commit(data)

    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert code in result.terminal_codes


def test_precommit_replans_when_canonical_head_has_moved() -> None:
    workflow, data = _workflow_and_data()
    canonical: Any = workflow._canonical_read
    canonical.commit.current = CommitId("sha256:" + "9" * 64)

    assert workflow._prepare_commit(data) is None
    assert data.state is MemoryWriteState.STOP
    assert data.directive is not None
    assert data.directive.action is RepairAction.REPLAN


@pytest.mark.parametrize(
    ("outcome", "state"),
    (
        (WriteGateOutcome.REQUIRE_HUMAN, MemoryWriteState.HUMAN_SUSPEND),
        (WriteGateOutcome.BLOCK_VALIDATION, MemoryWriteState.STOP),
    ),
)
def test_precommit_non_allow_gate_routes_without_commit(
    outcome: WriteGateOutcome,
    state: MemoryWriteState,
) -> None:
    workflow, data = _workflow_and_data()

    class Gate:
        def decide(self, candidate: Any, validation: Any, risk: Any, guardian: Any = None) -> Any:
            del validation, guardian
            from novel_agent.domain.stage2 import WriteGateDecision

            return WriteGateDecision(
                decision_id=StableId("gate.edge"),
                change_set_id=candidate.candidate_id,
                base_commit=BASE,
                outcome=outcome,
                risk_assessment_id=risk.assessment_id,
                reasons=("blocked",),
            )

    workflow._write_gate = Gate()
    assert workflow._prepare_commit(data) is None
    assert data.state is state
    assert data.commit_request is None


def test_precommit_rejects_write_gate_binding_mismatch() -> None:
    workflow, data = _workflow_and_data()

    class Gate:
        def decide(self, candidate: Any, validation: Any, risk: Any, guardian: Any = None) -> Any:
            del candidate, validation, risk, guardian
            from novel_agent.domain.stage2 import WriteGateDecision

            return WriteGateDecision(
                decision_id=StableId("gate.bad"),
                change_set_id=StableId("candidate.other"),
                base_commit=BASE,
                outcome=WriteGateOutcome.ALLOW_COMMIT,
                risk_assessment_id=StableId("risk.other"),
            )

    workflow._write_gate = Gate()
    result = workflow._prepare_commit(data)
    assert result is not None
    assert "WRITE_GATE_BINDING_MISMATCH" in result.terminal_codes


def test_commit_missing_rejected_conflicted_and_uncertain_paths() -> None:
    workflow, data = _workflow_and_data()
    missing = workflow._commit(data)
    assert missing is not None
    assert "COMMIT_REQUEST_MISSING" in missing.terminal_codes

    data.commit_request = _commit_request()
    data.commit_effect_id = data.commit_request.commit_effect_id
    data.state = MemoryWriteState.COMMIT

    class Broken:
        def resolve_or_replay_exact(self, _: object) -> object:
            raise OSError("uncertain")

    workflow._commit_port = Broken()
    uncertain = workflow._commit(data)
    assert uncertain is not None
    assert uncertain.status is MemoryWriteWorkflowStatus.SUSPENDED

    for commit_result, expected in (
        (
            MemoryWriteCommitResult(
                request_id=data.request.request_id,
                status=MemoryWriteCommitStatus.CONFLICTED,
            ),
            MemoryWriteWorkflowStatus.REPLAN_REQUIRED,
        ),
        (
            MemoryWriteCommitResult(
                request_id=data.request.request_id,
                status=MemoryWriteCommitStatus.REJECTED,
                reason="rejected",
            ),
            MemoryWriteWorkflowStatus.FATAL,
        ),
    ):
        workflow._commit_port = type(
            "Commit",
            (),
            {"resolve_or_replay_exact": lambda self, request, result=commit_result: result},
        )()
        result = workflow._commit(data)
        assert result is not None
        assert result.status is expected


def test_dry_run_commit_refusal_becomes_typed_precommit_pause() -> None:
    workflow, data = _workflow_and_data()
    data.commit_request = _commit_request()
    data.commit_effect_id = data.commit_request.commit_effect_id
    data.checkpoint_ref = _artifact("d")
    data.state = MemoryWriteState.COMMIT
    commit_result = MemoryWriteCommitResult(
        request_id=data.request.request_id,
        status=MemoryWriteCommitStatus.DRY_RUN_REFUSED,
        reason="dry_run_refuses_all_commits",
    )
    workflow._commit_port = type(
        "DryRunCommit",
        (),
        {"resolve_or_replay_exact": lambda self, request: commit_result},
    )()

    result = workflow._commit(data)

    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.SUSPENDED
    assert result.workflow_phase.value == "precommit"
    assert result.canonical_commit_accepted is False
    assert result.resulting_commit is None
    assert data.candidate is not None
    assert result.accepted_candidate_id == data.candidate.candidate_id
    assert result.continuation_decision.value == "block_next_chapter"
    assert result.terminal_codes == (
        "DRY_RUN_COMMIT_REFUSED",
        "dry_run_refuses_all_commits",
    )


def test_projection_and_freshness_non_resumable_failures_preserve_commit() -> None:
    workflow, data = _workflow_and_data()
    data.commit_result = MemoryWriteCommitResult(
        request_id=data.request.request_id,
        status=MemoryWriteCommitStatus.ACCEPTED,
        commit_id=CommitId("sha256:" + "e" * 64),
        commit_receipt_ref=_artifact("f"),
    )

    class FailedProjection:
        def request_or_read_by_effect_id(self, *args: object) -> ProjectionReadinessResult:
            return ProjectionReadinessResult(
                effect_id=StableId("effect.failed"),
                status=ProjectionReadinessStatus.FAILED,
                resumable=False,
                reason="failed",
            )

        def await_or_check(self, *args: object) -> ProjectionReadinessResult:
            return self.request_or_read_by_effect_id(*args)

    workflow._projection = FailedProjection()
    result = workflow._project(data)
    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert result.canonical_commit_accepted is True

    data.projection = None
    result = workflow._freshness(data)
    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert result.canonical_commit_accepted is True


def test_complete_with_commit_but_without_freshness_is_post_commit_fatal() -> None:
    workflow, data = _workflow_and_data()
    data.commit_result = MemoryWriteCommitResult(
        request_id=data.request.request_id,
        status=MemoryWriteCommitStatus.ACCEPTED,
        commit_id=CommitId("sha256:" + "e" * 64),
    )

    result = workflow._complete(data)

    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert result.canonical_commit_accepted is True


def test_curator_repair_success_creates_a_child_and_checkpoint() -> None:
    workflow, data = _workflow_and_data()
    data.state = MemoryWriteState.CURATOR_REPAIR
    data.started_at = workflow._clock.now()
    data.basis = workflow._canonical_read.load_verified(
        data.request.project_id,
        data.request.base_commit,
    )
    data.directive = RepairDirective(
        directive_id=StableId("directive.curator.success"),
        action=RepairAction.CURATOR_REPAIR,
    )
    assert data.bundle is not None
    assert data.candidate is not None
    bundle = data.bundle
    candidate = data.candidate

    class Curator:
        def repair(self, request: object) -> CuratorRepairResult:
            return CuratorRepairResult(
                observed_changes=bundle.observed_changes,
                producer_receipt=candidate.producer_receipt,
            )

    workflow._curator = Curator()
    parent = data.candidate

    assert _step(data, workflow) is None
    assert data.candidate is not None
    assert data.candidate.parent_candidate_id == parent.candidate_id
    assert data.state is MemoryWriteState.NORMALIZE
    assert data.checkpoint_ref is not None


def test_curator_scope_rejection_returns_to_policy_with_checkpoint() -> None:
    workflow, data = _workflow_and_data()
    data.state = MemoryWriteState.CURATOR_REPAIR
    data.started_at = workflow._clock.now()
    data.basis = workflow._canonical_read.load_verified(
        data.request.project_id,
        data.request.base_commit,
    )
    data.directive = RepairDirective(
        directive_id=StableId("directive.curator.scope-retry"),
        action=RepairAction.CURATOR_REPAIR,
    )

    class Curator:
        def repair(self, request: object) -> CuratorRepairResult:
            del request
            raise CuratorRepairRejectedError("target changed")

    workflow._curator = Curator()

    assert _step(data, workflow) is None
    assert data.state is MemoryWriteState.REPAIR_POLICY
    assert data.directive is None
    assert data.checkpoint_ref is not None
    assert "CURATOR_REPAIR_SCOPE_REJECTED" in data.terminal_codes
    assert data.usage.curator_repairs == 1


@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    (
        (GuardianOutcome.APPROVE, MemoryWriteState.PRECOMMIT),
        (GuardianOutcome.REVISE, MemoryWriteState.REPAIR_POLICY),
        (GuardianOutcome.HUMAN_REVIEW, MemoryWriteState.HUMAN_SUSPEND),
        (GuardianOutcome.REJECT, MemoryWriteState.QUARANTINE),
    ),
)
def test_guardian_outcomes_route_to_all_registered_states(
    outcome: GuardianOutcome,
    expected_state: MemoryWriteState,
) -> None:
    workflow, data = _workflow_and_data()
    data.state = MemoryWriteState.GUARDIAN
    data.started_at = workflow._clock.now()
    data.basis = workflow._canonical_read.load_verified(
        data.request.project_id,
        data.request.base_commit,
    )
    assert data.candidate is not None
    receipt = agent_receipt().model_copy(
        update={
            "agent_type": AgentType.MEMORY_GUARDIAN,
            "agent_mode": AgentMode.RISK_REVIEW,
            "base_commit": BASE,
        }
    )
    decision = GuardianDecision(
        decision_id=StableId(f"guardian.{outcome.value}"),
        proposal_id=data.candidate.candidate_id,
        base_commit=BASE,
        outcome=outcome,
        risk_codes=(),
        reasons=("reviewed",),
        revised_candidate=_artifact("8") if outcome is GuardianOutcome.REVISE else None,
        receipt=receipt,
    )

    class Guardian:
        def review(self, request: object) -> GuardianReviewResult:
            return GuardianReviewResult(decision=decision)

    workflow._guardian = Guardian()

    assert _step(data, workflow) is None
    assert data.state is expected_state
    assert data.guardian == decision


def test_guardian_unavailable_routes_to_human_required() -> None:
    workflow, data = _workflow_and_data()
    data.state = MemoryWriteState.GUARDIAN
    workflow._guardian = None

    result = _step(data, workflow)

    assert result.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED
    assert "GUARDIAN_PORT_UNAVAILABLE" in result.terminal_codes


@pytest.mark.parametrize(
    ("kind", "expected_state"),
    (
        (HumanDecisionKind.APPROVE_EXACT_CANDIDATE, MemoryWriteState.PRECOMMIT),
        (HumanDecisionKind.REQUEST_REVISION, MemoryWriteState.CURATOR_REPAIR),
        (HumanDecisionKind.REJECT, MemoryWriteState.QUARANTINE),
    ),
)
def test_human_suspend_and_resume_routes_decisions(
    kind: HumanDecisionKind,
    expected_state: MemoryWriteState,
) -> None:
    workflow, data = _workflow_and_data()

    class Human:
        def __init__(self) -> None:
            self.requested: list[object] = []
            self.decision: HumanApprovalDecision | None = None

        def request(self, request: object) -> None:
            self.requested.append(request)

        def read_decision(self, request_id: object) -> HumanApprovalDecision | None:
            return self.decision

    human = Human()
    workflow._human = human
    first = workflow._suspend_for_human(data)
    assert first.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED
    assert data.approval_request is not None
    assert data.candidate is not None
    directive = (
        RepairDirective(
            directive_id=StableId("directive.human.revision"),
            action=RepairAction.CURATOR_REPAIR,
        )
        if kind is HumanDecisionKind.REQUEST_REVISION
        else None
    )
    human.decision = HumanApprovalDecision(
        decision_id=StableId(f"human.{kind.value}"),
        approval_request_id=data.approval_request.approval_request_id,
        request_id=data.request.request_id,
        candidate_id=data.candidate.candidate_id,
        candidate_content_hash=data.candidate.content_hash,
        base_commit=BASE,
        kind=kind,
        directive=directive,
        reason="rejected" if kind is HumanDecisionKind.REJECT else None,
        decided_at=datetime(2026, 7, 23, tzinfo=UTC),
    )

    assert workflow._resume_human(data) is None
    assert data.state is expected_state


def test_human_patch_is_loaded_as_a_child_candidate() -> None:
    workflow, data = _workflow_and_data()

    class Human:
        decision: HumanApprovalDecision | None = None

        def request(self, request: object) -> None:
            return None

        def read_decision(self, request_id: object) -> HumanApprovalDecision | None:
            return self.decision

    human = Human()
    workflow._human = human
    workflow._suspend_for_human(data)
    assert data.approval_request is not None
    assert data.bundle is not None
    assert data.candidate is not None
    payload = MemoryWriteCandidatePayload(
        observed_changes=data.bundle.observed_changes,
        root_update_intents=data.request.root_update_intents,
        commit_profile=data.request.commit_profile,
    )
    patch_ref = workflow._artifacts.put(
        canonical_json_bytes(payload.model_dump(mode="json")),
        "application/vnd.novel-agent.memory-write-candidate+json",
        data.request.canonical_root_refs.schema_version,
    )
    human.decision = HumanApprovalDecision(
        decision_id=StableId("human.patch"),
        approval_request_id=data.approval_request.approval_request_id,
        request_id=data.request.request_id,
        candidate_id=data.candidate.candidate_id,
        candidate_content_hash=data.candidate.content_hash,
        base_commit=BASE,
        kind=HumanDecisionKind.HUMAN_PATCH,
        patch_candidate_artifact=patch_ref,
        decided_at=datetime(2026, 7, 23, tzinfo=UTC),
    )

    assert workflow._resume_human(data) is None
    assert data.candidate is not None
    assert data.candidate.revision_no == 2
    assert data.state is MemoryWriteState.NORMALIZE


def test_human_suspend_missing_inputs_and_port_fail_closed() -> None:
    workflow, data = _workflow_and_data()
    data.candidate = None
    assert workflow._suspend_for_human(data).status is MemoryWriteWorkflowStatus.FATAL

    workflow, data = _workflow_and_data()
    workflow._human = None
    result = workflow._suspend_for_human(data)
    assert result.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED
    assert "HUMAN_APPROVAL_PORT_UNAVAILABLE" in result.terminal_codes


def test_partial_operation_quarantine_creates_a_degraded_child() -> None:
    workflow, data = _workflow_and_data()
    assert data.bundle is not None
    operations = (
        ChangeOperation(
            operation_id=StableId("operation.keep"),
            root_kind=RootKind.WORLD,
            operation=ChangeOperationType.REPLACE,
            target_id=StableId("target.keep"),
            payload={"value": "keep"},
        ),
        ChangeOperation(
            operation_id=StableId("operation.drop"),
            root_kind=RootKind.WORLD,
            operation=ChangeOperationType.REPLACE,
            target_id=StableId("target.drop"),
            payload={"value": "drop"},
        ),
    )
    changes = data.bundle.observed_changes.model_copy(update={"operations": operations})
    data.bundle = data.bundle.model_copy(update={"observed_changes": changes})
    data.directive = RepairDirective(
        directive_id=StableId("directive.quarantine.partial"),
        action=RepairAction.QUARANTINE_OPERATION,
        operation_ids=(operations[1].operation_id,),
        allowed_scope=RepairScope(operation_ids=(operations[1].operation_id,)),
    )
    original = data.candidate
    assert original is not None
    workflow_any: Any = workflow
    workflow_any._new_candidate = lambda *args, **kwargs: original.model_copy(
        update={
            "candidate_id": StableId("candidate.quarantine.child"),
            "parent_candidate_id": original.candidate_id,
            "revision_no": 2,
        }
    )

    assert workflow._quarantine_candidate(data) is None
    assert data.degraded is True
    assert data.state is MemoryWriteState.NORMALIZE
    assert data.quarantined_operation_ids == [operations[1].operation_id]


def test_human_decision_binding_validation_rejects_foreign_basis() -> None:
    workflow, data = _workflow_and_data()
    assert data.candidate is not None
    candidate = data.candidate

    class Human:
        def read_decision(self, request_id: object) -> HumanApprovalDecision:
            return HumanApprovalDecision(
                decision_id=StableId("human.foreign"),
                approval_request_id=StableId("approval.foreign"),
                request_id=StableId("request.foreign"),
                candidate_id=candidate.candidate_id,
                candidate_content_hash=candidate.content_hash,
                base_commit=BASE,
                kind=HumanDecisionKind.APPROVE_EXACT_CANDIDATE,
                decided_at=datetime(2026, 7, 23, tzinfo=UTC),
            )

    workflow._human = Human()
    with pytest.raises(Exception, match="another candidate basis"):
        workflow._resume_human(data)


@pytest.mark.parametrize(
    ("state", "status"),
    (
        (MemoryWriteState.HUMAN_SUSPEND, MemoryWriteWorkflowStatus.HUMAN_REQUIRED),
        (MemoryWriteState.HUMAN_RESUME, MemoryWriteWorkflowStatus.HUMAN_REQUIRED),
        (MemoryWriteState.BUDGET_STOP, MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED),
        (MemoryWriteState.STOP, MemoryWriteWorkflowStatus.FATAL),
        (MemoryWriteState.COMPLETE, MemoryWriteWorkflowStatus.NOOP),
    ),
)
def test_terminal_state_dispatch_is_total(
    state: MemoryWriteState,
    status: MemoryWriteWorkflowStatus,
) -> None:
    workflow, data = _workflow_and_data()
    data.state = state

    result = _step(data, workflow)

    assert result.status is status


def test_stop_replan_and_unknown_state_dispatch() -> None:
    workflow, data = _workflow_and_data()
    data.state = MemoryWriteState.STOP
    data.directive = RepairDirective(
        directive_id=StableId("directive.dispatch.replan"),
        action=RepairAction.REPLAN,
        reason_codes=("head-moved",),
    )
    assert _step(data, workflow).status is MemoryWriteWorkflowStatus.REPLAN_REQUIRED

    data.state = "not-a-workflow-state"  # type: ignore[assignment]
    result = _step(data, workflow)
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert "UNKNOWN_WORKFLOW_STATE" in result.terminal_codes


def test_normalization_transformed_ambiguous_and_budget_paths() -> None:
    workflow, data = _workflow_and_data()
    data.basis = workflow._canonical_read.load_verified(
        data.request.project_id, data.request.base_commit
    )
    assert data.candidate is not None
    parent = data.candidate
    child = parent.model_copy(
        update={
            "candidate_id": StableId("candidate.normalized.child"),
            "parent_candidate_id": parent.candidate_id,
            "revision_no": parent.revision_no + 1,
        }
    )

    class Normalizer:
        result: NormalizationResult

        def normalize(self, *args: object) -> NormalizationResult:
            return self.result

    normalizer = Normalizer()
    workflow._normalizer = normalizer
    data.state = MemoryWriteState.NORMALIZE
    normalizer.result = NormalizationResult(
        status=NormalizationStatus.TRANSFORMED,
        candidate=child,
    )
    assert _step(data, workflow) is None
    assert data.candidate == child

    workflow, data = _workflow_and_data()
    data.basis = workflow._canonical_read.load_verified(
        data.request.project_id, data.request.base_commit
    )
    assert data.candidate is not None
    normalizer = Normalizer()
    workflow._normalizer = normalizer
    data.state = MemoryWriteState.NORMALIZE
    normalizer.result = NormalizationResult(
        status=NormalizationStatus.AMBIGUOUS,
        candidate=data.candidate,
        reason_codes=("NORMALIZATION_AMBIGUOUS",),
    )
    assert _step(data, workflow) is None
    assert "NORMALIZATION_AMBIGUOUS" in data.terminal_codes

    workflow, data = _workflow_and_data()
    data.basis = workflow._canonical_read.load_verified(
        data.request.project_id, data.request.base_commit
    )
    assert data.candidate is not None
    child = data.candidate.model_copy(
        update={"candidate_id": StableId("candidate.normalized.over-budget")}
    )
    normalizer = Normalizer()
    workflow._normalizer = normalizer
    data.state = MemoryWriteState.NORMALIZE
    data.usage = data.usage.model_copy(
        update={"candidate_revisions": data.request.budget.max_candidate_revisions}
    )
    normalizer.result = NormalizationResult(
        status=NormalizationStatus.TRANSFORMED,
        candidate=child,
    )
    assert _step(data, workflow).status is MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED


@pytest.mark.parametrize(
    ("update", "code"),
    (
        (
            {"candidate_id": StableId("candidate.validation.foreign")},
            "VALIDATION_CANDIDATE_BINDING_MISMATCH",
        ),
        (
            {"materialization_receipt": _artifact("9")},
            "VALIDATION_BASIS_BINDING_MISMATCH",
        ),
        (
            {"proposed_roots_hash": ArtifactId("sha256:" + "9" * 64)},
            "VALIDATION_MATERIALIZATION_BINDING_MISMATCH",
        ),
    ),
)
def test_validation_binding_mismatches_fail_closed(
    update: dict[str, object],
    code: str,
) -> None:
    workflow, data = _workflow_and_data()
    data.basis = workflow._canonical_read.load_verified(
        data.request.project_id, data.request.base_commit
    )
    assert data.validation is not None
    invalid = data.validation.model_copy(update=update)
    workflow._validator = type(
        "Validator",
        (),
        {"validate": lambda self, *args: invalid},
    )()
    data.state = MemoryWriteState.VALIDATE

    result = _step(data, workflow)

    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert code in result.terminal_codes


def test_nonrepairable_validation_records_finding_codes() -> None:
    workflow, data = _workflow_and_data()
    data.basis = workflow._canonical_read.load_verified(
        data.request.project_id, data.request.base_commit
    )
    assert data.validation is not None
    finding = ValidationFindingV2(
        finding_id=StableId("finding.nonrepairable"),
        code="NON_REPAIRABLE_EDGE",
        category=ValidationFindingCategory.UNKNOWN,
        severity=ValidationSeverity.ERROR,
        message="cannot repair",
        retryability=FindingRetryability.NON_REPAIRABLE,
        blocking_scope=BlockingScope.CANDIDATE,
        allowed_repair_scope=RepairScope(),
    )
    decision = data.validation.model_copy(
        update={
            "disposition": ValidationDisposition.NON_REPAIRABLE,
            "findings": (finding,),
        }
    )
    workflow._validator = type(
        "Validator",
        (),
        {"validate": lambda self, *args: decision},
    )()
    data.state = MemoryWriteState.VALIDATE

    assert _step(data, workflow) is None
    assert data.state is MemoryWriteState.REPAIR_POLICY
    assert data.terminal_codes == ["NON_REPAIRABLE_EDGE"]


def test_new_candidate_rejects_foreign_base_and_exhausted_budget() -> None:
    workflow, data = _workflow_and_data()
    assert data.bundle is not None
    foreign = data.bundle.observed_changes.model_copy(
        update={"base_commit": CommitId("sha256:" + "9" * 64)}
    )
    with pytest.raises(InformationBoundaryViolation, match="base commit"):
        workflow._new_candidate(data, foreign, CandidateProducerKind.CURATOR_REPAIR)

    data.usage = data.usage.model_copy(
        update={"candidate_revisions": data.request.budget.max_candidate_revisions}
    )
    with pytest.raises(MemoryWriteWorkflowError, match="budget exhausted"):
        workflow._new_candidate(
            data,
            data.bundle.observed_changes,
            CandidateProducerKind.CURATOR_REPAIR,
        )


def test_curator_repair_secondary_budget_and_missing_directive_stop() -> None:
    workflow, data = _workflow_and_data()
    data.state = MemoryWriteState.CURATOR_REPAIR
    data.started_at = workflow._clock.now()
    workflow._curator = object()
    data.usage = data.usage.model_copy(
        update={"curator_repairs": data.request.budget.max_curator_repairs}
    )
    assert _step(data, workflow).status is MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED

    workflow, data = _workflow_and_data()
    data.state = MemoryWriteState.CURATOR_REPAIR
    data.started_at = workflow._clock.now()
    workflow._curator = object()
    result = _step(data, workflow)
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert "CURATOR_REPAIR_WITHOUT_DIRECTIVE" in result.terminal_codes


def test_risk_classifier_type_and_default_warning_routing() -> None:
    workflow, data = _workflow_and_data()
    workflow._risk_classifier = type(
        "Classifier",
        (),
        {"assess": lambda self, candidate, validation: object()},
    )()
    with pytest.raises(MemoryWriteWorkflowError, match="invalid assessment"):
        workflow._assess_risk(data)

    workflow._risk_classifier = None
    assert data.validation is not None
    finding = ValidationFindingV2(
        finding_id=StableId("finding.warning"),
        code="WARNING_EDGE",
        category=ValidationFindingCategory.UNKNOWN,
        severity=ValidationSeverity.WARNING,
        message="review",
        retryability=FindingRetryability.REVIEW,
        blocking_scope=BlockingScope.CANDIDATE,
        allowed_repair_scope=RepairScope(),
    )
    data.validation = data.validation.model_copy(update={"findings": (finding,)})
    risk = workflow._assess_risk(data)
    assert risk.requires_guardian is True
    assert risk.risk_codes == ("VALIDATION_REVIEW",)


def test_precommit_tolerates_current_head_read_failure_and_rejects_bad_gate() -> None:
    workflow, data = _workflow_and_data()
    canonical: Any = workflow._canonical_read
    canonical.current_commit = lambda project_id: (_ for _ in ()).throw(OSError("unavailable"))

    assert workflow._prepare_commit(data) is None
    assert data.state is MemoryWriteState.COMMIT

    workflow, data = _workflow_and_data()
    workflow._write_gate = type(
        "Gate",
        (),
        {"decide": lambda self, *args, **kwargs: object()},
    )()
    with pytest.raises(MemoryWriteWorkflowError, match="invalid decision"):
        workflow._prepare_commit(data)


def test_commit_operation_fallback_and_existing_projection_effect() -> None:
    workflow, data = _workflow_and_data()
    data.commit_request = _commit_request()
    data.commit_effect_id = data.commit_request.commit_effect_id
    accepted = MemoryWriteCommitResult(
        request_id=data.request.request_id,
        status=MemoryWriteCommitStatus.ACCEPTED,
        commit_id=CommitId("sha256:" + "e" * 64),
    )
    workflow._commit_port = type(
        "Commit",
        (),
        {"resolve_or_replay_exact": lambda self, request: accepted},
    )()
    assert workflow._commit(data) is None
    assert data.state is MemoryWriteState.PROJECT

    data.projection_effect_id = StableId("effect.projection.existing")
    assert workflow._project(data) is None
    assert data.projection_effect_id == StableId("effect.projection.existing")


def test_freshness_pending_and_missing_freshness_preserve_commit() -> None:
    workflow, data = _workflow_and_data()
    data.commit_result = MemoryWriteCommitResult(
        request_id=data.request.request_id,
        status=MemoryWriteCommitStatus.ACCEPTED,
        commit_id=CommitId("sha256:" + "e" * 64),
    )
    data.projection_effect_id = StableId("effect.projection.pending")
    commit_id = data.commit_result.commit_id
    assert commit_id is not None
    ready = workflow._projection.request_or_read_by_effect_id(
        data.request.project_id,
        commit_id,
        StableId("effect.projection.no-freshness"),
    )
    pending = ProjectionReadinessResult(
        effect_id=StableId("effect.projection.pending"),
        status=ProjectionReadinessStatus.PENDING,
        resumable=True,
        reason="pending",
    )
    workflow._projection = type(
        "Projection",
        (),
        {"await_or_check": lambda self, *args: pending},
    )()
    result = workflow._freshness(data)
    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.SUSPENDED

    data.projection = ready.model_copy(update={"freshness": None})
    result = workflow._freshness(data)
    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert result.canonical_commit_accepted is True


def test_human_resume_pending_async_and_budget_edges() -> None:
    workflow, data = _workflow_and_data()

    class Human:
        def request(self, request: object) -> None:
            return None

        def read_decision(self, request_id: object) -> None:
            return None

    workflow._human = Human()
    workflow._suspend_for_human(data)
    approval = data.approval_request
    assert approval is not None
    assert workflow._suspend_for_human(data).status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED
    result = workflow._resume_human(data)
    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.HUMAN_REQUIRED

    async def async_decision() -> None:
        return None

    pending = async_decision()
    workflow._human = type(
        "AsyncHuman",
        (),
        {
            "read_decision": lambda self, request_id: pending,
            "request": lambda self, request: None,
        },
    )()
    with pytest.raises(MemoryWriteWorkflowError, match="must be synchronous"):
        workflow._resume_human(data)
    pending.close()

    assert data.candidate is not None
    patch_decision = HumanApprovalDecision(
        decision_id=StableId("human.patch.over-budget"),
        approval_request_id=approval.approval_request_id,
        request_id=data.request.request_id,
        candidate_id=data.candidate.candidate_id,
        candidate_content_hash=data.candidate.content_hash,
        base_commit=BASE,
        kind=HumanDecisionKind.HUMAN_PATCH,
        patch_candidate_artifact=_artifact("9"),
        decided_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    workflow._human = type(
        "PatchHuman",
        (),
        {
            "read_decision": lambda self, request_id: patch_decision,
            "request": lambda self, request: None,
        },
    )()
    data.usage = data.usage.model_copy(
        update={"candidate_revisions": data.request.budget.max_candidate_revisions}
    )
    result = workflow._resume_human(data)
    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED


def test_quarantine_budget_and_budget_stop_quarantine_paths() -> None:
    workflow, data = _workflow_and_data()
    assert data.bundle is not None
    operation = ChangeOperation(
        operation_id=StableId("operation.quarantine.budget"),
        root_kind=RootKind.WORLD,
        operation=ChangeOperationType.REPLACE,
        target_id=StableId("target.quarantine.budget"),
        payload={"value": "drop"},
    )
    kept = operation.model_copy(update={"operation_id": StableId("operation.keep.budget")})
    changes = data.bundle.observed_changes.model_copy(update={"operations": (operation, kept)})
    data.bundle = data.bundle.model_copy(update={"observed_changes": changes})
    data.directive = RepairDirective(
        directive_id=StableId("directive.quarantine.budget"),
        action=RepairAction.QUARANTINE_OPERATION,
        operation_ids=(operation.operation_id,),
        allowed_scope=RepairScope(operation_ids=(operation.operation_id,)),
    )
    data.usage = data.usage.model_copy(
        update={"candidate_revisions": data.request.budget.max_candidate_revisions}
    )
    result = workflow._quarantine_candidate(data)
    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.QUARANTINED
    assert "CANDIDATE_REVISION_BUDGET_EXHAUSTED" in result.terminal_codes

    workflow, data = _workflow_and_data()
    result = workflow._budget_stop(data)
    assert result.status is MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED
    assert data.quarantine_refs


def test_quarantine_package_id_is_bounded_for_max_length_candidate_id() -> None:
    workflow, data = _workflow_and_data()
    assert data.candidate is not None
    data.candidate = data.candidate.model_copy(
        update={"candidate_id": StableId("candidate." + "x" * 118)}
    )

    result = workflow._quarantine_candidate(data)

    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.QUARANTINED
    package = workflow._quarantine.packages[-1]
    assert package.package_id.root.startswith("quarantine.candidate.")
    assert len(package.package_id.root) <= 128
    assert data.candidate.candidate_id in package.candidate_ids


def test_guardian_conversion_and_human_decision_specific_binding_errors() -> None:
    workflow, data = _workflow_and_data()
    assert data.guardian is None
    assert data.candidate is not None
    guardian = GuardianDecision(
        decision_id=StableId("guardian.direct"),
        proposal_id=data.candidate.candidate_id,
        base_commit=BASE,
        outcome=GuardianOutcome.APPROVE,
        risk_codes=(),
        reasons=("approved",),
        receipt=agent_receipt().model_copy(
            update={
                "agent_type": AgentType.MEMORY_GUARDIAN,
                "agent_mode": AgentMode.RISK_REVIEW,
                "base_commit": BASE,
            }
        ),
    )
    assert _as_guardian_result(guardian).decision == guardian

    workflow._human = type(
        "Human",
        (),
        {
            "request": lambda self, request: None,
            "read_decision": lambda self, request_id: None,
        },
    )()
    workflow._suspend_for_human(data)
    assert data.approval_request is not None
    foreign_approval = HumanApprovalDecision(
        decision_id=StableId("human.foreign.approval"),
        approval_request_id=StableId("approval.other"),
        request_id=data.request.request_id,
        candidate_id=data.candidate.candidate_id,
        candidate_content_hash=data.candidate.content_hash,
        base_commit=BASE,
        kind=HumanDecisionKind.APPROVE_EXACT_CANDIDATE,
        decided_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    with pytest.raises(MemoryWriteWorkflowError, match="another approval"):
        _validate_human_decision(data, foreign_approval)

    wrong_revision = foreign_approval.model_copy(
        update={
            "approval_request_id": data.approval_request.approval_request_id,
            "kind": HumanDecisionKind.REQUEST_REVISION,
            "directive": RepairDirective(
                directive_id=StableId("directive.wrong.human"),
                action=RepairAction.HUMAN,
            ),
        }
    )
    with pytest.raises(MemoryWriteWorkflowError, match="curator-repair"):
        _validate_human_decision(data, wrong_revision)


def test_human_patch_payload_binding_mismatch_is_fatal() -> None:
    workflow, data = _workflow_and_data()

    class Human:
        decision: HumanApprovalDecision | None = None

        def request(self, request: object) -> None:
            return None

        def read_decision(self, request_id: object) -> HumanApprovalDecision | None:
            return self.decision

    human = Human()
    workflow._human = human
    workflow._suspend_for_human(data)
    assert data.approval_request is not None
    assert data.bundle is not None
    assert data.candidate is not None
    payload = MemoryWriteCandidatePayload(
        observed_changes=data.bundle.observed_changes,
        root_update_intents=data.request.root_update_intents,
        commit_profile=type(data.request.commit_profile).CHAPTER_REVEAL_ATOMIC,
    )
    patch_ref = workflow._artifacts.put(
        canonical_json_bytes(payload.model_dump(mode="json")),
        "application/vnd.novel-agent.memory-write-candidate+json",
        data.request.canonical_root_refs.schema_version,
    )
    human.decision = HumanApprovalDecision(
        decision_id=StableId("human.patch.binding-mismatch"),
        approval_request_id=data.approval_request.approval_request_id,
        request_id=data.request.request_id,
        candidate_id=data.candidate.candidate_id,
        candidate_content_hash=data.candidate.content_hash,
        base_commit=BASE,
        kind=HumanDecisionKind.HUMAN_PATCH,
        patch_candidate_artifact=patch_ref,
        decided_at=datetime(2026, 7, 23, tzinfo=UTC),
    )

    result = workflow._resume_human(data)

    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert "HUMAN_PATCH_PAYLOAD_BINDING_MISMATCH" in result.terminal_codes


def test_commit_explicit_operation_ids_skip_fallback() -> None:
    workflow, data = _workflow_and_data()
    data.commit_request = _commit_request()
    data.commit_effect_id = data.commit_request.commit_effect_id
    operation_id = StableId("operation.explicit.commit")
    accepted = MemoryWriteCommitResult(
        request_id=data.request.request_id,
        status=MemoryWriteCommitStatus.ACCEPTED,
        commit_id=CommitId("sha256:" + "e" * 64),
        committed_operation_ids=(operation_id,),
    )
    workflow._commit_port = type(
        "Commit",
        (),
        {"resolve_or_replay_exact": lambda self, request: accepted},
    )()

    assert workflow._commit(data) is None
    assert data.committed_operation_ids == [operation_id]


def test_quarantine_all_operations_and_nonquarantine_budget_stop() -> None:
    workflow, data = _workflow_and_data()
    assert data.bundle is not None
    operation = ChangeOperation(
        operation_id=StableId("operation.quarantine.all"),
        root_kind=RootKind.WORLD,
        operation=ChangeOperationType.REPLACE,
        target_id=StableId("target.quarantine.all"),
        payload={"value": "drop"},
    )
    changes = data.bundle.observed_changes.model_copy(update={"operations": (operation,)})
    data.bundle = data.bundle.model_copy(update={"observed_changes": changes})
    data.directive = RepairDirective(
        directive_id=StableId("directive.quarantine.all"),
        action=RepairAction.QUARANTINE_OPERATION,
        operation_ids=(operation.operation_id,),
        allowed_scope=RepairScope(operation_ids=(operation.operation_id,)),
    )
    result = workflow._quarantine_candidate(data)
    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.QUARANTINED

    workflow, data = _workflow_and_data()
    data.request = data.request.model_copy(
        update={
            "budget": data.request.budget.model_copy(update={"on_budget_exhausted": "stop"}),
        }
    )
    result = workflow._budget_stop(data)
    assert result.status is MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED
    assert not data.quarantine_refs
