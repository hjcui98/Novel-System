from pathlib import Path

import pytest

from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
from novel_agent.services.benchmark_importer import content_id
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
        compiler._historical_evidence({"gold_evidence_refs": []}, {"items": []}, history, commit)
    with pytest.raises(HumanBenchmarkCompileError, match="mapping is malformed"):
        compiler._historical_evidence(
            {"gold_evidence_refs": {1: []}}, {"items": []}, history, commit
        )
    with pytest.raises(HumanBenchmarkCompileError, match="chapter is absent"):
        compiler._historical_evidence(
            {"gold_evidence_refs": {"G": [999]}},
            {"items": []},
            history,
            commit,
        )
    with pytest.raises(HumanBenchmarkCompileError, match="prelude is absent"):
        compiler._historical_evidence(
            {"gold_evidence_refs": {"G": [0]}},
            {"items": []},
            history.model_copy(update={"prelude": None}),
            commit,
        )
    with pytest.raises(HumanBenchmarkCompileError, match="Gold items"):
        compiler._gold(
            {"target_range": [21, 25]},
            {"items": {}},
            {},
            {},
            future_evidence,
        )
    with pytest.raises(HumanBenchmarkCompileError, match="observed_use_gold"):
        compiler._gold(
            {"target_range": [21, 25], "observed_use_gold": {}},
            {"items": []},
            {},
            {},
            future_evidence,
        )
    with pytest.raises(HumanBenchmarkCompileError, match="lacks source evidence"):
        compiler._gold(
            {"target_range": [21, 25], "observed_use_gold": ["G"]},
            {"items": [None, {"id": "G", "fact": "fact"}]},
            {},
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

    with pytest.raises(HumanBenchmarkCompileError, match="unsupported Gold type"):
        compiler._gold_type({"type": "unknown-type"})
    assert set(compiler._applicable_profiles({})) == set(BenchmarkInformationProfile)
    invalid_profile_lists: tuple[object, ...] = ([], "visible_at_cutoff")
    for value in invalid_profile_lists:
        with pytest.raises(HumanBenchmarkCompileError, match="non-empty list"):
            compiler._applicable_profiles({"applicable_profiles": value})
    with pytest.raises(HumanBenchmarkCompileError, match="invalid information profile"):
        compiler._applicable_profiles({"applicable_profiles": ["not-a-profile"]})


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


def test_human_benchmark_compiler_precise_evidence_is_fail_closed() -> None:
    compiler = HumanBenchmarkCompiler()
    bundle = compiler.compile(PILOT)
    case = bundle.case_manifests[0]
    history = next(root for root in bundle.text_roots if root.root_hash == case.input_text_root)
    commit = CommitId(history.root_hash.root)
    raw_case = {"gold_evidence_refs": {"G": [0]}}

    with pytest.raises(HumanBenchmarkCompileError, match="Gold items must be a list"):
        compiler._historical_evidence(raw_case, {"items": {}}, history, commit)

    malformed: tuple[tuple[object, str], ...] = (
        ([], "non-empty list"),
        ({}, "non-empty list"),
        ([None], "must be an object"),
        ([{}], "requires evidence"),
        ([{"evidence": []}], "requires evidence"),
        (
            [
                {
                    "evidence": [{"chapter": 0, "quote": "按照中年道人的说法"}],
                    "components": "bad",
                }
            ],
            "components must be a list",
        ),
    )
    for raw_sets, message in malformed:
        with pytest.raises(HumanBenchmarkCompileError, match=message):
            compiler._historical_evidence(
                raw_case,
                {"items": [{"id": "G", "accepted_evidence_sets": raw_sets}]},
                history,
                commit,
            )

    with pytest.raises(HumanBenchmarkCompileError, match="must be an object"):
        compiler._annotated_evidence("bad", history, commit, namespace="test.raw")
    with pytest.raises(HumanBenchmarkCompileError, match="prelude is absent"):
        compiler._annotated_evidence(
            {"chapter": 0, "quote": "青山"},
            history.model_copy(update={"prelude": None}),
            commit,
            namespace="test.prelude",
        )
    with pytest.raises(HumanBenchmarkCompileError, match="chapter is absent"):
        compiler._annotated_evidence(
            {"chapter": 999, "quote": "missing"},
            history,
            commit,
            namespace="test.chapter",
        )
    for quote in ("not present anywhere", "陈长生"):
        with pytest.raises(HumanBenchmarkCompileError, match="found="):
            compiler._annotated_evidence(
                {"chapter": 0, "quote": quote},
                history,
                commit,
                namespace="test.quote",
            )


def test_human_benchmark_compiler_precise_evidence_defaults_are_content_bound() -> None:
    compiler = HumanBenchmarkCompiler()
    bundle = compiler.compile(PILOT)
    case = bundle.case_manifests[0]
    history = next(root for root in bundle.text_roots if root.root_hash == case.input_text_root)
    commit = CommitId(history.root_hash.root)
    historical, accepted = compiler._historical_evidence(
        {"gold_evidence_refs": {"G": [0]}},
        {
            "items": [
                {
                    "id": "G",
                    "target_components": ["component"],
                    "accepted_evidence_sets": [
                        {"evidence": [{"chapter": 0, "quote": "按照中年道人的说法"}]}
                    ],
                }
            ]
        },
        history,
        commit,
    )

    assert historical["G"] == accepted["G"][0].evidence_refs
    assert accepted["G"][0].evidence_set_id.root == "accepted.G.1"
    assert accepted["G"][0].component_ids == ("component",)


def test_packaging_frontmatter_is_excluded_but_narrative_prelude_is_preserved() -> None:
    packaged = "作者\uff1a某人\n内容简介\uff1a未来剧透\n第一卷 开始\n序章正文"
    assert HumanBenchmarkCompiler._strip_packaging_frontmatter(packaged) == (
        "第一卷 开始\n序章正文"
    )
    narrative_only = "第一卷 开始\n序章正文"
    assert HumanBenchmarkCompiler._strip_packaging_frontmatter(narrative_only) == narrative_only
    no_volume_marker = "序章正文,没有卷标记"
    assert HumanBenchmarkCompiler._strip_packaging_frontmatter(no_volume_marker) == no_volume_marker


def test_planning_contexts_are_compiled_bound_and_typed() -> None:
    compiler = HumanBenchmarkCompiler()
    bundle = compiler.compile(PILOT)

    assert len(bundle.planning_contexts) == len(bundle.case_manifests)
    for case in bundle.case_manifests:
        assert case.information_profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
        context = next(
            item
            for item in bundle.planning_contexts
            if item.source_hash == case.planning_context_hash
        )
        assert case.task_intent == context.task_intent
        assert content_id(context.model_dump(mode="json")) == case.planning_context_ref
        assert context.profile is case.information_profile
        assert context.target_range == case.target_range
        assert context.visible_outline_nodes
        assert context.chapter_goals
        assert context.planner_may_read_plan is True
        plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)
        assert [goal.goal_id for goal in context.chapter_goals] == [
            goal.goal_id for goal in plan.chapter_goals
        ]
        assert [node.node_id for node in context.visible_outline_nodes] == [
            node.plan_node_id for node in plan.nodes
        ]


