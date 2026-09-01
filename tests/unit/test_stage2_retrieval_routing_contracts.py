from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    CandidatePool,
    NeedRisk,
    RetrievalChannel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1QueryIntent,
)
from novel_agent.domain.planning_memory import RetrievalQueryBundle
from novel_agent.domain.retrieval_routing import (
    ChannelCoverage,
    ChannelFailure,
    ChannelFailureCode,
    ConditionalFallback,
    CounterfactualRouteRecord,
    EvidenceExpansionPolicy,
    GraphTraversalPolicy,
    InformationDomain,
    L2IndexKind,
    L2IndexManifest,
    ProjectionAttestation,
    ResolutionTier,
    RetrievalBackendProfile,
    RetrievalRoutingFeatures,
    RouteExecution,
    RoutePlan,
    RouteProfile,
    RouteStep,
    RouteStepGroup,
    RouteStopPolicy,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.services.stage2_retrieval_backend import _load_persisted_attestation

HASH_A = ArtifactId("sha256:" + "a" * 64)
COMMIT = CommitId(HASH_A.root)
SNAPSHOT = StableId("snapshot.stage2r")


def query_bundle() -> RetrievalQueryBundle:
    return RetrievalQueryBundle(
        semantic_query="q",
        lexical_queries=("q",),
        exact_entity_ids=(StableId("entity.route"),),
    )


def capability(
    *,
    status: SnapshotCapabilityStatus = SnapshotCapabilityStatus.EXACT,
    channels: tuple[RetrievalChannel, ...] = (RetrievalChannel.R1_EXACT,),
    coverage: tuple[ChannelCoverage, ...] | None = None,
) -> SnapshotCapability:
    return SnapshotCapability(
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        status=status,
        available_channels=channels,
        coverage_by_channel=(
            coverage
            if coverage is not None
            else tuple(
                ChannelCoverage(channel=channel, expected_units=1, ready_units=1)
                for channel in channels
            )
        ),
    )


def step(channel: RetrievalChannel = RetrievalChannel.R1_EXACT) -> RouteStep:
    return RouteStep(
        step_id=StableId(f"step.{channel.value}"),
        channel=channel,
        candidate_pool=CandidatePool.R1
        if channel in {RetrievalChannel.R1_EXACT, RetrievalChannel.R1_TEMPORAL}
        else CandidatePool.ANCHOR,
        query_template="registered-template-v1",
    )


def policy() -> tuple[EvidenceExpansionPolicy, RouteStopPolicy]:
    return (
        EvidenceExpansionPolicy(required_strength="text_supported"),
        RouteStopPolicy(stop_when="mandatory_gaps_closed_and_evidence_sufficient"),
    )


def test_retrieval_unit_v02_keeps_legacy_fields_compatible_and_validates_projection_metadata() -> (
    None
):
    legacy = RetrievalUnit(
        unit_id=StableId("unit.legacy"),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        text="hero location academy",
    )
    assert legacy.content_hash is None
    assert legacy.parent_unit_ids == ()

    unit = legacy.model_copy(
        update={
            "source_refs": (HASH_A,),
            "content_hash": HASH_A,
            "predicate": "location",
            "parent_unit_ids": (StableId("anchor.chapter.20"),),
            "narrative_start": 20,
            "narrative_end": 20,
            "story_time_start": 100,
            "story_time_end": 101,
            "access_scope": "writer_safe",
        }
    )
    assert unit.content_hash == HASH_A
    with pytest.raises(ValidationError, match="narrative end"):
        RetrievalUnit.model_validate(unit.model_dump() | {"narrative_end": 19})
    with pytest.raises(ValidationError, match="story time end"):
        RetrievalUnit.model_validate(unit.model_dump() | {"story_time_end": 99})
    with pytest.raises(ValidationError, match="parent ids must be unique"):
        RetrievalUnit.model_validate(
            unit.model_dump() | {"parent_unit_ids": ("anchor.chapter.20", "anchor.chapter.20")}
        )


def test_snapshot_capability_and_coverage_enforce_exact_and_test_only_boundaries() -> None:
    assert capability().status is SnapshotCapabilityStatus.EXACT
    with pytest.raises(ValidationError, match="exceed expected"):
        ChannelCoverage(
            channel=RetrievalChannel.R1_EXACT,
            expected_units=1,
            ready_units=1,
            failed_units=1,
        )
    with pytest.raises(ValidationError, match="exact snapshot"):
        capability(
            coverage=(
                ChannelCoverage(
                    channel=RetrievalChannel.R1_EXACT,
                    expected_units=2,
                    ready_units=1,
                ),
            )
        )
    with pytest.raises(ValidationError, match="both available and degraded"):
        SnapshotCapability(
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            status=SnapshotCapabilityStatus.PARTIAL,
            available_channels=(RetrievalChannel.R1_EXACT,),
            degraded_channels=(RetrievalChannel.R1_EXACT,),
        )
    with pytest.raises(ValidationError, match="test-only"):
        capability(status=SnapshotCapabilityStatus.TEST_ONLY)


def test_projection_attestation_requires_real_vector_evidence_for_exact_real_hybrid() -> None:
    manifest = L2IndexManifest(
        index_id=StableId("index.anchor"),
        index_kind=L2IndexKind.ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        physical_name="project-stage2r-anchor-123",
        alias="project-stage2r-anchor",
        document_count=4,
        mapping_hash=HASH_A,
        analyzer_profile="standard-cjk-exact-v1",
        embedding_profile="narrative-bge-m3-v0.1",
    )
    with pytest.raises(ValidationError, match="lacks R1 or locked retrieval-model"):
        ProjectionAttestation(
            attestation_id=StableId("attestation.missing"),
            retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            capability=capability(),
            r1_record_count=0,
            r1_entity_association_count=0,
            graph_node_count=0,
            graph_edge_count=0,
        )
    exact = ProjectionAttestation(
        attestation_id=StableId("attestation.real"),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        capability=capability(),
        r1_record_count=4,
        r1_entity_association_count=5,
        graph_node_count=4,
        graph_edge_count=2,
        indexes=(manifest,),
        embedding_model="BAAI/bge-m3",
        embedding_revision="5617a9f61b028005a4858fdac845db406aefb181",
        embedding_dimension=1024,
        embedding_normalized=True,
        embedding_runtime_fingerprint=HASH_A,
        reranker_model="BAAI/bge-reranker-v2-m3",
        reranker_revision="953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
    )
    assert exact.quality_eligible is True
    restored = _load_persisted_attestation(exact.model_dump(mode="json"))
    assert restored == exact
    blocked = exact.model_copy(
        update=(
            {
                "failures": (
                    ChannelFailure(
                        channel=RetrievalChannel.ANCHOR_DENSE,
                        code=ChannelFailureCode.TIMEOUT,
                        reason="embedding service timeout",
                    ),
                )
            }
        )
    )
    assert blocked.quality_eligible is False
    smoke = ProjectionAttestation(
        attestation_id=StableId("attestation.smoke"),
        retrieval_backend_profile=RetrievalBackendProfile.SCRIPTED_SMOKE,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        capability=SnapshotCapability(
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            status=SnapshotCapabilityStatus.TEST_ONLY,
        ),
        r1_record_count=0,
        r1_entity_association_count=0,
        graph_node_count=0,
        graph_edge_count=0,
    )
    assert smoke.quality_eligible is False
    with pytest.raises(ValidationError, match="scripted smoke"):
        ProjectionAttestation.model_validate(
            smoke.model_dump() | {"embedding_model": "deterministic-test-embedding"}
        )


def test_projection_attestation_names_text_replay_without_faking_hybrid_evidence() -> None:
    channels = (
        RetrievalChannel.ANCHOR_BM25,
        RetrievalChannel.ANCHOR_DENSE,
        RetrievalChannel.GROUNDED_BM25,
        RetrievalChannel.GROUNDED_DENSE,
        RetrievalChannel.HIERARCHY,
    )
    replay = ProjectionAttestation(
        attestation_id=StableId("attestation.text-replay"),
        retrieval_backend_profile=RetrievalBackendProfile.BENCHMARK_TEXT_REPLAY,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        capability=SnapshotCapability(
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            status=SnapshotCapabilityStatus.EXACT,
            available_channels=channels,
            coverage_by_channel=tuple(
                ChannelCoverage(channel=channel, expected_units=1, ready_units=1)
                for channel in channels
            ),
        ),
        r1_record_count=0,
        r1_entity_association_count=0,
        graph_node_count=0,
        graph_edge_count=0,
        reranker_model="deterministic-text-replay",
        reranker_revision="v1",
    )
    assert replay.retrieval_backend_profile is RetrievalBackendProfile.BENCHMARK_TEXT_REPLAY
    assert replay.quality_eligible is False
    with pytest.raises(ValidationError, match="embedding runtime"):
        ProjectionAttestation.model_validate(
            replay.model_dump(mode="python") | {"embedding_model": "not-a-text-replay"}
        )


def test_routing_features_and_route_contracts_reject_fanout_and_unsafe_graph_proof() -> None:
    features = RetrievalRoutingFeatures(
        query_intent=Stage1QueryIntent.EXACT_QUOTE,
        information_domains=(InformationDomain.TEXTUAL_EVIDENCE,),
        quoted_phrase_length=8,
        risk=NeedRisk.HIGH,
        access_sensitivity="writer_safe",
        latency_budget_ms=2000,
        token_budget=4000,
        snapshot_capabilities=(RetrievalChannel.GROUNDED_BM25,),
    )
    assert features.quoted_phrase_length == 8
    with pytest.raises(ValidationError, match="lexical quote intent"):
        RetrievalRoutingFeatures.model_validate(
            features.model_dump() | {"query_intent": Stage1QueryIntent.CURRENT_STATE}
        )
    with pytest.raises(ValidationError, match="inferred"):
        GraphTraversalPolicy(allowed_edge_semantics=("canonical", "inferred"))

    evidence, stop = policy()
    primary = RouteStepGroup(
        group_id=StableId("group.primary"),
        execution=RouteExecution.PARALLEL,
        steps=(
            step(RetrievalChannel.ANCHOR_BM25),
            step(RetrievalChannel.ANCHOR_DENSE),
        ),
        fusion_profile="application-rrf-v1",
    )
    profile = RouteProfile(
        profile_id=StableId("profile.related-event"),
        version=SchemaVersion("0.1.0"),
        query_intent=Stage1QueryIntent.RELATED_EVENT,
        resolution_tier=ResolutionTier.R2,
        allowed_channels=(RetrievalChannel.ANCHOR_BM25, RetrievalChannel.ANCHOR_DENSE),
        primary_groups=(primary,),
        evidence_policy=evidence,
        stop_policy=stop,
    )
    assert profile.primary_groups == (primary,)
    with pytest.raises(ValidationError, match="not allowed"):
        RouteProfile.model_validate(
            profile.model_dump() | {"allowed_channels": (RetrievalChannel.ANCHOR_BM25,)}
        )
    with pytest.raises(ValidationError, match="parallel step group"):
        RouteStepGroup(
            group_id=StableId("group.serial"),
            execution=RouteExecution.SERIAL,
            steps=(step(),),
            fusion_profile="application-rrf-v1",
        )
    with pytest.raises(ValidationError, match="only R2"):
        RouteProfile(
            profile_id=StableId("profile.r1"),
            version=SchemaVersion("0.1.0"),
            query_intent=Stage1QueryIntent.CURRENT_STATE,
            resolution_tier=ResolutionTier.R1,
            allowed_channels=(RetrievalChannel.R1_EXACT,),
            conditional_fallbacks=(
                ConditionalFallback(
                    fallback_id=StableId("fallback.1"),
                    condition="miss",
                    steps=(step(),),
                ),
            ),
            evidence_policy=evidence,
            stop_policy=stop,
        )


def test_route_plan_and_counterfactual_records_enforce_runtime_authority_boundaries() -> None:
    evidence, stop = policy()
    primary_step = step(RetrievalChannel.R1_EXACT)
    plan = RoutePlan(
        route_plan_id=StableId("route.1"),
        profile_id=StableId("profile.1"),
        need_id=StableId("need.1"),
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        resolution_tier=ResolutionTier.R1,
        domains=(InformationDomain.WORLD_SEMANTIC,),
        normalized_intent=Stage1QueryIntent.CURRENT_STATE,
        routing_features_hash=HASH_A,
        mandatory_steps=(primary_step,),
        evidence_policy=evidence,
        stop_policy=stop,
        compiled_query_bundle=query_bundle(),
        effective_channels=(RetrievalChannel.R1_EXACT,),
        policy_version=SchemaVersion("0.1.0"),
    )
    assert plan.resolution_tier is ResolutionTier.R1
    with pytest.raises(ValueError, match="effective channels"):
        RoutePlan.model_validate(plan.model_dump() | {"effective_channels": ()})
    with pytest.raises(ValueError, match="query-unavailable"):
        RoutePlan.model_validate(
            plan.model_dump()
            | {
                "query_unavailable_reasons": {
                    RetrievalChannel.R1_EXACT: "missing_exact_entity_or_predicate"
                }
            }
        )
    semantic_step = step(RetrievalChannel.ANCHOR_BM25)
    legal_r1_fallback = RoutePlan.model_validate(
        plan.model_dump()
        | {
            "conditional_fallbacks": (
                ConditionalFallback(
                    fallback_id=StableId("fallback.current-state"),
                    condition="exact_current_record_absent",
                    steps=(semantic_step,),
                ),
            ),
            "effective_channels": (
                RetrievalChannel.R1_EXACT,
                RetrievalChannel.ANCHOR_BM25,
            ),
        }
    )
    assert legal_r1_fallback.resolution_tier is ResolutionTier.R1
    with pytest.raises(ValidationError, match="active and excluded"):
        RoutePlan.model_validate(
            plan.model_dump()
            | {
                "excluded_channels": (
                    {"channel": RetrievalChannel.R1_EXACT, "reason": "forbidden"},
                )
            }
        )
    record = CounterfactualRouteRecord(
        record_id=StableId("counterfactual.1"),
        route_plan_id=plan.route_plan_id,
        need_id=plan.need_id,
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        added_channels=(RetrievalChannel.ANCHOR_DENSE,),
        candidate_count=3,
        selected_count=1,
    )
    assert record.evaluator_only is True
    with pytest.raises(ValidationError, match="evaluator-only"):
        CounterfactualRouteRecord.model_validate(record.model_dump() | {"evaluator_only": False})


def _construct(model: Any, **values: Any) -> Any:
    return model.model_construct(**values)


def _reject(model: Any, validator: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        getattr(model, validator)()


def test_retrieval_routing_contract_rejects_all_duplicate_and_basis_edges() -> None:
    hierarchy = _construct(
        L2IndexManifest,
        index_kind=L2IndexKind.HIERARCHY,
        embedding_profile="forbidden",
    )
    _reject(hierarchy, "validate_embedding_profile", "cannot claim")

    complete = ChannelCoverage(
        channel=RetrievalChannel.R1_EXACT,
        expected_units=1,
        ready_units=1,
    )
    capability_base = _construct(
        SnapshotCapability,
        status=SnapshotCapabilityStatus.PARTIAL,
        available_channels=(RetrievalChannel.R1_EXACT,),
        degraded_channels=(),
        coverage_by_channel=(complete,),
    )
    for update, message in (
        (
            {"available_channels": (RetrievalChannel.R1_EXACT,) * 2},
            "available channels must be unique",
        ),
        (
            {"degraded_channels": (RetrievalChannel.ANCHOR_BM25,) * 2},
            "degraded channels must be unique",
        ),
        (
            {"coverage_by_channel": (complete, complete)},
            "coverage entries must be unique",
        ),
    ):
        _reject(
            capability_base.model_copy(update=update),
            "validate_channels",
            message,
        )

    manifest = L2IndexManifest(
        index_id=StableId("index.one"),
        index_kind=L2IndexKind.ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        physical_name="index-one",
        alias="alias-one",
        document_count=1,
        mapping_hash=HASH_A,
        analyzer_profile="standard",
    )
    attestation = _construct(
        ProjectionAttestation,
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        capability=capability(),
        indexes=(),
        failures=(),
        r1_record_count=1,
        embedding_model="BAAI/bge-m3",
        embedding_revision="locked",
        embedding_dimension=1024,
        embedding_normalized=True,
        embedding_runtime_fingerprint=HASH_A,
        reranker_model="reranker",
        reranker_revision="locked",
    )
    wrong_capability = capability().model_copy(update={"snapshot_id": StableId("snapshot.other")})
    _reject(
        attestation.model_copy(update={"capability": wrong_capability}),
        "validate_basis_and_profile",
        "share a basis",
    )
    _reject(
        attestation.model_copy(update={"indexes": (manifest, manifest)}),
        "validate_basis_and_profile",
        "unique ids",
    )
    wrong_index = manifest.model_copy(update={"snapshot_id": StableId("snapshot.other")})
    _reject(
        attestation.model_copy(update={"indexes": (wrong_index,)}),
        "validate_basis_and_profile",
        "manifest basis mismatch",
    )
    failure = ChannelFailure(
        channel=RetrievalChannel.ANCHOR_DENSE,
        code=ChannelFailureCode.TIMEOUT,
        reason="timeout",
    )
    _reject(
        attestation.model_copy(update={"failures": (failure, failure)}),
        "validate_basis_and_profile",
        "failures must have unique",
    )
    _reject(
        attestation.model_copy(
            update={
                "retrieval_backend_profile": RetrievalBackendProfile.SCRIPTED_SMOKE,
            }
        ),
        "validate_basis_and_profile",
        "requires a test-only",
    )
    partial = attestation.model_copy(
        update={
            "capability": capability_base.model_copy(
                update={"source_commit": COMMIT, "snapshot_id": SNAPSHOT}
            )
        }
    )
    assert partial.validate_basis_and_profile() is partial


def test_retrieval_route_shapes_reject_duplicate_channels_and_invalid_tiers() -> None:
    features = _construct(
        RetrievalRoutingFeatures,
        information_domains=(
            InformationDomain.WORLD_SEMANTIC,
            InformationDomain.WORLD_SEMANTIC,
        ),
        snapshot_capabilities=(),
        quoted_phrase_length=0,
        query_intent=Stage1QueryIntent.CURRENT_STATE,
    )
    _reject(features, "validate_feature_sets", "domains must be unique")
    duplicate_capability = features.model_copy(
        update={
            "information_domains": (InformationDomain.WORLD_SEMANTIC,),
            "snapshot_capabilities": (RetrievalChannel.R1_EXACT,) * 2,
        }
    )
    _reject(
        duplicate_capability,
        "validate_feature_sets",
        "capabilities must be unique",
    )

    one_step = step()
    group = _construct(
        RouteStepGroup,
        steps=(one_step, one_step),
        fusion_profile=None,
        execution=RouteExecution.PARALLEL,
    )
    _reject(group, "validate_group", "channels must be unique")
    fallback = _construct(
        ConditionalFallback,
        steps=(one_step, one_step),
        fusion_profile=None,
    )
    _reject(fallback, "validate_steps", "channels must be unique")
    single_fusion = fallback.model_copy(update={"steps": (one_step,), "fusion_profile": "rrf"})
    _reject(single_fusion, "validate_steps", "at least two channels")

    graph = _construct(
        GraphTraversalPolicy,
        allowed_edge_semantics=("canonical", "canonical"),
    )
    _reject(graph, "validate_semantics", "semantics must be unique")
    valid_graph = graph.model_copy(update={"allowed_edge_semantics": ("canonical", "evidence")})
    assert valid_graph.validate_semantics() is valid_graph

    evidence, stop = policy()
    profile = _construct(
        RouteProfile,
        allowed_channels=(RetrievalChannel.R1_EXACT,) * 2,
        mandatory_steps=(),
        primary_groups=(),
        conditional_fallbacks=(),
        resolution_tier=ResolutionTier.R1,
    )
    _reject(profile, "validate_profile", "allowed channels must be unique")

    excluded = type(
        "Excluded",
        (),
        {"channel": RetrievalChannel.ANCHOR_BM25},
    )()
    plan = _construct(
        RoutePlan,
        domains=(InformationDomain.WORLD_SEMANTIC,) * 2,
        excluded_channels=(),
        mandatory_steps=(),
        primary_groups=(),
        conditional_fallbacks=(),
        resolution_tier=ResolutionTier.R1,
        evidence_policy=evidence,
        stop_policy=stop,
    )
    _reject(plan, "validate_plan", "domains must be unique")
    duplicate_excluded = plan.model_copy(
        update={
            "domains": (InformationDomain.WORLD_SEMANTIC,),
            "excluded_channels": (excluded, excluded),
        }
    )
    _reject(duplicate_excluded, "validate_plan", "excluded channels must be unique")
    tier_fallback = duplicate_excluded.model_copy(
        update={
            "excluded_channels": (),
            "conditional_fallbacks": (
                ConditionalFallback(
                    fallback_id=StableId("fallback.tier"),
                    condition="miss",
                    steps=(one_step,),
                ),
            ),
        }
    )
    _reject(tier_fallback, "validate_plan", "only R2")

    counterfactual = _construct(
        CounterfactualRouteRecord,
        added_channels=(RetrievalChannel.ANCHOR_DENSE,) * 2,
        evaluator_only=True,
    )
    _reject(counterfactual, "validate_counterfactual", "channels must be unique")
