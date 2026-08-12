"""Public contract of the Stage 3 generation quality evaluation tool (D)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.agents import (
    AgentRegistry,
    StructuredAgentRunner,
    WriterAgent,
    build_writer_contract_bundle,
)
from novel_agent.domain.generation import WriterBudget, WriterExecutionResult, WriterSourceBinding
from novel_agent.domain.ids import ProjectId, RunId, StableId
from novel_agent.domain.model_calls import (
    ModelRequest,
    ModelRole,
    ModelUsage,
    ProviderModelResult,
)
from novel_agent.domain.stage2 import AgentMode, AgentSpec, FutureIsolationAttestation
from novel_agent.domain.stage3_evaluation import (
    CollectedEditorialReport,
    CollectedReconciliationResult,
    ContextScheme,
    Stage3CaseResult,
    Stage3EvaluationCase,
    Stage3EvaluationReport,
    Stage3FailureCategory,
    Stage3RunConfig,
    Stage3SchemeResult,
)
from novel_agent.prompts import PromptRegistry
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import content_id
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.stage3_evaluation import (
    PreparedWriterRun,
    ScriptedEvaluator,
    Stage3WriterRunBuilder,
    assemble_scheme_result,
    evaluate_rules,
    load_case,
)
from novel_agent.services.writer_generation import WriterGenerationService
from novel_agent.skills import SkillRegistry

REPOSITORY_ROOT = Path(__file__).parents[2]
CASE_DIRECTORY = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "stage3_evaluation" / "cases" / "enter_tower"
)
EVALUATOR_SECRET = "EVALUATOR_SECRET_仅限评分员"
_SOURCE_MEDIA_TYPE = "application/vnd.novel-agent.writer-source+json"


def _make_case() -> Stage3EvaluationCase:
    case = load_case(CASE_DIRECTORY / "case.json")
    return case.model_copy(
        update={"evaluator_instructions": (EVALUATOR_SECRET, *case.evaluator_instructions)}
    )


def _prepare_runs(
    case: Stage3EvaluationCase,
    tmp_path: Path,
    *,
    spec: AgentSpec,
) -> tuple[Stage3WriterRunBuilder, tuple[PreparedWriterRun, ...], ArtifactRepository]:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path))
    source_id = StableId("source.stage3.evaluation.contract")
    attestation = FutureIsolationAttestation(
        attestation_id=StableId("attestation.contract"),
        checkpoint_chapter=case.writing_task.target_chapter - 1,
        canonical_source_ids=(source_id,),
        evaluator_only_source_ids=(),
        passed=True,
        configuration_fingerprint=content_id({"profile": "contract"}),
    )
    source_payload = b'{"source_id": "source.stage3.evaluation.contract"}'
    builder = Stage3WriterRunBuilder(
        artifacts=artifacts,
        schema_version=spec.version,
        project_id=ProjectId("project.contract"),
        run_id=RunId("run.contract"),
        writer_configuration_fingerprint=content_id(spec.model_dump(mode="json")),
        model_configuration_fingerprint=content_id({"model": "fake", "contract": True}),
        attestation=attestation,
        source_binding=WriterSourceBinding(
            source_id=source_id,
            source_artifact=artifacts.put(
                source_payload,
                _SOURCE_MEDIA_TYPE,
                spec.version,
            ),
        ),
        plan_payload=b'{"plan": true}',
        profile_payload=b'{"profile": true}',
        source_payload=source_payload,
        budget=WriterBudget(
            max_model_calls=1,
            input_token_limit=8_000,
            output_token_limit=2_000,
        ),
    )
    return builder, builder.prepare(case), artifacts


def _run_scheme(
    case: Stage3EvaluationCase,
    prepared: PreparedWriterRun,
    artifacts: ArtifactRepository,
    *,
    spec: AgentSpec,
    prompts: PromptRegistry,
    skills: SkillRegistry,
    registry: AgentRegistry,
    response_text: str,
) -> tuple[WriterExecutionResult, _RecordingEndpoint]:
    endpoint = _RecordingEndpoint(response_text)
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="contract-fake",
                model_name="fake-writer",
                adapter=endpoint,
            ),
        ),
        forbid_external_calls=True,
        structured_max_retries=0,
    )
    service = WriterGenerationService(
        WriterAgent(StructuredAgentRunner(gateway, registry, prompts, skills), prompts, skills),
        gateway,
        artifacts,
        spec.version,
        content_id({"model": "fake", "contract": True}),
    )
    writer_result = asyncio.run(service.execute(prepared.invocation, prepared.request))
    return writer_result, endpoint


class _RecordingEndpoint:
    is_external = False

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.requests.append(request)
        return ProviderModelResult(
            text=self.response_text,
            model_version="fake-v1",
            usage=ModelUsage(input_tokens=0, output_tokens=0, cost_usd=Decimal("0")),
        )


def test_three_context_schemes_enter_one_uniform_writer_entry(tmp_path: Path) -> None:
    """All three schemes are comparable Writer calls through the same entry."""
    bundle = build_writer_contract_bundle(
        REPOSITORY_ROOT / "src" / "novel_agent",
        modes=(AgentMode.DRAFT,),
    )
    spec = bundle.agent_specs[0]
    prompts = PromptRegistry(bundle.prompt_templates)
    skills = SkillRegistry(bundle.skill_templates)
    registry = AgentRegistry(bundle.agent_specs)
    case = _make_case()
    runs, artifacts = _prepare_runs(case, tmp_path, spec=spec)[1:]

    assert [run.scheme for run in runs] == list(ContextScheme)
    budgets = {run.invocation.budget for run in runs}
    assert len(budgets) == 1
    fingerprints = {run.invocation.basis.configuration_fingerprint for run in runs}
    assert len(fingerprints) == 1
    model_fingerprints = {run.invocation.basis.model_configuration_fingerprint for run in runs}
    assert len(model_fingerprints) == 1
    context_ids = {run.invocation.context_package.context_id.root for run in runs}
    assert len(context_ids) == 3
    run_ids = {run.invocation.run_id for run in runs}
    assert len(run_ids) == 1

    draft_outputs = json.loads((CASE_DIRECTORY / "draft_outputs.json").read_text(encoding="utf-8"))
    results: list[Stage3SchemeResult] = []
    for prepared in runs:
        payload = draft_outputs[prepared.scheme.value]
        writer_result, endpoint = _run_scheme(
            case,
            prepared,
            artifacts,
            spec=spec,
            prompts=prompts,
            skills=skills,
            registry=registry,
            response_text=json.dumps(payload, ensure_ascii=False),
        )
        assert writer_result.status.value == "completed"
        assert writer_result.draft is not None
        assert EVALUATOR_SECRET not in endpoint.requests[0].prompt
        text = artifacts.read_verified(writer_result.draft.text_artifact).decode("utf-8")
        rules = evaluate_rules(case, prepared.scheme, text)
        scheme_result = assemble_scheme_result(
            case=case,
            scheme=prepared.scheme,
            writer=writer_result,
            editorial=cast(
                CollectedEditorialReport | None,
                _collect_scheme_report(
                    CASE_DIRECTORY / "editorial_reports.json",
                    prepared.scheme,
                    CollectedEditorialReport,
                    writer_result.draft.draft_id.root,
                ),
            ),
            reconciliation=cast(
                CollectedReconciliationResult | None,
                _collect_scheme_report(
                    CASE_DIRECTORY / "reconciliation_results.json",
                    prepared.scheme,
                    CollectedReconciliationResult,
                    writer_result.draft.draft_id.root,
                ),
            ),
            rules=rules,
            evaluator_scores=ScriptedEvaluator(CASE_DIRECTORY / "evaluator_scores.json").evaluate(
                case, prepared.scheme
            ),
            evaluation_required=True,
        )
        assert scheme_result.status is Stage3FailureCategory.COMPLETED
        results.append(scheme_result)

    case_result = Stage3CaseResult(case_id=case.case_id, schemes=tuple(results))
    assert len(case_result.schemes) == 3


def _collect_scheme_report(
    path: Path,
    scheme: ContextScheme,
    model: type[CollectedEditorialReport] | type[CollectedReconciliationResult],
    draft_id: str,
) -> CollectedEditorialReport | CollectedReconciliationResult:
    mapping = json.loads(path.read_text(encoding="utf-8"))
    payload = {**mapping[scheme.value], "draft_id": draft_id}
    return model.model_validate_json(json.dumps(payload, ensure_ascii=False))


def test_evaluator_only_instructions_never_reach_the_writer(tmp_path: Path) -> None:
    bundle = build_writer_contract_bundle(
        REPOSITORY_ROOT / "src" / "novel_agent",
        modes=(AgentMode.DRAFT,),
    )
    spec = bundle.agent_specs[0]
    case = _make_case()
    runs = _prepare_runs(case, tmp_path, spec=spec)[1]
    assert any(EVALUATOR_SECRET in instruction for instruction in case.evaluator_instructions)

    for prepared in runs:
        assert EVALUATOR_SECRET not in prepared.invocation.model_dump_json()
        assert EVALUATOR_SECRET not in prepared.request.model_dump_json()


def test_writer_failure_is_typed_not_disguised_as_success(tmp_path: Path) -> None:
    bundle = build_writer_contract_bundle(
        REPOSITORY_ROOT / "src" / "novel_agent",
        modes=(AgentMode.DRAFT,),
    )
    spec = bundle.agent_specs[0]
    prompts = PromptRegistry(bundle.prompt_templates)
    skills = SkillRegistry(bundle.skill_templates)
    registry = AgentRegistry(bundle.agent_specs)
    case = _make_case()
    runs = _prepare_runs(case, tmp_path, spec=spec)[1]
    prepared = runs[0]
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="contract-fake",
                model_name="fake-writer",
                adapter=FakeModelEndpoint("{not-json"),
            ),
        ),
        forbid_external_calls=True,
        structured_max_retries=0,
    )
    artifacts = _prepare_runs(case, tmp_path, spec=spec)[2]
    service = WriterGenerationService(
        WriterAgent(StructuredAgentRunner(gateway, registry, prompts, skills), prompts, skills),
        gateway,
        artifacts,
        spec.version,
        content_id({"model": "fake", "contract": True}),
    )
    writer_result = asyncio.run(service.execute(prepared.invocation, prepared.request))
    assert writer_result.status.value == "model_output_rejected"

    scheme_result = assemble_scheme_result(
        case=case,
        scheme=prepared.scheme,
        writer=writer_result,
        editorial=None,
        reconciliation=None,
    )
    assert scheme_result.status is Stage3FailureCategory.WRITER_FAILED
    assert scheme_result.draft is None
    assert "model_output_rejected" in (scheme_result.failure_detail or "")


def test_report_traces_every_result_to_case_and_scheme(tmp_path: Path) -> None:
    """The machine-readable report identifies case and input scheme for each result."""
    case = _make_case()
    scheme_result = assemble_scheme_result(
        case=case,
        scheme=ContextScheme.RECENT_PROSE,
        writer=None,
        editorial=None,
        reconciliation=None,
    )
    assert scheme_result.status is Stage3FailureCategory.INPUT_NOT_READY
    config = Stage3RunConfig(
        git_commit="contract",
        git_dirty=False,
        writer_model="fake",
        command="contract test",
        created_at=datetime(2026, 7, 31, tzinfo=UTC),
        case_directory=str(CASE_DIRECTORY),
        output_directory="out",
    )
    report = Stage3EvaluationReport(
        report_id=StableId("report.contract"),
        run_config=config,
        cases=(
            Stage3CaseResult(
                case_id=case.case_id,
                schemes=(scheme_result,),
            ),
        ),
    )
    dumped = json.loads(report.model_dump_json())
    assert dumped["cases"][0]["schemes"][0]["case_id"] == case.case_id.root
    assert dumped["cases"][0]["schemes"][0]["scheme"] == "recent_prose"
    assert dumped["cases"][0]["schemes"][0]["status"] == "input_not_ready"
    assert dumped["run_config"]["command"] == "contract test"
