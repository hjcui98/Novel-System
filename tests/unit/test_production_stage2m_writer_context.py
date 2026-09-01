from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.runtime.stage3_writer import Stage2MWriterContextInvocation
from novel_agent.domain.benchmark import (
    AuthorPlanningContext,
    ChapterDocument,
    ChapterGoal,
    PlanRootDocument,
    SceneDocument,
    TextRootDocument,
    VisibleOutlineNode,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import (
    CandidatePool,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.stage2 import (
    ControllerArm,
    ControllerStopReason,
    MemoryGatewayMode,
    MemoryGatewayPolicy,
    MemoryGatewayResult,
    MemoryResolutionRequest,
    PairedContextArmResult,
    ToolPolicy,
)
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextBlock, TextSpanRef
from novel_agent.domain.world import Entity
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    ContextAssemblyStatus,
    EvidenceGapKind,
    MemoryContextBudgetExhaustedError,
    NeedEvidenceSemanticStatus,
    NeedFacetSemanticReceipt,
    WriterContextEvidenceItem,
    WriterContextSection,
)
from novel_agent.runtime.memory_controller import RouteBoundControllerPolicy
from novel_agent.runtime.production_components import ProductionStage2MWriterContext
from novel_agent.services.artifacts import ArtifactRepository, sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes, quote_hash
from novel_agent.services.evidence_first_writer_context_assembler import (
    NEED_EVIDENCE_SELECTIONS_MEDIA_TYPE,
    EvidenceFirstAssemblyResult,
    EvidenceFirstWriterContextAssembler,
    FrozenNeedEvidenceSelections,
    NeedEvidenceSelection,
    SliceSelectionTrace,
)
from novel_agent.services.evidence_slice_resolver import EvidenceSliceResolver
from novel_agent.services.memory_gateway import MemoryGateway
from novel_agent.services.memory_pipeline import ContextCompiler, EvidenceExpander
from novel_agent.services.paired_controller import PairedMemoryControllerRunner
from novel_agent.services.retrieval import InMemoryRetrievalBackend
from novel_agent.services.task_conditioned_need_generation import TaskPlanConditionedNeedGenerator
from tests.unit.test_evidence_first_writer_context import _block

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "c" * 64)
COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.production-writer")
HERO = StableId("entity.hero")


class _FixedNeedGenerator:
    def __init__(self, needs: tuple[Stage1MemoryNeed, ...]) -> None:
        self._needs = needs

    def generate_with_lineage(self, *_args: object, **_kwargs: object) -> object:
        return type("Result", (), {"needs": self._needs})()


def _task() -> BenchmarkTaskContract:
    return BenchmarkTaskContract(
        task_id=StableId("task.production-writer-context"),
        task_text="Write chapter 21 from the frozen checkpoint.",
        checkpoint_chapter=20,
        target_chapter_start=21,
        target_chapter_end=21,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_template_version="production-writing-task.v1",
        output_contract_version="writer_context.v2",
        task_intent="Enter the tower.",
    )


def _need() -> Stage1MemoryNeed:
    return Stage1MemoryNeed(
        need_id=StableId("need.production.hero.state"),
        run_id=RunId("run.production-writer-context"),
        task_id=TaskId("task.production-writer-context"),
        base_commit=COMMIT,
        chapter_target=20,
        need_type="continuity",
        query_intent=Stage1QueryIntent.CURRENT_STATE,
        query_text="hero injury",
        entity_ids=(HERO,),
        why_needed="writer needs current state",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
        allowed_candidate_pools=(CandidatePool.R1, CandidatePool.ANCHOR),
        stop_condition="current state found",
        expected_section=WriterContextSection.CURRENT_WORLD_STATE,
        priority=90,
    )


def _text() -> TextRootDocument:
    block = _block("Lin's left arm is still injured after the tower fight.", chapter=20)
    assert block.scene_id is not None
    scene = SceneDocument(scene_id=block.scene_id, scene_index=0, blocks=(block,))
    chapter = ChapterDocument(
        chapter_id=block.chapter_id,
        chapter_index=20,
        scenes=(scene,),
    )
    return TextRootDocument(root_hash=HASH, schema_version=VERSION, chapters=(chapter,))


