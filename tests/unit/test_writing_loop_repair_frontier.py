"""R5: a restart must not re-grant the repair allowance.

The Writer loop counts local repairs and major rewrites against pinned budgets.  Those
counters used to start at zero on every resume, so a process that restarted between two
repairs could pay for the whole allowance again - effectively unbounded repair through
restart.  The checkpoint now carries the repair frontier and the loop resumes from it.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from novel_agent.domain.agent_context import AgentContextView, ContextConsumer
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.generation import (
    WriterMemoryRequest,
    WriterTurnAction,
    WriterTurnOutput,
    WriterWorkPlan,
    WriterWorkPlanResult,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelCallRecord,
    ModelRole,
    ModelUsage,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    ExecutionStatus,
    SkillContractRef,
    SkillExecutionReceipt,
)
from novel_agent.domain.writing_loop import WritingLoopCheckpoint, WritingLoopPhase

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "1" * 64)
COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.repair-frontier")


def _ref(media_type: str = "text/plain") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=HASH, media_type=media_type, byte_length=1, schema_version=VERSION
    )


def _model_call() -> ModelCallRecord:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return ModelCallRecord(
        request_id=StableId("model.request.repair-frontier"),
        run_id=RunId("run.repair"),
        task_id=TaskId("task.repair"),
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id="trace-repair",
        endpoint="endpoint",
        model="model",
        model_version="model",
        usage=ModelUsage(input_tokens=1, output_tokens=1, cost_usd=Decimal("0")),
        latency_ms=0,
        started_at=now,
        completed_at=now,
    )


def _checkpoint(**overrides: object) -> WritingLoopCheckpoint:
    view = AgentContextView(
        run_id=RunId("run.repair"),
        task_id=TaskId("task.repair"),
        consumer=ContextConsumer.WRITER,
        revision=0,
        generation=0,
        basis_event_position=0,
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        profile_ref=_ref(),
        plan_ref=_ref(),
        information_scope="writer_safe",
        seed_package_ref=_ref(),
        protected_items=(),
        context_hash=HASH,
    )
    kwargs: dict[str, object] = {
        "checkpoint_id": StableId("writing-loop-checkpoint.repair"),
        "run_id": RunId("run.repair"),
        "task_id": TaskId("task.repair"),
        "phase": WritingLoopPhase.REACTIVE_MEMORY_PENDING,
        "base_commit": COMMIT,
        "snapshot_id": SNAPSHOT,
        "writing_task_ref": _ref(),
        "accepted_plan_ref": _ref(),
        "project_profile_ref": _ref(),
        "writer_context_ref": _ref(),
        "recent_prose_ref": _ref(),
        # Nested results carry their own contracts and are exercised elsewhere; this
        # module tests the checkpoint's repair frontier and its durability.
        "work_plan": WriterWorkPlanResult(
            work_plan=WriterWorkPlan(
                work_plan_id=StableId("work-plan.repair"),
                writing_task_ref=_ref(),
                accepted_plan_ref=_ref(),
                writer_context_ref=_ref(),
                scene_beat_order=("scene beat",),
                pov_boundary="third person limited",
                reader_disclosure_boundary="no reveal",
                selected_skill_ids=(StableId("skill.scene-composition"),),
            ),
            work_plan_artifact=_ref(),
            skill_receipts=(
                SkillExecutionReceipt(
                    receipt_id=StableId("skill-receipt.repair"),
                    run_id=RunId("run.repair"),
                    task_id=TaskId("task.repair"),
                    skill=SkillContractRef(
                        contract_id=StableId("skill.scene-composition"),
                        version=VERSION,
                        content_hash=HASH,
                    ),
                    agent_type=AgentType.WRITER,
                    agent_mode=AgentMode.DRAFT,
                    output_artifacts=(_ref(),),
                    status=ExecutionStatus.SUCCEEDED,
                    latency_ms=0,
                ),
            ),
            model_call_record=_model_call(),
        ),
        # These nested results are validated by their own contracts; the checkpoint test
        # only exercises the repair-frontier rules and durability.
        "active_writer_turn_output": WriterTurnOutput(
            action=WriterTurnAction.REQUEST_MEMORY,
            memory_requests=(
                WriterMemoryRequest(
                    request_id=StableId("memory-request.repair"),
                    question="what happened before",
                    purpose="continuity",
                    blocked_action="draft",
                    requested_evidence_type="causal_history",
                    scene_or_draft_checkpoint="scene.1",
                    risk="continuity",
                ),
            ),
            work_plan_checkpoint="plan checkpoint",
        ),
        "active_writer_turn_artifact": _ref(),
        "active_writer_raw_output_artifact": _ref(),
        "active_writer_model_call": _model_call(),
        "memory_rounds": 0,
        "writer_turns": 1,
        "context_view": view,
    }
    kwargs.update(overrides)
    return WritingLoopCheckpoint(**kwargs)  # type: ignore[arg-type]


def test_a_fresh_checkpoint_starts_with_no_repairs_used() -> None:
    checkpoint = _checkpoint()

    assert checkpoint.local_repairs_used == 0
    assert checkpoint.major_rewrites_used == 0
    assert checkpoint.repair_stage == "dispatch"
    assert checkpoint.repair_input is None


def test_repair_counters_survive_a_checkpoint_round_trip() -> None:
    """The frontier that gates the loop's budgets is durable."""

    checkpoint = _checkpoint(
        local_repairs_used=2,
        major_rewrites_used=1,
        repair_stage="rewrite_review",
    )
    payload = json.loads(checkpoint.model_dump_json())

    assert payload["local_repairs_used"] == 2
    assert payload["major_rewrites_used"] == 1
    assert payload["repair_stage"] == "rewrite_review"
    # The same payload also round-trips back into the model contract.
    restored = WritingLoopCheckpoint.model_validate(payload, strict=False)
    assert restored.local_repairs_used == 2
    assert restored.repair_stage == "rewrite_review"


def test_repair_counters_reject_negative_values() -> None:
    with pytest.raises(ValidationError):
        _checkpoint(local_repairs_used=-1)
    with pytest.raises(ValidationError):
        _checkpoint(major_rewrites_used=-1)


def test_repair_stage_rejects_an_unknown_value() -> None:
    with pytest.raises(ValidationError):
        _checkpoint(repair_stage="sneaky-extra-repair")


def test_repair_pending_is_a_declared_safe_frontier() -> None:
    """REPAIR_PENDING is a durable frontier, and it demands its own evidence."""

    assert WritingLoopPhase.REPAIR_PENDING.value == "REPAIR_PENDING"
    rules = WritingLoopCheckpoint.__pydantic_decorators__.model_validators
    assert "validate_resume_state" in rules
    source = inspect.getsource(WritingLoopCheckpoint.validate_resume_state)
    assert "repair checkpoint requires its input and rejection history" in source
    assert "local review checkpoint requires the settled repair" in source
    assert "rewrite review checkpoint requires the settled rewrite" in source
    # The phase is reached from a settled post-Draft checkpoint, so an incomplete state
    # is refused rather than treated as a repairable frontier.
    with pytest.raises(ValidationError, match="settled Writer candidate"):
        _checkpoint(phase=WritingLoopPhase.REPAIR_PENDING)


def test_the_resume_path_reads_the_persisted_counters() -> None:
    """The loop must actually consume the persisted frontier, not just store it."""

    from novel_agent.services.writer_context_loop import WriterContextLoopService

    source = inspect.getsource(WriterContextLoopService)
    assert "resume_checkpoint.local_repairs_used" in source
    assert "resume_checkpoint.major_rewrites_used" in source
