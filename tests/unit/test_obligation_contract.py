"""Contract tests for the shared obligation declaration/action normalization."""

from __future__ import annotations

import pytest

from novel_agent.domain.memory import ObligationKind
from novel_agent.domain.obligation_contract import (
    ObligationAction,
    ObligationContractError,
    ObligationWindow,
    compile_legacy_obligation_plan,
    compile_obligation_actions,
    is_legacy_obligation_plan,
    parse_chapter_window,
)

# Real shapes taken from the frozen v6 artifacts (see the remediation plan §2.2).
_V6_RESPONSIBILITY = {
    "kind": "objective",
    "not_before_chapter": 1,
    "payoff_window": "90-100",
    "progress_windows": ["51-80"],
    "setup_window": "1-50",
    "summary": "陆沉舟获得铜铭",
}
_V6_FORESHADOWING = {
    "kind": "foreshadowing",
    "not_before_chapter": 1,
    "payoff_window": "70-100",
    "progress_windows": ["31-60"],
    "setup_window": "1-30",
    "summary": "确认残星纹为断序星纹",
}
_V6_CHAPTER_ACTION = "setup: 确认残星纹为断序星纹 (setup_window: 1-30)"


def test_parse_chapter_window_reads_ranges_and_numbers() -> None:
    assert parse_chapter_window("1-50", field="w").as_text() == "1-50"
    assert parse_chapter_window(7, field="w").as_text() == "7-7"
    assert parse_chapter_window(" 101 - 120 ", field="w").as_text() == "101-120"


@pytest.mark.parametrize(
    "value",
    ["", "0-5", "50-10", "1..5", "第一卷", "1-", "-5", None, True, ["1-5"]],
)
def test_parse_chapter_window_rejects_ambiguous_values(value: object) -> None:
    with pytest.raises(ObligationContractError):
        parse_chapter_window(value, field="w")


def test_legacy_detection_distinguishes_the_declaration_form() -> None:
    assert is_legacy_obligation_plan([_V6_RESPONSIBILITY]) is True
    assert is_legacy_obligation_plan([]) is False
    assert is_legacy_obligation_plan("summary") is False
    assert (
        is_legacy_obligation_plan([{"summary": "x", "kind": "objective", "obligation_id": "id"}])
        is False
    )


def test_compile_legacy_plan_keeps_every_v6_responsibility_traceable() -> None:
    compilation = compile_legacy_obligation_plan([_V6_RESPONSIBILITY, _V6_FORESHADOWING])

    assert compilation.complete is True
    assert len(compilation.declarations) == 2
    first, second = compilation.declarations
    assert first.kind is ObligationKind.OBJECTIVE
    assert first.description == "陆沉舟获得铜铭"
    assert first.not_before_chapter == 1
    assert first.setup_window is not None and first.setup_window.as_text() == "1-50"
    assert [window.as_text() for window in first.progress_windows] == ["51-80"]
    assert first.payoff_window is not None and first.payoff_window.as_text() == "90-100"
    assert first.source_form == "obligation_plan"
    assert first.source_ordinal == 0
    assert second.kind is ObligationKind.FORESHADOWING
    assert second.as_source_record() == {
        "source_form": "obligation_plan",
        "source_ordinal": 1,
        "setup_window": "1-30",
        "progress_windows": ["31-60"],
        "payoff_window": "70-100",
    }


def test_compile_legacy_plan_reports_unreadable_entries_instead_of_dropping_them() -> None:
    compilation = compile_legacy_obligation_plan(
        [
            _V6_RESPONSIBILITY,
            {"kind": "objective", "summary": "缺窗口"},
            {"kind": "unknown-kind", "summary": "未知类型", "setup_window": "1-5"},
            {"summary": "无类型", "setup_window": "1-5"},
            "自由文本责任",
            {"kind": "promise", "summary": "窗口不可解析", "setup_window": "第一卷"},
        ]
    )

    assert compilation.complete is False
    assert len(compilation.declarations) == 1
    assert len(compilation.discrepancies) == 5
    assert any("no setup, progress or payoff window" in item for item in compilation.discrepancies)
    assert any("no obligation kind" in item for item in compilation.discrepancies)
    assert any("unknown kind" in item for item in compilation.discrepancies)
    assert any("not an object" in item for item in compilation.discrepancies)
    assert any("not a chapter number or range" in item for item in compilation.discrepancies)


def test_compile_legacy_plan_reports_a_missing_summary() -> None:
    compilation = compile_legacy_obligation_plan(
        [{"kind": "objective", "setup_window": "1-5", "payoff_window": "6-9"}]
    )

    assert compilation.declarations == ()
    assert compilation.discrepancies == (
        "obligation_plan entry 0 has no usable summary/description",
    )


