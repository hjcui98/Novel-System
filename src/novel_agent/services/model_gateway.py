"""Strict model-role routing with timeouts and complete call audit records."""

import asyncio
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from novel_agent.domain.artifacts import MODEL_RAW_RESPONSE_MEDIA_TYPE, ArtifactRef
from novel_agent.domain.ids import ArtifactId, SchemaVersion
from novel_agent.domain.model_calls import (
    BudgetResolutionProfile,
    EffectiveBudgetResult,
    ModelCallLedgerEntry,
    ModelCallLedgerStatus,
    ModelCallPurpose,
    ModelCallRecord,
    ModelRequest,
    ModelRole,
    ModelTextResult,
    ModelUsage,
    ProviderModelResult,
    RawModelResponseArtifact,
)
from novel_agent.ports.model_endpoint import ModelEndpointPort
from novel_agent.services.artifacts import ArtifactRepository, sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.effective_budget import (
    EffectiveBudgetResolver,
    ModelBudgetResolutionError,
    ProviderBudgetLimits,
)
from novel_agent.services.model_call_ledger import (
    InMemoryModelCallLedger,
    ModelCallLedgerCollision,
    ModelCallLedgerPort,
    bounded_model_request_id,
    model_request_hash,
)
from novel_agent.services.model_request_admission import (
    ModelRequestAdmissionController,
    ModelRequestLease,
    ModelRequestSchedulingInfo,
)

OutputModel = TypeVar("OutputModel", bound=BaseModel)


class ModelRoutingError(RuntimeError):
    pass


class ModelCallForbiddenError(RuntimeError):
    pass


class ModelCallUncertainError(RuntimeError):
    """A sent model request has no completion evidence and cannot be resent."""


class ModelCallCumulativeBudgetExceeded(ModelRoutingError):
    """A fully assembled request cannot fit its caller-owned token budget."""

    def __init__(
        self,
        *,
        request_id: str,
        token_budget: int,
        tokens_used: int,
        estimated_input_tokens: int,
        reserved_output_tokens: int,
    ) -> None:
        self.request_id = request_id
        self.token_budget = token_budget
        self.tokens_used = tokens_used
        self.estimated_input_tokens = estimated_input_tokens
        self.reserved_output_tokens = reserved_output_tokens
        required = estimated_input_tokens + reserved_output_tokens
        super().__init__(
            "cumulative model budget cannot admit request "
            f"{request_id}: used={tokens_used}, required={required}, budget={token_budget}"
        )


class RawResponsePersistenceError(RuntimeError):
    """Provider completed but the raw response was not durably retained."""


