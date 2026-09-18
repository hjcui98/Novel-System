from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.runtime.isolated import (
    FaultInjectionEffectStatusResolver,
    FaultInjectionWritingLeaf,
    StrictDeterministicCandidateMaterializer,
    StrictFakePlanningLeaf,
)
from novel_agent.adapters.runtime.materializers import PlanCandidateMaterializer
from novel_agent.adapters.runtime.stage3_writer import Stage3WritingLeafAdapter
from novel_agent.adapters.runtime.stage4_planner import (
    Stage4PlanningInvocation,
    Stage4PlanningLeafAdapter,
)
from novel_agent.domain.creative_runtime import (
    AcceptedCandidateBinding,
    ActorKind,
    CandidateBinding,
    CandidateKind,
    PlanningLoopRequest,
    PlanningLoopResult,
    PlanningTerminalStatus,
)
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import (
    ChannelHit,
    ContextBudgetReport,
    FacetClosureStatus,
    FacetEvidenceReceipt,
    FusedCandidate,
    NeedExecutionStatus,
    NeedFacetKind,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalTrace,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1ContextPackage,
    Stage1QueryIntent,
)
from novel_agent.domain.memory_write import (
    MemoryRepairFinding,
    MemoryRepairOwner,
    SourceVisibilityReceipt,
)
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.planning import (
    PlanningBudgets,
    PlanningLoopCheckpoint,
    PlanningLoopPhase,
    PlanningProblemIdentitySeed,
)
from novel_agent.domain.planning import (
    PlanningLoopRequest as Stage4PlanningLoopRequest,
)
from novel_agent.domain.planning import (
    PlanningLoopResult as Stage4PlanningLoopResult,
)
from novel_agent.domain.planning import (
    PlanningLoopTerminal as Stage4PlanningLoopTerminal,
)
from novel_agent.domain.runtime import EffectReceipt, EffectStatus
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    ContextBudget,
    ContractRef,
    ExecutionStatus,
    PlanningTask,
    PlanProposal,
    RetrievalBudget,
)
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.planning_context_loop import PlanningContextLoopService
from novel_agent.services.writer_context_loop import WriterContextLoopService
from novel_agent.services.writer_reactive_memory import ReactiveMemoryInputs
from tests.factories import make_manifest

_STAGE4_BUDGETS = PlanningBudgets(
    retrieval=RetrievalBudget(),
    context=ContextBudget(token_budget=4_000),
)
_STAGE4_FINGERPRINT = ArtifactId("sha256:" + "9" * 64)


def test_strict_fake_planner_has_deterministic_immutable_lineage(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path))
    request = PlanningLoopRequest(
        run_id=RunId("run.planner"),
        task_id=TaskId("task.planner"),
        project_id=ProjectId("project.test"),
        basis_commit=CommitId("sha256:" + "1" * 64),
    )
    result = asyncio.run(StrictFakePlanningLeaf(artifacts).run(request))
    repeated = asyncio.run(StrictFakePlanningLeaf(artifacts).run(request))
    assert result == repeated
    assert result.status is PlanningTerminalStatus.PLAN_CANDIDATE_READY
    assert result.candidate is not None
    artifacts.read_verified(result.candidate.artifact_ref)

    blocked = asyncio.run(
        StrictFakePlanningLeaf(artifacts, terminal=PlanningTerminalStatus.BLOCKED).run(request)
    )
    assert blocked.status is PlanningTerminalStatus.BLOCKED
    assert blocked.failure_code == "planner_blocked"


class _Loop:
    def __init__(self, result: WritingLoopResult) -> None:
        self.result = result
        self.calls: list[tuple[object, object, object]] = []

    async def execute(
        self, request: object, model_request: object, reactive_inputs: object
    ) -> WritingLoopResult:
        self.calls.append((request, model_request, reactive_inputs))
        return self.result


class _Request:
    def __init__(self, run_id: RunId, task_id: TaskId) -> None:
        self.run_id = run_id
        self.task_id = task_id


