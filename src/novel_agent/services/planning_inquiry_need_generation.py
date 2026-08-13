"""Generate Stage 2 Memory Needs from an independently accepted Planner inquiry."""

from __future__ import annotations

import re

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    ExpectedClaimScope,
    FacetEvidenceRequirement,
    NeedCompletionSpec,
    NeedFacet,
    NeedFacetKind,
    NeedGapPolicy,
    NeedRisk,
    NeedUncertaintyPolicy,
    RequirementLevel,
    ResolutionPath,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.planning import (
    PlanningInquiry,
    PlanningProvenance,
    PlanningQuestion,
    PlanningQuestionKind,
    PlanReview,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.planning_memory import (
    EntityMention,
    GroundedNeedDraft,
    PlannedNeedDraft,
    PlannerNeedGenerationResult,
    RelationMention,
)
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    WriterContextSection,
)
from novel_agent.services.content_addressing import content_id
from novel_agent.services.need_draft_grounder import NeedDraftGrounder
from novel_agent.services.need_query_compiler import NeedQueryCompiler
from novel_agent.services.need_validator import NeedValidator
from novel_agent.services.task_focus import TaskFocusExtractor


class PlanningInquiryNeedError(ValueError):
    pass


class PlanningInquiryConditionedNeedGenerator:
    """Reviewed inquiry -> Grounder -> Validator -> executable query bundles."""

    version = "planning_inquiry_conditioned_need.v1"

    def __init__(self, *, max_total_needs: int = 24) -> None:
        if max_total_needs < 1:
            raise ValueError("Planner Need budget must be positive")
        self._grounder = NeedDraftGrounder()
        self._validator = NeedValidator(max_total_needs=max_total_needs)
        self._max_total_needs = max_total_needs
        self._focus = TaskFocusExtractor()
        self._compiler = NeedQueryCompiler()

    def generate(
        self,
        *,
        inquiry: PlanningInquiry,
        inquiry_ref: ArtifactRef,
        review: PlanReview,
        review_ref: ArtifactRef,
        world: WorldRootDocument,
        run_id: RunId,
        task_id: TaskId,
        reviewer_bound: bool = False,
        exclude_question_ids: tuple[StableId, ...] = (),
    ) -> PlannerNeedGenerationResult:
        if review.target_kind is not ReviewTargetKind.INQUIRY and not (
            reviewer_bound and review.target_kind is ReviewTargetKind.PLAN_PROPOSAL
        ):
            raise PlanningInquiryNeedError("Planner Need generation requires an inquiry review")
        if review.decision is not ReviewDecision.ACCEPT and not (
            reviewer_bound
            and review.decision is ReviewDecision.REVISE
            and review.memory_gap_questions
        ):
            raise PlanningInquiryNeedError("Planner Need generation requires an accepted inquiry")
        if not reviewer_bound and review.target_artifact_ref != inquiry_ref:
            raise PlanningInquiryNeedError("inquiry review target differs from supplied inquiry")
        if inquiry.mode.value == "project_bootstrap":
            raise PlanningInquiryNeedError("PROJECT_BOOTSTRAP must not call commit-scoped Memory")
        start = inquiry.horizon_start or 1
        end = inquiry.horizon_end or start
        task = BenchmarkTaskContract(
            task_id=StableId(task_id.root),
            task_text=" ".join(goal.summary for goal in inquiry.goal_proposals),
            checkpoint_chapter=max(0, start - 1),
            target_chapter_start=start,
            target_chapter_end=end,
            information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            task_template_version="stage4.planning-inquiry.v1",
            output_contract_version="stage1-memory-need.v1",
            task_intent=" ".join(goal.summary for goal in inquiry.goal_proposals),
            planning_context_ref=inquiry_ref.artifact_id,
            planning_context_hash=inquiry_ref.artifact_id,
        )
        goal_by_id = {goal.goal_id: goal for goal in inquiry.goal_proposals}
        drafts: list[PlannedNeedDraft] = []
        question_by_draft: dict[str, PlanningQuestion] = {}
        rejected: dict[str, str] = {}
        excluded = set(exclude_question_ids)
        questions = tuple(
            question
            for question in (*inquiry.assumptions, *inquiry.questions)
            if question.question_id not in excluded
        )
        for question in questions:
            if question.kind is PlanningQuestionKind.HUMAN_CHOICE:
                rejected[question.question_id.root] = "human_choice_is_not_a_memory_fact"
                continue
            goal = goal_by_id.get(question.goal_id)
            if goal is None:
                rejected[question.question_id.root] = "unknown_goal_lineage"
                continue
            draft = self._draft(question, goal.summary, world, start=start, end=end)
            drafts.append(draft)
            question_by_draft[draft.draft_id] = question
        grounded = tuple(self._grounder.ground(draft, world) for draft in drafts)
        focus = self._focus.extract(task, world)
        grounded_ids = tuple(
            dict.fromkeys(
                entity_id
                for draft in grounded
                for entity_id in self._grounder.grounded_entity_ids(draft)
            )
        )
        if grounded_ids:
            focus = self._focus.extend(focus, grounded_ids)
        goal_bindings = {
            chapter: self._normalize(goal.summary)
            for chapter in range(start, end + 1)
            for goal in inquiry.goal_proposals[:1]
        }
        # Each question carries its own goal text.  Validator accepts the full
        # range only when the first reviewed goal is the selected horizon goal;
        # other goal-bound questions are validated in their own one-question pass.
        accepted_drafts: list[GroundedNeedDraft] = []
        validation_reasons: dict[str, str] = {}
        for grounded_draft in grounded:
            question = question_by_draft[grounded_draft.draft_id]
            goal = goal_by_id[question.goal_id]
            per_question_bindings = {
                chapter: self._normalize(goal.summary) for chapter in range(start, end + 1)
            }
            validation = self._validator.validate(
                drafts=(grounded_draft,),
                task=task,
                world=world,
                focus_set=focus,
                goal_bindings=per_question_bindings,
            )
            if validation.accepted_drafts:
                accepted_drafts.extend(validation.accepted_drafts)
            else:
                validation_reasons[question.question_id.root] = validation.rejected_reasons.get(
                    grounded_draft.draft_id,
                    "deterministic_validation_rejected",
                )
        del goal_bindings
        rejected.update(validation_reasons)
        # Per-question validation preserves the exact goal binding.  Apply the
        # product-wide tranche and dedup only after every question has passed
        # that validation, so the validator cap cannot reset for each question.
        ordered_valid = sorted(
            accepted_drafts,
            key=lambda item: (not question_by_draft[item.draft_id].blocking),
        )
        selected: list[GroundedNeedDraft] = []
        deferred: list[StableId] = []
        seen_keys: set[tuple[str, tuple[str, ...], str]] = set()
        for validated_draft in ordered_valid:
            question = question_by_draft[validated_draft.draft_id]
            key = (
                self._normalize(validated_draft.semantic_question),
                tuple(
                    item.root
                    for item in self._grounder.grounded_entity_ids(validated_draft)
                ),
                NeedValidator.need_type_for_facets(validated_draft.suggested_facets),
            )
            if key in seen_keys:
                rejected[question.question_id.root] = "duplicate_reviewed_memory_question"
                continue
            seen_keys.add(key)
            if len(selected) >= self._max_total_needs:
                deferred.append(question.question_id)
                continue
            selected.append(validated_draft)

        provisional = tuple(
            self._build_need(
                draft=draft,
                question=question_by_draft[draft.draft_id],
                inquiry_ref=inquiry_ref,
                world=world,
                run_id=run_id,
                task_id=task_id,
                start=start,
                end=end,
                validated_hash=ArtifactId("sha256:" + "0" * 64),
            )
            for draft in selected
        )
        identity_payload = tuple(
            need.model_dump(
                mode="json",
                exclude={"validated_need_set_hash"},
            )
            for need in provisional
        )
        validated_hash = content_id(
            {
                "version": self.version,
                "inquiry": inquiry_ref.artifact_id.root,
                "needs": identity_payload,
            }
        )
        needs = tuple(
            need.model_copy(update={"validated_need_set_hash": validated_hash})
            for need in provisional
        )
        bundles = {need.need_id.root: self._compiler.compile(need) for need in needs}
        return PlannerNeedGenerationResult(
            inquiry_ref=inquiry_ref,
            inquiry_review_ref=review_ref,
            needs=needs,
            query_bundles=bundles,
            selected_question_ids=tuple(
                question_by_draft[draft.draft_id].question_id for draft in selected
            ),
            rejected_question_ids=tuple(StableId(item) for item in sorted(rejected)),
            deferred_question_ids=tuple(deferred),
            rejection_reasons=rejected,
            validated_need_set_hash=validated_hash,
            generator_version=self.version,
        )

    def _draft(
        self,
        question: PlanningQuestion,
        goal_summary: str,
        world: WorldRootDocument,
        *,
        start: int,
        end: int,
    ) -> PlannedNeedDraft:
        labels = list(question.entity_labels)
        folded = question.question.casefold()
        for entity in world.entities:
            if any(
                label and label.casefold() in folded
                for label in (entity.internal_label, *entity.aliases)
            ):
                labels.append(entity.internal_label)
        relation_mentions: tuple[RelationMention, ...] = ()
        if question.relation_subject is not None:
            relation_mentions = (
                RelationMention(
                    subject_label=question.relation_subject,
                    relation_label=str(question.relation_predicate),
                    object_label=str(question.relation_object),
                ),
            )
            labels.extend((question.relation_subject, str(question.relation_object)))
        facets, scopes = self._facet_contract(question.kind)
        return PlannedNeedDraft(
            draft_id=question.question_id.root,
            semantic_question=question.question,
            entity_mentions=tuple(
                EntityMention(label=label, role_in_need="reviewed_inquiry_anchor")
                for label in dict.fromkeys(labels)
                if label
            ),
            relation_mentions=relation_mentions,
            trigger_plan_chapters=tuple(range(start, end + 1)),
            trigger_plan_goal=goal_summary,
            why_needed="reviewed Planner inquiry requires historical evidence",
            required_claim_scopes=scopes,
            suggested_facets=facets,
            historical_time_scope="main",
            query_hints=(question.question,),
        )

    @staticmethod
    def _facet_contract(kind: PlanningQuestionKind) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if kind is PlanningQuestionKind.FACT:
            return (("CURRENT_STATE",), ("current",))
        if kind is PlanningQuestionKind.RELATION_CAUSAL:
            return (("RELATION_STATE", "CAUSAL_HISTORY"), ("current", "historical"))
        if kind is PlanningQuestionKind.OBLIGATION_PACING:
            return (("COMMITMENT", "UNRESOLVED_STATUS"), ("historical", "current"))
        return (("SETUP",), ("historical",))

    def _build_need(
        self,
        *,
        draft: GroundedNeedDraft,
        question: PlanningQuestion,
        inquiry_ref: ArtifactRef,
        world: WorldRootDocument,
        run_id: RunId,
        task_id: TaskId,
        start: int,
        end: int,
        validated_hash: ArtifactId,
    ) -> Stage1MemoryNeed:
        grounded = draft
        entity_ids = self._grounder.grounded_entity_ids(grounded)
        digest = content_id(
            {
                "inquiry": inquiry_ref.artifact_id.root,
                "question": question.model_dump(mode="json"),
            }
        ).root.removeprefix("sha256:")[:24]
        need_id = StableId(f"need.stage4.planner.{digest}")
        facet_kinds = tuple(NeedFacetKind[item] for item in grounded.suggested_facets)
        facets = tuple(
            NeedFacet(
                need_facet_id=StableId(f"facet.{need_id.root}.{kind.value}"[:128]),
                need_id=need_id,
                facet_kind=kind,
                expected_claim_scope=self._scope_for_facet(kind),
                derivation_refs=(question.question_id, question.goal_id),
                producer="planning_inquiry_conditioned_need_generator",
                producer_version=self.version,
                information_scope="author_planning",
            )
            for kind in facet_kinds
        )
        facet_ids = tuple(item.need_facet_id for item in facets)
        completion = NeedCompletionSpec(
            need_id=need_id,
            required_need_facet_ids=facet_ids,
            irreducible_need_facet_ids=facet_ids,
            evidence_requirement_by_facet={
                item.root: FacetEvidenceRequirement.TRACEABLE_CUTOFF_SOURCE for item in facet_ids
            },
            require_current_claim=any(
                item in {NeedFacetKind.CURRENT_STATE, NeedFacetKind.RELATION_STATE}
                for item in facet_kinds
            ),
            require_causal_history=NeedFacetKind.CAUSAL_HISTORY in facet_kinds,
            uncertainty_policy=NeedUncertaintyPolicy.REJECT_UNVERIFIED_CLAIM,
            gap_policy=NeedGapPolicy.EMIT_TYPED_GAP,
            producer="planning_inquiry_conditioned_need_generator",
            producer_version=self.version,
        )
        intent, pools, section = self._routing(question.kind)
        plan_related = question.provenance.provenance is PlanningProvenance.ACCEPTED_PLAN_DERIVED
        return Stage1MemoryNeed(
            need_id=need_id,
            run_id=run_id,
            task_id=task_id,
            base_commit=world.source_commit,
            horizon_target=(start, end),
            need_type=f"planner_{question.kind.value}",
            query_intent=intent,
            query_text=grounded.semantic_question,
            entity_ids=entity_ids,
            access_scope="author_planning",
            allow_plan=plan_related,
            planner_may_read_plan=True,
            retrieval_may_return_plan=plan_related,
            claim_may_cite_plan=plan_related,
            legacy_allow_plan=plan_related,
            why_needed=grounded.why_needed,
            risk_level=NeedRisk.HIGH if question.blocking else NeedRisk.MEDIUM,
            requirement=(
                RequirementLevel.MANDATORY if question.blocking else RequirementLevel.OPTIONAL
            ),
            preferred_resolution_path=ResolutionPath.ANCHOR_FIRST,
            allowed_candidate_pools=pools,
            expected_evidence_types=("structured_record", "text_span", "graph_path_receipt"),
            stop_condition="all reviewed inquiry facets closed by cutoff-valid evidence",
            purpose=grounded.why_needed,
            expected_section=section,
            priority=95 if question.blocking else 80,
            query_hints=tuple(
                dict.fromkeys(
                    item for item in grounded.query_hints if item != grounded.semantic_question
                )
            ),
            completion_criteria="reviewed inquiry facets are traceably supported",
            need_facets=facets,
            completion_spec=completion,
            semantic_question=grounded.semantic_question,
            trigger_plan_chapters=grounded.trigger_plan_chapters,
            trigger_plan_goal=grounded.trigger_plan_goal,
            planner_artifact_ref=inquiry_ref.artifact_id,
            planned_draft_id=grounded.draft_id,
            validated_need_set_hash=validated_hash,
        )

    @staticmethod
    def _scope_for_facet(kind: NeedFacetKind) -> ExpectedClaimScope:
        if kind in {NeedFacetKind.CURRENT_STATE, NeedFacetKind.RELATION_STATE}:
            return ExpectedClaimScope.CURRENT
        if kind is NeedFacetKind.KNOWLEDGE_BOUNDARY:
            return ExpectedClaimScope.KNOWLEDGE
        return ExpectedClaimScope.HISTORICAL

    @staticmethod
    def _routing(
        kind: PlanningQuestionKind,
    ) -> tuple[Stage1QueryIntent, tuple[CandidatePool, ...], WriterContextSection]:
        if kind is PlanningQuestionKind.FACT:
            return (
                Stage1QueryIntent.CURRENT_STATE,
                (CandidatePool.R1, CandidatePool.ANCHOR, CandidatePool.GROUNDED),
                WriterContextSection.CURRENT_WORLD_STATE,
            )
        if kind is PlanningQuestionKind.RELATION_CAUSAL:
            return (
                Stage1QueryIntent.CAUSAL_MULTI_HOP,
                (CandidatePool.ANCHOR, CandidatePool.GRAPH, CandidatePool.GROUNDED),
                WriterContextSection.CAUSAL_HISTORY,
            )
        if kind is PlanningQuestionKind.OBLIGATION_PACING:
            return (
                Stage1QueryIntent.PLAN_OBLIGATION,
                (CandidatePool.R1, CandidatePool.ANCHOR, CandidatePool.HIERARCHY),
                WriterContextSection.PLAN_AND_OBLIGATIONS,
            )
        return (
            Stage1QueryIntent.STYLE_VOICE,
            (CandidatePool.GROUNDED,),
            WriterContextSection.LONG_RANGE_CALLBACKS,
        )

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(re.sub(r"[\s\u3000]+", " ", text).strip().casefold().split())
