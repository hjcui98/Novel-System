"""Teacher-forced adapters for the framework-neutral Stage 2W workflow.

The adapters keep legacy Stage 2 services behind the new ports.  They are the
only layer that knows how to translate v2 workflow contracts to the older
validator, Guardian, CommitService, and projection APIs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from novel_agent.agents.curator import CuratorReplayAgent
from novel_agent.agents.curator_repair import CuratorRepairAgent
from novel_agent.agents.guardian import GuardianRiskReviewAgent
from novel_agent.domain.artifacts import ArtifactRef, RootManifest
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.changes import (
    ObservedChangeSet,
    ValidationFinding,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    FreshnessMode,
    FreshnessRequest,
    FreshnessStatus,
    WorldRootDocument,
)
from novel_agent.domain.memory_write import (
    BoundaryPropagationReceipt,
    CanonicalWriteBasis,
    CuratorProposalAccepted,
    CuratorProposalAttemptReceipt,
    CuratorProposalAttemptStatus,
    CuratorProposalRejected,
    CuratorProposalRejection,
    InformationBoundary,
    MemoryWriteWorkflowRequest,
    NarrativePosition,
    ProjectionReadinessResult,
    ProjectionReadinessStatus,
    ProposalRejectionKind,
    ProposalRejectionStage,
    SourceProvenance,
    SourceVisibilityReceipt,
    ValidationDecision,
    ValidationDisposition,
)
from novel_agent.domain.model_calls import ModelCallLedgerEntry, ModelRequest
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentExecutionReceipt,
    AgentMode,
    ContractRef,
    CuratorReplayResult,
    GuardianDecision,
    PatchRiskAssessment,
    WriteGateDecision,
    WriteGateOutcome,
)
from novel_agent.domain.world import WorldGraphCandidateBatch
from novel_agent.ports.memory_write import (
    CuratorProposalAttemptRequest,
    CuratorProposalRequest,
    CuratorProposalResult,
    CuratorProposalTransportError,
    CuratorRepairRequest,
    CuratorRepairResult,
    DurableMemoryWriteCommitRequest,
    GuardianReviewRequest,
    GuardianReviewResult,
    MemoryWriteCommitResult,
    MemoryWriteCommitStatus,
)
from novel_agent.services.artifacts import ArtifactRepository, sha256_id
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.model_curation import (
    CuratorProposalSemanticRejected,
    ModelCurationContractError,
    ModelCurator,
)
from novel_agent.services.overlay import WorldOverlay
from novel_agent.services.projection import (
    DerivedProjectionService,
    DerivedSnapshotRepository,
    FreshnessGate,
    snapshot_id_for_commit,
)
from novel_agent.services.world_graph import (
    WorldGraphExtractionPass,
    WorldGraphExtractionResult,
)

ModelRequestFactory = Callable[[str, AgentMode], ModelRequest]
ScriptCallback = Callable[[ModelRequest, AgentMode], None]


@dataclass(frozen=True, slots=True)
class _CuratorProposalExecution:
    result: CuratorReplayResult
    graph_extraction: WorldGraphExtractionResult | None = None


class RepositoryCanonicalReadAdapter:
    """Read a complete, hash-verified canonical basis from existing services."""

    def __init__(self, commits: CommitService, artifacts: ArtifactRepository) -> None:
        self._commits = commits
        self._artifacts = artifacts

    def load_verified(self, project_id: ProjectId, commit_id: CommitId) -> CanonicalWriteBasis:
        manifest = self._commits.load_manifest(commit_id)
        if manifest.project_id != project_id:
            raise ValueError("canonical manifest belongs to another project")
        text = TextRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.text_root), strict=True
        )
        plan = PlanRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.plan_root), strict=True
        )
        world = WorldRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.world_root), strict=True
        )
        return CanonicalWriteBasis(
            project_id=project_id,
            commit_id=commit_id,
            root_manifest=manifest,
            canonical_text=text,
            canonical_plan=plan,
            canonical_world=world,
        )

    def current_commit(self, project_id: ProjectId) -> CommitId:
        return self._commits.current_commit(project_id)


class CommitServiceMemoryWriteAdapter:
    """Translate an accepted v2 bundle into the legacy atomic CommitService."""

    def __init__(self, commits: CommitService, artifacts: ArtifactRepository) -> None:
        self._commits = commits
        self._artifacts = artifacts

    def resolve_or_replay_exact(
        self, request: DurableMemoryWriteCommitRequest
    ) -> MemoryWriteCommitResult:
        rejection = _validate_durable_commit_request(request)
        if rejection is not None:
            return rejection
        validation = ValidationReport(
            report_id=StableId(f"validation-report.{request.candidate.candidate_id.root}"),
            bundle_id=request.bundle.bundle_id,
            status=ValidationStatus.PASSED,
            schema_version=request.bundle.proposed_roots.schema_version,
            validation_profile=request.validation.deterministic_profile,
            validated_at=request.validation.validated_at,
        )
        from novel_agent.domain.changes import CommitRequest

        result = self._commits.commit(
            CommitRequest(
                request_id=request.request_id,
                project_id=request.project_id,
                base_commit=request.base_commit,
                idempotency_key=request.idempotency_key,
                bundle=request.bundle,
                validation_report=validation,
            )
        )
        if result.status.value != MemoryWriteCommitStatus.ACCEPTED.value:
            return MemoryWriteCommitResult(
                request_id=request.request_id,
                status=result.status.value,
                reason=result.reason,
            )
        if result.commit_id is None or result.manifest is None:
            raise ValueError("CommitService accepted without a canonical commit")
        receipt_bytes = canonical_json_bytes(
            {
                "request_id": request.request_id.root,
                "idempotency_key": request.idempotency_key.root,
                "request_hash": request.request_hash.root,
                "commit_id": result.commit_id.root,
            }
        )
        receipt_ref = self._artifacts.put(
            receipt_bytes,
            "application/vnd.novel-agent.memory-write-commit-receipt+json",
            result.manifest.schema_version,
        )
        return MemoryWriteCommitResult(
            request_id=request.request_id,
            status=MemoryWriteCommitStatus.ACCEPTED,
            commit_id=result.commit_id,
            manifest=result.manifest,
            commit_receipt_ref=receipt_ref,
            committed_operation_ids=tuple(
                item.operation_id for item in request.bundle.observed_changes.operations
            ),
        )


class RefusingCommitPort:
    """Dry-run commit port: rejects every commit request without side effects."""

    def __init__(self, *, canonical_commit: CommitId) -> None:
        self._canonical_commit = canonical_commit
        self.calls = 0
        self.accepted_count = 0

    @property
    def current(self) -> CommitId:
        return self._canonical_commit

    def resolve_or_replay_exact(
        self,
        request: DurableMemoryWriteCommitRequest,
    ) -> MemoryWriteCommitResult:
        self.calls += 1
        return MemoryWriteCommitResult(
            request_id=request.request_id,
            status=MemoryWriteCommitStatus.DRY_RUN_REFUSED,
            reason="dry_run_refuses_all_commits",
        )


class ProjectionServiceReadinessAdapter:
    """Expose projection outbox processing and FreshnessGate as a v2 port."""

    def __init__(
        self,
        projections: DerivedProjectionService,
        snapshots: DerivedSnapshotRepository,
        artifacts: ArtifactRepository,
        *,
        auto_process: bool = True,
    ) -> None:
        self._projections = projections
        self._snapshots = snapshots
        self._artifacts = artifacts
        self.auto_process = auto_process

    def request_or_read_by_effect_id(
        self, project_id: ProjectId, commit_id: CommitId, effect_id: StableId
    ) -> ProjectionReadinessResult:
        return self._read(project_id, commit_id, effect_id)

    def await_or_check(
        self, project_id: ProjectId, commit_id: CommitId, effect_id: StableId
    ) -> ProjectionReadinessResult:
        return self._read(project_id, commit_id, effect_id)

    def _read(
        self, project_id: ProjectId, commit_id: CommitId, effect_id: StableId
    ) -> ProjectionReadinessResult:
        try:
            if self.auto_process:
                self._projections.process_all()
            snapshot = self._snapshots.get_for_commit(commit_id)
        except Exception as error:
            return ProjectionReadinessResult(
                effect_id=effect_id,
                status=ProjectionReadinessStatus.FAILED,
                resumable=True,
                reason=f"projection worker failed: {error}",
            )
        if snapshot is None:
            return ProjectionReadinessResult(
                effect_id=effect_id,
                status=ProjectionReadinessStatus.PENDING,
                reason="projection snapshot has not been published",
            )
        required_snapshot = snapshot_id_for_commit(commit_id)
        freshness = FreshnessGate.evaluate(
            FreshnessRequest(
                canonical_commit=commit_id,
                r1_basis_commit=commit_id,
                required_snapshot_id=required_snapshot,
                actual_alias_commit=snapshot.source_commit,
                actual_snapshot=snapshot,
                mode=FreshnessMode.BLOCK_ON_MISMATCH,
            )
        )
        if freshness.status is not FreshnessStatus.READY:
            return ProjectionReadinessResult(
                effect_id=effect_id,
                status=ProjectionReadinessStatus.FAILED,
                resumable=False,
                projection_snapshot_id=snapshot.snapshot_id,
                freshness=freshness,
                reason=freshness.reason,
            )
        projection_ref = self._artifacts.put(
            canonical_json_bytes(
                {
                    "effect_id": effect_id.root,
                    "project_id": project_id.root,
                    "commit_id": commit_id.root,
                    "snapshot": snapshot.model_dump(mode="json"),
                }
            ),
            "application/vnd.novel-agent.projection-receipt+json",
            SchemaVersion("0.1.0"),
        )
        freshness_ref = self._artifacts.put(
            canonical_json_bytes(freshness.model_dump(mode="json")),
            "application/vnd.novel-agent.freshness-receipt+json",
            SchemaVersion("0.1.0"),
        )
        return ProjectionReadinessResult(
            effect_id=effect_id,
            status=ProjectionReadinessStatus.READY,
            projection_receipt_ref=projection_ref,
            freshness_receipt_ref=freshness_ref,
            projection_snapshot_id=snapshot.snapshot_id,
            freshness=freshness,
        )


class TeacherForcedCuratorPort:
    """Bind existing Curator agents to the Stage 2W Curator port."""

    def __init__(
        self,
        replay: CuratorReplayAgent,
        repair: CuratorRepairAgent,
        artifacts: ArtifactRepository,
        request_factory: ModelRequestFactory,
        script: ScriptCallback | None = None,
        *,
        repair_script: ScriptCallback | None = None,
        graph_curator: ModelCurator | None = None,
    ) -> None:
        self._replay = replay
        self._repair = repair
        self._artifacts = artifacts
        self._request_factory = request_factory
        self._script = script
        self._repair_script = repair_script or script
        self._graph_curator = graph_curator
        if graph_curator is not None and graph_curator.gateway is not replay.curator.gateway:
            raise ValueError("graph Curator must share the replay Curator ModelGateway")
        self._graph_extraction = WorldGraphExtractionPass()
        self._revealed_text: TextRootDocument | None = None
        self.last_receipt: AgentExecutionReceipt | None = None
        self.proposal_calls = 0
        self.repair_calls = 0

    def set_revealed_text(self, text: TextRootDocument) -> None:
        self._revealed_text = text

    async def propose(self, request: CuratorProposalRequest) -> CuratorProposalResult:
        basis = request.basis
        text = self._revealed_text or basis.canonical_text
        world = basis.canonical_world
        if text is None or world is None:
            raise ValueError("Curator proposal requires revealed TextRoot and canonical WorldRoot")
        chapter_index = _chapter_index(request.request)
        model_request = self._request_factory(f"curator.replay.{chapter_index}", AgentMode.REPLAY)
        graph_request = self._graph_model_request(model_request)
        manifest = _basis_manifest(basis)
        execution = await self._run_proposal_profiles(
            version=manifest.schema_version,
            text_root=text,
            chapter_index=chapter_index,
            base_commit=basis.commit_id,
            current_world=world,
            request=model_request,
            graph_request=graph_request,
        )
        result = execution.result
        self.proposal_calls += 1
        changes_ref = self._persist_changes(result.observed_changes, manifest.schema_version)
        if execution.graph_extraction is None:
            receipt = result.receipt
            transform_refs: tuple[ArtifactRef, ...] = ()
        else:
            transform_refs = self._persist_graph_transforms(
                execution.graph_extraction,
                manifest.schema_version,
            )
            receipt = self._compose_agent_receipt(
                result.receipt,
                changes_ref,
                transform_refs,
                self._model_call_entries(model_request, graph_request),
            )
        self.last_receipt = receipt
        return CuratorProposalResult(
            observed_changes=result.observed_changes,
            agent_receipt=receipt,
            producer_receipt=self._persist_receipt(receipt, manifest.schema_version),
            candidate_artifact=changes_ref,
        )

    async def propose_attempt(
        self,
        request: CuratorProposalAttemptRequest,
    ) -> CuratorProposalAccepted | CuratorProposalRejected:
        basis = request.basis
        text = self._revealed_text or basis.canonical_text
        world = basis.canonical_world
        if text is None or world is None:
            raise ValueError("Curator proposal requires revealed TextRoot and canonical WorldRoot")
        chapter_index = _chapter_index(request.request)
        model_request = self._request_factory(
            f"curator.replay.{chapter_index}.proposal-{request.attempt_no}",
            AgentMode.REPLAY,
        ).model_copy(update={"request_id": request.model_request_id})
        graph_request = self._graph_model_request(model_request)
        feedback = (
            None
            if request.feedback_artifact_ref is None
            else self._artifacts.read_verified(request.feedback_artifact_ref).decode("utf-8")
        )
        manifest = _basis_manifest(basis)
        started_at = datetime.now(UTC)
        print(
            f"[measure] ch{chapter_index} attempt {request.attempt_no} start "
            f"world_entities={len(world.entities)} world_states={len(world.states)} "
            f"text_bytes={len(text.model_dump_json())}",
            flush=True,
        )
        try:
            execution = await self._run_proposal_profiles(
                version=manifest.schema_version,
                text_root=text,
                chapter_index=chapter_index,
                base_commit=basis.commit_id,
                current_world=world,
                request=model_request,
                proposal_feedback=feedback,
                graph_request=graph_request,
            )
        except (ValidationError, ModelCurationContractError) as error:
            self.proposal_calls += 1
            return self._proposal_rejected(
                request,
                model_request,
                error,
                started_at,
                manifest.schema_version,
                graph_request=graph_request,
            )
        except Exception as error:
            entries = self._model_call_entries(model_request, graph_request)
            if not entries:
                raise
            self.proposal_calls += 1
            import traceback

            traceback.print_exc()
            raise CuratorProposalTransportError(
                type(error).__name__,
                model_request_ids=tuple(entry.request_id for entry in entries),
                uncertain=any(entry.completed_at is None for entry in entries),
            ) from error

        result = execution.result
        self.proposal_calls += 1
        entries = self._model_call_entries(model_request, graph_request)
        call_refs = tuple(
            self._persist_model_call_entry(entry, manifest.schema_version) for entry in entries
        )
        raw_refs = self._persist_raw_responses(
            tuple(entry.request_id for entry in entries),
            manifest.schema_version,
        )
        primary_raw_refs = self._persist_raw_responses(
            (model_request.request_id,),
            manifest.schema_version,
        )
        changes_ref = self._persist_changes(result.observed_changes, manifest.schema_version)
        curator_transform_refs = tuple(
            self._artifacts.put(
                canonical_json_bytes(transform.model_dump(mode="json")),
                media_type,
                manifest.schema_version,
            )
            for transforms, media_type in (
                (
                    self._replay.curator.last_evidence_merge_receipts,
                    "application/vnd.novel-agent.proposal-evidence-merge-receipt+json",
                ),
                (
                    getattr(
                        self._replay.curator,
                        "last_operation_filter_receipts",
                        (),
                    ),
                    "application/vnd.novel-agent.proposal-operation-filter-receipt+json",
                ),
            )
            for transform in transforms
        )
        graph_transform_refs = (
            ()
            if execution.graph_extraction is None
            else self._persist_graph_transforms(
                execution.graph_extraction,
                manifest.schema_version,
            )
        )
        transform_refs = (*curator_transform_refs, *graph_transform_refs)
        agent_receipt = self._compose_agent_receipt(
            result.receipt,
            changes_ref,
            transform_refs,
            entries,
        )
        self.last_receipt = agent_receipt
        agent_ref = self._persist_receipt(agent_receipt, manifest.schema_version)
        receipt = CuratorProposalAttemptReceipt(
            attempt_id=request.attempt_id,
            workflow_request_id=request.request.request_id,
            run_id=request.request.run_id,
            task_id=request.request.task_id,
            attempt_no=request.attempt_no,
            base_commit=request.request.base_commit,
            boundary_id=request.request.information_boundary.boundary_id,
            configuration_fingerprint=request.request.configuration_fingerprint,
            status=CuratorProposalAttemptStatus.ACCEPTED,
            model_request_ids=tuple(entry.request_id for entry in entries),
            model_call_receipt_refs=call_refs,
            prompt_fingerprint=(
                getattr(self._replay.curator, "last_prompt_fingerprint", None)
                or sha256_id(model_request.prompt.encode("utf-8"))
            ),
            feedback_artifact_ref=request.feedback_artifact_ref,
            raw_response_refs=raw_refs,
            parsed_draft_ref=primary_raw_refs[-1] if primary_raw_refs else None,
            normalized_output_ref=changes_ref,
            output_hashes=tuple(
                entry.raw_response_hash for entry in entries if entry.raw_response_hash is not None
            ),
            agent_execution_receipt_ref=agent_ref,
            producer_receipt_ref=agent_ref,
            transform_receipt_refs=transform_refs,
            provider_call_count=len(entries),
            transport_attempt_count=len(entries),
            input_tokens=sum(
                entry.call_record.usage.input_tokens
                for entry in entries
                if entry.call_record is not None
            ),
            output_tokens=sum(
                entry.call_record.usage.output_tokens
                for entry in entries
                if entry.call_record is not None
            ),
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )
        return CuratorProposalAccepted(
            observed_changes=result.observed_changes,
            attempt_receipt=receipt,
        )

    def _graph_model_request(self, request: ModelRequest) -> ModelRequest | None:
        if self._graph_curator is None:
            return None
        suffix = ".graph"
        request_id = request.request_id.root[: 128 - len(suffix)] + suffix
        return request.model_copy(
            update={
                "request_id": StableId(request_id),
                "trace_id": f"{request.trace_id}.graph",
                "scheduling_stage": "curator_graph_extraction",
            }
        )

    async def _run_proposal_profiles(
        self,
        *,
        version: SchemaVersion,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        current_world: WorldRootDocument,
        request: ModelRequest,
        graph_request: ModelRequest | None,
        proposal_feedback: str | None = None,
    ) -> _CuratorProposalExecution:
        if self._script is not None:
            self._script(request, AgentMode.REPLAY)
            if graph_request is not None:
                self._script(graph_request, AgentMode.REPLAY)

        replay_call = self._replay.run(
            version=version,
            text_root=text_root,
            chapter_index=chapter_index,
            base_commit=base_commit,
            current_world=current_world,
            request=request,
            proposal_feedback=proposal_feedback,
        )
        if self._graph_curator is None or graph_request is None:
            result, _ = await replay_call
            return _CuratorProposalExecution(result=result)

        graph_curator = self._graph_curator
        replay_task = asyncio.create_task(replay_call)

        async def run_graph_profile() -> tuple[WorldGraphCandidateBatch, Any] | None:
            try:
                return await graph_curator.extract_graph_candidates(
                    text_root,
                    chapter_index,
                    base_commit,
                    current_world,
                    graph_request,
                )
            except ValidationError:
                # Graph extraction is an additive channel.  An invalid graph
                # batch must not discard a valid ordinary Curator proposal.
                return None

        graph_task = asyncio.create_task(run_graph_profile())
        try:
            replay_output, graph_output = await asyncio.gather(replay_task, graph_task)
        except BaseException:
            replay_task.cancel()
            graph_task.cancel()
            await asyncio.gather(replay_task, graph_task, return_exceptions=True)
            raise

        result, _ = replay_output
        if graph_output is None:
            return _CuratorProposalExecution(result=result)
        graph_batch, _ = graph_output
        try:
            provisional_world = WorldOverlay().apply(
                current_world,
                result.observed_changes,
                canonical_commit=base_commit,
            )
            graph_extraction = self._graph_extraction.run(
                provisional_world,
                text_root,
                candidate_batches=(graph_batch,),
                base_commit=base_commit,
            )
        except ValueError as error:
            raise ModelCurationContractError("graph candidate admission failed") from error

        graph_operations = graph_extraction.change_set.operations
        if graph_operations:
            operations = (*result.observed_changes.operations, *graph_operations)
            identity = sha256_id(
                canonical_json_bytes(
                    {
                        "base_commit": base_commit.root,
                        "source_artifact": result.observed_changes.source_artifact.artifact_id.root,
                        "operation_ids": tuple(item.operation_id.root for item in operations),
                    }
                )
            )
            observed_changes = ObservedChangeSet(
                change_set_id=StableId(f"changes.curator-graph.{identity.root[7:39]}"),
                base_commit=base_commit,
                source_artifact=result.observed_changes.source_artifact,
                operations=operations,
            )
            result = result.model_copy(update={"observed_changes": observed_changes})
        return _CuratorProposalExecution(
            result=result,
            graph_extraction=graph_extraction,
        )

    def _model_call_entries(
        self,
        request: ModelRequest,
        graph_request: ModelRequest | None,
    ) -> tuple[ModelCallLedgerEntry, ...]:
        ledger = self._replay.curator.gateway.call_ledger
        by_id = {
            entry.request_id: entry
            for prefix in (
                request.request_id.root,
                *((graph_request.request_id.root,) if graph_request is not None else ()),
            )
            for entry in ledger.list_for_prefix(prefix)
        }
        return tuple(sorted(by_id.values(), key=lambda entry: entry.request_id.root))

    def _persist_graph_transforms(
        self,
        extraction: WorldGraphExtractionResult,
        version: SchemaVersion,
    ) -> tuple[ArtifactRef, ...]:
        batch_refs = tuple(
            self._artifacts.put(
                canonical_json_bytes(batch.model_dump(mode="json")),
                "application/vnd.novel-agent.world-graph-candidate-batch+json",
                version,
            )
            for batch in extraction.candidate_batches
        )
        receipt_ref = self._artifacts.put(
            canonical_json_bytes(extraction.receipt.model_dump(mode="json")),
            "application/vnd.novel-agent.world-graph-extraction-receipt+json",
            version,
        )
        return (*batch_refs, receipt_ref)

    @staticmethod
    def _compose_agent_receipt(
        receipt: AgentExecutionReceipt,
        changes_ref: ArtifactRef,
        transform_refs: tuple[ArtifactRef, ...],
        entries: tuple[ModelCallLedgerEntry, ...],
    ) -> AgentExecutionReceipt:
        calls = tuple(entry.call_record for entry in entries if entry.call_record is not None)
        started_at = min((receipt.started_at, *(call.started_at for call in calls)))
        completed_at = max((receipt.completed_at, *(call.completed_at for call in calls)))
        return receipt.model_copy(
            update={
                "output_artifacts": (changes_ref, *transform_refs),
                "model_call_ids": tuple(entry.request_id for entry in entries),
                "started_at": started_at,
                "completed_at": completed_at,
                "latency_ms": max(int((completed_at - started_at).total_seconds() * 1000), 0),
            }
        )

    def _proposal_rejected(
        self,
        request: CuratorProposalAttemptRequest,
        model_request: ModelRequest,
        error: ValidationError | ModelCurationContractError,
        started_at: datetime,
        version: SchemaVersion,
        *,
        graph_request: ModelRequest | None = None,
    ) -> CuratorProposalRejected:
        entries = self._model_call_entries(model_request, graph_request)
        call_refs = tuple(self._persist_model_call_entry(entry, version) for entry in entries)
        raw_refs = self._persist_raw_responses(
            tuple(entry.request_id for entry in entries), version
        )
        if isinstance(error, ValidationError):
            errors = error.errors(include_url=False, include_input=False)
            paths = tuple(".".join(str(part) for part in item["loc"]) or "$" for item in errors)
            extra_fields = sorted(
                {
                    key
                    for item in error.errors(include_url=False)
                    if item["type"] == "extra_forbidden"
                    for key in (item.get("input") or {})
                }
            )
            if extra_fields:
                detail = (
                    "Curator Draft failed the structured domain contract; remove the "
                    f"extra fields: {', '.join(extra_fields)}"
                )
            else:
                validation_details = "; ".join(
                    f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                    for item in errors[:2]
                )
                detail = (
                    "Curator Draft schema error: "
                    f"{validation_details}. Relation records require predicate, subject_id, "
                    "object_id, valid_time, truth_class and never value; state records use "
                    "value."
                )[:240]
            kind = ProposalRejectionKind.SCHEMA_REJECTED
            stage = ProposalRejectionStage.STRUCTURED_SCHEMA
            reason_code = "CURATOR_PROPOSAL_SCHEMA_REJECTED"
            retryable = True
            operation_indexes: tuple[int, ...] = ()
            json_pointers = paths
            violation_rule: str | None = "schema_validation"
        elif isinstance(error, CuratorProposalSemanticRejected):
            paths = error.json_pointers
            reason_code = error.reason_code
            operation_indexes = error.operation_indexes
            violation_rule = error.violation_rule
            if error.information_boundary:
                detail = "Proposal evidence exceeds the frozen information boundary"
                kind = ProposalRejectionKind.INVALID_EVIDENCE
                stage = ProposalRejectionStage.INFORMATION_BOUNDARY
                retryable = False
            elif reason_code in {
                "CURATOR_PROPOSAL_INVALID_EVIDENCE",
                "CURATOR_PROPOSAL_EVIDENCE_UNRELATED",
                "CURATOR_PROPOSAL_EVIDENCE_UNSUPPORTED",
                "CURATOR_PROPOSAL_EVIDENCE_NEEDS_VERIFICATION",
                "CURATOR_PROPOSAL_EVIDENCE_UNRESOLVED",
            }:
                detail = "Proposal evidence is invalid or unsupported"
                kind = ProposalRejectionKind.INVALID_EVIDENCE
                stage = ProposalRejectionStage.SEMANTIC_CONTRACT
                retryable = True
            elif reason_code == "CURATOR_PROPOSAL_EMPTY_DELTA_UNVERIFIED":
                detail = "Empty proposal lacks verified no-durable-delta proof"
                kind = ProposalRejectionKind.INCOMPLETE_DELTA
                stage = ProposalRejectionStage.SEMANTIC_CONTRACT
                retryable = True
            elif reason_code == "CURATOR_PROPOSAL_DANGLING_ENTITY_REFERENCE":
                detail = "Proposal references an entity absent from Canonical World"
                kind = ProposalRejectionKind.DANGLING_ENTITY_REFERENCE
                stage = ProposalRejectionStage.SEMANTIC_CONTRACT
                retryable = True
            else:
                detail = "Normalized targets collide with different semantic payloads"
                kind = ProposalRejectionKind.NORMALIZED_TARGET_COLLISION
                stage = ProposalRejectionStage.TRUSTED_NORMALIZATION
                retryable = True
            json_pointers = paths
        else:
            paths = ()
            detail = "Curator Draft failed trusted semantic validation"
            kind = (
                ProposalRejectionKind.CHAPTER_MISMATCH
                if "chapter differs" in str(error)
                else ProposalRejectionKind.SCOPE_VIOLATION
            )
            stage = ProposalRejectionStage.SEMANTIC_CONTRACT
            reason_code = (
                "CURATOR_PROPOSAL_CHAPTER_MISMATCH"
                if kind is ProposalRejectionKind.CHAPTER_MISMATCH
                else "CURATOR_PROPOSAL_SEMANTIC_REJECTED"
            )
            retryable = True
            operation_indexes = ()
            json_pointers = paths
            violation_rule = None
        output_hash = next(
            (
                entry.raw_response_hash
                for entry in reversed(entries)
                if entry.raw_response_hash is not None
            ),
            None,
        )
        from novel_agent.services.proposal_finding_signature import (
            extract_block_or_candidate_ids,
            proposal_finding_signature,
        )

        feedback_lines = (
            error.safe_feedback
            if isinstance(error, CuratorProposalSemanticRejected) and error.safe_feedback
            else ()
        )
        # Finding signature must ignore output_hash so identical defects collapse.
        signature = proposal_finding_signature(
            reason_code=reason_code,
            rejection_stage=stage,
            json_pointers=json_pointers,
            violation_rule=violation_rule or reason_code,
            block_or_candidate_ids=extract_block_or_candidate_ids(feedback_lines),
        )
        rejection = CuratorProposalRejection(
            rejection_id=StableId(f"proposal-rejection.{request.attempt_id.root}"),
            attempt_id=request.attempt_id,
            workflow_request_id=request.request.request_id,
            base_commit=request.request.base_commit,
            stage=stage,
            kind=kind,
            reason_code=reason_code,
            retryable=retryable,
            rejection_signature=signature,
            output_hash=output_hash,
            conflicts=error.conflicts if isinstance(error, CuratorProposalSemanticRejected) else (),
            validation_error_paths=paths,
            safe_feedback=(
                error.safe_feedback
                if isinstance(error, CuratorProposalSemanticRejected) and error.safe_feedback
                else (detail,)
            ),
            operation_indexes=operation_indexes,
            json_pointers=json_pointers,
            violation_rule=violation_rule,
            raw_draft_ref=raw_refs[-1] if raw_refs else None,
            created_at=datetime.now(UTC),
        )
        rejection_ref = self._artifacts.put(
            canonical_json_bytes(rejection.model_dump(mode="json")),
            "application/vnd.novel-agent.curator-proposal-rejection+json",
            version,
        )
        receipt = CuratorProposalAttemptReceipt(
            attempt_id=request.attempt_id,
            workflow_request_id=request.request.request_id,
            run_id=request.request.run_id,
            task_id=request.request.task_id,
            attempt_no=request.attempt_no,
            base_commit=request.request.base_commit,
            boundary_id=request.request.information_boundary.boundary_id,
            configuration_fingerprint=request.request.configuration_fingerprint,
            status=CuratorProposalAttemptStatus.REJECTED,
            model_request_ids=tuple(entry.request_id for entry in entries),
            model_call_receipt_refs=call_refs,
            prompt_fingerprint=(
                getattr(self._replay.curator, "last_prompt_fingerprint", None)
                or sha256_id(model_request.prompt.encode("utf-8"))
            ),
            feedback_artifact_ref=request.feedback_artifact_ref,
            raw_response_refs=raw_refs,
            output_hashes=tuple(
                entry.raw_response_hash for entry in entries if entry.raw_response_hash is not None
            ),
            rejection_ref=rejection_ref,
            provider_call_count=len(entries),
            transport_attempt_count=len(entries),
            input_tokens=sum(
                entry.call_record.usage.input_tokens
                for entry in entries
                if entry.call_record is not None
            ),
            output_tokens=sum(
                entry.call_record.usage.output_tokens
                for entry in entries
                if entry.call_record is not None
            ),
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )
        return CuratorProposalRejected(rejection=rejection, attempt_receipt=receipt)

    def _persist_raw_responses(
        self,
        request_ids: tuple[StableId, ...],
        version: SchemaVersion,
    ) -> tuple[ArtifactRef, ...]:
        raw = self._replay.curator.gateway.raw_responses
        return tuple(
            self._artifacts.put(
                raw[request_id.root].encode("utf-8"),
                "application/vnd.novel-agent.raw-model-response+text",
                version,
            )
            for request_id in request_ids
            if request_id.root in raw
        )

    def _persist_model_call(self, call: Any, version: SchemaVersion) -> ArtifactRef:
        return self._artifacts.put(
            canonical_json_bytes(call.model_dump(mode="json")),
            "application/vnd.novel-agent.model-call-record+json",
            version,
        )

    def _persist_model_call_entry(
        self,
        entry: ModelCallLedgerEntry,
        version: SchemaVersion,
    ) -> ArtifactRef:
        if entry.call_record is not None:
            return self._persist_model_call(entry.call_record, version)
        return self._artifacts.put(
            canonical_json_bytes(entry.model_dump(mode="json")),
            "application/vnd.novel-agent.model-call-ledger-entry+json",
            version,
        )

    async def repair(self, request: CuratorRepairRequest) -> CuratorRepairResult:
        basis = request.basis
        text = self._revealed_text or basis.canonical_text
        world = basis.canonical_world
        if text is None or world is None:
            raise ValueError("Curator repair requires revealed TextRoot and canonical WorldRoot")
        chapter_index = _chapter_index(request.request)
        model_request = self._request_factory(
            f"curator.repair.{chapter_index}.{request.parent_candidate.revision_no}",
            AgentMode.CURATOR_REPAIR,
        )
        if self._repair_script is not None:
            self._repair_script(model_request, AgentMode.CURATOR_REPAIR)
        parent_payload = self._artifacts.read_verified(request.parent_candidate.candidate_artifact)
        from novel_agent.domain.memory_write import MemoryWriteCandidatePayload

        parent_changes = MemoryWriteCandidatePayload.model_validate_json(
            parent_payload, strict=True
        ).observed_changes
        manifest = _basis_manifest(basis)
        result = await self._repair.run(
            version=manifest.schema_version,
            text_root=text,
            chapter_index=chapter_index,
            base_commit=basis.commit_id,
            current_world=world,
            parent_candidate=request.parent_candidate,
            parent_changes=parent_changes,
            validation=request.validation,
            directive=request.directive,
            request=request,
            model_request=model_request,
        )
        self.repair_calls += 1
        self.last_receipt = result.agent_receipt
        if result.agent_receipt is None:
            return result
        return result.model_copy(
            update={
                "producer_receipt": self._persist_receipt(
                    result.agent_receipt, manifest.schema_version
                ),
                "candidate_artifact": self._persist_changes(
                    result.observed_changes, manifest.schema_version
                ),
            }
        )

    def _persist_changes(self, changes: Any, version: SchemaVersion) -> ArtifactRef:
        return self._artifacts.put(
            canonical_json_bytes(changes.model_dump(mode="json")),
            "application/vnd.novel-agent.observed-change-set+json",
            version,
        )

    def _persist_receipt(
        self, receipt: AgentExecutionReceipt, version: SchemaVersion
    ) -> ArtifactRef:
        return self._artifacts.put(
            canonical_json_bytes(receipt.model_dump(mode="json")),
            "application/vnd.novel-agent.agent-execution-receipt+json",
            version,
        )


class LegacyRiskClassifierAdapter:
    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts
        from novel_agent.services.guardian import PatchRiskClassifier

        self._classifier = PatchRiskClassifier()

    def assess(self, candidate: Any, validation: ValidationDecision | None) -> PatchRiskAssessment:
        if validation is None:
            raise ValueError("risk classification requires validation")
        changes = _candidate_changes(self._artifacts, candidate)
        return self._classifier.assess(changes, _validation_report(validation, changes))


class LegacyWriteGateAdapter:
    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts
        from novel_agent.services.guardian import GuardianWriteGate

        self._gate = GuardianWriteGate()

    def decide(
        self,
        candidate: Any,
        validation: ValidationDecision | None,
        risk: PatchRiskAssessment,
        *,
        guardian: GuardianDecision | None = None,
    ) -> WriteGateDecision:
        if validation is None:
            raise ValueError("write gate requires validation")
        changes = _candidate_changes(self._artifacts, candidate)
        decision = self._gate.decide(
            changes,
            _validation_report(validation, changes),
            risk,
            guardian=guardian,
        )
        return decision.model_copy(
            update={
                "change_set_id": candidate.candidate_id,
                "base_commit": candidate.base_commit,
            }
        )


class LegacyGuardianPortAdapter:
    def __init__(
        self,
        agent: GuardianRiskReviewAgent,
        artifacts: ArtifactRepository,
        request_factory: ModelRequestFactory,
        script: ScriptCallback | None = None,
        *,
        evidence_root: Callable[[], TextRootDocument | None] | None = None,
    ) -> None:
        self._agent = agent
        self._artifacts = artifacts
        self._request_factory = request_factory
        self._script = script
        self._evidence_root = evidence_root
        self.calls = 0

    async def review(self, request: GuardianReviewRequest) -> GuardianReviewResult:
        changes = _candidate_changes(self._artifacts, request.candidate)
        model_request = self._request_factory(
            f"guardian.{_chapter_index(request.request)}", AgentMode.RISK_REVIEW
        )
        if self._script is not None:
            self._script(model_request, AgentMode.RISK_REVIEW)
        manifest = _basis_manifest(request.basis)
        decision, _ = await self._agent.review(
            version=manifest.schema_version,
            changes=changes,
            validation=_validation_report(request.validation, changes),
            risk=request.risk,
            request=model_request,
            evidence_root=self._evidence_root()
            if self._evidence_root is not None
            else request.basis.canonical_text,
        )
        self.calls += 1
        return GuardianReviewResult(
            decision=decision,
            receipt=self._artifacts.put(
                canonical_json_bytes(decision.receipt.model_dump(mode="json")),
                "application/vnd.novel-agent.guardian-receipt+json",
                manifest.schema_version,
            ),
        )


class InformationBoundaryRegistryAdapter:
    """Create and register trusted visibility/derivation receipts."""

    def __init__(self, port: Any, artifacts: ArtifactRepository) -> None:
        self.port = port
        self.artifacts = artifacts

    def register_visibility(
        self,
        *,
        source: ArtifactRef,
        boundary: InformationBoundary,
        position: NarrativePosition,
        access_scope: AccessScope,
        provenance: SourceProvenance,
    ) -> SourceVisibilityReceipt:
        receipt = SourceVisibilityReceipt(
            receipt_id=StableId(
                f"visibility.{boundary.boundary_id.root}.{source.artifact_id.root[7:23]}"
            ),
            source_artifact=source,
            boundary_id=boundary.boundary_id,
            visible_through=position,
            access_scope=access_scope,
            provenance=provenance,
            issuer=StableId("issuer.teacher-forced.trusted-adapter"),
            receipt_hash=ArtifactId("sha256:" + "1" * 64),
        )
        payload = receipt.model_dump(mode="json")
        payload["receipt_hash"] = None
        receipt = receipt.model_copy(
            update={"receipt_hash": sha256_id(canonical_json_bytes(payload))}
        )
        self.port.register_visibility(receipt)
        return receipt

    def register_derivation(
        self,
        *,
        output: ArtifactRef,
        inputs: tuple[ArtifactRef, ...],
        visibility_receipts: tuple[SourceVisibilityReceipt, ...],
        boundary: InformationBoundary,
        policy: ContractRef,
        position: NarrativePosition,
        access_scope: AccessScope,
    ) -> ArtifactRef:
        receipt = BoundaryPropagationReceipt(
            receipt_id=StableId(f"propagation.{output.artifact_id.root[7:31]}"),
            boundary_id=boundary.boundary_id,
            base_commit=boundary.base_commit,
            input_source_artifact_refs=inputs,
            source_visibility_receipt_refs=tuple(
                _visibility_ref(item) for item in visibility_receipts
            ),
            output_artifact_hash=output.artifact_id,
            builder_policy_hash=policy.content_hash,
            effective_visible_through=position,
            effective_access_scope=access_scope,
            receipt_hash=ArtifactId("sha256:" + "1" * 64),
        )
        payload = receipt.model_dump(mode="json")
        payload["receipt_hash"] = None
        receipt = receipt.model_copy(
            update={"receipt_hash": sha256_id(canonical_json_bytes(payload))}
        )
        receipt_ref = self.artifacts.put(
            canonical_json_bytes(receipt.model_dump(mode="json")),
            "application/vnd.novel-agent.boundary-propagation-receipt+json",
            output.schema_version,
        )
        self.port.register_derivation(
            receipt,
            receipt_artifact=receipt_ref,
            output_artifact=output,
        )
        return receipt_ref


def _visibility_ref(receipt: SourceVisibilityReceipt) -> ArtifactRef:
    data = canonical_json_bytes(receipt.model_dump(mode="json"))
    return ArtifactRef(
        artifact_id=receipt.receipt_hash,
        media_type="application/vnd.novel-agent.source-visibility-receipt+json",
        byte_length=len(data),
        schema_version=receipt.source_artifact.schema_version,
    )


def _candidate_changes(artifacts: ArtifactRepository, candidate: Any) -> Any:
    from novel_agent.domain.memory_write import MemoryWriteCandidatePayload

    return MemoryWriteCandidatePayload.model_validate_json(
        artifacts.read_verified(candidate.candidate_artifact), strict=True
    ).observed_changes


def _basis_manifest(basis: CanonicalWriteBasis) -> RootManifest:
    if basis.root_manifest is None:
        raise ValueError("canonical basis has no root manifest")
    return basis.root_manifest


def _validation_report(
    validation: ValidationDecision,
    changes: Any,
) -> ValidationReport:
    findings = tuple(
        ValidationFinding(
            code=item.code,
            severity=item.severity.value,
            message=item.message,
            evidence_refs=item.evidence_refs,
        )
        for item in validation.findings
    )
    status = (
        ValidationStatus.PASSED
        if validation.disposition.value == "pass"
        else ValidationStatus.NEEDS_REVIEW
        if validation.disposition.value == "review_required"
        else ValidationStatus.FAILED
    )
    return ValidationReport(
        report_id=StableId(f"validation-report.{changes.change_set_id.root}"),
        bundle_id=StableId(f"bundle.validation.{changes.change_set_id.root}"),
        status=status,
        findings=findings,
        schema_version=SchemaVersion("0.1.0"),
        validation_profile=validation.deterministic_profile,
        validated_at=validation.validated_at,
    )


def _validate_durable_commit_request(
    request: DurableMemoryWriteCommitRequest,
) -> MemoryWriteCommitResult | None:
    candidate = request.candidate
    materialization = request.materialization
    bundle = request.bundle
    validation = request.validation
    gate = request.gate
    reason: str | None = None
    if candidate.base_commit != request.base_commit:
        reason = "candidate base commit does not match request"
    elif materialization.candidate_id != candidate.candidate_id:
        reason = "materialization candidate does not match request candidate"
    elif materialization.candidate_content_hash != candidate.content_hash:
        reason = "materialization content hash does not match request candidate"
    elif materialization.bundle is not None and materialization.bundle != bundle:
        reason = "materialization bundle does not match request bundle"
    elif materialization.proposed_roots_hash != sha256_id(
        canonical_json_bytes(bundle.proposed_roots.model_dump(mode="json"))
    ):
        reason = "materialization roots hash does not match bundle"
    elif bundle.project_id != request.project_id:
        reason = "bundle project does not match request"
    elif bundle.base_commit != request.base_commit:
        reason = "bundle base commit does not match request"
    elif bundle.observed_changes.base_commit != request.base_commit:
        reason = "observed change base commit does not match request"
    elif bundle.proposed_roots.parent_commit_ids != (request.base_commit,):
        reason = "proposed roots must have exactly the base commit as parent"
    elif validation.candidate_id != candidate.candidate_id:
        reason = "validation candidate does not match request candidate"
    elif validation.candidate_content_hash != candidate.content_hash:
        reason = "validation content hash does not match request candidate"
    elif validation.base_commit != request.base_commit:
        reason = "validation base commit does not match request"
    elif validation.proposed_roots_hash != materialization.proposed_roots_hash:
        reason = "validation roots hash does not match materialization"
    elif validation.disposition is not ValidationDisposition.PASS:
        reason = "validation disposition has not passed"
    elif gate.change_set_id != candidate.candidate_id:
        reason = "write gate candidate does not match request candidate"
    elif gate.base_commit != request.base_commit:
        reason = "write gate base commit does not match request"
    elif gate.outcome is not WriteGateOutcome.ALLOW_COMMIT:
        reason = "write gate has not allowed commit"
    if reason is None:
        return None
    return MemoryWriteCommitResult(
        request_id=request.request_id,
        status=MemoryWriteCommitStatus.REJECTED,
        reason=reason,
    )


def _chapter_index(request: MemoryWriteWorkflowRequest) -> int:
    trigger = request.trigger
    return int(getattr(trigger, "chapter_index", 0))


__all__ = [
    "CommitServiceMemoryWriteAdapter",
    "InformationBoundaryRegistryAdapter",
    "LegacyGuardianPortAdapter",
    "LegacyRiskClassifierAdapter",
    "LegacyWriteGateAdapter",
    "ProjectionServiceReadinessAdapter",
    "RepositoryCanonicalReadAdapter",
    "TeacherForcedCuratorPort",
]
