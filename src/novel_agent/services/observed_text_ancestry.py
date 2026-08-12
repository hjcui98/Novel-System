"""Observed TextRoot ancestry proof for cross-root Gold evidence matching.

A replay TextRoot is append-only: evidence records created earlier keep the
older root while the checkpoint uses a successor root, and each compiled case
carries its own case-local TextRoot namespace.  ``GoldEvidenceMatcher`` stays
fail-closed: a Gold reference and a ledger reference with different roots may
only bind after this proof establishes that both roots describe the same
canonical observed history of the current case up to the checkpoint cutoff.

The proof separates two identity layers that the v1 implementation conflated:

- the CAS artifact id of a TextRoot blob (``ArtifactRef.artifact_id``);
- the logical ``TextRootDocument.root_hash`` that EvidenceRefs carry.

It also separates the two root *roles*: the expected (Gold) side must be the
current case's compiled historical TextRoot, while the actual (Ledger) side
must be the checkpoint canonical TextRoot or one of its single-parent
ancestors.  The two namespaces are proven independently: the commit chain
proves the observed history, and the bundle content hash binds the compiled
case input root.  A symmetric ``allows()`` superset is intentionally not used.
"""

from __future__ import annotations

import re
from typing import ClassVar

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.text import EvidenceRef

_PRELUDE_ID = "prelude"
_CHAPTER_RE = re.compile(r"^chapter\.(?P<case>[^.]+)\.(?P<chapter>\d+)$")
_SCENE_RE = re.compile(r"^scene\.(?P<case>[^.]+)\.(?:(?P<chapter>\d+)|prelude)\.(?P<scene>\d+)$")


class TextRootAncestryEntry(DomainModel):
    """One commit in the single-parent canonical chain."""

    commit_id: CommitId
    text_root_ref: ArtifactRef
    text_root_logical_hash: ArtifactId


