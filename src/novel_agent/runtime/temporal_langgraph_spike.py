"""Temporal worker/workflow side of the U3.5 spike. Not a production runtime.

Module-level imports stay sandbox-safe. NS domain, converters, Worker, and the
test server are imported inside activities/runner only. ``novel_agent.runtime``
package init cannot load inside Temporal's default workflow sandbox.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, TypedDict

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

_RETRY = RetryPolicy(maximum_attempts=8, initial_interval=timedelta(milliseconds=50))
_SHUTDOWN = timedelta(seconds=1)
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
        "author_plan",
        "future_text",
    }
)


def _assert_public_payload(value: object) -> None:
    """Sandbox-safe copy of temporal_spike.assert_public_payload."""

    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in _FORBIDDEN_PAYLOAD_KEYS:
                raise ApplicationError(f"Temporal payload forbids field {key}")
            _assert_public_payload(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_public_payload(item)


def _ns() -> Any:
    from novel_agent.runtime import temporal_spike as spike

    return spike


@activity.defn
async def run_leaf_activity(task: dict[str, Any]) -> dict[str, Any]:
    import asyncio
    from pathlib import Path

    spike = _ns()
    identity = spike.decode_spike_task(task)
    store = spike.SpikeBusinessStore(Path(identity.object_root))
    result = store.apply_leaf(identity)
    hold = Path(identity.object_root) / "hold"
    if hold.exists():
        kill_requested = Path(identity.object_root) / "worker-kill"
        if kill_requested.exists():
            (Path(identity.object_root) / "worker-kill-ready").write_text("1", encoding="utf-8")
            activity.heartbeat("worker-kill-ready")
            await asyncio.sleep(30)
        activity.heartbeat("retry-after-settle")
        await asyncio.sleep(0.25)
        raise ApplicationError("spike hold forces activity retry", non_retryable=False)
    return result.model_dump(mode="json")


@workflow.defn(sandboxed=False)
class CreativeRunWorkflow:
    """Form A: one coarse leaf Activity. Recovery grain is the whole Activity."""

    def __init__(self) -> None:
        self._paused = False
        self._delaying = False
        self._activity_done = False
        self._complete_allowed = True
        self._command_id = ""

    @workflow.run
    async def run(self, task: dict[str, Any], controls: dict[str, Any]) -> dict[str, Any]:
        _assert_public_payload(task)
        _assert_public_payload(controls)
        self._paused = bool(controls.get("start_paused"))
        self._command_id = str(task.get("command_id", ""))
        await workflow.wait_condition(lambda: not self._paused)
        if bool(controls.get("delay_before_activity")):
            self._delaying = True
            await workflow.sleep(timedelta(hours=1))
            self._delaying = False
        result = await workflow.execute_activity(
            run_leaf_activity,
            task,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=2),
            retry_policy=_RETRY,
        )
        self._activity_done = True
        if bool(controls.get("hold_before_complete")):
            self._complete_allowed = False
            await workflow.wait_condition(lambda: self._complete_allowed)
        return result

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.update
    async def resume(self) -> str:
        self._paused = False
        return self._command_id or "resumed"

    @workflow.signal
    def resume_signal(self) -> None:
        self._paused = False

    @workflow.signal
    def allow_complete(self) -> None:
        self._complete_allowed = True

    @workflow.query
    def delaying(self) -> bool:
        return self._delaying

    @workflow.query
    def paused(self) -> bool:
        return self._paused

    @workflow.query
    def activity_done(self) -> bool:
        return self._activity_done


class SpikeGraphState(TypedDict, total=False):
    project_id: str
    run_id: str
    task_id: str
    command_id: str
    effect_identity: str
    object_root: str
    candidate_kind: str
    duplicate: bool
    accepted: bool


def _identity_fields(state: SpikeGraphState) -> dict[str, Any]:
    keys = (
        "project_id",
        "run_id",
        "task_id",
        "command_id",
        "effect_identity",
        "object_root",
        "candidate_kind",
    )
    return {key: state[key] for key in keys if key in state}


def _leaf_node(state: SpikeGraphState) -> SpikeGraphState:
    from pathlib import Path

    spike = _ns()
    identity = spike.decode_spike_task(_identity_fields(state))
    result = spike.SpikeBusinessStore(Path(identity.object_root)).apply_leaf(identity)
    return {
        **state,
        "candidate_kind": result.candidate_kind,
        "duplicate": result.duplicate,
        "accepted": False,
    }


def _accept_node(state: SpikeGraphState) -> SpikeGraphState:
    return {**state, "accepted": True}


def _plugin_parts() -> tuple[Any, Any, str | None, tuple[str, ...]]:
    try:
        from importlib.metadata import version

        from temporalio.contrib.langgraph import LangGraphPlugin, graph

        return LangGraphPlugin, graph, version("temporalio"), ()
    except Exception as error:
        return None, None, None, (f"langgraph_plugin_unavailable:{type(error).__name__}",)


def build_spike_graph() -> Any:
    from langgraph.graph import START, StateGraph

    graph = StateGraph(SpikeGraphState)
    graph.add_node(
        "run_leaf",
        _leaf_node,
        metadata={
            "execute_in": "activity",
            "start_to_close_timeout": timedelta(seconds=30),
            "retry_policy": _RETRY,
        },
    )
    graph.add_node(
        "accept",
        _accept_node,
        metadata={"execute_in": "workflow"},
    )
    graph.add_edge(START, "run_leaf")
    graph.add_edge("run_leaf", "accept")
    return graph


@workflow.defn(sandboxed=False)
class PluginLeafWorkflow:
    """Form B: official plugin maps graph nodes to Activity/Workflow."""

    @workflow.run
    async def run(self, task: dict[str, Any]) -> dict[str, Any]:
        from temporalio.contrib.langgraph import graph

        _assert_public_payload(task)
        state: SpikeGraphState = {**task, "duplicate": False, "accepted": False}
        result = await graph("spike-writer").compile().ainvoke(state)
        if not isinstance(result, dict):
            raise ApplicationError("plugin graph returned a non-dict state")
        return dict(result)


def _sdk_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("temporalio")
    except PackageNotFoundError:
        return "unknown"


def _plugin_status() -> tuple[bool, str | None, tuple[str, ...]]:
    plugin_cls, _graph, plugin_version, unsupported = _plugin_parts()
    return plugin_cls is not None, plugin_version, unsupported


def _event_payloads(event: Any) -> list[Any]:
    payloads: list[Any] = []
    for field, value in event.ListFields():
        if field.name.endswith("_event_attributes") and hasattr(value, "input"):
            payloads.extend(getattr(value.input, "payloads", []))
        if field.name.endswith("_event_attributes") and hasattr(value, "result"):
            payloads.extend(getattr(value.result, "payloads", []))
    return payloads


def _event_type_name(event: Any) -> str:
    try:
        from temporalio.api.enums.v1 import event_type_pb2

        return event_type_pb2.EventType.Name(event.event_type)
    except Exception:
        event_type = getattr(event, "event_type", event)
        name = getattr(event_type, "name", None)
        if isinstance(name, str) and name:
            return name
        return str(event_type)


def _data_converter() -> Any:
    from temporalio.converter import (
        CompositePayloadConverter,
        DataConverter,
        DefaultPayloadConverter,
        JSONPlainPayloadConverter,
    )

    spike = _ns()

    class PublicJSONPlainPayloadConverter(JSONPlainPayloadConverter):
        def to_payload(self, value: Any) -> Any:
            spike.assert_public_payload(value)
            return super().to_payload(value)

        def from_payload(self, payload: Any, type_hint: type | None = None) -> Any:
            value = super().from_payload(payload, type_hint)
            spike.assert_public_payload(value)
            return value

    class PublicPayloadConverter(CompositePayloadConverter):
        def __init__(self) -> None:
            json_converter = PublicJSONPlainPayloadConverter()
            super().__init__(
                *(
                    converter
                    if not isinstance(converter, JSONPlainPayloadConverter)
                    else json_converter
                    for converter in DefaultPayloadConverter.default_encoding_payload_converters
                )
            )

    return DataConverter(payload_converter_class=PublicPayloadConverter)


def _form_a_worker(env: Any, *, include_activities: bool = True) -> Any:
    from temporalio.worker import Worker

    spike = _ns()
    return Worker(
        env.client,
        task_queue=spike.SPIKE_TASK_QUEUE,
        workflows=[CreativeRunWorkflow],
        activities=[run_leaf_activity] if include_activities else [],
        graceful_shutdown_timeout=_SHUTDOWN,
    )


def _worker_process_entry(target_host: str, namespace: str, ready_path: str) -> None:
    """Serve the spike queue in a killable process for the recovery boundary."""

    import asyncio
    from pathlib import Path

    from temporalio.client import Client
    from temporalio.worker import Worker

    async def serve() -> None:
        spike = _ns()
        client = await Client.connect(
            target_host,
            namespace=namespace,
            data_converter=_data_converter(),
        )
        async with Worker(
            client,
            task_queue=spike.SPIKE_TASK_QUEUE,
            workflows=[],
            activities=[run_leaf_activity],
            graceful_shutdown_timeout=_SHUTDOWN,
        ):
            Path(ready_path).write_text("1", encoding="utf-8")
            await asyncio.Event().wait()

    asyncio.run(serve())


def _start_worker_process(env: Any, ready_path: Any) -> Any:
    from multiprocessing import get_context

    process = get_context("spawn").Process(
        target=_worker_process_entry,
        args=(env.client.service_client.config.target_host, env.client.namespace, str(ready_path)),
    )
    process.start()
    return process


def _terminate_worker_process(process: Any) -> None:
    if process.is_alive():
        process.terminate()
    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)


def _controls(
    *,
    start_paused: bool = False,
    hold_before_complete: bool = False,
    delay_before_activity: bool = False,
) -> dict[str, bool]:
    return {
        "start_paused": start_paused,
        "hold_before_complete": hold_before_complete,
        "delay_before_activity": delay_before_activity,
    }


async def _history_stats(handle: Any) -> tuple[tuple[str, ...], int, int]:
    history = await handle.fetch_history()
    events = list(history.events)
    names = tuple(sorted({_event_type_name(event) for event in events}))
    if hasattr(history, "to_json"):
        encoded = history.to_json().encode("utf-8")
    else:
        encoded = repr(events).encode()
    activity_count = sum(
        1 for event in events if "ACTIVITY_TASK_SCHEDULED" in _event_type_name(event)
    )
    return names, len(encoded), activity_count


async def _wait_query(handle: Any, method: Any, expected: bool) -> None:
    import asyncio
    import time

    deadline = time.monotonic() + 15.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = await handle.query(method, rpc_timeout=timedelta(seconds=2))
            if value is expected:
                return
        except Exception as error:
            last_error = error
        await asyncio.sleep(0.05)
    raise TimeoutError(f"workflow query {method} did not become {expected}: {last_error}")


async def _wait_for_path(path: Any, *, timeout_seconds: float = 15.0) -> None:
    import asyncio
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"spike marker {path} did not appear")


async def _start_form_a(
    env: Any,
    identity: Any,
    *,
    workflow_id: str,
    start_paused: bool,
    hold_before_complete: bool,
    delay_before_activity: bool = False,
) -> Any:
    spike = _ns()
    return await env.client.start_workflow(
        CreativeRunWorkflow.run,
        args=[
            identity.model_dump(mode="json"),
            _controls(
                start_paused=start_paused,
                hold_before_complete=hold_before_complete,
                delay_before_activity=delay_before_activity,
            ),
        ],
        id=workflow_id,
        task_queue=spike.SPIKE_TASK_QUEUE,
    )


async def _pause_resume_roundtrip(env: Any, object_root: Any) -> str:
    spike = _ns()
    identity = spike.public_spike_task(object_root=object_root, suffix="a.signal")
    handle = await _start_form_a(
        env,
        identity,
        workflow_id=f"wf.u35.a.signal.{identity.task_id.root}",
        start_paused=True,
        hold_before_complete=False,
    )
    await _wait_query(handle, CreativeRunWorkflow.paused, True)
    await handle.signal(CreativeRunWorkflow.pause)
    resumed = await handle.execute_update(CreativeRunWorkflow.resume)
    await handle.result()
    if resumed != identity.command_id.root:
        raise ApplicationError("resume update did not return command identity")
    return resumed


async def _before_activity_boundary(env: Any, object_root: Any) -> dict[str, Any]:
    spike = _ns()
    identity = spike.public_spike_task(object_root=object_root, suffix="a.pre")
    store = spike.SpikeBusinessStore(object_root)
    handle = await _start_form_a(
        env,
        identity,
        workflow_id=f"wf.u35.a.pre.{identity.task_id.root}",
        start_paused=False,
        hold_before_complete=False,
        delay_before_activity=True,
    )
    await _wait_query(handle, CreativeRunWorkflow.delaying, True)
    if identity.effect_identity.root in store._load()["effects"]:
        raise ApplicationError("effect recorded before activity was allowed to run")
    return await handle.result()


async def _after_settle_retry(
    env: Any, object_root: Any
) -> tuple[dict[str, Any], tuple[str, ...], int, int, int]:
    import time

    spike = _ns()
    identity = spike.public_spike_task(object_root=object_root, suffix="a.settle")
    store = spike.SpikeBusinessStore(object_root)
    kill_requested = object_root / "worker-kill"
    kill_ready = object_root / "worker-kill-ready"
    first_ready = object_root / "worker-first-ready"
    second_ready = object_root / "worker-second-ready"
    first_process: Any = None
    second_process: Any = None
    store.arm_hold()
    kill_requested.write_text("1", encoding="utf-8")
    async with _form_a_worker(env, include_activities=False):
        try:
            first_process = _start_worker_process(env, first_ready)
            await _wait_for_path(first_ready)
            handle = await _start_form_a(
                env,
                identity,
                workflow_id=f"wf.u35.a.settle.{identity.task_id.root}",
                start_paused=False,
                hold_before_complete=False,
            )
            await store.wait_until_settled_async(identity.effect_identity.root)
            await _wait_for_path(kill_ready)
            recovery_started = time.monotonic()
            _terminate_worker_process(first_process)
            first_process = None
            kill_requested.unlink(missing_ok=True)
            store.release_hold()
            second_process = _start_worker_process(env, second_ready)
            await _wait_for_path(second_ready)
            await env.time_environment.sleep(timedelta(seconds=5))
            result = await handle.result()
        finally:
            if first_process is not None:
                _terminate_worker_process(first_process)
            if second_process is not None:
                _terminate_worker_process(second_process)
            kill_requested.unlink(missing_ok=True)
            kill_ready.unlink(missing_ok=True)
            first_ready.unlink(missing_ok=True)
            second_ready.unlink(missing_ok=True)
            store.release_hold()
    types, history_bytes, activity_count = await _history_stats(handle)
    recovery_ms = max(0, round((time.monotonic() - recovery_started) * 1000))
    history = await handle.fetch_history()
    payload_text = b"".join(
        payload.data for event in history.events for payload in _event_payloads(event)
    ).decode("utf-8", errors="replace")
    if identity.command_id.root not in payload_text:
        raise ApplicationError("command identity missing from Temporal History")
    events = store._load()["events"]
    if not any(item.get("effect_identity") == identity.effect_identity.root for item in events):
        raise ApplicationError("command identity missing from NS events")
    return result, types, history_bytes, activity_count, recovery_ms


async def _before_complete_boundary(env: Any, object_root: Any) -> dict[str, Any]:
    spike = _ns()
    identity = spike.public_spike_task(object_root=object_root, suffix="a.done")
    store = spike.SpikeBusinessStore(object_root)
    handle = await _start_form_a(
        env,
        identity,
        workflow_id=f"wf.u35.a.done.{identity.task_id.root}",
        start_paused=False,
        hold_before_complete=True,
    )
    await store.wait_until_settled_async(identity.effect_identity.root)
    await _wait_query(handle, CreativeRunWorkflow.activity_done, True)
    await handle.signal(CreativeRunWorkflow.allow_complete)
    return await handle.result()


async def _form_a(env: Any, object_root: Any) -> Any:
    spike = _ns()
    object_root.mkdir(parents=True, exist_ok=True)
    store = spike.SpikeBusinessStore(object_root)
    async with _form_a_worker(env):
        await _pause_resume_roundtrip(env, object_root)
        await _before_activity_boundary(env, object_root)
    settle_result, types, history_bytes, activity_count, recovery_ms = await _after_settle_retry(
        env, object_root
    )
    async with _form_a_worker(env):
        await _before_complete_boundary(env, object_root)
    plugin_available, plugin_version, unsupported = _plugin_status()
    unsupported = (
        "default_sandbox_blocked_by_novel_agent.runtime_package_init",
        "temporal_cli_download_forbidden",
        *unsupported,
    )
    duplicate = bool(settle_result.get("duplicate"))
    if not duplicate:
        unsupported = (*unsupported, "settle_retry_did_not_see_duplicate_effect")
    return spike.SpikeReport(
        morphology="activity_wrapped",
        sdk_version=_sdk_version(),
        plugin_available=plugin_available,
        plugin_version=plugin_version,
        worker_build=f"temporalio-{_sdk_version()}/form-a",
        history_payload_types=types,
        history_bytes=history_bytes,
        activity_count=max(activity_count, 1),
        node_count=1,
        recovery_time_ms=recovery_ms,
        duplicate_effect_count=1 if duplicate else 0,
        business_effect_count=store.effect_count(),
        paused_resumed=True,
        unsupported_conditions=unsupported,
    )


async def _form_b(env: Any, object_root: Any) -> Any:
    import time

    from temporalio.worker import Worker

    spike = _ns()
    plugin_cls, _graph_helper, plugin_version, unsupported = _plugin_parts()
    identity = spike.public_spike_task(object_root=object_root, suffix="b")
    store = spike.SpikeBusinessStore(object_root)
    object_root.mkdir(parents=True, exist_ok=True)
    if plugin_cls is None:
        return spike.SpikeReport(
            morphology="plugin_integrated",
            sdk_version=_sdk_version(),
            plugin_available=False,
            plugin_version=plugin_version,
            worker_build=f"temporalio-{_sdk_version()}/form-b-unavailable",
            history_bytes=0,
            activity_count=0,
            node_count=2,
            recovery_time_ms=0,
            duplicate_effect_count=0,
            business_effect_count=store.effect_count(),
            unsupported_conditions=unsupported,
        )
    plugin = plugin_cls(graphs={"spike-writer": build_spike_graph()})
    types: tuple[str, ...] = ()
    history_bytes = 0
    activity_count = 0
    recovery_ms = 0
    try:
        recovery_started = time.monotonic()
        async with Worker(
            env.client,
            task_queue=f"{spike.SPIKE_TASK_QUEUE}.b",
            workflows=[PluginLeafWorkflow],
            plugins=[plugin],
            graceful_shutdown_timeout=_SHUTDOWN,
        ):
            handle = await env.client.start_workflow(
                PluginLeafWorkflow.run,
                identity.model_dump(mode="json"),
                id=f"wf.u35.b.{identity.task_id.root}",
                task_queue=f"{spike.SPIKE_TASK_QUEUE}.b",
            )
            result = await handle.result()
            types, history_bytes, activity_count = await _history_stats(handle)
            recovery_ms = max(0, round((time.monotonic() - recovery_started) * 1000))
            if not result.get("accepted"):
                unsupported = (*unsupported, "plugin_graph_did_not_accept")
    except Exception as error:
        unsupported = (*unsupported, f"plugin_integrated_failed:{type(error).__name__}")
    return spike.SpikeReport(
        morphology="plugin_integrated",
        sdk_version=_sdk_version(),
        plugin_available=True,
        plugin_version=plugin_version,
        worker_build=f"temporalio-{_sdk_version()}/form-b",
        history_payload_types=types,
        history_bytes=history_bytes,
        activity_count=activity_count,
        node_count=2,
        recovery_time_ms=recovery_ms,
        duplicate_effect_count=0,
        business_effect_count=store.effect_count(),
        unsupported_conditions=unsupported,
    )


async def run_payload_rejection(env: Any) -> None:
    from temporalio.worker import Worker

    spike = _ns()
    async with Worker(
        env.client,
        task_queue=f"{spike.SPIKE_TASK_QUEUE}.reject",
        workflows=[CreativeRunWorkflow],
        activities=[run_leaf_activity],
        graceful_shutdown_timeout=_SHUTDOWN,
    ):
        try:
            handle = await env.client.start_workflow(
                CreativeRunWorkflow.run,
                args=[
                    {"gold": "secret-answer", "object_root": "/tmp"},
                    _controls(),
                ],
                id="wf.u35.reject",
                task_queue=f"{spike.SPIKE_TASK_QUEUE}.reject",
            )
            await handle.result()
        except Exception:
            return
        raise ApplicationError("private payload was accepted")


async def _run_both(object_root: Any) -> dict[str, Any]:
    from types import SimpleNamespace

    from temporalio.client import Client
    from temporalio.testing import WorkflowEnvironment

    spike = _ns()
    object_root.mkdir(parents=True, exist_ok=True)
    base_env = await WorkflowEnvironment.start_time_skipping(
        data_converter=_data_converter(),
    )
    try:
        client_config = base_env.client.config()
        client_config["namespace"] = spike.SPIKE_NAMESPACE
        env = SimpleNamespace(client=Client(**client_config), time_environment=base_env)
        form_a = await _form_a(env, object_root / "form-a")
        await run_payload_rejection(env)
        form_b = await _form_b(env, object_root / "form-b")
        namespace = env.client.namespace
    finally:
        await base_env.shutdown()
    return {
        "namespace": namespace,
        "requested_namespace": spike.SPIKE_NAMESPACE,
        "task_queue": spike.SPIKE_TASK_QUEUE,
        "object_root": str(object_root),
        "activity_wrapped": form_a.model_dump(mode="json"),
        "plugin_integrated": form_b.model_dump(mode="json"),
    }


def run_both_morphologies(object_root: Any) -> dict[str, Any]:
    import asyncio

    return asyncio.run(_run_both(object_root))
