"""Real adapter to the public Stage 3 Writer Context Loop boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import (
    AuthorPlanningContext,
    ChapterGoal,
    PlanRootDocument,
    TextRootDocument,
    VisibleOutlineNode,
)
from novel_agent.domain.creative_runtime import DRAFT_REVISION_DIRECTIVE_MEDIA_TYPE
from novel_agent.domain.generation import (
    AcceptedPlanBinding,
    RecentProseContext,
    WritingLengthPolicy,
    WritingLoopBudgets,
    WritingLoopRequest,
    WritingTaskContract,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    bounded_stable_id,
)
from novel_agent.domain.memory import DerivedBuildStatus, WorldRootDocument
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.plan_obligation_scope import scoped_plan_obligation_ids
from novel_agent.domain.runtime import TaskRecord
from novel_agent.domain.stage2 import FutureIsolationAttestation, ProjectProfileRootDocument
from novel_agent.domain.world import PlanLevel, PlanNode, TruthClass
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    EvidenceLedgerV2,
    WriterContextPackageV2,
)
from novel_agent.domain.writer_readiness import (
    WriterContextInputNotReady,
    evaluate_writer_readiness,
)
from novel_agent.domain.writing_loop import (
    WRITING_LOOP_CHECKPOINT_MEDIA_TYPE,
    WritingLoopCheckpoint,
    WritingLoopResult,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.evidence_first_writer_context_assembler import (
    EvidenceFirstAssemblyResult,
)
from novel_agent.services.projection import DerivedSnapshotRepository
from novel_agent.services.recent_prose import RecentProseAssembler
from novel_agent.services.writer_context_loop import WriterContextLoopService
from novel_agent.services.writer_reactive_memory import ReactiveMemoryInputs

# The volume stage slots a chapter draft must honour: what the volume enters
# with, the ceilings it may not cross, and what it must exit with.
VOLUME_STAGE_WRITER_KEYS: tuple[str, ...] = (
    "entry_conditions",
    "exit_conditions",
    "reveal_window",
    "capability_ceiling",
    "equipment_ceiling",
)

# Where a stage slot binds when the plan states it as free text.  A free-text slot
# declares no window of its own, so the slot's meaning decides it: what a volume
# enters with is settled at the opening, what it exits with is due by the close,
# and a knowledge boundary or a ceiling holds for the whole volume.  A structured
# slot always wins with the window the plan itself declared.
VOLUME_STAGE_SLOT_SCOPE: Mapping[str, str] = {
    "entry_conditions": "opening",
    "exit_conditions": "closing",
    "reveal_window": "volume",
    "capability_ceiling": "volume",
    "equipment_ceiling": "volume",
}
VOLUME_STAGE_POSITION_LABELS: Mapping[str, str] = {
    "opening": "卷首",
    "middle": "卷中",
    "closing": "卷尾",
}

WRITING_TASK_MEDIA_TYPE = "application/vnd.novel-agent.writing-task+json"
WRITER_CONTEXT_V2_MEDIA_TYPE = "application/vnd.novel-agent.writer-context-v2+json"
EVIDENCE_LEDGER_V2_MEDIA_TYPE = "application/vnd.novel-agent.evidence-ledger-v2+json"
AUTHOR_PLANNING_CONTEXT_MEDIA_TYPE = "application/vnd.novel-agent.author-planning-context+json"


@dataclass(frozen=True, slots=True)
class VolumeStageSlot:
    """One plan stage entry with the chapter window it declares, when it declares one."""

    body: str
    chapter_start: int | None = None
    chapter_end: int | None = None

    @property
    def declares_window(self) -> bool:
        return self.chapter_start is not None and self.chapter_end is not None

    def covers(self, chapter_index: int) -> bool:
        """Whether the declared window covers the chapter; a windowless slot never does."""

        if self.chapter_start is None or self.chapter_end is None:
            return False
        return self.chapter_start <= chapter_index <= self.chapter_end

    @property
    def window_label(self) -> str:
        if self.chapter_start is None or self.chapter_end is None:
            return ""
        return f"{self.chapter_start}-{self.chapter_end}"


def _stage_slot_entries(payload: Mapping[str, object], key: str) -> tuple[VolumeStageSlot, ...]:
    """Read one stage slot whether it is text, a list, or a table.

    A plan may express a stage slot as a bare string, a list of strings, or
    structured entries carrying their own chapter range and obligation.  All
    three must reach the Writer as a constraint instead of only the structured
    shape, and the structured shape must keep the window it declared.
    """

    raw = payload.get(key)
    entries: Sequence[object]
    if isinstance(raw, Mapping):
        entries = (raw,)
    elif isinstance(raw, (list, tuple)):
        entries = raw
    elif isinstance(raw, str) and raw.strip():
        return (VolumeStageSlot(body=raw.strip()),)
    else:
        return ()
    values: list[VolumeStageSlot] = []
    for entry in entries:
        if isinstance(entry, str) and entry.strip():
            values.append(VolumeStageSlot(body=entry.strip()))
            continue
        if not isinstance(entry, Mapping):
            continue
        body = next(
            (
                value.strip()
                for field in ("description", "summary", "text", "condition", "value")
                for value in (entry.get(field),)
                if isinstance(value, str) and value.strip()
            ),
            None,
        )
        if body is None:
            continue
        start = entry.get("chapter_start")
        end = entry.get("chapter_end")
        values.append(
            VolumeStageSlot(
                body=body,
                chapter_start=start if isinstance(start, int) else None,
                chapter_end=end if isinstance(end, int) else None,
            )
        )
    deduped = {slot: None for slot in values}
    return tuple(deduped)


def _volume_stage_positions(
    chapter_index: int, chapter_start: int, chapter_end: int
) -> tuple[str, ...]:
    """Where a chapter sits inside its own stage range.

    The first chapter is the range's opening and the last is its close; the
    range between them splits into contiguous thirds, so a range always has a
    middle for its middle chapters to occupy.
    """

    if chapter_index == chapter_start and chapter_index == chapter_end:
        return ("opening", "closing")
    if chapter_index == chapter_start:
        return ("opening",)
    if chapter_index == chapter_end:
        return ("closing",)
    span = chapter_end - chapter_start + 1
    opening_end = chapter_start + -(-span // 3) - 1
    closing_start = chapter_end - span // 3 + 1
    if chapter_index <= opening_end:
        return ("opening",)
    if chapter_index >= closing_start:
        return ("closing",)
    return ("middle",)


def _volume_stage_constraints(nodes: Sequence[PlanNode], chapter_index: int) -> tuple[str, ...]:
    """Project the stage grid that actually applies to this chapter.

    A structured entry binds only inside the window the plan declared for it.
    A free-text entry has no window, so the slot's own scope decides: an
    opening slot binds at the stage's opening, a closing slot at its close, and
    an invariant for the whole stage.  A closing slot that is not due yet stays
    visible as a prohibition against cashing the stage's result in early,
    because the volume's exit is the one thing a mid-volume chapter must not
    resolve.
    """

    constraints: list[str] = []
    for node in nodes:
        chapter_start = node.chapter_start
        chapter_end = node.chapter_end
        if not isinstance(chapter_start, int) or not isinstance(chapter_end, int):
            continue
        if not chapter_start <= chapter_index <= chapter_end:
            continue
        positions = _volume_stage_positions(chapter_index, chapter_start, chapter_end)
        label = VOLUME_STAGE_POSITION_LABELS[positions[0]]
        for key in VOLUME_STAGE_WRITER_KEYS:
            scope = VOLUME_STAGE_SLOT_SCOPE.get(key, "volume")
            for slot in _stage_slot_entries(node.payload, key):
                if slot.declares_window:
                    if slot.covers(chapter_index):
                        constraints.append(
                            f"当前卷阶段[{label}·窗口{slot.window_label}:{key}]：{slot.body}"  # noqa: RUF001
                        )
                    continue
                if scope == "volume":
                    constraints.append(f"当前卷阶段[整卷:{key}]：{slot.body}")  # noqa: RUF001
                elif scope == "opening" and scope in positions:
                    constraints.append(
                        f"当前卷阶段[卷首目标:{key}]：{slot.body}"  # noqa: RUF001
                        "（这是卷首阶段需要建立的边界，不是本章开始前已经发生的事实；"  # noqa: RUF001
                        "只按当前章节目标建立相容部分）"  # noqa: RUF001
                    )
                elif scope in positions:
                    constraints.append(
                        f"当前卷阶段[{VOLUME_STAGE_POSITION_LABELS[scope]}:{key}]：{slot.body}"  # noqa: RUF001
                    )
                elif scope == "closing":
                    constraints.append(
                        f"当前卷阶段[{label}·出口未到期:exit_conditions]：{slot.body}"  # noqa: RUF001
                        "（本章不得提前兑现或解决本卷出口结果）"  # noqa: RUF001
                    )
                else:
                    constraints.append(
                        f"当前卷阶段[{label}·入口已成立:entry_conditions]：{slot.body}"  # noqa: RUF001
                        "（本卷入口条件已经成立，本章不得与之矛盾）"  # noqa: RUF001
                    )
    return tuple(dict.fromkeys(constraints))


@dataclass(frozen=True, slots=True)
class Stage2MWriterContextInvocation:
    """Exact production inputs handed to the existing Stage 2M read-side owner."""

    run_id: RunId
    task: BenchmarkTaskContract
    planning_context: AuthorPlanningContext
    plan: PlanRootDocument
    text: TextRootDocument
    world: WorldRootDocument
    base_commit: CommitId
    snapshot_id: StableId
    writing_task: WritingTaskContract | None = None
    project_id: ProjectId | None = None
    advisory_artifact_refs: tuple[ArtifactRef, ...] = ()
    plan_root_ref: ArtifactRef | None = None
    plan_revision: str | None = None
    chapter_goal_ids: tuple[StableId, ...] = ()
    planning_context_ref: ArtifactRef | None = None


Stage2MWriterContextFactory = Callable[
    [Stage2MWriterContextInvocation], EvidenceFirstAssemblyResult
]


@dataclass(frozen=True, slots=True)
class WritingRequestPolicy:
    """Pinned Writer defaults used only when ProjectProfile has no explicit value."""

    pov: str
    narrative_person: str
    length_policy: WritingLengthPolicy
    allowed_skills: tuple[StableId, ...]
    budgets: WritingLoopBudgets
    writer_configuration_fingerprint: ArtifactId
    model_configuration_fingerprint: ArtifactId
    future_isolation_configuration_fingerprint: ArtifactId

    def __post_init__(self) -> None:
        if not self.pov.strip() or not self.narrative_person.strip():
            raise ValueError("Writer request policy requires POV and narrative person")
        if not self.allowed_skills:
            raise ValueError("Writer request policy requires at least one allowed Skill")


class WriterRecoveryRefused(RuntimeError):
    """A durable Writer recovery checkpoint cannot be used and must not be rebuilt.

    The production factory restores the frozen Memory package and evidence ledger from
    the checkpoint's immutable references.  A checkpoint that exists but cannot be read
    or does not match the current task basis is a refusal, never a silent second build:
    rebuilding would re-run Memory and re-bill model calls the first attempt already
    paid for.
    """


@dataclass(frozen=True)
class _WriterRecovery:
    """The frozen state one Writer attempt resumes from."""

    checkpoint_ref: ArtifactRef
    package: WriterContextPackageV2
    evidence_ledger: EvidenceLedgerV2
    recent_prose: RecentProseContext
    recent_prose_ref: ArtifactRef


class ProductionWritingRequestFactory:
    """Build the sole Stage 5 -> Stage 2M -> Stage 3 request from accepted Canon."""

    is_fixture = False

    def __init__(
        self,
        *,
        commits: CommitService,
        artifacts: ArtifactRepository,
        recent_prose: RecentProseAssembler,
        writer_context: Stage2MWriterContextFactory,
        policy: WritingRequestPolicy,
        schema_version: SchemaVersion,
        snapshots: DerivedSnapshotRepository | None = None,
    ) -> None:
        self._commits = commits
        self._artifacts = artifacts
        self._recent_prose = recent_prose
        self._writer_context = writer_context
        self._policy = policy
        self._schema_version = schema_version
        self._snapshots = snapshots

    def _projection_is_exact(self, task: TaskRecord) -> bool:
        """Prove the task's Memory snapshot is the exact published projection.

        Returning a bare ``None`` used to mean "unknown" and silently disabled the
        readiness freshness gate on every production request.  A missing snapshot
        repository means the host cannot prove exactness, which is a blocked
        state, not a pass.
        """

        if self._snapshots is None or task.basis_snapshot is None:
            return False
        snapshot = self._snapshots.get_for_commit(task.basis_commit)
        return (
            snapshot is not None
            and snapshot.build_status is DerivedBuildStatus.EXACT
            and snapshot.source_commit == task.basis_commit
            and snapshot.published_at is not None
        )

    def _recover_writer_state(
        self,
        task: TaskRecord,
        *,
        writing_task_ref: ArtifactRef,
        accepted_plan_ref: ArtifactRef,
        project_profile_ref: ArtifactRef,
    ) -> _WriterRecovery | None:
        """Restore the frozen state a retried Writer attempt resumes from.

        A durable checkpoint is read *before* any Stage 2M Memory work, so a restart
        reuses the Memory package, evidence ledger and recent prose the first attempt
        already paid for.  A checkpoint that exists but cannot be read, or that belongs
        to another basis, is refused rather than silently rebuilt: rebuilding would run
        Memory again and re-bill model calls.
        """

        checkpoint_ref = next(
            (
                ref
                for ref in reversed(task.terminal_artifact_refs)
                if ref.media_type == WRITING_LOOP_CHECKPOINT_MEDIA_TYPE
            ),
            None,
        )
        if checkpoint_ref is None:
            return None
        try:
            checkpoint = WritingLoopCheckpoint.model_validate_json(
                self._artifacts.read_verified(checkpoint_ref)
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise WriterRecoveryRefused(
                "Writer recovery checkpoint is unreadable; refusing to rebuild Memory"
            ) from error
        matches = (
            checkpoint.run_id == task.run_id,
            checkpoint.task_id == task.task_id,
            checkpoint.base_commit == task.basis_commit,
            checkpoint.snapshot_id == task.basis_snapshot,
            checkpoint.writing_task_ref == writing_task_ref,
            checkpoint.accepted_plan_ref == accepted_plan_ref,
            checkpoint.project_profile_ref == project_profile_ref,
        )
        if not all(matches):
            raise WriterRecoveryRefused(
                "Writer recovery checkpoint does not match the current task basis"
            )
        try:
            package = WriterContextPackageV2.model_validate_json(
                self._artifacts.read_verified(checkpoint.writer_context_ref)
            )
            evidence_ledger = EvidenceLedgerV2.model_validate_json(
                self._artifacts.read_verified(package.evidence_ledger_ref)
            )
            recent_prose = RecentProseContext.model_validate_json(
                self._artifacts.read_verified(checkpoint.recent_prose_ref)
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise WriterRecoveryRefused(
                "Writer recovery checkpoint references unreadable frozen state"
            ) from error
        return _WriterRecovery(
            checkpoint_ref=checkpoint_ref,
            package=package,
            evidence_ledger=evidence_ledger,
            recent_prose=recent_prose,
            recent_prose_ref=checkpoint.recent_prose_ref,
        )

    def __call__(self, task: TaskRecord) -> WritingLoopRequest:
        if task.basis_snapshot is None:
            raise ValueError("production Writer request requires an exact snapshot")
        if self._commits.current_commit(task.project_id) != task.basis_commit:
            raise ValueError("Writer task basis is not the current project commit")
        manifest = self._commits.load_manifest(task.basis_commit)
        if manifest.project_id != task.project_id:
            raise ValueError("Writer task and canonical manifest belong to different projects")
        plan = PlanRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.plan_root), strict=True
        )
        text = TextRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.text_root), strict=True
        )
        world = WorldRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.world_root), strict=True
        )
        profile = ProjectProfileRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.project_profile_root), strict=True
        )
        latest = text.chapters[-1].chapter_index if text.chapters else 0
        if task.chapter_index != latest + 1:
            raise ValueError(
                f"Writer task targets chapter {task.chapter_index}, but TextRoot ends at {latest}"
            )
        goals = tuple(
            goal for goal in plan.chapter_goals if goal.chapter_index == task.chapter_index
        )
        if not goals:
            raise ValueError("accepted PlanRoot must contain a target chapter goal")
        obligation_ids = scoped_plan_obligation_ids(
            plan=plan,
            world=world,
            chapter_index=task.chapter_index,
        )
        world_obligation_ids = {item.obligation_id for item in world.obligations}
        unknown_obligations = set(obligation_ids) - world_obligation_ids
        if unknown_obligations:
            raise ValueError(
                "WritingTask references unknown obligations: "
                + ", ".join(sorted(item.root for item in unknown_obligations))
            )
        # The enclosing volume's stage slots are the plan's own statement of what
        # the volume must enter with, hold to, and exit with.  Every node whose own
        # chapter range covers this chapter contributes, whatever its planning
        # level, and each entry is projected by the window it declares (or by the
        # slot's own scope when it is free text), so a volume's opening, middle and
        # closing chapters no longer receive the same grid.
        volume_stage_constraints = _volume_stage_constraints(plan.nodes, task.chapter_index)
        summaries = tuple(dict.fromkeys(goal.summary for goal in goals))
        chapter_goal = "；".join(summaries)  # noqa: RUF001 - preserve Chinese prompt punctuation
        payload_beats = tuple(
            beat
            for goal in goals
            for beat in self._payload_strings(goal.payload, "beats", "required_beats")
        )
        state_changes = tuple(
            change for goal in goals for change in self._payload_state_changes(goal.payload)
        )
        required_beats = tuple(
            dict.fromkeys(
                (
                    *payload_beats,
                    *state_changes,
                    *summaries,
                )
            )
        )
        participating_entity_ids = tuple(
            dict.fromkeys(
                entity_id
                for goal in goals
                for entity_id in self._payload_ids(
                    goal.payload, "participating_entity_ids", "entity_ids", "participant_ids"
                )
            )
        )
        unknown_entities = set(participating_entity_ids) - {
            entity.entity_id for entity in world.entities
        }
        if unknown_entities:
            raise ValueError(
                "WritingTask references unknown entities: "
                + ", ".join(sorted(item.root for item in unknown_entities))
            )
        entity_labels = {entity.entity_id: entity.internal_label for entity in world.entities}
        # Only accepted facts are injected as canon.  A state the host classified as
        # a prediction, assertion, rumor or contested claim is not current truth, and
        # labelling it "Canon current state" told the chapter that planned content had
        # already happened.
        canon_states = tuple(
            state for state in world.states if state.truth_class is TruthClass.ACCEPTED_WORLD_FACT
        )
        non_canon_states = tuple(
            state
            for state in world.states
            if state.truth_class is not TruthClass.ACCEPTED_WORLD_FACT
        )
        current_state_constraints = tuple(
            f"Canon current state [{state.subject_id.root}] "
            f"{entity_labels.get(state.subject_id, state.subject_id.root)} "
            f"{state.predicate}={state.value}"
            for state in canon_states
            if state.subject_id in set(participating_entity_ids)
        )
        planned_state_constraints = tuple(
            f"planned but not yet true "
            f"({state.truth_class.value}) [{state.subject_id.root}] "
            f"{entity_labels.get(state.subject_id, state.subject_id.root)} "
            f"{state.predicate}={state.value}"
            for state in non_canon_states
            if state.subject_id in set(participating_entity_ids)
        )
        obligation_actions = tuple(
            dict.fromkeys(
                action
                for goal in goals
                for action in self._payload_obligation_actions(goal.payload, world_obligation_ids)
            )
        )
        advisory_entries: list[dict[str, JsonValue]] = []
        for goal in goals:
            raw_advisories = goal.payload.get("unresolved_advisories")
            if isinstance(raw_advisories, list):
                advisory_entries.extend(raw for raw in raw_advisories if isinstance(raw, dict))
        advisory_constraints = tuple(
            "未决 advisory (不得当作已证实事实)\uff1a" + summary
            for raw in advisory_entries
            if isinstance(summary := raw.get("summary"), str) and summary.strip()
        )
        advisory_forbidden_values: list[str] = []
        for raw in advisory_entries:
            raw_forbidden = raw.get("forbidden_assumptions")
            if not isinstance(raw_forbidden, list):
                continue
            advisory_forbidden_values.extend(
                assumption.strip()
                for assumption in raw_forbidden
                if isinstance(assumption, str) and assumption.strip()
            )
        advisory_forbidden = tuple(advisory_forbidden_values)
        lock_constraints, lock_forbids = self._future_lock_constraints(world, task.chapter_index)
        profile_lock_constraints, profile_lock_forbids = self._profile_lock_constraints(
            profile, task.chapter_index
        )
        draft_revision_constraints = self._draft_revision_constraints(task)
        next_chapter_boundaries = tuple(
            f"保留给第{goal.chapter_index}章，"  # noqa: RUF001
            f"不得在第{task.chapter_index}章发生、完成或写成既成事实："  # noqa: RUF001
            f"{goal.summary}"
            for goal in plan.chapter_goals
            if goal.chapter_index == task.chapter_index + 1
        )
        language = self._profile_string(profile, "language", "")
        language_constraint = (
            (
                f"正文语言：{language}",  # noqa: RUF001 - preserve Chinese prompt punctuation
            )
            if language
            else ()
        )
        language_allowlist = self._profile_strings(profile, "language_allowlist")
        language_allow_constraint = (
            ("允许英文代号\uff1a" + ", ".join(language_allowlist),) if language_allowlist else ()
        )
        writing_task = WritingTaskContract(
            contract_id=bounded_stable_id(
                f"writing-contract.{task.task_id.root}",
                f"writing-contract.{task.basis_commit.root}.{task.chapter_index}",
            ),
            target_chapter=task.chapter_index,
            target_scenes=(StableId(f"scene.chapter.{task.chapter_index}.0"),),
            pov=self._profile_string(profile, "pov", self._policy.pov),
            narrative_person=self._profile_string(
                profile, "narrative_person", self._policy.narrative_person
            ),
            chapter_goal=chapter_goal,
            scene_goals=required_beats,
            required_beats=required_beats,
            active_plan_obligations=obligation_ids,
            mandatory_constraints=(
                *language_constraint,
                *language_allow_constraint,
                *current_state_constraints,
                *planned_state_constraints,
                *self._profile_strings(profile, "mandatory_constraints"),
                *lock_constraints,
                *profile_lock_constraints,
                *volume_stage_constraints,
                *advisory_constraints,
                *draft_revision_constraints,
            ),
            forbidden_reveals=(
                *self._profile_strings(profile, "forbidden_reveals"),
                *lock_forbids,
                *profile_lock_forbids,
                *advisory_forbidden,
                *next_chapter_boundaries,
                *draft_revision_constraints,
            ),
            preserve_requirements=self._profile_strings(profile, "preserve_requirements"),
            style_requirements=(
                *self._profile_style_constraints(profile),
                *self._profile_strings(profile, "style_requirements"),
            ),
            participating_entity_ids=participating_entity_ids,
            obligation_actions=obligation_actions,
            length_policy=self._length_policy(profile),
        )
        writing_task_artifact = self._artifacts.put(
            canonical_json_bytes(writing_task.model_dump(mode="json")),
            WRITING_TASK_MEDIA_TYPE,
            self._schema_version,
        )
        planning_context = self._planning_context(task, plan, chapter_goal, goals)
        planning_context_artifact = self._artifacts.put(
            canonical_json_bytes(planning_context.model_dump(mode="json")),
            AUTHOR_PLANNING_CONTEXT_MEDIA_TYPE,
            self._schema_version,
        )
        memory_task = BenchmarkTaskContract(
            task_id=bounded_stable_id(
                f"memory-task.{task.task_id.root}",
                f"memory-task.{task.basis_commit.root}.{task.chapter_index}",
            ),
            task_text=self._task_text(writing_task),
            checkpoint_chapter=latest,
            target_chapter_start=task.chapter_index,
            target_chapter_end=task.chapter_index,
            information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            task_template_version="production-writing-task.v1",
            output_contract_version="writer_context.v2",
            task_intent=chapter_goal,
            planning_context_hash=planning_context.source_hash,
        )
        accepted_plan_ref = _as_artifact_ref(manifest.plan_root)
        recovery = self._recover_writer_state(
            task,
            writing_task_ref=writing_task_artifact,
            accepted_plan_ref=accepted_plan_ref,
            project_profile_ref=_as_artifact_ref(manifest.project_profile_root),
        )
        if recovery is None:
            assembly = self._writer_context(
                Stage2MWriterContextInvocation(
                    run_id=task.run_id,
                    task=memory_task,
                    planning_context=planning_context,
                    plan=plan,
                    text=text,
                    world=world,
                    base_commit=task.basis_commit,
                    snapshot_id=task.basis_snapshot,
                    writing_task=writing_task,
                    project_id=task.project_id,
                    advisory_artifact_refs=tuple(
                        ref
                        for ref in task.input_artifact_refs
                        if ref.media_type == "application/vnd.novel-agent.quarantine-package+json"
                    ),
                    plan_root_ref=accepted_plan_ref,
                    plan_revision=plan.root_hash.root,
                    chapter_goal_ids=tuple(goal.goal_id for goal in goals),
                    planning_context_ref=planning_context_artifact,
                )
            )
            package = assembly.package
            evidence_ledger = assembly.evidence_ledger
        else:
            package = recovery.package
            evidence_ledger = recovery.evidence_ledger
        readiness = evaluate_writer_readiness(
            plan=plan,
            target_chapter=task.chapter_index,
            writing_task=writing_task,
            world=world,
            package=package,
            expected_plan_root_ref=accepted_plan_ref,
            manifest_plan_revision=plan.root_hash.root,
            projection_exact=self._projection_is_exact(task),
            canonical_prose_present=any(
                scene.blocks for chapter in text.chapters for scene in chapter.scenes
            ),
        )
        if not readiness.ready:
            raise WriterContextInputNotReady(readiness)
        if (
            package.task_contract != memory_task
            or package.basis_commit_id != task.basis_commit
            or package.basis_snapshot_id != task.basis_snapshot
        ):
            raise ValueError("Stage 2M Writer Context changed the durable task basis")
        ledger_ref = self._artifacts.put(
            canonical_json_bytes(evidence_ledger.model_dump(mode="json")),
            EVIDENCE_LEDGER_V2_MEDIA_TYPE,
            self._schema_version,
        )
        if ledger_ref != package.evidence_ledger_ref:
            raise ValueError("Stage 2M package does not bind its persisted EvidenceLedger")
        package_ref = self._artifacts.put(
            canonical_json_bytes(package.model_dump(mode="json")),
            WRITER_CONTEXT_V2_MEDIA_TYPE,
            self._schema_version,
        )
        if recovery is None:
            recent, recent_ref = self._recent_prose.assemble(
                text_root=text,
                base_commit=task.basis_commit,
                snapshot_id=task.basis_snapshot,
                target_chapter=task.chapter_index,
            )
        else:
            recent, recent_ref = recovery.recent_prose, recovery.recent_prose_ref
        attestation = FutureIsolationAttestation(
            attestation_id=bounded_stable_id(
                f"future-isolation.{task.task_id.root}",
                f"future-isolation.{task.basis_commit.root}.{task.chapter_index}",
            ),
            checkpoint_chapter=latest,
            canonical_source_ids=tuple(chapter.chapter_id for chapter in text.chapters),
            evaluator_only_source_ids=(),
            passed=True,
            configuration_fingerprint=(self._policy.future_isolation_configuration_fingerprint),
        )
        return WritingLoopRequest(
            run_id=task.run_id,
            task_id=task.task_id,
            attempt_id=task.current_attempt_id,
            project_id=task.project_id,
            base_commit=task.basis_commit,
            snapshot_id=task.basis_snapshot,
            writing_task=writing_task,
            writing_task_artifact=writing_task_artifact,
            accepted_plan=AcceptedPlanBinding(
                artifact=accepted_plan_ref,
                revision=plan.root_hash.root,
                task_contract_id=writing_task.contract_id,
                base_commit=task.basis_commit,
                snapshot_id=task.basis_snapshot,
            ),
            project_profile_artifact=_as_artifact_ref(manifest.project_profile_root),
            project_profile_revision=profile.root_hash.root,
            writer_context_package=package,
            writer_context_package_artifact=package_ref,
            recent_prose_context=recent,
            recent_prose_context_artifact=recent_ref,
            resume_checkpoint_ref=(None if recovery is None else recovery.checkpoint_ref),
            future_isolation_attestation=attestation,
            allowed_skills=self._policy.allowed_skills,
            budgets=self._policy.budgets,
            writer_configuration_fingerprint=(self._policy.writer_configuration_fingerprint),
            model_configuration_fingerprint=self._policy.model_configuration_fingerprint,
        )

    @staticmethod
    def _task_text(task: WritingTaskContract) -> str:
        obligations = ", ".join(item.root for item in task.active_plan_obligations) or "none"
        return (
            f"Write chapter {task.target_chapter}. Goal: {task.chapter_goal}. "
            f"Active obligations: {obligations}."
        )

    @staticmethod
    def _future_lock_constraints(
        world: WorldRootDocument, chapter_index: int
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        constraints: list[str] = []
        forbids: list[str] = []
        for obligation in world.obligations:
            if not obligation.is_future_locked(chapter_index):
                continue
            boundary = obligation.not_before_chapter
            constraints.append(
                f"{obligation.description}当前只能 SETUP/PROGRESS, 不得 RESOLVE/PAYOFF; "
                f"最早第{boundary}章。"
            )
            forbids.append(f"不得在本章完成{obligation.description}最终获得或宣布该长期目标已解决.")
        return tuple(constraints), tuple(forbids)

    @staticmethod
    def _profile_lock_constraints(
        profile: ProjectProfileRootDocument, chapter_index: int
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        constraints: list[str] = []
        forbids: list[str] = []
        planning = profile.capability_profile.get("planning_constraints", {})
        sources: dict[str, object] = dict(profile.capability_profile)
        if isinstance(planning, dict):
            sources.update(planning)
        # Every compiled author lock channel reaches the Writer.  Equipment and
        # location locks were compiled into the profile but never projected, so a
        # "copper token at the end of volume 1" rule was invisible to the draft.
        for key in (
            "timeline_locks",
            "reveal_windows",
            "progression_locks",
            "equipment_locks",
            "location_preconditions",
        ):
            raw = sources.get(key)
            if not isinstance(raw, list):
                continue
            for item in raw:
                if isinstance(item, dict):
                    text = next(
                        (
                            value.strip()
                            for field in ("description", "reveal", "lock", "value", "summary")
                            for value in (item.get(field),)
                            if isinstance(value, str) and value.strip()
                        ),
                        str(item),
                    )
                    boundary = next(
                        (
                            value
                            for field in ("not_before_chapter", "chapter_start", "start")
                            for value in (item.get(field),)
                            if type(value) is int and value >= 1
                        ),
                        None,
                    )
                    latest = next(
                        (
                            value
                            for field in ("chapter_end", "end", "deadline_chapter")
                            for value in (item.get(field),)
                            if type(value) is int and value >= 1
                        ),
                        None,
                    )
                elif isinstance(item, str) and item.strip():
                    text = item.strip()
                    boundary = None
                    latest = None
                else:
                    continue
                if isinstance(boundary, int) and chapter_index < boundary:
                    protected = (
                        f"Profile {key} contains protected future material locked until "
                        f"chapter {boundary}; do not introduce or infer its people, objects, "
                        "events, identities, mechanisms, locations, or outcomes."
                    )
                    constraints.append(protected)
                    forbids.append(protected)
                    continue
                constraints.append(f"Profile {key}: {text}")
                if isinstance(latest, int) and chapter_index > latest:
                    # A deadline is the opposite of a lock: the chapter must land
                    # the item by then.  "locked until 90" would read as the
                    # inverse of an author rule that says "obtain it by 100".
                    forbids.append(f"Profile {key} is past its chapter {latest} deadline: {text}")
        return tuple(dict.fromkeys(constraints)), tuple(dict.fromkeys(forbids))

    @staticmethod
    def _planning_context(
        task: TaskRecord,
        plan: PlanRootDocument,
        task_intent: str,
        current_goals: tuple[ChapterGoal, ...] | None = None,
    ) -> AuthorPlanningContext:
        del current_goals
        # Writer receives the current chapter contract only. Future ChapterGoals
        # belong to planning/evaluation; exposing chapters N+1/N+2 as ordinary
        # author context caused the Writer to consume the next chapter early.
        target_end = task.chapter_index
        scoped_goal_ids = {
            goal.goal_id
            for goal in plan.chapter_goals
            if task.chapter_index <= goal.chapter_index <= target_end
        }
        scoped_obligation_ids = {
            obligation_id
            for goal in plan.chapter_goals
            if goal.goal_id in scoped_goal_ids
            for obligation_id in goal.obligation_ids
        }
        by_id = {node.plan_node_id: node for node in plan.nodes}

        def covers(node: object, chapter_start: int, chapter_end: int) -> bool:
            start = getattr(node, "chapter_start", None)
            end = getattr(node, "chapter_end", None)
            payload = getattr(node, "payload", {})
            if isinstance(payload, dict):
                chapter_index = payload.get("chapter_index")
                if type(chapter_index) is int and chapter_start <= chapter_index <= chapter_end:
                    return True
            return (
                isinstance(start, int)
                and isinstance(end, int)
                and start <= chapter_end
                and chapter_start <= end
            )

        selected: dict[StableId, object] = {}
        writer_safe_story_types = {"core_premise", "theme", "reader_promise"}

        def story_node_is_visible(node: PlanNode) -> bool:
            return (
                node.plan_level is not PlanLevel.STORY or node.node_type in writer_safe_story_types
            )

        for node in plan.nodes:
            related = (
                node.plan_node_id in scoped_goal_ids
                or bool(set(node.obligation_ids) & scoped_obligation_ids)
                or covers(node, task.chapter_index, target_end)
                or (node.plan_level is PlanLevel.STORY and story_node_is_visible(node))
            )
            if not related:
                continue
            current: PlanNode | None = node
            while current is not None:
                if story_node_is_visible(current):
                    selected[current.plan_node_id] = current
                current = by_id.get(current.parent_id) if current.parent_id is not None else None
        nodes = tuple(
            VisibleOutlineNode(
                node_id=node.plan_node_id,
                title=node.title,
                summary=node.summary,
            )
            for node in plan.nodes
            if node.plan_node_id in selected
        )
        goals = tuple(
            goal
            for goal in plan.chapter_goals
            if task.chapter_index <= goal.chapter_index <= target_end
        )
        source_hash = content_id(
            {
                "plan": plan.root_hash.root,
                "task": task.task_id.root,
                "target": task.chapter_index,
                "intent": task_intent,
            }
        )
        return AuthorPlanningContext(
            profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            task_intent=task_intent,
            target_range=(
                task.chapter_index,
                max((goal.chapter_index for goal in goals), default=task.chapter_index),
            ),
            visible_outline_nodes=nodes,
            chapter_goals=goals,
            source_hash=source_hash,
        )

    def _draft_revision_constraints(self, task: TaskRecord) -> tuple[str, ...]:
        constraints: list[str] = []
        for ref in task.input_artifact_refs:
            if ref.media_type != DRAFT_REVISION_DIRECTIVE_MEDIA_TYPE:
                continue
            try:
                directive = json.loads(self._artifacts.read_verified(ref))
            except (UnicodeDecodeError, ValueError) as error:
                raise ValueError("Draft revision directive is unreadable") from error
            if (
                directive.get("kind") != "draft_revision"
                or directive.get("run_id") != task.run_id.root
                or directive.get("project_id") != task.project_id.root
                or directive.get("target_chapter") != task.chapter_index
            ):
                raise ValueError("Draft revision directive does not match the Writer task")
            reason = directive.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("Draft revision directive requires a non-empty reason")
            recovery_kind = directive.get("recovery_kind")
            source = {
                "automatic_editorial_retry": "自动审校重试",
                "automatic_length_contract_retry": "自动长度合同重试",
            }.get(recovery_kind, "人工拒绝修订")
            constraints.append(
                f"{source}：必须完整重写本章，不得复用被拒稿中的新增设定或跨章事件；"  # noqa: RUF001
                f"修复原因：{reason.strip()}"  # noqa: RUF001
            )
        return tuple(dict.fromkeys(constraints))

    @staticmethod
    def _profile_string(
        profile: ProjectProfileRootDocument,
        key: str,
        default: str,
    ) -> str:
        value = profile.style_profile.get(key, default)
        return value.strip() if isinstance(value, str) and value.strip() else default

    @staticmethod
    def _profile_strings(
        profile: ProjectProfileRootDocument,
        key: str,
    ) -> tuple[str, ...]:
        value = profile.style_profile.get(key, ())
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            return ()
        strings = tuple(item.strip() for item in value if isinstance(item, str))
        return tuple(dict.fromkeys(strings))

    @staticmethod
    def _profile_style_constraints(
        profile: ProjectProfileRootDocument,
    ) -> tuple[str, ...]:
        constraints: list[str] = []
        for key in (
            "genre",
            "style",
            "genre_required_markers",
            "genre_warning_domains",
            "paragraph_policy",
            "template_watchlist",
        ):
            value = profile.style_profile.get(key)
            if value not in (None, "", (), []):
                constraints.append(f"Profile {key}: {value}")
        prose_preferences = profile.style_profile.get("prose_preferences")
        if isinstance(prose_preferences, dict):
            constraints.append(f"Profile prose_preferences: {prose_preferences}")
        elif isinstance(prose_preferences, list):
            constraints.extend(
                f"Profile prose_preferences: {item[:280]}"
                for item in prose_preferences[:3]
                if isinstance(item, str) and item.strip()
            )
        examples = profile.style_profile.get("style_examples")
        if isinstance(examples, list):
            constraints.extend(
                f"Profile style_example: {item[:280]}"
                for item in examples[:3]
                if isinstance(item, str) and item.strip()
            )
        guides = profile.style_profile.get("style_guide_sources")
        if isinstance(guides, list):
            for guide in guides:
                if not isinstance(guide, dict):
                    continue
                source_id = guide.get("source_id", "unknown")
                text = guide.get("text")
                if isinstance(text, str) and text.strip():
                    constraints.append(f"Style Guide {source_id}: {text}")
        return tuple(dict.fromkeys(constraints))

    def _length_policy(self, profile: ProjectProfileRootDocument) -> WritingLengthPolicy:
        minimum = self._profile_int(
            profile, "minimum_characters", self._policy.length_policy.minimum_characters
        )
        maximum = self._profile_int(
            profile, "maximum_characters", self._policy.length_policy.maximum_characters
        )
        target_default = (minimum + maximum) // 2
        target = self._profile_int(profile, "target_characters", target_default)
        if maximum < minimum:
            minimum, maximum = (
                self._policy.length_policy.minimum_characters,
                self._policy.length_policy.maximum_characters,
            )
        target = max(minimum, min(maximum, target))
        return WritingLengthPolicy(
            minimum_characters=minimum,
            target_characters=target,
            maximum_characters=maximum,
        )

    @staticmethod
    def _profile_int(profile: ProjectProfileRootDocument, key: str, default: int) -> int:
        value = profile.style_profile.get(key, default)
        return value if type(value) is int and value > 0 else default

    @staticmethod
    def _payload_strings(payload: Mapping[str, object], *keys: str) -> tuple[str, ...]:
        values: list[str] = []
        for key in keys:
            raw = payload.get(key)
            if isinstance(raw, list):
                values.extend(
                    item.strip() for item in raw if isinstance(item, str) and item.strip()
                )
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _payload_state_changes(payload: Mapping[str, object]) -> tuple[str, ...]:
        raw = payload.get("state_changes")
        if not isinstance(raw, list):
            return ()
        values: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                values.append(item.strip())
            elif isinstance(item, dict):
                for key in ("description", "change", "state", "value"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        values.append(value.strip())
                        break
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _payload_ids(payload: Mapping[str, object], *keys: str) -> tuple[StableId, ...]:
        values: list[StableId] = []
        for key in keys:
            raw = payload.get(key)
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, str):
                        values.append(StableId(item))
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _payload_obligation_actions(
        payload: Mapping[str, object], known_ids: set[StableId]
    ) -> tuple[str, ...]:
        raw = payload.get("obligation_actions")
        if not isinstance(raw, list):
            return ()
        values: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                values.append(item.strip())
                continue
            if not isinstance(item, dict):
                continue
            raw_id = item.get("obligation_id") or item.get("id")
            if not isinstance(raw_id, str):
                raise ValueError("obligation action requires obligation_id")
            obligation_id = StableId(raw_id)
            if obligation_id not in known_ids:
                raise ValueError(f"obligation action references unknown {raw_id}")
            action = str(item.get("action") or item.get("operation") or "SETUP").strip()
            detail = str(item.get("description") or "").strip()
            values.append(f"{raw_id}:{action}" + (f":{detail}" if detail else ""))
        return tuple(dict.fromkeys(values))


class Stage3WritingLeafAdapter:
    """Bind Stage 5 only to Stage 3 request/result and immutable evidence lineage."""

    is_fixture = False

    def __init__(
        self,
        loop: WriterContextLoopService,
        model_request_factory: Callable[[WritingLoopRequest], ModelRequest],
        reactive_inputs_factory: Callable[[WritingLoopRequest], ReactiveMemoryInputs],
    ) -> None:
        self._loop = loop
        self._model_request_factory = model_request_factory
        self._reactive_inputs_factory = reactive_inputs_factory

    async def run(self, request: WritingLoopRequest) -> WritingLoopResult:
        result = await self._loop.execute(
            request,
            self._model_request_factory(request),
            self._reactive_inputs_factory(request),
        )
        if result.run_id != request.run_id or result.task_id != request.task_id:
            raise RuntimeError("Stage 3 Writer returned cross-task lineage")
        return result


def _as_artifact_ref(ref: ArtifactRef) -> ArtifactRef:
    """Strip typed root extras so WriterWorkPlan lineage can echo ArtifactRef."""

    return ArtifactRef(
        artifact_id=ref.artifact_id,
        media_type=ref.media_type,
        byte_length=ref.byte_length,
        schema_version=ref.schema_version,
    )


__all__ = [
    "ProductionWritingRequestFactory",
    "Stage2MWriterContextFactory",
    "Stage2MWriterContextInvocation",
    "Stage3WritingLeafAdapter",
    "WritingRequestPolicy",
]
