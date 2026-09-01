"""Deterministic structured endpoint for the production assembly smoke path.

This adapter is deliberately an endpoint, not a second assembly or runtime.  It is
available only through the explicit ``deterministic_fake`` endpoint profile used by
isolated production-path evidence.
"""

from __future__ import annotations

import json
import re

from novel_agent.adapters.model.fake import FakeModelEndpoint
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.changes import CuratorV2EvidenceDraft
from novel_agent.domain.editorial import (
    CandidateObservationPayload,
    CuratorObservation,
    EditorialVerdict,
    EditorReviewPayload,
)
from novel_agent.domain.generation import WriterTurnAction, WriterTurnOutput, WriterWorkPlan
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.model_calls import ModelRequest, ProviderModelResult
from novel_agent.domain.planning import (
    GoalProposal,
    PlanningInquiryDraft,
    PlanningProvenance,
    PlanningQuestion,
    PlanningQuestionKind,
    PlanningReference,
    PlanningTurnAction,
    PlanningTurnDraft,
    PlanReviewDraft,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    PlannerProposalDraft,
    ProposalProvenance,
    ProposedItem,
)
from novel_agent.domain.world import GraphCandidatePageDraft, GraphCandidatePageStatus
from novel_agent.services.model_curation import NoOpSemanticVerificationDraft

_HASH = ArtifactId("sha256:" + "1" * 64)
_DRAFT_TEXT = (
    "Lin studies the moonlit groove along the tower gate and opens it without "
    "using her injured arm. She keeps the tower's final secret unsaid while the "
    "injured-arm constraint stays visible in every motion. "
) * 6
_CHAPTER_GOAL = "Enter the tower while protecting the injured arm."


def _artifact_ref(payload: object) -> ArtifactRef:
    if not isinstance(payload, dict):
        raise AssertionError(f"trusted artifact ref must be an object, got {type(payload)!r}")
    return ArtifactRef.model_validate(
        {key: payload[key] for key in ArtifactRef.model_fields if key in payload}
    )


def _catalog_quote(prompt: str) -> str:
    marker = "EVIDENCE_CANDIDATES="
    start = prompt.index(marker) + len(marker)
    end = prompt.index("\n</CURATOR_INPUT>", start)
    raw_views = json.loads(prompt[start:end])
    if not isinstance(raw_views, list):
        raise AssertionError("curator evidence catalog is not a list")
    quotes: list[str] = []
    for item in raw_views:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            quotes.append(text)
    if not quotes:
        raise AssertionError("curator prompt has no copyable evidence catalog text")
    return max(quotes, key=len)


def _no_op_verification(prompt: str) -> NoOpSemanticVerificationDraft:
    start = prompt.index("<NO_OP_VERIFICATION_INPUT")
    start = prompt.index(">", start) + 1
    end = prompt.index("</NO_OP_VERIFICATION_INPUT>", start)
    payload = json.loads(prompt[start:end])
    selected = tuple(StableId(item) for item in payload["selected_candidate_ids"])
    return NoOpSemanticVerificationDraft(
        selected_candidate_ids=selected,
        verified_no_durable_delta=True,
        reason_code="no_new_durable_world_records",
    )


