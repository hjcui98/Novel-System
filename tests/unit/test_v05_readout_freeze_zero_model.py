from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from novel_agent.domain.artifacts import (
    EFFECTIVE_BUDGET_MEDIA_TYPE,
    PRODUCTION_ASSEMBLY_ATTESTATION_MEDIA_TYPE,
    V05_READOUT_CAMPAIGN_MANIFEST_MEDIA_TYPE,
    ArtifactRef,
)
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
)
from novel_agent.domain.v05_readout import (
    V05CampaignExecutionFreeze,
    V05CampaignPhase,
    V05CampaignReportFreeze,
    V05CampaignSourceIdentity,
    V05CampaignStatus,
    V05JudgeRuntimeFreeze,
    V05ReadinessStatus,
    V05ReadoutFreezeReceipt,
    V05ReadoutManifest,
    V05RepresentativeTaskCoverage,
)
from novel_agent.runtime.creative_assembly import build_production_assembly
from novel_agent.services.v05_readout_manifest import (
    derive_v05_writer_runtime_from_attestation,
    freeze_v05_readout_campaign,
    load_v05_readout_campaign_manifest,
)
from tests.unit.test_production_assembly import _context
from tests.unit.test_v05_readout_manifest import (
    CONTEXT_WINDOWS,
    _checkpoints,
    _compile,
    _questions,
)

VERSION = SchemaVersion("1.0.0")


def _ref(label: str, media_type: str, payload: bytes | None = None) -> ArtifactRef:
    content = payload if payload is not None else label.encode("utf-8")
    return ArtifactRef(
        artifact_id=ArtifactId(f"sha256:{hashlib.sha256(content).hexdigest()}"),
        media_type=media_type,
        byte_length=len(content),
        schema_version=VERSION,
    )


def _coverage(manifest: V05ReadoutManifest) -> V05RepresentativeTaskCoverage:
    tasks = manifest.tasks
    qa = tuple(task for task in tasks if task.track.value == "novelmem_qa")
    context = tuple(task for task in tasks if task.track.value == "novelmem_context")
    return V05RepresentativeTaskCoverage(
        early_checkpoint_task_id=next(task.task_id for task in qa if task.checkpoint_chapter == 20),
        mid_checkpoint_task_id=next(task.task_id for task in qa if task.checkpoint_chapter == 120),
        late_checkpoint_task_id=next(task.task_id for task in qa if task.checkpoint_chapter == 280),
        legacy_five_chapter_window_task_id=next(
            task.task_id
            for task in context
            if task.target_chapter_end is not None
            and task.target_chapter_start is not None
            and task.target_chapter_end - task.target_chapter_start + 1 == 5
        ),
        twenty_chapter_window_task_id=next(
            task.task_id
            for task in context
            if task.target_chapter_end is not None
            and task.target_chapter_start is not None
            and task.target_chapter_end - task.target_chapter_start + 1 == 20
        ),
        unanswerable_qa_task_id=qa[-2].task_id,
        multi_hop_qa_task_id=qa[-1].task_id,
    )


