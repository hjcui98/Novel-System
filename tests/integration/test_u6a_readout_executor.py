"""Filesystem-backed U6-A lifecycle integration."""

from __future__ import annotations

import asyncio

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.domain.v05_readout import EvaluationNamespaceDiscardReceipt
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.u6a_readout_executor import U6AReadoutExecutor
from tests.unit.test_u6a_readout_executor import _Adapter, _fixture


def test_u6a_persists_each_checkpoint_discard_in_the_run_namespace(tmp_path) -> None:
    plan, plan_ref, manifest, basis_report = _fixture()
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    execution = asyncio.run(
        U6AReadoutExecutor(
            plan=plan,
            plan_ref=plan_ref,
            basis_manifest=manifest,
            basis_report=basis_report,
            adapter=_Adapter(),
            artifacts=repository,
            run_id=basis_report.run_id,
        ).run()
    )

    assert execution.report.status == "COMPLETED"
    discard_receipts = {
        EvaluationNamespaceDiscardReceipt.model_validate_json(
            repository.read_verified(item.discard_receipt_ref), strict=True
        ).receipt_id
        for item in execution.report.items
        if item.discard_receipt_ref is not None
    }
    assert len(discard_receipts) == 2
    for item in execution.report.items:
        assert item.discard_receipt_ref is not None
        assert repository.read_verified(item.discard_receipt_ref)
        assert item.memory_identity_before == item.memory_identity_after
