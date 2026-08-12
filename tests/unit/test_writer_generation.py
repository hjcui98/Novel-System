from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import NoReturn

import pytest
from pydantic import BaseModel

import novel_agent.services.writer_generation as writer_generation
from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.agents import AgentRegistry, StructuredAgentRunner
from novel_agent.agents.runner import PreparedAgentRun
from novel_agent.agents.writer import WriterAgent, build_writer_contract_bundle
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.generation import (
    ContinuationBoundary,
    DeclaredMemoryHint,
    DraftArtifact,
    MemoryHintChangeKind,
    RewriteDirective,
    RewriteScope,
    WriterArtifactBasis,
    WriterBudget,
    WriterContextItem,
    WriterContextSnapshot,
    WriterDraftPayload,
    WriterExecutionResult,
    WriterInvocation,
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
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelCallRecord,
    ModelRequest,
    ModelRole,
    ModelUsage,
    ProviderModelResult,
)
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentSpec,
    FutureIsolationAttestation,
)
from novel_agent.ports.object_store import ObjectStat
from novel_agent.prompts import PromptRegistry
from novel_agent.prompts.registry import content_hash
from novel_agent.services.artifacts import ArtifactRepository, object_key
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.model_call_ledger import ModelCallLedgerCollision
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.stage2_gate import Stage2GateEvaluator
from novel_agent.services.writer_generation import (
    DRAFT_TEXT_MEDIA_TYPE,
    RAW_OUTPUT_MEDIA_TYPE,
    SIDECAR_MEDIA_TYPE,
    WriterGenerationContractError,
    WriterGenerationService,
)
from novel_agent.skills import SkillRegistry
from tests.unit.test_stage2_gate import evidence

VERSION = SchemaVersion("1.0.0")
BASE_COMMIT = CommitId("sha256:" + "1" * 64)
SNAPSHOT = StableId("snapshot.writer-generation.20")
MODEL_CONFIGURATION = ArtifactId("sha256:" + "e" * 64)


class FaultInjectingStore:
    def __init__(self, root: Path) -> None:
        self.delegate = FilesystemObjectStore(root)
        self.failure_call: int | None = None
        self.write_calls = 0

    def arm(self, failure_call: int | None) -> None:
        self.failure_call = failure_call
        self.write_calls = 0

    def put_if_absent(self, key: str, data: bytes, media_type: str) -> ObjectStat:
        self.write_calls += 1
        if self.failure_call == self.write_calls:
            raise OSError(f"injected artifact write failure {self.write_calls}")
        return self.delegate.put_if_absent(key, data, media_type)

    def get(self, key: str) -> bytes:
        return self.delegate.get(key)

    def stat(self, key: str) -> ObjectStat:
        return self.delegate.stat(key)


class CancelledEndpoint(FakeModelEndpoint):
    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.requests.append(request)
        raise asyncio.CancelledError


class SequenceEndpoint(FakeModelEndpoint):
    def __init__(self, responses: tuple[str, ...]) -> None:
        super().__init__("")
        self._responses = iter(responses)

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.response_text = next(self._responses)
        return await super().generate(request)


class UsageEndpoint(FakeModelEndpoint):
    def __init__(self, response: str, *, input_tokens: int, output_tokens: int) -> None:
        super().__init__(response)
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        result = await super().generate(request)
        return result.model_copy(
            update={
                "usage": ModelUsage(
                    input_tokens=self._input_tokens,
                    output_tokens=self._output_tokens,
                    cost_usd=Decimal("0.25"),
                )
            }
        )


class DiscardingRawResponses(dict[str, str]):
    def __setitem__(self, key: str, value: str) -> None:
        del key, value


@dataclass(slots=True)
class Harness:
    root: Path
    store: FaultInjectingStore
    artifacts: ArtifactRepository
    endpoint: FakeModelEndpoint
    gateway: ModelGateway
    writer: WriterAgent
    service: WriterGenerationService
    specs: dict[AgentMode, AgentSpec]


def _payload(
    text: str = "Lin studies the moonlit groove and opens the gate with a mirror.",
    *,
    hints: tuple[DeclaredMemoryHint, ...] = (),
) -> WriterDraftPayload:
    return WriterDraftPayload(
        draft_text=text,
        declared_memory_hints=hints,
        unresolved_questions=("The guard beyond the gate is unknown.",),
        self_observations=("The injured-arm constraint is preserved.",),
    )


def _harness(
    tmp_path: Path,
    *,
    response: str | None = None,
    error: Exception | None = None,
    endpoint: FakeModelEndpoint | None = None,
    structured_max_retries: int = 0,
) -> Harness:
    bundle = build_writer_contract_bundle()
    selected = endpoint or FakeModelEndpoint(
        response or _payload().model_dump_json(),
        error=error,
    )
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="writer-generation-fake",
                model_name="writer-generation-fake",
                adapter=selected,
            ),
        ),
        forbid_external_calls=True,
        structured_max_retries=structured_max_retries,
    )
    prompts = PromptRegistry(bundle.prompt_templates)
    skills = SkillRegistry(bundle.skill_templates)
    runner = StructuredAgentRunner(
        gateway,
        AgentRegistry(bundle.agent_specs),
        prompts,
        skills,
    )
    writer = WriterAgent(runner, prompts, skills)
    root = tmp_path / "objects"
    store = FaultInjectingStore(root)
    artifacts = ArtifactRepository(store)
    return Harness(
        root=root,
        store=store,
        artifacts=artifacts,
        endpoint=selected,
        gateway=gateway,
        writer=writer,
        service=WriterGenerationService(
            writer,
            gateway,
            artifacts,
            VERSION,
            MODEL_CONFIGURATION,
        ),
        specs={spec.mode: spec for spec in bundle.agent_specs},
    )


