"""Thin Writer-response adapters over the existing Gold evidence matcher."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory_benchmark import (
    ContextWriterConclusion,
    ContextWriterResponse,
    QaEvidenceItem,
    QaWriterResponse,
)
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, QuoteHash, TextSpanRef
from novel_agent.domain.v05_readout import V05ReadoutTrack
from novel_agent.domain.writer_context import (
    WriterContextPackageV2,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.benchmark_scenario_compiler import BenchmarkScenarioCompiler
from novel_agent.services.content_addressing import quote_hash
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatcher
from novel_agent.services.writer_response_evaluation import (
    QaWriterResponseAdapter,
    WriterContextGoldAdapter,
    WriterResponseAdapterError,
    WriterResponseGoldAdapter,
)
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.fixtures.stage2_memory_benchmark import frozen_evaluation_inputs
from tests.unit.test_v05_readout_manifest import CONTEXT_WINDOWS, _checkpoints, _questions


def _context_response(
    package: WriterContextPackageV2,
    refs: tuple[EvidenceRef, ...],
    *,
    frozen: bool = True,
) -> ContextWriterResponse:
    return ContextWriterResponse(
        response_version="context_writer_response.v1",
        task_contract=package.task_contract,
        basis_commit_id=package.basis_commit_id,
        conclusions=(
            ContextWriterConclusion(
                conclusion_id=StableId("conclusion.writer-adapter"),
                text="陈长生仍受经脉问题约束。",
                evidence_refs=refs,
            ),
        ),
        frozen_before_gold_reveal=frozen,
    )


def test_wcp_and_writer_adapters_share_the_gold_matcher() -> None:
    gold, package, ledger, receipt = frozen_evaluation_inputs()
    matcher = GoldEvidenceMatcher()
    wcp = WriterContextGoldAdapter(matcher)
    writer = WriterResponseGoldAdapter(matcher)
    wcp_match = wcp.match(
        gold=gold,
        package=package,
        evidence_ledger=ledger,
        freeze_receipt=receipt,
    )
    writer_match = writer.match(
        response=_context_response(package, ledger.entries[0].evidence_refs),
        frozen_ledger=ledger,
        freeze_receipt=receipt,
        gold=gold,
    )
    assert matcher.version == GoldEvidenceMatcher.version
    assert wcp_match.matched is writer_match.matched is True


def test_writer_adapter_rejects_unfrozen_answer() -> None:
    gold, package, ledger, receipt = frozen_evaluation_inputs()
    with pytest.raises(WriterResponseAdapterError, match="not frozen"):
        WriterResponseGoldAdapter().match(
            response=_context_response(package, ledger.entries[0].evidence_refs, frozen=False),
            frozen_ledger=ledger,
            freeze_receipt=receipt,
            gold=gold,
        )


def test_writer_adapter_rejects_evidence_absent_from_frozen_ledger() -> None:
    gold, package, ledger, receipt = frozen_evaluation_inputs()
    actual = ledger.entries[0].evidence_refs[0]
    unknown = actual.model_copy(
        update={
            "evidence_id": StableId("evidence.unknown-writer"),
            "object_hash": ArtifactId("sha256:" + "9" * 64),
            "quote_hash": QuoteHash("sha256:" + "8" * 64),
        }
    )
    with pytest.raises(WriterResponseAdapterError, match="frozen ledger"):
        WriterResponseGoldAdapter().match(
            response=_context_response(package, (unknown,)),
            frozen_ledger=ledger,
            freeze_receipt=receipt,
            gold=gold,
        )


def test_writer_adapter_accepts_history_proven_extra_span() -> None:
    gold, package, ledger, receipt = frozen_evaluation_inputs()
    bundle = make_synthetic_bundle()
    history, _future = bundle.text_roots
    used = {
        ref.span.block_id
        for entry in ledger.entries
        for ref in entry.evidence_refs
        if ref.span is not None
    }
    block = next(
        block
        for chapter in history.chapters
        for scene in chapter.scenes
        for block in scene.blocks
        if block.block_id not in used and block.text
    )
    excerpt = block.text[: min(4, len(block.text))]
    extra = EvidenceRef(
        evidence_id=StableId("evidence.writer-history"),
        root_hash=history.root_hash,
        object_hash=sha256_id(block.text.encode("utf-8")),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=TextSpanRef(block_id=block.block_id, start=0, end=len(excerpt)),
        quote_hash=quote_hash(excerpt),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=package.basis_commit_id,
    )
    match = WriterResponseGoldAdapter().match(
        response=_context_response(package, (extra,)),
        frozen_ledger=ledger,
        freeze_receipt=receipt,
        gold=gold,
        history_text=history,
    )
    assert match.matched is False


def test_qa_adapter_freeze_gates_and_rejects_future_evidence() -> None:
    _gold, _package, _ledger, receipt = frozen_evaluation_inputs()
    adapter = QaWriterResponseAdapter()
    frozen = adapter.adapt(
        response=QaWriterResponse(
            answer="经脉堵塞",
            evidence=(QaEvidenceItem(chapter=20, span="陈长生被诊断出经脉堵塞"),),
        ),
        freeze_receipt=receipt,
        checkpoint_chapter=20,
    )
    assert frozen.answer == "经脉堵塞"
    with pytest.raises(WriterResponseAdapterError, match="before Gold reveal"):
        adapter.adapt(
            response=frozen,
            freeze_receipt=receipt,
            checkpoint_chapter=20,
            gold_revealed=True,
        )
    with pytest.raises(WriterResponseAdapterError, match="after the freeze"):
        adapter.adapt(
            response=QaWriterResponse(
                answer="经脉堵塞",
                evidence=(QaEvidenceItem(chapter=21, span="future span"),),
            ),
            freeze_receipt=receipt,
            checkpoint_chapter=20,
        )


def test_qa_response_still_rejects_gold_side_channels() -> None:
    payload = QaWriterResponse(answer="ok", evidence=()).model_dump(mode="json")
    payload["gold_ids"] = ["gold.forbidden"]
    with pytest.raises(ValidationError):
        QaWriterResponse.model_validate(payload)


def test_scenario_compiler_enumerates_v05_identities_for_fake_campaign() -> None:
    manifest = BenchmarkScenarioCompiler().compile_v05_readout_identities(
        benchmark_id="novelmem-eval-ztj",
        version="0.5-seed.2",
        checkpoints=_checkpoints(),
        qa_questions=_questions(),
        context_windows=CONTEXT_WINDOWS,
    )
    _gold, package, ledger, receipt = frozen_evaluation_inputs()
    qa_adapter = QaWriterResponseAdapter()
    context_adapter = WriterResponseGoldAdapter()
    qa_ids = []
    context_ids = []
    for task in manifest.tasks:
        if task.track is V05ReadoutTrack.QA:
            qa_adapter.adapt(
                response=QaWriterResponse(
                    answer="frozen",
                    evidence=(QaEvidenceItem(chapter=task.checkpoint_chapter, span="history"),),
                ),
                freeze_receipt=receipt,
                checkpoint_chapter=task.checkpoint_chapter,
            )
            qa_ids.append(task.task_id)
        else:
            context_adapter.writer_ledger(
                response=_context_response(package, ledger.entries[0].evidence_refs),
                frozen_ledger=ledger,
                freeze_receipt=receipt,
            )
            context_ids.append(task.task_id)
    assert len(qa_ids) == 51
    assert len(context_ids) == 30
    assert len(set(qa_ids)) == 51
    assert len(set(context_ids)) == 30
