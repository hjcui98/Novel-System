from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import ScriptedModelEndpoint
from novel_agent.agents import (
    AgentRegistry,
    StructuredAgentRunner,
    build_editor_contract_bundle,
    build_writer_contract_bundle,
)
from novel_agent.domain.editorial import (
    CuratorObservation,
    EditorialReport,
    EditorialVerdict,
    RepairedDraft,
)
from novel_agent.domain.generation import (
    WriterBudget,
    WriterContextHandoffRequest,
    WritingTaskContract,
)
from novel_agent.domain.ids import ArtifactId, ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.stage2 import AgentMode, FutureIsolationAttestation
from novel_agent.domain.writer_context import ContextAssemblyStatus, ContextGap
from novel_agent.prompts import PromptRegistry
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import content_id
from novel_agent.services.editorial import EditorialService
from novel_agent.services.writer_draft_integration import (
    WriterContextHandoffError,
    WriterDraftIntegrationService,
    WriterIntegrationResult,
    WriterIntegrationStatus,
)
from novel_agent.services.writer_generation import WriterGenerationService
from novel_agent.skills import SkillRegistry
from tests.fixtures.stage2_memory_benchmark import writer_context_inputs

ROOT = Path(__file__).parents[2]
VERSION = SchemaVersion("1.0.0")
MODEL_FINGERPRINT = content_id({"model": "stage3-integration-fake"})


def _writing_task() -> WritingTaskContract:
    return WritingTaskContract.model_validate_json(
        (ROOT / "tests" / "fixtures" / "stage3_writer" / "writing_task_contract.json").read_text(
            encoding="utf-8"
        )
    )


def _responses(
    *,
    editor_payload: dict[str, object] | None = None,
    repair_payload: dict[str, object] | None = None,
    writer_payload: dict[str, object] | None = None,
    writer_failure: bool = False,
) -> Callable[[ModelRequest], str]:
    writer = writer_payload or json.loads(
        (ROOT / "tests" / "fixtures" / "stage3_writer" / "draft_output.json").read_text(
            encoding="utf-8"
        )
    )
    editor = editor_payload or {"verdict": "PASS", "issues": []}
    repair = repair_payload or {"repaired_text": writer["draft_text"]}

    def respond(request: ModelRequest) -> str:
        if request.agent_mode in {
            AgentMode.DRAFT.value,
            AgentMode.CONTINUE.value,
            AgentMode.MAJOR_REWRITE.value,
        }:
            if writer_failure:
                return "{invalid-json"
            return json.dumps(writer, ensure_ascii=False)
        if request.agent_mode == AgentMode.LOCAL_REPAIR.value:
            return json.dumps(repair, ensure_ascii=False)
        if request.trace_id.endswith(":editor-repair-review"):
            return json.dumps({"verdict": "PASS", "issues": []}, ensure_ascii=False)
        return json.dumps(editor, ensure_ascii=False)

    return respond


