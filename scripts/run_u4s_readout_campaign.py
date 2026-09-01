#!/usr/bin/env python3
"""Run the frozen U4-S Track A/B seed Writer readout.

The script deliberately stops at Writer responses plus pending typed judges. It
never loads Gold, writes Canon/Memory, accepts a candidate, or settles a
creative task. Every task receives a fresh freeze receipt and the evaluation
artifacts are logically discarded only after the readout batch is closed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse

from pydantic import ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import OpenAICompatibleChatEndpoint
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.artifacts import (
    EVALUATION_NAMESPACE_DISCARD_MEDIA_TYPE,
    MODEL_RAW_RESPONSE_MEDIA_TYPE,
    U4S_SEED_READOUT_REPORT_MEDIA_TYPE,
    V05_READOUT_FREEZE_RECEIPT_MEDIA_TYPE,
    ArtifactRef,
)
from novel_agent.domain.creative_runtime import AutomationMode, CreativeRunPolicy
from novel_agent.domain.ids import ProjectId, RunId, SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelCallLedgerStatus, ModelRole
from novel_agent.domain.production_assembly import ResolvedProductionAssemblyAttestation
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.domain.v05_readout import (
    BenchmarkFailureLayer,
    U4SSeedReadoutReport,
    U4SSeedTaskReport,
    V05CampaignPhase,
    V05ReadoutCampaignManifest,
    V05ReadoutFreezeReceipt,
    V05ReadoutTaskIdentity,
    V05ReadoutTrack,
)
from novel_agent.domain.writer_context import FreezeReceipt
from novel_agent.runtime.creative_assembly import (
    DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    ProductionAssemblyContext,
    load_production_runtime_assembly,
)
from novel_agent.services.artifacts import ArtifactRepository, sha256_id
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.evaluation_namespace import discard_evaluation_namespace
from novel_agent.services.evidence_first_checkpoint_runner import EvidenceFirstCheckpointRunner
from novel_agent.services.model_gateway import (
    ModelCallForbiddenError,
    ModelGateway,
    RawResponsePersistenceError,
    RegisteredModelEndpoint,
    StructuredGenerationExhausted,
)
from novel_agent.services.u4s_seed_readout import (
    U4SCheckpointInput,
    U4SPublicCorpus,
    U4SSeedInputError,
    as_run_request_id,
)
from novel_agent.services.writer_context_readout import (
    CONTEXT_READOUT_STAGE,
    QA_READOUT_STAGE,
    WriterContextReadoutError,
    WriterContextReadoutRequest,
    bind_production_context_readout,
    bind_production_qa_readout,
    readout_model_request_id,
)
from novel_agent.services.writer_judge import WriterJudgeService

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = SchemaVersion("1.0.0")
PACKAGE_MEDIA_TYPE = "application/vnd.novel-agent.writer-context-package-v2+json"
LEDGER_MEDIA_TYPE = "application/vnd.novel-agent.evidence-ledger-v2+json"
# The full seed corpus has observed Context latency up to ~28s.  Keep the
# broader timeout local to this diagnostic campaign; production binding
# defaults remain unchanged.
U4S_WRITER_TIMEOUT_SECONDS = 60.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--freeze-receipt", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument(
        "--bundle-root", type=Path, default=ROOT / "benchmarks/private/ztj_novelmem_v0.5"
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("representative", "full"), required=True)
    parser.add_argument("--model-base-url")
    parser.add_argument("--model")
    parser.add_argument("--assembly-factory", default=DEFAULT_PRODUCTION_ASSEMBLY_FACTORY)
    return parser


def _load_inputs(
    manifest_path: Path,
    freeze_path: Path,
    facts_path: Path,
) -> tuple[V05ReadoutCampaignManifest, V05ReadoutFreezeReceipt, dict[str, Any]]:
    manifest = V05ReadoutCampaignManifest.model_validate_json(
        manifest_path.read_bytes(), strict=True
    )
    freeze = V05ReadoutFreezeReceipt.model_validate_json(freeze_path.read_bytes(), strict=True)
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    if not isinstance(facts, dict) or facts.get("schema") != "u4s-campaign-facts.v1":
        raise U4SSeedInputError("facts artifact is not u4s-campaign-facts.v1")
    if freeze.campaign_id != manifest.campaign_id:
        raise U4SSeedInputError("freeze receipt campaign does not match the manifest")
    if manifest.phase is not V05CampaignPhase.SEED:
        raise U4SSeedInputError("U4-S seed runner accepts only a frozen seed manifest")
    if facts.get("ledger_request_count") != 0 or freeze.ledger_before_request_count != 0:
        raise U4SSeedInputError("U4-S seed facts are not zero-call facts")
    return manifest, freeze, facts


def _selected_tasks(
    manifest: V05ReadoutCampaignManifest,
    mode: str,
) -> tuple[V05ReadoutTaskIdentity, ...]:
    all_tasks = {task.task_id: task for task in manifest.readout_manifest.tasks}
    if mode == "full":
        return tuple(manifest.readout_manifest.tasks)
    selected = tuple(all_tasks[task_id] for task_id in manifest.representative_task_ids)
    if len(selected) != 7:
        raise U4SSeedInputError("frozen representative coverage must contain seven tasks")
    return selected


def _endpoint_name(base_url: str, model: str) -> str:
    parsed = urlparse(base_url)
    return f"{model}@{parsed.port or (443 if parsed.scheme == 'https' else 80)}"


def _copy_canonical_refs(
    source: ArtifactRepository,
    destination: ArtifactRepository,
    refs: tuple[ArtifactRef, ...],
) -> None:
    """Seed a run object namespace with immutable canonical root objects."""

    for ref in refs:
        copied = destination.put(source.read_verified(ref), ref.media_type, ref.schema_version)
        if (
            copied.artifact_id != ref.artifact_id
            or copied.media_type != ref.media_type
            or copied.byte_length != ref.byte_length
            or copied.schema_version != ref.schema_version
        ):
            raise U4SSeedInputError(
                "canonical root copy changed identity: "
                f"expected={ref.artifact_id.root} copied={copied.artifact_id.root}"
            )


def _seed_production_object_namespace(
    *,
    database_url: str,
    destination_root: Path,
    facts: dict[str, Any],
    project_id: ProjectId,
) -> None:
    """Copy the facts-time canonical roots into this run's isolated object store."""

    source_root = Path(str(facts.get("object_store_root", ""))).resolve()
    if not source_root.is_dir():
        raise U4SSeedInputError(f"facts object store is missing: {source_root}")
    if source_root == destination_root.resolve():
        raise U4SSeedInputError("facts and run object namespaces must be distinct")

    source = ArtifactRepository(FilesystemObjectStore(source_root))
    destination = ArtifactRepository(FilesystemObjectStore(destination_root))
    engine = build_engine(database_url)
    try:
        commits = CommitService(build_session_factory(engine))
        current_commit = commits.current_commit(project_id)
        expected_basis = str(facts.get("basis_commit", ""))
        if current_commit.root != expected_basis:
            raise U4SSeedInputError(
                "facts basis commit differs from current production project: "
                f"facts={expected_basis} current={current_commit.root}"
            )
        manifest = commits.load_manifest(current_commit)
        _copy_canonical_refs(
            source,
            destination,
            (
                manifest.text_root,
                manifest.plan_root,
                manifest.world_root,
                manifest.reference_root,
                manifest.project_profile_root,
            ),
        )
    finally:
        engine.dispose()


