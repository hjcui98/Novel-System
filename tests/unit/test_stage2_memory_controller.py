from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
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
    ConditionalFallback,
    RouteStep,
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
    ControllerPolicyDraft,
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
    TOOL_BY_CHANNEL,
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


def structured_controller_spec() -> SimpleNamespace:
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
    return SimpleNamespace(
        agent_id=StableId("agent.controller"),
        agent_type=AgentType.MEMORY_CONTROLLER,
        mode=AgentMode.BOUNDED_R2,
        version=VERSION,
        content_hash=ArtifactId("sha256:" + "e" * 64),
        system_prompt=SimpleNamespace(render_fingerprint=ArtifactId("sha256:" + "d" * 64)),
        tool_policy=tool_policy,
    )


def controller_request_factory(state: ControllerStateView, round_index: int) -> ModelRequest:
    return ModelRequest(
        request_id=StableId(f"request.controller.{round_index}"),
        run_id=state["request"].run_id,
        task_id=state["request"].task_id,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id=f"trace.controller.{round_index}",
        prompt="replaced",
    )


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
                output=ControllerPolicyDraft(
                    action="call_tool",
                    need_id="need.1",
                    tool_name="memory.search_exact",
                    rationale_code="TRY_EXACT",
                ),
                model_call=SimpleNamespace(request_id=StableId("model-call.controller")),
                receipt=SimpleNamespace(),
            )

    spec = structured_controller_spec()
    tool_policy = spec.tool_policy
    runner = CapturingRunner()

    policy = StructuredControllerPolicy(
        cast(Any, runner),
        cast(Any, spec),
        controller_request_factory,
    )
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
    assert policy.contract_ref.contract_id == spec.agent_id
    assert policy.prompt_fingerprint == spec.system_prompt.render_fingerprint
    assert policy.tool_policy_hash == tool_policy.content_hash
    assert len(policy.decision_receipts) == 1
    assert policy.decision_receipt(StableId("model-call.controller")) is not None
    assert policy.decision_receipt(StableId("model-call.missing")) is None

    unavailable_spec = SimpleNamespace(**vars(spec))
    unavailable_spec.tool_policy = tool_policy.model_copy(
        update={"allowed_tools": ("memory.unknown",)}
    )
    unavailable = StructuredControllerPolicy(
        cast(Any, runner),
        cast(Any, unavailable_spec),
        controller_request_factory,
    )
    assert (
        unavailable._available_actions({"request": request(max_tool_calls=4), "tool_calls": ()})
        == []
    )


def test_structured_controller_repairs_missing_call_fields_from_sealed_actions() -> None:
    class DraftRunner:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            assert args[5] is ControllerPolicyDraft
            return SimpleNamespace(
                output=ControllerPolicyDraft(
                    action="call_tool",
                    rationale_code="TRY_ROUTE",
                    model_call_id="untrusted.call",
                ),
                model_call=SimpleNamespace(request_id=StableId("model-call.repaired")),
                receipt=SimpleNamespace(),
            )

    policy = StructuredControllerPolicy(
        cast(Any, DraftRunner()),
        cast(Any, structured_controller_spec()),
        controller_request_factory,
    )
    decision = policy.decide({"request": request(max_tool_calls=4), "tool_calls": ()})

    assert decision.action is ControllerPolicyAction.CALL_TOOL
    assert decision.need_id == StableId("need.1")
    assert decision.tool_name == "memory.search_exact"
    assert decision.rationale_code == "BOUND_FIRST_LEGAL_ACTION"
    assert decision.model_call_id == StableId("model-call.repaired")
    assert len(policy.decision_repairs) == 1
    assert policy.decision_repairs[0].reason == "BOUND_FIRST_LEGAL_ACTION"
    assert policy.decision_repairs[0].request_id == StableId("model-call.repaired")


def test_structured_controller_repairs_missing_action_without_legal_route() -> None:
    decision, repair = StructuredControllerPolicy._bind_draft(
        ControllerPolicyDraft(),
        [],
    )
    assert repair == "MISSING_OR_UNKNOWN_ACTION"
    assert decision.action is ControllerPolicyAction.STOP
    assert decision.stop_reason is ControllerStopReason.MANDATORY_GAP_UNRESOLVED
    assert decision.rationale_code == "NO_LEGAL_ACTION_AVAILABLE"