def test_real_stage3_adapter_uses_only_public_request_and_result() -> None:
    request = cast(
        WritingLoopRequest,
        _Request(RunId("run.writer"), TaskId("task.writer")),
    )
    result = WritingLoopResult(
        result_id=StableId("writer.result"),
        run_id=request.run_id,
        task_id=request.task_id,
        status=WritingLoopTerminalStatus.MODEL_UNAVAILABLE,
        failure_detail="offline endpoint unavailable",
    )
    loop = _Loop(result)
    model_request = cast(ModelRequest, object())
    reactive = cast(ReactiveMemoryInputs, object())
    adapter = Stage3WritingLeafAdapter(
        cast(WriterContextLoopService, loop),
        lambda _: model_request,
        lambda _: reactive,
    )
    assert asyncio.run(adapter.run(request)) == result
    assert loop.calls == [(request, model_request, reactive)]

    wrong = result.model_copy(update={"task_id": TaskId("task.other")})
    bad_adapter = Stage3WritingLeafAdapter(
        cast(WriterContextLoopService, _Loop(wrong)),
        lambda _: model_request,
        lambda _: reactive,
    )
    with pytest.raises(RuntimeError, match="cross-task lineage"):
        asyncio.run(bad_adapter.run(request))


def test_real_stage4_adapter_preserves_candidate_and_review_lineage(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "stage4"))
    author_ref = artifacts.put(b"author intent", "text/plain", SchemaVersion("1.0.0"))
    review_ref = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    simple = PlanningLoopRequest(
        run_id=RunId("run.stage4-adapter"),
        task_id=TaskId("task.stage4-adapter"),
        project_id=ProjectId("project.test"),
        basis_commit=CommitId("sha256:" + "1" * 64),
        basis_snapshot=StableId("snapshot.stage4-adapter"),
        input_artifact_refs=(author_ref,),
    )
    task = PlanningTask.model_construct(
        planning_task_id=StableId(simple.task_id.root),
        project_id=simple.project_id,
        mode=AgentMode.CHAPTER,
        base_commit=simple.basis_commit,
        source_ids=(StableId("source.author"),),
    )
    detailed = Stage4PlanningLoopRequest.model_construct(
        request_id=StableId("request.stage4-adapter"),
        run_id=simple.run_id,
        task_id=simple.task_id,
        project_id=simple.project_id,
        task=task,
        author_intent_artifacts=(author_ref,),
        snapshot_id=simple.basis_snapshot,
        budgets=_STAGE4_BUDGETS,
        configuration_fingerprint=_STAGE4_FINGERPRINT,
        model_fingerprint=_STAGE4_FINGERPRINT,
    )
    proposal = PlanProposal.model_construct(
        proposal_id=StableId("proposal.stage4-adapter"),
        project_id=simple.project_id,
        mode=AgentMode.CHAPTER,
        base_commit=simple.basis_commit,
        items=(),
        unresolved=(),
        coverage=1.0,
        receipt=AgentExecutionReceipt(
            receipt_id=StableId("receipt.stage4-adapter"),
            run_id=simple.run_id,
            task_id=simple.task_id,
            agent_spec=ContractRef(
                contract_id=StableId("contract.planner"),
                version=SchemaVersion("1.0.0"),
                content_hash=_STAGE4_FINGERPRINT,
            ),
            agent_type=AgentType.PLANNER,
            agent_mode=AgentMode.CHAPTER,
            prompt_fingerprint=_STAGE4_FINGERPRINT,
            configuration_fingerprint=_STAGE4_FINGERPRINT,
            status=ExecutionStatus.SUCCEEDED,
            started_at=datetime(2026, 8, 10, tzinfo=UTC),
            completed_at=datetime(2026, 8, 10, tzinfo=UTC),
            latency_ms=0,
        ),
    )
    stage4_result = Stage4PlanningLoopResult.model_construct(
        request_id=detailed.request_id,
        terminal=Stage4PlanningLoopTerminal.PLAN_CANDIDATE_READY,
        proposal=proposal,
        plan_review_ref=review_ref,
        event_artifacts=(review_ref,),
        diagnostic_codes=(),
        degraded=False,
    )

    class _Stage4Loop:
        async def run(self, **_: object) -> Stage4PlanningLoopResult:
            return stage4_result

    adapter = Stage4PlanningLeafAdapter(
        cast(PlanningContextLoopService, _Stage4Loop()),
        artifacts,
        lambda _: Stage4PlanningInvocation(
            request=detailed,
            model_request=lambda _phase, _mode, _attempt: cast(ModelRequest, object()),
        ),
        schema_version=SchemaVersion("1.0.0"),
    )
    result = asyncio.run(adapter.run(simple))
    assert result.status is PlanningTerminalStatus.PLAN_CANDIDATE_READY
    assert result.candidate is not None
    assert result.candidate.basis_commit == simple.basis_commit
    assert review_ref in result.candidate.lineage_artifact_refs
    assert artifacts.read_verified(result.candidate.artifact_ref)
    assert (
        Stage4PlanningLeafAdapter._terminal(Stage4PlanningLoopTerminal.YIELDED)
        is PlanningTerminalStatus.YIELDED
    )
    assert (
        Stage4PlanningLeafAdapter._terminal(Stage4PlanningLoopTerminal.HUMAN_REQUIRED)
        is PlanningTerminalStatus.WAITING_INPUT
    )
    assert (
        Stage4PlanningLeafAdapter._terminal(Stage4PlanningLoopTerminal.DEGRADED_NOT_PROMOTABLE)
        is PlanningTerminalStatus.BLOCKED
    )
    checkpoint_ref = artifacts.put(
        b'{"checkpoint":true}',
        "application/vnd.novel-agent.planning-loop-checkpoint+json",
        SchemaVersion("1.0.0"),
    )
    yielded_result = Stage4PlanningLoopResult.model_construct(
        request_id=detailed.request_id,
        terminal=Stage4PlanningLoopTerminal.YIELDED,
        event_artifacts=(checkpoint_ref,),
        diagnostic_codes=("PLAN_REVISION_SLICE_EXHAUSTED",),
        degraded=False,
    )

    class _YieldedStage4Loop:
        async def run(self, **_: object) -> Stage4PlanningLoopResult:
            return yielded_result

    yielded_adapter = Stage4PlanningLeafAdapter(
        cast(PlanningContextLoopService, _YieldedStage4Loop()),
        artifacts,
        lambda _: Stage4PlanningInvocation(
            request=detailed,
            model_request=lambda _phase, _mode, _attempt: cast(ModelRequest, object()),
        ),
        schema_version=SchemaVersion("1.0.0"),
    )
    yielded = asyncio.run(yielded_adapter.run(simple))
    assert yielded.status is PlanningTerminalStatus.YIELDED
    assert yielded.failure_code == "PLAN_REVISION_SLICE_EXHAUSTED"
    assert yielded.artifact_refs == (checkpoint_ref,)


