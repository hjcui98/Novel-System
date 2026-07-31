"""Freeze-gated, per-Gold evaluation of writer-facing memory contexts."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import cast

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import GoldItem
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    ContentAddressedGoldMetricDescriptor,
    ContextAssemblyStatus,
    EvidenceLedger,
    FreezeReceipt,
    GoldMatchStatus,
    MemoryBenchmarkEvaluationReport,
    PerGoldComparison,
    PerGoldStageLossDiagnostic,
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
    ) -> None:
        if not 1 <= batch_size <= 8:
            raise ValueError("semantic verifier batch size must be between 1 and 8")
        if not 1 <= max_batch_attempts <= 3:
            raise ValueError("semantic verifier batch attempts must be between 1 and 3")
        self._gateway = gateway
        self._evidence = evidence_matcher or GoldEvidenceMatcher()
        self._batch_size = batch_size
        self._max_batch_attempts = max_batch_attempts

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
        claims = [
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
        for offset in range(0, len(cases), self._batch_size):
            case_batch = [dict(case) for case in cases[offset : offset + self._batch_size]]
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
                    str(value)
                    for value in cast(tuple[str, ...], case["traceable_context_item_ids"])
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
            batch_number = offset // self._batch_size + 1
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
                calls.append(call)
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
            judgments.extend(accepted_batch.judgments)
        result = SemanticVerificationBatch(judgments=tuple(judgments))
        return result, tuple(calls)

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
    version = "per_gold_v2"

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
