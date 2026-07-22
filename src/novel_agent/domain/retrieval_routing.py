"""Stage 2R retrieval profiles, derived-snapshot attestations, and route contracts.

These values describe derived retrieval state only.  They deliberately do not
become a Canonical Root and cannot grant a controller authority beyond the
runtime-provided capability.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    CandidatePool,
    NeedRisk,
    RetrievalChannel,
    Stage1QueryIntent,
)


class RetrievalBackendProfile(StrEnum):
    """Whether a run is a scripted contract smoke or a real hybrid retrieval run."""

    SCRIPTED_SMOKE = "scripted_smoke"
    REAL_HYBRID = "real_hybrid"


class SnapshotCapabilityStatus(StrEnum):
    EXACT = "exact"
    PARTIAL = "partial"
    STALE = "stale"
    FAILED = "failed"
    TEST_ONLY = "test_only"


class InformationDomain(StrEnum):
    WORKING = "working"
    WORLD_SEMANTIC = "world_semantic"
    PLAN_INTENT = "plan_intent"
    TEXTUAL_EVIDENCE = "textual_evidence"
    REFERENCE_KNOWLEDGE = "reference_knowledge"
    PROCEDURAL = "procedural"
    OPERATIONAL = "operational"


class ResolutionTier(StrEnum):
    R0 = "r0"
    R1 = "r1"
    R2 = "r2"


class RouteExecution(StrEnum):
    SERIAL = "serial"
    PARALLEL = "parallel"


class ChannelFailureCode(StrEnum):
    BACKEND_UNAVAILABLE = "backend_unavailable"
    COVERAGE_INCOMPLETE = "coverage_incomplete"
    PROFILE_MISMATCH = "profile_mismatch"
    BASIS_MISMATCH = "basis_mismatch"
    STALE = "stale"
    FORBIDDEN = "forbidden"
    TIMEOUT = "timeout"
    BUILD_FAILED = "build_failed"


class ChannelCoverage(DomainModel):
    """A channel-specific count receipt produced during projection."""

    channel: RetrievalChannel
    expected_units: int = Field(ge=0)
    ready_units: int = Field(ge=0)
    failed_units: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> ChannelCoverage:
        if self.ready_units + self.failed_units > self.expected_units:
            raise ValueError("channel coverage ready and failed units exceed expected units")
        return self

    @property
    def complete(self) -> bool:
        return self.ready_units == self.expected_units and self.failed_units == 0


class ChannelFailure(DomainModel):
    channel: RetrievalChannel
    code: ChannelFailureCode
    reason: str = Field(min_length=1)
    retryable: bool = False


class L2IndexKind(StrEnum):
    ANCHOR = "anchor"
    GROUNDED = "grounded"
    HIERARCHY = "hierarchy"


class L2IndexManifest(DomainModel):
    """Physical-index receipt; the index remains a rebuildable L2 structure."""

    index_id: StableId
    index_kind: L2IndexKind
    source_commit: CommitId
    snapshot_id: StableId
    physical_name: str = Field(min_length=1)
    alias: str = Field(min_length=1)
    document_count: int = Field(ge=0)
    mapping_hash: ArtifactId
    analyzer_profile: str = Field(min_length=1)
    embedding_profile: str | None = None

    @model_validator(mode="after")
    def validate_embedding_profile(self) -> L2IndexManifest:
        if self.index_kind is L2IndexKind.HIERARCHY and self.embedding_profile is not None:
            raise ValueError("hierarchy index manifest cannot claim an embedding profile")
        return self


class SnapshotCapability(DomainModel):
    """Trusted query-time intersection of snapshot state and runtime permissions."""

    source_commit: CommitId
    snapshot_id: StableId
    status: SnapshotCapabilityStatus
    available_channels: tuple[RetrievalChannel, ...] = ()
    coverage_by_channel: tuple[ChannelCoverage, ...] = ()
    embedding_profile: str | None = None
    graph_profile: str | None = None
    degraded_channels: tuple[RetrievalChannel, ...] = ()

    @model_validator(mode="after")
    def validate_channels(self) -> SnapshotCapability:
        available = set(self.available_channels)
        degraded = set(self.degraded_channels)
        if len(available) != len(self.available_channels):
            raise ValueError("snapshot available channels must be unique")
        if len(degraded) != len(self.degraded_channels):
            raise ValueError("snapshot degraded channels must be unique")
        if available & degraded:
            raise ValueError("snapshot channel cannot be both available and degraded")
        coverage_channels = tuple(item.channel for item in self.coverage_by_channel)
        if len(coverage_channels) != len(set(coverage_channels)):
            raise ValueError("snapshot channel coverage entries must be unique")
        if self.status is SnapshotCapabilityStatus.EXACT and (
            degraded or any(not item.complete for item in self.coverage_by_channel)
        ):
            raise ValueError("exact snapshot cannot contain degraded or incomplete channels")
        if self.status is SnapshotCapabilityStatus.TEST_ONLY and self.available_channels:
            raise ValueError("test-only snapshot cannot expose retrieval channels")
        return self


class ProjectionAttestation(DomainModel):
    """Auditable proof of the R1, index, vector, and graph projection basis."""

    attestation_id: StableId
    retrieval_backend_profile: RetrievalBackendProfile
    source_commit: CommitId
    snapshot_id: StableId
    capability: SnapshotCapability
    r1_record_count: int = Field(ge=0)
    r1_entity_association_count: int = Field(ge=0)
    graph_node_count: int = Field(ge=0)
    graph_edge_count: int = Field(ge=0)
    embedding_cache_hits: int = Field(default=0, ge=0)
    embedding_cache_misses: int = Field(default=0, ge=0)
    indexes: tuple[L2IndexManifest, ...] = ()
    embedding_model: str | None = None
    embedding_revision: str | None = None
    embedding_dimension: int | None = Field(default=None, ge=1)
    embedding_normalized: bool | None = None
    embedding_runtime_fingerprint: ArtifactId | None = None
    reranker_model: str | None = None
    reranker_revision: str | None = None
    failures: tuple[ChannelFailure, ...] = ()

    @model_validator(mode="after")
    def validate_basis_and_profile(self) -> ProjectionAttestation:
        if (
            self.capability.source_commit != self.source_commit
            or self.capability.snapshot_id != self.snapshot_id
        ):
            raise ValueError("projection attestation and snapshot capability must share a basis")
        index_ids = tuple(item.index_id for item in self.indexes)
        if len(index_ids) != len(set(index_ids)):
            raise ValueError("projection index manifests must have unique ids")
        if any(
            item.source_commit != self.source_commit or item.snapshot_id != self.snapshot_id
            for item in self.indexes
        ):
            raise ValueError("projection index manifest basis mismatch")
        failure_channels = tuple(item.channel for item in self.failures)
        if len(failure_channels) != len(set(failure_channels)):
            raise ValueError("projection failures must have unique channels")
        if self.retrieval_backend_profile is RetrievalBackendProfile.SCRIPTED_SMOKE:
            if self.capability.status is not SnapshotCapabilityStatus.TEST_ONLY:
                raise ValueError("scripted smoke requires a test-only snapshot capability")
            if any(
                value is not None
                for value in (
                    self.embedding_model,
                    self.embedding_revision,
                    self.embedding_dimension,
                    self.embedding_normalized,
                    self.embedding_runtime_fingerprint,
                )
            ):
                raise ValueError("scripted smoke cannot attest a real embedding runtime")
        elif self.capability.status is SnapshotCapabilityStatus.EXACT:
            if (
                self.r1_record_count < 1
                or self.embedding_model is None
                or self.embedding_revision is None
                or self.embedding_dimension != 1024
                or self.embedding_normalized is not True
                or self.embedding_runtime_fingerprint is None
                or self.reranker_model is None
                or self.reranker_revision is None
            ):
                raise ValueError(
                    "exact real-hybrid attestation lacks R1 or locked retrieval-model evidence"
                )
        return self

    @property
    def quality_eligible(self) -> bool:
        return (
            self.retrieval_backend_profile is RetrievalBackendProfile.REAL_HYBRID
            and self.capability.status is SnapshotCapabilityStatus.EXACT
            and not self.failures
        )


class RetrievalRoutingFeatures(DomainModel):
    query_intent: Stage1QueryIntent
    information_domains: tuple[InformationDomain, ...] = Field(min_length=1)
    exact_id_count: int = Field(default=0, ge=0)
    resolved_entity_count: int = Field(default=0, ge=0)
    unresolved_alias_count: int = Field(default=0, ge=0)
    predicate_count: int = Field(default=0, ge=0)
    lexical_specificity: float = Field(default=0, ge=0, le=1)
    quoted_phrase_length: int = Field(default=0, ge=0)
    semantic_openness: float = Field(default=0, ge=0, le=1)
    temporal_scope_kind: str = Field(default="unspecified", min_length=1)
    temporal_complexity: str = Field(default="none", min_length=1)
    relation_hops_requested: int = Field(default=0, ge=0, le=3)
    hierarchy_scope: str = Field(default="unspecified", min_length=1)
    continuous_prose_required: bool = False
    evidence_strength_required: str = Field(default="canonical_supported", min_length=1)
    mandatory: bool = False
    risk: NeedRisk
    access_sensitivity: str = Field(min_length=1)
    latency_budget_ms: int = Field(ge=1)
    token_budget: int = Field(ge=1)
    snapshot_capabilities: tuple[RetrievalChannel, ...] = ()

    @model_validator(mode="after")
    def validate_feature_sets(self) -> RetrievalRoutingFeatures:
        if len(self.information_domains) != len(set(self.information_domains)):
            raise ValueError("routing information domains must be unique")
        if len(self.snapshot_capabilities) != len(set(self.snapshot_capabilities)):
            raise ValueError("routing snapshot capabilities must be unique")
        if self.quoted_phrase_length and self.query_intent not in {
            Stage1QueryIntent.EXACT_QUOTE,
            Stage1QueryIntent.RARE_PHRASE,
        }:
            raise ValueError("quoted phrase routing feature requires a lexical quote intent")
        return self


class RouteStep(DomainModel):
    step_id: StableId
    channel: RetrievalChannel
    candidate_pool: CandidatePool
    query_template: str = Field(min_length=1)
    per_channel_limit: int = Field(default=20, ge=1, le=100)
    mandatory: bool = False


class RouteStepGroup(DomainModel):
    group_id: StableId
    execution: RouteExecution
    steps: tuple[RouteStep, ...] = Field(min_length=1)
    fusion_profile: str | None = None

    @model_validator(mode="after")
    def validate_group(self) -> RouteStepGroup:
        channels = tuple(item.channel for item in self.steps)
        if len(channels) != len(set(channels)):
            raise ValueError("route step group channels must be unique")
        if self.fusion_profile is not None and self.execution is not RouteExecution.PARALLEL:
            raise ValueError("route fusion requires a parallel step group")
        return self


class ConditionalFallback(DomainModel):
    fallback_id: StableId
    condition: str = Field(min_length=1)
    steps: tuple[RouteStep, ...] = Field(min_length=1)
    fusion_profile: str | None = None

    @model_validator(mode="after")
    def validate_steps(self) -> ConditionalFallback:
        channels = tuple(item.channel for item in self.steps)
        if len(channels) != len(set(channels)):
            raise ValueError("conditional fallback channels must be unique")
        if self.fusion_profile is not None and len(self.steps) < 2:
            raise ValueError("fallback fusion requires at least two channels")
        return self


class GraphTraversalPolicy(DomainModel):
    max_depth: int = Field(default=2, ge=1, le=3)
    allowed_edge_semantics: tuple[str, ...] = Field(default=("canonical", "evidence"), min_length=1)

    @model_validator(mode="after")
    def validate_semantics(self) -> GraphTraversalPolicy:
        if len(self.allowed_edge_semantics) != len(set(self.allowed_edge_semantics)):
            raise ValueError("graph edge semantics must be unique")
        if {"inferred", "similarity"} & set(self.allowed_edge_semantics):
            raise ValueError("graph traversal policy cannot use inferred or similarity proof edges")
        return self


class EvidenceExpansionPolicy(DomainModel):
    required_strength: str = Field(min_length=1)
    max_anchor_expansions: int = Field(default=10, ge=0)
    max_scene_expansions: int = Field(default=2, ge=0)
    max_full_chapter_reads: int = Field(default=0, ge=0)


class RouteStopPolicy(DomainModel):
    max_rounds: int = Field(default=2, ge=1)
    max_tool_calls: int = Field(default=12, ge=0)
    stop_when: str = Field(min_length=1)


class ExcludedChannel(DomainModel):
    channel: RetrievalChannel
    reason: str = Field(min_length=1)


class RouteProfile(DomainModel):
    profile_id: StableId
    version: SchemaVersion
    query_intent: Stage1QueryIntent
    resolution_tier: ResolutionTier
    allowed_channels: tuple[RetrievalChannel, ...] = ()
    mandatory_steps: tuple[RouteStep, ...] = ()
    primary_groups: tuple[RouteStepGroup, ...] = ()
    conditional_fallbacks: tuple[ConditionalFallback, ...] = ()
    graph_policy: GraphTraversalPolicy | None = None
    evidence_policy: EvidenceExpansionPolicy
    stop_policy: RouteStopPolicy

    @model_validator(mode="after")
    def validate_profile(self) -> RouteProfile:
        if len(self.allowed_channels) != len(set(self.allowed_channels)):
            raise ValueError("route profile allowed channels must be unique")
        planned = {
            step.channel
            for step in (
                *self.mandatory_steps,
                *(step for group in self.primary_groups for step in group.steps),
                *(step for fallback in self.conditional_fallbacks for step in fallback.steps),
            )
        }
        if not planned.issubset(self.allowed_channels):
            raise ValueError("route profile step channel is not allowed")
        if self.resolution_tier is not ResolutionTier.R2 and self.conditional_fallbacks:
            raise ValueError("only R2 route profiles may define conditional fallbacks")
        return self


class RoutePlan(DomainModel):
    route_plan_id: StableId
    profile_id: StableId
    need_id: StableId
    base_commit: CommitId
    snapshot_id: StableId
    resolution_tier: ResolutionTier
    domains: tuple[InformationDomain, ...] = Field(min_length=1)
    normalized_intent: Stage1QueryIntent
    routing_features_hash: ArtifactId
    mandatory_steps: tuple[RouteStep, ...] = ()
    primary_groups: tuple[RouteStepGroup, ...] = ()
    conditional_fallbacks: tuple[ConditionalFallback, ...] = ()
    graph_policy: GraphTraversalPolicy | None = None
    evidence_policy: EvidenceExpansionPolicy
    stop_policy: RouteStopPolicy
    excluded_channels: tuple[ExcludedChannel, ...] = ()
    policy_version: SchemaVersion

    @model_validator(mode="after")
    def validate_plan(self) -> RoutePlan:
        if len(self.domains) != len(set(self.domains)):
            raise ValueError("route plan domains must be unique")
        excluded = tuple(item.channel for item in self.excluded_channels)
        if len(excluded) != len(set(excluded)):
            raise ValueError("route plan excluded channels must be unique")
        active = {
            step.channel
            for step in (
                *self.mandatory_steps,
                *(step for group in self.primary_groups for step in group.steps),
                *(step for fallback in self.conditional_fallbacks for step in fallback.steps),
            )
        }
        if active & set(excluded):
            raise ValueError("route plan channel cannot be active and excluded")
        if self.resolution_tier is not ResolutionTier.R2 and self.conditional_fallbacks:
            raise ValueError("only R2 route plans may define conditional fallbacks")
        return self


class CounterfactualRouteRecord(DomainModel):
    """Evaluator-only alternative route used to measure routing regret."""

    record_id: StableId
    route_plan_id: StableId
    need_id: StableId
    base_commit: CommitId
    snapshot_id: StableId
    added_channels: tuple[RetrievalChannel, ...] = Field(min_length=1)
    candidate_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    evaluator_only: bool = True

    @model_validator(mode="after")
    def validate_counterfactual(self) -> CounterfactualRouteRecord:
        if len(self.added_channels) != len(set(self.added_channels)):
            raise ValueError("counterfactual route channels must be unique")
        if not self.evaluator_only:
            raise ValueError("counterfactual route records must remain evaluator-only")
        return self
