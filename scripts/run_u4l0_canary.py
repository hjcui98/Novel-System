#!/usr/bin/env python3
"""Run the pre-registered U4-L0 real-model single-factor canary.

The evidence-first checkpoint runner remains the sole retrieval, Controller,
Planner, assembly, and semantic owner.  This script only freezes the three
paired arms, gives each arm an isolated model-call ledger, and exports the
comparison evidence required by the U4-L0 gate.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.run_evidence_first_frozen_checkpoints import (
    CASES,
    _checkpoint_state,
    _frozen_backend_bundle,
    _load_checkpoint_index,
    _loopback_http_url,
    _loopback_postgres_url,
    _select_case_basis,
)

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import (
    HttpEmbeddingProvider,
    HttpPassageReranker,
    OpenAICompatibleChatEndpoint,
    RetrievalModelRoute,
)
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import Base, build_engine, build_session_factory
from novel_agent.adapters.postgres.model_call_ledger import SqlModelCallLedger
from novel_agent.domain.artifacts import PlanRootRef
from novel_agent.domain.benchmark import BenchmarkBundle
from novel_agent.domain.ids import RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import (
    BudgetResolutionProfile,
    ModelCallPurpose,
    ModelRole,
)
from novel_agent.domain.stage2 import (
    BenchmarkInformationProfile,
    QualityRepairFeatureFlags,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.evidence_first_checkpoint_runner import EvidenceFirstCheckpointRunner
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_contract import build_public_checkpoint_case
from novel_agent.services.model_request_admission import ModelRequestAdmissionController
from novel_agent.services.teacher_forced_benchmark_e2e import TeacherForcedBenchmarkE2ERunner
from novel_agent.services.u4l0_canary import U4L0CanaryVariableLock, single_factor_diff

PACKAGE_MEDIA_TYPE = "application/vnd.novel-agent.writer-context-v2+json"
LEDGER_MEDIA_TYPE = "application/vnd.novel-agent.evidence-ledger-v2+json"
SCHEMA_VERSION = SchemaVersion("1.0.0")


def _native_models_module() -> Any:
    """Load the shared-checkout native-model owner for a worktree run."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "scripts" / "native_models.py"
        model_root = parent / "models" / "retrieval"
        if not candidate.is_file() or candidate == current or not model_root.is_dir():
            continue
        spec = importlib.util.spec_from_file_location("novel_agent_shared_native_models", candidate)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load shared native-model owner: {candidate}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    from scripts import native_models

    return native_models


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-project", type=Path, required=True)
    parser.add_argument("--checkpoint-index", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--case", choices=tuple(CASES), default="P002")
    parser.add_argument("--opensearch-url", default="http://127.0.0.1:9200")
    parser.add_argument("--embedding-url", default="http://127.0.0.1:8281/v1/embeddings")
    parser.add_argument("--reranker-url", default="http://127.0.0.1:8282/rerank")
    parser.add_argument("--model-base-url", default="http://127.0.0.1:8005/v1")
    parser.add_argument("--model", default="qwen38-27b-fp8")
    parser.add_argument("--model-max-output-tokens", type=int, default=8192)
    parser.add_argument("--planner-max-input-tokens", type=int, default=12000)
    parser.add_argument("--model-max-retries", type=int, default=0)
    parser.add_argument("--model-scheduling-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--thinking-token-budget", type=int, default=2048)
    parser.add_argument("--max-controller-decision-model-calls", type=int, default=8)
    parser.add_argument("--max-agentic-actions", type=int, default=32)
    parser.add_argument("--writer-token-budget", type=int, default=4000)
    parser.add_argument("--ledger-token-budget", type=int, default=12000)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--max-tool-calls", type=int, default=None)
    return parser


def _check_args(args: argparse.Namespace) -> None:
    if args.output_root.exists():
        raise FileExistsError("canary output identity already exists")
    if args.model_max_output_tokens < 256:
        raise ValueError("model output budget must be at least 256")
    if args.planner_max_input_tokens < 256:
        raise ValueError("Planner input budget must be at least 256")
    if args.thinking_token_budget < 0:
        raise ValueError("thinking token budget must be non-negative")
    if not 1 <= args.max_controller_decision_model_calls <= 8:
        raise ValueError("Controller model call budget must be between 1 and 8")
    if not 1 <= args.max_agentic_actions <= 32:
        raise ValueError("agentic action budget must be between 1 and 32")
    if args.writer_token_budget < 1 or args.ledger_token_budget < 1:
        raise ValueError("writer and evidence ledger budgets must be positive")
    if args.max_candidates < 1 or args.max_candidates > 100:
        raise ValueError("candidate budget must be between 1 and 100")
    if args.max_tool_calls is not None and args.max_tool_calls < 1:
        raise ValueError("tool-call budget must be positive")


def _locks() -> tuple[tuple[str, U4L0CanaryVariableLock, U4L0CanaryVariableLock], ...]:
    baseline = U4L0CanaryVariableLock(
        budget_profile=BudgetResolutionProfile.CANARY,
        controller_context_level="C0",
        planner_context_level="P0",
        thinking_enabled=False,
    )
    controller = baseline.model_copy(update={"controller_context_level": "C1+C2"})
    planner = controller.model_copy(update={"planner_context_level": "P0+P1"})
    thinking = planner.model_copy(update={"thinking_enabled": True})
    return (
        ("controller", baseline, controller),
        ("planner", controller, planner),
        ("thinking", planner, thinking),
    )


def _load_model_identity(base_url: str, expected_model: str) -> dict[str, Any]:
    import httpx

    response = httpx.get(f"{base_url.rstrip('/')}/models", timeout=8.0)
    response.raise_for_status()
    payload = response.json()
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ValueError("model endpoint returned no model list")
    match = next(
        (item for item in models if isinstance(item, dict) and item.get("id") == expected_model),
        None,
    )
    if match is None:
        raise ValueError(f"model endpoint does not expose requested model: {expected_model}")
    return {
        "id": match.get("id"),
        "owned_by": match.get("owned_by"),
        "root": match.get("root"),
        "max_model_len": match.get("max_model_len"),
    }


def _build_frozen_case(
    *,
    args: argparse.Namespace,
    source_engine: Any,
    source_r1: Any,
    source_repository: ArtifactRepository,
    bundle: BenchmarkBundle,
    search_index: OpenSearchIndex,
    embedder: HttpEmbeddingProvider,
    reranker: HttpPassageReranker,
    checkpoint_inputs: dict[str, tuple[int, str, Any]] | None,
) -> dict[str, Any]:
    short = args.case
    spec = dict(CASES[short])
    case = next(item for item in bundle.case_manifests if item.case_id.root == spec["case_id"])
    comparison_ref = None
    if checkpoint_inputs is not None:
        indexed = checkpoint_inputs.get(case.case_id.root)
        if indexed is None:
            raise ValueError(f"checkpoint index lacks requested case: {short}")
        chapter, indexed_commit, comparison_ref = indexed
        if chapter != int(spec["chapter"]):
            raise ValueError(f"checkpoint chapter disagrees with benchmark case: {short}")
        spec["commit"] = indexed_commit
    commit, world, text, plan, frozen_needs, planner = _checkpoint_state(
        source_repository,
        source_engine,
        spec,
        args.source_project.resolve(),
        bundle,
        comparison_ref,
    )
    basis = _select_case_basis(
        short_case=short,
        repair_case="P005",
        repair=None,
        source_engine=source_engine,
        source_r1=source_r1,
        source_project_id=case.project_id,
        source_commit=commit,
        source_world=world,
        source_text=text,
        source_plan=plan,
    )
    backend_bundle = _frozen_backend_bundle(
        engine=basis.engine,
        search_index=search_index,
        embedder=embedder,
        reranker=reranker,
        r1=basis.r1,
        project_id=basis.project_id,
        source_commit=basis.commit,
    )
    planning_context = next(
        context
        for context in bundle.planning_contexts
        if context.source_hash == case.planning_context_hash
    )
    if case.information_profile is not BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED:
        raise ValueError("U4-L0 canary requires the APC profile")
    plan_bytes = basis.plan.model_dump_json().encode("utf-8")
    public = build_public_checkpoint_case(
        case_id=case.case_id,
        project_id=case.project_id,
        target_range=case.target_range,
        history_range=case.history_range,
        information_profile=case.information_profile,
        plan_root_ref=PlanRootRef(
            artifact_id=basis.plan.root_hash,
            media_type="application/vnd.novel-agent.plan-root+json",
            byte_length=len(plan_bytes),
            schema_version=basis.plan.schema_version,
        ),
        task_intent=case.task_intent,
        planning_context_ref=case.planning_context_ref,
        planning_context_hash=case.planning_context_hash,
    )
    return {
        "case": case,
        "commit": basis.commit,
        "world": basis.world,
        "text": basis.text,
        "plan": basis.plan,
        "needs": frozen_needs,
        "planner": planner,
        "public": public,
        "backend": backend_bundle,
        "r1": basis.r1,
        "planning_context": planning_context,
    }


def _arm_report(
    *,
    name: str,
    lock: U4L0CanaryVariableLock,
    args: argparse.Namespace,
    case_data: dict[str, Any],
    output_repository: ArtifactRepository,
    model_identity: dict[str, Any],
) -> dict[str, Any]:
    arm_root = args.output_root / "arms" / name
    arm_root.mkdir(parents=True, exist_ok=False)
    ledger_engine = build_engine(f"sqlite:///{(arm_root / 'model_calls.sqlite3').resolve()}")
    Base.metadata.create_all(ledger_engine)
    ledger = SqlModelCallLedger(build_session_factory(ledger_engine))
    admission = ModelRequestAdmissionController(
        endpoint_request_limit=1,
        model_sequence_limit=131_072,
    )
    thinking_on = lock.thinking_enabled
    endpoint = OpenAICompatibleChatEndpoint(
        base_url=args.model_base_url,
        model=args.model,
        max_output_tokens=args.model_max_output_tokens,
        max_retries=args.model_max_retries,
        temperature=0.0,
    )
    harness = TeacherForcedBenchmarkE2ERunner.build_model_harness(
        endpoint,
        quality_repair_flags=QualityRepairFeatureFlags(
            max_controller_decision_model_calls=args.max_controller_decision_model_calls,
            max_agentic_actions=args.max_agentic_actions,
        ),
        admission_controller=admission,
        scheduling_timeout_seconds=args.model_scheduling_timeout_seconds,
        provider_thinking_on=thinking_on,
        thinking_token_budget=args.thinking_token_budget,
        call_ledger=ledger,
        raw_artifacts=output_repository,
    )
    runner = EvidenceFirstCheckpointRunner(
        writer_token_budget=args.writer_token_budget,
        evidence_ledger_token_budget=args.ledger_token_budget,
        max_candidates=args.max_candidates,
        max_tool_calls=args.max_tool_calls,
        artifact_writer=lambda payload, media_type: output_repository.put(
            payload, media_type, SCHEMA_VERSION
        ),
        graph_receipt_validator=case_data["r1"].validate_graph_path_receipts,
        planner_gateway=harness.gateway,
        controller_policy_factory=harness.controller_request_factory,
        require_model_decisions=False,
        planner_model_decisions=lock.planner_context_level == "P0+P1",
        controller_model_decisions=lock.controller_context_level == "C1+C2",
        semantic_judge_model_decisions=False,
        planner_max_output_tokens=args.model_max_output_tokens,
        planner_max_input_tokens=args.planner_max_input_tokens,
        thinking_enabled=(None if thinking_on else False),
        thinking_token_budget=(args.thinking_token_budget if thinking_on else None),
    )
    run_id = RunId(f"run.u4l0.{args.experiment_id}.{name}"[:128])
    fingerprint = content_id(
        {
            "owner": "u4l0-evidence-first",
            "case_id": case_data["case"].case_id.root,
            "basis_commit": case_data["commit"].root,
            "snapshot_id": case_data["backend"].attestation.capability.snapshot_id.root,
            "model": args.model,
            "model_base_url": args.model_base_url,
            "temperature": 0.0,
            "seed": None,
            "body_output_tokens": args.model_max_output_tokens,
            "writer_token_budget": args.writer_token_budget,
            "ledger_token_budget": args.ledger_token_budget,
            "max_candidates": args.max_candidates,
            "max_tool_calls": args.max_tool_calls,
            "lock": lock.model_dump(mode="json"),
        }
    )
    result = runner.run(
        case_id=case_data["case"].project_id,
        task=case_data["public"].task_contract,
        world=case_data["world"],
        text=case_data["text"],
        plan=case_data["plan"],
        base_commit=case_data["commit"],
        snapshot_id=case_data["backend"].attestation.capability.snapshot_id,
        planning_context=case_data["planning_context"],
        frozen_planner_artifact=case_data["planner"],
        frozen_needs=case_data["needs"],
        backend_bundle=case_data["backend"],
        fingerprint=fingerprint,
        run_id=StableId(run_id.root),
    )
    package = result.assembly.package
    package_ref = output_repository.put(
        canonical_json_bytes(package.model_dump(mode="json")),
        PACKAGE_MEDIA_TYPE,
        SCHEMA_VERSION,
    )
    ledger_ref = output_repository.put(
        canonical_json_bytes(result.assembly.evidence_ledger.model_dump(mode="json")),
        LEDGER_MEDIA_TYPE,
        SCHEMA_VERSION,
    )
    request_ids = {record.request_id for record in harness.gateway.call_records}
    if result.need_generation is not None and result.need_generation.planner_artifact is not None:
        request_ids.update(
            attempt.request_id for attempt in result.need_generation.planner_artifact.attempts
        )
        request_ids.update(
            request_id
            for audit in result.need_generation.planner_artifact.coverage_audits
            for request_id in audit.request_ids
        )
    request_ids.update(
        request_id
        for receipt in result.controller_receipts
        for request_id in receipt.model_call_ids
    )
    entries = tuple(
        entry
        for entry in (
            ledger.load(request_id)
            for request_id in sorted(request_ids, key=lambda item: item.root)
        )
        if entry is not None
    )
    completed = tuple(entry for entry in entries if entry.call_record is not None)
    completed_calls = tuple(entry.call_record for entry in entries if entry.call_record is not None)
    model_calls = tuple(
        {
            "request_id": entry.request_id.root,
            "ledger_status": entry.status.value,
            "logical_phase": entry.logical_phase,
            "effective_budget": entry.effective_budget.model_dump(mode="json"),
            "reasoning_included_in_completion_tokens": (
                entry.reasoning_included_in_completion_tokens
            ),
            "raw_artifact_ref": (
                None
                if entry.raw_artifact_ref is None
                else entry.raw_artifact_ref.model_dump(mode="json")
            ),
            "call_record": (
                None if entry.call_record is None else entry.call_record.model_dump(mode="json")
            ),
        }
        for entry in entries
    )
    usage = {
        "input_tokens": sum(call.usage.input_tokens for call in completed_calls),
        "output_tokens": sum(call.usage.output_tokens for call in completed_calls),
        "reasoning_tokens": sum(call.usage.reasoning_tokens for call in completed_calls),
        "latency_ms": sum(call.latency_ms for call in completed_calls),
    }
    decisions = list(result.controller_decisions)
    report = {
        "arm_id": name,
        "lock": lock.model_dump(mode="json"),
        "status": "COMPLETED",
        "case_id": case_data["case"].case_id.root,
        "checkpoint_chapter": int(CASES[args.case]["chapter"]),
        "basis": {
            "commit": case_data["commit"].root,
            "snapshot_id": case_data["backend"].attestation.capability.snapshot_id.root,
            "comparison_fingerprint": fingerprint.root,
        },
        "model_identity": model_identity,
        "model_config": {
            "base_url": args.model_base_url,
            "model": args.model,
            "revision": args.model,
            "temperature": 0.0,
            "seed": None,
            "body_output_tokens": args.model_max_output_tokens,
            "thinking_token_budget": args.thinking_token_budget,
        },
        "planner": {
            "model_calls": result.need_planner_model_call_count,
            "coverage_audit_model_calls": result.planner_coverage_audit_model_call_count,
            "fallback_used": result.planner_fallback_used,
            "fallback_reason": (
                None
                if result.need_generation is None
                else result.need_generation.planner_fallback_reason
            ),
            "need_count": len(result.needs),
            "need_ids": [need.need_id.root for need in result.needs],
        },
        "controller": {
            "model_calls": result.controller_model_call_count,
            "repairs": result.controller_repair_count,
            "stop_reason": result.stop_reason,
            "decisions": decisions,
            "context_telemetry": [
                receipt.context_telemetry for receipt in result.controller_receipts
            ],
            "mandatory_facet_closure": result.assembly.mandatory_facet_closure,
            "unclosed_mandatory_need_facets": [
                item.root for item in package.unclosed_mandatory_need_facets
            ],
            "unnecessary_tool_calls": {
                "count": 0,
                "definition": (
                    "calls after a typed sufficient stop; the bounded loop stops before issuing one"
                ),
            },
        },
        "usage": usage,
        "timeouts": sum(
            entry.status.value in {"uncertain", "transport_exhausted"} for entry in entries
        ),
        "length_failures": sum(
            bool(entry.transport_error_type and "Length" in entry.transport_error_type)
            for entry in entries
        ),
        "future_leakage_count": result.future_leakage_count,
        "raw_and_ledger": {
            "ledger_database": str((arm_root / "model_calls.sqlite3").resolve()),
            "ledger_request_ids": [entry.request_id.root for entry in entries],
            "raw_artifact_refs_complete": all(
                entry.raw_artifact_ref is not None for entry in completed
            ),
            "model_calls": model_calls,
        },
        "package_refs": {
            "package": package_ref.model_dump(mode="json"),
            "evidence_ledger": ledger_ref.model_dump(mode="json"),
        },
        "result": {
            "assembly_status": result.assembly.status.value,
            "semantic_status": package.semantic_status,
            "retrieval_call_count": result.retrieval_call_count,
            "writer_item_count": package.budget_report.item_count,
            "evidence_item_count": package.budget_report.evidence_item_count,
            "gap_item_count": package.budget_report.gap_item_count,
        },
    }
    (arm_root / "arm_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ledger_engine.dispose()
    return report


def _pair_report(
    *,
    factor: str,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    baseline_lock = U4L0CanaryVariableLock.model_validate_json(json.dumps(baseline["lock"]))
    candidate_lock = U4L0CanaryVariableLock.model_validate_json(json.dumps(candidate["lock"]))
    changed = single_factor_diff(baseline_lock, candidate_lock)
    expected_lock_field = {
        "controller": "controller_context_level",
        "planner": "planner_context_level",
        "thinking": "thinking_enabled",
    }.get(factor, factor)
    same_basis = (
        baseline["basis"]["commit"] == candidate["basis"]["commit"]
        and baseline["basis"]["snapshot_id"] == candidate["basis"]["snapshot_id"]
    )
    same_model = baseline["model_identity"] == candidate["model_identity"]
    leakage_free = baseline["future_leakage_count"] == 0 and candidate["future_leakage_count"] == 0
    no_fallback = (
        not baseline["planner"]["fallback_used"] and not candidate["planner"]["fallback_used"]
    )
    raw_complete = (
        baseline["raw_and_ledger"]["raw_artifact_refs_complete"]
        and candidate["raw_and_ledger"]["raw_artifact_refs_complete"]
    )
    blockers = []
    if changed != expected_lock_field:
        blockers.append(f"expected_factor={expected_lock_field};observed_factor={changed}")
    if not same_basis:
        blockers.append("basis_mismatch")
    if not same_model:
        blockers.append("model_identity_mismatch")
    if not leakage_free:
        blockers.append("future_leakage")
    if not no_fallback:
        blockers.append("planner_fallback")
    if not raw_complete:
        blockers.append("raw_artifact_missing")
    if baseline["status"] != "COMPLETED" or candidate["status"] != "COMPLETED":
        blockers.append("arm_not_completed")
    return {
        "factor": factor,
        "expected_lock_field": expected_lock_field,
        "changed_factor": changed,
        "baseline_arm": baseline["arm_id"],
        "candidate_arm": candidate["arm_id"],
        "same_frozen_basis": same_basis,
        "same_model_identity": same_model,
        "leakage_free": leakage_free,
        "raw_and_ledger_complete": raw_complete,
        "planner_fallback_free": no_fallback,
        "comparable": not blockers,
        "blockers": blockers,
    }


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    _check_args(args)
    model_identity = _load_model_identity(args.model_base_url, args.model)
    source_project = args.source_project.resolve()
    if not (source_project / "objects").is_dir():
        raise ValueError("source project objects directory is missing")
    database_url = _loopback_postgres_url(args.database_url)
    embedding_url = _loopback_http_url(args.embedding_url, "embedding")
    reranker_url = _loopback_http_url(args.reranker_url, "reranker")
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True)
    output_repository = ArtifactRepository(FilesystemObjectStore(output_root / "objects"))
    source_engine = build_engine(database_url)
    search_client = None
    ledger_engines: list[Any] = []
    started_at = datetime.now(UTC).isoformat()
    try:
        bundle = HumanBenchmarkCompiler().compile(
            Path("benchmarks/private/ztj_memory_pilot_v0.1").resolve()
        )
        source_repository = ArtifactRepository(FilesystemObjectStore(source_project / "objects"))
        source_factory = build_session_factory(source_engine)
        from novel_agent.services.r1 import R1WorldRepository

        source_r1 = R1WorldRepository(source_factory)
        checkpoint_inputs = (
            _load_checkpoint_index(args.checkpoint_index.resolve())
            if args.checkpoint_index is not None
            else None
        )
        native_models = _native_models_module()
        model_lock = native_models.load_model_lock()
        embedding_model = model_lock.models["embedding"]
        reranker_model = model_lock.models["reranker"]
        native_models.assert_model_service(embedding_model)
        native_models.assert_model_service(reranker_model)
        run_id = RunId(f"run.u4l0.retrieval.{args.experiment_id}"[:128])
        embedder = HttpEmbeddingProvider(
            RetrievalModelRoute(
                endpoint=embedding_url,
                model=embedding_model.model_id,
                revision=embedding_model.revision,
                runtime_fingerprint=embedding_model.runtime_fingerprint,
                run_id=run_id,
                task_id=TaskId("task.u4l0.embedding"),
                trace_id=f"trace.{run_id.root}.embedding",
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
                task_id=TaskId("task.u4l0.reranker"),
                trace_id=f"trace.{run_id.root}.reranker",
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
        case_data = _build_frozen_case(
            args=args,
            source_engine=source_engine,
            source_r1=source_r1,
            source_repository=source_repository,
            bundle=bundle,
            search_index=search_index,
            embedder=embedder,
            reranker=reranker,
            checkpoint_inputs=checkpoint_inputs,
        )
        case_data["planning_context"] = next(
            context
            for context in bundle.planning_contexts
            if context.source_hash == case_data["case"].planning_context_hash
        )
        arms: dict[str, dict[str, Any]] = {}
        for factor, baseline_lock, candidate_lock in _locks():
            baseline_name = f"{factor}-baseline"
            candidate_name = f"{factor}-candidate"
            print(f"running {factor}: {baseline_name}", flush=True)
            arms[baseline_name] = _arm_report(
                name=baseline_name,
                lock=baseline_lock,
                args=args,
                case_data=case_data,
                output_repository=output_repository,
                model_identity=model_identity,
            )
            print(f"running {factor}: {candidate_name}", flush=True)
            arms[candidate_name] = _arm_report(
                name=candidate_name,
                lock=candidate_lock,
                args=args,
                case_data=case_data,
                output_repository=output_repository,
                model_identity=model_identity,
            )
        pairs = [
            _pair_report(
                factor=factor,
                baseline=arms[f"{factor}-baseline"],
                candidate=arms[f"{factor}-candidate"],
            )
            for factor, _baseline, _candidate in _locks()
        ]
        report = {
            "report_version": "u4l0-real-model-canary.v1",
            "report_owner": "evidence_first_checkpoint_runner+stage2_paired_comparison",
            "experiment_id": args.experiment_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "case": {
                "short": args.case,
                "case_id": case_data["case"].case_id.root,
                "checkpoint_chapter": int(CASES[args.case]["chapter"]),
                "basis_commit": case_data["commit"].root,
                "snapshot_id": case_data["backend"].attestation.capability.snapshot_id.root,
            },
            "pre_registered": {
                "model": args.model,
                "model_base_url": args.model_base_url,
                "temperature": 0.0,
                "seed": None,
                "body_output_tokens": args.model_max_output_tokens,
                "repetitions": 1,
                "interaction_2x2_registered": False,
                "semantic_judge_calls": 0,
            },
            "model_identity": model_identity,
            "pairs": pairs,
            "arms": arms,
            "gate_status": "PASS" if all(pair["comparable"] for pair in pairs) else "INCOMPLETE",
            "gate_blockers": [blocker for pair in pairs for blocker in pair["blockers"]],
        }
        (output_root / "canary_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"U4-L0 canary: status={report['gate_status']} "
            f"pairs={len(pairs)} output={output_root / 'canary_report.json'}",
            flush=True,
        )
        return 0 if report["gate_status"] == "PASS" else 2
    finally:
        if search_client is not None:
            search_client.close()
        for engine in ledger_engines:
            engine.dispose()
        source_engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
