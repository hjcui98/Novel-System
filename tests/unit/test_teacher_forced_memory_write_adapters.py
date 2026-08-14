"""Failure-path coverage for the Stage 2W teacher-forced adapters."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from novel_agent.adapters.memory_write.teacher_forced import (
    CommitServiceMemoryWriteAdapter,
    LegacyGuardianPortAdapter,
    LegacyRiskClassifierAdapter,
    LegacyWriteGateAdapter,
    ProjectionServiceReadinessAdapter,
    RepositoryCanonicalReadAdapter,
    TeacherForcedCuratorPort,
    _basis_manifest,
    _validate_durable_commit_request,
)
from novel_agent.adapters.model import FakeModelEndpoint, ScriptedModelEndpoint
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.changes import ChapterChangeDraft, ObservedChangeSet, WorldRecordKind
from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite, WorldRootDocument
from novel_agent.domain.memory_write import (
    CanonicalWriteBasis,
    CuratorProposalAccepted,
    CuratorProposalRejected,
    MemoryWriteCandidatePayload,
    MemoryWriteCommitProfile,
    ProposalConflict,
    ProposalRejectionKind,
    ProposalRejectionStage,
    RepairAction,
    RepairDirective,
    ValidationDisposition,
)
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    CuratorReplayResult,
    GuardianDecision,
    GuardianOutcome,
    WriteGateDecision,
    WriteGateOutcome,
)
from novel_agent.domain.world import (
    GraphCandidatePageDraft,
    WorldGraphCandidateBatch,
)
from novel_agent.ports.memory_write import (
    CuratorProposalAttemptRequest,
    CuratorProposalRequest,
    CuratorProposalTransportError,
    CuratorRepairRequest,
    CuratorRepairResult,
    DurableMemoryWriteCommitRequest,
    GuardianReviewRequest,
    MemoryWriteCommitStatus,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.memory_write_workflow import (
    InMemoryArtifactRepository,
    InMemoryCandidateLineageRepository,
)
from novel_agent.services.model_curation import (
    CuratorProposalSemanticRejected,
    ModelCurationContractError,
)
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.projection import snapshot_id_for_commit
from tests.contract.test_memory_write_workflow_contract import (
    BASE,
    PROJECT,
    _manifest,
    _request,
)
from tests.contract.test_stage2_contract import agent_receipt
from tests.unit.test_memory_write_resume import _ready_data
from tests.unit.test_world_graph_repair import _teacher_world


def _commit_request() -> DurableMemoryWriteCommitRequest:
    artifacts = InMemoryArtifactRepository()
    data = _ready_data(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
    )
    assert data.candidate is not None
    assert data.materialization is not None
    assert data.bundle is not None
    assert data.validation is not None
    assert data.risk is not None
    roots_hash = sha256_id(canonical_json_bytes(data.bundle.proposed_roots.model_dump(mode="json")))
    materialization = data.materialization.model_copy(update={"proposed_roots_hash": roots_hash})
    validation = data.validation.model_copy(update={"proposed_roots_hash": roots_hash})
    gate = WriteGateDecision(
        decision_id=StableId("gate.teacher-forced.unit"),
        change_set_id=data.candidate.candidate_id,
        base_commit=BASE,
        outcome=WriteGateOutcome.ALLOW_COMMIT,
        risk_assessment_id=data.risk.assessment_id,
    )
    return DurableMemoryWriteCommitRequest(
        request_id=StableId("request.teacher-forced.unit"),
        project_id=PROJECT,
        base_commit=BASE,
        idempotency_key=StableId("idempotency.teacher-forced.unit"),
        commit_effect_id=StableId("effect.teacher-forced.unit"),
        request_hash=ArtifactId("sha256:" + "a" * 64),
        candidate=data.candidate,
        materialization=materialization,
        bundle=data.bundle,
        validation=validation,
        gate=gate,
    )


def _replace(
    request: DurableMemoryWriteCommitRequest,
    field: str,
    **updates: object,
) -> DurableMemoryWriteCommitRequest:
    return request.model_copy(update={field: getattr(request, field).model_copy(update=updates)})


def _replace_bundle(
    request: DurableMemoryWriteCommitRequest,
    **updates: object,
) -> DurableMemoryWriteCommitRequest:
    bundle = request.bundle.model_copy(update=updates)
    roots_hash = sha256_id(canonical_json_bytes(bundle.proposed_roots.model_dump(mode="json")))
    materialization = request.materialization.model_copy(
        update={"bundle": bundle, "proposed_roots_hash": roots_hash}
    )
    return request.model_copy(update={"bundle": bundle, "materialization": materialization})


@pytest.mark.parametrize(
    ("mutate", "reason"),
    (
        (
            lambda r: _replace(r, "candidate", base_commit=CommitId("sha256:" + "9" * 64)),
            "candidate base",
        ),
        (
            lambda r: _replace(
                r,
                "materialization",
                candidate_id=StableId("candidate.other"),
            ),
            "materialization candidate",
        ),
        (
            lambda r: _replace(
                r,
                "materialization",
                candidate_content_hash=ArtifactId("sha256:" + "9" * 64),
            ),
            "materialization content",
        ),
        (
            lambda r: _replace(
                r,
                "materialization",
                bundle=r.bundle.model_copy(update={"run_id": type(r.bundle.run_id)("run.other")}),
            ),
            "materialization bundle",
        ),
        (
            lambda r: _replace(
                r,
                "materialization",
                proposed_roots_hash=ArtifactId("sha256:" + "9" * 64),
            ),
            "materialization roots",
        ),
        (
            lambda r: _replace_bundle(
                r,
                project_id=type(r.project_id)("project.other"),
            ),
            "bundle project",
        ),
        (
            lambda r: _replace_bundle(
                r,
                base_commit=CommitId("sha256:" + "9" * 64),
            ),
            "bundle base",
        ),
        (
            lambda r: _replace_bundle(
                r,
                observed_changes=r.bundle.observed_changes.model_copy(
                    update={"base_commit": CommitId("sha256:" + "9" * 64)}
                ),
            ),
            "observed change base",
        ),
        (
            lambda r: _replace_bundle(
                r,
                proposed_roots=r.bundle.proposed_roots.model_copy(update={"parent_commit_ids": ()}),
            ),
            "proposed roots",
        ),
        (
            lambda r: _replace(
                r,
                "validation",
                candidate_id=StableId("candidate.other"),
            ),
            "validation candidate",
        ),
        (
            lambda r: _replace(
                r,
                "validation",
                candidate_content_hash=ArtifactId("sha256:" + "9" * 64),
            ),
            "validation content",
        ),
        (
            lambda r: _replace(
                r,
                "validation",
                base_commit=CommitId("sha256:" + "9" * 64),
            ),
            "validation base",
        ),
        (
            lambda r: _replace(
                r,
                "validation",
                proposed_roots_hash=ArtifactId("sha256:" + "9" * 64),
            ),
            "validation roots",
        ),
        (
            lambda r: _replace(
                r,
                "validation",
                disposition=ValidationDisposition.NON_REPAIRABLE,
            ),
            "disposition",
        ),
        (
            lambda r: _replace(r, "gate", change_set_id=StableId("candidate.other")),
            "write gate candidate",
        ),
        (
            lambda r: _replace(
                r,
                "gate",
                base_commit=CommitId("sha256:" + "9" * 64),
            ),
            "write gate base",
        ),
        (
            lambda r: _replace(
                r,
                "gate",
                outcome=WriteGateOutcome.BLOCK_VALIDATION,
            ),
            "not allowed",
        ),
    ),
)
def test_durable_commit_validation_rejects_every_broken_binding(
    mutate: Any,
    reason: str,
) -> None:
    result = _validate_durable_commit_request(mutate(_commit_request()))

    assert result is not None
    assert result.status == MemoryWriteCommitStatus.REJECTED
    assert reason in (result.reason or "")


def test_durable_commit_validation_accepts_a_fully_bound_request() -> None:
    assert _validate_durable_commit_request(_commit_request()) is None


def test_commit_adapter_maps_rejection_and_rejects_incomplete_acceptance() -> None:
    request = _commit_request()
    artifacts = InMemoryArtifactRepository()

    class Commits:
        def __init__(self, result: object) -> None:
            self.result = result

        def commit(self, _: object) -> object:
            return self.result

    rejected = SimpleNamespace(status=SimpleNamespace(value="conflicted"), reason="stale")
    result = CommitServiceMemoryWriteAdapter(
        cast(Any, Commits(rejected)),
        cast(Any, artifacts),
    ).resolve_or_replay_exact(request)
    assert result.status == "conflicted"

    accepted = SimpleNamespace(
        status=SimpleNamespace(value="accepted"),
        reason=None,
        commit_id=None,
        manifest=None,
    )
    with pytest.raises(ValueError, match="accepted without"):
        CommitServiceMemoryWriteAdapter(
            cast(Any, Commits(accepted)),
            cast(Any, artifacts),
        ).resolve_or_replay_exact(request)

    invalid = _replace(
        request,
        "candidate",
        base_commit=CommitId("sha256:" + "9" * 64),
    )
    assert (
        CommitServiceMemoryWriteAdapter(
            cast(Any, Commits(accepted)),
            cast(Any, artifacts),
        )
        .resolve_or_replay_exact(invalid)
        .status
        == MemoryWriteCommitStatus.REJECTED
    )


def test_repository_canonical_adapter_rejects_foreign_manifest() -> None:
    commits = SimpleNamespace(
        load_manifest=lambda _: _manifest(),
        current_commit=lambda _: BASE,
    )
    adapter = RepositoryCanonicalReadAdapter(
        cast(Any, commits),
        cast(Any, InMemoryArtifactRepository()),
    )

    with pytest.raises(ValueError, match="another project"):
        adapter.load_verified(type(PROJECT)("project.other"), BASE)
    assert adapter.current_commit(PROJECT) == BASE


def _snapshot(commit: CommitId, *, exact: bool = True) -> DerivedSnapshotLite:
    return DerivedSnapshotLite(
        snapshot_id=snapshot_id_for_commit(commit),
        source_commit=commit,
        anchor_build_id=StableId("build.teacher-forced.unit"),
        anchor_index_version="anchor-v1",
        grounded_index_version="grounded-v1",
        embedding_profile="embedding-v1",
        fusion_profile="fusion-v1",
        build_status=DerivedBuildStatus.EXACT if exact else DerivedBuildStatus.PARTIAL,
        published_at=datetime(2026, 7, 23, tzinfo=UTC),
    )


def test_projection_adapter_maps_worker_failure_pending_and_stale_snapshot() -> None:
    artifacts = InMemoryArtifactRepository()
    effect = StableId("effect.projection.unit")

    failing = ProjectionServiceReadinessAdapter(
        cast(
            Any,
            SimpleNamespace(process_all=lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        ),
        cast(Any, SimpleNamespace(get_for_commit=lambda _: None)),
        cast(Any, artifacts),
    )
    assert failing.request_or_read_by_effect_id(PROJECT, BASE, effect).status.value == "failed"

    pending = ProjectionServiceReadinessAdapter(
        cast(Any, SimpleNamespace(process_all=lambda: None)),
        cast(Any, SimpleNamespace(get_for_commit=lambda _: None)),
        cast(Any, artifacts),
        auto_process=False,
    )
    assert pending.await_or_check(PROJECT, BASE, effect).status.value == "pending"

    stale = ProjectionServiceReadinessAdapter(
        cast(Any, SimpleNamespace(process_all=lambda: None)),
        cast(Any, SimpleNamespace(get_for_commit=lambda _: _snapshot(BASE, exact=False))),
        cast(Any, artifacts),
        auto_process=False,
    )
    result = stale.await_or_check(PROJECT, BASE, effect)
    assert result.status.value == "failed"
    assert result.resumable is False


def test_legacy_adapters_require_validation_and_basis_manifest() -> None:
    artifacts = InMemoryArtifactRepository()
    with pytest.raises(ValueError, match="risk classification"):
        LegacyRiskClassifierAdapter(cast(Any, artifacts)).assess(object(), None)
    with pytest.raises(ValueError, match="write gate"):
        LegacyWriteGateAdapter(cast(Any, artifacts)).decide(
            object(),
            None,
            object(),  # type: ignore[arg-type]
        )

    basis = SimpleNamespace(root_manifest=None)
    with pytest.raises(ValueError, match="no root manifest"):
        _basis_manifest(basis)  # type: ignore[arg-type]


def _basis(*, include_documents: bool = True) -> CanonicalWriteBasis:
    manifest = _manifest()
    text = TextRootDocument(
        root_hash=manifest.text_root.artifact_id,
        schema_version=manifest.schema_version,
        chapters=(),
    )
    plan = PlanRootDocument(
        root_hash=manifest.plan_root.artifact_id,
        schema_version=manifest.schema_version,
    )
    world = WorldRootDocument(
        root_hash=manifest.world_root.artifact_id,
        schema_version=manifest.schema_version,
        source_commit=BASE,
    )
    return CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=BASE,
        root_manifest=manifest,
        canonical_text=text if include_documents else None,
        canonical_plan=plan if include_documents else None,
        canonical_world=world if include_documents else None,
    )


def _basis_with_world(
    text: TextRootDocument,
    world: WorldRootDocument,
) -> CanonicalWriteBasis:
    manifest = _manifest()
    return CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=BASE,
        root_manifest=manifest,
        canonical_text=text,
        canonical_plan=PlanRootDocument(
            root_hash=manifest.plan_root.artifact_id,
            schema_version=manifest.schema_version,
        ),
        canonical_world=world,
    )


def _curator_receipt() -> AgentExecutionReceipt:
    return agent_receipt().model_copy(
        update={
            "agent_type": AgentType.MEMORY_CURATOR,
            "agent_mode": AgentMode.CURATOR_REPAIR,
            "base_commit": BASE,
        }
    )


def _proposal_model_request(request_id: StableId) -> ModelRequest:
    request = _request()
    return ModelRequest(
        request_id=request_id,
        run_id=request.run_id,
        task_id=request.task_id,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace.teacher-forced.proposal",
        prompt="proposal",
    )


def _proposal_attempt_request(
    artifacts: InMemoryArtifactRepository,
    model_request_id: StableId,
    *,
    feedback: bool = False,
) -> CuratorProposalAttemptRequest:
    request = _request()
    budget = artifacts.put(
        b"budget",
        "application/vnd.novel-agent.proposal-budget-reservation+json",
        _manifest().schema_version,
    )
    feedback_ref = (
        artifacts.put(
            b'{"safe_feedback":["replace duplicate target"]}',
            "application/vnd.novel-agent.curator-proposal-feedback+json",
            _manifest().schema_version,
        )
        if feedback
        else None
    )
    return CuratorProposalAttemptRequest(
        request=request,
        basis=_basis(),
        attempt_id=StableId("proposal-attempt.adapter.1"),
        attempt_no=1,
        model_request_id=model_request_id,
        source_artifacts=(),
        source_visibility_receipts=(),
        budget_reservation_ref=budget,
        feedback_artifact_ref=feedback_ref,
    )


def test_curator_proposal_requires_text_and_world_documents() -> None:
    port = TeacherForcedCuratorPort(
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, InMemoryArtifactRepository()),
        cast(Any, lambda *_: object()),
    )
    request = CuratorProposalRequest(
        request=_request(),
        basis=_basis(include_documents=False),
        source_artifacts=(),
        source_visibility_receipts=(),
    )

    with pytest.raises(ValueError, match="proposal requires"):
        asyncio.run(port.propose(request))


def test_graph_curator_must_share_the_replay_gateway() -> None:
    replay = SimpleNamespace(curator=SimpleNamespace(gateway=ModelGateway(())))
    graph_curator = SimpleNamespace(gateway=ModelGateway(()))

    with pytest.raises(ValueError, match="must share"):
        TeacherForcedCuratorPort(
            cast(Any, replay),
            cast(Any, object()),
            cast(Any, InMemoryArtifactRepository()),
            cast(Any, lambda *_: object()),
            graph_curator=cast(Any, graph_curator),
        )


def test_curator_proposal_runs_without_an_optional_script() -> None:
    artifacts = InMemoryArtifactRepository()
    data = _ready_data(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
    )
    assert data.bundle is not None
    observed_changes = data.bundle.observed_changes
    receipt = agent_receipt().model_copy(
        update={
            "agent_type": AgentType.MEMORY_CURATOR,
            "agent_mode": AgentMode.REPLAY,
            "base_commit": BASE,
        }
    )

    class ReplayAgent:
        async def run(self, **_: object) -> tuple[object, None]:
            return (
                SimpleNamespace(
                    observed_changes=observed_changes,
                    receipt=receipt,
                ),
                None,
            )

    port = TeacherForcedCuratorPort(
        cast(Any, ReplayAgent()),
        cast(Any, object()),
        cast(Any, artifacts),
        cast(Any, lambda *_: object()),
    )
    result = asyncio.run(
        port.propose(
            CuratorProposalRequest(
                request=_request(),
                basis=_basis(),
                source_artifacts=(),
                source_visibility_receipts=(),
            )
        )
    )

    assert result.agent_receipt == receipt
    assert port.proposal_calls == 1


def test_legacy_curator_proposal_invokes_optional_script() -> None:
    artifacts = InMemoryArtifactRepository()
    data = _ready_data(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
    )
    assert data.bundle is not None
    observed_changes = data.bundle.observed_changes
    receipt = agent_receipt().model_copy(
        update={
            "agent_type": AgentType.MEMORY_CURATOR,
            "agent_mode": AgentMode.REPLAY,
            "base_commit": BASE,
        }
    )

    class Replay:
        async def run(self, **_: object) -> tuple[object, None]:
            return (
                SimpleNamespace(
                    observed_changes=observed_changes,
                    receipt=receipt,
                ),
                None,
            )

    scripted: list[ModelRequest] = []
    port = TeacherForcedCuratorPort(
        cast(Any, Replay()),
        cast(Any, object()),
        cast(Any, artifacts),
        cast(Any, lambda *_: _proposal_model_request(StableId("model.legacy.script"))),
        script=lambda request, _: scripted.append(request),
    )
    asyncio.run(
        port.propose(
            CuratorProposalRequest(
                request=_request(),
                basis=_basis(),
                source_artifacts=(),
                source_visibility_receipts=(),
            )
        )
    )
    assert len(scripted) == 1


def test_curator_proposal_attempt_runs_graph_profile_concurrently_and_merges_relation() -> None:
    artifacts = InMemoryArtifactRepository()
    world, text, _, _ = _teacher_world()
    request = _request(profile=MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC)
    basis = CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=BASE,
        root_manifest=_manifest(),
        canonical_text=text,
        canonical_plan=PlanRootDocument(
            root_hash=_manifest().plan_root.artifact_id,
            schema_version=_manifest().schema_version,
        ),
        canonical_world=world,
    )
    ordinary_changes = ObservedChangeSet(
        change_set_id=StableId("changes.concurrent-curator.unit"),
        base_commit=BASE,
        source_artifact=_manifest().text_root,
    )
    receipt = agent_receipt().model_copy(
        update={
            "agent_type": AgentType.MEMORY_CURATOR,
            "agent_mode": AgentMode.REPLAY,
            "base_commit": BASE,
        }
    )
    gateway = ModelGateway(())
    ready = asyncio.Event()
    inflight = 0
    max_inflight = 0

    async def rendezvous() -> None:
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        if inflight == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=1)
        inflight -= 1

    class Replay:
        curator = SimpleNamespace(
            gateway=gateway,
            last_evidence_merge_receipts=(),
            last_operation_filter_receipts=(),
        )

        async def run(self, **_: object) -> tuple[CuratorReplayResult, None]:
            await rendezvous()
            return (
                CuratorReplayResult(
                    observed_changes=ordinary_changes,
                    coverage=1.0,
                    receipt=receipt,
                ),
                None,
            )

    class GraphCurator:
        def __init__(self) -> None:
            self.gateway = gateway
            self.request: ModelRequest | None = None

        async def extract_graph_candidates(
            self,
            text_root: TextRootDocument,
            chapter_index: int,
            base_commit: CommitId,
            _world: WorldRootDocument,
            model_request: ModelRequest,
            repair_feedback: str | None = None,
        ) -> tuple[tuple[WorldGraphCandidateBatch, ...], tuple[()]]:
            self.request = model_request
            await rendezvous()
            return (
                (
                    WorldGraphCandidateBatch(
                        batch_id=StableId("graph-batch.concurrent-curator.unit"),
                        source_text_root=text_root.root_hash,
                        base_commit=base_commit,
                        chapter_index=chapter_index,
                        policy_version="graph-concurrency-unit.v1",
                        model_request_id=model_request.request_id,
                    ),
                ),
                (),
            )

    graph_curator = GraphCurator()
    port = TeacherForcedCuratorPort(
        cast(Any, Replay()),
        cast(Any, object()),
        cast(Any, artifacts),
        cast(Any, lambda *_: _proposal_model_request(StableId("model.concurrent-curator"))),
        graph_curator=cast(Any, graph_curator),
    )
    outcome = asyncio.run(
        port.propose_attempt(
            _proposal_attempt_request(
                artifacts,
                StableId("model.concurrent-curator.attempt"),
            ).model_copy(
                update={
                    "request": request,
                    "basis": basis,
                }
            )
        )
    )

    assert isinstance(outcome, CuratorProposalAccepted)
    assert max_inflight == 2
    assert world.relations == ()
    assert len(outcome.observed_changes.operations) == 1
    operation = outcome.observed_changes.operations[0]
    assert isinstance(operation.payload, dict)
    assert operation.payload["record_type"] == "relation"
    assert graph_curator.request is not None
    assert graph_curator.request.scheduling_stage == "curator_graph_extraction"
    agent_ref = outcome.attempt_receipt.agent_execution_receipt_ref
    assert agent_ref is not None
    persisted_receipt = AgentExecutionReceipt.model_validate_json(
        artifacts.read_verified(agent_ref),
        strict=True,
    )
    assert {item.media_type for item in persisted_receipt.output_artifacts} >= {
        "application/vnd.novel-agent.world-graph-candidate-batch+json",
        "application/vnd.novel-agent.world-graph-extraction-receipt+json",
    }

    legacy = asyncio.run(
        port.propose(
            CuratorProposalRequest(
                request=request,
                basis=basis,
                source_artifacts=(),
                source_visibility_receipts=(),
            )
        )
    )
    assert legacy.agent_receipt is not None
    assert {item.media_type for item in legacy.agent_receipt.output_artifacts} >= {
        "application/vnd.novel-agent.world-graph-candidate-batch+json",
        "application/vnd.novel-agent.world-graph-extraction-receipt+json",
    }


def test_curator_proposal_cancels_graph_profile_when_replay_fails() -> None:
    world, text, _, _ = _teacher_world()
    started = asyncio.Event()
    graph_cancelled = False
    gateway = ModelGateway(())

    class Replay:
        curator = SimpleNamespace(gateway=gateway)

        async def run(self, **_: object) -> None:
            await started.wait()
            raise ModelCurationContractError("ordinary Curator rejected")

    class GraphCurator:
        def __init__(self) -> None:
            self.gateway = gateway

        async def extract_graph_candidates(
            self, *_: object, repair_feedback: str | None = None
        ) -> None:
            nonlocal graph_cancelled
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                graph_cancelled = True

    port = TeacherForcedCuratorPort(
        cast(Any, Replay()),
        cast(Any, object()),
        cast(Any, InMemoryArtifactRepository()),
        cast(Any, lambda *_: _proposal_model_request(StableId("model.cancel-graph"))),
        graph_curator=cast(Any, GraphCurator()),
    )

    with pytest.raises(ModelCurationContractError, match="ordinary Curator rejected"):
        asyncio.run(
            port.propose(
                CuratorProposalRequest(
                    request=_request(profile=MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC),
                    basis=_basis_with_world(text, world),
                    source_artifacts=(),
                    source_visibility_receipts=(),
                )
            )
        )
    assert graph_cancelled is True


def test_curator_proposal_maps_graph_basis_mismatch_to_contract_failure() -> None:
    world, text, _, _ = _teacher_world()
    gateway = ModelGateway(())
    ordinary_changes = ObservedChangeSet(
        change_set_id=StableId("changes.graph-basis-mismatch.unit"),
        base_commit=BASE,
        source_artifact=_manifest().text_root,
    )
    receipt = agent_receipt().model_copy(
        update={
            "agent_type": AgentType.MEMORY_CURATOR,
            "agent_mode": AgentMode.REPLAY,
            "base_commit": BASE,
        }
    )

    class Replay:
        curator = SimpleNamespace(gateway=gateway)

        async def run(self, **_: object) -> tuple[CuratorReplayResult, None]:
            return CuratorReplayResult(
                observed_changes=ordinary_changes,
                coverage=1.0,
                receipt=receipt,
            ), None

    class GraphCurator:
        def __init__(self) -> None:
            self.gateway = gateway

        async def extract_graph_candidates(
            self,
            text_root: TextRootDocument,
            chapter_index: int,
            _base_commit: CommitId,
            _world: WorldRootDocument,
            model_request: ModelRequest,
            repair_feedback: str | None = None,
        ) -> tuple[tuple[WorldGraphCandidateBatch, ...], tuple[()]]:
            return (
                (
                    WorldGraphCandidateBatch(
                        batch_id=StableId("graph-batch.basis-mismatch.unit"),
                        source_text_root=text_root.root_hash,
                        base_commit=CommitId("sha256:" + "9" * 64),
                        chapter_index=chapter_index,
                        policy_version="graph-basis-mismatch-unit.v1",
                        model_request_id=model_request.request_id,
                    ),
                ),
                (),
            )

    port = TeacherForcedCuratorPort(
        cast(Any, Replay()),
        cast(Any, object()),
        cast(Any, InMemoryArtifactRepository()),
        cast(Any, lambda *_: _proposal_model_request(StableId("model.graph-basis-mismatch"))),
        graph_curator=cast(Any, GraphCurator()),
    )

    with pytest.raises(ModelCurationContractError, match="graph candidate admission failed"):
        asyncio.run(
            port.propose(
                CuratorProposalRequest(
                    request=_request(profile=MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC),
                    basis=_basis_with_world(text, world),
                    source_artifacts=(),
                    source_visibility_receipts=(),
                )
            )
        )


def test_typed_proposal_requires_documents_and_preserves_typed_validation_failure() -> None:
    artifacts = InMemoryArtifactRepository()
    model_request = _proposal_model_request(StableId("model.proposal.validation"))
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="proposal-test",
                model_name="fake",
                adapter=FakeModelEndpoint("invalid"),
            ),
        )
    )

    class Replay:
        curator = SimpleNamespace(gateway=gateway)

        async def run(self, **kwargs: object) -> None:
            await gateway.generate_text(cast(ModelRequest, kwargs["request"]))
            raise ModelCurationContractError("scope changed")

    port = TeacherForcedCuratorPort(
        cast(Any, Replay()),
        cast(Any, object()),
        cast(Any, artifacts),
        cast(Any, lambda *_: model_request),
    )
    missing = _proposal_attempt_request(artifacts, model_request.request_id).model_copy(
        update={"basis": _basis(include_documents=False)}
    )
    with pytest.raises(ValueError, match="proposal requires"):
        asyncio.run(port.propose_attempt(missing))

    rejected = asyncio.run(
        port.propose_attempt(_proposal_attempt_request(artifacts, model_request.request_id))
    )
    assert isinstance(rejected, CuratorProposalRejected)
    assert rejected.rejection.kind is ProposalRejectionKind.SCOPE_VIOLATION


def test_typed_proposal_receipt_counts_semantic_verifier_child_call() -> None:
    artifacts = InMemoryArtifactRepository()
    model_request = _proposal_model_request(StableId("model.proposal.semantic"))
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="proposal-test",
                model_name="fake",
                adapter=FakeModelEndpoint("valid"),
            ),
        )
    )

    class Replay:
        curator = SimpleNamespace(
            gateway=gateway,
            last_prompt_fingerprint=ArtifactId("sha256:" + "8" * 64),
        )

        async def run(self, **kwargs: object) -> None:
            parent = cast(ModelRequest, kwargs["request"])
            verifier = parent.model_copy(
                update={"request_id": StableId(f"{parent.request_id.root}.semantic-verifier")}
            )
            await gateway.generate_text(parent)
            await gateway.generate_text(verifier)
            raise CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_EVIDENCE_UNSUPPORTED",
                (),
                safe_feedback=("semantic verifier rejected evidence",),
            )

    port = TeacherForcedCuratorPort(
        cast(Any, Replay()),
        cast(Any, object()),
        cast(Any, artifacts),
        cast(Any, lambda *_: model_request),
    )
    outcome = asyncio.run(
        port.propose_attempt(_proposal_attempt_request(artifacts, model_request.request_id))
    )

    assert isinstance(outcome, CuratorProposalRejected)
    assert outcome.attempt_receipt.provider_call_count == 2
    assert len(outcome.attempt_receipt.model_call_receipt_refs) == 2
    assert outcome.attempt_receipt.model_request_ids == (
        model_request.request_id,
        StableId(f"{model_request.request_id.root}.semantic-verifier"),
    )
    assert outcome.attempt_receipt.prompt_fingerprint == ArtifactId("sha256:" + "8" * 64)


def test_typed_proposal_rejection_draft_ref_uses_primary_curator_response() -> None:
    artifacts = InMemoryArtifactRepository()
    model_request = _proposal_model_request(StableId("model.proposal.primary-draft"))
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="proposal-test",
                model_name="fake",
                adapter=ScriptedModelEndpoint(
                    lambda request: verifier_raw
                    if request.request_id.root.endswith(".semantic-verifier")
                    else primary_raw
                ),
            ),
        )
    )
    primary_raw = '{"operations":[{"operation":"create"}]}'
    verifier_raw = (
        '{"decisions":[{"operation_index":1,"disposition":"supports",'
        '"reason_code":"direct_support"}]}'
    )

    class Replay:
        curator = SimpleNamespace(gateway=gateway)

        async def run(self, **kwargs: object) -> None:
            parent = cast(ModelRequest, kwargs["request"])
            await gateway.generate_text(parent)
            verifier = parent.model_copy(
                update={"request_id": StableId(f"{parent.request_id.root}.semantic-verifier")}
            )
            await gateway.generate_text(verifier)
            raise CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_EVIDENCE_UNSUPPORTED",
                (),
                safe_feedback=("semantic verifier rejected evidence",),
            )

    port = TeacherForcedCuratorPort(
        cast(Any, Replay()),
        cast(Any, object()),
        cast(Any, artifacts),
        cast(Any, lambda *_: model_request),
    )
    outcome = asyncio.run(
        port.propose_attempt(_proposal_attempt_request(artifacts, model_request.request_id))
    )

    assert isinstance(outcome, CuratorProposalRejected)
    assert outcome.rejection.raw_draft_ref is not None
    assert artifacts.read_verified(outcome.rejection.raw_draft_ref).decode("utf-8") == primary_raw
    assert outcome.rejection.output_hash == sha256_id(primary_raw.encode("utf-8"))
    assert len(outcome.attempt_receipt.raw_response_refs) == 2
    assert (
        artifacts.read_verified(outcome.attempt_receipt.raw_response_refs[-1]).decode("utf-8")
        == verifier_raw
    )


def test_typed_proposal_receipt_preserves_failed_verifier_ledger_entry() -> None:
    artifacts = InMemoryArtifactRepository()
    model_request = _proposal_model_request(StableId("model.proposal.failed-verifier"))
    endpoint = FakeModelEndpoint("valid")
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="proposal-test",
                model_name="fake",
                adapter=endpoint,
            ),
        )
    )
    asyncio.run(gateway.generate_text(model_request))
    endpoint.error = RuntimeError("verifier transport failed")
    verifier_request = model_request.model_copy(
        update={"request_id": StableId(f"{model_request.request_id.root}.semantic-verifier")}
    )
    with pytest.raises(RuntimeError, match="verifier transport failed"):
        asyncio.run(gateway.generate_text(verifier_request))
    replay = SimpleNamespace(curator=SimpleNamespace(gateway=gateway))
    port = TeacherForcedCuratorPort(
        cast(Any, replay),
        cast(Any, object()),
        cast(Any, artifacts),
        cast(Any, lambda *_: model_request),
    )

    outcome = port._proposal_rejected(
        _proposal_attempt_request(artifacts, model_request.request_id),
        model_request,
        CuratorProposalSemanticRejected(
            "CURATOR_PROPOSAL_EVIDENCE_UNRESOLVED",
            (),
            safe_feedback=("semantic verifier unavailable",),
        ),
        datetime.now(UTC),
        _manifest().schema_version,
    )

    assert outcome.attempt_receipt.model_request_ids == (
        model_request.request_id,
        verifier_request.request_id,
    )
    assert len(outcome.attempt_receipt.model_call_receipt_refs) == 2
    assert outcome.attempt_receipt.model_call_receipt_refs[0].media_type == (
        "application/vnd.novel-agent.model-call-record+json"
    )
    assert outcome.attempt_receipt.model_call_receipt_refs[1].media_type == (
        "application/vnd.novel-agent.model-call-ledger-entry+json"
    )
    assert outcome.attempt_receipt.provider_call_count == 2


def test_typed_proposal_reraises_non_model_failure_without_ledger_evidence() -> None:
    artifacts = InMemoryArtifactRepository()
    model_request = _proposal_model_request(StableId("model.proposal.no-ledger"))
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="proposal-test",
                model_name="fake",
                adapter=FakeModelEndpoint("unused"),
            ),
        )
    )

    class Replay:
        curator = SimpleNamespace(gateway=gateway)

        async def run(self, **_: object) -> None:
            raise RuntimeError("failed before provider reservation")

    port = TeacherForcedCuratorPort(
        cast(Any, Replay()),
        cast(Any, object()),
        cast(Any, artifacts),
        cast(Any, lambda *_: model_request),
    )
    with pytest.raises(RuntimeError, match="before provider reservation"):
        asyncio.run(
            port.propose_attempt(_proposal_attempt_request(artifacts, model_request.request_id))
        )


def test_typed_proposal_rejection_maps_schema_semantic_and_boundary_errors() -> None:
    invalid_schema_payload = {
        "chapter_index": 1,
        "operations": [
            {
                "operation": "create",
                "record_kind": "entity",
                "target_id": "entity.invalid",
                "record": {
                    "subject_id": "entity.invalid",
                    "predicate": "has_state",
                    "value": "invalid",
                    "valid_time": {"worldline": "current", "start_ordinal": 1},
                    "truth_class": "assertion",
                },
                "evidence_refs": [{"block_id": "block.1", "start": 0, "end": 1}],
            },
        ],
    }
    with pytest.raises(ValidationError) as invalid_schema:
        ChapterChangeDraft.model_validate_json(canonical_json_bytes(invalid_schema_payload))
    schema_error = invalid_schema.value

    conflict = ProposalConflict(
        record_kind=WorldRecordKind.ENTITY,
        target_id=StableId("entity.canonical"),
        operation_indexes=(0, 1),
        semantic_hashes=(ArtifactId("sha256:" + "1" * 64),),
    )
    cases = (
        (
            schema_error,
            ProposalRejectionStage.STRUCTURED_SCHEMA,
            ProposalRejectionKind.SCHEMA_REJECTED,
            True,
        ),
        (
            CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_INFORMATION_BOUNDARY",
                (),
                information_boundary=True,
            ),
            ProposalRejectionStage.INFORMATION_BOUNDARY,
            ProposalRejectionKind.INVALID_EVIDENCE,
            False,
        ),
        (
            CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_INVALID_EVIDENCE",
                (),
                safe_feedback=(
                    "block.1: require 0 <= start < end <= 10; received start=20, end=30",
                ),
            ),
            ProposalRejectionStage.SEMANTIC_CONTRACT,
            ProposalRejectionKind.INVALID_EVIDENCE,
            True,
        ),
        (
            CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_EVIDENCE_UNRESOLVED",
                (),
                safe_feedback=("candidate evidence requires semantic verification",),
            ),
            ProposalRejectionStage.SEMANTIC_CONTRACT,
            ProposalRejectionKind.INVALID_EVIDENCE,
            True,
        ),
        (
            CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_EMPTY_DELTA_UNVERIFIED",
                (),
                safe_feedback=("trusted no-op verifier is unavailable",),
                json_pointers=(
                    "/operations",
                    "/no_durable_delta_reason",
                    "/no_op_evidence_candidate_ids",
                ),
                violation_rule="empty_delta_requires_trusted_verification",
            ),
            ProposalRejectionStage.SEMANTIC_CONTRACT,
            ProposalRejectionKind.INCOMPLETE_DELTA,
            True,
        ),
        (
            CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_DANGLING_ENTITY_REFERENCE",
                (),
                safe_feedback=(
                    "/operations/0/record/subject_id: unknown entity_id entity.missing",
                ),
                operation_indexes=(0,),
                json_pointers=("/operations/0/record/subject_id",),
                violation_rule=("referenced_entity_must_exist_or_be_created_in_same_proposal"),
            ),
            ProposalRejectionStage.SEMANTIC_CONTRACT,
            ProposalRejectionKind.DANGLING_ENTITY_REFERENCE,
            True,
        ),
        (
            CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_NORMALIZED_TARGET_COLLISION",
                (conflict,),
            ),
            ProposalRejectionStage.TRUSTED_NORMALIZATION,
            ProposalRejectionKind.NORMALIZED_TARGET_COLLISION,
            True,
        ),
        (
            ModelCurationContractError("draft chapter differs"),
            ProposalRejectionStage.SEMANTIC_CONTRACT,
            ProposalRejectionKind.CHAPTER_MISMATCH,
            True,
        ),
        (
            ModelCurationContractError("scope changed"),
            ProposalRejectionStage.SEMANTIC_CONTRACT,
            ProposalRejectionKind.SCOPE_VIOLATION,
            True,
        ),
    )
    for index, (error, stage, kind, retryable) in enumerate(cases):
        artifacts = InMemoryArtifactRepository()
        model_request = _proposal_model_request(StableId(f"model.proposal.rejection.{index}"))
        gateway = ModelGateway(
            (
                RegisteredModelEndpoint(
                    role=ModelRole.BATCH_TEST,
                    endpoint_name="proposal-test",
                    model_name="fake",
                    adapter=FakeModelEndpoint("raw rejected response"),
                ),
            )
        )
        asyncio.run(gateway.generate_text(model_request))
        replay = SimpleNamespace(curator=SimpleNamespace(gateway=gateway))
        port = TeacherForcedCuratorPort(
            cast(Any, replay),
            cast(Any, object()),
            cast(Any, artifacts),
            cast(Any, lambda *_, request=model_request: request),
        )
        outcome = port._proposal_rejected(
            _proposal_attempt_request(artifacts, model_request.request_id),
            model_request,
            cast(ValidationError | ModelCurationContractError, error),
            datetime.now(UTC),
            _manifest().schema_version,
        )
        assert outcome.rejection.stage is stage
        assert outcome.rejection.kind is kind
        assert outcome.rejection.retryable is retryable
        assert len(outcome.attempt_receipt.raw_response_refs) == 1
        assert len(outcome.attempt_receipt.model_call_receipt_refs) == 1
        if outcome.rejection.reason_code == "CURATOR_PROPOSAL_INVALID_EVIDENCE":
            assert "require 0 <= start < end" in outcome.rejection.safe_feedback[0]


def test_schema_rejection_feedback_names_semantic_rule_messages() -> None:
    """2026-08-14 corridor repair: semantic model-validator failures must name the rule.

    ch10 receipts showed the graph page semantic rule ("entity candidate must be a
    relation endpoint in the same page") was reported only as generic "failed the
    structured domain contract", so the model repeated the defect into a poison
    loop.  The rejection feedback must surface the actual rule message.
    """
    payload = {
        "status": "complete",
        "candidates": [
            {
                "kind": "entity",
                "surface": "陈长生",
                "entity_type": "person",
                "evidence_quotes": ["quote"],
            }
        ],
        "no_graph_candidate_reason": None,
    }
    with pytest.raises(ValidationError) as raised:
        GraphCandidatePageDraft.model_validate_json(canonical_json_bytes(payload))
    semantic_error = raised.value
    assert any(
        item["type"] == "value_error"
        for item in semantic_error.errors(include_url=False, include_input=False)
    )

    artifacts = InMemoryArtifactRepository()
    model_request = _proposal_model_request(StableId("model.proposal.rule-message"))
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="proposal-test",
                model_name="fake",
                adapter=FakeModelEndpoint("raw rejected response"),
            ),
        )
    )
    asyncio.run(gateway.generate_text(model_request))
    replay = SimpleNamespace(curator=SimpleNamespace(gateway=gateway))
    port = TeacherForcedCuratorPort(
        cast(Any, replay),
        cast(Any, object()),
        cast(Any, artifacts),
        cast(Any, lambda *_, request=model_request: request),
    )
    outcome = port._proposal_rejected(
        _proposal_attempt_request(artifacts, model_request.request_id),
        model_request,
        semantic_error,
        datetime.now(UTC),
        _manifest().schema_version,
    )
    assert outcome.rejection.stage is ProposalRejectionStage.STRUCTURED_SCHEMA
    assert outcome.rejection.kind is ProposalRejectionKind.SCHEMA_REJECTED
    assert any(
        "relation endpoint in the same page" in line for line in outcome.rejection.safe_feedback
    )


def test_typed_proposal_attempt_wraps_transport_and_preserves_safe_feedback() -> None:
    artifacts = InMemoryArtifactRepository()
    model_request = _proposal_model_request(StableId("model.proposal.transport"))
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="proposal-test",
                model_name="fake",
                adapter=FakeModelEndpoint("provider response"),
            ),
        )
    )

    class Replay:
        curator = SimpleNamespace(gateway=gateway)
        feedback: str | None = None

        async def run(self, **kwargs: object) -> None:
            self.feedback = cast(str | None, kwargs["proposal_feedback"])
            await gateway.generate_text(cast(ModelRequest, kwargs["request"]))
            raise RuntimeError("transport disconnected")

    scripts: list[ModelRequest] = []
    replay = Replay()
    port = TeacherForcedCuratorPort(
        cast(Any, replay),
        cast(Any, object()),
        cast(Any, artifacts),
        cast(Any, lambda *_: model_request),
        script=lambda request, _: scripts.append(request),
    )
    attempt = _proposal_attempt_request(
        artifacts,
        model_request.request_id,
        feedback=True,
    )

    with pytest.raises(CuratorProposalTransportError) as raised:
        asyncio.run(port.propose_attempt(attempt))

    assert raised.value.model_request_ids == (model_request.request_id,)
    assert "replace duplicate target" in cast(str, replay.feedback)
    assert "PROPOSAL_REPAIR_FEEDBACK" not in scripts[0].prompt
    assert port.proposal_calls == 1


@pytest.mark.parametrize("with_receipt", (False, True))
@pytest.mark.parametrize("with_script", (False, True))
def test_curator_repair_maps_agent_result_and_persists_receipts(
    with_receipt: bool,
    with_script: bool,
) -> None:
    artifacts = InMemoryArtifactRepository()
    data = _ready_data(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
    )
    assert data.candidate is not None
    assert data.bundle is not None
    payload = MemoryWriteCandidatePayload(
        observed_changes=data.bundle.observed_changes,
        root_update_intents=(),
        commit_profile=_request().commit_profile,
    )
    candidate_ref = artifacts.put(
        canonical_json_bytes(payload.model_dump(mode="json")),
        "application/vnd.novel-agent.memory-write-candidate+json",
        _manifest().schema_version,
    )
    candidate = data.candidate.model_copy(update={"candidate_artifact": candidate_ref})
    directive = RepairDirective(
        directive_id=StableId("directive.teacher-forced.unit"),
        action=RepairAction.CURATOR_REPAIR,
    )
    receipt = _curator_receipt() if with_receipt else None
    observed_changes = data.bundle.observed_changes

    class RepairAgent:
        async def run(self, **_: object) -> CuratorRepairResult:
            return CuratorRepairResult(
                observed_changes=observed_changes,
                agent_receipt=receipt,
            )

    scripts: list[AgentMode] = []
    port = TeacherForcedCuratorPort(
        cast(Any, object()),
        cast(Any, RepairAgent()),
        cast(Any, artifacts),
        cast(Any, lambda *_: object()),
        repair_script=(lambda _, mode: scripts.append(mode)) if with_script else None,
    )
    request = CuratorRepairRequest(
        request=_request(),
        basis=_basis(),
        parent_candidate=candidate,
        validation=data.validation,
        directive=directive,
        source_artifacts=(),
        source_visibility_receipts=(),
    )

    result = asyncio.run(port.repair(request))

    assert port.repair_calls == 1
    assert scripts == ([AgentMode.CURATOR_REPAIR] if with_script else [])
    if with_receipt:
        assert result.producer_receipt is not None
        assert result.candidate_artifact is not None
    else:
        assert result.producer_receipt is None


def test_curator_repair_requires_text_and_world_documents() -> None:
    port = TeacherForcedCuratorPort(
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, InMemoryArtifactRepository()),
        cast(Any, lambda *_: object()),
    )
    data = _ready_data(
        artifacts=InMemoryArtifactRepository(),
        lineage=InMemoryCandidateLineageRepository(),
    )
    assert data.candidate is not None
    request = CuratorRepairRequest(
        request=_request(),
        basis=_basis(include_documents=False),
        parent_candidate=data.candidate,
        directive=RepairDirective(
            directive_id=StableId("directive.teacher-forced.missing-docs"),
            action=RepairAction.CURATOR_REPAIR,
        ),
        source_artifacts=(),
        source_visibility_receipts=(),
    )

    with pytest.raises(ValueError, match="repair requires"):
        asyncio.run(port.repair(request))


@pytest.mark.parametrize("use_evidence_callback", (False, True))
@pytest.mark.parametrize("with_script", (False, True))
def test_guardian_adapter_maps_decision_and_evidence_root(
    use_evidence_callback: bool,
    with_script: bool,
) -> None:
    artifacts = InMemoryArtifactRepository()
    data = _ready_data(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
    )
    assert data.candidate is not None
    assert data.bundle is not None
    assert data.validation is not None
    assert data.risk is not None
    payload = MemoryWriteCandidatePayload(
        observed_changes=data.bundle.observed_changes,
        root_update_intents=(),
        commit_profile=_request().commit_profile,
    )
    candidate_ref = artifacts.put(
        canonical_json_bytes(payload.model_dump(mode="json")),
        "application/vnd.novel-agent.memory-write-candidate+json",
        _manifest().schema_version,
    )
    candidate = data.candidate.model_copy(update={"candidate_artifact": candidate_ref})
    guardian_receipt = agent_receipt().model_copy(
        update={
            "agent_type": AgentType.MEMORY_GUARDIAN,
            "agent_mode": AgentMode.RISK_REVIEW,
            "base_commit": BASE,
        }
    )
    decision = GuardianDecision(
        decision_id=StableId("guardian.teacher-forced.unit"),
        proposal_id=candidate.candidate_id,
        base_commit=BASE,
        outcome=GuardianOutcome.APPROVE,
        risk_codes=(),
        reasons=("safe",),
        receipt=guardian_receipt,
    )
    evidence: list[object] = []

    class GuardianAgent:
        async def review(self, **kwargs: object) -> tuple[GuardianDecision, None]:
            evidence.append(kwargs["evidence_root"])
            return decision, None

    scripts: list[AgentMode] = []
    basis = _basis()
    adapter = LegacyGuardianPortAdapter(
        cast(Any, GuardianAgent()),
        cast(Any, artifacts),
        cast(Any, lambda *_: object()),
        script=(lambda _, mode: scripts.append(mode)) if with_script else None,
        evidence_root=(lambda: basis.canonical_text) if use_evidence_callback else None,
    )
    result = asyncio.run(
        adapter.review(
            GuardianReviewRequest(
                request=_request(),
                basis=basis,
                candidate=candidate,
                validation=data.validation,
                risk=data.risk,
            )
        )
    )

    assert result.decision == decision
    assert result.receipt is not None
    assert evidence == [basis.canonical_text]
    assert scripts == ([AgentMode.RISK_REVIEW] if with_script else [])
