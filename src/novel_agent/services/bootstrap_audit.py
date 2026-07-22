"""Produce a source-backed Bootstrap and Planner audit without granting write authority."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from novel_agent.domain.changes import ValidationReport, ValidationStatus
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory import FreshnessDecision, FreshnessStatus
from novel_agent.domain.stage2 import (
    AuthorApprovalDecision,
    AuthorApprovalRequest,
    AuthorApprovalStatus,
    BootstrapAuditReport,
    BootstrapSourceAuditEntry,
    GenesisCommitReceipt,
    ProjectBootstrapBundle,
)
from novel_agent.services.bootstrap import IngestedBootstrapSource
from novel_agent.services.bootstrap_workflow import (
    BootstrapRootCandidates,
    candidate_manifest_hash,
)
from novel_agent.services.content_addressing import content_id


def utc_now() -> datetime:
    return datetime.now(UTC)


class BootstrapAuditService:
    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock

    def build(
        self,
        *,
        bundle: ProjectBootstrapBundle,
        ingested: tuple[IngestedBootstrapSource, ...],
        candidates: BootstrapRootCandidates,
        validation: ValidationReport,
        configuration_fingerprint: ArtifactId,
        approval_request: AuthorApprovalRequest | None = None,
        approval_decision: AuthorApprovalDecision | None = None,
        genesis: GenesisCommitReceipt | None = None,
        freshness: FreshnessDecision | None = None,
    ) -> BootstrapAuditReport:
        if candidates.bootstrap_bundle_id != bundle.bundle_id:
            raise ValueError("Bootstrap audit candidates belong to another bundle")
        if candidates.manifest.project_id != bundle.project_id:
            raise ValueError("Bootstrap audit candidates belong to another project")
        if validation.bundle_id != bundle.bundle_id:
            raise ValueError("Bootstrap audit validation belongs to another bundle")
        source_ids = {source.source_id for source in bundle.sources}
        if source_ids != {item.source.source_id for item in ingested}:
            raise ValueError("Bootstrap audit ingestion set differs from its bundle")
        classifications = {item.source.source_id: item.classification for item in ingested}
        sources = tuple(
            BootstrapSourceAuditEntry(
                source_id=source.source_id,
                source_class=source.source_class,
                content_hash=source.content_hash,
                evaluator_only=source.evaluator_only,
                allowed_destinations=classifications[source.source_id].allowed_destinations,
                forbidden_destinations=classifications[source.source_id].forbidden_destinations,
            )
            for source in bundle.sources
        )
        manifest_hash = candidate_manifest_hash(candidates.manifest)
        if approval_request is not None and (
            approval_request.bootstrap_bundle_id != bundle.bundle_id
            or approval_request.candidate_manifest_hash != manifest_hash
            or approval_request.validation_report_id != validation.report_id
        ):
            raise ValueError("Bootstrap audit approval request has another basis")
        if approval_decision is not None and (
            approval_request is None
            or approval_decision.approval_request_id != approval_request.approval_request_id
            or approval_decision.candidate_manifest_hash != manifest_hash
            or approval_decision.validation_report_id != validation.report_id
        ):
            raise ValueError("Bootstrap audit approval decision has another basis")
        if genesis is not None and (
            approval_decision is None
            or genesis.bootstrap_bundle_id != bundle.bundle_id
            or genesis.candidate_manifest_hash != manifest_hash
            or genesis.validation_report_id != validation.report_id
            or genesis.approval_decision_id != approval_decision.decision_id
        ):
            raise ValueError("Bootstrap audit Genesis receipt has another basis")
        if freshness is not None and (
            genesis is None or freshness.canonical_commit != genesis.commit_id
        ):
            raise ValueError("Bootstrap audit freshness has another Genesis basis")
        blockers: list[str] = []
        if validation.status is not ValidationStatus.PASSED:
            blockers.append("bootstrap validation did not pass")
        if (
            approval_decision is None
            or approval_decision.status is not AuthorApprovalStatus.APPROVED
        ):
            blockers.append("explicit author approval is pending")
        if genesis is None:
            blockers.append("Genesis commit is absent")
        if freshness is None or freshness.status not in {
            FreshnessStatus.READY,
            FreshnessStatus.DEGRADED,
            FreshnessStatus.OVERRIDDEN,
        }:
            blockers.append("Genesis projection freshness is not continuable")
        report_payload = {
            "bundle_hash": bundle.bundle_hash.root,
            "manifest_hash": manifest_hash.root,
            "validation_report_id": validation.report_id.root,
            "approval_request_id": (
                approval_request.approval_request_id.root if approval_request else None
            ),
            "approval_decision_id": (
                approval_decision.decision_id.root if approval_decision else None
            ),
            "genesis_receipt_id": genesis.receipt_id.root if genesis else None,
            "configuration_fingerprint": configuration_fingerprint.root,
        }
        report_hash = content_id(report_payload)
        return BootstrapAuditReport(
            report_id=StableId(f"bootstrap-audit.{report_hash.root[-24:]}"),
            project_id=bundle.project_id,
            bootstrap_bundle_id=bundle.bundle_id,
            bundle_hash=bundle.bundle_hash,
            sources=sources,
            planner_proposal_id=candidates.plan_proposal.proposal_id,
            planner_coverage=candidates.plan_proposal.coverage,
            planner_unresolved=candidates.plan_proposal.unresolved,
            curator_proposal_id=candidates.world_patch.proposal_id,
            curator_coverage=candidates.world_patch.extraction_coverage,
            curator_unresolved=candidates.world_patch.unresolved_claims,
            candidate_manifest_hash=manifest_hash,
            validation_report_id=validation.report_id,
            validation_status=validation.status.value,
            approval_request_id=(
                approval_request.approval_request_id if approval_request else None
            ),
            approval_status=(
                approval_decision.status
                if approval_decision is not None
                else approval_request.status
                if approval_request is not None
                else None
            ),
            genesis_receipt_id=genesis.receipt_id if genesis else None,
            genesis_commit_id=genesis.commit_id if genesis else None,
            freshness=freshness,
            blockers=tuple(blockers),
            complete=not blockers,
            configuration_fingerprint=configuration_fingerprint,
            created_at=self._clock(),
        )
