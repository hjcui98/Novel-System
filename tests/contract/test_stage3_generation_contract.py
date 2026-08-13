from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from novel_agent.domain import (
    agent_context,
    editorial,
    generation,
    stage3_evaluation,
    stage3_loop_evaluation,
    writing_loop,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.generation import (
    ContinuationBoundary,
    DeclaredMemoryHint,
    DraftArtifact,
    MemoryHintChangeKind,
    RewriteDirective,
    RewriteScope,
    WriterAdvisoryFinding,
    WriterArtifactBasis,
    WriterBudget,
    WriterContextSnapshot,
    WriterDraftPayload,
    WriterExecutionMetrics,
    WriterExecutionResult,
    WriterFailureCode,
    WriterInputTaint,
    WriterInvocation,
    WriterRuntimeFingerprints,
    WriterShadowManifest,
    WriterSidecar,
    WriterSourceBinding,
    WriterTerminalStatus,
    WritingLengthPolicy,
    WritingTaskContract,
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
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    ContractRef,
    ControllerPromotionDecision,
    ExecutionStatus,
    FutureIsolationAttestation,
    Stage2GateReport,
    Stage2GateVerdict,
)

REPOSITORY_ROOT = Path(__file__).parents[2]
VERSION = SchemaVersion("1.0.0")
NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _hash(digit: str) -> ArtifactId:
    return ArtifactId("sha256:" + digit * 64)


def _artifact(digit: str, media_type: str = "application/json") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=_hash(digit),
        media_type=media_type,
        byte_length=1,
        schema_version=VERSION,
    )


def _replace(model: DomainModel, **updates: Any) -> Any:
    values = {name: getattr(model, name) for name in type(model).model_fields}
    return type(model)(**(values | updates))


def _attestation(
    *,
    canonical: tuple[StableId, ...] = (StableId("source.writer"),),
    evaluator: tuple[StableId, ...] = (StableId("source.evaluator"),),
) -> FutureIsolationAttestation:
    return FutureIsolationAttestation(
        attestation_id=StableId("attestation.writer"),
        checkpoint_chapter=20,
        canonical_source_ids=canonical,
        evaluator_only_source_ids=evaluator,
        passed=True,
        configuration_fingerprint=_hash("9"),
    )


def _source(
    *,
    source_id: StableId | None = None,
    artifact: ArtifactRef | None = None,
) -> WriterSourceBinding:
    return WriterSourceBinding(
        source_id=source_id or StableId("source.writer"),
        source_artifact=artifact or _artifact("5"),
    )


def _basis(
    *,
    attestation: FutureIsolationAttestation | None = None,
    sources: tuple[WriterSourceBinding, ...] | None = None,
) -> WriterArtifactBasis:
    return WriterArtifactBasis(
        project_id=ProjectId("project.writer"),
        base_commit=CommitId(_hash("a").root),
        snapshot_id=StableId("snapshot.writer"),
        context_id=StableId("context.writer"),
        context_artifact=_artifact("1"),
        context_fingerprint=_hash("1"),
        writing_contract_artifact=_artifact("2"),
        plan_artifact=_artifact("3"),
        project_profile_artifact=_artifact("4"),
        configuration_fingerprint=_hash("b"),
        model_configuration_fingerprint=_hash("c"),
        future_isolation_attestation=attestation or _attestation(),
        source_artifacts=sources if sources is not None else (_source(),),
    )


def _context(
    *,
    base_commit: CommitId | None = None,
    snapshot_id: StableId | None = None,
    context_id: StableId | None = None,
) -> WriterContextSnapshot:
    return WriterContextSnapshot(
        context_id=context_id or StableId("context.writer"),
        base_commit=base_commit or CommitId(_hash("a").root),
        snapshot_id=snapshot_id or StableId("snapshot.writer"),
        task_contract="trusted Writer task",
        budget_report={"token_budget": 100, "mandatory_tokens": 10, "optional_tokens": 5},
    )


