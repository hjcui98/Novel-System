"""WP6: EvidenceRefAuditor report generation tests."""

from __future__ import annotations

import json
from pathlib import Path

from novel_agent.domain.benchmark import (
    ChapterDocument,
    SceneDocument,
    TextBlock,
    TextRootDocument,
)
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    WorldRootDocument,
)
from novel_agent.domain.text import (
    EvidenceRef,
    EvidenceSupportStatus,
    TextSpanRef,
)
from novel_agent.domain.world import (
    Entity,
    Event,
    RelationRecord,
    StateRecord,
    StoryTime,
    TruthClass,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import quote_hash
from novel_agent.services.evidence_audit import EvidenceRefAuditor


def _text_root(text: str) -> tuple[TextRootDocument, TextBlock]:
    block = TextBlock(
        block_id=StableId("block.c1.0"),
        chapter_id=StableId("chapter.1"),
        scene_id=StableId("scene.c1.0"),
        narrative_index=0,
        text=text,
    )
    scene = SceneDocument(
        scene_id=StableId("scene.c1.0"),
        scene_index=0,
        blocks=(block,),
    )
    chapter = ChapterDocument(
        chapter_id=StableId("chapter.1"),
        chapter_index=1,
        scenes=(scene,),
    )
    root_hash = ArtifactId("sha256:" + "f" * 64)
    return (
        TextRootDocument(
            root_hash=root_hash,
            schema_version=SchemaVersion("0.1.0"),
            chapters=(chapter,),
        ),
        block,
    )


def _evidence_ref(root: TextRootDocument, block: TextBlock) -> EvidenceRef:
    selected = block.text[0:20]
    return EvidenceRef(
        evidence_id=StableId("evidence.state.confidence"),
        root_hash=root.root_hash,
        object_hash=sha256_id(block.text.encode("utf-8")),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=TextSpanRef(
            block_id=block.block_id,
            start=0,
            end=20,
        ),
        quote_hash=quote_hash(selected),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=CommitId("sha256:" + "2" * 64),
    )


def test_evidence_auditor_writes_all_report_files(tmp_path: Path) -> None:
    text = "chen shows extreme_confidence cultivation-attitude clearly."
    text_root, block = _text_root(text)
    evidence = _evidence_ref(text_root, block)
    entity = Entity(
        entity_id=StableId("entity.chen"),
        entity_type="person",
        internal_label="Chen Fan",
    )
    state = StateRecord(
        state_id=StableId("state.confidence"),
        subject_id=StableId("entity.chen"),
        predicate="cultivation-attitude",
        value="extreme_confidence",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(evidence,),
    )
    world = WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "1" * 64),
        schema_version=SchemaVersion("0.1.0"),
        source_commit=CommitId("sha256:" + "2" * 64),
        entities=(entity,),
        states=(state,),
    )
    auditor = EvidenceRefAuditor()
    findings = auditor.audit_world(world, text_root)

    assert len(findings) == 1
    report_dir = auditor.write_report(findings, tmp_path, audit_id="audit-c1-c20-smoke")

    assert report_dir == tmp_path / "audit-c1-c20-smoke"
    summary = json.loads((report_dir / "summary.json").read_text("utf-8"))
    assert summary["audit_id"] == "audit-c1-c20-smoke"
    assert summary["finding_count"] == 1
    assert (report_dir / "evidence_findings.jsonl").is_file()
    assert (report_dir / "mandatory_findings.json").is_file()
    assert (report_dir / "human_review_queue.json").is_file()
    assert (report_dir / "audit_manifest.json").is_file()

    manifest = json.loads((report_dir / "audit_manifest.json").read_text("utf-8"))
    # The manifest lists files before writing itself, so it does not include
    # audit_manifest.json.  Verify it contains the other four expected files.
    assert "summary.json" in manifest["files"]
    assert "evidence_findings.jsonl" in manifest["files"]
    assert "mandatory_findings.json" in manifest["files"]
    assert "human_review_queue.json" in manifest["files"]

    # EvidenceAuditFinding uses slots=True; verify jsonl is valid asdict output
    # and does not raise AttributeError (the original __dict__ bug).
    line = (report_dir / "evidence_findings.jsonl").read_text("utf-8").strip()
    parsed = json.loads(line)
    assert parsed["record_kind"] == "state"
    assert parsed["hard_validation"] == "pass"
    assert "risk_tags" in parsed


def _long_text_root(text: str) -> tuple[TextRootDocument, TextBlock]:
    return _text_root(text)


def _evidence_at(
    root: TextRootDocument,
    block: TextBlock,
    start: int,
    end: int,
    *,
    evidence_id: str = "evidence.test",
    bad_hash: bool = False,
) -> EvidenceRef:
    selected = block.text[start:end]
    return EvidenceRef(
        evidence_id=StableId(evidence_id),
        root_hash=root.root_hash,
        object_hash=(
            ArtifactId("sha256:" + "0" * 64) if bad_hash else sha256_id(block.text.encode("utf-8"))
        ),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=TextSpanRef(block_id=block.block_id, start=start, end=end),
        quote_hash=quote_hash(selected),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=CommitId("sha256:" + "2" * 64),
    )


