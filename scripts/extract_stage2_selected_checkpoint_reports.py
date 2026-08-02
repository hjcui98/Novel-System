#!/usr/bin/env python3
"""Extract selected Stage 2M checkpoint reports from existing diagnostic outputs.

This command only reads existing case reports and content-addressed evaluator
artifacts. It does not invoke the model endpoint or rerun any benchmark chapter.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    MemoryBenchmarkCaseArmReport,
)
from novel_agent.services.memory_benchmark_reporting import MemoryBenchmarkReporter

_CASE_BY_CHECKPOINT = {
    20: "ZTJ-P001",
    40: "ZTJ-P002",
    60: "ZTJ-P003",
    80: "ZTJ-P004",
    95: "ZTJ-P005",
}
_MANIFEST_IDENTITY_FIELDS = (
    "code_commit",
    "code_source_fingerprint",
    "code_version",
    "run_config_hash",
    "benchmark_contract_hash",
    "matcher_version",
    "writer_token_budget",
    "evidence_ledger_token_budget",
)
_CASE_IDENTITY_FIELDS = (
    "code_version",
    "run_config_hash",
    "benchmark_contract_hash",
    "matcher_version",
    "writer_token_budget",
    "evidence_ledger_token_budget",
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--input-directory",
        type=Path,
        action="append",
        required=True,
        help="Existing P2 output directory; may be supplied more than once.",
    )
    value.add_argument("--output-directory", type=Path, required=True)
    value.add_argument(
        "--checkpoints",
        default="40,60,80,95",
        help="Comma-separated declared checkpoint chapters to select.",
    )
    value.add_argument("--arm", default="A")
    value.add_argument(
        "--information-profile",
        choices=tuple(item.value for item in BenchmarkInformationProfile),
    )
    return value


def _parse_checkpoints(raw: str) -> tuple[int, ...]:
    try:
        values = tuple(sorted({int(item.strip()) for item in raw.split(",") if item.strip()}))
    except ValueError as exc:
        raise ValueError(f"invalid checkpoint list: {raw!r}") from exc
    if not values:
        raise ValueError("at least one checkpoint is required")
    unknown = sorted(set(values) - set(_CASE_BY_CHECKPOINT))
    if unknown:
        raise ValueError(f"checkpoints are not declared Stage 2M points: {unknown}")
    return values


def _load_case(path: Path) -> MemoryBenchmarkCaseArmReport:
    try:
        return MemoryBenchmarkCaseArmReport.model_validate_json(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid Stage 2M case artifact {path}: {exc}") from exc


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON object {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _load_cases_from_directory(
    directory: Path,
    *,
    arm: str,
) -> tuple[tuple[MemoryBenchmarkCaseArmReport, Path], ...]:
    paths = tuple(sorted(directory.glob(f"stage2m_case_C*_{arm}.json")))
    if paths:
        return tuple((_load_case(path), path) for path in paths)
    diagnostic_path = directory / f"diagnostic_partial_report_{arm}.json"
    if not diagnostic_path.is_file():
        raise ValueError(
            f"{directory} has neither stage2m_case_C*_{{arm}}.json nor {diagnostic_path.name}"
        )
    payload = _load_json_object(diagnostic_path)
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError(f"diagnostic report has no cases list: {diagnostic_path}")
    cases: list[tuple[MemoryBenchmarkCaseArmReport, Path]] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict):
            raise ValueError(f"diagnostic case {index} is not an object: {diagnostic_path}")
        try:
            case = MemoryBenchmarkCaseArmReport.model_validate(raw_case)
        except ValueError as exc:
            raise ValueError(f"invalid diagnostic case {index}: {diagnostic_path}") from exc
        cases.append((case, diagnostic_path))
    return tuple(cases)


def _manifest_and_object_roots(directory: Path) -> tuple[dict[str, object], tuple[Path, ...]]:
    manifest_path = directory / "experiment_manifest.json"
    manifest = _load_json_object(manifest_path)
    if manifest.get("code_source_dirty") is not False:
        raise ValueError(f"source manifest is not clean: {manifest_path}")
    roots: list[Path] = []
    local_root = directory / "objects" / "sha256"
    if local_root.is_dir():
        roots.append(local_root)
    project_directory = manifest.get("project_directory")
    if isinstance(project_directory, str):
        project_root = Path(project_directory) / "objects" / "sha256"
        if project_root.is_dir():
            roots.append(project_root)
    if not roots:
        raise ValueError(f"no object store found for {directory}")
    return manifest, tuple(dict.fromkeys(roots))


def _manifest_identity(manifest: dict[str, object], directory: Path) -> tuple[object, ...]:
    values = tuple(manifest.get(field) for field in _MANIFEST_IDENTITY_FIELDS)
    if any(value is None for value in values):
        raise ValueError(f"source manifest is missing immutable identity fields: {directory}")
    return values


def _root_value(value: object) -> object:
    return getattr(value, "root", value)


def _case_identity(case: MemoryBenchmarkCaseArmReport) -> tuple[object, ...]:
    return tuple(_root_value(getattr(case, field)) for field in _CASE_IDENTITY_FIELDS)


def _collect_cases(
    directories: tuple[Path, ...],
    *,
    checkpoints: tuple[int, ...],
    arm: str,
) -> tuple[
    tuple[MemoryBenchmarkCaseArmReport, ...],
    tuple[dict[str, object], ...],
    tuple[Path, ...],
    tuple[tuple[MemoryBenchmarkCaseArmReport, Path, Path], ...],
    tuple[tuple[object, ...], ...],
]:
    selected_by_checkpoint: dict[int, tuple[MemoryBenchmarkCaseArmReport, Path, Path]] = {}
    manifests: list[dict[str, object]] = []
    object_roots: list[Path] = []
    manifest_identities: list[tuple[object, ...]] = []
    for directory in directories:
        manifest, roots = _manifest_and_object_roots(directory)
        manifest_identities.append(_manifest_identity(manifest, directory))
        manifests.append(
            {
                "input_directory": str(directory),
                "experiment_id": manifest.get("experiment_id"),
                "profile": manifest.get("information_profile"),
                "code_commit": manifest.get("code_commit"),
                "run_config_hash": manifest.get("run_config_hash"),
            }
        )
        object_roots.extend(roots)
        for case, source_path in _load_cases_from_directory(directory, arm=arm):
            checkpoint = case.checkpoint_chapter
            if checkpoint not in checkpoints:
                continue
            expected_case = _CASE_BY_CHECKPOINT[checkpoint]
            if case.case_id.root != expected_case:
                raise ValueError(
                    f"checkpoint C{checkpoint} has {case.case_id.root}; expected {expected_case}"
                )
            if case.arm != arm:
                raise ValueError(f"case {case.case_id.root} has arm {case.arm}, expected {arm}")
            if _case_identity(case) != manifest_identities[-1][2:]:
                raise ValueError(
                    f"case identity disagrees with source manifest at C{checkpoint}: {directory}"
                )
            existing = selected_by_checkpoint.get(checkpoint)
            candidate = (case, source_path, directory)
            if existing is not None and existing[0] != case:
                raise ValueError(f"conflicting selected cases at checkpoint C{checkpoint}")
            selected_by_checkpoint[checkpoint] = candidate
    missing = sorted(set(checkpoints) - set(selected_by_checkpoint))
    if missing:
        raise ValueError(f"missing selected checkpoint cases: {missing}")
    ordered = tuple(selected_by_checkpoint[item] for item in checkpoints)
    return (
        tuple(item[0] for item in ordered),
        tuple(manifests),
        tuple(dict.fromkeys(object_roots)),
        ordered,
        tuple(manifest_identities),
    )


def _reader(object_roots: tuple[Path, ...]):
    def read_artifact(ref: ArtifactRef) -> bytes:
        digest = ref.artifact_id.root.removeprefix("sha256:")
        for object_root in object_roots:
            candidate = object_root / digest[:2] / digest
            if candidate.is_file():
                return candidate.read_bytes()
        raise ValueError(f"missing evaluator artifact: {ref.artifact_id.root}")

    return read_artifact


def _write_report(
    output_directory: Path,
    *,
    report,
    cases: tuple[MemoryBenchmarkCaseArmReport, ...],
    selected_sources: tuple[tuple[MemoryBenchmarkCaseArmReport, Path, Path], ...],
    manifests: tuple[dict[str, object], ...],
    manifest_identities: tuple[tuple[object, ...], ...],
    profile: BenchmarkInformationProfile,
    arm: str,
    checkpoints: tuple[int, ...],
) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    for case in cases:
        path = output_directory / f"stage2m_case_C{case.checkpoint_chapter}_{arm}.json"
        path.write_text(case.model_dump_json(indent=2) + "\n", encoding="utf-8")
    report_path = output_directory / f"selected_checkpoint_report_{arm}.json"
    report_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    identities = {_case_identity(case) for case in cases}
    source_identity_consistent = len(set(manifest_identities)) == 1
    selection_manifest = {
        "report_version": "stage2m_selected_checkpoint.v1",
        "selection_mode": "existing_diagnostic_case_artifacts",
        "profile": profile.value,
        "arm": arm,
        "selected_checkpoints": list(checkpoints),
        "case_reports": [
            {
                "case_id": case.case_id.root,
                "checkpoint_chapter": case.checkpoint_chapter,
                "source_report": str(source_path),
                "source_directory": str(source_directory),
            }
            for case, source_path, source_directory in selected_sources
        ],
        "source_manifests": list(manifests),
        "source_identity_consistent": source_identity_consistent and len(identities) == 1,
        "formal_contract_validated": False,
        "gate_passed": False,
        "scenario_lifecycle_validation": "not_required_for_selected_checkpoint_report",
        "aggregate_report": report_path.name,
    }
    manifest_path = output_directory / "selected_checkpoint_manifest.json"
    manifest_path.write_text(
        json.dumps(selection_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parser().parse_args()
    checkpoints = _parse_checkpoints(args.checkpoints)
    directories = tuple(args.input_directory)
    cases, manifests, object_roots, selected_sources, manifest_identities = _collect_cases(
        directories,
        checkpoints=checkpoints,
        arm=args.arm,
    )
    observed_profiles = {case.evaluation.profile for case in cases}
    if len(observed_profiles) != 1:
        raise ValueError("selected cases contain multiple information profiles")
    profile = next(iter(observed_profiles))
    if args.information_profile is not None and profile.value != args.information_profile:
        raise ValueError(
            f"selected case profile is {profile.value}, not {args.information_profile}"
        )
    report = MemoryBenchmarkReporter(
        artifact_reader=_reader(object_roots),
        enforce_formal_contract=False,
    ).aggregate(profile=profile, cases=cases)
    if report.formal_contract_validated or report.gate_passed:
        raise ValueError("selected checkpoint extraction unexpectedly produced a formal PASS")
    _write_report(
        args.output_directory,
        report=report,
        cases=cases,
        selected_sources=selected_sources,
        manifests=manifests,
        manifest_identities=manifest_identities,
        profile=profile,
        arm=args.arm,
        checkpoints=checkpoints,
    )
    print(
        json.dumps(
            {
                "profile": profile.value,
                "arm": args.arm,
                "checkpoints": list(checkpoints),
                "output": str(args.output_directory),
                "formal_contract_validated": False,
                "gate_passed": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
