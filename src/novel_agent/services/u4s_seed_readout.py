"""U4-S benchmark corpus and frozen-readout inputs.

This module owns the deterministic side of the U4-S seed runner: public stream
ingestion, checkpoint roots, post-freeze task construction, and the truthful
text-replay retrieval attestation.  It never loads Gold or future text and it
does not call a model.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from novel_agent.domain.benchmark import (
    AuthorPlanningContext,
    ChapterDocument,
    ChapterGoal,
    PlanRootDocument,
    PreludeDocument,
    SceneDocument,
    TextRootDocument,
    VisibleOutlineNode,
)
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalChannel,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.planning_memory import (
    PLANNER_OUTPUT_SCHEMA_VERSION,
    PlannerArtifactMetadata,
    PlannerFallbackStatus,
    PlannerInvocationArtifact,
)
from novel_agent.domain.retrieval_routing import (
    ChannelCoverage,
    ProjectionAttestation,
    RetrievalBackendProfile,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.domain.stage2 import BenchmarkInformationProfile
from novel_agent.domain.text import TextBlock
from novel_agent.domain.u6_continuous_replay import U6CheckpointBasis
from novel_agent.domain.v05_readout import (
    MemoryIdentitySnapshot,
    V05HistoryAccess,
    V05ReadoutTaskIdentity,
    V05ReadoutTrack,
)
from novel_agent.domain.world import PlanNode
from novel_agent.domain.writer_context import WriterContextSection
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.benchmark_importer import content_id
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.memory_pipeline import AnchorBuilder
from novel_agent.services.plan_conditioned_need_planner import PlannerWorldSummaryBuilder
from novel_agent.services.retrieval import InMemoryRetrievalBackend, RerankService
from novel_agent.services.stage2_retrieval_backend import Stage2RetrievalBackendBundle
from novel_agent.services.text_timeline import SequentialTextRootService

SCHEMA_VERSION = SchemaVersion("1.0.0")
_ZERO = ArtifactId("sha256:" + "0" * 64)
_REPLAY_CHANNELS = (
    RetrievalChannel.ANCHOR_BM25,
    RetrievalChannel.ANCHOR_DENSE,
    RetrievalChannel.GROUNDED_BM25,
    RetrievalChannel.GROUNDED_DENSE,
    RetrievalChannel.HIERARCHY,
)


class U4SSeedInputError(ValueError):
    """The public U4-S bundle cannot produce a safe checkpoint input."""


class _TextReplayReranker:
    """Stable reranker identity for the exact deterministic text replay."""

    profile = "benchmark-text-replay-reranker-v1"

    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        # The replay is intentionally deterministic and does not claim a
        # semantic embedding or external reranker.  Stable input order is the
        # frozen tie-breaker used by the benchmark profile.
        return tuple(float(len(passages) - index) for index, _ in enumerate(passages))


@dataclass(frozen=True, slots=True)
class U4SCheckpointInput:
    """All public, pre-Gold inputs required for one frozen readout."""

    identity: V05ReadoutTaskIdentity
    task: Any
    planning_context: AuthorPlanningContext
    plan: PlanRootDocument
    text: TextRootDocument
    world: WorldRootDocument
    basis_commit: CommitId
    snapshot_id: StableId
    question_text: str | None
    need: Stage1MemoryNeed
    planner_artifact: PlannerInvocationArtifact
    backend_bundle: Stage2RetrievalBackendBundle
    memory_identity: MemoryIdentitySnapshot


class U4SPublicCorpus:
    """Read the public sequential stream once and cache immutable chapters."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._chapters = self._load_chapters()
        self._prelude = self._load_prelude()
        self._question_cache: dict[tuple[int, str], str] = {}
        self._context_cache: dict[tuple[int, V05HistoryAccess], dict[str, Any]] = {}
        self._text_cache: dict[int, TextRootDocument] = {}

    @property
    def chapter_count(self) -> int:
        return max(self._chapters, default=0)

    def continuous_text_roots(
        self, checkpoints: Iterable[int]
    ) -> Iterator[tuple[int, TextRootDocument]]:
        """Yield requested roots while appending the public stream exactly once.

        ``text_root`` is intentionally a random-access helper for U4-S task
        construction.  U6-A needs a separate path whose append count is
        observable and cannot accidentally rebuild every checkpoint from C0.
        """

        requested = set(checkpoints)
        if any(checkpoint < 1 or checkpoint > self.chapter_count for checkpoint in requested):
            raise U4SSeedInputError("continuous replay checkpoint is outside the public stream")
        timeline = SequentialTextRootService()
        current = timeline.empty(SCHEMA_VERSION)
        current, _ = timeline.append(current, self._prelude.prelude_id, self._prelude)
        for chapter_index in range(1, self.chapter_count + 1):
            current, _ = timeline.append(
                current,
                self._chapters[chapter_index].chapter_id,
                self._chapters[chapter_index],
            )
            if chapter_index in requested:
                yield chapter_index, current

    def text_root(self, checkpoint: int) -> TextRootDocument:
        if checkpoint < 0 or checkpoint > self.chapter_count:
            raise U4SSeedInputError(f"checkpoint is outside the public stream: {checkpoint}")
        cached = self._text_cache.get(checkpoint)
        if cached is not None:
            return cached
        chapters = tuple(self._chapters[index] for index in range(1, checkpoint + 1))
        root = TextRootDocument(
            root_hash=_ZERO,
            schema_version=SCHEMA_VERSION,
            prelude=self._prelude,
            chapters=chapters,
        )
        root = root.model_copy(update={"root_hash": _content_text_root(root)})
        self._text_cache[checkpoint] = root
        return root

    def question(self, checkpoint: int, question_id: StableId) -> str:
        key = (checkpoint, question_id.root)
        question = self._question_cache.get(key)
        if question is None:
            path = self.root / "private" / "questions" / f"C{checkpoint:03d}.json"
            if not path.is_file():
                raise U4SSeedInputError(f"question release file is missing: {path}")
            payload = json.loads(path.read_text(encoding="utf-8"))
            for item in payload.get("questions", ()):
                self._question_cache[(checkpoint, str(item["question_id"]))] = str(item["question"])
            question = self._question_cache.get(key)
        if question is None:
            raise U4SSeedInputError(
                f"question is missing from the public release index: {question_id}"
            )
        return question

    def context_task(self, checkpoint: int, access: V05HistoryAccess) -> dict[str, Any]:
        key = (checkpoint, access)
        task = self._context_cache.get(key)
        if task is None:
            path = (
                self.root
                / "private"
                / "context_tasks"
                / f"C{checkpoint:03d}"
                / f"{access.value}.json"
            )
            if not path.is_file():
                raise U4SSeedInputError(f"Context task release file is missing: {path}")
            task = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(task, dict):
                raise U4SSeedInputError(f"Context task release is not an object: {path}")
            self._context_cache[key] = task
        if task is None:
            raise U4SSeedInputError(f"Context task is missing for C{checkpoint:03d}/{access.value}")
        return task

    def checkpoint_input(
        self,
        identity: V05ReadoutTaskIdentity,
        *,
        run_id: RunId,
    ) -> U4SCheckpointInput:
        text = self.text_root(identity.checkpoint_chapter)
        basis_commit = CommitId(text.root_hash.root)
        plan, planning_context = self._plan_and_context(identity)
        target_range = (
            (identity.target_chapter_start, identity.target_chapter_end)
            if identity.track is V05ReadoutTrack.CONTEXT
            else (identity.checkpoint_chapter + 1, identity.checkpoint_chapter + 1)
        )
        assert target_range[0] is not None and target_range[1] is not None
        task_intent = (
            planning_context.task_intent
            if identity.history_access is V05HistoryAccess.AUTHOR_PLAN_CONDITIONED
            else ""
        )
        planning_ref = (
            content_id(planning_context.model_dump(mode="json"))
            if identity.history_access is V05HistoryAccess.AUTHOR_PLAN_CONDITIONED
            else None
        )
        planning_hash = (
            planning_context.source_hash
            if identity.history_access is V05HistoryAccess.AUTHOR_PLAN_CONDITIONED
            else None
        )
        task = build_safe_task_contract(
            case_id=identity.task_id,
            checkpoint_chapter=identity.checkpoint_chapter,
            target_range=(int(target_range[0]), int(target_range[1])),
            information_profile=identity.information_profile,
            task_intent=task_intent,
            planning_context_ref=planning_ref,
            planning_context_hash=planning_hash,
        ).model_copy(update={"task_id": identity.task_id})
        question_text = (
            self.question(identity.checkpoint_chapter, identity.question_id)
            if identity.track is V05ReadoutTrack.QA and identity.question_id is not None
            else None
        )
        query_seed = question_text or planning_context.task_intent
        if not query_seed:
            query_seed = "历史 当前 状态 关系 事件"
        first_block = self._first_history_text(text)
        need = _build_need(
            task=task,
            run_id=run_id,
            query_text=f"{query_seed} {first_block[:96]}",
            allow_plan=identity.history_access is V05HistoryAccess.AUTHOR_PLAN_CONDITIONED,
        )
        need = need.model_copy(update={"base_commit": basis_commit})
        world = _empty_world(basis_commit)
        planner_artifact = _fallback_planner_artifact(task, planning_context, world, need, run_id)
        backend_bundle = _replay_backend(world, text, plan, basis_commit, identity.checkpoint_id)
        memory_identity = _memory_identity(
            basis_commit=basis_commit,
            text=text,
            world=world,
            plan=plan,
            information_profile=identity.information_profile,
        )
        return U4SCheckpointInput(
            identity=identity,
            task=task,
            planning_context=planning_context,
            plan=plan,
            text=text,
            world=world,
            basis_commit=basis_commit,
            snapshot_id=backend_bundle.attestation.snapshot_id,
            question_text=question_text,
            need=need,
            planner_artifact=planner_artifact,
            backend_bundle=backend_bundle,
            memory_identity=memory_identity,
        )

    def checkpoint_input_for_frozen_basis(
        self,
        identity: V05ReadoutTaskIdentity,
        *,
        run_id: RunId,
        basis: U6CheckpointBasis,
        basis_artifacts: ArtifactRepository,
    ) -> U4SCheckpointInput:
        """Build a released task against one U6-A frozen basis.

        Public question/plan release remains owned by this corpus.  The
        checkpoint roots and snapshot, however, come only from the frozen U6-A
        object namespace; the ordinary ``checkpoint_input`` helper is not
        allowed to silently manufacture a second basis.
        """

        if identity.checkpoint_chapter != basis.checkpoint_chapter:
            raise U4SSeedInputError("readout task and frozen basis chapters differ")
        if (
            basis.commit_id is None
            or basis.snapshot_id is None
            or basis.text_root_ref is None
            or basis.world_root_ref is None
            or basis.plan_root_ref is None
            or basis.profile_root_ref is None
        ):
            raise U4SSeedInputError("U6-A readout basis is missing a frozen root")
        released = self.checkpoint_input(identity, run_id=run_id)
        text = TextRootDocument.model_validate_json(
            basis_artifacts.read_verified(basis.text_root_ref), strict=True
        )
        world = WorldRootDocument.model_validate_json(
            basis_artifacts.read_verified(basis.world_root_ref), strict=True
        )
        need = released.need.model_copy(update={"base_commit": basis.commit_id})
        planner_artifact = _fallback_planner_artifact(
            released.task,
            released.planning_context,
            world,
            need,
            run_id,
        )
        backend_bundle = _replay_backend(
            world,
            text,
            released.plan,
            basis.commit_id,
            identity.checkpoint_id,
            snapshot_id=basis.snapshot_id,
        )
        memory_identity = MemoryIdentitySnapshot(
            commit_id=basis.commit_id,
            text_root=basis.text_root_ref.artifact_id,
            world_root=basis.world_root_ref.artifact_id,
            plan_root=basis.plan_root_ref.artifact_id,
            profile_root=basis.profile_root_ref.artifact_id,
        )
        return replace(
            released,
            text=text,
            world=world,
            basis_commit=basis.commit_id,
            snapshot_id=basis.snapshot_id,
            need=need,
            planner_artifact=planner_artifact,
            backend_bundle=backend_bundle,
            memory_identity=memory_identity,
        )

    def _load_chapters(self) -> dict[int, ChapterDocument]:
        stream = self.root / "public" / "stream"
        if not stream.is_dir():
            raise U4SSeedInputError(f"public stream is missing: {stream}")
        chapters: dict[int, ChapterDocument] = {}
        for path in sorted(stream.glob("[0-9][0-9][0-9].txt")):
            index = int(path.stem)
            if index == 0:
                continue
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                raise U4SSeedInputError(f"public chapter is empty: {path}")
            chapter_id = StableId(f"chapter.{index:03d}")
            scene_id = StableId(f"scene.{index:03d}.0")
            block = TextBlock(
                block_id=StableId(f"block.{index:03d}.0"),
                chapter_id=chapter_id,
                scene_id=scene_id,
                narrative_index=0,
                text=raw,
            )
            chapters[index] = ChapterDocument(
                chapter_id=chapter_id,
                chapter_index=index,
                title=raw.splitlines()[0].strip(),
                scenes=(SceneDocument(scene_id=scene_id, scene_index=0, blocks=(block,)),),
            )
        if tuple(chapters) != tuple(range(1, max(chapters, default=0) + 1)):
            raise U4SSeedInputError("public stream chapters are not sequential")
        return chapters

    def _load_prelude(self) -> PreludeDocument:
        path = self.root / "public" / "stream" / "000_prologue_and_frontmatter.txt"
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            raise U4SSeedInputError("public prologue is empty")
        prelude_id = StableId("prelude.0")
        scene_id = StableId("scene.prelude.0")
        block = TextBlock(
            block_id=StableId("block.prelude.0"),
            chapter_id=prelude_id,
            scene_id=scene_id,
            narrative_index=0,
            text=raw,
        )
        return PreludeDocument(
            prelude_id=prelude_id,
            title=raw.splitlines()[0].strip(),
            scenes=(SceneDocument(scene_id=scene_id, scene_index=0, blocks=(block,)),),
        )

    def _plan_and_context(
        self,
        identity: V05ReadoutTaskIdentity,
    ) -> tuple[PlanRootDocument, AuthorPlanningContext]:
        if identity.history_access is V05HistoryAccess.AUTHOR_PLAN_CONDITIONED:
            payload = self.context_task(identity.checkpoint_chapter, identity.history_access)
            plan_payload = payload.get("author_plan") or {}
            nodes: list[PlanNode] = []
            outline: list[VisibleOutlineNode] = []
            goals: list[ChapterGoal] = []
            for stage in plan_payload.get("stages", ()):
                stage_id = StableId(f"plan.{stage['stage_id']}")
                objective = str(stage.get("objective", "")).strip() or "作者计划阶段"
                stage_node = PlanNode(
                    plan_node_id=stage_id,
                    node_type="stage",
                    title=str(stage["stage_id"]),
                    summary=objective,
                )
                nodes.append(stage_node)
                outline.append(
                    VisibleOutlineNode(
                        node_id=stage_id,
                        title=stage_node.title,
                        summary=objective,
                    )
                )
                for index, progression in enumerate(stage.get("progression", ())):
                    node_id = StableId(f"{stage_id.root}.progression.{index}")
                    nodes.append(
                        PlanNode(
                            plan_node_id=node_id,
                            node_type="progression",
                            title=f"{stage['stage_id']} progression {index + 1}",
                            summary=str(progression),
                            parent_id=stage_id,
                        )
                    )
                for raw_chapter, goal in (stage.get("chapter_goals") or {}).items():
                    chapter = int(raw_chapter)
                    goals.append(
                        ChapterGoal(
                            goal_id=StableId(f"goal.{identity.checkpoint_id.root}.{chapter}"),
                            chapter_index=chapter,
                            summary=str(goal),
                        )
                    )
            task_intent = str(plan_payload.get("window_objective", "")).strip()
            if not task_intent:
                raise U4SSeedInputError("APC task has no window objective")
            target_start = identity.target_chapter_start
            target_end = identity.target_chapter_end
            if target_start is None or target_end is None:
                raise U4SSeedInputError("APC context task has no target range")
            plan = PlanRootDocument(
                root_hash=_ZERO,
                schema_version=SCHEMA_VERSION,
                nodes=tuple(nodes),
                chapter_goals=tuple(goals),
            )
            plan = plan.model_copy(update={"root_hash": _content_plan_root(plan)})
            context_payload = {
                "profile": identity.information_profile.value,
                "task_intent": task_intent,
                "target_range": (target_start, target_end),
                "visible_outline_nodes": [item.model_dump(mode="json") for item in outline],
                "chapter_goals": [item.model_dump(mode="json") for item in goals],
                "planner_may_read_plan": True,
            }
            context = AuthorPlanningContext(
                profile=identity.information_profile,
                task_intent=task_intent,
                target_range=(target_start, target_end),
                visible_outline_nodes=tuple(outline),
                chapter_goals=tuple(goals),
                source_hash=content_id(context_payload),
                planner_may_read_plan=True,
            )
            return plan, context
        task_intent = "截至截止点读取历史上下文"
        context_payload = {
            "profile": identity.information_profile.value,
            "task_intent": task_intent,
            "target_range": (
                identity.target_chapter_start or identity.checkpoint_chapter + 1,
                identity.target_chapter_end or identity.checkpoint_chapter + 1,
            ),
            "visible_outline_nodes": [],
            "chapter_goals": [],
            "planner_may_read_plan": False,
        }
        context = AuthorPlanningContext(
            profile=identity.information_profile,
            task_intent=task_intent,
            target_range=(
                identity.target_chapter_start or identity.checkpoint_chapter + 1,
                identity.target_chapter_end or identity.checkpoint_chapter + 1,
            ),
            source_hash=content_id(context_payload),
            planner_may_read_plan=False,
        )
        plan = PlanRootDocument(root_hash=_ZERO, schema_version=SCHEMA_VERSION)
        return plan.model_copy(update={"root_hash": _content_plan_root(plan)}), context

    @staticmethod
    def _first_history_text(text: TextRootDocument) -> str:
        if text.chapters and text.chapters[0].scenes and text.chapters[0].scenes[0].blocks:
            return text.chapters[0].scenes[0].blocks[0].text
        if text.prelude is not None and text.prelude.scenes:
            return text.prelude.scenes[0].blocks[0].text
        return "历史"


