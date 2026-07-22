from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.agents import (
    AgentRegistry,
    GuardianInvocationError,
    GuardianRiskReviewAgent,
    StructuredAgentRunner,
)
from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.changes import (
    ChangeOperation,
    ChangeOperationType,
    ObservedChangeSet,
    ValidationFinding,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentSpec,
    AgentType,
    ContractRef,
    GuardianDecision,
    GuardianDecisionDraft,
    GuardianOutcome,
    PatchRiskAssessment,
    PatchRiskLevel,
    PromptContractRef,
    SkillContractRef,
    ToolPolicy,
)
from novel_agent.prompts import PromptRegistry, PromptTemplate
from novel_agent.prompts.registry import content_hash
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.guardian import PatchRiskClassifier
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.skills import SkillRegistry, SkillTemplate

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "a" * 64)
BASE = CommitId("sha256:" + "b" * 64)
ROOT = Path(__file__).parents[2]


def artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=HASH,
        media_type="application/json",
        byte_length=1,
        schema_version=VERSION,
    )


def changes() -> ObservedChangeSet:
    return ObservedChangeSet(
        change_set_id=StableId("changes.guardian-agent"),
        base_commit=BASE,
        source_artifact=artifact(),
        operations=(
            ChangeOperation(
                operation_id=StableId("operation.state-overwrite"),
                root_kind=RootKind.WORLD,
                operation=ChangeOperationType.REPLACE,
                target_id=StableId("state.hero.injury"),
                payload={
                    "record_type": "state",
                    "record": {"predicate": "injury", "value": "healed"},
                },
            ),
        ),
    )


def validation(status: ValidationStatus = ValidationStatus.PASSED) -> ValidationReport:
    return ValidationReport(
        report_id=StableId("validation.guardian-agent"),
        bundle_id=StableId("bundle.guardian-agent"),
        status=status,
        findings=(
            (ValidationFinding(code="BLOCKING", severity="error", message="deterministic failure"),)
            if status is ValidationStatus.FAILED
            else ()
        ),
        schema_version=VERSION,
        validated_at=datetime(2026, 7, 21, tzinfo=UTC),
    )


def request() -> ModelRequest:
    return ModelRequest(
        request_id=StableId("request.guardian-agent"),
        run_id=RunId("run.guardian-agent"),
        task_id=TaskId("task.guardian-agent"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace-guardian-agent",
        prompt="caller cannot override guardian policy",
    )


def prompt_ref(path: Path, identity: str) -> PromptContractRef:
    digest = content_hash(path.read_bytes())
    return PromptContractRef(
        contract_id=StableId(identity),
        version=VERSION,
        content_hash=digest,
        render_fingerprint=digest,
    )


def harness(
    tmp_path: Path,
    draft: GuardianDecisionDraft,
) -> tuple[GuardianRiskReviewAgent, FakeModelEndpoint, ArtifactRepository]:
    system_path = ROOT / "src/novel_agent/prompts/system_policy_v1.md"
    task_path = ROOT / "src/novel_agent/prompts/guardian_risk_review_v1.md"
    skill_path = ROOT / "src/novel_agent/skills/memory_risk_review_v1.md"
    system = prompt_ref(system_path, "prompt.system-policy")
    task = prompt_ref(task_path, "prompt.guardian-risk-review")
    skill = SkillContractRef(
        contract_id=StableId("skill.memory-risk-review"),
        version=VERSION,
        content_hash=content_hash(skill_path.read_bytes()),
    )
    schema = ContractRef(
        contract_id=StableId("schema.guardian-decision-draft"),
        version=VERSION,
        content_hash=HASH,
    )
    spec = AgentSpec(
        agent_id=StableId("agent.memory-guardian.risk-review"),
        agent_type=AgentType.MEMORY_GUARDIAN,
        mode=AgentMode.RISK_REVIEW,
        version=VERSION,
        content_hash=HASH,
        input_schema=schema,
        output_schema=schema,
        system_prompt=system,
        task_prompt=task,
        skills=(skill,),
        tool_policy=ToolPolicy(
            policy_id=StableId("policy.guardian-risk-review"),
            version=VERSION,
            content_hash=HASH,
            allowed_tools=(),
            max_tool_calls=0,
        ),
    )
    endpoint = FakeModelEndpoint(draft.model_dump_json())
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="guardian-test",
                model_name="fake-guardian",
                adapter=endpoint,
            ),
        )
    )
    runner = StructuredAgentRunner(
        gateway,
        AgentRegistry((spec,)),
        PromptRegistry(
            (
                PromptTemplate(system.contract_id, VERSION, system_path, system.content_hash),
                PromptTemplate(task.contract_id, VERSION, task_path, task.content_hash),
            )
        ),
        SkillRegistry((SkillTemplate(skill.contract_id, VERSION, skill_path, skill.content_hash),)),
    )
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    return GuardianRiskReviewAgent(runner, repository), endpoint, repository


