from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from novel_agent.adapters.runtime.stage3_writer import Stage2MWriterContextInvocation
from novel_agent.domain.benchmark import ChapterGoal, PlanRootDocument
from novel_agent.domain.generation import WritingLengthPolicy, WritingTaskContract
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.retrieval_decision import (
    FIRST_CHAPTER_WAIVER_REF,
    HistoryRetrievalReasonCode,
    HistoryRetrievalRequirement,
    RetrievalExecutionStatus,
)
from novel_agent.domain.writer_context import BenchmarkTaskContract
from novel_agent.domain.writer_readiness import (
    WriterContextInputNotReady,
    WriterReadinessReasonCode,
    evaluate_package_readiness,
    evaluate_writer_readiness,
)
from novel_agent.runtime.production_components import ProductionStage2MWriterContext
from novel_agent.services.evidence_first_writer_context_assembler import (
    EvidenceFirstWriterContextAssembler,
)
from novel_agent.services.task_conditioned_need_generation import (
    NeedGenerationResult,
    NeedGenerationStatus,
    TaskPlanConditionedNeedGenerator,
)
from novel_agent.services.task_focus import FocusSet
from tests.unit.test_production_stage2m_writer_context import (
    COMMIT,
    HASH,
    HERO,
    VERSION,
    _gateway,
    _task,
    _text,
    _unit,
)


def _writing_task(chapter: int = 21) -> WritingTaskContract:
    return WritingTaskContract(
        contract_id=StableId("writing-contract.retrieval-gate"),
        target_chapter=chapter,
        target_scenes=(StableId(f"scene.chapter.{chapter}.0"),),
        pov="third_person_limited",
        narrative_person="third",
        chapter_goal="Enter the tower.",
        length_policy=WritingLengthPolicy(
            minimum_characters=100,
            target_characters=200,
            maximum_characters=400,
        ),
    )


def _invocation_with(
    text: Any,
    *,
    chapter: int = 21,
    goal_payload: dict[str, Any],
    source_hash: ArtifactId = HASH,
) -> Stage2MWriterContextInvocation:
    base = _invocation_base(text)
    goal = ChapterGoal(
        goal_id=StableId(f"plan.chapter.{chapter}"),
        chapter_index=chapter,
        summary="Enter the tower.",
        payload=goal_payload,
    )
    task = BenchmarkTaskContract(
        task_id=StableId(f"task.retrieval-gate.{chapter}"),
        task_text=f"Write chapter {chapter}.",
        checkpoint_chapter=max(chapter - 1, 0),
        target_chapter_start=chapter,
        target_chapter_end=chapter,
        information_profile=base.task.information_profile,
        task_template_version="production-writing-task.v1",
        output_contract_version="writer_context.v2",
        task_intent="Enter the tower.",
    )
    return replace(
        base,
        task=task,
        plan=PlanRootDocument(
            root_hash=HASH,
            schema_version=VERSION,
            chapter_goals=(goal,),
        ),
        writing_task=_writing_task(chapter),
    )


def _invocation_base(text: Any) -> Stage2MWriterContextInvocation:
    from tests.unit.test_production_stage2m_writer_context import _invocation

    return _invocation(text)


class _StubGenerator:
    def __init__(self, result: NeedGenerationResult) -> None:
        self._result = result

    def generate_for_writing_task(self, *_: object, **__: object) -> NeedGenerationResult:
        return self._result


def _result(
    *,
    status: NeedGenerationStatus,
    requirement: HistoryRetrievalRequirement,
    needs: tuple[Any, ...] = (),
    waiver_ref: str | None = None,
    reason_code: HistoryRetrievalReasonCode | None = None,
) -> NeedGenerationResult:
    task = _task()
    return NeedGenerationResult(
        task_id=task.task_id,
        focus_set=FocusSet(task_id=task.task_id, focuses=()),
        needs=needs,
        status=status,
        need_completion_spec_version="need_completion_spec.v1",
        generator_version="test",
        retrieval_requirement=requirement,
        retrieval_reason_code=reason_code,
        history_waiver_ref=waiver_ref,
    )


class _ForbiddenGateway:
    def resolve(self, *_args: object, **_kwargs: object) -> Any:
        raise AssertionError("Memory Gateway must not run before the decision gate")