def _harness(
    tmp_path: Path,
    *,
    editor_payload: dict[str, object] | None = None,
    repair_payload: dict[str, object] | None = None,
    writer_failure: bool = False,
) -> tuple[
    WriterDraftIntegrationService,
    WriterContextHandoffRequest,
    ModelRequest,
    ScriptedModelEndpoint,
]:
    task, needs, units, base_commit = writer_context_inputs()
    package = (
        __import__(
            "novel_agent.services.writer_context_assembler",
            fromlist=["WriterContextAssembler"],
        )
        .WriterContextAssembler()
        .assemble(
            task=task,
            units=units,
            needs=needs,
            basis_commit_id=base_commit,
            basis_snapshot_id=StableId("snapshot.stage3.integration"),
            arm="A",
            writer_token_budget=20_000,
        )
        .package
    )
    assert package is not None
    writing_task = _writing_task()
    store = FilesystemObjectStore(tmp_path / "objects")
    artifacts = ArtifactRepository(store)
    plan = artifacts.put(b"{}", "application/json", VERSION)
    profile = artifacts.put(b"{}", "application/json", VERSION)
    writer_bundle = build_writer_contract_bundle(
        ROOT / "src" / "novel_agent", modes=(AgentMode.DRAFT,)
    )
    editor_bundle = build_editor_contract_bundle(ROOT / "src" / "novel_agent")
    specs = writer_bundle.agent_specs + editor_bundle.agent_specs
    prompts = PromptRegistry(
        {
            (item.prompt_id, item.version): item
            for item in writer_bundle.prompt_templates + editor_bundle.prompt_templates
        }.values()
    )
    skills = SkillRegistry(
        {
            (
                item.skill_id,
                item.version,
            ): item
            for item in writer_bundle.skill_templates + editor_bundle.skill_templates
        }.values()
    )
    endpoint = ScriptedModelEndpoint(
        _responses(
            editor_payload=editor_payload,
            repair_payload=repair_payload,
            writer_failure=writer_failure,
        )
    )
    from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint

    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="stage3-integration-fake",
                model_name="stage3-integration-fake",
                adapter=endpoint,
            ),
        ),
        forbid_external_calls=True,
        structured_max_retries=0,
    )
    runner = StructuredAgentRunner(
        gateway,
        AgentRegistry(specs),
        prompts,
        skills,
    )
    from novel_agent.agents import EditorAgent, WriterAgent

    writer = WriterAgent(runner, prompts, skills)
    editor = EditorAgent(runner)
    writer_service = WriterGenerationService(
        writer,
        gateway,
        artifacts,
        VERSION,
        MODEL_FINGERPRINT,
    )
    editorial_service = EditorialService(editor, artifacts, VERSION)
    integration = WriterDraftIntegrationService(
        writer_service,
        editorial_service,
        artifacts,
        VERSION,
        curator_observer=lambda draft_id, _artifact: CuratorObservation(draft_id=draft_id),
    )
    spec = writer_bundle.agent_specs[0]
    request = WriterContextHandoffRequest(
        integration_id=StableId("integration.stage3.offline"),
        run_id=RunId("run.stage3.integration"),
        task_id=TaskId("task.stage3.integration"),
        project_id=ProjectId("project.stage3.integration"),
        context_package=package,
        writing_task=writing_task,
        plan_artifact=plan,
        project_profile_artifact=profile,
        future_isolation_attestation=FutureIsolationAttestation(
            attestation_id=StableId("attestation.stage3.integration"),
            checkpoint_chapter=package.task_contract.checkpoint_chapter,
            canonical_source_ids=(),
            evaluator_only_source_ids=(),
            passed=True,
            configuration_fingerprint=content_id({"isolation": "offline"}),
        ),
        writer_configuration_fingerprint=content_id(spec.model_dump(mode="json")),
        model_configuration_fingerprint=MODEL_FINGERPRINT,
        budget=WriterBudget(input_token_limit=20_000, output_token_limit=2_000),
    )
    model_request = ModelRequest(
        request_id=StableId("request.stage3.integration.writer"),
        run_id=request.run_id,
        task_id=request.task_id,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace-stage3-integration",
        prompt="replaced by sealed Writer",
    )
    return integration, request, model_request, endpoint


def test_formal_context_handoff_runs_candidate_chain_without_canon_write(tmp_path: Path) -> None:
    integration, request, model_request, endpoint = _harness(tmp_path)

    result = asyncio.run(integration.execute(request, model_request))

    assert result.status is WriterIntegrationStatus.COMPLETED
    assert result.draft is not None
    assert result.editorial_report is not None
    assert result.reconciliation is not None
    assert result.handoff is not None
    assert result.handoff.context.task_contract == request.writing_task.contract_id.root
    assert len(endpoint.requests) == 2
    assert result.draft.candidate_only is True
    assert result.final_candidate_id == result.draft.draft_id
    assert result.complete is True
    assert result.handoff.evidence_ledger_ref == request.context_package.evidence_ledger_ref
    supplied = CuratorObservation(draft_id=result.draft.draft_id)
    replayed = asyncio.run(
        integration.execute(request, model_request, curator_observation=supplied)
    )
    assert replayed.status is WriterIntegrationStatus.COMPLETED


def test_target_mismatch_rejects_before_writer_or_editor_model_calls(tmp_path: Path) -> None:
    integration, request, model_request, endpoint = _harness(tmp_path)
    with pytest.raises(ValidationError, match="supplied together"):
        WriterContextHandoffRequest.model_validate(
            request.model_dump(mode="python") | {"memory_gate_artifact": request.plan_artifact}
        )
    wrong_task = request.writing_task.model_copy(update={"target_chapter": 99})
    request = request.model_copy(update={"writing_task": wrong_task})

    result = asyncio.run(integration.execute(request, model_request))

    assert result.status is WriterIntegrationStatus.HANDOFF_REJECTED
    assert endpoint.requests == []
    assert result.writer_result is None


def test_writer_failure_stops_before_editor(tmp_path: Path) -> None:
    integration, request, model_request, endpoint = _harness(tmp_path, writer_failure=True)

    result = asyncio.run(integration.execute(request, model_request))

    assert result.status is WriterIntegrationStatus.WRITER_FAILED
    assert result.writer_result is not None
    assert result.writer_result.draft is None
    assert len(endpoint.requests) == 1
    assert result.editorial_report is None