class ObservedTextAncestryProof(DomainModel):
    """Frozen proof binding one compiled case root to an observed replay chain."""

    proof_version: ClassVar[str] = "observed_text_ancestry_proof.v2"
    benchmark_content_hash: ArtifactId
    case_id: StableId
    profile: str
    checkpoint_chapter: int
    checkpoint_commit: CommitId
    checkpoint_text_root_ref: ArtifactRef
    checkpoint_text_root_hash: ArtifactId
    ancestry: tuple[TextRootAncestryEntry, ...]
    case_input_text_root_hash: ArtifactId
    proof_hash: ArtifactId

    @property
    def ancestry_logical_roots(self) -> tuple[ArtifactId, ...]:
        return tuple(
            dict.fromkeys(
                (
                    self.checkpoint_text_root_hash,
                    *(item.text_root_logical_hash for item in self.ancestry),
                )
            )
        )

    def allows_expected(self, root_hash: ArtifactId) -> bool:
        """The Gold side may only be the case-local compiled historical root."""
        return root_hash == self.case_input_text_root_hash

    def allows_actual(self, root_hash: ArtifactId) -> bool:
        """The Ledger side may only be the checkpoint or a proven ancestor root."""
        return root_hash in self.ancestry_logical_roots

    @classmethod
    def build(
        cls,
        *,
        benchmark_content_hash: ArtifactId,
        case_id: StableId,
        profile: str,
        checkpoint_chapter: int,
        checkpoint_commit: CommitId,
        checkpoint_text_root_ref: ArtifactRef,
        checkpoint_text_root_hash: ArtifactId,
        ancestry: tuple[TextRootAncestryEntry, ...],
        case_input_text_root_hash: ArtifactId,
    ) -> ObservedTextAncestryProof:
        """Construct with validated logical roots; never silently degrade."""
        if checkpoint_text_root_ref.artifact_id.root == "sha256:" + "0" * 64:
            raise ValueError("checkpoint TextRoot artifact ref is not resolvable")
        if not ancestry:
            raise ValueError("commit ancestry chain must not be empty")
        ancestry_ids = tuple(item.commit_id for item in ancestry)
        if len(ancestry_ids) != len(set(ancestry_ids)):
            raise ValueError("commit ancestry chain must be linear without repeats")
        first = ancestry[0]
        if first.commit_id != checkpoint_commit:
            raise ValueError(
                "checkpoint commit must equal the first ancestry commit: "
                f"{checkpoint_commit.root} != {first.commit_id.root}"
            )
        if first.text_root_ref.artifact_id != checkpoint_text_root_ref.artifact_id:
            raise ValueError(
                "checkpoint TextRoot artifact ref must equal the first ancestry ref: "
                f"{checkpoint_text_root_ref.artifact_id.root} != "
                f"{first.text_root_ref.artifact_id.root}"
            )
        if first.text_root_logical_hash != checkpoint_text_root_hash:
            raise ValueError(
                "checkpoint TextRoot logical hash must equal the first ancestry logical hash: "
                f"{checkpoint_text_root_hash.root} != {first.text_root_logical_hash.root}"
            )
        proof = cls(
            benchmark_content_hash=benchmark_content_hash,
            case_id=case_id,
            profile=profile,
            checkpoint_chapter=checkpoint_chapter,
            checkpoint_commit=checkpoint_commit,
            checkpoint_text_root_ref=checkpoint_text_root_ref,
            checkpoint_text_root_hash=checkpoint_text_root_hash,
            ancestry=ancestry,
            case_input_text_root_hash=case_input_text_root_hash,
            proof_hash=ArtifactId("sha256:" + "0" * 64),
        )
        proof = proof.model_copy(update={"proof_hash": cls._proof_hash(proof)})
        return proof

    @classmethod
    def _proof_hash(cls, proof: ObservedTextAncestryProof) -> ArtifactId:
        from novel_agent.services.benchmark_importer import content_id

        return content_id(proof.model_dump(mode="json"))

    def validates_evidence_refs(self, expected: EvidenceRef, actual: EvidenceRef) -> bool:
        """Both sides must be chapter/scene locatable and share a canonical block key."""
        expected_key = canonical_source_key(expected)
        actual_key = canonical_source_key(actual)
        if expected_key is None or actual_key is None:
            return False
        return expected_key == actual_key

    def span_overlaps(
        self,
        expected: EvidenceRef,
        actual: EvidenceRef,
        *,
        minimum_span_coverage: float,
    ) -> bool:
        if not self.validates_evidence_refs(expected, actual):
            return False
        if expected.span is None or actual.span is None:
            return False
        overlap = max(
            0,
            min(expected.span.end, actual.span.end) - max(expected.span.start, actual.span.start),
        )
        expected_width = max(1, expected.span.end - expected.span.start)
        actual_width = max(1, actual.span.end - actual.span.start)
        return overlap / min(expected_width, actual_width) >= minimum_span_coverage


def canonical_source_key(reference: EvidenceRef) -> tuple[int, int, ArtifactId] | None:
    """Stable block identity: chapter + scene + object hash.

    ``prelude.*`` ids normalize to the explicit prelude sentinel (0); the case
    namespace never participates in the key.  Returns None when the reference
    lacks the chapter/scene/object information needed for a cross-root key.
    """

    chapter_id = reference.chapter_id
    scene_id = reference.scene_id
    if chapter_id is None or scene_id is None:
        return None
    chapter_match = _chapter_scene_locator(chapter_id.root)
    scene_match = _chapter_scene_locator(scene_id.root)
    if chapter_match is None or scene_match is None:
        return None
    chapter, _ = chapter_match
    scene_chapter, scene = scene_match
    if scene_chapter is not None and chapter != scene_chapter:
        return None
    return chapter, scene, reference.object_hash


def _chapter_scene_locator(value: str) -> tuple[int, int] | None:
    """Return (chapter_index, scene_index); prelude ids map to sentinel 0."""
    if value.startswith(_PRELUDE_ID + "."):
        return 0, 0
    chapter_match = _CHAPTER_RE.match(value)
    if chapter_match is not None:
        return int(chapter_match.group("chapter")), 0
    scene_match = _SCENE_RE.match(value)
    if scene_match is not None:
        return int(scene_match.group("chapter") or 0), int(scene_match.group("scene"))
    return None


__all__ = [
    "ObservedTextAncestryProof",
    "TextRootAncestryEntry",
    "canonical_source_key",
]
