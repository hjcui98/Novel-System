"""Contract coverage for the deterministic U8-A recovery policy."""

from __future__ import annotations

import json
from pathlib import Path

from novel_agent.domain.runtime import (
    FailureClass,
    FailurePolicy,
    RecoveryCheckpoint,
    RetryOwner,
    TaskAttempt,
    TaskAttemptSettledPayload,
    TaskBlockedPayload,
    TaskClaimedPayload,
    TaskStatus,
    failure_policy,
    normalize_failure_class,
)
from novel_agent.domain.stage5_evaluation import Stage5RuntimeAuditReport

ROOT = Path(__file__).parents[2]


def test_every_failure_class_has_one_owner_budget_and_checkpoint_policy() -> None:
    policies = {failure: failure_policy(failure) for failure in FailureClass}

    assert set(policies) == set(FailureClass)
    assert all(policy.retry_owner in RetryOwner for policy in policies.values())
    assert all(isinstance(policy.retryable, bool) for policy in policies.values())
    assert all(isinstance(policy.consumes_task_budget, bool) for policy in policies.values())
    assert all(isinstance(policy.consumes_creative_budget, bool) for policy in policies.values())
    assert all(policy.resume_from in RecoveryCheckpoint for policy in policies.values())
    assert all(isinstance(policy.fallback_status, TaskStatus) for policy in policies.values())

    assert failure_policy(FailureClass.PROVIDER_TRANSIENT).resume_from is (
        RecoveryCheckpoint.LATEST_SETTLED
    )
    assert failure_policy(FailureClass.LEAF_SCHEMA_REJECTED).retry_owner is RetryOwner.LEAF
    assert failure_policy(FailureClass.RUNTIME_CAPABILITY_UNAVAILABLE).retry_owner is (
        RetryOwner.OPERATOR
    )
    assert failure_policy(FailureClass.EXTERNAL_RESOURCE_UNAVAILABLE).fallback_status is (
        TaskStatus.RECOVERY_PENDING
    )
    assert failure_policy(FailureClass.WRITER_LANE_BUSY).retry_owner is RetryOwner.RUNTIME
    assert failure_policy(FailureClass.WRITER_LANE_BUSY).retryable is True
    assert failure_policy(FailureClass.WRITER_LANE_BUSY).consumes_task_budget is False
    assert failure_policy(FailureClass.WRITER_LANE_BUSY).fallback_status is TaskStatus.WAITING_RETRY
    timeout = failure_policy(FailureClass.SCHEDULING_TIMEOUT)
    assert timeout.retryable is True
    assert timeout.consumes_task_budget is False
    assert timeout.consumes_creative_budget is False
    assert timeout.fallback_status is TaskStatus.WAITING_RETRY
    unsatisfiable = failure_policy(FailureClass.SCHEDULING_BUDGET_UNSATISFIABLE)
    assert unsatisfiable.retryable is False
    assert unsatisfiable.consumes_task_budget is False
    assert unsatisfiable.consumes_creative_budget is False
    assert unsatisfiable.fallback_status is TaskStatus.BLOCKED


def test_observed_failure_labels_route_to_their_narrow_owner() -> None:
    assert normalize_failure_class("StructuredGenerationExhausted") is (
        FailureClass.LEAF_SCHEMA_REJECTED
    )
    assert normalize_failure_class("UpdateWorkerBuildIdCompatibility") is (
        FailureClass.RUNTIME_CAPABILITY_UNAVAILABLE
    )
    assert normalize_failure_class("unseen-runtime-error") is FailureClass.UNKNOWN


def test_unknown_failure_is_operator_owned_non_retryable_and_fail_closed() -> None:
    policy = failure_policy("unseen-runtime-error")

    assert policy.retry_owner is RetryOwner.OPERATOR
    assert policy.retryable is False
    assert policy.consumes_task_budget is False
    assert policy.consumes_creative_budget is False
    assert policy.resume_from is RecoveryCheckpoint.NONE
    assert policy.fallback_status is TaskStatus.RECOVERY_PENDING


def test_u8a_failure_policy_and_failure_class_schemas_are_exported() -> None:
    models = (
        FailurePolicy,
        TaskAttempt,
        TaskClaimedPayload,
        TaskAttemptSettledPayload,
        TaskBlockedPayload,
        Stage5RuntimeAuditReport,
    )
    for model in models:
        path = ROOT / "schemas" / "stage5" / f"{model.__name__}.schema.json"
        assert json.loads(path.read_text(encoding="utf-8")) == model.model_json_schema()
