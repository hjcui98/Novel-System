#!/usr/bin/env python3
"""Verify Stage 5 model-response recovery across two Python processes."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.model.fake import FakeModelEndpoint
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.model_call_ledger import SqlModelCallLedger
from novel_agent.domain.artifacts import (
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
    ProviderModelResult,
)
from novel_agent.domain.runtime import (
    AttemptOutcome,
    EffectReceipt,
    FailureClass,
    ResumabilityStatus,
    RunCheckpoint,
    TaskStatus,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.runtime_commands import RuntimeCommandService
from novel_agent.services.runtime_recovery import RuntimeRecoveryService

RUN_ID = RunId("run.cross-process.runtime-replay")
TASK_ID = TaskId("run.cross-process.runtime-replay.plan")
PROJECT_ID = ProjectId("project.test")
POLICY_HASH = ArtifactId("sha256:" + "1" * 64)
PERMISSION_HASH = "sha256:" + "2" * 64
SCHEMA_VERSION = SchemaVersion("1.0.0")
REPLAY_MEDIA_TYPE = "application/vnd.novel-agent.runtime-model-replay-evidence+json"
MANIFEST_VERSION = SchemaVersion("0.1.0")


class _Output(BaseModel):
    model_config = ConfigDict(strict=True)

    value: str


def _manifest() -> RootManifest:
    return RootManifest(
        project_id=PROJECT_ID,
        schema_version=MANIFEST_VERSION,
        text_root=TextRootRef(
            artifact_id=ArtifactId("sha256:" + "a" * 64),
            media_type="application/json",
            byte_length=10,
            schema_version=MANIFEST_VERSION,
        ),
        plan_root=PlanRootRef(
            artifact_id=ArtifactId("sha256:" + "b" * 64),
            media_type="application/json",
            byte_length=10,
            schema_version=MANIFEST_VERSION,
        ),
        world_root=WorldRootRef(
            artifact_id=ArtifactId("sha256:" + "c" * 64),
            media_type="application/json",
            byte_length=10,
            schema_version=MANIFEST_VERSION,
        ),
        reference_root=ReferenceRootRef(
            artifact_id=ArtifactId("sha256:" + "d" * 64),
            media_type="application/json",
            byte_length=10,
            schema_version=MANIFEST_VERSION,
        ),
        project_profile_root=ProjectProfileRootRef(
            artifact_id=ArtifactId("sha256:" + "e" * 64),
            media_type="application/json",
            byte_length=10,
            schema_version=MANIFEST_VERSION,
        ),
    )


class _Resolution:
    def __init__(self, receipt: EffectReceipt) -> None:
        self.receipt = receipt


class _Resolver:
    def resolve(self, receipt: EffectReceipt) -> _Resolution:
        return _Resolution(receipt)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("send", "recover"), required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--objects", type=Path, required=True)
    parser.add_argument("--provider-count", type=Path, required=True)
    parser.add_argument("--memory-count", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def _open(
    args: argparse.Namespace,
) -> tuple[sessionmaker[Session], CommitService, ArtifactRepository]:
    engine = create_engine(f"sqlite+pysqlite:///{args.database}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    return factory, CommitService(factory), ArtifactRepository(FilesystemObjectStore(args.objects))


def _policy() -> CreativeRunPolicy:
    return CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=POLICY_HASH.root,
        permission_hash=PERMISSION_HASH,
    )


def _runtime_request(base: CommitId) -> CreativeRunRequest:
    return CreativeRunRequest(
        run_id=RUN_ID,
        project_id=PROJECT_ID,
        basis_commit=base,
        policy=_policy(),
    )


def _model_request(attempt_id: StableId) -> ModelRequest:
    return ModelRequest(
        request_id=StableId(f"model-request.{TASK_ID.root}.plan_revision.1"),
        run_id=RUN_ID,
        task_id=TASK_ID,
        attempt_id=attempt_id,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace.cross-process.runtime-replay",
        prompt="Return the bounded plan revision.",
        scheduling_stage="plan_revision",
    )


def _run_memory_boundary(count_path: Path) -> dict[str, int | str]:
    """Run the deterministic Memory boundary exactly once in the sender process."""

    count = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
    count += 1
    count_path.write_text(f"{count}\n", encoding="utf-8")
    return {"memory": "already-settled", "memory_call_count": count}


def _gateway(
    factory: sessionmaker[Session], artifacts: ArtifactRepository, count_path: Path
) -> ModelGateway:
    endpoint = FakeModelEndpoint('{"value":"replayed"}')
    original_generate = endpoint.generate

    async def counted(request: ModelRequest) -> ProviderModelResult:
        count = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
        count_path.write_text(f"{count + 1}\n", encoding="utf-8")
        return await original_generate(request)

    endpoint.generate = counted  # type: ignore[method-assign]
    route = RegisteredModelEndpoint(
        role=ModelRole.BATCH_TEST,
        endpoint_name="cross-process-runtime-replay",
        model_name="cross-process-runtime-replay-v1",
        adapter=endpoint,
    )
    return ModelGateway(
        (route,),
        call_ledger=SqlModelCallLedger(factory),
        raw_artifacts=artifacts,
    )


def _send(args: argparse.Namespace) -> int:
    factory, commits, artifacts = _open(args)
    base = commits.initialize_project(_manifest())
    events = RunEventLogRepository(factory)
    commands = RuntimeCommandService(
        factory,
        events,
        lambda _project_id: PERMISSION_HASH,
        artifacts=artifacts,
    )
    task = commands.create_run_and_initial_task(_runtime_request(base))
    _, fence = commands.claim(task.task_id, worker_id="runtime-replay.sender")
    commands.mark_started(fence)
    memory_state = _run_memory_boundary(args.memory_count)
    state_ref = artifacts.put(
        json.dumps(memory_state, sort_keys=True).encode("utf-8"),
        "application/json",
        SCHEMA_VERSION,
    )
    commands.save_checkpoint(
        fence,
        RunCheckpoint(
            checkpoint_id=StableId("checkpoint.cross-process.runtime-replay"),
            run_id=RUN_ID,
            event_position=events.replay(RUN_ID)[-1].sequence_no,
            logical_stage="stage4.plan_revision",
            state_artifact_ref=state_ref,
            resumability_status=ResumabilityStatus.RESUMABLE,
        ),
    )
    gateway = _gateway(factory, artifacts, args.provider_count)
    asyncio.run(gateway.generate_structured(_model_request(fence.attempt_id), _Output))
    commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUSPENDED,
        terminal_status=TaskStatus.WAITING_RETRY,
        failure_class=FailureClass.WORKER_STARTUP,
    )
    return 37


def _recover(args: argparse.Namespace) -> int:
    factory, commits, artifacts = _open(args)
    events = RunEventLogRepository(factory)
    commands = RuntimeCommandService(
        factory,
        events,
        lambda _project_id: PERMISSION_HASH,
        artifacts=artifacts,
    )
    recovery = RuntimeRecoveryService(
        factory,
        commands,
        RunCheckpointRepository(factory),
        artifacts,
        commits,
        _Resolver(),
    )
    checkpoint, attempt, fence = recovery.resume(
        TASK_ID,
        worker_id="runtime-replay.recoverer",
        actor_id="runtime-replay-test",
        current_configuration_fingerprint=POLICY_HASH,
    )
    checkpoint_state = json.loads(artifacts.read_verified(checkpoint.state_artifact_ref))
    if checkpoint_state.get("memory_call_count") != 1:
        raise RuntimeError("recovery did not load the sender's settled Memory checkpoint")
    recovered_task = commands.get_task(TASK_ID)
    replay_refs = tuple(
        ref for ref in recovered_task.terminal_artifact_refs if ref.media_type == REPLAY_MEDIA_TYPE
    )
    if len(replay_refs) != 1:
        raise RuntimeError("recovery did not attach exactly one replay evidence artifact")
    evidence = json.loads(artifacts.read_verified(replay_refs[0]))
    response = evidence["responses"][0]
    if response["logical_phase"] != "plan_revision":
        raise RuntimeError("replay response was not bound to plan_revision")
    commands.mark_started(fence)
    calls_before = int(args.provider_count.read_text(encoding="utf-8"))
    gateway = _gateway(factory, artifacts, args.provider_count)
    parsed, _record = asyncio.run(
        gateway.generate_structured(
            _model_request(StableId(response["source_attempt_id"])),
            _Output,
        )
    )
    calls_after = int(args.provider_count.read_text(encoding="utf-8"))
    if calls_after != calls_before:
        raise RuntimeError("recovery issued a duplicate provider call")
    memory_before = int(args.memory_count.read_text(encoding="utf-8"))
    output_ref = artifacts.put(b"replayed plan output", "application/json", SCHEMA_VERSION)
    commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        artifact_refs=(output_ref,),
    )
    ledger_entry = SqlModelCallLedger(factory).load(StableId(response["request_id"]))
    if ledger_entry is None or ledger_entry.response_consumed_at is None:
        raise RuntimeError("replayed response was not consumed after durable output")
    memory_after = int(args.memory_count.read_text(encoding="utf-8"))
    if memory_before != memory_after:
        raise RuntimeError("recovery reran Memory")
    if args.output is None:
        raise RuntimeError("recover phase requires --output")
    payload = {
        "status": "recovered",
        "checkpoint_id": checkpoint.checkpoint_id.root,
        "attempt_no": attempt.attempt_no,
        "logical_phase": response["logical_phase"],
        "provider_call_count": calls_after,
        "memory_call_count": memory_after,
        "parsed": parsed.model_dump(mode="json"),
        "response_consumed": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    args = _parser().parse_args()
    args.database = args.database.resolve()
    args.objects = args.objects.resolve()
    args.provider_count = args.provider_count.resolve()
    args.memory_count = args.memory_count.resolve()
    if args.phase == "send":
        args.database.parent.mkdir(parents=True, exist_ok=True)
        args.objects.mkdir(parents=True, exist_ok=True)
        args.provider_count.parent.mkdir(parents=True, exist_ok=True)
        args.memory_count.parent.mkdir(parents=True, exist_ok=True)
        return _send(args)
    return _recover(args)


if __name__ == "__main__":
    raise SystemExit(main())
