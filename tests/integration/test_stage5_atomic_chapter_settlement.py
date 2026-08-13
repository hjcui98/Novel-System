from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from novel_agent.adapters.memory_write import (
    CommitServiceMemoryWriteAdapter,
    InformationBoundaryRegistryAdapter,
    ProjectionServiceReadinessAdapter,
    RepositoryCanonicalReadAdapter,
)
from novel_agent.adapters.runtime.chapter_settlement import (
    AtomicChapterSettlementAdapter,
    ChapterSettlementPolicy,
)
from novel_agent.adapters.runtime.materializers import DraftCandidateMaterializer
from novel_agent.domain.artifacts import RootKind, TextRootRef
from novel_agent.domain.benchmark import ChapterDocument, SceneDocument, TextRootDocument
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ChangeOperation,
    ChangeOperationType,
    ObservedChangeSet,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.creative_runtime import (
    AcceptedCandidateBinding,
    ActorKind,
    CandidateBinding,
    CandidateKind,
)
from novel_agent.domain.ids import ProjectId, RunId, StableId, TaskId
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite, WorldRootDocument
from novel_agent.domain.memory_write import (
    CuratorProposalAccepted,
    CuratorProposalAttemptStatus,
    MemoryWriteWorkflowStatus,
    ValidationDecision,
    ValidationDisposition,
)
from novel_agent.domain.stage2 import ContractRef
from novel_agent.domain.text import TextBlock
from novel_agent.ports.memory_write import CuratorProposalAttemptRequest
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.information_boundary import InformationBoundaryPort
from novel_agent.services.memory_write_workflow import LocalMemoryWriteWorkflow
from novel_agent.services.pre_candidate_repair import requested_attempt
from novel_agent.services.projection import (
    DerivedProjectionService,
    DerivedSnapshotRepository,
    ProjectionOutboxRepository,
    snapshot_id_for_commit,
)
from novel_agent.services.text_timeline import SequentialTextRootService
from tests.unit.test_stage5_production_factories import HASH, VERSION, _canonical


class _ProjectionBuilder:
    def build(self, project_id: ProjectId, source_commit: Any) -> DerivedSnapshotLite:
        del project_id
        suffix = source_commit.root.removeprefix("sha256:")
        return DerivedSnapshotLite(
            snapshot_id=snapshot_id_for_commit(source_commit),
            source_commit=source_commit,
            anchor_build_id=StableId(f"anchor.{suffix[:24]}"),
            anchor_index_version="anchor-v1",
            grounded_index_version="grounded-v1",
            embedding_profile="offline-v1",
            fusion_profile="rrf-v1",
            build_status=DerivedBuildStatus.EXACT,
            published_at=datetime.now(UTC),
        )


class _PassValidator:
    def validate(self, candidate: Any, materialization: Any, basis: Any) -> ValidationDecision:
        return ValidationDecision(
            decision_id=StableId(f"validation.{candidate.candidate_id.root}"[:128]),
            candidate_id=candidate.candidate_id,
            candidate_content_hash=candidate.content_hash,
            materialization_receipt=materialization.materialization_receipt,
            proposed_roots_hash=materialization.proposed_roots_hash,
            base_commit=basis.commit_id,
            disposition=ValidationDisposition.PASS,
            deterministic_profile="stage5-chapter-settlement-test",
            validated_at=datetime.now(UTC),
        )


