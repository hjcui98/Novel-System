"""Small license-free 20→3 BenchmarkBundle fixture."""

from __future__ import annotations

from novel_agent.domain.benchmark import (
    BenchmarkBundle,
    BenchmarkCaseManifest,
    ChapterDocument,
    ChapterGoal,
    ChapterSummary,
    ChapterSummaryRootDocument,
    GoldItem,
    GoldKind,
    PlanRootDocument,
    ReplayCaseManifest,
    ReplayExpectedRecord,
    ReplayGoldChange,
    ReplayStateCategory,
    ReplayStateCheckpoint,
    SceneDocument,
    TextRootDocument,
)
from novel_agent.domain.changes import ChangeOperationType, WorldRecordKind
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    WorldRootDocument,
)
from novel_agent.domain.memory_benchmark import GoldType
from novel_agent.domain.text import (
    EvidenceRef,
    EvidenceSupportStatus,
    TextBlock,
    TextSpanRef,
)
from novel_agent.domain.world import Entity, Event, PlanNode, StateRecord, StoryTime, TruthClass
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.benchmark_importer import (
    bundle_content_id,
    plan_root_content_id,
    quote_hash,
    summary_root_content_id,
    text_root_content_id,
    world_root_content_id,
)

VERSION = SchemaVersion("0.1.0")
PLACEHOLDER_HASH = ArtifactId("sha256:" + "0" * 64)
BASE_COMMIT = CommitId("sha256:" + "1" * 64)


def _chapter(index: int, text: str) -> ChapterDocument:
    chapter_id = StableId(f"chapter.synthetic.{index}")
    scene_id = StableId(f"scene.synthetic.{index}.1")
    return ChapterDocument(
        chapter_id=chapter_id,
        chapter_index=index,
        title=f"合成章节 {index}",
        scenes=(
            SceneDocument(
                scene_id=scene_id,
                scene_index=1,
                blocks=(
                    TextBlock(
                        block_id=StableId(f"block.synthetic.{index}.1"),
                        chapter_id=chapter_id,
                        scene_id=scene_id,
                        narrative_index=1,
                        text=text,
                    ),
                ),
            ),
        ),
    )


def _text_root(chapters: tuple[ChapterDocument, ...]) -> TextRootDocument:
    provisional = TextRootDocument(
        root_hash=PLACEHOLDER_HASH,
        schema_version=VERSION,
        chapters=chapters,
    )
    return provisional.model_copy(update={"root_hash": text_root_content_id(provisional)})


def _evidence(root: TextRootDocument, chapter_index: int, phrase: str) -> EvidenceRef:
    chapter = next(chapter for chapter in root.chapters if chapter.chapter_index == chapter_index)
    block = chapter.scenes[0].blocks[0]
    start = block.text.index(phrase)
    return EvidenceRef(
        evidence_id=StableId(f"evidence.synthetic.{chapter_index}.{start}"),
        root_hash=root.root_hash,
        object_hash=sha256_id(block.text.encode("utf-8")),
        chapter_id=chapter.chapter_id,
        scene_id=chapter.scenes[0].scene_id,
        span=TextSpanRef(block_id=block.block_id, start=start, end=start + len(phrase)),
        quote_hash=quote_hash(phrase),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=BASE_COMMIT,
    )


def _summary_root(history: TextRootDocument) -> ChapterSummaryRootDocument:
    provisional = ChapterSummaryRootDocument(
        root_hash=PLACEHOLDER_HASH,
        schema_version=VERSION,
        source_text_root=history.root_hash,
        summaries=tuple(
            ChapterSummary(
                chapter_id=chapter.chapter_id,
                chapter_index=chapter.chapter_index,
                summary=(
                    "林澈立下必须进入北塔的旧誓言。"
                    if chapter.chapter_index == 5
                    else f"林澈完成第{chapter.chapter_index}章的合成旅程。"
                ),
                evidence_refs=(
                    _evidence(
                        history,
                        chapter.chapter_index,
                        chapter.scenes[0].blocks[0].text,
                    ),
                ),
            )
            for chapter in history.chapters
            if chapter.chapter_index <= 17
        ),
    )
    return provisional.model_copy(update={"root_hash": summary_root_content_id(provisional)})


