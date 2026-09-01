"""Contract checks for the U6-C immutable fault evidence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId
from novel_agent.domain.u6c_fault_matrix import (
    U6CFaultCase,
    U6CFaultCaseResult,
    U6CFaultCaseStatus,
    U6CFaultMatrixReport,
)


def _ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "a" * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )


def _case(
    case: U6CFaultCase,
    *,
    status: U6CFaultCaseStatus = U6CFaultCaseStatus.PASS,
) -> U6CFaultCaseResult:
    return U6CFaultCaseResult(
        case=case,
        run_id=RunId(f"run.u6c.{case.value}"),
        injection_point="test boundary",
        expected_action="test action",
        observed_action="test action observed",
        status=status,
        safe_checkpoint_id=StableId(f"checkpoint.{case.value}"),
        evidence_artifact_refs=(_ref(),),
        forbidden_results=("test failure",) if status is not U6CFaultCaseStatus.PASS else (),
    )


def _report(
    *,
    status: U6CFaultCaseStatus = U6CFaultCaseStatus.PASS,
    cases: tuple[U6CFaultCaseResult, ...] | None = None,
    duplicate_effect_count: int = 0,
) -> U6CFaultMatrixReport:
    values = cases or tuple(_case(case) for case in U6CFaultCase)
    return U6CFaultMatrixReport(
        experiment_id="u6c.unit",
        database_descriptor="unit",
        status=status,
        cases=values,
        total_provider_call_count=0,
        total_effect_count=0,
        total_commit_count=0,
        projection_rebuild_verified=True,
        duplicate_effect_count=duplicate_effect_count,
        forbidden_result_count=sum(len(item.forbidden_results) for item in values),
    )


def test_u6c_report_requires_each_of_the_eighteen_cases_once() -> None:
    report = _report()
    assert len(report.cases) == 18
    assert {item.case for item in report.cases} == set(U6CFaultCase)

    with pytest.raises(ValidationError, match="each fault case exactly once"):
        _report(cases=(*report.cases[:-1], report.cases[0]))


def test_u6c_pass_cannot_hide_forbidden_results_or_duplicate_effects() -> None:
    failed = tuple(
        _case(case, status=U6CFaultCaseStatus.REVIEW_REQUIRED)
        if case is U6CFaultCase.PROJECTION_FAILURE
        else _case(case)
        for case in U6CFaultCase
    )
    review = _report(status=U6CFaultCaseStatus.REVIEW_REQUIRED, cases=failed)
    assert review.status is U6CFaultCaseStatus.REVIEW_REQUIRED

    with pytest.raises(ValidationError, match="U6-C PASS contradicts"):
        _report(duplicate_effect_count=1)