class _StateChangingCurator:
    def __init__(self, artifacts: Any, state: Any, source: Any) -> None:
        self._artifacts = artifacts
        self._state = state
        self._source = source

    async def propose_attempt(
        self, request: CuratorProposalAttemptRequest
    ) -> CuratorProposalAccepted:
        updated_state = self._state.model_copy(update={"value": {"status": "settled"}})
        operation = ChangeOperation(
            operation_id=StableId("operation.chapter.21.state"),
            root_kind=RootKind.WORLD,
            operation=ChangeOperationType.REPLACE,
            target_id=self._state.state_id,
            payload={"record_type": "state", "record": updated_state.model_dump(mode="json")},
        )
        observed = ObservedChangeSet(
            change_set_id=StableId("changes.chapter.21"),
            base_commit=request.request.base_commit,
            source_artifact=self._source,
            operations=(operation,),
        )
        normalized = self._artifacts.put(
            canonical_json_bytes(observed.model_dump(mode="json")),
            "application/vnd.novel-agent.observed-change-set+json",
            VERSION,
        )
        call_ref = self._artifacts.put(
            canonical_json_bytes({"request_id": request.model_request_id.root}),
            "application/vnd.novel-agent.model-call-record+json",
            VERSION,
        )
        generic_receipt = self._artifacts.put(b"{}", "application/json", VERSION)
        receipt = requested_attempt(
            attempt_id=request.attempt_id,
            workflow_request_id=request.request.request_id,
            run_id=request.request.run_id,
            task_id=request.request.task_id,
            attempt_no=request.attempt_no,
            base_commit=request.request.base_commit,
            boundary_id=request.request.information_boundary.boundary_id,
            configuration_fingerprint=request.request.configuration_fingerprint,
            prompt_fingerprint=request.request.configuration_fingerprint,
        ).model_copy(
            update={
                "status": CuratorProposalAttemptStatus.ACCEPTED,
                "model_request_ids": (request.model_request_id,),
                "model_call_receipt_refs": (call_ref,),
                "normalized_output_ref": normalized,
                "output_hashes": (normalized.artifact_id,),
                "agent_execution_receipt_ref": generic_receipt,
                "producer_receipt_ref": generic_receipt,
                "provider_call_count": 1,
                "transport_attempt_count": 1,
                "completed_at": datetime.now(UTC),
            }
        )
        return CuratorProposalAccepted(observed_changes=observed, attempt_receipt=receipt)


class _TextMaterializer:
    def __init__(self, artifacts: Any, commits: Any, base: Any, source: Any) -> None:
        self._artifacts = artifacts
        self._commits = commits
        self._base = base
        self._source = source

    def materialize(self, accepted: AcceptedCandidateBinding):
        manifest = self._commits.load_manifest(self._base)
        current = TextRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.text_root), strict=True
        )
        chapter = ChapterDocument(
            chapter_id=StableId("chapter.21"),
            chapter_index=21,
            title="Chapter 21",
            scenes=(
                SceneDocument(
                    scene_id=StableId("scene.chapter.21.0"),
                    scene_index=0,
                    blocks=(
                        TextBlock(
                            block_id=StableId("block.chapter.21.0"),
                            chapter_id=StableId("chapter.21"),
                            scene_id=StableId("scene.chapter.21.0"),
                            narrative_index=0,
                            text=self._artifacts.read_verified(self._source).decode(),
                        ),
                    ),
                ),
            ),
        )
        updated, _receipt = SequentialTextRootService().append(
            current, accepted.candidate.candidate_id, chapter
        )
        text_artifact = self._artifacts.put(
            canonical_json_bytes(updated.model_dump(mode="json")),
            "application/vnd.novel-agent.text-root+json",
            VERSION,
        )
        text_ref = TextRootRef(**text_artifact.model_dump(mode="python"))
        proposed = manifest.model_copy(
            update={"text_root": text_ref, "parent_commit_ids": (self._base,)}
        )
        observed = ObservedChangeSet(
            change_set_id=StableId("changes.text.chapter.21"),
            base_commit=self._base,
            source_artifact=self._source,
        )
        bundle = CandidateChangeBundle(
            bundle_id=StableId("bundle.text.chapter.21"),
            project_id=accepted.project_id,
            run_id=accepted.run_id,
            base_commit=self._base,
            observed_changes=observed,
            proposed_roots=proposed,
            produced_artifacts=(text_ref,),
        )
        return bundle, ValidationReport(
            report_id=StableId("validation.text.chapter.21"),
            bundle_id=bundle.bundle_id,
            status=ValidationStatus.PASSED,
            schema_version=VERSION,
            validated_at=datetime.now(UTC),
        )