def test_stage4_adapter_keeps_a_proposalless_escalation_waiting(tmp_path: Path) -> None:
    """An escalation before any proposal must wait, not assert a proposal into being.

    An inquiry review or a reviewer-memory review can return HUMAN_REQUIRED before
    the planner produced a proposal.  There is nothing to accept at that point, so
    the adapter keeps the planning front waiting instead of raising.
    """

    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "stage4-escalated"))
    author_ref = artifacts.put(b"author intent", "text/plain", SchemaVersion("1.0.0"))
    review_ref = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    simple = PlanningLoopRequest(
        run_id=RunId("run.stage4-escalated"),
        task_id=TaskId("task.stage4-escalated"),
        project_id=ProjectId("project.test"),
        basis_commit=CommitId("sha256:" + "2" * 64),
        basis_snapshot=StableId("snapshot.stage4-escalated"),
        input_artifact_refs=(author_ref,),
    )
    task = PlanningTask.model_construct(
        planning_task_id=StableId(simple.task_id.root),
        project_id=simple.project_id,
        mode=AgentMode.CHAPTER,
        base_commit=simple.basis_commit,
        source_ids=(StableId("source.author"),),
    )
    detailed = Stage4PlanningLoopRequest.model_construct(
        request_id=StableId("request.stage4-escalated"),
        run_id=simple.run_id,
        task_id=simple.task_id,
        project_id=simple.project_id,
        task=task,
        author_intent_artifacts=(author_ref,),
        snapshot_id=simple.basis_snapshot,
        budgets=_STAGE4_BUDGETS,
        configuration_fingerprint=_STAGE4_FINGERPRINT,
        model_fingerprint=_STAGE4_FINGERPRINT,
    )
    escalated_result = Stage4PlanningLoopResult.model_construct(
        request_id=detailed.request_id,
        terminal=Stage4PlanningLoopTerminal.HUMAN_REQUIRED,
        proposal=None,
        inquiry_review_ref=review_ref,
        event_artifacts=(review_ref,),
        diagnostic_codes=("INQUIRY_REVIEW_HUMAN_REQUIRED",),
        degraded=False,
    )

    class _EscalatedStage4Loop:
        async def run(self, **_: object) -> Stage4PlanningLoopResult:
            return escalated_result

    adapter = Stage4PlanningLeafAdapter(
        cast(PlanningContextLoopService, _EscalatedStage4Loop()),
        artifacts,
        lambda _: Stage4PlanningInvocation(
            request=detailed,
            model_request=lambda _phase, _mode, _attempt: cast(ModelRequest, object()),
        ),
        schema_version=SchemaVersion("1.0.0"),
    )

    result = asyncio.run(adapter.run(simple))

    assert result.status is PlanningTerminalStatus.WAITING_INPUT
    assert result.candidate is None
    assert result.failure_code == "INQUIRY_REVIEW_HUMAN_REQUIRED"