def _writing_task(
    *,
    length_policy: WritingLengthPolicy | None = None,
) -> WritingTaskContract:
    return WritingTaskContract(
        contract_id=StableId("writing-task.writer"),
        target_chapter=21,
        target_scenes=(StableId("scene.writer.1"),),
        pov="Lin",
        narrative_person="third",
        chapter_goal="Enter the tower",
        active_plan_obligations=(StableId("obligation.writer.1"),),
        length_policy=length_policy
        or WritingLengthPolicy(
            minimum_characters=100,
            target_characters=200,
            maximum_characters=300,
        ),
    )


def _receipt(
    basis: WriterArtifactBasis,
    *,
    mode: AgentMode = AgentMode.DRAFT,
    outputs: tuple[ArtifactRef, ...] | None = None,
) -> AgentExecutionReceipt:
    return AgentExecutionReceipt(
        receipt_id=StableId("receipt.writer"),
        run_id=RunId("run.writer"),
        task_id=TaskId("task.writer"),
        agent_spec=ContractRef(
            contract_id=StableId("agent.writer"),
            version=VERSION,
            content_hash=_hash("d"),
        ),
        agent_type=AgentType.WRITER,
        agent_mode=mode,
        prompt_fingerprint=_hash("e"),
        configuration_fingerprint=basis.configuration_fingerprint,
        base_commit=basis.base_commit,
        output_artifacts=outputs or (_artifact("6"), _artifact("7"), _artifact("8")),
        model_call_ids=(StableId("model-call.writer"),),
        status=ExecutionStatus.SUCCEEDED,
        started_at=NOW,
        completed_at=NOW,
        latency_ms=0,
    )


def _draft(
    basis: WriterArtifactBasis,
    *,
    mode: AgentMode = AgentMode.DRAFT,
    parent_draft_id: ArtifactId | None = None,
    receipt: AgentExecutionReceipt | None = None,
) -> DraftArtifact:
    actual_receipt = receipt or _receipt(basis, mode=mode)
    return DraftArtifact(
        draft_id=_hash("f"),
        mode=mode,
        basis=basis,
        text_artifact=_artifact("6"),
        sidecar_artifact=_artifact("7"),
        raw_output_artifact=_artifact("8"),
        parent_draft_id=parent_draft_id,
        writer_receipt=actual_receipt,
        model_call_ids=actual_receipt.model_call_ids,
        created_at=NOW,
    )


def _fingerprints(basis: WriterArtifactBasis) -> WriterRuntimeFingerprints:
    return WriterRuntimeFingerprints(
        agent_spec_fingerprint=_hash("d"),
        prompt_fingerprint=_hash("e"),
        skill_fingerprints=(_hash("f"),),
        tool_policy_fingerprint=_hash("0"),
        configuration_fingerprint=basis.configuration_fingerprint,
        model_configuration_fingerprint=basis.model_configuration_fingerprint,
    )


def _metrics(*, called: bool = True, count: int = 1) -> WriterExecutionMetrics:
    return WriterExecutionMetrics(
        model_called=called,
        model_call_count=count,
        input_tokens=10 if called else 0,
        output_tokens=20 if called else 0,
        cost_usd=Decimal("0.01") if called else Decimal("0"),
        latency_ms=5 if called else 0,
    )


def _completed_result(basis: WriterArtifactBasis) -> WriterExecutionResult:
    draft = _draft(basis)
    return WriterExecutionResult(
        result_id=StableId("result.writer"),
        invocation_id=StableId("invocation.writer"),
        run_id=RunId("run.writer"),
        task_id=TaskId("task.writer"),
        status=WriterTerminalStatus.COMPLETED,
        basis=basis,
        draft=draft,
        receipt=draft.writer_receipt,
        artifacts=(
            draft.text_artifact,
            draft.sidecar_artifact,
            draft.raw_output_artifact,
        ),
        fingerprints=_fingerprints(basis),
        metrics=_metrics(),
        retry_safe=False,
    )