@pytest.mark.parametrize(
    ("draft", "expected_action", "expected_reason", "expected_rationale"),
    (
        (
            ControllerPolicyDraft(action="stop"),
            ControllerPolicyAction.STOP,
            None,
            "MODEL_STOP",
        ),
        (
            ControllerPolicyDraft(
                action="stop",
                stop_reason="budget_exhausted",
                rationale_code="MODEL_BUDGET_STOP",
            ),
            ControllerPolicyAction.STOP,
            None,
            "MODEL_BUDGET_STOP",
        ),
        (
            ControllerPolicyDraft(action="stop", stop_reason="not-a-reason"),
            ControllerPolicyAction.STOP,
            None,
            "MODEL_STOP",
        ),
        (
            ControllerPolicyDraft(
                action="call_tool",
                need_id="need.1",
                tool_name="memory.search_exact",
            ),
            ControllerPolicyAction.CALL_TOOL,
            None,
            "MODEL_LEGAL_ACTION",
        ),
        (
            ControllerPolicyDraft(
                action="call_tool",
                tool_name="memory.search_temporal",
            ),
            ControllerPolicyAction.CALL_TOOL,
            "INFERRED_UNIQUE_LEGAL_ACTION",
            "BOUND_UNIQUE_LEGAL_ACTION",
        ),
    ),
)
def test_structured_controller_binds_model_draft_cases(
    draft: ControllerPolicyDraft,
    expected_action: ControllerPolicyAction,
    expected_reason: str | None,
    expected_rationale: str,
) -> None:
    actions: list[dict[str, object]] = [
        {
            "need_id": "need.1",
            "tool_names": ["memory.search_exact", "memory.search_temporal"],
        }
    ]
    decision, repair = StructuredControllerPolicy._bind_draft(draft, actions)
    assert decision.action is expected_action
    assert repair == expected_reason
    assert decision.rationale_code == expected_rationale


def test_structured_controller_survives_exhausted_schema_retries() -> None:
    class InvalidRunner:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            ControllerPolicyDecision.model_validate(
                {
                    "action": "call_tool",
                    "rationale_code": "MISSING_FIELDS",
                }
            )
            raise AssertionError("invalid decision unexpectedly passed")

    policy = StructuredControllerPolicy(
        cast(Any, InvalidRunner()),
        cast(Any, structured_controller_spec()),
        controller_request_factory,
    )
    decision = policy.decide({"request": request(max_tool_calls=4), "tool_calls": ()})
    assert decision.action is ControllerPolicyAction.CALL_TOOL
    assert decision.need_id == StableId("need.1")
    assert decision.tool_name == "memory.search_exact"
    assert decision.rationale_code == "SCHEMA_RETRY_EXHAUSTED"
    assert policy.decision_receipts == ()
    assert policy.decision_repairs[0].reason == "SCHEMA_RETRY_EXHAUSTED"
    assert policy.decision_repairs[0].request_id == StableId("request.controller.1")


def test_structured_controller_rejects_corrupt_trusted_action_shape() -> None:
    with pytest.raises(AssertionError, match="invalid shape"):
        StructuredControllerPolicy._legal_pairs(
            [{"need_id": 1, "tool_names": ["memory.search_exact"]}]
        )
    with pytest.raises(AssertionError, match="tool name"):
        StructuredControllerPolicy._legal_pairs([{"need_id": "need.1", "tool_names": [1]}])


def test_structured_controller_rejects_wrong_agent_type_or_mode() -> None:
    runner = cast(Any, object())
    factory = cast(Any, lambda *_: None)
    wrong_type = SimpleNamespace(
        agent_type=AgentType.PLANNER,
        mode=AgentMode.BOUNDED_R2,
    )
    with pytest.raises(ValueError, match="Memory Controller AgentSpec"):
        StructuredControllerPolicy(runner, cast(Any, wrong_type), factory)

    wrong_mode = SimpleNamespace(
        agent_type=AgentType.MEMORY_CONTROLLER,
        mode=AgentMode.CHAPTER,
    )
    with pytest.raises(ValueError, match="BOUNDED_R2"):
        StructuredControllerPolicy(runner, cast(Any, wrong_mode), factory)


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


def _empty_result(call_id: str, *, succeeded: bool = False) -> ToolResult:
    return ToolResult(
        tool_call_id=StableId(call_id),
        status=ToolResultStatus.SUCCEEDED if succeeded else ToolResultStatus.FAILED,
        basis_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        payload={"hits": []} if succeeded else None,
        failure_code=None if succeeded else ToolFailureCode.TIMEOUT,
        audit_ref=StableId(f"audit.{call_id}"),
    )