def test_stage4_adapter_binds_evidence_limited_memory_gap_finding(tmp_path: Path) -> None:
    outcome = _run_stage4_gap_adapter(tmp_path)
    assert len(outcome.findings) == 1
    finding = outcome.findings[0]
    artifacts = outcome.artifacts

    assert finding.planner_attempt_id == StableId("attempt.stage4-gap")
    assert finding.cutoff.chapter_index == 4
    assert finding.no_progress_key.root.startswith("memory-problem.")
    assert finding.attempt_problem_key is not None
    assert finding.source_artifact_refs
    assert finding.repair_owner is MemoryRepairOwner.GRAPH_CURATOR
    assert finding.need_query == "陈长生当前经脉状态"
    assert finding.semantic_question == "预注册: 陈长生当前经脉状态"
    visibility = SourceVisibilityReceipt.model_validate_json(
        artifacts.read_verified(finding.source_visibility_receipt_refs[0]), strict=True
    )
    assert visibility.source_artifact == finding.source_artifact_refs[0]
    assert visibility.boundary_id == finding.information_boundary.boundary_id
    assert visibility.visible_through == finding.cutoff


def test_stage4_adapter_reports_unresolved_dependency_when_source_is_silent(
    tmp_path: Path,
) -> None:
    """Q02: name mentions and retrieval permission are not proposition support."""

    outcome = _run_stage4_gap_adapter(
        tmp_path,
        question_purpose="verify_history",
        dependency_expectation="must_establish_existing_fact",
        with_unit_evidence=False,
    )

    assert outcome.findings == ()
    assert outcome.result.failure_code == "historical_dependency_unresolved"
    assert "historical_dependency_unresolved" in (outcome.result.failure_detail or "")


def test_stage4_adapter_never_repairs_canon_for_a_future_design_question(
    tmp_path: Path,
) -> None:
    """Q01: a chapter-set question designs future content, not missing canon."""

    outcome = _run_stage4_gap_adapter(
        tmp_path,
        question_purpose="design_future",
        dependency_expectation="no_historical_precondition",
    )

    assert outcome.findings == ()
    assert outcome.result.failure_code == "historical_dependency_unresolved"


def test_stage4_adapter_splits_a_mixed_facet_problem_by_owner(tmp_path: Path) -> None:
    """Q11: only the confirmed relation facet reaches the Graph Curator."""

    outcome = _run_stage4_gap_adapter(
        tmp_path,
        extra_facet_kind=NeedFacetKind.CAUSAL_HISTORY,
    )
    by_owner = {finding.repair_owner: finding for finding in outcome.findings}
    relation_finding = by_owner[MemoryRepairOwner.GRAPH_CURATOR]
    assert set(by_owner) == {MemoryRepairOwner.GRAPH_CURATOR, MemoryRepairOwner.ORDINARY_CURATOR}
    # Each child owns exactly the facets its profile can represent.
    assert by_owner[MemoryRepairOwner.GRAPH_CURATOR].owned_facet_ids == (
        relation_finding.owned_facet_ids[0],
    )
    assert by_owner[MemoryRepairOwner.ORDINARY_CURATOR].owned_facet_ids == (
        StableId("facet.stage4-gap.causal"),
    )
    # Both children describe one problem: they share the durable problem key.
    assert (
        by_owner[MemoryRepairOwner.GRAPH_CURATOR].no_progress_key
        == by_owner[MemoryRepairOwner.ORDINARY_CURATOR].no_progress_key
    )
    assert (
        by_owner[MemoryRepairOwner.GRAPH_CURATOR].finding_id
        != by_owner[MemoryRepairOwner.ORDINARY_CURATOR].finding_id
    )


