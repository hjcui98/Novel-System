from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from novel_agent.domain.benchmark import TextRootDocument
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
    CandidatePool,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    ContextBudget,
    ControllerArm,
    ControllerStopReason,
    MemoryResolutionRequest,
    PairedContextComparison,
    RequiredSnapshotPolicy,
    RetrievalBudget,
    ToolPolicy,
)
from novel_agent.runtime.memory_controller import RouteBoundControllerPolicy
from novel_agent.services.memory_pipeline import ContextCompiler, EvidenceExpander
from novel_agent.services.paired_controller import PairedMemoryControllerRunner
from novel_agent.services.retrieval import InMemoryRetrievalBackend

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.paired")
CONFIG = ArtifactId("sha256:" + "c" * 64)
PRIVATE = ArtifactId("sha256:" + "f" * 64)
VERSION = SchemaVersion("2.0.0")


def need(
    *,
    intent: Stage1QueryIntent = Stage1QueryIntent.CURRENT_STATE,
) -> Stage1MemoryNeed:
    return Stage1MemoryNeed(
        need_id=StableId("need.paired"),
        run_id=RunId("run.paired"),
        task_id=TaskId("task.paired"),
        base_commit=COMMIT,
        chapter_target=20,
        need_type="continuity",
        query_intent=intent,
        query_text="hero injury",
        why_needed="paired controller comparison",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
        allowed_candidate_pools=(CandidatePool.R1,),
        stop_condition="current state found",
    )


def request(
    item: Stage1MemoryNeed,
    *,
    max_calls: int = 12,
    allow_future_plan: bool = False,
) -> MemoryResolutionRequest:
    return MemoryResolutionRequest(
        request_id=StableId("request.paired"),
        run_id=item.run_id,
        task_id=item.task_id,
        project_id=ProjectId("project.paired"),
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
        task_contract="prepare chapter 20",
        initial_memory_needs=(item,),
        worldline="main",
        narrative_chapter=20,
        access_scope=(AccessScope.EVALUATOR if allow_future_plan else AccessScope.WRITER_SAFE),
        allow_future_plan=allow_future_plan,
        retrieval_budget=RetrievalBudget(
            max_rounds=3,
            max_tool_calls=max_calls,
            max_candidates=20,
        ),
        context_budget=ContextBudget(token_budget=1000),
    )


def text_root() -> TextRootDocument:
    return TextRootDocument(root_hash=CONFIG, schema_version=VERSION, chapters=())


def runner(
    item: Stage1MemoryNeed,
    unit: RetrievalUnit,
    *,
    fresh: bool = True,
) -> PairedMemoryControllerRunner:
    backend = InMemoryRetrievalBackend((unit,))
    policy = ToolPolicy(
        policy_id=StableId("policy.paired"),
        version=VERSION,
        content_hash=CONFIG,
        allowed_tools=("memory.search_exact", "memory.search_temporal"),
        max_rounds=3,
        max_tool_calls=12,
    )
    return PairedMemoryControllerRunner.from_shared_backend(
        backend=backend,
        needs=(item,),
        tool_policy=policy,
        compiler=ContextCompiler(EvidenceExpander()),
        controller_policy=RouteBoundControllerPolicy(),
        freshness_check=lambda _: fresh,
        checkpointer=InMemorySaver(),
        comparison_basis_fingerprint=CONFIG,
    )


