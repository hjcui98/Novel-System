"""One classification of a waiting task, read from the canonical attempt ledger.

The v23 driver decided whether to retry a task by matching uppercase words in
``block_cause``.  That field is only set for ``BLOCKED``, so a task waiting for a
retry reported ``block_cause=null``; the driver then matched nothing, fell through
to "a deterministic failure, no blind retry", and stopped on a failure that was
retryable.  The real classification was in the attempt row all along, as a
lowercase :class:`FailureClass` the driver never read.

This module answers the question the driver was trying to answer, from the data
that actually holds the answer: the last settled attempt's failure class, and the
effect ledger that says whether an unresolved provider send exists.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import StableId
from novel_agent.domain.runtime import (
    FailureClass,
    FailurePolicy,
    RetryOwner,
    TaskStatus,
    failure_policy,
)


class RecoveryAction(StrEnum):
    """What the driver is allowed to do next for one waiting task.

    Each name is one of the handlings the remediation guidance names, so a driver
    cannot invent a fourth by pattern-matching a log line.
    """

    # A confirmed transient failure with nothing in flight: retry under the
    # existing policy, keeping the attempt and logical-request relationship.
    RETRY_UNDER_POLICY = "retry_under_policy"
    # A send happened and its result is unknown, or an older request is still
    # REQUESTED: reconcile or recover first.  A new request id would buy a second
    # free chance at a provider call that may already have been served.
    RECONCILE_FIRST = "reconcile_first"
    # The provider already answered and the response is durable: replay and parse
    # it instead of calling the provider again.
    REPLAY_COMPLETED = "replay_completed"
    # Configuration, basis or permission drift: keep the concrete failure and stop
    # work that depends on it until the source is repaired or re-frozen.
    STOP_DEPENDENT_WORK = "stop_dependent_work"
    # A deterministic failure in schema, review, materialization or frozen draft
    # length: retrying the same input cannot help, so the owning module has to be
    # fixed.
    REPAIR_OWNING_MODULE = "repair_owning_module"
    # A budget slice ran out while work was still moving: the authorised escalation
    # ladder, with lifetime usage carried forward rather than reset.
    ESCALATE_BUDGET = "escalate_budget"
    # No progress, or the same review problem coming back: stop spending, keep the
    # problem and the candidate, and change strategy before continuing.
    CHANGE_STRATEGY = "change_strategy"
    # Nothing has been classified yet.  Reported as its own answer instead of being
    # presented as a deterministic failure or as safely retryable.
    UNDETERMINED = "undetermined"


# The failure classes whose handling is a plain retry once nothing is in flight.
_TRANSIENT_CLASSES = frozenset(
    {
        FailureClass.WORKER_STARTUP,
        FailureClass.WORKER_LEASE_EXPIRED,
        FailureClass.PROVIDER_TRANSIENT,
        FailureClass.SCHEDULING_TIMEOUT,
        FailureClass.WRITER_LANE_BUSY,
    }
)
# Drift in what the task was planned and permitted against.  Retrying reproduces
# the same refusal, so the source has to be repaired first.
_DRIFT_CLASSES = frozenset(
    {
        FailureClass.BASIS_CHANGED,
        FailureClass.PERMISSION_DENIED,
        FailureClass.RUNTIME_CAPABILITY_UNAVAILABLE,
        FailureClass.EXTERNAL_RESOURCE_UNAVAILABLE,
        FailureClass.COMMIT_CONFLICT,
        FailureClass.FRESHNESS_BLOCKED,
        FailureClass.FRESHNESS_WAITING,
    }
)
# Deterministic refusals owned by a module: another attempt on the same input
# cannot change the outcome.
_DETERMINISTIC_CLASSES = frozenset(
    {
        FailureClass.LEAF_SCHEMA_REJECTED,
        FailureClass.LEAF_REVIEW_REQUIRED,
        FailureClass.VALIDATION_REJECTED,
        FailureClass.CANON_EXTRACTION_GAP,
        FailureClass.PROJECTION_FAILED,
        FailureClass.SCHEDULING_BUDGET_UNSATISFIABLE,
        FailureClass.POISON_LOOP,
    }
)


class AttemptClassification(DomainModel):
    """One task's recovery position, with the evidence it was read from."""

    task_id: StableId
    task_status: TaskStatus
    action: RecoveryAction
    # The canonical classification, or ``None`` when no attempt has settled one.
    failure_class: FailureClass | None = None
    settled_attempt_id: StableId | None = None
    # The Attempt whose completed model responses may be replayed.  Historical
    # unresolved sends are reported separately and still block a blind retry.
    frontier_attempt_id: StableId | None = None
    retryable: bool = False
    retry_owner: RetryOwner | None = None
    consumes_task_budget: bool = False
    consumes_creative_budget: bool = False
    # Provider sends whose outcome is not settled.  Any of these makes a blind
    # retry unsafe regardless of how the failure itself is classified.
    unsettled_sends: tuple[str, ...] = ()
    # Requests the ledger still shows as outstanding.
    outstanding_request_ids: tuple[str, ...] = ()
    # Responses already durable for this task: these are replayed, not re-requested.
    completed_response_refs: tuple[str, ...] = ()
    # Responses already consumed by an earlier logical request.  They remain audit
    # evidence but must not outrank a later deterministic failure as replay input.
    consumed_response_ids: tuple[str, ...] = ()
    # Terminal ledger rows without a verifiable raw response.  They are not
    # completed evidence and must not silently turn into a fresh, billable retry.
    unavailable_response_ids: tuple[str, ...] = ()
    reason: str = Field(min_length=1)

    @property
    def safe_to_retry(self) -> bool:
        """Whether a retry may be issued without touching the provider's contract."""

        return (
            self.action is RecoveryAction.RETRY_UNDER_POLICY
            and self.retryable
            and not self.unsettled_sends
            and not self.outstanding_request_ids
            and not self.unavailable_response_ids
        )


