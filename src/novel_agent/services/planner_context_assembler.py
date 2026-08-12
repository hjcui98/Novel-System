"""Assemble the immutable Planner-specific seed for the shared Context Runtime."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.memory import GraphPathReceipt, RetrievalUnit, Stage1ContextPackage
from novel_agent.domain.planning import (
    PlannerContextBudgetReport,
    PlannerContextItem,
    PlannerContextPackage,
    PlannerContextSection,
    PlannerEvidenceExpansionReceipt,
    PlanningInquiry,
    PlanningLoopRequest,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id


class PlannerContextAssemblyError(ValueError):
    pass


class PlannerContextAssembler:
    version = "planner_context_assembler.v1"
    contract_version = "planner_context.v1"

    def __init__(
        self,
        artifacts: ArtifactRepository,
        *,
        schema_version: SchemaVersion,
    ) -> None:
        self._artifacts = artifacts
        self._schema_version = schema_version

    def assemble(
        self,
        *,
        request: PlanningLoopRequest,
        inquiry: PlanningInquiry,
        inquiry_ref: ArtifactRef,
        stage1_context: Stage1ContextPackage | None = None,
        stage1_context_ref: ArtifactRef | None = None,
    ) -> tuple[PlannerContextPackage, ArtifactRef]:
        if inquiry.project_id != request.project_id or inquiry.mode is not request.task.mode:
            raise PlannerContextAssemblyError("Planner inquiry differs from loop request")
        if request.task.mode.value == "project_bootstrap":
            if stage1_context is not None or stage1_context_ref is not None:
                raise PlannerContextAssemblyError("bootstrap cannot consume Memory Gateway output")
        else:
            if stage1_context is None or stage1_context_ref is None:
                raise PlannerContextAssemblyError("post-Genesis Planner context requires Memory")
            if (
                stage1_context.base_commit != request.task.base_commit
                or stage1_context.snapshot_id != request.snapshot_id
            ):
                raise PlannerContextAssemblyError("Planner Memory context basis mismatch")

        mandatory: list[PlannerContextItem] = []
        optional: list[PlannerContextItem] = []
        graph_refs: dict[ArtifactId, ArtifactRef] = {}
        expansion_refs: list[ArtifactRef] = []
        for index, artifact in enumerate(request.author_intent_artifacts):
            mandatory.append(
                self._artifact_item(
                    StableId(f"planner-context.author.{index}"),
                    PlannerContextSection.AUTHOR_INTENT,
                    artifact,
                    protected=True,
                    mandatory=True,
                )
            )
        if request.accepted_plan_ref is not None:
            mandatory.append(
                self._artifact_item(
                    StableId("planner-context.accepted-plan"),
                    PlannerContextSection.ACCEPTED_PLAN,
                    request.accepted_plan_ref,
                    protected=True,
                    mandatory=True,
                )
            )
        for goal in inquiry.goal_proposals:
            mandatory.append(
                PlannerContextItem(
                    context_item_id=StableId(f"planner-context.goal.{goal.goal_id.root}"[:128]),
                    section=PlannerContextSection.WORKING_PROPOSAL,
                    text=f"CANDIDATE GOAL: {goal.summary}\nRATIONALE: {goal.rationale}",
                    protected=True,
                    mandatory=True,
                    token_count=self._tokens(goal.summary + goal.rationale),
                )
            )
        for question in (*inquiry.assumptions, *inquiry.questions):
            if question.blocking:
                mandatory.append(
                    PlannerContextItem(
                        context_item_id=StableId(
                            f"planner-context.unresolved.{question.question_id.root}"[:128]
                        ),
                        section=PlannerContextSection.UNRESOLVED,
                        text=question.question,
                        protected=True,
                        mandatory=True,
                        token_count=self._tokens(question.question),
                    )
                )
        if stage1_context is not None:
            graph_receipts = self._graph_receipts(stage1_context)
            anchor_handles = {
                unit.unit_id for unit in self._context_units(stage1_context, include_raw=False)
            }
            for unit in self._context_units(stage1_context, include_raw=False):
                item, receipt_refs = self._unit_item(
                    unit,
                    graph_receipts.get(unit.unit_id, ()),
                    compact_handle=unit.unit_id,
                )
                optional.append(item)
                for receipt_ref in receipt_refs:
                    graph_refs[receipt_ref.artifact_id] = receipt_ref
            expanded_by_handle: dict[StableId, list[RetrievalUnit]] = defaultdict(list)
            for unit in stage1_context.raw_evidence_spans:
                if unit.parent_unit_id is not None:
                    expanded_by_handle[unit.parent_unit_id].append(unit)
            for handle in sorted(anchor_handles, key=lambda item: item.root):
                expanded = tuple(expanded_by_handle.get(handle, ()))
                if not expanded:
                    continue
                parent_id = expanded[0].parent_unit_id
                assert parent_id is not None
                receipt = PlannerEvidenceExpansionReceipt(
                    receipt_id=content_id(
                        {
                            "basis": stage1_context.base_commit.root,
                            "snapshot": stage1_context.snapshot_id.root,
                            "handle": handle.root,
                            "source": parent_id.root,
                            "expanded": tuple(item.unit_id.root for item in expanded),
                            "evidence": tuple(
                                evidence.model_dump(mode="json")
                                for item in expanded
                                for evidence in item.evidence_refs
                            ),
                        }
                    ),
                    base_commit=stage1_context.base_commit,
                    snapshot_id=stage1_context.snapshot_id,
                    compact_handle=handle,
                    source_unit_id=parent_id,
                    expanded_unit_ids=tuple(item.unit_id for item in expanded),
                    evidence_refs=tuple(
                        evidence for item in expanded for evidence in item.evidence_refs
                    ),
                )
                receipt_ref = self._artifacts.put(
                    canonical_json_bytes(receipt.model_dump(mode="json")),
                    "application/vnd.novel-agent.planner-evidence-expansion+json",
                    self._schema_version,
                )
                expansion_refs.append(receipt_ref)
                for unit in expanded:
                    item, unit_graph_refs = self._unit_item(
                        unit,
                        graph_receipts.get(unit.unit_id, ()),
                        compact_handle=handle,
                    )
                    optional.append(item)
                    for graph_ref in unit_graph_refs:
                        graph_refs[graph_ref.artifact_id] = graph_ref

        budget = request.budgets.context.token_budget
        mandatory_tokens = sum(item.token_count for item in mandatory)
        if mandatory_tokens > budget:
            raise PlannerContextAssemblyError("protected Planner context exceeds hard limit")
        selected = list(mandatory)
        selected_tokens = mandatory_tokens
        dropped: list[StableId] = []
        drop_reasons: dict[str, str] = {}
        for item in self._diverse(optional):
            if selected_tokens + item.token_count <= budget:
                selected.append(item)
                selected_tokens += item.token_count
            else:
                dropped.append(item.context_item_id)
                drop_reasons[item.context_item_id.root] = "optional_token_budget"
        rendered = self._render(tuple(selected), inquiry)
        package_identity = content_id(
            {
                "version": self.version,
                "request": request.request_id.root,
                "inquiry": inquiry_ref.artifact_id.root,
                "items": tuple(item.model_dump(mode="json") for item in selected),
                "budget": budget,
            }
        ).root.removeprefix("sha256:")[:24]
        package = PlannerContextPackage(
            package_id=StableId(f"planner-context.{package_identity}"),
            contract_version=self.contract_version,
            project_id=request.project_id,
            mode=request.task.mode,
            planning_scope=inquiry.planning_scope,
            horizon_start=inquiry.horizon_start,
            horizon_end=inquiry.horizon_end,
            base_commit=request.task.base_commit,
            snapshot_id=request.snapshot_id,
            profile_ref=request.project_profile_ref,
            reviewed_inquiry_ref=inquiry_ref,
            stage1_context_ref=stage1_context_ref,
            items=tuple(selected),
            unresolved_gaps=(() if stage1_context is None else stage1_context.unresolved_gaps),
            need_ids=(
                ()
                if stage1_context is None
                else tuple(trace.need_id for trace in stage1_context.retrieval_traces)
            ),
            retrieval_unit_ids=tuple(
                dict.fromkeys(unit_id for item in selected for unit_id in item.retrieval_unit_ids)
            ),
            evidence_refs=tuple(
                dict.fromkeys(evidence for item in selected for evidence in item.evidence_refs)
            ),
            graph_path_receipt_refs=tuple(graph_refs.values()),
            expansion_receipt_refs=tuple(expansion_refs),
            budget_report=PlannerContextBudgetReport(
                token_budget=budget,
                mandatory_tokens=mandatory_tokens,
                selected_tokens=selected_tokens,
                dropped_item_ids=tuple(dropped),
                drop_reasons=drop_reasons,
            ),
            rendered_context=rendered,
        )
        artifact = self._artifacts.put(
            canonical_json_bytes(package.model_dump(mode="json")),
            "application/vnd.novel-agent.planner-context-package+json",
            self._schema_version,
        )
        return package, artifact

    def _artifact_item(
        self,
        item_id: StableId,
        section: PlannerContextSection,
        artifact: ArtifactRef,
        *,
        protected: bool,
        mandatory: bool,
    ) -> PlannerContextItem:
        try:
            text = self._artifacts.read_verified(artifact).decode("utf-8")
        except UnicodeDecodeError as error:
            raise PlannerContextAssemblyError("Planner context source is not UTF-8") from error
        return PlannerContextItem(
            context_item_id=item_id,
            section=section,
            text=text,
            protected=protected,
            mandatory=mandatory,
            token_count=self._tokens(text),
            source_artifact_refs=(artifact,),
        )

    def _unit_item(
        self,
        unit: RetrievalUnit,
        graph_receipts: tuple[GraphPathReceipt, ...],
        *,
        compact_handle: StableId,
    ) -> tuple[PlannerContextItem, tuple[ArtifactRef, ...]]:
        section = self._section(unit, has_graph=bool(graph_receipts))
        graph_refs = tuple(
            self._artifacts.put(
                canonical_json_bytes(receipt.model_dump(mode="json")),
                "application/vnd.novel-agent.graph-path+json",
                self._schema_version,
            )
            for receipt in graph_receipts
        )
        return (
            PlannerContextItem(
                context_item_id=StableId(f"planner-context.unit.{unit.unit_id.root}"[:128]),
                section=section,
                text=unit.text,
                protected=section is PlannerContextSection.ACCEPTED_PLAN,
                mandatory=unit.mandatory,
                token_count=self._tokens(unit.text),
                retrieval_unit_ids=(unit.unit_id,),
                evidence_refs=unit.evidence_refs,
                graph_path_receipt_refs=graph_refs,
                compact_handle=compact_handle,
            ),
            graph_refs,
        )

    @staticmethod
    def _graph_receipts(
        context: Stage1ContextPackage,
    ) -> dict[StableId, tuple[GraphPathReceipt, ...]]:
        by_unit: dict[StableId, dict[StableId, GraphPathReceipt]] = defaultdict(dict)
        for trace in context.retrieval_traces:
            for candidate in trace.candidates:
                for hit in candidate.channel_hits:
                    for receipt in hit.graph_path_receipts:
                        by_unit[candidate.unit.unit_id][receipt.path_id] = receipt
        return {unit_id: tuple(receipts.values()) for unit_id, receipts in by_unit.items()}

    @staticmethod
    def _context_units(
        context: Stage1ContextPackage,
        *,
        include_raw: bool,
    ) -> tuple[RetrievalUnit, ...]:
        units = (
            *context.mandatory_constraints,
            *context.current_world_state,
            *context.active_plan_obligations,
            *context.relevant_historical_events,
            *context.truth_and_knowledge_boundaries,
            *context.style_or_reference_optional,
        )
        if include_raw:
            units = (*units, *context.raw_evidence_spans)
        by_id = {unit.unit_id: unit for unit in units}
        return tuple(by_id.values())

    @staticmethod
    def _section(unit: RetrievalUnit, *, has_graph: bool) -> PlannerContextSection:
        if unit.information_label == "plan":
            return PlannerContextSection.ACCEPTED_PLAN
        if has_graph:
            return PlannerContextSection.RELATION_CAUSAL
        if unit.narrative_start is not None:
            return PlannerContextSection.HISTORY_DEVIATION
        return PlannerContextSection.CURRENT_STATE

    @staticmethod
    def _diverse(items: Iterable[PlannerContextItem]) -> tuple[PlannerContextItem, ...]:
        groups: dict[tuple[str, str, str], deque[PlannerContextItem]] = defaultdict(deque)
        for item in items:
            source = (
                item.source_artifact_refs[0].artifact_id.root
                if item.source_artifact_refs
                else "no-source"
            )
            path = (
                item.graph_path_receipt_refs[0].artifact_id.root
                if item.graph_path_receipt_refs
                else "no-path"
            )
            groups[(item.section.value, source, path)].append(item)
        ordered: list[PlannerContextItem] = []
        keys = sorted(groups)
        while keys:
            next_keys: list[tuple[str, str, str]] = []
            for key in keys:
                ordered.append(groups[key].popleft())
                if groups[key]:
                    next_keys.append(key)
            keys = next_keys
        return tuple(ordered)

    @staticmethod
    def _render(items: tuple[PlannerContextItem, ...], inquiry: PlanningInquiry) -> str:
        lines = [
            f"PLANNING_MODE={inquiry.mode.value}",
            f"PLANNING_SCOPE={','.join(inquiry.planning_scope)}",
        ]
        for item in items:
            lines.append(f"\n[{item.section.value}:{item.context_item_id.root}]\n{item.text}")
        return "\n".join(lines)

    @staticmethod
    def _tokens(text: str) -> int:
        return max(1, (len(text.encode("utf-8")) + 3) // 4)
