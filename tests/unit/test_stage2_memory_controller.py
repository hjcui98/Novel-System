from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

from langgraph.checkpoint.memory import InMemorySaver

from novel_agent.agents.controller import StructuredControllerPolicy
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
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.retrieval_routing import (
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentMode,
    AgentType,
    ContextBudget,
    ControllerPolicyAction,
    ControllerPolicyDecision,
    ControllerStopReason,
    MemoryResolutionRequest,
    RequiredSnapshotPolicy,
    ResolutionStatus,
    RetrievalBudget,
    ToolFailureCode,
    ToolPolicy,
    ToolResult,
    ToolResultStatus,
)
from novel_agent.runtime.memory_controller import (
    BoundedMemoryController,
    ControllerStateView,
    RouteBoundControllerPolicy,
)
from novel_agent.services.memory_pipeline import ContextCompiler, EvidenceExpander
from novel_agent.services.retrieval import InMemoryRetrievalBackend, RerankService
from novel_agent.services.retrieval_routing import DeterministicChannelPlanner
from novel_agent.tools import RetrievalToolAdapter, ToolBinding

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.1")
VERSION = SchemaVersion("2.0.0")


def test_controller_policy_decision_normalizes_mutually_exclusive_model_fields() -> None:
    stopped = ControllerPolicyDecision.model_validate_json(
        json.dumps(
            {
                "action": "stop",
                "need_id": "need.model-noise",
                "tool_name": "search_anchor",
                "stop_reason": "sufficient",
                "rationale_code": "MANDATORY_NEEDS_SATISFIED",
                "model_call_id": "untrusted.model-call",
            }
        )
    )
    assert stopped.action is ControllerPolicyAction.STOP
    assert stopped.need_id is None and stopped.tool_name is None
    assert stopped.model_call_id is None

    missing_reason = ControllerPolicyDecision.model_validate_json(
        json.dumps(
            {
                "action": "stop",
                "rationale_code": "MODEL_STOPPED_WITHOUT_REASON",
            }
        )
    )
    assert missing_reason.stop_reason is ControllerStopReason.MANDATORY_GAP_UNRESOLVED

    call = ControllerPolicyDecision.model_validate_json(
        json.dumps(
            {
                "action": "call_tool",
                "need_id": "need.1",
                "tool_name": "search_anchor",
                "stop_reason": "sufficient",
                "rationale_code": "NEXT_REGISTERED_ROUTE",
            }
        )
    )
    assert call.action is ControllerPolicyAction.CALL_TOOL
    assert call.stop_reason is None


def test_structured_controller_receives_current_legal_need_tool_actions() -> None:
    class CapturingRunner:
        payload: dict[str, Any] | None = None

        async def run(self, *args: Any, **kwargs: Any) -> Any:
            self.payload = json.loads(args[4])
            return SimpleNamespace(
                output=ControllerPolicyDecision(
                    action=ControllerPolicyAction.CALL_TOOL,
                    need_id=StableId("need.1"),
                    tool_name="memory.search_exact",
                    rationale_code="TRY_EXACT",
                ),
                model_call=SimpleNamespace(request_id=StableId("model-call.controller")),
                receipt=SimpleNamespace(),
            )

    tool_policy = ToolPolicy(
        policy_id=StableId("policy.structured-controller"),
        version=VERSION,
        content_hash=ArtifactId("sha256:" + "f" * 64),
        allowed_tools=(
            "memory.search_exact",
            "memory.search_temporal",
            "memory.search_anchor_bm25",
        ),
        max_rounds=2,
        max_tool_calls=4,
    )
    runner = CapturingRunner()
    spec = SimpleNamespace(
        agent_type=AgentType.MEMORY_CONTROLLER,
        mode=AgentMode.BOUNDED_R2,
        version=VERSION,
        tool_policy=tool_policy,
    )

    def request_factory(state: ControllerStateView, round_index: int) -> ModelRequest:
        return ModelRequest(
            request_id=StableId(f"request.controller.{round_index}"),
            run_id=state["request"].run_id,
            task_id=state["request"].task_id,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.BATCH_TEST,
            trace_id=f"trace.controller.{round_index}",
            prompt="replaced",
        )

    policy = StructuredControllerPolicy(cast(Any, runner), cast(Any, spec), request_factory)
    decision = policy.decide({"request": request(max_tool_calls=4), "tool_calls": ()})

    assert decision.tool_name == "memory.search_exact"
    assert runner.payload is not None
    assert runner.payload["available_actions"] == [
        {
            "need_id": "need.1",
            "query_intent": "current_state",
            "requirement": "mandatory",
            "tool_names": ["memory.search_exact", "memory.search_temporal"],
        }
    ]


