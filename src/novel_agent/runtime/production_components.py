"""Named production callables used by the unique composition root."""

from __future__ import annotations

from novel_agent.adapters.memory_write import TeacherForcedCuratorPort
from novel_agent.adapters.runtime.stage3_writer import Stage2MWriterContextInvocation
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.creative_runtime import CreativeRunPolicy
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import RunId, SchemaVersion, TaskId, bounded_stable_id
from novel_agent.domain.memory import DerivedBuildStatus, Stage1MemoryNeed, WorldRootDocument
from novel_agent.domain.memory_write import CuratorProposalRejection, QuarantinePackage
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentMode,
    ContextBudget,
    ControllerStopReason,
    MemoryGatewayResult,
    MemoryResolutionRequest,
    RequiredSnapshotPolicy,
    RetrievalBudget,
)
from novel_agent.domain.writer_context import (
    BenchmarkTaskContract,
    ContextAssemblyStatus,
    EvidenceGapKind,
    MemoryContextBudgetExhaustedError,
    MemoryContextBudgetExpansionReceipt,
    MemoryContextBudgetTier,
    MemoryContextBudgetTierRecord,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.evidence_first_writer_context_assembler import (
    NEED_EVIDENCE_SELECTIONS_MEDIA_TYPE,
    EvidenceFirstAssemblyResult,
    EvidenceFirstWriterContextAssembler,
    FrozenNeedEvidenceSelections,
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
        temperature: float = 0.8,
        enable_thinking: bool = True,
        thinking_token_budget: int = 2_048,
    ) -> None:
        self._role = role
        self._purpose = purpose
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._temperature = temperature
        self._enable_thinking = enable_thinking
        self._thinking_token_budget = thinking_token_budget

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
            temperature=self._temperature,
            enable_thinking=self._enable_thinking,
            thinking_token_budget=(self._thinking_token_budget if self._enable_thinking else None),
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


MEMORY_CONTEXT_BUDGET_TIERS: tuple[tuple[MemoryContextBudgetTier, int, int, int], ...] = (
    (MemoryContextBudgetTier.BASE, 24_000, 24_000, 16),
    (MemoryContextBudgetTier.EXPAND_1, 32_000, 32_000, 20),
    (MemoryContextBudgetTier.EXPAND_2, 40_000, 40_000, 24),
)
BUDGET_EXPANSION_RECEIPT_MEDIA_TYPE = (
    "application/vnd.novel-agent.memory-context-budget-expansion+json"
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
        schema_version: SchemaVersion | None = None,
    ) -> None:
        self._generator = generator
        self._gateway = gateway
        self._assembler = assembler
        self._artifacts = artifacts
        self._schema_version = schema_version or SchemaVersion("1.0.0")

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
        advisory_items = tuple(
            (ref, self._advisory_text(ref)) for ref in invocation.advisory_artifact_refs
        )
        records: list[MemoryContextBudgetTierRecord] = []
        gateway_result: MemoryGatewayResult | None = None
        selections: tuple[NeedEvidenceSelection, ...] = ()
        assembly: EvidenceFirstAssemblyResult | None = None
        for index, (tier, context_tokens, ledger_tokens, call_budget) in enumerate(
            MEMORY_CONTEXT_BUDGET_TIERS
        ):
            previous = records[-1] if records else None
            packing_only = (
                previous is not None
                and previous.expansion_reason == "assembler_dropped_mandatory_evidence"
                and previous.stop_reason != ControllerStopReason.BUDGET_EXHAUSTED.value
                and previous.retrieval_call_count < previous.backend_call_budget
            )
            if packing_only and gateway_result is not None:
                assert previous is not None
                request_id = previous.request_id
                reexecuted = False
            else:
                request_id = bounded_stable_id(
                    f"memory-request.{invocation.task.task_id.root}.{tier.value}",
                    (
                        f"memory-request.{invocation.base_commit.root}."
                        f"{invocation.task.target_chapter_start}.{tier.value}"
                    ),
                )
                resolution = MemoryResolutionRequest(
                    request_id=request_id,
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
                    retrieval_budget=RetrievalBudget(
                        max_rounds=3,
                        max_tool_calls=call_budget,
                        max_query_rewrites_per_need=0,
                        token_budget=context_tokens,
                    ),
                    context_budget=ContextBudget(token_budget=context_tokens),
                )
                gateway_result = self._gateway.resolve(
                    resolution,
                    invocation.text,
                    thread_id=f"writer-context.{invocation.task.task_id.root}.{tier.value}",
                )
                selections = self._load_frozen_selections(gateway_result, needs, invocation)
                reexecuted = True
            assert gateway_result is not None
            assembly = self._assembler.assemble(
                task=invocation.task,
                selections=selections,
                text_root=invocation.text,
                basis_commit_id=invocation.base_commit,
                basis_snapshot_id=invocation.snapshot_id,
                writer_token_budget=context_tokens,
                evidence_ledger_token_budget=ledger_tokens,
                advisory_items=advisory_items,
                gateway_context_artifact=gateway_result.frozen_context_artifact,
                frozen_evidence_selections_artifact=(
                    gateway_result.frozen_evidence_selections_artifact
                ),
            )
            expansion = self._expansion_reason(
                gateway_result=gateway_result,
                assembly=assembly,
                call_budget=call_budget,
                selections=selections,
            )
            records.append(
                MemoryContextBudgetTierRecord(
                    tier=tier,
                    request_id=request_id,
                    context_token_budget=context_tokens,
                    evidence_ledger_token_budget=ledger_tokens,
                    backend_call_budget=call_budget,
                    retrieval_call_count=gateway_result.selected_result.retrieval_call_count,
                    expansion_reason=expansion,
                    stop_reason=gateway_result.selected_result.stop_reason.value,
                    mandatory_need_facets_total=(
                        gateway_result.selected_result.mandatory_need_facets_total
                    ),
                    mandatory_need_facets_closed=(
                        gateway_result.selected_result.mandatory_need_facets_closed
                    ),
                    frozen_context_artifact=gateway_result.frozen_context_artifact,
                    frozen_evidence_selections_artifact=(
                        gateway_result.frozen_evidence_selections_artifact
                    ),
                    reexecuted_retrieval=reexecuted,
                )
            )
            if expansion is None:
                break
            if index == len(MEMORY_CONTEXT_BUDGET_TIERS) - 1:
                packing = expansion == "assembler_dropped_mandatory_evidence"
                if packing or assembly.status is not ContextAssemblyStatus.READY:
                    receipt_ref = self._freeze_budget_receipt(
                        invocation=invocation,
                        needs=needs,
                        records=tuple(records),
                        terminal_reason=expansion,
                        budget_review=True,
                    )
                    raise MemoryContextBudgetExhaustedError(
                        "Writer Context remained budget-exhausted after expand_2",
                        receipt=receipt_ref,
                    )
                break
        assert assembly is not None
        assert gateway_result is not None
        receipt_ref = self._freeze_budget_receipt(
            invocation=invocation,
            needs=needs,
            records=tuple(records),
            terminal_reason=records[-1].expansion_reason or records[-1].stop_reason,
            budget_review=False,
        )
        return assembly.model_copy(
            update={
                "package": assembly.package.model_copy(
                    update={
                        "lineage": assembly.package.lineage.model_copy(
                            update={
                                "gateway_context_artifact": gateway_result.frozen_context_artifact,
                                "frozen_evidence_selections_artifact": (
                                    gateway_result.frozen_evidence_selections_artifact
                                ),
                                "budget_expansion_receipt": receipt_ref,
                            }
                        )
                    }
                )
            }
        )

    def _load_frozen_selections(
        self,
        result: MemoryGatewayResult,
        needs: tuple[Stage1MemoryNeed, ...],
        invocation: Stage2MWriterContextInvocation,
    ) -> tuple[NeedEvidenceSelection, ...]:
        if result.frozen_evidence_selections_artifact.media_type != (
            NEED_EVIDENCE_SELECTIONS_MEDIA_TYPE
        ):
            raise ValueError("Memory Gateway selection artifact has an unexpected media type")
        try:
            frozen = FrozenNeedEvidenceSelections.model_validate_json(
                self._artifacts.read_verified(result.frozen_evidence_selections_artifact),
                strict=True,
            )
        except Exception as error:
            raise ValueError("Memory Gateway selection artifact is unreadable") from error
        expected_ids = tuple(need.need_id for need in needs)
        if (
            frozen.request_id != result.request_id
            or frozen.base_commit != invocation.base_commit
            or frozen.snapshot_id != invocation.snapshot_id
            or frozen.need_ids != expected_ids
        ):
            raise ValueError(
                "Memory Gateway selection artifact does not match the Writer request basis"
            )
        if result.selected_result.future_leakage_count:
            raise ValueError("Memory Gateway selection contains future leakage")
        return frozen.selections

    @staticmethod
    def _expansion_reason(
        *,
        gateway_result: MemoryGatewayResult,
        assembly: EvidenceFirstAssemblyResult,
        call_budget: int,
        selections: tuple[NeedEvidenceSelection, ...],
    ) -> str | None:
        dropped_mandatory = any(
            item.gap is not None
            and item.gap.kind is EvidenceGapKind.BUDGET_EXCEEDED
            and item.mandatory
            for item in assembly.package.items
        )
        if dropped_mandatory:
            return "assembler_dropped_mandatory_evidence"
        stop = gateway_result.selected_result.stop_reason
        if stop is ControllerStopReason.BUDGET_EXHAUSTED:
            return "controller_budget_exhausted"
        if gateway_result.selected_result.retrieval_call_count >= call_budget and stop not in {
            ControllerStopReason.SUFFICIENT,
            ControllerStopReason.NO_ADDITIONAL_EVIDENCE,
        }:
            return "retrieval_call_saturation"
        del selections
        return None

    def _freeze_budget_receipt(
        self,
        *,
        invocation: Stage2MWriterContextInvocation,
        needs: tuple[Stage1MemoryNeed, ...],
        records: tuple[MemoryContextBudgetTierRecord, ...],
        terminal_reason: str,
        budget_review: bool,
    ) -> ArtifactRef:
        receipt = MemoryContextBudgetExpansionReceipt(
            request_id=records[-1].request_id,
            base_commit=invocation.base_commit,
            snapshot_id=invocation.snapshot_id,
            need_ids=tuple(need.need_id for need in needs),
            tiers=records,
            final_tier=records[-1].tier,
            terminal_reason=terminal_reason,
            budget_review=budget_review,
        )
        return self._artifacts.put(
            canonical_json_bytes(receipt.model_dump(mode="json")),
            BUDGET_EXPANSION_RECEIPT_MEDIA_TYPE,
            self._schema_version,
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
    "BUDGET_EXPANSION_RECEIPT_MEDIA_TYPE",
    "MEMORY_CONTEXT_BUDGET_TIERS",
    "BoundPermissionResolver",
    "BoundPolicyResolver",
    "ExactSnapshotFreshnessCheck",
    "MemoryContextBudgetExhaustedError",
    "ProductionCuratorModelRequestFactory",
    "ProductionReactiveMemoryInputsFactory",
    "ProductionStage2MWriterContext",
    "ProductionWriterModelRequestFactory",
    "ProposedTextRootLoader",
    "SettlementTextReveal",
    "utf8_quarter_token_count",
]
