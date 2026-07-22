"""Stage 1 benchmark, normalized text, plan, and gold contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.changes import ChangeOperationType, WorldRecordKind
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, SchemaVersion, StableId
from novel_agent.domain.memory import PlanObligation, WorldRootDocument
from novel_agent.domain.model_calls import RetrievalInferenceCallRecord
from novel_agent.domain.text import EvidenceRef, TextBlock
from novel_agent.domain.world import Entity, Event, PlanNode, RelationRecord, StateRecord


class SceneDocument(DomainModel):
    scene_id: StableId
    scene_index: int = Field(ge=0)
    title: str | None = None
    blocks: tuple[TextBlock, ...] = ()

    @model_validator(mode="after")
    def validate_blocks(self) -> SceneDocument:
        if len({block.block_id for block in self.blocks}) != len(self.blocks):
            raise ValueError("scene block ids must be unique")
        if any(block.scene_id != self.scene_id for block in self.blocks):
            raise ValueError("scene blocks must reference their containing scene")
        if tuple(block.narrative_index for block in self.blocks) != tuple(
            sorted(block.narrative_index for block in self.blocks)
        ):
            raise ValueError("scene blocks must be in narrative order")
        return self


class ChapterDocument(DomainModel):
    chapter_id: StableId
    chapter_index: int = Field(ge=1)
    title: str | None = None
    scenes: tuple[SceneDocument, ...] = ()

    @model_validator(mode="after")
    def validate_scenes(self) -> ChapterDocument:
        if len({scene.scene_id for scene in self.scenes}) != len(self.scenes):
            raise ValueError("chapter scene ids must be unique")
        if any(
            block.chapter_id != self.chapter_id for scene in self.scenes for block in scene.blocks
        ):
            raise ValueError("chapter blocks must reference their containing chapter")
        if tuple(scene.scene_index for scene in self.scenes) != tuple(
            sorted(scene.scene_index for scene in self.scenes)
        ):
            raise ValueError("chapter scenes must be in narrative order")
        return self


class PreludeDocument(DomainModel):
    prelude_id: StableId
    title: str | None = None
    scenes: tuple[SceneDocument, ...]

    @model_validator(mode="after")
    def validate_scenes(self) -> PreludeDocument:
        if any(
            block.chapter_id != self.prelude_id for scene in self.scenes for block in scene.blocks
        ):
            raise ValueError("prelude blocks must reference their containing prelude")
        indexes = tuple(scene.scene_index for scene in self.scenes)
        if indexes != tuple(sorted(indexes)) or len(indexes) != len(set(indexes)):
            raise ValueError("prelude scenes must have unique narrative order")
        return self


class TextRootDocument(DomainModel):
    root_hash: ArtifactId
    schema_version: SchemaVersion
    prelude: PreludeDocument | None = None
    chapters: tuple[ChapterDocument, ...]

    @model_validator(mode="after")
    def validate_chapters(self) -> TextRootDocument:
        indexes = tuple(chapter.chapter_index for chapter in self.chapters)
        if indexes != tuple(sorted(indexes)) or len(indexes) != len(set(indexes)):
            raise ValueError("text root chapters must have unique ascending indexes")
        block_ids = [
            block.block_id
            for scene in (
                *(self.prelude.scenes if self.prelude is not None else ()),
                *(scene for chapter in self.chapters for scene in chapter.scenes),
            )
            for block in scene.blocks
        ]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("text root block ids must be globally unique")
        return self


class ChapterSummary(DomainModel):
    chapter_id: StableId
    chapter_index: int = Field(ge=1)
    summary: str = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)


class ChapterSummaryRootDocument(DomainModel):
    root_hash: ArtifactId
    schema_version: SchemaVersion
    source_text_root: ArtifactId
    summaries: tuple[ChapterSummary, ...]

    @model_validator(mode="after")
    def validate_summaries(self) -> ChapterSummaryRootDocument:
        indexes = tuple(summary.chapter_index for summary in self.summaries)
        if indexes != tuple(sorted(indexes)) or len(indexes) != len(set(indexes)):
            raise ValueError("chapter summaries must have unique ascending indexes")
        if len({summary.chapter_id for summary in self.summaries}) != len(self.summaries):
            raise ValueError("chapter summary ids must be unique")
        return self


class ChapterGoal(DomainModel):
    goal_id: StableId
    chapter_index: int = Field(ge=1)
    summary: str = Field(min_length=1)
    obligation_ids: tuple[StableId, ...] = ()


class PlanRootDocument(DomainModel):
    root_hash: ArtifactId
    schema_version: SchemaVersion
    nodes: tuple[PlanNode, ...] = ()
    chapter_goals: tuple[ChapterGoal, ...] = ()

    @model_validator(mode="after")
    def validate_plan_graph(self) -> PlanRootDocument:
        node_ids = {node.plan_node_id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("plan node ids must be unique")
        if any(
            node.parent_id is not None and node.parent_id not in node_ids for node in self.nodes
        ):
            raise ValueError("plan node parent must exist in the same plan root")
        if len({goal.goal_id for goal in self.chapter_goals}) != len(self.chapter_goals):
            raise ValueError("chapter goal ids must be unique")
        return self


class WorldConstructionDraft(DomainModel):
    entities: tuple[Entity, ...] = ()
    events: tuple[Event, ...] = ()
    states: tuple[StateRecord, ...] = ()
    relations: tuple[RelationRecord, ...] = ()
    obligations: tuple[PlanObligation, ...] = ()


class GoldKind(StrEnum):
    OBSERVED_USE = "observed_use"
    OPERATIONAL_CONSTRAINT = "operational_constraint"
    PLAN_OBLIGATION = "plan_obligation"


class PlanEvidenceRef(DomainModel):
    """Immutable pointer to author-visible intent without promoting it to World fact."""

    evidence_id: StableId
    plan_root_hash: ArtifactId
    goal_id: StableId
    object_hash: ArtifactId


class GoldItem(DomainModel):
    gold_id: StableId
    kind: GoldKind
    description: str = Field(min_length=1)
    target_chapters: tuple[int, ...] = Field(min_length=1)
    # Historical evidence the memory kernel is expected to recover.
    evidence_refs: tuple[EvidenceRef, ...] = ()
    # Author-visible plan provenance. This is intent evidence, never observed World evidence.
    plan_evidence_refs: tuple[PlanEvidenceRef, ...] = ()
    # Private target-text evidence proving the Gold was used or constrained output.
    future_evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    mandatory: bool = False

    @model_validator(mode="after")
    def validate_evidence_kind(self) -> GoldItem:
        if self.kind is GoldKind.PLAN_OBLIGATION:
            if not self.plan_evidence_refs and not self.evidence_refs:
                raise ValueError("plan obligation Gold requires plan or historical evidence")
        elif not self.evidence_refs:
            raise ValueError("observed and operational Gold require historical evidence")
        if self.kind is not GoldKind.PLAN_OBLIGATION and self.plan_evidence_refs:
            raise ValueError("only plan obligation Gold may carry plan evidence")
        return self


class BenchmarkTrack(StrEnum):
    ORACLE = "oracle_verified"
    END_TO_END = "end_to_end"


class BenchmarkQueryCondition(StrEnum):
    ORACLE = "oracle_need"
    GENERATED = "generated_need"


class BenchmarkCaseManifest(DomainModel):
    case_id: StableId
    project_id: ProjectId
    history_range: tuple[int, int]
    target_range: tuple[int, int]
    input_text_root: ArtifactId
    input_summary_root: ArtifactId | None = None
    future_text_root_private: ArtifactId
    input_plan_root: ArtifactId | None = None
    input_world_root_verified: ArtifactId | None = None
    chapter_goal_ids: tuple[StableId, ...] = ()
    observed_use_gold: tuple[GoldItem, ...]
    operational_constraint_gold: tuple[GoldItem, ...]
    plan_obligation_gold: tuple[GoldItem, ...]
    annotation_version: SchemaVersion
    expected_tracks: tuple[BenchmarkTrack, ...] = (
        BenchmarkTrack.ORACLE,
        BenchmarkTrack.END_TO_END,
    )
    gate_eligible: bool = True

    @model_validator(mode="after")
    def validate_case_ranges_and_gold(self) -> BenchmarkCaseManifest:
        history_start, history_end = self.history_range
        target_start, target_end = self.target_range
        if history_start < 1 or history_end < history_start:
            raise ValueError("history range is invalid")
        if target_start <= history_end or target_end < target_start:
            raise ValueError("target range must follow history without overlap")
        typed_gold = (
            (self.observed_use_gold, GoldKind.OBSERVED_USE),
            (self.operational_constraint_gold, GoldKind.OPERATIONAL_CONSTRAINT),
            (self.plan_obligation_gold, GoldKind.PLAN_OBLIGATION),
        )
        for items, kind in typed_gold:
            if self.gate_eligible and not items:
                raise ValueError(f"gate-eligible case requires {kind.value} gold")
            if any(item.kind is not kind for item in items):
                raise ValueError(
                    f"gold collection contains an item of the wrong kind: {kind.value}"
                )
            if any(
                chapter < target_start or chapter > target_end
                for item in items
                for chapter in item.target_chapters
            ):
                raise ValueError("gold target chapter falls outside target range")
        return self


class HistoryAccessPolicy(StrEnum):
    HISTORY_ONLY = "history_only"


class EvaluatorAccessPolicy(StrEnum):
    REVEAL_AFTER_FREEZE = "reveal_after_context_freeze"


class FailureCategory(StrEnum):
    STATE = "F-STATE"
    NEED = "F-NEED"
    ROUTE = "F-ROUTE"
    RETRIEVE = "F-RETRIEVE"
    RANK = "F-RANK"
    EXPAND = "F-EXPAND"
    CONTEXT = "F-CONTEXT"
    EXTRACT = "F-EXTRACT"
    TRUTH = "F-TRUTH"
    VALIDATE = "F-VALIDATE"
    COMMIT = "F-COMMIT"
    FRESH = "F-FRESH"
    EVAL = "F-EVAL"


class BenchmarkMetricSet(DomainModel):
    gold_evidence_recall: float = Field(ge=0, le=1)
    observed_use_coverage: float = Field(ge=0, le=1)
    operational_constraint_coverage: float = Field(ge=0, le=1)
    plan_obligation_coverage: float = Field(ge=0, le=1)
    mandatory_constraint_coverage: float = Field(ge=0, le=1)
    evidence_traceability: float = Field(ge=0, le=1)
    future_leakage_rate: float = Field(ge=0, le=1)
    l0_evidence_tokens_read: int = Field(ge=0)
    full_chapter_read_rate: float = Field(ge=0, le=1)
    context_utility_per_1k_tokens: float = Field(ge=0)
    need_recall: float | None = Field(default=None, ge=0, le=1)
    need_precision: float | None = Field(default=None, ge=0, le=1)
    need_f1: float | None = Field(default=None, ge=0, le=1)
    query_intent_routing_accuracy: float | None = Field(default=None, ge=0, le=1)
    wrong_route_rate: float | None = Field(default=None, ge=0, le=1)
    unnecessary_channel_rate: float | None = Field(default=None, ge=0, le=1)
    gold_evidence_recall_at_k: float | None = Field(default=None, ge=0, le=1)
    mean_reciprocal_rank: float | None = Field(default=None, ge=0, le=1)
    ndcg: float | None = Field(default=None, ge=0, le=1)
    anchor_recall_at_k: float | None = Field(default=None, ge=0, le=1)
    anchor_precision_at_k: float | None = Field(default=None, ge=0, le=1)
    anchor_to_gold_conversion_rate: float | None = Field(default=None, ge=0, le=1)
    evidence_recall_after_expansion: float | None = Field(default=None, ge=0, le=1)
    average_anchors_expanded: float | None = Field(default=None, ge=0)
    average_spans_expanded: float | None = Field(default=None, ge=0)
    average_scenes_expanded: float | None = Field(default=None, ge=0)
    grounded_fallback_rate: float | None = Field(default=None, ge=0, le=1)
    reranker_pair_tokens: int | None = Field(default=None, ge=0)
    current_state_accuracy: float | None = Field(default=None, ge=0, le=1)
    temporal_validity_accuracy: float | None = Field(default=None, ge=0, le=1)
    stale_state_rate: float | None = Field(default=None, ge=0, le=1)
    wrong_entity_binding_rate: float | None = Field(default=None, ge=0, le=1)
    irrelevant_token_ratio: float | None = Field(default=None, ge=0, le=1)
    conflict_exposure_rate: float | None = Field(default=None, ge=0, le=1)
    unresolved_gap_calibration: float | None = Field(default=None, ge=0, le=1)
    n_plus_1_coverage: float | None = Field(default=None, ge=0, le=1)
    n_plus_2_coverage: float | None = Field(default=None, ge=0, le=1)
    n_plus_3_coverage: float | None = Field(default=None, ge=0, le=1)
    shared_horizon_constraint_coverage: float | None = Field(default=None, ge=0, le=1)
    horizon_decay: float | None = Field(default=None, ge=0, le=1)
    premature_future_injection_rate: float | None = Field(default=None, ge=0, le=1)
    inter_chapter_context_repetition: float | None = Field(default=None, ge=0, le=1)


class BenchmarkProfileResult(DomainModel):
    profile: str = Field(min_length=1)
    metrics: BenchmarkMetricSet
    retrieved_evidence_ids: tuple[StableId, ...] = ()
    failure_categories: tuple[FailureCategory, ...] = ()


class Stage1BenchmarkConfig(DomainModel):
    config_version: SchemaVersion
    token_budget: int = Field(ge=1)
    per_channel_candidate_limit: int = Field(ge=1)
    fused_candidate_limit: int = Field(ge=1)
    rrf_k: int = Field(ge=1)
    embedding_profile: str = Field(min_length=1)
    reranker_profile: str = Field(min_length=1)
    expansion_profile: str = Field(min_length=1)
    summary_profile: str = Field(min_length=1)
    query_condition: BenchmarkQueryCondition
    need_profile: str = Field(min_length=1)
    random_seed: int


class Stage1BenchmarkResult(DomainModel):
    run_id: RunId | None = None
    bundle_id: StableId
    case_id: StableId
    track: BenchmarkTrack
    base_commit: CommitId
    snapshot_id: StableId
    config: Stage1BenchmarkConfig
    context_frozen: bool
    profile_results: tuple[BenchmarkProfileResult, ...]
    retrieval_model_calls: tuple[RetrievalInferenceCallRecord, ...] = ()


class ReplayGoldChange(DomainModel):
    gold_change_id: StableId
    chapter_index: int = Field(ge=1)
    operation: ChangeOperationType
    record_kind: WorldRecordKind
    target_id: StableId
    expected_record: dict[str, JsonValue]
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    critical: bool = False


class ReplayStateCategory(StrEnum):
    OWNERSHIP = "ownership"
    LOCATION = "location"
    VITAL_OR_INJURY = "vital_or_injury"
    OBLIGATION = "obligation"
    OTHER = "other"


class ReplayExpectedRecord(DomainModel):
    record_kind: WorldRecordKind
    target_id: StableId
    expected_record: dict[str, JsonValue]
    category: ReplayStateCategory = ReplayStateCategory.OTHER


class ReplayStateCheckpoint(DomainModel):
    chapter_index: int = Field(ge=1)
    expected_records: tuple[ReplayExpectedRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_records(self) -> ReplayStateCheckpoint:
        identities = tuple(
            (record.record_kind, record.target_id) for record in self.expected_records
        )
        if len(identities) != len(set(identities)):
            raise ValueError("replay checkpoint record identities must be unique")
        return self


class ReplayCaseManifest(DomainModel):
    replay_case_id: StableId
    project_id: ProjectId
    chapter_range: tuple[int, int]
    target_text_root: ArtifactId
    initial_world_root: ArtifactId
    gold_changes: tuple[ReplayGoldChange, ...]
    state_checkpoints: tuple[ReplayStateCheckpoint, ...] = ()
    annotation_version: SchemaVersion
    gate_eligible: bool = True

    @model_validator(mode="after")
    def validate_replay_scope(self) -> ReplayCaseManifest:
        start, end = self.chapter_range
        if start < 1 or end < start:
            raise ValueError("replay chapter range is invalid")
        if any(
            change.chapter_index < start or change.chapter_index > end
            for change in self.gold_changes
        ):
            raise ValueError("replay gold change falls outside chapter range")
        if self.gate_eligible and end - start + 1 < 50:
            raise ValueError("gate-eligible replay requires at least 50 chapters")
        if self.gate_eligible and not self.gold_changes:
            raise ValueError("gate-eligible replay requires gold changes")
        checkpoint_chapters = tuple(
            checkpoint.chapter_index for checkpoint in self.state_checkpoints
        )
        if len(checkpoint_chapters) != len(set(checkpoint_chapters)):
            raise ValueError("replay checkpoint chapters must be unique")
        if any(chapter < start or chapter > end for chapter in checkpoint_chapters):
            raise ValueError("replay checkpoint falls outside chapter range")
        if self.gate_eligible and end not in checkpoint_chapters:
            raise ValueError("gate-eligible replay requires a final state checkpoint")
        return self


class ReplayMetricSet(DomainModel):
    state_delta_precision: float = Field(ge=0, le=1)
    state_delta_recall: float = Field(ge=0, le=1)
    state_delta_f1: float = Field(ge=0, le=1)
    event_extraction_f1: float = Field(ge=0, le=1)
    relation_delta_f1: float = Field(ge=0, le=1)
    plan_obligation_update_f1: float = Field(ge=0, le=1)
    wrong_target_binding_rate: float = Field(ge=0, le=1)
    false_world_fact_promotion_rate: float = Field(ge=0, le=1)
    missed_critical_change_rate: float = Field(ge=0, le=1)
    invalid_state_overwrite_rate: float | None = Field(default=None, ge=0, le=1)
    evidence_binding_accuracy: float = Field(ge=0, le=1)
    commit_reject_rate: float = Field(ge=0, le=1)
    current_state_accuracy_by_chapter: dict[int, float] = Field(default_factory=dict)
    cumulative_state_drift: tuple[float, ...] = ()
    wrong_item_ownership_count: int | None = Field(default=None, ge=0)
    wrong_character_location_count: int | None = Field(default=None, ge=0)
    wrong_vital_or_injury_state_count: int | None = Field(default=None, ge=0)
    wrong_obligation_debt_count: int | None = Field(default=None, ge=0)
    orphan_evidence_ref_count: int | None = Field(default=None, ge=0)
    manual_repair_commit_count: int | None = Field(default=None, ge=0)
    first_pollution_chapter: int | None = Field(default=None, ge=1)
    pollution_propagation_depth: int | None = Field(default=None, ge=0)


class ReplayGateEvidence(DomainModel):
    replay_case_id: StableId
    metrics: ReplayMetricSet
    replayed_chapters: int = Field(ge=0)
    silent_canonical_pollution_count: int = Field(ge=0)
    silent_stale_snapshot_reads: int = Field(ge=0)


class Stage1GateVerdict(StrEnum):
    PASS = "pass"
    CONDITIONAL_PASS = "conditional_pass"
    FAIL = "fail"
    INCOMPLETE = "incomplete"
    NOT_ELIGIBLE = "not_eligible"


class Stage1GateReport(DomainModel):
    bundle_id: StableId
    verdict: Stage1GateVerdict
    read_results_expected: int = Field(ge=0)
    read_results_present: int = Field(ge=0)
    replay_cases_expected: int = Field(ge=0)
    replay_cases_present: int = Field(ge=0)
    checks: dict[str, bool]
    failure_counts: dict[FailureCategory, int]
    blockers: tuple[str, ...] = ()


class BenchmarkBundle(DomainModel):
    bundle_id: StableId
    bundle_schema_version: SchemaVersion
    content_hash: ArtifactId
    text_roots: tuple[TextRootDocument, ...]
    summary_roots: tuple[ChapterSummaryRootDocument, ...] = ()
    plan_roots: tuple[PlanRootDocument, ...] = ()
    world_roots: tuple[WorldRootDocument, ...] = ()
    case_manifests: tuple[BenchmarkCaseManifest, ...]
    replay_manifests: tuple[ReplayCaseManifest, ...] = ()
    history_access_policy: HistoryAccessPolicy = HistoryAccessPolicy.HISTORY_ONLY
    evaluator_access_policy: EvaluatorAccessPolicy = EvaluatorAccessPolicy.REVEAL_AFTER_FREEZE
    expected_profiles: tuple[str, ...] = ("stage1-pilot-v0.1",)

    @model_validator(mode="after")
    def validate_unique_roots_and_cases(self) -> BenchmarkBundle:
        text_hashes = {root.root_hash for root in self.text_roots}
        summary_hashes = {root.root_hash for root in self.summary_roots}
        plan_hashes = {root.root_hash for root in self.plan_roots}
        world_hashes = {root.root_hash for root in self.world_roots}
        if len(text_hashes) != len(self.text_roots):
            raise ValueError("text root hashes must be unique")
        if len(summary_hashes) != len(self.summary_roots):
            raise ValueError("summary root hashes must be unique")
        if len(plan_hashes) != len(self.plan_roots):
            raise ValueError("plan root hashes must be unique")
        if len(world_hashes) != len(self.world_roots):
            raise ValueError("world root hashes must be unique")
        if len({case.case_id for case in self.case_manifests}) != len(self.case_manifests):
            raise ValueError("benchmark case ids must be unique")
        if len({case.replay_case_id for case in self.replay_manifests}) != len(
            self.replay_manifests
        ):
            raise ValueError("replay case ids must be unique")
        return self
