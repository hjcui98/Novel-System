from pathlib import Path

import pytest

from novel_agent.domain.ids import CommitId, StableId
from novel_agent.services.human_benchmark_compiler import (
    HumanBenchmarkCompileError,
    HumanBenchmarkCompiler,
)

ROOT = Path(__file__).parents[2]
PILOT = ROOT / "benchmarks/private/ztj_memory_pilot_v0.1"


def test_human_benchmark_compiler_rejects_missing_empty_and_malformed_text_sources(
    tmp_path: Path,
) -> None:
    compiler = HumanBenchmarkCompiler()
    with pytest.raises(HumanBenchmarkCompileError, match="does not exist"):
        compiler._text_root(tmp_path / "missing", StableId("case.missing"))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(HumanBenchmarkCompileError, match="is empty"):
        compiler._text_root(empty, StableId("case.empty"))


def test_human_benchmark_compiler_rejects_malformed_plan_evidence_and_gold() -> None:
    compiler = HumanBenchmarkCompiler()
    bundle = compiler.compile(PILOT)
    case = bundle.case_manifests[0]
    history = next(root for root in bundle.text_roots if root.root_hash == case.input_text_root)
    future = next(
        root for root in bundle.text_roots if root.root_hash == case.future_text_root_private
    )
    commit = CommitId(history.root_hash.root)
    future_evidence = compiler._all_evidence(future, commit, "future.test")

    with pytest.raises(HumanBenchmarkCompileError, match="target_plan"):
        compiler._plan_root(StableId("case.bad"), {"visible_outline": [], "target_plan": {}})
    empty_plan = compiler._plan_root(
        StableId("case.empty-plan"), {"visible_outline": [], "target_plan": [None]}
    )
    assert empty_plan.chapter_goals == ()
    with pytest.raises(HumanBenchmarkCompileError, match="must be an object"):
        compiler._historical_evidence({"gold_evidence_refs": []}, history, commit)
    with pytest.raises(HumanBenchmarkCompileError, match="mapping is malformed"):
        compiler._historical_evidence({"gold_evidence_refs": {1: []}}, history, commit)
    with pytest.raises(HumanBenchmarkCompileError, match="chapter is absent"):
        compiler._historical_evidence({"gold_evidence_refs": {"G": [999]}}, history, commit)
    with pytest.raises(HumanBenchmarkCompileError, match="prelude is absent"):
        compiler._historical_evidence(
            {"gold_evidence_refs": {"G": [0]}},
            history.model_copy(update={"prelude": None}),
            commit,
        )
    with pytest.raises(HumanBenchmarkCompileError, match="Gold items"):
        compiler._gold(
            {"target_range": [21, 25]},
            {"items": {}},
            {},
            future_evidence,
        )
    with pytest.raises(HumanBenchmarkCompileError, match="observed_use_gold"):
        compiler._gold(
            {"target_range": [21, 25], "observed_use_gold": {}},
            {"items": []},
            {},
            future_evidence,
        )
    with pytest.raises(HumanBenchmarkCompileError, match="lacks source evidence"):
        compiler._gold(
            {"target_range": [21, 25], "observed_use_gold": ["G"]},
            {"items": [None, {"id": "G", "fact": "fact"}]},
            {},
            future_evidence,
        )


