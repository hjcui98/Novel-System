"""Bounded Writer semantic questions adapted to the existing Memory Gateway."""

from __future__ import annotations

from dataclasses import dataclass

from novel_agent.domain.agent_context import (
    AgentContextView,
    ContextDelta,
    ContextDeltaStatus,
    ContextItemKind,
    ContextLayer,
    ContextViewItem,
)
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.generation import WriterMemoryRequest, WritingLoopRequest
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    CandidatePool,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalChannel,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.planning_memory import EntityMention, GroundedNeedDraft, PlannedNeedDraft
from novel_agent.domain.stage2 import AccessScope, MemoryResolutionRequest
from novel_agent.domain.writer_context import BenchmarkTaskContract, WriterContextSection
from novel_agent.services.agent_context import TokenCounter
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.memory_gateway import MemoryGateway, MemoryGatewayBlockedError
from novel_agent.services.need_draft_grounder import NeedDraftGrounder
from novel_agent.services.need_query_compiler import NeedQueryCompiler
from novel_agent.services.need_validator import NeedValidator
from novel_agent.services.task_focus import FocusSet

WRITER_MEMORY_REQUEST_MEDIA_TYPE = "application/vnd.novel-agent.writer-memory-request+json"
WRITER_MEMORY_RESOLUTION_MEDIA_TYPE = "application/vnd.novel-agent.writer-memory-resolution+json"


class WriterReactiveMemoryError(ValueError):
    """A Writer memory request violates the bounded semantic-question boundary."""


@dataclass(frozen=True, slots=True)
class ReactiveMemoryInputs:
    task: BenchmarkTaskContract
    world: WorldRootDocument
    plan: PlanRootDocument
    focus_set: FocusSet
    text_root: TextRootDocument
    resolution_template: MemoryResolutionRequest
    thread_id: str


@dataclass(frozen=True, slots=True)
class ReactiveMemoryResult:
    delta: ContextDelta
    request_fingerprint: ArtifactId
    needs: tuple[Stage1MemoryNeed, ...]