@dataclass(frozen=True, slots=True)
class _GapAdapterOutcome:
    """One Stage 4 gap-adapter run: the terminal plus any emitted findings."""

    result: PlanningLoopResult
    findings: tuple[MemoryRepairFinding, ...]
    artifacts: ArtifactRepository


def _run_stage4_gap_adapter(
    tmp_path: Path,
    *,
    question_purpose: str | None = None,
    dependency_expectation: str | None = None,
    with_unit_evidence: bool = True,
    extra_facet_kind: NeedFacetKind | None = None,
) -> _GapAdapterOutcome:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "stage4-gap"))
    author_ref = artifacts.put(b"author intent", "text/plain", SchemaVersion("1.0.0"))
    checkpoint = PlanningLoopCheckpoint(
        checkpoint_id=StableId("checkpoint.stage4-gap.seed"),
        request_id=StableId("request.stage4-gap"),
        phase=PlanningLoopPhase.INQUIRY_ACCEPTED,
        base_commit=CommitId("sha256:" + "1" * 64),
        snapshot_id=StableId("snapshot.stage4-gap"),
        configuration_fingerprint=_STAGE4_FINGERPRINT,
        inquiry_ref=author_ref,
        inquiry_review_ref=author_ref,
        problem_identity_seed=PlanningProblemIdentitySeed(
            need_id=StableId("need.stage4-gap"),
            question_id=StableId("q.stage4-gap"),
            need_query="陈长生当前经脉状态",
            semantic_question="预注册: 陈长生当前经脉状态",
            facet=NeedFacetKind.RELATION_STATE,
            source_commit=CommitId("sha256:" + "1" * 64),
            source_text_root=ArtifactId("sha256:" + "2" * 64),
            cutoff_chapter=4,
        ),
    )
    checkpoint_ref = artifacts.put(
        canonical_json_bytes(checkpoint.model_dump(mode="json")),
        "application/vnd.novel-agent.planning-loop-checkpoint+json",
        SchemaVersion("1.0.0"),
    )
    evidence = EvidenceRef(
        evidence_id=StableId("evidence.stage4-gap"),
        root_hash=author_ref.artifact_id,
        object_hash=author_ref.artifact_id,
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=CommitId("sha256:" + "1" * 64),
    )
    need_id = StableId("need.stage4-gap")
    facet_id = StableId("facet.stage4-gap")
    candidate_unit = RetrievalUnit(
        unit_id=StableId("anchor.relation.stage4-gap"),
        unit_kind=RetrievalUnitKind.RELATION_ANCHOR,
        source_commit=CommitId("sha256:" + "1" * 64),
        snapshot_id=StableId("snapshot.stage4-gap"),
        text="fixture relation candidate",
        # A Canon extraction handoff requires real cutoff-safe source evidence
        # for the requested proposition, not merely a retrieved candidate.
        narrative_start=4,
        evidence_refs=(evidence,) if with_unit_evidence else (),
    )
    extra_facet_id = StableId("facet.stage4-gap.causal")
    extra_receipts = (
        (
            FacetEvidenceReceipt(
                need_id=need_id,
                need_facet_id=extra_facet_id,
                facet_kind=extra_facet_kind,
                mandatory=True,
                status=FacetClosureStatus.UNSUPPORTED,
                stop_reason="no causal source",
            ),
        )
        if extra_facet_kind is not None
        else ()
    )
    trace = RetrievalTrace(
        need_id=need_id,
        intent=Stage1QueryIntent.RELATION_CHAIN,
        allowed_channels=(RetrievalChannel.R1_EXACT,),
        channel_candidate_counts={RetrievalChannel.R1_EXACT: 0},
        candidates=(
            FusedCandidate(
                unit=candidate_unit,
                fused_rank=1,
                rrf_score=1.0,
                channel_hits=(
                    ChannelHit(
                        unit=candidate_unit,
                        channel=RetrievalChannel.R1_EXACT,
                        channel_rank=1,
                        raw_score=1.0,
                        candidate_count=1,
                        hit_reason="fixture relation candidate",
                    ),
                ),
            ),
        ),
        fusion_applied=False,
        stop_reason=RetrievalStopReason.CANDIDATES_EXHAUSTED,
        need_execution_status=NeedExecutionStatus.EXECUTED_WITH_CANDIDATES,
        required_need_facet_ids=(
            (facet_id, extra_facet_id) if extra_facet_kind is not None else (facet_id,)
        ),
        facet_receipts=(
            FacetEvidenceReceipt(
                need_id=need_id,
                need_facet_id=facet_id,
                facet_kind=NeedFacetKind.RELATION_STATE,
                mandatory=True,
                status=FacetClosureStatus.UNSUPPORTED,
                stop_reason="source did not state the relation",
            ),
            *extra_receipts,
        ),
        question_purpose=question_purpose,
        dependency_expectation=dependency_expectation,
        compiled_query_bundle={
            "semantic_query": "陈长生、国教学院 的当前关系状态是什么? 具体问题: 陈长生当前经脉状态",
            "lexical_queries": ["第4章结束时陈长生经脉状态"],
        },
        l0_fallback_evidence_refs=(evidence,),
    )
    context = Stage1ContextPackage(
        context_id=StableId("context.stage4-gap"),
        base_commit=CommitId("sha256:" + "1" * 64),
        snapshot_id=StableId("snapshot.stage4-gap"),
        task_contract="stage4-gap",
        budget_report=ContextBudgetReport(
            token_budget=100,
            mandatory_tokens=0,
            optional_tokens=0,
            full_chapter_read_count=0,
        ),
        retrieval_traces=(trace,),
    )
    context_ref = artifacts.put(
        canonical_json_bytes(context.model_dump(mode="json")),
        "application/vnd.novel-agent.context-package+json",
        SchemaVersion("1.0.0"),
    )
    assert (
        Stage1ContextPackage.model_validate_json(artifacts.read_verified(context_ref), strict=False)
        == context
    )
    simple = PlanningLoopRequest(
        run_id=RunId("run.stage4-gap"),
        task_id=TaskId("task.stage4-gap"),
        project_id=ProjectId("project.test"),
        basis_commit=CommitId("sha256:" + "1" * 64),
        attempt_id=StableId("attempt.stage4-gap"),
        basis_snapshot=StableId("snapshot.stage4-gap"),
        input_artifact_refs=(author_ref,),
        chapter_index=4,
    )
    task = PlanningTask.model_construct(
        planning_task_id=StableId(simple.task_id.root),
        project_id=simple.project_id,
        mode=AgentMode.CHAPTER,
        base_commit=simple.basis_commit,
        source_ids=(StableId("source.author"),),
    )
    detailed = Stage4PlanningLoopRequest.model_construct(
        request_id=StableId("request.stage4-gap"),
        run_id=simple.run_id,
        task_id=simple.task_id,
        project_id=simple.project_id,
        task=task,
        author_intent_artifacts=(author_ref,),
        accepted_text_ref=author_ref,
        snapshot_id=simple.basis_snapshot,
        budgets=_STAGE4_BUDGETS,
        configuration_fingerprint=_STAGE4_FINGERPRINT,
        model_fingerprint=_STAGE4_FINGERPRINT,
    )
    stage4_result = Stage4PlanningLoopResult.model_construct(
        request_id=detailed.request_id,
        terminal=Stage4PlanningLoopTerminal.REVIEW_REQUIRED,
        memory_context_ref=context_ref,
        event_artifacts=(checkpoint_ref,),
        diagnostic_codes=("PLANNER_MEMORY_FACETS_UNRESOLVED",),
        degraded=False,
    )

    class _Stage4Loop:
        async def run(self, **_: object) -> Stage4PlanningLoopResult:
            return stage4_result

    adapter = Stage4PlanningLeafAdapter(
        cast(PlanningContextLoopService, _Stage4Loop()),
        artifacts,
        lambda _: Stage4PlanningInvocation(
            request=detailed,
            model_request=lambda _phase, _mode, _attempt: cast(ModelRequest, object()),
        ),
        schema_version=SchemaVersion("1.0.0"),
    )

    result = asyncio.run(adapter.run(simple))
    findings = tuple(
        MemoryRepairFinding.model_validate_json(artifacts.read_verified(ref), strict=True)
        for ref in result.artifact_refs
        if ref.media_type == "application/vnd.novel-agent.memory-repair-finding+json"
    )
    return _GapAdapterOutcome(result=result, findings=findings, artifacts=artifacts)


