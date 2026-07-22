from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    AuthorApprovalDecision,
    AuthorApprovalRequest,
    AuthorApprovalStatus,
    BootstrapStrategy,
    ContractRef,
    ExecutionStatus,
    PlanProposal,
    ProjectProfileRootDocument,
    PromptContractRef,
    ProposalProvenance,
    ProposedItem,
    ReferenceAsset,
    ReferenceRootDocument,
    SkillContractRef,
    SourceClass,
    SourceClassification,
    SourceDestination,
    WorldPatchCandidate,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.bootstrap_workflow import (
    BootstrapCrossRootValidator,
    BootstrapRootBuilder,
    BootstrapRootCandidates,
    BootstrapWorkflowError,
    GenesisCoordinator,
    InMemoryAuthorApprovalRepository,
    SqlAuthorApprovalRepository,
    candidate_manifest_hash,
    project_profile_root_content_id,
    reference_root_content_id,
    utc_now,
)
from novel_agent.services.commits import CommitService
from tests.factories import make_artifact

VERSION = SchemaVersion("2.0.0")
HASH_A = ArtifactId("sha256:" + "a" * 64)
HASH_B = ArtifactId("sha256:" + "b" * 64)
GENESIS_BASIS = CommitId("sha256:" + "0" * 64)
NOW = datetime(2026, 7, 21, tzinfo=UTC)


@pytest.fixture
def database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine, build_session_factory(engine)
    engine.dispose()


def receipt(agent_type: AgentType, mode: AgentMode) -> AgentExecutionReceipt:
    return AgentExecutionReceipt(
        receipt_id=StableId(f"receipt.{agent_type.value}.{mode.value}"),
        run_id=RunId("run.bootstrap"),
        task_id=TaskId("task.bootstrap"),
        agent_spec=ContractRef(
            contract_id=StableId(f"agent.{agent_type.value}.{mode.value}"),
            version=VERSION,
            content_hash=HASH_A,
        ),
        agent_type=agent_type,
        agent_mode=mode,
        prompt_fingerprint=HASH_A,
        configuration_fingerprint=HASH_B,
        status=ExecutionStatus.SUCCEEDED,
        started_at=NOW,
        completed_at=NOW,
        latency_ms=0,
    )


def profile() -> ProjectProfileRootDocument:
    contract = ContractRef(
        contract_id=StableId("agent.planner"), version=VERSION, content_hash=HASH_A
    )
    prompt = PromptContractRef(
        contract_id=StableId("prompt.system"),
        version=VERSION,
        content_hash=HASH_A,
        render_fingerprint=HASH_B,
    )
    skill = SkillContractRef(
        contract_id=StableId("skill.bootstrap"), version=VERSION, content_hash=HASH_A
    )
    return ProjectProfileRootDocument(
        root_hash=HASH_A,
        schema_version=VERSION,
        agent_specs=(contract,),
        prompt_contracts=(prompt,),
        skill_contracts=(skill,),
        tool_policies=(
            ContractRef(
                contract_id=StableId("tool-policy.bootstrap"),
                version=VERSION,
                content_hash=HASH_A,
            ),
        ),
        model_profiles=("fake-model@v1",),
    )


def proposals(project_id: ProjectId) -> tuple[PlanProposal, WorldPatchCandidate]:
    plan = PlanProposal(
        proposal_id=StableId("proposal.plan"),
        project_id=project_id,
        mode=AgentMode.PROJECT_BOOTSTRAP,
        strategy=BootstrapStrategy.NORMALIZE_ONLY,
        items=(
            ProposedItem(
                item_id=StableId("plan.item"),
                kind="premise",
                payload={"summary": "story"},
                provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                source_ids=(StableId("source.brief"),),
            ),
        ),
        coverage=1,
        receipt=receipt(AgentType.PLANNER, AgentMode.PROJECT_BOOTSTRAP),
    )
    world = WorldPatchCandidate(
        proposal_id=StableId("proposal.world"),
        project_id=project_id,
        items=(
            ProposedItem(
                item_id=StableId("world.item"),
                kind="baseline_state",
                payload={"state": "known"},
                provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                source_ids=(StableId("source.setting"),),
            ),
        ),
        origin_source_ids=(StableId("source.setting"),),
        extraction_coverage=1,
        receipt=receipt(AgentType.MEMORY_CURATOR, AgentMode.BOOTSTRAP),
    )
    return plan, world