def test_guardian_agent_reviews_only_routed_patch_and_persists_audited_output(
    tmp_path: Path,
) -> None:
    draft = GuardianDecisionDraft(
        outcome=GuardianOutcome.APPROVE,
        risk_codes=("MODEL_CONFIRMED",),
        reasons=("evidence and transition are coherent",),
    )
    agent, endpoint, repository = harness(tmp_path, draft)
    candidate = changes()
    report = validation()
    risk = PatchRiskClassifier().assess(candidate, report)

    decision, call = asyncio.run(
        agent.review(
            version=VERSION,
            changes=candidate,
            validation=report,
            risk=risk,
            request=request(),
        )
    )

    assert call.request_id == request().request_id
    assert decision.outcome is GuardianOutcome.APPROVE
    assert set(decision.risk_codes) == {"MODEL_CONFIRMED", "STATE_OVERWRITE"}
    assert decision.receipt.base_commit == BASE
    assert len(decision.receipt.input_artifacts) == 2
    assert len(decision.receipt.output_artifacts) == 1
    assert repository.read_verified(decision.receipt.output_artifacts[0])
    sent = endpoint.requests[0]
    assert sent.agent_id == StableId("agent.memory-guardian.risk-review")
    assert sent.render_fingerprint == decision.receipt.prompt_fingerprint
    assert '<TASK_PAYLOAD trusted="false">' in sent.prompt
    assert "Memory Guardian RISK_REVIEW v1" in sent.prompt


def test_guardian_agent_persists_revised_candidate_separately(tmp_path: Path) -> None:
    draft = GuardianDecisionDraft(
        outcome=GuardianOutcome.REVISE,
        risk_codes=("STATE_OVERWRITE",),
        reasons=("narrow the state change",),
        revised_candidate={"operation": "replace", "value": "recovering"},
    )
    agent, _, repository = harness(tmp_path, draft)
    candidate = changes()
    report = validation()
    decision, _ = asyncio.run(
        agent.review(
            version=VERSION,
            changes=candidate,
            validation=report,
            risk=PatchRiskClassifier().assess(candidate, report),
            request=request(),
        )
    )
    assert decision.revised_candidate is not None
    assert len(decision.receipt.output_artifacts) == 2
    assert repository.read_verified(decision.revised_candidate)


def test_guardian_agent_blocks_invalid_invocation_before_model_call(tmp_path: Path) -> None:
    draft = GuardianDecisionDraft(
        outcome=GuardianOutcome.APPROVE,
        risk_codes=(),
        reasons=("ok",),
    )
    agent, endpoint, _ = harness(tmp_path, draft)
    candidate = changes()
    failed = validation(ValidationStatus.FAILED)
    with pytest.raises(GuardianInvocationError, match="validation failure"):
        asyncio.run(
            agent.review(
                version=VERSION,
                changes=candidate,
                validation=failed,
                risk=PatchRiskClassifier().assess(candidate, failed),
                request=request(),
            )
        )
    report = validation()
    high = PatchRiskClassifier().assess(candidate, report)
    with pytest.raises(GuardianInvocationError, match="does not belong"):
        asyncio.run(
            agent.review(
                version=VERSION,
                changes=candidate,
                validation=report,
                risk=high.model_copy(update={"change_set_id": StableId("changes.other")}),
                request=request(),
            )
        )
    low = PatchRiskAssessment(
        assessment_id=StableId("risk.low"),
        change_set_id=candidate.change_set_id,
        base_commit=BASE,
        level=PatchRiskLevel.LOW,
        risk_codes=(),
        requires_guardian=False,
        requires_human_review=False,
    )
    with pytest.raises(GuardianInvocationError, match="low-risk"):
        asyncio.run(
            agent.review(
                version=VERSION,
                changes=candidate,
                validation=report,
                risk=low,
                request=request(),
            )
        )
    assert endpoint.requests == []


def test_guardian_contracts_reject_revision_and_receipt_contradictions(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="requires a revised"):
        GuardianDecisionDraft(
            outcome=GuardianOutcome.REVISE,
            risk_codes=(),
            reasons=("revise",),
        )
    with pytest.raises(ValidationError, match="only Guardian revise"):
        GuardianDecisionDraft(
            outcome=GuardianOutcome.APPROVE,
            risk_codes=(),
            reasons=("approve",),
            revised_candidate={"unexpected": True},
        )
    agent, _, _ = harness(
        tmp_path,
        GuardianDecisionDraft(
            outcome=GuardianOutcome.APPROVE,
            risk_codes=(),
            reasons=("approve",),
        ),
    )
    candidate = changes()
    report = validation()
    decision, _ = asyncio.run(
        agent.review(
            version=VERSION,
            changes=candidate,
            validation=report,
            risk=PatchRiskClassifier().assess(candidate, report),
            request=request(),
        )
    )
    cases = (
        ({"agent_type": AgentType.PLANNER}, "Memory Guardian"),
        ({"agent_mode": AgentMode.REPLAY}, "RISK_REVIEW"),
        ({"base_commit": None}, "share a base commit"),
    )
    for update, message in cases:
        payload = decision.model_dump()
        payload["receipt"] = decision.receipt.model_copy(update=update).model_dump()
        with pytest.raises(ValidationError, match=message):
            GuardianDecision.model_validate(payload)
    with pytest.raises(ValidationError, match="persisted candidate"):
        GuardianDecision.model_validate(decision.model_dump() | {"outcome": GuardianOutcome.REVISE})
    with pytest.raises(ValidationError, match="only Guardian revise"):
        GuardianDecision.model_validate(decision.model_dump() | {"revised_candidate": artifact()})
