#!/usr/bin/env python3
"""Append a Stage 2 paired Pilot report to an Evaluation Ledger and export Parquet."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from novel_agent.adapters.postgres.database import Base, build_engine, build_session_factory
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import SchemaVersion
from novel_agent.domain.stage2 import Stage2PairedPilotReport
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.evaluation import EvaluationHarness, EvaluationLedgerRepository
from novel_agent.services.stage2_evaluation import Stage2PairedEvaluationBuilder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--ledger-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    raw = args.report.read_bytes()
    report = Stage2PairedPilotReport.model_validate_json(raw, strict=True)
    report_artifact = ArtifactRef(
        artifact_id=sha256_id(raw),
        media_type="application/vnd.novel-agent.stage2-paired-pilot+json",
        byte_length=len(raw),
        schema_version=SchemaVersion("2.0.0"),
    )
    args.ledger_db.parent.mkdir(parents=True, exist_ok=True)
    engine = build_engine(f"sqlite:///{args.ledger_db.resolve()}")
    Base.metadata.create_all(engine)
    ledger = EvaluationLedgerRepository(build_session_factory(engine))
    config, entries = Stage2PairedEvaluationBuilder().build(
        report,
        report_artifact,
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
        f"OK: entries={len(entries)} config={report.configuration_fingerprint.root} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