def classifications() -> tuple[SourceClassification, ...]:
    return (
        SourceClassification(
            source_id=StableId("source.brief"),
            source_class=SourceClass.AUTHOR_INITIAL_BRIEF,
            allowed_destinations=(SourceDestination.PLAN, SourceDestination.REFERENCE),
            classification_reason="brief",
        ),
        SourceClassification(
            source_id=StableId("source.setting"),
            source_class=SourceClass.BASELINE_SETTING,
            allowed_destinations=(SourceDestination.WORLD, SourceDestination.REFERENCE),
            classification_reason="setting",
        ),
    )


def candidates(tmp_path: Path, project_id: ProjectId | None = None) -> BootstrapRootCandidates:
    project_id = project_id or ProjectId("project.bootstrap")
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    source_artifact = artifacts.put(b"setting", "text/plain", VERSION)
    reference = ReferenceRootDocument(
        root_hash=HASH_A,
        schema_version=VERSION,
        assets=(
            ReferenceAsset(
                asset_id=StableId("reference.setting"),
                source_id=StableId("source.setting"),
                source_class=SourceClass.BASELINE_SETTING,
                artifact=source_artifact,
            ),
        ),
    )
    plan_proposal, world_patch = proposals(project_id)
    return BootstrapRootBuilder(artifacts).build(
        project_id,
        StableId("bootstrap.bundle"),
        TextRootDocument(root_hash=HASH_A, schema_version=VERSION, chapters=()),
        PlanRootDocument(root_hash=HASH_A, schema_version=VERSION),
        WorldRootDocument(root_hash=HASH_A, schema_version=VERSION, source_commit=GENESIS_BASIS),
        reference,
        profile(),
        plan_proposal,
        world_patch,
        classifications(),
    )


def test_root_builder_hashes_and_persists_five_root_candidates(tmp_path: Path) -> None:
    built = candidates(tmp_path)

    assert built.text.root_hash != HASH_A
    assert built.reference.root_hash == reference_root_content_id(built.reference)
    assert built.profile.root_hash == project_profile_root_content_id(built.profile)
    assert built.manifest.parent_commit_ids == ()
    assert built.manifest.project_id == ProjectId("project.bootstrap")
    assert utc_now().tzinfo is not None
    with pytest.raises(TypeError, match="versioned domain model"):
        BootstrapRootBuilder(
            ArtifactRepository(FilesystemObjectStore(tmp_path / "invalid"))
        )._store(object())

    other_plan, world = proposals(ProjectId("project.other"))
    with pytest.raises(BootstrapWorkflowError, match="another project"):
        BootstrapRootBuilder(ArtifactRepository(FilesystemObjectStore(tmp_path / "other"))).build(
            ProjectId("project.bootstrap"),
            StableId("bundle.other"),
            built.text,
            built.plan,
            built.world,
            built.reference,
            built.profile,
            other_plan,
            world,
            classifications(),
        )


