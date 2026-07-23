from __future__ import annotations

from typing import Any, cast

import pytest

from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.stage2 import (
    AuthorApprovalStatus,
    BenchmarkCheckpointDeclaration,
    BootstrapAuditReport,
    CanonicalWriteOutcome,
    ContextResolutionResult,
    ControllerArm,
    ControllerPolicyAction,
    ControllerPolicyDecision,
    ControllerStopReason,
    IndependentRebuildComparison,
    IndependentRebuildReport,
    MemoryGatewayMode,
    MemoryGatewayPolicy,
    MemoryGatewayResult,
    PairedContextArmResult,
    PatchApprovalDecision,
    ReplayWriteResult,
    ReplayWriteStatus,
    ResolutionStatus,
    ScenarioChapterTransition,
    ScenarioCheckpointArtifacts,
    SourceClass,
    Stage2ConfigurationManifest,
    SufficiencyReport,
    ToolPolicy,
    ToolResult,
    ToolResultStatus,
    WriteGateDecision,
    WriteGateOutcome,
)
from tests.contract.test_stage2_contract import agent_spec
from tests.factories import make_artifact
from tests.unit.test_stage2_scenario import (
    COMMIT_1,
    HASH_A,
    scenario,
    source,
)

SNAPSHOT_1 = StableId("snapshot.1")


def _expect_value_error(model: Any, method: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        getattr(model, method)()


def _construct(model: Any, **values: Any) -> Any:
    return model.model_construct(**values)


def test_tool_policy_rejects_duplicate_denied_tools() -> None:
    policy = _construct(
        ToolPolicy,
        allowed_tools=(),
        denied_tools=("write", "write"),
    )
    _expect_value_error(policy, "validate_tool_sets", "denied tool names must be unique")


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("agent", "AgentSpec ids"),
        ("prompt", "prompt contracts"),
        ("skill", "skill contracts"),
        ("tool", "ToolPolicies"),
        ("schema", "schema artifacts"),
        ("missing_prompt", "unlisted prompt"),
        ("missing_skill", "unlisted skill"),
        ("missing_tool", "unlisted ToolPolicy"),
    ),
)
def test_configuration_manifest_rejects_invalid_inventory(
    mutation: str,
    message: str,
) -> None:
    spec = agent_spec()
    system = spec.system_prompt
    task = spec.task_prompt.model_copy(update={"contract_id": StableId("prompt.task")})
    spec = spec.model_copy(update={"task_prompt": task})
    skill = spec.skills[0]
    policy = spec.tool_policy
    agents: tuple[Any, ...] = (spec,)
    prompts: tuple[Any, ...] = (system, task)
    skills: tuple[Any, ...] = (skill,)
    policies: tuple[Any, ...] = (policy,)
    schemas: tuple[Any, ...] = (make_artifact("1"),)
    if mutation == "agent":
        agents = (spec, spec)
    elif mutation == "prompt":
        prompts = (system, task, system)
    elif mutation == "skill":
        skills = (skill, skill)
    elif mutation == "tool":
        policies = (policy, policy)
    elif mutation == "schema":
        schemas = (schemas[0], schemas[0])
    elif mutation == "missing_prompt":
        prompts = (system,)
    elif mutation == "missing_skill":
        skills = ()
    elif mutation == "missing_tool":
        policies = ()
    manifest = _construct(
        Stage2ConfigurationManifest,
        agent_specs=agents,
        prompt_contracts=prompts,
        skill_contracts=skills,
        tool_policies=policies,
        schema_artifacts=schemas,
    )
    _expect_value_error(manifest, "validate_inventory", message)


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"complete": True, "blockers": ("blocked",)}, "cannot retain blockers"),
        (
            {
                "complete": True,
                "blockers": (),
                "approval_status": AuthorApprovalStatus.REJECTED,
            },
            "requires approval",
        ),
        (
            {"genesis_receipt_id": StableId("receipt.only"), "genesis_commit_id": None},
            "must appear together",
        ),
    ),
)
def test_bootstrap_audit_rejects_contradictory_completion(
    updates: dict[str, Any],
    message: str,
) -> None:
    report = _construct(
        BootstrapAuditReport,
        complete=False,
        blockers=(),
        approval_status=AuthorApprovalStatus.APPROVED,
        genesis_receipt_id=StableId("receipt.genesis"),
        genesis_commit_id=COMMIT_1,
        freshness=cast(Any, object()),
    ).model_copy(update=updates)
    _expect_value_error(report, "validate_completion", message)


