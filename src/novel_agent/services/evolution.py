"""Offline U8-D/U8-E campaign execution, promotion, and rollback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.evolution import (
    EVOLUTION_CAMPAIGN_RESULT_MEDIA_TYPE,
    EVOLUTION_PROMOTION_RECEIPT_MEDIA_TYPE,
    EVOLUTION_ROLLBACK_RECEIPT_MEDIA_TYPE,
    EvolutionCampaignManifest,
    EvolutionCampaignResult,
    EvolutionCandidate,
    EvolutionCheckpointAssignment,
    EvolutionCheckpointResult,
    EvolutionCheckpointUse,
    EvolutionDecision,
    EvolutionMetricThreshold,
    EvolutionPromotionReceipt,
    EvolutionRollbackReceipt,
)
from novel_agent.domain.ids import SchemaVersion, StableId, bounded_stable_id
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes


class SealedAcceptanceAlreadyOpened(RuntimeError):
    """A whole checkpoint was already consumed by this campaign."""


class EvolutionCampaignProtocolError(ValueError):
    """Runner evidence does not match the pre-registered campaign."""


class EvolutionPromotionError(ValueError):
    """A promotion or rollback violates the active-version lineage."""


class EvolutionEvaluationRunner(Protocol):
    async def run(
        self,
        assignment: EvolutionCheckpointAssignment,
        candidate: EvolutionCandidate,
    ) -> EvolutionCheckpointResult: ...


class SealedAcceptanceLedgerPort(Protocol):
    def claim(self, campaign_id: StableId, checkpoint_id: StableId) -> None: ...


class EvolutionVersionRegistryPort(Protocol):
    def active(self, target_id: StableId) -> ArtifactRef | None: ...

    def require_active(self, target_id: StableId, expected: ArtifactRef) -> None: ...

    def compare_and_swap(
        self, target_id: StableId, expected: ArtifactRef, replacement: ArtifactRef
    ) -> None: ...


class InMemorySealedAcceptanceLedger:
    def __init__(self) -> None:
        self._claims: set[tuple[StableId, StableId]] = set()

    def claim(self, campaign_id: StableId, checkpoint_id: StableId) -> None:
        key = (campaign_id, checkpoint_id)
        if key in self._claims:
            raise SealedAcceptanceAlreadyOpened(
                f"sealed checkpoint {checkpoint_id.root} was already opened"
            )
        self._claims.add(key)


@dataclass(frozen=True, slots=True)
class EvolutionCampaignExecution:
    result: EvolutionCampaignResult
    result_ref: ArtifactRef


class EvolutionCampaignExecutor:
    """Run calibration, one-shot sealed acceptance, and canary in that order."""

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        sealed_ledger: SealedAcceptanceLedgerPort,
        schema_version: SchemaVersion,
    ) -> None:
        self._artifacts = artifacts
        self._sealed_ledger = sealed_ledger
        self._schema_version = schema_version

    async def execute(
        self,
        manifest: EvolutionCampaignManifest,
        runner: EvolutionEvaluationRunner,
    ) -> EvolutionCampaignExecution:
        calibration = await self._run_phase(
            manifest, runner, EvolutionCheckpointUse.CALIBRATION, claim_sealed=False
        )
        if not self._passes(calibration, manifest.thresholds):
            return self._persist_result(
                EvolutionCampaignResult(
                    campaign_id=manifest.campaign_id,
                    candidate_id=manifest.candidate.candidate_id,
                    decision=EvolutionDecision.REJECT,
                    calibration_results=calibration,
                    decision_codes=("CALIBRATION_FAILED",),
                    sealed_opened_once=False,
                )
            )

        sealed = await self._run_phase(
            manifest, runner, EvolutionCheckpointUse.SEALED_ACCEPTANCE, claim_sealed=True
        )
        if not self._passes(sealed, manifest.thresholds):
            return self._persist_result(
                EvolutionCampaignResult(
                    campaign_id=manifest.campaign_id,
                    candidate_id=manifest.candidate.candidate_id,
                    decision=EvolutionDecision.REJECT,
                    calibration_results=calibration,
                    sealed_results=sealed,
                    decision_codes=("SEALED_ACCEPTANCE_FAILED",),
                    sealed_opened_once=True,
                )
            )

        canary = await self._run_phase(
            manifest, runner, EvolutionCheckpointUse.CANARY, claim_sealed=False
        )
        canary_passed = self._passes(canary, manifest.thresholds)
        if not canary_passed:
            decision = EvolutionDecision.REJECT
            codes = ("CANARY_FAILED",)
        elif manifest.candidate.requires_human_gate:
            decision = EvolutionDecision.HUMAN_REVIEW
            codes = ("HUMAN_GATE_REQUIRED",)
        else:
            decision = EvolutionDecision.PROMOTE
            codes = ("SEALED_AND_CANARY_PASS",)
        return self._persist_result(
            EvolutionCampaignResult(
                campaign_id=manifest.campaign_id,
                candidate_id=manifest.candidate.candidate_id,
                decision=decision,
                calibration_results=calibration,
                sealed_results=sealed,
                canary_results=canary,
                decision_codes=codes,
                sealed_opened_once=True,
            )
        )

    async def _run_phase(
        self,
        manifest: EvolutionCampaignManifest,
        runner: EvolutionEvaluationRunner,
        use: EvolutionCheckpointUse,
        *,
        claim_sealed: bool,
    ) -> tuple[EvolutionCheckpointResult, ...]:
        results = []
        for assignment in (item for item in manifest.assignments if item.use is use):
            if claim_sealed:
                self._sealed_ledger.claim(manifest.campaign_id, assignment.checkpoint_id)
            result = await runner.run(assignment, manifest.candidate)
            self._validate_result(manifest, assignment, result)
            results.append(result)
        if not results:
            raise EvolutionCampaignProtocolError(f"campaign has no {use.value} checkpoint")
        return tuple(results)

    @staticmethod
    def _validate_result(
        manifest: EvolutionCampaignManifest,
        assignment: EvolutionCheckpointAssignment,
        result: EvolutionCheckpointResult,
    ) -> None:
        if result.checkpoint_id != assignment.checkpoint_id:
            raise EvolutionCampaignProtocolError("runner returned another checkpoint identity")
        if result.use is not assignment.use:
            raise EvolutionCampaignProtocolError("runner changed the checkpoint use")
        if result.basis_commit != assignment.basis_commit:
            raise EvolutionCampaignProtocolError("runner changed the checkpoint basis")
        if result.candidate_artifact_id != manifest.candidate.candidate_artifact_ref.artifact_id:
            raise EvolutionCampaignProtocolError("runner evaluated another candidate version")

    @staticmethod
    def _passes(
        results: tuple[EvolutionCheckpointResult, ...],
        thresholds: tuple[EvolutionMetricThreshold, ...],
    ) -> bool:
        for result in results:
            if result.hard_failure_codes:
                return False
            comparisons = {item.metric: item for item in result.comparisons}
            for threshold in thresholds:
                comparison = comparisons.get(threshold.metric)
                if comparison is None:
                    return False
                delta = (
                    comparison.candidate_value - comparison.baseline_value
                    if threshold.higher_is_better
                    else comparison.baseline_value - comparison.candidate_value
                )
                if delta < threshold.minimum_delta:
                    return False
        return True

    def _persist_result(self, result: EvolutionCampaignResult) -> EvolutionCampaignExecution:
        result_ref = self._artifacts.put(
            canonical_json_bytes(result.model_dump(mode="json")),
            EVOLUTION_CAMPAIGN_RESULT_MEDIA_TYPE,
            self._schema_version,
        )
        return EvolutionCampaignExecution(result=result, result_ref=result_ref)


class EvolutionVersionRegistry:
    """Task-local active-version registry; production pins remain unchanged."""

    def __init__(self, active: dict[StableId, ArtifactRef] | None = None) -> None:
        self._active = dict(active or {})

    def active(self, target_id: StableId) -> ArtifactRef | None:
        return self._active.get(target_id)

    def require_active(self, target_id: StableId, expected: ArtifactRef) -> None:
        if self._active.get(target_id) != expected:
            raise EvolutionPromotionError("active evolution version changed before promotion")

    def compare_and_swap(
        self, target_id: StableId, expected: ArtifactRef, replacement: ArtifactRef
    ) -> None:
        self.require_active(target_id, expected)
        self._active[target_id] = replacement


class EvolutionPromotionService:
    """Promote or roll back the offline active identity with explicit receipts."""

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        registry: EvolutionVersionRegistryPort,
        schema_version: SchemaVersion,
    ) -> None:
        self._artifacts = artifacts
        self._registry = registry
        self._schema_version = schema_version

    def promote(
        self,
        *,
        manifest: EvolutionCampaignManifest,
        execution: EvolutionCampaignExecution,
        promoted_at: datetime,
    ) -> tuple[EvolutionPromotionReceipt, ArtifactRef]:
        if execution.result.decision is not EvolutionDecision.PROMOTE:
            raise EvolutionPromotionError("campaign result does not authorize promotion")
        candidate = manifest.candidate
        if candidate.requires_human_gate:
            raise EvolutionPromotionError("human-gated candidate cannot auto-promote")
        if execution.result.campaign_id != manifest.campaign_id:
            raise EvolutionPromotionError("campaign result identity differs from manifest")
        if execution.result.candidate_id != candidate.candidate_id:
            raise EvolutionPromotionError("campaign result candidate differs from manifest")
        if not execution.result.sealed_results or not execution.result.canary_results:
            raise EvolutionPromotionError("promotion requires sealed and canary evidence")
        self._verify_campaign_result_artifact(execution)
        self._registry.require_active(candidate.target_id, candidate.base_artifact_ref)
        receipt = EvolutionPromotionReceipt(
            receipt_id=bounded_stable_id(
                f"evolution-promotion.{manifest.campaign_id.root}",
                f"evolution-promotion.{candidate.candidate_id.root}",
            ),
            campaign_id=manifest.campaign_id,
            target_id=candidate.target_id,
            target_kind=candidate.target_kind,
            previous_active_ref=candidate.base_artifact_ref,
            promoted_ref=candidate.candidate_artifact_ref,
            campaign_result_ref=execution.result_ref,
            promoted_at=promoted_at,
        )
        receipt_ref = self._artifacts.put(
            canonical_json_bytes(receipt.model_dump(mode="json")),
            EVOLUTION_PROMOTION_RECEIPT_MEDIA_TYPE,
            self._schema_version,
        )
        self._registry.compare_and_swap(
            candidate.target_id,
            candidate.base_artifact_ref,
            candidate.candidate_artifact_ref,
        )
        return receipt, receipt_ref

    def rollback(
        self,
        *,
        promotion: EvolutionPromotionReceipt,
        promotion_ref: ArtifactRef,
        failure_evidence_refs: tuple[ArtifactRef, ...],
        rolled_back_at: datetime,
    ) -> tuple[EvolutionRollbackReceipt, ArtifactRef]:
        if not failure_evidence_refs:
            raise EvolutionPromotionError("rollback requires failure evidence")
        self._verify_promotion_artifact(promotion, promotion_ref)
        self._registry.require_active(promotion.target_id, promotion.promoted_ref)
        receipt = EvolutionRollbackReceipt(
            receipt_id=bounded_stable_id(
                f"evolution-rollback.{promotion.campaign_id.root}",
                f"evolution-rollback.{promotion.target_id.root}",
            ),
            target_id=promotion.target_id,
            failed_promotion_ref=promotion_ref,
            restored_ref=promotion.previous_active_ref,
            failure_evidence_refs=failure_evidence_refs,
            rolled_back_at=rolled_back_at,
        )
        receipt_ref = self._artifacts.put(
            canonical_json_bytes(receipt.model_dump(mode="json")),
            EVOLUTION_ROLLBACK_RECEIPT_MEDIA_TYPE,
            self._schema_version,
        )
        self._registry.compare_and_swap(
            promotion.target_id,
            promotion.promoted_ref,
            promotion.previous_active_ref,
        )
        return receipt, receipt_ref

    def _verify_campaign_result_artifact(self, execution: EvolutionCampaignExecution) -> None:
        if execution.result_ref.media_type != EVOLUTION_CAMPAIGN_RESULT_MEDIA_TYPE:
            raise EvolutionPromotionError("campaign result artifact has the wrong media type")
        try:
            persisted = EvolutionCampaignResult.model_validate_json(
                self._artifacts.read_verified(execution.result_ref), strict=True
            )
        except (RuntimeError, ValueError) as error:
            raise EvolutionPromotionError("campaign result artifact is invalid") from error
        if persisted != execution.result:
            raise EvolutionPromotionError("campaign result artifact differs from execution")

    def _verify_promotion_artifact(
        self, promotion: EvolutionPromotionReceipt, promotion_ref: ArtifactRef
    ) -> None:
        if promotion_ref.media_type != EVOLUTION_PROMOTION_RECEIPT_MEDIA_TYPE:
            raise EvolutionPromotionError("promotion artifact has the wrong media type")
        try:
            persisted = EvolutionPromotionReceipt.model_validate_json(
                self._artifacts.read_verified(promotion_ref), strict=True
            )
        except (RuntimeError, ValueError) as error:
            raise EvolutionPromotionError("promotion artifact is invalid") from error
        if persisted != promotion:
            raise EvolutionPromotionError("promotion artifact differs from receipt")


__all__ = [
    "EvolutionCampaignExecution",
    "EvolutionCampaignExecutor",
    "EvolutionCampaignProtocolError",
    "EvolutionEvaluationRunner",
    "EvolutionPromotionError",
    "EvolutionPromotionService",
    "EvolutionVersionRegistry",
    "EvolutionVersionRegistryPort",
    "InMemorySealedAcceptanceLedger",
    "SealedAcceptanceAlreadyOpened",
    "SealedAcceptanceLedgerPort",
]
