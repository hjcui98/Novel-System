"""Version-pinned Stage 2 Memory Curator REPLAY agent facade."""

from __future__ import annotations

from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import CommitId, SchemaVersion
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.stage2 import AgentMode, AgentType, CuratorReplayResult
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.model_curation import ModelCurator


class CuratorReplayAgent:
    def __init__(self, curator: ModelCurator, runner: StructuredAgentRunner) -> None:
        self._curator = curator
        self._runner = runner

    @property
    def curator(self) -> ModelCurator:
        return self._curator

    async def run(
        self,
        *,
        version: SchemaVersion,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        current_world: WorldRootDocument,
        request: ModelRequest,
    ) -> tuple[CuratorReplayResult, ModelCallRecord]:
        prepared = self._runner.prepare(
            AgentType.MEMORY_CURATOR,
            AgentMode.REPLAY,
            version.root,
            request,
            (
                f"chapter_index={chapter_index}\n"
                f"base_commit={base_commit.root}\n"
                "Output ChapterChangeDraft only; the trusted service binds evidence and IDs."
            ),
            source_hashes=(text_root.root_hash,),
            base_commit=base_commit,
        )
        changes, call, draft = await self._curator.extract_reported(
            text_root,
            chapter_index,
            base_commit,
            current_world,
            prepared.request,
            contract_prompt=prepared.rendered_prompt,
        )
        output_bytes = canonical_json_bytes(changes.model_dump(mode="json"))
        output_artifact = ArtifactRef(
            artifact_id=sha256_id(output_bytes),
            media_type="application/vnd.novel-agent.observed-change-set+json",
            byte_length=len(output_bytes),
            schema_version=version,
        )
        receipt = self._runner.receipt(
            prepared,
            call,
            output_artifacts=(output_artifact,),
            unresolved=draft.unresolved,
        )
        return (
            CuratorReplayResult(
                observed_changes=changes,
                coverage=draft.coverage,
                unresolved=draft.unresolved,
                declared_vs_observed_diff=draft.declared_vs_observed_diff,
                receipt=receipt,
            ),
            call,
        )
