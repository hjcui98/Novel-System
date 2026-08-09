from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from novel_agent.domain.benchmark import BenchmarkBundle, ChapterSummaryRootDocument
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
from novel_agent.services.benchmark_importer import (
    BenchmarkBundleImporter,
    BenchmarkImportError,
    bundle_content_id,
    content_id,
    summary_root_content_id,
    text_root_content_id,
)
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

UNKNOWN_HASH = ArtifactId("sha256:" + "f" * 64)
PILOT = Path(__file__).parents[2] / "benchmarks/private/ztj_memory_pilot_v0.1"


def rehash(bundle: BenchmarkBundle) -> BenchmarkBundle:
    return bundle.model_copy(update={"content_hash": bundle_content_id(bundle)})


def validate_error(bundle: BenchmarkBundle, message: str) -> None:
    with pytest.raises(BenchmarkImportError, match=message):
        BenchmarkBundleImporter().validate(rehash(bundle))


def test_load_wraps_missing_files_and_schema_errors(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkImportError, match="cannot read"):
        BenchmarkBundleImporter().load(tmp_path / "missing.json")
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    with pytest.raises(BenchmarkImportError, match="schema validation"):
        BenchmarkBundleImporter().load(invalid)


def test_planning_context_ref_is_bound_to_bundle_and_profile() -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    BenchmarkBundleImporter().validate(bundle)
    case = bundle.case_manifests[0]
    context = next(
        item for item in bundle.planning_contexts if item.source_hash == case.planning_context_hash
    )

    missing = case.model_copy(
        update={
            "planning_context_hash": UNKNOWN_HASH,
            "planning_context_ref": UNKNOWN_HASH,
        }
    )
    validate_error(
        bundle.model_copy(update={"case_manifests": (missing, *bundle.case_manifests[1:])}),
        "missing planning context",
    )

    swapped = case.model_copy(
        update={
            "planning_context_ref": UNKNOWN_HASH,
        }
    )
    validate_error(
        bundle.model_copy(update={"case_manifests": (swapped, *bundle.case_manifests[1:])}),
        "planning context ref mismatch",
    )

    profile_mismatch = case.model_copy(
        update={
            "information_profile": (
                BenchmarkInformationProfile.VISIBLE_AT_CUTOFF
                if case.information_profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
                else BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
            )
        }
    )
    validate_error(
        bundle.model_copy(
            update={"case_manifests": (profile_mismatch, *bundle.case_manifests[1:])}
        ),
        "profile mismatch",
    )

    orphan_intent = case.model_copy(
        update={
            "task_intent": "有任务意图却没有计划上下文引用。",
            "planning_context_ref": None,
            "planning_context_hash": None,
        }
    )
    validate_error(
        bundle.model_copy(update={"case_manifests": (orphan_intent, *bundle.case_manifests[1:])}),
        "lacks a planning context ref",
    )
    assert context is not None


