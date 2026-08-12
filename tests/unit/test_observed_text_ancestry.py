"""License-free regressions for the observed TextRoot ancestry proof (v2).

The v2 proof separates CAS artifact identity from logical TextRoot root hashes
and uses role-paired roots: the expected (Gold) side must be the case-local
compiled historical root while the actual (Ledger) side must be the checkpoint
root or a proven single-parent ancestor.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import GoldItem, GoldKind
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
from novel_agent.domain.writer_context import EvidenceLedger, EvidenceLedgerEntry
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatcher
from novel_agent.services.observed_text_ancestry import (
    ObservedTextAncestryProof,
    TextRootAncestryEntry,
    canonical_source_key,
)

ROOT_REPLAY = ArtifactId("sha256:" + "1" * 64)
ROOT_ANCESTOR = ArtifactId("sha256:" + "2" * 64)
ROOT_COMPILED = ArtifactId("sha256:" + "3" * 64)
ROOT_FOREIGN = ArtifactId("sha256:" + "4" * 64)
OBJECT_A = ArtifactId("sha256:" + "a" * 64)
OBJECT_B = ArtifactId("sha256:" + "b" * 64)
COMMIT = CommitId("sha256:" + "c" * 64)
ANCESTOR_COMMIT = CommitId("sha256:" + "d" * 64)


def _ref(
    *,
    root: ArtifactId,
    evidence_id: str = "evidence.x.1",
    object_hash: ArtifactId = OBJECT_A,
    chapter_id: str = "chapter.case.5",
    scene_id: str = "scene.case.5.0",
    start: int = 10,
    end: int = 40,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=StableId(evidence_id),
        root_hash=root,
        object_hash=object_hash,
        chapter_id=StableId(chapter_id),
        scene_id=StableId(scene_id),
        span=TextSpanRef(block_id=StableId("block.case.5.0"), start=start, end=end),
        quote_hash=None,
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=COMMIT,
    )


def _gold(ref: EvidenceRef, gold_id: str = "G1") -> GoldItem:
    return GoldItem(
        gold_id=StableId(gold_id),
        kind=GoldKind.OBSERVED_USE,
        description="a reviewed fact",
        fact="a reviewed fact",
        future_evidence_refs=(ref,),
        applicable_profiles=(BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,),
        evidence_refs=(ref,),
        target_chapters=(21,),
    )


def _ledger(refs: tuple[EvidenceRef, ...]) -> EvidenceLedger:
    return EvidenceLedger(
        contract_version="test.v1",
        entries=tuple(
            EvidenceLedgerEntry(
                ledger_id=StableId(f"ledger.test.{index}"),
                evidence_refs=(ref,),
                claim_excerpt="claim",
                source_commit=COMMIT,
                information_scope="cutoff_safe",
            )
            for index, ref in enumerate(refs)
        ),
        rendered_tokens=0,
    )


def _ref_of(root: ArtifactId) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=root,
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )


def _proof(
    *,
    ancestry_roots: tuple[ArtifactId, ...] = (ROOT_ANCESTOR,),
    compiled_root: ArtifactId = ROOT_COMPILED,
) -> ObservedTextAncestryProof:
    """Build a valid proof whose first ancestry entry binds the checkpoint."""
    checkpoint_entry = TextRootAncestryEntry(
        commit_id=COMMIT,
        text_root_ref=_ref_of(ROOT_REPLAY),
        text_root_logical_hash=ROOT_REPLAY,
    )
    ancestor_entries = tuple(
        TextRootAncestryEntry(
            commit_id=CommitId("sha256:" + f"{index + 2:064x}"),
            text_root_ref=_ref_of(root),
            text_root_logical_hash=root,
        )
        for index, root in enumerate(ancestry_roots)
    )
    return ObservedTextAncestryProof.build(
        benchmark_content_hash=ArtifactId("sha256:" + "9" * 64),
        case_id=StableId("case.ZTJ-TEST"),
        profile="author_plan_conditioned",
        checkpoint_chapter=20,
        checkpoint_commit=COMMIT,
        checkpoint_text_root_ref=_ref_of(ROOT_REPLAY),
        checkpoint_text_root_hash=ROOT_REPLAY,
        ancestry=(checkpoint_entry, *ancestor_entries),
        case_input_text_root_hash=compiled_root,
    )


def test_same_root_exact_evidence_id_matches() -> None:
    gold_ref = _ref(root=ROOT_REPLAY, evidence_id="evidence.same.1")
    actual = _ref(root=ROOT_REPLAY, evidence_id="evidence.same.1")
    matcher = GoldEvidenceMatcher(ancestry_proof=_proof())
    result = matcher.match(_gold(gold_ref), _ledger((actual,)))
    assert result.matched is True


def _real_text_root() -> tuple[Any, Any]:
    """Two concrete TextRoots with identical block content but different roots.

    Returns (compiled_root, replay_root) both built from the synthetic bundle
    history chapters so EvidenceRefs validate against their own root.
    """
    from tests.fixtures.stage1_synthetic import make_synthetic_bundle

    bundle = make_synthetic_bundle()
    history = bundle.text_roots[0]
    compiled = history.model_copy(update={"root_hash": ROOT_COMPILED})
    replay = history.model_copy(update={"root_hash": ROOT_REPLAY})
    return compiled, replay


def _real_ref(
    text_root: Any,
    *,
    start: int,
    end: int,
    evidence_id: str,
) -> EvidenceRef:
    """Build an EvidenceRef that validates against the given TextRoot."""
    from novel_agent.domain.text import TextSpanRef
    from novel_agent.services.content_addressing import quote_hash

    block = text_root.chapters[0].scenes[0].blocks[0]
    selected = block.text[start:end]
    return EvidenceRef(
        evidence_id=StableId(evidence_id),
        root_hash=text_root.root_hash,
        object_hash=ArtifactId("sha256:" + hashlib.sha256(block.text.encode("utf-8")).hexdigest()),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=TextSpanRef(block_id=block.block_id, start=start, end=end),
        quote_hash=quote_hash(selected),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=COMMIT,
    )


def _matcher_with_roots(proof: ObservedTextAncestryProof) -> GoldEvidenceMatcher:
    compiled, replay = _real_text_root()
    return GoldEvidenceMatcher(
        ancestry_proof=proof,
        text_roots={
            proof.case_input_text_root_hash: compiled,
            ROOT_REPLAY: replay,
            ROOT_ANCESTOR: replay,
        },
    )


def test_case_compiled_root_matches_replay_root_with_proof() -> None:
    compiled, replay = _real_text_root()
    gold_ref = _real_ref(
        compiled,
        start=0,
        end=min(20, len(compiled.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.comp.1",
    )
    actual = _real_ref(
        replay,
        start=0,
        end=min(20, len(replay.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.rec.1",
    )
    matcher = _matcher_with_roots(_proof())
    result = matcher.match(_gold(gold_ref), _ledger((actual,)))
    assert result.matched is True


def test_case_compiled_root_matches_ancestor_root_with_proof() -> None:
    compiled, replay = _real_text_root()
    gold_ref = _real_ref(
        compiled,
        start=0,
        end=min(20, len(compiled.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.comp.1",
    )
    actual = _real_ref(
        replay,
        start=0,
        end=min(20, len(replay.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.rec.1",
    )
    matcher = GoldEvidenceMatcher(
        ancestry_proof=_proof(ancestry_roots=(ROOT_ANCESTOR,)),
        text_roots={
            ROOT_COMPILED: compiled,
            ROOT_REPLAY: replay,
            ROOT_ANCESTOR: replay,
        },
    )
    result = matcher.match(_gold(gold_ref), _ledger((actual,)))
    assert result.matched is True


def test_same_object_bytes_without_proof_still_rejected() -> None:
    compiled, replay = _real_text_root()
    gold_ref = _real_ref(
        compiled,
        start=0,
        end=min(20, len(compiled.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.comp.1",
    )
    actual = _real_ref(
        replay,
        start=0,
        end=min(20, len(replay.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.rec.1",
    )
    matcher = GoldEvidenceMatcher(ancestry_proof=None)
    result = matcher.match(_gold(gold_ref), _ledger((actual,)))
    assert result.matched is False
    assert result.matched_ledger_ids == ()


def test_foreign_expected_root_rejected() -> None:
    gold_ref = _ref(root=ROOT_FOREIGN, evidence_id="evidence.foreign.1")
    actual = _ref(root=ROOT_REPLAY, evidence_id="evidence.rec.1")
    matcher = GoldEvidenceMatcher(ancestry_proof=_proof())
    result = matcher.match(_gold(gold_ref), _ledger((actual,)))
    assert result.matched is False


def test_non_ancestry_actual_root_rejected() -> None:
    gold_ref = _ref(root=ROOT_COMPILED, evidence_id="evidence.comp.1")
    actual = _ref(root=ROOT_FOREIGN, evidence_id="evidence.rec.1")
    matcher = GoldEvidenceMatcher(ancestry_proof=_proof())
    result = matcher.match(_gold(gold_ref), _ledger((actual,)))
    assert result.matched is False


def test_compiled_root_cannot_be_actual_side() -> None:
    gold_ref = _ref(root=ROOT_COMPILED, evidence_id="evidence.comp.1")
    actual = _ref(root=ROOT_COMPILED, evidence_id="evidence.rec.1")
    matcher = GoldEvidenceMatcher(ancestry_proof=_proof())
    result = matcher.match(_gold(gold_ref), _ledger((actual,)))
    assert result.matched is True  # same root exact path still valid


def test_chapter_scene_mismatch_rejected_with_proof() -> None:
    gold_ref = _ref(
        root=ROOT_COMPILED,
        evidence_id="evidence.comp.1",
        chapter_id="chapter.case.6",
        scene_id="scene.case.6.0",
    )
    actual = _ref(root=ROOT_REPLAY, evidence_id="evidence.rec.1")
    matcher = GoldEvidenceMatcher(ancestry_proof=_proof())
    result = matcher.match(_gold(gold_ref), _ledger((actual,)))
    assert result.matched is False


def test_object_hash_mismatch_rejected_with_proof() -> None:
    gold_ref = _ref(root=ROOT_COMPILED, evidence_id="evidence.comp.1", object_hash=OBJECT_A)
    actual = _ref(root=ROOT_REPLAY, evidence_id="evidence.rec.1", object_hash=OBJECT_B)
    matcher = GoldEvidenceMatcher(ancestry_proof=_proof())
    result = matcher.match(_gold(gold_ref), _ledger((actual,)))
    assert result.matched is False


def test_disjoint_spans_rejected_with_proof() -> None:
    gold_ref = _ref(root=ROOT_COMPILED, evidence_id="evidence.comp.1", start=0, end=10)
    actual = _ref(root=ROOT_REPLAY, evidence_id="evidence.rec.1", start=50, end=90)
    matcher = GoldEvidenceMatcher(ancestry_proof=_proof())
    result = matcher.match(_gold(gold_ref), _ledger((actual,)))
    assert result.matched is False


def test_broad_gold_span_contains_precise_actual_span() -> None:
    compiled, replay = _real_text_root()
    block_len = len(compiled.chapters[0].scenes[0].blocks[0].text)
    gold_ref = _real_ref(compiled, start=0, end=block_len, evidence_id="evidence.comp.1")
    actual = _real_ref(
        replay,
        start=min(10, block_len),
        end=min(40, block_len),
        evidence_id="evidence.rec.1",
    )
    matcher = _matcher_with_roots(_proof())
    result = matcher.match(_gold(gold_ref), _ledger((actual,)))
    assert result.matched is True


def test_proof_build_rejects_unresolvable_checkpoint_ref() -> None:
    with pytest.raises(ValueError, match="not resolvable"):
        ObservedTextAncestryProof.build(
            benchmark_content_hash=ArtifactId("sha256:" + "9" * 64),
            case_id=StableId("case.ZTJ-TEST"),
            profile="author_plan_conditioned",
            checkpoint_chapter=20,
            checkpoint_commit=COMMIT,
            checkpoint_text_root_ref=ArtifactRef(
                artifact_id=ArtifactId("sha256:" + "0" * 64),
                media_type="application/json",
                byte_length=1,
                schema_version=SchemaVersion("1.0.0"),
            ),
            checkpoint_text_root_hash=ROOT_REPLAY,
            ancestry=(),
            case_input_text_root_hash=ROOT_COMPILED,
        )


def test_proof_build_rejects_duplicate_ancestry_commits() -> None:
    with pytest.raises(ValueError, match="linear without repeats"):
        ObservedTextAncestryProof.build(
            benchmark_content_hash=ArtifactId("sha256:" + "9" * 64),
            case_id=StableId("case.ZTJ-TEST"),
            profile="author_plan_conditioned",
            checkpoint_chapter=20,
            checkpoint_commit=COMMIT,
            checkpoint_text_root_ref=_ref_of(ROOT_REPLAY),
            checkpoint_text_root_hash=ROOT_REPLAY,
            ancestry=(
                TextRootAncestryEntry(
                    commit_id=ANCESTOR_COMMIT,
                    text_root_ref=_ref_of(ROOT_ANCESTOR),
                    text_root_logical_hash=ROOT_ANCESTOR,
                ),
                TextRootAncestryEntry(
                    commit_id=ANCESTOR_COMMIT,
                    text_root_ref=_ref_of(ROOT_ANCESTOR),
                    text_root_logical_hash=ROOT_ANCESTOR,
                ),
            ),
            case_input_text_root_hash=ROOT_COMPILED,
        )


def test_proof_roles_expected_is_compiled_only() -> None:
    proof = _proof()
    assert proof.allows_expected(ROOT_COMPILED) is True
    assert proof.allows_expected(ROOT_REPLAY) is False
    assert proof.allows_expected(ROOT_ANCESTOR) is False
    assert proof.allows_expected(ROOT_FOREIGN) is False


def test_proof_roles_actual_is_checkpoint_or_ancestry() -> None:
    proof = _proof()
    assert proof.allows_actual(ROOT_REPLAY) is True
    assert proof.allows_actual(ROOT_ANCESTOR) is True
    assert proof.allows_actual(ROOT_COMPILED) is False
    assert proof.allows_actual(ROOT_FOREIGN) is False


def test_proof_hash_is_stable_and_content_addressed() -> None:
    proof_a = _proof()
    proof_b = _proof()
    assert proof_a.proof_hash == proof_b.proof_hash
    assert proof_a.proof_hash.root != "sha256:" + "0" * 64


def test_canonical_source_key_requires_chapter_and_scene() -> None:
    no_ids = _ref(root=ROOT_REPLAY, evidence_id="e1")
    no_ids = no_ids.model_copy(update={"chapter_id": None, "scene_id": None})
    assert canonical_source_key(no_ids) is None
    no_scene = no_ids.model_copy(
        update={"chapter_id": StableId("chapter.case.5"), "scene_id": None}
    )
    assert canonical_source_key(no_scene) is None


def test_prelude_namespaces_normalize_to_sentinel() -> None:
    p001 = _ref(
        root=ROOT_COMPILED,
        evidence_id="e-p1",
        chapter_id="prelude.ZTJ-P001",
        scene_id="scene.ZTJ-P001.prelude.0",
    )
    p005 = _ref(
        root=ROOT_REPLAY,
        evidence_id="e-p5",
        chapter_id="prelude.ZTJ-P005",
        scene_id="scene.ZTJ-P005.prelude.0",
    )
    assert canonical_source_key(p001) == canonical_source_key(p005)
    assert canonical_source_key(p001) == (0, 0, OBJECT_A)


def test_numbered_chapter_scene_namespace_normalization() -> None:
    left = _ref(
        root=ROOT_COMPILED,
        evidence_id="e-n1",
        chapter_id="chapter.ZTJ-P005.21",
        scene_id="scene.ZTJ-P005.21.3",
    )
    right = _ref(
        root=ROOT_REPLAY,
        evidence_id="e-n2",
        chapter_id="chapter.ZTJ-P005.21",
        scene_id="scene.ZTJ-P005.21.3",
    )
    assert canonical_source_key(left) == canonical_source_key(right)
    assert canonical_source_key(left) == (21, 3, OBJECT_A)


def test_case_namespace_never_participates_in_key() -> None:
    left = _ref(
        root=ROOT_COMPILED,
        evidence_id="e-c1",
        chapter_id="chapter.ZTJ-P001.7",
        scene_id="scene.ZTJ-P001.7.1",
    )
    right = _ref(
        root=ROOT_REPLAY,
        evidence_id="e-c2",
        chapter_id="chapter.ZTJ-P005.7",
        scene_id="scene.ZTJ-P005.7.1",
    )
    assert canonical_source_key(left) == canonical_source_key(right)


def test_validates_evidence_refs_fails_closed_on_unlocatable() -> None:
    proof = _proof()
    bare = _ref(root=ROOT_COMPILED, evidence_id="e1")
    bare = bare.model_copy(update={"chapter_id": None, "scene_id": None})
    regular = _ref(root=ROOT_REPLAY, evidence_id="e2")
    assert proof.validates_evidence_refs(bare, regular) is False
    assert proof.validates_evidence_refs(regular, bare) is False


def test_span_overlaps_fails_closed_without_span() -> None:
    proof = _proof()
    no_span = _ref(root=ROOT_REPLAY, evidence_id="e1").model_copy(update={"span": None})
    regular = _ref(root=ROOT_COMPILED, evidence_id="e2")
    assert proof.span_overlaps(regular, no_span, minimum_span_coverage=0.5) is False
    assert proof.span_overlaps(no_span, regular, minimum_span_coverage=0.5) is False


def test_canonical_source_key_rejects_chapter_scene_mismatch() -> None:
    chapter_5 = _ref(
        root=ROOT_COMPILED,
        evidence_id="e1",
        chapter_id="chapter.ZTJ-P005.5",
        scene_id="scene.ZTJ-P005.5.0",
    )
    scene_7 = chapter_5.model_copy(
        update={
            "chapter_id": StableId("chapter.ZTJ-P005.5"),
            "scene_id": StableId("scene.ZTJ-P005.7.0"),
        }
    )
    assert canonical_source_key(chapter_5) != canonical_source_key(scene_7)


def test_canonical_source_key_prelude_without_scene_match() -> None:
    ref = _ref(
        root=ROOT_COMPILED,
        evidence_id="e1",
        chapter_id="prelude.ZTJ-P001",
        scene_id="scene.ZTJ-P001.prelude.3",
    )
    assert canonical_source_key(ref) == (0, 3, OBJECT_A)
    scene_only = _ref(
        root=ROOT_COMPILED,
        evidence_id="e2",
        chapter_id="prelude.ZTJ-P001.0",
        scene_id="scene.ZTJ-P001.prelude.0",
    )
    assert canonical_source_key(scene_only) == (0, 0, OBJECT_A)


def test_canonical_source_key_rejects_unparseable_ids() -> None:
    ref = _ref(
        root=ROOT_COMPILED,
        evidence_id="e1",
        chapter_id="chapter.unknown",
        scene_id="scene.unknown",
    )
    assert canonical_source_key(ref) is None
    chapter_only = _ref(
        root=ROOT_COMPILED,
        evidence_id="e2",
        chapter_id="chapter.ZTJ-P005.21",
        scene_id="not-a-scene-id",
    )
    assert canonical_source_key(chapter_only) is None


def test_chapter_locator_without_scene_component() -> None:
    ref = _ref(
        root=ROOT_COMPILED,
        evidence_id="e1",
        chapter_id="chapter.ZTJ-P005.21",
        scene_id="scene.ZTJ-P005.21",
    )
    assert canonical_source_key(ref) is None


def test_forged_block_id_rejected_by_concrete_validator() -> None:
    compiled, replay = _real_text_root()
    gold_ref = _real_ref(
        compiled,
        start=0,
        end=min(20, len(compiled.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.comp.1",
    )
    forged = _real_ref(
        replay,
        start=0,
        end=min(20, len(replay.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.forged.1",
    ).model_copy(
        update={"span": TextSpanRef(block_id=StableId("block.does-not-exist"), start=0, end=5)}
    )
    matcher = _matcher_with_roots(_proof())
    result = matcher.match(_gold(gold_ref), _ledger((forged,)))
    assert result.matched is False


def test_forged_object_hash_rejected_by_concrete_validator() -> None:
    compiled, replay = _real_text_root()
    gold_ref = _real_ref(
        compiled,
        start=0,
        end=min(20, len(compiled.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.comp.1",
    )
    forged = _real_ref(
        replay,
        start=0,
        end=min(20, len(replay.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.forged.1",
    ).model_copy(update={"object_hash": ArtifactId("sha256:" + "f" * 64)})
    matcher = _matcher_with_roots(_proof())
    result = matcher.match(_gold(gold_ref), _ledger((forged,)))
    assert result.matched is False


def test_forged_quote_hash_rejected_by_concrete_validator() -> None:
    compiled, replay = _real_text_root()
    gold_ref = _real_ref(
        compiled,
        start=0,
        end=min(20, len(compiled.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.comp.1",
    )
    forged = _real_ref(
        replay,
        start=0,
        end=min(20, len(replay.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.forged.1",
    ).model_copy(update={"quote_hash": None})
    matcher = _matcher_with_roots(_proof())
    result = matcher.match(_gold(gold_ref), _ledger((forged,)))
    assert result.matched is False


def test_forged_span_bounds_rejected_by_concrete_validator() -> None:
    compiled, replay = _real_text_root()
    gold_ref = _real_ref(
        compiled,
        start=0,
        end=min(20, len(compiled.chapters[0].scenes[0].blocks[0].text)),
        evidence_id="evidence.comp.1",
    )
    block_len = len(replay.chapters[0].scenes[0].blocks[0].text)
    forged = _real_ref(
        replay,
        start=0,
        end=min(20, block_len),
        evidence_id="evidence.forged.1",
    ).model_copy(
        update={
            "span": TextSpanRef(
                block_id=replay.chapters[0].scenes[0].blocks[0].block_id,
                start=0,
                end=block_len + 100,
            )
        }
    )
    matcher = _matcher_with_roots(_proof())
    result = matcher.match(_gold(gold_ref), _ledger((forged,)))
    assert result.matched is False


def test_proof_build_rejects_checkpoint_commit_mismatch() -> None:
    with pytest.raises(ValueError, match="checkpoint commit must equal"):
        ObservedTextAncestryProof.build(
            benchmark_content_hash=ArtifactId("sha256:" + "9" * 64),
            case_id=StableId("case.ZTJ-TEST"),
            profile="author_plan_conditioned",
            checkpoint_chapter=20,
            checkpoint_commit=COMMIT,
            checkpoint_text_root_ref=_ref_of(ROOT_REPLAY),
            checkpoint_text_root_hash=ROOT_REPLAY,
            ancestry=(
                TextRootAncestryEntry(
                    commit_id=ANCESTOR_COMMIT,
                    text_root_ref=_ref_of(ROOT_REPLAY),
                    text_root_logical_hash=ROOT_REPLAY,
                ),
            ),
            case_input_text_root_hash=ROOT_COMPILED,
        )


def test_proof_build_rejects_checkpoint_ref_mismatch() -> None:
    with pytest.raises(ValueError, match="checkpoint TextRoot artifact ref must equal"):
        ObservedTextAncestryProof.build(
            benchmark_content_hash=ArtifactId("sha256:" + "9" * 64),
            case_id=StableId("case.ZTJ-TEST"),
            profile="author_plan_conditioned",
            checkpoint_chapter=20,
            checkpoint_commit=COMMIT,
            checkpoint_text_root_ref=_ref_of(ROOT_REPLAY),
            checkpoint_text_root_hash=ROOT_REPLAY,
            ancestry=(
                TextRootAncestryEntry(
                    commit_id=COMMIT,
                    text_root_ref=_ref_of(ROOT_ANCESTOR),
                    text_root_logical_hash=ROOT_REPLAY,
                ),
            ),
            case_input_text_root_hash=ROOT_COMPILED,
        )


def test_proof_build_rejects_checkpoint_hash_mismatch() -> None:
    with pytest.raises(ValueError, match="checkpoint TextRoot logical hash must equal"):
        ObservedTextAncestryProof.build(
            benchmark_content_hash=ArtifactId("sha256:" + "9" * 64),
            case_id=StableId("case.ZTJ-TEST"),
            profile="author_plan_conditioned",
            checkpoint_chapter=20,
            checkpoint_commit=COMMIT,
            checkpoint_text_root_ref=_ref_of(ROOT_REPLAY),
            checkpoint_text_root_hash=ROOT_REPLAY,
            ancestry=(
                TextRootAncestryEntry(
                    commit_id=COMMIT,
                    text_root_ref=_ref_of(ROOT_REPLAY),
                    text_root_logical_hash=ROOT_ANCESTOR,
                ),
            ),
            case_input_text_root_hash=ROOT_COMPILED,
        )


def test_proof_build_rejects_empty_ancestry() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        ObservedTextAncestryProof.build(
            benchmark_content_hash=ArtifactId("sha256:" + "9" * 64),
            case_id=StableId("case.ZTJ-TEST"),
            profile="author_plan_conditioned",
            checkpoint_chapter=20,
            checkpoint_commit=COMMIT,
            checkpoint_text_root_ref=_ref_of(ROOT_REPLAY),
            checkpoint_text_root_hash=ROOT_REPLAY,
            ancestry=(),
            case_input_text_root_hash=ROOT_COMPILED,
        )
