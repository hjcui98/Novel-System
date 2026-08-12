from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.runtime.isolated import (
    FaultInjectionEffectStatusResolver,
    FaultInjectionWritingLeaf,
    StrictDeterministicCandidateMaterializer,
    StrictFakePlanningLeaf,
)
from novel_agent.adapters.runtime.stage3_writer import Stage3WritingLeafAdapter
from novel_agent.domain.creative_runtime import (
    AcceptedCandidateBinding,
    ActorKind,
    CandidateBinding,
    CandidateKind,
    PlanningLoopRequest,
    PlanningTerminalStatus,
)
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import CommitId, ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.runtime import EffectReceipt, EffectStatus
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.writer_context_loop import WriterContextLoopService
from novel_agent.services.writer_reactive_memory import ReactiveMemoryInputs
from tests.factories import make_manifest


def test_strict_fake_planner_has_deterministic_immutable_lineage(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path))
    request = PlanningLoopRequest(
        run_id=RunId("run.planner"),
        task_id=TaskId("task.planner"),
        project_id=ProjectId("project.test"),
        basis_commit=CommitId("sha256:" + "1" * 64),
    )
    result = asyncio.run(StrictFakePlanningLeaf(artifacts).run(request))
    repeated = asyncio.run(StrictFakePlanningLeaf(artifacts).run(request))
    assert result == repeated
    assert result.status is PlanningTerminalStatus.PLAN_CANDIDATE_READY
    assert result.candidate is not None
    artifacts.read_verified(result.candidate.artifact_ref)

    blocked = asyncio.run(
        StrictFakePlanningLeaf(artifacts, terminal=PlanningTerminalStatus.BLOCKED).run(request)
    )
    assert blocked.status is PlanningTerminalStatus.BLOCKED
    assert blocked.failure_code == "planner_blocked"


class _Loop:
    def __init__(self, result: WritingLoopResult) -> None:
        self.result = result
        self.calls: list[tuple[object, object, object]] = []

    async def execute(
        self, request: object, model_request: object, reactive_inputs: object
    ) -> WritingLoopResult:
        self.calls.append((request, model_request, reactive_inputs))
        return self.result


class _Request:
    def __init__(self, run_id: RunId, task_id: TaskId) -> None:
        self.run_id = run_id
        self.task_id = task_id


def test_real_stage3_adapter_uses_only_public_request_and_result() -> None:
    request = cast(
        WritingLoopRequest,
        _Request(RunId("run.writer"), TaskId("task.writer")),
    )
    result = WritingLoopResult(
        result_id=StableId("writer.result"),
        run_id=request.run_id,
        task_id=request.task_id,
        status=WritingLoopTerminalStatus.MODEL_UNAVAILABLE,
        failure_detail="offline endpoint unavailable",
    )
    loop = _Loop(result)
    model_request = cast(ModelRequest, object())
    reactive = cast(ReactiveMemoryInputs, object())
    adapter = Stage3WritingLeafAdapter(
        cast(WriterContextLoopService, loop),
        lambda _: model_request,
        lambda _: reactive,
    )
    assert asyncio.run(adapter.run(request)) == result
    assert loop.calls == [(request, model_request, reactive)]

    wrong = result.model_copy(update={"task_id": TaskId("task.other")})
    bad_adapter = Stage3WritingLeafAdapter(
        cast(WriterContextLoopService, _Loop(wrong)),
        lambda _: model_request,
        lambda _: reactive,
    )
    with pytest.raises(RuntimeError, match="cross-task lineage"):
        asyncio.run(bad_adapter.run(request))


def test_fault_writer_refuses_cross_task_injection() -> None:
    result = WritingLoopResult(
        result_id=StableId("writer.failed"),
        run_id=RunId("run.writer"),
        task_id=TaskId("task.writer"),
        status=WritingLoopTerminalStatus.WRITER_FAILED,
        failure_detail="injected",
    )
    leaf = FaultInjectionWritingLeaf(result)
    request = cast(WritingLoopRequest, _Request(result.run_id, result.task_id))
    assert asyncio.run(leaf.run(request)) == result
    with pytest.raises(ValueError, match="must match"):
        asyncio.run(
            leaf.run(cast(WritingLoopRequest, _Request(result.run_id, TaskId("task.other"))))
        )


def test_fault_effect_resolver_is_isolated_only_and_rejects_unresolved_inputs() -> None:
    with pytest.raises(ValueError, match="post-request"):
        FaultInjectionEffectStatusResolver(EffectStatus.REQUESTED)
    resolver = FaultInjectionEffectStatusResolver(EffectStatus.COMPLETED)
    receipt = EffectReceipt(
        effect_identity=StableId("effect.resolve"),
        external_system="provider",
        request_identity=StableId("request.resolve"),
        status=EffectStatus.REQUESTED,
        attempt_no=1,
    )
    resolution = resolver.resolve(receipt)
    assert resolution.receipt.status is EffectStatus.COMPLETED
    with pytest.raises(ValueError, match="unresolved"):
        resolver.resolve(resolution.receipt.model_copy(update={"status": EffectStatus.COMPLETED}))


def test_materializer_rejects_wrong_candidate_kind(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path))
    candidate_ref = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    accepted = AcceptedCandidateBinding(
        acceptance_id=StableId("acceptance.materialize"),
        command_id=StableId("command.materialize"),
        project_id=ProjectId("project.test"),
        run_id=RunId("run.materialize"),
        task_id=TaskId("task.materialize"),
        candidate=CandidateBinding(
            candidate_id=StableId("candidate.draft"),
            kind=CandidateKind.DRAFT,
            artifact_ref=candidate_ref,
            candidate_hash=candidate_ref.artifact_id.root,
            basis_commit=base,
        ),
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        accepted_at=datetime(2026, 8, 10, tzinfo=UTC),
        expected_project_commit=base,
    )
    materializer = StrictDeterministicCandidateMaterializer(
        commits, candidate_kind=CandidateKind.PLAN
    )
    with pytest.raises(ValueError, match="wrong candidate kind"):
        materializer.materialize(accepted)
    engine.dispose()
