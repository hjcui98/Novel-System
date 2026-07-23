"""Versioned, table-driven semantic repair policy for Stage 2W."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory_write import (
    FindingRetryability,
    RepairAction,
    RepairContext,
    RepairDirective,
    RepairScope,
    RepairStrategy,
    ValidationFindingV2,
)
from novel_agent.domain.stage2 import GuardianOutcome, WriteGateOutcome
from novel_agent.services.content_addressing import canonical_json_bytes


@dataclass(frozen=True, slots=True)
class FindingRule:
    code: str
    retryability: FindingRetryability
    strategies: tuple[RepairStrategy, ...]
    default_action: RepairAction
    category: str


FINDING_REGISTRY: tuple[FindingRule, ...] = (
    FindingRule(
        "STATE_IDENTITY_MUTATION",
        FindingRetryability.REPAIRABLE,
        (RepairStrategy.SUCCESSOR_STATE, RepairStrategy.CORRECT_TARGET),
        RepairAction.DETERMINISTIC_REPAIR,
        "identity",
    ),
    FindingRule(
        "CREATE_TARGET_EXISTS",
        FindingRetryability.REPAIRABLE,
        (RepairStrategy.CREATE_TO_REPLACE, RepairStrategy.QUARANTINE),
        RepairAction.DETERMINISTIC_REPAIR,
        "operation",
    ),
    FindingRule(
        "REPLACE_TARGET_MISSING",
        FindingRetryability.REPAIRABLE,
        (RepairStrategy.REPLACE_TO_CREATE, RepairStrategy.CURATOR_REPAIR),
        RepairAction.DETERMINISTIC_REPAIR,
        "operation",
    ),
    FindingRule(
        "OPERATION_TARGET_MISMATCH",
        FindingRetryability.REPAIRABLE,
        (RepairStrategy.CORRECT_TARGET,),
        RepairAction.DETERMINISTIC_REPAIR,
        "identity",
    ),
    FindingRule(
        "RECORD_EVIDENCE_MISMATCH",
        FindingRetryability.REPAIRABLE,
        (RepairStrategy.REBIND_EVIDENCE, RepairStrategy.CURATOR_REPAIR),
        RepairAction.DETERMINISTIC_REPAIR,
        "evidence",
    ),
    FindingRule(
        "INVALID_EVIDENCE_REF",
        FindingRetryability.CONDITIONAL,
        (RepairStrategy.REFRESH_CONTEXT, RepairStrategy.CURATOR_REPAIR),
        RepairAction.RETRY_AFTER_SOURCE_CONTEXT_REFRESH,
        "evidence",
    ),
    FindingRule(
        "ILLEGAL_STATE_TRANSITION",
        FindingRetryability.CONDITIONAL,
        (RepairStrategy.SUCCESSOR_STATE, RepairStrategy.CURATOR_REPAIR),
        RepairAction.CURATOR_REPAIR,
        "transition",
    ),
    FindingRule(
        "UNLISTED_STATE_TRANSITION",
        FindingRetryability.REVIEW,
        (RepairStrategy.GUARDIAN_REVIEW, RepairStrategy.HUMAN),
        RepairAction.GUARDIAN_REVIEW,
        "transition",
    ),
    FindingRule(
        "TRUTH_PROMOTION",
        FindingRetryability.REVIEW,
        (RepairStrategy.GUARDIAN_REVIEW, RepairStrategy.HUMAN),
        RepairAction.GUARDIAN_REVIEW,
        "truth",
    ),
    FindingRule(
        "FUTURE_EVIDENCE",
        FindingRetryability.NON_REPAIRABLE,
        (),
        RepairAction.STOP_FATAL,
        "information_boundary",
    ),
    FindingRule(
        "BASE_COMMIT_MISMATCH",
        FindingRetryability.NON_REPAIRABLE,
        (),
        RepairAction.REPLAN,
        "basis",
    ),
    FindingRule(
        "INFORMATION_DERIVATION_BOUNDARY_VIOLATION",
        FindingRetryability.NON_REPAIRABLE,
        (),
        RepairAction.STOP_FATAL,
        "information_boundary",
    ),
)

_RULES = {rule.code: rule for rule in FINDING_REGISTRY}


class BoundedMemoryRepairPolicy:
    """Select exactly one next edge from trusted validation/gate evidence."""

    policy_id = "bounded-v1"

    def __init__(
        self,
        *,
        same_content_hash_limit: int = 2,
        same_finding_signature_limit: int = 2,
    ) -> None:
        if same_content_hash_limit < 1 or same_finding_signature_limit < 1:
            raise ValueError("poison-loop limits must be positive")
        self._content_limit = same_content_hash_limit
        self._finding_limit = same_finding_signature_limit

    def decide(self, context: RepairContext) -> RepairDirective:
        if context.current_canonical_commit != context.candidate.base_commit:
            return self._directive(
                RepairAction.REPLAN,
                reason_codes=("BASE_COMMIT_MISMATCH",),
                checkpoint=True,
            )
        findings = () if context.validation is None else context.validation.findings
        finding_signature = _finding_signature(findings)
        if (
            finding_signature
            and _repeated_finding_signature(context.prior_actions, finding_signature)
            >= self._finding_limit
        ):
            return self._directive(
                RepairAction.QUARANTINE_OPERATION,
                reason_codes=("POISON_LOOP_FINDING",),
                operation_ids=_finding_operation_ids(findings),
                allowed_scope=_all_scope(context),
                checkpoint=True,
            )
        if (
            _repeated(context.repeated_content_hashes, context.candidate.content_hash)
            >= self._content_limit
        ):
            return self._directive(
                RepairAction.QUARANTINE_OPERATION,
                reason_codes=("POISON_LOOP_CONTENT",),
                operation_ids=_finding_operation_ids(findings),
                allowed_scope=_all_scope(context),
                checkpoint=True,
            )

        if context.guardian is not None:
            if context.guardian.outcome is GuardianOutcome.REVISE:
                return self._curator_or_budget(context, "GUARDIAN_REVISE")
            if context.guardian.outcome is GuardianOutcome.REJECT:
                return self._directive(
                    RepairAction.QUARANTINE_OPERATION,
                    reason_codes=("GUARDIAN_REJECT",),
                    operation_ids=_finding_operation_ids(findings),
                    allowed_scope=_all_scope(context),
                    checkpoint=True,
                )
            if context.guardian.outcome is GuardianOutcome.HUMAN_REVIEW:
                return self._directive(
                    RepairAction.HUMAN, reason_codes=("GUARDIAN_HUMAN_REVIEW",), checkpoint=True
                )

        if context.gate is not None:
            if context.gate.outcome is WriteGateOutcome.REQUIRE_HUMAN:
                return self._directive(
                    RepairAction.HUMAN, reason_codes=("WRITE_GATE_REQUIRE_HUMAN",), checkpoint=True
                )
            if context.gate.outcome is WriteGateOutcome.REQUIRE_GUARDIAN:
                return self._guardian_or_budget(context)
            if context.gate.outcome in {
                WriteGateOutcome.BLOCK_GUARDIAN,
                WriteGateOutcome.BLOCK_HUMAN,
            }:
                return self._directive(
                    RepairAction.QUARANTINE_OPERATION,
                    reason_codes=("WRITE_GATE_BLOCKED",),
                    operation_ids=_finding_operation_ids(findings),
                    allowed_scope=_all_scope(context),
                    checkpoint=True,
                )

        if not findings:
            return (
                self._guardian_or_budget(context)
                if context.risk is not None and context.risk.requires_guardian
                else self._fatal("NO_REPAIR_DECISION")
            )
        if any(finding.code == "FUTURE_EVIDENCE" for finding in findings):
            return self._fatal("FUTURE_EVIDENCE")
        if any(finding.code in {"BASE_COMMIT_MISMATCH", "BASE_MISMATCH"} for finding in findings):
            return self._directive(
                RepairAction.REPLAN, reason_codes=("BASE_COMMIT_MISMATCH",), checkpoint=True
            )
        if any(finding.requires_human for finding in findings):
            return self._directive(
                RepairAction.HUMAN, reason_codes=("FINDING_REQUIRES_HUMAN",), checkpoint=True
            )
        if any(finding.requires_context_refresh for finding in findings):
            return self._refresh_or_curator(context)

        first = findings[0]
        rule = _RULES.get(first.code)
        action = rule.default_action if rule is not None else _fallback_action(first)
        if action is RepairAction.STOP_FATAL:
            return self._fatal(first.code)
        if action is RepairAction.REPLAN:
            return self._directive(
                RepairAction.REPLAN,
                finding_ids=_finding_ids(findings),
                reason_codes=(first.code,),
                checkpoint=True,
            )
        if action is RepairAction.RETRY_AFTER_SOURCE_CONTEXT_REFRESH:
            return self._refresh_or_curator(context, finding_ids=_finding_ids(findings))
        if action is RepairAction.GUARDIAN_REVIEW:
            return self._guardian_or_budget(context, finding_ids=_finding_ids(findings))
        if action is RepairAction.CURATOR_REPAIR:
            return self._curator_or_budget(context, first.code, finding_ids=_finding_ids(findings))
        if action is RepairAction.DETERMINISTIC_REPAIR:
            deterministic_seen = any(
                receipt.action is RepairAction.DETERMINISTIC_REPAIR
                and receipt.candidate_id == context.candidate.candidate_id
                for receipt in context.prior_actions
            )
            if deterministic_seen:
                return self._curator_or_budget(
                    context, first.code, finding_ids=_finding_ids(findings)
                )
            if context.budget_remaining.normalization_passes <= 0:
                return self._curator_or_budget(
                    context, first.code, finding_ids=_finding_ids(findings)
                )
            return self._directive(
                RepairAction.DETERMINISTIC_REPAIR,
                finding_ids=_finding_ids(findings),
                operation_ids=first.operation_ids,
                allowed_scope=first.allowed_repair_scope,
                reason_codes=(first.code,),
            )
        return self._curator_or_budget(context, first.code, finding_ids=_finding_ids(findings))

    def _curator_or_budget(
        self,
        context: RepairContext,
        reason: str = "CURATOR_REPAIR",
        *,
        finding_ids: tuple[StableId, ...] = (),
    ) -> RepairDirective:
        if (
            context.budget_remaining.curator_repairs <= 0
            or context.budget_remaining.total_model_calls <= 0
        ):
            return self._budget(reason)
        finding = _first_finding(context)
        return self._directive(
            RepairAction.CURATOR_REPAIR,
            finding_ids=finding_ids or (() if finding is None else (finding.finding_id,)),
            operation_ids=() if finding is None else finding.operation_ids,
            allowed_scope=RepairScope() if finding is None else finding.allowed_repair_scope,
            reason_codes=(reason,),
        )

    def _guardian_or_budget(
        self,
        context: RepairContext,
        *,
        finding_ids: tuple[StableId, ...] = (),
    ) -> RepairDirective:
        if (
            context.budget_remaining.guardian_reviews <= 0
            or context.budget_remaining.total_model_calls <= 0
        ):
            return self._budget("GUARDIAN_REVIEW")
        return self._directive(
            RepairAction.GUARDIAN_REVIEW,
            finding_ids=finding_ids,
            reason_codes=("GUARDIAN_REVIEW",),
        )

    def _refresh_or_curator(
        self,
        context: RepairContext,
        *,
        finding_ids: tuple[StableId, ...] = (),
    ) -> RepairDirective:
        if context.budget_remaining.context_refreshes > 0:
            return self._directive(
                RepairAction.RETRY_AFTER_SOURCE_CONTEXT_REFRESH,
                finding_ids=finding_ids,
                reason_codes=("SOURCE_CONTEXT_REFRESH",),
            )
        return self._curator_or_budget(
            context, "SOURCE_CONTEXT_REFRESH_EXHAUSTED", finding_ids=finding_ids
        )

    def _directive(
        self,
        action: RepairAction,
        *,
        finding_ids: tuple[StableId, ...] = (),
        operation_ids: tuple[StableId, ...] = (),
        allowed_scope: RepairScope | None = None,
        reason_codes: tuple[str, ...] = (),
        checkpoint: bool = False,
    ) -> RepairDirective:
        scope = allowed_scope or RepairScope(operation_ids=operation_ids)
        if operation_ids and not set(operation_ids).issubset(scope.operation_ids):
            operation_ids = tuple(item for item in operation_ids if item in scope.operation_ids)
        seed = (
            ".".join((action.value, *reason_codes, *(item.root for item in finding_ids)))
            or action.value
        )
        digest = hashlib.sha256(canonical_json_bytes(seed)).hexdigest()[:24]
        directive_id = StableId(f"repair.{digest}")
        return RepairDirective(
            directive_id=directive_id,
            action=action,
            finding_ids=finding_ids,
            operation_ids=operation_ids,
            allowed_scope=scope,
            reason_codes=reason_codes,
            checkpoint_required=checkpoint,
        )

    def _budget(self, reason: str) -> RepairDirective:
        return self._directive(
            RepairAction.STOP_BUDGET_EXHAUSTED,
            reason_codes=("SEMANTIC_REPAIR_BUDGET_EXHAUSTED", reason),
            checkpoint=True,
        )

    def _fatal(self, reason: str) -> RepairDirective:
        return self._directive(
            RepairAction.STOP_FATAL,
            reason_codes=(reason,),
            checkpoint=False,
        )


def _first_finding(context: RepairContext) -> ValidationFindingV2 | None:
    return (
        None
        if context.validation is None or not context.validation.findings
        else context.validation.findings[0]
    )


def _finding_ids(findings: tuple[ValidationFindingV2, ...]) -> tuple[StableId, ...]:
    return tuple(finding.finding_id for finding in findings)


def _finding_operation_ids(findings: tuple[ValidationFindingV2, ...]) -> tuple[StableId, ...]:
    return tuple(
        dict.fromkeys(
            operation_id for finding in findings for operation_id in finding.operation_ids
        )
    )


def _fallback_action(finding: ValidationFindingV2) -> RepairAction:
    if finding.retryability is FindingRetryability.NON_REPAIRABLE:
        return RepairAction.STOP_FATAL
    if finding.retryability is FindingRetryability.REVIEW:
        return RepairAction.GUARDIAN_REVIEW
    return RepairAction.CURATOR_REPAIR


def _repeated(values: tuple[Any, ...], value: object) -> int:
    return sum(item == value for item in values)


def _finding_signature(findings: tuple[ValidationFindingV2, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                finding.code
                for finding in findings
                if finding.severity.value in {"error", "critical"}
            }
        )
    )


def _repeated_finding_signature(prior_actions: tuple[Any, ...], signature: tuple[str, ...]) -> int:
    return sum(
        1 for receipt in prior_actions if all(code in receipt.reason_codes for code in signature)
    )


def _all_scope(context: RepairContext) -> RepairScope:
    findings = () if context.validation is None else context.validation.findings
    return RepairScope(operation_ids=_finding_operation_ids(findings))


__all__ = ["FINDING_REGISTRY", "BoundedMemoryRepairPolicy", "FindingRule"]
