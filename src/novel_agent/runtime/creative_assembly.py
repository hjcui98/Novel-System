"""Fail-closed Stage 5 production/isolated assembly admission."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from novel_agent.adapters.runtime.chapter_settlement import AtomicChapterSettlementAdapter
from novel_agent.adapters.runtime.materializers import (
    DraftCandidateMaterializer,
    PlanCandidateMaterializer,
)
from novel_agent.adapters.runtime.stage3_writer import (
    ProductionWritingRequestFactory,
    Stage3WritingLeafAdapter,
)
from novel_agent.adapters.runtime.stage4_planner import (
    ProductionStage4InvocationFactory,
    Stage4PlanningLeafAdapter,
)
from novel_agent.domain.creative_runtime import CreativeRunPolicy
from novel_agent.domain.ids import ProjectId, RunId
from novel_agent.domain.stage5_manifest import Stage5DevelopmentManifest
from novel_agent.ports.creative_runtime import RuntimeTaskReader
from novel_agent.runtime.creative_dispatcher import CreativeDispatcher
from novel_agent.services.creative_runtime import CreativeRuntimeService


@dataclass(frozen=True, slots=True)
class ProductionAssemblyContext:
    database_url: str
    object_store_root: Path
    project_id: ProjectId
    run_id: RunId
    policy: CreativeRunPolicy
    manifest: Stage5DevelopmentManifest


@dataclass(frozen=True, slots=True)
class ProductionRuntimeAssembly:
    runtime: CreativeRuntimeService
    dispatcher: CreativeDispatcher
    planner: Stage4PlanningLeafAdapter
    planner_invocation_factory: ProductionStage4InvocationFactory
    writer: Stage3WritingLeafAdapter
    writing_request_factory: ProductionWritingRequestFactory
    plan_materializer: PlanCandidateMaterializer
    draft_materializer: DraftCandidateMaterializer
    chapter_settlement: AtomicChapterSettlementAdapter
    task_reader: RuntimeTaskReader

    def __post_init__(self) -> None:
        if self.runtime.planner_leaf is not self.planner:
            raise ValueError("production runtime must use the declared Stage 4 adapter")
        if self.runtime.writer_leaf is not self.writer:
            raise ValueError("production runtime must use the declared Stage 3 adapter")
        if self.planner.invocation_factory is not self.planner_invocation_factory:
            raise ValueError("production Stage 4 adapter must use the declared invocation factory")
        if self.runtime.writing_request_factory is not self.writing_request_factory:
            raise ValueError("production runtime must use the declared Writing request factory")
        if self.runtime.plan_materializer is not self.plan_materializer:
            raise ValueError("production runtime must use the declared Plan materializer")
        if self.runtime.draft_materializer is not self.draft_materializer:
            raise ValueError("production runtime must use the declared Draft materializer")
        if self.runtime.chapter_settlement is not self.chapter_settlement:
            raise ValueError("production runtime must use the declared Chapter Settlement")
        if self.chapter_settlement.draft_materializer is not self.draft_materializer:
            raise ValueError("Chapter Settlement must reuse the declared Draft materializer")


ProductionAssemblyFactory = Callable[
    [ProductionAssemblyContext], ProductionRuntimeAssembly
]


def validate_runtime_assembly(
    manifest: Stage5DevelopmentManifest,
    *,
    planner: object,
    writer: object,
    plan_materializer: object,
    draft_materializer: object,
    production: bool,
    chapter_settlement: object | None = None,
) -> None:
    components = (planner, writer, plan_materializer, draft_materializer)
    fixture_flags = tuple(bool(getattr(item, "is_fixture", False)) for item in components)
    if production:
        if not manifest.feature_admission.real_stage4_adapter:
            raise RuntimeError("production Stage 5 requires an admitted real Stage 4 adapter")
        if any(fixture_flags):
            raise RuntimeError("production Stage 5 rejects fixture leaves and materializers")
        if chapter_settlement is None or bool(
            getattr(chapter_settlement, "is_fixture", False)
        ):
            raise RuntimeError("production Stage 5 requires atomic Chapter Settlement")
    else:
        if not fixture_flags[0]:
            raise RuntimeError("isolated A-layer assembly requires the strict fake Planner")
        if fixture_flags[1]:
            raise RuntimeError(
                "isolated A-layer primary path requires the real Stage 3 Writer adapter"
            )


def load_production_runtime_assembly(
    spec: str,
    context: ProductionAssemblyContext,
) -> ProductionRuntimeAssembly:
    """Load one deployment composition root shared by runtime CLI and vertical runner."""

    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("assembly factory must use module.path:callable")
    factory = getattr(import_module(module_name), attribute, None)
    if not callable(factory):
        raise ValueError("assembly factory does not resolve to a callable")
    assembly = factory(context)
    if not isinstance(assembly, ProductionRuntimeAssembly):
        raise TypeError("assembly factory returned the wrong production assembly type")
    validate_runtime_assembly(
        context.manifest,
        planner=assembly.planner,
        writer=assembly.writer,
        plan_materializer=assembly.plan_materializer,
        draft_materializer=assembly.draft_materializer,
        chapter_settlement=assembly.chapter_settlement,
        production=True,
    )
    return assembly


__all__ = [
    "ProductionAssemblyContext",
    "ProductionAssemblyFactory",
    "ProductionRuntimeAssembly",
    "load_production_runtime_assembly",
    "validate_runtime_assembly",
]