def unit(*, source_artifact: ArtifactId = CONFIG) -> RetrievalUnit:
    return RetrievalUnit(
        unit_id=StableId("anchor.hero.injury"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        source_artifact=source_artifact,
        text="hero injury remains",
        mandatory=True,
    )


def test_paired_runner_uses_shared_basis_and_budgets_for_both_arms() -> None:
    item = need()
    result = runner(item, unit()).run(request(item), text_root(), thread_id="paired-success")

    assert result.comparable is True
    assert result.blockers == ()
    assert result.deterministic.arm is ControllerArm.DETERMINISTIC
    assert result.agentic.arm is ControllerArm.BOUNDED_R2
    assert result.deterministic.context.base_commit == result.agentic.context.base_commit
    assert result.deterministic.selected_unit_ids == result.agentic.selected_unit_ids
    assert result.deterministic.retrieval_call_count == 2
    assert result.agentic.retrieval_call_count == 1
    assert result.agentic.stop_reason is ControllerStopReason.SUFFICIENT


def test_paired_runner_enforces_budget_and_freshness_without_hidden_backend_calls() -> None:
    item = need()
    budgeted = runner(item, unit()).run(
        request(item, max_calls=1), text_root(), thread_id="paired-budget"
    )
    assert budgeted.deterministic.retrieval_call_count == 1
    assert budgeted.deterministic.stop_reason is ControllerStopReason.BUDGET_EXHAUSTED
    stale = runner(item, unit(), fresh=False).run(
        request(item), text_root(), thread_id="paired-stale"
    )
    assert stale.deterministic.retrieval_call_count == 0
    assert stale.agentic.retrieval_call_count == 0
    assert stale.deterministic.stop_reason is ControllerStopReason.FRESHNESS_BLOCKED
    assert stale.agentic.stop_reason is ControllerStopReason.FRESHNESS_BLOCKED


def test_paired_runner_applies_plan_permission_to_deterministic_and_agentic_arms() -> None:
    item = need(intent=Stage1QueryIntent.PLAN_NODE)
    result = runner(item, unit()).run(request(item), text_root(), thread_id="paired-plan-block")
    assert result.deterministic.selected_unit_ids == ()
    assert result.agentic.selected_unit_ids == ()
    assert result.deterministic.stop_reason is ControllerStopReason.MANDATORY_GAP_UNRESOLVED


def test_paired_runner_marks_any_evaluator_artifact_leak_as_non_comparable() -> None:
    item = need()
    result = runner(item, unit(source_artifact=PRIVATE)).run(
        request(item),
        text_root(),
        thread_id="paired-leak",
        evaluator_only_artifacts=(PRIVATE,),
    )
    assert result.comparable is False
    assert result.deterministic.future_leakage_count == 1
    assert result.agentic.future_leakage_count == 1
    assert len(result.blockers) == 2
    trace = result.agentic.context.retrieval_traces[0]
    rejected = trace.candidates[0].model_copy(
        update={"selected": False, "rejection_reason": "test rejection"}
    )
    rejected_context = result.agentic.context.model_copy(
        update={"retrieval_traces": (trace.model_copy(update={"candidates": (rejected,)}),)}
    )
    assert PairedMemoryControllerRunner._leakage_count(rejected_context, (PRIVATE,)) == 0


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("deterministic_arm", "wrong controller"),
        ("agentic_arm", "wrong controller"),
        ("basis", "canonical and snapshot"),
        ("budget", "token budget"),
        ("fingerprint", "comparison configuration"),
        ("comparable", "contradicts"),
    ),
)
def test_paired_comparison_contract_rejects_unfair_or_contradictory_results(
    mutation: str,
    message: str,
) -> None:
    item = need()
    result = runner(item, unit()).run(
        request(item), text_root(), thread_id=f"paired-invalid-{mutation}"
    )
    payload = result.model_dump()
    if mutation == "deterministic_arm":
        payload["deterministic"]["arm"] = ControllerArm.BOUNDED_R2
    elif mutation == "agentic_arm":
        payload["agentic"]["arm"] = ControllerArm.DETERMINISTIC
    elif mutation == "basis":
        payload["agentic"]["context"]["snapshot_id"] = StableId("snapshot.other")
    elif mutation == "budget":
        payload["agentic"]["context"]["budget_report"]["token_budget"] = 999
    elif mutation == "fingerprint":
        payload["agentic"]["comparison_basis_fingerprint"] = PRIVATE
    else:
        payload["comparable"] = False
    with pytest.raises(ValidationError, match=message):
        PairedContextComparison.model_validate(payload)
