#!/usr/bin/env python3
"""Freeze one V0.5 Writer-readout campaign before any model call."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Literal, cast

from sqlalchemy.exc import SQLAlchemyError

from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.model_call_ledger import SqlModelCallLedger
from novel_agent.domain.artifacts import (
    EFFECTIVE_BUDGET_MEDIA_TYPE,
    PRODUCTION_ASSEMBLY_ATTESTATION_MEDIA_TYPE,
    V05_READOUT_CAMPAIGN_MANIFEST_MEDIA_TYPE,
    ArtifactRef,
)
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId
from novel_agent.domain.model_calls import EffectiveBudgetResult
from novel_agent.domain.production_assembly import ResolvedProductionAssemblyAttestation
from novel_agent.domain.v05_readout import (
    V05CampaignExecutionFreeze,
    V05CampaignPhase,
    V05CampaignReportFreeze,
    V05CampaignSourceIdentity,
    V05CampaignStatus,
    V05CampaignThreshold,
    V05JudgeRuntimeFreeze,
    V05ReadinessStatus,
    V05ReadoutFreezeReceipt,
    V05RepresentativeTaskCoverage,
)
from novel_agent.services.v05_readout_manifest import (
    V05ReadoutManifestError,
    derive_v05_writer_runtime_from_attestation,
    freeze_v05_readout_campaign,
    load_effective_budget_result,
    load_production_attestation,
    load_v05_readout_manifest,
    select_v05_representative_task_coverage,
)


def _artifact_ref(path: Path, media_type: str, schema_version: SchemaVersion) -> ArtifactRef:
    return _artifact_ref_from_bytes(path.read_bytes(), media_type, schema_version)


def _artifact_ref_from_bytes(
    content: bytes, media_type: str, schema_version: SchemaVersion
) -> ArtifactRef:
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactRef(
        artifact_id=ArtifactId(f"sha256:{digest}"),
        media_type=media_type,
        byte_length=len(content),
        schema_version=schema_version,
    )


def _threshold(value: str) -> V05CampaignThreshold:
    metric, operator, raw_value = value.split(":", maxsplit=2)
    if operator not in {">=", "<=", "=="}:
        raise ValueError(f"unsupported threshold operator: {operator}")
    typed_operator = cast(Literal[">=", "<=", "=="], operator)
    return V05CampaignThreshold(metric=metric, operator=typed_operator, value=float(raw_value))


def _phase_status(phase: V05CampaignPhase) -> V05CampaignStatus:
    return {
        V05CampaignPhase.SEED: V05CampaignStatus.SEED_DIAGNOSTIC_NOT_ACCEPTANCE,
        V05CampaignPhase.CALIBRATION: V05CampaignStatus.CALIBRATION_NOT_ACCEPTANCE,
        V05CampaignPhase.FORMAL: V05CampaignStatus.FORMAL_CANDIDATE,
    }[phase]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze a V0.5 Writer-readout campaign without calling a model."
    )
    parser.add_argument(
        "--bundle-root",
        type=Path,
        default=Path("benchmarks/private/ztj_novelmem_v0.5"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--source-identity", required=True)
    parser.add_argument("--phase", choices=[item.value for item in V05CampaignPhase], required=True)
    for name in ("r-bundle", "r-annotation", "r-judge", "r-runner"):
        parser.add_argument(
            f"--{name}",
            dest=name.replace("-", "_"),
            choices=[item.value for item in V05ReadinessStatus],
            required=True,
        )
    parser.add_argument(
        "--writer-endpoint",
        "--expected-writer-endpoint",
        dest="expected_writer_endpoint",
    )
    parser.add_argument(
        "--writer-model",
        "--expected-writer-model",
        dest="expected_writer_model",
    )
    parser.add_argument(
        "--writer-revision",
        "--expected-writer-revision",
        dest="expected_writer_revision",
    )
    parser.add_argument("--writer-prompt", type=Path)
    parser.add_argument("--writer-schema", type=Path)
    parser.add_argument("--assembly-attestation", type=Path)
    parser.add_argument("--effective-budget", type=Path)
    parser.add_argument("--database-url")
    parser.add_argument("--ledger-run-id")
    parser.add_argument("--freeze-receipt-output", type=Path)
    parser.add_argument("--readiness-output", type=Path)
    parser.add_argument("--writer-prompt-media-type", default="text/plain")
    parser.add_argument("--writer-schema-media-type", default="application/json")
    parser.add_argument("--contract-schema-version", default="1.0.0")
    parser.add_argument("--writer-role", default="writer_readout")
    parser.add_argument("--judge-role", default="writer_judge")
    parser.add_argument("--temperature", type=float, required=True)
    parser.add_argument("--seed", type=int)
    parser.add_argument(
        "--seed-capability",
        choices=("fixed", "unsupported", "uncontrolled"),
        required=True,
    )
    parser.add_argument("--evidence-token-budget", type=int, required=True)
    parser.add_argument(
        "--output-token-budget",
        "--expected-body-output-budget",
        dest="expected_body_output_budget",
        type=int,
    )
    parser.add_argument(
        "--expected-provider-total-output-budget",
        dest="expected_provider_total_output_budget",
        type=int,
    )
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument(
        "--budget-source",
        "--expected-budget-source",
        dest="expected_budget_source",
    )
    parser.add_argument(
        "--context-limit", "--expected-context-limit", dest="expected_context_limit", type=int
    )
    parser.add_argument(
        "--reasoning-reserve",
        "--expected-reasoning-reserve",
        dest="expected_reasoning_reserve",
        type=int,
    )
    parser.add_argument(
        "--safety-allowance",
        "--expected-safety-allowance",
        dest="expected_safety_allowance",
        type=int,
    )
    parser.add_argument("--controller-level", choices=("C0", "C1+C2"), required=True)
    parser.add_argument("--planner-level", choices=("P0", "P0+P1"), required=True)
    thinking = parser.add_mutually_exclusive_group(required=True)
    thinking.add_argument("--thinking-enabled", action="store_true")
    thinking.add_argument("--thinking-disabled", action="store_true")
    provider_reasoning = parser.add_mutually_exclusive_group()
    provider_reasoning.add_argument(
        "--expected-reasoning-included-in-completion-tokens",
        dest="expected_reasoning_included",
        action="store_true",
    )
    provider_reasoning.add_argument(
        "--expected-reasoning-excluded-from-completion-tokens",
        dest="expected_reasoning_included",
        action="store_false",
    )
    parser.set_defaults(expected_reasoning_included=None)
    parser.add_argument("--deterministic-scorer-version", required=True)
    parser.add_argument("--answer-judge-version", required=True)
    parser.add_argument("--evidence-judge-version", required=True)
    parser.add_argument(
        "--judge-unavailable-policy",
        choices=("pending", "unavailable", "fail_closed"),
        required=True,
    )
    parser.add_argument("--repetitions", type=int, required=True)
    parser.add_argument("--max-model-calls", type=int, required=True)
    parser.add_argument("--max-wall-time-seconds", type=int, required=True)
    parser.add_argument("--output-namespace", required=True)
    parser.add_argument("--object-namespace", required=True)
    parser.add_argument("--database-namespace", required=True)
    parser.add_argument("--stop-condition", action="append", required=True)
    parser.add_argument("--representative-task-id", action="append", default=[])
    parser.add_argument("--representative-early-task-id")
    parser.add_argument("--representative-mid-task-id")
    parser.add_argument("--representative-late-task-id")
    parser.add_argument("--representative-legacy-five-task-id")
    parser.add_argument("--representative-twenty-chapter-task-id")
    parser.add_argument("--representative-unanswerable-task-id")
    parser.add_argument("--representative-multi-hop-task-id")
    parser.add_argument("--report-dimension", action="append", required=True)
    parser.add_argument(
        "--threshold-policy",
        choices=("diagnostic_only", "pre_registered"),
        required=True,
    )
    parser.add_argument("--threshold", action="append", default=[])
    return parser


def main() -> int:
    args = _parser().parse_args()
    bundle_root = args.bundle_root.resolve()
    output = (
        args.output.resolve()
        if args.output is not None
        else bundle_root / "private/manifests/writer_readout_campaign_manifest.json"
    )
    freeze_receipt_output = (
        args.freeze_receipt_output.resolve()
        if args.freeze_receipt_output is not None
        else output.with_name(f"{output.stem}.freeze-receipt.json")
    )
    if output == freeze_receipt_output:
        raise SystemExit("campaign manifest and freeze receipt must use distinct output paths")
    if output.exists():
        raise SystemExit(f"refusing to overwrite immutable campaign manifest: {output}")
    if freeze_receipt_output.exists():
        raise SystemExit(f"refusing to overwrite immutable freeze receipt: {freeze_receipt_output}")

    try:
        schema_version = SchemaVersion(args.contract_schema_version)
        readout_manifest = load_v05_readout_manifest(bundle_root)
        build_report = bundle_root / "reports/build_report.json"
        if not build_report.is_file():
            return _ready_to_freeze(
                args,
                output,
                (f"missing source build report: {build_report}",),
            )
        if args.assembly_attestation is None:
            return _ready_to_freeze(
                args,
                output,
                ("U2 ResolvedProductionAssemblyAttestation artifact is required",),
            )
        if args.effective_budget is None:
            return _ready_to_freeze(
                args,
                output,
                ("U2 EffectiveBudgetResult artifact is required",),
            )
        if args.database_url is None or args.ledger_run_id is None:
            return _ready_to_freeze(
                args,
                output,
                ("one durable SQL ledger URL and ledger run id are required",),
            )
        if args.writer_prompt is None or not args.writer_prompt.is_file():
            return _ready_to_freeze(
                args,
                output,
                ("Writer prompt artifact is required and must exist",),
            )
        if args.writer_schema is None or not args.writer_schema.is_file():
            return _ready_to_freeze(
                args,
                output,
                ("Writer response-schema artifact is required and must exist",),
            )
        attestation_path = args.assembly_attestation.resolve()
        effective_budget_path = args.effective_budget.resolve()
        attestation = load_production_attestation(attestation_path)
        effective_budget = load_effective_budget_result(effective_budget_path)
        ledger = SqlModelCallLedger(build_session_factory(build_engine(args.database_url)))
        ledger_run_id = RunId(args.ledger_run_id)
        ledger_before_count = len(ledger.list_for_run(ledger_run_id))
        if ledger_before_count != 0:
            return _ready_to_freeze(
                args,
                output,
                (
                    "freeze ledger must be empty before manifest creation; "
                    f"observed {ledger_before_count} requests",
                ),
            )
        ledger_identity = (
            f"{type(ledger).__module__}.{type(ledger).__qualname__}:{ledger_run_id.root}"
        )
        attestation_ref = _artifact_ref(
            attestation_path,
            PRODUCTION_ASSEMBLY_ATTESTATION_MEDIA_TYPE,
            schema_version,
        )
        effective_budget_ref = _artifact_ref(
            effective_budget_path,
            EFFECTIVE_BUDGET_MEDIA_TYPE,
            schema_version,
        )
        prompt_ref = _artifact_ref(
            args.writer_prompt.resolve(), args.writer_prompt_media_type, schema_version
        )
        response_schema_ref = _artifact_ref(
            args.writer_schema.resolve(), args.writer_schema_media_type, schema_version
        )
        writer = derive_v05_writer_runtime_from_attestation(
            attestation=attestation,
            effective_budget=effective_budget,
            prompt_ref=prompt_ref,
            response_schema_ref=response_schema_ref,
            request_role=args.writer_role,
            temperature=args.temperature,
            seed=args.seed,
            seed_capability=args.seed_capability,
            evidence_token_budget=args.evidence_token_budget,
            concurrency=args.concurrency,
        )
        for name, expected, observed in (
            ("writer endpoint", args.expected_writer_endpoint, writer.endpoint),
            ("writer model", args.expected_writer_model, writer.model),
            ("writer revision", args.expected_writer_revision, writer.revision),
        ):
            if expected is not None and expected != observed:
                raise V05ReadoutManifestError(
                    f"manual {name} assertion does not match the resolved assembly facts"
                )
        coverage = select_v05_representative_task_coverage(
            readout_manifest,
            bundle_root=bundle_root,
        )
        representative_ids = coverage.task_ids()
        _assert_expected_representatives(args, coverage)
        if args.representative_task_id:
            expected_ids = tuple(StableId(item) for item in args.representative_task_id)
            if set(expected_ids) != set(representative_ids):
                raise V05ReadoutManifestError(
                    "manual representative task ids are assertions and do not match "
                    "derived coverage"
                )
        _assert_expected_budget(args, effective_budget, attestation)
        thresholds = tuple(_threshold(item) for item in args.threshold)
        phase = V05CampaignPhase(args.phase)
        source = V05CampaignSourceIdentity(
            bundle_id=readout_manifest.benchmark_id,
            bundle_version=readout_manifest.version,
            build_report_ref=_artifact_ref(build_report, "application/json", schema_version),
            assembly_attestation_ref=attestation_ref,
            effective_budget_ref=effective_budget_ref,
            source_identity=ArtifactId(args.source_identity),
            r_bundle=V05ReadinessStatus(args.r_bundle),
            r_annotation=V05ReadinessStatus(args.r_annotation),
            r_judge=V05ReadinessStatus(args.r_judge),
            r_runner=V05ReadinessStatus(args.r_runner),
        )
        manifest = freeze_v05_readout_campaign(
            readout_manifest=readout_manifest,
            campaign_id=StableId(args.campaign_id),
            phase=phase,
            status=_phase_status(phase),
            source=source,
            writer=writer,
            judges=V05JudgeRuntimeFreeze(
                request_role=args.judge_role,
                deterministic_scorer_version=args.deterministic_scorer_version,
                answer_judge_version=args.answer_judge_version,
                evidence_support_judge_version=args.evidence_judge_version,
                unavailable_policy=args.judge_unavailable_policy,
            ),
            effective_budget=effective_budget,
            provider_reasoning_included_in_completion_tokens=(
                attestation.reasoning_included_in_completion_tokens
            ),
            controller_level=args.controller_level,
            planner_level=args.planner_level,
            thinking_enabled=args.thinking_enabled,
            execution=V05CampaignExecutionFreeze(
                repetitions=args.repetitions,
                max_model_calls=args.max_model_calls,
                max_wall_time_seconds=args.max_wall_time_seconds,
                output_namespace=args.output_namespace,
                object_namespace=args.object_namespace,
                database_namespace=args.database_namespace,
                stop_conditions=tuple(args.stop_condition),
            ),
            report=V05CampaignReportFreeze(
                dimensions=tuple(args.report_dimension),
                threshold_policy=args.threshold_policy,
                thresholds=thresholds,
            ),
            representative_task_ids=representative_ids,
            representative_task_coverage=coverage,
        )
    except SQLAlchemyError as error:
        return _ready_to_freeze(
            args,
            output,
            (f"durable SQL ledger is unavailable: {error}",),
        )
    except V05ReadoutManifestError as error:
        return _ready_to_freeze(args, output, (str(error),))

    manifest_payload = (
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    try:
        ledger_after_count = len(ledger.list_for_run(ledger_run_id))
        freeze_receipt = V05ReadoutFreezeReceipt(
            receipt_id=StableId(f"freeze.{manifest.campaign_id.root}"[:128]),
            campaign_id=manifest.campaign_id,
            ledger_identity=ledger_identity,
            ledger_before_request_count=ledger_before_count,
            ledger_after_request_count=ledger_after_count,
            manifest_ref=_artifact_ref_from_bytes(
                manifest_payload.encode("utf-8"),
                V05_READOUT_CAMPAIGN_MANIFEST_MEDIA_TYPE,
                schema_version,
            ),
            attestation_ref=manifest.source.assembly_attestation_ref,
            effective_budget_ref=manifest.source.effective_budget_ref,
        )
    except SQLAlchemyError as error:
        return _ready_to_freeze(
            args,
            output,
            (f"durable SQL ledger became unavailable before freeze receipt: {error}",),
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(manifest_payload, encoding="utf-8")
    freeze_receipt_output.parent.mkdir(parents=True, exist_ok=True)
    freeze_receipt_output.write_text(
        json.dumps(
            freeze_receipt.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote {output} and {freeze_receipt_output}: {len(readout_manifest.tasks)} readout tasks"
    )
    return 0


def _ready_to_freeze(args: argparse.Namespace, output: Path, missing: tuple[str, ...]) -> int:
    """Report exact Stage-B facts without manufacturing a final manifest."""

    payload = {
        "status": "READY_TO_FREEZE",
        "manifest_written": False,
        "manifest_output": str(output),
        "missing_facts": list(missing),
        "stage_b_owner": "U2 production preflight / registered endpoint",
        "required_facts": [
            "verifiable ResolvedProductionAssemblyAttestation with implementation "
            "endpoint revision",
            "registered endpoint sequence/output/reasoning/safety limits",
            "one EffectiveBudgetResult resolved by ModelGateway",
            "Writer prompt and response-schema artifact refs",
            "durable SQL ledger identity with before/after request count zero",
            "manifest, attestation, and EffectiveBudgetResult refs in one freeze receipt",
        ],
    }
    if args.readiness_output is not None:
        readiness = args.readiness_output.resolve()
        if readiness.exists():
            raise SystemExit(f"refusing to overwrite readiness report: {readiness}")
        readiness.parent.mkdir(parents=True, exist_ok=True)
        payload["readiness_output"] = str(readiness)
        readiness.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _assert_expected_representatives(
    args: argparse.Namespace,
    coverage: V05RepresentativeTaskCoverage,
) -> None:
    expected = {
        "early_checkpoint_task_id": args.representative_early_task_id,
        "mid_checkpoint_task_id": args.representative_mid_task_id,
        "late_checkpoint_task_id": args.representative_late_task_id,
        "legacy_five_chapter_window_task_id": args.representative_legacy_five_task_id,
        "twenty_chapter_window_task_id": args.representative_twenty_chapter_task_id,
        "unanswerable_qa_task_id": args.representative_unanswerable_task_id,
        "multi_hop_qa_task_id": args.representative_multi_hop_task_id,
    }
    for field, value in expected.items():
        if value is not None and StableId(value) != getattr(coverage, field):
            raise V05ReadoutManifestError(
                f"manual representative assertion {field} does not match derived coverage"
            )


def _assert_expected_budget(
    args: argparse.Namespace,
    effective_budget: EffectiveBudgetResult,
    attestation: ResolvedProductionAssemblyAttestation,
) -> None:
    expected = {
        "expected_budget_source": "budget_source",
        "expected_context_limit": "context_limit",
        "expected_body_output_budget": "body_output_budget",
        "expected_reasoning_reserve": "thinking_budget",
        "expected_safety_allowance": "safety_allowance_tokens",
    }
    for arg_name, field in expected.items():
        value = getattr(args, arg_name)
        if value is not None:
            observed = getattr(effective_budget, field)
            observed = getattr(observed, "value", observed)
            if observed != value:
                raise V05ReadoutManifestError(
                    f"manual budget assertion {arg_name} does not match EffectiveBudgetResult"
                )
    if (
        args.expected_provider_total_output_budget is not None
        and args.expected_provider_total_output_budget != effective_budget.total_output_budget
    ):
        raise V05ReadoutManifestError(
            "manual provider total output assertion does not match EffectiveBudgetResult"
        )
    if (
        args.expected_reasoning_included is not None
        and args.expected_reasoning_included != attestation.reasoning_included_in_completion_tokens
    ):
        raise V05ReadoutManifestError(
            "manual reasoning billing assertion does not match the assembly attestation"
        )


if __name__ == "__main__":
    raise SystemExit(main())
