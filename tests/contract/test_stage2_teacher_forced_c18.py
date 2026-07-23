"""Regression coverage for the real ZTJ C18/checkpoint-20 fixture."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import CandidateChangeBundle, ChangeOperation, ObservedChangeSet
from novel_agent.domain.ids import ArtifactId, ProjectId, RunId, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.memory_write import (
    BlockingScope,
    CandidateMaterialization,
    CandidateProducerKind,
    CandidateRevision,
    CanonicalWriteBasis,
    FindingRetryability,
    MemoryWriteBudgetRemaining,
    MemoryWriteCandidatePayload,
    MemoryWriteCommitProfile,
    NormalizationStatus,
    RepairAction,
    RepairActionReceipt,
    RepairContext,
    RepairScope,
    ValidationDisposition,
    ValidationSeverity,
)
from novel_agent.domain.stage2 import BenchmarkInformationProfile, ContractRef
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_repair_policy import BoundedMemoryRepairPolicy
from novel_agent.services.memory_write_validation import Stage2ValidationV2Adapter
from novel_agent.services.memory_write_workflow import InMemoryArtifactRepository
from novel_agent.services.mutation_normalizer import MutationNormalizer
from novel_agent.services.teacher_forced_benchmark_e2e import (
    TeacherForcedBenchmarkE2ERunner,
)
from tests.factories import make_artifact, make_manifest

ROOT = Path(__file__).parents[2]
PILOT = ROOT / "benchmarks/private/ztj_memory_pilot_v0.1"
CASES = ROOT / "tests/fixtures/stage2w/c18_memory_write_cases.json"
NOW = datetime(2026, 7, 23, tzinfo=UTC)
PROJECT = ProjectId("project.c18.characterization")


def _cases() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(CASES.read_text(encoding="utf-8")))


def _candidate_and_basis(
    case_name: str,
) -> tuple[
    CandidateRevision,
    CanonicalWriteBasis,
    MemoryWriteCandidatePayload,
    InMemoryArtifactRepository,
]:
    fixture = _cases()
    world = WorldRootDocument.model_validate_json(
        json.dumps(fixture["base_world"]),
        strict=True,
    )
    operation = ChangeOperation.model_validate_json(
        json.dumps(fixture[case_name]["operation"]),
        strict=True,
    )
    changes = ObservedChangeSet(
        change_set_id=StableId(f"changes.{case_name}"),
        base_commit=world.source_commit,
        source_artifact=ArtifactRef(
            artifact_id=ArtifactId(fixture["provenance"]["source_chapter_artifact"]),
            media_type="application/vnd.novel-agent.chapter+json",
            byte_length=1,
            schema_version=world.schema_version,
        ),
        operations=(operation,),
    )
    payload = MemoryWriteCandidatePayload(
        observed_changes=changes,
        root_update_intents=(),
        commit_profile=MemoryWriteCommitProfile.CHANGED_ROOTS_ONLY,
    )
    artifacts = InMemoryArtifactRepository()
    data = canonical_json_bytes(payload.model_dump(mode="json"))
    artifact = artifacts.put(
        data,
        "application/vnd.novel-agent.memory-write-candidate+json",
        world.schema_version,
    )
    candidate = CandidateRevision(
        candidate_id=StableId(f"candidate.{case_name}.1"),
        revision_no=1,
        base_commit=world.source_commit,
        basis_hash=ArtifactId(fixture["base_world"]["root_hash"]),
        candidate_artifact=artifact,
        producer_kind=CandidateProducerKind.CURATOR_PROPOSE,
        content_hash=artifact.artifact_id,
        created_at=NOW,
    )
    basis = CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=world.source_commit,
        root_manifest=make_manifest(PROJECT),
        canonical_world=world,
    )
    return candidate, basis, payload, artifacts


def _finding(code: str, operation_id: StableId) -> Any:
    return SimpleNamespace(
        finding_id=StableId(f"finding.c18.{code.lower().replace('_', '-')}"),
        code=code,
        severity=ValidationSeverity.ERROR,
        retryability=(
            FindingRetryability.NON_REPAIRABLE
            if code == "FUTURE_EVIDENCE"
            else FindingRetryability.REPAIRABLE
        ),
        operation_ids=(operation_id,),
        allowed_repair_scope=RepairScope(
            operation_ids=(operation_id,),
            allow_identity_rebind=True,
            allow_successor_creation=True,
        ),
        blocking_scope=BlockingScope.OPERATION,
        requires_human=False,
        requires_context_refresh=False,
    )


def _repair_context(
    candidate: CandidateRevision,
    finding: Any,
    *,
    prior: tuple[RepairActionReceipt, ...] = (),
) -> RepairContext:
    return cast(
        RepairContext,
        SimpleNamespace(
            candidate=candidate,
            validation=SimpleNamespace(findings=(finding,)),
            risk=None,
            guardian=None,
            gate=None,
            budget_remaining=MemoryWriteBudgetRemaining(
                candidate_revisions=2,
                curator_repairs=1,
                normalization_passes=1,
                guardian_reviews=1,
                context_refreshes=1,
                total_model_calls=1,
                token_budget=100,
                wall_clock_budget_ms=100,
            ),
            prior_actions=prior,
            repeated_content_hashes=(),
            current_canonical_commit=candidate.base_commit,
        ),
    )


def test_real_c18_fixture_runs_through_stage2w_candidate_normalization(
    tmp_path: Path,
) -> None:
    """C18 uses the checked-in novel fixture, not a reconstructed benchmark result."""

    bundle = HumanBenchmarkCompiler().compile(PILOT)
    summary = TeacherForcedBenchmarkE2ERunner(semantic_endpoint=None).run(
        PILOT,
        tmp_path / "c18-stage2w",
        bundle,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        max_chapter=20,
    )

    assert summary["last_revealed_chapter"] == 20
    assert summary["chapter_commit_count"] == 21
    assert summary["memory_write_status_counts"] == {"committed": 21}
    assert summary["memory_write_candidate_revisions"] == 21
    assert summary["memory_write_normalization_passes"] == 20
    assert summary["future_isolation_failure_count"] == 0
    assert summary["future_leakage_count"] == 0


def test_c18_a_unique_successor_is_deterministically_repaired() -> None:
    candidate, basis, payload, artifacts = _candidate_and_basis("c18_a")
    normalizer = MutationNormalizer(
        payload_loader=lambda _: payload,
        artifact_writer=artifacts,
        clock=lambda: NOW,
    )

    result = normalizer.normalize(candidate, basis)

    assert result.status is NormalizationStatus.TRANSFORMED
    assert result.candidate.parent_candidate_id == candidate.candidate_id
    assert [item.rule_id.root for item in result.transforms] == ["normalize.state-successor-v1"]
    repaired = MemoryWriteCandidatePayload.model_validate_json(
        artifacts.read_verified(result.candidate.candidate_artifact),
        strict=True,
    )
    assert [item.operation.value for item in repaired.observed_changes.operations] == [
        "replace",
        "create",
    ]
    assert repaired.observed_changes.operations[0].target_id == StableId(
        "state.chen-changsheng-admission"
    )
    bundle = CandidateChangeBundle(
        bundle_id=StableId(f"bundle.{result.candidate.candidate_id.root}"),
        project_id=PROJECT,
        run_id=RunId("run.c18-a"),
        base_commit=result.candidate.base_commit,
        observed_changes=repaired.observed_changes,
        proposed_roots=make_manifest(PROJECT),
    )
    materialization = CandidateMaterialization(
        candidate_id=result.candidate.candidate_id,
        candidate_content_hash=result.candidate.content_hash,
        bundle_artifact=make_artifact("a"),
        proposed_roots_hash=ArtifactId("sha256:" + "b" * 64),
        materialization_receipt=make_artifact("c"),
        materializer_policy_ref=ContractRef(
            contract_id=StableId("policy.c18.materializer"),
            version=bundle.proposed_roots.schema_version,
            content_hash=ArtifactId("sha256:" + "d" * 64),
        ),
        bundle=bundle,
    )
    text_root = TextRootDocument.model_validate_json(
        json.dumps(_cases()["validation_text_root"]),
        strict=True,
    )
    typed_basis = basis.model_copy(update={"canonical_text": text_root})
    decision = asyncio.run(
        Stage2ValidationV2Adapter().validate(
            result.candidate,
            materialization,
            typed_basis,
        )
    )
    assert decision.disposition is ValidationDisposition.PASS


def test_c18_b_ambiguous_identity_routes_to_scoped_curator_repair() -> None:
    candidate, basis, payload, artifacts = _candidate_and_basis("c18_b")
    operation = payload.observed_changes.operations[0]
    normalizer = MutationNormalizer(
        payload_loader=lambda _: payload,
        artifact_writer=artifacts,
        clock=lambda: NOW,
    )
    normalized = normalizer.normalize(candidate, basis)
    finding = _finding("STATE_IDENTITY_MUTATION", operation.operation_id)
    prior = (
        RepairActionReceipt(
            receipt_id=StableId("receipt.c18.deterministic"),
            action=RepairAction.DETERMINISTIC_REPAIR,
            directive_id=StableId("directive.c18.deterministic"),
            candidate_id=candidate.candidate_id,
            reason_codes=("STATE_IDENTITY_MUTATION",),
        ),
    )

    directive = BoundedMemoryRepairPolicy().decide(_repair_context(candidate, finding, prior=prior))

    assert normalized.status is NormalizationStatus.AMBIGUOUS
    assert directive.action is RepairAction.CURATOR_REPAIR
    assert directive.operation_ids == (operation.operation_id,)
    assert directive.allowed_scope.operation_ids == (operation.operation_id,)


def test_c18_c_future_evidence_conflict_fails_closed() -> None:
    candidate, _, payload, _ = _candidate_and_basis("c18_b")
    operation = payload.observed_changes.operations[0]
    finding = _finding(_cases()["c18_c"]["finding_code"], operation.operation_id)

    directive = BoundedMemoryRepairPolicy().decide(_repair_context(candidate, finding))

    assert directive.action is RepairAction.STOP_FATAL
    assert directive.reason_codes == ("FUTURE_EVIDENCE",)
