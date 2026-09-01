"""Thin Track A/B Writer readout adapter.

The probe does not assemble a Writer, does not write Memory/Canon, and does
not reveal Gold. Production binds the unique ModelGateway Writer role.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from novel_agent.domain.artifacts import (
    CONTEXT_WRITER_READOUT_RECORD_MEDIA_TYPE,
    CONTEXT_WRITER_RESPONSE_MEDIA_TYPE,
    QA_WRITER_READOUT_RECORD_MEDIA_TYPE,
    QA_WRITER_RESPONSE_MEDIA_TYPE,
    ArtifactRef,
)
from novel_agent.domain.ids import RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.memory_benchmark import (
    ContextWriterModelDraft,
    ContextWriterReadoutRecord,
    ContextWriterResponse,
    QaWriterReadoutRecord,
    QaWriterResponse,
)
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    FreezeReceipt,
    WriterContextPackageV2,
)
from novel_agent.services.artifacts import ArtifactRepository, sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.memory_benchmark_contract import assert_safe_public_payload
from novel_agent.services.model_gateway import ModelGateway

WriterReadoutResponse = ContextWriterResponse | QaWriterResponse
WriterContextReadoutFn = Callable[
    ["WriterContextReadoutRequest"],
    WriterReadoutResponse | Awaitable[WriterReadoutResponse],
]
READOUT_SCHEMA_VERSION = SchemaVersion("1.0.0")
CONTEXT_READOUT_STAGE = "benchmark.writer_context_readout"
QA_READOUT_STAGE = "benchmark.writer_qa_readout"


class WriterContextReadoutError(ValueError):
    """The readout contract was violated before or after the Writer call."""


@dataclass(frozen=True, slots=True)
class WriterContextReadoutRequest:
    task_contract: BenchmarkTaskContract
    writer_context: WriterContextPackageV2
    gold_revealed: bool = False
    freeze_receipt: FreezeReceipt | None = None
    case_id: StableId | None = None
    question_id: StableId | None = None
    question_text: str | None = None
    track: str = "novelmem_context"


class WriterContextReadoutProbe:
    """WCP v2 + task contract -> frozen Writer readout response."""

    def __init__(
        self,
        writer: WriterContextReadoutFn,
        *,
        require_production_contract: bool = False,
    ) -> None:
        self._writer = writer
        self._require_production_contract = require_production_contract

    def run(self, request: WriterContextReadoutRequest) -> WriterReadoutResponse:
        self._preflight(request)
        result = self._writer(request)
        if inspect.isawaitable(result):
            raise WriterContextReadoutError("async Writer readout must use arun")
        return self._finalize(request, result)

    async def arun(self, request: WriterContextReadoutRequest) -> WriterReadoutResponse:
        self._preflight(request)
        result = self._writer(request)
        if inspect.isawaitable(result):
            response = await result
        else:
            response = result
        return self._finalize(request, response)

    def _preflight(self, request: WriterContextReadoutRequest) -> None:
        if request.gold_revealed:
            raise WriterContextReadoutError("readout must freeze before Gold reveal")
        if request.writer_context.task_contract.task_id != request.task_contract.task_id:
            raise WriterContextReadoutError("WCP task_id does not match readout task")
        if not self._require_production_contract:
            return
        if request.freeze_receipt is None or not request.freeze_receipt.frozen_before_reveal:
            raise WriterContextReadoutError("production readout requires a freeze receipt")
        if request.case_id is None:
            raise WriterContextReadoutError("production readout requires a case id")
        if request.track == "novelmem_qa" and request.question_id is None:
            raise WriterContextReadoutError("QA readout requires a question id")
        task = request.task_contract
        if task.information_profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED:
            if task.planning_context_ref is None or task.planning_context_hash is None:
                raise WriterContextReadoutError(
                    "author_plan_conditioned requires frozen planning context"
                )
        elif task.planning_context_ref is not None or task.planning_context_hash is not None:
            raise WriterContextReadoutError("history-only profile must not carry planning context")

    def _finalize(
        self,
        request: WriterContextReadoutRequest,
        response: WriterReadoutResponse,
    ) -> WriterReadoutResponse:
        if request.track == "novelmem_qa":
            if not isinstance(response, QaWriterResponse):
                raise WriterContextReadoutError("QA readout must return QaWriterResponse")
            self._reject_qa_taint(request, response)
            return response
        if not isinstance(response, ContextWriterResponse):
            raise WriterContextReadoutError("Context readout must return ContextWriterResponse")
        if not response.frozen_before_gold_reveal:
            raise WriterContextReadoutError("Writer answer was not frozen before Gold reveal")
        if response.task_contract.task_id != request.task_contract.task_id:
            raise WriterContextReadoutError("Writer answer task_id does not match readout task")
        if response.basis_commit_id != request.writer_context.basis_commit_id:
            raise WriterContextReadoutError("Writer answer basis does not match frozen WCP")
        self._reject_context_taint(request, response)
        return response

    @staticmethod
    def _reject_context_taint(
        request: WriterContextReadoutRequest,
        response: ContextWriterResponse,
    ) -> None:
        rendered = response.rendered_response.strip()
        if rendered:
            start = request.task_contract.target_chapter_start
            end = request.task_contract.target_chapter_end
            if f"第{start}章" in rendered or f"第{end}章" in rendered:
                raise WriterContextReadoutError(
                    "Writer answer appears to contain target-window prose"
                )
        assert_safe_public_payload(
            {
                "conclusions": tuple(item.text for item in response.conclusions),
                "gaps": tuple(item.description for item in response.gaps),
                "rendered_response": response.rendered_response,
            }
        )

    @staticmethod
    def _reject_qa_taint(
        request: WriterContextReadoutRequest,
        response: QaWriterResponse,
    ) -> None:
        answer = response.answer
        if isinstance(answer, str) and answer.strip():
            start = request.task_contract.target_chapter_start
            end = request.task_contract.target_chapter_end
            if f"第{start}章" in answer or f"第{end}章" in answer:
                raise WriterContextReadoutError(
                    "Writer answer appears to contain target-window prose"
                )
        for item in response.evidence:
            if item.chapter > request.task_contract.checkpoint_chapter:
                raise WriterContextReadoutError("QA evidence chapter is after the freeze")
        assert_safe_public_payload(
            {
                "answer": answer if isinstance(answer, str) else "",
                "evidence": tuple(item.span for item in response.evidence),
            }
        )


class ProductionContextWriterReadout:
    """Named production callable: Writer-role Context readout through ModelGateway."""

    is_fixture = False

    def __init__(
        self,
        gateway: ModelGateway,
        artifacts: ArtifactRepository,
        *,
        run_id: RunId,
        max_output_tokens: int,
        timeout_seconds: float = 30.0,
        enable_thinking: bool | None = None,
        thinking_token_budget: int | None = None,
    ) -> None:
        self._gateway = gateway
        self._artifacts = artifacts
        self._run_id = run_id
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._enable_thinking = enable_thinking
        self._thinking_token_budget = thinking_token_budget
        self.last_record_ref: ArtifactRef | None = None
        self.last_response_ref: ArtifactRef | None = None

    async def __call__(self, request: WriterContextReadoutRequest) -> ContextWriterResponse:
        prompt = _context_readout_prompt(request)
        model_request = _readout_model_request(
            request,
            run_id=self._run_id,
            prompt=prompt,
            stage=CONTEXT_READOUT_STAGE,
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
            enable_thinking=self._enable_thinking,
            thinking_token_budget=self._thinking_token_budget,
        )
        draft, call = await self._gateway.generate_structured(
            model_request,
            ContextWriterModelDraft,
        )
        response = ContextWriterResponse(
            response_version="context_writer_response.v1",
            task_contract=request.task_contract,
            basis_commit_id=request.writer_context.basis_commit_id,
            conclusions=draft.conclusions,
            gaps=draft.gaps,
            rendered_response=draft.rendered_response,
            frozen_before_gold_reveal=True,
        )
        if request.case_id is None or request.freeze_receipt is None:
            raise WriterContextReadoutError("production readout identity is incomplete")
        record = ContextWriterReadoutRecord(
            run_id=self._run_id,
            case_id=request.case_id,
            checkpoint_chapter=request.task_contract.checkpoint_chapter,
            information_profile=request.task_contract.information_profile,
            task_id=request.task_contract.task_id,
            package_ref=request.writer_context.evidence_ledger_ref,
            basis_commit_id=request.writer_context.basis_commit_id,
            freeze_receipt_id=request.freeze_receipt.receipt_id,
            model_role=ModelRole.IMPLEMENTATION,
            model_request_id=call.request_id,
            prompt_hash=sha256_id(prompt.encode("utf-8")),
            schema_title="ContextWriterResponse",
            response=response,
        )
        self.last_response_ref = self._artifacts.put(
            canonical_json_bytes(response.model_dump(mode="json")),
            CONTEXT_WRITER_RESPONSE_MEDIA_TYPE,
            READOUT_SCHEMA_VERSION,
        )
        self.last_record_ref = self._artifacts.put(
            canonical_json_bytes(record.model_dump(mode="json")),
            CONTEXT_WRITER_READOUT_RECORD_MEDIA_TYPE,
            READOUT_SCHEMA_VERSION,
        )
        return response


class ProductionQaWriterReadout:
    """Named production callable: Writer-role QA readout through ModelGateway."""

    is_fixture = False

    def __init__(
        self,
        gateway: ModelGateway,
        artifacts: ArtifactRepository,
        *,
        run_id: RunId,
        max_output_tokens: int,
        timeout_seconds: float = 30.0,
        enable_thinking: bool | None = None,
        thinking_token_budget: int | None = None,
    ) -> None:
        self._gateway = gateway
        self._artifacts = artifacts
        self._run_id = run_id
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._enable_thinking = enable_thinking
        self._thinking_token_budget = thinking_token_budget
        self.last_record_ref: ArtifactRef | None = None
        self.last_response_ref: ArtifactRef | None = None

    async def __call__(self, request: WriterContextReadoutRequest) -> QaWriterResponse:
        prompt = _qa_readout_prompt(request)
        model_request = _readout_model_request(
            request,
            run_id=self._run_id,
            prompt=prompt,
            stage=QA_READOUT_STAGE,
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
            enable_thinking=self._enable_thinking,
            thinking_token_budget=self._thinking_token_budget,
        )
        response, call = await self._gateway.generate_structured(model_request, QaWriterResponse)
        if request.case_id is None or request.freeze_receipt is None:
            raise WriterContextReadoutError("production readout identity is incomplete")
        if request.question_id is None:
            raise WriterContextReadoutError("QA readout requires a question id")
        record = QaWriterReadoutRecord(
            run_id=self._run_id,
            case_id=request.case_id,
            checkpoint_chapter=request.task_contract.checkpoint_chapter,
            information_profile=request.task_contract.information_profile,
            task_id=request.task_contract.task_id,
            question_id=request.question_id,
            package_ref=request.writer_context.evidence_ledger_ref,
            basis_commit_id=request.writer_context.basis_commit_id,
            freeze_receipt_id=request.freeze_receipt.receipt_id,
            model_role=ModelRole.IMPLEMENTATION,
            model_request_id=call.request_id,
            prompt_hash=sha256_id(prompt.encode("utf-8")),
            schema_title="QaWriterResponse",
            response=response,
        )
        self.last_response_ref = self._artifacts.put(
            canonical_json_bytes(response.model_dump(mode="json")),
            QA_WRITER_RESPONSE_MEDIA_TYPE,
            READOUT_SCHEMA_VERSION,
        )
        self.last_record_ref = self._artifacts.put(
            canonical_json_bytes(record.model_dump(mode="json")),
            QA_WRITER_READOUT_RECORD_MEDIA_TYPE,
            READOUT_SCHEMA_VERSION,
        )
        return response


def bind_production_context_readout(
    gateway: ModelGateway,
    artifacts: ArtifactRepository,
    *,
    run_id: RunId,
    max_output_tokens: int = 8000,
    timeout_seconds: float = 30.0,
    enable_thinking: bool | None = None,
    thinking_token_budget: int | None = None,
) -> tuple[WriterContextReadoutProbe, ProductionContextWriterReadout]:
    writer = ProductionContextWriterReadout(
        gateway,
        artifacts,
        run_id=run_id,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        enable_thinking=enable_thinking,
        thinking_token_budget=thinking_token_budget,
    )
    return WriterContextReadoutProbe(writer, require_production_contract=True), writer


def bind_production_qa_readout(
    gateway: ModelGateway,
    artifacts: ArtifactRepository,
    *,
    run_id: RunId,
    max_output_tokens: int = 8000,
    timeout_seconds: float = 30.0,
    enable_thinking: bool | None = None,
    thinking_token_budget: int | None = None,
) -> tuple[WriterContextReadoutProbe, ProductionQaWriterReadout]:
    writer = ProductionQaWriterReadout(
        gateway,
        artifacts,
        run_id=run_id,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        enable_thinking=enable_thinking,
        thinking_token_budget=thinking_token_budget,
    )
    return WriterContextReadoutProbe(writer, require_production_contract=True), writer


def _readout_model_request(
    request: WriterContextReadoutRequest,
    *,
    run_id: RunId,
    prompt: str,
    stage: str,
    max_output_tokens: int,
    timeout_seconds: float = 30.0,
    enable_thinking: bool | None = None,
    thinking_token_budget: int | None = None,
) -> ModelRequest:
    task_id = TaskId(request.task_contract.task_id.root[:128])
    return ModelRequest(
        request_id=readout_model_request_id(
            run_id=run_id,
            task_id=task_id.root,
            stage=stage,
        ),
        run_id=run_id,
        task_id=task_id,
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id=f"trace.{run_id.root}.writer-readout"[:256],
        prompt=prompt,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        enable_thinking=enable_thinking,
        thinking_token_budget=thinking_token_budget,
        scheduling_stage=stage,
    )


def readout_model_request_id(*, run_id: RunId, task_id: str, stage: str) -> StableId:
    """Return a durable readout request identity unique to one campaign run."""

    stage_identity = stage.removeprefix("benchmark.writer_")
    return StableId(f"model-request.readout.{run_id.root}.{task_id}.{stage_identity}")


def _context_readout_prompt(request: WriterContextReadoutRequest) -> str:
    task = request.task_contract
    return (
        "You are the production Writer answering a frozen Track B context readout.\n"
        "Return historical conclusions with evidence refs. Do not write target-window "
        "prose, future text, Gold ids, or why_needed fields.\n"
        f"profile={task.information_profile.value}\n"
        f"checkpoint={task.checkpoint_chapter}\n"
        f"task={task.task_text}\n"
        f"<WRITER_CONTEXT>\n{request.writer_context.rendered_context}\n"
        "</WRITER_CONTEXT>\n"
    )


def _qa_readout_prompt(request: WriterContextReadoutRequest) -> str:
    question = request.question_text or ""
    task = request.task_contract
    return (
        "You are the production Writer answering a frozen Track A QA readout.\n"
        "Return answer plus at most 20 history evidence spans. Do not write future text "
        "or Gold ids.\n"
        f"profile={task.information_profile.value}\n"
        f"checkpoint={task.checkpoint_chapter}\n"
        f"question={question}\n"
        f"<WRITER_CONTEXT>\n{request.writer_context.rendered_context}\n"
        "</WRITER_CONTEXT>\n"
    )


def map_v05_history_access(history_access: str) -> BenchmarkInformationProfile:
    """Compatibility wrapper for the domain mapping."""

    from novel_agent.domain.v05_readout import map_v05_history_access as _map

    return _map(history_access)
