"""Compile V0.5 public checkpoint identities into one Writer readout manifest."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import StableId
from novel_agent.domain.model_calls import (
    BudgetResolutionProfile,
    EffectiveBudgetResult,
    ModelRole,
)
from novel_agent.domain.production_assembly import ResolvedProductionAssemblyAttestation
from novel_agent.domain.v05_readout import (
    U4L0CanaryVariableLock,
    V05CampaignExecutionFreeze,
    V05CampaignPhase,
    V05CampaignReportFreeze,
    V05CampaignSourceIdentity,
    V05CampaignStatus,
    V05HistoryAccess,
    V05JudgeRuntimeFreeze,
    V05ReadoutCampaignManifest,
    V05ReadoutManifest,
    V05ReadoutTaskIdentity,
    V05ReadoutTrack,
    V05RepresentativeTaskCoverage,
    V05RuntimeVariableFreeze,
    V05WriterRuntimeFreeze,
    map_v05_history_access,
)
from novel_agent.services.model_gateway import ModelGateway, ModelRoutingError

EXPECTED_QA_TASKS = 51
EXPECTED_CONTEXT_TASKS = 30
V05_CONTEXT_PROFILES: tuple[V05HistoryAccess, ...] = (
    V05HistoryAccess.HISTORY_ONLY,
    V05HistoryAccess.AUTHOR_PLAN_CONDITIONED,
)


class V05ReadoutManifestError(ValueError):
    """V0.5 runner identities are incomplete, duplicated, or mistimed."""


def freeze_v05_readout_campaign(
    *,
    readout_manifest: V05ReadoutManifest,
    campaign_id: StableId,
    phase: V05CampaignPhase,
    status: V05CampaignStatus,
    source: V05CampaignSourceIdentity,
    writer: V05WriterRuntimeFreeze,
    judges: V05JudgeRuntimeFreeze,
    execution: V05CampaignExecutionFreeze,
    report: V05CampaignReportFreeze,
    runtime: V05RuntimeVariableFreeze | None = None,
    effective_budget: EffectiveBudgetResult | None = None,
    provider_reasoning_included_in_completion_tokens: bool | None = None,
    controller_level: str = "C1+C2",
    planner_level: str = "P0+P1",
    thinking_enabled: bool | None = None,
    budget_profile: BudgetResolutionProfile = BudgetResolutionProfile.CANARY,
    canary_lock: U4L0CanaryVariableLock | None = None,
    representative_task_ids: Sequence[StableId] = (),
    representative_task_coverage: V05RepresentativeTaskCoverage | None = None,
) -> V05ReadoutCampaignManifest:
    """Create one immutable U4-S0 campaign manifest before any Writer call."""

    validate_v05_seed_identities(readout_manifest)
    resolved_budget = effective_budget or (
        runtime.effective_budget if runtime is not None else None
    )
    if resolved_budget is None:
        raise V05ReadoutManifestError(
            "READY_TO_FREEZE: U2 must provide one verifiable EffectiveBudgetResult"
        )
    if runtime is None:
        if provider_reasoning_included_in_completion_tokens is None:
            raise V05ReadoutManifestError(
                "READY_TO_FREEZE: provider reasoning billing policy is unavailable"
            )
        if thinking_enabled is None:
            thinking_enabled = resolved_budget.thinking_budget > 0
        runtime = V05RuntimeVariableFreeze.from_effective_budget(
            resolved_budget,
            budget_profile=budget_profile,
            controller_level=controller_level,  # type: ignore[arg-type]
            planner_level=planner_level,  # type: ignore[arg-type]
            thinking_enabled=thinking_enabled,
            provider_reasoning_included_in_completion_tokens=(
                provider_reasoning_included_in_completion_tokens
            ),
        )
    elif runtime.effective_budget != resolved_budget:
        raise V05ReadoutManifestError(
            "runtime variables must reference the exact EffectiveBudgetResult supplied by U2"
        )
    if representative_task_coverage is None:
        raise V05ReadoutManifestError(
            "READY_TO_FREEZE: representative task coverage has not been preselected"
        )
    representative_ids = tuple(representative_task_ids)
    missing_coverage_ids = set(representative_task_coverage.task_ids()) - set(representative_ids)
    if missing_coverage_ids:
        raise V05ReadoutManifestError(
            "representative task ids omit coverage roles: "
            + ", ".join(sorted(item.root for item in missing_coverage_ids))
        )
    frozen_canary_lock = canary_lock or U4L0CanaryVariableLock(
        budget_profile=runtime.budget_profile,
        controller_context_level=runtime.controller_level,
        planner_context_level=runtime.planner_level,
        thinking_enabled=runtime.thinking_enabled,
    )
    if readout_manifest.canary_lock is None:
        readout_manifest = readout_manifest.model_copy(update={"canary_lock": frozen_canary_lock})
    elif readout_manifest.canary_lock != frozen_canary_lock:
        raise V05ReadoutManifestError("readout and campaign canary locks must be identical")
    if report.canary_lock is None:
        report = report.model_copy(update={"canary_lock": frozen_canary_lock})
    elif report.canary_lock != frozen_canary_lock:
        raise V05ReadoutManifestError("report and campaign canary locks must be identical")
    return V05ReadoutCampaignManifest(
        campaign_id=campaign_id,
        benchmark_id=readout_manifest.benchmark_id,
        benchmark_version=readout_manifest.version,
        phase=phase,
        status=status,
        readout_manifest=readout_manifest,
        source=source,
        writer=writer,
        judges=judges,
        runtime=runtime,
        execution=execution,
        report=report,
        canary_lock=frozen_canary_lock,
        representative_task_ids=representative_ids,
        representative_task_coverage=representative_task_coverage,
    )


def load_effective_budget_result(path: Path) -> EffectiveBudgetResult:
    """Load a strict, pre-resolved budget artifact; no fields are recomputed here."""

    try:
        return EffectiveBudgetResult.model_validate_json(path.read_bytes(), strict=True)
    except ValueError as error:
        raise V05ReadoutManifestError(f"invalid EffectiveBudgetResult artifact: {path}") from error


def load_production_attestation(path: Path) -> ResolvedProductionAssemblyAttestation:
    """Load the immutable U2 startup facts used by the offline freeze CLI."""

    try:
        return ResolvedProductionAssemblyAttestation.model_validate_json(
            path.read_bytes(), strict=True
        )
    except ValueError as error:
        raise V05ReadoutManifestError(
            f"invalid ResolvedProductionAssemblyAttestation artifact: {path}"
        ) from error


def runtime_variables_from_effective_budget(
    effective_budget: EffectiveBudgetResult,
    *,
    controller_level: str,
    planner_level: str,
    thinking_enabled: bool,
    provider_reasoning_included_in_completion_tokens: bool,
) -> V05RuntimeVariableFreeze:
    """Expose the one canonical budget to the campaign domain without hand arithmetic."""

    return V05RuntimeVariableFreeze.from_effective_budget(
        effective_budget,
        controller_level=controller_level,  # type: ignore[arg-type]
        planner_level=planner_level,  # type: ignore[arg-type]
        thinking_enabled=thinking_enabled,
        provider_reasoning_included_in_completion_tokens=(
            provider_reasoning_included_in_completion_tokens
        ),
    )


def derive_v05_writer_runtime_freeze(
    *,
    gateway: ModelGateway,
    attestation: ResolvedProductionAssemblyAttestation,
    effective_budget: EffectiveBudgetResult,
    prompt_ref: ArtifactRef,
    response_schema_ref: ArtifactRef,
    request_role: str,
    temperature: float,
    seed: int | None,
    seed_capability: str,
    evidence_token_budget: int,
    concurrency: int,
) -> V05WriterRuntimeFreeze:
    """Derive Writer identity and provider limits from the registered production gateway."""

    try:
        endpoint, model, revision = gateway.endpoint_runtime_identity(ModelRole.IMPLEMENTATION)
        limits = gateway.endpoint_budget_limits(ModelRole.IMPLEMENTATION)
    except ModelRoutingError as error:
        raise V05ReadoutManifestError(
            "READY_TO_FREEZE: registered Writer endpoint identity or limits are unavailable"
        ) from error
    registered = tuple(
        item for item in attestation.endpoints if item.role == ModelRole.IMPLEMENTATION.value
    )
    if len(registered) != 1:
        raise V05ReadoutManifestError(
            "READY_TO_FREEZE: attestation must contain one registered implementation endpoint"
        )
    attested = registered[0]
    if attested.revision is None:
        raise V05ReadoutManifestError(
            "READY_TO_FREEZE: registered Writer endpoint revision is missing"
        )
    if (attested.endpoint_name, attested.model_name, attested.revision) != (
        endpoint,
        model,
        revision,
    ):
        raise V05ReadoutManifestError(
            "registered Writer endpoint identity does not match the resolved assembly attestation"
        )
    if attestation.sequence_limit != limits.sequence_limit:
        raise V05ReadoutManifestError(
            "registered Writer sequence limit does not match the resolved assembly attestation"
        )
    if limits.output_limit is None or attestation.output_limit != limits.output_limit:
        raise V05ReadoutManifestError(
            "READY_TO_FREEZE: registered Writer output limit is not verifiable"
        )
    if effective_budget.context_limit != limits.sequence_limit:
        raise V05ReadoutManifestError(
            "resolved budget context limit does not match the registered Writer endpoint"
        )
    if effective_budget.total_output_budget > limits.output_limit:
        raise V05ReadoutManifestError(
            "resolved provider total output reserve exceeds the registered Writer limit"
        )
    return _writer_runtime_from_attested_facts(
        attestation=attestation,
        effective_budget=effective_budget,
        prompt_ref=prompt_ref,
        response_schema_ref=response_schema_ref,
        request_role=request_role,
        temperature=temperature,
        seed=seed,
        seed_capability=seed_capability,
        evidence_token_budget=evidence_token_budget,
        concurrency=concurrency,
    )


def derive_v05_writer_runtime_from_attestation(
    *,
    attestation: ResolvedProductionAssemblyAttestation,
    effective_budget: EffectiveBudgetResult,
    prompt_ref: ArtifactRef,
    response_schema_ref: ArtifactRef,
    request_role: str,
    temperature: float,
    seed: int | None,
    seed_capability: str,
    evidence_token_budget: int,
    concurrency: int,
) -> V05WriterRuntimeFreeze:
    """Offline counterpart used only with a serialized U2 registry attestation."""

    return _writer_runtime_from_attested_facts(
        attestation=attestation,
        effective_budget=effective_budget,
        prompt_ref=prompt_ref,
        response_schema_ref=response_schema_ref,
        request_role=request_role,
        temperature=temperature,
        seed=seed,
        seed_capability=seed_capability,
        evidence_token_budget=evidence_token_budget,
        concurrency=concurrency,
    )


def _writer_runtime_from_attested_facts(
    *,
    attestation: ResolvedProductionAssemblyAttestation,
    effective_budget: EffectiveBudgetResult,
    prompt_ref: ArtifactRef,
    response_schema_ref: ArtifactRef,
    request_role: str,
    temperature: float,
    seed: int | None,
    seed_capability: str,
    evidence_token_budget: int,
    concurrency: int,
) -> V05WriterRuntimeFreeze:
    registered = tuple(
        item for item in attestation.endpoints if item.role == ModelRole.IMPLEMENTATION.value
    )
    if len(registered) != 1:
        raise V05ReadoutManifestError(
            "READY_TO_FREEZE: attestation must contain one registered implementation endpoint"
        )
    attested = registered[0]
    if attested.revision is None:
        raise V05ReadoutManifestError(
            "READY_TO_FREEZE: registered Writer endpoint revision is missing"
        )
    if attestation.output_limit < effective_budget.total_output_budget:
        raise V05ReadoutManifestError(
            "resolved provider total output reserve exceeds the attested Writer limit"
        )
    if attestation.global_output_cap < effective_budget.total_output_budget or (
        attestation.safety_allowance_tokens is not None
        and attestation.safety_allowance_tokens != effective_budget.safety_allowance_tokens
    ):
        raise V05ReadoutManifestError(
            "resolved budget safety/output caps do not match the attested Writer policy"
        )
    if attestation.sequence_limit != effective_budget.context_limit:
        raise V05ReadoutManifestError(
            "resolved budget context limit does not match the attested Writer limit"
        )
    return V05WriterRuntimeFreeze(
        endpoint=attested.endpoint_name,
        model=attested.model_name,
        revision=attested.revision,
        prompt_ref=prompt_ref,
        response_schema_ref=response_schema_ref,
        request_role=request_role,
        temperature=temperature,
        seed=seed,
        seed_capability=seed_capability,  # type: ignore[arg-type]
        evidence_token_budget=evidence_token_budget,
        output_token_budget=effective_budget.body_output_budget,
        provider_total_output_budget=effective_budget.total_output_budget,
        concurrency=concurrency,
    )


def compile_v05_readout_manifest(
    *,
    benchmark_id: str,
    version: str,
    checkpoints: Sequence[Mapping[str, Any]],
    qa_questions: Sequence[Mapping[str, Any]],
    context_windows: Mapping[str, Sequence[int]],
    require_v05_seed_invariants: bool = True,
) -> V05ReadoutManifest:
    tasks: list[V05ReadoutTaskIdentity] = []
    windows = {str(key): value for key, value in context_windows.items()}
    for checkpoint in checkpoints:
        chapter = int(checkpoint["after_chapter"])
        checkpoint_id = StableId(str(checkpoint["checkpoint_id"]))
        tracks = tuple(checkpoint["tracks"])
        if V05ReadoutTrack.QA.value in tracks:
            matching = [
                question for question in qa_questions if int(question["checkpoint"]) == chapter
            ]
            if not matching:
                raise V05ReadoutManifestError(f"QA checkpoint {chapter} has no question identities")
            for question in matching:
                question_id = StableId(str(question["question_id"]))
                history_access = V05HistoryAccess.HISTORY_ONLY
                tasks.append(
                    V05ReadoutTaskIdentity(
                        task_id=StableId(f"task.v05.qa.{question_id.root}"),
                        track=V05ReadoutTrack.QA,
                        checkpoint_id=checkpoint_id,
                        checkpoint_chapter=chapter,
                        history_access=history_access,
                        information_profile=map_v05_history_access(history_access),
                        question_release="after_checkpoint_freeze",
                        question_id=question_id,
                    )
                )
        if V05ReadoutTrack.CONTEXT.value in tracks:
            window = windows.get(str(chapter))
            if window is None or len(window) != 2:
                raise V05ReadoutManifestError(
                    f"Context checkpoint {chapter} is missing a two-chapter target window"
                )
            start, end = int(window[0]), int(window[1])
            for history_access in V05_CONTEXT_PROFILES:
                tasks.append(
                    V05ReadoutTaskIdentity(
                        task_id=StableId(
                            f"task.v05.context.{checkpoint_id.root}."
                            f"{history_access.value.replace('_', '-')}"
                        ),
                        track=V05ReadoutTrack.CONTEXT,
                        checkpoint_id=checkpoint_id,
                        checkpoint_chapter=chapter,
                        history_access=history_access,
                        information_profile=map_v05_history_access(history_access),
                        target_chapter_start=start,
                        target_chapter_end=end,
                    )
                )
    manifest = V05ReadoutManifest(
        benchmark_id=benchmark_id,
        version=version,
        tasks=tuple(tasks),
    )
    if require_v05_seed_invariants:
        validate_v05_seed_identities(manifest)
    return manifest


def select_v05_representative_task_coverage(
    manifest: V05ReadoutManifest,
    *,
    bundle_root: Path,
) -> V05RepresentativeTaskCoverage:
    """Select deterministic representative ids from public identities and private labels only."""

    qa_tasks = sorted(
        (task for task in manifest.tasks if task.track is V05ReadoutTrack.QA),
        key=lambda task: (task.checkpoint_chapter, task.task_id.root),
    )
    context_tasks = sorted(
        (task for task in manifest.tasks if task.track is V05ReadoutTrack.CONTEXT),
        key=lambda task: (task.checkpoint_chapter, task.task_id.root),
    )
    if not qa_tasks or not context_tasks:
        raise V05ReadoutManifestError("representative coverage requires QA and Context identities")
    qa_chapters = sorted({task.checkpoint_chapter for task in qa_tasks})
    early_chapter = qa_chapters[0]
    middle_chapter = qa_chapters[len(qa_chapters) // 2]
    late_chapter = qa_chapters[-1]

    def first_qa(chapter: int) -> V05ReadoutTaskIdentity:
        return next(task for task in qa_tasks if task.checkpoint_chapter == chapter)

    legacy = next(
        (
            task
            for task in context_tasks
            if task.target_chapter_start is not None
            and task.target_chapter_end is not None
            and task.target_chapter_end - task.target_chapter_start + 1 == 5
        ),
        None,
    )
    long_window = next(
        (
            task
            for task in context_tasks
            if task.target_chapter_start is not None
            and task.target_chapter_end is not None
            and task.target_chapter_end - task.target_chapter_start + 1 == 20
        ),
        None,
    )
    if legacy is None or long_window is None:
        raise V05ReadoutManifestError(
            "representative coverage requires both five-chapter and twenty-chapter windows"
        )
    question_path = bundle_root / "annotations" / "track_a_seed.json"
    try:
        question_rows = json.loads(question_path.read_text(encoding="utf-8"))["questions"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise V05ReadoutManifestError(
            "READY_TO_FREEZE: Track A ability labels are unavailable for representative coverage"
        ) from error
    by_question_id = {
        str(row["question_id"]): row
        for row in question_rows
        if isinstance(row, Mapping) and "question_id" in row
    }
    unanswerable = next(
        (
            task
            for task in qa_tasks
            if task.question_id is not None
            and by_question_id.get(task.question_id.root, {}).get("answerability") == "unanswerable"
        ),
        None,
    )
    multi_hop = next(
        (
            task
            for task in qa_tasks
            if task.question_id is not None
            and "multi_hop" in str(by_question_id.get(task.question_id.root, {}).get("ability", ""))
        ),
        None,
    )
    if unanswerable is None or multi_hop is None:
        raise V05ReadoutManifestError(
            "READY_TO_FREEZE: unanswerable and multi-hop QA labels are unavailable"
        )
    return V05RepresentativeTaskCoverage(
        early_checkpoint_task_id=first_qa(early_chapter).task_id,
        mid_checkpoint_task_id=first_qa(middle_chapter).task_id,
        late_checkpoint_task_id=first_qa(late_chapter).task_id,
        legacy_five_chapter_window_task_id=legacy.task_id,
        twenty_chapter_window_task_id=long_window.task_id,
        unanswerable_qa_task_id=unanswerable.task_id,
        multi_hop_qa_task_id=multi_hop.task_id,
    )


def validate_v05_seed_identities(manifest: V05ReadoutManifest) -> None:
    qa = tuple(task for task in manifest.tasks if task.track is V05ReadoutTrack.QA)
    context = tuple(task for task in manifest.tasks if task.track is V05ReadoutTrack.CONTEXT)
    if any(task.checkpoint_chapter == 100 for task in qa):
        raise V05ReadoutManifestError("C100 must not carry a QA readout identity")
    if any(task.checkpoint_chapter == 300 for task in context):
        raise V05ReadoutManifestError("C300 must not carry a Context readout identity")
    if len(qa) != EXPECTED_QA_TASKS:
        raise V05ReadoutManifestError(
            f"V0.5 seed requires {EXPECTED_QA_TASKS} unique QA identities, got {len(qa)}"
        )
    if len(context) != EXPECTED_CONTEXT_TASKS:
        raise V05ReadoutManifestError(
            f"V0.5 seed requires {EXPECTED_CONTEXT_TASKS} unique Context identities, "
            f"got {len(context)}"
        )
    if any(task.checkpoint_chapter > 300 for task in manifest.tasks):
        raise V05ReadoutManifestError("V0.5 readout identities cannot target a future checkpoint")
    for task in context:
        if task.target_chapter_end is not None and task.target_chapter_end > 300:
            raise V05ReadoutManifestError("Context readout cannot target chapters after C300")


def load_v05_readout_manifest(bundle_root: Path) -> V05ReadoutManifest:
    """Load identities from a V0.5 bundle. Question text and Gold are ignored."""

    benchmark = json.loads((bundle_root / "benchmark.json").read_text(encoding="utf-8"))
    checkpoints = json.loads(
        (bundle_root / "public" / "checkpoints.json").read_text(encoding="utf-8")
    )
    questions_path = bundle_root / "annotations" / "track_a_seed.json"
    raw_questions = json.loads(questions_path.read_text(encoding="utf-8"))["questions"]
    qa_questions = tuple(
        {"question_id": item["question_id"], "checkpoint": item["checkpoint"]}
        for item in raw_questions
    )
    return compile_v05_readout_manifest(
        benchmark_id=str(benchmark["benchmark_id"]),
        version=str(benchmark["version"]),
        checkpoints=tuple(checkpoints["checkpoints"]),
        qa_questions=qa_questions,
        context_windows=benchmark["context_target_windows"],
    )


def load_v05_readout_campaign_manifest(path: Path) -> V05ReadoutCampaignManifest:
    """Load the immutable U4-S0 manifest without rebuilding or changing it."""

    try:
        manifest = V05ReadoutCampaignManifest.model_validate_json(path.read_bytes(), strict=True)
        validate_v05_seed_identities(manifest.readout_manifest)
        return manifest
    except ValueError as error:
        raise V05ReadoutManifestError(f"invalid frozen V0.5 campaign manifest: {path}") from error
