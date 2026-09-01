"""Execute all Stage 3 Context schemes through the real full candidate loop."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from novel_agent.domain.generation import DraftArtifact, WriterSidecar, WritingLoopRequest
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.stage3_evaluation import (
    ContextScheme,
    EvaluatorScore,
    Stage3EvaluationCase,
)
from novel_agent.domain.stage3_loop_evaluation import (
    Stage3FormalManifest,
    Stage3FullChainCaseResult,
    Stage3FullChainEvaluationReport,
    Stage3FullChainSchemeResult,
)
from novel_agent.domain.writing_loop import WritingLoopTerminalStatus
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.model_call_ledger import (
    ModelCallLedgerPort,
    aggregate_model_calls,
    summarize_model_cost,
)
from novel_agent.services.stage3_evaluation import evaluate_rules
from novel_agent.services.writer_context_loop import WriterContextLoopService
from novel_agent.services.writer_reactive_memory import ReactiveMemoryInputs


@dataclass(frozen=True, slots=True)
class PreparedFullChainRun:
    loop: WriterContextLoopService
    request: WritingLoopRequest
    model_request: ModelRequest
    reactive_inputs: ReactiveMemoryInputs
    artifacts: ArtifactRepository


class Stage3FullChainRuntimeFactory(Protocol):
    def prepare(
        self,
        case: Stage3EvaluationCase,
        scheme: ContextScheme,
    ) -> PreparedFullChainRun: ...


class Stage3PostFreezeEvaluator(Protocol):
    async def evaluate(
        self,
        case: Stage3EvaluationCase,
        scheme: ContextScheme,
        final_text: str,
    ) -> tuple[EvaluatorScore, ...]: ...


class Stage3FullChainEvaluationService:
    """No fixture Writer, Editor, Observer, or reconciliation results are accepted."""

    async def run(
        self,
        cases: Sequence[Stage3EvaluationCase],
        manifest: Stage3FormalManifest,
        factory: Stage3FullChainRuntimeFactory,
        evaluator: Stage3PostFreezeEvaluator,
        call_ledger: ModelCallLedgerPort | None = None,
    ) -> Stage3FullChainEvaluationReport:
        from datetime import UTC, datetime

        case_results: list[Stage3FullChainCaseResult] = []
        for case in cases:
            schemes: list[Stage3FullChainSchemeResult] = []
            for scheme in ContextScheme:
                prepared = factory.prepare(case, scheme)
                result = await prepared.loop.execute(
                    prepared.request,
                    prepared.model_request,
                    prepared.reactive_inputs,
                )
                final_text = ""
                rules = None
                scores: tuple[EvaluatorScore, ...] = ()
                if result.final_text_artifact is not None:
                    final_text = prepared.artifacts.read_verified(
                        result.final_text_artifact
                    ).decode("utf-8")
                if result.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY:
                    draft = cast(DraftArtifact, result.rewritten_draft or result.initial_draft)
                    sidecar = WriterSidecar.model_validate_json(
                        prepared.artifacts.read_verified(draft.sidecar_artifact)
                    )
                    rules = evaluate_rules(case, scheme, final_text, sidecar)
                    scores = await evaluator.evaluate(case, scheme, final_text)
                ledger_entries = (
                    tuple(
                        entry
                        for entry in call_ledger.list_for_run(prepared.request.run_id)
                        if entry.task_id == prepared.request.task_id
                    )
                    if call_ledger is not None
                    else ()
                )
                calls = (
                    tuple(
                        entry.call_record
                        for entry in ledger_entries
                        if entry.call_record is not None
                    )
                    if call_ledger is not None
                    else result.model_call_records
                )
                call_aggregates = aggregate_model_calls(ledger_entries)
                model_cost, model_cost_availability = summarize_model_cost(calls)
                schemes.append(
                    Stage3FullChainSchemeResult(
                        case_id=case.case_id,
                        scheme=scheme,
                        loop_result=result,
                        deterministic_rules=rules,
                        evaluator_scores=scores,
                        context_revision_count=(
                            result.context_view.revision if result.context_view is not None else 0
                        ),
                        compaction_count=len(result.compaction_receipts),
                        memory_request_count=len(result.context_deltas),
                        evidence_added_count=sum(
                            len(item.added_memory_items) for item in result.context_deltas
                        ),
                        repair_count=int(result.repaired_draft is not None),
                        rewrite_count=int(result.rewritten_draft is not None),
                        input_tokens=sum(call.usage.input_tokens for call in calls),
                        output_tokens=sum(call.usage.output_tokens for call in calls),
                        latency_ms=sum(call.latency_ms for call in calls),
                        model_cost_usd=model_cost,
                        model_cost_availability=model_cost_availability,
                        model_call_aggregates=call_aggregates,
                    )
                )
            case_results.append(
                Stage3FullChainCaseResult(case_id=case.case_id, schemes=tuple(schemes))
            )
        return Stage3FullChainEvaluationReport(
            report_id=manifest.manifest_id,
            manifest=manifest,
            cases=tuple(case_results),
            generated_at=datetime.now(UTC),
        )


__all__ = [
    "PreparedFullChainRun",
    "Stage3FullChainEvaluationService",
    "Stage3FullChainRuntimeFactory",
    "Stage3PostFreezeEvaluator",
]
