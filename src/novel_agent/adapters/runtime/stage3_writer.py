"""Real adapter to the public Stage 3 Writer Context Loop boundary."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

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
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    ContextAssemblyStatus,
    WriterContextPackageV2,
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
from novel_agent.services.planning_contracts import (
    chapter_plan_nodes,
    dependency_constraints,
    effective_obligations,
)
from novel_agent.services.recent_prose import RecentProseAssembler
from novel_agent.services.writer_context_loop import WriterContextLoopService
from novel_agent.services.writer_reactive_memory import ReactiveMemoryInputs

WRITING_TASK_MEDIA_TYPE = "application/vnd.novel-agent.writing-task+json"
WRITER_CONTEXT_V2_MEDIA_TYPE = "application/vnd.novel-agent.writer-context-v2+json"
EVIDENCE_LEDGER_V2_MEDIA_TYPE = "application/vnd.novel-agent.evidence-ledger-v2+json"


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
    project_id: ProjectId | None = None
    advisory_artifact_refs: tuple[ArtifactRef, ...] = ()


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
        checkpoint_ref = next(
            (
                ref
                for ref in reversed(task.terminal_artifact_refs)
                if ref.media_type == WRITING_LOOP_CHECKPOINT_MEDIA_TYPE
            ),
            None,
        )
        if checkpoint_ref is not None:
            checkpoint = WritingLoopCheckpoint.model_validate_json(
                self._artifacts.read_verified(checkpoint_ref)
            )
            if (
                checkpoint.run_id != task.run_id
                or checkpoint.task_id != task.task_id
                or checkpoint.base_commit != task.basis_commit
                or checkpoint.snapshot_id != task.basis_snapshot
                or checkpoint.accepted_plan_ref.artifact_id != manifest.plan_root.artifact_id
                or checkpoint.project_profile_ref.artifact_id
                != manifest.project_profile_root.artifact_id
            ):
                raise ValueError("frozen Writer checkpoint differs from current task/Canon basis")
            if checkpoint.frozen_request_ref is not None:
                frozen = WritingLoopRequest.model_validate_json(
                    self._artifacts.read_verified(checkpoint.frozen_request_ref)
                )
                if (
                    frozen.writer_configuration_fingerprint
                    != self._policy.writer_configuration_fingerprint
                    or frozen.model_configuration_fingerprint
                    != self._policy.model_configuration_fingerprint
                    or frozen.allowed_skills != self._policy.allowed_skills
                    or frozen.budgets != self._policy.budgets
                ):
                    raise ValueError("Writer configuration changed; explicit rebase is required")
            else:
                # Legacy checkpoints already freeze these artifacts individually.
                def read(ref: ArtifactRef) -> dict[str, object]:
                    return cast(dict[str, object], json.loads(self._artifacts.read_verified(ref)))

                contract = read(checkpoint.writing_task_ref)
                payload = {
                    "run_id": task.run_id.root,
                    "task_id": task.task_id.root,
                    "project_id": task.project_id.root,
                    "base_commit": task.basis_commit.root,
                    "snapshot_id": task.basis_snapshot.root,
                    "writing_task": contract,
                    "writing_task_artifact": checkpoint.writing_task_ref.model_dump(mode="json"),
                    "accepted_plan": {
                        "artifact": checkpoint.accepted_plan_ref.model_dump(mode="json"),
                        "revision": plan.root_hash.root,
                        "task_contract_id": contract["contract_id"],
                        "base_commit": task.basis_commit.root,
                        "snapshot_id": task.basis_snapshot.root,
                    },
                    "project_profile_artifact": checkpoint.project_profile_ref.model_dump(
                        mode="json"
                    ),
                    "project_profile_revision": profile.root_hash.root,
                    "writer_context_package": read(checkpoint.writer_context_ref),
                    "writer_context_package_artifact": checkpoint.writer_context_ref.model_dump(
                        mode="json"
                    ),
                    "recent_prose_context": read(checkpoint.recent_prose_ref),
                    "recent_prose_context_artifact": checkpoint.recent_prose_ref.model_dump(
                        mode="json"
                    ),
                    "future_isolation_attestation": {
                        "attestation_id": "future-isolation.restored",
                        "checkpoint_chapter": task.chapter_index - 1,
                        "canonical_source_ids": [item.chapter_id.root for item in text.chapters],
                        "evaluator_only_source_ids": [],
                        "passed": True,
                        "configuration_fingerprint": (
                            self._policy.future_isolation_configuration_fingerprint.root
                        ),
                    },
                    "allowed_skills": [item.root for item in self._policy.allowed_skills],
                    "budgets": self._policy.budgets.model_dump(mode="json"),
                    "writer_configuration_fingerprint": (
                        self._policy.writer_configuration_fingerprint.root
                    ),
                    "model_configuration_fingerprint": (
                        self._policy.model_configuration_fingerprint.root
                    ),
                }
                frozen = WritingLoopRequest.model_validate_json(json.dumps(payload))
            if (
                frozen.base_commit != task.basis_commit
                or frozen.snapshot_id != task.basis_snapshot
                or frozen.task_id != task.task_id
                or frozen.run_id != task.run_id
                or frozen.writing_task.target_chapter != task.chapter_index
                or frozen.writing_task_artifact != checkpoint.writing_task_ref
                or frozen.accepted_plan.artifact != checkpoint.accepted_plan_ref
                or frozen.project_profile_artifact != checkpoint.project_profile_ref
                or frozen.writer_context_package_artifact != checkpoint.writer_context_ref
                or frozen.recent_prose_context_artifact != checkpoint.recent_prose_ref
                or frozen.future_isolation_attestation.configuration_fingerprint
                != self._policy.future_isolation_configuration_fingerprint
            ):
                raise ValueError("frozen Writer request belongs to another task")
            return frozen.model_copy(
                update={
                    "attempt_id": task.current_attempt_id,
                    "resume_checkpoint_ref": checkpoint_ref,
                }
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
        relevant_nodes = chapter_plan_nodes(plan, task.chapter_index)
        obligation_ids = tuple(
            dict.fromkeys(
                (
                    *[item for goal in goals for item in goal.obligation_ids],
                    *[item for node in relevant_nodes for item in node.obligation_ids],
                )
            )
        )
        summaries = tuple(dict.fromkeys(goal.summary for goal in goals))
        chapter_goal = "；".join(summaries)  # noqa: RUF001
        # Parent outcomes become due at their boundary, not on every child chapter.
        due_nodes = tuple(node for node in relevant_nodes if node.chapter_end == task.chapter_index)
        required_beats = tuple(
            dict.fromkeys(
                outcome
                for source in (*goals, *due_nodes)
                for outcome in (
                    source.required_outcomes or ((source.summary,) if source in goals else ())
                )
            )
        )
        due_obligations = tuple(
            obligation
            for obligation in effective_obligations(plan, world)
            if obligation.due_chapter is not None
            and obligation.due_chapter <= task.chapter_index
            and obligation.status.value not in {"resolved", "abandoned"}
        )
        required_beats = tuple(
            dict.fromkeys(
                (
                    *required_beats,
                    *(f"Fulfill due obligation: {item.description}" for item in due_obligations),
                )
            )
        )
        acceptance_criteria = tuple(
            dict.fromkeys(
                criterion
                for source in (*goals, *due_nodes)
                for criterion in source.acceptance_criteria
            )
        )
        entry_nodes = tuple(
            node for node in relevant_nodes if (node.chapter_start or 1) == task.chapter_index
        )
        preconditions = tuple(
            dict.fromkeys(
                value for source in (*goals, *entry_nodes) for value in source.preconditions
            )
        )
        invariants = tuple(
            dict.fromkeys(
                value for source in (*goals, *relevant_nodes) for value in source.invariants
            )
        )
        forbidden = tuple(
            dict.fromkeys(
                value for source in (*goals, *relevant_nodes) for value in source.forbidden_outcomes
            )
        )
        parent_constraints = tuple(
            f"Parent plan {node.plan_node_id.root} "
            f"(chapters {node.chapter_start}-{node.chapter_end}): "
            f"{node.summary}. Required by its end: {'; '.join(node.required_outcomes)}"
            for node in relevant_nodes
            if node not in due_nodes
        )
        lock_constraints, lock_forbids = self._future_lock_constraints(
            world, task.chapter_index, plan=plan
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
            acceptance_criteria=acceptance_criteria,
            active_plan_obligations=obligation_ids,
            entry_conditions=preconditions,
            mandatory_constraints=(
                *self._profile_strings(profile, "mandatory_constraints"),
                *lock_constraints,
                *invariants,
                *dependency_constraints(
                    plan,
                    world,
                    tuple(
                        item
                        for source in (*goals, *entry_nodes, *due_nodes)
                        for item in source.dependency_ids
                    ),
                ),
                *parent_constraints,
            ),
            forbidden_reveals=(
                *self._profile_strings(profile, "forbidden_reveals"),
                *lock_forbids,
                *forbidden,
            ),
            preserve_requirements=self._profile_strings(profile, "preserve_requirements"),
            style_requirements=self._profile_strings(profile, "style_requirements"),
            length_policy=self._policy.length_policy,
        )
        writing_task_artifact = self._artifacts.put(
            canonical_json_bytes(writing_task.model_dump(mode="json")),
            WRITING_TASK_MEDIA_TYPE,
            self._schema_version,
        )
        planning_context = self._planning_context(task, plan, chapter_goal, goals)
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
                project_id=task.project_id,
                advisory_artifact_refs=tuple(
                    ref
                    for ref in task.input_artifact_refs
                    if ref.media_type == "application/vnd.novel-agent.quarantine-package+json"
                ),
            )
        )
        package = assembly.package
        if (
            assembly.status is ContextAssemblyStatus.READY
            and isinstance(package, WriterContextPackageV2)
            and package.semantic_status == "COMPLETE"
            and (package.unclosed_mandatory_need_facets or package.usable_with_gaps)
        ):
            raise ValueError("READY assembly cannot rewrite semantic incompleteness as COMPLETE")
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
                artifact=_as_artifact_ref(manifest.plan_root),
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
            f"Active obligations: {obligations}. "
            f"Entry conditions: {task.entry_conditions}. "
            f"Persistent constraints: {task.mandatory_constraints}. "
            f"Required outcomes: {task.required_beats}. "
            f"Acceptance: {task.acceptance_criteria}."
        )

    @staticmethod
    def _future_lock_constraints(
        world: WorldRootDocument, chapter_index: int, *, plan: PlanRootDocument | None = None
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        constraints: list[str] = []
        forbids: list[str] = []
        obligations = world.obligations if plan is None else effective_obligations(plan, world)
        for obligation in obligations:
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
    def _planning_context(
        task: TaskRecord,
        plan: PlanRootDocument,
        task_intent: str,
        current_goals: tuple[ChapterGoal, ...] | None = None,
    ) -> AuthorPlanningContext:
        scoped_goals = current_goals or tuple(
            goal for goal in plan.chapter_goals if goal.chapter_index == task.chapter_index
        )
        selected = {node.plan_node_id for node in chapter_plan_nodes(plan, task.chapter_index)}
        nodes = tuple(
            VisibleOutlineNode(
                node_id=node.plan_node_id,
                title=node.title,
                summary=canonical_json_bytes(node.model_dump(mode="json")).decode("utf-8"),
            )
            for node in plan.nodes
            if node.plan_node_id in selected
        )
        goals = scoped_goals
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
            target_range=(task.chapter_index, task.chapter_index),
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
