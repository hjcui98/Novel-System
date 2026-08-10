"""Trusted Stage 3 Writer generation and candidate materialization."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import ValidationError

from novel_agent.agents.registry import RegistryError
from novel_agent.agents.runner import AgentExecutionError, PreparedAgentRun
from novel_agent.agents.writer import WriterAgent, WriterAgentError
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.generation import (
    DraftArtifact,
    WriterAdvisoryFinding,
    WriterArtifactBasis,
    WriterContextItem,
    WriterContextSnapshot,
    WriterDraftPayload,
    WriterExecutionMetrics,
    WriterExecutionResult,
    WriterFailureCode,
    WriterInvocation,
    WriterRuntimeFingerprints,
    WriterSidecar,
    WriterTerminalStatus,
)
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.model_calls import (
    ModelCallLedgerEntry,
    ModelCallLedgerStatus,
    ModelCallRecord,
    ModelRequest,
)
from novel_agent.prompts.registry import PromptRegistryError
from novel_agent.services.artifacts import ArtifactIntegrityError, ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.model_call_ledger import ModelCallLedgerCollision
from novel_agent.services.model_gateway import (
    ModelCallForbiddenError,
    ModelGateway,
    ModelRoutingError,
    StructuredGenerationExhausted,
)
from novel_agent.skills.registry import SkillRegistryError

RAW_OUTPUT_MEDIA_TYPE = "application/vnd.novel-agent.writer-raw-output+json"
DRAFT_TEXT_MEDIA_TYPE = "text/plain; charset=utf-8"
SIDECAR_MEDIA_TYPE = "application/vnd.novel-agent.writer-sidecar+json"

_FORBIDDEN_LABEL_PARTS = ("future", "evaluator", "gold")


class WriterGenerationContractError(ValueError):
    """A trusted input failed before any model call."""


class WriterOutputContractError(ValueError):
    """A parsed model output failed a trusted Writer-only invariant."""


class WriterArtifactWriteError(RuntimeError):
    """A candidate artifact could not be durably content-addressed."""


@dataclass(frozen=True, slots=True)
class _Preflight:
    prepared: PreparedAgentRun
    frozen_prefix: str | None


@dataclass(slots=True)
class _ReplayState:
    identity_fingerprint: ArtifactId
    result_id: StableId
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    contract_prepared: PreparedAgentRun | None = None
    prepared: PreparedAgentRun | None = None
    payload: WriterDraftPayload | None = None
    call: ModelCallRecord | None = None
    raw_response: str | None = None
    frozen_prefix: str | None = None
    written: dict[str, ArtifactRef] = field(default_factory=dict)
    terminal: WriterExecutionResult | None = None


class WriterGenerationService:
    """Generate immutable Writer candidates with exact replay semantics."""

    def __init__(
        self,
        writer: WriterAgent,
        gateway: ModelGateway,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion,
        model_configuration_fingerprint: ArtifactId,
    ) -> None:
        self._writer = writer
        self._gateway = gateway
        self._artifacts = artifacts
        self._schema_version = schema_version
        self._model_configuration_fingerprint = model_configuration_fingerprint
        self._replays: dict[StableId, _ReplayState] = {}

    async def execute(
        self,
        invocation: WriterInvocation,
        request: ModelRequest,
    ) -> WriterExecutionResult:
        """Execute or exactly replay one Writer invocation."""

        identity_fingerprint = content_id(
            {
                "invocation": invocation.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
            }
        )
        state = self._replays.get(invocation.invocation_id)
        if state is None:
            state = _ReplayState(
                identity_fingerprint=identity_fingerprint,
                result_id=_result_id(identity_fingerprint),
            )
            self._replays[invocation.invocation_id] = state
        elif state.identity_fingerprint != identity_fingerprint:
            return self._failure(
                invocation,
                request,
                WriterTerminalStatus.CONTRACT_REJECTED,
                "IDEMPOTENCY_IDENTITY_COLLISION",
                retry_safe=False,
                prepared=state.prepared or state.contract_prepared,
                artifacts=(),
                entries=(),
                result_id=_result_id(identity_fingerprint),
            )

        async with state.lock:
            if state.terminal is not None:
                return state.terminal
            if state.contract_prepared is None:
                try:
                    probe_request = request.model_copy(
                        update={
                            "run_id": invocation.run_id,
                            "task_id": invocation.task_id,
                            "timeout_seconds": min(
                                request.timeout_seconds,
                                invocation.budget.timeout_seconds,
                            ),
                        }
                    )
                    state.contract_prepared = self._writer.prepare_contract(
                        invocation,
                        probe_request,
                    )
                except (Exception, asyncio.CancelledError):
                    # A typed preflight result still needs explicit fingerprints.  If the
                    # immutable contract itself cannot be resolved, _fingerprints emits
                    # domain-separated "unavailable" identities rather than pretending
                    # the Writer configuration hash is a prompt/tool/spec hash.
                    state.contract_prepared = None

            try:
                preflight = self._preflight(invocation, request, state)
            except (
                ArtifactIntegrityError,
                UnicodeError,
                WriterGenerationContractError,
                WriterAgentError,
                AgentExecutionError,
                RegistryError,
                PromptRegistryError,
                SkillRegistryError,
                ValueError,
            ) as error:
                result = self._failure(
                    invocation,
                    request,
                    WriterTerminalStatus.CONTRACT_REJECTED,
                    _safe_error_detail(error),
                    retry_safe=False,
                    prepared=state.prepared or state.contract_prepared,
                    artifacts=tuple(state.written.values()),
                )
                state.terminal = result
                return result
            except Exception as error:
                result = self._failure(
                    invocation,
                    request,
                    WriterTerminalStatus.FATAL,
                    _safe_error_detail(error),
                    retry_safe=False,
                    prepared=state.prepared or state.contract_prepared,
                    artifacts=tuple(state.written.values()),
                )
                state.terminal = result
                return result

            state.prepared = preflight.prepared
            state.frozen_prefix = preflight.frozen_prefix
            if invocation.writing_task.blocking_gaps:
                result = self._failure(
                    invocation,
                    request,
                    WriterTerminalStatus.NEEDS_CONTEXT,
                    "WRITING_TASK_BLOCKING_GAPS",
                    retry_safe=False,
                    prepared=preflight.prepared,
                    artifacts=(),
                )
                state.terminal = result
                return result
            if invocation.budget.max_model_calls == 0:
                result = self._failure(
                    invocation,
                    request,
                    WriterTerminalStatus.BUDGET_EXHAUSTED,
                    "MODEL_CALL_BUDGET_EXHAUSTED",
                    retry_safe=False,
                    prepared=preflight.prepared,
                    artifacts=(),
                )
                state.terminal = result
                return result

            if state.payload is None or state.call is None or state.raw_response is None:
                try:
                    execution = await self._writer.execute(preflight.prepared)
                except asyncio.CancelledError as error:
                    result = self._failure(
                        invocation,
                        request,
                        WriterTerminalStatus.CANCELLED,
                        _safe_error_detail(error),
                        retry_safe=True,
                        prepared=preflight.prepared,
                        artifacts=tuple(state.written.values()),
                    )
                    state.terminal = result
                    return result
                except (ValidationError, StructuredGenerationExhausted) as error:
                    try:
                        rejected_artifacts = self._persist_rejected_raw(request, state)
                    except Exception as artifact_error:
                        result = self._failure(
                            invocation,
                            request,
                            WriterTerminalStatus.ARTIFACT_WRITE_FAILED,
                            _safe_error_detail(artifact_error),
                            retry_safe=True,
                            prepared=preflight.prepared,
                            artifacts=tuple(state.written.values()),
                        )
                        state.terminal = result
                        return result
                    result = self._failure(
                        invocation,
                        request,
                        WriterTerminalStatus.MODEL_OUTPUT_REJECTED,
                        _safe_error_detail(error),
                        retry_safe=False,
                        prepared=preflight.prepared,
                        artifacts=rejected_artifacts,
                    )
                    state.terminal = result
                    return result
                except (ModelRoutingError, ModelCallForbiddenError, TimeoutError) as error:
                    result = self._failure(
                        invocation,
                        request,
                        WriterTerminalStatus.MODEL_UNAVAILABLE,
                        _safe_error_detail(error),
                        retry_safe=True,
                        prepared=preflight.prepared,
                        artifacts=tuple(state.written.values()),
                    )
                    state.terminal = result
                    return result
                except ModelCallLedgerCollision as error:
                    result = self._failure(
                        invocation,
                        request,
                        WriterTerminalStatus.CONTRACT_REJECTED,
                        _safe_error_detail(error),
                        retry_safe=False,
                        prepared=preflight.prepared,
                        artifacts=tuple(state.written.values()),
                    )
                    state.terminal = result
                    return result
                except (WriterAgentError, AgentExecutionError) as error:
                    result = self._failure(
                        invocation,
                        request,
                        WriterTerminalStatus.CONTRACT_REJECTED,
                        _safe_error_detail(error),
                        retry_safe=False,
                        prepared=preflight.prepared,
                        artifacts=tuple(state.written.values()),
                    )
                    state.terminal = result
                    return result
                except Exception as error:
                    entries = self._entries(request)
                    terminal = (
                        WriterTerminalStatus.MODEL_UNAVAILABLE
                        if any(
                            item.status
                            in {
                                ModelCallLedgerStatus.TRANSPORT_EXHAUSTED,
                                ModelCallLedgerStatus.UNCERTAIN,
                            }
                            for item in entries
                        )
                        else WriterTerminalStatus.FATAL
                    )
                    result = self._failure(
                        invocation,
                        request,
                        terminal,
                        _safe_error_detail(error),
                        retry_safe=terminal is WriterTerminalStatus.MODEL_UNAVAILABLE,
                        prepared=preflight.prepared,
                        artifacts=tuple(state.written.values()),
                        entries=entries,
                    )
                    state.terminal = result
                    return result

                state.payload = execution.output
                state.call = execution.model_call
                state.raw_response = self._gateway.raw_responses.get(
                    execution.model_call.request_id.root
                )
                if state.raw_response is None:
                    result = self._failure(
                        invocation,
                        request,
                        WriterTerminalStatus.FATAL,
                        "MODEL_RAW_RESPONSE_MISSING",
                        retry_safe=False,
                        prepared=preflight.prepared,
                        artifacts=tuple(state.written.values()),
                    )
                    state.terminal = result
                    return result

            entries = self._entries(request)
            metrics = _metrics(entries)
            if (
                metrics.model_call_count > invocation.budget.max_model_calls
                or metrics.input_tokens > invocation.budget.input_token_limit
                or metrics.output_tokens > invocation.budget.output_token_limit
            ):
                result = self._failure(
                    invocation,
                    request,
                    WriterTerminalStatus.BUDGET_EXHAUSTED,
                    "MODEL_USAGE_BUDGET_EXHAUSTED",
                    retry_safe=False,
                    prepared=preflight.prepared,
                    artifacts=tuple(state.written.values()),
                    entries=entries,
                )
                state.terminal = result
                return result

            try:
                return self._materialize(invocation, request, state, entries)
            except WriterOutputContractError as error:
                result = self._failure(
                    invocation,
                    request,
                    WriterTerminalStatus.MODEL_OUTPUT_REJECTED,
                    _safe_error_detail(error),
                    retry_safe=False,
                    prepared=preflight.prepared,
                    artifacts=tuple(state.written.values()),
                    entries=entries,
                )
                state.terminal = result
                return result
            except WriterArtifactWriteError as error:
                return self._failure(
                    invocation,
                    request,
                    WriterTerminalStatus.ARTIFACT_WRITE_FAILED,
                    _safe_error_detail(error),
                    retry_safe=True,
                    prepared=preflight.prepared,
                    artifacts=tuple(state.written.values()),
                    entries=entries,
                )
            except Exception as error:
                result = self._failure(
                    invocation,
                    request,
                    WriterTerminalStatus.FATAL,
                    _safe_error_detail(error),
                    retry_safe=False,
                    prepared=preflight.prepared,
                    artifacts=tuple(state.written.values()),
                    entries=entries,
                )
                state.terminal = result
                return result

    def _persist_rejected_raw(
        self,
        request: ModelRequest,
        state: _ReplayState,
    ) -> tuple[ArtifactRef, ...]:
        for entry in self._entries(request):
            raw = self._gateway.raw_responses.get(entry.request_id.root)
            if raw is None:
                continue
            key = f"rejected_raw:{entry.request_id.root}"
            if key not in state.written:
                state.written[key] = self._put_artifact(
                    raw.encode("utf-8"),
                    RAW_OUTPUT_MEDIA_TYPE,
                )
        return tuple(state.written.values())

    def _preflight(
        self,
        invocation: WriterInvocation,
        request: ModelRequest,
        state: _ReplayState,
    ) -> _Preflight:
        if request.run_id != invocation.run_id or request.task_id != invocation.task_id:
            raise WriterGenerationContractError(
                "model request run/task does not match WriterInvocation"
            )
        basis = invocation.basis
        if (
            basis.base_commit != invocation.context_package.base_commit
            or basis.snapshot_id != invocation.context_package.snapshot_id
            or basis.context_id != invocation.context_package.context_id
        ):
            raise WriterGenerationContractError("Writer basis differs from ContextPackage")
        if basis.model_configuration_fingerprint != self._model_configuration_fingerprint:
            raise WriterGenerationContractError("Writer model configuration fingerprint mismatch")
        self._validate_context(invocation.context_package, basis)
        expected = _expected_input_artifacts(invocation)
        supplied = {item.artifact_id: item for item in invocation.input_artifacts}
        if len(supplied) != len(invocation.input_artifacts):
            raise WriterGenerationContractError("Writer input artifacts are not unique")
        expected_by_id = {item.artifact_id: item for item in expected}
        if supplied != expected_by_id:
            raise WriterGenerationContractError(
                "Writer input artifacts do not exactly match trusted bindings"
            )

        data = {
            artifact.artifact_id: self._artifacts.read_verified(artifact)
            for artifact in invocation.input_artifacts
        }
        context_bytes = canonical_json_bytes(invocation.context_package.model_dump(mode="json"))
        if data[basis.context_artifact.artifact_id] != context_bytes:
            raise WriterGenerationContractError("ContextPackage artifact is not its canonical JSON")
        writing_bytes = canonical_json_bytes(invocation.writing_task.model_dump(mode="json"))
        if data[basis.writing_contract_artifact.artifact_id] != writing_bytes:
            raise WriterGenerationContractError(
                "WritingTaskContract artifact is not its canonical JSON"
            )
        if (
            basis.memory_gate_report is not None
            and basis.memory_gate_artifact is not None
            and data[basis.memory_gate_artifact.artifact_id]
            != canonical_json_bytes(basis.memory_gate_report.model_dump(mode="json"))
        ):
            raise WriterGenerationContractError("Memory Gate artifact is not its canonical JSON")

        source_payloads: dict[str, object] = {
            "plan": _decode_payload(data[basis.plan_artifact.artifact_id]),
            "project_profile": _decode_payload(data[basis.project_profile_artifact.artifact_id]),
        }
        for binding in basis.source_artifacts:
            if (
                binding.source_id not in basis.future_isolation_attestation.canonical_source_ids
                or binding.source_id in basis.future_isolation_attestation.evaluator_only_source_ids
            ):
                raise WriterGenerationContractError(
                    "Writer source binding violates future isolation"
                )
            payload = _decode_payload(data[binding.source_artifact.artifact_id])
            _reject_unsafe_metadata(payload)
            source_payloads[f"source:{binding.source_id.root}"] = payload

        prior_text: str | None = None
        if invocation.prior_draft is not None:
            prior_text = data[invocation.prior_draft.text_artifact.artifact_id].decode("utf-8")

        frozen_prefix: str | None = None
        if invocation.continuation_boundary is not None:
            boundary = invocation.continuation_boundary
            frozen_prefix = data[boundary.frozen_prefix_artifact.artifact_id].decode("utf-8")
            if len(frozen_prefix) != boundary.frozen_prefix_characters:
                raise WriterGenerationContractError(
                    "continuation boundary character count mismatch"
                )
            if prior_text is None or not prior_text.startswith(frozen_prefix):
                raise WriterGenerationContractError(
                    "continuation boundary is not a prefix of the prior draft"
                )
        if invocation.rewrite_directive is not None:
            _decode_payload(data[invocation.rewrite_directive.directive_artifact.artifact_id])

        safe_request = request.model_copy(
            update={
                "timeout_seconds": min(
                    request.timeout_seconds,
                    invocation.budget.timeout_seconds,
                )
            }
        )
        if state.prepared is None:
            prepared = self._writer.prepare(
                invocation,
                safe_request,
                source_payloads=source_payloads,
                prior_text=prior_text,
            )
        else:
            prepared = state.prepared
        if prepared.configuration_fingerprint != basis.configuration_fingerprint:
            raise WriterGenerationContractError(
                "Writer AgentSpec configuration fingerprint mismatch"
            )
        return _Preflight(prepared=prepared, frozen_prefix=frozen_prefix)

    def _validate_context(
        self,
        context: WriterContextSnapshot,
        basis: WriterArtifactBasis,
    ) -> None:
        for item in _context_items(context):
            if item.source_commit != basis.base_commit:
                raise WriterGenerationContractError(
                    f"Writer context item {item.item_id.root} has the wrong source commit"
                )
            if item.snapshot_id != basis.snapshot_id:
                raise WriterGenerationContractError(
                    f"Writer context item {item.item_id.root} has the wrong snapshot"
                )
            if item.access_scope != "writer_safe":
                raise WriterGenerationContractError(
                    f"Writer context item {item.item_id.root} is not writer_safe"
                )
            labels = (item.information_label, *item.derivation_taint)
            if any(
                forbidden in label.lower()
                for label in labels
                for forbidden in _FORBIDDEN_LABEL_PARTS
            ):
                raise WriterGenerationContractError(
                    f"Writer context item {item.item_id.root} contains forbidden taint"
                )

    def _materialize(
        self,
        invocation: WriterInvocation,
        request: ModelRequest,
        state: _ReplayState,
        entries: tuple[ModelCallLedgerEntry, ...],
    ) -> WriterExecutionResult:
        prepared = state.prepared
        payload = state.payload
        call = state.call
        raw_response = state.raw_response
        if prepared is None or payload is None or call is None or raw_response is None:
            raise AssertionError("Writer replay state is incomplete")

        if "raw" not in state.written:
            state.written["raw"] = self._put_artifact(
                raw_response.encode("utf-8"),
                RAW_OUTPUT_MEDIA_TYPE,
            )
        self._validate_output(invocation, payload, state.frozen_prefix)
        if "text" not in state.written:
            state.written["text"] = self._put_artifact(
                payload.draft_text.encode("utf-8"),
                DRAFT_TEXT_MEDIA_TYPE,
            )
        sidecar = _sidecar(payload)
        if "sidecar" not in state.written:
            state.written["sidecar"] = self._put_artifact(
                canonical_json_bytes(sidecar.model_dump(mode="json")),
                SIDECAR_MEDIA_TYPE,
            )

        output_artifacts = (
            state.written["raw"],
            state.written["text"],
            state.written["sidecar"],
        )
        receipt = self._writer.receipt(
            prepared,
            call,
            output_artifacts=output_artifacts,
            unresolved=payload.unresolved_questions,
        )
        parent_draft_id = (
            invocation.prior_draft.draft_id if invocation.prior_draft is not None else None
        )
        draft_id = content_id(
            {
                "mode": invocation.mode.value,
                "basis": invocation.basis.model_dump(mode="json"),
                "text_artifact": state.written["text"].model_dump(mode="json"),
                "sidecar_artifact": state.written["sidecar"].model_dump(mode="json"),
                "raw_output_artifact": state.written["raw"].model_dump(mode="json"),
                "parent_draft_id": (parent_draft_id.root if parent_draft_id is not None else None),
            }
        )
        draft = DraftArtifact(
            draft_id=draft_id,
            mode=invocation.mode,
            basis=invocation.basis,
            text_artifact=state.written["text"],
            sidecar_artifact=state.written["sidecar"],
            raw_output_artifact=state.written["raw"],
            parent_draft_id=parent_draft_id,
            writer_receipt=receipt,
            model_call_ids=(call.request_id,),
            created_at=call.completed_at,
        )
        result = WriterExecutionResult(
            result_id=state.result_id,
            invocation_id=invocation.invocation_id,
            run_id=invocation.run_id,
            task_id=invocation.task_id,
            status=WriterTerminalStatus.COMPLETED,
            basis=invocation.basis,
            draft=draft,
            receipt=receipt,
            artifacts=output_artifacts,
            fingerprints=_fingerprints(prepared, invocation.basis),
            metrics=_metrics(entries),
            retry_safe=True,
        )
        state.terminal = result
        return result

    def _put_artifact(self, data: bytes, media_type: str) -> ArtifactRef:
        try:
            return self._artifacts.put(data, media_type, self._schema_version)
        except Exception as error:
            raise WriterArtifactWriteError(
                f"failed to write {media_type} Writer artifact"
            ) from error

    @staticmethod
    def _validate_output(
        invocation: WriterInvocation,
        payload: WriterDraftPayload,
        frozen_prefix: str | None,
    ) -> None:
        length = len(payload.draft_text)
        policy = invocation.writing_task.length_policy
        if length < policy.minimum_characters or length > policy.maximum_characters:
            raise WriterOutputContractError("Writer draft violates trusted length policy")
        if invocation.continuation_boundary is not None:
            if frozen_prefix is None or not payload.draft_text.startswith(frozen_prefix):
                raise WriterOutputContractError("CONTINUE output changed the frozen prefix")
            if len(payload.draft_text) <= len(frozen_prefix):
                raise WriterOutputContractError(
                    "CONTINUE output did not add text after the frozen prefix"
                )

    def _failure(
        self,
        invocation: WriterInvocation,
        request: ModelRequest,
        status: WriterTerminalStatus,
        detail: str,
        *,
        retry_safe: bool,
        prepared: PreparedAgentRun | None,
        artifacts: tuple[ArtifactRef, ...],
        entries: tuple[ModelCallLedgerEntry, ...] | None = None,
        result_id: StableId | None = None,
    ) -> WriterExecutionResult:
        resolved_entries = self._entries(request) if entries is None else entries
        resolved_detail = detail or status.value
        if prepared is None:
            resolved_detail = "RUNTIME_CONTRACT_FINGERPRINTS_UNAVAILABLE; " + resolved_detail
        return WriterExecutionResult(
            result_id=result_id
            or _result_id(
                content_id(
                    {
                        "invocation": invocation.model_dump(mode="json"),
                        "request": request.model_dump(mode="json"),
                    }
                )
            ),
            invocation_id=invocation.invocation_id,
            run_id=invocation.run_id,
            task_id=invocation.task_id,
            status=status,
            basis=invocation.basis,
            artifacts=artifacts,
            fingerprints=_fingerprints(prepared, invocation.basis),
            metrics=_metrics(resolved_entries),
            retry_safe=retry_safe,
            failure_code=WriterFailureCode(status.value),
            failure_detail=resolved_detail,
        )

    def _entries(self, request: ModelRequest) -> tuple[ModelCallLedgerEntry, ...]:
        return self._gateway.call_ledger.list_for_prefix(request.request_id.root)


def _expected_input_artifacts(invocation: WriterInvocation) -> tuple[ArtifactRef, ...]:
    basis = invocation.basis
    items: list[ArtifactRef] = [
        basis.context_artifact,
        basis.writing_contract_artifact,
        basis.plan_artifact,
        basis.project_profile_artifact,
        *((basis.memory_gate_artifact,) if basis.memory_gate_artifact is not None else ()),
        *(binding.source_artifact for binding in basis.source_artifacts),
    ]
    if invocation.prior_draft is not None:
        items.extend(
            (
                invocation.prior_draft.text_artifact,
                invocation.prior_draft.sidecar_artifact,
                invocation.prior_draft.raw_output_artifact,
            )
        )
    if invocation.continuation_boundary is not None:
        items.append(invocation.continuation_boundary.frozen_prefix_artifact)
    if invocation.rewrite_directive is not None:
        items.append(invocation.rewrite_directive.directive_artifact)
    unique: dict[ArtifactId, ArtifactRef] = {}
    for item in items:
        existing = unique.get(item.artifact_id)
        if existing is not None and existing != item:
            raise WriterGenerationContractError(
                "one artifact identity has conflicting trusted metadata"
            )
        unique[item.artifact_id] = item
    return tuple(unique.values())


def _context_items(context: WriterContextSnapshot) -> Iterable[WriterContextItem]:
    yield from context.items


def _decode_payload(data: bytes) -> object:
    text = data.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _reject_unsafe_metadata(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text == "access_scope":
                values = item if isinstance(item, (list, tuple)) else (item,)
                if any(str(label).lower() != "writer_safe" for label in values):
                    raise WriterGenerationContractError(
                        "Writer source payload access scope is not writer_safe"
                    )
            elif key_text in {"derivation_taint", "information_label"}:
                _reject_unsafe_label(item)
            _reject_unsafe_metadata(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_unsafe_metadata(item)


def _reject_unsafe_label(value: object) -> None:
    values = value if isinstance(value, (list, tuple)) else (value,)
    for item in values:
        text = str(item).lower()
        if any(forbidden in text for forbidden in _FORBIDDEN_LABEL_PARTS):
            raise WriterGenerationContractError("Writer source payload contains forbidden taint")


def _sidecar(payload: WriterDraftPayload) -> WriterSidecar:
    findings: list[WriterAdvisoryFinding] = []
    for index, hint in enumerate(payload.declared_memory_hints):
        count = _occurrence_count(payload.draft_text, hint.evidence_quote)
        if count == 1:
            continue
        if count == 0:
            code = "EVIDENCE_QUOTE_NOT_FOUND"
            message = "Declared evidence quote was not found in the draft text."
        else:
            code = "EVIDENCE_QUOTE_AMBIGUOUS"
            message = "Declared evidence quote occurs more than once in the draft text."
        findings.append(
            WriterAdvisoryFinding(
                hint_index=index,
                evidence_quote=hint.evidence_quote,
                occurrence_count=count,
                code=code,
                message=message,
            )
        )
    return WriterSidecar(
        declared_memory_hints=payload.declared_memory_hints,
        unresolved_questions=payload.unresolved_questions,
        self_observations=payload.self_observations,
        advisory_findings=tuple(findings),
    )


def _occurrence_count(text: str, quote: str) -> int:
    count = 0
    start = 0
    while True:
        index = text.find(quote, start)
        if index < 0:
            return count
        count += 1
        start = index + 1


def _metrics(entries: tuple[ModelCallLedgerEntry, ...]) -> WriterExecutionMetrics:
    calls = tuple(item.call_record for item in entries if item.call_record is not None)
    return WriterExecutionMetrics(
        model_called=bool(entries),
        model_call_count=len(entries),
        input_tokens=sum(call.usage.input_tokens for call in calls),
        output_tokens=sum(call.usage.output_tokens for call in calls),
        cost_usd=sum((call.usage.cost_usd for call in calls), start=Decimal("0")),
        latency_ms=sum(call.latency_ms for call in calls),
    )


def _fingerprints(
    prepared: PreparedAgentRun | None,
    basis: WriterArtifactBasis,
) -> WriterRuntimeFingerprints:
    if prepared is None:
        return WriterRuntimeFingerprints(
            agent_spec_fingerprint=_unavailable_fingerprint("agent_spec", basis),
            prompt_fingerprint=_unavailable_fingerprint("prompt", basis),
            skill_fingerprints=(_unavailable_fingerprint("skills", basis),),
            tool_policy_fingerprint=_unavailable_fingerprint("tool_policy", basis),
            configuration_fingerprint=basis.configuration_fingerprint,
            model_configuration_fingerprint=basis.model_configuration_fingerprint,
        )
    return WriterRuntimeFingerprints(
        agent_spec_fingerprint=prepared.spec.content_hash,
        prompt_fingerprint=prepared.prompt_fingerprint,
        skill_fingerprints=tuple(item.content_hash for item in prepared.skill_refs),
        tool_policy_fingerprint=prepared.spec.tool_policy.content_hash,
        configuration_fingerprint=basis.configuration_fingerprint,
        model_configuration_fingerprint=basis.model_configuration_fingerprint,
    )


def _unavailable_fingerprint(kind: str, basis: WriterArtifactBasis) -> ArtifactId:
    return content_id(
        {
            "writer_runtime_fingerprint": "unavailable",
            "kind": kind,
            "writer_configuration_fingerprint": basis.configuration_fingerprint.root,
            "model_configuration_fingerprint": (basis.model_configuration_fingerprint.root),
        }
    )


def _result_id(identity_fingerprint: ArtifactId) -> StableId:
    digest = identity_fingerprint.root.removeprefix("sha256:")
    return StableId(f"writer-result.{digest[:24]}")


def _safe_error_detail(error: BaseException) -> str:
    if isinstance(error, ValidationError):
        return json.dumps(
            error.errors(include_url=False, include_input=False),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )[:4096]
    detail = str(error).strip()
    return f"{type(error).__name__}: {detail or 'no detail'}"[:4096]


__all__ = [
    "DRAFT_TEXT_MEDIA_TYPE",
    "RAW_OUTPUT_MEDIA_TYPE",
    "SIDECAR_MEDIA_TYPE",
    "WriterArtifactWriteError",
    "WriterGenerationContractError",
    "WriterGenerationService",
    "WriterOutputContractError",
]
