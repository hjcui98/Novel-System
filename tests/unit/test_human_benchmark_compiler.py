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