def test_stage4_memory_gap_owner_routes_event_and_state_to_ordinary_curator() -> None:
    assert (
        Stage4PlanningLeafAdapter._memory_gap_owner({NeedFacetKind.RELATION_STATE})
        is MemoryRepairOwner.GRAPH_CURATOR
    )
    assert (
        Stage4PlanningLeafAdapter._memory_gap_owner({NeedFacetKind.CAUSAL_HISTORY})
        is MemoryRepairOwner.ORDINARY_CURATOR
    )
    assert (
        Stage4PlanningLeafAdapter._memory_gap_owner({NeedFacetKind.CURRENT_STATE})
        is MemoryRepairOwner.ORDINARY_CURATOR
    )


def test_stage4_gap_source_selection_keeps_named_state_predicate_chapters() -> None:
    def candidate(predicate: str, chapter: int) -> SimpleNamespace:
        return SimpleNamespace(
            selected=True,
            unit=SimpleNamespace(
                predicate=predicate,
                narrative_start=None,
                evidence_refs=(
                    SimpleNamespace(chapter_id=StableId(f"chapter.ZTJ-P005.{chapter}")),
                ),
            ),
        )

    trace = SimpleNamespace(
        intent=Stage1QueryIntent.CURRENT_STATE,
        compiled_query_bundle={
            "semantic_query": "陈长生的 current_location 和 physical_state 是什么?",
            "lexical_queries": ("current_location physical_state",),
        },
        candidates=(
            candidate("physical_state", 59),
            candidate("current_location", 19),
            candidate("current_location", 21),
            candidate("current_location", 35),
            candidate("current_location", 46),
            candidate("located_at", 9),
        ),
    )

    assert Stage4PlanningLeafAdapter._source_chapter_indices(trace, 95) == (19, 21, 35, 59)

    relation_trace = SimpleNamespace(
        intent=Stage1QueryIntent.RELATION_CHAIN,
        compiled_query_bundle={"semantic_query": "陈长生与黑龙的关系是什么?"},
        candidates=(candidate("located_at", 9), candidate("located_at", 57)),
    )
    assert Stage4PlanningLeafAdapter._source_chapter_indices(relation_trace, 95) == (9,)