def _unit(block: TextBlock) -> RetrievalUnit:
    evidence = EvidenceRef(
        evidence_id=StableId("evidence.hero.injury"),
        root_hash=HASH,
        object_hash=sha256_id(block.text.encode("utf-8")),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=TextSpanRef(block_id=block.block_id, start=0, end=len(block.text)),
        quote_hash=quote_hash(block.text),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=COMMIT,
    )
    return RetrievalUnit(
        unit_id=StableId("anchor.hero.injury"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        source_artifact=HASH,
        text=block.text,
        entity_ids=(HERO,),
        mandatory=True,
        evidence_refs=(evidence,),
        access_scope="writer_safe",
    )


def _gateway(
    tmp_path: Path,
    unit: RetrievalUnit,
) -> tuple[MemoryGateway, ArtifactRepository]:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    policy = ToolPolicy(
        policy_id=StableId("policy.production-writer-context"),
        version=VERSION,
        content_hash=HASH,
        allowed_tools=("memory.search_exact", "memory.search_temporal"),
        max_rounds=3,
        max_tool_calls=24,
    )
    runner = PairedMemoryControllerRunner.from_shared_backend(
        backend=InMemoryRetrievalBackend((unit,)),
        needs=(),
        tool_policy=policy,
        compiler=ContextCompiler(EvidenceExpander()),
        controller_policy=RouteBoundControllerPolicy(),
        freshness_check=lambda _: True,
        checkpointer=InMemorySaver(),
        comparison_basis_fingerprint=HASH,
    )
    gateway = MemoryGateway(
        runner,
        MemoryGatewayPolicy(
            policy_id=StableId("policy.production-memory-gateway"),
            mode=MemoryGatewayMode.DETERMINISTIC,
            configuration_fingerprint=HASH,
        ),
        artifacts,
        schema_version=VERSION,
    )
    return gateway, artifacts


def _invocation(text: TextRootDocument) -> Stage2MWriterContextInvocation:
    return Stage2MWriterContextInvocation(
        run_id=RunId("run.production-writer-context"),
        task=_task(),
        planning_context=AuthorPlanningContext(
            profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            task_intent="Enter the tower.",
            target_range=(21, 21),
            visible_outline_nodes=(
                VisibleOutlineNode(
                    node_id=StableId("outline.21"),
                    title="Tower",
                    summary="Enter the tower.",
                ),
            ),
            chapter_goals=(
                ChapterGoal(
                    goal_id=StableId("plan.chapter.21"),
                    chapter_index=21,
                    summary="Enter the tower.",
                ),
            ),
            source_hash=HASH,
        ),
        plan=PlanRootDocument(
            root_hash=HASH,
            schema_version=VERSION,
            chapter_goals=(
                ChapterGoal(
                    goal_id=StableId("plan.chapter.21"),
                    chapter_index=21,
                    summary="Enter the tower.",
                ),
            ),
        ),
        text=text,
        world=WorldRootDocument(
            root_hash=HASH,
            schema_version=VERSION,
            source_commit=COMMIT,
            entities=(Entity(entity_id=HERO, entity_type="character", internal_label="Lin"),),
        ),
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        project_id=ProjectId("project.production-writer"),
    )


def test_production_writer_context_uses_gateway_selections_not_empty_stubs(
    tmp_path: Path,
) -> None:
    text = _text()
    block = text.chapters[0].scenes[0].blocks[0]
    gateway, artifacts = _gateway(tmp_path, _unit(block))
    context = ProductionStage2MWriterContext(
        generator=cast(TaskPlanConditionedNeedGenerator, _FixedNeedGenerator((_need(),))),
        gateway=gateway,
        assembler=EvidenceFirstWriterContextAssembler(),
        artifacts=artifacts,
        schema_version=VERSION,
    )

    result = context(_invocation(text))

    assert result.status is ContextAssemblyStatus.READY
    assert result.package.items
    assert any(item.gap is None for item in result.package.items)
    assert result.evidence_ledger.entries
    assert result.package.lineage.gateway_context_artifact is not None
    assert result.package.lineage.frozen_evidence_selections_artifact is not None
    frozen = FrozenNeedEvidenceSelections.model_validate_json(
        artifacts.read_verified(result.package.lineage.frozen_evidence_selections_artifact),
        strict=True,
    )
    assert frozen.selections[0].slices
    assert frozen.base_commit == COMMIT
    assert frozen.snapshot_id == SNAPSHOT


def test_production_writer_context_keeps_empty_selection_gaps(tmp_path: Path) -> None:
    text = _text()
    empty_unit = _unit(text.chapters[0].scenes[0].blocks[0]).model_copy(
        update={"entity_ids": (StableId("entity.other"),), "text": "unrelated"}
    )
    gateway, artifacts = _gateway(tmp_path, empty_unit)
    context = ProductionStage2MWriterContext(
        generator=cast(TaskPlanConditionedNeedGenerator, _FixedNeedGenerator((_need(),))),
        gateway=gateway,
        assembler=EvidenceFirstWriterContextAssembler(),
        artifacts=artifacts,
        schema_version=VERSION,
    )

    result = context(_invocation(text))

    assert result.package.gaps
    assert result.package.lineage.frozen_evidence_selections_artifact is not None
    frozen = FrozenNeedEvidenceSelections.model_validate_json(
        artifacts.read_verified(result.package.lineage.frozen_evidence_selections_artifact),
        strict=True,
    )
    assert frozen.need_ids == (_need().need_id,)
    assert frozen.selections[0].slices == ()


def test_production_writer_context_rejects_corrupt_or_mismatched_selection_artifact(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    text = _text()
    need = _need()
    bad = artifacts.put(b"not-json", NEED_EVIDENCE_SELECTIONS_MEDIA_TYPE, VERSION)
    context_ref = artifacts.put(b"{}", "application/vnd.novel-agent.context-package+json", VERSION)

    class _BrokenGateway:
        def resolve(
            self, request: MemoryResolutionRequest, *_: object, **__: object
        ) -> MemoryGatewayResult:
            from novel_agent.domain.memory import ContextBudgetReport, Stage1ContextPackage

            package = Stage1ContextPackage(
                context_id=StableId("context.broken"),
                base_commit=request.base_commit,
                snapshot_id=request.snapshot_id,
                task_contract=request.task_contract,
                budget_report=ContextBudgetReport(
                    token_budget=24_000,
                    mandatory_tokens=0,
                    optional_tokens=0,
                    full_chapter_read_count=0,
                ),
            )
            selected = PairedContextArmResult.model_construct(
                arm=ControllerArm.DETERMINISTIC,
                context=package,
                selected_unit_ids=(),
                retrieval_call_count=0,
                stop_reason=ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
                comparison_basis_fingerprint=HASH,
                future_leakage_count=0,
            )
            return MemoryGatewayResult.model_construct(
                gateway_result_id=StableId("gateway-result.broken"),
                request_id=request.request_id,
                selected_arm=ControllerArm.DETERMINISTIC,
                fallback_used=False,
                context=package,
                frozen_context_artifact=context_ref,
                frozen_evidence_selections_artifact=bad,
                selected_result=selected,
                policy_id=StableId("policy.broken"),
                configuration_fingerprint=HASH,
            )

    context = ProductionStage2MWriterContext(
        generator=cast(TaskPlanConditionedNeedGenerator, _FixedNeedGenerator((need,))),
        gateway=_BrokenGateway(),  # type: ignore[arg-type]
        assembler=EvidenceFirstWriterContextAssembler(),
        artifacts=artifacts,
        schema_version=VERSION,
    )
    with pytest.raises(ValueError, match="unreadable"):
        context(_invocation(text))


def test_production_writer_context_preserves_semantic_receipts(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    text = _text()
    block = text.chapters[0].scenes[0].blocks[0]
    need = _need()
    slice_ = EvidenceSliceResolver().resolve_block(
        block,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        access_scope="writer_safe",
    )[0]
    selection = NeedEvidenceSelection(
        need=need,
        selections=(
            SliceSelectionTrace(
                slice_id=slice_.slice_id,
                unit_id=StableId("anchor.hero.injury"),
                route_channel="r1_exact",
                fused_rank=1,
                selection_reason="gateway_live_l0",
            ),
        ),
        slices=(slice_,),
        semantic_receipts=(
            NeedFacetSemanticReceipt(
                need_id=need.need_id,
                need_facet_id=StableId("facet.hero.state"),
                facet_kind="current_state",
                status=NeedEvidenceSemanticStatus.SUPPORTED,
                mandatory=True,
                evaluated_slice_ids=(slice_.slice_id,),
                supporting_slice_ids=(slice_.slice_id,),
                judge_version="test",
            ),
        ),
    )
    frozen = artifacts.put(
        canonical_json_bytes(
            FrozenNeedEvidenceSelections(
                request_id=StableId("request.semantic"),
                base_commit=COMMIT,
                snapshot_id=SNAPSHOT,
                need_ids=(need.need_id,),
                selections=(selection,),
            ).model_dump(mode="json")
        ),
        NEED_EVIDENCE_SELECTIONS_MEDIA_TYPE,
        VERSION,
    )
    context_ref = artifacts.put(
        canonical_json_bytes({"context": "semantic"}),
        "application/vnd.novel-agent.context-package+json",
        VERSION,
    )

    class _Gateway:
        def resolve(
            self, request: MemoryResolutionRequest, *_: object, **__: object
        ) -> MemoryGatewayResult:
            from novel_agent.domain.memory import ContextBudgetReport, Stage1ContextPackage

            package = Stage1ContextPackage(
                context_id=StableId("context.semantic"),
                base_commit=request.base_commit,
                snapshot_id=request.snapshot_id,
                task_contract=request.task_contract,
                budget_report=ContextBudgetReport(
                    token_budget=24_000,
                    mandatory_tokens=0,
                    optional_tokens=0,
                    full_chapter_read_count=0,
                ),
            )
            selected = PairedContextArmResult.model_construct(
                arm=ControllerArm.DETERMINISTIC,
                context=package,
                selected_unit_ids=(),
                retrieval_call_count=1,
                stop_reason=ControllerStopReason.SUFFICIENT,
                comparison_basis_fingerprint=HASH,
                future_leakage_count=0,
            )
            return MemoryGatewayResult.model_construct(
                gateway_result_id=StableId("gateway-result.semantic"),
                request_id=request.request_id,
                selected_arm=ControllerArm.DETERMINISTIC,
                fallback_used=False,
                context=package,
                frozen_context_artifact=context_ref,
                frozen_evidence_selections_artifact=frozen.model_copy(
                    update={"artifact_id": frozen.artifact_id}
                )
                if False
                else frozen,
                selected_result=selected,
                policy_id=StableId("policy.semantic"),
                configuration_fingerprint=HASH,
            )

    context = ProductionStage2MWriterContext(
        generator=cast(TaskPlanConditionedNeedGenerator, _FixedNeedGenerator((need,))),
        gateway=_Gateway(),  # type: ignore[arg-type]
        assembler=EvidenceFirstWriterContextAssembler(),
        artifacts=artifacts,
        schema_version=VERSION,
    )
    # The frozen artifact binds a fixed request_id; rewrite it to the live one.
    live_request_id = None

    class _RewritingGateway(_Gateway):
        def resolve(
            self, request: MemoryResolutionRequest, *_: object, **__: object
        ) -> MemoryGatewayResult:
            nonlocal live_request_id
            live_request_id = request.request_id
            rewritten = artifacts.put(
                canonical_json_bytes(
                    FrozenNeedEvidenceSelections(
                        request_id=request.request_id,
                        base_commit=COMMIT,
                        snapshot_id=SNAPSHOT,
                        need_ids=(need.need_id,),
                        selections=(selection,),
                    ).model_dump(mode="json")
                ),
                NEED_EVIDENCE_SELECTIONS_MEDIA_TYPE,
                VERSION,
            )
            result = super().resolve(request)
            return result.model_copy(update={"frozen_evidence_selections_artifact": rewritten})

    context = ProductionStage2MWriterContext(
        generator=cast(TaskPlanConditionedNeedGenerator, _FixedNeedGenerator((need,))),
        gateway=_RewritingGateway(),  # type: ignore[arg-type]
        assembler=EvidenceFirstWriterContextAssembler(),
        artifacts=artifacts,
        schema_version=VERSION,
    )
    result = context(_invocation(text))
    assert result.semantic_receipts
    assert result.semantic_receipts[0].status is NeedEvidenceSemanticStatus.SUPPORTED


def test_production_writer_context_does_not_expand_on_ordinary_gaps(tmp_path: Path) -> None:
    text = _text()
    empty_unit = _unit(text.chapters[0].scenes[0].blocks[0]).model_copy(
        update={"entity_ids": (StableId("entity.other"),)}
    )
    gateway, artifacts = _gateway(tmp_path, empty_unit)
    original_resolve = gateway.resolve
    calls: list[str] = []

    def _count(
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        *,
        thread_id: str,
        evaluator_only_artifacts: tuple[ArtifactId, ...] = (),
    ) -> MemoryGatewayResult:
        calls.append(request.request_id.root)
        return original_resolve(
            request,
            text_root,
            thread_id=thread_id,
            evaluator_only_artifacts=evaluator_only_artifacts,
        )

    gateway.resolve = _count
    context = ProductionStage2MWriterContext(
        generator=cast(TaskPlanConditionedNeedGenerator, _FixedNeedGenerator((_need(),))),
        gateway=gateway,
        assembler=EvidenceFirstWriterContextAssembler(),
        artifacts=artifacts,
        schema_version=VERSION,
    )
    context(_invocation(text))
    assert len(calls) == 1
    assert "base" in calls[0]


def test_production_writer_context_expands_packing_then_reviews(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    text = _text()
    need = _need()
    block = text.chapters[0].scenes[0].blocks[0]
    slice_ = EvidenceSliceResolver().resolve_block(
        block,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        access_scope="writer_safe",
    )[0]
    selection = NeedEvidenceSelection(
        need=need,
        selections=(
            SliceSelectionTrace(
                slice_id=slice_.slice_id,
                unit_id=StableId("anchor.hero.injury"),
                route_channel="r1_exact",
                fused_rank=1,
                selection_reason="gateway_live_l0",
            ),
        ),
        slices=(slice_,),
    )
    resolves: list[int] = []

    class _SaturatedGateway:
        def resolve(
            self, request: MemoryResolutionRequest, *_: object, **__: object
        ) -> MemoryGatewayResult:
            from novel_agent.domain.memory import ContextBudgetReport, Stage1ContextPackage

            resolves.append(request.retrieval_budget.max_tool_calls)
            frozen = artifacts.put(
                canonical_json_bytes(
                    FrozenNeedEvidenceSelections(
                        request_id=request.request_id,
                        base_commit=request.base_commit,
                        snapshot_id=request.snapshot_id,
                        need_ids=(need.need_id,),
                        selections=(selection,),
                    ).model_dump(mode="json")
                ),
                NEED_EVIDENCE_SELECTIONS_MEDIA_TYPE,
                VERSION,
            )
            context_ref = artifacts.put(
                canonical_json_bytes({"tier": request.request_id.root}),
                "application/vnd.novel-agent.context-package+json",
                VERSION,
            )
            package = Stage1ContextPackage(
                context_id=StableId(f"context.{len(resolves)}"),
                base_commit=request.base_commit,
                snapshot_id=request.snapshot_id,
                task_contract=request.task_contract,
                budget_report=ContextBudgetReport(
                    token_budget=request.context_budget.token_budget,
                    mandatory_tokens=0,
                    optional_tokens=0,
                    full_chapter_read_count=0,
                ),
            )
            selected = PairedContextArmResult.model_construct(
                arm=ControllerArm.DETERMINISTIC,
                context=package,
                selected_unit_ids=(),
                retrieval_call_count=request.retrieval_budget.max_tool_calls,
                stop_reason=ControllerStopReason.BUDGET_EXHAUSTED,
                comparison_basis_fingerprint=HASH,
                future_leakage_count=0,
            )
            return MemoryGatewayResult.model_construct(
                gateway_result_id=StableId(f"gateway-result.{len(resolves)}"),
                request_id=request.request_id,
                selected_arm=ControllerArm.DETERMINISTIC,
                fallback_used=False,
                context=package,
                frozen_context_artifact=context_ref,
                frozen_evidence_selections_artifact=frozen,
                selected_result=selected,
                policy_id=StableId("policy.saturated"),
                configuration_fingerprint=HASH,
            )

    class _TinyAssembler:
        def assemble(self, **kwargs: object) -> EvidenceFirstAssemblyResult:
            item = WriterContextEvidenceItem.model_construct(
                item_id=StableId("item.budget"),
                need_ids=(need.need_id,),
                need_facet_ids=(),
                section=WriterContextSection.CURRENT_WORLD_STATE,
                purpose="budget packing",
                mandatory=True,
                gap=type("Gap", (), {"kind": EvidenceGapKind.BUDGET_EXCEEDED})(),
            )
            package = type(
                "Package",
                (),
                {
                    "items": (item,),
                    "gaps": (),
                    "lineage": type("Lineage", (), {})(),
                    "task_contract": kwargs["task"],
                    "basis_commit_id": kwargs["basis_commit_id"],
                    "basis_snapshot_id": kwargs["basis_snapshot_id"],
                },
            )()
            return EvidenceFirstAssemblyResult.model_construct(
                status=ContextAssemblyStatus.READY,
                package=package,
                evidence_ledger=type("Ledger", (), {"entries": (1,)})(),
                mandatory_facet_closure="INCOMPLETE",
                assembler_version="test",
            )

    context = ProductionStage2MWriterContext(
        generator=cast(TaskPlanConditionedNeedGenerator, _FixedNeedGenerator((need,))),
        gateway=_SaturatedGateway(),  # type: ignore[arg-type]
        assembler=_TinyAssembler(),  # type: ignore[arg-type]
        artifacts=artifacts,
        schema_version=VERSION,
    )
    with pytest.raises(MemoryContextBudgetExhaustedError):
        context(_invocation(text))
    assert resolves == [16, 20, 24]
