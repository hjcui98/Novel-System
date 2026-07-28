"""Audited model-assisted Curator producing deterministic, evidence-bound changes."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Literal, cast

from pydantic import Field, create_model

from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import (
    ChangeOperation,
    ChangeOperationType,
    ChapterChangeDraft,
    ChapterChangeDraftV2,
    CuratedOperationDraft,
    CuratedOperationDraftV2,
    CuratorEventRecord,
    CuratorObligationRecord,
    CuratorRelationRecord,
    CuratorStateRecord,
    CuratorTypedRecord,
    EvidenceCandidate,
    EvidenceRepairAction,
    EvidenceRepairDraft,
    EvidenceSupportDecision,
    EvidenceSupportDisposition,
    ObservedChangeSet,
    WorldRecordKind,
)
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.memory_write import ProposalConflict, ProposalEvidenceMergeReceipt
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.benchmark_importer import validate_evidence_ref
from novel_agent.services.content_addressing import canonical_json_bytes, quote_hash
from novel_agent.services.curation import Stage1Curator
from novel_agent.services.evidence_candidates import EvidenceCandidateGenerator
from novel_agent.services.evidence_support import EvidenceSupportGate
from novel_agent.services.model_gateway import ModelGateway


class ModelCurationContractError(ValueError):
    pass


class CuratorProposalSemanticRejected(ModelCurationContractError):
    def __init__(
        self,
        reason_code: str,
        conflicts: tuple[ProposalConflict, ...],
        *,
        information_boundary: bool = False,
        safe_feedback: tuple[str, ...] = (),
        operation_indexes: tuple[int, ...] = (),
        json_pointers: tuple[str, ...] = (),
        violation_rule: str | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.conflicts = conflicts
        self.information_boundary = information_boundary
        self.safe_feedback = safe_feedback
        self.operation_indexes = operation_indexes
        self.json_pointers = json_pointers
        self.violation_rule = violation_rule


_RECORD_ID_FIELD = {
    WorldRecordKind.ENTITY: "entity_id",
    WorldRecordKind.EVENT: "event_id",
    WorldRecordKind.STATE: "state_id",
    WorldRecordKind.RELATION: "relation_id",
    WorldRecordKind.OBLIGATION: "obligation_id",
}


TargetResolver = Callable[[WorldRecordKind, StableId, WorldRootDocument], StableId]

SemanticVerifier = Callable[
    [CuratorTypedRecord, EvidenceCandidate],
    "tuple[EvidenceSupportDisposition, str] | None",
]

NoOpVerifier = Callable[
    [str, tuple[EvidenceCandidate, ...], WorldRootDocument],
    "tuple[bool, str] | None",
]


class EvidenceSemanticVerificationItem(DomainModel):
    operation_index: int
    candidate_ids: tuple[StableId, ...] = Field(min_length=1, max_length=4)
    disposition: EvidenceSupportDisposition
    reason_code: str = Field(min_length=1, max_length=160)


class EvidenceSemanticVerificationDraft(DomainModel):
    decisions: tuple[EvidenceSemanticVerificationItem, ...] = Field(max_length=4)


class ProposalOperationFilterReceipt(DomainModel):
    transform_id: StableId
    base_commit: CommitId
    operation_index: int
    record_kind: WorldRecordKind
    proposed_target_id: StableId
    existing_target_id: StableId | None = None
    reason: Literal[
        "existing_semantic_duplicate",
        "evidence_support_rejected",
    ]
    source_operation_hash: ArtifactId
    support_disposition: EvidenceSupportDisposition | None = None
    support_reason_code: str | None = None


class ModelCurator:
    def __init__(
        self,
        gateway: ModelGateway,
        *,
        target_resolver: TargetResolver | None = None,
        evidence_generator: EvidenceCandidateGenerator | None = None,
        support_gate: EvidenceSupportGate | None = None,
        enforce_support_gate: bool = True,
        semantic_verifier: SemanticVerifier | None = None,
        no_op_verifier: NoOpVerifier | None = None,
        enable_model_semantic_verifier: bool = False,
    ) -> None:
        self._gateway = gateway
        self._target_resolver = target_resolver or (lambda _kind, target_id, _world: target_id)
        self._evidence_generator = evidence_generator or EvidenceCandidateGenerator()
        self._support_gate = support_gate or EvidenceSupportGate()
        self._enforce_support_gate = enforce_support_gate
        self._semantic_verifier = semantic_verifier
        self._no_op_verifier = no_op_verifier
        self._enable_model_semantic_verifier = enable_model_semantic_verifier
        self.last_evidence_merge_receipts: tuple[ProposalEvidenceMergeReceipt, ...] = ()
        self.last_support_decisions: tuple[EvidenceSupportDecision, ...] = ()
        self.last_partial_support_decisions: tuple[EvidenceSupportDecision, ...] = ()
        self.last_evidence_candidates: tuple[EvidenceCandidate, ...] = ()
        self.last_no_op_verification: tuple[bool, str] | None = None
        self.last_prompt_fingerprint: ArtifactId | None = None
        self.last_operation_filter_receipts: tuple[
            ProposalOperationFilterReceipt, ...
        ] = ()

    @property
    def gateway(self) -> ModelGateway:
        return self._gateway

    async def extract(
        self,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        current_world: WorldRootDocument,
        request: ModelRequest,
    ) -> tuple[ObservedChangeSet, ModelCallRecord]:
        changes, call, _ = await self.extract_reported(
            text_root,
            chapter_index,
            base_commit,
            current_world,
            request,
        )
        return changes, call

    async def extract_reported(
        self,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        current_world: WorldRootDocument,
        request: ModelRequest,
        *,
        contract_prompt: str | None = None,
    ) -> tuple[ObservedChangeSet, ModelCallRecord, ChapterChangeDraft]:
        chapter = Stage1Curator._chapter(text_root, chapter_index)
        contract = f"{contract_prompt}\n\n" if contract_prompt else ""
        safe_request = request.model_copy(
            update={
                "prompt": (
                    contract + "Extract ChapterChangeDraft JSON from this revealed chapter only. "
                    "Every operation must cite exact EvidenceRef spans from the supplied chapter; "
                    "preserve assertion/rumor/dream truth classes and do not infer future events.\n"
                    '<CURATOR_INPUT trusted="false">\n'
                    f"BASE_COMMIT={base_commit.root}\n"
                    "WORLD="
                    f"{canonical_json_bytes(self._world_model_view(current_world)).decode()}\n"
                    f"CHAPTER={chapter.model_dump_json()}\n"
                    "</CURATOR_INPUT>"
                )
            }
        )
        self.last_prompt_fingerprint = sha256_id(safe_request.prompt.encode("utf-8"))
        draft, call = await self._gateway.generate_structured(safe_request, ChapterChangeDraft)
        if draft.chapter_index != chapter_index:
            raise ModelCurationContractError("Curator draft chapter differs from requested chapter")
        draft = self._normalize_operations(draft, current_world)
        chapter_blocks = {
            block.block_id: block for scene in chapter.scenes for block in scene.blocks
        }
        if any(
            selection.block_id not in chapter_blocks
            for operation in draft.operations
            for selection in operation.evidence_refs
        ):
            raise CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_INFORMATION_BOUNDARY",
                (),
                information_boundary=True,
            )
        invalid_selections = tuple(
            (
                selection.block_id,
                selection.start,
                selection.end,
                len(chapter_blocks[selection.block_id].text),
            )
            for operation in draft.operations
            for selection in operation.evidence_refs
            if not (selection.start < selection.end <= len(chapter_blocks[selection.block_id].text))
        )
        if invalid_selections:
            pointers = tuple(
                f"/operations/{op_index}/evidence_refs/{ev_index}"
                for op_index, operation in enumerate(draft.operations)
                for ev_index, selection in enumerate(operation.evidence_refs)
                if not (
                    selection.start
                    < selection.end
                    <= len(chapter_blocks[selection.block_id].text)
                )
            )
            op_indexes = tuple(
                sorted(
                    {
                        op_index
                        for op_index, operation in enumerate(draft.operations)
                        for selection in operation.evidence_refs
                        if not (
                            selection.start
                            < selection.end
                            <= len(chapter_blocks[selection.block_id].text)
                        )
                    }
                )
            )
            raise CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_INVALID_EVIDENCE",
                (),
                safe_feedback=tuple(
                    (
                        f"{block_id.root}: require 0 <= start < end <= {block_length}; "
                        f"received start={start}, end={end}"
                    )[:240]
                    for block_id, start, end, block_length in invalid_selections[:4]
                ),
                operation_indexes=op_indexes,
                json_pointers=pointers[:8],
                violation_rule="evidence_span_in_block_bounds",
            )
        draft, merge_receipts = self._merge_normalized_collisions(draft, base_commit)
        self.last_evidence_merge_receipts = merge_receipts

        operations: list[ChangeOperation] = []
        for operation in draft.operations:
            bound_evidence = []
            for selection in operation.evidence_refs:
                # The scope filter above guarantees every retained selection is
                # bound to a block in this chapter.
                block = chapter_blocks[selection.block_id]
                selected = block.text[selection.start : selection.end]
                evidence_digest = self._digest(
                    chapter.chapter_id.root.encode(),
                    selection.block_id.root.encode(),
                    str(selection.start).encode(),
                    str(selection.end).encode(),
                )
                canonical_evidence = EvidenceRef(
                    evidence_id=StableId(f"evidence.curator.{evidence_digest}"),
                    root_hash=text_root.root_hash,
                    object_hash=sha256_id(block.text.encode("utf-8")),
                    chapter_id=block.chapter_id,
                    scene_id=block.scene_id,
                    span=TextSpanRef.model_validate(selection.model_dump()),
                    quote_hash=quote_hash(selected),
                    resolved_at_commit=base_commit,
                    support_status=EvidenceSupportStatus.CURRENT,
                )
                validate_evidence_ref(canonical_evidence, text_root)
                bound_evidence.append(canonical_evidence)
            evidence_refs = tuple(bound_evidence)
            record = operation.record.model_dump(mode="json")
            record[_RECORD_ID_FIELD[operation.record_kind]] = operation.target_id.root
            if operation.record_kind is not WorldRecordKind.ENTITY:
                record["evidence_refs"] = [
                    evidence.model_dump(mode="json") for evidence in evidence_refs
                ]
            bound_operation = operation.model_copy(update={"evidence_refs": evidence_refs})
            digest = self._digest(canonical_json_bytes(bound_operation.model_dump(mode="json")))
            operations.append(
                ChangeOperation(
                    operation_id=StableId(f"change.model.{digest}"),
                    root_kind=RootKind.WORLD,
                    operation=operation.operation,
                    target_id=operation.target_id,
                    payload={
                        "record_type": operation.record_kind.value,
                        "record": record,
                    },
                    evidence_refs=evidence_refs,
                )
            )
        source_bytes = canonical_json_bytes(chapter.model_dump(mode="json"))
        return (
            ObservedChangeSet(
                change_set_id=StableId(
                    "changes.model."
                    f"{self._digest(base_commit.root.encode(), chapter.chapter_id.root.encode())}"
                ),
                base_commit=base_commit,
                source_artifact=ArtifactRef(
                    artifact_id=sha256_id(source_bytes),
                    media_type="application/vnd.novel-agent.chapter+json",
                    byte_length=len(source_bytes),
                    schema_version=SchemaVersion("0.1.0"),
                ),
                operations=tuple(operations),
            ),
            call,
            draft,
        )

    async def extract_reported_v2(
        self,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        current_world: WorldRootDocument,
        request: ModelRequest,
        *,
        contract_prompt: str | None = None,
    ) -> tuple[ObservedChangeSet, ModelCallRecord, ChapterChangeDraftV2]:
        """Candidate-id evidence contract: model never emits character offsets."""

        chapter = Stage1Curator._chapter(text_root, chapter_index)
        candidates = self._evidence_generator.generate(text_root, chapter_index)
        self.last_evidence_candidates = candidates
        catalog = self._evidence_generator.index_by_id(candidates)
        views = self._evidence_generator.model_views(candidates)
        contract = f"{contract_prompt}\n\n" if contract_prompt else ""
        safe_request = request.model_copy(
            update={
                "prompt": (
                    contract
                    + "Extract ChapterChangeDraftV2 JSON from this revealed chapter only. "
                    "The operations key is required. An empty operations array is valid only "
                    "for a complete no-durable-delta result: coverage must equal 1, unresolved "
                    "and declared_vs_observed_diff must be empty, and the draft must include "
                    "no_durable_delta_reason plus supporting no_op_evidence_candidate_ids. "
                    "Cite only registered evidence_candidate_ids; do not emit start/end offsets. "
                    "Preserve assertion/rumor/dream truth classes and do not infer future events. "
                    "Emit only durable world-state deltas: exclude one-scene encounters, "
                    "atmosphere, immediate perceptions, temporary emotions, plans, estimates, "
                    "and unresolved possibilities. Every predicate and value must describe "
                    "exactly what its cited evidence states; do not convert general rules, "
                    "hypotheticals, maxima, or other characters' achievements into a fact about "
                    "the subject.\n"
                    '<CURATOR_INPUT trusted="false">\n'
                    f"BASE_COMMIT={base_commit.root}\n"
                    "WORLD="
                    f"{canonical_json_bytes(self._world_model_view(current_world)).decode()}\n"
                    f"CHAPTER={chapter.model_dump_json()}\n"
                    "EVIDENCE_CANDIDATES="
                    f"{canonical_json_bytes([v.model_dump(mode='json') for v in views]).decode()}\n"
                    "</CURATOR_INPUT>\n"
                    '<CURATOR_OUTPUT_CONTRACT trusted="true">\n'
                    "Return at most four durable operations. Exclude transient encounters, "
                    "temporary feelings, estimates, plans, and unresolved possibilities. "
                    "Prefer one or two precise operations over filling the maximum. "
                    "Use only facts directly stated by each cited evidence candidate. "
                    "A composite method or process MUST cite the detail-bearing sentences "
                    "for every encoded step, usually with two to four candidate IDs; a "
                    "summary sentence such as 'this is the method' is not sufficient by "
                    "itself. Preserve source units exactly unless an explicit conversion is "
                    "certain: for example, half_shichen is not half_hour. Preserve epistemic "
                    "qualifiers: evidence saying believes, estimates, claims, or may must be "
                    "encoded as a belief/estimate/claim, never as an objective state. "
                    "Before emitting a composite value, verify that every semantic component "
                    "(including each underscore-separated component) has explicit support in "
                    "at least one selected evidence candidate. "
                    "Every evidence_candidate_id MUST be copied verbatim from the current "
                    "EVIDENCE_CANDIDATES catalog; never invent, reconstruct, hash, or reuse an "
                    "ID from another chapter. Do not restate facts already present in WORLD. "
                    "When operations is non-empty, no_durable_delta_reason MUST be null and "
                    "no_op_evidence_candidate_ids MUST be an empty array. Those two no-op "
                    "proof fields may be populated only when operations is empty.\n"
                    "</CURATOR_OUTPUT_CONTRACT>"
                )
            }
        )
        self.last_prompt_fingerprint = sha256_id(safe_request.prompt.encode("utf-8"))
        draft, call = await self._gateway.generate_structured(safe_request, ChapterChangeDraftV2)
        self.last_no_op_verification = None
        if draft.chapter_index != chapter_index:
            raise ModelCurationContractError("Curator draft chapter differs from requested chapter")
        proposed_operation_count = len(draft.operations)
        draft = self._filter_existing_semantic_duplicates(
            draft,
            current_world,
            base_commit,
        )
        duplicate_no_op = (
            proposed_operation_count > 0
            and not draft.operations
            and len(self.last_operation_filter_receipts) == proposed_operation_count
            and all(
                receipt.reason == "existing_semantic_duplicate"
                for receipt in self.last_operation_filter_receipts
            )
        )
        if duplicate_no_op:
            # A deterministic comparison against Canonical World is a stronger
            # no-op proof than the model's stale completeness fields. Keep the
            # filter receipts as the auditable proof and do not carry candidate
            # IDs from operations that cannot mutate Canonical World.
            draft = draft.model_copy(
                update={
                    "coverage": 1.0,
                    "unresolved": (),
                    "declared_vs_observed_diff": (),
                    "no_durable_delta_reason": (
                        "all proposed operations already exist in Canonical World"
                    ),
                    "no_op_evidence_candidate_ids": (),
                }
            )
            self.last_no_op_verification = (
                True,
                "ALL_OPERATIONS_ALREADY_CANONICAL",
            )
        unknown = tuple(
            candidate_id
            for candidate_id in (
                *(
                    candidate_id
                    for operation in draft.operations
                    for candidate_id in operation.evidence_candidate_ids
                ),
                *draft.no_op_evidence_candidate_ids,
            )
            if candidate_id not in catalog
        )
        if unknown:
            feedback = tuple(
                f"{item.root}: unknown evidence candidate" for item in unknown[:4]
            )
            raise CuratorProposalSemanticRejected(
                "CURATOR_PROPOSAL_INFORMATION_BOUNDARY",
                (),
                information_boundary=True,
                safe_feedback=feedback,
                json_pointers=tuple(
                    f"/operations/{op_i}/evidence_candidate_ids/{ev_i}"
                    for op_i, operation in enumerate(draft.operations)
                    for ev_i, candidate_id in enumerate(operation.evidence_candidate_ids)
                    if candidate_id not in catalog
                )
                + tuple(
                    f"/no_op_evidence_candidate_ids/{ev_i}"
                    for ev_i, candidate_id in enumerate(
                        draft.no_op_evidence_candidate_ids
                    )
                    if candidate_id not in catalog
                ),
                violation_rule="candidate_id_must_belong_to_chapter",
            )
        if not draft.operations and self._enforce_support_gate and not duplicate_no_op:
            proof_errors = []
            if draft.coverage != 1.0:
                proof_errors.append("coverage must equal 1")
            if draft.unresolved:
                proof_errors.append("unresolved must be empty")
            if draft.declared_vs_observed_diff:
                proof_errors.append("declared_vs_observed_diff must be empty")
            if draft.no_durable_delta_reason is None:
                proof_errors.append("no_durable_delta_reason is required")
            if not draft.no_op_evidence_candidate_ids:
                proof_errors.append("no_op_evidence_candidate_ids are required")
            if proof_errors:
                raise CuratorProposalSemanticRejected(
                    "CURATOR_PROPOSAL_EMPTY_DELTA_UNVERIFIED",
                    (),
                    safe_feedback=(
                        ("incomplete empty-delta proof: " + "; ".join(proof_errors))[
                            :240
                        ],
                    ),
                    json_pointers=(
                        "/operations",
                        "/coverage",
                        "/unresolved",
                        "/declared_vs_observed_diff",
                        "/no_durable_delta_reason",
                        "/no_op_evidence_candidate_ids",
                    ),
                    violation_rule="empty_delta_requires_complete_proof",
                )
            selected_candidates = tuple(
                catalog[candidate_id]
                for candidate_id in draft.no_op_evidence_candidate_ids
            )
            verification: tuple[bool, str] | None = None
            if self._no_op_verifier is not None:
                try:
                    verification = self._no_op_verifier(
                        draft.no_durable_delta_reason or "",
                        selected_candidates,
                        current_world,
                    )
                except Exception:
                    verification = None
            self.last_no_op_verification = verification
            if verification is None or not verification[0]:
                detail = (
                    "trusted no-op verifier is unavailable"
                    if verification is None
                    else f"trusted no-op verifier rejected proof: {verification[1]}"
                )
                raise CuratorProposalSemanticRejected(
                    "CURATOR_PROPOSAL_EMPTY_DELTA_UNVERIFIED",
                    (),
                    safe_feedback=(detail[:240],),
                    json_pointers=(
                        "/operations",
                        "/no_durable_delta_reason",
                        "/no_op_evidence_candidate_ids",
                    ),
                    violation_rule="empty_delta_requires_trusted_verification",
                )
        support_decisions = self._support_gate.evaluate_draft(draft.operations, catalog)
        self.last_support_decisions = support_decisions
        if self._enforce_support_gate:
            # Collect hard rejections (CONTRADICTS/UNRELATED) and PARTIAL items
            # that need a narrow semantic verifier.
            hard_rejected = tuple(
                item
                for item in support_decisions
                if item.disposition
                in {
                    EvidenceSupportDisposition.CONTRADICTS,
                    EvidenceSupportDisposition.UNRELATED,
                }
            )
            # PARTIAL must be resolved by the narrow semantic verifier; without a
            # verifier the gate fails closed and the candidate cannot proceed.
            partial_decisions = tuple(
                item
                for item in support_decisions
                if item.disposition is EvidenceSupportDisposition.PARTIAL
            )
            model_verifications: dict[
                int,
                tuple[EvidenceSupportDisposition, str],
            ] = {}
            if (
                partial_decisions
                and self._semantic_verifier is None
                and self._enable_model_semantic_verifier
            ):
                try:
                    model_verifications = await self._verify_partial_batch(
                        partial_decisions,
                        draft,
                        catalog,
                        request,
                    )
                except Exception:
                    model_verifications = {}
            unresolved_partials: list[EvidenceSupportDecision] = []
            verifier_rejected: list[
                tuple[
                    EvidenceSupportDecision,
                    EvidenceSupportDisposition,
                    str,
                ]
            ] = []
            model_operations_seen: set[int] = set()
            for item in partial_decisions:
                operation = draft.operations[item.operation_index]
                candidate = catalog[item.candidate_id]
                verified: tuple[EvidenceSupportDisposition, str] | None = None
                if self._semantic_verifier is not None:
                    try:
                        verified = self._semantic_verifier(operation.record, candidate)
                    except Exception:
                        verified = None
                elif self._enable_model_semantic_verifier:
                    if item.operation_index in model_operations_seen:
                        continue
                    model_operations_seen.add(item.operation_index)
                    verified = model_verifications.get(item.operation_index)
                if verified is None:
                    unresolved_partials.append(item)
                    continue
                v_disposition, v_reason = verified
                if v_disposition is not EvidenceSupportDisposition.SUPPORTS:
                    verifier_rejected.append((item, v_disposition, v_reason))
            self.last_partial_support_decisions = tuple(partial_decisions)
            rejected_indexes = {
                *(item.operation_index for item in hard_rejected),
                *(item.operation_index for item, _, _ in verifier_rejected),
                *(item.operation_index for item in unresolved_partials),
            }
            if rejected_indexes:
                if len(rejected_indexes) == len(draft.operations):
                    if (
                        unresolved_partials
                        and not verifier_rejected
                        and not hard_rejected
                    ):
                        raise CuratorProposalSemanticRejected(
                            "CURATOR_PROPOSAL_EVIDENCE_UNRESOLVED",
                            (),
                            safe_feedback=tuple(
                                (
                                    f"{item.candidate_id.root}: "
                                    f"{item.reason_code} unresolved "
                                    f"(no verifier) at operation "
                                    f"{item.operation_index}"
                                )[:240]
                                for item in unresolved_partials[:4]
                            ),
                            operation_indexes=tuple(sorted(rejected_indexes)),
                            json_pointers=tuple(
                                f"/operations/{index}/evidence_candidate_ids/0"
                                for index in sorted(rejected_indexes)
                            ),
                            violation_rule="partial_evidence_unresolved_no_verifier",
                        )
                    if verifier_rejected:
                        item, disposition, reason = verifier_rejected[0]
                        raise CuratorProposalSemanticRejected(
                            "CURATOR_PROPOSAL_EVIDENCE_UNSUPPORTED",
                            (),
                            safe_feedback=(
                                (
                                    f"{item.candidate_id.root}: verifier="
                                    f"{disposition.value} ({reason}) "
                                    f"at operation {item.operation_index}"
                                )[:240],
                            ),
                            operation_indexes=tuple(sorted(rejected_indexes)),
                            json_pointers=tuple(
                                f"/operations/{index}/evidence_candidate_ids/0"
                                for index in sorted(rejected_indexes)
                            ),
                            violation_rule="semantic_verifier_rejected_partial",
                        )
                    raise CuratorProposalSemanticRejected(
                        "CURATOR_PROPOSAL_EVIDENCE_UNSUPPORTED",
                        (),
                        safe_feedback=tuple(
                            (
                                f"{item.candidate_id.root}: "
                                f"{item.disposition.value} "
                                f"({item.reason_code}) at operation "
                                f"{item.operation_index}"
                            )[:240]
                            for item in hard_rejected[:4]
                        ),
                        operation_indexes=tuple(sorted(rejected_indexes)),
                        json_pointers=tuple(
                            f"/operations/{index}/evidence_candidate_ids/0"
                            for index in sorted(rejected_indexes)
                        ),
                        violation_rule="candidate_text_contradicts_or_unrelated",
                    )
                rejected_details = {
                    item.operation_index: (
                        item.disposition,
                        item.reason_code,
                    )
                    for item in hard_rejected
                }
                rejected_details.update(
                    {
                        item.operation_index: (disposition, reason)
                        for item, disposition, reason in verifier_rejected
                    }
                )
                rejected_details.update(
                    {
                        item.operation_index: (
                            EvidenceSupportDisposition.PARTIAL,
                            f"{item.reason_code}_UNRESOLVED",
                        )
                        for item in unresolved_partials
                    }
                )
                draft = self._filter_unsupported_operations(
                    draft,
                    rejected_details,
                    base_commit,
                )

        chapter_blocks = {
            block.block_id: block for scene in chapter.scenes for block in scene.blocks
        }
        operations: list[ChangeOperation] = []
        for operation in draft.operations:
            bound_evidence = []
            for candidate_id in operation.evidence_candidate_ids:
                candidate = catalog[candidate_id]
                block = chapter_blocks[candidate.block_id]
                selected = block.text[candidate.start : candidate.end]
                evidence_digest = self._digest(
                    chapter.chapter_id.root.encode(),
                    candidate.block_id.root.encode(),
                    str(candidate.start).encode(),
                    str(candidate.end).encode(),
                )
                canonical_evidence = EvidenceRef(
                    evidence_id=StableId(f"evidence.curator.{evidence_digest}"),
                    root_hash=text_root.root_hash,
                    object_hash=sha256_id(block.text.encode("utf-8")),
                    chapter_id=block.chapter_id,
                    scene_id=block.scene_id,
                    span=TextSpanRef(
                        block_id=candidate.block_id,
                        start=candidate.start,
                        end=candidate.end,
                    ),
                    quote_hash=quote_hash(selected),
                    resolved_at_commit=base_commit,
                    support_status=EvidenceSupportStatus.CURRENT,
                )
                validate_evidence_ref(canonical_evidence, text_root)
                bound_evidence.append(canonical_evidence)
            evidence_refs = tuple(bound_evidence)
            record = operation.record.model_dump(mode="json")
            record[_RECORD_ID_FIELD[operation.record_kind]] = operation.target_id.root
            if operation.record_kind is not WorldRecordKind.ENTITY:
                record["evidence_refs"] = [
                    evidence.model_dump(mode="json") for evidence in evidence_refs
                ]
            digest = self._digest(
                canonical_json_bytes(
                    {
                        "operation": operation.operation.value,
                        "record_kind": operation.record_kind.value,
                        "target_id": operation.target_id.root,
                        "record": record,
                        "evidence": [item.model_dump(mode="json") for item in evidence_refs],
                    }
                )
            )
            operations.append(
                ChangeOperation(
                    operation_id=StableId(f"change.model.{digest}"),
                    root_kind=RootKind.WORLD,
                    operation=operation.operation,
                    target_id=operation.target_id,
                    payload={
                        "record_type": operation.record_kind.value,
                        "record": record,
                    },
                    evidence_refs=evidence_refs,
                )
            )
        source_bytes = canonical_json_bytes(chapter.model_dump(mode="json"))
        return (
            ObservedChangeSet(
                change_set_id=StableId(
                    "changes.model."
                    f"{self._digest(base_commit.root.encode(), chapter.chapter_id.root.encode())}"
                ),
                base_commit=base_commit,
                source_artifact=ArtifactRef(
                    artifact_id=sha256_id(source_bytes),
                    media_type="application/vnd.novel-agent.chapter+json",
                    byte_length=len(source_bytes),
                    schema_version=SchemaVersion("0.1.0"),
                ),
                operations=tuple(operations),
            ),
            call,
            draft,
        )

    @staticmethod
    def _world_model_view(current_world: WorldRootDocument) -> dict[str, object]:
        """Return the accepted semantic state without historical evidence identifiers."""

        view = current_world.model_dump(
            mode="json",
            exclude={"root_hash", "schema_version", "source_commit"},
        )
        for collection in ("events", "states", "relations", "obligations"):
            for record in view[collection]:
                record.pop("evidence_refs", None)
        return view

    def _filter_existing_semantic_duplicates(
        self,
        draft: ChapterChangeDraftV2,
        current_world: WorldRootDocument,
        base_commit: CommitId,
    ) -> ChapterChangeDraftV2:
        """Drop model restatements that are already exact accepted World semantics."""

        current_records = {
            WorldRecordKind.ENTITY: tuple(
                (item.entity_id, item) for item in current_world.entities
            ),
            WorldRecordKind.EVENT: tuple(
                (item.event_id, item) for item in current_world.events
            ),
            WorldRecordKind.STATE: tuple(
                (item.state_id, item) for item in current_world.states
            ),
            WorldRecordKind.RELATION: tuple(
                (item.relation_id, item) for item in current_world.relations
            ),
            WorldRecordKind.OBLIGATION: tuple(
                (item.obligation_id, item) for item in current_world.obligations
            ),
        }
        id_fields = {
            WorldRecordKind.ENTITY: "entity_id",
            WorldRecordKind.EVENT: "event_id",
            WorldRecordKind.STATE: "state_id",
            WorldRecordKind.RELATION: "relation_id",
            WorldRecordKind.OBLIGATION: "obligation_id",
        }
        semantic_index: dict[
            tuple[WorldRecordKind, bytes],
            StableId,
        ] = {}
        current_ids: dict[WorldRecordKind, set[StableId]] = {}
        for kind, records in current_records.items():
            current_ids[kind] = {record_id for record_id, _ in records}
            for record_id, record in records:
                excluded_fields = {id_fields[kind], "evidence_refs"}
                if kind is WorldRecordKind.STATE:
                    excluded_fields.add("valid_time")
                semantic_index[
                    (
                        kind,
                        canonical_json_bytes(
                            record.model_dump(
                                mode="json",
                                exclude=excluded_fields,
                            )
                        ),
                    )
                ] = record_id

        accepted: list[CuratedOperationDraftV2] = []
        receipts: list[ProposalOperationFilterReceipt] = []
        for index, operation in enumerate(draft.operations):
            excluded_fields = (
                {"valid_time"}
                if operation.record_kind is WorldRecordKind.STATE
                else set()
            )
            semantic_payload = canonical_json_bytes(
                operation.record.model_dump(
                    mode="json",
                    exclude=excluded_fields,
                )
            )
            existing_target = semantic_index.get(
                (operation.record_kind, semantic_payload)
            )
            if existing_target is not None:
                source_hash = sha256_id(
                    canonical_json_bytes(operation.model_dump(mode="json"))
                )
                digest = self._digest(
                    base_commit.root.encode(),
                    str(index).encode(),
                    source_hash.root.encode(),
                    existing_target.root.encode(),
                )
                receipts.append(
                    ProposalOperationFilterReceipt(
                        transform_id=StableId(
                            f"proposal-operation-filter.{digest}"
                        ),
                        base_commit=base_commit,
                        operation_index=index,
                        record_kind=operation.record_kind,
                        proposed_target_id=operation.target_id,
                        existing_target_id=existing_target,
                        reason="existing_semantic_duplicate",
                        source_operation_hash=source_hash,
                    )
                )
                continue
            normalized_type = operation.operation
            target_exists = operation.target_id in current_ids[operation.record_kind]
            if normalized_type is ChangeOperationType.CREATE and target_exists:
                normalized_type = ChangeOperationType.REPLACE
            elif normalized_type is ChangeOperationType.REPLACE and not target_exists:
                normalized_type = ChangeOperationType.CREATE
            accepted.append(
                operation.model_copy(update={"operation": normalized_type})
            )
        self.last_operation_filter_receipts = tuple(receipts)
        return draft.model_copy(update={"operations": tuple(accepted)})

    def _filter_unsupported_operations(
        self,
        draft: ChapterChangeDraftV2,
        rejected: dict[int, tuple[EvidenceSupportDisposition, str]],
        base_commit: CommitId,
    ) -> ChapterChangeDraftV2:
        accepted: list[CuratedOperationDraftV2] = []
        receipts = list(self.last_operation_filter_receipts)
        for index, operation in enumerate(draft.operations):
            detail = rejected.get(index)
            if detail is None:
                accepted.append(operation)
                continue
            disposition, reason_code = detail
            source_hash = sha256_id(
                canonical_json_bytes(operation.model_dump(mode="json"))
            )
            digest = self._digest(
                base_commit.root.encode(),
                str(index).encode(),
                source_hash.root.encode(),
                disposition.value.encode(),
            )
            receipts.append(
                ProposalOperationFilterReceipt(
                    transform_id=StableId(
                        f"proposal-operation-filter.{digest}"
                    ),
                    base_commit=base_commit,
                    operation_index=index,
                    record_kind=operation.record_kind,
                    proposed_target_id=operation.target_id,
                    reason="evidence_support_rejected",
                    source_operation_hash=source_hash,
                    support_disposition=disposition,
                    support_reason_code=reason_code[:240],
                )
            )
        self.last_operation_filter_receipts = tuple(receipts)
        return draft.model_copy(update={"operations": tuple(accepted)})

    async def _verify_partial_batch(
        self,
        partial_decisions: tuple[EvidenceSupportDecision, ...],
        draft: ChapterChangeDraftV2,
        catalog: dict[StableId, EvidenceCandidate],
        request: ModelRequest,
    ) -> dict[int, tuple[EvidenceSupportDisposition, str]]:
        """Resolve PARTIAL operations by evaluating each operation's evidence jointly."""

        operation_indexes = tuple(
            dict.fromkeys(item.operation_index for item in partial_decisions)
        )
        items: list[dict[str, object]] = []
        expected: dict[int, tuple[StableId, ...]] = {}
        for operation_index in operation_indexes:
            operation = draft.operations[operation_index]
            candidate_ids = operation.evidence_candidate_ids
            expected[operation_index] = candidate_ids
            items.append(
                {
                    "operation_index": operation_index,
                    "candidate_ids": tuple(item.root for item in candidate_ids),
                    "record": operation.record.model_dump(mode="json"),
                    "evidence": tuple(
                        {
                            "candidate_id": candidate_id.root,
                            "text": catalog[candidate_id].text,
                        }
                        for candidate_id in candidate_ids
                    ),
                }
            )
        suffix = ".semantic-verifier"
        verifier_request = request.model_copy(
            update={
                "request_id": StableId(
                    request.request_id.root[: 128 - len(suffix)] + suffix
                ),
                "timeout_seconds": min(request.timeout_seconds, 90.0),
                "prompt": (
                    "Verify whether each typed World record is directly supported by its "
                    "complete evidence set. Evaluate all excerpts for one operation "
                    "collectively; do not require every individual excerpt to support the "
                    "whole composite record. Interpret English predicate/value labels and "
                    "Chinese evidence semantically; lexical language mismatch is not a failure. "
                    "Require exact units and quantities; traditional Chinese 时辰 equals two "
                    "hours, so 半个时辰 is one hour and never half_hour. Preserve epistemic "
                    "scope: 相信/认为/估计/believes/estimates supports only a record that "
                    "explicitly encodes belief, self-assessment, or estimate, not an objective "
                    "fact. A summary sentence does not support unstated method details. "
                    "Reject transient reading progress, elapsed reading time, and one-scene "
                    "actions as unrelated even when textually true; accepted records must be "
                    "durable World state. Return exactly one decision for every operation, "
                    "copying its complete ordered candidate_ids unchanged. Use supports only for "
                    "direct support, contradicts for explicit conflict, unrelated for no "
                    "material support, and partial only when the excerpt is genuinely "
                    "insufficient. Do not infer future facts.\n"
                    '<EVIDENCE_VERIFICATION_INPUT trusted="false">\n'
                    f"{canonical_json_bytes(items).decode('utf-8')}\n"
                    "</EVIDENCE_VERIFICATION_INPUT>"
                ),
            }
        )
        verification_type = self._semantic_verification_batch_type(len(expected))
        result, _call = await self._gateway.generate_structured(
            verifier_request,
            verification_type,
        )
        resolved: dict[int, tuple[EvidenceSupportDisposition, str]] = {}
        for item in result.decisions:
            if item.operation_index in resolved:
                return {}
            if expected.get(item.operation_index) != item.candidate_ids:
                return {}
            resolved[item.operation_index] = (item.disposition, item.reason_code)
        return resolved

    @staticmethod
    def _semantic_verification_batch_type(
        decision_count: int,
    ) -> type[EvidenceSemanticVerificationDraft]:
        if not 1 <= decision_count <= 4:
            raise ValueError("semantic verification batch must contain one to four decisions")
        return cast(
            type[EvidenceSemanticVerificationDraft],
            create_model(
                f"EvidenceSemanticVerificationBatch{decision_count}",
                __base__=EvidenceSemanticVerificationDraft,
                decisions=(
                    tuple[EvidenceSemanticVerificationItem, ...],
                    Field(min_length=decision_count, max_length=decision_count),
                ),
            ),
        )

    async def evidence_repair_v2(
        self,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        parent_changes: ObservedChangeSet,
        request: ModelRequest,
        *,
        contract_prompt: str | None = None,
        repair_operation_indexes: tuple[int, ...] = (),
    ) -> tuple[ObservedChangeSet, ModelCallRecord, tuple[EvidenceRepairDraft, ...]]:
        """Evidence-only repair: model picks replacement candidate IDs, never rewrites record."""

        chapter = Stage1Curator._chapter(text_root, chapter_index)
        candidates = self._evidence_generator.generate(text_root, chapter_index)
        self.last_evidence_candidates = candidates
        catalog = self._evidence_generator.index_by_id(candidates)
        views = self._evidence_generator.model_views(candidates)
        chapter_blocks = {
            block.block_id: block for scene in chapter.scenes for block in scene.blocks
        }
        target_indexes = set(repair_operation_indexes) or set(
            range(len(parent_changes.operations))
        )
        parent_ops = [
            {
                "operation_index": index,
                "operation_type": operation.operation.value,
                "target_id": operation.target_id.root,
                "payload": operation.payload,
                "current_evidence_refs": [
                    {
                        "block_id": evidence.span.block_id.root if evidence.span else None,
                        "start": evidence.span.start if evidence.span else None,
                        "end": evidence.span.end if evidence.span else None,
                    }
                    for evidence in operation.evidence_refs
                ],
            }
            for index, operation in enumerate(parent_changes.operations)
            if index in target_indexes
        ]
        contract = f"{contract_prompt}\n\n" if contract_prompt else ""
        safe_request = request.model_copy(
            update={
                "prompt": (
                    contract
                    + "Output a JSON array of EvidenceRepairDraft objects. "
                    "Each draft must specify operation_index, replacement_candidate_ids, "
                    "and action. Only choose from the supplied evidence_candidate_ids. "
                    "Do NOT rewrite target_id, record_kind, record payload, or operation type. "
                    "Use action=replace_evidence to swap candidates, "
                    "action=drop_operation to remove the operation, "
                    "or action=mark_unresolved if no candidate supports the record.\n"
                    '<EVIDENCE_REPAIR_INPUT trusted="false">\n'
                    f"BASE_COMMIT={base_commit.root}\n"
                    f"REPAIR_OPERATIONS={canonical_json_bytes(parent_ops).decode()}\n"
                    "EVIDENCE_CANDIDATES="
                    f"{canonical_json_bytes([v.model_dump(mode='json') for v in views]).decode()}\n"
                    "</EVIDENCE_REPAIR_INPUT>"
                )
            }
        )
        self.last_prompt_fingerprint = sha256_id(safe_request.prompt.encode("utf-8"))
        raw_drafts, call = await self._gateway.generate_structured(
            safe_request,
            list[EvidenceRepairDraft],  # type: ignore[type-var]
        )
        drafts = tuple(raw_drafts)
        # Apply evidence-only repairs to parent operations.
        repair_by_index: dict[int, EvidenceRepairDraft] = {
            draft.operation_index: draft for draft in drafts
        }
        new_operations: list[ChangeOperation] = []
        for index, operation in enumerate(parent_changes.operations):
            repair = repair_by_index.get(index)
            if repair is None:
                new_operations.append(operation)
                continue
            if repair.action is EvidenceRepairAction.DROP_OPERATION:
                continue
            if repair.action is EvidenceRepairAction.MARK_UNRESOLVED:
                new_operations.append(operation)
                continue
            # replace_evidence: bind new candidate IDs
            unknown = tuple(
                candidate_id
                for candidate_id in repair.replacement_candidate_ids
                if candidate_id not in catalog
            )
            if unknown:
                raise CuratorProposalSemanticRejected(
                    "CURATOR_PROPOSAL_INFORMATION_BOUNDARY",
                    (),
                    information_boundary=True,
                    safe_feedback=tuple(
                        f"{item.root}: unknown evidence candidate" for item in unknown[:4]
                    ),
                    json_pointers=tuple(
                        f"/operations/{index}/evidence_candidate_ids/{ev_i}"
                        for ev_i, item in enumerate(unknown)
                    ),
                    violation_rule="candidate_id_must_belong_to_chapter",
                )
            bound_evidence = []
            for candidate_id in repair.replacement_candidate_ids:
                candidate = catalog[candidate_id]
                block = chapter_blocks[candidate.block_id]
                selected = block.text[candidate.start : candidate.end]
                evidence_digest = self._digest(
                    chapter.chapter_id.root.encode(),
                    candidate.block_id.root.encode(),
                    str(candidate.start).encode(),
                    str(candidate.end).encode(),
                )
                canonical_evidence = EvidenceRef(
                    evidence_id=StableId(f"evidence.curator.{evidence_digest}"),
                    root_hash=text_root.root_hash,
                    object_hash=sha256_id(block.text.encode("utf-8")),
                    chapter_id=block.chapter_id,
                    scene_id=block.scene_id,
                    span=TextSpanRef(
                        block_id=candidate.block_id,
                        start=candidate.start,
                        end=candidate.end,
                    ),
                    quote_hash=quote_hash(selected),
                    resolved_at_commit=base_commit,
                    support_status=EvidenceSupportStatus.CURRENT,
                )
                validate_evidence_ref(canonical_evidence, text_root)
                bound_evidence.append(canonical_evidence)
            # Preserve operation_id, operation type, target_id, payload; only swap evidence.
            new_operations.append(
                operation.model_copy(update={"evidence_refs": tuple(bound_evidence)})
            )
        source_bytes = canonical_json_bytes(chapter.model_dump(mode="json"))
        return (
            ObservedChangeSet(
                change_set_id=StableId(
                    "changes.model.repair."
                    f"{self._digest(base_commit.root.encode(), chapter.chapter_id.root.encode())}"
                ),
                base_commit=base_commit,
                source_artifact=ArtifactRef(
                    artifact_id=sha256_id(source_bytes),
                    media_type="application/vnd.novel-agent.chapter+json",
                    byte_length=len(source_bytes),
                    schema_version=SchemaVersion("0.1.0"),
                ),
                operations=tuple(new_operations),
            ),
            call,
            drafts,
        )

    def _normalize_operations(
        self,
        draft: ChapterChangeDraft,
        current_world: WorldRootDocument,
    ) -> ChapterChangeDraft:
        """Normalize existence semantics and filter dangling entity references."""
        current_ids = {
            WorldRecordKind.ENTITY: {item.entity_id for item in current_world.entities},
            WorldRecordKind.EVENT: {item.event_id for item in current_world.events},
            WorldRecordKind.STATE: {item.state_id for item in current_world.states},
            WorldRecordKind.RELATION: {item.relation_id for item in current_world.relations},
            WorldRecordKind.OBLIGATION: {item.obligation_id for item in current_world.obligations},
        }
        current_states = {item.state_id: item for item in current_world.states}
        created_entities = {
            operation.target_id
            for operation in draft.operations
            if operation.record_kind is WorldRecordKind.ENTITY
            and operation.operation is ChangeOperationType.CREATE
        }
        known_entities = current_ids[WorldRecordKind.ENTITY] | created_entities
        accepted: list[CuratedOperationDraft] = []
        dropped: list[str] = []
        unchanged: list[str] = []
        for operation in draft.operations:
            # Chapter replay may add or revise observed memory, but it cannot
            # autonomously delete canonical memory. Destructive retirement is a
            # separate patch workflow requiring explicit human approval.
            if operation.operation is ChangeOperationType.RETIRE:
                dropped.append(operation.target_id.root)
                continue
            record = operation.record
            referenced_entities: set[StableId] = set()
            if isinstance(record, CuratorEventRecord):
                referenced_entities.update(record.participant_ids)
            elif isinstance(record, CuratorStateRecord):
                referenced_entities.add(record.subject_id)
            elif isinstance(record, CuratorRelationRecord):
                referenced_entities.update((record.subject_id, record.object_id))
            elif isinstance(record, CuratorObligationRecord):
                referenced_entities.update(record.owner_ids)
            missing = referenced_entities - known_entities
            if missing:
                dropped.append(operation.target_id.root)
                continue
            exists = operation.target_id in current_ids[operation.record_kind]
            normalized_type = operation.operation
            if operation.operation is ChangeOperationType.CREATE and exists:
                normalized_type = ChangeOperationType.REPLACE
            elif operation.operation is ChangeOperationType.REPLACE and not exists:
                normalized_type = ChangeOperationType.CREATE
            if (
                operation.record_kind is WorldRecordKind.STATE
                and normalized_type is ChangeOperationType.REPLACE
                and isinstance(record, CuratorStateRecord)
                and (current := current_states.get(operation.target_id)) is not None
                and (
                    record.subject_id,
                    record.predicate,
                    record.value,
                    record.truth_class,
                )
                == (
                    current.subject_id,
                    current.predicate,
                    current.value,
                    current.truth_class,
                )
            ):
                unchanged.append(operation.target_id.root)
                continue
            normalized_target = self._target_resolver(
                operation.record_kind,
                operation.target_id,
                current_world,
            )
            accepted.append(
                operation.model_copy(
                    update={
                        "operation": normalized_type,
                        "target_id": normalized_target,
                    }
                )
            )
        unresolved = list(draft.unresolved)
        if dropped:
            detail = "runtime filtered dangling or missing targets: " + ", ".join(dropped)
            unresolved.append(detail[:160])
        if unchanged:
            detail = "runtime filtered unchanged state targets: " + ", ".join(unchanged)
            unresolved.append(detail[:160])
        original_count = len(draft.operations)
        bounded_coverage = (
            draft.coverage
            if original_count == 0
            else min(draft.coverage, len(accepted) / original_count)
        )
        return draft.model_copy(
            update={
                "operations": tuple(accepted),
                "unresolved": tuple(unresolved[:4]),
                "coverage": bounded_coverage,
            }
        )

    @classmethod
    def _merge_normalized_collisions(
        cls,
        draft: ChapterChangeDraft,
        base_commit: CommitId,
    ) -> tuple[ChapterChangeDraft, tuple[ProposalEvidenceMergeReceipt, ...]]:
        groups: dict[
            tuple[WorldRecordKind, StableId],
            list[tuple[int, CuratedOperationDraft]],
        ] = {}
        for index, operation in enumerate(draft.operations):
            groups.setdefault((operation.record_kind, operation.target_id), []).append(
                (index, operation)
            )

        merged: list[CuratedOperationDraft] = []
        receipts: list[ProposalEvidenceMergeReceipt] = []
        for (record_kind, target_id), indexed in groups.items():
            if len(indexed) == 1:
                merged.append(indexed[0][1])
                continue
            semantic_payloads = tuple(
                canonical_json_bytes(operation.model_dump(mode="json", exclude={"evidence_refs"}))
                for _, operation in indexed
            )
            semantic_hashes = tuple(sha256_id(payload) for payload in semantic_payloads)
            evidence_payloads = tuple(
                canonical_json_bytes(evidence.model_dump(mode="json"))
                for _, operation in indexed
                for evidence in operation.evidence_refs
            )
            evidence_hashes = tuple(sha256_id(payload) for payload in evidence_payloads)
            if len(set(semantic_hashes)) != 1:
                raise CuratorProposalSemanticRejected(
                    "CURATOR_PROPOSAL_NORMALIZED_TARGET_COLLISION",
                    (
                        ProposalConflict(
                            record_kind=record_kind,
                            target_id=target_id,
                            operation_indexes=tuple(index for index, _ in indexed),
                            semantic_hashes=tuple(
                                sorted(set(semantic_hashes), key=lambda item: item.root)
                            ),
                            evidence_hashes=tuple(
                                sorted(set(evidence_hashes), key=lambda item: item.root)
                            ),
                        ),
                    ),
                )
            unique_evidence = {
                canonical_json_bytes(evidence.model_dump(mode="json")): evidence
                for _, operation in indexed
                for evidence in operation.evidence_refs
            }
            ordered_evidence = tuple(
                unique_evidence[payload] for payload in sorted(unique_evidence)
            )
            merged.append(indexed[0][1].model_copy(update={"evidence_refs": ordered_evidence}))
            source_hashes = tuple(
                sha256_id(canonical_json_bytes(operation.model_dump(mode="json")))
                for _, operation in indexed
            )
            digest = cls._digest(
                base_commit.root.encode(),
                record_kind.value.encode(),
                target_id.root.encode(),
                semantic_hashes[0].root.encode(),
            )
            receipts.append(
                ProposalEvidenceMergeReceipt(
                    transform_id=StableId(f"proposal-evidence-merge.{digest}"),
                    base_commit=base_commit,
                    record_kind=record_kind,
                    target_id=target_id,
                    semantic_hash=semantic_hashes[0],
                    source_operation_hashes=source_hashes,
                    merged_evidence_hashes=tuple(
                        sorted(set(evidence_hashes), key=lambda item: item.root)
                    ),
                )
            )
        return (
            draft.model_copy(update={"operations": tuple(merged)}),
            tuple(receipts),
        )

    @staticmethod
    def _digest(*parts: bytes) -> str:
        return hashlib.sha256(b"\0".join(parts)).hexdigest()[:24]
