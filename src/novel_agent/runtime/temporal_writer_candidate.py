"""Isolated U7-B Temporal candidates for the selected Stage 3 Writer leaf.

Form A wraps the existing Writer port in one coarse Activity. Form B runs the same port through
the official Temporal LangGraph plugin. Both forms carry only ArtifactRef and typed identity in
Temporal history; the existing Writer, Artifact, Editor, Observer, and reconciliation owners stay
outside the workflow state.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import SchemaVersion
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.ports.creative_runtime import WritingLeafPort
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes

WRITER_TEMPORAL_NAMESPACE = "ns-u7b-writer-candidate"
WRITER_TEMPORAL_TASK_QUEUE = "ns-u7b-writer-candidate"
WRITER_TEMPORAL_PLUGIN_TASK_QUEUE = "ns-u7b-writer-candidate-plugin"
WRITER_TEMPORAL_ACTIVITY_NAME = "u7b.writer_leaf"
WRITER_TEMPORAL_SETTLEMENT_ACTIVITY_NAME = "u7b.chapter_settlement"
WRITER_TEMPORAL_GRAPH_NAME = "u7b.writer_graph"
WRITER_TEMPORAL_WORKFLOW_PATCH_ID = "u7c.writer.workflow-v2"
WRITER_TEMPORAL_WORKFLOW_BUILD = "u7c-writer-v2"
WRITER_TEMPORAL_RESULT_MEDIA_TYPE = "application/vnd.novel-agent.u7b-writer-result+json"
WRITER_TEMPORAL_SETTLEMENT_MEDIA_TYPE = "application/vnd.novel-agent.u7b-settlement-result+json"
WRITER_TEMPORAL_SCHEMA_VERSION = SchemaVersion("1.0.0")
WRITER_TEMPORAL_RETRY_POLICY = RetryPolicy(
    maximum_attempts=2,
    initial_interval=timedelta(milliseconds=50),
)
_ACTIVITY_TIMEOUT = timedelta(seconds=120)
_WORKER_SHUTDOWN_TIMEOUT = timedelta(seconds=1)
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "gold",
        "raw_answer",
        "raw_response",
        "raw_content",
        "target_text",
        "body",
        "chapter_text",
        "private_text",
        "question_text",
        "target_realization",
        "author_plan",
        "future_text",
    }
)


class WriterTemporalExecutor(Protocol):
    async def run(self, request: WritingLoopRequest) -> WritingLoopResult: ...


class WriterSettlementExecutor(Protocol):
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class WriterTemporalState(TypedDict, total=False):
    """The graph-side state contract; no Writer content or raw model output is allowed."""

    request_artifact_ref: dict[str, Any]
    result_artifact_ref: dict[str, Any]
    checkpoint_ref: dict[str, Any]
    terminal_status: str
    final_candidate_id: str
    phase: str
    run_id: str
    task_id: str
    basis_commit: str
    policy_hash: str
    permission_hash: str
    command_id: str
    runtime_key: str
    track: str
    checkpoint_chapter: int
    readout_checkpoint_id: str
    question_id: str
    question_release: str
    gold_revealed: bool
    evaluation_namespace: str
    workflow_build: str
    candidate_id: str
    attempt_fence: int
    await_acceptance: bool
    hold_after_acceptance: bool
    acceptance_status: str
    budget_extension: int
    settlement_required: bool
    effect_identity: str
    settlement_artifact_ref: dict[str, Any]
    settlement_status: str
    settled_commit_id: str
    effect_reconciled: bool


class WriterSequenceState(TypedDict, total=False):
    """Ref-only rolling state used to test safe Continue-As-New boundaries."""

    request_artifact_refs: list[dict[str, Any]]
    request_identities: list[dict[str, str]]
    completed_result_refs: list[dict[str, Any]]
    next_index: int
    continue_as_new: bool
    continue_as_new_after: int
    pending_acceptance: bool
    pending_effect: bool
    pending_repair: bool
    pending_command: bool
    pending_projection: bool
    policy_hash: str
    permission_hash: str
    runtime_key: str
    phase: str
    result_artifact_ref: dict[str, Any]
    checkpoint_ref: dict[str, Any]
    terminal_status: str
    final_candidate_id: str
    effect_identities: list[str]
    settlement_required: bool
    settlement_artifact_refs: list[dict[str, Any]]
    settlement_effect_ids: list[str]
    settled_commit_ids: list[str]
    effect_reconciled_count: int
    pause_after_index: int
    settlement_artifact_ref: dict[str, Any]
    effect_identity: str
    settled_commit_id: str
    effect_reconciled: bool


def assert_public_writer_payload(value: object) -> None:
    """Reject private or unstructured content before it can enter Temporal history."""

    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in _FORBIDDEN_PAYLOAD_KEYS:
                raise ApplicationError(f"Temporal Writer payload forbids field {key}")
            assert_public_writer_payload(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            assert_public_writer_payload(item)


def _required_ref(value: object, name: str) -> ArtifactRef:
    if not isinstance(value, dict):
        raise ApplicationError(f"Temporal Writer payload is missing {name}")
    try:
        return ArtifactRef.model_validate(value, strict=True)
    except ValueError as error:
        raise ApplicationError(f"Temporal Writer payload has invalid {name}") from error


def _result_update(result: WritingLoopResult, result_ref: ArtifactRef) -> dict[str, Any]:
    update: dict[str, Any] = {
        "result_artifact_ref": result_ref.model_dump(mode="json"),
        "terminal_status": result.status.value,
    }
    if result.checkpoint_ref is not None:
        update["checkpoint_ref"] = result.checkpoint_ref.model_dump(mode="json")
    if result.final_candidate_id is not None:
        update["final_candidate_id"] = result.final_candidate_id.root
    return update


def _assert_request_identity(payload: dict[str, Any], request: WritingLoopRequest) -> None:
    expected = {
        "run_id": request.run_id.root,
        "task_id": request.task_id.root,
        "basis_commit": request.base_commit.root,
    }
    for field, value in expected.items():
        supplied = payload.get(field)
        if supplied is not None and supplied != value:
            raise ApplicationError(f"Temporal Writer payload identity mismatch for {field}")


def _workflow_build() -> str:
    """Select a deterministic build identity while preserving old histories."""

    if workflow.patched(WRITER_TEMPORAL_WORKFLOW_PATCH_ID):
        return WRITER_TEMPORAL_WORKFLOW_BUILD
    return "u7c-writer-v1"


_WRITER_RUNTIME_REGISTRY: dict[
    str,
    tuple[WriterTemporalExecutor, ArtifactRepository, WriterSettlementExecutor | None],
] = {}


def _register_writer_runtime(
    runtime_key: str,
    delegate: WriterTemporalExecutor,
    artifacts: ArtifactRepository,
    settlement: WriterSettlementExecutor | None = None,
) -> None:
    if not runtime_key.strip():
        raise ValueError("Temporal Writer runtime key must not be empty")
    _WRITER_RUNTIME_REGISTRY[runtime_key] = (delegate, artifacts, settlement)


async def _execute_writer_graph_node(state: WriterTemporalState) -> WriterTemporalState:
    runtime_key = state.get("runtime_key")
    if not isinstance(runtime_key, str):
        raise ApplicationError("Temporal Writer graph state is missing runtime key")
    runtime = _WRITER_RUNTIME_REGISTRY.get(runtime_key)
    if runtime is None:
        raise ApplicationError("Temporal Writer graph runtime is not registered")
    delegate, artifacts, _settlement = runtime
    update = await _execute_writer_payload(delegate, artifacts, state)
    return cast(WriterTemporalState, {**state, **update, "phase": "ROUTE_TYPED_TERMINAL"})


async def _execute_writer_payload(
    delegate: WriterTemporalExecutor,
    artifacts: ArtifactRepository,
    request_payload: object,
) -> dict[str, Any]:
    assert_public_writer_payload(request_payload)
    if not isinstance(request_payload, dict):
        raise ApplicationError("Temporal Writer payload must be an object")
    request_ref = _required_ref(request_payload.get("request_artifact_ref"), "request_artifact_ref")
    request = WritingLoopRequest.model_validate_json(
        artifacts.read_verified(request_ref), strict=True
    )
    _assert_request_identity(request_payload, request)
    result = await delegate.run(request)
    if result.run_id != request.run_id or result.task_id != request.task_id:
        raise ApplicationError("Temporal Writer delegate returned cross-task lineage")
    result_ref = artifacts.put_or_reuse_existing(
        canonical_json_bytes(result.model_dump(mode="json")),
        WRITER_TEMPORAL_RESULT_MEDIA_TYPE,
        WRITER_TEMPORAL_SCHEMA_VERSION,
    )
    return _result_update(result, result_ref)


async def _execute_settlement_payload(
    settlement: WriterSettlementExecutor | None,
    artifacts: ArtifactRepository,
    payload: object,
) -> dict[str, Any]:
    assert_public_writer_payload(payload)
    if settlement is None or not isinstance(payload, dict):
        raise ApplicationError("Temporal Writer settlement is not configured")
    effect_identity = payload.get("effect_identity")
    if not isinstance(effect_identity, str) or not effect_identity:
        raise ApplicationError("Temporal Writer settlement payload has no effect identity")
    result = await settlement.run(payload)
    assert_public_writer_payload(result)
    if not isinstance(result, dict):
        raise ApplicationError("Temporal Writer settlement returned a non-dict result")
    result_effect = result.get("effect_identity")
    commit_id = result.get("commit_id")
    status = result.get("status")
    reconciled = result.get("reconciled")
    if (
        result_effect != effect_identity
        or not isinstance(commit_id, str)
        or not isinstance(status, str)
        or not isinstance(reconciled, bool)
    ):
        raise ApplicationError("Temporal Writer settlement returned invalid effect identity")
    settlement_ref = artifacts.put_or_reuse_existing(
        canonical_json_bytes(result),
        WRITER_TEMPORAL_SETTLEMENT_MEDIA_TYPE,
        WRITER_TEMPORAL_SCHEMA_VERSION,
    )
    return {
        "settlement_artifact_ref": settlement_ref.model_dump(mode="json"),
        "effect_identity": effect_identity,
        "settlement_status": status,
        "settled_commit_id": commit_id,
        "effect_reconciled": reconciled,
    }


async def _execute_settlement_graph_node(state: WriterTemporalState) -> WriterTemporalState:
    runtime_key = state.get("runtime_key")
    if not isinstance(runtime_key, str):
        raise ApplicationError("Temporal Writer graph state is missing runtime key")
    runtime = _WRITER_RUNTIME_REGISTRY.get(runtime_key)
    if runtime is None:
        raise ApplicationError("Temporal Writer graph runtime is not registered")
    _delegate, artifacts, settlement = runtime
    update = await _execute_settlement_payload(
        settlement,
        artifacts,
        {
            "result_artifact_ref": state.get("result_artifact_ref"),
            "effect_identity": state.get("effect_identity"),
            "run_id": state.get("run_id"),
            "task_id": state.get("task_id"),
        },
    )
    return cast(WriterTemporalState, {**state, **update})


def build_writer_activity(
    delegate: WritingLeafPort,
    artifacts: ArtifactRepository,
) -> Any:
    """Build the Form-A activity around an existing Writer port."""

    @activity.defn(name=WRITER_TEMPORAL_ACTIVITY_NAME)
    async def run_writer(payload: dict[str, Any]) -> dict[str, Any]:
        return await _execute_writer_payload(delegate, artifacts, payload)

    return run_writer


def build_settlement_activity(
    settlement: WriterSettlementExecutor,
    artifacts: ArtifactRepository,
) -> Any:
    """Build the isolated settlement Activity around the existing NS effect owner."""

    @activity.defn(name=WRITER_TEMPORAL_SETTLEMENT_ACTIVITY_NAME)
    async def run_settlement(payload: dict[str, Any]) -> dict[str, Any]:
        return await _execute_settlement_payload(settlement, artifacts, payload)

    return run_settlement


def _route_writer_state(state: WriterTemporalState) -> str:
    status = state.get("terminal_status")
    if status in {
        WritingLoopTerminalStatus.YIELDED.value,
        WritingLoopTerminalStatus.MEMORY_BUDGET_EXHAUSTED.value,
    }:
        if not isinstance(state.get("checkpoint_ref"), dict):
            raise ApplicationError("resumable Temporal Writer result has no checkpoint ref")
        return "resumable"
    return "terminal"


def _sequence_item_state(state: WriterSequenceState) -> WriterTemporalState:
    refs = state.get("request_artifact_refs")
    identities = state.get("request_identities")
    index = state.get("next_index", 0)
    if not isinstance(refs, list) or not isinstance(identities, list):
        raise ApplicationError("Temporal Writer sequence is missing request refs or identities")
    if not isinstance(index, int) or index < 0 or index >= len(refs) or index >= len(identities):
        raise ApplicationError("Temporal Writer sequence has an invalid next index")
    identity = identities[index]
    if not isinstance(identity, dict):
        raise ApplicationError("Temporal Writer sequence has an invalid request identity")
    item: WriterTemporalState = {
        "request_artifact_ref": refs[index],
        "run_id": identity.get("run_id", ""),
        "task_id": identity.get("task_id", ""),
        "basis_commit": identity.get("basis_commit", ""),
        "policy_hash": str(state.get("policy_hash", "")),
        "permission_hash": str(state.get("permission_hash", "")),
    }
    if bool(state.get("settlement_required", False)):
        item["settlement_required"] = True
        effect_identities = state.get("effect_identities")
        if (
            not isinstance(effect_identities, list)
            or index >= len(effect_identities)
            or not isinstance(effect_identities[index], str)
        ):
            raise ApplicationError("Temporal Writer sequence has no effect identity")
        item["effect_identity"] = effect_identities[index]
    runtime_key = state.get("runtime_key")
    if isinstance(runtime_key, str):
        item["runtime_key"] = runtime_key
    return item


def _sequence_after_activity(
    state: WriterSequenceState,
    result: dict[str, Any],
) -> WriterSequenceState:
    refs = state.get("request_artifact_refs")
    index = state.get("next_index", 0)
    if not isinstance(refs, list) or not isinstance(index, int):
        raise ApplicationError("Temporal Writer sequence has invalid continuation state")
    result_ref = result.get("result_artifact_ref")
    if not isinstance(result_ref, dict):
        raise ApplicationError("Temporal Writer sequence result has no result ref")
    completed = list(state.get("completed_result_refs", []))
    completed.append(result_ref)
    updated: WriterSequenceState = {
        **state,
        "completed_result_refs": completed,
        "next_index": index + 1,
        "phase": (
            "WRITER_COMPLETE"
            if bool(state.get("settlement_required", False))
            else "SEQUENCE_COMPLETE"
            if index + 1 >= len(refs)
            else "CHAPTER_SETTLED"
        ),
        "result_artifact_ref": result_ref,
        "terminal_status": str(result.get("terminal_status", "")),
    }
    checkpoint_ref = result.get("checkpoint_ref")
    if isinstance(checkpoint_ref, dict):
        updated["checkpoint_ref"] = checkpoint_ref
    final_candidate_id = result.get("final_candidate_id")
    if isinstance(final_candidate_id, str):
        updated["final_candidate_id"] = final_candidate_id
    if bool(state.get("settlement_required", False)):
        effect_identities = state.get("effect_identities")
        if (
            not isinstance(effect_identities, list)
            or index >= len(effect_identities)
            or not isinstance(effect_identities[index], str)
        ):
            raise ApplicationError("Temporal Writer sequence has no settlement effect identity")
        updated["effect_identity"] = effect_identities[index]
    return updated


def _sequence_after_settlement(
    state: WriterSequenceState,
    result: dict[str, Any],
) -> WriterSequenceState:
    settlement_ref = result.get("settlement_artifact_ref")
    effect_identity = result.get("effect_identity")
    commit_id = result.get("settled_commit_id")
    if (
        not isinstance(settlement_ref, dict)
        or not isinstance(effect_identity, str)
        or not isinstance(commit_id, str)
    ):
        raise ApplicationError("Temporal Writer sequence settlement is incomplete")
    refs = list(state.get("settlement_artifact_refs", []))
    effect_ids = list(state.get("settlement_effect_ids", []))
    commit_ids = list(state.get("settled_commit_ids", []))
    refs.append(settlement_ref)
    effect_ids.append(effect_identity)
    commit_ids.append(commit_id)
    reconciled = state.get("effect_reconciled_count", 0)
    if not isinstance(reconciled, int):
        raise ApplicationError("Temporal Writer sequence has invalid reconciliation count")
    if bool(result.get("effect_reconciled", False)):
        reconciled += 1
    index = state.get("next_index", 0)
    request_refs = state.get("request_artifact_refs")
    if not isinstance(index, int) or not isinstance(request_refs, list):
        raise ApplicationError("Temporal Writer sequence has invalid settlement frontier")
    return {
        **state,
        "settlement_artifact_refs": refs,
        "settlement_effect_ids": effect_ids,
        "settled_commit_ids": commit_ids,
        "effect_reconciled_count": reconciled,
        "phase": "SEQUENCE_COMPLETE" if index >= len(request_refs) else "CHAPTER_SETTLED",
        "settlement_artifact_ref": settlement_ref,
        "effect_identity": effect_identity,
        "settled_commit_id": commit_id,
        "effect_reconciled": bool(result.get("effect_reconciled", False)),
    }


def _continue_sequence_if_safe(
    workflow_method: Any,
    state: WriterSequenceState,
) -> WriterSequenceState:
    refs = state.get("request_artifact_refs")
    index = state.get("next_index", 0)
    if not isinstance(refs, list) or not isinstance(index, int):
        raise ApplicationError("Temporal Writer sequence has invalid continuation state")
    if index >= len(refs) or not bool(state.get("continue_as_new", False)):
        return state
    interval = state.get("continue_as_new_after", 1)
    if not isinstance(interval, int) or interval < 1:
        raise ApplicationError("Temporal Writer sequence has an invalid Continue-As-New interval")
    if index % interval != 0:
        return state
    if any(
        bool(state.get(field, False))
        for field in (
            "pending_acceptance",
            "pending_effect",
            "pending_repair",
            "pending_command",
            "pending_projection",
        )
    ):
        return {**state, "phase": "CONTINUE_AS_NEW_BLOCKED"}
    workflow.continue_as_new(state, workflow=workflow_method)


def build_writer_graph(
    delegate: WritingLeafPort,
    artifacts: ArtifactRepository,
    *,
    runtime_key: str,
    settlement: WriterSettlementExecutor | None = None,
) -> StateGraph[Any, Any, Any, Any]:
    """Build the Form-B graph; the official plugin maps its leaf node to an Activity."""

    _register_writer_runtime(runtime_key, delegate, artifacts, settlement)

    def route_typed_terminal(state: WriterTemporalState) -> WriterTemporalState:
        route = _route_writer_state(state)
        return {
            **state,
            "phase": "RESUMABLE_CHECKPOINT" if route == "resumable" else "TERMINAL_RESULT",
        }

    graph = StateGraph(WriterTemporalState)
    graph.add_node(
        "execute_writer_leaf",
        _execute_writer_graph_node,
        metadata={
            "execute_in": "activity",
            "start_to_close_timeout": _ACTIVITY_TIMEOUT,
            "retry_policy": WRITER_TEMPORAL_RETRY_POLICY,
        },
    )
    graph.add_node(
        "route_typed_terminal",
        route_typed_terminal,
        metadata={"execute_in": "workflow"},
    )
    graph.add_edge(START, "execute_writer_leaf")
    if settlement is not None:
        graph.add_node(
            "settle_writer_effect",
            _execute_settlement_graph_node,
            metadata={
                "execute_in": "activity",
                "start_to_close_timeout": _ACTIVITY_TIMEOUT,
                "retry_policy": WRITER_TEMPORAL_RETRY_POLICY,
            },
        )
        graph.add_edge("execute_writer_leaf", "settle_writer_effect")
        graph.add_edge("settle_writer_effect", "route_typed_terminal")
    else:
        graph.add_edge("execute_writer_leaf", "route_typed_terminal")
    graph.add_edge("route_typed_terminal", END)
    return graph


class _CommandControlledWorkflow:
    """Shared deterministic command semantics used by both candidate workflows."""

    def __init__(self) -> None:
        self._paused = False
        self._cancelled = False
        self._commands: dict[str, str] = {}
        self._awaiting_acceptance = False
        self._acceptance_status: str | None = None
        self._acceptance_released = False
        self._terminal = False
        self._expected_candidate_id: str | None = None
        self._expected_basis_commit: str | None = None
        self._expected_attempt_fence: int | None = None
        self._budget_extension = 0

    def _start_command_scope(self, state: dict[str, Any]) -> None:
        candidate_id = state.get("candidate_id")
        if not isinstance(candidate_id, str):
            candidate_id = state.get("final_candidate_id")
        self._expected_candidate_id = candidate_id if isinstance(candidate_id, str) else None
        basis_commit = state.get("basis_commit")
        self._expected_basis_commit = basis_commit if isinstance(basis_commit, str) else None
        attempt_fence = state.get("attempt_fence")
        self._expected_attempt_fence = attempt_fence if isinstance(attempt_fence, int) else None

    def _remember_command(self, command_id: str, outcome: str) -> str:
        if not command_id.strip():
            return "INVALID_COMMAND"
        previous = self._commands.get(command_id)
        if previous is not None:
            return previous
        self._commands[command_id] = outcome
        return outcome

    def _command_identity_matches(
        self,
        candidate_id: str,
        basis_commit: str,
        attempt_fence: int,
    ) -> bool:
        return (
            self._expected_candidate_id in (None, candidate_id)
            and self._expected_basis_commit in (None, basis_commit)
            and self._expected_attempt_fence in (None, attempt_fence)
        )

    def _start_paused(self, state: dict[str, Any]) -> None:
        self._paused = bool(state.get("start_paused"))

    async def _wait_for_start(self) -> bool:
        await workflow.wait_condition(lambda: not self._paused or self._cancelled)
        return not self._cancelled

    def _resume(self, command_id: str) -> str:
        previous = self._commands.get(command_id)
        if previous is not None:
            return previous
        if self._terminal:
            return self._remember_command(command_id, "LATE_COMMAND")
        outcome = "CANCELLED" if self._cancelled else "RESUMED"
        outcome = self._remember_command(command_id, outcome)
        if outcome == "RESUMED":
            self._paused = False
        return outcome

    def _pause_command(self, command_id: str) -> str:
        previous = self._commands.get(command_id)
        if previous is not None:
            return previous
        if self._terminal:
            return self._remember_command(command_id, "LATE_COMMAND")
        outcome = "CANCELLED" if self._cancelled else "PAUSED"
        outcome = self._remember_command(command_id, outcome)
        if outcome == "PAUSED":
            self._paused = True
        return outcome

    def _acceptance_command(
        self,
        action: str,
        command_id: str,
        candidate_id: str,
        basis_commit: str,
        attempt_fence: int,
    ) -> str:
        previous = self._commands.get(command_id)
        if previous is not None:
            return previous
        if self._terminal or not self._awaiting_acceptance or self._acceptance_status is not None:
            return self._remember_command(command_id, "LATE_COMMAND")
        if not self._command_identity_matches(candidate_id, basis_commit, attempt_fence):
            return self._remember_command(command_id, "STALE_FENCE")
        outcome = self._remember_command(command_id, action)
        if outcome == action:
            self._acceptance_status = action
        return outcome

    def _release_acceptance(self, command_id: str) -> str:
        previous = self._commands.get(command_id)
        if previous is not None:
            return previous
        if self._terminal or not self._awaiting_acceptance or self._acceptance_status is None:
            return self._remember_command(command_id, "LATE_COMMAND")
        outcome = self._remember_command(command_id, "SETTLED")
        if outcome == "SETTLED":
            self._acceptance_released = True
        return outcome

    def _extend_budget(
        self,
        command_id: str,
        amount: int,
        candidate_id: str,
        basis_commit: str,
        attempt_fence: int,
    ) -> str:
        previous = self._commands.get(command_id)
        if previous is not None:
            return previous
        if self._terminal or not self._awaiting_acceptance:
            return self._remember_command(command_id, "LATE_COMMAND")
        if amount <= 0:
            return self._remember_command(command_id, "INVALID_COMMAND")
        if not self._command_identity_matches(candidate_id, basis_commit, attempt_fence):
            return self._remember_command(command_id, "STALE_FENCE")
        outcome = self._remember_command(command_id, "BUDGET_EXTENDED")
        if outcome == "BUDGET_EXTENDED":
            self._budget_extension += amount
        return outcome

    def _cancel_command(self, command_id: str) -> str:
        previous = self._commands.get(command_id)
        if previous is not None:
            return previous
        if self._terminal:
            return self._remember_command(command_id, "LATE_COMMAND")
        outcome = self._remember_command(command_id, "CANCELLED")
        if outcome == "CANCELLED":
            self._cancel()
        return outcome

    def _cancel(self) -> None:
        self._cancelled = True
        self._paused = False

    async def _finish_after_leaf(self, state: dict[str, Any]) -> dict[str, Any]:
        if not bool(state.get("await_acceptance", False)):
            self._terminal = True
            return state
        self._awaiting_acceptance = True
        await workflow.wait_condition(
            lambda: self._acceptance_status is not None or self._cancelled
        )
        if self._cancelled:
            self._awaiting_acceptance = False
            self._terminal = True
            return {
                **state,
                "phase": "CANCELLED",
                "terminal_status": "CANCELLED",
                "acceptance_status": "CANCELLED",
                "budget_extension": self._budget_extension,
            }
        if bool(state.get("hold_after_acceptance", False)):
            await workflow.wait_condition(lambda: self._acceptance_released or self._cancelled)
            if self._cancelled:
                self._awaiting_acceptance = False
                self._terminal = True
                return {
                    **state,
                    "phase": "CANCELLED",
                    "terminal_status": "CANCELLED",
                    "acceptance_status": "CANCELLED",
                    "budget_extension": self._budget_extension,
                }
        self._awaiting_acceptance = False
        self._terminal = True
        return {
            **state,
            "phase": self._acceptance_status,
            "acceptance_status": self._acceptance_status,
            "budget_extension": self._budget_extension,
        }


class _SequenceWorkflowControl:
    def __init__(self) -> None:
        self._sequence_phase = ""
        self._sequence_index = 0
        self._sequence_resume_requested = False

    async def _wait_for_sequence_restart(self, state: WriterSequenceState) -> None:
        pause_after_index = state.get("pause_after_index")
        next_index = state.get("next_index")
        if not isinstance(pause_after_index, int) or not isinstance(next_index, int):
            return
        if next_index != pause_after_index:
            return
        self._sequence_phase = "WAITING_RESTART"
        await workflow.wait_condition(lambda: self._sequence_resume_requested)
        self._sequence_resume_requested = False
        self._sequence_phase = "RESTARTED"

    def _record_sequence_progress(self, state: WriterSequenceState) -> None:
        self._sequence_phase = str(state.get("phase", ""))
        index = state.get("next_index", 0)
        self._sequence_index = index if isinstance(index, int) else -1


@workflow.defn(sandboxed=False)
class ActivityWrappedWriterWorkflow(_CommandControlledWorkflow):
    """Form A: a coarse Activity invokes the existing Writer leaf."""

    @workflow.run
    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        assert_public_writer_payload(state)
        workflow_build = _workflow_build()
        self._start_command_scope(state)
        self._start_paused(state)
        if not await self._wait_for_start():
            self._terminal = True
            return {**state, "phase": "CANCELLED", "terminal_status": "CANCELLED"}
        result = await workflow.execute_activity(
            WRITER_TEMPORAL_ACTIVITY_NAME,
            state,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=WRITER_TEMPORAL_RETRY_POLICY,
        )
        if not isinstance(result, dict):
            raise ApplicationError("Temporal Writer activity returned a non-dict result")
        updated = cast(WriterTemporalState, {**state, **result})
        if bool(updated.get("settlement_required", False)):
            settlement = await workflow.execute_activity(
                WRITER_TEMPORAL_SETTLEMENT_ACTIVITY_NAME,
                {
                    "result_artifact_ref": updated.get("result_artifact_ref"),
                    "effect_identity": updated.get("effect_identity"),
                    "run_id": updated.get("run_id"),
                    "task_id": updated.get("task_id"),
                },
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=WRITER_TEMPORAL_RETRY_POLICY,
            )
            if not isinstance(settlement, dict):
                raise ApplicationError("Temporal Writer settlement returned a non-dict result")
            updated = cast(WriterTemporalState, {**updated, **settlement})
        phase = (
            "RESUMABLE_CHECKPOINT"
            if _route_writer_state(updated) == "resumable"
            else "TERMINAL_RESULT"
        )
        return await self._finish_after_leaf(
            {**updated, "phase": phase, "workflow_build": workflow_build}
        )

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.update
    async def pause_command(self, command_id: str) -> str:
        return self._pause_command(command_id)

    @workflow.update
    async def resume(self, command_id: str) -> str:
        return self._resume(command_id)

    @workflow.update
    async def approve(
        self,
        command_id: str,
        candidate_id: str,
        basis_commit: str,
        attempt_fence: int,
    ) -> str:
        return self._acceptance_command(
            "ACCEPTED", command_id, candidate_id, basis_commit, attempt_fence
        )

    @workflow.update
    async def reject(
        self,
        command_id: str,
        candidate_id: str,
        basis_commit: str,
        attempt_fence: int,
    ) -> str:
        return self._acceptance_command(
            "REJECTED", command_id, candidate_id, basis_commit, attempt_fence
        )

    @workflow.update
    async def extend_budget(
        self,
        command_id: str,
        amount: int,
        candidate_id: str,
        basis_commit: str,
        attempt_fence: int,
    ) -> str:
        return self._extend_budget(command_id, amount, candidate_id, basis_commit, attempt_fence)

    @workflow.update
    async def cancel_command(self, command_id: str) -> str:
        return self._cancel_command(command_id)

    @workflow.update
    async def settle(self, command_id: str) -> str:
        return self._release_acceptance(command_id)

    @workflow.signal
    def cancel(self) -> None:
        self._cancel()

    @workflow.query
    def awaiting_acceptance(self) -> bool:
        return self._awaiting_acceptance

    @workflow.query
    def paused(self) -> bool:
        return self._paused


@workflow.defn(sandboxed=False)
class PluginIntegratedWriterWorkflow(_CommandControlledWorkflow):
    """Form B: the official Temporal LangGraph plugin owns the graph node Activity."""

    @workflow.run
    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        from temporalio.contrib.langgraph import graph

        assert_public_writer_payload(state)
        workflow_build = _workflow_build()
        self._start_command_scope(state)
        self._start_paused(state)
        if not await self._wait_for_start():
            self._terminal = True
            return {**state, "phase": "CANCELLED", "terminal_status": "CANCELLED"}
        graph_result = await graph(WRITER_TEMPORAL_GRAPH_NAME).compile().ainvoke(state)
        if not isinstance(graph_result, dict):
            raise ApplicationError("Temporal LangGraph Writer returned a non-dict state")
        return await self._finish_after_leaf(
            {**dict(graph_result), "workflow_build": workflow_build}
        )

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.update
    async def pause_command(self, command_id: str) -> str:
        return self._pause_command(command_id)

    @workflow.update
    async def resume(self, command_id: str) -> str:
        return self._resume(command_id)

    @workflow.update
    async def approve(
        self,
        command_id: str,
        candidate_id: str,
        basis_commit: str,
        attempt_fence: int,
    ) -> str:
        return self._acceptance_command(
            "ACCEPTED", command_id, candidate_id, basis_commit, attempt_fence
        )

    @workflow.update
    async def reject(
        self,
        command_id: str,
        candidate_id: str,
        basis_commit: str,
        attempt_fence: int,
    ) -> str:
        return self._acceptance_command(
            "REJECTED", command_id, candidate_id, basis_commit, attempt_fence
        )

    @workflow.update
    async def extend_budget(
        self,
        command_id: str,
        amount: int,
        candidate_id: str,
        basis_commit: str,
        attempt_fence: int,
    ) -> str:
        return self._extend_budget(command_id, amount, candidate_id, basis_commit, attempt_fence)

    @workflow.update
    async def cancel_command(self, command_id: str) -> str:
        return self._cancel_command(command_id)

    @workflow.update
    async def settle(self, command_id: str) -> str:
        return self._release_acceptance(command_id)

    @workflow.signal
    def cancel(self) -> None:
        self._cancel()

    @workflow.query
    def awaiting_acceptance(self) -> bool:
        return self._awaiting_acceptance

    @workflow.query
    def paused(self) -> bool:
        return self._paused


@workflow.defn(sandboxed=False)
class ActivityWrappedWriterSequenceWorkflow(_SequenceWorkflowControl):
    """Form A sequence probe: continue only after a settled ref frontier."""

    @workflow.run
    async def run(self, state: WriterSequenceState) -> WriterSequenceState:
        assert_public_writer_payload(state)
        item = _sequence_item_state(state)
        result = await workflow.execute_activity(
            WRITER_TEMPORAL_ACTIVITY_NAME,
            item,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=WRITER_TEMPORAL_RETRY_POLICY,
        )
        if not isinstance(result, dict):
            raise ApplicationError("Temporal Writer sequence activity returned a non-dict result")
        updated = _sequence_after_activity(state, result)
        if bool(state.get("settlement_required", False)):
            settlement = await workflow.execute_activity(
                WRITER_TEMPORAL_SETTLEMENT_ACTIVITY_NAME,
                {
                    "result_artifact_ref": updated.get("result_artifact_ref"),
                    "effect_identity": updated.get("effect_identity"),
                    "run_id": item.get("run_id"),
                    "task_id": item.get("task_id"),
                },
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=WRITER_TEMPORAL_RETRY_POLICY,
            )
            if not isinstance(settlement, dict):
                raise ApplicationError("Temporal Writer sequence settlement is not a dict")
            updated = _sequence_after_settlement(updated, settlement)
        self._record_sequence_progress(updated)
        await self._wait_for_sequence_restart(updated)
        next_state = _continue_sequence_if_safe(self.run, updated)
        if next_state.get("phase") != "CONTINUE_AS_NEW_BLOCKED" and next_state.get(
            "next_index", 0
        ) < len(next_state.get("request_artifact_refs", [])):
            return await self.run(next_state)
        return next_state

    @workflow.signal
    def resume_after_restart(self) -> None:
        self._sequence_resume_requested = True

    @workflow.query
    def sequence_phase(self) -> str:
        return self._sequence_phase

    @workflow.query
    def sequence_progress(self) -> str:
        return f"{self._sequence_phase}:{self._sequence_index}"

    @workflow.update
    async def observe_progress(self) -> str:
        return f"{self._sequence_phase}:{self._sequence_index}"


@workflow.defn(sandboxed=False)
class PluginIntegratedWriterSequenceWorkflow(_SequenceWorkflowControl):
    """Form B sequence probe using the official plugin graph for each chapter."""

    @workflow.run
    async def run(self, state: WriterSequenceState) -> WriterSequenceState:
        from temporalio.contrib.langgraph import graph

        assert_public_writer_payload(state)
        item = _sequence_item_state(state)
        graph_result = await graph(WRITER_TEMPORAL_GRAPH_NAME).compile().ainvoke(item)
        if not isinstance(graph_result, dict):
            raise ApplicationError("Temporal Writer sequence graph returned a non-dict state")
        updated = _sequence_after_activity(state, graph_result)
        if bool(state.get("settlement_required", False)):
            updated = _sequence_after_settlement(updated, graph_result)
        self._record_sequence_progress(updated)
        await self._wait_for_sequence_restart(updated)
        next_state = _continue_sequence_if_safe(self.run, updated)
        if next_state.get("phase") != "CONTINUE_AS_NEW_BLOCKED" and next_state.get(
            "next_index", 0
        ) < len(next_state.get("request_artifact_refs", [])):
            return await self.run(next_state)
        return next_state

    @workflow.signal
    def resume_after_restart(self) -> None:
        self._sequence_resume_requested = True

    @workflow.query
    def sequence_phase(self) -> str:
        return self._sequence_phase

    @workflow.query
    def sequence_progress(self) -> str:
        return f"{self._sequence_phase}:{self._sequence_index}"

    @workflow.update
    async def observe_progress(self) -> str:
        return f"{self._sequence_phase}:{self._sequence_index}"


def build_activity_worker(
    client: Any,
    delegate: WritingLeafPort,
    artifacts: ArtifactRepository,
    *,
    settlement: WriterSettlementExecutor | None = None,
    build_id: str | None = None,
) -> Any:
    """Create an isolated Form-A worker; production assembly never calls this helper."""

    from temporalio.worker import Worker

    activities = [build_writer_activity(delegate, artifacts)]
    if settlement is not None:
        activities.append(build_settlement_activity(settlement, artifacts))
    return Worker(
        client,
        task_queue=WRITER_TEMPORAL_TASK_QUEUE,
        workflows=[ActivityWrappedWriterWorkflow, ActivityWrappedWriterSequenceWorkflow],
        activities=activities,
        build_id=build_id,
        use_worker_versioning=build_id is not None,
        max_cached_workflows=0,
        graceful_shutdown_timeout=_WORKER_SHUTDOWN_TIMEOUT,
    )


def build_plugin_worker(
    client: Any,
    delegate: WritingLeafPort,
    artifacts: ArtifactRepository,
    *,
    runtime_key: str,
    settlement: WriterSettlementExecutor | None = None,
    build_id: str | None = None,
) -> Any:
    """Create an isolated Form-B worker using the official LangGraph plugin."""

    from temporalio.contrib.langgraph import LangGraphPlugin
    from temporalio.worker import Worker

    plugin = LangGraphPlugin(
        graphs={
            WRITER_TEMPORAL_GRAPH_NAME: build_writer_graph(
                delegate,
                artifacts,
                runtime_key=runtime_key,
                settlement=settlement,
            )
        }
    )
    return Worker(
        client,
        task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
        workflows=[PluginIntegratedWriterWorkflow, PluginIntegratedWriterSequenceWorkflow],
        plugins=[plugin],
        build_id=build_id,
        use_worker_versioning=build_id is not None,
        max_cached_workflows=0,
        graceful_shutdown_timeout=_WORKER_SHUTDOWN_TIMEOUT,
    )


__all__ = [
    "WRITER_TEMPORAL_ACTIVITY_NAME",
    "WRITER_TEMPORAL_GRAPH_NAME",
    "WRITER_TEMPORAL_NAMESPACE",
    "WRITER_TEMPORAL_PLUGIN_TASK_QUEUE",
    "WRITER_TEMPORAL_RESULT_MEDIA_TYPE",
    "WRITER_TEMPORAL_RETRY_POLICY",
    "WRITER_TEMPORAL_SCHEMA_VERSION",
    "WRITER_TEMPORAL_SETTLEMENT_ACTIVITY_NAME",
    "WRITER_TEMPORAL_SETTLEMENT_MEDIA_TYPE",
    "WRITER_TEMPORAL_TASK_QUEUE",
    "WRITER_TEMPORAL_WORKFLOW_BUILD",
    "WRITER_TEMPORAL_WORKFLOW_PATCH_ID",
    "ActivityWrappedWriterSequenceWorkflow",
    "ActivityWrappedWriterWorkflow",
    "PluginIntegratedWriterSequenceWorkflow",
    "PluginIntegratedWriterWorkflow",
    "WriterSequenceState",
    "WriterSettlementExecutor",
    "WriterTemporalState",
    "assert_public_writer_payload",
    "build_activity_worker",
    "build_plugin_worker",
    "build_writer_activity",
    "build_writer_graph",
]
