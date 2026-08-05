"""Public, Gold-free support verification and Controller-side context selection."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import httpx
from pydantic import Field, ValidationError

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
from novel_agent.domain.text import EvidenceRef, QuoteHash, TextSpanRef
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    ClaimReductionLevel,
    ClaimSupportGroup,
    ClaimSupportReceipt,
    ClaimVariant,
    CutoffAttestation,
    EvidenceLedgerEntry,
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
# Retrieval-handle window: how many ranked units may enter one Need's semantic
# pool. This bounds the handles, not the exact slices derived from them.
SEMANTIC_SUPPORT_INPUT_LIMIT = 20
# A verifier must see every cutoff-safe exact slice exposed to the proposer
# for the same Need, plus bounded compatible counter-evidence from the same
# pool. Larger context makes the entailment decision less auditable, so the
# counter-evidence context stays bounded while cited slices are always whole.
SEMANTIC_SUPPORT_VERIFIER_CONTEXT_UNIT_LIMIT = SEMANTIC_SUPPORT_INPUT_LIMIT
# Preserve the former maximum verifier prompt scale, but batch by accumulated
# context rather than by claim count.
SEMANTIC_SUPPORT_VERIFIER_BATCH_CONTEXT_UNIT_BUDGET = 64
SEMANTIC_SUPPORT_LATE_GROUNDED_UNIT_LIMIT = 4
SEMANTIC_SUPPORT_CAUSAL_CHAPTER_WINDOW = 2
# Proposal calls occasionally spend most of a completion on model deliberation
# and reach the endpoint limit before the small structured payload is closed.
# This is a model-call ceiling only; Writer and evidence-ledger budgets remain
# enforced independently by the assembler.  The single-slice probe runs without
# thinking mode and shares the 4096 ceiling used by the multi-slice synthesis.
SEMANTIC_SUPPORT_PROPOSAL_MAX_OUTPUT_TOKENS = 4096
# A synthesized multi-slice claim may cite many exact slices; the completion
# carries a bounded thinking-mode deliberation plus the final JSON.  The
# measured synthesis closes at ~600-2000 completion tokens with a 500-token
# thinking budget, so the 4096 ceiling leaves generous headroom and completes
# well inside the ModelRequest 600-second timeout.
SEMANTIC_SUPPORT_MULTI_SLICE_PROPOSAL_MAX_OUTPUT_TOKENS = 4096
# The local Qwen3.6 endpoint enforces a hard per-request thinking token budget
# (`thinking_token_budget`): the sampler forces the close of the thinking block
# once the budget is consumed, so a multi-slice synthesis request completes
# with bounded deliberation and still closes the structured JSON within the
# output ceiling.  Measured on the live endpoint: a 170-slice synthesis with a
# 500-token budget closes in ~34s with a valid cross-slice claim.
SEMANTIC_SUPPORT_MULTI_SLICE_THINKING_TOKEN_BUDGET = 500
# The whole-claim verifier runs without thinking mode; its per-claim entailment
# decision stays small, so the ceiling covers the bounded decisions JSON.
SEMANTIC_SUPPORT_VERIFICATION_MAX_OUTPUT_TOKENS = 1024
# The local Qwen service is single-concurrency.  C60 showed that proposal
# generations can legitimately cross 120 seconds, while every verifier batch
# completed well below that ceiling.  A cancelled proposal can also remain in
# the inference server briefly and make subsequent requests queue behind it.
# Separate the two stages instead of applying the verifier's short limit to the
# more expensive proposal call.
SEMANTIC_SUPPORT_PROPOSAL_TIMEOUT_SECONDS = 300.0
# A multi-slice synthesis request carries a serialized-request-bounded chunk
# of the Need's workset plus the task contract, so its completion can take
# substantially longer than a single-slice proposal.  The domain ModelRequest
# caps timeout_seconds at 600, so the dedicated ceiling stays at that cap.
SEMANTIC_SUPPORT_MULTI_SLICE_PROPOSAL_TIMEOUT_SECONDS = 600.0
SEMANTIC_SUPPORT_VERIFICATION_TIMEOUT_SECONDS = 120.0
# Internal exact-slice read budgets, separate from the Writer 4000-token
# product budget and the Ledger 12000-token budget.  A short paragraph passes
# through unchanged; an oversized paragraph is split into contiguous sentence
# windows of at most SLICE_MAX_CHARS.  The per-Need SupportWorkset is packed
# from those exact slices until the explicit token budget is consumed; the
# same budget caps one semantic proposal request, so one request normally
# carries one Need's full workset.  The local endpoint serves 128K context, so
# the workset budget is derived from that request capacity (40K slice tokens
# plus prompt/output headroom) and is reported in the workset artifacts; it is
# not the Writer product limit.
SEMANTIC_SUPPORT_SLICE_MAX_CHARS = 300
SEMANTIC_SUPPORT_WORKSET_TOKEN_BUDGET = 40_000
# One semantic synthesis request is token-bounded to stay under the local
# endpoint's practical request ceiling.  The endpoint serves 128K context and
# accepts ~48K-token CJK prompts (re-verified 2026-08-05 on the restarted
# server), so the serialized-request budget allows one request to carry most
# of a Need's full token-bounded workset.  With the 4096-token synthesis
# ceiling (a ~500-token thinking budget plus the final JSON), 30K total leaves
# ~26K estimated input tokens, so a chunk carries ~170-250 exact slices and
# the thinking-mode completion closes within the output ceiling and the
# 600-second timeout (measured: 170 slices + 500 thinking budget closes in
# ~34s with valid JSON).
SEMANTIC_SUPPORT_SERIALIZED_REQUEST_TOKEN_BUDGET = 30_000
# The single-slice sufficiency probe needs only a bounded slice window to
# judge whether one supplied slice directly expresses the complete conclusion;
# the on-demand multi-slice synthesis then receives the full token-bounded
# workset.  The window carries the workset's leading chapter-diverse slices;
# keeping it small keeps the probe request fast and inside the 300-second
# timeout while the multi-slice synthesis remains the complete fallback.
SEMANTIC_SUPPORT_SINGLE_SLICE_INPUT_TOKEN_BUDGET = 4_000
# Internal raw-slice retention budget for the separate EvidenceLedger, derived
# from the Ledger 12000-token product budget minus claim headroom.  It bounds
# how many exact slices may be retained under raw identity; the assembler still
# enforces the final 12000 cap and records any overflow as a typed Ledger drop.
SEMANTIC_SUPPORT_LEDGER_RETENTION_TOKEN_BUDGET = 8000
# Durable pre-SupportWorkset membership audit boundaries.  Every public Need
# retains ordered membership and typed keep/drop reasons at each boundary in
# the existing typed support-progress artifact.
AUDIT_LEGAL_INPUT_HANDLES = "legal_input_handles"
AUDIT_DIRECT_RANKED_HANDLES = "direct_ranked_handles"
AUDIT_COMPATIBLE_HANDLES = "compatible_handles"
AUDIT_DIVERSIFIED_POOL = "deduplicated_diversified_handle_pool"
AUDIT_BOUNDED_SELECTED_HANDLES = "bounded_selected_handles"
AUDIT_L0_BLOCKS_SPANS_RESOLVED = "l0_blocks_spans_resolved"
AUDIT_EXACT_SLICES_SEGMENTED = "exact_slices_segmented"
AUDIT_SUPPORT_WORKSET_PACKED = "support_workset_packed"
AUDIT_SEMANTIC_CHUNKS_EXPOSED = "semantic_chunks_exposed"
AUDIT_RAW_LEDGER_RETAINED = "raw_ledger_entries_retained"

# Shared instruction envelopes for the two semantic proposal stages.  The
# serialized-request estimator measures these together with the task/Need
# JSON and the slice fragments, so the chunk budget covers the complete
# request rather than slice text alone.
_SINGLE_SLICE_PROMPT_TEMPLATE = (
    "You are a pre-freeze support claim proposer. Work only from the public "
    "memory Need and its exact cutoff-safe evidence slices below. Gold "
    "annotations, future text, and evaluator contracts are unavailable and must "
    "never be inferred. Determine whether ONE supplied exact slice directly and "
    "completely expresses the conclusion for every required facet of this Need. "
    "If yes, return exactly ONE claim citing exactly that one slice "
    "(single_slice_sufficient=true, one slice_unit_id). Keep the claim concise "
    "and Writer-facing: at most 400 characters. If no single slice "
    "suffices, copy the Need's need_id verbatim into insufficient_need_ids "
    "instead of writing a weak or partial claim. For each returned claim copy "
    "the need_facet_ids of the required facets the claim directly establishes; "
    "do not list facets it does not establish. Preserve all material "
    "qualifications, negation, and epistemic scope of the slice. Treat facet "
    "kinds as questions to resolve, not asserted values: for an "
    "unresolved_status facet, write the claim as a coverage question, not an "
    "asserted value; never infer that it remains unresolved from that label "
    "alone, and never let an earlier plan, wish, or promise override a "
    "supplied observed/current state establishing fulfillment or a current "
    "relationship. Unknown IDs "
    "are invalid; every claim must reference a supplied slice_unit_id. "
    "Return the JSON response EXACTLY in this shape and no other keys: "
    '{"claims": [{"need_id": "<need id>", "need_facet_ids": ["<facet id>"], '
    '"slice_unit_id": "<one slice id>", "claim_text": "<claim>"}], '
    '"insufficient_need_ids": ["<need id>"]}. Use exactly the keys '
    "claim_text, slice_unit_id (singular), need_facet_ids; never use the "
    "keys claim, cited_slice_unit_ids, slice_unit_ids, or facet_ids.\n"
)
_MULTI_SLICE_PROMPT_TEMPLATE = (
    "You are a pre-freeze support claim synthesizer. Work only from the public "
    "memory Need and its exact cutoff-safe evidence slices below. Gold "
    "annotations, future text, and evaluator contracts are unavailable and must "
    "never be inferred. Answer ONLY the required facets' questions. If the "
    "supplied slices cannot jointly establish the complete required-facet "
    "conclusion, return `insufficient_need_ids` — never write a claim about a "
    "background or unrelated slice, and never claim a slice supports a "
    "conclusion it does not contain. Synthesize ONE complete Writer-facing "
    "claim from the subset of supplied exact slices whose content jointly "
    "establishes the complete required-facet conclusion. The claim must be a "
    "new sentence combining the slices' content; it must not be a verbatim "
    "copy of any slice text, and it must not begin with a chapter title. Cite "
    "in `slice_unit_ids` ONLY the slices whose content the claim's clauses "
    "directly depend on — never the whole supplied list. The claim must be at "
    "most 400 characters. If the complete conclusion cannot be expressed "
    "within that bound, return `insufficient_need_ids` instead of exceeding "
    "the ceiling. "
    "Return the need_facet_ids of the required facets the complete claim "
    "establishes. Do not rewrite source clauses into a bridge, invent a "
    "proposition, or cite a slice you did not use. Preserve all material "
    "qualifications, negation, and epistemic scope. Treat facet kinds as "
    "questions to resolve, not asserted values: for an unresolved_status "
    "facet, write the claim as a coverage question, not an asserted value; "
    "never infer that it remains unresolved from that label alone, and never "
    "let an earlier plan, wish, or promise override a supplied "
    "observed/current state establishing fulfillment or a current "
    "relationship. Unknown IDs are invalid; every cited "
    "slice_unit_id must come from the supplied exact_slices list. "
    "Return the JSON response EXACTLY in this shape and no other keys: "
    '{"claims": [{"need_id": "<need id>", "need_facet_ids": ["<facet id>"], '
    '"slice_unit_ids": ["<slice id>"], "claim_text": "<claim>"}], '
    '"insufficient_need_ids": ["<need id>"]}. Use exactly the keys '
    "claim_text, slice_unit_ids, need_facet_ids; never use the keys claim, "
    "cited_slice_unit_ids, or facet_ids.\n"
)


class SupportSelectionResult(DomainModel):
    context_assembly_spec: ContextAssemblySpec
    support_groups: tuple[ClaimSupportGroup, ...]
    claim_variants: tuple[ClaimVariant, ...]
    support_receipts: tuple[ClaimSupportReceipt, ...]
    cutoff_attestations: tuple[CutoffAttestation, ...]
    completion_results: tuple[NeedCompletionResult, ...]
    diagnostic_codes: tuple[str, ...] = ()
    producer_version: str = Field(min_length=1)
    workset_reports: tuple[SupportWorksetReport, ...] = ()
    raw_evidence_ledger_entries: tuple[EvidenceLedgerEntry, ...] = ()


class SemanticSupportClaimDraft(DomainModel):
    """Gold-free semantic claim proposed against public Need/facet identities."""

    need_id: StableId
    need_facet_ids: tuple[StableId, ...] = Field(min_length=1)
    retrieval_unit_ids: tuple[StableId, ...] = Field(min_length=1)
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


class SingleSliceClaimDraft(DomainModel):
    """One claim proposed from exactly one exact slice.

    ``single_slice_sufficient`` is the semantic owner's judgment that this
    single slice directly and completely expresses the required-facet
    conclusion; the host still runs the whole-claim verifier and never closes
    a facet from the proposal alone.
    """

    need_id: StableId
    need_facet_ids: tuple[StableId, ...] = Field(min_length=1)
    slice_unit_id: StableId
    claim_text: str = Field(min_length=1, max_length=600)
    single_slice_sufficient: bool = False


class SingleSliceProposalBatch(DomainModel):
    claims: tuple[SingleSliceClaimDraft, ...] = Field(max_length=64)
    insufficient_need_ids: tuple[StableId, ...] = ()


class MultiSliceClaimDraft(DomainModel):
    """One synthesized claim citing any number of legal exact slices.

    The semantic owner writes the complete claim and names every supplied
    exact slice it used; there is no fixed two/three-slice shape.  The host
    validates identity, facet ownership, and the exact cited-ref union, then
    runs the independent whole-claim verifier.
    """

    need_id: StableId
    need_facet_ids: tuple[StableId, ...] = Field(min_length=1)
    slice_unit_ids: tuple[StableId, ...] = Field(min_length=1)
    claim_text: str = Field(min_length=1, max_length=1200)


class MultiSliceProposalBatch(DomainModel):
    claims: tuple[MultiSliceClaimDraft, ...] = Field(max_length=64)
    insufficient_need_ids: tuple[StableId, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceSlice:
    """Service-local exact raw read view over one canonical block window.

    A slice is a short paragraph preserved unchanged, or one contiguous
    sentence window of an oversized paragraph.  Its identity derives from the
    parent block identity, the exact unicode-codepoint start/end in the block
    text, and the text hash; the full block is retained only as lineage.
    """

    slice_id: StableId
    parent_unit_id: StableId
    parent_block_id: StableId
    chapter_id: StableId | None
    scene_id: StableId | None
    object_hash: ArtifactId
    text: str
    start: int
    end: int
    text_hash: QuoteHash
    evidence_ref: EvidenceRef
    source_commit: CommitId
    snapshot_id: StableId
    access_scope: str
    taint: tuple[str, ...]
    retrieval_order: int


class SupportWorksetReport(DomainModel):
    """One Need's token-bounded exact-slice workset as a typed artifact."""

    need_id: StableId
    slice_ids: tuple[StableId, ...]
    slice_token_counts: tuple[int, ...]
    total_tokens: int = Field(ge=0)
    dropped_slice_count: int = Field(ge=0)


