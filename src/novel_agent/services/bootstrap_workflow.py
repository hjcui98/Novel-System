"""Stage 2 bootstrap root construction, validation, approval, and Genesis commit."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Protocol

from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import AuthorApprovalRow
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.changes import (
    ValidationFinding,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.ids import ArtifactId, ProjectId, SchemaVersion, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    AuthorApprovalDecision,
    AuthorApprovalRequest,
    AuthorApprovalStatus,
    GenesisCommitReceipt,
    PlanProposal,
    ProjectProfileRootDocument,
    ReferenceRootDocument,
    SourceClass,
    SourceClassification,
    SourceDestination,
    WorldPatchCandidate,
)
from novel_agent.services.artifacts import ArtifactRepository, sha256_id
from novel_agent.services.commits import (
    CommitService,
    ProjectAlreadyExistsError,
)
from novel_agent.services.content_addressing import (
    canonical_json_bytes,
    content_id,
    plan_root_content_id,
    text_root_content_id,
    world_root_content_id,
)


class BootstrapWorkflowError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def reference_root_content_id(root: ReferenceRootDocument) -> ArtifactId:
    return content_id(root.model_dump(mode="json", exclude={"root_hash"}))


def project_profile_root_content_id(root: ProjectProfileRootDocument) -> ArtifactId:
    return content_id(root.model_dump(mode="json", exclude={"root_hash"}))


def candidate_manifest_hash(manifest: RootManifest) -> ArtifactId:
    return content_id(manifest.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class BootstrapRootCandidates:
    bootstrap_bundle_id: StableId
    manifest: RootManifest
    text: TextRootDocument
    plan: PlanRootDocument
    world: WorldRootDocument
    reference: ReferenceRootDocument
    profile: ProjectProfileRootDocument
    plan_proposal: PlanProposal
    world_patch: WorldPatchCandidate
    classifications: tuple[SourceClassification, ...]


class BootstrapRootBuilder:
    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts

    def build(
        self,
        project_id: ProjectId,
        bootstrap_bundle_id: StableId,
        text: TextRootDocument,
        plan: PlanRootDocument,
        world: WorldRootDocument,
        reference: ReferenceRootDocument,
        profile: ProjectProfileRootDocument,
        plan_proposal: PlanProposal,
        world_patch: WorldPatchCandidate,
        classifications: tuple[SourceClassification, ...],
    ) -> BootstrapRootCandidates:
        if plan_proposal.project_id != project_id or world_patch.project_id != project_id:
            raise BootstrapWorkflowError("bootstrap proposals belong to another project")
        normalized_text = text.model_copy(update={"root_hash": text_root_content_id(text)})
        normalized_plan = plan.model_copy(update={"root_hash": plan_root_content_id(plan)})
        normalized_world = world.model_copy(update={"root_hash": world_root_content_id(world)})
        normalized_reference = reference.model_copy(
            update={"root_hash": reference_root_content_id(reference)}
        )
        normalized_profile = profile.model_copy(
            update={"root_hash": project_profile_root_content_id(profile)}
        )
        text_artifact = self._store(normalized_text)
        plan_artifact = self._store(normalized_plan)
        world_artifact = self._store(normalized_world)
        reference_artifact = self._store(normalized_reference)
        profile_artifact = self._store(normalized_profile)
        manifest = RootManifest(
            project_id=project_id,
            schema_version=profile.schema_version,
            text_root=TextRootRef.model_validate(text_artifact.model_dump()),
            plan_root=PlanRootRef.model_validate(plan_artifact.model_dump()),
            world_root=WorldRootRef.model_validate(world_artifact.model_dump()),
            reference_root=ReferenceRootRef.model_validate(reference_artifact.model_dump()),
            project_profile_root=ProjectProfileRootRef.model_validate(
                profile_artifact.model_dump()
            ),
        )
        return BootstrapRootCandidates(
            bootstrap_bundle_id=bootstrap_bundle_id,
            manifest=manifest,
            text=normalized_text,
            plan=normalized_plan,
            world=normalized_world,
            reference=normalized_reference,
            profile=normalized_profile,
            plan_proposal=plan_proposal,
            world_patch=world_patch,
            classifications=classifications,
        )

    def _store(self, document: object) -> ArtifactRef:
        if not hasattr(document, "model_dump") or not hasattr(document, "schema_version"):
            raise TypeError("root document must be a versioned domain model")
        payload = canonical_json_bytes(document.model_dump(mode="json"))
        schema_version: SchemaVersion = document.schema_version
        return self._artifacts.put(payload, "application/json", schema_version)


class BootstrapCrossRootValidator:
    _EVALUATOR_ONLY: ClassVar[frozenset[SourceClass]] = frozenset(
        {
            SourceClass.RETROSPECTIVE_SUMMARY,
            SourceClass.FUTURE_TEXT_PRIVATE,
            SourceClass.READ_GOLD,
            SourceClass.REPLAY_GOLD,
        }
    )

    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock

    def validate(self, candidates: BootstrapRootCandidates) -> ValidationReport:
        findings: list[ValidationFinding] = []
        classifications = {item.source_id: item for item in candidates.classifications}
        if len(classifications) != len(candidates.classifications):
            findings.append(
                self._finding("BOOTSTRAP_DUPLICATE_CLASSIFICATION", "source ids repeat")
            )
        if candidates.manifest.parent_commit_ids:
            findings.append(
                self._finding("BOOTSTRAP_GENESIS_PARENT", "Genesis cannot have parents")
            )
        if candidates.plan_proposal.receipt.agent_type is not AgentType.PLANNER:
            findings.append(
                self._finding("BOOTSTRAP_PLAN_AUTHORITY", "Plan was not proposed by Planner")
            )
        if candidates.plan_proposal.receipt.agent_mode is not AgentMode.PROJECT_BOOTSTRAP:
            findings.append(
                self._finding("BOOTSTRAP_PLAN_MODE", "Plan proposal used the wrong mode")
            )
        if candidates.world_patch.receipt.agent_type is not AgentType.MEMORY_CURATOR:
            findings.append(
                self._finding("BOOTSTRAP_WORLD_AUTHORITY", "World was not proposed by Curator")
            )
        if candidates.world_patch.receipt.agent_mode is not AgentMode.BOOTSTRAP:
            findings.append(
                self._finding("BOOTSTRAP_WORLD_MODE", "World proposal used the wrong mode")
            )
        root_documents = (
            ("TEXT", candidates.manifest.text_root, candidates.text),
            ("PLAN", candidates.manifest.plan_root, candidates.plan),
            ("WORLD", candidates.manifest.world_root, candidates.world),
            ("REFERENCE", candidates.manifest.reference_root, candidates.reference),
            ("PROFILE", candidates.manifest.project_profile_root, candidates.profile),
        )
        for name, root_ref, document in root_documents:
            payload_hash = sha256_id(canonical_json_bytes(document.model_dump(mode="json")))
            if root_ref.artifact_id != payload_hash:
                findings.append(
                    self._finding(
                        f"BOOTSTRAP_{name}_ARTIFACT_MISMATCH",
                        "manifest does not reference the candidate root payload",
                    )
                )

        for item in candidates.plan_proposal.items:
            self._validate_sources(
                item.source_ids,
                SourceDestination.PLAN,
                classifications,
                findings,
                "PLAN",
            )
        self._validate_sources(
            candidates.world_patch.origin_source_ids,
            SourceDestination.WORLD,
            classifications,
            findings,
            "WORLD",
        )
        for item in candidates.world_patch.items:
            self._validate_sources(
                item.source_ids,
                SourceDestination.WORLD,
                classifications,
                findings,
                "WORLD",
            )
        for asset in candidates.reference.assets:
            self._validate_sources(
                (asset.source_id,),
                SourceDestination.REFERENCE,
                classifications,
                findings,
                "REFERENCE",
            )
        status = ValidationStatus.PASSED if not findings else ValidationStatus.FAILED
        return ValidationReport(
            report_id=StableId(
                f"bootstrap-validation.{candidate_manifest_hash(candidates.manifest).root[-24:]}"
            ),
            bundle_id=candidates.bootstrap_bundle_id,
            status=status,
            findings=tuple(findings),
            schema_version=SchemaVersion("2.0.0"),
            validation_profile="stage2-bootstrap-cross-root-v1",
            validated_at=self._clock(),
        )

    def _validate_sources(
        self,
        source_ids: tuple[StableId, ...],
        destination: SourceDestination,
        classifications: dict[StableId, SourceClassification],
        findings: list[ValidationFinding],
        path: str,
    ) -> None:
        for source_id in source_ids:
            classification = classifications.get(source_id)
            if classification is None:
                findings.append(self._finding(f"BOOTSTRAP_{path}_UNKNOWN_SOURCE", source_id.root))
                continue
            if classification.source_class in self._EVALUATOR_ONLY:
                findings.append(self._finding(f"BOOTSTRAP_{path}_FUTURE_LEAKAGE", source_id.root))
            if destination not in classification.allowed_destinations:
                findings.append(self._finding(f"BOOTSTRAP_{path}_ROUTE_DENIED", source_id.root))

    @staticmethod
    def _finding(code: str, message: str) -> ValidationFinding:
        return ValidationFinding(code=code, severity="error", message=message)


class InMemoryAuthorApprovalRepository:
    """Checkpointable interface baseline; durable adapters can replace this store."""

    def __init__(self) -> None:
        self._requests: dict[StableId, AuthorApprovalRequest] = {}
        self._decisions: dict[StableId, AuthorApprovalDecision] = {}

    def request(self, approval: AuthorApprovalRequest) -> AuthorApprovalRequest:
        existing = self._requests.get(approval.approval_request_id)
        if existing is not None:
            if not self._same_request_basis(existing, approval):
                raise BootstrapWorkflowError("approval request identity collision")
            return existing
        self._requests[approval.approval_request_id] = approval
        return approval

    def decide(self, decision: AuthorApprovalDecision) -> AuthorApprovalDecision:
        request = self._requests.get(decision.approval_request_id)
        if request is None:
            raise BootstrapWorkflowError("unknown approval request")
        self._validate_decision_basis(request, decision)
        existing = self._decisions.get(decision.approval_request_id)
        if existing is not None:
            if existing != decision:
                raise BootstrapWorkflowError("approval request already decided differently")
            return existing
        self._decisions[decision.approval_request_id] = decision
        self._requests[decision.approval_request_id] = request.model_copy(
            update={"status": decision.status}
        )
        return decision

    @staticmethod
    def _same_request_basis(
        left: AuthorApprovalRequest,
        right: AuthorApprovalRequest,
    ) -> bool:
        return (
            left.approval_request_id,
            left.project_id,
            left.bootstrap_bundle_id,
            left.candidate_manifest_hash,
            left.validation_report_id,
        ) == (
            right.approval_request_id,
            right.project_id,
            right.bootstrap_bundle_id,
            right.candidate_manifest_hash,
            right.validation_report_id,
        )

    @staticmethod
    def _validate_decision_basis(
        request: AuthorApprovalRequest, decision: AuthorApprovalDecision
    ) -> None:
        if (
            request.project_id != decision.project_id
            or request.candidate_manifest_hash != decision.candidate_manifest_hash
            or request.validation_report_id != decision.validation_report_id
        ):
            raise BootstrapWorkflowError("approval decision does not match request basis")

    def load_request(self, approval_request_id: StableId) -> AuthorApprovalRequest:
        try:
            return self._requests[approval_request_id]
        except KeyError as error:
            raise BootstrapWorkflowError("unknown approval request") from error

    def load_decision(self, approval_request_id: StableId) -> AuthorApprovalDecision | None:
        return self._decisions.get(approval_request_id)


class AuthorApprovalRepository(Protocol):
    def request(self, approval: AuthorApprovalRequest) -> AuthorApprovalRequest: ...

    def decide(self, decision: AuthorApprovalDecision) -> AuthorApprovalDecision: ...

    def load_request(self, approval_request_id: StableId) -> AuthorApprovalRequest: ...

    def load_decision(self, approval_request_id: StableId) -> AuthorApprovalDecision | None: ...


class SqlAuthorApprovalRepository:
    """Durable approval interrupt state used across process restarts."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def request(self, approval: AuthorApprovalRequest) -> AuthorApprovalRequest:
        with self._session_factory() as session, session.begin():
            existing = session.get(AuthorApprovalRow, approval.approval_request_id.root)
            if existing is not None:
                persisted = AuthorApprovalRequest.model_validate_json(
                    json.dumps(existing.request_json)
                ).model_copy(update={"status": AuthorApprovalStatus(existing.status)})
                if not InMemoryAuthorApprovalRepository._same_request_basis(persisted, approval):
                    raise BootstrapWorkflowError("approval request identity collision")
                return persisted
            session.add(
                AuthorApprovalRow(
                    approval_request_id=approval.approval_request_id.root,
                    project_id=approval.project_id.root,
                    status=approval.status.value,
                    request_json=approval.model_dump(mode="json"),
                    decision_json=None,
                    requested_at=approval.requested_at,
                    decided_at=None,
                )
            )
        return approval

    def decide(self, decision: AuthorApprovalDecision) -> AuthorApprovalDecision:
        with self._session_factory() as session, session.begin():
            row = session.get(AuthorApprovalRow, decision.approval_request_id.root)
            if row is None:
                raise BootstrapWorkflowError("unknown approval request")
            request = AuthorApprovalRequest.model_validate_json(json.dumps(row.request_json))
            InMemoryAuthorApprovalRepository._validate_decision_basis(request, decision)
            if row.decision_json is not None:
                persisted = AuthorApprovalDecision.model_validate_json(
                    json.dumps(row.decision_json)
                )
                if persisted != decision:
                    raise BootstrapWorkflowError("approval request already decided differently")
                return persisted
            row.status = decision.status.value
            row.decision_json = decision.model_dump(mode="json")
            row.decided_at = decision.decided_at
        return decision

    def load_request(self, approval_request_id: StableId) -> AuthorApprovalRequest:
        with self._session_factory() as session:
            row = session.get(AuthorApprovalRow, approval_request_id.root)
            if row is None:
                raise BootstrapWorkflowError("unknown approval request")
            request = AuthorApprovalRequest.model_validate_json(json.dumps(row.request_json))
            return request.model_copy(update={"status": AuthorApprovalStatus(row.status)})

    def load_decision(self, approval_request_id: StableId) -> AuthorApprovalDecision | None:
        with self._session_factory() as session:
            row = session.get(AuthorApprovalRow, approval_request_id.root)
            if row is None or row.decision_json is None:
                return None
            return AuthorApprovalDecision.model_validate_json(json.dumps(row.decision_json))


