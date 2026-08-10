from __future__ import annotations

import asyncio
import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

try:
    from scripts.run_stage3_writer_shadow import DEFAULT_FIXTURE_DIRECTORY, main
except ImportError:  # pragma: no cover - isolated pre-migration Writer compatibility
    from scripts.run_stage2b_writer_shadow import (  # type: ignore[import-not-found, no-redef]
        DEFAULT_FIXTURE_DIRECTORY,
        main,
    )

import novel_agent.agents.editor as editor_agent_module
import novel_agent.services.editorial as editorial_service_module
import novel_agent.services.writer_change_reconciliation as reconciliation
from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.agents import (
    AgentRegistry,
    EditorAgent,
    StructuredAgentRunner,
    build_editor_contract_bundle,
)
from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.base import DomainModel
from novel_agent.domain.changes import (
    ChangeOperation,
    ChangeOperationType,
    ObservedChangeSet,
)
from novel_agent.domain.editorial import (
    CuratorChangeObservation,
    CuratorObservation,
    DraftSpan,
    EditorialIssue,
    EditorialIssueDraft,
    EditorialIssueType,
    EditorialLocation,
    EditorialRepairHistoryEntry,
    EditorialReport,
    EditorialReviewInput,
    EditorialSeverity,
    EditorialVerdict,
    EditorRepairPayload,
    EditorReviewPayload,
    LocalRepairScope,
    ReconciliationClass,
    ReconciliationComparison,
    ReconciliationResult,
    RepairedDraft,
)
from novel_agent.domain.generation import (
    DeclaredMemoryHint,
    DraftArtifact,
    MemoryHintChangeKind,
    RewriteScope,
    WritingTaskContract,
)

try:
    from novel_agent.domain.generation import WriterContextSnapshot
except ImportError:  # pragma: no cover - isolated pre-migration Writer compatibility
    from novel_agent.domain.memory import (  # type: ignore[assignment]
        Stage1ContextPackage as WriterContextSnapshot,
    )
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.stage2 import AgentMode, AgentType, ExecutionStatus
from novel_agent.prompts import PromptRegistry
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.editorial import (
    EditorialRepairError,
    EditorialReviewError,
    EditorialService,
)
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.writer_change_reconciliation import (
    ReconciliationError,
    WriterChangeReconciliationService,
)
from novel_agent.skills import SkillRegistry

VERSION = SchemaVersion("1.0.0")


@dataclass(slots=True)
class Harness:
    output: Path
    artifacts: ArtifactRepository
    endpoint: FakeModelEndpoint
    service: EditorialService
    review_input: EditorialReviewInput


def _harness(tmp_path: Path, response: str, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setenv("NOVEL_AGENT_FORBID_MODEL_CALLS", "true")
    output = tmp_path / "writer-shadow"
    assert (
        main(
            (
                "--fixture-directory",
                str(DEFAULT_FIXTURE_DIRECTORY),
                "--output-directory",
                str(output),
                "--run-id",
                "run.editor.fixture",
            )
        )
        == 0
    )
    artifacts = ArtifactRepository(FilesystemObjectStore(output / "objects"))
    draft = DraftArtifact.model_validate_json(
        (output / "draft_artifact.json").read_text(encoding="utf-8")
    )
    context = WriterContextSnapshot.model_validate_json(
        (DEFAULT_FIXTURE_DIRECTORY / "context_package.json").read_text(encoding="utf-8")
    )
    writing_task = WritingTaskContract.model_validate_json(
        (DEFAULT_FIXTURE_DIRECTORY / "writing_task_contract.json").read_text(encoding="utf-8")
    )
    bundle = build_editor_contract_bundle()
    endpoint = FakeModelEndpoint(response)
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="editor-fake",
                model_name="editor-fake",
                adapter=endpoint,
            ),
        ),
        forbid_external_calls=True,
    )
    prompts = PromptRegistry(bundle.prompt_templates)
    skills = SkillRegistry(bundle.skill_templates)
    runner = StructuredAgentRunner(
        gateway,
        AgentRegistry(bundle.agent_specs),
        prompts,
        skills,
    )
    service = EditorialService(EditorAgent(runner), artifacts, VERSION)
    return Harness(
        output=output,
        artifacts=artifacts,
        endpoint=endpoint,
        service=service,
        review_input=EditorialReviewInput(
            draft=draft,
            writing_task=writing_task,
            context=context,
        ),
    )


