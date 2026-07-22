#!/usr/bin/env python3
"""Locate the first Canonical → R1/L2 → candidate loss for one Stage 2R case."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from opensearchpy import OpenSearch
from sqlalchemy import select

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.models import CommitRow, ProjectRow
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.domain.memory import Stage1MemoryNeed
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedSnapshotRepository,
)
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.stage1_benchmark import OracleGoldNeedGenerator, Stage1NeedGenerator


def _need_diagnostic(
    need: Stage1MemoryNeed,
    case: Any,
    commit: CommitId,
    text_root_hash: Any,
    snapshot: Any,
    attestation: Any,
    r1: R1WorldRepository,
    search: OpenSearchIndex,
    *,
    oracle: bool,
) -> dict[str, object]:
    gold = next(
        (
            item
            for item in (
                *case.observed_use_gold,
                *case.operational_constraint_gold,
                *case.plan_obligation_gold,
            )
            if need.need_id == StableId(f"need.oracle.{item.gold_id.root}")
        ),
        None,
    )
    evidence_ids = tuple(item.evidence_id for item in gold.evidence_refs) if gold else ()
    canonical_present = (
        all(item.root_hash == text_root_hash for item in gold.evidence_refs)
        if gold is not None and gold.evidence_refs
        else bool(gold is not None and gold.plan_evidence_refs)
        if gold is not None
        else True
    )
    r1_matches = {
        record.record_id
        for evidence_id in evidence_ids
        for record in r1.records_for_evidence(commit, evidence_id)
    }
    l2_evidence_present = not evidence_ids
    candidate_count = 0
    selected_evidence_ids: set[str] = set()
    sample_error: str | None = None
    if attestation is not None and snapshot is not None:
        for manifest in attestation.indexes:
            filters: list[dict[str, object]] = [
                {"term": {"source_commit": commit.root}},
                {"term": {"snapshot_id": snapshot.snapshot_id.root}},
                {"terms": {"access_scope": [need.access_scope]}},
            ]
            if not need.allow_plan:
                filters.append({"term": {"information_label": "observed"}})
            try:
                hits, total = search.search_with_total(
                    manifest.alias,
                    {
                        "bool": {
                            "must": [{"match": {"text.standard": need.query_text}}],
                            "filter": filters,
                        }
                    },
                    size=20,
                )
                candidate_count += total
                for hit in hits:
                    source = hit.get("_source")
                    if isinstance(source, dict):
                        raw_ids = source.get("evidence_ids", [])
                        if isinstance(raw_ids, list):
                            selected_evidence_ids.update(
                                item for item in raw_ids if isinstance(item, str)
                            )
                if evidence_ids:
                    _, evidence_total = search.search_with_total(
                        manifest.alias,
                        {
                            "bool": {
                                "must": [
                                    {
                                        "terms": {
                                            "evidence_ids": [item.root for item in evidence_ids]
                                        }
                                    }
                                ],
                                "filter": filters,
                            }
                        },
                        size=1,
                    )
                    l2_evidence_present = l2_evidence_present or evidence_total > 0
            except Exception as error:
                sample_error = type(error).__name__
                break
    candidate_evidence_present = (
        bool({item.root for item in evidence_ids} & selected_evidence_ids)
        if evidence_ids
        else candidate_count > 0
    )
    stages = {
        "canonical": canonical_present,
        "r1": bool(r1_matches) if evidence_ids else None,
        "l2": l2_evidence_present,
        "candidate": candidate_evidence_present,
        "selection": candidate_evidence_present,
    }
    first_loss = next(
        (name for name, present in stages.items() if present is False),
        "l2_sample_query" if sample_error else None,
    )
    return {
        "need": need.model_dump(mode="json"),
        "gold_id": None if gold is None else gold.gold_id.root,
        "oracle": oracle,
        "evidence_ids": [item.root for item in evidence_ids],
        "r1_record_ids": sorted(item.root for item in r1_matches),
        "candidate_count": candidate_count,
        "sample_query_error": sample_error,
        "stages": stages,
        "first_loss": first_loss,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--project-directory", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--checkpoint", type=int, required=True)
    parser.add_argument("--query-condition", choices=("oracle", "generated"), default="oracle")
    parser.add_argument("--project-id")
    parser.add_argument("--database-url")
    parser.add_argument("--opensearch-url", default="http://127.0.0.1:9200")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.checkpoint < 1:
        raise ValueError("--checkpoint must be positive")
    bundle = HumanBenchmarkCompiler().compile(args.source)
    case = next(
        (item for item in bundle.case_manifests if item.case_id == StableId(args.case_id)),
        None,
    )
    if case is None:
        raise ValueError(f"case is not present in source bundle: {args.case_id}")
    directory = args.project_directory.resolve()
    database_url = args.database_url or f"sqlite:///{(directory / 'project.sqlite3').resolve()}"
    target = _loopback_url(args.opensearch_url)
    engine = build_engine(database_url)
    client: OpenSearch | None = None
    try:
        factory = build_session_factory(engine)
        project_id = _project_id(factory, args.project_id)
        loader = ArtifactProjectionSourceLoader(
            CommitService(factory),
            ArtifactRepository(FilesystemObjectStore(directory / "objects")),
        )
        commit = _commit_at_checkpoint(factory, loader, project_id, args.checkpoint)
        source = loader.load(commit)
        snapshot_repo = DerivedSnapshotRepository(factory)
        snapshot = snapshot_repo.get_for_commit(commit)
        attestation = snapshot_repo.get_attestation_for_commit(commit)
        need_generator = (
            OracleGoldNeedGenerator() if args.query_condition == "oracle" else Stage1NeedGenerator()
        )
        needs = need_generator.generate(
            source.world.model_copy(update={"source_commit": commit}), case
        )
        r1 = R1WorldRepository(factory)
        r1_counts = r1.counts(commit)
        client = OpenSearch(
            hosts=[{"host": target.hostname, "port": target.port}],
            use_ssl=target.scheme == "https",
            verify_certs=target.scheme == "https",
        )
        if not client.ping():
            raise RuntimeError("OpenSearch is unavailable")
        search = OpenSearchIndex(client)
        diagnostics = tuple(
            _need_diagnostic(
                need,
                case,
                commit,
                source.text.root_hash,
                snapshot,
                attestation,
                r1,
                search,
                oracle=args.query_condition == "oracle",
            )
            for need in needs
        )
        first_loss = next(
            (item["first_loss"] for item in diagnostics if item["first_loss"] is not None),
            None,
        )
        report = {
            "case_id": case.case_id.root,
            "checkpoint": args.checkpoint,
            "query_condition": args.query_condition,
            "need_profile": need_generator.profile,
            "source_commit": commit.root,
            "snapshot_id": None if snapshot is None else snapshot.snapshot_id.root,
            "needs": diagnostics,
            "canonical": {
                "text_chapters": len(source.text.chapters),
                "world_records": r1_counts[0],
            },
            "r1": {
                "record_count": r1_counts[0],
                "entity_association_count": r1_counts[1],
                "relation_edge_count": r1_counts[2],
            },
            "first_loss": first_loss,
        }
        payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            print(payload, end="")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, "utf-8")
        return 0 if first_loss is None else 2
    finally:
        if client is not None:
            client.close()
        engine.dispose()


def _first_loss(
    snapshot_exists: bool,
    attestation_exists: bool,
    r1_counts: tuple[int, int, int],
    candidate_count: int,
    sample_error: str | None,
) -> str | None:
    if not snapshot_exists:
        return "derived_snapshot"
    if not attestation_exists:
        return "projection_attestation"
    if r1_counts[0] == 0:
        return "r1_materialization"
    if sample_error is not None:
        return "l2_sample_query"
    if candidate_count == 0:
        return "l2_candidate_recall"
    return None


def _commit_at_checkpoint(
    factory: object,
    loader: ArtifactProjectionSourceLoader,
    project_id: ProjectId,
    checkpoint: int,
) -> CommitId:
    with factory() as session:  # type: ignore[operator]
        values = tuple(
            session.scalars(
                select(CommitRow.commit_id)
                .where(CommitRow.project_id == project_id.root)
                .order_by(CommitRow.created_at, CommitRow.commit_id)
            )
        )
    for raw in values:
        commit = CommitId(raw)
        if len(loader.load(commit).text.chapters) == checkpoint:
            return commit
    raise RuntimeError(f"no project commit has {checkpoint} visible chapters")


def _project_id(factory: object, requested: str | None) -> ProjectId:
    with factory() as session:  # type: ignore[operator]
        values = tuple(
            session.scalars(select(ProjectRow.project_id).order_by(ProjectRow.project_id))
        )
    if requested is not None:
        project_id = ProjectId(requested)
        if project_id.root not in values:
            raise RuntimeError(f"project is not present: {project_id.root}")
        return project_id
    if len(values) != 1:
        raise RuntimeError("--project-id is required when the database has multiple projects")
    return ProjectId(values[0])


def _loopback_url(value: str):
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
