from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.census_u8b_zrun1 import census_from_engine, database_descriptor
from sqlalchemy import create_engine

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import (
    ModelCallLedgerRow,
    ProjectWriterClaimRow,
    RunEventRow,
    RuntimeEffectProjectionRow,
    RuntimeTaskAttemptRow,
    RuntimeTaskProjectionRow,
)


def _artifact(digest: str, media_type: str) -> dict[str, object]:
    return {
        "artifact_id": f"sha256:{digest}",
        "media_type": media_type,
        "byte_length": 1,
        "schema_version": "1.0.0",
    }


def test_database_descriptor_drops_credentials_and_query() -> None:
    assert (
        database_descriptor("postgresql+psycopg://user:secret@127.0.0.1:5432/db?sslmode=require")
        == "postgresql+psycopg://127.0.0.1:5432/db"
    )


def test_census_from_engine_is_read_only_and_expands_referenced_identity(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'census.sqlite'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    run_id = "run.census"
    project_id = "project.census"
    task_id = "run.census.maintenance"
    attempt_id = "attempt.census"
    finding = _artifact("a" * 64, "application/vnd.novel-agent.memory-repair-finding+json")
    terminal = _artifact("b" * 64, "application/vnd.novel-agent.terminal-result+json")
    orphan_proposal = _artifact(
        "9" * 64,
        "application/vnd.novel-agent.curator-proposal-attempt-receipt+json",
    )
    object_root = tmp_path / "objects"
    for ref, artifact_payload in (
        (finding, {"finding_id": "memory-gap.census", "project_id": project_id}),
        (
            terminal,
            {
                "request_id": "memory-write.memory-gap.census",
                "status": "fatal",
                "workflow_phase": "precommit",
                "checkpoint_ref": None,
                "terminal_result_ref": None,
            },
        ),
    ):
        artifact_id = str(ref["artifact_id"]).removeprefix("sha256:")
        path = object_root / "sha256" / artifact_id[:2] / artifact_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(artifact_payload), encoding="utf-8")
    orphan_digest = str(orphan_proposal["artifact_id"]).removeprefix("sha256:")
    orphan_path = object_root / "sha256" / orphan_digest[:2] / orphan_digest
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_text(
        json.dumps(
            {
                "workflow_request_id": "memory-maintenance.run.census.maintenance",
                "attempt_id": "proposal-attempt.census",
                "status": "requested",
                "provider_call_count": 0,
                "model_request_ids": [],
                "raw_response_refs": [],
                "run_id": run_id,
            }
        ),
        encoding="utf-8",
    )
    orphan_path.with_name(orphan_path.name + ".metadata.json").write_text(
        json.dumps(
            {
                "byte_length": orphan_path.stat().st_size,
                "media_type": orphan_proposal["media_type"],
            }
        ),
        encoding="utf-8",
    )

    task_json = {
        "input_artifact_refs": [finding],
        "terminal_artifact_refs": [terminal],
    }
    factory = build_session_factory(engine)
    with factory() as session, session.begin():
        session.add(
            RuntimeTaskProjectionRow(
                task_id=task_id,
                run_id=run_id,
                project_id=project_id,
                kind="maintenance",
                status="running",
                revision=2,
                current_attempt_id=attempt_id,
                basis_commit="sha256:" + "c" * 64,
                basis_snapshot="snapshot.census",
                policy_hash="sha256:" + "d" * 64,
                permission_hash="sha256:" + "e" * 64,
                priority=50,
                scheduled_for=None,
                task_json=task_json,
                updated_at=now,
            )
        )
        session.add(
            RuntimeTaskAttemptRow(
                attempt_id=attempt_id,
                task_id=task_id,
                attempt_no=1,
                worker_id="worker.census",
                claim_digest="sha256:" + "f" * 64,
                fence_generation=1,
                claimed_at=now,
                heartbeat_at=now,
                lease_expires_at=now,
                started_at=now,
                ended_at=None,
                outcome=None,
                failure_class=None,
                attempt_json={"status": "running"},
            )
        )
        session.add(
            ProjectWriterClaimRow(
                project_id=project_id,
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                generation=3,
                updated_at=now,
            )
        )
        session.add(
            RunEventRow(
                event_id="event.census",
                run_id=run_id,
                task_id=task_id,
                sequence_no=7,
                event_type="runtime.task.created",
                occurred_at=now,
                idempotency_identity="event-effect.census",
                payload_schema_version="1.0.0",
                trace_id="trace.census",
                event_json={"artifact_refs": [orphan_proposal]},
            )
        )
        session.add(
            RuntimeEffectProjectionRow(
                effect_identity="effect.census",
                request_identity="request.census",
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                status="requested",
                provider_request_id=None,
                result_ref_json=None,
                effect_json={},
            )
        )
        session.add(
            ModelCallLedgerRow(
                request_id="model-request.census",
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                request_hash="sha256:" + "1" * 64,
                status="requested",
                logical_phase="curator",
                effective_budget_json={},
                reasoning_included_in_completion_tokens=False,
                provider_request_id=None,
                provider_sent_at=None,
                raw_response_hash=None,
                raw_artifact_json=None,
                call_record_json=None,
                validation_error=None,
                transport_error_type=None,
                requested_at=now,
                completed_at=None,
            )
        )

    payload: Any = census_from_engine(
        engine,
        project_id=project_id,
        run_id=run_id,
        object_store_root=object_root,
        database_url="postgresql+psycopg://user:secret@127.0.0.1:5432/census",
    )
    assert payload["status"] == "CENSUS_READY"
    tables = payload["tables"]
    assert tables["runtime_task_projection"]["count"] == 1
    assert tables["runtime_task_attempt"]["count"] == 1
    assert tables["run_event"]["count"] == 1
    assert tables["runtime_effect_projection"]["count"] == 1
    assert tables["model_call_ledger"]["count"] == 1
    assert (
        tables["run_event"]["rows"][0]["artifact_refs"][0]["artifact_id"]
        == (orphan_proposal["artifact_id"])
    )
    assert payload["unresolved_effects"][0]["effect_identity"] == "effect.census"
    assert payload["provider_activity"]["provider_sent_rows"] == ()
    assert payload["provider_activity"]["raw_response_rows"] == ()
    assert payload["provider_activity"]["unsettled_ledger_rows"][0]["request_id"] == (
        "model-request.census"
    )
    assert payload["recommended_next_action"] == "effect_reconcile_before_task_settlement"
    assert payload["identities"]["last_event_sequence"] == 7
    assert payload["identities"]["writer_claim"] == {
        "project_id": project_id,
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "generation": 3,
        "updated_at": now.replace(tzinfo=None).isoformat(),
    }
    assert payload["identities"]["writer_claim_matches_active_owner"] is True
    assert payload["identities"]["active_writer_tasks"][0]["task_id"] == task_id
    assert any(
        item["artifact_ref"]["artifact_id"] == orphan_proposal["artifact_id"]
        for item in payload["identities"]["referenced_artifacts"]
    )
    assert any(
        item["identity"]["attempt_id"] == "proposal-attempt.census"
        for item in payload["identities"]["proposal_attempts"]
    )
    identities = {
        item["identity"]["finding_id"]
        for item in payload["identities"]["referenced_artifacts"]
        if item["identity"] is not None and "finding_id" in item["identity"]
    }
    assert identities == {"memory-gap.census"}
    assert payload["database_descriptor"] == "postgresql+psycopg://127.0.0.1:5432/census"


def test_census_from_engine_reports_missing_tables_without_mutation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    payload: Any = census_from_engine(
        engine,
        project_id="project.census",
        run_id="run.census",
        database_url="postgresql+psycopg://127.0.0.1:5432/census",
    )
    assert payload["status"] == "PREPARATION_FAILED"
    assert payload["mutation_attempted"] is False
    assert set(payload["missing_tables"]) == {
        "runtime_task_projection",
        "runtime_task_attempt",
        "run_event",
        "runtime_effect_projection",
        "model_call_ledger",
    }