def _build_production_binding(
    *,
    database_url: str,
    output_root: Path,
    run_id: RunId,
    facts: dict[str, Any],
    attestation: ResolvedProductionAssemblyAttestation,
    manifest: V05ReadoutCampaignManifest,
    base_url: str,
    model: str,
    assembly_factory: str,
) -> tuple[Any, ArtifactRepository, ModelGateway]:
    if urlparse(base_url).hostname not in {"127.0.0.1", "localhost"}:
        raise U4SSeedInputError("U4-S Writer endpoint must be loopback")
    output_root.mkdir(parents=True)
    object_root = output_root / "objects"
    project_id = ProjectId(str(facts["project_id"]))
    _seed_production_object_namespace(
        database_url=database_url,
        destination_root=object_root,
        facts=facts,
        project_id=project_id,
    )
    endpoint = OpenAICompatibleChatEndpoint(
        base_url=base_url,
        model=model,
        max_output_tokens=manifest.writer.output_token_budget,
        temperature=manifest.writer.temperature,
        local_only=True,
        max_retries=0,
    )
    registration = RegisteredModelEndpoint(
        role=ModelRole.IMPLEMENTATION,
        endpoint_name=_endpoint_name(base_url, model),
        model_name=model,
        adapter=endpoint,
        revision=manifest.writer.revision,
        sequence_limit=manifest.runtime.context_limit,
        output_limit=manifest.writer.output_token_budget,
        safety_allowance_tokens=manifest.runtime.safety_allowance_tokens,
        estimated_reasoning_reserve=manifest.runtime.reasoning_reserve_tokens,
        default_thinking=manifest.runtime.thinking_enabled,
        reasoning_included_in_completion_tokens=(
            bool(manifest.runtime.provider_reasoning_included_in_completion_tokens)
        ),
        global_output_cap=manifest.writer.provider_total_output_budget or 131_072,
    )
    # U6-A C-ROLL uses the same frozen local endpoint for the existing
    # Stage-2M Planner owner.  This is an additional named role on the same
    # production gateway, not a second transport or a new assembly factory;
    # U4-S Writer requests continue to route through IMPLEMENTATION.
    planner_registration = RegisteredModelEndpoint(
        role=ModelRole.BATCH_TEST,
        endpoint_name=_endpoint_name(base_url, model) + ".batch",
        model_name=model,
        adapter=endpoint,
        revision=manifest.writer.revision,
        sequence_limit=manifest.runtime.context_limit,
        output_limit=manifest.writer.output_token_budget,
        safety_allowance_tokens=manifest.runtime.safety_allowance_tokens,
        estimated_reasoning_reserve=manifest.runtime.reasoning_reserve_tokens,
        default_thinking=manifest.runtime.thinking_enabled,
        reasoning_included_in_completion_tokens=(
            bool(manifest.runtime.provider_reasoning_included_in_completion_tokens)
        ),
        global_output_cap=manifest.writer.provider_total_output_budget or 131_072,
    )
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
        assembly_factory,
        ProductionAssemblyContext(
            database_url=database_url,
            object_store_root=object_root,
            project_id=project_id,
            run_id=run_id,
            policy=policy,
            manifest=runtime_manifest,
            model_endpoints=(registration, planner_registration),
            schema_version=SCHEMA_VERSION,
        ),
    )
    assert assembly.artifacts is not None
    assert assembly.model_gateway is not None
    observed = assembly.model_gateway.endpoint_runtime_identity(ModelRole.IMPLEMENTATION)
    expected = (manifest.writer.endpoint, manifest.writer.model, manifest.writer.revision)
    if observed != expected:
        raise U4SSeedInputError(f"production Writer identity differs from freeze: {observed!r}")
    attested = tuple(
        item for item in attestation.endpoints if item.role == ModelRole.IMPLEMENTATION.value
    )
    if (
        len(attested) != 1
        or (attested[0].endpoint_name, attested[0].model_name, attested[0].revision) != expected
    ):
        raise U4SSeedInputError("production Writer identity differs from frozen attestation")
    if len(assembly.model_gateway.call_ledger.list_for_run(run_id)) != 0:
        raise U4SSeedInputError("Writer run identity already has model calls in the SQL ledger")
    return assembly, assembly.artifacts, assembly.model_gateway