class RawResponseReparseError(ValueError):
    """A stored raw response cannot be safely re-parsed for its request."""


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
    revision: str | None = None
    sequence_limit: int = 131_072
    output_limit: int | None = None
    safety_allowance_tokens: int | None = None
    estimated_reasoning_reserve: int = 2_048
    default_thinking: bool = False
    reasoning_included_in_completion_tokens: bool = False
    global_output_cap: int = 131_072

    def __post_init__(self) -> None:
        if not isinstance(self.default_thinking, bool):
            raise ValueError("registered endpoint default_thinking must be an explicit bool")


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
        admission_controller: ModelRequestAdmissionController | None = None,
        raw_artifacts: ArtifactRepository | None = None,
        raw_artifact_schema_version: SchemaVersion | None = None,
        scheduling_timeout_seconds: float = 120.0,
        budget_profile: BudgetResolutionProfile = BudgetResolutionProfile.CANARY,
        budget_resolver: EffectiveBudgetResolver | None = None,
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
        self._raw_artifacts = raw_artifacts
        self._raw_artifact_schema_version = raw_artifact_schema_version or SchemaVersion("1.0.0")
        self._admission_controller = admission_controller
        if scheduling_timeout_seconds <= 0:
            raise ValueError("scheduling timeout must be positive")
        self._scheduling_timeout_seconds = scheduling_timeout_seconds
        self._budget_profile = budget_profile
        self._budget_resolver = budget_resolver or EffectiveBudgetResolver()
        self.budget_results: dict[str, EffectiveBudgetResult] = {}
        self._records_lock = threading.Lock()
        self._ledger_lock = threading.Lock()

    @property
    def call_ledger(self) -> ModelCallLedgerPort:
        return self._call_ledger

    @property
    def raw_artifacts(self) -> ArtifactRepository | None:
        return self._raw_artifacts

    @property
    def admission_controller(self) -> ModelRequestAdmissionController | None:
        return self._admission_controller

    @property
    def structured_max_retries(self) -> int:
        """Configured structured-output retry count for campaign resource freezes."""

        return self._structured_max_retries

    def endpoint_adapter(self, role: ModelRole) -> ModelEndpointPort:
        """Public adapter access for transport-diagnostic classification.

        The support corridor uses this only to read retry attempts for
        sanitized failed-call diagnostics; the adapter never sees private
        source text beyond the already-audited request prompts.
        """

        endpoint = self._endpoints.get(role)
        if endpoint is None:
            raise ModelRoutingError(f"no endpoint configured for {role.value}")
        return endpoint.adapter

    def endpoint_policy_identity(self, role: ModelRole) -> tuple[tuple[str, str], ...]:
        """Stable fields that can change the semantics of a cached model call."""

        endpoint = self._endpoints.get(role)
        if endpoint is None:
            raise ModelRoutingError(f"no endpoint configured for {role.value}")
        adapter = endpoint.adapter
        return (
            ("endpoint_name", endpoint.endpoint_name),
            ("registered_model", endpoint.model_name),
            ("adapter_model", str(getattr(adapter, "model", endpoint.model_name))),
            ("adapter_revision", str(getattr(adapter, "revision", "unknown"))),
            ("adapter_max_retries", str(getattr(adapter, "max_retries", 0))),
            ("structured_max_retries", str(self._structured_max_retries)),
        )

    def endpoint_runtime_identity(self, role: ModelRole) -> tuple[str, str, str]:
        """Return the explicit endpoint/model/revision identity for a frozen campaign.

        The production assembly spec deliberately owns limits, not deployment identity. A
        campaign therefore freezes the concrete endpoint values supplied to this gateway and
        checks them at the call boundary instead of inventing a model or revision default.
        """

        endpoint = self._endpoints.get(role)
        if endpoint is None:
            raise ModelRoutingError(f"no endpoint configured for {role.value}")
        adapter = endpoint.adapter
        revision = endpoint.revision
        if not revision:
            revision = getattr(adapter, "revision", None)
        if not revision:
            revision = getattr(adapter, "model_version", None)
        if not revision:
            raise ModelRoutingError(
                f"endpoint {endpoint.endpoint_name!r} has no explicit model revision"
            )
        return endpoint.endpoint_name, endpoint.model_name, str(revision)

    @staticmethod
    def _parse_structured_output(output_type: type[OutputModel], raw_text: str) -> OutputModel:
        """Parse one typed response, allowing only the observed extra-leading-brace shape.

        The local guided-json endpoint has emitted ``{{...}`` for some empty graph pages. The
        raw response is already retained verbatim by ``generate_text``; this narrow repair only
        accepts the response when removing exactly the first ``{`` makes the complete payload
        pass the same strict Pydantic contract. Any other framing or domain error remains a
        normal validation failure.
        """

        try:
            return output_type.model_validate_json(raw_text)
        except ValidationError as direct_error:
            stripped = raw_text.lstrip()
            if not stripped.startswith("{{"):
                raise
            try:
                return output_type.model_validate_json(stripped[1:])
            except ValidationError:
                raise direct_error from None

    def endpoint_budget_limits(self, role: ModelRole) -> ProviderBudgetLimits:
        """Expose the registered provider limits for a frozen campaign preflight."""

        endpoint = self._endpoints.get(role)
        if endpoint is None:
            raise ModelRoutingError(f"no endpoint configured for {role.value}")
        return self._provider_limits(endpoint)

    def resolve_effective_budget(
        self,
        request: ModelRequest,
        *,
        estimated_input_tokens: int | None = None,
        invocation_output_tokens: int | None = None,
    ) -> EffectiveBudgetResult:
        """Resolve and retain one budget without creating a ledger request or calling a model."""

        endpoint = self._endpoints.get(request.model_role)
        if endpoint is None:
            raise ModelRoutingError(f"no endpoint configured for {request.model_role.value}")
        if request.budget_source is not None:
            _, budget = self._bind_budget(request, endpoint)
            return budget
        prompt_tokens = (
            estimated_input_tokens
            if estimated_input_tokens is not None
            else max(1, (len(request.prompt.encode("utf-8")) + 2) // 3)
        )
        budget = self._budget_resolver.resolve(
            request,
            limits=self._provider_limits(endpoint),
            profile=self._budget_profile,
            estimated_input_tokens=prompt_tokens,
            invocation_output_tokens=invocation_output_tokens,
        )
        self.budget_results[request.request_id.root] = budget
        return budget

    def preflight_cumulative_token_budget(
        self,
        request: ModelRequest,
        *,
        token_budget: int,
        tokens_used: int = 0,
    ) -> EffectiveBudgetResult:
        """Admit a fully assembled request before any provider ledger marker.

        Workflow-level budgets cover actual input and output usage, while this
        gateway owns provider sequence resolution.  The check therefore runs
        at the last safe boundary: the caller supplies already-spent tokens,
        and the gateway compares them with the prompt estimate plus the
        resolved output reserve before acquiring admission or creating a
        ``REQUESTED`` ledger row.
        """

        if token_budget < 0 or tokens_used < 0:
            raise ValueError("cumulative token budget and usage must be non-negative")
        endpoint = self._endpoints.get(request.model_role)
        if endpoint is None:
            raise ModelRoutingError(f"no endpoint configured for {request.model_role.value}")
        prompt_tokens = max(1, (len(request.prompt.encode("utf-8")) + 2) // 3)
        if request.budget_source is None:
            budget = self.resolve_effective_budget(
                request,
                estimated_input_tokens=prompt_tokens,
            )
        else:
            _, budget = self._bind_budget(request, endpoint)
        if tokens_used + budget.estimated_input_tokens + budget.total_output_budget > token_budget:
            raise ModelCallCumulativeBudgetExceeded(
                request_id=request.request_id.root,
                token_budget=token_budget,
                tokens_used=tokens_used,
                estimated_input_tokens=budget.estimated_input_tokens,
                reserved_output_tokens=budget.total_output_budget,
            )
        return budget

    async def generate_text(self, request: ModelRequest) -> ModelTextResult:
        self._validate_purpose(request)
        endpoint = self._endpoints.get(request.model_role)
        if endpoint is None:
            raise ModelRoutingError(f"no endpoint configured for {request.model_role.value}")
        if self._forbid_external_calls and endpoint.adapter.is_external:
            raise ModelCallForbiddenError("external model calls are disabled for this run")

        unbound_request = request
        request, budget = self._bind_budget(request, endpoint)
        scheduling_info = self._scheduling_info(request, endpoint.endpoint_name, budget)
        lease = None
        if self._admission_controller is not None:
            lease = await self._acquire_scheduled_lease(scheduling_info)
        try:
            with self._ledger_lock:
                try:
                    requested = self._call_ledger.create_requested(
                        request,
                        effective_budget=budget,
                        reasoning_included_in_completion_tokens=(
                            endpoint.reasoning_included_in_completion_tokens
                        ),
                    )
                except ModelCallLedgerCollision:
                    existing = self._call_ledger.load(request.request_id)
                    if existing is None or existing.request_hash != model_request_hash(
                        unbound_request
                    ):
                        raise
                    requested = self._call_ledger.rebind_requested(
                        request,
                        expected_request_hash=existing.request_hash,
                        effective_budget=budget,
                        reasoning_included_in_completion_tokens=(
                            endpoint.reasoning_included_in_completion_tokens
                        ),
                    )
                if requested.status is ModelCallLedgerStatus.UNCERTAIN:
                    raise ModelCallUncertainError(
                        f"model request {requested.request_id.root} is unresolved; "
                        "reconcile before retry"
                    )
                sent_at = datetime.now(UTC)
                sent_entry = self._call_ledger.settle(
                    requested.model_copy(
                        update={
                            "provider_request_id": self._provider_request_id(
                                endpoint.adapter, request
                            ),
                            "provider_sent_at": sent_at,
                        }
                    )
                )
        except BaseException:
            # The admission lease is acquired before the durable reservation.
            # A ledger failure must not strand endpoint capacity for every
            # later request in this process.
            if lease is not None:
                lease.release()
                lease = None
            raise
        started_at = requested.requested_at
        started_clock = monotonic()
        try:
            try:
                provider_result = await asyncio.wait_for(
                    endpoint.adapter.generate(request), timeout=request.timeout_seconds
                )
            except asyncio.CancelledError as error:
                # A worker stop after the sent marker is not evidence that the
                # provider was never called.  Preserve the durable uncertainty
                # so recovery can reconcile instead of issuing a blind retry.
                with self._ledger_lock:
                    self._call_ledger.settle(
                        sent_entry.model_copy(
                            update={
                                "status": ModelCallLedgerStatus.UNCERTAIN,
                                "transport_error_type": type(error).__name__,
                            }
                        )
                    )
                raise
            except TimeoutError as error:
                with self._ledger_lock:
                    self._call_ledger.settle(
                        sent_entry.model_copy(
                            update={
                                "status": ModelCallLedgerStatus.UNCERTAIN,
                                "transport_error_type": type(error).__name__,
                            }
                        )
                    )
                raise
            except Exception as error:
                if self._is_output_incomplete(error):
                    try:
                        self._settle_output_incomplete(
                            request=request,
                            endpoint=endpoint,
                            sent_entry=sent_entry,
                            error=error,
                            started_at=started_at,
                            started_clock=started_clock,
                        )
                    except Exception as persistence_error:
                        with self._ledger_lock:
                            self._call_ledger.settle(
                                sent_entry.model_copy(
                                    update={
                                        "status": ModelCallLedgerStatus.UNCERTAIN,
                                        "transport_error_type": "RawResponsePersistenceError",
                                        "completed_at": datetime.now(UTC),
                                    }
                                )
                            )
                        raise RawResponsePersistenceError(
                            "incomplete provider response was not retained as a raw artifact"
                        ) from persistence_error
                    raise
                with self._ledger_lock:
                    self._call_ledger.settle(
                        sent_entry.model_copy(
                            update={
                                "status": ModelCallLedgerStatus.TRANSPORT_EXHAUSTED,
                                "transport_error_type": type(error).__name__,
                                "completed_at": datetime.now(UTC),
                            }
                        )
                    )
                raise
        finally:
            if lease is not None:
                lease.release()
        completed_at = datetime.now(UTC)
        latency_ms = max(0, round((monotonic() - started_clock) * 1000))
        provider_request_id = provider_result.provider_request_id or sent_entry.provider_request_id
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
        raw_hash = sha256_id(result.text.encode("utf-8"))
        try:
            raw_artifact_ref = self._persist_raw_response(
                request=request,
                request_hash=sent_entry.request_hash,
                endpoint_name=endpoint.endpoint_name,
                model_name=endpoint.model_name,
                provider_request_id=provider_request_id,
                provider_result=provider_result,
                result=result,
                raw_response_hash=raw_hash,
            )
        except Exception as error:
            with self._ledger_lock:
                self._call_ledger.settle(
                    sent_entry.model_copy(
                        update={
                            "status": ModelCallLedgerStatus.UNCERTAIN,
                            "provider_request_id": provider_request_id,
                            "transport_error_type": "RawResponsePersistenceError",
                            "completed_at": completed_at,
                        }
                    )
                )
            raise RawResponsePersistenceError(
                "provider response was not retained as a raw artifact"
            ) from error
        with self._records_lock:
            self.call_records.append(result.call_record)
            self.raw_responses[request.request_id.root] = result.text
        with self._ledger_lock:
            self._call_ledger.settle(
                sent_entry.model_copy(
                    update={
                        "status": ModelCallLedgerStatus.COMPLETED,
                        "provider_request_id": provider_request_id,
                        "raw_response_hash": raw_hash,
                        "raw_artifact_ref": raw_artifact_ref,
                        "call_record": result.call_record,
                        "completed_at": completed_at,
                    }
                )
            )
        return result

    @staticmethod
    def _is_output_incomplete(error: BaseException) -> bool:
        return getattr(error, "finish_reason", None) == "length"

    def _settle_output_incomplete(
        self,
        *,
        request: ModelRequest,
        endpoint: RegisteredModelEndpoint,
        sent_entry: ModelCallLedgerEntry,
        error: BaseException,
        started_at: datetime,
        started_clock: float,
    ) -> None:
        completed_at = datetime.now(UTC)
        raw_text = getattr(error, "raw_content", None)
        if not isinstance(raw_text, str):
            raw_text = ""

        def nonnegative_int(name: str) -> int:
            value = getattr(error, name, 0)
            return max(0, int(value)) if isinstance(value, int) else 0

        latency = getattr(error, "latency_ms", None)
        latency_ms = (
            max(0, latency)
            if isinstance(latency, int)
            else max(0, round((monotonic() - started_clock) * 1000))
        )
        model_version = str(
            getattr(endpoint.adapter, "model_version", None)
            or getattr(endpoint.adapter, "model", None)
            or endpoint.model_name
        )
        call_record = ModelCallRecord(
            request_id=request.request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            model_role=request.model_role,
            purpose=request.purpose,
            trace_id=request.trace_id,
            span_id=request.span_id,
            endpoint=endpoint.endpoint_name,
            model=endpoint.model_name,
            model_version=model_version,
            usage=ModelUsage(
                input_tokens=nonnegative_int("input_tokens"),
                output_tokens=nonnegative_int("output_tokens"),
                reasoning_tokens=nonnegative_int("reasoning_tokens"),
                cost_usd=Decimal("0"),
            ),
            latency_ms=latency_ms,
            started_at=started_at,
            completed_at=completed_at,
        )
        raw_hash = sha256_id(raw_text.encode("utf-8"))
        provider_request_id = (
            getattr(error, "provider_request_id", None) or sent_entry.provider_request_id
        )
        raw_artifact_ref = self._persist_raw_response_text(
            request=request,
            request_hash=sent_entry.request_hash,
            endpoint_name=endpoint.endpoint_name,
            model_name=endpoint.model_name,
            model_version=model_version,
            provider_request_id=provider_request_id,
            raw_response_text=raw_text,
            call_record=call_record,
            raw_response_hash=raw_hash,
            finish_reason=str(getattr(error, "finish_reason", "length")),
        )
        with self._records_lock:
            self.call_records.append(call_record)
            self.raw_responses[request.request_id.root] = raw_text
        with self._ledger_lock:
            self._call_ledger.settle(
                sent_entry.model_copy(
                    update={
                        "status": ModelCallLedgerStatus.OUTPUT_INCOMPLETE,
                        "provider_request_id": provider_request_id,
                        "raw_response_hash": raw_hash,
                        "raw_artifact_ref": raw_artifact_ref,
                        "call_record": call_record,
                        "transport_error_type": "OutputLengthError",
                        "completed_at": completed_at,
                    }
                )
            )

    def _persist_raw_response(
        self,
        *,
        request: ModelRequest,
        request_hash: ArtifactId,
        endpoint_name: str,
        model_name: str,
        provider_request_id: str | None,
        provider_result: ProviderModelResult,
        result: ModelTextResult,
        raw_response_hash: ArtifactId,
    ) -> ArtifactRef | None:
        return self._persist_raw_response_text(
            request=request,
            request_hash=request_hash,
            endpoint_name=endpoint_name,
            model_name=model_name,
            model_version=provider_result.model_version,
            provider_request_id=provider_request_id,
            raw_response_text=result.text,
            call_record=result.call_record,
            raw_response_hash=raw_response_hash,
        )

    def _persist_raw_response_text(
        self,
        *,
        request: ModelRequest,
        request_hash: ArtifactId,
        endpoint_name: str,
        model_name: str,
        model_version: str,
        provider_request_id: str | None,
        raw_response_text: str,
        call_record: ModelCallRecord,
        raw_response_hash: ArtifactId,
        finish_reason: str | None = None,
    ) -> ArtifactRef | None:
        if self._raw_artifacts is None:
            return None
        envelope = RawModelResponseArtifact(
            artifact_version="model_raw_response.v1",
            request_id=request.request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            request_hash=request_hash,
            logical_phase=request.scheduling_stage or request.purpose.value,
            model_role=request.model_role,
            purpose=request.purpose,
            endpoint=endpoint_name,
            model=model_name,
            model_version=model_version,
            provider_request_id=provider_request_id,
            prompt_hash=sha256_id(request.prompt.encode("utf-8")),
            response_schema_hash=(
                sha256_id(canonical_json_bytes(request.response_schema))
                if request.response_schema is not None
                else None
            ),
            raw_response_hash=raw_response_hash,
            raw_response_text=raw_response_text,
            call_record=call_record,
            finish_reason=finish_reason,
        )
        return self._raw_artifacts.put(
            canonical_json_bytes(envelope.model_dump(mode="json")),
            MODEL_RAW_RESPONSE_MEDIA_TYPE,
            self._raw_artifact_schema_version,
        )

    @staticmethod
    def _provider_request_id(adapter: object, request: ModelRequest) -> str | None:
        provider_identity = getattr(adapter, "provider_request_id", None)
        value = provider_identity(request) if callable(provider_identity) else provider_identity
        return value if isinstance(value, str) and value else None

    def reparse_structured_from_raw(
        self,
        request: ModelRequest,
        output_type: type[OutputModel],
        *,
        raw_artifact_ref: ArtifactRef | None = None,
    ) -> tuple[OutputModel, ModelCallRecord]:
        """Parse retained provider output without issuing another provider call."""

        if self._raw_artifacts is None:
            raise RawResponseReparseError("raw artifact repository is not configured")
        entry = self._call_ledger.load(request.request_id)
        if entry is None:
            raise RawResponseReparseError("model request is absent from the call ledger")
        reference = raw_artifact_ref or entry.raw_artifact_ref
        if reference is None:
            raise RawResponseReparseError("model request has no retained raw artifact")
        if entry.raw_artifact_ref is not None and reference != entry.raw_artifact_ref:
            raise RawResponseReparseError("raw artifact does not match the ledger reference")
        try:
            payload = self._raw_artifacts.read_verified(reference)
            envelope = RawModelResponseArtifact.model_validate_json(payload, strict=True)
        except Exception as error:
            raise RawResponseReparseError("retained raw artifact is unreadable") from error
        if (
            envelope.request_id != request.request_id
            or envelope.run_id != request.run_id
            or envelope.task_id != request.task_id
            or envelope.attempt_id != request.attempt_id
            or envelope.logical_phase != (request.scheduling_stage or request.purpose.value)
            or envelope.request_hash != entry.request_hash
            or envelope.raw_response_hash != entry.raw_response_hash
            or sha256_id(envelope.raw_response_text.encode("utf-8")) != envelope.raw_response_hash
        ):
            raise RawResponseReparseError("retained raw artifact identity does not match request")
        try:
            parsed = self._parse_structured_output(output_type, envelope.raw_response_text)
        except ValidationError as error:
            detail = json.dumps(
                error.errors(include_url=False, include_input=False),
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            with self._ledger_lock:
                current = self._call_ledger.load(request.request_id)
                if current is not None:
                    self._call_ledger.settle(
                        current.model_copy(
                            update={
                                "status": ModelCallLedgerStatus.VALIDATION_REJECTED,
                                "validation_error": detail,
                            }
                        )
                    )
            raise
        return parsed, envelope.call_record

    async def _acquire_scheduled_lease(
        self, scheduling_info: ModelRequestSchedulingInfo
    ) -> ModelRequestLease:
        """Bridge the endpoint-global blocking Condition without owning loop executors."""

        controller = self._admission_controller
        if controller is None:
            raise RuntimeError("model admission controller is not configured")
        loop = asyncio.get_running_loop()
        completed: asyncio.Future[ModelRequestLease] = loop.create_future()

        def settle_result(lease: ModelRequestLease) -> None:
            if completed.cancelled():
                lease.release()
            else:
                completed.set_result(lease)

        def settle_error(error: BaseException) -> None:
            if not completed.cancelled():
                completed.set_exception(error)

        def acquire() -> None:
            try:
                lease = controller.acquire(
                    scheduling_info,
                    timeout_seconds=scheduling_info.scheduling_timeout_seconds,
                )
            except BaseException as error:
                loop.call_soon_threadsafe(settle_error, error)
            else:
                loop.call_soon_threadsafe(settle_result, lease)

        threading.Thread(
            target=acquire,
            name=f"model-admission-{scheduling_info.request_id}",
            daemon=True,
        ).start()
        try:
            return await asyncio.shield(completed)
        except asyncio.CancelledError:
            completed.cancel()
            raise

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[OutputModel],
        *,
        json_object_framing: bool = False,
    ) -> tuple[OutputModel, ModelCallRecord]:
        schema = None if json_object_framing else output_type.model_json_schema()
        retry_request = request.model_copy(update={"response_schema": schema})
        for attempt in range(self._structured_max_retries + 1):
            constrained_request = retry_request
            result = await self.generate_text(constrained_request)
            try:
                return self._parse_structured_output(output_type, result.text), result.call_record
            except ValidationError as error:
                validation_detail = json.dumps(
                    error.errors(include_url=False, include_input=False),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
                with self._records_lock:
                    self.structured_validation_attempts.append(
                        StructuredValidationAttempt(
                            request_id=constrained_request.request_id.root,
                            call_record=result.call_record,
                            error_detail=validation_detail,
                        )
                    )
                with self._ledger_lock:
                    ledger_entry = self._call_ledger.load(constrained_request.request_id)
                if ledger_entry is None:
                    raise AssertionError("completed model call missing from ledger") from error
                with self._ledger_lock:
                    self._call_ledger.settle(
                        ledger_entry.model_copy(
                            update={
                                "status": ModelCallLedgerStatus.VALIDATION_REJECTED,
                                "validation_error": validation_detail,
                            }
                        )
                    )
                if attempt >= self._structured_max_retries:
                    # Round-19 repair: preserve the exact terminal
                    # structured-generation request identity and raw-response
                    # hash with the validation failure so the rejection audit
                    # can attribute the defect to the failing request (e.g. a
                    # Graph page) instead of the concurrent ordinary primary.
                    # `ledger_entry` is non-None here: the load above raises
                    # AssertionError when it is missing.
                    error._structured_request_id = (  # type: ignore[attr-defined]
                        constrained_request.request_id.root
                    )
                    error._structured_raw_response_hash = (  # type: ignore[attr-defined]
                        ledger_entry.raw_response_hash
                    )
                    raise
                suffix = f".schema-retry{attempt + 1}"
                try:
                    retry_id = bounded_model_request_id(request, suffix)
                except ValueError as error:
                    raise ModelRoutingError(
                        "structured retry request identity has no bounded request, attempt, "
                        "or task scope"
                    ) from error
                retry_request = request.model_copy(
                    update={
                        "request_id": retry_id,
                        "response_schema": schema,
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

    def _bind_budget(
        self,
        request: ModelRequest,
        endpoint: RegisteredModelEndpoint,
    ) -> tuple[ModelRequest, EffectiveBudgetResult]:
        if request.budget_source is not None:
            if request.max_output_tokens is None:
                raise ModelBudgetResolutionError(
                    "bound request requires a resolved max_output_tokens"
                )
            budget = self.budget_results.get(request.request_id.root)
            if budget is None:
                raise ModelBudgetResolutionError(
                    "bound request has no in-process EffectiveBudgetResult"
                )
            if request.max_output_tokens != budget.total_output_budget:
                raise ModelBudgetResolutionError(
                    "bound request output does not match its EffectiveBudgetResult"
                )
            return request, budget
        prompt_tokens = max(1, (len(request.prompt.encode("utf-8")) + 2) // 3)
        budget = self._budget_resolver.resolve(
            request,
            limits=self._provider_limits(endpoint),
            profile=self._budget_profile,
            estimated_input_tokens=prompt_tokens,
        )
        bound = request.model_copy(
            update={
                "max_output_tokens": budget.total_output_budget,
                "budget_source": budget.budget_source,
            }
        )
        self.budget_results[bound.request_id.root] = budget
        return bound, budget

    @staticmethod
    def _provider_limits(endpoint: RegisteredModelEndpoint) -> ProviderBudgetLimits:
        adapter_default = getattr(endpoint.adapter, "max_output_tokens", None)
        output_limit = endpoint.output_limit
        if output_limit is None and isinstance(adapter_default, int):
            output_limit = adapter_default
        return ProviderBudgetLimits(
            sequence_limit=endpoint.sequence_limit,
            output_limit=output_limit,
            safety_allowance_tokens=endpoint.safety_allowance_tokens,
            estimated_reasoning_reserve=endpoint.estimated_reasoning_reserve,
            default_thinking=endpoint.default_thinking,
            reasoning_included_in_completion_tokens=(
                endpoint.reasoning_included_in_completion_tokens
            ),
            global_output_cap=endpoint.global_output_cap,
        )

    def _scheduling_info(
        self,
        request: ModelRequest,
        endpoint_id: str,
        budget: EffectiveBudgetResult | None = None,
    ) -> ModelRequestSchedulingInfo:
        if budget is None:
            prompt_tokens = max(1, (len(request.prompt.encode("utf-8")) + 2) // 3)
            budget = self._budget_resolver.resolve(
                request,
                limits=ProviderBudgetLimits(),
                profile=self._budget_profile,
                estimated_input_tokens=prompt_tokens,
            )
        context_hash = sha256_id(
            json.dumps(
                {
                    "prompt": request.prompt,
                    "response_schema": request.response_schema,
                    "max_output_tokens": request.max_output_tokens,
                    "enable_thinking": request.enable_thinking,
                    "thinking_token_budget": request.thinking_token_budget,
                    "budget_source": budget.budget_source.value,
                    "resolved_output_tokens": budget.total_output_budget,
                    "model_role": request.model_role.value,
                    "purpose": request.purpose.value,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).root
        scheduling_timeout = request.scheduling_timeout_seconds or self._scheduling_timeout_seconds
        return ModelRequestSchedulingInfo(
            request_id=request.request_id.root,
            endpoint_id=endpoint_id,
            need_id=(
                request.scheduling_need_id.root if request.scheduling_need_id is not None else None
            ),
            stage=request.scheduling_stage or request.purpose.value,
            estimated_prompt_tokens=budget.estimated_input_tokens,
            reserved_output_tokens=budget.total_output_budget,
            safety_allowance_tokens=budget.safety_allowance_tokens,
            reserved_sequence_tokens=budget.reserved_sequence_tokens,
            dependency_ids=tuple(item.root for item in request.scheduling_dependency_ids),
            context_hash=context_hash,
            priority=request.scheduling_priority,
            scheduling_timeout_seconds=scheduling_timeout,
            scheduling_deadline=monotonic() + scheduling_timeout,
        )
