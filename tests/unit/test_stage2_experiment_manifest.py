from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest
import scripts.run_stage2_teacher_forced_e2e as stage2_runner
from scripts.run_stage2_teacher_forced_e2e import (
    _ensure_experiment_manifest,
    _load_quality_repair_flags,
    _loopback_postgres_url,
    _prepare_project_artifact_directory,
)

from novel_agent.domain.retrieval_routing import RetrievalBackendProfile
from novel_agent.domain.stage2 import (
    BenchmarkInformationProfile,
    ControllerMode,
    CuratorEvidenceContract,
    EvidenceSupportGateMode,
    QualityRepairFeatureFlags,
)
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatcher
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_evaluation import MemoryBenchmarkEvaluator
from novel_agent.services.memory_benchmark_metric_contracts import (
    GATE_METRIC_FORMULA_HASH,
    GATE_METRIC_FORMULA_VERSION,
)
from novel_agent.services.retrieval_unit_normalizer import RetrievalUnitNormalizer
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner
from novel_agent.services.task_conditioned_need_generation import (
    TaskPlanConditionedNeedGenerator,
)
from novel_agent.services.task_focus import TaskFocusExtractor
from novel_agent.services.writer_context_assembler import WriterContextAssembler

ROOT = Path(__file__).parents[2]
PILOT = ROOT / "benchmarks/private/ztj_memory_pilot_v0.1"


def _args(experiment_id: str = "stage2r-run3") -> Namespace:
    return Namespace(
        experiment_id=experiment_id,
        source=PILOT,
        database_url=("postgresql+psycopg://user:secret@127.0.0.1:5432/novel_agent_stage2r_run3"),
        retrieval_backend="real_hybrid",
        opensearch_url="http://127.0.0.1:9200",
        embedding_url="http://127.0.0.1:8081/v1/embeddings",
        reranker_url="http://127.0.0.1:8082/rerank",
        model_base_url="http://127.0.0.1:8002/v1",
        model="qwen36-27b-nvfp4",
        memory_write_dry_run=False,
        arms="A",
        token_budget=4000,
        max_candidates=20,
        max_tool_calls=48,
        support_max_concurrent_needs=1,
        support_kv_token_budget=200_000,
        endpoint_request_limit=1,
        checkpoint_workers=1,
        evaluator_max_concurrent_batches=1,
        model_scheduling_timeout_seconds=120.0,
        frozen_planner_artifact=None,
        resume_commit=None,
        resume_chapter=None,
        quality_repair_config=None,
        allow_dirty_diagnostic=False,
    )