def test_major_rewrite_is_a_route_and_does_not_loop(tmp_path: Path) -> None:
    integration, request, model_request, endpoint = _harness(
        tmp_path,
        editor_payload={
            "verdict": "MAJOR_REWRITE",
            "issues": [
                {
                    "issue_type": "structure",
                    "severity": "critical",
                    "description": "The scene premise must be rebuilt.",
                    "structural": True,
                }
            ],
            "rewrite_targets": ("Rebuild the scene around the gate observation.",),
            "rewrite_preserve_requirements": ("Keep the injury constraint.",),
        },
    )

    result = asyncio.run(integration.execute(request, model_request))

    assert result.status is WriterIntegrationStatus.REWRITE_REQUIRED
    assert result.editorial_report is not None
    assert result.editorial_report.rewrite_directive is not None
    assert result.reconciliation is None
    assert len(endpoint.requests) == 2


def test_local_repair_rebinds_observation_to_the_new_candidate(tmp_path: Path) -> None:
    original = json.loads(
        (ROOT / "tests" / "fixtures" / "stage3_writer" / "draft_output.json").read_text(
            encoding="utf-8"
        )
    )["draft_text"]
    old_phrase = "靠蛮力推门并不可行"
    new_phrase = "左臂不能用力推门"
    integration, request, model_request, endpoint = _harness(
        tmp_path,
        editor_payload={
            "verdict": "LOCAL_REPAIR",
            "issues": [
                {
                    "issue_type": "constraint_violation",
                    "severity": "error",
                    "description": "The injury constraint needs a clearer local sentence.",
                    "evidence_quote": old_phrase,
                    "repairable": True,
                }
            ],
            "repair_instructions": ("Clarify the injury constraint in this sentence.",),
        },
        repair_payload={
            "repaired_text": original.replace(old_phrase, new_phrase),
        },
    )

    result = asyncio.run(integration.execute(request, model_request))

    assert result.status is WriterIntegrationStatus.COMPLETED
    assert result.draft is not None
    assert result.repaired_draft is not None
    assert result.repaired_draft.parent_draft_id == result.draft.draft_id
    assert result.repair_verification_report is not None
    assert result.repair_verification_report.verdict.value == "PASS"
    assert result.reconciliation is not None
    assert result.reconciliation.draft_id == result.repaired_draft.draft_id
    assert len(endpoint.requests) == 4


def test_handoff_readiness_rejects_unsafe_context_inputs(tmp_path: Path) -> None:
    integration, request, _model_request, _endpoint = _harness(tmp_path)

    cases = (
        (
            request.model_copy(
                update={
                    "context_package": request.context_package.model_copy(
                        update={
                            "budget_report": request.context_package.budget_report.model_copy(
                                update={"final_status": ContextAssemblyStatus.EVIDENCE_INSUFFICIENT}
                            )
                        }
                    )
                }
            ),
            "CONTEXT_NOT_READY",
        ),
        (
            request.model_copy(
                update={"budget": WriterBudget(input_token_limit=1, output_token_limit=1)}
            ),
            "CONTEXT_BUDGET_INSUFFICIENT",
        ),
        (
            request.model_copy(
                update={
                    "context_package": request.context_package.model_copy(
                        update={"rendered_context": " "}
                    )
                }
            ),
            "CONTEXT_EMPTY",
        ),
        (
            request.model_copy(
                update={
                    "writing_task": request.writing_task.model_copy(update={"target_chapter": 99})
                }
            ),
            "TASK_TARGET_MISMATCH",
        ),
        (
            request.model_copy(
                update={
                    "future_isolation_attestation": request.future_isolation_attestation.model_copy(
                        update={
                            "checkpoint_chapter": (
                                request.context_package.task_contract.checkpoint_chapter + 1
                            )
                        }
                    )
                }
            ),
            "BASIS_MISMATCH",
        ),
        (
            request.model_copy(
                update={
                    "future_isolation_attestation": FutureIsolationAttestation(
                        attestation_id=StableId("attestation.stage3.overlap"),
                        checkpoint_chapter=request.context_package.task_contract.checkpoint_chapter,
                        canonical_source_ids=(StableId("source.future"),),
                        evaluator_only_source_ids=(StableId("source.future"),),
                        overlap_source_ids=(StableId("source.future"),),
                        passed=False,
                        configuration_fingerprint=MODEL_FINGERPRINT,
                    )
                }
            ),
            "FUTURE_ISOLATION_FAILED",
        ),
        (
            request.model_copy(
                update={
                    "writing_task": request.writing_task.model_copy(
                        update={"blocking_gaps": ("missing continuity",)}
                    )
                }
            ),
            "BLOCKING_GAP",
        ),
        (
            request.model_copy(
                update={
                    "context_package": request.context_package.model_copy(
                        update={
                            "gaps": (
                                ContextGap(
                                    gap_id=StableId("gap.stage3.conflict"),
                                    description="conflicting fact",
                                    conflict=True,
                                ),
                            )
                        }
                    )
                }
            ),
            "BLOCKING_GAP",
        ),
    )

    for invalid_request, code in cases:
        with pytest.raises(WriterContextHandoffError) as error:
            integration.handoff(invalid_request)
        assert error.value.code == code