def _request(name: str) -> ModelRequest:
    return ModelRequest(
        request_id=StableId(f"request.editor.{name}"),
        run_id=RunId("run.editor.fixture"),
        task_id=TaskId("task.run.editor.fixture"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id=f"trace.editor.{name}",
        prompt="replaced by EditorAgent",
    )


def _pass() -> str:
    return EditorReviewPayload(verdict=EditorialVerdict.PASS).model_dump_json()


def _local_review(quote: str) -> str:
    return json.dumps(
        {
            "verdict": "LOCAL_REPAIR",
            "issues": [
                {
                    "issue_type": EditorialIssueType.STYLE.value,
                    "severity": EditorialSeverity.ERROR.value,
                    "description": "Replace one imprecise action phrase.",
                    "evidence_quote": quote,
                    "occurrence": 0,
                    "repairable": True,
                    "structural": False,
                }
            ],
            "repair_instructions": ["Clarify the action without changing the scene beat."],
            "preserve_requirements": ["Keep the injury constraint and POV unchanged."],
            "rewrite_targets": [],
            "rewrite_preserve_requirements": [],
            "unresolved_needs": [],
        },
        ensure_ascii=False,
    )


def _major_review() -> str:
    return json.dumps(
        {
            "verdict": "MAJOR_REWRITE",
            "issues": [
                {
                    "issue_type": EditorialIssueType.STRUCTURE.value,
                    "severity": EditorialSeverity.ERROR.value,
                    "description": "The scene needs a different entrance structure.",
                    "evidence_quote": None,
                    "occurrence": 0,
                    "repairable": False,
                    "structural": True,
                }
            ],
            "repair_instructions": [],
            "preserve_requirements": [],
            "rewrite_targets": [
                "Rebuild the entrance action around the discovered light condition."
            ],
            "rewrite_preserve_requirements": ["Preserve the injured arm and limited POV."],
            "unresolved_needs": [],
        },
        ensure_ascii=False,
    )


def test_editor_contract_bundle_is_independent_and_zero_write_capability() -> None:
    bundle = build_editor_contract_bundle()

    assert tuple(spec.mode.value for spec in bundle.specs) == ("review", "local_repair")
    assert all(spec.agent_type.value == "editor" for spec in bundle.specs)
    assert all(spec.tool_policy.allowed_tools == () for spec in bundle.specs)
    assert all(spec.tool_policy.max_tool_calls == 0 for spec in bundle.specs)
    assert all("memory.write" in spec.tool_policy.denied_tools for spec in bundle.specs)
    assert all("canonical.commit" in spec.tool_policy.denied_tools for spec in bundle.specs)
    with pytest.raises(editor_agent_module.EditorAgentError, match="at least one"):
        build_editor_contract_bundle(modes=())
    with pytest.raises(editor_agent_module.EditorAgentError, match="unique"):
        build_editor_contract_bundle(modes=(AgentMode.REVIEW, AgentMode.REVIEW))
    with pytest.raises(editor_agent_module.EditorAgentError, match="unsupported Editor modes"):
        build_editor_contract_bundle(modes=(AgentMode.DRAFT,))
    assert editor_agent_module._json_safe(AgentMode.REVIEW) == AgentMode.REVIEW.value


def test_pass_review_is_read_only_and_does_not_replace_original_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path, _pass(), monkeypatch)
    original = harness.artifacts.read_verified(harness.review_input.draft.text_artifact)

    report = asyncio.run(harness.service.review(harness.review_input, _request("pass")))

    assert report.verdict is EditorialVerdict.PASS
    assert report.issues == ()
    assert harness.artifacts.read_verified(harness.review_input.draft.text_artifact) == original
    assert len(harness.endpoint.requests) == 1
    with pytest.raises(editor_agent_module.EditorAgentError, match="unsupported Editor mode"):
        harness.service._editor.prepare(
            AgentMode.DRAFT,
            _request("unsupported-mode"),
            {},
        )


def test_local_repair_creates_child_candidate_and_preserves_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quote = "石门向里退开"
    harness = _harness(tmp_path, _local_review(quote), monkeypatch)
    parent_bytes = harness.artifacts.read_verified(harness.review_input.draft.text_artifact)
    report = asyncio.run(harness.service.review(harness.review_input, _request("local-review")))
    assert report.verdict is EditorialVerdict.LOCAL_REPAIR
    assert report.repair_scope is not None

    original = parent_bytes.decode("utf-8")
    repaired = original.replace(quote, "石门向外退开", 1)
    harness.endpoint.response_text = json.dumps(
        {
            "repaired_text": repaired,
            "self_observations": ["Only the scoped action phrase changed."],
        },
        ensure_ascii=False,
    )
    child = asyncio.run(
        harness.service.repair(harness.review_input, report, _request("local-repair"))
    )

    assert isinstance(child, RepairedDraft)
    assert child.parent_draft_id == harness.review_input.draft.draft_id
    assert child.draft_id != child.parent_draft_id
    assert harness.artifacts.read_verified(harness.review_input.draft.text_artifact) == parent_bytes
    assert harness.artifacts.read_verified(child.text_artifact) == repaired.encode("utf-8")


