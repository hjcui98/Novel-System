"""V0.5 readout domain validators and receipt contracts."""

from __future__ import annotations

from typing import Any

import pytest

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelCostAvailability
from novel_agent.domain.v05_readout import (
    BenchmarkFailureLayer,
    DurableEvidenceReport,
    EvaluationNamespaceDiscardReceipt,
    MemoryIdentitySnapshot,
    V05FakeCampaignReceipt,
    V05FakeCampaignTaskReceipt,
    V05HistoryAccess,
    V05ReadoutManifest,
    V05ReadoutTaskIdentity,
    V05ReadoutTrack,
    WriterJudgeAvailability,
    WriterJudgeKind,
    WriterJudgePair,
    WriterJudgeReceipt,
    map_v05_history_access,
)
from novel_agent.domain.writer_context import BenchmarkInformationProfile


def _construct(model: Any, **values: Any) -> Any:
    return model.model_construct(**values)


def _reject(model: Any, method: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        getattr(model, method)()


def _ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "a" * 64),
        media_type="application/vnd.novel-agent.evaluation.qa-writer-response+json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )


DOMAIN_TASK_ID = StableId("task.v05-domain")


def _judge_receipt(
    *,
    kind: WriterJudgeKind,
    availability: WriterJudgeAvailability,
    task_id: StableId = DOMAIN_TASK_ID,
    score: float | None = None,
    input_ref: ArtifactRef | None = None,
    output_ref: ArtifactRef | None = None,
    model_request_id: StableId | None = None,
) -> WriterJudgeReceipt:
    phase = (
        "benchmark.answer_judge"
        if kind is WriterJudgeKind.ANSWER
        else "benchmark.evidence_support_judge"
    )
    return WriterJudgeReceipt(
        receipt_id=StableId(f"writer-judge.{kind.value}.{task_id.root}"[:128]),
        kind=kind,
        availability=availability,
        logical_phase=phase,
        run_id=RunId("run.v05-domain"),
        task_id=task_id,
        freeze_receipt_id=StableId("freeze.v05-domain"),
        response_ref=_ref(),
        input_artifact_ref=input_ref,
        output_artifact_ref=output_ref,
        model_request_id=model_request_id,
        score=score,
    )


def test_map_v05_history_access_rejects_unknown_strings() -> None:
    assert map_v05_history_access(V05HistoryAccess.HISTORY_ONLY) is (
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF
    )
    with pytest.raises(ValueError, match=r"unsupported V0.5 history access"):
        map_v05_history_access("not-a-profile")


def test_readout_task_identity_rejects_cross_track_fields() -> None:
    qa_missing = _construct(
        V05ReadoutTaskIdentity,
        task_id=StableId("task.v05.qa.bad"),
        track=V05ReadoutTrack.QA,
        checkpoint_id=StableId("checkpoint.v05"),
        checkpoint_chapter=20,
        history_access=V05HistoryAccess.HISTORY_ONLY,
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        question_release="after_checkpoint_freeze",
    )
    _reject(qa_missing, "validate_identity", "requires a question id")
    qa_with_window = qa_missing.model_copy(
        update={
            "question_id": StableId("question.v05"),
            "target_chapter_start": 21,
            "target_chapter_end": 22,
        }
    )
    _reject(qa_with_window, "validate_identity", "must not carry a target window")

    context = _construct(
        V05ReadoutTaskIdentity,
        task_id=StableId("task.v05.context.bad"),
        track=V05ReadoutTrack.CONTEXT,
        checkpoint_id=StableId("checkpoint.v05"),
        checkpoint_chapter=20,
        history_access=V05HistoryAccess.AUTHOR_PLAN_CONDITIONED,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        target_chapter_start=21,
        target_chapter_end=22,
    )
    context_with_question = context.model_copy(
        update={
            "question_id": StableId("question.v05"),
            "question_release": "after_checkpoint_freeze",
        }
    )
    _reject(context_with_question, "validate_identity", "must not carry a QA question")
    _reject(
        context.model_copy(update={"target_chapter_start": None, "target_chapter_end": None}),
        "validate_identity",
        "requires a target window",
    )
    _reject(
        context.model_copy(update={"target_chapter_start": 25, "target_chapter_end": 21}),
        "validate_identity",
        "target range is invalid",
    )
    _reject(
        context.model_copy(update={"target_chapter_start": 20, "target_chapter_end": 22}),
        "validate_identity",
        "must follow its checkpoint",
    )
    wrong_profile = context.model_copy(
        update={"information_profile": BenchmarkInformationProfile.VISIBLE_AT_CUTOFF}
    )
    _reject(wrong_profile, "validate_identity", "does not match the production profile")


