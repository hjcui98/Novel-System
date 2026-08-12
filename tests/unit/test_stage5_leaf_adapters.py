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
from novel_agent.adapters.runtime.stage4_planner import (
    Stage4PlanningInvocation,
    Stage4PlanningLeafAdapter,
)
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
from novel_agent.domain.planning import (
    PlanningLoopRequest as Stage4PlanningLoopRequest,
)
from novel_agent.domain.planning import (
    PlanningLoopResult as Stage4PlanningLoopResult,
)
from novel_agent.domain.planning import (
    PlanningLoopTerminal as Stage4PlanningLoopTerminal,
)
from novel_agent.domain.runtime import EffectReceipt, EffectStatus
from novel_agent.domain.stage2 import AgentMode, PlanningTask, PlanProposal
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.planning_context_loop import PlanningContextLoopService
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


def test_real_stage4_adapter_preserves_candidate_and_review_lineage(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "stage4"))
    author_ref = artifacts.put(b"author intent", "text/plain", SchemaVersion("1.0.0"))
    review_ref = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    simple = PlanningLoopRequest(
        run_id=RunId("run.stage4-adapter"),
        task_id=TaskId("task.stage4-adapter"),
        project_id=ProjectId("project.test"),
        basis_commit=CommitId("sha256:" + "1" * 64),
        basis_snapshot=StableId("snapshot.stage4-adapter"),
        input_artifact_refs=(author_ref,),
    )
    task = PlanningTask.model_construct(
        task_id=StableId(simple.task_id.root),
        project_id=simple.project_id,
        mode=AgentMode.CHAPTER,
        base_commit=simple.basis_commit,
        source_ids=(StableId("source.author"),),
    )
    detailed = Stage4PlanningLoopRequest.model_construct(
        request_id=StableId("request.stage4-adapter"),
        run_id=simple.run_id,
        task_id=simple.task_id,
        project_id=simple.project_id,
        task=task,
        author_intent_artifacts=(author_ref,),
        snapshot_id=simple.basis_snapshot,
    )
    proposal = PlanProposal.model_construct(
        proposal_id=StableId("proposal.stage4-adapter"),
        project_id=simple.project_id,
        mode=AgentMode.CHAPTER,
        base_commit=simple.basis_commit,
        items=(),
        unresolved=(),
        coverage=1.0,
        receipt={},
    )
    stage4_result = Stage4PlanningLoopResult.model_construct(
        request_id=detailed.request_id,
        terminal=Stage4PlanningLoopTerminal.PLAN_CANDIDATE_READY,
        proposal=proposal,
        plan_review_ref=review_ref,
        event_artifacts=(review_ref,),
        diagnostic_codes=(),
        degraded=False,
    )

    class _Stage4Loop:
        async def run(self, **_: object) -> Stage4PlanningLoopResult:
            return stage4_result

    adapter = Stage4PlanningLeafAdapter(
        cast(PlanningContextLoopService, _Stage4Loop()),
        artifacts,
        lambda _: Stage4PlanningInvocation(
            request=detailed,
            model_request=lambda _phase, _mode, _attempt: cast(ModelRequest, object()),
        ),
        schema_version=SchemaVersion("1.0.0"),
    )
    result = asyncio.run(adapter.run(simple))
    assert result.status is PlanningTerminalStatus.PLAN_CANDIDATE_READY
    assert result.candidate is not None
    assert result.candidate.basis_commit == simple.basis_commit
    assert review_ref in result.candidate.lineage_artifact_refs
    assert artifacts.read_verified(result.candidate.artifact_ref)


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
