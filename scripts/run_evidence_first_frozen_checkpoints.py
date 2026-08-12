#!/usr/bin/env python3
"""Evidence-first frozen five-checkpoint Writing Package run (plan §6).

Reuses the frozen C20/C40/C60/C80/C95 Commit/World/TextRoot/Plan and the
frozen real-hybrid snapshot indexes; runs the public
Need -> Retrieval/Rank -> Exact L0 Slice Selection -> Package/Ledger ->
Freeze/Export pipeline with zero Planner / Claim Support / whole-verifier /
semantic-evaluator model calls.  The database is opened read-only for the
commit chain and derived-snapshot attestations; nothing is published or
rebuilt.

Usage:
  .conda-env/bin/python scripts/run_evidence_first_frozen_checkpoints.py \
    --source-project /tmp/ns-stage2m-phase4-v33-apc-20260810 \
    --output-root /tmp/ns-stage2m-evidence-first-five-20260811-v1 \
    --database-url postgresql+psycopg://...@127.0.0.1:5432/na_s2m_phase4_v33_apc_v1 \
    --experiment-id stage2m-evidence-first-v1-20260811 \
    --case P001 --case P002 --case P003 --case P004 --case P005
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import (
    HttpEmbeddingProvider,
    HttpPassageReranker,
    RetrievalModelRoute,
)
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.artifacts import ArtifactRef, PlanRootRef
from novel_agent.domain.benchmark import BenchmarkBundle, PlanRootDocument, TextRootDocument
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import Stage1MemoryNeed, WorldRootDocument
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRole
from novel_agent.domain.planning_memory import PlannerInvocationArtifact
from novel_agent.domain.retrieval_routing import SnapshotCapabilityStatus
from novel_agent.domain.stage2 import BenchmarkInformationProfile
from novel_agent.domain.writer_context import (
    EvidenceFirstPackageManifest,
    EvidenceLedgerV2,
    WriterContextPackageV2,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.evidence_first_checkpoint_runner import EvidenceFirstCheckpointRunner
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_contract import build_public_checkpoint_case
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedSnapshotRepository,
)
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.stage2_retrieval_backend import (
    Stage2RetrievalBackendBundle,
    build_real_hybrid_backend,
)

CASES: dict[str, dict[str, str]] = {
    "P001": {
        "case_id": "ZTJ-P001",
        "chapter": "20",
        "commit": "sha256:9ad34064a1343e2e5ee89296e7cbafa8ba9dfcdf385cfbd74ff3b2ccfb7432d6",
        "paired": "sha256:d495e6b9f711f70ccfdaf5c278862ffb50ee82798bf07f123abbbe05970af018",
    },
    "P002": {
        "case_id": "ZTJ-P002",
        "chapter": "40",
        "commit": "sha256:378d71e6cb211782dff5cde651ac96fa55d8e83d600a6e1e7e92228b6046a0d6",
        "paired": "sha256:41bfc516d7c3cffbec8eaa58b78f39d2a8e4db286b8c9ca53e584bdb5e9a3cfe",
    },
    "P003": {
        "case_id": "ZTJ-P003",
        "chapter": "60",
        "commit": "sha256:86c060c6f10b9cf4d7a47618f7e0f339ec9adc5e2d33c2461ecb3ad1286e4bd0",
        "paired": "sha256:569ad56b9f51110d40a5567ac96c85f96b9ef488c626eb90cf6b00d40357a3cd",
    },
    "P004": {
        "case_id": "ZTJ-P004",
        "chapter": "80",
        "commit": "sha256:ba7c17cd3f91c47f425f68b26cf77471c8029c757cfb64622fadf1ba22dca57d",
        "paired": "sha256:2f95d13bc8cdcd1243fcf0414bf8ade27427dd83e2b0781314dd7fb43de6a785",
    },
    "P005": {
        "case_id": "ZTJ-P005",
        "chapter": "95",
        "commit": "sha256:8bb66f7d10cef9b8859766b4bb4126a6791c506e6f936287608595527ff254fd",
        "paired": "sha256:0e135b907fd5e5bd8645342625477e18cf65c80e61a889403cc027fe18e0b3e0",
    },
}
PACKAGE_MEDIA_TYPE = "application/vnd.novel-agent.writer-context-v2+json"
LEDGER_MEDIA_TYPE = "application/vnd.novel-agent.evidence-ledger-v2+json"
MANIFEST_MEDIA_TYPE = "application/vnd.novel-agent.evidence-first-package-manifest+json"


@dataclass(frozen=True, slots=True)
class _RepairCheckpoint:
    engine: Engine
    repository: ArtifactRepository
    r1: R1WorldRepository
    project_id: ProjectId
    repair_commit: CommitId
    selected_source_commit: CommitId
    world: WorldRootDocument
    text: TextRootDocument
    plan: PlanRootDocument
    manifest: dict[str, object]


@dataclass(frozen=True, slots=True)
class _CaseBasis:
    engine: Engine
    r1: R1WorldRepository
    project_id: ProjectId
    commit: CommitId
    world: WorldRootDocument
    text: TextRootDocument
    plan: PlanRootDocument
    joint_repair: bool


def _load_repair_checkpoint(workspace: Path) -> _RepairCheckpoint:
    manifest_path = workspace / "repair_manifest.json"
    database_path = workspace / "repair.sqlite3"
    objects_path = workspace / "objects"
    if not manifest_path.is_file() or not database_path.is_file() or not objects_path.is_dir():
        raise ValueError("repair workspace is missing manifest, database, or object store")
    manifest = json.loads(manifest_path.read_text("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("status") != "world_graph_repair_completed":
        raise ValueError("repair workspace manifest is not completed")
    selected = manifest.get("selected_source_commit")
    repair_commit_value = manifest.get("repair_commit")
    project_value = manifest.get("project_id")
    if (
        not isinstance(selected, str)
        or not selected
        or not isinstance(repair_commit_value, str)
        or not repair_commit_value
        or not isinstance(project_value, str)
        or not project_value
    ):
        raise ValueError(
            "repair manifest lacks selected source, repair commit, or project identity"
        )
    engine = create_engine(f"sqlite+pysqlite:///{database_path.resolve()}")
    repository = ArtifactRepository(FilesystemObjectStore(objects_path.resolve()))
    factory = build_session_factory(engine)
    repair_commit = CommitId(repair_commit_value)
    source = ArtifactProjectionSourceLoader(CommitService(factory), repository).load(repair_commit)
    if source.plan is None:
        engine.dispose()
        raise ValueError("joint Evidence-First acceptance requires a repair PlanRoot")
    project_id = ProjectId(project_value)
    if source.manifest.project_id != project_id:
        engine.dispose()
        raise ValueError("repair manifest project differs from committed project")
    world = source.world.model_copy(update={"source_commit": repair_commit})
    return _RepairCheckpoint(
        engine=engine,
        repository=repository,
        r1=R1WorldRepository(factory),
        project_id=project_id,
        repair_commit=repair_commit,
        selected_source_commit=CommitId(selected),
        world=world,
        text=source.text,
        plan=source.plan,
        manifest=manifest,
    )


def _select_case_basis(
    *,
    short_case: str,
    repair_case: str,
    repair: _RepairCheckpoint | None,
    source_engine: Engine,
    source_r1: R1WorldRepository,
    source_project_id: ProjectId,
    source_commit: CommitId,
    source_world: WorldRootDocument,
    source_text: TextRootDocument,
    source_plan: PlanRootDocument,
) -> _CaseBasis:
    if repair is None or short_case != repair_case:
        return _CaseBasis(
            engine=source_engine,
            r1=source_r1,
            project_id=source_project_id,
            commit=source_commit,
            world=source_world,
            text=source_text,
            plan=source_plan,
            joint_repair=False,
        )
    if repair.selected_source_commit != source_commit:
        raise ValueError("repair selected source commit differs from frozen checkpoint")
    if (
        repair.text.root_hash != source_text.root_hash
        or repair.plan.root_hash != source_plan.root_hash
    ):
        raise ValueError("repair TextRoot or PlanRoot differs from frozen checkpoint")
    manifest_source_text = repair.manifest.get("source_text_root")
    if manifest_source_text != source_text.root_hash.root:
        raise ValueError("repair manifest source TextRoot differs from frozen checkpoint")
    return _CaseBasis(
        engine=repair.engine,
        r1=repair.r1,
        project_id=repair.project_id,
        commit=repair.repair_commit,
        world=repair.world,
        text=repair.text,
        plan=repair.plan,
        joint_repair=True,
    )


def _read_object(objects_root: Path, artifact_id: str) -> bytes:
    key = artifact_id.removeprefix("sha256:")
    return (objects_root / "sha256" / key[:2] / key).read_bytes()


def _loopback_postgres_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        not parsed.scheme.startswith("postgresql+")
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
    ):
        raise ValueError("frozen database must use a loopback PostgreSQL URL")
    return value


def _loopback_http_url(value: str, label: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} endpoint must be a loopback HTTP(S) URL")
    return value


def _frozen_backend_bundle(
    *,
    engine: Engine,
    search_index: OpenSearchIndex,
    embedder: HttpEmbeddingProvider,
    reranker: HttpPassageReranker,
    r1: R1WorldRepository,
    project_id: ProjectId,
    source_commit: CommitId,
) -> Stage2RetrievalBackendBundle:
    """Reuse the frozen per-commit attestation and indexes without any write."""
    snapshots = DerivedSnapshotRepository(sessionmaker(bind=engine))
    attestation = snapshots.get_attestation_for_commit(source_commit)
    if attestation is None:
        raise ValueError(f"frozen checkpoint has no derived snapshot: {source_commit.root}")
    if (
        not attestation.quality_eligible
        or attestation.capability.status is not SnapshotCapabilityStatus.EXACT
        or attestation.capability.snapshot_id is None
    ):
        raise ValueError("frozen checkpoint attestation is not exact and quality-eligible")
    if any(not search_index.index_exists(index.physical_name) for index in attestation.indexes):
        raise ValueError("frozen checkpoint indexes are missing; refusing to rebuild")
    return build_real_hybrid_backend(
        r1=r1,
        search_index=search_index,
        embedder=embedder,
        project_id=project_id,
        source_commit=source_commit,
        snapshot_id=attestation.capability.snapshot_id,
        attestation=attestation,
        reranker=reranker,
    )


def _immutable_roots(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        head = conn.execute(
            text(
                "SELECT commit_id FROM project_commit WHERE "
                "manifest_json->>'parent_commit_ids' = '[]'"
            )
        ).fetchone()
        count = conn.execute(text("SELECT count(*) FROM project_commit")).scalar()
        rows = conn.execute(
            text(
                "SELECT source_commit, snapshot_json FROM derived_snapshot WHERE "
                "build_status = 'exact' AND snapshot_json->'projection_attestation' IS NOT NULL"
            )
        ).fetchall()
    return {
        "db_head_commit": head[0] if head is not None else "",
        "db_commit_count": str(count),
        "derived_snapshot_count": str(len(rows)),
    }


def _checkpoint_state(
    repository: ArtifactRepository,
    engine: Engine,
    case_spec: dict[str, str],
    project_directory: Path,
    bundle: BenchmarkBundle,
) -> tuple[
    CommitId,
    WorldRootDocument,
    TextRootDocument,
    PlanRootDocument,
    tuple[Stage1MemoryNeed, ...],
    PlannerInvocationArtifact,
]:
    objects_root = project_directory / "objects"
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT manifest_json FROM project_commit WHERE commit_id=:c"),
            {"c": case_spec["commit"]},
        ).fetchone()
        if row is None:
            raise ValueError(f"frozen commit missing: {case_spec['commit']}")
        manifest = row[0]
    text_ref = ArtifactRef.model_validate_json(
        json.dumps({k: v for k, v in manifest["text_root"].items() if k != "root_kind"})
    )
    world_ref = ArtifactRef.model_validate_json(
        json.dumps({k: v for k, v in manifest["world_root"].items() if k != "root_kind"})
    )
    text_root_doc = TextRootDocument.model_validate_json(
        repository.read_verified(text_ref), strict=True
    )
    world = WorldRootDocument.model_validate_json(repository.read_verified(world_ref), strict=True)
    commit = CommitId(case_spec["commit"])
    world = world.model_copy(update={"source_commit": commit})
    case = next(item for item in bundle.case_manifests if item.case_id.root == case_spec["case_id"])
    plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)
    paired = json.loads(_read_object(objects_root, case_spec["paired"]).decode("utf-8"))
    needs = tuple(
        Stage1MemoryNeed.model_validate_json(json.dumps(item))
        for item in (paired.get("generated_needs") or ())
    )
    planner_payload = paired.get("planner_artifact_ref") or {}
    planner_id = planner_payload.get("artifact_id")
    if not planner_id:
        raise ValueError(
            f"frozen paired artifact has no planner artifact ref: {case_spec['case_id']}"
        )
    planner_ref = ArtifactRef.model_validate_json(
        json.dumps(
            {
                "artifact_id": planner_id,
                "media_type": planner_payload.get(
                    "media_type", "application/vnd.novel-agent.planner-invocation+json"
                ),
                "byte_length": planner_payload.get("byte_length", 1),
                "schema_version": planner_payload.get("schema_version", "1.0.0"),
            }
        )
    )
    planner = PlannerInvocationArtifact.model_validate_json(
        repository.read_verified(planner_ref), strict=True
    )
    return commit, world, text_root_doc, plan, needs, planner


def _render_markdown(
    package: WriterContextPackageV2,
    ledger: EvidenceLedgerV2,
    manifest: EvidenceFirstPackageManifest,
) -> str:
    lines = [
        f"# Writer Context Package (evidence-first) — {manifest.case_id.root}",
        "",
        f"- contract_version: `{package.contract_version}`",
        f"- checkpoint chapter: {package.task_contract.checkpoint_chapter}",
        f"- basis commit: `{package.basis_commit_id.root}`",
        f"- basis snapshot: `{package.basis_snapshot_id.root}`",
        f"- assembly status: `{package.budget_report.final_status.value}`",
        f"- writer tokens: {package.budget_report.actual_rendered_writer_tokens}/"
        f"{package.budget_report.configured_writer_token_budget}",
        f"- ledger tokens: {package.budget_report.actual_rendered_ledger_tokens}/"
        f"{package.budget_report.configured_ledger_token_budget}",
        f"- item count: {package.budget_report.item_count} "
        f"(evidence {package.budget_report.evidence_item_count}, "
        f"gaps {package.budget_report.gap_item_count})",
        f"- ledger entries: {package.budget_report.ledger_entry_count}",
        f"- future leakage: {manifest.future_leakage_count}",
        "",
        "## Task",
        "",
        package.task_contract.task_text,
        "",
        "## Evidence items (by Need/facet/scope)",
        "",
    ]
    for section in dict.fromkeys(item.section.value for item in package.items):
        section_items = tuple(item for item in package.items if item.section.value == section)
        if not section_items:
            continue
        lines.append(f"### {section}")
        lines.append("")
        for item in section_items:
            ids = ", ".join(f"`{item_id.root}`" for item_id in item.evidence_ledger_ids)
            facets = ", ".join(facet.root for facet in item.need_facet_ids) or "(none)"
            if item.gap is not None:
                lines.append(
                    f"- **[gap] {item.gap.kind.value}** — {item.purpose} "
                    f"(need `{item.need_ids[0].root}`, facets: {facets})"
                )
                lines.append(f"  - reason: {item.gap.reason}")
                continue
            lines.append(
                f"- {item.purpose} "
                f"(need `{item.need_ids[0].root}`, facets: {facets}, "
                f"validity: {item.validity.value}, selection: {item.selection_reason})"
            )
            lines.append(f"  - evidence ids: {ids}")
            lines.append(f"  - preview: {item.raw_preview}")
            if item.preview_truncated:
                lines.append("  - preview truncated")
        lines.append("")
    lines.append("## Typed gaps")
    lines.append("")
    if package.gaps:
        for gap in package.gaps:
            lines.append(f"- `{gap.kind.value}`: {gap.reason}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Evidence ledger (exact raw slices)")
    lines.append("")
    if ledger.entries:
        for entry in ledger.entries:
            chapter = entry.evidence_slices[0].chapter_id.root
            needs = ", ".join(need.root for need in entry.need_ids)
            lines.append(f"### `{entry.ledger_id.root}`")
            lines.append("")
            lines.append(
                f"- chapter: `{chapter}`; scope: {entry.information_scope}; "
                f"cutoff: {entry.cutoff_chapter}"
            )
            lines.append(f"- serves needs: {needs}")
            lines.append(f"- dereference: {entry.dereference_receipt}; taint: {entry.taint}")
            lines.append(
                f"- span hash: `{entry.span_hash.root[:24]}…`; "
                f"quote hash: `{entry.quote_hash.root[:24]}…`"
            )
            lines.append("")
            lines.append("```text")
            lines.append(entry.evidence_text)
            lines.append("```")
            lines.append("")
    else:
        lines.append("(no ledger entries)")
        lines.append("")
    return "\n".join(lines)


def _readiness_status(
    assembly_status: str,
    mechanical_failures: dict[str, int],
    future_leakage_count: int,
) -> str:
    if assembly_status != "READY":
        return assembly_status
    if any(mechanical_failures.values()) or future_leakage_count:
        return "MECHANICAL_FAILURE"
    return "READY"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-project", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repair-workspace", type=Path)
    parser.add_argument("--repair-case", choices=tuple(CASES), default="P005")
    parser.add_argument("--case", action="append", choices=tuple(CASES), required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--opensearch-url", default="http://127.0.0.1:9200")
    parser.add_argument("--embedding-url", default="http://127.0.0.1:8281/v1/embeddings")
    parser.add_argument("--reranker-url", default="http://127.0.0.1:8282/rerank")
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--writer-token-budget", type=int, default=4000)
    parser.add_argument("--ledger-token-budget", type=int, default=12000)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--max-tool-calls", type=int, default=48)
    args = parser.parse_args()

    if args.output_root.exists():
        raise FileExistsError("evidence-first output identity already exists")
    if len(args.case) != len(set(args.case)):
        raise ValueError("evidence-first cases must be unique")
    if args.repair_workspace is not None and args.repair_case not in args.case:
        raise ValueError("repair-case must be included when repair-workspace is configured")

    try:
        from scripts.native_models import assert_model_service, load_model_lock
    except ModuleNotFoundError:  # pragma: no cover - direct script execution
        from native_models import (  # type: ignore[import-not-found,no-redef]
            assert_model_service,
            load_model_lock,
        )

    project_directory = args.source_project.resolve()
    objects_root = project_directory / "objects"
    if not objects_root.is_dir():
        print(f"source objects missing: {objects_root}", file=sys.stderr)
        return 2
    database_url = _loopback_postgres_url(args.database_url)
    embedding_url = _loopback_http_url(args.embedding_url, "embedding")
    reranker_url = _loopback_http_url(args.reranker_url, "reranker")
    engine = build_engine(database_url)
    repair: _RepairCheckpoint | None = None
    search_client = None
    started_at = datetime.now(UTC).isoformat()
    try:
        bundle = HumanBenchmarkCompiler().compile(
            Path("benchmarks/private/ztj_memory_pilot_v0.1").resolve()
        )
        repository = ArtifactRepository(FilesystemObjectStore(objects_root))
        model_lock = load_model_lock()
        embedding_model = model_lock.models["embedding"]
        reranker_model = model_lock.models["reranker"]
        assert_model_service(embedding_model)
        assert_model_service(reranker_model)
        run_id = RunId(f"run.evidence-first.{args.experiment_id}")
        embedder = HttpEmbeddingProvider(
            RetrievalModelRoute(
                endpoint=embedding_url,
                model=embedding_model.model_id,
                revision=embedding_model.revision,
                runtime_fingerprint=embedding_model.runtime_fingerprint,
                run_id=run_id,
                task_id=TaskId("task.evidence-first.embedding"),
                trace_id=f"trace.{run_id.root}",
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
                endpoint=reranker_url,
                model=reranker_model.model_id,
                revision=reranker_model.revision,
                runtime_fingerprint=reranker_model.runtime_fingerprint,
                run_id=run_id,
                task_id=TaskId("task.evidence-first.reranker"),
                trace_id=f"trace.{run_id.root}",
                span_id=None,
                model_role=ModelRole.BATCH_TEST,
                purpose=ModelCallPurpose.EVALUATION,
                timeout_seconds=300,
            )
        )
        from opensearchpy import OpenSearch

        parsed = urlparse(args.opensearch_url)
        search_client = OpenSearch(
            hosts=[{"host": parsed.hostname, "port": parsed.port}],
            use_ssl=parsed.scheme == "https",
            verify_certs=parsed.scheme == "https",
        )
        if not search_client.ping():
            raise RuntimeError("OpenSearch is unavailable")
        search_index = OpenSearchIndex(search_client)
        factory = build_session_factory(engine)
        r1 = R1WorldRepository(factory)
        if args.repair_workspace is not None:
            repair = _load_repair_checkpoint(args.repair_workspace.resolve())
        roots_before = _immutable_roots(engine)
        repair_roots_before = _immutable_roots(repair.engine) if repair is not None else None
        args.output_root.mkdir(parents=True)
        output_repository = ArtifactRepository(
            FilesystemObjectStore((args.output_root / "objects").resolve())
        )

        def build_runner(graph_r1: R1WorldRepository) -> EvidenceFirstCheckpointRunner:
            return EvidenceFirstCheckpointRunner(
                writer_token_budget=args.writer_token_budget,
                evidence_ledger_token_budget=args.ledger_token_budget,
                max_candidates=args.max_candidates,
                max_tool_calls=args.max_tool_calls,
                artifact_writer=lambda payload, media_type: output_repository.put(
                    payload, media_type, SchemaVersion("1.0.0")
                ),
                graph_receipt_validator=graph_r1.validate_graph_path_receipts,
            )

        index_entries: list[dict[str, object]] = []
        runner_version: str | None = None
        assembler_version: str | None = None
        case_fingerprints: list[ArtifactId] = []
        for short in args.case:
            case_spec = CASES[short]
            case = next(
                item for item in bundle.case_manifests if item.case_id.root == case_spec["case_id"]
            )
            commit, world, text, plan, frozen_needs, planner = _checkpoint_state(
                repository,
                engine,
                case_spec,
                project_directory,
                bundle,
            )
            basis = _select_case_basis(
                short_case=short,
                repair_case=args.repair_case,
                repair=repair,
                source_engine=engine,
                source_r1=r1,
                source_project_id=case.project_id,
                source_commit=commit,
                source_world=world,
                source_text=text,
                source_plan=plan,
            )
            commit = basis.commit
            world = basis.world
            text = basis.text
            plan = basis.plan
            backend_engine = basis.engine
            backend_r1 = basis.r1
            backend_project_id = basis.project_id
            joint_repair = basis.joint_repair
            runner = build_runner(basis.r1)
            runner_version = runner.version
            assembler_version = runner.assembler.version
            planning_context = next(
                (
                    context
                    for context in bundle.planning_contexts
                    if context.source_hash == case.planning_context_hash
                ),
                None,
            )
            if planning_context is None:
                raise ValueError(f"frozen planning context missing: {case_spec['case_id']}")
            if case.information_profile is not BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED:
                raise ValueError(
                    f"evidence-first checkpoint requires APC profile: {case_spec['case_id']}"
                )
            author_plan = plan
            plan_bytes = plan.model_dump_json().encode("utf-8")
            plan_root_ref = (
                PlanRootRef(
                    artifact_id=plan.root_hash,
                    media_type="application/vnd.novel-agent.plan-root+json",
                    byte_length=len(plan_bytes),
                    schema_version=plan.schema_version,
                )
                if case.information_profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
                else None
            )
            public = build_public_checkpoint_case(
                case_id=case.case_id,
                project_id=case.project_id,
                target_range=case.target_range,
                history_range=case.history_range,
                information_profile=case.information_profile,
                plan_root_ref=plan_root_ref,
                task_intent=case.task_intent,
                planning_context_ref=case.planning_context_ref,
                planning_context_hash=case.planning_context_hash,
            )
            if (
                planning_context.profile is not case.information_profile
                or planning_context.target_range != case.target_range
                or planning_context.task_intent != public.task_contract.task_intent
                or planning_context.source_hash != public.task_contract.planning_context_hash
                or content_id(planning_context.model_dump(mode="json"))
                != public.task_contract.planning_context_ref
            ):
                raise ValueError("frozen planning context disagrees with its public task binding")
            backend_bundle = _frozen_backend_bundle(
                engine=backend_engine,
                search_index=search_index,
                embedder=embedder,
                reranker=reranker,
                r1=backend_r1,
                project_id=backend_project_id,
                source_commit=commit,
            )
            fingerprint = content_id(
                {
                    "runner": runner.version,
                    "experiment_id": args.experiment_id,
                    "case_id": case.case_id.root,
                    "checkpoint_chapter": int(case_spec["chapter"]),
                    "basis_commit": commit.root,
                    "snapshot_id": backend_bundle.attestation.capability.snapshot_id.root,
                    "writer_token_budget": args.writer_token_budget,
                    "evidence_ledger_token_budget": args.ledger_token_budget,
                    "max_candidates": args.max_candidates,
                    "max_tool_calls": args.max_tool_calls,
                    "profile": case.information_profile.value,
                    "grounder_version": "need_draft_grounder.v3",
                    "assembler_version": runner.assembler.version,
                    "joint_repair": joint_repair,
                    "repair_project_id": backend_project_id.root if joint_repair else None,
                }
            )
            case_fingerprints.append(fingerprint)
            embedding_calls_before = len(embedder.call_records)
            rerank_calls_before = len(reranker.call_records)
            result = runner.run(
                case_id=case.project_id,
                task=public.task_contract,
                world=world,
                text=text,
                plan=author_plan,
                base_commit=commit,
                snapshot_id=backend_bundle.attestation.capability.snapshot_id,
                planning_context=planning_context,
                frozen_planner_artifact=planner,
                frozen_needs=frozen_needs,
                backend_bundle=backend_bundle,
                fingerprint=fingerprint,
                run_id=StableId(f"request.evidence-first.{case_spec['case_id']}"),
            )
            embedding_calls = len(embedder.call_records) - embedding_calls_before
            rerank_calls = len(reranker.call_records) - rerank_calls_before
            package = result.assembly.package
            ledger = result.assembly.evidence_ledger
            package_ref = output_repository.put(
                canonical_json_bytes(package.model_dump(mode="json")),
                PACKAGE_MEDIA_TYPE,
                SchemaVersion("1.0.0"),
            )
            ledger_ref = output_repository.put(
                canonical_json_bytes(ledger.model_dump(mode="json")),
                LEDGER_MEDIA_TYPE,
                SchemaVersion("1.0.0"),
            )
            output_repository.read_verified(package_ref)
            output_repository.read_verified(ledger_ref)
            source_roots_unchanged = _immutable_roots(engine) == roots_before
            repair_roots_unchanged = (
                repair is None
                or repair_roots_before is None
                or _immutable_roots(repair.engine) == repair_roots_before
            )
            roots_unchanged = source_roots_unchanged and repair_roots_unchanged
            immutable = {
                "basis_commit": commit.root,
                "world_root": world.root_hash.root,
                "text_root": text.root_hash.root,
                "plan_root": plan.root_hash.root,
                "snapshot_id": backend_bundle.attestation.capability.snapshot_id.root,
                "source_checkpoint_commit": case_spec["commit"],
                "joint_repair": str(joint_repair).lower(),
                "indexes": ";".join(
                    item.physical_name for item in backend_bundle.attestation.indexes
                ),
            }
            mechanical = result.assembly.mechanical_failure_counts
            gap_codes = tuple(dict.fromkeys(gap.kind.value for gap in package.gaps))
            graph_readiness = {
                str(record["need_id"]): str(record["graph_readiness_status"])
                for record in result.trace_records
            }
            verified_graph_receipts = tuple(
                StableId(receipt_id)
                for receipt_id in dict.fromkeys(
                    receipt_id
                    for record in result.trace_records
                    for receipt_id in record["verified_graph_path_receipt_ids"]
                )
            )
            manifest = EvidenceFirstPackageManifest(
                manifest_id=StableId(
                    f"manifest.evidence-first.{case_spec['case_id']}.{args.experiment_id}"[:128]
                ),
                experiment_id=args.experiment_id,
                case_id=case.case_id,
                checkpoint_chapter=int(case_spec["chapter"]),
                basis_commit_id=commit,
                basis_snapshot_id=backend_bundle.attestation.capability.snapshot_id,
                assembler_version=runner.assembler.version,
                run_config_hash=fingerprint,
                package_artifact_ref=package_ref,
                evidence_ledger_ref=ledger_ref,
                package_hash=package_ref.artifact_id,
                evidence_ledger_hash=ledger_ref.artifact_id,
                generated_at=datetime.now(UTC).isoformat(),
                writer_token_budget=args.writer_token_budget,
                evidence_ledger_token_budget=args.ledger_token_budget,
                call_counts={
                    "need_planner_model_calls": 0,
                    "claim_support_calls": 0,
                    "whole_verifier_calls": 0,
                    "semantic_evaluator_calls": 0,
                    "retrieval_backend_calls": result.retrieval_call_count,
                    "embedding_calls": embedding_calls,
                    "rerank_calls": rerank_calls,
                },
                immutable_root_hashes=immutable,
                need_count=len(result.needs),
                item_count=package.budget_report.item_count,
                gap_count=package.budget_report.gap_item_count,
                gap_codes=gap_codes,
                ledger_entry_count=len(ledger.entries),
                ledger_tokens=ledger.rendered_tokens,
                future_leakage_count=result.future_leakage_count,
                leakage_failure_count=result.future_leakage_count,
                dereference_failure_count=mechanical.get("dereference", 0),
                scope_failure_count=mechanical.get("scope", 0),
                cutoff_failure_count=mechanical.get("cutoff", 0),
                budget_status=package.budget_report.final_status.value,
                root_hashes_unchanged=roots_unchanged,
                embedding_call_count=embedding_calls,
                rerank_call_count=rerank_calls,
                assembly_status=result.assembly.status.value,
                projection_attestation_id=backend_bundle.attestation.attestation_id,
                graph_edge_count=backend_bundle.attestation.graph_edge_count,
                graph_readiness_by_need=graph_readiness,
                verified_graph_path_receipt_ids=verified_graph_receipts,
            )
            markdown = _render_markdown(package, ledger, manifest)
            manifest = manifest.model_copy(
                update={
                    "markdown_hash": ArtifactId(
                        "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()
                    )
                }
            )
            manifest_ref = output_repository.put(
                canonical_json_bytes(manifest.model_dump(mode="json")),
                MANIFEST_MEDIA_TYPE,
                SchemaVersion("1.0.0"),
            )
            output_repository.read_verified(manifest_ref)
            out_dir = args.output_root / short
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "writer_context_package.json").write_text(
                output_repository.read_verified(package_ref).decode("utf-8"), encoding="utf-8"
            )
            (out_dir / "evidence_ledger.json").write_text(
                output_repository.read_verified(ledger_ref).decode("utf-8"), encoding="utf-8"
            )
            (out_dir / "package_manifest.json").write_text(
                manifest.model_dump_json(indent=2), encoding="utf-8"
            )
            (out_dir / "writer_context_package.md").write_text(
                _render_markdown(package, ledger, manifest), encoding="utf-8"
            )
            case_record = {
                "case_id": case.case_id.root,
                "checkpoint_chapter": int(case_spec["chapter"]),
                "contract_version": package.contract_version,
                "basis_commit": commit.root,
                "basis_snapshot_id": backend_bundle.attestation.capability.snapshot_id.root,
                "source_checkpoint_commit": case_spec["commit"],
                "joint_repair": joint_repair,
                "backend_project_id": backend_project_id.root,
                "needs": [
                    {
                        "need_id": need.need_id.root,
                        "query": need.query_text,
                        "semantic_question": need.semantic_question,
                        "entity_ids": [item.root for item in need.entity_ids],
                        "requirement": need.requirement.value,
                        "priority": need.priority,
                    }
                    for need in result.needs
                ],
                "route_plans": [
                    {
                        "need_id": plan.need_id.root,
                        "tier": plan.resolution_tier.value,
                        "effective_channels": [c.value for c in plan.effective_channels],
                        "excluded_channels": [
                            {"channel": item.channel.value, "reason": item.reason}
                            for item in plan.excluded_channels
                        ],
                        "query_unavailable_reasons": {
                            channel.value: reason
                            for channel, reason in plan.query_unavailable_reasons.items()
                        },
                    }
                    for plan in result.route_plans
                ],
                "traces": list(result.trace_records),
                "package": {
                    "item_count": package.budget_report.item_count,
                    "evidence_items": package.budget_report.evidence_item_count,
                    "gap_items": package.budget_report.gap_item_count,
                    "ledger_entries": len(ledger.entries),
                    "writer_tokens": package.budget_report.actual_rendered_writer_tokens,
                    "ledger_tokens": ledger.rendered_tokens,
                    "status": result.assembly.status.value,
                    "diagnostics": list(result.assembly.diagnostic_codes),
                    "future_leakage_count": result.future_leakage_count,
                    "retrieval_call_count": result.retrieval_call_count,
                    "stop_reason": result.stop_reason,
                    "planner_fallback_used": result.planner_fallback_used,
                    "unresolved_lexical_anchors": [
                        anchor.model_dump(mode="json")
                        for anchor in result.unresolved_lexical_anchors
                    ],
                },
                "readiness": {
                    "package_status": result.assembly.status.value,
                    "gap_codes": list(gap_codes),
                    "ledger_entry_count": len(ledger.entries),
                    "evidence_item_count": package.budget_report.evidence_item_count,
                    "dereference_failures": mechanical.get("dereference", 0),
                    "scope_failures": mechanical.get("scope", 0),
                    "cutoff_failures": mechanical.get("cutoff", 0),
                    "leakage_failures": result.future_leakage_count,
                    "budget_status": package.budget_report.final_status.value,
                    "root_hashes_unchanged": roots_unchanged,
                    "source_roots_unchanged": source_roots_unchanged,
                    "repair_roots_unchanged": repair_roots_unchanged,
                    "claim_support_calls": 0,
                    "whole_verifier_calls": 0,
                    "semantic_evaluator_calls": 0,
                    "need_planner_model_calls": 0,
                    "retrieval_calls": result.retrieval_call_count,
                    "embedding_calls": embedding_calls,
                    "rerank_calls": rerank_calls,
                    "markdown_hash": manifest.markdown_hash.root if manifest.markdown_hash else "",
                    "projection_attestation_id": backend_bundle.attestation.attestation_id.root,
                    "graph_edge_count": backend_bundle.attestation.graph_edge_count,
                    "graph_readiness_by_need": graph_readiness,
                    "verified_graph_path_receipt_ids": [
                        item.root for item in verified_graph_receipts
                    ],
                },
                "refs": {
                    "package_ref": package_ref.artifact_id.root,
                    "ledger_ref": ledger_ref.artifact_id.root,
                    "manifest_ref": manifest_ref.artifact_id.root,
                    "manifest_id": manifest.manifest_id.root,
                    "run_config_hash": fingerprint.root,
                },
                "immutable_root_hashes": immutable,
            }
            (out_dir / "case_record.json").write_text(
                json.dumps(case_record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            index_entries.append(
                {
                    "case": short,
                    "case_id": case.case_id.root,
                    "checkpoint_chapter": int(case_spec["chapter"]),
                    "directory": str(out_dir),
                    "package_ref": package_ref.artifact_id.root,
                    "ledger_ref": ledger_ref.artifact_id.root,
                    "manifest_ref": manifest_ref.artifact_id.root,
                    "manifest_id": manifest.manifest_id.root,
                    "assembly_status": result.assembly.status.value,
                    "readiness_status": _readiness_status(
                        result.assembly.status.value,
                        mechanical,
                        result.future_leakage_count,
                    ),
                    "gap_codes": list(gap_codes),
                    "dereference_failures": mechanical.get("dereference", 0),
                    "scope_failures": mechanical.get("scope", 0),
                    "cutoff_failures": mechanical.get("cutoff", 0),
                    "leakage_failures": result.future_leakage_count,
                    "root_hashes_unchanged": roots_unchanged,
                    "joint_repair": joint_repair,
                    "backend_project_id": backend_project_id.root,
                    "package_artifact": f"{package_ref.media_type}@{package_ref.artifact_id.root}",
                    "ledger_artifact": f"{ledger_ref.media_type}@{ledger_ref.artifact_id.root}",
                    "manifest_artifact": (
                        f"{manifest_ref.media_type}@{manifest_ref.artifact_id.root}"
                    ),
                }
            )
            print(
                f"{short}/C{case_spec['chapter']}: status={result.assembly.status.value} "
                f"needs={len(result.needs)} items={package.budget_report.item_count} "
                f"gaps={package.budget_report.gap_item_count} "
                f"ledger={len(ledger.entries)} writer_tokens="
                f"{package.budget_report.actual_rendered_writer_tokens} "
                f"ledger_tokens={ledger.rendered_tokens} "
                f"retrieval_calls={result.retrieval_call_count} "
                f"leakage={result.future_leakage_count} "
                f"package={package_ref.artifact_id.root[:16]} "
                f"ledger={ledger_ref.artifact_id.root[:16]}"
            )
        roots_after = _immutable_roots(engine)
        if roots_before != roots_after:
            raise RuntimeError("immutable roots changed during the evidence-first run")
        repair_roots_after = _immutable_roots(repair.engine) if repair is not None else None
        if repair_roots_before != repair_roots_after:
            raise RuntimeError("repair workspace roots changed during the evidence-first run")
        if runner_version is None or assembler_version is None or not case_fingerprints:
            raise RuntimeError("evidence-first run produced no cases")
        aggregate_mechanical_status = (
            "PASS"
            if all(
                item["readiness_status"] == "READY"
                and item["dereference_failures"] == 0
                and item["scope_failures"] == 0
                and item["cutoff_failures"] == 0
                and item["leakage_failures"] == 0
                and item["root_hashes_unchanged"] is True
                for item in index_entries
            )
            else "FAIL"
        )
        parsed_database = urlparse(database_url)
        aggregate_config_hash = content_id(
            {
                "case_fingerprints": [item.root for item in case_fingerprints],
                "repair_case": args.repair_case if repair is not None else None,
            }
        )
        (args.output_root / "output_index.json").write_text(
            json.dumps(
                {
                    "experiment_id": args.experiment_id,
                    "runner_version": runner_version,
                    "grounder_version": "need_draft_grounder.v3",
                    "assembler_version": assembler_version,
                    "contract_version": "writer_context.v2",
                    "ledger_contract_version": "evidence_ledger.v2",
                    "run_config_hash": aggregate_config_hash.root,
                    "started_at": started_at,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "source_project": str(project_directory),
                    "frozen_database": {
                        "host": parsed_database.hostname,
                        "port": parsed_database.port,
                        "database": parsed_database.path.removeprefix("/"),
                    },
                    "profile": BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED.value,
                    "aggregate_mechanical_status": aggregate_mechanical_status,
                    "immutable_roots": roots_after,
                    "repair_workspace": (
                        str(args.repair_workspace.resolve())
                        if args.repair_workspace is not None
                        else None
                    ),
                    "repair_case": args.repair_case if repair is not None else None,
                    "repair_roots": repair_roots_after,
                    "cases": index_entries,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return 0
    finally:
        if search_client is not None:
            search_client.close()
        if repair is not None:
            repair.engine.dispose()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
