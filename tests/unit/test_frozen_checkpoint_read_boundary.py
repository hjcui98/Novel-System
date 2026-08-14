"""Frozen checkpoint read boundary regression (2026-08-14 review P2).

Covers the frozen-driver read fix: a PairedContextComparison whose
RetrievalTrace carries a mode="before" validator cannot be re-validated with
strict=True from its own canonical JSON dump, so the driver reads lax and
requires byte-identical canonical re-dump.  This test pins that boundary:
accepted canonical input, rejected drift, rejected commit mismatch, and
checkpoint-index loading.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.run_evidence_first_frozen_checkpoints import (
    _load_checkpoint_index,
    _parse_frozen_comparison,
)

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    ContextBudgetReport,
    FacetClosureStatus,
    FacetEvidenceReceipt,
    NeedFacetKind,
    RequirementLevel,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalTrace,
    Stage1ContextPackage,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.planning_memory import PlannerInvocationArtifact
from novel_agent.domain.stage2 import (
    ArmExecutionStatus,
    ControllerArm,
    ControllerStopReason,
    PairedContextArmResult,
    PairedContextComparison,
)
from novel_agent.services.content_addressing import canonical_json_bytes

COMMIT = CommitId("sha256:" + "0" * 64)
OTHER_COMMIT = CommitId("sha256:" + "1" * 64)
SNAPSHOT = StableId("snapshot.frozen-read")
FINGERPRINT = ArtifactId("sha256:" + "2" * 64)


def _trace() -> RetrievalTrace:
    """Trace carrying a before-validator path and a mandatory facet receipt."""
    facet_id = StableId("need-facet.test.current_state")
    receipt = FacetEvidenceReceipt(
        need_id=StableId("need.test"),
        need_facet_id=facet_id,
        facet_kind=NeedFacetKind.CURRENT_STATE,
        mandatory=True,
        status=FacetClosureStatus.SUPPORTED,
        supporting_unit_ids=(StableId("unit.state"),),
    )
    return RetrievalTrace(
        need_id=StableId("need.test"),
        intent=Stage1QueryIntent.CURRENT_STATE,
        allowed_channels=(RetrievalChannel.R1_EXACT,),
        channel_candidate_counts={RetrievalChannel.R1_EXACT: 1},
        candidates=(),
        fusion_applied=False,
        stop_reason=RetrievalStopReason.EXACT_SATISFIED,
        required_need_facet_ids=(facet_id,),
        closed_need_facet_ids=(facet_id,),
        facet_receipts=(receipt,),
    )


def _arm(arm: ControllerArm) -> PairedContextArmResult:
    return PairedContextArmResult(
        arm=arm,
        execution_status=ArmExecutionStatus.COMPLETED,
        context=Stage1ContextPackage(
            context_id=StableId("context.test"),
            base_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            task_contract="frozen read boundary contract",
            retrieval_traces=(_trace(),),
            budget_report=ContextBudgetReport(
                token_budget=1000,
                mandatory_tokens=0,
                optional_tokens=0,
                full_chapter_read_count=0,
            ),
        ),
        selected_unit_ids=(),
        retrieval_call_count=1,
        stop_reason=ControllerStopReason.SUFFICIENT,
        comparison_basis_fingerprint=FINGERPRINT,
        future_leakage_count=0,
    )


def _comparison() -> PairedContextComparison:
    planner_ref = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "3" * 64),
        media_type="application/vnd.novel-agent.planner-invocation+json",
        byte_length=1,
        schema_version="1.0.0",
    )
    return PairedContextComparison(
        pair_id=StableId("pair.frozen-read"),
        request_id=StableId("request.frozen-read"),
        deterministic=_arm(ControllerArm.DETERMINISTIC),
        agentic=_arm(ControllerArm.BOUNDED_R2),
        comparable=True,
        generated_needs=(),
        planner_artifact_ref=planner_ref,
    )


def _need_based_comparison() -> PairedContextComparison:
    """Comparison whose generated Needs are bound to the checkpoint commit."""
    from novel_agent.domain.memory import (
        CandidatePool,
        NeedRisk,
        ResolutionPath,
    )

    need = Stage1MemoryNeed(
        need_id=StableId("need.test"),
        run_id=RunId("run.test"),
        task_id=TaskId("task.test"),
        base_commit=COMMIT,
        horizon_target=(21, 25),
        need_type="test",
        query_intent=Stage1QueryIntent.CURRENT_STATE,
        query_text="test",
        entity_ids=(),
        access_scope="writer_safe",
        why_needed="test",
        risk_level=NeedRisk.MEDIUM,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
        allowed_candidate_pools=(CandidatePool.R1,),
        stop_condition="test",
        need_facets=(),
        completion_spec=None,
    )
    return _comparison().model_copy(update={"generated_needs": (need,)})


def _canonical(comparison: PairedContextComparison) -> bytes:
    return canonical_json_bytes(comparison.model_dump(mode="json"))


def test_accepted_canonical_comparison_reads_lax_and_round_trips() -> None:
    comparison = _need_based_comparison()
    parsed, needs, planner_ref = _parse_frozen_comparison(_canonical(comparison), COMMIT)
    assert parsed.pair_id == comparison.pair_id
    assert len(needs) == 1
    assert planner_ref is not None
    # The trace with a before-validator path round-trips through the receipt.
    trace = parsed.deterministic.context.retrieval_traces[0]
    assert trace.facet_receipts[0].status is FacetClosureStatus.SUPPORTED


def test_rejected_canonical_drift() -> None:
    comparison = _need_based_comparison()
    original = _canonical(comparison)
    # A legacy writer may emit the same payload with non-canonical key order:
    # still valid JSON and still parseable, but not a canonical dump of the
    # current schema, so it must be rejected as stale/drifted.
    data = json.loads(original)
    reordered = json.dumps(
        {key: data[key] for key in reversed(list(data.keys()))},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert reordered != original
    with pytest.raises(ValueError, match="not a canonical dump"):
        _parse_frozen_comparison(reordered, COMMIT)


def test_rejected_commit_mismatch() -> None:
    comparison = _need_based_comparison()
    with pytest.raises(ValueError, match="do not match the checkpoint commit"):
        _parse_frozen_comparison(_canonical(comparison), OTHER_COMMIT)


def test_rejected_commit_mismatch_on_context_basis() -> None:
    # Needs match the checkpoint commit but the deterministic/agentic context
    # base commits do not: both arms must be rebound together to stay a valid
    # comparison (paired arms must share a canonical basis).
    comparison = _need_based_comparison()
    comparison = comparison.model_copy(
        update={
            "deterministic": _arm(ControllerArm.DETERMINISTIC).model_copy(
                update={
                    "context": comparison.deterministic.context.model_copy(
                        update={"base_commit": OTHER_COMMIT}
                    )
                }
            ),
            "agentic": _arm(ControllerArm.BOUNDED_R2).model_copy(
                update={
                    "context": comparison.agentic.context.model_copy(
                        update={"base_commit": OTHER_COMMIT}
                    )
                }
            ),
        }
    )
    with pytest.raises(ValueError, match="does not match the checkpoint commit"):
        _parse_frozen_comparison(_canonical(comparison), COMMIT)


def test_missing_planner_ref_is_rejected() -> None:
    comparison = _need_based_comparison().model_copy(update={"planner_artifact_ref": None})
    with pytest.raises(ValueError, match="no Planner artifact ref"):
        _parse_frozen_comparison(_canonical(comparison), COMMIT)


def test_load_checkpoint_index_accepts_canonical_entry() -> None:
    path = Path("tmp") / "frozen-read-test-index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "frozen_checkpoint_inputs": [
                    {
                        "case_id": "ZTJ-P001",
                        "checkpoint_chapter": 20,
                        "commit": COMMIT.root,
                        "comparison_ref": {
                            "artifact_id": "sha256:" + "3" * 64,
                            "media_type": "application/vnd.novel-agent.frozen-paired-context+json",
                            "byte_length": 1,
                            "schema_version": "1.0.0",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    try:
        resolved = _load_checkpoint_index(path)
        chapter, commit, comparison_ref = resolved["ZTJ-P001"]
        assert chapter == 20
        assert commit == COMMIT.root
        assert comparison_ref.artifact_id.root == "sha256:" + "3" * 64
    finally:
        path.unlink(missing_ok=True)


def test_load_checkpoint_index_rejects_incomplete_entry() -> None:
    path = Path("tmp") / "frozen-read-bad-index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"frozen_checkpoint_inputs": [{"case_id": "ZTJ-P001", "checkpoint_chapter": 20}]}
        ),
        encoding="utf-8",
    )
    try:
        with pytest.raises(ValueError, match="entry is incomplete"):
            _load_checkpoint_index(path)
    finally:
        path.unlink(missing_ok=True)


def test_planner_invocation_artifact_remains_strictly_readable() -> None:
    # The Planner artifact has no before-validator, so strict reading stays
    # valid for it; keep that boundary pinned as well.
    from novel_agent.domain.benchmark import AuthorPlanningContext
    from novel_agent.domain.planning_memory import (
        PlannerArtifactMetadata,
        PlannerFallbackStatus,
        PlannerWorldSummary,
    )
    from novel_agent.domain.stage2 import BenchmarkInformationProfile

    metadata = PlannerArtifactMetadata(
        run_id=RunId("run.test"),
        planner_model="test",
        planner_model_revision="test",
        planner_prompt_version="test",
        planner_prompt_hash=ArtifactId("sha256:" + "4" * 64),
        planner_output_schema_version="test",
        temperature=0.0,
        effective_seed_supported=False,
        planning_context_hash=ArtifactId("sha256:" + "5" * 64),
        world_summary_hash=ArtifactId("sha256:" + "6" * 64),
        raw_response_hash=ArtifactId("sha256:" + "7" * 64),
        validated_need_set_hash=ArtifactId("sha256:" + "8" * 64),
        fallback_used=True,
        input_tokens=0,
        output_tokens=0,
    )
    planning_context = AuthorPlanningContext(
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="test",
        target_range=(21, 25),
        source_hash=ArtifactId("sha256:" + "5" * 64),
    )
    world_summary = PlannerWorldSummary(
        checkpoint_chapter=20,
        target_range=(21, 25),
        task_intent="test",
        entity_count=0,
        state_count=0,
        event_count=0,
        relation_count=0,
        obligation_count=0,
    )
    planner = PlannerInvocationArtifact(
        planning_context=planning_context,
        world_summary=world_summary,
        exact_prompt="test",
        metadata=metadata,
        validated_need_set_hash=metadata.validated_need_set_hash,
        fallback_status=PlannerFallbackStatus.PLANNER_FALLBACK,
        fallback_reason="test",
    )
    parsed = PlannerInvocationArtifact.model_validate_json(
        canonical_json_bytes(planner.model_dump(mode="json")), strict=True
    )
    assert parsed.metadata is not None
    assert parsed.metadata.planner_model == "test"


def test_semantic_status_separates_closure_from_mechanical() -> None:
    # Review 2026-08-14 P1-2: a READY mechanical package with mandatory facet
    # gaps must report semantic INCOMPLETE, and COMPLETE only when closure is
    # complete.
    from scripts.run_evidence_first_frozen_checkpoints import _semantic_status

    assert _semantic_status("COMPLETE") == "COMPLETE"
    assert _semantic_status("INCOMPLETE") == "INCOMPLETE"
    assert _semantic_status("READY") == "INCOMPLETE"


def test_aggregate_semantic_status_requires_all_complete() -> None:
    # Review follow-up P2: the test must exercise the driver's production
    # aggregate helper, not a local re-implementation of all(...).
    from scripts.run_evidence_first_frozen_checkpoints import _aggregate_semantic_status

    entries_complete: list[dict[str, object]] = [
        {"semantic_status": "COMPLETE"},
        {"semantic_status": "COMPLETE"},
    ]
    entries_with_gap: list[dict[str, object]] = [
        {"semantic_status": "COMPLETE"},
        {"semantic_status": "INCOMPLETE"},
    ]
    assert _aggregate_semantic_status(entries_complete) == "COMPLETE"
    assert _aggregate_semantic_status(entries_with_gap) == "INCOMPLETE"
    assert _aggregate_semantic_status([]) == "COMPLETE"


def test_aggregate_mechanical_status_uses_driver_helper() -> None:
    # The aggregate mechanical gate is exercised through the driver helper:
    # READY-with-zero-failures passes, any failure fails, and it stays separate
    # from semantic closure (a mechanically PASSing run may be semantically
    # INCOMPLETE, review 2026-08-14 P1-2).
    from scripts.run_evidence_first_frozen_checkpoints import _aggregate_mechanical_status

    ready: list[dict[str, object]] = [
        {
            "readiness_status": "READY",
            "dereference_failures": 0,
            "scope_failures": 0,
            "cutoff_failures": 0,
            "leakage_failures": 0,
            "root_hashes_unchanged": True,
        },
        {
            "readiness_status": "READY",
            "dereference_failures": 0,
            "scope_failures": 0,
            "cutoff_failures": 0,
            "leakage_failures": 0,
            "root_hashes_unchanged": True,
        },
    ]
    one_leakage: list[dict[str, object]] = [
        {
            "readiness_status": "READY",
            "dereference_failures": 0,
            "scope_failures": 0,
            "cutoff_failures": 0,
            "leakage_failures": 1,
            "root_hashes_unchanged": True,
        },
        {
            "readiness_status": "READY",
            "dereference_failures": 0,
            "scope_failures": 0,
            "cutoff_failures": 0,
            "leakage_failures": 0,
            "root_hashes_unchanged": True,
        },
    ]
    not_ready: list[dict[str, object]] = [
        {
            "readiness_status": "EVIDENCE_INSUFFICIENT",
            "dereference_failures": 0,
            "scope_failures": 0,
            "cutoff_failures": 0,
            "leakage_failures": 0,
            "root_hashes_unchanged": True,
        }
    ]
    assert _aggregate_mechanical_status(ready) == "PASS"
    assert _aggregate_mechanical_status(one_leakage) == "FAIL"
    assert _aggregate_mechanical_status(not_ready) == "FAIL"