class WriterReactiveNeedAdapter:
    """Ground, validate, compile, and resolve one bounded reactive memory round."""

    def __init__(
        self,
        gateway: MemoryGateway,
        artifacts: ArtifactRepository,
        token_counter: TokenCounter,
        *,
        schema_version: SchemaVersion,
        grounder: NeedDraftGrounder | None = None,
        validator: NeedValidator | None = None,
        query_compiler: NeedQueryCompiler | None = None,
    ) -> None:
        self._gateway = gateway
        self._artifacts = artifacts
        self._count_tokens = token_counter
        self._schema_version = schema_version
        self._grounder = grounder or NeedDraftGrounder()
        self._validator = validator or NeedValidator(max_total_needs=8)
        self._compiler = query_compiler or NeedQueryCompiler()

    def resolve(
        self,
        loop_request: WritingLoopRequest,
        view: AgentContextView,
        requests: tuple[WriterMemoryRequest, ...],
        inputs: ReactiveMemoryInputs,
        *,
        seen_fingerprints: frozenset[ArtifactId] = frozenset(),
    ) -> ReactiveMemoryResult:
        self._validate_basis(loop_request, view, inputs)
        if not requests:
            raise WriterReactiveMemoryError("reactive Memory round requires a semantic question")
        if len(requests) > loop_request.budgets.max_memory_questions:
            raise WriterReactiveMemoryError("reactive Memory question budget exceeded")
        fingerprint = content_id(
            [
                {
                    "question": item.question,
                    "purpose": item.purpose,
                    "blocked_action": item.blocked_action,
                    "known": [known.root for known in item.known_context_item_ids],
                    "evidence_type": item.requested_evidence_type,
                    "checkpoint": item.scene_or_draft_checkpoint,
                }
                for item in requests
            ]
        )
        if fingerprint in seen_fingerprints:
            return self._terminal_delta(
                loop_request,
                view,
                requests,
                fingerprint,
                ContextDeltaStatus.INSUFFICIENT,
            )

        goal = next(
            (
                item
                for item in inputs.plan.chapter_goals
                if item.chapter_index == loop_request.writing_task.target_chapter
            ),
            None,
        )
        if goal is None:
            return self._terminal_delta(
                loop_request,
                view,
                requests,
                fingerprint,
                ContextDeltaStatus.DENIED,
            )

        drafts = tuple(
            self._draft(index, item, loop_request.writing_task.target_chapter, goal.summary)
            for index, item in enumerate(requests)
        )
        grounded = tuple(self._grounder.ground(item, inputs.world) for item in drafts)
        validation = self._validator.validate(
            drafts=grounded,
            task=inputs.task,
            world=inputs.world,
            focus_set=inputs.focus_set,
            plan=inputs.plan,
        )
        accepted_by_id = {item.draft_id: item for item in validation.accepted_drafts}
        needs = tuple(
            self._need(
                loop_request,
                request,
                accepted_by_id[f"reactive-{index}"],
                validation.need_type_by_draft[f"reactive-{index}"],
            )
            for index, request in enumerate(requests)
            if f"reactive-{index}" in accepted_by_id
        )
        for need in needs:
            bundle = self._compiler.compile(need)
            eligible, _unavailable = self._compiler.eligible_channels(
                need,
                bundle,
                (
                    RetrievalChannel.R1_EXACT,
                    RetrievalChannel.R1_TEMPORAL,
                    RetrievalChannel.ANCHOR_BM25,
                    RetrievalChannel.ANCHOR_DENSE,
                    RetrievalChannel.GROUNDED_BM25,
                    RetrievalChannel.GROUNDED_DENSE,
                    RetrievalChannel.TYPED_GRAPH,
                ),
            )
            if not eligible:
                raise WriterReactiveMemoryError("reactive Need has no executable query channel")
        if not needs:
            return self._terminal_delta(
                loop_request,
                view,
                requests,
                fingerprint,
                ContextDeltaStatus.INSUFFICIENT,
            )

        request_ref = self._artifacts.put(
            canonical_json_bytes([item.model_dump(mode="json") for item in requests]),
            WRITER_MEMORY_REQUEST_MEDIA_TYPE,
            self._schema_version,
        )
        resolution_request = inputs.resolution_template.model_copy(
            update={
                "run_id": loop_request.run_id,
                "task_id": loop_request.task_id,
                "project_id": loop_request.project_id,
                "base_commit": loop_request.base_commit,
                "snapshot_id": loop_request.snapshot_id,
                "task_contract": loop_request.writing_task.contract_id.root,
                "initial_memory_needs": needs,
                "access_scope": AccessScope.WRITER_SAFE,
                "allow_future_plan": False,
            }
        )
        try:
            result = self._gateway.resolve(
                resolution_request,
                inputs.text_root,
                thread_id=inputs.thread_id,
            )
        except MemoryGatewayBlockedError:
            return self._terminal_delta(
                loop_request,
                view,
                requests,
                fingerprint,
                ContextDeltaStatus.DENIED,
            )
        resolution_ref = self._artifacts.put(
            canonical_json_bytes(result.model_dump(mode="json")),
            WRITER_MEMORY_RESOLUTION_MEDIA_TYPE,
            self._schema_version,
        )
        units = tuple(
            unit
            for section in (
                result.context.mandatory_constraints,
                result.context.current_world_state,
                result.context.active_plan_obligations,
                result.context.relevant_historical_events,
                result.context.truth_and_knowledge_boundaries,
                result.context.raw_evidence_spans,
                result.context.style_or_reference_optional,
            )
            for unit in section
            if unit.access_scope == "writer_safe" and not unit.derivation_taint
        )
        existing_ids = {item.item_id for item in view.active_memory_items}
        added = tuple(
            ContextViewItem(
                item_id=StableId(f"context-memory.{unit.unit_id.root}"[:128]),
                layer=ContextLayer.MEMORY,
                kind=ContextItemKind.MEMORY_CLAIM,
                content=unit.text,
                token_count=max(1, self._count_tokens(unit.text)),
                source_artifact_refs=(result.frozen_context_artifact,),
                mandatory=unit.mandatory,
                information_scope="writer_safe",
            )
            for unit in units
            if StableId(f"context-memory.{unit.unit_id.root}"[:128]) not in existing_ids
        )
        status = (
            ContextDeltaStatus.RESOLVED
            if added and not result.context.unresolved_gaps
            else ContextDeltaStatus.PARTIAL
            if added
            else ContextDeltaStatus.INSUFFICIENT
        )
        delta = ContextDelta(
            delta_id=StableId(f"context-delta.{fingerprint.root[-32:]}"),
            request_ref=request_ref,
            resolution_ref=resolution_ref,
            parent_view_revision=view.revision,
            base_commit=loop_request.base_commit,
            snapshot_id=loop_request.snapshot_id,
            profile_ref=loop_request.project_profile_artifact,
            plan_ref=loop_request.accepted_plan.artifact,
            added_memory_items=added,
            resolved_need_ids=tuple(
                need.need_id for need in needs if status is ContextDeltaStatus.RESOLVED
            ),
            unresolved_need_ids=tuple(
                need.need_id for need in needs if status is not ContextDeltaStatus.RESOLVED
            ),
            evidence_refs=(result.frozen_context_artifact,),
            token_impact=sum(item.token_count for item in added),
            status=status,
        )
        return ReactiveMemoryResult(delta=delta, request_fingerprint=fingerprint, needs=needs)

    @staticmethod
    def _draft(
        index: int,
        request: WriterMemoryRequest,
        chapter: int,
        plan_goal: str,
    ) -> PlannedNeedDraft:
        evidence = request.requested_evidence_type.casefold()
        if any(marker in evidence for marker in ("history", "event", "cause", "历史", "因果")):
            scopes = ("historical",)
            facets = ("CAUSAL_HISTORY",)
        elif any(marker in evidence for marker in ("knowledge", "disclosure", "知识", "知情")):
            scopes = ("knowledge",)
            facets = ("KNOWLEDGE_BOUNDARY",)
        elif any(marker in evidence for marker in ("relation", "emotion", "关系", "情绪")):
            scopes = ("current",)
            facets = ("RELATION_STATE",)
        else:
            scopes = ("current",)
            facets = ("CURRENT_STATE",)
        return PlannedNeedDraft(
            draft_id=f"reactive-{index}",
            semantic_question=request.question,
            entity_mentions=tuple(EntityMention(label=item) for item in request.anchor_labels),
            trigger_plan_chapters=(chapter,),
            trigger_plan_goal=plan_goal,
            why_needed=request.purpose,
            required_claim_scopes=scopes,
            suggested_facets=facets,
            historical_time_scope="main",
            query_hints=(request.blocked_action,),
        )

    def _need(
        self,
        loop_request: WritingLoopRequest,
        request: WriterMemoryRequest,
        draft: GroundedNeedDraft,
        need_type: str,
    ) -> Stage1MemoryNeed:
        entity_ids = self._grounder.grounded_entity_ids(draft)
        history = need_type in {"entity_history", "long_range_callback", "knowledge_boundary"}
        need_id = StableId(f"need.stage3.reactive.{request.request_id.root}"[:128])
        return Stage1MemoryNeed(
            need_id=need_id,
            run_id=loop_request.run_id,
            task_id=loop_request.task_id,
            base_commit=loop_request.base_commit,
            chapter_target=loop_request.writing_task.target_chapter,
            need_type=need_type,
            query_intent=(
                Stage1QueryIntent.SEMANTIC_HISTORY if history else Stage1QueryIntent.CURRENT_STATE
            ),
            query_text=request.question,
            entity_ids=entity_ids,
            access_scope="writer_safe",
            allow_plan=False,
            planner_may_read_plan=True,
            retrieval_may_return_plan=False,
            claim_may_cite_plan=False,
            legacy_allow_plan=False,
            why_needed=request.purpose,
            risk_level=NeedRisk.HIGH if request.mandatory_suggestion else NeedRisk.MEDIUM,
            requirement=(
                RequirementLevel.MANDATORY
                if request.mandatory_suggestion
                else RequirementLevel.OPTIONAL
            ),
            preferred_resolution_path=(
                ResolutionPath.EXACT_TEMPORAL if entity_ids else ResolutionPath.ANCHOR_FIRST
            ),
            allowed_candidate_pools=(
                CandidatePool.R1,
                CandidatePool.ANCHOR,
                CandidatePool.GROUNDED,
            ),
            expected_evidence_types=(request.requested_evidence_type,),
            stop_condition="one cutoff-valid claim closes the Writer question",
            purpose=request.purpose,
            expected_section=(
                WriterContextSection.CAUSAL_HISTORY
                if history
                else WriterContextSection.CURRENT_WORLD_STATE
            ),
            priority=90 if request.mandatory_suggestion else 70,
            query_hints=(request.blocked_action,),
            completion_criteria="answer is supported by cutoff-valid evidence",
        )

    def _terminal_delta(
        self,
        loop_request: WritingLoopRequest,
        view: AgentContextView,
        requests: tuple[WriterMemoryRequest, ...],
        fingerprint: ArtifactId,
        status: ContextDeltaStatus,
    ) -> ReactiveMemoryResult:
        request_ref = self._artifacts.put(
            canonical_json_bytes([item.model_dump(mode="json") for item in requests]),
            WRITER_MEMORY_REQUEST_MEDIA_TYPE,
            self._schema_version,
        )
        resolution_ref = self._artifacts.put(
            canonical_json_bytes({"status": status.value, "request": fingerprint.root}),
            WRITER_MEMORY_RESOLUTION_MEDIA_TYPE,
            self._schema_version,
        )
        delta = ContextDelta(
            delta_id=StableId(f"context-delta.{fingerprint.root[-32:]}"),
            request_ref=request_ref,
            resolution_ref=resolution_ref,
            parent_view_revision=view.revision,
            base_commit=loop_request.base_commit,
            snapshot_id=loop_request.snapshot_id,
            profile_ref=loop_request.project_profile_artifact,
            plan_ref=loop_request.accepted_plan.artifact,
            unresolved_need_ids=tuple(item.request_id for item in requests),
            token_impact=0,
            status=status,
        )
        return ReactiveMemoryResult(delta=delta, request_fingerprint=fingerprint, needs=())

    @staticmethod
    def _validate_basis(
        loop_request: WritingLoopRequest,
        view: AgentContextView,
        inputs: ReactiveMemoryInputs,
    ) -> None:
        template = inputs.resolution_template
        if (
            view.run_id != loop_request.run_id
            or view.task_id != loop_request.task_id
            or inputs.world.source_commit != loop_request.base_commit
            or inputs.task.task_id != loop_request.writer_context_package.task_contract.task_id
            or template.base_commit != loop_request.base_commit
            or template.snapshot_id != loop_request.snapshot_id
            or template.access_scope is not AccessScope.WRITER_SAFE
            or template.allow_future_plan
        ):
            raise WriterReactiveMemoryError(
                "reactive Memory inputs differ from the WritingLoop basis"
            )


__all__ = [
    "ReactiveMemoryInputs",
    "ReactiveMemoryResult",
    "WriterReactiveMemoryError",
    "WriterReactiveNeedAdapter",
]
