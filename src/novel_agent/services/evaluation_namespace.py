"""Logically close evaluation artifacts without touching Memory or Canon."""

from __future__ import annotations

from novel_agent.domain.artifacts import (
    EVALUATION_NAMESPACE_DISCARD_MEDIA_TYPE,
    ArtifactRef,
    is_evaluation_artifact_media_type,
)
from novel_agent.domain.ids import RunId, SchemaVersion, StableId
from novel_agent.domain.v05_readout import (
    EvaluationNamespaceDiscardReceipt,
    MemoryIdentitySnapshot,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes

DISCARD_SCHEMA_VERSION = SchemaVersion("1.0.0")


class EvaluationNamespaceError(ValueError):
    """Evaluation namespace discard contract was violated."""


def discard_evaluation_namespace(
    artifacts: ArtifactRepository,
    *,
    run_id: RunId,
    discarded_refs: tuple[ArtifactRef, ...],
    memory_before: MemoryIdentitySnapshot,
    memory_after: MemoryIdentitySnapshot,
    discard_identity: StableId | None = None,
) -> EvaluationNamespaceDiscardReceipt:
    """Close the evaluation side channel. Memory identity must be unchanged."""

    if not discarded_refs:
        raise EvaluationNamespaceError("discard requires evaluation artifacts")
    if any(not is_evaluation_artifact_media_type(ref.media_type) for ref in discarded_refs):
        raise EvaluationNamespaceError("discard can only close evaluation-namespace artifacts")
    if memory_before != memory_after:
        raise EvaluationNamespaceError("discard must not change Memory identity")
    receipt = EvaluationNamespaceDiscardReceipt(
        receipt_id=discard_identity or StableId(f"evaluation-discard.{run_id.root}"[:128]),
        run_id=run_id,
        discarded_refs=discarded_refs,
        memory_identity_before=memory_before,
        memory_identity_after=memory_after,
    )
    artifacts.put(
        canonical_json_bytes(receipt.model_dump(mode="json")),
        EVALUATION_NAMESPACE_DISCARD_MEDIA_TYPE,
        DISCARD_SCHEMA_VERSION,
    )
    return receipt