def test_seed_freeze_binds_real_assembly_budget_and_sql_ledger_without_model_call(
    tmp_path: Path,
) -> None:
    assembly = build_production_assembly(_context(tmp_path))
    assert assembly.attestation is not None
    assert assembly.model_gateway is not None

    run_id = RunId("run.production-factory")
    request = ModelRequest(
        request_id=StableId("request.v05.freeze-budget"),
        run_id=run_id,
        task_id=TaskId("task.v05.freeze-budget"),
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.EVALUATION,
        trace_id="trace.v05.freeze-budget",
        prompt="Freeze the registered Writer budget.",
        max_output_tokens=8_000,
        enable_thinking=False,
    )
    effective_budget = assembly.model_gateway.resolve_effective_budget(request)
    assert assembly.model_gateway.call_records == []
    assert assembly.model_gateway.call_ledger.list_for_run(run_id) == ()

    readout_manifest = _compile()
    coverage = _coverage(readout_manifest)
    attestation_payload = assembly.attestation.model_dump_json().encode("utf-8")
    budget_payload = effective_budget.model_dump_json().encode("utf-8")
    source = V05CampaignSourceIdentity(
        bundle_id=readout_manifest.benchmark_id,
        bundle_version=readout_manifest.version,
        build_report_ref=_ref("build-report", "application/json"),
        assembly_attestation_ref=_ref(
            "assembly-attestation", PRODUCTION_ASSEMBLY_ATTESTATION_MEDIA_TYPE, attestation_payload
        ),
        effective_budget_ref=_ref("effective-budget", EFFECTIVE_BUDGET_MEDIA_TYPE, budget_payload),
        source_identity=ArtifactId("sha256:" + "a" * 64),
        r_bundle=V05ReadinessStatus.PASS,
        r_annotation=V05ReadinessStatus.PENDING,
        r_judge=V05ReadinessStatus.PENDING,
        r_runner=V05ReadinessStatus.PASS,
    )
    writer = derive_v05_writer_runtime_from_attestation(
        attestation=assembly.attestation,
        effective_budget=effective_budget,
        prompt_ref=_ref("writer-prompt", "text/plain"),
        response_schema_ref=_ref("writer-schema", "application/json"),
        request_role="writer_readout",
        temperature=0.0,
        seed=17,
        seed_capability="fixed",
        evidence_token_budget=512,
        concurrency=1,
    )
    manifest = freeze_v05_readout_campaign(
        readout_manifest=readout_manifest,
        campaign_id=StableId("campaign.v05.seed.zero-model"),
        phase=V05CampaignPhase.SEED,
        status=V05CampaignStatus.SEED_DIAGNOSTIC_NOT_ACCEPTANCE,
        source=source,
        writer=writer,
        judges=V05JudgeRuntimeFreeze(
            request_role="writer_judge",
            deterministic_scorer_version="deterministic-v1",
            answer_judge_version="pending",
            evidence_support_judge_version="pending",
            unavailable_policy="pending",
        ),
        effective_budget=effective_budget,
        provider_reasoning_included_in_completion_tokens=(
            assembly.attestation.reasoning_included_in_completion_tokens
        ),
        controller_level="C1+C2",
        planner_level="P0+P1",
        thinking_enabled=False,
        execution=V05CampaignExecutionFreeze(
            repetitions=5,
            max_model_calls=100,
            max_wall_time_seconds=3_600,
            output_namespace="evaluation/v05/seed",
            object_namespace="evaluation/v05/seed",
            database_namespace="evaluation/v05/seed",
            stop_conditions=("stop on unavailable judge",),
        ),
        report=V05CampaignReportFreeze(
            dimensions=("writer_readout",),
            threshold_policy="diagnostic_only",
        ),
        representative_task_ids=coverage.task_ids(),
        representative_task_coverage=coverage,
    )

    manifest_payload = manifest.model_dump_json().encode("utf-8")
    receipt = V05ReadoutFreezeReceipt(
        receipt_id=StableId("freeze.campaign.v05.seed.zero-model"),
        campaign_id=manifest.campaign_id,
        ledger_identity=(
            f"{type(assembly.model_gateway.call_ledger).__module__}."
            f"{type(assembly.model_gateway.call_ledger).__qualname__}:run.production-factory"
        ),
        ledger_before_request_count=0,
        ledger_after_request_count=len(assembly.model_gateway.call_ledger.list_for_run(run_id)),
        manifest_ref=_ref(
            "campaign-manifest", V05_READOUT_CAMPAIGN_MANIFEST_MEDIA_TYPE, manifest_payload
        ),
        attestation_ref=source.assembly_attestation_ref,
        effective_budget_ref=source.effective_budget_ref,
    )
    assert manifest.phase is V05CampaignPhase.SEED
    assert manifest.status is V05CampaignStatus.SEED_DIAGNOSTIC_NOT_ACCEPTANCE
    assert manifest.runtime.effective_budget == effective_budget
    assert manifest.canary_lock is not None
    assert manifest.report.canary_lock == manifest.canary_lock
    assert manifest.canary_lock.controller_context_level == manifest.runtime.controller_level
    assert manifest.canary_lock.planner_context_level == manifest.runtime.planner_level
    assert manifest.canary_lock.thinking_enabled == manifest.runtime.thinking_enabled
    assert receipt.zero_model_call is True
    assert receipt.ledger_before_request_count == receipt.ledger_after_request_count == 0

    with pytest.raises(ValueError, match="empty ledger"):
        V05ReadoutFreezeReceipt.model_validate(
            receipt.model_dump(mode="python")
            | {"ledger_before_request_count": 1, "ledger_after_request_count": 1}
        )


def test_v05_freeze_receipt_schema_is_exported() -> None:
    schema_path = (
        Path(__file__).parents[2] / "schemas" / "stage2" / "V05ReadoutFreezeReceipt.schema.json"
    )
    assert json.loads(schema_path.read_text()) == V05ReadoutFreezeReceipt.model_json_schema()