def _content_text_root(root: TextRootDocument) -> ArtifactId:
    from novel_agent.services.content_addressing import text_root_content_id

    return text_root_content_id(root)


def _content_plan_root(root: PlanRootDocument) -> ArtifactId:
    from novel_agent.services.content_addressing import plan_root_content_id

    return plan_root_content_id(root)


def _empty_world(commit: CommitId) -> WorldRootDocument:
    from novel_agent.services.content_addressing import world_root_content_id

    world = WorldRootDocument(root_hash=_ZERO, schema_version=SCHEMA_VERSION, source_commit=commit)
    return world.model_copy(update={"root_hash": world_root_content_id(world)})


def _build_need(
    *,
    task: Any,
    run_id: RunId,
    query_text: str,
    allow_plan: bool,
) -> Stage1MemoryNeed:
    task_id = TaskId(task.task_id.root)
    need_id = StableId(f"need.u4s.{task.task_id.root}"[:128])
    return Stage1MemoryNeed(
        need_id=need_id,
        run_id=run_id,
        task_id=task_id,
        base_commit=CommitId("sha256:" + "0" * 64),
        horizon_target=(task.checkpoint_chapter, task.checkpoint_chapter),
        need_type="benchmark_readout",
        query_intent=Stage1QueryIntent.SEMANTIC_HISTORY,
        query_text=query_text,
        access_scope="author_planning" if allow_plan else "writer_safe",
        allow_plan=allow_plan,
        planner_may_read_plan=allow_plan,
        retrieval_may_return_plan=allow_plan,
        claim_may_cite_plan=allow_plan,
        legacy_allow_plan=allow_plan,
        why_needed=query_text,
        risk_level=NeedRisk.MEDIUM,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.ANCHOR_FIRST,
        allowed_candidate_pools=(CandidatePool.ANCHOR, CandidatePool.GROUNDED),
        expected_evidence_types=("text_span",),
        stop_condition="one exact historical evidence path or typed unresolved gap",
        purpose=query_text,
        expected_section=WriterContextSection.CAUSAL_HISTORY,
        priority=90,
    )


