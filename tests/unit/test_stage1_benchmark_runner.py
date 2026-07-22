from __future__ import annotations

import pytest

from novel_agent.domain.benchmark import (
    BenchmarkCaseManifest,
    BenchmarkQueryCondition,
    BenchmarkTrack,
    TextRootDocument,
)
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory import (
    ChannelHit,
    HorizonNeedSet,
    RetrievalChannel,
    Stage1MemoryNeed,
    WorldRootDocument,
)
from novel_agent.services.benchmark_importer import bundle_content_id
from novel_agent.services.stage1_benchmark import (
    FrozenHorizonNeedGenerator,
    Stage1BenchmarkRunner,
    Stage1NeedGenerator,
    _evidence_matches,
    _resolve_text,
)
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


class FixtureMemoryConstructor:
    def __init__(self, world: WorldRootDocument) -> None:
        self._world = world

    def construct(
        self,
        history: TextRootDocument,
        case: BenchmarkCaseManifest,
    ) -> WorldRootDocument:
        assert history.root_hash == case.input_text_root
        return self._world


class EmptyExternalBackend:
    def __init__(self) -> None:
        self.calls: list[RetrievalChannel] = []

    def search(
        self, need: Stage1MemoryNeed, channel: RetrievalChannel, limit: int
    ) -> tuple[ChannelHit, ...]:
        self.calls.append(channel)
        return ()


class ExternalReranker:
    profile = "external-reranker-v1"

    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        return tuple(0.0 for _ in passages)


def test_oracle_20_to_3_runner_freezes_context_and_beats_smoke_baselines() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]

    result = Stage1BenchmarkRunner(token_budget=4000).run(
        bundle, case.case_id, BenchmarkTrack.ORACLE
    )

    profiles = {profile.profile: profile for profile in result.profile_results}
    assert set(profiles) == {
        "B0-recent-3",
        "B1-recent-3+chapter-summary",
        "B2-naive-dense",
        "B3-grounded-bm25",
        "B4-grounded-rrf",
        "K1-exact+hybrid",
        "K2-+hierarchy",
        "K3-+reranker",
        "K4-memory-kernel",
        "A0-grounded-bm25-direct",
        "A1-anchor-bm25-no-expansion",
        "A2-anchor-bm25-expand",
        "A3-anchor-rrf-expand",
        "A4-anchor-rerank-expand",
        "A5-bounded-fallback",
        "A6-all-channel-upper-bound",
    }
    assert profiles["A1-anchor-bm25-no-expansion"].metrics.l0_evidence_tokens_read == 0
    recent = profiles["B0-recent-3"].metrics
    recent_with_summaries = profiles["B1-recent-3+chapter-summary"].metrics
    bm25 = profiles["B3-grounded-bm25"].metrics
    kernel = profiles["K4-memory-kernel"].metrics
    assert result.context_frozen is True
    assert kernel.gold_evidence_recall == 1.0
    assert kernel.mandatory_constraint_coverage == 1.0
    assert kernel.evidence_traceability == 1.0
    assert kernel.future_leakage_rate == 0.0
    assert kernel.gold_evidence_recall > recent.gold_evidence_recall
    assert recent_with_summaries.gold_evidence_recall > recent.gold_evidence_recall
    assert recent_with_summaries.l0_evidence_tokens_read != recent.l0_evidence_tokens_read
    assert result.config.summary_profile == "evidence-bound-chapter-summary-v1"
    assert kernel.gold_evidence_recall > bm25.gold_evidence_recall
    assert profiles["K4-memory-kernel"].failure_categories == ()


def test_runner_is_deterministic_for_the_same_bundle_and_profile() -> None:
    bundle = make_synthetic_bundle()
    case_id = bundle.case_manifests[0].case_id
    runner = Stage1BenchmarkRunner()

    first = runner.run(bundle, case_id, BenchmarkTrack.ORACLE)
    second = runner.run(bundle, case_id, BenchmarkTrack.ORACLE)

    assert first == second
    assert first.config.query_condition is BenchmarkQueryCondition.GENERATED
    assert first.config.need_profile == Stage1NeedGenerator.profile


def test_runner_accepts_frozen_oracle_or_generated_horizon_needs() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    world = bundle.world_roots[0]
    needs = Stage1NeedGenerator().generate(world, case)
    horizon = HorizonNeedSet(
        horizon_start=case.target_range[0],
        horizon_end=case.target_range[1],
        shared_constraints=needs,
    )
    generator = FrozenHorizonNeedGenerator(
        horizon,
        profile="oracle.annotation-v1",
        query_condition=BenchmarkQueryCondition.ORACLE,
    )

    result = Stage1BenchmarkRunner().run(
        bundle,
        case.case_id,
        BenchmarkTrack.ORACLE,
        need_generator=generator,
    )

    assert result.config.query_condition is BenchmarkQueryCondition.ORACLE
    assert result.config.need_profile == "oracle.annotation-v1"


def test_runner_accepts_versioned_external_retrieval_and_reranker() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    backend = EmptyExternalBackend()
    snapshot_id = StableId("snapshot.external.v1")

    result = Stage1BenchmarkRunner().run(
        bundle,
        case.case_id,
        BenchmarkTrack.ORACLE,
        retrieval_backend=backend,
        retrieval_snapshot_id=snapshot_id,
        embedding_profile="bge-m3.test-profile",
        reranker=ExternalReranker(),
    )

    assert backend.calls
    assert result.snapshot_id == snapshot_id
    assert result.config.embedding_profile == "bge-m3.test-profile"
    assert result.config.reranker_profile == ExternalReranker.profile
    assert all(
        profile.metrics.future_leakage_rate == 0.0
        and profile.metrics.premature_future_injection_rate == 0.0
        for profile in result.profile_results
    )