def test_planning_context_compilation_is_fail_closed_on_raw_yaml_fields() -> None:
    compiler = HumanBenchmarkCompiler()
    with pytest.raises(HumanBenchmarkCompileError, match="unsupported input mode"):
        compiler._compile_planning_context(
            StableId("case.bad-mode"),
            {"mode": "some_future_mode"},
            (21, 25),
        )
    with pytest.raises(HumanBenchmarkCompileError, match="non-empty string"):
        compiler._compile_planning_context(
            StableId("case.no-task"),
            {"mode": "plan_conditioned", "task": ""},
            (21, 25),
        )
    with pytest.raises(HumanBenchmarkCompileError, match="target_plan must be a list"):
        compiler._compile_planning_context(
            StableId("case.bad-plan"),
            {
                "mode": "plan_conditioned",
                "task": "任务",
                "visible_outline": ["大纲"],
                "target_plan": {},
            },
            (21, 25),
        )


def test_planning_context_rejects_invalid_ranges_and_duplicate_ids() -> None:
    from novel_agent.domain.benchmark import (
        AuthorPlanningContext,
        ChapterGoal,
        VisibleOutlineNode,
    )

    outline_nodes = (
        VisibleOutlineNode(
            node_id=StableId("plan.outline.1"),
            title="Visible outline 1",
            summary="粗粒度大纲。",
        ),
    )
    base = {
        "profile": BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        "task_intent": "为写作准备历史记忆。",
        "target_range": (21, 23),
        "visible_outline_nodes": outline_nodes,
        "chapter_goals": (),
        "source_hash": content_id({"source": "test"}),
    }
    with pytest.raises(ValueError, match="target range is invalid"):
        AuthorPlanningContext.model_validate(base | {"target_range": (21, 19)})
    duplicated_goal = ChapterGoal(
        goal_id=StableId("goal.outline.1"),
        chapter_index=21,
        summary="重复目标。",
    )
    with pytest.raises(ValueError, match="chapter goal ids must be unique"):
        AuthorPlanningContext.model_validate(
            base
            | {
                "chapter_goals": (duplicated_goal, duplicated_goal),
            }
        )
    with pytest.raises(ValueError, match="outline node ids must be unique"):
        AuthorPlanningContext.model_validate(
            base
            | {
                "visible_outline_nodes": (
                    outline_nodes[0],
                    outline_nodes[0],
                )
            }
        )
    with pytest.raises(ValueError, match="node type must be visible_outline"):
        VisibleOutlineNode.model_validate(
            {
                "node_id": StableId("plan.outline.1"),
                "node_type": "chapter_goal",
                "title": "Visible outline 1",
                "summary": "粗粒度大纲。",
            }
        )


def test_gate_subset_requires_bound_and_present_planning_context() -> None:
    compiler = HumanBenchmarkCompiler()
    bundle = compiler.compile(PILOT)
    first = bundle.case_manifests[0]
    unbound = first.model_copy(update={"planning_context_hash": None, "planning_context_ref": None})
    with pytest.raises(HumanBenchmarkCompileError, match="requires a bound planning context"):
        compiler.derive_gate_subset(
            bundle.model_copy(update={"case_manifests": (unbound, *bundle.case_manifests[1:])})
        )

    with pytest.raises(HumanBenchmarkCompileError, match="planning context is missing"):
        compiler.derive_gate_subset(bundle.model_copy(update={"planning_contexts": ()}))