def _context(*, item: WriterContextItem | None = None) -> WriterContextSnapshot:
    selected = item or WriterContextItem(
        item_id=StableId("item.writer-generation.constraint"),
        category="mandatory_constraints",
        source_commit=BASE_COMMIT,
        snapshot_id=SNAPSHOT,
        text="The gate opens only under moonlight.",
        mandatory=True,
    )
    return WriterContextSnapshot(
        context_id=StableId("context.writer-generation.21"),
        base_commit=BASE_COMMIT,
        snapshot_id=SNAPSHOT,
        task_contract="writer-generation task contract",
        items=(selected,),
        budget_report={
            "token_budget": 1000,
            "mandatory_tokens": 20,
            "optional_tokens": 0,
        },
    )


def _task(*, blocking: bool = False) -> WritingTaskContract:
    return WritingTaskContract(
        contract_id=StableId("writing-contract.writer-generation.21"),
        target_chapter=21,
        target_scenes=(StableId("scene.writer-generation.21.1"),),
        pov="Lin",
        narrative_person="third person limited",
        chapter_goal="Enter the tower without violating the injury constraint.",
        required_beats=("Observe the gate.", "Redirect moonlight."),
        mandatory_constraints=("Do not force the gate with the injured arm.",),
        forbidden_reveals=("Do not reveal the tower's final secret.",),
        length_policy=WritingLengthPolicy(
            minimum_characters=20,
            target_characters=100,
            maximum_characters=500,
        ),
        blocking_gaps=("missing gate state",) if blocking else (),
    )


def _configuration_fingerprint(spec: AgentSpec) -> ArtifactId:
    return content_hash(canonical_json_bytes(spec.model_dump(mode="json")))


def _put_json(
    artifacts: ArtifactRepository,
    value: object,
    media_type: str = "application/json",
) -> ArtifactRef:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return artifacts.put(canonical_json_bytes(payload), media_type, VERSION)


def _invocation(
    harness: Harness,
    *,
    mode: AgentMode = AgentMode.DRAFT,
    suffix: str = "base",
    context: WriterContextSnapshot | None = None,
    task: WritingTaskContract | None = None,
    prior: DraftArtifact | None = None,
    frozen_prefix: str | None = None,
    source_payload: object | None = None,
    max_model_calls: int = 1,
) -> WriterInvocation:
    selected_context = context or _context()
    selected_task = task or _task()
    context_ref = _put_json(harness.artifacts, selected_context)
    writing_ref = _put_json(harness.artifacts, selected_task)
    plan_ref = _put_json(harness.artifacts, {"goal": "enter the tower"})
    profile_ref = _put_json(harness.artifacts, {"voice": "restrained"})
    input_artifacts: list[ArtifactRef] = [
        context_ref,
        writing_ref,
        plan_ref,
        profile_ref,
    ]
    source_bindings: tuple[WriterSourceBinding, ...] = ()
    canonical_source_ids = (StableId("source.writer-generation.visible"),)
    if source_payload is not None:
        source_ref = _put_json(harness.artifacts, source_payload)
        source_bindings = (
            WriterSourceBinding(
                source_id=canonical_source_ids[0],
                source_artifact=source_ref,
            ),
        )
        input_artifacts.append(source_ref)
    basis = WriterArtifactBasis(
        project_id=ProjectId("project.writer-generation"),
        base_commit=BASE_COMMIT,
        snapshot_id=SNAPSHOT,
        context_id=selected_context.context_id,
        context_artifact=context_ref,
        context_fingerprint=context_ref.artifact_id,
        writing_contract_artifact=writing_ref,
        plan_artifact=plan_ref,
        project_profile_artifact=profile_ref,
        configuration_fingerprint=_configuration_fingerprint(harness.specs[mode]),
        model_configuration_fingerprint=MODEL_CONFIGURATION,
        future_isolation_attestation=FutureIsolationAttestation(
            attestation_id=StableId("attestation.writer-generation.20"),
            checkpoint_chapter=20,
            canonical_source_ids=canonical_source_ids,
            evaluator_only_source_ids=(StableId("source.writer-generation.future"),),
            passed=True,
            configuration_fingerprint=ArtifactId("sha256:" + "f" * 64),
        ),
        source_artifacts=source_bindings,
    )
    boundary: ContinuationBoundary | None = None
    directive: RewriteDirective | None = None
    if prior is not None:
        input_artifacts.extend(
            (prior.text_artifact, prior.sidecar_artifact, prior.raw_output_artifact)
        )
    if mode is AgentMode.CONTINUE:
        assert prior is not None and frozen_prefix is not None
        prefix_ref = harness.artifacts.put(
            frozen_prefix.encode("utf-8"),
            DRAFT_TEXT_MEDIA_TYPE,
            VERSION,
        )
        input_artifacts.append(prefix_ref)
        boundary = ContinuationBoundary(
            parent_draft_id=prior.draft_id,
            frozen_prefix_artifact=prefix_ref,
            frozen_prefix_characters=len(frozen_prefix),
        )
    elif mode is AgentMode.MAJOR_REWRITE:
        assert prior is not None
        directive_ref = _put_json(
            harness.artifacts,
            {
                "scope": "major_rewrite",
                "instructions": ["Change the entrance action."],
            },
        )
        input_artifacts.append(directive_ref)
        directive = RewriteDirective(
            directive_id=StableId(f"directive.writer-generation.{suffix}"),
            parent_draft_id=prior.draft_id,
            scope=RewriteScope.MAJOR_REWRITE,
            directive_artifact=directive_ref,
            instructions=("Change the entrance action.",),
        )
    return WriterInvocation(
        invocation_id=StableId(f"invocation.writer-generation.{mode.value}.{suffix}"),
        run_id=RunId(f"run.writer-generation.{mode.value}.{suffix}"),
        task_id=TaskId(f"task.writer-generation.{mode.value}.{suffix}"),
        mode=mode,
        basis=basis,
        writing_task=selected_task,
        context_package=selected_context,
        input_artifacts=tuple(dict.fromkeys(input_artifacts)),
        prior_draft=prior,
        continuation_boundary=boundary,
        rewrite_directive=directive,
        budget=WriterBudget(
            max_model_calls=max_model_calls,
            input_token_limit=2000,
            output_token_limit=1000,
        ),
    )


