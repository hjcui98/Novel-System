"""Isolated LangGraph adapter for the selected Stage 3 Writer leaf.

This module is a U7-A differential candidate, not a production assembly change.  The graph owns
only typed routing around the existing ``WritingLeafPort`` implementation.  Request and result
payloads live in the existing ArtifactRepository; graph state contains references and terminal
metadata only, so the graph cannot become a second text or raw-response store.
"""

from __future__ import annotations

from typing import Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import SchemaVersion
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.ports.creative_runtime import WritingLeafPort
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes

WRITER_LANGGRAPH_REQUEST_MEDIA_TYPE = (
    "application/vnd.novel-agent.u7a-writer-langgraph-request+json"
)
WRITER_LANGGRAPH_RESULT_MEDIA_TYPE = "application/vnd.novel-agent.u7a-writer-langgraph-result+json"
WRITER_LANGGRAPH_SCHEMA_VERSION = SchemaVersion("1.0.0")

WriterGraphPhase = Literal[
    "PREPARE_REQUEST_REF",
    "EXECUTE_EXISTING_WRITER_LEAF",
    "RESUMABLE_CHECKPOINT",
    "TERMINAL_RESULT",
]
WriterGraphRoute = Literal["resumable", "terminal"]


class WriterLangGraphState(TypedDict, total=False):
    """The only state allowed to cross the candidate graph boundary."""

    request_artifact_ref: dict[str, object]
    result_artifact_ref: dict[str, object]
    checkpoint_ref: dict[str, object]
    terminal_status: str
    final_candidate_id: str
    phase: WriterGraphPhase


