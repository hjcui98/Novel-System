"""Version-pinned Stage 2 Memory Curator REPLAY agent facade."""

from __future__ import annotations

from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import CommitId, SchemaVersion
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    CuratorEvidenceContract,
    CuratorReplayResult,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.model_curation import ModelCurator


class CuratorReplayAgent:
    def __init__(
        self,
        curator: ModelCurator,
        runner: StructuredAgentRunner,
        *,
        evidence_contract: CuratorEvidenceContract = CuratorEvidenceContract.CANDIDATE_ID_V2,
    ) -> None:
        self._curator = curator
        self._runner = runner
        self._evidence_contract = evidence_contract

    @property
    def curator(self) -> ModelCurator:
        return self._curator

    @property
    def evidence_contract(self) -> CuratorEvidenceContract:
        return self._evidence_contract

    async def run(
        self,
        *,
        version: SchemaVersion,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        current_world: WorldRootDocument,
        request: ModelRequest,
        proposal_feedback: str | None = None,
    ) -> tuple[CuratorReplayResult, ModelCallRecord]:
        if self._evidence_contract is CuratorEvidenceContract.CANDIDATE_ID_V2:
            return await self._run_v2(
                version=version,
                text_root=text_root,
                chapter_index=chapter_index,
                base_commit=base_commit,
                current_world=current_world,
                request=request,
                proposal_feedback=proposal_feedback,
            )
        return await self._run_v1(
            version=version,
            text_root=text_root,
            chapter_index=chapter_index,
            base_commit=base_commit,
            current_world=current_world,
            request=request,
            proposal_feedback=proposal_feedback,
        )

    async def _run_v1(
        self,
        *,
        version: SchemaVersion,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        current_world: WorldRootDocument,
        request: ModelRequest,
        proposal_feedback: str | None,
    ) -> tuple[CuratorReplayResult, ModelCallRecord]:
        task_payload = (
            f"chapter_index={chapter_index}\n"
            f"base_commit={base_commit.root}\n"
            "Output ChapterChangeDraft only; the trusted service binds evidence and IDs."
        )
        if proposal_feedback is not None:
            task_payload += (
                '\n<PROPOSAL_REPAIR_FEEDBACK trusted="true">\n'
                + proposal_feedback
                + "\n</PROPOSAL_REPAIR_FEEDBACK>"
            )
        prepared = self._runner.prepare(
            AgentType.MEMORY_CURATOR,
            AgentMode.REPLAY,
            version.root,
            request,
            task_payload,
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

    async def _run_v2(
        self,
        *,
        version: SchemaVersion,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        current_world: WorldRootDocument,
        request: ModelRequest,
        proposal_feedback: str | None,
    ) -> tuple[CuratorReplayResult, ModelCallRecord]:
        task_payload = (
            f"chapter_index={chapter_index}\n"
            f"base_commit={base_commit.root}\n"
            "Output ChapterChangeDraftV2 only; cite registered evidence_candidate_ids. "
            "Always emit the operations key. An empty array requires a complete explicit "
            "no-durable-delta proof using no_durable_delta_reason and "
            "no_op_evidence_candidate_ids; incomplete empty output is rejected. "
            "The trusted service binds all offsets, hashes and EvidenceRef values."
        )
        prepared = self._runner.prepare(
            AgentType.MEMORY_CURATOR,
            AgentMode.REPLAY,
            version.root,
            request,
            task_payload,
            source_hashes=(text_root.root_hash,),
            base_commit=base_commit,
        )
        changes, call, draft = await self._curator.extract_reported_v2(
            text_root,
            chapter_index,
            base_commit,
            current_world,
            prepared.request,
            contract_prompt=prepared.rendered_prompt,
            repair_feedback=proposal_feedback,
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
