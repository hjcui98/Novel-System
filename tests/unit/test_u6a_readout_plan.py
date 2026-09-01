"""U6-A readout lifecycle contract edges."""

from __future__ import annotations

import pytest

from novel_agent.domain.ids import StableId
from novel_agent.domain.u6_continuous_replay import (
    U6A_READOUT_LIFECYCLE,
    U6ACanaryJob,
    U6AReadoutTask,
    U6AReadoutTrack,
)


def test_readout_task_rejects_missing_or_reordered_lifecycle_step() -> None:
    with pytest.raises(ValueError, match="lifecycle is incomplete"):
        U6AReadoutTask(
            task_id=StableId("task.u6a.qa"),
            track="novelmem_qa",
            checkpoint_chapter=20,
            basis_id=StableId("basis.20"),
            source_task_id=StableId("task.v05.qa"),
            lifecycle=U6A_READOUT_LIFECYCLE[:-1],
        )


def test_canary_job_carries_the_same_lifecycle_and_evaluator_boundary() -> None:
    job = U6ACanaryJob(
        job_id=StableId("dshort-101"),
        track=U6AReadoutTrack.D_SHORT,
        checkpoint_chapter=100,
        basis_id=StableId("basis.100"),
    )
    assert job.lifecycle == U6A_READOUT_LIFECYCLE
    assert job.future_visibility == "evaluator_only"
    assert job.release_policy == "after_basis_freeze"