def test_compile_legacy_plan_rejects_a_non_list_value() -> None:
    compilation = compile_legacy_obligation_plan({"summary": "x"})

    assert compilation.declarations == ()
    assert compilation.discrepancies == (
        "obligation_plan must be a list of responsibility objects",
    )


def test_declaration_payload_exposes_only_binder_inputs() -> None:
    declaration = compile_legacy_obligation_plan([_V6_RESPONSIBILITY]).declarations[0]

    assert declaration.as_binder_payload() == {
        "kind": "objective",
        "summary": "陆沉舟获得铜铭",
        "not_before_chapter": 1,
    }


def test_legacy_free_text_action_is_rejected_with_guidance() -> None:
    compilation = compile_obligation_actions([_V6_CHAPTER_ACTION])

    assert compilation.actions == ()
    assert compilation.complete is False
    assert "free-text string" in compilation.discrepancies[0]
    assert "accepted obligation id" in compilation.discrepancies[0]


def test_normalized_action_round_trips_through_its_payload() -> None:
    compilation = compile_obligation_actions(
        [
            {
                "obligation_id": "obligation.vol_01.0.objective",
                "action": "progress",
                "expected_delta": "陆沉舟首次听到铜铭来历但不得揭示断序真相",
                "window": "progress",
            }
        ]
    )

    assert compilation.complete is True
    action = compilation.actions[0]
    assert action.action is ObligationAction.PROGRESS
    assert action.window is ObligationWindow.PROGRESS
    assert action.resolves_obligation is False
    assert action.as_payload() == {
        "obligation_id": "obligation.vol_01.0.objective",
        "action": "PROGRESS",
        "expected_delta": "陆沉舟首次听到铜铭来历但不得揭示断序真相",
        "window": "progress",
    }


@pytest.mark.parametrize(
    ("action", "fragment"),
    [
        ({"action": "PROGRESS", "expected_delta": "x"}, "string obligation_id"),
        ({"obligation_id": "obligation.x", "expected_delta": "x"}, "explicit action"),
        (
            {"obligation_id": "obligation.x", "action": "REVEAL", "expected_delta": "x"},
            "unsupported action",
        ),
        ({"obligation_id": "obligation.x", "action": "PROGRESS"}, "non-empty expected_delta"),
        (
            {"obligation_id": "obligation.x", "action": "PROGRESS", "expected_delta": " "},
            "non-empty expected_delta",
        ),
        (
            {
                "obligation_id": "obligation.x",
                "action": "PROGRESS",
                "expected_delta": "x",
                "window": "中期",
            },
            "unknown window",
        ),
        (
            {"obligation_id": " ", "action": "PROGRESS", "expected_delta": "x"},
            "string obligation_id",
        ),
        (42, "must be an object"),
    ],
)
def test_action_discrepancies_are_explicit(action: object, fragment: str) -> None:
    compilation = compile_obligation_actions([action])

    assert compilation.actions == ()
    assert len(compilation.discrepancies) == 1
    assert fragment in compilation.discrepancies[0]


def test_action_list_compiles_valid_items_beside_discrepancies() -> None:
    compilation = compile_obligation_actions(
        [
            _V6_CHAPTER_ACTION,
            {
                "obligation_id": "obligation.vol_01.0.objective",
                "action": "PAYOFF",
                "expected_delta": "铜铭来历公开",
            },
        ]
    )

    assert len(compilation.actions) == 1
    assert compilation.actions[0].resolves_obligation is True
    assert compilation.complete is False


def test_absent_actions_compile_to_nothing() -> None:
    compilation = compile_obligation_actions(None)

    assert compilation.actions == ()
    assert compilation.complete is True
    assert compile_obligation_actions([]).complete is True


def test_non_list_action_value_is_a_discrepancy() -> None:
    compilation = compile_obligation_actions({"obligation_id": "x"})

    assert compilation.discrepancies == ("obligation_actions must be a list",)


def test_resolved_obligation_is_only_reported_for_payoff() -> None:
    compilation = compile_obligation_actions(
        [
            {"obligation_id": "obligation.x", "action": "DEFER", "expected_delta": "推迟"},
            {"obligation_id": "obligation.y", "action": "SETUP", "expected_delta": "埋设"},
            {"obligation_id": "obligation.z", "action": "PAYOFF", "expected_delta": "兑现"},
        ]
    )

    assert [action.resolves_obligation for action in compilation.actions] == [False, False, True]
    assert compilation.complete is True