def test_manifest_and_judge_receipt_validators_fail_closed() -> None:
    identity = V05ReadoutTaskIdentity(
        task_id=StableId("task.v05.manifest"),
        track=V05ReadoutTrack.QA,
        checkpoint_id=StableId("checkpoint.v05"),
        checkpoint_chapter=20,
        history_access=V05HistoryAccess.HISTORY_ONLY,
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        question_release="after_checkpoint_freeze",
        question_id=StableId("question.v05"),
    )
    duplicate = _construct(
        V05ReadoutManifest,
        benchmark_id="novelmem-eval-ztj",
        version="0.5-seed.2",
        tasks=(identity, identity),
    )
    _reject(duplicate, "validate_unique_tasks", "must be unique")

    wrong_phase = _judge_receipt(
        kind=WriterJudgeKind.ANSWER,
        availability=WriterJudgeAvailability.PENDING,
    ).model_copy(update={"logical_phase": "benchmark.evidence_support_judge"})
    _reject(wrong_phase, "validate_pending_score", "phase does not match")
    pending = _judge_receipt(
        kind=WriterJudgeKind.ANSWER,
        availability=WriterJudgeAvailability.PENDING,
    )
    pending_data = pending.model_dump()
    pending_data["score"] = 0.5
    pending_with_score = _construct(WriterJudgeReceipt, **pending_data)
    _reject(pending_with_score, "validate_pending_score", "cannot carry a score")
    available_missing = _construct(
        WriterJudgeReceipt,
        receipt_id=StableId("writer-judge.evidence-support.missing"),
        kind=WriterJudgeKind.EVIDENCE_SUPPORT,
        availability=WriterJudgeAvailability.AVAILABLE,
        logical_phase="benchmark.evidence_support_judge",
        run_id=RunId("run.v05-domain"),
        task_id=StableId("task.v05-domain"),
        freeze_receipt_id=StableId("freeze.v05-domain"),
        response_ref=_ref(),
    )
    _reject(available_missing, "validate_pending_score", "requires a score")
    available_missing_refs = _construct(
        WriterJudgeReceipt,
        receipt_id=StableId("writer-judge.answer.missing-refs"),
        kind=WriterJudgeKind.ANSWER,
        availability=WriterJudgeAvailability.AVAILABLE,
        logical_phase="benchmark.answer_judge",
        run_id=RunId("run.v05-domain"),
        task_id=StableId("task.v05-domain"),
        freeze_receipt_id=StableId("freeze.v05-domain"),
        response_ref=_ref(),
        score=0.5,
    )
    _reject(available_missing_refs, "validate_pending_score", "requires input and output artifacts")

    answer = _judge_receipt(
        kind=WriterJudgeKind.ANSWER,
        availability=WriterJudgeAvailability.PENDING,
    )
    wrong_kind = _construct(
        WriterJudgePair,
        task_id=StableId("task.v05.pair"),
        answer_judge=_judge_receipt(
            kind=WriterJudgeKind.EVIDENCE_SUPPORT,
            availability=WriterJudgeAvailability.PENDING,
        ),
        evidence_support_judge=answer,
    )
    _reject(wrong_kind, "validate_pair", "answer judge receipt has the wrong kind")
    mismatch = _construct(
        WriterJudgePair,
        task_id=StableId("task.v05.pair"),
        answer_judge=answer,
        evidence_support_judge=_judge_receipt(
            kind=WriterJudgeKind.EVIDENCE_SUPPORT,
            availability=WriterJudgeAvailability.PENDING,
            task_id=StableId("task.other"),
        ),
    )
    _reject(mismatch, "validate_pair", "task ids must match")
    pending_output = pending.model_dump()
    pending_output["output_artifact_ref"] = _ref()
    _reject(
        _construct(WriterJudgeReceipt, **pending_output),
        "validate_pending_score",
        "cannot claim an output artifact",
    )
    pending_request = pending.model_dump()
    pending_request["model_request_id"] = StableId("model.pending")
    _reject(
        _construct(WriterJudgeReceipt, **pending_request),
        "validate_pending_score",
        "cannot claim a model request",
    )
    both_answer = _construct(
        WriterJudgePair,
        task_id=StableId("task.v05.pair"),
        answer_judge=answer,
        evidence_support_judge=answer,
    )
    _reject(both_answer, "validate_pair", "evidence-support judge receipt has the wrong kind")


