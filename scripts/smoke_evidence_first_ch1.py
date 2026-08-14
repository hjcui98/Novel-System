#!/usr/bin/env python3
"""Offline evidence-first pipeline run on the real ch1 smoke project.

Exercises the repaired Need admission -> facet-driven retrieval -> exact-L0
packing chains against the REAL ch1 WorldRoot/TextRoot/PlanRoot, R1 records and
real-hybrid OpenSearch indexes produced by the 2026-08-14 model smoke.  Uses the
deterministic template Need path (no Planner/Claim/evaluator model calls), the
same machinery the five-point frozen runner uses, and reports per-Need facet
receipts, retrieval pages, stop reasons, package status and mandatory-facet
closure.

Usage:
  SMOKE_DATABASE_URL=... [SMOKE_PROJECT_DIRECTORY=... SMOKE_EXPERIMENT_ID=...] \
    .conda-env/bin/python scripts/smoke_evidence_first_ch1.py
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import (
    HttpEmbeddingProvider,
    HttpPassageReranker,
    RetrievalModelRoute,
)
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRole
from novel_agent.domain.planning_memory import (
    AuthorPlanningContext,
    PlannerArtifactMetadata,
    PlannerFallbackStatus,
    PlannerInvocationArtifact,
)
from novel_agent.domain.stage2 import BenchmarkInformationProfile
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import content_id
from novel_agent.services.embedding_cache import SqlEmbeddingCache
from novel_agent.services.evidence_first_checkpoint_runner import EvidenceFirstCheckpointRunner
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.plan_conditioned_need_planner import (
    PlannerWorldSummaryBuilder,
)
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedSnapshotRepository,
    FullDerivedProjectionBuilder,
)
from novel_agent.services.r1 import R1WorldRepository

try:
    from scripts.native_models import load_model_lock
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from native_models import load_model_lock  # type: ignore[import-not-found,no-redef]
from novel_agent.services.search_retrieval import Stage2RSearchIndexer
from novel_agent.services.stage2_retrieval_backend import (
    RealHybridProjectionGateway,
    Stage2RetrievalBackendBundle,
)
from novel_agent.services.task_conditioned_need_generation import (
    TaskPlanConditionedNeedGenerator,
)

PROJECT_ROOT = Path(__file__).parents[1]
PROJECT_DIRECTORY = PROJECT_ROOT / os.environ.get(
    "SMOKE_PROJECT_DIRECTORY", "tmp/smoke-20260813-v1"
)
DATABASE_URL = os.environ["SMOKE_DATABASE_URL"]
PROJECT_ID = ProjectId("ztj_volume01_preview")
CASE_INPUT = PROJECT_ROOT / "benchmarks/private/ztj_memory_pilot_v0.1/cases/ZTJ-P001/input.yaml"
MODEL_BASE = "http://127.0.0.1:8003/v1"
EMBEDDING_URL = os.environ.get("NOVEL_AGENT_EMBEDDING_URL", "http://127.0.0.1:8081/v1/embeddings")
RERANKER_URL = os.environ.get("NOVEL_AGENT_RERANKER_URL", "http://127.0.0.1:8082/rerank")
OPENSEARCH_URL = "http://127.0.0.1:9200"
EXPERIMENT_ID = os.environ.get("SMOKE_EXPERIMENT_ID", "stage2m-semantic-smoke-v1-20260813")


def _case_task_intent() -> str:
    import yaml

    raw = yaml.safe_load(CASE_INPUT.read_text(encoding="utf-8"))
    intent = str(raw.get("task", "")).strip()
    if not intent:
        raise SystemExit(f"case input {CASE_INPUT} has no non-empty task intent")
    return intent


def main() -> int:
    engine = build_engine(DATABASE_URL)
    session_factory = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(PROJECT_DIRECTORY / "objects"))
    commits = CommitService(session_factory)
    chain = _commit_chain(commits)
    latest = chain[-1]
    manifest = commits.load_manifest(latest)
    text = TextRootDocument.model_validate_json(
        artifacts.read_verified(manifest.text_root).decode("utf-8"), strict=True
    )
    world = WorldRootDocument.model_validate_json(
        artifacts.read_verified(manifest.world_root).decode("utf-8"), strict=True
    )
    # The genesis WorldRoot is authored before any commit; rebind it to the
    # checkpoint basis so generated Needs route against the exact snapshot.
    world = world.model_copy(update={"source_commit": latest})
    plan = PlanRootDocument.model_validate_json(
        artifacts.read_verified(manifest.plan_root).decode("utf-8"), strict=True
    )
    print(f"basis commit: {latest.root[:20]}")
    print(
        f"world: entities={len(world.entities)} states={len(world.states)} "
        f"relations={len(world.relations)} events={len(world.events)} "
        f"obligations={len(world.obligations)}"
    )

    task_intent = _case_task_intent()
    task = build_safe_task_contract(
        case_id=StableId("smoke-c20"),
        checkpoint_chapter=int(os.environ.get("SMOKE_CHECKPOINT_CHAPTER", "1")),
        target_range=(
            int(os.environ.get("SMOKE_TARGET_START", "2")),
            int(os.environ.get("SMOKE_TARGET_END", "6")),
        ),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent=task_intent,
    )
    planning_context = AuthorPlanningContext(
        profile=task.information_profile,
        task_intent=task.task_intent,
        target_range=(task.target_chapter_start, task.target_chapter_end),
        visible_outline_nodes=(),
        chapter_goals=plan.chapter_goals,
        source_hash=ArtifactId("sha256:" + "a" * 64),
        planner_may_read_plan=True,
    )

    # Deterministic template Needs (no Planner model call).
    needs = TaskPlanConditionedNeedGenerator().generate(task, world, plan)
    if not needs:
        raise SystemExit("deterministic template need generation produced no needs")
    print(f"template needs: {len(needs)}")

    world_summary = PlannerWorldSummaryBuilder.build(task, world, planning_context)
    metadata = PlannerArtifactMetadata(
        run_id=RunId("run.smoke-evidence-first"),
        planner_model="qwen36-27b-nvfp4",
        planner_model_revision="smoke",
        planner_prompt_version="smoke.v1",
        planner_prompt_hash=ArtifactId("sha256:" + "b" * 64),
        planner_output_schema_version="smoke.v1",
        temperature=0.0,
        effective_seed_supported=False,
        planning_context_hash=planning_context.source_hash,
        world_summary_hash=content_id(world_summary.model_dump(mode="json")),
        raw_response_hash=ArtifactId("sha256:" + "e" * 64),
        validated_need_set_hash=content_id([need.model_dump(mode="json") for need in needs]),
        fallback_used=True,
        input_tokens=0,
        output_tokens=0,
    )
    fallback_artifact = PlannerInvocationArtifact(
        planning_context=planning_context,
        world_summary=world_summary,
        exact_prompt="smoke fallback",
        metadata=metadata,
        validated_need_set_hash=metadata.validated_need_set_hash,
        fallback_status=PlannerFallbackStatus.PLANNER_FALLBACK,
        fallback_reason="smoke_offline_fallback",
    )

    lock = load_model_lock()
    embedding_model = lock.models["embedding"]
    reranker_model = lock.models["reranker"]
    embedding_profile = RetrievalModelRoute(
        endpoint=EMBEDDING_URL,
        model=embedding_model.model_id,
        revision=embedding_model.revision,
        runtime_fingerprint=embedding_model.runtime_fingerprint,
        run_id=RunId("run.smoke-evidence-first"),
        task_id=TaskId("task.smoke-evidence-first"),
        trace_id="trace.smoke-evidence-first",
        span_id=None,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.EVALUATION,
        timeout_seconds=300,
    )
    embedder = HttpEmbeddingProvider(embedding_profile, dimension=1024, batch_size=32)
    reranker = HttpPassageReranker(
        RetrievalModelRoute(
            endpoint=RERANKER_URL,
            model=reranker_model.model_id,
            revision=reranker_model.revision,
            runtime_fingerprint=reranker_model.runtime_fingerprint,
            run_id=RunId("run.smoke-evidence-first"),
            task_id=TaskId("task.smoke-evidence-first"),
            trace_id="trace.smoke-evidence-first",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        )
    )
    from opensearchpy import OpenSearch

    client = OpenSearch(hosts=[{"host": "127.0.0.1", "port": 9200}])
    r1 = R1WorldRepository(session_factory)
    search_index = OpenSearchIndex(client)
    builder = FullDerivedProjectionBuilder(
        ArtifactProjectionSourceLoader(commits, artifacts),
        r1,
        Stage2RSearchIndexer(
            search_index,
            embedder,
            embedding_cache=SqlEmbeddingCache(session_factory),
            index_namespace=EXPERIMENT_ID,
        ),
        retrieval_backend_profile="real_hybrid",
        build_profile="stage2r-hybrid-v0.1",
        embedding_model="BAAI/bge-m3",
        embedding_revision="5617a9f61b028005a4858fdac845db406aefb181",
        embedding_runtime_fingerprint=ArtifactId("sha256:" + embedding_model.runtime_fingerprint),
        reranker_model="BAAI/bge-reranker-v2-m3",
        reranker_revision="953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
    )
    gateway = RealHybridProjectionGateway(
        builder=builder,
        snapshots=DerivedSnapshotRepository(session_factory),
        r1=r1,
        search_index=search_index,
        embedder=embedder,
        reranker=reranker,
    )
    backend_bundle: Stage2RetrievalBackendBundle = gateway.backend_for(PROJECT_ID, latest)
    snapshot_id = backend_bundle.attestation.snapshot_id
    print(
        f"attestation: snapshot={snapshot_id.root[:24]} channels={backend_bundle.allowed_channels}"
    )

    runner = EvidenceFirstCheckpointRunner()
    result = runner.run(
        case_id=PROJECT_ID,
        task=task,
        world=world,
        text=text,
        plan=plan,
        base_commit=latest,
        snapshot_id=snapshot_id,
        planning_context=planning_context,
        frozen_planner_artifact=fallback_artifact,
        frozen_needs=needs,
        backend_bundle=backend_bundle,
        fingerprint=ArtifactId("sha256:" + hashlib.sha256(b"smoke-evidence-first").hexdigest()),
        run_id=StableId("run.smoke-evidence-first.ch1"),
    )
    print("=== evidence-first pipeline result ===")
    print("stop_reason:", result.stop_reason)
    print("retrieval_call_count:", result.retrieval_call_count)
    print("future_leakage_count:", result.future_leakage_count)
    print("assembly status:", result.assembly.status.value)
    print("mandatory_facet_closure:", result.assembly.mandatory_facet_closure)
    print("diagnostics:", result.assembly.diagnostic_codes)
    print("package items:", len(result.assembly.package.items))
    for record in result.trace_records:
        print(
            f"  need={record['need_id']} intent={record['intent']} "
            f"stop={record['stop_reason']} pages={record['retrieval_pages']} "
            f"facets={[r['facet_kind'] + ':' + r['status'] for r in record['facet_receipts']]}"
        )
    engine.dispose()
    return 0


def _commit_chain(commits: CommitService) -> list[CommitId]:
    head = commits.current_commit(PROJECT_ID)
    chain: list[CommitId] = []
    node: CommitId | None = head
    seen: set[str] = set()
    while node is not None and node.root not in seen:
        seen.add(node.root)
        chain.append(node)
        manifest = commits.load_manifest(node)
        parents = manifest.parent_commit_ids
        node = parents[0] if parents else None
    return list(reversed(chain))


if __name__ == "__main__":
    raise SystemExit(main())
