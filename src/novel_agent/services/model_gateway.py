"""Strict model-role routing with timeouts and complete call audit records."""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from novel_agent.domain.model_calls import (
    ModelCallLedgerEntry,
    ModelCallLedgerStatus,
    ModelCallPurpose,
    ModelCallRecord,
    ModelRequest,
    ModelRole,
    ModelTextResult,
)
from novel_agent.ports.model_endpoint import ModelEndpointPort
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.model_call_ledger import (
    InMemoryModelCallLedger,
    ModelCallLedgerPort,
)

OutputModel = TypeVar("OutputModel", bound=BaseModel)


class ModelRoutingError(RuntimeError):
    pass


class ModelCallForbiddenError(RuntimeError):
    pass


class StructuredGenerationExhausted(ValueError):
    """Structured retries ended with complete durable attempt evidence."""

    def __init__(
        self,
        error: ValidationError,
        entries: tuple[ModelCallLedgerEntry, ...],
    ) -> None:
        super().__init__("structured generation retries exhausted")
        self.validation_error = error
        self.entries = entries


@dataclass(frozen=True, slots=True)
class RegisteredModelEndpoint:
    role: ModelRole
    endpoint_name: str
    model_name: str
    adapter: ModelEndpointPort


@dataclass(frozen=True, slots=True)
class StructuredValidationAttempt:
    """Auditable failed structured-output validation attempt."""

    request_id: str
    call_record: ModelCallRecord
    error_detail: str


class ModelGateway:
    def __init__(
        self,
        endpoints: tuple[RegisteredModelEndpoint, ...],
        *,
        forbid_external_calls: bool = False,
        structured_max_retries: int = 0,
        call_ledger: ModelCallLedgerPort | None = None,
    ) -> None:
        self._endpoints = {endpoint.role: endpoint for endpoint in endpoints}
        if len(self._endpoints) != len(endpoints):
            raise ModelRoutingError("each model role must have at most one registered endpoint")
        self._forbid_external_calls = forbid_external_calls
        if structured_max_retries < 0 or structured_max_retries > 2:
            raise ValueError("structured retries must be between zero and two")
        self._structured_max_retries = structured_max_retries
        self.structured_validation_attempts: list[StructuredValidationAttempt] = []
        self.call_records: list[ModelCallRecord] = []
        self.raw_responses: dict[str, str] = {}
        self._call_ledger = call_ledger or InMemoryModelCallLedger()

    @property
    def call_ledger(self) -> ModelCallLedgerPort:
        return self._call_ledger

    async def generate_text(self, request: ModelRequest) -> ModelTextResult:
        self._validate_purpose(request)
        endpoint = self._endpoints.get(request.model_role)
        if endpoint is None:
            raise ModelRoutingError(f"no endpoint configured for {request.model_role.value}")
        if self._forbid_external_calls and endpoint.adapter.is_external:
            raise ModelCallForbiddenError("external model calls are disabled for this run")

        requested = self._call_ledger.create_requested(request)
        started_at = requested.requested_at
        started_clock = monotonic()
        try:
            provider_result = await asyncio.wait_for(
                endpoint.adapter.generate(request), timeout=request.timeout_seconds
            )
        except TimeoutError as error:
            self._call_ledger.settle(
                requested.model_copy(
                    update={
                        "status": ModelCallLedgerStatus.UNCERTAIN,
                        "transport_error_type": type(error).__name__,
                    }
                )
            )
            raise
        except Exception as error:
            self._call_ledger.settle(
                requested.model_copy(
                    update={
                        "status": ModelCallLedgerStatus.TRANSPORT_EXHAUSTED,
                        "transport_error_type": type(error).__name__,
                        "completed_at": datetime.now(UTC),
                    }
                )
            )
            raise
        completed_at = datetime.now(UTC)
        latency_ms = max(0, round((monotonic() - started_clock) * 1000))
        result = ModelTextResult(
            text=provider_result.text,
            call_record=ModelCallRecord(
                request_id=request.request_id,
                run_id=request.run_id,
                task_id=request.task_id,
                model_role=request.model_role,
                purpose=request.purpose,
                trace_id=request.trace_id,
                span_id=request.span_id,
                endpoint=endpoint.endpoint_name,
                model=endpoint.model_name,
                model_version=provider_result.model_version,
                usage=provider_result.usage,
                latency_ms=latency_ms,
                started_at=started_at,
                completed_at=completed_at,
            ),
        )
        self.call_records.append(result.call_record)
        self.raw_responses[request.request_id.root] = result.text
        self._call_ledger.settle(
            requested.model_copy(
                update={
                    "status": ModelCallLedgerStatus.COMPLETED,
                    "raw_response_hash": sha256_id(result.text.encode("utf-8")),
                    "call_record": result.call_record,
                    "completed_at": completed_at,
                }
            )
        )
        return result

    async def generate_structured(
        self, request: ModelRequest, output_type: type[OutputModel]
    ) -> tuple[OutputModel, ModelCallRecord]:
        schema = output_type.model_json_schema()
        retry_request = request
        for attempt in range(self._structured_max_retries + 1):
            constrained_request = retry_request.model_copy(update={"response_schema": schema})
            result = await self.generate_text(constrained_request)
            try:
                return output_type.model_validate_json(result.text), result.call_record
            except ValidationError as error:
                validation_detail = json.dumps(
                    error.errors(include_url=False, include_input=False),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
                self.structured_validation_attempts.append(
                    StructuredValidationAttempt(
                        request_id=constrained_request.request_id.root,
                        call_record=result.call_record,
                        error_detail=validation_detail,
                    )
                )
                ledger_entry = self._call_ledger.load(constrained_request.request_id)
                if ledger_entry is None:
                    raise AssertionError("completed model call missing from ledger") from error
                self._call_ledger.settle(
                    ledger_entry.model_copy(
                        update={
                            "status": ModelCallLedgerStatus.VALIDATION_REJECTED,
                            "validation_error": validation_detail,
                        }
                    )
                )
                if attempt >= self._structured_max_retries:
                    raise
                suffix = f".schema-retry{attempt + 1}"
                retry_id = request.request_id.root[: 128 - len(suffix)] + suffix
                retry_request = request.model_copy(
                    update={
                        "request_id": type(request.request_id)(retry_id),
                        "prompt": (
                            request.prompt
                            + "\n\n<STRUCTURED_OUTPUT_RETRY>\n"
                            + "The previous JSON violated the required domain contract. "
                            + "Return a complete replacement JSON object, correcting this "
                            + "validation error:\n"
                            + validation_detail
                            + "\n</STRUCTURED_OUTPUT_RETRY>"
                        ),
                    }
                )
        raise AssertionError("structured retry loop did not terminate")  # pragma: no cover

    async def generate_structured_audited(
        self,
        request: ModelRequest,
        output_type: type[OutputModel],
    ) -> tuple[OutputModel, ModelCallRecord]:
        """Typed variant for workflow boundaries that cannot leak ValidationError."""

        try:
            return await self.generate_structured(request, output_type)
        except ValidationError as error:
            raise StructuredGenerationExhausted(
                error,
                self._call_ledger.list_for_prefix(request.request_id.root),
            ) from error

    @staticmethod
    def _validate_purpose(request: ModelRequest) -> None:
        if (
            request.purpose in {ModelCallPurpose.BATCH_TEST, ModelCallPurpose.EVALUATION}
            and request.model_role is not ModelRole.BATCH_TEST
        ):
            raise ModelRoutingError(
                "batch tests and evaluation must use batch_test_model without fallback"
            )
