"""U3.5 Temporal spike: local test server, fake identities, no production assembly."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from novel_agent.runtime.temporal_spike import SpikePayloadError, public_spike_task


def _require_temporal() -> Any:
    pytest.importorskip("temporalio")
    from novel_agent.runtime import temporal_langgraph_spike as spike

    return spike


@pytest.mark.integration
def test_temporal_spike_both_morphologies_and_payload_rejection(
    tmp_path: Path,
) -> None:
    spike = _require_temporal()
    report = asyncio.run(spike._run_both(tmp_path))
    assert report["namespace"] == report["requested_namespace"]
    wrapped = report["activity_wrapped"]
    assert wrapped["morphology"] == "activity_wrapped"
    assert wrapped["business_effect_count"] == 4
    assert wrapped["paused_resumed"] is True
    assert wrapped["duplicate_effect_count"] == 1
    assert (
        "worker_restart_hangs_on_time_skipping_test_server" not in wrapped["unsupported_conditions"]
    )
    assert wrapped["history_bytes"] > 0
    assert wrapped["activity_count"] >= 1
    plugin = report["plugin_integrated"]
    assert plugin["morphology"] == "plugin_integrated"
    if plugin["plugin_available"] and not plugin["unsupported_conditions"]:
        assert plugin["business_effect_count"] >= 1
        assert plugin["node_count"] == 2
        assert plugin["activity_count"] >= 1


@pytest.mark.integration
def test_temporal_spike_rejects_gold_payload(tmp_path: Path) -> None:
    spike = _require_temporal()

    async def run() -> None:
        from temporalio.testing import WorkflowEnvironment

        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=spike._data_converter(),
        ) as env:
            await spike.run_payload_rejection(env)

    asyncio.run(run())


@pytest.mark.integration
def test_leaf_activity_rejects_private_fields_before_store(tmp_path: Path) -> None:
    spike = _require_temporal()
    task = public_spike_task(object_root=tmp_path).model_dump(mode="json")
    task["gold"] = "secret"

    async def run() -> None:
        with pytest.raises(SpikePayloadError, match="forbids field gold"):
            await spike.run_leaf_activity(task)

    asyncio.run(run())
