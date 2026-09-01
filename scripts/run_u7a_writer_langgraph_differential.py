#!/usr/bin/env python3
"""Run an isolated real-model Writer-vs-LangGraph U7-A differential.

The two sides use cloned PostgreSQL databases and separate object stores.  The production
assembly is reused unchanged; only the graph side wraps its already-built Writer leaf.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from run_u4l1_writer_leaf import (
    _build_task,
    _copy_canonical_roots,
    _model_identity,
)

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.model import OpenAICompatibleChatEndpoint
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.artifacts import RootManifest
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.creative_runtime import AutomationMode, CreativeRunPolicy
from novel_agent.domain.ids import ProjectId, RunId, SchemaVersion, TaskId
from novel_agent.domain.model_calls import ModelRole
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.domain.writing_loop import WritingLoopResult
from novel_agent.runtime import writer_langgraph_leaf
from novel_agent.runtime.creative_assembly import (
    DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    ProductionAssemblyContext,
    load_production_runtime_assembly,
)
from novel_agent.runtime.writer_langgraph_leaf import WriterLangGraphLeafAdapter
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.model_gateway import RegisteredModelEndpoint
from novel_agent.services.projection import DerivedSnapshotRepository

SCHEMA_VERSION = SchemaVersion("1.0.0")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-object-root", type=Path, required=True)
    parser.add_argument("--direct-database-url", required=True)
    parser.add_argument("--graph-database-url", required=True)
    parser.add_argument("--direct-output-root", type=Path, required=True)
    parser.add_argument("--graph-output-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--opensearch-url", default="http://127.0.0.1:9200")
    parser.add_argument("--embedding-url", default="http://127.0.0.1:8081/v1/embeddings")
    parser.add_argument("--reranker-url", default="http://127.0.0.1:8082/rerank")
    parser.add_argument("--model-base-url", default="http://127.0.0.1:8005/v1")
    parser.add_argument("--model", default="qwen38-27b-fp8")
    parser.add_argument("--model-output-tokens", type=int, default=8000)
    return parser


def _check_free(paths: tuple[Path, ...]) -> None:
    occupied = tuple(str(path) for path in paths if path.exists())
    if occupied:
        raise RuntimeError("U7-A refuses to reuse output identity: " + ", ".join(occupied))


def _endpoint(args: argparse.Namespace) -> RegisteredModelEndpoint:
    return RegisteredModelEndpoint(
        role=ModelRole.IMPLEMENTATION,
        endpoint_name=f"{args.model}@8005",
        model_name=args.model,
        adapter=OpenAICompatibleChatEndpoint(
            base_url=args.model_base_url,
            model=args.model,
            max_output_tokens=args.model_output_tokens,
            temperature=0.0,
            local_only=True,
            max_retries=0,
        ),
        revision=args.model,
        sequence_limit=131_072,
        output_limit=args.model_output_tokens,
        safety_allowance_tokens=1_000,
        estimated_reasoning_reserve=2_048,
        default_thinking=False,
        reasoning_included_in_completion_tokens=False,
        global_output_cap=131_072,
    )


def _build_side(
    *,
    args: argparse.Namespace,
    database_url: str,
    output_root: Path,
    endpoint_registration: RegisteredModelEndpoint,
) -> tuple[WritingLoopResult, RegisteredModelEndpoint]:
    database_engine = build_engine(database_url)
    try:
        session_factory = build_session_factory(database_engine)
        commits = CommitService(session_factory)
        project_id = ProjectId(args.project_id)
        run_id = RunId(args.run_id)
        task_id = TaskId(args.task_id)
        current_commit = commits.current_commit(project_id)
        manifest = commits.load_manifest(current_commit)
        if not isinstance(manifest, RootManifest) or manifest.project_id != project_id:
            raise RuntimeError("isolated Writer basis manifest does not match project")
        snapshots = DerivedSnapshotRepository(session_factory)
        snapshot = snapshots.get_for_commit(current_commit)
        if snapshot is None:
            raise RuntimeError("isolated Writer basis has no derived snapshot")
        if (
            snapshot.source_commit != current_commit
            or snapshot.build_status.value != "exact"
            or snapshot.published_at is None
        ):
            raise RuntimeError("isolated Writer basis is not an exact quality-eligible projection")

        output_root.mkdir(parents=True)
        source = ArtifactRepository(FilesystemObjectStore(args.source_object_root.resolve()))
        output = ArtifactRepository(FilesystemObjectStore(output_root / "objects"))
        _copy_canonical_roots(source, output, manifest)
        text_root = TextRootDocument.model_validate_json(
            output.read_verified(manifest.text_root), strict=True
        )
        runtime_manifest = load_stage5_manifest(
            Path(__file__).resolve().parents[1]
            / "src"
            / "novel_agent"
            / "runtime"
            / "stage5_development_manifest.json"
        )
        policy = CreativeRunPolicy(
            automation_mode=AutomationMode.MANUAL,
            policy_hash=runtime_manifest.configuration_fingerprint,
            permission_hash=runtime_manifest.configuration_fingerprint,
            runtime_parallelism=1,
        )
        assembly = load_production_runtime_assembly(
            DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
            ProductionAssemblyContext(
                database_url=database_url,
                object_store_root=output_root / "objects",
                project_id=project_id,
                run_id=run_id,
                policy=policy,
                manifest=runtime_manifest,
                model_endpoints=(endpoint_registration,),
                schema_version=SCHEMA_VERSION,
            ),
        )
        task = _build_task(
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            commit=current_commit,
            snapshot_id=snapshot.snapshot_id,
            policy=policy,
            manifest=manifest,
            latest_chapter=text_root.chapters[-1].chapter_index if text_root.chapters else 0,
        )
        request = assembly.writing_request_factory(task)
        if assembly.artifacts is None:
            raise RuntimeError("production assembly did not expose ArtifactRepository")
        result = asyncio.run(
            WriterLangGraphLeafAdapter(assembly.writer, assembly.artifacts).run(request)
            if output_root.name.endswith("-graph")
            else assembly.writer.run(request)
        )
        if not isinstance(result, WritingLoopResult):
            raise TypeError("Writer differential returned the wrong result type")
        return result, endpoint_registration
    finally:
        database_engine.dispose()


def _receipt_shape(result: WritingLoopResult) -> dict[str, object]:
    return {
        "status": result.status.value,
        "candidate_only": result.candidate_only,
        "canon_mutated": result.canon_mutated,
        "memory_patch_generated": result.memory_patch_generated,
        "commit_called": result.commit_called,
        "final_candidate_present": result.final_candidate_id is not None,
        "initial_draft_present": result.initial_draft is not None,
        "rewritten_draft_present": result.rewritten_draft is not None,
        "repaired_draft_present": result.repaired_draft is not None,
        "observation_present": result.observation_artifact is not None,
        "reconciliation_present": result.reconciliation is not None,
        "checkpoint_present": result.checkpoint_ref is not None,
        "editorial_verdicts": tuple(report.verdict.value for report in result.editorial_reports),
        "model_call_count": len(result.model_call_records),
        "model_call_shapes": tuple(
            (record.model_role.value, record.purpose.value, record.usage.model_dump(mode="json"))
            for record in result.model_call_records
        ),
    }


def main() -> int:
    args = _parser().parse_args()
    _check_free(
        (
            args.direct_output_root,
            args.graph_output_root,
            args.output,
        )
    )
    if args.direct_output_root.name != "direct" and not args.direct_output_root.name.endswith(
        "-direct"
    ):
        raise ValueError("direct output root must be visibly marked direct")
    if args.graph_output_root.name != "graph" and not args.graph_output_root.name.endswith(
        "-graph"
    ):
        raise ValueError("graph output root must be visibly marked graph")
    identity = _model_identity(args.model_base_url, args.model)
    endpoint_registration = _endpoint(args)
    direct, _ = _build_side(
        args=args,
        database_url=args.direct_database_url,
        output_root=args.direct_output_root,
        endpoint_registration=endpoint_registration,
    )
    graph, _ = _build_side(
        args=args,
        database_url=args.graph_database_url,
        output_root=args.graph_output_root,
        endpoint_registration=_endpoint(args),
    )
    direct_shape = _receipt_shape(direct)
    graph_shape = _receipt_shape(graph)
    comparison = {
        "same_run_task_lineage": direct.run_id == graph.run_id and direct.task_id == graph.task_id,
        "same_terminal_status": direct.status is graph.status,
        "same_acceptance_boundary": all(
            direct_shape[key] == graph_shape[key]
            for key in (
                "candidate_only",
                "canon_mutated",
                "memory_patch_generated",
                "commit_called",
            )
        ),
        "same_review_repair_shape": direct_shape["editorial_verdicts"]
        == graph_shape["editorial_verdicts"],
        "same_usage_shape": direct_shape["model_call_shapes"] == graph_shape["model_call_shapes"],
        "same_checkpoint_shape": direct_shape["checkpoint_present"]
        == graph_shape["checkpoint_present"],
        "same_artifact_boundary_shape": all(
            direct_shape[key] == graph_shape[key]
            for key in (
                "initial_draft_present",
                "rewritten_draft_present",
                "repaired_draft_present",
                "observation_present",
                "reconciliation_present",
            )
        ),
    }
    payload: dict[str, Any] = {
        "schema": "u7a-writer-langgraph-real-differential.v1",
        "status": "PASS" if all(comparison.values()) else "REVIEW_REQUIRED",
        "candidate_only": True,
        "model_identity": identity,
        "basis": {
            "project_id": args.project_id,
            "run_id": args.run_id,
            "task_id": args.task_id,
            "direct_database": args.direct_database_url.rsplit("@", 1)[-1],
            "graph_database": args.graph_database_url.rsplit("@", 1)[-1],
        },
        "direct": {
            "result": direct_shape,
            "final_candidate_id": getattr(direct.final_candidate_id, "root", None),
        },
        "graph": {
            "result": graph_shape,
            "final_candidate_id": getattr(graph.final_candidate_id, "root", None),
        },
        "comparison": comparison,
        "production_default_changed": False,
        "acceptance_or_commit_called": direct.commit_called or graph.commit_called,
        "writer_langgraph_module": writer_langgraph_leaf.__file__,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
