"""Append-only Evaluation Ledger and reproducible Parquet export."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import EvaluationEntryRow
from novel_agent.domain.evaluation import BenchmarkRunConfig
from novel_agent.domain.runtime import EvaluationEntry


class EvaluationConflictError(RuntimeError):
    pass


class EvaluationConfigError(RuntimeError):
    pass


def config_fingerprint(config: BenchmarkRunConfig) -> str:
    value = config.model_dump(mode="json")
    value["parameters"] = sorted(value["parameters"], key=lambda parameter: parameter["name"])
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class EvaluationLedgerRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def append(self, config: BenchmarkRunConfig, entry: EvaluationEntry) -> EvaluationEntry:
        if config.run_id != entry.run_id:
            raise EvaluationConfigError("entry run_id does not match benchmark configuration")
        if entry.model_role is not None and (
            not config.model_required or entry.model_role is not config.model_role
        ):
            raise EvaluationConfigError(
                "entry model role does not match the model-required benchmark configuration"
            )
        fingerprint = config_fingerprint(config)
        with self._session_factory() as session, session.begin():
            existing = session.get(EvaluationEntryRow, entry.evaluation_id.root)
            if existing is not None:
                restored = self._from_row(existing)
                if restored != entry or existing.config_fingerprint != fingerprint:
                    raise EvaluationConflictError("evaluation_id refers to another entry")
                return restored
            session.add(
                EvaluationEntryRow(
                    evaluation_id=entry.evaluation_id.root,
                    run_id=entry.run_id.root,
                    config_fingerprint=fingerprint,
                    entry_json=entry.model_dump(mode="json"),
                    created_at=entry.created_at,
                )
            )
            return entry

    def list_run(self, config: BenchmarkRunConfig) -> tuple[EvaluationEntry, ...]:
        fingerprint = config_fingerprint(config)
        with self._session_factory() as session:
            rows = session.scalars(
                select(EvaluationEntryRow)
                .where(
                    EvaluationEntryRow.run_id == config.run_id.root,
                    EvaluationEntryRow.config_fingerprint == fingerprint,
                )
                .order_by(EvaluationEntryRow.evaluation_id)
            )
            return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _from_row(row: EvaluationEntryRow) -> EvaluationEntry:
        return EvaluationEntry.model_validate_json(json.dumps(row.entry_json))


class EvaluationHarness:
    def __init__(self, ledger: EvaluationLedgerRepository) -> None:
        self._ledger = ledger

    def record_and_export(
        self,
        config: BenchmarkRunConfig,
        entries: tuple[EvaluationEntry, ...],
        output_path: Path,
    ) -> Path:
        for entry in entries:
            self._ledger.append(config, entry)
        persisted = self._ledger.list_run(config)
        self._export_parquet(config, persisted, output_path)
        return output_path

    @staticmethod
    def _export_parquet(
        config: BenchmarkRunConfig,
        entries: tuple[EvaluationEntry, ...],
        output_path: Path,
    ) -> None:
        fingerprint = config_fingerprint(config)
        rows = []
        for entry in entries:
            rows.append(
                {
                    "evaluation_id": entry.evaluation_id.root,
                    "run_id": entry.run_id.root,
                    "candidate_id": entry.candidate_id.root if entry.candidate_id else None,
                    "commit_id": entry.commit_id.root if entry.commit_id else None,
                    "evaluator": entry.evaluator,
                    "evaluator_version": entry.evaluator_version,
                    "model_role": entry.model_role.value if entry.model_role else None,
                    "model_endpoint": entry.model_endpoint,
                    "model_version": entry.model_version,
                    "model_cost_usd": (
                        str(entry.model_cost_usd) if entry.model_cost_usd is not None else None
                    ),
                    "model_latency_ms": entry.model_latency_ms,
                    "rubric_version": entry.rubric_version,
                    "decision": entry.decision.value,
                    "metrics_json": json.dumps(
                        [metric.model_dump(mode="json") for metric in entry.metrics],
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "failure_codes_json": json.dumps(entry.failure_codes, ensure_ascii=False),
                    "entry_json": entry.model_dump_json(),
                    "config_fingerprint": fingerprint,
                }
            )
        table = pa.Table.from_pylist(rows)
        metadata = dict(table.schema.metadata or {})
        metadata[b"novel_agent.config"] = config.model_dump_json().encode("utf-8")
        metadata[b"novel_agent.config_fingerprint"] = fingerprint.encode("ascii")
        table = table.replace_schema_metadata(metadata)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, output_path, compression="zstd", version="2.6")