def test_stage4_gap_source_selection_routes_cutoff_for_current_question() -> None:
    def candidate(predicate: str, chapter: int) -> SimpleNamespace:
        return SimpleNamespace(
            selected=True,
            unit=SimpleNamespace(
                predicate=predicate,
                narrative_start=None,
                evidence_refs=(
                    SimpleNamespace(chapter_id=StableId(f"chapter.ZTJ-P005.{chapter}")),
                ),
            ),
        )

    trace = SimpleNamespace(
        intent=Stage1QueryIntent.RELATION_CHAIN,
        compiled_query_bundle={
            "semantic_query": ("陈长生 located_at 国教学院 藏书馆 在比试结束后的当前状态是什么?"),
            "lexical_queries": ("陈长生 located_at 国教学院 藏书馆 在比试结束后的当前状态是什么?",),
        },
        candidates=(candidate("located_at", 9), candidate("located_at", 81)),
    )

    # Historical relation anchors remain useful for retrieval, but they are
    # not the source unit that can answer a cutoff/current question.  The
    # immutable C95 TextRoot chapter must be routed to the maintenance owner.
    assert Stage4PlanningLeafAdapter._source_chapter_indices(trace, 95) == (95,)


def test_stage4_gap_source_selection_retains_required_source_chapter() -> None:
    def candidate(chapter: int) -> SimpleNamespace:
        return SimpleNamespace(
            selected=True,
            unit=SimpleNamespace(
                predicate="located_at",
                narrative_start=None,
                evidence_refs=(
                    SimpleNamespace(
                        chapter_id=StableId(f"chapter.ZTJ-P005.{chapter}"),
                    ),
                ),
            ),
        )

    trace = SimpleNamespace(
        intent=Stage1QueryIntent.CAUSAL_MULTI_HOP,
        compiled_query_bundle={
            "semantic_query": "预注册: 天海家派人撞破国教学院院门后，现场发生了哪些直接后果？",  # noqa: RUF001
            "lexical_queries": ("天海家派人撞破国教学院院门后，现场发生了哪些直接后果？",),  # noqa: RUF001
        },
        candidates=(candidate(91), candidate(95)),
    )

    # The causal retrieval ranking may select chapter 91 first.  A seeded
    # source-bound requirement still has to survive into the finding so the
    # maintenance request can validate the registered C95 span.
    assert Stage4PlanningLeafAdapter._source_chapter_indices(trace, 95, required_chapter=95) == (
        91,
        95,
    )


