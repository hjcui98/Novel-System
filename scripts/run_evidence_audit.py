#!/usr/bin/env python3
"""Read-only EvidenceRef audit for accepted C1-C20 commits (WP6).

Usage::

    python scripts/run_evidence_audit.py \
        --project-directory /path/to/project \
        --database-url postgresql+psycopg://... \
        --output-directory reports/stage2a/evidence_audit \
        --audit-id audit_c1_c20_20260724

The audit is strictly read-only: it does not modify commits, roots, snapshots,
or search indices.  It loads the canonical World and Text from the latest
accepted commit and runs hard integrity + risk heuristics + lexical prefilter
checks on every EvidenceRef.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.memory_write.teacher_forced import (
    RepositoryCanonicalReadAdapter,
)
from novel_agent.adapters.postgres.database import (
    build_engine,
    build_session_factory,
)
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import ProjectId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.evidence_audit import EvidenceRefAuditor


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--project-directory", type=Path, required=True)
    value.add_argument("--database-url", required=True)
    value.add_argument("--output-directory", type=Path, required=True)
    value.add_argument("--audit-id", default=f"audit_{uuid4().hex[:12]}")
    value.add_argument(
        "--project-id",
        default="project.stage2-teacher-forced",
        help="ProjectId whose current commit is the audit basis",
    )
    return value


def main() -> int:
    args = parser().parse_args()
    engine = build_engine(args.database_url)
    try:
        session_factory = build_session_factory(engine)
        commits = CommitService(session_factory)
        artifacts = ArtifactRepository(
            FilesystemObjectStore(args.project_directory / "objects")
        )
        canonical_read = RepositoryCanonicalReadAdapter(commits, artifacts)
        project_id = ProjectId(args.project_id)
        current = commits.current_commit(project_id)
        basis = canonical_read.load_verified(project_id, current)
        if basis.canonical_world is None or basis.canonical_text is None:
            raise ValueError(
                "canonical World or Text is missing from the current commit"
            )
        world = WorldRootDocument.model_validate_json(
            basis.canonical_world.model_dump_json(),
        )
        text = TextRootDocument.model_validate_json(
            basis.canonical_text.model_dump_json(),
        )
        auditor = EvidenceRefAuditor()
        findings = auditor.audit_world(world, text)
        report_dir = auditor.write_report(
            findings,
            args.output_directory,
            audit_id=args.audit_id,
        )
        summary_path = report_dir / "summary.json"
        summary = json.loads(summary_path.read_text("utf-8"))
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        hard_failures = summary.get("hard_failures", 0)
        if hard_failures > 0:
            print(f"WARNING: {hard_failures} hard validation failures detected")
            return 1
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
