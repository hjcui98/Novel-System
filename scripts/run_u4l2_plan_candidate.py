#!/usr/bin/env python3
"""Run one real, candidate-only Stage 4 planning request on the production leaf.

This runner supplies the existing canonical basis and the author-intent assets in an
isolated object store, then invokes the production ``Stage4PlanningLeafAdapter``.
It deliberately stops before runtime task creation, acceptance, PlanRoot materialization,
Commit, and Chapter Settlement.  Its output is a candidate/review lineage receipt for the
human or policy acceptance boundary that follows the Planner leaf.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Any

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import OpenAICompatibleChatEndpoint
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    PlanningLoopRequest,
)
from novel_agent.domain.ids import ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import ModelRole
from novel_agent.domain.planning import (
    PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
    PlanningLoopCheckpoint,
)
from novel_agent.domain.runtime import TaskPurpose
from novel_agent.domain.stage2 import ReferenceRootDocument
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.runtime.creative_assembly import (
    DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    ProductionAssemblyContext,
    load_production_runtime_assembly,
)
from novel_agent.runtime.production_bootstrap import (
    _default_stage4_policy,
    load_production_assembly_spec,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.model_gateway import RegisteredModelEndpoint
from novel_agent.services.projection import DerivedSnapshotRepository

SCHEMA_VERSION = SchemaVersion("1.0.0")
ROOT = Path(__file__).resolve().parents[1]


def _load_u4l1_helpers() -> Any:
    try:
        return import_module("run_u4l1_writer_leaf")
    except ModuleNotFoundError as error:
        if error.name != "run_u4l1_writer_leaf":
            raise
        return import_module("scripts.run_u4l1_writer_leaf")


_U4L1_HELPERS = _load_u4l1_helpers()


def _helper(name: str) -> Any:
    return getattr(_U4L1_HELPERS, name)


_build_retrieval_bundle = _helper("_build_retrieval_bundle")
_copy_canonical_roots = _helper("_copy_canonical_roots")
_model_identity = _helper("_model_identity")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-project", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--project-id", default="ztj_volume01_preview")
    parser.add_argument("--opensearch-url", default="http://127.0.0.1:9200")
    parser.add_argument("--embedding-url", default="http://127.0.0.1:8081/v1/embeddings")
    parser.add_argument("--reranker-url", default="http://127.0.0.1:8082/rerank")
    parser.add_argument("--model-base-url", default="http://127.0.0.1:8005/v1")
    parser.add_argument("--model", default="qwen38-27b-fp8")
    parser.add_argument("--model-output-tokens", type=int, default=8000)
    parser.add_argument("--model-token-budget", type=int, default=20_000)
    parser.add_argument("--model-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--horizon-start", type=int)
    parser.add_argument("--horizon-end", type=int)
    parser.add_argument("--resume-checkpoint", type=Path)
    parser.add_argument("--resume-object-store", type=Path)
    parser.add_argument("--resume-attempt", type=int, default=0)
    parser.add_argument("--planner-memory-rounds", type=int, default=8)
    parser.add_argument("--planner-memory-budget-extensions", type=int, default=0)
    return parser


def _check_args(args: argparse.Namespace) -> None:
    if args.output_root.exists():
        raise FileExistsError("U4-L2 output identity already exists")
    if args.model_output_tokens < 1:
        raise ValueError("Planner output budget must be positive")
    if args.model_token_budget < 1:
        raise ValueError("Planner token slice budget must be positive")
    if args.model_timeout_seconds <= 0:
        raise ValueError("Planner model timeout must be positive")
    if args.resume_attempt < 0:
        raise ValueError("Planner resume attempt must not be negative")
    if args.planner_memory_rounds < 1:
        raise ValueError("Planner memory rounds must be at least one")
    if args.planner_memory_budget_extensions < 0:
        raise ValueError("Planner memory budget extensions must not be negative")
    StableId(args.experiment_id)
    if not (args.source_project / "objects").is_dir():
        raise ValueError("source project object store is missing")
    if args.output_root.resolve() == args.source_project.resolve():
        raise ValueError("U4-L2 output must be separate from the canonical source store")
    if (args.horizon_start is None) != (args.horizon_end is None):
        raise ValueError("Planner horizon bounds must appear together")
    if args.horizon_start is not None and args.horizon_end < args.horizon_start:
        raise ValueError("Planner horizon end precedes start")
    if args.resume_checkpoint is None and args.resume_object_store is not None:
        raise ValueError("resume object store requires a resume checkpoint")
    if args.resume_checkpoint is None and args.resume_attempt != 0:
        raise ValueError("resume attempt requires a resume checkpoint")
    if args.resume_checkpoint is not None:
        if not args.resume_checkpoint.is_file():
            raise ValueError("resume checkpoint file is missing")
        if args.resume_object_store is None or not args.resume_object_store.is_dir():
            raise ValueError("resume checkpoint requires an existing resume object store")


def _copy_ref(
    source: ArtifactRepository, destination: ArtifactRepository, ref: ArtifactRef
) -> None:
    copied = destination.put(source.read_verified(ref), ref.media_type, ref.schema_version)
    if copied != ref:
        raise RuntimeError(f"artifact copy changed identity: {ref.artifact_id.root}")


def _copy_resume_checkpoint(
    checkpoint_path: Path,
    source: ArtifactRepository,
    destination: ArtifactRepository,
) -> tuple[ArtifactRef, PlanningLoopCheckpoint]:
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint = PlanningLoopCheckpoint.model_validate_json(checkpoint_bytes, strict=True)
    refs = (
        checkpoint.inquiry_ref,
        checkpoint.inquiry_review_ref,
        checkpoint.memory_context_ref,
        checkpoint.planner_context_ref,
        checkpoint.proposal_ref,
        checkpoint.plan_review_ref,
        checkpoint.execution_ref,
        *checkpoint.reviewer_context_refs,
        *checkpoint.planner_memory_context_refs,
    )
    _copy_reachable_artifacts(
        source,
        destination,
        tuple(ref for ref in refs if ref is not None),
    )
    checkpoint_ref = destination.put(
        checkpoint_bytes,
        PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
        SCHEMA_VERSION,
    )
    return checkpoint_ref, checkpoint


def _embedded_artifact_refs(value: object) -> tuple[ArtifactRef, ...]:
    found: list[ArtifactRef] = []
    if isinstance(value, dict):
        if {"artifact_id", "byte_length", "media_type", "schema_version"}.issubset(value):
            with contextlib.suppress(ValueError):
                found.append(ArtifactRef.model_validate(value, strict=True))
        for child in value.values():
            found.extend(_embedded_artifact_refs(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_embedded_artifact_refs(child))
    return tuple(found)


def _copy_reachable_artifacts(
    source: ArtifactRepository,
    destination: ArtifactRepository,
    roots: tuple[ArtifactRef, ...],
) -> tuple[ArtifactRef, ...]:
    pending = list(roots)
    copied: dict[str, ArtifactRef] = {}
    while pending:
        ref = pending.pop()
        if ref.artifact_id.root in copied:
            continue
        raw = source.read_verified(ref)
        _copy_ref(source, destination, ref)
        copied[ref.artifact_id.root] = ref
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        pending.extend(_embedded_artifact_refs(value))
    return tuple(copied.values())


def _copy_runtime_context_artifacts(
    run_id: RunId,
    session_factory: Any,
    source: ArtifactRepository,
    destination: ArtifactRepository,
) -> tuple[ArtifactRef, ...]:
    """Copy the DB-owned Context Runtime artifacts required by a resumed Planner stream."""

    events = RunEventLogRepository(session_factory).replay(run_id)
    checkpoint = RunCheckpointRepository(session_factory).latest(run_id)
    refs = [ref for event in events for ref in event.artifact_refs]
    if checkpoint is not None:
        refs.append(checkpoint.state_artifact_ref)
    unique_refs: dict[str, ArtifactRef] = {}
    for ref in refs:
        unique_refs.setdefault(ref.artifact_id.root, ref)
    return _copy_reachable_artifacts(source, destination, tuple(unique_refs.values()))


def _model_calls(gateway: Any, run_id: RunId) -> tuple[dict[str, Any], ...]:
    entries = gateway.call_ledger.list_for_run(run_id)
    return tuple(
        {
            "request_id": entry.request_id.root,
            "status": entry.status.value,
            "logical_phase": entry.logical_phase,
            "effective_budget": entry.effective_budget.model_dump(mode="json"),
            "raw_artifact_ref": (
                entry.raw_artifact_ref.model_dump(mode="json")
                if entry.raw_artifact_ref is not None
                else None
            ),
            "call_record": (
                entry.call_record.model_dump(mode="json") if entry.call_record is not None else None
            ),
        }
        for entry in entries
    )


def main() -> int:
    args = _parser().parse_args()
    _check_args(args)
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True)
    project_id = ProjectId(args.project_id)
    run_id = RunId(f"run.u4l2.{args.experiment_id}"[:128])
    task_id = TaskId(f"task.u4l2.{args.experiment_id}"[:128])
    model_identity = _model_identity(args.model_base_url, args.model)
    database_engine = build_engine(args.database_url)
    search_client = None
    try:
        session_factory = build_session_factory(database_engine)
        commits = CommitService(session_factory)
        current_commit = commits.current_commit(project_id)
        manifest = commits.load_manifest(current_commit)
        source = ArtifactRepository(
            FilesystemObjectStore(args.source_project.resolve() / "objects")
        )
        destination = ArtifactRepository(FilesystemObjectStore(output_root / "objects"))
        _copy_canonical_roots(source, destination, manifest)
        continuation_refs: tuple[ArtifactRef, ...] = ()
        resume_checkpoint: PlanningLoopCheckpoint | None = None
        model_request_namespace: str | None = None
        if args.resume_checkpoint is not None:
            resume_source = ArtifactRepository(
                FilesystemObjectStore(args.resume_object_store.resolve())
            )
            checkpoint_ref, resume_checkpoint = _copy_resume_checkpoint(
                args.resume_checkpoint.resolve(), resume_source, destination
            )
            expected_request_id = StableId(f"planning-request.{task_id.root}"[:128])
            if resume_checkpoint.request_id != expected_request_id:
                raise ValueError(
                    "resume checkpoint request identity differs from experiment task identity"
                )
            continuation_refs = (checkpoint_ref,)
            model_request_namespace = (
                "resume-"
                + checkpoint_ref.artifact_id.root.removeprefix("sha256:")[:16]
                + f"-attempt-{args.resume_attempt}"
            )
            runtime_context_refs = _copy_runtime_context_artifacts(
                run_id,
                session_factory,
                resume_source,
                destination,
            )
        else:
            runtime_context_refs = ()
        reference_root = ReferenceRootDocument.model_validate_json(
            destination.read_verified(manifest.reference_root), strict=True
        )
        author_refs = tuple(asset.artifact for asset in reference_root.assets)
        for ref in author_refs:
            _copy_ref(source, destination, ref)
        snapshots = DerivedSnapshotRepository(session_factory)
        snapshot = snapshots.get_for_commit(current_commit)
        attestation = snapshots.get_attestation_for_commit(current_commit)
        if snapshot is None or attestation is None:
            raise RuntimeError("current Planner basis has no derived snapshot attestation")
        if (
            snapshot.source_commit != current_commit
            or snapshot.snapshot_id != attestation.snapshot_id
            or snapshot.build_status.value != "exact"
            or snapshot.published_at is None
            or not attestation.quality_eligible
        ):
            raise RuntimeError("current Planner basis is not exact and quality eligible")
        retrieval_bundle, search_client, retrieval_identity = _build_retrieval_bundle(
            args=args,
            project_id=project_id,
            source_commit=current_commit,
            snapshot_id=attestation.snapshot_id,
            projection_attestation=attestation,
            session_factory=session_factory,
        )
        endpoint = OpenAICompatibleChatEndpoint(
            base_url=args.model_base_url,
            model=args.model,
            max_output_tokens=args.model_output_tokens,
            temperature=0.0,
            local_only=True,
            max_retries=0,
        )
        semantic_endpoint = OpenAICompatibleChatEndpoint(
            base_url=args.model_base_url,
            model=args.model,
            max_output_tokens=args.model_output_tokens,
            temperature=0.0,
            local_only=True,
            max_retries=0,
        )
        endpoint_registration = RegisteredModelEndpoint(
            role=ModelRole.IMPLEMENTATION,
            endpoint_name=f"{args.model}@8005",
            model_name=args.model,
            adapter=endpoint,
            revision=args.model,
            sequence_limit=131_072,
            output_limit=args.model_output_tokens,
            safety_allowance_tokens=1_000,
            estimated_reasoning_reserve=2_048,
            default_thinking=False,
            reasoning_included_in_completion_tokens=False,
            global_output_cap=131_072,
        )
        semantic_endpoint_registration = RegisteredModelEndpoint(
            role=ModelRole.BATCH_TEST,
            endpoint_name=f"{args.model}@8005-semantic-judge",
            model_name=args.model,
            adapter=semantic_endpoint,
            revision=args.model,
            sequence_limit=131_072,
            output_limit=args.model_output_tokens,
            safety_allowance_tokens=1_000,
            estimated_reasoning_reserve=2_048,
            default_thinking=False,
            reasoning_included_in_completion_tokens=False,
            global_output_cap=131_072,
        )
        manifest_runtime = load_stage5_manifest(
            ROOT / "src" / "novel_agent" / "runtime" / "stage5_development_manifest.json"
        )
        policy = CreativeRunPolicy(
            automation_mode=AutomationMode.MANUAL,
            policy_hash=manifest_runtime.configuration_fingerprint,
            permission_hash=manifest_runtime.configuration_fingerprint,
            runtime_parallelism=1,
        )
        stage4_policy = _default_stage4_policy(load_production_assembly_spec())
        stage4_policy = replace(
            stage4_policy,
            budgets=stage4_policy.budgets.model_copy(
                update={
                    "model_token_budget": args.model_token_budget,
                    "planner_memory_rounds": args.planner_memory_rounds,
                }
            ),
            model_timeout_seconds=args.model_timeout_seconds,
        )
        assembly = load_production_runtime_assembly(
            DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
            ProductionAssemblyContext(
                database_url=args.database_url,
                object_store_root=output_root / "objects",
                project_id=project_id,
                run_id=run_id,
                policy=policy,
                manifest=manifest_runtime,
                model_endpoints=(endpoint_registration, semantic_endpoint_registration),
                retrieval_backend=retrieval_bundle.backend,
                reranker=retrieval_bundle.reranker,
                schema_version=SCHEMA_VERSION,
                stage4_policy=stage4_policy,
                model_request_namespace=model_request_namespace,
            ),
        )
        text_root = TextRootDocument.model_validate_json(
            destination.read_verified(manifest.text_root), strict=True
        )
        latest_chapter = text_root.chapters[-1].chapter_index if text_root.chapters else 0
        horizon_start = args.horizon_start or latest_chapter + 1
        horizon_end = args.horizon_end or horizon_start
        request = PlanningLoopRequest(
            run_id=run_id,
            task_id=task_id,
            project_id=project_id,
            basis_commit=current_commit,
            basis_snapshot=attestation.snapshot_id,
            input_artifact_refs=author_refs,
            continuation_artifact_refs=continuation_refs,
            planner_memory_budget_extensions=args.planner_memory_budget_extensions,
            purpose=TaskPurpose.NORMAL,
            chapter_index=latest_chapter,
            horizon_start=horizon_start,
            horizon_end=horizon_end,
        )
        result = asyncio.run(assembly.planner.run(request))
        after_commit = commits.current_commit(project_id)
        candidate = result.candidate
        proposal: dict[str, Any] | None = None
        if candidate is not None:
            proposal = json.loads(destination.read_verified(candidate.artifact_ref))
        report = {
            "schema": "u4l2-plan-candidate.v1",
            "experiment_id": args.experiment_id,
            "run_id": run_id.root,
            "task_id": task_id.root,
            "project_id": project_id.root,
            "model": model_identity,
            "retrieval": retrieval_identity,
            "basis_commit": current_commit.root,
            "after_commit": after_commit.root,
            "basis_unchanged": after_commit == current_commit,
            "snapshot_id": attestation.snapshot_id.root,
            "author_intent_refs": [ref.model_dump(mode="json") for ref in author_refs],
            "horizon": {"start": horizon_start, "end": horizon_end},
            "model_output_tokens": args.model_output_tokens,
            "model_token_budget": args.model_token_budget,
            "model_timeout_seconds": args.model_timeout_seconds,
            "resume_checkpoint_ref": (
                continuation_refs[0].model_dump(mode="json") if continuation_refs else None
            ),
            "resume_checkpoint_request_id": (
                resume_checkpoint.request_id.root if resume_checkpoint is not None else None
            ),
            "model_request_namespace": model_request_namespace,
            "resume_attempt": args.resume_attempt,
            "planner_memory_rounds": args.planner_memory_rounds,
            "planner_memory_budget_extensions": args.planner_memory_budget_extensions,
            "resume_runtime_context_refs": [
                ref.model_dump(mode="json") for ref in runtime_context_refs
            ],
            "terminal_status": result.status.value,
            "failure_code": result.failure_code,
            "failure_detail": result.failure_detail,
            "candidate": candidate.model_dump(mode="json") if candidate is not None else None,
            "proposal": proposal,
            "artifact_refs": [ref.model_dump(mode="json") for ref in result.artifact_refs],
            "model_calls": list(_model_calls(assembly.model_gateway, run_id)),
            "candidate_only": True,
            "acceptance_called": False,
            "commit_called": False,
            "chapter_settlement_called": False,
            "output_object_store": str(output_root / "objects"),
        }
        output = output_root / "u4l2_plan_candidate.json"
        if output.exists():
            raise RuntimeError(f"refusing to overwrite U4-L2 output: {output}")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        print(
            f"U4-L2 planner candidate: status={result.status.value} "
            f"calls={len(report['model_calls'])} output={output}"
        )
        return 0
    finally:
        if search_client is not None:
            search_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
