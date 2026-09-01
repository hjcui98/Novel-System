from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest
from scripts.audit_u8c_action_corpus import build_corpus_report
from scripts.audit_u8c_incident_manifest import AuditError

_FINDING = "application/vnd.novel-agent.memory-repair-finding+json"
_CANDIDATE_REVISION = "application/vnd.novel-agent.candidate-revision+json"
_CANDIDATE = "application/vnd.novel-agent.memory-write-candidate+json"
_VALIDATION = "application/vnd.novel-agent.validation+json"
_WORKFLOW = "application/vnd.novel-agent.memory-write-workflow-result+json"
_BASE_COMMIT = "sha256:" + "a" * 64


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


def _add_accepted_case(
    root: Path,
    *,
    finding_id: str,
    owner: str,
    facet: str,
    predicate: str,
    revision_content_hash: str | None = None,
    validation_disposition: str = "pass",
    finding_marker: str | None = None,
    workflow_base_commit: str | None = None,
    need_id: str | None = None,
    need_query: str | None = None,
    semantic_question: str | None = None,
) -> None:
    finding = {
        "finding_id": finding_id,
        "base_commit": _BASE_COMMIT,
        "classification": "canon_extraction_gap",
        "repair_owner": owner,
        "access_scope": "writer_safe",
        "mandatory_facet_ids": [f"facet.{facet}"],
        "information_boundary": {
            "maximum_visible_position": {"chapter_index": 95},
            "policy_ref": {
                "contract_id": "policy.stage5.memory-gap-boundary",
                "version": "1.0.0",
                "content_hash": "sha256:" + "b" * 64,
            },
        },
        "target_root_kind": "world",
    }
    if need_id is not None:
        finding["need_id"] = need_id
    if need_query is not None:
        finding["need_query"] = need_query
    if semantic_question is not None:
        finding["semantic_question"] = semantic_question
    if finding_marker is not None:
        finding["test_marker"] = finding_marker
    _artifact(root, finding, _FINDING)
    candidate_id = f"candidate.{finding_id}.revision-1"
    candidate = {
        "observed_changes": {
            "operations": [
                {
                    "operation": "create",
                    "payload": {
                        "record": {
                            "record_type": "world_graph_edge",
                            "predicate": predicate,
                            "subject_id": "entity.subject",
                            "relation_type": "supports",
                        }
                    },
                }
            ]
        },
        "root_update_intents": [{"root_kind": "world"}],
    }
    candidate_artifact_id = _artifact(root, candidate, _CANDIDATE)
    _artifact(
        root,
        {
            "candidate_id": candidate_id,
            "base_commit": _BASE_COMMIT,
            "content_hash": revision_content_hash or candidate_artifact_id,
            "candidate_artifact": {
                "artifact_id": candidate_artifact_id,
                "media_type": _CANDIDATE,
            },
        },
        _CANDIDATE_REVISION,
    )
    _artifact(
        root,
        {
            "candidate_id": candidate_id,
            "base_commit": _BASE_COMMIT,
            "candidate_content_hash": candidate_artifact_id,
            "disposition": validation_disposition,
        },
        _VALIDATION,
    )
    _artifact(
        root,
        {
            "accepted_candidate_id": candidate_id,
            "status": "committed",
            "workflow_phase": "complete",
            "canonical_commit_accepted": True,
            "base_commit": workflow_base_commit or _BASE_COMMIT,
            "resulting_commit": "sha256:" + "c" * 64,
            "committed_operation_ids": ["change.world-graph.relation.test"],
        },
        _WORKFLOW,
    )


def _census(root: Path) -> dict[str, object]:
    return build_corpus_report((root,))


def test_facet_identity_discriminates_owner_variance(tmp_path: Path) -> None:
    _add_accepted_case(
        tmp_path,
        finding_id="finding.current-state",
        owner="ordinary_curator",
        facet="current_state",
        predicate="state_is",
    )
    _add_accepted_case(
        tmp_path,
        finding_id="finding.relation-state",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
    )

    report = _census(tmp_path)
    census = cast(dict[str, object], report["census"])
    assert census["owner_variance_group_count"] == 1
    assert census["facet_discriminated_owner_variance_group_count"] == 1
    assert census["undiscriminated_route_ambiguity_group_count"] == 0
    admission = cast(dict[str, object], report["admission"])
    assert admission["decision"] == "U8-C_NOT_ADMITTED_KEEP_DETERMINISTIC_POLICY"


def test_same_facet_owner_variance_triggers_u8c(tmp_path: Path) -> None:
    _add_accepted_case(
        tmp_path,
        finding_id="finding.relation-one",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
    )
    _add_accepted_case(
        tmp_path,
        finding_id="finding.relation-two",
        owner="ordinary_curator",
        facet="relation_state",
        predicate="relation_is",
    )

    report = _census(tmp_path)
    census = cast(dict[str, object], report["census"])
    assert census["undiscriminated_route_ambiguity_group_count"] == 1
    admission = cast(dict[str, object], report["admission"])
    assert admission["decision"] == "U8-C_TRIGGER_SHOWN"


