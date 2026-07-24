"""WP5: semantic support gate and finding signatures."""

from __future__ import annotations

from novel_agent.domain.changes import (
    ChangeOperationType,
    CuratedOperationDraftV2,
    CuratorEventRecord,
    CuratorObligationRecord,
    CuratorRelationRecord,
    CuratorStateRecord,
    CuratorStoryTime,
    EvidenceCandidate,
    EvidenceSupportDisposition,
    WorldRecordKind,
)
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory_write import ProposalRejectionStage
from novel_agent.domain.world import TruthClass
from novel_agent.services.evidence_support import EvidenceSupportGate
from novel_agent.services.proposal_finding_signature import (
    extract_block_or_candidate_ids,
    proposal_finding_signature,
)


def _candidate(text: str, cid: str = "evidence-candidate.a") -> EvidenceCandidate:
    return EvidenceCandidate(
        candidate_id=StableId(cid),
        block_id=StableId("block.1"),
        chapter_index=1,
        scene_index=0,
        text=text,
        start=0,
        end=len(text),
        content_hash=ArtifactId("sha256:" + "e" * 64),
    )


def _state_op(text_support: str) -> tuple[CuratedOperationDraftV2, EvidenceCandidate]:
    candidate = _candidate(text_support)
    op = CuratedOperationDraftV2(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.STATE,
        target_id=StableId("state.confidence"),
        record=CuratorStateRecord(
            subject_id=StableId("entity.chen"),
            predicate="cultivation-attitude",
            value="extreme_confidence",
            valid_time=CuratorStoryTime(worldline="main"),
            truth_class=TruthClass.ASSERTION,
        ),
        evidence_candidate_ids=(candidate.candidate_id,),
    )
    return op, candidate


# -- Lexical gate (never blocks) --

def test_lexical_hit_returns_supports() -> None:
    gate = EvidenceSupportGate()
    op, cand = _state_op("chen shows extreme_confidence cultivation-attitude clearly.")
    decisions = gate.evaluate_operation(operation_index=0, operation=op, candidates=(cand,))
    assert decisions[0].disposition is EvidenceSupportDisposition.SUPPORTS
    assert decisions[0].reason_code == "CANDIDATE_TEXT_LEXICAL_HIT"


def test_lexical_miss_flags_needs_verifier_never_blocks() -> None:
    gate = EvidenceSupportGate()
    op, cand = _state_op("shuang-er appears at the door and mocks him.")
    decisions = gate.evaluate_operation(operation_index=0, operation=op, candidates=(cand,))
    assert decisions[0].disposition is EvidenceSupportDisposition.PARTIAL
    assert "NEEDS_VERIFIER" in decisions[0].reason_code


def test_chinese_plan_evidence_flags_not_rejects() -> None:
    # C21 real case: value "read_49_books_100_times" vs Chinese source text
    plan_op = CuratedOperationDraftV2(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.STATE,
        target_id=StableId("state.cultivation"),
        record=CuratorStateRecord(
            subject_id=StableId("entity.chen"),
            predicate="has_cultivation_plan",
            value="read_49_books_100_times",
            valid_time=CuratorStoryTime(worldline="main"),
            truth_class=TruthClass.ASSERTION,
        ),
        evidence_candidate_ids=(StableId("evidence-candidate.c21-plan"),),
    )
    cand = _candidate("\u56db\u5341\u4e5d\u5377\u4e66\uff0c\u4e00\u767e\u904d\uff0c\u5341\u5929",
                       cid="evidence-candidate.c21-plan")
    gate = EvidenceSupportGate()
    decisions = gate.evaluate_operation(operation_index=0, operation=plan_op, candidates=(cand,))
    assert decisions[0].disposition is EvidenceSupportDisposition.PARTIAL
    assert "NEEDS_VERIFIER" in decisions[0].reason_code


def test_all_lexical_support_empty_decisions_returns_false() -> None:
    gate = EvidenceSupportGate()
    assert not gate.all_lexical_support(())


def test_lexical_partial_token_match() -> None:
    gate = EvidenceSupportGate()
    op, cand = _state_op("extreme is here.")
    decisions = gate.evaluate_operation(operation_index=0, operation=op, candidates=(cand,))
    assert decisions[0].disposition is EvidenceSupportDisposition.PARTIAL
    assert "NEEDS_VERIFIER" in decisions[0].reason_code


def test_negation_near_primary_returns_contradicts() -> None:
    gate = EvidenceSupportGate()
    op, cand = _state_op("chen does not have cultivation-attitude anymore.")
    decisions = gate.evaluate_operation(operation_index=0, operation=op, candidates=(cand,))
    assert decisions[0].disposition is EvidenceSupportDisposition.CONTRADICTS
    assert decisions[0].reason_code == "CANDIDATE_TEXT_CONTRADICTS"


def test_disposition_far_negation_does_not_contradict() -> None:
    gate = EvidenceSupportGate()
    op, cand = _state_op(
        "not at all relevant here today, but cultivation-attitude "
        "shows extreme_confidence clearly."
    )
    decisions = gate.evaluate_operation(operation_index=0, operation=op, candidates=(cand,))
    assert decisions[0].disposition is EvidenceSupportDisposition.SUPPORTS
    assert decisions[0].reason_code == "CANDIDATE_TEXT_LEXICAL_HIT"


# -- Token extraction (private helpers) --

