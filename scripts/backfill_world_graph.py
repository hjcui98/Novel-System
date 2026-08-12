#!/usr/bin/env python3
"""Repair one immutable World/Text basis through the canonical write/projection corridor."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import ParseResult, urlparse

from sqlalchemy import create_engine, func, select

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import HttpEmbeddingProvider, RetrievalModelRoute
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import Base, build_engine, build_session_factory
from novel_agent.adapters.postgres.models import R1RecordRow
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.changes import CommitRequest, CommitStatus, ValidationStatus
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import (
    GraphPathDereferenceStatus,
    GraphPathReceipt,
    WorldRootDocument,
)
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelCallRecord,
    ModelRequest,
    ModelRole,
)
from novel_agent.domain.retrieval_routing import RetrievalBackendProfile
from novel_agent.domain.world import WorldGraphCandidateBatch
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.embedding_cache import SqlEmbeddingCache
from novel_agent.services.model_curation import ModelCurator
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.overlay import build_candidate_bundle
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedSnapshotRepository,
    FullDerivedProjectionBuilder,
    ProjectionSource,
)
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.search_retrieval import (
    DeterministicHashEmbedder,
    Stage1SearchIndexer,
    Stage2RSearchIndexer,
)
from novel_agent.services.validation import Stage1Validator
from novel_agent.services.world_graph import WorldGraphExtractionPass


class _WorkspaceSearchIndex:
    """Small offline SearchIndexPort used only by the JSON diagnostic mode."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, dict[str, Any]]] = {}
        self.aliases: dict[str, str] = {}

    def ensure_index(self, index: str, mapping: dict[str, Any]) -> None:
        del mapping
        self.documents.setdefault(index, {})

    def index_document(self, index: str, document_id: str, document: dict[str, Any]) -> None:
        self.documents[index][document_id] = document

    def bulk_index(
        self,
        index: str,
        documents: tuple[tuple[str, dict[str, Any]], ...],
    ) -> None:
        self.documents[index].update(dict(documents))

    def refresh(self, index: str) -> None:
        if index not in self.documents:
            raise LookupError(index)

    def publish_alias(self, index: str, alias: str) -> None:
        self.publish_aliases(((index, alias),))

    def publish_aliases(self, bindings: tuple[tuple[str, str], ...]) -> None:
        for index, alias in bindings:
            self.aliases[alias] = index

    def delete_index(self, index: str) -> None:
        self.documents.pop(index, None)

    def get_document(self, index: str, document_id: str) -> dict[str, Any] | None:
        physical = self.aliases.get(index, index)
        return self.documents.get(physical, {}).get(document_id)

    def search(
        self,
        index: str,
        query: dict[str, Any],
        *,
        size: int,
    ) -> tuple[dict[str, Any], ...]:
        del query
        physical = self.aliases.get(index, index)
        return tuple(
            {"_id": document_id, "_source": document, "_score": 1.0}
            for document_id, document in tuple(self.documents.get(physical, {}).items())[:size]
        )


class _RepairProjectionLoader:
    def __init__(
        self,
        manifest: RootManifest,
        text: TextRootDocument,
        plan: PlanRootDocument | None,
        world: WorldRootDocument,
    ) -> None:
        self._source = ProjectionSource(manifest=manifest, text=text, plan=plan, world=world)

    def load(self, source_commit: CommitId) -> ProjectionSource:
        del source_commit
        return self._source


def _artifact_ref(
    repository: ArtifactRepository,
    payload: bytes,
    media_type: str,
) -> ArtifactRef:
    return repository.put(payload, media_type, SchemaVersion("1.0.0"))


def _load_candidate_batches(path: Path | None) -> tuple[WorldGraphCandidateBatch, ...]:
    if path is None:
        return ()
    raw = json.loads(path.read_text("utf-8"))
    values = raw if isinstance(raw, list) else [raw]
    return tuple(WorldGraphCandidateBatch.model_validate(item) for item in values)


