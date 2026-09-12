#!/usr/bin/env python3
"""A02 evidence: trace the frozen v6 eight-volume responsibility table.

Reads the real v6 ARC_VOLUME PlanProposal from the read-only object store and runs
the current integration binder over it.  No model call, no commit, no write to the
frozen run: the object store is only read, and the materializer's artifact sink is
an in-memory recorder.

Usage:
    PYTHONPATH=<integration worktree>/src python3 scripts/audit_v6_responsibility_binding.py \
        --object-store /home/cuihengjia/agent/novel/NS/yujin-jiuxu-v6/objects \
        --proposal sha256:362b7626fe852428176b509dc1674cd281a802a04fceafc1c548f7cfe9152475 \
        --world sha256:6912843c3ae9dfaa9303ad4d4a1c86ea8db793b67e45d610603cf49495c3dd0d
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.runtime.materializers import PlanCandidateMaterializer
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import SchemaVersion
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.stage2 import (
    PlanProposal,
    ProposalProvenance,
)
from novel_agent.services.artifacts import ArtifactRepository

VERSION = SchemaVersion("1.0.0")
ARC_MEDIA_TYPE = "application/vnd.novel-agent.plan-proposal+json"
WORLD_MEDIA_TYPE = "application/vnd.novel-agent.world-root+json"


class _RecordingArtifacts(ArtifactRepository):
    """Artifact repository double that never writes to the frozen store."""

    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put(self, payload: bytes, media_type: str, schema_version: SchemaVersion) -> ArtifactRef:
        self.puts.append(
            {
                "media_type": media_type,
                "byte_length": len(payload),
                "schema_version": schema_version.root,
            }
        )
        return ArtifactRef(
            artifact_id="sha256:" + "0" * 64,
            media_type=media_type,
            byte_length=len(payload),
            schema_version=schema_version,
        )


def _read_bytes(root: Path, artifact_id: str) -> bytes:
    digest = artifact_id.removeprefix("sha256:")
    return (root / "sha256" / digest[:2] / digest).read_bytes()


def _read_json(root: Path, artifact_id: str) -> dict[str, Any]:
    return json.loads(_read_bytes(root, artifact_id).decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--object-store", type=Path, required=True)
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--world", required=True)
    args = parser.parse_args(argv)

    FilesystemObjectStore(args.object_store)
    proposal_payload = _read_json(args.object_store, args.proposal)

    world = WorldRootDocument.model_validate_json(_read_bytes(args.object_store, args.world))
    proposal = PlanProposal.model_validate(
        {
            **proposal_payload,
            "items": [
                {**item, "provenance": ProposalProvenance.PLANNER_PROPOSED}
                for item in proposal_payload["items"]
            ],
        },
        strict=False,
    )
    print(f"candidate mode            : {proposal.mode.value}")
    print(f"candidate items           : {len(proposal.items)}")
    print(f"frozen world obligations  : {len(world.obligations)}")
    print(f"frozen world entities     : {len(world.entities)}")

    declared = 0
    for item in proposal.items:
        plan = item.payload.get("obligation_plan")
        if isinstance(plan, list):
            declared += len(plan)
    print(f"responsibilities declared : {declared}")

    artifacts = _RecordingArtifacts()
    materializer = PlanCandidateMaterializer(artifacts, object(), schema_version=VERSION)
    bound_world, world_ref, bindings = materializer._bind_obligation_declarations(
        world, proposal
    )

    print(f"compiled obligations      : {len(bound_world.obligations)}")
    print(f"world ref written         : {world_ref is not None}")
    print(f"plan items with bindings  : {len(bindings)}")
    print(f"artifact sink writes      : {len(artifacts.puts)}")
    print("--- compiled responsibility map ---")
    untraced: list[str] = []
    for item in proposal.items:
        plan = item.payload.get("obligation_plan")
        if not isinstance(plan, list):
            continue
        bound_ids = bindings.get(item.item_id, ())
        print(f"{item.item_id.root}: declared={len(plan)} bound={len(bound_ids)}")
        if len(plan) != len(bound_ids):
            untraced.append(item.item_id.root)
        for obligation_id, entry in zip(bound_ids, plan, strict=False):
            match = next(
                (item for item in bound_world.obligations if item.obligation_id == obligation_id),
                None,
            )
            description = match.description if match is not None else "<unbound>"
            print(
                f"  {obligation_id.root}  kind={(match.kind.value if match else '?')}"
                f"  not_before={(match.not_before_chapter if match else '?')}"
                f"  source={entry.get('summary')!r} -> {description!r}"
            )
    print("--- result ---")
    if untraced:
        print(f"UNTRACED ITEMS: {untraced}")
        return 2
    complete = len(bound_world.obligations) == declared
    print(f"every declared responsibility compiled: {complete}")
    return 0 if complete else 3


if __name__ == "__main__":
    sys.exit(main())
