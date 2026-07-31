#!/usr/bin/env python3
"""Append a deterministic Stage 2 retrieval gate to the Evaluation Ledger."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from novel_agent.adapters.postgres.database import Base, build_engine, build_session_factory
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.gates import Stage2RetrievalGateReport
from novel_agent.domain.ids import ArtifactId, SchemaVersion
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.evaluation import EvaluationHarness, EvaluationLedgerRepository
from novel_agent.services.stage2_retrieval_gate_evaluation import (
    Stage2RetrievalGateEvaluationBuilder,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--experiment-manifest", type=Path, required=True)
    parser.add_argument("--ledger-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    raw = args.report.read_bytes()
    report = Stage2RetrievalGateReport.model_validate_json(raw, strict=True)
    dataset_hash, code_version = _experiment_identity(args.experiment_manifest)
    report_artifact = ArtifactRef(
        artifact_id=sha256_id(raw),
        media_type="application/vnd.novel-agent.stage2-retrieval-gate+json",
        byte_length=len(raw),
        schema_version=SchemaVersion("2.0.0"),
    )
    args.ledger_db.parent.mkdir(parents=True, exist_ok=True)
    engine = build_engine(f"sqlite:///{args.ledger_db.resolve()}")
    Base.metadata.create_all(engine)
    ledger = EvaluationLedgerRepository(build_session_factory(engine))
    config, entries = Stage2RetrievalGateEvaluationBuilder().build(
        report,
        report_artifact,
        dataset_hash=dataset_hash,
        code_version=code_version,
        created_at=datetime.now(UTC),
    )
    existing = {item.evaluation_id: item for item in ledger.list_run(config)}
    entries = tuple(
        prior
        if (prior := existing.get(entry.evaluation_id)) is not None
        and prior.model_copy(update={"created_at": entry.created_at}) == entry
        else entry
        for entry in entries
    )
    EvaluationHarness(ledger).record_and_export(config, entries, args.output)
    print(
        f"OK: entries={len(entries)} final_commit={report.checkpoints[-1].source_commit.root} "
        f"output={args.output}"
    )
    return 0


def _experiment_identity(path: Path) -> tuple[ArtifactId, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("experiment manifest must be a JSON object")
    raw_hash = payload.get("benchmark_content_hash")
    code_version = payload.get("code_commit")
    if not isinstance(raw_hash, str):
        raise ValueError("experiment manifest lacks benchmark_content_hash")
    if not isinstance(code_version, str) or not code_version:
        raise ValueError("experiment manifest lacks code_commit")
    return ArtifactId(raw_hash), code_version


if __name__ == "__main__":
    raise SystemExit(main())