def test_planning_context_normalization_and_case_binding_fail_closed() -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    case = bundle.case_manifests[0]
    context = next(
        item for item in bundle.planning_contexts if item.source_hash == case.planning_context_hash
    )

    validate_error(
        bundle.model_copy(
            update={
                "case_manifests": (
                    case.model_copy(update={"planning_context_ref": None}),
                    *bundle.case_manifests[1:],
                )
            }
        ),
        "ref/hash must appear as a pair",
    )

    corrupt = context.model_copy(update={"task_intent": context.task_intent + " drift"})
    validate_error(
        bundle.model_copy(update={"planning_contexts": (corrupt, *bundle.planning_contexts[1:])}),
        "source hash mismatch",
    )
    validate_error(
        bundle.model_copy(update={"planning_contexts": (context, context)}),
        "duplicate normalized planning context identity",
    )

    for update, message in (
        ({"target_range": (22, 23)}, "target range mismatch"),
        ({"task_intent": context.task_intent + " drift"}, "task intent mismatch"),
    ):
        changed = context.model_copy(update=update)
        changed = changed.model_copy(
            update={
                "source_hash": content_id(
                    {
                        "profile": changed.profile.value,
                        "task_intent": changed.task_intent,
                        "target_range": changed.target_range,
                        "visible_outline_nodes": [
                            node.model_dump(mode="json") for node in changed.visible_outline_nodes
                        ],
                        "chapter_goals": [
                            goal.model_dump(mode="json") for goal in changed.chapter_goals
                        ],
                        "planner_may_read_plan": changed.planner_may_read_plan,
                    }
                )
            }
        )
        changed_case = case.model_copy(
            update={
                "planning_context_hash": changed.source_hash,
                "planning_context_ref": content_id(changed.model_dump(mode="json")),
            }
        )
        validate_error(
            bundle.model_copy(
                update={"planning_contexts": (changed,), "case_manifests": (changed_case,)}
            ),
            message,
        )

    assert case.information_profile is not None
    mismatched_task = case.model_copy(
        update={
            "task_contract": build_safe_task_contract(
                case_id=case.case_id,
                checkpoint_chapter=case.history_range[1],
                target_range=case.target_range,
                information_profile=case.information_profile,
                task_intent="drift",
                planning_context_ref=case.planning_context_ref,
                planning_context_hash=case.planning_context_hash,
            )
        }
    )
    validate_error(
        bundle.model_copy(update={"case_manifests": (mismatched_task,)}),
        "task/planning context mismatch",
    )

    matching_task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=case.history_range[1],
        target_range=case.target_range,
        information_profile=case.information_profile,
        task_intent=case.task_intent,
        planning_context_ref=case.planning_context_ref,
        planning_context_hash=case.planning_context_hash,
    )
    BenchmarkBundleImporter().validate(
        rehash(
            bundle.model_copy(
                update={
                    "case_manifests": (case.model_copy(update={"task_contract": matching_task}),)
                }
            )
        )
    )

    planless_gold = tuple(
        item.model_copy(update={"plan_evidence_refs": ()}) for item in case.plan_obligation_gold
    )
    planless = case.model_copy(
        update={
            "input_plan_root": None,
            "chapter_goal_ids": (),
            "plan_obligation_gold": planless_gold,
            "gate_eligible": False,
            "task_contract": matching_task,
        }
    )
    validate_error(
        bundle.model_copy(update={"case_manifests": (planless,)}),
        "APC planning context requires a PlanRoot",
    )

    visible_context = context.model_copy(
        update={"profile": BenchmarkInformationProfile.VISIBLE_AT_CUTOFF}
    )
    visible_context = visible_context.model_copy(
        update={
            "source_hash": content_id(
                {
                    "profile": visible_context.profile.value,
                    "task_intent": visible_context.task_intent,
                    "target_range": visible_context.target_range,
                    "visible_outline_nodes": [
                        node.model_dump(mode="json")
                        for node in visible_context.visible_outline_nodes
                    ],
                    "chapter_goals": [
                        goal.model_dump(mode="json") for goal in visible_context.chapter_goals
                    ],
                    "planner_may_read_plan": visible_context.planner_may_read_plan,
                }
            )
        }
    )
    visible_case = case.model_copy(
        update={
            "information_profile": BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            "planning_context_hash": visible_context.source_hash,
            "planning_context_ref": content_id(visible_context.model_dump(mode="json")),
            "task_contract": None,
        }
    )
    BenchmarkBundleImporter().validate(
        rehash(
            bundle.model_copy(
                update={"planning_contexts": (visible_context,), "case_manifests": (visible_case,)}
            )
        )
    )

    altered_context = context.model_copy(
        update={"visible_outline_nodes": context.visible_outline_nodes[:-1]}
    )
    altered_context = altered_context.model_copy(
        update={
            "source_hash": content_id(
                {
                    "profile": altered_context.profile.value,
                    "task_intent": altered_context.task_intent,
                    "target_range": altered_context.target_range,
                    "visible_outline_nodes": [
                        node.model_dump(mode="json")
                        for node in altered_context.visible_outline_nodes
                    ],
                    "chapter_goals": [
                        goal.model_dump(mode="json") for goal in altered_context.chapter_goals
                    ],
                    "planner_may_read_plan": altered_context.planner_may_read_plan,
                }
            )
        }
    )
    altered_case = case.model_copy(
        update={
            "planning_context_hash": altered_context.source_hash,
            "planning_context_ref": content_id(altered_context.model_dump(mode="json")),
            "task_contract": None,
        }
    )
    validate_error(
        bundle.model_copy(
            update={"planning_contexts": (altered_context,), "case_manifests": (altered_case,)}
        ),
        "planning context and PlanRoot content mismatch",
    )


def test_bundle_rejects_duplicate_planning_context_source_hashes() -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    duplicated = bundle.model_copy(update={"planning_contexts": (bundle.planning_contexts[0],) * 2})
    validate = cast(Any, BenchmarkBundle.validate_unique_roots_and_cases)
    with pytest.raises(ValueError, match="planning context source hashes must be unique"):
        validate(duplicated)