def _world_with(
    *,
    states: tuple[StateRecord, ...] = (),
    relations: tuple[RelationRecord, ...] = (),
    obligations: tuple[PlanObligation, ...] = (),
    events: tuple[Event, ...] = (),
) -> WorldRootDocument:
    return WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "1" * 64),
        schema_version=SchemaVersion("0.1.0"),
        source_commit=CommitId("sha256:" + "2" * 64),
        entities=(
            Entity(
                entity_id=StableId("entity.chen"),
                entity_type="person",
                internal_label="Chen Fan",
            ),
        ),
        states=states,
        relations=relations,
        obligations=obligations,
        events=events,
    )


def test_audit_detects_range_invalid_span() -> None:
    """Line 167: span with start >= end or out of block bounds -> range_invalid."""
    text = "short text"
    root, block = _long_text_root(text)
    evidence = _evidence_at(root, block, 5, 5, evidence_id="evidence.range")
    state = StateRecord(
        state_id=StableId("state.range"),
        subject_id=StableId("entity.chen"),
        predicate="status",
        value="ok",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(evidence,),
    )
    world = _world_with(states=(state,))
    findings = EvidenceRefAuditor().audit_world(world, root)
    assert findings[0].hard_validation == "range_invalid"
    assert findings[0].severity == "high"
    assert findings[0].recommended_action == "stop_c21_p0"


def test_audit_flags_round_hundred_and_fifty_offsets() -> None:
    """Lines 171, 173: ROUND_HUNDRED_OFFSET and ROUND_FIFTY_OFFSET risk tags."""
    text = "x" * 500
    root, block = _long_text_root(text)
    hundred = _evidence_at(root, block, 100, 200, evidence_id="evidence.hundred")
    fifty = _evidence_at(root, block, 50, 150, evidence_id="evidence.fifty")
    state_h = StateRecord(
        state_id=StableId("state.hundred"),
        subject_id=StableId("entity.chen"),
        predicate="status",
        value="ok",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(hundred,),
    )
    state_f = StateRecord(
        state_id=StableId("state.fifty"),
        subject_id=StableId("entity.chen"),
        predicate="status",
        value="ok",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(fifty,),
    )
    world = _world_with(states=(state_h, state_f))
    findings = EvidenceRefAuditor().audit_world(world, root)
    tags_by_id = {f.evidence_id: f.risk_tags for f in findings}
    assert "ROUND_HUNDRED_OFFSET" in tags_by_id["evidence.hundred"]
    assert "ROUND_FIFTY_OFFSET" in tags_by_id["evidence.fifty"]


def test_audit_flags_unusually_wide_span() -> None:
    """Lines 176-178: span width > 400 -> UNUSUALLY_WIDE_SPAN, severity medium."""
    text = "status ok " * 50  # 550 chars, contains summary tokens
    root, block = _long_text_root(text)
    evidence = _evidence_at(root, block, 1, 450, evidence_id="evidence.wide")
    state = StateRecord(
        state_id=StableId("state.wide"),
        subject_id=StableId("entity.chen"),
        predicate="status",
        value="ok",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(evidence,),
    )
    world = _world_with(states=(state,))
    findings = EvidenceRefAuditor().audit_world(world, root)
    assert "UNUSUALLY_WIDE_SPAN" in findings[0].risk_tags
    assert findings[0].semantic_disposition == "supports"
    assert findings[0].severity == "medium"
    assert findings[0].recommended_action == "human_sample_review"


def test_audit_detects_identity_or_hash_failure() -> None:
    """Lines 177-178: bad object_hash -> identity_or_hash_failure, stop_c21_p0."""
    text = "chen shows extreme_confidence cultivation-attitude clearly."
    root, block = _long_text_root(text)
    evidence = _evidence_at(root, block, 0, 20, evidence_id="evidence.bad", bad_hash=True)
    state = StateRecord(
        state_id=StableId("state.bad"),
        subject_id=StableId("entity.chen"),
        predicate="cultivation-attitude",
        value="extreme_confidence",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(evidence,),
    )
    world = _world_with(states=(state,))
    findings = EvidenceRefAuditor().audit_world(world, root)
    assert findings[0].hard_validation == "identity_or_hash_failure"
    assert findings[0].severity == "high"
    assert findings[0].recommended_action == "stop_c21_p0"


def test_audit_resolves_evidence_against_historical_text_root() -> None:
    old_root, old_block = _text_root("chen shows extreme_confidence cultivation-attitude clearly.")
    evidence = _evidence_ref(old_root, old_block)
    current_root = old_root.model_copy(update={"root_hash": ArtifactId("sha256:" + "e" * 64)})
    state = StateRecord(
        state_id=StableId("state.historical"),
        subject_id=StableId("entity.chen"),
        predicate="cultivation-attitude",
        value="extreme_confidence",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(evidence,),
    )
    world = _world_with(states=(state,))

    without_history = EvidenceRefAuditor().audit_world(world, current_root)
    with_history = EvidenceRefAuditor().audit_world(
        world,
        current_root,
        historical_text_roots={old_root.root_hash: old_root},
    )

    assert without_history[0].hard_validation == "identity_or_hash_failure"
    assert with_history[0].hard_validation == "pass"


