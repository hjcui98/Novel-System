"""Assemble the unique production graph from existing constructors."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.memory_write import (
    CommitServiceMemoryWriteAdapter,
    InformationBoundaryRegistryAdapter,
    LegacyGuardianPortAdapter,
    LegacyRiskClassifierAdapter,
    LegacyWriteGateAdapter,
    ProjectionServiceReadinessAdapter,
    RepositoryCanonicalReadAdapter,
    TeacherForcedCuratorPort,
)
from novel_agent.adapters.model import OpenAICompatibleChatEndpoint
from novel_agent.adapters.model.production_fake import ProductionChapterEndpoint
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.model_call_ledger import SqlModelCallLedger
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.adapters.runtime.chapter_settlement import (
    AtomicChapterSettlementAdapter,
    ChapterSettlementPolicy,
)
from novel_agent.adapters.runtime.materializers import (
    DraftCandidateMaterializer,
    PlanCandidateMaterializer,
)
from novel_agent.adapters.runtime.memory_maintenance import (
    MemoryMaintenanceAdapter,
    MemoryMaintenancePolicy,
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
from novel_agent.agents.candidate_observer import CandidateObservationAgent
from novel_agent.agents.curator import CuratorReplayAgent
from novel_agent.agents.curator_repair import CuratorRepairAgent
from novel_agent.agents.editor import EditorAgent, build_editor_contract_bundle
from novel_agent.agents.guardian import GuardianRiskReviewAgent
from novel_agent.agents.plan_reviewer import PlanReviewerAgent
from novel_agent.agents.planner import PlannerAgent, build_planner_contract_bundle
from novel_agent.agents.registry import AgentRegistry, seal_agent_spec, seal_tool_policy
from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.generation import WritingLengthPolicy, WritingLoopBudgets
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    SchemaVersion,
    StableId,
    TaskId,
    bounded_stable_id,
)
from novel_agent.domain.memory import ChannelHit, RetrievalChannel, Stage1MemoryNeed
from novel_agent.domain.memory_write import MemoryWriteBudget
from novel_agent.domain.model_calls import BudgetResolutionProfile, ModelCallPurpose, ModelRole
from novel_agent.domain.planning import PlanningBudgets
from novel_agent.domain.production_assembly import (
    ProductionAssemblySpec,
    ResolvedEndpointRevision,
    ResolvedProductionAssemblyAttestation,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentSpec,
    AgentType,
    ContextBudget,
    ContractRef,
    MemoryGatewayMode,
    MemoryGatewayPolicy,
    PromptContractRef,
    RetrievalBudget,
    SkillContractRef,
    ToolPermission,
    ToolPolicy,
)
from novel_agent.prompts.registry import PromptRegistry, PromptTemplate, content_hash
from novel_agent.runtime.creative_assembly import (
    DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    ProductionAssemblyContext,
    ProductionRuntimeAssembly,
)
from novel_agent.runtime.creative_dispatcher import CreativeDispatcher
from novel_agent.runtime.memory_controller import RouteBoundControllerPolicy
from novel_agent.runtime.production_components import (
    BoundPermissionResolver,
    BoundPolicyResolver,
    ExactSnapshotFreshnessCheck,
    ProductionCuratorModelRequestFactory,
    ProductionReactiveMemoryInputsFactory,
    ProductionStage2MWriterContext,
    ProductionWriterModelRequestFactory,
    ProposedTextRootLoader,
    SettlementTextReveal,
    utf8_quarter_token_count,
)
from novel_agent.services.agent_context import (
    AgentContextProjector,
    AgentContextRuntime,
    ContextCompactor,
    ContextWindowPolicy,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import content_id
from novel_agent.services.creative_runtime import CreativeRuntimeService
from novel_agent.services.editorial import EditorialService
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.evidence_first_writer_context_assembler import (
    EvidenceFirstWriterContextAssembler,
)
from novel_agent.services.information_boundary import InformationBoundaryPort
from novel_agent.services.memory_gateway import MemoryGateway
from novel_agent.services.memory_pipeline import AnchorBuilder, ContextCompiler, EvidenceExpander
from novel_agent.services.memory_write_validation import Stage2ValidationV2Adapter
from novel_agent.services.memory_write_workflow import (
    InMemoryCandidateLineageRepository,
    InMemoryCheckpointRepository,
    InMemoryQuarantineRepository,
    LocalMemoryWriteWorkflow,
)
from novel_agent.services.model_curation import ModelCurator
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.model_request_admission import ModelRequestAdmissionController
from novel_agent.services.need_evidence_semantic_judgment import NeedEvidenceSemanticJudge
from novel_agent.services.paired_controller import PairedMemoryControllerRunner
from novel_agent.services.planner_context_assembler import PlannerContextAssembler
from novel_agent.services.planner_context_runtime import SharedPlannerContextRuntime
from novel_agent.services.planning_context_loop import PlanningContextLoopService
from novel_agent.services.planning_inquiry_need_generation import (
    PlanningInquiryConditionedNeedGenerator,
)
from novel_agent.services.pre_candidate_repair import InMemoryCuratorProposalAttemptRepository
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedProjectionService,
    DerivedSnapshotRepository,
    ProjectionOutboxRepository,
    snapshot_id_for_commit,
)
from novel_agent.services.recent_prose import RecentProseAssembler
from novel_agent.services.replay import ExactReplayProjectionBuilder
from novel_agent.services.retrieval import InMemoryRetrievalBackend, RetrievalBackend
from novel_agent.services.runtime_acceptance import RuntimeAcceptanceService
from novel_agent.services.runtime_commands import RuntimeCommandService
from novel_agent.services.task_conditioned_need_generation import TaskPlanConditionedNeedGenerator
from novel_agent.services.writer_candidate import WriterCandidateMaterializer
from novel_agent.services.writer_change_reconciliation import WriterChangeReconciliationService
from novel_agent.services.writer_cognition import WriterCognitionService
from novel_agent.services.writer_context_loop import WriterContextLoopService
from novel_agent.services.writer_reactive_memory import WriterReactiveNeedAdapter
from novel_agent.skills.registry import SkillRegistry, SkillTemplate
from novel_agent.tools.retrieval import CHANNEL_BY_TOOL

DETERMINISTIC_FAKE_ENDPOINT_PROFILE = "deterministic_fake"
QWEN38_27B_FP8_8005_ENDPOINT_PROFILE = "qwen38_27b_fp8_8005"
QWEN38_27B_FP8_8005_BASE_URL = "http://127.0.0.1:8005/v1"
QWEN38_27B_FP8_MODEL = "qwen38-27b-fp8"


def resolve_registered_model_endpoints(
    profile: str | None,
) -> tuple[RegisteredModelEndpoint, ...]:
    """Resolve an explicit endpoint profile through the production registry.

    No profile means no endpoint is registered and production startup remains
    fail-closed.  The profiles are explicit deployment identities; adding the
    local real endpoint does not change the default or silently select a model.
    """

    if profile is None:
        return ()
    if profile == DETERMINISTIC_FAKE_ENDPOINT_PROFILE:
        return (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="deterministic-fake-production",
                model_name="production-fake-v1",
                revision="production-fake-v1",
                adapter=ProductionChapterEndpoint(),
                sequence_limit=131_072,
                output_limit=None,
                safety_allowance_tokens=256,
                estimated_reasoning_reserve=2_048,
                default_thinking=False,
                reasoning_included_in_completion_tokens=False,
                global_output_cap=131_072,
            ),
        )
    if profile == QWEN38_27B_FP8_8005_ENDPOINT_PROFILE:
        adapter = OpenAICompatibleChatEndpoint(
            base_url=QWEN38_27B_FP8_8005_BASE_URL,
            model=QWEN38_27B_FP8_MODEL,
            max_output_tokens=8_000,
            temperature=0.0,
            local_only=True,
            max_retries=0,
        )
        return (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="qwen38-27b-fp8@8005",
                model_name=QWEN38_27B_FP8_MODEL,
                revision=QWEN38_27B_FP8_MODEL,
                adapter=adapter,
                sequence_limit=131_072,
                output_limit=8_000,
                safety_allowance_tokens=1_000,
                estimated_reasoning_reserve=2_048,
                default_thinking=False,
                reasoning_included_in_completion_tokens=False,
                global_output_cap=131_072,
            ),
        )
    raise RuntimeError(f"unknown production endpoint profile: {profile}")


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = Path(__file__).with_name("production_assembly_spec.json")
_WRITER_SKILL_FILES = {
    "skill.scene-composition": "scene_composition_v1.md",
    "skill.continuation": "continuation_v1.md",
    "skill.major-rewrite": "major_rewrite_v1.md",
    "skill.character-voice-writing": "character_voice_writing_v1.md",
    "skill.dialogue-subtext-writing": "dialogue_subtext_writing_v1.md",
    "skill.pov-epistemic-writing": "pov_epistemic_writing_v1.md",
    "skill.pacing-transition-writing": "pacing_transition_writing_v1.md",
    "skill.hook-foreshadowing-writing": "hook_foreshadowing_writing_v1.md",
    "skill.style-genre-writing": "style_genre_writing_v1.md",
}
_ZERO = ArtifactId("sha256:" + "0" * 64)


def _type_identity(value: object) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def _endpoint_revision(endpoint: RegisteredModelEndpoint) -> str | None:
    revision = endpoint.revision
    if not revision:
        revision = getattr(endpoint.adapter, "revision", None)
    if not revision:
        revision = getattr(endpoint.adapter, "model_version", None)
    return str(revision) if revision else None


def _resolve_production_admission(
    context: ProductionAssemblyContext,
    spec: ProductionAssemblySpec,
) -> ModelRequestAdmissionController:
    admission = context.admission
    if admission is None:
        return ModelRequestAdmissionController(
            endpoint_request_limit=context.endpoint_request_limit,
            kv_token_budget=context.kv_token_budget,
            kv_safety_reserve_ratio=context.kv_safety_reserve_ratio,
            model_sequence_limit=spec.model_policy.sequence_limit,
            default_scheduling_timeout_seconds=context.scheduling_timeout_seconds,
        )
    endpoint_request_limit = cast(int, admission.snapshot()["endpoint_request_limit"])
    if endpoint_request_limit not in (1, 2):
        raise ValueError("production endpoint_request_limit must be 1 or 2")
    return admission


def _validate_endpoint_contracts(
    endpoints: tuple[RegisteredModelEndpoint, ...],
    spec: ProductionAssemblySpec,
) -> None:
    """Validate registered deployment facts before constructing model callers."""

    implementations = tuple(
        endpoint for endpoint in endpoints if endpoint.role is ModelRole.IMPLEMENTATION
    )
    if len(implementations) != 1:
        raise RuntimeError(
            "production assembly requires exactly one registered implementation endpoint"
        )
    for endpoint in endpoints:
        if not endpoint.endpoint_name or not endpoint.model_name:
            raise RuntimeError("registered model endpoint identity is incomplete")
        if not callable(getattr(endpoint.adapter, "generate", None)):
            raise RuntimeError(
                f"registered endpoint {endpoint.endpoint_name!r} has no model adapter"
            )
        if not isinstance(getattr(endpoint.adapter, "is_external", None), bool):
            raise RuntimeError(
                f"registered endpoint {endpoint.endpoint_name!r} has no externality identity"
            )
        if endpoint.sequence_limit != spec.model_policy.sequence_limit:
            raise RuntimeError(
                "registered endpoint sequence limit does not match the production assembly spec"
            )
        if _endpoint_revision(endpoint) is None:
            raise RuntimeError(
                f"registered endpoint {endpoint.endpoint_name!r} has no explicit model revision"
            )
        if endpoint.safety_allowance_tokens is not None and endpoint.safety_allowance_tokens < 0:
            raise RuntimeError("registered endpoint safety allowance cannot be negative")
        if endpoint.estimated_reasoning_reserve < 0:
            raise RuntimeError("registered endpoint reasoning reserve cannot be negative")
        effective_output_limit = endpoint.output_limit
        if effective_output_limit is None:
            effective_output_limit = spec.model_policy.default_output_limit
        if effective_output_limit < 1:
            raise RuntimeError("registered endpoint output limit must be positive")
        if endpoint.role is ModelRole.IMPLEMENTATION and (
            effective_output_limit != spec.model_policy.default_output_limit
        ):
            raise RuntimeError(
                "registered Writer output limit does not match the production assembly spec"
            )
        if endpoint.global_output_cap < effective_output_limit:
            raise RuntimeError(
                "registered endpoint global output cap is below its effective output limit"
            )


def _validate_prompt_skill_contracts(
    spec: ProductionAssemblySpec,
    *,
    prompt_ids: tuple[StableId, ...],
    skill_ids: tuple[StableId, ...],
) -> None:
    if not spec.expected_prompt_ids:
        raise RuntimeError("production assembly spec does not declare prompt identities")
    if not spec.expected_skill_ids:
        raise RuntimeError("production assembly spec does not declare skill identities")
    prompt_set = set(prompt_ids)
    skill_set = set(skill_ids)
    missing_prompts = tuple(
        item.root for item in spec.expected_prompt_ids if item not in prompt_set
    )
    missing_skills = tuple(item.root for item in spec.expected_skill_ids if item not in skill_set)
    if missing_prompts:
        raise RuntimeError(f"production prompt identity is not registered: {missing_prompts}")
    if missing_skills:
        raise RuntimeError(f"production skill identity is not registered: {missing_skills}")


def load_production_assembly_spec(path: Path | None = None) -> ProductionAssemblySpec:
    payload = json.loads((path or SPEC_PATH).read_text(encoding="utf-8"))
    payload["expected_prompt_ids"] = tuple(payload["expected_prompt_ids"])
    payload["expected_skill_ids"] = tuple(payload["expected_skill_ids"])
    return ProductionAssemblySpec.model_validate(payload)


def preflight_production_environment(
    context: ProductionAssemblyContext,
    spec: ProductionAssemblySpec,
    *,
    model_endpoints: tuple[RegisteredModelEndpoint, ...] = (),
    prompt_ids: tuple[StableId, ...] = (),
    skill_ids: tuple[StableId, ...] = (),
) -> str:
    if context.manifest.runtime_contract_version != spec.runtime_contract_version:
        raise RuntimeError("production manifest runtime contract does not match the assembly spec")
    if context.manifest.stage4_implementation_status != "INTEGRATED":
        raise RuntimeError("production preflight requires an integrated Stage 4 implementation")
    if not context.manifest.feature_admission.real_stage4_adapter:
        raise RuntimeError("production preflight requires an admitted real Stage 4 adapter")
    resolved_endpoints = model_endpoints or context.model_endpoints
    if not resolved_endpoints:
        raise RuntimeError("production preflight requires registered model endpoints")
    _validate_endpoint_contracts(resolved_endpoints, spec)
    if prompt_ids or skill_ids:
        _validate_prompt_skill_contracts(spec, prompt_ids=prompt_ids, skill_ids=skill_ids)
    engine = create_engine(context.database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            try:
                head = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception as error:
                raise RuntimeError("production preflight could not read migration head") from error
    finally:
        engine.dispose()
    if head != spec.expected_migration_head:
        raise RuntimeError(
            "production preflight migration head mismatch: "
            f"{head!r} != {spec.expected_migration_head!r}"
        )
    root = context.object_store_root
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".production-preflight-write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        raise RuntimeError("production preflight object root is not writable") from error
    return str(head)


def _default_writing_policy(spec: ProductionAssemblySpec) -> WritingRequestPolicy:
    fingerprint = content_id({"factory": spec.factory_locator, "kind": "writing-request-policy"})
    reserved = spec.model_policy.default_output_limit
    sequence = spec.model_policy.sequence_limit
    return WritingRequestPolicy(
        pov="third-person limited",
        narrative_person="third person limited",
        length_policy=WritingLengthPolicy(
            minimum_characters=500,
            target_characters=1_500,
            maximum_characters=3_000,
        ),
        allowed_skills=spec.expected_skill_ids,
        budgets=WritingLoopBudgets(
            context_sequence_limit=sequence,
            reserved_output_tokens=reserved,
            context_safety_allowance_tokens=1_000,
            context_soft_limit_tokens=max(1, sequence - reserved - 1_000),
        ),
        writer_configuration_fingerprint=fingerprint,
        model_configuration_fingerprint=fingerprint,
        future_isolation_configuration_fingerprint=fingerprint,
    )


def _default_stage4_policy(spec: ProductionAssemblySpec) -> Stage4InvocationPolicy:
    fingerprint = content_id({"factory": spec.factory_locator, "kind": "stage4-policy"})
    return Stage4InvocationPolicy(
        budgets=PlanningBudgets(
            retrieval=RetrievalBudget(),
            context=ContextBudget(token_budget=12_000),
        ),
        configuration_fingerprint=fingerprint,
        model_fingerprint=fingerprint,
        allowed_skill_ids=spec.expected_skill_ids,
        model_max_output_tokens=spec.model_policy.default_output_limit,
    )


def _default_settlement_policy(
    spec: ProductionAssemblySpec,
    *,
    timeout_seconds: float | None = None,
    output_tokens: int | None = None,
    token_budget: int | None = None,
    max_total_model_calls: int | None = None,
) -> ChapterSettlementPolicy:
    if output_tokens is not None and not 1 <= output_tokens <= spec.model_policy.sequence_limit:
        raise ValueError(
            "settlement output tokens must be between one and the model sequence limit"
        )
    budget = MemoryWriteBudget()
    fingerprint_payload: dict[str, object] = {
        "factory": spec.factory_locator,
        "kind": "settlement-policy",
    }
    if timeout_seconds is not None:
        budget = MemoryWriteBudget.model_validate(
            {
                **budget.model_dump(mode="python"),
                "model_transport": {
                    **budget.model_transport.model_dump(mode="python"),
                    "timeout_seconds": timeout_seconds,
                },
            },
            strict=True,
        )
        fingerprint_payload["model_transport_timeout_seconds"] = (
            budget.model_transport.timeout_seconds
        )
    if output_tokens is not None:
        fingerprint_payload["model_transport_output_tokens"] = output_tokens
    if token_budget is not None:
        if token_budget < 0:
            raise ValueError("settlement token budget must not be negative")
        budget = budget.model_copy(update={"token_budget": token_budget})
        fingerprint_payload["token_budget"] = token_budget
    if max_total_model_calls is not None:
        if max_total_model_calls < 1:
            raise ValueError("settlement max total model calls must be positive")
        budget = budget.model_copy(update={"max_total_model_calls": max_total_model_calls})
        fingerprint_payload["max_total_model_calls"] = max_total_model_calls
    fingerprint = content_id(fingerprint_payload)
    contract = ContractRef(
        contract_id=StableId("contract.production-chapter-settlement"),
        version=spec.spec_version,
        content_hash=fingerprint,
    )
    return ChapterSettlementPolicy(
        curator_agent_spec=contract,
        boundary_policy_ref=contract,
        tool_policy_ref=contract,
        repair_policy_ref=contract,
        configuration_fingerprint=fingerprint,
        budget=budget,
    )


def _writer_skill_registry() -> SkillRegistry:
    contracts = WriterCognitionService.skill_contracts(PACKAGE_ROOT)
    templates = []
    for contract in contracts:
        filename = _WRITER_SKILL_FILES.get(contract.contract_id.root)
        if filename is None:
            continue
        templates.append(
            SkillTemplate(
                skill_id=contract.contract_id,
                version=contract.version,
                path=PACKAGE_ROOT / "skills" / filename,
                expected_hash=contract.content_hash,
            )
        )
    if not templates:
        raise RuntimeError("production Writer Skill registry is empty")
    return SkillRegistry(templates)


def _curator_contract_versions(schema_version: SchemaVersion) -> tuple[SchemaVersion, ...]:
    # TeacherForced looks up agent contracts by RootManifest.schema_version.
    # Stage 1 canon is 0.1.0 while the production runtime contract is 1.0.0.
    canon = SchemaVersion("0.1.0")
    if schema_version == canon:
        return (schema_version,)
    return (schema_version, canon)


def _curator_runner(gateway: ModelGateway, schema_version: SchemaVersion) -> StructuredAgentRunner:
    prompt_root = PACKAGE_ROOT / "prompts"
    skill_root = PACKAGE_ROOT / "skills"
    system_path = prompt_root / "system_policy_v1.md"
    replay_path = prompt_root / "curator_replay_v1.md"
    repair_path = prompt_root / "curator_repair_v1.md"
    guardian_path = prompt_root / "guardian_risk_review_v1.md"
    skill_path = skill_root / "memory_delta_extraction_v1.md"
    guardian_skill_path = skill_root / "memory_risk_review_v1.md"
    system_digest = content_hash(system_path.read_bytes())
    replay_digest = content_hash(replay_path.read_bytes())
    repair_digest = content_hash(repair_path.read_bytes())
    guardian_digest = content_hash(guardian_path.read_bytes())
    skill_digest = content_hash(skill_path.read_bytes())
    guardian_skill_digest = content_hash(guardian_skill_path.read_bytes())
    specs: list[AgentSpec] = []
    prompts: list[PromptTemplate] = []
    skills: list[SkillTemplate] = []
    for version in _curator_contract_versions(schema_version):
        system_prompt = PromptContractRef(
            contract_id=StableId("prompt.system-policy"),
            version=version,
            content_hash=system_digest,
            render_fingerprint=system_digest,
        )
        replay_prompt = PromptContractRef(
            contract_id=StableId("prompt.curator-replay"),
            version=version,
            content_hash=replay_digest,
            render_fingerprint=replay_digest,
        )
        repair_prompt = PromptContractRef(
            contract_id=StableId("prompt.curator-repair"),
            version=version,
            content_hash=repair_digest,
            render_fingerprint=repair_digest,
        )
        guardian_prompt = PromptContractRef(
            contract_id=StableId("prompt.guardian-risk-review"),
            version=version,
            content_hash=guardian_digest,
            render_fingerprint=guardian_digest,
        )
        skill_ref = SkillContractRef(
            contract_id=StableId("skill.memory-delta-extraction"),
            version=version,
            content_hash=skill_digest,
        )
        guardian_skill_ref = SkillContractRef(
            contract_id=StableId("skill.memory-risk-review"),
            version=version,
            content_hash=guardian_skill_digest,
        )
        schema = ContractRef(
            contract_id=StableId("schema.chapter-change-draft"),
            version=version,
            content_hash=content_id({"schema": "ChapterChangeDraft", "version": version.root}),
        )
        replay_policy = seal_tool_policy(
            ToolPolicy(
                policy_id=StableId("policy.curator-replay"),
                version=version,
                content_hash=_ZERO,
                allowed_tools=(),
                permission=ToolPermission.READ,
                max_tool_calls=0,
            )
        )
        repair_policy = seal_tool_policy(
            ToolPolicy(
                policy_id=StableId("policy.curator-repair"),
                version=version,
                content_hash=_ZERO,
                allowed_tools=(),
                permission=ToolPermission.READ,
                max_tool_calls=0,
            )
        )
        guardian_policy = seal_tool_policy(
            ToolPolicy(
                policy_id=StableId("policy.memory-guardian-risk-review"),
                version=version,
                content_hash=_ZERO,
                allowed_tools=(),
                permission=ToolPermission.READ,
                max_tool_calls=0,
            )
        )
        specs.append(
            seal_agent_spec(
                AgentSpec(
                    agent_id=StableId("agent.memory-curator.replay"),
                    agent_type=AgentType.MEMORY_CURATOR,
                    mode=AgentMode.REPLAY,
                    version=version,
                    content_hash=_ZERO,
                    input_schema=schema,
                    output_schema=schema,
                    system_prompt=system_prompt,
                    task_prompt=replay_prompt,
                    skills=(skill_ref,),
                    tool_policy=replay_policy,
                )
            )
        )
        specs.append(
            seal_agent_spec(
                AgentSpec(
                    agent_id=StableId("agent.memory-curator.repair"),
                    agent_type=AgentType.MEMORY_CURATOR,
                    mode=AgentMode.CURATOR_REPAIR,
                    version=version,
                    content_hash=_ZERO,
                    input_schema=schema,
                    output_schema=schema,
                    system_prompt=system_prompt,
                    task_prompt=repair_prompt,
                    skills=(skill_ref,),
                    tool_policy=repair_policy,
                )
            )
        )
        guardian_schema = ContractRef(
            contract_id=StableId("schema.guardian-decision-draft"),
            version=version,
            content_hash=content_id({"schema": "guardian-decision-draft", "version": version.root}),
        )
        specs.append(
            seal_agent_spec(
                AgentSpec(
                    agent_id=StableId("agent.memory-guardian.risk-review"),
                    agent_type=AgentType.MEMORY_GUARDIAN,
                    mode=AgentMode.RISK_REVIEW,
                    version=version,
                    content_hash=_ZERO,
                    input_schema=guardian_schema,
                    output_schema=guardian_schema,
                    system_prompt=system_prompt,
                    task_prompt=guardian_prompt,
                    skills=(guardian_skill_ref,),
                    tool_policy=guardian_policy,
                )
            )
        )
        prompts.extend(
            (
                PromptTemplate(system_prompt.contract_id, version, system_path, system_digest),
                PromptTemplate(replay_prompt.contract_id, version, replay_path, replay_digest),
                PromptTemplate(repair_prompt.contract_id, version, repair_path, repair_digest),
                PromptTemplate(
                    guardian_prompt.contract_id, version, guardian_path, guardian_digest
                ),
            )
        )
        skills.extend(
            (
                SkillTemplate(skill_ref.contract_id, version, skill_path, skill_digest),
                SkillTemplate(
                    guardian_skill_ref.contract_id,
                    version,
                    guardian_skill_path,
                    guardian_skill_digest,
                ),
            )
        )
    return StructuredAgentRunner(
        gateway,
        AgentRegistry(specs),
        PromptRegistry(prompts),
        SkillRegistry(skills),
    )


def _named_model_endpoints(
    endpoints: tuple[RegisteredModelEndpoint, ...],
    spec: ProductionAssemblySpec,
) -> tuple[RegisteredModelEndpoint, ...]:
    """Fill missing output limits from the assembly spec so STRICT can resolve."""

    named: list[RegisteredModelEndpoint] = []
    for endpoint in endpoints:
        if endpoint.sequence_limit != spec.model_policy.sequence_limit:
            raise RuntimeError(
                "registered endpoint sequence limit does not match the production assembly spec"
            )
        if endpoint.output_limit is not None:
            named.append(endpoint)
            continue
        named.append(
            replace(
                endpoint,
                output_limit=spec.model_policy.default_output_limit,
            )
        )
    resolved = tuple(named)
    _validate_endpoint_contracts(resolved, spec)
    return resolved


def _assert_spec_identities(
    spec: ProductionAssemblySpec, assembly: ProductionRuntimeAssembly
) -> None:
    observed = {
        spec.expected_planner_adapter: _type_identity(assembly.planner),
        spec.expected_writer_adapter: _type_identity(assembly.writer),
        spec.expected_plan_materializer: _type_identity(assembly.plan_materializer),
        spec.expected_draft_materializer: _type_identity(assembly.draft_materializer),
        spec.expected_chapter_settlement: _type_identity(assembly.chapter_settlement),
        spec.expected_memory_maintenance: _type_identity(assembly.memory_maintenance),
    }
    for expected, actual in observed.items():
        if expected != actual:
            raise RuntimeError(
                f"production adapter identity mismatch: expected {expected}, got {actual}"
            )
    if spec.factory_locator != DEFAULT_PRODUCTION_ASSEMBLY_FACTORY:
        raise RuntimeError("production spec factory locator is not the repo-owned factory")


class _CommitScopedRetrievalBackend:
    """Resolve smoke retrieval units from the commit named by each Memory Need."""

    def __init__(self, commits: CommitService, artifacts: ArtifactRepository) -> None:
        self._loader = ArtifactProjectionSourceLoader(commits, artifacts)
        self._backends: dict[str, InMemoryRetrievalBackend] = {}

    def search(
        self,
        need: Stage1MemoryNeed,
        channel: RetrievalChannel,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        return self._backend_for(need.base_commit).search(need, channel, limit)

    def validate_current(self, project_id: ProjectId, commits: CommitService) -> None:
        self._backend_for(commits.current_commit(project_id))

    def _backend_for(self, commit: CommitId) -> InMemoryRetrievalBackend:
        cached = self._backends.get(commit.root)
        if cached is not None:
            return cached
        source = self._loader.load(commit)
        units = AnchorBuilder().build(
            source.world,
            source.text,
            source.plan,
            snapshot_id=snapshot_id_for_commit(commit),
            canonical_commit=commit,
        )
        if not units:
            raise RuntimeError("production assembly canonical basis produced no retrieval units")
        backend = InMemoryRetrievalBackend(units)
        self._backends[commit.root] = backend
        return backend


def _default_retrieval_backend(
    *,
    context: ProductionAssemblyContext,
    commits: CommitService,
    artifacts: ArtifactRepository,
) -> RetrievalBackend:
    """Build the deterministic smoke backend from the durable canonical basis.

    The normal production entry point does not receive a test fixture's retrieval
    units. Deriving them from the current committed roots keeps the fake endpoint
    on the same production assembly path while preserving the explicit backend
    injection seam for real deployments.
    """

    backend = _CommitScopedRetrievalBackend(commits, artifacts)
    try:
        backend.validate_current(context.project_id, commits)
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        raise RuntimeError(
            "production assembly requires an initialized project with readable canonical roots"
        ) from error
    return backend


def freeze_production_attestation(
    *,
    spec: ProductionAssemblySpec,
    context: ProductionAssemblyContext,
    assembly: ProductionRuntimeAssembly,
    session_factory: sessionmaker[Session],
    model_gateway: ModelGateway,
    memory_gateway: MemoryGateway,
    endpoints: tuple[RegisteredModelEndpoint, ...],
    retrieval_backend: RetrievalBackend,
    projection_builder: object,
    sequence_limit: int,
    output_limit: int,
    prompt_pins: tuple[ArtifactId, ...],
    skill_pins: tuple[ArtifactId, ...],
    reranker_resolved: bool,
    migration_head: str,
    settlement_policy_fingerprint: ArtifactId,
) -> ResolvedProductionAssemblyAttestation:
    if not prompt_pins:
        raise RuntimeError("production attestation requires prompt pins")
    if not skill_pins:
        raise RuntimeError("production attestation requires skill pins")
    if assembly.memory_maintenance is None:
        raise RuntimeError("production attestation requires Memory Maintenance")
    if any(pin == _ZERO for pin in (*prompt_pins, *skill_pins)):
        raise RuntimeError("production attestation contains an empty prompt or skill pin")
    if any(_endpoint_revision(endpoint) is None for endpoint in endpoints):
        raise RuntimeError("production attestation requires endpoint revisions")
    writer_limits = model_gateway.endpoint_budget_limits(ModelRole.IMPLEMENTATION)
    if writer_limits.sequence_limit != sequence_limit:
        raise RuntimeError(
            "attestation sequence limit does not match the registered Writer endpoint"
        )
    if writer_limits.output_limit is None:
        raise RuntimeError("attestation requires a registered Writer output limit")
    if writer_limits.output_limit != output_limit:
        raise RuntimeError("attestation output limit does not match the registered Writer endpoint")
    admission = model_gateway.admission_controller
    if admission is None:
        raise RuntimeError("production attestation requires endpoint admission")
    admission_snapshot = admission.snapshot()
    endpoint_request_limit = cast(int, admission_snapshot["endpoint_request_limit"])
    if endpoint_request_limit not in (1, 2):
        raise RuntimeError("production attestation requires endpoint_request_limit 1 or 2")
    configured_kv_token_budget = cast(int | None, admission_snapshot["configured_kv_token_budget"])
    effective_kv_token_budget = cast(int | None, admission_snapshot["effective_kv_token_budget"])
    kv_safety_reserve_ratio = cast(float, admission_snapshot["kv_safety_reserve_ratio"])
    scheduling_timeout_seconds = cast(
        float, admission_snapshot["default_scheduling_timeout_seconds"]
    )
    fingerprint = content_id(
        {
            "factory": spec.factory_locator,
            "migration": migration_head,
            "planner": _type_identity(assembly.planner),
            "writer": _type_identity(assembly.writer),
            "gateway": _type_identity(model_gateway),
            "memory": _type_identity(memory_gateway),
            "maintenance": _type_identity(assembly.memory_maintenance),
            "backend": _type_identity(retrieval_backend),
            "projection": _type_identity(projection_builder),
            "session": id(session_factory),
            "settlement_policy": settlement_policy_fingerprint.root,
            "memory_write_validation_only": context.memory_write_validation_only,
            "admission": {
                "endpoint_request_limit": endpoint_request_limit,
                "configured_kv_token_budget": configured_kv_token_budget,
                "effective_kv_token_budget": effective_kv_token_budget,
                "kv_safety_reserve_ratio": kv_safety_reserve_ratio,
                "scheduling_timeout_seconds": scheduling_timeout_seconds,
            },
        }
    )
    return ResolvedProductionAssemblyAttestation(
        spec_version=spec.spec_version,
        factory_locator=spec.factory_locator,
        migration_head=migration_head,
        object_store_root=str(context.object_store_root),
        session_factory_identity=str(id(session_factory)),
        planner_adapter=_type_identity(assembly.planner),
        writer_adapter=_type_identity(assembly.writer),
        plan_materializer=_type_identity(assembly.plan_materializer),
        draft_materializer=_type_identity(assembly.draft_materializer),
        chapter_settlement=_type_identity(assembly.chapter_settlement),
        writing_request_factory=_type_identity(assembly.writing_request_factory),
        planner_invocation_factory=_type_identity(assembly.planner_invocation_factory),
        model_gateway=_type_identity(model_gateway),
        memory_gateway=_type_identity(memory_gateway),
        memory_maintenance=_type_identity(assembly.memory_maintenance),
        projection_builder=_type_identity(projection_builder),
        retrieval_backend=_type_identity(retrieval_backend),
        endpoints=tuple(
            ResolvedEndpointRevision(
                role=endpoint.role.value,
                endpoint_name=endpoint.endpoint_name,
                model_name=endpoint.model_name,
                revision=_endpoint_revision(endpoint),
                adapter_identity=_type_identity(endpoint.adapter),
                is_external=bool(endpoint.adapter.is_external),
            )
            for endpoint in endpoints
        ),
        sequence_limit=sequence_limit,
        output_limit=output_limit,
        reasoning_billing_mode=spec.model_policy.reasoning_billing_mode,
        reasoning_included_in_completion_tokens=(
            writer_limits.reasoning_included_in_completion_tokens
        ),
        estimated_reasoning_reserve=writer_limits.estimated_reasoning_reserve,
        safety_allowance_tokens=writer_limits.safety_allowance_tokens,
        global_output_cap=writer_limits.global_output_cap,
        endpoint_request_limit=endpoint_request_limit,
        configured_kv_token_budget=configured_kv_token_budget,
        effective_kv_token_budget=effective_kv_token_budget,
        kv_safety_reserve_ratio=kv_safety_reserve_ratio,
        scheduling_timeout_seconds=scheduling_timeout_seconds,
        prompt_pins=prompt_pins,
        skill_pins=skill_pins,
        reranker_declared=spec.reranker_required,
        reranker_resolved=reranker_resolved,
        configuration_fingerprint=fingerprint,
    )


def build_production_assembly(context: ProductionAssemblyContext) -> ProductionRuntimeAssembly:
    spec = context.spec or load_production_assembly_spec()
    if context.manifest.runtime_contract_version != spec.runtime_contract_version:
        raise RuntimeError("production manifest runtime contract does not match the assembly spec")
    if not context.manifest.feature_admission.real_stage4_adapter:
        raise RuntimeError("production assembly requires an admitted real Stage 4 adapter")
    if spec.reranker_required and context.reranker is None:
        raise RuntimeError("production spec requires a resolved reranker")
    if not context.model_endpoints:
        raise RuntimeError("production assembly requires registered model endpoints")
    model_endpoints = _named_model_endpoints(context.model_endpoints, spec)
    schema_version = context.schema_version or SchemaVersion("1.0.0")
    planner_bundle = build_planner_contract_bundle(
        package_root=PACKAGE_ROOT, version=schema_version
    )
    editor_bundle = build_editor_contract_bundle(PACKAGE_ROOT)
    writer_skill_contracts = WriterCognitionService.skill_contracts(PACKAGE_ROOT)
    prompt_ids = tuple(
        dict.fromkeys(
            (
                *(spec_.system_prompt.contract_id for spec_ in planner_bundle.agent_specs),
                *(spec_.task_prompt.contract_id for spec_ in planner_bundle.agent_specs),
                *(spec_.system_prompt.contract_id for spec_ in editor_bundle.agent_specs),
                *(spec_.task_prompt.contract_id for spec_ in editor_bundle.agent_specs),
            )
        )
    )
    skill_ids = tuple(
        dict.fromkeys(
            (
                *(
                    skill.contract_id
                    for spec_ in planner_bundle.agent_specs
                    for skill in spec_.skills
                ),
                *(
                    skill.contract_id
                    for spec_ in editor_bundle.agent_specs
                    for skill in spec_.skills
                ),
                *(skill.contract_id for skill in writer_skill_contracts),
            )
        )
    )
    migration_head = preflight_production_environment(
        context,
        spec,
        model_endpoints=model_endpoints,
        prompt_ids=prompt_ids,
        skill_ids=skill_ids,
    )
    writing_policy = context.writing_policy or _default_writing_policy(spec)
    if context.max_major_rewrites is not None or context.max_local_repairs is not None:
        max_major_rewrites = (
            writing_policy.budgets.max_major_rewrites
            if context.max_major_rewrites is None
            else context.max_major_rewrites
        )
        max_local_repairs = (
            writing_policy.budgets.max_local_repairs
            if context.max_local_repairs is None
            else context.max_local_repairs
        )
        if not 0 <= max_major_rewrites <= 2:
            raise ValueError("max_major_rewrites override must be between zero and two")
        if not 0 <= max_local_repairs <= 2:
            raise ValueError("max_local_repairs override must be between zero and two")
        max_post_draft_model_calls = max(
            writing_policy.budgets.max_post_draft_model_calls,
            (2 * max_major_rewrites) + 3,
            (3 * max_local_repairs) + 3,
        )
        override_fingerprint = content_id(
            {
                "factory": spec.factory_locator,
                "kind": "writing-request-policy",
                "max_major_rewrites": max_major_rewrites,
                "max_local_repairs": max_local_repairs,
                "max_post_draft_model_calls": max_post_draft_model_calls,
            }
        )
        writing_policy = replace(
            writing_policy,
            budgets=writing_policy.budgets.model_copy(
                update={
                    "max_major_rewrites": max_major_rewrites,
                    "max_local_repairs": max_local_repairs,
                    "max_post_draft_model_calls": max_post_draft_model_calls,
                }
            ),
            writer_configuration_fingerprint=override_fingerprint,
            model_configuration_fingerprint=override_fingerprint,
            future_isolation_configuration_fingerprint=override_fingerprint,
        )
    stage4_policy = context.stage4_policy or _default_stage4_policy(spec)
    if context.settlement_policy is not None and (
        context.settlement_timeout_seconds is not None
        or context.settlement_output_tokens is not None
        or context.settlement_token_budget is not None
        or context.settlement_max_total_model_calls is not None
    ):
        raise ValueError(
            "settlement overrides cannot be combined with an explicit settlement_policy"
        )
    settlement_policy = context.settlement_policy or _default_settlement_policy(
        spec,
        timeout_seconds=context.settlement_timeout_seconds,
        output_tokens=context.settlement_output_tokens,
        token_budget=context.settlement_token_budget,
        max_total_model_calls=context.settlement_max_total_model_calls,
    )
    settlement_output_tokens = (
        context.settlement_output_tokens
        if context.settlement_output_tokens is not None
        else spec.model_policy.default_output_limit
    )
    admission = _resolve_production_admission(context, spec)
    session_factory = build_session_factory(build_engine(context.database_url))
    artifacts = ArtifactRepository(FilesystemObjectStore(context.object_store_root))
    events = RunEventLogRepository(session_factory)
    checkpoints = RunCheckpointRepository(session_factory)
    commits = CommitService(session_factory)
    commands = RuntimeCommandService(
        session_factory,
        events,
        BoundPermissionResolver(context.policy.permission_hash),
        artifacts=artifacts,
    )
    task_reader = RuntimeTaskQueryRepository(session_factory)
    snapshots = DerivedSnapshotRepository(session_factory)
    projection_builder = context.projection_builder or ExactReplayProjectionBuilder()
    projections = DerivedProjectionService(
        ProjectionOutboxRepository(session_factory), projection_builder
    )
    retrieval_backend: RetrievalBackend = context.retrieval_backend or _default_retrieval_backend(
        context=context,
        commits=commits,
        artifacts=artifacts,
    )
    comparison_fingerprint = content_id(
        {
            "factory": spec.factory_locator,
            "backend": _type_identity(retrieval_backend),
            "reranker": context.reranker is not None,
        }
    )
    tool_policy = seal_tool_policy(
        ToolPolicy(
            policy_id=StableId("policy.production-memory-gateway"),
            version=schema_version,
            content_hash=_ZERO,
            allowed_tools=tuple(sorted(CHANNEL_BY_TOOL)),
            max_rounds=2,
            max_tool_calls=12,
            max_query_rewrites_per_need=0,
            wall_clock_budget_ms=120_000,
            token_budget=12_000,
        )
    )
    model_gateway = ModelGateway(
        model_endpoints,
        forbid_external_calls=all(not endpoint.adapter.is_external for endpoint in model_endpoints),
        admission_controller=admission,
        call_ledger=SqlModelCallLedger(session_factory),
        raw_artifacts=artifacts,
        raw_artifact_schema_version=schema_version,
        scheduling_timeout_seconds=admission.default_scheduling_timeout_seconds,
        budget_profile=BudgetResolutionProfile.STRICT,
    )
    batch_endpoint = next(
        (endpoint for endpoint in model_endpoints if endpoint.role is ModelRole.BATCH_TEST),
        None,
    )
    semantic_judge = (
        NeedEvidenceSemanticJudge(
            model_gateway,
            max_input_tokens=12_000,
            max_output_tokens=batch_endpoint.output_limit or 2_048,
        )
        if batch_endpoint is not None
        else None
    )
    memory_gateway = MemoryGateway(
        PairedMemoryControllerRunner.from_shared_backend(
            backend=retrieval_backend,
            needs=(),
            tool_policy=tool_policy,
            compiler=ContextCompiler(EvidenceExpander()),
            controller_policy=RouteBoundControllerPolicy(),
            freshness_check=ExactSnapshotFreshnessCheck(snapshots),
            checkpointer=InMemorySaver(),
            comparison_basis_fingerprint=comparison_fingerprint,
            reranker=context.reranker,
        ),
        MemoryGatewayPolicy(
            policy_id=StableId("policy.production-memory-gateway"),
            mode=MemoryGatewayMode.DETERMINISTIC,
            configuration_fingerprint=comparison_fingerprint,
        ),
        artifacts,
        schema_version=schema_version,
        semantic_judge=semantic_judge,
    )
    planner_runner = StructuredAgentRunner(
        model_gateway, planner_bundle.agents, planner_bundle.prompts, planner_bundle.skills
    )
    projector = AgentContextProjector(utf8_quarter_token_count)
    agent_runtime = AgentContextRuntime(projector, artifacts, events, checkpoints, schema_version)
    reserved = spec.model_policy.default_output_limit
    sequence = spec.model_policy.sequence_limit
    window = ContextWindowPolicy(
        sequence_limit=sequence,
        reserved_output_tokens=reserved,
        safety_allowance_tokens=1_000,
        soft_limit_tokens=max(1, sequence - reserved - 1_000),
        tokenizer="deterministic_unicode",
        tokenizer_version="v1",
    )
    compactor = ContextCompactor(projector, artifacts, schema_version, utf8_quarter_token_count)
    planner_invocation_factory = ProductionStage4InvocationFactory(
        commits=commits,
        artifacts=artifacts,
        policy=stage4_policy,
        model_request_namespace=context.model_request_namespace,
    )
    planner = Stage4PlanningLeafAdapter(
        PlanningContextLoopService(
            planner=PlannerAgent(planner_runner, artifacts),
            reviewer=PlanReviewerAgent(planner_runner, artifacts),
            need_generator=PlanningInquiryConditionedNeedGenerator(),
            memory_gateway=memory_gateway,
            context_assembler=PlannerContextAssembler(artifacts, schema_version=schema_version),
            context_runtime=SharedPlannerContextRuntime(
                projector=projector,
                runtime=agent_runtime,
                compactor=compactor,
                artifacts=artifacts,
                schema_version=schema_version,
                policy=window,
            ),
            artifacts=artifacts,
            schema_version=schema_version,
        ),
        artifacts,
        planner_invocation_factory,
        schema_version=schema_version,
    )
    editor = EditorialService(
        EditorAgent(
            StructuredAgentRunner(
                model_gateway,
                AgentRegistry(editor_bundle.agent_specs),
                PromptRegistry(editor_bundle.prompt_templates),
                SkillRegistry(editor_bundle.skill_templates),
            )
        ),
        artifacts,
        schema_version,
    )
    writer = Stage3WritingLeafAdapter(
        WriterContextLoopService(
            projector,
            compactor,
            agent_runtime,
            WriterCognitionService(
                model_gateway,
                artifacts,
                _writer_skill_registry(),
                schema_version=schema_version,
                package_root=PACKAGE_ROOT,
                require_admission=spec.model_policy.require_admission,
            ),
            WriterReactiveNeedAdapter(
                memory_gateway,
                artifacts,
                utf8_quarter_token_count,
                schema_version=schema_version,
            ),
            WriterCandidateMaterializer(artifacts, schema_version),
            editor,
            CandidateObservationAgent(
                model_gateway,
                artifacts,
                schema_version=schema_version,
                package_root=PACKAGE_ROOT,
                require_admission=spec.model_policy.require_admission,
            ),
            WriterChangeReconciliationService(),
            artifacts,
            events,
        ),
        ProductionWriterModelRequestFactory(
            role=ModelRole.IMPLEMENTATION,
            purpose=ModelCallPurpose.DEVELOPMENT,
            max_output_tokens=spec.model_policy.default_output_limit,
            timeout_seconds=120.0,
        ),
        ProductionReactiveMemoryInputsFactory(commits, artifacts),
    )
    writing_request_factory = ProductionWritingRequestFactory(
        commits=commits,
        artifacts=artifacts,
        recent_prose=RecentProseAssembler(artifacts, schema_version),
        writer_context=ProductionStage2MWriterContext(
            generator=TaskPlanConditionedNeedGenerator(
                planner_max_output_tokens=spec.model_policy.default_output_limit,
                planner_max_input_tokens=stage4_policy.budgets.context.token_budget,
            ),
            gateway=memory_gateway,
            assembler=EvidenceFirstWriterContextAssembler(token_counter=utf8_quarter_token_count),
            artifacts=artifacts,
        ),
        policy=writing_policy,
        schema_version=schema_version,
    )
    plan_materializer = PlanCandidateMaterializer(artifacts, commits, schema_version=schema_version)
    draft_materializer = DraftCandidateMaterializer(
        artifacts, commits, schema_version=schema_version
    )
    curator_model = ModelCurator(
        model_gateway,
        enforce_support_gate=True,
        enable_model_semantic_verifier=True,
    )
    # Graph repair is a distinct Curator owner.  Keep a separate ModelCurator
    # instance so the graph profile's request-local evidence/coverage receipts
    # cannot race with the ordinary Curator instance while both profiles run
    # concurrently for one proposal.
    graph_curator_model = ModelCurator(
        model_gateway,
        enforce_support_gate=True,
        enable_model_semantic_verifier=True,
    )
    curator_runner = _curator_runner(model_gateway, schema_version)
    curator = TeacherForcedCuratorPort(
        CuratorReplayAgent(curator_model, curator_runner),
        CuratorRepairAgent(curator_model, curator_runner),
        artifacts,
        ProductionCuratorModelRequestFactory(
            run_id=context.run_id,
            task_id=TaskId(
                bounded_stable_id(
                    f"{context.run_id.root}.settlement",
                    f"settlement.{context.run_id.root}",
                    "settlement",
                ).root
            ),
            max_output_tokens=settlement_output_tokens,
            timeout_seconds=settlement_policy.budget.model_transport.timeout_seconds,
        ),
        graph_curator=graph_curator_model,
    )
    guardian = LegacyGuardianPortAdapter(
        GuardianRiskReviewAgent(curator_runner, artifacts),
        artifacts,
        ProductionCuratorModelRequestFactory(
            run_id=context.run_id,
            task_id=TaskId(
                bounded_stable_id(
                    f"{context.run_id.root}.maintenance",
                    f"maintenance.{context.run_id.root}",
                    "maintenance",
                ).root
            ),
            max_output_tokens=settlement_output_tokens,
            timeout_seconds=settlement_policy.budget.model_transport.timeout_seconds,
            request_namespace="guardian",
        ),
    )
    boundary = InformationBoundaryPort(
        artifact_reader=artifacts,
        trusted_policy_hashes=(settlement_policy.configuration_fingerprint,),
    )
    lineage = InMemoryCandidateLineageRepository()
    checkpoint = InMemoryCheckpointRepository(artifacts)
    quarantine = InMemoryQuarantineRepository(artifacts)
    proposal_attempts = InMemoryCuratorProposalAttemptRepository(artifacts)
    memory_write_workflow = LocalMemoryWriteWorkflow(
        canonical_read=RepositoryCanonicalReadAdapter(commits, artifacts),
        curator=curator,
        validator=Stage2ValidationV2Adapter(proposed_text_loader=ProposedTextRootLoader(artifacts)),
        risk_classifier=LegacyRiskClassifierAdapter(artifacts),
        guardian=guardian,
        write_gate=LegacyWriteGateAdapter(artifacts),
        commit=CommitServiceMemoryWriteAdapter(commits, artifacts),
        information_boundary=boundary,
        artifacts=artifacts,
        lineage=lineage,
        checkpoint=checkpoint,
        quarantine=quarantine,
        events=events,
        projection=ProjectionServiceReadinessAdapter(projections, snapshots, artifacts),
        proposal_attempts=proposal_attempts,
    )
    chapter_settlement = AtomicChapterSettlementAdapter(
        workflow=memory_write_workflow,
        draft_materializer=draft_materializer,
        commits=commits,
        artifacts=artifacts,
        boundary_registry=InformationBoundaryRegistryAdapter(boundary, artifacts),
        policy=settlement_policy,
        reveal_text=SettlementTextReveal(curator),
    )
    memory_maintenance = MemoryMaintenanceAdapter(
        workflow=memory_write_workflow,
        commits=commits,
        artifacts=artifacts,
        policy=MemoryMaintenancePolicy(
            curator_agent_spec=settlement_policy.curator_agent_spec,
            tool_policy_ref=settlement_policy.tool_policy_ref,
            repair_policy_ref=settlement_policy.repair_policy_ref,
            configuration_fingerprint=settlement_policy.configuration_fingerprint,
            boundary_policy_ref=settlement_policy.boundary_policy_ref,
            prompt_contract_refs=settlement_policy.prompt_contract_refs,
            skill_contract_refs=settlement_policy.skill_contract_refs,
            budget=settlement_policy.budget,
            validation_only=context.memory_write_validation_only,
        ),
    )
    creative = CreativeRuntimeService(
        commands,
        RuntimeAcceptanceService(commands, commits, artifacts),
        commits,
        artifacts,
        planner,
        writer,
        writing_request_factory,
        plan_materializer,
        draft_materializer,
        projections,
        snapshots,
        BoundPolicyResolver(context.policy),
        task_reader=task_reader,
        chapter_settlement=chapter_settlement,
        memory_maintenance=memory_maintenance,
    )
    dispatcher = CreativeDispatcher(
        task_reader,
        creative,
        worker_id=context.worker_id,
        project_id=context.project_id,
        run_id=context.run_id,
        parallelism=context.policy.runtime_parallelism,
    )
    prompt_pins = tuple(
        dict.fromkeys(
            (
                *(spec_.system_prompt.content_hash for spec_ in planner_bundle.agent_specs),
                *(spec_.task_prompt.content_hash for spec_ in planner_bundle.agent_specs),
                *(item.expected_hash for item in editor_bundle.prompt_templates),
            )
        )
    )
    skill_pins = tuple(
        dict.fromkeys(
            (
                *(
                    skill.content_hash
                    for spec_ in planner_bundle.agent_specs
                    for skill in spec_.skills
                ),
                *(item.expected_hash for item in editor_bundle.skill_templates),
            )
        )
    )
    assembly = ProductionRuntimeAssembly(
        runtime=creative,
        dispatcher=dispatcher,
        planner=planner,
        planner_invocation_factory=planner_invocation_factory,
        writer=writer,
        writing_request_factory=writing_request_factory,
        plan_materializer=plan_materializer,
        draft_materializer=draft_materializer,
        chapter_settlement=chapter_settlement,
        memory_maintenance=memory_maintenance,
        task_reader=task_reader,
        session_factory=session_factory,
        artifacts=artifacts,
        model_gateway=model_gateway,
        memory_gateway=memory_gateway,
        attestation=None,
        admission=admission,
    )
    _assert_spec_identities(spec, assembly)
    attestation = freeze_production_attestation(
        spec=spec,
        context=context,
        assembly=assembly,
        session_factory=session_factory,
        model_gateway=model_gateway,
        memory_gateway=memory_gateway,
        endpoints=model_endpoints,
        retrieval_backend=retrieval_backend,
        projection_builder=projection_builder,
        sequence_limit=sequence,
        output_limit=reserved,
        prompt_pins=prompt_pins,
        skill_pins=skill_pins,
        reranker_resolved=context.reranker is not None,
        migration_head=migration_head,
        settlement_policy_fingerprint=settlement_policy.configuration_fingerprint,
    )
    object.__setattr__(assembly, "attestation", attestation)
    return assembly


__all__ = [
    "DETERMINISTIC_FAKE_ENDPOINT_PROFILE",
    "QWEN38_27B_FP8_8005_ENDPOINT_PROFILE",
    "build_production_assembly",
    "load_production_assembly_spec",
    "preflight_production_environment",
    "resolve_registered_model_endpoints",
]
