"""Run reproducible Stage 2 deterministic-vs-bounded comparisons on a real bundle."""

from __future__ import annotations

import json
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from novel_agent.agents import seal_tool_policy
from novel_agent.domain.benchmark import (
    BenchmarkBundle,
    BenchmarkCaseManifest,
    GoldItem,
    PlanEvidenceRef,
    PlanRootDocument,
    TextRootDocument,
)
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    FusedCandidate,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalUnit,
    Stage1ContextPackage,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.retrieval_routing import (
    RetrievalBackendProfile,
    RoutePlan,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    BenchmarkInformationProfile,
    ContextBudget,
    ControllerArm,
    ControllerMode,
    MemoryResolutionRequest,
    PairedContextArmResult,
    PairedContextComparison,
    PairedPilotArmMetrics,
    PairedPilotCaseResult,
    PublicBenchmarkConfig,
    PublicCheckpointCase,
    RequiredSnapshotPolicy,
    RetrievalBudget,
    Stage2PairedPilotReport,
    ToolPolicy,
)
from novel_agent.domain.text import EvidenceRef
from novel_agent.runtime.memory_controller import RouteBoundControllerPolicy
from novel_agent.services.benchmark_importer import BenchmarkBundleImporter, content_id
from novel_agent.services.memory_pipeline import AnchorBuilder, ContextCompiler, EvidenceExpander
from novel_agent.services.paired_controller import PairedMemoryControllerRunner
from novel_agent.services.retrieval import (
    InMemoryRetrievalBackend,
    RerankService,
    RetrievalBackend,
)
from novel_agent.services.retrieval_routing import DeterministicChannelPlanner
from novel_agent.services.stage1_benchmark import Stage1NeedGenerator
from novel_agent.tools.retrieval import CHANNEL_BY_TOOL


