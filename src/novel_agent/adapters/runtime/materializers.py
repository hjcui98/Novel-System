"""Trusted Stage 5 adapters from accepted leaf candidates to canonical root bundles."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    RootKind,
    RootManifest,
    TextRootRef,
)
from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import (
    ChapterDocument,
    ChapterGoal,
    PlanRootDocument,
    SceneDocument,
    TextRootDocument,
)
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ObservedChangeSet,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.creative_runtime import AcceptedCandidateBinding, CandidateKind
from novel_agent.domain.editorial import ReconciliationResult
from novel_agent.domain.generation import WritingTaskContract
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId
from novel_agent.domain.planning import (
    PlanningLoopEventReceipt,
    PlanReview,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.runtime import TaskPurpose
from novel_agent.domain.stage2 import (
    AgentType,
    ExecutionStatus,
    PlannerExecutionResult,
    PlanProposal,
    ProposedItem,
)
from novel_agent.domain.text import TextBlock
from novel_agent.domain.world import PlanNode
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.ports.creative_runtime import CandidateMaterializationError
from novel_agent.services.artifacts import ArtifactIntegrityError, ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import (
    canonical_json_bytes,
    plan_root_content_id,
)
from novel_agent.services.text_timeline import SequentialTextRootService

PLAN_PROPOSAL_MEDIA_TYPE = "application/vnd.novel-agent.plan-proposal+json"
PLAN_REVIEW_MEDIA_TYPE = "application/vnd.novel-agent.plan-review+json"
PLANNING_EVENT_MEDIA_TYPE = "application/vnd.novel-agent.planning-loop-event+json"
PLANNER_EXECUTION_MEDIA_TYPE = "application/vnd.novel-agent.planner-execution-result+json"
PLAN_ROOT_MEDIA_TYPE = "application/vnd.novel-agent.plan-root+json"
TEXT_ROOT_MEDIA_TYPE = "application/vnd.novel-agent.text-root+json"
WRITING_LOOP_RESULT_MEDIA_TYPE = "application/vnd.novel-agent.writing-loop-result+json"
RECONCILIATION_MEDIA_TYPE = "application/vnd.novel-agent.reconciliation+json"
ModelT = TypeVar("ModelT", bound=DomainModel)


class _TrustedMaterializer:
    is_fixture = False

    def __init__(
        self,
        artifacts: ArtifactRepository,
        commits: CommitService,
        *,
        schema_version: SchemaVersion,
    ) -> None:
        self._artifacts = artifacts
        self._commits = commits
        self._schema_version = schema_version

    def _base(
        self, accepted: AcceptedCandidateBinding, kind: CandidateKind
    ) -> RootManifest:
        if accepted.candidate.kind is not kind:
            raise CandidateMaterializationError("materializer received the wrong candidate kind")
        if accepted.candidate.basis_commit != accepted.expected_project_commit:
            raise CandidateMaterializationError("accepted candidate basis differs from acceptance")
        if self._commits.current_commit(accepted.project_id) != accepted.expected_project_commit:
            raise CandidateMaterializationError("accepted candidate basis is no longer current")
        manifest = self._commits.load_manifest(accepted.expected_project_commit)
        if manifest.project_id != accepted.project_id:
            raise CandidateMaterializationError("accepted candidate belongs to another project")
        return manifest

    def _read(self, ref: ArtifactRef, model_type: type[ModelT]) -> ModelT:
        try:
            return model_type.model_validate_json(self._artifacts.read_verified(ref))
        except (ArtifactIntegrityError, ValueError) as error:
            raise CandidateMaterializationError(
                f"invalid {ref.media_type} candidate evidence"
            ) from error

    @staticmethod
    def _one(
        refs: Iterable[ArtifactRef], media_type: str, *, label: str
    ) -> ArtifactRef:
        matches = tuple(
            {ref.artifact_id: ref for ref in refs if ref.media_type == media_type}.values()
        )
        if len(matches) != 1:
            raise CandidateMaterializationError(f"candidate requires exactly one {label}")
        return matches[0]

    @staticmethod
    def _stable_id(prefix: str, value: str) -> StableId:
        return StableId(f"{prefix}.{value}"[:128])

    def _report(
        self, accepted: AcceptedCandidateBinding, bundle: CandidateChangeBundle, profile: str
    ) -> ValidationReport:
        return ValidationReport(
            report_id=self._stable_id("validation", accepted.acceptance_id.root),
            bundle_id=bundle.bundle_id,
            status=ValidationStatus.PASSED,
            schema_version=self._schema_version,
            validation_profile=profile,
            validated_at=accepted.accepted_at,
        )


class PlanCandidateMaterializer(_TrustedMaterializer):
    """Merge one accepted and independently reviewed Stage 4 proposal into PlanRoot."""

    def materialize(
        self, accepted: AcceptedCandidateBinding
    ) -> tuple[CandidateChangeBundle, ValidationReport]:
        try:
            return self._materialize(accepted)
        except CandidateMaterializationError:
            raise
        except ValueError as error:
            raise CandidateMaterializationError("Plan candidate mapping failed") from error

    def _materialize(
        self, accepted: AcceptedCandidateBinding
    ) -> tuple[CandidateChangeBundle, ValidationReport]:
        base = self._base(accepted, CandidateKind.PLAN)
        candidate = accepted.candidate
        if candidate.artifact_ref.media_type != PLAN_PROPOSAL_MEDIA_TYPE:
            raise CandidateMaterializationError("Plan candidate is not a Stage 4 PlanProposal")
        if candidate.planning_purpose is TaskPurpose.LOOKAHEAD:
            raise CandidateMaterializationError("unpromoted lookahead cannot reach PlanRoot")
        proposal = self._read(candidate.artifact_ref, PlanProposal)
        if (
            proposal.project_id != accepted.project_id
            or proposal.base_commit != accepted.expected_project_commit
            or proposal.receipt.agent_type is not AgentType.PLANNER
            or proposal.receipt.status is not ExecutionStatus.SUCCEEDED
            or proposal.receipt.base_commit != accepted.expected_project_commit
            or proposal.unresolved
            or not proposal.items
        ):
            raise CandidateMaterializationError(
                "PlanProposal project, basis, or readiness is invalid"
            )

        review_ref, review = self._accepted_review(
            candidate.lineage_artifact_refs,
            candidate.artifact_ref,
            accepted.expected_project_commit,
        )
        execution_ref, execution = self._planner_execution(
            candidate.lineage_artifact_refs, proposal
        )
        if execution.receipt.status is not ExecutionStatus.SUCCEEDED:
            raise CandidateMaterializationError("Planner execution did not succeed")
        current = self._read(base.plan_root, PlanRootDocument)
        invalidated = {
            item_id
            for deviation in execution.deviations
            for item_id in deviation.affected_plan_item_ids
        } - set(review.preserve_item_ids)
        incoming_nodes = tuple(self._node(item) for item in proposal.items)
        incoming_goals = tuple(
            goal for item in proposal.items if (goal := self._chapter_goal(item)) is not None
        )
        if candidate.horizon_start is not None and candidate.horizon_end is not None:
            expected_chapters = tuple(
                range(candidate.horizon_start, candidate.horizon_end + 1)
            )
            actual_chapters = tuple(
                sorted(goal.chapter_index for goal in incoming_goals)
            )
            if actual_chapters != expected_chapters:
                raise CandidateMaterializationError(
                    "Plan candidate must provide exactly one chapter goal for every "
                    "chapter in its accepted horizon"
                )
        incoming_node_ids = {item.plan_node_id for item in incoming_nodes}
        incoming_goal_ids = {item.goal_id for item in incoming_goals}
        nodes = tuple(
            item
            for item in current.nodes
            if item.plan_node_id not in invalidated
            and item.plan_node_id not in incoming_node_ids
        ) + incoming_nodes
        goals = tuple(
            item
            for item in current.chapter_goals
            if item.goal_id not in invalidated and item.goal_id not in incoming_goal_ids
        ) + incoming_goals
        provisional = current.model_copy(
            update={
                "root_hash": "sha256:" + "0" * 64,
                "nodes": nodes,
                "chapter_goals": tuple(sorted(goals, key=lambda item: item.chapter_index)),
            }
        )
        updated = provisional.model_copy(update={"root_hash": plan_root_content_id(provisional)})
        root_artifact = self._artifacts.put(
            canonical_json_bytes(updated.model_dump(mode="json")),
            PLAN_ROOT_MEDIA_TYPE,
            updated.schema_version,
        )
        root_ref = PlanRootRef(
            **root_artifact.model_dump(mode="python"), root_kind=RootKind.PLAN
        )
        proposed_roots = base.model_copy(
            update={
                "plan_root": root_ref,
                "parent_commit_ids": (accepted.expected_project_commit,),
            }
        )
        bundle = CandidateChangeBundle(
            bundle_id=self._stable_id("bundle", accepted.acceptance_id.root),
            project_id=accepted.project_id,
            run_id=accepted.run_id,
            base_commit=accepted.expected_project_commit,
            observed_changes=ObservedChangeSet(
                change_set_id=self._stable_id("changes", accepted.acceptance_id.root),
                base_commit=accepted.expected_project_commit,
                source_artifact=candidate.artifact_ref,
            ),
            proposed_roots=proposed_roots,
            produced_artifacts=(
                root_ref,
                candidate.artifact_ref,
                review_ref,
                execution_ref,
            ),
        )
        return bundle, self._report(accepted, bundle, "stage5-plan-materializer-v1")

    def _accepted_review(
        self,
        refs: tuple[ArtifactRef, ...],
        proposal_ref: ArtifactRef,
        expected_commit: CommitId,
    ) -> tuple[ArtifactRef, PlanReview]:
        matches: list[tuple[ArtifactRef, PlanReview]] = []
        for ref in refs:
            if ref.media_type != PLAN_REVIEW_MEDIA_TYPE:
                continue
            review = self._read(ref, PlanReview)
            if (
                review.target_kind is ReviewTargetKind.PLAN_PROPOSAL
                and review.target_artifact_ref == proposal_ref
            ):
                matches.append((ref, review))
        if len(matches) != 1:
            raise CandidateMaterializationError(
                "Plan candidate requires one review bound to the accepted proposal"
            )
        ref, review = matches[0]
        if (
            review.decision is not ReviewDecision.ACCEPT
            or any(item.blocking for item in review.issues)
            or review.receipt.agent_type is not AgentType.PLAN_REVIEWER
            or review.receipt.status is not ExecutionStatus.SUCCEEDED
            or review.receipt.base_commit != expected_commit
            or proposal_ref not in review.receipt.input_artifacts
        ):
            raise CandidateMaterializationError("Plan candidate did not pass independent review")
        return ref, review

    def _planner_execution(
        self, refs: tuple[ArtifactRef, ...], proposal: PlanProposal
    ) -> tuple[ArtifactRef, PlannerExecutionResult]:
        nested: dict[object, ArtifactRef] = {}
        for ref in refs:
            if ref.media_type != PLANNING_EVENT_MEDIA_TYPE:
                continue
            event = self._read(ref, PlanningLoopEventReceipt)
            for artifact in event.artifact_refs:
                nested[artifact.artifact_id] = artifact
        matches: list[tuple[ArtifactRef, PlannerExecutionResult]] = []
        for ref in nested.values():
            if ref.media_type != PLANNER_EXECUTION_MEDIA_TYPE:
                continue
            execution = self._read(ref, PlannerExecutionResult)
            if execution.plan_proposal == proposal:
                matches.append((ref, execution))
        if len(matches) != 1:
            raise CandidateMaterializationError(
                "Plan candidate requires one matching Planner execution receipt"
            )
        return matches[0]

    @staticmethod
    def _ids(value: object, field: str) -> tuple[StableId, ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise CandidateMaterializationError(f"Plan item {field} must be a string list")
        return tuple(StableId(item) for item in value)

    @classmethod
    def _node(cls, item: ProposedItem) -> PlanNode:
        summary = item.payload.get("summary")
        title = item.payload.get("title", item.item_id.root)
        parent = item.payload.get("parent_id")
        if not isinstance(summary, str) or not summary.strip():
            raise CandidateMaterializationError("Plan item requires a non-empty summary")
        if not isinstance(title, str) or not title.strip():
            raise CandidateMaterializationError("Plan item title must be a non-empty string")
        if parent is not None and not isinstance(parent, str):
            raise CandidateMaterializationError("Plan item parent_id must be a string")
        return PlanNode(
            plan_node_id=item.item_id,
            node_type=item.kind,
            title=title,
            summary=summary,
            parent_id=None if parent is None else StableId(parent),
            obligation_ids=cls._ids(item.payload.get("obligation_ids"), "obligation_ids"),
        )

    @classmethod
    def _chapter_goal(cls, item: ProposedItem) -> ChapterGoal | None:
        chapter_index = item.payload.get("chapter_index")
        if chapter_index is None:
            return None
        if isinstance(chapter_index, bool) or not isinstance(chapter_index, int):
            raise CandidateMaterializationError("Plan item chapter_index must be an integer")
        summary = item.payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise CandidateMaterializationError("Chapter goal requires a non-empty summary")
        return ChapterGoal(
            goal_id=item.item_id,
            chapter_index=chapter_index,
            summary=summary,
            obligation_ids=cls._ids(item.payload.get("obligation_ids"), "obligation_ids"),
        )


class DraftCandidateMaterializer(_TrustedMaterializer):
    """Append one accepted, reviewed, and reconciled Stage 3 Draft to TextRoot."""

    def __init__(
        self,
        artifacts: ArtifactRepository,
        commits: CommitService,
        *,
        schema_version: SchemaVersion,
        timeline: SequentialTextRootService | None = None,
    ) -> None:
        super().__init__(artifacts, commits, schema_version=schema_version)
        self._timeline = timeline or SequentialTextRootService()

    def materialize(
        self, accepted: AcceptedCandidateBinding
    ) -> tuple[CandidateChangeBundle, ValidationReport]:
        try:
            return self._materialize(accepted)
        except CandidateMaterializationError:
            raise
        except (ArtifactIntegrityError, UnicodeDecodeError, ValueError) as error:
            raise CandidateMaterializationError("Draft candidate mapping failed") from error

    def _materialize(
        self, accepted: AcceptedCandidateBinding
    ) -> tuple[CandidateChangeBundle, ValidationReport]:
        base = self._base(accepted, CandidateKind.DRAFT)
        candidate = accepted.candidate
        result_ref = self._one(
            candidate.lineage_artifact_refs,
            WRITING_LOOP_RESULT_MEDIA_TYPE,
            label="WritingLoopResult",
        )
        result = self._read(result_ref, WritingLoopResult)
        if (
            result.status is not WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
            or result.run_id != accepted.run_id
            or result.final_text_artifact != candidate.artifact_ref
            or result.final_candidate_id is None
            or result.initial_draft is None
            or result.observation is None
            or result.observation_artifact is None
            or result.reconciliation is None
        ):
            raise CandidateMaterializationError("Draft candidate evidence chain is incomplete")
        expected_candidate_id = StableId(
            "draft-candidate."
            + result.final_candidate_id.root.removeprefix("sha256:")[:48]
        )
        if (
            accepted.task_id.root != f"{result.task_id.root}.accept"
            or candidate.candidate_id != expected_candidate_id
        ):
            raise CandidateMaterializationError("Draft candidate task lineage is invalid")
        basis = result.initial_draft.basis
        if (
            basis.project_id != accepted.project_id
            or basis.base_commit != accepted.expected_project_commit
            or basis.plan_artifact.artifact_id != base.plan_root.artifact_id
            or basis.project_profile_artifact.artifact_id
            != base.project_profile_root.artifact_id
            or basis.snapshot_id != candidate.basis_snapshot
        ):
            raise CandidateMaterializationError("Draft candidate basis differs from accepted roots")
        expected_impact = bool(result.observation.changes)
        if candidate.affects_future_plan != expected_impact:
            raise CandidateMaterializationError("Draft future-Plan impact was not preserved")
        reconciliation_ref = self._one(
            candidate.lineage_artifact_refs,
            RECONCILIATION_MEDIA_TYPE,
            label="ReconciliationResult",
        )
        if self._read(reconciliation_ref, ReconciliationResult) != result.reconciliation:
            raise CandidateMaterializationError("Draft reconciliation lineage differs from result")
        if result.observation_artifact not in candidate.lineage_artifact_refs:
            raise CandidateMaterializationError(
                "Draft observation is absent from candidate lineage"
            )
        writing_task = self._read(basis.writing_contract_artifact, WritingTaskContract)
        current = self._read(base.text_root, TextRootDocument)
        text = self._artifacts.read_verified(candidate.artifact_ref).decode("utf-8")
        if not text.strip():
            raise CandidateMaterializationError("accepted Draft text is blank")
        chapter_index = writing_task.target_chapter
        chapter_id = self._stable_id(
            f"chapter.{chapter_index}", accepted.acceptance_id.root
        )
        scene_id = writing_task.target_scenes[0]
        block_id = self._stable_id(
            f"block.{chapter_index}", result.final_candidate_id.root.removeprefix("sha256:")
        )
        chapter = ChapterDocument(
            chapter_id=chapter_id,
            chapter_index=chapter_index,
            title=f"Chapter {chapter_index}",
            scenes=(
                SceneDocument(
                    scene_id=scene_id,
                    scene_index=0,
                    blocks=(
                        TextBlock(
                            block_id=block_id,
                            chapter_id=chapter_id,
                            scene_id=scene_id,
                            narrative_index=0,
                            text=text,
                        ),
                    ),
                ),
            ),
        )
        updated, _receipt = self._timeline.append(
            current, accepted.candidate.candidate_id, chapter
        )
        root_artifact = self._artifacts.put(
            canonical_json_bytes(updated.model_dump(mode="json")),
            TEXT_ROOT_MEDIA_TYPE,
            updated.schema_version,
        )
        root_ref = TextRootRef(
            **root_artifact.model_dump(mode="python"), root_kind=RootKind.TEXT
        )
        proposed_roots = base.model_copy(
            update={
                "text_root": root_ref,
                "parent_commit_ids": (accepted.expected_project_commit,),
            }
        )
        bundle = CandidateChangeBundle(
            bundle_id=self._stable_id("bundle", accepted.acceptance_id.root),
            project_id=accepted.project_id,
            run_id=accepted.run_id,
            base_commit=accepted.expected_project_commit,
            observed_changes=ObservedChangeSet(
                change_set_id=self._stable_id("changes", accepted.acceptance_id.root),
                base_commit=accepted.expected_project_commit,
                source_artifact=result.observation_artifact,
            ),
            proposed_roots=proposed_roots,
            produced_artifacts=(
                root_ref,
                candidate.artifact_ref,
                result_ref,
                result.observation_artifact,
                reconciliation_ref,
            ),
        )
        return bundle, self._report(accepted, bundle, "stage5-draft-materializer-v1")


__all__ = [
    "DraftCandidateMaterializer",
    "PlanCandidateMaterializer",
]