class WriterLangGraphLeafAdapter:
    """Run the existing Writer leaf through an isolated typed LangGraph shell.

    The delegate remains the owner of Writer cognition, Memory, checkpoints, Editor, Observer,
    reconciliation, and all model/tool/DB side effects.  This adapter only serializes the public
    request/result envelope and routes the typed terminal status to a resumable or terminal state.
    """

    is_fixture = False

    def __init__(self, delegate: WritingLeafPort, artifacts: ArtifactRepository) -> None:
        self._delegate = delegate
        self._artifacts = artifacts
        self._graph: CompiledStateGraph[
            WriterLangGraphState, None, WriterLangGraphState, WriterLangGraphState
        ] = self._build_graph()

    async def run(self, request: object) -> WritingLoopResult:
        from novel_agent.domain.generation import WritingLoopRequest

        if not isinstance(request, WritingLoopRequest):
            raise TypeError("Writer LangGraph adapter requires a WritingLoopRequest")
        request_ref = self._artifacts.put(
            canonical_json_bytes(request.model_dump(mode="json")),
            WRITER_LANGGRAPH_REQUEST_MEDIA_TYPE,
            WRITER_LANGGRAPH_SCHEMA_VERSION,
        )
        initial: WriterLangGraphState = {
            "request_artifact_ref": self._ref_payload(request_ref),
            "phase": "PREPARE_REQUEST_REF",
        }
        final = cast(WriterLangGraphState, await self._graph.ainvoke(initial))
        result_ref = self._required_ref(final, "result_artifact_ref")
        result = WritingLoopResult.model_validate_json(
            self._artifacts.read_verified(result_ref),
            strict=True,
        )
        if result.status.value != final.get("terminal_status"):
            raise RuntimeError("Writer LangGraph terminal status disagrees with result artifact")
        candidate_id = final.get("final_candidate_id")
        if candidate_id is not None and (
            result.final_candidate_id is None or result.final_candidate_id.root != candidate_id
        ):
            raise RuntimeError("Writer LangGraph candidate identity disagrees with result artifact")
        checkpoint = (
            None
            if final.get("checkpoint_ref") is None
            else self._required_ref(final, "checkpoint_ref")
        )
        if checkpoint != result.checkpoint_ref:
            raise RuntimeError(
                "Writer LangGraph checkpoint identity disagrees with result artifact"
            )
        return result

    def _build_graph(
        self,
    ) -> CompiledStateGraph[WriterLangGraphState, None, WriterLangGraphState, WriterLangGraphState]:
        graph = StateGraph(WriterLangGraphState)
        graph.add_node("prepare_request_ref", self._prepare_request_ref)
        graph.add_node("execute_existing_writer_leaf", self._execute_existing_writer_leaf)
        graph.add_node("mark_resumable", self._mark_resumable)
        graph.add_node("mark_terminal", self._mark_terminal)
        graph.add_edge(START, "prepare_request_ref")
        graph.add_edge("prepare_request_ref", "execute_existing_writer_leaf")
        graph.add_conditional_edges(
            "execute_existing_writer_leaf",
            self._route_typed_terminal_async,
            {"resumable": "mark_resumable", "terminal": "mark_terminal"},
        )
        graph.add_edge("mark_resumable", END)
        graph.add_edge("mark_terminal", END)
        return graph.compile()

    @staticmethod
    def _required_ref(state: WriterLangGraphState, key: str) -> ArtifactRef:
        value = state.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"Writer LangGraph state is missing {key}")
        try:
            return ArtifactRef.model_validate(value, strict=True)
        except ValueError as error:
            raise ValueError(f"Writer LangGraph state has invalid {key}") from error

    @staticmethod
    def _ref_payload(ref: ArtifactRef) -> dict[str, object]:
        return cast(dict[str, object], ref.model_dump(mode="json"))

    async def _prepare_request_ref(self, state: WriterLangGraphState) -> WriterLangGraphState:
        self._required_ref(state, "request_artifact_ref")
        return {**state, "phase": "EXECUTE_EXISTING_WRITER_LEAF"}

    async def _execute_existing_writer_leaf(
        self,
        state: WriterLangGraphState,
    ) -> WriterLangGraphState:
        from novel_agent.domain.generation import WritingLoopRequest

        request_ref = self._required_ref(state, "request_artifact_ref")
        request = WritingLoopRequest.model_validate_json(
            self._artifacts.read_verified(request_ref),
            strict=True,
        )
        result = await self._delegate.run(request)
        if result.run_id != request.run_id or result.task_id != request.task_id:
            raise RuntimeError("Writer LangGraph delegate returned cross-task lineage")
        result_ref = self._artifacts.put_or_reuse_existing(
            canonical_json_bytes(result.model_dump(mode="json")),
            WRITER_LANGGRAPH_RESULT_MEDIA_TYPE,
            WRITER_LANGGRAPH_SCHEMA_VERSION,
        )
        update: WriterLangGraphState = {
            **state,
            "result_artifact_ref": self._ref_payload(result_ref),
            "terminal_status": result.status.value,
        }
        if result.checkpoint_ref is not None:
            update["checkpoint_ref"] = self._ref_payload(result.checkpoint_ref)
        if result.final_candidate_id is not None:
            update["final_candidate_id"] = result.final_candidate_id.root
        return update

    @staticmethod
    def _route_typed_terminal(state: WriterLangGraphState) -> WriterGraphRoute:
        status = state.get("terminal_status")
        if status in {
            WritingLoopTerminalStatus.YIELDED.value,
            WritingLoopTerminalStatus.MEMORY_BUDGET_EXHAUSTED.value,
        }:
            if not isinstance(state.get("checkpoint_ref"), dict):
                raise ValueError("resumable Writer result must expose its checkpoint ref")
            return "resumable"
        return "terminal"

    async def _route_typed_terminal_async(self, state: WriterLangGraphState) -> WriterGraphRoute:
        return self._route_typed_terminal(state)

    @staticmethod
    async def _mark_resumable(state: WriterLangGraphState) -> WriterLangGraphState:
        return {**state, "phase": "RESUMABLE_CHECKPOINT"}

    @staticmethod
    async def _mark_terminal(state: WriterLangGraphState) -> WriterLangGraphState:
        return {**state, "phase": "TERMINAL_RESULT"}


def build_writer_langgraph_leaf(
    delegate: WritingLeafPort,
    artifacts: ArtifactRepository,
) -> WriterLangGraphLeafAdapter:
    """Isolated U7-A factory; production composition does not call this function."""

    return WriterLangGraphLeafAdapter(delegate, artifacts)


__all__ = [
    "WRITER_LANGGRAPH_REQUEST_MEDIA_TYPE",
    "WRITER_LANGGRAPH_RESULT_MEDIA_TYPE",
    "WriterLangGraphLeafAdapter",
    "WriterLangGraphState",
    "build_writer_langgraph_leaf",
]
