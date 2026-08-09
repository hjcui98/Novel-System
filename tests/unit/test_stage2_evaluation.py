from __future__ import annotations

from datetime import UTC, datetime

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, SchemaVersion
from novel_agent.domain.runtime import EvaluationDecision
from novel_agent.domain.stage2 import ArmExecutionStatus
from novel_agent.services.stage2_evaluation import Stage2PairedEvaluationBuilder
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

NOW = datetime(2026, 7, 21, tzinfo=UTC)


def artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "e" * 64),
        media_type="application/vnd.novel-agent.stage2-paired-pilot+json",
        byte_length=10,
        schema_version=SchemaVersion("2.0.0"),
    )


def test_stage2_paired_evaluation_builds_independent_ledger_entries() -> None:
    report = Stage2PairedPilotRunner().run(make_synthetic_bundle())
    config, entries = Stage2PairedEvaluationBuilder().build(
        report,
        artifact(),
        created_at=NOW,
    )

    assert config.dataset_hash == report.bundle_hash
    assert config.model_required is False
    assert config.parameters[0].value == report.configuration_fingerprint.root
    assert len(entries) == 3
    assert len({entry.evaluation_id for entry in entries}) == 3
    entry = entries[0]
    assert entry.run_id == config.run_id
    assert entry.candidate_id == report.cases[0].pair_id
    assert entry.decision is EvaluationDecision.INFORMATIONAL
    assert entry.failure_codes == ("HELD_OUT_COMPLEX_GAIN_NOT_PROVEN",)
    assert entry.evidence_artifacts == (artifact(),)
    assert {metric.name: metric.value for metric in entry.metrics} == {
        "deterministic_gold_evidence_recall": 1.0,
        "agentic_gold_evidence_recall": 1.0,
        "deterministic_plan_obligation_coverage": 1.0,
        "agentic_plan_obligation_coverage": 1.0,
        "deterministic_retrieval_calls": 4.0,
        "agentic_retrieval_calls": 1.0,
        "future_leakage_count": 0.0,
        "safety_regression": 0.0,
        "comparable": 1.0,
    }


def test_stage2_paired_evaluation_preserves_pair_blockers_and_proven_gain_state() -> None:
    original = Stage2PairedPilotRunner().run(make_synthetic_bundle())
    blocked = original.cases[0].model_copy(
        update={"comparable": False, "blockers": ("pair basis mismatch",)}
    )
    report = original.model_copy(
        update={
            "cases": (blocked,),
            "comparable_results_count": 0,
            "held_out_complex_gain_proven": True,
        }
    )

    _, entries = Stage2PairedEvaluationBuilder().build(report, artifact(), created_at=NOW)

    assert entries[0].failure_codes == ("pair basis mismatch",)
    assert entries[0].metrics[-1].value == 0.0


def test_stage2_evaluation_omits_agentic_metrics_for_single_arm_report() -> None:
    report = Stage2PairedPilotRunner().run(make_synthetic_bundle())
    case = report.cases[0].model_copy(
        update={
            "agentic_execution_status": ArmExecutionStatus.SKIPPED,
            "agentic_metrics": None,
            "delta_metrics": None,
            "accuracy_gain": None,
            "tool_call_reduction": None,
            "safety_regression": None,
            "comparable": False,
            "paired_comparison_status": "NOT_RUN",
        }
    )
    report = report.model_copy(update={"cases": (case,)})
    _, entries = Stage2PairedEvaluationBuilder().build(report, artifact(), created_at=NOW)
    names = {metric.name for metric in entries[0].metrics}
    assert not any(name.startswith("agentic_") for name in names)
