"""Local, framework-independent Stage 2W memory-write coordinator."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from novel_agent.domain.artifacts import ArtifactRef, RootManifest
from novel_agent.domain.changes import ObservedChangeSet
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    DerivedBuildStatus,
    DerivedSnapshotLite,
    FreshnessDecision,
    FreshnessMode,
    FreshnessRequest,
    FreshnessStatus,
)
from novel_agent.domain.memory_write import (
    BoundaryPropagationReceipt,
    CandidateMaterialization,
    CandidateProducerKind,
    CandidateRevision,
    CanonicalWriteBasis,
    ContinuationDecision,
    CuratorProposalAccepted,
    CuratorProposalAttemptReceipt,
    CuratorProposalAttemptStatus,
    CuratorProposalRejected,
    CuratorProposalRejection,
    CuratorProposalRepairDirective,
    HumanApprovalDecision,
    HumanApprovalRequest,
    HumanDecisionKind,
    MemoryWriteBudgetUsage,
    MemoryWriteCandidatePayload,
    MemoryWriteCheckpoint,
    MemoryWriteState,
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowRequest,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
    NarrativePosition,
    NormalizationResult,
    NormalizationStatus,
    ProjectionReadinessResult,
    ProjectionReadinessStatus,
    ProposalHumanDecisionKind,
    ProposalHumanReviewRequest,
    ProposalRejectionStage,
    QuarantinePackage,
    RepairAction,
    RepairActionReceipt,
    RepairContext,
    RepairDirective,
    TrustedWorldCandidateInput,
    ValidationDecision,
    ValidationDisposition,
)
from novel_agent.domain.runtime import EffectStatus, ResumabilityStatus, RunEvent, RunEventType
from novel_agent.domain.stage2 import (
    GuardianDecision,
    GuardianOutcome,
    PatchRiskAssessment,
    PatchRiskLevel,
    WriteGateDecision,
    WriteGateOutcome,
)
from novel_agent.ports.memory_write import (
    CanonicalReadPort,
    CuratorProposalAttemptRequest,
    CuratorProposalResult,
    CuratorProposalTransportError,
    CuratorRepairRejectedError,
    CuratorRepairRequest,
    CuratorRepairResult,
    DurableMemoryWriteCommitRequest,
    GuardianReviewRequest,
    GuardianReviewResult,
    MemoryWriteCommitResult,
    MemoryWriteCommitStatus,
    ProjectionReadinessPort,
    RootMaterializationResult,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.commits import manifest_commit_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.information_boundary import (
    InformationBoundaryPort,
    InformationBoundaryViolation,
)
from novel_agent.services.memory_repair_policy import BoundedMemoryRepairPolicy
from novel_agent.services.memory_write_validation import Stage2ValidationV2Adapter
from novel_agent.services.mutation_normalizer import MutationNormalizer
from novel_agent.services.pre_candidate_repair import (
    BoundedPreCandidateRepairPolicy,
    InMemoryCuratorProposalAttemptRepository,
    requested_attempt,
)
from novel_agent.services.root_update_materializer import RootUpdateMaterializer


class MemoryWriteWorkflowError(RuntimeError):
    """Programming, I/O, or contract corruption error."""


class MemoryWriteIdentityCollision(MemoryWriteWorkflowError):
    pass


class InMemoryArtifactRepository:
    """Small immutable ArtifactRepositoryPort used by local workflow tests."""

    def __init__(self) -> None:
        self._objects: dict[ArtifactId, tuple[bytes, str, SchemaVersion]] = {}

    def put(self, data: bytes, media_type: str, schema_version: SchemaVersion) -> ArtifactRef:
        artifact_id = sha256_id(data)
        existing = self._objects.get(artifact_id)
        value = (data, media_type, schema_version)
        if existing is not None and existing[0] != data:
            raise MemoryWriteWorkflowError("content-addressed artifact collision")
        self._objects[artifact_id] = existing or value
        return ArtifactRef(
            artifact_id=artifact_id,
            media_type=media_type,
            byte_length=len(data),
            schema_version=schema_version,
        )

    def read_verified(self, artifact: ArtifactRef) -> bytes:
        value = self._objects.get(artifact.artifact_id)
        if value is None:
            raise KeyError(f"artifact not found: {artifact.artifact_id.root}")
        data, media_type, _ = value
        if (
            len(data) != artifact.byte_length
            or media_type != artifact.media_type
            or sha256_id(data) != artifact.artifact_id
        ):
            raise MemoryWriteWorkflowError("artifact integrity verification failed")
        return data

    def put_model(self, model: Any, media_type: str, schema_version: SchemaVersion) -> ArtifactRef:
        return self.put(
            canonical_json_bytes(model.model_dump(mode="json")), media_type, schema_version
        )

    def read_model(self, artifact: ArtifactRef, model_type: Any) -> Any:
        return model_type.model_validate_json(self.read_verified(artifact), strict=True)


class InMemoryCandidateLineageRepository:
    def __init__(self) -> None:
        self._items: dict[StableId, CandidateRevision] = {}

    def persist(self, candidate: CandidateRevision) -> CandidateRevision:
        existing = self._items.get(candidate.candidate_id)
        if existing is not None:
            if (
                existing.content_hash != candidate.content_hash
                or existing.basis_hash != candidate.basis_hash
                or existing.parent_candidate_id != candidate.parent_candidate_id
                or existing.revision_no != candidate.revision_no
            ):
                raise MemoryWriteWorkflowError("candidate identity collision")
            return existing
        if candidate.parent_candidate_id is None:
            if candidate.revision_no != 1:
                raise MemoryWriteWorkflowError("first candidate revision must be revision 1")
        else:
            parent = self._items.get(candidate.parent_candidate_id)
            if parent is None or candidate.revision_no != parent.revision_no + 1:
                raise MemoryWriteWorkflowError("candidate revision parent or sequence is invalid")
            if candidate.base_commit != parent.base_commit:
                raise MemoryWriteWorkflowError("candidate revision changed base commit")
        self._items[candidate.candidate_id] = candidate
        return candidate

    def get(self, candidate_id: StableId) -> CandidateRevision | None:
        return self._items.get(candidate_id)

    def list_for_request(self, request_id: StableId) -> tuple[CandidateRevision, ...]:
        prefix = f"candidate.{request_id.root}."
        return tuple(
            item for item in self._items.values() if item.candidate_id.root.startswith(prefix)
        )


class InMemoryCheckpointRepository:
    def __init__(self, artifacts: InMemoryArtifactRepository) -> None:
        self._artifacts = artifacts
        self._items: dict[ArtifactId, MemoryWriteCheckpoint] = {}

    def save(self, checkpoint: MemoryWriteCheckpoint) -> ArtifactRef:
        ref = _put_model(
            self._artifacts,
            checkpoint,
            "application/vnd.novel-agent.memory-write-checkpoint+json",
            SchemaVersion("0.1.0"),
        )
        self._items[ref.artifact_id] = checkpoint
        return ref

    def load(self, checkpoint_ref: ArtifactRef) -> MemoryWriteCheckpoint:
        existing = self._items.get(checkpoint_ref.artifact_id)
        if existing is not None:
            return existing
        return MemoryWriteCheckpoint.model_validate_json(
            self._artifacts.read_verified(checkpoint_ref), strict=True
        )


class InMemoryQuarantineRepository:
    def __init__(self, artifacts: InMemoryArtifactRepository) -> None:
        self._artifacts = artifacts
        self.packages: list[QuarantinePackage] = []

    def persist(self, package: QuarantinePackage) -> ArtifactRef:
        self.packages.append(package)
        return _put_model(
            self._artifacts,
            package,
            "application/vnd.novel-agent.quarantine-package+json",
            SchemaVersion("0.1.0"),
        )


class InMemoryRunEventSink:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def append(self, event: RunEvent) -> RunEvent:
        if event.sequence_no != len(self.events) + 1:
            raise MemoryWriteWorkflowError("workflow event sequence is not contiguous")
        self.events.append(event)
        return event


class StaticCanonicalReadPort:
    def __init__(self, basis: CanonicalWriteBasis) -> None:
        self._basis = basis

    def load_verified(self, project_id: ProjectId, commit_id: CommitId) -> CanonicalWriteBasis:
        if project_id != self._basis.project_id or commit_id != self._basis.commit_id:
            raise MemoryWriteWorkflowError("requested canonical basis is not available")
        return self._basis

    def current_commit(self, project_id: ProjectId) -> CommitId:
        if project_id != self._basis.project_id:
            raise MemoryWriteWorkflowError("unknown project")
        return self._basis.commit_id


class InMemoryCommitPort:
    """Idempotent CAS commit port with exact request replay semantics."""

    def __init__(self, *, current_commit: CommitId) -> None:
        self.current = current_commit
        self.results: dict[StableId, tuple[ArtifactId, MemoryWriteCommitResult]] = {}
        self.calls = 0

    def resolve_or_replay_exact(
        self, request: DurableMemoryWriteCommitRequest
    ) -> MemoryWriteCommitResult:
        previous = self.results.get(request.idempotency_key)
        if previous is not None:
            request_hash, result = previous
            if request_hash != request.request_hash:
                raise MemoryWriteIdentityCollision(
                    "idempotency key refers to another request basis"
                )
            return result
        self.calls += 1
        if request.base_commit != self.current:
            result = MemoryWriteCommitResult(
                request_id=request.request_id,
                status=MemoryWriteCommitStatus.CONFLICTED,
                reason="base commit is not current",
            )
            self.results[request.idempotency_key] = (request.request_hash, result)
            return result
        manifest = request.bundle.proposed_roots
        commit_id = manifest_commit_id(manifest)
        receipt_data = canonical_json_bytes(
            {"effect": request.commit_effect_id.root, "commit": commit_id.root}
        )
        receipt = ArtifactRef(
            artifact_id=sha256_id(receipt_data),
            media_type="application/vnd.novel-agent.commit-receipt+json",
            byte_length=len(receipt_data),
            schema_version=manifest.schema_version,
        )
        result = MemoryWriteCommitResult(
            request_id=request.request_id,
            status=MemoryWriteCommitStatus.ACCEPTED,
            commit_id=commit_id,
            manifest=manifest,
            commit_receipt_ref=receipt,
            committed_operation_ids=tuple(
                item.operation_id for item in request.bundle.observed_changes.operations
            ),
        )
        self.current = commit_id
        self.results[request.idempotency_key] = (request.request_hash, result)
        return result


class ImmediateProjectionReadinessPort:
    def __init__(
        self, *, pending: bool = False, artifacts: InMemoryArtifactRepository | None = None
    ) -> None:
        self.pending = pending
        self.artifacts = artifacts or InMemoryArtifactRepository()
        self.calls: list[StableId] = []

    def request_or_read_by_effect_id(
        self, project_id: ProjectId, commit_id: CommitId, effect_id: StableId
    ) -> ProjectionReadinessResult:
        del project_id
        self.calls.append(effect_id)
        if self.pending:
            return ProjectionReadinessResult(
                effect_id=effect_id,
                status=ProjectionReadinessStatus.PENDING,
                reason="projection is waiting for an outbox worker",
            )
        return self._ready(commit_id, effect_id)

    def await_or_check(
        self, project_id: ProjectId, commit_id: CommitId, effect_id: StableId
    ) -> ProjectionReadinessResult:
        del project_id
        self.calls.append(effect_id)
        return (
            self._ready(commit_id, effect_id)
            if not self.pending
            else ProjectionReadinessResult(
                effect_id=effect_id,
                status=ProjectionReadinessStatus.PENDING,
                reason="projection is still pending",
            )
        )

    def _ready(self, commit_id: CommitId, effect_id: StableId) -> ProjectionReadinessResult:
        snapshot_id = StableId(f"snapshot.{commit_id.root.removeprefix('sha256:')}")
        now = datetime.now(UTC)
        snapshot = DerivedSnapshotLite(
            snapshot_id=snapshot_id,
            source_commit=commit_id,
            anchor_build_id=StableId(f"anchor.{commit_id.root[-16:]}"),
            anchor_index_version="stage2w-memory",
            grounded_index_version="stage2w-memory",
            embedding_profile="stage2w-test",
            fusion_profile="stage2w-test",
            build_status=DerivedBuildStatus.EXACT,
            published_at=now,
        )
        freshness = FreshnessRequest(
            canonical_commit=commit_id,
            r1_basis_commit=commit_id,
            required_snapshot_id=snapshot_id,
            actual_alias_commit=commit_id,
            actual_snapshot=snapshot,
            mode=FreshnessMode.BLOCK_ON_MISMATCH,
        )
        from novel_agent.services.projection import FreshnessGate

        decision = FreshnessGate.evaluate(freshness)
        projection_ref = self.artifacts.put(
            canonical_json_bytes({"effect": effect_id.root, "commit": commit_id.root}),
            "application/vnd.novel-agent.projection-receipt+json",
            SchemaVersion("0.1.0"),
        )
        freshness_ref = self.artifacts.put(
            canonical_json_bytes(decision.model_dump(mode="json")),
            "application/vnd.novel-agent.freshness-receipt+json",
            SchemaVersion("0.1.0"),
        )
        return ProjectionReadinessResult(
            effect_id=effect_id,
            status=ProjectionReadinessStatus.READY,
            projection_receipt_ref=projection_ref,
            freshness_receipt_ref=freshness_ref,
            projection_snapshot_id=snapshot_id,
            freshness=decision,
        )


class _Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _NoopBoundary:
    def verify_request_and_derivation_graph(
        self, request: MemoryWriteWorkflowRequest, basis: CanonicalWriteBasis
    ) -> None:
        if request.project_id != basis.project_id or request.base_commit != basis.commit_id:
            raise InformationBoundaryViolation("workflow basis mismatch")

    def verify_derivation_chain(self, **_: Any) -> Any:
        raise InformationBoundaryViolation("no derivation receipt verifier configured")


@dataclass(slots=True)
class _WorkflowData:
    request: MemoryWriteWorkflowRequest
    artifacts: Any | None = None
    state: MemoryWriteState = MemoryWriteState.LOAD_BASIS
    basis: CanonicalWriteBasis | None = None
    candidate: CandidateRevision | None = None
    proposal_attempt_no: int = 0
    inflight_proposal_attempt: CuratorProposalAttemptReceipt | None = None
    proposal_outcome: CuratorProposalAccepted | CuratorProposalRejected | None = None
    proposal_attempt_refs: list[ArtifactRef] = field(default_factory=list)
    proposal_rejections: list[CuratorProposalRejection] = field(default_factory=list)
    proposal_rejection_refs: list[ArtifactRef] = field(default_factory=list)
    proposal_directive: CuratorProposalRepairDirective | None = None
    proposal_directive_ref: ArtifactRef | None = None
    proposal_feedback_ref: ArtifactRef | None = None
    proposal_budget_reservation_ref: ArtifactRef | None = None
    last_proposal_output_hash: ArtifactId | None = None
    last_proposal_rejection_signature: ArtifactId | None = None
    same_proposal_output_count: int = 0
    same_proposal_rejection_count: int = 0
    materialization: Any | None = None
    bundle: Any | None = None
    validation: ValidationDecision | None = None
    risk: PatchRiskAssessment | None = None
    guardian: GuardianDecision | None = None
    gate: WriteGateDecision | None = None
    directive: RepairDirective | None = None
    approval_request: HumanApprovalRequest | None = None
    proposal_human_request: ProposalHumanReviewRequest | None = None
    commit_request: DurableMemoryWriteCommitRequest | None = None
    commit_result: MemoryWriteCommitResult | None = None
    projection: ProjectionReadinessResult | None = None
    usage: MemoryWriteBudgetUsage = field(default_factory=MemoryWriteBudgetUsage)
    action_receipts: list[RepairActionReceipt] = field(default_factory=list)
    seen_content_hashes: list[ArtifactId] = field(default_factory=list)
    quarantine_refs: list[ArtifactRef] = field(default_factory=list)
    quarantined_operation_ids: list[StableId] = field(default_factory=list)
    committed_operation_ids: list[StableId] = field(default_factory=list)
    world_mutation_noop: bool = False
    degraded: bool = False
    terminal_codes: list[str] = field(default_factory=list)
    checkpoint_ref: ArtifactRef | None = None
    commit_effect_id: StableId | None = None
    projection_effect_id: StableId | None = None
    started_at: datetime | None = None
    recovered_inflight_proposal: bool = False


class LocalMemoryWriteWorkflow:
    """The only Stage 2W component that owns the semantic repair loop."""

    def __init__(
        self,
        *,
        canonical_read: CanonicalReadPort,
        curator: Any | None = None,
        normalizer: Any | None = None,
        validator: Any | None = None,
        repair_policy: Any | None = None,
        guardian: Any | None = None,
        risk_classifier: Any | None = None,
        write_gate: Any | None = None,
        commit: Any,
        root_updates: Any | None = None,
        information_boundary: Any | None = None,
        artifacts: InMemoryArtifactRepository | Any | None = None,
        lineage: Any | None = None,
        quarantine: Any | None = None,
        checkpoint: Any | None = None,
        events: Any | None = None,
        projection: ProjectionReadinessPort | None = None,
        human: Any | None = None,
        proposal_attempts: Any | None = None,
        proposal_policy: Any | None = None,
        proposal_human: Any | None = None,
        fault_injector: Any | None = None,
        clock: Any | None = None,
        request_step_limit: int = 128,
    ) -> None:
        self._canonical_read = canonical_read
        self._artifacts = artifacts or InMemoryArtifactRepository()
        self._lineage = lineage or InMemoryCandidateLineageRepository()
        self._checkpoint = checkpoint or InMemoryCheckpointRepository(self._artifacts)
        self._quarantine = quarantine or InMemoryQuarantineRepository(self._artifacts)
        self._events = events or InMemoryRunEventSink()
        self._clock = clock or _Clock()
        self._curator = curator
        self._commit_port = commit
        self._guardian = guardian
        self._human = human
        self._proposal_human = proposal_human
        self._fault_injector = fault_injector
        self._projection = projection or ImmediateProjectionReadinessPort(artifacts=self._artifacts)
        self._boundary = information_boundary or InformationBoundaryPort(
            artifact_reader=self._artifacts
        )
        self._repair_policy = repair_policy or BoundedMemoryRepairPolicy()
        self._proposal_attempts = proposal_attempts or InMemoryCuratorProposalAttemptRepository(
            self._artifacts
        )
        self._proposal_policy = proposal_policy or BoundedPreCandidateRepairPolicy()
        self._risk_classifier = risk_classifier
        self._write_gate = write_gate
        self._request_step_limit = request_step_limit
        self._materializations: dict[StableId, Any] = {}
        self._validations: dict[StableId, ValidationDecision] = {}
        self._checkpoint_states: dict[ArtifactId, _WorkflowData] = {}
        self._normalizer = normalizer
        self._root_updates = root_updates
        if self._normalizer is None:
            self._normalizer = MutationNormalizer(
                payload_loader=lambda ref: _read_model(
                    self._artifacts, ref, MemoryWriteCandidatePayload
                ),
                artifact_writer=self._artifacts,
            )
        if self._root_updates is None:
            self._root_updates = RootUpdateMaterializer(
                payload_loader=lambda ref: _read_model(
                    self._artifacts, ref, MemoryWriteCandidatePayload
                ),
                artifact_writer=self._artifacts,
            )
        self._validator = validator or Stage2ValidationV2Adapter()

    async def execute(self, request: MemoryWriteWorkflowRequest) -> MemoryWriteWorkflowResult:
        data = await self._initialize(request)
        if isinstance(data, MemoryWriteWorkflowResult):
            return data
        self._event(
            data,
            RunEventType.RUN_RESUMED if request.resume_checkpoint else RunEventType.TASK_STARTED,
        )
        try:
            for _ in range(self._request_step_limit):
                self._update_elapsed(data)
                result = await self._step(data)
                if result is not None:
                    return result
            return self._fatal(data, "WORKFLOW_STEP_LIMIT_EXCEEDED")
        except InformationBoundaryViolation as error:
            data.terminal_codes.append("INFORMATION_DERIVATION_BOUNDARY_VIOLATION")
            return self._exception_result(data, str(error))
        except MemoryWriteIdentityCollision as error:
            return self._exception_result(data, "IDEMPOTENCY_IDENTITY_COLLISION", str(error))
        except (MemoryWriteWorkflowError, ValueError) as error:
            return self._exception_result(data, str(error))
        except Exception as error:
            return self._exception_result(
                data, "UNEXPECTED_WORKFLOW_FAILURE", type(error).__name__, str(error)
            )

    async def _initialize(
        self, request: MemoryWriteWorkflowRequest
    ) -> _WorkflowData | MemoryWriteWorkflowResult:
        if request.resume_checkpoint is None:
            data = _WorkflowData(request=request, artifacts=self._artifacts)
            data.started_at = self._clock.now()
            return data
        try:
            checkpoint = self._checkpoint.load(request.resume_checkpoint)
        except Exception as error:
            return MemoryWriteWorkflowResult(
                request_id=request.request_id,
                status=MemoryWriteWorkflowStatus.FATAL,
                workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                canonical_commit_accepted=False,
                base_commit=request.base_commit,
                terminal_codes=("CHECKPOINT_CORRUPT", str(error)),
                continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
            )
        if not _checkpoint_matches(request, checkpoint):
            return MemoryWriteWorkflowResult(
                request_id=request.request_id,
                status=MemoryWriteWorkflowStatus.FATAL,
                workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                canonical_commit_accepted=False,
                base_commit=request.base_commit,
                checkpoint_ref=request.resume_checkpoint,
                terminal_codes=("RESUME_BASIS_MISMATCH",),
                continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
            )
        if (
            checkpoint.workflow_phase is MemoryWriteWorkflowPhase.COMPLETE
            and checkpoint.terminal_result_ref is not None
        ):
            try:
                terminal = _read_model(
                    self._artifacts,
                    checkpoint.terminal_result_ref,
                    MemoryWriteWorkflowResult,
                )
            except Exception as error:
                return MemoryWriteWorkflowResult(
                    request_id=request.request_id,
                    status=MemoryWriteWorkflowStatus.FATAL,
                    workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                    canonical_commit_accepted=False,
                    base_commit=request.base_commit,
                    checkpoint_ref=request.resume_checkpoint,
                    terminal_codes=("TERMINAL_RESULT_CORRUPT", str(error)),
                    continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
                )
            if terminal.request_id != request.request_id:
                return MemoryWriteWorkflowResult(
                    request_id=request.request_id,
                    status=MemoryWriteWorkflowStatus.FATAL,
                    workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                    canonical_commit_accepted=False,
                    base_commit=request.base_commit,
                    checkpoint_ref=request.resume_checkpoint,
                    terminal_codes=("TERMINAL_RESULT_IDENTITY_MISMATCH",),
                    continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
                )
            terminal_result = cast(MemoryWriteWorkflowResult, terminal)
            return terminal_result.model_copy(update={"checkpoint_ref": request.resume_checkpoint})
        saved = self._checkpoint_states.get(request.resume_checkpoint.artifact_id)
        if saved is not None:
            saved.request = request
            saved.state = checkpoint.resume_state
            saved.recovered_inflight_proposal = (
                checkpoint.resume_state is MemoryWriteState.CURATE_ATTEMPT_EXECUTE
                and saved.inflight_proposal_attempt is not None
                and saved.inflight_proposal_attempt.status
                in {
                    CuratorProposalAttemptStatus.REQUESTED,
                    CuratorProposalAttemptStatus.RUNNING,
                    CuratorProposalAttemptStatus.UNCERTAIN,
                }
            )
            if saved.started_at is None:
                saved.started_at = self._clock.now()
            return saved
        data = _WorkflowData(
            request=request, state=checkpoint.resume_state, artifacts=self._artifacts
        )
        data.started_at = self._clock.now()
        data.checkpoint_ref = request.resume_checkpoint
        data.usage = checkpoint.budget_usage
        data.proposal_attempt_no = checkpoint.proposal_attempt_no
        data.proposal_attempt_refs = list(checkpoint.proposal_attempt_refs)
        data.proposal_rejection_refs = list(checkpoint.proposal_rejection_refs)
        data.proposal_feedback_ref = checkpoint.proposal_feedback_ref
        data.proposal_directive_ref = checkpoint.proposal_directive_ref
        if checkpoint.proposal_directive_ref is not None:
            data.proposal_directive = _read_model(
                self._artifacts,
                checkpoint.proposal_directive_ref,
                CuratorProposalRepairDirective,
            )
        data.proposal_budget_reservation_ref = checkpoint.proposal_budget_reservation_ref
        data.last_proposal_output_hash = checkpoint.last_proposal_output_hash
        data.last_proposal_rejection_signature = checkpoint.last_proposal_rejection_signature
        data.same_proposal_output_count = checkpoint.same_proposal_output_count
        data.same_proposal_rejection_count = checkpoint.same_proposal_rejection_count
        if checkpoint.inflight_proposal_attempt_id is not None:
            try:
                data.inflight_proposal_attempt = self._proposal_attempts.load(
                    checkpoint.inflight_proposal_attempt_id
                )
                data.recovered_inflight_proposal = (
                    checkpoint.resume_state is MemoryWriteState.CURATE_ATTEMPT_EXECUTE
                    and data.inflight_proposal_attempt.status
                    in {
                        CuratorProposalAttemptStatus.REQUESTED,
                        CuratorProposalAttemptStatus.RUNNING,
                        CuratorProposalAttemptStatus.UNCERTAIN,
                    }
                )
                if data.inflight_proposal_attempt.status is CuratorProposalAttemptStatus.ACCEPTED:
                    output_ref = _require(data.inflight_proposal_attempt.normalized_output_ref)
                    data.proposal_outcome = CuratorProposalAccepted(
                        observed_changes=_read_model(
                            self._artifacts,
                            output_ref,
                            ObservedChangeSet,
                        ),
                        attempt_receipt=data.inflight_proposal_attempt,
                    )
                elif data.inflight_proposal_attempt.status is CuratorProposalAttemptStatus.REJECTED:
                    rejection_ref = _require(data.inflight_proposal_attempt.rejection_ref)
                    rejection = _read_model(
                        self._artifacts,
                        rejection_ref,
                        CuratorProposalRejection,
                    )
                    if checkpoint.resume_state is not MemoryWriteState.PROPOSAL_VALIDATE:
                        data.proposal_rejections.append(rejection)
                    data.proposal_outcome = CuratorProposalRejected(
                        rejection=rejection,
                        attempt_receipt=data.inflight_proposal_attempt,
                    )
            except Exception as error:
                return MemoryWriteWorkflowResult(
                    request_id=request.request_id,
                    status=MemoryWriteWorkflowStatus.FATAL,
                    workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                    canonical_commit_accepted=False,
                    base_commit=request.base_commit,
                    checkpoint_ref=request.resume_checkpoint,
                    terminal_codes=("PROPOSAL_ATTEMPT_CORRUPT", str(error)),
                    continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
                )
        data.commit_effect_id = checkpoint.commit_effect_id
        data.projection_effect_id = checkpoint.projection_effect_id
        if checkpoint.commit_request_ref is not None:
            data.commit_request = _read_model(
                self._artifacts,
                checkpoint.commit_request_ref,
                DurableMemoryWriteCommitRequest,
            )
        if checkpoint.current_candidate_id is not None:
            data.candidate = self._lineage.get(checkpoint.current_candidate_id)
            if data.candidate is None:
                return MemoryWriteWorkflowResult(
                    request_id=request.request_id,
                    status=MemoryWriteWorkflowStatus.FATAL,
                    workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                    canonical_commit_accepted=False,
                    base_commit=request.base_commit,
                    checkpoint_ref=request.resume_checkpoint,
                    terminal_codes=("CANDIDATE_LINEAGE_MISSING",),
                    continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
                )
        if checkpoint.materialization_artifact is not None:
            try:
                data.materialization = _read_model(
                    self._artifacts,
                    checkpoint.materialization_artifact,
                    CandidateMaterialization,
                )
                data.bundle = data.materialization.bundle
            except Exception as error:
                return MemoryWriteWorkflowResult(
                    request_id=request.request_id,
                    status=MemoryWriteWorkflowStatus.FATAL,
                    workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                    canonical_commit_accepted=False,
                    base_commit=request.base_commit,
                    checkpoint_ref=request.resume_checkpoint,
                    terminal_codes=("MATERIALIZATION_CORRUPT", str(error)),
                    continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
                )
        if checkpoint.validation_artifact is not None:
            data.validation = _read_model(
                self._artifacts, checkpoint.validation_artifact, ValidationDecision
            )
        if checkpoint.risk_artifact is not None:
            data.risk = _read_model(self._artifacts, checkpoint.risk_artifact, PatchRiskAssessment)
        if checkpoint.guardian_artifact is not None:
            data.guardian = _read_model(
                self._artifacts, checkpoint.guardian_artifact, GuardianDecision
            )
        if checkpoint.approval_request_artifact is not None:
            data.approval_request = _read_model(
                self._artifacts, checkpoint.approval_request_artifact, HumanApprovalRequest
            )
        if checkpoint.proposal_human_request_artifact is not None:
            data.proposal_human_request = _read_model(
                self._artifacts,
                checkpoint.proposal_human_request_artifact,
                ProposalHumanReviewRequest,
            )
        if checkpoint.accepted_commit_id is not None:
            data.commit_result = MemoryWriteCommitResult(
                request_id=request.request_id,
                status=MemoryWriteCommitStatus.ACCEPTED,
                commit_id=checkpoint.accepted_commit_id,
                commit_receipt_ref=checkpoint.commit_receipt_ref,
            )
        if (
            checkpoint.projection_receipt_ref is not None
            and checkpoint.freshness_receipt_ref is not None
            and checkpoint.projection_snapshot_id is not None
        ):
            try:
                freshness = _read_model(
                    self._artifacts,
                    checkpoint.freshness_receipt_ref,
                    FreshnessDecision,
                )
                data.projection = ProjectionReadinessResult(
                    effect_id=checkpoint.projection_effect_id
                    or StableId(f"effect.projection.{request.request_id.root}"),
                    status=ProjectionReadinessStatus.READY,
                    projection_receipt_ref=checkpoint.projection_receipt_ref,
                    freshness_receipt_ref=checkpoint.freshness_receipt_ref,
                    projection_snapshot_id=checkpoint.projection_snapshot_id,
                    freshness=freshness,
                )
            except Exception:
                # A pending checkpoint can be resumed by the readiness port.  A
                # COMPLETE checkpoint is still protected by terminal_result_ref
                # above, so do not fabricate a successful projection here.
                data.projection = None
        if checkpoint.resume_state in {
            MemoryWriteState.NORMALIZE,
            MemoryWriteState.MATERIALIZE,
            MemoryWriteState.VALIDATE,
            MemoryWriteState.REPAIR_POLICY,
            MemoryWriteState.REFRESH_SOURCE_CONTEXT,
            MemoryWriteState.CURATOR_REPAIR,
            MemoryWriteState.CURATE_ATTEMPT_PREPARE,
            MemoryWriteState.CURATE_ATTEMPT_EXECUTE,
            MemoryWriteState.PROPOSAL_VALIDATE,
            MemoryWriteState.PROPOSAL_REPAIR_POLICY,
            MemoryWriteState.PROPOSAL_RETRY,
            MemoryWriteState.PROPOSAL_HUMAN_SUSPEND,
            MemoryWriteState.PROPOSAL_HUMAN_RESUME,
            MemoryWriteState.RISK_CLASSIFY,
            MemoryWriteState.GUARDIAN,
            MemoryWriteState.HUMAN_SUSPEND,
            MemoryWriteState.HUMAN_RESUME,
        }:
            data.basis = await _maybe_await(
                self._canonical_read.load_verified(request.project_id, request.base_commit)
            )
            self._boundary.verify_request_and_derivation_graph(request, data.basis)
        return data

    async def _step(self, data: _WorkflowData) -> MemoryWriteWorkflowResult | None:
        request = data.request
        if data.state is MemoryWriteState.LOAD_BASIS:
            data.basis = await _maybe_await(
                self._canonical_read.load_verified(request.project_id, request.base_commit)
            )
            self._boundary.verify_request_and_derivation_graph(request, data.basis)
            self._event(data, RunEventType.INFORMATION_BOUNDARY_VERIFIED)
            data.state = MemoryWriteState.PREPARE_CANDIDATE
            return None

        if data.state is MemoryWriteState.PREPARE_CANDIDATE:
            if request.world_mutation.mode == "curator_proposal":
                data.state = MemoryWriteState.CURATE
            elif request.world_mutation.mode == "trusted_candidate":
                data.candidate = self._candidate_from_trusted(
                    data, request.world_mutation.candidate_artifact
                )
                data.state = MemoryWriteState.NORMALIZE
                data.checkpoint_ref = self._save_checkpoint(
                    data,
                    phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                    resume_state=data.state,
                )
            else:
                data.candidate = self._new_candidate(
                    data,
                    ObservedChangeSet(
                        change_set_id=StableId(f"changes.empty.{request.request_id.root}"),
                        base_commit=request.base_commit,
                        source_artifact=_source_artifact(request),
                        operations=(),
                    ),
                    CandidateProducerKind.EMPTY_DELTA,
                )
                data.state = MemoryWriteState.MATERIALIZE
                data.checkpoint_ref = self._save_checkpoint(
                    data,
                    phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                    resume_state=data.state,
                )
            return None

        if data.state is MemoryWriteState.CURATE:
            data.state = MemoryWriteState.CURATE_ATTEMPT_PREPARE
            return None

        if data.state is MemoryWriteState.CURATE_ATTEMPT_PREPARE:
            if self._curator is None:
                return self._fatal(data, "CURATOR_PORT_UNAVAILABLE")
            if not self._proposal_budget_available(data):
                data.terminal_codes.append("CURATOR_PROPOSAL_BUDGET_EXHAUSTED")
                self._event(data, RunEventType.CURATOR_PROPOSAL_BUDGET_EXHAUSTED)
                return self._budget_stop(data)
            attempt_no = data.proposal_attempt_no + 1
            attempt_id = StableId(f"proposal-attempt.{request.request_id.root}.{attempt_no}")
            reservation_payload = {
                "workflow_request_id": request.request_id.root,
                "attempt_id": attempt_id.root,
                "attempt_no": attempt_no,
                "max_provider_calls": 1,
                "remaining_total_model_calls": (
                    request.budget.max_total_model_calls - data.usage.total_model_calls
                ),
                "remaining_tokens": request.budget.token_budget - data.usage.tokens_used,
            }
            data.proposal_budget_reservation_ref = self._artifacts.put(
                canonical_json_bytes(reservation_payload),
                "application/vnd.novel-agent.proposal-budget-reservation+json",
                SchemaVersion("0.1.0"),
            )
            receipt = requested_attempt(
                attempt_id=attempt_id,
                workflow_request_id=request.request_id,
                run_id=request.run_id,
                task_id=request.task_id,
                attempt_no=attempt_no,
                base_commit=request.base_commit,
                boundary_id=request.information_boundary.boundary_id,
                configuration_fingerprint=request.configuration_fingerprint,
                prompt_fingerprint=request.configuration_fingerprint,
            )
            attempt_ref = self._proposal_attempts.create_requested(receipt)
            data.proposal_attempt_no = attempt_no
            data.inflight_proposal_attempt = receipt
            data.proposal_attempt_refs.append(attempt_ref)
            data.usage = data.usage.model_copy(
                update={"curator_proposal_attempts": (data.usage.curator_proposal_attempts + 1)}
            )
            data.state = MemoryWriteState.CURATE_ATTEMPT_EXECUTE
            self._event(
                data,
                RunEventType.CURATOR_PROPOSAL_ATTEMPT_REQUESTED,
                artifact_refs=(attempt_ref,),
            )
            data.checkpoint_ref = self._save_checkpoint(
                data,
                phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                resume_state=data.state,
            )
            self._fault("attempt_checkpoint_committed", data)
            return None

        if data.state is MemoryWriteState.CURATE_ATTEMPT_EXECUTE:
            attempt = _require(data.inflight_proposal_attempt)
            if data.recovered_inflight_proposal:
                uncertain_ref = self._proposal_attempts.mark_uncertain(
                    attempt.attempt_id,
                    "process restarted without a terminal proposal receipt",
                )
                data.inflight_proposal_attempt = self._proposal_attempts.load(attempt.attempt_id)
                data.proposal_attempt_refs[-1] = uncertain_ref
                self._hold_uncertain_proposal_budget(data)
                data.state = MemoryWriteState.CURATE_ATTEMPT_PREPARE
                data.checkpoint_ref = self._save_checkpoint(
                    data,
                    phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                    resume_state=data.state,
                )
                return self._suspended(
                    data,
                    "CURATOR_PROPOSAL_ATTEMPT_UNCERTAIN",
                    "recovery will not resend an unresolved model request identity",
                )
            model_request_id = self._proposal_model_request_id(
                request.request_id,
                attempt.attempt_no,
            )
            running_ref = self._proposal_attempts.mark_running(
                attempt.attempt_id,
                model_request_id,
            )
            data.inflight_proposal_attempt = self._proposal_attempts.load(attempt.attempt_id)
            data.proposal_attempt_refs[-1] = running_ref
            try:
                outcome = await self._execute_proposal_attempt(
                    data,
                    CuratorProposalAttemptRequest(
                        request=request,
                        basis=_require(data.basis),
                        attempt_id=attempt.attempt_id,
                        attempt_no=attempt.attempt_no,
                        model_request_id=model_request_id,
                        source_artifacts=request.source_artifacts,
                        source_visibility_receipts=request.source_visibility_receipts,
                        budget_reservation_ref=_require(data.proposal_budget_reservation_ref),
                        feedback_artifact_ref=data.proposal_feedback_ref,
                        previous_rejection_ref=(
                            data.proposal_rejection_refs[-1]
                            if data.proposal_rejection_refs
                            else None
                        ),
                    ),
                )
            except CuratorProposalTransportError as error:
                uncertain_ref = self._proposal_attempts.mark_uncertain(
                    attempt.attempt_id,
                    str(error),
                )
                data.inflight_proposal_attempt = self._proposal_attempts.load(attempt.attempt_id)
                data.proposal_attempt_refs[-1] = uncertain_ref
                self._hold_uncertain_proposal_budget(data)
                data.state = MemoryWriteState.CURATE_ATTEMPT_PREPARE
                data.checkpoint_ref = self._save_checkpoint(
                    data,
                    phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                    resume_state=data.state,
                )
                return self._suspended(
                    data,
                    "CURATOR_PROPOSAL_TRANSPORT_UNAVAILABLE",
                    str(error),
                )
            except InformationBoundaryViolation:
                raise
            self._settle_proposal_outcome(data, outcome)
            data.proposal_outcome = outcome
            data.inflight_proposal_attempt = outcome.attempt_receipt
            data.state = MemoryWriteState.PROPOSAL_VALIDATE
            data.checkpoint_ref = self._save_checkpoint(
                data,
                phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                resume_state=data.state,
            )
            self._fault("provider_outcome_committed", data)
            self._fault(
                (
                    "accepted_attempt_committed"
                    if isinstance(outcome, CuratorProposalAccepted)
                    else "typed_rejection_committed"
                ),
                data,
            )
            return None

        if data.state is MemoryWriteState.PROPOSAL_VALIDATE:
            outcome = _require(data.proposal_outcome)
            if isinstance(outcome, CuratorProposalAccepted):
                data.candidate = self._new_candidate(
                    data,
                    outcome.observed_changes,
                    CandidateProducerKind.CURATOR_PROPOSE,
                    producer_receipt=outcome.attempt_receipt.producer_receipt_ref,
                    origin_proposal_attempt=outcome.attempt_receipt,
                    origin_proposal_attempt_ref=data.proposal_attempt_refs[-1],
                )
                data.state = MemoryWriteState.NORMALIZE
                self._event(data, RunEventType.CANDIDATE_PROPOSED)
                data.checkpoint_ref = self._save_checkpoint(
                    data,
                    phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                    resume_state=data.state,
                )
                self._fault("candidate_v1_committed", data)
                return None
            rejection = outcome.rejection
            data.proposal_rejections.append(rejection)
            data.usage = data.usage.model_copy(
                update={"curator_proposal_rejections": (data.usage.curator_proposal_rejections + 1)}
            )
            self._track_proposal_repetition(data, rejection)
            data.state = MemoryWriteState.PROPOSAL_REPAIR_POLICY
            data.checkpoint_ref = self._save_checkpoint(
                data,
                phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                resume_state=data.state,
            )
            self._fault("rejection_policy_checkpoint_committed", data)
            return None

        if data.state is MemoryWriteState.PROPOSAL_REPAIR_POLICY:
            if not data.proposal_rejections:
                return self._fatal(data, "PROPOSAL_REPAIR_WITHOUT_REJECTION")
            directive = self._proposal_policy.decide(
                rejection=data.proposal_rejections[-1],
                attempt_count=data.usage.curator_proposal_attempts,
                rejection_count=data.usage.curator_proposal_rejections,
                same_output_count=data.same_proposal_output_count,
                same_rejection_count=data.same_proposal_rejection_count,
                budget=request.budget,
                remaining=self._budget_remaining(data),
            )
            data.proposal_directive = directive
            directive_ref = _put_model(
                self._artifacts,
                directive,
                "application/vnd.novel-agent.curator-proposal-repair-directive+json",
                SchemaVersion("0.1.0"),
            )
            data.proposal_directive_ref = directive_ref
            if directive.action == "retry_with_feedback":
                data.state = MemoryWriteState.PROPOSAL_RETRY
            elif directive.action == "human_review":
                data.state = MemoryWriteState.PROPOSAL_HUMAN_SUSPEND
            elif directive.action == "quarantine":
                return self._quarantine_proposal(data, directive_ref)
            elif directive.action == "budget_stop":
                data.terminal_codes.append("CURATOR_PROPOSAL_BUDGET_EXHAUSTED")
                return self._proposal_budget_stop(data)
            else:
                return self._fatal(
                    data,
                    "CURATOR_PROPOSAL_INFORMATION_BOUNDARY"
                    if directive.action == "fatal"
                    else "UNSUPPORTED_PROPOSAL_REPAIR_DIRECTIVE",
                )
            return None

        if data.state is MemoryWriteState.PROPOSAL_RETRY:
            rejection = data.proposal_rejections[-1]
            directive = _require(data.proposal_directive)
            feedback = {
                "reason_code": rejection.reason_code,
                "rejection_signature": rejection.rejection_signature.root,
                "previous_output_hash": (
                    None if rejection.output_hash is None else rejection.output_hash.root
                ),
                "safe_feedback": rejection.safe_feedback,
                "mutable_operation_indexes": directive.scope.mutable_operation_indexes,
                "allow_complete_replacement": directive.scope.allow_complete_replacement,
                "immutable_operation_semantic_hashes": tuple(
                    item.root for item in directive.scope.immutable_operation_semantic_hashes
                ),
                "remaining_budget": self._budget_remaining(data).model_dump(mode="json"),
                "require_complete_replacement_json": True,
            }
            data.proposal_feedback_ref = self._artifacts.put(
                canonical_json_bytes(feedback),
                "application/vnd.novel-agent.curator-proposal-feedback+json",
                SchemaVersion("0.1.0"),
            )
            data.state = MemoryWriteState.CURATE_ATTEMPT_PREPARE
            self._event(
                data,
                RunEventType.CURATOR_PROPOSAL_RETRY_SCHEDULED,
                artifact_refs=(data.proposal_feedback_ref,),
            )
            data.checkpoint_ref = self._save_checkpoint(
                data,
                phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                resume_state=data.state,
            )
            self._fault("feedback_checkpoint_committed", data)
            return None

        if data.state is MemoryWriteState.PROPOSAL_HUMAN_SUSPEND:
            return self._proposal_human_required(data)

        if data.state is MemoryWriteState.PROPOSAL_HUMAN_RESUME:
            return self._resume_proposal_human(data)

        if data.state is MemoryWriteState.NORMALIZE:
            if data.candidate is None:
                return self._fatal(data, "NORMALIZE_WITHOUT_CANDIDATE")
            if data.usage.normalization_passes >= request.budget.max_normalization_passes:
                return self._budget_stop(data)
            data.usage = data.usage.model_copy(
                update={"normalization_passes": data.usage.normalization_passes + 1}
            )
            normalizer = self._normalizer
            if normalizer is None:
                return self._fatal(data, "NORMALIZER_PORT_UNAVAILABLE")
            normalized = await _maybe_await(
                normalizer.normalize(data.candidate, _require(data.basis), data.directive)
            )
            result = _as_normalization_result(normalized, data.candidate)
            if (
                result.status is NormalizationStatus.TRANSFORMED
                and result.candidate != data.candidate
            ):
                if not self._candidate_budget_available(data):
                    return self._budget_stop(data)
                data.candidate = self._persist_candidate(data, result.candidate)
                self._event(data, RunEventType.CANDIDATE_NORMALIZED)
            elif result.status is NormalizationStatus.AMBIGUOUS:
                data.terminal_codes.extend(result.reason_codes)
            data.directive = None
            data.state = MemoryWriteState.MATERIALIZE
            data.checkpoint_ref = self._save_checkpoint(
                data,
                phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                resume_state=data.state,
            )
            return None

        if data.state is MemoryWriteState.MATERIALIZE:
            if data.candidate is None:
                return self._fatal(data, "MATERIALIZE_WITHOUT_CANDIDATE")
            try:
                root_updates = self._root_updates
                if root_updates is None:
                    return self._fatal(data, "ROOT_UPDATE_PORT_UNAVAILABLE")
                materialized = await _maybe_await(
                    root_updates.materialize_atomic_bundle(
                        candidate=data.candidate,
                        basis=_require(data.basis),
                    )
                )
            except Exception as error:
                return self._fatal(data, f"MATERIALIZATION_FAILED:{error}")
            root_result = _as_materialization_result(materialized)
            data.materialization = root_result.materialization
            data.bundle = root_result.bundle
            data.world_mutation_noop = root_result.world_mutation_noop
            self._materializations[data.candidate.candidate_id] = root_result.materialization
            data.state = MemoryWriteState.VALIDATE
            self._event(data, RunEventType.ROOT_UPDATE_MATERIALIZED)
            return None

        if data.state is MemoryWriteState.VALIDATE:
            if data.candidate is None or data.materialization is None:
                return self._fatal(data, "VALIDATE_WITHOUT_MATERIALIZATION")
            validation = await _maybe_await(
                self._validator.validate(data.candidate, data.materialization, _require(data.basis))
            )
            if not isinstance(validation, ValidationDecision):
                raise MemoryWriteWorkflowError("ValidationPort returned a non-v2 decision")
            if (
                validation.candidate_id != data.candidate.candidate_id
                or validation.candidate_content_hash != data.candidate.content_hash
            ):
                return self._fatal(data, "VALIDATION_CANDIDATE_BINDING_MISMATCH")
            if (
                validation.base_commit != data.request.base_commit
                or validation.materialization_receipt
                != data.materialization.materialization_receipt
            ):
                return self._fatal(data, "VALIDATION_BASIS_BINDING_MISMATCH")
            if validation.proposed_roots_hash != data.materialization.proposed_roots_hash:
                return self._fatal(data, "VALIDATION_MATERIALIZATION_BINDING_MISMATCH")
            data.validation = validation
            self._validations[data.candidate.candidate_id] = validation
            self._event(data, RunEventType.CANDIDATE_VALIDATED)
            data.state = (
                MemoryWriteState.RISK_CLASSIFY
                if validation.disposition is ValidationDisposition.PASS
                else MemoryWriteState.REPAIR_POLICY
            )
            if validation.disposition is ValidationDisposition.NON_REPAIRABLE:
                data.terminal_codes.extend(item.code for item in validation.findings)
            data.checkpoint_ref = self._save_checkpoint(
                data,
                phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                resume_state=data.state,
            )
            self._fault("candidate_validation_committed", data)
            return None

        if data.state is MemoryWriteState.REPAIR_POLICY:
            directive = self._repair_policy.decide(self._repair_context(data))
            data.directive = directive
            data.action_receipts.append(
                RepairActionReceipt(
                    receipt_id=StableId(f"repair-receipt.{directive.directive_id.root}"),
                    action=directive.action,
                    directive_id=directive.directive_id,
                    candidate_id=_candidate_id(data),
                    reason_codes=directive.reason_codes,
                )
            )
            self._event(data, RunEventType.REPAIR_DECIDED)
            data.state = {
                RepairAction.DETERMINISTIC_REPAIR: MemoryWriteState.NORMALIZE,
                RepairAction.CURATOR_REPAIR: MemoryWriteState.CURATOR_REPAIR,
                RepairAction.GUARDIAN_REVIEW: MemoryWriteState.GUARDIAN,
                RepairAction.RETRY_AFTER_SOURCE_CONTEXT_REFRESH: (
                    MemoryWriteState.REFRESH_SOURCE_CONTEXT
                ),
                RepairAction.HUMAN: MemoryWriteState.HUMAN_SUSPEND,
                RepairAction.QUARANTINE_OPERATION: MemoryWriteState.QUARANTINE,
                RepairAction.STOP_BUDGET_EXHAUSTED: MemoryWriteState.BUDGET_STOP,
                RepairAction.REPLAN: MemoryWriteState.STOP,
                RepairAction.STOP_FATAL: MemoryWriteState.STOP,
            }[directive.action]
            return None

        if data.state is MemoryWriteState.REFRESH_SOURCE_CONTEXT:
            if data.usage.context_refreshes >= request.budget.max_context_refreshes:
                return self._budget_stop(data)
            data.usage = data.usage.model_copy(
                update={"context_refreshes": data.usage.context_refreshes + 1}
            )
            self._boundary.verify_request_and_derivation_graph(request, _require(data.basis))
            data.directive = None
            data.state = MemoryWriteState.REPAIR_POLICY
            return None

        if data.state is MemoryWriteState.CURATOR_REPAIR:
            if self._curator is None or data.candidate is None:
                return self._fatal(data, "CURATOR_REPAIR_PORT_UNAVAILABLE")
            if not self._candidate_budget_available(data):
                return self._budget_stop(data)
            if not self._reserve(data, "curator.repair", "curator_repairs"):
                return self._budget_stop(data)
            data.usage = data.usage.model_copy(
                update={"curator_repairs": data.usage.curator_repairs + 1}
            )
            if data.directive is None:
                return self._fatal(data, "CURATOR_REPAIR_WITHOUT_DIRECTIVE")
            try:
                result = await _maybe_await(
                    self._curator.repair(
                        CuratorRepairRequest(
                            request=request,
                            basis=_require(data.basis),
                            parent_candidate=data.candidate,
                            validation=data.validation,
                            guardian=data.guardian,
                            directive=data.directive,
                            source_artifacts=request.source_artifacts,
                            source_visibility_receipts=request.source_visibility_receipts,
                        )
                    )
                )
            except CuratorRepairRejectedError as error:
                data.terminal_codes.append(error.reason_code)
                data.directive = None
                data.state = MemoryWriteState.REPAIR_POLICY
                data.checkpoint_ref = self._save_checkpoint(
                    data,
                    phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                    resume_state=data.state,
                )
                return None
            repair = _as_repair_result(result)
            self._settle_model(data, repair.token_usage, repair.transport_attempts)
            child = self._new_candidate(
                data,
                repair.observed_changes,
                CandidateProducerKind.CURATOR_REPAIR,
                parent=data.candidate,
                producer_receipt=repair.producer_receipt,
                directive=data.directive,
            )
            data.candidate = child
            data.directive = None
            data.state = MemoryWriteState.NORMALIZE
            self._event(data, RunEventType.CANDIDATE_REPAIRED)
            data.checkpoint_ref = self._save_checkpoint(
                data,
                phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                resume_state=data.state,
            )
            return None

        if data.state is MemoryWriteState.RISK_CLASSIFY:
            data.risk = self._assess_risk(data)
            data.state = (
                MemoryWriteState.GUARDIAN
                if data.risk.requires_guardian
                else MemoryWriteState.PRECOMMIT
            )
            return None

        if data.state is MemoryWriteState.GUARDIAN:
            if data.candidate is None or data.validation is None or data.risk is None:
                return self._fatal(data, "GUARDIAN_WITHOUT_VALIDATED_CANDIDATE")
            if self._guardian is None:
                return self._human_required(data, "GUARDIAN_PORT_UNAVAILABLE")
            if not self._reserve(data, "guardian.review", "guardian_reviews"):
                return self._budget_stop(data)
            data.usage = data.usage.model_copy(
                update={"guardian_reviews": data.usage.guardian_reviews + 1}
            )
            self._event(data, RunEventType.GUARDIAN_REQUESTED)
            reviewed = await _maybe_await(
                self._guardian.review(
                    GuardianReviewRequest(
                        request=request,
                        basis=_require(data.basis),
                        candidate=data.candidate,
                        validation=data.validation,
                        risk=data.risk,
                    )
                )
            )
            guardian_result = _as_guardian_result(reviewed)
            self._settle_model(
                data,
                guardian_result.token_usage,
                guardian_result.transport_attempts,
            )
            data.guardian = guardian_result.decision
            self._event(data, RunEventType.GUARDIAN_COMPLETED)
            if data.guardian.outcome is GuardianOutcome.APPROVE:
                data.state = MemoryWriteState.PRECOMMIT
            elif data.guardian.outcome is GuardianOutcome.REVISE:
                data.state = MemoryWriteState.REPAIR_POLICY
            elif data.guardian.outcome is GuardianOutcome.HUMAN_REVIEW:
                data.state = MemoryWriteState.HUMAN_SUSPEND
            else:
                data.state = MemoryWriteState.QUARANTINE
            data.checkpoint_ref = self._save_checkpoint(
                data,
                phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                resume_state=data.state,
            )
            return None

        if data.state is MemoryWriteState.HUMAN_SUSPEND:
            return self._suspend_for_human(data)

        if data.state is MemoryWriteState.HUMAN_RESUME:
            return self._resume_human(data)

        if data.state is MemoryWriteState.PRECOMMIT:
            return self._prepare_commit(data)

        if data.state is MemoryWriteState.COMMIT:
            return self._commit(data)

        if data.state is MemoryWriteState.PROJECT:
            return self._project(data)

        if data.state is MemoryWriteState.FRESHNESS_GATE:
            return self._freshness(data)

        if data.state is MemoryWriteState.QUARANTINE:
            return self._quarantine_candidate(data)

        if data.state is MemoryWriteState.BUDGET_STOP:
            return self._budget_stop(data)

        if data.state is MemoryWriteState.STOP:
            if data.directive is not None and data.directive.action is RepairAction.REPLAN:
                return self._replan(data, data.directive.reason_codes)
            return self._fatal(data, *(data.terminal_codes or ["WORKFLOW_STOPPED"]))

        if data.state is MemoryWriteState.COMPLETE:
            return self._complete(data)
        return self._fatal(data, "UNKNOWN_WORKFLOW_STATE")

    async def _execute_proposal_attempt(
        self,
        data: _WorkflowData,
        attempt_request: CuratorProposalAttemptRequest,
    ) -> CuratorProposalAccepted | CuratorProposalRejected:
        curator = self._curator
        if curator is None:
            raise MemoryWriteWorkflowError("Curator port is unavailable")
        propose_attempt = getattr(curator, "propose_attempt", None)
        if not callable(propose_attempt):
            raise MemoryWriteWorkflowError("Curator port must implement typed propose_attempt")
        outcome = await _maybe_await(propose_attempt(attempt_request))
        if not isinstance(outcome, (CuratorProposalAccepted, CuratorProposalRejected)):
            raise MemoryWriteWorkflowError("Curator propose_attempt returned an untyped outcome")
        return outcome

    def _settle_proposal_outcome(
        self,
        data: _WorkflowData,
        outcome: CuratorProposalAccepted | CuratorProposalRejected,
    ) -> None:
        receipt = outcome.attempt_receipt
        if receipt.workflow_request_id != data.request.request_id:
            raise MemoryWriteIdentityCollision("proposal outcome belongs to another workflow")
        if isinstance(outcome, CuratorProposalAccepted):
            ref = self._proposal_attempts.settle_accepted(receipt.attempt_id, receipt)
        else:
            ref = self._proposal_attempts.settle_rejected(
                receipt.attempt_id,
                outcome.rejection,
                receipt,
            )
            data.proposal_rejection_refs.append(_require(receipt.rejection_ref))
        data.proposal_attempt_refs[-1] = ref
        data.usage = data.usage.model_copy(
            update={
                "structured_generation_attempts": (
                    data.usage.structured_generation_attempts + len(receipt.model_request_ids)
                ),
                "total_model_calls": (data.usage.total_model_calls + receipt.provider_call_count),
                "transport_attempts": (
                    data.usage.transport_attempts + receipt.transport_attempt_count
                ),
                "tokens_used": (
                    data.usage.tokens_used + receipt.input_tokens + receipt.output_tokens
                ),
            }
        )
        self._event(
            data,
            RunEventType.CURATOR_PROPOSAL_ATTEMPT_COMPLETED,
            artifact_refs=(ref,),
        )
        if isinstance(outcome, CuratorProposalRejected):
            self._event(
                data,
                (
                    RunEventType.CURATOR_PROPOSAL_SCHEMA_REJECTED
                    if outcome.rejection.stage is ProposalRejectionStage.STRUCTURED_SCHEMA
                    else RunEventType.CURATOR_PROPOSAL_SEMANTIC_REJECTED
                ),
                artifact_refs=(_require(receipt.rejection_ref),),
            )

    @staticmethod
    def _track_proposal_repetition(
        data: _WorkflowData,
        rejection: CuratorProposalRejection,
    ) -> None:
        data.same_proposal_output_count = (
            data.same_proposal_output_count + 1
            if rejection.output_hash is not None
            and rejection.output_hash == data.last_proposal_output_hash
            else 1
        )
        data.same_proposal_rejection_count = (
            data.same_proposal_rejection_count + 1
            if rejection.rejection_signature == data.last_proposal_rejection_signature
            else 1
        )
        data.last_proposal_output_hash = rejection.output_hash
        data.last_proposal_rejection_signature = rejection.rejection_signature

    @staticmethod
    def _proposal_model_request_id(
        request_id: StableId,
        attempt_no: int,
    ) -> StableId:
        suffix = f".proposal-{attempt_no}.schema-1"
        return StableId(request_id.root[: 128 - len(suffix)] + suffix)

    def _proposal_budget_available(self, data: _WorkflowData) -> bool:
        self._update_elapsed(data)
        budget = data.request.budget
        usage = data.usage
        return not (
            usage.curator_proposal_attempts >= budget.max_curator_proposal_attempts
            or usage.curator_proposal_rejections >= budget.max_curator_proposal_rejections
            or usage.total_model_calls >= budget.max_total_model_calls
            or usage.tokens_used >= budget.token_budget
            or usage.elapsed_ms >= budget.wall_clock_budget_ms
        )

    @staticmethod
    def _hold_uncertain_proposal_budget(data: _WorkflowData) -> None:
        """Charge the reserved call once when provider completion is unknowable."""

        data.usage = data.usage.model_copy(
            update={
                "structured_generation_attempts": (data.usage.structured_generation_attempts + 1),
                "total_model_calls": data.usage.total_model_calls + 1,
                "transport_attempts": data.usage.transport_attempts + 1,
            }
        )

    @staticmethod
    def _budget_remaining(data: _WorkflowData) -> Any:
        return _remaining(data.request.budget, data.usage)

    def _proposal_budget_stop(self, data: _WorkflowData) -> MemoryWriteWorkflowResult:
        self._event(data, RunEventType.CURATOR_PROPOSAL_BUDGET_EXHAUSTED)
        data.checkpoint_ref = self._save_checkpoint(
            data,
            phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            resume_state=MemoryWriteState.PROPOSAL_REPAIR_POLICY,
        )
        return self._budget_result(data)

    def _quarantine_proposal(
        self,
        data: _WorkflowData,
        directive_ref: ArtifactRef,
    ) -> MemoryWriteWorkflowResult:
        package = QuarantinePackage(
            package_id=_bounded_workflow_id(
                "quarantine.proposal",
                data.request.request_id.root,
            ),
            request_id=data.request.request_id,
            base_commit=data.request.base_commit,
            candidate_ids=(),
            source_artifacts=data.request.source_artifacts,
            repair_directive_refs=(directive_ref,),
            proposal_attempt_refs=tuple(data.proposal_attempt_refs),
            proposal_rejection_refs=tuple(data.proposal_rejection_refs),
            proposal_feedback_ref=data.proposal_feedback_ref,
            current_project_commit=_current_commit(data),
            configuration_fingerprint=data.request.configuration_fingerprint,
            terminal_reason="curator proposal poison loop or policy quarantine",
            recommended_action="review rejected proposal attempts before retry",
        )
        ref = self._quarantine.persist(package)
        data.quarantine_refs.append(ref)
        data.terminal_codes.append("CURATOR_PROPOSAL_POISON_LOOP")
        self._event(
            data,
            RunEventType.CURATOR_PROPOSAL_POISON_LOOP,
            artifact_refs=(ref,),
        )
        data.checkpoint_ref = self._save_checkpoint(
            data,
            phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            resume_state=MemoryWriteState.PROPOSAL_REPAIR_POLICY,
        )
        return self._quarantined(data)

    def _proposal_human_required(
        self,
        data: _WorkflowData,
    ) -> MemoryWriteWorkflowResult:
        rejection = data.proposal_rejections[-1]
        latest_draft = rejection.raw_draft_ref
        if latest_draft is None and data.inflight_proposal_attempt is not None:
            raw_refs = data.inflight_proposal_attempt.raw_response_refs
            latest_draft = raw_refs[-1] if raw_refs else None
        if latest_draft is None:
            return self._fatal(data, "PROPOSAL_HUMAN_DRAFT_MISSING")
        if data.proposal_human_request is None:
            data.proposal_human_request = ProposalHumanReviewRequest(
                approval_request_id=StableId(f"proposal-approval.{data.request.request_id.root}"),
                workflow_request_id=data.request.request_id,
                base_commit=data.request.base_commit,
                proposal_attempt_refs=tuple(data.proposal_attempt_refs),
                latest_rejected_draft_ref=latest_draft,
                latest_rejection_ref=data.proposal_rejection_refs[-1],
                safe_feedback=rejection.safe_feedback,
                created_at=self._clock.now(),
            )
            if self._proposal_human is not None:
                self._proposal_human.request(data.proposal_human_request)
        data.terminal_codes.append("CURATOR_PROPOSAL_HUMAN_REQUIRED")
        self._event(data, RunEventType.CURATOR_PROPOSAL_HUMAN_REQUIRED)
        request_ref = _put_model(
            self._artifacts,
            data.proposal_human_request,
            "application/vnd.novel-agent.proposal-human-review-request+json",
            SchemaVersion("0.1.0"),
        )
        data.checkpoint_ref = self._save_checkpoint(
            data,
            phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            resume_state=MemoryWriteState.PROPOSAL_HUMAN_RESUME,
            proposal_human_request_artifact=request_ref,
        )
        return self._human_required(data, "CURATOR_PROPOSAL_HUMAN_REQUIRED")

    def _resume_proposal_human(
        self,
        data: _WorkflowData,
    ) -> MemoryWriteWorkflowResult | None:
        review = data.proposal_human_request
        if self._proposal_human is None or review is None:
            return self._proposal_human_required(data)
        decision = self._proposal_human.read_decision(review.approval_request_id)
        if inspect.isawaitable(decision):
            raise MemoryWriteWorkflowError(
                "ProposalHumanReviewPort.read_decision must be synchronous"
            )
        if decision is None:
            return self._proposal_human_required(data)
        if (
            decision.approval_request_id != review.approval_request_id
            or decision.workflow_request_id != data.request.request_id
            or decision.base_commit != data.request.base_commit
        ):
            return self._fatal(data, "PROPOSAL_HUMAN_DECISION_BINDING_MISMATCH")
        if decision.kind is ProposalHumanDecisionKind.RETRY:
            data.state = MemoryWriteState.PROPOSAL_RETRY
            return None
        if decision.kind is ProposalHumanDecisionKind.REJECT:
            directive_ref = _put_model(
                self._artifacts,
                _require(data.proposal_directive),
                "application/vnd.novel-agent.curator-proposal-repair-directive+json",
                SchemaVersion("0.1.0"),
            )
            return self._quarantine_proposal(data, directive_ref)
        data.terminal_codes.append("PROPOSAL_TRUSTED_REPLACEMENT_VALIDATION_REQUIRED")
        return self._proposal_human_required(data)

    def _candidate_from_trusted(
        self, data: _WorkflowData, artifact: ArtifactRef
    ) -> CandidateRevision:
        payload = _read_model(self._artifacts, artifact, MemoryWriteCandidatePayload)
        if (
            payload.root_update_intents != data.request.root_update_intents
            or payload.commit_profile is not data.request.commit_profile
        ):
            raise InformationBoundaryViolation("trusted candidate payload differs from request")
        trusted_input = data.request.world_mutation
        if not isinstance(trusted_input, TrustedWorldCandidateInput):
            raise InformationBoundaryViolation("trusted candidate input is missing")
        return self._new_candidate(
            data,
            payload.observed_changes,
            CandidateProducerKind.TRUSTED_CANDIDATE,
            producer_receipt=trusted_input.producer_receipt,
            candidate_artifact=artifact,
        )

    def _new_candidate(
        self,
        data: _WorkflowData,
        changes: ObservedChangeSet,
        producer_kind: CandidateProducerKind,
        *,
        parent: CandidateRevision | None = None,
        producer_receipt: ArtifactRef | None = None,
        directive: RepairDirective | None = None,
        candidate_artifact: ArtifactRef | None = None,
        origin_proposal_attempt: CuratorProposalAttemptReceipt | None = None,
        origin_proposal_attempt_ref: ArtifactRef | None = None,
    ) -> CandidateRevision:
        if changes.base_commit != data.request.base_commit:
            raise InformationBoundaryViolation("candidate changed the request base commit")
        if data.usage.candidate_revisions >= data.request.budget.max_candidate_revisions:
            raise MemoryWriteWorkflowError("candidate revision budget exhausted")
        payload = MemoryWriteCandidatePayload(
            observed_changes=changes,
            root_update_intents=data.request.root_update_intents,
            commit_profile=data.request.commit_profile,
        )
        raw = canonical_json_bytes(payload.model_dump(mode="json"))
        artifact = candidate_artifact or self._artifacts.put(
            raw,
            "application/vnd.novel-agent.memory-write-candidate+json",
            data.request.canonical_root_refs.schema_version,
        )
        content_hash = sha256_id(raw)
        revision_no = 1 if parent is None else parent.revision_no + 1
        candidate_id = StableId(
            f"candidate.{data.request.request_id.root}.{revision_no}.{content_hash.root[7:23]}"
        )
        candidate = CandidateRevision(
            candidate_id=candidate_id,
            parent_candidate_id=None if parent is None else parent.candidate_id,
            revision_no=revision_no,
            base_commit=data.request.base_commit,
            basis_hash=_request_basis_hash(data.request),
            candidate_artifact=artifact,
            source_artifacts=data.request.source_artifacts,
            producer_kind=producer_kind,
            producer_receipt=self._candidate_producer_receipt(
                data,
                artifact,
                parent,
                producer_receipt,
                producer_kind,
            ),
            origin_proposal_attempt_id=(
                None if origin_proposal_attempt is None else origin_proposal_attempt.attempt_id
            ),
            origin_proposal_attempt_receipt=origin_proposal_attempt_ref,
            proposal_attempt_chain_refs=(
                () if origin_proposal_attempt is None else tuple(data.proposal_attempt_refs)
            ),
            repair_scope=None if directive is None else directive.allowed_scope,
            applied_directive_ids=() if directive is None else (directive.directive_id,),
            supersedes_candidate_id=None if parent is None else parent.candidate_id,
            content_hash=content_hash,
            created_at=self._clock.now(),
        )
        return self._persist_candidate(data, candidate)

    def _persist_candidate(
        self, data: _WorkflowData, candidate: CandidateRevision
    ) -> CandidateRevision:
        parent = None
        if candidate.parent_candidate_id is not None:
            parent = self._lineage.get(candidate.parent_candidate_id)
            if parent is None:
                raise MemoryWriteWorkflowError("candidate parent is missing")
        if candidate.producer_receipt is None or not candidate.producer_receipt.media_type.endswith(
            "boundary-propagation-receipt+json"
        ):
            candidate = candidate.model_copy(
                update={
                    "producer_receipt": self._candidate_producer_receipt(
                        data,
                        candidate.candidate_artifact,
                        parent,
                        candidate.producer_receipt,
                        candidate.producer_kind,
                    )
                }
            )
        if candidate.producer_receipt is None:
            raise InformationBoundaryViolation("candidate is missing a propagation receipt")
        self._boundary.verify_derivation_chain(
            artifact=candidate.candidate_artifact,
            producer_receipt=candidate.producer_receipt,
            boundary=data.request.information_boundary,
            configuration_fingerprint=data.request.configuration_fingerprint,
        )
        persisted = self._lineage.persist(candidate)
        data.usage = data.usage.model_copy(
            update={
                "candidate_revisions": max(data.usage.candidate_revisions, persisted.revision_no)
            }
        )
        if persisted.content_hash not in data.seen_content_hashes:
            data.seen_content_hashes.append(persisted.content_hash)
        return persisted

    def _candidate_producer_receipt(
        self,
        data: _WorkflowData,
        output: ArtifactRef,
        parent: CandidateRevision | None,
        original_receipt: ArtifactRef | None,
        producer_kind: CandidateProducerKind,
    ) -> ArtifactRef | None:
        if producer_kind is CandidateProducerKind.TRUSTED_CANDIDATE:
            return original_receipt
        register = getattr(self._boundary, "register_derivation", None)
        if not callable(register):
            if original_receipt is None:
                return None
            self._boundary.verify_derivation_chain(
                artifact=output,
                producer_receipt=original_receipt,
                boundary=data.request.information_boundary,
                configuration_fingerprint=data.request.configuration_fingerprint,
            )
            return original_receipt

        input_sources = tuple(data.request.source_artifacts)
        visibility_receipts = tuple(
            _visibility_ref(item) for item in data.request.source_visibility_receipts
        )
        derivation_receipts = [
            intent.producer_receipt for intent in data.request.root_update_intents
        ]
        positions: list[NarrativePosition] = [
            item.visible_through
            for item in data.request.source_visibility_receipts
            if item.visible_through is not None
        ]
        scopes = [item.access_scope for item in data.request.source_visibility_receipts]
        for receipt_ref in tuple(derivation_receipts):
            receipt = self._read_derivation_receipt(receipt_ref)
            if receipt.effective_visible_through is not None:
                positions.append(receipt.effective_visible_through)
            scopes.append(receipt.effective_access_scope)
        if parent is not None:
            if parent.producer_receipt is None:
                raise InformationBoundaryViolation(
                    "candidate revision parent is missing a propagation receipt"
                )
            derivation_receipts.append(parent.producer_receipt)
            parent_receipt = self._read_derivation_receipt(parent.producer_receipt)
            if parent_receipt.effective_visible_through is not None:
                positions.append(parent_receipt.effective_visible_through)
            scopes.append(parent_receipt.effective_access_scope)
        unique_derivations: dict[ArtifactId, ArtifactRef] = {}
        for item in derivation_receipts:
            unique_derivations.setdefault(item.artifact_id, item)
        derivation_receipts = list(unique_derivations.values())
        if not input_sources and not derivation_receipts:
            # A pure empty-delta request has no information-bearing input.  It
            # remains auditable through the request/basis artifacts and does
            # not invent a synthetic visibility leaf.
            return None
        effective_position = _earliest_position([item for item in positions if item is not None])
        effective_scope = _narrowest_scope((*scopes, data.request.access_scope))
        receipt = BoundaryPropagationReceipt(
            receipt_id=StableId(f"candidate-propagation.{output.artifact_id.root[7:31]}"),
            boundary_id=data.request.information_boundary.boundary_id,
            base_commit=data.request.base_commit,
            input_source_artifact_refs=input_sources,
            source_visibility_receipt_refs=visibility_receipts,
            input_derivation_receipt_refs=tuple(derivation_receipts),
            output_artifact_hash=output.artifact_id,
            builder_policy_hash=data.request.configuration_fingerprint,
            effective_visible_through=effective_position,
            effective_access_scope=effective_scope,
            receipt_hash=ArtifactId("sha256:" + "1" * 64),
        )
        payload = receipt.model_dump(mode="json")
        payload["receipt_hash"] = None
        receipt = receipt.model_copy(
            update={"receipt_hash": sha256_id(canonical_json_bytes(payload))}
        )
        receipt_ref = self._artifacts.put(
            canonical_json_bytes(receipt.model_dump(mode="json")),
            "application/vnd.novel-agent.boundary-propagation-receipt+json",
            output.schema_version,
        )
        registered = register(
            receipt,
            receipt_artifact=receipt_ref,
            output_artifact=output,
        )
        if not isinstance(registered, ArtifactRef):
            raise InformationBoundaryViolation(
                "boundary port returned an invalid receipt reference"
            )
        self._boundary.verify_derivation_chain(
            artifact=output,
            producer_receipt=registered,
            boundary=data.request.information_boundary,
            configuration_fingerprint=data.request.configuration_fingerprint,
        )
        return registered

    def _read_derivation_receipt(self, reference: ArtifactRef) -> BoundaryPropagationReceipt:
        reader = getattr(self._boundary, "read_derivation_receipt", None)
        if not callable(reader):
            raise InformationBoundaryViolation(
                "boundary port cannot read parent propagation receipts"
            )
        receipt = reader(reference)
        if not isinstance(receipt, BoundaryPropagationReceipt):
            raise InformationBoundaryViolation(
                "boundary port returned an invalid propagation receipt"
            )
        return receipt

    def _assess_risk(self, data: _WorkflowData) -> PatchRiskAssessment:
        if self._risk_classifier is not None:
            result = self._risk_classifier.assess(data.candidate, data.validation)
            if not isinstance(result, PatchRiskAssessment):
                raise MemoryWriteWorkflowError("risk classifier returned an invalid assessment")
            return result
        change_set_id = _require(data.candidate).candidate_id
        findings = () if data.validation is None else data.validation.findings
        requires_guardian = any(
            finding.requires_guardian or finding.severity.value in {"critical", "warning"}
            for finding in findings
        )
        return PatchRiskAssessment(
            assessment_id=StableId(f"risk.{change_set_id.root}"),
            change_set_id=change_set_id,
            base_commit=data.request.base_commit,
            level=PatchRiskLevel.HIGH if requires_guardian else PatchRiskLevel.LOW,
            risk_codes=("VALIDATION_REVIEW",) if requires_guardian else (),
            requires_guardian=requires_guardian,
            requires_human_review=False,
        )

    def _repair_context(self, data: _WorkflowData) -> RepairContext:
        budget = data.request.budget
        usage = data.usage
        return RepairContext(
            request_id=data.request.request_id,
            candidate=_require(data.candidate),
            validation=data.validation,
            risk=data.risk,
            guardian=data.guardian,
            gate=data.gate,
            budget_remaining=_remaining(budget, usage),
            prior_actions=tuple(data.action_receipts),
            repeated_content_hashes=tuple(data.seen_content_hashes),
            current_canonical_commit=_current_commit(data),
        )

    def _prepare_commit(self, data: _WorkflowData) -> MemoryWriteWorkflowResult | None:
        try:
            current = awaitable_result(self._canonical_read.current_commit(data.request.project_id))
        except Exception:
            current = data.request.base_commit
        if current != data.request.base_commit:
            data.directive = RepairDirective(
                directive_id=StableId(f"repair.replan.{data.request.request_id.root}"),
                action=RepairAction.REPLAN,
                reason_codes=("BASE_COMMIT_MISMATCH",),
                checkpoint_required=True,
            )
            data.state = MemoryWriteState.STOP
            return None
        if data.candidate is None or data.materialization is None or data.validation is None:
            return self._fatal(data, "PRECOMMIT_INCOMPLETE")
        if data.validation.disposition is not ValidationDisposition.PASS:
            return self._fatal(data, "PRECOMMIT_VALIDATION_NOT_PASS")
        if data.bundle is None:
            return self._fatal(data, "PRECOMMIT_BUNDLE_MISSING")
        if data.risk is None:
            return self._fatal(data, "PRECOMMIT_RISK_MISSING")
        data.gate = self._gate(data)
        if (
            data.gate.change_set_id != data.candidate.candidate_id
            or data.gate.base_commit != data.request.base_commit
            or data.gate.risk_assessment_id != data.risk.assessment_id
        ):
            return self._fatal(data, "WRITE_GATE_BINDING_MISMATCH")
        if data.gate.outcome is not WriteGateOutcome.ALLOW_COMMIT:
            data.directive = RepairDirective(
                directive_id=StableId(f"repair.gate.{data.request.request_id.root}"),
                action=RepairAction.HUMAN
                if data.gate.outcome is WriteGateOutcome.REQUIRE_HUMAN
                else RepairAction.STOP_FATAL,
                reason_codes=data.gate.reasons or (data.gate.outcome.value,),
                checkpoint_required=data.gate.outcome is WriteGateOutcome.REQUIRE_HUMAN,
            )
            data.state = (
                MemoryWriteState.HUMAN_SUSPEND
                if data.gate.outcome is WriteGateOutcome.REQUIRE_HUMAN
                else MemoryWriteState.STOP
            )
            return None
        roots_changed = _roots_changed(data.bundle.proposed_roots, data.request.canonical_root_refs)
        profile = data.request.commit_profile
        text_changed = (
            data.bundle.proposed_roots.text_root.artifact_id
            != data.request.canonical_root_refs.text_root.artifact_id
        )
        if profile.value == "chapter_reveal_atomic" and not text_changed:
            data.terminal_codes.append("CHAPTER_TEXT_UPDATE_REQUIRED")
            return self._fatal(data, data.terminal_codes[-1])
        if not roots_changed:
            if profile.value == "changed_roots_only":
                data.state = MemoryWriteState.COMPLETE
                return None
            data.terminal_codes.append(
                "CHAPTER_TEXT_UPDATE_REQUIRED"
                if profile.value == "chapter_reveal_atomic"
                else "REQUIRED_ROOT_UPDATE_MISSING"
            )
            return self._fatal(data, data.terminal_codes[-1])
        request_hash = sha256_id(
            canonical_json_bytes(
                {
                    "request": data.request.model_dump(mode="json"),
                    "candidate": data.candidate.content_hash.root,
                    "materialization": data.materialization.proposed_roots_hash.root,
                    "gate": data.gate.model_dump(mode="json"),
                }
            )
        )
        data.commit_effect_id = StableId(f"effect.commit.{data.request.idempotency_key.root}")
        data.commit_request = DurableMemoryWriteCommitRequest(
            request_id=data.request.request_id,
            project_id=data.request.project_id,
            base_commit=data.request.base_commit,
            idempotency_key=data.request.idempotency_key,
            commit_effect_id=data.commit_effect_id,
            request_hash=request_hash,
            candidate=data.candidate,
            materialization=data.materialization,
            bundle=data.bundle,
            validation=data.validation,
            gate=data.gate,
        )
        data.checkpoint_ref = self._save_checkpoint(
            data,
            phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            resume_state=MemoryWriteState.COMMIT,
            commit_attempt_status=EffectStatus.REQUESTED,
        )
        self._event(data, RunEventType.COMMIT_REQUESTED)
        data.state = MemoryWriteState.COMMIT
        self._fault("commit_request_checkpoint_committed", data)
        return None

    def _commit(self, data: _WorkflowData) -> MemoryWriteWorkflowResult | None:
        if data.commit_request is None:
            return self._fatal(data, "COMMIT_REQUEST_MISSING")
        try:
            raw = self._commit_port.resolve_or_replay_exact(data.commit_request)
            result = _as_commit_result(raw)
        except Exception as error:
            data.checkpoint_ref = self._save_checkpoint(
                data,
                phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                resume_state=MemoryWriteState.COMMIT,
                commit_attempt_status=EffectStatus.UNCERTAIN,
            )
            return self._suspended(data, "COMMIT_EFFECT_UNCERTAIN", str(error))
        data.commit_result = result
        if result.status == MemoryWriteCommitStatus.CONFLICTED:
            data.directive = RepairDirective(
                directive_id=StableId(f"repair.conflict.{data.request.request_id.root}"),
                action=RepairAction.REPLAN,
                reason_codes=("COMMIT_CONFLICTED",),
                checkpoint_required=True,
            )
            return self._replan(data, ("COMMIT_CONFLICTED",))
        if result.status == MemoryWriteCommitStatus.DRY_RUN_REFUSED:
            return self._suspended(
                data,
                "DRY_RUN_COMMIT_REFUSED",
                result.reason or "dry-run commit port refused the commit",
            )
        if result.status != MemoryWriteCommitStatus.ACCEPTED or result.commit_id is None:
            return self._fatal(data, result.reason or "COMMIT_REJECTED")
        self._event(data, RunEventType.COMMIT_ACCEPTED)
        data.committed_operation_ids = list(result.committed_operation_ids)
        if not data.committed_operation_ids and data.bundle is not None:
            data.committed_operation_ids = [
                item.operation_id for item in data.bundle.observed_changes.operations
            ]
        self._fault("commit_accepted_before_checkpoint", data)
        data.checkpoint_ref = self._save_checkpoint(
            data,
            phase=MemoryWriteWorkflowPhase.CANON_COMMITTED,
            resume_state=MemoryWriteState.PROJECT,
            accepted_commit_id=result.commit_id,
            commit_receipt_ref=result.commit_receipt_ref or _receipt_ref(data, "commit"),
            commit_attempt_status=EffectStatus.COMPLETED,
        )
        data.state = MemoryWriteState.PROJECT
        return None

    def _project(self, data: _WorkflowData) -> MemoryWriteWorkflowResult | None:
        if data.commit_result is None or data.commit_result.commit_id is None:
            return self._fatal(data, "PROJECT_WITHOUT_COMMIT")
        if data.projection_effect_id is None:
            data.projection_effect_id = StableId(
                f"effect.projection.{data.commit_result.commit_id.root.removeprefix('sha256:')[:40]}"
            )
        projection = self._projection.request_or_read_by_effect_id(
            data.request.project_id,
            data.commit_result.commit_id,
            data.projection_effect_id,
        )
        data.projection = projection
        if projection.status is ProjectionReadinessStatus.READY:
            data.state = MemoryWriteState.FRESHNESS_GATE
            return None
        data.checkpoint_ref = self._save_checkpoint(
            data,
            phase=MemoryWriteWorkflowPhase.PROJECTION_PENDING,
            resume_state=MemoryWriteState.FRESHNESS_GATE,
            accepted_commit_id=data.commit_result.commit_id,
            commit_receipt_ref=data.commit_result.commit_receipt_ref
            or _receipt_ref(data, "commit"),
            projection_effect_id=data.projection_effect_id,
            projection_status=EffectStatus.REQUESTED,
            projection_receipt_ref=projection.projection_receipt_ref,
        )
        self._event(data, RunEventType.PROJECTION_WAITING)
        if projection.status is ProjectionReadinessStatus.FAILED and not projection.resumable:
            return self._post_commit_fatal(
                data, "PROJECTION_FAILED", projection.reason or "projection failed"
            )
        return self._suspended(
            data, "PROJECTION_PENDING", projection.reason or "projection pending"
        )

    def _freshness(self, data: _WorkflowData) -> MemoryWriteWorkflowResult | None:
        if data.commit_result is None or data.commit_result.commit_id is None:
            return self._fatal(data, "FRESHNESS_WITHOUT_COMMIT")
        if data.projection is None or data.projection.status is not ProjectionReadinessStatus.READY:
            projection = self._projection.await_or_check(
                data.request.project_id,
                data.commit_result.commit_id,
                data.projection_effect_id
                or StableId(f"effect.projection.{data.request.request_id.root}"),
            )
            data.projection = projection
            if projection.status is not ProjectionReadinessStatus.READY:
                if (
                    projection.status is ProjectionReadinessStatus.FAILED
                    and not projection.resumable
                ):
                    return self._post_commit_fatal(
                        data, "PROJECTION_FAILED", projection.reason or "projection failed"
                    )
                data.checkpoint_ref = self._save_checkpoint(
                    data,
                    phase=MemoryWriteWorkflowPhase.PROJECTION_PENDING,
                    resume_state=MemoryWriteState.FRESHNESS_GATE,
                    accepted_commit_id=data.commit_result.commit_id,
                    commit_receipt_ref=data.commit_result.commit_receipt_ref
                    or _receipt_ref(data, "commit"),
                    projection_effect_id=data.projection_effect_id,
                    projection_status=EffectStatus.REQUESTED,
                )
                self._event(data, RunEventType.PROJECTION_WAITING)
                return self._suspended(
                    data, "PROJECTION_PENDING", projection.reason or "projection pending"
                )
        if (
            data.projection.freshness is None
            or data.projection.freshness.status is not FreshnessStatus.READY
        ):
            return self._post_commit_fatal(
                data, "FRESHNESS_NOT_READY", "projection freshness gate did not pass"
            )
        self._event(data, RunEventType.FRESHNESS_PASSED)
        data.state = MemoryWriteState.COMPLETE
        return self._complete(data)

    def _complete(self, data: _WorkflowData) -> MemoryWriteWorkflowResult:
        if data.commit_result is None or data.commit_result.commit_id is None:
            result = MemoryWriteWorkflowResult(
                request_id=data.request.request_id,
                status=MemoryWriteWorkflowStatus.NOOP,
                workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
                canonical_commit_accepted=False,
                base_commit=data.request.base_commit,
                world_mutation_noop=data.world_mutation_noop,
                terminal_candidate_id=None
                if data.candidate is None
                else data.candidate.candidate_id,
                validation_receipt=None
                if data.validation is None
                else _model_ref(data.validation, data, "validation"),
                checkpoint_ref=data.checkpoint_ref,
                continuation_decision=ContinuationDecision.SAFE_TO_CONTINUE,
                budget_usage=data.usage,
                terminal_codes=tuple(data.terminal_codes),
            )
            self._save_terminal(data, result)
            return result
        projection = data.projection
        if (
            projection is None
            or projection.status is not ProjectionReadinessStatus.READY
            or projection.freshness is None
        ):
            return self._post_commit_fatal(
                data, "COMPLETE_WITHOUT_FRESHNESS", "complete state has no freshness"
            )
        commit_ref = data.commit_result.commit_receipt_ref or _receipt_ref(data, "commit")
        validation_ref = (
            None if data.validation is None else _model_ref(data.validation, data, "validation")
        )
        result = MemoryWriteWorkflowResult(
            request_id=data.request.request_id,
            status=MemoryWriteWorkflowStatus.COMMITTED,
            workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
            canonical_commit_accepted=True,
            base_commit=data.request.base_commit,
            resulting_commit=data.commit_result.commit_id,
            world_mutation_noop=data.world_mutation_noop,
            accepted_candidate_id=None if data.candidate is None else data.candidate.candidate_id,
            terminal_candidate_id=None if data.candidate is None else data.candidate.candidate_id,
            validation_receipt=validation_ref,
            guardian_receipt=None
            if data.guardian is None
            else _model_ref(data.guardian, data, "guardian"),
            commit_receipt=commit_ref,
            projection_receipt_ref=projection.projection_receipt_ref,
            freshness_receipt_ref=projection.freshness_receipt_ref,
            projection_snapshot_id=projection.projection_snapshot_id,
            freshness=projection.freshness,
            checkpoint_ref=data.checkpoint_ref,
            degraded=data.degraded,
            quarantine_refs=tuple(data.quarantine_refs),
            committed_operation_ids=tuple(data.committed_operation_ids),
            quarantined_operation_ids=tuple(data.quarantined_operation_ids),
            continuation_decision=(
                ContinuationDecision.BLOCK_NEXT_CHAPTER
                if data.degraded
                else ContinuationDecision.SAFE_TO_CONTINUE
            ),
            budget_usage=data.usage,
            terminal_codes=tuple(data.terminal_codes),
        )
        data.checkpoint_ref = self._save_checkpoint(
            data,
            phase=MemoryWriteWorkflowPhase.COMPLETE,
            resume_state=MemoryWriteState.COMPLETE,
            accepted_commit_id=data.commit_result.commit_id,
            commit_receipt_ref=commit_ref,
            projection_effect_id=data.projection_effect_id,
            projection_status=EffectStatus.COMPLETED,
            projection_receipt_ref=projection.projection_receipt_ref,
            projection_snapshot_id=projection.projection_snapshot_id,
            freshness_receipt_ref=projection.freshness_receipt_ref,
            terminal_result_ref=self._save_terminal(data, result),
        )
        result = result.model_copy(update={"checkpoint_ref": data.checkpoint_ref})
        return result

    def _suspend_for_human(self, data: _WorkflowData) -> MemoryWriteWorkflowResult:
        if data.candidate is None or data.validation is None:
            return self._fatal(data, "HUMAN_WITHOUT_CANDIDATE")
        if self._human is None:
            return self._human_required(data, "HUMAN_APPROVAL_PORT_UNAVAILABLE")
        if data.approval_request is None:
            data.approval_request = HumanApprovalRequest(
                approval_request_id=StableId(
                    f"approval.{data.request.request_id.root}.{data.candidate.candidate_id.root}"
                ),
                request_id=data.request.request_id,
                candidate_id=data.candidate.candidate_id,
                candidate_content_hash=data.candidate.content_hash,
                base_commit=data.request.base_commit,
                validation=_model_ref(data.validation, data, "validation"),
                risk=None if data.risk is None else _model_ref(data.risk, data, "risk"),
                guardian=None
                if data.guardian is None
                else _model_ref(data.guardian, data, "guardian"),
                created_at=self._clock.now(),
            )
            self._human.request(data.approval_request)
        data.checkpoint_ref = self._save_checkpoint(
            data,
            phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            resume_state=MemoryWriteState.HUMAN_RESUME,
            approval_request_artifact=_model_ref(data.approval_request, data, "approval-request"),
        )
        return self._human_required(data, "HUMAN_APPROVAL_PENDING")

    def _resume_human(self, data: _WorkflowData) -> MemoryWriteWorkflowResult | None:
        if self._human is None or data.candidate is None:
            return self._human_required(data, "HUMAN_APPROVAL_PORT_UNAVAILABLE")
        request_id = (
            data.approval_request.approval_request_id
            if data.approval_request
            else StableId(f"approval.{data.request.request_id.root}")
        )
        decision = self._human.read_decision(request_id)
        if inspect.isawaitable(decision):
            raise MemoryWriteWorkflowError("HumanApprovalPort.read_decision must be synchronous")
        if decision is None:
            return self._suspend_for_human(data)
        _validate_human_decision(data, decision)
        if decision.kind is HumanDecisionKind.APPROVE_EXACT_CANDIDATE:
            data.state = MemoryWriteState.PRECOMMIT
        elif decision.kind is HumanDecisionKind.REQUEST_REVISION:
            data.directive = decision.directive
            data.state = MemoryWriteState.CURATOR_REPAIR
        elif decision.kind is HumanDecisionKind.HUMAN_PATCH:
            if not self._candidate_budget_available(data):
                return self._budget_stop(data)
            payload = _read_model(
                self._artifacts,
                _require(decision.patch_candidate_artifact),
                MemoryWriteCandidatePayload,
            )
            if (
                payload.root_update_intents != data.request.root_update_intents
                or payload.commit_profile is not data.request.commit_profile
            ):
                return self._fatal(data, "HUMAN_PATCH_PAYLOAD_BINDING_MISMATCH")
            data.candidate = self._new_candidate(
                data,
                payload.observed_changes,
                CandidateProducerKind.HUMAN_PATCH,
                parent=data.candidate,
                producer_receipt=decision.patch_candidate_artifact,
                candidate_artifact=decision.patch_candidate_artifact,
            )
            data.state = MemoryWriteState.NORMALIZE
        else:
            data.state = MemoryWriteState.QUARANTINE
        return None

    def _quarantine_candidate(self, data: _WorkflowData) -> MemoryWriteWorkflowResult | None:
        candidate = data.candidate
        if candidate is None:
            return self._fatal(data, "QUARANTINE_WITHOUT_CANDIDATE")
        operation_ids = () if data.directive is None else data.directive.operation_ids
        package = QuarantinePackage(
            package_id=_bounded_workflow_id(
                "quarantine.candidate",
                data.request.request_id.root,
                candidate.candidate_id.root,
            ),
            request_id=data.request.request_id,
            base_commit=data.request.base_commit,
            candidate_ids=tuple(
                item.candidate_id
                for item in self._lineage.list_for_request(data.request.request_id)
            ),
            source_artifacts=data.request.source_artifacts,
            validation_ref=None
            if data.validation is None
            else _model_ref(data.validation, data, "validation"),
            guardian_ref=None
            if data.guardian is None
            else _model_ref(data.guardian, data, "guardian"),
            gate_ref=None if data.gate is None else _model_ref(data.gate, data, "gate"),
            quarantined_operation_ids=operation_ids,
            configuration_fingerprint=data.request.configuration_fingerprint,
            terminal_reason=";".join(data.terminal_codes or ("QUARANTINED",)),
            recommended_action="human_review",
        )
        quarantine_ref = self._quarantine.persist(package)
        data.quarantine_refs.append(quarantine_ref)
        self._event(data, RunEventType.CANDIDATE_QUARANTINED, (quarantine_ref,))
        if operation_ids and data.bundle is not None:
            remaining = tuple(
                item
                for item in data.bundle.observed_changes.operations
                if item.operation_id not in operation_ids
            )
            if remaining:
                if not self._candidate_budget_available(data):
                    data.terminal_codes.append("CANDIDATE_REVISION_BUDGET_EXHAUSTED")
                    return self._quarantined(data)
                data.quarantined_operation_ids.extend(operation_ids)
                data.degraded = True
                changes = data.bundle.observed_changes.model_copy(update={"operations": remaining})
                data.candidate = self._new_candidate(
                    data,
                    changes,
                    CandidateProducerKind.OPERATION_QUARANTINE,
                    parent=candidate,
                )
                data.directive = None
                data.state = MemoryWriteState.NORMALIZE
                return None
        return self._quarantined(data)

    def _quarantined(self, data: _WorkflowData) -> MemoryWriteWorkflowResult:
        return MemoryWriteWorkflowResult(
            request_id=data.request.request_id,
            status=MemoryWriteWorkflowStatus.QUARANTINED,
            workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            canonical_commit_accepted=False,
            base_commit=data.request.base_commit,
            terminal_candidate_id=None if data.candidate is None else data.candidate.candidate_id,
            validation_receipt=None
            if data.validation is None
            else _model_ref(data.validation, data, "validation"),
            guardian_receipt=None
            if data.guardian is None
            else _model_ref(data.guardian, data, "guardian"),
            checkpoint_ref=data.checkpoint_ref,
            quarantine_refs=tuple(data.quarantine_refs),
            quarantined_operation_ids=tuple(data.quarantined_operation_ids),
            continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
            budget_usage=data.usage,
            terminal_codes=tuple(data.terminal_codes or ["QUARANTINED"]),
        )

    def _budget_stop(self, data: _WorkflowData) -> MemoryWriteWorkflowResult:
        data.terminal_codes.append("SEMANTIC_REPAIR_BUDGET_EXHAUSTED")
        self._event(data, RunEventType.REPAIR_EXHAUSTED)
        if (
            data.request.budget.on_budget_exhausted == "quarantine"
            and data.candidate is not None
            and not data.quarantine_refs
        ):
            data.state = MemoryWriteState.QUARANTINE
            self._quarantine_candidate(data)
        return self._budget_result(data)

    def _budget_result(self, data: _WorkflowData) -> MemoryWriteWorkflowResult:
        return MemoryWriteWorkflowResult(
            request_id=data.request.request_id,
            status=MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED,
            workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            canonical_commit_accepted=False,
            base_commit=data.request.base_commit,
            terminal_candidate_id=None if data.candidate is None else data.candidate.candidate_id,
            checkpoint_ref=data.checkpoint_ref,
            quarantine_refs=tuple(data.quarantine_refs),
            continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
            budget_usage=data.usage,
            terminal_codes=tuple(dict.fromkeys(data.terminal_codes)),
        )

    def _human_required(self, data: _WorkflowData, code: str) -> MemoryWriteWorkflowResult:
        data.terminal_codes.append(code)
        if data.checkpoint_ref is None:
            data.checkpoint_ref = self._save_checkpoint(
                data,
                phase=MemoryWriteWorkflowPhase.PRECOMMIT,
                resume_state=MemoryWriteState.HUMAN_RESUME,
            )
        return MemoryWriteWorkflowResult(
            request_id=data.request.request_id,
            status=MemoryWriteWorkflowStatus.HUMAN_REQUIRED,
            workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            canonical_commit_accepted=False,
            base_commit=data.request.base_commit,
            terminal_candidate_id=None if data.candidate is None else data.candidate.candidate_id,
            validation_receipt=None
            if data.validation is None
            else _model_ref(data.validation, data, "validation"),
            guardian_receipt=None
            if data.guardian is None
            else _model_ref(data.guardian, data, "guardian"),
            checkpoint_ref=data.checkpoint_ref,
            continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
            budget_usage=data.usage,
            terminal_codes=tuple(data.terminal_codes),
        )

    def _suspended(self, data: _WorkflowData, code: str, reason: str) -> MemoryWriteWorkflowResult:
        data.terminal_codes.extend((code, reason))
        self._event(data, RunEventType.WORKFLOW_SUSPENDED)
        commit_result = data.commit_result
        accepted_commit = None if commit_result is None else commit_result.commit_id
        accepted_receipt = None if commit_result is None else commit_result.commit_receipt_ref
        accepted = accepted_commit is not None
        phase = (
            MemoryWriteWorkflowPhase.PROJECTION_PENDING
            if accepted
            else MemoryWriteWorkflowPhase.PRECOMMIT
        )
        return MemoryWriteWorkflowResult(
            request_id=data.request.request_id,
            status=MemoryWriteWorkflowStatus.SUSPENDED,
            workflow_phase=phase,
            canonical_commit_accepted=accepted,
            base_commit=data.request.base_commit,
            resulting_commit=accepted_commit,
            accepted_candidate_id=None if data.candidate is None else data.candidate.candidate_id,
            terminal_candidate_id=None if data.candidate is None else data.candidate.candidate_id,
            validation_receipt=None
            if data.validation is None
            else _model_ref(data.validation, data, "validation"),
            guardian_receipt=None
            if data.guardian is None
            else _model_ref(data.guardian, data, "guardian"),
            commit_receipt=None
            if not accepted
            else accepted_receipt or _receipt_ref(data, "commit"),
            projection_receipt_ref=None
            if data.projection is None
            else data.projection.projection_receipt_ref,
            freshness_receipt_ref=None
            if data.projection is None
            else data.projection.freshness_receipt_ref,
            projection_snapshot_id=None
            if data.projection is None
            else data.projection.projection_snapshot_id,
            freshness=None if data.projection is None else data.projection.freshness,
            checkpoint_ref=data.checkpoint_ref,
            continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
            budget_usage=data.usage,
            terminal_codes=tuple(data.terminal_codes),
        )

    def _post_commit_fatal(
        self, data: _WorkflowData, code: str, reason: str
    ) -> MemoryWriteWorkflowResult:
        data.terminal_codes.extend((code, reason))
        phase = (
            MemoryWriteWorkflowPhase.CANON_COMMITTED
            if data.projection is None
            else MemoryWriteWorkflowPhase.PROJECTION_PENDING
        )
        return MemoryWriteWorkflowResult(
            request_id=data.request.request_id,
            status=MemoryWriteWorkflowStatus.FATAL,
            workflow_phase=phase,
            canonical_commit_accepted=True,
            base_commit=data.request.base_commit,
            resulting_commit=_require(data.commit_result).commit_id,
            accepted_candidate_id=None if data.candidate is None else data.candidate.candidate_id,
            terminal_candidate_id=None if data.candidate is None else data.candidate.candidate_id,
            validation_receipt=None
            if data.validation is None
            else _model_ref(data.validation, data, "validation"),
            commit_receipt=_require(data.commit_result).commit_receipt_ref
            or _receipt_ref(data, "commit"),
            projection_receipt_ref=None
            if data.projection is None
            else data.projection.projection_receipt_ref,
            checkpoint_ref=data.checkpoint_ref,
            continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
            budget_usage=data.usage,
            terminal_codes=tuple(data.terminal_codes),
        )

    def _exception_result(self, data: _WorkflowData, *codes: str) -> MemoryWriteWorkflowResult:
        commit_result = data.commit_result
        if (
            commit_result is not None
            and MemoryWriteCommitResult.accepted_status(commit_result.status)
            and commit_result.commit_id is not None
        ):
            reason = " | ".join(codes)
            return self._post_commit_fatal(data, "POST_COMMIT_FAILURE", reason)
        return self._fatal(data, *codes)

    def _fatal(self, data: _WorkflowData, *codes: str) -> MemoryWriteWorkflowResult:
        data.terminal_codes.extend(codes)
        return MemoryWriteWorkflowResult(
            request_id=data.request.request_id,
            status=MemoryWriteWorkflowStatus.FATAL,
            workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            canonical_commit_accepted=False,
            base_commit=data.request.base_commit,
            terminal_candidate_id=None if data.candidate is None else data.candidate.candidate_id,
            validation_receipt=None
            if data.validation is None
            else _model_ref(data.validation, data, "validation"),
            guardian_receipt=None
            if data.guardian is None
            else _model_ref(data.guardian, data, "guardian"),
            checkpoint_ref=data.checkpoint_ref,
            continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
            budget_usage=data.usage,
            terminal_codes=tuple(dict.fromkeys(data.terminal_codes)),
        )

    def _replan(self, data: _WorkflowData, codes: tuple[str, ...]) -> MemoryWriteWorkflowResult:
        data.terminal_codes.extend(codes)
        data.checkpoint_ref = self._save_checkpoint(
            data,
            phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            resume_state=MemoryWriteState.STOP,
            commit_attempt_status=(
                EffectStatus.UNCERTAIN if data.commit_request is not None else None
            ),
        )
        return MemoryWriteWorkflowResult(
            request_id=data.request.request_id,
            status=MemoryWriteWorkflowStatus.REPLAN_REQUIRED,
            workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            canonical_commit_accepted=False,
            base_commit=data.request.base_commit,
            terminal_candidate_id=None if data.candidate is None else data.candidate.candidate_id,
            checkpoint_ref=data.checkpoint_ref,
            continuation_decision=ContinuationDecision.BLOCK_NEXT_CHAPTER,
            budget_usage=data.usage,
            terminal_codes=tuple(dict.fromkeys(data.terminal_codes)),
        )

    def _gate(self, data: _WorkflowData) -> WriteGateDecision:
        candidate = _require(data.candidate)
        risk = _require(data.risk)
        if self._write_gate is not None:
            result = self._write_gate.decide(
                candidate, data.validation, risk, guardian=data.guardian
            )
            if not isinstance(result, WriteGateDecision):
                raise MemoryWriteWorkflowError("write gate returned an invalid decision")
            return result
        guardian_id = None if data.guardian is None else data.guardian.decision_id
        outcome = (
            WriteGateOutcome.ALLOW_COMMIT
            if not risk.requires_guardian
            or (data.guardian is not None and data.guardian.outcome is GuardianOutcome.APPROVE)
            else WriteGateOutcome.REQUIRE_GUARDIAN
        )
        return WriteGateDecision(
            decision_id=StableId(f"write-gate.{candidate.candidate_id.root}"),
            change_set_id=candidate.candidate_id,
            base_commit=data.request.base_commit,
            outcome=outcome,
            risk_assessment_id=risk.assessment_id,
            guardian_decision_id=guardian_id,
            reasons=() if outcome is WriteGateOutcome.ALLOW_COMMIT else ("GUARDIAN_REQUIRED",),
        )

    def _reserve(self, data: _WorkflowData, operation: str, counter: str) -> bool:
        del operation
        self._update_elapsed(data)
        budget = data.request.budget
        value = getattr(data.usage, counter)
        limit = {
            "total_model_calls": budget.max_total_model_calls,
            "curator_repairs": budget.max_curator_repairs,
            "guardian_reviews": budget.max_guardian_reviews,
        }[counter]
        return not (
            value >= limit
            or data.usage.elapsed_ms >= budget.wall_clock_budget_ms
            or data.usage.tokens_used >= budget.token_budget
        )

    def _settle_model(self, data: _WorkflowData, tokens: int, attempts: int) -> None:
        self._update_elapsed(data)
        data.usage = data.usage.model_copy(
            update={
                "total_model_calls": data.usage.total_model_calls + 1,
                "transport_attempts": data.usage.transport_attempts + max(attempts, 1),
                "tokens_used": data.usage.tokens_used + max(tokens, 0),
            }
        )

    @staticmethod
    def _candidate_budget_available(data: _WorkflowData) -> bool:
        return data.usage.candidate_revisions < data.request.budget.max_candidate_revisions

    def _update_elapsed(self, data: _WorkflowData) -> None:
        if data.started_at is None:
            data.started_at = self._clock.now()
            return
        elapsed = max(0, int((self._clock.now() - data.started_at).total_seconds() * 1000))
        if elapsed > data.usage.elapsed_ms:
            data.usage = data.usage.model_copy(update={"elapsed_ms": elapsed})

    def _save_checkpoint(
        self,
        data: _WorkflowData,
        *,
        phase: MemoryWriteWorkflowPhase,
        resume_state: MemoryWriteState,
        commit_attempt_status: EffectStatus | None = None,
        accepted_commit_id: CommitId | None = None,
        commit_receipt_ref: ArtifactRef | None = None,
        projection_effect_id: StableId | None = None,
        projection_status: EffectStatus | None = None,
        projection_receipt_ref: ArtifactRef | None = None,
        projection_snapshot_id: StableId | None = None,
        freshness_receipt_ref: ArtifactRef | None = None,
        terminal_result_ref: ArtifactRef | None = None,
        approval_request_artifact: ArtifactRef | None = None,
        proposal_human_request_artifact: ArtifactRef | None = None,
    ) -> ArtifactRef:
        request_ref = _model_ref(data.request, data, "request")
        checkpoint = MemoryWriteCheckpoint(
            checkpoint_id=StableId(
                f"checkpoint.{data.request.request_id.root}.{len(self._checkpoint_states) + 1}"
            ),
            request_identity_hash=_request_identity_hash(data.request),
            request_artifact_ref=request_ref,
            run_id=data.request.run_id,
            task_id=data.request.task_id,
            project_id=data.request.project_id,
            base_commit=data.request.base_commit,
            source_artifacts=data.request.source_artifacts,
            root_update_intents=data.request.root_update_intents,
            world_mutation=data.request.world_mutation,
            information_boundary=data.request.information_boundary,
            configuration_fingerprint=data.request.configuration_fingerprint,
            workflow_phase=phase,
            state=data.state,
            resume_state=resume_state,
            current_candidate_id=None if data.candidate is None else data.candidate.candidate_id,
            proposal_attempt_no=data.proposal_attempt_no,
            inflight_proposal_attempt_id=(
                None
                if data.inflight_proposal_attempt is None
                else data.inflight_proposal_attempt.attempt_id
            ),
            inflight_proposal_attempt_ref=(
                data.proposal_attempt_refs[-1]
                if data.inflight_proposal_attempt is not None and data.proposal_attempt_refs
                else None
            ),
            proposal_attempt_status=(
                None
                if data.inflight_proposal_attempt is None
                else data.inflight_proposal_attempt.status
            ),
            proposal_attempt_refs=tuple(data.proposal_attempt_refs),
            proposal_rejection_refs=tuple(data.proposal_rejection_refs),
            proposal_feedback_ref=data.proposal_feedback_ref,
            proposal_directive_ref=(data.proposal_directive_ref),
            last_proposal_output_hash=data.last_proposal_output_hash,
            last_proposal_rejection_signature=(data.last_proposal_rejection_signature),
            same_proposal_output_count=data.same_proposal_output_count,
            same_proposal_rejection_count=data.same_proposal_rejection_count,
            proposal_budget_reservation_ref=data.proposal_budget_reservation_ref,
            lineage_head_artifact=None
            if data.candidate is None
            else data.candidate.candidate_artifact,
            materialization_artifact=None
            if data.materialization is None
            else _model_ref(data.materialization, data, "candidate-materialization"),
            validation_artifact=None
            if data.validation is None
            else _model_ref(data.validation, data, "validation"),
            risk_artifact=None if data.risk is None else _model_ref(data.risk, data, "risk"),
            guardian_artifact=None
            if data.guardian is None
            else _model_ref(data.guardian, data, "guardian"),
            gate_artifact=None if data.gate is None else _model_ref(data.gate, data, "gate"),
            approval_request_artifact=approval_request_artifact,
            proposal_human_request_artifact=proposal_human_request_artifact,
            commit_effect_id=data.commit_effect_id,
            commit_request_ref=None
            if data.commit_request is None
            else _model_ref(data.commit_request, data, "commit-request"),
            commit_attempt_status=commit_attempt_status,
            accepted_commit_id=accepted_commit_id,
            commit_receipt_ref=commit_receipt_ref,
            projection_effect_id=projection_effect_id or data.projection_effect_id,
            projection_status=projection_status,
            projection_receipt_ref=projection_receipt_ref,
            projection_snapshot_id=projection_snapshot_id,
            freshness_receipt_ref=freshness_receipt_ref,
            terminal_result_ref=terminal_result_ref,
            completed_effect_ids=tuple(
                item
                for item in (data.commit_effect_id, data.projection_effect_id)
                if item is not None
            ),
            budget_usage=data.usage,
            last_event_sequence=len(getattr(self._events, "events", ())),
            resumability_status=ResumabilityStatus.RESUMABLE,
        )
        ref = self._checkpoint.save(checkpoint)
        self._checkpoint_states[ref.artifact_id] = data
        data.checkpoint_ref = ref
        self._event(data, RunEventType.CHECKPOINT_CREATED, artifact_refs=(ref,))
        return ref

    def _save_terminal(self, data: _WorkflowData, result: MemoryWriteWorkflowResult) -> ArtifactRef:
        return _model_ref(result, data, "terminal-result")

    def _event(
        self,
        data: _WorkflowData,
        event_type: RunEventType,
        artifact_refs: tuple[ArtifactRef, ...] = (),
    ) -> None:
        event = RunEvent(
            event_id=StableId(
                "event."
                f"{data.request.request_id.root}."
                f"{len(getattr(self._events, 'events', ())) + 1}"
            ),
            run_id=data.request.run_id,
            task_id=data.request.task_id,
            sequence_no=len(getattr(self._events, "events", ())) + 1,
            event_type=event_type,
            occurred_at=self._clock.now(),
            idempotency_identity=StableId(
                "event-effect."
                f"{data.request.request_id.root}."
                f"{event_type.value}."
                f"{len(getattr(self._events, 'events', ())) + 1}"
            ),
            payload_schema_version=SchemaVersion("0.1.0"),
            trace_id=f"trace.{data.request.run_id.root}",
            payload={
                "request_id": data.request.request_id.root,
                "candidate_id": None
                if data.candidate is None
                else data.candidate.candidate_id.root,
                "logical_state": data.state.value,
                "workflow_phase": _phase_for_data(data).value,
                "canonical_commit_accepted": data.commit_result is not None
                and data.commit_result.commit_id is not None,
                "budget_usage": data.usage.model_dump(mode="json"),
            },
            artifact_refs=artifact_refs,
        )
        self._events.append(event)

    def _fault(self, point: str, data: _WorkflowData) -> None:
        injector = self._fault_injector
        if injector is None:
            return
        hit = getattr(injector, "hit", injector)
        if callable(hit):
            hit(point, data)


def _as_proposal_result(value: Any) -> CuratorProposalResult:
    if isinstance(value, CuratorProposalResult):
        return value
    if isinstance(value, ObservedChangeSet):
        return CuratorProposalResult(observed_changes=value)
    if isinstance(value, tuple) and value and isinstance(value[0], ObservedChangeSet):
        return CuratorProposalResult(
            observed_changes=value[0],
            token_usage=getattr(value[1], "usage", 0) if len(value) > 1 else 0,
        )
    if hasattr(value, "observed_changes"):
        return CuratorProposalResult(
            observed_changes=value.observed_changes,
            agent_receipt=getattr(value, "receipt", None),
            producer_receipt=getattr(value, "producer_receipt", None),
        )
    raise MemoryWriteWorkflowError("Curator propose returned an unsupported result")


def _as_repair_result(value: Any) -> CuratorRepairResult:
    if isinstance(value, CuratorRepairResult):
        return value
    if isinstance(value, ObservedChangeSet):
        return CuratorRepairResult(observed_changes=value)
    if hasattr(value, "observed_changes"):
        return CuratorRepairResult(
            observed_changes=value.observed_changes,
            agent_receipt=getattr(value, "receipt", None),
            producer_receipt=getattr(value, "producer_receipt", None),
        )
    raise MemoryWriteWorkflowError("Curator repair returned an unsupported result")


def _as_normalization_result(value: Any, candidate: CandidateRevision) -> NormalizationResult:
    if isinstance(value, NormalizationResult):
        return value
    if isinstance(value, CandidateRevision):
        return NormalizationResult(
            status=NormalizationStatus.TRANSFORMED
            if value != candidate
            else NormalizationStatus.UNCHANGED,
            candidate=value,
        )
    raise MemoryWriteWorkflowError("Normalizer returned an unsupported result")


def _as_materialization_result(value: Any) -> RootMaterializationResult:
    if isinstance(value, RootMaterializationResult):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        return RootMaterializationResult(
            materialization=value[0], bundle=value[1], world_mutation_noop=False
        )
    if hasattr(value, "materialization") and hasattr(value, "bundle"):
        return cast(RootMaterializationResult, value)
    raise MemoryWriteWorkflowError("RootUpdatePort returned an unsupported result")


def _as_guardian_result(value: Any) -> GuardianReviewResult:
    if isinstance(value, GuardianReviewResult):
        return value
    if isinstance(value, GuardianDecision):
        return GuardianReviewResult(decision=value)
    if hasattr(value, "decision"):
        return GuardianReviewResult(
            decision=value.decision, receipt=getattr(value, "receipt", None)
        )
    raise MemoryWriteWorkflowError("GuardianPort returned an unsupported result")


def _as_commit_result(value: Any) -> MemoryWriteCommitResult:
    if isinstance(value, MemoryWriteCommitResult):
        return value
    status = getattr(value, "status", None)
    status_value = str(getattr(status, "value", status))
    return MemoryWriteCommitResult(
        request_id=value.request_id,
        status=status_value,
        commit_id=getattr(value, "commit_id", None),
        manifest=getattr(value, "manifest", None),
        commit_receipt_ref=getattr(value, "commit_receipt_ref", None),
        reason=getattr(value, "reason", None),
        committed_operation_ids=tuple(getattr(value, "committed_operation_ids", ())),
    )


def _checkpoint_matches(
    request: MemoryWriteWorkflowRequest, checkpoint: MemoryWriteCheckpoint
) -> bool:
    return (
        checkpoint.request_identity_hash == _request_identity_hash(request)
        and checkpoint.run_id == request.run_id
        and checkpoint.task_id == request.task_id
        and checkpoint.project_id == request.project_id
        and checkpoint.base_commit == request.base_commit
        and checkpoint.source_artifacts == request.source_artifacts
        and checkpoint.root_update_intents == request.root_update_intents
        and checkpoint.world_mutation == request.world_mutation
        and checkpoint.information_boundary == request.information_boundary
        and checkpoint.configuration_fingerprint == request.configuration_fingerprint
    )


def _request_identity_hash(request: MemoryWriteWorkflowRequest) -> ArtifactId:
    payload = request.model_dump(mode="json")
    payload["resume_checkpoint"] = None
    return sha256_id(canonical_json_bytes(payload))


def _request_basis_hash(request: MemoryWriteWorkflowRequest) -> ArtifactId:
    payload = request.model_dump(mode="json")
    payload.pop("resume_checkpoint", None)
    return sha256_id(canonical_json_bytes(payload))


def _bounded_workflow_id(namespace: str, *identity_parts: str) -> StableId:
    """Keep compound workflow identities deterministic and within StableId limits."""

    digest = sha256_id(canonical_json_bytes(identity_parts)).root.removeprefix("sha256:")
    return StableId(f"{namespace}.{digest}")


def _model_ref(value: Any, data: _WorkflowData, label: str) -> ArtifactRef:
    artifacts = data.artifacts
    if artifacts is None:
        raise MemoryWriteWorkflowError("artifact repository is unavailable")
    result = artifacts.put(
        canonical_json_bytes(value.model_dump(mode="json")),
        f"application/vnd.novel-agent.{label}+json",
        data.request.canonical_root_refs.schema_version,
    )
    if not isinstance(result, ArtifactRef):
        raise MemoryWriteWorkflowError("artifact repository returned an invalid reference")
    return result


def _put_model(artifacts: Any, value: Any, media_type: str, version: SchemaVersion) -> ArtifactRef:
    if hasattr(artifacts, "put_model"):
        return cast(ArtifactRef, artifacts.put_model(value, media_type, version))
    return cast(
        ArtifactRef,
        artifacts.put(
            canonical_json_bytes(value.model_dump(mode="json")),
            media_type,
            version,
        ),
    )


def _read_model(artifacts: Any, ref: ArtifactRef, model_type: Any) -> Any:
    if hasattr(artifacts, "read_model"):
        return artifacts.read_model(ref, model_type)
    return model_type.model_validate_json(artifacts.read_verified(ref), strict=True)


def _receipt_ref(data: _WorkflowData, label: str) -> ArtifactRef:
    artifacts = data.artifacts
    if artifacts is None:
        raise MemoryWriteWorkflowError("artifact repository is unavailable")
    return cast(
        ArtifactRef,
        artifacts.put(
            canonical_json_bytes({"request": data.request.request_id.root, "kind": label}),
            f"application/vnd.novel-agent.{label}-receipt+json",
            data.request.canonical_root_refs.schema_version,
        ),
    )


def _source_artifact(request: MemoryWriteWorkflowRequest) -> ArtifactRef:
    if request.source_artifacts:
        return request.source_artifacts[0]
    data = canonical_json_bytes({"request": request.request_id.root, "source": "empty"})
    return ArtifactRef(
        artifact_id=sha256_id(data),
        media_type="application/vnd.novel-agent.empty-source+json",
        byte_length=len(data),
        schema_version=request.canonical_root_refs.schema_version,
    )


def _visibility_ref(receipt: Any) -> ArtifactRef:
    data = canonical_json_bytes(receipt.model_dump(mode="json"))
    return ArtifactRef(
        artifact_id=receipt.receipt_hash,
        media_type="application/vnd.novel-agent.source-visibility-receipt+json",
        byte_length=len(data),
        schema_version=receipt.source_artifact.schema_version,
    )


def _earliest_position(values: list[Any]) -> Any:
    if not values:
        return None
    return min(values, key=_position_key)


def _position_key(position: Any) -> tuple[int, int, int]:
    return (
        position.chapter_index,
        -1 if position.scene_index is None else position.scene_index,
        -1 if position.block_index is None else position.block_index,
    )


def _narrowest_scope(values: tuple[Any, ...]) -> Any:
    from novel_agent.domain.stage2 import AccessScope

    if not values:
        return AccessScope.WRITER_SAFE
    rank = {
        AccessScope.WRITER_SAFE: 0,
        AccessScope.AUTHOR_PLANNING: 1,
        AccessScope.EVALUATOR: 2,
    }
    return min(values, key=lambda item: rank[item])


def _roots_changed(candidate: RootManifest, base: RootManifest) -> bool:
    return any(
        left.artifact_id != right.artifact_id
        for left, right in (
            (candidate.text_root, base.text_root),
            (candidate.plan_root, base.plan_root),
            (candidate.world_root, base.world_root),
            (candidate.reference_root, base.reference_root),
            (candidate.project_profile_root, base.project_profile_root),
        )
    )


def _remaining(budget: Any, usage: MemoryWriteBudgetUsage) -> Any:
    from novel_agent.domain.memory_write import MemoryWriteBudgetRemaining

    return MemoryWriteBudgetRemaining(
        curator_proposal_attempts=max(
            budget.max_curator_proposal_attempts - usage.curator_proposal_attempts,
            0,
        ),
        curator_proposal_rejections=max(
            budget.max_curator_proposal_rejections - usage.curator_proposal_rejections,
            0,
        ),
        structured_generation_attempts=max(
            budget.max_total_model_calls - usage.structured_generation_attempts,
            0,
        ),
        candidate_revisions=max(budget.max_candidate_revisions - usage.candidate_revisions, 0),
        curator_repairs=max(budget.max_curator_repairs - usage.curator_repairs, 0),
        normalization_passes=max(budget.max_normalization_passes - usage.normalization_passes, 0),
        guardian_reviews=max(budget.max_guardian_reviews - usage.guardian_reviews, 0),
        context_refreshes=max(budget.max_context_refreshes - usage.context_refreshes, 0),
        total_model_calls=max(budget.max_total_model_calls - usage.total_model_calls, 0),
        token_budget=max(budget.token_budget - usage.tokens_used, 0),
        wall_clock_budget_ms=max(budget.wall_clock_budget_ms - usage.elapsed_ms, 0),
    )


def _candidate_id(data: _WorkflowData) -> StableId:
    if data.candidate is None:
        return StableId(f"candidate.missing.{data.request.request_id.root}")
    return data.candidate.candidate_id


def _require(value: Any) -> Any:
    if value is None:
        raise MemoryWriteWorkflowError("required workflow value is missing")
    return value


def _current_commit(data: _WorkflowData) -> CommitId:
    try:
        result = data.basis.commit_id if data.basis is not None else data.request.base_commit
        return result
    except AttributeError:
        return data.request.base_commit


def _phase_for_data(data: _WorkflowData) -> MemoryWriteWorkflowPhase:
    if data.commit_result is None or data.commit_result.commit_id is None:
        return MemoryWriteWorkflowPhase.PRECOMMIT
    if data.state in {MemoryWriteState.PROJECT, MemoryWriteState.FRESHNESS_GATE}:
        return MemoryWriteWorkflowPhase.PROJECTION_PENDING
    if data.state is MemoryWriteState.COMPLETE:
        return MemoryWriteWorkflowPhase.COMPLETE
    return MemoryWriteWorkflowPhase.CANON_COMMITTED


def _validate_human_decision(data: _WorkflowData, decision: HumanApprovalDecision) -> None:
    candidate = _require(data.candidate)
    if (
        data.approval_request is not None
        and decision.approval_request_id != data.approval_request.approval_request_id
    ):
        raise MemoryWriteWorkflowError("Human decision references another approval request")
    if (
        decision.request_id != data.request.request_id
        or decision.candidate_id != candidate.candidate_id
        or decision.candidate_content_hash != candidate.content_hash
        or decision.base_commit != data.request.base_commit
    ):
        raise MemoryWriteWorkflowError("Human decision is bound to another candidate basis")
    if decision.kind is HumanDecisionKind.REQUEST_REVISION and (
        decision.directive is None or decision.directive.action is not RepairAction.CURATOR_REPAIR
    ):
        raise MemoryWriteWorkflowError("Human revision must carry a curator-repair directive")


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def awaitable_result(value: Any) -> Any:
    if inspect.isawaitable(value):
        raise MemoryWriteWorkflowError(
            "synchronous CanonicalReadPort is required for current_commit"
        )
    return value


def _as_repair_result_or_none(value: Any) -> Any:
    return value


__all__ = [
    "ImmediateProjectionReadinessPort",
    "InMemoryArtifactRepository",
    "InMemoryCandidateLineageRepository",
    "InMemoryCheckpointRepository",
    "InMemoryCommitPort",
    "InMemoryQuarantineRepository",
    "InMemoryRunEventSink",
    "LocalMemoryWriteWorkflow",
    "MemoryWriteIdentityCollision",
    "MemoryWriteWorkflowError",
    "StaticCanonicalReadPort",
]