def test_stage4_gap_target_query_preserves_lexical_problem_identity() -> None:
    """Maintenance findings must retain the exact Planner query text."""

    original = "若第96章涉及婚约冲突，是否应优先展现徐有容的主动选择，还是陈长生的被动应对？"  # noqa: RUF001
    compiled = {
        "lexical_queries": (original,),
        "semantic_query": f"预注册: {original}",
    }

    assert Stage4PlanningLeafAdapter._repair_target_query(compiled) == original


def test_fault_writer_refuses_cross_task_injection() -> None:
    result = WritingLoopResult(
        result_id=StableId("writer.failed"),
        run_id=RunId("run.writer"),
        task_id=TaskId("task.writer"),
        status=WritingLoopTerminalStatus.WRITER_FAILED,
        failure_detail="injected",
    )
    leaf = FaultInjectionWritingLeaf(result)
    request = cast(WritingLoopRequest, _Request(result.run_id, result.task_id))
    assert asyncio.run(leaf.run(request)) == result
    with pytest.raises(ValueError, match="must match"):
        asyncio.run(
            leaf.run(cast(WritingLoopRequest, _Request(result.run_id, TaskId("task.other"))))
        )


def test_fault_effect_resolver_is_isolated_only_and_rejects_unresolved_inputs() -> None:
    with pytest.raises(ValueError, match="post-request"):
        FaultInjectionEffectStatusResolver(EffectStatus.REQUESTED)
    resolver = FaultInjectionEffectStatusResolver(EffectStatus.COMPLETED)
    receipt = EffectReceipt(
        effect_identity=StableId("effect.resolve"),
        external_system="provider",
        request_identity=StableId("request.resolve"),
        status=EffectStatus.REQUESTED,
        attempt_no=1,
    )
    resolution = resolver.resolve(receipt)
    assert resolution.receipt.status is EffectStatus.COMPLETED
    with pytest.raises(ValueError, match="unresolved"):
        resolver.resolve(resolution.receipt.model_copy(update={"status": EffectStatus.COMPLETED}))


def test_materializer_rejects_wrong_candidate_kind(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path))
    candidate_ref = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    accepted = AcceptedCandidateBinding(
        acceptance_id=StableId("acceptance.materialize"),
        command_id=StableId("command.materialize"),
        project_id=ProjectId("project.test"),
        run_id=RunId("run.materialize"),
        task_id=TaskId("task.materialize"),
        candidate=CandidateBinding(
            candidate_id=StableId("candidate.draft"),
            kind=CandidateKind.DRAFT,
            artifact_ref=candidate_ref,
            candidate_hash=candidate_ref.artifact_id.root,
            basis_commit=base,
        ),
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        accepted_at=datetime(2026, 8, 10, tzinfo=UTC),
        expected_project_commit=base,
    )
    materializer = StrictDeterministicCandidateMaterializer(
        commits, candidate_kind=CandidateKind.PLAN
    )
    with pytest.raises(ValueError, match="wrong candidate kind"):
        materializer.materialize(accepted)
    engine.dispose()


def test_trusted_materializer_preserves_max_length_identity() -> None:
    identity = "i" * 128

    assert PlanCandidateMaterializer._stable_id("bundle", identity).root == identity