class ProductionChapterEndpoint(FakeModelEndpoint):
    """Return schema-valid fake payloads for every production leaf caller."""

    def __init__(self) -> None:
        super().__init__("", model_version="production-fake-v1")

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.response_text = self._payload(request)
        return await super().generate(request)

    def _payload(self, request: ModelRequest) -> str:
        title = ""
        if isinstance(request.response_schema, dict):
            raw_title = request.response_schema.get("title")
            if isinstance(raw_title, str):
                title = raw_title
        agent = request.agent_id.root if request.agent_id is not None else ""
        prompt = request.prompt
        if title == "PlanningInquiryDraft":
            return self._inquiry(prompt).model_dump_json()
        if title in {"PlanningTurnDraft", "PlannerProposalDraft"}:
            turn = PlanningTurnDraft(
                action=PlanningTurnAction.PLAN_READY,
                plan_proposal_draft=self._proposal(prompt),
            )
            return (
                self._proposal(prompt).model_dump_json()
                if title == "PlannerProposalDraft"
                else turn.model_dump_json(exclude_defaults=True)
            )
        if title == "PlanReviewDraft":
            return self._review(prompt).model_dump_json()
        if title == "WriterWorkPlan" or agent == "agent.writer.work-plan":
            return self._work_plan(prompt).model_dump_json()
        if title == "WriterTurnOutput" or agent == "agent.writer.turn":
            return WriterTurnOutput(
                action=WriterTurnAction.DRAFT_READY,
                draft_text=_DRAFT_TEXT,
                unresolved_questions=("The guard beyond the gate remains unknown.",),
                self_observations=("The injured-arm constraint is preserved.",),
                work_plan_checkpoint="gate opens",
            ).model_dump_json()
        if title == "EditorReviewPayload" or agent.startswith("agent.editor"):
            return EditorReviewPayload(verdict=EditorialVerdict.PASS).model_dump_json()
        if title == "CandidateObservationPayload":
            match = re.search(r'"draft_id"\s*:\s*"(sha256:[0-9a-f]{64})"', prompt)
            draft_id = ArtifactId(match.group(1)) if match is not None else _HASH
            return CandidateObservationPayload(draft_id=draft_id).model_dump_json()
        if title == "CuratorObservation":
            match = re.search(r'"draft_id"\s*:\s*"(sha256:[0-9a-f]{64})"', prompt)
            draft_id = ArtifactId(match.group(1)) if match is not None else _HASH
            return CuratorObservation(draft_id=draft_id).model_dump_json()
        if title in {"CuratorV2EvidenceDraft", "ChapterChangeDraftV2"}:
            chapter = 21
            match = re.search(r"chapter_index=(\d+)", prompt)
            if match is not None:
                chapter = int(match.group(1))
            return CuratorV2EvidenceDraft(
                chapter_index=chapter,
                operations=(),
                no_durable_delta_reason="chapter states no new world records",
                no_op_evidence_quotes=(_catalog_quote(prompt),),
            ).model_dump_json()
        if title == "NoOpSemanticVerificationDraft":
            return _no_op_verification(prompt).model_dump_json()
        if title == "GraphCandidatePageDraft":
            # The deterministic production smoke has no graph fact to add.
            # Return the typed empty-page terminal so the graph owner records
            # a real model call without inventing a relation or entity.
            return GraphCandidatePageDraft(
                status=GraphCandidatePageStatus.COMPLETE,
                candidates=(),
                no_graph_candidate_reason="production fake has no graph delta",
            ).model_dump_json()
        if "PLANNING_PHASE=inquiry" in prompt and "PlanningTurn" not in title:
            return self._inquiry(prompt).model_dump_json()
        if "REVIEW_TARGET_KIND=" in prompt:
            return self._review(prompt).model_dump_json()
        if '"draft_id"' in prompt:
            match = re.search(r'"draft_id"\s*:\s*"(sha256:[0-9a-f]{64})"', prompt)
            draft_id = ArtifactId(match.group(1)) if match is not None else _HASH
            return CuratorObservation(draft_id=draft_id).model_dump_json()
        raise AssertionError(f"unscripted production fake request title={title!r} agent={agent!r}")

    @staticmethod
    def _review(prompt: str) -> PlanReviewDraft:
        kind = ReviewTargetKind.INQUIRY
        match = re.search(r"REVIEW_TARGET_KIND=([a-z_]+)", prompt)
        if match is not None:
            kind = ReviewTargetKind(match.group(1))
        return PlanReviewDraft(target_kind=kind, decision=ReviewDecision.ACCEPT)

    @staticmethod
    def _inquiry(prompt: str) -> PlanningInquiryDraft:
        horizon_start, horizon_end = ProductionChapterEndpoint._horizon(prompt)
        chapter = horizon_start
        goal_id = StableId(f"plan.chapter.{chapter}")
        return PlanningInquiryDraft(
            mode=AgentMode.CHAPTER_SET,
            planning_scope=("rolling",),
            horizon_start=horizon_start,
            horizon_end=horizon_end,
            goal_proposals=(
                GoalProposal(
                    goal_id=goal_id,
                    summary=_CHAPTER_GOAL,
                    rationale="Continue from the committed chapter without violating continuity.",
                    provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
                    decision_criteria=("feasible", "injury preserved"),
                ),
            ),
            questions=(
                PlanningQuestion(
                    question_id=StableId(f"question.chapter.{chapter}.injury"),
                    kind=PlanningQuestionKind.FACT,
                    question="What is Lin's current injury state?",
                    provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
                    goal_id=goal_id,
                    entity_labels=("林澈",),
                ),
            ),
            expected_output_shape="bounded PlanProposal",
        )

    @staticmethod
    def _proposal(prompt: str) -> PlannerProposalDraft:
        chapter = ProductionChapterEndpoint._horizon(prompt)[0]
        return PlannerProposalDraft(
            mode=AgentMode.CHAPTER_SET,
            plan_items=(
                ProposedItem(
                    item_id=StableId(f"plan.chapter.{chapter}"),
                    kind="chapter",
                    payload={
                        "title": "Enter the tower",
                        "summary": _CHAPTER_GOAL,
                        "chapter_index": chapter,
                    },
                    provenance=ProposalProvenance.PLANNER_PROPOSED,
                ),
            ),
            unresolved=(),
            coverage=1.0,
        )

    @staticmethod
    def _horizon(prompt: str) -> tuple[int, int]:
        match = re.search(r"HORIZON=(\d+):(\d+)", prompt)
        if match is not None:
            return int(match.group(1)), int(match.group(2))
        match = re.search(r"chapters:(\d+)-(\d+)", prompt)
        if match is not None:
            return int(match.group(1)), int(match.group(2))
        return 21, 21

    @staticmethod
    def _work_plan(prompt: str) -> WriterWorkPlan:
        # The outer trusted block also carries the task payload and therefore is
        # not itself valid JSON: the nested opaque binding follows the payload.
        # Mirror the WriterWorkPlan contract and parse only that binding, which
        # is the sole source for the three lineage refs.
        start = prompt.index("<OPAQUE_LINEAGE_BINDING>") + len("<OPAQUE_LINEAGE_BINDING>")
        end = prompt.index("</OPAQUE_LINEAGE_BINDING>", start)
        trusted = json.loads(prompt[start:end])
        return WriterWorkPlan(
            work_plan_id=StableId("work-plan.chapter.21"),
            writing_task_ref=_artifact_ref(trusted["writing_task_ref"]),
            accepted_plan_ref=_artifact_ref(trusted["accepted_plan_ref"]),
            writer_context_ref=_artifact_ref(trusted["writer_context_ref"]),
            scene_beat_order=("Observe the gate.", "Redirect moonlight."),
            participating_characters=("Lin",),
            character_current_states=("Lin's left arm remains injured.",),
            pov_boundary="Only Lin's current knowledge may be narrated.",
            reader_disclosure_boundary="Keep the tower's final secret hidden.",
            must_keep=("Do not force the gate with the injured arm.",),
            must_avoid=("Do not reveal the tower's final secret.",),
            selected_skill_ids=(StableId("skill.scene-composition"),),
            expected_skill_checkpoints={"skill.scene-composition": ("gate opens",)},
        )


__all__ = ["ProductionChapterEndpoint"]
