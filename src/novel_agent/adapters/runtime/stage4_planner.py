"""Real adapter to the public Stage 4 Planner Context Loop boundary."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.creative_runtime import (
    OPERATOR_PLAN_REVIEW_MEDIA_TYPE,
    RUNTIME_MODEL_REPLAY_EVIDENCE_MEDIA_TYPE,
    CandidateBinding,
    CandidateKind,
    PlanningLoopRequest,
    PlanningLoopResult,
    PlanningTerminalStatus,
    RuntimeModelReplayEvidence,
)
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId, bounded_stable_id
from novel_agent.domain.memory import (
    FacetClosureStatus,
    NeedFacetKind,
    Stage1ContextPackage,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.memory_write import (
    InformationBoundary,
    MemoryGapClassification,
    MemoryRepairFinding,
    MemoryRepairOwner,
    NarrativePosition,
    RepairScope,
    SourceProvenance,
    SourceVisibilityReceipt,
)
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.planning import (
    PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
    PlanningBudgets,
    PlanningLoopCheckpoint,
)
from novel_agent.domain.planning import (
    PlanningLoopRequest as Stage4PlanningLoopRequest,
)
from novel_agent.domain.planning import (
    PlanningLoopResult as Stage4PlanningLoopResult,
)
from novel_agent.domain.planning import (
    PlanningLoopTerminal as Stage4PlanningLoopTerminal,
)
from novel_agent.domain.planning_gap import (
    HISTORICAL_DEPENDENCY_UNRESOLVED,
    DependencyExpectation,
    GapDisposition,
    QuestionPurpose,
    VerifiedGapEvidence,
    classify_gap,
    disposition_diagnostic,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentMode,
    ContractRef,
    PlanningTask,
)
from novel_agent.domain.text import SourceBoundEvidenceRequirement
from novel_agent.domain.world import PlanLevel, TruthClass
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.planning_context_loop import (
    ModelRequestFactory,
    PlanningContextLoopService,
)

PLAN_PROPOSAL_MEDIA_TYPE = "application/vnd.novel-agent.plan-proposal+json"
PLAN_REVIEW_DRAFT_MEDIA_TYPE = "application/vnd.novel-agent.plan-review-draft+json"

# Runtime task inputs are a lineage envelope, not an assertion that every
# referenced artifact is author intent.  In particular, an operator rejection
# carries the original planning sources alongside a candidate, review evidence,
# and a structured revision directive.  Only the directive is a Planner control
# input; audit/rebind and prior-loop artifacts must remain out of the model's
# author source binding.
_REVISION_DIRECTIVE_MEDIA_TYPES = frozenset(
    {
        "application/vnd.novel-agent.author-revision-directive+json",
        "application/vnd.novel-agent.operator-revision-directive+json",
    }
)
_REVISION_REVIEW_MEDIA_TYPES = frozenset({OPERATOR_PLAN_REVIEW_MEDIA_TYPE})
_NON_AUTHOR_PLANNING_MEDIA_TYPES = frozenset(
    {
        *_REVISION_DIRECTIVE_MEDIA_TYPES,
        "application/vnd.novel-agent.runtime-rebind-evidence+json",
        "application/vnd.novel-agent.operator-plan-review+json",
        "application/vnd.novel-agent.stage5-candidate-binding+json",
        PLAN_PROPOSAL_MEDIA_TYPE,
        "application/vnd.novel-agent.planning-inquiry+json",
        "application/vnd.novel-agent.plan-review+json",
        "application/vnd.novel-agent.context-package+json",
        "application/vnd.novel-agent.planner-context-package+json",
        "application/vnd.novel-agent.planning-loop-event+json",
        PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE,
        RUNTIME_MODEL_REPLAY_EVIDENCE_MEDIA_TYPE,
        "application/vnd.novel-agent.stage5-acceptance-receipt+json",
    }
)


#: Truth classes that are a sourced negative rather than silence.  A contested
#: or disproved record is evidence about the proposition, so it must not be
#: reported as "no support found".
_NEGATIVE_TRUTH_CLASSES = frozenset(
    {TruthClass.CONTESTED, TruthClass.DISPROVED, TruthClass.RETCONNED}
)


@dataclass(frozen=True, slots=True)
class _MemoryGapAdmission:
    """What one unresolved mandatory facet set is allowed to produce."""

    findings: tuple[ArtifactRef, ...]
    diagnostic_codes: tuple[str, ...]


def _enum_or_none[EnumT: StrEnum](enum_type: type[EnumT], value: str | None) -> EnumT | None:
    """Decode a stored enum value without letting an unknown one pass silently."""

    if value is None:
        return None
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"unknown {enum_type.__name__} value {value!r}") from error


@dataclass(frozen=True, slots=True)
class Stage4PlanningInvocation:
    request: Stage4PlanningLoopRequest
    model_request: ModelRequestFactory
    world: WorldRootDocument | None = None
    text_root: TextRootDocument | None = None
    resume_checkpoint_ref: ArtifactRef | None = None
    replay_completion_check: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class Stage4InvocationPolicy:
    budgets: PlanningBudgets
    configuration_fingerprint: ArtifactId
    model_fingerprint: ArtifactId
    allowed_skill_ids: tuple[StableId, ...] = ()
    explicit_author_overrides: tuple[str, ...] = ()
    model_role: ModelRole = ModelRole.IMPLEMENTATION
    model_purpose: ModelCallPurpose = ModelCallPurpose.DEVELOPMENT
    model_timeout_seconds: float = 120.0
    model_max_output_tokens: int | None = None


class ProductionStage4InvocationFactory:
    """Project one durable Stage 5 planning task into the public Stage 4 loop."""

    is_fixture = False

    def __init__(
        self,
        *,
        commits: CommitService,
        artifacts: ArtifactRepository,
        policy: Stage4InvocationPolicy,
        model_request_namespace: str | None = None,
    ) -> None:
        self._commits = commits
        self._artifacts = artifacts
        self._policy = policy
        self._model_request_namespace = model_request_namespace

    @staticmethod
    def _mode(request: PlanningLoopRequest) -> AgentMode:
        """Select the Planner mode from the task's trusted plan level.

        REPLAN is an operation purpose, not a tree level.  A V2 REPLAN that
        names its target level must use that level's Planner so the revision
        keeps the level's own payload contract; falling through to the generic
        REPLAN mode for a levelled task would let a chapter revision drop the
        chapter contract.  Only a REPLAN that names no legal level keeps the
        generic mode.
        """

        level_mode = (
            None
            if request.plan_level is None
            else {
                PlanLevel.STORY: AgentMode.STORY,
                PlanLevel.ARC_VOLUME: AgentMode.ARC_VOLUME,
                PlanLevel.CHAPTER_SET: AgentMode.CHAPTER_SET,
                PlanLevel.CHAPTER: AgentMode.CHAPTER,
                PlanLevel.SCENE: AgentMode.SCENE,
            }.get(request.plan_level)
        )
        if request.purpose.value == "replan":
            if request.plan_level is None:
                return AgentMode.REPLAN
            if level_mode is None:  # pragma: no cover - PlanLevel is exhaustive
                raise ValueError("Stage 4 REPLAN names an unsupported plan level")
            return level_mode
        if level_mode is not None:
            return level_mode
        return AgentMode.CHAPTER_SET

    def _allowed_skill_ids(self, mode: AgentMode) -> tuple[StableId, ...]:
        from novel_agent.agents.planner import planner_skill_ids_for_mode

        mode_ids = planner_skill_ids_for_mode(mode)
        policy_ids = set(self._policy.allowed_skill_ids)
        if not policy_ids:
            # Legacy fixture policies omit the deployment allowlist.  The
            # real production policy is populated from production_assembly_spec
            # and therefore takes the strict branch below.
            return mode_ids
        allowed = tuple(item for item in mode_ids if item in policy_ids)
        required = {
            StableId("skill.planning-inquiry"),
            StableId(f"skill.planner.{mode.value}"),
        }
        if not required.issubset(allowed):
            raise ValueError(
                f"Stage 4 policy allowlist is missing required skills for planner mode {mode.value}"
            )
        return allowed

    def __call__(self, request: PlanningLoopRequest) -> Stage4PlanningInvocation:
        if request.basis_snapshot is None:
            raise ValueError("production Stage 4 invocation requires an exact snapshot")
        mode = self._mode(request)
        if mode is AgentMode.CHAPTER_SET and (
            request.horizon_start is None or request.horizon_end is None
        ):
            raise ValueError("production Stage 4 invocation requires a rolling horizon")
        if mode in {AgentMode.STORY, AgentMode.ARC_VOLUME} and (
            request.horizon_start is not None or request.horizon_end is not None
        ):
            raise ValueError("STORY/ARC_VOLUME production tasks cannot use rolling horizon")
        if self._commits.current_commit(request.project_id) != request.basis_commit:
            raise ValueError("Stage 4 task basis is not the current project commit")
        manifest = self._commits.load_manifest(request.basis_commit)
        if manifest.project_id != request.project_id:
            raise ValueError("Stage 4 task and canonical manifest belong to different projects")
        revision_artifacts = tuple(
            ref
            for ref in request.input_artifact_refs
            if ref.media_type in _REVISION_DIRECTIVE_MEDIA_TYPES
        )
        revision_parent_refs = tuple(
            ref for ref in request.input_artifact_refs if ref.media_type == PLAN_PROPOSAL_MEDIA_TYPE
        )
        revision_review_refs = tuple(
            ref
            for ref in request.input_artifact_refs
            if ref.media_type in _REVISION_REVIEW_MEDIA_TYPES
        )
        if len(revision_parent_refs) > 1:
            raise ValueError("Stage 4 revision task may bind only one parent Plan proposal")
        if len(revision_review_refs) > 1:
            raise ValueError("Stage 4 revision task may bind only one host revision review")
        author_intent = tuple(
            ref
            for ref in request.input_artifact_refs
            if ref.media_type not in _NON_AUTHOR_PLANNING_MEDIA_TYPES
        )
        text = TextRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.text_root), strict=True
        )
        world = WorldRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.world_root), strict=True
        )
        # Canon bytes may carry the producing-commit label from import; the
        # planning view is the WorldRoot currently bound at this task basis.
        if world.source_commit != request.basis_commit:
            world = world.model_copy(update={"source_commit": request.basis_commit})
        latest = text.chapters[-1].chapter_index if text.chapters else 0
        if latest != request.chapter_index:
            raise ValueError(
                f"Stage 4 task starts at chapter {request.chapter_index}, "
                f"but TextRoot ends at {latest}"
            )
        if (
            mode is AgentMode.CHAPTER_SET
            and request.horizon_start is not None
            and request.horizon_start <= request.chapter_index
        ):
            raise ValueError("Stage 4 horizon must begin after the committed chapter")
        source_ids = tuple(
            StableId(f"source.author-intent.{ref.artifact_id.root[-24:]}") for ref in author_intent
        )
        retrieval = self._policy.budgets.retrieval
        tranche_count = request.planner_memory_budget_extensions + 1
        effective_budgets = self._policy.budgets.model_copy(
            update={
                "retrieval": retrieval.model_copy(
                    update={
                        "max_rounds": retrieval.max_rounds * tranche_count,
                        "max_tool_calls": retrieval.max_tool_calls * tranche_count,
                        "max_anchor_expansions": (retrieval.max_anchor_expansions * tranche_count),
                        "max_full_chapter_reads": (
                            retrieval.max_full_chapter_reads * tranche_count
                        ),
                        "wall_clock_budget_ms": (retrieval.wall_clock_budget_ms * tranche_count),
                        "token_budget": retrieval.token_budget * tranche_count,
                    }
                )
            }
        )
        planning_task = PlanningTask(
            planning_task_id=bounded_stable_id(
                f"planning-task.{request.task_id.root}",
                f"planning-task.{request.run_id.root}",
                f"planning-task.{request.basis_commit.root}",
            ),
            project_id=request.project_id,
            mode=mode,
            base_commit=request.basis_commit,
            source_ids=source_ids,
            creative_scope=(
                f"chapters:{request.horizon_start}-{request.horizon_end}"
                if request.horizon_start is not None and request.horizon_end is not None
                else f"level:{mode.value}",
                f"purpose:{request.purpose.value}",
            ),
        )
        detailed = Stage4PlanningLoopRequest(
            request_id=bounded_stable_id(
                f"planning-request.{request.task_id.root}",
                f"planning-request.{request.run_id.root}",
                f"planning-request.{request.basis_commit.root}",
            ),
            run_id=request.run_id,
            task_id=request.task_id,
            project_id=request.project_id,
            task=planning_task,
            author_intent_artifacts=author_intent,
            revision_artifact_refs=(
                *revision_artifacts,
                *(
                    ref
                    for ref in request.continuation_artifact_refs
                    if ref.media_type == PLAN_REVIEW_DRAFT_MEDIA_TYPE
                ),
            ),
            revision_parent_proposal_ref=(
                revision_parent_refs[0] if revision_parent_refs else None
            ),
            revision_review_artifact_refs=revision_review_refs,
            accepted_plan_ref=manifest.plan_root,
            accepted_world_ref=manifest.world_root,
            accepted_text_ref=manifest.text_root,
            project_profile_ref=manifest.project_profile_root,
            snapshot_id=request.basis_snapshot,
            explicit_author_overrides=self._policy.explicit_author_overrides,
            horizon_start=request.horizon_start,
            horizon_end=request.horizon_end,
            allowed_skill_ids=self._allowed_skill_ids(mode),
            budgets=effective_budgets,
            configuration_fingerprint=self._policy.configuration_fingerprint,
            model_fingerprint=self._policy.model_fingerprint,
        )
        resume_checkpoint_ref = next(
            (
                ref
                for ref in reversed(request.continuation_artifact_refs)
                if ref.media_type == PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE
            ),
            None,
        )
        replay_evidence_refs = tuple(
            ref
            for ref in request.continuation_artifact_refs
            if ref.media_type == RUNTIME_MODEL_REPLAY_EVIDENCE_MEDIA_TYPE
        )
        if len(replay_evidence_refs) > 1:
            raise ValueError("Stage 4 recovery may bind only one model replay evidence artifact")
        replay_responses = []
        if replay_evidence_refs:
            replay_evidence = RuntimeModelReplayEvidence.model_validate_json(
                self._artifacts.read_verified(replay_evidence_refs[0]),
                strict=True,
            )
            if (
                replay_evidence.run_id != request.run_id
                or replay_evidence.task_id != request.task_id
            ):
                raise ValueError("Stage 4 model replay evidence is not task-bound")
            replay_responses = list(replay_evidence.responses)

        def model_request(phase: str, mode: AgentMode, attempt: int) -> ModelRequest:
            suffix = f"{phase}.{attempt}"
            request_namespace = self._model_request_namespace
            if request_namespace is None and request.attempt_id is not None:
                # A retried runtime task must reserve a fresh provider request
                # identity.  Keep the default namespace short and derived from
                # the durable Attempt so long task ids cannot force the
                # bounded fallback to collide across retries.
                request_namespace = (
                    "attempt-"
                    + hashlib.sha256(request.attempt_id.root.encode("utf-8")).hexdigest()[:16]
                )
            request_prefix = f"model-request.{request.task_id.root}"
            if request_namespace is not None:
                request_prefix += f".{request_namespace}"
            candidates = [f"{request_prefix}.{suffix}"]
            if request_namespace is not None:
                candidates.extend(
                    (
                        f"model-request.{request_namespace}.{suffix}",
                        f"model-request.{request.run_id.root}.{request_namespace}.{suffix}",
                    )
                )
            candidates.extend(
                (
                    f"model-request.{request.task_id.root}.{suffix}",
                    f"model-request.{request.run_id.root}.{suffix}",
                )
            )
            candidate = ModelRequest(
                request_id=bounded_stable_id(
                    *candidates,
                ),
                run_id=request.run_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                model_role=self._policy.model_role,
                purpose=self._policy.model_purpose,
                trace_id=f"trace.{request.run_id.root}.{request.task_id.root}",
                prompt="",
                agent_mode=mode.value,
                scheduling_stage=phase,
                max_output_tokens=self._policy.model_max_output_tokens,
                timeout_seconds=self._policy.model_timeout_seconds,
                enable_thinking=False,
            )
            if not replay_responses:
                return candidate
            matching_index = next(
                (
                    index
                    for index, response in enumerate(replay_responses)
                    if response.logical_phase == phase
                ),
                None,
            )
            if matching_index is None:
                raise ValueError(
                    f"model replay evidence has no response for Stage 4 logical phase {phase}"
                )
            response = replay_responses.pop(matching_index)
            return candidate.model_copy(
                update={
                    "request_id": response.request_id,
                    "attempt_id": response.source_attempt_id,
                }
            )

        def replay_completion_check() -> None:
            if replay_responses:
                remaining = ", ".join(
                    f"{response.logical_phase}:{response.request_id.root}"
                    for response in replay_responses
                )
                raise ValueError(
                    "Stage 4 replay completed with unconsumed response(s): " + remaining
                )

        return Stage4PlanningInvocation(
            request=detailed,
            model_request=model_request,
            world=world,
            text_root=text,
            resume_checkpoint_ref=resume_checkpoint_ref,
            replay_completion_check=(replay_completion_check if replay_responses else None),
        )


class Stage4PlanningLeafAdapter:
    """Map Stage 5 task identity to one complete, candidate-only Stage 4 loop."""

    is_fixture = False

    def __init__(
        self,
        loop: PlanningContextLoopService,
        artifacts: ArtifactRepository,
        invocation_factory: Callable[[PlanningLoopRequest], Stage4PlanningInvocation],
        *,
        schema_version: SchemaVersion,
    ) -> None:
        self._loop = loop
        self._artifacts = artifacts
        self._invocation_factory = invocation_factory
        self._schema_version = schema_version

    @property
    def invocation_factory(
        self,
    ) -> Callable[[PlanningLoopRequest], Stage4PlanningInvocation]:
        return self._invocation_factory

    async def run(self, request: PlanningLoopRequest) -> PlanningLoopResult:
        invocation = self._invocation_factory(request)
        detailed = invocation.request
        if (
            detailed.run_id != request.run_id
            or detailed.task_id != request.task_id
            or detailed.project_id != request.project_id
            or detailed.task.base_commit != request.basis_commit
            or detailed.snapshot_id != request.basis_snapshot
        ):
            raise ValueError("Stage 4 request factory violated the durable task basis")
        bound_inputs = set(request.input_artifact_refs)
        bound_continuation = set(request.continuation_artifact_refs)
        if not set(detailed.author_intent_artifacts).issubset(bound_inputs):
            raise ValueError("Stage 4 request introduced an unbound author-intent artifact")
        if not set(detailed.revision_artifact_refs).issubset(bound_inputs | bound_continuation):
            raise ValueError("Stage 4 request introduced an unbound revision artifact")
        if (
            detailed.revision_parent_proposal_ref is not None
            and detailed.revision_parent_proposal_ref not in bound_inputs
        ):
            raise ValueError("Stage 4 request introduced an unbound revision parent")
        if not set(detailed.revision_review_artifact_refs).issubset(bound_inputs):
            raise ValueError("Stage 4 request introduced an unbound revision review")
        if (
            detailed.task.mode in {AgentMode.STORY, AgentMode.ARC_VOLUME, AgentMode.CHAPTER_SET}
            and request.input_artifact_refs
            and not detailed.author_intent_artifacts
        ):
            raise ValueError(
                f"Stage 4 {detailed.task.mode.value} requires visible author-intent artifacts"
            )

        result = await self._loop.run(
            request=detailed,
            model_request=invocation.model_request,
            world=invocation.world,
            text_root=invocation.text_root,
            resume_checkpoint_ref=invocation.resume_checkpoint_ref,
        )
        if result.request_id != detailed.request_id:
            raise RuntimeError("Stage 4 Planner returned cross-request lineage")
        # An escalated review ("human_required") is still a settled proposal: the
        # author has to rule on it, and that ruling needs the same immutable candidate
        # binding a ready proposal gets.  Without it the only front the author could
        # touch was the planning task, which no author command can resolve.
        # An inquiry review or a reviewer-memory review may escalate to a human
        # before any proposal exists.  That is still a wait on the planning front:
        # there is nothing to accept, so it must not assert a proposal into being.
        candidate_terminals = (
            {Stage4PlanningLoopTerminal.PLAN_CANDIDATE_READY}
            if result.proposal is None
            else {
                Stage4PlanningLoopTerminal.PLAN_CANDIDATE_READY,
                Stage4PlanningLoopTerminal.HUMAN_REQUIRED,
            }
        )
        if result.terminal in candidate_terminals:
            if invocation.replay_completion_check is not None:
                invocation.replay_completion_check()
            assert result.proposal is not None
            proposal_ref = self._artifacts.put(
                canonical_json_bytes(result.proposal.model_dump(mode="json")),
                PLAN_PROPOSAL_MEDIA_TYPE,
                self._schema_version,
            )
            lineage = self._lineage(result, proposal_ref)
            candidate = CandidateBinding(
                candidate_id=bounded_stable_id(
                    f"plan-candidate.{result.proposal.proposal_id.root}",
                    f"plan-candidate.{proposal_ref.artifact_id.root}",
                    f"plan-candidate.{request.task_id.root}",
                ),
                kind=CandidateKind.PLAN,
                artifact_ref=proposal_ref,
                candidate_hash=proposal_ref.artifact_id.root,
                basis_commit=request.basis_commit,
                basis_snapshot=request.basis_snapshot,
                lineage_artifact_refs=lineage,
            )
            escalated = result.terminal is Stage4PlanningLoopTerminal.HUMAN_REQUIRED
            return PlanningLoopResult(
                result_id=bounded_stable_id(
                    f"{request.task_id.root}.planner-result",
                    request.task_id.root,
                ),
                run_id=request.run_id,
                task_id=request.task_id,
                status=(
                    PlanningTerminalStatus.WAITING_INPUT
                    if escalated
                    else PlanningTerminalStatus.PLAN_CANDIDATE_READY
                ),
                candidate=candidate,
                artifact_refs=lineage,
                failure_code="PLAN_REVIEW_HUMAN_REQUIRED" if escalated else None,
                failure_detail=(
                    "independent review escalated to the author" if escalated else None
                ),
            )

        status = self._terminal(result.terminal)
        diagnostic = (
            result.diagnostic_codes[0] if result.diagnostic_codes else result.terminal.value
        )
        artifact_refs = result.event_artifacts
        gap_diagnostics = list(result.diagnostic_codes)
        if diagnostic == "PLANNER_MEMORY_FACETS_UNRESOLVED" and request.attempt_id is not None:
            admission = self._memory_gap_admission(
                request,
                detailed,
                result,
                attempt_id=request.attempt_id,
            )
            if admission.findings:
                artifact_refs = tuple(dict.fromkeys((*artifact_refs, *admission.findings)))
            # The disposition of a non-closing question is a first-class result,
            # not a log line.  A planner terminal that found no trusted support
            # reports an unresolved historical dependency instead of pretending
            # the canon omitted a fact.
            for code in admission.diagnostic_codes:
                if code not in gap_diagnostics:
                    gap_diagnostics.append(code)
            if not admission.findings:
                diagnostic = (
                    admission.diagnostic_codes[0] if admission.diagnostic_codes else diagnostic
                )
        return PlanningLoopResult(
            result_id=bounded_stable_id(
                f"{request.task_id.root}.planner-result",
                request.task_id.root,
            ),
            run_id=request.run_id,
            task_id=request.task_id,
            status=status,
            artifact_refs=artifact_refs,
            failure_code=diagnostic[:128],
            failure_detail=(
                f"Stage 4 terminal: {result.terminal.value}; " + "; ".join(gap_diagnostics)
            )[:512],
        )

    def _memory_gap_admission(
        self,
        request: PlanningLoopRequest,
        detailed: Stage4PlanningLoopRequest,
        result: Stage4PlanningLoopResult,
        *,
        attempt_id: StableId,
    ) -> _MemoryGapAdmission:
        """Decide what one unresolved mandatory facet set actually means.

        A Planner terminal alone proves nothing about the canon.  This method
        reconstructs the host-verified evidence state, classifies the reviewed
        question with :func:`classify_gap`, and materializes a Canon extraction
        handoff only for the one disposition that a repair can act on.  Every
        other disposition becomes a typed diagnostic that the planning loop
        owns, so an unresolved dependency can never masquerade as an omission.
        """

        if (
            detailed.accepted_text_ref is None
            or result.memory_context_ref is None
            or not detailed.author_intent_artifacts
        ):
            return _MemoryGapAdmission(
                findings=(),
                diagnostic_codes=(HISTORICAL_DEPENDENCY_UNRESOLVED,),
            )
        checkpoint_ref = next(
            (
                ref
                for ref in reversed(result.event_artifacts)
                if ref.media_type == PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE
            ),
            None,
        )
        if checkpoint_ref is None:
            return _MemoryGapAdmission(
                findings=(),
                diagnostic_codes=(HISTORICAL_DEPENDENCY_UNRESOLVED,),
            )
        problem_identity_seed = None
        try:
            checkpoint = PlanningLoopCheckpoint.model_validate_json(
                self._artifacts.read_verified(checkpoint_ref), strict=False
            )
            problem_identity_seed = checkpoint.problem_identity_seed
        except (ValueError, RuntimeError):
            # Legacy checkpoints predate the seed field.  They retain the
            # historical finding semantics; seeded runs fail closed below if
            # the typed checkpoint cannot be decoded.
            problem_identity_seed = None
        try:
            context = Stage1ContextPackage.model_validate_json(
                self._artifacts.read_verified(result.memory_context_ref), strict=False
            )
        except (ValueError, RuntimeError):
            return _MemoryGapAdmission(
                findings=(),
                diagnostic_codes=(HISTORICAL_DEPENDENCY_UNRESOLVED,),
            )
        trace = next(
            (
                item
                for item in context.retrieval_traces
                if any(
                    receipt.mandatory and receipt.status is not FacetClosureStatus.SUPPORTED
                    for receipt in item.facet_receipts
                )
                and item.candidates
            ),
            None,
        )
        if trace is None:
            return _MemoryGapAdmission(
                findings=(),
                diagnostic_codes=(HISTORICAL_DEPENDENCY_UNRESOLVED,),
            )
        unresolved_facets = tuple(
            receipt.need_facet_id
            for receipt in trace.facet_receipts
            if receipt.mandatory and receipt.status is not FacetClosureStatus.SUPPORTED
        )
        if not unresolved_facets:
            return _MemoryGapAdmission(
                findings=(),
                diagnostic_codes=(HISTORICAL_DEPENDENCY_UNRESOLVED,),
            )
        cutoff = NarrativePosition(chapter_index=request.chapter_index)
        source_evidence_requirement = (
            None
            if problem_identity_seed is None
            else problem_identity_seed.source_evidence_requirement
        )
        disposition = self._classify_memory_gap(
            request,
            trace,
            unresolved_facets=unresolved_facets,
            source_evidence_requirement=source_evidence_requirement,
        )
        if disposition is not GapDisposition.CANON_EXTRACTION_GAP:
            return _MemoryGapAdmission(
                findings=(),
                diagnostic_codes=(disposition_diagnostic(disposition),),
            )
        boundary = InformationBoundary(
            boundary_id=bounded_stable_id(
                f"boundary.memory-gap.{request.task_id.root}",
                f"boundary.memory-gap.{request.run_id.root}",
                f"boundary.memory-gap.{request.basis_commit.root}",
            ),
            base_commit=request.basis_commit,
            maximum_visible_position=cutoff,
            evaluator_sources_forbidden=True,
            policy_ref=ContractRef(
                contract_id=StableId("policy.stage5.memory-gap-boundary"),
                version=self._schema_version,
                content_hash=detailed.configuration_fingerprint,
            ),
        )
        visibility = SourceVisibilityReceipt(
            receipt_id=bounded_stable_id(
                f"visibility.memory-gap.{request.task_id.root}.{attempt_id.root}",
                f"visibility.memory-gap.{request.run_id.root}.{attempt_id.root}",
                f"visibility.memory-gap.{request.basis_commit.root}.{attempt_id.root}",
            ),
            source_artifact=detailed.accepted_text_ref,
            boundary_id=boundary.boundary_id,
            visible_through=cutoff,
            access_scope=AccessScope.WRITER_SAFE,
            provenance=SourceProvenance.CANONICAL_ROOT,
            issuer=StableId("issuer.stage5.planner-gap"),
            receipt_hash=ArtifactId("sha256:" + "0" * 64),
        )
        receipt_payload = visibility.model_dump(mode="json")
        receipt_payload["receipt_hash"] = None
        visibility = visibility.model_copy(update={"receipt_hash": content_id(receipt_payload)})
        visibility_ref = self._artifacts.put(
            canonical_json_bytes(visibility.model_dump(mode="json")),
            "application/vnd.novel-agent.source-visibility-receipt+json",
            self._schema_version,
        )
        if problem_identity_seed is not None and problem_identity_seed.need_id != trace.need_id:
            return _MemoryGapAdmission(
                findings=(),
                diagnostic_codes=(HISTORICAL_DEPENDENCY_UNRESOLVED,),
            )
        compiled = trace.compiled_query_bundle
        target_query = self._repair_target_query(compiled)
        if not target_query:
            lexical = compiled.get("lexical_queries")
            if isinstance(lexical, list | tuple):
                target_query = next(
                    (str(item).strip() for item in lexical if str(item).strip()), ""
                )
        if not target_query:
            target_query = f"Planner Memory need {trace.need_id.root}"
        semantic_question = target_query
        need_query = target_query
        if problem_identity_seed is not None:
            need_query = problem_identity_seed.need_query
            semantic_question = problem_identity_seed.semantic_question
        identity = self._attempt_problem_identity(
            request,
            attempt_id=attempt_id,
            trace_need_id=trace.need_id,
            unresolved_facets=unresolved_facets,
        )
        stable_identity = self._stable_problem_identity(
            request,
            semantic_question=semantic_question,
            trace_need_id=trace.need_id,
            unresolved_facets=unresolved_facets,
            source_evidence_digest=self._source_evidence_digest(
                detailed,
                unresolved_facets=unresolved_facets,
                source_evidence_requirement=source_evidence_requirement,
            ),
        )
        # A mixed problem is split into one finding per owner so a
        # relation-only graph profile never receives event/state facets it
        # cannot represent.  Both children share the parent problem key.
        findings: list[ArtifactRef] = []
        for owner, owner_facets in self._facet_groups_by_owner(trace, unresolved_facets):
            finding = MemoryRepairFinding(
                finding_id=StableId(f"memory-gap.{identity}.{owner.value}"),
                incident_id=StableId(f"incident.memory-gap.{identity}"),
                planner_run_id=request.run_id,
                planner_task_id=request.task_id,
                planner_attempt_id=attempt_id,
                planner_request_id=detailed.request_id,
                planner_intent_ref=detailed.author_intent_artifacts[0],
                planner_checkpoint_ref=checkpoint_ref,
                project_id=request.project_id,
                base_commit=request.basis_commit,
                basis_snapshot_id=request.basis_snapshot,
                projection_snapshot_id=request.basis_snapshot,
                information_boundary=boundary,
                cutoff=cutoff,
                access_scope=AccessScope.WRITER_SAFE,
                source_artifact_refs=(detailed.accepted_text_ref,),
                source_visibility_receipt_refs=(visibility_ref,),
                source_chapter_indices=self._source_chapter_indices(
                    trace,
                    cutoff.chapter_index,
                    required_chapter=(
                        None
                        if source_evidence_requirement is None
                        else source_evidence_requirement.source_chapter_index
                    ),
                ),
                source_evidence_requirement=source_evidence_requirement,
                need_id=trace.need_id,
                need_query=need_query[:2048],
                semantic_question=semantic_question[:2048],
                entity_ids=tuple(
                    dict.fromkeys(
                        entity_id
                        for candidate in trace.candidates
                        for entity_id in candidate.unit.entity_ids
                    )
                ),
                mandatory_facet_ids=owner_facets,
                graph_receipt_refs=(),
                l0_receipt_refs=(),
                semantic_judge_receipt_refs=trace.semantic_receipt_refs,
                classification=MemoryGapClassification.CANON_EXTRACTION_GAP,
                repair_owner=owner,
                target_root_kind=RootKind.WORLD,
                repair_scope=RepairScope(
                    field_paths=(
                        ("world.entities", "world.relations")
                        if owner is MemoryRepairOwner.GRAPH_CURATOR
                        else (
                            "world.entities",
                            "world.events",
                            "world.states",
                            "world.obligations",
                        )
                    )
                ),
                no_progress_key=stable_identity,
                attempt_problem_key=StableId(f"memory-problem-attempt.{identity}"),
                owned_facet_ids=owner_facets,
            )
            findings.append(
                self._artifacts.put(
                    canonical_json_bytes(finding.model_dump(mode="json")),
                    "application/vnd.novel-agent.memory-repair-finding+json",
                    self._schema_version,
                )
            )
        return _MemoryGapAdmission(findings=tuple(findings), diagnostic_codes=())

    @staticmethod
    def _memory_gap_owner(unresolved_kinds: set[NeedFacetKind]) -> MemoryRepairOwner:
        """Select the sole Curator profile for the unresolved facet set."""

        return (
            MemoryRepairOwner.GRAPH_CURATOR
            if NeedFacetKind.RELATION_STATE in unresolved_kinds
            else MemoryRepairOwner.ORDINARY_CURATOR
        )

    @staticmethod
    def _facet_groups_by_owner(
        trace: object,
        unresolved_facets: tuple[StableId, ...],
    ) -> tuple[tuple[MemoryRepairOwner, tuple[StableId, ...]], ...]:
        """Split unresolved facets into the owners that can actually repair them.

        A Graph Curator profile can represent relation records only.  When one
        reviewed question needs both a relation and an event/state record, the
        single-owner shortcut would hand the unrepresentable half to a profile
        that deterministically drops it.  Each group keeps the same parent
        problem and its own facet subset instead.
        """

        relation_facets: list[StableId] = []
        other_facets: list[StableId] = []
        for receipt in getattr(trace, "facet_receipts", ()):
            if receipt.need_facet_id not in unresolved_facets:
                continue
            destination = (
                relation_facets
                if receipt.facet_kind is NeedFacetKind.RELATION_STATE
                else other_facets
            )
            destination.append(receipt.need_facet_id)
        groups: list[tuple[MemoryRepairOwner, tuple[StableId, ...]]] = []
        if relation_facets:
            groups.append((MemoryRepairOwner.GRAPH_CURATOR, tuple(relation_facets)))
        if other_facets:
            groups.append((MemoryRepairOwner.ORDINARY_CURATOR, tuple(other_facets)))
        if not groups:
            # Legacy receipts may carry no facet rows at all.  Preserve the
            # historical single-owner handoff for exactly that shape.
            groups.append((MemoryRepairOwner.ORDINARY_CURATOR, unresolved_facets))
        return tuple(groups)

    def _classify_memory_gap(
        self,
        request: PlanningLoopRequest,
        trace: object,
        *,
        unresolved_facets: tuple[StableId, ...],
        source_evidence_requirement: SourceBoundEvidenceRequirement | None,
    ) -> GapDisposition:
        """Return the host-verified disposition of one unresolved facet set.

        The semantics come from the Need the trace was produced for, so a
        retried or resumed run reaches the same disposition without re-asking a
        model.  A trace predating those fields keeps the fail-closed historical
        reading rather than claiming a future design it never declared.
        """

        purpose = _enum_or_none(QuestionPurpose, getattr(trace, "question_purpose", None))
        expectation = _enum_or_none(
            DependencyExpectation, getattr(trace, "dependency_expectation", None)
        )
        if purpose is None:
            purpose = QuestionPurpose.VERIFY_HISTORY
        if expectation is None:
            expectation = DependencyExpectation.CHECK_STATUS
        evidence = self._verified_gap_evidence(
            request,
            trace,
            unresolved_facets=unresolved_facets,
            source_evidence_requirement=source_evidence_requirement,
        )
        return classify_gap(
            purpose=purpose,
            dependency_expectation=expectation,
            evidence=evidence,
        )

    def _verified_gap_evidence(
        self,
        request: PlanningLoopRequest,
        trace: object,
        *,
        unresolved_facets: tuple[StableId, ...],
        source_evidence_requirement: SourceBoundEvidenceRequirement | None,
    ) -> VerifiedGapEvidence:
        """Read the frozen retrieval trace into host-verified gap evidence.

        Positive source support requires exact, cutoff-safe, canon-authored
        evidence *for the requested proposition*.  A retrieval permission, a
        paragraph that happens to mention a participant, or a plan-authored
        unit is not support, and the absence of support is never a negative
        answer.

        In particular, holding an ``EvidenceRef`` is not proposition support.
        The reported G3 deadlock came from exactly that shortcut: a selected
        unit about the protagonist carried a real evidence reference, so the
        gap was handed to the Curator as a Canon omission even though no source
        stated the requested appointment document.  The Curator then had
        nothing to extract, returned ``noop``, and the run looped.
        """

        cutoff = request.chapter_index
        positive_source = False
        explicit_negative = False
        for candidate in getattr(trace, "candidates", ()):
            if not getattr(candidate, "selected", False):
                continue
            unit = candidate.unit
            truth_class = getattr(unit, "truth_class", None)
            if truth_class is not None and truth_class is not TruthClass.ACCEPTED_WORLD_FACT:
                # Assertions, rumors, predictions and hypotheticals can never
                # support a world fact, and a contested or disproved record is
                # a sourced negative rather than silence.
                if truth_class in _NEGATIVE_TRUTH_CLASSES:
                    explicit_negative = True
                continue
            if not self._unit_is_cutoff_safe(unit, cutoff):
                continue
            if self._unit_witnesses_proposition(
                unit,
                trace,
                unresolved_facets=unresolved_facets,
                source_evidence_requirement=source_evidence_requirement,
            ):
                positive_source = True
                break
        return VerifiedGapEvidence(
            # The projection is exact whenever this trace was produced from the
            # task's frozen snapshot; a stale projection never reaches a
            # Planner terminal, it fails the freshness gate earlier.
            projection_exact=True,
            positive_source_support=positive_source,
            positive_projection_support=False,
            explicit_negative_support=explicit_negative,
            # Only a named predicate makes the frozen projection the right place
            # to look for this fact.  A bare mention of a participant does not,
            # so an unqualified hit can never be called a missing projection.
            projection_expected=positive_source,
        )

    def _unit_witnesses_proposition(
        self,
        unit: object,
        trace: object,
        *,
        unresolved_facets: tuple[StableId, ...],
        source_evidence_requirement: SourceBoundEvidenceRequirement | None,
    ) -> bool:
        """Return whether one unit actually states the requested proposition.

        Only two things qualify, and both are proposition-level:

        * a pre-registered :class:`SourceBoundEvidenceRequirement` whose exact
          artifact, span and consequence markers the unit's text covers; or
        * an exact state/relation witness in the frozen projection whose
          predicate the reviewed question explicitly names.

        A unit that merely carries an ``EvidenceRef``, or that mentions one of
        the question's entities while stating something else, does not qualify.
        That distinction is what keeps "not found" from being reported as "the
        canon omitted it".
        """

        if self._unit_meets_source_evidence_requirement(unit, source_evidence_requirement):
            return True
        if source_evidence_requirement is not None:
            # A pre-registered requirement is authoritative: when it is present,
            # nothing weaker may substitute for it.
            return False
        unit_predicate = str(getattr(unit, "predicate", "") or "").strip().casefold()
        if not unit_predicate:
            # A unit that names no predicate cannot witness a named fact.
            return False
        named_predicates = self._named_proposition_predicates(unit, trace)
        if not named_predicates:
            return False
        return (
            bool(
                set(getattr(unit, "entity_ids", ()) or ())
                & self._question_entity_ids(trace, unresolved_facets=unresolved_facets)
            )
            and unit_predicate in named_predicates
        )

    @staticmethod
    def _question_entity_ids(
        trace: object,
        *,
        unresolved_facets: tuple[StableId, ...],
    ) -> set[StableId]:
        """Return the grounded entity ids the reviewed question is about.

        The set travels on the frozen trace, because the reviewed question's
        grounded entities and a candidate's entities are different things: using
        the candidate's own entities would let any candidate witness its own
        proposition.  A trace predating the field falls back to the selected
        candidates' entities, which preserves legacy admission behaviour.
        """

        del unresolved_facets
        declared = tuple(getattr(trace, "question_entity_ids", ()) or ())
        if declared:
            return set(declared)
        return {
            entity_id
            for candidate in getattr(trace, "candidates", ())
            if getattr(candidate, "selected", False)
            for entity_id in getattr(candidate.unit, "entity_ids", ()) or ()
        }

    @staticmethod
    def _named_proposition_predicates(unit: object, trace: object) -> set[str]:
        """Return the predicates the reviewed question explicitly names.

        The compiled query bundle and the facet's declared predicate bindings
        are both host-owned: the admission routine never invents a predicate
        from the unit it is trying to judge.
        """

        bundle = getattr(trace, "compiled_query_bundle", {})
        query_texts: list[str] = []
        if isinstance(bundle, Mapping):
            for key in ("semantic_query", "lexical_queries"):
                value = bundle.get(key)
                if isinstance(value, str):
                    query_texts.append(value)
                elif isinstance(value, (list, tuple)):
                    query_texts.extend(str(item) for item in value)
        raw_predicates = bundle.get("predicates") if isinstance(bundle, Mapping) else None
        declared: list[str] = []
        if isinstance(raw_predicates, (list, tuple)):
            declared.extend(str(item) for item in raw_predicates if str(item).strip())
        unit_predicate = str(getattr(unit, "predicate", "") or "").strip().casefold()
        if not unit_predicate:
            return set()
        mentioned = any(
            Stage4PlanningLeafAdapter._token_is_named(unit_predicate, query)
            for query in query_texts
        )
        return (
            {unit_predicate}
            if mentioned or unit_predicate in {item.casefold() for item in declared}
            else set()
        )

    @staticmethod
    def _token_is_named(predicate: str, query: str) -> bool:
        """Match one predicate in a question, token-aware for identifier names."""

        normalized = predicate.strip().casefold()
        if not normalized:
            return False
        folded = query.casefold()
        if re.fullmatch(r"[a-z0-9_]+", normalized):
            return (
                re.search(
                    rf"(?<![a-z0-9_]){re.escape(normalized)}(?![a-z0-9_])",
                    folded,
                )
                is not None
            )
        return normalized in folded

    @staticmethod
    def _unit_is_cutoff_safe(unit: object, cutoff: int) -> bool:
        start = getattr(unit, "narrative_start", None)
        if isinstance(start, int) and start > cutoff:
            return False
        for evidence in getattr(unit, "evidence_refs", ()):
            chapter_id = getattr(evidence, "chapter_id", None)
            if chapter_id is None:
                continue
            match = re.search(r"\.(\d+)$", chapter_id.root)
            if match and int(match.group(1)) > cutoff:
                return False
        return True

    @staticmethod
    def _unit_meets_source_evidence_requirement(
        unit: object,
        requirement: SourceBoundEvidenceRequirement | None,
    ) -> bool:
        """Return whether one unit carries the exact required source span.

        A pre-registered requirement is the strongest available proof that the
        frozen source states the target proposition: it names the artifact,
        chapter, span and consequence markers.  A unit only satisfies it when
        the immutable text really contains every marker.
        """

        if requirement is None:
            return False
        if requirement.source_artifact_id not in set(getattr(unit, "source_refs", ())):
            source_artifact = getattr(unit, "source_artifact", None)
            if source_artifact != requirement.source_artifact_id:
                return False
        text = getattr(unit, "text", "")
        if not isinstance(text, str):
            return False
        return all(marker in text for marker in requirement.required_consequence_markers)

    @staticmethod
    def _attempt_problem_identity(
        request: PlanningLoopRequest,
        *,
        attempt_id: StableId,
        trace_need_id: StableId,
        unresolved_facets: tuple[StableId, ...],
    ) -> str:
        """Return the attempt-scoped identity of one reported problem."""

        return content_id(
            {
                "request": request.task_id.root,
                "attempt": attempt_id.root,
                "need": trace_need_id.root,
                "facets": tuple(item.root for item in unresolved_facets),
            }
        ).root.removeprefix("sha256:")[:32]

    @staticmethod
    def _stable_problem_identity(
        request: PlanningLoopRequest,
        *,
        semantic_question: str,
        trace_need_id: StableId,
        unresolved_facets: tuple[StableId, ...],
        source_evidence_digest: str,
    ) -> StableId:
        """Return the cross-attempt dedup identity of one Planner problem.

        It contains the project, the frozen source facts, the cutoff, the
        normalized question, the facet set and the trusted source binding.  It
        deliberately excludes run identity, attempt identity, any random
        question identity, **and the enclosing Canon commit**: a plan
        acceptance changes the project commit without changing the text, the
        world, the question or the evidence, and re-dispatching the identical
        repair after every plan revision is exactly the loop this identity
        exists to stop.  A real change to the frozen source digest does yield a
        new opportunity.
        """

        semantic = semantic_question or trace_need_id.root
        digest = content_id(
            {
                "project": request.project_id.root,
                "cutoff": request.chapter_index,
                "semantic_question": semantic,
                "facets": tuple(sorted(item.root for item in unresolved_facets)),
                "source_evidence": source_evidence_digest,
            }
        ).root.removeprefix("sha256:")[:32]
        return StableId(f"memory-problem.{digest}")

    @staticmethod
    def _source_evidence_digest(
        detailed: Stage4PlanningLoopRequest,
        *,
        unresolved_facets: tuple[StableId, ...],
        source_evidence_requirement: SourceBoundEvidenceRequirement | None,
    ) -> str:
        """Return a digest of the frozen source facts this problem reads.

        The digest binds the two roots the repair can actually change — the
        frozen TextRoot and WorldRoot — rather than the whole Canon commit.
        The full ``base_commit`` remains on the finding for safety checks and
        audit, but it is not part of the problem identity: committing an
        unrelated plan revision must not make an unchanged problem look new.
        """

        digest = content_id(
            {
                "text_root": (
                    None
                    if detailed.accepted_text_ref is None
                    else detailed.accepted_text_ref.artifact_id.root
                ),
                "world_root": (
                    None
                    if detailed.accepted_world_ref is None
                    else detailed.accepted_world_ref.artifact_id.root
                ),
                # The snapshot names the exact derived projection the trace was
                # built from, so an actually rebuilt projection is a new fact
                # state; a plan-only commit does not change it.
                "projection_snapshot": (
                    detailed.snapshot_id.root if detailed.snapshot_id is not None else None
                ),
                "facets": tuple(sorted(item.root for item in unresolved_facets)),
                "requirement": (
                    None
                    if source_evidence_requirement is None
                    else source_evidence_requirement.model_dump(mode="json")
                ),
            }
        ).root.removeprefix("sha256:")[:32]
        return digest

    @staticmethod
    def _source_chapter_indices(
        trace: object,
        cutoff: int,
        *,
        required_chapter: int | None = None,
    ) -> tuple[int, ...]:
        """Carry the ranked canonical source chapters into maintenance.

        Planner memory gaps are often grounded in a historical anchor even
        though the runtime task's chapter cursor is the latest committed
        chapter.  The retrieval trace is the authoritative, already bounded
        source selection.  When the reviewed question names scalar predicates,
        preserve the first few matching predicate sources so a maintenance
        attempt cannot silently discard a second requested field.  Relation and
        causal repairs remain single-source to retain their existing bounded
        graph budget.  A question explicitly asking for the current/cutoff
        state is different: the historical anchor is not the source unit that
        can answer it.  In that case route the immutable cutoff chapter itself,
        even when retrieval selected only older World anchors.  The chapter is
        already covered by the finding's TextRoot, visibility receipt, and
        cutoff; this is a bounded source-selection fallback, not an expansion
        of the information boundary.
        """

        def finalize(chapters: tuple[int, ...]) -> tuple[int, ...]:
            # A pre-registered source-bound requirement is authoritative for
            # maintenance routing.  Retrieval may rank an unrelated historical
            # anchor first, but the required source chapter must remain in the
            # finding or MemoryRepairFinding correctly rejects the handoff.
            if required_chapter is not None and 0 <= required_chapter <= cutoff:
                chapters = (*chapters, required_chapter)
            return tuple(sorted(dict.fromkeys(chapters)))

        bundle = getattr(trace, "compiled_query_bundle", {})
        query_texts: list[str] = []
        if isinstance(bundle, Mapping):
            for key in ("semantic_query", "lexical_queries"):
                value = bundle.get(key)
                if isinstance(value, str):
                    query_texts.append(value)
                elif isinstance(value, (list, tuple)):
                    query_texts.extend(str(item) for item in value)

        def predicate_is_named(predicate: object) -> bool:
            if not isinstance(predicate, str) or not predicate.strip():
                return False
            normalized = predicate.strip().casefold()
            if re.fullmatch(r"[a-z0-9_]+", normalized):
                return any(
                    re.search(
                        rf"(?<![a-z0-9_]){re.escape(normalized)}(?![a-z0-9_])",
                        query.casefold(),
                    )
                    for query in query_texts
                )
            return any(normalized in query.casefold() for query in query_texts)

        def asks_for_cutoff_state() -> bool:
            if cutoff < 0:
                return False
            chapter_marker = re.compile(
                rf"(?:第\s*)?{cutoff}\s*章|\b(?:chapter|ch)\s*{cutoff}\b",
                re.IGNORECASE,
            )
            state_markers = (
                "当前",
                "截至",
                "截止",
                "结束时",
                "结尾",
                "末尾",
                "此时",
                "结束后",
                "at the end",
                "as of",
            )
            return any(
                chapter_marker.search(query) is not None
                or any(marker in query.casefold() for marker in state_markers)
                or re.search(
                    r"\bcurrent\s+(?:state|status|location|position)\b",
                    query.casefold(),
                )
                for query in query_texts
            )

        matched_chapters: list[int] = []
        fallback_chapters: list[int] = []
        for candidate in getattr(trace, "candidates", ()):
            if not getattr(candidate, "selected", False):
                continue
            unit = candidate.unit
            candidate_chapters: list[int] = []
            narrative_start = getattr(unit, "narrative_start", None)
            if isinstance(narrative_start, int) and 0 <= narrative_start <= cutoff:
                candidate_chapters.append(narrative_start)
            for evidence in getattr(unit, "evidence_refs", ()):
                chapter_id = getattr(evidence, "chapter_id", None)
                if chapter_id is None:
                    # Evidence that names no chapter cannot select a source
                    # chapter; skip it instead of dereferencing a missing id.
                    continue
                match = re.search(r"\.(\d+)$", chapter_id.root)
                if match:
                    chapter = int(match.group(1))
                    if chapter <= cutoff:
                        candidate_chapters.append(chapter)
            destination = (
                matched_chapters
                if predicate_is_named(getattr(unit, "predicate", None))
                else fallback_chapters
            )
            destination.extend(candidate_chapters)

        intent = getattr(trace, "intent", None)
        if asks_for_cutoff_state():
            # A cutoff/current question must be answered from the latest
            # visible source unit.  Do not spend the bounded maintenance
            # budget on a historical relation anchor first and then risk
            # never reading the chapter that contains the requested fact.
            return finalize((cutoff,))
        limit = 4 if intent is Stage1QueryIntent.CURRENT_STATE and matched_chapters else 1
        selected = matched_chapters if matched_chapters else fallback_chapters
        # MaintenanceTrigger requires canonical ascending chapter indices.  Keep
        # the retrieval-ranked prefix first, then sort only the bounded result.
        return finalize(tuple(dict.fromkeys(selected))[:limit])

    @staticmethod
    def _repair_target_query(compiled: Mapping[str, object]) -> str:
        """Promote the original Planner question, not the retrieval label prefix.

        Retrieval prefixes a facet query with every grounded label to improve
        recall (``<labels> 的当前关系状态... 具体问题: <original>``).  That
        prefix is useful for search but makes a maintenance prompt look like a
        request about dozens of unrelated entities.  The suffix is the
        user-authored question and is the only portion promoted to the durable
        repair handoff.
        """

        # NeedQueryCompiler keeps the original Planner ``query_text`` as the
        # first lexical entry.  Prefer it over the semantic scope label so a
        # pre-registered problem identity remains byte-for-byte stable.
        lexical = compiled.get("lexical_queries")
        original = ""
        if isinstance(lexical, (list, tuple)):
            original = next((str(item).strip() for item in lexical if str(item).strip()), "")
        semantic = str(compiled.get("semantic_query") or "").strip()
        for marker in ("具体问题:", "具体问题\uff1a"):
            _, separator, suffix = semantic.partition(marker)
            if separator and suffix.strip():
                return suffix.strip()
        if semantic.startswith("预注册:") and original:
            return original
        return semantic

    @staticmethod
    def _lineage(
        result: Stage4PlanningLoopResult, proposal_ref: ArtifactRef
    ) -> tuple[ArtifactRef, ...]:
        refs = [proposal_ref]
        for name in (
            "inquiry_ref",
            "inquiry_review_ref",
            "memory_context_ref",
            "planner_context_ref",
            "plan_review_ref",
        ):
            ref = getattr(result, name)
            if ref is not None:
                refs.append(ref)
        refs.extend(result.event_artifacts)
        return tuple({ref.artifact_id: ref for ref in refs}.values())

    @staticmethod
    def _terminal(terminal: Stage4PlanningLoopTerminal) -> PlanningTerminalStatus:
        if terminal is Stage4PlanningLoopTerminal.YIELDED:
            return PlanningTerminalStatus.YIELDED
        if terminal in {
            Stage4PlanningLoopTerminal.MODEL_UNAVAILABLE,
            Stage4PlanningLoopTerminal.SUSPENDED,
        }:
            return PlanningTerminalStatus.SUSPENDED
        if terminal is Stage4PlanningLoopTerminal.HUMAN_REQUIRED:
            return PlanningTerminalStatus.WAITING_INPUT
        if terminal in {
            Stage4PlanningLoopTerminal.INQUIRY_REVIEW_REQUIRED,
            Stage4PlanningLoopTerminal.PLAN_CONFLICT,
            Stage4PlanningLoopTerminal.REVIEW_REVISION_REQUIRED,
            Stage4PlanningLoopTerminal.REVIEW_REQUIRED,
        }:
            return PlanningTerminalStatus.REVIEW_REQUIRED
        return PlanningTerminalStatus.BLOCKED


__all__ = [
    "ProductionStage4InvocationFactory",
    "Stage4InvocationPolicy",
    "Stage4PlanningInvocation",
    "Stage4PlanningLeafAdapter",
]
