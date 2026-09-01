"""Named production callables used by the unique composition root."""

from __future__ import annotations

from novel_agent.adapters.memory_write import TeacherForcedCuratorPort
from novel_agent.adapters.runtime.stage3_writer import Stage2MWriterContextInvocation
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.creative_runtime import CreativeRunPolicy
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import RunId, TaskId, bounded_stable_id
from novel_agent.domain.memory import DerivedBuildStatus, WorldRootDocument
from novel_agent.domain.memory_write import CuratorProposalRejection, QuarantinePackage
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentMode,
    ContextBudget,
    MemoryResolutionRequest,
    RequiredSnapshotPolicy,
    RetrievalBudget,
)
from novel_agent.domain.writer_context import BenchmarkTaskContract
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.evidence_first_writer_context_assembler import (
    EvidenceFirstAssemblyResult,
    EvidenceFirstWriterContextAssembler,
    NeedEvidenceSelection,
)
from novel_agent.services.memory_gateway import MemoryGateway
from novel_agent.services.projection import DerivedSnapshotRepository
from novel_agent.services.task_conditioned_need_generation import TaskPlanConditionedNeedGenerator
from novel_agent.services.task_focus import TaskFocusExtractor
from novel_agent.services.writer_reactive_memory import ReactiveMemoryInputs

QUARANTINE_PACKAGE_MEDIA_TYPE = "application/vnd.novel-agent.quarantine-package+json"


