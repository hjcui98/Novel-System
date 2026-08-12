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
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.planning import (
    PlanningLoopRequest as Stage4PlanningLoopRequest,
)
from novel_agent.domain.planning import (
    PlanningLoopResult as Stage4PlanningLoopResult,
)
from novel_agent.domain.planning import (
    PlanningLoopTerminal as Stage4PlanningLoopTerminal,
)
from novel_agent.services.artifacts import ArtifactRepository
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
            result.diagnostic_codes[0]
            if result.diagnostic_codes
            else result.terminal.value
        )
        return PlanningLoopResult(
            result_id=StableId(f"{request.task_id.root}.planner-result"),
            run_id=request.run_id,
            task_id=request.task_id,
            status=status,
            artifact_refs=result.event_artifacts,
            failure_code=f"stage4_{diagnostic}"[:128],
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


__all__ = ["Stage4PlanningInvocation", "Stage4PlanningLeafAdapter"]