def _request(invocation: WriterInvocation) -> ModelRequest:
    return ModelRequest(
        request_id=StableId(f"request.{invocation.invocation_id.root}"),
        run_id=invocation.run_id,
        task_id=invocation.task_id,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id=f"trace.{invocation.invocation_id.root}",
        prompt="replaced by WriterAgent",
    )


def _replace_input_ref(
    invocation: WriterInvocation,
    old: ArtifactRef,
    new: ArtifactRef,
) -> tuple[ArtifactRef, ...]:
    return tuple(
        new if item.artifact_id == old.artifact_id else item for item in invocation.input_artifacts
    )


def _run(
    harness: Harness,
    invocation: WriterInvocation,
) -> WriterExecutionResult:
    return asyncio.run(harness.service.execute(invocation, _request(invocation)))


def test_writer_generation_materializes_three_modes_and_preserves_lineage(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    draft_invocation = _invocation(harness, suffix="draft")
    draft_result = _run(harness, draft_invocation)
    assert draft_result.status is WriterTerminalStatus.COMPLETED
    assert draft_result.draft is not None
    assert harness.artifacts.read_verified(draft_result.draft.text_artifact) == (
        _payload().draft_text.encode("utf-8")
    )

    prefix = _payload().draft_text[:24]
    continuation_text = prefix + " and then crosses the threshold without looking back."
    harness.endpoint.response_text = _payload(continuation_text).model_dump_json()
    continuation = _invocation(
        harness,
        mode=AgentMode.CONTINUE,
        suffix="continue",
        prior=draft_result.draft,
        frozen_prefix=prefix,
    )
    continued = _run(harness, continuation)
    assert continued.status is WriterTerminalStatus.COMPLETED
    assert continued.draft is not None
    assert continued.draft.parent_draft_id == draft_result.draft.draft_id
    assert continued.draft.mode is AgentMode.CONTINUE

    harness.endpoint.response_text = _payload(
        "Lin abandons force, angles a mirror, and lets moonlight open the gate."
    ).model_dump_json()
    rewrite = _invocation(
        harness,
        mode=AgentMode.MAJOR_REWRITE,
        suffix="rewrite",
        prior=continued.draft,
    )
    rewritten = _run(harness, rewrite)
    assert rewritten.status is WriterTerminalStatus.COMPLETED
    assert rewritten.draft is not None
    assert rewritten.draft.parent_draft_id == continued.draft.draft_id
    assert rewritten.draft.mode is AgentMode.MAJOR_REWRITE
    assert len(harness.endpoint.requests) == 3


def test_raw_text_sidecar_are_exact_content_addressed_and_quote_findings_are_advisory(
    tmp_path: Path,
) -> None:
    text = "Moonlight reaches the groove. Moonlight reaches the groove. The gate opens."
    hints = (
        DeclaredMemoryHint(
            subject_hint="gate",
            change_kind=MemoryHintChangeKind.CHANGE,
            predicate_hint="state",
            value_hint="open",
            evidence_quote="The gate opens.",
            confidence=0.9,
        ),
        DeclaredMemoryHint(
            subject_hint="moonlight",
            change_kind=MemoryHintChangeKind.UNCERTAIN,
            evidence_quote="Moonlight reaches the groove.",
            confidence=0.5,
        ),
        DeclaredMemoryHint(
            subject_hint="guard",
            change_kind=MemoryHintChangeKind.UNCERTAIN,
            evidence_quote="A guard appears.",
            confidence=0.1,
        ),
    )
    raw = json.dumps(
        _payload(text, hints=hints).model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    ).replace("\n", "\r\n")
    harness = _harness(tmp_path, response=raw)
    invocation = _invocation(harness)
    result = _run(harness, invocation)

    assert result.status is WriterTerminalStatus.COMPLETED
    assert result.draft is not None
    assert harness.artifacts.read_verified(result.draft.raw_output_artifact) == raw.encode("utf-8")
    assert result.draft.raw_output_artifact.media_type == RAW_OUTPUT_MEDIA_TYPE
    assert harness.artifacts.read_verified(result.draft.text_artifact) == text.encode("utf-8")
    assert result.draft.text_artifact.media_type == DRAFT_TEXT_MEDIA_TYPE
    sidecar_bytes = harness.artifacts.read_verified(result.draft.sidecar_artifact)
    assert result.draft.sidecar_artifact.media_type == SIDECAR_MEDIA_TYPE
    sidecar = json.loads(sidecar_bytes)
    assert sidecar_bytes == canonical_json_bytes(sidecar)
    assert [item["occurrence_count"] for item in sidecar["advisory_findings"]] == [2, 0]


def test_same_bytes_are_stable_and_one_character_changes_hash(tmp_path: Path) -> None:
    first = _harness(tmp_path / "first")
    second = _harness(tmp_path / "second")
    changed = _harness(
        tmp_path / "changed",
        response=_payload(
            "Lin studies the moonlit groove and opens the gate with a mirror!"
        ).model_dump_json(),
    )
    first_result = _run(first, _invocation(first))
    second_result = _run(second, _invocation(second))
    changed_result = _run(changed, _invocation(changed))

    assert first_result.draft is not None
    assert second_result.draft is not None
    assert changed_result.draft is not None
    assert first_result.draft.draft_id == second_result.draft.draft_id
    assert first_result.draft.text_artifact == second_result.draft.text_artifact
    assert first_result.draft.text_artifact != changed_result.draft.text_artifact


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("request", WriterTerminalStatus.CONTRACT_REJECTED),
        ("binding", WriterTerminalStatus.CONTRACT_REJECTED),
        ("configuration", WriterTerminalStatus.CONTRACT_REJECTED),
        ("taint", WriterTerminalStatus.CONTRACT_REJECTED),
        ("blocking", WriterTerminalStatus.NEEDS_CONTEXT),
        ("budget", WriterTerminalStatus.BUDGET_EXHAUSTED),
    ),
)
def test_preflight_terminals_never_call_model(
    tmp_path: Path,
    kind: str,
    expected: WriterTerminalStatus,
) -> None:
    harness = _harness(tmp_path)
    context = None
    task = None
    source_payload = None
    max_calls = 1
    if kind == "taint":
        source_payload = {"information_label": "gold_evaluator_only"}
    elif kind == "blocking":
        task = _task(blocking=True)
    elif kind == "budget":
        max_calls = 0
    invocation = _invocation(
        harness,
        context=context,
        task=task,
        source_payload=source_payload,
        max_model_calls=max_calls,
    )
    request = _request(invocation)
    if kind == "request":
        request = request.model_copy(update={"task_id": TaskId("task.writer-generation.other")})
    elif kind == "binding":
        invocation = invocation.model_copy(
            update={"input_artifacts": invocation.input_artifacts[:-1]}
        )
        request = _request(invocation)
    elif kind == "configuration":
        invocation = invocation.model_copy(
            update={
                "basis": invocation.basis.model_copy(
                    update={"model_configuration_fingerprint": ArtifactId("sha256:" + "9" * 64)}
                )
            }
        )
        request = _request(invocation)

    result = asyncio.run(harness.service.execute(invocation, request))

    assert result.status is expected
    assert result.draft is None and result.receipt is None
    assert result.metrics.model_call_count == 0
    assert harness.endpoint.requests == []