def _freeze_receipt(
    *,
    manifest: V05ReadoutCampaignManifest,
    task_input: U4SCheckpointInput,
    package_ref: ArtifactRef,
    ledger_ref: ArtifactRef,
) -> FreezeReceipt:
    identity = task_input.identity
    public_hash = content_id(
        {
            "campaign_id": manifest.campaign_id.root,
            "task_id": identity.task_id.root,
            "checkpoint": identity.checkpoint_chapter,
            "track": identity.track.value,
            "information_profile": identity.information_profile.value,
        }
    )
    config_hash = content_id(
        {
            "campaign_id": manifest.campaign_id.root,
            "writer": manifest.writer.model_dump(mode="json"),
            "runtime": manifest.runtime.model_dump(mode="json"),
        }
    )
    return FreezeReceipt(
        receipt_id=StableId(f"freeze.u4s.{identity.task_id.root}"[:128]),
        public_input_hash=public_hash,
        code_version="u4s_seed_readout.v1",
        run_config_hash=config_hash,
        arm_artifact_hashes={
            "A": package_ref.artifact_id,
            "B": ledger_ref.artifact_id,
            "C": task_input.text.root_hash,
        },
        frozen_before_reveal=True,
    )


def _artifact_ref_from_bytes(data: bytes, media_type: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=sha256_id(data),
        media_type=media_type,
        byte_length=len(data),
        schema_version=SCHEMA_VERSION,
    )


