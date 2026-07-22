from __future__ import annotations

import asyncio

import pytest

from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ModelValidationDraft,
    ModelValidationFindingDraft,
    ModelValidationSeverity,
    ValidationStatus,
)
from novel_agent.domain.ids import ArtifactId, ProjectId, RunId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.model_validation import (
    ModelAssistedValidator,
    ModelValidationContractError,
)
from novel_agent.services.overlay import WorldOverlay, build_candidate_bundle
from tests.factories import make_manifest
from tests.unit.test_model_curation import _request
from tests.unit.test_stage1_write_side import _changes, _roots


def _gateway(draft: ModelValidationDraft) -> tuple[ModelGateway, FakeModelEndpoint]:
    endpoint = FakeModelEndpoint(draft.model_dump_json())
    from novel_agent.domain.model_calls import ModelRole

    return (
        ModelGateway(
            (
                RegisteredModelEndpoint(
                    role=ModelRole.BATCH_TEST,
                    endpoint_name="batch-validator",
                    model_name="fake-validator",
                    adapter=endpoint,
                ),
            )
        ),
        endpoint,
    )


def _candidate() -> tuple[
    WorldRootDocument,
    TextRootDocument,
    WorldRootDocument,
    CandidateChangeBundle,
]:
    world, future = _roots()
    changes = _changes(world)
    proposed = WorldOverlay().apply(world, changes)
    candidate = build_candidate_bundle(
        project_id=ProjectId("project.synthetic"),
        run_id=RunId("run.model-validation"),
        current_manifest=make_manifest(),
        changes=changes,
        proposed_world=proposed,
    )
    return world, future, proposed, candidate


def _finding(
    *, severity: ModelValidationSeverity = ModelValidationSeverity.WARNING
) -> ModelValidationFindingDraft:
    _, _, _, candidate = _candidate()
    evidence = candidate.observed_changes.operations[0].evidence_refs[0]
    return ModelValidationFindingDraft(
        code="SEMANTIC_RISK",
        severity=severity,
        message="candidate may conflict with an implicit narrative constraint",
        evidence_refs=(evidence,),
    )


def test_model_validator_adds_audited_warning_without_future_context() -> None:
    world, future, proposed, candidate = _candidate()
    gateway, endpoint = _gateway(ModelValidationDraft(findings=(_finding(),)))
    report, call = asyncio.run(
        ModelAssistedValidator(gateway).validate(
            candidate,
            world,
            proposed,
            future,
            _request(),
            canonical_commit=world.source_commit,
        )
    )
    assert report.status is ValidationStatus.NEEDS_REVIEW
    assert report.findings[0].code == "MODEL_SEMANTIC_RISK"
    assert call is not None and call.model == "fake-validator"
    assert "model:fake-validator@fake-v1" in report.validation_profile
    prompt = endpoint.requests[0].prompt
    assert "进入北塔" in prompt
    assert "重申旧誓言" not in prompt and "受伤仍未痊愈" not in prompt


def test_model_validator_error_blocks_and_empty_draft_passes() -> None:
    world, future, proposed, candidate = _candidate()
    error_gateway, _ = _gateway(
        ModelValidationDraft(findings=(_finding(severity=ModelValidationSeverity.ERROR),))
    )
    blocked, _ = asyncio.run(
        ModelAssistedValidator(error_gateway).validate(
            candidate,
            world,
            proposed,
            future,
            _request(),
            canonical_commit=world.source_commit,
        )
    )
    assert blocked.status is ValidationStatus.FAILED

    passed, _ = asyncio.run(
        ModelAssistedValidator(_gateway(ModelValidationDraft())[0]).validate(
            candidate,
            world,
            proposed,
            future,
            _request(),
            canonical_commit=world.source_commit,
        )
    )
    assert passed.status is ValidationStatus.PASSED


def test_model_validator_never_calls_model_after_deterministic_failure() -> None:
    world, future, proposed, candidate = _candidate()
    corrupted = proposed.model_copy(update={"root_hash": ArtifactId("sha256:" + "f" * 64)})
    gateway, endpoint = _gateway(ModelValidationDraft())
    report, call = asyncio.run(
        ModelAssistedValidator(gateway).validate(
            candidate,
            world,
            corrupted,
            future,
            _request(),
            canonical_commit=world.source_commit,
        )
    )
    assert report.status is ValidationStatus.FAILED
    assert call is None and endpoint.requests == []


def test_model_validator_rejects_external_altered_and_duplicate_evidence_findings() -> None:
    world, future, proposed, candidate = _candidate()
    candidate_evidence = candidate.observed_changes.operations[0].evidence_refs[0]
    outside = future.chapters[1].scenes[0].blocks[0]
    from tests.fixtures.stage1_synthetic import make_synthetic_bundle

    outside_evidence = make_synthetic_bundle().replay_manifests[0].gold_changes[1].evidence_refs[0]
    assert outside_evidence.span is not None and outside.block_id == outside_evidence.span.block_id
    outside_finding = _finding().model_copy(update={"evidence_refs": (outside_evidence,)})
    with pytest.raises(ModelValidationContractError, match="outside"):
        asyncio.run(
            ModelAssistedValidator(
                _gateway(ModelValidationDraft(findings=(outside_finding,)))[0]
            ).validate(
                candidate,
                world,
                proposed,
                future,
                _request(),
                canonical_commit=world.source_commit,
            )
        )

    altered = outside_evidence.model_copy(update={"evidence_id": candidate_evidence.evidence_id})
    altered_finding = _finding().model_copy(update={"evidence_refs": (altered,)})
    with pytest.raises(ModelValidationContractError, match="altered"):
        asyncio.run(
            ModelAssistedValidator(
                _gateway(ModelValidationDraft(findings=(altered_finding,)))[0]
            ).validate(
                candidate,
                world,
                proposed,
                future,
                _request(),
                canonical_commit=world.source_commit,
            )
        )

    finding = _finding()
    duplicate = ModelValidationDraft(findings=(finding, finding))
    with pytest.raises(ModelValidationContractError, match="duplicate"):
        asyncio.run(
            ModelAssistedValidator(_gateway(duplicate)[0]).validate(
                candidate,
                world,
                proposed,
                future,
                _request(),
                canonical_commit=world.source_commit,
            )
        )