def test_bootstrap_contracts_reject_duplicate_or_unpinned_roots() -> None:
    asset = ReferenceAsset(
        asset_id=StableId("asset.1"),
        source_id=StableId("source.1"),
        source_class=SourceClass.BASELINE_SETTING,
        artifact=make_artifact(),
    )
    with pytest.raises(ValidationError, match="asset ids"):
        ReferenceRootDocument(root_hash=HASH_A, schema_version=VERSION, assets=(asset, asset))
    with pytest.raises(ValidationError, match="source ids"):
        ReferenceRootDocument(
            root_hash=HASH_A,
            schema_version=VERSION,
            assets=(asset, asset.model_copy(update={"asset_id": StableId("asset.2")})),
        )
    with pytest.raises(ValidationError, match="must pin"):
        ProjectProfileRootDocument(
            root_hash=HASH_A,
            schema_version=VERSION,
            agent_specs=(),
            prompt_contracts=(),
            skill_contracts=(),
            tool_policies=(),
            model_profiles=(),
        )
    pinned = profile()
    with pytest.raises(ValidationError, match="unique"):
        ProjectProfileRootDocument.model_validate(
            pinned.model_dump() | {"agent_specs": pinned.agent_specs * 2}
        )


def test_cross_root_validation_and_author_approved_genesis_are_idempotent(
    tmp_path: Path, database: tuple[Engine, sessionmaker[Session]]
) -> None:
    built = candidates(tmp_path)
    validation = BootstrapCrossRootValidator(lambda: NOW).validate(built)
    assert validation.status.value == "passed"
    approvals = InMemoryAuthorApprovalRepository()
    coordinator = GenesisCoordinator(CommitService(database[1]), approvals, lambda: NOW)
    approval = coordinator.create_approval_request(built, validation)

    with pytest.raises(BootstrapWorkflowError, match="explicit author approval"):
        coordinator.commit(built, validation, approval.approval_request_id)
    decision = AuthorApprovalDecision(
        decision_id=StableId("decision.approve"),
        approval_request_id=approval.approval_request_id,
        project_id=approval.project_id,
        candidate_manifest_hash=approval.candidate_manifest_hash,
        validation_report_id=approval.validation_report_id,
        status=AuthorApprovalStatus.APPROVED,
        author_id=StableId("author.1"),
        reason="reviewed all roots",
        decided_at=NOW,
    )
    approvals.decide(decision)
    first = coordinator.commit(built, validation, approval.approval_request_id)
    replay = coordinator.commit(built, validation, approval.approval_request_id)

    assert first.commit_id == replay.commit_id
    assert not first.idempotent_replay
    assert replay.idempotent_replay
    assert first.candidate_manifest_hash == candidate_manifest_hash(built.manifest)


def test_cross_root_validator_blocks_wrong_authority_routes_and_future_leakage(
    tmp_path: Path,
) -> None:
    built = candidates(tmp_path)
    private = SourceClassification(
        source_id=StableId("source.private"),
        source_class=SourceClass.FUTURE_TEXT_PRIVATE,
        allowed_destinations=(SourceDestination.EVALUATION,),
        classification_reason="private",
    )
    leaked_item = built.plan_proposal.items[0].model_copy(
        update={"source_ids": (StableId("source.private"), StableId("source.unknown"))}
    )
    bad_plan = built.plan_proposal.model_copy(
        update={
            "items": (leaked_item,),
            "receipt": receipt(AgentType.MEMORY_CONTROLLER, AgentMode.BOUNDED_R2),
        }
    )
    bad_world = built.world_patch.model_copy(
        update={"receipt": receipt(AgentType.PLANNER, AgentMode.STORY)}
    )
    bad_manifest = built.manifest.model_copy(update={"parent_commit_ids": (GENESIS_BASIS,)})
    bad_text_ref = bad_manifest.text_root.model_copy(update={"artifact_id": HASH_A})
    bad_manifest = bad_manifest.model_copy(update={"text_root": bad_text_ref})
    malformed = BootstrapRootCandidates(
        bootstrap_bundle_id=built.bootstrap_bundle_id,
        manifest=bad_manifest,
        text=built.text,
        plan=built.plan,
        world=built.world,
        reference=built.reference,
        profile=built.profile,
        plan_proposal=bad_plan,
        world_patch=bad_world,
        classifications=(*built.classifications, private, built.classifications[0]),
    )
    report = BootstrapCrossRootValidator(lambda: NOW).validate(malformed)
    codes = {finding.code for finding in report.findings}

    assert report.status.value == "failed"
    assert "BOOTSTRAP_DUPLICATE_CLASSIFICATION" in codes
    assert "BOOTSTRAP_GENESIS_PARENT" in codes
    assert "BOOTSTRAP_PLAN_AUTHORITY" in codes
    assert "BOOTSTRAP_PLAN_MODE" in codes
    assert "BOOTSTRAP_WORLD_AUTHORITY" in codes
    assert "BOOTSTRAP_WORLD_MODE" in codes
    assert "BOOTSTRAP_PLAN_FUTURE_LEAKAGE" in codes
    assert "BOOTSTRAP_PLAN_ROUTE_DENIED" in codes
    assert "BOOTSTRAP_PLAN_UNKNOWN_SOURCE" in codes
    assert "BOOTSTRAP_TEXT_ARTIFACT_MISMATCH" in codes


