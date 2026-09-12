"""Origin-preserving views of the artifacts actually supplied to planning agents."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.services.artifacts import ArtifactRepository


def trusted_sections(
    artifacts: ArtifactRepository,
    refs: tuple[ArtifactRef, ...],
) -> Iterator[tuple[str, dict[str, Any], tuple[ArtifactRef, ...]]]:
    for ref in refs:
        media = ref.media_type
        if not any(
            name in media
            for name in (
                "planning-basis+json",
                "planner-context-package+json",
                "agent-context-view+json",
                "plan-root+json",
                "plan-review+json",
                "editor-plan-feedback+json",
            )
        ):
            continue
        payload = json.loads(artifacts.read_verified(ref))
        if "plan-root+json" in media:
            yield "accepted_plan", payload, (ref,)
            continue
        if "plan-review+json" in media or "editor-plan-feedback+json" in media:
            yield "revision_feedback", payload, (ref,)
            continue
        rows = (
            payload.get("planning_basis", [])
            if "planning-basis+json" in media
            else payload.get("items", [])
            if "planner-context-package+json" in media
            else [*payload.get("protected_items", []), *payload.get("active_memory_items", [])]
        )
        for row in rows:
            section = row.get("section", row.get("kind", ""))
            sources = tuple(
                ArtifactRef.model_validate_json(json.dumps(value))
                for value in row.get("source_artifact_refs", [])
            )
            try:
                content = json.loads(row.get("text", row.get("content", "")))
            except (json.JSONDecodeError, TypeError):
                content = {}
            if isinstance(content, dict):
                yield section, content, sources


def reference_ids(payload: object) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.endswith("_id") and isinstance(value, str):
                found.add(value)
            if isinstance(value, (list, dict)):
                found.update(reference_ids(value))
    elif isinstance(payload, list):
        for value in payload:
            found.update(reference_ids(value))
    return found


def accepted_plan_items(
    artifacts: ArtifactRepository,
    refs: tuple[ArtifactRef, ...],
) -> list[dict[str, Any]]:
    result = []
    for section, payload, _sources in trusted_sections(artifacts, refs):
        if section != "accepted_plan":
            continue
        for node in payload.get("nodes", payload.get("relevant_plan_nodes", [])):
            result.append(
                {"item_id": node["plan_node_id"], "kind": node["node_type"], "payload": node}
            )
        for obligation in payload.get("obligations", payload.get("future_locked_obligations", [])):
            if isinstance(obligation, dict) and "obligation_id" in obligation:
                result.append(
                    {
                        "item_id": obligation["obligation_id"],
                        "kind": obligation.get("kind"),
                        "payload": {**obligation, "obligation_kind": obligation.get("kind")},
                    }
                )
    return result
