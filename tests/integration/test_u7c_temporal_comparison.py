"""First deterministic U7-C comparison slice for the selected Writer leaf."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from temporalio import workflow

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.editorial import EditorialVerdict
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId
from novel_agent.domain.v05_readout import (
    EvaluationNamespaceDiscardReceipt,
    MemoryIdentitySnapshot,
    V05HistoryAccess,
    V05ReadoutTaskIdentity,
    V05ReadoutTrack,
)
from novel_agent.domain.writer_context import BenchmarkInformationProfile
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.runtime.temporal_writer_candidate import (
    WRITER_TEMPORAL_ACTIVITY_NAME,
    WRITER_TEMPORAL_GRAPH_NAME,
    WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
    WRITER_TEMPORAL_TASK_QUEUE,
    WRITER_TEMPORAL_WORKFLOW_BUILD,
    WRITER_TEMPORAL_WORKFLOW_PATCH_ID,
    ActivityWrappedWriterSequenceWorkflow,
    ActivityWrappedWriterWorkflow,
    PluginIntegratedWriterSequenceWorkflow,
    PluginIntegratedWriterWorkflow,
    build_activity_worker,
    build_plugin_worker,
    build_writer_activity,
    build_writer_graph,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from tests.integration.test_temporal_writer_candidate import (
    _FixedWriter,
    _namespaced_client,
    _request,
    _request_payload,
    _result,
    _run_both,
)
from tests.integration.test_writer_context_loop import _loop

pytestmark = pytest.mark.integration


class _MappedWriter:
    def __init__(self, results: tuple[WritingLoopResult, ...]) -> None:
        self._results = {result.task_id.root: result for result in results}
        self.calls = 0
        self.calls_by_task: dict[str, int] = {}

    async def run(self, request: WritingLoopRequest) -> WritingLoopResult:
        self.calls += 1
        task_id = request.task_id.root
        self.calls_by_task[task_id] = self.calls_by_task.get(task_id, 0) + 1
        return self._results[request.task_id.root]


class _CommitThenCrashSettlement:
    def __init__(self) -> None:
        self.calls = 0
        self._committed: dict[str, dict[str, object]] = {}

    async def run(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        effect_identity = str(payload["effect_identity"])
        prior = self._committed.get(effect_identity)
        if prior is None:
            prior = {
                "effect_identity": effect_identity,
                "commit_id": f"commit.{effect_identity}",
                "status": "COMMITTED",
                "reconciled": False,
            }
            self._committed[effect_identity] = prior
            raise RuntimeError("commit succeeded before settlement bookkeeping")
        return {**prior, "reconciled": True}

    @property
    def committed_effects(self) -> tuple[str, ...]:
        return tuple(self._committed)


def _history_contains_patch(history: Any) -> bool:
    return any(
        event.HasField("marker_recorded_event_attributes")
        and any(
            WRITER_TEMPORAL_WORKFLOW_PATCH_ID.encode() in payload.data
            for payloads in event.marker_recorded_event_attributes.details.values()
            for payload in payloads.payloads
        )
        for event in history.events
    )


@workflow.defn(name="ActivityWrappedWriterWorkflow", sandboxed=False)
class _LegacyActivityWrappedWriterWorkflow:
    """Pre-patch workflow used only to create an immutable old History fixture."""

    def __init__(self) -> None:
        self._released = False

    @workflow.run
    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        if bool(state.get("start_paused", False)):
            await workflow.wait_condition(lambda: self._released)
        result = await workflow.execute_activity(
            WRITER_TEMPORAL_ACTIVITY_NAME,
            state,
            start_to_close_timeout=timedelta(seconds=120),
        )
        if not isinstance(result, dict):
            raise RuntimeError("legacy Writer Activity returned a non-dict result")
        return result


@workflow.defn(name="PluginIntegratedWriterWorkflow", sandboxed=False)
class _LegacyPluginIntegratedWriterWorkflow:
    """Pre-patch plugin workflow used only to create an immutable old History fixture."""

    def __init__(self) -> None:
        self._released = False

    @workflow.run
    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        from temporalio.contrib.langgraph import graph

        if bool(state.get("start_paused", False)):
            await workflow.wait_condition(lambda: self._released)
        result = await graph(WRITER_TEMPORAL_GRAPH_NAME).compile().ainvoke(state)
        if not isinstance(result, dict):
            raise RuntimeError("legacy Writer graph returned a non-dict state")
        return result


def _legacy_activity_worker(
    client: Any,
    delegate: _FixedWriter,
    artifacts: ArtifactRepository,
) -> Any:
    from temporalio.worker import Worker

    return Worker(
        client,
        task_queue=WRITER_TEMPORAL_TASK_QUEUE,
        workflows=[_LegacyActivityWrappedWriterWorkflow],
        activities=[build_writer_activity(delegate, artifacts)],
        max_cached_workflows=0,
    )


def _legacy_plugin_worker(
    client: Any,
    delegate: _FixedWriter,
    artifacts: ArtifactRepository,
    *,
    runtime_key: str,
) -> Any:
    from temporalio.contrib.langgraph import LangGraphPlugin
    from temporalio.worker import Worker

    plugin = LangGraphPlugin(
        graphs={
            WRITER_TEMPORAL_GRAPH_NAME: build_writer_graph(
                delegate,
                artifacts,
                runtime_key=runtime_key,
            )
        }
    )
    return Worker(
        client,
        task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
        workflows=[_LegacyPluginIntegratedWriterWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    )


async def _stop_worker_task(worker: Any, task: asyncio.Task[Any]) -> None:
    shutdown_task = asyncio.create_task(worker.shutdown())
    try:
        await asyncio.wait_for(shutdown_task, timeout=5)
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        shutdown_task.cancel()
        await asyncio.gather(shutdown_task, return_exceptions=True)
    else:
        await asyncio.gather(task, return_exceptions=True)


def _public_result_shape(result: WritingLoopResult) -> tuple[object, ...]:
    return (
        result.status,
        result.run_id,
        result.task_id,
        result.candidate_only,
        result.canon_mutated,
        result.commit_called,
        result.failure_detail,
    )


def test_u7c_single_no_repair_matches_direct_and_both_temporal_forms(tmp_path: Path) -> None:
    async def run() -> tuple[WritingLoopResult, dict[str, object], dict[str, object]]:
        direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
        direct_request = _request(direct_artifacts, "u7c-single-no-repair")
        direct_delegate = _FixedWriter(_result(direct_request))
        baseline = await direct_delegate.run(direct_request)
        temporal, plugin, _, _ = await _run_both(
            tmp_path / "temporal",
            suffix="u7c-single-no-repair",
        )
        return baseline, temporal, plugin

    baseline, temporal, plugin = asyncio.run(run())
    assert _public_result_shape(baseline) == (
        baseline.status,
        baseline.run_id,
        baseline.task_id,
        True,
        False,
        False,
        baseline.failure_detail,
    )
    assert temporal["terminal_status"] == plugin["terminal_status"] == baseline.status.value
    assert temporal["run_id"] == plugin["run_id"]
    assert temporal["task_id"] == plugin["task_id"]
    for state in (temporal, plugin):
        assert set(state["result_artifact_ref"]) == {
            "artifact_id",
            "media_type",
            "byte_length",
            "schema_version",
        }
        assert not {"chapter_text", "raw_response", "author_plan"} & set(state)


def test_u7c_resumable_checkpoint_routes_equally_in_both_temporal_forms(tmp_path: Path) -> None:
    async def run() -> tuple[
        dict[str, object],
        dict[str, object],
        WritingLoopResult,
        WritingLoopResult,
    ]:
        direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
        graph_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph"))
        direct_request = _request(direct_artifacts, "u7c-resumable")
        graph_request = _request(graph_artifacts, "u7c-resumable")

        async def build_resumable(
            request: WritingLoopRequest,
            artifacts: ArtifactRepository,
            root: Path,
        ) -> WritingLoopResult:
            engine = create_engine("sqlite+pysqlite:///:memory:")
            Base.metadata.create_all(engine)
            factory = build_session_factory(engine)
            repositories = (RunEventLogRepository(factory), RunCheckpointRepository(factory))
            bounded = request.model_copy(
                update={
                    "budgets": request.budgets.model_copy(update={"max_post_draft_model_calls": 0})
                }
            )
            loop, model_request, _ = _loop(
                root,
                repositories,
                bounded,
                EditorialVerdict.PASS,
                artifact_repository=artifacts,
            )
            result = await loop.execute(bounded, model_request, cast(Any, object()))
            engine.dispose()
            return result

        direct_result = await build_resumable(
            direct_request,
            direct_artifacts,
            tmp_path / "direct-loop",
        )
        graph_result = await build_resumable(
            graph_request,
            graph_artifacts,
            tmp_path / "graph-loop",
        )
        assert direct_result.status is WritingLoopTerminalStatus.YIELDED
        assert graph_result.status is WritingLoopTerminalStatus.YIELDED

        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            direct_delegate = _FixedWriter(direct_result)
            graph_delegate = _FixedWriter(graph_result)
            async with (
                build_activity_worker(client, direct_delegate, direct_artifacts),
                build_plugin_worker(
                    client,
                    graph_delegate,
                    graph_artifacts,
                    runtime_key="u7c-resumable",
                ),
            ):
                direct = await client.start_workflow(
                    ActivityWrappedWriterWorkflow.run,
                    _request_payload(direct_artifacts, direct_request),
                    id="wf.u7c.resumable.activity",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                graph = await client.start_workflow(
                    PluginIntegratedWriterWorkflow.run,
                    _request_payload(
                        graph_artifacts,
                        graph_request,
                        runtime_key="u7c-resumable",
                    ),
                    id="wf.u7c.resumable.plugin",
                    task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
                )
                return await direct.result(), await graph.result(), direct_result, graph_result

    temporal, plugin, direct_result, graph_result = asyncio.run(run())
    assert temporal["phase"] == plugin["phase"] == "RESUMABLE_CHECKPOINT"
    assert temporal["terminal_status"] == plugin["terminal_status"] == "YIELDED"
    assert direct_result.checkpoint_ref is not None
    assert graph_result.checkpoint_ref is not None
    assert temporal["checkpoint_ref"] == direct_result.checkpoint_ref.model_dump(mode="json")
    assert plugin["checkpoint_ref"] == graph_result.checkpoint_ref.model_dump(mode="json")
    assert temporal["checkpoint_ref"]["media_type"] == plugin["checkpoint_ref"]["media_type"]
    assert temporal["checkpoint_ref"]["byte_length"] == plugin["checkpoint_ref"]["byte_length"]


def test_u7c_safe_continue_as_new_preserves_ref_only_sequence(tmp_path: Path) -> None:
    async def run() -> tuple[dict[str, object], dict[str, object], int, int]:
        direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
        graph_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph"))
        direct_requests = tuple(
            _request(direct_artifacts, f"u7c-sequence-{index}") for index in (1, 2)
        )
        graph_requests = tuple(
            _request(graph_artifacts, f"u7c-sequence-{index}") for index in (1, 2)
        )
        direct_delegate = _MappedWriter(tuple(_result(request) for request in direct_requests))
        graph_delegate = _MappedWriter(tuple(_result(request) for request in graph_requests))
        direct_payloads = tuple(
            _request_payload(direct_artifacts, request) for request in direct_requests
        )
        graph_payloads = tuple(
            _request_payload(graph_artifacts, request) for request in graph_requests
        )

        def sequence_state(payloads: tuple[dict[str, object], ...]) -> dict[str, object]:
            return {
                "request_artifact_refs": [payload["request_artifact_ref"] for payload in payloads],
                "request_identities": [
                    {
                        "run_id": str(payload["run_id"]),
                        "task_id": str(payload["task_id"]),
                        "basis_commit": str(payload["basis_commit"]),
                    }
                    for payload in payloads
                ],
                "completed_result_refs": [],
                "next_index": 0,
                "continue_as_new": True,
                "policy_hash": "policy.u7c",
                "permission_hash": "permission.u7c",
            }

        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with (
                build_activity_worker(client, direct_delegate, direct_artifacts),
                build_plugin_worker(
                    client,
                    graph_delegate,
                    graph_artifacts,
                    runtime_key="u7c-sequence",
                ),
            ):
                direct = await client.start_workflow(
                    ActivityWrappedWriterSequenceWorkflow.run,
                    sequence_state(direct_payloads),
                    id="wf.u7c.sequence.activity",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                graph = await client.start_workflow(
                    PluginIntegratedWriterSequenceWorkflow.run,
                    {**sequence_state(graph_payloads), "runtime_key": "u7c-sequence"},
                    id="wf.u7c.sequence.plugin",
                    task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
                )
                return (
                    await direct.result(),
                    await graph.result(),
                    direct_delegate.calls,
                    graph_delegate.calls,
                )

    direct, graph, direct_calls, graph_calls = asyncio.run(run())
    assert direct["phase"] == graph["phase"] == "SEQUENCE_COMPLETE"
    assert direct["next_index"] == graph["next_index"] == 2
    assert len(direct["completed_result_refs"]) == len(graph["completed_result_refs"]) == 2
    assert direct_calls == graph_calls == 2
    assert not {"chapter_text", "raw_response", "author_plan"} & set(direct)
    assert not {"chapter_text", "raw_response", "author_plan"} & set(graph)


def test_u7c_pending_effect_blocks_continue_as_new_in_both_forms(tmp_path: Path) -> None:
    async def run() -> tuple[dict[str, object], dict[str, object], int, int]:
        direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
        graph_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph"))
        direct_request = _request(direct_artifacts, "u7c-pending-effect")
        graph_request = _request(graph_artifacts, "u7c-pending-effect")
        direct_delegate = _MappedWriter((_result(direct_request),))
        graph_delegate = _MappedWriter((_result(graph_request),))
        direct_payload = _request_payload(direct_artifacts, direct_request)
        graph_payload = _request_payload(graph_artifacts, graph_request)

        def state(payload: dict[str, object]) -> dict[str, object]:
            return {
                "request_artifact_refs": [
                    payload["request_artifact_ref"],
                    payload["request_artifact_ref"],
                ],
                "request_identities": [
                    {
                        "run_id": str(payload["run_id"]),
                        "task_id": str(payload["task_id"]),
                        "basis_commit": str(payload["basis_commit"]),
                    }
                ]
                * 2,
                "completed_result_refs": [],
                "next_index": 0,
                "continue_as_new": True,
                "pending_effect": True,
                "policy_hash": "policy.u7c",
                "permission_hash": "permission.u7c",
            }

        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with (
                build_activity_worker(client, direct_delegate, direct_artifacts),
                build_plugin_worker(
                    client,
                    graph_delegate,
                    graph_artifacts,
                    runtime_key="u7c-pending-effect",
                ),
            ):
                direct = await client.start_workflow(
                    ActivityWrappedWriterSequenceWorkflow.run,
                    state(direct_payload),
                    id="wf.u7c.pending.activity",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                graph = await client.start_workflow(
                    PluginIntegratedWriterSequenceWorkflow.run,
                    {**state(graph_payload), "runtime_key": "u7c-pending-effect"},
                    id="wf.u7c.pending.plugin",
                    task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
                )
                return (
                    await direct.result(),
                    await graph.result(),
                    direct_delegate.calls,
                    graph_delegate.calls,
                )

    direct, graph, direct_calls, graph_calls = asyncio.run(run())
    assert direct["phase"] == graph["phase"] == "CONTINUE_AS_NEW_BLOCKED"
    assert direct["next_index"] == graph["next_index"] == 1
    assert direct_calls == graph_calls == 1


def test_u7c_all_pending_boundaries_block_continue_as_new_in_both_forms(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[list[tuple[dict[str, object], dict[str, object]]], int, int]:
        direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
        graph_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph"))
        direct_requests = tuple(
            _request(direct_artifacts, f"u7c-pending-boundary-{index}") for index in range(5)
        )
        graph_requests = tuple(
            _request(graph_artifacts, f"u7c-pending-boundary-{index}") for index in range(5)
        )
        direct_delegate = _MappedWriter(tuple(_result(request) for request in direct_requests))
        graph_delegate = _MappedWriter(tuple(_result(request) for request in graph_requests))
        direct_payloads = tuple(
            _request_payload(direct_artifacts, request) for request in direct_requests
        )
        graph_payloads = tuple(
            _request_payload(graph_artifacts, request) for request in graph_requests
        )
        pending_fields = (
            "pending_acceptance",
            "pending_effect",
            "pending_repair",
            "pending_command",
            "pending_projection",
        )

        def state(payload: dict[str, object], pending_field: str) -> dict[str, object]:
            return {
                "request_artifact_refs": [payload["request_artifact_ref"]] * 2,
                "request_identities": [
                    {
                        "run_id": str(payload["run_id"]),
                        "task_id": str(payload["task_id"]),
                        "basis_commit": str(payload["basis_commit"]),
                    }
                ]
                * 2,
                "completed_result_refs": [],
                "next_index": 0,
                "continue_as_new": True,
                pending_field: True,
                "policy_hash": "policy.u7c.pending-boundary",
                "permission_hash": "permission.u7c.pending-boundary",
            }

        from temporalio.testing import WorkflowEnvironment

        results: list[tuple[dict[str, object], dict[str, object]]] = []
        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with (
                build_activity_worker(client, direct_delegate, direct_artifacts),
                build_plugin_worker(
                    client,
                    graph_delegate,
                    graph_artifacts,
                    runtime_key="u7c-pending-boundary",
                ),
            ):
                for index, pending_field in enumerate(pending_fields):
                    direct = await client.start_workflow(
                        ActivityWrappedWriterSequenceWorkflow.run,
                        state(direct_payloads[index], pending_field),
                        id=f"wf.u7c.pending-boundary.{pending_field}.activity",
                        task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                    )
                    graph = await client.start_workflow(
                        PluginIntegratedWriterSequenceWorkflow.run,
                        {
                            **state(graph_payloads[index], pending_field),
                            "runtime_key": "u7c-pending-boundary",
                        },
                        id=f"wf.u7c.pending-boundary.{pending_field}.plugin",
                        task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
                    )
                    results.append((await direct.result(), await graph.result()))
        return results, direct_delegate.calls, graph_delegate.calls

    results, direct_calls, graph_calls = asyncio.run(run())
    assert len(results) == 5
    assert direct_calls == graph_calls == 5
    for direct, graph in results:
        assert direct["phase"] == graph["phase"] == "CONTINUE_AS_NEW_BLOCKED"
        assert direct["next_index"] == graph["next_index"] == 1


def test_u7c_settlement_activity_retry_reconciles_once_in_both_forms(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[dict[str, object], dict[str, object], int, int, int, int]:
        direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
        graph_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph"))
        direct_request = _request(direct_artifacts, "u7c-settlement-retry")
        graph_request = _request(graph_artifacts, "u7c-settlement-retry")
        direct_delegate = _FixedWriter(_result(direct_request))
        graph_delegate = _FixedWriter(_result(graph_request))
        direct_settlement = _CommitThenCrashSettlement()
        graph_settlement = _CommitThenCrashSettlement()
        direct_payload = {
            **_request_payload(direct_artifacts, direct_request),
            "settlement_required": True,
            "effect_identity": "effect.u7c.settlement-retry",
        }
        graph_payload = {
            **_request_payload(
                graph_artifacts,
                graph_request,
                runtime_key="u7c-settlement-retry",
            ),
            "settlement_required": True,
            "effect_identity": "effect.u7c.settlement-retry",
        }

        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with (
                build_activity_worker(
                    client,
                    direct_delegate,
                    direct_artifacts,
                    settlement=direct_settlement,
                ),
                build_plugin_worker(
                    client,
                    graph_delegate,
                    graph_artifacts,
                    runtime_key="u7c-settlement-retry",
                    settlement=graph_settlement,
                ),
            ):
                direct = await client.start_workflow(
                    ActivityWrappedWriterWorkflow.run,
                    direct_payload,
                    id="wf.u7c.settlement-retry.activity",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                graph = await client.start_workflow(
                    PluginIntegratedWriterWorkflow.run,
                    graph_payload,
                    id="wf.u7c.settlement-retry.plugin",
                    task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
                )
                direct_result = await direct.result()
                graph_result = await graph.result()
        return (
            direct_result,
            graph_result,
            direct_delegate.calls,
            graph_delegate.calls,
            direct_settlement.calls,
            graph_settlement.calls,
        )

    direct, graph, direct_calls, graph_calls, direct_settle, graph_settle = asyncio.run(run())
    for state in (direct, graph):
        assert state["phase"] == "TERMINAL_RESULT"
        assert state["settlement_status"] == "COMMITTED"
        assert state["effect_reconciled"] is True
        assert state["settled_commit_id"] == "commit.effect.u7c.settlement-retry"
    assert direct_calls == graph_calls == 1
    assert direct_settle == graph_settle == 2


def test_u7c_old_history_replays_and_inflight_patched_worker_continues(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[
        bool,
        bool,
        dict[str, object],
        dict[str, object],
        bool,
        bool,
        int,
        int,
    ]:
        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Replayer

        direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
        graph_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph"))
        direct_request = _request(direct_artifacts, "u7c-history-upgrade-old")
        graph_request = _request(graph_artifacts, "u7c-history-upgrade-old")
        direct_delegate = _FixedWriter(_result(direct_request))
        graph_delegate = _FixedWriter(_result(graph_request))
        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with (
                _legacy_activity_worker(client, direct_delegate, direct_artifacts),
                _legacy_plugin_worker(
                    client,
                    graph_delegate,
                    graph_artifacts,
                    runtime_key="u7c-history-upgrade-old",
                ),
            ):
                old_direct = await client.start_workflow(
                    _LegacyActivityWrappedWriterWorkflow.run,
                    _request_payload(direct_artifacts, direct_request),
                    id="wf.u7c.history-upgrade.old.activity",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                old_graph = await client.start_workflow(
                    _LegacyPluginIntegratedWriterWorkflow.run,
                    _request_payload(
                        graph_artifacts,
                        graph_request,
                        runtime_key="u7c-history-upgrade-old",
                    ),
                    id="wf.u7c.history-upgrade.old.plugin",
                    task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
                )
                await old_direct.result()
                await old_graph.result()
                old_direct_history = await old_direct.fetch_history()
                old_graph_history = await old_graph.fetch_history()

            old_direct_json = old_direct_history.to_json()
            old_graph_json = old_graph_history.to_json()
            assert WRITER_TEMPORAL_WORKFLOW_PATCH_ID not in old_direct_json
            assert WRITER_TEMPORAL_WORKFLOW_PATCH_ID not in old_graph_json

            replay_direct = await Replayer(
                workflows=[ActivityWrappedWriterWorkflow],
                build_id=WRITER_TEMPORAL_WORKFLOW_BUILD,
            ).replay_workflow(old_direct_history)
            replay_graph_plugin = LangGraphPlugin(
                graphs={
                    WRITER_TEMPORAL_GRAPH_NAME: build_writer_graph(
                        graph_delegate,
                        graph_artifacts,
                        runtime_key="u7c-history-upgrade-replay",
                    )
                }
            )
            replay_graph = await Replayer(
                workflows=[PluginIntegratedWriterWorkflow],
                plugins=[replay_graph_plugin],
                build_id=WRITER_TEMPORAL_WORKFLOW_BUILD,
            ).replay_workflow(old_graph_history)
            assert replay_direct.replay_failure is None
            assert replay_graph.replay_failure is None
            direct_history_unchanged = old_direct_history.to_json() == old_direct_json
            graph_history_unchanged = old_graph_history.to_json() == old_graph_json

            inflight_direct_artifacts = ArtifactRepository(
                FilesystemObjectStore(tmp_path / "inflight-direct")
            )
            inflight_graph_artifacts = ArtifactRepository(
                FilesystemObjectStore(tmp_path / "inflight-graph")
            )
            inflight_direct_request = _request(
                inflight_direct_artifacts,
                "u7c-history-upgrade-inflight",
            )
            inflight_graph_request = _request(
                inflight_graph_artifacts,
                "u7c-history-upgrade-inflight",
            )
            inflight_direct_delegate = _FixedWriter(_result(inflight_direct_request))
            inflight_graph_delegate = _FixedWriter(_result(inflight_graph_request))
            first_workers = (
                build_activity_worker(
                    client,
                    inflight_direct_delegate,
                    inflight_direct_artifacts,
                    build_id=WRITER_TEMPORAL_WORKFLOW_BUILD,
                ),
                build_plugin_worker(
                    client,
                    inflight_graph_delegate,
                    inflight_graph_artifacts,
                    runtime_key="u7c-history-upgrade-inflight",
                    build_id=WRITER_TEMPORAL_WORKFLOW_BUILD,
                ),
            )
            first_tasks = tuple(asyncio.create_task(worker.run()) for worker in first_workers)
            try:
                inflight_direct = await client.start_workflow(
                    ActivityWrappedWriterWorkflow.run,
                    {
                        **_request_payload(inflight_direct_artifacts, inflight_direct_request),
                        "start_paused": True,
                    },
                    id="wf.u7c.history-upgrade.inflight.activity",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                inflight_graph = await client.start_workflow(
                    PluginIntegratedWriterWorkflow.run,
                    {
                        **_request_payload(
                            inflight_graph_artifacts,
                            inflight_graph_request,
                            runtime_key="u7c-history-upgrade-inflight",
                        ),
                        "start_paused": True,
                    },
                    id="wf.u7c.history-upgrade.inflight.plugin",
                    task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
                )
                deadline = timedelta(seconds=2)
                while not await inflight_direct.query(
                    ActivityWrappedWriterWorkflow.paused,
                    rpc_timeout=deadline,
                ) or not await inflight_graph.query(
                    PluginIntegratedWriterWorkflow.paused,
                    rpc_timeout=deadline,
                ):
                    await asyncio.sleep(0.01)
                before_direct_history = await inflight_direct.fetch_history()
                before_graph_history = await inflight_graph.fetch_history()
                direct_patch_seen = _history_contains_patch(before_direct_history)
                graph_patch_seen = _history_contains_patch(before_graph_history)
                assert direct_patch_seen
                assert graph_patch_seen
            finally:
                await _stop_worker_task(first_workers[0], first_tasks[0])
                await _stop_worker_task(first_workers[1], first_tasks[1])

            second_workers = (
                build_activity_worker(
                    client,
                    inflight_direct_delegate,
                    inflight_direct_artifacts,
                    build_id=WRITER_TEMPORAL_WORKFLOW_BUILD,
                ),
                build_plugin_worker(
                    client,
                    inflight_graph_delegate,
                    inflight_graph_artifacts,
                    runtime_key="u7c-history-upgrade-inflight",
                    build_id=WRITER_TEMPORAL_WORKFLOW_BUILD,
                ),
            )
            second_tasks = tuple(asyncio.create_task(worker.run()) for worker in second_workers)
            try:
                await inflight_direct.execute_update(
                    ActivityWrappedWriterWorkflow.resume,
                    "command.u7c.history-upgrade.direct",
                )
                await inflight_graph.execute_update(
                    PluginIntegratedWriterWorkflow.resume,
                    "command.u7c.history-upgrade.graph",
                )
                direct_result = await inflight_direct.result()
                graph_result = await inflight_graph.result()
            finally:
                await _stop_worker_task(second_workers[0], second_tasks[0])
                await _stop_worker_task(second_workers[1], second_tasks[1])
            return (
                direct_history_unchanged,
                graph_history_unchanged,
                direct_result,
                graph_result,
                direct_patch_seen,
                graph_patch_seen,
                inflight_direct_delegate.calls,
                inflight_graph_delegate.calls,
            )

    (
        direct_unchanged,
        graph_unchanged,
        direct,
        graph,
        direct_patch_seen,
        graph_patch_seen,
        direct_calls,
        graph_calls,
    ) = asyncio.run(run())
    assert direct_unchanged is True
    assert graph_unchanged is True
    assert direct["workflow_build"] == graph["workflow_build"] == WRITER_TEMPORAL_WORKFLOW_BUILD
    assert direct["phase"] == graph["phase"] == "TERMINAL_RESULT"
    assert direct_patch_seen is True
    assert graph_patch_seen is True
    assert direct_calls == graph_calls == 1


def test_u7c_command_matrix_has_stable_typed_outcomes_in_both_forms(tmp_path: Path) -> None:
    async def run() -> tuple[dict[str, object], dict[str, object], int, int, list[str]]:
        direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
        graph_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph"))
        direct_request = _request(direct_artifacts, "u7c-command-matrix")
        graph_request = _request(graph_artifacts, "u7c-command-matrix")
        direct_delegate = _FixedWriter(_result(direct_request))
        graph_delegate = _FixedWriter(_result(graph_request))
        direct_payload = _request_payload(direct_artifacts, direct_request)
        graph_payload = _request_payload(graph_artifacts, graph_request)
        candidate_id = "candidate.u7c.command"
        attempt_fence = 7

        def state(payload: dict[str, object]) -> dict[str, object]:
            return {
                **payload,
                "candidate_id": candidate_id,
                "attempt_fence": attempt_fence,
                "start_paused": True,
                "await_acceptance": True,
                "hold_after_acceptance": True,
            }

        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with (
                build_activity_worker(client, direct_delegate, direct_artifacts),
                build_plugin_worker(
                    client,
                    graph_delegate,
                    graph_artifacts,
                    runtime_key="u7c-command-matrix",
                ),
            ):
                direct = await client.start_workflow(
                    ActivityWrappedWriterWorkflow.run,
                    state(direct_payload),
                    id="wf.u7c.command.activity",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                graph = await client.start_workflow(
                    PluginIntegratedWriterWorkflow.run,
                    {**state(graph_payload), "runtime_key": "u7c-command-matrix"},
                    id="wf.u7c.command.plugin",
                    task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
                )
                for handle, workflow_type in (
                    (direct, ActivityWrappedWriterWorkflow),
                    (graph, PluginIntegratedWriterWorkflow),
                ):
                    assert await handle.query(workflow_type.paused)
                    assert (
                        await handle.execute_update(workflow_type.pause_command, "pause.u7c")
                        == "PAUSED"
                    )
                    assert (
                        await handle.execute_update(workflow_type.pause_command, "pause.u7c")
                        == "PAUSED"
                    )
                    assert (
                        await handle.execute_update(workflow_type.resume, "resume.u7c") == "RESUMED"
                    )
                    assert (
                        await handle.execute_update(workflow_type.resume, "resume.u7c") == "RESUMED"
                    )
                    while not await handle.query(workflow_type.awaiting_acceptance):
                        await asyncio.sleep(0.01)

                basis = str(direct_payload["basis_commit"])
                assert (
                    await direct.execute_update(
                        ActivityWrappedWriterWorkflow.approve,
                        args=(
                            "stale.approve.u7c",
                            "candidate.changed",
                            basis,
                            attempt_fence,
                        ),
                    )
                    == "STALE_FENCE"
                )
                assert (
                    await graph.execute_update(
                        PluginIntegratedWriterWorkflow.reject,
                        args=(
                            "stale.reject.u7c",
                            candidate_id,
                            "basis.changed",
                            attempt_fence,
                        ),
                    )
                    == "STALE_FENCE"
                )
                assert (
                    await direct.execute_update(
                        ActivityWrappedWriterWorkflow.extend_budget,
                        args=("budget.u7c", 3, candidate_id, basis, attempt_fence),
                    )
                    == "BUDGET_EXTENDED"
                )
                assert (
                    await direct.execute_update(
                        ActivityWrappedWriterWorkflow.extend_budget,
                        args=("budget.u7c", 3, candidate_id, basis, attempt_fence),
                    )
                    == "BUDGET_EXTENDED"
                )
                assert (
                    await direct.execute_update(
                        ActivityWrappedWriterWorkflow.approve,
                        args=("approve.u7c", candidate_id, basis, attempt_fence),
                    )
                    == "ACCEPTED"
                )
                assert (
                    await direct.execute_update(
                        ActivityWrappedWriterWorkflow.approve,
                        args=("approve.u7c", candidate_id, basis, attempt_fence),
                    )
                    == "ACCEPTED"
                )
                assert (
                    await direct.execute_update(
                        ActivityWrappedWriterWorkflow.reject,
                        args=("late.reject.u7c", candidate_id, basis, attempt_fence),
                    )
                    == "LATE_COMMAND"
                )
                assert (
                    await graph.execute_update(
                        PluginIntegratedWriterWorkflow.reject,
                        args=("reject.u7c", candidate_id, basis, attempt_fence),
                    )
                    == "REJECTED"
                )
                assert (
                    await graph.execute_update(
                        PluginIntegratedWriterWorkflow.reject,
                        args=("reject.u7c", candidate_id, basis, attempt_fence),
                    )
                    == "REJECTED"
                )
                assert (
                    await graph.execute_update(
                        PluginIntegratedWriterWorkflow.approve,
                        args=("late.approve.u7c", candidate_id, basis, attempt_fence),
                    )
                    == "LATE_COMMAND"
                )
                assert (
                    await direct.execute_update(ActivityWrappedWriterWorkflow.settle, "settle.u7c")
                    == "SETTLED"
                )
                assert (
                    await graph.execute_update(PluginIntegratedWriterWorkflow.settle, "settle.u7c")
                    == "SETTLED"
                )
                direct_result = await direct.result()
                graph_result = await graph.result()
                return (
                    direct_result,
                    graph_result,
                    direct_delegate.calls,
                    graph_delegate.calls,
                    [
                        str(direct_result["budget_extension"]),
                        str(graph_result["acceptance_status"]),
                    ],
                )

    direct, graph, direct_calls, graph_calls, markers = asyncio.run(run())
    assert direct["phase"] == "ACCEPTED"
    assert graph["phase"] == "REJECTED"
    assert direct["acceptance_status"] == "ACCEPTED"
    assert graph["acceptance_status"] == "REJECTED"
    assert direct_calls == graph_calls == 1
    assert markers == ["3", "REJECTED"]


def test_u7c_c20_c25_restart_reconciles_commit_before_settlement(tmp_path: Path) -> None:
    async def run() -> tuple[
        dict[str, object],
        dict[str, object],
        int,
        int,
        int,
        int,
        int,
        int,
        float,
    ]:
        direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
        graph_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph"))
        direct_requests = tuple(
            _request(direct_artifacts, f"u7c-c20-c25-{chapter}") for chapter in range(21, 26)
        )
        graph_requests = tuple(
            _request(graph_artifacts, f"u7c-c20-c25-{chapter}") for chapter in range(21, 26)
        )
        direct_delegate = _MappedWriter(tuple(_result(request) for request in direct_requests))
        graph_delegate = _MappedWriter(tuple(_result(request) for request in graph_requests))
        direct_settlement = _CommitThenCrashSettlement()
        graph_settlement = _CommitThenCrashSettlement()
        direct_payloads = tuple(
            _request_payload(direct_artifacts, request) for request in direct_requests
        )
        graph_payloads = tuple(
            _request_payload(graph_artifacts, request) for request in graph_requests
        )
        effect_ids = [f"effect.u7c.c{chapter}" for chapter in range(21, 26)]

        def state(payloads: tuple[dict[str, object], ...]) -> dict[str, object]:
            return {
                "request_artifact_refs": [payload["request_artifact_ref"] for payload in payloads],
                "request_identities": [
                    {
                        "run_id": str(payload["run_id"]),
                        "task_id": str(payload["task_id"]),
                        "basis_commit": str(payload["basis_commit"]),
                    }
                    for payload in payloads
                ],
                "effect_identities": effect_ids,
                "completed_result_refs": [],
                "settlement_artifact_refs": [],
                "settlement_effect_ids": [],
                "settled_commit_ids": [],
                "effect_reconciled_count": 0,
                "next_index": 0,
                "continue_as_new": False,
                "settlement_required": True,
                "pause_after_index": 2,
                "policy_hash": "policy.u7c",
                "permission_hash": "permission.u7c",
            }

        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)

            def make_workers(build_id: str) -> tuple[Any, Any]:
                return (
                    build_activity_worker(
                        client,
                        direct_delegate,
                        direct_artifacts,
                        settlement=direct_settlement,
                        build_id=build_id,
                    ),
                    build_plugin_worker(
                        client,
                        graph_delegate,
                        graph_artifacts,
                        runtime_key="u7c-c20-c25",
                        settlement=graph_settlement,
                        build_id=build_id,
                    ),
                )

            async def stop_workers(workers: tuple[Any, Any], tasks: tuple[Any, Any]) -> None:
                shutdown_tasks = tuple(asyncio.create_task(worker.shutdown()) for worker in workers)
                try:
                    await asyncio.wait_for(asyncio.gather(*shutdown_tasks), timeout=5)
                except TimeoutError:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    for shutdown_task in shutdown_tasks:
                        shutdown_task.cancel()
                    await asyncio.gather(*shutdown_tasks, return_exceptions=True)
                else:
                    await asyncio.gather(*tasks, return_exceptions=True)

            first_workers = make_workers("u7c-original")
            first_tasks = tuple(asyncio.create_task(worker.run()) for worker in first_workers)
            await asyncio.sleep(0.05)
            second_workers: tuple[Any, Any] | None = None
            second_tasks: tuple[Any, Any] | None = None
            first_stopped = False
            try:
                direct = await client.start_workflow(
                    ActivityWrappedWriterSequenceWorkflow.run,
                    state(direct_payloads),
                    id="wf.u7c.c20-c25.activity",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                graph = await client.start_workflow(
                    PluginIntegratedWriterSequenceWorkflow.run,
                    {**state(graph_payloads), "runtime_key": "u7c-c20-c25"},
                    id="wf.u7c.c20-c25.plugin",
                    task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
                )
                for _ in range(600):
                    if (
                        await direct.query(ActivityWrappedWriterSequenceWorkflow.sequence_progress)
                        == "WAITING_RESTART:2"
                        and await graph.query(
                            PluginIntegratedWriterSequenceWorkflow.sequence_progress
                        )
                        == "WAITING_RESTART:2"
                    ):
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("workflows did not reach the C22/C23 restart boundary")
                await stop_workers(first_workers, first_tasks)
                first_stopped = True
                recovery_started = time.perf_counter()
                second_workers = make_workers("u7c-original")
                second_tasks = tuple(asyncio.create_task(worker.run()) for worker in second_workers)
                await asyncio.sleep(0.05)
                await direct.signal(ActivityWrappedWriterSequenceWorkflow.resume_after_restart)
                await graph.signal(PluginIntegratedWriterSequenceWorkflow.resume_after_restart)
                direct_result = await direct.result()
                graph_result = await graph.result()
                recovery_seconds = time.perf_counter() - recovery_started
                print(f"U7C_RECOVERY_SECONDS {recovery_seconds:.6f}", flush=True)
            finally:
                if second_workers is not None and second_tasks is not None:
                    await stop_workers(second_workers, second_tasks)
                if not first_stopped:
                    await stop_workers(first_workers, first_tasks)
            return (
                direct_result,
                graph_result,
                direct_delegate.calls,
                graph_delegate.calls,
                direct_settlement.calls,
                graph_settlement.calls,
                len(direct_settlement.committed_effects),
                len(graph_settlement.committed_effects),
                recovery_seconds,
            )

    (
        direct,
        graph,
        direct_calls,
        graph_calls,
        direct_settle_calls,
        graph_settle_calls,
        direct_effects,
        graph_effects,
        recovery_seconds,
    ) = asyncio.run(run())
    assert direct["phase"] == graph["phase"] == "SEQUENCE_COMPLETE"
    assert direct["next_index"] == graph["next_index"] == 5
    assert len(direct["completed_result_refs"]) == len(graph["completed_result_refs"]) == 5
    assert len(direct["settlement_artifact_refs"]) == len(graph["settlement_artifact_refs"]) == 5
    assert direct["settlement_effect_ids"] == graph["settlement_effect_ids"]
    assert direct["settled_commit_ids"] == graph["settled_commit_ids"]
    assert direct["effect_reconciled_count"] == graph["effect_reconciled_count"] == 5
    assert direct_calls == graph_calls == 5
    assert direct_settle_calls == graph_settle_calls == 10
    assert direct_effects == graph_effects == 5
    assert recovery_seconds <= 30
    assert len(set(direct["settlement_effect_ids"])) == 5
    assert not {"chapter_text", "raw_response", "author_plan"} & set(direct)
    assert not {"chapter_text", "raw_response", "author_plan"} & set(graph)


def test_u7c_track_b_twenty_chapter_profiles_match_in_both_temporal_forms(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        _MappedWriter,
        _MappedWriter,
    ]:
        direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
        graph_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph"))
        profiles = ("history_only", "author_plan_conditioned")
        direct_requests = tuple(
            _request(direct_artifacts, f"u7c-track-b-{profile}-{chapter}")
            for profile in profiles
            for chapter in range(1, 21)
        )
        graph_requests = tuple(
            _request(graph_artifacts, f"u7c-track-b-{profile}-{chapter}")
            for profile in profiles
            for chapter in range(1, 21)
        )
        direct_delegate = _MappedWriter(tuple(_result(request) for request in direct_requests))
        graph_delegate = _MappedWriter(tuple(_result(request) for request in graph_requests))
        direct_payloads = tuple(
            _request_payload(direct_artifacts, request) for request in direct_requests
        )
        graph_payloads = tuple(
            _request_payload(graph_artifacts, request) for request in graph_requests
        )

        def state(
            payloads: tuple[dict[str, object], ...], profile: str, offset: int
        ) -> dict[str, object]:
            selected = payloads[offset : offset + 20]
            return {
                "request_artifact_refs": [payload["request_artifact_ref"] for payload in selected],
                "request_identities": [
                    {
                        "run_id": str(payload["run_id"]),
                        "task_id": str(payload["task_id"]),
                        "basis_commit": str(payload["basis_commit"]),
                    }
                    for payload in selected
                ],
                "completed_result_refs": [],
                "next_index": 0,
                "continue_as_new": True,
                "continue_as_new_after": 10,
                "information_profile": profile,
                "profile_namespace": f"u7c.track-b.{profile}",
                "policy_hash": "policy.u7c.track-b",
                "permission_hash": "permission.u7c.track-b",
            }

        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with (
                build_activity_worker(client, direct_delegate, direct_artifacts),
                build_plugin_worker(
                    client,
                    graph_delegate,
                    graph_artifacts,
                    runtime_key="u7c-track-b",
                ),
            ):
                results: list[dict[str, object]] = []
                for profile, offset, name in (
                    (profiles[0], 0, "history"),
                    (profiles[1], 20, "apc"),
                ):
                    direct = await client.start_workflow(
                        ActivityWrappedWriterSequenceWorkflow.run,
                        state(direct_payloads, profile, offset),
                        id=f"wf.u7c.track-b.{name}.activity",
                        task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                    )
                    results.append(await direct.result())
                    graph = await client.start_workflow(
                        PluginIntegratedWriterSequenceWorkflow.run,
                        {
                            **state(graph_payloads, profile, offset),
                            "runtime_key": "u7c-track-b",
                        },
                        id=f"wf.u7c.track-b.{name}.plugin",
                        task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
                    )
                    results.append(await graph.result())
                return (
                    results[0],
                    results[1],
                    results[2],
                    results[3],
                    direct_delegate,
                    graph_delegate,
                )

    (
        direct_history,
        graph_history,
        direct_apc,
        graph_apc,
        direct_delegate,
        graph_delegate,
    ) = asyncio.run(run())
    for direct, graph, profile, namespace in (
        (direct_history, graph_history, "history_only", "u7c.track-b.history_only"),
        (direct_apc, graph_apc, "author_plan_conditioned", "u7c.track-b.author_plan_conditioned"),
    ):
        assert direct["phase"] == graph["phase"] == "SEQUENCE_COMPLETE"
        assert direct["next_index"] == graph["next_index"] == 20
        assert len(direct["completed_result_refs"]) == len(graph["completed_result_refs"]) == 20
        assert direct["information_profile"] == graph["information_profile"] == profile
        assert direct["profile_namespace"] == graph["profile_namespace"] == namespace
    assert direct_delegate.calls == graph_delegate.calls == 40, graph_delegate.calls_by_task
    assert all(count == 1 for count in direct_delegate.calls_by_task.values())
    assert all(count == 1 for count in graph_delegate.calls_by_task.values())
    assert direct_history["profile_namespace"] != direct_apc["profile_namespace"]
    for state in (direct_history, graph_history, direct_apc, graph_apc):
        assert not {"chapter_text", "raw_response", "author_plan"} & set(state)


def test_u7c_track_a_freeze_answer_discard_is_idempotent_in_both_temporal_forms(
    tmp_path: Path,
) -> None:
    identity = V05ReadoutTaskIdentity(
        task_id=StableId("task.u7c.track-a.qa"),
        track=V05ReadoutTrack.QA,
        checkpoint_id=StableId("checkpoint.u7c.track-a.c20"),
        checkpoint_chapter=20,
        history_access=V05HistoryAccess.HISTORY_ONLY,
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        plan_release="after_checkpoint_freeze",
        question_release="after_checkpoint_freeze",
        question_id=StableId("question.u7c.track-a.qa"),
    )
    assert identity.question_release == "after_checkpoint_freeze"

    async def run() -> tuple[dict[str, object], dict[str, object], int, int]:
        direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
        graph_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph"))
        direct_request = _request(direct_artifacts, "u7c-track-a")
        graph_request = _request(graph_artifacts, "u7c-track-a")
        direct_delegate = _FixedWriter(_result(direct_request))
        graph_delegate = _FixedWriter(_result(graph_request))
        direct_payload = {
            **_request_payload(direct_artifacts, direct_request),
            "track": "novelmem_qa",
            "checkpoint_chapter": identity.checkpoint_chapter,
            "readout_checkpoint_id": identity.checkpoint_id.root,
            "question_id": identity.question_id.root,
            "question_release": identity.question_release,
            "gold_revealed": False,
            "evaluation_namespace": "u7c.track-a.evaluation",
        }
        graph_payload = {
            **_request_payload(graph_artifacts, graph_request, runtime_key="u7c-track-a"),
            "track": "novelmem_qa",
            "checkpoint_chapter": identity.checkpoint_chapter,
            "readout_checkpoint_id": identity.checkpoint_id.root,
            "question_id": identity.question_id.root,
            "question_release": identity.question_release,
            "gold_revealed": False,
            "evaluation_namespace": "u7c.track-a.evaluation",
        }

        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with (
                build_activity_worker(client, direct_delegate, direct_artifacts),
                build_plugin_worker(
                    client,
                    graph_delegate,
                    graph_artifacts,
                    runtime_key="u7c-track-a",
                ),
            ):
                direct = await client.start_workflow(
                    ActivityWrappedWriterWorkflow.run,
                    direct_payload,
                    id="wf.u7c.track-a.activity",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                graph = await client.start_workflow(
                    PluginIntegratedWriterWorkflow.run,
                    graph_payload,
                    id="wf.u7c.track-a.plugin",
                    task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
                )
                direct_result = await direct.result()
                graph_result = await graph.result()
                return (
                    direct_result,
                    graph_result,
                    direct_delegate.calls,
                    graph_delegate.calls,
                )

    direct, graph, direct_calls, graph_calls = asyncio.run(run())
    assert direct["phase"] == graph["phase"] == "TERMINAL_RESULT"
    assert direct["track"] == graph["track"] == "novelmem_qa"
    assert direct["question_release"] == graph["question_release"] == "after_checkpoint_freeze"
    assert direct["gold_revealed"] is graph["gold_revealed"] is False
    assert direct_calls == graph_calls == 1

    memory_identity = MemoryIdentitySnapshot(
        commit_id=CommitId("sha256:" + "a" * 64),
        text_root=ArtifactId("sha256:" + "b" * 64),
        world_root=ArtifactId("sha256:" + "c" * 64),
        plan_root=ArtifactId("sha256:" + "d" * 64),
        profile_root=ArtifactId("sha256:" + "e" * 64),
    )

    def discard(artifacts: ArtifactRepository) -> EvaluationNamespaceDiscardReceipt:
        response_ref = artifacts.put(
            canonical_json_bytes({"answer": "evaluation-only"}),
            "application/vnd.novel-agent.evaluation.qa-writer-response+json",
            SchemaVersion("1.0.0"),
        )
        receipt = EvaluationNamespaceDiscardReceipt(
            receipt_id=StableId("discard.u7c.track-a"),
            run_id=RunId("run.u7c.track-a"),
            discarded_refs=(response_ref,),
            memory_identity_before=memory_identity,
            memory_identity_after=memory_identity,
        )
        receipt_ref = artifacts.put(
            canonical_json_bytes(receipt.model_dump(mode="json")),
            "application/vnd.novel-agent.evaluation.discard-receipt+json",
            SchemaVersion("1.0.0"),
        )
        return EvaluationNamespaceDiscardReceipt.model_validate_json(
            artifacts.read_verified(receipt_ref), strict=True
        )

    direct_discard = discard(ArtifactRepository(FilesystemObjectStore(tmp_path / "discard-direct")))
    graph_discard = discard(ArtifactRepository(FilesystemObjectStore(tmp_path / "discard-graph")))
    assert direct_discard == graph_discard
    assert direct_discard.memory_identity_before == direct_discard.memory_identity_after
    assert not {"gold", "target_realization", "question_text"} & set(direct)
    assert not {"gold", "target_realization", "question_text"} & set(graph)


def test_u7c_preregistered_history_activity_and_orchestration_limits(
    tmp_path: Path,
) -> None:
    async def run() -> list[dict[str, float | int | str]]:
        from temporalio.api.history.v1 import History
        from temporalio.testing import WorkflowEnvironment

        direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
        graph_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph"))
        direct_requests = tuple(
            _request(direct_artifacts, f"u7c-cost-direct-{chapter}") for chapter in range(1, 21)
        )
        graph_requests = tuple(
            _request(graph_artifacts, f"u7c-cost-graph-{chapter}") for chapter in range(1, 21)
        )
        direct_delegate = _MappedWriter(tuple(_result(request) for request in direct_requests))
        graph_delegate = _MappedWriter(tuple(_result(request) for request in graph_requests))
        direct_payloads = tuple(
            _request_payload(direct_artifacts, request) for request in direct_requests
        )
        graph_payloads = tuple(
            _request_payload(graph_artifacts, request) for request in graph_requests
        )

        def state(payloads: tuple[dict[str, object], ...], profile: str) -> dict[str, object]:
            return {
                "request_artifact_refs": [payload["request_artifact_ref"] for payload in payloads],
                "request_identities": [
                    {
                        "run_id": str(payload["run_id"]),
                        "task_id": str(payload["task_id"]),
                        "basis_commit": str(payload["basis_commit"]),
                    }
                    for payload in payloads
                ],
                "completed_result_refs": [],
                "next_index": 0,
                "continue_as_new": True,
                "continue_as_new_after": 10,
                "information_profile": profile,
                "profile_namespace": f"u7c.cost.{profile}",
                "policy_hash": "policy.u7c.cost",
                "permission_hash": "permission.u7c.cost",
            }

        def history_bytes(history: Any) -> int:
            return History(events=list(history.events)).ByteSize()

        def scheduled_activities(history: Any) -> int:
            return sum(
                event.HasField("activity_task_scheduled_event_attributes")
                for event in history.events
            )

        metrics: list[dict[str, float | int | str]] = []
        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with (
                build_activity_worker(client, direct_delegate, direct_artifacts),
                build_plugin_worker(
                    client,
                    graph_delegate,
                    graph_artifacts,
                    runtime_key="u7c-cost",
                ),
            ):
                for form, workflow_run, payload, queue in (
                    (
                        "activity",
                        ActivityWrappedWriterSequenceWorkflow.run,
                        state(direct_payloads, "history_only"),
                        WRITER_TEMPORAL_TASK_QUEUE,
                    ),
                    (
                        "plugin",
                        PluginIntegratedWriterSequenceWorkflow.run,
                        {**state(graph_payloads, "history_only"), "runtime_key": "u7c-cost"},
                        WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
                    ),
                ):
                    started = time.perf_counter()
                    handle = await client.start_workflow(
                        workflow_run,
                        payload,
                        id=f"wf.u7c.cost.{form}",
                        task_queue=queue,
                    )
                    result = await handle.result()
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    history = await handle.fetch_history()
                    activity_count = scheduled_activities(history)
                    assert activity_count > 0
                    metrics.append(
                        {
                            "form": form,
                            "chapters": int(result["next_index"]),
                            "history_segment_chapters": activity_count,
                            "history_bytes": history_bytes(history),
                            "history_bytes_per_chapter": history_bytes(history) / activity_count,
                            "activities": activity_count,
                            "activities_per_chapter": activity_count / activity_count,
                            "orchestration_overhead_ms": elapsed_ms,
                        }
                    )
        print("U7C_COST_METRICS " + json.dumps(metrics, sort_keys=True), flush=True)
        return metrics

    metrics = asyncio.run(run())
    assert len(metrics) == 2
    for metric in metrics:
        assert metric["chapters"] == 20
        assert metric["history_bytes_per_chapter"] <= 262144
        assert metric["activities_per_chapter"] <= 8
        assert metric["orchestration_overhead_ms"] <= 5000