def test_human_benchmark_compiler_fail_closed_parsers_and_scalar_helpers(
    tmp_path: Path,
) -> None:
    compiler = HumanBenchmarkCompiler()
    with pytest.raises(HumanBenchmarkCompileError, match="cannot read JSON"):
        compiler._json(tmp_path / "missing.json")
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{", encoding="utf-8")
    with pytest.raises(HumanBenchmarkCompileError, match="cannot read JSON"):
        compiler._json(bad_json)
    list_json = tmp_path / "list.json"
    list_json.write_text("[]", encoding="utf-8")
    with pytest.raises(HumanBenchmarkCompileError, match="root must be an object"):
        compiler._json(list_json)

    with pytest.raises(HumanBenchmarkCompileError, match="cannot read YAML"):
        compiler._yaml(tmp_path / "missing.yaml")
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("[", encoding="utf-8")
    with pytest.raises(HumanBenchmarkCompileError, match="cannot read YAML"):
        compiler._yaml(bad_yaml)
    list_yaml = tmp_path / "list.yaml"
    list_yaml.write_text("[]", encoding="utf-8")
    with pytest.raises(HumanBenchmarkCompileError, match="root must be an object"):
        compiler._yaml(list_yaml)

    for string_value in (None, ""):
        with pytest.raises(HumanBenchmarkCompileError, match="non-empty string"):
            compiler._string(string_value)
    with pytest.raises(HumanBenchmarkCompileError, match="string list"):
        compiler._strings("not-a-list")
    for integer_value in ("1", True):
        with pytest.raises(HumanBenchmarkCompileError, match="integer"):
            compiler._integer(integer_value)
    for range_value in ("1-2", [1]):
        with pytest.raises(HumanBenchmarkCompileError, match="two-item range"):
            compiler._range(range_value)


def test_human_benchmark_compiler_derives_content_bound_gate_subset() -> None:
    compiler = HumanBenchmarkCompiler()
    pilot = compiler.compile(PILOT)
    gate = compiler.derive_gate_subset(pilot, target_width=2)

    assert gate.content_hash != pilot.content_hash
    assert gate.bundle_id.root.endswith("gate-target-width-2")
    assert all(case.target_range[1] - case.target_range[0] + 1 <= 2 for case in gate.case_manifests)
    assert all(case.gate_eligible is False for case in gate.case_manifests)
    assert {case.future_text_root_private for case in gate.case_manifests}.issubset(
        {root.root_hash for root in gate.text_roots}
    )
    assert {case.input_plan_root for case in gate.case_manifests}.issubset(
        {root.root_hash for root in gate.plan_roots}
    )

    with pytest.raises(HumanBenchmarkCompileError, match="target width must be positive"):
        compiler.derive_gate_subset(pilot, target_width=0)
    missing_plan_case = pilot.case_manifests[0].model_copy(update={"input_plan_root": None})
    invalid = pilot.model_copy(
        update={"case_manifests": (missing_plan_case, *pilot.case_manifests[1:])}
    )
    with pytest.raises(HumanBenchmarkCompileError, match="requires an input PlanRoot"):
        compiler.derive_gate_subset(invalid)


def test_human_benchmark_compiler_rejects_every_invalid_plan_gold_reference() -> None:
    compiler = HumanBenchmarkCompiler()
    bundle = compiler.compile(PILOT)
    case = bundle.case_manifests[0]
    plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)
    future = next(
        root for root in bundle.text_roots if root.root_hash == case.future_text_root_private
    )
    commit = CommitId(case.input_text_root.root)
    base: dict[str, object] = {
        "plan_obligation_gold": ["PLAN"],
        "gold_evidence_refs": {},
    }
    cases: tuple[tuple[dict[str, object], str], ...] = (
        ({"plan_obligation_gold": {}}, "must be a list"),
        (
            base,
            "lacks explicit manifest evidence",
        ),
        (
            base | {"gold_evidence_refs": []},
            "must be an object",
        ),
        (
            base | {"gold_evidence_refs": {"PLAN": [21]}},
            "must use plan",
        ),
        (
            base | {"gold_evidence_refs": {"PLAN": ["plan:bad"]}},
            "chapter is invalid",
        ),
        (
            base | {"gold_evidence_refs": {"PLAN": ["plan:21", "plan:21"]}},
            "repeats a goal",
        ),
        (
            base | {"gold_evidence_refs": {"PLAN": ["plan:999"]}},
            "missing goal or target chapter",
        ),
    )
    for raw_case, message in cases:
        with pytest.raises(HumanBenchmarkCompileError, match=message):
            compiler._plan_gold(raw_case, plan, future, commit)