def test_sufficiency_report_rejects_false_sufficiency_and_negative_gain() -> None:
    report = _construct(
        SufficiencyReport,
        stop_reason=ControllerStopReason.SUFFICIENT,
        mandatory_gaps_closed=False,
        evidence_strength_satisfied=True,
        unresolved_unknowns=(),
        freshness_warnings=(),
        new_information_gain_by_round=(),
    )
    _expect_value_error(report, "validate_stopping_claim", "sufficient report")
    negative = report.model_copy(
        update={
            "stop_reason": ControllerStopReason.BUDGET_EXHAUSTED,
            "new_information_gain_by_round": (-1,),
        }
    )
    _expect_value_error(negative, "validate_stopping_claim", "non-negative")


def test_controller_policy_normalizes_non_mapping_and_rejects_invalid_shapes() -> None:
    marker = object()
    normalize = cast(Any, ControllerPolicyDecision.normalize_action_shape)
    assert normalize(marker) is marker
    assert (
        normalize({"action": ControllerPolicyAction.CALL_TOOL.value, "stop_reason": "sufficient"})[
            "stop_reason"
        ]
        is None
    )
    unknown = {"action": "future-action", "model_call_id": "untrusted"}
    assert normalize(unknown) == {
        "action": "future-action",
        "model_call_id": None,
    }

    call = _construct(
        ControllerPolicyDecision,
        action=ControllerPolicyAction.CALL_TOOL,
        need_id=None,
        tool_name=None,
        stop_reason=None,
    )
    _expect_value_error(call, "validate_action", "requires need/tool")
    stop = _construct(
        ControllerPolicyDecision,
        action=ControllerPolicyAction.STOP,
        need_id=StableId("need.unexpected"),
        tool_name=None,
        stop_reason=ControllerStopReason.SUFFICIENT,
    )
    _expect_value_error(stop, "validate_action", "requires only a stop reason")


def test_ready_context_resolution_requires_sufficiency_report() -> None:
    result = _construct(
        ContextResolutionResult,
        stop_reason=ControllerStopReason.BUDGET_EXHAUSTED,
        unresolved_gaps=(),
        status=ResolutionStatus.READY,
        context_assembly_spec=cast(Any, object()),
        sufficiency_report=None,
    )
    _expect_value_error(result, "validate_sufficiency", "requires a sufficiency report")


