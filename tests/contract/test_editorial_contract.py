from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from novel_agent.domain.editorial import (
    CuratorObservation,
    EditorialIssueDraft,
    EditorialIssueType,
    EditorialSeverity,
    EditorialVerdict,
    EditorReviewPayload,
    ReconciliationClass,
    ReconciliationResult,
)
from novel_agent.domain.ids import ArtifactId, StableId


def test_editor_public_literals_and_strict_output_contract_are_frozen() -> None:
    assert tuple(item.value for item in EditorialVerdict) == (
        "PASS",
        "LOCAL_REPAIR",
        "MAJOR_REWRITE",
    )
    assert tuple(item.value for item in ReconciliationClass) == (
        "MATCHED",
        "DECLARED_ONLY",
        "OBSERVED_ONLY",
        "MISMATCHED",
    )
    payload = EditorReviewPayload(verdict=EditorialVerdict.PASS)
    marked_payload = EditorReviewPayload(
        verdict=EditorialVerdict.PASS,
        unresolved_needs=("missing but related context",),
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EditorReviewPayload.model_validate_json(
            json.dumps({"verdict": "PASS", "canonical_id": "forbidden"})
        )
    with pytest.raises(ValidationError, match="blocking"):
        EditorReviewPayload(
            verdict=EditorialVerdict.PASS,
            issues=(
                EditorialIssueDraft(
                    issue_type=EditorialIssueType.CONSTRAINT_VIOLATION,
                    severity=EditorialSeverity.ERROR,
                    description="blocking issue",
                    repairable=True,
                ),
            ),
        )
    assert payload.verdict is EditorialVerdict.PASS
    assert marked_payload.unresolved_needs == ("missing but related context",)


def test_local_repair_treats_nonstructural_blocking_issues_as_repairable() -> None:
    payload = EditorReviewPayload.model_validate_json(
        json.dumps(
            {
                "verdict": "LOCAL_REPAIR",
                "issues": [
                    {
                        "issue_type": "continuity",
                        "severity": "critical",
                        "description": "The draft contradicts the previous chapter ending.",
                        "evidence_quote": "he instantly understood the whole meridian map",
                        "structural": False,
                    }
                ],
                "repair_instructions": [
                    "Rewrite the opening so the first reading remains first-time."
                ],
                "preserve_requirements": ["Keep third-person limited POV."],
            }
        )
    )
    assert payload.verdict is EditorialVerdict.LOCAL_REPAIR
    assert payload.issues[0].repairable is True
    assert payload.issues[0].structural is False


def test_empty_reconciliation_is_bound_to_exact_current_draft() -> None:
    draft_id = ArtifactId("sha256:" + "a" * 64)
    result = ReconciliationResult(
        result_id=StableId("reconciliation.empty"),
        draft_id=draft_id,
        curator_observation=CuratorObservation(draft_id=draft_id),
        comparisons=(),
    )
    assert result.matched == ()
    assert result.declared_only == ()
    assert result.observed_only == ()
    assert result.mismatched == ()

    with pytest.raises(ValidationError, match="another Draft"):
        ReconciliationResult(
            result_id=StableId("reconciliation.wrong"),
            draft_id=ArtifactId("sha256:" + "b" * 64),
            curator_observation=CuratorObservation(draft_id=draft_id),
            comparisons=(),
        )
