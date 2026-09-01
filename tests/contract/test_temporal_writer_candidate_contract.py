"""Public contract checks for the isolated U7-B Temporal Writer candidate."""

from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError

from novel_agent.runtime.temporal_writer_candidate import (
    WRITER_TEMPORAL_NAMESPACE,
    WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
    WRITER_TEMPORAL_TASK_QUEUE,
    WriterSequenceState,
    WriterTemporalState,
    assert_public_writer_payload,
)


def test_u7b_temporal_contract_is_isolated_and_ref_only() -> None:
    assert WRITER_TEMPORAL_NAMESPACE != "default"
    assert WRITER_TEMPORAL_TASK_QUEUE.startswith("ns-u7b-")
    assert WRITER_TEMPORAL_PLUGIN_TASK_QUEUE.startswith("ns-u7b-")
    assert set(WriterTemporalState.__annotations__) == {
        "request_artifact_ref",
        "result_artifact_ref",
        "checkpoint_ref",
        "terminal_status",
        "final_candidate_id",
        "phase",
        "run_id",
        "task_id",
        "basis_commit",
        "policy_hash",
        "permission_hash",
        "command_id",
        "runtime_key",
        "track",
        "checkpoint_chapter",
        "readout_checkpoint_id",
        "question_id",
        "question_release",
        "gold_revealed",
        "evaluation_namespace",
        "workflow_build",
        "candidate_id",
        "attempt_fence",
        "await_acceptance",
        "hold_after_acceptance",
        "acceptance_status",
        "budget_extension",
        "settlement_required",
        "effect_identity",
        "settlement_artifact_ref",
        "settlement_status",
        "settled_commit_id",
        "effect_reconciled",
    }
    assert set(WriterSequenceState.__annotations__) == {
        "request_artifact_refs",
        "request_identities",
        "completed_result_refs",
        "next_index",
        "continue_as_new",
        "continue_as_new_after",
        "pending_acceptance",
        "pending_effect",
        "pending_repair",
        "pending_command",
        "pending_projection",
        "policy_hash",
        "permission_hash",
        "runtime_key",
        "phase",
        "result_artifact_ref",
        "checkpoint_ref",
        "terminal_status",
        "final_candidate_id",
        "effect_identities",
        "settlement_required",
        "settlement_artifact_refs",
        "settlement_effect_ids",
        "settled_commit_ids",
        "effect_reconciled_count",
        "pause_after_index",
        "settlement_artifact_ref",
        "effect_identity",
        "settled_commit_id",
        "effect_reconciled",
    }
    assert_public_writer_payload(
        {
            "request_artifact_ref": {"artifact_id": "sha256:" + "a" * 64},
            "run_id": "run.u7b",
            "task_id": "task.u7b",
        }
    )
    with pytest.raises(ApplicationError, match="author_plan"):
        assert_public_writer_payload({"author_plan": "private"})
    with pytest.raises(ApplicationError, match="question_text"):
        assert_public_writer_payload({"question_text": "released only to the Activity"})
    with pytest.raises(ApplicationError, match="target_realization"):
        assert_public_writer_payload({"target_realization": "evaluator-only"})
