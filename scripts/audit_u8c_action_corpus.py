#!/usr/bin/env python3
"""Census accepted U8-C recovery routes across immutable artifact roots.

The U8-C trigger concerns recovery-route selection, not whether two different
questions produce different fact payloads.  This read-only census therefore
reports both dimensions and marks owner variance as discriminated whenever the
finding's facet/query identity already explains it.  It never invokes a model or
changes a database, object root, Canon, or existing report.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

if __package__ in {None, ""}:  # pragma: no cover - direct ``python scripts/...`` execution
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.audit_u8c_incident_manifest import (
    _FINDING,
    AuditError,
    _mapping,
    _read_artifact,
    _sha256_file,
    _write_once,
)

_CANDIDATE_REVISION = "application/vnd.novel-agent.candidate-revision+json"
_CANDIDATE = "application/vnd.novel-agent.memory-write-candidate+json"
_VALIDATION = "application/vnd.novel-agent.validation+json"
_WORKFLOW = "application/vnd.novel-agent.memory-write-workflow-result+json"
_SAFE_ACTION_CODE = "VALIDATION_ONLY_SAFE_ACTION"


def _artifact_payloads(root: Path) -> dict[str, tuple[str, Mapping[str, Any]]]:
    result: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for metadata_path in sorted(root.glob("sha256/*/*.metadata.json")):
        try:
            metadata = _mapping(
                json.loads(metadata_path.read_text(encoding="utf-8")), label="metadata"
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AuditError):
            continue
        media_type = metadata.get("media_type")
        if media_type not in {_FINDING, _CANDIDATE_REVISION, _CANDIDATE, _VALIDATION, _WORKFLOW}:
            continue
        artifact_id = f"sha256:{metadata_path.name.removesuffix('.metadata.json')}"
        ref: dict[str, object] = {"artifact_id": artifact_id, "media_type": media_type}
        byte_length = metadata.get("byte_length")
        if isinstance(byte_length, int):
            ref["byte_length"] = byte_length
        try:
            normalized, payload = _read_artifact(root, ref, label=f"{root.name} {media_type}")
        except AuditError:
            raise
        result[artifact_id] = (cast(str, normalized["media_type"]), payload)
    return result


def _artifact_ref_ids(value: object) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        artifact_id = value.get("artifact_id")
        return (artifact_id,) if isinstance(artifact_id, str) else ()
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_artifact_ref_ids(item))
        return tuple(result)
    return ()


def _index_unique_payloads(
    artifacts: Mapping[str, tuple[str, Mapping[str, Any]]],
    *,
    media_type: str,
    identity_field: str,
    label: str,
) -> dict[str, Mapping[str, Any]]:
    """Index logical identities without silently overwriting duplicate artifacts.

    Content-addressing makes byte-identical references converge on one object id,
    but it does not prevent two different objects from declaring the same logical
    finding/candidate identity.  Such a collision makes the lineage choice
    order-dependent, so the census must fail closed instead of selecting one.
    """

    indexed: dict[str, Mapping[str, Any]] = {}
    for artifact_id, (actual_media_type, payload) in artifacts.items():
        if actual_media_type != media_type:
            continue
        identity = payload.get(identity_field)
        if not isinstance(identity, str):
            continue
        if identity in indexed:
            raise AuditError(
                f"{label} has duplicate {identity_field} {identity!r} "
                f"across immutable artifacts (including {artifact_id})"
            )
        indexed[identity] = payload
    return indexed


def _accepted_workflows(
    workflows: Sequence[Mapping[str, Any]], *, label: str
) -> tuple[set[str], dict[str, tuple[Mapping[str, Any], ...]]]:
    """Return accepted workflow ids after checking the committed-result contract."""

    accepted_ids: set[str] = set()
    by_candidate: dict[str, list[Mapping[str, Any]]] = {}
    for workflow in workflows:
        candidate_id = workflow.get("accepted_candidate_id")
        if not isinstance(candidate_id, str):
            continue
        if workflow.get("status") != "committed":
            raise AuditError(
                f"{label} accepted candidate {candidate_id!r} has a non-committed workflow"
            )
        if workflow.get("workflow_phase") != "complete":
            raise AuditError(
                f"{label} accepted candidate {candidate_id!r} workflow is not complete"
            )
        if workflow.get("canonical_commit_accepted") is not True:
            raise AuditError(
                f"{label} accepted candidate {candidate_id!r} does not prove Canon acceptance"
            )
        committed_operations = workflow.get("committed_operation_ids")
        if not isinstance(committed_operations, list) or not committed_operations:
            raise AuditError(
                f"{label} accepted candidate {candidate_id!r} has no committed operations"
            )
        if not all(
            isinstance(operation_id, str) and operation_id for operation_id in committed_operations
        ):
            raise AuditError(
                f"{label} accepted candidate {candidate_id!r} has invalid operation ids"
            )
        if not isinstance(workflow.get("base_commit"), str):
            raise AuditError(f"{label} accepted candidate {candidate_id!r} has no base commit")
        if not isinstance(workflow.get("resulting_commit"), str):
            raise AuditError(f"{label} accepted candidate {candidate_id!r} has no resulting commit")
        accepted_ids.add(candidate_id)
        by_candidate.setdefault(candidate_id, []).append(workflow)
    return accepted_ids, {
        candidate_id: tuple(items) for candidate_id, items in by_candidate.items()
    }


def _safe_action_workflows(
    workflows: Sequence[Mapping[str, Any]], *, label: str
) -> tuple[set[str], dict[str, tuple[Mapping[str, Any], ...]]]:
    """Return complete validator-pass candidates without Canon authority."""

    safe_ids: set[str] = set()
    by_candidate: dict[str, list[Mapping[str, Any]]] = {}
    for workflow in workflows:
        if workflow.get("safe_action_accepted") is not True:
            continue
        candidate_id = workflow.get("terminal_candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise AuditError(f"{label} safe action has no terminal candidate id")
        if workflow.get("status") != "noop" or workflow.get("workflow_phase") != "complete":
            raise AuditError(f"{label} safe action is not a completed NOOP")
        if workflow.get("canonical_commit_accepted") is not False:
            raise AuditError(f"{label} safe action exposes Canon acceptance")
        if (
            workflow.get("resulting_commit") is not None
            or workflow.get("commit_receipt") is not None
        ):
            raise AuditError(f"{label} safe action carries a commit result")
        if workflow.get("world_mutation_noop") is not False:
            raise AuditError(f"{label} safe action does not contain a proposed mutation")
        terminal_codes = workflow.get("terminal_codes")
        if not isinstance(terminal_codes, list) or _SAFE_ACTION_CODE not in terminal_codes:
            raise AuditError(f"{label} safe action is missing its terminal code")
        safe_ids.add(candidate_id)
        by_candidate.setdefault(candidate_id, []).append(workflow)
    return safe_ids, {candidate_id: tuple(items) for candidate_id, items in by_candidate.items()}


def _semantic_action(payload: Mapping[str, Any]) -> tuple[tuple[object, ...], ...]:
    observed = payload.get("observed_changes")
    operations = observed.get("operations") if isinstance(observed, Mapping) else None
    actions: list[tuple[object, ...]] = []
    if isinstance(operations, list):
        for operation in operations:
            if not isinstance(operation, Mapping):
                continue
            operation_payload = operation.get("payload")
            record = (
                operation_payload.get("record") if isinstance(operation_payload, Mapping) else None
            )
            if not isinstance(record, Mapping):
                record = {}
            actions.append(
                (
                    operation.get("operation"),
                    record.get("record_type"),
                    record.get("predicate"),
                    record.get("subject_id"),
                    record.get("relation_type"),
                )
            )
    intents = payload.get("root_update_intents")
    root_kind_values = (
        [
            intent.get("root_kind")
            for intent in intents
            if isinstance(intent, Mapping) and isinstance(intent.get("root_kind"), str)
        ]
        if isinstance(intents, list)
        else []
    )
    root_kinds = tuple(sorted(cast(list[str], root_kind_values)))
    return (tuple(actions), root_kinds)


def _facet_suffixes(finding: Mapping[str, Any]) -> tuple[str, ...]:
    facets = finding.get("mandatory_facet_ids")
    if not isinstance(facets, list):
        return ()
    return tuple(sorted({str(item).rsplit(".", 1)[-1] for item in facets if isinstance(item, str)}))


def _problem_identity(finding: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Return the stable semantic-problem key used for cross-finding comparison.

    A planner can re-emit the same problem with a new finding id.  Finding ids
    therefore cannot be the only key for the U8-C same-problem action check.
    Missing identity fields are intentionally excluded rather than collapsed
    into one bucket: an unknown problem is not evidence that two actions answer
    the same question.
    """

    identity_fields = ("need_id", "need_query", "semantic_question")
    values = [finding.get(field) for field in identity_fields]
    if not all(isinstance(value, str) and value for value in values):
        return None
    facets = _facet_suffixes(finding)
    if not facets:
        return None
    return tuple(cast(str, value) for value in values) + facets


