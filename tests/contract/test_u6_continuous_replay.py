"""U6-A benchmark bundle contract checks."""

from __future__ import annotations

from pathlib import Path

from novel_agent.domain.u6_continuous_replay import U6BasisKind, U6BasisStatus
from novel_agent.services.u6_continuous_replay import U6ContinuousReplayService

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "benchmarks/private/ztj_novelmem_v0.5"


def test_frozen_protocol_template_has_unique_public_and_internal_basis_sets() -> None:
    service = U6ContinuousReplayService(
        bundle_root=BUNDLE,
        output_root=Path("/tmp/u6-contract-unused"),
        experiment_id="contract",
    )
    template = service._load_basis_template()
    public = service._load_public_chapters()
    internal = tuple(
        node.checkpoint_chapter
        for node in template.basis_nodes
        if node.kind is U6BasisKind.INTERNAL_N_MINUS_1
    )
    template.validate_shape(public_chapters=public, internal_chapters=internal)
    assert template.status is U6BasisStatus.PENDING_REPLAY
    assert len(template.basis_nodes) == 34
    assert len(public) == 16
    assert len(internal) == 18
    assert sum(len(node.jobs) for node in template.basis_nodes) == 45
    assert tuple(node.checkpoint_chapter for node in template.basis_nodes) == tuple(
        sorted(node.checkpoint_chapter for node in template.basis_nodes)
    )
    service._validate_d_short_attachments(template)


def test_public_protocol_keeps_c100_context_only_and_c300_qa_only() -> None:
    service = U6ContinuousReplayService(
        bundle_root=BUNDLE,
        output_root=Path("/tmp/u6-contract-unused-boundary"),
        experiment_id="contract-boundary",
    )
    assert 100 in service._load_public_chapters()
    assert 300 in service._load_public_chapters()
