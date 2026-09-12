"""R2.3: four independent coverage ratios instead of one opaque float.

A single ``coverage=1.0`` cannot say what was covered.  Each ratio here has a trusted
denominator and a missing-item list, and the arc-volume case proves that a level that
does not own individual chapters must not report a real-looking zero.
"""

from __future__ import annotations

from novel_agent.domain.author_constraints import (
    AuthorConstraint,
    AuthorConstraintCategory,
)
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.planning_coverage import (
    PlanningCoverageKind,
    compile_planning_coverage_report,
)

HASH = ArtifactId("sha256:" + "1" * 64)
VERSION = SchemaVersion("1.0.0")

# The exact frozen v6 ARC_VOLUME responsibilities.
_VOLUME_ONE = [
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
]


def _constraint(text: str, constraint_id: str = "author-constraint.language.0") -> AuthorConstraint:
    return AuthorConstraint(
        constraint_id=StableId(constraint_id),
        category=AuthorConstraintCategory.LANGUAGE,
        text=text,
        source_ref={
            "artifact_id": HASH,
            "media_type": "text/plain",
            "byte_length": 10,
            "schema_version": VERSION,
        },
        source_hash=HASH,
    )


def _volume_item(item_id: str = "vol_01", **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "plan_level": "arc_volume",
        "chapter_start": 1,
        "chapter_end": 100,
        "obligation_plan": [dict(entry) for entry in _VOLUME_ONE],
    }
    payload.update(extra)
    return {"item_id": item_id, "kind": "arc_volume", "payload": payload}


def test_arc_volume_has_no_chapter_ratio_but_full_obligation_ratio() -> None:
    report = compile_planning_coverage_report(
        items=[_volume_item()],
        target_chapter_start=1,
        target_chapter_end=100,
        mode="arc_volume",
    )

    chapter = report.ratio_for(PlanningCoverageKind.CHAPTER)
    assert chapter.applicable is False
    assert chapter.ratio == 1.0
    obligation = report.ratio_for(PlanningCoverageKind.OBLIGATION)
    assert obligation.applicable is True
    assert (obligation.covered, obligation.total) == (2, 2)
    assert obligation.missing == ()
    history = report.ratio_for(PlanningCoverageKind.HISTORY_DECISION)
    assert history.applicable is False


def test_chapter_set_reports_chapter_and_history_coverage() -> None:
    items = [
        {
            "item_id": f"plan-item.ch{index}",
            "kind": "chapter_goal",
            "payload": {
                "chapter_index": index,
                "history_retrieval": (
                    {"requirement": "REQUIRED", "needs": [{"kind": "causal_history", "query": "x"}]}
                    if index == 1
                    else None
                ),
            },
        }
        for index in (1, 2, 3)
    ]
    # Drop the explicit None so chapter 2/3 simply have no decision.
    for item in items[1:]:
        del item["payload"]["history_retrieval"]  # type: ignore[index]

    report = compile_planning_coverage_report(
        items=items,
        target_chapter_start=1,
        target_chapter_end=3,
        mode="chapter_set",
    )

    chapter = report.ratio_for(PlanningCoverageKind.CHAPTER)
    assert (chapter.covered, chapter.total) == (3, 3)
    assert chapter.complete is True
    history = report.ratio_for(PlanningCoverageKind.HISTORY_DECISION)
    assert (history.covered, history.total) == (1, 3)
    assert history.missing == ("chapter.2", "chapter.3")
    assert history.complete is False


def test_missing_chapter_is_named_not_hidden_in_the_ratio() -> None:
    report = compile_planning_coverage_report(
        items=[
            {"item_id": "plan-item.ch1", "kind": "chapter_goal", "payload": {"chapter_index": 1}}
        ],
        target_chapter_start=1,
        target_chapter_end=3,
        mode="chapter_set",
    )

    chapter = report.ratio_for(PlanningCoverageKind.CHAPTER)
    assert chapter.missing == ("chapter.2", "chapter.3")
    assert abs(chapter.ratio - (1 / 3)) < 1e-9
    assert report.complete is False


def test_author_constraint_coverage_is_measured_against_its_own_wording() -> None:
    report = compile_planning_coverage_report(
        items=[_volume_item(capability_ceiling="正文语言必须为 zh-CN")],
        target_chapter_start=1,
        target_chapter_end=100,
        constraints=(_constraint("正文语言必须为 zh-CN"),),
        mode="arc_volume",
    )

    ratio = report.ratio_for(PlanningCoverageKind.AUTHOR_CONSTRAINT)
    assert ratio.complete is True
    assert ratio.covered == 1


def test_absent_constraint_is_reported_as_missing() -> None:
    report = compile_planning_coverage_report(
        items=[_volume_item()],
        target_chapter_start=1,
        target_chapter_end=100,
        constraints=(_constraint("正文语言必须为 zh-CN"),),
        mode="arc_volume",
    )

    ratio = report.ratio_for(PlanningCoverageKind.AUTHOR_CONSTRAINT)
    assert ratio.missing == ("author-constraint.language.0",)
    assert ratio.ratio == 0.0
    assert report.complete is False


def test_unreadable_responsibility_is_missing_not_counted_as_coverage() -> None:
    item = _volume_item(
        obligation_plan=[
            dict(_VOLUME_ONE[0]),
            {"kind": "objective", "summary": "缺 payoff 窗口", "setup_window": "1-10"},
        ]
    )

    report = compile_planning_coverage_report(
        items=[item],
        target_chapter_start=1,
        target_chapter_end=100,
        mode="arc_volume",
    )

    ratio = report.ratio_for(PlanningCoverageKind.OBLIGATION)
    assert ratio.total == 2
    assert ratio.covered == 1
    assert len(ratio.missing) == 1
    assert "no observable completion boundary" in ratio.missing[0]
    assert ratio.denominator_source == "declared responsibility table"
    assert ratio.complete is False


def test_declaration_missing_kind_or_description_is_missing() -> None:
    item = _volume_item()
    del item["payload"]["obligation_plan"]  # type: ignore[union-attr]
    item["payload"]["obligation_declarations"] = [  # type: ignore[index]
        {"kind": "objective", "summary": "合法责任", "not_before_chapter": 1},
        {"summary": "缺 kind", "not_before_chapter": 1},
    ]

    report = compile_planning_coverage_report(
        items=[item],
        target_chapter_start=1,
        target_chapter_end=100,
        mode="arc_volume",
    )

    ratio = report.ratio_for(PlanningCoverageKind.OBLIGATION)
    assert (ratio.covered, ratio.total) == (1, 2)
    assert ratio.missing == ("obligation_declarations[1] needs a kind and a description",)


def test_report_payload_is_serializable_evidence() -> None:
    report = compile_planning_coverage_report(
        items=[_volume_item()],
        target_chapter_start=1,
        target_chapter_end=100,
        mode="arc_volume",
    )
    payload = report.as_payload()

    assert payload["complete"] is True
    kinds = {entry["kind"] for entry in payload["ratios"]}
    assert kinds == {
        "chapter_coverage",
        "author_constraint_coverage",
        "obligation_coverage",
        "history_decision_coverage",
    }