def test_campaign_and_discard_receipts_enforce_evaluation_boundaries() -> None:
    identity = V05ReadoutTaskIdentity(
        task_id=StableId("task.v05.campaign"),
        track=V05ReadoutTrack.QA,
        checkpoint_id=StableId("checkpoint.v05"),
        checkpoint_chapter=20,
        history_access=V05HistoryAccess.HISTORY_ONLY,
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        question_release="after_checkpoint_freeze",
        question_id=StableId("question.v05"),
    )
    judges = WriterJudgePair(
        task_id=identity.task_id,
        answer_judge=_judge_receipt(
            kind=WriterJudgeKind.ANSWER,
            availability=WriterJudgeAvailability.PENDING,
            task_id=identity.task_id,
        ),
        evidence_support_judge=_judge_receipt(
            kind=WriterJudgeKind.EVIDENCE_SUPPORT,
            availability=WriterJudgeAvailability.PENDING,
            task_id=identity.task_id,
        ),
    )
    task_receipt = V05FakeCampaignTaskReceipt(
        identity=identity,
        freeze_receipt_id=StableId("freeze.v05"),
        model_request_id=StableId("model.v05"),
        response_ref=_ref(),
        record_ref=_ref(),
        judges=judges,
    )
    bad_counts = _construct(
        V05FakeCampaignReceipt,
        campaign_id=StableId("campaign.v05"),
        run_id=RunId("run.v05"),
        freeze_receipt_id=StableId("freeze.v05"),
        qa_count=2,
        context_count=0,
        tasks=(task_receipt,),
    )
    _reject(bad_counts, "validate_campaign", "counts do not match")
    c100 = identity.model_copy(update={"checkpoint_chapter": 100})
    bad_c100 = _construct(
        V05FakeCampaignReceipt,
        campaign_id=StableId("campaign.v05"),
        run_id=RunId("run.v05"),
        freeze_receipt_id=StableId("freeze.v05"),
        qa_count=1,
        context_count=0,
        tasks=(task_receipt.model_copy(update={"identity": c100}),),
    )
    _reject(bad_c100, "validate_campaign", "C100 must not carry a QA")
    duplicate_tasks = _construct(
        V05FakeCampaignReceipt,
        campaign_id=StableId("campaign.v05"),
        run_id=RunId("run.v05"),
        freeze_receipt_id=StableId("freeze.v05"),
        qa_count=2,
        context_count=0,
        tasks=(task_receipt, task_receipt),
    )
    _reject(duplicate_tasks, "validate_campaign", "must be unique")
    context_c300 = identity.model_copy(
        update={
            "track": V05ReadoutTrack.CONTEXT,
            "checkpoint_chapter": 300,
            "question_id": None,
            "question_release": None,
            "target_chapter_start": 301,
            "target_chapter_end": 305,
        }
    )
    bad_c300 = _construct(
        V05FakeCampaignReceipt,
        campaign_id=StableId("campaign.v05"),
        run_id=RunId("run.v05"),
        freeze_receipt_id=StableId("freeze.v05"),
        qa_count=0,
        context_count=1,
        tasks=(task_receipt.model_copy(update={"identity": context_c300}),),
    )
    _reject(bad_c300, "validate_campaign", "C300 must not carry a Context")
    revealed = _construct(
        V05FakeCampaignReceipt,
        campaign_id=StableId("campaign.v05"),
        run_id=RunId("run.v05"),
        freeze_receipt_id=StableId("freeze.v05"),
        qa_count=1,
        context_count=0,
        tasks=(task_receipt.model_copy(update={"gold_revealed": True}),),
    )
    _reject(revealed, "validate_campaign", "without revealing Gold")
    freeze_mismatch = _construct(
        V05FakeCampaignReceipt,
        campaign_id=StableId("campaign.v05"),
        run_id=RunId("run.v05"),
        freeze_receipt_id=StableId("freeze.other"),
        qa_count=1,
        context_count=0,
        tasks=(task_receipt,),
    )
    _reject(freeze_mismatch, "validate_campaign", "share the freeze receipt")

    identity_snapshot = MemoryIdentitySnapshot(
        commit_id=CommitId("sha256:" + "b" * 64),
        text_root=ArtifactId("sha256:" + "c" * 64),
        world_root=ArtifactId("sha256:" + "d" * 64),
        plan_root=ArtifactId("sha256:" + "e" * 64),
        profile_root=ArtifactId("sha256:" + "f" * 64),
    )
    empty_discard = _construct(
        EvaluationNamespaceDiscardReceipt,
        receipt_id=StableId("discard.empty"),
        run_id=RunId("run.v05"),
        discarded_refs=(),
        memory_identity_before=identity_snapshot,
        memory_identity_after=identity_snapshot,
    )
    _reject(empty_discard, "validate_discard", "at least one evaluation artifact")
    changed = identity_snapshot.model_copy(update={"commit_id": CommitId("sha256:" + "9" * 64)})
    discard = _construct(
        EvaluationNamespaceDiscardReceipt,
        receipt_id=StableId("discard.v05"),
        run_id=RunId("run.v05"),
        discarded_refs=(_ref(),),
        memory_identity_before=identity_snapshot,
        memory_identity_after=changed,
    )
    _reject(discard, "validate_discard", "must not change Memory identity")
    production = _ref().model_copy(
        update={"media_type": "application/vnd.novel-agent.stage5-plan-candidate+json"}
    )
    bad_discard = _construct(
        EvaluationNamespaceDiscardReceipt,
        receipt_id=StableId("discard.v05.bad"),
        run_id=RunId("run.v05"),
        discarded_refs=(production,),
        memory_identity_before=identity_snapshot,
        memory_identity_after=identity_snapshot,
    )
    _reject(bad_discard, "validate_discard", "evaluation-namespace artifacts")

    duplicate_namespaces = _construct(
        DurableEvidenceReport,
        run_id=RunId("run.v05"),
        freeze_receipt_id=StableId("freeze.v05"),
        phase_aggregates=(),
        profile_namespaces=("ns.one", "ns.one"),
        writer_context_item_count=0,
        writer_used_item_count=0,
        cited_evidence_count=0,
        gold_hit_count=None,
        first_failure_layer=BenchmarkFailureLayer.NONE,
        answer_judge_availability=WriterJudgeAvailability.PENDING,
        evidence_judge_availability=WriterJudgeAvailability.PENDING,
        cost_availability=ModelCostAvailability.NOT_APPLICABLE,
    )
    _reject(duplicate_namespaces, "validate_report", "profile namespaces must be unique")
    valid_report = DurableEvidenceReport(
        run_id=RunId("run.v05"),
        freeze_receipt_id=StableId("freeze.v05"),
        phase_aggregates=(),
        profile_namespaces=("ns.one",),
        writer_context_item_count=0,
        writer_used_item_count=0,
        cited_evidence_count=0,
        gold_hit_count=None,
        first_failure_layer=BenchmarkFailureLayer.NONE,
        answer_judge_availability=WriterJudgeAvailability.PENDING,
        evidence_judge_availability=WriterJudgeAvailability.PENDING,
        cost_availability=ModelCostAvailability.NOT_APPLICABLE,
    )
    pending_gold = valid_report.model_copy(update={"gold_hit_count": 1})
    _reject(pending_gold, "validate_report", "cannot report gold hits")
    missing_gold = valid_report.model_copy(
        update={"evidence_judge_availability": WriterJudgeAvailability.AVAILABLE}
    )
    _reject(missing_gold, "validate_report", "requires a gold-hit count")
