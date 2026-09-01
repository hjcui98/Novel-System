from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.model_calls import ModelRole
from novel_agent.domain.recovery_reasoning import (
    RECOVERY_PROPOSAL_MEDIA_TYPE,
    RecoveryActionCandidate,
    RecoveryActionKind,
    RecoveryProposal,
    RecoveryReasonerAdmission,
    RecoveryReasonerBudget,
    RecoveryReasonerRequest,
)
from novel_agent.domain.runtime import FailureClass
from novel_agent.prompts.registry import PromptRegistry, PromptTemplate, content_hash
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.recovery_reasoner import (
    RecoveryReasonerContextExceeded,
    RecoveryReasonerRejected,
    RecoveryReasonerService,
)
from novel_agent.skills.registry import SkillRegistry, SkillTemplate

SCHEMA = SchemaVersion("1.0.0")
BASIS = CommitId("sha256:" + "b" * 64)
BOUNDARY = ArtifactId("sha256:" + "c" * 64)


def _put(repository: ArtifactRepository, value: object, media_type: str = "application/json"):
    return repository.put(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        media_type,
        SCHEMA,
    )


def _fixture(
    tmp_path: Path,
    response: dict[str, object],
) -> tuple[RecoveryReasonerService, RecoveryReasonerRequest, FakeModelEndpoint, ArtifactRepository]:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    prompt_path = tmp_path / "prompt.md"
    skill_path = tmp_path / "skill.md"
    prompt_path.write_text("select one safe action", encoding="utf-8")
    skill_path.write_text("never execute the selected action", encoding="utf-8")
    prompt_hash = content_hash(prompt_path.read_bytes())
    skill_hash = content_hash(skill_path.read_bytes())
    prompts = PromptRegistry(
        (PromptTemplate(StableId("prompt.recovery.v1"), SCHEMA, prompt_path, prompt_hash),)
    )
    skills = SkillRegistry(
        (SkillTemplate(StableId("skill.recovery.v1"), SCHEMA, skill_path, skill_hash),)
    )
    fake = FakeModelEndpoint(json.dumps(response))
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="recovery-test",
                model_name="fake",
                adapter=fake,
                revision="fake-v1",
                output_limit=4096,
            ),
        )
    )
    incident = _put(repository, {"failure": "canon extraction gap"})
    receipt = _put(repository, {"route": "ambiguous"})
    state = _put(repository, {"basis": BASIS.root})
    first_proposal = _put(repository, {"operation": "graph"})
    second_proposal = _put(repository, {"operation": "ordinary"})
    first_validation = _put(repository, {"disposition": "pass", "candidate": "graph"})
    second_validation = _put(repository, {"disposition": "pass", "candidate": "ordinary"})
    candidates = (
        RecoveryActionCandidate(
            action_id=StableId("action.graph"),
            action_kind=RecoveryActionKind.GRAPH_CURATOR,
            proposal_ref=first_proposal,
            validation_ref=first_validation,
            basis_commit=BASIS,
            safety_boundary_id=BOUNDARY,
        ),
        RecoveryActionCandidate(
            action_id=StableId("action.ordinary"),
            action_kind=RecoveryActionKind.ORDINARY_CURATOR,
            proposal_ref=second_proposal,
            validation_ref=second_validation,
            basis_commit=BASIS,
            safety_boundary_id=BOUNDARY,
        ),
    )
    request = RecoveryReasonerRequest(
        request_id=StableId("recovery.request.1"),
        model_request_id=StableId("model.recovery.request.1"),
        project_id=ProjectId("project.recovery.test"),
        run_id=RunId("run.recovery.test"),
        task_id=TaskId("task.recovery.test"),
        incident_ref=incident,
        failure_class=FailureClass.CANON_EXTRACTION_GAP,
        basis_commit=BASIS,
        safety_boundary_id=BOUNDARY,
        receipt_refs=(receipt,),
        state_refs=(state,),
        candidates=candidates,
        allowed_action_kinds=(
            RecoveryActionKind.GRAPH_CURATOR,
            RecoveryActionKind.ORDINARY_CURATOR,
        ),
        prompt_contract_hash=prompt_hash,
        skill_contract_hash=skill_hash,
        admission=RecoveryReasonerAdmission(evidence_refs=(incident, receipt, state)),
    )
    service = RecoveryReasonerService(
        gateway=gateway,
        artifacts=repository,
        prompts=prompts,
        skills=skills,
        prompt_id=StableId("prompt.recovery.v1"),
        prompt_version=SCHEMA,
        skill_id=StableId("skill.recovery.v1"),
        skill_version=SCHEMA,
    )
    return service, request, fake, repository


