"""R1 regression: legacy responsibility tables must compile, free text must not.

The frozen v6 run exposed two contract gaps:

* an eight-volume ``obligation_plan`` responsibility table produced **zero**
  World obligations because the binder never looked at that key, and
* chapter ``obligation_actions`` written as natural-language strings were
  accepted by host review and only failed later at commit.

The shapes used here are the real frozen v6 ones.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from novel_agent.adapters.runtime.materializers import (
    CandidateMaterializationError,
    PlanCandidateMaterializer,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import ObligationKind, ObligationStatus, WorldRootDocument
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    ContractRef,
    ExecutionStatus,
    PlanProposal,
    ProposalProvenance,
    ProposedItem,
)

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "1" * 64)
COMMIT = CommitId("sha256:" + "2" * 64)

# Real frozen v6 ARC_VOLUME responsibility entries (vol_01 / vol_02).
_VOLUME_ONE = (
    {
        "kind": "objective",
        "not_before_chapter": 1,
        "payoff_window": "90-100",
        "progress_windows": ["51-80"],
        "setup_window": "1-50",
        "summary": "陆沉舟获得铜铭",
    },
    {
        "kind": "foreshadowing",
        "not_before_chapter": 1,
        "payoff_window": "70-100",
        "progress_windows": ["31-60"],
        "setup_window": "1-30",
        "summary": "确认残星纹为断序星纹",
    },
)
_VOLUME_TWO = (
    {
        "kind": "objective",
        "not_before_chapter": 101,
        "payoff_window": "160-200",
        "progress_windows": ["121-150"],
        "setup_window": "101-120",
        "summary": "陆沉舟进入内府",
    },
    {
        "kind": "objective",
        "not_before_chapter": 101,
        "payoff_window": "190-200",
        "progress_windows": ["151-180"],
        "setup_window": "101-150",
        "summary": "陆沉舟晋升银铭",
    },
)
# Real frozen v6 CHAPTER_SET action string.
_CHAPTER_ACTION = "setup: 确认残星纹为断序星纹 (setup_window: 1-30)"


def _world() -> WorldRootDocument:
    return WorldRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        source_commit=COMMIT,
        entities=(),
        events=(),
        states=(),
        relations=(),
        obligations=(),
    )


def _receipt(mode: AgentMode) -> AgentExecutionReceipt:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return AgentExecutionReceipt(
        receipt_id=StableId(f"receipt.planner.{mode.value}"),
        run_id=RunId("run.yujin-jiuxu.v7"),
        task_id=TaskId("task.plan"),
        agent_spec=ContractRef(
            contract_id=StableId(f"agent.planner.{mode.value}"),
            version=VERSION,
            content_hash=HASH,
        ),
        agent_type=AgentType.PLANNER,
        agent_mode=mode,
        prompt_fingerprint=HASH,
        configuration_fingerprint=HASH,
        base_commit=COMMIT,
        status=ExecutionStatus.SUCCEEDED,
        started_at=now,
        completed_at=now,
        latency_ms=0,
    )


def _proposal(items: tuple[ProposedItem, ...]) -> PlanProposal:
    return PlanProposal(
        proposal_id=StableId("proposal.arc"),
        project_id=ProjectId("project.yujin-jiuxu.v7"),
        mode=AgentMode.ARC_VOLUME,
        base_commit=COMMIT,
        items=items,
        coverage=1.0,
        receipt=_receipt(AgentMode.ARC_VOLUME),
    )


def _item(item_id: str, payload: dict[str, object]) -> ProposedItem:
    return ProposedItem(
        item_id=StableId(item_id),
        kind="arc_volume",
        payload=payload,
        provenance=ProposalProvenance.PLANNER_PROPOSED,
    )


def _materializer() -> PlanCandidateMaterializer:
    artifacts = Mock()
    artifacts.put.return_value = ArtifactRef(
        artifact_id=HASH,
        media_type="application/vnd.novel-agent.world-root+json",
        byte_length=10,
        schema_version=VERSION,
    )
    return PlanCandidateMaterializer(artifacts, Mock(), schema_version=VERSION)


def test_a_declaration_keeps_its_owner_window_and_deadline() -> None:
    """Every legal field a declaration states must reach the World root.

    The shared parser kept only kind/description/not_before, so a candidate that
    named an owner, a target window and a deadline materialized with all three
    empty - a silent field loss that downstream coverage and readiness read as
    "no scope".
    """

    planner = _materializer()
    proposal = _proposal(
        (
            _item(
                "vol_02",
                {
                    "plan_level": "arc_volume",
                    "obligation_declarations": [
                        {
                            "kind": "objective",
                            "description": "内府资格推进",
                            "owner_ids": ["entity.bootstrap.1"],
                            "target_chapter_start": 201,
                            "target_chapter_end": 250,
                            "due_chapter": 260,
                        }
                    ],
                },
            ),
        )
    )

    world, _ref, _bindings = planner._bind_obligation_declarations(_world(), proposal)

    assert len(world.obligations) == 1
    obligation = world.obligations[0]
    assert obligation.owner_ids == (StableId("entity.bootstrap.1"),)
    assert (obligation.target_chapter_start, obligation.target_chapter_end) == (201, 250)
    assert obligation.due_chapter == 260


def test_a_declaration_that_resolves_or_renames_itself_is_refused() -> None:
    """A wrong id or a self-resolved status must be refused, not ignored."""

    planner = _materializer()
    resolved = _proposal(
        (
            _item(
                "vol_02",
                {
                    "plan_level": "arc_volume",
                    "obligation_declarations": [
                        {
                            "kind": "objective",
                            "description": "内府资格推进",
                            "status": "resolved",
                        }
                    ],
                },
            ),
        )
    )
    with pytest.raises(CandidateMaterializationError, match="cannot resolve or abandon"):
        planner._bind_obligation_declarations(_world(), resolved)

    wrong_id = _proposal(
        (
            _item(
                "vol_02",
                {
                    "plan_level": "arc_volume",
                    "obligation_declarations": [
                        {
                            "kind": "objective",
                            "description": "内府资格推进",
                            "obligation_id": "obligation.someone.else.0.objective",
                        }
                    ],
                },
            ),
        )
    )
    with pytest.raises(CandidateMaterializationError, match="does not match host-derived identity"):
        planner._bind_obligation_declarations(_world(), wrong_id)

    malformed_window = _proposal(
        (
            _item(
                "vol_02",
                {
                    "plan_level": "arc_volume",
                    "obligation_declarations": [
                        {
                            "kind": "objective",
                            "description": "内府资格推进",
                            "target_chapter_start": "201",
                        }
                    ],
                },
            ),
        )
    )
    with pytest.raises(CandidateMaterializationError, match="positive chapter number"):
        planner._bind_obligation_declarations(_world(), malformed_window)


def test_current_and_legacy_forms_on_one_item_keep_distinct_identities() -> None:
    """A responsibility table must never be appended twice under different ordinals."""

    planner = _materializer()
    proposal = _proposal(
        (
            _item(
                "vol_01",
                {
                    "plan_level": "arc_volume",
                    "obligation_plan": list(_VOLUME_ONE),
                    "obligation_declarations": [
                        {"kind": "promise", "summary": "铜铭另有来历", "not_before_chapter": 1}
                    ],
                },
            ),
        )
    )

    world, _ref, bindings = planner._bind_obligation_declarations(_world(), proposal)

    descriptions = [obligation.description for obligation in world.obligations]
    assert descriptions == ["铜铭另有来历", "陆沉舟获得铜铭", "确认残星纹为断序星纹"]
    ids = [obligation.obligation_id for obligation in world.obligations]
    assert len(ids) == len(set(ids)) == 3
    assert len(bindings[StableId("vol_01")]) == 3


def test_legacy_volume_responsibility_table_now_declares_world_obligations() -> None:
    planner = _materializer()
    proposal = _proposal(
        (
            _item(
                "vol_01",
                {
                    "plan_level": "arc_volume",
                    "chapter_start": 1,
                    "chapter_end": 100,
                    "obligation_plan": list(_VOLUME_ONE),
                },
            ),
            _item(
                "vol_02",
                {
                    "plan_level": "arc_volume",
                    "chapter_start": 101,
                    "chapter_end": 200,
                    "obligation_plan": list(_VOLUME_TWO),
                },
            ),
        )
    )

    world, _ref, bindings = planner._bind_obligation_declarations(_world(), proposal)

    assert len(world.obligations) == 4
    assert len(bindings[StableId("vol_01")]) == 2
    assert len(bindings[StableId("vol_02")]) == 2
    descriptions = {obligation.description for obligation in world.obligations}
    assert descriptions == {
        "陆沉舟获得铜铭",
        "确认残星纹为断序星纹",
        "陆沉舟进入内府",
        "陆沉舟晋升银铭",
    }
    kinds = {obligation.kind for obligation in world.obligations}
    assert kinds == {ObligationKind.OBJECTIVE, ObligationKind.FORESHADOWING}
    assert {obligation.status for obligation in world.obligations} == {ObligationStatus.OPEN}
    assert {obligation.not_before_chapter for obligation in world.obligations} == {1, 101}
    assert all(obligation.obligation_id.root for obligation in world.obligations)


def test_legacy_responsibility_windows_do_not_collapse_into_due_chapters() -> None:
    """The setup/payoff distinction survives: setup alone must stay injectable."""

    planner = _materializer()
    proposal = _proposal(
        (
            _item(
                "vol_01",
                {"plan_level": "arc_volume", "obligation_plan": [dict(_VOLUME_ONE[1])]},
            ),
        )
    )

    world, _ref, _bindings = planner._bind_obligation_declarations(_world(), proposal)

    obligation = world.obligations[0]
    assert obligation.kind is ObligationKind.FORESHADOWING
    assert obligation.not_before_chapter == 1
    assert obligation.due_chapter is None
    assert obligation.target_chapter_start is None


def test_unreadable_legacy_responsibility_entry_fails_closed() -> None:
    planner = _materializer()
    proposal = _proposal(
        (
            _item(
                "vol_01",
                {
                    "plan_level": "arc_volume",
                    "obligation_plan": [
                        dict(_VOLUME_ONE[0]),
                        {"kind": "objective", "summary": "缺窗口", "setup_window": "1-5"},
                    ],
                },
            ),
        )
    )

    with pytest.raises(CandidateMaterializationError) as error:
        planner._bind_obligation_declarations(_world(), proposal)

    detail = str(error.value)
    assert "obligation declaration is not readable" in detail
    assert "no observable completion boundary" in detail


def test_legacy_free_text_chapter_action_is_rejected_at_the_binder() -> None:
    planner = _materializer()
    proposal = _proposal(
        (
            _item(
                "plan-item.ch1",
                {
                    "chapter_index": 1,
                    "obligation_actions": [_CHAPTER_ACTION],
                },
            ),
        )
    )

    with pytest.raises(CandidateMaterializationError) as error:
        planner._bind_obligation_declarations(_world(), proposal)

    detail = str(error.value)
    assert "chapter obligation actions are not readable" in detail
    assert "free-text string" in detail


def test_chapter_action_referencing_an_undeclared_obligation_is_rejected() -> None:
    planner = _materializer()
    proposal = _proposal(
        (
            _item(
                "plan-item.ch1",
                {
                    "chapter_index": 1,
                    "obligation_actions": [
                        {
                            "obligation_id": "obligation.absent",
                            "action": "SETUP",
                            "expected_delta": "推进",
                        }
                    ],
                },
            ),
        )
    )

    with pytest.raises(CandidateMaterializationError, match="undeclared obligation"):
        planner._bind_obligation_declarations(_world(), proposal)


def test_valid_structured_action_binds_after_the_declaration_is_compiled() -> None:
    planner = _materializer()
    proposal = _proposal(
        (
            _item(
                "vol_01",
                {"plan_level": "arc_volume", "obligation_plan": [dict(_VOLUME_ONE[0])]},
            ),
        )
    )
    world, _ref, bindings = planner._bind_obligation_declarations(_world(), proposal)
    declared_id = bindings[StableId("vol_01")][0]

    chapter_proposal = _proposal(
        (
            _item(
                "plan-item.ch1",
                {
                    "chapter_index": 1,
                    "obligation_actions": [
                        {
                            "obligation_id": declared_id.root,
                            "action": "SETUP",
                            "expected_delta": "铜铭首次出现但不解释其来历",
                        }
                    ],
                },
            ),
        )
    )
    _world_after, _ref_after, chapter_bindings = planner._bind_obligation_declarations(
        world, chapter_proposal
    )

    assert chapter_bindings[StableId("plan-item.ch1")] == (declared_id,)


def test_chapter_set_may_not_declare_new_durable_obligations() -> None:
    planner = _materializer()
    proposal = PlanProposal(
        proposal_id=StableId("proposal.chapter-set"),
        project_id=ProjectId("project.yujin-jiuxu.v7"),
        mode=AgentMode.CHAPTER_SET,
        base_commit=COMMIT,
        items=(
            _item(
                "plan-item.ch1",
                {"chapter_index": 1, "obligation_plan": [dict(_VOLUME_ONE[0])]},
            ),
        ),
        coverage=1.0,
        receipt=_receipt(AgentMode.ARC_VOLUME),
    )

    with pytest.raises(CandidateMaterializationError, match="may not declare new obligations"):
        planner._bind_obligation_declarations(_world(), proposal)


def test_review_and_materialization_agree_on_a_real_declaration_payload() -> None:
    """One candidate payload: host review accepts it, the binder keeps every field.

    Review and materialization each grew their own reader once, so a declaration
    could pass review and be refused at commit (or lose its owner/window/deadline).
    This walks a real volume payload through both surfaces and reads the committed
    obligation back.
    """

    from novel_agent.agents.plan_reviewer import apply_host_plan_review_constraints
    from novel_agent.domain.planning import (
        PlanReviewDraft,
        ReviewDecision,
        ReviewIssueKind,
        ReviewTargetKind,
    )
    from novel_agent.domain.stage2 import AgentMode

    payload = {
        "items": [
            {
                "item_id": "vol_04",
                "kind": "arc_volume",
                "payload": {
                    "obligation_declarations": [
                        {
                            "kind": "foreshadowing",
                            "description": "长程真相首次暗示",
                            "owner_ids": ["entity.bootstrap.1"],
                            "not_before_chapter": 350,
                            "target_chapter_start": 351,
                            "target_chapter_end": 400,
                            "due_chapter": 400,
                        }
                    ]
                },
            }
        ],
        "unresolved": [],
        "coverage": 1.0,
    }
    draft = PlanReviewDraft(
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        decision=ReviewDecision.ACCEPT,
        issues=(),
    )

    reviewed = apply_host_plan_review_constraints(
        draft,
        mode=AgentMode.ARC_VOLUME,
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=json.dumps(payload, ensure_ascii=False),
        expected_volume_count=None,
        expected_target_chapters=None,
        accepted_obligation_ids=frozenset(),
        author_constraints=(),
    )
    # This payload is deliberately a minimal volume: what matters here is that the
    # review reads its declaration exactly as the binder will.  Any obligation-contract
    # or declaration-identity issue would mean the two surfaces disagree again.
    obligation_issues = [
        issue
        for issue in reviewed.issues
        if issue.kind
        in {
            ReviewIssueKind.OBLIGATION_CONTRACT,
            ReviewIssueKind.OBLIGATION,
            ReviewIssueKind.PROVENANCE,
        }
    ]
    assert obligation_issues == [], [issue.summary for issue in obligation_issues]

    planner = _materializer()
    proposal = _proposal(
        (
            _item(
                "vol_04",
                {
                    "plan_level": "arc_volume",
                    **payload["items"][0]["payload"],
                },
            ),
        )
    )
    world, ref, bindings = planner._bind_obligation_declarations(_world(), proposal)

    assert ref is not None
    assert len(world.obligations) == 1
    obligation = world.obligations[0]
    assert obligation.kind is ObligationKind.FORESHADOWING
    assert obligation.owner_ids == (StableId("entity.bootstrap.1"),)
    assert obligation.not_before_chapter == 350
    assert (obligation.target_chapter_start, obligation.target_chapter_end) == (351, 400)
    assert obligation.due_chapter == 400
    assert bindings[StableId("vol_04")] == (obligation.obligation_id,)