def test_same_problem_identity_with_new_finding_id_triggers_u8c(tmp_path: Path) -> None:
    shared = {
        "need_id": "need.shared",
        "need_query": "same question",
        "semantic_question": "same question",
    }
    _add_accepted_case(
        tmp_path,
        finding_id="finding.same-problem-one",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
        **shared,
    )
    _add_accepted_case(
        tmp_path,
        finding_id="finding.same-problem-two",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_at",
        **shared,
    )

    report = _census(tmp_path)
    census = cast(dict[str, object], report["census"])
    assert census["same_problem_multiple_semantic_action_group_count"] == 1
    admission = cast(dict[str, object], report["admission"])
    assert admission["decision"] == "U8-C_TRIGGER_SHOWN"


def test_distinct_problem_identity_does_not_trigger_same_problem_check(tmp_path: Path) -> None:
    _add_accepted_case(
        tmp_path,
        finding_id="finding.distinct-problem-one",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
        need_id="need.one",
        need_query="question one",
        semantic_question="question one",
    )
    _add_accepted_case(
        tmp_path,
        finding_id="finding.distinct-problem-two",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_at",
        need_id="need.two",
        need_query="question two",
        semantic_question="question two",
    )

    report = _census(tmp_path)
    census = cast(dict[str, object], report["census"])
    assert census["same_problem_multiple_semantic_action_group_count"] == 0
    admission = cast(dict[str, object], report["admission"])
    assert admission["decision"] == "U8-C_NOT_ADMITTED_KEEP_DETERMINISTIC_POLICY"


def test_missing_problem_identity_is_not_guessed_or_merged(tmp_path: Path) -> None:
    _add_accepted_case(
        tmp_path,
        finding_id="finding.unknown-problem-one",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
    )
    _add_accepted_case(
        tmp_path,
        finding_id="finding.unknown-problem-two",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_at",
    )

    report = _census(tmp_path)
    census = cast(dict[str, object], report["census"])
    assert census["same_problem_multiple_semantic_action_group_count"] == 0
    admission = cast(dict[str, object], report["admission"])
    assert admission["decision"] == "U8-C_NOT_ADMITTED_KEEP_DETERMINISTIC_POLICY"


def test_candidate_lineage_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    _add_accepted_case(
        tmp_path,
        finding_id="finding.invalid-lineage",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
        revision_content_hash="sha256:" + "f" * 64,
    )

    with pytest.raises(AuditError, match="content_hash does not match candidate"):
        _census(tmp_path)


def test_duplicate_candidate_revision_identity_fails_closed(tmp_path: Path) -> None:
    _add_accepted_case(
        tmp_path,
        finding_id="finding.duplicate",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
    )
    _add_accepted_case(
        tmp_path,
        finding_id="finding.duplicate",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_alias",
    )

    with pytest.raises(AuditError, match="duplicate candidate_id"):
        _census(tmp_path)


def test_duplicate_finding_identity_fails_closed(tmp_path: Path) -> None:
    _add_accepted_case(
        tmp_path,
        finding_id="finding.duplicate-finding",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
        finding_marker="one",
    )
    _add_accepted_case(
        tmp_path,
        finding_id="finding.duplicate-finding",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
        finding_marker="two",
    )

    with pytest.raises(AuditError, match="duplicate finding_id"):
        _census(tmp_path)


def test_duplicate_validation_identity_fails_closed(tmp_path: Path) -> None:
    _add_accepted_case(
        tmp_path,
        finding_id="finding.duplicate-validation",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
    )
    _artifact(
        tmp_path,
        {
            "candidate_id": "candidate.finding.duplicate-validation.revision-1",
            "base_commit": _BASE_COMMIT,
            "candidate_content_hash": "sha256:" + "d" * 64,
            "disposition": "pass",
        },
        _VALIDATION,
    )

    with pytest.raises(AuditError, match="duplicate candidate_id"):
        _census(tmp_path)


def test_accepted_workflow_requires_committed_result(tmp_path: Path) -> None:
    _add_accepted_case(
        tmp_path,
        finding_id="finding.invalid-workflow",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
    )
    _artifact(
        tmp_path,
        {
            "accepted_candidate_id": "candidate.finding.invalid-workflow.revision-1",
            "status": "noop",
        },
        _WORKFLOW,
    )

    with pytest.raises(AuditError, match="non-committed workflow"):
        _census(tmp_path)


def test_accepted_workflow_base_must_match_finding(tmp_path: Path) -> None:
    _add_accepted_case(
        tmp_path,
        finding_id="finding.workflow-basis-mismatch",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
        workflow_base_commit="sha256:" + "d" * 64,
    )

    with pytest.raises(AuditError, match="workflow base_commit does not match finding"):
        _census(tmp_path)


def test_accepted_candidate_requires_passing_validation(tmp_path: Path) -> None:
    _add_accepted_case(
        tmp_path,
        finding_id="finding.invalid-validation",
        owner="graph_curator",
        facet="relation_state",
        predicate="relation_is",
        validation_disposition="reject",
    )

    with pytest.raises(AuditError, match="validation disposition is not pass"):
        _census(tmp_path)
