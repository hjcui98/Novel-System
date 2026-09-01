#!/usr/bin/env python3
"""Create a read-only U8-B5 R0 census for one frozen runtime run.

The census deliberately has no write path.  It reads the five runtime tables named by
the U8-B5 execution document and, when an object-store root is supplied, inspects only
identity-bearing JSON artifacts for bounded identity fields.  It must be run
against the original run database; a fresh empty database is not a valid substitute.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.models import (
    ModelCallLedgerRow,
    ProjectWriterClaimRow,
    RunEventRow,
    RuntimeEffectProjectionRow,
    RuntimeTaskAttemptRow,
    RuntimeTaskProjectionRow,
)

REQUIRED_TABLES: tuple[str, ...] = (
    "runtime_task_projection",
    "runtime_task_attempt",
    "run_event",
    "runtime_effect_projection",
    "model_call_ledger",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UNSETTLED_EFFECT_STATUSES = frozenset({"requested", "uncertain"})
_UNSETTLED_LEDGER_STATUSES = frozenset({"requested", "uncertain"})
_IDENTITY_MEDIA_TYPES = frozenset(
    {
        "application/vnd.novel-agent.request+json",
        "application/vnd.novel-agent.memory-repair-finding+json",
        "application/vnd.novel-agent.curator-proposal-attempt-receipt+json",
        "application/vnd.novel-agent.terminal-result+json",
        "application/vnd.novel-agent.memory-write-workflow-result+json",
    }
)


def database_descriptor(database_url: str) -> str:
    """Return a safe descriptor without username, password, or query parameters."""

    parsed = urlsplit(database_url)
    if parsed.hostname is None:
        return "unparseable"
    authority = parsed.hostname
    if parsed.port is not None:
        authority = f"{authority}:{parsed.port}"
    return f"{parsed.scheme}://{authority}{parsed.path}"


def _write_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"U8-B5 R0 census refuses to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _artifact_ref(value: object) -> dict[str, object] | None:
    item = _mapping(value)
    artifact_id = item.get("artifact_id")
    media_type = item.get("media_type")
    byte_length = item.get("byte_length")
    schema_version = item.get("schema_version")
    if not isinstance(artifact_id, str) or not isinstance(media_type, str):
        return None
    result: dict[str, object] = {"artifact_id": artifact_id, "media_type": media_type}
    if isinstance(byte_length, int):
        result["byte_length"] = byte_length
    if isinstance(schema_version, str):
        result["schema_version"] = schema_version
    return result


def _artifact_refs(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        return ()
    refs = tuple(ref for item in value if (ref := _artifact_ref(item)) is not None)
    unique: dict[str, dict[str, object]] = {}
    for ref in refs:
        unique.setdefault(json.dumps(ref, sort_keys=True), ref)
    return tuple(unique.values())


def _artifact_path(root: Path, artifact_id: str) -> Path | None:
    if not artifact_id.startswith("sha256:"):
        return None
    digest = artifact_id.removeprefix("sha256:")
    if _SHA256.fullmatch(digest) is None:
        return None
    return root / "sha256" / digest[:2] / digest


def _read_artifact_identity(
    root: Path | None, ref: Mapping[str, object]
) -> dict[str, object] | None:
    if root is None:
        return None
    artifact_id = ref.get("artifact_id")
    if not isinstance(artifact_id, str):
        return None
    path = _artifact_path(root, artifact_id)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    identity_fields = (
        "request_id",
        "workflow_request_id",
        "attempt_id",
        "status",
        "workflow_phase",
        "checkpoint_ref",
        "terminal_result_ref",
        "provider_call_count",
        "model_request_ids",
        "raw_response_refs",
        "finding_id",
        "planner_task_id",
        "task_id",
        "run_id",
        "project_id",
    )
    identity = {key: payload[key] for key in identity_fields if key in payload}
    if not identity:
        return None
    return identity


def _scan_object_store(
    root: Path | None, *, run_id: str, project_id: str
) -> tuple[dict[str, object], ...]:
    """Read identity-bearing artifacts from the supplied run object root only."""

    if root is None or not root.is_dir():
        return ()
    results: list[dict[str, object]] = []
    for metadata_path in sorted(root.glob("sha256/*/*.metadata.json")):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, Mapping):
            continue
        media_type = metadata.get("media_type")
        byte_length = metadata.get("byte_length")
        if not isinstance(media_type, str) or media_type not in _IDENTITY_MEDIA_TYPES:
            continue
        artifact_name = metadata_path.name.removesuffix(".metadata.json")
        ref: dict[str, object] = {
            "artifact_id": f"sha256:{artifact_name}",
            "media_type": media_type,
        }
        if isinstance(byte_length, int):
            ref["byte_length"] = byte_length
        artifact_path = metadata_path.with_name(artifact_name)
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        identity = _read_artifact_identity(root, ref)
        if identity is None:
            continue
        identity_text = json.dumps(identity, ensure_ascii=False, sort_keys=True)
        if run_id not in identity_text and project_id not in identity_text:
            continue
        results.append(
            {
                "artifact_ref": ref,
                "identity": identity,
                "source": "object_store_scan",
            }
        )
    return tuple(results)


def _has_identity_field(item: Mapping[str, object], field: str) -> bool:
    identity = item.get("identity")
    return isinstance(identity, Mapping) and field in identity


def _task_summary(row: RuntimeTaskProjectionRow) -> dict[str, object]:
    task = _mapping(row.task_json)
    input_refs = _artifact_refs(task.get("input_artifact_refs"))
    terminal_refs = _artifact_refs(task.get("terminal_artifact_refs"))
    finding_refs = tuple(
        ref
        for ref in input_refs
        if ref.get("media_type") == "application/vnd.novel-agent.memory-repair-finding+json"
    )
    return {
        "task_id": row.task_id,
        "run_id": row.run_id,
        "project_id": row.project_id,
        "kind": row.kind,
        "status": row.status,
        "revision": row.revision,
        "current_attempt_id": row.current_attempt_id,
        "basis_commit": row.basis_commit,
        "basis_snapshot": row.basis_snapshot,
        "input_artifact_refs": input_refs,
        "finding_artifact_refs": finding_refs,
        "terminal_artifact_refs": terminal_refs,
    }


def _attempt_summary(row: RuntimeTaskAttemptRow) -> dict[str, object]:
    attempt = _mapping(row.attempt_json)
    return {
        "attempt_id": row.attempt_id,
        "task_id": row.task_id,
        "attempt_no": row.attempt_no,
        "worker_id": row.worker_id,
        "fence_generation": row.fence_generation,
        "claimed_at": row.claimed_at.isoformat(),
        "heartbeat_at": row.heartbeat_at.isoformat(),
        "lease_expires_at": row.lease_expires_at.isoformat(),
        "started_at": None if row.started_at is None else row.started_at.isoformat(),
        "ended_at": None if row.ended_at is None else row.ended_at.isoformat(),
        "outcome": row.outcome,
        "failure_class": row.failure_class,
        "attempt_status": attempt.get("status"),
    }


def _writer_claim_summary(row: ProjectWriterClaimRow) -> dict[str, object]:
    return {
        "project_id": row.project_id,
        "run_id": row.run_id,
        "task_id": row.task_id,
        "attempt_id": row.attempt_id,
        "generation": row.generation,
        "updated_at": row.updated_at.isoformat(),
    }


def _event_summary(row: RunEventRow) -> dict[str, object]:
    event = _mapping(row.event_json)
    return {
        "event_id": row.event_id,
        "task_id": row.task_id,
        "sequence_no": row.sequence_no,
        "event_type": row.event_type,
        "idempotency_identity": row.idempotency_identity,
        "artifact_refs": _artifact_refs(event.get("artifact_refs")),
    }


def _effect_summary(row: RuntimeEffectProjectionRow) -> dict[str, object]:
    return {
        "effect_identity": row.effect_identity,
        "request_identity": row.request_identity,
        "task_id": row.task_id,
        "attempt_id": row.attempt_id,
        "status": row.status,
        "provider_request_id": row.provider_request_id,
        "result_ref_present": row.result_ref_json is not None,
    }


def _ledger_summary(row: ModelCallLedgerRow) -> dict[str, object]:
    return {
        "request_id": row.request_id,
        "task_id": row.task_id,
        "attempt_id": row.attempt_id,
        "status": row.status,
        "provider_request_id": row.provider_request_id,
        "provider_sent": row.provider_sent_at is not None,
        "raw_response_present": (
            row.raw_response_hash is not None or row.raw_artifact_json is not None
        ),
        "validation_error_present": row.validation_error is not None,
        "transport_error_type": row.transport_error_type,
        "completed": row.completed_at is not None,
    }


def census_from_engine(
    engine: Engine,
    *,
    project_id: str,
    run_id: str,
    object_store_root: Path | None = None,
    database_url: str = "",
) -> dict[str, object]:
    """Read one run's five required tables without opening a write transaction."""

    available = set(inspect(engine).get_table_names())
    missing = tuple(name for name in REQUIRED_TABLES if name not in available)
    if missing:
        return {
            "schema": "u8b-r0-census.v1",
            "status": "PREPARATION_FAILED",
            "read_only": True,
            "project_id": project_id,
            "run_id": run_id,
            "database_descriptor": database_descriptor(database_url),
            "missing_tables": missing,
            "mutation_attempted": False,
        }

    factory = build_session_factory(engine)
    with factory() as session:
        writer_claim_row = session.get(ProjectWriterClaimRow, project_id)
        task_rows = list(
            session.scalars(
                select(RuntimeTaskProjectionRow)
                .where(
                    RuntimeTaskProjectionRow.run_id == run_id,
                    RuntimeTaskProjectionRow.project_id == project_id,
                )
                .order_by(RuntimeTaskProjectionRow.task_id)
            )
        )
        task_ids = tuple(row.task_id for row in task_rows)
        attempt_rows = (
            list(
                session.scalars(
                    select(RuntimeTaskAttemptRow)
                    .where(RuntimeTaskAttemptRow.task_id.in_(task_ids))
                    .order_by(RuntimeTaskAttemptRow.attempt_id)
                )
            )
            if task_ids
            else []
        )
        event_rows = list(
            session.scalars(
                select(RunEventRow)
                .where(RunEventRow.run_id == run_id)
                .order_by(RunEventRow.sequence_no)
            )
        )
        effect_rows = list(
            session.scalars(
                select(RuntimeEffectProjectionRow)
                .where(RuntimeEffectProjectionRow.run_id == run_id)
                .order_by(RuntimeEffectProjectionRow.effect_identity)
            )
        )
        ledger_rows = list(
            session.scalars(
                select(ModelCallLedgerRow)
                .where(ModelCallLedgerRow.run_id == run_id)
                .order_by(ModelCallLedgerRow.request_id)
            )
        )
        project_writer_tasks = list(
            session.scalars(
                select(RuntimeTaskProjectionRow)
                .where(
                    RuntimeTaskProjectionRow.project_id == project_id,
                    RuntimeTaskProjectionRow.status == "running",
                    RuntimeTaskProjectionRow.kind.in_(
                        ("plan_commit", "draft_commit", "maintenance")
                    ),
                )
                .order_by(RuntimeTaskProjectionRow.task_id)
            )
        )

    tasks = tuple(_task_summary(row) for row in task_rows)
    attempts = tuple(_attempt_summary(row) for row in attempt_rows)
    events = tuple(_event_summary(row) for row in event_rows)
    effects = tuple(_effect_summary(row) for row in effect_rows)
    ledger = tuple(_ledger_summary(row) for row in ledger_rows)
    writer_claim = None if writer_claim_row is None else _writer_claim_summary(writer_claim_row)
    active_writer_tasks = tuple(_task_summary(row) for row in project_writer_tasks)
    writer_claim_matches_active_owner = writer_claim is not None and any(
        task["task_id"] == writer_claim["task_id"]
        and task["current_attempt_id"] == writer_claim["attempt_id"]
        for task in active_writer_tasks
    )

    refs: dict[str, dict[str, object]] = {}
    for task in tasks:
        input_refs = cast(tuple[dict[str, object], ...], task["input_artifact_refs"])
        terminal_refs = cast(tuple[dict[str, object], ...], task["terminal_artifact_refs"])
        for ref in (*input_refs, *terminal_refs):
            artifact_id = ref.get("artifact_id")
            if isinstance(artifact_id, str):
                refs[artifact_id] = dict(ref)
    for event in events:
        event_refs = cast(tuple[dict[str, object], ...], event["artifact_refs"])
        for ref in event_refs:
            artifact_id = ref.get("artifact_id")
            if isinstance(artifact_id, str):
                refs[artifact_id] = dict(ref)
    artifacts: list[dict[str, object]] = []
    for artifact_ref in sorted(refs.values(), key=lambda item: str(item.get("artifact_id"))):
        identity = _read_artifact_identity(object_store_root, artifact_ref)
        artifacts.append(
            {
                "artifact_ref": artifact_ref,
                "identity": identity,
                "source": "database_reference",
            }
        )
    scanned_artifacts = _scan_object_store(
        object_store_root,
        run_id=run_id,
        project_id=project_id,
    )
    seen_artifact_ids: set[str] = set()
    for item in artifacts:
        ref_for_scan = item.get("artifact_ref")
        if isinstance(ref_for_scan, Mapping):
            artifact_id = ref_for_scan.get("artifact_id")
            if isinstance(artifact_id, str):
                seen_artifact_ids.add(artifact_id)
    for item in scanned_artifacts:
        scanned_ref = item.get("artifact_ref")
        if isinstance(scanned_ref, Mapping):
            artifact_id = scanned_ref.get("artifact_id")
            if isinstance(artifact_id, str) and artifact_id not in seen_artifact_ids:
                artifacts.append(item)
                seen_artifact_ids.add(artifact_id)
    proposal_attempts = tuple(item for item in artifacts if _has_identity_field(item, "attempt_id"))
    terminal_results = tuple(
        item
        for item in artifacts
        if _has_identity_field(item, "workflow_phase") and _has_identity_field(item, "status")
    )

    unresolved_effects = tuple(
        effect for effect in effects if effect["status"] in _UNSETTLED_EFFECT_STATUSES
    )
    provider_sent = tuple(item for item in ledger if item["provider_sent"])
    raw_response = tuple(item for item in ledger if item["raw_response_present"])
    unsettled_ledger = tuple(
        item
        for item in ledger
        if not item["completed"]
        or item["status"] in _UNSETTLED_LEDGER_STATUSES
        or (item["provider_sent"] and not item["raw_response_present"])
    )
    active_tasks = tuple(
        task for task in tasks if task["status"] in {"running", "waiting_retry", "recovery_pending"}
    )
    event_positions = tuple(
        int(value) for event in events if isinstance((value := event.get("sequence_no")), int)
    )
    recommended = (
        "effect_reconcile_before_task_settlement"
        if unresolved_effects or unsettled_ledger
        else "operator_reconcile_attempt_after_census"
    )
    return {
        "schema": "u8b-r0-census.v1",
        "status": "CENSUS_READY",
        "read_only": True,
        "project_id": project_id,
        "run_id": run_id,
        "database_descriptor": database_descriptor(database_url),
        "tables": {
            "runtime_task_projection": {"count": len(tasks), "rows": tasks},
            "runtime_task_attempt": {"count": len(attempts), "rows": attempts},
            "run_event": {"count": len(events), "rows": events},
            "runtime_effect_projection": {"count": len(effects), "rows": effects},
            "model_call_ledger": {"count": len(ledger), "rows": ledger},
        },
        "identities": {
            "active_tasks": active_tasks,
            "active_writer_tasks": active_writer_tasks,
            "outer_attempts": attempts,
            "referenced_artifacts": artifacts,
            "proposal_attempts": proposal_attempts,
            "terminal_results": terminal_results,
            "last_event_sequence": max(event_positions, default=0),
            "writer_claim": writer_claim,
            "writer_claim_matches_active_owner": writer_claim_matches_active_owner,
        },
        "provider_activity": {
            "provider_sent_rows": provider_sent,
            "raw_response_rows": raw_response,
            "unsettled_ledger_rows": unsettled_ledger,
            "ledger_statuses": tuple(sorted({str(item["status"]) for item in ledger})),
        },
        "unresolved_effects": unresolved_effects,
        "recommended_next_action": recommended,
        "mutation_attempted": False,
    }


def collect_census(
    database_url: str,
    *,
    project_id: str,
    run_id: str,
    object_store_root: Path | None = None,
) -> dict[str, object]:
    try:
        engine = build_engine(database_url)
        try:
            return census_from_engine(
                engine,
                project_id=project_id,
                run_id=run_id,
                object_store_root=object_store_root,
                database_url=database_url,
            )
        finally:
            engine.dispose()
    except SQLAlchemyError as error:
        return {
            "schema": "u8b-r0-census.v1",
            "status": "RESOURCE_BLOCKED",
            "read_only": True,
            "project_id": project_id,
            "run_id": run_id,
            "database_descriptor": database_descriptor(database_url),
            "error_type": type(error).__name__,
            "error_summary": "database connection or read failed",
            "mutation_attempted": False,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--object-store-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    payload = collect_census(
        args.database_url,
        project_id=args.project_id,
        run_id=args.run_id,
        object_store_root=(
            None if args.object_store_root is None else args.object_store_root.resolve()
        ),
    )
    _write_once(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] == "CENSUS_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