def test_freeze_cli_writes_seed_manifest_and_receipt_once_from_attestation_and_budget(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    assembly = build_production_assembly(context)
    assert assembly.attestation is not None
    assert assembly.model_gateway is not None
    budget_request = ModelRequest(
        request_id=StableId("request.v05.freeze-cli-budget"),
        run_id=RunId("run.production-factory"),
        task_id=TaskId("task.v05.freeze-cli-budget"),
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.EVALUATION,
        trace_id="trace.v05.freeze-cli-budget",
        prompt="Resolve the frozen Writer budget.",
        max_output_tokens=8_000,
        enable_thinking=False,
    )
    effective_budget = assembly.model_gateway.resolve_effective_budget(budget_request)
    assert assembly.model_gateway.call_records == []
    assert assembly.model_gateway.call_ledger.list_for_run(RunId("run.production-factory")) == ()

    bundle_root = tmp_path / "bundle"
    (bundle_root / "public").mkdir(parents=True)
    (bundle_root / "annotations").mkdir()
    (bundle_root / "reports").mkdir()
    (bundle_root / "private" / "manifests").mkdir(parents=True)
    questions = [
        {
            **question,
            "answerability": "unanswerable" if index == 4 else "answerable",
            "ability": "multi_hop" if index == 20 else "single_hop",
        }
        for index, question in enumerate(_questions())
    ]
    (bundle_root / "benchmark.json").write_text(
        json.dumps(
            {
                "benchmark_id": "novelmem-eval-ztj",
                "version": "0.5-seed.2",
                "context_target_windows": CONTEXT_WINDOWS,
            }
        ),
        encoding="utf-8",
    )
    (bundle_root / "public" / "checkpoints.json").write_text(
        json.dumps({"checkpoints": _checkpoints()}), encoding="utf-8"
    )
    (bundle_root / "annotations" / "track_a_seed.json").write_text(
        json.dumps({"questions": questions}), encoding="utf-8"
    )
    (bundle_root / "reports" / "build_report.json").write_text(
        '{"build": "synthetic-public-identity-test"}\n', encoding="utf-8"
    )
    prompt_path = tmp_path / "writer-prompt.txt"
    schema_path = tmp_path / "writer-schema.json"
    attestation_path = tmp_path / "resolved-attestation.json"
    budget_path = tmp_path / "effective-budget.json"
    prompt_path.write_text("writer readout prompt\n", encoding="utf-8")
    schema_path.write_text('{"type": "object"}\n', encoding="utf-8")
    attestation_path.write_text(assembly.attestation.model_dump_json() + "\n", encoding="utf-8")
    budget_path.write_text(effective_budget.model_dump_json() + "\n", encoding="utf-8")
    output_path = tmp_path / "outputs" / "seed-manifest.json"
    receipt_path = tmp_path / "outputs" / "seed-freeze-receipt.json"
    script_path = Path(__file__).parents[2] / "scripts" / "freeze_v05_readout_campaign.py"
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[2] / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    arguments = [
        sys.executable,
        str(script_path),
        "--bundle-root",
        str(bundle_root),
        "--output",
        str(output_path),
        "--freeze-receipt-output",
        str(receipt_path),
        "--campaign-id",
        "campaign.v05.seed.cli-zero-model",
        "--source-identity",
        "sha256:" + "b" * 64,
        "--phase",
        "seed",
        "--r-bundle",
        "PASS",
        "--r-annotation",
        "PENDING",
        "--r-judge",
        "PENDING",
        "--r-runner",
        "PASS",
        "--writer-prompt",
        str(prompt_path),
        "--writer-schema",
        str(schema_path),
        "--assembly-attestation",
        str(attestation_path),
        "--effective-budget",
        str(budget_path),
        "--database-url",
        context.database_url,
        "--ledger-run-id",
        "run.production-factory",
        "--temperature",
        "0",
        "--seed",
        "17",
        "--seed-capability",
        "fixed",
        "--evidence-token-budget",
        "512",
        "--concurrency",
        "1",
        "--controller-level",
        "C1+C2",
        "--planner-level",
        "P0+P1",
        "--thinking-disabled",
        "--deterministic-scorer-version",
        "deterministic-v1",
        "--answer-judge-version",
        "pending",
        "--evidence-judge-version",
        "pending",
        "--judge-unavailable-policy",
        "pending",
        "--repetitions",
        "5",
        "--max-model-calls",
        "100",
        "--max-wall-time-seconds",
        "3600",
        "--output-namespace",
        "evaluation/v05/seed",
        "--object-namespace",
        "evaluation/v05/seed",
        "--database-namespace",
        "evaluation/v05/seed",
        "--stop-condition",
        "stop on unavailable judge",
        "--report-dimension",
        "writer_readout",
        "--threshold-policy",
        "diagnostic_only",
    ]
    first = subprocess.run(
        arguments,
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    assert output_path.is_file()
    assert receipt_path.is_file()
    frozen_manifest = load_v05_readout_campaign_manifest(output_path)
    receipt = V05ReadoutFreezeReceipt.model_validate_json(receipt_path.read_bytes(), strict=True)
    assert frozen_manifest.phase.value == "seed"
    assert frozen_manifest.source.assembly_attestation_ref.media_type == (
        PRODUCTION_ASSEMBLY_ATTESTATION_MEDIA_TYPE
    )
    assert frozen_manifest.source.effective_budget_ref.media_type == EFFECTIVE_BUDGET_MEDIA_TYPE
    assert (
        receipt.manifest_ref.artifact_id
        == _ref(
            "campaign-manifest",
            V05_READOUT_CAMPAIGN_MANIFEST_MEDIA_TYPE,
            output_path.read_bytes(),
        ).artifact_id
    )
    assert receipt.zero_model_call is True
    assert receipt.ledger_before_request_count == receipt.ledger_after_request_count == 0
    assert assembly.model_gateway.call_ledger.list_for_run(RunId("run.production-factory")) == ()

    second = subprocess.run(
        arguments,
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode != 0
    assert "refusing to overwrite immutable campaign manifest" in second.stderr
