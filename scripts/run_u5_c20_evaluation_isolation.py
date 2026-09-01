#!/usr/bin/env python3
"""Run the disposable U5-A C20 Writer readout before C21 production work."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from prepare_u5_c20_isolated_basis import copy_canonical_roots
from run_u4s_readout_campaign import _run_task
from sqlalchemy import func, select

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.models import CommitRow, RuntimeTaskProjectionRow
from novel_agent.domain.artifacts import (
    EVALUATION_NAMESPACE_DISCARD_MEDIA_TYPE,
    RootManifest,
)
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
)
from novel_agent.domain.ids import CommitId, ProjectId, RunId, SchemaVersion
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.stage2 import (
    BenchmarkInformationProfile,
    ReferenceRootDocument,
    SourceClass,
)
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.domain.u5_evaluation import (
    U5C20EvaluationIsolationReport,
    U5EvaluationTaskEvidence,
)
from novel_agent.domain.v05_readout import (
    MemoryIdentitySnapshot,
    V05ReadoutCampaignManifest,
    V05ReadoutTaskIdentity,
    V05ReadoutTrack,
)
from novel_agent.runtime.creative_assembly import (
    DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    ProductionAssemblyContext,
    load_production_runtime_assembly,
)
from novel_agent.runtime.production_bootstrap import resolve_registered_model_endpoints
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.evaluation_namespace import discard_evaluation_namespace
from novel_agent.services.projection import snapshot_id_for_commit
from novel_agent.services.u4s_seed_readout import (
    U4SCheckpointInput,
    U4SPublicCorpus,
    _fallback_planner_artifact,
    _replay_backend,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = SchemaVersion("1.0.0")
_FORBIDDEN_C21_KEYS = frozenset(
    {
        "question",
        "question_text",
        "answer",
        "writer_answer",
        "gold",
        "judge",
        "judge_result",
        "target_text",
        "response_ref",
        "readout_ref",
    }
)


def _count_rows(database_url: str, project_id: ProjectId) -> tuple[int, int]:
    engine = build_engine(database_url)
    try:
        session_factory = build_session_factory(engine)
        with session_factory() as session:
            commits = int(
                session.scalar(
                    select(func.count())
                    .select_from(CommitRow)
                    .where(CommitRow.project_id == project_id.root)
                )
                or 0
            )
            tasks = int(
                session.scalar(
                    select(func.count())
                    .select_from(RuntimeTaskProjectionRow)
                    .where(RuntimeTaskProjectionRow.project_id == project_id.root)
                )
                or 0
            )
            return commits, tasks
    finally:
        engine.dispose()


def _load_basis(
    *, database_url: str, project_id: ProjectId, object_root: Path
) -> tuple[
    CommitId,
    RootManifest,
    TextRootDocument,
    PlanRootDocument,
    WorldRootDocument,
    ReferenceRootDocument,
]:
    engine = build_engine(database_url)
    try:
        commits = CommitService(build_session_factory(engine))
        basis = commits.current_commit(project_id)
        manifest = commits.load_manifest(basis)
        artifacts = ArtifactRepository(FilesystemObjectStore(object_root))
        text = TextRootDocument.model_validate_json(artifacts.read_verified(manifest.text_root))
        plan = PlanRootDocument.model_validate_json(artifacts.read_verified(manifest.plan_root))
        world = WorldRootDocument.model_validate_json(artifacts.read_verified(manifest.world_root))
        reference = ReferenceRootDocument.model_validate_json(
            artifacts.read_verified(manifest.reference_root), strict=True
        )
        if not text.chapters or text.chapters[-1].chapter_index != 20:
            raise RuntimeError("U5-A basis must end at C20")
        return basis, manifest, text, plan, world, reference
    finally:
        engine.dispose()


def _memory_identity(
    *,
    basis: CommitId,
    manifest: RootManifest,
    text: TextRootDocument,
    plan: PlanRootDocument,
    world: WorldRootDocument,
) -> MemoryIdentitySnapshot:
    return MemoryIdentitySnapshot(
        commit_id=basis,
        text_root=text.root_hash,
        world_root=world.root_hash,
        plan_root=plan.root_hash,
        profile_root=manifest.project_profile_root.artifact_id,
    )


def _selected_tasks(
    manifest: V05ReadoutCampaignManifest,
) -> tuple[V05ReadoutTaskIdentity, ...]:
    qa = tuple(
        task
        for task in manifest.readout_manifest.tasks
        if task.checkpoint_chapter == 20 and task.track is V05ReadoutTrack.QA
    )
    history = tuple(
        task
        for task in manifest.readout_manifest.tasks
        if task.checkpoint_chapter == 20
        and task.track is V05ReadoutTrack.CONTEXT
        and task.information_profile is BenchmarkInformationProfile.VISIBLE_AT_CUTOFF
    )
    apc = tuple(
        task
        for task in manifest.readout_manifest.tasks
        if task.checkpoint_chapter == 20
        and task.track is V05ReadoutTrack.CONTEXT
        and task.information_profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
    )
    if not qa or not history or not apc:
        raise RuntimeError("frozen U4-S manifest lacks the three C20 U5-A task classes")
    return (qa[0], history[0], apc[0])


def _actual_task_input(
    *,
    identity: V05ReadoutTaskIdentity,
    run_id: RunId,
    corpus: U4SPublicCorpus,
    basis: CommitId,
    text: TextRootDocument,
    plan: PlanRootDocument,
    world: WorldRootDocument,
    memory: MemoryIdentitySnapshot,
) -> U4SCheckpointInput:
    public = corpus.checkpoint_input(identity, run_id=run_id)
    need = public.need.model_copy(update={"base_commit": basis, "run_id": run_id})
    planner_artifact = _fallback_planner_artifact(
        public.task, public.planning_context, world, need, run_id
    )
    backend_bundle = _replay_backend(world, text, plan, basis, identity.checkpoint_id)
    return U4SCheckpointInput(
        identity=identity,
        task=public.task,
        planning_context=public.planning_context,
        plan=plan,
        text=text,
        world=world,
        basis_commit=basis,
        snapshot_id=backend_bundle.attestation.snapshot_id,
        question_text=public.question_text,
        need=need,
        planner_artifact=planner_artifact,
        backend_bundle=backend_bundle,
        memory_identity=memory,
    )


def _has_forbidden_keys(value: object) -> bool:
    if isinstance(value, dict):
        if any(str(key).lower() in _FORBIDDEN_C21_KEYS for key in value):
            return True
        return any(_has_forbidden_keys(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(_has_forbidden_keys(item) for item in value)
    return False


def _write_once(path: Path, data: bytes) -> None:
    if path.exists():
        raise RuntimeError(f"U5-A refuses to overwrite artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def _run_readout(
    *,
    campaign: V05ReadoutCampaignManifest,
    tasks: tuple[U4SCheckpointInput, ...],
    project_id: ProjectId,
    run_id: RunId,
    artifacts: ArtifactRepository,
    gateway: Any,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    reports: list[Any] = []
    discarded_refs: list[Any] = []
    for task_input in tasks:
        report = await _run_task(
            manifest=campaign,
            task_input=task_input,
            case_id=project_id,
            run_id=run_id,
            artifacts=artifacts,
            gateway=gateway,
            discarded_refs=discarded_refs,
        )
        reports.append(report)
        marker = artifacts.put(
            canonical_json_bytes(
                {"run_id": run_id.root, "task_id": task_input.identity.task_id.root}
            ),
            "application/vnd.novel-agent.evaluation.u5-task-marker+json",
            SCHEMA_VERSION,
        )
        discarded_refs.append(marker)
    return tuple(reports), tuple(discarded_refs)


def _task_evidence(report: Any) -> U5EvaluationTaskEvidence:
    return U5EvaluationTaskEvidence(
        task_id=report.task_id,
        track=report.track.value,
        information_profile=report.information_profile.value,
        evaluation_task_identity=report.task_id,
        basis_commit_id=report.basis_commit_id,
        freeze_receipt_id=report.freeze_receipt_id,
        writer_status=report.writer_status,
        response_ref=report.response_ref,
        readout_record_ref=report.readout_record_ref,
        raw_response_ref=report.raw_response_ref,
    )


def _build_request(
    *,
    project_id: ProjectId,
    run_id: RunId,
    basis: CommitId,
    policy_hash: str,
    input_artifact_refs: tuple[Any, ...],
) -> CreativeRunRequest:
    policy = CreativeRunPolicy(
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
    )
    return CreativeRunRequest(
        run_id=run_id,
        project_id=project_id,
        basis_commit=basis,
        basis_snapshot=snapshot_id_for_commit(basis),
        policy=policy,
        current_chapter=20,
        target_chapters=25,
        input_artifact_refs=input_artifact_refs,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--basis-commit", required=True)
    parser.add_argument("--basis-object-root", type=Path, required=True)
    parser.add_argument("--evaluation-object-root", type=Path, required=True)
    parser.add_argument("--campaign-manifest", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--endpoint-profile", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.output.exists() or args.evaluation_object_root.exists():
        raise RuntimeError("U5-A refuses to overwrite an evaluation identity")
    project_id = ProjectId(args.project_id)
    run_id = RunId(args.run_id)
    campaign = V05ReadoutCampaignManifest.model_validate_json(
        args.campaign_manifest.read_bytes(), strict=True
    )
    basis, manifest, text, plan, world, reference = _load_basis(
        database_url=args.database_url,
        project_id=project_id,
        object_root=args.basis_object_root,
    )
    if basis != CommitId(args.basis_commit):
        raise RuntimeError("U5-A requested basis does not match isolated project current Commit")
    before_memory = _memory_identity(
        basis=basis, manifest=manifest, text=text, plan=plan, world=world
    )
    before_counts = _count_rows(args.database_url, project_id)
    args.evaluation_object_root.mkdir(parents=True, exist_ok=True)
    evaluation_artifacts = ArtifactRepository(FilesystemObjectStore(args.evaluation_object_root))
    basis_artifacts = ArtifactRepository(FilesystemObjectStore(args.basis_object_root))
    copy_canonical_roots(basis_artifacts, evaluation_artifacts, manifest)

    runtime_manifest = load_stage5_manifest(
        ROOT / "src" / "novel_agent" / "runtime" / "stage5_development_manifest.json"
    )
    policy = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=runtime_manifest.configuration_fingerprint,
        permission_hash=runtime_manifest.configuration_fingerprint,
        runtime_parallelism=1,
    )
    assembly = load_production_runtime_assembly(
        DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
        ProductionAssemblyContext(
            database_url=args.database_url,
            object_store_root=args.evaluation_object_root,
            project_id=project_id,
            run_id=run_id,
            policy=policy,
            manifest=runtime_manifest,
            model_endpoints=resolve_registered_model_endpoints(args.endpoint_profile),
            schema_version=SCHEMA_VERSION,
        ),
    )
    if assembly.artifacts is None or assembly.model_gateway is None:
        raise RuntimeError("U5-A assembly did not reuse the evaluation object namespace")
    evaluation_artifacts = assembly.artifacts

    corpus = U4SPublicCorpus(args.bundle_root.resolve())
    inputs = tuple(
        _actual_task_input(
            identity=identity,
            run_id=run_id,
            corpus=corpus,
            basis=basis,
            text=text,
            plan=plan,
            world=world,
            memory=before_memory,
        )
        for identity in _selected_tasks(campaign)
    )
    task_reports, discarded_refs = asyncio.run(
        _run_readout(
            campaign=campaign,
            tasks=inputs,
            project_id=project_id,
            run_id=run_id,
            artifacts=evaluation_artifacts,
            gateway=assembly.model_gateway,
        )
    )
    (
        after_basis,
        after_manifest,
        after_text,
        after_plan,
        after_world,
        _after_reference,
    ) = _load_basis(
        database_url=args.database_url,
        project_id=project_id,
        object_root=args.basis_object_root,
    )
    after_memory = _memory_identity(
        basis=after_basis,
        manifest=after_manifest,
        text=after_text,
        plan=after_plan,
        world=after_world,
    )
    after_counts = _count_rows(args.database_url, project_id)
    discard = discard_evaluation_namespace(
        evaluation_artifacts,
        run_id=run_id,
        discarded_refs=tuple(discarded_refs),
        memory_before=before_memory,
        memory_after=after_memory,
    )
    discard_ref = evaluation_artifacts.put(
        canonical_json_bytes(discard.model_dump(mode="json")),
        EVALUATION_NAMESPACE_DISCARD_MEDIA_TYPE,
        SCHEMA_VERSION,
    )

    author_intent_refs = tuple(
        asset.artifact
        for asset in reference.assets
        if asset.source_class is SourceClass.AUTHOR_INITIAL_BRIEF
    )
    if not author_intent_refs:
        raise RuntimeError("C21 request requires a frozen AUTHOR_INITIAL_BRIEF artifact")
    request = _build_request(
        project_id=project_id,
        run_id=RunId(f"{run_id.root}.production"[:128]),
        basis=basis,
        policy_hash=runtime_manifest.configuration_fingerprint,
        input_artifact_refs=(author_intent_refs[0],),
    )
    request_payload = request.model_dump(mode="json")
    if _has_forbidden_keys(request_payload):
        raise RuntimeError("C21 production request contains benchmark-private fields")
    request_path = args.output.with_name("c21-production-request.json")
    _write_once(
        request_path,
        (json.dumps(request_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
    )
    status = (
        "COMPLETED"
        if all(item.writer_status == "SCHEMA_VALID" for item in task_reports)
        else "REVIEW_REQUIRED"
    )
    report = U5C20EvaluationIsolationReport(
        run_id=run_id,
        project_id=project_id,
        basis_commit=basis,
        memory_identity_before=before_memory,
        memory_identity_after=after_memory,
        tasks=tuple(_task_evidence(item) for item in task_reports),
        discard_receipt_ref=discard_ref,
        canonical_commit_count_before=before_counts[0],
        canonical_commit_count_after=after_counts[0],
        runtime_task_count_before=before_counts[1],
        runtime_task_count_after=after_counts[1],
        model_call_count=len(assembly.model_gateway.call_records),
        evaluation_artifact_count=len(discarded_refs),
        c21_request_path=str(request_path.resolve()),
        status=status,
    )
    _write_once(
        args.output,
        (report.model_dump_json(by_alias=True, indent=2) + "\n").encode(),
    )
    print(report.model_dump_json(by_alias=True, indent=2))
    return 0 if status == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