def classify_attempt(
    *,
    task_id: StableId,
    task_status: TaskStatus,
    attempt: object | None,
    unsettled_sends: tuple[str, ...] = (),
    outstanding_request_ids: tuple[str, ...] = (),
    completed_response_refs: tuple[str, ...] = (),
    consumed_response_ids: tuple[str, ...] = (),
    unavailable_response_ids: tuple[str, ...] = (),
    frontier_attempt_id: StableId | None = None,
    block_cause: str | None = None,
) -> AttemptClassification:
    """Decide the next handling for one task from its canonical evidence.

    ``attempt`` is the last settled :class:`TaskAttempt`, and the ledger inputs are
    what the effect projection reports for it.  ``block_cause`` is accepted only so
    a conflict between it and the canonical classification can be reported: it is
    never used to decide the action.
    """

    failure = getattr(attempt, "failure_class", None)
    settled_attempt_id = getattr(attempt, "attempt_id", None)
    if failure is None:
        return AttemptClassification(
            task_id=task_id,
            task_status=task_status,
            action=RecoveryAction.UNDETERMINED,
            settled_attempt_id=settled_attempt_id,
            frontier_attempt_id=frontier_attempt_id,
            unsettled_sends=unsettled_sends,
            outstanding_request_ids=outstanding_request_ids,
            completed_response_refs=completed_response_refs,
            consumed_response_ids=consumed_response_ids,
            unavailable_response_ids=unavailable_response_ids,
            reason=_undetermined_reason(attempt, block_cause),
        )
    policy = failure_policy(failure)
    common = {
        "task_id": task_id,
        "task_status": task_status,
        "failure_class": failure,
        "settled_attempt_id": settled_attempt_id,
        "frontier_attempt_id": frontier_attempt_id,
        "retryable": policy.retryable,
        "retry_owner": policy.retry_owner,
        "consumes_task_budget": policy.consumes_task_budget,
        "consumes_creative_budget": policy.consumes_creative_budget,
        "unsettled_sends": unsettled_sends,
        "outstanding_request_ids": outstanding_request_ids,
        "completed_response_refs": completed_response_refs,
        "consumed_response_ids": consumed_response_ids,
        "unavailable_response_ids": unavailable_response_ids,
    }
    # Order matters. An unresolved send outranks the failure's own classification.
    # A completed response is replayable only when the settled failure is not already
    # a deterministic/drift/budget verdict: an earlier logical response must not
    # disguise the later phase that actually stopped the Attempt.
    if outstanding_request_ids or unsettled_sends:
        return AttemptClassification(
            **common,
            action=RecoveryAction.RECONCILE_FIRST,
            reason=(
                "the effect ledger has "
                f"{len(outstanding_request_ids)} outstanding request(s) and "
                f"{len(unsettled_sends)} unsettled send(s); reconcile them before "
                "issuing another provider call"
            ),
        )
    if unavailable_response_ids:
        return AttemptClassification(
            **common,
            action=RecoveryAction.RECONCILE_FIRST,
            reason=(
                "the model-call ledger has terminal response(s) without verifiable raw "
                f"evidence ({len(unavailable_response_ids)}); preserve unknown usage and "
                "reconcile the response before issuing another provider call"
            ),
        )
    if completed_response_refs and not (
        failure in _DRIFT_CLASSES
        or failure is FailureClass.BUDGET_EXHAUSTED
        or failure in _DETERMINISTIC_CLASSES
    ):
        return AttemptClassification(
            **common,
            action=RecoveryAction.REPLAY_COMPLETED,
            reason=(
                "the provider already answered and the response is durable; replay and "
                "parse it instead of calling again"
            ),
        )
    if failure in _DRIFT_CLASSES:
        return AttemptClassification(
            **common,
            action=RecoveryAction.STOP_DEPENDENT_WORK,
            reason=(
                f"{failure.value} is configuration, basis or permission drift; retrying "
                "reproduces the same refusal"
            ),
        )
    if failure is FailureClass.BUDGET_EXHAUSTED:
        return AttemptClassification(
            **common,
            action=RecoveryAction.ESCALATE_BUDGET,
            reason="the budget slice is exhausted; escalate under the existing ladder",
        )
    if policy.retryable and failure in _TRANSIENT_CLASSES:
        return AttemptClassification(
            **common,
            action=RecoveryAction.RETRY_UNDER_POLICY,
            reason=f"{failure.value} is a confirmed transient failure with nothing in flight",
        )
    if failure in _DETERMINISTIC_CLASSES:
        return AttemptClassification(
            **common,
            action=RecoveryAction.REPAIR_OWNING_MODULE,
            reason=(
                f"{failure.value} is deterministic for this input; another attempt cannot "
                "change the outcome"
            ),
        )
    if failure is FailureClass.EFFECT_UNCERTAIN:
        return AttemptClassification(
            **common,
            action=RecoveryAction.RECONCILE_FIRST,
            reason="the previous send's outcome is unknown; reconcile before retrying",
        )
    if failure is FailureClass.CANCELLED:
        return AttemptClassification(
            **common,
            action=RecoveryAction.STOP_DEPENDENT_WORK,
            reason="the task was cancelled; it is not retried automatically",
        )
    if failure is FailureClass.UNKNOWN:
        return AttemptClassification(
            **common,
            action=RecoveryAction.UNDETERMINED,
            reason=(
                "the attempt settled as unknown; the classification has not been "
                "determined and must not be presented as a deterministic failure"
            ),
        )
    return AttemptClassification(
        **common,
        action=RecoveryAction.REPAIR_OWNING_MODULE,
        reason=f"{failure.value} has no automatic retry path in the current policy",
    )


def _undetermined_reason(attempt: object | None, block_cause: str | None) -> str:
    if attempt is None:
        detail = "no attempt has settled for this task"
    else:
        detail = "the last settled attempt recorded no failure class"
    if block_cause:
        detail = (
            f"{detail}; block_cause is present ({block_cause[:80]!r}) but is not the "
            "canonical classification and was not used"
        )
    return detail


def policy_for(failure: FailureClass) -> FailurePolicy:
    """Expose the shared policy so a caller never restates it."""

    return failure_policy(failure)
