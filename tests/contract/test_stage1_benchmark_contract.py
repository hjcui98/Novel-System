from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from novel_agent.domain.benchmark import (
    BenchmarkBundle,
    BenchmarkCaseManifest,
    ChapterDocument,
    ChapterGoal,
    GoldItem,
    PlanRootDocument,
    SceneDocument,
    TextRootDocument,
)
from novel_agent.domain.changes import ExtractionRule
from novel_agent.domain.memory import FreshnessDecision, FreshnessRequest
from novel_agent.domain.replay import ContinuousReplayResult, ReplayChapterResult
from novel_agent.domain.text import QuoteHash
from novel_agent.services.benchmark_importer import (
    BenchmarkBundleImporter,
    BenchmarkImportError,
    bundle_content_id,
    text_root_content_id,
)
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def _write_bundle(path: Path, bundle: BenchmarkBundle) -> None:
    path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")


def test_synthetic_20_to_3_bundle_round_trips_and_validates(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    path = tmp_path / "bundle.json"
    _write_bundle(path, bundle)

    restored = BenchmarkBundleImporter().load(path)

    assert restored == bundle
    assert restored.case_manifests[0].history_range == (1, 20)
    assert restored.case_manifests[0].target_range == (21, 23)
    evidence = restored.case_manifests[0].observed_use_gold[0].future_evidence_refs[0]
    assert evidence.span is not None
    assert evidence.span.end - evidence.span.start == len("旧誓言")


def test_import_rejects_bundle_content_tampering(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    raw = json.loads(bundle.model_dump_json())
    raw["expected_profiles"] = ["tampered-profile"]
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(BenchmarkImportError, match="content hash mismatch"):
        BenchmarkBundleImporter().load(path)


def test_import_rejects_target_chapter_leakage_in_history(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    history, future = bundle.text_roots
    provisional_history = history.model_copy(
        update={"chapters": (*history.chapters, future.chapters[0])}
    )
    leaking_history = provisional_history.model_copy(
        update={"root_hash": text_root_content_id(provisional_history)}
    )
    case = bundle.case_manifests[0].model_copy(
        update={
            "input_text_root": leaking_history.root_hash,
            "input_summary_root": None,
            "gate_eligible": False,
        }
    )
    provisional = bundle.model_copy(
        update={
            "text_roots": (leaking_history, future),
            "summary_roots": (),
            "case_manifests": (case,),
        }
    )
    leaking_bundle = provisional.model_copy(update={"content_hash": bundle_content_id(provisional)})
    path = tmp_path / "leaking.json"
    _write_bundle(path, leaking_bundle)

    with pytest.raises(BenchmarkImportError, match="history root leaks target chapters"):
        BenchmarkBundleImporter().load(path)


def test_import_rejects_wrong_quote_hash_after_valid_bundle_rehash(tmp_path: Path) -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    gold = case.observed_use_gold[0]
    evidence = gold.evidence_refs[0].model_copy(
        update={"quote_hash": QuoteHash("sha256:" + "f" * 64)}
    )
    changed_gold = gold.model_copy(update={"evidence_refs": (evidence,)})
    changed_case = case.model_copy(update={"observed_use_gold": (changed_gold,)})
    provisional = bundle.model_copy(update={"case_manifests": (changed_case,)})
    changed_bundle = provisional.model_copy(update={"content_hash": bundle_content_id(provisional)})
    path = tmp_path / "bad-evidence.json"
    _write_bundle(path, changed_bundle)

    with pytest.raises(BenchmarkImportError, match="quote hash mismatch"):
        BenchmarkBundleImporter().load(path)


def test_gate_eligible_manifest_requires_each_gold_class() -> None:
    case = make_synthetic_bundle().case_manifests[0]
    with pytest.raises(ValidationError, match="requires observed_use gold"):
        BenchmarkCaseManifest.model_validate(
            {**case.model_dump(), "observed_use_gold": ()}, strict=True
        )


def test_checked_in_stage1_schemas_match_models() -> None:
    schema_directory = Path(__file__).parents[2] / "schemas" / "stage1"
    models = (
        BenchmarkBundle,
        BenchmarkCaseManifest,
        ChapterDocument,
        ChapterGoal,
        GoldItem,
        PlanRootDocument,
        SceneDocument,
        TextRootDocument,
        ExtractionRule,
        FreshnessDecision,
        FreshnessRequest,
        ContinuousReplayResult,
        ReplayChapterResult,
    )
    for model in models:
        actual = json.loads(
            (schema_directory / f"{model.__name__}.schema.json").read_text(encoding="utf-8")
        )
        assert actual == model.model_json_schema()


def test_checked_in_stage1_memory_schemas_match_models() -> None:
    # Stage 1 memory-contract schemas embed NeedCompletionSpec (including
    # predicates_by_facet) in Need, HorizonNeedSet and ContextPackage.  These
    # schemas use additionalProperties=false, so a stale export would reject
    # new-code Needs at the Stage 1 JSON contract boundary.
    schema_directory = Path(__file__).parents[2] / "schemas" / "stage1"
    from novel_agent.domain.memory import (
        HorizonNeedSet,
        NeedCompletionSpec,
        Stage1ContextPackage,
        Stage1MemoryNeed,
    )

    for model in (
        NeedCompletionSpec,
        Stage1MemoryNeed,
        HorizonNeedSet,
        Stage1ContextPackage,
    ):
        actual = json.loads(
            (schema_directory / f"{model.__name__}.schema.json").read_text(encoding="utf-8")
        )
        assert actual == model.model_json_schema()
    # The facet-level predicate binding must be present in the exported
    # contracts that would otherwise reject new-code Needs.
    spec_schema = json.loads(
        (schema_directory / "NeedCompletionSpec.schema.json").read_text(encoding="utf-8")
    )
    assert "predicates_by_facet" in spec_schema["properties"]
    need_schema = json.loads(
        (schema_directory / "Stage1MemoryNeed.schema.json").read_text(encoding="utf-8")
    )
    assert "predicates_by_facet" in need_schema["$defs"]["NeedCompletionSpec"]["properties"]