def test_external_retrieval_requires_complete_basis_fingerprint() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    backend = EmptyExternalBackend()
    with pytest.raises(ValueError, match="requires snapshot id"):
        Stage1BenchmarkRunner().run(
            bundle,
            case.case_id,
            BenchmarkTrack.ORACLE,
            retrieval_backend=backend,
        )


def test_frozen_horizon_need_generator_rejects_invalid_inputs() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    world = bundle.world_roots[0]
    needs = Stage1NeedGenerator().generate(world, case)
    with pytest.raises(ValueError, match="must not be empty"):
        FrozenHorizonNeedGenerator(
            HorizonNeedSet(horizon_start=21, horizon_end=23),
            profile="",
            query_condition=BenchmarkQueryCondition.GENERATED,
        )
    wrong_horizon = FrozenHorizonNeedGenerator(
        HorizonNeedSet(horizon_start=21, horizon_end=22),
        profile="test",
        query_condition=BenchmarkQueryCondition.GENERATED,
    )
    with pytest.raises(ValueError, match="horizon differs"):
        wrong_horizon.generate(world, case)
    duplicate = FrozenHorizonNeedGenerator(
        HorizonNeedSet(
            horizon_start=21,
            horizon_end=23,
            shared_constraints=(needs[0], needs[0]),
        ),
        profile="test",
        query_condition=BenchmarkQueryCondition.GENERATED,
    )
    with pytest.raises(ValueError, match="ids must be unique"):
        duplicate.generate(world, case)
    wrong_basis = FrozenHorizonNeedGenerator(
        HorizonNeedSet(
            horizon_start=21,
            horizon_end=23,
            shared_constraints=(
                needs[0].model_copy(update={"base_commit": CommitId("sha256:" + "f" * 64)}),
            ),
        ),
        profile="test",
        query_condition=BenchmarkQueryCondition.GENERATED,
    )
    with pytest.raises(ValueError, match="basis differs"):
        wrong_basis.generate(world, case)


def test_end_to_end_track_requires_an_explicit_memory_constructor() -> None:
    bundle = make_synthetic_bundle()
    case_id = bundle.case_manifests[0].case_id

    with pytest.raises(ValueError, match="explicit memory constructor"):
        Stage1BenchmarkRunner().run(bundle, case_id, BenchmarkTrack.END_TO_END)

    result = Stage1BenchmarkRunner().run(
        bundle,
        case_id,
        BenchmarkTrack.END_TO_END,
        constructor=FixtureMemoryConstructor(bundle.world_roots[0]),
    )
    assert result.track is BenchmarkTrack.END_TO_END


def test_runner_rejects_invalid_budget_missing_case_and_missing_oracle_world() -> None:
    bundle = make_synthetic_bundle()
    with pytest.raises(ValueError, match="token budget"):
        Stage1BenchmarkRunner(token_budget=0)
    with pytest.raises(ValueError, match="case does not exist"):
        Stage1BenchmarkRunner().run(
            bundle,
            StableId("case.missing"),
            BenchmarkTrack.ORACLE,
        )

    case = bundle.case_manifests[0].model_copy(update={"input_world_root_verified": None})
    provisional = bundle.model_copy(update={"case_manifests": (case,)})
    reduced = provisional.model_copy(update={"content_hash": bundle_content_id(provisional)})
    with pytest.raises(ValueError, match="verified world root"):
        Stage1BenchmarkRunner().run(reduced, case.case_id, BenchmarkTrack.ORACLE)

    no_summary_case = bundle.case_manifests[0].model_copy(
        update={"input_summary_root": None, "gate_eligible": False}
    )
    no_summary_bundle = bundle.model_copy(
        update={"summary_roots": (), "case_manifests": (no_summary_case,)}
    )
    no_summary_bundle = no_summary_bundle.model_copy(
        update={"content_hash": bundle_content_id(no_summary_bundle)}
    )
    no_summary_result = Stage1BenchmarkRunner().run(
        no_summary_bundle, no_summary_case.case_id, BenchmarkTrack.ORACLE
    )
    assert no_summary_result.config.summary_profile == "summary-unavailable"


def test_need_generator_skips_obligations_outside_target_horizon() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    deferred = world.obligations[0].model_copy(update={"due_chapter": 30})
    changed_world = world.model_copy(update={"obligations": (deferred,)})

    needs = Stage1NeedGenerator().generate(changed_world, bundle.case_manifests[0])

    assert all(need.need_type != "plan_obligation" for need in needs)


def test_evidence_metric_helpers_cover_spanless_and_mismatched_refs() -> None:
    bundle = make_synthetic_bundle()
    history = bundle.text_roots[0]
    evidence = bundle.case_manifests[0].observed_use_gold[0].evidence_refs[0]
    spanless = evidence.model_copy(
        update={"evidence_id": StableId("evidence.spanless"), "span": None}
    )
    assert _resolve_text(spanless, history) == ""
    assert _evidence_matches(spanless, evidence) is False
    other = evidence.model_copy(
        update={
            "evidence_id": StableId("evidence.other-root"),
            "root_hash": bundle.text_roots[1].root_hash,
        }
    )
    assert _evidence_matches(other, evidence) is False
    assert evidence.span is not None
    other_block = evidence.model_copy(
        update={
            "evidence_id": StableId("evidence.other-block"),
            "span": evidence.span.model_copy(update={"block_id": StableId("block.other")}),
        }
    )
    assert _evidence_matches(other_block, evidence) is False