@dataclass(slots=True)
class SupportFunnel:
    """Typed raw-support rejection funnel (block -> slice -> workset -> claim)."""

    blocks_resolved: int = 0
    slices_resolved: int = 0
    slices_invalid_span: int = 0
    slices_filtered: int = 0
    slices_budget_dropped: int = 0
    semantic_input_dropped: int = 0
    slices_not_proposed_transport: int = 0
    proposal_transport_failures: int = 0
    proposal_requests: int = 0
    single_slice_proposals: int = 0
    multi_slice_proposals: int = 0
    proposals_rejected: int = 0
    single_slice_verified: int = 0
    multi_slice_verified: int = 0
    whole_verifier_rejected: int = 0
    facet_not_closed: int = 0
    needs_insufficient: int = 0
    verifier_transport_failures: int = 0
    controller_dropped: int = 0
    semantic_input_dropped_total: int = 0
    writer_dropped: int = 0
    ledger_dropped: int = 0
    affected_need_ids: tuple[str, ...] = ()
    affected_slice_counts: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, int | tuple[str, ...] | tuple[int, ...]]:
        return {
            "blocks_resolved": self.blocks_resolved,
            "slices_resolved": self.slices_resolved,
            "slices_invalid_span": self.slices_invalid_span,
            "slices_filtered": self.slices_filtered,
            "slices_budget_dropped": self.slices_budget_dropped,
            "semantic_input_dropped": self.semantic_input_dropped,
            "slices_not_proposed_transport": self.slices_not_proposed_transport,
            "proposal_transport_failures": self.proposal_transport_failures,
            "proposal_requests": self.proposal_requests,
            "single_slice_proposals": self.single_slice_proposals,
            "multi_slice_proposals": self.multi_slice_proposals,
            "proposals_rejected": self.proposals_rejected,
            "single_slice_verified": self.single_slice_verified,
            "multi_slice_verified": self.multi_slice_verified,
            "whole_verifier_rejected": self.whole_verifier_rejected,
            "facet_not_closed": self.facet_not_closed,
            "needs_insufficient": self.needs_insufficient,
            "verifier_transport_failures": self.verifier_transport_failures,
            "controller_dropped": self.controller_dropped,
            "semantic_input_dropped_total": self.semantic_input_dropped_total,
            "writer_dropped": self.writer_dropped,
            "ledger_dropped": self.ledger_dropped,
            "affected_need_ids": self.affected_need_ids,
            "affected_slice_counts": self.affected_slice_counts,
        }


@dataclass(frozen=True, slots=True)
class _SliceProposalAudit:
    batch: SingleSliceProposalBatch | MultiSliceProposalBatch
    call: ModelCallRecord
    input_hash: ArtifactId
    output_hash: ArtifactId
    input_ref: ArtifactRef
    output_ref: ArtifactRef
    single_slice: bool = False


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


@dataclass(frozen=True, slots=True)
class _AuditRow:
    """One membership row of the durable pre-SupportWorkset boundary audit.

    ``drop_reason`` is None for a row that is kept at its boundary and a typed
    string otherwise.  ``chunk_index`` applies only to the semantic-chunks
    boundary.  Costs are byte lengths where the raw text is available; they are
    transport/retrieval unit sizes, not public Writer/Ledger token budgets.
    """

    stage: str
    unit_id: str
    l0_family: str
    chapter: int | None
    kind: str
    origin_need_ids: tuple[str, ...]
    order: int
    cost: int
    chunk_index: int | None = None
    drop_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "unit_id": self.unit_id,
            "l0_family": self.l0_family,
            "chapter": self.chapter,
            "kind": self.kind,
            "origin_need_ids": list(self.origin_need_ids),
            "order": self.order,
            "cost": self.cost,
            "chunk_index": self.chunk_index,
            "drop_reason": self.drop_reason,
        }


@dataclass(frozen=True, slots=True)
class _FailedCallDiagnostic:
    """Sanitized classification of one failed semantic model call."""

    category: str
    detail: str
    status_code: int | None = None
    retry_count: int = 0
    failed_input_ref: ArtifactRef | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "detail": self.detail,
            "status_code": self.status_code,
            "retry_count": self.retry_count,
            "failed_input_ref": (
                self.failed_input_ref.model_dump(mode="json")
                if self.failed_input_ref is not None
                else None
            ),
        }


