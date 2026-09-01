#!/usr/bin/env python3
"""Prepare an isolated production project from the accepted C20 root basis.

The source project and source Commit remain read-only.  The destination uses
the same immutable five-root content under a fresh project identity and a
parentless evaluation Commit so the production runtime can start at C20
without rewinding the source project's C95 head.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.artifacts import RootManifest
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import CommitId, ProjectId
from novel_agent.domain.stage2 import ReferenceRootDocument
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService

CANONICAL_ROOT_NAMES = (
    "text_root",
    "plan_root",
    "world_root",
    "reference_root",
    "project_profile_root",
)
SCHEMA = "u5-c20-isolated-basis.v1"


def copy_canonical_roots(
    source: ArtifactRepository,
    destination: ArtifactRepository,
    manifest: RootManifest,
) -> None:
    for name in CANONICAL_ROOT_NAMES:
        reference = getattr(manifest, name)
        copied = destination.put(
            source.read_verified(reference), reference.media_type, reference.schema_version
        )
        if (
            copied.artifact_id != reference.artifact_id
            or copied.media_type != reference.media_type
            or copied.byte_length != reference.byte_length
            or copied.schema_version != reference.schema_version
        ):
            raise RuntimeError(f"canonical root identity changed while copying {name}")
    reference_root = ReferenceRootDocument.model_validate_json(
        source.read_verified(manifest.reference_root), strict=True
    )
    for asset in reference_root.assets:
        copied = destination.put(
            source.read_verified(asset.artifact),
            asset.artifact.media_type,
            asset.artifact.schema_version,
        )
        if (
            copied.artifact_id != asset.artifact.artifact_id
            or copied.media_type != asset.artifact.media_type
            or copied.byte_length != asset.artifact.byte_length
            or copied.schema_version != asset.artifact.schema_version
        ):
            raise RuntimeError(f"reference asset identity changed while copying {asset.asset_id}")


def isolated_manifest(manifest: RootManifest, project_id: ProjectId) -> RootManifest:
    return manifest.model_copy(update={"project_id": project_id, "parent_commit_ids": ()})


def database_descriptor(database_url: str) -> str:
    """Keep host/database identity while excluding credentials from evidence."""

    parsed = urlsplit(database_url)
    if parsed.hostname is None:
        return database_url
    authority = parsed.hostname
    if parsed.port is not None:
        authority = f"{authority}:{parsed.port}"
    return f"{parsed.scheme}://{authority}{parsed.path}"


def prepare_basis(
    *,
    source_database_url: str,
    source_project_id: ProjectId,
    source_commit: CommitId,
    source_object_root: Path,
    destination_database_url: str,
    destination_project_id: ProjectId,
    destination_object_root: Path,
) -> dict[str, object]:
    source_engine = build_engine(source_database_url)
    destination_engine = build_engine(destination_database_url)
    try:
        source_commits = CommitService(build_session_factory(source_engine))
        source_current = source_commits.current_commit(source_project_id)
        source_manifest = source_commits.load_manifest(source_commit)
        if source_manifest.project_id != source_project_id:
            raise RuntimeError("source Commit belongs to a different project")
        source_artifacts = ArtifactRepository(FilesystemObjectStore(source_object_root))
        source_text = TextRootDocument.model_validate_json(
            source_artifacts.read_verified(source_manifest.text_root)
        )
        if not source_text.chapters or source_text.chapters[-1].chapter_index != 20:
            raise RuntimeError("source accepted basis is not exactly C20")

        destination_object_root.mkdir(parents=True, exist_ok=True)
        destination_artifacts = ArtifactRepository(FilesystemObjectStore(destination_object_root))
        copy_canonical_roots(source_artifacts, destination_artifacts, source_manifest)

        destination_manifest = isolated_manifest(source_manifest, destination_project_id)
        destination_commits = CommitService(build_session_factory(destination_engine))
        destination_commit = destination_commits.initialize_project(destination_manifest)
        if destination_commits.current_commit(destination_project_id) != destination_commit:
            raise RuntimeError("isolated project current Commit did not settle to its basis")
        loaded = destination_commits.load_manifest(destination_commit)
        loaded_text = TextRootDocument.model_validate_json(
            destination_artifacts.read_verified(loaded.text_root)
        )
        if loaded_text.chapters[-1].chapter_index != 20:
            raise RuntimeError("isolated basis does not end at C20")
        return {
            "schema": SCHEMA,
            "source_project_id": source_project_id.root,
            "source_commit": source_commit.root,
            "source_current_commit": source_current.root,
            "source_is_current": source_current == source_commit,
            "isolated_project_id": destination_project_id.root,
            "isolated_basis_commit": destination_commit.root,
            "history_last_chapter": loaded_text.chapters[-1].chapter_index,
            "canonical_root_artifacts": {
                name: getattr(loaded, name).artifact_id.root for name in CANONICAL_ROOT_NAMES
            },
            "source_object_store_root": str(source_object_root.resolve()),
            "isolated_object_store_root": str(destination_object_root.resolve()),
            "isolated_database_descriptor": database_descriptor(destination_database_url),
        }
    finally:
        source_engine.dispose()
        destination_engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-database-url", required=True)
    parser.add_argument("--source-project-id", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-object-root", type=Path, required=True)
    parser.add_argument("--destination-database-url", required=True)
    parser.add_argument("--destination-project-id", required=True)
    parser.add_argument("--destination-object-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.output.exists():
        raise RuntimeError(f"refusing to overwrite basis receipt: {args.output}")
    receipt = prepare_basis(
        source_database_url=args.source_database_url,
        source_project_id=ProjectId(args.source_project_id),
        source_commit=CommitId(args.source_commit),
        source_object_root=args.source_object_root,
        destination_database_url=args.destination_database_url,
        destination_project_id=ProjectId(args.destination_project_id),
        destination_object_root=args.destination_object_root,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
