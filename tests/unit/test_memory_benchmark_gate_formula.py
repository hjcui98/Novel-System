from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.memory_benchmark import (
    AcceptedEvidenceContract,
    BenchmarkInformationProfile,
    ContentAddressedGoldMetricDescriptor,
    ContextAssemblyStatus,
    EvaluatorManifestContract,
    EvidenceLedger,
    EvidenceLedgerEntry,
    EvidenceSet,
    EvidenceStageCoverage,
    EvidenceStageFailure,
    GateMetricAxis,
    GateMetricAxisResult,
    GoldMatchStatus,
    GoldMetricContract,
    GoldMetricDescriptor,
    MemoryBenchmarkCaseArmReport,
    MemoryBenchmarkEvaluationReport,
    MemoryBenchmarkUnifiedReport,
    PerGoldComparison,
    PerGoldStageLossDiagnostic,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_metric_contracts import (
    GATE_METRIC_FORMULA_HASH,
    GATE_METRIC_FORMULA_VERSION,
    GoldMetricContractBuilder,
)
from novel_agent.services.memory_benchmark_reporting import MemoryBenchmarkReporter, _ResolvedGold
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

SOURCE = Path(__file__).parents[2] / "benchmarks" / "private" / "ztj_memory_pilot_v0.1"


def _put_contract(repository: ArtifactRepository, value: object) -> ArtifactRef:
    payload = value.model_dump(mode="json") if isinstance(value, DomainModel) else value
    return repository.put(
        canonical_json_bytes(payload),
        "application/vnd.test.gate-contract+json",
        SchemaVersion("1.0.0"),
    )


def test_gate_metric_formula_identity_is_frozen() -> None:
    assert GATE_METRIC_FORMULA_VERSION == "gate_metric_formula.v2"
    assert (
        GATE_METRIC_FORMULA_HASH.root
        == "sha256:6fdfa956164b1823cec6b8058a63f9a710dc53b5faff00eae32d8d7046915cec"
    )


def test_metric_descriptor_builder_rejects_untyped_gate_gold(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    gold = (
        make_synthetic_bundle()
        .case_manifests[0]
        .observed_use_gold[0]
        .model_copy(update={"gold_type": None})
    )
    with pytest.raises(ValueError, match="has no GoldType"):
        GoldMetricContractBuilder(repository).build(
            gold_items=(gold,),
            evaluator_manifest_id=StableId("evaluator-manifest.untyped"),
            evaluator_manifest_hash=ArtifactId("sha256:" + "1" * 64),
        )


def test_frozen_profile_denominators_match_gate_m4_baseline(tmp_path: Path) -> None:
    bundle = HumanBenchmarkCompiler().compile(SOURCE)
    all_gold = tuple(
        gold
        for case in bundle.case_manifests
        for gold in (
            *case.observed_use_gold,
            *case.operational_constraint_gold,
            *case.plan_obligation_gold,
        )
    )
    expected = {
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF: {
            "applicable": (47, None),
            GateMetricAxis.CURRENT_STATE_ACCURACY: (36, 100.0),
            GateMetricAxis.OPERATIONAL_PLAN_COVERAGE: (26, 71.0),
            GateMetricAxis.KEY_HISTORICAL_EVIDENCE_RECALL: (9, 29.0),
        },
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED: {
            "applicable": (72, None),
            GateMetricAxis.CURRENT_STATE_ACCURACY: (36, 100.0),
            GateMetricAxis.OPERATIONAL_PLAN_COVERAGE: (51, 96.0),
            GateMetricAxis.KEY_HISTORICAL_EVIDENCE_RECALL: (9, 29.0),
        },
    }

    for profile, baseline in expected.items():
        applicable = tuple(item for item in all_gold if profile in item.applicable_profiles)
        repository = ArtifactRepository(FilesystemObjectStore(tmp_path / profile.value / "objects"))
        builder = GoldMetricContractBuilder(repository)
        manifest_id = StableId(f"evaluator-manifest.denominator.{profile.value}")
        _manifest, manifest_ref = builder.build_manifest(
            gold_items=applicable,
            evaluator_manifest_id=manifest_id,
        )
        descriptors = builder.build(
            gold_items=applicable,
            evaluator_manifest_id=manifest_id,
            evaluator_manifest_hash=manifest_ref.artifact_id,
        )

        assert len(descriptors) == baseline["applicable"][0]
        for axis in GateMetricAxis:
            selected = tuple(
                binding.descriptor
                for binding in descriptors.values()
                if axis in MemoryBenchmarkReporter.axes_for_descriptor(binding.descriptor)
            )
            assert (len(selected), sum(item.weight for item in selected)) == baseline[axis]


def _formula_case(
    repository: ArtifactRepository,
    *,
    suffix: str,
    checkpoint: int,
    current_weight: float,
    current_status: GoldMatchStatus,
    assembly_status: ContextAssemblyStatus = ContextAssemblyStatus.READY,
    historical_status: GoldMatchStatus = GoldMatchStatus.HIT,
    include_historical: bool = True,
) -> MemoryBenchmarkCaseArmReport:
    synthetic = make_synthetic_bundle()
    source_case = synthetic.case_manifests[0]
    current = source_case.operational_constraint_gold[0].model_copy(
        update={
            "gold_id": StableId(f"gold.formula.current.{suffix}"),
            "weight": current_weight,
        }
    )
    historical = source_case.observed_use_gold[0].model_copy(
        update={"gold_id": StableId(f"gold.formula.historical.{suffix}")}
    )
    present = historical.evidence_refs[0]
    absent = present.model_copy(
        update={
            "evidence_id": StableId(f"evidence.formula.absent.{suffix}"),
            "object_hash": ArtifactId("sha256:" + "f" * 64),
        }
    )
    historical = historical.model_copy(
        update={
            "accepted_evidence_sets": (
                EvidenceSet(
                    evidence_set_id=StableId(f"accepted.formula.partial.{suffix}"),
                    evidence_refs=(present, absent),
                ),
                EvidenceSet(
                    evidence_set_id=StableId(f"accepted.formula.complete.{suffix}"),
                    evidence_refs=(present,),
                ),
            )
        }
    )
    gold_items = (current, historical) if include_historical else (current,)
    manifest_id = StableId(f"evaluator-manifest.formula.{suffix}")
    builder = GoldMetricContractBuilder(repository)
    _manifest, manifest_ref = builder.build_manifest(
        gold_items=gold_items,
        evaluator_manifest_id=manifest_id,
    )
    descriptors = builder.build(
        gold_items=gold_items,
        evaluator_manifest_id=manifest_id,
        evaluator_manifest_hash=manifest_ref.artifact_id,
    )
    statuses = {
        current.gold_id: current_status,
        historical.gold_id: historical_status,
    }
    comparisons = tuple(
        PerGoldComparison(
            gold_id=gold.gold_id,
            status=statuses[gold.gold_id],
            weight=gold.weight,
            mandatory=gold.mandatory,
            gold_metric_descriptor_ref=descriptors[gold.gold_id].descriptor_ref,
            gold_metric_descriptor_hash=descriptors[gold.gold_id].descriptor_hash,
            explanation="locked formula fixture",
        )
        for gold in gold_items
    )
    ledger = EvidenceLedger(
        contract_version="evidence_ledger.formula.v1",
        entries=(
            EvidenceLedgerEntry(
                ledger_id=StableId(f"ledger.formula.{suffix}"),
                evidence_refs=tuple(
                    dict.fromkeys(
                        (
                            *current.evidence_refs,
                            *(historical.evidence_refs if include_historical else ()),
                        )
                    )
                ),
                claim_excerpt="formula fixture support",
                source_commit=synthetic.world_roots[0].source_commit,
                information_scope="writer_visible",
            ),
        ),
        rendered_tokens=10,
    )
    ledger_ref = repository.put(
        canonical_json_bytes(ledger.model_dump(mode="json")),
        "application/vnd.test.formula-ledger+json",
        SchemaVersion("1.0.0"),
    )
    score = {
        GoldMatchStatus.HIT: 1.0,
        GoldMatchStatus.PARTIAL: 0.5,
        GoldMatchStatus.MISS: 0.0,
        GoldMatchStatus.UNTRACEABLE: 0.0,
        GoldMatchStatus.CONTRADICTS: 0.0,
    }
    total_weight = sum(item.weight for item in comparisons)
    weighted = sum(item.weight * score[item.status] for item in comparisons) / total_weight
    evaluation = MemoryBenchmarkEvaluationReport(
        evaluator_version="per_gold_v3",
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        comparisons=comparisons,
        weighted_coverage=weighted,
        mandatory_hit_rate=(1.0 if current_status is GoldMatchStatus.HIT else 0.0),
        contradiction_rate=sum(item.status is GoldMatchStatus.CONTRADICTS for item in comparisons)
        / len(comparisons),
        untraceable_rate=sum(item.status is GoldMatchStatus.UNTRACEABLE for item in comparisons)
        / len(comparisons),
        freeze_receipt_id=StableId(f"freeze.formula.{suffix}"),
        evaluator_manifest_id=manifest_id,
        evaluator_manifest_ref=manifest_ref,
        evaluator_manifest_hash=manifest_ref.artifact_id,
        gate_metric_formula_version=GATE_METRIC_FORMULA_VERSION,
        gate_metric_formula_hash=GATE_METRIC_FORMULA_HASH,
    )
    return MemoryBenchmarkCaseArmReport(
        case_id=StableId(f"case.formula.{suffix}"),
        checkpoint_chapter=checkpoint,
        arm="A",
        code_version="formula-test.v1",
        run_config_hash=ArtifactId("sha256:" + "1" * 64),
        benchmark_contract_hash=ArtifactId("sha256:" + "2" * 64),
        matcher_version="gold_evidence_matcher.v4",
        writer_token_budget=4000,
        evidence_ledger_token_budget=12_000,
        assembly_status=assembly_status,
        writer_tokens=100,
        evidence_tokens=10,
        selected_unit_count=1,
        comparable=True,
        writer_evidence_ledger_ref=ledger_ref,
        evaluation=evaluation,
    )


def _formal_cases(
    repository: ArtifactRepository,
    profile: BenchmarkInformationProfile,
) -> tuple[MemoryBenchmarkCaseArmReport, ...]:
    bundle = HumanBenchmarkCompiler().compile(SOURCE)
    cases: list[MemoryBenchmarkCaseArmReport] = []
    for source_case in bundle.case_manifests:
        checkpoint = source_case.history_range[1]
        gold_items = tuple(
            item
            for item in (
                *source_case.observed_use_gold,
                *source_case.operational_constraint_gold,
                *source_case.plan_obligation_gold,
            )
            if profile in item.applicable_profiles
        )
        manifest_id = StableId(f"evaluator-manifest.{source_case.case_id.root}.{profile.value}")
        builder = GoldMetricContractBuilder(repository)
        _manifest, manifest_ref = builder.build_manifest(
            gold_items=gold_items,
            evaluator_manifest_id=manifest_id,
        )
        descriptors = builder.build(
            gold_items=gold_items,
            evaluator_manifest_id=manifest_id,
            evaluator_manifest_hash=manifest_ref.artifact_id,
        )
        comparisons = tuple(
            PerGoldComparison(
                gold_id=gold.gold_id,
                status=GoldMatchStatus.HIT,
                weight=gold.weight,
                mandatory=gold.mandatory,
                gold_metric_descriptor_ref=descriptors[gold.gold_id].descriptor_ref,
                gold_metric_descriptor_hash=descriptors[gold.gold_id].descriptor_hash,
                explanation="formal perfect fixture",
            )
            for gold in gold_items
        )
        evidence_refs = tuple(
            dict.fromkeys(ref for gold in gold_items for ref in gold.evidence_refs)
        )
        ledger = EvidenceLedger(
            contract_version="evidence_ledger.formal.v1",
            entries=(
                EvidenceLedgerEntry(
                    ledger_id=StableId(f"ledger.formal.{profile.value}.{checkpoint}"),
                    evidence_refs=evidence_refs,
                    claim_excerpt="formal perfect fixture",
                    source_commit=bundle.world_roots[0].source_commit,
                    information_scope="writer_visible",
                ),
            ),
            rendered_tokens=100,
        )
        ledger_ref = repository.put(
            canonical_json_bytes(ledger.model_dump(mode="json")),
            "application/vnd.test.formal-ledger+json",
            SchemaVersion("1.0.0"),
        )
        evaluation = MemoryBenchmarkEvaluationReport(
            evaluator_version="per_gold_v3",
            profile=profile,
            comparisons=comparisons,
            weighted_coverage=1.0,
            mandatory_hit_rate=1.0,
            contradiction_rate=0.0,
            untraceable_rate=0.0,
            freeze_receipt_id=StableId(f"freeze.formal.{profile.value}.{checkpoint}"),
            evaluator_manifest_id=manifest_id,
            evaluator_manifest_ref=manifest_ref,
            evaluator_manifest_hash=manifest_ref.artifact_id,
            gate_metric_formula_version=GATE_METRIC_FORMULA_VERSION,
            gate_metric_formula_hash=GATE_METRIC_FORMULA_HASH,
        )
        cases.append(
            MemoryBenchmarkCaseArmReport(
                case_id=source_case.case_id,
                checkpoint_chapter=checkpoint,
                arm="A",
                code_version="formal-test.v1",
                run_config_hash=ArtifactId("sha256:" + "1" * 64),
                benchmark_contract_hash=bundle.content_hash,
                matcher_version="gold_evidence_matcher.v4",
                writer_token_budget=4000,
                evidence_ledger_token_budget=12_000,
                assembly_status=ContextAssemblyStatus.READY,
                writer_tokens=100,
                evidence_tokens=100,
                selected_unit_count=1,
                comparable=False,
                writer_evidence_ledger_ref=ledger_ref,
                evaluation=evaluation,
            )
        )
    return tuple(cases)


@pytest.mark.parametrize("profile", tuple(BenchmarkInformationProfile))
def test_formal_five_point_report_passes_only_with_frozen_denominators(
    tmp_path: Path,
    profile: BenchmarkInformationProfile,
) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / profile.value / "objects"))
    cases = _formal_cases(repository, profile)

    report = MemoryBenchmarkReporter(artifact_reader=repository.read_verified).aggregate(
        profile=profile,
        cases=cases,
    )

    assert report.case_count == 5
    assert report.formal_contract_validated is True
    assert report.gate_passed is True
    assert report.trace_complete is True
    assert report.contradiction_free is True
    assert report.current_state.value == 1.0
    assert report.operational_plan.value == 1.0
    assert report.historical.value == 1.0


