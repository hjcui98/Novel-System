"""Single fail-closed Writer readiness decision shared by Stage 3 and loops."""

from __future__ import annotations

from enum import StrEnum

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import (
    PlanRootDocument,
    chapter_goal_history_retrieval_decision,
)
from novel_agent.domain.generation import WritingTaskContract
from novel_agent.domain.memory import (
    WorldRootDocument,
    obligation_in_scope_for_chapter,
)
from novel_agent.domain.retrieval_decision import (
    FIRST_CHAPTER_WAIVER_REF,
    HistoryRetrievalRequirement,
)
from novel_agent.domain.writer_context import (
    ContextAssemblyStatus,
    WriterContextPackageV2,
)


class WriterReadinessReasonCode(StrEnum):
    PLAN_TARGET_AMBIGUOUS = "PLAN_TARGET_AMBIGUOUS"
    PLAN_BLOCKING_UNRESOLVED = "PLAN_BLOCKING_UNRESOLVED"
    HISTORY_DECISION_MISSING = "HISTORY_DECISION_MISSING"
    HISTORY_NEED_EMPTY = "HISTORY_NEED_EMPTY"
    MEMORY_GATEWAY_NOT_EXECUTED = "MEMORY_GATEWAY_NOT_EXECUTED"
    MANDATORY_FACET_INCOMPLETE = "MANDATORY_FACET_INCOMPLETE"
    OBLIGATION_BINDING_INCOMPLETE = "OBLIGATION_BINDING_INCOMPLETE"
    PLANNING_LINEAGE_INCOMPLETE = "PLANNING_LINEAGE_INCOMPLETE"
    PROJECTION_NOT_EXACT = "PROJECTION_NOT_EXACT"
    WRITER_PACKAGE_NOT_READY = "WRITER_PACKAGE_NOT_READY"
    HISTORY_WAIVER_NOT_APPLICABLE = "HISTORY_WAIVER_NOT_APPLICABLE"


class WriterReadinessDecision(DomainModel):
    ready: bool
    reason_codes: tuple[WriterReadinessReasonCode, ...] = ()
    details: tuple[str, ...] = ()

    @classmethod
    def pass_decision(cls) -> WriterReadinessDecision:
        return cls(ready=True)

    @classmethod
    def blocked(
        cls,
        reason_codes: tuple[WriterReadinessReasonCode, ...],
        details: tuple[str, ...] = (),
    ) -> WriterReadinessDecision:
        unique = tuple(dict.fromkeys(reason_codes))
        if not unique:
            raise ValueError("a blocked readiness decision requires a reason code")
        return cls(ready=False, reason_codes=unique, details=details)

    @property
    def primary_reason(self) -> WriterReadinessReasonCode | None:
        return self.reason_codes[0] if self.reason_codes else None


class WriterContextInputNotReady(RuntimeError):
    """The Writer input chain is not complete; Writer must not be called."""

    def __init__(self, decision: WriterReadinessDecision) -> None:
        self.decision = decision
        message = ", ".join(reason.value for reason in decision.reason_codes)
        super().__init__(f"Writer input is not ready: {message}")


def _package_mechanics(package: object) -> tuple[bool, list[str]]:
    details: list[str] = []
    assembly_status = getattr(package, "assembly_status", ContextAssemblyStatus.READY)
    budget = getattr(package, "budget_report", None)
    final_status = getattr(budget, "final_status", ContextAssemblyStatus.READY)
    if assembly_status != ContextAssemblyStatus.READY:
        details.append(f"assembly_status={assembly_status}")
    if final_status != ContextAssemblyStatus.READY:
        details.append(f"budget_status={final_status}")
    return (not details), details


def evaluate_package_readiness(package: object) -> WriterReadinessDecision:
    """Evaluate the package-local invariants shared by Stage 3 and WritingLoop."""

    mechanical, details = _package_mechanics(package)
    if not isinstance(package, WriterContextPackageV2):
        return (
            WriterReadinessDecision.pass_decision()
            if mechanical
            else WriterReadinessDecision.blocked(
                (WriterReadinessReasonCode.WRITER_PACKAGE_NOT_READY,), tuple(details)
            )
        )
    codes: list[WriterReadinessReasonCode] = []
    if not mechanical:
        codes.append(WriterReadinessReasonCode.WRITER_PACKAGE_NOT_READY)
    requirement = package.retrieval_requirement
    if requirement is HistoryRetrievalRequirement.UNDECIDED:
        codes.append(WriterReadinessReasonCode.HISTORY_DECISION_MISSING)
    elif requirement is HistoryRetrievalRequirement.REQUIRED:
        lineage = package.lineage
        if not lineage.need_ids:
            codes.append(WriterReadinessReasonCode.HISTORY_NEED_EMPTY)
        if (
            lineage.gateway_context_artifact is None
            or lineage.frozen_evidence_selections_artifact is None
        ):
            codes.append(WriterReadinessReasonCode.MEMORY_GATEWAY_NOT_EXECUTED)
        if package.semantic_status != "COMPLETE" or package.unclosed_mandatory_need_facets:
            codes.append(WriterReadinessReasonCode.MANDATORY_FACET_INCOMPLETE)
    else:
        if not package.lineage.history_waiver_ref:
            codes.append(WriterReadinessReasonCode.HISTORY_DECISION_MISSING)
    if codes:
        return WriterReadinessDecision.blocked(tuple(codes))
    return WriterReadinessDecision.pass_decision()


