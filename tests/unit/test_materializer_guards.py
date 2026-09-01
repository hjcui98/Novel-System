"""Fail-closed coverage for trusted Stage 5 candidate materializer guards."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from novel_agent.adapters.runtime.materializers import (
    PLAN_PROPOSAL_MEDIA_TYPE,
    WRITING_LOOP_RESULT_MEDIA_TYPE,
    DraftCandidateMaterializer,
    PlanCandidateMaterializer,
)
from novel_agent.domain.artifacts import ArtifactRef, RootManifest
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
from novel_agent.domain.runtime import TaskPurpose
from novel_agent.domain.stage2 import (
    AgentType,
    ExecutionStatus,
    ProposalProvenance,
    ProposedItem,
)
from novel_agent.ports.creative_runtime import CandidateMaterializationError
from novel_agent.services.artifacts import ArtifactIntegrityError
from tests.factories import make_manifest

HASH = "sha256:" + "1" * 64
COMMIT = CommitId("sha256:" + "a" * 64)
NOW = datetime(2026, 8, 20, tzinfo=UTC)
VERSION = SchemaVersion("1.0.0")
PROJECT = ProjectId("project.test")


def _ref(digest: str = "a", *, media_type: str = "application/json") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + digest * 64),
        media_type=media_type,
        byte_length=1,
        schema_version=VERSION,
    )


def _accepted(
    *,
    kind: CandidateKind = CandidateKind.PLAN,
    basis: CommitId = COMMIT,
    expected: CommitId = COMMIT,
    project_id: ProjectId = PROJECT,
    lineage: tuple[ArtifactRef, ...] = (),
    artifact: ArtifactRef | None = None,
) -> AcceptedCandidateBinding:
    ref = artifact or _ref()
    return AcceptedCandidateBinding(
        acceptance_id=StableId("acceptance.materialize"),
        command_id=StableId("command.materialize"),
        project_id=project_id,
        run_id=RunId("run.materialize"),
        task_id=TaskId("task.materialize"),
        candidate=CandidateBinding(
            candidate_id=StableId("candidate.materialize"),
            kind=kind,
            artifact_ref=ref,
            candidate_hash=ref.artifact_id.root,
            basis_commit=basis,
            lineage_artifact_refs=lineage,
        ),
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        accepted_at=NOW,
        expected_project_commit=expected,
    )


def test_trusted_base_read_and_one_fail_closed() -> None:
    artifacts = Mock()
    commits = Mock()
    commits.current_commit.return_value = COMMIT
    commits.load_manifest.return_value = make_manifest(PROJECT)
    planner = PlanCandidateMaterializer(artifacts, commits, schema_version=VERSION)

    with pytest.raises(CandidateMaterializationError, match="wrong candidate kind"):
        planner._base(_accepted(kind=CandidateKind.DRAFT), CandidateKind.PLAN)
    with pytest.raises(CandidateMaterializationError, match="differs from acceptance"):
        planner._base(
            _accepted(basis=CommitId("sha256:" + "b" * 64), expected=COMMIT),
            CandidateKind.PLAN,
        )
    commits.current_commit.return_value = CommitId("sha256:" + "c" * 64)
    with pytest.raises(CandidateMaterializationError, match="no longer current"):
        planner._base(_accepted(), CandidateKind.PLAN)
    commits.current_commit.return_value = COMMIT
    commits.load_manifest.return_value = make_manifest(ProjectId("project.other"))
    with pytest.raises(CandidateMaterializationError, match="another project"):
        planner._base(_accepted(), CandidateKind.PLAN)

    with pytest.raises(CandidateMaterializationError, match="exactly one"):
        PlanCandidateMaterializer._one((), "application/json", label="evidence")
    with pytest.raises(CandidateMaterializationError, match="exactly one"):
        PlanCandidateMaterializer._one(
            (_ref("d"), _ref("e")),
            "application/json",
            label="evidence",
        )

    artifacts.read_verified.side_effect = ArtifactIntegrityError("tampered")
    with pytest.raises(CandidateMaterializationError, match="invalid application/json"):
        planner._read(_ref(), RootManifest)
    artifacts.read_verified.side_effect = None
    artifacts.read_verified.return_value = b"not-json"
    with pytest.raises(CandidateMaterializationError, match="invalid application/json"):
        planner._read(_ref(), RootManifest)


def test_plan_and_draft_materialize_wrap_mapping_failures() -> None:
    artifacts = Mock()
    commits = Mock()
    commits.current_commit.return_value = COMMIT
    commits.load_manifest.return_value = make_manifest(PROJECT)
    planner = PlanCandidateMaterializer(artifacts, commits, schema_version=VERSION)
    planner._materialize = Mock(side_effect=ValueError("mapping exploded"))  # type: ignore[method-assign]
    with pytest.raises(CandidateMaterializationError, match="Plan candidate mapping failed"):
        planner.materialize(_accepted())
    planner._materialize = Mock(  # type: ignore[method-assign]
        side_effect=CandidateMaterializationError("already typed")
    )
    with pytest.raises(CandidateMaterializationError, match="already typed"):
        planner.materialize(_accepted())

    draft = DraftCandidateMaterializer(artifacts, commits, schema_version=VERSION)
    draft._materialize = Mock(  # type: ignore[method-assign]
        side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
    )
    with pytest.raises(CandidateMaterializationError, match="Draft candidate mapping failed"):
        draft.materialize(_accepted(kind=CandidateKind.DRAFT))
    draft._materialize = Mock(  # type: ignore[method-assign]
        side_effect=CandidateMaterializationError("typed draft")
    )
    with pytest.raises(CandidateMaterializationError, match="typed draft"):
        draft.materialize(_accepted(kind=CandidateKind.DRAFT))


def test_plan_materialize_rejects_wrong_media_and_unpromoted_lookahead() -> None:
    artifacts = Mock()
    commits = Mock()
    commits.current_commit.return_value = COMMIT
    commits.load_manifest.return_value = make_manifest(PROJECT)
    planner = PlanCandidateMaterializer(artifacts, commits, schema_version=VERSION)
    with pytest.raises(CandidateMaterializationError, match="not a Stage 4 PlanProposal"):
        planner.materialize(_accepted(artifact=_ref(media_type="application/json")))
    lookahead = CandidateBinding(
        candidate_id=StableId("candidate.lookahead"),
        kind=CandidateKind.PLAN,
        artifact_ref=_ref("e", media_type=PLAN_PROPOSAL_MEDIA_TYPE),
        candidate_hash=_ref("e", media_type=PLAN_PROPOSAL_MEDIA_TYPE).artifact_id.root,
        basis_commit=COMMIT,
        planning_purpose=TaskPurpose.LOOKAHEAD,
        horizon_start=2,
        horizon_end=4,
        protected_chapter_index=1,
    )
    accepted = _accepted(artifact=lookahead.artifact_ref).model_copy(
        update={"candidate": lookahead}
    )
    with pytest.raises(CandidateMaterializationError, match="unpromoted lookahead"):
        planner.materialize(accepted)


def test_plan_materialize_allows_documented_unresolved_when_items_exist() -> None:
    artifacts = Mock()
    commits = Mock()
    commits.current_commit.return_value = COMMIT
    commits.load_manifest.return_value = make_manifest(PROJECT)
    planner = PlanCandidateMaterializer(artifacts, commits, schema_version=VERSION)
    proposal = Mock()
    proposal.project_id = PROJECT
    proposal.base_commit = COMMIT
    proposal.receipt.agent_type = AgentType.PLANNER
    proposal.receipt.status = ExecutionStatus.SUCCEEDED
    proposal.receipt.base_commit = COMMIT
    proposal.unresolved = ("memory gap remains",)
    proposal.items = (
        ProposedItem(
            item_id=StableId("item.ch21"),
            kind="chapter_goal",
            payload={"chapter": 21, "goal": "enter the academy", "end_state": "arrived"},
            provenance=ProposalProvenance.PLANNER_PROPOSED,
        ),
    )
    planner._read = Mock(return_value=proposal)  # type: ignore[method-assign]
    with pytest.raises(
        CandidateMaterializationError,
        match="one review bound to the accepted proposal",
    ):
        planner.materialize(_accepted(artifact=_ref("b", media_type=PLAN_PROPOSAL_MEDIA_TYPE)))


def test_draft_materialize_requires_exactly_one_writing_loop_result() -> None:
    artifacts = Mock()
    commits = Mock()
    commits.current_commit.return_value = COMMIT
    commits.load_manifest.return_value = make_manifest(PROJECT)
    draft = DraftCandidateMaterializer(artifacts, commits, schema_version=VERSION)
    with pytest.raises(CandidateMaterializationError, match="exactly one WritingLoopResult"):
        draft.materialize(_accepted(kind=CandidateKind.DRAFT))
    with pytest.raises(CandidateMaterializationError, match="exactly one WritingLoopResult"):
        draft.materialize(
            _accepted(
                kind=CandidateKind.DRAFT,
                lineage=(
                    _ref("c", media_type=WRITING_LOOP_RESULT_MEDIA_TYPE),
                    _ref("d", media_type=WRITING_LOOP_RESULT_MEDIA_TYPE),
                ),
            )
        )