def _boundary(finding: Mapping[str, Any]) -> tuple[object, ...]:
    boundary = finding.get("information_boundary")
    boundary_map = boundary if isinstance(boundary, Mapping) else {}
    maximum = boundary_map.get("maximum_visible_position")
    maximum_map = maximum if isinstance(maximum, Mapping) else {}
    policy = boundary_map.get("policy_ref")
    policy_map = policy if isinstance(policy, Mapping) else {}
    source_refs = finding.get("source_artifact_refs")
    source_ids = tuple(sorted(_artifact_ref_ids(source_refs)))
    return (
        finding.get("classification"),
        finding.get("access_scope"),
        maximum_map.get("chapter_index"),
        policy_map.get("contract_id"),
        policy_map.get("version"),
        policy_map.get("content_hash"),
        source_ids,
        finding.get("target_root_kind"),
    )


def _finding_id_from_candidate(candidate_id: object, finding_ids: Sequence[str]) -> str | None:
    if not isinstance(candidate_id, str):
        return None
    matches = [finding_id for finding_id in finding_ids if finding_id in candidate_id]
    return max(matches, key=len) if matches else None


def build_corpus_report(object_roots: tuple[Path, ...]) -> dict[str, object]:
    if not object_roots:
        raise AuditError("at least one object root is required")
    group_rows: dict[tuple[object, ...], list[dict[str, object]]] = defaultdict(list)
    root_summaries: list[dict[str, object]] = []
    unique_findings: set[tuple[str, str]] = set()
    accepted_candidates = 0
    safe_action_candidates = 0
    accepted_validated_candidates = 0
    safe_action_validated_candidates = 0

    for root in object_roots:
        if not root.is_dir():
            raise AuditError(f"object root is not a directory: {root}")
        artifacts = _artifact_payloads(root)
        findings = _index_unique_payloads(
            artifacts,
            media_type=_FINDING,
            identity_field="finding_id",
            label=f"{root.name} finding lineage",
        )
        revisions = _index_unique_payloads(
            artifacts,
            media_type=_CANDIDATE_REVISION,
            identity_field="candidate_id",
            label=f"{root.name} candidate revision lineage",
        )
        candidates = {
            artifact_id: payload
            for artifact_id, (media_type, payload) in artifacts.items()
            if media_type == _CANDIDATE
        }
        validations = _index_unique_payloads(
            artifacts,
            media_type=_VALIDATION,
            identity_field="candidate_id",
            label=f"{root.name} validation lineage",
        )
        workflows = [
            payload for media_type, payload in artifacts.values() if media_type == _WORKFLOW
        ]
        accepted_ids, accepted_workflows = _accepted_workflows(
            workflows, label=f"{root.name} workflow lineage"
        )
        safe_ids, safe_workflows = _safe_action_workflows(
            workflows, label=f"{root.name} workflow lineage"
        )
        overlap = accepted_ids & safe_ids
        if overlap:
            raise AuditError(
                f"{root.name} candidate is both Canon-accepted and validation-only: "
                f"{sorted(overlap)}"
            )
        root_accepted = 0
        root_safe = 0
        root_validated = 0
        root_safe_validated = 0
        for candidate_id in sorted(accepted_ids | safe_ids):
            revision = revisions.get(candidate_id)
            if revision is None:
                raise AuditError(
                    f"{root.name} accepted candidate has no candidate revision: {candidate_id}"
                )
            candidate_ref = revision.get("candidate_artifact")
            candidate_artifact_id = (
                candidate_ref.get("artifact_id")
                if isinstance(candidate_ref, Mapping)
                and isinstance(candidate_ref.get("artifact_id"), str)
                else None
            )
            if candidate_artifact_id is None:
                raise AuditError(
                    f"{root.name} candidate revision has no candidate artifact: {candidate_id}"
                )
            if (
                not isinstance(candidate_ref, Mapping)
                or candidate_ref.get("media_type") != _CANDIDATE
            ):
                raise AuditError(
                    f"{root.name} candidate revision points to the wrong media type: {candidate_id}"
                )
            candidate = candidates.get(candidate_artifact_id)
            if candidate is None:
                raise AuditError(
                    f"{root.name} candidate revision points to a missing candidate: {candidate_id}"
                )
            finding_id = _finding_id_from_candidate(candidate_id, tuple(findings))
            if finding_id is None:
                raise AuditError(
                    f"{root.name} selected candidate is not bound to a finding: {candidate_id}"
                )
            finding = findings[finding_id]
            expected_base_commit = finding.get("base_commit")
            if revision.get("base_commit") != expected_base_commit:
                raise AuditError(
                    f"{root.name} candidate revision base_commit does not match finding: "
                    f"{candidate_id}"
                )
            if revision.get("content_hash") != candidate_artifact_id:
                raise AuditError(
                    f"{root.name} candidate revision content_hash does not match candidate: "
                    f"{candidate_id}"
                )
            validation = validations.get(candidate_id)
            if validation is None:
                raise AuditError(
                    f"{root.name} selected candidate has no validation: {candidate_id}"
                )
            if validation.get("base_commit") != expected_base_commit:
                raise AuditError(
                    f"{root.name} validation base_commit does not match finding: {candidate_id}"
                )
            if validation.get("candidate_content_hash") != candidate_artifact_id:
                raise AuditError(
                    f"{root.name} validation candidate hash does not match candidate: "
                    f"{candidate_id}"
                )
            if validation.get("disposition") != "pass":
                raise AuditError(
                    f"{root.name} accepted candidate validation disposition is not pass: "
                    f"{candidate_id}"
                )
            selected_workflows = (
                accepted_workflows[candidate_id]
                if candidate_id in accepted_ids
                else safe_workflows[candidate_id]
            )
            for workflow in selected_workflows:
                if workflow.get("base_commit") != expected_base_commit:
                    raise AuditError(
                        f"{root.name} workflow base_commit does not match finding: {candidate_id}"
                    )
            is_safe_action = candidate_id in safe_ids
            root_accepted += int(not is_safe_action)
            root_safe += int(is_safe_action)
            unique_findings.add((root.name, finding_id))
            validated = isinstance(validation, Mapping) and validation.get("disposition") == "pass"
            if validated:
                root_validated += 1
                root_safe_validated += int(is_safe_action)
            group_rows[_boundary(finding)].append(
                {
                    "root": root.name,
                    "finding_id": finding_id,
                    "candidate_id": candidate_id,
                    "repair_owner": finding.get("repair_owner"),
                    "facet_suffixes": _facet_suffixes(finding),
                    "problem_identity": _problem_identity(finding),
                    "semantic_action": _semantic_action(candidate),
                    "validator_disposition": validation.get("disposition")
                    if isinstance(validation, Mapping)
                    else None,
                    "validated": validated,
                    "action_kind": (
                        "validation_only_safe" if is_safe_action else "canonical_commit"
                    ),
                }
            )
        accepted_candidates += root_accepted
        safe_action_candidates += root_safe
        accepted_validated_candidates += root_validated
        safe_action_validated_candidates += root_safe_validated
        root_summaries.append(
            {
                "root": root.name,
                "path": str(root.resolve()),
                "artifact_count": len(artifacts),
                "finding_count": len(findings),
                "accepted_candidate_count": root_accepted,
                "safe_action_candidate_count": root_safe,
                "validator_pass_count": root_validated,
                "safe_action_validator_pass_count": root_safe_validated,
            }
        )

    groups: list[dict[str, object]] = []
    route_ambiguity_groups = 0
    facet_discriminated_groups = 0
    semantic_variant_groups = 0
    same_finding_multi_action_groups = 0
    same_problem_multi_action_groups = 0
    safe_action_variant_groups = 0
    same_problem_safe_action_variant_groups = 0
    for boundary, rows in sorted(group_rows.items(), key=lambda item: repr(item[0])):
        validated_rows = [row for row in rows if row["validated"] is True]
        owners = sorted({str(row["repair_owner"]) for row in validated_rows})
        action_signatures = {
            json.dumps(row["semantic_action"], ensure_ascii=False, sort_keys=True)
            for row in validated_rows
        }
        finding_ids = sorted({str(row["finding_id"]) for row in validated_rows})
        facet_sets = {
            json.dumps(row["facet_suffixes"], ensure_ascii=False, sort_keys=True)
            for row in validated_rows
        }
        owner_variance = len(owners) > 1
        semantic_variance = len(action_signatures) > 1
        facet_discriminated = owner_variance and len(facet_sets) > 1
        same_finding_actions = any(
            len(
                {
                    json.dumps(row["semantic_action"], ensure_ascii=False, sort_keys=True)
                    for row in validated_rows
                    if row["finding_id"] == finding_id
                }
            )
            > 1
            for finding_id in finding_ids
        )
        problem_actions: dict[str, set[str]] = defaultdict(set)
        for row in validated_rows:
            problem_identity = row.get("problem_identity")
            if not isinstance(problem_identity, tuple):
                continue
            problem_key = json.dumps(problem_identity, ensure_ascii=False, sort_keys=True)
            action_key = json.dumps(row["semantic_action"], ensure_ascii=False, sort_keys=True)
            problem_actions[problem_key].add(action_key)
        same_problem_actions = any(len(actions) > 1 for actions in problem_actions.values())
        safe_action_signatures = {
            json.dumps(row["semantic_action"], ensure_ascii=False, sort_keys=True)
            for row in validated_rows
            if row["action_kind"] == "validation_only_safe"
        }
        safe_problem_actions: dict[str, set[str]] = defaultdict(set)
        for row in validated_rows:
            if row["action_kind"] != "validation_only_safe":
                continue
            problem_identity = row.get("problem_identity")
            if not isinstance(problem_identity, tuple):
                continue
            problem_key = json.dumps(problem_identity, ensure_ascii=False, sort_keys=True)
            action_key = json.dumps(row["semantic_action"], ensure_ascii=False, sort_keys=True)
            safe_problem_actions[problem_key].add(action_key)
        same_problem_safe_actions = any(
            len(actions) > 1 for actions in safe_problem_actions.values()
        )
        if owner_variance and facet_discriminated:
            facet_discriminated_groups += 1
        if semantic_variance:
            semantic_variant_groups += 1
        if same_finding_actions:
            same_finding_multi_action_groups += 1
        if same_problem_actions:
            same_problem_multi_action_groups += 1
        if len(safe_action_signatures) > 1:
            safe_action_variant_groups += 1
        if same_problem_safe_actions:
            same_problem_safe_action_variant_groups += 1
        if owner_variance and not facet_discriminated:
            route_ambiguity_groups += 1
        groups.append(
            {
                "boundary": boundary,
                "validated_candidate_count": len(validated_rows),
                "finding_ids": finding_ids,
                "repair_owners": owners,
                "facet_sets": [json.loads(item) for item in sorted(facet_sets)],
                "semantic_action_variant_count": len(action_signatures),
                "owner_variance": owner_variance,
                "facet_discriminated": facet_discriminated,
                "same_finding_multiple_semantic_actions": same_finding_actions,
                "problem_identity_group_count": len(problem_actions),
                "same_problem_multiple_semantic_actions": same_problem_actions,
                "safe_action_variant_count": len(safe_action_signatures),
                "same_problem_multiple_safe_actions": same_problem_safe_actions,
            }
        )

    return {
        "schema": "u8c-action-corpus-audit.v2",
        "status": "AUDITED",
        "scope": {
            "object_roots": [str(root.resolve()) for root in object_roots],
            "evaluator_feedback_included": False,
            "reasoner_invoked": False,
        },
        "census": {
            "object_root_count": len(object_roots),
            "unique_finding_count": len(unique_findings),
            "accepted_candidate_count": accepted_candidates,
            "validator_pass_candidate_count": accepted_validated_candidates,
            "safe_action_candidate_count": safe_action_candidates,
            "safe_action_validator_pass_count": safe_action_validated_candidates,
            "safety_boundary_group_count": len(groups),
            "semantic_variant_group_count": semantic_variant_groups,
            "owner_variance_group_count": sum(
                1 for group in groups if group["owner_variance"] is True
            ),
            "facet_discriminated_owner_variance_group_count": facet_discriminated_groups,
            "same_finding_multiple_semantic_action_group_count": same_finding_multi_action_groups,
            "same_problem_multiple_semantic_action_group_count": same_problem_multi_action_groups,
            "safe_action_variant_group_count": safe_action_variant_groups,
            "same_problem_multiple_safe_action_variant_group_count": (
                same_problem_safe_action_variant_groups
            ),
            "undiscriminated_route_ambiguity_group_count": route_ambiguity_groups,
        },
        "root_summaries": root_summaries,
        "groups": groups,
        "admission": {
            "decision": (
                "U8-C_TRIGGER_SHOWN"
                if (
                    route_ambiguity_groups > 0
                    or same_finding_multi_action_groups > 0
                    or same_problem_multi_action_groups > 0
                    or same_problem_safe_action_variant_groups > 0
                )
                else "U8-C_NOT_ADMITTED_KEEP_DETERMINISTIC_POLICY"
            ),
            "reasoner_enabled": False,
            "interpretation": {
                "semantic_candidate_variance_is_recovery_route_evidence": False,
                "owner_variance_requires_unresolved_receipt_state": True,
                "facet_or_finding_identity_is_a_discriminator": True,
                "problem_identity_is_a_discriminator": True,
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--object-root", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = build_corpus_report(tuple(args.object_root))
        _write_once(args.output, payload)
        print(
            json.dumps({"output": str(args.output.resolve()), "sha256": _sha256_file(args.output)})
        )
    except (AuditError, OSError, RuntimeError) as exc:
        print(f"U8-C action corpus audit failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
