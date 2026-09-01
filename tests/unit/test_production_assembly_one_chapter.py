"""One-chapter AUTO path through the unique production factory and fake endpoint."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.model.production_fake import ProductionChapterEndpoint
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.adapters.runtime.stage3_writer import ProductionWritingRequestFactory
from novel_agent.domain.artifacts import ArtifactRef
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
from novel_agent.domain.runtime import TaskKind, TaskStatus
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    AuthorApprovalDecision,
    AuthorApprovalStatus,
    BootstrapStrategy,
    ContractRef,
    ExecutionStatus,
    PlanProposal,
    ProposalProvenance,
    ProposedItem,
    ReferenceAsset,
    ReferenceRootDocument,
    SourceClass,
    WorldPatchCandidate,
)
from novel_agent.domain.stage5_evaluation import VerticalRunStatus
from novel_agent.runtime.creative_assembly import (
    DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    build_production_assembly,
)
from novel_agent.runtime.production_bootstrap import resolve_registered_model_endpoints
from novel_agent.runtime.vertical_runner import VerticalCreativeRunner
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.bootstrap import BootstrapIngestionService, RawBootstrapSource
from novel_agent.services.bootstrap_workflow import (
    BootstrapCrossRootValidator,
    BootstrapRootBuilder,
    GenesisCoordinator,
    SqlAuthorApprovalRepository,
)
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, plan_root_content_id
from novel_agent.services.projection import (
    DerivedProjectionService,
    ProjectionOutboxRepository,
    snapshot_id_for_commit,
)
from novel_agent.services.replay import ExactReplayProjectionBuilder
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.unit.test_production_assembly import HASH, PERMISSION, _context, _stamp_sqlite
from tests.unit.test_stage5_production_factories import _profile

VERSION = SchemaVersion("1.0.0")


def test_production_fake_work_plan_uses_opaque_lineage_binding() -> None:
    def ref(fill: str) -> dict[str, object]:
        return {
            "artifact_id": "sha256:" + fill * 64,
            "media_type": "application/json",
            "byte_length": 1,
            "schema_version": "1.0.0",
        }

    outer = {
        "writing_task_ref": ref("1"),
        "accepted_plan_ref": ref("1"),
        "writer_context_ref": ref("1"),
    }
    opaque = {
        "writing_task_ref": ref("2"),
        "accepted_plan_ref": ref("3"),
        "writer_context_ref": ref("4"),
    }
    prompt = (
        "<TRUSTED_INPUT>\n"
        + json.dumps(outer, separators=(",", ":"))
        + "\n<OPAQUE_LINEAGE_BINDING>\n"
        + json.dumps(opaque, separators=(",", ":"))
        + "\n</OPAQUE_LINEAGE_BINDING>\n</TRUSTED_INPUT>"
    )

    work_plan = ProductionChapterEndpoint._work_plan(prompt)

    assert work_plan.writing_task_ref.artifact_id == ArtifactId("sha256:" + "2" * 64)
    assert work_plan.accepted_plan_ref.artifact_id == ArtifactId("sha256:" + "3" * 64)
    assert work_plan.writer_context_ref.artifact_id == ArtifactId("sha256:" + "4" * 64)


def _bootstrap_canon(
    object_root: Path, session_factory: object
) -> tuple[CommitId, ArtifactRef, ArtifactRef]:
    """Create the fake workload through the public bootstrap/Genesis owners."""

    artifacts = ArtifactRepository(FilesystemObjectStore(object_root))
    project_id = ProjectId("project.test")
    bundle = make_synthetic_bundle()
    text = next(item for item in bundle.text_roots if len(item.chapters) == 20)
    plan = next(item for item in bundle.plan_roots if item.chapter_goals)
    plan = plan.model_copy(
        update={
            "root_hash": ArtifactId("sha256:" + "0" * 64),
            "chapter_goals": tuple(goal for goal in plan.chapter_goals if goal.chapter_index != 21),
        }
    )
    plan = plan.model_copy(update={"root_hash": plan_root_content_id(plan)})
    world = bundle.world_roots[0]
    raw_sources = (
        RawBootstrapSource(
            source_id=StableId("source.production.brief"),
            source_class=SourceClass.AUTHOR_INITIAL_BRIEF,
            media_type="text/plain",
            data=b"Enter the tower while preserving the injured arm.",
        ),
        RawBootstrapSource(
            source_id=StableId("source.production.setting"),
            source_class=SourceClass.BASELINE_SETTING,
            media_type="text/plain",
            data=b"[WORLD_FACT_AT_STORY_OPEN] Lin's left arm is injured.",
        ),
        RawBootstrapSource(
            source_id=StableId("source.production.chapter.20"),
            source_class=SourceClass.CHAPTER_TEXT,
            media_type="text/plain",
            data=b"Reference excerpt: Lin's injured arm remains part of the continuity.",
            chapter_index=20,
        ),
    )
    bootstrap, ingested = BootstrapIngestionService(artifacts).ingest(
        project_id,
        StableId("bootstrap.production.fake"),
        raw_sources,
        VERSION,
    )
    brief = next(
        item.source.artifact_ref
        for item in ingested
        if item.source.source_class is SourceClass.AUTHOR_INITIAL_BRIEF
    )
    reference = ReferenceRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        assets=tuple(
            ReferenceAsset(
                asset_id=StableId(f"reference.{item.source.source_id.root}"),
                source_id=item.source.source_id,
                source_class=item.source.source_class,
                artifact=item.source.artifact_ref,
            )
            for item in ingested
            if item.reference_candidate is not None
        ),
    )
    plan_proposal = PlanProposal(
        proposal_id=StableId("proposal.production.plan"),
        project_id=project_id,
        mode=AgentMode.PROJECT_BOOTSTRAP,
        strategy=BootstrapStrategy.NORMALIZE_ONLY,
        items=(
            ProposedItem(
                item_id=StableId("proposal.production.brief"),
                kind="author_brief",
                payload={"summary": "Enter the tower while preserving the injured arm."},
                provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                source_ids=(StableId("source.production.brief"),),
            ),
        ),
        coverage=1.0,
        receipt=_bootstrap_receipt(AgentType.PLANNER, AgentMode.PROJECT_BOOTSTRAP),
    )
    world_patch = WorldPatchCandidate(
        proposal_id=StableId("proposal.production.world"),
        project_id=project_id,
        items=(
            ProposedItem(
                item_id=StableId("proposal.production.setting"),
                kind="baseline_state",
                payload={"fact": "Lin's left arm is injured."},
                provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                source_ids=(StableId("source.production.setting"),),
            ),
        ),
        origin_source_ids=(StableId("source.production.setting"),),
        extraction_coverage=1.0,
        receipt=_bootstrap_receipt(AgentType.MEMORY_CURATOR, AgentMode.BOOTSTRAP),
    )
    candidates = BootstrapRootBuilder(artifacts).build(
        project_id,
        bootstrap.bundle_id,
        text,
        plan,
        world,
        reference,
        _profile(),
        plan_proposal,
        world_patch,
        tuple(item.classification for item in ingested),
    )
    validation = BootstrapCrossRootValidator().validate(candidates)
    assert validation.status.value == "passed"
    approvals = SqlAuthorApprovalRepository(session_factory)  # type: ignore[arg-type]
    coordinator = GenesisCoordinator(CommitService(session_factory), approvals)  # type: ignore[arg-type]
    approval = coordinator.create_approval_request(candidates, validation)
    approvals.decide(
        AuthorApprovalDecision(
            decision_id=StableId("approval.production.fake"),
            approval_request_id=approval.approval_request_id,
            project_id=approval.project_id,
            candidate_manifest_hash=approval.candidate_manifest_hash,
            validation_report_id=approval.validation_report_id,
            status=AuthorApprovalStatus.APPROVED,
            author_id=StableId("author.production.fake"),
            reason="deterministic isolated production-path bootstrap",
            decided_at=datetime.now(UTC),
        )
    )
    genesis = coordinator.commit(candidates, validation, approval.approval_request_id)
    bootstrap_receipt = artifacts.put(
        canonical_json_bytes(
            {
                "bundle": bootstrap.model_dump(mode="json"),
                "validation": validation.model_dump(mode="json"),
                "approval": approval.model_dump(mode="json"),
                "genesis": genesis.model_dump(mode="json"),
            }
        ),
        "application/vnd.novel-agent.bootstrap-receipt+json",
        VERSION,
    )
    DerivedProjectionService(
        ProjectionOutboxRepository(session_factory),  # type: ignore[arg-type]
        ExactReplayProjectionBuilder(),
    ).process_all()
    return genesis.commit_id, bootstrap_receipt, brief


def _bootstrap_receipt(agent_type: AgentType, mode: AgentMode) -> AgentExecutionReceipt:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    return AgentExecutionReceipt(
        receipt_id=StableId(f"receipt.production.{agent_type.value}.{mode.value}"),
        run_id=RunId("run.production.bootstrap"),
        task_id=TaskId("task.production.bootstrap"),
        agent_spec=ContractRef(
            contract_id=StableId(f"agent.production.{agent_type.value}"),
            version=VERSION,
            content_hash=HASH,
        ),
        agent_type=agent_type,
        agent_mode=mode,
        prompt_fingerprint=HASH,
        configuration_fingerprint=HASH,
        status=ExecutionStatus.SUCCEEDED,
        started_at=now,
        completed_at=now,
        latency_ms=0,
    )


def test_production_factory_runs_one_chapter_with_fake_endpoint(tmp_path: Path) -> None:
    url = _stamp_sqlite(tmp_path / "chapter.db")
    object_root = tmp_path / "objects"
    seed_factory = build_session_factory(build_engine(url))
    base, bootstrap_receipt, author = _bootstrap_canon(object_root, seed_factory)
    snapshot = snapshot_id_for_commit(base)
    endpoints = resolve_registered_model_endpoints("deterministic_fake")
    endpoint = endpoints[0].adapter
    assert isinstance(endpoint, ProductionChapterEndpoint)
    context = _context(
        tmp_path,
        url=url,
        policy=CreativeRunPolicy(
            automation_mode=AutomationMode.AUTO,
            policy_hash=HASH.root,
            permission_hash=PERMISSION,
            auto_accept_plan=True,
            auto_accept_draft=True,
        ),
        model_endpoints=endpoints,
    )
    assembly = build_production_assembly(context)
    assert assembly.attestation is not None
    assert assembly.attestation.factory_locator == DEFAULT_PRODUCTION_ASSEMBLY_FACTORY
    assert isinstance(assembly.writing_request_factory, ProductionWritingRequestFactory)
    assert assembly.planner.is_fixture is False
    assert assembly.writer.is_fixture is False
    assert assembly.session_factory is not None
    request = CreativeRunRequest(
        run_id=context.run_id,
        project_id=context.project_id,
        basis_commit=base,
        basis_snapshot=snapshot,
        policy=context.policy,
        current_chapter=20,
        target_chapters=21,
        input_artifact_refs=(bootstrap_receipt, author),
    )
    report = asyncio.run(
        VerticalCreativeRunner(
            runtime=assembly.runtime,
            dispatcher=assembly.dispatcher,
            tasks=assembly.task_reader,
        ).run(request, max_tasks=1, max_slices=24)
    )
    tasks = assembly.task_reader.list_run(request.run_id)
    kinds = tuple(task.kind for task in tasks)
    assert TaskKind.DRAFT_CANDIDATE in kinds
    assert any(task.kind is TaskKind.PROJECTION_FRESHNESS for task in tasks)
    assert any(task.kind is TaskKind.DRAFT_COMMIT for task in tasks)
    assert report.status is VerticalRunStatus.COMPLETED
    assert report.completed_chapters == (21,)
    assert isinstance(assembly.task_reader, RuntimeTaskQueryRepository)
    commits = CommitService(assembly.session_factory)
    artifacts = ArtifactRepository(FilesystemObjectStore(context.object_store_root))
    manifest = commits.load_manifest(report.final_commit)
    text = TextRootDocument.model_validate_json(artifacts.read_verified(manifest.text_root))
    assert text.chapters[-1].chapter_index == 21
    assert "injured arm" in text.chapters[-1].scenes[0].blocks[0].text
    assert all(task.status is not TaskStatus.FAILED for task in tasks)
    assert endpoint.requests
    assert assembly.model_gateway is not None
    assert assembly.model_gateway.call_records


def test_production_cli_and_runner_execute_one_chapter_with_receipts(tmp_path: Path) -> None:
    repo_root = Path(__file__).parents[2]
    url = _stamp_sqlite(tmp_path / "cli.db")
    object_root = tmp_path / "cli-objects"
    seed_factory = build_session_factory(build_engine(url))
    base, bootstrap_receipt, author = _bootstrap_canon(object_root, seed_factory)
    endpoints = resolve_registered_model_endpoints("deterministic_fake")
    context = _context(
        tmp_path,
        url=url,
        policy=CreativeRunPolicy(
            automation_mode=AutomationMode.AUTO,
            policy_hash=HASH.root,
            permission_hash=PERMISSION,
            auto_accept_plan=True,
            auto_accept_draft=True,
        ),
        model_endpoints=endpoints,
    )
    request = CreativeRunRequest(
        run_id=context.run_id,
        project_id=context.project_id,
        basis_commit=base,
        basis_snapshot=snapshot_id_for_commit(base),
        policy=context.policy,
        current_chapter=20,
        target_chapters=21,
        input_artifact_refs=(bootstrap_receipt, author),
    )
    request_path = tmp_path / "run-request.json"
    policy_path = tmp_path / "run-policy.json"
    manifest_path = (
        repo_root / "src" / "novel_agent" / "runtime" / "stage5_development_manifest.json"
    )
    request_path.write_text(request.model_dump_json(), encoding="utf-8")
    policy_path.write_text(context.policy.model_dump_json(), encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(repo_root / "src"), environment.get("PYTHONPATH")) if item
    )

    start = subprocess.run(
        [
            sys.executable,
            "-m",
            "novel_agent.cli",
            "runtime",
            "--database-url",
            url,
            "start",
            "--request",
            str(request_path),
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert start.returncode == 0, start.stdout + start.stderr

    advance = subprocess.run(
        [
            sys.executable,
            "-m",
            "novel_agent.cli",
            "runtime",
            "--database-url",
            url,
            "advance",
            "--project-id",
            context.project_id.root,
            "--run-id",
            context.run_id.root,
            "--policy",
            str(policy_path),
            "--manifest",
            str(manifest_path),
            "--object-store-root",
            str(object_root),
            "--endpoint-profile",
            "deterministic_fake",
            "--max-tasks",
            "1",
            "--receipt",
            str(tmp_path / "stage5-result.cli-receipt.json"),
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert advance.returncode == 0, advance.stdout + advance.stderr
    assert json.loads(advance.stdout)["progressed"] == 1

    output = tmp_path / "stage5-result.json"
    cli_receipt = tmp_path / "stage5-result.cli-receipt.json"
    runner = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_stage5_runtime_evaluation.py"),
            "--request",
            str(request_path),
            "--manifest",
            str(manifest_path),
            "--database-url",
            url,
            "--object-store-root",
            str(object_root),
            "--endpoint-profile",
            "deterministic_fake",
            "--settlement-output-tokens",
            "12000",
            "--max-tasks",
            "1",
            "--max-slices",
            "24",
            "--output",
            str(output),
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert runner.returncode == 0, runner.stdout + runner.stderr
    expected_outputs = {
        "stage5-result.assembly-spec.json",
        "stage5-result.resolved-attestation.json",
        "stage5-result.endpoint-revision.json",
        "stage5-result.run-request.json",
        "stage5-result.invocation.json",
        "stage5-result.cli-receipt.json",
        "stage5-result.json",
    }
    assert {path.name for path in tmp_path.iterdir() if path.name.startswith("stage5-result")} == (
        expected_outputs
    )
    result = json.loads(output.read_text(encoding="utf-8"))
    attestation = json.loads(
        (tmp_path / "stage5-result.resolved-attestation.json").read_text(encoding="utf-8")
    )
    invocation = json.loads(
        (tmp_path / "stage5-result.invocation.json").read_text(encoding="utf-8")
    )
    cli_receipt_payload = json.loads(cli_receipt.read_text(encoding="utf-8"))
    assert result["status"] == "completed"
    assert result["completed_chapters"] == [21]
    assert attestation["factory_locator"] == DEFAULT_PRODUCTION_ASSEMBLY_FACTORY
    assert attestation["endpoints"][0]["endpoint_name"] == "deterministic-fake-production"
    assert invocation["spec_locator"] == DEFAULT_PRODUCTION_ASSEMBLY_FACTORY
    assert invocation["endpoint_profile"] == "deterministic_fake"
    assert invocation["settlement_output_tokens"] == 12000
    assert invocation["session_factory_identity"]
    assert cli_receipt_payload["receipt_type"] == "runtime_cli_advance"
    assert cli_receipt_payload["spec_locator"] == DEFAULT_PRODUCTION_ASSEMBLY_FACTORY
    assert cli_receipt_payload["endpoints"][0]["revision"] == "production-fake-v1"

    replay = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_stage5_runtime_evaluation.py"),
            "--request",
            str(request_path),
            "--manifest",
            str(manifest_path),
            "--database-url",
            url,
            "--object-store-root",
            str(object_root),
            "--endpoint-profile",
            "deterministic_fake",
            "--max-tasks",
            "1",
            "--max-slices",
            "24",
            "--output",
            str(output),
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert replay.returncode != 0
    assert "already exist" in replay.stderr
