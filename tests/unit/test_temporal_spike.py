"""Deterministic U3.5 spike store and public-payload tests. No Temporal server."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from novel_agent.runtime.temporal_spike import (
    SpikeBusinessStore,
    SpikePayloadError,
    assert_public_payload,
    decode_spike_task,
    public_spike_task,
    run_spike,
)


def test_public_payload_rejects_nested_gold() -> None:
    with pytest.raises(SpikePayloadError, match="forbids field gold"):
        assert_public_payload({"task_id": "task.u35.a", "nested": [{"gold": "secret"}]})
    assert_public_payload({"task_id": "task.u35.a", "refs": ("artifact.1",)})


def test_decode_spike_task_rejects_private_and_unknown_fields(tmp_path: Path) -> None:
    identity = public_spike_task(object_root=tmp_path).model_dump(mode="json")
    decoded = decode_spike_task(identity)
    assert decoded.task_id.root == "task.u35.a"
    with pytest.raises(SpikePayloadError, match="forbids field raw_answer"):
        decode_spike_task({**identity, "raw_answer": "nope"})
    with pytest.raises(ValueError):
        decode_spike_task({**identity, "unexpected": "x"})


def test_store_is_idempotent_and_survives_reload(tmp_path: Path) -> None:
    identity = public_spike_task(object_root=tmp_path)
    store = SpikeBusinessStore(tmp_path)
    first = store.apply_leaf(identity)
    second = store.apply_leaf(identity)
    assert first.duplicate is False
    assert second.duplicate is True
    assert second.effect_identity == first.effect_identity
    assert store.effect_count() == 1
    assert store.event_count() == 1
    reloaded = SpikeBusinessStore(tmp_path)
    assert reloaded.effect_count() == 1
    store.wait_until_settled(identity.effect_identity.root, timeout_seconds=0.2)


def test_store_rejects_corrupt_document_and_missing_effect_times_out(tmp_path: Path) -> None:
    store = SpikeBusinessStore(tmp_path)
    store.path.write_text('{"effects": {}, "events": {}}', encoding="utf-8")
    with pytest.raises(SpikePayloadError, match="unreadable"):
        store.effect_count()
    store.path.write_text('{"effects": {}, "events": []}', encoding="utf-8")
    with pytest.raises(TimeoutError, match="did not settle"):
        store.wait_until_settled("effect.missing", timeout_seconds=0.15)


def test_hold_file_blocks_until_released(tmp_path: Path) -> None:
    store = SpikeBusinessStore(tmp_path)
    asyncio.run(store.wait_hold_cleared())
    store.arm_hold()

    async def run() -> None:
        async def release() -> None:
            await asyncio.sleep(0.1)
            store.release_hold()

        await asyncio.gather(store.wait_hold_cleared(), release())

    asyncio.run(run())
    store.release_hold()


def test_async_settle_wait(tmp_path: Path) -> None:
    identity = public_spike_task(object_root=tmp_path, suffix="async")
    store = SpikeBusinessStore(tmp_path)

    async def run() -> None:
        async def apply() -> None:
            await asyncio.sleep(0.05)
            store.apply_leaf(identity)

        await asyncio.gather(
            store.wait_until_settled_async(identity.effect_identity.root, timeout_seconds=2.0),
            apply(),
        )
        with pytest.raises(TimeoutError, match="did not settle"):
            await store.wait_until_settled_async("effect.missing", timeout_seconds=0.12)

    asyncio.run(run())


def test_run_spike_lazy_imports_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = types.SimpleNamespace(run_both_morphologies=lambda root: {"object_root": str(root)})
    monkeypatch.setitem(sys.modules, "novel_agent.runtime.temporal_langgraph_spike", fake)
    assert run_spike(tmp_path)["object_root"] == str(tmp_path)
