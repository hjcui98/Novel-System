"""Real adapter to the public Stage 3 Writer Context Loop boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import (
    AuthorPlanningContext,
    ChapterGoal,
    PlanRootDocument,
    TextRootDocument,
    VisibleOutlineNode,
)
from novel_agent.domain.generation import (
    AcceptedPlanBinding,
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
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.runtime import TaskRecord
from novel_agent.domain.stage2 import FutureIsolationAttestation, ProjectProfileRootDocument
from novel_agent.domain.world import PlanLevel, PlanNode
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
)
from novel_agent.domain.writer_readiness import (
    WriterContextInputNotReady,
    evaluate_writer_readiness,
)
from novel_agent.domain.writing_loop import (
    WRITING_LOOP_CHECKPOINT_MEDIA_TYPE,
    WritingLoopResult,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.evidence_first_writer_context_assembler import (
    EvidenceFirstAssemblyResult,
)
from novel_agent.services.recent_prose import RecentProseAssembler
from novel_agent.services.writer_context_loop import WriterContextLoopService
from novel_agent.services.writer_reactive_memory import ReactiveMemoryInputs

WRITING_TASK_MEDIA_TYPE = "application/vnd.novel-agent.writing-task+json"
WRITER_CONTEXT_V2_MEDIA_TYPE = "application/vnd.novel-agent.writer-context-v2+json"
EVIDENCE_LEDGER_V2_MEDIA_TYPE = "application/vnd.novel-agent.evidence-ledger-v2+json"
AUTHOR_PLANNING_CONTEXT_MEDIA_TYPE = (
    "application/vnd.novel-agent.author-planning-context+json"
)


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
    ) -> None:
        self._commits = commits
        self._artifacts = artifacts
        self._recent_prose = recent_prose
        self._writer_context = writer_context
        self._policy = policy
        self._schema_version = schema_version

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
        goal_ids = {goal.goal_id for goal in goals}
        obligation_ids = tuple(
            dict.fromkeys(item for goal in goals for item in goal.obligation_ids)
        )
        world_obligation_ids = {item.obligation_id for item in world.obligations}
        unknown_obligations = set(obligation_ids) - world_obligation_ids
        if unknown_obligations:
            raise ValueError(
                "WritingTask references unknown obligations: "
                + ", ".join(sorted(item.root for item in unknown_obligations))
            )
        relevant_nodes = tuple(
            node
            for node in plan.nodes
            if node.plan_node_id in goal_ids or bool(set(node.obligation_ids) & set(obligation_ids))
        )
        summaries = tuple(dict.fromkeys(goal.summary for goal in goals))
        chapter_goal = "；".join(summaries)
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
                    *(node.summary for node in relevant_nodes),
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
        current_state_constraints = tuple(
            f"Canon current state [{state.subject_id.root}] "
            f"{entity_labels.get(state.subject_id, state.subject_id.root)} "
            f"{state.predicate}={state.value}"
            for state in world.states
            if state.subject_id in set(participating_entity_ids)
        )
        obligation_actions = tuple(
            dict.fromkeys(
                action
                for goal in goals
                for action in self._payload_obligation_actions(goal.payload, world_obligation_ids)
            )
        )
        advisory_entries = tuple(
            raw
            for goal in goals
            for raw in (
                goal.payload.get("unresolved_advisories")
                if isinstance(goal.payload.get("unresolved_advisories"), list)
                else ()
            )
            if isinstance(raw, dict)
        )
        advisory_constraints = tuple(
            "未决 advisory (不得当作已证实事实)\uff1a" + summary
            for raw in advisory_entries
            if isinstance(summary := raw.get("summary"), str) and summary.strip()
        )
        advisory_forbidden = tuple(
            assumption.strip()
            for raw in advisory_entries
            for assumption in (
                raw.get("forbidden_assumptions")
                if isinstance(raw.get("forbidden_assumptions"), list)
                else ()
            )
            if isinstance(assumption, str) and assumption.strip()
        )
        lock_constraints, lock_forbids = self._future_lock_constraints(world, task.chapter_index)
        profile_lock_constraints, profile_lock_forbids = self._profile_lock_constraints(
            profile, task.chapter_index
        )
        language = self._profile_string(profile, "language", "")
        language_constraint = (f"正文语言：{language}",) if language else ()
        language_allowlist = self._profile_strings(profile, "language_allowlist")
        language_allow_constraint = (
            ("允许英文代号\uff1a" + ", ".join(language_allowlist),)
            if language_allowlist
            else ()
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
                *self._profile_strings(profile, "mandatory_constraints"),
                *lock_constraints,
                *profile_lock_constraints,
                *advisory_constraints,
            ),
            forbidden_reveals=(
                *self._profile_strings(profile, "forbidden_reveals"),
                *lock_forbids,
                *profile_lock_forbids,
                *advisory_forbidden,
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
        readiness = evaluate_writer_readiness(
            plan=plan,
            target_chapter=task.chapter_index,
            writing_task=writing_task,
            world=world,
            package=package,
            expected_plan_root_ref=accepted_plan_ref,
            manifest_plan_revision=plan.root_hash.root,
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
            canonical_json_bytes(assembly.evidence_ledger.model_dump(mode="json")),
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
        recent, recent_ref = self._recent_prose.assemble(
            text_root=text,
            base_commit=task.basis_commit,
            snapshot_id=task.basis_snapshot,
            target_chapter=task.chapter_index,
        )
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
            resume_checkpoint_ref=next(
                (
                    ref
                    for ref in reversed(task.terminal_artifact_refs)
                    if ref.media_type == WRITING_LOOP_CHECKPOINT_MEDIA_TYPE
                ),
                None,
            ),
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
        for key in ("timeline_locks", "reveal_windows", "progression_locks"):
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
                elif isinstance(item, str) and item.strip():
                    text = item.strip()
                    boundary = None
                else:
                    continue
                constraints.append(f"Profile {key}: {text}")
                if isinstance(boundary, int) and chapter_index < boundary:
                    forbids.append(f"Profile {key} is locked until chapter {boundary}: {text}")
        return tuple(dict.fromkeys(constraints)), tuple(dict.fromkeys(forbids))

    @staticmethod
    def _planning_context(
        task: TaskRecord,
        plan: PlanRootDocument,
        task_intent: str,
        current_goals: tuple[ChapterGoal, ...] | None = None,
    ) -> AuthorPlanningContext:
        del current_goals
        target_end = min(task.horizon_end or task.chapter_index + 2, task.chapter_index + 2)
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
        for node in plan.nodes:
            related = (
                node.plan_node_id in scoped_goal_ids
                or bool(set(node.obligation_ids) & scoped_obligation_ids)
                or covers(node, task.chapter_index, target_end)
                or node.plan_level is PlanLevel.STORY
            )
            if not related:
                continue
            current: PlanNode | None = node
            while current is not None:
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