def memory_need(
    *,
    intent: Stage1QueryIntent = Stage1QueryIntent.CURRENT_STATE,
    pools: tuple[CandidatePool, ...] = (CandidatePool.R1,),
) -> Stage1MemoryNeed:
    return Stage1MemoryNeed(
        need_id=StableId("need.1"),
        run_id=RunId("run.1"),
        task_id=TaskId("task.1"),
        base_commit=COMMIT,
        chapter_target=20,
        need_type="current state",
        query_intent=intent,
        query_text="hero injury",
        why_needed="mandatory continuity",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
        allowed_candidate_pools=pools,
        stop_condition="supported state found",
    )


def request(
    *,
    need: Stage1MemoryNeed | None = None,
    max_rounds: int = 3,
    max_tool_calls: int = 12,
    context_tokens: int = 1000,
) -> MemoryResolutionRequest:
    return MemoryResolutionRequest(
        request_id=StableId("request.1"),
        run_id=RunId("run.1"),
        task_id=TaskId("task.1"),
        project_id=ProjectId("project.1"),
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
        task_contract="write chapter 20",
        initial_memory_needs=(need or memory_need(),),
        worldline="main",
        narrative_chapter=20,
        access_scope=AccessScope.WRITER_SAFE,
        retrieval_budget=RetrievalBudget(
            max_rounds=max_rounds,
            max_tool_calls=max_tool_calls,
            max_candidates=20,
        ),
        context_budget=ContextBudget(token_budget=context_tokens),
    )


def controller(
    units: tuple[RetrievalUnit, ...],
    *,
    freshness: bool = True,
    policy: Any | None = None,
    checkpointer: InMemorySaver | None = None,
    reranker: RerankService | None = None,
) -> tuple[BoundedMemoryController, InMemorySaver]:
    backend = InMemoryRetrievalBackend(units)
    adapter = RetrievalToolAdapter(backend, (memory_need(),))
    tool_policy = ToolPolicy(
        policy_id=StableId("policy.controller"),
        version=VERSION,
        content_hash=ArtifactId("sha256:" + "c" * 64),
        allowed_tools=("memory.search_exact", "memory.search_temporal"),
        max_rounds=3,
        max_tool_calls=12,
    )
    saver = checkpointer or InMemorySaver()
    return (
        BoundedMemoryController(
            ToolBinding(tool_policy, adapter.handlers()),
            tool_policy,
            ContextCompiler(EvidenceExpander()),
            policy or RouteBoundControllerPolicy(),
            lambda _: freshness,
            saver,
            reranker=reranker,
        ),
        saver,
    )


def text_root() -> TextRootDocument:
    return TextRootDocument(
        root_hash=ArtifactId("sha256:" + "d" * 64),
        schema_version=VERSION,
        chapters=(),
    )