def _invocation(
    *,
    mode: AgentMode = AgentMode.DRAFT,
    basis: WriterArtifactBasis | None = None,
    context: WriterContextSnapshot | None = None,
    prior_draft: DraftArtifact | None = None,
    boundary: ContinuationBoundary | None = None,
    directive: RewriteDirective | None = None,
) -> WriterInvocation:
    actual_basis = basis or _basis()
    return WriterInvocation(
        invocation_id=StableId("invocation.writer"),
        run_id=RunId("run.writer"),
        task_id=TaskId("task.writer"),
        mode=mode,
        basis=actual_basis,
        writing_task=_writing_task(),
        context_package=context or _context(),
        input_artifacts=(
            actual_basis.context_artifact,
            actual_basis.writing_contract_artifact,
            actual_basis.plan_artifact,
            actual_basis.project_profile_artifact,
            actual_basis.source_artifacts[0].source_artifact,
        ),
        prior_draft=prior_draft,
        continuation_boundary=boundary,
        rewrite_directive=directive,
        budget=WriterBudget(
            max_model_calls=1,
            input_token_limit=2_000,
            output_token_limit=2_000,
        ),
    )


def test_generation_models_inherit_strict_frozen_extra_forbid_contract() -> None:
    assert WriterInvocation.model_config["strict"] is True
    assert WriterInvocation.model_config["frozen"] is True
    assert WriterInvocation.model_config["extra"] == "forbid"
    payload = WriterDraftPayload(draft_text="text")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WriterDraftPayload(draft_text="text", canonical_id="forbidden")  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="Instance is frozen"):
        payload.draft_text = "changed"  # type: ignore[misc]


def test_writer_public_enums_and_shadow_only_literals_are_frozen() -> None:
    assert AgentType.WRITER.value == "writer"
    assert tuple(
        mode.value for mode in (AgentMode.DRAFT, AgentMode.CONTINUE, AgentMode.MAJOR_REWRITE)
    ) == ("draft", "continue", "major_rewrite")
    assert tuple(item.value for item in MemoryHintChangeKind) == (
        "ADD",
        "CHANGE",
        "END",
        "UNCERTAIN",
    )
    basis = _basis()
    result = _completed_result(basis)
    manifest = WriterShadowManifest(
        manifest_id=StableId("manifest.writer"),
        run_id=result.run_id,
        result=result,
        artifacts=result.artifacts,
        created_at=NOW,
    )
    assert manifest.engineering_only
    assert manifest.semantic_quality_not_evaluated
    assert not manifest.evaluation_ledger_eligible
    for field, value in (
        ("engineering_only", False),
        ("semantic_quality_not_evaluated", False),
        ("evaluation_ledger_eligible", True),
    ):
        payload = manifest.model_dump(mode="json")
        payload[field] = value
        with pytest.raises(ValidationError):
            WriterShadowManifest.model_validate_json(
                json.dumps(payload, ensure_ascii=False),
            )


