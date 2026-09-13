"""D0 mechanical recovery on the immutable v24 ARC_VOLUME failure artifacts.

The v23 test covers the content-side composition shape.  This test keeps the v24
parent, the stale host review and the actual failed revision response in view, then
replays the repaired path with the current contracts: field-level host identities,
structured unresolved operations, scoped composition, and the public Plan materializer.
It never writes to the v24 workspace or its database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.runtime.materializers import (
    PLAN_COMPOSITION_MEDIA_TYPE,
    PLAN_PROPOSAL_MEDIA_TYPE,
    PLAN_ROOT_MEDIA_TYPE,
    PLANNER_EXECUTION_MEDIA_TYPE,
    PLANNING_EVENT_MEDIA_TYPE,
    PlanCandidateMaterializer,
)
from novel_agent.agents.plan_reviewer import apply_host_plan_review_constraints
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootKind,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.author_constraints import AuthorConstraint
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.creative_runtime import (
    AcceptedCandidateBinding,
    ActorKind,
    CandidateBinding,
    CandidateKind,
)
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
from novel_agent.domain.plan_composition import (
    build_composition_proof,
    compose_scoped_revision,
    out_of_scope_items,
    revision_scope,
)
from novel_agent.domain.planning import (
    PlanningLoopEventReceipt,
    PlanningLoopPhase,
    PlanReview,
    PlanReviewDraft,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    PlannerExecutionResult,
    PlannerProposalDraft,
    PlanProposal,
    PlanUnresolvedOperation,
    PlanUnresolvedOperationRecord,
    ProposedItem,
)
from novel_agent.domain.world import PlanLevel, PlanNode
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from tests.integration.test_yujin_d0_frozen_candidate_chain import (
    _stages_for,
    author_constraint_root,
)
from tests.unit.test_stage4_planning_contracts import _receipt

VERSION = SchemaVersion("1.0.0")
PROJECT = ProjectId("project.yujin-jiuxu.v24")
COMMIT = CommitId("sha256:87792c6bb8e9f4a311a201d00f8d520ff7df0ef3df6aad015e9d25a1964da7f6")
V24_ROOT = Path("/home/cuihengjia/agent/novel/NS/yujin-jiuxu-v24") / "objects/sha256"
V24_PARENT = V24_ROOT / "e4/e48d0871e87d4424cbebb4ed1248de3a42dfa31d70d5713216cd2dec1d92a8b8"
V24_REVIEW = V24_ROOT / "c7/c7ff9e721df0fb147109199eea5de4b7bd04bc2d6461a3715a91decdabb30fc4"
V24_REVISION_CALL = V24_ROOT / "de/de40ee6707b7beab8687255359b21fdfc56c7528121dfe96e7ee4ffd6c4a4e16"
V24_LOCKS = Path("/home/cuihengjia/agent/novel/NS/yujin-jiuxu-v24/input/planning-locks.json")

pytestmark = pytest.mark.skipif(
    not all(path.exists() for path in (V24_PARENT, V24_REVIEW, V24_REVISION_CALL, V24_LOCKS)),
    reason="the immutable v24 D0 diagnostic artifacts are not present",
)


def _proposal() -> PlanProposal:
    document = json.loads(V24_PARENT.read_text(encoding="utf-8"))
    receipt = _receipt(AgentMode.ARC_VOLUME, AgentType.PLANNER).model_copy(
        update={"base_commit": COMMIT}
    )
    return PlanProposal.model_validate(
        {**document, "project_id": PROJECT, "base_commit": COMMIT, "receipt": receipt},
        strict=False,
    )


def _mechanical_review(
    proposal: PlanProposal,
) -> tuple[PlanReview, tuple[AuthorConstraint, ...]]:
    constraints, _root, _profile_ref = author_constraint_root(V24_LOCKS)
    draft = apply_host_plan_review_constraints(
        PlanReviewDraft(
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            decision=ReviewDecision.ACCEPT,
            issues=(),
        ),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=proposal.model_dump_json(),
        mode=AgentMode.ARC_VOLUME,
        accepted_obligation_ids=frozenset(),
        accepted_obligation_windows={},
        author_constraints=constraints,
    )
    receipt = _receipt(AgentMode.ARC_VOLUME, AgentType.PLAN_REVIEWER).model_copy(
        update={"base_commit": COMMIT}
    )
    review = PlanReview(
        review_id=StableId("plan-review.d0.v24-mechanical"),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_artifact_ref=ArtifactRef(
            artifact_id=ArtifactId("sha256:" + "2" * 64),
            byte_length=1,
            media_type=PLAN_PROPOSAL_MEDIA_TYPE,
            schema_version=VERSION,
        ),
        decision=draft.decision,
        issues=draft.issues,
        revision_instruction=draft.revision_instruction,
        receipt=receipt,
    )
    return review, constraints


def _revision(proposal: PlanProposal, constraints: tuple[AuthorConstraint, ...]) -> PlanProposal:
    raw_call = json.loads(V24_REVISION_CALL.read_text(encoding="utf-8"))
    raw_response = json.loads(raw_call["raw_response_text"])
    draft = PlannerProposalDraft.model_validate(
        {"mode": AgentMode.ARC_VOLUME.value, **raw_response}, strict=False
    )
    repaired_items: list[ProposedItem] = []
    for item in draft.plan_items:
        payload = dict(item.payload)
        start = payload.get("chapter_start")
        end = payload.get("chapter_end")
        if isinstance(start, int) and isinstance(end, int):
            payload.update(_stages_for(payload, start, end, constraints))
        repaired_items.append(item.model_copy(update={"payload": payload}))

    ranges = {
        "plan-issue.draft.fda235454003445412d99d8d.0": (201, 300),
        "plan-issue.draft.fda235454003445412d99d8d.1": (350, 400),
        "plan-issue.memory-gap.0": (201, 300),
        "plan-issue.memory-gap.1": (350, 400),
    }
    unresolved = []
    operations: list[PlanUnresolvedOperationRecord] = []
    for issue in proposal.unresolved:
        start, end = ranges[issue.issue_id.root]
        updated = issue.model_copy(
            update={
                "operation": PlanUnresolvedOperation.MODIFY,
                "parent_issue_id": issue.issue_id,
                "affected_chapters": tuple(range(start, end + 1)),
            }
        )
        unresolved.append(updated)
        operations.append(
            PlanUnresolvedOperationRecord.model_validate(
                {**updated.model_dump(mode="json"), "operation": PlanUnresolvedOperation.MODIFY},
                strict=False,
            )
        )
    return proposal.model_copy(
        update={
            "proposal_id": StableId("plan-proposal.d0.v24-mechanical-revision"),
            "items": tuple(repaired_items),
            "unresolved": tuple(unresolved),
            "unresolved_operations": tuple(operations),
        }
    )


def _base_manifest(repo: ArtifactRepository) -> RootManifest:
    story = PlanNode(
        plan_node_id=StableId("plan.story.base"),
        node_type="story",
        title="Yujin Jiuxu",
        summary="The accepted story parent for the diagnostic.",
        plan_level=PlanLevel.STORY,
        chapter_start=1,
        chapter_end=800,
    )
    plan = PlanRootDocument(
        root_hash=ArtifactId("sha256:" + "0" * 64),
        schema_version=VERSION,
        nodes=(story,),
    )
    world = WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "1" * 64),
        schema_version=VERSION,
        source_commit=COMMIT,
    )
    text = TextRootDocument(
        root_hash=ArtifactId("sha256:" + "2" * 64),
        schema_version=VERSION,
        chapters=(),
    )
    plan_artifact = repo.put(
        canonical_json_bytes(plan.model_dump(mode="json")), PLAN_ROOT_MEDIA_TYPE, VERSION
    )
    world_artifact = repo.put(
        canonical_json_bytes(world.model_dump(mode="json")),
        "application/vnd.novel-agent.world-root+json",
        VERSION,
    )
    text_artifact = repo.put(
        canonical_json_bytes(text.model_dump(mode="json")),
        "application/vnd.novel-agent.text-root+json",
        VERSION,
    )
    reference_artifact = repo.put(b"{}", "application/json", VERSION)
    profile_artifact = repo.put(b"{}", "application/json", VERSION)
    return RootManifest(
        project_id=PROJECT,
        schema_version=VERSION,
        text_root=TextRootRef(**text_artifact.model_dump(), root_kind=RootKind.TEXT),
        plan_root=PlanRootRef(**plan_artifact.model_dump(), root_kind=RootKind.PLAN),
        world_root=WorldRootRef(**world_artifact.model_dump(), root_kind=RootKind.WORLD),
        reference_root=ReferenceRootRef(
            **reference_artifact.model_dump(), root_kind=RootKind.REFERENCE
        ),
        project_profile_root=ProjectProfileRootRef(
            **profile_artifact.model_dump(), root_kind=RootKind.PROJECT_PROFILE
        ),
    )


def test_v24_mechanical_failure_reaches_public_materializer(tmp_path: Path) -> None:
    proposal = _proposal()
    old_review = json.loads(V24_REVIEW.read_text(encoding="utf-8"))
    raw_call = json.loads(V24_REVISION_CALL.read_text(encoding="utf-8"))
    raw_response = json.loads(raw_call["raw_response_text"])
    assert len(old_review["issues"]) == 18
    assert any(issue["field_path"] is None for issue in old_review["issues"])
    assert len(raw_response["plan_items"]) == 8
    assert len(raw_response["unresolved"]) == 2

    mechanical, constraints = _mechanical_review(proposal)
    blocking = tuple(issue for issue in mechanical.issues if issue.blocking)
    assert mechanical.decision is ReviewDecision.REVISE
    assert len(blocking) == 18
    assert all(issue.host_issued for issue in blocking)
    assert all(issue.field_path and issue.constraint_id for issue in blocking)
    assert all(issue.actual and issue.expected for issue in blocking)
    assert len({issue.issue_id for issue in blocking}) == len(blocking)

    revised = _revision(proposal, constraints)
    scope = revision_scope(mechanical)
    composed = compose_scoped_revision(proposal, revised, scope)
    settled = apply_host_plan_review_constraints(
        PlanReviewDraft(
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            decision=ReviewDecision.ACCEPT,
            issues=(),
        ),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=composed.model_dump_json(),
        mode=AgentMode.ARC_VOLUME,
        accepted_obligation_ids=frozenset(),
        accepted_obligation_windows={},
        author_constraints=constraints,
    )
    assert settled.decision is ReviewDecision.ACCEPT
    assert settled.issues == ()
    assert out_of_scope_items(proposal, revised, scope) == ()
    assert all(
        operation.operation is PlanUnresolvedOperation.MODIFY
        for operation in composed.unresolved_operations
    )

    repo = ArtifactRepository(FilesystemObjectStore(tmp_path / "d0-v24-objects"))
    parent_ref = repo.put(
        canonical_json_bytes(proposal.model_dump(mode="json")), PLAN_PROPOSAL_MEDIA_TYPE, VERSION
    )
    mechanical = mechanical.model_copy(
        update={
            "target_artifact_ref": parent_ref,
            "receipt": mechanical.receipt.model_copy(update={"input_artifacts": (parent_ref,)}),
        }
    )
    mechanical_ref = repo.put(
        canonical_json_bytes(mechanical.model_dump(mode="json")),
        "application/vnd.novel-agent.plan-review+json",
        VERSION,
    )
    execution_receipt = proposal.receipt.model_copy(update={"output_artifacts": ()})
    revised_for_execution = revised.model_copy(update={"receipt": execution_receipt})
    composed_for_execution = composed.model_copy(update={"receipt": execution_receipt})
    raw_output_ref = repo.put(
        raw_call["raw_response_text"].encode("utf-8"),
        "application/vnd.novel-agent.model.raw-response+json",
        VERSION,
    )
    raw_execution = PlannerExecutionResult(
        mode=AgentMode.ARC_VOLUME,
        plan_proposal=revised_for_execution,
        output_artifact=raw_output_ref,
        receipt=execution_receipt,
    )
    raw_execution_ref = repo.put(
        canonical_json_bytes(raw_execution.model_dump(mode="json")),
        PLANNER_EXECUTION_MEDIA_TYPE,
        VERSION,
    )
    proof = build_composition_proof(
        parent_ref=parent_ref,
        raw_execution_ref=raw_execution_ref,
        review_ref=mechanical_ref,
        scope=scope,
        composed=composed_for_execution,
        out_of_scope=(),
    )
    proof_ref = repo.put(
        canonical_json_bytes(proof.model_dump(mode="json")),
        PLAN_COMPOSITION_MEDIA_TYPE,
        VERSION,
    )
    execution = PlannerExecutionResult(
        mode=AgentMode.ARC_VOLUME,
        plan_proposal=composed_for_execution,
        output_artifact=raw_output_ref,
        receipt=execution_receipt,
        composition_proof=proof_ref,
        raw_plan_proposal=revised_for_execution,
    )
    execution_ref = repo.put(
        canonical_json_bytes(execution.model_dump(mode="json")),
        PLANNER_EXECUTION_MEDIA_TYPE,
        VERSION,
    )
    composed_ref = repo.put(
        canonical_json_bytes(composed_for_execution.model_dump(mode="json")),
        PLAN_PROPOSAL_MEDIA_TYPE,
        VERSION,
    )
    final_review_receipt = _receipt(AgentMode.ARC_VOLUME, AgentType.PLAN_REVIEWER).model_copy(
        update={"base_commit": COMMIT, "input_artifacts": (composed_ref,)}
    )
    final_review = PlanReview(
        review_id=StableId("plan-review.d0.v24-mechanical-accept"),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_artifact_ref=composed_ref,
        decision=ReviewDecision.ACCEPT,
        issues=(),
        receipt=final_review_receipt,
    )
    final_review_ref = repo.put(
        canonical_json_bytes(final_review.model_dump(mode="json")),
        "application/vnd.novel-agent.plan-review+json",
        VERSION,
    )
    event = PlanningLoopEventReceipt(
        event_id=StableId("planning-event.d0.v24-mechanical"),
        request_id=StableId("planning-request.d0.v24-mechanical"),
        phase=PlanningLoopPhase.PLAN_REVIEWED,
        event_kind="plan.review_settled",
        artifact_refs=(
            parent_ref,
            mechanical_ref,
            raw_execution_ref,
            proof_ref,
            execution_ref,
            composed_ref,
            final_review_ref,
        ),
    )
    event_ref = repo.put(
        canonical_json_bytes(event.model_dump(mode="json")), PLANNING_EVENT_MEDIA_TYPE, VERSION
    )
    binding = CandidateBinding(
        candidate_id=StableId("candidate.d0.v24-mechanical"),
        kind=CandidateKind.PLAN,
        artifact_ref=composed_ref,
        candidate_hash=composed_ref.artifact_id.root,
        basis_commit=COMMIT,
        lineage_artifact_refs=(
            event_ref,
            final_review_ref,
            execution_ref,
            proof_ref,
            raw_execution_ref,
            mechanical_ref,
            parent_ref,
        ),
    )
    accepted = AcceptedCandidateBinding(
        acceptance_id=StableId("acceptance.d0.v24-mechanical"),
        command_id=StableId("command.d0.v24-mechanical"),
        project_id=PROJECT,
        run_id=RunId("run.d0.v24-mechanical"),
        task_id=TaskId("task.d0.v24-mechanical"),
        candidate=binding,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author.diagnostic",
        accepted_at=datetime(2026, 9, 14, tzinfo=UTC),
        expected_project_commit=COMMIT,
    )
    commits = Mock()
    commits.current_commit.return_value = COMMIT
    commits.load_manifest.return_value = _base_manifest(repo)
    bundle, report = PlanCandidateMaterializer(repo, commits, schema_version=VERSION).materialize(
        accepted
    )

    assert report.status.value == "passed"
    assert bundle.project_id == PROJECT
    assert bundle.proposed_roots.plan_root is not None
    materialized = PlanRootDocument.model_validate_json(
        repo.read_verified(bundle.proposed_roots.plan_root), strict=True
    )
    assert (
        len([node for node in materialized.nodes if node.plan_level is PlanLevel.ARC_VOLUME]) == 8
    )
