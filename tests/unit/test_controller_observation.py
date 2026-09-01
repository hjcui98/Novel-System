"""Unit tests for controller observation assembly and compaction."""

from __future__ import annotations

import json
from typing import cast

import pytest
from pydantic import JsonValue

from novel_agent.domain.ids import CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    ChannelHit,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalChannel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    ContextBudget,
    MemoryResolutionRequest,
    RequiredSnapshotPolicy,
    RetrievalBudget,
    ToolFailureCode,
    ToolResult,
    ToolResultStatus,
)
from novel_agent.runtime.memory_controller import ControllerStateView
from novel_agent.services.controller_observation import (
    CompactionRoute,
    ContextAssemblyBudgetExceeded,
    ControllerObservationAssembler,
)

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.observation")


def _need(
    *,
    need_id: str = "need.1",
    requirement: RequirementLevel = RequirementLevel.MANDATORY,
    query_text: str = "hero injury",
) -> Stage1MemoryNeed:
    return Stage1MemoryNeed(
        need_id=StableId(need_id),
        run_id=RunId("run.observation"),
        task_id=TaskId("task.observation"),
        base_commit=COMMIT,
        chapter_target=20,
        need_type="current state",
        query_intent=Stage1QueryIntent.CURRENT_STATE,
        query_text=query_text,
        why_needed="continuity",
        risk_level=NeedRisk.HIGH,
        requirement=requirement,
        preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
        allowed_candidate_pools=(CandidatePool.R1,),
        stop_condition="supported state found",
    )


def _request(
    needs: tuple[Stage1MemoryNeed, ...],
    *,
    context_tokens: int = 50_000,
) -> MemoryResolutionRequest:
    return MemoryResolutionRequest(
        request_id=StableId("request.observation"),
        run_id=RunId("run.observation"),
        task_id=TaskId("task.observation"),
        project_id=ProjectId("project.observation"),
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
        task_contract="write chapter 20",
        initial_memory_needs=needs,
        worldline="main",
        narrative_chapter=20,
        access_scope=AccessScope.WRITER_SAFE,
        retrieval_budget=RetrievalBudget(
            max_rounds=3,
            max_tool_calls=12,
            max_candidates=20,
        ),
        context_budget=ContextBudget(token_budget=context_tokens),
    )


def _unit(text: str, unit_id: str = "unit.1") -> RetrievalUnit:
    return RetrievalUnit(
        unit_id=StableId(unit_id),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        text=text,
        narrative_start=20,
    )


def _hit(text: str, unit_id: str = "unit.1") -> ChannelHit:
    return ChannelHit(
        unit=_unit(text, unit_id=unit_id),
        channel=RetrievalChannel.R1_EXACT,
        channel_rank=1,
        raw_score=1.0,
        candidate_count=1,
        hit_reason="test",
    )


def _tool_result(hit: ChannelHit | None = None, *, coverage: float = 1.0) -> ToolResult:
    payload: JsonValue | None = None
    if hit is not None:
        payload = {"hits": [json.loads(hit.model_dump_json())]}
    return ToolResult(
        tool_call_id=StableId("tool.observation"),
        status=ToolResultStatus.SUCCEEDED,
        basis_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        payload=payload,
        coverage=coverage,
        audit_ref=StableId("audit.observation"),
    )


def _state(
    needs: tuple[Stage1MemoryNeed, ...],
    tool_calls: tuple[tuple[StableId, str, ToolResult], ...] = (),
) -> ControllerStateView:
    return ControllerStateView(
        request=_request(needs),
        tool_calls=tool_calls,
    )


def test_assembler_rejects_non_positive_preview_chars() -> None:
    with pytest.raises(ValueError, match="preview character budget must be positive"):
        ControllerObservationAssembler(preview_chars=0)