def test_repaired_candidate_review_failures_are_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quote = "石门向里退开"
    harness = _harness(tmp_path, _local_review(quote), monkeypatch)
    report = asyncio.run(harness.service.review(harness.review_input, _request("review-errors")))
    original = harness.artifacts.read_verified(harness.review_input.draft.text_artifact).decode(
        "utf-8"
    )
    harness.endpoint.response_text = json.dumps(
        {"repaired_text": original.replace(quote, "石门向外退开", 1)},
        ensure_ascii=False,
    )
    child = asyncio.run(
        harness.service.repair(harness.review_input, report, _request("repair-errors"))
    )

    with pytest.raises(EditorialReviewError, match="another Draft"):
        asyncio.run(
            harness.service.review_repaired(
                harness.review_input,
                report,
                child.model_copy(update={"parent_draft_id": child.draft_id}),
                _request("wrong-parent"),
            )
        )
    with pytest.raises(EditorialReviewError, match="another repair report"):
        asyncio.run(
            harness.service.review_repaired(
                harness.review_input,
                report,
                child.model_copy(update={"repair_report_id": StableId("report.other")}),
                _request("wrong-report"),
            )
        )

    original_read = harness.artifacts.read_verified
    monkeypatch.setattr(
        harness.artifacts,
        "read_verified",
        lambda _artifact: (_ for _ in ()).throw(OSError("unreadable")),
    )
    with pytest.raises(EditorialReviewError, match="could not read"):
        asyncio.run(
            harness.service.review_repaired(
                harness.review_input, report, child, _request("unreadable")
            )
        )
    monkeypatch.setattr(harness.artifacts, "read_verified", original_read)

    blank = harness.artifacts.put(b" ", "text/plain; charset=utf-8", VERSION)
    with pytest.raises(EditorialReviewError, match="blank"):
        asyncio.run(
            harness.service.review_repaired(
                harness.review_input,
                report,
                child.model_copy(update={"text_artifact": blank}),
                _request("blank"),
            )
        )

    harness.endpoint.response_text = "{invalid-json"
    with pytest.raises(EditorialReviewError, match="did not form a valid report"):
        asyncio.run(
            harness.service.review_repaired(
                harness.review_input, report, child, _request("invalid-review")
            )
        )


def test_local_repair_rejects_out_of_scope_change_without_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quote = "石门向里退开"
    harness = _harness(tmp_path, _local_review(quote), monkeypatch)
    report = asyncio.run(harness.service.review(harness.review_input, _request("local-review")))
    original = harness.artifacts.read_verified(harness.review_input.draft.text_artifact).decode(
        "utf-8"
    )
    harness.endpoint.response_text = json.dumps(
        {"repaired_text": "错误" + original, "self_observations": []},
        ensure_ascii=False,
    )

    with pytest.raises(EditorialRepairError, match="outside"):
        asyncio.run(harness.service.repair(harness.review_input, report, _request("local-repair")))

    assert harness.artifacts.read_verified(
        harness.review_input.draft.text_artifact
    ) == original.encode("utf-8")


def test_major_rewrite_returns_writer_directive_and_does_not_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path, _major_review(), monkeypatch)

    report = asyncio.run(harness.service.review(harness.review_input, _request("major")))

    assert report.verdict is EditorialVerdict.MAJOR_REWRITE
    assert report.repair_scope is None
    assert report.rewrite_directive is not None
    assert report.rewrite_directive.scope.value == "major_rewrite"
    assert report.rewrite_directive.parent_draft_id == harness.review_input.draft.draft_id
    directive_bytes = harness.artifacts.read_verified(report.rewrite_directive.directive_artifact)
    assert json.loads(directive_bytes)["scope"] == "major_rewrite"

    with pytest.raises(EditorialRepairError, match="LOCAL_REPAIR"):
        asyncio.run(harness.service.repair(harness.review_input, report, _request("not-local")))


def test_invalid_review_route_fails_without_pseudo_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = json.dumps(
        {
            "verdict": "PASS",
            "issues": [
                {
                    "issue_type": "constraint_violation",
                    "severity": "error",
                    "description": "blocking",
                    "repairable": True,
                    "structural": False,
                }
            ],
        }
    )
    harness = _harness(tmp_path, invalid, monkeypatch)

    with pytest.raises(EditorialReviewError):
        asyncio.run(harness.service.review(harness.review_input, _request("invalid")))