def make_synthetic_bundle() -> BenchmarkBundle:
    history = _text_root(
        tuple(
            _chapter(
                index,
                "林澈受伤仍未痊愈。"
                if index == 20
                else "林澈立下旧誓言,未来必须进入北塔。"
                if index == 5
                else f"林澈在第{index}章完成一段无版权的合成旅程。",
            )
            for index in range(1, 21)
        )
    )
    future = _text_root(
        (
            _chapter(21, "林澈重申旧誓言,决定继续北行。"),
            _chapter(22, "林澈受伤仍未痊愈,只能暂缓赶路。"),
            _chapter(23, "林澈终于进入北塔,兑现了阶段目标。"),
        )
    )
    summaries = _summary_root(history)
    plan_provisional = PlanRootDocument(
        root_hash=PLACEHOLDER_HASH,
        schema_version=VERSION,
        nodes=(
            PlanNode(
                plan_node_id=StableId("plan.synthetic.volume.1"),
                node_type="volume",
                title="北塔篇",
                summary="角色前往北塔并兑现旧誓言。",
                obligation_ids=(StableId("obligation.synthetic.north-tower"),),
            ),
        ),
        chapter_goals=tuple(
            ChapterGoal(
                goal_id=StableId(f"goal.synthetic.{index}"),
                chapter_index=index,
                summary=summary,
                obligation_ids=(StableId("obligation.synthetic.north-tower"),),
            )
            for index, summary in (
                (21, "重申旧誓言"),
                (22, "保持受伤状态约束"),
                (23, "进入北塔"),
            )
        ),
    )
    plan = plan_provisional.model_copy(update={"root_hash": plan_root_content_id(plan_provisional)})
    character_id = StableId("entity.synthetic.lin-che")
    world_provisional = WorldRootDocument(
        root_hash=PLACEHOLDER_HASH,
        schema_version=VERSION,
        source_commit=BASE_COMMIT,
        entities=(
            Entity(
                entity_id=character_id,
                entity_type="character",
                internal_label="林澈",
                aliases=("林澈",),
            ),
        ),
        events=(
            Event(
                event_id=StableId("event.synthetic.promise"),
                event_type="promise_remembered",
                participant_ids=(character_id,),
                evidence_refs=(_evidence(history, 5, "旧誓言"),),
                truth_class=TruthClass.ACCEPTED_WORLD_FACT,
            ),
        ),
        states=(
            StateRecord(
                state_id=StableId("state.synthetic.injury"),
                subject_id=character_id,
                predicate="injury",
                value="not_healed",
                valid_time=StoryTime(worldline="main", start_ordinal=20),
                evidence_refs=(_evidence(history, 20, "受伤仍未痊愈"),),
                truth_class=TruthClass.ACCEPTED_WORLD_FACT,
            ),
        ),
        obligations=(
            PlanObligation(
                obligation_id=StableId("obligation.synthetic.north-tower"),
                kind=ObligationKind.OBJECTIVE,
                description="林澈需要进入北塔。",
                status=ObligationStatus.OPEN,
                owner_ids=(character_id,),
                due_chapter=23,
                evidence_refs=(_evidence(history, 5, "旧誓言"),),
            ),
        ),
    )
    world = world_provisional.model_copy(
        update={"root_hash": world_root_content_id(world_provisional)}
    )
    case = BenchmarkCaseManifest(
        case_id=StableId("case.synthetic.20-to-3"),
        project_id=ProjectId("project.synthetic"),
        history_range=(1, 20),
        target_range=(21, 23),
        input_text_root=history.root_hash,
        input_summary_root=summaries.root_hash,
        future_text_root_private=future.root_hash,
        input_plan_root=plan.root_hash,
        input_world_root_verified=world.root_hash,
        chapter_goal_ids=tuple(goal.goal_id for goal in plan.chapter_goals),
        observed_use_gold=(
            GoldItem(
                gold_id=StableId("gold.synthetic.observed.promise"),
                kind=GoldKind.OBSERVED_USE,
                description="旧誓言在目标章节被明确调用。",
                target_chapters=(21,),
                evidence_refs=(_evidence(history, 5, "旧誓言"),),
                future_evidence_refs=(_evidence(future, 21, "旧誓言"),),
                gold_type=GoldType.CAUSAL_HISTORY,
            ),
        ),
        operational_constraint_gold=(
            GoldItem(
                gold_id=StableId("gold.synthetic.constraint.injury"),
                kind=GoldKind.OPERATIONAL_CONSTRAINT,
                description="角色的伤势限制行动。",
                target_chapters=(22,),
                evidence_refs=(_evidence(history, 20, "受伤仍未痊愈"),),
                future_evidence_refs=(_evidence(future, 22, "受伤仍未痊愈"),),
                mandatory=True,
                gold_type=GoldType.CURRENT_STATE,
            ),
        ),
        plan_obligation_gold=(
            GoldItem(
                gold_id=StableId("gold.synthetic.plan.north-tower"),
                kind=GoldKind.PLAN_OBLIGATION,
                description="进入北塔的义务得到兑现。",
                target_chapters=(23,),
                evidence_refs=(_evidence(history, 5, "旧誓言"),),
                future_evidence_refs=(_evidence(future, 23, "进入北塔"),),
                mandatory=True,
                gold_type=GoldType.PLAN_OBLIGATION,
            ),
        ),
        annotation_version=VERSION,
    )
    provisional = BenchmarkBundle(
        bundle_id=StableId("bundle.synthetic.stage1"),
        bundle_schema_version=VERSION,
        content_hash=PLACEHOLDER_HASH,
        text_roots=(history, future),
        summary_roots=(summaries,),
        plan_roots=(plan,),
        world_roots=(world,),
        case_manifests=(case,),
        replay_manifests=(
            ReplayCaseManifest(
                replay_case_id=StableId("replay.synthetic.21-to-23"),
                project_id=ProjectId("project.synthetic"),
                chapter_range=(21, 23),
                target_text_root=future.root_hash,
                initial_world_root=world.root_hash,
                gold_changes=(
                    ReplayGoldChange(
                        gold_change_id=StableId("replay-gold.synthetic.21.promise"),
                        chapter_index=21,
                        operation=ChangeOperationType.CREATE,
                        record_kind=WorldRecordKind.EVENT,
                        target_id=StableId("event.synthetic.promise-reaffirmed"),
                        expected_record={
                            "event_type": "promise_reaffirmed",
                            "truth_class": "accepted_world_fact",
                        },
                        evidence_refs=(_evidence(future, 21, "重申旧誓言"),),
                    ),
                    ReplayGoldChange(
                        gold_change_id=StableId("replay-gold.synthetic.22.injury"),
                        chapter_index=22,
                        operation=ChangeOperationType.REPLACE,
                        record_kind=WorldRecordKind.STATE,
                        target_id=StableId("state.synthetic.injury"),
                        expected_record={"predicate": "injury", "value": "not_healed"},
                        evidence_refs=(_evidence(future, 22, "受伤仍未痊愈"),),
                        critical=True,
                    ),
                    ReplayGoldChange(
                        gold_change_id=StableId("replay-gold.synthetic.23.obligation"),
                        chapter_index=23,
                        operation=ChangeOperationType.REPLACE,
                        record_kind=WorldRecordKind.OBLIGATION,
                        target_id=StableId("obligation.synthetic.north-tower"),
                        expected_record={"status": "resolved"},
                        evidence_refs=(_evidence(future, 23, "进入北塔"),),
                        critical=True,
                    ),
                ),
                state_checkpoints=(
                    ReplayStateCheckpoint(
                        chapter_index=22,
                        expected_records=(
                            ReplayExpectedRecord(
                                record_kind=WorldRecordKind.STATE,
                                target_id=StableId("state.synthetic.injury"),
                                expected_record={
                                    "predicate": "injury",
                                    "value": "not_healed",
                                },
                                category=ReplayStateCategory.VITAL_OR_INJURY,
                            ),
                        ),
                    ),
                    ReplayStateCheckpoint(
                        chapter_index=23,
                        expected_records=(
                            ReplayExpectedRecord(
                                record_kind=WorldRecordKind.STATE,
                                target_id=StableId("state.synthetic.injury"),
                                expected_record={"value": "not_healed"},
                                category=ReplayStateCategory.VITAL_OR_INJURY,
                            ),
                            ReplayExpectedRecord(
                                record_kind=WorldRecordKind.OBLIGATION,
                                target_id=StableId("obligation.synthetic.north-tower"),
                                expected_record={"status": "resolved"},
                                category=ReplayStateCategory.OBLIGATION,
                            ),
                        ),
                    ),
                ),
                annotation_version=VERSION,
                gate_eligible=False,
            ),
        ),
    )
    return provisional.model_copy(update={"content_hash": bundle_content_id(provisional)})
