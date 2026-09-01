"""State-machine terminals and fail-closed Stage 2W workflow branches."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from novel_agent.domain.ids import CommitId, RunId, StableId, TaskId
from novel_agent.domain.memory_write import (
    CandidateProducerKind,
    CanonicalWriteBasis,
    MemoryWriteState,
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
    ProjectionReadinessStatus,
)
from novel_agent.domain.runtime import RunEventType
from novel_agent.ports.memory_write import (
    MemoryWriteCommitResult,
    MemoryWriteCommitStatus,
)
from novel_agent.services.memory_write_workflow import (
    ImmediateProjectionReadinessPort,
    InMemoryArtifactRepository,
    InMemoryCandidateLineageRepository,
    InMemoryCheckpointRepository,
    InMemoryCommitPort,
    LocalMemoryWriteWorkflow,
    MemoryWriteWorkflowError,
    _WorkflowData,
)
from tests.contract.test_memory_write_workflow_contract import (
    BASE,
    PROJECT,
    _artifact,
    _BoundarySpy,
    _manifest,
    _request,
)
from tests.unit.test_memory_write_resume import _Canonical, _ready_data


def _workflow_and_data() -> tuple[LocalMemoryWriteWorkflow, _WorkflowData]:
    artifacts = InMemoryArtifactRepository()
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    commit = InMemoryCommitPort(current_commit=BASE)
    workflow = LocalMemoryWriteWorkflow(
        canonical_read=_Canonical(commit),  # type: ignore[arg-type]
        commit=commit,
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        projection=ImmediateProjectionReadinessPort(artifacts=artifacts),
        information_boundary=_BoundarySpy(),
    )
    return workflow, _ready_data(artifacts=artifacts, lineage=lineage)


def test_long_request_event_identity_retains_run_scope() -> None:
    request_id = "r" * 117
    first = LocalMemoryWriteWorkflow._event_identity(
        "event",
        request_id,
        "task_started",
        1,
        scope_ids=("run.u8b.event-one", "task.u8b.event-one"),
    )
    second = LocalMemoryWriteWorkflow._event_identity(
        "event",
        request_id,
        "task_started",
        1,
        scope_ids=("run.u8b.event-two", "task.u8b.event-two"),
    )

    assert first.root == "event.run.u8b.event-one.task_started.1"
    assert second.root == "event.run.u8b.event-two.task_started.1"
    assert first != second


def test_event_identity_fails_closed_when_all_scopes_are_max_length() -> None:
    maximum = "i" * 128

    with pytest.raises(MemoryWriteWorkflowError, match="no bounded"):
        LocalMemoryWriteWorkflow._event_identity(
            "event",
            maximum,
            "task_started",
            1,
            scope_ids=(maximum, maximum),
        )


def test_workflow_exposes_terminal_ref_when_event_identity_is_unadmissible() -> None:
    workflow, data = _workflow_and_data()
    maximum = "i" * 128
    request = data.request.model_copy(
        update={
            "request_id": StableId(maximum),
            "task_id": TaskId(maximum),
            "run_id": RunId(maximum),
        }
    )

    result = asyncio.run(workflow.execute(request))

    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert result.terminal_result_ref is not None
    assert result.checkpoint_ref is None
    assert any("no bounded" in code for code in result.terminal_codes)


def test_long_run_event_trace_reuses_existing_run_identity() -> None:
    workflow, data = _workflow_and_data()
    data.request = data.request.model_copy(update={"run_id": RunId("r" * 128)})

    workflow._event(data, RunEventType.CHECKPOINT_CREATED)

    event = workflow._events.events[0]
    assert event.trace_id == data.request.run_id.root
    assert len(event.trace_id) <= 128


def test_all_workflow_terminal_statuses_have_a_real_constructor_path() -> None:
    workflow, ready = _workflow_and_data()
    request = ready.request
    observed: set[MemoryWriteWorkflowStatus] = set()

    observed.add(workflow._fatal(_WorkflowData(request=request), "fatal").status)
    observed.add(workflow._budget_result(_WorkflowData(request=request)).status)
    observed.add(
        workflow._replan(
            _WorkflowData(request=request, artifacts=workflow._artifacts),
            ("replan",),
        ).status
    )
    observed.add(
        workflow._human_required(
            _WorkflowData(request=request, artifacts=workflow._artifacts),
            "human",
        ).status
    )
    suspended_data = _WorkflowData(
        request=request,
        checkpoint_ref=_artifact("8"),
    )
    observed.add(workflow._suspended(suspended_data, "pending", "waiting").status)
    quarantine_data = _WorkflowData(
        request=request,
        quarantine_refs=[_artifact("9")],
    )
    observed.add(workflow._quarantined(quarantine_data).status)
    observed.add(
        workflow._complete(_WorkflowData(request=request, artifacts=workflow._artifacts)).status
    )

    accepted_commit = CommitId("sha256:" + "e" * 64)
    ready.commit_result = MemoryWriteCommitResult(
        request_id=request.request_id,
        status=MemoryWriteCommitStatus.ACCEPTED,
        commit_id=accepted_commit,
        commit_receipt_ref=_artifact("f"),
    )
    projection = workflow._projection.request_or_read_by_effect_id(
        request.project_id,
        accepted_commit,
        StableId("effect.projection.terminal"),
    )
    assert projection.status is ProjectionReadinessStatus.READY
    ready.projection = projection
    ready.projection_effect_id = projection.effect_id
    observed.add(workflow._complete(ready).status)

    assert observed == set(MemoryWriteWorkflowStatus)


@pytest.mark.parametrize(
    ("state", "code"),
    (
        (MemoryWriteState.NORMALIZE, "NORMALIZE_WITHOUT_CANDIDATE"),
        (MemoryWriteState.MATERIALIZE, "MATERIALIZE_WITHOUT_CANDIDATE"),
        (MemoryWriteState.VALIDATE, "VALIDATE_WITHOUT_MATERIALIZATION"),
        (MemoryWriteState.CURATOR_REPAIR, "CURATOR_REPAIR_PORT_UNAVAILABLE"),
        (MemoryWriteState.GUARDIAN, "GUARDIAN_WITHOUT_VALIDATED_CANDIDATE"),
        (MemoryWriteState.PROJECT, "PROJECT_WITHOUT_COMMIT"),
        (MemoryWriteState.FRESHNESS_GATE, "FRESHNESS_WITHOUT_COMMIT"),
        (MemoryWriteState.QUARANTINE, "QUARANTINE_WITHOUT_CANDIDATE"),
    ),
)
def test_missing_state_preconditions_return_typed_fatal(state: MemoryWriteState, code: str) -> None:
    workflow, _ = _workflow_and_data()
    data = _WorkflowData(request=_request(), state=state)
    result = asyncio.run(workflow._step(data))
    assert result is not None
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert code in result.terminal_codes


def test_step_limit_returns_typed_fatal_instead_of_escaping() -> None:
    workflow, _ = _workflow_and_data()
    workflow._request_step_limit = 0
    result = asyncio.run(workflow.execute(_request()))
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert "WORKFLOW_STEP_LIMIT_EXCEEDED" in result.terminal_codes


def test_unexpected_step_exception_is_captured_precommit() -> None:
    workflow, _ = _workflow_and_data()

    async def explode(_: _WorkflowData) -> None:
        raise LookupError("unexpected")

    workflow_with_fault: Any = workflow
    workflow_with_fault._step = explode
    result = asyncio.run(workflow.execute(_request()))
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert result.canonical_commit_accepted is False
    assert "UNEXPECTED_WORKFLOW_FAILURE" in result.terminal_codes
    assert result.checkpoint_ref is not None
    checkpoint = workflow._checkpoint.load(result.checkpoint_ref)
    assert checkpoint.resume_state is MemoryWriteState.STOP
    assert checkpoint.terminal_result_ref is not None
    terminal = MemoryWriteWorkflowResult.model_validate_json(
        workflow._artifacts.read_verified(checkpoint.terminal_result_ref),
        strict=True,
    )
    assert terminal.request_id == result.request_id
    assert terminal.terminal_codes == result.terminal_codes
    assert result.terminal_result_ref == checkpoint.terminal_result_ref


def test_terminal_ref_survives_checkpoint_persistence_failure() -> None:
    workflow, _ = _workflow_and_data()

    class FailingCheckpointRepository:
        def save(self, _checkpoint: object) -> Any:
            raise RuntimeError("checkpoint store unavailable")

        def load(self, _checkpoint_ref: object) -> Any:  # pragma: no cover - not reached
            raise AssertionError("load must not be called")

    workflow._checkpoint = FailingCheckpointRepository()

    async def explode(_: _WorkflowData) -> None:
        raise LookupError("unexpected")

    workflow_with_fault: Any = workflow
    workflow_with_fault._step = explode
    result = asyncio.run(workflow.execute(_request()))

    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert result.checkpoint_ref is None
    assert result.terminal_result_ref is not None
    assert "CHECKPOINT_PERSIST_FAILED" in result.terminal_codes
    terminal = MemoryWriteWorkflowResult.model_validate_json(
        workflow._artifacts.read_verified(result.terminal_result_ref),
        strict=True,
    )
    assert terminal.terminal_codes == result.terminal_codes


def test_checkpoint_ref_is_cleared_when_checkpoint_event_persistence_fails() -> None:
    workflow, _ = _workflow_and_data()

    class FailingEventSink:
        def __init__(self) -> None:
            self.events: list[object] = []

        def append(self, _event: object) -> object:
            raise RuntimeError("event sink unavailable")

    workflow._events = FailingEventSink()

    result = asyncio.run(workflow.execute(_request()))

    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert result.checkpoint_ref is None
    assert result.terminal_result_ref is not None
    assert "CHECKPOINT_PERSIST_FAILED" in result.terminal_codes
    terminal = MemoryWriteWorkflowResult.model_validate_json(
        workflow._artifacts.read_verified(result.terminal_result_ref),
        strict=True,
    )
    assert terminal.checkpoint_ref is None
    assert terminal.terminal_codes == result.terminal_codes


def test_checkpoint_identity_reuses_short_semantic_maintenance_request_id() -> None:
    workflow, data = _workflow_and_data()
    data.request = data.request.model_copy(
        update={"request_id": StableId("memory-write.memory-gap." + "a" * 32)}
    )

    ref = workflow._save_checkpoint(
        data,
        phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        resume_state=MemoryWriteState.MATERIALIZE,
    )

    checkpoint = workflow._checkpoint.load(ref)
    assert len(checkpoint.checkpoint_id.root) <= 128
    assert checkpoint.checkpoint_id.root.startswith("checkpoint.memory-write.memory-gap.")
    assert checkpoint.run_id == data.request.run_id


def test_long_request_id_uses_existing_task_scope_for_checkpoint_and_candidate() -> None:
    workflow, data = _workflow_and_data()
    data.request = data.request.model_copy(update={"request_id": StableId("r" * 128)})

    checkpoint_ref = workflow._save_checkpoint(
        data,
        phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        resume_state=MemoryWriteState.MATERIALIZE,
    )
    checkpoint = workflow._checkpoint.load(checkpoint_ref)
    assert checkpoint.checkpoint_id.root == "checkpoint.task.stage2w.contract.1"

    workflow_with_any: Any = workflow
    workflow_with_any._persist_candidate = lambda _data, candidate: candidate
    candidate = workflow._new_candidate(
        data,
        data.bundle.observed_changes,  # type: ignore[union-attr]
        CandidateProducerKind.EMPTY_DELTA,
    )
    assert candidate.candidate_id.root.startswith("candidate.task.stage2w.contract.1.")
    assert len(candidate.candidate_id.root) <= 128


def test_proposal_and_event_identities_are_readable_and_replay_stable() -> None:
    request_id = StableId("memory-write.memory-gap." + "a" * 32)

    first_attempt = LocalMemoryWriteWorkflow._proposal_attempt_id(request_id, 1)
    second_attempt = LocalMemoryWriteWorkflow._proposal_attempt_id(request_id, 2)
    first_model_request = LocalMemoryWriteWorkflow._proposal_model_request_id(request_id, 1)

    assert first_attempt.root == "proposal-attempt.memory-write.memory-gap." + "a" * 32 + ".1"
    assert second_attempt != first_attempt
    assert first_model_request.root.startswith("proposal-model-request.memory-write.memory-gap.")
    assert all(
        len(identity.root) <= 128
        for identity in (first_attempt, second_attempt, first_model_request)
    )
    assert "sha256" not in first_attempt.root


def test_long_request_id_uses_existing_task_scope_for_proposal_identities() -> None:
    request_id = StableId("r" * 128)
    task_id = StableId("task.stage2w.proposal")
    run_id = StableId("run.stage2w.proposal")

    attempt = LocalMemoryWriteWorkflow._proposal_attempt_id(
        request_id,
        1,
        fallback_ids=(task_id, run_id),
    )
    model_request = LocalMemoryWriteWorkflow._proposal_model_request_id(
        request_id,
        1,
        fallback_ids=(task_id, run_id),
    )

    assert attempt.root == "proposal-attempt.task.stage2w.proposal.1"
    assert model_request.root == "proposal-model-request.task.stage2w.proposal.1.schema-1"


def test_invalid_validation_adapter_result_is_a_programming_error() -> None:
    workflow, data = _workflow_and_data()
    data.state = MemoryWriteState.VALIDATE
    data.basis = CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=BASE,
        root_manifest=_manifest(),
    )

    class InvalidValidator:
        async def validate(self, *_: object) -> str:
            return "invalid"

    workflow._validator = InvalidValidator()
    with pytest.raises(MemoryWriteWorkflowError, match="non-v2"):
        asyncio.run(workflow._step(data))


def test_projection_failure_after_commit_never_claims_rollback() -> None:
    workflow, data = _workflow_and_data()
    data.commit_result = MemoryWriteCommitResult(
        request_id=data.request.request_id,
        status=MemoryWriteCommitStatus.ACCEPTED,
        commit_id=CommitId("sha256:" + "e" * 64),
        commit_receipt_ref=_artifact("f"),
    )
    result = workflow._post_commit_fatal(data, "projection", "failed")
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert result.workflow_phase is MemoryWriteWorkflowPhase.CANON_COMMITTED
    assert result.canonical_commit_accepted is True


def test_proposal_retry_feedback_carries_precise_validation_paths() -> None:
    """2026-08-14 corridor repair: FEEDBACK must include json_pointers etc.

    ch2 receipts (§28.14) showed the model repeated the same schema defect
    because the retry feedback dropped validation_error_paths, json_pointers
    and violation_rule even though the repair contract promises to treat them
    as mandatory corrections.
    """
    import json as _json
    from datetime import UTC, datetime

    from novel_agent.domain.memory_write import (
        CuratorProposalRejection,
        MemoryWriteBudget,
        MemoryWriteBudgetRemaining,
        ProposalRejectionKind,
        ProposalRejectionStage,
    )
    from novel_agent.services.pre_candidate_repair import (
        BoundedPreCandidateRepairPolicy,
        proposal_rejection_signature,
    )

    workflow, data = _workflow_and_data()
    rejection = CuratorProposalRejection(
        rejection_id=StableId("rejection.retry-feedback"),
        attempt_id=StableId("attempt.retry-feedback"),
        workflow_request_id=data.request.request_id,
        base_commit=BASE,
        stage=ProposalRejectionStage.STRUCTURED_SCHEMA,
        kind=ProposalRejectionKind.SCHEMA_REJECTED,
        reason_code="CURATOR_PROPOSAL_SCHEMA_REJECTED",
        retryable=True,
        rejection_signature=proposal_rejection_signature(
            {"reason": "schema", "paths": ("candidates.0",)}
        ),
        validation_error_paths=("candidates.0", "candidates.1"),
        json_pointers=("/candidates/0", "/candidates/1"),
        violation_rule="kind_discriminator_required",
        safe_feedback=("Curator Draft failed the structured domain contract",),
        created_at=datetime.now(UTC),
    )
    policy = BoundedPreCandidateRepairPolicy()
    directive = policy.decide(
        rejection=rejection,
        attempt_count=2,
        rejection_count=2,
        same_output_count=0,
        same_rejection_count=1,
        budget=MemoryWriteBudget(),
        remaining=MemoryWriteBudgetRemaining(
            candidate_revisions=1,
            curator_repairs=1,
            normalization_passes=1,
            guardian_reviews=1,
            context_refreshes=1,
            total_model_calls=2,
            token_budget=12_000,
            wall_clock_budget_ms=90_000,
        ),
    )
    data.state = MemoryWriteState.PROPOSAL_RETRY
    data.proposal_rejections = [rejection]
    data.proposal_directive = directive
    data.candidate = None
    assert asyncio.run(workflow._step(data)) is None
    assert data.state is MemoryWriteState.CURATE_ATTEMPT_PREPARE
    assert data.proposal_feedback_ref is not None
    feedback = _json.loads(
        workflow._artifacts.read_verified(data.proposal_feedback_ref).decode("utf-8")
    )
    assert feedback["reason_code"] == "CURATOR_PROPOSAL_SCHEMA_REJECTED"
    assert feedback["validation_error_paths"] == ["candidates.0", "candidates.1"]
    assert feedback["json_pointers"] == ["/candidates/0", "/candidates/1"]
    assert feedback["violation_rule"] == "kind_discriminator_required"
    assert feedback["mutable_operation_indexes"] == list(directive.scope.mutable_operation_indexes)
