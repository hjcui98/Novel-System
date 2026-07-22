"""Complete six-mode, version-pinned Stage 2 Planner agent facade."""

from __future__ import annotations

from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.stage2 import (
    AgentType,
    PlannerExecutionResult,
    PlannerProposalDraft,
    PlanningTask,
    PlanProposal,
    ProjectIntentModel,
    ProjectProfileProposal,
    ProposalProvenance,
    WorldDesignProposal,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes


class PlannerInvocationError(ValueError):
    pass


class PlannerAgent:
    def __init__(
        self,
        runner: StructuredAgentRunner,
        artifacts: ArtifactRepository,
    ) -> None:
        self._runner = runner
        self._artifacts = artifacts

    async def run(
        self,
        *,
        version: SchemaVersion,
        task: PlanningTask,
        source_payload: str,
        source_artifacts: tuple[ArtifactRef, ...],
        request: ModelRequest,
    ) -> tuple[PlannerExecutionResult, ModelCallRecord]:
        if len(source_artifacts) != len(task.source_ids) or len(
            {artifact.artifact_id for artifact in source_artifacts}
        ) != len(source_artifacts):
            raise PlannerInvocationError("PlanningTask sources require unique artifact bindings")
        prepared = self._runner.prepare(
            AgentType.PLANNER,
            task.mode,
            version.root,
            request,
            f"PLANNING_TASK={task.model_dump_json()}\nSOURCE_DATA={source_payload}",
            source_hashes=tuple(artifact.artifact_id for artifact in source_artifacts),
            input_artifacts=source_artifacts,
            base_commit=task.base_commit,
        )
        execution = await self._runner.execute(prepared, PlannerProposalDraft)
        draft = execution.output
        if draft.mode is not task.mode or draft.strategy is not task.strategy:
            raise PlannerInvocationError("Planner draft mode/strategy differs from trusted task")
        allowed_sources = set(task.source_ids)
        authored_items = (
            *draft.project_intent_items,
            *draft.plan_items,
            *draft.world_design_items,
            *draft.profile_items,
        )
        if any(
            item.provenance is ProposalProvenance.AUTHOR_SUPPLIED
            and not set(item.source_ids).issubset(allowed_sources)
            for item in authored_items
        ):
            raise PlannerInvocationError("Planner draft cites a source outside PlanningTask")
        output_artifact = self._artifacts.put(
            canonical_json_bytes(draft.model_dump(mode="json")),
            "application/vnd.novel-agent.planner-proposal-draft+json",
            version,
        )
        receipt = self._runner.receipt(
            prepared,
            execution.model_call,
            output_artifacts=(output_artifact,),
            unresolved=draft.unresolved,
        )
        digest = output_artifact.artifact_id.root.removeprefix("sha256:")[:24]
        plan = PlanProposal(
            proposal_id=StableId(f"plan-proposal.{digest}"),
            project_id=task.project_id,
            mode=task.mode,
            strategy=task.strategy,
            base_commit=task.base_commit,
            items=draft.plan_items,
            unresolved=draft.unresolved,
            coverage=draft.coverage,
            receipt=receipt,
        )
        intent = (
            ProjectIntentModel(
                intent_id=StableId(f"project-intent.{digest}"),
                project_id=task.project_id,
                strategy=task.strategy,
                items=draft.project_intent_items,
                source_ids=task.source_ids,
                unresolved=draft.unresolved,
                coverage=draft.coverage,
            )
            if task.strategy is not None
            else None
        )
        world = (
            WorldDesignProposal(
                proposal_id=StableId(f"world-design.{digest}"),
                project_id=task.project_id,
                items=draft.world_design_items,
                unresolved=draft.unresolved,
            )
            if draft.world_design_items
            else None
        )
        profile = (
            ProjectProfileProposal(
                proposal_id=StableId(f"profile-proposal.{digest}"),
                project_id=task.project_id,
                items=draft.profile_items,
                unresolved=draft.unresolved,
            )
            if draft.profile_items
            else None
        )
        return (
            PlannerExecutionResult(
                mode=task.mode,
                project_intent=intent,
                plan_proposal=plan,
                world_design=world,
                project_profile=profile,
                deviations=draft.deviations,
                output_artifact=output_artifact,
                receipt=receipt,
            ),
            execution.model_call,
        )
