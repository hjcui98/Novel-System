"""Version-pinned Stage 2 Memory Curator BOOTSTRAP agent facade."""

from __future__ import annotations

from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ProjectId, SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    CuratorBootstrapDraft,
    ProposalProvenance,
    WorldPatchCandidate,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes


class CuratorBootstrapInvocationError(ValueError):
    pass


class CuratorBootstrapAgent:
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
        project_id: ProjectId,
        source_ids: tuple[StableId, ...],
        source_payload: str,
        source_artifacts: tuple[ArtifactRef, ...],
        request: ModelRequest,
    ) -> tuple[WorldPatchCandidate, ModelCallRecord]:
        if len(source_artifacts) != len(source_ids) or len(
            {artifact.artifact_id for artifact in source_artifacts}
        ) != len(source_artifacts):
            raise CuratorBootstrapInvocationError(
                "Curator BOOTSTRAP sources require unique artifact bindings"
            )
        prepared = self._runner.prepare(
            AgentType.MEMORY_CURATOR,
            AgentMode.BOOTSTRAP,
            version.root,
            request,
            f"SOURCE_IDS={[item.root for item in source_ids]}\nSOURCE_DATA={source_payload}",
            source_hashes=tuple(artifact.artifact_id for artifact in source_artifacts),
            input_artifacts=source_artifacts,
        )
        execution = await self._runner.execute(prepared, CuratorBootstrapDraft)
        draft = execution.output
        allowed_sources = set(source_ids)
        if any(
            item.provenance is ProposalProvenance.AUTHOR_SUPPLIED
            and not set(item.source_ids).issubset(allowed_sources)
            for item in draft.items
        ):
            raise CuratorBootstrapInvocationError(
                "Curator BOOTSTRAP draft cites a source outside the task"
            )
        output_artifact = self._artifacts.put(
            canonical_json_bytes(draft.model_dump(mode="json")),
            "application/vnd.novel-agent.curator-bootstrap-draft+json",
            version,
        )
        receipt = self._runner.receipt(
            prepared,
            execution.model_call,
            output_artifacts=(output_artifact,),
            unresolved=draft.unresolved_claims,
        )
        digest = output_artifact.artifact_id.root.removeprefix("sha256:")[:24]
        return (
            WorldPatchCandidate(
                proposal_id=StableId(f"world-patch.{digest}"),
                project_id=project_id,
                items=draft.items,
                origin_source_ids=source_ids,
                unresolved_claims=draft.unresolved_claims,
                extraction_coverage=draft.extraction_coverage,
                receipt=receipt,
            ),
            execution.model_call,
        )
