"""Real adapter to the public Stage 4 Planner Context Loop boundary."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.creative_runtime import (
    CandidateBinding,
    CandidateKind,
    PlanningLoopRequest,
    PlanningLoopResult,
    PlanningTerminalStatus,
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
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentMode,
    ContractRef,
    PlanningTask,
)
from novel_agent.domain.world import PlanLevel
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.planning_context_loop import (
    ModelRequestFactory,
    PlanningContextLoopService,
)

PLAN_PROPOSAL_MEDIA_TYPE = "application/vnd.novel-agent.plan-proposal+json"


@dataclass(frozen=True, slots=True)
class Stage4PlanningInvocation:
    request: Stage4PlanningLoopRequest
    model_request: ModelRequestFactory
    world: WorldRootDocument | None = None
    text_root: TextRootDocument | None = None
    resume_checkpoint_ref: ArtifactRef | None = None


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
    model_max_output_tokens: int = 8_000


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
        if request.purpose.value == "replan":
            return AgentMode.REPLAN
        if request.plan_level is PlanLevel.STORY:
            return AgentMode.STORY
        if request.plan_level is PlanLevel.ARC_VOLUME:
            return AgentMode.ARC_VOLUME
        if request.plan_level is PlanLevel.CHAPTER:
            return AgentMode.CHAPTER
        if request.plan_level is PlanLevel.SCENE:
            return AgentMode.SCENE
        return AgentMode.CHAPTER_SET

    def _allowed_skill_ids(self, mode: AgentMode) -> tuple[StableId, ...]:
        from novel_agent.agents.planner import planner_skill_ids_for_mode

        mode_ids = planner_skill_ids_for_mode(mode)
        policy_ids = self._policy.allowed_skill_ids
        if not policy_ids:
            return mode_ids
        allowed = tuple(item for item in mode_ids if item in set(policy_ids))
        return allowed or mode_ids

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
        author_intent = request.input_artifact_refs
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
            return ModelRequest(
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
                max_output_tokens=self._policy.model_max_output_tokens,
                timeout_seconds=self._policy.model_timeout_seconds,
                enable_thinking=False,
            )

        return Stage4PlanningInvocation(
            request=detailed,
            model_request=model_request,
            world=world,
            text_root=text,
            resume_checkpoint_ref=resume_checkpoint_ref,
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
        if request.input_artifact_refs and not set(detailed.author_intent_artifacts).issubset(
            request.input_artifact_refs
        ):
            raise ValueError("Stage 4 request introduced an unbound author-intent artifact")
        if not detailed.author_intent_artifacts:
            raise ValueError("Stage 4 CHAPTER_SET requires author-intent artifacts")

        result = await self._loop.run(
            request=detailed,
            model_request=invocation.model_request,
            world=invocation.world,
            text_root=invocation.text_root,
            resume_checkpoint_ref=invocation.resume_checkpoint_ref,
        )
        if result.request_id != detailed.request_id:
            raise RuntimeError("Stage 4 Planner returned cross-request lineage")
        if result.terminal is Stage4PlanningLoopTerminal.PLAN_CANDIDATE_READY:
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
            return PlanningLoopResult(
                result_id=bounded_stable_id(
                    f"{request.task_id.root}.planner-result",
                    request.task_id.root,
                ),
                run_id=request.run_id,
                task_id=request.task_id,
                status=PlanningTerminalStatus.PLAN_CANDIDATE_READY,
                candidate=candidate,
                artifact_refs=lineage,
            )

        status = self._terminal(result.terminal)
        diagnostic = (
            result.diagnostic_codes[0] if result.diagnostic_codes else result.terminal.value
        )
        artifact_refs = result.event_artifacts
        if diagnostic == "PLANNER_MEMORY_FACETS_UNRESOLVED" and request.attempt_id is not None:
            finding_ref = self._memory_gap_finding(
                request,
                detailed,
                result,
                attempt_id=request.attempt_id,
            )
            if finding_ref is not None:
                artifact_refs = tuple(dict.fromkeys((*artifact_refs, finding_ref)))
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
            failure_detail=f"Stage 4 terminal: {result.terminal.value}"[:512],
        )

    def _memory_gap_finding(
        self,
        request: PlanningLoopRequest,
        detailed: Stage4PlanningLoopRequest,
        result: Stage4PlanningLoopResult,
        *,
        attempt_id: StableId,
    ) -> ArtifactRef | None:
        """Materialize only an evidence-bound Canon extraction handoff.

        A Planner terminal is not enough by itself.  The handoff requires the
        exact Stage1 context, an immutable checkpoint, the canonical text root,
        and the current chapter cutoff.  Missing any of these keeps the typed
        Planner terminal fail-closed.
        """

        if (
            detailed.accepted_text_ref is None
            or result.memory_context_ref is None
            or not detailed.author_intent_artifacts
        ):
            return None
        checkpoint_ref = next(
            (
                ref
                for ref in reversed(result.event_artifacts)
                if ref.media_type == PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE
            ),
            None,
        )
        if checkpoint_ref is None:
            return None
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
            return None
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
            return None
        unresolved_facets = tuple(
            receipt.need_facet_id
            for receipt in trace.facet_receipts
            if receipt.mandatory and receipt.status is not FacetClosureStatus.SUPPORTED
        )
        if not unresolved_facets:
            return None
        cutoff = NarrativePosition(chapter_index=request.chapter_index)
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
        identity = content_id(
            {
                "request": detailed.request_id.root,
                "attempt": attempt_id.root,
                "need": trace.need_id.root,
                "facets": tuple(item.root for item in unresolved_facets),
            }
        ).root.removeprefix("sha256:")[:32]
        unresolved_kinds = {
            receipt.facet_kind
            for receipt in trace.facet_receipts
            if receipt.mandatory
            and receipt.status is not FacetClosureStatus.SUPPORTED
            and receipt.need_facet_id in unresolved_facets
        }
        # Graph Curator owns relation candidates only. Causal-history gaps
        # are event/state evidence and must be handed to the ordinary Curator;
        # its graph profile cannot represent those records and would
        # deterministically drop source-bound consequence markers.
        owner = self._memory_gap_owner(unresolved_kinds)
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
            if problem_identity_seed.need_id != trace.need_id:
                return None
            need_query = problem_identity_seed.need_query
            semantic_question = problem_identity_seed.semantic_question
        source_evidence_requirement = (
            None
            if problem_identity_seed is None
            else problem_identity_seed.source_evidence_requirement
        )
        finding = MemoryRepairFinding(
            finding_id=StableId(f"memory-gap.{identity}"),
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
            mandatory_facet_ids=unresolved_facets,
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
            no_progress_key=StableId(f"memory-gap-progress.{identity}"),
        )
        return self._artifacts.put(
            canonical_json_bytes(finding.model_dump(mode="json")),
            "application/vnd.novel-agent.memory-repair-finding+json",
            self._schema_version,
        )

    @staticmethod
    def _memory_gap_owner(unresolved_kinds: set[NeedFacetKind]) -> MemoryRepairOwner:
        """Select the sole Curator profile for the unresolved facet set."""

        return (
            MemoryRepairOwner.GRAPH_CURATOR
            if NeedFacetKind.RELATION_STATE in unresolved_kinds
            else MemoryRepairOwner.ORDINARY_CURATOR
        )

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
                match = re.search(r"\.(\d+)$", evidence.chapter_id.root)
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
