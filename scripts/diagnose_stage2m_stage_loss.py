#!/usr/bin/env python3
"""Recompute evaluator-only accepted-evidence stage loss from frozen WP7 artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from novel_agent.domain.memory import Stage1ContextPackage
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    EvidenceLedger,
    PerGoldStageLossDiagnostic,
)
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_diagnostics import StageLossDiagnosticBuilder


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--run-directory",
        type=Path,
        required=True,
        help="WP7 run containing checkpoints/Cxx directories",
    )
    return value


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text("utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"expected JSON object: {path}")
    return payload


def _artifact_path(project_directory: Path, artifact_id: str) -> Path:
    if not artifact_id.startswith("sha256:"):
        raise SystemExit(f"unsupported artifact identity: {artifact_id}")
    digest = artifact_id.removeprefix("sha256:")
    return project_directory / "objects" / "sha256" / digest[:2] / digest


def _read_verified_artifact(project_directory: Path, artifact: dict[str, Any]) -> bytes:
    artifact_id = artifact.get("artifact_id")
    if not isinstance(artifact_id, str):
        raise SystemExit("freeze artifact has no artifact_id")
    path = _artifact_path(project_directory, artifact_id)
    payload = path.read_bytes()
    expected_size = artifact.get("byte_length")
    if expected_size != len(payload):
        raise SystemExit(f"artifact byte length mismatch: {artifact_id}")
    actual = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if actual != artifact_id:
        raise SystemExit(f"artifact content hash mismatch: {artifact_id}")
    return payload


def _stage_summary(
    diagnostics: tuple[PerGoldStageLossDiagnostic, ...],
    field: str,
) -> dict[str, int]:
    coverages = [getattr(item, field) for item in diagnostics]
    return {
        "complete_gold_count": sum(bool(item.complete_alternative_ids) for item in coverages),
        "matched_reference_count": sum(item.matched_reference_count for item in coverages),
        "accepted_reference_count": sum(item.accepted_reference_count for item in coverages),
    }


def main() -> int:
    args = parser().parse_args()
    checkpoint_directories = sorted((args.run_directory / "checkpoints").glob("C*"))
    if not checkpoint_directories:
        raise SystemExit("run directory has no checkpoints/Cxx artifacts")

    first_manifest = _read_json(checkpoint_directories[0] / "experiment_manifest.json")
    source = Path(str(first_manifest["benchmark_source"]))
    profile = BenchmarkInformationProfile(str(first_manifest["information_profile"]))
    bundle = HumanBenchmarkCompiler().compile(source)
    cases = {case.case_id.root: case for case in bundle.case_manifests}
    builder = StageLossDiagnosticBuilder()
    checkpoint_results: list[dict[str, Any]] = []

    for checkpoint_directory in checkpoint_directories:
        manifest = _read_json(checkpoint_directory / "experiment_manifest.json")
        if manifest.get("benchmark_source") != str(source):
            raise SystemExit("checkpoint benchmark source drifted")
        if manifest.get("information_profile") != profile.value:
            raise SystemExit("checkpoint information profile drifted")
        scenario = _read_json(checkpoint_directory / "scenario_run.json")
        freezes = scenario.get("freezes")
        if not isinstance(freezes, list) or len(freezes) != 1:
            raise SystemExit(f"expected exactly one freeze: {checkpoint_directory}")
        freeze = freezes[0]
        if not isinstance(freeze, dict):
            raise SystemExit("freeze must be an object")
        case_id = freeze.get("case_id")
        if not isinstance(case_id, str) or case_id not in cases:
            raise SystemExit(f"unknown frozen case: {case_id}")
        project_directory = Path(str(manifest["project_directory"]))
        context_artifact = freeze.get("context_artifact")
        if not isinstance(context_artifact, dict):
            raise SystemExit("freeze has no context artifact")
        frozen = json.loads(
            _read_verified_artifact(project_directory, context_artifact).decode("utf-8")
        )
        deterministic = frozen.get("deterministic")
        if not isinstance(deterministic, dict):
            raise SystemExit("frozen artifact has no deterministic arm")
        stage1_context = Stage1ContextPackage.model_validate_json(
            json.dumps(deterministic.get("context"))
        )
        writer_ledger = EvidenceLedger.model_validate_json(
            json.dumps(deterministic.get("evidence_ledger"))
        )
        case = cases[case_id]
        gold_items = (
            *case.observed_use_gold,
            *case.operational_constraint_gold,
            *case.plan_obligation_gold,
        )
        diagnostics = builder.build(
            gold_items=gold_items,
            profile=profile,
            stage1_context=stage1_context,
            writer_ledger=writer_ledger,
        )
        checkpoint_results.append(
            {
                "checkpoint_chapter": freeze["checkpoint_chapter"],
                "case_id": case_id,
                "applicable_gold_count": len(diagnostics),
                "candidate": _stage_summary(diagnostics, "candidate"),
                "rank_selected": _stage_summary(diagnostics, "rank_selected"),
                "stage1_selected": _stage_summary(diagnostics, "stage1_selected"),
                "writer_ledger": _stage_summary(diagnostics, "writer_ledger"),
                "primary_failure_counts": dict(
                    sorted(Counter(item.primary_failure.value for item in diagnostics).items())
                ),
            }
        )

    totals: dict[str, Any] = {
        "applicable_gold_count": sum(
            int(item["applicable_gold_count"]) for item in checkpoint_results
        )
    }
    for field in ("candidate", "rank_selected", "stage1_selected", "writer_ledger"):
        totals[field] = {
            key: sum(int(item[field][key]) for item in checkpoint_results)
            for key in (
                "complete_gold_count",
                "matched_reference_count",
                "accepted_reference_count",
            )
        }
    failure_counts: Counter[str] = Counter()
    for item in checkpoint_results:
        failure_counts.update(item["primary_failure_counts"])
    totals["primary_failure_counts"] = dict(sorted(failure_counts.items()))
    output = {
        "diagnostic_version": builder.version,
        "profile": profile.value,
        "benchmark_source": str(source),
        "checkpoints": checkpoint_results,
        "totals": totals,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
