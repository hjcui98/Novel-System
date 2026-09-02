"""Version-pinned Stage 2 Memory Curator BOOTSTRAP agent facade."""

from __future__ import annotations

import json
from typing import Any

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


def _default_author_source_ids(source_ids: tuple[StableId, ...]) -> list[str]:
    for item in source_ids:
        if item.root == "source.author-initial-brief":
            return [item.root]
    return [source_ids[0].root] if source_ids else []


def _bind_omitted_author_sources(data: Any, default_sources: list[str]) -> Any:
    if not isinstance(data, dict) or not default_sources:
        return data
    items = data.get("items")
    if not isinstance(items, list):
        return data
    bound: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            bound.append(item)
            continue
        provenance = item.get("provenance")
        sources = item.get("source_ids")
        if provenance == ProposalProvenance.AUTHOR_SUPPLIED.value and not sources:
            item = {**item, "source_ids": list(default_sources)}
        bound.append(item)
    return {**data, "items": bound}


def _curator_output_type(source_ids: tuple[StableId, ...]) -> type[CuratorBootstrapDraft]:
    """Bind omitted author source_ids, then parse on the JSON contract path."""

    default_sources = _default_author_source_ids(source_ids)

    class BoundCuratorBootstrapDraft(CuratorBootstrapDraft):
        @classmethod
        def model_validate_json(
            cls, json_data: str | bytes | bytearray, **kwargs: Any
        ) -> CuratorBootstrapDraft:
            if isinstance(json_data, (bytes, bytearray)):
                json_data = json_data.decode()
            payload = _bind_omitted_author_sources(json.loads(json_data), default_sources)
            return CuratorBootstrapDraft.model_validate_json(json.dumps(payload), **kwargs)

        @classmethod
        def model_validate(cls, obj: Any, **kwargs: Any) -> CuratorBootstrapDraft:
            if isinstance(obj, dict):
                payload = _bind_omitted_author_sources(obj, default_sources)
                return CuratorBootstrapDraft.model_validate_json(json.dumps(payload), **kwargs)
            return CuratorBootstrapDraft.model_validate(obj, **kwargs)

    BoundCuratorBootstrapDraft.__name__ = "CuratorBootstrapDraft"
    BoundCuratorBootstrapDraft.__qualname__ = "CuratorBootstrapDraft"
    return BoundCuratorBootstrapDraft


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
        execution = await self._runner.execute(prepared, _curator_output_type(source_ids))
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