def test_handoff_mode_contracts_and_artifact_identity_are_enforced(tmp_path: Path) -> None:
    integration, request, _model_request, _endpoint = _harness(tmp_path)
    result = asyncio.run(integration.execute(request, _model_request))
    assert result.draft is not None

    invalid_mode_requests = (
        request.model_copy(update={"mode": AgentMode.DRAFT, "prior_draft": result.draft}),
        request.model_copy(update={"mode": AgentMode.CONTINUE}),
        request.model_copy(update={"mode": AgentMode.MAJOR_REWRITE}),
    )
    for invalid_request in invalid_mode_requests:
        with pytest.raises(WriterContextHandoffError) as error:
            integration.handoff(invalid_request)
        assert error.value.code == "MODE_CONTRACT_REJECTED"

    conflicting_plan = request.plan_artifact.model_copy(update={"media_type": "text/plain"})
    with pytest.raises(WriterContextHandoffError) as error:
        integration.handoff(request.model_copy(update={"plan_artifact": conflicting_plan}))
    assert error.value.code == "ARTIFACT_BASIS_MISMATCH"

    with pytest.raises(WriterContextHandoffError) as error:
        integration.handoff(request.model_copy(update={"mode": "invalid-mode"}))
    assert error.value.code == "HANDOFF_REJECTED"


def test_handoff_request_requires_gate_report_and_artifact_as_a_pair(tmp_path: Path) -> None:
    _integration, request, _model_request, _endpoint = _harness(tmp_path)
    payload = request.model_dump(mode="python")
    payload["memory_gate_artifact"] = request.plan_artifact
    with pytest.raises(ValueError, match="Gate report and artifact"):
        WriterContextHandoffRequest(**payload)


def test_result_properties_cover_empty_and_repaired_candidates(tmp_path: Path) -> None:
    empty = WriterIntegrationResult(status=WriterIntegrationStatus.HANDOFF_REJECTED)
    assert empty.draft is None
    assert empty.final_candidate_id is None
    assert empty.complete is False

    original = json.loads(
        (ROOT / "tests" / "fixtures" / "stage3_writer" / "draft_output.json").read_text(
            encoding="utf-8"
        )
    )["draft_text"]
    integration, request, model_request, _endpoint = _harness(
        tmp_path,
        editor_payload={
            "verdict": "LOCAL_REPAIR",
            "issues": [
                {
                    "issue_type": "constraint_violation",
                    "severity": "error",
                    "description": "Clarify the constraint.",
                    "evidence_quote": "靠蛮力推门并不可行",
                    "repairable": True,
                }
            ],
            "repair_instructions": ("Clarify the constraint.",),
        },
        repair_payload={
            "repaired_text": original.replace("靠蛮力推门并不可行", "左臂不能用力推门")
        },
    )
    result = asyncio.run(integration.execute(request, model_request))
    assert result.repaired_draft is not None
    assert result.final_candidate_id == result.repaired_draft.draft_id


