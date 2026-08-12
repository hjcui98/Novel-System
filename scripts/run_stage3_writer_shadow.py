#!/usr/bin/env python3
"""Run the Stage 3 Writer DRAFT path with synthetic fixtures and an offline fake model."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.agents import (
    AgentRegistry,
    StructuredAgentRunner,
    WriterAgent,
    build_writer_contract_bundle,
)
from novel_agent.domain.generation import (
    WriterArtifactBasis,
    WriterBudget,
    WriterContextSnapshot,
    WriterInvocation,
    WriterShadowManifest,
    WriterSourceBinding,
    WritingTaskContract,
)
from novel_agent.domain.ids import (
    ProjectId,
    RunId,
    StableId,
    TaskId,
)
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.stage2 import AgentMode, FutureIsolationAttestation
from novel_agent.ports.object_store import ObjectStat, ObjectStorePort
from novel_agent.prompts import PromptRegistry
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.writer_generation import WriterGenerationService
from novel_agent.skills import SkillRegistry

ROOT = Path(__file__).parents[1]
DEFAULT_FIXTURE_DIRECTORY = ROOT / "tests" / "fixtures" / "stage3_writer"
_FAULTS = (
    "none",
    "invalid-json",
    "model-unavailable",
    "cancelled",
    "budget-exhausted",
    "raw-write",
    "text-write",
    "sidecar-write",
)


class _InjectedArtifactFailure(RuntimeError):
    pass


class _FailingObjectStore:
    """Delegate reads and fail one selected candidate Artifact write."""

    def __init__(self, delegate: ObjectStorePort, fail_on_put: int) -> None:
        self._delegate = delegate
        self._fail_on_put = fail_on_put
        self._put_count = 0

    def put_if_absent(self, key: str, data: bytes, media_type: str) -> ObjectStat:
        self._put_count += 1
        if self._put_count == self._fail_on_put:
            raise _InjectedArtifactFailure(f"injected candidate write failure #{self._put_count}")
        return self._delegate.put_if_absent(key, data, media_type)

    def get(self, key: str) -> bytes:
        return self._delegate.get(key)

    def stat(self, key: str) -> ObjectStat:
        return self._delegate.stat(key)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-directory",
        type=Path,
        default=DEFAULT_FIXTURE_DIRECTORY,
    )
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--run-id", default="run.stage3.writer-shadow")
    parser.add_argument("--fault", choices=_FAULTS, default="none")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.environ.get("NOVEL_AGENT_FORBID_MODEL_CALLS", "").lower() != "true":
        raise RuntimeError("Writer shadow requires NOVEL_AGENT_FORBID_MODEL_CALLS=true")
    fixture_directory = args.fixture_directory.resolve()
    output_directory = args.output_directory.resolve()
    _validate_output_directory(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)

    context = WriterContextSnapshot.model_validate_json(
        (fixture_directory / "context_package.json").read_text(encoding="utf-8")
    )
    writing_task = WritingTaskContract.model_validate_json(
        (fixture_directory / "writing_task_contract.json").read_text(encoding="utf-8")
    )
    plan_bytes = _canonical_fixture_bytes(fixture_directory / "plan.json")
    profile_bytes = _canonical_fixture_bytes(fixture_directory / "project_profile.json")
    source_path = fixture_directory / "visible_reference.json"
    source_payload = json.loads(source_path.read_text(encoding="utf-8"))
    source_bytes = canonical_json_bytes(source_payload)
    source_id = StableId(str(source_payload["source_id"]))
    raw_response = (fixture_directory / "draft_output.json").read_text(encoding="utf-8")

    object_store = FilesystemObjectStore(output_directory / "objects")
    seed_artifacts = ArtifactRepository(object_store)
    schema_version = (
        build_writer_contract_bundle(
            ROOT / "src" / "novel_agent",
            modes=(AgentMode.DRAFT,),
        )
        .agent_specs[0]
        .version
    )
    context_bytes = canonical_json_bytes(context.model_dump(mode="json"))
    writing_bytes = canonical_json_bytes(writing_task.model_dump(mode="json"))
    context_artifact = seed_artifacts.put(
        context_bytes,
        "application/vnd.novel-agent.stage1-context-package+json",
        schema_version,
    )
    writing_artifact = seed_artifacts.put(
        writing_bytes,
        "application/vnd.novel-agent.writing-task-contract+json",
        schema_version,
    )
    plan_artifact = seed_artifacts.put(
        plan_bytes,
        "application/vnd.novel-agent.plan-input+json",
        schema_version,
    )
    profile_artifact = seed_artifacts.put(
        profile_bytes,
        "application/vnd.novel-agent.project-profile-input+json",
        schema_version,
    )
    source_artifact = seed_artifacts.put(
        source_bytes,
        "application/vnd.novel-agent.writer-source+json",
        schema_version,
    )

    bundle = build_writer_contract_bundle(
        ROOT / "src" / "novel_agent",
        modes=(AgentMode.DRAFT,),
    )
    spec = bundle.agent_specs[0]
    prompts = PromptRegistry(bundle.prompt_templates)
    skills = SkillRegistry(bundle.skill_templates)
    model_fingerprint = content_id(
        {
            "endpoint": "stage3-writer-shadow-fake",
            "model": "fake-writer",
            "model_version": "fake-v1",
            "external": False,
        }
    )
    basis = WriterArtifactBasis(
        project_id=ProjectId("project.stage3.writer-shadow"),
        base_commit=context.base_commit,
        snapshot_id=context.snapshot_id,
        context_id=context.context_id,
        context_artifact=context_artifact,
        context_fingerprint=context_artifact.artifact_id,
        writing_contract_artifact=writing_artifact,
        plan_artifact=plan_artifact,
        project_profile_artifact=profile_artifact,
        configuration_fingerprint=content_id(spec.model_dump(mode="json")),
        model_configuration_fingerprint=model_fingerprint,
        future_isolation_attestation=FutureIsolationAttestation(
            attestation_id=StableId("attestation.stage3.writer-shadow"),
            checkpoint_chapter=writing_task.target_chapter - 1,
            canonical_source_ids=(source_id,),
            evaluator_only_source_ids=(),
            overlap_source_ids=(),
            passed=True,
            configuration_fingerprint=content_id({"profile": "stage3-writer-shadow-visible-only"}),
        ),
        source_artifacts=(
            WriterSourceBinding(
                source_id=source_id,
                source_artifact=source_artifact,
            ),
        ),
    )
    run_id = RunId(args.run_id)
    task_id = TaskId(f"task.{args.run_id}")
    budget = WriterBudget(
        max_model_calls=0 if args.fault == "budget-exhausted" else 1,
        input_token_limit=8_000,
        output_token_limit=2_000,
    )
    invocation = WriterInvocation(
        invocation_id=StableId(f"invocation.{args.run_id}"),
        run_id=run_id,
        task_id=task_id,
        mode=AgentMode.DRAFT,
        basis=basis,
        writing_task=writing_task,
        context_package=context,
        input_artifacts=(
            context_artifact,
            writing_artifact,
            plan_artifact,
            profile_artifact,
            source_artifact,
        ),
        budget=budget,
    )

    response_text = "{not-json" if args.fault == "invalid-json" else raw_response
    model_error: BaseException | None = None
    if args.fault == "model-unavailable":
        model_error = RuntimeError("injected offline transport failure")
    elif args.fault == "cancelled":
        model_error = asyncio.CancelledError("injected cancellation")
    endpoint = FakeModelEndpoint(response_text, error=model_error)  # type: ignore[arg-type]
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="stage3-writer-shadow-fake",
                model_name="fake-writer",
                adapter=endpoint,
            ),
        ),
        forbid_external_calls=True,
        structured_max_retries=0,
    )
    runner = StructuredAgentRunner(
        gateway,
        AgentRegistry(bundle.agent_specs),
        prompts,
        skills,
    )
    writer = WriterAgent(runner, prompts, skills)
    fail_on_put = {"raw-write": 1, "text-write": 2, "sidecar-write": 3}.get(args.fault)
    candidate_store: ObjectStorePort = object_store
    if fail_on_put is not None:
        candidate_store = _FailingObjectStore(object_store, fail_on_put)
    service = WriterGenerationService(
        writer,
        gateway,
        ArtifactRepository(candidate_store),
        schema_version,
        model_fingerprint,
    )
    request = ModelRequest(
        request_id=StableId(f"request.{args.run_id}"),
        run_id=run_id,
        task_id=task_id,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id=f"trace-{args.run_id}",
        prompt="replaced by sealed WriterAgent",
        timeout_seconds=budget.timeout_seconds,
    )
    result = asyncio.run(service.execute(invocation, request))
    created_at = result.receipt.completed_at if result.receipt is not None else datetime.now(UTC)
    manifest = WriterShadowManifest(
        manifest_id=StableId(f"manifest.{args.run_id}"),
        run_id=run_id,
        result=result,
        artifacts=result.artifacts,
        created_at=created_at,
    )
    _write_json(output_directory / "writer_result.json", result)
    _write_json(output_directory / "shadow_manifest.json", manifest)
    if result.draft is not None:
        _write_json(output_directory / "draft_artifact.json", result.draft)
        assert result.receipt is not None
        _write_json(output_directory / "writer_receipt.json", result.receipt)
    print(
        json.dumps(
            {
                "run_id": run_id.root,
                "status": result.status.value,
                "engineering_only": True,
                "semantic_quality_not_evaluated": True,
                "model_call_count": result.metrics.model_call_count,
                "draft_id": result.draft.draft_id.root if result.draft is not None else None,
                "output_directory": str(output_directory),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _canonical_fixture_bytes(path: Path) -> bytes:
    return canonical_json_bytes(json.loads(path.read_text(encoding="utf-8")))


def _validate_output_directory(output_directory: Path) -> None:
    forbidden = (ROOT / "reports" / "stage2a").resolve()
    if output_directory == forbidden or forbidden in output_directory.parents:
        raise ValueError("Writer shadow output must not write reports/stage2a")


def _write_json(path: Path, model: BaseModel) -> None:
    payload = (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if path.exists():
        if path.read_text(encoding="utf-8") != payload:
            raise RuntimeError(f"refusing to overwrite different Writer evidence: {path}")
        return
    path.write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
