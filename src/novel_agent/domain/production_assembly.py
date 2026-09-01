"""Versioned production composition-root declaration and startup attestation."""

from __future__ import annotations

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId


class ProductionModelPolicy(DomainModel):
    """Non-secret model admission limits declared by the assembly spec."""

    require_admission: bool = True
    sequence_limit: int = Field(default=131_072, ge=1)
    default_output_limit: int = Field(default=8_000, ge=1)
    reasoning_billing_mode: str = Field(default="unknown_not_applicable", min_length=1)


class ProductionAssemblySpec(DomainModel):
    """Repo-owned declaration. Runtime observations must not be written back."""

    spec_version: SchemaVersion
    factory_locator: str = Field(min_length=1)
    runtime_contract_version: SchemaVersion
    expected_migration_head: str = Field(min_length=1)
    expected_planner_adapter: str = Field(min_length=1)
    expected_writer_adapter: str = Field(min_length=1)
    expected_plan_materializer: str = Field(min_length=1)
    expected_draft_materializer: str = Field(min_length=1)
    expected_chapter_settlement: str = Field(min_length=1)
    expected_memory_maintenance: str = Field(min_length=1)
    model_policy: ProductionModelPolicy
    expected_prompt_ids: tuple[StableId, ...]
    expected_skill_ids: tuple[StableId, ...]
    reranker_required: bool = False


class ResolvedEndpointRevision(DomainModel):
    role: str = Field(min_length=1)
    endpoint_name: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    revision: str | None = Field(default=None, min_length=1)
    adapter_identity: str = Field(min_length=1)
    is_external: bool


class ResolvedProductionAssemblyAttestation(DomainModel):
    """Startup facts frozen after preflight. Secrets never appear here."""

    spec_version: SchemaVersion
    factory_locator: str = Field(min_length=1)
    migration_head: str = Field(min_length=1)
    object_store_root: str = Field(min_length=1)
    session_factory_identity: str = Field(min_length=1)
    planner_adapter: str = Field(min_length=1)
    writer_adapter: str = Field(min_length=1)
    plan_materializer: str = Field(min_length=1)
    draft_materializer: str = Field(min_length=1)
    chapter_settlement: str = Field(min_length=1)
    memory_maintenance: str = Field(min_length=1)
    writing_request_factory: str = Field(min_length=1)
    planner_invocation_factory: str = Field(min_length=1)
    model_gateway: str = Field(min_length=1)
    memory_gateway: str = Field(min_length=1)
    projection_builder: str = Field(min_length=1)
    retrieval_backend: str = Field(min_length=1)
    endpoints: tuple[ResolvedEndpointRevision, ...]
    sequence_limit: int = Field(ge=1)
    output_limit: int = Field(ge=1)
    reasoning_billing_mode: str = Field(min_length=1)
    reasoning_included_in_completion_tokens: bool = False
    estimated_reasoning_reserve: int = Field(default=0, ge=0)
    safety_allowance_tokens: int | None = Field(default=None, ge=0)
    global_output_cap: int = Field(default=131_072, ge=1)
    endpoint_request_limit: int = Field(default=1, ge=1)
    configured_kv_token_budget: int | None = Field(default=None, ge=1)
    effective_kv_token_budget: int | None = Field(default=None, ge=1)
    kv_safety_reserve_ratio: float = Field(default=0.20, ge=0.0, lt=1.0)
    scheduling_timeout_seconds: float = Field(default=120.0, gt=0.0)
    prompt_pins: tuple[ArtifactId, ...]
    skill_pins: tuple[ArtifactId, ...]
    reranker_declared: bool
    reranker_resolved: bool
    configuration_fingerprint: ArtifactId


__all__ = [
    "ProductionAssemblySpec",
    "ProductionModelPolicy",
    "ResolvedEndpointRevision",
    "ResolvedProductionAssemblyAttestation",
]
