#!/usr/bin/env python3
"""Create a fresh C20 basis and request for the U6-B production baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from prepare_u5_c20_isolated_basis import (
    copy_canonical_roots,
    database_descriptor,
    isolated_manifest,
)

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.artifacts import RootManifest
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
)
from novel_agent.domain.ids import CommitId, ProjectId, RunId, SchemaVersion
from novel_agent.domain.stage2 import ReferenceRootDocument, SourceClass
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.projection import snapshot_id_for_commit

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = SchemaVersion("1.0.0")


def _write_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"U6-B refuses to overwrite preparation output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def prepare(
    *,
    source_database_url: str,
    source_project_id: ProjectId,
    source_commit: CommitId,
    source_object_root: Path,
    destination_database_url: str,
    destination_project_id: ProjectId,
    destination_object_root: Path,
    run_id: RunId,
) -> tuple[dict[str, object], CreativeRunRequest]:
    source_engine = build_engine(source_database_url)
    destination_engine = build_engine(destination_database_url)
    try:
        source_commits = CommitService(build_session_factory(source_engine))
        source_manifest = source_commits.load_manifest(source_commit)
        if source_manifest.project_id != source_project_id:
            raise RuntimeError("U6-B source Commit belongs to another project")
        source_artifacts = ArtifactRepository(FilesystemObjectStore(source_object_root))
        source_text = TextRootDocument.model_validate_json(
            source_artifacts.read_verified(source_manifest.text_root), strict=True
        )
        if not source_text.chapters or source_text.chapters[-1].chapter_index != 20:
            raise RuntimeError("U6-B source basis must end at C20")
        reference = ReferenceRootDocument.model_validate_json(
            source_artifacts.read_verified(source_manifest.reference_root), strict=True
        )
        author_assets = tuple(
            asset.artifact
            for asset in reference.assets
            if asset.source_class is SourceClass.AUTHOR_INITIAL_BRIEF
        )
        if not author_assets:
            raise RuntimeError("U6-B source basis lacks the frozen author initial brief")

        destination_object_root.mkdir(parents=True, exist_ok=True)
        destination_artifacts = ArtifactRepository(FilesystemObjectStore(destination_object_root))
        copy_canonical_roots(source_artifacts, destination_artifacts, source_manifest)
        destination_manifest: RootManifest = isolated_manifest(
            source_manifest, destination_project_id
        )
        destination_commits = CommitService(build_session_factory(destination_engine))
        basis = destination_commits.initialize_project(destination_manifest)
        loaded = destination_commits.load_manifest(basis)
        loaded_text = TextRootDocument.model_validate_json(
            destination_artifacts.read_verified(loaded.text_root), strict=True
        )
        if loaded_text.chapters[-1].chapter_index != 20:
            raise RuntimeError("U6-B isolated basis does not end at C20")
        runtime_manifest = load_stage5_manifest(
            ROOT / "src" / "novel_agent" / "runtime" / "stage5_development_manifest.json"
        )
        policy_hash = runtime_manifest.configuration_fingerprint
        request = CreativeRunRequest(
            run_id=run_id,
            project_id=destination_project_id,
            basis_commit=basis,
            basis_snapshot=snapshot_id_for_commit(basis),
            policy=CreativeRunPolicy(
                automation_mode=AutomationMode.AUTO,
                policy_hash=policy_hash,
                permission_hash=policy_hash,
                auto_accept_plan=True,
                auto_accept_draft=True,
                max_task_attempts=3,
                max_tasks_per_advance=1,
                planning_horizon=5,
                runtime_parallelism=1,
                enable_planner_lookahead=False,
            ),
            current_chapter=20,
            target_chapters=40,
            input_artifact_refs=author_assets[:1],
        )
        receipt = {
            "schema": "u6b-production-basis.v1",
            "source_project_id": source_project_id.root,
            "source_commit": source_commit.root,
            "isolated_project_id": destination_project_id.root,
            "isolated_basis_commit": basis.root,
            "history_last_chapter": loaded_text.chapters[-1].chapter_index,
            "author_initial_brief": author_assets[0].artifact_id.root,
            "source_object_store_root": str(source_object_root.resolve()),
            "isolated_object_store_root": str(destination_object_root.resolve()),
            "isolated_database_descriptor": database_descriptor(destination_database_url),
            "request_id": request.run_id.root,
        }
        return receipt, request
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
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    receipt, request = prepare(
        source_database_url=args.source_database_url,
        source_project_id=ProjectId(args.source_project_id),
        source_commit=CommitId(args.source_commit),
        source_object_root=args.source_object_root,
        destination_database_url=args.destination_database_url,
        destination_project_id=ProjectId(args.destination_project_id),
        destination_object_root=args.destination_object_root,
        run_id=RunId(args.run_id),
    )
    _write_once(args.receipt, receipt)
    _write_once(args.request, request.model_dump(mode="json"))
    print(
        json.dumps(
            {"receipt": receipt, "request": request.model_dump(mode="json")},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
