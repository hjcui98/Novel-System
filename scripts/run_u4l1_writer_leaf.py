#!/usr/bin/env python3
"""Run one real, candidate-only Stage 3 Writer leaf and freeze its Gate evidence.

The request is constructed by the production composition root.  This runner only
seeds an isolated object store with the already committed five roots, injects the
locked real-hybrid retrieval backend, invokes ``assembly.writer.run`` directly,
and reconstructs the report from the durable model-call ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from opensearchpy import OpenSearch
from pydantic import JsonValue

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import (
    HttpEmbeddingProvider,
    HttpPassageReranker,
    OpenAICompatibleChatEndpoint,
    RetrievalModelRoute,
)
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.artifacts import ArtifactRef, RootManifest
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.creative_runtime import AutomationMode, CreativeRunPolicy
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import CommitId, ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallLedgerStatus,
    ModelCallPurpose,
    ModelRole,
)
from novel_agent.domain.retrieval_routing import ProjectionAttestation
from novel_agent.domain.runtime import TaskKind, TaskPurpose, TaskRecord, TaskStatus
from novel_agent.domain.stage2 import ProjectProfileRootDocument
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.domain.u4l1_writer_leaf import (
    U4L1BoundaryCheck,
    U4L1GateStatus,
    U4L1RubricItem,
    U4L1RubricStatus,
    U4L1WriterLeafReport,
)
from novel_agent.domain.writer_context import WriterContextPackageV2
from novel_agent.runtime.creative_assembly import (
    DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    ProductionAssemblyContext,
    load_production_runtime_assembly,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.model_call_ledger import aggregate_model_calls
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.projection import DerivedSnapshotRepository
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.stage2_retrieval_backend import build_real_hybrid_backend

SCHEMA_VERSION = SchemaVersion("1.0.0")
ROOT = Path(__file__).resolve().parents[1]


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
    return parser


def _check_args(args: argparse.Namespace) -> None:
    if args.output_root.exists():
        raise FileExistsError("U4-L1 output identity already exists")
    if args.model_output_tokens < 1:
        raise ValueError("Writer output budget must be positive")
    StableId(args.experiment_id)
    if not (args.source_project / "objects").is_dir():
        raise ValueError("source project object store is missing")
    if args.output_root.resolve() == args.source_project.resolve():
        raise ValueError("U4-L1 output must be separate from the canonical source store")


def _native_models_module() -> Any:
    """Load the shared-checkout native-model lock and service assertions."""

    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "scripts" / "native_models.py"
        model_root = parent / "models" / "retrieval"
        if not candidate.is_file() or candidate == current or not model_root.is_dir():
            continue
        spec = importlib.util.spec_from_file_location("novel_agent_shared_native_models", candidate)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load shared native-model owner: {candidate}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    raise RuntimeError("shared native-model owner is unavailable")


def _model_identity(base_url: str, expected_model: str) -> dict[str, Any]:
    response = httpx.get(f"{base_url.rstrip('/')}/models", timeout=8.0)
    response.raise_for_status()
    payload = response.json()
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ValueError("Writer endpoint returned no model list")
    match = next(
        (item for item in models if isinstance(item, dict) and item.get("id") == expected_model),
        None,
    )
    if match is None:
        raise ValueError(f"Writer endpoint does not expose requested model: {expected_model}")
    return {
        "id": match.get("id"),
        "owned_by": match.get("owned_by"),
        "root": match.get("root"),
        "max_model_len": match.get("max_model_len"),
    }


def _copy_canonical_roots(
    source: ArtifactRepository,
    destination: ArtifactRepository,
    manifest: RootManifest,
) -> tuple[ArtifactRef, ...]:
    refs = (
        manifest.text_root,
        manifest.plan_root,
        manifest.world_root,
        manifest.reference_root,
        manifest.project_profile_root,
    )
    copied: list[ArtifactRef] = []
    for ref in refs:
        data = source.read_verified(ref)
        copied_ref = destination.put(data, ref.media_type, ref.schema_version)
        if (
            copied_ref.artifact_id != ref.artifact_id
            or copied_ref.byte_length != ref.byte_length
            or copied_ref.media_type != ref.media_type
            or copied_ref.schema_version != ref.schema_version
        ):
            raise RuntimeError(f"canonical root copy changed identity: {ref.artifact_id.root}")
        copied.append(copied_ref)
    return tuple(copied)


def _runtime_fingerprint(value: str) -> str:
    fingerprint = value.removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("retrieval model lock contains an invalid runtime fingerprint")
    return fingerprint


def _build_retrieval_bundle(
    *,
    args: argparse.Namespace,
    project_id: ProjectId,
    source_commit: CommitId,
    snapshot_id: StableId,
    projection_attestation: ProjectionAttestation,
    session_factory: Any,
) -> tuple[Any, OpenSearch, dict[str, JsonValue]]:
    native_models = _native_models_module()
    model_lock = native_models.load_model_lock()
    embedding_model = model_lock.models["embedding"]
    reranker_model = model_lock.models["reranker"]
    native_models.assert_model_service(embedding_model)
    native_models.assert_model_service(reranker_model)
    retrieval_run = RunId(f"run.u4l1.retrieval.{args.experiment_id}"[:128])
    embedder = HttpEmbeddingProvider(
        RetrievalModelRoute(
            endpoint=args.embedding_url,
            model=embedding_model.model_id,
            revision=embedding_model.revision,
            runtime_fingerprint=_runtime_fingerprint(embedding_model.runtime_fingerprint),
            run_id=retrieval_run,
            task_id=TaskId("task.u4l1.embedding"),
            trace_id=f"trace.{retrieval_run.root}.embedding",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        ),
        dimension=embedding_model.dimension or 0,
        batch_size=32,
    )
    reranker = HttpPassageReranker(
        RetrievalModelRoute(
            endpoint=args.reranker_url,
            model=reranker_model.model_id,
            revision=reranker_model.revision,
            runtime_fingerprint=_runtime_fingerprint(reranker_model.runtime_fingerprint),
            run_id=retrieval_run,
            task_id=TaskId("task.u4l1.reranker"),
            trace_id=f"trace.{retrieval_run.root}.reranker",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        )
    )
    parsed = urlparse(args.opensearch_url)
    if parsed.hostname is None or parsed.port is None:
        raise ValueError("OpenSearch URL must include a host and port")
    search_client = OpenSearch(
        hosts=[{"host": parsed.hostname, "port": parsed.port}],
        use_ssl=parsed.scheme == "https",
        verify_certs=parsed.scheme == "https",
    )
    if not search_client.ping():
        search_client.close()
        raise RuntimeError("OpenSearch is unavailable")
    search_index = OpenSearchIndex(search_client)
    missing_indexes = tuple(
        index.physical_name
        for index in projection_attestation.indexes
        if not search_index.index_exists(index.physical_name)
    )
    if missing_indexes:
        search_client.close()
        raise RuntimeError(f"projection attestation indexes are unavailable: {missing_indexes}")
    bundle = build_real_hybrid_backend(
        r1=R1WorldRepository(session_factory),
        search_index=search_index,
        embedder=embedder,
        project_id=project_id,
        source_commit=source_commit,
        snapshot_id=snapshot_id,
        attestation=projection_attestation,
        reranker=reranker,
    )
    retrieval_identity = {
        "embedding_model": embedding_model.model_id,
        "embedding_revision": embedding_model.revision,
        "reranker_model": reranker_model.model_id,
        "reranker_revision": reranker_model.revision,
    }
    return bundle, search_client, retrieval_identity


def _unique_refs(refs: tuple[ArtifactRef, ...] | list[ArtifactRef]) -> tuple[ArtifactRef, ...]:
    result: list[ArtifactRef] = []
    seen: set[str] = set()
    for ref in refs:
        key = ref.artifact_id.root
        if key not in seen:
            result.append(ref)
            seen.add(key)
    return tuple(result)


def _request_artifacts(request: WritingLoopRequest) -> tuple[ArtifactRef, ...]:
    package_ledger = request.writer_context_package.evidence_ledger_ref
    return _unique_refs(
        [
            request.writing_task_artifact,
            request.accepted_plan.artifact,
            request.project_profile_artifact,
            request.writer_context_package_artifact,
            package_ledger,
            request.recent_prose_context_artifact,
        ]
    )


def _result_artifacts(result: Any) -> tuple[ArtifactRef, ...]:
    refs = list(result.artifacts)
    if result.work_plan is not None:
        refs.append(result.work_plan.work_plan_artifact)
        for receipt in result.work_plan.skill_receipts:
            refs.extend(receipt.input_artifacts)
            refs.extend(receipt.output_artifacts)
    if result.initial_draft is not None:
        refs.extend(
            (
                result.initial_draft.basis.context_artifact,
                result.initial_draft.text_artifact,
                result.initial_draft.sidecar_artifact,
                result.initial_draft.raw_output_artifact,
            )
        )
    if result.rewritten_draft is not None:
        refs.extend(
            (
                result.rewritten_draft.basis.context_artifact,
                result.rewritten_draft.text_artifact,
                result.rewritten_draft.sidecar_artifact,
                result.rewritten_draft.raw_output_artifact,
            )
        )
    if result.repaired_draft is not None:
        refs.extend(result.repaired_draft.editor_receipt.output_artifacts)
        refs.append(result.repaired_draft.text_artifact)
    return _unique_refs(refs)


def _check_artifacts(
    repository: ArtifactRepository, refs: tuple[ArtifactRef, ...]
) -> tuple[bool, str]:
    for ref in refs:
        try:
            repository.read_verified(ref)
        except Exception as error:  # boundary evidence must classify all storage failures
            return False, f"artifact {ref.artifact_id.root} is not readable: {error}"
    return True, f"verified {len(refs)} immutable artifact references"


def _ledger_evidence(
    *,
    gateway: ModelGateway,
    result: Any,
    endpoint: OpenAICompatibleChatEndpoint,
    repository: ArtifactRepository,
) -> tuple[
    tuple[Any, ...],
    tuple[Any, ...],
    tuple[Any, ...],
    tuple[ArtifactRef, ...],
    tuple[ArtifactRef, ...],
    bool,
    bool,
    tuple[str, ...],
]:
    entries = gateway.call_ledger.list_for_run(result.run_id)
    aggregates = aggregate_model_calls(entries)
    effective_budgets = tuple(entry.effective_budget for entry in entries)
    raw_refs = _unique_refs(
        [entry.raw_artifact_ref for entry in entries if entry.raw_artifact_ref is not None]
    )
    parsed_refs = _result_artifacts(result)
    request_by_id = {request.request_id: request for request in endpoint.requests}
    budget_consistent = len(entries) == len(request_by_id)
    blockers: list[str] = []
    for entry in entries:
        request = request_by_id.get(entry.request_id)
        if request is None:
            budget_consistent = False
            blockers.append(f"missing_api_request:{entry.request_id.root}")
            continue
        if (
            request.max_output_tokens != entry.effective_budget.total_output_budget
            or request.budget_source != entry.effective_budget.budget_source
            or request.enable_thinking is True
            or request.thinking_token_budget not in {None, entry.effective_budget.thinking_budget}
        ):
            budget_consistent = False
            blockers.append(f"budget_mismatch:{entry.request_id.root}")
        if entry.status in {
            ModelCallLedgerStatus.REQUESTED,
            ModelCallLedgerStatus.UNCERTAIN,
            ModelCallLedgerStatus.TRANSPORT_EXHAUSTED,
        }:
            blockers.append(f"unsettled_or_failed_model_call:{entry.request_id.root}")
        if entry.status in {
            ModelCallLedgerStatus.COMPLETED,
            ModelCallLedgerStatus.VALIDATION_REJECTED,
        } and (entry.raw_artifact_ref is None or entry.call_record is None):
            blockers.append(f"incomplete_terminal_evidence:{entry.request_id.root}")
        if entry.raw_artifact_ref is not None:
            try:
                repository.read_verified(entry.raw_artifact_ref)
            except Exception as error:  # report the boundary, not an untyped crash
                blockers.append(f"raw_artifact_unreadable:{entry.request_id.root}:{error}")
    result_call_ids = {call.request_id for call in result.model_call_records}
    ledger_call_ids = {entry.request_id for entry in entries if entry.call_record is not None}
    reconstructed = result_call_ids.issubset(ledger_call_ids) and not blockers
    return (
        entries,
        aggregates,
        effective_budgets,
        raw_refs,
        parsed_refs,
        budget_consistent,
        reconstructed,
        tuple(blockers),
    )


def _boundary_checks(
    *,
    request: WritingLoopRequest,
    result: Any,
    package: Any,
    reactive_allow_future_plan: bool,
    basis_unchanged: bool,
    cited_artifacts_ok: bool,
    cited_artifact_detail: str,
    ledger_report_reconstructed: bool,
) -> tuple[U4L1BoundaryCheck, ...]:
    future = request.future_isolation_attestation
    future_ok = (
        future.passed
        and not future.overlap_source_ids
        and not future.evaluator_only_source_ids
        and not reactive_allow_future_plan
    )
    access_ok = request.information_scope == "writer_safe" and (
        result.context_view is not None
        and result.context_view.information_scope == "writer_safe"
        and all(
            item.information_scope != "planner_safe"
            for item in (
                *result.context_view.protected_items,
                *result.context_view.active_memory_items,
                *result.context_view.working_items,
                *result.context_view.recent_settled_tail,
                *result.context_view.compacted_prefix_items,
            )
        )
        and all(item.source_scope != "planner_safe" for item in package.items)
    )
    acceptance_ok = (
        result.status.value == "DRAFT_CANDIDATE_READY"
        and result.candidate_only is True
        and result.canon_mutated is False
        and result.memory_patch_generated is False
        and result.commit_called is False
    )
    return (
        U4L1BoundaryCheck(
            name="future_isolation",
            passed=future_ok,
            detail=(
                "Future Isolation passed with no evaluator-only or overlap source ids and "
                f"reactive allow_future_plan={reactive_allow_future_plan}"
            ),
        ),
        U4L1BoundaryCheck(
            name="access_scope",
            passed=access_ok,
            detail="Writer request, Context View, and package items carry no planner-only scope",
        ),
        U4L1BoundaryCheck(
            name="basis_lineage",
            passed=basis_unchanged,
            detail="request, package, Context View, Draft, Editor, and final candidate share basis",
        ),
        U4L1BoundaryCheck(
            name="citation_and_artifact_integrity",
            passed=cited_artifacts_ok,
            detail=cited_artifact_detail,
        ),
        U4L1BoundaryCheck(
            name="candidate_acceptance",
            passed=acceptance_ok,
            detail="Writer result is candidate-only and never calls canonical settlement",
        ),
        U4L1BoundaryCheck(
            name="durable_ledger_reconstruction",
            passed=ledger_report_reconstructed,
            detail="model usage and raw references are rebuilt from the durable ledger",
        ),
    )


def _rubric(
    *,
    request: WritingLoopRequest,
    result: Any,
    refs: tuple[ArtifactRef, ...],
    ledger_count: int,
) -> tuple[U4L1RubricItem, ...]:
    repair_count = sum(
        verdict in {"LOCAL_REPAIR", "MAJOR_REWRITE"}
        for verdict in (report.verdict.value for report in result.editorial_reports)
    )
    return (
        U4L1RubricItem(
            dimension="plan_obedience",
            status=U4L1RubricStatus.NOT_SCORED,
            detail=(
                "No independent semantic scorer was authorized; accepted Plan binding is "
                "mechanical."
            ),
            evidence_refs=(request.accepted_plan.artifact,),
        ),
        U4L1RubricItem(
            dimension="evidence_use",
            status=U4L1RubricStatus.MECHANICAL,
            detail="Writer Context and Evidence Ledger are bound to the same request basis.",
            evidence_refs=(
                request.writer_context_package_artifact,
                request.writer_context_package.evidence_ledger_ref,
            ),
        ),
        U4L1RubricItem(
            dimension="knowledge_boundary",
            status=U4L1RubricStatus.MECHANICAL,
            detail="Knowledge boundary is represented by the zero-leakage hard checks.",
            evidence_refs=refs,
        ),
        U4L1RubricItem(
            dimension="readability",
            status=U4L1RubricStatus.NOT_SCORED,
            detail=(
                "Readability requires an independent human or approved scorer; no score is "
                "inferred."
            ),
        ),
        U4L1RubricItem(
            dimension="repair_convergence",
            status=U4L1RubricStatus.MECHANICAL,
            detail=(
                f"Editor reports={len(result.editorial_reports)}, "
                f"repair/rewrite routes={repair_count}, "
                "final typed acceptance is recorded by the leaf result."
            ),
            evidence_refs=refs,
        ),
        U4L1RubricItem(
            dimension="cost",
            status=U4L1RubricStatus.MECHANICAL,
            detail=f"Durable model-call ledger contains {ledger_count} entries and exact budgets.",
            evidence_refs=refs,
        ),
    )


def _build_task(
    *,
    project_id: ProjectId,
    run_id: RunId,
    task_id: TaskId,
    commit: CommitId,
    snapshot_id: StableId,
    policy: CreativeRunPolicy,
    manifest: RootManifest,
    latest_chapter: int,
) -> TaskRecord:
    target = latest_chapter + 1
    return TaskRecord(
        task_id=task_id,
        run_id=run_id,
        project_id=project_id,
        kind=TaskKind.DRAFT_CANDIDATE,
        purpose=TaskPurpose.NORMAL,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=commit,
        basis_snapshot=snapshot_id,
        policy_hash=policy.policy_hash,
        permission_hash=policy.permission_hash,
        input_artifact_refs=(
            manifest.text_root,
            manifest.plan_root,
            manifest.world_root,
            manifest.reference_root,
            manifest.project_profile_root,
        ),
        failure_budget=3,
        retry_tranche_size=3,
        chapter_index=target,
        target_chapters=target,
    )


def _write_json_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"U4-L1 refuses to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = _parser().parse_args()
    _check_args(args)
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True)
    model_identity = _model_identity(args.model_base_url, args.model)
    project_id = ProjectId(args.project_id)
    run_id = RunId(f"run.u4l1.{args.experiment_id}"[:128])
    task_id = TaskId(f"task.u4l1.{args.experiment_id}"[:128])
    database_engine = build_engine(args.database_url)
    search_client: OpenSearch | None = None
    try:
        session_factory = build_session_factory(database_engine)
        commits = CommitService(session_factory)
        current_commit = commits.current_commit(project_id)
        manifest = commits.load_manifest(current_commit)
        if manifest.project_id != project_id:
            raise RuntimeError("current commit manifest belongs to another project")
        snapshots = DerivedSnapshotRepository(session_factory)
        snapshot = snapshots.get_for_commit(current_commit)
        projection_attestation = snapshots.get_attestation_for_commit(current_commit)
        if snapshot is None or projection_attestation is None:
            raise RuntimeError("current Writer basis has no derived exact snapshot attestation")
        if (
            snapshot.source_commit != current_commit
            or snapshot.snapshot_id != projection_attestation.snapshot_id
            or snapshot.build_status.value != "exact"
            or snapshot.published_at is None
            or not projection_attestation.quality_eligible
        ):
            raise RuntimeError("current Writer basis is not an exact quality-eligible projection")
        source_repository = ArtifactRepository(
            FilesystemObjectStore(args.source_project.resolve() / "objects")
        )
        output_repository = ArtifactRepository(FilesystemObjectStore(output_root / "objects"))
        _copy_canonical_roots(source_repository, output_repository, manifest)
        text_root = TextRootDocument.model_validate_json(
            output_repository.read_verified(manifest.text_root), strict=True
        )
        PlanRootDocument.model_validate_json(
            output_repository.read_verified(manifest.plan_root), strict=True
        )
        ProjectProfileRootDocument.model_validate_json(
            output_repository.read_verified(manifest.project_profile_root), strict=True
        )
        latest_chapter = text_root.chapters[-1].chapter_index if text_root.chapters else 0
        native_models = _native_models_module()
        model_lock = native_models.load_model_lock()
        embedding_model = model_lock.models["embedding"]
        reranker_model = model_lock.models["reranker"]
        native_models.assert_model_service(embedding_model)
        native_models.assert_model_service(reranker_model)
        retrieval_bundle, search_client, retrieval_identity = _build_retrieval_bundle(
            args=args,
            project_id=project_id,
            source_commit=current_commit,
            snapshot_id=projection_attestation.snapshot_id,
            projection_attestation=projection_attestation,
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
        manifest_runtime = load_stage5_manifest(
            ROOT / "src" / "novel_agent" / "runtime" / "stage5_development_manifest.json"
        )
        policy = CreativeRunPolicy(
            automation_mode=AutomationMode.MANUAL,
            policy_hash=manifest_runtime.configuration_fingerprint,
            permission_hash=manifest_runtime.configuration_fingerprint,
            runtime_parallelism=1,
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
                model_endpoints=(endpoint_registration,),
                retrieval_backend=retrieval_bundle.backend,
                reranker=retrieval_bundle.reranker,
                schema_version=SCHEMA_VERSION,
            ),
        )
        model_gateway = assembly.model_gateway
        if model_gateway is None:
            raise RuntimeError("production assembly did not expose its ModelGateway")
        production_attestation = assembly.attestation
        if production_attestation is None:
            raise RuntimeError("production assembly did not expose its attestation")
        task = _build_task(
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            commit=current_commit,
            snapshot_id=projection_attestation.snapshot_id,
            policy=policy,
            manifest=manifest,
            latest_chapter=latest_chapter,
        )
        request = assembly.writing_request_factory(task)
        if not isinstance(request, WritingLoopRequest):
            raise TypeError("production Writing request factory returned the wrong type")
        reactive_inputs = assembly.writer._reactive_inputs_factory(request)
        result = asyncio.run(assembly.writer.run(request))
        after_commit = commits.current_commit(project_id)
        basis_unchanged = after_commit == current_commit
        package = request.writer_context_package
        if not isinstance(package, WriterContextPackageV2):
            raise RuntimeError("production Writer request did not produce writer_context.v2")
        exact_editor_context_ref = (
            result.initial_draft.basis.context_artifact
            if result.initial_draft is not None
            else None
        )
        refs_to_verify = _unique_refs(
            [
                *_request_artifacts(request),
                *_result_artifacts(result),
                *(tuple([exact_editor_context_ref]) if exact_editor_context_ref else ()),
            ]
        )
        cited_ok, cited_detail = _check_artifacts(output_repository, refs_to_verify)
        (
            entries,
            aggregates,
            effective_budgets,
            raw_refs,
            parsed_refs,
            api_budget_consistent,
            ledger_report_reconstructed,
            ledger_blockers,
        ) = _ledger_evidence(
            gateway=model_gateway,
            result=result,
            endpoint=endpoint,
            repository=output_repository,
        )
        boundaries = _boundary_checks(
            request=request,
            result=result,
            package=package,
            reactive_allow_future_plan=reactive_inputs.resolution_template.allow_future_plan,
            basis_unchanged=basis_unchanged,
            cited_artifacts_ok=cited_ok,
            cited_artifact_detail=cited_detail,
            ledger_report_reconstructed=ledger_report_reconstructed,
        )
        blockers = list(ledger_blockers)
        blockers.extend(check.name for check in boundaries if not check.passed)
        if result.status.value != "DRAFT_CANDIDATE_READY":
            blockers.append(f"writer_status:{result.status.value}")
        gate_status = U4L1GateStatus.PASS if not blockers else U4L1GateStatus.FAILED
        result_refs = _result_artifacts(result)
        report = U4L1WriterLeafReport(
            report_id=StableId(f"report.u4l1.{args.experiment_id}"[:128]),
            generated_at=datetime.now(UTC),
            gate_status=gate_status,
            gate_blockers=tuple(dict.fromkeys(blockers)),
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            basis_commit=current_commit,
            snapshot_id=projection_attestation.snapshot_id,
            model_identity={**model_identity, "retrieval": retrieval_identity},
            endpoint_url=args.model_base_url,
            production_attestation=production_attestation,
            projection_attestation=projection_attestation,
            request_artifacts=_request_artifacts(request),
            writing_task_artifact=request.writing_task_artifact,
            accepted_plan_artifact=request.accepted_plan.artifact,
            project_profile_artifact=request.project_profile_artifact,
            writer_context_package_artifact=request.writer_context_package_artifact,
            evidence_ledger_artifact=package.evidence_ledger_ref,
            recent_prose_artifact=request.recent_prose_context_artifact,
            exact_editor_context_ref=exact_editor_context_ref,
            raw_artifact_refs=raw_refs,
            parsed_artifact_refs=result_refs or parsed_refs,
            memory_request_refs=tuple(delta.request_ref for delta in result.context_deltas),
            skill_receipts=(
                result.work_plan.skill_receipts if result.work_plan is not None else ()
            ),
            effective_budgets=effective_budgets,
            ledger_entries=entries,
            model_call_aggregates=aggregates,
            api_budget_consistent=api_budget_consistent,
            ledger_report_reconstructed=ledger_report_reconstructed,
            editor_verdicts=tuple(report.verdict.value for report in result.editorial_reports),
            boundary_checks=boundaries,
            rubric=_rubric(
                request=request,
                result=result,
                refs=refs_to_verify,
                ledger_count=len(entries),
            ),
            result=result,
        )
        _write_json_once(output_root / "u4l1_report.json", report.model_dump(mode="json"))
        print(
            f"U4-L1 Writer leaf: status={report.gate_status.value} "
            f"model_calls={len(entries)} output={output_root / 'u4l1_report.json'}",
            flush=True,
        )
        return 0 if report.gate_status is U4L1GateStatus.PASS else 2
    finally:
        if search_client is not None:
            search_client.close()
        database_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
