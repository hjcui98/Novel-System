from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest
from scripts.audit_u8c_incident_manifest import AuditError, build_audit_report

SHA_SOURCE = "sha256:" + "a" * 64
SHA_DEV = "sha256:" + "b" * 64
SHA_HOLD = "sha256:" + "c" * 64
SOURCE_ARTIFACT_ID = (
    "sha256:"
    + hashlib.sha256(json.dumps({"stream": "public"}, sort_keys=True).encode()).hexdigest()
)


def _artifact(root: Path, payload: object, media_type: str) -> str:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload, sort_keys=True).encode()
    digest = hashlib.sha256(raw).hexdigest()
    directory = root / "sha256" / digest[:2]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / digest).write_bytes(raw)
    (directory / f"{digest}.metadata.json").write_text(
        json.dumps({"byte_length": len(raw), "media_type": media_type}), encoding="utf-8"
    )
    return f"sha256:{digest}"


def _ref(artifact_id: str, media_type: str, byte_length: int | None = None) -> dict[str, object]:
    ref: dict[str, object] = {"artifact_id": artifact_id, "media_type": media_type}
    if byte_length is not None:
        ref["byte_length"] = byte_length
    return ref


def _problem_identity() -> dict[str, object]:
    return {
        "need_id": "need.u8c.preregistered.betrothed",
        "question_id": "question.u8c.betrothed",
        "need_query": "徐有容与陈长生的婚约关系是什么?",
        "semantic_question": "预注册: 徐有容与陈长生的婚约关系是什么?",
        "facet": "relation_state",
        "source_commit": SHA_SOURCE,
        "source_text_root": SOURCE_ARTIFACT_ID,
        "cutoff_chapter": 95,
    }


def _manifest(*, problem_identity: dict[str, object] | None = None) -> dict[str, object]:
    payload = {
        "schema": "u8c-incident-preregistration.v1",
        "status": "PREREGISTERED",
        "source": {
            "project_id": "project.source",
            "commit": SHA_SOURCE,
            "text_root": SOURCE_ARTIFACT_ID,
            "benchmark_stream_file_count": 301,
            "future_gold_or_evaluator_inputs": False,
        },
        "fixed_runtime": {
            "failure_class": "canon_extraction_gap",
            "cutoff_chapter": 95,
            "target_chapter": 96,
            "access_scope": "writer_safe",
            "model_profile": "test-model",
            "reasoner_enabled": False,
            "online_policy_mutation": False,
            "evaluator_feedback_writeback": False,
        },
        "identity_split": [
            {
                "split": "development",
                "incident_id": "incident.dev",
                "project_id": "project.dev",
                "run_id": "run.dev",
                "basis_commit": SHA_DEV,
            },
            {
                "split": "held_out",
                "incident_id": "incident.hold",
                "project_id": "project.hold",
                "run_id": "run.hold",
                "basis_commit": SHA_HOLD,
            },
        ],
    }
    if problem_identity is not None:
        payload["problem_identity"] = problem_identity
    return payload


def _run(
    root: Path,
    *,
    project_id: str,
    run_id: str,
    basis_commit: str,
    problem_identity: dict[str, object] | None = None,
) -> dict[str, object]:
    source_ref = _ref(_artifact(root, {"stream": "public"}, "application/json"), "application/json")
    visibility_id = _artifact(
        root,
        {
            "access_scope": "writer_safe",
            "boundary_id": f"boundary.{run_id}",
            "provenance": "canonical_root",
            "source_artifact": source_ref,
            "visible_through": {"chapter_index": 95},
        },
        "application/vnd.novel-agent.source-visibility-receipt+json",
    )
    checkpoint_ref = None
    if problem_identity is not None:
        checkpoint_id = _artifact(
            root,
            {"problem_identity_seed": problem_identity},
            "application/vnd.novel-agent.planning-loop-checkpoint+json",
        )
        checkpoint_ref = _ref(
            checkpoint_id, "application/vnd.novel-agent.planning-loop-checkpoint+json"
        )
    finding_payload = {
        "finding_id": f"finding.{run_id}",
        "incident_id": f"incident.{run_id}",
        "project_id": project_id,
        "planner_run_id": run_id,
        "base_commit": basis_commit,
        "classification": "canon_extraction_gap",
        "repair_owner": "graph_curator",
        "access_scope": "writer_safe",
        "mandatory_facet_ids": [
            f"facet.{problem_identity['need_id']}.relation_state"
            if problem_identity is not None
            else "facet.relation_state"
        ],
        "source_chapter_indices": [95],
        "source_artifact_refs": [source_ref],
        "source_visibility_receipt_refs": [
            _ref(visibility_id, "application/vnd.novel-agent.source-visibility-receipt+json")
        ],
        "information_boundary": {
            "evaluator_sources_forbidden": True,
            "maximum_visible_position": {"chapter_index": 95},
            "policy_ref": {
                "contract_id": "policy.stage5.memory-gap-boundary",
                "version": "1.0.0",
                "content_hash": SHA_SOURCE,
            },
        },
        "planner_intent_ref": _ref(
            _artifact(root, b"markdown intent", "text/markdown"), "text/markdown"
        ),
    }
    if problem_identity is not None:
        finding_payload.update(
            {
                "need_id": problem_identity["need_id"],
                "need_query": problem_identity["need_query"],
                "semantic_question": problem_identity["semantic_question"],
                "planner_checkpoint_ref": checkpoint_ref,
            }
        )
    finding_id = _artifact(
        root,
        finding_payload,
        "application/vnd.novel-agent.memory-repair-finding+json",
    )
    workflow_id = _artifact(
        root,
        {
            "base_commit": basis_commit,
            "status": "noop",
            "accepted_candidate_id": None,
            "canonical_commit_accepted": False,
            "committed_operation_ids": [],
            "world_mutation_noop": True,
        },
        "application/vnd.novel-agent.memory-write-workflow-result+json",
    )
    validation_id = _artifact(
        root,
        {
            "base_commit": basis_commit,
            "disposition": "pass",
            "model_profile": None,
        },
        "application/vnd.novel-agent.validation+json",
    )
    terminal_id = _artifact(
        root,
        {
            "base_commit": basis_commit,
            "status": "noop",
            "canonical_commit_accepted": False,
            "committed_operation_ids": [],
            "world_mutation_noop": True,
        },
        "application/vnd.novel-agent.terminal-result+json",
    )
    finding_ref = _ref(finding_id, "application/vnd.novel-agent.memory-repair-finding+json")
    report = {
        "project_id": project_id,
        "run_id": run_id,
        "final_commit": basis_commit,
        "status": "blocked",
        "current_chapter": 95,
        "target_chapter": 96,
        "completed_chapters": [],
        "outputs_frozen": False,
        "tasks": [
            {"status": "blocked", "terminal_artifact_refs": [finding_ref]},
            {
                "status": "blocked",
                "terminal_artifact_refs": [
                    _ref(
                        workflow_id, "application/vnd.novel-agent.memory-write-workflow-result+json"
                    ),
                    _ref(validation_id, "application/vnd.novel-agent.validation+json"),
                    _ref(terminal_id, "application/vnd.novel-agent.terminal-result+json"),
                ],
            },
        ],
    }
    return report


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.write_text(json.dumps(report), encoding="utf-8")