def test_writer_source_and_basis_fail_closed_on_taint_or_unattested_lineage() -> None:
    with pytest.raises(ValidationError, match="taint"):
        WriterSourceBinding(
            source_id=StableId("source.future"),
            source_artifact=_artifact("5"),
            taints=(WriterInputTaint.FUTURE,),
        )
    basis = _basis()
    with pytest.raises(ValidationError, match="context fingerprint"):
        _replace(basis, context_fingerprint=_hash("2"))
    with pytest.raises(ValidationError, match="supplied together"):
        _replace(basis, memory_gate_artifact=_artifact("9"))
    failed_gate = Stage2GateReport.model_construct(
        report_id=StableId("gate.failed"),
        evidence_id=StableId("gate-evidence.failed"),
        verdict=Stage2GateVerdict.FAIL,
        checks={},
        blockers=("failed",),
        controller_promotion=ControllerPromotionDecision.REJECT_ARCHITECTURE,
        memory_gateway_frozen=False,
        configuration_fingerprint=_hash("8"),
    )
    with pytest.raises(ValidationError, match="frozen passing"):
        _replace(
            basis,
            memory_gate_report=failed_gate,
            memory_gate_artifact=_artifact("9"),
        )

    failed_attestation = FutureIsolationAttestation(
        attestation_id=StableId("attestation.failed"),
        checkpoint_chapter=20,
        canonical_source_ids=(StableId("source.overlap"),),
        evaluator_only_source_ids=(StableId("source.overlap"),),
        overlap_source_ids=(StableId("source.overlap"),),
        passed=False,
        configuration_fingerprint=_hash("9"),
    )
    with pytest.raises(ValidationError, match="passing future-isolation"):
        _basis(attestation=failed_attestation, sources=())

    with pytest.raises(ValidationError, match="unique source ids"):
        _basis(
            attestation=_attestation(
                canonical=(StableId("source.writer"),),
                evaluator=(),
            ),
            sources=(
                _source(artifact=_artifact("5")),
                _source(artifact=_artifact("6")),
            ),
        )
    with pytest.raises(ValidationError, match="unique artifacts"):
        _basis(
            attestation=_attestation(
                canonical=(StableId("source.writer"), StableId("source.second")),
                evaluator=(),
            ),
            sources=(
                _source(),
                _source(
                    source_id=StableId("source.second"),
                    artifact=_artifact("5"),
                ),
            ),
        )
    with pytest.raises(ValidationError, match="evaluator-only"):
        _basis(
            attestation=_attestation(
                canonical=(),
                evaluator=(StableId("source.evaluator"),),
            ),
            sources=(_source(source_id=StableId("source.evaluator")),),
        )
    with pytest.raises(ValidationError, match="absent from canonical"):
        _basis(sources=(_source(source_id=StableId("source.unattested")),))
    assert _basis(attestation=_attestation(canonical=()), sources=()).source_artifacts == ()
    assert (
        basis.future_isolation_attestation.configuration_fingerprint
        != basis.configuration_fingerprint
    )


def test_writing_contract_length_and_unique_targets_are_enforced() -> None:
    assert _writing_task().length_policy.target_characters == 200
    with pytest.raises(ValidationError, match="minimum <= target <= maximum"):
        WritingLengthPolicy(
            minimum_characters=300,
            target_characters=200,
            maximum_characters=100,
        )
    with pytest.raises(ValidationError, match="scene ids"):
        _replace(
            _writing_task(),
            target_scenes=(StableId("scene.same"), StableId("scene.same")),
        )
    with pytest.raises(ValidationError, match="obligation ids"):
        _replace(
            _writing_task(),
            active_plan_obligations=(
                StableId("obligation.same"),
                StableId("obligation.same"),
            ),
        )
    assert (
        WriterBudget(
            max_model_calls=0,
            input_token_limit=1,
            output_token_limit=1,
        ).max_model_calls
        == 0
    )


