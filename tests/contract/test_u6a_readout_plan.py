"""U6-A readout plan compilation against frozen local identities."""

from __future__ import annotations

import json
from pathlib import Path

from novel_agent.domain import u6_continuous_replay
from novel_agent.services.u6a_readout_plan import U6AReadoutPlanCompiler

ROOT = Path(__file__).resolve().parents[2]
BASIS = Path("/tmp/novel-agent-u6a-basis-20260825-b/checkpoint_basis_manifest.json")
SOURCE = Path("/tmp/novel-agent-u4s-s0-seed-20260823-n/writer_readout_campaign_manifest.json")


def test_compiler_builds_81_public_tasks_and_45_canary_jobs(tmp_path: Path) -> None:
    assert (ROOT / "benchmarks/private/ztj_novelmem_v0.5").is_dir()
    result = U6AReadoutPlanCompiler(
        basis_manifest_path=BASIS,
        source_readout_manifest_path=SOURCE,
        output_root=tmp_path / "u6a-readout-plan",
        experiment_id="contract",
    ).run()

    assert result.plan.status == "READY"
    assert result.plan.qa_task_count == 51
    assert result.plan.context_task_count == 30
    assert result.plan.canary_job_count == 45
    assert len(result.plan.tasks) == 81
    assert len(result.plan.canary_jobs) == 45
    assert all(task.future_visibility == "evaluator_only" for task in result.plan.tasks)
    assert all(job.future_visibility == "evaluator_only" for job in result.plan.canary_jobs)


def test_u6_schemas_are_exported_from_the_domain_models() -> None:
    schema_root = ROOT / "schemas" / "stage2"
    models = (
        u6_continuous_replay.U6CheckpointBasis,
        u6_continuous_replay.U6CheckpointBasisManifest,
        u6_continuous_replay.U6CheckpointLineage,
        u6_continuous_replay.U6ContinuousReplayReport,
        u6_continuous_replay.U6AReadoutTask,
        u6_continuous_replay.U6ACanaryJob,
        u6_continuous_replay.U6AReadoutPlan,
    )
    for model in models:
        path = schema_root / f"{model.__name__}.schema.json"
        assert json.loads(path.read_text(encoding="utf-8")) == model.model_json_schema()
