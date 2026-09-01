#!/usr/bin/env python3
"""Prepare U4-S0 production facts without creating a model-call ledger entry.

The U4-S0 freeze command consumes a resolved production attestation and one
EffectiveBudgetResult.  This helper binds the same production composition root
used by the U4-L2 candidate runner, copies the existing canonical roots into a
new evaluation object namespace, and serializes only the resolved facts.  It
does not invoke a model, retrieve benchmark Gold, or create a ledger request.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import OpenAICompatibleChatEndpoint
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import AutomationMode, CreativeRunPolicy
from novel_agent.domain.ids import ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.runtime.creative_assembly import (
    DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    ProductionAssemblyContext,
    load_production_runtime_assembly,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = SchemaVersion("1.0.0")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-project", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--project-id", default="ztj_volume01_preview")
    parser.add_argument("--model-base-url", default="http://127.0.0.1:8005/v1")
    parser.add_argument("--model", default="qwen38-27b-fp8")
    parser.add_argument("--model-output-tokens", type=int, default=8000)
    parser.add_argument("--prompt-text", default="u4s-writer-readout-prompt.v1")
    return parser


def _copy_ref(
    source: ArtifactRepository,
    destination: ArtifactRepository,
    ref: ArtifactRef,
) -> None:
    """Copy a canonical root while preserving its content-addressed identity.

    ``RootManifest`` stores typed ``ArtifactRef`` subclasses, while the
    repository writer returns the common base type.  The four immutable
    reference fields, rather than the Pydantic model class, define identity.
    """
    copied = destination.put(source.read_verified(ref), ref.media_type, ref.schema_version)
    if (
        copied.artifact_id != ref.artifact_id
        or copied.media_type != ref.media_type
        or copied.byte_length != ref.byte_length
        or copied.schema_version != ref.schema_version
    ):
        raise RuntimeError(
            "canonical root copy changed identity: "
            f"expected={ref.artifact_id.root} copied={copied.artifact_id.root}"
        )


def _endpoint_name(base_url: str, model: str) -> str:
    parsed = urlparse(base_url)
    return f"{model}@{parsed.port or (443 if parsed.scheme == 'https' else 80)}"


def _check_args(args: argparse.Namespace) -> None:
    if args.output_root.exists():
        raise FileExistsError("U4-S facts output identity already exists")
    if args.model_output_tokens < 1:
        raise ValueError("Writer output budget must be positive")
    if not (args.source_project / "objects").is_dir():
        raise ValueError("source project object store is missing")
    if args.output_root.resolve() == args.source_project.resolve():
        raise ValueError("U4-S facts must use a separate object namespace")
    if urlparse(args.model_base_url).hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("U4-S model endpoint must be loopback")
    StableId(args.experiment_id)


def main() -> int:
    args = _parser().parse_args()
    _check_args(args)
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True)
    object_root = output_root / "objects"
    object_root.mkdir()
    project_id = ProjectId(args.project_id)
    run_id = RunId(f"run.u4s.{args.experiment_id}"[:128])
    database_engine = build_engine(args.database_url)
    try:
        session_factory = build_session_factory(database_engine)
        commits = CommitService(session_factory)
        current_commit = commits.current_commit(project_id)
        source = ArtifactRepository(
            FilesystemObjectStore(args.source_project.resolve() / "objects")
        )
        destination = ArtifactRepository(FilesystemObjectStore(object_root))
        manifest = commits.load_manifest(current_commit)
        for ref in (
            manifest.text_root,
            manifest.plan_root,
            manifest.world_root,
            manifest.reference_root,
            manifest.project_profile_root,
        ):
            _copy_ref(source, destination, ref)

        endpoint = OpenAICompatibleChatEndpoint(
            base_url=args.model_base_url,
            model=args.model,
            max_output_tokens=args.model_output_tokens,
            temperature=0.0,
            local_only=True,
            max_retries=0,
        )
        from novel_agent.services.model_gateway import RegisteredModelEndpoint

        endpoint_registration = RegisteredModelEndpoint(
            role=ModelRole.IMPLEMENTATION,
            endpoint_name=_endpoint_name(args.model_base_url, args.model),
            model_name=args.model,
            adapter=endpoint,
            revision=args.model,
            sequence_limit=131_072,
            output_limit=args.model_output_tokens,
            safety_allowance_tokens=1_000,
            estimated_reasoning_reserve=2_048,
            default_thinking=False,
            reasoning_included_in_completion_tokens=False,
            global_output_cap=131_072,
        )
        runtime_manifest = load_stage5_manifest(
            ROOT / "src" / "novel_agent" / "runtime" / "stage5_development_manifest.json"
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
                database_url=args.database_url,
                object_store_root=object_root,
                project_id=project_id,
                run_id=run_id,
                policy=policy,
                manifest=runtime_manifest,
                model_endpoints=(endpoint_registration,),
                schema_version=SCHEMA_VERSION,
            ),
        )
        if assembly.attestation is None or assembly.model_gateway is None:
            raise RuntimeError("production assembly did not expose frozen facts")

        prompt_path = output_root / "writer_readout_prompt.txt"
        prompt_path.write_text(args.prompt_text + "\n", encoding="utf-8")
        combined_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "u4s_writer_readout_contract.v1",
            "title": "U4SWriterReadoutContract",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "qa": json.loads(
                    (ROOT / "schemas" / "stage2" / "QaWriterResponse.schema.json").read_text()
                ),
                "context": json.loads(
                    (ROOT / "schemas" / "stage2" / "ContextWriterResponse.schema.json").read_text()
                ),
            },
            "required": ["qa", "context"],
        }
        schema_path = output_root / "writer_readout_schema.json"
        schema_path.write_text(
            json.dumps(combined_schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        budget_request = ModelRequest(
            request_id=StableId("model-request.u4s.freeze-budget"),
            run_id=run_id,
            task_id=TaskId("task.u4s.freeze-budget"),
            model_role=ModelRole.IMPLEMENTATION,
            purpose=ModelCallPurpose.DEVELOPMENT,
            trace_id=f"trace.{run_id.root}.freeze-budget",
            prompt=args.prompt_text,
            max_output_tokens=args.model_output_tokens,
            enable_thinking=False,
            scheduling_stage="u4s.freeze_budget",
        )
        effective_budget = assembly.model_gateway.resolve_effective_budget(
            budget_request,
            estimated_input_tokens=256,
        )
        attestation_path = output_root / "resolved_production_assembly_attestation.json"
        budget_path = output_root / "effective_budget.json"
        attestation_path.write_text(
            json.dumps(
                assembly.attestation.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        budget_path.write_text(
            json.dumps(
                effective_budget.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        ledger_count = len(assembly.model_gateway.call_ledger.list_for_run(run_id))
        if ledger_count != 0:
            raise RuntimeError(
                f"fact preparation unexpectedly created {ledger_count} ledger requests"
            )
        facts = {
            "schema": "u4s-campaign-facts.v1",
            "experiment_id": args.experiment_id,
            "run_id": run_id.root,
            "project_id": project_id.root,
            "basis_commit": current_commit.root,
            "object_store_root": str(object_root),
            "attestation": str(attestation_path),
            "effective_budget": str(budget_path),
            "writer_prompt": str(prompt_path),
            "writer_schema": str(schema_path),
            "ledger_request_count": ledger_count,
            "model_endpoint": endpoint_registration.endpoint_name,
            "model": args.model,
            "model_base_url": args.model_base_url,
        }
        facts_path = output_root / "u4s_campaign_facts.json"
        facts_path.write_text(
            json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(facts, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        database_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