def test_writer_invocation_enforces_basis_and_three_exclusive_modes() -> None:
    assert _invocation().mode is AgentMode.DRAFT
    with pytest.raises(ValidationError, match="Writer mode"):
        _invocation(mode=AgentMode.CHAPTER)
    with pytest.raises(ValidationError, match="base commit"):
        _invocation(context=_context(base_commit=CommitId(_hash("b").root)))
    with pytest.raises(ValidationError, match="snapshot"):
        _invocation(context=_context(snapshot_id=StableId("snapshot.other")))
    with pytest.raises(ValidationError, match="context id"):
        _invocation(context=_context(context_id=StableId("context.other")))

    invocation = _invocation()
    with pytest.raises(ValidationError, match="input artifacts must be unique"):
        _replace(
            invocation,
            input_artifacts=(
                invocation.basis.context_artifact,
                invocation.basis.context_artifact,
            ),
        )
    prior = _draft(invocation.basis)
    with pytest.raises(ValidationError, match="DRAFT forbids"):
        _replace(invocation, prior_draft=prior)
    with pytest.raises(ValidationError, match="require a prior"):
        _replace(invocation, mode=AgentMode.CONTINUE)

    boundary = ContinuationBoundary(
        parent_draft_id=prior.draft_id,
        frozen_prefix_artifact=_artifact("0", "text/plain"),
        frozen_prefix_characters=10,
    )
    continued = _replace(
        invocation,
        mode=AgentMode.CONTINUE,
        prior_draft=prior,
        continuation_boundary=boundary,
    )
    assert continued.continuation_boundary == boundary
    with pytest.raises(ValidationError, match="requires only a continuation"):
        _replace(continued, continuation_boundary=None)
    with pytest.raises(ValidationError, match="boundary does not match"):
        _replace(
            continued,
            continuation_boundary=_replace(boundary, parent_draft_id=_hash("0")),
        )

    directive = RewriteDirective(
        directive_id=StableId("directive.writer"),
        parent_draft_id=prior.draft_id,
        scope=RewriteScope.MAJOR_REWRITE,
        directive_artifact=_artifact("0"),
        instructions=("Rewrite the chapter structure",),
    )
    rewritten = _replace(
        invocation,
        mode=AgentMode.MAJOR_REWRITE,
        prior_draft=prior,
        rewrite_directive=directive,
    )
    assert rewritten.rewrite_directive == directive
    with pytest.raises(ValidationError, match="requires only a rewrite"):
        _replace(rewritten, continuation_boundary=boundary)
    with pytest.raises(ValidationError, match="directive does not match"):
        _replace(
            rewritten,
            rewrite_directive=_replace(directive, parent_draft_id=_hash("0")),
        )
    with pytest.raises(ValidationError, match="LOCAL_REPAIR"):
        _replace(
            rewritten,
            rewrite_directive=_replace(directive, scope=RewriteScope.LOCAL_REPAIR),
        )


def test_untrusted_payload_sidecar_and_draft_candidate_boundaries() -> None:
    hint = DeclaredMemoryHint(
        subject_hint="Lin",
        change_kind=MemoryHintChangeKind.CHANGE,
        predicate_hint="location",
        value_hint="tower",
        evidence_quote="Lin entered the tower.",
        confidence=0.8,
    )
    payload = WriterDraftPayload(
        draft_text="Lin entered the tower.",
        declared_memory_hints=(hint,),
        unresolved_questions=("Who waits upstairs?",),
        self_observations=("POV remains limited.",),
    )
    finding = WriterAdvisoryFinding(
        hint_index=0,
        evidence_quote=hint.evidence_quote,
        occurrence_count=1,
        code="quote.unique",
        message="Quote located exactly once.",
    )
    sidecar = WriterSidecar(
        declared_memory_hints=payload.declared_memory_hints,
        unresolved_questions=payload.unresolved_questions,
        self_observations=payload.self_observations,
        advisory_findings=(finding,),
    )
    assert sidecar.declared_memory_hints == (hint,)
    with pytest.raises(ValidationError, match="must not be blank"):
        WriterDraftPayload(draft_text=" \n ")

    basis = _basis()
    draft = _draft(basis)
    assert draft.candidate_only
    with pytest.raises(ValidationError, match="Writer mode"):
        _replace(draft, mode=AgentMode.CHAPTER)
    with pytest.raises(ValidationError, match="cannot have a parent"):
        _replace(draft, parent_draft_id=_hash("0"))
    with pytest.raises(ValidationError, match="require a parent"):
        _replace(draft, mode=AgentMode.CONTINUE)
    with pytest.raises(ValidationError, match="identify this Writer mode"):
        _replace(
            draft,
            writer_receipt=_replace(draft.writer_receipt, agent_type=AgentType.PLANNER),
        )
    with pytest.raises(ValidationError, match="successful"):
        _replace(
            draft,
            writer_receipt=_replace(draft.writer_receipt, status=ExecutionStatus.FAILED),
        )
    with pytest.raises(ValidationError, match="base commit"):
        _replace(
            draft,
            writer_receipt=_replace(
                draft.writer_receipt,
                base_commit=CommitId(_hash("0").root),
            ),
        )
    with pytest.raises(ValidationError, match="configuration fingerprint"):
        _replace(
            draft,
            writer_receipt=_replace(
                draft.writer_receipt,
                configuration_fingerprint=_hash("0"),
            ),
        )
    with pytest.raises(ValidationError, match="model call ids"):
        _replace(draft, model_call_ids=(StableId("model-call.other"),))
    with pytest.raises(ValidationError, match="every candidate artifact"):
        _replace(
            draft,
            writer_receipt=_receipt(basis, outputs=(draft.text_artifact,)),
        )
    continued_receipt = _receipt(basis, mode=AgentMode.CONTINUE)
    assert (
        _draft(
            basis,
            mode=AgentMode.CONTINUE,
            parent_draft_id=draft.draft_id,
            receipt=continued_receipt,
        ).parent_draft_id
        == draft.draft_id
    )