def test_experiment_manifest_is_credential_safe_and_resume_stable(tmp_path: Path) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    flags = QualityRepairFeatureFlags()

    _ensure_experiment_manifest(_args(), bundle, tmp_path, flags)
    _ensure_experiment_manifest(_args(), bundle, tmp_path, flags)

    payload = json.loads((tmp_path / "experiment_manifest.json").read_text("utf-8"))
    assert payload["experiment_id"] == "stage2r-run3"
    assert payload["database"] == ("postgresql+psycopg://127.0.0.1:5432/novel_agent_stage2r_run3")
    assert payload["schema_version"] == 4
    assert payload["task_focus_version"] == TaskFocusExtractor.version
    assert payload["need_generation_profile"] == TaskPlanConditionedNeedGenerator.version
    assert payload["retrieval_unit_normalizer_version"] == RetrievalUnitNormalizer.version
    assert payload["writer_context_assembler_version"] == WriterContextAssembler.version
    assert payload["gold_evidence_matcher_version"] == GoldEvidenceMatcher.version
    assert payload["evaluator_version"] == MemoryBenchmarkEvaluator.version
    assert payload["gate_metric_formula_version"] == GATE_METRIC_FORMULA_VERSION
    assert payload["gate_metric_formula_hash"] == GATE_METRIC_FORMULA_HASH.root
    assert payload["code_version"] == Stage2PairedPilotRunner.version
    expected_run_config_hash = Stage2PairedPilotRunner(
        arms=("A",),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
    ).public_configuration_fingerprint(bundle.bundle_schema_version.root)
    assert payload["run_config_hash"] == expected_run_config_hash.root
    execution_config = payload["execution_config"]
    assert execution_config["support_max_concurrent_needs"] == 1
    assert execution_config["support_multi_enable_thinking"] is False
    assert execution_config["support_multi_thinking_token_budget"] == 0
    assert execution_config["support_multi_max_output_tokens"] == 2048
    assert execution_config["evaluator_max_concurrent_batches"] == 1
    assert execution_config["checkpoint_workers"] == 1
    assert execution_config["endpoint_request_limit"] == 1
    assert execution_config["configured_kv_token_budget"] == 200_000
    assert execution_config["effective_kv_token_budget"] == 160_000
    assert execution_config["kv_safety_reserve_ratio"] == 0.2
    assert execution_config["model_sequence_limit"] == 131_072
    assert execution_config["scheduling_timeout_seconds"] == 120.0
    assert execution_config["frozen_planner_artifact_hash"] is None
    assert payload["execution_config_hash"].startswith("sha256:")
    assert payload["execution_config_hash"] == (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                execution_config,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    assert payload["benchmark_contract_hash"] == bundle.content_hash.root
    assert payload["matcher_version"] == GoldEvidenceMatcher.version
    assert payload["writer_token_budget"] == 4000
    assert payload["evidence_ledger_token_budget"] == 12_000
    assert payload["code_source_fingerprint"].startswith("sha256:")
    assert isinstance(payload["code_source_dirty"], bool)
    assert "secret" not in json.dumps(payload)

    with pytest.raises(ValueError, match="manifest differs"):
        _ensure_experiment_manifest(_args("stage2r-run4"), bundle, tmp_path, flags)


def test_parser_exposes_fixed_claim_support_transport_policy() -> None:
    parsed = stage2_runner.parser().parse_args(
        [
            "--source",
            str(PILOT),
            "--output-directory",
            "/tmp/stage2m-parser-test",
            "--experiment-id",
            "stage2m-parser-test",
            "--support-multi-thinking",
            "--support-multi-thinking-token-budget",
            "256",
            "--support-multi-max-output-tokens",
            "1536",
        ]
    )
    assert parsed.support_multi_thinking is True
    assert parsed.support_multi_thinking_token_budget == 256
    assert parsed.support_multi_max_output_tokens == 1536


def test_default_information_profile_resolves_from_planning_contexts() -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    assert stage2_runner._default_information_profile(bundle) == (
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED.value
    )
    blind = bundle.model_copy(
        update={
            "case_manifests": tuple(
                case.model_copy(update={"information_profile": None})
                for case in bundle.case_manifests
            )
        }
    )
    assert stage2_runner._default_information_profile(blind) == (
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED.value
    )


def test_formal_manifest_rejects_dirty_executable_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    monkeypatch.setattr(stage2_runner, "_source_status", lambda _root: " M src/changed.py\n")

    with pytest.raises(ValueError, match="clean executable source tree"):
        _ensure_experiment_manifest(
            _args("stage2m-dirty-source"),
            bundle,
            tmp_path,
            QualityRepairFeatureFlags(),
            require_clean_source=True,
        )


def test_evaluation_manifest_is_separate_from_immutable_project(tmp_path: Path) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    project_directory = tmp_path / "canonical-project"
    output_directory = tmp_path / "evaluation-run"
    project_directory.mkdir()
    project_manifest = project_directory / "experiment_manifest.json"
    project_manifest.write_text('{"immutable": true}\n', encoding="utf-8")

    _ensure_experiment_manifest(
        _args("stage2m-evaluation"),
        bundle,
        output_directory,
        QualityRepairFeatureFlags(),
        project_directory=project_directory,
    )

    assert json.loads(project_manifest.read_text("utf-8")) == {"immutable": True}
    evaluation = json.loads((output_directory / "experiment_manifest.json").read_text("utf-8"))
    assert evaluation["experiment_id"] == "stage2m-evaluation"
    assert evaluation["project_directory"] == str(project_directory.resolve())


def test_execution_config_drift_is_fail_closed(tmp_path: Path) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    flags = QualityRepairFeatureFlags()
    baseline = _args("stage2m-exec-drift")
    _ensure_experiment_manifest(baseline, bundle, tmp_path, flags)

    drifted = _args("stage2m-exec-drift")
    drifted.support_max_concurrent_needs = 4
    drifted.endpoint_request_limit = 4
    with pytest.raises(ValueError, match="manifest differs"):
        _ensure_experiment_manifest(drifted, bundle, tmp_path, flags)


def test_execution_config_binds_frozen_planner_artifact_content(tmp_path: Path) -> None:
    bundle = HumanBenchmarkCompiler().compile(PILOT)
    flags = QualityRepairFeatureFlags()
    frozen = tmp_path / "frozen-planner.json"
    frozen.write_text('{"artifact_version": "planner-invocation.v2"}\n', encoding="utf-8")
    args = _args("stage2m-frozen-planner")
    args.frozen_planner_artifact = frozen

    _ensure_experiment_manifest(args, bundle, tmp_path, flags)

    payload = json.loads((tmp_path / "experiment_manifest.json").read_text("utf-8"))
    expected = "sha256:" + hashlib.sha256(frozen.read_bytes()).hexdigest()
    assert payload["execution_config"]["frozen_planner_artifact_hash"] == expected
    assert payload["execution_config_hash"] != payload["run_config_hash"]


def test_quality_repair_config_parses_json_enum_values_in_strict_mode(
    tmp_path: Path,
) -> None:
    config = tmp_path / "quality-repair.json"
    config.write_text(
        json.dumps(
            {
                "controller_mode": "deterministic_plus_agentic_delta",
                "curator_evidence_contract": "candidate_id_v2",
                "evidence_support_gate": "enforce_pre_candidate",
                "max_controller_decision_model_calls": 2,
                "max_agentic_actions": 8,
            }
        ),
        encoding="utf-8",
    )

    flags = _load_quality_repair_flags(Namespace(quality_repair_config=config))

    assert flags.controller_mode is ControllerMode.DETERMINISTIC_PLUS_AGENTIC_DELTA
    assert flags.curator_evidence_contract is CuratorEvidenceContract.CANDIDATE_ID_V2
    assert flags.evidence_support_gate is EvidenceSupportGateMode.ENFORCE_PRE_CANDIDATE


def test_postgres_database_name_rejects_server_side_truncation() -> None:
    valid = "a" * 63
    assert _loopback_postgres_url(f"postgresql+psycopg://127.0.0.1:5432/{valid}").endswith(valid)

    with pytest.raises(ValueError, match="at most 63 UTF-8 bytes"):
        _loopback_postgres_url(f"postgresql+psycopg://127.0.0.1:5432/{'a' * 64}")
    with pytest.raises(ValueError, match="at most 63 UTF-8 bytes"):
        _loopback_postgres_url(f"postgresql+psycopg://127.0.0.1:5432/{'界' * 22}")


def test_new_real_project_creates_objects_but_resume_fails_closed(tmp_path: Path) -> None:
    args = _args()
    args.resume_project = None
    _prepare_project_artifact_directory(args, tmp_path)
    _prepare_project_artifact_directory(args, tmp_path)
    assert (tmp_path / "objects").is_dir()

    missing = tmp_path / "resume"
    missing.mkdir()
    args.resume_project = missing
    with pytest.raises(ValueError, match="missing project artifact"):
        _prepare_project_artifact_directory(args, missing)

    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "objects").write_text("not a directory", encoding="utf-8")
    args.resume_project = None
    with pytest.raises(ValueError, match="not a directory"):
        _prepare_project_artifact_directory(args, invalid)