def evaluate_writer_readiness(
    *,
    plan: PlanRootDocument | None,
    target_chapter: int,
    writing_task: WritingTaskContract,
    world: WorldRootDocument,
    package: object,
    expected_plan_root_ref: ArtifactRef | None = None,
    manifest_plan_revision: str | None = None,
    projection_exact: bool,
    canonical_prose_present: bool | None = None,
) -> WriterReadinessDecision:
    """Full pre-model readiness gate for the production Writer request.

    ``projection_exact`` is required: a caller that cannot prove the Memory
    projection is exact must say so and be blocked, because ``None`` used to mean
    "unknown" and silently disabled the freshness gate for every production
    request.  ``canonical_prose_present`` states whether the canonical Text basis
    already holds prose; the host first-chapter history waiver is only valid while
    that basis is still empty.
    """

    codes: list[WriterReadinessReasonCode] = []
    details: list[str] = []
    package_decision = evaluate_package_readiness(package)
    codes.extend(package_decision.reason_codes)
    details.extend(package_decision.details)

    goals = tuple(
        goal
        for goal in (plan.chapter_goals if plan is not None else ())
        if goal.chapter_index == target_chapter
    )
    if len(goals) != 1:
        codes.append(WriterReadinessReasonCode.PLAN_TARGET_AMBIGUOUS)
        details.append(f"target chapter {target_chapter} has {len(goals)} active goals")
    goal = goals[0] if len(goals) == 1 else None

    if goal is not None:
        decision = chapter_goal_history_retrieval_decision(goal)
        if decision.requirement is HistoryRetrievalRequirement.UNDECIDED:
            codes.append(WriterReadinessReasonCode.HISTORY_DECISION_MISSING)
            details.append(f"goal {goal.goal_id.root} has no history retrieval decision")
        elif decision.requirement is HistoryRetrievalRequirement.NOT_REQUIRED and (
            not decision.waiver_ref
        ):
            codes.append(WriterReadinessReasonCode.HISTORY_DECISION_MISSING)
            details.append(f"goal {goal.goal_id.root} NOT_REQUIRED without waiver")
        elif (
            decision.requirement is HistoryRetrievalRequirement.NOT_REQUIRED
            and decision.waiver_ref == FIRST_CHAPTER_WAIVER_REF
            and canonical_prose_present
        ):
            codes.append(WriterReadinessReasonCode.HISTORY_WAIVER_NOT_APPLICABLE)
            details.append(
                f"goal {goal.goal_id.root} claims the first-chapter history waiver while "
                "the canonical basis already contains prose"
            )
        elif isinstance(package, WriterContextPackageV2) and (
            package.retrieval_requirement is not decision.requirement
        ):
            codes.append(WriterReadinessReasonCode.HISTORY_DECISION_MISSING)
            details.append(
                f"goal {goal.goal_id.root} decision does not match the Writer Context package"
            )

    due_obligations = {
        obligation.obligation_id
        for obligation in world.obligations
        if obligation_in_scope_for_chapter(obligation, target_chapter)
    }
    missing_obligations = due_obligations - set(writing_task.active_plan_obligations)
    if missing_obligations:
        codes.append(WriterReadinessReasonCode.OBLIGATION_BINDING_INCOMPLETE)
        details.append(
            "unbound due obligations: "
            + ", ".join(sorted(item.root for item in missing_obligations))
        )

    if isinstance(package, WriterContextPackageV2):
        lineage = package.lineage
        if lineage.plan_root_ref is None or not lineage.chapter_goal_ids:
            codes.append(WriterReadinessReasonCode.PLANNING_LINEAGE_INCOMPLETE)
            details.append("Writer Context lineage is missing plan root binding")
        elif expected_plan_root_ref is not None and (
            lineage.plan_root_ref.artifact_id != expected_plan_root_ref.artifact_id
        ):
            codes.append(WriterReadinessReasonCode.PLANNING_LINEAGE_INCOMPLETE)
            details.append("Writer Context lineage plan root does not match the accepted plan")
        if (
            manifest_plan_revision is not None
            and lineage.plan_revision is not None
            and lineage.plan_revision != manifest_plan_revision
        ):
            codes.append(WriterReadinessReasonCode.PLANNING_LINEAGE_INCOMPLETE)
            details.append("Writer Context lineage plan revision does not match the manifest")
    if not projection_exact:
        codes.append(WriterReadinessReasonCode.PROJECTION_NOT_EXACT)
        details.append("projection freshness is not exact")

    if codes:
        return WriterReadinessDecision.blocked(tuple(codes), tuple(details))
    return WriterReadinessDecision.pass_decision()