def test_approval_repository_rejects_stale_colliding_and_nonterminal_decisions(
    tmp_path: Path,
) -> None:
    built = candidates(tmp_path)
    validation = BootstrapCrossRootValidator(lambda: NOW).validate(built)
    repository = InMemoryAuthorApprovalRepository()
    request = AuthorApprovalRequest(
        approval_request_id=StableId("approval.1"),
        project_id=built.manifest.project_id,
        bootstrap_bundle_id=built.bootstrap_bundle_id,
        candidate_manifest_hash=candidate_manifest_hash(built.manifest),
        validation_report_id=validation.report_id,
        requested_at=NOW,
    )
    assert repository.request(request) == request
    assert repository.request(request) == request
    collision = request.model_copy(update={"bootstrap_bundle_id": StableId("bundle.other")})
    with pytest.raises(BootstrapWorkflowError, match="collision"):
        repository.request(collision)
    with pytest.raises(ValidationError, match="cannot remain pending"):
        AuthorApprovalDecision(
            decision_id=StableId("decision.pending"),
            approval_request_id=request.approval_request_id,
            project_id=request.project_id,
            candidate_manifest_hash=request.candidate_manifest_hash,
            validation_report_id=request.validation_report_id,
            status=AuthorApprovalStatus.PENDING,
            author_id=StableId("author.1"),
            reason="pending",
            decided_at=NOW,
        )
    with pytest.raises(BootstrapWorkflowError, match="unknown approval"):
        repository.load_request(StableId("approval.unknown"))
    with pytest.raises(BootstrapWorkflowError, match="unknown approval"):
        repository.decide(
            AuthorApprovalDecision(
                decision_id=StableId("decision.unknown"),
                approval_request_id=StableId("approval.unknown"),
                project_id=request.project_id,
                candidate_manifest_hash=request.candidate_manifest_hash,
                validation_report_id=request.validation_report_id,
                status=AuthorApprovalStatus.REJECTED,
                author_id=StableId("author.1"),
                reason="unknown",
                decided_at=NOW,
            )
        )
    mismatched = AuthorApprovalDecision(
        decision_id=StableId("decision.mismatch"),
        approval_request_id=request.approval_request_id,
        project_id=ProjectId("project.other"),
        candidate_manifest_hash=request.candidate_manifest_hash,
        validation_report_id=request.validation_report_id,
        status=AuthorApprovalStatus.REJECTED,
        author_id=StableId("author.1"),
        reason="wrong project",
        decided_at=NOW,
    )
    with pytest.raises(BootstrapWorkflowError, match="does not match"):
        repository.decide(mismatched)
    rejected = mismatched.model_copy(
        update={
            "decision_id": StableId("decision.reject"),
            "project_id": request.project_id,
        }
    )
    assert repository.decide(rejected) == rejected
    assert repository.decide(rejected) == rejected
    assert (
        repository.decide(rejected.model_copy(update={"decided_at": NOW + timedelta(seconds=1)}))
        == rejected
    )
    with pytest.raises(BootstrapWorkflowError, match="already decided differently"):
        repository.decide(rejected.model_copy(update={"reason": "changed reason"}))


