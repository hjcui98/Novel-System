"""Fail-closed Stage 5 production/isolated assembly admission."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.runtime.chapter_settlement import (
    AtomicChapterSettlementAdapter,
    ChapterSettlementPolicy,
)
from novel_agent.adapters.runtime.materializers import (
    DraftCandidateMaterializer,
    PlanCandidateMaterializer,
)
from novel_agent.adapters.runtime.stage3_writer import (
    ProductionWritingRequestFactory,
    Stage3WritingLeafAdapter,
    WritingRequestPolicy,
)
from novel_agent.adapters.runtime.stage4_planner import (
    ProductionStage4InvocationFactory,
    Stage4InvocationPolicy,
    Stage4PlanningLeafAdapter,
)
from novel_agent.domain.creative_runtime import CreativeRunPolicy
from novel_agent.domain.ids import ProjectId, RunId, SchemaVersion
from novel_agent.domain.production_assembly import (
    ProductionAssemblySpec,
    ResolvedProductionAssemblyAttestation,
)
from novel_agent.domain.stage5_manifest import Stage5DevelopmentManifest
from novel_agent.ports.creative_runtime import MemoryMaintenancePort, RuntimeTaskReader
from novel_agent.runtime.creative_dispatcher import CreativeDispatcher
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.creative_runtime import CreativeRuntimeService
from novel_agent.services.memory_gateway import MemoryGateway
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.model_request_admission import ModelRequestAdmissionController
from novel_agent.services.projection import ProjectionBuilder
from novel_agent.services.retrieval import RerankService, RetrievalBackend

DEFAULT_PRODUCTION_ASSEMBLY_FACTORY = (
    "novel_agent.runtime.creative_assembly:build_production_assembly"
)


@dataclass(frozen=True, slots=True)
class ProductionAssemblyContext:
    database_url: str
    object_store_root: Path
    project_id: ProjectId
    run_id: RunId
    policy: CreativeRunPolicy
    manifest: Stage5DevelopmentManifest
    model_endpoints: tuple[RegisteredModelEndpoint, ...] = ()
    retrieval_backend: RetrievalBackend | None = None
    reranker: RerankService | None = None
    spec: ProductionAssemblySpec | None = None
    writing_policy: WritingRequestPolicy | None = None
    stage4_policy: Stage4InvocationPolicy | None = None
    model_request_namespace: str | None = None
    settlement_policy: ChapterSettlementPolicy | None = None
    # Optional isolated-campaign override; None preserves the production default.
    settlement_timeout_seconds: float | None = None
    # Optional isolated-campaign override; None preserves the production default.
    settlement_output_tokens: int | None = None
    # Optional isolated-campaign override; None preserves the production default.
    settlement_token_budget: int | None = None
    # Optional isolated-campaign override; Graph Curator extraction may use
    # one model call per bounded source unit.
    settlement_max_total_model_calls: int | None = None
    # Isolated U8-C campaign mode: run maintenance through validation and
    # acceptance gates but stop before Canon commit.  Production defaults to
    # False and retain the normal commit owner.
    memory_write_validation_only: bool = False
    # Optional isolated-campaign override; None preserves the production default.
    max_major_rewrites: int | None = None
    # Optional isolated-campaign override; None preserves the production default.
    max_local_repairs: int | None = None
    schema_version: SchemaVersion | None = None
    worker_id: str = "production.dispatcher"
    admission: ModelRequestAdmissionController | None = None
    projection_builder: ProjectionBuilder | None = None


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
    memory_maintenance: MemoryMaintenancePort | None = None
    session_factory: sessionmaker[Session] | None = None
    artifacts: ArtifactRepository | None = None
    model_gateway: ModelGateway | None = None
    memory_gateway: MemoryGateway | None = None
    attestation: ResolvedProductionAssemblyAttestation | None = None

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
        if (
            self.memory_maintenance is not None
            and self.runtime.memory_maintenance is not self.memory_maintenance
        ):
            raise ValueError("production runtime must use the declared Memory Maintenance")
        if self.chapter_settlement.draft_materializer is not self.draft_materializer:
            raise ValueError("Chapter Settlement must reuse the declared Draft materializer")
        task_factory = getattr(self.task_reader, "session_factory", None)
        if self.session_factory is not None and task_factory is not self.session_factory:
            raise ValueError("production task reader must use the declared session factory")
        if self.artifacts is not None and self.model_gateway is not None:
            raw_artifacts = self.model_gateway.raw_artifacts
            if raw_artifacts is not self.artifacts:
                raise ValueError(
                    "production ModelGateway must reuse the declared artifact repository"
                )
            if self.model_gateway.admission_controller is None:
                raise ValueError("production ModelGateway must use endpoint admission")
        if self.artifacts is not None:
            runtime_artifacts = getattr(self.runtime, "_artifacts", None)
            if runtime_artifacts is not self.artifacts:
                raise ValueError("production runtime must reuse the declared artifact repository")
            memory_artifacts = getattr(self.memory_gateway, "_artifacts", None)
            if memory_artifacts is not self.artifacts:
                raise ValueError(
                    "production MemoryGateway must reuse the declared artifact repository"
                )
        runtime_task_reader = getattr(self.runtime, "_task_reader", None)
        if runtime_task_reader is not None and runtime_task_reader is not self.task_reader:
            raise ValueError("production runtime must reuse the declared task reader")
        if self.session_factory is not None:
            runtime_commands = getattr(self.runtime, "_commands", None)
            command_factory = getattr(runtime_commands, "_session_factory", None)
            if command_factory is not None and command_factory is not self.session_factory:
                raise ValueError("production runtime commands must reuse the session factory")
        dispatcher_tasks = getattr(self.dispatcher, "_tasks", None)
        if dispatcher_tasks is not None and dispatcher_tasks is not self.task_reader:
            raise ValueError("production dispatcher must reuse the declared task reader")
        dispatcher_runtime = getattr(self.dispatcher, "_runtime", None)
        if dispatcher_runtime is not None and dispatcher_runtime is not self.runtime:
            raise ValueError("production dispatcher must reuse the declared runtime")


ProductionAssemblyFactory = Callable[[ProductionAssemblyContext], ProductionRuntimeAssembly]


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
        if chapter_settlement is None or bool(getattr(chapter_settlement, "is_fixture", False)):
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
    missing = tuple(
        name
        for name, value in (
            ("session_factory", assembly.session_factory),
            ("artifacts", assembly.artifacts),
            ("model_gateway", assembly.model_gateway),
            ("memory_gateway", assembly.memory_gateway),
            ("memory_maintenance", assembly.memory_maintenance),
            ("attestation", assembly.attestation),
        )
        if value is None
    )
    if missing:
        raise RuntimeError(
            "production assembly is missing required singleton components: " + ", ".join(missing)
        )
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


def build_production_assembly(context: ProductionAssemblyContext) -> ProductionRuntimeAssembly:
    from novel_agent.runtime.production_bootstrap import build_production_assembly as build

    return build(context)


__all__ = [
    "DEFAULT_PRODUCTION_ASSEMBLY_FACTORY",
    "ProductionAssemblyContext",
    "ProductionAssemblyFactory",
    "ProductionRuntimeAssembly",
    "build_production_assembly",
    "load_production_runtime_assembly",
    "validate_runtime_assembly",
]