def test_assemble_without_compaction() -> None:
    need = _need()
    hit = _hit("short preview text")
    state = _state(
        (need,),
        ((need.need_id, "memory.search_exact", _tool_result(hit)),),
    )
    assembler = ControllerObservationAssembler()
    assembly = assembler.assemble(
        state,
        available_actions=[{"action_id": "retry"}],
        registered_action_ids=["retry"],
        round_index=1,
        max_agentic_actions=3,
        available_input_tokens=50_000,
    )
    assert assembly.compaction_route is CompactionRoute.NONE
    assert assembly.preview_count == 1
    assert assembly.payload["c3_admission"] == "NOT_ADMITTED"
    telemetry = assembly.telemetry()
    assert telemetry["context_level"] == "C1+C2"
    assert telemetry["available_input_tokens"] == 50_000
    assert telemetry["preview_count"] == 1
    assert telemetry["c3_admission"] == "NOT_ADMITTED"


def test_assemble_without_hits_reports_c1_and_zero_preview_cause() -> None:
    need = _need()
    assembly = ControllerObservationAssembler().assemble(
        _state((need,)),
        available_actions=[{"action_id": "retry"}],
        registered_action_ids=["retry"],
        round_index=1,
        max_agentic_actions=3,
        available_input_tokens=50_000,
    )

    assert assembly.context_level == "C1"
    assert assembly.preview_count == 0
    assert assembly.zero_preview_cause == "no_resolved_hits"
    assert assembly.telemetry()["zero_preview_cause"] == "no_resolved_hits"


def test_assemble_rejects_zero_input_budget() -> None:
    need = _need()
    state = _state((need,))
    assembler = ControllerObservationAssembler()
    with pytest.raises(ContextAssemblyBudgetExceeded, match="protected Controller identity"):
        assembler.assemble(
            state,
            available_actions=[],
            registered_action_ids=[],
            round_index=0,
            max_agentic_actions=1,
            available_input_tokens=0,
        )


def test_assemble_truncates_candidate_previews() -> None:
    need = _need()
    hits = [_hit("y" * 500, unit_id=f"unit.{index}") for index in range(20)]
    payload_hits = [json.loads(hit.model_dump_json()) for hit in hits]
    result = ToolResult(
        tool_call_id=StableId("tool.observation"),
        status=ToolResultStatus.SUCCEEDED,
        basis_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        payload={"hits": payload_hits},
        coverage=1.0,
        audit_ref=StableId("audit.observation"),
    )
    state = _state(
        (need,),
        ((need.need_id, "memory.search_exact", result),),
    )
    assembler = ControllerObservationAssembler(preview_chars=240)
    assembly = assembler.assemble(
        state,
        available_actions=[],
        registered_action_ids=[],
        round_index=1,
        max_agentic_actions=1,
        available_input_tokens=2000,
    )
    assert assembly.compaction_route is CompactionRoute.TRUNCATE_CANDIDATE_TEXT
    assert assembly.truncated_preview_count == 20
    assert assembly.preview_count == 20


def test_assemble_drops_optional_needs() -> None:
    mandatory = _need(need_id="need.mandatory")
    optional = _need(
        need_id="need.optional",
        requirement=RequirementLevel.OPTIONAL,
        query_text="optional detail",
    )
    hit = _hit("preview for compaction", unit_id="unit.optional")
    state = _state(
        (mandatory, optional),
        ((optional.need_id, "memory.search_exact", _tool_result(hit)),),
    )
    assembler = ControllerObservationAssembler(preview_chars=64)
    assembly = assembler.assemble(
        state,
        available_actions=[{"action_id": "retry", "label": "retry"}],
        registered_action_ids=["retry"],
        round_index=2,
        max_agentic_actions=2,
        available_input_tokens=400,
    )
    assert assembly.compaction_route is CompactionRoute.DROP_OPTIONAL_NEED
    assert assembly.dropped_optional_needs == 1
    summaries = cast(list[dict[str, JsonValue]], assembly.payload["need_summaries"])
    assert len(summaries) == 1
    assert summaries[0]["id"] == "need.mandatory"


