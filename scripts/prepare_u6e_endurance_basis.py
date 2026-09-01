#!/usr/bin/env python3
"""Create a fresh U6-E basis from a frozen terminal Commit.

The source project and Commit remain read-only.  The destination receives the
same immutable canonical roots under a fresh project identity, so a 50-chapter
endurance run can continue from an already accepted production terminal state
without reusing the failed run or changing the production default.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

try:
    from scripts.prepare_u5_c20_isolated_basis import (
        copy_canonical_roots,
        database_descriptor,
        isolated_manifest,
    )
except ModuleNotFoundError as error:
    if error.name != "scripts":
        raise
    from prepare_u5_c20_isolated_basis import (
        copy_canonical_roots,
        database_descriptor,
        isolated_manifest,
    )
from sqlalchemy import inspect, text

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.artifacts import ArtifactRef, RootManifest
from novel_agent.domain.benchmark import TextRootDocument
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
from novel_agent.domain.planning import (
    PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
    PlanningInquiry,
    PlanningLoopCheckpoint,
    PlanningLoopPhase,
    PlanningProblemIdentitySeed,
    PlanReview,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    ExecutionStatus,
    ReferenceRootDocument,
    SourceClass,
)
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.runtime.production_bootstrap import (
    QWEN38_27B_FP8_8005_ENDPOINT_PROFILE,
    QWEN38_27B_FP8_MODEL,
    _default_stage4_policy,
    load_production_assembly_spec,
    resolve_registered_model_endpoints,
)
from novel_agent.services.artifacts import ArtifactRepository, object_key, sha256_id
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.projection import snapshot_id_for_commit

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "u6e-endurance-basis.v3"
ALEMBIC_HEAD = "0010_model_call_ledger"
REFERENCE_INPUT_MODES = ("author_initial_brief", "all")
PLANNING_SCHEMA_VERSION = SchemaVersion("1.0.0")
REQUIRED_TABLES = (
    "alembic_version",
    "project",
    "project_commit",
    "commit_receipt",
    "run_stream",
    "runtime_task_projection",
    "runtime_task_attempt",
    "model_call_ledger",
)


def _write_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"U6-E refuses to overwrite preparation output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _expected_chapters(
    current_chapter: int, target_chapters: int, *, canary: bool = False
) -> tuple[int, ...]:
    expected_delta = 1 if canary else 50
    if target_chapters - current_chapter != expected_delta:
        requirement = "one canary chapter" if canary else "exactly fifty target chapters"
        raise ValueError(f"U6-E requires {requirement} after the frozen basis")
    return tuple(range(current_chapter + 1, target_chapters + 1))


def _select_reference_inputs(
    reference: ReferenceRootDocument, *, mode: str
) -> tuple[ArtifactRef, ...]:
    if mode not in REFERENCE_INPUT_MODES:
        raise ValueError(
            f"unsupported reference input mode {mode!r}; expected one of {REFERENCE_INPUT_MODES}"
        )
    if mode == "all":
        return tuple(asset.artifact for asset in reference.assets)
    return tuple(
        asset.artifact
        for asset in reference.assets
        if asset.source_class is SourceClass.AUTHOR_INITIAL_BRIEF
    )


def _checkpoint_ref_from_path(
    path: Path, *, source_object_root: Path, source_artifacts: ArtifactRepository
) -> tuple[ArtifactRef, bytes]:
    """Read one immutable checkpoint object and verify its object-store identity."""

    if not path.is_file() or path.name.endswith(".metadata.json"):
        raise ValueError("Planner continuation seed must be an object-store checkpoint file")
    data = path.read_bytes()
    artifact_id = sha256_id(data)
    if path.name != artifact_id.root.removeprefix("sha256:"):
        raise ValueError("Planner continuation seed filename does not match its content hash")
    expected_path = (source_object_root / object_key(artifact_id)).resolve()
    if path.resolve() != expected_path:
        raise ValueError("Planner continuation seed must belong to the source object store")
    metadata_path = Path(f"{path}.metadata.json")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        media_type = metadata["media_type"]
        byte_length = int(metadata["byte_length"])
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError("Planner continuation seed metadata is invalid") from error
    if media_type != PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE:
        raise ValueError("Planner continuation seed is not a planning-loop checkpoint")
    ref = ArtifactRef(
        artifact_id=ArtifactId(artifact_id.root),
        media_type=media_type,
        byte_length=byte_length,
        schema_version=PLANNING_SCHEMA_VERSION,
    )
    return ref, source_artifacts.read_verified(ref)


def _load_problem_identity_seed(path: Path) -> PlanningProblemIdentitySeed:
    """Read one pre-registered problem identity without accepting model output."""

    try:
        return PlanningProblemIdentitySeed.model_validate_json(path.read_bytes(), strict=True)
    except (OSError, ValueError) as error:
        raise ValueError(f"problem identity seed is invalid: {path}") from error


def _seed_planner_continuation(
    *,
    source_checkpoint_path: Path,
    source_object_root: Path,
    source_artifacts: ArtifactRepository,
    destination_artifacts: ArtifactRepository,
    input_assets: tuple[ArtifactRef, ...],
    source_commit: CommitId,
    destination_project_id: ProjectId,
    destination_basis: CommitId,
    destination_snapshot: StableId,
    run_id: RunId,
    current_chapter: int,
    target_chapters: int,
    problem_identity_seed: PlanningProblemIdentitySeed | None = None,
    source_text_root: ArtifactRef | None = None,
) -> tuple[ArtifactRef, dict[str, object]]:
    """Rebase only an accepted inquiry frontier onto a fresh Stage 5 basis.

    A seed may carry the frozen Planner's task intent, but never resolved Memory,
    Planner context, proposal, review, or execution artifacts.  Those are rebuilt
    against the isolated destination Commit by the normal runtime.
    """

    checkpoint_ref, checkpoint_bytes = _checkpoint_ref_from_path(
        source_checkpoint_path,
        source_object_root=source_object_root,
        source_artifacts=source_artifacts,
    )
    checkpoint = PlanningLoopCheckpoint.model_validate_json(checkpoint_bytes, strict=True)
    if checkpoint.phase is not PlanningLoopPhase.INQUIRY_ACCEPTED:
        raise ValueError("Planner continuation seed must stop at INQUIRY_ACCEPTED")
    if checkpoint.base_commit != source_commit:
        raise ValueError("Planner continuation seed belongs to another source Commit")
    if problem_identity_seed is not None:
        if problem_identity_seed.source_commit != source_commit:
            raise ValueError("problem identity seed belongs to another source Commit")
        if (
            source_text_root is None
            or problem_identity_seed.source_text_root != source_text_root.artifact_id
        ):
            raise ValueError("problem identity seed belongs to another source TextRoot")
        if problem_identity_seed.cutoff_chapter != current_chapter:
            raise ValueError("problem identity seed cutoff differs from the source basis")
    if checkpoint.problem_identity_seed is not None and (
        problem_identity_seed is None or checkpoint.problem_identity_seed != problem_identity_seed
    ):
        raise ValueError("Planner continuation seed problem identity does not match the run")
    if any(
        ref is not None
        for ref in (
            checkpoint.memory_context_ref,
            checkpoint.planner_context_ref,
            checkpoint.proposal_ref,
            checkpoint.plan_review_ref,
            checkpoint.execution_ref,
        )
    ):
        raise ValueError("Planner continuation seed must not carry resolved or execution state")
    if checkpoint.reviewer_context_refs or checkpoint.planner_memory_context_refs:
        raise ValueError("Planner continuation seed must not carry context artifacts")
    seeded_pending_question = (
        problem_identity_seed is not None
        and checkpoint.problem_identity_seed == problem_identity_seed
        and checkpoint.pending_planner_memory_questions == (problem_identity_seed.need_query,)
    )
    if (
        checkpoint.reviewer_memory_review_ids
        or checkpoint.handled_memory_question_ids
        or checkpoint.deferred_memory_question_ids
        or (checkpoint.pending_planner_memory_questions and not seeded_pending_question)
    ):
        raise ValueError("Planner continuation seed must not carry Memory progress state")
    expected_configuration = _default_stage4_policy(
        load_production_assembly_spec()
    ).configuration_fingerprint
    if checkpoint.configuration_fingerprint != expected_configuration:
        raise ValueError("Planner continuation seed uses a stale Stage 4 configuration")
    if checkpoint.inquiry_ref is None or checkpoint.inquiry_review_ref is None:
        raise ValueError("Planner continuation seed requires inquiry and review artifacts")
    inquiry = PlanningInquiry.model_validate_json(
        source_artifacts.read_verified(checkpoint.inquiry_ref), strict=True
    )
    review = PlanReview.model_validate_json(
        source_artifacts.read_verified(checkpoint.inquiry_review_ref), strict=True
    )
    if review.target_kind is not ReviewTargetKind.INQUIRY:
        raise ValueError("Planner continuation seed review must target the inquiry")
    if review.target_artifact_ref != checkpoint.inquiry_ref:
        raise ValueError("Planner continuation seed review targets another inquiry")
    if review.decision is not ReviewDecision.ACCEPT:
        raise ValueError("Planner continuation seed requires an accepted inquiry review")
    if inquiry.author_intent_refs != input_assets:
        raise ValueError("Planner continuation seed does not match selected input assets")
    if problem_identity_seed is not None:
        matching_questions = tuple(
            question
            for question in (*inquiry.assumptions, *inquiry.questions)
            if question.question_id == problem_identity_seed.question_id
        )
        if len(matching_questions) != 1:
            raise ValueError("problem identity seed question is not present exactly once")
        if matching_questions[0].question.strip() != problem_identity_seed.need_query:
            raise ValueError("problem identity seed query differs from the source inquiry")
    if inquiry.horizon_start != current_chapter + 1 or (
        inquiry.horizon_end is None or inquiry.horizon_end > target_chapters
    ):
        raise ValueError("Planner continuation seed horizon does not match the destination run")

    destination_inquiry = inquiry.model_copy(
        update={
            "inquiry_id": StableId(f"planning-inquiry.{run_id.root}.seed"[:128]),
            "project_id": destination_project_id,
            "author_intent_refs": input_assets,
        }
    )
    destination_inquiry_ref = destination_artifacts.put(
        canonical_json_bytes(destination_inquiry.model_dump(mode="json")),
        checkpoint.inquiry_ref.media_type,
        checkpoint.inquiry_ref.schema_version,
    )
    seed_receipt: AgentExecutionReceipt = review.receipt.model_copy(
        update={
            "receipt_id": StableId(f"agent-receipt.{run_id.root}.inquiry-seed"[:128]),
            "run_id": run_id,
            "task_id": TaskId(f"{run_id.root}.plan"[:128]),
            "base_commit": destination_basis,
            "input_artifacts": (*input_assets, destination_inquiry_ref),
            "output_artifacts": (),
            "skill_receipts": (),
            "model_call_ids": (),
            "tool_call_ids": (),
            "unresolved": tuple(
                dict.fromkeys((*review.receipt.unresolved, "FROZEN_ACCEPTED_INQUIRY_REVIEW_SEED"))
            ),
            "status": ExecutionStatus.SKIPPED,
            "latency_ms": 0,
        }
    )
    destination_review = review.model_copy(
        update={
            "review_id": StableId(f"plan-review.{run_id.root}.inquiry-seed"[:128]),
            "target_artifact_ref": destination_inquiry_ref,
            "receipt": seed_receipt,
        }
    )
    destination_review_ref = destination_artifacts.put(
        canonical_json_bytes(destination_review.model_dump(mode="json")),
        checkpoint.inquiry_review_ref.media_type,
        checkpoint.inquiry_review_ref.schema_version,
    )
    destination_checkpoint = PlanningLoopCheckpoint(
        checkpoint_id=StableId("planning-checkpoint.pending"),
        request_id=StableId(f"planning-request.{run_id.root}.plan"[:128]),
        phase=PlanningLoopPhase.INQUIRY_ACCEPTED,
        base_commit=destination_basis,
        snapshot_id=destination_snapshot,
        configuration_fingerprint=expected_configuration,
        inquiry_ref=destination_inquiry_ref,
        inquiry_review_ref=destination_review_ref,
        problem_identity_seed=problem_identity_seed,
        pending_planner_memory_questions=checkpoint.pending_planner_memory_questions,
        inquiry_revisions_used=checkpoint.inquiry_revisions_used,
        model_calls_used=checkpoint.model_calls_used,
        model_input_tokens_used=checkpoint.model_input_tokens_used,
        model_output_tokens_used=checkpoint.model_output_tokens_used,
        model_reasoning_tokens_used=checkpoint.model_reasoning_tokens_used,
    )
    checkpoint_id = content_id(
        destination_checkpoint.model_dump(mode="json", exclude={"checkpoint_id"})
    ).root.removeprefix("sha256:")[:24]
    destination_checkpoint = destination_checkpoint.model_copy(
        update={"checkpoint_id": StableId(f"planning-checkpoint.{checkpoint_id}")}
    )
    destination_checkpoint_ref = destination_artifacts.put(
        canonical_json_bytes(destination_checkpoint.model_dump(mode="json")),
        PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
        checkpoint_ref.schema_version,
    )
    return destination_checkpoint_ref, {
        "source_checkpoint_ref": checkpoint_ref.model_dump(mode="json"),
        "source_inquiry_ref": checkpoint.inquiry_ref.model_dump(mode="json"),
        "source_inquiry_review_ref": checkpoint.inquiry_review_ref.model_dump(mode="json"),
        "destination_checkpoint_ref": destination_checkpoint_ref.model_dump(mode="json"),
        "destination_inquiry_ref": destination_inquiry_ref.model_dump(mode="json"),
        "destination_inquiry_review_ref": destination_review_ref.model_dump(mode="json"),
        "mode": "accepted_inquiry_only",
        "problem_identity_seed": (
            None if problem_identity_seed is None else problem_identity_seed.model_dump(mode="json")
        ),
    }


def _code_identity() -> dict[str, object]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return {
        "head": head,
        "dirty": bool(status),
        "dirty_paths": [line[3:] if len(line) >= 3 else line for line in status],
    }


def _probe_json(url: str) -> Mapping[str, object]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=8.0) as response:
            if response.status != 200:
                raise RuntimeError(f"endpoint returned HTTP {response.status}: {url}")
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError) as error:
        raise RuntimeError(f"endpoint probe failed: {url}: {error}") from error
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"endpoint probe did not return an object: {url}")
    return payload


def _probe_health(url: str) -> dict[str, object]:
    request = Request(url, headers={"Accept": "*/*"})
    try:
        with urlopen(request, timeout=8.0) as response:
            if response.status != 200:
                raise RuntimeError(f"endpoint returned HTTP {response.status}: {url}")
            body_present = bool(response.read())
    except (OSError, URLError) as error:
        raise RuntimeError(f"endpoint health probe failed: {url}: {error}") from error
    return {"http_status": 200, "body_present": body_present}


def _preflight_target_database(
    *,
    database_url: str,
    project_id: ProjectId,
    run_id: RunId,
) -> dict[str, object]:
    engine = build_engine(database_url)
    try:
        table_names = set(inspect(engine).get_table_names())
        missing_tables = sorted(set(REQUIRED_TABLES) - table_names)
        if missing_tables:
            raise RuntimeError(f"U6-E target database is missing required tables: {missing_tables}")
        with engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            if revision != ALEMBIC_HEAD:
                raise RuntimeError(
                    f"U6-E target database migration is {revision!r}, expected {ALEMBIC_HEAD!r}"
                )
            project_exists = connection.execute(
                text("SELECT 1 FROM project WHERE project_id = :project_id LIMIT 1"),
                {"project_id": project_id.root},
            ).scalar()
            run_exists = connection.execute(
                text("SELECT 1 FROM run_stream WHERE run_id = :run_id LIMIT 1"),
                {"run_id": run_id.root},
            ).scalar()
        if project_exists is not None or run_exists is not None:
            raise RuntimeError("U6-E destination project or run identity already exists")
        return {
            "database_created": True,
            "alembic_head": revision,
            "required_tables": list(REQUIRED_TABLES),
            "identity_free": True,
        }
    finally:
        engine.dispose()


def _preflight_endpoint(
    *,
    endpoint_profile: str,
    health_url: str,
    models_url: str,
) -> dict[str, object]:
    if endpoint_profile != QWEN38_27B_FP8_8005_ENDPOINT_PROFILE:
        raise RuntimeError(
            f"U6-E R5 requires the registered 8005 endpoint, got {endpoint_profile!r}"
        )
    endpoints = resolve_registered_model_endpoints(endpoint_profile)
    implementation = next(
        endpoint for endpoint in endpoints if endpoint.model_name == QWEN38_27B_FP8_MODEL
    )
    health = _probe_health(health_url)
    models = _probe_json(models_url)
    model_rows = models.get("data")
    if not isinstance(model_rows, list) or not any(
        isinstance(row, Mapping) and row.get("id") == implementation.model_name
        for row in model_rows
    ):
        raise RuntimeError(f"registered model {implementation.model_name!r} was not advertised")
    return {
        "profile": endpoint_profile,
        "endpoint_name": implementation.endpoint_name,
        "model_name": implementation.model_name,
        "revision": implementation.revision,
        "health_url": health_url,
        "models_url": models_url,
        "health_ok": health["http_status"] == 200,
        "models_ok": True,
        "8003_dependency": False,
        "health_body_present": health["body_present"],
    }


def _preflight(
    *,
    destination_database_url: str,
    source_project_id: ProjectId,
    destination_project_id: ProjectId,
    run_id: RunId,
    source_object_root: Path,
    destination_object_root: Path,
    endpoint_profile: str,
    endpoint_health_url: str,
    endpoint_models_url: str,
    output_paths: tuple[Path, ...],
    output_root: Path | None,
) -> dict[str, object]:
    if destination_object_root.exists() and any(destination_object_root.iterdir()):
        raise RuntimeError("U6-E destination object root must be new and empty")
    occupied = [str(path) for path in output_paths if path.exists()]
    if occupied:
        raise RuntimeError(f"U6-E preparation output identity already exists: {occupied}")
    if output_root is not None and output_root.exists() and any(output_root.iterdir()):
        raise RuntimeError("U6-E target output root must be new and empty")
    database = _preflight_target_database(
        database_url=destination_database_url,
        project_id=destination_project_id,
        run_id=run_id,
    )
    endpoint = _preflight_endpoint(
        endpoint_profile=endpoint_profile,
        health_url=endpoint_health_url,
        models_url=endpoint_models_url,
    )
    if not source_object_root.exists():
        raise RuntimeError(f"U6-E source object root does not exist: {source_object_root}")
    return {
        "status": "PASS",
        "schema_revision": ALEMBIC_HEAD,
        "source_project_id": source_project_id.root,
        "destination_project_id": destination_project_id.root,
        "run_id": run_id.root,
        "database": database,
        "endpoint": endpoint,
        "object_root_empty": True,
        "output_identity_free": True,
        "code": _code_identity(),
    }


def prepare(
    *,
    source_database_url: str,
    source_project_id: ProjectId,
    source_commit: CommitId,
    source_object_root: Path,
    destination_database_url: str,
    destination_project_id: ProjectId,
    destination_object_root: Path,
    run_id: RunId,
    target_chapters: int,
    canary: bool = False,
    endpoint_profile: str = QWEN38_27B_FP8_8005_ENDPOINT_PROFILE,
    endpoint_health_url: str = "http://127.0.0.1:8005/health",
    endpoint_models_url: str = "http://127.0.0.1:8005/v1/models",
    output_paths: tuple[Path, ...] = (),
    output_root: Path | None = None,
    allow_historical_c40_source: bool = False,
    reference_input_mode: str = "author_initial_brief",
    planner_continuation_checkpoint: Path | None = None,
    problem_identity_seed_path: Path | None = None,
) -> tuple[dict[str, object], CreativeRunRequest]:
    preflight: dict[str, object] | None = None
    if output_paths:
        preflight = _preflight(
            destination_database_url=destination_database_url,
            source_project_id=source_project_id,
            destination_project_id=destination_project_id,
            run_id=run_id,
            source_object_root=source_object_root,
            destination_object_root=destination_object_root,
            endpoint_profile=endpoint_profile,
            endpoint_health_url=endpoint_health_url,
            endpoint_models_url=endpoint_models_url,
            output_paths=output_paths,
            output_root=output_root,
        )
    source_engine = build_engine(source_database_url)
    destination_engine = build_engine(destination_database_url)
    try:
        source_commits = CommitService(build_session_factory(source_engine))
        source_current = source_commits.current_commit(source_project_id)
        if not allow_historical_c40_source and source_current != source_commit:
            raise RuntimeError("U6-E source Commit must be the current frozen project head")
        source_manifest = source_commits.load_manifest(source_commit)
        if source_manifest.project_id != source_project_id:
            raise RuntimeError("U6-E source Commit belongs to another project")
        source_artifacts = ArtifactRepository(FilesystemObjectStore(source_object_root))
        source_text = TextRootDocument.model_validate_json(
            source_artifacts.read_verified(source_manifest.text_root), strict=True
        )
        if not source_text.chapters:
            raise RuntimeError("U6-E source frozen basis has no chapters")
        chapter_indexes = tuple(chapter.chapter_index for chapter in source_text.chapters)
        if chapter_indexes != tuple(range(chapter_indexes[0], chapter_indexes[-1] + 1)):
            raise RuntimeError("U6-E source frozen basis has a non-contiguous chapter history")
        current_chapter = chapter_indexes[-1]
        if allow_historical_c40_source and current_chapter != 40:
            raise RuntimeError("U6-E historical source mode requires an exactly C40 basis")
        expected_chapters = _expected_chapters(current_chapter, target_chapters, canary=canary)

        reference = ReferenceRootDocument.model_validate_json(
            source_artifacts.read_verified(source_manifest.reference_root), strict=True
        )
        author_assets = _select_reference_inputs(reference, mode="author_initial_brief")
        if not author_assets:
            raise RuntimeError("U6-E source frozen basis lacks the author initial brief")
        input_assets = _select_reference_inputs(reference, mode=reference_input_mode)
        if not input_assets:
            raise RuntimeError("selected U6-E reference input set is empty")
        if problem_identity_seed_path is not None and planner_continuation_checkpoint is None:
            raise RuntimeError(
                "problem identity seed requires an accepted Planner continuation checkpoint"
            )
        problem_identity_seed = (
            None
            if problem_identity_seed_path is None
            else _load_problem_identity_seed(problem_identity_seed_path)
        )
        if problem_identity_seed is not None:
            if problem_identity_seed.source_commit != source_commit:
                raise RuntimeError("problem identity seed source Commit does not match the run")
            if problem_identity_seed.source_text_root != source_manifest.text_root.artifact_id:
                raise RuntimeError("problem identity seed source TextRoot does not match the run")
            if problem_identity_seed.cutoff_chapter != current_chapter:
                raise RuntimeError("problem identity seed cutoff does not match the run")

        if destination_object_root.exists() and any(destination_object_root.iterdir()):
            raise RuntimeError("U6-E destination object root must be new and empty")
        destination_object_root.mkdir(parents=True, exist_ok=True)
        destination_artifacts = ArtifactRepository(FilesystemObjectStore(destination_object_root))
        copy_canonical_roots(source_artifacts, destination_artifacts, source_manifest)
        for ref in input_assets:
            copied = destination_artifacts.put(
                source_artifacts.read_verified(ref), ref.media_type, ref.schema_version
            )
            if copied != ref:
                raise RuntimeError(f"reference input copy changed identity: {ref.artifact_id.root}")
        destination_manifest: RootManifest = isolated_manifest(
            source_manifest, destination_project_id
        )
        destination_commits = CommitService(build_session_factory(destination_engine))
        basis = destination_commits.initialize_project(destination_manifest)
        if destination_commits.current_commit(destination_project_id) != basis:
            raise RuntimeError("U6-E destination basis did not become the current Commit")
        loaded = destination_commits.load_manifest(basis)
        loaded_text = TextRootDocument.model_validate_json(
            destination_artifacts.read_verified(loaded.text_root), strict=True
        )
        if loaded_text.chapters[-1].chapter_index != current_chapter:
            raise RuntimeError("U6-E destination basis changed the source chapter boundary")

        destination_snapshot = snapshot_id_for_commit(basis)
        continuation_ref: ArtifactRef | None = None
        continuation_seed: dict[str, object] | None = None
        if planner_continuation_checkpoint is not None:
            continuation_ref, continuation_seed = _seed_planner_continuation(
                source_checkpoint_path=planner_continuation_checkpoint,
                source_object_root=source_object_root,
                source_artifacts=source_artifacts,
                destination_artifacts=destination_artifacts,
                input_assets=input_assets,
                source_commit=source_commit,
                destination_project_id=destination_project_id,
                destination_basis=basis,
                destination_snapshot=destination_snapshot,
                run_id=run_id,
                current_chapter=current_chapter,
                target_chapters=target_chapters,
                problem_identity_seed=problem_identity_seed,
                source_text_root=source_manifest.text_root,
            )

        runtime_manifest = load_stage5_manifest(
            ROOT / "src" / "novel_agent" / "runtime" / "stage5_development_manifest.json"
        )
        policy_hash = runtime_manifest.configuration_fingerprint
        request = CreativeRunRequest(
            run_id=run_id,
            project_id=destination_project_id,
            basis_commit=basis,
            basis_snapshot=destination_snapshot,
            policy=CreativeRunPolicy(
                automation_mode=AutomationMode.AUTO,
                policy_hash=policy_hash,
                permission_hash=policy_hash,
                auto_accept_plan=True,
                auto_accept_draft=True,
                max_task_attempts=3,
                max_tasks_per_advance=1,
                planning_horizon=5,
                runtime_parallelism=1,
                enable_planner_lookahead=False,
            ),
            current_chapter=current_chapter,
            target_chapters=target_chapters,
            input_artifact_refs=input_assets,
            continuation_artifact_refs=() if continuation_ref is None else (continuation_ref,),
        )
        receipt = {
            "schema": SCHEMA,
            "run_kind": "canary" if canary else "endurance",
            "preflight": preflight,
            "source_project_id": source_project_id.root,
            "source_commit": source_commit.root,
            "source_current_commit": source_current.root,
            "source_is_current": source_current == source_commit,
            "source_is_frozen_c40_history": allow_historical_c40_source,
            "source_history_last_chapter": current_chapter,
            "isolated_project_id": destination_project_id.root,
            "isolated_basis_commit": basis.root,
            "history_last_chapter": loaded_text.chapters[-1].chapter_index,
            "target_chapters": target_chapters,
            "expected_chapters": list(expected_chapters),
            "author_initial_brief": author_assets[0].artifact_id.root,
            "input_reference_asset_mode": reference_input_mode,
            "input_artifact_refs": [ref.model_dump(mode="json") for ref in input_assets],
            "planner_continuation_seed": continuation_seed,
            "problem_identity_seed": (
                None
                if problem_identity_seed is None
                else problem_identity_seed.model_dump(mode="json")
            ),
            "source_object_store_root": str(source_object_root.resolve()),
            "isolated_object_store_root": str(destination_object_root.resolve()),
            "isolated_database_descriptor": database_descriptor(destination_database_url),
            "request_id": request.run_id.root,
        }
        return receipt, request
    finally:
        source_engine.dispose()
        destination_engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-database-url", required=True)
    parser.add_argument("--source-project-id", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-object-root", type=Path, required=True)
    parser.add_argument("--destination-database-url", required=True)
    parser.add_argument("--destination-project-id", required=True)
    parser.add_argument("--destination-object-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target-chapters", type=int, default=70)
    parser.add_argument("--canary", action="store_true")
    parser.add_argument("--allow-historical-c40-source", action="store_true")
    parser.add_argument("--endpoint-profile", default=QWEN38_27B_FP8_8005_ENDPOINT_PROFILE)
    parser.add_argument("--endpoint-health-url", default="http://127.0.0.1:8005/health")
    parser.add_argument("--endpoint-models-url", default="http://127.0.0.1:8005/v1/models")
    parser.add_argument(
        "--input-reference-assets",
        choices=REFERENCE_INPUT_MODES,
        default="author_initial_brief",
        help="reference assets bound into the Planner task intent",
    )
    parser.add_argument(
        "--planner-continuation-checkpoint",
        type=Path,
        help=(
            "optional source-object-store checkpoint at INQUIRY_ACCEPTED; "
            "only inquiry/review intent is rebased"
        ),
    )
    parser.add_argument(
        "--problem-identity-seed",
        type=Path,
        help=(
            "optional pre-registered source-bound Planner Memory problem identity; "
            "requires --planner-continuation-checkpoint"
        ),
    )
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt, request = prepare(
            source_database_url=args.source_database_url,
            source_project_id=ProjectId(args.source_project_id),
            source_commit=CommitId(args.source_commit),
            source_object_root=args.source_object_root,
            destination_database_url=args.destination_database_url,
            destination_project_id=ProjectId(args.destination_project_id),
            destination_object_root=args.destination_object_root,
            run_id=RunId(args.run_id),
            target_chapters=args.target_chapters,
            canary=args.canary,
            endpoint_profile=args.endpoint_profile,
            endpoint_health_url=args.endpoint_health_url,
            endpoint_models_url=args.endpoint_models_url,
            reference_input_mode=args.input_reference_assets,
            planner_continuation_checkpoint=args.planner_continuation_checkpoint,
            problem_identity_seed_path=args.problem_identity_seed,
            output_paths=(args.receipt, args.request),
            output_root=args.output_root,
            allow_historical_c40_source=args.allow_historical_c40_source,
        )
    except Exception as error:
        failure = {
            "schema": SCHEMA,
            "status": "FAILED",
            "failure_type": type(error).__name__,
            "failure": str(error),
            "code": _code_identity(),
            "destination_project_id": args.destination_project_id,
            "run_id": args.run_id,
            "receipt_path": str(args.receipt.resolve()),
            "request_written": False,
        }
        _write_once(args.receipt, failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 2
    _write_once(args.receipt, receipt)
    _write_once(args.request, request.model_dump(mode="json"))
    print(
        json.dumps(
            {"receipt": receipt, "request": request.model_dump(mode="json")},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
