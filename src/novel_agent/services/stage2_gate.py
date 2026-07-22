"""Formal Stage 2A gate; missing real evidence remains explicitly incomplete."""

from __future__ import annotations

from novel_agent.domain.ids import StableId
from novel_agent.domain.stage2 import (
    ControllerPromotionDecision,
    FailureLedgerType,
    Stage2GateEvidence,
    Stage2GateReport,
    Stage2GateVerdict,
)

REQUIRED_CHECKPOINTS = (20, 40, 60, 80, 95)


class Stage2GateEvaluator:
    def evaluate(self, evidence: Stage2GateEvidence) -> Stage2GateReport:
        ledger_types = {item.ledger_type for item in evidence.failure_ledgers}
        checks = {
            "bootstrap.user_data_imported": evidence.bootstrap_user_data_imported is True,
            "bootstrap.source_traceability": evidence.source_traceability_passed is True,
            "bootstrap.genesis_author_approved": evidence.genesis_author_approved is True,
            "contracts.versioned_and_fingerprinted": evidence.contracts_versioned is True,
            "controller.read_only_and_bounded": evidence.controller_read_only_bounded is True,
            "controller.tool_calls_replayable": evidence.tool_calls_replayable is True,
            "controller.checkpoint_resume": evidence.controller_checkpoint_resume_passed is True,
            "controller.future_leakage_zero": evidence.future_leakage_count == 0,
            "controller.paired_results_present": evidence.paired_results_count is not None
            and evidence.paired_results_count > 0,
            "controller.held_out_gain": evidence.agentic_gain_proven is True
            and evidence.held_out_complex_classes_with_gain is not None
            and evidence.held_out_complex_classes_with_gain > 0,
            "controller.safety_no_regression": evidence.agentic_safety_regression_count == 0,
            "curator.real_replay_passed": evidence.curator_real_replay_passed is True,
            "curator.replay_length": evidence.real_replay_chapters is not None
            and evidence.real_replay_chapters >= 50,
            "write_side.rejected_patch_pollution_zero": (
                evidence.rejected_patch_pollution_count == 0
            ),
            "projection.freshness_violations_zero": evidence.freshness_violation_count == 0,
            "evaluation.ledger_complete": evidence.evaluation_ledger_complete is True,
            "scenario.required_checkpoints": evidence.checkpoint_chapters == REQUIRED_CHECKPOINTS,
            "scenario.receipt_chain_consistent": evidence.checkpoint_chain_consistent is True,
            "scenario.future_isolation": evidence.future_isolation_failure_count == 0,
            "scenario.information_profiles_separate": evidence.information_profiles_separate
            is True,
            "report.failure_ledgers_complete": ledger_types == set(FailureLedgerType),
            "report.real_bundle_pinned": evidence.real_bundle_hash is not None,
        }
        critical_checks = {
            "bootstrap.source_traceability",
            "bootstrap.genesis_author_approved",
            "contracts.versioned_and_fingerprinted",
            "controller.read_only_and_bounded",
            "controller.tool_calls_replayable",
            "controller.checkpoint_resume",
            "controller.future_leakage_zero",
            "controller.safety_no_regression",
            "write_side.rejected_patch_pollution_zero",
            "projection.freshness_violations_zero",
            "scenario.future_isolation",
            "scenario.information_profiles_separate",
        }
        observed_critical_failures = {
            "bootstrap.source_traceability": evidence.source_traceability_passed is False,
            "bootstrap.genesis_author_approved": evidence.genesis_author_approved is False,
            "contracts.versioned_and_fingerprinted": evidence.contracts_versioned is False,
            "controller.read_only_and_bounded": evidence.controller_read_only_bounded is False,
            "controller.tool_calls_replayable": evidence.tool_calls_replayable is False,
            "controller.checkpoint_resume": evidence.controller_checkpoint_resume_passed is False,
            "controller.future_leakage_zero": evidence.future_leakage_count is not None
            and evidence.future_leakage_count > 0,
            "controller.safety_no_regression": evidence.agentic_safety_regression_count is not None
            and evidence.agentic_safety_regression_count > 0,
            "write_side.rejected_patch_pollution_zero": evidence.rejected_patch_pollution_count
            is not None
            and evidence.rejected_patch_pollution_count > 0,
            "projection.freshness_violations_zero": evidence.freshness_violation_count is not None
            and evidence.freshness_violation_count > 0,
            "scenario.future_isolation": evidence.future_isolation_failure_count is not None
            and evidence.future_isolation_failure_count > 0,
            "scenario.information_profiles_separate": evidence.information_profiles_separate
            is False,
        }
        failed_critical = tuple(
            name for name in critical_checks if observed_critical_failures[name]
        )
        missing_fields = {
            "bootstrap.user_data_imported": evidence.bootstrap_user_data_imported is None,
            "bootstrap.source_traceability": evidence.source_traceability_passed is None,
            "bootstrap.genesis_author_approved": evidence.genesis_author_approved is None,
            "contracts.versioned_and_fingerprinted": evidence.contracts_versioned is None,
            "controller.read_only_and_bounded": evidence.controller_read_only_bounded is None,
            "controller.tool_calls_replayable": evidence.tool_calls_replayable is None,
            "controller.checkpoint_resume": evidence.controller_checkpoint_resume_passed is None,
            "controller.future_leakage_zero": evidence.future_leakage_count is None,
            "controller.paired_results_present": evidence.paired_results_count is None,
            "controller.held_out_gain": evidence.agentic_gain_proven is None,
            "controller.safety_no_regression": evidence.agentic_safety_regression_count is None,
            "curator.real_replay_passed": evidence.curator_real_replay_passed is None,
            "curator.replay_length": evidence.real_replay_chapters is None,
            "write_side.rejected_patch_pollution_zero": evidence.rejected_patch_pollution_count
            is None,
            "projection.freshness_violations_zero": evidence.freshness_violation_count is None,
            "evaluation.ledger_complete": evidence.evaluation_ledger_complete is None,
            "scenario.required_checkpoints": not evidence.checkpoint_chapters,
            "scenario.receipt_chain_consistent": evidence.checkpoint_chain_consistent is None,
            "scenario.future_isolation": evidence.future_isolation_failure_count is None,
            "scenario.information_profiles_separate": evidence.information_profiles_separate
            is None,
            "report.failure_ledgers_complete": not evidence.failure_ledgers,
            "report.real_bundle_pinned": evidence.real_bundle_hash is None,
        }
        missing = tuple(name for name, absent in missing_fields.items() if absent)
        non_gain_checks = tuple(
            passed for name, passed in checks.items() if name != "controller.held_out_gain"
        )
        if failed_critical:
            verdict = Stage2GateVerdict.FAIL
            promotion = ControllerPromotionDecision.REJECT_ARCHITECTURE
            frozen = False
            blockers = tuple(sorted(failed_critical))
        elif missing:
            verdict = Stage2GateVerdict.INCOMPLETE
            promotion = ControllerPromotionDecision.DEFER
            frozen = False
            blockers = tuple(
                sorted(set(missing) | {name for name, passed in checks.items() if not passed})
            )
        elif all(checks.values()):
            verdict = Stage2GateVerdict.PASS
            promotion = ControllerPromotionDecision.ACCEPT_BOUNDED_DEFAULT
            frozen = True
            blockers = ()
        elif all(non_gain_checks) and evidence.agentic_gain_proven is False:
            verdict = Stage2GateVerdict.CONDITIONAL_PASS
            promotion = ControllerPromotionDecision.FREEZE_DETERMINISTIC_GATEWAY
            frozen = True
            blockers = ("controller.held_out_gain",)
        else:
            verdict = Stage2GateVerdict.FAIL
            promotion = ControllerPromotionDecision.REJECT_ARCHITECTURE
            frozen = False
            blockers = tuple(sorted(name for name, passed in checks.items() if not passed))
        return Stage2GateReport(
            report_id=StableId(f"stage2-gate.{evidence.evidence_id.root}"),
            evidence_id=evidence.evidence_id,
            verdict=verdict,
            checks=checks,
            blockers=blockers,
            controller_promotion=promotion,
            memory_gateway_frozen=frozen,
            configuration_fingerprint=evidence.configuration_fingerprint,
        )
