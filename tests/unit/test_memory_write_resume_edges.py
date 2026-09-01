"""Corrupt and reconstruction checkpoint matrix for Stage 2W resume."""

from __future__ import annotations

import asyncio
from typing import Any

from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory_write import (
    MemoryWriteState,
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
)
from novel_agent.domain.runtime import EffectStatus
from novel_agent.services.memory_write_workflow import (
    LocalMemoryWriteWorkflow,
    _model_ref,
    _WorkflowData,
)
from tests.contract.test_memory_write_workflow_contract import _artifact
from tests.unit.test_memory_write_workflow import _workflow_and_data
from tests.unit.test_teacher_forced_memory_write_adapters import _commit_request


def _resume_request(data: Any, ref: Any) -> Any:
    return data.request.model_copy(update={"resume_checkpoint": ref})


def test_resume_corrupt_checkpoint_and_identity_mismatch_fail_closed() -> None:
    workflow, data = _workflow_and_data()
    ref = _artifact("8")

    class Corrupt:
        def load(self, checkpoint_ref: object) -> object:
            raise OSError("corrupt")

    workflow._checkpoint = Corrupt()
    result = asyncio.run(workflow._initialize(_resume_request(data, ref)))
    assert isinstance(result, MemoryWriteWorkflowResult)
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert "CHECKPOINT_CORRUPT" in result.terminal_codes

    workflow, data = _workflow_and_data()
    saved_ref = workflow._save_checkpoint(
        data,
        phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        resume_state=MemoryWriteState.LOAD_BASIS,
    )
    checkpoint = workflow._checkpoint.load(saved_ref).model_copy(
        update={"configuration_fingerprint": ArtifactId("sha256:" + "9" * 64)}
    )
    workflow._checkpoint = type("Mismatch", (), {"load": lambda self, ref: checkpoint})()
    result = asyncio.run(workflow._initialize(_resume_request(data, saved_ref)))
    assert isinstance(result, MemoryWriteWorkflowResult)
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert "RESUME_BASIS_MISMATCH" in result.terminal_codes


def test_complete_checkpoint_corrupt_terminal_and_foreign_identity_fail_closed() -> None:
    workflow, data = _workflow_and_data()
    saved_ref = workflow._save_checkpoint(
        data,
        phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        resume_state=MemoryWriteState.NORMALIZE,
    )
    base = workflow._checkpoint.load(saved_ref)

    corrupt = base.model_copy(
        update={
            "workflow_phase": MemoryWriteWorkflowPhase.COMPLETE,
            "terminal_result_ref": _artifact("9"),
        }
    )
    workflow._checkpoint = type("CorruptTerminal", (), {"load": lambda self, ref: corrupt})()
    result = asyncio.run(workflow._initialize(_resume_request(data, saved_ref)))
    assert isinstance(result, MemoryWriteWorkflowResult)
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert "TERMINAL_RESULT_CORRUPT" in result.terminal_codes

    foreign = MemoryWriteWorkflowResult(
        request_id=StableId("request.foreign"),
        status=MemoryWriteWorkflowStatus.NOOP,
        workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
        canonical_commit_accepted=False,
        base_commit=data.request.base_commit,
    )
    foreign_ref = _model_ref(foreign, data, "terminal-result")
    mismatch = base.model_copy(
        update={
            "workflow_phase": MemoryWriteWorkflowPhase.COMPLETE,
            "terminal_result_ref": foreign_ref,
        }
    )
    workflow._checkpoint = type("ForeignTerminal", (), {"load": lambda self, ref: mismatch})()
    result = asyncio.run(workflow._initialize(_resume_request(data, saved_ref)))
    assert isinstance(result, MemoryWriteWorkflowResult)
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert "TERMINAL_RESULT_IDENTITY_MISMATCH" in result.terminal_codes


def test_same_process_checkpoint_reuses_state_and_restores_start_time() -> None:
    workflow, data = _workflow_and_data()
    data.started_at = None
    ref = workflow._save_checkpoint(
        data,
        phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        resume_state=MemoryWriteState.MATERIALIZE,
    )

    restored = asyncio.run(workflow._initialize(_resume_request(data, ref)))

    assert isinstance(restored, _WorkflowData)
    assert restored is data
    assert restored.state is MemoryWriteState.MATERIALIZE
    assert restored.started_at is not None

    restored.started_at = workflow._clock.now()
    again = asyncio.run(workflow._initialize(_resume_request(data, ref)))
    assert isinstance(again, _WorkflowData)
    assert again.started_at == restored.started_at