def test_record_tokens_for_each_kind() -> None:
    gate = EvidenceSupportGate()
    state_tokens = gate._record_tokens(
        CuratorStateRecord(
            subject_id=StableId("s"), predicate="hp", value="critical",
            valid_time=CuratorStoryTime(worldline="main"), truth_class=TruthClass.ASSERTION,
        )
    )
    assert "hp" in state_tokens
    relation_tokens = gate._record_tokens(
        CuratorRelationRecord(
            predicate="loves", subject_id=StableId("a"), object_id=StableId("b"),
            valid_time=CuratorStoryTime(worldline="main"), truth_class=TruthClass.ASSERTION,
        )
    )
    assert "loves" in relation_tokens
    event_tokens = gate._record_tokens(
        CuratorEventRecord(
            event_type="battle", truth_class=TruthClass.ASSERTION,
        )
    )
    assert "battle" in event_tokens
    obligation_tokens = gate._record_tokens(
        CuratorObligationRecord(
            kind="objective", description="enter the tower", status="open",
        )
    )
    assert "objective" in obligation_tokens
    assert "enter" in obligation_tokens or "tower" in obligation_tokens


def test_record_tokens_empty_when_only_punctuation_over_24_chars() -> None:
    # A value longer than 24 chars with no word characters yields no tokens,
    # so the gate grants a NO_LEXICAL_ANCHOR pass rather than blocking.
    gate = EvidenceSupportGate()
    op = CuratedOperationDraftV2(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.EVENT,
        target_id=StableId("event.noise"),
        record=CuratorEventRecord(event_type="!" * 25, truth_class=TruthClass.ASSERTION),
        evidence_candidate_ids=(StableId("e.c"),),
    )
    cand = _candidate("anything goes here")
    decisions = gate.evaluate_operation(operation_index=0, operation=op, candidates=(cand,))
    assert decisions[0].disposition is EvidenceSupportDisposition.SUPPORTS
    assert decisions[0].reason_code == "NO_LEXICAL_ANCHOR_GRANTED_PASS"


def test_record_tokens_extracts_word_parts_from_long_value() -> None:
    # Values longer than 24 chars are not kept whole but are still tokenized.
    gate = EvidenceSupportGate()
    tokens = gate._record_tokens(
        CuratorStateRecord(
            subject_id=StableId("s"),
            predicate="extremely-long-predicate-name",
            value="short",
            valid_time=CuratorStoryTime(worldline="main"),
            truth_class=TruthClass.ASSERTION,
        )
    )
    assert "predicate" in tokens
    assert "short" in tokens


def test_scalar_none_handled() -> None:
    gate = EvidenceSupportGate()
    op = CuratedOperationDraftV2(
        operation=ChangeOperationType.CREATE, record_kind=WorldRecordKind.STATE,
        target_id=StableId("state.x"),
        record=CuratorStateRecord(
            subject_id=StableId("s"), predicate="p", value=None,
            valid_time=CuratorStoryTime(worldline="main"), truth_class=TruthClass.ASSERTION,
        ),
        evidence_candidate_ids=(StableId("e.c"),),
    )
    cand = _candidate("some text")
    decisions = gate.evaluate_operation(operation_index=0, operation=op, candidates=(cand,))
    assert decisions[0].disposition is EvidenceSupportDisposition.PARTIAL


# -- Finding signatures (WP5) --

def test_finding_signature_ignores_output_hash_and_is_stable() -> None:
    first = proposal_finding_signature(
        reason_code="CURATOR_PROPOSAL_INVALID_EVIDENCE",
        rejection_stage=ProposalRejectionStage.SEMANTIC_CONTRACT,
        json_pointers=("/operations/3/evidence_refs/0",),
        violation_rule="evidence_span_in_block_bounds",
        block_or_candidate_ids=("block.ZTJ-P005.21.0",),
    )
    second = proposal_finding_signature(
        reason_code="CURATOR_PROPOSAL_INVALID_EVIDENCE",
        rejection_stage=ProposalRejectionStage.SEMANTIC_CONTRACT,
        json_pointers=("/operations/3/evidence_refs/0",),
        violation_rule="evidence_span_in_block_bounds",
        block_or_candidate_ids=("block.ZTJ-P005.21.0",),
    )
    third = proposal_finding_signature(
        reason_code="CURATOR_PROPOSAL_INVALID_EVIDENCE",
        rejection_stage=ProposalRejectionStage.SEMANTIC_CONTRACT,
        json_pointers=("/operations/2/evidence_refs/0",),
        violation_rule="evidence_span_in_block_bounds",
        block_or_candidate_ids=("block.other",),
    )
    assert first == second
    assert first != third


def test_extract_block_or_candidate_ids_skips_non_matching_lines() -> None:
    """Lines without a block./evidence-candidate./evidence. prefix are skipped."""
    result = extract_block_or_candidate_ids(
        (
            "block.21.0: require 0 <= start < end <= 100",
            "not-a-prefix: this line should be skipped",
            "evidence-candidate.abc: unknown evidence candidate",
            "evidence.ref: some evidence ref",
            "random feedback without prefix",
        )
    )
    assert result == ("block.21.0", "evidence-candidate.abc", "evidence.ref")


def test_extract_block_or_candidate_ids_deduplicates_preserving_order() -> None:
    result = extract_block_or_candidate_ids(
        (
            "block.21.0: first occurrence",
            "block.21.0: duplicate occurrence",
            "evidence-candidate.xyz: another id",
        )
    )
    assert result == ("block.21.0", "evidence-candidate.xyz")


def test_extract_block_or_candidate_ids_returns_empty_for_no_matches() -> None:
    assert extract_block_or_candidate_ids(("no id here", "also no id")) == ()
