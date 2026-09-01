#!/usr/bin/env python3
"""Run the U6-A public Writer readout and C/D seed canary campaign.

The basis and readout plan are immutable inputs.  This runner creates one new
output/object namespace, binds the existing production assembly, executes one
checkpoint at a time, discards only the evaluation namespace, and finalizes
the frozen basis report only after every planned item completes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from run_u4s_readout_campaign import _build_production_binding

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ProjectId, SchemaVersion
from novel_agent.domain.production_assembly import ResolvedProductionAssemblyAttestation
from novel_agent.domain.u6_continuous_replay import (
    U6A_READOUT_PLAN_MEDIA_TYPE,
    U6AReadoutPlan,
    U6CheckpointBasisManifest,
    U6ContinuousReplayReport,
)
from novel_agent.domain.v05_readout import V05ReadoutCampaignManifest
from novel_agent.runtime.creative_assembly import DEFAULT_PRODUCTION_ASSEMBLY_FACTORY
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.u4s_seed_readout import U4SPublicCorpus
from novel_agent.services.u6a_production_readout import (
    U6AProductionReadoutAdapter,
    U6AProductionReadoutConfig,
)
from novel_agent.services.u6a_readout_executor import (
    U6AReadoutExecutor,
    finalize_u6a_basis_report,
)

SCHEMA_VERSION = SchemaVersion("1.0.0")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basis-manifest", type=Path, required=True)
    parser.add_argument("--basis-report", type=Path, required=True)
    parser.add_argument("--readout-plan", type=Path, required=True)
    parser.add_argument("--source-readout-manifest", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--model-base-url")
    parser.add_argument("--model")
    parser.add_argument("--assembly-factory", default=DEFAULT_PRODUCTION_ASSEMBLY_FACTORY)
    return parser


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def _load_facts(path: Path) -> dict[str, Any]:
    facts = _load_json(path)
    if facts.get("schema") != "u4s-campaign-facts.v1":
        raise ValueError("facts artifact is not u4s-campaign-facts.v1")
    return facts


def _plan_ref(plan_path: Path, plan: U6AReadoutPlan) -> ArtifactRef:
    repository = ArtifactRepository(FilesystemObjectStore(plan_path.parent / "objects"))
    payload = canonical_json_bytes(plan.model_dump(mode="json", by_alias=True))
    ref = repository.put(payload, U6A_READOUT_PLAN_MEDIA_TYPE, SCHEMA_VERSION)
    return ref


def main() -> int:
    args = _parser().parse_args()
    output_root = args.output_root.resolve()
    if output_root.exists():
        raise SystemExit(f"refusing to overwrite U6-A output identity: {output_root}")
    if not args.bundle_root.is_dir():
        raise SystemExit(f"benchmark bundle is missing: {args.bundle_root}")

    basis_manifest = U6CheckpointBasisManifest.model_validate_json(
        args.basis_manifest.read_bytes(), strict=True
    )
    basis_report = U6ContinuousReplayReport.model_validate_json(
        args.basis_report.read_bytes(), strict=True
    )
    plan = U6AReadoutPlan.model_validate_json(args.readout_plan.read_bytes(), strict=True)
    source_manifest = V05ReadoutCampaignManifest.model_validate_json(
        args.source_readout_manifest.read_bytes(), strict=True
    )
    facts = _load_facts(args.facts)
    attestation = ResolvedProductionAssemblyAttestation.model_validate_json(
        Path(str(facts["attestation"])).read_bytes(), strict=True
    )
    run_id = basis_report.run_id
    if facts.get("ledger_request_count") != 0:
        raise SystemExit("U6-A production facts are not zero-call facts")
    # U4-S facts identify the frozen production assembly and its canonical
    # source roots.  The U6-A basis has its own continuous-replay run
    # identity, which is the identity that must own the readout ledger.
    if plan.basis_manifest_ref != basis_report.basis_manifest_ref:
        raise SystemExit("U6-A plan is not bound to the frozen basis manifest")
    if plan.qa_task_count + plan.context_task_count != len(source_manifest.readout_manifest.tasks):
        raise SystemExit("U6-A plan public task count differs from the source readout manifest")

    base_url = args.model_base_url or str(facts["model_base_url"])
    model = args.model or str(facts["model"])
    assembly, artifacts, gateway = _build_production_binding(
        database_url=args.database_url,
        output_root=output_root,
        run_id=run_id,
        facts=facts,
        attestation=attestation,
        manifest=source_manifest,
        base_url=base_url,
        model=model,
        assembly_factory=args.assembly_factory,
    )
    del assembly
    basis_artifacts = ArtifactRepository(
        FilesystemObjectStore(args.basis_manifest.resolve().parent / "objects")
    )
    adapter = U6AProductionReadoutAdapter(
        U6AProductionReadoutConfig(
            manifest=source_manifest,
            corpus=U4SPublicCorpus(args.bundle_root.resolve()),
            basis_artifacts=basis_artifacts,
            artifacts=artifacts,
            gateway=gateway,
            bundle_root=args.bundle_root.resolve(),
            project_id=ProjectId(str(facts["project_id"])),
            run_id=run_id,
        )
    )
    execution = asyncio.run(
        U6AReadoutExecutor(
            plan=plan,
            plan_ref=_plan_ref(args.readout_plan.resolve(), plan),
            basis_manifest=basis_manifest,
            basis_report=basis_report,
            adapter=adapter,
            artifacts=artifacts,
            run_id=run_id,
        ).run()
    )
    report_path = output_root / "u6a_readout_run_report.json"
    report_path.write_bytes(
        canonical_json_bytes(execution.report.model_dump(mode="json", by_alias=True)) + b"\n"
    )
    final_path: Path | None = None
    if execution.report.status == "COMPLETED":
        finalized = finalize_u6a_basis_report(basis_report, execution)
        final_path = output_root / "u6_continuous_replay_report.json"
        final_path.write_bytes(
            canonical_json_bytes(finalized.model_dump(mode="json", by_alias=True)) + b"\n"
        )
    print(
        json.dumps(
            {
                "status": execution.report.status,
                "report": str(report_path),
                "report_ref": execution.report_ref.artifact_id.root,
                "completed_item_count": execution.report.completed_item_count,
                "expected_item_count": execution.report.expected_item_count,
                "completed_checkpoint_count": execution.report.completed_checkpoint_count,
                "expected_checkpoint_count": execution.report.expected_checkpoint_count,
                "final_basis_report": None if final_path is None else str(final_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if execution.report.status == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
