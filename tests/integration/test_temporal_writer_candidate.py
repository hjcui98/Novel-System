"""U7-B isolated Temporal candidates around the existing Writer port."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.worker import Replayer

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.runtime.temporal_writer_candidate import (
    WRITER_TEMPORAL_NAMESPACE,
    WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
    WRITER_TEMPORAL_TASK_QUEUE,
    ActivityWrappedWriterWorkflow,
    PluginIntegratedWriterWorkflow,
    build_activity_worker,
    build_plugin_worker,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from tests.integration.test_writer_context_loop import _request

pytestmark = pytest.mark.integration


class _FixedWriter:
    def __init__(self, result: WritingLoopResult, *, failures: int = 0) -> None:
        self.result = result
        self.failures = failures
        self.calls = 0

    async def run(self, request: WritingLoopRequest) -> WritingLoopResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("transient candidate failure")
        assert request.run_id == self.result.run_id
        assert request.task_id == self.result.task_id
        return self.result


def _result(request: WritingLoopRequest) -> WritingLoopResult:
    return WritingLoopResult(
        result_id=StableId("u7b-result"),
        run_id=request.run_id,
        task_id=request.task_id,
        status=WritingLoopTerminalStatus.WRITER_FAILED,
        failure_detail="deterministic candidate result",
    )


def _namespaced_client(env: Any) -> Any:
    from temporalio.client import Client

    base_client = env.client
    config = base_client.config()
    config["namespace"] = WRITER_TEMPORAL_NAMESPACE
    return Client(**config)


def _request_payload(
    artifacts: ArtifactRepository,
    request: WritingLoopRequest,
    *,
    runtime_key: str | None = None,
) -> dict[str, object]:
    ref = artifacts.put(
        canonical_json_bytes(request.model_dump(mode="json")),
        "application/json",
        SchemaVersion("1.0.0"),
    )
    payload: dict[str, object] = {
        "request_artifact_ref": ref.model_dump(mode="json"),
        "run_id": request.run_id.root,
        "task_id": request.task_id.root,
        "basis_commit": request.base_commit.root,
        "policy_hash": "policy.u7b",
        "permission_hash": "permission.u7b",
        "command_id": "command.u7b",
    }
    if runtime_key is not None:
        payload["runtime_key"] = runtime_key
    return payload


async def _run_both(
    tmp_path: Path,
    *,
    failures: int = 0,
    suffix: str = "u7b-temporal",
) -> tuple[dict[str, object], dict[str, object], _FixedWriter, _FixedWriter]:
    direct_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "direct"))
    graph_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "graph"))
    direct_request = _request(direct_artifacts, suffix)
    graph_request = _request(graph_artifacts, suffix)
    direct_delegate = _FixedWriter(_result(direct_request), failures=failures)
    graph_delegate = _FixedWriter(_result(graph_request), failures=failures)
    from temporalio.testing import WorkflowEnvironment

    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = _namespaced_client(env)
        async with (
            build_activity_worker(client, direct_delegate, direct_artifacts),
            build_plugin_worker(
                client,
                graph_delegate,
                graph_artifacts,
                runtime_key="graph",
            ),
        ):
            direct = await client.start_workflow(
                ActivityWrappedWriterWorkflow.run,
                _request_payload(direct_artifacts, direct_request),
                id="wf.u7b.activity",
                task_queue=WRITER_TEMPORAL_TASK_QUEUE,
            )
            graph = await client.start_workflow(
                PluginIntegratedWriterWorkflow.run,
                _request_payload(graph_artifacts, graph_request, runtime_key="graph"),
                id="wf.u7b.plugin",
                task_queue=WRITER_TEMPORAL_PLUGIN_TASK_QUEUE,
            )
            return await direct.result(), await graph.result(), direct_delegate, graph_delegate


def test_temporal_writer_forms_preserve_result_and_ref_only_boundary(tmp_path: Path) -> None:
    direct, graph, direct_delegate, graph_delegate = asyncio.run(_run_both(tmp_path))

    assert direct["phase"] == graph["phase"] == "TERMINAL_RESULT"
    assert direct["terminal_status"] == graph["terminal_status"] == "WRITER_FAILED"
    assert direct["result_artifact_ref"] == graph["result_artifact_ref"]
    assert direct["run_id"] == graph["run_id"]
    assert direct["task_id"] == graph["task_id"]
    assert direct_delegate.calls == graph_delegate.calls == 1
    assert "chapter_text" not in direct
    assert "raw_response" not in direct
    assert "author_plan" not in direct


def test_temporal_writer_activity_retry_is_bounded_and_equivalent(tmp_path: Path) -> None:
    direct, graph, direct_delegate, graph_delegate = asyncio.run(_run_both(tmp_path, failures=1))

    assert direct["terminal_status"] == graph["terminal_status"] == "WRITER_FAILED"
    assert direct_delegate.calls == graph_delegate.calls == 2
    assert direct["result_artifact_ref"] == graph["result_artifact_ref"]


def test_temporal_writer_duplicate_resume_command_is_stable(tmp_path: Path) -> None:
    async def run() -> tuple[str, str, str, str]:
        artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "commands"))
        request = _request(artifacts, "u7b-commands")
        delegate = _FixedWriter(_result(request))
        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with build_activity_worker(client, delegate, artifacts):
                handle = await client.start_workflow(
                    ActivityWrappedWriterWorkflow.run,
                    {**_request_payload(artifacts, request), "start_paused": True},
                    id="wf.u7b.commands",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                deadline = timedelta(seconds=2)
                while not await handle.query(
                    ActivityWrappedWriterWorkflow.paused, rpc_timeout=deadline
                ):
                    await asyncio.sleep(0.01)
                first = await handle.execute_update(
                    ActivityWrappedWriterWorkflow.resume, "command.u7b.resume"
                )
                second = await handle.execute_update(
                    ActivityWrappedWriterWorkflow.resume, "command.u7b.resume"
                )
                result = await handle.result()
                return first, second, str(result["terminal_status"]), str(delegate.calls)

    first, second, status, calls = asyncio.run(run())
    assert first == second == "RESUMED"
    assert status == "WRITER_FAILED"
    assert calls == "1"


def test_temporal_writer_replays_after_worker_lifecycle_restart(tmp_path: Path) -> None:
    async def run() -> tuple[dict[str, object], int, bool]:
        artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "restart"))
        request = _request(artifacts, "u7b-restart")
        delegate = _FixedWriter(_result(request))
        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with build_activity_worker(client, delegate, artifacts):
                handle = await client.start_workflow(
                    ActivityWrappedWriterWorkflow.run,
                    _request_payload(artifacts, request),
                    id="wf.u7b.restart",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                result = await handle.result()
                history = await handle.fetch_history()

            restarted_request = _request(artifacts, "u7b-restarted")
            restarted_delegate = _FixedWriter(_result(restarted_request))
            async with build_activity_worker(client, restarted_delegate, artifacts):
                restarted_handle = await client.start_workflow(
                    ActivityWrappedWriterWorkflow.run,
                    _request_payload(artifacts, restarted_request),
                    id="wf.u7b.restarted",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                await restarted_handle.result()

            replay = await Replayer(
                workflows=[ActivityWrappedWriterWorkflow],
            ).replay_workflow(history)
            return result, delegate.calls + restarted_delegate.calls, replay.replay_failure is None

    result, calls, replayed = asyncio.run(run())
    assert result["terminal_status"] == "WRITER_FAILED"
    assert calls == 2
    assert replayed is True


def test_temporal_writer_cancel_signal_is_terminal_before_activity(tmp_path: Path) -> None:
    async def run() -> tuple[str, int]:
        artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "cancel"))
        request = _request(artifacts, "u7b-cancel")
        delegate = _FixedWriter(_result(request))
        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with build_activity_worker(client, delegate, artifacts):
                handle = await client.start_workflow(
                    ActivityWrappedWriterWorkflow.run,
                    {**_request_payload(artifacts, request), "start_paused": True},
                    id="wf.u7b.cancel",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                while not await handle.query(
                    ActivityWrappedWriterWorkflow.paused,
                    rpc_timeout=timedelta(seconds=2),
                ):
                    await asyncio.sleep(0.01)
                await handle.signal(ActivityWrappedWriterWorkflow.cancel)
                result = await handle.result()
                return str(result["phase"]), delegate.calls

    phase, calls = asyncio.run(run())
    assert phase == "CANCELLED"
    assert calls == 0


def test_temporal_writer_rejects_changed_request_identity_before_delegate(tmp_path: Path) -> None:
    async def run() -> int:
        artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "identity"))
        request = _request(artifacts, "u7b-identity")
        delegate = _FixedWriter(_result(request))
        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with build_activity_worker(client, delegate, artifacts):
                handle = await client.start_workflow(
                    ActivityWrappedWriterWorkflow.run,
                    {
                        **_request_payload(artifacts, request),
                        "basis_commit": "sha256:" + "b" * 64,
                    },
                    id="wf.u7b.identity",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                with pytest.raises(WorkflowFailureError):
                    await handle.result()
        return delegate.calls

    assert asyncio.run(run()) == 0


def test_temporal_writer_rejects_private_payload_before_activity(tmp_path: Path) -> None:
    async def run() -> tuple[bool, bool]:
        artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "private"))
        request = _request(artifacts, "u7b-private")
        delegate = _FixedWriter(_result(request))
        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _namespaced_client(env)
            async with build_activity_worker(client, delegate, artifacts):
                handle = await client.start_workflow(
                    ActivityWrappedWriterWorkflow.run,
                    {**_request_payload(artifacts, request), "chapter_text": "private"},
                    id="wf.u7b.private",
                    task_queue=WRITER_TEMPORAL_TASK_QUEUE,
                )
                with pytest.raises(WorkflowFailureError):
                    await handle.result()
                return delegate.calls == 0, True

    no_activity, rejected = asyncio.run(run())
    assert no_activity is True
    assert rejected is True


def test_temporal_writer_public_payload_requires_an_artifact_ref() -> None:
    from novel_agent.runtime.temporal_writer_candidate import assert_public_writer_payload

    assert_public_writer_payload({"request_artifact_ref": {"artifact_id": "sha256:" + "a" * 64}})
    with pytest.raises(Exception, match="chapter_text"):
        assert_public_writer_payload({"chapter_text": "private"})