def test_audit_no_tokens_returns_partial() -> None:
    """Lines 194-195: summary with no tokens -> PARTIAL/PREDICATE_VALUE_LOW_SUPPORT."""
    text = "x" * 100
    root, block = _long_text_root(text)
    evidence = _evidence_at(root, block, 0, 50, evidence_id="evidence.notoken")
    state = StateRecord(
        state_id=StableId("state.notoken"),
        subject_id=StableId("entity.chen"),
        predicate="=",
        value="",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(evidence,),
    )
    world = _world_with(states=(state,))
    findings = EvidenceRefAuditor().audit_world(world, root)
    assert findings[0].semantic_disposition == "partial"
    assert "PREDICATE_VALUE_LOW_SUPPORT" in findings[0].risk_tags


def test_audit_language_mismatch_returns_partial() -> None:
    """Lines 202-203: ASCII summary vs CJK text -> PARTIAL/LEXICAL_GATE_LANGUAGE_MISMATCH."""
    text = "陈凡展示出极强的自信态度。"
    root, block = _long_text_root(text)
    evidence = _evidence_at(root, block, 0, 10, evidence_id="evidence.lang")
    state = StateRecord(
        state_id=StableId("state.lang"),
        subject_id=StableId("entity.chen"),
        predicate="cultivation-attitude",
        value="extreme_confidence",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(evidence,),
    )
    world = _world_with(states=(state,))
    findings = EvidenceRefAuditor().audit_world(world, root)
    assert findings[0].semantic_disposition == "unrelated"
    assert "SELECTED_TEXT_UNRELATED" in findings[0].risk_tags


def test_audit_partial_and_supports_dispositions() -> None:
    """Lines 207-211: partial support and full support dispositions."""
    # Audit tokenizes summary on "=" and whitespace; need 4 tokens for threshold=2
    text = "alpha beta gamma delta extra padding here and more padding."
    root, block = _long_text_root(text)
    # Partial: "alpha" matches 1 of 4 tokens -> 1 < 2 -> PARTIAL
    partial_ev = _evidence_at(root, block, 0, 5, evidence_id="evidence.partial")
    # Supports: full text matches all 4 tokens -> SUPPORTS
    supports_ev = _evidence_at(root, block, 0, 30, evidence_id="evidence.supports")
    state_p = StateRecord(
        state_id=StableId("state.partial"),
        subject_id=StableId("entity.chen"),
        predicate="alpha beta gamma",
        value="delta",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(partial_ev,),
    )
    state_s = StateRecord(
        state_id=StableId("state.supports"),
        subject_id=StableId("entity.chen"),
        predicate="alpha beta gamma",
        value="delta",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(supports_ev,),
    )
    world = _world_with(states=(state_p, state_s))
    findings = EvidenceRefAuditor().audit_world(world, root)
    by_id = {f.evidence_id: f for f in findings}
    assert by_id["evidence.partial"].semantic_disposition == "partial"
    assert by_id["evidence.supports"].semantic_disposition == "supports"
    assert by_id["evidence.supports"].severity == "low"
    assert by_id["evidence.supports"].recommended_action == "none"


def test_audit_summary_for_relation_obligation_event() -> None:
    """Lines 254-258: _summary for relation, obligation, event kinds."""
    text = "allies_with breakthrough promise sworn_oath open padding."
    root, block = _long_text_root(text)
    evidence = _evidence_at(root, block, 0, 50, evidence_id="evidence.summary")
    relation = RelationRecord(
        relation_id=StableId("relation.test"),
        predicate="allies_with",
        subject_id=StableId("entity.chen"),
        object_id=StableId("entity.other"),
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(evidence,),
    )
    obligation = PlanObligation(
        obligation_id=StableId("obligation.test"),
        kind=ObligationKind.PROMISE,
        description="sworn_oath",
        status=ObligationStatus.OPEN,
        evidence_refs=(evidence,),
    )
    event = Event(
        event_id=StableId("event.test"),
        event_type="breakthrough",
        truth_class=TruthClass.ASSERTION,
        evidence_refs=(evidence,),
    )
    world = WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "1" * 64),
        schema_version=SchemaVersion("0.1.0"),
        source_commit=CommitId("sha256:" + "2" * 64),
        entities=(
            Entity(
                entity_id=StableId("entity.chen"),
                entity_type="person",
                internal_label="Chen Fan",
            ),
            Entity(
                entity_id=StableId("entity.other"),
                entity_type="person",
                internal_label="Other",
            ),
        ),
        relations=(relation,),
        obligations=(obligation,),
        events=(event,),
    )
    findings = EvidenceRefAuditor().audit_world(world, root)
    summaries = {f.record_kind: f.predicate_value_summary for f in findings}
    assert "allies_with" in summaries["relation"]
    assert "promise" in summaries["obligation"]
    assert "sworn_oath" in summaries["obligation"]
    assert "breakthrough" in summaries["event"]