def test_bounded_controller_resolves_with_typed_tools_and_freezes_context() -> None:
    unit = RetrievalUnit(
        unit_id=StableId("anchor.hero.injury"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        text="hero remains injured",
        mandatory=True,
    )
    runtime, saver = controller((unit,))
    result = runtime.resolve(request(), text_root(), thread_id="controller-success")

    assert result["resolution"].status is ResolutionStatus.READY
    assert result["resolution"].stop_reason is ControllerStopReason.SUFFICIENT
    assert result["resolution"].memory_selection[0].unit_id == unit.unit_id
    assert result["context"].mandatory_constraints == (unit,)
    assert len(result["tool_results"]) == 1
    snapshot = runtime.graph.get_state({"configurable": {"thread_id": "controller-success"}})
    assert snapshot.values["stopped"] is True
    assert saver is not None


def test_bounded_controller_blocks_stale_snapshot_before_any_tool_call() -> None:
    runtime, _ = controller((), freshness=False)
    result = runtime.resolve(request(), text_root(), thread_id="controller-stale")

    assert result["resolution"].status is ResolutionStatus.PARTIAL
    assert result["resolution"].stop_reason is ControllerStopReason.FRESHNESS_BLOCKED
    assert result["tool_results"] == ()
    assert result["context"].unresolved_gaps == ("hero injury",)


def test_bounded_controller_distinguishes_budget_and_exhausted_evidence() -> None:
    semantic_need = memory_need(
        intent=Stage1QueryIntent.SEMANTIC_HISTORY,
        pools=(CandidatePool.ANCHOR,),
    )
    backend = InMemoryRetrievalBackend(())
    adapter = RetrievalToolAdapter(backend, (semantic_need,))
    tool_policy = ToolPolicy(
        policy_id=StableId("policy.empty"),
        version=VERSION,
        content_hash=ArtifactId("sha256:" + "e" * 64),
        allowed_tools=("memory.search_anchor_bm25", "memory.search_anchor_dense"),
        max_rounds=3,
        max_tool_calls=12,
    )
    runtime = BoundedMemoryController(
        ToolBinding(tool_policy, adapter.handlers()),
        tool_policy,
        ContextCompiler(EvidenceExpander()),
        RouteBoundControllerPolicy(),
        lambda _: True,
        InMemorySaver(),
    )
    exhausted = runtime.resolve(
        request(need=semantic_need), text_root(), thread_id="controller-empty"
    )
    assert exhausted["resolution"].stop_reason is ControllerStopReason.MANDATORY_GAP_UNRESOLVED
    budgeted = runtime.resolve(
        request(need=semantic_need, max_rounds=1),
        text_root(),
        thread_id="controller-budget",
    )
    assert budgeted["resolution"].stop_reason is ControllerStopReason.BUDGET_EXHAUSTED


def test_bounded_controller_rejects_budgets_above_registered_policy() -> None:
    runtime, _ = controller(())
    too_many_calls = request(max_tool_calls=13)
    try:
        runtime.resolve(too_many_calls, text_root(), thread_id="too-many-calls")
    except ValueError as error:
        assert "tool budget" in str(error)
    else:
        raise AssertionError("tool budget above policy was accepted")
    too_many_rounds = request(max_rounds=4)
    try:
        runtime.resolve(too_many_rounds, text_root(), thread_id="too-many-rounds")
    except ValueError as error:
        assert "round budget" in str(error)
    else:
        raise AssertionError("round budget above policy was accepted")


class ForbiddenPolicy:
    def decide(self, state: ControllerStateView) -> tuple[StableId, str] | ControllerStopReason:
        return StableId("need.1"), "memory.commit"


class FalseSufficientPolicy:
    def decide(self, state: ControllerStateView) -> tuple[StableId, str] | ControllerStopReason:
        return ControllerStopReason.SUFFICIENT


def test_controller_blocks_unknown_tools_and_false_sufficient_claims() -> None:
    forbidden, _ = controller((), policy=ForbiddenPolicy())
    denied = forbidden.resolve(request(), text_root(), thread_id="controller-denied")
    assert denied["resolution"].stop_reason is ControllerStopReason.ACCESS_BLOCKED

    false_sufficient, _ = controller((), policy=FalseSufficientPolicy())
    corrected = false_sufficient.resolve(
        request(), text_root(), thread_id="controller-false-sufficient"
    )
    assert corrected["resolution"].stop_reason is ControllerStopReason.MANDATORY_GAP_UNRESOLVED


def test_controller_trace_builder_ignores_failed_or_malformed_tool_payloads() -> None:
    runtime, _ = controller(())
    failed = ToolResult(
        tool_call_id=StableId("call.failed"),
        status=ToolResultStatus.FAILED,
        basis_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        failure_code=ToolFailureCode.TIMEOUT,
        audit_ref=StableId("audit.failed"),
    )
    malformed = ToolResult(
        tool_call_id=StableId("call.malformed"),
        status=ToolResultStatus.SUCCEEDED,
        basis_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        payload={"hits": "not-a-list"},
        audit_ref=StableId("audit.malformed"),
    )
    trace = runtime._build_traces(
        (memory_need(),),
        (
            (StableId("need.1"), "memory.search_exact", failed),
            (StableId("need.1"), "memory.search_temporal", malformed),
        ),
    )[0]

    assert trace.candidates == ()


class _ReverseReranker:
    profile = "locked-reranker-test"

    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        return tuple(float(index) for index, _ in enumerate(passages))


class _UnavailableReranker:
    profile = "unavailable-reranker-test"

    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        raise RuntimeError("reranker unavailable")


def test_agentic_route_plan_applies_one_rerank_after_declared_rrf_group() -> None:
    semantic_need = memory_need(
        intent=Stage1QueryIntent.RELATED_EVENT,
        pools=(CandidatePool.ANCHOR,),
    )
    capability = SnapshotCapability(
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=(
            RetrievalChannel.ANCHOR_BM25,
            RetrievalChannel.ANCHOR_DENSE,
        ),
    )
    plan = DeterministicChannelPlanner().plan(semantic_need, capability)
    first = RetrievalUnit(
        unit_id=StableId("anchor.first"),
        unit_kind=RetrievalUnitKind.EVENT_ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        text="first passage",
    )
    second = first.model_copy(
        update={"unit_id": StableId("anchor.second"), "text": "second passage"}
    )
    channel_results = {
        channel: tuple(
            ChannelHit(
                unit=item,
                channel=channel,
                channel_rank=rank,
                raw_score=float(3 - rank),
                candidate_count=2,
                hit_reason="test",
            )
            for rank, item in enumerate((first, second), start=1)
        )
        for channel in (
            RetrievalChannel.ANCHOR_BM25,
            RetrievalChannel.ANCHOR_DENSE,
        )
    }
    runtime, _ = controller((), reranker=RerankService(_ReverseReranker()))

    candidates, fusion_applied, rerank_applied, rerank_failure = runtime._route_candidates(
        semantic_need,
        plan,
        channel_results,
        candidate_limit=2,
    )

    assert fusion_applied is True
    assert rerank_applied is True
    assert rerank_failure is None
    assert candidates[0].unit.unit_id == second.unit_id
    assert (
        sum(
            hit.channel is RetrievalChannel.RERANK
            for candidate in candidates
            for hit in candidate.channel_hits
        )
        == 2
    )
    assert runtime._pool_for_unit("grounded_block") is CandidatePool.GROUNDED
    assert runtime._pool_for_unit("unknown") is CandidatePool.R1

    degraded_runtime, _ = controller((), reranker=RerankService(_UnavailableReranker()))
    degraded, _, degraded_applied, degraded_failure = degraded_runtime._route_candidates(
        semantic_need,
        plan,
        channel_results,
        candidate_limit=2,
    )
    assert degraded[0].unit.unit_id == first.unit_id
    assert degraded_applied is False
    assert degraded_failure == "reranker_degraded:RuntimeError"


def test_controller_never_silently_drops_mandatory_context_on_overflow() -> None:
    unit = RetrievalUnit(
        unit_id=StableId("anchor.large.mandatory"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        text="hero injury mandatory continuity " * 20,
        mandatory=True,
    )
    runtime, _ = controller((unit,))
    result = runtime.resolve(
        request(context_tokens=1), text_root(), thread_id="controller-overflow"
    )

    assert result["resolution"].status is ResolutionStatus.PARTIAL
    assert result["resolution"].stop_reason is ControllerStopReason.BUDGET_EXHAUSTED
    assert "mandatory context exceeds token budget" in result["resolution"].unresolved_gaps
    assert result["context"].mandatory_constraints == (unit,)