class GenesisCoordinator:
    def __init__(
        self,
        commits: CommitService,
        approvals: AuthorApprovalRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._commits = commits
        self._approvals = approvals
        self._clock = clock

    def create_approval_request(
        self,
        candidates: BootstrapRootCandidates,
        validation: ValidationReport,
    ) -> AuthorApprovalRequest:
        if validation.bundle_id != candidates.bootstrap_bundle_id:
            raise BootstrapWorkflowError("validation report belongs to another bundle")
        if validation.status is not ValidationStatus.PASSED:
            raise BootstrapWorkflowError("failed bootstrap validation cannot request approval")
        manifest_hash = candidate_manifest_hash(candidates.manifest)
        self._validate_report_basis(candidates, validation, manifest_hash)
        request = AuthorApprovalRequest(
            approval_request_id=StableId(f"bootstrap-approval.{manifest_hash.root[-24:]}"),
            project_id=candidates.manifest.project_id,
            bootstrap_bundle_id=candidates.bootstrap_bundle_id,
            candidate_manifest_hash=manifest_hash,
            validation_report_id=validation.report_id,
            requested_at=self._clock(),
        )
        return self._approvals.request(request)

    def commit(
        self,
        candidates: BootstrapRootCandidates,
        validation: ValidationReport,
        approval_request_id: StableId,
    ) -> GenesisCommitReceipt:
        request = self._approvals.load_request(approval_request_id)
        decision = self._approvals.load_decision(approval_request_id)
        manifest_hash = candidate_manifest_hash(candidates.manifest)
        if validation.status is not ValidationStatus.PASSED:
            raise BootstrapWorkflowError("Genesis commit requires passed validation")
        self._validate_report_basis(candidates, validation, manifest_hash)
        if decision is None or decision.status is not AuthorApprovalStatus.APPROVED:
            raise BootstrapWorkflowError("Genesis commit requires explicit author approval")
        expected = (
            candidates.manifest.project_id,
            candidates.bootstrap_bundle_id,
            manifest_hash,
            validation.report_id,
        )
        actual = (
            request.project_id,
            request.bootstrap_bundle_id,
            decision.candidate_manifest_hash,
            decision.validation_report_id,
        )
        if actual != expected:
            raise BootstrapWorkflowError("approval or validation basis is stale")
        replayed = False
        try:
            commit_id = self._commits.initialize_project(candidates.manifest)
        except ProjectAlreadyExistsError:
            commit_id = self._commits.current_commit(candidates.manifest.project_id)
            if self._commits.load_manifest(commit_id) != candidates.manifest:
                raise BootstrapWorkflowError("project exists with a different Genesis") from None
            replayed = True
        return GenesisCommitReceipt(
            receipt_id=StableId(f"genesis-receipt.{commit_id.root[-24:]}"),
            project_id=candidates.manifest.project_id,
            bootstrap_bundle_id=candidates.bootstrap_bundle_id,
            candidate_manifest_hash=manifest_hash,
            validation_report_id=validation.report_id,
            approval_decision_id=decision.decision_id,
            commit_id=commit_id,
            manifest=candidates.manifest,
            idempotent_replay=replayed,
            committed_at=self._clock(),
        )

    @staticmethod
    def _validate_report_basis(
        candidates: BootstrapRootCandidates,
        validation: ValidationReport,
        manifest_hash: ArtifactId,
    ) -> None:
        expected_id = StableId(f"bootstrap-validation.{manifest_hash.root[-24:]}")
        if (
            validation.bundle_id != candidates.bootstrap_bundle_id
            or validation.report_id != expected_id
            or validation.validation_profile != "stage2-bootstrap-cross-root-v1"
        ):
            raise BootstrapWorkflowError("validation report basis is stale or untrusted")