def test_registered_route_policy_covers_budget_missing_plan_and_fallback_exhaustion() -> None:
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
    with pytest.raises(ValueError, match="unique memory need ids"):
        RouteBoundControllerPolicy((plan, plan))

    policy = RouteBoundControllerPolicy((plan,))
    first = policy.decide({"request": request(need=semantic_need), "tool_calls": ()})
    assert first.action is ControllerPolicyAction.CALL_TOOL
    assert policy.tool_policy_hash is None
    assert policy.decision_receipt(StableId("model-call.none")) is None

    budget_request = request(need=semantic_need, max_rounds=1, max_tool_calls=1)
    budgeted = policy.decide(
        {
            "request": budget_request,
            "tool_calls": (
                (
                    semantic_need.need_id,
                    cast(str, first.tool_name),
                    _empty_result("call.budget"),
                ),
            ),
        }
    )
    assert budgeted.stop_reason is ControllerStopReason.BUDGET_EXHAUSTED

    wrong_plan = plan.model_copy(update={"need_id": StableId("need.other")})
    with pytest.raises(ValueError, match="no plan for a mandatory"):
        RouteBoundControllerPolicy((wrong_plan,)).decide(
            {"request": request(need=semantic_need), "tool_calls": ()}
        )

    all_steps = (
        *plan.mandatory_steps,
        *(step for group in plan.primary_groups for step in group.steps),
        *(step for fallback in plan.conditional_fallbacks for step in fallback.steps),
    )
    exhausted_calls = tuple(
        (
            semantic_need.need_id,
            TOOL_BY_CHANNEL[step.channel],
            _empty_result(f"call.{index}"),
        )
        for index, step in enumerate(all_steps)
    )
    exhausted = policy.decide(
        {
            "request": request(need=semantic_need),
            "tool_calls": exhausted_calls,
        }
    )
    assert exhausted.stop_reason in {
        ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
        ControllerStopReason.MANDATORY_GAP_UNRESOLVED,
    }
    optional_need = semantic_need.model_copy(update={"requirement": RequirementLevel.OPTIONAL})
    optional_plan = plan.model_copy(update={"need_id": optional_need.need_id})
    optional = RouteBoundControllerPolicy((optional_plan,)).decide(
        {"request": request(need=optional_need), "tool_calls": ()}
    )
    assert optional.stop_reason is ControllerStopReason.SUFFICIENT

    successful_calls = tuple(
        (
            need_id,
            tool_name,
            result.model_copy(
                update={
                    "status": ToolResultStatus.SUCCEEDED,
                    "failure_code": None,
                    "payload": {"hits": []},
                    "coverage": 1.0,
                }
            )
            if index == 0
            else result,
        )
        for index, (need_id, tool_name, result) in enumerate(exhausted_calls)
    )
    succeeded = policy.decide(
        {
            "request": request(need=semantic_need),
            "tool_calls": successful_calls,
        }
    )
    assert succeeded.stop_reason is ControllerStopReason.SUFFICIENT
    assert policy._channels_for_need(semantic_need)
    with pytest.raises(ValueError, match="unregistered route fallback"):
        policy._fallback_applies("unknown-condition", False)
    assert policy._fallback_applies("anchor_evidence_insufficient", False)
    assert policy._fallback_applies("hierarchy_scope_resolved", True)


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


def test_bounded_controller_rejects_policy_hash_and_duplicate_route_plans() -> None:
    class WrongHashPolicy(RouteBoundControllerPolicy):
        @property
        def tool_policy_hash(self) -> ArtifactId:
            return ArtifactId("sha256:" + "0" * 64)

    with pytest.raises(ValueError, match="fingerprints differ"):
        controller((), policy=WrongHashPolicy())

    item = memory_need(intent=Stage1QueryIntent.RELATED_EVENT)
    capability = SnapshotCapability(
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=(RetrievalChannel.ANCHOR_BM25,),
    )
    plan = DeterministicChannelPlanner().plan(item, capability)
    backend = InMemoryRetrievalBackend(())
    adapter = RetrievalToolAdapter(backend, (item,))
    policy = ToolPolicy(
        policy_id=StableId("policy.duplicate-routes"),
        version=VERSION,
        content_hash=ArtifactId("sha256:" + "1" * 64),
        allowed_tools=("memory.search_anchor_bm25",),
        max_rounds=3,
        max_tool_calls=12,
    )
    with pytest.raises(ValueError, match="route plans must have unique"):
        BoundedMemoryController(
            ToolBinding(policy, adapter.handlers()),
            policy,
            ContextCompiler(EvidenceExpander()),
            RouteBoundControllerPolicy(),
            lambda _: True,
            InMemorySaver(),
            route_plans=(plan, plan),
        )