def test_reconciliation_distinguishes_all_four_result_classes() -> None:
    service = WriterChangeReconciliationService()
    hints = (
        DeclaredMemoryHint(
            subject_hint="石门",
            change_kind=MemoryHintChangeKind.CHANGE,
            predicate_hint="state",
            value_hint="open",
            evidence_quote="石门向里退开",
            confidence=0.9,
        ),
        DeclaredMemoryHint(
            subject_hint="守卫",
            change_kind=MemoryHintChangeKind.ADD,
            predicate_hint="location",
            value_hint="塔门",
            evidence_quote="守卫出现",
            confidence=0.5,
        ),
        DeclaredMemoryHint(
            subject_hint="铜镜",
            change_kind=MemoryHintChangeKind.CHANGE,
            predicate_hint="state",
            value_hint="举起",
            evidence_quote="铜镜",
            confidence=0.6,
        ),
    )
    observation = CuratorObservation(
        draft_id=ArtifactId("sha256:" + "a" * 64),
        changes=(
            CuratorChangeObservation(
                observation_id=StableId("observation.gate"),
                subject_hint="石门",
                change_kind=MemoryHintChangeKind.CHANGE,
                predicate_hint="state",
                value_hint="open",
            ),
            CuratorChangeObservation(
                observation_id=StableId("observation.mirror"),
                subject_hint="铜镜",
                change_kind=MemoryHintChangeKind.CHANGE,
                predicate_hint="state",
                value_hint="放下",
            ),
            CuratorChangeObservation(
                observation_id=StableId("observation.lin"),
                subject_hint="林澈",
                change_kind=MemoryHintChangeKind.CHANGE,
                predicate_hint="location",
                value_hint="塔内",
            ),
        ),
    )

    result = service.reconcile(observation.draft_id, hints, observation)

    assert {item.classification for item in result.comparisons} == {
        ReconciliationClass.MATCHED,
        ReconciliationClass.DECLARED_ONLY,
        ReconciliationClass.MISMATCHED,
        ReconciliationClass.OBSERVED_ONLY,
    }
    assert len(result.matched) == 1
    assert len(result.declared_only) == 1
    assert len(result.mismatched) == 1
    assert len(result.observed_only) == 1


def test_reconciliation_binds_observation_to_current_draft_and_adapts_changeset() -> None:
    service = WriterChangeReconciliationService()
    draft_id = ArtifactId("sha256:" + "b" * 64)
    changeset = ObservedChangeSet(
        change_set_id=StableId("change-set.editor"),
        base_commit=CommitId("sha256:" + "b" * 64),
        source_artifact=_artifact("c"),
        operations=(
            ChangeOperation(
                operation_id=StableId("operation.editor"),
                root_kind=RootKind.WORLD,
                operation=ChangeOperationType.CREATE,
                target_id=StableId("entity.editor"),
                payload={"subject": "石门", "predicate": "state", "value": "open"},
            ),
        ),
    )
    observation = service.observation_from_change_set(draft_id, changeset)
    assert observation.changes[0].subject_hint == "石门"
    assert observation.changes[0].change_kind is MemoryHintChangeKind.ADD
    result = service.reconcile(draft_id, (), observation)
    assert result.observed_only[0].classification is ReconciliationClass.OBSERVED_ONLY

    with pytest.raises(ReconciliationError, match="another Draft"):
        service.reconcile(ArtifactId("sha256:" + "d" * 64), (), observation)


def _artifact(digit: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + digit * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=VERSION,
    )


def _validated_replace[ModelT: DomainModel](model: ModelT, **updates: object) -> ModelT:
    model_type = type(model)
    payload = {name: getattr(model, name) for name in model_type.model_fields}
    return model_type(**(payload | updates))


def test_editorial_value_contract_negative_edges() -> None:
    with pytest.raises(ValueError, match="end precedes"):
        DraftSpan(start=2, end=1)
    with pytest.raises(ValueError, match="supplied together"):
        EditorialLocation(start=1)
    with pytest.raises(ValueError, match="end precedes"):
        EditorialLocation(start=2, end=1)
    with pytest.raises(ValueError, match="block id"):
        EditorialLocation(block_id=StableId("block.editorial"))
    with pytest.raises(ValueError, match="issue ids must be unique"):
        LocalRepairScope(
            issue_ids=(StableId("issue.same"), StableId("issue.same")),
            allowed_spans=(DraftSpan(start=0, end=1),),
            instructions=("repair",),
        )
    with pytest.raises(ValueError, match="must not be blank"):
        EditorRepairPayload(repaired_text=" \n ")