def test_audit_accepts_pair_and_proves_no_reasoner_trigger(tmp_path: Path) -> None:
    dev_root = tmp_path / "dev"
    hold_root = tmp_path / "hold"
    dev_report = tmp_path / "dev-report.json"
    hold_report = tmp_path / "hold-report.json"
    _write_report(
        dev_report, _run(dev_root, project_id="project.dev", run_id="run.dev", basis_commit=SHA_DEV)
    )
    _write_report(
        hold_report,
        _run(hold_root, project_id="project.hold", run_id="run.hold", basis_commit=SHA_HOLD),
    )
    manifest_path = tmp_path / "manifest.json"
    _write_report(manifest_path, _manifest())

    report = build_audit_report(
        manifest_path=manifest_path,
        development_report=dev_report,
        development_object_root=dev_root,
        held_out_report=hold_report,
        held_out_object_root=hold_root,
    )

    assert report["status"] == "AUDITED"
    comparison = cast(dict[str, object], report["comparison"])
    assert comparison["same_typed_failure_and_safety_boundary"] is True
    assert comparison["validator_accepted_action_count"] == 0
    assert comparison["receipt_state_disambiguation"] == "NOT_SHOWN"
    assert comparison["held_out_beats_deterministic_baseline"] is False
    admission = cast(dict[str, object], report["admission"])
    assert admission["decision"] == "U8-C_NOT_ADMITTED_KEEP_DETERMINISTIC_POLICY"


def test_audit_rejects_report_identity_mismatch(tmp_path: Path) -> None:
    dev_root = tmp_path / "dev"
    hold_root = tmp_path / "hold"
    dev_report = tmp_path / "dev-report.json"
    hold_report = tmp_path / "hold-report.json"
    _write_report(
        dev_report,
        _run(dev_root, project_id="project.other", run_id="run.dev", basis_commit=SHA_DEV),
    )
    _write_report(
        hold_report,
        _run(hold_root, project_id="project.hold", run_id="run.hold", basis_commit=SHA_HOLD),
    )
    manifest_path = tmp_path / "manifest.json"
    _write_report(manifest_path, _manifest())

    with pytest.raises(AuditError, match="project_id identity mismatch"):
        build_audit_report(
            manifest_path=manifest_path,
            development_report=dev_report,
            development_object_root=dev_root,
            held_out_report=hold_report,
            held_out_object_root=hold_root,
        )


def test_audit_requires_pre_registered_problem_identity_in_both_runs(tmp_path: Path) -> None:
    problem = _problem_identity()
    dev_root = tmp_path / "dev"
    hold_root = tmp_path / "hold"
    dev_report = tmp_path / "dev-report.json"
    hold_report = tmp_path / "hold-report.json"
    _write_report(
        dev_report,
        _run(
            dev_root,
            project_id="project.dev",
            run_id="run.dev",
            basis_commit=SHA_DEV,
            problem_identity=problem,
        ),
    )
    _write_report(
        hold_report,
        _run(
            hold_root,
            project_id="project.hold",
            run_id="run.hold",
            basis_commit=SHA_HOLD,
            problem_identity=problem,
        ),
    )
    manifest_path = tmp_path / "manifest.json"
    _write_report(manifest_path, _manifest(problem_identity=problem))

    report = build_audit_report(
        manifest_path=manifest_path,
        development_report=dev_report,
        development_object_root=dev_root,
        held_out_report=hold_report,
        held_out_object_root=hold_root,
    )

    assert report["status"] == "AUDITED"