def test_cross_profile_delta_requires_profile_order_and_is_diagnostic_only(
    tmp_path: Path,
) -> None:
    visible_repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "visible"))
    planned_repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "planned"))
    visible = MemoryBenchmarkReporter(artifact_reader=visible_repository.read_verified).aggregate(
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        cases=_formal_cases(
            visible_repository,
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        ),
    )
    planned = MemoryBenchmarkReporter(artifact_reader=planned_repository.read_verified).aggregate(
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        cases=_formal_cases(
            planned_repository,
            BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        ),
    )
    assert (
        MemoryBenchmarkReporter.cross_profile_delta(visible, planned)[
            "current_state_accuracy_delta"
        ]
        == 0.0
    )
    with pytest.raises(ValueError, match="visible report"):
        MemoryBenchmarkReporter.cross_profile_delta(planned, planned)
    with pytest.raises(ValueError, match="planned report"):
        MemoryBenchmarkReporter.cross_profile_delta(visible, visible)


def test_gate_metric_domain_contracts_fail_closed_on_identity_drift(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    case = _formal_cases(
        repository,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )[0]
    evaluation = case.evaluation
    comparison = evaluation.comparisons[0]
    descriptor = GoldMetricDescriptor.model_validate_json(
        repository.read_verified(comparison.gold_metric_descriptor_ref)
    )
    gold_contract = GoldMetricContract.model_validate_json(
        repository.read_verified(descriptor.gold_contract_ref)
    )
    accepted = AcceptedEvidenceContract.model_validate_json(
        repository.read_verified(descriptor.accepted_evidence_contract_ref)
    )
    manifest = EvaluatorManifestContract.model_validate_json(
        repository.read_verified(evaluation.evaluator_manifest_ref)
    )

    with pytest.raises(ValidationError, match="at least one alternative"):
        AcceptedEvidenceContract.model_validate(accepted.model_dump() | {"alternatives": ()})
    with pytest.raises(ValidationError, match="alternatives must be unique"):
        AcceptedEvidenceContract.model_validate(
            accepted.model_dump()
            | {"alternatives": (accepted.alternatives[0], accepted.alternatives[0])}
        )
    with pytest.raises(ValidationError, match="applicable profile"):
        GoldMetricContract.model_validate(gold_contract.model_dump() | {"applicable_profiles": ()})
    with pytest.raises(ValidationError, match="profiles must be unique"):
        GoldMetricContract.model_validate(
            gold_contract.model_dump()
            | {
                "applicable_profiles": (
                    BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                    BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                )
            }
        )
    with pytest.raises(ValidationError, match="ref/hash mismatch"):
        GoldMetricContract.model_validate(
            gold_contract.model_dump()
            | {"accepted_evidence_contract_hash": ArtifactId("sha256:" + "0" * 64)}
        )
    for update, message in (
        ({"gold_contract_hash": ArtifactId("sha256:" + "0" * 64)}, "Gold contract"),
        (
            {"accepted_evidence_contract_hash": ArtifactId("sha256:" + "0" * 64)},
            "accepted evidence",
        ),
        ({"applicable_profiles": ()}, "applicable profile"),
        (
            {
                "applicable_profiles": (
                    BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                    BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                )
            },
            "profiles must be unique",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            GoldMetricDescriptor.model_validate(descriptor.model_dump() | update)
    with pytest.raises(ValidationError, match="at least one Gold"):
        EvaluatorManifestContract.model_validate(manifest.model_dump() | {"gold_ids": ()})
    with pytest.raises(ValidationError, match="must be unique"):
        EvaluatorManifestContract.model_validate(
            manifest.model_dump() | {"gold_ids": (manifest.gold_ids[0], manifest.gold_ids[0])}
        )
    binding = ContentAddressedGoldMetricDescriptor(
        descriptor=descriptor,
        descriptor_ref=comparison.gold_metric_descriptor_ref,
        descriptor_hash=comparison.gold_metric_descriptor_hash,
    )
    with pytest.raises(ValidationError, match="descriptor ref/hash mismatch"):
        ContentAddressedGoldMetricDescriptor.model_validate(
            binding.model_dump() | {"descriptor_hash": ArtifactId("sha256:" + "0" * 64)}
        )
    with pytest.raises(ValidationError, match="per-Gold descriptor"):
        PerGoldComparison.model_validate(
            comparison.model_dump()
            | {"gold_metric_descriptor_hash": ArtifactId("sha256:" + "0" * 64)}
        )
    with pytest.raises(ValidationError, match="cannot exceed"):
        EvidenceStageCoverage(accepted_reference_count=0, matched_reference_count=1)
    with pytest.raises(ValidationError, match="manifest ref/hash mismatch"):
        MemoryBenchmarkEvaluationReport.model_validate(
            evaluation.model_dump() | {"evaluator_manifest_hash": ArtifactId("sha256:" + "0" * 64)}
        )
    axis = GateMetricAxisResult(
        axis=GateMetricAxis.CURRENT_STATE_ACCURACY,
        item_count=1,
        denominator_weight=1.0,
        weighted_score_sum=1.0,
        value=1.0,
        threshold=0.95,
        passed=True,
    )
    with pytest.raises(ValidationError, match="weighted numerator"):
        GateMetricAxisResult.model_validate(axis.model_dump() | {"value": 0.5})
    with pytest.raises(ValidationError, match="pass flag"):
        GateMetricAxisResult.model_validate(axis.model_dump() | {"passed": False})

    formal = MemoryBenchmarkReporter(artifact_reader=repository.read_verified).aggregate(
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        cases=_formal_cases(repository, BenchmarkInformationProfile.VISIBLE_AT_CUTOFF),
    )
    with pytest.raises(ValidationError, match="cannot pass"):
        MemoryBenchmarkUnifiedReport.model_validate(
            formal.model_dump() | {"formal_contract_validated": False}
        )


def test_weight_micro_partial_and_best_historical_alternative(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    low_weight_partial = _formula_case(
        repository,
        suffix="low",
        checkpoint=20,
        current_weight=1.0,
        current_status=GoldMatchStatus.PARTIAL,
    )
    high_weight_hit = _formula_case(
        repository,
        suffix="high",
        checkpoint=40,
        current_weight=3.0,
        current_status=GoldMatchStatus.HIT,
    )

    report = MemoryBenchmarkReporter(
        artifact_reader=repository.read_verified,
        enforce_formal_contract=False,
    ).aggregate(
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        cases=(low_weight_partial, high_weight_hit),
    )

    assert report.current_state.value == pytest.approx(0.875)
    assert report.current_state.value != pytest.approx((0.5 + 1.0) / 2)
    assert report.operational_plan.value == pytest.approx(0.875)
    assert report.historical.value == 1.0
    assert report.formal_contract_validated is False
    assert report.gate_passed is False


def test_formal_report_requires_arm_a_five_point_case_matrix(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    base = _formula_case(
        repository,
        suffix="formal-shape",
        checkpoint=20,
        current_weight=1.0,
        current_status=GoldMatchStatus.HIT,
    )
    reporter = MemoryBenchmarkReporter(artifact_reader=repository.read_verified)
    with pytest.raises(ValueError, match="exactly five"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(base,),
        )

    case_ids = ("ZTJ-P001", "ZTJ-P002", "ZTJ-P003", "ZTJ-P004", "ZTJ-P005")
    checkpoints = (20, 40, 60, 80, 95)
    shaped = tuple(
        base.model_copy(
            update={
                "case_id": StableId(case_id),
                "checkpoint_chapter": checkpoint,
                "arm": "B",
                "evaluation": base.evaluation.model_copy(
                    update={"freeze_receipt_id": StableId(f"freeze.formal-shape.{checkpoint}")}
                ),
            }
        )
        for case_id, checkpoint in zip(case_ids, checkpoints, strict=True)
    )
    with pytest.raises(ValueError, match="Arm A only"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=shaped,
        )


def test_formal_report_rejects_case_identity_and_frozen_run_drift(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    base = _formula_case(
        repository,
        suffix="formal-identity",
        checkpoint=20,
        current_weight=1.0,
        current_status=GoldMatchStatus.HIT,
    )
    reporter = MemoryBenchmarkReporter(artifact_reader=repository.read_verified)
    case_ids = ("ZTJ-P001", "ZTJ-P002", "ZTJ-P003", "ZTJ-P004", "ZTJ-P005")
    checkpoints = (20, 40, 60, 80, 95)
    shaped = tuple(
        base.model_copy(
            update={
                "case_id": StableId(case_id),
                "checkpoint_chapter": checkpoint,
                "evaluation": base.evaluation.model_copy(
                    update={
                        "freeze_receipt_id": StableId(f"freeze.formal-identity.{checkpoint}"),
                        "evaluator_manifest_id": StableId(
                            f"evaluator-manifest.{case_id}.visible_at_cutoff"
                        ),
                    }
                ),
            }
        )
        for case_id, checkpoint in zip(case_ids, checkpoints, strict=True)
    )
    wrong_case = (
        shaped[0].model_copy(update={"case_id": StableId("ZTJ-P999")}),
        *shaped[1:],
    )
    with pytest.raises(ValueError, match="checkpoint/case identity"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=wrong_case,
        )

    drifted = (
        shaped[0].model_copy(update={"run_config_hash": ArtifactId("sha256:" + "9" * 64)}),
        *shaped[1:],
    )
    with pytest.raises(ValueError, match="frozen run identity"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=drifted,
        )


def test_typed_failure_counts_zero_and_hard_vetoes_are_preserved(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    typed_failure = _formula_case(
        repository,
        suffix="typed",
        checkpoint=20,
        current_weight=1.0,
        current_status=GoldMatchStatus.MISS,
        historical_status=GoldMatchStatus.MISS,
        assembly_status=ContextAssemblyStatus.EVIDENCE_INSUFFICIENT,
    )
    report = MemoryBenchmarkReporter(
        artifact_reader=repository.read_verified,
        enforce_formal_contract=False,
    ).aggregate(
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        cases=(typed_failure,),
    )
    assert report.current_state.value == 0.0
    assert report.historical.value == 0.0

    untraceable = _formula_case(
        repository,
        suffix="untraceable",
        checkpoint=40,
        current_weight=1.0,
        current_status=GoldMatchStatus.HIT,
        historical_status=GoldMatchStatus.UNTRACEABLE,
    )
    vetoed = MemoryBenchmarkReporter(
        artifact_reader=repository.read_verified,
        enforce_formal_contract=False,
    ).aggregate(
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        cases=(untraceable,),
    )
    assert vetoed.trace_complete is False
    assert vetoed.gate_passed is False


def test_formula_descriptor_and_empty_denominator_drift_fail_closed(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    case = _formula_case(
        repository,
        suffix="drift",
        checkpoint=20,
        current_weight=1.0,
        current_status=GoldMatchStatus.HIT,
    )
    reporter = MemoryBenchmarkReporter(
        artifact_reader=repository.read_verified,
        enforce_formal_contract=False,
    )
    wrong_formula = case.model_copy(
        update={
            "evaluation": case.evaluation.model_copy(
                update={"gate_metric_formula_hash": ArtifactId("sha256:" + "0" * 64)}
            )
        }
    )
    with pytest.raises(ValueError, match="formula version/hash"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(wrong_formula,),
        )

    first = case.evaluation.comparisons[0]
    descriptor_drift = case.model_copy(
        update={
            "evaluation": case.evaluation.model_copy(
                update={
                    "comparisons": (
                        first.model_copy(update={"weight": first.weight + 1}),
                        *case.evaluation.comparisons[1:],
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="descriptor disagree"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(descriptor_drift,),
        )

    no_historical = _formula_case(
        repository,
        suffix="empty",
        checkpoint=60,
        current_weight=1.0,
        current_status=GoldMatchStatus.HIT,
        include_historical=False,
    )
    with pytest.raises(ValueError, match="denominator is empty"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(no_historical,),
        )


def test_aggregator_uses_only_content_addressed_bundle_and_rejects_artifact_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    case = _formula_case(
        repository,
        suffix="content-addressed",
        checkpoint=20,
        current_weight=1.0,
        current_status=GoldMatchStatus.HIT,
    )

    def workspace_yaml_forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("aggregator attempted to read mutable workspace YAML")

    monkeypatch.setattr("yaml.safe_load", workspace_yaml_forbidden)
    report = MemoryBenchmarkReporter(
        artifact_reader=repository.read_verified,
        enforce_formal_contract=False,
    ).aggregate(
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        cases=(case,),
    )
    assert report.case_count == 1

    comparison = case.evaluation.comparisons[0]
    descriptor = GoldMetricDescriptor.model_validate_json(
        repository.read_verified(comparison.gold_metric_descriptor_ref)
    )

    def corrupt_accepted_evidence(ref: ArtifactRef) -> bytes:
        if ref == descriptor.accepted_evidence_contract_ref:
            return repository.read_verified(descriptor.accepted_evidence_contract_ref) + b"x"
        return repository.read_verified(ref)

    with pytest.raises(ValueError, match="artifact integrity"):
        MemoryBenchmarkReporter(
            artifact_reader=corrupt_accepted_evidence,
            enforce_formal_contract=False,
        ).aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(case,),
        )

    manifest_drift = case.model_copy(
        update={
            "evaluation": case.evaluation.model_copy(
                update={
                    "evaluator_manifest_ref": comparison.gold_metric_descriptor_ref,
                    "evaluator_manifest_hash": comparison.gold_metric_descriptor_hash,
                }
            )
        }
    )
    with pytest.raises(ValueError):
        MemoryBenchmarkReporter(
            artifact_reader=repository.read_verified,
            enforce_formal_contract=False,
        ).aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(manifest_drift,),
        )


def test_reporter_rejects_aggregate_shape_and_manifest_drift(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    reporter = MemoryBenchmarkReporter(
        artifact_reader=repository.read_verified,
        enforce_formal_contract=False,
    )
    first = _formula_case(
        repository,
        suffix="aggregate-shape-first",
        checkpoint=20,
        current_weight=1.0,
        current_status=GoldMatchStatus.HIT,
    )
    second = _formula_case(
        repository,
        suffix="aggregate-shape-second",
        checkpoint=40,
        current_weight=1.0,
        current_status=GoldMatchStatus.HIT,
    )
    with pytest.raises(ValueError, match="at least one case"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(),
        )
    with pytest.raises(ValueError, match="mix information profiles"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(
                first.model_copy(
                    update={
                        "evaluation": first.evaluation.model_copy(
                            update={"profile": BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED}
                        )
                    }
                ),
            ),
        )
    with pytest.raises(ValueError, match="separately per arm"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(first, second.model_copy(update={"arm": "B"})),
        )
    with pytest.raises(ValueError, match="duplicate case/arm"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(first, first),
        )

    with pytest.raises(ValueError, match="manifest id mismatch"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(
                first.model_copy(
                    update={
                        "evaluation": first.evaluation.model_copy(
                            update={
                                "evaluator_manifest_id": StableId(
                                    "evaluator-manifest.wrong-runtime-id"
                                )
                            }
                        )
                    }
                ),
            ),
        )
    with pytest.raises(ValueError, match="no applicable Gold"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(
                first.model_copy(
                    update={"evaluation": first.evaluation.model_copy(update={"comparisons": ()})}
                ),
            ),
        )
    comparison = first.evaluation.comparisons[0]
    with pytest.raises(ValueError, match="duplicate Gold"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(
                first.model_copy(
                    update={
                        "evaluation": first.evaluation.model_copy(
                            update={"comparisons": (comparison, comparison)}
                        )
                    }
                ),
            ),
        )

    original_manifest = EvaluatorManifestContract.model_validate_json(
        repository.read_verified(first.evaluation.evaluator_manifest_ref)
    )
    shortened_manifest = original_manifest.model_copy(
        update={"gold_ids": (original_manifest.gold_ids[0],)}
    )
    shortened_ref = _put_contract(repository, shortened_manifest)
    rebound_comparisons = []
    for item in first.evaluation.comparisons:
        item_descriptor = GoldMetricDescriptor.model_validate_json(
            repository.read_verified(item.gold_metric_descriptor_ref)
        )
        rebound_ref = _put_contract(
            repository,
            item_descriptor.model_copy(
                update={"evaluator_manifest_hash": shortened_ref.artifact_id}
            ),
        )
        rebound_comparisons.append(
            item.model_copy(
                update={
                    "gold_metric_descriptor_ref": rebound_ref,
                    "gold_metric_descriptor_hash": rebound_ref.artifact_id,
                }
            )
        )
    with pytest.raises(ValueError, match="Gold ids do not match"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(
                first.model_copy(
                    update={
                        "evaluation": first.evaluation.model_copy(
                            update={
                                "evaluator_manifest_ref": shortened_ref,
                                "evaluator_manifest_hash": shortened_ref.artifact_id,
                                "comparisons": tuple(rebound_comparisons),
                            }
                        )
                    }
                ),
            ),
        )

    repeated = first.model_copy(
        update={
            "case_id": StableId("case.formula.aggregate-shape-repeat"),
            "checkpoint_chapter": 40,
            "evaluation": first.evaluation.model_copy(
                update={"freeze_receipt_id": StableId("freeze.formula.aggregate-shape-repeat")}
            ),
        }
    )
    with pytest.raises(ValueError, match="repeats a Gold"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(first, repeated),
        )

    coverage = EvidenceStageCoverage(accepted_reference_count=1, matched_reference_count=1)
    diagnostic = PerGoldStageLossDiagnostic(
        gold_id=StableId("gold.formula.not-in-comparisons"),
        candidate=coverage,
        rank_selected=coverage,
        stage1_selected=coverage,
        writer_ledger=coverage,
        primary_failure=EvidenceStageFailure.COMPLETE,
    )
    with pytest.raises(ValueError, match="diagnostics do not cover"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(
                first.model_copy(
                    update={
                        "evaluation": first.evaluation.model_copy(
                            update={"stage_loss_diagnostics": (diagnostic,)}
                        )
                    }
                ),
            ),
        )


def test_reporter_rejects_descriptor_contract_and_typed_failure_drift(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    reporter = MemoryBenchmarkReporter(
        artifact_reader=repository.read_verified,
        enforce_formal_contract=False,
    )
    case = _formula_case(
        repository,
        suffix="resolved-contract",
        checkpoint=20,
        current_weight=1.0,
        current_status=GoldMatchStatus.HIT,
    )
    comparison = case.evaluation.comparisons[0]
    descriptor = GoldMetricDescriptor.model_validate_json(
        repository.read_verified(comparison.gold_metric_descriptor_ref)
    )

    def case_with_descriptor(updated: GoldMetricDescriptor) -> MemoryBenchmarkCaseArmReport:
        descriptor_ref = _put_contract(repository, updated)
        replacement = comparison.model_copy(
            update={
                "gold_metric_descriptor_ref": descriptor_ref,
                "gold_metric_descriptor_hash": descriptor_ref.artifact_id,
            }
        )
        return case.model_copy(
            update={
                "evaluation": case.evaluation.model_copy(
                    update={"comparisons": (replacement, *case.evaluation.comparisons[1:])}
                )
            }
        )

    with pytest.raises(ValueError, match="descriptor evaluator manifest"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(
                case_with_descriptor(
                    descriptor.model_copy(
                        update={"evaluator_manifest_id": StableId("manifest.descriptor.wrong")}
                    )
                ),
            ),
        )
    with pytest.raises(ValueError, match="not applicable"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(
                case_with_descriptor(
                    descriptor.model_copy(
                        update={
                            "applicable_profiles": (
                                BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
                            )
                        }
                    )
                ),
            ),
        )

    contract = GoldMetricContract.model_validate_json(
        repository.read_verified(descriptor.gold_contract_ref)
    )
    drifted_contract_ref = _put_contract(
        repository,
        contract.model_copy(update={"weight": contract.weight + 1}),
    )
    with pytest.raises(ValueError, match="Gold contract disagree"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(
                case_with_descriptor(
                    descriptor.model_copy(
                        update={
                            "gold_contract_ref": drifted_contract_ref,
                            "gold_contract_hash": drifted_contract_ref.artifact_id,
                        }
                    )
                ),
            ),
        )

    accepted = AcceptedEvidenceContract.model_validate_json(
        repository.read_verified(descriptor.accepted_evidence_contract_ref)
    )
    for update, message in (
        ({"gold_id": StableId("gold.accepted.wrong")}, "Gold id mismatch"),
        ({"matcher_version": "matcher.wrong"}, "matcher version mismatch"),
    ):
        accepted_ref = _put_contract(repository, accepted.model_copy(update=update))
        matching_contract_ref = _put_contract(
            repository,
            contract.model_copy(
                update={
                    "accepted_evidence_contract_ref": accepted_ref,
                    "accepted_evidence_contract_hash": accepted_ref.artifact_id,
                }
            ),
        )
        with pytest.raises(ValueError, match=message):
            reporter.aggregate(
                profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                cases=(
                    case_with_descriptor(
                        descriptor.model_copy(
                            update={
                                "gold_contract_ref": matching_contract_ref,
                                "gold_contract_hash": matching_contract_ref.artifact_id,
                                "accepted_evidence_contract_ref": accepted_ref,
                                "accepted_evidence_contract_hash": accepted_ref.artifact_id,
                            }
                        )
                    ),
                ),
            )

    with pytest.raises(ValueError, match="typed failure comparisons"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(
                case.model_copy(
                    update={"assembly_status": ContextAssemblyStatus.EVIDENCE_INSUFFICIENT}
                ),
            ),
        )
    with pytest.raises(ValueError, match="ref/hash mismatch"):
        reporter._read_model(
            comparison.gold_metric_descriptor_ref,
            GoldMetricDescriptor,
            expected_hash=ArtifactId("sha256:" + "0" * 64),
        )


def test_formal_report_rejects_freeze_matcher_manifest_and_denominator_drift(
    tmp_path: Path,
) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    reporter = MemoryBenchmarkReporter(artifact_reader=repository.read_verified)
    cases = _formal_cases(repository, BenchmarkInformationProfile.VISIBLE_AT_CUTOFF)
    duplicate_freeze = (
        cases[0],
        cases[1].model_copy(
            update={
                "evaluation": cases[1].evaluation.model_copy(
                    update={"freeze_receipt_id": cases[0].evaluation.freeze_receipt_id}
                )
            }
        ),
        *cases[2:],
    )
    with pytest.raises(ValueError, match="distinct freeze"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=duplicate_freeze,
        )
    wrong_matcher = tuple(
        case.model_copy(update={"matcher_version": "matcher.wrong"}) for case in cases
    )
    with pytest.raises(ValueError, match="matcher identity"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=wrong_matcher,
        )
    wrong_manifest = (
        cases[0].model_copy(
            update={
                "evaluation": cases[0].evaluation.model_copy(
                    update={"evaluator_manifest_id": StableId("evaluator-manifest.wrong")}
                )
            }
        ),
        *cases[1:],
    )
    with pytest.raises(ValueError, match="evaluator manifest identity"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=wrong_manifest,
        )


def test_aggregation_manifest_names_exact_child_reports_and_rejects_missing_child(
    tmp_path: Path,
) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    profile = BenchmarkInformationProfile.VISIBLE_AT_CUTOFF
    cases = _formal_cases(repository, profile)
    reporter = MemoryBenchmarkReporter(artifact_reader=repository.read_verified)
    children = []
    for case in cases:
        reference = repository.put(
            canonical_json_bytes(case.model_dump(mode="json")),
            "application/vnd.novel-agent.stage2m-case-arm-report+json",
            SchemaVersion("1.0.0"),
        )
        children.append(
            {
                "case_id": case.case_id.root,
                "checkpoint": case.checkpoint_chapter,
                "arm": case.arm,
                "artifact_ref": reference.model_dump(mode="json"),
            }
        )
    manifest = {
        "manifest_version": "stage2m_report_manifest.v1",
        "profile": profile.value,
        "formula_version": GATE_METRIC_FORMULA_VERSION,
        "formula_hash": GATE_METRIC_FORMULA_HASH.root,
        "children": children,
    }
    manifest_ref = repository.put(
        canonical_json_bytes(manifest),
        "application/vnd.novel-agent.stage2m-report-manifest+json",
        SchemaVersion("1.0.0"),
    )

    report = reporter.aggregate(
        profile=profile,
        cases=cases,
        aggregation_manifest_ref=manifest_ref,
    )
    assert report.aggregation_manifest_ref == manifest_ref

    missing_ref = repository.put(
        canonical_json_bytes(manifest | {"children": children[:-1]}),
        "application/vnd.novel-agent.stage2m-report-manifest+json",
        SchemaVersion("1.0.0"),
    )
    with pytest.raises(ValueError, match="missing, duplicate, or foreign"):
        MemoryBenchmarkReporter(artifact_reader=repository.read_verified).aggregate(
            profile=profile,
            cases=cases,
            aggregation_manifest_ref=missing_ref,
        )

    wrong_identity_ref = repository.put(
        canonical_json_bytes(manifest | {"formula_version": "wrong"}),
        "application/vnd.novel-agent.stage2m-report-manifest+json",
        SchemaVersion("1.0.0"),
    )
    with pytest.raises(ValueError, match="profile/formula identity mismatch"):
        reporter.aggregate(
            profile=profile,
            cases=cases,
            aggregation_manifest_ref=wrong_identity_ref,
        )

    foreign_report_ref = repository.put(
        canonical_json_bytes(
            cases[0].model_copy(update={"writer_tokens": 101}).model_dump(mode="json")
        ),
        "application/vnd.novel-agent.stage2m-case-arm-report+json",
        SchemaVersion("1.0.0"),
    )
    conflicting_children = [*children]
    conflicting_children[0] = {
        **conflicting_children[0],
        "artifact_ref": foreign_report_ref.model_dump(mode="json"),
    }
    conflict_ref = repository.put(
        canonical_json_bytes(manifest | {"children": conflicting_children}),
        "application/vnd.novel-agent.stage2m-report-manifest+json",
        SchemaVersion("1.0.0"),
    )
    with pytest.raises(ValueError, match="child report content conflicts"):
        reporter.aggregate(profile=profile, cases=cases, aggregation_manifest_ref=conflict_ref)

    payload = report.model_dump()
    with pytest.raises(ValidationError, match="must appear together"):
        MemoryBenchmarkUnifiedReport.model_validate(payload | {"aggregation_manifest_hash": None})
    with pytest.raises(ValidationError, match="ref/hash mismatch"):
        MemoryBenchmarkUnifiedReport.model_validate(
            payload | {"aggregation_manifest_hash": ArtifactId("sha256:" + "f" * 64)}
        )

    resolved: list[_ResolvedGold] = []
    for case in cases:
        ledger = EvidenceLedger.model_validate_json(
            repository.read_verified(case.writer_evidence_ledger_ref)
        )
        resolved.extend(
            reporter._resolve_comparison(case, comparison, ledger)
            for comparison in case.evaluation.comparisons
        )
    axes = tuple(
        reporter._axis(
            axis,
            tuple(
                item for item in resolved if axis in reporter.axes_for_descriptor(item.descriptor)
            ),
        )
        for axis in GateMetricAxis
    )
    with pytest.raises(ValueError, match="applicable-Gold denominator"):
        reporter._validate_denominators(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            all_gold=tuple(resolved[:-1]),
            axes=axes,
        )
    with pytest.raises(ValueError, match="denominator drift"):
        reporter._validate_denominators(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            all_gold=tuple(resolved),
            axes=(axes[0].model_copy(update={"item_count": 0}), *axes[1:]),
        )
