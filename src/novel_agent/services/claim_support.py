"""Public, Gold-free support verification and Controller-side context selection."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

from pydantic import Field

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    NeedFacet,
    NeedFacetKind,
    RequirementLevel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
)
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelCallRecord,
    ModelRequest,
    ModelRole,
)
from novel_agent.domain.stage2 import ContextAssemblySpec
from novel_agent.domain.text import EvidenceRef
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    ClaimReductionLevel,
    ClaimSupportGroup,
    ClaimSupportReceipt,
    ClaimVariant,
    CutoffAttestation,
    EvidenceResolutionStatus,
    SemanticSupportStatus,
    WriterContextSection,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes, quote_hash
from novel_agent.services.model_gateway import ModelGateway
from novel_agent.services.need_completion import (
    NeedCompletionEvaluator,
    NeedCompletionResult,
    NeedCompletionStatus,
    NeedFacetClosureState,
)

TokenCounter = Callable[[str], int]
SupportArtifactWriter = Callable[[bytes, str], ArtifactRef]
SupportProgressWriter = Callable[[Mapping[str, object]], None]
# Two bounded Needs fit comfortably inside the 128K local endpoint and halve
# the number of serial proposal requests. A larger batch makes one malformed
# response affect too many Needs, so keep the blast radius small.
PROPOSAL_BATCH_SIZE = 2
SEMANTIC_SUPPORT_INPUT_LIMIT = 20
# A semantic claim may close a small public multi-hop Need with more than one
# cutoff-safe passage.  Keep this bounded: larger groups make the model's
# entailment decision less auditable and increase the chance that unrelated
# evidence is smuggled into one claim.
SEMANTIC_SUPPORT_MAX_UNITS_PER_CLAIM = 3
# A verifier must see every cutoff-safe unit exposed to the proposer for the
# same Need. Otherwise a stale claim can cite one passage while a later unit
# that supersedes it remains invisible to verification.
SEMANTIC_SUPPORT_VERIFIER_CONTEXT_UNIT_LIMIT = SEMANTIC_SUPPORT_INPUT_LIMIT
# Preserve the former maximum verifier prompt scale (8 claims x 8 units), but
# batch by accumulated context rather than by claim count.
SEMANTIC_SUPPORT_VERIFIER_BATCH_CONTEXT_UNIT_BUDGET = 64
SEMANTIC_SUPPORT_LATE_GROUNDED_UNIT_LIMIT = 4
SEMANTIC_SUPPORT_CAUSAL_CHAPTER_WINDOW = 2
# Proposal calls occasionally spend most of a 1024-token completion on model
# deliberation and reach the endpoint limit before the small structured payload
# is closed.  This is a model-call ceiling only; Writer and evidence-ledger
# budgets remain enforced independently by the assembler.
SEMANTIC_SUPPORT_PROPOSAL_MAX_OUTPUT_TOKENS = 2048
SEMANTIC_SUPPORT_VERIFICATION_MAX_OUTPUT_TOKENS = 1024
# The local Qwen service is single-concurrency.  C60 showed that proposal
# generations can legitimately cross 120 seconds, while every verifier batch
# completed well below that ceiling.  A cancelled proposal can also remain in
# the inference server briefly and make subsequent requests queue behind it.
# Separate the two stages instead of applying the verifier's short limit to the
# more expensive proposal call.
SEMANTIC_SUPPORT_PROPOSAL_TIMEOUT_SECONDS = 300.0
SEMANTIC_SUPPORT_VERIFICATION_TIMEOUT_SECONDS = 120.0
SEMANTIC_SUPPORT_HISTORICAL_EXCERPT_LIMIT = 1600


class SupportSelectionResult(DomainModel):
    context_assembly_spec: ContextAssemblySpec
    support_groups: tuple[ClaimSupportGroup, ...]
    claim_variants: tuple[ClaimVariant, ...]
    support_receipts: tuple[ClaimSupportReceipt, ...]
    cutoff_attestations: tuple[CutoffAttestation, ...]
    completion_results: tuple[NeedCompletionResult, ...]
    diagnostic_codes: tuple[str, ...] = ()
    producer_version: str = Field(min_length=1)


class SemanticSupportClaimDraft(DomainModel):
    """Gold-free semantic claim proposed against public Need/facet identities."""

    need_id: StableId
    need_facet_ids: tuple[StableId, ...] = Field(min_length=1)
    retrieval_unit_ids: tuple[StableId, ...] = Field(
        min_length=1,
        max_length=SEMANTIC_SUPPORT_MAX_UNITS_PER_CLAIM,
    )
    claim_text: str = Field(min_length=1, max_length=600)


class SemanticSupportBatch(DomainModel):
    claims: tuple[SemanticSupportClaimDraft, ...] = Field(max_length=64)
    insufficient_need_ids: tuple[StableId, ...] = ()


class SemanticSupportDecision(DomainModel):
    claim_index: int = Field(ge=0)
    supports: bool
    counter_evidence_retrieval_unit_ids: tuple[StableId, ...] = ()


class SemanticSupportVerificationBatch(DomainModel):
    decisions: tuple[SemanticSupportDecision, ...]


@dataclass(frozen=True, slots=True)
class _SemanticProposalAudit:
    draft: SemanticSupportClaimDraft
    call: ModelCallRecord
    input_hash: ArtifactId
    output_hash: ArtifactId
    input_ref: ArtifactRef
    output_ref: ArtifactRef


@dataclass(frozen=True, slots=True)
class _SemanticVerificationAudit:
    decision: SemanticSupportDecision
    call: ModelCallRecord
    input_hash: ArtifactId
    output_hash: ArtifactId
    input_ref: ArtifactRef
    output_ref: ArtifactRef


class TrustedClaimSupportProducer:
    """Produce replayable narrow claims from public retrieval results."""

    version = "trusted_claim_support_producer.v24"

    def __init__(
        self,
        *,
        semantic_gateway: ModelGateway | None = None,
        artifact_writer: SupportArtifactWriter | None = None,
        progress_writer: SupportProgressWriter | None = None,
    ) -> None:
        self._semantic_gateway = semantic_gateway
        self._artifact_writer = artifact_writer
        self._progress_writer = progress_writer
        self.last_diagnostic_codes: tuple[str, ...] = ()
        self._semantic_cache: dict[
            str,
            tuple[
                SemanticSupportBatch,
                ModelCallRecord,
                ArtifactId,
                ArtifactId,
                ArtifactRef,
                ArtifactRef,
            ],
        ] = {}
        self._verification_cache: dict[
            str,
            tuple[
                SemanticSupportVerificationBatch,
                ModelCallRecord,
                ArtifactId,
                ArtifactId,
                ArtifactRef,
                ArtifactRef,
            ],
        ] = {}

    def produce(
        self,
        *,
        task: BenchmarkTaskContract,
        units: tuple[RetrievalUnit, ...],
        needs: tuple[Stage1MemoryNeed, ...],
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        unit_need_ids: Mapping[StableId, tuple[StableId, ...]],
        token_counter: TokenCounter,
    ) -> tuple[
        tuple[ClaimSupportGroup, ...],
        tuple[ClaimVariant, ...],
        tuple[ClaimSupportReceipt, ...],
        tuple[CutoffAttestation, ...],
    ]:
        self.last_diagnostic_codes = ()
        need_by_id = {need.need_id: need for need in needs}
        groups: list[ClaimSupportGroup] = []
        variants: list[ClaimVariant] = []
        receipts: list[ClaimSupportReceipt] = []
        attestations: list[CutoffAttestation] = []
        for unit in units:
            mapped_needs = tuple(
                need_by_id[need_id]
                for need_id in unit_need_ids.get(unit.unit_id, ())
                if need_id in need_by_id
            )
            for need in mapped_needs:
                if not self._legal_for_need(task, need, unit):
                    continue
                for claim_text, evidence_refs in self._claim_candidates(unit, need):
                    facets = self._supported_facets(need, unit, claim_text)
                    if not facets:
                        continue
                    plan_node_ids = self._plan_node_ids(unit)
                    if not evidence_refs and not plan_node_ids:
                        continue
                    resolution = self._resolution_status(
                        evidence_refs,
                        unit,
                        basis_commit_id=basis_commit_id,
                        checkpoint_chapter=task.checkpoint_chapter,
                        plan_node_ids=plan_node_ids,
                    )
                    semantic = (
                        SemanticSupportStatus.VERIFIED
                        if resolution is EvidenceResolutionStatus.RESOLVED
                        and unit.support_status not in {"unsupported", "contradicted"}
                        else SemanticSupportStatus.UNVERIFIED
                    )
                    identity = canonical_json_bytes(
                        {
                            "need_id": need.need_id.root,
                            "unit_id": unit.unit_id.root,
                            "claim": claim_text,
                            "facets": [facet.need_facet_id.root for facet in facets],
                            "evidence": [
                                reference.model_dump(mode="json") for reference in evidence_refs
                            ],
                            "plan_nodes": [item.root for item in plan_node_ids],
                        }
                    )
                    digest = sha256_id(identity).root.removeprefix("sha256:")
                    claim_id = StableId(f"claim.{digest[:48]}")
                    group_id = StableId(f"support-group.{digest[:48]}")
                    attestation = CutoffAttestation(
                        attestation_id=StableId(f"cutoff-attestation.{digest[:48]}"),
                        basis_commit_id=basis_commit_id,
                        basis_snapshot_id=basis_snapshot_id,
                        checkpoint_chapter=task.checkpoint_chapter,
                        information_scope=unit.access_scope,
                        retrieval_unit_ids=(unit.unit_id,),
                        producer=self.version,
                        producer_version=self.version,
                    )
                    attestation_ref = self._artifact_ref(
                        attestation,
                        "application/vnd.novel-agent.cutoff-attestation+json",
                    )
                    receipt = ClaimSupportReceipt(
                        receipt_id=StableId(f"support-receipt.{digest[:48]}"),
                        support_group_id=group_id,
                        claim_id=claim_id,
                        claim_text_hash=sha256_id(claim_text.encode("utf-8")),
                        need_ids=(need.need_id,),
                        need_facet_ids=tuple(facet.need_facet_id for facet in facets),
                        retrieval_unit_ids=(unit.unit_id,),
                        evidence_refs=evidence_refs,
                        plan_node_ids=plan_node_ids,
                        evidence_resolution_status=resolution,
                        semantic_support_status=semantic,
                        basis_commit_id=basis_commit_id,
                        basis_snapshot_id=basis_snapshot_id,
                        cutoff_attestation_ref=attestation_ref,
                        information_scope=unit.access_scope,
                        producer=self.version,
                        producer_version=self.version,
                    )
                    receipt_ref = self._artifact_ref(
                        receipt,
                        "application/vnd.novel-agent.claim-support-receipt+json",
                    )
                    group = ClaimSupportGroup(
                        support_group_id=group_id,
                        claim_id=claim_id,
                        need_ids=(need.need_id,),
                        need_facet_ids=receipt.need_facet_ids,
                        retrieval_unit_ids=(unit.unit_id,),
                        evidence_refs=evidence_refs,
                        plan_node_ids=plan_node_ids,
                        evidence_resolution_status=resolution,
                        semantic_support_status=semantic,
                        support_receipt_ref=receipt_ref,
                        producer=self.version,
                        producer_version=self.version,
                        cutoff_attestation_ref=attestation_ref,
                    )
                    variant = ClaimVariant(
                        claim_variant_id=StableId(f"claim-variant.{digest[:48]}"),
                        claim_id=claim_id,
                        support_group_id=group_id,
                        claim_text=claim_text,
                        claim_text_hash=receipt.claim_text_hash,
                        covered_need_facet_ids=receipt.need_facet_ids,
                        support_receipt_ref=receipt_ref,
                        token_cost=max(1, token_counter(claim_text)),
                        reduction_level=(
                            ClaimReductionLevel.NARROW
                            if unit.unit_kind
                            in {
                                RetrievalUnitKind.GROUNDED_BLOCK,
                                RetrievalUnitKind.GROUNDED_SPAN,
                            }
                            else ClaimReductionLevel.FULL
                        ),
                        producer=self.version,
                        producer_version=self.version,
                    )
                    groups.append(group)
                    variants.append(variant)
                    receipts.append(receipt)
                    attestations.append(attestation)
        deterministic = self._coalesce(
            tuple(groups),
            tuple(variants),
            tuple(receipts),
            tuple(attestations),
            need_by_id=need_by_id,
        )
        if self._semantic_gateway is None or not needs or not units:
            return deterministic
        semantic_bundle = self._produce_semantic_support(
            task=task,
            units=units,
            needs=needs,
            basis_commit_id=basis_commit_id,
            basis_snapshot_id=basis_snapshot_id,
            unit_need_ids=unit_need_ids,
            token_counter=token_counter,
        )
        return self._coalesce(
            (*deterministic[0], *semantic_bundle[0]),
            (*deterministic[1], *semantic_bundle[1]),
            (*deterministic[2], *semantic_bundle[2]),
            (*deterministic[3], *semantic_bundle[3]),
            need_by_id=need_by_id,
        )

    def _produce_semantic_support(
        self,
        *,
        task: BenchmarkTaskContract,
        units: tuple[RetrievalUnit, ...],
        needs: tuple[Stage1MemoryNeed, ...],
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        unit_need_ids: Mapping[StableId, tuple[StableId, ...]],
        token_counter: TokenCounter,
    ) -> tuple[
        tuple[ClaimSupportGroup, ...],
        tuple[ClaimVariant, ...],
        tuple[ClaimSupportReceipt, ...],
        tuple[CutoffAttestation, ...],
    ]:
        assert self._semantic_gateway is not None
        need_by_id = {need.need_id: need for need in needs}
        unit_by_id = {unit.unit_id: unit for unit in units}
        input_order = {unit.unit_id: index for index, unit in enumerate(units)}
        units_by_need: dict[StableId, list[RetrievalUnit]] = {need.need_id: [] for need in needs}
        for unit in units:
            for need_id in unit_need_ids.get(unit.unit_id, ()):
                mapped_need = need_by_id.get(need_id)
                if mapped_need is not None and self._legal_for_need(task, mapped_need, unit):
                    units_by_need[need_id].append(unit)

        public_needs: list[dict[str, object]] = []
        allowed_units_by_need: dict[StableId, set[StableId]] = {}
        for public_need in needs:
            ranked_all = sorted(
                units_by_need[public_need.need_id],
                # `units` is assembled from the Controller's fused retrieval
                # order.  Sorting this list by query-term overlap promoted
                # common words such as "同伴" above a more relevant grounded
                # fallback hit and made the semantic proposer see the wrong
                # evidence first.  Preserve the retrieval order here; the
                # lexical relevance helper remains available for deterministic
                # Controller selection after a claim has been proposed.
                key=lambda unit: (input_order[unit.unit_id], unit.unit_id.root),
            )
            # Keep the semantic prompt bounded at the same scale as the
            # Controller's candidate window. Long-range and plan-conditioned
            # Needs deliberately put grounded rescue results first: the
            # primary anchor window often contains a lexical near-match, while
            # the declared grounded fallback contains the actual event. When
            # an expanded span exposes a parent grounded block, prefer that
            # canonical parent so the semantic claim follows the same unit
            # lineage that the Writer ledger can select. This is still public
            # retrieval metadata, not evaluator knowledge.
            ordered: tuple[RetrievalUnit, ...] = tuple(ranked_all)
            need_type = getattr(public_need.need_type, "value", public_need.need_type)
            historical_grounded_rescue = need_type in {
                "long_range_callback",
                "plan_conditioned_history",
            } or any(
                facet.facet_kind is NeedFacetKind.CAUSAL_HISTORY
                or facet.expected_claim_scope.value == "historical"
                for facet in public_need.need_facets
            )
            if historical_grounded_rescue:
                grounded = tuple(
                    unit
                    for unit in ranked_all
                    if getattr(unit.unit_kind, "value", unit.unit_kind)
                    in {
                        RetrievalUnitKind.GROUNDED_BLOCK.value,
                        RetrievalUnitKind.GROUNDED_SPAN.value,
                    }
                    and unit.evidence_refs
                )
                anchor_chapters = tuple(
                    chapter
                    for unit in ranked_all
                    if getattr(unit.unit_kind, "value", unit.unit_kind)
                    == RetrievalUnitKind.EVENT_ANCHOR.value
                    for chapter in (self._chapter_index(unit),)
                    if chapter is not None
                )
                latest_anchor_chapter = max(anchor_chapters, default=None)
                local_grounded = tuple(
                    unit
                    for unit in grounded
                    if (
                        (chapter := self._chapter_index(unit)) is not None
                        and latest_anchor_chapter is not None
                        and 0
                        < chapter - latest_anchor_chapter
                        <= SEMANTIC_SUPPORT_CAUSAL_CHAPTER_WINDOW
                    )
                )
                local_grounded = tuple(
                    sorted(
                        local_grounded,
                        key=lambda unit: (
                            -len(unit.text),
                            0
                            if getattr(unit.unit_kind, "value", unit.unit_kind)
                            == RetrievalUnitKind.GROUNDED_BLOCK.value
                            else 1,
                            -(self._chapter_index(unit) or -1),
                            input_order[unit.unit_id],
                            unit.unit_id.root,
                        ),
                    )
                )
                late_grounded_candidates = grounded[-SEMANTIC_SUPPORT_LATE_GROUNDED_UNIT_LIMIT:]
                grounded_by_id = {unit.unit_id: unit for unit in grounded}
                lineage_grounded_list: list[RetrievalUnit] = []
                for late_unit in late_grounded_candidates:
                    if getattr(late_unit.unit_kind, "value", late_unit.unit_kind) != (
                        RetrievalUnitKind.GROUNDED_SPAN.value
                    ):
                        continue
                    for parent_id in (late_unit.parent_unit_id, *late_unit.parent_unit_ids):
                        if parent_id is None:
                            continue
                        parent = grounded_by_id.get(parent_id)
                        if (
                            parent is not None
                            and getattr(parent.unit_kind, "value", parent.unit_kind)
                            == RetrievalUnitKind.GROUNDED_BLOCK.value
                        ):
                            lineage_grounded_list.append(parent)
                rescue_candidates = (
                    *local_grounded,
                    *lineage_grounded_list,
                    *late_grounded_candidates,
                )
                late_grounded = tuple({unit.unit_id: unit for unit in rescue_candidates}.values())[
                    :SEMANTIC_SUPPORT_LATE_GROUNDED_UNIT_LIMIT
                ]
                late_ids = {unit.unit_id for unit in late_grounded}
                if local_grounded:
                    # A causal event with a local follow-up should not expose
                    # older lexical matches to the proposer. They can win on
                    # wording alone even after the event anchor is removed.
                    ordered = local_grounded
                else:
                    # When no local chapter window is available, keep the
                    # bounded grounded rescue set as the only prose evidence
                    # shown to the semantic proposer.  The remaining grounded
                    # hits are still available to deterministic support
                    # production, but mixing them into this prompt lets a
                    # lexical near-match close a historical Need before the
                    # protected rescue unit is considered.
                    ordered = (
                        *late_grounded,
                        *(
                            unit
                            for unit in ranked_all
                            if unit.unit_id not in late_ids
                            and getattr(unit.unit_kind, "value", unit.unit_kind)
                            not in {
                                RetrievalUnitKind.EVENT_ANCHOR.value,
                                RetrievalUnitKind.GROUNDED_BLOCK.value,
                                RetrievalUnitKind.GROUNDED_SPAN.value,
                            }
                        ),
                    )
            ranked = tuple(dict.fromkeys(ordered[:SEMANTIC_SUPPORT_INPUT_LIMIT]))
            if not ranked:
                continue
            allowed_units_by_need[public_need.need_id] = {unit.unit_id for unit in ranked}
            public_needs.append(
                {
                    "need_id": public_need.need_id.root,
                    "need_type": public_need.need_type,
                    "query_intent": public_need.query_intent.value,
                    "query_text": public_need.query_text[:1600],
                    "why_needed": public_need.why_needed[:600],
                    "required_need_facets": [
                        {
                            "need_facet_id": facet.need_facet_id.root,
                            "facet_kind": facet.facet_kind.value,
                            "expected_claim_scope": facet.expected_claim_scope.value,
                        }
                        for facet in public_need.need_facets
                        if public_need.completion_spec is None
                        or facet.need_facet_id
                        in public_need.completion_spec.required_need_facet_ids
                    ],
                    "evidence_units": [
                        {
                            "retrieval_unit_id": unit.unit_id.root,
                            "unit_kind": unit.unit_kind.value,
                            "chapter_id": (
                                unit.evidence_refs[0].chapter_id.root
                                if unit.evidence_refs
                                and unit.evidence_refs[0].chapter_id is not None
                                else None
                            ),
                            "text": self._semantic_excerpt(public_need, unit),
                            "has_evidence": bool(unit.evidence_refs),
                            "plan_node_ids": [item.root for item in self._plan_node_ids(unit)],
                        }
                        for unit in ranked
                    ],
                }
            )
        if not public_needs:
            return (), (), (), ()
        proposal_audits: list[_SemanticProposalAudit] = []
        for offset in range(0, len(public_needs), PROPOSAL_BATCH_SIZE):
            need_batch = public_needs[offset : offset + PROPOSAL_BATCH_SIZE]
            producer_input = {
                "task": task.model_dump(mode="json"),
                "basis_commit_id": basis_commit_id.root,
                "basis_snapshot_id": basis_snapshot_id.root,
                "needs": need_batch,
            }
            input_bytes = canonical_json_bytes(producer_input)
            prompt = (
                "You are a pre-freeze support claim proposer. Work only from the public "
                "memory Needs and cutoff-safe evidence units below. Gold annotations, future "
                "text, and evaluator contracts are unavailable and must never be inferred. "
                "For each Need, emit only concise Chinese Writer-facing claims that are "
                "directly entailed by the cited evidence units. Prefer one compound claim "
                "when multiple cited units jointly establish the required public facets; "
                "preserve quantities, negation, epistemic scope, causal links, limitations, "
                "unresolved status, and relationship direction exactly. Do not add plausible "
                "details. A public facet kind is a coverage question, not an asserted value: "
                "in particular, unresolved_status asks whether the matter is still open or has "
                "been resolved. Never infer that it remains unresolved from that label alone. "
                "When a supplied observed/current state establishes fulfillment or a current "
                "relationship, report that resolved/current state; an earlier plan, wish, or "
                "promise remains historical intent and cannot override the later state. "
                "Every returned need_id, need_facet_id, and retrieval_unit_id must "
                "be copied verbatim from the same Need entry. You MUST account for every "
                "input Need: return exactly one concise compound claim when evidence supports "
                "a required facet; otherwise copy its need_id into insufficient_need_ids. "
                "For causal_history, long_range_callback, and plan_conditioned_history Needs, "
                "when a grounded_block or grounded_span with evidence is available, prefer it "
                "over an event anchor that only shares the query wording; use an anchor only "
                "when no grounded unit can directly support the required facet. "
                f"Each claim must cite exactly the smallest sufficient evidence set: normally one "
                f"retrieval unit; use two or three only when distinct units are needed to close "
                f"different required facets or historical steps, and never more than "
                f"{SEMANTIC_SUPPORT_MAX_UNITS_PER_CLAIM}; "
                "When one required facet is fully supported by one unit, cite that unit. When a "
                "complete conclusion is distributed across multiple supplied evidence units, "
                "combine two or three jointly necessary units into one compound claim instead "
                "of emitting a partial claim; this is allowed even when the Need has one facet, "
                "but never combine unrelated passages. For a grounded historical unit, include "
                "all material subject/action/cause/consequence details "
                "visible in that passage, rather than copying its chapter title or unrelated "
                "background. "
                "do not "
                "copy the full evidence_units list into a claim. "
                "The union of claimed and insufficient Need IDs must equal all input Need "
                "IDs, with no unknown IDs.\n"
                '<PUBLIC_SUPPORT_INPUT trusted="false">\n'
                + input_bytes.decode("utf-8")
                + "\n</PUBLIC_SUPPORT_INPUT>"
            )
            prompt_bytes = prompt.encode("utf-8")
            input_hash = sha256_id(prompt_bytes)
            cache_key = input_hash.root
            cached = self._semantic_cache.get(cache_key)
            if cached is None:
                suffix = input_hash.root.removeprefix("sha256:")[:24]
                request = ModelRequest(
                    request_id=StableId(f"support-proposal.{suffix}"),
                    run_id=needs[0].run_id,
                    task_id=needs[0].task_id,
                    model_role=ModelRole.BATCH_TEST,
                    purpose=ModelCallPurpose.BATCH_TEST,
                    trace_id=f"stage2m-support-proposal:{task.task_id.root}:{suffix}",
                    prompt=prompt,
                    max_output_tokens=SEMANTIC_SUPPORT_PROPOSAL_MAX_OUTPUT_TOKENS,
                    timeout_seconds=SEMANTIC_SUPPORT_PROPOSAL_TIMEOUT_SECONDS,
                )
                try:
                    batch, call = asyncio.run(
                        self._semantic_gateway.generate_structured(
                            request,
                            SemanticSupportBatch,
                        )
                    )
                except Exception as error:
                    self._record_progress(
                        stage="proposal",
                        batch_index=offset // PROPOSAL_BATCH_SIZE + 1,
                        status="failed",
                        error_type=type(error).__name__,
                    )
                    self.last_diagnostic_codes = tuple(
                        dict.fromkeys(
                            (
                                *self.last_diagnostic_codes,
                                f"SEMANTIC_SUPPORT_PRODUCER_{type(error).__name__.upper()}",
                            )
                        )
                    )
                    continue
                raw_output = self._semantic_gateway.raw_responses.get(request.request_id.root)
                if raw_output is None:
                    self.last_diagnostic_codes = tuple(
                        dict.fromkeys(
                            (
                                *self.last_diagnostic_codes,
                                "SEMANTIC_SUPPORT_RAW_OUTPUT_MISSING",
                            )
                        )
                    )
                    continue
                raw_output_bytes = raw_output.encode("utf-8")
                output_hash = sha256_id(raw_output_bytes)
                input_ref = self._retain_bytes(
                    prompt_bytes,
                    "application/vnd.novel-agent.support-proposal-prompt+text",
                )
                output_ref = self._retain_bytes(
                    raw_output_bytes,
                    "application/vnd.novel-agent.support-proposal-output+json",
                )
                cached = (
                    batch,
                    call,
                    input_hash,
                    output_hash,
                    input_ref,
                    output_ref,
                )
                self._semantic_cache[cache_key] = cached
            (
                batch,
                call_object,
                input_hash,
                output_hash,
                input_ref,
                output_ref,
            ) = cached
            self._record_progress(
                stage="proposal",
                batch_index=offset // PROPOSAL_BATCH_SIZE + 1,
                status="completed",
                input_hash=input_hash.root,
                output_hash=output_hash.root,
                claim_count=len(batch.claims),
            )
            expected_need_ids = {StableId(str(item["need_id"])) for item in need_batch}
            claimed_need_ids = {item.need_id for item in batch.claims}
            insufficient_need_ids = set(batch.insufficient_need_ids)
            accounted_need_ids = claimed_need_ids | insufficient_need_ids
            if (
                accounted_need_ids != expected_need_ids
                or insufficient_need_ids - expected_need_ids
                or claimed_need_ids & insufficient_need_ids
                or len(insufficient_need_ids) != len(batch.insufficient_need_ids)
            ):
                self.last_diagnostic_codes = tuple(
                    dict.fromkeys(
                        (
                            *self.last_diagnostic_codes,
                            "SEMANTIC_SUPPORT_INCOMPLETE_NEED_COVERAGE",
                        )
                    )
                )
                continue
            proposal_audits.extend(
                _SemanticProposalAudit(
                    draft=draft,
                    call=call_object,
                    input_hash=input_hash,
                    output_hash=output_hash,
                    input_ref=input_ref,
                    output_ref=output_ref,
                )
                for draft in batch.claims
            )
        verification_items: list[dict[str, object]] = []
        normalized_drafts: dict[int, SemanticSupportClaimDraft] = {}
        for claim_index, proposal in enumerate(proposal_audits):
            draft = proposal.draft
            need = need_by_id[draft.need_id]
            allowed_facets = {facet.need_facet_id for facet in need.need_facets}
            facet_ids = tuple(dict.fromkeys(draft.need_facet_ids))
            unit_ids = tuple(dict.fromkeys(draft.retrieval_unit_ids))
            bound_facet_ids = tuple(item for item in facet_ids if item in allowed_facets)
            if len(bound_facet_ids) != len(facet_ids):
                self.last_diagnostic_codes = tuple(
                    dict.fromkeys(
                        (
                            *self.last_diagnostic_codes,
                            "SEMANTIC_SUPPORT_NORMALIZED_UNKNOWN_FACET_IDS",
                        )
                    )
                )
            if (
                not bound_facet_ids
                or not unit_ids
                or not set(unit_ids).issubset(allowed_units_by_need.get(need.need_id, set()))
            ):
                continue
            normalized_draft = draft.model_copy(update={"need_facet_ids": bound_facet_ids})
            normalized_drafts[claim_index] = normalized_draft
            allowed_units = tuple(
                unit
                for unit in unit_by_id.values()
                if unit.unit_id in allowed_units_by_need.get(need.need_id, set())
            )
            cited_ids = set(unit_ids)
            context_units = tuple(
                dict.fromkeys(
                    (
                        *tuple(unit for unit in allowed_units if unit.unit_id in cited_ids),
                        *tuple(unit for unit in allowed_units if unit.unit_id not in cited_ids),
                    )
                )
            )[:SEMANTIC_SUPPORT_VERIFIER_CONTEXT_UNIT_LIMIT]
            verification_items.append(
                {
                    "claim_index": claim_index,
                    "need_id": need.need_id.root,
                    "claim_text": normalized_draft.claim_text,
                    "need_facets": [
                        {
                            "need_facet_id": facet.need_facet_id.root,
                            "facet_kind": facet.facet_kind.value,
                            "expected_claim_scope": facet.expected_claim_scope.value,
                        }
                        for facet in need.need_facets
                        if facet.need_facet_id in bound_facet_ids
                    ],
                    "evidence_units": [
                        {
                            "retrieval_unit_id": unit_id.root,
                            "text": self._semantic_excerpt(need, unit_by_id[unit_id]),
                        }
                        for unit_id in unit_ids
                    ],
                    "context_units": [
                        {
                            "retrieval_unit_id": unit.unit_id.root,
                            "cited_in_claim": unit.unit_id in cited_ids,
                            "text": self._semantic_excerpt(need, unit),
                        }
                        for unit in context_units
                    ],
                }
            )
        if not verification_items:
            return (), (), (), ()
        verification_audits: dict[int, _SemanticVerificationAudit] = {}
        verification_batches: list[list[dict[str, object]]] = []
        pending_batch: list[dict[str, object]] = []
        pending_context_units = 0
        for item in verification_items:
            context_unit_count = len(cast(list[object], item["context_units"]))
            if pending_batch and pending_context_units + context_unit_count > (
                SEMANTIC_SUPPORT_VERIFIER_BATCH_CONTEXT_UNIT_BUDGET
            ):
                verification_batches.append(pending_batch)
                pending_batch = []
                pending_context_units = 0
            pending_batch.append(item)
            pending_context_units += context_unit_count
        if pending_batch:
            verification_batches.append(pending_batch)
        for batch_index, verification_batch_items in enumerate(verification_batches):
            batch_indexes = {cast(int, item["claim_index"]) for item in verification_batch_items}
            verifier_input = {
                "task": task.model_dump(mode="json"),
                "basis_commit_id": basis_commit_id.root,
                "basis_snapshot_id": basis_snapshot_id.root,
                "claim_proposal_hashes": sorted(
                    {proposal_audits[index].output_hash.root for index in batch_indexes}
                ),
                "claims": verification_batch_items,
            }
            verifier_input_bytes = canonical_json_bytes(verifier_input)
            verifier_prompt = (
                "You are an independent pre-freeze semantic support verifier. You did not "
                "write the candidate claims. Judge each claim only against its supplied "
                "cutoff-safe evidence units and public Need facets. supports=true only when "
                "all material clauses, quantities, negation, epistemic scope, causality, "
                "limitations, unresolved status, and relationship direction are directly "
                "entailed. Treat facet kinds as questions to resolve, not asserted values. "
                "Reject a claim that calls a matter unresolved merely because its facet kind "
                "is unresolved_status, or that lets an earlier plan, wish, or promise override "
                "a supplied observed/current state establishing fulfillment or a current "
                "relationship. Plausibility or partial support is false. context_units lists "
                "every cutoff-safe unit for the same Need that the claim may legally cite, "
                "including units the claim did not cite. If supplied evidence or any context "
                "unit contradicts a claim, set supports=false and copy the contradicting unit "
                "IDs into counter_evidence_retrieval_unit_ids. Those IDs must be copied "
                "verbatim from context_units. Return exactly one decision for "
                "every claim_index, without adding or omitting indexes.\n"
                '<PUBLIC_SUPPORT_VERIFICATION_INPUT trusted="false">\n'
                + verifier_input_bytes.decode("utf-8")
                + "\n</PUBLIC_SUPPORT_VERIFICATION_INPUT>"
            )
            verifier_prompt_bytes = verifier_prompt.encode("utf-8")
            verifier_input_hash = sha256_id(verifier_prompt_bytes)
            verification_cached = self._verification_cache.get(verifier_input_hash.root)
            if verification_cached is None:
                suffix = verifier_input_hash.root.removeprefix("sha256:")[:24]
                verifier_request = ModelRequest(
                    request_id=StableId(f"support-verification.{suffix}"),
                    run_id=needs[0].run_id,
                    task_id=needs[0].task_id,
                    model_role=ModelRole.BATCH_TEST,
                    purpose=ModelCallPurpose.BATCH_TEST,
                    trace_id=(f"stage2m-support-verification:{task.task_id.root}:{suffix}"),
                    prompt=verifier_prompt,
                    max_output_tokens=SEMANTIC_SUPPORT_VERIFICATION_MAX_OUTPUT_TOKENS,
                    timeout_seconds=SEMANTIC_SUPPORT_VERIFICATION_TIMEOUT_SECONDS,
                )
                try:
                    verification, verification_call = asyncio.run(
                        self._semantic_gateway.generate_structured(
                            verifier_request,
                            SemanticSupportVerificationBatch,
                        )
                    )
                except Exception as error:
                    self._record_progress(
                        stage="verification",
                        batch_index=batch_index + 1,
                        status="failed",
                        error_type=type(error).__name__,
                    )
                    self.last_diagnostic_codes = tuple(
                        dict.fromkeys(
                            (
                                *self.last_diagnostic_codes,
                                f"SEMANTIC_SUPPORT_VERIFIER_{type(error).__name__.upper()}",
                            )
                        )
                    )
                    continue
                raw_verifier_output = self._semantic_gateway.raw_responses.get(
                    verifier_request.request_id.root
                )
                if raw_verifier_output is None:
                    self.last_diagnostic_codes = tuple(
                        dict.fromkeys(
                            (
                                *self.last_diagnostic_codes,
                                "SEMANTIC_SUPPORT_VERIFIER_RAW_OUTPUT_MISSING",
                            )
                        )
                    )
                    continue
                raw_verifier_output_bytes = raw_verifier_output.encode("utf-8")
                verifier_output_hash = sha256_id(raw_verifier_output_bytes)
                verifier_input_ref = self._retain_bytes(
                    verifier_prompt_bytes,
                    "application/vnd.novel-agent.support-verification-prompt+text",
                )
                verifier_output_ref = self._retain_bytes(
                    raw_verifier_output_bytes,
                    "application/vnd.novel-agent.support-verification-output+json",
                )
                verification_cached = (
                    verification,
                    verification_call,
                    verifier_input_hash,
                    verifier_output_hash,
                    verifier_input_ref,
                    verifier_output_ref,
                )
                self._verification_cache[verifier_input_hash.root] = verification_cached
            (
                verification,
                verification_call,
                verifier_input_hash,
                verifier_output_hash,
                verifier_input_ref,
                verifier_output_ref,
            ) = verification_cached
            self._record_progress(
                stage="verification",
                batch_index=batch_index + 1,
                status="completed",
                input_hash=verifier_input_hash.root,
                output_hash=verifier_output_hash.root,
                decision_count=len(verification.decisions),
            )
            decisions_by_index: dict[int, SemanticSupportDecision] = {}
            invalid_decisions = False
            for decision in verification.decisions:
                if (
                    decision.claim_index not in batch_indexes
                    or decision.claim_index in decisions_by_index
                ):
                    invalid_decisions = True
                    continue
                draft = proposal_audits[decision.claim_index].draft
                need = need_by_id[draft.need_id]
                allowed_counter_ids = allowed_units_by_need.get(need.need_id, set())
                if not set(decision.counter_evidence_retrieval_unit_ids).issubset(
                    allowed_counter_ids
                ):
                    invalid_decisions = True
                    continue
                decisions_by_index[decision.claim_index] = decision
            if invalid_decisions or set(decisions_by_index) != batch_indexes:
                self.last_diagnostic_codes = tuple(
                    dict.fromkeys(
                        (
                            *self.last_diagnostic_codes,
                            "SEMANTIC_SUPPORT_VERIFIER_INCOMPLETE_DECISIONS",
                        )
                    )
                )
                continue
            verification_audits.update(
                {
                    index: _SemanticVerificationAudit(
                        decision=decision,
                        call=verification_call,
                        input_hash=verifier_input_hash,
                        output_hash=verifier_output_hash,
                        input_ref=verifier_input_ref,
                        output_ref=verifier_output_ref,
                    )
                    for index, decision in decisions_by_index.items()
                }
            )

        groups: list[ClaimSupportGroup] = []
        variants: list[ClaimVariant] = []
        receipts: list[ClaimSupportReceipt] = []
        attestations: list[CutoffAttestation] = []
        seen: set[tuple[StableId, str, tuple[StableId, ...]]] = set()
        for claim_index, proposal_audit in enumerate(proposal_audits):
            final_draft = normalized_drafts.get(claim_index)
            if final_draft is None:
                continue
            verification_audit = verification_audits.get(claim_index)
            if verification_audit is None or not verification_audit.decision.supports:
                continue
            if verification_audit.decision.counter_evidence_retrieval_unit_ids:
                self.last_diagnostic_codes = tuple(
                    dict.fromkeys(
                        (
                            *self.last_diagnostic_codes,
                            "SEMANTIC_SUPPORT_COUNTER_EVIDENCE_REJECTED",
                        )
                    )
                )
                continue
            need = need_by_id[final_draft.need_id]
            facet_ids = tuple(dict.fromkeys(final_draft.need_facet_ids))
            unit_ids = tuple(dict.fromkeys(final_draft.retrieval_unit_ids))
            claim_text = self._clean_claim(final_draft.claim_text)
            identity_key = (need.need_id, claim_text, unit_ids)
            if not claim_text or identity_key in seen:
                continue
            seen.add(identity_key)
            selected_units = tuple(unit_by_id[unit_id] for unit_id in unit_ids)
            evidence_refs = tuple(
                dict.fromkeys(
                    evidence for unit in selected_units for evidence in unit.evidence_refs
                )
            )
            plan_node_ids = tuple(
                dict.fromkeys(
                    node_id for unit in selected_units for node_id in self._plan_node_ids(unit)
                )
            )
            resolution = (
                EvidenceResolutionStatus.RESOLVED
                if all(
                    self._resolution_status(
                        unit.evidence_refs,
                        unit,
                        basis_commit_id=basis_commit_id,
                        checkpoint_chapter=task.checkpoint_chapter,
                        plan_node_ids=self._plan_node_ids(unit),
                    )
                    is EvidenceResolutionStatus.RESOLVED
                    for unit in selected_units
                )
                else EvidenceResolutionStatus.UNRESOLVED
            )
            if resolution is not EvidenceResolutionStatus.RESOLVED or (
                not evidence_refs and not plan_node_ids
            ):
                continue
            identity = canonical_json_bytes(
                {
                    "need_id": need.need_id.root,
                    "facets": [item.root for item in facet_ids],
                    "units": [item.root for item in unit_ids],
                    "claim": claim_text,
                    "producer_input_hash": proposal_audit.input_hash.root,
                    "producer_output_hash": proposal_audit.output_hash.root,
                    "verifier_input_hash": verification_audit.input_hash.root,
                    "verifier_output_hash": verification_audit.output_hash.root,
                }
            )
            digest = sha256_id(identity).root.removeprefix("sha256:")
            claim_id = StableId(f"claim.{digest[:48]}")
            group_id = StableId(f"support-group.{digest[:48]}")
            information_scope = (
                "author_planning"
                if any(unit.access_scope == "author_planning" for unit in selected_units)
                else "writer_safe"
            )
            attestation = CutoffAttestation(
                attestation_id=StableId(f"cutoff-attestation.{digest[:48]}"),
                basis_commit_id=basis_commit_id,
                basis_snapshot_id=basis_snapshot_id,
                checkpoint_chapter=task.checkpoint_chapter,
                information_scope=information_scope,
                retrieval_unit_ids=unit_ids,
                producer=self.version,
                producer_version=self.version,
            )
            attestation_ref = self._artifact_ref(
                attestation,
                "application/vnd.novel-agent.cutoff-attestation+json",
            )
            receipt = ClaimSupportReceipt(
                receipt_id=StableId(f"support-receipt.{digest[:48]}"),
                support_group_id=group_id,
                claim_id=claim_id,
                claim_text_hash=sha256_id(claim_text.encode("utf-8")),
                need_ids=(need.need_id,),
                need_facet_ids=facet_ids,
                retrieval_unit_ids=unit_ids,
                evidence_refs=evidence_refs,
                plan_node_ids=plan_node_ids,
                evidence_resolution_status=resolution,
                semantic_support_status=SemanticSupportStatus.VERIFIED,
                basis_commit_id=basis_commit_id,
                basis_snapshot_id=basis_snapshot_id,
                cutoff_attestation_ref=attestation_ref,
                information_scope=information_scope,
                producer=self.version,
                producer_version=self.version,
                producer_input_hash=proposal_audit.input_hash,
                producer_output_hash=proposal_audit.output_hash,
                producer_input_ref=proposal_audit.input_ref,
                producer_output_ref=proposal_audit.output_ref,
                model_call_record=proposal_audit.call,
                verifier_input_hash=verification_audit.input_hash,
                verifier_output_hash=verification_audit.output_hash,
                verifier_input_ref=verification_audit.input_ref,
                verifier_output_ref=verification_audit.output_ref,
                verification_model_call_record=verification_audit.call,
            )
            receipt_ref = self._artifact_ref(
                receipt,
                "application/vnd.novel-agent.claim-support-receipt+json",
            )
            groups.append(
                ClaimSupportGroup(
                    support_group_id=group_id,
                    claim_id=claim_id,
                    need_ids=(need.need_id,),
                    need_facet_ids=facet_ids,
                    retrieval_unit_ids=unit_ids,
                    evidence_refs=evidence_refs,
                    plan_node_ids=plan_node_ids,
                    evidence_resolution_status=resolution,
                    semantic_support_status=SemanticSupportStatus.VERIFIED,
                    support_receipt_ref=receipt_ref,
                    producer=self.version,
                    producer_version=self.version,
                    cutoff_attestation_ref=attestation_ref,
                )
            )
            variants.append(
                ClaimVariant(
                    claim_variant_id=StableId(f"claim-variant.{digest[:48]}"),
                    claim_id=claim_id,
                    support_group_id=group_id,
                    claim_text=claim_text,
                    claim_text_hash=receipt.claim_text_hash,
                    covered_need_facet_ids=facet_ids,
                    support_receipt_ref=receipt_ref,
                    token_cost=max(1, token_counter(claim_text)),
                    reduction_level=ClaimReductionLevel.FULL,
                    producer=self.version,
                    producer_version=self.version,
                )
            )
            receipts.append(receipt)
            attestations.append(attestation)
        return tuple(groups), tuple(variants), tuple(receipts), tuple(attestations)

    @staticmethod
    def _chapter_index(unit: RetrievalUnit) -> int | None:
        if not unit.evidence_refs:
            return None
        chapter_id = unit.evidence_refs[0].chapter_id
        if chapter_id is None:
            return None
        suffix = chapter_id.root.rsplit(".", 1)[-1]
        return int(suffix) if suffix.isdecimal() else None

    @classmethod
    def _semantic_excerpt(cls, need: Stage1MemoryNeed, unit: RetrievalUnit) -> str:
        historical = need.need_type in {
            "causal_history",
            "long_range_callback",
            "plan_conditioned_history",
        } or any(
            facet.facet_kind is NeedFacetKind.CAUSAL_HISTORY
            or getattr(facet.expected_claim_scope, "value", facet.expected_claim_scope)
            == "historical"
            for facet in need.need_facets
        )
        excerpt_limit = SEMANTIC_SUPPORT_HISTORICAL_EXCERPT_LIMIT if historical else 600
        if len(unit.text) <= excerpt_limit:
            return unit.text
        terms = cls._query_terms(need.query_text)
        historical_action_terms = (
            "因为",
            "所以",
            "因此",
            "于是",
            "才",
            "不得不",
            "挡",
            "拦",
            "保护",
            "救",
            "杀",
            "死",
            "袭",
            "面对",
            "决定",
            "答应",
            "拒绝",
            "进入",
            "成为",
            "带走",
            "发生",
            "随后",
        )
        sentences = tuple(
            item.strip()
            for item in re.split(r"(?<=[\u3002\uff01\uff1f!?;\uff1b])|\n+", unit.text)
            if item.strip()
        )
        ranked = sorted(
            enumerate(sentences),
            key=lambda item: (
                -sum(term in item[1].casefold() for term in terms),
                -(sum(term in item[1] for term in historical_action_terms) if historical else 0),
                item[0],
            ),
        )
        sentence_limit = 10 if historical else 4
        selected_indexes = sorted(index for index, _text in ranked[:sentence_limit])
        excerpt = " ".join(sentences[index] for index in selected_indexes)
        return excerpt[:excerpt_limit]

    def _coalesce(
        self,
        groups: tuple[ClaimSupportGroup, ...],
        variants: tuple[ClaimVariant, ...],
        receipts: tuple[ClaimSupportReceipt, ...],
        attestations: tuple[CutoffAttestation, ...],
        *,
        need_by_id: dict[StableId, Stage1MemoryNeed],
    ) -> tuple[
        tuple[ClaimSupportGroup, ...],
        tuple[ClaimVariant, ...],
        tuple[ClaimSupportReceipt, ...],
        tuple[CutoffAttestation, ...],
    ]:
        """Merge identical supported claims without losing Need/facet edges."""

        variant_by_group = {item.support_group_id: item for item in variants}
        receipt_by_group = {item.support_group_id: item for item in receipts}
        attestation_by_ref = {
            self._artifact_ref(
                item,
                "application/vnd.novel-agent.cutoff-attestation+json",
            ).artifact_id: item
            for item in attestations
        }
        buckets: dict[tuple[object, ...], list[ClaimSupportGroup]] = {}
        for group in groups:
            variant = variant_by_group[group.support_group_id]
            sections = tuple(
                sorted(
                    {
                        (
                            need_by_id[need_id].expected_section
                            or WriterContextSection.CONTINUITY_CONSTRAINTS
                        ).value
                        for need_id in group.need_ids
                    }
                )
            )
            key = (
                variant.claim_text_hash,
                group.retrieval_unit_ids,
                group.evidence_refs,
                group.plan_node_ids,
                sections,
            )
            buckets.setdefault(key, []).append(group)
        merged_groups: list[ClaimSupportGroup] = []
        merged_variants: list[ClaimVariant] = []
        merged_receipts: list[ClaimSupportReceipt] = []
        merged_attestations: dict[StableId, CutoffAttestation] = {}
        for bucket_groups in buckets.values():
            first = bucket_groups[0]
            first_variant = variant_by_group[first.support_group_id]
            first_receipt = receipt_by_group[first.support_group_id]
            need_ids = tuple(
                dict.fromkeys(need_id for group in bucket_groups for need_id in group.need_ids)
            )
            facet_ids = tuple(
                dict.fromkeys(
                    facet_id for group in bucket_groups for facet_id in group.need_facet_ids
                )
            )
            digest = sha256_id(
                canonical_json_bytes(
                    {
                        "claim": first_variant.claim_text,
                        "needs": [item.root for item in need_ids],
                        "facets": [item.root for item in facet_ids],
                        "units": [item.root for item in first.retrieval_unit_ids],
                        "evidence": [item.model_dump(mode="json") for item in first.evidence_refs],
                        "plan_nodes": [item.root for item in first.plan_node_ids],
                    }
                )
            ).root.removeprefix("sha256:")
            claim_id = StableId(f"claim.{digest[:48]}")
            group_id = StableId(f"support-group.{digest[:48]}")
            attestation = attestation_by_ref[first.cutoff_attestation_ref.artifact_id]
            attestation_ref = self._artifact_ref(
                attestation,
                "application/vnd.novel-agent.cutoff-attestation+json",
            )
            receipt = first_receipt.model_copy(
                update={
                    "receipt_id": StableId(f"support-receipt.{digest[:48]}"),
                    "support_group_id": group_id,
                    "claim_id": claim_id,
                    "need_ids": need_ids,
                    "need_facet_ids": facet_ids,
                    "cutoff_attestation_ref": attestation_ref,
                }
            )
            receipt_ref = self._artifact_ref(
                receipt,
                "application/vnd.novel-agent.claim-support-receipt+json",
            )
            merged_groups.append(
                first.model_copy(
                    update={
                        "support_group_id": group_id,
                        "claim_id": claim_id,
                        "need_ids": need_ids,
                        "need_facet_ids": facet_ids,
                        "support_receipt_ref": receipt_ref,
                        "cutoff_attestation_ref": attestation_ref,
                    }
                )
            )
            merged_variants.append(
                first_variant.model_copy(
                    update={
                        "claim_variant_id": StableId(f"claim-variant.{digest[:48]}"),
                        "claim_id": claim_id,
                        "support_group_id": group_id,
                        "covered_need_facet_ids": facet_ids,
                        "support_receipt_ref": receipt_ref,
                    }
                )
            )
            merged_receipts.append(receipt)
            merged_attestations[attestation.attestation_id] = attestation
        return (
            tuple(merged_groups),
            tuple(merged_variants),
            tuple(merged_receipts),
            tuple(merged_attestations.values()),
        )

    @staticmethod
    def _legal_for_need(
        task: BenchmarkTaskContract,
        need: Stage1MemoryNeed,
        unit: RetrievalUnit,
    ) -> bool:
        plan_information = TrustedClaimSupportProducer._is_plan_information(unit)
        if plan_information and (
            task.information_profile is BenchmarkInformationProfile.VISIBLE_AT_CUTOFF
            or not need.allow_plan
        ):
            return False
        visible_scopes = {
            "writer_safe": frozenset({"writer_safe"}),
            "author_planning": frozenset({"writer_safe", "author_planning"}),
            "evaluator": frozenset({"writer_safe", "author_planning", "evaluator"}),
        }.get(need.access_scope)
        if visible_scopes is None:
            return False
        return unit.access_scope in visible_scopes

    def _claim_candidates(
        self,
        unit: RetrievalUnit,
        need: Stage1MemoryNeed,
    ) -> tuple[tuple[str, tuple[EvidenceRef, ...]], ...]:
        if unit.unit_kind not in {
            RetrievalUnitKind.GROUNDED_BLOCK,
            RetrievalUnitKind.GROUNDED_SPAN,
        }:
            claim = self._clean_claim(unit.text)
            return ((claim, unit.evidence_refs),) if claim else ()
        if not unit.evidence_refs or unit.evidence_refs[0].span is None:
            return ()
        terms = self._query_terms(need.query_text)
        sentences: list[tuple[int, int, str]] = []
        sentence_pattern = r"[^\u3002\uff01\uff1f!?;\uff1b\n]+[\u3002\uff01\uff1f!?;\uff1b]?"
        for match in re.finditer(sentence_pattern, unit.text):
            text = match.group().strip()
            if not text:
                continue
            leading = len(match.group()) - len(match.group().lstrip())
            start = match.start() + leading
            sentences.append((start, start + len(text), text))
        lexical_scores = [
            (
                sum(term in text.casefold() for term in terms),
                index,
            )
            for index, (_start, _end, text) in enumerate(sentences)
        ]
        eligible = sorted(
            lexical_scores,
            key=lambda item: (-item[0], sentences[item[1]][0]),
        )
        lexical_matches = [item for item in eligible if item[0] > 0]
        chosen = (lexical_matches or eligible)[:3]
        base = unit.evidence_refs[0]
        assert base.span is not None
        claims: list[tuple[str, tuple[EvidenceRef, ...]]] = []
        for claim_index, (_score, sentence_index) in enumerate(chosen, start=1):
            start, _end, text = sentences[sentence_index]
            clipped = text[:240].rstrip()
            reference = base.model_copy(
                update={
                    "evidence_id": StableId(
                        "evidence.support."
                        + quote_hash(clipped).root.removeprefix("sha256:")[:32]
                        + f".{claim_index}"
                    ),
                    "quote_hash": quote_hash(clipped),
                    "span": base.span.model_copy(
                        update={
                            "start": base.span.start + start,
                            "end": base.span.start + start + len(clipped),
                        }
                    ),
                }
            )
            claims.append((clipped, (reference,)))
        return tuple(claims)

    @classmethod
    def _supported_facets(
        cls,
        need: Stage1MemoryNeed,
        unit: RetrievalUnit,
        claim: str,
    ) -> tuple[NeedFacet, ...]:
        folded = claim.casefold()
        query_terms = cls._query_terms(need.query_text)
        relevance = sum(term in folded for term in query_terms)
        grounded_historical = (
            bool(unit.evidence_refs)
            and unit.unit_kind
            in {
                RetrievalUnitKind.GROUNDED_BLOCK,
                RetrievalUnitKind.GROUNDED_SPAN,
            }
            and (
                need.query_intent.value in {"semantic_history", "related_event"}
                or need.need_type
                in {"causal_history", "long_range_callback", "plan_conditioned_history"}
                or any(
                    facet.facet_kind in {NeedFacetKind.CAUSAL_HISTORY, NeedFacetKind.SETUP}
                    for facet in need.need_facets
                )
            )
        )
        if (
            relevance == 0
            and not set(need.entity_ids).intersection(unit.entity_ids)
            and not grounded_historical
        ):
            return ()
        historical = unit.unit_kind in {
            RetrievalUnitKind.EVENT_ANCHOR,
            RetrievalUnitKind.SCENE_ANCHOR,
            RetrievalUnitKind.CHAPTER_ANCHOR,
            RetrievalUnitKind.GROUNDED_BLOCK,
            RetrievalUnitKind.GROUNDED_SPAN,
        }
        plan = cls._is_plan_information(unit)
        limitation_terms = ("cannot", "unable", "limit", "cost", "限制", "无法", "不能", "代价")
        unresolved_terms = (
            "unresolved",
            "pending",
            "remain",
            "尚",
            "未",
            "仍",
            "等待",
            "承诺",
            "promise",
            "oath",
            "誓",
            "义务",
            "必须",
            "需要",
        )
        supported: list[NeedFacet] = []
        for facet in need.need_facets:
            kind = facet.facet_kind
            matches = (
                (
                    kind
                    in {
                        NeedFacetKind.CURRENT_STATE,
                        NeedFacetKind.RELATION_STATE,
                        NeedFacetKind.CAPABILITY_STATUS,
                        NeedFacetKind.KNOWLEDGE_BOUNDARY,
                    }
                    and not historical
                    and not plan
                )
                or (
                    kind is NeedFacetKind.LIMITATION
                    and any(term in folded for term in limitation_terms)
                )
                or (
                    kind
                    in {
                        NeedFacetKind.CAUSAL_HISTORY,
                        NeedFacetKind.SETUP,
                    }
                    and historical
                )
                or (
                    kind in {NeedFacetKind.UNRESOLVED_STATUS, NeedFacetKind.COMMITMENT}
                    and (
                        any(term in folded for term in unresolved_terms)
                        or (need.need_type == "unresolved_obligation" and relevance > 0)
                    )
                )
                or (kind is NeedFacetKind.PLAN_NODE and plan)
            )
            if matches:
                supported.append(facet)
        return tuple(supported)

    @staticmethod
    def _resolution_status(
        evidence_refs: tuple[EvidenceRef, ...],
        unit: RetrievalUnit,
        *,
        basis_commit_id: CommitId,
        checkpoint_chapter: int,
        plan_node_ids: tuple[StableId, ...],
    ) -> EvidenceResolutionStatus:
        if plan_node_ids:
            return (
                EvidenceResolutionStatus.RESOLVED
                if unit.source_commit == basis_commit_id
                else EvidenceResolutionStatus.BASIS_MISMATCH
            )
        if not evidence_refs:
            return EvidenceResolutionStatus.UNRESOLVED
        # The RetrievalUnit is projected from the exact current snapshot and
        # therefore binds the evaluation basis.  EvidenceRef.resolved_at_commit
        # intentionally preserves the historical commit at which that text was
        # resolved; requiring it to equal the current checkpoint would reject
        # every still-valid long-range source.
        if unit.source_commit != basis_commit_id:
            return EvidenceResolutionStatus.BASIS_MISMATCH
        chapters = [
            chapter
            for item in evidence_refs
            if item.chapter_id is not None
            for chapter in (TrustedClaimSupportProducer._chapter_number(item.chapter_id),)
            if chapter is not None
        ]
        if chapters and max(chapters) > checkpoint_chapter:
            return EvidenceResolutionStatus.CUTOFF_VIOLATION
        return EvidenceResolutionStatus.RESOLVED

    @staticmethod
    def _chapter_number(chapter_id: StableId) -> int | None:
        if "prelude" in chapter_id.root.casefold():
            return 0
        match = re.search(r"(?:^|[._:-])(\d+)$", chapter_id.root)
        return int(match.group(1)) if match is not None else None

    @staticmethod
    def _plan_node_ids(unit: RetrievalUnit) -> tuple[StableId, ...]:
        if not TrustedClaimSupportProducer._is_plan_information(unit):
            return ()
        return (StableId(unit.unit_id.root.removeprefix("anchor.")),)

    @staticmethod
    def _is_plan_information(unit: RetrievalUnit) -> bool:
        """Recognize real labeled Plan and legacy evidence-free scripted anchors."""

        return unit.information_label == "plan" or (
            unit.unit_kind
            in {
                RetrievalUnitKind.PLAN_ANCHOR,
                RetrievalUnitKind.ARC_ANCHOR,
            }
            and not unit.evidence_refs
        )

    @staticmethod
    def _clean_claim(value: str) -> str:
        first = next((line for line in value.splitlines() if line.strip()), value)
        return re.sub(r"\s+", " ", first).strip()

    @staticmethod
    def _query_terms(value: str) -> tuple[str, ...]:
        tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", value.casefold())
        stopwords = {
            "a",
            "an",
            "and",
            "as",
            "at",
            "be",
            "by",
            "for",
            "from",
            "in",
            "is",
            "it",
            "of",
            "on",
            "or",
            "the",
            "to",
            "use",
            "with",
        }
        terms: list[str] = []
        for token in tokens:
            if len(token) < 2 or token in stopwords:
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 2:
                terms.extend(token[index : index + 2] for index in range(len(token) - 1))
            else:
                terms.append(token)
        return tuple(dict.fromkeys(terms))

    @staticmethod
    def _artifact_ref(model: DomainModel, media_type: str) -> ArtifactRef:
        payload = canonical_json_bytes(model.model_dump(mode="json"))
        return ArtifactRef(
            artifact_id=sha256_id(payload),
            media_type=media_type,
            byte_length=len(payload),
            schema_version=SchemaVersion("1.0.0"),
        )

    def _retain_bytes(self, payload: bytes, media_type: str) -> ArtifactRef:
        if self._artifact_writer is not None:
            return self._artifact_writer(payload, media_type)
        return ArtifactRef(
            artifact_id=sha256_id(payload),
            media_type=media_type,
            byte_length=len(payload),
            schema_version=SchemaVersion("1.0.0"),
        )

    def _record_progress(self, **event: object) -> None:
        if self._progress_writer is not None:
            self._progress_writer(event)


class ControllerSupportSelector:
    """Select receipt-bound support groups before deterministic assembly."""

    version = "controller_support_selector.v3"

    def __init__(self, producer: TrustedClaimSupportProducer | None = None) -> None:
        self._producer = producer or TrustedClaimSupportProducer()
        self._completion = NeedCompletionEvaluator()

    def select(
        self,
        *,
        task: BenchmarkTaskContract,
        units: tuple[RetrievalUnit, ...],
        needs: tuple[Stage1MemoryNeed, ...],
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        unit_need_ids: Mapping[StableId, tuple[StableId, ...]],
        writer_token_budget: int,
        evidence_ledger_token_budget: int,
        token_counter: TokenCounter,
    ) -> SupportSelectionResult:
        groups, variants, receipts, attestations = self._producer.produce(
            task=task,
            units=units,
            needs=needs,
            basis_commit_id=basis_commit_id,
            basis_snapshot_id=basis_snapshot_id,
            unit_need_ids=unit_need_ids,
            token_counter=token_counter,
        )
        variant_by_group = {item.support_group_id: item for item in variants}
        need_by_id = {item.need_id: item for item in needs}
        semantic_group_ids = {
            item.support_group_id for item in receipts if item.model_call_record is not None
        }
        verified = tuple(
            group
            for group in groups
            if group.evidence_resolution_status is EvidenceResolutionStatus.RESOLVED
            and group.semantic_support_status is SemanticSupportStatus.VERIFIED
            and not group.counter_evidence_refs
        )
        selected: list[ClaimSupportGroup] = []
        mandatory_group_ids: set[StableId] = set()
        closed_facets: set[StableId] = set()
        completion_results: list[NeedCompletionResult] = []
        diagnostics: list[str] = list(self._producer.last_diagnostic_codes)
        for need in sorted(
            needs,
            key=lambda item: (
                0 if item.requirement is RequirementLevel.MANDATORY else 1,
                -item.priority,
                item.need_id.root,
            ),
        ):
            if need.completion_spec is None:
                continue
            candidates = sorted(
                (
                    group
                    for group in verified
                    if need.need_id in group.need_ids and set(group.need_facet_ids) - closed_facets
                ),
                key=lambda group: (
                    -len(set(group.need_facet_ids) - closed_facets),
                    0 if group.support_group_id in semantic_group_ids else 1,
                    -self._relevance(
                        variant_by_group[group.support_group_id].claim_text,
                        need,
                    ),
                    variant_by_group[group.support_group_id].token_cost,
                    group.support_group_id.root,
                ),
            )
            state_groups: list[ClaimSupportGroup] = []
            result = self._completion_for(need, ())
            for group in candidates:
                trial_groups = (*state_groups, group)
                trial = self._completion_for(need, trial_groups)
                if (
                    trial.closed_need_facet_ids == result.closed_need_facet_ids
                    and trial.diagnostic_codes == result.diagnostic_codes
                ):
                    continue
                state_groups.append(group)
                result = trial
                if group not in selected:
                    selected.append(group)
                if need.requirement is RequirementLevel.MANDATORY:
                    mandatory_group_ids.add(group.support_group_id)
                if result.status is NeedCompletionStatus.REQUIRED_FACETS_CLOSED:
                    break
            completion_results.append(result)
            if result.status is NeedCompletionStatus.REQUIRED_FACETS_CLOSED:
                closed_facets.update(result.closed_need_facet_ids)
            if (
                need.requirement is RequirementLevel.MANDATORY
                and result.status is not NeedCompletionStatus.REQUIRED_FACETS_CLOSED
            ):
                missing = (
                    result.missing_required_need_facet_ids
                    or need.completion_spec.required_need_facet_ids
                )
                diagnostics.extend(f"MANDATORY_FACET_UNCLOSED:{item.root}" for item in missing)

        # Optional groups are ordered by public marginal facet gain and stable cost.
        optional_groups = sorted(
            (group for group in verified if group not in selected),
            key=lambda group: (
                -len(set(group.need_facet_ids) - closed_facets),
                0 if group.support_group_id in semantic_group_ids else 1,
                -max(
                    (
                        need_by_id[need_id].priority
                        for need_id in group.need_ids
                        if need_id in need_by_id
                    ),
                    default=0,
                ),
                -max(
                    (
                        self._relevance(
                            variant_by_group[group.support_group_id].claim_text,
                            need_by_id[need_id],
                        )
                        for need_id in group.need_ids
                        if need_id in need_by_id
                    ),
                    default=0,
                ),
                variant_by_group[group.support_group_id].token_cost,
                group.support_group_id.root,
            ),
        )
        estimated_writer_tokens = sum(
            variant_by_group[group.support_group_id].token_cost for group in selected
        )
        estimated_ledger_tokens = sum(
            token_counter(
                " ".join(
                    (
                        variant_by_group[group.support_group_id].claim_text,
                        *(item.evidence_id.root for item in group.evidence_refs),
                        *(item.root for item in group.plan_node_ids),
                    )
                )
            )
            for group in selected
        )
        packed_optional: list[ClaimSupportGroup] = []
        for group in optional_groups:
            writer_cost = variant_by_group[group.support_group_id].token_cost
            ledger_cost = token_counter(
                " ".join(
                    (
                        variant_by_group[group.support_group_id].claim_text,
                        *(item.evidence_id.root for item in group.evidence_refs),
                        *(item.root for item in group.plan_node_ids),
                    )
                )
            )
            if (
                estimated_writer_tokens + writer_cost > writer_token_budget
                or estimated_ledger_tokens + ledger_cost > evidence_ledger_token_budget
            ):
                continue
            selected.append(group)
            packed_optional.append(group)
            estimated_writer_tokens += writer_cost
            estimated_ledger_tokens += ledger_cost
        selected_optional_ids = tuple(
            group.support_group_id
            for group in selected
            if group.support_group_id not in mandatory_group_ids
        )
        selected_ids = tuple(group.support_group_id for group in selected)
        mandatory_ids = tuple(
            group.support_group_id
            for group in selected
            if group.support_group_id in mandatory_group_ids
        )
        allowed: dict[str, tuple[StableId, ...]] = {
            group.support_group_id.root: (
                variant_by_group[group.support_group_id].claim_variant_id,
            )
            for group in selected
        }
        mandatory_variants = tuple(
            variant_by_group[group_id].claim_variant_id for group_id in mandatory_ids
        )
        all_required = {
            facet_id
            for need in needs
            if need.completion_spec is not None
            for facet_id in need.completion_spec.required_need_facet_ids
        }
        spec = ContextAssemblySpec(
            selected_unit_ids=tuple(
                dict.fromkeys(unit_id for group in selected for unit_id in group.retrieval_unit_ids)
            ),
            mandatory_unit_ids=tuple(
                dict.fromkeys(
                    unit_id
                    for group in selected
                    if group.support_group_id in mandatory_group_ids
                    for unit_id in group.retrieval_unit_ids
                )
            ),
            token_budget=writer_token_budget,
            selected_support_group_ids=selected_ids,
            mandatory_support_group_ids=mandatory_ids,
            allowed_claim_variant_ids_by_support_group=allowed,
            mandatory_claim_variant_ids=mandatory_variants,
            closed_need_facet_ids=tuple(sorted(closed_facets, key=lambda item: item.root)),
            unresolved_need_facet_ids=tuple(
                sorted(all_required - closed_facets, key=lambda item: item.root)
            ),
            ordered_optional_support_group_ids=tuple(
                dict.fromkeys(
                    (*selected_optional_ids, *(group.support_group_id for group in packed_optional))
                )
            ),
            writer_token_budget=writer_token_budget,
            evidence_ledger_token_budget=evidence_ledger_token_budget,
            reduction_policy="receipt_bound_variants_only",
            selection_policy_version=self.version,
        )
        return SupportSelectionResult(
            context_assembly_spec=spec,
            support_groups=tuple(selected),
            claim_variants=tuple(variant_by_group[group.support_group_id] for group in selected),
            support_receipts=tuple(
                receipt for receipt in receipts if receipt.support_group_id in set(selected_ids)
            ),
            cutoff_attestations=tuple(
                attestation
                for attestation in attestations
                if any(
                    group.cutoff_attestation_ref
                    == TrustedClaimSupportProducer._artifact_ref(
                        attestation,
                        "application/vnd.novel-agent.cutoff-attestation+json",
                    )
                    for group in selected
                )
            ),
            completion_results=tuple(completion_results),
            diagnostic_codes=tuple(dict.fromkeys(diagnostics)),
            producer_version=self.version,
        )

    @staticmethod
    def _relevance(claim_text: str, need: Stage1MemoryNeed) -> int:
        folded = claim_text.casefold()
        return sum(
            term in folded for term in TrustedClaimSupportProducer._query_terms(need.query_text)
        )

    def _completion_for(
        self,
        need: Stage1MemoryNeed,
        groups: tuple[ClaimSupportGroup, ...],
    ) -> NeedCompletionResult:
        assert need.completion_spec is not None
        facets = tuple(
            dict.fromkeys(
                facet_id
                for group in groups
                for facet_id in group.need_facet_ids
                if facet_id in need.completion_spec.required_need_facet_ids
            )
        )
        sources = {
            facet_id.root: tuple(
                dict.fromkeys(
                    reference.evidence_id
                    for group in groups
                    if facet_id in group.need_facet_ids
                    for reference in group.evidence_refs
                )
            )
            for facet_id in facets
        }
        chapters = {
            facet_id.root: tuple(
                dict.fromkeys(
                    reference.chapter_id
                    for group in groups
                    if facet_id in group.need_facet_ids
                    for reference in group.evidence_refs
                    if reference.chapter_id is not None
                )
            )
            for facet_id in facets
        }
        plan_nodes = {
            facet_id.root: tuple(
                dict.fromkeys(
                    node
                    for group in groups
                    if facet_id in group.need_facet_ids
                    for node in group.plan_node_ids
                )
            )
            for facet_id in facets
        }
        return self._completion.evaluate(
            need.completion_spec,
            NeedFacetClosureState(
                need_id=need.need_id,
                verified_need_facet_ids=facets,
                evidence_source_ids_by_facet=sources,
                evidence_chapter_ids_by_facet=chapters,
                plan_node_ids_by_facet=plan_nodes,
                current_claim_facet_ids=tuple(
                    facet.need_facet_id
                    for facet in need.need_facets
                    if facet.need_facet_id in facets
                    and facet.expected_claim_scope.value in {"current", "knowledge"}
                ),
                causal_history_facet_ids=tuple(
                    facet.need_facet_id
                    for facet in need.need_facets
                    if facet.need_facet_id in facets
                    and (
                        facet.facet_kind is NeedFacetKind.CAUSAL_HISTORY
                        or facet.expected_claim_scope.value == "historical"
                    )
                ),
            ),
        )


__all__ = [
    "ControllerSupportSelector",
    "SupportSelectionResult",
    "TrustedClaimSupportProducer",
]