class ForbiddenPolicy:
    def decide(self, state: ControllerStateView) -> tuple[StableId, str] | ControllerStopReason:
        return StableId("need.1"), "memory.commit"


class FalseSufficientPolicy:
    def decide(self, state: ControllerStateView) -> tuple[StableId, str] | ControllerStopReason:
        return ControllerStopReason.SUFFICIENT


class UnknownNeedPolicy:
    def decide(self, state: ControllerStateView) -> tuple[StableId, str]:
        return StableId("need.unknown"), "memory.search_exact"


class RepeatingPolicy:
    def decide(self, state: ControllerStateView) -> tuple[StableId, str]:
        return StableId("need.1"), "memory.search_exact"


class ReceiptPolicy:
    tool_policy_hash = None

    def decide(self, state: ControllerStateView) -> ControllerPolicyDecision:
        return ControllerPolicyDecision(
            action=ControllerPolicyAction.STOP,
            stop_reason=ControllerStopReason.SUFFICIENT,
            rationale_code="MODEL_STOP",
        ).model_copy(update={"model_call_id": StableId("model-call.receipted")})

    def decision_receipt(self, model_call_id: StableId) -> Any | None:
        return SimpleNamespace(receipt_id=StableId(f"receipt.{model_call_id.root}"))


class NullReceiptPolicy(ReceiptPolicy):
    def decision_receipt(self, model_call_id: StableId) -> None:
        return None