def test_memory_gateway_policy_and_result_reject_missing_or_mismatched_authority() -> None:
    policy = _construct(
        MemoryGatewayPolicy,
        mode=MemoryGatewayMode.BOUNDED_R2,
        promotion_evidence=None,
    )
    _expect_value_error(policy, "validate_promotion", "requires promotion evidence")

    selected = _construct(
        PairedContextArmResult,
        arm=ControllerArm.DETERMINISTIC,
        context=cast(Any, "context"),
        comparison_basis_fingerprint=HASH_A,
    )
    result = _construct(
        MemoryGatewayResult,
        selected_arm=ControllerArm.BOUNDED_R2,
        selected_result=selected,
        context="context",
        comparison=None,
        fallback_used=False,
        fallback_reason=None,
        promotion_evidence=None,
        configuration_fingerprint=HASH_A,
    )
    _expect_value_error(result, "validate_gateway_selection", "arm differs")
    bounded = selected.model_copy(update={"arm": ControllerArm.BOUNDED_R2})
    missing_evidence = result.model_copy(update={"selected_result": bounded})
    _expect_value_error(
        missing_evidence,
        "validate_gateway_selection",
        "requires promotion evidence",
    )
    comparison = cast(
        Any,
        type(
            "Comparison",
            (),
            {"deterministic": object(), "agentic": object(), "comparable": True},
        )(),
    )
    mismatched = missing_evidence.model_copy(
        update={"comparison": comparison, "promotion_evidence": make_artifact("2")}
    )
    _expect_value_error(mismatched, "validate_gateway_selection", "paired execution")


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        (
            {
                "status": ToolResultStatus.SUCCEEDED,
                "channel_failure_code": "backend_unavailable",
                "retrieval_channel": "semantic",
            },
            "successful tool result cannot carry a channel failure",
        ),
        (
            {
                "status": ToolResultStatus.FAILED,
                "failure_code": "TOOL_TIMEOUT",
                "channel_failure_code": "backend_unavailable",
                "retrieval_channel": None,
            },
            "channel failure requires a retrieval channel",
        ),
    ),
)
def test_tool_result_rejects_channel_failure_contradictions(
    updates: dict[str, Any],
    message: str,
) -> None:
    result = _construct(
        ToolResult,
        status=ToolResultStatus.SUCCEEDED,
        failure_code=None,
        channel_failure_code=None,
        retrieval_channel=None,
        partial=False,
    ).model_copy(update=updates)
    _expect_value_error(result, "validate_status", message)


def test_patch_approval_decision_cannot_remain_pending() -> None:
    decision = _construct(
        PatchApprovalDecision,
        status=AuthorApprovalStatus.PENDING,
    )
    _expect_value_error(decision, "validate_terminal", "cannot remain pending")


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("checkpoints", "match profile checkpoints"),
        ("case_ids", "unique case ids"),
        ("evaluator_ids", "only one checkpoint"),
        ("non_evaluator", "evaluator-only sources"),
        ("visibility", "visibility must match"),
    ),
)
def test_benchmark_scenario_rejects_invalid_checkpoint_bindings(
    mutation: str,
    message: str,
) -> None:
    base = scenario()
    private_id = StableId("source.future.private")
    declarations: tuple[Any, ...] = (
        BenchmarkCheckpointDeclaration(
            case_id=StableId("case.1"),
            checkpoint_chapter=1,
            evaluator_source_ids=(private_id,),
        ),
        BenchmarkCheckpointDeclaration(
            case_id=StableId("case.2"),
            checkpoint_chapter=2,
            evaluator_source_ids=(StableId("source.future.second"),),
        ),
    )
    second = source(
        "source.future.second",
        SourceClass.FUTURE_TEXT_PRIVATE,
        chapter=2,
        evaluator_only=True,
    )
    sources = (*base.sources, second)
    classifications = (
        *base.classifications,
        base.classifications[-1].model_copy(update={"source_id": second.source_id}),
    )
    if mutation == "checkpoints":
        declarations = declarations[:1]
    elif mutation == "case_ids":
        declarations = (
            declarations[0],
            declarations[1].model_copy(update={"case_id": declarations[0].case_id}),
        )
    elif mutation == "evaluator_ids":
        declarations = (
            declarations[0],
            declarations[1].model_copy(update={"evaluator_source_ids": (private_id,)}),
        )
    elif mutation == "non_evaluator":
        declarations = (
            declarations[0].model_copy(
                update={"evaluator_source_ids": (StableId("source.brief"),)}
            ),
            declarations[1],
        )
    invalid = base.model_copy(
        update={
            "sources": sources,
            "classifications": classifications,
            "checkpoint_cases": declarations,
        }
    )
    _expect_value_error(invalid, "validate_source_bindings", message)