@pytest.mark.parametrize(
    "payload",
    (
        {"verdict": EditorialVerdict.PASS, "repair_instructions": ("repair",)},
        {
            "verdict": EditorialVerdict.PASS,
            "issues": (
                EditorialIssueDraft(
                    issue_type=EditorialIssueType.STYLE,
                    severity=EditorialSeverity.ERROR,
                    description="blocking",
                ),
            ),
        },
        {"verdict": EditorialVerdict.LOCAL_REPAIR, "issues": ()},
        {
            "verdict": EditorialVerdict.LOCAL_REPAIR,
            "issues": (
                EditorialIssueDraft(
                    issue_type=EditorialIssueType.STYLE,
                    severity=EditorialSeverity.ERROR,
                    description="blocking",
                    repairable=True,
                ),
            ),
        },
        {
            "verdict": EditorialVerdict.LOCAL_REPAIR,
            "issues": (
                EditorialIssueDraft(
                    issue_type=EditorialIssueType.STYLE,
                    severity=EditorialSeverity.ERROR,
                    description="blocking",
                    repairable=True,
                ),
            ),
            "repair_instructions": ("repair",),
            "rewrite_targets": ("rewrite",),
        },
        {
            "verdict": EditorialVerdict.LOCAL_REPAIR,
            "issues": (
                EditorialIssueDraft(
                    issue_type=EditorialIssueType.STYLE,
                    severity=EditorialSeverity.ERROR,
                    description="blocking",
                    repairable=True,
                ),
            ),
            "repair_instructions": ("repair",),
            "planner_replan_required": True,
        },
        {
            "verdict": EditorialVerdict.LOCAL_REPAIR,
            "issues": (
                EditorialIssueDraft(
                    issue_type=EditorialIssueType.STRUCTURE,
                    severity=EditorialSeverity.ERROR,
                    description="structural",
                    structural=True,
                ),
            ),
            "repair_instructions": ("repair",),
        },
        {
            "verdict": EditorialVerdict.MAJOR_REWRITE,
            "issues": (
                EditorialIssueDraft(
                    issue_type=EditorialIssueType.STRUCTURE,
                    severity=EditorialSeverity.CRITICAL,
                    description="critical",
                ),
            ),
        },
        {
            "verdict": EditorialVerdict.MAJOR_REWRITE,
            "issues": (
                EditorialIssueDraft(
                    issue_type=EditorialIssueType.STRUCTURE,
                    severity=EditorialSeverity.CRITICAL,
                    description="critical",
                ),
            ),
            "rewrite_targets": ("rewrite",),
            "repair_instructions": ("repair",),
        },
        {
            "verdict": EditorialVerdict.MAJOR_REWRITE,
            "issues": (
                EditorialIssueDraft(
                    issue_type=EditorialIssueType.STYLE,
                    severity=EditorialSeverity.WARNING,
                    description="nonblocking",
                ),
            ),
            "rewrite_targets": ("rewrite",),
        },
    ),
)
def test_editor_review_payload_route_edges(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        EditorReviewPayload.model_validate(payload)


def test_editorial_review_input_and_report_lineage_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pass_harness = _harness(tmp_path / "pass", _pass(), monkeypatch)
    review_input = pass_harness.review_input
    with pytest.raises(ValueError, match="different tasks"):
        _validated_replace(
            review_input,
            context=_validated_replace(
                cast(WriterContextSnapshot, review_input.context),
                task_contract="another-task",
            ),
        )
    with pytest.raises(ValueError, match="different snapshots"):
        _validated_replace(
            review_input,
            context=_validated_replace(
                cast(WriterContextSnapshot, review_input.context),
                context_id=StableId("context.editor.other"),
            ),
        )
    with pytest.raises(ValueError, match="current Draft"):
        _validated_replace(
            review_input,
            prior_repair_history=(
                EditorialRepairHistoryEntry(
                    report_id=StableId("report.editor.history"),
                    draft_id=review_input.draft.draft_id,
                    verdict=EditorialVerdict.PASS,
                ),
            ),
        )

    pass_report = asyncio.run(pass_harness.service.review(review_input, _request("domain-pass")))
    blocking_issue = EditorialIssue(
        issue_id=StableId("issue.editor.blocking"),
        issue_type=EditorialIssueType.STYLE,
        severity=EditorialSeverity.ERROR,
        description="blocking",
        repairable=True,
    )
    for updates in (
        {"planner_replan_required": True},
        {"unresolved_needs": ("missing context",)},
        {"issues": (blocking_issue,)},
    ):
        with pytest.raises(ValueError):
            _validated_replace(pass_report, **updates)
    with pytest.raises(ValueError, match="Editor"):
        _validated_replace(
            pass_report,
            receipt=_validated_replace(pass_report.receipt, agent_type=AgentType.WRITER),
        )
    with pytest.raises(ValueError, match="REVIEW"):
        _validated_replace(
            pass_report,
            receipt=_validated_replace(
                pass_report.receipt,
                agent_mode=AgentMode.LOCAL_REPAIR,
            ),
        )
    with pytest.raises(ValueError, match="successful"):
        _validated_replace(
            pass_report,
            receipt=_validated_replace(pass_report.receipt, status=ExecutionStatus.FAILED),
        )

    quote = "石门向里退开"
    local_harness = _harness(tmp_path / "local", _local_review(quote), monkeypatch)
    local_report = asyncio.run(
        local_harness.service.review(local_harness.review_input, _request("domain-local"))
    )
    assert local_report.repair_scope is not None
    duplicate_issue_report = {
        name: getattr(local_report, name) for name in EditorialReport.model_fields
    }
    duplicate_issue_report["issues"] = (local_report.issues[0], local_report.issues[0])
    with pytest.raises(ValueError, match="issue ids must be unique"):
        EditorialReport(**duplicate_issue_report)
    with pytest.raises(ValueError, match="requires only a repair scope"):
        _validated_replace(local_report, repair_scope=None)
    unknown_scope = _validated_replace(
        local_report.repair_scope,
        issue_ids=(StableId("issue.editor.unknown"),),
    )
    with pytest.raises(ValueError, match="unknown issue"):
        _validated_replace(local_report, repair_scope=unknown_scope)
    with pytest.raises(ValueError, match="cover all blocking"):
        _validated_replace(
            local_report,
            issues=(_validated_replace(local_report.issues[0], repairable=False),),
        )

    major_harness = _harness(tmp_path / "major", _major_review(), monkeypatch)
    major_report = asyncio.run(
        major_harness.service.review(major_harness.review_input, _request("domain-major"))
    )
    assert major_report.rewrite_directive is not None
    with pytest.raises(ValueError, match="requires only a rewrite directive"):
        _validated_replace(major_report, rewrite_directive=None)
    with pytest.raises(ValueError, match="parent"):
        _validated_replace(
            major_report,
            rewrite_directive=_validated_replace(
                major_report.rewrite_directive,
                parent_draft_id=ArtifactId("sha256:" + "0" * 64),
            ),
        )
    with pytest.raises(ValueError, match="wrong scope"):
        _validated_replace(
            major_report,
            rewrite_directive=_validated_replace(
                major_report.rewrite_directive,
                scope=RewriteScope.LOCAL_REPAIR,
            ),
        )
    with pytest.raises(ValueError, match="structural or critical"):
        _validated_replace(
            major_report,
            issues=(
                _validated_replace(
                    major_report.issues[0],
                    structural=False,
                    severity=EditorialSeverity.WARNING,
                ),
            ),
        )


def test_repaired_draft_and_reconciliation_contract_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quote = "石门向里退开"
    harness = _harness(tmp_path, _local_review(quote), monkeypatch)
    report = asyncio.run(harness.service.review(harness.review_input, _request("edge-review")))
    original = harness.artifacts.read_verified(harness.review_input.draft.text_artifact).decode()
    harness.endpoint.response_text = EditorRepairPayload(
        repaired_text=original.replace(quote, "石门向外退开", 1)
    ).model_dump_json()
    repaired = asyncio.run(
        harness.service.repair(harness.review_input, report, _request("edge-repair"))
    )
    assert repaired.source_draft_id == repaired.parent_draft_id
    with pytest.raises(ValueError, match="new candidate"):
        _validated_replace(repaired, draft_id=repaired.parent_draft_id)
    with pytest.raises(ValueError, match="Editor"):
        _validated_replace(
            repaired,
            editor_receipt=_validated_replace(
                repaired.editor_receipt,
                agent_type=AgentType.WRITER,
            ),
        )
    with pytest.raises(ValueError, match="LOCAL_REPAIR"):
        _validated_replace(
            repaired,
            editor_receipt=_validated_replace(
                repaired.editor_receipt,
                agent_mode=AgentMode.REVIEW,
            ),
        )
    with pytest.raises(ValueError, match="successful"):
        _validated_replace(
            repaired,
            editor_receipt=_validated_replace(
                repaired.editor_receipt,
                status=ExecutionStatus.FAILED,
            ),
        )

    hint = DeclaredMemoryHint(
        subject_hint="石门",
        change_kind=MemoryHintChangeKind.CHANGE,
        predicate_hint="state",
        value_hint="open",
        evidence_quote=quote,
        confidence=1.0,
    )
    observation = CuratorChangeObservation(
        observation_id=StableId("observation.editor.edge"),
        subject_hint="石门",
        change_kind=MemoryHintChangeKind.CHANGE,
        predicate_hint="state",
        value_hint="open",
    )
    with pytest.raises(ValueError, match="observation ids must be unique"):
        CuratorObservation(
            draft_id=repaired.draft_id,
            changes=(observation, observation),
        )
    curator = CuratorObservation(draft_id=repaired.draft_id, changes=(observation,))
    result = WriterChangeReconciliationService().reconcile(
        repaired.draft_id,
        (hint,),
        curator,
    )
    matched = result.comparisons[0]
    for classification, updates in (
        (ReconciliationClass.MATCHED, {"writer_hint": None}),
        (ReconciliationClass.DECLARED_ONLY, {"observation": observation}),
        (ReconciliationClass.OBSERVED_ONLY, {"writer_hint": hint}),
        (ReconciliationClass.MISMATCHED, {"observation": None}),
    ):
        payload = {name: getattr(matched, name) for name in ReconciliationComparison.model_fields}
        payload.update({"classification": classification, **updates})
        with pytest.raises(ValueError):
            ReconciliationComparison(**payload)
    with pytest.raises(ValueError, match="Writer hint index"):
        _validated_replace(matched, writer_hint_index=None)
    with pytest.raises(ValueError, match="observation id"):
        _validated_replace(matched, observation_id=None)

    with pytest.raises(ValueError, match="another Draft"):
        _validated_replace(
            result,
            draft_id=ArtifactId("sha256:" + "9" * 64),
        )
    with pytest.raises(ValueError, match="every Writer hint"):
        _validated_replace(result, comparisons=())
    unknown_observation = _validated_replace(
        matched,
        observation_id=StableId("observation.editor.unknown"),
        observation=_validated_replace(
            observation,
            observation_id=StableId("observation.editor.unknown"),
        ),
    )
    with pytest.raises(ValueError, match=r"every observation|unknown Curator"):
        ReconciliationResult(
            result_id=result.result_id,
            draft_id=result.draft_id,
            writer_hints=result.writer_hints,
            curator_observation=result.curator_observation,
            comparisons=(unknown_observation,),
        )


def test_reconciliation_helper_edges_are_explicit() -> None:
    hint = DeclaredMemoryHint(
        subject_hint="subject",
        change_kind=MemoryHintChangeKind.ADD,
        predicate_hint="state",
        value_hint="open",
        evidence_quote="subject state open",
        confidence=1.0,
    )
    observation = CuratorChangeObservation(
        observation_id=StableId("observation.reconciliation.helpers"),
        subject_hint="subject",
        change_kind=MemoryHintChangeKind.END,
        predicate_hint="location",
        value_hint="closed",
    )
    reason = reconciliation._mismatch_reason(hint, observation)
    assert "change type" in reason and "predicate" in reason and "value/object" in reason
    equal_reason = reconciliation._mismatch_reason(
        hint,
        observation.model_copy(
            update={
                "change_kind": hint.change_kind,
                "predicate_hint": hint.predicate_hint,
                "value_hint": hint.value_hint,
            }
        ),
    )
    assert equal_reason.endswith("the change")
    target = StableId("entity.reconciliation.fallback")
    assert reconciliation._subject_from_payload(target, {}) == target.root
    assert reconciliation._subject_from_payload(target, "not-a-mapping") == target.root
    assert reconciliation._optional_text("not-a-mapping", "predicate") is None
    assert reconciliation._optional_value("not-a-mapping") is None
    assert reconciliation._optional_value({}) is None


def test_editorial_context_summary_supports_legacy_section_shapes() -> None:
    summary = editorial_service_module._context_summary(
        cast(
            EditorialReviewInput,
            SimpleNamespace(
                context=SimpleNamespace(
                    mandatory_constraints=({"item_id": "stable:constraint", "entity_ids": ["e1"]},),
                    current_world_state=("plain context item",),
                    active_plan_obligations=({"unit_id": "unit.plan", "entity_ids": "invalid"},),
                    relevant_historical_events=({"item_id": 123},),
                )
            ),
        )
    )

    assert summary[0]["item_id"] == "constraint"
    assert summary[0]["entity_ids"] == ("e1",)
    assert summary[1] == {"text": "plain context item"}
    assert summary[2]["item_id"] == "unit.plan"
    assert summary[2]["entity_ids"] == ()
    assert summary[3]["item_id"] == 123


@pytest.mark.parametrize(
    "payload",
    (
        _local_review("quote absent from the draft"),
        json.dumps(
            {
                "verdict": "LOCAL_REPAIR",
                "issues": [
                    {
                        "issue_type": "style",
                        "severity": "error",
                        "description": "wrong block",
                        "evidence_quote": "石门向里退开",
                        "block_hint": "block.wrong",
                        "repairable": True,
                    }
                ],
                "repair_instructions": ["repair"],
            }
        ),
        json.dumps(
            {
                "verdict": "LOCAL_REPAIR",
                "issues": [
                    {
                        "issue_type": "style",
                        "severity": "warning",
                        "description": "no blocking issue",
                        "repairable": False,
                    }
                ],
                "repair_instructions": ["repair"],
            }
        ),
        json.dumps(
            {
                "verdict": "LOCAL_REPAIR",
                "issues": [
                    {
                        "issue_type": "style",
                        "severity": "error",
                        "description": "hint without quote",
                        "block_hint": "block.wrong",
                        "repairable": True,
                    }
                ],
                "repair_instructions": ["repair"],
            }
        ),
    ),
)
def test_editor_review_rejects_unresolved_or_spoofed_issue_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    harness = _harness(tmp_path, payload, monkeypatch)
    with pytest.raises(EditorialReviewError, match="valid report"):
        asyncio.run(harness.service.review(harness.review_input, _request("bad-location")))


def test_editor_service_read_repair_and_lineage_failures_are_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreadable = _harness(tmp_path / "unreadable", _pass(), monkeypatch)

    def fail_read(_artifact: ArtifactRef) -> bytes:
        raise OSError("unreadable")

    monkeypatch.setattr(unreadable.artifacts, "read_verified", fail_read)
    with pytest.raises(EditorialReviewError, match="could not read"):
        asyncio.run(unreadable.service.review(unreadable.review_input, _request("unreadable")))

    blank = _harness(tmp_path / "blank", _pass(), monkeypatch)
    monkeypatch.setattr(blank.artifacts, "read_verified", lambda _artifact: b" \n")
    with pytest.raises(EditorialReviewError, match="blank"):
        asyncio.run(blank.service.review(blank.review_input, _request("blank")))

    quote = "石门向里退开"
    harness = _harness(tmp_path / "repair", _local_review(quote), monkeypatch)
    report = asyncio.run(harness.service.review(harness.review_input, _request("repair-report")))
    original = harness.artifacts.read_verified(harness.review_input.draft.text_artifact).decode()

    for request_suffix, updates, message in (
        ("draft", {"draft_id": ArtifactId("sha256:" + "1" * 64)}, "another Draft"),
        ("task", {"task_contract_id": StableId("writing-task.other")}, "WritingTaskContract"),
        ("context", {"context_id": StableId("context.other")}, "Context snapshot"),
        ("commit", {"base_commit": CommitId("sha256:" + "2" * 64)}, "base commit"),
    ):
        with pytest.raises(EditorialRepairError, match=message):
            asyncio.run(
                harness.service.repair(
                    harness.review_input,
                    report.model_copy(update=updates),
                    _request(f"wrong-{request_suffix}"),
                )
            )

    harness.endpoint.response_text = "not valid EditorRepairPayload JSON"
    with pytest.raises(EditorialRepairError, match="failed without a candidate"):
        asyncio.run(
            harness.service.repair(harness.review_input, report, _request("invalid-output"))
        )

    harness.endpoint.response_text = EditorRepairPayload(repaired_text=original).model_dump_json()
    with pytest.raises(EditorialRepairError, match="no text change"):
        asyncio.run(harness.service.repair(harness.review_input, report, _request("unchanged")))

    repaired_text = original.replace(quote, "石门向外退开", 1)
    harness.endpoint.response_text = EditorRepairPayload(
        repaired_text=repaired_text
    ).model_dump_json()

    def fail_write(*_args: object, **_kwargs: object) -> ArtifactRef:
        raise OSError("write failed")

    original_put = harness.artifacts.put
    monkeypatch.setattr(harness.artifacts, "put", fail_write)
    with pytest.raises(EditorialRepairError, match="artifact write failed"):
        asyncio.run(harness.service.repair(harness.review_input, report, _request("write-failed")))
    monkeypatch.setattr(harness.artifacts, "put", original_put)

    def reject_lineage(**_kwargs: object) -> RepairedDraft:
        raise ValueError("invalid lineage")

    monkeypatch.setattr(editorial_service_module, "RepairedDraft", reject_lineage)
    with pytest.raises(EditorialRepairError, match="lineage is invalid"):
        asyncio.run(harness.service.repair(harness.review_input, report, _request("bad-lineage")))


def test_changed_spans_merges_adjacent_differences(monkeypatch: pytest.MonkeyPatch) -> None:
    class AdjacentMatcher:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_opcodes(self) -> tuple[tuple[str, int, int, int, int], ...]:
            return (
                ("equal", 0, 1, 0, 1),
                ("replace", 1, 2, 1, 2),
                ("replace", 2, 3, 2, 3),
                ("equal", 3, 4, 3, 4),
            )

    monkeypatch.setattr(difflib, "SequenceMatcher", AdjacentMatcher)
    spans = editorial_service_module._changed_spans(
        ArtifactId("sha256:" + "3" * 64),
        "abcd",
        "axyd",
    )
    assert len(spans) == 1
    assert (spans[0].start, spans[0].end) == (1, 3)