def _context(
    result: NeedGenerationResult,
    *,
    artifacts: Any | None = None,
    gateway: Any | None = None,
) -> ProductionStage2MWriterContext:
    if artifacts is None:
        from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
        from novel_agent.services.artifacts import ArtifactRepository

        artifacts = ArtifactRepository(FilesystemObjectStore(Path("/tmp/unused-objects")))
    return ProductionStage2MWriterContext(
        generator=cast(TaskPlanConditionedNeedGenerator, _StubGenerator(result)),
        gateway=gateway or _ForbiddenGateway(),
        assembler=EvidenceFirstWriterContextAssembler(),
        artifacts=artifacts,
        schema_version=VERSION,
    )


def test_a1_legacy_empty_history_needs_is_undecided_and_blocks_before_models(
    tmp_path: Path,
) -> None:
    from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
    from novel_agent.services.artifacts import ArtifactRepository

    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    text = _text()
    invocation = _invocation_with(text, goal_payload={"history_needs": []})
    generator = TaskPlanConditionedNeedGenerator()
    result = generator.generate_for_writing_task(
        invocation.task,
        invocation.writing_task,
        invocation.world,
        invocation.plan,
        invocation.planning_context,
    )
    assert result.status is NeedGenerationStatus.INVALID
    assert result.retrieval_requirement is HistoryRetrievalRequirement.UNDECIDED
    assert result.needs == ()

    context = ProductionStage2MWriterContext(
        generator=generator,
        gateway=_ForbiddenGateway(),
        assembler=EvidenceFirstWriterContextAssembler(),
        artifacts=artifacts,
        schema_version=VERSION,
    )
    with pytest.raises(WriterContextInputNotReady) as error:
        context(invocation)
    assert error.value.decision.primary_reason is WriterReadinessReasonCode.HISTORY_DECISION_MISSING


def test_a2_required_decision_with_zero_needs_blocks_as_history_need_empty() -> None:
    text = _text()
    invocation = _invocation_with(
        text,
        goal_payload={
            "history_retrieval": {
                "requirement": "REQUIRED",
                "needs": [{"kind": "causal_history", "query": "此前受伤状态"}],
            }
        },
    )
    context = _context(
        _result(
            status=NeedGenerationStatus.NO_FOCUS,
            requirement=HistoryRetrievalRequirement.REQUIRED,
        )
    )
    with pytest.raises(WriterContextInputNotReady) as error:
        context(invocation)
    assert error.value.decision.primary_reason is WriterReadinessReasonCode.HISTORY_NEED_EMPTY


def test_a4_chapter_one_auto_waiver_is_not_applicable_and_releasable(tmp_path: Path) -> None:
    from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
    from novel_agent.services.artifacts import ArtifactRepository

    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    text = _text()
    invocation = _invocation_with(text, chapter=1, goal_payload={"history_needs": []})
    generator = TaskPlanConditionedNeedGenerator()
    result = generator.generate_for_writing_task(
        invocation.task,
        invocation.writing_task,
        invocation.world,
        invocation.plan,
        invocation.planning_context,
    )
    assert result.retrieval_requirement is HistoryRetrievalRequirement.NOT_REQUIRED
    assert result.history_waiver_ref == "waiver.history.first_chapter"

    context = ProductionStage2MWriterContext(
        generator=generator,
        gateway=_ForbiddenGateway(),
        assembler=EvidenceFirstWriterContextAssembler(),
        artifacts=artifacts,
        schema_version=VERSION,
    )
    assembly = context(invocation)
    package = assembly.package
    assert package.retrieval_requirement is HistoryRetrievalRequirement.NOT_REQUIRED
    assert package.retrieval_status is RetrievalExecutionStatus.NOT_APPLICABLE
    assert package.semantic_status == "NOT_APPLICABLE"
    assert package.lineage.history_waiver_ref == "waiver.history.first_chapter"
    assert evaluate_package_readiness(package).ready


def test_a3_required_retrieval_without_hits_never_claims_complete(tmp_path: Path) -> None:
    from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
    from novel_agent.services.artifacts import ArtifactRepository

    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    text = _text()
    unmatched_unit = _unit(text.chapters[0].scenes[0].blocks[0]).model_copy(
        update={"entity_ids": (StableId("entity.unrelated"),), "text": "unrelated"}
    )
    gateway, _ = _gateway(tmp_path, unmatched_unit)
    invocation = _invocation_with(
        text,
        goal_payload={
            "history_retrieval": {
                "requirement": "REQUIRED",
                "needs": [
                    {
                        "kind": "causal_history",
                        "query": "此前受伤状态",
                        "entity_ids": [HERO.root],
                    }
                ],
            }
        },
    )
    generator = TaskPlanConditionedNeedGenerator()
    context = ProductionStage2MWriterContext(
        generator=generator,
        gateway=gateway,
        assembler=EvidenceFirstWriterContextAssembler(),
        artifacts=artifacts,
        schema_version=VERSION,
    )
    assembly = context(invocation)
    package = assembly.package
    assert package.semantic_status in {"INCOMPLETE", "UNASSESSED"}
    assert package.gaps
    decision = evaluate_package_readiness(package)
    assert not decision.ready
    assert WriterReadinessReasonCode.MANDATORY_FACET_INCOMPLETE in decision.reason_codes


