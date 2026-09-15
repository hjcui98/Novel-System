"""Prepare a new V27 continuation from the project's current committed plan."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.creative_runtime import CreativeRunRequest
from novel_agent.domain.ids import RunId
from novel_agent.domain.world import PlanLevel
from novel_agent.services.commits import CommitService
from novel_agent.services.projection import DerivedSnapshotRepository


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.environ.get("NOVEL_DATABASE_URL"))
    parser.add_argument("--source-request", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("--database-url or NOVEL_DATABASE_URL is required")

    source = CreativeRunRequest.model_validate_json(args.source_request.read_bytes(), strict=True)
    factory = build_session_factory(build_engine(args.database_url))
    current_commit = CommitService(factory).current_commit(source.project_id)
    snapshot = DerivedSnapshotRepository(factory).get_for_commit(current_commit)
    if snapshot is None:
        raise SystemExit(f"no exact projection snapshot for current commit of {source.project_id}")
    author_inputs = tuple(
        ref for ref in source.input_artifact_refs if ref.media_type == "text/plain"
    )
    if not author_inputs:
        raise SystemExit("source request has no author text inputs")

    continuation = source.model_copy(
        update={
            "run_id": RunId(args.run_id),
            "basis_commit": current_commit,
            "basis_snapshot": snapshot.snapshot_id,
            "input_artifact_refs": author_inputs,
            "continuation_artifact_refs": (),
            "plan_level": PlanLevel.CHAPTER_SET,
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(continuation.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(
        {
            "run_id": continuation.run_id.root,
            "project_id": continuation.project_id.root,
            "plan_level": continuation.plan_level.value if continuation.plan_level else None,
            "author_input_count": len(author_inputs),
            "output": str(args.output),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
