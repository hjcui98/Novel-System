#!/usr/bin/env python3
"""No-model fixed-case probe for frozen v11 C95 (U4-L2-R4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.planning import (
    GoalProposal,
    PlanningInquiry,
    PlanningProvenance,
    PlanningQuestion,
    PlanningQuestionKind,
    PlanningReference,
    PlanReview,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    ContractRef,
    ExecutionStatus,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.evidence_slice_resolver import (
    EvidenceSliceResolver,
    LiveEvidenceBasis,
    text_root_indexes,
)
from novel_agent.services.planning_inquiry_need_generation import (
    PlanningInquiryConditionedNeedGenerator,
)
from novel_agent.tools.retrieval import CHANNEL_BY_TOOL, POOL_BY_CHANNEL

VERSION = SchemaVersion("1.0.0")
SOURCE = Path("/tmp/ns-stage2m-genesis-8005-20260822-v11-repaired-20260822")
WORLD_ID = ArtifactId("sha256:2bab743f44cad911c776fc269220d0c273cfd721d9a8e278ffe7b736eefc0438")
TEXT_ID = ArtifactId("sha256:656c31df6507cc47aeff3220964fab4f1cd9737b31147221777d54f41ca97d83")
COMMIT = "sha256:2f12fb3ae7114c2a46a104a31ff6d3475c57bcd1755f83129462b9cf8d088593"
QUESTIONS = (
    "徐有容与陈长生的婚约关系是什么?",
    "朝廷政治反弹及后果是什么?",
    "陈长生与黑龙现在如何?",
    "陈长生经脉当前状态如何?",
    "落落与陈长生的 relationship 是什么?",
)


def _ref(artifact_id: ArtifactId, media_type: str, byte_length: int) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        media_type=media_type,
        byte_length=byte_length,
        schema_version=VERSION,
    )


def _receipt() -> AgentExecutionReceipt:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return AgentExecutionReceipt(
        receipt_id=StableId("receipt.u4l2.probe"),
        run_id=RunId("run.u4l2.probe"),
        task_id=TaskId("task.u4l2.probe"),
        agent_spec=ContractRef(
            contract_id=StableId("agent.plan_reviewer.chapter_set"),
            version=VERSION,
            content_hash=ArtifactId("sha256:" + "a" * 64),
        ),
        agent_type=AgentType.PLAN_REVIEWER,
        agent_mode=AgentMode.CHAPTER_SET,
        prompt_fingerprint=ArtifactId("sha256:" + "a" * 64),
        configuration_fingerprint=ArtifactId("sha256:" + "a" * 64),
        base_commit=None,
        status=ExecutionStatus.SUCCEEDED,
        started_at=now,
        completed_at=now,
        latency_ms=0,
    )


def probe() -> dict[str, object]:
    store = ArtifactRepository(FilesystemObjectStore(SOURCE / "objects"))
    world_meta = json.loads(
        (SOURCE / "objects/sha256/2b" / f"{WORLD_ID.root[7:]}.metadata.json").read_text()
    )
    text_meta = json.loads(
        (SOURCE / "objects/sha256/65" / f"{TEXT_ID.root[7:]}.metadata.json").read_text()
    )
    world = WorldRootDocument.model_validate_json(
        store.read_verified(
            _ref(WORLD_ID, world_meta["media_type"], int(world_meta["byte_length"]))
        )
    )
    text = TextRootDocument.model_validate_json(
        store.read_verified(_ref(TEXT_ID, text_meta["media_type"], int(text_meta["byte_length"])))
    )
    world = world.model_copy(update={"source_commit": CommitId(COMMIT)})
    blocks, chapter_indexes = text_root_indexes(text)
    resolver = EvidenceSliceResolver()
    basis = LiveEvidenceBasis(
        request_commit=world.source_commit,
        request_snapshot_id=StableId(
            "snapshot.2f12fb3ae7114c2a46a104a31ff6d3475c57bcd1755f83129462b9cf8d088593"
        ),
        checkpoint_chapter=95,
    )
    generator = PlanningInquiryConditionedNeedGenerator()
    goal = GoalProposal(
        goal_id=StableId("goal.u4l2.probe"),
        summary="v11 C95 five-question probe",
        rationale="no-model fixed case",
        provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
        decision_criteria=("typed",),
    )
    questions = tuple(
        PlanningQuestion(
            question_id=StableId(f"question.u4l2.probe.{index}"),
            kind=PlanningQuestionKind.FACT,
            question=text_question,
            provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
            goal_id=goal.goal_id,
            blocking=True,
        )
        for index, text_question in enumerate(QUESTIONS)
    )
    inquiry = PlanningInquiry(
        inquiry_id=StableId("inquiry.u4l2.probe"),
        project_id=ProjectId("ztj_volume01_preview"),
        mode=AgentMode.CHAPTER_SET,
        planning_scope=("rolling",),
        horizon_start=96,
        horizon_end=96,
        author_intent_refs=(
            _ref(WORLD_ID, world_meta["media_type"], int(world_meta["byte_length"])),
        ),
        goal_proposals=(goal,),
        questions=questions,
        expected_output_shape="bounded PlanProposal",
    )
    inquiry_ref = _ref(
        ArtifactId("sha256:" + "b" * 64),
        "application/json",
        1,
    )
    # generate() requires real artifact equality with review target; use a temp repo.
    probe_objects = Path("/tmp/grok-goal-3b689d075790/implementer/probe-objects")
    tmp = ArtifactRepository(FilesystemObjectStore(probe_objects))
    inquiry_ref = tmp.put(inquiry.model_dump_json().encode(), "application/json", VERSION)
    review = PlanReview(
        review_id=StableId("review.u4l2.probe"),
        target_kind=ReviewTargetKind.INQUIRY,
        target_artifact_ref=inquiry_ref,
        decision=ReviewDecision.ACCEPT,
        receipt=_receipt(),
    )
    review_ref = tmp.put(review.model_dump_json().encode(), "application/json", VERSION)
    generated = generator.generate(
        inquiry=inquiry,
        inquiry_ref=inquiry_ref,
        review=review,
        review_ref=review_ref,
        world=world,
        run_id=RunId("run.u4l2.probe"),
        task_id=TaskId("task.u4l2.probe"),
    )
    per_question: list[dict[str, object]] = []
    for question, need in zip(generated.selected_question_ids, generated.needs, strict=False):
        tools = tuple(
            name
            for name, channel in CHANNEL_BY_TOOL.items()
            if POOL_BY_CHANNEL.get(channel) in need.allowed_candidate_pools
        )
        live_slices = 0
        for relation in world.relations:
            if not set(need.entity_ids).intersection({relation.subject_id, relation.object_id}):
                continue
            for ref in relation.evidence_refs:
                block = blocks.get(ref.span.block_id) if ref.span is not None else None
                chapter = None if block is None else chapter_indexes.get(block.chapter_id)
                slices = resolver.resolve_live_evidence(
                    basis=basis,
                    unit_source_commit=world.source_commit,
                    unit_snapshot_id=basis.request_snapshot_id,
                    evidence=ref,
                    block=block,
                    chapter_index=chapter,
                    access_scope="author_planning",
                )
                if slices:
                    live_slices += len(slices)
        per_question.append(
            {
                "question_id": question.root,
                "question": need.query_text,
                "grounded_entities": tuple(item.root for item in need.entity_ids),
                "facets": tuple(facet.facet_kind.value for facet in need.need_facets),
                "intent": need.query_intent.value,
                "pools": tuple(pool.value for pool in need.allowed_candidate_pools),
                "allowed_tools": tools,
                "live_l0_slices": live_slices,
                "graph_pool": "graph" in {pool.value for pool in need.allowed_candidate_pools},
            }
        )
    return {
        "basis_commit": COMMIT,
        "world_root": WORLD_ID.root,
        "text_root": TEXT_ID.root,
        "selected_question_ids": tuple(item.root for item in generated.selected_question_ids),
        "rejection_reasons": generated.rejection_reasons,
        "questions": per_question,
        "model_calls": 0,
    }


def main() -> int:
    payload = probe()
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
