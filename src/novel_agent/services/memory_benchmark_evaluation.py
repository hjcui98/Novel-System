"""Freeze-gated, per-Gold evaluation of writer-facing memory contexts."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import ClassVar, cast

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import ChapterGoal, GoldItem
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory import Stage1MemoryNeed
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    ContentAddressedGoldMetricDescriptor,
    ContextAssemblyStatus,
    EvidenceLedger,
    FiveSegmentReport,
    FreezeReceipt,
    GoldBlindness,
    GoldMatchStatus,
    GoldNeedBinding,
    GoldNeedSpec,
    MemoryBenchmarkEvaluationReport,
    PerGoldComparison,
    PerGoldStageLossDiagnostic,
    SegmentAvailability,
    WriterContextItem,
    WriterContextPackage,
)
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.services.benchmark_importer import content_id
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatcher
from novel_agent.services.memory_benchmark_metric_contracts import (
    GATE_METRIC_FORMULA_HASH,
    GATE_METRIC_FORMULA_VERSION,
)
from novel_agent.services.model_gateway import ModelGateway


class SemanticSupport(StrEnum):
    SUPPORTS = "SUPPORTS"
    PARTIAL = "PARTIAL"
    CONTRADICTS = "CONTRADICTS"
    NONE = "NONE"


SemanticVerifier = Callable[[GoldItem, tuple[WriterContextItem, ...]], SemanticSupport]


class SemanticGoldJudgment(DomainModel):
    gold_id: StableId
    all_claims_support: SemanticSupport
    traceable_claims_support: SemanticSupport
    all_context_item_ids: tuple[StableId, ...]
    traceable_context_item_ids: tuple[StableId, ...]
    validation_diagnostics: tuple[str, ...] = ()
    explanation: str = Field(min_length=1, max_length=240)


class SemanticVerificationBatch(DomainModel):
    judgments: tuple[SemanticGoldJudgment, ...]

    @model_validator(mode="after")
    def validate_unique_gold(self) -> SemanticVerificationBatch:
        identities = tuple(item.gold_id for item in self.judgments)
        if len(identities) != len(set(identities)):
            raise ValueError("semantic verifier returned duplicate Gold ids")
        return self


class ModelSemanticSupportVerifier:
    """Post-freeze batch verifier; its prompt contains private Gold by design."""

    version = "semantic_support_model.v7"

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        evidence_matcher: GoldEvidenceMatcher | None = None,
        batch_size: int = 2,
        max_batch_attempts: int = 3,
        max_concurrent_batches: int = 4,
    ) -> None:
        if not 1 <= batch_size <= 8:
            raise ValueError("semantic verifier batch size must be between 1 and 8")
        if not 1 <= max_batch_attempts <= 3:
            raise ValueError("semantic verifier batch attempts must be between 1 and 3")
        if not 1 <= max_concurrent_batches <= 8:
            raise ValueError("semantic verifier concurrent batches must be between 1 and 8")
        self._gateway = gateway
        self._evidence = evidence_matcher or GoldEvidenceMatcher()
        self._batch_size = batch_size
        self._max_batch_attempts = max_batch_attempts
        self._max_concurrent_batches = max_concurrent_batches

    async def verify(
        self,
        *,
        gold_items: tuple[GoldItem, ...],
        package: WriterContextPackage,
        evidence_ledger: EvidenceLedger,
        request: ModelRequest,
    ) -> tuple[SemanticVerificationBatch, tuple[ModelCallRecord, ...]]:
        items = MemoryBenchmarkEvaluator._items(package)
        cases: list[dict[str, object]] = []
        for gold in gold_items:
            match = self._evidence.match(gold, evidence_ledger)
            matched_ledger_ids = set(match.matched_ledger_ids)
            traceable_item_ids = tuple(
                item.context_item_id.root
                for item in items
                if matched_ledger_ids.intersection(item.evidence_ledger_ids)
            )
            cases.append(
                {
                    "gold_id": gold.gold_id.root,
                    "fact": gold.fact or gold.description,
                    "why_needed": gold.why_needed,
                    "gold_type": (
                        gold.gold_type.value if gold.gold_type is not None else gold.kind.value
                    ),
                    "target_components": list(gold.target_components),
                    "traceable_context_item_ids": traceable_item_ids,
                }
            )
        claims: list[dict[str, object]] = [
            {
                "context_item_id": item.context_item_id.root,
                "claim": item.claim,
                "validity": item.validity.value,
                "evidence_ledger_ids": [value.root for value in item.evidence_ledger_ids],
            }
            for item in items
        ]
        judgments: list[SemanticGoldJudgment] = []
        calls: list[ModelCallRecord] = []
        batches: list[tuple[int, list[dict[str, object]]]] = []
        for offset in range(0, len(cases), self._batch_size):
            case_batch = [dict(case) for case in cases[offset : offset + self._batch_size]]
            batches.append((offset // self._batch_size + 1, case_batch))
        if self._max_concurrent_batches <= 1 or len(batches) <= 1:
            batch_results = [
                await self._verify_batch(
                    batch_number=batch_number,
                    case_batch=case_batch,
                    claims=claims,
                    request=request,
                )
                for batch_number, case_batch in batches
            ]
        else:
            semaphore = asyncio.Semaphore(self._max_concurrent_batches)

            async def _guarded(
                batch_number: int,
                case_batch: list[dict[str, object]],
            ) -> tuple[tuple[SemanticGoldJudgment, ...], tuple[ModelCallRecord, ...]]:
                async with semaphore:
                    return await self._verify_batch(
                        batch_number=batch_number,
                        case_batch=case_batch,
                        claims=claims,
                        request=request,
                    )

            batch_results = await asyncio.gather(
                *(_guarded(batch_number, case_batch) for batch_number, case_batch in batches)
            )
        for batch_judgments, batch_calls in batch_results:
            judgments.extend(batch_judgments)
            calls.extend(batch_calls)
        result = SemanticVerificationBatch(judgments=tuple(judgments))
        return result, tuple(calls)

    async def _verify_batch(
        self,
        *,
        batch_number: int,
        case_batch: list[dict[str, object]],
        claims: list[dict[str, object]],
        request: ModelRequest,
    ) -> tuple[tuple[SemanticGoldJudgment, ...], tuple[ModelCallRecord, ...]]:
        """Verify one Gold batch; independent of every other batch.

        Concurrent execution changes only when the request is submitted, never
        the prompt content, claim set, retry policy, or fail-closed semantics.
        """

        relevant_ids: set[str] = set()
        for case in case_batch:
            semantic_query = " ".join(
                (
                    str(case["fact"]),
                    *(str(value) for value in cast(list[str], case["target_components"])),
                )
            )
            ranked_claims = sorted(
                claims,
                key=lambda item: (
                    self._semantic_overlap(str(item["claim"]), semantic_query),
                    str(item["context_item_id"]),
                ),
                reverse=True,
            )
            traceable_ids = {
                str(value) for value in cast(tuple[str, ...], case["traceable_context_item_ids"])
            }
            focused_traceable_ids = tuple(
                str(item["context_item_id"])
                for item in ranked_claims
                if str(item["context_item_id"]) in traceable_ids
            )[:6]
            case["traceable_context_item_ids"] = focused_traceable_ids
            relevant_ids.update(focused_traceable_ids)
            relevant_ids.update(
                str(item["context_item_id"])
                for item in ranked_claims[:6]
                if self._semantic_overlap(str(item["claim"]), semantic_query) > 0
            )
        batch_claims = [item for item in claims if str(item["context_item_id"]) in relevant_ids]
        batch_suffix = f".batch{batch_number}"
        prompt_base = (
            "You are the post-freeze semantic verifier for a memory benchmark. "
            "Judge only whether the supplied frozen Writer Context conclusions express each "
            "Gold fact. SUPPORTS means all material clauses are present and compatible; "
            "PARTIAL means only some material clauses are present; CONTRADICTS means a frozen "
            "claim states a contrary or stale conclusion; NONE means the conclusion is absent. "
            "Treat target_components as the required positive clauses. A Gold clause saying "
            "that a detail, deadline, explanation, or causal link is not yet revealed is a "
            "truth boundary: it is satisfied when the positive components are supported and "
            "no frozen claim asserts the unrevealed detail as known fact. "
            "For PLAN_OBLIGATION, an accepted author-visible plan node in the traceable claims "
            "is sufficient semantic support for the coarse obligation; do not require "
            "evaluator-only chapter-level details. "
            "For traceable_claims_support, consider only context_item_ids listed "
            "for that Gold. "
            "Return all_context_item_ids naming the frozen claims used for "
            "all_claims_support, and traceable_context_item_ids naming the subset used for "
            "traceable_claims_support. Use empty id lists when support is NONE. "
            "Evidence identity alone never proves semantic support. "
            "Each explanation must be a single sentence of at most 120 characters. "
            "Return one judgment for every Gold id, without adding or omitting ids.\n"
            f"GOLD_CASES={case_batch!r}\n"
            f"FROZEN_CLAIMS={batch_claims!r}"
        )
        expected_ids = {StableId(str(item["gold_id"])) for item in case_batch}
        accepted_batch: SemanticVerificationBatch | None = None
        batch_calls: list[ModelCallRecord] = []
        for attempt in range(1, self._max_batch_attempts + 1):
            attempt_suffix = batch_suffix + ("" if attempt == 1 else f".retry{attempt}")
            request_id = request.request_id.root[: 128 - len(attempt_suffix)] + attempt_suffix
            prompt = (
                prompt_base
                + f"\nVERIFICATION_ATTEMPT={attempt}. "
                + "Count GOLD_CASES before responding and preserve every Gold id exactly."
            )
            try:
                batch, call = await self._gateway.generate_structured(
                    request.model_copy(
                        update={
                            "request_id": StableId(request_id),
                            "prompt": prompt,
                        }
                    ),
                    SemanticVerificationBatch,
                )
            except TimeoutError:
                accepted_batch = SemanticVerificationBatch(
                    judgments=tuple(
                        SemanticGoldJudgment(
                            gold_id=StableId(str(item["gold_id"])),
                            all_claims_support=SemanticSupport.NONE,
                            traceable_claims_support=SemanticSupport.NONE,
                            all_context_item_ids=(),
                            traceable_context_item_ids=(),
                            validation_diagnostics=("SEMANTIC_VERIFIER_TIMEOUT",),
                            explanation=("semantic verifier timed out; result failed closed"),
                        )
                        for item in case_batch
                    )
                )
                break
            batch_calls.append(call)
            actual_ids = {item.gold_id for item in batch.judgments}
            if actual_ids == expected_ids:
                traceable_by_gold = {
                    StableId(str(item["gold_id"])): {
                        StableId(str(value))
                        for value in cast(
                            tuple[str, ...],
                            item["traceable_context_item_ids"],
                        )
                    }
                    for item in case_batch
                }
                frozen_item_ids = {StableId(str(item["context_item_id"])) for item in claims}
                accepted_batch = SemanticVerificationBatch(
                    judgments=tuple(
                        self._validate_judgment(
                            judgment,
                            frozen_item_ids=frozen_item_ids,
                            matcher_traceable_item_ids=traceable_by_gold[judgment.gold_id],
                        )
                        for judgment in batch.judgments
                    )
                )
                break
        if accepted_batch is None:
            raise ValueError("semantic verifier Gold id set does not match evaluator batch")
        return accepted_batch.judgments, tuple(batch_calls)

    @staticmethod
    def _validate_judgment(
        judgment: SemanticGoldJudgment,
        *,
        frozen_item_ids: set[StableId],
        matcher_traceable_item_ids: set[StableId],
    ) -> SemanticGoldJudgment:
        """Bind model support claims to the frozen package and accepted provenance."""

        diagnostics = list(judgment.validation_diagnostics)
        all_ids = set(judgment.all_context_item_ids)
        traceable_ids = set(judgment.traceable_context_item_ids)
        all_support = judgment.all_claims_support
        traceable_support = judgment.traceable_claims_support

        if not all_ids.issubset(frozen_item_ids):
            diagnostics.append("ALL_CONTEXT_ITEM_IDS_OUTSIDE_FROZEN_PACKAGE")
            all_support = SemanticSupport.NONE
        if not traceable_ids.issubset(frozen_item_ids):
            diagnostics.append("TRACEABLE_CONTEXT_ITEM_IDS_OUTSIDE_FROZEN_PACKAGE")
            traceable_support = SemanticSupport.NONE
        if not traceable_ids.issubset(all_ids):
            diagnostics.append("TRACEABLE_CONTEXT_ITEM_IDS_NOT_IN_ALL_CONTEXT_ITEMS")
            traceable_support = SemanticSupport.NONE
        if not traceable_ids.issubset(matcher_traceable_item_ids):
            diagnostics.append("TRACEABLE_CONTEXT_ITEM_IDS_NOT_MATCHER_BOUND")
            traceable_support = SemanticSupport.NONE
        if all_support is not SemanticSupport.NONE and not all_ids:
            diagnostics.append("ALL_CLAIMS_SUPPORT_WITHOUT_CONTEXT_ITEM_IDS")
            all_support = SemanticSupport.NONE
        if traceable_support is not SemanticSupport.NONE and not traceable_ids:
            diagnostics.append("TRACEABLE_CLAIMS_SUPPORT_WITHOUT_CONTEXT_ITEM_IDS")
            traceable_support = SemanticSupport.NONE

        return judgment.model_copy(
            update={
                "all_claims_support": all_support,
                "traceable_claims_support": traceable_support,
                "validation_diagnostics": tuple(dict.fromkeys(diagnostics)),
            }
        )

    @staticmethod
    def _semantic_overlap(claim: str, query: str) -> int:
        query_terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2}", query.casefold()))
        folded = claim.casefold()
        return sum(term in folded for term in query_terms)


class MemoryBenchmarkEvaluator:
    version = "per_gold_v3"

    def __init__(
        self,
        *,
        evidence_matcher: GoldEvidenceMatcher | None = None,
        semantic_verifier: SemanticVerifier | None = None,
    ) -> None:
        self._evidence = evidence_matcher or GoldEvidenceMatcher()
        self._semantic = semantic_verifier or self._deterministic_semantic_verifier

    def evaluate(
        self,
        *,
        package: WriterContextPackage,
        evidence_ledger: EvidenceLedger,
        gold_items: tuple[GoldItem, ...],
        profile: BenchmarkInformationProfile,
        freeze_receipt: FreezeReceipt,
        evaluator_manifest_id: StableId,
        evaluator_manifest_ref: ArtifactRef,
        evaluator_manifest_hash: ArtifactId,
        gold_metric_descriptors: Mapping[StableId, ContentAddressedGoldMetricDescriptor],
        semantic_judgments: Mapping[StableId, SemanticGoldJudgment] | None = None,
        verifier_receipt_ref: ArtifactRef | None = None,
        stage_loss_diagnostics: tuple[PerGoldStageLossDiagnostic, ...] = (),
    ) -> MemoryBenchmarkEvaluationReport:
        self._verify_frozen_artifacts(package, evidence_ledger, freeze_receipt)
        applicable = tuple(item for item in gold_items if profile in item.applicable_profiles)
        self._verify_metric_descriptors(
            applicable,
            gold_metric_descriptors,
            evaluator_manifest_id=evaluator_manifest_id,
            evaluator_manifest_hash=evaluator_manifest_hash,
        )
        if semantic_judgments is not None:
            if verifier_receipt_ref is None:
                raise ValueError("model semantic judgments require a verifier receipt")
            if set(semantic_judgments) != {item.gold_id for item in applicable}:
                raise ValueError("semantic judgment Gold ids do not match applicable Gold")
            frozen_items = self._items(package)
            frozen_item_ids = {item.context_item_id for item in frozen_items}
            normalized_judgments: dict[StableId, SemanticGoldJudgment] = {}
            for gold in applicable:
                evidence = self._evidence.match(gold, evidence_ledger)
                matched_ledger_ids = set(evidence.matched_ledger_ids)
                matcher_traceable_item_ids = {
                    item.context_item_id
                    for item in frozen_items
                    if matched_ledger_ids.intersection(item.evidence_ledger_ids)
                }
                normalized_judgments[gold.gold_id] = (
                    ModelSemanticSupportVerifier._validate_judgment(
                        semantic_judgments[gold.gold_id],
                        frozen_item_ids=frozen_item_ids,
                        matcher_traceable_item_ids=matcher_traceable_item_ids,
                    )
                )
            semantic_judgments = normalized_judgments
        comparisons = tuple(
            self._compare(
                item,
                package,
                evidence_ledger,
                semantic_judgment=(
                    None if semantic_judgments is None else semantic_judgments[item.gold_id]
                ),
                verifier_receipt_ref=verifier_receipt_ref,
                metric_descriptor=gold_metric_descriptors[item.gold_id],
            )
            for item in applicable
        )
        self._verify_stage_loss_diagnostics(applicable, stage_loss_diagnostics)
        stage_loss_diagnostics = self._reconcile_terminal_stage_loss(
            stage_loss_diagnostics, comparisons
        )
        total_weight = sum(item.weight for item in comparisons)
        score = {
            GoldMatchStatus.HIT: 1.0,
            GoldMatchStatus.PARTIAL: 0.5,
            GoldMatchStatus.MISS: 0.0,
            GoldMatchStatus.CONTRADICTS: 0.0,
            GoldMatchStatus.UNTRACEABLE: 0.0,
        }
        weighted = (
            sum(item.weight * score[item.status] for item in comparisons) / total_weight
            if total_weight
            else 0.0
        )
        mandatory = tuple(item for item in comparisons if item.mandatory)
        mandatory_hit = (
            sum(item.status is GoldMatchStatus.HIT for item in mandatory) / len(mandatory)
            if mandatory
            else 1.0
        )
        count = len(comparisons)
        return MemoryBenchmarkEvaluationReport(
            evaluator_version=self.version,
            profile=profile,
            comparisons=comparisons,
            weighted_coverage=weighted,
            mandatory_hit_rate=mandatory_hit,
            contradiction_rate=(
                sum(item.status is GoldMatchStatus.CONTRADICTS for item in comparisons) / count
                if count
                else 0.0
            ),
            untraceable_rate=(
                sum(item.status is GoldMatchStatus.UNTRACEABLE for item in comparisons) / count
                if count
                else 0.0
            ),
            freeze_receipt_id=freeze_receipt.receipt_id,
            evaluator_manifest_id=evaluator_manifest_id,
            evaluator_manifest_ref=evaluator_manifest_ref,
            evaluator_manifest_hash=evaluator_manifest_hash,
            gate_metric_formula_version=GATE_METRIC_FORMULA_VERSION,
            gate_metric_formula_hash=GATE_METRIC_FORMULA_HASH,
            stage_loss_diagnostics=stage_loss_diagnostics,
        )

    _SCOPE_BY_NEED_TYPE: ClassVar[dict[str, str]] = {
        "current_state": "current",
        "capability_boundary": "current",
        "relationship_emotion": "relation",
        "knowledge_boundary": "knowledge",
        "long_range_callback": "historical",
        "unresolved_obligation": "current",
        "entity_history": "historical",
        "causal_history": "historical",
        "plan_obligation": "planned",
        "plan_conditioned_history": "historical",
        "target_transition_history": "historical",
        "continuity_constraint": "current",
    }
    _PLAN_CHANNEL_NEED_TYPES = frozenset({"plan_obligation", "plan_conditioned_history"})

    def evaluate_five_segments(
        self,
        *,
        needs: tuple[Stage1MemoryNeed, ...],
        gold_need_specs: tuple[GoldNeedSpec, ...],
        plan_goals: tuple[ChapterGoal, ...],
        gold_items: tuple[GoldItem, ...],
        evidence_ledger: EvidenceLedger,
        completion_accuracy: float,
        per_gold_comparisons: tuple[PerGoldComparison, ...] = (),
        future_leakage_count: int,
        entity_id_by_label: Mapping[str, StableId] | None = None,
        planner_fallback_used: bool = False,
        planner_fallback_reason: str | None = None,
        planner_artifact_ref: ArtifactRef | None = None,
        grounded_status_counts: tuple[int, int, int] = (0, 0, 0),
        profile: BenchmarkInformationProfile = BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    ) -> FiveSegmentReport:
        """Five-segment diagnostic evaluation (Phase 3).

        Segment 1 (Plan Goal Coverage), 2 (Need Recall), and 5 (Leakage) are
        computed here deterministically; segments 3 (Evidence Recall) and 4
        (Completion/Claim Accuracy) reuse the frozen matcher and the existing
        weighted coverage.  Leakage is never folded into accuracy.

        Gate 1 planning health is added as ``planner_fallback_rate`` (fallback
        share of generation runs) and ``grounding_success_rate`` (GROUNDED
        share of grounded entity mentions; an empty mention set is trivially
        successful).
        """

        planner_fallback_rate = 1.0 if planner_fallback_used else 0.0
        grounded_total = sum(grounded_status_counts)
        grounded_success = grounded_status_counts[0]
        applicable_gold = tuple(item for item in gold_items if profile in item.applicable_profiles)
        goal_text_by_chapter = {goal.chapter_index: goal.summary for goal in plan_goals}
        covered_chapters: set[int] = set()
        for need in needs:
            if (
                planner_artifact_ref is None
                or planner_fallback_used
                or need.planner_artifact_ref is None
                or need.planner_artifact_ref != planner_artifact_ref.artifact_id
                or need.validated_need_set_hash is None
                or need.planned_draft_id is None
                or not need.semantic_question
            ):
                continue
            for chapter in need.trigger_plan_chapters:
                if (
                    goal_text_by_chapter.get(chapter) == need.trigger_plan_goal
                    and need.semantic_question != need.trigger_plan_goal
                    and all(facet.facet_kind.name != "PLAN_NODE" for facet in need.need_facets)
                ):
                    covered_chapters.add(chapter)
        spec_by_gold = {spec.gold_id: spec for spec in gold_need_specs}
        labels_by_need: dict[StableId, set[str]] = {}
        for need in needs:
            labels_by_need[need.need_id] = {
                label
                for label, entity_id in (entity_id_by_label or {}).items()
                if entity_id in need.entity_ids
            }
        bindings: list[GoldNeedBinding] = []
        missing_specs: list[StableId] = []
        evidence_matches = 0
        evidence_total = 0
        need_total = 0
        need_matched = 0
        for gold in applicable_gold:
            spec = spec_by_gold.get(gold.gold_id)
            unavailable_reason: str | None = None
            if spec is None:
                unavailable_reason = "MISSING_GOLD_NEED_SPEC"
                missing_specs.append(gold.gold_id)
            elif spec.blindness is GoldBlindness.HINDSIGHT_ONLY:
                unavailable_reason = "HINDSIGHT_ONLY"
            elif (
                profile
                in {
                    BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                    BenchmarkInformationProfile.TASK_INTENT_ONLY,
                }
                and spec.blindness is not GoldBlindness.BLIND_RECOVERABLE
            ):
                unavailable_reason = "PROFILE_BLINDNESS_NOT_APPLICABLE"
            elif "planned" in spec.required_need_scopes or "PLAN_NODE" in spec.required_facets:
                unavailable_reason = "PLAN_ONLY_STRICT_D9"
            if unavailable_reason is not None:
                bindings.append(
                    GoldNeedBinding(
                        profile=profile,
                        gold_id=gold.gold_id,
                        blindness=spec.blindness if spec is not None else None,
                        spec_hash=(
                            content_id(spec.model_dump(mode="json")) if spec is not None else None
                        ),
                        availability=SegmentAvailability.UNAVAILABLE,
                        unavailable_reason=unavailable_reason,
                    )
                )
                continue
            assert spec is not None
            candidates: list[tuple[int, str, Stage1MemoryNeed, GoldNeedBinding]] = []
            for need in needs:
                need_scopes = {facet.expected_claim_scope.value for facet in need.need_facets} or {
                    self._SCOPE_BY_NEED_TYPE.get(need.need_type, "current")
                }
                need_facets = {facet.facet_kind.name for facet in need.need_facets}
                need_labels = labels_by_need[need.need_id]
                scope_hits = tuple(
                    item for item in spec.required_need_scopes if item in need_scopes
                )
                entity_hits = tuple(item for item in spec.required_entities if item in need_labels)
                facet_hits = tuple(item for item in spec.required_facets if item in need_facets)
                scope_misses = tuple(
                    item for item in spec.required_need_scopes if item not in need_scopes
                )
                entity_misses = tuple(
                    item for item in spec.required_entities if item not in need_labels
                )
                facet_misses = tuple(
                    item for item in spec.required_facets if item not in need_facets
                )
                score = len(scope_hits) + len(entity_hits) + len(facet_hits)
                eligible_entries = tuple(
                    entry for entry in evidence_ledger.entries if need.need_id in entry.need_ids
                )
                candidates.append(
                    (
                        score,
                        need.need_id.root,
                        need,
                        GoldNeedBinding(
                            profile=profile,
                            gold_id=gold.gold_id,
                            blindness=spec.blindness,
                            spec_hash=content_id(spec.model_dump(mode="json")),
                            selected_need_id=need.need_id,
                            scope_hits=scope_hits,
                            scope_misses=scope_misses,
                            entity_hits=entity_hits,
                            entity_misses=entity_misses,
                            facet_hits=facet_hits,
                            facet_misses=facet_misses,
                            eligible_ledger_ids=tuple(
                                entry.ledger_id for entry in eligible_entries
                            ),
                            full_need_match=not (scope_misses or entity_misses or facet_misses),
                            tie_break_evidence=(f"component_hits={score}", need.need_id.root),
                        ),
                    )
                )
            need_total += 1
            if not candidates:
                bindings.append(
                    GoldNeedBinding(
                        profile=profile,
                        gold_id=gold.gold_id,
                        blindness=spec.blindness,
                        spec_hash=content_id(spec.model_dump(mode="json")),
                        availability=SegmentAvailability.UNAVAILABLE,
                        unavailable_reason="NO_GENERATED_NEEDS",
                    )
                )
                continue
            _score, _need_id, selected_need, binding = sorted(
                candidates, key=lambda item: (-item[0], item[1])
            )[0]
            bindings.append(binding)
            if binding.full_need_match:
                need_matched += 1
            if gold.evidence_refs or gold.accepted_evidence_sets:
                evidence_total += 1
                bound_ledger = EvidenceLedger(
                    contract_version=evidence_ledger.contract_version,
                    entries=tuple(
                        entry
                        for entry in evidence_ledger.entries
                        if selected_need.need_id in entry.need_ids
                    ),
                    rendered_tokens=evidence_ledger.rendered_tokens,
                )
                if self._evidence.match(gold, bound_ledger).matched_ledger_ids:
                    evidence_matches += 1

        plan_citation_count = sum(1 for entry in evidence_ledger.entries if entry.plan_node_ids)
        plan_leakage_count = plan_citation_count
        plan_available = bool(goal_text_by_chapter)
        need_available = need_total > 0
        evidence_available = evidence_total > 0
        completion_available = any(
            binding.availability is SegmentAvailability.AVAILABLE for binding in bindings
        )
        available_gold_ids = {
            binding.gold_id
            for binding in bindings
            if binding.availability is SegmentAvailability.AVAILABLE
        }
        completion_comparisons = tuple(
            comparison
            for comparison in per_gold_comparisons
            if comparison.gold_id in available_gold_ids
        )
        completion_weight_total = sum(item.weight for item in completion_comparisons)
        completion_score = {
            GoldMatchStatus.HIT: 1.0,
            GoldMatchStatus.PARTIAL: 0.5,
            GoldMatchStatus.MISS: 0.0,
            GoldMatchStatus.CONTRADICTS: 0.0,
            GoldMatchStatus.UNTRACEABLE: 0.0,
        }
        bound_completion_accuracy = (
            sum(item.weight * completion_score[item.status] for item in completion_comparisons)
            / completion_weight_total
            if completion_weight_total
            else completion_accuracy
        )
        return FiveSegmentReport(
            plan_goals_total=len(goal_text_by_chapter),
            plan_goals_covered=len(covered_chapters),
            plan_goal_coverage=(
                len(covered_chapters) / len(goal_text_by_chapter) if plan_available else None
            ),
            plan_goal_availability=(
                SegmentAvailability.AVAILABLE if plan_available else SegmentAvailability.UNAVAILABLE
            ),
            need_recall_total=need_total,
            need_recall_matched=need_matched,
            need_recall=(need_matched / need_total if need_available else None),
            need_recall_availability=(
                SegmentAvailability.AVAILABLE if need_available else SegmentAvailability.UNAVAILABLE
            ),
            evidence_recall=(evidence_matches / evidence_total if evidence_available else None),
            evidence_recall_total=evidence_total,
            evidence_recall_matched=evidence_matches,
            evidence_recall_availability=(
                SegmentAvailability.AVAILABLE
                if evidence_available
                else SegmentAvailability.UNAVAILABLE
            ),
            completion_accuracy=(bound_completion_accuracy if completion_available else None),
            completion_gold_total=(
                len(completion_comparisons) if per_gold_comparisons else len(available_gold_ids)
            ),
            completion_weight_total=(
                completion_weight_total if per_gold_comparisons else float(len(available_gold_ids))
            ),
            completion_availability=(
                SegmentAvailability.AVAILABLE
                if completion_available
                else SegmentAvailability.UNAVAILABLE
            ),
            future_leakage_count=future_leakage_count,
            plan_citation_count=plan_citation_count,
            plan_leakage_count=plan_leakage_count,
            planner_fallback_rate=planner_fallback_rate,
            grounding_success_rate=(grounded_success / grounded_total if grounded_total else 1.0),
            planner_artifact_ref=planner_artifact_ref,
            planner_fallback_reason=planner_fallback_reason,
            grounded_status_counts=grounded_status_counts,
            bindings=tuple(bindings),
            missing_spec_gold_ids=tuple(missing_specs),
        )

    def evaluate_typed_failure(
        self,
        *,
        gold_items: tuple[GoldItem, ...],
        profile: BenchmarkInformationProfile,
        assembly_status: ContextAssemblyStatus,
        freeze_receipt: FreezeReceipt,
        evaluator_manifest_id: StableId,
        evaluator_manifest_ref: ArtifactRef,
        evaluator_manifest_hash: ArtifactId,
        gold_metric_descriptors: Mapping[StableId, ContentAddressedGoldMetricDescriptor],
        stage_loss_diagnostics: tuple[PerGoldStageLossDiagnostic, ...] = (),
    ) -> MemoryBenchmarkEvaluationReport:
        if assembly_status is ContextAssemblyStatus.READY:
            raise ValueError("READY context must use normal per-Gold evaluation")
        applicable = tuple(item for item in gold_items if profile in item.applicable_profiles)
        self._verify_metric_descriptors(
            applicable,
            gold_metric_descriptors,
            evaluator_manifest_id=evaluator_manifest_id,
            evaluator_manifest_hash=evaluator_manifest_hash,
        )
        comparisons = tuple(
            PerGoldComparison(
                gold_id=item.gold_id,
                status=GoldMatchStatus.MISS,
                weight=item.weight,
                mandatory=item.mandatory,
                gold_metric_descriptor_ref=gold_metric_descriptors[item.gold_id].descriptor_ref,
                gold_metric_descriptor_hash=gold_metric_descriptors[item.gold_id].descriptor_hash,
                missing_components=item.target_components,
                explanation=(f"Writer Context was not quality-ready: {assembly_status.value}"),
            )
            for item in applicable
        )
        self._verify_stage_loss_diagnostics(applicable, stage_loss_diagnostics)
        stage_loss_diagnostics = self._reconcile_terminal_stage_loss(
            stage_loss_diagnostics, comparisons
        )
        mandatory = tuple(item for item in comparisons if item.mandatory)
        return MemoryBenchmarkEvaluationReport(
            evaluator_version=self.version,
            profile=profile,
            comparisons=comparisons,
            weighted_coverage=0.0,
            mandatory_hit_rate=0.0 if mandatory else 1.0,
            contradiction_rate=0.0,
            untraceable_rate=0.0,
            freeze_receipt_id=freeze_receipt.receipt_id,
            evaluator_manifest_id=evaluator_manifest_id,
            evaluator_manifest_ref=evaluator_manifest_ref,
            evaluator_manifest_hash=evaluator_manifest_hash,
            gate_metric_formula_version=GATE_METRIC_FORMULA_VERSION,
            gate_metric_formula_hash=GATE_METRIC_FORMULA_HASH,
            stage_loss_diagnostics=stage_loss_diagnostics,
        )

    @staticmethod
    def _verify_stage_loss_diagnostics(
        applicable_gold: tuple[GoldItem, ...],
        diagnostics: tuple[PerGoldStageLossDiagnostic, ...],
    ) -> None:
        if not diagnostics:
            return
        expected = {item.gold_id for item in applicable_gold}
        actual = {item.gold_id for item in diagnostics}
        if len(actual) != len(diagnostics):
            raise ValueError("stage-loss diagnostics contain duplicate Gold ids")
        if actual != expected:
            raise ValueError("stage-loss diagnostic Gold ids do not match applicable Gold")

    @staticmethod
    def _reconcile_terminal_stage_loss(
        diagnostics: tuple[PerGoldStageLossDiagnostic, ...],
        comparisons: tuple[PerGoldComparison, ...],
    ) -> tuple[PerGoldStageLossDiagnostic, ...]:
        """A complete evidence path is not a complete semantic verdict."""

        from novel_agent.domain.memory_benchmark import EvidenceStageFailure

        status_by_gold = {item.gold_id: item.status for item in comparisons}
        return tuple(
            diagnostic.model_copy(
                update={"primary_failure": EvidenceStageFailure.F_CLAIM_EVALUATOR}
            )
            if diagnostic.primary_failure is EvidenceStageFailure.COMPLETE
            and status_by_gold[diagnostic.gold_id] is not GoldMatchStatus.HIT
            else diagnostic
            for diagnostic in diagnostics
        )

    @staticmethod
    def _verify_metric_descriptors(
        applicable_gold: tuple[GoldItem, ...],
        descriptors: Mapping[StableId, ContentAddressedGoldMetricDescriptor],
        *,
        evaluator_manifest_id: StableId,
        evaluator_manifest_hash: ArtifactId,
    ) -> None:
        expected = {item.gold_id for item in applicable_gold}
        if set(descriptors) != expected:
            raise ValueError("Gold metric descriptor ids do not match applicable Gold")
        for gold in applicable_gold:
            binding = descriptors[gold.gold_id]
            descriptor = binding.descriptor
            if descriptor.gold_id != gold.gold_id:
                raise ValueError("Gold metric descriptor Gold id mismatch")
            if descriptor.weight != gold.weight or descriptor.mandatory != gold.mandatory:
                raise ValueError("Gold metric descriptor score fields mismatch")
            if descriptor.gold_type != gold.gold_type or descriptor.gold_kind != gold.kind.value:
                raise ValueError("Gold metric descriptor classification mismatch")
            if descriptor.applicable_profiles != gold.applicable_profiles:
                raise ValueError("Gold metric descriptor profile applicability mismatch")
            if (
                descriptor.evaluator_manifest_id != evaluator_manifest_id
                or descriptor.evaluator_manifest_hash != evaluator_manifest_hash
            ):
                raise ValueError("Gold metric descriptor evaluator manifest mismatch")

    def _compare(
        self,
        gold: GoldItem,
        package: WriterContextPackage,
        ledger: EvidenceLedger,
        *,
        semantic_judgment: SemanticGoldJudgment | None,
        verifier_receipt_ref: ArtifactRef | None,
        metric_descriptor: ContentAddressedGoldMetricDescriptor,
    ) -> PerGoldComparison:
        evidence = self._evidence.match(gold, ledger)
        all_items = self._items(package)
        evidence_item_ids = set(evidence.matched_ledger_ids)
        traceable_items = tuple(
            item for item in all_items if evidence_item_ids.intersection(item.evidence_ledger_ids)
        )
        semantic_all = (
            self._semantic(gold, all_items)
            if semantic_judgment is None
            else semantic_judgment.all_claims_support
        )
        semantic_traceable = (
            (self._semantic(gold, traceable_items) if traceable_items else SemanticSupport.NONE)
            if semantic_judgment is None
            else semantic_judgment.traceable_claims_support
        )
        target_components = gold.target_components
        supported_components = (
            tuple(
                component
                for component in target_components
                if component in evidence.supported_components
            )
            if target_components
            else ()
        )
        missing_components = tuple(
            component for component in target_components if component not in supported_components
        )

        if semantic_all is SemanticSupport.CONTRADICTS:
            status = GoldMatchStatus.CONTRADICTS
            explanation = "the frozen Writer Context states a contrary or stale conclusion"
        elif semantic_traceable is SemanticSupport.SUPPORTS and evidence.matched:
            status = GoldMatchStatus.HIT
            supported_components = target_components or evidence.supported_components
            missing_components = ()
            explanation = "the frozen claim supports Gold and cites an accepted cutoff-safe source"
        elif semantic_traceable is SemanticSupport.PARTIAL and evidence.partially_matched:
            status = GoldMatchStatus.PARTIAL
            explanation = "the frozen, traceable claim supports only part of the Gold conclusion"
        elif semantic_all in {SemanticSupport.SUPPORTS, SemanticSupport.PARTIAL}:
            status = GoldMatchStatus.UNTRACEABLE
            explanation = "a semantically matching claim has no accepted cutoff-safe provenance"
        else:
            status = GoldMatchStatus.MISS
            explanation = "the frozen Writer Context does not express the required conclusion"

        return PerGoldComparison(
            gold_id=gold.gold_id,
            status=status,
            weight=gold.weight,
            mandatory=gold.mandatory,
            gold_metric_descriptor_ref=metric_descriptor.descriptor_ref,
            gold_metric_descriptor_hash=metric_descriptor.descriptor_hash,
            matched_context_item_ids=tuple(item.context_item_id for item in traceable_items),
            matched_evidence_ledger_ids=evidence.matched_ledger_ids,
            supported_components=tuple(dict.fromkeys(supported_components)),
            missing_components=missing_components,
            explanation=explanation,
            verifier_receipt_ref=verifier_receipt_ref,
        )

    @staticmethod
    def _items(package: WriterContextPackage) -> tuple[WriterContextItem, ...]:
        return (
            *package.continuity_constraints,
            *package.current_world_state,
            *package.relationship_and_emotion,
            *package.causal_history,
            *package.knowledge_and_disclosure,
            *package.plan_and_obligations,
            *package.long_range_callbacks,
        )

    @staticmethod
    def _verify_frozen_artifacts(
        package: WriterContextPackage,
        ledger: EvidenceLedger,
        receipt: FreezeReceipt,
    ) -> None:
        if package.budget_report.final_status.value != "READY":
            raise ValueError("only READY Writer Context can enter quality evaluation")
        ledger_hash = content_id(ledger.model_dump(mode="json"))
        if ledger_hash != package.evidence_ledger_ref.artifact_id:
            raise ValueError("evidence ledger hash does not match Writer Context reference")
        package_hash = content_id(package.model_dump(mode="json"))
        if receipt.arm_artifact_hashes[package.arm] != package_hash:
            raise ValueError("freeze receipt hash does not match Writer Context artifact")

    @staticmethod
    def _deterministic_semantic_verifier(
        gold: GoldItem,
        items: tuple[WriterContextItem, ...],
    ) -> SemanticSupport:
        expected = (gold.fact or gold.description).casefold()
        if not items:
            return SemanticSupport.NONE
        expected_terms = MemoryBenchmarkEvaluator._semantic_terms(expected)
        best = 0.0
        contradicted = False
        for item in items:
            claim = item.claim.casefold()
            claim_terms = MemoryBenchmarkEvaluator._semantic_terms(claim)
            overlap = (
                len(expected_terms.intersection(claim_terms)) / len(expected_terms)
                if expected_terms
                else 0.0
            )
            best = max(best, overlap)
            # This conservative deterministic check is only a floor. Formal
            # model verification can be injected and receives a versioned receipt.
            if overlap >= 0.45 and MemoryBenchmarkEvaluator._polarity(expected) != (
                MemoryBenchmarkEvaluator._polarity(claim)
            ):
                contradicted = True
        if contradicted:
            return SemanticSupport.CONTRADICTS
        if best >= 0.65:
            return SemanticSupport.SUPPORTS
        if best >= 0.3:
            return SemanticSupport.PARTIAL
        return SemanticSupport.NONE

    @staticmethod
    def _semantic_terms(value: str) -> set[str]:
        compact = re.sub(r"\s+", "", value)
        cjk = [compact[index : index + 2] for index in range(max(0, len(compact) - 1))]
        latin = re.findall(r"[a-z0-9_]+", value)
        return {*cjk, *latin}

    @staticmethod
    def _polarity(value: str) -> int:
        negatives = ("不", "未", "无", "没有", "never", "not", "no ")
        return -1 if any(token in value for token in negatives) else 1