def test_controller_blocks_unknown_tools_and_false_sufficient_claims() -> None:
    forbidden, _ = controller((), policy=ForbiddenPolicy())
    denied = forbidden.resolve(request(), text_root(), thread_id="controller-denied")
    assert denied["resolution"].stop_reason is ControllerStopReason.ACCESS_BLOCKED

    false_sufficient, _ = controller((), policy=FalseSufficientPolicy())
    corrected = false_sufficient.resolve(
        request(), text_root(), thread_id="controller-false-sufficient"
    )
    assert corrected["resolution"].stop_reason is ControllerStopReason.MANDATORY_GAP_UNRESOLVED

    unknown_need, _ = controller((), policy=UnknownNeedPolicy())
    unknown = unknown_need.resolve(request(), text_root(), thread_id="controller-unknown-need")
    assert unknown["resolution"].stop_reason is ControllerStopReason.ACCESS_BLOCKED

    repeating, _ = controller((), policy=RepeatingPolicy())
    repeated = repeating.resolve(request(), text_root(), thread_id="controller-repeated-call")
    assert repeated["resolution"].stop_reason is ControllerStopReason.NO_ADDITIONAL_EVIDENCE

    receipted, _ = controller((), policy=ReceiptPolicy())
    receipt_result = receipted.resolve(
        request(), text_root(), thread_id="controller-decision-receipt"
    )
    assert len(receipt_result["decision_receipts"]) == 1
    null_receipt, _ = controller((), policy=NullReceiptPolicy())
    assert (
        null_receipt.resolve(request(), text_root(), thread_id="controller-null-decision-receipt")[
            "decision_receipts"
        ]
        == ()
    )


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
    assert runtime._new_information_gain([], failed) == 0
    assert runtime._result_unit_ids(failed) == ()
    assert runtime._result_unit_ids(malformed) == ()
    malformed_list = malformed.model_copy(update={"payload": {"hits": ["bad", {}]}})
    assert runtime._result_unit_ids(malformed_list) == ()


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

    no_reranker, _ = controller(())
    direct, fused, reranked, failure = no_reranker._route_candidates(
        semantic_need,
        plan,
        channel_results,
        candidate_limit=2,
    )
    assert direct and fused and not reranked and failure is None

    fallback_step = RouteStep(
        step_id=StableId("route-step.fallback"),
        channel=RetrievalChannel.GROUNDED_BM25,
        candidate_pool=CandidatePool.GROUNDED,
        query_template="fallback",
    )
    fallback_plan = plan.model_copy(
        update={
            "mandatory_steps": (plan.primary_groups[0].steps[0],),
            "primary_groups": (),
            "conditional_fallbacks": (
                ConditionalFallback(
                    fallback_id=StableId("fallback.one"),
                    condition="anchor_evidence_insufficient",
                    steps=(fallback_step,),
                ),
            ),
        }
    )
    primary_tool = TOOL_BY_CHANNEL[fallback_plan.mandatory_steps[0].channel]
    assert (
        RouteBoundControllerPolicy._next_registered_tool(
            fallback_plan,
            (
                (
                    semantic_need.need_id,
                    primary_tool,
                    _empty_result("call.primary-empty"),
                ),
            ),
        )
        == TOOL_BY_CHANNEL[RetrievalChannel.GROUNDED_BM25]
    )
    primary_success = _empty_result("call.primary-success", succeeded=True).model_copy(
        update={"coverage": 1.0}
    )
    assert (
        RouteBoundControllerPolicy._next_registered_tool(
            fallback_plan,
            ((semantic_need.need_id, primary_tool, primary_success),),
        )
        is None
    )
    fallback_tool = TOOL_BY_CHANNEL[RetrievalChannel.GROUNDED_BM25]
    assert (
        RouteBoundControllerPolicy._next_registered_tool(
            fallback_plan,
            (
                (
                    semantic_need.need_id,
                    primary_tool,
                    _empty_result("call.primary-failed"),
                ),
                (
                    semantic_need.need_id,
                    fallback_tool,
                    _empty_result("call.fallback-failed"),
                ),
            ),
        )
        is None
    )
    fallback_hit = ChannelHit(
        unit=first,
        channel=RetrievalChannel.GROUNDED_BM25,
        channel_rank=1,
        raw_score=1.0,
        candidate_count=1,
        hit_reason="fallback",
    )
    remaining_hit = fallback_hit.model_copy(update={"channel": RetrievalChannel.R1_EXACT})
    staged, _, _, _ = no_reranker._route_candidates(
        semantic_need,
        fallback_plan,
        {
            RetrievalChannel.ANCHOR_BM25: channel_results[RetrievalChannel.ANCHOR_BM25],
            RetrievalChannel.GROUNDED_BM25: (fallback_hit,),
            RetrievalChannel.R1_EXACT: (remaining_hit,),
        },
        candidate_limit=2,
    )
    assert staged
    remaining_only, _, _, _ = no_reranker._route_candidates(
        semantic_need,
        fallback_plan,
        {RetrievalChannel.R1_EXACT: (remaining_hit,)},
        candidate_limit=2,
    )
    assert remaining_only
    original_remaining_only, _, _, _ = no_reranker._route_candidates(
        semantic_need,
        plan,
        {RetrievalChannel.R1_EXACT: (remaining_hit,)},
        candidate_limit=2,
    )
    assert original_remaining_only


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


def test_controller_rejects_selected_candidate_without_qualifying_evidence() -> None:
    unit = RetrievalUnit(
        unit_id=StableId("anchor.empty"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        text="   ",
        mandatory=True,
    )
    runtime, _ = controller((unit,))
    hit = ChannelHit(
        unit=unit,
        channel=RetrievalChannel.R1_EXACT,
        channel_rank=1,
        raw_score=1.0,
        candidate_count=1,
        hit_reason="forced empty-evidence candidate",
    )
    result = runtime._finalize(
        request(),
        text_root(),
        cast(
            Any,
            {
                "tool_calls": [
                    {
                        "need_id": "need.1",
                        "tool_name": "memory.search_exact",
                        "result": ToolResult(
                            tool_call_id=StableId("call.empty-evidence"),
                            status=ToolResultStatus.SUCCEEDED,
                            basis_commit=COMMIT,
                            snapshot_id=SNAPSHOT,
                            payload={"hits": [hit.model_dump(mode="json")]},
                            coverage=1.0,
                            audit_ref=StableId("audit.empty-evidence"),
                        ).model_dump(mode="json"),
                    }
                ],
                "policy_decisions": [],
                "stopped": True,
                "stop_reason": ControllerStopReason.SUFFICIENT.value,
            },
        ),
    )

    assert result["resolution"].status is ResolutionStatus.PARTIAL
    assert result["resolution"].stop_reason is ControllerStopReason.NO_ADDITIONAL_EVIDENCE
    assert "lack qualifying evidence" in result["resolution"].unresolved_gaps[-1]
