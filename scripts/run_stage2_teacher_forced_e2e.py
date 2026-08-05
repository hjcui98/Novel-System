#!/usr/bin/env python3
"""Run Stage 2 Genesis plus teacher-forced chapter replay on the human benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import ParseResult, urlparse
from uuid import uuid4

from opensearchpy import OpenSearch
from sqlalchemy.engine import Engine

try:
    from scripts.native_models import assert_model_service, load_model_lock
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from native_models import (  # type: ignore[import-not-found,no-redef]
        assert_model_service,
        load_model_lock,
    )

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import (
    HttpEmbeddingProvider,
    HttpPassageReranker,
    OpenAICompatibleChatEndpoint,
    RetrievalModelRoute,
)
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.benchmark import BenchmarkBundle
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, TaskId
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRole
from novel_agent.domain.retrieval_routing import RetrievalBackendProfile
from novel_agent.domain.stage2 import BenchmarkInformationProfile, QualityRepairFeatureFlags
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.claim_support import (
    ControllerSupportSelector,
    TrustedClaimSupportProducer,
)
from novel_agent.services.commits import CommitService
from novel_agent.services.embedding_cache import SqlEmbeddingCache
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatcher
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_contract import TASK_TEMPLATE_VERSION
from novel_agent.services.memory_benchmark_evaluation import MemoryBenchmarkEvaluator
from novel_agent.services.memory_benchmark_metric_contracts import (
    GATE_METRIC_FORMULA_HASH,
    GATE_METRIC_FORMULA_VERSION,
)
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedSnapshotRepository,
    FullDerivedProjectionBuilder,
)
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.retrieval_unit_normalizer import RetrievalUnitNormalizer
from novel_agent.services.search_retrieval import Stage2RSearchIndexer
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner
from novel_agent.services.stage2_retrieval_backend import RealHybridProjectionGateway
from novel_agent.services.task_conditioned_need_generation import (
    TaskPlanConditionedNeedGenerator,
)
from novel_agent.services.task_focus import TaskFocusExtractor
from novel_agent.services.teacher_forced_benchmark_e2e import (
    TeacherForcedBenchmarkE2ERunner,
)
from novel_agent.services.writer_context_assembler import WriterContextAssembler


def _bounded_int(label: str, *, minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        value = int(raw)
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(f"{label} must be between {minimum} and {maximum}")
        return value

    return parse


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source", type=Path, required=True)
    value.add_argument("--output-directory", type=Path, required=True)
    value.add_argument(
        "--resume-project",
        type=Path,
        help=(
            "existing Canonical project directory whose objects, commit chain, "
            "and snapshots are reused"
        ),
    )
    value.add_argument(
        "--resume-commit",
        help="explicit historical Canonical commit for checkpoint-only evaluation",
    )
    value.add_argument(
        "--resume-chapter",
        type=_bounded_int("resume chapter", minimum=0, maximum=95),
        help="chapter represented by --resume-commit",
    )
    value.add_argument(
        "--database-url",
        help="required loopback PostgreSQL URL for formal real_hybrid retrieval",
    )
    value.add_argument(
        "--experiment-id",
        required=True,
        help="stable experiment namespace for database/filesystem/OpenSearch isolation",
    )
    value.add_argument(
        "--opensearch-url",
        default=f"http://127.0.0.1:{os.getenv('OPENSEARCH_PORT', '9200')}",
    )
    value.add_argument(
        "--embedding-url",
        default=(
            "http://127.0.0.1:"
            f"{os.getenv('NOVEL_AGENT_EMBEDDING_MODEL_PORT', '8081')}/v1/embeddings"
        ),
    )
    value.add_argument(
        "--reranker-url",
        default=(f"http://127.0.0.1:{os.getenv('NOVEL_AGENT_RERANKER_MODEL_PORT', '8082')}/rerank"),
    )
    value.add_argument(
        "--information-profile",
        choices=tuple(item.value for item in BenchmarkInformationProfile),
        default=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED.value,
    )
    value.add_argument("--token-budget", type=int, default=4000)
    value.add_argument(
        "--max-candidates",
        type=_bounded_int("max candidates", minimum=1, maximum=100),
        default=20,
    )
    value.add_argument("--max-tool-calls", type=int, default=48)
    value.add_argument(
        "--arms",
        choices=("A", "ABC"),
        default="ABC",
        help="A runs the deterministic gate only; ABC enables the paired experiment",
    )
    value.add_argument(
        "--semantic-backend",
        choices=("local_openai", "scripted"),
        default="local_openai",
    )
    value.add_argument("--model-base-url", default="http://127.0.0.1:8002/v1")
    value.add_argument("--model", default="qwen36-27b-nvfp4")
    value.add_argument("--model-max-output-tokens", type=int, default=8192)
    value.add_argument("--model-max-retries", type=int, default=0)
    value.add_argument(
        "--allow-dirty-diagnostic",
        action="store_true",
        help=(
            "allow a non-formal real_hybrid Arm A canary to record the current executable tree; "
            "never use this option for final P3/Gate"
        ),
    )
    value.add_argument(
        "--retrieval-backend",
        choices=tuple(item.value for item in RetrievalBackendProfile),
        default=RetrievalBackendProfile.REAL_HYBRID.value,
        help="real_hybrid is the only formal benchmark mode; scripted_smoke is contract-test only",
    )
    value.add_argument("--stop-after-genesis", action="store_true")
    value.add_argument("--max-chapter", type=int, default=None)
    value.add_argument("--resume", action="store_true")
    value.add_argument(
        "--quality-repair-config",
        type=Path,
        default=None,
        help=(
            "JSON file with QualityRepairFeatureFlags (controller_mode, "
            "curator_evidence_contract, evidence_support_gate, "
            "max_controller_decision_model_calls, max_agentic_actions)"
        ),
    )
    value.add_argument(
        "--memory-write-dry-run",
        action="store_true",
        help=(
            "Pre-commit dry-run: generate and validate Candidate, then stop at a "
            "refusing commit port without accepting a Canonical commit"
        ),
    )
    value.add_argument(
        "--support-pre-proposal-trace",
        action="store_true",
        help=(
            "Run the deterministic pre-SupportWorkset corridor (handles -> ranked -> "
            "compatible -> pool -> L0 resolution -> exact slices -> workset -> chunks -> "
            "raw Ledger) and record the durable membership audit without any model call. "
            "Diagnostic only; no claims are proposed or verified."
        ),
    )
    return value


def main() -> int:
    args = parser().parse_args()
    if (args.resume_commit is None) != (args.resume_chapter is None):
        raise ValueError("--resume-commit and --resume-chapter must be supplied together")
    if args.resume_commit is not None and args.resume_project is None:
        raise ValueError("explicit historical resume requires --resume-project")
    bundle = HumanBenchmarkCompiler().compile(args.source)
    project_directory = (args.resume_project or args.output_directory).resolve()
    output_directory = args.output_directory.resolve()
    retrieval_profile = RetrievalBackendProfile(args.retrieval_backend)
    repository_root = Path(__file__).parents[1]
    require_clean_source = (
        retrieval_profile is RetrievalBackendProfile.REAL_HYBRID and not args.allow_dirty_diagnostic
    )
    if require_clean_source:
        _assert_formal_source_clean(repository_root)
    quality_repair_flags = _load_quality_repair_flags(args)
    _ensure_experiment_manifest(
        args,
        bundle,
        output_directory,
        quality_repair_flags,
        project_directory=project_directory,
        require_clean_source=require_clean_source,
    )
    endpoint = (
        OpenAICompatibleChatEndpoint(
            base_url=args.model_base_url,
            model=args.model,
            max_output_tokens=args.model_max_output_tokens,
            max_retries=args.model_max_retries,
        )
        if args.semantic_backend == "local_openai"
        else None
    )
    provider_engine = None
    search_client = None
    try:
        real_hybrid_provider = None
        if retrieval_profile is RetrievalBackendProfile.REAL_HYBRID:
            if args.database_url is None:
                raise ValueError("--database-url is required for real_hybrid execution")
            provider_engine, search_client, gateway = _real_hybrid_gateway(
                args,
                project_directory,
            )
            real_hybrid_provider = gateway.backend_for
        summary = TeacherForcedBenchmarkE2ERunner(
            token_budget=args.token_budget,
            max_candidates=args.max_candidates,
            max_tool_calls=args.max_tool_calls,
            benchmark_arms=(("A",) if args.arms == "A" else ("A", "B", "C")),
            semantic_endpoint=endpoint,
            retrieval_backend_profile=retrieval_profile,
            real_hybrid_backend_provider=real_hybrid_provider,
            database_url=args.database_url,
            quality_repair_flags=quality_repair_flags,
            memory_write_dry_run=bool(args.memory_write_dry_run),
            support_pre_proposal_trace=bool(args.support_pre_proposal_trace),
        ).run(
            args.source,
            args.output_directory,
            bundle,
            information_profile=BenchmarkInformationProfile(args.information_profile),
            stop_after_genesis=args.stop_after_genesis,
            max_chapter=args.max_chapter,
            resume=args.resume or args.resume_project is not None,
            project_directory=project_directory,
            resume_commit=(
                CommitId(args.resume_commit) if args.resume_commit is not None else None
            ),
            resume_chapter_override=args.resume_chapter,
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        if search_client is not None:
            search_client.close()
        if provider_engine is not None:
            provider_engine.dispose()
        if endpoint is not None:
            import asyncio

            asyncio.run(endpoint.aclose())


def _real_hybrid_gateway(
    args: argparse.Namespace,
    project_directory: Path,
) -> tuple[Engine, OpenSearch, RealHybridProjectionGateway]:
    """Build the exact commit-scoped gateway used by both paired arms.

    This validates all native retrieval dependencies up front.  It does not
    call an embedding service or replay any benchmark chapter until the
    runner freezes a checkpoint and asks the gateway for that commit.
    """

    database_url = _loopback_postgres_url(args.database_url)
    search_target = _loopback_http_url(args.opensearch_url, "OpenSearch")
    embedding_target = _loopback_http_url(args.embedding_url, "embedding")
    reranker_target = _loopback_http_url(args.reranker_url, "reranker")
    _prepare_project_artifact_directory(args, project_directory)
    model_lock = load_model_lock()
    embedding_model = model_lock.models["embedding"]
    reranker_model = model_lock.models["reranker"]
    assert_model_service(embedding_model)
    assert_model_service(reranker_model)
    run_id = RunId(f"run.stage2r-teacher-forced.{uuid4().hex}")
    embedder = HttpEmbeddingProvider(
        RetrievalModelRoute(
            endpoint=embedding_target.geturl(),
            model=embedding_model.model_id,
            revision=embedding_model.revision,
            runtime_fingerprint=embedding_model.runtime_fingerprint,
            run_id=run_id,
            task_id=TaskId("task.stage2r-teacher-forced.embedding"),
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
            endpoint=reranker_target.geturl(),
            model=reranker_model.model_id,
            revision=reranker_model.revision,
            runtime_fingerprint=reranker_model.runtime_fingerprint,
            run_id=run_id,
            task_id=TaskId("task.stage2r-teacher-forced.reranker"),
            trace_id=f"trace.{run_id.root}",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        )
    )
    engine = build_engine(database_url)
    client = OpenSearch(
        hosts=[{"host": search_target.hostname, "port": search_target.port}],
        use_ssl=search_target.scheme == "https",
        verify_certs=search_target.scheme == "https",
    )
    if not client.ping():
        client.close()
        engine.dispose()
        raise RuntimeError("OpenSearch is unavailable")
    factory = build_session_factory(engine)
    r1 = R1WorldRepository(factory)
    artifacts = ArtifactRepository(FilesystemObjectStore(project_directory / "objects"))
    builder = FullDerivedProjectionBuilder(
        ArtifactProjectionSourceLoader(CommitService(factory), artifacts),
        r1,
        Stage2RSearchIndexer(
            OpenSearchIndex(client),
            embedder,
            embedding_cache=SqlEmbeddingCache(factory),
            index_namespace=args.experiment_id,
        ),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        build_profile="stage2r-hybrid-v0.1",
        embedding_model=embedding_model.model_id,
        embedding_revision=embedding_model.revision,
        embedding_runtime_fingerprint=ArtifactId(f"sha256:{embedding_model.runtime_fingerprint}"),
        reranker_model=reranker_model.model_id,
        reranker_revision=reranker_model.revision,
    )
    return (
        engine,
        client,
        RealHybridProjectionGateway(
            builder=builder,
            snapshots=DerivedSnapshotRepository(factory),
            r1=r1,
            search_index=OpenSearchIndex(client),
            embedder=embedder,
            reranker=reranker,
        ),
    )


def _prepare_project_artifact_directory(
    args: argparse.Namespace,
    project_directory: Path,
) -> None:
    objects = project_directory / "objects"
    if objects.is_dir():
        return
    if objects.exists():
        raise ValueError(f"project artifact path is not a directory: {objects}")
    if args.resume_project is not None or (project_directory / "progress_manifest.json").exists():
        raise ValueError(f"missing project artifact directory: {objects}")
    objects.mkdir(parents=True)


def _loopback_postgres_url(value: str | None) -> str:
    if value is None:
        raise ValueError("real_hybrid requires a PostgreSQL database URL")
    parsed = urlparse(value)
    if (
        not parsed.scheme.startswith("postgresql+")
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
    ):
        raise ValueError("real_hybrid database must use a loopback PostgreSQL URL")
    database_name = parsed.path.removeprefix("/")
    if not database_name or len(database_name.encode("utf-8")) > 63:
        raise ValueError("PostgreSQL database name must contain at most 63 UTF-8 bytes")
    return value


def _loopback_http_url(value: str, label: str) -> ParseResult:
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
    return parsed


def _load_quality_repair_flags(args: argparse.Namespace) -> QualityRepairFeatureFlags:
    if args.quality_repair_config is None:
        return QualityRepairFeatureFlags()
    return QualityRepairFeatureFlags.model_validate_json(
        args.quality_repair_config.read_text("utf-8")
    )


def _ensure_experiment_manifest(
    args: argparse.Namespace,
    bundle: BenchmarkBundle,
    manifest_directory: Path,
    quality_repair_flags: QualityRepairFeatureFlags,
    *,
    project_directory: Path | None = None,
    require_clean_source: bool = False,
) -> None:
    manifest_directory.mkdir(parents=True, exist_ok=True)
    resolved_project_directory = (project_directory or manifest_directory).resolve()
    database_url = (
        _loopback_postgres_url(args.database_url)
        if args.retrieval_backend == RetrievalBackendProfile.REAL_HYBRID.value
        else args.database_url or f"sqlite:///{resolved_project_directory / 'project.sqlite3'}"
    )
    parsed_database = urlparse(database_url)
    database_descriptor = (
        f"{parsed_database.scheme}://{parsed_database.hostname}:{parsed_database.port}"
        f"{parsed_database.path}"
        if parsed_database.scheme.startswith("postgresql+")
        else database_url
    )
    repository_root = Path(__file__).parents[1]
    code_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    code_status = _source_status(repository_root)
    if require_clean_source and code_status.strip():
        raise ValueError(
            "formal Stage 2M run requires a clean executable source tree; "
            "commit or otherwise remove changes under src/, scripts/, schemas/, Makefile, "
            "and pyproject.toml before starting"
        )
    arms = getattr(args, "arms", "ABC")
    benchmark_runner = Stage2PairedPilotRunner(
        token_budget=getattr(args, "token_budget", 4000),
        max_candidates=getattr(args, "max_candidates", 20),
        max_tool_calls=getattr(args, "max_tool_calls", 48),
        arms=("A",) if arms == "A" else ("A", "B", "C"),
        retrieval_backend_profile=RetrievalBackendProfile(args.retrieval_backend),
        controller_mode=quality_repair_flags.controller_mode,
    )
    run_config_hash = benchmark_runner.public_configuration_fingerprint(
        bundle.bundle_schema_version.root
    )
    payload = {
        "schema_version": 3,
        "experiment_id": args.experiment_id,
        "code_commit": code_commit,
        "code_source_fingerprint": _code_source_fingerprint(repository_root).root,
        "code_source_dirty": bool(code_status.strip()),
        "benchmark_source": str(args.source.resolve()),
        "benchmark_content_hash": bundle.content_hash.root,
        "database": database_descriptor,
        "project_directory": str(resolved_project_directory),
        "retrieval_backend": args.retrieval_backend,
        "opensearch_url": _loopback_http_url(args.opensearch_url, "OpenSearch").geturl(),
        "embedding_url": _loopback_http_url(args.embedding_url, "embedding").geturl(),
        "reranker_url": _loopback_http_url(args.reranker_url, "reranker").geturl(),
        "model_base_url": args.model_base_url,
        "model": args.model,
        "information_profile": getattr(
            args,
            "information_profile",
            BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED.value,
        ),
        "memory_benchmark_contract_version": "memory_benchmark.v0.2",
        "public_task_template_version": TASK_TEMPLATE_VERSION,
        "task_focus_version": TaskFocusExtractor.version,
        "need_generation_profile": TaskPlanConditionedNeedGenerator.version,
        "claim_support_producer_version": TrustedClaimSupportProducer.version,
        "support_selection_policy_version": ControllerSupportSelector.version,
        "retrieval_unit_normalizer_version": RetrievalUnitNormalizer.version,
        "writer_context_profile": WriterContextAssembler.contract_version,
        "writer_context_assembler_version": WriterContextAssembler.version,
        "gold_evidence_matcher_version": GoldEvidenceMatcher.version,
        "evaluator_version": MemoryBenchmarkEvaluator.version,
        "gate_metric_formula_version": GATE_METRIC_FORMULA_VERSION,
        "gate_metric_formula_hash": GATE_METRIC_FORMULA_HASH.root,
        "code_version": Stage2PairedPilotRunner.version,
        "run_config_hash": run_config_hash.root,
        "benchmark_contract_hash": bundle.content_hash.root,
        "matcher_version": GoldEvidenceMatcher.version,
        "writer_token_budget": getattr(args, "token_budget", 4000),
        "evidence_ledger_token_budget": 12_000,
        "max_candidates": getattr(args, "max_candidates", 20),
        "max_tool_calls": getattr(args, "max_tool_calls", 48),
        "arms": getattr(args, "arms", "ABC"),
        "quality_repair_flags": quality_repair_flags.model_dump(mode="json"),
        "memory_write_dry_run": args.memory_write_dry_run,
        "resume_commit": getattr(args, "resume_commit", None),
        "resume_chapter": getattr(args, "resume_chapter", None),
    }
    path = manifest_directory / "experiment_manifest.json"
    if path.exists():
        existing = json.loads(path.read_text("utf-8"))
        if existing != payload:
            raise ValueError("experiment manifest differs from the requested run configuration")
        return
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=manifest_directory,
        prefix=".experiment-manifest.",
        delete=False,
        encoding="utf-8",
    ) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
    os.replace(temporary.name, path)


def _source_status(repository_root: Path) -> str:
    return subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            "src",
            "scripts",
            "schemas",
            "Makefile",
            "pyproject.toml",
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _assert_formal_source_clean(repository_root: Path) -> None:
    if _source_status(repository_root).strip():
        raise ValueError(
            "formal Stage 2M run requires a clean executable source tree; "
            "commit or otherwise remove changes under src/, scripts/, schemas/, Makefile, "
            "and pyproject.toml before starting"
        )


def _code_source_fingerprint(repository_root: Path) -> ArtifactId:
    """Bind formal runs to the exact executable source, including uncommitted files."""

    digest = hashlib.sha256()
    roots = (
        repository_root / "src",
        repository_root / "scripts",
        repository_root / "schemas",
    )
    files = [
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".pyc")
    ]
    files.extend((repository_root / "Makefile", repository_root / "pyproject.toml"))
    for path in sorted(files):
        relative = path.relative_to(repository_root).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + hashlib.sha256(path.read_bytes()).digest() + b"\n")
    return ArtifactId(f"sha256:{digest.hexdigest()}")


if __name__ == "__main__":
    raise SystemExit(main())
