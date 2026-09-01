"""Answer Judge and Evidence-Support Judge receipts for frozen Writer answers.

Pending/unavailable is a typed state. It is never coerced into a zero score.
"""

from __future__ import annotations

from novel_agent.domain.artifacts import (
    WRITER_JUDGE_INPUT_MEDIA_TYPE,
    WRITER_JUDGE_OUTPUT_MEDIA_TYPE,
    WRITER_JUDGE_RECEIPT_MEDIA_TYPE,
    ArtifactRef,
)
from novel_agent.domain.benchmark import GoldItem
from novel_agent.domain.ids import RunId, SchemaVersion, StableId
from novel_agent.domain.memory_benchmark import ContextWriterResponse, EvidenceLedger, FreezeReceipt
from novel_agent.domain.v05_readout import (
    WriterJudgeAvailability,
    WriterJudgeKind,
    WriterJudgePair,
    WriterJudgeReceipt,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.writer_response_evaluation import WriterResponseGoldAdapter

JUDGE_SCHEMA_VERSION = SchemaVersion("1.0.0")
ANSWER_PHASE = "benchmark.answer_judge"
EVIDENCE_PHASE = "benchmark.evidence_support_judge"


class WriterJudgeError(ValueError):
    """Judge receipt contract was violated."""


class WriterJudgeService:
    """Record the two Writer-answer judge phases without a second scorer."""

    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts

    def pending_pair(
        self,
        *,
        run_id: RunId,
        task_id: StableId,
        freeze_receipt_id: StableId,
        response_ref: ArtifactRef,
        availability: WriterJudgeAvailability = WriterJudgeAvailability.PENDING,
    ) -> WriterJudgePair:
        if availability is WriterJudgeAvailability.AVAILABLE:
            raise WriterJudgeError("available judges must use record_available_pair")
        answer = self._receipt(
            kind=WriterJudgeKind.ANSWER,
            availability=availability,
            phase=ANSWER_PHASE,
            run_id=run_id,
            task_id=task_id,
            freeze_receipt_id=freeze_receipt_id,
            response_ref=response_ref,
        )
        evidence = self._receipt(
            kind=WriterJudgeKind.EVIDENCE_SUPPORT,
            availability=availability,
            phase=EVIDENCE_PHASE,
            run_id=run_id,
            task_id=task_id,
            freeze_receipt_id=freeze_receipt_id,
            response_ref=response_ref,
        )
        return WriterJudgePair(
            task_id=task_id,
            answer_judge=answer,
            evidence_support_judge=evidence,
        )

    def record_available_pair(
        self,
        *,
        run_id: RunId,
        task_id: StableId,
        freeze_receipt_id: StableId,
        response_ref: ArtifactRef,
        answer_input_ref: ArtifactRef,
        answer_output_ref: ArtifactRef,
        answer_score: float,
        evidence_input_ref: ArtifactRef,
        evidence_output_ref: ArtifactRef,
        evidence_score: float,
        answer_model_request_id: StableId,
        evidence_model_request_id: StableId,
    ) -> WriterJudgePair:
        answer = self._receipt(
            kind=WriterJudgeKind.ANSWER,
            availability=WriterJudgeAvailability.AVAILABLE,
            phase=ANSWER_PHASE,
            run_id=run_id,
            task_id=task_id,
            freeze_receipt_id=freeze_receipt_id,
            response_ref=response_ref,
            input_artifact_ref=answer_input_ref,
            output_artifact_ref=answer_output_ref,
            model_request_id=answer_model_request_id,
            score=answer_score,
        )
        evidence = self._receipt(
            kind=WriterJudgeKind.EVIDENCE_SUPPORT,
            availability=WriterJudgeAvailability.AVAILABLE,
            phase=EVIDENCE_PHASE,
            run_id=run_id,
            task_id=task_id,
            freeze_receipt_id=freeze_receipt_id,
            response_ref=response_ref,
            input_artifact_ref=evidence_input_ref,
            output_artifact_ref=evidence_output_ref,
            model_request_id=evidence_model_request_id,
            score=evidence_score,
        )
        return WriterJudgePair(
            task_id=task_id,
            answer_judge=answer,
            evidence_support_judge=evidence,
        )

    def complete_evidence_support(
        self,
        pair: WriterJudgePair,
        *,
        response: ContextWriterResponse,
        frozen_ledger: EvidenceLedger,
        freeze_receipt: FreezeReceipt,
        gold: GoldItem,
    ) -> WriterJudgePair:
        """Finish Evidence-Support via the existing matcher. Answer Judge stays pending."""

        if pair.answer_judge.availability is WriterJudgeAvailability.AVAILABLE:
            raise WriterJudgeError("answer judge is already available")
        input_ref = self._artifacts.put(
            canonical_json_bytes(
                {
                    "task_id": pair.task_id.root,
                    "freeze_receipt_id": freeze_receipt.receipt_id.root,
                    "gold_id": gold.gold_id.root,
                    "conclusion_ids": [item.conclusion_id.root for item in response.conclusions],
                }
            ),
            WRITER_JUDGE_INPUT_MEDIA_TYPE,
            JUDGE_SCHEMA_VERSION,
        )
        match = WriterResponseGoldAdapter().match(
            response=response,
            frozen_ledger=frozen_ledger,
            freeze_receipt=freeze_receipt,
            gold=gold,
        )
        output_ref = self._artifacts.put(
            canonical_json_bytes(match.model_dump(mode="json")),
            WRITER_JUDGE_OUTPUT_MEDIA_TYPE,
            JUDGE_SCHEMA_VERSION,
        )
        if input_ref.artifact_id == output_ref.artifact_id:
            raise WriterJudgeError("judge input and output must be distinct artifacts")
        if pair.evidence_support_judge.response_ref.artifact_id in {
            input_ref.artifact_id,
            output_ref.artifact_id,
        }:
            raise WriterJudgeError("judge artifacts must be distinct from the Writer answer")
        evidence = self._receipt(
            kind=WriterJudgeKind.EVIDENCE_SUPPORT,
            availability=WriterJudgeAvailability.AVAILABLE,
            phase=EVIDENCE_PHASE,
            run_id=pair.evidence_support_judge.run_id,
            task_id=pair.task_id,
            freeze_receipt_id=pair.evidence_support_judge.freeze_receipt_id,
            response_ref=pair.evidence_support_judge.response_ref,
            input_artifact_ref=input_ref,
            output_artifact_ref=output_ref,
            score=1.0 if match.matched else 0.0,
        )
        return WriterJudgePair(
            task_id=pair.task_id,
            answer_judge=pair.answer_judge,
            evidence_support_judge=evidence,
        )

    def persist(self, receipt: WriterJudgeReceipt) -> ArtifactRef:
        return self._artifacts.put(
            canonical_json_bytes(receipt.model_dump(mode="json")),
            WRITER_JUDGE_RECEIPT_MEDIA_TYPE,
            JUDGE_SCHEMA_VERSION,
        )

    def _receipt(
        self,
        *,
        kind: WriterJudgeKind,
        availability: WriterJudgeAvailability,
        phase: str,
        run_id: RunId,
        task_id: StableId,
        freeze_receipt_id: StableId,
        response_ref: ArtifactRef,
        input_artifact_ref: ArtifactRef | None = None,
        output_artifact_ref: ArtifactRef | None = None,
        model_request_id: StableId | None = None,
        score: float | None = None,
    ) -> WriterJudgeReceipt:
        return WriterJudgeReceipt(
            receipt_id=StableId(f"writer-judge.{kind.value}.{task_id.root}"[:128]),
            kind=kind,
            availability=availability,
            logical_phase=phase,
            run_id=run_id,
            task_id=task_id,
            freeze_receipt_id=freeze_receipt_id,
            response_ref=response_ref,
            input_artifact_ref=input_artifact_ref,
            output_artifact_ref=output_artifact_ref,
            model_request_id=model_request_id,
            score=score,
        )
