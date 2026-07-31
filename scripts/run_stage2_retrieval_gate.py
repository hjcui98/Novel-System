#!/usr/bin/env python3
"""Fail closed unless selected Stage 2R checkpoints have real-hybrid retrieval receipts."""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import ParseResult, urlparse

from opensearchpy import OpenSearch
from sqlalchemy import select

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.models import CommitRow, ProjectRow
from novel_agent.domain.gates import (
    Stage2RetrievalCheckpointEvidence,
    Stage2RetrievalGateR1Counts,
    Stage2RetrievalGateReport,
)
from novel_agent.domain.ids import CommitId, ProjectId
from novel_agent.domain.retrieval_routing import L2IndexKind, RetrievalBackendProfile
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedSnapshotRepository,
)
from novel_agent.services.r1 import R1WorldRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-directory", required=True, type=Path)
    parser.add_argument("--project-id")
    parser.add_argument("--database-url")
    parser.add_argument("--opensearch-url", default="http://127.0.0.1:9200")
    parser.add_argument("--checkpoints", default="20,40,60,80,95")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    checkpoints = _checkpoints(args.checkpoints)
    project_directory = args.project_directory.resolve()
    database_url = args.database_url or (
        f"sqlite:///{(project_directory / 'project.sqlite3').resolve()}"
    )
    search_target = _loopback_url(args.opensearch_url)
    engine = build_engine(database_url)
    client: OpenSearch | None = None
    try:
        factory = build_session_factory(engine)
        project_id = _project_id(factory, args.project_id)
        loader = ArtifactProjectionSourceLoader(
            CommitService(factory),
            ArtifactRepository(FilesystemObjectStore(project_directory / "objects")),
        )
        selected = _checkpoint_commits(factory, loader, project_id, checkpoints)
        client = OpenSearch(
            hosts=[{"host": search_target.hostname, "port": search_target.port}],
            use_ssl=search_target.scheme == "https",
            verify_certs=search_target.scheme == "https",
        )
        if not client.ping():
            raise RuntimeError("OpenSearch is unavailable")
        search = OpenSearchIndex(client)
        snapshots = DerivedSnapshotRepository(factory)
        r1 = R1WorldRepository(factory)
        entries = tuple(
            _gate_entry(checkpoint, commit, snapshots, r1, search)
            for checkpoint, commit in selected.items()
        )
        passed = bool(entries) and all(entry.passed for entry in entries)
        report = Stage2RetrievalGateReport(
            status="passed" if passed else "failed",
            project_id=project_id,
            retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
            checkpoints=entries,
        )
        payload = report.model_dump_json(indent=2) + "\n"
        if args.output is None:
            print(payload, end="")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, "utf-8")
        return 0 if passed else 2
    finally:
        if client is not None:
            client.close()
        engine.dispose()


def _gate_entry(
    checkpoint: int,
    source_commit: CommitId,
    snapshots: DerivedSnapshotRepository,
    r1: R1WorldRepository,
    search: OpenSearchIndex,
) -> Stage2RetrievalCheckpointEvidence:
    failures: list[str] = []
    snapshot = snapshots.get_for_commit(source_commit)
    attestation = snapshots.get_attestation_for_commit(source_commit)
    if snapshot is None:
        failures.append("derived_snapshot_missing")
    elif snapshot.retrieval_backend_profile != "real_hybrid":
        failures.append("retrieval_backend_not_real_hybrid")
    if attestation is None:
        failures.append("projection_attestation_missing")
    elif not attestation.quality_eligible:
        failures.append("projection_attestation_not_quality_eligible")
    r1_counts = r1.counts(source_commit)
    index_totals: dict[L2IndexKind, int] = {}
    index_targets: dict[L2IndexKind, str] = {}
    if attestation is not None:
        if r1_counts[0] != attestation.r1_record_count:
            failures.append("r1_record_count_mismatch")
        if r1_counts[1] != attestation.r1_entity_association_count:
            failures.append("r1_entity_association_count_mismatch")
        if r1_counts[2] != attestation.graph_edge_count:
            failures.append("graph_edge_count_mismatch")
        for index in attestation.indexes:
            # An alias is intentionally advanced as newer snapshots publish.  A
            # historical checkpoint gate must query the immutable physical index
            # recorded by that checkpoint's attestation, matching runtime
            # retrieval and retention semantics.
            index_targets[index.index_kind] = index.physical_name
            try:
                _, total = search.search_with_total(
                    index.physical_name,
                    {"match_all": {}},
                    size=1,
                )
                index_totals[index.index_kind] = total
                if total != index.document_count:
                    failures.append(f"{index.index_kind.value}_index_count_mismatch")
            except Exception as error:
                failures.append(
                    f"{index.index_kind.value}_sample_query_failed:{type(error).__name__}"
                )
    return Stage2RetrievalCheckpointEvidence(
        checkpoint=checkpoint,
        source_commit=source_commit,
        snapshot_id=None if snapshot is None else snapshot.snapshot_id,
        r1_counts=Stage2RetrievalGateR1Counts(
            records=r1_counts[0],
            entity_associations=r1_counts[1],
            relation_edges=r1_counts[2],
        ),
        index_targets=index_targets,
        index_totals=index_totals,
        failures=tuple(failures),
        passed=not failures,
    )


def _checkpoint_commits(
    factory: object,
    loader: ArtifactProjectionSourceLoader,
    project_id: ProjectId,
    checkpoints: tuple[int, ...],
) -> dict[int, CommitId]:
    with factory() as session:  # type: ignore[operator]
        commit_ids = tuple(
            session.scalars(
                select(CommitRow.commit_id)
                .where(CommitRow.project_id == project_id.root)
                .order_by(CommitRow.created_at, CommitRow.commit_id)
            )
        )
    by_chapter: dict[int, CommitId] = {}
    for raw in commit_ids:
        commit = CommitId(raw)
        source = loader.load(commit)
        by_chapter[len(source.text.chapters)] = commit
    missing = tuple(checkpoint for checkpoint in checkpoints if checkpoint not in by_chapter)
    if missing:
        raise RuntimeError(f"checkpoint commits are missing: {list(missing)}")
    return {checkpoint: by_chapter[checkpoint] for checkpoint in checkpoints}


def _project_id(factory: object, requested: str | None) -> ProjectId:
    with factory() as session:  # type: ignore[operator]
        values = tuple(
            session.scalars(select(ProjectRow.project_id).order_by(ProjectRow.project_id))
        )
    if requested is not None:
        value = ProjectId(requested)
        if value.root not in values:
            raise RuntimeError(f"project is not present: {value.root}")
        return value
    if len(values) != 1:
        raise RuntimeError("--project-id is required when the database has multiple projects")
    return ProjectId(values[0])


def _checkpoints(raw: str) -> tuple[int, ...]:
    try:
        values = tuple(int(value) for value in raw.split(",") if value)
    except ValueError as error:
        raise ValueError(
            "--checkpoints must be a comma-separated list of positive integers"
        ) from error
    if not values or any(value < 1 for value in values) or len(values) != len(set(values)):
        raise ValueError("--checkpoints must contain unique positive integers")
    return values


def _loopback_url(value: str) -> ParseResult:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.path not in {"", "/"}
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("--opensearch-url must be a bare loopback HTTP(S) origin")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