def test_reasoner_persists_proposal_without_execution_authority(tmp_path: Path) -> None:
    service, request, fake, repository = _fixture(
        tmp_path,
        {
            "selected_action_id": "action.graph",
            "rejected_action_ids": ["action.ordinary"],
            "rationale": "The incident evidence supports the graph candidate.",
        },
    )

    result = asyncio.run(service.propose(request))

    assert result.proposal.selected_action_id == StableId("action.graph")
    assert result.proposal.proposal_only is True
    assert result.proposal.may_mutate_canon is False
    assert result.proposal.may_mutate_active_skill is False
    assert result.proposal.may_execute_tools is False
    assert result.proposal_ref.media_type == RECOVERY_PROPOSAL_MEDIA_TYPE
    assert json.loads(repository.read_verified(result.proposal_ref))["selected_action_id"] == (
        "action.graph"
    )
    assert len(fake.requests) == 1
    assert fake.requests[0].model_role is ModelRole.BATCH_TEST


def test_reasoner_rejects_model_selection_outside_validated_set(tmp_path: Path) -> None:
    service, request, fake, _ = _fixture(
        tmp_path,
        {
            "selected_action_id": "action.unknown",
            "rejected_action_ids": ["action.graph", "action.ordinary"],
            "rationale": "invented",
        },
    )

    with pytest.raises(RecoveryReasonerRejected, match="outside the validated"):
        asyncio.run(service.propose(request))

    assert len(fake.requests) == 1


def test_reasoner_rejects_incomplete_rejected_action_partition(tmp_path: Path) -> None:
    service, request, fake, _ = _fixture(
        tmp_path,
        {
            "selected_action_id": "action.graph",
            "rejected_action_ids": [],
            "rationale": "incomplete",
        },
    )
    with pytest.raises(RecoveryReasonerRejected, match="explicitly reject every"):
        asyncio.run(service.propose(request))
    assert len(fake.requests) == 1


@pytest.mark.parametrize("field", ("prompt_contract_hash", "skill_contract_hash"))
def test_reasoner_rejects_frozen_prompt_or_skill_drift(tmp_path: Path, field: str) -> None:
    service, request, fake, _ = _fixture(
        tmp_path,
        {
            "selected_action_id": "action.graph",
            "rejected_action_ids": ["action.ordinary"],
            "rationale": "unused",
        },
    )
    payload = {name: getattr(request, name) for name in RecoveryReasonerRequest.model_fields}
    payload[field] = ArtifactId("sha256:" + "f" * 64)
    with pytest.raises(RecoveryReasonerRejected, match="hash does not match"):
        asyncio.run(service.propose(RecoveryReasonerRequest(**payload)))
    assert fake.requests == []


def test_reasoner_rejects_unique_deterministic_failure_before_model(tmp_path: Path) -> None:
    _, request, fake, _ = _fixture(
        tmp_path,
        {
            "selected_action_id": "action.graph",
            "rejected_action_ids": ["action.ordinary"],
            "rationale": "unused",
        },
    )

    payload = {name: getattr(request, name) for name in RecoveryReasonerRequest.model_fields}
    payload["failure_class"] = FailureClass.PROVIDER_TRANSIENT
    with pytest.raises(ValidationError, match="one deterministic safe owner"):
        RecoveryReasonerRequest.model_validate(payload)

    assert fake.requests == []


