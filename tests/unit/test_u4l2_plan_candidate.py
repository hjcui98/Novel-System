from __future__ import annotations

import json
from pathlib import Path

import scripts.run_u4l2_plan_candidate as candidate_runner
from pytest import MonkeyPatch
from scripts.run_u4l2_plan_candidate import (
    _copy_resume_checkpoint,
    _copy_runtime_context_artifacts,
)

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId
from novel_agent.domain.planning import PlanningLoopCheckpoint, PlanningLoopPhase
from novel_agent.domain.runtime import ResumabilityStatus, RunCheckpoint
from novel_agent.services.artifacts import ArtifactRepository

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "a" * 64)


class _Event:
    def __init__(self, artifact_refs: tuple[object, ...]) -> None:
        self.artifact_refs = artifact_refs


class _EventRepository:
    def __init__(self, session_factory: object) -> None:
        del session_factory

    def replay(self, run_id: RunId) -> tuple[_Event, ...]:
        del run_id
        return (_Event((self.event_ref,)),)

    event_ref: ArtifactRef | None = None


class _CheckpointRepository:
    checkpoint: RunCheckpoint | None = None

    def __init__(self, session_factory: object) -> None:
        del session_factory

    def latest(self, run_id: RunId) -> RunCheckpoint | None:
        del run_id
        return self.checkpoint


def test_resume_checkpoint_copies_lineage_artifacts_without_rebinding(tmp_path: Path) -> None:
    source = ArtifactRepository(FilesystemObjectStore(tmp_path / "source" / "objects"))
    destination = ArtifactRepository(FilesystemObjectStore(tmp_path / "destination" / "objects"))
    inquiry = source.put(b"inquiry", "application/json", VERSION)
    review = source.put(b"review", "application/json", VERSION)
    nested = source.put(b"nested", "application/json", VERSION)
    planner_context = source.put(
        json.dumps({"state_artifact_ref": nested.model_dump(mode="json")}).encode(),
        "application/json",
        VERSION,
    )
    checkpoint = PlanningLoopCheckpoint(
        checkpoint_id=StableId("checkpoint.resume"),
        request_id=StableId("planning-request.task.resume"),
        phase=PlanningLoopPhase.CONTEXT_READY,
        base_commit=CommitId("sha256:" + "b" * 64),
        snapshot_id=StableId("snapshot.resume"),
        configuration_fingerprint=HASH,
        inquiry_ref=inquiry,
        inquiry_review_ref=review,
        planner_context_ref=planner_context,
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(checkpoint.model_dump_json(), encoding="utf-8")

    copied_checkpoint, loaded = _copy_resume_checkpoint(checkpoint_path, source, destination)

    assert loaded == checkpoint
    assert destination.read_verified(inquiry) == b"inquiry"
    assert destination.read_verified(review) == b"review"
    assert destination.read_verified(planner_context)
    assert destination.read_verified(nested) == b"nested"
    assert destination.read_verified(copied_checkpoint) == checkpoint_path.read_bytes()


def test_resume_copies_runtime_event_and_checkpoint_artifacts(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source = ArtifactRepository(FilesystemObjectStore(tmp_path / "source" / "objects"))
    destination = ArtifactRepository(FilesystemObjectStore(tmp_path / "destination" / "objects"))
    event_ref = source.put(b"context seed", "application/json", VERSION)
    nested_state_ref = source.put(b"nested context state", "application/json", VERSION)
    state_ref = source.put(
        json.dumps({"nested_state_ref": nested_state_ref.model_dump(mode="json")}).encode(),
        "application/json",
        VERSION,
    )
    _EventRepository.event_ref = event_ref
    _CheckpointRepository.checkpoint = RunCheckpoint(
        checkpoint_id=StableId("checkpoint.runtime"),
        run_id=RunId("run.runtime"),
        event_position=1,
        logical_stage="stage3.context:planner:task.runtime",
        state_artifact_ref=state_ref,
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    monkeypatch.setattr(candidate_runner, "RunEventLogRepository", _EventRepository)
    monkeypatch.setattr(candidate_runner, "RunCheckpointRepository", _CheckpointRepository)

    copied = _copy_runtime_context_artifacts(
        RunId("run.runtime"),
        object(),
        source,
        destination,
    )

    assert {ref.artifact_id for ref in copied} == {
        event_ref.artifact_id,
        state_ref.artifact_id,
        nested_state_ref.artifact_id,
    }
    assert destination.read_verified(event_ref) == b"context seed"
    assert destination.read_verified(state_ref)
    assert destination.read_verified(nested_state_ref) == b"nested context state"
