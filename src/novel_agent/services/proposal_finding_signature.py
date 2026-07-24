"""Dual-signature helpers for proposal poison-loop detection (WP5)."""

from __future__ import annotations

from collections.abc import Sequence

from novel_agent.domain.ids import ArtifactId
from novel_agent.domain.memory_write import ProposalRejectionStage
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes


def proposal_finding_signature(
    *,
    reason_code: str,
    rejection_stage: ProposalRejectionStage | str,
    json_pointers: Sequence[str] = (),
    violation_rule: str | None = None,
    block_or_candidate_ids: Sequence[str] = (),
) -> ArtifactId:
    """Hash stable defect identity without output content or attempt metadata."""

    stage = (
        rejection_stage.value
        if isinstance(rejection_stage, ProposalRejectionStage)
        else str(rejection_stage)
    )
    return sha256_id(
        canonical_json_bytes(
            {
                "reason_code": reason_code,
                "rejection_stage": stage,
                "json_pointer": tuple(json_pointers),
                "violation_rule": violation_rule,
                "block_or_candidate_id": tuple(block_or_candidate_ids),
            }
        )
    )


def extract_block_or_candidate_ids(safe_feedback: Sequence[str]) -> tuple[str, ...]:
    """Best-effort parse of block/candidate ids from bounded feedback lines."""

    ids: list[str] = []
    for line in safe_feedback:
        token = line.split(":", 1)[0].strip()
        if token.startswith(("block.", "evidence-candidate.", "evidence.")):
            ids.append(token)
    return tuple(dict.fromkeys(ids))
