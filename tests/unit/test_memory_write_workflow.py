"""State-machine terminals and fail-closed Stage 2W workflow branches."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory_write import (
    CanonicalWriteBasis,
    MemoryWriteState,
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowStatus,
    ProjectionReadinessStatus,
)
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