def test_sql_approval_repository_resumes_after_reconstruction(
    tmp_path: Path, database: tuple[Engine, sessionmaker[Session]]
) -> None:
    built = candidates(tmp_path)
    validation = BootstrapCrossRootValidator(lambda: NOW).validate(built)
    first_repository = SqlAuthorApprovalRepository(database[1])
    coordinator = GenesisCoordinator(CommitService(database[1]), first_repository, lambda: NOW)
    request = coordinator.create_approval_request(built, validation)
    assert first_repository.request(request) == request
    reconstructed = SqlAuthorApprovalRepository(database[1])
    assert reconstructed.load_request(request.approval_request_id) == request
    assert reconstructed.load_decision(request.approval_request_id) is None
    with pytest.raises(BootstrapWorkflowError, match="collision"):
        reconstructed.request(
            request.model_copy(update={"bootstrap_bundle_id": StableId("bundle.changed")})
        )
    with pytest.raises(BootstrapWorkflowError, match="unknown approval"):
        reconstructed.load_request(StableId("approval.missing"))
    unknown_decision = AuthorApprovalDecision(
        decision_id=StableId("decision.missing"),
        approval_request_id=StableId("approval.missing"),
        project_id=request.project_id,
        candidate_manifest_hash=request.candidate_manifest_hash,
        validation_report_id=request.validation_report_id,
        status=AuthorApprovalStatus.REJECTED,
        author_id=StableId("author.sql"),
        reason="missing",
        decided_at=NOW,
    )
    with pytest.raises(BootstrapWorkflowError, match="unknown approval"):
        reconstructed.decide(unknown_decision)
    decision = AuthorApprovalDecision(
        decision_id=StableId("decision.sql"),
        approval_request_id=request.approval_request_id,
        project_id=request.project_id,
        candidate_manifest_hash=request.candidate_manifest_hash,
        validation_report_id=request.validation_report_id,
        status=AuthorApprovalStatus.APPROVED,
        author_id=StableId("author.sql"),
        reason="approved after restart",
        decided_at=NOW,
    )
    assert reconstructed.decide(decision) == decision
    assert reconstructed.decide(decision) == decision
    assert (
        reconstructed.decide(decision.model_copy(update={"decided_at": NOW + timedelta(seconds=1)}))
        == decision
    )
    with pytest.raises(BootstrapWorkflowError, match="already decided differently"):
        reconstructed.decide(decision.model_copy(update={"reason": "different"}))
    resumed = GenesisCoordinator(CommitService(database[1]), reconstructed, lambda: NOW)
    receipt = resumed.commit(built, validation, request.approval_request_id)

    assert receipt.commit_id == CommitService(database[1]).current_commit(request.project_id)
    assert (
        reconstructed.load_request(request.approval_request_id).status
        is AuthorApprovalStatus.APPROVED
    )
    assert reconstructed.load_decision(request.approval_request_id) == decision


