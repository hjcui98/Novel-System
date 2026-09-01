#!/usr/bin/env python3
"""Audit a pre-registered U8-C development/held-out incident pair.

This command is deliberately read-only.  It verifies that each report belongs to
the identity reserved in the manifest, that referenced immutable artifacts still
match their content-addressed identities, and that the two runs actually exercise
the same typed failure and safety boundary.  It does not infer a better recovery
action from model text and it never changes a database, Canon, or manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_FINDING = "application/vnd.novel-agent.memory-repair-finding+json"
_WORKFLOW = "application/vnd.novel-agent.memory-write-workflow-result+json"
_VALIDATION = "application/vnd.novel-agent.validation+json"
_TERMINAL = "application/vnd.novel-agent.terminal-result+json"
_VISIBILITY = "application/vnd.novel-agent.source-visibility-receipt+json"
_PLANNING_CHECKPOINT = "application/vnd.novel-agent.planning-loop-checkpoint+json"
_REQUIRED_REPORT_ARTIFACTS = frozenset({_FINDING, _WORKFLOW, _VALIDATION, _TERMINAL})


class AuditError(ValueError):
    """Raised when evidence cannot support an U8-C comparison."""


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuditError(f"{label} must be a JSON object")
    return cast(Mapping[str, Any], value)


def _list(value: object, *, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise AuditError(f"{label} must be a JSON array")
    return value


def _load_json(path: Path, *, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read {label}: {path}: {exc}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AuditError(f"cannot hash evidence file {path}: {exc}") from exc
    return f"sha256:{digest.hexdigest()}"


def _artifact_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AuditError(f"{label} must be a sha256:<64 lowercase hex> artifact id")
    return value


def _artifact_ref(value: object, *, label: str) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    artifact_id = value.get("artifact_id")
    media_type = value.get("media_type")
    if not isinstance(artifact_id, str) or not isinstance(media_type, str):
        return None
    result: dict[str, object] = {
        "artifact_id": _artifact_id(artifact_id, label=f"{label}.artifact_id"),
        "media_type": media_type,
    }
    for field in ("byte_length", "schema_version"):
        item = value.get(field)
        if item is not None:
            result[field] = item
    return result


def _artifact_refs(value: object, *, label: str) -> tuple[dict[str, object], ...]:
    found: dict[str, dict[str, object]] = {}

    def visit(item: object, path: str) -> None:
        ref = _artifact_ref(item, label=path)
        if ref is not None:
            artifact_id = cast(str, ref["artifact_id"])
            previous = found.get(artifact_id)
            if previous is not None and previous.get("media_type") != ref.get("media_type"):
                raise AuditError(f"artifact id has conflicting media types: {artifact_id}")
            found.setdefault(artifact_id, ref)
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                visit(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")

    visit(value, label)
    return tuple(found.values())


def _artifact_path(root: Path, artifact_id: str) -> Path:
    digest = artifact_id.removeprefix("sha256:")
    return root / "sha256" / digest[:2] / digest


def _read_artifact(
    root: Path,
    ref: Mapping[str, object],
    *,
    label: str,
) -> tuple[dict[str, object], Mapping[str, Any]]:
    artifact_id = _artifact_id(ref.get("artifact_id"), label=f"{label}.artifact_id")
    media_type = ref.get("media_type")
    if not isinstance(media_type, str) or not media_type:
        raise AuditError(f"{label}.media_type must be non-empty")
    object_path = _artifact_path(root, artifact_id)
    metadata_path = object_path.with_name(object_path.name + ".metadata.json")
    if not object_path.is_file() or not metadata_path.is_file():
        raise AuditError(f"{label} is missing immutable object or metadata: {artifact_id}")
    try:
        payload_bytes = object_path.read_bytes()
    except OSError as exc:
        raise AuditError(f"cannot read {label}: {object_path}: {exc}") from exc
    actual_hash = f"sha256:{hashlib.sha256(payload_bytes).hexdigest()}"
    if actual_hash != artifact_id:
        raise AuditError(f"{label} content hash mismatch: {artifact_id} != {actual_hash}")
    metadata = _mapping(
        _load_json(metadata_path, label=f"{label} metadata"), label=f"{label} metadata"
    )
    if metadata.get("media_type") != media_type:
        raise AuditError(
            f"{label} media type mismatch: ref={media_type!r}, "
            f"metadata={metadata.get('media_type')!r}"
        )
    byte_length = metadata.get("byte_length")
    if not isinstance(byte_length, int) or byte_length != len(payload_bytes):
        raise AuditError(f"{label} metadata byte_length does not match object")
    declared_length = ref.get("byte_length")
    if declared_length is not None and declared_length != len(payload_bytes):
        raise AuditError(f"{label} report byte_length does not match object")
    try:
        decoded = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Reports can reference immutable markdown/raw-model artifacts.  Their
        # storage identity is still checked above, but this audit only interprets
        # structured JSON receipts.
        if media_type.endswith("+json") or media_type == "application/json":
            raise AuditError(f"{label} payload is not valid JSON: {exc}") from exc
        return {
            "artifact_id": artifact_id,
            "media_type": media_type,
            "byte_length": len(payload_bytes),
        }, {}
    payload = _mapping(decoded, label=f"{label} payload")
    return {
        "artifact_id": artifact_id,
        "media_type": media_type,
        "byte_length": len(payload_bytes),
    }, payload


def _required_string(item: Mapping[str, Any], key: str, *, label: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise AuditError(f"{label}.{key} must be a non-empty string")
    return value


def _manifest_identity(manifest: Mapping[str, Any], split: str) -> Mapping[str, Any]:
    identities = _list(manifest.get("identity_split"), label="manifest.identity_split")
    matches = [
        _mapping(item, label="manifest.identity_split item")
        for item in identities
        if isinstance(item, Mapping) and item.get("split") == split
    ]
    if len(matches) != 1:
        raise AuditError(f"manifest must contain exactly one {split} identity")
    return matches[0]


def _report_artifact_refs(report: Mapping[str, Any]) -> tuple[dict[str, object], ...]:
    refs = _artifact_refs(report, label="report")
    by_type = {ref.get("media_type") for ref in refs}
    missing = sorted(_REQUIRED_REPORT_ARTIFACTS - cast(set[str], by_type))
    if missing:
        raise AuditError(f"report is missing required artifact types: {missing}")
    return refs


def _ref_by_type(
    refs: Sequence[dict[str, object]], media_type: str, *, label: str
) -> dict[str, object]:
    matches = [ref for ref in refs if ref.get("media_type") == media_type]
    if len(matches) != 1:
        raise AuditError(f"{label} must contain exactly one {media_type}, got {len(matches)}")
    return matches[0]


def _assert_identity(value: object, expected: object, *, label: str) -> None:
    if value != expected:
        raise AuditError(f"{label} identity mismatch: expected {expected!r}, got {value!r}")


def _assert_problem_identity(
    *,
    manifest: Mapping[str, Any],
    finding: Mapping[str, Any],
    loaded: Mapping[str, tuple[dict[str, object], Mapping[str, Any]]],
    split: str,
) -> None:
    """Require a pre-registered identity to survive into the finding/checkpoint.

    A pair can only be compared when both runs exercised the same Need input.
    The finding carries the durable Need/query/facet fields; the planner
    checkpoint carries the source question id and source-bound seed.  Requiring
    both prevents a report from silently relabelling a different inquiry as the
    pre-registered problem.
    """

    raw_expected = manifest.get("problem_identity")
    if raw_expected is None:
        return
    expected = _mapping(raw_expected, label="manifest.problem_identity")
    for field in ("need_id", "need_query", "semantic_question"):
        _assert_identity(finding.get(field), expected.get(field), label=f"{split} finding.{field}")
    expected_facet = expected.get("facet")
    if not isinstance(expected_facet, str) or not expected_facet:
        raise AuditError("manifest.problem_identity.facet must be a non-empty string")
    facets = finding.get("mandatory_facet_ids")
    if not isinstance(facets, list) or len(facets) != 1:
        raise AuditError(
            f"{split} finding must carry exactly one mandatory facet for the pre-registered problem"
        )
    expected_facet_id = f"facet.{expected.get('need_id')}.{expected_facet}"
    _assert_identity(facets[0], expected_facet_id, label=f"{split} finding.facet")

    checkpoint_ref = _artifact_ref(
        finding.get("planner_checkpoint_ref"), label=f"{split} finding.planner_checkpoint_ref"
    )
    if checkpoint_ref is None:
        raise AuditError(f"{split} finding has no planner checkpoint for problem identity")
    checkpoint_id = cast(str, checkpoint_ref["artifact_id"])
    checkpoint_entry = loaded.get(checkpoint_id)
    if checkpoint_entry is None:
        raise AuditError(f"{split} planner checkpoint was not loaded for problem identity")
    checkpoint = checkpoint_entry[1]
    seed = _mapping(
        checkpoint.get("problem_identity_seed"),
        label=f"{split} planner checkpoint.problem_identity_seed",
    )
    for field in (
        "need_id",
        "question_id",
        "need_query",
        "semantic_question",
        "facet",
        "source_commit",
        "source_text_root",
        "cutoff_chapter",
    ):
        _assert_identity(
            seed.get(field), expected.get(field), label=f"{split} problem_identity.{field}"
        )


def _boundary_signature(
    finding: Mapping[str, Any], *, source_text_root: str, label: str
) -> dict[str, object]:
    boundary = _mapping(finding.get("information_boundary"), label=f"{label}.information_boundary")
    maximum = _mapping(
        boundary.get("maximum_visible_position"), label=f"{label}.maximum_visible_position"
    )
    policy = _mapping(boundary.get("policy_ref"), label=f"{label}.policy_ref")
    sources = _list(finding.get("source_artifact_refs"), label=f"{label}.source_artifact_refs")
    source_ids = []
    for index, item in enumerate(sources):
        ref = _artifact_ref(item, label=f"{label}.source_artifact_refs[{index}]")
        if ref is None:
            raise AuditError(f"{label}.source_artifact_refs[{index}] is not an artifact ref")
        source_ids.append(ref["artifact_id"])
    if source_text_root not in source_ids:
        raise AuditError(f"{label} does not reference the manifest source text root")
    return {
        "classification": _required_string(finding, "classification", label=label),
        "access_scope": _required_string(finding, "access_scope", label=label),
        "cutoff_chapter": maximum.get("chapter_index"),
        "policy_contract_id": policy.get("contract_id"),
        "policy_version": policy.get("version"),
        "policy_content_hash": policy.get("content_hash"),
        "source_text_root": source_text_root,
        "evaluator_sources_forbidden": boundary.get("evaluator_sources_forbidden"),
    }


def _audit_run(
    *,
    manifest: Mapping[str, Any],
    split: str,
    report_path: Path,
    object_root: Path,
) -> dict[str, object]:
    identity = _manifest_identity(manifest, split)
    report = _mapping(_load_json(report_path, label=f"{split} report"), label=f"{split} report")
    refs = _report_artifact_refs(report)
    project_id = _required_string(identity, "project_id", label=f"manifest {split}")
    run_id = _required_string(identity, "run_id", label=f"manifest {split}")
    basis_commit = _required_string(identity, "basis_commit", label=f"manifest {split}")
    source = _mapping(manifest.get("source"), label="manifest.source")
    fixed = _mapping(manifest.get("fixed_runtime"), label="manifest.fixed_runtime")
    source_text_root = _required_string(source, "text_root", label="manifest.source")
    expected_class = _required_string(fixed, "failure_class", label="manifest.fixed_runtime")
    cutoff = fixed.get("cutoff_chapter")
    target = fixed.get("target_chapter")
    if not isinstance(cutoff, int) or not isinstance(target, int):
        raise AuditError("manifest fixed_runtime chapter bounds must be integers")
    access_scope = _required_string(fixed, "access_scope", label="manifest.fixed_runtime")

    for field in ("project_id", "run_id"):
        _assert_identity(
            report.get(field), cast(str, locals()[field]), label=f"{split} report.{field}"
        )
    _assert_identity(report.get("final_commit"), basis_commit, label=f"{split} report.final_commit")
    _assert_identity(report.get("status"), "blocked", label=f"{split} report.status")
    _assert_identity(report.get("current_chapter"), cutoff, label=f"{split} report.current_chapter")
    _assert_identity(report.get("target_chapter"), target, label=f"{split} report.target_chapter")
    if report.get("completed_chapters") != [] or report.get("outputs_frozen") is not False:
        raise AuditError(f"{split} report indicates completion or frozen outputs")

    loaded: dict[str, tuple[dict[str, object], Mapping[str, Any]]] = {}
    for ref in refs:
        normalized, payload = _read_artifact(object_root, ref, label=f"{split} report artifact")
        loaded[cast(str, normalized["artifact_id"])] = (normalized, payload)
    finding_refs = [ref for ref in refs if ref.get("media_type") == _FINDING]
    if len(finding_refs) != 1:
        raise AuditError(f"{split} report must reference exactly one unique finding")
    finding_ref = finding_refs[0]
    finding_id = cast(str, finding_ref["artifact_id"])
    finding = loaded[finding_id][1]
    _assert_identity(finding.get("project_id"), project_id, label=f"{split} finding.project_id")
    _assert_identity(finding.get("planner_run_id"), run_id, label=f"{split} finding.planner_run_id")
    _assert_identity(finding.get("base_commit"), basis_commit, label=f"{split} finding.base_commit")
    _assert_identity(
        finding.get("classification"), expected_class, label=f"{split} finding.classification"
    )
    _assert_identity(
        finding.get("access_scope"),
        access_scope,
        label=f"{split} finding.access_scope",
    )
    boundary = _mapping(
        finding.get("information_boundary"), label=f"{split} finding.information_boundary"
    )
    if boundary.get("evaluator_sources_forbidden") is not True:
        raise AuditError(f"{split} finding does not forbid evaluator sources")
    _assert_identity(
        _mapping(boundary.get("maximum_visible_position"), label=f"{split} boundary position").get(
            "chapter_index"
        ),
        cutoff,
        label=f"{split} finding cutoff",
    )

    nested_refs = _artifact_refs(finding, label=f"{split} finding")
    for ref in nested_refs:
        artifact_id = cast(str, ref["artifact_id"])
        if artifact_id not in loaded:
            loaded[artifact_id] = _read_artifact(
                object_root, ref, label=f"{split} finding nested artifact"
            )
    _assert_problem_identity(
        manifest=manifest,
        finding=finding,
        loaded=loaded,
        split=split,
    )
    visibility_refs = [ref for ref in nested_refs if ref.get("media_type") == _VISIBILITY]
    if len(visibility_refs) != 1:
        raise AuditError(f"{split} finding must reference exactly one source visibility receipt")
    visibility = loaded[cast(str, visibility_refs[0]["artifact_id"])][1]
    _assert_identity(
        visibility.get("access_scope"),
        access_scope,
        label=f"{split} visibility.access_scope",
    )
    _assert_identity(
        visibility.get("provenance"), "canonical_root", label=f"{split} visibility.provenance"
    )
    visible_through = _mapping(
        visibility.get("visible_through"), label=f"{split} visibility.visible_through"
    )
    _assert_identity(
        visible_through.get("chapter_index"), cutoff, label=f"{split} visibility cutoff"
    )
    source_ref = _mapping(
        visibility.get("source_artifact"), label=f"{split} visibility.source_artifact"
    )
    _assert_identity(
        source_ref.get("artifact_id"), source_text_root, label=f"{split} visibility.source_artifact"
    )

    workflow_ref = _ref_by_type(refs, _WORKFLOW, label=f"{split} report")
    validation_ref = _ref_by_type(refs, _VALIDATION, label=f"{split} report")
    terminal_ref = _ref_by_type(refs, _TERMINAL, label=f"{split} report")
    workflow = loaded[cast(str, workflow_ref["artifact_id"])][1]
    validation = loaded[cast(str, validation_ref["artifact_id"])][1]
    terminal = loaded[cast(str, terminal_ref["artifact_id"])][1]
    safe_action = workflow.get("safe_action_accepted") is True
    for label, payload in (("workflow", workflow), ("terminal", terminal)):
        _assert_identity(
            payload.get("base_commit"), basis_commit, label=f"{split} {label}.base_commit"
        )
        _assert_identity(payload.get("status"), "noop", label=f"{split} {label}.status")
        if payload.get("canonical_commit_accepted") is not False:
            raise AuditError(f"{split} {label} does not prove canonical commit rejection")
        if payload.get("committed_operation_ids") != []:
            raise AuditError(f"{split} {label} contains committed operations")
        if safe_action:
            if payload.get("safe_action_accepted") is not True:
                raise AuditError(f"{split} {label} does not carry the safe-action receipt")
            if payload.get("world_mutation_noop") is not False:
                raise AuditError(
                    f"{split} {label} safe action must carry a non-empty proposed mutation"
                )
            if payload.get("terminal_candidate_id") is None:
                raise AuditError(f"{split} {label} safe action has no candidate identity")
            if payload.get("accepted_candidate_id") is not None:
                raise AuditError(f"{split} {label} safe action exposes Canon acceptance")
            if "VALIDATION_ONLY_SAFE_ACTION" not in payload.get("terminal_codes", []):
                raise AuditError(f"{split} {label} is missing the validation-only terminal code")
        elif payload.get("world_mutation_noop") is not True:
            raise AuditError(f"{split} {label} does not prove world mutation was a no-op")
    _assert_identity(
        validation.get("base_commit"), basis_commit, label=f"{split} validation.base_commit"
    )
    _assert_identity(validation.get("disposition"), "pass", label=f"{split} validation.disposition")
    if validation.get("model_profile") is not None:
        raise AuditError(f"{split} validation unexpectedly carries a model profile")

    tasks = _list(report.get("tasks"), label=f"{split} report.tasks")
    task_statuses = [
        _mapping(item, label=f"{split} task[{index}]").get("status")
        for index, item in enumerate(tasks)
    ]
    if not task_statuses or any(status != "blocked" for status in task_statuses):
        raise AuditError(f"{split} report tasks are not all blocked")
    repair_owner = finding.get("repair_owner")
    if repair_owner not in {"graph_curator", "ordinary_curator"}:
        raise AuditError(f"{split} finding has unsupported repair owner: {repair_owner!r}")
    accepted_action = safe_action or workflow.get("accepted_candidate_id") is not None
    action = {
        "repair_owner": repair_owner,
        "classification": expected_class,
        "validation_disposition": validation.get("disposition"),
        "accepted_candidate": accepted_action,
        "safe_action_accepted": safe_action,
        "workflow_status": workflow.get("status"),
        "terminal_status": terminal.get("status"),
        "canonical_commit_accepted": workflow.get("canonical_commit_accepted"),
        "world_mutation_noop": workflow.get("world_mutation_noop"),
    }
    return {
        "split": split,
        "report": {"path": str(report_path.resolve()), "sha256": _sha256_file(report_path)},
        "object_root": str(object_root.resolve()),
        "project_id": project_id,
        "run_id": run_id,
        "basis_commit": basis_commit,
        "finding_artifact": finding_ref,
        "finding": {
            "finding_id": finding.get("finding_id"),
            "incident_id": finding.get("incident_id"),
            "planner_run_id": finding.get("planner_run_id"),
            "classification": finding.get("classification"),
            "repair_owner": repair_owner,
            "mandatory_facet_ids": finding.get("mandatory_facet_ids"),
            "source_chapter_indices": finding.get("source_chapter_indices"),
        },
        "boundary": _boundary_signature(
            finding, source_text_root=source_text_root, label=f"{split} finding"
        ),
        "action": action,
        "artifact_counts": {"report_refs": len(refs), "validated_objects": len(loaded)},
    }


def build_audit_report(
    *,
    manifest_path: Path,
    development_report: Path,
    development_object_root: Path,
    held_out_report: Path,
    held_out_object_root: Path,
) -> dict[str, object]:
    """Validate the pair and return a write-once, machine-readable audit report."""

    manifest = _mapping(_load_json(manifest_path, label="manifest"), label="manifest")
    _assert_identity(
        manifest.get("schema"), "u8c-incident-preregistration.v1", label="manifest.schema"
    )
    _assert_identity(manifest.get("status"), "PREREGISTERED", label="manifest.status")
    fixed = _mapping(manifest.get("fixed_runtime"), label="manifest.fixed_runtime")
    if fixed.get("reasoner_enabled") is True:
        raise AuditError("manifest cannot enable a reasoner for this deterministic comparison")
    source = _mapping(manifest.get("source"), label="manifest.source")
    source_text_root = _required_string(source, "text_root", label="manifest.source")
    development = _audit_run(
        manifest=manifest,
        split="development",
        report_path=development_report,
        object_root=development_object_root,
    )
    held_out = _audit_run(
        manifest=manifest,
        split="held_out",
        report_path=held_out_report,
        object_root=held_out_object_root,
    )
    dev_boundary = cast(dict[str, object], development["boundary"])
    hold_boundary = cast(dict[str, object], held_out["boundary"])
    same_boundary = dev_boundary == hold_boundary
    dev_action = cast(dict[str, object], development["action"])
    hold_action = cast(dict[str, object], held_out["action"])
    same_action = dev_action == hold_action
    accepted_actions = sum(
        int(cast(dict[str, object], item["action"])["accepted_candidate"] is True)
        for item in (development, held_out)
    )
    held_out_beats_baseline = False
    safe_actions = all(
        cast(dict[str, object], item["action"])["safe_action_accepted"] is True
        for item in (development, held_out)
    )
    if safe_actions and same_action:
        admission_reasons = [
            "the pair has one typed failure and one shared safety boundary",
            "both runs produced validator-accepted validation-only safe actions",
            "the safe actions selected the same deterministic graph-curator route, so no "
            "same-problem action ambiguity is shown",
            "receipt/state disambiguation remains NOT_SHOWN",
            "the held-out run does not outperform the deterministic baseline",
        ]
    elif accepted_actions == 0:
        admission_reasons = [
            "the pair has one typed failure and one shared safety boundary",
            "neither run produced a validator-accepted action",
            "receipt/state disambiguation remains NOT_SHOWN",
            "the held-out run does not outperform the deterministic baseline",
        ]
    else:
        admission_reasons = [
            "the pair has one typed failure and one shared safety boundary",
            "validator-accepted actions are present but the pair does not show two "
            "different safe actions for one problem identity",
            "receipt/state disambiguation remains NOT_SHOWN",
            "the held-out run does not outperform the deterministic baseline",
        ]
    return {
        "schema": "u8c-independent-comparison-audit.v1",
        "status": "AUDITED",
        "manifest": {"path": str(manifest_path.resolve()), "sha256": _sha256_file(manifest_path)},
        "fixed_runtime": {
            "failure_class": fixed.get("failure_class"),
            "cutoff_chapter": fixed.get("cutoff_chapter"),
            "target_chapter": fixed.get("target_chapter"),
            "access_scope": fixed.get("access_scope"),
            "model_profile": fixed.get("model_profile"),
            "reasoner_enabled": False,
            "online_policy_mutation": fixed.get("online_policy_mutation"),
            "evaluator_feedback_writeback": fixed.get("evaluator_feedback_writeback"),
        },
        "source": {
            "project_id": source.get("project_id"),
            "commit": source.get("commit"),
            "text_root": source_text_root,
            "benchmark_stream_file_count": source.get("benchmark_stream_file_count"),
            "future_gold_or_evaluator_inputs": source.get("future_gold_or_evaluator_inputs"),
        },
        "runs": [development, held_out],
        "comparison": {
            "same_typed_failure_and_safety_boundary": same_boundary,
            "development_action": dev_action,
            "held_out_action": hold_action,
            "actions_equal": same_action,
            "validator_accepted_action_count": accepted_actions,
            "receipt_state_disambiguation": "NOT_SHOWN",
            "held_out_beats_deterministic_baseline": held_out_beats_baseline,
            "canonical_or_skill_mutation_observed": False,
        },
        "admission": {
            "decision": "U8-C_NOT_ADMITTED_KEEP_DETERMINISTIC_POLICY",
            "reasoner_enabled": False,
            "reasons": admission_reasons,
        },
    }


def _write_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"U8-C audit refuses to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--development-report", type=Path, required=True)
    parser.add_argument("--development-object-root", type=Path, required=True)
    parser.add_argument("--held-out-report", type=Path, required=True)
    parser.add_argument("--held-out-object-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = build_audit_report(
            manifest_path=args.manifest,
            development_report=args.development_report,
            development_object_root=args.development_object_root,
            held_out_report=args.held_out_report,
            held_out_object_root=args.held_out_object_root,
        )
        _write_once(args.output, payload)
        print(
            json.dumps({"output": str(args.output.resolve()), "sha256": _sha256_file(args.output)})
        )
    except (AuditError, OSError, RuntimeError) as exc:
        print(f"U8-C audit failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