class TrustedClaimSupportProducer:
    """Produce replayable narrow claims from public retrieval results."""

    version = "trusted_claim_support_producer.v31"

    def __init__(
        self,
        *,
        semantic_gateway: ModelGateway | None = None,
        artifact_writer: SupportArtifactWriter | None = None,
        progress_writer: SupportProgressWriter | None = None,
        pre_proposal_trace: bool = False,
    ) -> None:
        """Produce deterministic support evidence for public Needs.

        ``pre_proposal_trace`` runs the complete deterministic corridor
        (legal handles -> ranked -> compatible -> diversified pool -> bounded
        handles -> L0 resolution -> exact slices -> workset -> chunks -> raw
        Ledger) and records the durable membership audit without any model
        call.  It never proposes or verifies claims and is used to locate the
        first source-family loss on a frozen input before a real run.
        """
        self._semantic_gateway = semantic_gateway
        self._artifact_writer = artifact_writer
        self._progress_writer = progress_writer
        self._pre_proposal_trace = pre_proposal_trace
        self.last_diagnostic_codes: tuple[str, ...] = ()
        self.last_funnel: SupportFunnel = SupportFunnel()
        self.last_workset_reports: tuple[SupportWorksetReport, ...] = ()
        self.last_raw_ledger_entries: tuple[EvidenceLedgerEntry, ...] = ()
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
        if self._semantic_gateway is None and not self._pre_proposal_trace:
            self.last_workset_reports = ()
            self.last_raw_ledger_entries = ()
            return deterministic
        semantic_bundle = self._produce_semantic_support_with_worksets(
            task=task,
            units=units,
            needs=needs,
            basis_commit_id=basis_commit_id,
            basis_snapshot_id=basis_snapshot_id,
            unit_need_ids=unit_need_ids,
            token_counter=token_counter,
        )
        self.last_workset_reports = semantic_bundle[4]
        self.last_raw_ledger_entries = semantic_bundle[5]
        return self._coalesce(
            (*deterministic[0], *semantic_bundle[0]),
            (*deterministic[1], *semantic_bundle[1]),
            (*deterministic[2], *semantic_bundle[2]),
            (*deterministic[3], *semantic_bundle[3]),
            need_by_id=need_by_id,
        )

    @classmethod
    def _unit_family_id(cls, unit: RetrievalUnit) -> str:
        """Canonical L0 source family/block identity of one retrieval unit.

        The family is the exact span's parent block when the unit carries an
        exact evidence ref, then the recorded parent lineage, and finally the
        unit itself.  Compact copies, anchors, and curator sub-spans therefore
        share the family of the block they derive from.
        """

        for reference in unit.evidence_refs:
            if reference.span is not None:
                return reference.span.block_id.root
        if unit.parent_unit_id is not None:
            return unit.parent_unit_id.root
        if unit.parent_unit_ids:
            return unit.parent_unit_ids[0].root
        return unit.unit_id.root

    @classmethod
    def _unit_audit_row(
        cls,
        *,
        stage: str,
        unit: RetrievalUnit,
        order: int,
        cost: int,
        origin_need_ids: Sequence[StableId],
        drop_reason: str | None = None,
    ) -> _AuditRow:
        return _AuditRow(
            stage=stage,
            unit_id=unit.unit_id.root,
            l0_family=cls._unit_family_id(unit),
            chapter=cls._chapter_index(unit),
            kind=getattr(unit.unit_kind, "value", str(unit.unit_kind)),
            origin_need_ids=tuple(item.root for item in origin_need_ids),
            order=order,
            cost=cost,
            drop_reason=drop_reason,
        )

    @classmethod
    def _slice_audit_row(
        cls,
        *,
        stage: str,
        slice_: EvidenceSlice,
        order: int,
        cost: int,
        origin_need_ids: Sequence[StableId],
        chunk_index: int | None = None,
        drop_reason: str | None = None,
    ) -> _AuditRow:
        return _AuditRow(
            stage=stage,
            unit_id=slice_.slice_id.root,
            l0_family=slice_.parent_block_id.root,
            chapter=(
                cls._chapter_number(slice_.chapter_id)
                if slice_.chapter_id is not None
                else cls._chapter_number(slice_.parent_block_id)
                if slice_.parent_block_id is not None
                else None
            ),
            kind="exact_slice",
            origin_need_ids=tuple(item.root for item in origin_need_ids),
            order=order,
            cost=cost,
            chunk_index=chunk_index,
            drop_reason=drop_reason,
        )

    def _emit_audit(
        self,
        boundary: str,
        need_id: StableId,
        rows: Sequence[_AuditRow],
    ) -> None:
        self._record_progress(
            stage="handle_audit",
            boundary=boundary,
            need_id=need_id.root,
            row_count=len(rows),
            rows=[row.as_dict() for row in rows],
        )

    @staticmethod
    def _estimate_prompt_tokens(prompt: str) -> int:
        """Conservative serialized-request token estimate for the local model.

        Calibrated against the live endpoint: CJK text tokenizes at ~1.23
        chars/token and ASCII at ~4.46 chars/token.  The estimator over-counts
        both (1.0 tokens per CJK char, 0.35 tokens per other char) so a request
        inside the serialized budget lands below the measured ~30K-token
        practical ceiling even before output headroom.
        """

        cjk = sum(1 for char in prompt if "\u4e00" <= char <= "\u9fff")
        other = len(prompt) - cjk
        return max(1, cjk + int(other * 0.35) + 1)

    @staticmethod
    def _serialized_request_input(
        *,
        task: BenchmarkTaskContract,
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        entry: dict[str, object],
        need: Stage1MemoryNeed,
        slices: Sequence[EvidenceSlice],
        template: str,
    ) -> str:
        """Build the exact serialized work input and prompt for one request.

        The single-slice sufficiency probe and the multi-slice synthesis use
        the same structural envelope with different instructions, so both
        budget and record against the complete serialized request.
        """

        producer_input = {
            "task": task.model_dump(mode="json"),
            "basis_commit_id": basis_commit_id.root,
            "basis_snapshot_id": basis_snapshot_id.root,
            "needs": [
                {
                    "need_id": need.need_id.root,
                    "need_type": cast(str, entry["need_type"]),
                    "query_intent": cast(str, entry["query_intent"]),
                    "query_text": cast(str, entry["query_text"])[:1600],
                    "why_needed": cast(str, entry["why_needed"])[:600],
                    "required_need_facets": cast(
                        list[dict[str, object]], entry["required_need_facets"]
                    ),
                    "exact_slices": [
                        {
                            "slice_unit_id": slice_.slice_id.root,
                            "chapter_id": (
                                slice_.chapter_id.root if slice_.chapter_id is not None else None
                            ),
                            "start": slice_.start,
                            "end": slice_.end,
                            "text": slice_.text,
                        }
                        for slice_ in slices
                    ],
                }
            ],
        }
        input_bytes = canonical_json_bytes(producer_input)
        prompt = (
            template
            + '<PUBLIC_SUPPORT_INPUT trusted="false">\n'
            + input_bytes.decode("utf-8")
            + "\n</PUBLIC_SUPPORT_INPUT>"
        )
        return prompt

    def _produce_semantic_support_with_worksets(
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
        tuple[SupportWorksetReport, ...],
        tuple[EvidenceLedgerEntry, ...],
    ]:
        need_by_id = {need.need_id: need for need in needs}
        input_order = {unit.unit_id: index for index, unit in enumerate(units)}
        units_by_need: dict[StableId, list[RetrievalUnit]] = {need.need_id: [] for need in needs}
        for unit in units:
            for need_id in unit_need_ids.get(unit.unit_id, ()):
                mapped_need = need_by_id.get(need_id)
                if mapped_need is not None and self._legal_for_need(task, mapped_need, unit):
                    units_by_need[need_id].append(unit)
        # Public audit lineage: every real origin Need that selected the unit.
        # A unit without a recorded origin Need is never borrowed across routes.
        origin_need_ids: dict[StableId, tuple[StableId, ...]] = {
            unit.unit_id: tuple(
                dict.fromkeys(
                    need_id
                    for need_id in unit_need_ids.get(unit.unit_id, ())
                    if need_id in need_by_id
                )
            )
            for unit in units
        }

        public_needs: list[dict[str, object]] = []
        allowed_units_by_need: dict[StableId, set[StableId]] = {}
        ordered_pool_by_need: dict[StableId, tuple[RetrievalUnit, ...]] = {}
        direct_ids_by_need: dict[StableId, set[StableId]] = {}
        for public_need in needs:
            legal_input = units_by_need[public_need.need_id]
            legal_rows = [
                self._unit_audit_row(
                    stage=AUDIT_LEGAL_INPUT_HANDLES,
                    unit=unit,
                    order=index,
                    cost=len(unit.text.encode("utf-8")),
                    origin_need_ids=origin_need_ids.get(unit.unit_id, ()),
                )
                for index, unit in enumerate(legal_input)
            ]
            ranked_all = sorted(
                units_by_need[public_need.need_id],
                # `units` is assembled from the Controller's fused retrieval
                # order.  Sorting this list by query-term overlap promoted
                # common words such as "同伴" above a more relevant grounded
                # fallback hit and made the semantic proposer see the wrong
                # evidence first.  Preserve the retrieval order here; the
                # lexical relevance helper remains available for deterministic
                # Controller selection after a claim has been proposed.
                #
                # When several units share the same passage, the unit carrying
                # the full-passage evidence reference is the complete citation
                # target: a narrow curator sub-span inside the same block can
                # never cover the whole passage.  Ranking the passage-complete
                # unit first keeps the proposer's evidence order aligned with
                # the most complete legal citation instead of the first-seen
                # fragment.
                key=lambda unit: (
                    not TrustedClaimSupportProducer._has_full_passage_ref(unit),
                    input_order[unit.unit_id],
                    unit.unit_id.root,
                ),
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
                    # When no local chapter window is available, lead the
                    # pool with the bounded grounded rescue set so the
                    # protected rescue evidence is considered first, but do
                    # not hide the remaining direct grounded evidence: a
                    # rank-complete historical conclusion may span more than
                    # the late window, and dropping those units from the
                    # proposer pool loses the complete evidence group.  The
                    # rescue set still comes first, so a lexical near-match
                    # cannot displace it.
                    ordered = (
                        *late_grounded,
                        *(
                            unit
                            for unit in ranked_all
                            if unit.unit_id not in late_ids
                            and getattr(unit.unit_kind, "value", unit.unit_kind)
                            in {
                                RetrievalUnitKind.GROUNDED_BLOCK.value,
                                RetrievalUnitKind.GROUNDED_SPAN.value,
                            }
                        ),
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
            ranked = tuple(dict.fromkeys(ordered))
            ranked_rows = [
                self._unit_audit_row(
                    stage=AUDIT_DIRECT_RANKED_HANDLES,
                    unit=unit,
                    order=index,
                    cost=len(unit.text.encode("utf-8")),
                    origin_need_ids=origin_need_ids.get(unit.unit_id, ()),
                )
                for index, unit in enumerate(ordered)
            ]
            if not ranked:
                self._emit_audit(AUDIT_LEGAL_INPUT_HANDLES, public_need.need_id, legal_rows)
                self._emit_audit(AUDIT_DIRECT_RANKED_HANDLES, public_need.need_id, ranked_rows)
                continue
            compatible = self._compatible_support_units(
                task=task,
                target_need=public_need,
                units=units,
                need_by_id=need_by_id,
                units_by_need=units_by_need,
                origin_need_ids=origin_need_ids,
                basis_commit_id=basis_commit_id,
                basis_snapshot_id=basis_snapshot_id,
            )
            compatible_rows = [
                self._unit_audit_row(
                    stage=AUDIT_COMPATIBLE_HANDLES,
                    unit=unit,
                    order=index,
                    cost=len(unit.text.encode("utf-8")),
                    origin_need_ids=origin_need_ids.get(unit.unit_id, ()),
                )
                for index, unit in enumerate(compatible)
            ]
            direct_ids = {unit.unit_id for unit in ranked}
            # Direct units always lead the pool and are never displaced by
            # compatible units under the shared input window.  When demand
            # exceeds the window, the same passage is often carried by several
            # selected units (a grounded block plus its relation/state anchors
            # and curator copies).  Two collapses run before any item cap:
            #
            # 1. L0-lineage canonicalization: every representation of one
            #    canonical source family collapses to the most canonical unit
            #    (full-passage grounded block/span first, then compact
            #    excerpts, then anchors), so a compact or anchor copy can never
            #    crowd out the canonical block it derives from.
            # 2. Evidence-diversity collapse: units that only duplicate
            #    evidence already retained by another family are dropped, so
            #    distinct compatible evidence from other routes can enter.
            #
            # Only then is the explicit retrieval-handle budget applied, in a
            # chapter-diverse stable order: every legal source chapter keeps
            # its leading handle before the budget fills by retrieval order.
            combined = tuple(dict.fromkeys((*ranked, *compatible)))
            canonical_combined, family_collapsed_ids = self._family_canonicalize(combined)
            diversity_collapsed_ids: set[StableId] = set()
            if len(canonical_combined) > SEMANTIC_SUPPORT_INPUT_LIMIT:
                diversified = self._evidence_diverse_pool(canonical_combined, ())
                diversity_collapsed_ids = {
                    unit.unit_id
                    for unit in canonical_combined
                    if unit.unit_id not in {item.unit_id for item in diversified}
                }
                canonical_combined = tuple(dict.fromkeys(diversified))
            combined = self._chapter_diverse_order(canonical_combined)
            collapsed_ids = set(family_collapsed_ids) | diversity_collapsed_ids
            diversity_rows = [
                self._unit_audit_row(
                    stage=AUDIT_DIVERSIFIED_POOL,
                    unit=unit,
                    order=index,
                    cost=len(unit.text.encode("utf-8")),
                    origin_need_ids=origin_need_ids.get(unit.unit_id, ()),
                    drop_reason=(
                        (
                            "family_collapsed:duplicate_representation"
                            if unit.unit_id in family_collapsed_ids
                            else "diversity_collapsed:duplicate_evidence"
                        )
                        if unit.unit_id in collapsed_ids
                        else None
                    ),
                )
                for index, unit in enumerate((*ranked, *compatible))
            ]
            # The explicit, reported retrieval-handle budget.  Every unit that
            # survives ranking, compatibility, and the two deduplication
            # passes is eligible in stable chapter-diverse order; the budget
            # drops only the tail after the canonicalized pool is formed, so
            # a distinct source family is never displaced by a copy of an
            # already-retained passage.
            pool_units = combined[:SEMANTIC_SUPPORT_INPUT_LIMIT]
            pool_ids = {unit.unit_id for unit in pool_units}
            bounded_rows = [
                self._unit_audit_row(
                    stage=AUDIT_BOUNDED_SELECTED_HANDLES,
                    unit=unit,
                    order=index,
                    cost=len(unit.text.encode("utf-8")),
                    origin_need_ids=origin_need_ids.get(unit.unit_id, ()),
                    drop_reason=(None if unit.unit_id in pool_ids else "handle_budget_cap_dropped"),
                )
                for index, unit in enumerate(combined)
            ]
            allowed_units_by_need[public_need.need_id] = pool_ids
            ordered_pool_by_need[public_need.need_id] = pool_units
            direct_ids_by_need[public_need.need_id] = direct_ids
            self._emit_audit(AUDIT_LEGAL_INPUT_HANDLES, public_need.need_id, legal_rows)
            self._emit_audit(AUDIT_DIRECT_RANKED_HANDLES, public_need.need_id, ranked_rows)
            self._emit_audit(AUDIT_COMPATIBLE_HANDLES, public_need.need_id, compatible_rows)
            self._emit_audit(AUDIT_DIVERSIFIED_POOL, public_need.need_id, diversity_rows)
            self._emit_audit(AUDIT_BOUNDED_SELECTED_HANDLES, public_need.need_id, bounded_rows)
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
                }
            )
        if not public_needs:
            return (), (), (), (), (), ()
        # Deterministic exact-slice read corridor: the selected grounded blocks
        # are resolved to their canonical text and segmented by original
        # paragraph boundaries; only an oversized paragraph is split into
        # contiguous sentence windows.  The ordered exact slices are packed
        # into a token-bounded SupportWorkset per Need, packed directly into
        # the semantic work input with their raw identity and exact refs, and
        # retained in the separate EvidenceLedger under raw identity.
        funnel = SupportFunnel()
        workset_reports: list[SupportWorksetReport] = []
        slices_by_need: dict[StableId, tuple[EvidenceSlice, ...]] = {}
        worksets_by_need: dict[StableId, tuple[EvidenceSlice, ...]] = {}
        resolved_block_ids: set[StableId] = set()
        for public_need in needs:
            pool_units = ordered_pool_by_need.get(public_need.need_id, ())
            resolved, resolution_rows, slice_rows = self._resolve_exact_slices(
                pool_units,
                need=public_need,
                basis_commit_id=basis_commit_id,
                basis_snapshot_id=basis_snapshot_id,
                checkpoint_chapter=task.checkpoint_chapter,
                origin_need_ids=origin_need_ids,
            )
            self._emit_audit(AUDIT_L0_BLOCKS_SPANS_RESOLVED, public_need.need_id, resolution_rows)
            self._emit_audit(AUDIT_EXACT_SLICES_SEGMENTED, public_need.need_id, slice_rows)
            slices_by_need[public_need.need_id] = resolved
            resolved_block_ids.update(
                slice_.parent_block_id for slice_ in resolved if slice_.parent_block_id
            )
            funnel.slices_resolved += len(resolved)
            workset, budget_dropped, workset_rows = self._pack_workset(
                resolved,
                need=public_need,
                token_counter=token_counter,
            )
            worksets_by_need[public_need.need_id] = workset
            self._emit_audit(AUDIT_SUPPORT_WORKSET_PACKED, public_need.need_id, workset_rows)
            funnel.slices_budget_dropped += budget_dropped
            workset_reports.append(
                SupportWorksetReport(
                    need_id=public_need.need_id,
                    slice_ids=tuple(slice_.slice_id for slice_ in workset),
                    slice_token_counts=tuple(
                        max(1, token_counter(slice_.text)) for slice_ in workset
                    ),
                    total_tokens=sum(max(1, token_counter(slice_.text)) for slice_ in workset),
                    dropped_slice_count=budget_dropped,
                )
            )
        funnel.blocks_resolved = len(resolved_block_ids)
        groups: list[ClaimSupportGroup] = []
        variants: list[ClaimVariant] = []
        receipts: list[ClaimSupportReceipt] = []
        attestations: list[CutoffAttestation] = []
        for entry in public_needs:
            need_id = StableId(cast(str, entry["need_id"]))
            need = need_by_id[need_id]
            workset = worksets_by_need.get(need_id, ())
            if not workset:
                funnel.facet_not_closed += 1
                continue
            required_facets = tuple(
                facet.need_facet_id
                for facet in need.need_facets
                if need.completion_spec is None
                or facet.need_facet_id in need.completion_spec.required_need_facet_ids
            )
            # Single-slice sufficiency: the semantic owner decides whether one
            # exact slice directly expresses the complete required-facet
            # conclusion.  The host never closes a facet from the proposal
            # alone; the whole-claim verifier runs over the complete claim,
            # the cited slice, and bounded counter-evidence.  The probe window
            # is a bounded prefix of the workset so the request stays under the
            # endpoint's practical request ceiling; multi-slice synthesis then
            # receives the full token-bounded workset.  A pre-proposal trace
            # runs no model call and therefore proposes nothing.
            single_window = self._single_slice_window(
                workset,
                token_counter=token_counter,
            )
            single_audit: _SliceProposalAudit | None = None
            if self._semantic_gateway is not None:
                single_audit = self._propose_single_slice(
                    task=task,
                    need=need,
                    workset=single_window,
                    entry=entry,
                    basis_commit_id=basis_commit_id,
                    basis_snapshot_id=basis_snapshot_id,
                    funnel=funnel,
                )
            if single_audit is not None and single_audit.single_slice:
                claim = cast(SingleSliceClaimDraft, single_audit.batch.claims[0])
                if claim.single_slice_sufficient:
                    funnel.single_slice_proposals += 1
                    if self._reject_garbage_claim(claim.claim_text):
                        funnel.proposals_rejected += 1
                        self._record_progress(
                            stage="proposal_rejected",
                            reason="rejected:garbage_claim",
                            need_id=need.need_id.root,
                            claim_text=claim.claim_text,
                        )
                    elif (
                        cited := self._slice_by_id(workset, claim.slice_unit_id)
                    ) is not None and set(claim.need_facet_ids).issubset(
                        {facet.need_facet_id for facet in need.need_facets}
                    ):
                        audit = self._verify_claim_whole(
                            task=task,
                            need=need,
                            claim_text=claim.claim_text,
                            facet_ids=claim.need_facet_ids,
                            cited_slices=(cited,),
                            context_slices=tuple(
                                slice_ for slice_ in workset if slice_.slice_id != cited.slice_id
                            ),
                            basis_commit_id=basis_commit_id,
                            basis_snapshot_id=basis_snapshot_id,
                            funnel=funnel,
                        )
                        if audit is not None:
                            self._emit_verified_claim(
                                need=need,
                                claim_text=claim.claim_text,
                                unit_ids=(cited.slice_id,),
                                evidence_refs=(cited.evidence_ref,),
                                facet_ids=claim.need_facet_ids,
                                audit=audit,
                                proposal_audit=single_audit,
                                task=task,
                                basis_commit_id=basis_commit_id,
                                basis_snapshot_id=basis_snapshot_id,
                                token_counter=token_counter,
                                groups=groups,
                                variants=variants,
                                receipts=receipts,
                                attestations=attestations,
                                producer_marker="single",
                            )
                            funnel.single_slice_verified += 1
                    else:
                        funnel.proposals_rejected += 1
            # On-demand multi-slice synthesis only for a still-open Need.
            # Transport isolation: the workset is partitioned into serialized
            # request-bounded chunks; each chunk is one synthesis request, and
            # a transport failure loses only that chunk's slices.  Required-
            # facet closure is recomputed after every emitted verified claim,
            # so a Need closed by an early chunk schedules no further chunks.
            covered_facets = {
                facet_id
                for group in groups
                if need_id in group.need_ids
                for facet_id in group.need_facet_ids
            }
            chunks = self._workset_chunks(
                workset,
                task=task,
                need=need,
                entry=entry,
                basis_commit_id=basis_commit_id,
                basis_snapshot_id=basis_snapshot_id,
            )
            for chunk_index, chunk in enumerate(chunks):
                chunk_rows = [
                    self._slice_audit_row(
                        stage=AUDIT_SEMANTIC_CHUNKS_EXPOSED,
                        slice_=slice_,
                        order=index,
                        cost=len(slice_.text.encode("utf-8")),
                        origin_need_ids=(
                            origin_need_ids.get(slice_.parent_unit_id, ())
                            if slice_.parent_unit_id is not None
                            else ()
                        ),
                        chunk_index=chunk_index,
                    )
                    for index, slice_ in enumerate(chunk)
                ]
                self._emit_audit(AUDIT_SEMANTIC_CHUNKS_EXPOSED, need_id, chunk_rows)
                if set(required_facets).issubset(covered_facets):
                    break
                if self._semantic_gateway is None:
                    continue
                multi_audit = self._propose_multi_slice(
                    task=task,
                    need=need,
                    workset=chunk,
                    entry=entry,
                    basis_commit_id=basis_commit_id,
                    basis_snapshot_id=basis_snapshot_id,
                    funnel=funnel,
                )
                if multi_audit is None or multi_audit.single_slice:
                    continue
                multi_claim = cast(MultiSliceClaimDraft, multi_audit.batch.claims[0])
                funnel.multi_slice_proposals += 1
                cited_ids = set(multi_claim.slice_unit_ids)
                chunk_ids = {slice_.slice_id for slice_ in chunk}
                legal_facets = {facet.need_facet_id for facet in need.need_facets}
                if (
                    cited_ids.issubset(chunk_ids)
                    and cited_ids
                    and set(multi_claim.need_facet_ids).issubset(legal_facets)
                ):
                    if self._reject_garbage_claim(multi_claim.claim_text):
                        funnel.proposals_rejected += 1
                        self._record_progress(
                            stage="proposal_rejected",
                            reason="rejected:garbage_claim",
                            need_id=need.need_id.root,
                            claim_text=multi_claim.claim_text,
                        )
                        continue
                    cited_slices = tuple(slice_ for slice_ in chunk if slice_.slice_id in cited_ids)
                    audit = self._verify_claim_whole(
                        task=task,
                        need=need,
                        claim_text=multi_claim.claim_text,
                        facet_ids=multi_claim.need_facet_ids,
                        cited_slices=cited_slices,
                        context_slices=tuple(
                            slice_ for slice_ in chunk if slice_.slice_id not in cited_ids
                        ),
                        basis_commit_id=basis_commit_id,
                        basis_snapshot_id=basis_snapshot_id,
                        funnel=funnel,
                    )
                    if audit is not None:
                        self._emit_verified_claim(
                            need=need,
                            claim_text=multi_claim.claim_text,
                            unit_ids=tuple(slice_.slice_id for slice_ in cited_slices),
                            evidence_refs=tuple(slice_.evidence_ref for slice_ in cited_slices),
                            facet_ids=multi_claim.need_facet_ids,
                            audit=audit,
                            proposal_audit=multi_audit,
                            task=task,
                            basis_commit_id=basis_commit_id,
                            basis_snapshot_id=basis_snapshot_id,
                            token_counter=token_counter,
                            groups=groups,
                            variants=variants,
                            receipts=receipts,
                            attestations=attestations,
                            producer_marker="synthesized",
                        )
                        funnel.multi_slice_verified += 1
                        covered_facets.update(multi_claim.need_facet_ids)
                else:
                    funnel.proposals_rejected += 1
            covered_facets = {
                facet_id
                for group in groups
                if need_id in group.need_ids
                for facet_id in group.need_facet_ids
            }
            if not set(required_facets).issubset(covered_facets):
                funnel.facet_not_closed += 1

        # Raw-slice ledger retention: the packed exact slices are retained in
        # the separate EvidenceLedger under raw identity (no support group, no
        # facet closure).  Retention follows need requirement/priority then
        # workset order, bounded by the internal retention budget; the
        # assembler still enforces the final Ledger 12000-token cap.
        raw_ledger_entries: list[EvidenceLedgerEntry] = []
        retention_tokens = 0
        for need in sorted(
            needs,
            key=lambda item: (
                0 if item.requirement is RequirementLevel.MANDATORY else 1,
                -item.priority,
                item.need_id.root,
            ),
        ):
            ledger_rows: list[_AuditRow] = []
            for slice_ in worksets_by_need.get(need.need_id, ()):
                cost = max(1, token_counter(slice_.text))
                if retention_tokens + cost > SEMANTIC_SUPPORT_LEDGER_RETENTION_TOKEN_BUDGET:
                    funnel.ledger_dropped += 1
                    ledger_rows.append(
                        self._slice_audit_row(
                            stage=AUDIT_RAW_LEDGER_RETAINED,
                            slice_=slice_,
                            order=len(ledger_rows),
                            cost=len(slice_.text.encode("utf-8")),
                            origin_need_ids=(
                                origin_need_ids.get(slice_.parent_unit_id, ())
                                if slice_.parent_unit_id is not None
                                else ()
                            ),
                            drop_reason="ledger_budget_drop",
                        )
                    )
                    continue
                retention_tokens += cost
                ledger_rows.append(
                    self._slice_audit_row(
                        stage=AUDIT_RAW_LEDGER_RETAINED,
                        slice_=slice_,
                        order=len(ledger_rows),
                        cost=len(slice_.text.encode("utf-8")),
                        origin_need_ids=(
                            origin_need_ids.get(slice_.parent_unit_id, ())
                            if slice_.parent_unit_id is not None
                            else ()
                        ),
                    )
                )
                raw_ledger_entries.append(
                    EvidenceLedgerEntry(
                        ledger_id=StableId(f"ledger.raw-slice.{slice_.slice_id.root}"[:128]),
                        evidence_refs=(slice_.evidence_ref,),
                        claim_excerpt=slice_.text[:240],
                        source_commit=basis_commit_id,
                        information_scope=slice_.access_scope,
                        need_ids=(need.need_id,),
                        retrieval_unit_ids=(slice_.parent_unit_id,),
                    )
                )
            self._emit_audit(AUDIT_RAW_LEDGER_RETAINED, need.need_id, ledger_rows)

        self.last_funnel = funnel
        self._record_progress(stage="funnel", **funnel.as_dict())
        self._record_progress(
            stage="terminal",
            state=(
                "failed"
                if funnel.proposal_transport_failures or funnel.verifier_transport_failures
                else "completed_with_failures"
                if any(
                    value > 0
                    for key, value in funnel.as_dict().items()
                    if isinstance(value, int)
                    and key
                    not in {
                        "blocks_resolved",
                        "slices_resolved",
                        "proposal_requests",
                        "single_slice_proposals",
                        "multi_slice_proposals",
                        "single_slice_verified",
                        "multi_slice_verified",
                    }
                )
                else "completed"
            ),
            **funnel.as_dict(),
        )
        return (
            tuple(groups),
            tuple(variants),
            tuple(receipts),
            tuple(attestations),
            tuple(workset_reports),
            tuple(raw_ledger_entries),
        )

    def _slice_by_id(
        self,
        workset: Sequence[EvidenceSlice],
        slice_id: StableId,
    ) -> EvidenceSlice | None:
        return next((slice_ for slice_ in workset if slice_.slice_id == slice_id), None)

    def _resolve_exact_slices(
        self,
        pool_units: Sequence[RetrievalUnit],
        *,
        need: Stage1MemoryNeed,
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        checkpoint_chapter: int,
        origin_need_ids: Mapping[StableId, tuple[StableId, ...]],
    ) -> tuple[tuple[EvidenceSlice, ...], tuple[_AuditRow, ...], tuple[_AuditRow, ...]]:
        """Resolve one Need's ordered pool into exact paragraph/sentence slices.

        Only canonical contiguous text is sliced: a grounded block's own text
        (the full block) or an exact grounded span.  Compact excerpts and
        anchors are navigation/preview and never produce canonical slices; the
        full block is retained only as parent lineage.  Slices deduplicate by
        exact source location and text hash while preserving pool order, and
        every slice passes the same basis/snapshot/scope/cutoff/taint checks
        before it may enter the workset.

        Returns the resolved slices together with the durable per-unit
        resolution audit rows (kept or typed drop at the L0 boundary) and the
        per-slice segmentation rows (kept or duplicate-drop).
        """

        slices: list[EvidenceSlice] = []
        seen: set[tuple[str, int, int, str]] = set()
        resolution_rows: list[_AuditRow] = []
        slice_rows: list[_AuditRow] = []
        for order, unit in enumerate(pool_units):
            unit_kind = getattr(unit.unit_kind, "value", unit.unit_kind)
            drop_reason: str | None = None
            if unit_kind not in {
                RetrievalUnitKind.GROUNDED_BLOCK.value,
                RetrievalUnitKind.GROUNDED_SPAN.value,
            }:
                drop_reason = "not_grounded:anchor_or_preview_only"
            elif unit.unit_id.root.startswith("compact."):
                drop_reason = "compact_preview:not_canonical_text"
            elif not unit.evidence_refs:
                drop_reason = "no_evidence_refs:unresolved_unit"
            else:
                base = self._canonical_base_ref(unit)
                if base is None or base.span is None:
                    drop_reason = "resolution_failed:no_canonical_exact_span"
                elif unit.derivation_taint:
                    drop_reason = "filtered:taint"
                elif unit.source_commit != basis_commit_id:
                    drop_reason = "filtered:basis_commit_mismatch"
                elif unit.snapshot_id != basis_snapshot_id:
                    drop_reason = "filtered:snapshot_mismatch"
                elif unit.access_scope not in self._visible_scopes(need):
                    drop_reason = "filtered:access_scope"
                else:
                    chapter = self._chapter_index(unit)
                    if chapter is not None and chapter > checkpoint_chapter:
                        drop_reason = "filtered:cutoff_violation"
            resolution_rows.append(
                self._unit_audit_row(
                    stage=AUDIT_L0_BLOCKS_SPANS_RESOLVED,
                    unit=unit,
                    order=order,
                    cost=len(unit.text.encode("utf-8")),
                    origin_need_ids=origin_need_ids.get(unit.unit_id, ()),
                    drop_reason=drop_reason,
                )
            )
            if drop_reason is not None:
                continue
            base = self._canonical_base_ref(unit)
            assert base is not None and base.span is not None
            for start, end, text in self._segment_block_text(unit.text):
                digest = quote_hash(text).root.removeprefix("sha256:")[:24]
                absolute_start = base.span.start + start
                absolute_end = base.span.start + end
                identity = (base.span.block_id.root, absolute_start, absolute_end, text)
                if identity in seen:
                    slice_rows.append(
                        self._slice_audit_row(
                            stage=AUDIT_EXACT_SLICES_SEGMENTED,
                            slice_=EvidenceSlice(
                                slice_id=StableId(
                                    f"slice.{unit.unit_id.root}.{absolute_start}.{digest}"
                                ),
                                parent_unit_id=unit.unit_id,
                                parent_block_id=base.span.block_id,
                                chapter_id=base.chapter_id,
                                scene_id=base.scene_id,
                                object_hash=base.object_hash,
                                text=text,
                                start=absolute_start,
                                end=absolute_end,
                                text_hash=quote_hash(text),
                                evidence_ref=base.model_copy(
                                    update={
                                        "evidence_id": StableId(
                                            f"evidence.slice.{unit.unit_id.root}.{absolute_start}.{digest}"
                                        ),
                                        "quote_hash": quote_hash(text),
                                        "span": TextSpanRef(
                                            block_id=base.span.block_id,
                                            start=absolute_start,
                                            end=absolute_end,
                                        ),
                                    }
                                ),
                                source_commit=unit.source_commit,
                                snapshot_id=unit.snapshot_id,
                                access_scope=unit.access_scope,
                                taint=unit.derivation_taint,
                                retrieval_order=order,
                            ),
                            order=len(slice_rows),
                            cost=len(text.encode("utf-8")),
                            origin_need_ids=origin_need_ids.get(unit.unit_id, ()),
                            drop_reason="duplicate_slice",
                        )
                    )
                    continue
                seen.add(identity)
                evidence_ref = base.model_copy(
                    update={
                        "evidence_id": StableId(
                            f"evidence.slice.{unit.unit_id.root}.{absolute_start}.{digest}"
                        ),
                        "quote_hash": quote_hash(text),
                        "span": TextSpanRef(
                            block_id=base.span.block_id,
                            start=absolute_start,
                            end=absolute_end,
                        ),
                    }
                )
                slice_ = EvidenceSlice(
                    slice_id=StableId(f"slice.{unit.unit_id.root}.{absolute_start}.{digest}"),
                    parent_unit_id=unit.unit_id,
                    parent_block_id=base.span.block_id,
                    chapter_id=base.chapter_id,
                    scene_id=base.scene_id,
                    object_hash=base.object_hash,
                    text=text,
                    start=absolute_start,
                    end=absolute_end,
                    text_hash=quote_hash(text),
                    evidence_ref=evidence_ref,
                    source_commit=unit.source_commit,
                    snapshot_id=unit.snapshot_id,
                    access_scope=unit.access_scope,
                    taint=unit.derivation_taint,
                    retrieval_order=order,
                )
                slices.append(slice_)
                slice_rows.append(
                    self._slice_audit_row(
                        stage=AUDIT_EXACT_SLICES_SEGMENTED,
                        slice_=slice_,
                        order=len(slice_rows),
                        cost=len(text.encode("utf-8")),
                        origin_need_ids=origin_need_ids.get(unit.unit_id, ()),
                    )
                )
        return tuple(slices), tuple(resolution_rows), tuple(slice_rows)

    @classmethod
    def _canonical_base_ref(cls, unit: RetrievalUnit) -> EvidenceRef | None:
        """Return the exact full-passage or precise span ref of a canonical unit.

        A grounded block carries a full-passage ref covering its whole text; an
        exact grounded span carries a precise span ref.  Compact segment refs
        remain derivation evidence but are never the canonical slice base.
        """

        for reference in unit.evidence_refs:
            if reference.span is None:
                continue
            if unit.unit_id.root.startswith("compact."):
                continue
            if reference.span.block_id.root == unit.unit_id.root.removeprefix("grounded."):
                return reference
            if reference.span.block_id.root in unit.unit_id.root:
                return reference
        return unit.evidence_refs[0] if unit.evidence_refs else None

    @classmethod
    def _visible_scopes(cls, need: Stage1MemoryNeed) -> frozenset[str]:
        return {
            "writer_safe": frozenset({"writer_safe"}),
            "author_planning": frozenset({"writer_safe", "author_planning"}),
            "evaluator": frozenset({"writer_safe", "author_planning", "evaluator"}),
        }.get(need.access_scope, frozenset())

    @classmethod
    def _segment_block_text(cls, text: str) -> tuple[tuple[int, int, str], ...]:
        """Segment canonical block text into exact contiguous slices.

        Paragraph boundaries come from the original text.  A short paragraph is
        preserved unchanged; an oversized paragraph is split only into
        contiguous sentence windows.  Non-adjacent sentences are never joined.
        """

        segments: list[tuple[int, int, str]] = []
        paragraph_start = 0
        for match in re.finditer(r"\n+", text):
            paragraph = text[paragraph_start : match.start()]
            if paragraph.strip():
                segments.extend(cls._segment_paragraph(paragraph, paragraph_start))
            paragraph_start = match.end()
        tail = text[paragraph_start:]
        if tail.strip():
            segments.extend(cls._segment_paragraph(tail, paragraph_start))
        return tuple(segments)

    @classmethod
    def _segment_paragraph(
        cls,
        paragraph: str,
        base_offset: int,
    ) -> tuple[tuple[int, int, str], ...]:
        if len(paragraph) <= SEMANTIC_SUPPORT_SLICE_MAX_CHARS:
            return ((base_offset, base_offset + len(paragraph), paragraph),)
        sentences: list[tuple[int, int, str]] = []
        pattern = r"[^\u3002\uff01\uff1f!?;\uff1b\n]+[\u3002\uff01\uff1f!?;\uff1b]*"
        for match in re.finditer(pattern, paragraph):
            raw = match.group()
            if not raw.strip():
                continue
            start = match.start() + len(raw) - len(raw.lstrip())
            text = raw.strip()
            sentences.append((base_offset + start, base_offset + start + len(text), text))
        windows: list[tuple[int, int, str]] = []
        window: list[tuple[int, int, str]] = []
        window_chars = 0
        for sentence in sentences:
            if window and window_chars + len(sentence[2]) > SEMANTIC_SUPPORT_SLICE_MAX_CHARS:
                windows.append(
                    (
                        window[0][0],
                        window[-1][1],
                        paragraph[window[0][0] - base_offset : window[-1][1] - base_offset],
                    )
                )
                window = []
                window_chars = 0
            window.append(sentence)
            window_chars += len(sentence[2])
        if window:
            windows.append(
                (
                    window[0][0],
                    window[-1][1],
                    paragraph[window[0][0] - base_offset : window[-1][1] - base_offset],
                )
            )
        return tuple((start, end, text) for start, end, text in windows if text and start < end)

    def _pack_workset(
        self,
        slices: Sequence[EvidenceSlice],
        *,
        need: Stage1MemoryNeed,
        token_counter: TokenCounter,
    ) -> tuple[tuple[EvidenceSlice, ...], int, tuple[_AuditRow, ...]]:
        """Pack as many short exact slices as fit the explicit token budget.

        Selection is bounded only by the token budget, never by a fixed item
        count.  When not every legal slice fits, the packer gives every legal
        source chapter a fair token share (round-robin in pool order) so one
        large block cannot starve a deep-rank chapter's paragraphs, then fills
        the remaining budget by stable retrieval order with public query
        relevance as a tie-break.  Relevance never disqualifies a slice: every
        legal deep-rank slice stays eligible, and a slice is dropped only when
        the explicit token budget cannot hold it.  The returned audit rows
        retain the packed membership and the typed budget drops.
        """

        budget = SEMANTIC_SUPPORT_WORKSET_TOKEN_BUDGET
        terms = self._query_terms(need.query_text)

        def relevance(slice_: EvidenceSlice) -> int:
            folded = slice_.text.casefold()
            return sum(term in folded for term in terms)

        chapters = tuple(dict.fromkeys(slice_.chapter_id for slice_ in slices))
        by_chapter: dict[StableId | None, list[EvidenceSlice]] = {
            chapter_id: [] for chapter_id in chapters
        }
        for slice_ in slices:
            by_chapter[slice_.chapter_id].append(slice_)
        for chapter_id in chapters:
            by_chapter[chapter_id].sort(
                key=lambda slice_: (
                    slice_.start,
                    slice_.retrieval_order,
                    slice_.slice_id.root,
                )
            )
        # Diversity pass: each distinct source chapter gets its single most
        # query-relevant slice first, so legal source/chapter diversity enters
        # before budget pressure.
        kept: list[EvidenceSlice] = []
        tokens = 0
        for chapter_id in chapters:
            best = min(
                by_chapter[chapter_id],
                key=lambda slice_: (
                    -relevance(slice_),
                    slice_.retrieval_order,
                    slice_.slice_id.root,
                ),
            )
            cost = max(1, token_counter(best.text))
            if tokens + cost > budget:
                continue
            kept.append(best)
            tokens += cost
        # Fair-share pass: round-robin one slice per chapter so a deep-rank
        # chapter's middle paragraphs are not starved by earlier chapters.
        pointers = {chapter_id: 0 for chapter_id in chapters}
        for _round in range(max((len(items) for items in by_chapter.values()), default=0)):
            advanced = False
            for chapter_id in chapters:
                index = pointers[chapter_id]
                if index >= len(by_chapter[chapter_id]):
                    continue
                candidate = by_chapter[chapter_id][index]
                pointers[chapter_id] = index + 1
                cost = max(1, token_counter(candidate.text))
                if tokens + cost > budget:
                    continue
                if candidate not in kept:
                    kept.append(candidate)
                    tokens += cost
                advanced = True
            if not advanced:
                break
        # Fill pass: remaining slices by stable retrieval order with public
        # query relevance as a tie-break; a slice is dropped only when the
        # explicit token budget cannot hold it.
        for slice_ in sorted(
            (slice_ for slice_ in slices if slice_ not in kept),
            key=lambda slice_: (
                slice_.retrieval_order,
                -relevance(slice_),
                slice_.slice_id.root,
            ),
        ):
            cost = max(1, token_counter(slice_.text))
            if tokens + cost > budget:
                continue
            kept.append(slice_)
            tokens += cost
        kept_ids = {slice_.slice_id for slice_ in kept}
        workset_rows: list[_AuditRow] = []
        for slice_ in slices:
            workset_rows.append(
                self._slice_audit_row(
                    stage=AUDIT_SUPPORT_WORKSET_PACKED,
                    slice_=slice_,
                    order=len(workset_rows),
                    cost=len(slice_.text.encode("utf-8")),
                    origin_need_ids=(),
                    drop_reason=(None if slice_.slice_id in kept_ids else "workset_budget_drop"),
                )
            )
        dropped = sum(1 for slice_ in slices if slice_.slice_id not in kept_ids)
        return tuple(kept), dropped, tuple(workset_rows)

    def _single_slice_window(
        self,
        workset: Sequence[EvidenceSlice],
        *,
        token_counter: TokenCounter,
    ) -> tuple[EvidenceSlice, ...]:
        """A bounded probe window for the single-slice sufficiency call.

        The probe only judges whether one supplied slice directly expresses the
        complete conclusion, so it packs the workset's leading slices up to the
        dedicated single-slice input budget; the full workset stays available
        for the on-demand multi-slice synthesis.
        """

        budget = SEMANTIC_SUPPORT_SINGLE_SLICE_INPUT_TOKEN_BUDGET
        packed: list[EvidenceSlice] = []
        tokens = 0
        for slice_ in workset:
            cost = max(1, token_counter(slice_.text))
            if tokens + cost > budget:
                break
            packed.append(slice_)
            tokens += cost
        return tuple(packed)

    def _propose_single_slice(
        self,
        *,
        task: BenchmarkTaskContract,
        need: Stage1MemoryNeed,
        workset: Sequence[EvidenceSlice],
        entry: dict[str, object],
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        funnel: SupportFunnel,
    ) -> _SliceProposalAudit | None:
        """One serialized-request-bounded single-slice proposal request."""
        assert self._semantic_gateway is not None
        prompt = self._serialized_request_input(
            task=task,
            basis_commit_id=basis_commit_id,
            basis_snapshot_id=basis_snapshot_id,
            entry=entry,
            need=need,
            slices=workset,
            template=_SINGLE_SLICE_PROMPT_TEMPLATE,
        )
        prompt_bytes = prompt.encode("utf-8")
        estimated_input_tokens = self._estimate_prompt_tokens(prompt)
        input_hash = sha256_id(prompt_bytes)
        suffix = input_hash.root.removeprefix("sha256:")[:24]
        funnel.proposal_requests += 1
        proposal_request = ModelRequest(
            request_id=StableId(f"support-single-slice-proposal.{suffix}"),
            run_id=need.run_id,
            task_id=need.task_id,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.BATCH_TEST,
            trace_id=(f"stage2m-support-single:{task.task_id.root}:{suffix}"),
            prompt=prompt,
            max_output_tokens=SEMANTIC_SUPPORT_PROPOSAL_MAX_OUTPUT_TOKENS,
            timeout_seconds=SEMANTIC_SUPPORT_PROPOSAL_TIMEOUT_SECONDS,
            enable_thinking=False,
        )
        budget_fields: dict[str, object] = {
            "estimated_input_tokens": estimated_input_tokens,
            "max_output_tokens": SEMANTIC_SUPPORT_PROPOSAL_MAX_OUTPUT_TOKENS,
            "timeout_seconds": SEMANTIC_SUPPORT_PROPOSAL_TIMEOUT_SECONDS,
            "applied_input_token_budget": SEMANTIC_SUPPORT_SERIALIZED_REQUEST_TOKEN_BUDGET,
        }
        try:
            result = asyncio.run(self._semantic_gateway.generate_text(proposal_request))
            batch = SingleSliceProposalBatch.model_validate_json(
                self._extract_json_payload(result.text)
            )
            call = result.call_record
        except ValidationError as error:
            funnel.proposals_rejected += 1
            diagnostic = _FailedCallDiagnostic(
                category="invalid_structured_content",
                detail=self._sanitize_error_message(str(error)),
                failed_input_ref=self._retain_bytes(
                    prompt_bytes,
                    "application/vnd.novel-agent.support-proposal-prompt+text",
                ),
            )
            self._record_progress(
                stage="proposal",
                batch_index=funnel.proposal_requests,
                status="failed",
                error_type="ValidationError",
                need_ids=[need.need_id.root],
                slice_unit_ids=[slice_.slice_id.root for slice_ in workset],
                input_hash=input_hash.root,
                prompt_bytes=len(prompt_bytes),
                request_id=proposal_request.request_id.root,
                failed_call=diagnostic.as_dict(),
                **budget_fields,
            )
            return None
        except Exception as error:
            funnel.proposal_transport_failures += 1
            funnel.slices_not_proposed_transport += len(workset)
            funnel.affected_need_ids = (*funnel.affected_need_ids, need.need_id.root)
            funnel.affected_slice_counts = (*funnel.affected_slice_counts, len(workset))
            classified = self._classify_failed_call(error)
            diagnostic = _FailedCallDiagnostic(
                category=classified.category,
                detail=classified.detail,
                status_code=classified.status_code,
                retry_count=classified.retry_count,
                failed_input_ref=self._retain_bytes(
                    prompt_bytes,
                    "application/vnd.novel-agent.support-proposal-prompt+text",
                ),
            )
            self._record_progress(
                stage="proposal",
                batch_index=funnel.proposal_requests,
                status="failed",
                error_type=type(error).__name__,
                need_ids=[need.need_id.root],
                slice_unit_ids=[slice_.slice_id.root for slice_ in workset],
                input_hash=input_hash.root,
                prompt_bytes=len(prompt_bytes),
                request_id=proposal_request.request_id.root,
                failed_call=diagnostic.as_dict(),
                **budget_fields,
            )
            self.last_diagnostic_codes = tuple(
                dict.fromkeys(
                    (
                        *self.last_diagnostic_codes,
                        f"PRODUCER_SINGLE_SLICE_{type(error).__name__.upper()}",
                    )
                )
            )
            return None
        raw_output = self._semantic_gateway.raw_responses.get(proposal_request.request_id.root)
        if raw_output is None:
            funnel.proposal_transport_failures += 1
            funnel.slices_not_proposed_transport += len(workset)
            self.last_diagnostic_codes = tuple(
                dict.fromkeys(
                    (*self.last_diagnostic_codes, "PRODUCER_SINGLE_SLICE_RAW_OUTPUT_MISSING")
                )
            )
            return None
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
        declared_insufficient_ids = tuple(item.root for item in batch.insufficient_need_ids)
        if need.need_id.root in declared_insufficient_ids:
            funnel.needs_insufficient += 1
        if len(batch.claims) != 1:
            funnel.proposals_rejected += 1
            return None
        self._record_progress(
            stage="proposal",
            batch_index=funnel.proposal_requests,
            status="completed",
            input_hash=input_hash.root,
            output_hash=output_hash.root,
            need_ids=[need.need_id.root],
            slice_unit_ids=[slice_.slice_id.root for slice_ in workset],
            request_id=proposal_request.request_id.root,
            prompt_bytes=len(prompt_bytes),
            **budget_fields,
        )
        return _SliceProposalAudit(
            batch=batch,
            call=call,
            input_hash=input_hash,
            output_hash=output_hash,
            input_ref=input_ref,
            output_ref=output_ref,
            single_slice=True,
        )

    def _workset_chunks(
        self,
        workset: Sequence[EvidenceSlice],
        *,
        task: BenchmarkTaskContract,
        need: Stage1MemoryNeed,
        entry: dict[str, object],
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
    ) -> tuple[tuple[EvidenceSlice, ...], ...]:
        """Partition one Need's workset into serialized-request-bounded chunks.

        The union of all chunks is the Need's full SupportWorkset; each chunk
        stays within the serialized-request token budget so one synthesis
        request reliably fits the endpoint.  The budget covers the complete
        serialized prompt: task and Need/facet data, slice identities and
        metadata, slice text, instructions, structured-output framing, and
        output headroom (``max_output_tokens``).  A transport failure fails
        only the affected chunk's slices.
        """

        output_headroom = SEMANTIC_SUPPORT_MULTI_SLICE_PROPOSAL_MAX_OUTPUT_TOKENS
        prompt_budget = max(
            1,
            SEMANTIC_SUPPORT_SERIALIZED_REQUEST_TOKEN_BUDGET - output_headroom,
        )
        # The fixed envelope (task JSON, Need/facet fields, instructions,
        # PUBLIC_SUPPORT_INPUT tags) is measured once with an empty slice
        # list; each slice's marginal cost is its JSON fragment plus framing.
        fixed_cost = self._estimate_prompt_tokens(
            self._serialized_request_input(
                task=task,
                basis_commit_id=basis_commit_id,
                basis_snapshot_id=basis_snapshot_id,
                entry=entry,
                need=need,
                slices=(),
                template=_MULTI_SLICE_PROMPT_TEMPLATE,
            )
        )
        per_slice_costs = [
            self._estimate_prompt_tokens(
                json.dumps(
                    {
                        "slice_unit_id": slice_.slice_id.root,
                        "chapter_id": (
                            slice_.chapter_id.root if slice_.chapter_id is not None else None
                        ),
                        "start": slice_.start,
                        "end": slice_.end,
                        "text": slice_.text,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            + 1
            for slice_ in workset
        ]
        chunks: list[tuple[EvidenceSlice, ...]] = []
        current: list[EvidenceSlice] = []
        tokens = 0
        for slice_, cost in zip(workset, per_slice_costs, strict=True):
            if current and fixed_cost + tokens + cost > prompt_budget:
                chunks.append(tuple(current))
                current = []
                tokens = 0
            current.append(slice_)
            tokens += cost
        if current:
            chunks.append(tuple(current))
        return tuple(chunks)

    def _propose_multi_slice(
        self,
        *,
        task: BenchmarkTaskContract,
        need: Stage1MemoryNeed,
        workset: Sequence[EvidenceSlice],
        entry: dict[str, object],
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        funnel: SupportFunnel,
    ) -> _SliceProposalAudit | None:
        """One serialized-request-bounded multi-slice synthesis request."""
        assert self._semantic_gateway is not None
        prompt = self._serialized_request_input(
            task=task,
            basis_commit_id=basis_commit_id,
            basis_snapshot_id=basis_snapshot_id,
            entry=entry,
            need=need,
            slices=workset,
            template=_MULTI_SLICE_PROMPT_TEMPLATE,
        )
        prompt_bytes = prompt.encode("utf-8")
        estimated_input_tokens = self._estimate_prompt_tokens(prompt)
        input_hash = sha256_id(prompt_bytes)
        suffix = input_hash.root.removeprefix("sha256:")[:24]
        funnel.proposal_requests += 1
        proposal_request = ModelRequest(
            request_id=StableId(f"support-multi-slice-proposal.{suffix}"),
            run_id=need.run_id,
            task_id=need.task_id,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.BATCH_TEST,
            trace_id=(f"stage2m-support-multi:{task.task_id.root}:{suffix}"),
            prompt=prompt,
            max_output_tokens=SEMANTIC_SUPPORT_MULTI_SLICE_PROPOSAL_MAX_OUTPUT_TOKENS,
            timeout_seconds=SEMANTIC_SUPPORT_MULTI_SLICE_PROPOSAL_TIMEOUT_SECONDS,
            enable_thinking=True,
            thinking_token_budget=SEMANTIC_SUPPORT_MULTI_SLICE_THINKING_TOKEN_BUDGET,
        )
        budget_fields: dict[str, object] = {
            "estimated_input_tokens": estimated_input_tokens,
            "max_output_tokens": SEMANTIC_SUPPORT_MULTI_SLICE_PROPOSAL_MAX_OUTPUT_TOKENS,
            "timeout_seconds": SEMANTIC_SUPPORT_MULTI_SLICE_PROPOSAL_TIMEOUT_SECONDS,
            "applied_input_token_budget": SEMANTIC_SUPPORT_SERIALIZED_REQUEST_TOKEN_BUDGET,
        }
        try:
            result = asyncio.run(self._semantic_gateway.generate_text(proposal_request))
            batch = MultiSliceProposalBatch.model_validate_json(
                self._extract_json_payload(result.text)
            )
            call = result.call_record
        except ValidationError as error:
            funnel.proposals_rejected += 1
            diagnostic = _FailedCallDiagnostic(
                category="invalid_structured_content",
                detail=self._sanitize_error_message(str(error)),
                failed_input_ref=self._retain_bytes(
                    prompt_bytes,
                    "application/vnd.novel-agent.support-proposal-prompt+text",
                ),
            )
            self._record_progress(
                stage="proposal",
                batch_index=funnel.proposal_requests,
                status="failed",
                error_type="ValidationError",
                need_ids=[need.need_id.root],
                slice_unit_ids=[slice_.slice_id.root for slice_ in workset],
                input_hash=input_hash.root,
                prompt_bytes=len(prompt_bytes),
                request_id=proposal_request.request_id.root,
                failed_call=diagnostic.as_dict(),
                **budget_fields,
            )
            return None
        except Exception as error:
            funnel.proposal_transport_failures += 1
            funnel.slices_not_proposed_transport += len(workset)
            funnel.affected_need_ids = (*funnel.affected_need_ids, need.need_id.root)
            funnel.affected_slice_counts = (*funnel.affected_slice_counts, len(workset))
            classified = self._classify_failed_call(error)
            diagnostic = _FailedCallDiagnostic(
                category=classified.category,
                detail=classified.detail,
                status_code=classified.status_code,
                retry_count=classified.retry_count,
                failed_input_ref=self._retain_bytes(
                    prompt_bytes,
                    "application/vnd.novel-agent.support-proposal-prompt+text",
                ),
            )
            self._record_progress(
                stage="proposal",
                batch_index=funnel.proposal_requests,
                status="failed",
                error_type=type(error).__name__,
                need_ids=[need.need_id.root],
                slice_unit_ids=[slice_.slice_id.root for slice_ in workset],
                input_hash=input_hash.root,
                prompt_bytes=len(prompt_bytes),
                request_id=proposal_request.request_id.root,
                failed_call=diagnostic.as_dict(),
                **budget_fields,
            )
            self.last_diagnostic_codes = tuple(
                dict.fromkeys(
                    (
                        *self.last_diagnostic_codes,
                        f"PRODUCER_MULTI_SLICE_{type(error).__name__.upper()}",
                    )
                )
            )
            return None
        raw_output = self._semantic_gateway.raw_responses.get(proposal_request.request_id.root)
        if raw_output is None:
            funnel.proposal_transport_failures += 1
            funnel.slices_not_proposed_transport += len(workset)
            self.last_diagnostic_codes = tuple(
                dict.fromkeys(
                    (*self.last_diagnostic_codes, "PRODUCER_MULTI_SLICE_RAW_OUTPUT_MISSING")
                )
            )
            return None
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
        if need.need_id.root in tuple(item.root for item in batch.insufficient_need_ids):
            funnel.needs_insufficient += 1
        if len(batch.claims) != 1:
            funnel.proposals_rejected += 1
            return None
        self._record_progress(
            stage="proposal",
            batch_index=funnel.proposal_requests,
            status="completed",
            input_hash=input_hash.root,
            output_hash=output_hash.root,
            need_ids=[need.need_id.root],
            slice_unit_ids=[slice_.slice_id.root for slice_ in workset],
            request_id=proposal_request.request_id.root,
            prompt_bytes=len(prompt_bytes),
            **budget_fields,
        )
        return _SliceProposalAudit(
            batch=batch,
            call=call,
            input_hash=input_hash,
            output_hash=output_hash,
            input_ref=input_ref,
            output_ref=output_ref,
        )

    def _verify_claim_whole(
        self,
        *,
        task: BenchmarkTaskContract,
        need: Stage1MemoryNeed,
        claim_text: str,
        facet_ids: tuple[StableId, ...],
        cited_slices: Sequence[EvidenceSlice],
        context_slices: Sequence[EvidenceSlice],
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        funnel: SupportFunnel,
    ) -> _SemanticVerificationAudit | None:
        """Independently verify a complete claim over its cited exact slices.

        The verifier sees the complete claim, every cited exact slice, and a
        bounded compatible counter-evidence context.  Unknown IDs, missing
        decisions, contradictions, or unsupported clauses fail only this claim.
        """

        assert self._semantic_gateway is not None
        cited_ids = {slice_.slice_id for slice_ in cited_slices}
        context_units = tuple(
            dict.fromkeys(
                (
                    *tuple(slice_.slice_id for slice_ in cited_slices),
                    *tuple(
                        slice_.slice_id
                        for slice_ in context_slices[:SEMANTIC_SUPPORT_VERIFIER_CONTEXT_UNIT_LIMIT]
                    ),
                )
            )
        )
        evidence_units = [
            {
                "retrieval_unit_id": slice_.slice_id.root,
                "text": slice_.text,
            }
            for slice_ in cited_slices
        ]
        context_items = [
            {
                "retrieval_unit_id": slice_.slice_id.root,
                "cited_in_claim": slice_.slice_id in cited_ids,
                "text": slice_.text,
            }
            for slice_ in (
                *cited_slices,
                *context_slices,
            )[:SEMANTIC_SUPPORT_VERIFIER_CONTEXT_UNIT_LIMIT]
        ]
        claim_item: dict[str, object] = {
            "claim_index": 0,
            "need_id": need.need_id.root,
            "claim_text": claim_text,
            "need_facets": [
                {
                    "need_facet_id": facet.need_facet_id.root,
                    "facet_kind": facet.facet_kind.value,
                    "expected_claim_scope": facet.expected_claim_scope.value,
                }
                for facet in need.need_facets
                if facet.need_facet_id in set(facet_ids)
            ],
            "evidence_units": evidence_units,
            "context_units": context_items,
        }
        verifier_input = {
            "task": task.model_dump(mode="json"),
            "basis_commit_id": basis_commit_id.root,
            "basis_snapshot_id": basis_snapshot_id.root,
            "claim_proposal_hashes": [item.root for item in context_units],
            "claims": [claim_item],
        }
        verifier_input_bytes = canonical_json_bytes(verifier_input)
        verifier_prompt = (
            "You are an independent pre-freeze semantic support verifier. You did not "
            "write the candidate claim. Judge the claim only against its supplied "
            "cutoff-safe exact evidence slices and public Need facets. supports=true only "
            "when all material clauses, quantities, negation, epistemic scope, causality, "
            "limitations, unresolved status, and relationship direction are directly "
            "entailed. Treat facet kinds as questions to resolve, not asserted values. "
            "Reject a claim that calls a matter unresolved merely because its facet kind "
            "is unresolved_status, or that lets an earlier plan, wish, or promise override "
            "a supplied observed/current state establishing fulfillment or a current "
            "relationship. Plausibility or partial support is false. context_units lists "
            "every cutoff-safe slice for the same Need that the claim may legally cite, "
            "including slices the claim did not cite. If supplied evidence or any context "
            "unit contradicts a claim, set supports=false and copy the contradicting unit "
            "IDs into counter_evidence_retrieval_unit_ids. Those IDs must be copied "
            "verbatim from context_units. Return exactly one decision for "
            "claim_index 0.\n"
            '<PUBLIC_SUPPORT_VERIFICATION_INPUT trusted="false">\n'
            + verifier_input_bytes.decode("utf-8")
            + "\n</PUBLIC_SUPPORT_VERIFICATION_INPUT>"
        )
        verifier_prompt_bytes = verifier_prompt.encode("utf-8")
        verifier_input_hash = sha256_id(verifier_prompt_bytes)
        cached = self._verification_cache.get(verifier_input_hash.root)
        if cached is not None:
            verification, call, input_hash, output_hash, input_ref, output_ref = cached
        else:
            suffix = verifier_input_hash.root.removeprefix("sha256:")[:24]
            verifier_request = ModelRequest(
                request_id=StableId(f"support-whole-verification.{suffix}"),
                run_id=need.run_id,
                task_id=need.task_id,
                model_role=ModelRole.BATCH_TEST,
                purpose=ModelCallPurpose.BATCH_TEST,
                trace_id=(f"stage2m-support-whole:{task.task_id.root}:{suffix}"),
                prompt=verifier_prompt,
                max_output_tokens=SEMANTIC_SUPPORT_VERIFICATION_MAX_OUTPUT_TOKENS,
                timeout_seconds=SEMANTIC_SUPPORT_VERIFICATION_TIMEOUT_SECONDS,
                enable_thinking=False,
            )
            try:
                verification, call = asyncio.run(
                    self._semantic_gateway.generate_structured(
                        verifier_request,
                        SemanticSupportVerificationBatch,
                    )
                )
            except Exception as error:
                classified = self._classify_failed_call(error)
                diagnostic = _FailedCallDiagnostic(
                    category=classified.category,
                    detail=classified.detail,
                    status_code=classified.status_code,
                    retry_count=classified.retry_count,
                    failed_input_ref=self._retain_bytes(
                        verifier_prompt_bytes,
                        "application/vnd.novel-agent.support-verification-prompt+text",
                    ),
                )
                self._record_progress(
                    stage="verification",
                    batch_index=0,
                    status="failed",
                    error_type=type(error).__name__,
                    request_id=verifier_request.request_id.root,
                    input_hash=verifier_input_hash.root,
                    prompt_bytes=len(verifier_prompt_bytes),
                    estimated_input_tokens=self._estimate_prompt_tokens(verifier_prompt),
                    max_output_tokens=SEMANTIC_SUPPORT_VERIFICATION_MAX_OUTPUT_TOKENS,
                    timeout_seconds=SEMANTIC_SUPPORT_VERIFICATION_TIMEOUT_SECONDS,
                    failed_call=diagnostic.as_dict(),
                )
                self.last_diagnostic_codes = tuple(
                    dict.fromkeys(
                        (
                            *self.last_diagnostic_codes,
                            f"SEMANTIC_SUPPORT_WHOLE_VERIFIER_{type(error).__name__.upper()}",
                        )
                    )
                )
                funnel.verifier_transport_failures += 1
                return None
            raw_verifier_output = self._semantic_gateway.raw_responses.get(
                verifier_request.request_id.root
            )
            if raw_verifier_output is None:
                self.last_diagnostic_codes = tuple(
                    dict.fromkeys(
                        (
                            *self.last_diagnostic_codes,
                            "SEMANTIC_SUPPORT_WHOLE_VERIFIER_RAW_OUTPUT_MISSING",
                        )
                    )
                )
                funnel.verifier_transport_failures += 1
                return None
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
            cached = (
                verification,
                call,
                verifier_input_hash,
                verifier_output_hash,
                verifier_input_ref,
                verifier_output_ref,
            )
            self._verification_cache[verifier_input_hash.root] = cached
        verification, call, input_hash, output_hash, input_ref, output_ref = cached
        self._record_progress(
            stage="verification",
            batch_index=0,
            status="completed",
            input_hash=input_hash.root,
            output_hash=output_hash.root,
            decision_count=len(verification.decisions),
        )
        if not verification.decisions or len(verification.decisions) != 1:
            self.last_diagnostic_codes = tuple(
                dict.fromkeys(
                    (*self.last_diagnostic_codes, "SEMANTIC_SUPPORT_WHOLE_INCOMPLETE_DECISIONS")
                )
            )
            return None
        decision = verification.decisions[0]
        if decision.claim_index != 0:
            self.last_diagnostic_codes = tuple(
                dict.fromkeys(
                    (*self.last_diagnostic_codes, "SEMANTIC_SUPPORT_WHOLE_INVALID_DECISION_INDEX")
                )
            )
            return None
        if not decision.supports or decision.counter_evidence_retrieval_unit_ids:
            self.last_diagnostic_codes = tuple(
                dict.fromkeys(
                    (*self.last_diagnostic_codes, "SEMANTIC_SUPPORT_WHOLE_VERIFIER_REJECTED")
                )
            )
            funnel.whole_verifier_rejected += 1
            return None
        return _SemanticVerificationAudit(
            decision=decision,
            call=call,
            input_hash=input_hash,
            output_hash=output_hash,
            input_ref=input_ref,
            output_ref=output_ref,
        )

    def _emit_verified_claim(
        self,
        *,
        need: Stage1MemoryNeed,
        claim_text: str,
        unit_ids: tuple[StableId, ...],
        evidence_refs: tuple[EvidenceRef, ...],
        facet_ids: tuple[StableId, ...],
        audit: _SemanticVerificationAudit,
        proposal_audit: _SliceProposalAudit | None,
        task: BenchmarkTaskContract,
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        token_counter: TokenCounter,
        groups: list[ClaimSupportGroup],
        variants: list[ClaimVariant],
        receipts: list[ClaimSupportReceipt],
        attestations: list[CutoffAttestation],
        producer_marker: str | None,
    ) -> None:
        cleaned = self._clean_claim(claim_text)
        if not cleaned:
            return
        plan_node_ids: tuple[StableId, ...] = ()
        identity = canonical_json_bytes(
            {
                "need_id": need.need_id.root,
                "facets": [item.root for item in facet_ids],
                "units": [item.root for item in unit_ids],
                "claim": cleaned,
                "verifier_input_hash": audit.input_hash.root,
                "verifier_output_hash": audit.output_hash.root,
            }
        )
        digest = sha256_id(identity).root.removeprefix("sha256:")
        claim_id = StableId(f"claim.{digest[:48]}")
        group_id = StableId(f"support-group.{digest[:48]}")
        information_scope = "writer_safe"
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
        producer_identity = f"{self.version}.{producer_marker}" if producer_marker else self.version
        receipt = ClaimSupportReceipt(
            receipt_id=StableId(f"support-receipt.{digest[:48]}"),
            support_group_id=group_id,
            claim_id=claim_id,
            claim_text_hash=sha256_id(cleaned.encode("utf-8")),
            need_ids=(need.need_id,),
            need_facet_ids=facet_ids,
            retrieval_unit_ids=unit_ids,
            evidence_refs=evidence_refs,
            plan_node_ids=plan_node_ids,
            evidence_resolution_status=EvidenceResolutionStatus.RESOLVED,
            semantic_support_status=SemanticSupportStatus.VERIFIED,
            basis_commit_id=basis_commit_id,
            basis_snapshot_id=basis_snapshot_id,
            cutoff_attestation_ref=attestation_ref,
            information_scope=information_scope,
            producer=producer_identity,
            producer_version=producer_marker or self.version,
            producer_input_hash=proposal_audit.input_hash if proposal_audit else None,
            producer_output_hash=proposal_audit.output_hash if proposal_audit else None,
            producer_input_ref=proposal_audit.input_ref if proposal_audit else None,
            producer_output_ref=proposal_audit.output_ref if proposal_audit else None,
            model_call_record=proposal_audit.call if proposal_audit else None,
            verifier_input_hash=audit.input_hash,
            verifier_output_hash=audit.output_hash,
            verifier_input_ref=audit.input_ref,
            verifier_output_ref=audit.output_ref,
            verification_model_call_record=audit.call,
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
                evidence_resolution_status=EvidenceResolutionStatus.RESOLVED,
                semantic_support_status=SemanticSupportStatus.VERIFIED,
                support_receipt_ref=receipt_ref,
                producer=producer_identity,
                producer_version=producer_marker or self.version,
                cutoff_attestation_ref=attestation_ref,
            )
        )
        variants.append(
            ClaimVariant(
                claim_variant_id=StableId(f"claim-variant.{digest[:48]}"),
                claim_id=claim_id,
                support_group_id=group_id,
                claim_text=cleaned,
                claim_text_hash=receipt.claim_text_hash,
                covered_need_facet_ids=facet_ids,
                support_receipt_ref=receipt_ref,
                token_cost=max(1, token_counter(cleaned)),
                reduction_level=ClaimReductionLevel.FULL,
                producer=producer_identity,
                producer_version=producer_marker or self.version,
            )
        )
        receipts.append(receipt)
        attestations.append(attestation)

    @classmethod
    def _compatible_support_units(
        self,
        *,
        task: BenchmarkTaskContract,
        target_need: Stage1MemoryNeed,
        units: tuple[RetrievalUnit, ...],
        need_by_id: Mapping[StableId, Stage1MemoryNeed],
        units_by_need: Mapping[StableId, Sequence[RetrievalUnit]],
        origin_need_ids: Mapping[StableId, tuple[StableId, ...]],
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
    ) -> tuple[RetrievalUnit, ...]:
        """Return other-Need units that may legally join the target support pool.

        A non-direct unit is compatible only when it was already selected this
        round, has at least one recorded origin Need, passes the full legal
        scope/VAC lattice, resolves against the current checkpoint/basis, is
        taint-free, and shares a public anchor with the target Need. Public
        anchors are entity/focus intersection with the target or an origin
        Need, or an exact parent/child lineage relation to a direct unit of
        the target Need. The exact lineage path stays available even when the
        target Need has no entity/focus ids; without any public anchor at all
        the pool stays direct-only.
        """

        direct_bound = units_by_need.get(target_need.need_id, ())
        direct_ids = {unit.unit_id for unit in direct_bound}
        unit_ids_by_origin = {
            need_id: {unit.unit_id for unit in bound} for need_id, bound in units_by_need.items()
        }
        direct_anchor_parents = {
            parent
            for unit in direct_bound
            for parent in (unit.parent_unit_id, *unit.parent_unit_ids)
            if parent is not None
        }
        target_anchors = {*target_need.entity_ids, *target_need.focus_ids}
        has_public_anchors = bool(target_anchors)
        input_order = {unit.unit_id: index for index, unit in enumerate(units)}
        compatible: list[tuple[int, RetrievalUnit]] = []
        for unit in units:
            if unit.unit_id in direct_ids:
                continue
            origin_ids = origin_need_ids.get(unit.unit_id, ())
            if not origin_ids:
                continue
            if not self._legal_for_need(task, target_need, unit):
                continue
            if unit.source_commit != basis_commit_id or unit.snapshot_id != basis_snapshot_id:
                continue
            if unit.derivation_taint:
                continue
            resolution = self._resolution_status(
                unit.evidence_refs,
                unit,
                basis_commit_id=basis_commit_id,
                checkpoint_chapter=task.checkpoint_chapter,
                plan_node_ids=self._plan_node_ids(unit),
            )
            if resolution is not EvidenceResolutionStatus.RESOLVED:
                continue
            exact_lineage = (
                unit.unit_id in direct_anchor_parents
                or (unit.parent_unit_id is not None and unit.parent_unit_id in direct_ids)
                or bool(set(unit.parent_unit_ids).intersection(direct_ids))
            )
            if has_public_anchors and set(unit.entity_ids).intersection(target_anchors):
                anchor_tier = 0
            elif has_public_anchors and any(
                unit.unit_id in unit_ids_by_origin.get(origin_id, ())
                and (
                    set((*need_by_id[origin_id].entity_ids, *need_by_id[origin_id].focus_ids))
                ).intersection(target_anchors)
                for origin_id in origin_ids
            ):
                anchor_tier = 1
            elif exact_lineage:
                anchor_tier = 2
            else:
                continue
            compatible.append((anchor_tier, unit))
        return tuple(
            unit
            for _tier, unit in sorted(
                compatible,
                key=lambda item: (
                    0
                    if item[1].unit_kind
                    in {
                        RetrievalUnitKind.GROUNDED_BLOCK,
                        RetrievalUnitKind.GROUNDED_SPAN,
                    }
                    else 1,
                    item[0],
                    input_order[item[1].unit_id],
                    item[1].unit_id.root,
                ),
            )
        )

    @classmethod
    def _family_canonicalize(
        cls,
        units: Sequence[RetrievalUnit],
    ) -> tuple[tuple[RetrievalUnit, ...], set[StableId]]:
        """Collapse every representation of one L0 source family to one unit.

        Multiple selected units often carry the same canonical passage: a
        grounded block, its compact excerpt, and its relation/state anchors
        all share the block's L0 family identity.  Only the most canonical
        representative survives (full-passage grounded block/span first, then
        compact excerpts, then anchors), so a compact or anchor copy can never
        crowd out the canonical block it derives from.  The family keeps the
        earliest stable position so pool order stays deterministic.
        """

        kept_by_family: dict[str, RetrievalUnit] = {}
        order: list[RetrievalUnit] = []
        collapsed: set[StableId] = set()
        for unit in units:
            family = cls._unit_family_id(unit)
            kept = kept_by_family.get(family)
            if kept is None:
                kept_by_family[family] = unit
                order.append(unit)
                continue
            if cls._representation_canonical_score(unit) > cls._representation_canonical_score(
                kept
            ):
                order[order.index(kept)] = unit
                kept_by_family[family] = unit
                collapsed.add(kept.unit_id)
            else:
                collapsed.add(unit.unit_id)
        return tuple(order), collapsed

    @staticmethod
    def _representation_canonical_score(unit: RetrievalUnit) -> int:
        """Canonicality of one unit as the L0 raw-text representative.

        A compact excerpt is always a derived preview regardless of its unit
        kind (a compact carries the parent block's full-passage ref but only a
        truncated excerpt of the canonical text), so it scores below every
        exact unit.  A grounded block carrying its full-passage evidence ref
        is the most complete citation target; a plain grounded block follows
        (its own text is the canonical source, a superset of any exact span
        inside it); a precise grounded span is exact but narrower; anchors are
        the least canonical.
        """

        if unit.unit_id.root.startswith("compact."):
            return 2
        kind = getattr(unit.unit_kind, "value", str(unit.unit_kind))
        if (
            kind == RetrievalUnitKind.GROUNDED_BLOCK.value
            and TrustedClaimSupportProducer._has_full_passage_ref(unit)
        ):
            return 5
        if kind == RetrievalUnitKind.GROUNDED_BLOCK.value:
            return 4
        if (
            kind == RetrievalUnitKind.GROUNDED_SPAN.value
            and unit.evidence_refs
            and unit.evidence_refs[0].span is not None
        ):
            return 3
        return 0

    @classmethod
    def _chapter_diverse_order(
        cls,
        units: Sequence[RetrievalUnit],
    ) -> tuple[RetrievalUnit, ...]:
        """Order units so every legal source chapter keeps its leading handle.

        The first unit of each distinct source chapter leads in stable pool
        order; the remaining units follow in the same stable order.  A
        deep-rank chapter's first unit therefore survives the retrieval-handle
        budget before the budget fills with earlier chapters' copies.
        """

        first_by_chapter: dict[int | None, RetrievalUnit] = {}
        rest: list[RetrievalUnit] = []
        for unit in units:
            chapter = cls._chapter_index(unit)
            if chapter not in first_by_chapter:
                first_by_chapter[chapter] = unit
            else:
                rest.append(unit)
        return tuple((*first_by_chapter.values(), *rest))

    def _evidence_diverse_pool(
        self,
        direct_units: Sequence[RetrievalUnit],
        compatible_units: Sequence[RetrievalUnit],
    ) -> tuple[RetrievalUnit, ...]:
        """Collapse pool units that only duplicate evidence already retained.

        A unit is kept when it carries at least one evidence reference not
        covered by an earlier retained unit, or when it has no evidence refs
        at all (plan-provenance anchors and unresolved-safely-excluded units
        keep their own slot).  Direct units are processed before compatible
        units, so a compatible copy of an already-retained passage never
        displaces a distinct one.
        """

        kept: list[RetrievalUnit] = []
        covered: list[EvidenceRef] = []
        for unit in (*direct_units, *compatible_units):
            if unit.evidence_refs and not any(
                not self._evidence_ref_covered(reference, covered)
                for reference in unit.evidence_refs
            ):
                continue
            kept.append(unit)
            covered.extend(unit.evidence_refs)
        return tuple(kept)

    @staticmethod
    def _evidence_ref_covered(reference: EvidenceRef, covered: Sequence[EvidenceRef]) -> bool:
        """Return whether one reference is already represented by the kept set.

        Identity mirrors the provenance matcher: an exact evidence id, or the
        same object content with overlapping precise spans.  Spans must be
        present on both sides, which forbids whole-chapter coincidence.

        Coverage is measured against the reference's own span, not the
        narrower of the two: a curator sub-span inside a grounded block is a
        strict subset of the block's text, so it can never cover the block.
        Covering the block would silently drop the passages the sub-span does
        not contain (a later quote or the block body itself) from the pool.
        """

        for other in covered:
            if other.evidence_id == reference.evidence_id:
                return True
            if other.object_hash != reference.object_hash:
                continue
            if reference.span is None or other.span is None:
                continue
            reference_width = max(1, reference.span.end - reference.span.start)
            overlap = max(
                0,
                min(other.span.end, reference.span.end)
                - max(other.span.start, reference.span.start),
            )
            if overlap / reference_width >= 0.5:
                return True
        return False

    @staticmethod
    def _has_full_passage_ref(unit: RetrievalUnit) -> bool:
        """Return whether the unit carries a whole-passage evidence reference.

        Full-passage refs (evidence.full.block.*) span the entire source
        passage, so they are the most complete citation target when several
        units share the same underlying passage content.
        """

        return any(
            isinstance(reference.evidence_id.root, str)
            and reference.evidence_id.root.startswith("evidence.full.block.")
            for reference in unit.evidence_refs
        )

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
        first = next((line for line in value.splitlines() if line.strip()), "")
        return re.sub(r"\s+", " ", first).strip()

    @staticmethod
    def _extract_json_payload(text: str) -> str:
        """Recover the JSON document from a thinking-mode model completion.

        The local Qwen3.6 endpoint with thinking enabled does not always apply
        the json_object grammar; the model sometimes wraps the document in a
        markdown code fence (`````json ... `````).  The host strips the fence
        deterministically before pydantic validation; anything else remains
        fail-closed.  Plain JSON documents pass through unchanged.
        """

        stripped = text.strip()
        if not stripped or stripped.startswith(("{", "[")):
            return stripped
        match = re.search(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", text, re.DOTALL)
        if match is not None:
            return match.group(1)
        return stripped

    @staticmethod
    def _reject_garbage_claim(claim_text: str) -> bool:
        """Return True for a claim that is purely punctuation or too short to be a conclusion.

        Strip Unicode whitespace, punctuation, and symbols, then count the
        remaining semantic characters.  Python's ``re`` has no ``\\p{...}``
        escape, so the removal is expressed as ``[\\s\\W]``: every Unicode
        whitespace, punctuation (``\\p{P}``), and symbol (``\\p{S}``) character
        is a non-word character, and the remaining count is the claim's
        semantic length.  This is a deterministic non-semantic guard only; it
        never compares claim text against slice text.
        """

        stripped = "".join(char for char in claim_text if not re.match(r"[\s\W]", char))
        return bool(not stripped or len(stripped.strip()) < 4)

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

    @staticmethod
    def _sanitize_error_message(message: str) -> str:
        """Strip URLs, credentials, and excessive length from one error text."""

        cleaned = re.sub(r"https?://[^\s\"']+", "[endpoint-url]", str(message))
        cleaned = re.sub(r"\b[\w.+-]+@[\w.-]+\.\w+\b", "[email]", cleaned)
        return cleaned[:500]

    def _classify_failed_call(self, error: Exception) -> _FailedCallDiagnostic:
        """Classify one failed semantic call without leaking private source text.

        The sanitized detail distinguishes HTTP status, connect/read timeout,
        retry exhaustion, output-length truncation, invalid JSON, and missing
        structured content.  The failed input is persisted under a
        content-addressed reference for the artifact store.
        """

        detail = self._sanitize_error_message(str(error))
        status_code: int | None = None
        match = re.search(r"HTTP (\d+)", detail)
        if match is not None:
            status_code = int(match.group(1))
            category = "http_status"
        elif "truncated by output length limit" in detail:
            category = "output_length_truncation"
        elif "not valid JSON" in detail:
            category = "invalid_json"
        elif "missing choices" in detail or "null or empty content" in detail:
            category = "missing_structured_content"
        else:
            category = "transport"
            cause: BaseException | None = error.__cause__
            while cause is not None:
                if isinstance(cause, httpx.TimeoutException):
                    category = "connect_read_timeout"
                    break
                if isinstance(cause, httpx.HTTPStatusError):
                    category = "http_status"
                    status_code = cause.response.status_code
                    break
                cause = cause.__cause__
        retry_count = 0
        if self._semantic_gateway is not None:
            adapter = self._semantic_gateway.endpoint_adapter(ModelRole.BATCH_TEST)
            retry_count = len(getattr(adapter, "attempts", ()))
        if category == "transport" and retry_count > 0:
            category = "retry_exhausted"
        return _FailedCallDiagnostic(
            category=category,
            detail=detail,
            status_code=status_code,
            retry_count=retry_count,
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
        # Funnel attribution: verified groups that the Controller did not
        # select are controller-dropped; the Writer's own packing drops are
        # attributed after assembly.
        verified_count = len(verified)
        self._producer.last_funnel.controller_dropped = max(0, verified_count - len(selected))
        workset_reports = tuple(
            report for report in self._producer.last_workset_reports if report.need_id in need_by_id
        )
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
            workset_reports=workset_reports,
            raw_evidence_ledger_entries=self._producer.last_raw_ledger_entries,
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