def _failure_layer(error: BaseException, *, writer_started: bool) -> BenchmarkFailureLayer:
    if isinstance(error, (ModelCallForbiddenError, TimeoutError, ConnectionError)):
        return BenchmarkFailureLayer.TRANSPORT
    if isinstance(error, RawResponsePersistenceError):
        return BenchmarkFailureLayer.RAW
    if isinstance(error, (ValidationError, StructuredGenerationExhausted)):
        return BenchmarkFailureLayer.PARSE
    if isinstance(error, WriterContextReadoutError):
        return BenchmarkFailureLayer.WRITER_ANSWER
    return BenchmarkFailureLayer.WRITER_ANSWER if writer_started else BenchmarkFailureLayer.PACKAGE


def _entry_for_task(
    gateway: ModelGateway,
    run_id: RunId,
    task_id: StableId,
    track: V05ReadoutTrack,
) -> Any:
    stage = QA_READOUT_STAGE if track is V05ReadoutTrack.QA else CONTEXT_READOUT_STAGE
    request_id = readout_model_request_id(run_id=run_id, task_id=task_id.root, stage=stage)
    return gateway.call_ledger.load(request_id)


async def _run_task(
    *,
    manifest: V05ReadoutCampaignManifest,
    task_input: U4SCheckpointInput,
    case_id: ProjectId,
    run_id: RunId,
    artifacts: ArtifactRepository,
    gateway: ModelGateway,
    discarded_refs: list[ArtifactRef],
) -> U4SSeedTaskReport:
    identity = task_input.identity
    freeze_id = StableId(f"freeze.u4s.{identity.task_id.root}"[:128])
    package_ref: ArtifactRef | None = None
    ledger_ref: ArtifactRef | None = None
    freeze_ref: ArtifactRef | None = None
    response_ref: ArtifactRef | None = None
    record_ref: ArtifactRef | None = None
    raw_ref: ArtifactRef | None = None
    judge_refs: list[ArtifactRef] = []
    writer_started = False
    package_status = "NOT_BUILT"
    semantic_status = "UNASSESSED"
    try:
        package_run = EvidenceFirstCheckpointRunner(
            writer_token_budget=manifest.writer.output_token_budget,
            evidence_ledger_token_budget=manifest.writer.evidence_token_budget,
        ).run(
            case_id=case_id,
            task=task_input.task,
            world=task_input.world,
            text=task_input.text,
            plan=task_input.plan,
            base_commit=task_input.basis_commit,
            snapshot_id=task_input.snapshot_id,
            planning_context=task_input.planning_context,
            frozen_planner_artifact=task_input.planner_artifact,
            frozen_needs=(task_input.need,),
            backend_bundle=task_input.backend_bundle,
            fingerprint=content_id(
                {
                    "campaign_id": manifest.campaign_id.root,
                    "task_id": identity.task_id.root,
                    "basis_commit": task_input.basis_commit.root,
                }
            ),
            run_id=as_run_request_id(identity.task_id),
        )
        if package_run.future_leakage_count:
            raise U4SSeedInputError("evidence-first package contains future leakage")
        package_status = package_run.assembly.status.value
        semantic_status = package_run.assembly.semantic_status
        package_payload = canonical_json_bytes(package_run.assembly.package.model_dump(mode="json"))
        package_ref = artifacts.put(package_payload, PACKAGE_MEDIA_TYPE, SCHEMA_VERSION)
        ledger_payload = canonical_json_bytes(
            package_run.assembly.evidence_ledger.model_dump(mode="json")
        )
        ledger_ref = artifacts.put(ledger_payload, LEDGER_MEDIA_TYPE, SCHEMA_VERSION)
        if (
            ledger_ref.artifact_id != package_run.assembly.package.evidence_ledger_ref.artifact_id
            or ledger_ref.byte_length
            != package_run.assembly.package.evidence_ledger_ref.byte_length
        ):
            raise U4SSeedInputError("persisted evidence ledger changed its frozen identity")
        receipt = _freeze_receipt(
            manifest=manifest,
            task_input=task_input,
            package_ref=package_ref,
            ledger_ref=ledger_ref,
        )
        freeze_payload = canonical_json_bytes(receipt.model_dump(mode="json"))
        freeze_ref = artifacts.put(
            freeze_payload,
            V05_READOUT_FREEZE_RECEIPT_MEDIA_TYPE,
            SCHEMA_VERSION,
        )
        discarded_refs.append(freeze_ref)
        request = WriterContextReadoutRequest(
            task_contract=task_input.task,
            writer_context=package_run.assembly.package,
            freeze_receipt=receipt,
            case_id=StableId(f"case.{identity.checkpoint_id.root}"),
            question_id=identity.question_id,
            question_text=task_input.question_text,
            track=identity.track.value,
        )
        probe: Any
        writer: Any
        if identity.track is V05ReadoutTrack.QA:
            probe, writer = bind_production_qa_readout(
                gateway,
                artifacts,
                run_id=run_id,
                max_output_tokens=manifest.writer.output_token_budget,
                timeout_seconds=U4S_WRITER_TIMEOUT_SECONDS,
                enable_thinking=manifest.runtime.thinking_enabled,
            )
        else:
            probe, writer = bind_production_context_readout(
                gateway,
                artifacts,
                run_id=run_id,
                max_output_tokens=manifest.writer.output_token_budget,
                timeout_seconds=U4S_WRITER_TIMEOUT_SECONDS,
                enable_thinking=manifest.runtime.thinking_enabled,
            )
        writer_started = True
        response = await probe.arun(request)
        if identity.track is V05ReadoutTrack.QA and getattr(response, "answer", None) == "":
            raise WriterContextReadoutError("QA Writer returned an empty answer")
        response_ref = writer.last_response_ref
        record_ref = writer.last_record_ref
        if response_ref is None or record_ref is None:
            raise U4SSeedInputError("production Writer returned no durable readout refs")
        entry = _entry_for_task(gateway, run_id, identity.task_id, identity.track)
        if entry is None or entry.status is not ModelCallLedgerStatus.COMPLETED:
            raise U4SSeedInputError("Writer response has no completed SQL ledger entry")
        raw_ref = entry.raw_artifact_ref
        if raw_ref is None or raw_ref.media_type != MODEL_RAW_RESPONSE_MEDIA_TYPE:
            raise U4SSeedInputError("completed Writer response has no raw response evidence")
        judge_pair = WriterJudgeService(artifacts).pending_pair(
            run_id=run_id,
            task_id=identity.task_id,
            freeze_receipt_id=receipt.receipt_id,
            response_ref=response_ref,
        )
        judge_refs.extend(
            (
                WriterJudgeService(artifacts).persist(judge_pair.answer_judge),
                WriterJudgeService(artifacts).persist(judge_pair.evidence_support_judge),
            )
        )
        discarded_refs.extend((response_ref, record_ref, *judge_refs))
        return U4SSeedTaskReport(
            task_id=identity.task_id,
            track=identity.track,
            checkpoint_chapter=identity.checkpoint_chapter,
            information_profile=identity.information_profile,
            basis_commit_id=task_input.basis_commit,
            snapshot_id=task_input.snapshot_id,
            package_ref=package_ref,
            evidence_ledger_ref=ledger_ref,
            freeze_receipt_id=receipt.receipt_id,
            writer_status="SCHEMA_VALID",
            first_failure_layer=BenchmarkFailureLayer.NONE,
            package_status=package_status,
            semantic_status=semantic_status,
            future_leakage_count=0,
            response_ref=response_ref,
            readout_record_ref=record_ref,
            raw_response_ref=raw_ref,
            judge_receipt_refs=tuple(judge_refs),
        )
    except Exception as error:
        if freeze_ref is not None:
            discarded_refs.append(freeze_ref)
        if response_ref is not None:
            discarded_refs.append(response_ref)
        if record_ref is not None:
            discarded_refs.append(record_ref)
        discarded_refs.extend(judge_refs)
        return U4SSeedTaskReport(
            task_id=identity.task_id,
            track=identity.track,
            checkpoint_chapter=identity.checkpoint_chapter,
            information_profile=identity.information_profile,
            basis_commit_id=task_input.basis_commit,
            snapshot_id=task_input.snapshot_id,
            package_ref=package_ref,
            evidence_ledger_ref=ledger_ref,
            freeze_receipt_id=freeze_id,
            writer_status="TYPED_FAILURE",
            first_failure_layer=_failure_layer(error, writer_started=writer_started),
            package_status=package_status,
            semantic_status=semantic_status,
            future_leakage_count=0,
            response_ref=response_ref,
            readout_record_ref=record_ref,
            raw_response_ref=raw_ref,
            judge_receipt_refs=tuple(judge_refs),
        )