def _fallback_planner_artifact(
    task: Any,
    context: AuthorPlanningContext,
    world: WorldRootDocument,
    need: Stage1MemoryNeed,
    run_id: RunId,
) -> PlannerInvocationArtifact:
    validated_hash = content_id({"task_id": task.task_id.root, "need_id": need.need_id.root})
    raw_response = '{"drafts":[]}'
    summary = PlannerWorldSummaryBuilder.build(task, world, context)
    metadata = PlannerArtifactMetadata(
        run_id=run_id,
        planner_model="u4s-deterministic-fallback",
        planner_model_revision="u4s-deterministic-fallback.v1",
        planner_prompt_version="u4s-seed-fallback.v1",
        planner_prompt_hash=content_id({"task_id": task.task_id.root}),
        planner_output_schema_version=PLANNER_OUTPUT_SCHEMA_VERSION,
        temperature=0.0,
        requested_seed=None,
        effective_seed_supported=False,
        planning_context_hash=context.source_hash,
        world_summary_hash=content_id(summary.model_dump(mode="json")),
        raw_response_hash=content_id({"raw_response": raw_response}),
        validated_need_set_hash=validated_hash,
        fallback_used=True,
        input_tokens=0,
        output_tokens=1,
    )
    return PlannerInvocationArtifact(
        planning_context=context,
        world_summary=summary,
        exact_prompt="u4s deterministic fallback; no model call",
        metadata=metadata,
        raw_response=raw_response,
        parsed_drafts=(),
        validated_need_set_hash=validated_hash,
        fallback_status=PlannerFallbackStatus.PLANNER_FALLBACK,
        fallback_reason="U4-S seed uses a frozen deterministic benchmark Need template",
    )