def test_root_hash_and_manifest_reference_failures() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    text = bundle.text_roots[0].model_copy(update={"chapters": bundle.text_roots[0].chapters[:-1]})
    validate_error(
        bundle.model_copy(update={"text_roots": (text, bundle.text_roots[1])}),
        "text root content hash",
    )
    plan = bundle.plan_roots[0].model_copy(update={"chapter_goals": ()})
    validate_error(bundle.model_copy(update={"plan_roots": (plan,)}), "plan root content hash")
    world = bundle.world_roots[0].model_copy(update={"events": ()})
    validate_error(bundle.model_copy(update={"world_roots": (world,)}), "world root content hash")

    missing_text = case.model_copy(update={"input_text_root": UNKNOWN_HASH})
    validate_error(
        bundle.model_copy(update={"case_manifests": (missing_text,)}),
        "missing text root",
    )
    missing_plan = case.model_copy(update={"input_plan_root": UNKNOWN_HASH})
    validate_error(
        bundle.model_copy(update={"case_manifests": (missing_plan,)}),
        "missing plan root",
    )
    missing_goal = case.model_copy(update={"chapter_goal_ids": (StableId("goal.missing"),)})
    validate_error(
        bundle.model_copy(update={"case_manifests": (missing_goal,)}),
        "missing chapter goal",
    )
    missing_world = case.model_copy(update={"input_world_root_verified": UNKNOWN_HASH})
    validate_error(
        bundle.model_copy(update={"case_manifests": (missing_world,)}),
        "missing world root",
    )


def test_history_and_future_chapter_completeness_failures() -> None:
    bundle = make_synthetic_bundle()
    history, future = bundle.text_roots
    case = bundle.case_manifests[0]

    provisional_history = history.model_copy(update={"chapters": history.chapters[1:]})
    short_history = provisional_history.model_copy(
        update={"root_hash": text_root_content_id(provisional_history)}
    )
    history_case = case.model_copy(
        update={
            "input_text_root": short_history.root_hash,
            "input_summary_root": None,
            "gate_eligible": False,
        }
    )
    validate_error(
        bundle.model_copy(
            update={
                "text_roots": (short_history, future),
                "summary_roots": (),
                "case_manifests": (history_case,),
            }
        ),
        "history chapters are incomplete",
    )

    provisional_future = future.model_copy(update={"chapters": future.chapters[:-1]})
    short_future = provisional_future.model_copy(
        update={"root_hash": text_root_content_id(provisional_future)}
    )
    future_case = case.model_copy(update={"future_text_root_private": short_future.root_hash})
    validate_error(
        bundle.model_copy(
            update={"text_roots": (history, short_future), "case_manifests": (future_case,)}
        ),
        "future chapters are incomplete",
    )


def test_summary_root_hash_source_chapter_and_case_requirements() -> None:
    bundle = make_synthetic_bundle()
    summary_root = bundle.summary_roots[0]
    case = bundle.case_manifests[0]

    changed_summary = summary_root.summaries[0].model_copy(update={"summary": "changed"})
    bad_hash = summary_root.model_copy(
        update={"summaries": (changed_summary, *summary_root.summaries[1:])}
    )
    validate_error(bundle.model_copy(update={"summary_roots": (bad_hash,)}), "summary root content")

    missing_source_provisional = summary_root.model_copy(update={"source_text_root": UNKNOWN_HASH})
    missing_source = missing_source_provisional.model_copy(
        update={"root_hash": summary_root_content_id(missing_source_provisional)}
    )
    missing_source_case = case.model_copy(update={"input_summary_root": missing_source.root_hash})
    validate_error(
        bundle.model_copy(
            update={
                "summary_roots": (missing_source,),
                "case_manifests": (missing_source_case,),
            }
        ),
        "missing text root",
    )

    wrong_chapter_summary = summary_root.summaries[0].model_copy(
        update={"chapter_id": StableId("chapter.wrong")}
    )
    wrong_chapter_provisional = summary_root.model_copy(
        update={"summaries": (wrong_chapter_summary, *summary_root.summaries[1:])}
    )
    wrong_chapter = wrong_chapter_provisional.model_copy(
        update={"root_hash": summary_root_content_id(wrong_chapter_provisional)}
    )
    wrong_chapter_case = case.model_copy(update={"input_summary_root": wrong_chapter.root_hash})
    validate_error(
        bundle.model_copy(
            update={
                "summary_roots": (wrong_chapter,),
                "case_manifests": (wrong_chapter_case,),
            }
        ),
        "does not match",
    )

    incomplete_provisional = summary_root.model_copy(
        update={"summaries": summary_root.summaries[:-1]}
    )
    incomplete = incomplete_provisional.model_copy(
        update={"root_hash": summary_root_content_id(incomplete_provisional)}
    )
    incomplete_case = case.model_copy(update={"input_summary_root": incomplete.root_hash})
    validate_error(
        bundle.model_copy(
            update={"summary_roots": (incomplete,), "case_manifests": (incomplete_case,)}
        ),
        "required by B1",
    )

    missing_case = case.model_copy(update={"input_summary_root": UNKNOWN_HASH})
    validate_error(
        bundle.model_copy(update={"case_manifests": (missing_case,)}),
        "missing summary root",
    )
    shorter_case = case.model_copy(update={"history_range": (1, 16)})
    validate_error(
        bundle.model_copy(update={"case_manifests": (shorter_case,)}),
        "outside history",
    )

    future_summary_provisional = ChapterSummaryRootDocument(
        root_hash=UNKNOWN_HASH,
        schema_version=summary_root.schema_version,
        source_text_root=bundle.text_roots[1].root_hash,
        summaries=(),
    )
    future_summary = future_summary_provisional.model_copy(
        update={"root_hash": summary_root_content_id(future_summary_provisional)}
    )
    wrong_basis_case = case.model_copy(
        update={
            "input_summary_root": future_summary.root_hash,
            "gate_eligible": False,
        }
    )
    validate_error(
        bundle.model_copy(
            update={
                "summary_roots": (future_summary,),
                "case_manifests": (wrong_basis_case,),
            }
        ),
        "does not describe",
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("root_hash", UNKNOWN_HASH, "declared text root"),
        ("object_hash", UNKNOWN_HASH, "object hash"),
        ("chapter_id", StableId("chapter.wrong"), "chapter or scene"),
    ),
)
def test_evidence_binding_mismatches(field: str, value: object, message: str) -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    gold = case.observed_use_gold[0]
    evidence = gold.evidence_refs[0].model_copy(update={field: value})
    changed_gold = gold.model_copy(update={"evidence_refs": (evidence,)})
    changed_case = case.model_copy(update={"observed_use_gold": (changed_gold,)})
    validate_error(
        bundle.model_copy(update={"case_manifests": (changed_case,)}),
        message,
    )


