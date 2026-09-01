from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from novel_agent.adapters.filesystem import (
    EvolutionManifestAlreadyFrozen,
    FilesystemEvolutionCampaignRepository,
    FilesystemEvolutionVersionRegistry,
    FilesystemObjectStore,
    FilesystemSealedAcceptanceLedger,
)
from novel_agent.domain.evolution import (
    EVOLUTION_CAMPAIGN_RESULT_MEDIA_TYPE,
    EVOLUTION_PROMOTION_RECEIPT_MEDIA_TYPE,
    EvolutionCampaignManifest,
    EvolutionCampaignResult,
    EvolutionCandidate,
    EvolutionCandidateGenerationBudget,
    EvolutionCheckpointAssignment,
    EvolutionCheckpointResult,
    EvolutionCheckpointUse,
    EvolutionDecision,
    EvolutionIncident,
    EvolutionIncidentCluster,
    EvolutionMetricComparison,
    EvolutionMetricThreshold,
    EvolutionTargetKind,
)
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.runtime import FailureClass
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.evolution import (
    EvolutionCampaignExecution,
    EvolutionCampaignExecutor,
    EvolutionCampaignProtocolError,
    EvolutionPromotionError,
    EvolutionPromotionService,
    EvolutionVersionRegistry,
    InMemorySealedAcceptanceLedger,
    SealedAcceptanceAlreadyOpened,
)

SCHEMA = SchemaVersion("1.0.0")
NOW = datetime(2026, 8, 31, tzinfo=UTC)


def _put(repository: ArtifactRepository, value: object):
    return repository.put(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        "application/json",
        SCHEMA,
    )


def _manifest(
    repository: ArtifactRepository,
    *,
    target_kind: EvolutionTargetKind = EvolutionTargetKind.PROMPT,
    human_gate: bool = False,
) -> EvolutionCampaignManifest:
    base = _put(repository, {"version": "base"})
    candidate_ref = _put(repository, {"version": "candidate"})
    incidents = (
        _put(repository, {"incident": 1}),
        _put(repository, {"incident": 2}),
    )
    candidate = EvolutionCandidate(
        candidate_id=StableId("evolution.candidate.1"),
        target_id=StableId("prompt.writer"),
        target_kind=target_kind,
        base_artifact_ref=base,
        candidate_artifact_ref=candidate_ref,
        incident_refs=incidents,
        affected_contract_ids=(StableId("prompt.writer"),),
        change_summary="Clarify one evidence selection instruction.",
        requires_human_gate=human_gate,
    )
    assignments = tuple(
        EvolutionCheckpointAssignment(
            checkpoint_id=StableId(f"checkpoint.{use.value}"),
            use=use,
            basis_commit=CommitId("sha256:" + digit * 64),
            basis_ref=_put(repository, {"checkpoint": use.value}),
            incident_ids=(StableId(f"incident.{use.value}"),),
        )
        for use, digit in (
            (EvolutionCheckpointUse.CALIBRATION, "1"),
            (EvolutionCheckpointUse.SEALED_ACCEPTANCE, "2"),
            (EvolutionCheckpointUse.CANARY, "3"),
        )
    )
    return EvolutionCampaignManifest(
        campaign_id=StableId("evolution.campaign.1"),
        candidate=candidate,
        assignments=assignments,
        thresholds=(
            EvolutionMetricThreshold(metric="safe_action_accuracy", minimum_delta=0.1),
            EvolutionMetricThreshold(
                metric="invalid_repairs", minimum_delta=0.0, higher_is_better=False
            ),
        ),
        code_source_fingerprint=ArtifactId("sha256:" + "a" * 64),
        configuration_fingerprint=ArtifactId("sha256:" + "b" * 64),
        preregistered_at=NOW,
    )


