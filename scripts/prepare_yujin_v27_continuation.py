"""Prepare a new V27 continuation from the project's current committed plan."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from sqlalchemy.engine import URL

from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.creative_runtime import CreativeRunPolicy, CreativeRunRequest
from novel_agent.domain.ids import RunId
from novel_agent.domain.runtime import TaskKind
from novel_agent.domain.world import PlanLevel
from novel_agent.services.commits import CommitService
from novel_agent.services.projection import DerivedSnapshotRepository


def database_url_from_env_file(path: Path) -> str:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key] = value
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_PORT", "POSTGRES_DB")
    missing = tuple(key for key in required if not values.get(key))
    if missing:
        raise SystemExit("database env file is missing: " + ", ".join(missing))
    return URL.create(
        "postgresql+psycopg",
        username=values["POSTGRES_USER"],
        password=values["POSTGRES_PASSWORD"],
        host="127.0.0.1",
        port=int(values["POSTGRES_PORT"]),
        database=values["POSTGRES_DB"],
    ).render_as_string(hide_password=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.environ.get("NOVEL_DATABASE_URL"))
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--source-request", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--initial-task-kind",
        choices=["draft_candidate", "plan_candidate"],
        default="draft_candidate",
    )
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database_url = args.database_url
    if database_url is None and args.env_file is not None:
        database_url = database_url_from_env_file(args.env_file)
    if not database_url:
        raise SystemExit("--database-url, --env-file, or NOVEL_DATABASE_URL is required")

    source = CreativeRunRequest.model_validate_json(args.source_request.read_bytes(), strict=True)
    factory = build_session_factory(build_engine(database_url))
    current_commit = CommitService(factory).current_commit(source.project_id)
    snapshot = DerivedSnapshotRepository(factory).get_for_commit(current_commit)
    if snapshot is None:
        raise SystemExit(f"no exact projection snapshot for current commit of {source.project_id}")
    author_inputs = tuple(
        ref for ref in source.input_artifact_refs if ref.media_type == "text/plain"
    )
    if not author_inputs:
        raise SystemExit("source request has no author text inputs")

    initial_kind = (
        TaskKind.DRAFT_CANDIDATE
        if args.initial_task_kind == "draft_candidate"
        else TaskKind.PLAN_CANDIDATE
    )
    plan_level = None if initial_kind is TaskKind.DRAFT_CANDIDATE else PlanLevel.CHAPTER_SET
    policy = (
        CreativeRunPolicy.model_validate_json(args.policy.read_bytes())
        if args.policy is not None
        else source.policy
    )
    continuation = source.model_copy(
        update={
            "run_id": RunId(args.run_id),
            "basis_commit": current_commit,
            "basis_snapshot": snapshot.snapshot_id,
            "input_artifact_refs": author_inputs,
            "continuation_artifact_refs": (),
            "plan_level": plan_level,
            "initial_task_kind": initial_kind,
            "policy": policy,
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(continuation.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(
        {
            "run_id": continuation.run_id.root,
            "project_id": continuation.project_id.root,
            "initial_task_kind": continuation.initial_task_kind.value,
            "plan_level": continuation.plan_level.value if continuation.plan_level else None,
            "author_input_count": len(author_inputs),
            "output": str(args.output),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