def utf8_quarter_token_count(text: str) -> int:
    return max(1, len(text) // 4) if text else 1


class BoundPolicyResolver:
    def __init__(self, policy: CreativeRunPolicy) -> None:
        self._policy = policy

    def __call__(self, policy_hash: str) -> CreativeRunPolicy:
        if policy_hash != self._policy.policy_hash:
            raise KeyError(policy_hash)
        return self._policy


class BoundPermissionResolver:
    def __init__(self, permission_hash: str) -> None:
        self._permission_hash = permission_hash

    def __call__(self, project_id: str) -> str:
        del project_id
        return self._permission_hash


class ExactSnapshotFreshnessCheck:
    def __init__(self, snapshots: DerivedSnapshotRepository) -> None:
        self._snapshots = snapshots

    def __call__(self, request: MemoryResolutionRequest) -> bool:
        snapshot = self._snapshots.get_for_commit(request.base_commit)
        if snapshot is None:
            return True
        return (
            snapshot.snapshot_id == request.snapshot_id
            and snapshot.build_status is DerivedBuildStatus.EXACT
        )


class SettlementTextReveal:
    def __init__(self, curator: TeacherForcedCuratorPort) -> None:
        self._curator = curator

    def __call__(self, text: TextRootDocument) -> None:
        self._curator.set_revealed_text(text)


class ProposedTextRootLoader:
    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts

    def __call__(self, ref: ArtifactRef) -> TextRootDocument:
        return TextRootDocument.model_validate_json(
            self._artifacts.read_verified(ref),
            strict=True,
        )


class ProductionWriterModelRequestFactory:
    def __init__(
        self,
        *,
        role: ModelRole,
        purpose: ModelCallPurpose,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> None:
        self._role = role
        self._purpose = purpose
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds

    def __call__(self, request: WritingLoopRequest) -> ModelRequest:
        attempt_suffix = "" if request.attempt_id is None else f".{request.attempt_id.root}"
        return ModelRequest(
            request_id=bounded_stable_id(
                f"model-request.{request.task_id.root}.writer{attempt_suffix}",
                f"model-request.{request.run_id.root}.writer{attempt_suffix}",
                *(
                    (
                        f"model-request.{request.attempt_id.root}.writer",
                        request.attempt_id.root,
                    )
                    if request.attempt_id is not None
                    else ()
                ),
            ),
            run_id=request.run_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            model_role=self._role,
            purpose=self._purpose,
            trace_id=f"trace.{request.run_id.root}.{request.task_id.root}",
            prompt="",
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
            enable_thinking=False,
        )


class ProductionCuratorModelRequestFactory:
    def __init__(
        self,
        *,
        run_id: RunId,
        task_id: TaskId,
        max_output_tokens: int,
        timeout_seconds: float,
        request_namespace: str = "curator",
    ) -> None:
        self._run_id = run_id
        self._task_id = task_id
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        if not request_namespace:
            raise ValueError("production memory-write request namespace is required")
        self._request_namespace = request_namespace
        self._sequence = 0

    def __call__(self, phase: str, mode: AgentMode) -> ModelRequest:
        self._sequence += 1
        sequence = str(self._sequence)
        return ModelRequest(
            request_id=bounded_stable_id(
                f"model-request.{self._request_namespace}.{self._run_id.root}"
                f".{self._task_id.root}.{sequence}.{phase}",
                f"model-request.{self._request_namespace}.{self._task_id.root}.{sequence}.{phase}",
                f"model-request.{self._request_namespace}.{self._run_id.root}.{sequence}.{phase}",
                f"model-request.{self._task_id.root}.{sequence}.{phase}",
                f"model-request.{self._run_id.root}.{sequence}.{phase}",
                f"model-request.{self._request_namespace}.{self._task_id.root}.{sequence}",
                f"model-request.{self._request_namespace}.{self._run_id.root}.{sequence}",
            ),
            run_id=self._run_id,
            task_id=self._task_id,
            model_role=ModelRole.IMPLEMENTATION,
            purpose=ModelCallPurpose.DEVELOPMENT,
            trace_id=f"trace.{self._run_id.root}.curator",
            prompt="",
            agent_mode=mode.value,
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
            enable_thinking=False,
        )


class ProductionReactiveMemoryInputsFactory:
    def __init__(self, commits: CommitService, artifacts: ArtifactRepository) -> None:
        self._commits = commits
        self._artifacts = artifacts
        self._focus = TaskFocusExtractor()

    def __call__(self, request: WritingLoopRequest) -> ReactiveMemoryInputs:
        manifest = self._commits.load_manifest(request.base_commit)
        plan = PlanRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.plan_root), strict=True
        )
        text = TextRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.text_root), strict=True
        )
        world = WorldRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.world_root), strict=True
        )
        # Canon bytes may retain the producing-commit label from import; the
        # reactive Memory view is the WorldRoot currently bound at this task basis.
        if world.source_commit != request.base_commit:
            world = world.model_copy(update={"source_commit": request.base_commit})
        task = request.writer_context_package.task_contract
        if not isinstance(task, BenchmarkTaskContract):
            raise ValueError("production reactive Memory requires a v2 task contract")
        template = MemoryResolutionRequest(
            request_id=bounded_stable_id(
                f"reactive-template.{request.task_id.root}",
                f"reactive-template.{request.base_commit.root}.{request.writing_task.target_chapter}",
            ),
            run_id=request.run_id,
            task_id=request.task_id,
            project_id=request.project_id,
            base_commit=request.base_commit,
            snapshot_id=request.snapshot_id,
            required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
            task_contract=request.writing_task.chapter_goal,
            initial_memory_needs=(),
            worldline="main",
            narrative_chapter=request.writing_task.target_chapter,
            access_scope=AccessScope.WRITER_SAFE,
            allow_future_plan=False,
            retrieval_budget=RetrievalBudget(),
            context_budget=ContextBudget(token_budget=12_000),
        )
        return ReactiveMemoryInputs(
            task=task,
            world=world,
            plan=plan,
            focus_set=self._focus.extract(task, world, plan),
            text_root=text,
            resolution_template=template,
            thread_id=f"writer-reactive.{request.task_id.root}",
        )