async def _run_campaign(
    *,
    manifest: V05ReadoutCampaignManifest,
    facts: dict[str, Any],
    corpus: U4SPublicCorpus,
    case_id: ProjectId,
    mode: str,
    artifacts: ArtifactRepository,
    gateway: ModelGateway,
) -> U4SSeedReadoutReport:
    run_id = RunId(str(facts["run_id"]))
    selected = _selected_tasks(manifest, mode)
    discarded_refs: list[ArtifactRef] = []
    task_reports: list[U4SSeedTaskReport] = []
    for identity in selected:
        task_input = corpus.checkpoint_input(identity, run_id=run_id)
        report = await _run_task(
            manifest=manifest,
            task_input=task_input,
            case_id=case_id,
            run_id=run_id,
            artifacts=artifacts,
            gateway=gateway,
            discarded_refs=discarded_refs,
        )
        task_reports.append(report)
        if report.writer_status == "TYPED_FAILURE":
            break
    discard_ref: ArtifactRef | None = None
    if discarded_refs:
        memory = None
        for identity in selected:
            memory = corpus.checkpoint_input(identity, run_id=run_id).memory_identity
            break
        assert memory is not None
        discard = discard_evaluation_namespace(
            artifacts,
            run_id=run_id,
            discarded_refs=tuple(dict.fromkeys(discarded_refs)),
            memory_before=memory,
            memory_after=memory,
        )
        discard_payload = canonical_json_bytes(discard.model_dump(mode="json"))
        discard_ref = _artifact_ref_from_bytes(
            discard_payload, EVALUATION_NAMESPACE_DISCARD_MEDIA_TYPE
        )
    failures = tuple(
        item.first_failure_layer
        for item in task_reports
        if item.first_failure_layer is not BenchmarkFailureLayer.NONE
    )
    return U4SSeedReadoutReport(
        campaign_id=manifest.campaign_id,
        mode=cast(Literal["representative", "full"], mode),
        run_id=run_id,
        task_count=len(task_reports),
        qa_count=sum(item.track is V05ReadoutTrack.QA for item in task_reports),
        context_count=sum(item.track is V05ReadoutTrack.CONTEXT for item in task_reports),
        chapters_ingested_once=max((item.checkpoint_chapter for item in task_reports), default=0),
        checkpoint_count=len({item.checkpoint_chapter for item in task_reports}),
        tasks=tuple(task_reports),
        first_failure_layer=failures[0] if failures else BenchmarkFailureLayer.NONE,
        discard_receipt_ref=discard_ref,
        status="REVIEW_REQUIRED" if failures else "COMPLETED",
    )