def test_atomic_chapter_settlement_updates_text_and_world_in_one_commit(
    tmp_path: Path,
) -> None:
    artifacts, commits, base, _text = _canonical(tmp_path)
    manifest = commits.load_manifest(base)
    world = WorldRootDocument.model_validate_json(
        artifacts.read_verified(manifest.world_root), strict=True
    )
    source = artifacts.put(
        b"Lin enters the tower and lowers her injured arm.", "text/plain", VERSION
    )
    candidate = CandidateBinding(
        candidate_id=StableId("draft-candidate.chapter.21"),
        kind=CandidateKind.DRAFT,
        artifact_ref=source,
        candidate_hash=source.artifact_id.root,
        basis_commit=base,
        basis_snapshot=StableId("snapshot.chapter.20"),
        affects_future_plan=True,
    )
    accepted = AcceptedCandidateBinding(
        acceptance_id=StableId("acceptance.chapter.21"),
        command_id=StableId("command.chapter.21"),
        project_id=ProjectId("project.test"),
        run_id=RunId("run.chapter-settlement"),
        task_id=TaskId("task.chapter-settlement.accept"),
        candidate=candidate,
        actor_kind=ActorKind.POLICY,
        actor_id="test-policy",
        accepted_at=datetime.now(UTC),
        expected_project_commit=base,
    )
    boundary = InformationBoundaryPort(
        artifact_reader=artifacts,
        trusted_policy_hashes=(HASH,),
    )
    registry = InformationBoundaryRegistryAdapter(boundary, artifacts)
    session_factory = cast(Any, commits)._session_factory
    projections = DerivedProjectionService(
        ProjectionOutboxRepository(session_factory), _ProjectionBuilder()
    )
    snapshots = DerivedSnapshotRepository(session_factory)
    workflow = LocalMemoryWriteWorkflow(
        canonical_read=RepositoryCanonicalReadAdapter(commits, artifacts),
        curator=_StateChangingCurator(artifacts, world.states[0], source),
        validator=_PassValidator(),
        commit=CommitServiceMemoryWriteAdapter(commits, artifacts),
        information_boundary=boundary,
        artifacts=artifacts,
        projection=ProjectionServiceReadinessAdapter(
            projections, snapshots, artifacts
        ),
    )
    contract = ContractRef(
        contract_id=StableId("contract.chapter-settlement"),
        version=VERSION,
        content_hash=HASH,
    )
    revealed: list[TextRootDocument] = []
    adapter = AtomicChapterSettlementAdapter(
        workflow=workflow,
        draft_materializer=cast(
            DraftCandidateMaterializer,
            _TextMaterializer(artifacts, commits, base, source),
        ),
        commits=commits,
        artifacts=artifacts,
        boundary_registry=registry,
        policy=ChapterSettlementPolicy(
            curator_agent_spec=contract,
            boundary_policy_ref=contract,
            tool_policy_ref=contract,
            repair_policy_ref=contract,
            configuration_fingerprint=HASH,
        ),
        reveal_text=revealed.append,
    )

    result = asyncio.run(adapter.settle(accepted))

    assert result.status is MemoryWriteWorkflowStatus.COMMITTED
    assert result.resulting_commit is not None
    committed = commits.load_manifest(result.resulting_commit)
    assert committed.text_root.artifact_id != manifest.text_root.artifact_id
    assert committed.world_root.artifact_id != manifest.world_root.artifact_id
    committed_text = TextRootDocument.model_validate_json(
        artifacts.read_verified(committed.text_root), strict=True
    )
    committed_world = WorldRootDocument.model_validate_json(
        artifacts.read_verified(committed.world_root), strict=True
    )
    assert committed_text.chapters[-1].chapter_index == 21
    assert committed_world.states[0].value == {"status": "settled"}
    assert revealed[-1] == committed_text
