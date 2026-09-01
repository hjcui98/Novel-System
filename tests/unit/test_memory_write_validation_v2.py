"""Stage 2W validation-v2 mapping and fail-closed tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from novel_agent.domain.artifacts import RootKind
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ChangeOperation,
    ChangeOperationType,
    ObservedChangeSet,
    ValidationFinding,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.ids import ArtifactId, ProjectId, RunId, StableId
from novel_agent.domain.memory_write import (
    BlockingScope,
    CandidateMaterialization,
    CandidateProducerKind,
    CandidateRevision,
    CanonicalWriteBasis,
    FindingRetryability,
    RepairScope,
    ValidationDisposition,
    ValidationFindingCategory,
    ValidationFindingV2,
    ValidationSeverity,
)
from novel_agent.domain.stage2 import ContractRef
from novel_agent.domain.text import SourceBoundEvidenceRequirement, TextSpanRef
from novel_agent.services.memory_write_validation import (
    ModelFindingProvider,
    Stage2ValidationV2Adapter,
    ValidationV1Adapter,
    ValidationV2AdapterError,
    _category,
    _disposition_from_findings,
    _operation_ids,
)
from tests.factories import make_artifact, make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

NOW = datetime(2026, 7, 23, tzinfo=UTC)
PROJECT = ProjectId("project.validation-v2")
WORLD = make_synthetic_bundle().world_roots[0]
TEXT = make_synthetic_bundle().text_roots[0]
SOURCE_BLOB_ROOT = ArtifactId("sha256:" + "1" * 64)


def _operation(
    *,
    operation: ChangeOperationType = ChangeOperationType.REPLACE,
    record_type: str = "state",
    target: str = "state.synthetic.injury",
    record: dict[str, Any] | None = None,
    evidence: bool = False,
) -> ChangeOperation:
    if record is None:
        record = WORLD.states[0].model_dump(mode="json")
    return ChangeOperation(
        operation_id=StableId(f"operation.validation.{operation.value}.{record_type}"),
        root_kind=RootKind.WORLD,
        operation=operation,
        target_id=StableId(target),
        payload={"record_type": record_type, "record": record},
        evidence_refs=(WORLD.states[0].evidence_refs[0],) if evidence else (),
    )


def _bundle(*operations: ChangeOperation) -> CandidateChangeBundle:
    observed = ObservedChangeSet(
        change_set_id=StableId("changes.validation-v2"),
        base_commit=WORLD.source_commit,
        source_artifact=make_artifact("9"),
        operations=operations,
    )
    return CandidateChangeBundle(
        bundle_id=StableId("bundle.candidate.validation-v2"),
        project_id=PROJECT,
        run_id=RunId("run.validation-v2"),
        base_commit=WORLD.source_commit,
        observed_changes=observed,
        proposed_roots=make_manifest(PROJECT),
    )


def _candidate(*, base: Any = None) -> CandidateRevision:
    return CandidateRevision(
        candidate_id=StableId("candidate.validation-v2"),
        revision_no=1,
        base_commit=base or WORLD.source_commit,
        basis_hash=ArtifactId("sha256:" + "a" * 64),
        candidate_artifact=make_artifact("b"),
        producer_kind=CandidateProducerKind.CURATOR_PROPOSE,
        content_hash=ArtifactId("sha256:" + "c" * 64),
        created_at=NOW,
    )


def _source_text_artifact() -> Any:
    return make_artifact("1").model_copy(
        update={
            # Production RootManifest refs identify the serialized blob,
            # whereas EvidenceRef.root_hash identifies the canonical
            # TextRoot content.  Keep those identities distinct in this
            # fixture so the bridge is exercised.
            "artifact_id": SOURCE_BLOB_ROOT,
            "byte_length": 1,
        }
    )


def _source_requirement() -> SourceBoundEvidenceRequirement:
    chapter = TEXT.chapters[4]
    block = chapter.scenes[0].blocks[0]
    phrase = "旧誓言"
    start = block.text.index(phrase)
    return SourceBoundEvidenceRequirement(
        source_artifact_id=SOURCE_BLOB_ROOT,
        source_chapter_index=chapter.chapter_index,
        source_chapter_id=chapter.chapter_id,
        required_span=TextSpanRef(
            block_id=block.block_id,
            start=start,
            end=start + len(phrase),
        ),
        required_consequence_markers=(phrase,),
    )


def _candidate_with_source_requirement() -> CandidateRevision:
    return _candidate().model_copy(
        update={
            "source_artifacts": (_source_text_artifact(),),
            "source_evidence_requirement": _source_requirement(),
        }
    )


def _operation_with_evidence(evidence: Any) -> ChangeOperation:
    operation = _operation()
    payload = dict(operation.payload)
    record = dict(payload["record"])
    record["evidence_refs"] = [evidence.model_dump(mode="json")]
    payload["record"] = record
    return operation.model_copy(update={"payload": payload, "evidence_refs": (evidence,)})


def _source_bundle(*operations: ChangeOperation) -> CandidateChangeBundle:
    bundle = _bundle(*operations)
    return bundle.model_copy(update={"proposed_roots": _basis().root_manifest})


def _materialization(
    bundle: CandidateChangeBundle | None,
) -> CandidateMaterialization:
    return CandidateMaterialization(
        candidate_id=StableId("candidate.validation-v2"),
        candidate_content_hash=ArtifactId("sha256:" + "c" * 64),
        bundle_artifact=make_artifact("d"),
        proposed_roots_hash=ArtifactId("sha256:" + "e" * 64),
        materialization_receipt=make_artifact("f"),
        materializer_policy_ref=ContractRef(
            contract_id=StableId("policy.materializer"),
            version=make_manifest().schema_version,
            content_hash=ArtifactId("sha256:" + "1" * 64),
        ),
        bundle=bundle,
    )


def _basis(*, typed: bool = False) -> CanonicalWriteBasis:
    manifest = make_manifest(PROJECT)
    manifest = manifest.model_copy(
        update={
            "text_root": manifest.text_root.model_copy(update={"artifact_id": SOURCE_BLOB_ROOT})
        }
    )
    return CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=WORLD.source_commit,
        root_manifest=manifest,
        canonical_world=WORLD if typed else None,
        canonical_text=TEXT if typed else None,
    )


def _report(
    status: ValidationStatus,
    *findings: ValidationFinding,
) -> ValidationReport:
    return ValidationReport(
        report_id=StableId(f"report.validation.{status.value}"),
        bundle_id=StableId("bundle.candidate.validation-v2"),
        status=status,
        findings=findings,
        schema_version=make_manifest().schema_version,
        validation_profile="validation-v1-test",
        validated_at=NOW,
    )


def test_validation_requires_materialized_bundle() -> None:
    with pytest.raises(ValidationV2AdapterError, match="materialized"):
        asyncio.run(
            Stage2ValidationV2Adapter().validate(_candidate(), _materialization(None), _basis())
        )


def test_basis_mismatch_is_non_repairable_without_running_validator() -> None:
    other = ArtifactId("sha256:" + "2" * 64)
    candidate = _candidate(base=type(WORLD.source_commit)(other.root))
    decision = asyncio.run(
        Stage2ValidationV2Adapter().validate(candidate, _materialization(_bundle()), _basis())
    )
    assert decision.disposition is ValidationDisposition.NON_REPAIRABLE
    assert decision.findings[0].code == "BASE_COMMIT_MISMATCH"
    assert decision.findings[0].category is ValidationFindingCategory.BASIS


def test_structural_validation_passes_without_legacy_typed_roots() -> None:
    decision = asyncio.run(
        Stage2ValidationV2Adapter().validate(_candidate(), _materialization(_bundle()), _basis())
    )
    assert decision.disposition is ValidationDisposition.PASS
    assert decision.deterministic_profile == "stage2w-structural-v2"


def test_source_bound_validation_rejects_evidence_from_another_chapter() -> None:
    candidate = _candidate_with_source_requirement()
    bundle = _source_bundle(_operation_with_evidence(WORLD.states[0].evidence_refs[0]))

    decision = asyncio.run(
        Stage2ValidationV2Adapter().validate(
            candidate, _materialization(bundle), _basis(typed=True)
        )
    )

    assert decision.disposition is ValidationDisposition.NON_REPAIRABLE
    assert any(item.code == "SOURCE_BOUND_EVIDENCE_MISSING" for item in decision.findings)


def test_source_bound_validation_accepts_evidence_covering_registered_span() -> None:
    candidate = _candidate_with_source_requirement()
    bundle = _source_bundle(_operation_with_evidence(WORLD.states[1].evidence_refs[0]))

    decision = asyncio.run(
        Stage2ValidationV2Adapter().validate(
            candidate, _materialization(bundle), _basis(typed=True)
        )
    )

    assert decision.disposition is ValidationDisposition.PASS
    assert not any(item.code.startswith("SOURCE_BOUND_EVIDENCE") for item in decision.findings)


def test_source_bound_validation_accepts_bounded_evidence_for_wide_registered_span() -> None:
    requirement = _source_requirement().model_copy(
        update={
            "required_span": _source_requirement().required_span.model_copy(
                update={"start": 0, "end": len(TEXT.chapters[4].scenes[0].blocks[0].text)}
            )
        }
    )
    candidate = _candidate().model_copy(
        update={
            "source_artifacts": (_source_text_artifact(),),
            "source_evidence_requirement": requirement,
        }
    )
    bundle = _source_bundle(_operation_with_evidence(WORLD.states[1].evidence_refs[0]))

    decision = asyncio.run(
        Stage2ValidationV2Adapter().validate(
            candidate, _materialization(bundle), _basis(typed=True)
        )
    )

    assert decision.disposition is ValidationDisposition.PASS


def test_validation_report_id_is_bounded_for_long_materialized_bundle() -> None:
    candidate = _candidate()
    bundle = _bundle().model_copy(update={"bundle_id": StableId("bundle." + "b" * 115)})
    report = Stage2ValidationV2Adapter()._deterministic_report(bundle, _basis(), candidate)

    assert len(report.report_id.root) <= 128
    assert report.report_id.root == "validation.structural." + ("c" * 64)


def test_stage1_validator_bounds_long_bundle_report_id() -> None:
    bundle = _bundle().model_copy(update={"bundle_id": StableId("bundle." + "b" * 115)})
    report = Stage2ValidationV2Adapter()._deterministic.validate(
        bundle,
        WORLD,
        WORLD,
        TEXT,
        canonical_commit=WORLD.source_commit,
    )

    assert report.report_id == StableId("validation.changes.validation-v2")


def test_overlay_failure_becomes_a_non_repairable_schema_finding() -> None:
    invalid = _operation(
        operation=ChangeOperationType.REPLACE,
        target="state.synthetic.missing",
        record={
            **WORLD.states[0].model_dump(mode="json"),
            "state_id": "state.synthetic.missing",
        },
    )
    decision = asyncio.run(
        Stage2ValidationV2Adapter().validate(
            _candidate(), _materialization(_bundle(invalid)), _basis(typed=True)
        )
    )
    assert decision.disposition is ValidationDisposition.NON_REPAIRABLE
    assert decision.findings[0].code == "INVALID_OVERLAY"
    assert decision.findings[0].category is ValidationFindingCategory.SCHEMA


def test_typed_roots_delegate_to_deterministic_validator() -> None:
    class Deterministic:
        def validate(self, *_: object, **__: object) -> ValidationReport:
            return _report(ValidationStatus.PASSED)

    decision = asyncio.run(
        Stage2ValidationV2Adapter(deterministic=Deterministic()).validate(  # type: ignore[arg-type]
            _candidate(), _materialization(_bundle()), _basis(typed=True)
        )
    )
    assert decision.disposition is ValidationDisposition.PASS


def test_new_evidence_is_validated_against_proposed_text_root() -> None:
    bundle = _bundle(_operation(evidence=True))
    proposed_text_ref = bundle.proposed_roots.text_root.model_copy(
        update={"artifact_id": ArtifactId("sha256:" + "7" * 64)}
    )
    bundle = bundle.model_copy(
        update={
            "proposed_roots": bundle.proposed_roots.model_copy(
                update={"text_root": proposed_text_ref}
            )
        }
    )
    loaded: list[object] = []
    validated_text: list[object] = []

    class Deterministic:
        def validate(
            self,
            bundle: object,
            current_world: object,
            proposed_world: object,
            text_root: object,
            **kwargs: object,
        ) -> ValidationReport:
            del bundle, current_world, proposed_world, kwargs
            validated_text.append(text_root)
            return _report(ValidationStatus.PASSED)

    def load_text(ref: object) -> Any:
        loaded.append(ref)
        return TEXT

    decision = asyncio.run(
        Stage2ValidationV2Adapter(
            deterministic=cast(Any, Deterministic()),
            proposed_text_loader=cast(Any, load_text),
        ).validate(
            _candidate(),
            _materialization(bundle),
            _basis(typed=True),
        )
    )

    assert decision.disposition is ValidationDisposition.PASS
    assert loaded == [proposed_text_ref]
    assert validated_text == [TEXT]


@pytest.mark.parametrize("loader_fails", (False, True))
def test_new_evidence_fails_closed_without_readable_proposed_text(
    loader_fails: bool,
) -> None:
    bundle = _bundle(_operation(evidence=True))
    bundle = bundle.model_copy(
        update={
            "proposed_roots": bundle.proposed_roots.model_copy(
                update={
                    "text_root": bundle.proposed_roots.text_root.model_copy(
                        update={"artifact_id": ArtifactId("sha256:" + "8" * 64)}
                    )
                }
            )
        }
    )

    def broken_loader(_: object) -> Any:
        raise OSError("unreadable")

    adapter = Stage2ValidationV2Adapter(
        proposed_text_loader=cast(Any, broken_loader) if loader_fails else None
    )
    decision = asyncio.run(
        adapter.validate(
            _candidate(),
            _materialization(bundle),
            _basis(typed=True),
        )
    )

    assert decision.disposition is ValidationDisposition.NON_REPAIRABLE
    assert decision.findings[0].code == "PROPOSED_TEXT_UNAVAILABLE"
    assert ("cannot be loaded" in decision.findings[0].message) is loader_fails


def _v2_finding(
    *,
    retryability: FindingRetryability,
    scope: BlockingScope = BlockingScope.CANDIDATE,
    human: bool = False,
) -> ValidationFindingV2:
    return ValidationFindingV2(
        finding_id=StableId(f"finding.model.{retryability.value}"),
        code=f"MODEL_{retryability.value.upper()}",
        category=ValidationFindingCategory.UNKNOWN,
        severity=ValidationSeverity.WARNING,
        message="model-added restriction",
        retryability=retryability,
        blocking_scope=scope,
        allowed_repair_scope=RepairScope(),
        requires_human=human,
    )


@pytest.mark.parametrize(
    ("finding", "expected"),
    (
        (
            _v2_finding(retryability=FindingRetryability.NON_REPAIRABLE),
            ValidationDisposition.NON_REPAIRABLE,
        ),
        (
            _v2_finding(retryability=FindingRetryability.REVIEW),
            ValidationDisposition.REVIEW_REQUIRED,
        ),
        (
            _v2_finding(
                retryability=FindingRetryability.REPAIRABLE,
                scope=BlockingScope.OPERATION,
            ),
            ValidationDisposition.PARTIAL_REPAIRABLE,
        ),
        (
            _v2_finding(retryability=FindingRetryability.REPAIRABLE),
            ValidationDisposition.REPAIRABLE,
        ),
    ),
)
def test_model_findings_only_add_restrictions(
    finding: ValidationFindingV2, expected: ValidationDisposition
) -> None:
    async def provider(*_: object) -> tuple[ValidationFindingV2, ...]:
        return (finding,)

    decision = asyncio.run(
        Stage2ValidationV2Adapter(model_findings=cast(ModelFindingProvider, provider)).validate(
            _candidate(), _materialization(_bundle()), _basis()
        )
    )
    assert decision.disposition is expected
    assert decision.model_profile == "model-assisted-v2"


def test_empty_model_finding_set_preserves_deterministic_decision() -> None:
    async def provider(*_: object) -> tuple[ValidationFindingV2, ...]:
        return ()

    decision = asyncio.run(
        Stage2ValidationV2Adapter(model_findings=cast(ModelFindingProvider, provider)).validate(
            _candidate(), _materialization(_bundle()), _basis()
        )
    )
    assert decision.disposition is ValidationDisposition.PASS
    assert decision.model_profile is None


@pytest.mark.parametrize(
    ("code", "severity", "expected_category", "expected_severity"),
    (
        (
            "STATE_IDENTITY_MUTATION",
            "error",
            ValidationFindingCategory.IDENTITY,
            ValidationSeverity.ERROR,
        ),
        (
            "UNKNOWN_EVIDENCE",
            "critical",
            ValidationFindingCategory.EVIDENCE,
            ValidationSeverity.CRITICAL,
        ),
        (
            "UNKNOWN_TRANSITION",
            "warning",
            ValidationFindingCategory.TRANSITION,
            ValidationSeverity.WARNING,
        ),
        (
            "UNKNOWN_TRUTH",
            "other",
            ValidationFindingCategory.TRUTH,
            ValidationSeverity.ERROR,
        ),
        (
            "UNKNOWN_BASE",
            "error",
            ValidationFindingCategory.BASIS,
            ValidationSeverity.ERROR,
        ),
        (
            "UNKNOWN_SCHEMA",
            "error",
            ValidationFindingCategory.SCHEMA,
            ValidationSeverity.ERROR,
        ),
        (
            "UNCLASSIFIED",
            "error",
            ValidationFindingCategory.UNKNOWN,
            ValidationSeverity.ERROR,
        ),
    ),
)
def test_v1_mapping_is_conservative(
    code: str,
    severity: str,
    expected_category: ValidationFindingCategory,
    expected_severity: ValidationSeverity,
) -> None:
    finding = ValidationFinding(code=code, severity=severity, message="finding")
    decision = ValidationV1Adapter.convert(
        _report(ValidationStatus.FAILED, finding),
        candidate=_candidate(),
        materialization=_materialization(_bundle(_operation())),
    )
    mapped = decision.findings[0]
    assert mapped.category is expected_category
    assert mapped.severity is expected_severity


@pytest.mark.parametrize(
    ("code", "operation"),
    (
        (
            "STATE_IDENTITY_MUTATION",
            _operation(),
        ),
        (
            "CREATE_TARGET_EXISTS",
            _operation(operation=ChangeOperationType.CREATE),
        ),
        (
            "REPLACE_TARGET_MISSING",
            _operation(operation=ChangeOperationType.REPLACE),
        ),
        (
            "INVALID_EVIDENCE_REF",
            _operation(evidence=True),
        ),
        (
            "OPERATION_TARGET_MISMATCH",
            _operation(
                record={
                    **WORLD.states[0].model_dump(mode="json"),
                    "state_id": "state.synthetic.other",
                }
            ),
        ),
    ),
)
def test_v1_findings_bind_to_matching_operation(code: str, operation: ChangeOperation) -> None:
    decision = ValidationV1Adapter.convert(
        _report(
            ValidationStatus.FAILED,
            ValidationFinding(code=code, severity="error", message="finding"),
        ),
        candidate=_candidate(),
        materialization=_materialization(_bundle(operation)),
    )
    assert decision.findings[0].operation_ids == (operation.operation_id,)
    if code == "INVALID_EVIDENCE_REF":
        assert decision.findings[0].allowed_repair_scope.field_paths == (
            "evidence_refs",
            "record.evidence_refs",
        )


def test_evidence_mismatch_and_single_unknown_code_bind_conservatively() -> None:
    record = WORLD.states[0].model_dump(mode="json")
    record["evidence_refs"] = []
    operation = _operation(record=record, evidence=True)
    mismatch = ValidationV1Adapter.convert(
        _report(
            ValidationStatus.FAILED,
            ValidationFinding(
                code="RECORD_EVIDENCE_MISMATCH",
                severity="error",
                message="mismatch",
            ),
        ),
        candidate=_candidate(),
        materialization=_materialization(_bundle(operation)),
    )
    unknown = ValidationV1Adapter.convert(
        _report(
            ValidationStatus.NEEDS_REVIEW,
            ValidationFinding(code="OTHER", severity="warning", message="other"),
        ),
        candidate=_candidate(),
        materialization=_materialization(_bundle(operation)),
    )
    assert mismatch.findings[0].operation_ids == (operation.operation_id,)
    assert unknown.findings[0].operation_ids == (operation.operation_id,)


def test_empty_finding_disposition_and_missing_bundle_operation_binding() -> None:
    assert _disposition_from_findings(()) is ValidationDisposition.PASS
    finding = ValidationFinding(code="OTHER", severity="error", message="other")
    assert _operation_ids(_candidate(), finding, _materialization(None)) == ()


def test_target_category_uses_identity_classification() -> None:
    assert _category("OPERATION_TARGET_MISMATCH") is ValidationFindingCategory.IDENTITY


def test_unknown_multi_operation_finding_does_not_guess_operation_binding() -> None:
    first = _operation(target="state.synthetic.one")
    second = _operation(target="state.synthetic.two").model_copy(
        update={"operation_id": StableId("operation.validation.second")}
    )
    finding = ValidationFinding(code="OTHER", severity="error", message="other")
    assert (
        _operation_ids(
            _candidate(),
            finding,
            _materialization(_bundle(first, second)),
        )
        == ()
    )
