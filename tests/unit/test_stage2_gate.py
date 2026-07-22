from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.stage2 import (
    ControllerPromotionDecision,
    FailureLedgerRef,
    FailureLedgerType,
    Stage2GateEvidence,
    Stage2GateReport,
    Stage2GateVerdict,
)
from novel_agent.services.stage2_gate import Stage2GateEvaluator

VERSION = SchemaVersion("2.0.0")
HASH = ArtifactId("sha256:" + "a" * 64)


def artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=HASH,
        media_type="application/json",
        byte_length=1,
        schema_version=VERSION,
    )


def evidence(**updates: object) -> Stage2GateEvidence:
    values: dict[str, object] = {
        "evidence_id": StableId("evidence.stage2-gate"),
        "configuration_fingerprint": HASH,
        "real_bundle_hash": HASH,
        "bootstrap_user_data_imported": True,
        "source_traceability_passed": True,
        "genesis_author_approved": True,
        "contracts_versioned": True,
        "controller_read_only_bounded": True,
        "tool_calls_replayable": True,
        "controller_checkpoint_resume_passed": True,
        "future_leakage_count": 0,
        "paired_results_count": 10,
        "held_out_complex_classes_with_gain": 1,
        "agentic_gain_proven": True,
        "agentic_safety_regression_count": 0,
        "curator_real_replay_passed": True,
        "real_replay_chapters": 100,
        "rejected_patch_pollution_count": 0,
        "freshness_violation_count": 0,
        "evaluation_ledger_complete": True,
        "checkpoint_chapters": (20, 40, 60, 80, 95),
        "checkpoint_chain_consistent": True,
        "future_isolation_failure_count": 0,
        "information_profiles_separate": True,
        "failure_ledgers": tuple(
            FailureLedgerRef(ledger_type=kind, artifact=artifact(), entry_count=0)
            for kind in FailureLedgerType
        ),
    }
    values.update(updates)
    return Stage2GateEvidence.model_validate(values)


def test_stage2_gate_pass_accepts_bounded_default_only_with_all_evidence() -> None:
    report = Stage2GateEvaluator().evaluate(evidence())
    assert report.verdict is Stage2GateVerdict.PASS
    assert report.controller_promotion is ControllerPromotionDecision.ACCEPT_BOUNDED_DEFAULT
    assert report.memory_gateway_frozen is True
    assert report.blockers == ()
    assert all(report.checks.values())


def test_stage2_gate_conditional_pass_freezes_deterministic_gateway_without_gain() -> None:
    report = Stage2GateEvaluator().evaluate(
        evidence(agentic_gain_proven=False, held_out_complex_classes_with_gain=0)
    )
    assert report.verdict is Stage2GateVerdict.CONDITIONAL_PASS
    assert report.controller_promotion is ControllerPromotionDecision.FREEZE_DETERMINISTIC_GATEWAY
    assert report.memory_gateway_frozen is True
    assert report.blockers == ("controller.held_out_gain",)


def test_stage2_gate_missing_real_runs_is_incomplete_not_a_synthetic_pass() -> None:
    report = Stage2GateEvaluator().evaluate(
        evidence(
            real_bundle_hash=None,
            bootstrap_user_data_imported=False,
            paired_results_count=0,
            agentic_gain_proven=False,
            held_out_complex_classes_with_gain=0,
            curator_real_replay_passed=False,
            real_replay_chapters=0,
            evaluation_ledger_complete=False,
            checkpoint_chapters=(),
            checkpoint_chain_consistent=False,
            failure_ledgers=(),
        )
    )
    assert report.verdict is Stage2GateVerdict.INCOMPLETE
    assert report.controller_promotion is ControllerPromotionDecision.DEFER
    assert report.memory_gateway_frozen is False
    assert "report.real_bundle_pinned" in report.blockers
    assert "curator.replay_length" in report.blockers

    unknown = Stage2GateEvaluator().evaluate(
        Stage2GateEvidence(
            evidence_id=StableId("evidence.unknown"),
            configuration_fingerprint=HASH,
        )
    )
    assert unknown.verdict is Stage2GateVerdict.INCOMPLETE
    assert "bootstrap.genesis_author_approved" in unknown.blockers


def test_stage2_gate_noncritical_requirement_failure_is_still_a_fail() -> None:
    report = Stage2GateEvaluator().evaluate(evidence(bootstrap_user_data_imported=False))
    assert report.verdict is Stage2GateVerdict.FAIL
    assert "bootstrap.user_data_imported" in report.blockers


@pytest.mark.parametrize(
    "updates",
    (
        {"future_leakage_count": 1},
        {"rejected_patch_pollution_count": 1},
        {"freshness_violation_count": 1},
        {"future_isolation_failure_count": 1},
        {"agentic_safety_regression_count": 1},
        {"source_traceability_passed": False},
    ),
)
def test_stage2_gate_critical_safety_failure_rejects_architecture(
    updates: dict[str, object],
) -> None:
    report = Stage2GateEvaluator().evaluate(evidence(**updates))
    assert report.verdict is Stage2GateVerdict.FAIL
    assert report.controller_promotion is ControllerPromotionDecision.REJECT_ARCHITECTURE
    assert report.memory_gateway_frozen is False


def test_stage2_gate_contracts_reject_inconsistent_evidence_and_decisions() -> None:
    with pytest.raises(ValidationError, match="unique and ascending"):
        evidence(checkpoint_chapters=(40, 20))
    ledgers = evidence().failure_ledgers
    with pytest.raises(ValidationError, match="ledger types must be unique"):
        evidence(failure_ledgers=(ledgers[0], ledgers[0]))
    with pytest.raises(ValidationError, match="paired held-out"):
        evidence(paired_results_count=0)
    valid = Stage2GateEvaluator().evaluate(evidence())
    with pytest.raises(ValidationError, match="contradicts"):
        Stage2GateReport.model_validate(
            valid.model_dump() | {"controller_promotion": ControllerPromotionDecision.DEFER}
        )
