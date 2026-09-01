#!/usr/bin/env python3
"""Execute and freeze one production Planner -> Memory -> Writer -> Curator run."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from opensearchpy import OpenSearch

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import (
    HttpEmbeddingProvider,
    HttpPassageReranker,
    RetrievalModelRoute,
)
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.runtime.stage4_planner import Stage4InvocationPolicy
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import CreativeRunRequest
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, TaskId
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRole
from novel_agent.domain.retrieval_routing import RetrievalBackendProfile
from novel_agent.domain.stage5_evaluation import VerticalRunStatus
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.ports.model_endpoint import ModelEndpointError
from novel_agent.runtime.creative_assembly import (
    DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    ProductionAssemblyContext,
    load_production_runtime_assembly,
)
from novel_agent.runtime.production_bootstrap import (
    _default_stage4_policy,
    load_production_assembly_spec,
    resolve_registered_model_endpoints,
)
from novel_agent.runtime.vertical_runner import VerticalCreativeRunner
from novel_agent.services.artifacts import ArtifactIntegrityError, ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.embedding_cache import SqlEmbeddingCache
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedSnapshotRepository,
    FullDerivedProjectionBuilder,
)
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.retrieval import RetrievalBackend
from novel_agent.services.search_retrieval import Stage2RSearchIndexer
from novel_agent.runtime.real_hybrid import CommitScopedRealHybridBackend
from novel_agent.services.stage2_retrieval_backend import (
    RealHybridProjectionGateway,
    build_real_hybrid_backend,
)


def _artifact_paths(output: Path) -> dict[str, Path]:
    stem = output.stem if output.suffix else output.name
    return {
        "spec": output.with_name(f"{stem}.assembly-spec.json"),
        "attestation": output.with_name(f"{stem}.resolved-attestation.json"),
        "endpoint_revision": output.with_name(f"{stem}.endpoint-revision.json"),
        "request": output.with_name(f"{stem}.run-request.json"),
        "invocation": output.with_name(f"{stem}.invocation.json"),
        "result": output,
    }


def _assert_output_paths_free(paths: dict[str, Path]) -> None:
    occupied = tuple(f"{name}={path}" for name, path in paths.items() if path.exists())
    if occupied:
        raise RuntimeError("stage5 evaluation output paths already exist: " + ", ".join(occupied))


def _assert_object_and_output_roots_disjoint(object_store_root: Path, output: Path) -> None:
    """Reject an invocation that can mix immutable inputs with write-once outputs."""

    object_root = object_store_root.resolve()
    output_root = output.resolve().parent
    if object_root == output_root:
        raise RuntimeError(
            "stage5 evaluation requires disjoint object-store and output roots: "
            f"object_store_root={object_root}, output_root={output_root}"
        )


def _assert_input_artifacts_present(
    object_store_root: Path, references: tuple[ArtifactRef, ...]
) -> None:
    """Verify every request input before the first model call or runtime mutation."""

    root = object_store_root.resolve()
    if not root.is_dir():
        raise RuntimeError(f"stage5 evaluation object-store root is not a directory: {root}")
    repository = ArtifactRepository(FilesystemObjectStore(root))
    for reference in references:
        try:
            repository.read_verified(reference)
        except ArtifactIntegrityError as error:
            raise RuntimeError(
                "stage5 evaluation input artifact is unavailable or invalid: "
                f"{reference.artifact_id.root} under {root}"
            ) from error


def _write_json_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"stage5 evaluation refuses to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _database_descriptor(database_url: str) -> str:
    """Return database origin/path without persisting credentials in audit artifacts."""

    parsed = urlparse(database_url)
    if parsed.hostname is None:
        return "<redacted-database-url>"
    authority = parsed.hostname
    if parsed.port is not None:
        authority = f"{authority}:{parsed.port}"
    return f"{parsed.scheme}://{authority}{parsed.path}"


def _redacted_argv(argv: list[str]) -> list[str]:
    redacted = list(argv)
    for index, value in enumerate(redacted[:-1]):
        if value == "--database-url":
            redacted[index + 1] = _database_descriptor(redacted[index + 1])
    return redacted


def _resource_blocked(error: BaseException) -> int:
    print(
        json.dumps(
            {"status": "RESOURCE_BLOCKED", "reason": str(error)},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 2


def _isolated_stage4_policy(args: argparse.Namespace) -> Stage4InvocationPolicy | None:
    """Return an explicit Planner override without changing production defaults."""

    if args.planner_memory_rounds is None and args.planner_token_budget is None:
        return None
    if args.planner_memory_rounds is not None and args.planner_memory_rounds < 0:
        raise ValueError("planner memory rounds must not be negative")
    if args.planner_token_budget is not None and args.planner_token_budget < 1:
        raise ValueError("planner token budget must be positive")
    policy = _default_stage4_policy(load_production_assembly_spec())
    budget_updates: dict[str, int] = {}
    if args.planner_memory_rounds is not None:
        budget_updates["planner_memory_rounds"] = args.planner_memory_rounds
    if args.planner_token_budget is not None:
        budget_updates["model_token_budget"] = args.planner_token_budget
    return replace(policy, budgets=policy.budgets.model_copy(update=budget_updates))


_CommitScopedRealHybridBackend = CommitScopedRealHybridBackend


def _native_models_module() -> Any:
    try:
        return import_module("native_models")
    except ModuleNotFoundError as error:
        if error.name != "native_models":
            raise
        return import_module("scripts.native_models")


def _real_hybrid_inputs(
    args: argparse.Namespace, request: CreativeRunRequest
) -> tuple[Any, Any, Any, Any, Any]:
    """Build the existing real-hybrid backend only for an isolated run override."""

    if args.retrieval_backend_profile != "real_hybrid":
        return None, None, None, None, None
    native_models = _native_models_module()
    lock = native_models.load_model_lock()
    embedding_model = lock.models["embedding"]
    reranker_model = lock.models["reranker"]
    native_models.assert_model_service(embedding_model)
    native_models.assert_model_service(reranker_model)
    engine = build_engine(args.database_url)
    sessions = build_session_factory(engine)
    attestation = DerivedSnapshotRepository(sessions).get_attestation_for_commit(
        request.basis_commit
    )
    if attestation is None:
        engine.dispose()
        raise RuntimeError(
            "real-hybrid retrieval requires an exact projection attestation for the request basis"
        )
    embedding_run = RunId(f"run.stage2r.runtime.{request.run_id.root}"[:128])
    embedder = HttpEmbeddingProvider(
        RetrievalModelRoute(
            endpoint=args.embedding_url,
            model=embedding_model.model_id,
            revision=embedding_model.revision,
            runtime_fingerprint=embedding_model.runtime_fingerprint,
            run_id=embedding_run,
            task_id=TaskId("task.stage2r.runtime.embedding"),
            trace_id=f"trace.{embedding_run.root}.embedding",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        ),
        dimension=embedding_model.dimension or 0,
        batch_size=32,
    )
    passage_reranker = HttpPassageReranker(
        RetrievalModelRoute(
            endpoint=args.reranker_url,
            model=reranker_model.model_id,
            revision=reranker_model.revision,
            runtime_fingerprint=reranker_model.runtime_fingerprint,
            run_id=embedding_run,
            task_id=TaskId("task.stage2r.runtime.reranker"),
            trace_id=f"trace.{embedding_run.root}.reranker",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        )
    )
    parsed = urlparse(args.opensearch_url)
    if parsed.hostname is None or parsed.port is None:
        engine.dispose()
        raise ValueError("OpenSearch URL must include a host and port")
    search_client = OpenSearch(
        hosts=[{"host": parsed.hostname, "port": parsed.port}],
        use_ssl=parsed.scheme == "https",
        verify_certs=parsed.scheme == "https",
    )
    if not search_client.ping():
        search_client.close()
        engine.dispose()
        raise RuntimeError("OpenSearch is unavailable")
    search_index = OpenSearchIndex(search_client)
    missing_indexes = tuple(
        index.physical_name
        for index in attestation.indexes
        if not search_index.index_exists(index.physical_name)
    )
    if missing_indexes:
        search_client.close()
        engine.dispose()
        raise RuntimeError(f"projection attestation indexes are unavailable: {missing_indexes}")
    try:
        initial_bundle = build_real_hybrid_backend(
            r1=R1WorldRepository(sessions),
            search_index=search_index,
            embedder=embedder,
            project_id=request.project_id,
            source_commit=request.basis_commit,
            snapshot_id=attestation.snapshot_id,
            attestation=attestation,
            reranker=passage_reranker,
        )
    except BaseException:
        search_client.close()
        engine.dispose()
        raise
    projection_builder = FullDerivedProjectionBuilder(
        ArtifactProjectionSourceLoader(
            CommitService(sessions),
            ArtifactRepository(FilesystemObjectStore(args.object_store_root)),
        ),
        R1WorldRepository(sessions),
        Stage2RSearchIndexer(
            search_index,
            embedder,
            embedding_cache=SqlEmbeddingCache(sessions),
            index_namespace=request.run_id.root,
        ),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        build_profile="stage2r-hybrid-v0.1",
        embedding_model=embedding_model.model_id,
        embedding_revision=embedding_model.revision,
        embedding_runtime_fingerprint=ArtifactId(f"sha256:{embedding_model.runtime_fingerprint}"),
        reranker_model=reranker_model.model_id,
        reranker_revision=reranker_model.revision,
    )
    gateway = RealHybridProjectionGateway(
        builder=projection_builder,
        snapshots=DerivedSnapshotRepository(sessions),
        r1=R1WorldRepository(sessions),
        search_index=search_index,
        embedder=embedder,
        reranker=passage_reranker,
    )
    backend = _CommitScopedRealHybridBackend(
        project_id=request.project_id,
        initial_commit=request.basis_commit,
        initial_backend=initial_bundle.backend,
        gateway=gateway,
    )
    return engine, search_client, backend, initial_bundle.reranker, projection_builder


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--object-store-root", type=Path, required=True)
    parser.add_argument(
        "--endpoint-profile",
        help="explicit registered endpoint profile; omit to fail closed without an endpoint",
    )
    parser.add_argument(
        "--assembly-factory",
        default=DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    )
    parser.add_argument("--max-tasks", type=int, required=True)
    parser.add_argument("--max-slices", type=int)
    parser.add_argument(
        "--retrieval-backend-profile",
        choices=("scripted_smoke", "real_hybrid"),
        default="scripted_smoke",
        help="isolated retrieval backend override; omit to keep the production smoke backend",
    )
    parser.add_argument("--opensearch-url", default="http://127.0.0.1:9200")
    parser.add_argument("--embedding-url", default="http://127.0.0.1:8081/v1/embeddings")
    parser.add_argument("--reranker-url", default="http://127.0.0.1:8082/rerank")
    parser.add_argument(
        "--planner-memory-rounds",
        type=int,
        help="isolated Planner memory-round override; omit to keep the production default",
    )
    parser.add_argument(
        "--planner-token-budget",
        type=int,
        help="isolated Planner token-slice override; omit to keep the production default",
    )
    parser.add_argument(
        "--settlement-timeout-seconds",
        type=float,
        help=(
            "isolated Chapter Settlement Curator transport timeout; omit for the production default"
        ),
    )
    parser.add_argument(
        "--settlement-output-tokens",
        type=int,
        help=("isolated Chapter Settlement Curator output cap; omit for the production default"),
    )
    parser.add_argument(
        "--settlement-token-budget",
        type=int,
        help=(
            "isolated Chapter Settlement memory-write token budget; omit for the production default"
        ),
    )
    parser.add_argument(
        "--settlement-max-total-model-calls",
        type=int,
        help=(
            "isolated Chapter Settlement total Curator model-call budget; "
            "omit for the production default"
        ),
    )
    parser.add_argument(
        "--memory-write-validation-only",
        action="store_true",
        help=(
            "isolated U8-C mode: run maintenance proposal/materialization/validation gates "
            "without invoking Canon commit"
        ),
    )
    parser.add_argument(
        "--max-major-rewrites",
        type=int,
        help="isolated Writer major-rewrite allowance; omit for the production default",
    )
    parser.add_argument(
        "--max-local-repairs",
        type=int,
        help="isolated Editor local-repair allowance; omit for the production default",
    )
    parser.add_argument(
        "--stop-after-chapter",
        type=int,
        help="yield after a completed natural chapter boundary without changing the run target",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    request = CreativeRunRequest.model_validate_json(args.request.read_bytes())
    try:
        _assert_object_and_output_roots_disjoint(args.object_store_root, args.output)
        _assert_input_artifacts_present(
            args.object_store_root,
            request.input_artifact_refs + request.continuation_artifact_refs,
        )
    except RuntimeError as error:
        return _resource_blocked(error)
    paths = _artifact_paths(args.output)
    _assert_output_paths_free(paths)
    manifest = load_stage5_manifest(args.manifest)
    stage4_policy = _isolated_stage4_policy(args)
    retrieval_engine, search_client, retrieval_backend, reranker, projection_builder = (
        _real_hybrid_inputs(args, request)
    )
    try:
        assembly = load_production_runtime_assembly(
            args.assembly_factory,
            ProductionAssemblyContext(
                database_url=args.database_url,
                object_store_root=args.object_store_root,
                project_id=request.project_id,
                run_id=request.run_id,
                policy=request.policy,
                manifest=manifest,
                model_endpoints=resolve_registered_model_endpoints(args.endpoint_profile),
                settlement_timeout_seconds=args.settlement_timeout_seconds,
                settlement_output_tokens=args.settlement_output_tokens,
                settlement_token_budget=args.settlement_token_budget,
                settlement_max_total_model_calls=args.settlement_max_total_model_calls,
                memory_write_validation_only=args.memory_write_validation_only,
                max_major_rewrites=args.max_major_rewrites,
                max_local_repairs=args.max_local_repairs,
                stage4_policy=stage4_policy,
                retrieval_backend=retrieval_backend,
                reranker=reranker,
                projection_builder=projection_builder,
            ),
        )
    except RuntimeError as error:
        if "requires registered model endpoints" in str(error):
            return _resource_blocked(error)
        raise
    if assembly.attestation is None:
        raise RuntimeError("production assembly did not provide a resolved attestation")
    _write_json_once(paths["spec"], load_production_assembly_spec().model_dump(mode="json"))
    _write_json_once(paths["attestation"], assembly.attestation.model_dump(mode="json"))
    _write_json_once(
        paths["endpoint_revision"],
        {
            "endpoint_profile": args.endpoint_profile,
            "factory_locator": assembly.attestation.factory_locator,
            "endpoints": [item.model_dump(mode="json") for item in assembly.attestation.endpoints],
        },
    )
    _write_json_once(paths["request"], request.model_dump(mode="json"))
    _write_json_once(
        paths["invocation"],
        {
            "argv": _redacted_argv([str(item) for item in sys.argv]),
            "assembly_factory": args.assembly_factory,
            "endpoint_profile": args.endpoint_profile,
            "settlement_timeout_seconds": args.settlement_timeout_seconds,
            "settlement_output_tokens": args.settlement_output_tokens,
            "settlement_token_budget": args.settlement_token_budget,
            "settlement_max_total_model_calls": args.settlement_max_total_model_calls,
            "memory_write_validation_only": args.memory_write_validation_only,
            "max_major_rewrites": args.max_major_rewrites,
            "max_local_repairs": args.max_local_repairs,
            "planner_memory_rounds": args.planner_memory_rounds,
            "planner_token_budget": args.planner_token_budget,
            "retrieval_backend_profile": args.retrieval_backend_profile,
            "opensearch_url": args.opensearch_url,
            "embedding_url": args.embedding_url,
            "reranker_url": args.reranker_url,
            "database_url": _database_descriptor(args.database_url),
            "object_store_root": str(args.object_store_root.resolve()),
            "output": str(args.output.resolve()),
            "spec_locator": assembly.attestation.factory_locator,
            "session_factory_identity": assembly.attestation.session_factory_identity,
        },
    )
    try:
        report = asyncio.run(
            VerticalCreativeRunner(
                runtime=assembly.runtime,
                dispatcher=assembly.dispatcher,
                tasks=assembly.task_reader,
            ).run(
                request,
                max_tasks=args.max_tasks,
                max_slices=args.max_slices,
                stop_after_chapter=args.stop_after_chapter,
            )
        )
    except (ModelEndpointError, ConnectionError, TimeoutError, OSError) as error:
        return _resource_blocked(error)
    _write_json_once(paths["result"], report.model_dump(mode="json"))
    if search_client is not None:
        search_client.close()
    if retrieval_engine is not None:
        retrieval_engine.dispose()
    print(report.model_dump_json(indent=2))
    if report.status is VerticalRunStatus.BLOCKED:
        return 2
    if report.status is VerticalRunStatus.RECOVERY_PENDING:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
