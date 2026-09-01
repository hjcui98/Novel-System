"""U3-D: Answer and Evidence-Support Judge receipts keep pending distinct from zero."""

from __future__ import annotations

from pathlib import Path

import pytest

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.domain.artifacts import WRITER_JUDGE_RECEIPT_MEDIA_TYPE, ArtifactRef
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId
from novel_agent.domain.v05_readout import WriterJudgeAvailability, WriterJudgeKind
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.writer_judge import WriterJudgeError, WriterJudgeService

HASH = ArtifactId("sha256:" + "a" * 64)
RUN = RunId("run.writer-judge")
TASK = StableId("task.writer-judge")
FREEZE = StableId("freeze.writer-judge")


def _ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=HASH,
        media_type="application/vnd.novel-agent.evaluation.qa-writer-response+json",
        byte_length=2,
        schema_version=SchemaVersion("1.0.0"),
    )


def test_pending_judge_pair_has_no_score_or_output(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    pair = WriterJudgeService(artifacts).pending_pair(
        run_id=RUN,
        task_id=TASK,
        freeze_receipt_id=FREEZE,
        response_ref=_ref(),
    )
    assert pair.answer_judge.kind is WriterJudgeKind.ANSWER
    assert pair.evidence_support_judge.kind is WriterJudgeKind.EVIDENCE_SUPPORT
    assert pair.answer_judge.availability is WriterJudgeAvailability.PENDING
    assert pair.evidence_support_judge.availability is WriterJudgeAvailability.PENDING
    assert pair.answer_judge.score is None
    assert pair.evidence_support_judge.score is None
    assert pair.answer_judge.logical_phase == "benchmark.answer_judge"
    assert pair.evidence_support_judge.logical_phase == "benchmark.evidence_support_judge"
    stored = WriterJudgeService(artifacts).persist(pair.answer_judge)
    assert stored.media_type == WRITER_JUDGE_RECEIPT_MEDIA_TYPE


def test_pending_judge_rejects_zero_score() -> None:
    from novel_agent.domain.v05_readout import WriterJudgeReceipt

    with pytest.raises(ValueError, match="cannot carry a score"):
        WriterJudgeReceipt(
            receipt_id=StableId("writer-judge.answer.bad"),
            kind=WriterJudgeKind.ANSWER,
            availability=WriterJudgeAvailability.PENDING,
            logical_phase="benchmark.answer_judge",
            run_id=RUN,
            task_id=TASK,
            freeze_receipt_id=FREEZE,
            response_ref=_ref(),
            score=0.0,
        )


def test_available_pair_keeps_two_phases(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    response = _ref()
    pair = WriterJudgeService(artifacts).record_available_pair(
        run_id=RUN,
        task_id=TASK,
        freeze_receipt_id=FREEZE,
        response_ref=response,
        answer_input_ref=response,
        answer_output_ref=response,
        answer_score=0.5,
        evidence_input_ref=response,
        evidence_output_ref=response,
        evidence_score=0.25,
        answer_model_request_id=StableId("model.answer-judge"),
        evidence_model_request_id=StableId("model.evidence-judge"),
    )
    assert pair.answer_judge.score == 0.5
    assert pair.evidence_support_judge.score == 0.25
    assert pair.answer_judge.model_request_id != pair.evidence_support_judge.model_request_id


def test_pending_factory_rejects_available_flag(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    with pytest.raises(WriterJudgeError, match="available judges"):
        WriterJudgeService(artifacts).pending_pair(
            run_id=RUN,
            task_id=TASK,
            freeze_receipt_id=FREEZE,
            response_ref=_ref(),
            availability=WriterJudgeAvailability.AVAILABLE,
        )
