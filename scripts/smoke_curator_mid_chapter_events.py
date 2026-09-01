#!/usr/bin/env python3
"""Real-model probe: ordinary Curator on a mid chapter (Event/Obligation elicitation).

Authority §11.2 requires three real smokes (early/mid/late) checking whether the
model actually produces durable Event/Obligation records.  Early chapters (ch1/ch2)
produced none and the receipt correctly distinguished "model did not propose" from
"host rejected".  This probe runs the ordinary Curator (extract_reported_v2, the
repaired subject-bearing quote contract) on a MID chapter of the real pilot bundle
with the real committed smoke world, and prints the proposed record kinds so the
mid-chapter Event/Obligation question is answered by evidence, not assumption.

Diagnostic only: nothing is committed, no identity is claimed.

Usage:
  SMOKE_DATABASE_URL=... .conda-env/bin/python scripts/smoke_curator_mid_chapter_events.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import OpenAICompatibleChatEndpoint
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.ids import (
    CommitId,
    ProjectId,
    RunId,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.model_curation import (
    CuratorProposalSemanticRejected,
    ModelCurator,
)
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint

PROJECT_ROOT = Path(__file__).parents[1]
PROJECT_DIRECTORY = PROJECT_ROOT / "tmp" / "smoke-20260814-repair-v1"
DATABASE_URL = os.environ["SMOKE_DATABASE_URL"]
PROJECT_ID = ProjectId("ztj_volume01_preview")
BUNDLE_ROOT = PROJECT_ROOT / "benchmarks/private/ztj_memory_pilot_v0.1"
MODEL_BASE = "http://127.0.0.1:8003/v1"
MODEL = "qwen36-27b-nvfp4"
MID_CHAPTER = int(os.environ.get("SMOKE_MID_CHAPTER", "10"))
CASE_ID = StableId("ZTJ-P001")


def _request(attempt: int = 1) -> ModelRequest:
    return ModelRequest(
        request_id=StableId(f"request.smoke-mid-chapter.a{attempt}"),
        run_id=RunId("run.smoke-mid-chapter"),
        task_id=TaskId("task.smoke-mid-chapter"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id=f"trace-smoke-mid-chapter-a{attempt}",
        prompt="replaced by extract_reported_v2",
        max_output_tokens=12288,
        timeout_seconds=600,
        enable_thinking=False,
        thinking_token_budget=None,
        scheduling_stage="agent_smoke_mid_chapter",
    )


def _commit_chain(commits: CommitService) -> list[CommitId]:
    head = commits.current_commit(PROJECT_ID)
    chain: list[CommitId] = []
    node: CommitId | None = head
    seen: set[str] = set()
    while node is not None and node.root not in seen:
        seen.add(node.root)
        chain.append(node)
        manifest = commits.load_manifest(node)
        parents = manifest.parent_commit_ids
        node = parents[0] if parents else None
    return list(reversed(chain))


def main() -> int:
    engine = build_engine(DATABASE_URL)
    session_factory = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(PROJECT_DIRECTORY / "objects"))
    commits = CommitService(session_factory)
    chain = _commit_chain(commits)
    latest = chain[-1]
    manifest = commits.load_manifest(latest)
    world = WorldRootDocument.model_validate_json(
        artifacts.read_verified(manifest.world_root).decode("utf-8"), strict=True
    )
    print(f"basis commit: {latest.root[:20]}")
    print(f"world: entities={len(world.entities)} states={len(world.states)}")

    bundle = HumanBenchmarkCompiler().compile(BUNDLE_ROOT)
    case = next(item for item in bundle.case_manifests if item.case_id == CASE_ID)
    history = next(root for root in bundle.text_roots if root.root_hash == case.input_text_root)
    chapters = {chapter.chapter_index: chapter for chapter in history.chapters}
    print(f"history chapters available: {sorted(chapters)}")
    chapter = chapters.get(MID_CHAPTER)
    if chapter is None:
        raise SystemExit(
            f"mid chapter {MID_CHAPTER} not in P001 history range {case.history_range}"
        )
    print(f"mid chapter {MID_CHAPTER}: {chapter.chapter_id.root}")

    endpoint = OpenAICompatibleChatEndpoint(
        base_url=MODEL_BASE,
        model=MODEL,
        max_output_tokens=12288,
        max_retries=0,
    )
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="local-openai-chat",
                model_name=MODEL,
                adapter=endpoint,
            ),
        ),
        structured_max_retries=0,
    )
    curator = ModelCurator(gateway, enforce_support_gate=True, enable_model_semantic_verifier=True)

    def run() -> None:
        from novel_agent.services.evidence_candidates import EvidenceCandidateGenerator

        generator = EvidenceCandidateGenerator()
        candidates = generator.generate(history, MID_CHAPTER)
        print(f"evidence candidates for ch{MID_CHAPTER}: {len(candidates)}")

        changes = None
        draft = None
        feedback: str | None = None
        for attempt in range(1, 4):
            print(f"\n=== attempt {attempt} ===")
            try:
                changes, _call, draft = asyncio.run(
                    curator.extract_reported_v2(
                        history,
                        MID_CHAPTER,
                        latest,
                        world,
                        _request(attempt),
                        repair_feedback=feedback,
                    )
                )
                break
            except CuratorProposalSemanticRejected as error:
                print(f"rejected: {error.reason_code}")
                print(f"  violation_rule: {error.violation_rule}")
                print(f"  safe_feedback: {error.safe_feedback}")
                feedback = (
                    '{"reason_code":"'
                    + error.reason_code
                    + '","safe_feedback":'
                    + __import__("json").dumps(list(error.safe_feedback), ensure_ascii=False)
                    + "}"
                )
        if draft is None:
            raise SystemExit("all curator attempts rejected")
        print("=== curator draft operations ===")
        catalog = {item.candidate_id: item for item in curator.last_evidence_candidates or ()}
        from collections import Counter

        kinds = Counter(item.record_kind.value for item in draft.operations)
        print(f"record kinds proposed: {dict(kinds)}")
        for index, operation in enumerate(draft.operations):
            print(
                f"  [{index}] {operation.operation.value} {operation.record_kind.value} "
                f"target={operation.target_id.root}"
            )
            for bound in operation.evidence_candidate_ids:
                evidence = catalog.get(bound)
                if evidence is not None:
                    print(f"        evidence: {evidence.text[:100]!r}")
                else:
                    print(f"        {bound.root}")
        print("no_durable_delta_reason:", draft.no_durable_delta_reason)
        print("coverage:", draft.coverage)
        print("=== support gate decisions ===")
        for decision in curator.last_support_decisions:
            print(
                f"  op={decision.operation_index} candidate={decision.candidate_id.root[:20]} "
                f"disposition={decision.disposition.value} reason={decision.reason_code}"
            )
        print("=== record-kind coverage receipt ===")
        if curator.last_record_kind_coverage is not None:
            for count in curator.last_record_kind_coverage.counts:
                print(
                    f"  {count.record_kind.value}: proposed={count.proposed} "
                    f"accepted={count.accepted} rejected={count.rejected}"
                )
            print("  no_durable_delta:", curator.last_record_kind_coverage.no_durable_delta)
            print("  reason:", curator.last_record_kind_coverage.no_durable_delta_reason)
        else:
            print("  (no receipt produced)")
        print("=== materialized changes ===")
        print(f"  operations: {len(changes.operations)}" if changes else "  (none)")
        if changes:
            for op in changes.operations:
                print(f"    {op.operation.value} {op.target_id.root} root={op.root_kind.value}")

    try:
        run()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
