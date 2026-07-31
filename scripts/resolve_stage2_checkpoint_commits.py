#!/usr/bin/env python3
"""Resolve immutable historical checkpoint commits from a completed project."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import CommitId
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--project-directory", type=Path, required=True)
    value.add_argument("--database-url", required=True)
    value.add_argument("--checkpoints", default="20,40,60,80,95")
    return value


def main() -> int:
    args = parser().parse_args()
    progress_path = args.project_directory / "progress_manifest.json"
    progress = json.loads(progress_path.read_text("utf-8"))
    head_chapter = int(progress["last_accepted_chapter"])
    head = CommitId(progress["last_accepted_commit"])
    checkpoints = tuple(
        sorted({int(raw.strip()) for raw in args.checkpoints.split(",") if raw.strip()})
    )
    if not checkpoints or checkpoints[0] < 0 or checkpoints[-1] > head_chapter:
        raise SystemExit(f"checkpoints must be between 0 and completed chapter C{head_chapter}")

    engine = build_engine(args.database_url)
    try:
        commits = CommitService(build_session_factory(engine))
        artifacts = ArtifactRepository(FilesystemObjectStore(args.project_directory / "objects"))
        cursor = head
        by_chapter: dict[int, str] = {}
        visited: set[CommitId] = set()
        while cursor not in visited:
            visited.add(cursor)
            manifest = commits.load_manifest(cursor)
            text = TextRootDocument.model_validate_json(artifacts.read_verified(manifest.text_root))
            chapter = len(text.chapters)
            if chapter in checkpoints:
                existing = by_chapter.setdefault(chapter, cursor.root)
                if existing != cursor.root:
                    raise SystemExit(f"multiple canonical commits represent requested C{chapter}")
            if not manifest.parent_commit_ids:
                break
            if len(manifest.parent_commit_ids) != 1:
                raise SystemExit(f"commit chain is not linear at C{chapter}: {cursor.root}")
            cursor = manifest.parent_commit_ids[0]
    finally:
        engine.dispose()

    missing = [chapter for chapter in checkpoints if chapter not in by_chapter]
    if missing:
        raise SystemExit(f"could not resolve checkpoint commits: {missing}")
    print(
        json.dumps(
            {str(chapter): by_chapter[chapter] for chapter in checkpoints},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
