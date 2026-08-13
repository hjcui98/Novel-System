"""Stage 5 adapter for one atomic accepted-Draft Chapter Settlement."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from novel_agent.adapters.memory_write import InformationBoundaryRegistryAdapter
from novel_agent.adapters.runtime.materializers import DraftCandidateMaterializer
from novel_agent.domain.artifacts import RootKind
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import CommitResult, ValidationStatus
from novel_agent.domain.creative_runtime import AcceptedCandidateBinding
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory_write import (
    ChapterRevealTrigger,
    CuratorWorldProposalInput,
    InformationBoundary,
    MemoryWriteBudget,
    MemoryWriteCommitProfile,
    MemoryWriteWorkflowRequest,
    MemoryWriteWorkflowResult,
    NarrativePosition,
    RootUpdateIntent,
    RootUpdateKind,
    SourceProvenance,
)
from novel_agent.domain.stage2 import AccessScope, ContractRef
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.memory_write_workflow import LocalMemoryWriteWorkflow


@dataclass(frozen=True, slots=True)
class ChapterSettlementPolicy:
    """Pinned Stage 2W contracts used by the Stage 5 settlement caller."""

    curator_agent_spec: ContractRef
    boundary_policy_ref: ContractRef
    tool_policy_ref: ContractRef
    repair_policy_ref: ContractRef
    configuration_fingerprint: ArtifactId
    prompt_contract_refs: tuple[ContractRef, ...] = ()
    skill_contract_refs: tuple[ContractRef, ...] = ()
    budget: MemoryWriteBudget = field(default_factory=MemoryWriteBudget)


class AtomicChapterSettlementAdapter:
    """Convert accepted Stage 3 prose into the existing Stage 2W atomic workflow."""

    is_fixture = False

    def __init__(
        self,
        *,
        workflow: LocalMemoryWriteWorkflow,
        draft_materializer: DraftCandidateMaterializer,
        commits: CommitService,
        artifacts: ArtifactRepository,
        boundary_registry: InformationBoundaryRegistryAdapter,
        policy: ChapterSettlementPolicy,
        reveal_text: Callable[[TextRootDocument], None],
    ) -> None:
        self._workflow = workflow
        self._draft_materializer = draft_materializer
        self._commits = commits
        self._artifacts = artifacts
        self._boundaries = boundary_registry
        self._policy = policy
        self._reveal_text = reveal_text

    @property
    def draft_materializer(self) -> DraftCandidateMaterializer:
        return self._draft_materializer

    @staticmethod
    def effect_identity(accepted: AcceptedCandidateBinding) -> StableId:
        return StableId(f"chapter-settlement.{accepted.acceptance_id.root}"[:128])

    def resolve_commit(self, accepted: AcceptedCandidateBinding) -> CommitResult | None:
        return self._commits.result_for_idempotency(
            accepted.project_id, self.effect_identity(accepted)
        )

    async def settle(
        self, accepted: AcceptedCandidateBinding
    ) -> MemoryWriteWorkflowResult:
        if self._commits.current_commit(accepted.project_id) != accepted.expected_project_commit:
            raise ValueError("Chapter Settlement basis is no longer current")
        manifest = self._commits.load_manifest(accepted.expected_project_commit)
        text_bundle, validation = self._draft_materializer.materialize(accepted)
        if validation.status is not ValidationStatus.PASSED:
            raise ValueError("accepted Draft did not pass trusted text materialization")
        updated_text_ref = text_bundle.proposed_roots.text_root
        if updated_text_ref.artifact_id == manifest.text_root.artifact_id:
            raise ValueError("accepted Draft did not advance TextRoot")
        updated_text = TextRootDocument.model_validate_json(
            self._artifacts.read_verified(updated_text_ref), strict=True
        )
        if not updated_text.chapters:
            raise ValueError("Chapter Settlement produced an empty TextRoot")
        chapter = updated_text.chapters[-1]
        position = NarrativePosition(chapter_index=chapter.chapter_index)
        boundary = InformationBoundary(
            boundary_id=StableId(f"boundary.{accepted.acceptance_id.root}"[:128]),
            base_commit=accepted.expected_project_commit,
            reveal_position=position,
            maximum_visible_position=position,
            evaluator_sources_forbidden=True,
            policy_ref=self._policy.boundary_policy_ref,
        )
        source = accepted.candidate.artifact_ref
        visibility = self._boundaries.register_visibility(
            source=source,
            boundary=boundary,
            position=position,
            access_scope=AccessScope.WRITER_SAFE,
            provenance=SourceProvenance.REVEALED_TEXT,
        )
        text_producer = self._boundaries.register_derivation(
            output=updated_text_ref,
            inputs=(source,),
            visibility_receipts=(visibility,),
            boundary=boundary,
            policy=self._policy.boundary_policy_ref,
            position=position,
            access_scope=AccessScope.WRITER_SAFE,
        )
        text_intent = RootUpdateIntent(
            intent_id=StableId(f"intent.text.{accepted.acceptance_id.root}"[:128]),
            root_kind=RootKind.TEXT,
            update_kind=RootUpdateKind.REPLACE,
            expected_base_root=manifest.text_root,
            update_artifact=updated_text_ref,
            producer_receipt=text_producer,
            builder_policy_ref=self._policy.boundary_policy_ref,
        )
        self._reveal_text(updated_text)
        request = MemoryWriteWorkflowRequest(
            request_id=self.effect_identity(accepted),
            run_id=accepted.run_id,
            task_id=accepted.task_id,
            project_id=accepted.project_id,
            trigger=ChapterRevealTrigger(
                chapter_id=chapter.chapter_id,
                chapter_index=chapter.chapter_index,
                reveal_position=position,
            ),
            commit_profile=MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC,
            base_commit=accepted.expected_project_commit,
            source_artifacts=(source,),
            root_update_intents=(text_intent,),
            world_mutation=CuratorWorldProposalInput(
                curator_agent_spec=self._policy.curator_agent_spec
            ),
            canonical_root_refs=manifest,
            information_boundary=boundary,
            source_visibility_receipts=(visibility,),
            access_scope=AccessScope.WRITER_SAFE,
            source_provenance=(SourceProvenance.REVEALED_TEXT,),
            configuration_fingerprint=self._policy.configuration_fingerprint,
            prompt_contract_refs=self._policy.prompt_contract_refs,
            skill_contract_refs=self._policy.skill_contract_refs,
            tool_policy_ref=self._policy.tool_policy_ref,
            repair_policy_ref=self._policy.repair_policy_ref,
            budget=self._policy.budget,
            idempotency_key=self.effect_identity(accepted),
        )
        return await self._workflow.execute(request)


__all__ = ["AtomicChapterSettlementAdapter", "ChapterSettlementPolicy"]
