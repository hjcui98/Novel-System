#!/usr/bin/env python3
"""Recover and aggregate audited Stage 2 checkpoint results from the object store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory_benchmark import MemoryBenchmarkCaseArmReport
from novel_agent.domain.stage2 import (
    BenchmarkInformationProfile,
    PairedPilotCaseResult,
    ScenarioRunResult,
    Stage2PairedPilotReport,
)
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_reporting import MemoryBenchmarkReporter
from novel_agent.services.teacher_forced_benchmark_e2e import TeacherForcedBenchmarkE2ERunner

_FORMAL_CASE_BY_CHECKPOINT = {
    20: "ZTJ-P001",
    40: "ZTJ-P002",
    60: "ZTJ-P003",
    80: "ZTJ-P004",
    95: "ZTJ-P005",
}
_FORMAL_MANIFEST_IDENTITY_FIELDS = (
    "code_version",
    "run_config_hash",
    "benchmark_contract_hash",
    "matcher_version",
    "writer_token_budget",
    "evidence_ledger_token_budget",
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source", type=Path, required=True)
    value.add_argument("--output-directory", type=Path, required=True)
    value.add_argument(
        "--information-profile",
        choices=tuple(item.value for item in BenchmarkInformationProfile),
        default=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED.value,
    )
    return value


def _load_stage2m_case(path: Path) -> MemoryBenchmarkCaseArmReport:
    try:
        return MemoryBenchmarkCaseArmReport.model_validate_json(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        raise SystemExit(f"invalid Stage 2M case artifact {path}: {exc}") from exc


def _validate_formal_run_lifecycle(
    output_directory: Path,
    *,
    profile: BenchmarkInformationProfile,
    cases: tuple[MemoryBenchmarkCaseArmReport, ...],
) -> None:
    manifests = sorted(output_directory.rglob("experiment_manifest.json"))
    if not manifests:
        raise SystemExit("formal Stage 2M aggregation requires experiment manifests")
    manifest_payloads: list[dict[str, object]] = []
    for path in manifests:
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"invalid experiment manifest {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SystemExit(f"experiment manifest must be an object: {path}")
        if payload.get("code_source_dirty") is not False:
            raise SystemExit(f"formal run has dirty or unverified source: {path}")
        if payload.get("information_profile") != profile.value:
            raise SystemExit(f"formal run information profile drifted: {path}")
        for field in _FORMAL_MANIFEST_IDENTITY_FIELDS:
            value = payload.get(field)
            if value is None or value == "":
                raise SystemExit(f"formal run manifest is missing {field}: {path}")
        for field in ("code_commit", "code_source_fingerprint", "experiment_id"):
            value = payload.get(field)
            if value is None or value == "":
                raise SystemExit(f"formal run manifest is missing {field}: {path}")
        manifest_payloads.append(payload)

    identity_fields = (
        "code_commit",
        "code_source_fingerprint",
        "experiment_id",
        *_FORMAL_MANIFEST_IDENTITY_FIELDS,
    )
    manifest_identities = {
        tuple(payload[field] for field in identity_fields) for payload in manifest_payloads
    }
    if len(manifest_identities) != 1:
        raise SystemExit("formal run manifests do not share one frozen identity")
    manifest_identity = manifest_payloads[0]
    for case in cases:
        for field in _FORMAL_MANIFEST_IDENTITY_FIELDS:
            case_value = getattr(case, field)
            manifest_value = manifest_identity[field]
            if getattr(case_value, "root", case_value) != manifest_value:
                raise SystemExit(f"formal case {case.case_id.root} disagrees with manifest {field}")

    scenario_paths = sorted(output_directory.rglob("scenario_run.json"))
    if not scenario_paths:
        raise SystemExit("formal Stage 2M aggregation requires scenario_run.json artifacts")
    observed: dict[int, tuple[Path, StableId]] = {}
    for path in scenario_paths:
        try:
            scenario = ScenarioRunResult.model_validate_json(path.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, ValidationError) as exc:
            raise SystemExit(f"invalid scenario lifecycle artifact {path}: {exc}") from exc
        if not scenario.completed or scenario.blockers:
            raise SystemExit(f"scenario lifecycle is not closed: {path}")
        if not scenario.checkpoints:
            raise SystemExit(f"completed scenario has no checkpoints: {path}")
        if len(scenario.freezes) != len(scenario.checkpoints):
            raise SystemExit(f"scenario freeze count does not match checkpoints: {path}")
        if len(scenario.evaluator_reveals) != len(scenario.checkpoints):
            raise SystemExit(f"scenario reveal count does not match checkpoints: {path}")
        freeze_by_case = {item.case_id: item for item in scenario.freezes}
        if len(freeze_by_case) != len(scenario.freezes):
            raise SystemExit(f"scenario contains duplicate case freezes: {path}")
        for basis in scenario.checkpoints:
            checkpoint = basis.last_revealed_chapter
            expected_case = _FORMAL_CASE_BY_CHECKPOINT.get(checkpoint)
            if expected_case is None or basis.case_id.root != expected_case:
                raise SystemExit(f"scenario checkpoint/case identity mismatch: {path}")
            if not basis.future_isolation.passed:
                raise SystemExit(f"scenario future-isolation attestation failed: {path}")
            if checkpoint in observed:
                raise SystemExit(f"formal scenario lifecycle repeats C{checkpoint}")
            freeze = freeze_by_case.get(basis.case_id)
            if freeze is None or freeze.checkpoint_chapter != checkpoint:
                raise SystemExit(f"scenario freeze does not match checkpoint C{checkpoint}")
            if (
                freeze.canonical_commit != basis.canonical_commit
                or freeze.snapshot_id != basis.derived_snapshot_id
            ):
                raise SystemExit(f"scenario freeze basis mismatch at C{checkpoint}")
            reveals = [
                item for item in scenario.evaluator_reveals if item.freeze_id == freeze.freeze_id
            ]
            if len(reveals) != 1:
                raise SystemExit(f"scenario reveal does not match freeze at C{checkpoint}")
            reveal = reveals[0]
            if not reveal.evaluator_context_destroyed or reveal.completed_at < freeze.frozen_at:
                raise SystemExit(f"scenario freeze/reveal ordering is invalid at C{checkpoint}")
            observed[checkpoint] = (path, basis.case_id)
    if set(observed) != set(_FORMAL_CASE_BY_CHECKPOINT):
        missing = sorted(set(_FORMAL_CASE_BY_CHECKPOINT) - set(observed))
        raise SystemExit(f"formal scenario lifecycle is missing checkpoints: {missing}")


def main() -> int:
    args = parser().parse_args()
    bundle = HumanBenchmarkCompiler().compile(args.source)
    expected = {case.case_id for case in bundle.case_manifests}
    found: dict[StableId, PairedPilotCaseResult] = {}
    stage2m_found: dict[tuple[StableId, str], MemoryBenchmarkCaseArmReport] = {}
    for path in args.output_directory.rglob("e2e_paired_report.json"):
        report = Stage2PairedPilotReport.model_validate_json(path.read_text("utf-8"))
        for case in report.cases:
            if case.case_id not in expected:
                continue
            existing = found.get(case.case_id)
            if existing is not None and existing != case:
                raise SystemExit(f"conflicting audited results for {case.case_id.root}")
            found[case.case_id] = case
    for path in args.output_directory.rglob("stage2m_case_C*_*.json"):
        stage2m_case = _load_stage2m_case(path)
        if stage2m_case.case_id not in expected:
            continue
        key = (stage2m_case.case_id, stage2m_case.arm)
        existing_stage2m_file = stage2m_found.get(key)
        if existing_stage2m_file is not None and existing_stage2m_file != stage2m_case:
            raise SystemExit(
                "conflicting Stage 2M audited results for "
                f"{stage2m_case.case_id.root}/{stage2m_case.arm}"
            )
        stage2m_found[key] = stage2m_case
    object_root_set = {
        path for path in args.output_directory.rglob("objects/sha256") if path.is_dir()
    }
    for manifest_path in args.output_directory.rglob("experiment_manifest.json"):
        manifest = json.loads(manifest_path.read_text("utf-8"))
        project_directory = manifest.get("project_directory")
        if isinstance(project_directory, str):
            candidate = Path(project_directory) / "objects" / "sha256"
            if candidate.is_dir():
                object_root_set.add(candidate)
    object_roots = tuple(sorted(object_root_set))
    for path in (artifact for object_root in object_roots for artifact in object_root.glob("*/*")):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or "case_id" not in payload or "pair_id" not in payload:
            if (
                isinstance(payload, dict)
                and "case_id" in payload
                and "arm" in payload
                and "evaluation" in payload
            ):
                try:
                    stage2m_case = MemoryBenchmarkCaseArmReport.model_validate(payload)
                except ValueError:
                    continue
                if stage2m_case.case_id not in expected:
                    continue
                key = (stage2m_case.case_id, stage2m_case.arm)
                existing_stage2m = stage2m_found.get(key)
                if existing_stage2m is not None and existing_stage2m != stage2m_case:
                    raise SystemExit(
                        "conflicting Stage 2M audited results for "
                        f"{stage2m_case.case_id.root}/{stage2m_case.arm}"
                    )
                stage2m_found[key] = stage2m_case
            continue
        try:
            case = PairedPilotCaseResult.model_validate_json(path.read_text("utf-8"))
        except ValueError:
            continue
        if case.case_id not in expected:
            continue
        existing = found.get(case.case_id)
        if existing is not None and existing != case:
            raise SystemExit(f"conflicting audited results for {case.case_id.root}")
        found[case.case_id] = case
    missing = sorted((item.root for item in expected - set(found)), key=str)
    if missing:
        raise SystemExit(f"missing audited checkpoint results: {missing}")
    cases = tuple(sorted(found.values(), key=lambda item: item.checkpoint_chapter))
    profile = BenchmarkInformationProfile(args.information_profile)
    report = TeacherForcedBenchmarkE2ERunner._paired_report(bundle, profile, cases)
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for case in cases:
        path = args.output_directory / f"paired_case_C{case.checkpoint_chapter}.json"
        path.write_text(case.model_dump_json(indent=2) + "\n", encoding="utf-8")
    aggregate = args.output_directory / "e2e_paired_report_all_checkpoints.json"
    aggregate.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if stage2m_found:
        missing_stage2m = sorted(
            (case_id.root for case_id in expected if (case_id, "A") not in stage2m_found),
            key=str,
        )
        if missing_stage2m:
            raise SystemExit(f"missing Stage 2M checkpoint results: {missing_stage2m}")
        stage2m_cases = tuple(stage2m_found.values())

        def read_artifact(ref: ArtifactRef) -> bytes:
            digest = ref.artifact_id.root.removeprefix("sha256:")
            for object_root in object_roots:
                candidate = object_root / digest[:2] / digest
                if candidate.is_file():
                    return bytes(candidate.read_bytes())
            raise ValueError(f"missing evaluator artifact: {ref.artifact_id.root}")

        reporter = MemoryBenchmarkReporter(artifact_reader=read_artifact)
        arm_a_cases = tuple(item for item in stage2m_cases if item.arm == "A")
        _validate_formal_run_lifecycle(
            args.output_directory,
            profile=profile,
            cases=arm_a_cases,
        )
        formal_report = reporter.aggregate(profile=profile, cases=arm_a_cases)
        for stage2m_case in stage2m_cases:
            path = (
                args.output_directory
                / f"stage2m_case_C{stage2m_case.checkpoint_chapter}_{stage2m_case.arm}.json"
            )
            path.write_text(stage2m_case.model_dump_json(indent=2) + "\n", encoding="utf-8")
        stage2m_output = args.output_directory / "unified_report_A.json"
        stage2m_output.write_text(
            formal_report.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        (args.output_directory / "unified_report.json").write_text(
            formal_report.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "cases": [item.case_id.root for item in cases],
                "paired_results_count": report.paired_results_count,
                "comparable_results_count": report.comparable_results_count,
                "future_leakage_count": report.future_leakage_count,
                "output": str(aggregate),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