class _Runner:
    def __init__(
        self,
        evidence_ref,
        *,
        fail_use: EvolutionCheckpointUse | None = None,
    ) -> None:
        self.evidence_ref = evidence_ref
        self.fail_use = fail_use
        self.calls: list[EvolutionCheckpointUse] = []

    async def run(self, assignment, candidate):
        self.calls.append(assignment.use)
        failed = assignment.use is self.fail_use
        return EvolutionCheckpointResult(
            checkpoint_id=assignment.checkpoint_id,
            use=assignment.use,
            basis_commit=assignment.basis_commit,
            candidate_artifact_id=candidate.candidate_artifact_ref.artifact_id,
            comparisons=(
                EvolutionMetricComparison(
                    metric="safe_action_accuracy",
                    baseline_value=0.5,
                    candidate_value=0.4 if failed else 0.7,
                ),
                EvolutionMetricComparison(
                    metric="invalid_repairs",
                    baseline_value=2.0,
                    candidate_value=3.0 if failed else 1.0,
                ),
            ),
            hard_failure_codes=("LEAKAGE",) if failed else (),
            evidence_refs=(self.evidence_ref,),
        )


def _execute(
    repository: ArtifactRepository,
    manifest: EvolutionCampaignManifest,
    *,
    fail_use: EvolutionCheckpointUse | None = None,
    sealed_ledger=None,
):
    return asyncio.run(
        EvolutionCampaignExecutor(
            artifacts=repository,
            sealed_ledger=sealed_ledger or InMemorySealedAcceptanceLedger(),
            schema_version=SCHEMA,
        ).execute(manifest, _Runner(_put(repository, {"evidence": "run"}), fail_use=fail_use))
    )