def test_integration_stops_when_writer_or_editor_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration, request, model_request, _endpoint = _harness(tmp_path)

    class RaisingWriter:
        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("writer exploded")

    monkeypatch.setattr(integration, "_writer", RaisingWriter())
    writer_result = asyncio.run(integration.execute(request, model_request))
    assert writer_result.status is WriterIntegrationStatus.WRITER_FAILED
    assert "writer exploded" in (writer_result.failure_detail or "")

    integration, request, model_request, _endpoint = _harness(tmp_path / "editor")

    class RaisingEditorial:
        async def review(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("editor exploded")

    monkeypatch.setattr(integration, "_editorial", RaisingEditorial())
    editor_result = asyncio.run(integration.execute(request, model_request))
    assert editor_result.status is WriterIntegrationStatus.EDITOR_FAILED
    assert "editor exploded" in (editor_result.failure_detail or "")


def test_local_repair_failure_is_terminal_for_the_candidate(tmp_path: Path) -> None:
    integration, request, model_request, _endpoint = _harness(
        tmp_path,
        editor_payload={
            "verdict": "LOCAL_REPAIR",
            "issues": [
                {
                    "issue_type": "style",
                    "severity": "error",
                    "description": "Repair the local style issue.",
                    "evidence_quote": "靠蛮力推门并不可行",
                    "repairable": True,
                }
            ],
            "repair_instructions": ("Repair the local style issue.",),
        },
        repair_payload={"repaired_text": ""},
    )

    result = asyncio.run(integration.execute(request, model_request))

    assert result.status is WriterIntegrationStatus.EDITOR_FAILED
    assert result.repaired_draft is None
    assert result.editorial_report is not None


@pytest.mark.parametrize(
    ("verification", "expected_status"),
    (
        (EditorialVerdict.MAJOR_REWRITE, WriterIntegrationStatus.REWRITE_REQUIRED),
        (EditorialVerdict.LOCAL_REPAIR, WriterIntegrationStatus.EDITOR_FAILED),
        (RuntimeError("review failed"), WriterIntegrationStatus.EDITOR_FAILED),
    ),
)
def test_local_repair_verification_stops_on_non_pass_or_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verification: EditorialVerdict | RuntimeError,
    expected_status: WriterIntegrationStatus,
) -> None:
    original = json.loads(
        (ROOT / "tests" / "fixtures" / "stage3_writer" / "draft_output.json").read_text(
            encoding="utf-8"
        )
    )["draft_text"]
    integration, request, model_request, _endpoint = _harness(
        tmp_path,
        editor_payload={
            "verdict": "LOCAL_REPAIR",
            "issues": [
                {
                    "issue_type": "style",
                    "severity": "error",
                    "description": "Repair the local style issue.",
                    "evidence_quote": "靠蛮力推门并不可行",
                    "repairable": True,
                }
            ],
            "repair_instructions": ("Repair the local style issue.",),
        },
        repair_payload={
            "repaired_text": original.replace("靠蛮力推门并不可行", "左臂不能用力推门")
        },
    )

    async def verify(*args: object, **_kwargs: object) -> object:
        if isinstance(verification, RuntimeError):
            raise verification
        report = cast(EditorialReport, args[1])
        repaired = cast(RepairedDraft, args[2])
        return report.model_copy(update={"draft_id": repaired.draft_id, "verdict": verification})

    monkeypatch.setattr(integration._editorial, "review_repaired", verify)
    result = asyncio.run(integration.execute(request, model_request))

    assert result.status is expected_status
    assert result.reconciliation is None


def test_reconciliation_requires_the_matching_independent_observation(tmp_path: Path) -> None:
    integration, request, model_request, _endpoint = _harness(tmp_path)
    wrong = CuratorObservation(draft_id=content_id({"draft": "other"}))
    result = asyncio.run(integration.execute(request, model_request, curator_observation=wrong))
    assert result.status is WriterIntegrationStatus.RECONCILIATION_FAILED

    integration, request, model_request, _endpoint = _harness(tmp_path / "none")
    integration._curator_observer = None
    result = asyncio.run(integration.execute(request, model_request))
    assert result.status is WriterIntegrationStatus.RECONCILIATION_FAILED


def test_async_curator_observer_is_supported_by_run_alias(tmp_path: Path) -> None:
    integration, request, model_request, _endpoint = _harness(tmp_path)

    async def observe(draft_id: ArtifactId, _artifact: object) -> CuratorObservation:
        return CuratorObservation(draft_id=draft_id)

    integration._curator_observer = observe
    result = asyncio.run(integration.run(request, model_request))
    assert result.status is WriterIntegrationStatus.COMPLETED


def test_curator_observer_return_contract_is_enforced(tmp_path: Path) -> None:
    integration, request, model_request, _endpoint = _harness(tmp_path)

    def invalid_observer(_draft_id: ArtifactId, _artifact: object) -> CuratorObservation:
        return cast(CuratorObservation, object())

    integration._curator_observer = invalid_observer
    result = asyncio.run(integration.execute(request, model_request))
    assert result.status is WriterIntegrationStatus.RECONCILIATION_FAILED

    integration, request, model_request, _endpoint = _harness(tmp_path / "wrong")
    integration._curator_observer = lambda _draft_id, _artifact: CuratorObservation(
        draft_id=content_id({"draft": "other"})
    )
    result = asyncio.run(integration.execute(request, model_request))
    assert result.status is WriterIntegrationStatus.RECONCILIATION_FAILED