def test_a9_plan_root_rejects_two_active_goals_for_one_chapter() -> None:
    with pytest.raises(ValidationError, match="at most one active chapter goal"):
        PlanRootDocument(
            root_hash=HASH,
            schema_version=VERSION,
            chapter_goals=(
                ChapterGoal(
                    goal_id=StableId("ch2_goal"),
                    chapter_index=2,
                    summary="old",
                ),
                ChapterGoal(
                    goal_id=StableId("ch2_plan_item"),
                    chapter_index=2,
                    summary="new",
                ),
            ),
        )


def test_host_obligation_need_is_merged_even_with_not_required_decision() -> None:
    from novel_agent.domain.memory import (
        ObligationKind,
        ObligationStatus,
        PlanObligation,
        WorldRootDocument,
    )

    task = _task()
    plan = PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        chapter_goals=(
            ChapterGoal(
                goal_id=StableId("plan.chapter.21"),
                chapter_index=21,
                summary="Enter the tower.",
                payload={
                    "history_retrieval": {
                        "requirement": "NOT_REQUIRED",
                        "reason_code": "no_historical_dependency",
                        "waiver_ref": "waiver.review.ch21",
                    }
                },
            ),
        ),
    )
    world = WorldRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        source_commit=COMMIT,
        obligations=(
            PlanObligation(
                obligation_id=StableId("obligation.foreshadow.tower"),
                kind=ObligationKind.FORESHADOWING,
                description="塔中封印者身份",
                status=ObligationStatus.OPEN,
                not_before_chapter=300,
            ),
        ),
    )
    goal = plan.chapter_goals[0].model_copy(
        update={"obligation_ids": (StableId("obligation.foreshadow.tower"),)}
    )
    plan = plan.model_copy(update={"chapter_goals": (goal,)})
    result = TaskPlanConditionedNeedGenerator().generate_for_writing_task(
        task,
        _writing_task(),
        world,
        plan,
        None,
    )
    assert result.retrieval_requirement is HistoryRetrievalRequirement.REQUIRED
    assert result.needs
    assert result.needs[0].need_type == "setup_evidence"


def test_first_chapter_waiver_is_not_applicable_once_canonical_prose_exists(
    tmp_path: Path,
) -> None:
    """The host waiver only covers an empty canonical basis (plan section 5.3)."""

    from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
    from novel_agent.services.artifacts import ArtifactRepository

    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    invocation = _invocation_with(
        _text(),
        chapter=1,
        goal_payload={
            "history_retrieval": {
                "requirement": "NOT_REQUIRED",
                "reason_code": "first_chapter",
                "waiver_ref": FIRST_CHAPTER_WAIVER_REF,
            }
        },
    )
    generator = TaskPlanConditionedNeedGenerator()
    context = ProductionStage2MWriterContext(
        generator=generator,
        gateway=_ForbiddenGateway(),
        assembler=EvidenceFirstWriterContextAssembler(),
        artifacts=artifacts,
        schema_version=VERSION,
    )
    package = context(invocation).package

    empty_basis = evaluate_writer_readiness(
        plan=invocation.plan,
        target_chapter=1,
        writing_task=invocation.writing_task,
        world=invocation.world,
        package=package,
        canonical_prose_present=False,
    )
    assert WriterReadinessReasonCode.HISTORY_WAIVER_NOT_APPLICABLE not in (empty_basis.reason_codes)

    with_prose = evaluate_writer_readiness(
        plan=invocation.plan,
        target_chapter=1,
        writing_task=invocation.writing_task,
        world=invocation.world,
        package=package,
        canonical_prose_present=True,
    )
    assert WriterReadinessReasonCode.HISTORY_WAIVER_NOT_APPLICABLE in with_prose.reason_codes
    assert with_prose.ready is False
