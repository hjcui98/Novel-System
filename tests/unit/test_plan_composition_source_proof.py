"""N3 (2026-09-13 guidance): scope control, composition source proof, and acceptance.

The host already composed a "scoped" revision, but the scope was advisory: any new
item survived, a named item could vanish, an item the review only mentioned
advisories about was still writable, and an item that *was* named could change
freely inside.  Worse, the composed candidate could never be committed, because the
persisted ``PlannerExecutionResult`` still held the model's raw output while the
formal materializer requires ``execution.plan_proposal == proposal``.

These tests pin the scope rules and the proof that replaces the accidental match.
The end-to-end case goes through the real ``PlanCandidateMaterializer``, not a
helper that returns the right object.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.runtime.materializers import (
    PLAN_COMPOSITION_MEDIA_TYPE,
    PLAN_PROPOSAL_MEDIA_TYPE,
    PLANNER_EXECUTION_MEDIA_TYPE,
    PLANNING_EVENT_MEDIA_TYPE,
    PlanCandidateMaterializer,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import OperatorReviewEvidence, OperatorReviewFinding
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    SchemaVersion,
    StableId,
)
from novel_agent.domain.obligation_contract import parse_obligation_declarations
from novel_agent.domain.plan_composition import (
    COMPOSITION_RULE_VERSION,
    PlanCompositionError,
    PlanCompositionProof,
    PlanRevisionOperation,
    PlanRevisionScope,
    PlanRevisionTarget,
    blocking_issue_details,
    blocking_issue_identity,
    build_composition_proof,
    compose_scoped_revision,
    issue_identity_seed,
    operator_revision_scope,
    out_of_scope_items,
    progress_against,
    proposal_digest,
    revision_scope,
    seed_identity,
    validate_composed_proposal,
    verify_composition,
)
from novel_agent.domain.planning import (
    PlanReview,
    PlanReviewIssue,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    PlannerExecutionResult,
    PlanProposal,
    PlanUnresolvedIssue,
    PlanUnresolvedOperation,
    PlanUnresolvedOperationRecord,
    ProposalProvenance,
    ProposedItem,
)
from novel_agent.ports.creative_runtime import CandidateMaterializationError
from novel_agent.services.artifacts import ArtifactRepository
from tests.unit.test_stage4_planning_contracts import _receipt

VERSION = SchemaVersion("1.0.0")
PROJECT = ProjectId("project.n3")
COMMIT = CommitId("sha256:" + "a" * 64)
HASH = ArtifactId("sha256:" + "1" * 64)
FORMAL4_OBJECTS = (
    Path(__file__).parents[2] / "tmp/yujin-evidence-preserved/formal4-v25-objects/sha256"
)


@pytest.mark.skipif(not FORMAL4_OBJECTS.exists(), reason="formal4 source objects are absent")
def test_formal4_scope_repair_preserves_parent_memory_evidence(tmp_path: Path) -> None:
    def object_bytes(digest: str) -> bytes:
        return (FORMAL4_OBJECTS / digest[:2] / digest).read_bytes()

    parent = PlanProposal.model_validate_json(
        object_bytes("9f8bd9d7c3c957e3dcb8a0697ca0b05de340edd268d2469ea537312307dbddf6")
    )
    review = OperatorReviewEvidence.model_validate_json(
        object_bytes("6d8a21dd1dfd49c56a8c58ac3e2989f47fdf2cf34981875d4e1f4597183f6c54")
    )
    revised = PlanProposal.model_validate_json(
        object_bytes("86bce418bb93b3bf7c5c1ad1393997dcbf7e72c144a26de289f505f4e8e21a57")
    )
    composed = compose_scoped_revision(parent, revised, operator_revision_scope(review))
    expected_lengths = (800, 100, 151, 800, 100, 151)
    for before, after, length in zip(
        parent.unresolved, composed.unresolved, expected_lengths, strict=True
    ):
        assert len(after.affected_chapters) == length
        assert after.affected_chapters == tuple(
            range(after.affected_chapters[0], after.affected_chapters[-1] + 1)
        )
        assert after.blocking == before.blocking
        assert after.resolution_owner == before.resolution_owner
        assert after.source_ids == before.source_ids
        assert after.source_artifact_refs == before.source_artifact_refs
        assert after.forbidden_assumptions == before.forbidden_assumptions
        assert after.summary == before.summary
    for before, after in zip(
        parent.unresolved_operations, composed.unresolved_operations, strict=True
    ):
        assert after.affected_chapters == next(
            issue.affected_chapters
            for issue in composed.unresolved
            if issue.issue_id == after.issue_id
        )
        assert after.resolution_owner == before.resolution_owner
        assert after.source_ids == before.source_ids
        assert after.source_artifact_refs == before.source_artifact_refs

    repo = _artifacts(tmp_path)
    parent_ref = repo.put(
        object_bytes("9f8bd9d7c3c957e3dcb8a0697ca0b05de340edd268d2469ea537312307dbddf6"),
        PLAN_PROPOSAL_MEDIA_TYPE,
        VERSION,
    )
    review_ref = repo.put(
        object_bytes("6d8a21dd1dfd49c56a8c58ac3e2989f47fdf2cf34981875d4e1f4597183f6c54"),
        "application/vnd.novel-agent.operator-plan-review+json",
        VERSION,
    )
    raw_execution = PlannerExecutionResult(
        mode=revised.mode,
        plan_proposal=revised,
        output_artifact=repo.put(b"{}", "application/json", VERSION),
        receipt=revised.receipt,
    )
    raw_ref = _put(repo, raw_execution, PLANNER_EXECUTION_MEDIA_TYPE)
    scope = operator_revision_scope(review)
    proof = build_composition_proof(
        parent_ref=parent_ref,
        raw_execution_ref=raw_ref,
        review_ref=review_ref,
        scope=scope,
        composed=composed,
        out_of_scope=out_of_scope_items(parent, revised, scope),
    )
    proof_ref = _put(repo, proof, PLAN_COMPOSITION_MEDIA_TYPE)
    composed_ref = _put(repo, composed, PLAN_PROPOSAL_MEDIA_TYPE)
    composed_execution = PlannerExecutionResult(
        mode=revised.mode,
        plan_proposal=composed,
        output_artifact=raw_execution.output_artifact,
        receipt=composed.receipt,
        composition_proof=proof_ref,
        raw_plan_proposal=revised,
    )
    composed_execution_ref = _put(repo, composed_execution, PLANNER_EXECUTION_MEDIA_TYPE)
    event_ref = _event_ref(repo, (composed_ref, proof_ref, composed_execution_ref))
    materialized_ref, materialized = _materializer(repo)._planner_execution((event_ref,), composed)
    assert materialized_ref == composed_execution_ref
    assert materialized.plan_proposal == composed


def _item(item_id: str, **payload: Any) -> ProposedItem:
    return ProposedItem(
        item_id=StableId(item_id),
        kind="arc_volume",
        payload={"plan_level": "arc_volume", **payload},
        provenance=ProposalProvenance.PLANNER_PROPOSED,
    )


def _proposal(
    items: tuple[ProposedItem, ...],
    *,
    number: int = 1,
    coverage: float = 1.0,
    unresolved: tuple[PlanUnresolvedIssue, ...] = (),
    unresolved_operations: tuple[PlanUnresolvedOperationRecord, ...] = (),
) -> PlanProposal:
    return PlanProposal(
        proposal_id=StableId(f"plan-proposal.n3.{number}"),
        project_id=PROJECT,
        mode=AgentMode.ARC_VOLUME,
        base_commit=COMMIT,
        items=items,
        unresolved=unresolved,
        unresolved_operations=unresolved_operations,
        coverage=coverage,
        receipt=_receipt(AgentMode.ARC_VOLUME, AgentType.PLANNER),
    )


def _review(
    *findings: PlanReviewIssue,
    target: ArtifactRef | None = None,
    decision: ReviewDecision = ReviewDecision.REVISE,
) -> PlanReview:
    return PlanReview(
        review_id=StableId("plan-review.n3"),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_artifact_ref=target
        or ArtifactRef(
            artifact_id=ArtifactId("sha256:" + "2" * 64),
            byte_length=1,
            media_type=PLAN_PROPOSAL_MEDIA_TYPE,
            schema_version=VERSION,
        ),
        decision=decision,
        issues=findings,
        revision_instruction="bounded repair" if decision is ReviewDecision.REVISE else None,
        receipt=_receipt(AgentMode.ARC_VOLUME, AgentType.PLAN_REVIEWER),
    )


def _finding(
    item_id: str,
    *,
    field_path: str | None = "midpoint_reversal",
    blocking: bool = True,
    host_issued: bool = False,
    summary: str = "field finding",
    kind: ReviewIssueKind = ReviewIssueKind.CONTRADICTION,
    authorized_operations: tuple[str, ...] = (),
) -> PlanReviewIssue:
    return PlanReviewIssue(
        issue_id=StableId(f"issue.{item_id}.{field_path or 'whole'}"),
        kind=kind,
        summary=summary,
        blocking=blocking,
        affected_item_ids=(StableId(item_id),),
        proposed_target_item_ids=(StableId(item_id),) if not host_issued else (),
        authorized_target_item_ids=(StableId(item_id),) if not host_issued else (),
        field_path=field_path,
        quote="x" if blocking else None,
        unmet_condition="y" if blocking else None,
        authorized_operations=authorized_operations,
        host_issued=host_issued,
    )


# ------------------------------------------------------------------ V06: scope control


def test_an_item_nobody_named_keeps_its_parent_bytes() -> None:
    parent = _proposal((_item("vol-1", midpoint_reversal="父值", ending_state="父末"),), number=1)
    revised = parent.model_copy(
        update={
            "items": (_item("vol-1", midpoint_reversal="子值", ending_state="子末"),),
            "proposal_id": StableId("plan-proposal.n3.2"),
        }
    )
    scope = revision_scope(_review(_finding("vol-1", field_path="midpoint_reversal")))

    composed = compose_scoped_revision(parent, revised, scope)

    assert composed.items[0].payload["midpoint_reversal"] == "子值"
    # Only the named entry moved; the sibling entry is the parent's.
    assert composed.items[0].payload["ending_state"] == "父末"


def test_nested_finding_preserves_sibling_stage_metadata() -> None:
    parent = _proposal(
        (
            _item(
                "vol-1",
                midpoint_reversal={
                    "description": "父描述",
                    "window": "101-110",
                    "role": "progression",
                    "serves": "lock.parent",
                },
            ),
        ),
        number=1,
    )
    revised = parent.model_copy(
        update={
            "items": (
                _item(
                    "vol-1",
                    midpoint_reversal={
                        "description": "子描述",
                        "window": "201-210",
                        "role": "payoff",
                        "serves": "lock.unrelated",
                    },
                ),
            ),
            "proposal_id": StableId("plan-proposal.n3.2"),
        }
    )
    scope = revision_scope(_review(_finding("vol-1", field_path="midpoint_reversal.description")))

    assert scope.target_for("vol-1").field_paths == (  # type: ignore[union-attr]
        "midpoint_reversal.description",
    )
    composed = compose_scoped_revision(parent, revised, scope)

    assert composed.items[0].payload["midpoint_reversal"] == {
        "description": "子描述",
        "window": "101-110",
        "role": "progression",
        "serves": "lock.parent",
    }


def test_obligation_contract_scope_maps_semantic_finding_to_real_surfaces() -> None:
    """A contract finding may migrate the declaration container, not a fake key."""

    parent = _proposal(
        (
            _item(
                "story.reveal.obligations",
                title="旧义务容器",
                summary="旧容器需要修订",
                obligations=[{"lock_id": "legacy", "description": "缺少 kind"}],
                unrelated="parent-only",
            ),
            _item("story.core.premise", summary="保持不变"),
        ),
        number=1,
    )
    issue = PlanReviewIssue(
        issue_id=StableId("issue.story.obligation-contract"),
        kind=ReviewIssueKind.OBLIGATION_CONTRACT,
        summary="OBLIGATION_DECLARATION_UNREADABLE: missing kind",
        blocking=True,
        affected_item_ids=(StableId("story.reveal.obligations"),),
        authorized_target_item_ids=(StableId("story.reveal.obligations"),),
        field_path="obligation_contract",
        constraint_id="host.obligation_contract",
        actual="legacy obligations",
        expected="readable obligation_declarations",
        authorized_operations=("modify",),
        host_issued=True,
    )
    scope = revision_scope(_review(issue))

    target = scope.target_for("story.reveal.obligations")
    assert target is not None
    assert "obligation_contract" not in target.field_paths
    assert "obligations" in target.field_paths
    assert "obligation_declarations" in target.field_paths

    revised = parent.model_copy(
        update={
            "proposal_id": StableId("plan-proposal.n3.2"),
            "items": (
                _item(
                    "story.reveal.obligations",
                    title="新义务容器",
                    summary="本故事按窗口推进信息揭示义务",
                    obligation_declarations=[
                        {
                            "obligation_kind": "foreshadowing",
                            "summary": "首次揭示断序星纹的来历",
                            "not_before_chapter": 350,
                        }
                    ],
                    unrelated="model-moved-but-authorized-sibling",
                ),
                _item("story.core.premise", summary="模型不应改动"),
            ),
        }
    )
    composed = compose_scoped_revision(parent, revised, scope)
    obligation_item = next(
        item for item in composed.items if item.item_id.root == "story.reveal.obligations"
    )
    assert "obligations" not in obligation_item.payload
    assert obligation_item.payload["obligation_declarations"]
    assert obligation_item.payload["unrelated"] == "parent-only"
    assert (
        next(item for item in composed.items if item.item_id.root == "story.core.premise")
        == parent.items[1]
    )
    parsed = parse_obligation_declarations(
        obligation_item.payload,
        item_kind=obligation_item.kind,
        item_id=obligation_item.item_id.root,
    )
    assert parsed.complete


def test_an_unnamed_item_is_restored_byte_for_byte() -> None:
    parent = _proposal((_item("vol-1", goal="父"), _item("vol-2", goal="父二")), number=1)
    revised = parent.model_copy(
        update={
            "items": (_item("vol-1", goal="子"), _item("vol-2", goal="子二")),
            "proposal_id": StableId("plan-proposal.n3.2"),
        }
    )
    scope = revision_scope(_review(_finding("vol-1", field_path="goal")))

    composed = compose_scoped_revision(parent, revised, scope)

    restored = next(item for item in composed.items if item.item_id.root == "vol-2")
    assert restored == parent.items[1]


def test_an_unauthorised_addition_does_not_enter_the_result() -> None:
    parent = _proposal((_item("vol-1", goal="父"),), number=1)
    revised = parent.model_copy(
        update={
            "items": (*parent.items, _item("vol-9", goal="偷偷新增")),
            "proposal_id": StableId("plan-proposal.n3.2"),
        }
    )
    scope = revision_scope(_review(_finding("vol-1", field_path="goal")))

    composed = compose_scoped_revision(parent, revised, scope)

    assert [item.item_id.root for item in composed.items] == ["vol-1"]


def test_an_unauthorised_removal_is_restored() -> None:
    parent = _proposal((_item("vol-1", goal="父一"), _item("vol-2", goal="父二")), number=1)
    revised = parent.model_copy(
        update={
            "items": (_item("vol-1", goal="子一"),),
            "proposal_id": StableId("plan-proposal.n3.2"),
        }
    )
    scope = revision_scope(_review(_finding("vol-1", field_path="goal")))

    composed = compose_scoped_revision(parent, revised, scope)

    assert [item.item_id.root for item in composed.items] == ["vol-1", "vol-2"]


def test_an_advisory_grants_no_write_permission() -> None:
    parent = _proposal((_item("vol-1", goal="父"),), number=1)
    revised = parent.model_copy(
        update={
            "items": (_item("vol-1", goal="子"),),
            "proposal_id": StableId("plan-proposal.n3.2"),
        }
    )
    scope = revision_scope(_review(_finding("vol-1", field_path="goal", blocking=False)))

    assert scope.targets == ()
    assert compose_scoped_revision(parent, revised, scope) == parent


def test_operator_review_is_a_direct_scope_source_without_a_model_receipt() -> None:
    issue_id = StableId("plan-issue.draft.operator.0")
    parent = _proposal(
        (_item("vol-1", goal="父"),),
        unresolved=(
            PlanUnresolvedIssue(
                issue_id=issue_id,
                summary="待补范围",
                affected_chapters=(1, 800),
                resolution_owner="MEMORY",
                source_ids=(StableId("source.memory.parent"),),
                forbidden_assumptions=("不得把缺口当作事实",),
            ),
        ),
        unresolved_operations=(
            PlanUnresolvedOperationRecord(
                operation=PlanUnresolvedOperation.ADD,
                issue_id=issue_id,
                summary="待补范围",
                affected_chapters=(1, 800),
                resolution_owner="MEMORY",
                source_ids=(StableId("source.memory.parent"),),
                forbidden_assumptions=("不得把缺口当作事实",),
            ),
        ),
    )
    revised = parent.model_copy(
        update={
            "proposal_id": StableId("plan-proposal.n3.operator-revised"),
            "unresolved": (
                PlanUnresolvedIssue(
                    issue_id=issue_id,
                    operation=PlanUnresolvedOperation.MODIFY,
                    parent_issue_id=issue_id,
                    summary="已补范围",
                    affected_chapters=(350, 500),
                ),
            ),
            "unresolved_operations": (
                PlanUnresolvedOperationRecord(
                    operation=PlanUnresolvedOperation.MODIFY,
                    issue_id=issue_id,
                    parent_issue_id=issue_id,
                    summary="已补范围",
                    affected_chapters=(350, 500),
                ),
            ),
        }
    )
    parent_ref = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "3" * 64),
        byte_length=1,
        media_type=PLAN_PROPOSAL_MEDIA_TYPE,
        schema_version=VERSION,
    )
    operator_review = OperatorReviewEvidence(
        review_id=StableId("operator-review.n3"),
        target_artifact_ref=parent_ref,
        reviewer_id="reviewer.codex",
        reason="范围字段缺失",
        issues=(
            OperatorReviewFinding(
                issue_id=StableId("operator-issue.n3"),
                kind="unresolved_scope_missing",
                summary="unresolved 必须声明范围",
                affected_item_ids=(issue_id,),
                field_path="affected_chapters",
                actual="[]",
                expected="350-500",
            ),
        ),
    )

    scope = operator_revision_scope(operator_review)
    assert scope.advisory_ids == (issue_id,)
    composed = compose_scoped_revision(parent, revised, scope)
    assert composed.unresolved[0].affected_chapters == tuple(range(350, 501))
    assert composed.unresolved_operations[0].affected_chapters == tuple(range(350, 501))
    for issue in (composed.unresolved[0], composed.unresolved_operations[0]):
        assert issue.summary == "待补范围"
        assert issue.resolution_owner == "MEMORY"
        assert issue.source_ids == (StableId("source.memory.parent"),)
        assert issue.forbidden_assumptions == ("不得把缺口当作事实",)

    proof = build_composition_proof(
        parent_ref=parent_ref,
        raw_execution_ref=parent_ref.model_copy(
            update={"media_type": PLANNER_EXECUTION_MEDIA_TYPE}
        ),
        review_ref=ArtifactRef(
            artifact_id=ArtifactId("sha256:" + "4" * 64),
            byte_length=1,
            media_type="application/vnd.novel-agent.operator-plan-review+json",
            schema_version=VERSION,
        ),
        scope=scope,
        composed=composed,
        out_of_scope=(),
    )
    ok, reason = verify_composition(
        proof,
        parent=parent,
        revised=revised,
        review=operator_review,
        composed=composed,
    )
    assert ok, reason


def test_a_review_with_no_finding_cannot_rewrite_the_plan() -> None:
    """The frozen 47f9a758 shape: REVISE, no issues, prose instruction."""

    parent = _proposal((_item("vol-1", goal="父"),), number=1)
    revised = parent.model_copy(
        update={
            "items": tuple(_item(f"vol-{index}", goal="整份重写") for index in range(1, 9)),
            "proposal_id": StableId("plan-proposal.n3.2"),
        }
    )
    scope = revision_scope(_review(decision=ReviewDecision.REVISE))

    assert compose_scoped_revision(parent, revised, scope) == parent


def test_an_authorised_structural_repair_still_works() -> None:
    """A missing item is a real defect; the scope must be able to say so."""

    parent = _proposal((_item("vol-1", goal="父"),), number=1)
    revised = parent.model_copy(
        update={
            "items": (*parent.items, _item("vol-2", goal="补齐的卷二")),
            "proposal_id": StableId("plan-proposal.n3.2"),
        }
    )
    scope = revision_scope(
        _review(
            _finding(
                "vol-2",
                field_path=None,
                host_issued=True,
                kind=ReviewIssueKind.COVERAGE,
                summary="VOLUME_RANGE_COVERAGE: missing volume vol-2",
            )
        )
    )

    assert PlanRevisionOperation.ADD in scope.target_for("vol-2").operations  # type: ignore[union-attr]
    composed = compose_scoped_revision(parent, revised, scope)
    assert [item.item_id.root for item in composed.items] == ["vol-1", "vol-2"]


def test_a_host_advisory_finding_never_authorises_adding_an_advisory_id() -> None:
    """A blocking advisory must not put ``plan-issue.`` ids into the authorised scope.

    Live ARC_VOLUME run ``run.yujin-jiuxu.v24.plan.arc-volume.g0`` failed with
    ``validation_rejected`` on a *correct* candidate.  The host files its advisory
    findings as ``ReviewIssueKind.UNRESOLVED_SCOPE_MISSING``, and the English text of
    that host finding ("this advisory questions chapters 1-100 but declares no
    affected_chapters") contains the word "missing".  The wording heuristic in
    ``_issue_operations`` therefore read a blocking advisory as "an item is missing",
    authorised ADD for the advisory's ``plan-issue.`` id, and
    ``validate_composed_proposal`` then demanded the composed plan contain an id that
    is not an item of the proposal.  Every retry reproduced it exactly.
    """

    advisory_ids = tuple(f"plan-issue.draft.e49b3ae95f21509138cd2233.{index}" for index in range(3))
    parent = _proposal(
        (_item("vol-1", midpoint_reversal="父值"), _item("vol-2", midpoint_reversal="父值")),
        number=1,
        unresolved=tuple(
            PlanUnresolvedIssue(
                issue_id=StableId(issue_id),
                summary=f"第 {index + 1} 卷的某个未决事实 [relation_state]",
            )
            for index, issue_id in enumerate(advisory_ids)
        ),
    )
    # The host's own finding, exactly as the live run recorded it.
    findings = tuple(
        _finding(
            issue_id,
            field_path=None,
            host_issued=True,
            kind=ReviewIssueKind.UNRESOLVED_SCOPE_MISSING,
            summary=(
                "UNRESOLVED_SCOPE_MISSING: this advisory questions chapters "
                f"{(index + 1) * 100 - 99}-{(index + 1) * 100} but declares no "
                "affected_chapters, so the uncertainty cannot be checked at the "
                "affected chapter"
            ),
        )
        for index, issue_id in enumerate(advisory_ids)
    )
    review = _review(*findings)

    scope = revision_scope(review)

    # No wording may turn an advisory id into an addable plan item.  A target entry
    # for the advisory is harmless -- a target only bounds writes *inside* an item the
    # parent already has, and `_compose_items` ignores one whose id is not a parent
    # item -- so the property that matters is precisely `additions`.
    assert scope.additions == (), scope.additions

    # The composition of correct output therefore validates instead of raising the
    # out-of-scope error the live run hit.
    composed = compose_scoped_revision(parent, parent, scope)
    assert {item.item_id.root for item in composed.items} == {"vol-1", "vol-2"}


def test_a_named_item_that_disappeared_is_restored_unless_removal_was_authorised() -> None:
    parent = _proposal((_item("vol-1", goal="父"), _item("vol-2", goal="父二")), number=1)
    revised = parent.model_copy(
        update={
            "items": (_item("vol-1", goal="子"),),
            "proposal_id": StableId("plan-proposal.n3.2"),
        }
    )
    modify_only = revision_scope(_review(_finding("vol-1", field_path="goal")))
    assert [
        item.item_id.root for item in compose_scoped_revision(parent, revised, modify_only).items
    ] == ["vol-1", "vol-2"]

    removing = revision_scope(
        _review(
            _finding(
                "vol-2",
                field_path=None,
                host_issued=True,
                summary="DUPLICATE_ITEM: vol-2 duplicates vol-1",
            )
        )
    )
    assert [
        item.item_id.root for item in compose_scoped_revision(parent, revised, removing).items
    ] == ["vol-1"]


def test_out_of_scope_items_are_reported_not_silently_fixed() -> None:
    parent = _proposal((_item("vol-1", goal="父"), _item("vol-2", goal="父二")), number=1)
    revised = parent.model_copy(
        update={
            "items": (_item("vol-1", goal="子"), _item("vol-2", goal="子二")),
            "proposal_id": StableId("plan-proposal.n3.2"),
        }
    )
    scope = revision_scope(_review(_finding("vol-1", field_path="goal")))

    assert out_of_scope_items(parent, revised, scope) == ("vol-2",)


# ------------------------------------------------ the composed proposal as a whole


def _advisory(issue_id: str, summary: str) -> PlanUnresolvedIssue:
    return PlanUnresolvedIssue(issue_id=StableId(issue_id), summary=summary)


def test_a_revised_advisory_list_cannot_change_the_composed_plan_by_itself() -> None:
    """Live ARC_VOLUME attempt 8: ``composed plan changed its unresolved issues``.

    The revision refreshed its advisories while every other unauthorised field was
    frozen to the parent, so the leak made the invariant check reject a composition
    whose volumes were correct -- and no revision that refreshes advisories could
    ever compose.  The rule for advisories is now the rule for item fields: what the
    review did not name keeps the parent's bytes.
    """

    parent = _proposal(
        (_item("vol-1", midpoint_reversal="父值"), _item("vol-2", midpoint_reversal="父值")),
        number=1,
        unresolved=(
            _advisory("plan-issue.draft.a.0", "第一卷的某个未决事实"),
            _advisory("plan-issue.draft.a.1", "第二卷的某个未决事实"),
        ),
    )
    # The revision restates one advisory, drops the other, and writes a fresh one.
    revised = _proposal(
        (_item("vol-1", midpoint_reversal="改后"), _item("vol-2", midpoint_reversal="父值")),
        number=2,
        unresolved=(
            _advisory("plan-issue.draft.a.0", "第一卷的未决事实已被重新表述"),
            _advisory("plan-issue.draft.b.0", "第三卷新提出的未决事实"),
        ),
    )
    scope = revision_scope(_review(_finding("vol-1")))

    composed = compose_scoped_revision(parent, revised, scope)

    # The named item was written; the advisory list was not, so it is the parent's.
    assert composed.unresolved == parent.unresolved
    assert composed.items[0].payload["midpoint_reversal"] == "改后"


def test_a_named_advisory_may_be_restated_and_an_unnamed_one_may_not_be_dropped() -> None:
    """Naming an advisory is the only permission to touch the advisory list."""

    parent = _proposal(
        (_item("vol-1", midpoint_reversal="父值"),),
        number=1,
        unresolved=(
            _advisory("plan-issue.draft.a.0", "第一卷的某个未决事实"),
            _advisory("plan-issue.draft.a.1", "第二卷的某个未决事实"),
        ),
    )
    revised = _proposal(
        (_item("vol-1", midpoint_reversal="父值"),),
        number=2,
        unresolved=(_advisory("plan-issue.draft.a.0", "重新表述后的未决事实"),),
    )
    # The host files its advisory finding against the advisory id it wants restated.
    scope = revision_scope(
        _review(
            _finding(
                "plan-issue.draft.a.0",
                field_path=None,
                host_issued=True,
                kind=ReviewIssueKind.BLOCKING_UNRESOLVED,
                summary="BLOCKING_UNRESOLVED[relation_state]: 该未决事项必须重新表述",
            )
        )
    )
    assert {item.root for item in scope.advisory_ids} == {"plan-issue.draft.a.0"}

    composed = compose_scoped_revision(parent, revised, scope)

    restated = {issue.issue_id.root: issue.summary for issue in composed.unresolved}
    assert restated["plan-issue.draft.a.0"] == "重新表述后的未决事实"
    # The advisory the review did not name survives even though the revision dropped it.
    assert restated["plan-issue.draft.a.1"] == "第二卷的某个未决事实"


def test_an_authorised_close_removes_active_issue_but_retains_close_history() -> None:
    issue_id = StableId("plan-issue.draft.close")
    active = PlanUnresolvedIssue(issue_id=issue_id, summary="待核验的状态")
    parent_record = PlanUnresolvedOperationRecord(
        operation=PlanUnresolvedOperation.ADD,
        issue_id=issue_id,
        summary=active.summary,
    )
    close_record = parent_record.model_copy(
        update={
            "operation": PlanUnresolvedOperation.CLOSE,
            "parent_issue_id": issue_id,
            "closure_reason": "宿主已核验并持久化来源",
        }
    )
    parent = _proposal(
        (_item("vol-1", goal="父"),),
        number=1,
        unresolved=(active,),
        unresolved_operations=(parent_record,),
    )
    revised = parent.model_copy(
        update={
            "proposal_id": StableId("plan-proposal.n3.close"),
            "unresolved": (),
            "unresolved_operations": (close_record,),
        }
    )
    scope = revision_scope(
        _review(
            _finding(
                issue_id.root,
                field_path="unresolved",
                host_issued=True,
                kind=ReviewIssueKind.BLOCKING_UNRESOLVED,
                authorized_operations=("close",),
            )
        )
    )

    composed = compose_scoped_revision(parent, revised, scope)

    assert composed.unresolved == ()
    assert len(composed.unresolved_operations) == 1
    assert composed.unresolved_operations[0].operation is PlanUnresolvedOperation.CLOSE
    assert composed.unresolved_operations[0].issue_id == issue_id
    assert composed.unresolved_operations[0].closure_reason == "宿主已核验并持久化来源"


def test_modify_is_valid_when_review_allows_modify_or_close() -> None:
    """An alternative CLOSE permission must not force every repair to close."""

    issue_id = StableId("plan-issue.draft.modify-or-close")
    active = PlanUnresolvedIssue(
        issue_id=issue_id,
        summary="待补范围",
        affected_chapters=(),
        blocking=True,
    )
    parent_record = PlanUnresolvedOperationRecord(
        operation=PlanUnresolvedOperation.ADD,
        issue_id=issue_id,
        summary=active.summary,
        blocking=True,
    )
    modified = active.model_copy(
        update={
            "operation": PlanUnresolvedOperation.MODIFY,
            "parent_issue_id": issue_id,
            "affected_chapters": (1, 800),
        }
    )
    modify_record = parent_record.model_copy(
        update={
            "operation": PlanUnresolvedOperation.MODIFY,
            "parent_issue_id": issue_id,
            "affected_chapters": (1, 800),
        }
    )
    parent = _proposal(
        (_item("vol-1", goal="父"),),
        unresolved=(active,),
        unresolved_operations=(parent_record,),
    )
    revised = parent.model_copy(
        update={
            "proposal_id": StableId("plan-proposal.n3.modify-or-close"),
            "unresolved": (modified,),
            "unresolved_operations": (modify_record,),
        }
    )
    scope = revision_scope(
        _review(
            _finding(
                issue_id.root,
                field_path="unresolved",
                host_issued=True,
                kind=ReviewIssueKind.BLOCKING_UNRESOLVED,
                authorized_operations=("modify", "close"),
            )
        )
    )

    composed = compose_scoped_revision(parent, revised, scope)

    assert composed.unresolved[0].affected_chapters == (1, 800)
    assert composed.unresolved_operations[0].operation is PlanUnresolvedOperation.MODIFY


def test_structured_unresolved_operation_cannot_name_unknown_identity() -> None:
    parent = _proposal((_item("vol-1", goal="父"),), number=1)
    issue_id = StableId("plan-issue.draft.unknown")
    active = PlanUnresolvedIssue(issue_id=issue_id, summary="模型擅自新增的未决事项")
    operation = PlanUnresolvedOperationRecord(
        operation=PlanUnresolvedOperation.ADD,
        issue_id=issue_id,
        summary=active.summary,
    )
    revised = _proposal(
        parent.items,
        number=2,
        unresolved=(active,),
        unresolved_operations=(operation,),
    )

    with pytest.raises(PlanCompositionError, match="unknown or unauthorized identity"):
        compose_scoped_revision(parent, revised, PlanRevisionScope(targets=()))


def test_validate_rejects_duplicate_items_and_metadata_drift() -> None:
    parent = _proposal((_item("vol-1", goal="父"),), number=1)
    scope = PlanRevisionScope(
        targets=(PlanRevisionTarget(item_id=StableId("vol-1")),),
    )
    duplicated = parent.model_copy(update={"items": (parent.items[0], parent.items[0])})
    with pytest.raises(PlanCompositionError, match="repeats an item id"):
        validate_composed_proposal(parent, duplicated, scope)

    empty = parent.model_copy(update={"items": ()})
    with pytest.raises(PlanCompositionError, match="no items"):
        validate_composed_proposal(parent, empty, scope)

    moved_coverage = parent.model_copy(update={"coverage": 0.5})
    with pytest.raises(PlanCompositionError, match="coverage"):
        validate_composed_proposal(parent, moved_coverage, scope)

    moved_basis = parent.model_copy(update={"base_commit": CommitId("sha256:" + "b" * 64)})
    with pytest.raises(PlanCompositionError, match="basis commit"):
        validate_composed_proposal(parent, moved_basis, scope)

    moved_project = parent.model_copy(update={"project_id": ProjectId("project.other")})
    with pytest.raises(PlanCompositionError, match="mode or project"):
        validate_composed_proposal(parent, moved_project, scope)

    extra = parent.model_copy(update={"items": (*parent.items, _item("vol-9", goal="x"))})
    with pytest.raises(PlanCompositionError, match="authorised scope"):
        validate_composed_proposal(parent, extra, scope)


def test_validate_rejects_an_unresolved_issue_edited_away() -> None:
    from novel_agent.domain.stage2 import PlanUnresolvedIssue

    advisory = PlanUnresolvedIssue(
        issue_id=StableId("plan-issue.n3"),
        summary="still uncertain",
        blocking=False,
    )
    parent = _proposal((_item("vol-1", goal="父"),), number=1, unresolved=(advisory,))
    scope = PlanRevisionScope(targets=(PlanRevisionTarget(item_id=StableId("vol-1")),))
    stripped = parent.model_copy(update={"unresolved": ()})

    with pytest.raises(PlanCompositionError, match="unresolved"):
        validate_composed_proposal(parent, stripped, scope)


# ------------------------------------------------------------------ V07: proof identity


def _artifacts(tmp_path: Path) -> ArtifactRepository:
    return ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))


def _put(repo: ArtifactRepository, model: object, media_type: str) -> ArtifactRef:
    return repo.put(
        model.model_dump_json().encode(),  # type: ignore[attr-defined]
        media_type,
        VERSION,
    )


def _composed_case(
    tmp_path: Path,
) -> tuple[
    ArtifactRepository, PlanProposal, PlanProposal, PlanReview, PlanCompositionProof, ArtifactRef
]:
    repo = _artifacts(tmp_path)
    parent = _proposal((_item("vol-1", goal="父", ending_state="父末"),), number=1)
    revised = parent.model_copy(
        update={
            "items": (_item("vol-1", goal="子", ending_state="子末"),),
            "proposal_id": StableId("plan-proposal.n3.2"),
        }
    )
    parent_ref = _put(repo, parent, PLAN_PROPOSAL_MEDIA_TYPE)
    review = _review(_finding("vol-1", field_path="goal"), target=parent_ref)
    review_ref = _put(repo, review, "application/vnd.novel-agent.plan-review+json")
    scope = revision_scope(review)
    composed = compose_scoped_revision(parent, revised, scope)
    raw_execution = PlannerExecutionResult(
        mode=AgentMode.ARC_VOLUME,
        plan_proposal=revised,
        output_artifact=repo.put(b"{}", "application/json", VERSION),
        receipt=revised.receipt,
    )
    raw_ref = _put(repo, raw_execution, PLANNER_EXECUTION_MEDIA_TYPE)
    proof = build_composition_proof(
        parent_ref=parent_ref,
        raw_execution_ref=raw_ref,
        review_ref=review_ref,
        scope=scope,
        composed=composed,
        out_of_scope=out_of_scope_items(parent, revised, scope),
    )
    proof_ref = _put(repo, proof, PLAN_COMPOSITION_MEDIA_TYPE)
    return repo, parent, revised, review, proof, proof_ref


def test_the_proof_verifies_the_composition_it_claims(tmp_path: Path) -> None:
    _repo, parent, revised, review, proof, _ref = _composed_case(tmp_path)
    composed = compose_scoped_revision(parent, revised, proof.scope)

    ok, reason = verify_composition(
        proof, parent=parent, revised=revised, review=review, composed=composed
    )

    assert ok, reason
    assert proof.composed_digest == proposal_digest(composed)
    assert proof.rule_version == COMPOSITION_RULE_VERSION
    assert proof.out_of_scope_item_ids == ()


def test_a_scope_that_does_not_match_the_review_is_refused(tmp_path: Path) -> None:
    _, parent, revised, review, proof, _ref = _composed_case(tmp_path)
    composed = compose_scoped_revision(parent, revised, proof.scope)
    # Widen the claimed scope: the review only authorised one entry.
    tampered = proof.model_copy(
        update={
            "scope": proof.scope.model_copy(
                update={"targets": (PlanRevisionTarget(item_id=StableId("vol-1")),)}
            )
        }
    )

    ok, reason = verify_composition(
        tampered, parent=parent, revised=revised, review=review, composed=composed
    )

    assert not ok
    assert "scope does not match" in reason


def test_a_digest_that_does_not_recompute_is_refused(tmp_path: Path) -> None:
    _, parent, revised, review, proof, _ref = _composed_case(tmp_path)
    composed = compose_scoped_revision(parent, revised, proof.scope)
    tampered = proof.model_copy(update={"composed_digest": "sha256:" + "9" * 64})

    ok, reason = verify_composition(
        tampered, parent=parent, revised=revised, review=review, composed=composed
    )

    assert not ok
    assert "digest" in reason


def test_a_candidate_that_is_not_the_composition_is_refused(tmp_path: Path) -> None:
    _, parent, revised, review, proof, _ref = _composed_case(tmp_path)
    other = revised.model_copy(update={"proposal_id": StableId("plan-proposal.n3.other")})

    ok, reason = verify_composition(
        proof, parent=parent, revised=revised, review=review, composed=other
    )

    assert not ok
    assert "not the composition" in reason


def test_an_unknown_composition_rule_is_refused(tmp_path: Path) -> None:
    _, parent, revised, review, proof, _ref = _composed_case(tmp_path)
    composed = compose_scoped_revision(parent, revised, proof.scope)
    # Bypass the model's own rule guard to prove the verifier checks it too.
    tampered = proof.model_copy(update={"rule_version": "scoped-revision.v99"})

    ok, reason = verify_composition(
        tampered, parent=parent, revised=revised, review=review, composed=composed
    )

    assert not ok
    assert "unsupported composition rule" in reason


def test_a_previous_composition_rule_is_not_a_current_proof(tmp_path: Path) -> None:
    _, parent, revised, review, proof, _ref = _composed_case(tmp_path)
    composed = compose_scoped_revision(parent, revised, proof.scope)
    previous = proof.model_copy(update={"rule_version": "scoped-revision.v2"})

    ok, reason = verify_composition(
        previous, parent=parent, revised=revised, review=review, composed=composed
    )

    assert not ok
    assert "unsupported composition rule" in reason


def test_the_proof_model_refuses_an_unknown_rule_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown composition rule"):
        PlanCompositionProof(
            proof_id=StableId("proof.bad"),
            rule_version="scoped-revision.v99",
            parent_proposal_ref=ArtifactRef(
                artifact_id=ArtifactId("sha256:" + "3" * 64),
                byte_length=1,
                media_type=PLAN_PROPOSAL_MEDIA_TYPE,
                schema_version=VERSION,
            ),
            raw_execution_ref=ArtifactRef(
                artifact_id=ArtifactId("sha256:" + "4" * 64),
                byte_length=1,
                media_type=PLANNER_EXECUTION_MEDIA_TYPE,
                schema_version=VERSION,
            ),
            review_ref=ArtifactRef(
                artifact_id=ArtifactId("sha256:" + "5" * 64),
                byte_length=1,
                media_type="application/vnd.novel-agent.plan-review+json",
                schema_version=VERSION,
            ),
            scope=PlanRevisionScope(targets=()),
            composed_digest="sha256:" + "6" * 64,
        )


# ------------------------------------------- V08: the real materializer accepts it


def _event_ref(repo: ArtifactRepository, nested: tuple[ArtifactRef, ...]) -> ArtifactRef:
    """A planning event whose nested artifacts are the ones under test."""

    from novel_agent.domain.planning import PlanningLoopEventReceipt, PlanningLoopPhase

    event = PlanningLoopEventReceipt(
        event_id=StableId("planning-event.n3"),
        request_id=StableId("planning-request.n3"),
        phase=PlanningLoopPhase.PLAN_REVIEWED,
        event_kind="plan.review_settled",
        artifact_refs=nested,
    )
    return repo.put(event.model_dump_json().encode(), PLANNING_EVENT_MEDIA_TYPE, VERSION)


def _materializer(repo: ArtifactRepository) -> PlanCandidateMaterializer:
    from unittest.mock import Mock

    commits = Mock()
    commits.current_commit.return_value = COMMIT
    from tests.factories import make_manifest

    commits.load_manifest.return_value = make_manifest(PROJECT)
    return PlanCandidateMaterializer(repo, commits, schema_version=VERSION)


def test_the_materializer_accepts_a_legitimately_composed_candidate(tmp_path: Path) -> None:
    """The contract this whole package exists for.

    The composed candidate is not the model's output, so nothing matches it by
    equality.  The proof is what carries it: the materializer re-derives the
    composition and finds exactly the candidate it was handed.
    """

    repo, parent, revised, _review, proof, proof_ref = _composed_case(tmp_path)
    composed = compose_scoped_revision(parent, revised, proof.scope)
    composed_ref = _put(repo, composed, PLAN_PROPOSAL_MEDIA_TYPE)
    composed_execution = PlannerExecutionResult(
        mode=AgentMode.ARC_VOLUME,
        plan_proposal=composed,
        output_artifact=repo.put(b"{}", "application/json", VERSION),
        receipt=composed.receipt,
        composition_proof=proof_ref,
        raw_plan_proposal=revised,
    )
    composed_execution_ref = _put(repo, composed_execution, PLANNER_EXECUTION_MEDIA_TYPE)
    event_ref = _event_ref(repo, (composed_ref, proof_ref, composed_execution_ref))

    ref, execution = _materializer(repo)._planner_execution((event_ref,), composed)

    assert ref == composed_execution_ref
    assert execution.plan_proposal == composed


def test_the_materializer_refuses_a_missing_proof(tmp_path: Path) -> None:
    repo, parent, revised, _review, proof, _proof_ref = _composed_case(tmp_path)
    composed = compose_scoped_revision(parent, revised, proof.scope)
    composed_ref = _put(repo, composed, PLAN_PROPOSAL_MEDIA_TYPE)
    # A composed candidate whose proof reference points at nothing.
    dangling = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "7" * 64),
        byte_length=1,
        media_type=PLAN_COMPOSITION_MEDIA_TYPE,
        schema_version=VERSION,
    )
    execution = PlannerExecutionResult(
        mode=AgentMode.ARC_VOLUME,
        plan_proposal=composed,
        output_artifact=repo.put(b"{}", "application/json", VERSION),
        receipt=composed.receipt,
        composition_proof=dangling,
        raw_plan_proposal=revised,
    )
    execution_ref = _put(repo, execution, PLANNER_EXECUTION_MEDIA_TYPE)
    event_ref = _event_ref(repo, (composed_ref, dangling, execution_ref))

    with pytest.raises(CandidateMaterializationError, match="invalid"):
        _materializer(repo)._planner_execution((event_ref,), composed)


def test_the_materializer_refuses_a_tampered_proof(tmp_path: Path) -> None:
    repo, parent, revised, _review, proof, _ref = _composed_case(tmp_path)
    composed = compose_scoped_revision(parent, revised, proof.scope)
    composed_ref = _put(repo, composed, PLAN_PROPOSAL_MEDIA_TYPE)
    # The proof claims a wider scope than the review's findings authorise.
    tampered = proof.model_copy(
        update={
            "scope": proof.scope.model_copy(
                update={"targets": (PlanRevisionTarget(item_id=StableId("vol-1")),)}
            )
        }
    )
    tampered_ref = _put(repo, tampered, PLAN_COMPOSITION_MEDIA_TYPE)
    execution = PlannerExecutionResult(
        mode=AgentMode.ARC_VOLUME,
        plan_proposal=composed,
        output_artifact=repo.put(b"{}", "application/json", VERSION),
        receipt=composed.receipt,
        composition_proof=tampered_ref,
        raw_plan_proposal=revised,
    )
    execution_ref = _put(repo, execution, PLANNER_EXECUTION_MEDIA_TYPE)
    event_ref = _event_ref(repo, (composed_ref, tampered_ref, execution_ref))

    with pytest.raises(CandidateMaterializationError, match="composition proof rejected"):
        _materializer(repo)._planner_execution((event_ref,), composed)


def test_the_materializer_refuses_two_competing_compositions(tmp_path: Path) -> None:
    repo, parent, revised, _review, proof, proof_ref = _composed_case(tmp_path)
    composed = compose_scoped_revision(parent, revised, proof.scope)
    composed_ref = _put(repo, composed, PLAN_PROPOSAL_MEDIA_TYPE)
    execution = PlannerExecutionResult(
        mode=AgentMode.ARC_VOLUME,
        plan_proposal=composed,
        output_artifact=repo.put(b"{}", "application/json", VERSION),
        receipt=composed.receipt,
        composition_proof=proof_ref,
        raw_plan_proposal=revised,
    )
    first = _put(repo, execution, PLANNER_EXECUTION_MEDIA_TYPE)
    second = repo.put(
        json.dumps(
            {
                **json.loads(execution.model_dump_json()),
                "output_artifact": execution.output_artifact.model_dump(mode="json"),
            }
        ).encode(),
        PLANNER_EXECUTION_MEDIA_TYPE,
        VERSION,
    )
    event_ref = _event_ref(repo, (composed_ref, proof_ref, first, second))

    with pytest.raises(CandidateMaterializationError, match="one matching Planner execution"):
        _materializer(repo)._planner_execution((event_ref,), composed)


def test_a_direct_execution_still_wins_over_a_composed_one(tmp_path: Path) -> None:
    """The original direct match is not deleted to make room for composition."""

    repo, parent, revised, _review, proof, proof_ref = _composed_case(tmp_path)
    composed = compose_scoped_revision(parent, revised, proof.scope)
    composed_ref = _put(repo, composed, PLAN_PROPOSAL_MEDIA_TYPE)
    direct = PlannerExecutionResult(
        mode=AgentMode.ARC_VOLUME,
        plan_proposal=composed,
        output_artifact=repo.put(b"{}", "application/json", VERSION),
        receipt=composed.receipt,
    )
    direct_ref = _put(repo, direct, PLANNER_EXECUTION_MEDIA_TYPE)
    composed_execution = PlannerExecutionResult(
        mode=AgentMode.ARC_VOLUME,
        plan_proposal=composed,
        output_artifact=repo.put(b"{}", "application/json", VERSION),
        receipt=composed.receipt,
        composition_proof=proof_ref,
        raw_plan_proposal=revised,
    )
    composed_execution_ref = _put(repo, composed_execution, PLANNER_EXECUTION_MEDIA_TYPE)
    event_ref = _event_ref(repo, (composed_ref, direct_ref, proof_ref, composed_execution_ref))

    ref, _execution = _materializer(repo)._planner_execution((event_ref,), composed)

    assert ref == direct_ref


def test_a_composed_result_must_carry_both_proof_and_raw_output() -> None:
    """Half a proof is not a proof; the model refuses to construct it."""

    receipt = _receipt(AgentMode.ARC_VOLUME, AgentType.PLANNER)
    proposal = _proposal((_item("vol-1", goal="x"),), number=1)
    with pytest.raises(ValueError, match="both the composition proof and the raw proposal"):
        PlannerExecutionResult(
            mode=AgentMode.ARC_VOLUME,
            plan_proposal=proposal,
            output_artifact=ArtifactRef(
                artifact_id=ArtifactId("sha256:" + "8" * 64),
                byte_length=1,
                media_type="application/json",
                schema_version=VERSION,
            ),
            receipt=receipt,
            composition_proof=ArtifactRef(
                artifact_id=ArtifactId("sha256:" + "9" * 64),
                byte_length=1,
                media_type=PLAN_COMPOSITION_MEDIA_TYPE,
                schema_version=VERSION,
            ),
        )


def test_a_direct_result_carries_neither() -> None:
    proposal = _proposal((_item("vol-1", goal="x"),), number=1)
    result = PlannerExecutionResult(
        mode=AgentMode.ARC_VOLUME,
        plan_proposal=proposal,
        output_artifact=ArtifactRef(
            artifact_id=ArtifactId("sha256:" + "8" * 64),
            byte_length=1,
            media_type="application/json",
            schema_version=VERSION,
        ),
        receipt=proposal.receipt,
    )

    assert result.composition_proof is None
    assert result.raw_plan_proposal is None


def test_the_proof_records_the_raw_output_identity_not_a_restatement() -> None:
    """The raw model output keeps its own identity; the composed candidate gets its own."""

    parent = _proposal((_item("vol-1", goal="父", ending_state="父末"),), number=1)
    revised = parent.model_copy(
        update={
            "items": (_item("vol-1", goal="子", ending_state="子末"),),
            "proposal_id": StableId("plan-proposal.n3.2"),
        }
    )
    scope = revision_scope(_review(_finding("vol-1", field_path="goal")))
    composed = compose_scoped_revision(parent, revised, scope)

    assert composed.proposal_id == revised.proposal_id
    # The composed candidate is neither document: the named entry came from the
    # revision, the entry it did not name came from the parent.
    assert proposal_digest(composed) != proposal_digest(revised)
    assert proposal_digest(composed) != proposal_digest(parent)
    assert composed.items[0].payload["goal"] == "子"
    assert composed.items[0].payload["ending_state"] == "父末"


# ------------------------------------------------- V09: progress identity across slices


def _issues(*specs: tuple[ReviewIssueKind, str, str | None, str]) -> tuple[PlanReviewIssue, ...]:
    """Build findings from ``(kind, item, field_path, unmet_condition)`` specs."""

    return tuple(
        PlanReviewIssue(
            issue_id=StableId(f"issue.{index}"),
            kind=kind,
            summary=condition,
            blocking=True,
            affected_item_ids=(StableId(item),),
            field_path=field_path,
            quote="x",
            unmet_condition=condition,
            host_issued=True,
        )
        for index, (kind, item, field_path, condition) in enumerate(specs)
    )


def _seed(*specs: tuple[ReviewIssueKind, str, str | None, str]) -> tuple[str, ...]:
    return issue_identity_seed(_review(*_issues(*specs)), ())


def _progress(seed: tuple[str, ...], *specs: tuple[ReviewIssueKind, str, str | None, str]) -> bool:
    return progress_against(seed, _review(*_issues(*specs)))


_WINDOW = ReviewIssueKind.VOLUME_STAGE_WINDOW_VIOLATION
_COVERAGE = ReviewIssueKind.COVERAGE


def test_a_reworded_finding_is_not_progress() -> None:
    """The same problem in the same place is the same problem."""

    seed = _seed((_WINDOW, "vol-4", "midpoint_reversal.window", "越过 350"))

    assert not _progress(seed, (_WINDOW, "vol-4", "midpoint_reversal.window", "与受信边界不符"))


def test_a_partially_repaired_finding_set_is_progress() -> None:
    """One of two problems closed: the remaining set is smaller."""

    seed = _seed(
        (_WINDOW, "vol-4", "midpoint_reversal.window", "越过 350"),
        (_COVERAGE, "vol-4", None, "缺少必要槽位"),
    )

    assert _progress(seed, (_COVERAGE, "vol-4", None, "缺少必要槽位"))


def test_a_problem_that_moved_is_progress() -> None:
    """The outstanding problem is now somewhere else, which is a change to fix."""

    seed = _seed((_WINDOW, "vol-4", "midpoint_reversal.window", "越过 350"))

    assert _progress(seed, (_WINDOW, "vol-5", "volume_climax.window", "越过 401"))


def test_a_narrowed_condition_on_the_same_problem_is_progress() -> None:
    """The same identity with a strictly smaller condition still counts."""

    seed = _seed((_WINDOW, "vol-4", "midpoint_reversal.window", "越过 350 和 401 两处边界"))

    assert _progress(seed, (_WINDOW, "vol-4", "midpoint_reversal.window", "越过 350"))


def test_a_fresh_problem_is_progress_and_a_restatement_is_not() -> None:
    seed = _seed((_WINDOW, "vol-4", "midpoint_reversal.window", "越过 350"))

    assert _progress(seed, (_WINDOW, "vol-5", "volume_climax.window", "越过 401"))
    assert not _progress(seed, (_WINDOW, "vol-4", "midpoint_reversal.window", "越过 350"))


def test_an_unknown_field_produces_a_distinct_identity() -> None:
    """A finding with no field path is about the item, not about one of its fields."""

    seed = _seed((_COVERAGE, "vol-4", "midpoint_reversal.window", "越过 350"))

    assert _progress(seed, (_COVERAGE, "vol-4", None, "缺少必要槽位"))


def test_an_empty_frontier_reports_no_progress() -> None:
    """Nothing recorded means nothing to compare against, not progress."""

    assert not progress_against((), _review(_finding("vol-4")))


def test_a_problem_already_attempted_is_not_progress_again() -> None:
    """Oscillation needs history: two states can trade places for ever otherwise."""

    moved = _seed((_WINDOW, "vol-5", "volume_climax.window", "越过 401"))
    # The run has already spent a revision on vol-4's window.
    history = _seed((_WINDOW, "vol-4", "midpoint_reversal.window", "越过 350"))
    with_history = history + tuple(f"attempted:{entry}" for entry in seed_identity(history))

    # Moving back to the first problem is not progress: it was already attempted.
    assert not progress_against(
        with_history, _review(*_issues((_WINDOW, "vol-4", "midpoint_reversal.window", "越过 350")))
    )
    # Moving to a problem this run has not tried is progress.
    assert progress_against(
        with_history, _review(*_issues((_WINDOW, "vol-6", "volume_climax.window", "越过 501")))
    )
    assert seed_identity(moved) == frozenset(
        {"volume_stage_window_violation|vol-5|volume_climax.window|"}
    )


def test_identity_ignores_the_wording_and_keeps_the_field() -> None:
    first = _review(
        PlanReviewIssue(
            issue_id=StableId("a"),
            kind=_WINDOW,
            summary="wording one",
            blocking=True,
            affected_item_ids=(StableId("vol-4"),),
            proposed_target_item_ids=(StableId("vol-4"),),
            authorized_target_item_ids=(StableId("vol-4"),),
            field_path="midpoint_reversal.window",
            quote="x",
            unmet_condition="condition one",
        )
    )
    second = _review(
        PlanReviewIssue(
            issue_id=StableId("b"),
            kind=_WINDOW,
            summary="completely different wording",
            blocking=True,
            affected_item_ids=(StableId("vol-4"),),
            proposed_target_item_ids=(StableId("vol-4"),),
            authorized_target_item_ids=(StableId("vol-4"),),
            field_path="midpoint_reversal.window",
            quote="y",
            unmet_condition="condition two",
        )
    )

    assert blocking_issue_identity(first) == blocking_issue_identity(second)
    assert blocking_issue_details(first) != blocking_issue_details(second)


def test_identity_separates_an_item_finding_from_a_field_finding() -> None:
    field_level = _review(_finding("vol-4", field_path="midpoint_reversal"))
    item_level = _review(_finding("vol-4", field_path=None, host_issued=True))

    assert blocking_issue_identity(field_level) != blocking_issue_identity(item_level)


def test_an_advisory_never_enters_the_frontier() -> None:
    review = _review(_finding("vol-4", field_path="goal", blocking=False))

    assert blocking_issue_identity(review) == ()
    assert blocking_issue_details(review) == ()
    assert issue_identity_seed(review, ()) == ()
