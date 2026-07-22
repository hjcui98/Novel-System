from pathlib import Path

from novel_agent.services.benchmark_importer import BenchmarkBundleImporter
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler

ROOT = Path(__file__).parents[2]
PILOT = ROOT / "benchmarks/private/ztj_memory_pilot_v0.1"


def test_real_ztj_pilot_compiles_to_five_isolated_typed_checkpoint_cases() -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    BenchmarkBundleImporter().validate(bundle)

    assert len(bundle.case_manifests) == 5
    assert tuple(case.history_range[1] for case in bundle.case_manifests) == (
        20,
        40,
        60,
        80,
        95,
    )
    assert all(case.gate_eligible is False for case in bundle.case_manifests)
    assert all(case.expected_tracks == () for case in bundle.case_manifests)
    assert all(len(case.plan_obligation_gold) == 5 for case in bundle.case_manifests)
    assert all(
        all(item.plan_evidence_refs for item in case.plan_obligation_gold)
        for case in bundle.case_manifests
    )
    for case in bundle.case_manifests:
        assert case.input_text_root != case.future_text_root_private
        history = next(root for root in bundle.text_roots if root.root_hash == case.input_text_root)
        future = next(
            root for root in bundle.text_roots if root.root_hash == case.future_text_root_private
        )
        assert not ({item.chapter_index for item in history.chapters} & set(case.target_range))
        assert {item.chapter_index for item in future.chapters} == set(
            range(case.target_range[0], case.target_range[1] + 1)
        )
