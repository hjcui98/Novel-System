"""U3-C tests for planned versus completed Skill receipts."""

from __future__ import annotations

import pytest

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    ExecutionStatus,
    SkillContractRef,
    SkillExecutionReceipt,
)

HASH = ArtifactId("sha256:" + "a" * 64)


def _receipt(**updates: object) -> SkillExecutionReceipt:
    payload: dict[str, object] = {
        "receipt_id": StableId("skill-receipt.test"),
        "run_id": RunId("run.skill-test"),
        "task_id": TaskId("task.skill-test"),
        "skill": SkillContractRef(
            contract_id=StableId("skill.test"),
            version=SchemaVersion("1.0.0"),
            content_hash=HASH,
        ),
        "agent_type": AgentType.WRITER,
        "agent_mode": AgentMode.DRAFT,
        "planned_checkpoints": ("draft",),
        "selected_checkpoints": ("draft",),
        "status": ExecutionStatus.PLANNED,
        "latency_ms": 1,
    }
    payload.update(updates)
    return SkillExecutionReceipt.model_validate(payload)


def test_planned_receipt_does_not_claim_completion() -> None:
    receipt = _receipt()
    assert receipt.status is ExecutionStatus.PLANNED
    assert receipt.selected_checkpoints == ("draft",)
    assert receipt.completed_checkpoints == ()


def test_succeeded_receipt_requires_output_evidence() -> None:
    output = ArtifactRef(
        artifact_id=HASH,
        media_type="application/json",
        byte_length=2,
        schema_version=SchemaVersion("1.0.0"),
    )
    with pytest.raises(ValueError, match="output evidence"):
        _receipt(status=ExecutionStatus.SUCCEEDED)
    succeeded = _receipt(status=ExecutionStatus.SUCCEEDED, output_artifacts=(output,))
    assert succeeded.status is ExecutionStatus.SUCCEEDED


def test_planned_and_skipped_receipts_cannot_claim_completed_checkpoints() -> None:
    with pytest.raises(ValueError, match="planned Skill receipt"):
        _receipt(completed_checkpoints=("draft",))
    with pytest.raises(ValueError, match="skipped Skill receipt"):
        _receipt(
            status=ExecutionStatus.SKIPPED,
            selected_checkpoints=(),
            planned_checkpoints=(),
            output_artifacts=(
                ArtifactRef(
                    artifact_id=HASH,
                    media_type="application/json",
                    byte_length=2,
                    schema_version=SchemaVersion("1.0.0"),
                ),
            ),
        )