def _replay_backend(
    world: WorldRootDocument,
    text: TextRootDocument,
    plan: PlanRootDocument,
    basis_commit: CommitId,
    checkpoint_id: StableId,
    snapshot_id: StableId | None = None,
) -> Stage2RetrievalBackendBundle:
    snapshot_id = snapshot_id or StableId(f"snapshot.u4s.text-replay.{checkpoint_id.root}"[:128])
    units = AnchorBuilder().build(
        world,
        text,
        plan,
        snapshot_id=snapshot_id,
        canonical_commit=basis_commit,
    )
    unit_count = max(1, len(units))
    coverage = tuple(
        ChannelCoverage(channel=channel, expected_units=unit_count, ready_units=unit_count)
        for channel in _REPLAY_CHANNELS
    )
    capability = SnapshotCapability(
        source_commit=basis_commit,
        snapshot_id=snapshot_id,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=_REPLAY_CHANNELS,
        coverage_by_channel=coverage,
    )
    attestation = ProjectionAttestation(
        attestation_id=StableId(f"attestation.u4s.text-replay.{checkpoint_id.root}"[:128]),
        retrieval_backend_profile=RetrievalBackendProfile.BENCHMARK_TEXT_REPLAY,
        source_commit=basis_commit,
        snapshot_id=snapshot_id,
        capability=capability,
        r1_record_count=0,
        r1_entity_association_count=0,
        graph_node_count=0,
        graph_edge_count=0,
        reranker_model="deterministic-text-replay",
        reranker_revision="v1",
    )
    return Stage2RetrievalBackendBundle(
        backend=InMemoryRetrievalBackend(units),
        attestation=attestation,
        allowed_channels=_REPLAY_CHANNELS,
        reranker=RerankService(_TextReplayReranker()),
    )


def _memory_identity(
    *,
    basis_commit: CommitId,
    text: TextRootDocument,
    world: WorldRootDocument,
    plan: PlanRootDocument,
    information_profile: BenchmarkInformationProfile,
) -> MemoryIdentitySnapshot:
    return MemoryIdentitySnapshot(
        commit_id=basis_commit,
        text_root=text.root_hash,
        world_root=world.root_hash,
        plan_root=plan.root_hash,
        profile_root=content_id({"information_profile": information_profile.value}),
    )


def as_run_request_id(task_id: StableId) -> StableId:
    """Return a bounded evidence-first request id for a task identity."""

    return StableId(f"request.u4s.readout.{task_id.root}"[:128])


__all__ = [
    "U4SCheckpointInput",
    "U4SPublicCorpus",
    "U4SSeedInputError",
    "as_run_request_id",
]