def test_assemble_compacts_oldest_actions() -> None:
    need = _need()
    tool_calls: list[tuple[StableId, str, ToolResult]] = []
    for index in range(6):
        hit = _hit(f"action outcome {index}", unit_id=f"unit.{index}")
        tool_calls.append(
            (
                need.need_id,
                "memory.search_exact",
                _tool_result(hit),
            )
        )
    state = _state((need,), tuple(tool_calls))
    assembler = ControllerObservationAssembler(preview_chars=32)
    assembly = assembler.assemble(
        state,
        available_actions=[{"action_id": f"action.{index}"} for index in range(4)],
        registered_action_ids=[f"action.{index}" for index in range(4)],
        round_index=3,
        max_agentic_actions=4,
        available_input_tokens=800,
    )
    assert assembly.compaction_route is CompactionRoute.COMPACT_OLDEST_ACTIONS
    assert assembly.dropped_actions >= 1


def test_assemble_preserves_one_mandatory_preview() -> None:
    need = _need()
    hits = [_hit("y" * 500, unit_id=f"unit.{index}") for index in range(20)]
    payload_hits = [json.loads(hit.model_dump_json()) for hit in hits]
    result = ToolResult(
        tool_call_id=StableId("tool.observation"),
        status=ToolResultStatus.SUCCEEDED,
        basis_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        payload={"hits": payload_hits},
        coverage=1.0,
        audit_ref=StableId("audit.observation"),
    )
    state = _state(
        (need,),
        ((need.need_id, "memory.search_exact", result),),
    )
    assembler = ControllerObservationAssembler(preview_chars=240)
    assembly = assembler.assemble(
        state,
        available_actions=[],
        registered_action_ids=[],
        round_index=1,
        max_agentic_actions=1,
        available_input_tokens=1500,
    )
    assert assembly.compaction_route is CompactionRoute.PRESERVE_MANDATORY_PREVIEW
    assert assembly.preview_count == 1
    previews = cast(list[dict[str, JsonValue]], assembly.payload["candidate_previews"])
    assert previews[0]["need_id"] == need.need_id.root
    assert len(str(previews[0]["text"])) <= 32


def test_assemble_raises_when_mandatory_contract_exceeds_budget() -> None:
    need = _need(query_text="x" * 5000)
    state = _state((need,))
    assembler = ControllerObservationAssembler()
    with pytest.raises(ContextAssemblyBudgetExceeded, match="mandatory Need contract"):
        assembler.assemble(
            state,
            available_actions=[],
            registered_action_ids=[],
            round_index=0,
            max_agentic_actions=1,
            available_input_tokens=5,
        )


def test_assemble_skips_invalid_tool_payloads() -> None:
    need = _need()
    failed = ToolResult(
        tool_call_id=StableId("tool.failed"),
        status=ToolResultStatus.FAILED,
        basis_commit=COMMIT,
        failure_code=ToolFailureCode.TIMEOUT,
        audit_ref=StableId("audit.failed"),
    )
    missing_hits = ToolResult(
        tool_call_id=StableId("tool.missing-hits"),
        status=ToolResultStatus.SUCCEEDED,
        basis_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        payload={"hits": "not-a-list"},
        coverage=1.0,
        audit_ref=StableId("audit.missing-hits"),
    )
    invalid_hit = ToolResult(
        tool_call_id=StableId("tool.invalid-hit"),
        status=ToolResultStatus.SUCCEEDED,
        basis_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        payload={"hits": [1]},
        coverage=1.0,
        audit_ref=StableId("audit.invalid-hit"),
    )
    state = _state(
        (need,),
        (
            (need.need_id, "memory.search_exact", failed),
            (need.need_id, "memory.search_temporal", missing_hits),
            (need.need_id, "memory.search_anchor_bm25", invalid_hit),
        ),
    )
    assembly = ControllerObservationAssembler().assemble(
        state,
        available_actions=[],
        registered_action_ids=[],
        round_index=1,
        max_agentic_actions=1,
        available_input_tokens=50_000,
    )
    assert assembly.preview_count == 0