def main() -> int:
    args = _parser().parse_args()
    if args.output_root.exists():
        raise SystemExit(f"refusing to overwrite U4-S output identity: {args.output_root}")
    manifest, freeze, facts = _load_inputs(
        args.manifest.resolve(), args.freeze_receipt.resolve(), args.facts.resolve()
    )
    base_url = args.model_base_url or str(facts["model_base_url"])
    model = args.model or str(facts["model"])
    attestation = ResolvedProductionAssemblyAttestation.model_validate_json(
        Path(str(facts["attestation"])).read_bytes(), strict=True
    )
    run_id = RunId(str(facts["run_id"]))
    assembly, artifacts, gateway = _build_production_binding(
        database_url=args.database_url,
        output_root=args.output_root.resolve(),
        run_id=run_id,
        facts=facts,
        attestation=attestation,
        manifest=manifest,
        base_url=base_url,
        model=model,
        assembly_factory=args.assembly_factory,
    )
    del assembly, freeze
    corpus = U4SPublicCorpus(args.bundle_root.resolve())
    report = asyncio.run(
        _run_campaign(
            manifest=manifest,
            facts=facts,
            corpus=corpus,
            case_id=ProjectId(str(facts["project_id"])),
            mode=args.mode,
            artifacts=artifacts,
            gateway=gateway,
        )
    )
    payload = canonical_json_bytes(report.model_dump(mode="json", by_alias=True))
    report_ref = artifacts.put(payload, U4S_SEED_READOUT_REPORT_MEDIA_TYPE, SCHEMA_VERSION)
    output_root = args.output_root.resolve()
    report_path = output_root / "u4s_seed_readout_report.json"
    report_path.write_bytes(payload + b"\n")
    ref_path = output_root / "u4s_seed_readout_report.ref.json"
    ref_path.write_text(
        json.dumps(report_ref.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report": str(report_path),
                "report_ref": report_ref.model_dump(mode="json"),
                "status": report.status,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.status == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
