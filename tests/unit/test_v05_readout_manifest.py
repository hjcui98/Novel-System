"""V0.5 runner identity compiler: 51 QA / 30 Context, C100/C300 timing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_agent.domain.v05_readout import (
    V05HistoryAccess,
    V05ReadoutManifest,
    V05ReadoutTrack,
    map_v05_history_access,
)
from novel_agent.domain.writer_context import BenchmarkInformationProfile
from novel_agent.services.v05_readout_manifest import (
    V05ReadoutManifestError,
    compile_v05_readout_manifest,
    load_v05_readout_manifest,
)

QA_COUNTS = {
    20: 4,
    40: 4,
    60: 4,
    80: 4,
    95: 4,
    120: 4,
    140: 3,
    160: 3,
    180: 3,
    200: 3,
    220: 3,
    240: 3,
    260: 3,
    280: 3,
    300: 3,
}
CONTEXT_WINDOWS = {
    "20": (21, 25),
    "40": (41, 45),
    "60": (61, 65),
    "80": (81, 85),
    "95": (96, 100),
    "100": (101, 120),
    "120": (121, 140),
    "140": (141, 160),
    "160": (161, 180),
    "180": (181, 200),
    "200": (201, 220),
    "220": (221, 240),
    "240": (241, 260),
    "260": (261, 280),
    "280": (281, 300),
}


def _checkpoints() -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for chapter in (
        20,
        40,
        60,
        80,
        95,
        100,
        120,
        140,
        160,
        180,
        200,
        220,
        240,
        260,
        280,
        300,
    ):
        tracks = ["novelmem_qa", "novelmem_context"]
        if chapter == 100:
            tracks = ["novelmem_context"]
        elif chapter == 300:
            tracks = ["novelmem_qa"]
        rows.append(
            {
                "checkpoint_id": f"ZTJ-C{chapter:03d}",
                "after_chapter": chapter,
                "tracks": tracks,
            }
        )
    return tuple(rows)


def _questions() -> tuple[dict[str, object], ...]:
    questions: list[dict[str, object]] = []
    for chapter, count in QA_COUNTS.items():
        for index in range(1, count + 1):
            questions.append(
                {
                    "question_id": f"ZTJ-C{chapter:03d}-Q{index:03d}",
                    "checkpoint": chapter,
                }
            )
    return tuple(questions)


def _compile() -> V05ReadoutManifest:
    return compile_v05_readout_manifest(
        benchmark_id="novelmem-eval-ztj",
        version="0.5-seed.2",
        checkpoints=_checkpoints(),
        qa_questions=_questions(),
        context_windows=CONTEXT_WINDOWS,
    )


def test_history_only_maps_to_invisible_author_plan_profile() -> None:
    assert map_v05_history_access("history_only") is BenchmarkInformationProfile.VISIBLE_AT_CUTOFF
    assert (
        map_v05_history_access("author_plan_conditioned")
        is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
    )


def test_v05_manifest_has_unique_51_qa_and_30_context_identities() -> None:
    manifest = _compile()
    qa = tuple(task for task in manifest.tasks if task.track is V05ReadoutTrack.QA)
    context = tuple(task for task in manifest.tasks if task.track is V05ReadoutTrack.CONTEXT)
    assert len(qa) == 51
    assert len(context) == 30
    assert len({task.task_id for task in manifest.tasks}) == 81
    assert not any(task.checkpoint_chapter == 100 for task in qa)
    assert not any(task.checkpoint_chapter == 300 for task in context)
    profiles = {task.history_access for task in context}
    assert profiles == {
        V05HistoryAccess.HISTORY_ONLY,
        V05HistoryAccess.AUTHOR_PLAN_CONDITIONED,
    }
    assert all(task.question_release == "after_checkpoint_freeze" for task in qa)
    assert all(task.plan_release == "after_checkpoint_freeze" for task in context)


def test_v05_manifest_rejects_qa_identity_on_c100() -> None:
    questions = (*_questions(), {"question_id": "ZTJ-C100-Q001", "checkpoint": 100})
    checkpoints = []
    for item in _checkpoints():
        if item["after_chapter"] == 100:
            checkpoints.append({**item, "tracks": ["novelmem_qa", "novelmem_context"]})
        else:
            checkpoints.append(item)
    with pytest.raises(V05ReadoutManifestError, match="C100"):
        compile_v05_readout_manifest(
            benchmark_id="novelmem-eval-ztj",
            version="0.5-seed.2",
            checkpoints=checkpoints,
            qa_questions=questions,
            context_windows=CONTEXT_WINDOWS,
        )


def test_v05_manifest_rejects_context_identity_on_c300() -> None:
    windows = dict(CONTEXT_WINDOWS)
    windows["300"] = (301, 305)
    checkpoints = []
    for item in _checkpoints():
        if item["after_chapter"] == 300:
            checkpoints.append({**item, "tracks": ["novelmem_qa", "novelmem_context"]})
        else:
            checkpoints.append(item)
    with pytest.raises(V05ReadoutManifestError, match="C300"):
        compile_v05_readout_manifest(
            benchmark_id="novelmem-eval-ztj",
            version="0.5-seed.2",
            checkpoints=checkpoints,
            qa_questions=_questions(),
            context_windows=windows,
        )


def test_optional_private_bundle_identities_match_seed_invariants() -> None:
    bundle = Path("benchmarks/private/ztj_novelmem_v0.5")
    if not (bundle / "benchmark.json").is_file():
        pytest.skip("private V0.5 bundle is not present in this worktree")
    manifest = load_v05_readout_manifest(bundle)
    qa = tuple(task for task in manifest.tasks if task.track is V05ReadoutTrack.QA)
    context = tuple(task for task in manifest.tasks if task.track is V05ReadoutTrack.CONTEXT)
    assert len(qa) == 51
    assert len(context) == 30


def test_v05_manifest_rejects_missing_questions_and_windows() -> None:
    with pytest.raises(V05ReadoutManifestError, match="no question identities"):
        compile_v05_readout_manifest(
            benchmark_id="novelmem-eval-ztj",
            version="0.5-seed.2",
            checkpoints=(
                {
                    "checkpoint_id": "ZTJ-C020",
                    "after_chapter": 20,
                    "tracks": ["novelmem_qa"],
                },
            ),
            qa_questions=(),
            context_windows={},
            require_v05_seed_invariants=False,
        )
    with pytest.raises(V05ReadoutManifestError, match="target window"):
        compile_v05_readout_manifest(
            benchmark_id="novelmem-eval-ztj",
            version="0.5-seed.2",
            checkpoints=(
                {
                    "checkpoint_id": "ZTJ-C020",
                    "after_chapter": 20,
                    "tracks": ["novelmem_context"],
                },
            ),
            qa_questions=(),
            context_windows={"20": (21,)},
            require_v05_seed_invariants=False,
        )


def test_v05_seed_invariants_reject_wrong_counts_and_future_targets() -> None:
    with pytest.raises(V05ReadoutManifestError, match="unique QA identities"):
        compile_v05_readout_manifest(
            benchmark_id="novelmem-eval-ztj",
            version="0.5-seed.2",
            checkpoints=_checkpoints(),
            qa_questions=_questions()[:-1],
            context_windows=CONTEXT_WINDOWS,
        )
    windows = dict(CONTEXT_WINDOWS)
    windows["20"] = (21, 301)
    with pytest.raises(V05ReadoutManifestError, match="after C300"):
        compile_v05_readout_manifest(
            benchmark_id="novelmem-eval-ztj",
            version="0.5-seed.2",
            checkpoints=_checkpoints(),
            qa_questions=_questions(),
            context_windows=windows,
        )


def test_v05_readout_manifest_schema_is_exported() -> None:
    schema_path = (
        Path(__file__).parents[2] / "schemas" / "stage2" / "V05ReadoutManifest.schema.json"
    )
    assert json.loads(schema_path.read_text()) == V05ReadoutManifest.model_json_schema()