def test_writer_terminal_result_never_fabricates_failure_drafts_or_receipts() -> None:
    basis = _basis()
    completed = _completed_result(basis)
    assert completed.status is WriterTerminalStatus.COMPLETED
    with pytest.raises(ValidationError, match="configuration fingerprint"):
        _replace(
            completed,
            fingerprints=_replace(
                completed.fingerprints,
                configuration_fingerprint=_hash("0"),
            ),
        )
    with pytest.raises(ValidationError, match="model fingerprint"):
        _replace(
            completed,
            fingerprints=_replace(
                completed.fingerprints,
                model_configuration_fingerprint=_hash("0"),
            ),
        )
    with pytest.raises(ValidationError, match="requires DraftArtifact"):
        _replace(completed, draft=None)
    with pytest.raises(ValidationError, match="cannot carry a failure"):
        _replace(completed, failure_code=WriterFailureCode.FATAL)
    assert completed.receipt is not None
    other_receipt = _replace(
        completed.receipt,
        receipt_id=StableId("receipt.writer.other"),
    )
    with pytest.raises(ValidationError, match="differs from DraftArtifact"):
        _replace(completed, receipt=other_receipt)
    with pytest.raises(ValidationError, match="identity"):
        _replace(completed, run_id=RunId("run.other"))

    failed = WriterExecutionResult(
        result_id=StableId("result.failed"),
        invocation_id=StableId("invocation.writer"),
        run_id=RunId("run.writer"),
        task_id=TaskId("task.writer"),
        status=WriterTerminalStatus.MODEL_UNAVAILABLE,
        basis=basis,
        fingerprints=_fingerprints(basis),
        metrics=_metrics(called=False, count=0),
        retry_safe=True,
        failure_code=WriterFailureCode.MODEL_UNAVAILABLE,
    )
    assert failed.draft is None and failed.receipt is None
    with pytest.raises(ValidationError, match="cannot contain"):
        _replace(failed, draft=completed.draft)
    with pytest.raises(ValidationError, match="requires a failure code"):
        _replace(failed, failure_code=None)
    with pytest.raises(ValidationError, match="contradicts terminal"):
        _replace(failed, failure_code=WriterFailureCode.FATAL)
    with pytest.raises(ValidationError, match="model_called"):
        _metrics(called=False, count=2)
    assert _metrics(called=True, count=2).model_call_count == 2


def test_shadow_manifest_rejects_cross_run_result() -> None:
    result = _completed_result(_basis())
    with pytest.raises(ValidationError, match="another run"):
        WriterShadowManifest(
            manifest_id=StableId("manifest.writer"),
            run_id=RunId("run.other"),
            result=result,
            created_at=NOW,
        )


def test_checked_in_stage3_schemas_match_stage3_models() -> None:
    schema_directory = REPOSITORY_ROOT / "schemas" / "stage3"
    model_types = {
        value.__name__: value
        for module in (
            generation,
            editorial,
            agent_context,
            writing_loop,
            stage3_evaluation,
            stage3_loop_evaluation,
        )
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, DomainModel)
        and value is not DomainModel
        and value.__module__ == module.__name__
    }
    assert {path.stem.removesuffix(".schema") for path in schema_directory.iterdir()} == set(
        model_types
    )
    for name, model_type in model_types.items():
        checked_in = json.loads(
            (schema_directory / f"{name}.schema.json").read_text(encoding="utf-8")
        )
        assert checked_in == model_type.model_json_schema()
