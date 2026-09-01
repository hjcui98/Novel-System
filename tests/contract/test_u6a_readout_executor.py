"""U6-A execution report and discard contract tests."""

from __future__ import annotations

import asyncio

import pytest

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.domain.u6_continuous_replay import U6AReadoutPhaseResult, U6ContinuousReplayReport
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.u6a_readout_executor import U6AReadoutExecutor, finalize_u6a_basis_report
from tests.unit.test_u6a_readout_executor import _Adapter, _fixture


def test_u6a_report_and_discard_are_strictly_reparseable(tmp_path) -> None:
    plan, plan_ref, manifest, basis_report = _fixture()
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    execution = asyncio.run(
        U6AReadoutExecutor(
            plan=plan,
            plan_ref=plan_ref,
            basis_manifest=manifest,
            basis_report=basis_report,
            adapter=_Adapter(),
            artifacts=artifacts,
            run_id=basis_report.run_id,
        ).run()
    )

    reparsed = type(execution.report).model_validate_json(
        execution.report.model_dump_json(by_alias=True), strict=True
    )
    stored = artifacts.read_verified(execution.report_ref)
    assert type(execution.report).model_validate_json(stored, strict=True) == reparsed
    finalized = finalize_u6a_basis_report(basis_report, execution)
    assert finalized.status == "COMPLETED"
    assert finalized.readout_report_ref == execution.report_ref
    assert all(item.evaluation_namespace == "DISCARDED" for item in finalized.lineage)
    assert (
        U6ContinuousReplayReport.model_validate_json(
            finalized.model_dump_json(by_alias=True), strict=True
        )
        == finalized
    )


def test_u6a_phase_result_rejects_an_unbound_evaluation_ref() -> None:
    from novel_agent.domain.artifacts import ArtifactRef
    from novel_agent.domain.ids import ArtifactId, SchemaVersion

    ref = ArtifactRef(
        artifact_id=ArtifactId(f"sha256:{'1' * 64}"),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )
    with pytest.raises(ValueError, match="subset"):
        U6AReadoutPhaseResult(
            phase="writer",
            artifact_refs=(),
            evaluation_refs=(ref,),
        )
