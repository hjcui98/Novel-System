#!/usr/bin/env python3
"""Run the U8-B5 cold-process recovery matrix.

This harness deliberately uses the existing ``LocalMemoryWriteWorkflow``,
``CommitService`` and filesystem artifacts.  A child process is killed at one
durable boundary, then a fresh child reconstructs the workflow from the
checkpoint/artifacts and resumes it.  The matrix is intentionally small: it
proves idempotent acceptance/Commit/projection recovery, not creative quality
or model routing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
from pathlib import Path
from typing import Any

from sqlalchemy import event, func, select, text

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.memory_write import CommitServiceMemoryWriteAdapter
from novel_agent.adapters.postgres.database import Base, build_engine, build_session_factory
from novel_agent.adapters.postgres.models import (
    CommitReceiptRow,
    CommitRow,
    RuntimeTaskProjectionRow,
)
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootKind,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.changes import (
    ChangeOperation,
    ChangeOperationType,
    ObservedChangeSet,
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
from novel_agent.domain.memory import FreshnessDecision, FreshnessStatus, WorldRootDocument
from novel_agent.domain.memory_write import (
    CanonicalWriteBasis,
    InformationBoundary,
    MaintenanceTrigger,
    MemoryGapClassification,
    MemoryRepairFinding,
    MemoryRepairOwner,
    MemoryWriteCandidatePayload,
    MemoryWriteCommitProfile,
    MemoryWriteWorkflowRequest,
    MemoryWriteWorkflowResult,
    NarrativePosition,
    ProjectionReadinessResult,
    ProjectionReadinessStatus,
    RepairScope,
    TrustedWorldCandidateInput,
)
from novel_agent.domain.runtime import AttemptFence, TaskKind, TaskPurpose, TaskRecord, TaskStatus
from novel_agent.domain.stage2 import AccessScope, ContractRef
from novel_agent.domain.world import Entity, RelationRecord, StoryTime, TruthClass
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.information_boundary import InformationBoundaryViolation
from novel_agent.services.memory_write_validation import Stage2ValidationV2Adapter
from novel_agent.services.memory_write_workflow import (
    InMemoryCandidateLineageRepository,
    InMemoryCheckpointRepository,
    InMemoryRunEventSink,
    LocalMemoryWriteWorkflow,
    StaticCanonicalReadPort,
)
from novel_agent.services.root_update_materializer import RootUpdateMaterializer
from novel_agent.services.runtime_commands import RuntimeCommandService

VERSION = SchemaVersion("0.1.0")
PROJECT = ProjectId("u8b.process-kill.matrix")
RUN = RunId("u8b.process-kill.matrix.run")
POLICY_HASH = ArtifactId("sha256:" + "1" * 64)
MEDIA_WORLD = "application/vnd.novel-agent.world-root+json"
MEDIA_CANDIDATE = "application/vnd.novel-agent.memory-write-candidate+json"
MEDIA_RECEIPT = "application/vnd.novel-agent.boundary-propagation-receipt+json"
MEMORY_WRITE_RESULT_MEDIA_TYPE = "application/vnd.novel-agent.memory-write-workflow-result+json"


def _case_schema(output_root: Path, case_name: str, database_url: str | None) -> str | None:
    if database_url is None:
        return None
    if not database_url.startswith(("postgresql+psycopg://", "postgresql://")):
        raise ValueError("--database-url must be a PostgreSQL SQLAlchemy URL")
    prefix = re.sub(r"[^a-zA-Z0-9_]+", "_", output_root.name).strip("_") or "run"
    suffix = re.sub(r"[^a-zA-Z0-9_]+", "_", case_name).strip("_") or "case"
    # PostgreSQL identifiers are bounded to 63 bytes.  The output root and
    # case name are operator-selected identities, so refusing a collision is
    # safer than truncating into an existing schema.
    schema = f"u8bpk_{prefix}_{suffix}"
    if len(schema) > 63:
        raise ValueError(f"generated PostgreSQL schema name is too long: {schema}")
    return schema


def _build_case_engine(
    case_root: Path,
    *,
    database_url: str | None,
    schema: str | None,
    create_schema: bool = True,
) -> Any:
    if database_url is None:
        return build_engine(f"sqlite+pysqlite:///{case_root / 'matrix.sqlite'}")
    if schema is None:  # pragma: no cover - guarded by _case_schema
        raise ValueError("PostgreSQL case engine requires a schema")
    if create_schema:
        bootstrap = build_engine(database_url)
        with bootstrap.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        bootstrap.dispose()
    engine = build_engine(database_url)

    @event.listens_for(engine, "checkout")
    def _set_case_search_path(dbapi_connection: Any, _: Any, __: Any) -> None:
        cursor = dbapi_connection.cursor()
        # Set the schema on every pool checkout.  This keeps fresh parent and
        # child-process Sessions isolated even when a driver/pool reset
        # restores the connection's default search path on return.
        cursor.execute(f'SET SESSION search_path TO "{schema}"')
        cursor.close()

    return engine


def _write_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"refusing to overwrite matrix artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _write_replace(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    os.replace(temporary, path)


class _Boundary:
    """Test-only boundary owner; basis and producer artifacts remain typed."""

    def verify_request_and_derivation_graph(
        self, request: MemoryWriteWorkflowRequest, basis: CanonicalWriteBasis
    ) -> None:
        if request.project_id != basis.project_id or request.base_commit != basis.commit_id:
            raise InformationBoundaryViolation("matrix request basis mismatch")

    def verify_derivation_chain(self, **_: object) -> None:
        return None


class _Fault:
    def __init__(self, marker: Path, point: str | None) -> None:
        self.marker = marker
        self.point = point

    def hit(self, point: str, data: Any) -> None:
        checkpoint = getattr(data, "checkpoint_ref", None)
        _write_replace(
            self.marker,
            {
                "last_point": point,
                "checkpoint_ref": (
                    None if checkpoint is None else checkpoint.model_dump(mode="json")
                ),
            },
        )
        if point == self.point:
            os.kill(os.getpid(), signal.SIGKILL)


class _KillAfterCommit:
    def __init__(self, inner: CommitServiceMemoryWriteAdapter, marker: Path) -> None:
        self._inner = inner
        self._marker = marker

    def resolve_or_replay_exact(self, request: Any) -> Any:
        self._inner.resolve_or_replay_exact(request)
        latest: dict[str, Any] = {}
        if self._marker.exists():
            latest = json.loads(self._marker.read_text())
        _write_replace(
            self._marker,
            {
                **latest,
                "commit_effect_returned": True,
                "commit_request_id": request.request_id.root,
            },
        )
        os.kill(os.getpid(), signal.SIGKILL)


class _DurableProjection:
    """Filesystem-idempotent projection port used by the cold restart proof."""

    def __init__(self, root: Path, artifacts: ArtifactRepository) -> None:
        self._root = root
        self._artifacts = artifacts

    def request_or_read_by_effect_id(
        self, project_id: ProjectId, commit_id: CommitId, effect_id: StableId
    ) -> ProjectionReadinessResult:
        path = self._root / "projection" / f"{effect_id.root}.json"
        if path.exists():
            return ProjectionReadinessResult.model_validate_json(path.read_bytes(), strict=False)
        snapshot_id = StableId(f"snapshot.{commit_id.root.removeprefix('sha256:')[:32]}")
        freshness = FreshnessDecision(
            status=FreshnessStatus.READY,
            canonical_commit=commit_id,
            r1_basis_commit=commit_id,
            required_snapshot_id=snapshot_id,
            actual_alias_commit=commit_id,
            actual_snapshot_id=snapshot_id,
            actual_snapshot_commit=commit_id,
            reason="matrix exact snapshot ready",
        )
        projection_ref = self._artifacts.put(
            canonical_json_bytes(
                {
                    "project_id": project_id.root,
                    "commit_id": commit_id.root,
                    "effect": effect_id.root,
                }
            ),
            "application/vnd.novel-agent.projection-receipt+json",
            VERSION,
        )
        freshness_ref = self._artifacts.put(
            canonical_json_bytes(freshness.model_dump(mode="json")),
            "application/vnd.novel-agent.freshness-receipt+json",
            VERSION,
        )
        result = ProjectionReadinessResult(
            effect_id=effect_id,
            status=ProjectionReadinessStatus.READY,
            projection_receipt_ref=projection_ref,
            freshness_receipt_ref=freshness_ref,
            projection_snapshot_id=snapshot_id,
            freshness=freshness,
        )
        _write_once(path, result.model_dump(mode="json"))
        return result

    def await_or_check(
        self, project_id: ProjectId, commit_id: CommitId, effect_id: StableId
    ) -> ProjectionReadinessResult:
        return self.request_or_read_by_effect_id(project_id, commit_id, effect_id)


def _ref(digest: str, media_type: str = "application/json") -> Any:
    from novel_agent.domain.artifacts import ArtifactRef

    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + digest * 64),
        media_type=media_type,
        byte_length=1,
        schema_version=VERSION,
    )


def _setup_case(
    case_root: Path,
    *,
    database_url: str | None,
    schema: str | None,
) -> dict[str, Any]:
    object_root = case_root / "objects"
    artifacts = ArtifactRepository(FilesystemObjectStore(object_root))
    engine = _build_case_engine(case_root, database_url=database_url, schema=schema)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)

    world = WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "0" * 64),
        schema_version=VERSION,
        source_commit=CommitId("sha256:" + "0" * 64),
        entities=(
            Entity(entity_id=StableId("entity.chen"), entity_type="person", internal_label="Chen"),
            Entity(
                entity_id=StableId("entity.academy"),
                entity_type="location",
                internal_label="Academy",
            ),
        ),
    )
    world_ref = artifacts.put(
        canonical_json_bytes(world.model_dump(mode="json")), MEDIA_WORLD, VERSION
    )
    manifest = RootManifest(
        project_id=PROJECT,
        schema_version=VERSION,
        text_root=TextRootRef.model_validate(_ref("2").model_dump()),
        plan_root=PlanRootRef.model_validate(_ref("3").model_dump()),
        world_root=WorldRootRef.model_validate(world_ref.model_dump()),
        reference_root=ReferenceRootRef.model_validate(_ref("4").model_dump()),
        project_profile_root=ProjectProfileRootRef.model_validate(_ref("5").model_dump()),
    )
    base = commits.initialize_project(manifest)
    operation = ChangeOperation(
        operation_id=StableId("change.relation.matrix"),
        root_kind=RootKind.WORLD,
        operation=ChangeOperationType.CREATE,
        target_id=StableId("relation.matrix"),
        payload={
            "record_type": "relation",
            "record": RelationRecord(
                relation_id=StableId("relation.matrix"),
                predicate="located_at",
                subject_id=StableId("entity.chen"),
                object_id=StableId("entity.academy"),
                valid_time=StoryTime(worldline="main", start_ordinal=1, end_ordinal=1),
                truth_class=TruthClass.ACCEPTED_WORLD_FACT,
            ).model_dump(mode="json"),
        },
    )
    source_ref = artifacts.put(b"matrix-source", "application/octet-stream", VERSION)
    changes = ObservedChangeSet(
        change_set_id=StableId("changes.matrix"),
        base_commit=base,
        source_artifact=source_ref,
        operations=(operation,),
    )
    payload = MemoryWriteCandidatePayload(
        observed_changes=changes,
        root_update_intents=(),
        commit_profile=MemoryWriteCommitProfile.REQUIRE_CANONICAL_COMMIT,
    )
    candidate_ref = artifacts.put(
        canonical_json_bytes(payload.model_dump(mode="json")), MEDIA_CANDIDATE, VERSION
    )
    producer_ref = artifacts.put(b"matrix-producer", MEDIA_RECEIPT, VERSION)
    boundary = InformationBoundary(
        boundary_id=StableId("boundary.matrix"),
        base_commit=base,
        maximum_visible_position=NarrativePosition(chapter_index=1),
        evaluator_sources_forbidden=True,
        policy_ref=ContractRef(
            contract_id=StableId("policy.matrix.boundary"),
            version=VERSION,
            content_hash=POLICY_HASH,
        ),
    )
    request = MemoryWriteWorkflowRequest(
        request_id=StableId("memory-write.matrix"),
        run_id=RUN,
        task_id=TaskId("maintenance.matrix"),
        project_id=PROJECT,
        trigger=MaintenanceTrigger(
            maintenance_task_id=StableId("maintenance.matrix"), chapter_indices=(1,)
        ),
        commit_profile=MemoryWriteCommitProfile.REQUIRE_CANONICAL_COMMIT,
        base_commit=base,
        world_mutation=TrustedWorldCandidateInput(
            candidate_artifact=candidate_ref,
            producer_receipt=producer_ref,
        ),
        canonical_root_refs=manifest,
        information_boundary=boundary,
        access_scope=AccessScope.WRITER_SAFE,
        configuration_fingerprint=POLICY_HASH,
        tool_policy_ref=boundary.policy_ref,
        repair_policy_ref=boundary.policy_ref,
        idempotency_key=StableId("idempotency.matrix"),
    )
    state = case_root / "state.json"
    _write_once(
        state,
        {
            "base_commit": base.root,
            "manifest": manifest.model_dump(mode="json"),
            "world": world.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
        },
    )
    engine.dispose()
    return {
        "database_url": database_url,
        "schema": schema,
        "object_root": object_root,
        "state": state,
        "base_commit": base,
    }


def _derived_maintenance_task(
    planner: TaskRecord, finding: MemoryRepairFinding, finding_ref: ArtifactRef
) -> TaskRecord:
    return TaskRecord(
        task_id=RuntimeCommandService._maintenance_task_id(finding),
        run_id=planner.run_id,
        project_id=planner.project_id,
        kind=TaskKind.MAINTENANCE,
        purpose=TaskPurpose.DERIVED_MAINTENANCE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=planner.basis_commit,
        basis_snapshot=planner.basis_snapshot,
        policy_hash=planner.policy_hash,
        permission_hash=planner.permission_hash,
        input_artifact_refs=(finding_ref,),
        failure_budget=planner.failure_budget,
        retry_tranche_size=planner.retry_tranche_size,
        chapter_index=planner.chapter_index,
        target_chapters=planner.target_chapters,
        horizon_start=planner.horizon_start,
        horizon_end=planner.horizon_end,
        protected_chapter_index=planner.protected_chapter_index,
    )


def _setup_retry_case(
    case_root: Path,
    *,
    database_url: str | None,
    schema: str | None,
) -> dict[str, Any]:
    setup = _setup_case(case_root, database_url=database_url, schema=schema)
    engine = _build_case_engine(
        case_root,
        database_url=database_url,
        schema=schema,
        create_schema=False,
    )
    factory = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(case_root / "objects"))
    commands = RuntimeCommandService(
        factory,
        RunEventLogRepository(factory),
        lambda _project_id: POLICY_HASH.root,
        artifacts=artifacts,
    )
    planner = commands.create_run_and_initial_task(
        CreativeRunRequest(
            run_id=RunId("u8b.process-kill.matrix.planner"),
            project_id=PROJECT,
            basis_commit=setup["base_commit"],
            basis_snapshot=StableId("snapshot.matrix.base"),
            policy=CreativeRunPolicy(
                automation_mode=AutomationMode.MANUAL,
                policy_hash=POLICY_HASH.root,
                permission_hash=POLICY_HASH.root,
            ),
        )
    )
    _planner_attempt, planner_fence = commands.claim(planner.task_id, worker_id="planner")
    commands.mark_started(planner_fence)
    finding = MemoryRepairFinding(
        finding_id=StableId("finding.u8b.process-kill"),
        incident_id=StableId("incident.u8b.process-kill"),
        planner_run_id=planner.run_id,
        planner_task_id=planner.task_id,
        planner_attempt_id=planner_fence.attempt_id,
        planner_request_id=StableId("request.u8b.process-kill.planner"),
        planner_intent_ref=_ref("6"),
        planner_checkpoint_ref=_ref("7"),
        project_id=PROJECT,
        base_commit=setup["base_commit"],
        basis_snapshot_id=planner.basis_snapshot,
        information_boundary=InformationBoundary(
            boundary_id=StableId("boundary.u8b.process-kill"),
            base_commit=setup["base_commit"],
            maximum_visible_position=NarrativePosition(chapter_index=1),
            evaluator_sources_forbidden=True,
            policy_ref=ContractRef(
                contract_id=StableId("policy.u8b.process-kill"),
                version=VERSION,
                content_hash=POLICY_HASH,
            ),
        ),
        cutoff=NarrativePosition(chapter_index=1),
        access_scope=AccessScope.WRITER_SAFE,
        need_id=StableId("need.u8b.process-kill"),
        need_query="which relation is missing from the canonical world graph?",
        semantic_question="which visible source supports the missing relation?",
        classification=MemoryGapClassification.CANON_EXTRACTION_GAP,
        repair_owner=MemoryRepairOwner.GRAPH_CURATOR,
        target_root_kind=RootKind.WORLD,
        repair_scope=RepairScope(field_paths=("world.relations",)),
        no_progress_key=StableId("progress.u8b.process-kill"),
    )
    finding_ref = artifacts.put(
        canonical_json_bytes(finding.model_dump(mode="json")),
        "application/vnd.novel-agent.memory-repair-finding+json",
        VERSION,
    )
    maintenance = _derived_maintenance_task(planner, finding, finding_ref)
    commands.settle_gap_and_create_maintenance(
        planner_fence,
        finding_ref=finding_ref,
        maintenance_task=maintenance,
    )
    _maintenance_attempt, maintenance_fence = commands.claim(
        maintenance.task_id, worker_id="curator"
    )
    commands.mark_started(maintenance_fence)
    retry_state = case_root / "retry-state.json"
    _write_once(
        retry_state,
        {
            "finding_ref": finding_ref.model_dump(mode="json"),
            "planner_task_id": planner.task_id.root,
            "maintenance_task_id": maintenance.task_id.root,
            "maintenance_fence": maintenance_fence.model_dump(mode="json"),
        },
    )
    engine.dispose()
    return {**setup, "retry_state": retry_state}


def _workflow_for(
    case_root: Path,
    *,
    point: str | None,
    commit_kill: bool,
    resume: ArtifactRef | None,
    database_url: str | None,
    schema: str | None,
) -> tuple[MemoryWriteWorkflowRequest, LocalMemoryWriteWorkflow, Any]:
    state = json.loads((case_root / "state.json").read_text())
    request = MemoryWriteWorkflowRequest.model_validate(state["request"], strict=False)
    if resume is not None:
        request = request.model_copy(update={"resume_checkpoint": resume})
    engine = _build_case_engine(
        case_root,
        database_url=database_url,
        schema=schema,
        create_schema=False,
    )
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(case_root / "objects"))
    commits = CommitService(factory)
    manifest = RootManifest.model_validate(state["manifest"], strict=False)
    world = WorldRootDocument.model_validate(state["world"], strict=False)
    basis = CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=request.base_commit,
        root_manifest=manifest,
        canonical_world=world,
    )
    workflow = LocalMemoryWriteWorkflow(
        canonical_read=StaticCanonicalReadPort(basis),
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
        checkpoint=InMemoryCheckpointRepository(artifacts),
        events=InMemoryRunEventSink(),
        information_boundary=_Boundary(),
        root_updates=RootUpdateMaterializer(
            payload_loader=lambda ref: MemoryWriteCandidatePayload.model_validate_json(
                artifacts.read_verified(ref), strict=False
            ),
            artifact_writer=artifacts,
        ),
        validator=Stage2ValidationV2Adapter(),
        projection=_DurableProjection(case_root, artifacts),
        commit=(
            _KillAfterCommit(
                CommitServiceMemoryWriteAdapter(commits, artifacts), case_root / "fault.json"
            )
            if commit_kill
            else CommitServiceMemoryWriteAdapter(commits, artifacts)
        ),
        fault_injector=(
            _Fault(case_root / "fault.json", point) if point is not None or commit_kill else None
        ),
    )
    # Attach the running workflow to the request through a short-lived child
    # registry.  Keeping construction here makes the fresh process own all
    # in-memory caches and avoids accidentally testing the parent's objects.
    return request, workflow, engine


def _child(
    case_root: Path,
    *,
    point: str | None,
    commit_kill: bool,
    resume: ArtifactRef | None,
    database_url: str | None,
    schema: str | None,
) -> None:
    request, workflow, engine = _workflow_for(
        case_root,
        point=point,
        commit_kill=commit_kill,
        resume=resume,
        database_url=database_url,
        schema=schema,
    )
    try:
        import asyncio

        result = asyncio.run(workflow.execute(request))
        _write_once(case_root / "result.json", result.model_dump(mode="json"))
    finally:
        engine.dispose()


def _run_child(
    case_root: Path,
    *,
    point: str | None,
    commit_kill: bool,
    resume: ArtifactRef | None,
    database_url: str | None,
    schema: str | None,
) -> int:
    pid = os.fork()
    if pid == 0:
        try:
            _child(
                case_root,
                point=point,
                commit_kill=commit_kill,
                resume=resume,
                database_url=database_url,
                schema=schema,
            )
        except BaseException as error:
            _write_replace(
                case_root / "child-error.json",
                {"type": type(error).__name__, "message": str(error)},
            )
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def _retry_first_child(case_root: Path, *, database_url: str | None, schema: str | None) -> None:
    """Complete the accepted repair, then die immediately before retry-create."""

    request, workflow, engine = _workflow_for(
        case_root,
        point=None,
        commit_kill=False,
        resume=None,
        database_url=database_url,
        schema=schema,
    )
    try:
        import asyncio

        result = asyncio.run(workflow.execute(request))
        if result.status.value != "committed" or result.resulting_commit is None:
            raise RuntimeError("retry-create precondition did not produce a committed repair")
        artifacts = ArtifactRepository(FilesystemObjectStore(case_root / "objects"))
        result_ref = artifacts.put(
            canonical_json_bytes(result.model_dump(mode="json")),
            MEMORY_WRITE_RESULT_MEDIA_TYPE,
            VERSION,
        )
        factory = build_session_factory(engine)
        commands = RuntimeCommandService(
            factory,
            RunEventLogRepository(factory),
            lambda _project_id: POLICY_HASH.root,
            artifacts=artifacts,
        )
        retry_state = json.loads((case_root / "retry-state.json").read_text())
        planner = commands.get_task(TaskId(retry_state["planner_task_id"]))
        maintenance_id = TaskId(retry_state["maintenance_task_id"])
        maintenance_fence = AttemptFence.model_validate(
            retry_state["maintenance_fence"], strict=False
        )
        retry = planner.model_copy(
            update={
                "task_id": RuntimeCommandService._planner_retry_task_id(planner, result),
                "task_revision": 0,
                "status": TaskStatus.READY,
                "basis_commit": result.resulting_commit,
                "basis_snapshot": result.projection_snapshot_id,
                "dependency_task_ids": (maintenance_id,),
                "terminal_artifact_refs": (),
                "block_cause": None,
                "superseded": False,
            }
        )
        _write_replace(
            case_root / "retry-fault.json",
            {
                "boundary": "retry_create_before",
                "workflow_result_ref": result_ref.model_dump(mode="json"),
                "maintenance_fence": maintenance_fence.model_dump(mode="json"),
                "retry_task": retry.model_dump(mode="json"),
            },
        )
        os.kill(os.getpid(), signal.SIGKILL)
    finally:
        engine.dispose()


def _retry_resume_child(case_root: Path, *, database_url: str | None, schema: str | None) -> None:
    """Rebuild RuntimeCommandService and settle the retry atomically."""

    marker = json.loads((case_root / "retry-fault.json").read_text())
    engine = _build_case_engine(
        case_root,
        database_url=database_url,
        schema=schema,
        create_schema=False,
    )
    try:
        factory = build_session_factory(engine)
        artifacts = ArtifactRepository(FilesystemObjectStore(case_root / "objects"))
        commands = RuntimeCommandService(
            factory,
            RunEventLogRepository(factory),
            lambda _project_id: POLICY_HASH.root,
            artifacts=artifacts,
        )
        result_ref = ArtifactRef.model_validate(marker["workflow_result_ref"], strict=False)
        maintenance_fence = AttemptFence.model_validate(marker["maintenance_fence"], strict=False)
        retry = TaskRecord.model_validate(marker["retry_task"], strict=False)
        settled = commands.settle_maintenance_and_retry_planner(
            maintenance_fence,
            workflow_result_ref=result_ref,
            retry_task=retry,
        )
        _write_once(case_root / "result.json", settled.model_dump(mode="json"))
    finally:
        engine.dispose()


def _run_retry_child(
    case_root: Path,
    *,
    resume: bool,
    database_url: str | None,
    schema: str | None,
) -> int:
    pid = os.fork()
    if pid == 0:
        try:
            if resume:
                _retry_resume_child(case_root, database_url=database_url, schema=schema)
            else:
                _retry_first_child(case_root, database_url=database_url, schema=schema)
        except BaseException as error:
            _write_replace(
                case_root / "child-error.json",
                {"type": type(error).__name__, "message": str(error)},
            )
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def _run_retry_create_case(
    case_root: Path,
    *,
    database_url: str | None,
    schema: str | None,
) -> dict[str, Any]:
    setup = _setup_retry_case(case_root, database_url=database_url, schema=schema)
    first_exit = _run_retry_child(
        case_root,
        resume=False,
        database_url=database_url,
        schema=schema,
    )
    marker = json.loads((case_root / "retry-fault.json").read_text())
    resume_exit = _run_retry_child(
        case_root,
        resume=True,
        database_url=database_url,
        schema=schema,
    )
    retry = TaskRecord.model_validate_json((case_root / "result.json").read_bytes(), strict=False)
    retry_state = json.loads((case_root / "retry-state.json").read_text())
    engine = _build_case_engine(
        case_root,
        database_url=database_url,
        schema=schema,
        create_schema=False,
    )
    try:
        factory = build_session_factory(engine)
        commands = RuntimeCommandService(
            factory,
            RunEventLogRepository(factory),
            lambda _project_id: POLICY_HASH.root,
        )
        planner = commands.get_task(TaskId(retry_state["planner_task_id"]))
        maintenance = commands.get_task(TaskId(retry_state["maintenance_task_id"]))
    finally:
        engine.dispose()
    commit_count, receipt_count, retry_count = _database_counts(
        case_root,
        database_url=database_url,
        schema=schema,
    )
    passed = (
        first_exit == -signal.SIGKILL
        and resume_exit == 0
        and retry.status is TaskStatus.READY
        and retry.basis_commit != setup["base_commit"]
        and retry_count == 1
        and planner.status is TaskStatus.CANCELLED
        and planner.superseded
        and maintenance.status is TaskStatus.SUCCEEDED
        and commit_count == 2
        and receipt_count == 1
        and len(tuple((case_root / "projection").glob("*.json"))) == 1
    )
    evidence = {
        "case": "retry_create_before",
        "fault_point": marker["boundary"],
        "first_exit": first_exit,
        "resume_exit": resume_exit,
        "workflow_result_ref": marker["workflow_result_ref"],
        "retry_task_id": retry.task_id.root,
        "retry_basis_commit": retry.basis_commit.root,
        "retry_basis_snapshot": (
            None if retry.basis_snapshot is None else retry.basis_snapshot.root
        ),
        "retry_task_status": retry.status.value,
        "retry_task_count": retry_count,
        "planner_status": planner.status.value,
        "planner_superseded": planner.superseded,
        "maintenance_status": maintenance.status.value,
        "commit_rows": commit_count,
        "commit_receipts": receipt_count,
        "projection_effect_files": len(tuple((case_root / "projection").glob("*.json"))),
        "pass": passed,
    }
    _write_once(case_root / "evidence.json", evidence)
    return evidence


def _database_counts(
    case_root: Path,
    *,
    database_url: str | None,
    schema: str | None,
) -> tuple[int, int, int]:
    engine = _build_case_engine(
        case_root,
        database_url=database_url,
        schema=schema,
        create_schema=False,
    )
    try:
        factory = build_session_factory(engine)
        with factory() as session:
            commits = int(
                session.scalar(
                    select(func.count())
                    .select_from(CommitRow)
                    .where(CommitRow.project_id == PROJECT.root)
                )
                or 0
            )
            receipts = int(
                session.scalar(
                    select(func.count())
                    .select_from(CommitReceiptRow)
                    .where(CommitReceiptRow.project_id == PROJECT.root)
                )
                or 0
            )
            retries = int(
                session.scalar(
                    select(func.count())
                    .select_from(RuntimeTaskProjectionRow)
                    .where(RuntimeTaskProjectionRow.task_id.like("%retry.%"))
                )
                or 0
            )
            return commits, receipts, retries
    finally:
        engine.dispose()


def run_matrix(output_root: Path, *, database_url: str | None = None) -> dict[str, Any]:
    if output_root.exists() and any(output_root.iterdir()):
        raise RuntimeError(f"U8-B5 process-kill matrix refuses to reuse output root: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    schedule = (
        ("candidate_validation", "candidate_validation_committed", False),
        ("acceptance_before_commit", "commit_request_checkpoint_committed", False),
        ("acceptance_after_commit", "commit_accepted_before_checkpoint", False),
        ("commit_uncertain", None, True),
        ("projection_before", "projection_before_request", False),
        ("projection_after", "projection_after_request", False),
    )
    cases: list[dict[str, Any]] = []
    for name, point, commit_kill in schedule:
        case_root = output_root / name
        case_root.mkdir(parents=True)
        schema = _case_schema(output_root, name, database_url)
        setup = _setup_case(case_root, database_url=database_url, schema=schema)
        first_exit = _run_child(
            case_root,
            point=point,
            commit_kill=commit_kill,
            resume=None,
            database_url=database_url,
            schema=schema,
        )
        fault = json.loads((case_root / "fault.json").read_text())
        checkpoint = fault.get("checkpoint_ref")
        if checkpoint is None:
            raise RuntimeError(f"{name}: killed child did not publish a checkpoint marker")
        resume_ref = ArtifactRef.model_validate(checkpoint, strict=False)
        resume_exit = _run_child(
            case_root,
            point=None,
            commit_kill=False,
            resume=resume_ref,
            database_url=database_url,
            schema=schema,
        )
        result = MemoryWriteWorkflowResult.model_validate_json(
            (case_root / "result.json").read_bytes(), strict=False
        )
        commit_count, receipt_count, _retry_count = _database_counts(
            case_root,
            database_url=database_url,
            schema=schema,
        )
        projection_files = tuple((case_root / "projection").glob("*.json"))
        passed = (
            first_exit == -signal.SIGKILL
            and resume_exit == 0
            and result.status.value == "committed"
            and result.canonical_commit_accepted
            and len(result.committed_operation_ids) == 1
            and commit_count == 2
            and receipt_count == 1
            and len(projection_files) == 1
        )
        evidence = {
            "case": name,
            "fault_point": point or "commit_port_after_durable_effect",
            "first_exit": first_exit,
            "resume_exit": resume_exit,
            "checkpoint_ref": checkpoint,
            "result_status": result.status.value,
            "resulting_commit": (
                None if result.resulting_commit is None else result.resulting_commit.root
            ),
            "committed_operation_ids": [item.root for item in result.committed_operation_ids],
            "commit_rows": commit_count,
            "commit_receipts": receipt_count,
            "projection_effect_files": len(projection_files),
            "base_commit": setup["base_commit"].root,
            "pass": passed,
        }
        _write_once(case_root / "evidence.json", evidence)
        cases.append(evidence)
    retry_name = "retry_create_before"
    retry_root = output_root / retry_name
    retry_root.mkdir(parents=True)
    retry_schema = _case_schema(output_root, retry_name, database_url)
    cases.append(
        _run_retry_create_case(
            retry_root,
            database_url=database_url,
            schema=retry_schema,
        )
    )
    report = {
        "report_schema": "u8b-process-kill-matrix.v1",
        "project_id": PROJECT.root,
        "database_backend": "postgresql" if database_url is not None else "sqlite",
        "schedule": [item[0] for item in schedule] + ["retry_create_before"],
        "cases": cases,
        "status": "PASS" if all(item["pass"] for item in cases) else "FAIL",
        "duplicate_commit_count": sum(max(0, item["commit_rows"] - 2) for item in cases),
        "duplicate_projection_effect_count": sum(
            max(0, item["projection_effect_files"] - 1) for item in cases
        ),
    }
    _write_once(output_root / "u8b-process-kill-matrix-report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--database-url",
        help="optional isolated PostgreSQL URL; each case is placed in a fresh schema",
    )
    args = parser.parse_args()
    report = run_matrix(args.output_root, database_url=args.database_url)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