def test_genesis_coordinator_rejects_untrusted_reports_stale_approval_and_other_genesis(
    tmp_path: Path, database: tuple[Engine, sessionmaker[Session]]
) -> None:
    built = candidates(tmp_path)
    validation = BootstrapCrossRootValidator(lambda: NOW).validate(built)
    approvals = InMemoryAuthorApprovalRepository()
    coordinator = GenesisCoordinator(CommitService(database[1]), approvals, lambda: NOW)
    with pytest.raises(BootstrapWorkflowError, match="another bundle"):
        coordinator.create_approval_request(
            built,
            validation.model_copy(update={"bundle_id": StableId("bundle.wrong")}),
        )
    with pytest.raises(BootstrapWorkflowError, match="failed bootstrap"):
        coordinator.create_approval_request(
            built, validation.model_copy(update={"status": "failed"})
        )
    with pytest.raises(BootstrapWorkflowError, match="stale or untrusted"):
        coordinator.create_approval_request(
            built,
            validation.model_copy(update={"report_id": StableId("validation.fake")}),
        )
    request = coordinator.create_approval_request(built, validation)
    decision = AuthorApprovalDecision(
        decision_id=StableId("decision.valid"),
        approval_request_id=request.approval_request_id,
        project_id=request.project_id,
        candidate_manifest_hash=request.candidate_manifest_hash,
        validation_report_id=request.validation_report_id,
        status=AuthorApprovalStatus.APPROVED,
        author_id=StableId("author.1"),
        reason="approved",
        decided_at=NOW,
    )
    approvals.decide(decision)
    with pytest.raises(BootstrapWorkflowError, match="requires passed validation"):
        coordinator.commit(
            built, validation.model_copy(update={"status": "failed"}), request.approval_request_id
        )
    stale_validation = validation.model_copy(
        update={"report_id": StableId("bootstrap-validation." + "f" * 24)}
    )
    with pytest.raises(BootstrapWorkflowError, match="stale or untrusted"):
        coordinator.commit(built, stale_validation, request.approval_request_id)
    first = coordinator.commit(built, validation, request.approval_request_id)
    assert first.commit_id

    changed_profile = built.profile.model_copy(update={"style_profile": {"voice": "changed"}})
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "alternate"))
    changed = BootstrapRootBuilder(artifacts).build(
        built.manifest.project_id,
        StableId("bootstrap.bundle.changed"),
        built.text,
        built.plan,
        built.world,
        built.reference,
        changed_profile,
        built.plan_proposal,
        built.world_patch,
        built.classifications,
    )
    changed_validation = BootstrapCrossRootValidator(lambda: NOW).validate(changed)
    changed_request = coordinator.create_approval_request(changed, changed_validation)
    approvals.decide(
        AuthorApprovalDecision(
            decision_id=StableId("decision.changed"),
            approval_request_id=changed_request.approval_request_id,
            project_id=changed_request.project_id,
            candidate_manifest_hash=changed_request.candidate_manifest_hash,
            validation_report_id=changed_request.validation_report_id,
            status=AuthorApprovalStatus.APPROVED,
            author_id=StableId("author.1"),
            reason="alternate approved",
            decided_at=NOW,
        )
    )
    with pytest.raises(BootstrapWorkflowError, match="different Genesis"):
        coordinator.commit(changed, changed_validation, changed_request.approval_request_id)


def test_genesis_commit_detects_approval_basis_staleness(
    tmp_path: Path, database: tuple[Engine, sessionmaker[Session]]
) -> None:
    built = candidates(tmp_path)
    validation = BootstrapCrossRootValidator(lambda: NOW).validate(built)
    approvals = InMemoryAuthorApprovalRepository()
    coordinator = GenesisCoordinator(CommitService(database[1]), approvals, lambda: NOW)
    request = coordinator.create_approval_request(built, validation)
    approvals.decide(
        AuthorApprovalDecision(
            decision_id=StableId("decision.stale"),
            approval_request_id=request.approval_request_id,
            project_id=request.project_id,
            candidate_manifest_hash=request.candidate_manifest_hash,
            validation_report_id=request.validation_report_id,
            status=AuthorApprovalStatus.APPROVED,
            author_id=StableId("author.1"),
            reason="approved old basis",
            decided_at=NOW,
        )
    )
    stale_request = request.model_copy(update={"bootstrap_bundle_id": StableId("bundle.stale")})
    approvals._requests[request.approval_request_id] = stale_request
    with pytest.raises(BootstrapWorkflowError, match="basis is stale"):
        coordinator.commit(built, validation, request.approval_request_id)
