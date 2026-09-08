"""Trusted Stage 5 adapters from accepted leaf candidates to canonical root bundles."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypeVar, cast

from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    RootKind,
    RootManifest,
    TextRootRef,
    WorldRootRef,
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
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId, bounded_stable_id
from novel_agent.domain.memory import (
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    TemporalObligationError,
    WorldRootDocument,
    require_not_before_for_kind,
)
from novel_agent.domain.planning import (
    PlanningLoopEventReceipt,
    PlanReview,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.runtime import TaskPurpose
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    ExecutionStatus,
    PlannerExecutionResult,
    PlanProposal,
    ProposedItem,
)
from novel_agent.domain.text import TextBlock
from novel_agent.domain.world import PlanLevel, PlanNode
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.ports.creative_runtime import (
    CandidateMaterializationError,
    DraftLengthContractError,
)
from novel_agent.services.artifacts import ArtifactIntegrityError, ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import (
    canonical_json_bytes,
    content_id,
    plan_root_content_id,
    world_root_content_id,
)
from novel_agent.services.text_timeline import SequentialTextRootService
from novel_agent.services.writer_cognition import draft_surface_error

PLAN_PROPOSAL_MEDIA_TYPE = "application/vnd.novel-agent.plan-proposal+json"
PLAN_REVIEW_MEDIA_TYPE = "application/vnd.novel-agent.plan-review+json"
PLANNING_EVENT_MEDIA_TYPE = "application/vnd.novel-agent.planning-loop-event+json"
PLANNER_EXECUTION_MEDIA_TYPE = "application/vnd.novel-agent.planner-execution-result+json"
PLAN_ROOT_MEDIA_TYPE = "application/vnd.novel-agent.plan-root+json"
WORLD_ROOT_MEDIA_TYPE = "application/vnd.novel-agent.world-root+json"
TEXT_ROOT_MEDIA_TYPE = "application/vnd.novel-agent.text-root+json"
WRITING_LOOP_RESULT_MEDIA_TYPE = "application/vnd.novel-agent.writing-loop-result+json"
RECONCILIATION_MEDIA_TYPE = "application/vnd.novel-agent.reconciliation+json"
_KIND_PLAN_LEVEL = {
    "story": PlanLevel.STORY,
    "arc_volume": PlanLevel.ARC_VOLUME,
    "volume": PlanLevel.ARC_VOLUME,
    "volume_scope": PlanLevel.ARC_VOLUME,
    "chapter_set": PlanLevel.CHAPTER_SET,
}
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

    def _base(self, accepted: AcceptedCandidateBinding, kind: CandidateKind) -> RootManifest:
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
    def _one(refs: Iterable[ArtifactRef], media_type: str, *, label: str) -> ArtifactRef:
        matches = tuple(
            {ref.artifact_id: ref for ref in refs if ref.media_type == media_type}.values()
        )
        if len(matches) != 1:
            raise CandidateMaterializationError(f"candidate requires exactly one {label}")
        return matches[0]

    @staticmethod
    def _stable_id(prefix: str, value: str) -> StableId:
        """Preserve the source identity when a readable prefix would overflow."""

        return bounded_stable_id(f"{prefix}.{value}", value)

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
        current = self._normalize_plan_root_nodes(self._read(base.plan_root, PlanRootDocument))
        text = self._read(base.text_root, TextRootDocument)
        current_chapter = text.chapters[-1].chapter_index if text.chapters else 0
        trusted_level = self._trusted_plan_level(proposal.mode)
        invalidated = self._effective_invalidated_ids(
            current,
            execution,
            review,
            current_chapter=current_chapter,
        )
        self._assert_single_plan_level(proposal.items, trusted_level, mode=proposal.mode)
        story_parent = next(
            (node.plan_node_id for node in current.nodes if node.plan_level is PlanLevel.STORY),
            None,
        )
        volume_parent = next(
            (
                node.plan_node_id
                for node in current.nodes
                if node.plan_level is PlanLevel.ARC_VOLUME
                and (
                    candidate.horizon_start is None
                    or (
                        node.chapter_start is not None
                        and node.chapter_end is not None
                        and node.chapter_start <= candidate.horizon_start
                        and (
                            candidate.horizon_end is None
                            or candidate.horizon_end <= node.chapter_end
                        )
                    )
                )
            ),
            None,
        )
        if trusted_level is PlanLevel.CHAPTER_SET:
            if candidate.horizon_start is None or candidate.horizon_end is None:
                raise CandidateMaterializationError(
                    "CHAPTER_SET candidate requires a complete horizon"
                )
            if volume_parent is None:
                raise CandidateMaterializationError(
                    "CHAPTER_SET candidate requires a covering ARC_VOLUME parent"
                )
            chapter_numbers = tuple(
                sorted(
                    chapter_number
                    for item in proposal.items
                    if (chapter_number := self._chapter_number(item.payload)) is not None
                )
            )
            expected_chapters = tuple(range(candidate.horizon_start, candidate.horizon_end + 1))
            if chapter_numbers != expected_chapters:
                raise CandidateMaterializationError(
                    "CHAPTER_SET candidate must contain one chapter item for every horizon chapter"
                )
            wrapper = self._chapter_set_wrapper(
                proposal.items,
                parent_id=volume_parent,
                horizon_start=candidate.horizon_start,
                horizon_end=candidate.horizon_end,
            )
            valid_parent_ids = {node.plan_node_id.root for node in current.nodes} | {
                wrapper.plan_node_id.root,
                *(item.item_id.root for item in proposal.items),
            }
            incoming_nodes = (
                wrapper,
                *tuple(
                    self._node(
                        item,
                        plan_level=PlanLevel.CHAPTER,
                        default_parent_id=wrapper.plan_node_id,
                        required_parent_id=wrapper.plan_node_id,
                        valid_parent_ids=valid_parent_ids,
                        candidate_start=candidate.horizon_start,
                        candidate_end=candidate.horizon_end,
                    )
                    for item in proposal.items
                ),
            )
        else:
            valid_parent_ids = {node.plan_node_id.root for node in current.nodes} | {
                item.item_id.root for item in proposal.items
            }
            incoming_nodes = tuple(
                self._node(
                    item,
                    plan_level=trusted_level,
                    default_parent_id=story_parent,
                    valid_parent_ids=valid_parent_ids,
                    candidate_start=candidate.horizon_start,
                    candidate_end=candidate.horizon_end,
                )
                for item in proposal.items
            )
        if (
            trusted_level is PlanLevel.CHAPTER_SET
            and candidate.horizon_start is not None
            and candidate.horizon_end is not None
        ):
            invalidated.update(
                self._chapter_set_replacement_ids(
                    current,
                    horizon_start=candidate.horizon_start,
                    horizon_end=candidate.horizon_end,
                    current_chapter=current_chapter,
                )
            )
        incoming_goals = tuple(
            goal for item in proposal.items if (goal := self._chapter_goal(item)) is not None
        )
        world = self._read(base.world_root, WorldRootDocument)
        world, world_ref, obligation_bindings = self._bind_obligation_declarations(
            world,
            proposal,
            trusted_level=trusted_level,
        )
        incoming_nodes = tuple(
            cast(
                PlanNode,
                self._attach_obligations(node, obligation_bindings.get(node.plan_node_id, ())),
            )
            for node in incoming_nodes
        )
        incoming_goals = tuple(
            cast(
                ChapterGoal,
                self._attach_obligations(goal, obligation_bindings.get(goal.goal_id, ())),
            )
            for goal in incoming_goals
        )
        self._validate_obligation_references(world, incoming_nodes, incoming_goals, proposal)
        if trusted_level in {PlanLevel.STORY, PlanLevel.ARC_VOLUME} and (
            candidate.horizon_start is not None or candidate.horizon_end is not None
        ):
            raise CandidateMaterializationError(
                "STORY/ARC_VOLUME candidates cannot use rolling horizon"
            )
        if (
            (trusted_level is PlanLevel.CHAPTER_SET or trusted_level is None)
            and candidate.horizon_start is not None
            and candidate.horizon_end is not None
        ):
            expected_chapters = tuple(range(candidate.horizon_start, candidate.horizon_end + 1))
            actual_chapters = tuple(sorted(goal.chapter_index for goal in incoming_goals))
            if actual_chapters != expected_chapters:
                raise CandidateMaterializationError(
                    "Plan candidate must provide exactly one chapter goal for every "
                    "chapter in its accepted horizon"
                )
        self._validate_temporal_obligation_use(
            current=current,
            incoming_nodes=incoming_nodes,
            incoming_goals=incoming_goals,
            proposal=proposal,
            base=base,
            candidate=candidate,
            world=world,
        )
        incoming_node_ids = {item.plan_node_id for item in incoming_nodes}
        incoming_goal_ids = {item.goal_id for item in incoming_goals}
        nodes = (
            tuple(
                item
                for item in current.nodes
                if item.plan_node_id not in invalidated
                and item.plan_node_id not in incoming_node_ids
            )
            + incoming_nodes
        )
        keep_goals = []
        for item in current.chapter_goals:
            if item.goal_id in invalidated or item.goal_id in incoming_goal_ids:
                continue
            if (
                trusted_level is PlanLevel.CHAPTER_SET
                and candidate.horizon_start is not None
                and candidate.horizon_end is not None
                and candidate.horizon_start <= item.chapter_index <= candidate.horizon_end
                and item.chapter_index > current_chapter
            ):
                continue
            keep_goals.append(item)
        goals = tuple(keep_goals) + incoming_goals
        self._validate_parent_scope(
            nodes,
            trusted_level=trusted_level,
            horizon_start=candidate.horizon_start,
            horizon_end=candidate.horizon_end,
        )
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
        root_ref = PlanRootRef(**root_artifact.model_dump(mode="python"), root_kind=RootKind.PLAN)
        produced_artifacts: list[ArtifactRef] = [
            root_ref,
            candidate.artifact_ref,
            review_ref,
            execution_ref,
        ]
        proposed_roots = base.model_copy(
            update={
                "plan_root": root_ref,
                "parent_commit_ids": (accepted.expected_project_commit,),
            }
        )
        if world_ref is not None:
            proposed_roots = proposed_roots.model_copy(update={"world_root": world_ref})
            produced_artifacts.append(world_ref)
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
            produced_artifacts=tuple(produced_artifacts),
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

    @staticmethod
    def _payload_text(payload: Mapping[str, object], *keys: str) -> str | None:
        for key in (*keys, "description", "primary_conflict", "scope_boundaries"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    @staticmethod
    def _chapter_number(payload: Mapping[str, object]) -> int | None:
        if "chapter_index" in payload:
            raw = payload["chapter_index"]
        elif "chapter" in payload:
            raw = payload["chapter"]
        else:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise CandidateMaterializationError("Plan item chapter_index must be an integer")
        return raw

    @classmethod
    def _node(
        cls,
        item: ProposedItem,
        *,
        plan_level: PlanLevel | None = None,
        default_parent_id: StableId | None = None,
        required_parent_id: StableId | None = None,
        valid_parent_ids: set[str] | None = None,
        candidate_start: int | None = None,
        candidate_end: int | None = None,
    ) -> PlanNode:
        summary = cls._payload_text(
            item.payload,
            "summary",
            "goal",
            "primary_conflict",
            "structure",
            "class_system",
            "description",
            "content",
            "overview",
        )
        if summary is None and "summary" not in item.payload and "goal" not in item.payload:
            for val in item.payload.values():
                if isinstance(val, str) and val.strip():
                    summary = val.strip()
                    break
        if "title" in item.payload:
            title = item.payload.get("title")
        else:
            title = summary or item.item_id.root
        parent = item.payload.get("parent_id") or item.payload.get("parent_plan_node_id")
        if not isinstance(summary, str) or not summary.strip():
            raise CandidateMaterializationError("Plan item requires a non-empty summary")
        if not isinstance(title, str) or not title.strip():
            raise CandidateMaterializationError("Plan item title must be a non-empty string")
        if parent is not None and not isinstance(parent, str):
            raise CandidateMaterializationError("Plan item parent_id must be a string")
        if required_parent_id is not None:
            if parent is not None and parent != required_parent_id.root:
                raise CandidateMaterializationError(
                    "CHAPTER items in a CHAPTER_SET must use the current CHAPTER_SET wrapper"
                )
            parent = required_parent_id.root
        raw_start = item.payload.get("chapter_start")
        raw_end = item.payload.get("chapter_end")
        if raw_start is None or raw_end is None:
            for range_key in ("chapter_range", "chapter_window", "target_window", "range"):
                if range_key in item.payload:
                    range_val = item.payload.get(range_key)
                    if isinstance(range_val, dict):
                        if raw_start is None:
                            raw_start = range_val.get("start") or range_val.get("chapter_start")
                        if raw_end is None:
                            raw_end = range_val.get("end") or range_val.get("chapter_end")
                    elif isinstance(range_val, str) and "-" in range_val:
                        parts = range_val.split("-", 1)
                        try:
                            if raw_start is None:
                                raw_start = int(parts[0].strip())
                            if raw_end is None:
                                raw_end = int(parts[1].strip())
                        except ValueError:
                            pass
                    if raw_start is not None and raw_end is not None:
                        break
        if raw_start is None or raw_end is None:
            for vol_key in ("volume_number", "volume_index", "volume_no", "volume"):
                if vol_key in item.payload:
                    vol_num = item.payload.get(vol_key)
                    if isinstance(vol_num, int) and not isinstance(vol_num, bool):
                        if raw_start is None:
                            raw_start = (vol_num - 1) * 100 + 1
                        if raw_end is None:
                            raw_end = vol_num * 100
                    if raw_start is not None and raw_end is not None:
                        break
        if (
            raw_start is None
            and candidate_start is not None
            and (
                plan_level is PlanLevel.CHAPTER_SET
                or cls._declared_plan_level(item) is PlanLevel.CHAPTER_SET
            )
        ):
            raw_start = candidate_start
            raw_end = candidate_end
        if (
            raw_start is None
            and isinstance(item.payload.get("chapter_index"), int)
            and not isinstance(item.payload.get("chapter_index"), bool)
            and plan_level is PlanLevel.CHAPTER
        ):
            raw_start = item.payload["chapter_index"]
            raw_end = item.payload["chapter_index"]
        chapter_start = (
            raw_start if isinstance(raw_start, int) and not isinstance(raw_start, bool) else None
        )
        chapter_end = (
            raw_end if isinstance(raw_end, int) and not isinstance(raw_end, bool) else None
        )
        if parent is None:
            deps = item.payload.get("dependencies")
            if (
                isinstance(deps, (list, tuple))
                and deps
                and isinstance(deps[0], str)
                and (valid_parent_ids is None or deps[0] in valid_parent_ids)
            ):
                parent = deps[0]
            elif default_parent_id is not None and (
                plan_level in {PlanLevel.ARC_VOLUME, PlanLevel.CHAPTER_SET, PlanLevel.CHAPTER}
                or cls._declared_plan_level(item)
                in {PlanLevel.ARC_VOLUME, PlanLevel.CHAPTER_SET, PlanLevel.CHAPTER}
            ):
                parent = default_parent_id.root
        if parent is not None and valid_parent_ids is not None and parent not in valid_parent_ids:
            parent = default_parent_id.root if default_parent_id is not None else None

        item_level = plan_level
        if plan_level is PlanLevel.ARC_VOLUME:
            is_volume = (
                item.kind in {"arc_volume", "volume", "volume_scope", "volume_arc", "arc"}
                or "volume" in item.kind.lower()
                or "arc" in item.kind.lower()
                or chapter_start is not None
                or cls._declared_plan_level(item) is PlanLevel.ARC_VOLUME
            )
            if not is_volume:
                item_level = None

        return PlanNode(
            plan_node_id=item.item_id,
            node_type=item.kind,
            title=title,
            summary=summary,
            parent_id=None if parent is None else StableId(parent),
            obligation_ids=cls._ids(item.payload.get("obligation_ids"), "obligation_ids"),
            source_ids=item.source_ids,
            payload=dict(item.payload),
            plan_level=item_level,
            chapter_start=chapter_start,
            chapter_end=chapter_end,
        )

    @classmethod
    def _chapter_set_wrapper(
        cls,
        items: tuple[ProposedItem, ...],
        *,
        parent_id: StableId,
        horizon_start: int,
        horizon_end: int,
    ) -> PlanNode:
        source_ids = tuple(
            dict.fromkeys(source_id for item in items for source_id in item.source_ids)
        )
        wrapper_id = bounded_stable_id(
            f"chapter-set.{parent_id.root}.{horizon_start}.{horizon_end}",
            "chapter-set."
            + content_id(
                {
                    "parent": parent_id.root,
                    "start": horizon_start,
                    "end": horizon_end,
                }
            ).root.removeprefix("sha256:")[:48],
        )
        return PlanNode(
            plan_node_id=wrapper_id,
            node_type="chapter_set",
            title=f"ChapterSet {horizon_start}-{horizon_end}",
            summary=f"Structural chapter window {horizon_start}-{horizon_end}",
            parent_id=parent_id,
            source_ids=source_ids,
            payload={
                "horizon_start": horizon_start,
                "horizon_end": horizon_end,
                "structural_wrapper": True,
            },
            plan_level=PlanLevel.CHAPTER_SET,
            chapter_start=horizon_start,
            chapter_end=horizon_end,
        )

    @classmethod
    def _chapter_goal(cls, item: ProposedItem) -> ChapterGoal | None:
        chapter_index = cls._chapter_number(item.payload)
        if chapter_index is None:
            import re

            m = re.search(r"(?:chapter|ch)[._-]?(\d+)", item.item_id.root, re.IGNORECASE)
            if not m:
                m = re.search(r"g(\d+)", item.item_id.root, re.IGNORECASE)
            if m:
                chapter_index = int(m.group(1))
        if chapter_index is None:
            return None
        summary = cls._payload_text(item.payload, "summary", "goal")
        if not isinstance(summary, str) or not summary.strip():
            raise CandidateMaterializationError("Chapter goal requires a non-empty summary")
        return ChapterGoal(
            goal_id=item.item_id,
            chapter_index=chapter_index,
            summary=summary,
            obligation_ids=cls._ids(item.payload.get("obligation_ids"), "obligation_ids"),
            source_ids=item.source_ids,
            payload=dict(item.payload),
        )

    @classmethod
    def _normalize_plan_root_nodes(cls, current: PlanRootDocument) -> PlanRootDocument:
        import re

        updated_nodes = []
        for node in current.nodes:
            if (
                node.node_type in {"arc_volume", "volume", "volume_scope", "volume_arc"}
                or "vol_" in node.plan_node_id.root
                or "volume" in node.node_type.lower()
            ):
                plan_level = node.plan_level or PlanLevel.ARC_VOLUME
                c_start = node.chapter_start
                c_end = node.chapter_end
                if c_start is None or c_end is None:
                    m = re.search(r"vol_?(\d+)", node.plan_node_id.root)
                    if m:
                        vol_num = int(m.group(1))
                        c_start = c_start or ((vol_num - 1) * 100 + 1)
                        c_end = c_end or (vol_num * 100)
                node = node.model_copy(
                    update={
                        "plan_level": plan_level,
                        "chapter_start": c_start,
                        "chapter_end": c_end,
                    }
                )
            updated_nodes.append(node)
        return current.model_copy(update={"nodes": tuple(updated_nodes)})

    @staticmethod
    def _trusted_plan_level(mode: AgentMode) -> PlanLevel | None:
        mapping = {
            AgentMode.STORY: PlanLevel.STORY,
            AgentMode.ARC_VOLUME: PlanLevel.ARC_VOLUME,
            AgentMode.CHAPTER_SET: PlanLevel.CHAPTER_SET,
            AgentMode.CHAPTER: PlanLevel.CHAPTER,
            AgentMode.SCENE: PlanLevel.SCENE,
        }
        return mapping.get(mode)

    @classmethod
    def _declared_plan_level(cls, item: ProposedItem) -> PlanLevel | None:
        raw = item.payload.get("plan_level")
        if isinstance(raw, str) and raw.strip():
            try:
                return PlanLevel(raw)
            except ValueError as error:
                raise CandidateMaterializationError(
                    "Plan item plan_level is not a valid PlanLevel"
                ) from error
        return _KIND_PLAN_LEVEL.get(item.kind)

    @classmethod
    def _assert_single_plan_level(
        cls,
        items: tuple[ProposedItem, ...],
        trusted_level: PlanLevel | None,
        *,
        mode: AgentMode | None = None,
    ) -> None:
        if trusted_level is None:
            return
        if trusted_level is PlanLevel.CHAPTER_SET:
            mixed = tuple(
                item.item_id.root
                for item in items
                if (declared := cls._declared_plan_level(item)) not in {None, PlanLevel.CHAPTER}
            )
            if mixed:
                raise CandidateMaterializationError(
                    "CHAPTER_SET proposals must contain chapter items, not another wrapper"
                )
            return
        mixed = tuple(
            item.item_id.root
            for item in items
            if (declared := cls._declared_plan_level(item)) is not None
            and declared is not trusted_level
        )
        if mixed:
            raise CandidateMaterializationError(
                "a post-Genesis Plan candidate may only produce one PlanLevel"
            )

    @staticmethod
    def _effective_invalidated_ids(
        current: PlanRootDocument,
        execution: PlannerExecutionResult,
        review: PlanReview,
        *,
        current_chapter: int,
    ) -> set[StableId]:
        roots = {
            item_id
            for deviation in execution.deviations
            for item_id in deviation.affected_plan_item_ids
        } - set(review.preserve_item_ids)
        children: dict[StableId, list[StableId]] = {}
        by_id = {node.plan_node_id: node for node in current.nodes}
        for node in current.nodes:
            if node.parent_id is None:
                continue
            children.setdefault(node.parent_id, []).append(node.plan_node_id)
        descendants: set[StableId] = set()
        stack = list(roots)
        while stack:
            current_id = stack.pop()
            for child in children.get(current_id, ()):
                if child not in descendants:
                    descendants.add(child)
                    stack.append(child)

        def committed(node_id: StableId) -> bool:
            node = by_id.get(node_id)
            if node is None or node.chapter_end is None:
                return False
            if node.plan_level not in {PlanLevel.CHAPTER, PlanLevel.SCENE, None}:
                return False
            return node.chapter_end <= current_chapter

        return {item for item in roots | descendants if not committed(item)}

    @staticmethod
    def _chapter_set_replacement_ids(
        current: PlanRootDocument,
        *,
        horizon_start: int,
        horizon_end: int,
        current_chapter: int,
    ) -> set[StableId]:
        """Remove only uncommitted structures for the horizon being replaced."""

        by_id = {node.plan_node_id: node for node in current.nodes}
        children: dict[StableId, list[StableId]] = {}
        for node in current.nodes:
            if node.parent_id is not None:
                children.setdefault(node.parent_id, []).append(node.plan_node_id)

        roots = {
            node.plan_node_id
            for node in current.nodes
            if node.plan_level is PlanLevel.CHAPTER_SET
            and node.chapter_start == horizon_start
            and node.chapter_end == horizon_end
            and node.chapter_end > current_chapter
        }
        volume_ids = {
            node.plan_node_id for node in current.nodes if node.plan_level is PlanLevel.ARC_VOLUME
        }
        roots.update(
            node.plan_node_id
            for node in current.nodes
            if node.plan_level is PlanLevel.CHAPTER
            and node.chapter_start is not None
            and node.chapter_end is not None
            and horizon_start <= node.chapter_start
            and node.chapter_end <= horizon_end
            and node.parent_id in volume_ids
            and node.chapter_end > current_chapter
        )

        def committed(node: PlanNode) -> bool:
            return (
                node.plan_level in {PlanLevel.CHAPTER, PlanLevel.SCENE, None}
                and node.chapter_end is not None
                and node.chapter_end <= current_chapter
            )

        invalidated: set[StableId] = set()
        stack = list(roots)
        while stack:
            node_id = stack.pop()
            node = by_id[node_id]
            if committed(node) or node_id in invalidated:
                continue
            invalidated.add(node_id)
            stack.extend(children.get(node_id, ()))
        return invalidated

    @staticmethod
    def _validate_parent_scope(
        nodes: tuple[PlanNode, ...],
        *,
        trusted_level: PlanLevel | None = None,
        horizon_start: int | None = None,
        horizon_end: int | None = None,
    ) -> None:
        by_id = {node.plan_node_id: node for node in nodes}
        legacy_direct_chapter = trusted_level is PlanLevel.CHAPTER and not any(
            node.plan_level is PlanLevel.STORY for node in nodes
        )
        for node in nodes:
            if node.plan_level is PlanLevel.ARC_VOLUME:
                if node.parent_id is None:
                    if legacy_direct_chapter:
                        continue
                    raise CandidateMaterializationError("ARC_VOLUME nodes require a STORY parent")
                parent = by_id.get(node.parent_id)
                if parent is None:
                    raise CandidateMaterializationError("ARC_VOLUME parent does not exist")
                if parent.plan_level is not PlanLevel.STORY:
                    raise CandidateMaterializationError("ARC_VOLUME parent must be a STORY node")
                if node.chapter_start is None or node.chapter_end is None:
                    raise CandidateMaterializationError(
                        "ARC_VOLUME nodes require a bounded chapter range"
                    )
                if (
                    parent.chapter_start is not None
                    and parent.chapter_end is not None
                    and (
                        node.chapter_start < parent.chapter_start
                        or node.chapter_end > parent.chapter_end
                    )
                ):
                    raise CandidateMaterializationError("child plan scope exceeds parent scope")
                continue
            if trusted_level is PlanLevel.CHAPTER_SET and node.plan_level is PlanLevel.CHAPTER:
                if node.parent_id is None:
                    raise CandidateMaterializationError(
                        "CHAPTER nodes in a CHAPTER_SET require a CHAPTER_SET parent"
                    )
                chapter_parent = by_id.get(node.parent_id)
                if chapter_parent is None:
                    raise CandidateMaterializationError("CHAPTER parent does not exist")
                if chapter_parent.plan_level is not PlanLevel.CHAPTER_SET:
                    raise CandidateMaterializationError(
                        "CHAPTER parent must be a CHAPTER_SET node"
                    )
            if node.parent_id is None:
                continue
            parent = by_id.get(node.parent_id)
            if parent is None:
                raise CandidateMaterializationError("plan node parent does not exist")
            if (
                node.plan_level is PlanLevel.CHAPTER_SET
                and parent.plan_level is not PlanLevel.ARC_VOLUME
            ):
                raise CandidateMaterializationError("CHAPTER_SET parent must be an ARC_VOLUME node")
            if parent.chapter_start is None or parent.chapter_end is None:
                continue
            if node.chapter_start is None or node.chapter_end is None:
                continue
            if node.chapter_start < parent.chapter_start or node.chapter_end > parent.chapter_end:
                raise CandidateMaterializationError("child plan scope exceeds parent scope")
        if (
            trusted_level is PlanLevel.CHAPTER_SET
            and horizon_start is not None
            and horizon_end is not None
        ):
            volumes = tuple(node for node in nodes if node.plan_level is PlanLevel.ARC_VOLUME)
            covering = False
            for volume in volumes:
                start = volume.chapter_start
                end = volume.chapter_end
                if start is None or end is None:
                    continue
                if start <= horizon_start and horizon_end <= end:
                    covering = True
                    break
            hierarchy_enabled = any(node.plan_level is PlanLevel.STORY for node in nodes)
            if (volumes or hierarchy_enabled) and not covering:
                raise CandidateMaterializationError(
                    "CHAPTER_SET horizon must fall inside an accepted ARC_VOLUME"
                )
            wrappers = tuple(
                node
                for node in nodes
                if node.plan_level is PlanLevel.CHAPTER_SET
                and node.chapter_start == horizon_start
                and node.chapter_end == horizon_end
            )
            if wrappers and len(wrappers) != 1:
                raise CandidateMaterializationError(
                    "CHAPTER_SET scope requires exactly one structural wrapper"
                )

    @staticmethod
    def _attach_obligations(
        value: PlanNode | ChapterGoal,
        obligation_ids: tuple[StableId, ...],
    ) -> PlanNode | ChapterGoal:
        if not obligation_ids:
            return value
        return value.model_copy(
            update={
                "obligation_ids": tuple(dict.fromkeys((*value.obligation_ids, *obligation_ids)))
            }
        )

    def _bind_obligation_declarations(
        self,
        world: WorldRootDocument,
        proposal: PlanProposal,
        *,
        trusted_level: PlanLevel | None = None,
    ) -> tuple[WorldRootDocument, WorldRootRef | None, dict[StableId, tuple[StableId, ...]]]:
        """Normalize legal declarations into OPEN World obligations.

        Plan identity is host-owned.  A model-provided obligation id is only
        accepted when it matches the bounded id derived from the plan item,
        declaration ordinal, and obligation kind.
        """

        if trusted_level is None:
            mode = getattr(proposal, "mode", None)
            trusted_level = self._trusted_plan_level(mode) if isinstance(mode, AgentMode) else None
        declaration_forbidden = {
            PlanLevel.CHAPTER_SET,
            PlanLevel.CHAPTER,
            PlanLevel.SCENE,
        }
        existing = {item.obligation_id: item for item in world.obligations}
        declarations: list[PlanObligation] = []
        bindings: dict[StableId, list[StableId]] = {}
        obligation_kinds = {kind.value for kind in ObligationKind}
        declaration_keys = {
            "obligation",
            "obligations",
            "obligation_declarations",
            "key_obligations",
            "obligation_declaration",
        }
        for item in proposal.items:
            payload = item.payload
            raw_declarations: list[Mapping[str, object]] = []
            referenced_ids: list[StableId] = []
            raw_references = payload.get("obligation_ids")
            if raw_references is not None:
                if not isinstance(raw_references, (list, tuple)):
                    raise CandidateMaterializationError("obligation_ids must be a string list")
                for raw_reference in raw_references:
                    if not isinstance(raw_reference, str):
                        raise CandidateMaterializationError("obligation_ids must contain strings")
                    referenced_ids.append(StableId(raw_reference))
            raw_actions = payload.get("obligation_actions")
            if raw_actions is not None:
                if not isinstance(raw_actions, (list, tuple)):
                    raise CandidateMaterializationError("obligation_actions must be a list")
                for action in raw_actions:
                    if isinstance(action, dict):
                        raw_reference = action.get("obligation_id") or action.get("id")
                        if not isinstance(raw_reference, str) or not raw_reference.strip():
                            raise CandidateMaterializationError(
                                "obligation action requires a string obligation_id"
                            )
                        referenced_ids.append(StableId(raw_reference.strip()))
                    elif isinstance(action, str):
                        try:
                            candidate_id = StableId(action.strip())
                            if any(o.obligation_id == candidate_id for o in world.obligations):
                                referenced_ids.append(candidate_id)
                        except ValueError:
                            pass
            if referenced_ids:
                bindings[item.item_id] = list(dict.fromkeys(referenced_ids))

            direct_kind = payload.get("obligation_kind") or payload.get("obligation_type")
            item_kind = item.kind.lower()
            has_direct_declaration = direct_kind is not None or item_kind in obligation_kinds | {
                "obligation"
            }
            has_nested_declaration = any(key in payload for key in declaration_keys)
            if trusted_level in declaration_forbidden and (
                has_direct_declaration or has_nested_declaration
            ):
                raise CandidateMaterializationError(
                    f"{trusted_level.value} plans may reference obligation_ids/actions but "
                    "may not declare new obligations"
                )

            if has_direct_declaration:
                direct_payload = dict(payload)
                if direct_kind is None and item_kind in obligation_kinds:
                    direct_payload.setdefault("kind", item_kind)
                raw_declarations.append(direct_payload)
            nested = payload.get("obligation")
            if isinstance(nested, dict):
                raw_declarations.append(nested)
            elif nested is not None:
                raise CandidateMaterializationError("obligation declaration must be an object")
            for key in (
                "obligations",
                "obligation_declarations",
                "key_obligations",
                "obligation_declaration",
            ):
                values = payload.get(key)
                if isinstance(values, dict):
                    values = [values]
                if values is None:
                    continue
                if not isinstance(values, (list, tuple)) or not all(
                    isinstance(value, dict) for value in values
                ):
                    raise CandidateMaterializationError(
                        f"{key} must contain obligation declaration objects"
                    )
                raw_declarations.extend(cast(list[Mapping[str, object]], values))

            for ordinal, raw in enumerate(raw_declarations):
                kind_raw = (
                    raw.get("obligation_kind") or raw.get("kind") or raw.get("obligation_type")
                )
                try:
                    kind = ObligationKind(str(kind_raw))
                except ValueError as error:
                    raise CandidateMaterializationError(
                        "obligation declaration has an unknown kind"
                    ) from error
                description = self._payload_text(raw, "description", "summary", "goal", "text")
                if not description:
                    raise CandidateMaterializationError(
                        "obligation declaration requires a description"
                    )
                expected_id = bounded_stable_id(
                    f"obligation.{item.item_id.root}.{ordinal}.{kind.value}",
                    "obligation."
                    + content_id(
                        {
                            "plan_item_id": item.item_id.root,
                            "ordinal": ordinal,
                            "kind": kind.value,
                        }
                    ).root.removeprefix("sha256:")[:48],
                )
                raw_id = raw.get("obligation_id") or raw.get("id")
                if raw_id is not None:
                    if not isinstance(raw_id, str):
                        raise CandidateMaterializationError(
                            "obligation declaration id must be a string"
                        )
                    try:
                        supplied_id = StableId(raw_id)
                    except ValueError as error:
                        raise CandidateMaterializationError(
                            "obligation declaration id is invalid"
                        ) from error
                    if supplied_id != expected_id:
                        raise CandidateMaterializationError(
                            "obligation declaration id does not match host-derived identity"
                        )
                status_raw = str(raw.get("status") or "open").lower()
                if status_raw in {
                    ObligationStatus.RESOLVED.value,
                    ObligationStatus.ABANDONED.value,
                }:
                    raise CandidateMaterializationError(
                        "Plan actions cannot directly resolve or abandon a World obligation"
                    )
                not_before = self._optional_positive_int(raw.get("not_before_chapter"))
                try:
                    require_not_before_for_kind(kind, not_before)
                    declaration = PlanObligation(
                        obligation_id=expected_id,
                        kind=kind,
                        description=description,
                        status=ObligationStatus.OPEN,
                        owner_ids=self._ids(raw.get("owner_ids"), "obligation owner_ids"),
                        not_before_chapter=not_before,
                        target_chapter_start=self._optional_positive_int(
                            raw.get("target_chapter_start")
                        ),
                        target_chapter_end=self._optional_positive_int(
                            raw.get("target_chapter_end")
                        ),
                        due_chapter=self._optional_positive_int(raw.get("due_chapter")),
                    )
                except (TemporalObligationError, ValueError) as error:
                    raise CandidateMaterializationError(str(error)) from error
                bindings.setdefault(item.item_id, []).append(expected_id)
                bindings[item.item_id] = list(dict.fromkeys(bindings[item.item_id]))
                prior = existing.get(expected_id)
                if prior is not None:
                    if prior != declaration:
                        raise CandidateMaterializationError(
                            f"obligation declaration conflicts with existing {expected_id.root}"
                        )
                    continue
                if any(
                    existing_item.obligation_id == expected_id for existing_item in declarations
                ):
                    continue
                declarations.append(declaration)
        if not declarations:
            return (
                world,
                None,
                {
                    item_id: tuple(dict.fromkeys(obligation_ids))
                    for item_id, obligation_ids in bindings.items()
                },
            )
        updated = world.model_copy(
            update={
                "root_hash": "sha256:" + "0" * 64,
                "obligations": (*world.obligations, *declarations),
            }
        )
        updated = updated.model_copy(update={"root_hash": world_root_content_id(updated)})
        artifact = self._artifacts.put(
            canonical_json_bytes(updated.model_dump(mode="json")),
            WORLD_ROOT_MEDIA_TYPE,
            updated.schema_version,
        )
        return (
            updated,
            WorldRootRef(**artifact.model_dump(mode="python"), root_kind=RootKind.WORLD),
            {
                item_id: tuple(dict.fromkeys(obligation_ids))
                for item_id, obligation_ids in bindings.items()
            },
        )

    @staticmethod
    def _optional_positive_int(value: object) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value < 1:
            raise ValueError("obligation timing fields must be positive integers")
        return value

    @staticmethod
    def _validate_obligation_references(
        world: WorldRootDocument,
        incoming_nodes: tuple[PlanNode, ...],
        incoming_goals: tuple[ChapterGoal, ...],
        proposal: PlanProposal,
    ) -> None:
        known = {item.obligation_id for item in world.obligations}
        referenced = {
            *(obligation_id for node in incoming_nodes for obligation_id in node.obligation_ids),
            *(obligation_id for goal in incoming_goals for obligation_id in goal.obligation_ids),
        }
        for item in proposal.items:
            actions = item.payload.get("obligation_actions")
            if not isinstance(actions, (list, tuple)):
                continue
            for action in actions:
                if isinstance(action, dict):
                    raw_id = action.get("obligation_id") or action.get("id")
                    if not isinstance(raw_id, str) or not raw_id.strip():
                        raise CandidateMaterializationError(
                            "obligation action requires a string obligation_id"
                        )
                    referenced.add(StableId(raw_id.strip()))
                elif isinstance(action, str):
                    try:
                        candidate_id = StableId(action.strip())
                        if candidate_id in known:
                            referenced.add(candidate_id)
                    except ValueError:
                        pass
        unknown = sorted(item.root for item in referenced if item not in known)
        if unknown:
            raise CandidateMaterializationError(
                "Plan references unknown obligation ids: " + ", ".join(unknown)
            )

    def _validate_temporal_obligation_use(
        self,
        *,
        current: PlanRootDocument,
        incoming_nodes: tuple[PlanNode, ...],
        incoming_goals: tuple[ChapterGoal, ...],
        proposal: PlanProposal,
        base: RootManifest,
        candidate: object,
        world: WorldRootDocument | None = None,
    ) -> None:
        if world is None:
            world = self._read(base.world_root, WorldRootDocument)
        text = self._read(base.text_root, TextRootDocument)
        current_chapter = text.chapters[-1].chapter_index if text.chapters else 0
        by_id = {item.obligation_id: item for item in world.obligations}
        try:
            for item in proposal.items:
                self._reject_item_without_required_window(item)
                if not self._item_resolves_obligation(item):
                    continue
                resolution_chapter = self._resolution_chapter(
                    item,
                    incoming_goals,
                    candidate,
                    current_chapter,
                )
                for obligation_id in self._ids(
                    item.payload.get("obligation_ids"), "obligation_ids"
                ):
                    obligation = by_id.get(obligation_id)
                    if obligation is None:
                        raise CandidateMaterializationError(
                            f"Plan references unknown obligation id: {obligation_id.root}"
                        )
                    if obligation.forbids_resolution(resolution_chapter):
                        raise TemporalObligationError(
                            "future-locked obligation cannot be resolved in this planning scope"
                        )
                for obligation_id in self._obligation_action_ids(item.payload):
                    obligation = by_id.get(obligation_id)
                    if obligation is None:
                        raise CandidateMaterializationError(
                            f"Plan references unknown obligation id: {obligation_id.root}"
                        )
                    if obligation.forbids_resolution(resolution_chapter):
                        raise TemporalObligationError(
                            "future-locked obligation cannot be resolved in this planning scope"
                        )
            for obligation in world.obligations:
                for goal in incoming_goals:
                    if obligation.obligation_id not in goal.obligation_ids:
                        continue
                    if not self._goal_resolves(goal, proposal):
                        continue
                    if obligation.forbids_resolution(goal.chapter_index):
                        raise TemporalObligationError(
                            "future-locked obligation cannot be resolved in this planning scope"
                        )
        except TemporalObligationError as error:
            raise CandidateMaterializationError(str(error)) from error

    @classmethod
    def _resolution_chapter(
        cls,
        item: ProposedItem,
        incoming_goals: tuple[ChapterGoal, ...],
        candidate: object,
        current_chapter: int,
    ) -> int:
        try:
            payload_chapter = cls._chapter_number(item.payload)
        except CandidateMaterializationError:
            payload_chapter = None
        if payload_chapter is not None:
            return payload_chapter
        for goal in incoming_goals:
            if goal.goal_id == item.item_id:
                return goal.chapter_index
        horizon_start = getattr(candidate, "horizon_start", None)
        if isinstance(horizon_start, int) and not isinstance(horizon_start, bool):
            return horizon_start
        horizon_end = getattr(candidate, "horizon_end", None)
        if isinstance(horizon_end, int) and not isinstance(horizon_end, bool):
            return horizon_end
        return current_chapter

    @classmethod
    def _reject_item_without_required_window(cls, item: ProposedItem) -> None:
        kind_raw = str(item.payload.get("obligation_kind") or item.kind)
        try:
            kind = ObligationKind(kind_raw)
        except ValueError:
            return
        not_before = item.payload.get("not_before_chapter")
        not_before_chapter = not_before if isinstance(not_before, int) else None
        try:
            require_not_before_for_kind(kind, not_before_chapter)
        except TemporalObligationError as error:
            raise CandidateMaterializationError(str(error)) from error

    @staticmethod
    def _item_resolves_obligation(item: ProposedItem) -> bool:
        payload = item.payload
        markers = {"resolved", "payoff", "resolve"}
        status = str(payload.get("status") or payload.get("obligation_status") or "").lower()
        operation = str(payload.get("operation") or "").lower()
        if status in markers or operation in markers or item.kind.lower() in markers:
            return True
        actions = payload.get("obligation_actions")
        if isinstance(actions, (list, tuple)):
            return any(
                isinstance(action, dict)
                and str(action.get("action") or action.get("operation") or "").lower() in markers
                for action in actions
            )
        return False

    @staticmethod
    def _obligation_action_ids(payload: Mapping[str, object]) -> tuple[StableId, ...]:
        actions = payload.get("obligation_actions")
        if not isinstance(actions, (list, tuple)):
            return ()
        ids: list[StableId] = []
        for action in actions:
            if isinstance(action, dict):
                raw_id = action.get("obligation_id") or action.get("id")
                if isinstance(raw_id, str) and raw_id.strip():
                    try:
                        ids.append(StableId(raw_id.strip()))
                    except ValueError:
                        pass
            elif isinstance(action, str):
                try:
                    ids.append(StableId(action.strip()))
                except ValueError:
                    pass
        return tuple(dict.fromkeys(ids))

    @classmethod
    def _goal_resolves(cls, goal: ChapterGoal, proposal: PlanProposal) -> bool:
        for item in proposal.items:
            if item.item_id == goal.goal_id:
                return cls._item_resolves_obligation(item)
        return False


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
            "draft-candidate." + result.final_candidate_id.root.removeprefix("sha256:")[:48]
        )
        expected_acceptance_task = bounded_stable_id(
            f"{result.task_id.root}.accept",
            f"accept.{candidate.candidate_hash}",
            f"accept.{result.run_id.root}",
        )
        if (
            accepted.task_id.root != expected_acceptance_task.root
            or candidate.candidate_id != expected_candidate_id
        ):
            raise CandidateMaterializationError("Draft candidate task lineage is invalid")
        basis = result.initial_draft.basis
        if (
            basis.project_id != accepted.project_id
            or basis.base_commit != accepted.expected_project_commit
            or basis.plan_artifact.artifact_id != base.plan_root.artifact_id
            or basis.project_profile_artifact.artifact_id != base.project_profile_root.artifact_id
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
        self._enforce_length_contract(text, writing_task)
        chapter_index = writing_task.target_chapter
        language = next(
            (
                constraint.split("：", 1)[1].strip()
                for constraint in writing_task.mandatory_constraints
                if constraint.startswith("正文语言：") and constraint.split("：", 1)[1].strip()
            ),
            None,
        )
        recent_prose: list[tuple[str, bool]] = []
        for prior in sorted(
            (chapter for chapter in current.chapters if chapter.chapter_index < chapter_index),
            key=lambda chapter: chapter.chapter_index,
            reverse=True,
        )[:4]:
            prose = "\n".join(block.text for scene in prior.scenes for block in scene.blocks)
            if not prose:
                continue
            compact = prior.chapter_index != chapter_index - 1
            recent_prose.append((prose if not compact else prose[-1_500:], compact))
        surface_error = draft_surface_error(
            text,
            target_language=language,
            forbidden_reveals=writing_task.forbidden_reveals,
            recent_prose=tuple(recent_prose),
        )
        if surface_error is not None:
            raise CandidateMaterializationError(surface_error)
        chapter_id = self._stable_id(f"chapter.{chapter_index}", accepted.acceptance_id.root)
        scene_id = writing_task.target_scenes[0]
        block_id = self._stable_id(
            f"block.{chapter_index}", result.final_candidate_id.root.removeprefix("sha256:")
        )
        chapter = ChapterDocument(
            chapter_id=chapter_id,
            chapter_index=chapter_index,
            title=f"第{chapter_index}章",
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
        updated, _receipt = self._timeline.append(current, accepted.candidate.candidate_id, chapter)
        root_artifact = self._artifacts.put(
            canonical_json_bytes(updated.model_dump(mode="json")),
            TEXT_ROOT_MEDIA_TYPE,
            updated.schema_version,
        )
        root_ref = TextRootRef(**root_artifact.model_dump(mode="python"), root_kind=RootKind.TEXT)
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

    @staticmethod
    def _enforce_length_contract(text: str, writing_task: WritingTaskContract) -> None:
        length = len(text)
        policy = writing_task.length_policy
        effective_min = policy.minimum_characters
        effective_max = policy.maximum_characters
        if length < effective_min:
            raise DraftLengthContractError(
                "accepted Draft is shorter than trusted WritingTask minimum"
            )
        if length > effective_max:
            raise DraftLengthContractError("accepted Draft exceeds trusted WritingTask maximum")


__all__ = [
    "DraftCandidateMaterializer",
    "PlanCandidateMaterializer",
]