def test_reasoner_cannot_be_enabled_without_held_out_superiority(tmp_path: Path) -> None:
    _, request, fake, _ = _fixture(
        tmp_path,
        {
            "selected_action_id": "action.graph",
            "rejected_action_ids": ["action.ordinary"],
            "rationale": "unused",
        },
    )
    payload = {
        name: getattr(request.admission, name) for name in RecoveryReasonerAdmission.model_fields
    }
    payload["held_out_beats_deterministic_baseline"] = False

    with pytest.raises(ValidationError, match="Input should be True"):
        RecoveryReasonerAdmission(**payload)

    assert fake.requests == []


def test_reasoner_rejects_candidate_basis_drift() -> None:
    candidate = RecoveryActionCandidate(
        action_id=StableId("action.drift"),
        action_kind=RecoveryActionKind.GRAPH_CURATOR,
        proposal_ref={
            "artifact_id": "sha256:" + "1" * 64,
            "media_type": "application/json",
            "byte_length": 1,
            "schema_version": "1.0.0",
        },
        validation_ref={
            "artifact_id": "sha256:" + "2" * 64,
            "media_type": "application/json",
            "byte_length": 1,
            "schema_version": "1.0.0",
        },
        basis_commit=CommitId("sha256:" + "d" * 64),
        safety_boundary_id=BOUNDARY,
    )
    other = candidate.model_copy(
        update={
            "action_id": StableId("action.2"),
            "proposal_ref": candidate.proposal_ref.model_copy(
                update={"artifact_id": ArtifactId("sha256:" + "5" * 64)}
            ),
        }
    )
    with pytest.raises(ValidationError, match="basis differs"):
        RecoveryReasonerRequest(
            request_id=StableId("recovery.request.drift"),
            model_request_id=StableId("model.recovery.request.drift"),
            project_id=ProjectId("project.recovery.test"),
            run_id=RunId("run.recovery.test"),
            task_id=TaskId("task.recovery.test"),
            incident_ref=candidate.proposal_ref,
            failure_class=FailureClass.CANON_EXTRACTION_GAP,
            basis_commit=BASIS,
            safety_boundary_id=BOUNDARY,
            receipt_refs=(candidate.proposal_ref,),
            state_refs=(candidate.proposal_ref,),
            candidates=(candidate, other),
            allowed_action_kinds=(
                RecoveryActionKind.GRAPH_CURATOR,
                RecoveryActionKind.ORDINARY_CURATOR,
            ),
            prompt_contract_hash=ArtifactId("sha256:" + "3" * 64),
            skill_contract_hash=ArtifactId("sha256:" + "4" * 64),
            admission=RecoveryReasonerAdmission(
                evidence_refs=(
                    candidate.proposal_ref,
                    candidate.validation_ref,
                    other.proposal_ref,
                )
            ),
        )


def test_reasoner_enforces_context_budget_before_provider_call(tmp_path: Path) -> None:
    service, request, fake, _ = _fixture(
        tmp_path,
        {
            "selected_action_id": "action.graph",
            "rejected_action_ids": ["action.ordinary"],
            "rationale": "unused",
        },
    )
    request = request.model_copy(update={"budget": RecoveryReasonerBudget(max_context_bytes=1024)})

    with pytest.raises(RecoveryReasonerContextExceeded):
        asyncio.run(service.propose(request))

    assert fake.requests == []