class ProductionStage2MWriterContext:
    is_fixture = False

    def __init__(
        self,
        *,
        generator: TaskPlanConditionedNeedGenerator,
        gateway: MemoryGateway,
        assembler: EvidenceFirstWriterContextAssembler,
        artifacts: ArtifactRepository,
    ) -> None:
        self._generator = generator
        self._gateway = gateway
        self._assembler = assembler
        self._artifacts = artifacts

    def __call__(self, invocation: Stage2MWriterContextInvocation) -> EvidenceFirstAssemblyResult:
        if invocation.project_id is None:
            raise ValueError("production Stage 2M Writer Context requires a project id")
        generated = self._generator.generate_with_lineage(
            invocation.task,
            invocation.world,
            invocation.plan,
            invocation.planning_context,
            history_text=invocation.text,
            snapshot_id=invocation.snapshot_id,
        ).needs
        if not generated:
            raise ValueError("production Stage 2M Writer Context produced no Memory Needs")
        needs = tuple(
            need.model_copy(
                update={
                    "run_id": invocation.run_id,
                    "task_id": TaskId(invocation.task.task_id.root),
                    "base_commit": invocation.base_commit,
                }
            )
            for need in generated
        )
        resolution = MemoryResolutionRequest(
            request_id=bounded_stable_id(
                f"memory-request.{invocation.task.task_id.root}",
                f"memory-request.{invocation.base_commit.root}.{invocation.task.target_chapter_start}",
            ),
            run_id=invocation.run_id,
            task_id=TaskId(invocation.task.task_id.root),
            project_id=invocation.project_id,
            base_commit=invocation.base_commit,
            snapshot_id=invocation.snapshot_id,
            required_snapshot_policy=RequiredSnapshotPolicy.EXACT,
            task_contract=invocation.task.task_text,
            initial_memory_needs=needs,
            worldline="main",
            narrative_chapter=invocation.task.target_chapter_start,
            access_scope=AccessScope.WRITER_SAFE,
            allow_future_plan=False,
            retrieval_budget=RetrievalBudget(),
            context_budget=ContextBudget(token_budget=12_000),
        )
        self._gateway.resolve(
            resolution,
            invocation.text,
            thread_id=f"writer-context.{invocation.task.task_id.root}",
        )
        selections = tuple(NeedEvidenceSelection(need=need) for need in needs)
        advisory_items = tuple(
            (
                ref,
                self._advisory_text(ref),
            )
            for ref in invocation.advisory_artifact_refs
        )
        return self._assembler.assemble(
            task=invocation.task,
            selections=selections,
            text_root=invocation.text,
            basis_commit_id=invocation.base_commit,
            basis_snapshot_id=invocation.snapshot_id,
            advisory_items=advisory_items,
        )

    def _advisory_text(self, ref: ArtifactRef) -> str:
        if ref.media_type != QUARANTINE_PACKAGE_MEDIA_TYPE:
            raise ValueError("Writer advisory ref is not a QuarantinePackage")
        package = QuarantinePackage.model_validate_json(
            self._artifacts.read_verified(ref),
            strict=True,
        )
        feedback: list[str] = []
        for rejection_ref in package.proposal_rejection_refs[:3]:
            rejection = CuratorProposalRejection.model_validate_json(
                self._artifacts.read_verified(rejection_ref),
                strict=True,
            )
            feedback.extend(rejection.safe_feedback[:2])
        detail = "; ".join(item.strip() for item in feedback if item.strip())
        if not detail:
            detail = package.terminal_reason
        return (
            "unverified=true; advisory_only=true; upstream Memory proposal was not written to "
            "Canonical World. Treat the following as an open lead, never as an established fact: "
            f"{detail[:720]}"
        )


__all__ = [
    "BoundPermissionResolver",
    "BoundPolicyResolver",
    "ExactSnapshotFreshnessCheck",
    "ProductionCuratorModelRequestFactory",
    "ProductionReactiveMemoryInputsFactory",
    "ProductionStage2MWriterContext",
    "ProductionWriterModelRequestFactory",
    "ProposedTextRootLoader",
    "SettlementTextReveal",
    "utf8_quarter_token_count",
]
