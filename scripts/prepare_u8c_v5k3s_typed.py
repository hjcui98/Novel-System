#!/usr/bin/env python3
"""Prepare a fresh, typed U8-C development/held-out pair.

This is an operational seed builder for the v5k3u experiment.  It deliberately
does not call a model and does not modify the source project.  Each destination
gets a new genesis commit, a copied immutable object root, a typed
``PlanningProblemIdentitySeed`` embedded in a typed checkpoint, and an exact
real-hybrid projection attestation.  The Stage 5 runner is invoked separately
after this script succeeds.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import quote_plus

from opensearchpy import OpenSearch
from pydantic import BaseModel

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import HttpEmbeddingProvider, RetrievalModelRoute
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.models import CommitRow
from novel_agent.domain.artifacts import ArtifactId, ArtifactRef, RootManifest
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
)
from novel_agent.domain.ids import CommitId, ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.memory import NeedFacetKind
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRole
from novel_agent.domain.planning import (
    PlanningInquiry,
    PlanningLoopCheckpoint,
    PlanningLoopPhase,
    PlanningProblemIdentitySeed,
    PlanReview,
)
from novel_agent.domain.retrieval_routing import RetrievalBackendProfile
from novel_agent.domain.text import RangeUnit, SourceBoundEvidenceRequirement, TextSpanRef
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import (
    CommitService,
    ProjectAlreadyExistsError,
)
from novel_agent.services.embedding_cache import SqlEmbeddingCache
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedProjectionService,
    DerivedSnapshotRepository,
    FullDerivedProjectionBuilder,
    ProjectionOutboxRepository,
    snapshot_id_for_commit,
)
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.search_retrieval import Stage2RSearchIndexer

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB_NAME = "na_u8b_20260830_zrun31"
SOURCE_OBJECT_ROOT = Path("/tmp/novel-agent-u8b-basis-20260830-zrun31")
SOURCE_COMMIT = CommitId("sha256:0578778cb3fa84ee1b6d414fd52c09e9b44dbf7c0ca0dc9ac564e2d2cbfff8a7")
SOURCE_INQUIRY = Path(
    "/tmp/novel-agent-u8c-source-seed-20260831-v5k3r/sha256/ac/"
    "acee3738b7396b49b223ea6f05cfd6e3c667a3759600b1d8eea990fa2f150ac0"
)
SOURCE_REVIEW = Path(
    "/tmp/novel-agent-u8c-source-seed-20260831-v5k3r/sha256/ce/"
    "cefcabaa7242a1acb51d6723afe56b2a43d36b1e45bb29b231ffc966fddba4fe"
)
SOURCE_TEXT_ROOT = ArtifactId(
    "sha256:656c31df6507cc47aeff3220964fab4f1cd9737b31147221777d54f41ca97d83"
)
SOURCE_CHAPTER = 95
TARGET_CHAPTER = 96
EXPERIMENT_TAG = "v5k3u"
COMMON_QUESTION_ID = f"planner-memory.u8c.{EXPERIMENT_TAG}.gate-breach"
GATE_BREACH_QUESTION = "天海家派人撞破国教学院院门后，现场发生了哪些直接后果？"  # noqa: RUF001
GATE_BREACH_SEMANTIC_QUESTION = f"预注册: {GATE_BREACH_QUESTION}"
GATE_BREACH_SPAN = TextSpanRef(
    block_id=StableId("block.ZTJ-P005.95.0"),
    range_unit=RangeUnit.UNICODE_CODEPOINT,
    start=1472,
    end=2772,
)
GATE_BREACH_MARKERS = (
    "无数劲气在国教学院的院门口激射而出",
    "昏迷不醒，浑身是血，生死不知",  # noqa: RUF001
    "叫骂声、喝斥声，戛然而止",  # noqa: RUF001
)
POLICY_HASH = ArtifactId("sha256:013d685d9a1f3ff67dfbb5b3e92c2ad6eed1bbe36f83ed29f2f02c9407984f99")
PLANNING_CONFIGURATION = ArtifactId(
    "sha256:f1f94eee90abb633aff31e45f226ce4eab07af69d0c41b59f7c2fb02166bcab8"
)


DESTINATIONS = (
    {
        "suffix": "dev",
        "database": "na_u8c_20260901_v5k3udev",
        "project": "project.u8c.real.20260901.v5k3u.dev",
        "run": "u8c-real-20260901-v5k3udev",
        "incident": "incident.u8c.real.20260901.v5k3u.dev",
        "crash_chain": "crash-chain.u8c.real.20260901-v5k3u.dev",
    },
    {
        "suffix": "hol",
        "database": "na_u8c_20260901_v5k3uhol",
        "project": "project.u8c.real.20260901.v5k3u.hol",
        "run": "u8c-real-20260901-v5k3uhol",
        "incident": "incident.u8c.real.20260901.v5k3u.hol",
        "crash_chain": "crash-chain.u8c.real.20260901-v5k3u.hol",
    },
)


def _env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    # The native services are owned by the main worktree, while this script
    # runs from the isolated integration worktree. Use that service owner's
    # credential file explicitly; the isolated worktree has a different
    # development password and must not be used to address the live loopback
    # PostgreSQL instance.
    env_path = REPOSITORY_ROOT.parent.parent / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value.strip()
    return values


def _database_url(database: str) -> str:
    values = _env_values()
    return "postgresql+psycopg://{}:{}@127.0.0.1:{}/{}".format(
        quote_plus(values["POSTGRES_USER"]),
        quote_plus(values["POSTGRES_PASSWORD"]),
        values["POSTGRES_PORT"],
        database,
    )


def _json_bytes(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _code_source_fingerprint() -> ArtifactId:
    digest = hashlib.sha256()
    files: list[Path] = []
    for root in (REPOSITORY_ROOT / "src", REPOSITORY_ROOT / "scripts", REPOSITORY_ROOT / "schemas"):
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".pyc")
        )
    files.extend((REPOSITORY_ROOT / "Makefile", REPOSITORY_ROOT / "pyproject.toml"))
    for path in sorted(files):
        digest.update(
            path.relative_to(REPOSITORY_ROOT).as_posix().encode("utf-8")
            + b"\0"
            + hashlib.sha256(path.read_bytes()).digest()
            + b"\n"
        )
    return ArtifactId(f"sha256:{digest.hexdigest()}")


def _source_manifest() -> RootManifest:
    engine = build_engine(_database_url(SOURCE_DB_NAME))
    source_factory = build_session_factory(engine)
    try:
        with source_factory() as session:
            row = session.get(CommitRow, SOURCE_COMMIT.root)
            if row is None:
                raise RuntimeError(f"source commit is absent: {SOURCE_COMMIT.root}")
            return RootManifest.model_validate_json(json.dumps(row.manifest_json))
    finally:
        engine.dispose()


def _prepare_inquiry(project_id: ProjectId, suffix: str, repository: ArtifactRepository):
    raw = json.loads(SOURCE_INQUIRY.read_text(encoding="utf-8"))
    raw["project_id"] = project_id.root
    raw["inquiry_id"] = f"planning-inquiry.u8c.{EXPERIMENT_TAG}.{suffix}"
    questions = list(raw.get("questions", []))
    if not questions:
        raise RuntimeError("source inquiry has no questions")
    questions[0] = {
        **questions[0],
        "question_id": COMMON_QUESTION_ID,
        "question": GATE_BREACH_QUESTION,
        "entity_labels": ["天海家", "国教学院", "院门"],
        "relation_subject": None,
        "relation_predicate": None,
        "relation_object": None,
        "blocking": False,
    }
    # The original goal text names unrelated Chen Changsheng facts.  Keeping
    # that text would make the bounded task-focus closure add unrelated state
    # and relation IDs to the pre-registered need, which can falsely close a
    # causal-history gap.  This neutral reviewed goal preserves the task
    # contract while keeping the problem identity's entity frontier exact.
    goals = list(raw.get("goal_proposals", []))
    if goals:
        goals[0] = {
            **goals[0],
            "summary": "为下一章节准备一个经过证据审查的事实问题。",
        }
        raw["goal_proposals"] = goals
    raw["questions"] = questions
    inquiry = PlanningInquiry.model_validate_json(json.dumps(raw, ensure_ascii=False), strict=True)
    ref = repository.put(
        _json_bytes(inquiry),
        "application/vnd.novel-agent.planning-inquiry+json",
        SchemaVersion("1.0.0"),
    )
    return inquiry, ref


def _prepare_review(
    project_id: ProjectId,
    run_id: RunId,
    suffix: str,
    inquiry_ref,
    repository: ArtifactRepository,
):
    raw = json.loads(SOURCE_REVIEW.read_text(encoding="utf-8"))
    raw["review_id"] = f"plan-review.u8c.{EXPERIMENT_TAG}.{suffix}"
    raw["target_artifact_ref"] = inquiry_ref.model_dump(mode="json")
    raw["memory_gap_questions"] = [COMMON_QUESTION_ID]
    receipt = dict(raw.get("receipt", {}))
    receipt["run_id"] = run_id.root
    receipt["task_id"] = f"{run_id.root}.plan"
    receipt["base_commit"] = None
    # The source receipt is retained as provenance, but its old source-only
    # basis is not allowed to masquerade as the destination basis.
    raw["receipt"] = receipt
    try:
        # The receipt contract requires a commit id.  Reinsert the immutable
        # source commit here; the destination basis is carried by the typed
        # checkpoint and the runtime request.
        raw["receipt"]["base_commit"] = SOURCE_COMMIT.root
        review = PlanReview.model_validate_json(json.dumps(raw, ensure_ascii=False), strict=True)
    except Exception:
        # A fresh review receipt is not a model call.  Keep the reviewed
        # decision and target binding while using the source receipt unchanged.
        raw["receipt"] = json.loads(SOURCE_REVIEW.read_text(encoding="utf-8"))["receipt"]
        raw["receipt"]["run_id"] = run_id.root
        raw["receipt"]["task_id"] = f"{run_id.root}.plan"
        raw["receipt"]["base_commit"] = SOURCE_COMMIT.root
        review = PlanReview.model_validate_json(json.dumps(raw, ensure_ascii=False), strict=True)
    ref = repository.put(
        _json_bytes(review),
        "application/vnd.novel-agent.plan-review+json",
        SchemaVersion("1.0.0"),
    )
    return review, ref


def _prepare_seed(question_id: StableId) -> PlanningProblemIdentitySeed:
    requirement = SourceBoundEvidenceRequirement(
        source_artifact_id=SOURCE_TEXT_ROOT,
        source_chapter_index=SOURCE_CHAPTER,
        source_chapter_id=StableId("chapter.ZTJ-P005.95"),
        required_span=GATE_BREACH_SPAN,
        required_consequence_markers=GATE_BREACH_MARKERS,
    )
    return PlanningProblemIdentitySeed(
        need_id=StableId(f"need.u8c.preregistered.gate-breach.z31.{EXPERIMENT_TAG}"),
        question_id=question_id,
        need_query=GATE_BREACH_QUESTION,
        semantic_question=GATE_BREACH_SEMANTIC_QUESTION,
        facet=NeedFacetKind.CAUSAL_HISTORY,
        source_commit=SOURCE_COMMIT,
        source_text_root=SOURCE_TEXT_ROOT,
        cutoff_chapter=SOURCE_CHAPTER,
        source_evidence_requirement=requirement,
    )


def _prepare_checkpoint(
    *,
    project_id: ProjectId,
    run_id: RunId,
    basis: CommitId,
    snapshot: StableId,
    inquiry_ref,
    review_ref,
    seed: PlanningProblemIdentitySeed,
    repository: ArtifactRepository,
):
    checkpoint = PlanningLoopCheckpoint(
        checkpoint_id=StableId(f"planning-checkpoint.u8c.{EXPERIMENT_TAG}.{run_id.root}"),
        request_id=StableId(f"planning-request.{run_id.root}.plan"),
        phase=PlanningLoopPhase.INQUIRY_ACCEPTED,
        base_commit=basis,
        snapshot_id=snapshot,
        configuration_fingerprint=PLANNING_CONFIGURATION,
        inquiry_ref=inquiry_ref,
        inquiry_review_ref=review_ref,
        problem_identity_seed=seed,
        pending_planner_memory_questions=(seed.need_query,),
    )
    ref = repository.put(
        _json_bytes(checkpoint),
        "application/vnd.novel-agent.planning-loop-checkpoint+json",
        SchemaVersion("1.0.0"),
    )
    return checkpoint, ref


def _prepare_request(
    *,
    project_id: ProjectId,
    run_id: RunId,
    basis: CommitId,
    snapshot: StableId,
    checkpoint_ref,
    author_intent_ref,
):
    request = CreativeRunRequest(
        run_id=run_id,
        project_id=project_id,
        basis_commit=basis,
        basis_snapshot=snapshot,
        policy=CreativeRunPolicy(
            automation_mode=AutomationMode.AUTO,
            policy_hash=POLICY_HASH.root,
            permission_hash=POLICY_HASH.root,
            auto_accept_plan=True,
            auto_accept_draft=True,
            max_task_attempts=3,
            max_tasks_per_advance=1,
            planning_horizon=5,
            runtime_parallelism=1,
            enable_planner_lookahead=False,
            lookahead_horizon=3,
        ),
        input_artifact_refs=(author_intent_ref,),
        continuation_artifact_refs=(checkpoint_ref,),
        current_chapter=SOURCE_CHAPTER,
        target_chapters=TARGET_CHAPTER,
    )
    return request


def _build_projection(
    *,
    project_id: ProjectId,
    run_id: RunId,
    basis: CommitId,
    object_root: Path,
    database_url: str,
    suffix: str,
):
    try:
        from scripts.native_models import assert_model_service, load_model_lock
    except ModuleNotFoundError:  # pragma: no cover
        from native_models import assert_model_service, load_model_lock  # type: ignore[no-redef]

    lock = load_model_lock()
    embedding_model = lock.models["embedding"]
    reranker_model = lock.models["reranker"]
    assert_model_service(embedding_model)
    assert_model_service(reranker_model)
    engine = build_engine(database_url)
    factory = build_session_factory(engine)
    search_client = OpenSearch(hosts=[{"host": "127.0.0.1", "port": 9200}])
    if not search_client.ping():
        search_client.close()
        engine.dispose()
        raise RuntimeError("OpenSearch is unavailable")
    projection_run = RunId(f"run.u8c.{EXPERIMENT_TAG}.projection.{suffix}")
    embedder = HttpEmbeddingProvider(
        RetrievalModelRoute(
            endpoint="http://127.0.0.1:8081/v1/embeddings",
            model=embedding_model.model_id,
            revision=embedding_model.revision,
            runtime_fingerprint=embedding_model.runtime_fingerprint,
            run_id=projection_run,
            task_id=TaskId(f"task.u8c.{EXPERIMENT_TAG}.projection.{suffix}"),
            trace_id=f"trace.u8c.{EXPERIMENT_TAG}.projection.{suffix}",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        ),
        dimension=embedding_model.dimension or 0,
        batch_size=32,
    )
    repo = ArtifactRepository(FilesystemObjectStore(object_root))
    search = Stage2RSearchIndexer(
        OpenSearchIndex(search_client),
        embedder,
        embedding_cache=SqlEmbeddingCache(factory),
        index_namespace=f"u8c-{EXPERIMENT_TAG}-{suffix}",
    )
    builder = FullDerivedProjectionBuilder(
        ArtifactProjectionSourceLoader(CommitService(factory), repo),
        R1WorldRepository(factory),
        search,
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        build_profile=f"u8c-{EXPERIMENT_TAG}-real-hybrid.v1",
        embedding_model=embedding_model.model_id,
        embedding_revision=embedding_model.revision,
        embedding_runtime_fingerprint=ArtifactId(f"sha256:{embedding_model.runtime_fingerprint}"),
        reranker_model=reranker_model.model_id,
        reranker_revision=reranker_model.revision,
    )
    service = DerivedProjectionService(
        ProjectionOutboxRepository(factory),
        builder,
        worker_id=f"projection-worker.u8c.{EXPERIMENT_TAG}.{suffix}",
        project_id=project_id,
    )
    processed = service.process_all(max_items=1)
    if processed != 1:
        raise RuntimeError(f"expected one projection outbox item, got {processed}")
    snapshots = DerivedSnapshotRepository(factory)
    snapshot = snapshots.get_for_commit(basis)
    attestation = snapshots.get_attestation_for_commit(basis)
    if snapshot is None or attestation is None:
        raise RuntimeError("exact real-hybrid projection attestation is missing")
    result = {
        "processed_outbox_items": processed,
        "snapshot_id": snapshot.snapshot_id.root,
        "source_commit": snapshot.source_commit.root,
        "build_status": snapshot.build_status.value,
        "retrieval_backend_profile": snapshot.retrieval_backend_profile,
        "anchor_index_version": snapshot.anchor_index_version,
        "grounded_index_version": snapshot.grounded_index_version,
        "projection_attestation": attestation.model_dump(mode="json"),
    }
    search_client.close()
    engine.dispose()
    return result


def _preflight_source(manifest: RootManifest, repository: ArtifactRepository, basis: CommitId):
    from novel_agent.domain.benchmark import TextRootDocument
    from novel_agent.domain.memory import WorldRootDocument

    text = TextRootDocument.model_validate_json(
        repository.read_verified(manifest.text_root), strict=True
    )
    world = WorldRootDocument.model_validate_json(
        repository.read_verified(manifest.world_root), strict=True
    )
    chapter = next(item for item in text.chapters if item.chapter_index == SOURCE_CHAPTER)
    block = next(
        block
        for scene in chapter.scenes
        for block in scene.blocks
        if block.block_id == GATE_BREACH_SPAN.block_id
    )
    span_text = block.text[GATE_BREACH_SPAN.start : GATE_BREACH_SPAN.end]
    if any(marker not in span_text for marker in GATE_BREACH_MARKERS):
        raise RuntimeError("C95 source span does not contain all registered gate-breach markers")
    by_label = {entity.internal_label: entity.entity_id for entity in world.entities}
    xue = by_label.get("大周御天神将薛醒川") or by_label.get("薛醒川")
    red = by_label.get("红云麟")
    if xue is None or red is None:
        raise RuntimeError("typed C95 entities are absent from the frozen WorldRoot")
    relation_matches = [
        relation
        for relation in world.relations
        if {relation.subject_id, relation.object_id} == {xue, red}
    ]
    return {
        "source_commit": SOURCE_COMMIT.root,
        "destination_basis_commit": basis.root,
        "text_root": manifest.text_root.artifact_id.root,
        "chapter_id": chapter.chapter_id.root,
        "block_id": block.block_id.root,
        "span": GATE_BREACH_SPAN.model_dump(mode="json"),
        "span_markers_verified": True,
        "entity_ids": {"xue": xue.root, "red": red.root},
        "typed_graph_relation_matches": len(relation_matches),
        "typed_graph_relation_covered": bool(relation_matches),
        "anchor_pair_coverage": False,
        "failure_class": "canon_extraction_gap",
        "safety_action_allowlist": ["graph_curator", "ordinary_curator", "review_required"],
    }


def prepare_one(source_manifest: RootManifest, spec: dict[str, str], code_fingerprint: ArtifactId):
    project_id = ProjectId(spec["project"])
    run_id = RunId(spec["run"])
    suffix = spec["suffix"]
    object_root = Path(f"/tmp/novel-agent-u8c-stage2r-20260901-{EXPERIMENT_TAG}{suffix}/objects")
    output_root = Path(f"/tmp/novel-agent-u8c-20260901-{EXPERIMENT_TAG}{suffix}")
    # A failed preparation is resumable only within this newly allocated
    # identity.  Never overwrite bytes: copy the source tree only when the
    # destination object root is absent, and leave any existing output files
    # untouched.
    if not object_root.exists():
        object_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SOURCE_OBJECT_ROOT, object_root)
    if not output_root.exists():
        output_root.mkdir(parents=True, exist_ok=False)
    target_manifest = source_manifest.model_copy(
        update={"project_id": project_id, "parent_commit_ids": ()}
    )
    database_url = _database_url(spec["database"])
    engine = build_engine(database_url)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    try:
        basis = commits.initialize_project(target_manifest)
    except ProjectAlreadyExistsError:
        basis = commits.current_commit(project_id)
    engine.dispose()
    repository = ArtifactRepository(FilesystemObjectStore(object_root))
    author_intent_ref = ArtifactRef(
        artifact_id=ArtifactId(
            "sha256:7a9af2490043b54dad60a65591f12d5c04028ea7a37a5460109e75ec33db70e1"
        ),
        media_type="text/markdown",
        byte_length=2713,
        schema_version=SchemaVersion("1.0.0"),
    )
    # The author brief is a source artifact in the copied object root; verify
    # it before binding the request so a stale path cannot pass preflight.
    repository.read_verified(author_intent_ref)
    _inquiry, inquiry_ref = _prepare_inquiry(project_id, suffix, repository)
    _review, review_ref = _prepare_review(project_id, run_id, suffix, inquiry_ref, repository)
    question_id = StableId(COMMON_QUESTION_ID)
    seed = _prepare_seed(question_id)
    snapshot = snapshot_id_for_commit(basis)
    _checkpoint, checkpoint_ref = _prepare_checkpoint(
        project_id=project_id,
        run_id=run_id,
        basis=basis,
        snapshot=snapshot,
        inquiry_ref=inquiry_ref,
        review_ref=review_ref,
        seed=seed,
        repository=repository,
    )
    request = _prepare_request(
        project_id=project_id,
        run_id=run_id,
        basis=basis,
        snapshot=snapshot,
        checkpoint_ref=checkpoint_ref,
        author_intent_ref=author_intent_ref,
    )
    preflight = _preflight_source(target_manifest, repository, basis)
    projection = _build_projection(
        project_id=project_id,
        run_id=run_id,
        basis=basis,
        object_root=object_root,
        database_url=database_url,
        suffix=suffix,
    )
    payload = {
        "schema": f"u8c-{EXPERIMENT_TAG}-typed-basis.v1",
        "suffix": suffix,
        "project_id": project_id.root,
        "run_id": run_id.root,
        "database_descriptor": f"postgresql://127.0.0.1:5432/{spec['database']}",
        "object_store_root": str(object_root),
        "output_root": str(output_root),
        "basis_commit": basis.root,
        "basis_snapshot": snapshot.root,
        "source_commit": SOURCE_COMMIT.root,
        "source_text_root": SOURCE_TEXT_ROOT.root,
        "code_source_fingerprint": code_fingerprint.root,
        "inquiry_ref": inquiry_ref.model_dump(mode="json"),
        "review_ref": review_ref.model_dump(mode="json"),
        "checkpoint_ref": checkpoint_ref.model_dump(mode="json"),
        "problem_identity": seed.model_dump(mode="json"),
        "request": request.model_dump(mode="json"),
        "source_preflight": preflight,
        "projection": projection,
        "fresh_identity": True,
        "production_reasoner_enabled": False,
        "production_hot_swap_enabled": False,
    }
    (output_root / "basis-receipt.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_root / "problem-identity.json").write_text(
        json.dumps(seed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (output_root / "run-request.json").write_text(
        json.dumps(request.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (output_root / "source-preflight.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_root / "projection-preflight.json").write_text(
        json.dumps(projection, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> int:
    source_manifest = _source_manifest()
    if source_manifest.project_id.root != "project.u8b.natural.20260830.zrun31":
        raise RuntimeError("unexpected source project identity")
    code_fingerprint = _code_source_fingerprint()
    results = [prepare_one(source_manifest, spec, code_fingerprint) for spec in DESTINATIONS]
    print(
        json.dumps(
            {"code_source_fingerprint": code_fingerprint.root, "destinations": results},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