async def _model_batches(
    args: argparse.Namespace,
    text: TextRootDocument,
    world: WorldRootDocument,
    base_commit: CommitId,
) -> tuple[tuple[WorldGraphCandidateBatch, ...], tuple[ModelCallRecord, ...]]:
    if args.model_base_url is None:
        return (), ()
    if not args.model or not args.chapter:
        raise ValueError("--model-base-url requires --model and at least one --chapter")
    from novel_agent.adapters.model import OpenAICompatibleChatEndpoint

    endpoint = OpenAICompatibleChatEndpoint(
        base_url=args.model_base_url,
        model=args.model,
        max_output_tokens=args.model_max_output_tokens,
    )
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name=args.model_base_url,
                model_name=args.model,
                adapter=endpoint,
            ),
        ),
        structured_max_retries=1,
    )
    curator = ModelCurator(gateway, enable_model_semantic_verifier=True)
    batches: list[WorldGraphCandidateBatch] = []
    calls: list[ModelCallRecord] = []
    for chapter in args.chapter:
        request = ModelRequest(
            request_id=StableId(f"request.world-graph.{chapter}"),
            run_id=RunId(f"run.world-graph.{args.repair_id}"),
            task_id=TaskId(f"task.world-graph.{chapter}"),
            model_role=ModelRole.IMPLEMENTATION,
            purpose=ModelCallPurpose.DEVELOPMENT,
            trace_id=f"trace.world-graph.{args.repair_id}.{chapter}",
            prompt="",
            max_output_tokens=args.model_max_output_tokens,
            timeout_seconds=args.model_timeout_seconds,
        )
        batch, call = await curator.extract_graph_candidates(
            text,
            chapter,
            base_commit,
            world,
            request,
        )
        batches.append(batch)
        calls.append(call)
    audited_calls = tuple(gateway.call_records)
    if not audited_calls:
        audited_calls = tuple(calls)
    return tuple(batches), audited_calls


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world-root", type=Path)
    parser.add_argument("--text-root", type=Path)
    parser.add_argument("--plan-root", type=Path)
    parser.add_argument("--checkpoint-plan-root", type=Path)
    parser.add_argument("--source-project", type=Path)
    parser.add_argument("--source-database-url")
    parser.add_argument("--source-commit")
    parser.add_argument("--candidate-batches", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repair-id", required=True)
    parser.add_argument("--model-base-url")
    parser.add_argument("--model")
    parser.add_argument("--chapter", type=int, action="append")
    parser.add_argument("--model-max-output-tokens", type=int, default=4096)
    parser.add_argument("--model-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--real-hybrid", action="store_true")
    parser.add_argument("--opensearch-url", default="http://127.0.0.1:9200")
    parser.add_argument("--embedding-url", default="http://127.0.0.1:8281/v1/embeddings")
    return parser


def _loopback_url(value: str, label: str) -> ParseResult:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
    ):
        raise ValueError(f"{label} must use an explicit loopback HTTP(S) URL")
    return parsed