def test_evidence_missing_block_and_out_of_bounds_span() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    gold = case.observed_use_gold[0]
    evidence = gold.evidence_refs[0]
    assert evidence.span is not None
    missing_span = evidence.span.model_copy(update={"block_id": StableId("block.missing")})
    missing = evidence.model_copy(update={"span": missing_span})
    missing_gold = gold.model_copy(update={"evidence_refs": (missing,)})
    validate_error(
        bundle.model_copy(
            update={
                "case_manifests": (case.model_copy(update={"observed_use_gold": (missing_gold,)}),)
            }
        ),
        "missing block",
    )

    long_span = evidence.span.model_copy(update={"end": 10_000})
    outside = evidence.model_copy(update={"span": long_span})
    outside_gold = gold.model_copy(update={"evidence_refs": (outside,)})
    validate_error(
        bundle.model_copy(
            update={
                "case_manifests": (case.model_copy(update={"observed_use_gold": (outside_gold,)}),)
            }
        ),
        "exceeds block length",
    )


def test_optional_plan_and_verified_world_can_be_explicitly_unavailable() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0].model_copy(
        update={
            "input_plan_root": None,
            "input_summary_root": None,
            "input_world_root_verified": None,
            "chapter_goal_ids": (),
            "expected_tracks": (),
            "gate_eligible": False,
        }
    )
    reduced = rehash(
        bundle.model_copy(
            update={
                "plan_roots": (),
                "summary_roots": (),
                "world_roots": (),
                "case_manifests": (case,),
                "replay_manifests": (),
            }
        )
    )
    BenchmarkBundleImporter().validate(reduced)


def test_replay_manifest_missing_roots_and_chapters_are_rejected() -> None:
    bundle = make_synthetic_bundle()
    replay = bundle.replay_manifests[0]
    validate_error(
        bundle.model_copy(
            update={
                "replay_manifests": (replay.model_copy(update={"target_text_root": UNKNOWN_HASH}),)
            }
        ),
        "references a missing root",
    )
    validate_error(
        bundle.model_copy(
            update={"replay_manifests": (replay.model_copy(update={"chapter_range": (21, 24)}),)}
        ),
        "chapters are incomplete",
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("plan_root_hash", UNKNOWN_HASH, "another PlanRoot"),
        ("goal_id", StableId("goal.missing"), "missing goal"),
        ("object_hash", UNKNOWN_HASH, "goal hash mismatch"),
    ),
)
def test_plan_gold_evidence_is_bound_to_exact_plan_goal(
    field: str,
    value: object,
    message: str,
) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    case = bundle.case_manifests[0]
    gold = case.plan_obligation_gold[0]
    evidence = gold.plan_evidence_refs[0].model_copy(update={field: value})
    changed = gold.model_copy(update={"plan_evidence_refs": (evidence,)})
    changed_case = case.model_copy(update={"plan_obligation_gold": (changed,)})
    validate_error(
        bundle.model_copy(
            update={
                "case_manifests": (changed_case, *bundle.case_manifests[1:]),
            }
        ),
        message,
    )


def test_plan_gold_evidence_requires_input_plan_root() -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    case = bundle.case_manifests[0].model_copy(update={"input_plan_root": None})
    validate_error(
        bundle.model_copy(update={"case_manifests": (case, *bundle.case_manifests[1:])}),
        "requires an input PlanRoot",
    )