def test_u8d_u8e_pass_promotes_and_rolls_back_with_receipts(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    runner = _Runner(_put(repository, {"evidence": "pass"}))
    executor = EvolutionCampaignExecutor(
        artifacts=repository,
        sealed_ledger=InMemorySealedAcceptanceLedger(),
        schema_version=SCHEMA,
    )

    execution = asyncio.run(executor.execute(manifest, runner))

    assert execution.result.decision is EvolutionDecision.PROMOTE
    assert runner.calls == [
        EvolutionCheckpointUse.CALIBRATION,
        EvolutionCheckpointUse.SEALED_ACCEPTANCE,
        EvolutionCheckpointUse.CANARY,
    ]
    assert execution.result.sealed_opened_once is True
    assert execution.result.evaluator_feedback_written_back is False
    assert execution.result.active_version_changed is False

    registry = EvolutionVersionRegistry(
        {manifest.candidate.target_id: manifest.candidate.base_artifact_ref}
    )
    promotions = EvolutionPromotionService(
        artifacts=repository,
        registry=registry,
        schema_version=SCHEMA,
    )
    promotion, promotion_ref = promotions.promote(
        manifest=manifest,
        execution=execution,
        promoted_at=NOW,
    )
    assert registry.active(manifest.candidate.target_id) == (
        manifest.candidate.candidate_artifact_ref
    )

    failure = _put(repository, {"canary": "regressed after promotion"})
    rollback, rollback_ref = promotions.rollback(
        promotion=promotion,
        promotion_ref=promotion_ref,
        failure_evidence_refs=(failure,),
        rolled_back_at=NOW,
    )
    assert registry.active(manifest.candidate.target_id) == manifest.candidate.base_artifact_ref
    assert rollback.restored_ref == manifest.candidate.base_artifact_ref
    assert repository.read_verified(rollback_ref)


def test_u8e_durable_active_identity_survives_promotion_and_rollback_restart(
    tmp_path: Path,
) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    executor = EvolutionCampaignExecutor(
        artifacts=repository,
        sealed_ledger=FilesystemSealedAcceptanceLedger(tmp_path / "sealed"),
        schema_version=SCHEMA,
    )
    execution = asyncio.run(
        executor.execute(manifest, _Runner(_put(repository, {"evidence": "pass"})))
    )
    versions_root = tmp_path / "active-versions"
    registry = FilesystemEvolutionVersionRegistry(versions_root)
    registry.initialize(manifest.candidate.target_id, manifest.candidate.base_artifact_ref)
    promotions = EvolutionPromotionService(
        artifacts=repository,
        registry=registry,
        schema_version=SCHEMA,
    )

    promotion, promotion_ref = promotions.promote(
        manifest=manifest,
        execution=execution,
        promoted_at=NOW,
    )
    restarted = FilesystemEvolutionVersionRegistry(versions_root)
    assert restarted.active(manifest.candidate.target_id) == (
        manifest.candidate.candidate_artifact_ref
    )

    promotions_after_restart = EvolutionPromotionService(
        artifacts=repository,
        registry=restarted,
        schema_version=SCHEMA,
    )
    promotions_after_restart.rollback(
        promotion=promotion,
        promotion_ref=promotion_ref,
        failure_evidence_refs=(_put(repository, {"canary": "regressed"}),),
        rolled_back_at=NOW,
    )
    assert (
        FilesystemEvolutionVersionRegistry(versions_root).active(manifest.candidate.target_id)
        == manifest.candidate.base_artifact_ref
    )


def test_u8e_sealed_checkpoint_is_durable_and_one_shot(tmp_path: Path) -> None:
    ledger_root = tmp_path / "sealed"
    first = FilesystemSealedAcceptanceLedger(ledger_root)
    first.claim(StableId("campaign.sealed"), StableId("checkpoint.95"))

    restarted = FilesystemSealedAcceptanceLedger(ledger_root)
    with pytest.raises(SealedAcceptanceAlreadyOpened, match="already opened"):
        restarted.claim(StableId("campaign.sealed"), StableId("checkpoint.95"))


def test_u8e_manifest_is_write_once_and_survives_process_restart(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    manifest_root = tmp_path / "campaigns"
    first = FilesystemEvolutionCampaignRepository(manifest_root)
    frozen_path = first.freeze(manifest)

    restarted = FilesystemEvolutionCampaignRepository(manifest_root)
    assert restarted.load(manifest.campaign_id.root) == manifest
    with pytest.raises(EvolutionManifestAlreadyFrozen, match="already frozen"):
        restarted.freeze(manifest)
    assert frozen_path.read_bytes()


def test_u8e_failed_sealed_result_is_rejected_and_never_runs_canary(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    runner = _Runner(
        _put(repository, {"evidence": "sealed failure"}),
        fail_use=EvolutionCheckpointUse.SEALED_ACCEPTANCE,
    )
    executor = EvolutionCampaignExecutor(
        artifacts=repository,
        sealed_ledger=InMemorySealedAcceptanceLedger(),
        schema_version=SCHEMA,
    )

    execution = asyncio.run(executor.execute(manifest, runner))

    assert execution.result.decision is EvolutionDecision.REJECT
    assert execution.result.decision_codes == ("SEALED_ACCEPTANCE_FAILED",)
    assert runner.calls == [
        EvolutionCheckpointUse.CALIBRATION,
        EvolutionCheckpointUse.SEALED_ACCEPTANCE,
    ]


def test_u8e_failed_canary_result_is_rejected_after_sealed(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    runner = _Runner(
        _put(repository, {"evidence": "canary failure"}),
        fail_use=EvolutionCheckpointUse.CANARY,
    )
    execution = asyncio.run(
        EvolutionCampaignExecutor(
            artifacts=repository,
            sealed_ledger=InMemorySealedAcceptanceLedger(),
            schema_version=SCHEMA,
        ).execute(manifest, runner)
    )
    assert execution.result.decision is EvolutionDecision.REJECT
    assert execution.result.decision_codes == ("CANARY_FAILED",)
    assert execution.result.sealed_opened_once is True
    assert runner.calls == [
        EvolutionCheckpointUse.CALIBRATION,
        EvolutionCheckpointUse.SEALED_ACCEPTANCE,
        EvolutionCheckpointUse.CANARY,
    ]


def test_u8d_code_candidate_stops_at_human_gate(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(
        repository,
        target_kind=EvolutionTargetKind.CODE,
        human_gate=True,
    )
    executor = EvolutionCampaignExecutor(
        artifacts=repository,
        sealed_ledger=InMemorySealedAcceptanceLedger(),
        schema_version=SCHEMA,
    )
    execution = asyncio.run(
        executor.execute(manifest, _Runner(_put(repository, {"evidence": "pass"})))
    )

    assert execution.result.decision is EvolutionDecision.HUMAN_REVIEW
    promotions = EvolutionPromotionService(
        artifacts=repository,
        registry=EvolutionVersionRegistry(
            {manifest.candidate.target_id: manifest.candidate.base_artifact_ref}
        ),
        schema_version=SCHEMA,
    )
    with pytest.raises(EvolutionPromotionError, match="does not authorize"):
        promotions.promote(manifest=manifest, execution=execution, promoted_at=NOW)


def test_u8e_manifest_rejects_checkpoint_reuse_across_splits(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    assignments = list(manifest.assignments)
    assignments[1] = assignments[1].model_copy(
        update={"checkpoint_id": assignments[0].checkpoint_id}
    )

    with pytest.raises(ValidationError, match="exactly one campaign use"):
        EvolutionCampaignManifest(
            campaign_id=manifest.campaign_id,
            candidate=manifest.candidate,
            assignments=tuple(assignments),
            thresholds=manifest.thresholds,
            code_source_fingerprint=manifest.code_source_fingerprint,
            configuration_fingerprint=manifest.configuration_fingerprint,
            preregistered_at=manifest.preregistered_at,
        )


def test_u8e_manifest_rejects_incident_reuse_across_splits(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    assignments = list(manifest.assignments)
    assignments[1] = assignments[1].model_copy(update={"incident_ids": assignments[0].incident_ids})

    with pytest.raises(ValidationError, match="incident identity"):
        EvolutionCampaignManifest(
            campaign_id=manifest.campaign_id,
            candidate=manifest.candidate,
            assignments=tuple(assignments),
            thresholds=manifest.thresholds,
            code_source_fingerprint=manifest.code_source_fingerprint,
            configuration_fingerprint=manifest.configuration_fingerprint,
            preregistered_at=manifest.preregistered_at,
        )


def test_u8e_promotion_result_cannot_omit_sealed_or_canary_evidence(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)

    with pytest.raises(ValidationError, match="sealed and canary evidence"):
        EvolutionCampaignResult(
            campaign_id=manifest.campaign_id,
            candidate_id=manifest.candidate.candidate_id,
            decision=EvolutionDecision.PROMOTE,
            calibration_results=(
                asyncio.run(
                    _Runner(_put(repository, {"evidence": "calibration"})).run(
                        manifest.assignments[0], manifest.candidate
                    )
                ),
            ),
            decision_codes=("INVALID_PROMOTE",),
            sealed_opened_once=False,
        )


def test_u8e_campaign_result_rejects_sealed_evidence_without_opening(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    sealed_result = asyncio.run(
        _Runner(_put(repository, {"evidence": "sealed"})).run(
            manifest.assignments[1], manifest.candidate
        )
    )

    with pytest.raises(ValidationError, match="one-shot opening"):
        EvolutionCampaignResult(
            campaign_id=manifest.campaign_id,
            candidate_id=manifest.candidate.candidate_id,
            decision=EvolutionDecision.REJECT,
            calibration_results=(sealed_result,),
            sealed_results=(sealed_result,),
            decision_codes=("INVALID",),
            sealed_opened_once=False,
        )

    canary_result = asyncio.run(
        _Runner(_put(repository, {"evidence": "canary"})).run(
            manifest.assignments[2], manifest.candidate
        )
    )
    with pytest.raises(ValidationError, match="canary results require sealed evidence"):
        EvolutionCampaignResult(
            campaign_id=manifest.campaign_id,
            candidate_id=manifest.candidate.candidate_id,
            decision=EvolutionDecision.REJECT,
            calibration_results=(sealed_result,),
            canary_results=(canary_result,),
            decision_codes=("INVALID_CANARY_ORDER",),
            sealed_opened_once=False,
        )


def test_u8e_domain_validators_reject_duplicate_or_mismatched_identities(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    incident = EvolutionIncident(
        incident_id=StableId("incident.domain.one"),
        problem_key=StableId("problem.domain"),
        failure_class=FailureClass.CANON_EXTRACTION_GAP,
        safety_boundary_id=ArtifactId("sha256:" + "c" * 64),
        target_id=StableId("prompt.writer"),
        target_kind=EvolutionTargetKind.PROMPT,
        incident_ref=manifest.candidate.incident_refs[0],
    )
    with pytest.raises(ValidationError, match="identities must be unique"):
        EvolutionIncidentCluster(
            cluster_id=StableId("cluster.domain.duplicate"),
            problem_key=incident.problem_key,
            failure_class=incident.failure_class,
            safety_boundary_id=incident.safety_boundary_id,
            target_id=incident.target_id,
            target_kind=incident.target_kind,
            incidents=(incident, incident),
        )
    mismatched = incident.model_copy(
        update={
            "incident_id": StableId("incident.domain.two"),
            "problem_key": StableId("problem.other"),
        }
    )
    with pytest.raises(ValidationError, match="differs from its evolution cluster"):
        EvolutionIncidentCluster(
            cluster_id=StableId("cluster.domain.mismatch"),
            problem_key=incident.problem_key,
            failure_class=incident.failure_class,
            safety_boundary_id=incident.safety_boundary_id,
            target_id=incident.target_id,
            target_kind=incident.target_kind,
            incidents=(incident, mismatched),
        )

    candidate_payload = manifest.candidate.model_dump(mode="python")
    with pytest.raises(ValidationError, match="differ from its active base"):
        EvolutionCandidate(
            **{
                **candidate_payload,
                "candidate_artifact_ref": manifest.candidate.base_artifact_ref,
            }
        )
    with pytest.raises(ValidationError, match="incident corpus must be unique"):
        EvolutionCandidate(
            **{**candidate_payload, "incident_refs": (manifest.candidate.incident_refs[0],) * 2}
        )
    with pytest.raises(ValidationError, match="affected contract identities"):
        EvolutionCandidate(
            **{**candidate_payload, "affected_contract_ids": (StableId("prompt.writer"),) * 2}
        )
    with pytest.raises(ValidationError, match="always requires"):
        EvolutionCandidate(
            **{
                **candidate_payload,
                "target_kind": EvolutionTargetKind.CODE,
                "requires_human_gate": False,
            }
        )


def test_u8e_domain_validators_reject_invalid_budget_split_thresholds_and_metrics(
    tmp_path: Path,
) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    with pytest.raises(ValidationError, match="thinking flag and thinking budget"):
        EvolutionCandidateGenerationBudget(enable_thinking=True, thinking_token_budget=0)
    with pytest.raises(ValidationError, match="campaign requires"):
        EvolutionCampaignManifest(
            **{
                **manifest.model_dump(mode="python"),
                "assignments": tuple(
                    assignment.model_copy(update={"use": EvolutionCheckpointUse.CALIBRATION})
                    for assignment in manifest.assignments
                ),
            }
        )
    with pytest.raises(ValidationError, match="thresholds must be unique"):
        EvolutionCampaignManifest(
            **{
                **manifest.model_dump(mode="python"),
                "thresholds": (manifest.thresholds[0], manifest.thresholds[0]),
            }
        )
    with pytest.raises(ValidationError, match="finite"):
        EvolutionMetricThreshold(metric="nan", minimum_delta=float("nan"))
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        EvolutionMetricThreshold(metric="negative", minimum_delta=-0.1)

    result = asyncio.run(
        _Runner(_put(repository, {"evidence": "metrics"})).run(
            manifest.assignments[0], manifest.candidate
        )
    )
    with pytest.raises(ValidationError, match="metrics must be unique"):
        EvolutionCheckpointResult(
            **{
                **result.model_dump(mode="python"),
                "comparisons": (result.comparisons[0], result.comparisons[0]),
            }
        )


def test_u8e_inmemory_ledger_and_version_registry_fail_closed(tmp_path: Path) -> None:
    ledger = InMemorySealedAcceptanceLedger()
    campaign_id = StableId("campaign.inmemory")
    checkpoint_id = StableId("checkpoint.inmemory")
    ledger.claim(campaign_id, checkpoint_id)
    with pytest.raises(SealedAcceptanceAlreadyOpened, match="already opened"):
        ledger.claim(campaign_id, checkpoint_id)

    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    registry = FilesystemEvolutionVersionRegistry(tmp_path / "versions")
    assert registry.active(manifest.candidate.target_id) is None
    with pytest.raises(EvolutionPromotionError, match="changed before promotion"):
        registry.require_active(manifest.candidate.target_id, manifest.candidate.base_artifact_ref)
    registry.initialize(manifest.candidate.target_id, manifest.candidate.base_artifact_ref)
    registry.initialize(manifest.candidate.target_id, manifest.candidate.base_artifact_ref)
    with pytest.raises(EvolutionPromotionError, match="changed before promotion"):
        registry.initialize(manifest.candidate.target_id, manifest.candidate.candidate_artifact_ref)


def test_u8e_calibration_failure_never_opens_sealed_or_runs_later_phases(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    runner = _Runner(
        _put(repository, {"evidence": "calibration failure"}),
        fail_use=EvolutionCheckpointUse.CALIBRATION,
    )
    execution = asyncio.run(
        EvolutionCampaignExecutor(
            artifacts=repository,
            sealed_ledger=InMemorySealedAcceptanceLedger(),
            schema_version=SCHEMA,
        ).execute(manifest, runner)
    )
    assert execution.result.decision is EvolutionDecision.REJECT
    assert execution.result.decision_codes == ("CALIBRATION_FAILED",)
    assert execution.result.sealed_opened_once is False
    assert runner.calls == [EvolutionCheckpointUse.CALIBRATION]


def test_u8e_campaign_rejects_missing_preregistered_metric(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository).model_copy(
        update={"thresholds": (EvolutionMetricThreshold(metric="not_returned", minimum_delta=0.0),)}
    )
    execution = _execute(repository, manifest)
    assert execution.result.decision is EvolutionDecision.REJECT
    assert execution.result.decision_codes == ("CALIBRATION_FAILED",)


def test_u8e_campaign_rejects_metric_below_registered_delta(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    evidence = _put(repository, {"evidence": "low delta"})

    class _LowDeltaRunner:
        async def run(self, assignment, candidate):
            result = await _Runner(evidence).run(assignment, candidate)
            comparisons = (
                result.comparisons[0].model_copy(update={"candidate_value": 0.55}),
                result.comparisons[1],
            )
            return result.model_copy(update={"comparisons": comparisons})

    execution = asyncio.run(
        EvolutionCampaignExecutor(
            artifacts=repository,
            sealed_ledger=InMemorySealedAcceptanceLedger(),
            schema_version=SCHEMA,
        ).execute(manifest, _LowDeltaRunner())
    )
    assert execution.result.decision is EvolutionDecision.REJECT
    assert execution.result.decision_codes == ("CALIBRATION_FAILED",)


def test_u8e_empty_phase_is_a_protocol_error(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    empty_calibration = manifest.model_copy(update={"assignments": manifest.assignments[1:]})
    executor = EvolutionCampaignExecutor(
        artifacts=repository,
        sealed_ledger=InMemorySealedAcceptanceLedger(),
        schema_version=SCHEMA,
    )
    with pytest.raises(EvolutionCampaignProtocolError, match="no calibration checkpoint"):
        asyncio.run(
            executor._run_phase(  # type: ignore[attr-defined]
                empty_calibration,
                _Runner(_put(repository, {"evidence": "empty phase"})),
                EvolutionCheckpointUse.CALIBRATION,
                claim_sealed=False,
            )
        )


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("checkpoint_id", "another checkpoint identity"),
        ("use", "changed the checkpoint use"),
        ("basis_commit", "changed the checkpoint basis"),
        ("candidate_artifact_id", "another candidate version"),
    ),
)
def test_u8e_runner_evidence_identity_is_fail_closed(
    tmp_path: Path, field: str, message: str
) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    evidence = _put(repository, {"evidence": "identity mismatch"})

    class _MismatchRunner:
        async def run(self, assignment, candidate):
            good = await _Runner(evidence).run(assignment, candidate)
            updates = {
                "checkpoint_id": StableId("checkpoint.other"),
                "use": EvolutionCheckpointUse.CANARY,
                "basis_commit": CommitId("sha256:" + "9" * 64),
                "candidate_artifact_id": ArtifactId("sha256:" + "9" * 64),
            }
            return good.model_copy(update={field: updates[field]})

    with pytest.raises(EvolutionCampaignProtocolError, match=message):
        asyncio.run(
            EvolutionCampaignExecutor(
                artifacts=repository,
                sealed_ledger=InMemorySealedAcceptanceLedger(),
                schema_version=SCHEMA,
            ).execute(manifest, _MismatchRunner())
        )


def test_u8e_promotion_requires_the_durable_result_artifact_and_lineage(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    execution = _execute(repository, manifest)
    service = EvolutionPromotionService(
        artifacts=repository,
        registry=EvolutionVersionRegistry(
            {manifest.candidate.target_id: manifest.candidate.base_artifact_ref}
        ),
        schema_version=SCHEMA,
    )

    tampered = EvolutionCampaignExecution(
        result=execution.result.model_copy(update={"decision_codes": ("tampered",)}),
        result_ref=execution.result_ref,
    )
    with pytest.raises(EvolutionPromotionError, match="differs from execution"):
        service.promote(manifest=manifest, execution=tampered, promoted_at=NOW)

    wrong_media = EvolutionCampaignExecution(
        result=execution.result,
        result_ref=_put(repository, {"not": "a campaign result"}),
    )
    with pytest.raises(EvolutionPromotionError, match="wrong media type"):
        service.promote(manifest=manifest, execution=wrong_media, promoted_at=NOW)

    wrong_campaign = EvolutionCampaignExecution(
        result=execution.result.model_copy(update={"campaign_id": StableId("campaign.other")}),
        result_ref=execution.result_ref,
    )
    with pytest.raises(EvolutionPromotionError, match="identity differs"):
        service.promote(manifest=manifest, execution=wrong_campaign, promoted_at=NOW)

    wrong_candidate = EvolutionCampaignExecution(
        result=execution.result.model_copy(update={"candidate_id": StableId("candidate.other")}),
        result_ref=execution.result_ref,
    )
    with pytest.raises(EvolutionPromotionError, match="candidate differs"):
        service.promote(manifest=manifest, execution=wrong_candidate, promoted_at=NOW)

    invalid_ref = repository.put(b"{}", EVOLUTION_CAMPAIGN_RESULT_MEDIA_TYPE, SCHEMA)
    with pytest.raises(EvolutionPromotionError, match="artifact is invalid"):
        service.promote(
            manifest=manifest,
            execution=EvolutionCampaignExecution(
                result=execution.result,
                result_ref=invalid_ref,
            ),
            promoted_at=NOW,
        )

    human_candidate = manifest.candidate.model_copy(update={"requires_human_gate": True})
    human_manifest = manifest.model_copy(update={"candidate": human_candidate})
    with pytest.raises(EvolutionPromotionError, match="human-gated"):
        service.promote(manifest=human_manifest, execution=execution, promoted_at=NOW)

    missing_evidence = EvolutionCampaignExecution(
        result=execution.result.model_copy(update={"sealed_results": (), "canary_results": ()}),
        result_ref=execution.result_ref,
    )
    with pytest.raises(EvolutionPromotionError, match="sealed and canary evidence"):
        service.promote(manifest=manifest, execution=missing_evidence, promoted_at=NOW)


def test_u8e_rollback_requires_matching_promotion_receipt_and_active_identity(
    tmp_path: Path,
) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    manifest = _manifest(repository)
    execution = _execute(repository, manifest)
    registry = EvolutionVersionRegistry(
        {manifest.candidate.target_id: manifest.candidate.base_artifact_ref}
    )
    service = EvolutionPromotionService(
        artifacts=repository,
        registry=registry,
        schema_version=SCHEMA,
    )
    promotion, promotion_ref = service.promote(
        manifest=manifest, execution=execution, promoted_at=NOW
    )
    with pytest.raises(EvolutionPromotionError, match="failure evidence"):
        service.rollback(
            promotion=promotion,
            promotion_ref=promotion_ref,
            failure_evidence_refs=(),
            rolled_back_at=NOW,
        )
    with pytest.raises(EvolutionPromotionError, match="wrong media type"):
        service.rollback(
            promotion=promotion,
            promotion_ref=execution.result_ref,
            failure_evidence_refs=(_put(repository, {"failure": True}),),
            rolled_back_at=NOW,
        )
    invalid_promotion_ref = repository.put(b"{}", EVOLUTION_PROMOTION_RECEIPT_MEDIA_TYPE, SCHEMA)
    with pytest.raises(EvolutionPromotionError, match="artifact is invalid"):
        service.rollback(
            promotion=promotion,
            promotion_ref=invalid_promotion_ref,
            failure_evidence_refs=(_put(repository, {"failure": True}),),
            rolled_back_at=NOW,
        )
    tampered = promotion.model_copy(update={"target_id": StableId("target.other")})
    with pytest.raises(EvolutionPromotionError, match="differs from receipt"):
        service.rollback(
            promotion=tampered,
            promotion_ref=promotion_ref,
            failure_evidence_refs=(_put(repository, {"failure": True}),),
            rolled_back_at=NOW,
        )
    registry.compare_and_swap(
        manifest.candidate.target_id,
        manifest.candidate.candidate_artifact_ref,
        manifest.candidate.base_artifact_ref,
    )
    with pytest.raises(EvolutionPromotionError, match="changed before promotion"):
        service.rollback(
            promotion=promotion,
            promotion_ref=promotion_ref,
            failure_evidence_refs=(_put(repository, {"failure": True}),),
            rolled_back_at=NOW,
        )