def test_recovery_admission_and_budget_require_distinct_consistent_values(tmp_path: Path) -> None:
    service, request, fake, repository = _fixture(
        tmp_path,
        {
            "selected_action_id": "action.graph",
            "rejected_action_ids": ["action.ordinary"],
            "rationale": "unused",
        },
    )
    duplicate_admission = request.admission.model_copy(
        update={"evidence_refs": (request.incident_ref,) * 3}
    )
    with pytest.raises(ValidationError, match="evidence references must be unique"):
        RecoveryReasonerAdmission(**duplicate_admission.model_dump(mode="python"))
    with pytest.raises(ValidationError, match="thinking flag and thinking budget"):
        RecoveryReasonerBudget(enable_thinking=True, thinking_token_budget=0)

    raw = repository.put(b"\xff", "text/plain", SCHEMA)
    raw_request = request.model_copy(
        update={
            "admission": request.admission.model_copy(
                update={"evidence_refs": (raw, request.receipt_refs[0], request.state_refs[0])}
            )
        }
    )
    result = asyncio.run(service.propose(raw_request))
    assert result.proposal.proposal_only is True
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("allowed_action_kinds", "action allowlist must be unique"),
        ("candidates", "action identities must be unique"),
    ),
)
def test_reasoner_request_rejects_duplicate_surface_values(
    tmp_path: Path, field: str, message: str
) -> None:
    _, request, _, _ = _fixture(
        tmp_path,
        {
            "selected_action_id": "action.graph",
            "rejected_action_ids": ["action.ordinary"],
            "rationale": "unused",
        },
    )
    if field == "allowed_action_kinds":
        value = (RecoveryActionKind.GRAPH_CURATOR, RecoveryActionKind.GRAPH_CURATOR)
    else:
        first = request.candidates[0]
        value = (first, first)
    payload = {name: getattr(request, name) for name in RecoveryReasonerRequest.model_fields}
    payload[field] = value
    with pytest.raises(ValidationError, match=message):
        RecoveryReasonerRequest(**payload)


def test_reasoner_request_rejects_same_payload_boundary_and_allowlist_drift(tmp_path: Path) -> None:
    _, request, _, _ = _fixture(
        tmp_path,
        {
            "selected_action_id": "action.graph",
            "rejected_action_ids": ["action.ordinary"],
            "rationale": "unused",
        },
    )
    same_payload = request.candidates[0].model_copy(
        update={"action_id": StableId("action.same-payload")}
    )
    with pytest.raises(ValidationError, match="distinct action payloads"):
        RecoveryReasonerRequest(
            **{
                **{name: getattr(request, name) for name in RecoveryReasonerRequest.model_fields},
                "candidates": (request.candidates[0], same_payload),
            }
        )

    drift_boundary = request.candidates[1].model_copy(
        update={"safety_boundary_id": ArtifactId("sha256:" + "e" * 64)}
    )
    with pytest.raises(ValidationError, match="safety boundary differs"):
        RecoveryReasonerRequest(
            **{
                **{name: getattr(request, name) for name in RecoveryReasonerRequest.model_fields},
                "candidates": (request.candidates[0], drift_boundary),
            }
        )

    outside_allowlist = request.candidates[1].model_copy(
        update={"action_kind": RecoveryActionKind.REVIEW_REQUIRED}
    )
    with pytest.raises(ValidationError, match="outside the action allowlist"):
        RecoveryReasonerRequest(
            **{
                **{name: getattr(request, name) for name in RecoveryReasonerRequest.model_fields},
                "candidates": (request.candidates[0], outside_allowlist),
            }
        )


def test_recovery_proposal_partition_is_total_and_disjoint(tmp_path: Path) -> None:
    service, request, _, _ = _fixture(
        tmp_path,
        {
            "selected_action_id": "action.graph",
            "rejected_action_ids": ["action.ordinary"],
            "rationale": "select graph",
        },
    )
    proposal = asyncio.run(service.propose(request)).proposal
    for update, message in (
        (
            {"selected_action_id": StableId("action.unknown")},
            "selected recovery action was not considered",
        ),
        (
            {"rejected_action_ids": (proposal.selected_action_id,)},
            "selected recovery action cannot also be rejected",
        ),
        (
            {"rejected_action_ids": ()},
            "proposal must account for every unselected recovery action",
        ),
        (
            {"considered_action_ids": (proposal.selected_action_id,) * 2},
            "considered recovery action identities must be unique",
        ),
        (
            {
                "rejected_action_ids": (
                    proposal.rejected_action_ids[0],
                    proposal.rejected_action_ids[0],
                )
            },
            "rejected recovery action identities must be unique",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            RecoveryProposal(**{**proposal.model_dump(mode="python"), **update})
