"""Track B `context_writer_response` contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.memory_benchmark import (
    ContextWriterConclusion,
    ContextWriterExpectedAction,
    ContextWriterGap,
    ContextWriterGapType,
    ContextWriterResponse,
)
from novel_agent.domain.text import (
    EvidenceRef,
    EvidenceSupportStatus,
    QuoteHash,
    TextSpanRef,
)
from novel_agent.domain.writer_context import BenchmarkInformationProfile
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract

COMMIT = CommitId("sha256:" + "a" * 64)
EVIDENCE = EvidenceRef(
    evidence_id=StableId("evidence.context-writer"),
    root_hash=ArtifactId("sha256:" + "b" * 64),
    object_hash=ArtifactId("sha256:" + "c" * 64),
    chapter_id=StableId("chapter.100"),
    scene_id=StableId("scene.100"),
    span=TextSpanRef(block_id=StableId("block.100"), start=0, end=1),
    quote_hash=QuoteHash("sha256:" + "d" * 64),
    support_status=EvidenceSupportStatus.CURRENT,
    resolved_at_commit=COMMIT,
)


def _response() -> ContextWriterResponse:
    task = build_safe_task_contract(
        case_id=StableId("case.context-writer"),
        checkpoint_chapter=100,
        target_range=(101, 120),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="test",
    )
    gap = ContextWriterGap(
        gap_id=StableId("gap.context-writer"),
        description="角色当前仍不知道寿命秘密是否已在历史中明确。",
        gap_type=ContextWriterGapType.EPISTEMIC_BOUNDARY,
        blocking=True,
        evidence_available=False,
        expected_action=ContextWriterExpectedAction.REQUEST_MEMORY,
    )
    conclusion = ContextWriterConclusion(
        conclusion_id=StableId("conclusion.context-writer"),
        text="陈长生当前仍在研究修行困境。",
        evidence_refs=(EVIDENCE,),
        gap_ids=(gap.gap_id,),
    )
    return ContextWriterResponse(
        response_version="context_writer_response.v1",
        task_contract=task,
        basis_commit_id=COMMIT,
        conclusions=(conclusion,),
        gaps=(gap,),
        rendered_response="",
        frozen_before_gold_reveal=True,
    )


def test_context_writer_response_accepts_valid_track_b_answer() -> None:
    response = _response()
    assert response.conclusions[0].evidence_refs == (EVIDENCE,)
    assert response.gaps[0].blocking is True
    assert response.frozen_before_gold_reveal is True


def test_context_writer_conclusion_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        ContextWriterConclusion(
            conclusion_id=StableId("conclusion.no-evidence"),
            text="没有证据的结论。",
            evidence_refs=(),
        )


def test_context_writer_response_rejects_undeclared_gap_reference() -> None:
    task = build_safe_task_contract(
        case_id=StableId("case.context-writer-invalid"),
        checkpoint_chapter=100,
        target_range=(101, 120),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="test",
    )
    conclusion = ContextWriterConclusion(
        conclusion_id=StableId("conclusion.invalid-gap"),
        text="引用了不存在的 gap。",
        evidence_refs=(EVIDENCE,),
        gap_ids=(StableId("gap.undeclared"),),
    )
    with pytest.raises(ValidationError, match="undeclared gap"):
        ContextWriterResponse(
            response_version="context_writer_response.v1",
            task_contract=task,
            basis_commit_id=COMMIT,
            conclusions=(conclusion,),
            gaps=(),
        )


def test_context_writer_response_rejects_duplicate_conclusions() -> None:
    task = build_safe_task_contract(
        case_id=StableId("case.context-writer-dup"),
        checkpoint_chapter=100,
        target_range=(101, 120),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="test",
    )
    conclusion = ContextWriterConclusion(
        conclusion_id=StableId("conclusion.dup"),
        text="重复结论。",
        evidence_refs=(EVIDENCE,),
    )
    with pytest.raises(ValidationError, match="conclusions must be unique"):
        ContextWriterResponse(
            response_version="context_writer_response.v1",
            task_contract=task,
            basis_commit_id=COMMIT,
            conclusions=(conclusion, conclusion),
        )


def test_context_writer_response_rejects_gold_and_draft_side_channels() -> None:
    payload = _response().model_dump(mode="json")
    payload["gold_ids"] = ["gold.forbidden"]
    with pytest.raises(ValidationError):
        ContextWriterResponse.model_validate(payload)
    payload = _response().model_dump(mode="json")
    payload["target_realization"] = "future chapter prose"
    with pytest.raises(ValidationError):
        ContextWriterResponse.model_validate(payload)
    payload = _response().model_dump(mode="json")
    payload["draft_prose"] = "chapter 101 text"
    with pytest.raises(ValidationError):
        ContextWriterResponse.model_validate(payload)