def _freshness(
    *,
    commit: CommitId = COMMIT_1,
    snapshot: StableId = SNAPSHOT_1,
) -> Any:
    return cast(
        Any,
        type(
            "Freshness",
            (),
            {
                "canonical_commit": commit,
                "r1_basis_commit": commit,
                "required_snapshot_id": snapshot,
            },
        )(),
    )


def test_scenario_transition_rejects_inconsistent_bases() -> None:
    transition = _construct(
        ScenarioChapterTransition,
        parent_commit=COMMIT_1,
        resulting_commit=COMMIT_1,
        curator_receipt=cast(
            Any,
            type("Receipt", (), {"base_commit": CommitId("sha256:" + "2" * 64)})(),
        ),
        freshness=_freshness(),
        projection_snapshot_id=StableId("snapshot.1"),
        checkpoint_artifacts=None,
    )
    _expect_value_error(transition, "validate_basis", "Curator basis")
    stale = transition.model_copy(
        update={
            "curator_receipt": cast(Any, type("Receipt", (), {"base_commit": COMMIT_1})()),
            "freshness": _freshness(commit=CommitId("sha256:" + "2" * 64)),
        }
    )
    _expect_value_error(stale, "validate_basis", "freshness basis")
    wrong_snapshot = transition.model_copy(
        update={
            "curator_receipt": cast(Any, type("Receipt", (), {"base_commit": COMMIT_1})()),
            "checkpoint_artifacts": _construct(
                ScenarioCheckpointArtifacts, derived_snapshot_id=StableId("snapshot.other")
            ),
        }
    )
    _expect_value_error(wrong_snapshot, "validate_basis", "another projection snapshot")


def test_canonical_write_rejects_stale_freshness() -> None:
    outcome = _construct(
        CanonicalWriteOutcome,
        resulting_commit=COMMIT_1,
        freshness=_freshness(commit=CommitId("sha256:" + "2" * 64)),
        projection_snapshot_id=StableId("snapshot.1"),
    )
    _expect_value_error(outcome, "validate_write_basis", "freshness basis")


@pytest.mark.parametrize(
    ("status", "transition", "outcome", "message"),
    (
        (
            ReplayWriteStatus.BLOCKED,
            object(),
            WriteGateOutcome.BLOCK_VALIDATION,
            "exactly one",
        ),
        (
            ReplayWriteStatus.COMMITTED,
            object(),
            WriteGateOutcome.BLOCK_VALIDATION,
            "requires an allowing",
        ),
        (
            ReplayWriteStatus.SUSPENDED,
            None,
            WriteGateOutcome.BLOCK_VALIDATION,
            "resumable gate",
        ),
    ),
)
def test_replay_write_result_rejects_status_gate_contradictions(
    status: ReplayWriteStatus,
    transition: object | None,
    outcome: WriteGateOutcome,
    message: str,
) -> None:
    result = _construct(
        ReplayWriteResult,
        status=status,
        transition=cast(Any, transition),
        write_gate=_construct(WriteGateDecision, outcome=outcome),
    )
    _expect_value_error(result, "validate_status", message)


def test_independent_rebuild_rejects_inconsistent_details_and_summary() -> None:
    contradictory = _construct(
        IndependentRebuildComparison,
        consistent=True,
        mismatched_chapters=(1,),
    )
    _expect_value_error(contradictory, "validate_consistency", "contradicts mismatches")

    first = _construct(
        IndependentRebuildComparison,
        case_id=StableId("case.same"),
        consistent=True,
    )
    duplicate = _construct(
        IndependentRebuildReport,
        comparisons=(first, first),
        all_consistent=True,
    )
    _expect_value_error(duplicate, "validate_summary", "case ids must be unique")
    wrong_summary = duplicate.model_copy(
        update={
            "comparisons": (
                first,
                first.model_copy(update={"case_id": StableId("case.other"), "consistent": False}),
            )
        }
    )
    _expect_value_error(wrong_summary, "validate_summary", "summary contradicts")