def main() -> int:
    args = build_parser().parse_args()
    if args.output_dir.exists():
        raise FileExistsError("graph backfill output identity already exists")
    project_mode = args.source_project is not None
    json_mode = args.world_root is not None or args.text_root is not None
    if project_mode == json_mode:
        raise ValueError(
            "choose either --source-project/--source-database-url/--source-commit "
            "or --world-root/--text-root"
        )
    if args.checkpoint_plan_root is not None and not project_mode:
        raise ValueError("--checkpoint-plan-root requires source-project mode")
    source_engine: Any | None = None
    source_repository: ArtifactRepository | None = None
    source_manifest: RootManifest | None = None
    source_head_before: CommitId | None = None
    source_project_id: ProjectId | None = None
    selected_source_commit: CommitId | None = None
    source_plan: PlanRootDocument | None = None
    source_plan_bytes: bytes | None = None
    if project_mode:
        if args.source_database_url is None or args.source_commit is None:
            raise ValueError(
                "source-project mode requires --source-database-url and --source-commit"
            )
        source_objects = args.source_project.resolve() / "objects"
        if not source_objects.is_dir():
            raise ValueError(f"source project has no object store: {source_objects}")
        source_engine = build_engine(args.source_database_url)
        source_factory = build_session_factory(source_engine)
        source_repository = ArtifactRepository(FilesystemObjectStore(source_objects))
        source_commits = CommitService(source_factory)
        source_commit = CommitId(args.source_commit)
        selected_source_commit = source_commit
        source = ArtifactProjectionSourceLoader(source_commits, source_repository).load(
            source_commit
        )
        source_manifest = source.manifest
        source_project_id = source.manifest.project_id
        source_head_before = source_commits.current_commit(source_project_id)
        world = source.world
        text = source.text
        source_plan = source.plan
        plan = source_plan
        world_bytes = source_repository.read_verified(source.manifest.world_root)
        text_bytes = source_repository.read_verified(source.manifest.text_root)
        source_plan_bytes = source_repository.read_verified(source.manifest.plan_root)
        if args.checkpoint_plan_root is not None:
            plan_bytes = args.checkpoint_plan_root.read_bytes()
            plan = PlanRootDocument.model_validate_json(plan_bytes, strict=True)
        else:
            plan_bytes = source_plan_bytes
        reference_bytes = source_repository.read_verified(source.manifest.reference_root)
        profile_bytes = source_repository.read_verified(source.manifest.project_profile_root)
    else:
        if args.world_root is None or args.text_root is None:
            raise ValueError("JSON mode requires --world-root and --text-root")
        world_bytes = args.world_root.read_bytes()
        text_bytes = args.text_root.read_bytes()
        plan_bytes = args.plan_root.read_bytes() if args.plan_root is not None else b"{}"
        world = WorldRootDocument.model_validate_json(world_bytes, strict=True)
        text = TextRootDocument.model_validate_json(text_bytes, strict=True)
        plan = (
            PlanRootDocument.model_validate_json(plan_bytes, strict=True)
            if args.plan_root is not None
            else None
        )
        reference_bytes = b'{"kind":"empty_reference_root"}'
        profile_bytes = b'{"kind":"empty_profile_root"}'

    args.output_dir.mkdir(parents=True)
    objects = args.output_dir / "objects"
    repository = ArtifactRepository(FilesystemObjectStore(objects))
    engine = create_engine(f"sqlite+pysqlite:///{args.output_dir / 'repair.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    project_id = ProjectId(f"project.world-graph-repair.{args.repair_id}")
    search_client: Any | None = None
    try:
        text_artifact = _artifact_ref(
            repository,
            text_bytes,
            "application/vnd.novel-agent.text-root+json",
        )
        plan_artifact = _artifact_ref(
            repository,
            plan_bytes,
            "application/vnd.novel-agent.plan-root+json",
        )
        world_artifact = _artifact_ref(
            repository,
            world_bytes,
            "application/vnd.novel-agent.world-root+json",
        )
        reference_artifact = _artifact_ref(
            repository,
            reference_bytes,
            (
                source_manifest.reference_root.media_type
                if source_manifest is not None
                else "application/json"
            ),
        )
        profile_artifact = _artifact_ref(
            repository,
            profile_bytes,
            (
                source_manifest.project_profile_root.media_type
                if source_manifest is not None
                else "application/json"
            ),
        )
        genesis_manifest = RootManifest(
            project_id=project_id,
            schema_version=SchemaVersion("1.0.0"),
            text_root=TextRootRef.model_validate(text_artifact.model_dump(mode="json")),
            plan_root=PlanRootRef.model_validate(plan_artifact.model_dump(mode="json")),
            world_root=WorldRootRef.model_validate(world_artifact.model_dump(mode="json")),
            reference_root=ReferenceRootRef.model_validate(
                reference_artifact.model_dump(mode="json")
            ),
            project_profile_root=ProjectProfileRootRef.model_validate(
                profile_artifact.model_dump(mode="json")
            ),
        )
        commits = CommitService(factory)
        repair_base = commits.initialize_project(genesis_manifest)
        supplied_batches = tuple(
            batch.model_copy(
                update={"source_text_root": text.root_hash, "base_commit": repair_base}
            )
            for batch in _load_candidate_batches(args.candidate_batches)
        )
        model_batches, model_calls = asyncio.run(_model_batches(args, text, world, repair_base))
        extraction = WorldGraphExtractionPass().run(
            world,
            text,
            candidate_batches=(*supplied_batches, *model_batches),
            base_commit=repair_base,
        )
        candidate_payload = canonical_json_bytes(
            [batch.model_dump(mode="json") for batch in extraction.candidate_batches]
        )
        candidate_artifact = _artifact_ref(
            repository,
            candidate_payload,
            "application/vnd.novel-agent.world-graph-candidates+json",
        )
        if candidate_artifact != extraction.change_set.source_artifact:
            raise RuntimeError("candidate CAS identity differs from ObservedChangeSet source")
        repaired_bytes = canonical_json_bytes(extraction.repaired_world.model_dump(mode="json"))
        repaired_artifact = _artifact_ref(
            repository,
            repaired_bytes,
            "application/vnd.novel-agent.world-root+json",
        )
        bundle = build_candidate_bundle(
            project_id=project_id,
            run_id=RunId(f"run.world-graph-repair.{args.repair_id}"),
            current_manifest=genesis_manifest,
            changes=extraction.change_set,
            proposed_world=extraction.repaired_world,
        )
        report = Stage1Validator().validate(
            bundle,
            world,
            extraction.repaired_world,
            text,
            canonical_commit=repair_base,
        )
        if report.status is not ValidationStatus.PASSED:
            raise RuntimeError(f"repair validation failed: {report.model_dump(mode='json')}")
        commit_result = commits.commit(
            CommitRequest(
                request_id=StableId(f"commit-request.world-graph.{args.repair_id}"),
                project_id=project_id,
                base_commit=repair_base,
                idempotency_key=StableId(f"idempotency.world-graph.{args.repair_id}"),
                bundle=bundle,
                validation_report=report,
            )
        )
        if commit_result.status is not CommitStatus.ACCEPTED or commit_result.commit_id is None:
            raise RuntimeError(f"repair commit failed: {commit_result.reason}")
        repair_commit = commit_result.commit_id
        committed_manifest = commits.load_manifest(repair_commit)
        if committed_manifest.world_root.artifact_id != repaired_artifact.artifact_id:
            raise RuntimeError("repair commit does not bind the repaired WorldRoot")

        r1 = R1WorldRepository(factory)
        projection_kwargs: dict[str, Any] = {}
        if args.real_hybrid:
            try:
                from scripts.native_models import assert_model_service, load_model_lock
            except ModuleNotFoundError:  # pragma: no cover - direct script execution
                from native_models import (  # type: ignore[import-not-found,no-redef]
                    assert_model_service,
                    load_model_lock,
                )
            from opensearchpy import OpenSearch

            lock = load_model_lock()
            embedding_model = lock.models["embedding"]
            reranker_model = lock.models["reranker"]
            assert_model_service(embedding_model)
            assert_model_service(reranker_model)
            embedding_target = _loopback_url(args.embedding_url, "embedding")
            search_target = _loopback_url(args.opensearch_url, "OpenSearch")
            projection_run = RunId(f"run.world-graph-projection.{args.repair_id}")
            embedder = HttpEmbeddingProvider(
                RetrievalModelRoute(
                    endpoint=embedding_target.geturl(),
                    model=embedding_model.model_id,
                    revision=embedding_model.revision,
                    runtime_fingerprint=embedding_model.runtime_fingerprint,
                    run_id=projection_run,
                    task_id=TaskId(f"task.world-graph-projection.{args.repair_id}"),
                    trace_id=f"trace.{projection_run.root}",
                    span_id=None,
                    model_role=ModelRole.BATCH_TEST,
                    purpose=ModelCallPurpose.EVALUATION,
                    timeout_seconds=300,
                ),
                dimension=embedding_model.dimension or 0,
                batch_size=32,
            )
            search_client = OpenSearch(
                hosts=[{"host": search_target.hostname, "port": search_target.port}],
                use_ssl=search_target.scheme == "https",
                verify_certs=search_target.scheme == "https",
            )
            if not search_client.ping():
                raise RuntimeError("OpenSearch is unavailable")
            search_indexer: Stage1SearchIndexer = Stage2RSearchIndexer(
                OpenSearchIndex(search_client),
                embedder,
                embedding_cache=SqlEmbeddingCache(factory),
                index_namespace=args.repair_id,
            )
            retrieval_profile = RetrievalBackendProfile.REAL_HYBRID
            projection_kwargs = {
                "embedding_model": embedding_model.model_id,
                "embedding_revision": embedding_model.revision,
                "embedding_runtime_fingerprint": ArtifactId(
                    f"sha256:{embedding_model.runtime_fingerprint}"
                ),
                "reranker_model": reranker_model.model_id,
                "reranker_revision": reranker_model.revision,
            }
        else:
            search_adapter = _WorkspaceSearchIndex()
            search_indexer = Stage1SearchIndexer(
                cast(OpenSearchIndex, search_adapter),
                DeterministicHashEmbedder(),
            )
            retrieval_profile = RetrievalBackendProfile.SCRIPTED_SMOKE
        projection = FullDerivedProjectionBuilder(
            _RepairProjectionLoader(
                committed_manifest,
                text,
                plan,
                extraction.repaired_world,
            ),
            r1,
            search_indexer,
            retrieval_backend_profile=retrieval_profile,
            build_profile="stage2m-world-graph-repair.v1",
            **projection_kwargs,
        ).build(project_id, repair_commit)
        snapshots = DerivedSnapshotRepository(factory)
        snapshots.publish_rebuilt(project_id, projection)
        if snapshots.get_for_commit(repair_commit) != projection:
            raise RuntimeError("published repair snapshot differs from verified projection")
        record_count, association_count, graph_edge_count = r1.counts(repair_commit)
        with factory() as session:
            relation_row_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(R1RecordRow)
                    .where(
                        R1RecordRow.source_commit == repair_commit.root,
                        R1RecordRow.record_kind == "relation",
                    )
                )
                or 0
            )
        if graph_edge_count != relation_row_count:
            raise RuntimeError("R1 graph_edge_count differs from visible relation rows")
        accepted = tuple(
            candidate
            for candidate in extraction.receipt.candidates
            if candidate.relation_id is not None and candidate.status.value == "accepted"
        )
        graph_receipts: tuple[GraphPathReceipt, ...] = ()
        if accepted:
            subject_id = accepted[0].subject_id
            if subject_id is None:
                raise RuntimeError("accepted relation receipt has no subject")
            graph_receipts = r1.typed_graph_paths(
                repair_commit,
                (subject_id,),
                max_depth=3,
                limit=20,
                snapshot_id=projection.snapshot_id,
            )
            graph_receipts = r1.validate_graph_path_receipts(graph_receipts, text)
            if not graph_receipts or any(
                item.dereference_status is not GraphPathDereferenceStatus.L0_VERIFIED
                for item in graph_receipts
            ):
                raise RuntimeError("typed graph receipt did not verify exact L0 evidence")

        (args.output_dir / "repaired_world_root.json").write_bytes(repaired_bytes)
        (args.output_dir / "world_graph_extraction_receipt.json").write_bytes(
            canonical_json_bytes(extraction.receipt.model_dump(mode="json"))
        )
        (args.output_dir / "validation_report.json").write_bytes(
            canonical_json_bytes(report.model_dump(mode="json"))
        )
        (args.output_dir / "derived_snapshot.json").write_bytes(
            canonical_json_bytes(projection.model_dump(mode="json"))
        )
        if projection.projection_attestation is not None:
            (args.output_dir / "projection_attestation.json").write_bytes(
                canonical_json_bytes(projection.projection_attestation)
            )
        (args.output_dir / "graph_path_receipts.json").write_bytes(
            canonical_json_bytes([item.model_dump(mode="json") for item in graph_receipts])
        )
        (args.output_dir / "model_call_records.json").write_bytes(
            canonical_json_bytes([item.model_dump(mode="json") for item in model_calls])
        )
        candidate_count = sum(
            len(batch.entities) + len(batch.relations) for batch in extraction.candidate_batches
        )
        accounted = len(extraction.receipt.entity_admissions) + len(extraction.receipt.candidates)
        if candidate_count != accounted:
            raise RuntimeError("graph candidate accounting is not closed")
        if source_repository is not None and source_manifest is not None:
            assert source_engine is not None
            source_commits_after = CommitService(build_session_factory(source_engine))
            source_unchanged = (
                source_project_id is not None
                and source_head_before == source_commits_after.current_commit(source_project_id)
                and source_repository.read_verified(source_manifest.world_root) == world_bytes
                and source_repository.read_verified(source_manifest.text_root) == text_bytes
                and source_plan_bytes is not None
                and source_repository.read_verified(source_manifest.plan_root) == source_plan_bytes
                and source_repository.read_verified(source_manifest.reference_root)
                == reference_bytes
                and source_repository.read_verified(source_manifest.project_profile_root)
                == profile_bytes
            )
        else:
            assert args.world_root is not None and args.text_root is not None
            source_unchanged = (
                args.world_root.read_bytes() == world_bytes
                and args.text_root.read_bytes() == text_bytes
                and (args.plan_root is None or args.plan_root.read_bytes() == plan_bytes)
            )
        if not source_unchanged:
            raise RuntimeError("source root bytes changed during isolated repair")
        manifest = {
            "status": "world_graph_repair_completed",
            "repair_id": args.repair_id,
            "project_id": project_id.root,
            "source_commit": world.source_commit.root,
            "selected_source_commit": (
                selected_source_commit.root if selected_source_commit is not None else None
            ),
            "repair_base_commit": repair_base.root,
            "repair_commit": repair_commit.root,
            "source_world_root": world.root_hash.root,
            "source_text_root": text.root_hash.root,
            "source_plan_root": source_plan.root_hash.root if source_plan is not None else None,
            "checkpoint_plan_root": plan.root_hash.root if plan is not None else None,
            "repaired_world_root": extraction.repaired_world.root_hash.root,
            "change_set_id": extraction.change_set.change_set_id.root,
            "validation_report_id": report.report_id.root,
            "source_batch_ids": [item.root for item in extraction.receipt.source_batch_ids],
            "model_request_ids": [
                batch.model_request_id.root
                for batch in extraction.candidate_batches
                if batch.model_request_id is not None
            ],
            "candidate_count": candidate_count,
            "accepted_count": extraction.receipt.accepted_count,
            "rejected_count": extraction.receipt.rejected_count,
            "deduped_count": extraction.receipt.deduped_count,
            "r1_record_count": record_count,
            "r1_entity_association_count": association_count,
            "r1_relation_row_count": relation_row_count,
            "graph_edge_count": graph_edge_count,
            "snapshot_id": projection.snapshot_id.root,
            "anchor_index": projection.anchor_index_version,
            "grounded_index": projection.grounded_index_version,
            "projection_attestation_available": projection.projection_attestation is not None,
            "retrieval_backend_profile": projection.retrieval_backend_profile,
            "graph_path_receipt_count": len(graph_receipts),
            "graph_paths_l0_verified": all(
                item.dereference_status is GraphPathDereferenceStatus.L0_VERIFIED
                for item in graph_receipts
            ),
            "source_roots_unchanged": source_unchanged,
            "completed_at": datetime.now(UTC).isoformat(),
        }
        (args.output_dir / "repair_manifest.json").write_bytes(canonical_json_bytes(manifest))
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        if search_client is not None:
            search_client.close()
        engine.dispose()
        if source_engine is not None:
            source_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
