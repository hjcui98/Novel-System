"""Real adapter to the public Stage 4 Planner Context Loop boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.creative_runtime import (
    CandidateBinding,
    CandidateKind,
    PlanningLoopRequest,
    PlanningLoopResult,
    PlanningTerminalStatus,
)
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.planning import (
    PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
    PlanningBudgets,
)
from novel_agent.domain.planning import (
    PlanningLoopRequest as Stage4PlanningLoopRequest,
)
from novel_agent.domain.planning import (
    PlanningLoopResult as Stage4PlanningLoopResult,
)
from novel_agent.domain.planning import (
    PlanningLoopTerminal as Stage4PlanningLoopTerminal,
)
from novel_agent.domain.stage2 import AgentMode, PlanningTask
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.planning_context_loop import (
    ModelRequestFactory,
    PlanningContextLoopService,
)

PLAN_PROPOSAL_MEDIA_TYPE = "application/vnd.novel-agent.plan-proposal+json"


@dataclass(frozen=True, slots=True)
class Stage4PlanningInvocation:
    request: Stage4PlanningLoopRequest
    model_request: ModelRequestFactory
    world: WorldRootDocument | None = None
    text_root: TextRootDocument | None = None
    resume_checkpoint_ref: ArtifactRef | None = None


@dataclass(frozen=True, slots=True)
class Stage4InvocationPolicy:
    budgets: PlanningBudgets
    configuration_fingerprint: ArtifactId
    model_fingerprint: ArtifactId
    allowed_skill_ids: tuple[StableId, ...] = ()
    explicit_author_overrides: tuple[str, ...] = ()
    model_role: ModelRole = ModelRole.IMPLEMENTATION
    model_purpose: ModelCallPurpose = ModelCallPurpose.DEVELOPMENT
    model_timeout_seconds: float = 120.0
    model_max_output_tokens: int = 8_000


class ProductionStage4InvocationFactory:
    """Project one durable Stage 5 planning task into the public Stage 4 loop."""

    is_fixture = False

    def __init__(
        self,
        *,
        commits: CommitService,
        artifacts: ArtifactRepository,
        policy: Stage4InvocationPolicy,
    ) -> None:
        self._commits = commits
        self._artifacts = artifacts
        self._policy = policy

    def __call__(self, request: PlanningLoopRequest) -> Stage4PlanningInvocation:
        if request.basis_snapshot is None:
            raise ValueError("production Stage 4 invocation requires an exact snapshot")
        if request.horizon_start is None or request.horizon_end is None:
            raise ValueError("production Stage 4 invocation requires a rolling horizon")
        if self._commits.current_commit(request.project_id) != request.basis_commit:
            raise ValueError("Stage 4 task basis is not the current project commit")
        if not request.input_artifact_refs:
            raise ValueError("Stage 4 CHAPTER_SET requires author-intent artifacts")
        manifest = self._commits.load_manifest(request.basis_commit)
        if manifest.project_id != request.project_id:
            raise ValueError("Stage 4 task and canonical manifest belong to different projects")
        text = TextRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.text_root), strict=True
        )
        world = WorldRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.world_root), strict=True
        )
        latest = text.chapters[-1].chapter_index if text.chapters else 0
        if latest != request.chapter_index:
            raise ValueError(
                f"Stage 4 task starts at chapter {request.chapter_index}, "
                f"but TextRoot ends at {latest}"
            )
        if request.horizon_start <= request.chapter_index:
            raise ValueError("Stage 4 horizon must begin after the committed chapter")
        source_ids = tuple(
            StableId(f"source.author-intent.{ref.artifact_id.root[-24:]}")
            for ref in request.input_artifact_refs
        )
        retrieval = self._policy.budgets.retrieval
        tranche_count = request.planner_memory_budget_extensions + 1
        effective_budgets = self._policy.budgets.model_copy(
            update={
                "retrieval": retrieval.model_copy(
                    update={
                        "max_rounds": retrieval.max_rounds * tranche_count,
                        "max_tool_calls": retrieval.max_tool_calls * tranche_count,
                        "max_anchor_expansions": (retrieval.max_anchor_expansions * tranche_count),
                        "max_full_chapter_reads": (
                            retrieval.max_full_chapter_reads * tranche_count
                        ),
                        "wall_clock_budget_ms": (retrieval.wall_clock_budget_ms * tranche_count),
                        "token_budget": retrieval.token_budget * tranche_count,
                    }
                )
            }
        )
        planning_task = PlanningTask(
            planning_task_id=StableId(f"planning-task.{request.task_id.root}"[:128]),
            project_id=request.project_id,
            mode=AgentMode.CHAPTER_SET,
            base_commit=request.basis_commit,
            source_ids=source_ids,
            creative_scope=(
                f"chapters:{request.horizon_start}-{request.horizon_end}",
                f"purpose:{request.purpose.value}",
            ),
        )
        detailed = Stage4PlanningLoopRequest(
            request_id=StableId(f"planning-request.{request.task_id.root}"[:128]),
            run_id=request.run_id,
            task_id=request.task_id,
            project_id=request.project_id,
            task=planning_task,
            author_intent_artifacts=request.input_artifact_refs,
            accepted_plan_ref=manifest.plan_root,
            accepted_world_ref=manifest.world_root,
            accepted_text_ref=manifest.text_root,
            project_profile_ref=manifest.project_profile_root,
            snapshot_id=request.basis_snapshot,
            explicit_author_overrides=self._policy.explicit_author_overrides,
            horizon_start=request.horizon_start,
            horizon_end=request.horizon_end,
            allowed_skill_ids=self._policy.allowed_skill_ids,
            budgets=effective_budgets,
            configuration_fingerprint=self._policy.configuration_fingerprint,
            model_fingerprint=self._policy.model_fingerprint,
        )
        resume_checkpoint_ref = next(
            (
                ref
                for ref in reversed(request.continuation_artifact_refs)
                if ref.media_type == PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE
            ),
            None,
        )

        def model_request(phase: str, mode: AgentMode, attempt: int) -> ModelRequest:
            suffix = f"{phase}.{attempt}"
            return ModelRequest(
                request_id=StableId(f"model-request.{request.task_id.root}.{suffix}"[:128]),
                run_id=request.run_id,
                task_id=request.task_id,
                model_role=self._policy.model_role,
                purpose=self._policy.model_purpose,
                trace_id=f"trace.{request.run_id.root}.{request.task_id.root}"[:256],
                prompt="",
                agent_mode=mode.value,
                max_output_tokens=self._policy.model_max_output_tokens,
                timeout_seconds=self._policy.model_timeout_seconds,
            )

        return Stage4PlanningInvocation(
            request=detailed,
            model_request=model_request,
            world=world,
            text_root=text,
            resume_checkpoint_ref=resume_checkpoint_ref,
        )


class Stage4PlanningLeafAdapter:
    """Map Stage 5 task identity to one complete, candidate-only Stage 4 loop."""

    is_fixture = False

    def __init__(
        self,
        loop: PlanningContextLoopService,
        artifacts: ArtifactRepository,
        invocation_factory: Callable[[PlanningLoopRequest], Stage4PlanningInvocation],
        *,
        schema_version: SchemaVersion,
    ) -> None:
        self._loop = loop
        self._artifacts = artifacts
        self._invocation_factory = invocation_factory
        self._schema_version = schema_version

    @property
    def invocation_factory(
        self,
    ) -> Callable[[PlanningLoopRequest], Stage4PlanningInvocation]:
        return self._invocation_factory

    async def run(self, request: PlanningLoopRequest) -> PlanningLoopResult:
        invocation = self._invocation_factory(request)
        detailed = invocation.request
        if (
            detailed.run_id != request.run_id
            or detailed.task_id != request.task_id
            or detailed.project_id != request.project_id
            or detailed.task.base_commit != request.basis_commit
            or detailed.snapshot_id != request.basis_snapshot
        ):
            raise ValueError("Stage 4 request factory violated the durable task basis")
        if not set(detailed.author_intent_artifacts).issubset(request.input_artifact_refs):
            raise ValueError("Stage 4 request introduced an unbound author-intent artifact")

        result = await self._loop.run(
            request=detailed,
            model_request=invocation.model_request,
            world=invocation.world,
            text_root=invocation.text_root,
            resume_checkpoint_ref=invocation.resume_checkpoint_ref,
        )
        if result.request_id != detailed.request_id:
            raise RuntimeError("Stage 4 Planner returned cross-request lineage")
        if result.terminal is Stage4PlanningLoopTerminal.PLAN_CANDIDATE_READY:
            assert result.proposal is not None
            proposal_ref = self._artifacts.put(
                canonical_json_bytes(result.proposal.model_dump(mode="json")),
                PLAN_PROPOSAL_MEDIA_TYPE,
                self._schema_version,
            )
            lineage = self._lineage(result, proposal_ref)
            candidate = CandidateBinding(
                candidate_id=StableId(f"plan-candidate.{result.proposal.proposal_id.root}"[:128]),
                kind=CandidateKind.PLAN,
                artifact_ref=proposal_ref,
                candidate_hash=proposal_ref.artifact_id.root,
                basis_commit=request.basis_commit,
                basis_snapshot=request.basis_snapshot,
                lineage_artifact_refs=lineage,
            )
            return PlanningLoopResult(
                result_id=StableId(f"{request.task_id.root}.planner-result"),
                run_id=request.run_id,
                task_id=request.task_id,
                status=PlanningTerminalStatus.PLAN_CANDIDATE_READY,
                candidate=candidate,
                artifact_refs=lineage,
            )

        status = self._terminal(result.terminal)
        diagnostic = (
            result.diagnostic_codes[0] if result.diagnostic_codes else result.terminal.value
        )
        return PlanningLoopResult(
            result_id=StableId(f"{request.task_id.root}.planner-result"),
            run_id=request.run_id,
            task_id=request.task_id,
            status=status,
            artifact_refs=result.event_artifacts,
            failure_code=diagnostic[:128],
            failure_detail=f"Stage 4 terminal: {result.terminal.value}"[:512],
        )

    @staticmethod
    def _lineage(
        result: Stage4PlanningLoopResult, proposal_ref: ArtifactRef
    ) -> tuple[ArtifactRef, ...]:
        refs = [proposal_ref]
        for name in (
            "inquiry_ref",
            "inquiry_review_ref",
            "memory_context_ref",
            "planner_context_ref",
            "plan_review_ref",
        ):
            ref = getattr(result, name)
            if ref is not None:
                refs.append(ref)
        refs.extend(result.event_artifacts)
        return tuple({ref.artifact_id: ref for ref in refs}.values())

    @staticmethod
    def _terminal(terminal: Stage4PlanningLoopTerminal) -> PlanningTerminalStatus:
        if terminal is Stage4PlanningLoopTerminal.YIELDED:
            return PlanningTerminalStatus.YIELDED
        if terminal in {
            Stage4PlanningLoopTerminal.MODEL_UNAVAILABLE,
            Stage4PlanningLoopTerminal.SUSPENDED,
        }:
            return PlanningTerminalStatus.SUSPENDED
        if terminal in {
            Stage4PlanningLoopTerminal.INQUIRY_REVIEW_REQUIRED,
            Stage4PlanningLoopTerminal.PLAN_CONFLICT,
            Stage4PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
            Stage4PlanningLoopTerminal.HUMAN_REQUIRED,
            Stage4PlanningLoopTerminal.DEGRADED_NOT_PROMOTABLE,
            Stage4PlanningLoopTerminal.REVIEW_REQUIRED,
        }:
            return PlanningTerminalStatus.REVIEW_REQUIRED
        return PlanningTerminalStatus.BLOCKED


__all__ = [
    "ProductionStage4InvocationFactory",
    "Stage4InvocationPolicy",
    "Stage4PlanningInvocation",
    "Stage4PlanningLeafAdapter",
]