def test_context_item_and_artifact_tamper_fail_closed(tmp_path: Path) -> None:
    wrong = WriterContextItem(
        item_id=StableId("item.writer-generation.wrong"),
        category="relevant_historical_events",
        source_commit=BASE_COMMIT,
        snapshot_id=StableId("snapshot.writer-generation.wrong"),
        text="unsafe context item",
    )
    harness = _harness(tmp_path / "trace")
    trace_invocation = _invocation(
        harness,
        context=_context().model_copy(update={"items": (wrong,)}),
    )
    trace_result = _run(harness, trace_invocation)
    assert trace_result.status is WriterTerminalStatus.CONTRACT_REJECTED
    assert harness.endpoint.requests == []


def test_memory_gate_artifact_must_be_canonical_report_json(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    invocation = _invocation(harness)
    gate_report = Stage2GateEvaluator().evaluate(evidence())
    wrong_gate_ref = _put_json(harness.artifacts, {"not": "the gate report"})
    invocation = invocation.model_copy(
        update={
            "basis": invocation.basis.model_copy(
                update={
                    "memory_gate_report": gate_report,
                    "memory_gate_artifact": wrong_gate_ref,
                }
            ),
            "input_artifacts": (*invocation.input_artifacts, wrong_gate_ref),
        }
    )
    result = _run(harness, invocation)
    assert result.status is WriterTerminalStatus.CONTRACT_REJECTED
    assert harness.endpoint.requests == []

    tamper = _harness(tmp_path / "tamper")
    tamper_invocation = _invocation(tamper)
    content_path = tamper.root / object_key(tamper_invocation.basis.context_artifact.artifact_id)
    content_path.write_bytes(b"tampered context bytes")
    tamper_result = _run(tamper, tamper_invocation)
    assert tamper_result.status is WriterTerminalStatus.CONTRACT_REJECTED
    assert tamper.endpoint.requests == []


@pytest.mark.parametrize(
    ("response", "error", "expected"),
    (
        ("not json", None, WriterTerminalStatus.MODEL_OUTPUT_REJECTED),
        (
            json.dumps(_payload().model_dump(mode="json") | {"base_commit": "sha256:" + "9" * 64}),
            None,
            WriterTerminalStatus.MODEL_OUTPUT_REJECTED,
        ),
        (None, TimeoutError("late"), WriterTerminalStatus.MODEL_UNAVAILABLE),
        (None, RuntimeError("provider down"), WriterTerminalStatus.MODEL_UNAVAILABLE),
    ),
)
def test_model_failures_are_typed_without_success_receipt(
    tmp_path: Path,
    response: str | None,
    error: Exception | None,
    expected: WriterTerminalStatus,
) -> None:
    harness = _harness(tmp_path, response=response, error=error)
    invocation = _invocation(harness)
    result = _run(harness, invocation)

    assert result.status is expected
    assert result.draft is None and result.receipt is None
    assert result.metrics.model_call_count == 1
    assert len(harness.endpoint.requests) == 1
    if response is not None:
        assert len(result.artifacts) == 1
        assert result.artifacts[0].media_type == RAW_OUTPUT_MEDIA_TYPE
        assert harness.artifacts.read_verified(result.artifacts[0]) == response.encode("utf-8")


def test_rejected_raw_write_failure_overrides_model_rejection_terminal(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, response="not json")
    invocation = _invocation(harness)
    harness.store.arm(1)

    result = _run(harness, invocation)

    assert result.status is WriterTerminalStatus.ARTIFACT_WRITE_FAILED
    assert result.draft is None and result.receipt is None
    assert result.artifacts == ()
    assert result.metrics.model_call_count == 1


def test_cancel_is_typed_and_has_no_success_receipt(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path,
        endpoint=CancelledEndpoint(_payload().model_dump_json()),
    )
    invocation = _invocation(harness)
    result = _run(harness, invocation)

    assert result.status is WriterTerminalStatus.CANCELLED
    assert result.draft is None and result.receipt is None
    assert result.metrics.model_call_count == 1


def test_continue_requires_exact_frozen_prefix_and_new_text(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    initial = _run(harness, _invocation(harness, suffix="initial"))
    assert initial.draft is not None
    prefix = _payload().draft_text[:20]
    harness.endpoint.response_text = _payload(
        "A changed prefix followed by new text."
    ).model_dump_json()
    invocation = _invocation(
        harness,
        mode=AgentMode.CONTINUE,
        suffix="bad-prefix",
        prior=initial.draft,
        frozen_prefix=prefix,
    )
    result = _run(harness, invocation)

    assert result.status is WriterTerminalStatus.MODEL_OUTPUT_REJECTED
    assert result.draft is None and result.receipt is None
    assert len(result.artifacts) == 1
    assert result.artifacts[0].media_type == RAW_OUTPUT_MEDIA_TYPE


@pytest.mark.parametrize("failure_call", (1, 2, 3))
def test_each_artifact_write_failure_resumes_without_second_model_call(
    tmp_path: Path,
    failure_call: int,
) -> None:
    harness = _harness(tmp_path)
    invocation = _invocation(harness)
    request = _request(invocation)
    harness.store.arm(failure_call)

    failed = asyncio.run(harness.service.execute(invocation, request))

    assert failed.status is WriterTerminalStatus.ARTIFACT_WRITE_FAILED
    assert failed.draft is None and failed.receipt is None
    assert len(failed.artifacts) == failure_call - 1
    assert len(harness.endpoint.requests) == 1

    harness.store.arm(None)
    completed = asyncio.run(harness.service.execute(invocation, request))
    assert completed.status is WriterTerminalStatus.COMPLETED
    assert completed.draft is not None and completed.receipt is not None
    assert len(harness.endpoint.requests) == 1


def test_completed_replay_and_identity_collision_are_exact_and_side_effect_free(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    invocation = _invocation(harness)
    request = _request(invocation)
    first = asyncio.run(harness.service.execute(invocation, request))
    writes_after_first = harness.store.write_calls

    replay = asyncio.run(harness.service.execute(invocation, request))
    collision = asyncio.run(
        harness.service.execute(
            invocation,
            request.model_copy(update={"prompt": "different immutable request"}),
        )
    )

    assert replay == first
    assert harness.store.write_calls == writes_after_first
    assert collision.status is WriterTerminalStatus.CONTRACT_REJECTED
    assert collision.metrics.model_call_count == 0
    assert collision.draft is None and collision.receipt is None
    assert len(harness.endpoint.requests) == 1


def test_preflight_failure_uses_resolved_contract_fingerprints_not_config_aliases(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    invocation = _invocation(harness)
    wrong_request = _request(invocation).model_copy(
        update={"task_id": TaskId("task.writer-generation.wrong")}
    )

    result = asyncio.run(harness.service.execute(invocation, wrong_request))

    spec = harness.specs[AgentMode.DRAFT]
    assert result.status is WriterTerminalStatus.CONTRACT_REJECTED
    assert result.fingerprints.agent_spec_fingerprint == spec.content_hash
    assert result.fingerprints.tool_policy_fingerprint == spec.tool_policy.content_hash
    assert result.fingerprints.skill_fingerprints == tuple(
        item.content_hash for item in spec.skills
    )
    assert result.fingerprints.prompt_fingerprint != invocation.basis.configuration_fingerprint


def test_unresolvable_contract_uses_explicit_unavailable_fingerprints(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    invocation = _invocation(harness)
    invocation = invocation.model_copy(
        update={
            "basis": invocation.basis.model_copy(
                update={"configuration_fingerprint": ArtifactId("sha256:" + "8" * 64)}
            )
        }
    )

    result = _run(harness, invocation)

    assert result.status is WriterTerminalStatus.CONTRACT_REJECTED
    assert "RUNTIME_CONTRACT_FINGERPRINTS_UNAVAILABLE" in (result.failure_detail or "")
    assert result.fingerprints.agent_spec_fingerprint != invocation.basis.configuration_fingerprint
    assert harness.endpoint.requests == []


@pytest.mark.parametrize(
    "updates",
    (
        {"source_commit": CommitId("sha256:" + "7" * 64)},
        {"access_scope": "evaluator_only"},
        {"information_label": "future_gold"},
    ),
)
def test_every_context_unit_boundary_field_is_enforced(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    harness = _harness(tmp_path)
    item = _context().items[0].model_copy(update=updates)
    invocation = _invocation(harness, context=_context(item=item))

    result = _run(harness, invocation)

    assert result.status is WriterTerminalStatus.CONTRACT_REJECTED
    assert harness.endpoint.requests == []


def test_basis_duplicate_and_conflicting_input_bindings_fail_closed(tmp_path: Path) -> None:
    basis_harness = _harness(tmp_path / "basis")
    basis_invocation = _invocation(basis_harness)
    mismatched = basis_invocation.model_copy(
        update={
            "basis": basis_invocation.basis.model_copy(
                update={"context_id": StableId("context.writer-generation.other")}
            )
        }
    )
    assert _run(basis_harness, mismatched).status is WriterTerminalStatus.CONTRACT_REJECTED

    duplicate_harness = _harness(tmp_path / "duplicate")
    duplicate = _invocation(duplicate_harness)
    duplicate = duplicate.model_copy(
        update={"input_artifacts": (*duplicate.input_artifacts, duplicate.input_artifacts[0])}
    )
    assert _run(duplicate_harness, duplicate).status is WriterTerminalStatus.CONTRACT_REJECTED

    conflict_harness = _harness(tmp_path / "conflict")
    conflict = _invocation(conflict_harness)
    conflicting_ref = conflict.basis.context_artifact.model_copy(
        update={"media_type": "application/vnd.conflicting+json"}
    )
    conflict = conflict.model_copy(
        update={"basis": conflict.basis.model_copy(update={"plan_artifact": conflicting_ref})}
    )
    assert _run(conflict_harness, conflict).status is WriterTerminalStatus.CONTRACT_REJECTED

    assert basis_harness.endpoint.requests == []
    assert duplicate_harness.endpoint.requests == []
    assert conflict_harness.endpoint.requests == []


def test_context_and_writing_artifacts_require_exact_canonical_json(tmp_path: Path) -> None:
    context_harness = _harness(tmp_path / "context")
    context_invocation = _invocation(context_harness)
    pretty_context_ref = context_harness.artifacts.put(
        json.dumps(
            context_invocation.context_package.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8"),
        "application/json",
        VERSION,
    )
    context_invocation = context_invocation.model_copy(
        update={
            "basis": context_invocation.basis.model_copy(
                update={
                    "context_artifact": pretty_context_ref,
                    "context_fingerprint": pretty_context_ref.artifact_id,
                }
            ),
            "input_artifacts": _replace_input_ref(
                context_invocation,
                context_invocation.basis.context_artifact,
                pretty_context_ref,
            ),
        }
    )
    assert (
        _run(context_harness, context_invocation).status is WriterTerminalStatus.CONTRACT_REJECTED
    )

    writing_harness = _harness(tmp_path / "writing")
    writing_invocation = _invocation(writing_harness)
    pretty_writing_ref = writing_harness.artifacts.put(
        json.dumps(
            writing_invocation.writing_task.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8"),
        "application/json",
        VERSION,
    )
    writing_invocation = writing_invocation.model_copy(
        update={
            "basis": writing_invocation.basis.model_copy(
                update={"writing_contract_artifact": pretty_writing_ref}
            ),
            "input_artifacts": _replace_input_ref(
                writing_invocation,
                writing_invocation.basis.writing_contract_artifact,
                pretty_writing_ref,
            ),
        }
    )
    assert (
        _run(writing_harness, writing_invocation).status is WriterTerminalStatus.CONTRACT_REJECTED
    )


def test_source_binding_attestation_is_rechecked(
    tmp_path: Path,
) -> None:
    isolation_harness = _harness(tmp_path / "isolation")
    isolation_invocation = _invocation(
        isolation_harness,
        source_payload={"text": "visible"},
    )
    attestation = isolation_invocation.basis.future_isolation_attestation.model_copy(
        update={"canonical_source_ids": ()}
    )
    basis_data = isolation_invocation.basis.model_dump()
    basis_data["future_isolation_attestation"] = attestation
    unattested_basis = WriterArtifactBasis.model_validate(basis_data)
    isolation_invocation = isolation_invocation.model_copy(update={"basis": unattested_basis})
    assert (
        _run(isolation_harness, isolation_invocation).status
        is WriterTerminalStatus.CONTRACT_REJECTED
    )


def test_source_payload_metadata_and_plain_text_projection_paths(tmp_path: Path) -> None:
    unsafe = _harness(tmp_path / "unsafe")
    unsafe_invocation = _invocation(
        unsafe,
        source_payload={
            "nested": [
                {
                    "access_scope": "evaluator_only",
                    "derivation_taint": ["observed"],
                }
            ]
        },
    )
    assert _run(unsafe, unsafe_invocation).status is WriterTerminalStatus.CONTRACT_REJECTED

    plain = _harness(tmp_path / "plain")
    plain_invocation = _invocation(
        plain,
        source_payload={
            "nested": [
                {
                    "access_scope": "writer_safe",
                    "derivation_taint": ["observed"],
                }
            ]
        },
    )
    plain_plan = plain.artifacts.put(
        b"plain visible plan payload",
        "text/plain; charset=utf-8",
        VERSION,
    )
    plain_invocation = plain_invocation.model_copy(
        update={
            "basis": plain_invocation.basis.model_copy(update={"plan_artifact": plain_plan}),
            "input_artifacts": _replace_input_ref(
                plain_invocation,
                plain_invocation.basis.plan_artifact,
                plain_plan,
            ),
        }
    )
    assert _run(plain, plain_invocation).status is WriterTerminalStatus.COMPLETED


def test_continuation_boundary_count_and_prior_prefix_are_rechecked(tmp_path: Path) -> None:
    count_harness = _harness(tmp_path / "count")
    initial = _run(count_harness, _invocation(count_harness, suffix="initial"))
    assert initial.draft is not None
    prefix = _payload().draft_text[:20]
    count_harness.endpoint.response_text = _payload(
        prefix + " with genuinely new continuation text."
    ).model_dump_json()
    count_invocation = _invocation(
        count_harness,
        mode=AgentMode.CONTINUE,
        suffix="count",
        prior=initial.draft,
        frozen_prefix=prefix,
    )
    assert count_invocation.continuation_boundary is not None
    count_invocation = count_invocation.model_copy(
        update={
            "continuation_boundary": count_invocation.continuation_boundary.model_copy(
                update={"frozen_prefix_characters": len(prefix) + 1}
            )
        }
    )
    assert _run(count_harness, count_invocation).status is WriterTerminalStatus.CONTRACT_REJECTED

    prefix_harness = _harness(tmp_path / "prefix")
    prefix_initial = _run(
        prefix_harness,
        _invocation(prefix_harness, suffix="initial"),
    )
    assert prefix_initial.draft is not None
    invalid_prefix = "This prefix is not in the prior draft."
    prefix_invocation = _invocation(
        prefix_harness,
        mode=AgentMode.CONTINUE,
        suffix="wrong",
        prior=prefix_initial.draft,
        frozen_prefix=invalid_prefix,
    )
    assert _run(prefix_harness, prefix_invocation).status is WriterTerminalStatus.CONTRACT_REJECTED


@pytest.mark.parametrize(
    "text",
    (
        "too short",
        "X" * 501,
    ),
)
def test_trusted_length_policy_rejects_short_and_long_drafts(
    tmp_path: Path,
    text: str,
) -> None:
    harness = _harness(tmp_path, response=_payload(text).model_dump_json())
    result = _run(harness, _invocation(harness))
    assert result.status is WriterTerminalStatus.MODEL_OUTPUT_REJECTED
    assert result.draft is None and result.receipt is None
    assert len(result.artifacts) == 1


def test_continue_requires_content_after_frozen_prefix(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    initial = _run(harness, _invocation(harness, suffix="initial"))
    assert initial.draft is not None
    prefix = _payload().draft_text[:20]
    harness.endpoint.response_text = _payload(prefix).model_dump_json()
    invocation = _invocation(
        harness,
        mode=AgentMode.CONTINUE,
        suffix="no-new-text",
        prior=initial.draft,
        frozen_prefix=prefix,
    )
    result = _run(harness, invocation)
    assert result.status is WriterTerminalStatus.MODEL_OUTPUT_REJECTED


def test_actual_retry_and_token_usage_cannot_complete_over_budget(tmp_path: Path) -> None:
    retry_endpoint = SequenceEndpoint(
        (
            '{"draft_text":1}',
            _payload().model_dump_json(),
        )
    )
    retry = _harness(
        tmp_path / "retry",
        endpoint=retry_endpoint,
        structured_max_retries=1,
    )
    retry_result = _run(retry, _invocation(retry))
    assert retry_result.status is WriterTerminalStatus.BUDGET_EXHAUSTED
    assert retry_result.metrics.model_call_count == 2
    assert retry_result.draft is None and retry_result.receipt is None

    input_usage = _harness(
        tmp_path / "input",
        endpoint=UsageEndpoint(
            _payload().model_dump_json(),
            input_tokens=2001,
            output_tokens=1,
        ),
    )
    input_result = _run(input_usage, _invocation(input_usage))
    assert input_result.status is WriterTerminalStatus.BUDGET_EXHAUSTED
    assert input_result.metrics.cost_usd == Decimal("0.25")

    output_usage = _harness(
        tmp_path / "output",
        endpoint=UsageEndpoint(
            _payload().model_dump_json(),
            input_tokens=1,
            output_tokens=1001,
        ),
    )
    output_result = _run(output_usage, _invocation(output_usage))
    assert output_result.status is WriterTerminalStatus.BUDGET_EXHAUSTED


def test_missing_raw_and_unclassified_execution_failures_are_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = _harness(tmp_path / "missing")
    missing.gateway.raw_responses = DiscardingRawResponses()
    missing_result = _run(missing, _invocation(missing))
    assert missing_result.status is WriterTerminalStatus.FATAL

    fatal = _harness(tmp_path / "fatal")

    async def explode(_prepared: object) -> NoReturn:
        raise ArithmeticError("unexpected execution bug")

    monkeypatch.setattr(fatal.writer, "execute", explode)
    fatal_result = _run(fatal, _invocation(fatal))
    assert fatal_result.status is WriterTerminalStatus.FATAL


def test_typed_execution_contract_and_ledger_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _harness(tmp_path / "ledger")

    async def collide(_prepared: object) -> NoReturn:
        raise ModelCallLedgerCollision("collision")

    monkeypatch.setattr(ledger.writer, "execute", collide)
    ledger_result = _run(ledger, _invocation(ledger))
    assert ledger_result.status is WriterTerminalStatus.CONTRACT_REJECTED

    contract = _harness(tmp_path / "contract")

    async def reject(_prepared: object) -> NoReturn:
        from novel_agent.agents.writer import WriterAgentError

        raise WriterAgentError("prepared contract changed")

    monkeypatch.setattr(contract.writer, "execute", reject)
    contract_result = _run(contract, _invocation(contract))
    assert contract_result.status is WriterTerminalStatus.CONTRACT_REJECTED


def test_unexpected_preflight_and_receipt_errors_are_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = _harness(tmp_path / "preflight")

    def explode_preflight(*_args: object) -> NoReturn:
        raise RuntimeError("unexpected preflight failure")

    monkeypatch.setattr(preflight.service, "_preflight", explode_preflight)
    preflight_result = _run(preflight, _invocation(preflight))
    assert preflight_result.status is WriterTerminalStatus.FATAL

    receipt = _harness(tmp_path / "receipt")
    original_receipt = receipt.writer.receipt
    receipt_calls = 0

    def explode_receipt(
        prepared: PreparedAgentRun,
        call: ModelCallRecord,
        *,
        output_artifacts: tuple[ArtifactRef, ...] = (),
        unresolved: tuple[str, ...] = (),
    ) -> AgentExecutionReceipt:
        nonlocal receipt_calls
        receipt_calls += 1
        if receipt_calls == 2:
            raise RuntimeError("unexpected receipt failure")
        return original_receipt(
            prepared,
            call,
            output_artifacts=output_artifacts,
            unresolved=unresolved,
        )

    monkeypatch.setattr(receipt.writer, "receipt", explode_receipt)
    receipt_result = _run(receipt, _invocation(receipt))
    assert receipt_result.status is WriterTerminalStatus.FATAL
    assert len(receipt_result.artifacts) == 3


def test_rejected_raw_missing_and_cached_paths_are_deterministic(tmp_path: Path) -> None:
    missing = _harness(tmp_path / "missing", response="not json")
    missing.gateway.raw_responses = DiscardingRawResponses()
    missing_invocation = _invocation(missing)
    missing_result = _run(missing, missing_invocation)
    assert missing_result.status is WriterTerminalStatus.MODEL_OUTPUT_REJECTED
    assert missing_result.artifacts == ()

    cached = _harness(tmp_path / "cached", response="not json")
    cached_invocation = _invocation(cached)
    cached_result = _run(cached, cached_invocation)
    assert len(cached_result.artifacts) == 1
    state = cached.service._replays[cached_invocation.invocation_id]
    writes = cached.store.write_calls
    assert cached.service._persist_rejected_raw(_request(cached_invocation), state) == (
        cached_result.artifacts
    )
    assert cached.store.write_calls == writes


def test_writer_configuration_mismatch_and_incomplete_replay_state_fail_closed(
    tmp_path: Path,
) -> None:
    mismatch = _harness(tmp_path / "mismatch")
    invocation = _invocation(mismatch)
    request = _request(invocation)
    prepared = mismatch.writer.prepare_contract(invocation, request)
    mismatched_invocation = invocation.model_copy(
        update={
            "basis": invocation.basis.model_copy(
                update={"configuration_fingerprint": ArtifactId("sha256:" + "7" * 64)}
            )
        }
    )
    replay = writer_generation._ReplayState(
        identity_fingerprint=ArtifactId("sha256:" + "6" * 64),
        result_id=StableId("writer-result.configuration-mismatch"),
        prepared=prepared,
    )
    with pytest.raises(WriterGenerationContractError, match="AgentSpec configuration"):
        mismatch.service._preflight(mismatched_invocation, request, replay)

    incomplete = _harness(tmp_path / "incomplete")
    valid_invocation = _invocation(incomplete)
    request = _request(valid_invocation)
    state = writer_generation._ReplayState(
        identity_fingerprint=ArtifactId("sha256:" + "8" * 64),
        result_id=StableId("writer-result.incomplete"),
    )
    with pytest.raises(AssertionError, match="replay state"):
        incomplete.service._materialize(valid_invocation, request, state, ())


def test_sidecar_cached_resume_does_not_repeat_model_or_artifact_writes(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    invocation = _invocation(harness)
    completed = _run(harness, invocation)
    assert completed.status is WriterTerminalStatus.COMPLETED
    writes = harness.store.write_calls

    state = harness.service._replays[invocation.invocation_id]
    state.terminal = None
    replayed = asyncio.run(harness.service.execute(invocation, _request(invocation)))

    assert replayed.status is WriterTerminalStatus.COMPLETED
    assert harness.store.write_calls == writes
    assert len(harness.endpoint.requests) == 1


def test_context_item_iterator_covers_empty_and_multi_item_snapshots() -> None:
    empty = _context().model_copy(update={"items": ()})
    assert tuple(writer_generation._context_items(empty)) == ()

    first = _context().items[0]
    second = first.model_copy(
        update={
            "item_id": StableId("item.writer-generation.second"),
            "category": "current_world_state",
        }
    )
    context = _context().model_copy(update={"items": (first, second)})

    assert tuple(writer_generation._context_items(context)) == (first, second)


def test_writer_service_has_no_canon_database_or_search_dependency() -> None:
    path = Path(__file__).parents[2] / "src" / "novel_agent" / "services" / "writer_generation.py"
    source = path.read_text(encoding="utf-8").lower()
    assert "sqlalchemy" not in source
    assert "opensearch" not in source
    assert "commitservice" not in source
    assert "memory_write_workflow" not in source
