"""Edge contracts for Stage 2W local ports and workflow conversion helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from novel_agent.domain.changes import ObservedChangeSet
from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.memory_write import (
    BoundaryPropagationReceipt,
    CandidateProducerKind,
    MemoryWriteCandidatePayload,
    MemoryWriteState,
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
    NarrativePosition,
    NormalizationResult,
    NormalizationStatus,
    ProjectionReadinessStatus,
    TrustedWorldCandidateInput,
)
from novel_agent.domain.runtime import ResumabilityStatus, RunEvent, RunEventType
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentMode,
    AgentType,
    GuardianDecision,
    GuardianOutcome,
)
from novel_agent.ports.memory_write import (
    CuratorProposalResult,
    CuratorRepairResult,
    MemoryWriteCommitStatus,
    RootMaterializationResult,
)
from novel_agent.services.information_boundary import InformationBoundaryViolation
from novel_agent.services.memory_write_workflow import (
    ImmediateProjectionReadinessPort,
    InMemoryArtifactRepository,
    InMemoryCandidateLineageRepository,
    InMemoryCheckpointRepository,
    InMemoryCommitPort,
    InMemoryRunEventSink,
    LocalMemoryWriteWorkflow,
    MemoryWriteIdentityCollision,
    MemoryWriteWorkflowError,
    StaticCanonicalReadPort,
    _as_commit_result,
    _as_guardian_result,
    _as_materialization_result,
    _as_normalization_result,
    _as_proposal_result,
    _as_repair_result,
    _as_repair_result_or_none,
    _candidate_id,
    _current_commit,
    _earliest_position,
    _maybe_await,
    _model_ref,
    _narrowest_scope,
    _NoopBoundary,
    _phase_for_data,
    _put_model,
    _read_model,
    _receipt_ref,
    _remaining,
    _require,
    _roots_changed,
    _source_artifact,
    _WorkflowData,
    awaitable_result,
)
from tests.contract.test_memory_write_workflow_contract import (
    BASE,
    PROJECT,
    VERSION,
    _artifact,
    _candidate,
    _manifest,
    _request,
)
from tests.contract.test_stage2_contract import agent_receipt
from tests.unit.test_memory_write_resume import _ready_data
from tests.unit.test_memory_write_workflow import _workflow_and_data
from tests.unit.test_teacher_forced_memory_write_adapters import _commit_request


def test_artifact_repository_detects_missing_metadata_corruption_and_collision() -> None:
    artifacts = InMemoryArtifactRepository()
    ref = artifacts.put(b"data", "application/json", VERSION)
    assert artifacts.read_verified(ref) == b"data"

    with pytest.raises(KeyError, match="not found"):
        artifacts.read_verified(_artifact("9"))
    with pytest.raises(MemoryWriteWorkflowError, match="integrity"):
        artifacts.read_verified(ref.model_copy(update={"byte_length": 999}))

    artifacts._objects[ref.artifact_id] = (b"other", "application/json", VERSION)
    with pytest.raises(MemoryWriteWorkflowError, match="collision"):
        artifacts.put(b"data", "application/json", VERSION)


def test_candidate_lineage_enforces_identity_parent_sequence_and_base() -> None:
    lineage = InMemoryCandidateLineageRepository()
    parent = _candidate(_artifact("e"))
    assert lineage.persist(parent) == parent
    assert lineage.persist(parent) == parent
    assert lineage.get(parent.candidate_id) == parent
    assert lineage.list_for_request(_request().request_id) == (parent,)

    with pytest.raises(MemoryWriteWorkflowError, match="identity collision"):
        lineage.persist(
            parent.model_copy(update={"content_hash": ArtifactId("sha256:" + "9" * 64)})
        )
    with pytest.raises(MemoryWriteWorkflowError, match="revision 1"):
        lineage.persist(
            parent.model_copy(
                update={
                    "candidate_id": StableId("candidate.orphan.first"),
                    "revision_no": 2,
                }
            )
        )
    with pytest.raises(MemoryWriteWorkflowError, match="parent or sequence"):
        lineage.persist(
            parent.model_copy(
                update={
                    "candidate_id": StableId("candidate.orphan.child"),
                    "parent_candidate_id": StableId("candidate.missing"),
                    "revision_no": 2,
                }
            )
        )
    child = parent.model_copy(
        update={
            "candidate_id": StableId("candidate.child"),
            "parent_candidate_id": parent.candidate_id,
            "revision_no": 2,
            "base_commit": CommitId("sha256:" + "9" * 64),
        }
    )
    with pytest.raises(MemoryWriteWorkflowError, match="changed base"):
        lineage.persist(child)


def test_checkpoint_reload_event_sequence_and_static_basis_errors() -> None:
    artifacts = InMemoryArtifactRepository()
    request = _request()
    data = _WorkflowData(request=request, artifacts=artifacts)
    checkpoint_repo = InMemoryCheckpointRepository(artifacts)
    from novel_agent.domain.memory_write import MemoryWriteCheckpoint

    request_ref = artifacts.put(
        request.model_dump_json().encode(),
        "application/json",
        VERSION,
    )
    checkpoint = MemoryWriteCheckpoint(
        checkpoint_id=StableId("checkpoint.edges"),
        request_identity_hash=ArtifactId("sha256:" + "a" * 64),
        request_artifact_ref=request_ref,
        run_id=request.run_id,
        task_id=request.task_id,
        project_id=request.project_id,
        base_commit=request.base_commit,
        source_artifacts=request.source_artifacts,
        root_update_intents=request.root_update_intents,
        world_mutation=request.world_mutation,
        information_boundary=request.information_boundary,
        configuration_fingerprint=request.configuration_fingerprint,
        workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        state=MemoryWriteState.LOAD_BASIS,
        resume_state=MemoryWriteState.LOAD_BASIS,
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    ref = checkpoint_repo.save(checkpoint)
    assert InMemoryCheckpointRepository(artifacts).load(ref) == checkpoint
    assert data.request == request

    sink = InMemoryRunEventSink()
    bad_event = RunEvent(
        event_id=StableId("event.bad"),
        run_id=request.run_id,
        task_id=request.task_id,
        sequence_no=2,
        event_type=RunEventType.TASK_STARTED,
        occurred_at=parent_time(),
        idempotency_identity=StableId("event-effect.bad"),
        payload_schema_version=VERSION,
        trace_id="trace.bad",
        payload={},
    )
    with pytest.raises(MemoryWriteWorkflowError, match="contiguous"):
        sink.append(bad_event)

    basis = _ready_data(
        artifacts=artifacts,
        lineage=InMemoryCandidateLineageRepository(),
    ).request
    from novel_agent.domain.memory_write import CanonicalWriteBasis

    canonical = StaticCanonicalReadPort(
        CanonicalWriteBasis(
            project_id=PROJECT,
            commit_id=BASE,
            root_manifest=_manifest(),
        )
    )
    with pytest.raises(MemoryWriteWorkflowError, match="not available"):
        canonical.load_verified(PROJECT, CommitId("sha256:" + "9" * 64))
    with pytest.raises(MemoryWriteWorkflowError, match="unknown project"):
        canonical.current_commit(type(PROJECT)("project.other"))
    assert canonical.load_verified(PROJECT, BASE).commit_id == BASE
    assert canonical.current_commit(PROJECT) == BASE
    assert basis.project_id == PROJECT


def test_workflow_event_identities_remain_bounded_after_shared_sequence_grows() -> None:
    workflow, _ = _workflow_and_data()
    sink = InMemoryRunEventSink()
    workflow._events = sink
    request = _request().model_copy(
        update={"request_id": StableId("chapter-settlement.acceptance." + "a" * 64)}
    )
    data = _WorkflowData(request=request, artifacts=workflow._artifacts)

    for sequence_no in range(1, 10):
        sink.append(
            RunEvent(
                event_id=StableId(f"seed.event.{sequence_no}"),
                run_id=request.run_id,
                task_id=request.task_id,
                sequence_no=sequence_no,
                event_type=RunEventType.TASK_STARTED,
                occurred_at=parent_time(),
                idempotency_identity=StableId(f"seed.effect.{sequence_no}"),
                payload_schema_version=VERSION,
                trace_id="trace.seed",
                payload={},
            )
        )

    workflow._event(data, RunEventType.CURATOR_PROPOSAL_ATTEMPT_REQUESTED)

    event = sink.events[-1]
    assert event.sequence_no == 10
    assert len(event.event_id.root) <= 128
    assert len(event.idempotency_identity.root) <= 128
    assert event.event_id.root.startswith("event.")
    assert event.idempotency_identity.root.startswith("event-effect.")


def parent_time() -> Any:
    from datetime import UTC, datetime

    return datetime(2026, 7, 23, tzinfo=UTC)


def test_commit_port_conflict_exact_replay_and_identity_collision() -> None:
    request = _commit_request()
    conflicted = InMemoryCommitPort(current_commit=CommitId("sha256:" + "9" * 64))
    result = conflicted.resolve_or_replay_exact(request)
    assert result.status == MemoryWriteCommitStatus.CONFLICTED
    assert conflicted.resolve_or_replay_exact(request) == result
    with pytest.raises(MemoryWriteIdentityCollision):
        conflicted.resolve_or_replay_exact(
            request.model_copy(update={"request_hash": ArtifactId("sha256:" + "8" * 64)})
        )

    accepted = InMemoryCommitPort(current_commit=BASE)
    result = accepted.resolve_or_replay_exact(request)
    assert result.status == MemoryWriteCommitStatus.ACCEPTED
    assert result.commit_receipt_ref is not None


def test_projection_port_pending_and_ready_modes() -> None:
    pending = ImmediateProjectionReadinessPort(pending=True)
    effect = StableId("effect.edges")
    assert (
        pending.request_or_read_by_effect_id(PROJECT, BASE, effect).status
        is ProjectionReadinessStatus.PENDING
    )
    assert pending.await_or_check(PROJECT, BASE, effect).status is ProjectionReadinessStatus.PENDING

    ready = ImmediateProjectionReadinessPort()
    assert (
        ready.request_or_read_by_effect_id(PROJECT, BASE, effect).status
        is ProjectionReadinessStatus.READY
    )
    assert ready.await_or_check(PROJECT, BASE, effect).status is ProjectionReadinessStatus.READY


def test_noop_boundary_fails_closed_for_mismatched_basis_and_derivation() -> None:
    boundary = _NoopBoundary()
    request = _request()
    from novel_agent.domain.memory_write import CanonicalWriteBasis

    basis = CanonicalWriteBasis(
        project_id=type(PROJECT)("project.other"),
        commit_id=BASE,
        root_manifest=_manifest().model_copy(update={"project_id": type(PROJECT)("project.other")}),
    )
    with pytest.raises(InformationBoundaryViolation, match="basis mismatch"):
        boundary.verify_request_and_derivation_graph(request, basis)
    with pytest.raises(InformationBoundaryViolation, match="no derivation"):
        boundary.verify_derivation_chain()
    matching = _ready_data(
        artifacts=InMemoryArtifactRepository(),
        lineage=InMemoryCandidateLineageRepository(),
    )
    matching.basis = StaticCanonicalReadPort(
        CanonicalWriteBasis(
            project_id=PROJECT,
            commit_id=BASE,
            root_manifest=_manifest(),
        )
    ).load_verified(PROJECT, BASE)
    boundary.verify_request_and_derivation_graph(matching.request, matching.basis)


def test_result_conversion_helpers_accept_legacy_shapes_and_reject_unknowns() -> None:
    data = _ready_data(
        artifacts=InMemoryArtifactRepository(),
        lineage=InMemoryCandidateLineageRepository(),
    )
    assert data.bundle is not None
    assert data.candidate is not None
    assert data.materialization is not None
    changes = data.bundle.observed_changes

    assert _as_proposal_result(changes).observed_changes == changes
    assert _as_proposal_result((changes, SimpleNamespace(usage=7))).token_usage == 7
    assert (
        _as_proposal_result(SimpleNamespace(observed_changes=changes)).observed_changes == changes
    )
    with pytest.raises(MemoryWriteWorkflowError, match="propose"):
        _as_proposal_result(object())

    assert _as_repair_result(changes).observed_changes == changes
    assert _as_repair_result(SimpleNamespace(observed_changes=changes)).observed_changes == changes
    with pytest.raises(MemoryWriteWorkflowError, match="repair"):
        _as_repair_result(object())

    transformed = data.candidate.model_copy(
        update={"content_hash": ArtifactId("sha256:" + "8" * 64)}
    )
    assert _as_normalization_result(data.candidate, data.candidate).status is (
        NormalizationStatus.UNCHANGED
    )
    assert _as_normalization_result(transformed, data.candidate).status is (
        NormalizationStatus.TRANSFORMED
    )
    with pytest.raises(MemoryWriteWorkflowError, match="Normalizer"):
        _as_normalization_result(object(), data.candidate)

    root = RootMaterializationResult(
        materialization=data.materialization,
        bundle=data.bundle,
        world_mutation_noop=False,
    )
    assert _as_materialization_result((data.materialization, data.bundle)).bundle == data.bundle
    assert _as_materialization_result(root) == root
    assert (
        cast(
            Any,
            _as_materialization_result(SimpleNamespace(materialization=1, bundle=2)),
        ).bundle
        == 2
    )
    with pytest.raises(MemoryWriteWorkflowError, match="RootUpdatePort"):
        _as_materialization_result(object())


def test_miscellaneous_helpers_cover_defaults_and_error_contracts() -> None:
    request = _request()
    artifacts = InMemoryArtifactRepository()
    data = _WorkflowData(request=request, artifacts=artifacts)
    changes = ObservedChangeSet(
        change_set_id=StableId("changes.edges"),
        base_commit=BASE,
        source_artifact=_artifact("9"),
    )
    assert isinstance(
        _as_proposal_result(CuratorProposalResult(observed_changes=changes)), CuratorProposalResult
    )
    assert isinstance(
        _as_repair_result(CuratorRepairResult(observed_changes=changes)), CuratorRepairResult
    )
    assert _as_repair_result_or_none("value") == "value"
    normalization = NormalizationResult(
        status=NormalizationStatus.UNCHANGED,
        candidate=_candidate(_artifact("e")),
    )
    assert _as_normalization_result(normalization, normalization.candidate) == normalization

    receipt = agent_receipt().model_copy(
        update={
            "agent_type": AgentType.MEMORY_GUARDIAN,
            "agent_mode": AgentMode.RISK_REVIEW,
            "base_commit": BASE,
        }
    )
    decision = GuardianDecision(
        decision_id=StableId("guardian.edges"),
        proposal_id=StableId("proposal.edges"),
        base_commit=BASE,
        outcome=GuardianOutcome.APPROVE,
        risk_codes=(),
        reasons=("safe",),
        receipt=receipt,
    )
    guardian = SimpleNamespace(decision=decision, receipt=None)
    assert _as_guardian_result(guardian).decision == decision
    with pytest.raises(MemoryWriteWorkflowError, match="GuardianPort"):
        _as_guardian_result(object())

    raw_commit = SimpleNamespace(
        request_id=request.request_id,
        status=SimpleNamespace(value="rejected"),
        reason="no",
    )
    assert _as_commit_result(raw_commit).status == "rejected"

    assert _earliest_position([]) is None
    assert (
        _earliest_position(
            [NarrativePosition(chapter_index=2), NarrativePosition(chapter_index=1)]
        ).chapter_index
        == 1
    )
    assert _narrowest_scope(()) is AccessScope.WRITER_SAFE
    assert _narrowest_scope((AccessScope.EVALUATOR, AccessScope.AUTHOR_PLANNING)) is (
        AccessScope.AUTHOR_PLANNING
    )
    assert _roots_changed(_manifest(world="9"), _manifest())
    assert not _roots_changed(_manifest(), _manifest())
    assert _source_artifact(request).media_type.endswith("empty-source+json")
    assert _candidate_id(data).root.startswith("candidate.missing")
    assert _current_commit(data) == BASE
    assert _phase_for_data(data) is MemoryWriteWorkflowPhase.PRECOMMIT
    assert _require("x") == "x"
    with pytest.raises(MemoryWriteWorkflowError, match="required"):
        _require(None)

    remaining = _remaining(request.budget, data.usage)
    assert remaining.candidate_revisions == request.budget.max_candidate_revisions
    assert _receipt_ref(data, "edge").media_type.endswith("edge-receipt+json")

    no_artifacts = _WorkflowData(request=request)
    with pytest.raises(MemoryWriteWorkflowError, match="unavailable"):
        _model_ref(request, no_artifacts, "request")
    with pytest.raises(MemoryWriteWorkflowError, match="unavailable"):
        _receipt_ref(no_artifacts, "edge")

    class InvalidArtifacts:
        def put(self, *_: object) -> object:
            return object()

    with pytest.raises(MemoryWriteWorkflowError, match="invalid reference"):
        _model_ref(
            request,
            _WorkflowData(request=request, artifacts=InvalidArtifacts()),
            "request",
        )

    fallback = SimpleNamespace(
        put=lambda raw, media, version: artifacts.put(raw, media, version),
        read_verified=artifacts.read_verified,
    )
    ref = _put_model(fallback, request, "application/json", VERSION)
    assert _read_model(fallback, ref, type(request)) == request
    assert asyncio.run(_maybe_await(asyncio.sleep(0, result=3))) == 3
    assert asyncio.run(_maybe_await(4)) == 4
    assert awaitable_result(5) == 5

    async def value() -> int:
        return 1

    pending = value()
    try:
        with pytest.raises(MemoryWriteWorkflowError, match="synchronous"):
            awaitable_result(pending)
    finally:
        pending.close()


def test_execute_maps_initialize_and_all_exception_families_to_results() -> None:
    request = _request()
    initialized = MemoryWriteWorkflowResult(
        request_id=request.request_id,
        status=MemoryWriteWorkflowStatus.NOOP,
        workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
        canonical_commit_accepted=False,
        base_commit=BASE,
    )

    class Harness(LocalMemoryWriteWorkflow):
        initial: MemoryWriteWorkflowResult | None = None
        error: Exception | None = None

        async def _initialize(self, request: object) -> Any:
            if self.initial is not None:
                return self.initial
            return _WorkflowData(request=cast(Any, request), artifacts=self._artifacts)

        async def _step(self, data: _WorkflowData) -> Any:
            assert self.error is not None
            raise self.error

    workflow = Harness(canonical_read=cast(Any, object()), commit=object())
    workflow.initial = initialized
    initialized_result = asyncio.run(workflow.execute(request))
    assert initialized_result.model_copy(update={"terminal_result_ref": None}) == initialized
    assert initialized_result.terminal_result_ref is not None

    for error, code in (
        (InformationBoundaryViolation("boundary"), "INFORMATION_DERIVATION_BOUNDARY_VIOLATION"),
        (MemoryWriteIdentityCollision("collision"), "IDEMPOTENCY_IDENTITY_COLLISION"),
        (MemoryWriteWorkflowError("workflow"), "workflow"),
        (ValueError("value"), "value"),
        (OSError("unexpected"), "UNEXPECTED_WORKFLOW_FAILURE"),
    ):
        workflow = Harness(canonical_read=cast(Any, object()), commit=object())
        workflow.error = error
        result = asyncio.run(workflow.execute(request))
        assert result.status is MemoryWriteWorkflowStatus.FATAL
        assert code in result.terminal_codes


def test_trusted_candidate_binding_success_and_failures() -> None:
    workflow, data = _workflow_and_data()
    assert data.bundle is not None
    receipt = _artifact(
        "e",
        "application/vnd.novel-agent.boundary-propagation-receipt+json",
    )
    payload = MemoryWriteCandidatePayload(
        observed_changes=data.bundle.observed_changes,
        root_update_intents=data.request.root_update_intents,
        commit_profile=data.request.commit_profile,
    )
    artifact = workflow._artifacts.put(
        payload.model_dump_json().encode(),
        "application/vnd.novel-agent.memory-write-candidate+json",
        VERSION,
    )
    data.request = data.request.model_copy(
        update={
            "world_mutation": TrustedWorldCandidateInput(
                candidate_artifact=artifact,
                producer_receipt=receipt,
            )
        }
    )
    data.state = MemoryWriteState.PREPARE_CANDIDATE
    assert asyncio.run(workflow._step(data)) is None
    assert data.candidate is not None
    assert data.candidate.producer_kind is CandidateProducerKind.TRUSTED_CANDIDATE

    workflow, data = _workflow_and_data()
    mismatched = payload.model_copy(
        update={"root_update_intents": (_request().root_update_intents)}
    )
    mismatched = mismatched.model_copy(
        update={
            "commit_profile": type(data.request.commit_profile).CHAPTER_REVEAL_ATOMIC,
        }
    )
    artifact = workflow._artifacts.put(
        mismatched.model_dump_json().encode(),
        "application/vnd.novel-agent.memory-write-candidate+json",
        VERSION,
    )
    with pytest.raises(InformationBoundaryViolation, match="differs"):
        workflow._candidate_from_trusted(data, artifact)

    artifact = workflow._artifacts.put(
        payload.model_dump_json().encode(),
        "application/vnd.novel-agent.memory-write-candidate+json",
        VERSION,
    )
    with pytest.raises(InformationBoundaryViolation, match="input is missing"):
        workflow._candidate_from_trusted(data, artifact)


def test_candidate_receipt_port_degradation_and_parent_edges() -> None:
    workflow, data = _workflow_and_data()
    output = _artifact("c")
    original = _artifact(
        "e",
        "application/vnd.novel-agent.boundary-propagation-receipt+json",
    )
    assert (
        workflow._candidate_producer_receipt(
            data,
            output,
            None,
            original,
            CandidateProducerKind.TRUSTED_CANDIDATE,
        )
        == original
    )

    class VerifyOnly:
        def verify_derivation_chain(self, **kwargs: object) -> None:
            return None

    workflow._boundary = VerifyOnly()
    assert (
        workflow._candidate_producer_receipt(
            data, output, None, None, CandidateProducerKind.CURATOR_REPAIR
        )
        is None
    )
    assert (
        workflow._candidate_producer_receipt(
            data, output, None, original, CandidateProducerKind.CURATOR_REPAIR
        )
        == original
    )

    workflow, data = _workflow_and_data()
    data.request = data.request.model_copy(update={"source_artifacts": (_artifact("9"),)})

    class InvalidRegister:
        def register_derivation(self, *args: object, **kwargs: object) -> object:
            return object()

        def verify_derivation_chain(self, **kwargs: object) -> None:
            return None

    workflow._boundary = InvalidRegister()
    with pytest.raises(InformationBoundaryViolation, match="invalid receipt reference"):
        workflow._candidate_producer_receipt(
            data, output, None, None, CandidateProducerKind.CURATOR_REPAIR
        )

    workflow._boundary = object()
    with pytest.raises(InformationBoundaryViolation, match="cannot read"):
        workflow._read_derivation_receipt(original)
    workflow._boundary = type(
        "InvalidReader",
        (),
        {"read_derivation_receipt": lambda self, reference: object()},
    )()
    with pytest.raises(InformationBoundaryViolation, match="invalid propagation"):
        workflow._read_derivation_receipt(original)


def test_parent_receipt_is_propagated_and_missing_parent_receipt_fails() -> None:
    workflow, data = _workflow_and_data()
    assert data.candidate is not None
    parent = data.candidate
    parent_ref = _artifact(
        "e",
        "application/vnd.novel-agent.boundary-propagation-receipt+json",
    )
    parent = parent.model_copy(update={"producer_receipt": parent_ref})
    receipt = BoundaryPropagationReceipt(
        receipt_id=StableId("receipt.parent.edge"),
        boundary_id=data.request.information_boundary.boundary_id,
        base_commit=BASE,
        input_source_artifact_refs=(_artifact("9"),),
        output_artifact_hash=parent.candidate_artifact.artifact_id,
        builder_policy_hash=data.request.configuration_fingerprint,
        effective_visible_through=NarrativePosition(chapter_index=1),
        effective_access_scope=AccessScope.WRITER_SAFE,
        receipt_hash=ArtifactId("sha256:" + "7" * 64),
    )
    intent_receipt = receipt.model_copy(update={"effective_visible_through": None})

    class Boundary:
        def read_derivation_receipt(self, reference: object) -> BoundaryPropagationReceipt:
            return receipt if reference == parent_ref else intent_receipt

        def register_derivation(self, *args: object, **kwargs: object) -> Any:
            return _artifact(
                "8",
                "application/vnd.novel-agent.boundary-propagation-receipt+json",
            )

        def verify_derivation_chain(self, **kwargs: object) -> None:
            return None

    workflow._boundary = Boundary()
    registered = workflow._candidate_producer_receipt(
        data,
        _artifact("c"),
        parent,
        None,
        CandidateProducerKind.CURATOR_REPAIR,
    )
    assert registered is not None

    data.request = _request(
        profile=type(data.request.commit_profile).CHAPTER_REVEAL_ATOMIC,
        chapter_text_changed=True,
    )
    registered = workflow._candidate_producer_receipt(
        data,
        _artifact("c"),
        None,
        None,
        CandidateProducerKind.CURATOR_REPAIR,
    )
    assert registered is not None

    parent_without_position = parent.model_copy(
        update={
            "producer_receipt": _artifact(
                "6",
                "application/vnd.novel-agent.boundary-propagation-receipt+json",
            )
        }
    )
    registered = workflow._candidate_producer_receipt(
        data,
        _artifact("c"),
        parent_without_position,
        None,
        CandidateProducerKind.CURATOR_REPAIR,
    )
    assert registered is not None

    missing = parent.model_copy(update={"producer_receipt": None})
    with pytest.raises(InformationBoundaryViolation, match="parent is missing"):
        workflow._candidate_producer_receipt(
            data,
            _artifact("c"),
            missing,
            None,
            CandidateProducerKind.CURATOR_REPAIR,
        )


def test_persist_candidate_missing_parent_receipt_and_seen_hash_branches() -> None:
    workflow, data = _workflow_and_data()
    assert data.candidate is not None
    candidate = data.candidate
    missing_parent = candidate.model_copy(
        update={
            "candidate_id": StableId("candidate.missing.parent.edge"),
            "parent_candidate_id": StableId("candidate.parent.absent"),
            "revision_no": 2,
        }
    )
    with pytest.raises(MemoryWriteWorkflowError, match="parent is missing"):
        workflow._persist_candidate(data, missing_parent)

    class VerifyOnly:
        def verify_derivation_chain(self, **kwargs: object) -> None:
            return None

    workflow._boundary = VerifyOnly()
    without_receipt = candidate.model_copy(
        update={
            "candidate_id": StableId("candidate.no.receipt.edge"),
            "producer_receipt": None,
        }
    )
    with pytest.raises(InformationBoundaryViolation, match="missing a propagation"):
        workflow._persist_candidate(data, without_receipt)

    workflow, data = _workflow_and_data()
    assert data.candidate is not None
    data.seen_content_hashes.append(data.candidate.content_hash)
    assert workflow._persist_candidate(data, data.candidate) == data.candidate


def test_constructor_injected_ports_and_pure_delta_receipt_branches() -> None:
    workflow, data = _workflow_and_data()
    custom = LocalMemoryWriteWorkflow(
        canonical_read=workflow._canonical_read,
        commit=workflow._commit_port,
        normalizer=object(),
        root_updates=object(),
        information_boundary=workflow._boundary,
    )
    assert custom._normalizer is not None
    assert custom._root_updates is not None

    class Register:
        def register_derivation(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("pure delta must not register a synthetic receipt")

    workflow._boundary = Register()
    assert (
        workflow._candidate_producer_receipt(
            data,
            _artifact("c"),
            None,
            None,
            CandidateProducerKind.EMPTY_DELTA,
        )
        is None
    )


def test_current_commit_attribute_fallback() -> None:
    _, data = _workflow_and_data()
    data_any: Any = data
    data_any.basis = object()
    assert _current_commit(data) == data.request.base_commit