class Stage2PairedPilotRunner:
    """Execute both arms against one immutable retrieval basis per case.

    The in-memory implementation is retained solely for deterministic contract
    smoke tests.  A formal ``real_hybrid`` execution must inject the
    commit-scoped backend introduced by Stage 2R projection work; it may never
    silently receive this synthetic backend.
    """

    version = "stage2-paired-pilot-v0.2"

    def __init__(
        self,
        *,
        token_budget: int = 4000,
        max_candidates: int = 20,
        retrieval_backend_profile: RetrievalBackendProfile = RetrievalBackendProfile.SCRIPTED_SMOKE,
        controller_mode: ControllerMode = ControllerMode.DETERMINISTIC,
    ) -> None:
        if token_budget < 1:
            raise ValueError("paired Pilot token budget must be positive")
        if max_candidates < 1 or max_candidates > 100:
            raise ValueError("paired Pilot candidate limit must be between 1 and 100")
        self._token_budget = token_budget
        self._max_candidates = max_candidates
        self._retrieval_backend_profile = retrieval_backend_profile
        self._controller_mode = controller_mode

    @property
    def retrieval_backend_profile(self) -> RetrievalBackendProfile:
        return self._retrieval_backend_profile

    def run(self, bundle: BenchmarkBundle) -> Stage2PairedPilotReport:
        BenchmarkBundleImporter().validate(bundle)
        fingerprint = self._configuration_fingerprint(bundle.content_hash)
        cases = tuple(
            self._run_case(bundle, case, fingerprint, profile)
            for case in bundle.case_manifests
            for profile in BenchmarkInformationProfile
        )
        return Stage2PairedPilotReport(
            report_id=StableId(f"stage2-paired-pilot.v0-2.{bundle.bundle_id.root}"),
            bundle_hash=bundle.content_hash,
            configuration_fingerprint=fingerprint,
            controller_mode=self._controller_mode,
            cases=cases,
            paired_results_count=len(cases),
            comparable_results_count=sum(item.comparable for item in cases),
            future_leakage_count=sum(
                item.deterministic_metrics.future_leakage_count
                + item.agentic_metrics.future_leakage_count
                for item in cases
            ),
            safety_regression_count=sum(item.safety_regression for item in cases),
            accuracy_gain_count=sum(item.accuracy_gain for item in cases),
            tool_call_reduction_count=sum(item.tool_call_reduction for item in cases),
            delta_gain_count=sum(
                1
                for item in cases
                if item.delta_metrics is not None
                and item.delta_metrics.gold_evidence_recall
                > item.deterministic_metrics.gold_evidence_recall
            ),
            held_out_complex_gain_proven=False,
        )

    def _run_case(
        self,
        bundle: BenchmarkBundle,
        case: BenchmarkCaseManifest,
        fingerprint: ArtifactId,
        profile: BenchmarkInformationProfile,
        *,
        history_override: TextRootDocument | None = None,
        world_override: WorldRootDocument | None = None,
        plan_override: PlanRootDocument | None = None,
        base_commit_override: CommitId | None = None,
        controller_policy: Any | None = None,
    ) -> PairedPilotCaseResult:
        history = history_override or next(
            root for root in bundle.text_roots if root.root_hash == case.input_text_root
        )
        world = world_override or next(
            root for root in bundle.world_roots if root.root_hash == case.input_world_root_verified
        )
        plan = plan_override or next(
            root for root in bundle.plan_roots if root.root_hash == case.input_plan_root
        )
        base_commit = base_commit_override or world.source_commit
        world = world.model_copy(update={"source_commit": base_commit})
        snapshot_id = StableId(f"snapshot.{case.case_id.root}.stage2-paired")
        backend = self._scripted_smoke_backend(world, history, plan, snapshot_id=snapshot_id)
        world_needs = Stage1NeedGenerator().generate(world, case)
        needs = (
            (
                *world_needs,
                *self._plan_needs(
                    case,
                    plan,
                    world.source_commit,
                    world_needs,
                ),
            )
            if profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
            else world_needs
        )
        needs = self._scope_needs(needs, profile)
        if not needs:
            raise ValueError(f"paired Pilot case has no generated needs: {case.case_id.root}")
        max_calls = max(1, len(needs) * 2)
        tool_policy = ToolPolicy(
            policy_id=StableId(f"policy.stage2-paired-pilot.v0.2.{profile.value}"),
            version=bundle.bundle_schema_version,
            content_hash=fingerprint,
            allowed_tools=tuple(sorted(CHANNEL_BY_TOOL)),
            max_rounds=2,
            max_tool_calls=max_calls,
            max_query_rewrites_per_need=0,
            token_budget=self._token_budget,
        )
        runner = PairedMemoryControllerRunner.from_shared_backend(
            backend=backend,
            needs=needs,
            tool_policy=tool_policy,
            compiler=ContextCompiler(EvidenceExpander()),
            controller_policy=controller_policy or RouteBoundControllerPolicy(),
            freshness_check=lambda _: True,
            checkpointer=InMemorySaver(),
            comparison_basis_fingerprint=fingerprint,
        )
        first = needs[0]
        profile_suffix = profile.value.replace("_", "-")
        request = MemoryResolutionRequest(
            request_id=StableId(f"request.stage2-paired.{case.case_id.root}.{profile_suffix}"),
            run_id=first.run_id,
            task_id=first.task_id,
            project_id=case.project_id,
            base_commit=world.source_commit,
            snapshot_id=snapshot_id,
            required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
            task_contract=(
                f"prepare chapters {case.target_range[0]}-{case.target_range[1]} "
                f"under {profile.value}"
            ),
            initial_memory_needs=needs,
            worldline="main",
            narrative_chapter=case.target_range[0],
            access_scope=(
                AccessScope.AUTHOR_PLANNING
                if profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
                else AccessScope.WRITER_SAFE
            ),
            allow_future_plan=(profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED),
            retrieval_budget=RetrievalBudget(
                max_rounds=2,
                max_tool_calls=max_calls,
                max_query_rewrites_per_need=0,
                max_candidates=self._max_candidates,
                token_budget=self._token_budget,
            ),
            context_budget=ContextBudget(token_budget=self._token_budget),
        )
        comparison = runner.run(
            request,
            history,
            thread_id=f"stage2-paired.{case.case_id.root}.{profile_suffix}",
            evaluator_only_artifacts=(case.future_text_root_private,),
        )
        deterministic = self._metrics(case, comparison.deterministic)
        agentic = self._metrics(case, comparison.agentic)
        delta_metrics: PairedPilotArmMetrics | None = None
        arm_c_safety_regression = False
        if self._controller_mode is ControllerMode.DETERMINISTIC_PLUS_AGENTIC_DELTA:
            _arm_c_result, delta_metrics = self._build_arm_c(
                case, comparison.deterministic, comparison.agentic
            )
            # Arm C safety regression compares the production candidate (C)
            # against the deterministic floor (A), not B against A.
            arm_c_safety_regression = (
                delta_metrics.future_leakage_count > deterministic.future_leakage_count
                or delta_metrics.mandatory_constraint_coverage
                < deterministic.mandatory_constraint_coverage
            )
        return PairedPilotCaseResult(
            case_id=case.case_id,
            information_profile=profile,
            checkpoint_chapter=case.history_range[1],
            pair_id=comparison.pair_id,
            request_id=comparison.request_id,
            comparison_basis_fingerprint=fingerprint,
            comparable=comparison.comparable,
            blockers=comparison.blockers,
            deterministic_metrics=deterministic,
            agentic_metrics=agentic,
            delta_metrics=delta_metrics,
            accuracy_gain=(agentic.gold_evidence_recall > deterministic.gold_evidence_recall),
            tool_call_reduction=(agentic.retrieval_call_count < deterministic.retrieval_call_count),
            safety_regression=(
                arm_c_safety_regression
                if self._controller_mode is ControllerMode.DETERMINISTIC_PLUS_AGENTIC_DELTA
                else (
                    agentic.future_leakage_count > deterministic.future_leakage_count
                    or agentic.mandatory_constraint_coverage
                    < deterministic.mandatory_constraint_coverage
                )
            ),
        )

    def run_state_case(
        self,
        bundle: BenchmarkBundle,
        case: BenchmarkCaseManifest,
        profile: BenchmarkInformationProfile,
        *,
        history: TextRootDocument,
        world: WorldRootDocument,
        plan: PlanRootDocument,
        base_commit: CommitId,
        controller_policy: Any | None = None,
    ) -> PairedPilotCaseResult:
        """Evaluate one frozen E2E state without substituting the Oracle roots."""

        BenchmarkBundleImporter().validate(bundle)
        return self._run_case(
            bundle,
            case,
            self._configuration_fingerprint(bundle.content_hash),
            profile,
            history_override=history,
            world_override=world,
            plan_override=plan,
            base_commit_override=base_commit,
            controller_policy=controller_policy,
        )

    def resolve_state_case(
        self,
        config: PublicBenchmarkConfig,
        case: PublicCheckpointCase,
        profile: BenchmarkInformationProfile,
        *,
        history: TextRootDocument,
        world: WorldRootDocument,
        plan: PlanRootDocument,
        base_commit: CommitId,
        controller_policy_factory: Any | None = None,
        retrieval_backend: RetrievalBackend | None = None,
        snapshot_capability: SnapshotCapability | None = None,
        reranker: RerankService | None = None,
    ) -> PairedContextComparison:
        """Resolve both arms on E2E state without consulting private Gold.

        Receives PublicBenchmarkConfig (no Gold-containing content hash) and
        PublicCheckpointCase (no Gold fields).  The configuration_fingerprint
        is derived without the bundle's content hash.
        """

        world = world.model_copy(update={"source_commit": base_commit})
        fingerprint = config.configuration_fingerprint
        if self._retrieval_backend_profile is RetrievalBackendProfile.REAL_HYBRID:
            if retrieval_backend is None or snapshot_capability is None:
                raise RuntimeError(
                    "real_hybrid paired execution requires an injected commit-scoped backend "
                    "and exact snapshot capability"
                )
            if (
                snapshot_capability.source_commit != base_commit
                or snapshot_capability.status is not SnapshotCapabilityStatus.EXACT
            ):
                raise ValueError("real_hybrid paired capability must be exact for the state basis")
            snapshot_id = snapshot_capability.snapshot_id
            backend = retrieval_backend
        else:
            snapshot_id = StableId(f"snapshot.{case.case_id.root}.stage2-e2e")
            backend = self._scripted_smoke_backend(
                world,
                history,
                plan,
                snapshot_id=snapshot_id,
                canonical_commit=base_commit,
            )
        world_needs = Stage1NeedGenerator().generate(world, case)  # type: ignore[arg-type]
        needs = (
            (
                *world_needs,
                *self._plan_needs(case, plan, base_commit, world_needs),
            )
            if profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
            else world_needs
        )
        needs = self._scope_needs(needs, profile)
        if not needs:
            raise ValueError(f"E2E state produced no memory needs: {case.case_id.root}")
        route_plans = self._route_plans(needs, snapshot_capability)
        max_calls = max(1, len(needs) * 2)
        tool_policy = seal_tool_policy(
            ToolPolicy(
                policy_id=StableId(f"policy.stage2-e2e.v0.1.{profile.value}"),
                version=config.schema_version,
                content_hash=fingerprint,
                allowed_tools=self._allowed_tools(route_plans),
                max_rounds=2,
                max_tool_calls=max_calls,
                max_query_rewrites_per_need=0,
                token_budget=self._token_budget,
            )
        )
        controller_policy = (
            controller_policy_factory(tool_policy, route_plans)
            if controller_policy_factory is not None
            else RouteBoundControllerPolicy(route_plans)
        )
        runner = PairedMemoryControllerRunner.from_shared_backend(
            backend=backend,
            needs=needs,
            tool_policy=tool_policy,
            compiler=ContextCompiler(EvidenceExpander()),
            controller_policy=controller_policy,
            freshness_check=lambda _: True,
            checkpointer=InMemorySaver(),
            comparison_basis_fingerprint=fingerprint,
            route_plans=route_plans,
            reranker=reranker,
        )
        first = needs[0]
        suffix = profile.value.replace("_", "-")
        request = MemoryResolutionRequest(
            request_id=StableId(f"request.stage2-e2e.{case.case_id.root}.{suffix}"),
            run_id=first.run_id,
            task_id=first.task_id,
            project_id=case.project_id,
            base_commit=base_commit,
            snapshot_id=snapshot_id,
            required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
            task_contract=(
                f"prepare chapters {case.target_range[0]}-{case.target_range[1]} "
                f"from teacher-forced E2E state under {profile.value}"
            ),
            initial_memory_needs=needs,
            worldline="main",
            narrative_chapter=case.target_range[0],
            access_scope=(
                AccessScope.AUTHOR_PLANNING
                if profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
                else AccessScope.WRITER_SAFE
            ),
            allow_future_plan=(profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED),
            retrieval_budget=RetrievalBudget(
                max_rounds=2,
                max_tool_calls=max_calls,
                max_query_rewrites_per_need=0,
                max_candidates=self._max_candidates,
                token_budget=self._token_budget,
            ),
            context_budget=ContextBudget(token_budget=self._token_budget),
        )
        return runner.run(
            request,
            history,
            thread_id=f"stage2-e2e.{case.case_id.root}.{suffix}",
            evaluator_only_artifacts=(),
        )

    @staticmethod
    def _allowed_tools(route_plans: tuple[RoutePlan, ...]) -> tuple[str, ...]:
        if not route_plans:
            return tuple(sorted(CHANNEL_BY_TOOL))
        active_channels = {
            step.channel
            for plan in route_plans
            for step in (
                *plan.mandatory_steps,
                *(step for group in plan.primary_groups for step in group.steps),
                *(step for fallback in plan.conditional_fallbacks for step in fallback.steps),
            )
        }
        return tuple(
            tool_name
            for tool_name, channel in sorted(CHANNEL_BY_TOOL.items())
            if channel in active_channels
        )

    @staticmethod
    def _route_plans(
        needs: tuple[Stage1MemoryNeed, ...],
        capability: SnapshotCapability | None,
    ) -> tuple[RoutePlan, ...]:
        if capability is None:
            return ()
        planner = DeterministicChannelPlanner()
        return tuple(planner.plan(need, capability) for need in needs)

    @classmethod
    def _build_arm_c(
        cls,
        case: BenchmarkCaseManifest,
        deterministic: PairedContextArmResult,
        agentic: PairedContextArmResult,
    ) -> tuple[PairedContextArmResult, PairedPilotArmMetrics]:
        """Build real Arm C = selected(A) union accepted_delta(B) and compute its metrics.

        Arm C is the production candidate: the deterministic floor (A) plus the
        Agentic delta (B's selected units that A missed), restricted to delta
        that does not introduce future leakage.  Because ``future_leakage_count``
        is an arm-level signal that cannot be attributed to a single candidate,
        the entire delta is conservatively rejected whenever B leaked.
        """
        deterministic_selected_ids = set(deterministic.selected_unit_ids)
        agentic_selected_candidates = tuple(
            candidate
            for trace in agentic.context.retrieval_traces
            for candidate in trace.candidates
            if candidate.selected
        )
        # delta(B) = B's selected candidates whose unit_id is not already in A.
        delta_candidates = tuple(
            candidate
            for candidate in agentic_selected_candidates
            if candidate.unit.unit_id not in deterministic_selected_ids
        )
        # accepted_delta(B): drop the whole delta when B leaked, since arm-level
        # leakage cannot be localized to individual candidates.
        accepted_delta = delta_candidates if agentic.future_leakage_count == 0 else ()
        merged_traces = deterministic.context.retrieval_traces
        if accepted_delta:
            extra_trace = deterministic.context.retrieval_traces[0].model_copy(
                update={
                    "need_id": StableId("need.arm-c.delta"),
                    "candidates": accepted_delta,
                }
            )
            merged_traces = (*deterministic.context.retrieval_traces, extra_trace)
        merged_selected_ids = tuple(
            dict.fromkeys(
                (
                    *deterministic.selected_unit_ids,
                    *(candidate.unit.unit_id for candidate in accepted_delta),
                )
            )
        )
        arm_c_context = deterministic.context.model_copy(
            update={
                "context_id": StableId("context.arm-c"),
                "retrieval_traces": merged_traces,
            }
        )
        arm_c_result = PairedContextArmResult(
            arm=ControllerArm.DETERMINISTIC,
            context=arm_c_context,
            selected_unit_ids=merged_selected_ids,
            retrieval_call_count=(
                deterministic.retrieval_call_count + agentic.retrieval_call_count
            ),
            stop_reason=deterministic.stop_reason,
            comparison_basis_fingerprint=deterministic.comparison_basis_fingerprint,
            future_leakage_count=deterministic.future_leakage_count,
        )
        return arm_c_result, cls._metrics(case, arm_c_result)

    def _scripted_smoke_backend(
        self,
        world: WorldRootDocument,
        history: TextRootDocument,
        plan: PlanRootDocument,
        *,
        snapshot_id: StableId,
        canonical_commit: CommitId | None = None,
    ) -> InMemoryRetrievalBackend:
        if self._retrieval_backend_profile is not RetrievalBackendProfile.SCRIPTED_SMOKE:
            raise RuntimeError(
                "real_hybrid paired execution requires an injected commit-scoped "
                "retrieval backend; "
                "InMemoryRetrievalBackend is scripted_smoke only"
            )
        return InMemoryRetrievalBackend(
            AnchorBuilder().build(
                world,
                history,
                plan,
                snapshot_id=snapshot_id,
                canonical_commit=canonical_commit,
            )
        )

    def score_comparison(
        self,
        case: BenchmarkCaseManifest,
        profile: BenchmarkInformationProfile,
        comparison: PairedContextComparison,
    ) -> PairedPilotCaseResult:
        """Evaluator-only conversion of a frozen comparison into private-Gold metrics."""

        deterministic = self._metrics(case, comparison.deterministic)
        agentic = self._metrics(case, comparison.agentic)
        delta_metrics: PairedPilotArmMetrics | None = None
        arm_c_safety_regression = False
        if self._controller_mode is ControllerMode.DETERMINISTIC_PLUS_AGENTIC_DELTA:
            _arm_c_result, delta_metrics = self._build_arm_c(
                case,
                comparison.deterministic,
                comparison.agentic,
            )
            arm_c_safety_regression = (
                delta_metrics.future_leakage_count > deterministic.future_leakage_count
                or delta_metrics.mandatory_constraint_coverage
                < deterministic.mandatory_constraint_coverage
            )
        return PairedPilotCaseResult(
            case_id=case.case_id,
            information_profile=profile,
            checkpoint_chapter=case.history_range[1],
            pair_id=comparison.pair_id,
            request_id=comparison.request_id,
            comparison_basis_fingerprint=(comparison.deterministic.comparison_basis_fingerprint),
            comparable=comparison.comparable,
            blockers=comparison.blockers,
            deterministic_metrics=deterministic,
            agentic_metrics=agentic,
            delta_metrics=delta_metrics,
            accuracy_gain=(agentic.gold_evidence_recall > deterministic.gold_evidence_recall),
            tool_call_reduction=(agentic.retrieval_call_count < deterministic.retrieval_call_count),
            safety_regression=(
                arm_c_safety_regression
                if self._controller_mode is ControllerMode.DETERMINISTIC_PLUS_AGENTIC_DELTA
                else (
                    agentic.future_leakage_count > deterministic.future_leakage_count
                    or agentic.mandatory_constraint_coverage
                    < deterministic.mandatory_constraint_coverage
                )
            ),
        )

    def _configuration_fingerprint(self, bundle_hash: ArtifactId) -> ArtifactId:
        return content_id(
            {
                "runner": self.version,
                "bundle_hash": bundle_hash.root,
                "backend": "in-memory-oracle",
                "controller_policy": "route-bound-v0.1",
                "controller_mode": self._controller_mode.value,
                "max_rounds": 2,
                "max_calls_formula": "2*need_count",
                "max_candidates": self._max_candidates,
                "token_budget": self._token_budget,
                "allowed_tools": sorted(CHANNEL_BY_TOOL),
                "information_profiles": [item.value for item in BenchmarkInformationProfile],
            }
        )

    @classmethod
    def _metrics(
        cls,
        case: BenchmarkCaseManifest,
        arm: PairedContextArmResult,
    ) -> PairedPilotArmMetrics:
        context: Stage1ContextPackage = arm.context
        selected = tuple(
            candidate
            for trace in context.retrieval_traces
            for candidate in trace.candidates
            if candidate.selected
        )
        evidence = tuple(ref for candidate in selected for ref in candidate.unit.evidence_refs)
        all_gold = (
            *case.observed_use_gold,
            *case.operational_constraint_gold,
            *case.plan_obligation_gold,
        )
        mandatory = tuple(item for item in all_gold if item.mandatory)
        gold_evidence = tuple(ref for item in all_gold for ref in item.evidence_refs)
        plan_evidence = tuple(ref for item in all_gold for ref in item.plan_evidence_refs)
        evidence_denominator = len(gold_evidence) + len(plan_evidence)
        matched_evidence = sum(
            any(cls._matches(candidate, expected) for candidate in evidence)
            for expected in gold_evidence
        ) + sum(
            any(cls._matches_plan(candidate.unit, expected) for candidate in selected)
            for expected in plan_evidence
        )
        return PairedPilotArmMetrics(
            gold_evidence_recall=(
                matched_evidence / evidence_denominator if evidence_denominator else 1.0
            ),
            observed_use_coverage=cls._gold_coverage(case.observed_use_gold, evidence, selected),
            operational_constraint_coverage=cls._gold_coverage(
                case.operational_constraint_gold, evidence, selected
            ),
            plan_obligation_coverage=cls._gold_coverage(
                case.plan_obligation_gold, evidence, selected
            ),
            mandatory_constraint_coverage=cls._gold_coverage(mandatory, evidence, selected),
            evidence_traceability=(
                sum(bool(item.unit.evidence_refs or item.unit.source_artifact) for item in selected)
                / len(selected)
                if selected
                else 1.0
            ),
            selected_unit_count=len({item.unit.unit_id for item in selected}),
            retrieval_call_count=arm.retrieval_call_count,
            future_leakage_count=arm.future_leakage_count,
            stop_reason=arm.stop_reason,
        )

    @classmethod
    def _gold_coverage(
        cls,
        gold: tuple[GoldItem, ...],
        evidence: tuple[EvidenceRef, ...],
        selected: tuple[FusedCandidate, ...] = (),
    ) -> float:
        if not gold:
            return 1.0
        return sum(
            any(
                cls._matches(candidate, expected)
                for candidate in evidence
                for expected in item.evidence_refs
            )
            or any(
                cls._matches_plan(candidate.unit, expected)
                for candidate in selected
                for expected in item.plan_evidence_refs
            )
            for item in gold
        ) / len(gold)

    @staticmethod
    def _plan_needs(
        case: Any,
        plan: PlanRootDocument,
        base_commit: CommitId,
        world_needs: tuple[Stage1MemoryNeed, ...],
    ) -> tuple[Stage1MemoryNeed, ...]:
        """Generate plan-based needs from public case fields (case_id, target_range)."""
        if world_needs:
            run_id = world_needs[0].run_id
            task_id = world_needs[0].task_id
        else:
            run_id = RunId(f"run.{case.case_id.root}")
            task_id = TaskId(f"task.{case.case_id.root}")
        goals = tuple(
            goal
            for goal in plan.chapter_goals
            if case.target_range[0] <= goal.chapter_index <= case.target_range[1]
        )
        return tuple(
            Stage1MemoryNeed(
                need_id=StableId(f"need.{goal.goal_id.root}"),
                run_id=run_id,
                task_id=task_id,
                base_commit=base_commit,
                horizon_target=case.target_range,
                need_type="plan_obligation",
                query_intent=Stage1QueryIntent.PLAN_NODE,
                query_text=goal.summary,
                predicates=("chapter_goal",),
                access_scope="author_planning",
                allow_plan=True,
                why_needed="author-visible chapter goal constrains the target horizon",
                risk_level=NeedRisk.HIGH,
                requirement=RequirementLevel.MANDATORY,
                preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
                allowed_candidate_pools=(CandidatePool.R1,),
                stop_condition="author-visible Plan goal found",
            )
            for goal in goals
        )

    @staticmethod
    def _scope_needs(
        needs: tuple[Stage1MemoryNeed, ...],
        profile: BenchmarkInformationProfile,
    ) -> tuple[Stage1MemoryNeed, ...]:
        """Inject runtime-owned audience scope without granting plan access to world needs."""

        scope = (
            "author_planning"
            if profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
            else "writer_safe"
        )
        return tuple(need.model_copy(update={"access_scope": scope}) for need in needs)

    @classmethod
    def _evidence_coverage(
        cls,
        gold: tuple[EvidenceRef, ...],
        evidence: tuple[EvidenceRef, ...],
    ) -> float:
        if not gold:
            return 1.0
        return sum(
            any(cls._matches(candidate, expected) for candidate in evidence) for expected in gold
        ) / len(gold)

    @staticmethod
    def _matches(candidate: EvidenceRef, expected: EvidenceRef) -> bool:
        if candidate.evidence_id == expected.evidence_id:
            return True
        if (
            candidate.span is None
            or expected.span is None
            or candidate.object_hash != expected.object_hash
        ):
            return False
        return candidate.span.start < expected.span.end and expected.span.start < candidate.span.end

    @staticmethod
    def _matches_plan(candidate: RetrievalUnit, expected: PlanEvidenceRef) -> bool:
        """Match both legacy Anchor ids and content-addressed R1 plan records.

        Continuous E2E replay projects R1 rows under commit-specific unit ids, so
        ``anchor.<goal_id>`` is not a stable evaluator identity.  The immutable
        goal object hash remains stable and prevents a same-id but semantically
        different Planner proposal from receiving Gold credit.
        """

        if candidate.unit_id == StableId(f"anchor.{expected.goal_id.root}"):
            return candidate.source_artifact == expected.plan_root_hash
        if candidate.information_label != "plan":
            return False
        try:
            record = json.loads(candidate.text)
        except (TypeError, json.JSONDecodeError):
            return False
        return (
            isinstance(record, dict)
            and record.get("goal_id") == expected.goal_id.root
            and content_id(record) == expected.object_hash
        )