def test_complete_checkpoint_replays_valid_terminal_result() -> None:
    workflow, data = _workflow_and_data()
    terminal = MemoryWriteWorkflowResult(
        request_id=data.request.request_id,
        status=MemoryWriteWorkflowStatus.NOOP,
        workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
        canonical_commit_accepted=False,
        base_commit=data.request.base_commit,
    )
    terminal_ref = _model_ref(terminal, data, "terminal-result")
    saved_ref = workflow._save_checkpoint(
        data,
        phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        resume_state=MemoryWriteState.NORMALIZE,
    )
    complete = workflow._checkpoint.load(saved_ref).model_copy(
        update={
            "workflow_phase": MemoryWriteWorkflowPhase.COMPLETE,
            "terminal_result_ref": terminal_ref,
        }
    )
    workflow._checkpoint = type("Complete", (), {"load": lambda self, ref: complete})()

    restored = asyncio.run(workflow._initialize(_resume_request(data, saved_ref)))

    assert isinstance(restored, MemoryWriteWorkflowResult)
    assert restored.status is MemoryWriteWorkflowStatus.NOOP
    assert restored.checkpoint_ref == saved_ref
    assert restored.terminal_result_ref == terminal_ref


def test_new_process_restores_human_approval_artifact() -> None:
    workflow, data = _workflow_and_data()

    class Human:
        def request(self, request: object) -> None:
            return None

        def read_decision(self, request_id: object) -> None:
            return None

    workflow._human = Human()
    workflow._suspend_for_human(data)
    ref = data.checkpoint_ref
    assert ref is not None
    fresh = LocalMemoryWriteWorkflow(
        canonical_read=workflow._canonical_read,
        commit=workflow._commit_port,
        artifacts=workflow._artifacts,
        lineage=workflow._lineage,
        checkpoint=workflow._checkpoint,
        information_boundary=workflow._boundary,
    )

    restored = asyncio.run(fresh._initialize(_resume_request(data, ref)))

    assert isinstance(restored, _WorkflowData)
    assert restored.approval_request == data.approval_request


def test_new_process_missing_lineage_and_corrupt_materialization_fail_closed() -> None:
    workflow, data = _workflow_and_data()
    ref = workflow._save_checkpoint(
        data,
        phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        resume_state=MemoryWriteState.NORMALIZE,
    )
    checkpoint = workflow._checkpoint.load(ref)
    repository = workflow._checkpoint
    fresh = LocalMemoryWriteWorkflow(
        canonical_read=workflow._canonical_read,
        commit=workflow._commit_port,
        artifacts=workflow._artifacts,
        checkpoint=repository,
        information_boundary=workflow._boundary,
    )
    legacy_without_revision_refs = checkpoint.model_copy(update={"candidate_revision_refs": ()})
    fresh._checkpoint = type(
        "MissingLineage", (), {"load": lambda self, ref: legacy_without_revision_refs}
    )()
    result = asyncio.run(fresh._initialize(_resume_request(data, ref)))
    assert isinstance(result, MemoryWriteWorkflowResult)
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert "CANDIDATE_LINEAGE_MISSING" in result.terminal_codes

    corrupt = checkpoint.model_copy(
        update={
            "current_candidate_id": None,
            "materialization_artifact": _artifact("9"),
        }
    )
    fresh._checkpoint = type("CorruptMaterialization", (), {"load": lambda self, ref: corrupt})()
    result = asyncio.run(fresh._initialize(_resume_request(data, ref)))
    assert isinstance(result, MemoryWriteWorkflowResult)
    assert result.status is MemoryWriteWorkflowStatus.FATAL
    assert "MATERIALIZATION_CORRUPT" in result.terminal_codes


def test_new_process_reconstructs_projection_receipts_and_tolerates_bad_freshness() -> None:
    workflow, data = _workflow_and_data()
    commit_result = workflow._commit_port.resolve_or_replay_exact(_commit_request())
    commit_id = commit_result.commit_id
    assert commit_id is not None
    projection = workflow._projection.request_or_read_by_effect_id(
        data.request.project_id,
        commit_id,
        StableId("effect.resume.projection"),
    )
    data.commit_result = commit_result
    data.projection = projection
    data.projection_effect_id = projection.effect_id
    ref = workflow._save_checkpoint(
        data,
        phase=MemoryWriteWorkflowPhase.PROJECTION_PENDING,
        resume_state=MemoryWriteState.FRESHNESS_GATE,
        accepted_commit_id=commit_id,
        commit_receipt_ref=_artifact("f"),
        projection_effect_id=projection.effect_id,
        projection_status=EffectStatus.COMPLETED,
        projection_receipt_ref=projection.projection_receipt_ref,
        projection_snapshot_id=projection.projection_snapshot_id,
        freshness_receipt_ref=projection.freshness_receipt_ref,
    )
    fresh = LocalMemoryWriteWorkflow(
        canonical_read=workflow._canonical_read,
        commit=workflow._commit_port,
        artifacts=workflow._artifacts,
        lineage=workflow._lineage,
        checkpoint=workflow._checkpoint,
        information_boundary=workflow._boundary,
    )
    restored = asyncio.run(fresh._initialize(_resume_request(data, ref)))
    assert isinstance(restored, _WorkflowData)
    assert restored.projection is not None
    assert restored.projection.status.value == "ready"

    checkpoint = workflow._checkpoint.load(ref).model_copy(
        update={"freshness_receipt_ref": _artifact("9")}
    )
    fresh._checkpoint = type("BadFreshness", (), {"load": lambda self, ref: checkpoint})()
    restored = asyncio.run(fresh._initialize(_resume_request(data, ref)))
    assert isinstance(restored, _WorkflowData)
    assert restored.projection is None
