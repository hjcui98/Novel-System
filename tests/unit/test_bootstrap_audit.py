"""Source-backed Bootstrap audit binding and completeness tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from novel_agent.domain.changes import ValidationStatus
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, StableId
from novel_agent.domain.memory import FreshnessDecision, FreshnessStatus
from novel_agent.domain.stage2 import (
    AuthorApprovalDecision,
    AuthorApprovalRequest,
    AuthorApprovalStatus,
    GenesisCommitReceipt,
    SourceClass,
)
from novel_agent.services.bootstrap_audit import BootstrapAuditService, utc_now
from novel_agent.services.bootstrap_workflow import (
    BootstrapCrossRootValidator,
    candidate_manifest_hash,
)
from tests.unit.test_stage2_bootstrap import raw, service
from tests.unit.test_stage2_bootstrap_workflow import NOW, candidates

PROJECT = ProjectId("project.bootstrap")


def _basis(tmp_path: Path) -> dict[str, Any]:
    ingestion, _ = service(tmp_path / "ingestion")
    bundle, ingested = ingestion.ingest(
        PROJECT,
        StableId("bootstrap.bundle"),
        (
            raw("source.brief", SourceClass.AUTHOR_INITIAL_BRIEF, b"brief"),
            raw("source.setting", SourceClass.BASELINE_SETTING, b"setting"),
        ),
    )
    built = candidates(tmp_path / "candidates", PROJECT)
    validation = BootstrapCrossRootValidator(lambda: NOW).validate(built)
    manifest_hash = candidate_manifest_hash(built.manifest)
    request = AuthorApprovalRequest(
        approval_request_id=StableId("approval.bootstrap-audit"),
        project_id=PROJECT,
        bootstrap_bundle_id=bundle.bundle_id,
        candidate_manifest_hash=manifest_hash,
        validation_report_id=validation.report_id,
        requested_at=NOW,
    )
    decision = AuthorApprovalDecision(
        decision_id=StableId("decision.bootstrap-audit"),
        approval_request_id=request.approval_request_id,
        project_id=PROJECT,
        candidate_manifest_hash=manifest_hash,
        validation_report_id=validation.report_id,
        status=AuthorApprovalStatus.APPROVED,
        author_id=StableId("author.bootstrap-audit"),
        reason="approved",
        decided_at=NOW,
    )
    commit_id = CommitId("sha256:" + "d" * 64)
    genesis = GenesisCommitReceipt(
        receipt_id=StableId("genesis.bootstrap-audit"),
        project_id=PROJECT,
        bootstrap_bundle_id=bundle.bundle_id,
        candidate_manifest_hash=manifest_hash,
        validation_report_id=validation.report_id,
        approval_decision_id=decision.decision_id,
        commit_id=commit_id,
        manifest=built.manifest,
        committed_at=NOW,
    )
    freshness = FreshnessDecision(
        status=FreshnessStatus.READY,
        canonical_commit=commit_id,
        r1_basis_commit=commit_id,
        required_snapshot_id=StableId("snapshot.bootstrap-audit"),
        actual_alias_commit=commit_id,
        actual_snapshot_id=StableId("snapshot.bootstrap-audit"),
        actual_snapshot_commit=commit_id,
        reason="ready",
    )
    return {
        "bundle": bundle,
        "ingested": ingested,
        "candidates": built,
        "validation": validation,
        "configuration_fingerprint": ArtifactId("sha256:" + "f" * 64),
        "approval_request": request,
        "approval_decision": decision,
        "genesis": genesis,
        "freshness": freshness,
    }


def test_bootstrap_audit_reports_incomplete_and_complete_states(tmp_path: Path) -> None:
    assert utc_now().tzinfo is not None
    basis = _basis(tmp_path)
    service = BootstrapAuditService(lambda: datetime(2026, 7, 23, tzinfo=UTC))
    incomplete = service.build(
        **{
            key: basis[key]
            for key in (
                "bundle",
                "ingested",
                "candidates",
                "validation",
                "configuration_fingerprint",
            )
        }
    )
    assert incomplete.complete is False
    assert len(incomplete.blockers) == 3
    assert incomplete.approval_status is None

    complete = service.build(**basis)
    assert complete.complete is True
    assert complete.blockers == ()
    assert complete.approval_status is AuthorApprovalStatus.APPROVED
    assert complete.sources[0].allowed_destinations
    assert complete.created_at == datetime(2026, 7, 23, tzinfo=UTC)


@pytest.mark.parametrize(
    ("field", "mutate", "message"),
    (
        (
            "candidates",
            lambda value: replace(value, bootstrap_bundle_id=StableId("bundle.other")),
            "another bundle",
        ),
        (
            "candidates",
            lambda value: replace(
                value,
                manifest=value.manifest.model_copy(
                    update={"project_id": ProjectId("project.other")}
                ),
            ),
            "another project",
        ),
        (
            "validation",
            lambda value: value.model_copy(update={"bundle_id": StableId("bundle.other")}),
            "validation belongs",
        ),
        (
            "ingested",
            lambda value: value[:-1],
            "ingestion set",
        ),
        (
            "approval_request",
            lambda value: value.model_copy(
                update={"validation_report_id": StableId("validation.other")}
            ),
            "approval request",
        ),
        (
            "approval_decision",
            lambda value: value.model_copy(
                update={"validation_report_id": StableId("validation.other")}
            ),
            "approval decision",
        ),
        (
            "genesis",
            lambda value: value.model_copy(
                update={"validation_report_id": StableId("validation.other")}
            ),
            "Genesis receipt",
        ),
        (
            "freshness",
            lambda value: value.model_copy(
                update={"canonical_commit": CommitId("sha256:" + "9" * 64)}
            ),
            "freshness",
        ),
    ),
)
def test_bootstrap_audit_rejects_every_foreign_basis(
    tmp_path: Path,
    field: str,
    mutate: Any,
    message: str,
) -> None:
    basis = _basis(tmp_path)
    basis[field] = mutate(basis[field])
    with pytest.raises(ValueError, match=message):
        BootstrapAuditService(lambda: NOW).build(**basis)


def test_bootstrap_audit_blockers_include_failed_validation_and_freshness(
    tmp_path: Path,
) -> None:
    basis = _basis(tmp_path)
    basis["validation"] = basis["validation"].model_copy(update={"status": ValidationStatus.FAILED})
    basis["approval_decision"] = basis["approval_decision"].model_copy(
        update={"status": AuthorApprovalStatus.REJECTED}
    )
    basis["freshness"] = basis["freshness"].model_copy(update={"status": FreshnessStatus.BLOCKED})
    report = BootstrapAuditService(lambda: NOW).build(**basis)
    assert len(report.blockers) == 3
    assert "bootstrap validation did not pass" in report.blockers
