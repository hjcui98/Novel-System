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
    RelationFacetBinding,
    RequirementLevel,
    ResolutionPath,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.planning import (
    PlanningInquiry,
    PlanningProblemIdentitySeed,
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
from novel_agent.services.retrieval import ROUTES
from novel_agent.services.task_focus import TaskFocusExtractor
from novel_agent.tools.retrieval import POOL_BY_CHANNEL

_FAMILY_SUFFIXES = ("家", "族", "氏")
_RELATION_BACKED_STATE_PREDICATES = frozenset({"location", "residence"})


class PlanningInquiryNeedError(ValueError):
    pass


class PlanningInquiryConditionedNeedGenerator:
    """Reviewed inquiry -> Grounder -> Validator -> executable query bundles."""

    version = "planning_inquiry_conditioned_need.v2"

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
        problem_identity_seed: PlanningProblemIdentitySeed | None = None,
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
        if problem_identity_seed is not None:
            seeded_questions = tuple(
                question
                for question in questions
                if question.question_id == problem_identity_seed.question_id
            )
            if len(seeded_questions) != 1:
                raise PlanningInquiryNeedError(
                    "problem identity seed question is not present exactly once"
                )
            if self._normalize(seeded_questions[0].question) != self._normalize(
                problem_identity_seed.need_query
            ):
                raise PlanningInquiryNeedError(
                    "problem identity seed query differs from the reviewed Planner question"
                )
            questions = seeded_questions
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
        grounded = tuple(
            self._compile_grounded_facets(
                self._grounder.ground(draft, world),
                question_by_draft,
                world,
            )
            for draft in drafts
        )
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
            if (
                question.relation_subject is not None
                and self._explicit_relation_binding(question, grounded_draft) is None
            ):
                # An explicit relation is never widened to every predicate
                # touching either endpoint when one endpoint is ambiguous or
                # unresolved.  Keep the question auditable, but reject this
                # Need before validation so no broad fallback can close it.
                validation_reasons[question.question_id.root] = (
                    "explicit_relation_endpoint_unresolved"
                )
                continue
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
                tuple(item.root for item in self._grounder.grounded_entity_ids(validated_draft)),
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
                problem_identity_seed=problem_identity_seed,
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
        labels.extend(self._mentioned_world_labels(question.question, world))
        folded = question.question.casefold()
        entity_by_id = {entity.entity_id: entity for entity in world.entities}
        for relation in world.relations:
            if relation.predicate and relation.predicate.casefold() in folded:
                for endpoint_id in (relation.subject_id, relation.object_id):
                    endpoint = entity_by_id.get(endpoint_id)
                    if endpoint is not None:
                        labels.append(endpoint.internal_label)
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
            required_claim_scopes=("current",),
            suggested_facets=("CURRENT_STATE",),
            historical_time_scope="main",
            query_hints=(question.question,),
        )

    def _compile_grounded_facets(
        self,
        draft: GroundedNeedDraft,
        question_by_draft: dict[str, PlanningQuestion],
        world: WorldRootDocument,
    ) -> GroundedNeedDraft:
        question = question_by_draft[draft.draft_id]
        facet_kinds = self._compile_facets(draft, question, world)
        scopes = tuple(
            "current"
            if kind in {NeedFacetKind.CURRENT_STATE, NeedFacetKind.RELATION_STATE}
            else "historical"
            for kind in facet_kinds
        )
        return draft.model_copy(
            update={
                "suggested_facets": tuple(kind.name for kind in facet_kinds),
                "required_claim_scopes": scopes,
            }
        )

    def _compile_facets(
        self,
        draft: GroundedNeedDraft,
        question: PlanningQuestion,
        world: WorldRootDocument,
    ) -> tuple[NeedFacetKind, ...]:
        if question.kind is PlanningQuestionKind.OBLIGATION_PACING:
            return (NeedFacetKind.COMMITMENT, NeedFacetKind.UNRESOLVED_STATUS)
        if question.kind is PlanningQuestionKind.STYLE_REFERENCE:
            return (NeedFacetKind.SETUP,)
        change = self._expresses_change_or_cause(draft.semantic_question)
        facets: list[NeedFacetKind]
        if question.relation_subject is not None:
            # Structured relation fields are authoritative even when the
            # natural-language question also mentions a scalar state
            # predicate.  Routing it through CURRENT_STATE would discard the
            # ordered triple and re-open the old predicate/entity widening.
            facets = [NeedFacetKind.RELATION_STATE]
            if change:
                facets.append(NeedFacetKind.CAUSAL_HISTORY)
            return tuple(facets)
        entity_ids = self._grounder.grounded_entity_ids(draft)
        explicit_state_predicates = self._explicit_state_predicates(
            draft.semantic_question,
            world,
            entity_ids,
        )
        if explicit_state_predicates:
            # A Planner question can mention several entities while still
            # asking for scalar state fields (for example
            # ``current_location`` and ``physical_state``).  Entity count is
            # not sufficient evidence for a graph relation route in that
            # case; the named canonical predicates are the stronger signal.
            # ``location`` and ``residence`` are the two state predicates
            # explicitly mapped to canonical World relations by the Stage 2M
            # PredicateRegistry.  Route those named predicates through the
            # relation owner so a maintenance finding can create the missing
            # ``located_at``/``resides_at`` edge.  Synthetic scalar names such
            # as ``current_location`` remain ordinary CURRENT_STATE fields.
            relation_backed = {
                predicate.casefold()
                for predicate in explicit_state_predicates
                if predicate.casefold() in _RELATION_BACKED_STATE_PREDICATES
            }
            facets = [
                NeedFacetKind.RELATION_STATE if relation_backed else NeedFacetKind.CURRENT_STATE
            ]
        elif len(entity_ids) >= 2:
            facets = [NeedFacetKind.RELATION_STATE]
        elif change:
            # Single-entity 政治反弹/backlash is causal history, not CURRENT_STATE.
            # CURRENT_STATE on an org like 天海家 selects evidence-less identity and L0=0.
            facets = [NeedFacetKind.CAUSAL_HISTORY]
        else:
            facets = [NeedFacetKind.CURRENT_STATE]
        if change and NeedFacetKind.CAUSAL_HISTORY not in facets:
            facets.append(NeedFacetKind.CAUSAL_HISTORY)
        return tuple(dict.fromkeys(facets))

    @staticmethod
    def _explicit_state_predicates(
        question: str,
        world: WorldRootDocument,
        entity_ids: tuple[StableId, ...],
    ) -> tuple[str, ...]:
        """Return canonical scalar predicates named by this reviewed question.

        The Planner world summary can add family members and state-literal
        entity anchors, so a multi-entity draft does not necessarily ask for
        a graph relation.  Only predicates already present on the grounded
        entities are considered, and matching is token-aware for identifier
        predicates while retaining substring matching for non-Latin registry
        names.
        """

        folded = question.casefold()
        grounded = set(entity_ids)
        predicates: list[str] = []
        for state in world.states:
            predicate = state.predicate.strip()
            if not predicate or state.subject_id not in grounded:
                continue
            normalized = predicate.casefold()
            if re.fullmatch(r"[a-z0-9_]+", normalized):
                mentioned = re.search(
                    rf"(?<![a-z0-9_]){re.escape(normalized)}(?![a-z0-9_])",
                    folded,
                )
            else:
                mentioned = re.search(re.escape(normalized), folded)
            if mentioned:
                predicates.append(predicate)
        return tuple(dict.fromkeys(predicates))

    @staticmethod
    def _expresses_change_or_cause(question: str) -> bool:
        folded = question.casefold()
        return any(
            marker.casefold() in folded
            for marker in (
                "后果",
                "结果",
                "原因",
                "后是否",
                "反弹",
                "旧案",
                "废弃",
                "重审",
                "consequence",
                "consequences",
                "cause",
                "caused",
                "backlash",
                "old case",
                "abandonment",
                "abandoned",
                "re-litig",
                "relitig",
                "sanction",
                "threat",
            )
        )

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
        problem_identity_seed: PlanningProblemIdentitySeed | None = None,
    ) -> Stage1MemoryNeed:
        grounded = draft
        explicit_relation = self._explicit_relation_binding(question, grounded)
        if question.relation_subject is not None and explicit_relation is None:
            raise PlanningInquiryNeedError(
                "explicit relation requires two deterministically grounded endpoints"
            )
        entity_ids: tuple[StableId, ...]
        if explicit_relation is not None:
            # An explicit relation question is a complete ordered triple.  Do
            # not add bounded mention-closure or family-member expansion to
            # its graph seed set: either would let a relation on an unrelated
            # endpoint appear to answer the reviewed question.
            entity_ids = (explicit_relation.subject_id, explicit_relation.object_id)
            labels = self._grounded_labels(world, entity_ids)
        else:
            entity_ids = self._grounder.grounded_entity_ids(grounded)
            labels = self._grounded_labels(world, entity_ids)
            entity_ids = tuple(
                dict.fromkeys((*entity_ids, *self._family_member_ids(world, entity_ids)))
            )
        original = question.question
        digest = content_id(
            {
                "inquiry": inquiry_ref.artifact_id.root,
                "question": question.model_dump(mode="json"),
            }
        ).root.removeprefix("sha256:")[:24]
        need_id = (
            problem_identity_seed.need_id
            if problem_identity_seed is not None
            else StableId(f"need.stage4.planner.{digest}")
        )
        facet_kinds = tuple(NeedFacetKind[item] for item in grounded.suggested_facets)
        if problem_identity_seed is not None:
            seeded_facet = problem_identity_seed.facet
            if seeded_facet not in facet_kinds:
                raise PlanningInquiryNeedError(
                    "problem identity seed facet differs from deterministic Need routing"
                )
            facet_kinds = (seeded_facet,)
            semantic_question = problem_identity_seed.semantic_question
        else:
            semantic_question = (
                self._facet_semantic_question(facet_kinds[0], labels, original)
                if facet_kinds
                else original
            )
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
        # FacetSupportEvaluator is deliberately fail-closed: a structured
        # anchor can close a facet only when the Need declares the predicates
        # that the facet represents.  The inquiry-conditioned generator used
        # to omit this contract, so even an exact relation/state anchor was
        # treated as semantically unsupported and every real Planner run
        # stopped at MANDATORY_MEMORY_FACETS_UNRESOLVED.  Derive the bindings
        # from the immutable world snapshot already supplied to this method;
        # this does not add facts or consult model output.
        state_predicates = tuple(
            dict.fromkeys(
                state.predicate
                for state in world.states
                if state.subject_id in set(entity_ids) and state.predicate
            )
        )[:16]
        relation_predicates = tuple(
            dict.fromkeys(
                relation.predicate
                for relation in world.relations
                if relation.predicate
                and bool({relation.subject_id, relation.object_id} & set(entity_ids))
            )
        )[:16]
        # A reviewed question may name a canonical relation predicate (for
        # example ``located_at``).  Keep that explicit predicate narrow; when
        # the question uses natural relation wording (for example “婚约关系”)
        # retain the bounded relation predicate set for the grounded entities.
        mentioned_relation_predicates = tuple(
            predicate
            for predicate in relation_predicates
            if self._predicate_is_mentioned(original, predicate)
            or self._predicate_is_mentioned(semantic_question, predicate)
        )
        relation_binding = (
            (explicit_relation.predicate,)
            if explicit_relation is not None
            else mentioned_relation_predicates or relation_predicates
        )
        event_predicates = tuple(
            dict.fromkeys(
                event.event_type
                for event in world.events
                if event.event_type and set(event.participant_ids) & set(entity_ids)
            )
        )[:16]
        obligation_predicates = tuple(
            dict.fromkeys(
                obligation.kind.value
                for obligation in world.obligations
                if set(obligation.owner_ids) & set(entity_ids)
            )
        )[:16]
        facet_predicates: dict[NeedFacetKind, tuple[str, ...]] = {
            NeedFacetKind.CURRENT_STATE: state_predicates,
            NeedFacetKind.CAPABILITY_STATUS: state_predicates,
            NeedFacetKind.LIMITATION: state_predicates,
            NeedFacetKind.KNOWLEDGE_BOUNDARY: state_predicates,
            NeedFacetKind.RELATION_STATE: relation_binding,
            NeedFacetKind.CAUSAL_HISTORY: tuple(
                dict.fromkeys((*state_predicates, *event_predicates))
            ),
            NeedFacetKind.SETUP: tuple(dict.fromkeys((*state_predicates, *event_predicates))),
            NeedFacetKind.COMMITMENT: obligation_predicates,
            NeedFacetKind.UNRESOLVED_STATUS: obligation_predicates,
            NeedFacetKind.PLAN_NODE: (),
        }
        relation_bindings_by_facet: dict[str, tuple[RelationFacetBinding, ...]] = {
            facet.need_facet_id.root: (explicit_relation,)
            for facet in facets
            if explicit_relation is not None and facet.facet_kind is NeedFacetKind.RELATION_STATE
        }
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
            predicates_by_facet={
                facet.need_facet_id.root: tuple(
                    dict.fromkeys(facet_predicates.get(facet.facet_kind, ()))
                )
                for facet in facets
            },
            relation_bindings_by_facet=relation_bindings_by_facet,
        )
        need_predicates = tuple(
            dict.fromkeys(
                predicate
                for facet_kind in facet_kinds
                for predicate in facet_predicates.get(facet_kind, ())
            )
        )
        intent, pools, section = self._routing(facet_kinds, structured_kind=question.kind)
        plan_related = question.provenance.provenance is PlanningProvenance.ACCEPTED_PLAN_DERIVED
        return Stage1MemoryNeed(
            need_id=need_id,
            run_id=run_id,
            task_id=task_id,
            base_commit=world.source_commit,
            horizon_target=(start, end),
            need_type=f"planner_{question.kind.value}",
            query_intent=intent,
            query_text=original,
            entity_ids=entity_ids,
            predicates=need_predicates,
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
                    item
                    for item in grounded.query_hints
                    if item not in {original, semantic_question}
                )
            ),
            completion_criteria="reviewed inquiry facets are traceably supported",
            need_facets=facets,
            completion_spec=completion,
            semantic_question=semantic_question,
            trigger_plan_chapters=grounded.trigger_plan_chapters,
            trigger_plan_goal=grounded.trigger_plan_goal,
            planner_artifact_ref=inquiry_ref.artifact_id,
            planned_draft_id=grounded.draft_id,
            validated_need_set_hash=validated_hash,
        )

    @staticmethod
    def _explicit_relation_binding(
        question: PlanningQuestion,
        grounded: GroundedNeedDraft,
    ) -> RelationFacetBinding | None:
        """Return the exact ordered triple for a reviewed relation question.

        Endpoint ids are deliberately taken from the grounder's relation
        result, not reconstructed from the broad entity mention closure.  A
        missing result is an unresolved question and must not be widened into
        a predicate/entity OR-set.
        """

        if not all(
            item is not None
            for item in (
                question.relation_subject,
                question.relation_predicate,
                question.relation_object,
            )
        ):
            return None
        subject_label = question.relation_subject
        predicate = question.relation_predicate
        object_label = question.relation_object
        assert subject_label is not None
        assert predicate is not None
        assert object_label is not None
        normalized = NeedDraftGrounder._normalize
        matches = tuple(
            mention
            for mention in grounded.relation_mentions
            if normalized(mention.subject_label) == normalized(subject_label)
            and mention.relation_label == predicate
            and normalized(mention.object_label) == normalized(object_label)
            and mention.subject_id is not None
            and mention.object_id is not None
        )
        if len(matches) != 1:
            return None
        mention = matches[0]
        assert mention.subject_id is not None and mention.object_id is not None
        return RelationFacetBinding(
            subject_id=mention.subject_id,
            predicate=predicate,
            object_id=mention.object_id,
        )

    @staticmethod
    def _predicate_is_mentioned(question: str, predicate: str) -> bool:
        """Match an explicit world predicate in a reviewed question.

        Identifier-like predicates use token boundaries so ``state`` does not
        accidentally match ``stateful``; non-Latin registry names retain
        substring matching because they have no word-boundary syntax.
        """

        normalized = predicate.strip().casefold()
        folded = question.casefold()
        if not normalized:
            return False
        if re.fullmatch(r"[a-z0-9_]+", normalized):
            return (
                re.search(
                    rf"(?<![a-z0-9_]){re.escape(normalized)}(?![a-z0-9_])",
                    folded,
                )
                is not None
            )
        return normalized in folded

    @classmethod
    def _mentioned_world_labels(cls, question: str, world: WorldRootDocument) -> tuple[str, ...]:
        """Longest-span mention wins so 短剑 does not also ground generic 剑."""

        folded = question.casefold()
        hits: list[tuple[int, int, str]] = []
        for entity in world.entities:
            for label in (entity.internal_label, *entity.aliases):
                needle = label.casefold().strip()
                if not needle:
                    continue
                start = 0
                while True:
                    index = folded.find(needle, start)
                    if index < 0:
                        break
                    hits.append((index, index + len(needle), entity.internal_label))
                    start = index + max(len(needle), 1)
        hits.sort(key=lambda item: (item[0], item[0] - item[1]))
        kept: list[tuple[int, int, str]] = []
        for start, end, label in hits:
            if any(
                start >= other[0] and end <= other[1] and end - start < other[1] - other[0]
                for other in kept
            ):
                continue
            kept = [
                other
                for other in kept
                if not (other[0] >= start and other[1] <= end and other[1] - other[0] < end - start)
            ]
            kept.append((start, end, label))
        return tuple(
            dict.fromkeys(
                (
                    *(label for _start, _end, label in kept),
                    *cls._family_org_labels(question, world),
                )
            )
        )

    @staticmethod
    def _latin_id_token(entity_id: str) -> str | None:
        raw = entity_id.removeprefix("entity.")
        if raw.startswith("graph."):
            return None
        token = raw.split("-", 1)[0]
        if token.isalpha() and len(token) >= 4:
            return token
        return None

    @classmethod
    def _family_org_labels(cls, question: str, world: WorldRootDocument) -> tuple[str, ...]:
        """Map 'Tianhai family' to 天海家 via entity.tianhai-* members, not empty aliases."""

        folded = question.casefold()
        if not any(word in folded for word in ("family", "clan", "house")):
            return ()
        by_token: dict[str, list[str]] = {}
        org_labels = {
            entity.internal_label
            for entity in world.entities
            if entity.entity_type in {"organization", "org"}
        }
        for entity in world.entities:
            token = cls._latin_id_token(entity.entity_id.root)
            if token is None or not re.search(rf"\b{re.escape(token)}\b", folded):
                continue
            by_token.setdefault(token, []).append(entity.internal_label)
        found: list[str] = []
        for labels in by_token.values():
            prefix = labels[0]
            for label in labels[1:]:
                while prefix and not label.startswith(prefix):
                    prefix = prefix[:-1]
            if len(prefix) < 2:
                continue
            for candidate in (prefix, f"{prefix}家"):
                if candidate in org_labels:
                    found.append(candidate)
        return tuple(dict.fromkeys(found))

    @classmethod
    def _family_member_ids(
        cls,
        world: WorldRootDocument,
        entity_ids: tuple[StableId, ...],
    ) -> tuple[StableId, ...]:
        """Expand a grounded family org to its existing latin-id members for retrieval.

        The organization remains the only semantic entity label.  Member IDs are
        query seeds because the frozen Anchor projection stores evidence under
        ``entity.<family>-*`` members, not under the organization row.  This is a
        retrieval expansion only: it never mutates the WorldRoot or writes an alias.
        """

        by_id = {entity.entity_id: entity for entity in world.entities}
        stems = tuple(
            entity.internal_label[: -len(suffix)]
            for entity_id in entity_ids
            if (entity := by_id.get(entity_id)) is not None
            for suffix in _FAMILY_SUFFIXES
            if entity.internal_label.endswith(suffix) and len(entity.internal_label) > len(suffix)
        )
        if not stems:
            return ()
        return tuple(
            entity.entity_id
            for entity in world.entities
            if entity.entity_id not in entity_ids
            and cls._latin_id_token(entity.entity_id.root) is not None
            and any(entity.internal_label.startswith(stem) for stem in stems)
        )

    @staticmethod
    def _grounded_labels(
        world: WorldRootDocument,
        entity_ids: tuple[StableId, ...],
    ) -> tuple[str, ...]:
        by_id = {entity.entity_id: entity.internal_label for entity in world.entities}
        return tuple(by_id[item] for item in entity_ids if item in by_id and by_id[item].strip())

    @staticmethod
    def _facet_semantic_question(
        kind: NeedFacetKind,
        labels: tuple[str, ...],
        original: str,
    ) -> str:
        named = "、".join(labels) if labels else original
        if kind is NeedFacetKind.RELATION_STATE:
            scoped = f"{named} 的当前关系状态是什么?"
        elif kind is NeedFacetKind.CURRENT_STATE:
            scoped = f"{named} 的当前状态是什么?"
        elif kind is NeedFacetKind.CAUSAL_HISTORY:
            scoped = f"{named} 相关的原因或后果是什么?"
        else:
            return original
        if original.strip() and original.strip() != scoped:
            return f"{scoped} 具体问题: {original}"
        return scoped

    @staticmethod
    def _scope_for_facet(kind: NeedFacetKind) -> ExpectedClaimScope:
        if kind in {NeedFacetKind.CURRENT_STATE, NeedFacetKind.RELATION_STATE}:
            return ExpectedClaimScope.CURRENT
        if kind is NeedFacetKind.KNOWLEDGE_BOUNDARY:
            return ExpectedClaimScope.KNOWLEDGE
        return ExpectedClaimScope.HISTORICAL

    @staticmethod
    def _routing(
        facet_kinds: tuple[NeedFacetKind, ...],
        *,
        structured_kind: PlanningQuestionKind | None = None,
    ) -> tuple[Stage1QueryIntent, tuple[CandidatePool, ...], WriterContextSection]:
        if structured_kind is PlanningQuestionKind.OBLIGATION_PACING:
            intent = Stage1QueryIntent.PLAN_OBLIGATION
            section = WriterContextSection.PLAN_AND_OBLIGATIONS
        elif structured_kind is PlanningQuestionKind.STYLE_REFERENCE:
            intent = Stage1QueryIntent.STYLE_VOICE
            section = WriterContextSection.LONG_RANGE_CALLBACKS
        elif NeedFacetKind.CAUSAL_HISTORY in facet_kinds:
            intent = Stage1QueryIntent.CAUSAL_MULTI_HOP
            section = WriterContextSection.CAUSAL_HISTORY
        elif NeedFacetKind.RELATION_STATE in facet_kinds:
            intent = Stage1QueryIntent.RELATION_CHAIN
            section = WriterContextSection.RELATIONSHIP_AND_EMOTION
        else:
            intent = Stage1QueryIntent.CURRENT_STATE
            section = WriterContextSection.CURRENT_WORLD_STATE
        route = ROUTES[intent]
        pools = tuple(
            dict.fromkeys(
                POOL_BY_CHANNEL[channel]
                for channel in (*route.channels, *route.fallback_channels)
                if channel in POOL_BY_CHANNEL
            )
        )
        if not pools:
            pools = (CandidatePool.ANCHOR,)
        return intent, pools, section

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(re.sub(r"[\s\u3000]+", " ", text).strip().casefold().split())
