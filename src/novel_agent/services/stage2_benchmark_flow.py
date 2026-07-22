"""One-shot, auditable Stage 2 read-Pilot preparation and evaluation flow."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_engine, build_session_factory
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import BenchmarkBundle
from novel_agent.domain.ids import SchemaVersion
from novel_agent.domain.stage2 import BenchmarkInformationProfile, Stage2PairedPilotReport
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.benchmark_importer import BenchmarkBundleImporter
from novel_agent.services.benchmark_scenario_compiler import BenchmarkScenarioCompiler
from novel_agent.services.evaluation import EvaluationHarness, EvaluationLedgerRepository
from novel_agent.services.stage2_evaluation import Stage2PairedEvaluationBuilder
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner


class Stage2BenchmarkFlowError(RuntimeError):
    """The requested output would overwrite another immutable benchmark run."""


class Stage2BenchmarkFlowRunner:
    """Persist all currently executable read-Pilot evidence without claiming write-side success."""

    schema_version = SchemaVersion("2.0.0")

    def __init__(self, *, token_budget: int = 4000, max_candidates: int = 20) -> None:
        self._paired = Stage2PairedPilotRunner(
            token_budget=token_budget,
            max_candidates=max_candidates,
        )

    def run(
        self,
        bundle: BenchmarkBundle,
        output_directory: Path,
        *,
        gate_bundle: BenchmarkBundle | None = None,
    ) -> dict[str, Any]:
        importer = BenchmarkBundleImporter()
        importer.validate(bundle)
        if gate_bundle is not None:
            importer.validate(gate_bundle)
        output_directory.mkdir(parents=True, exist_ok=True)
        artifacts = ArtifactRepository(FilesystemObjectStore(output_directory / "objects"))

        canonical_bytes = self._json_bytes(bundle)
        self._write_immutable(output_directory / "canonical.bundle.json", canonical_bytes)
        persisted: dict[str, ArtifactRef] = {
            "canonical_bundle": artifacts.put(
                canonical_bytes,
                "application/vnd.novel-agent.benchmark-bundle+json",
                self.schema_version,
            )
        }
        if gate_bundle is not None:
            gate_bytes = self._json_bytes(gate_bundle)
            self._write_immutable(output_directory / "canonical.gate.bundle.json", gate_bytes)
            persisted["gate_bundle"] = artifacts.put(
                gate_bytes,
                "application/vnd.novel-agent.benchmark-bundle+json",
                self.schema_version,
            )

        scenario_compiler = BenchmarkScenarioCompiler()
        scenario_directory = output_directory / "scenarios"
        scenario_directory.mkdir(parents=True, exist_ok=True)
        for profile in BenchmarkInformationProfile:
            scenario = scenario_compiler.compile(bundle, profile)
            payload = self._json_bytes(scenario)
            name = f"scenario.{profile.value}"
            self._write_immutable(scenario_directory / f"{name}.json", payload)
            persisted[name] = artifacts.put(
                payload,
                "application/vnd.novel-agent.benchmark-scenario+json",
                self.schema_version,
            )
        rebuild = scenario_compiler.independent_rebuild_report(bundle)
        rebuild_bytes = self._json_bytes(rebuild)
        self._write_immutable(
            scenario_directory / "independent_rebuild.json",
            rebuild_bytes,
        )
        persisted["independent_rebuild"] = artifacts.put(
            rebuild_bytes,
            "application/vnd.novel-agent.independent-rebuild-report+json",
            self.schema_version,
        )

        paired = self._paired.run(bundle)
        paired_bytes = self._json_bytes(paired)
        paired_path = output_directory / "paired_controller_report.json"
        self._write_immutable(paired_path, paired_bytes)
        paired_artifact = artifacts.put(
            paired_bytes,
            "application/vnd.novel-agent.stage2-paired-pilot+json",
            self.schema_version,
        )
        persisted["paired_controller_report"] = paired_artifact

        evaluation_directory = output_directory / "evaluation"
        evaluation_directory.mkdir(parents=True, exist_ok=True)
        ledger_path = evaluation_directory / "ledger.sqlite3"
        parquet_path = evaluation_directory / "paired_controller.parquet"
        self._persist_evaluation(paired, paired_artifact, ledger_path, parquet_path)

        checkpoints = tuple(sorted({case.history_range[1] for case in bundle.case_manifests}))
        blockers = [
            "planner_and_curator_genesis_not_executed",
            "continuous_curator_replay_not_executed",
            "canonical_checkpoint_commit_chain_not_persisted",
        ]
        if not bundle.replay_manifests:
            blockers.append("replay_gold_manifest_unavailable")
        summary: dict[str, Any] = {
            "status": "read_pilot_completed",
            "bundle_id": bundle.bundle_id.root,
            "bundle_hash": bundle.content_hash.root,
            "checkpoint_chapters": checkpoints,
            "information_profiles": tuple(profile.value for profile in BenchmarkInformationProfile),
            "paired_results_count": paired.paired_results_count,
            "comparable_results_count": paired.comparable_results_count,
            "future_leakage_count": paired.future_leakage_count,
            "source_compiled": True,
            "scenarios_compiled": True,
            "bounded_controller_executed": True,
            "evaluation_ledger_written": True,
            "artifact_store_written": True,
            "planner_bootstrap_executed": False,
            "curator_continuous_replay_executed": False,
            "project_commit_database_written": False,
            "stage2_gate_ready": False,
            "blockers": tuple(blockers),
            "artifact_refs": {
                name: artifact.model_dump(mode="json")
                for name, artifact in sorted(persisted.items())
            },
            "evaluation_ledger": str(ledger_path),
            "evaluation_parquet": str(parquet_path),
        }
        summary_bytes = (
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        self._write_immutable(output_directory / "flow_summary.json", summary_bytes)
        return summary

    def _persist_evaluation(
        self,
        report: Stage2PairedPilotReport,
        report_artifact: ArtifactRef,
        ledger_path: Path,
        parquet_path: Path,
    ) -> None:
        engine = build_engine(f"sqlite:///{ledger_path.resolve()}")
        try:
            Base.metadata.create_all(engine)
            ledger = EvaluationLedgerRepository(build_session_factory(engine))
            config, entries = Stage2PairedEvaluationBuilder().build(
                report,
                report_artifact,
                created_at=datetime.now(UTC),
            )
            existing = {item.evaluation_id: item for item in ledger.list_run(config)}
            idempotent_entries = tuple(
                prior
                if (prior := existing.get(entry.evaluation_id)) is not None
                and prior.model_copy(update={"created_at": entry.created_at}) == entry
                else entry
                for entry in entries
            )
            EvaluationHarness(ledger).record_and_export(
                config,
                idempotent_entries,
                parquet_path,
            )
        finally:
            engine.dispose()

    @staticmethod
    def _json_bytes(model: Any) -> bytes:
        return (str(model.model_dump_json(indent=2)) + "\n").encode("utf-8")

    @staticmethod
    def _write_immutable(path: Path, payload: bytes) -> None:
        if path.exists():
            if path.read_bytes() != payload:
                raise Stage2BenchmarkFlowError(
                    f"refusing to overwrite different benchmark evidence: {path}"
                )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
