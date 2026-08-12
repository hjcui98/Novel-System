from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef, PlanRootRef
from novel_agent.domain.benchmark import EvaluatorCase
from novel_agent.domain.canonical import CanonicalAliasReceipt
from novel_agent.domain.gates import (
    Stage2RetrievalCheckpointEvidence,
    Stage2RetrievalGateReport,
)
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    NeedCompletionSpec,
    NeedExecutionStatus,
    NeedFacet,
    RetrievalTrace,
    RetrievalUnit,
    Stage1ContextPackage,
    Stage1MemoryNeed,
)
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    ContextAssemblyStatus,
)
from novel_agent.domain.stage2 import (
    ArmExecutionStatus,
    ContextAssemblySpec,
    PairedContextArmResult,
    PairedContextComparison,
    PublicCheckpointCase,
)
from novel_agent.domain.text import QuoteHash
from novel_agent.domain.writer_context import (
    ClaimSupportReceipt,
    ContextLineage,
    EvidenceResolutionStatus,
    SemanticSupportStatus,
)
from tests.fixtures.stage2_memory_benchmark import (
    frozen_evaluation_inputs,
    resolved_public_comparison,
    writer_context_inputs,
)
from tests.unit.test_stage2_retrieval_gate_evaluation import _checkpoint, _report


def test_gold_profiles_and_private_evaluator_case_require_unique_nonempty_items() -> None:
    gold, _package, _ledger, _receipt = frozen_evaluation_inputs()
    with pytest.raises(ValidationError, match="profiles must be unique"):
        type(gold).model_validate(
            gold.model_dump()
            | {
                "applicable_profiles": (
                    BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                    BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                )
            }
        )
    base = {
        "case_id": StableId("case.evaluator"),
        "public_case_hash": gold.future_evidence_refs[0].object_hash,
        "gold_items": (gold,),
        "future_text_root_private": gold.future_evidence_refs[0].root_hash,
        "evaluator_manifest_hash": gold.future_evidence_refs[0].object_hash,
    }
    with pytest.raises(ValidationError, match="at least one Gold"):
        EvaluatorCase(**(base | {"gold_items": ()}))
    with pytest.raises(ValidationError, match="Gold ids must be unique"):
        EvaluatorCase(**(base | {"gold_items": (gold, gold)}))
    assert EvaluatorCase(**base).gold_items == (gold,)


def test_memory_need_focus_ids_must_be_unique() -> None:
    _task, needs, _units, _commit = writer_context_inputs()
    need = needs[0]
    with pytest.raises(ValidationError, match="focus ids must be unique"):
        Stage1MemoryNeed.model_validate(
            need.model_dump() | {"focus_ids": (need.focus_ids[0], need.focus_ids[0])}
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("checkpoint", "checkpoint does not match"),
        ("target", "target range does not match"),
        ("visible_plan", "cannot expose a PlanRoot"),
    ),
)
def test_public_checkpoint_contract_rejects_profile_inconsistency(
    mutation: str,
    message: str,
) -> None:
    _bundle, _private, public, _runner, _comparison = resolved_public_comparison()
    payload = public.model_dump()
    if mutation == "checkpoint":
        payload["history_range"] = (1, 19)
    elif mutation == "target":
        payload["target_range"] = (21, 24)
    else:
        _gold, package, _ledger, _receipt = frozen_evaluation_inputs()
        payload["plan_root_ref"] = PlanRootRef(**package.evidence_ledger_ref.model_dump())
    with pytest.raises(ValidationError, match=message):
        PublicCheckpointCase.model_validate(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("missing_ledger", "must appear together"),
        ("missing_status", "requires an assembly status"),
        ("nonready_eligible", "cannot be quality eligible"),
        ("ineligible_no_failure", "requires a failure category"),
    ),
)
def test_paired_arm_writer_artifacts_are_atomic(
    mutation: str,
    message: str,
) -> None:
    _bundle, _private, _public, _runner, comparison = resolved_public_comparison()
    payload = comparison.deterministic.model_dump()
    if mutation == "missing_ledger":
        payload["evidence_ledger"] = None
    elif mutation == "missing_status":
        payload["assembly_status"] = None
    elif mutation == "nonready_eligible":
        payload["assembly_status"] = ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
    else:
        payload["quality_eligible"] = False
        payload["failure_category"] = None
    with pytest.raises(ValidationError, match=message):
        PairedContextArmResult.model_validate(payload)


def test_arm_c_writer_artifacts_are_atomic() -> None:
    _bundle, _private, _public, _runner, comparison = resolved_public_comparison()
    with pytest.raises(ValidationError, match="Arm C"):
        PairedContextComparison.model_validate(
            comparison.model_dump() | {"arm_c_evidence_ledger": None}
        )


def test_public_need_retrieval_and_lineage_contract_edges() -> None:
    _task, needs, units, _commit = writer_context_inputs()
    need = next(item for item in needs if item.completion_spec is not None)
    assert need.completion_spec is not None and need.need_facets
    facet = need.need_facets[0]
    spec = need.completion_spec
    with pytest.raises(ValidationError, match="public derivation"):
        NeedFacet.model_validate(facet.model_dump() | {"derivation_refs": ()})
    with pytest.raises(ValidationError, match="derivation refs must be unique"):
        NeedFacet.model_validate(
            facet.model_dump()
            | {"derivation_refs": (facet.derivation_refs[0], facet.derivation_refs[0])}
        )
    for spec_update, message in (
        ({"required_need_facet_ids": ()}, "at least one facet"),
        (
            {
                "required_need_facet_ids": (
                    spec.required_need_facet_ids[0],
                    spec.required_need_facet_ids[0],
                )
            },
            "must be unique",
        ),
        (
            {"irreducible_need_facet_ids": (StableId("facet.unknown"),)},
            "must be required",
        ),
        ({"evidence_requirement_by_facet": {}}, "cover every required"),
    ):
        with pytest.raises(ValidationError, match=message):
            NeedCompletionSpec.model_validate(spec.model_dump() | spec_update)

    unknown_facet = StableId("facet.unknown")
    unknown_spec = spec.model_copy(
        update={
            "required_need_facet_ids": (unknown_facet,),
            "irreducible_need_facet_ids": (),
            "evidence_requirement_by_facet": {
                unknown_facet.root: next(iter(spec.evidence_requirement_by_facet.values()))
            },
        }
    )
    for need_update, message in (
        ({"need_facets": ()}, "appear together"),
        (
            {"completion_spec": spec.model_copy(update={"need_id": StableId("need.other")})},
            "need id mismatch",
        ),
        ({"need_facets": (facet, facet)}, "facet ids must be unique"),
        (
            {"need_facets": (facet.model_copy(update={"need_id": StableId("need.other")}),)},
            "NeedFacet need id mismatch",
        ),
        (
            {"completion_spec": unknown_spec},
            "unknown NeedFacet",
        ),
        (
            {
                "allow_plan": False,
                "planner_may_read_plan": False,
                "retrieval_may_return_plan": False,
                "claim_may_cite_plan": False,
                "legacy_allow_plan": False,
                "need_facets": tuple(
                    item.model_copy(update={"information_scope": "author_plan"})
                    if item.need_facet_id == facet.need_facet_id
                    else item
                    for item in need.need_facets
                ),
            },
            "plan-derived NeedFacet",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            Stage1MemoryNeed.model_validate(need.model_dump() | need_update)

    unit = next(item for item in units if item.canonical_value_id is not None)
    fake_ref = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "0" * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )
    for unit_update, message in (
        ({"canonicalizer_version": None}, "appear together"),
        (
            {
                "canonical_value_id": None,
                "canonicalizer_version": None,
                "canonical_alias_receipt_ref": fake_ref,
            },
            "requires canonical value",
        ),
        ({"narrative_start": 2, "narrative_end": 1}, "narrative end"),
        ({"story_time_start": 2, "story_time_end": 1}, "story time end"),
        (
            {"parent_unit_ids": (StableId("unit.same"), StableId("unit.same"))},
            "parent ids must be unique",
        ),
        (
            {
                "source_refs": (
                    ArtifactId("sha256:" + "1" * 64),
                    ArtifactId("sha256:" + "1" * 64),
                )
            },
            "source refs must be unique",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            RetrievalUnit.model_validate(unit.model_dump() | unit_update)

    _bundle, _private, _public, _runner, comparison = resolved_public_comparison()
    trace = comparison.deterministic.context.retrieval_traces[0]
    with pytest.raises(ValidationError, match="allocated retrieval calls"):
        RetrievalTrace.model_validate(
            trace.model_dump()
            | {
                "need_execution_status": NeedExecutionStatus.NOT_EXECUTED_BUDGET_EXHAUSTED,
                "calls_allocated": 1,
                "candidates": (),
            }
        )
    with pytest.raises(ValidationError, match="retrieval candidates"):
        RetrievalTrace.model_validate(
            trace.model_dump()
            | {
                "need_execution_status": NeedExecutionStatus.NOT_EXECUTED_BUDGET_EXHAUSTED,
                "calls_allocated": 0,
            }
        )
    with pytest.raises(ValidationError, match="requires candidates"):
        RetrievalTrace.model_validate(
            trace.model_dump()
            | {
                "need_execution_status": NeedExecutionStatus.EXECUTED_WITH_CANDIDATES,
                "candidates": (),
            }
        )
    with pytest.raises(ValidationError, match="cannot contain candidates"):
        RetrievalTrace.model_validate(
            trace.model_dump() | {"need_execution_status": NeedExecutionStatus.EXECUTED_EMPTY}
        )
    with pytest.raises(ValidationError, match="closed Need facets"):
        RetrievalTrace.model_validate(
            trace.model_dump()
            | {
                "required_need_facet_ids": (),
                "closed_need_facet_ids": (StableId("facet.closed"),),
            }
        )
    inferred = RetrievalTrace.model_validate(
        {key: value for key, value in trace.model_dump().items() if key != "need_execution_status"}
    )
    assert inferred.need_execution_status is NeedExecutionStatus.EXECUTED_WITH_CANDIDATES
    assert RetrievalTrace.model_validate(trace).need_id == trace.need_id
    with pytest.raises(ValidationError, match="valid dictionary"):
        RetrievalTrace.model_validate("not-a-mapping")

    context = comparison.deterministic.context
    with pytest.raises(ValidationError, match="cover same Needs"):
        Stage1ContextPackage.model_validate(context.model_dump() | {"need_completion_specs": ()})
    with pytest.raises(ValidationError, match="raw values cannot be empty"):
        CanonicalAliasReceipt(
            receipt_id=StableId("alias.empty"),
            registry_version="v1",
            predicate="attitude",
            raw_values=("", "value"),
            canonical_value_id=StableId("canonical.value"),
            canonicalizer_version="v1",
        )
    with pytest.raises(ValidationError, match="distinct raw values"):
        CanonicalAliasReceipt(
            receipt_id=StableId("alias.same"),
            registry_version="v1",
            predicate="attitude",
            raw_values=("value", "value"),
            canonical_value_id=StableId("canonical.value"),
            canonicalizer_version="v1",
        )
    with pytest.raises(ValidationError, match="appear together"):
        ContextLineage(
            assembler_version="v1",
            normalized_unit_count=0,
            canonical_alias_receipts=(),
            canonical_alias_receipt_refs=(fake_ref,),
        )


def test_support_assembly_and_typed_arm_contract_edges() -> None:
    _bundle, _private, _public, _runner, comparison = resolved_public_comparison()
    deterministic = comparison.deterministic
    spec = deterministic.context_assembly_spec
    assert spec is not None
    selected_group = spec.selected_support_group_ids[0]
    allowed_variant = next(iter(spec.allowed_claim_variant_ids_by_support_group.values()))[0]
    unknown_receipt_ref = deterministic.support_receipt_refs[0].model_copy(
        update={"artifact_id": ArtifactId("sha256:" + "f" * 64)}
    )
    unavailable_group = deterministic.claim_support_groups[0].model_copy(
        update={"support_receipt_ref": unknown_receipt_ref}
    )
    for assembly_update, message in (
        (
            {"mandatory_support_group_ids": (StableId("support.unknown"),)},
            "mandatory support groups",
        ),
        (
            {"ordered_optional_support_group_ids": (StableId("support.unknown"),)},
            "optional support groups",
        ),
        ({"writer_token_budget": None}, "Writer token budget"),
        ({"allowed_claim_variant_ids_by_support_group": {}}, "variants for every group"),
        (
            {"mandatory_claim_variant_ids": (StableId("variant.unknown"),)},
            "mandatory claim variants",
        ),
        (
            {
                "closed_need_facet_ids": (StableId("facet.same"),),
                "unresolved_need_facet_ids": (StableId("facet.same"),),
            },
            "must be disjoint",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            ContextAssemblySpec.model_validate(spec.model_dump() | assembly_update)
    assert selected_group and allowed_variant

    for arm_update, message in (
        ({"calls_allocated_by_need": {"need": -1}}, "non-negative"),
        (
            {"calls_allocated_by_need": {"need": deterministic.retrieval_call_count + 1}},
            "exceed",
        ),
        (
            {
                "mandatory_need_facets_total": 0,
                "mandatory_need_facets_closed": 1,
            },
            "closed mandatory",
        ),
        (
            {"context_assembly_spec_ref": None},
            "content-addressed ref",
        ),
        ({"support_receipt_refs": ()}, "appear together"),
        (
            {"claim_support_groups": (unavailable_group,)},
            "unavailable receipt",
        ),
        (
            {"selected_claim_variant_ids": (StableId("variant.unknown"),)},
            "not embedded",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            PairedContextArmResult.model_validate(deterministic.model_dump() | arm_update)

    skipped_payload = comparison.agentic.model_dump() | {
        "execution_status": ArmExecutionStatus.SKIPPED,
        "quality_eligible": False,
        "failure_category": "NOT_RUN",
        "writer_context": None,
        "evidence_ledger": None,
        "assembly_status": None,
        "retrieval_call_count": 0,
        "calls_allocated_by_need": {},
    }
    for skipped_update, message in (
        ({"quality_eligible": True}, "cannot be quality eligible"),
        (
            {
                "writer_context": deterministic.writer_context,
                "evidence_ledger": deterministic.evidence_ledger,
                "assembly_status": ContextAssemblyStatus.READY,
            },
            "cannot expose Writer",
        ),
        ({"retrieval_call_count": 1}, "successful retrieval calls"),
    ):
        with pytest.raises(ValidationError, match=message):
            PairedContextArmResult.model_validate(skipped_payload | skipped_update)

    completed_c = comparison
    assert completed_c.arm_c_writer_context is not None
    for comparison_update, message in (
        ({"arm_c_writer_context": None, "arm_c_evidence_ledger": None}, "requires frozen"),
        ({"arm_c_failure_category": "FAILED"}, "cannot carry"),
        (
            {"arm_c_execution_status": ArmExecutionStatus.SKIPPED},
            "cannot expose Writer",
        ),
        (
            {
                "arm_c_writer_context": None,
                "arm_c_evidence_ledger": None,
                "arm_c_status": None,
                "arm_c_execution_status": ArmExecutionStatus.SKIPPED,
                "arm_c_failure_category": None,
            },
            "requires a typed failure",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            PairedContextComparison.model_validate(completed_c.model_dump() | comparison_update)

    receipt = deterministic.support_receipts[0]
    with pytest.raises(ValidationError, match="requires evidence"):
        ClaimSupportReceipt.model_validate(
            receipt.model_dump() | {"evidence_refs": (), "plan_node_ids": ()}
        )
    for receipt_update in (
        {"evidence_resolution_status": EvidenceResolutionStatus.UNRESOLVED},
        {"counter_evidence_refs": receipt.evidence_refs},
        {"need_facet_ids": ()},
    ):
        with pytest.raises(ValidationError, match="verified support"):
            ClaimSupportReceipt.model_validate(receipt.model_dump() | receipt_update)
    with pytest.raises(ValidationError, match="complete audit binding"):
        ClaimSupportReceipt.model_validate(
            receipt.model_dump() | {"producer_input_hash": ArtifactId("sha256:" + "2" * 64)}
        )
    with pytest.raises(ValidationError, match="semantic verification"):
        ClaimSupportReceipt.model_validate(
            receipt.model_dump() | {"verifier_input_hash": ArtifactId("sha256:" + "3" * 64)}
        )
    assert receipt.semantic_support_status is SemanticSupportStatus.VERIFIED


def test_retrieval_gate_enforces_exact_pass_evidence_and_report_status() -> None:
    checkpoint = _checkpoint(20)
    cases: tuple[tuple[dict[str, object], str], ...] = (
        ({"snapshot_id": None}, "exact snapshot"),
        ({"index_targets": {}}, "Anchor and Grounded"),
        ({"index_totals": {}}, "Anchor and Grounded"),
        (
            {
                "index_targets": checkpoint.index_targets
                | {next(iter(checkpoint.index_targets)): ""}
            },
            "non-empty",
        ),
    )
    for update, message in cases:
        with pytest.raises(ValidationError, match=message):
            Stage2RetrievalCheckpointEvidence.model_validate(checkpoint.model_dump() | update)
    report = _report()
    with pytest.raises(ValidationError, match="status must agree"):
        Stage2RetrievalGateReport.model_validate(report.model_dump() | {"status": "failed"})


def test_graph_path_receipt_validator_branches() -> None:
    """Round 1 graph receipt domain: every path-shape failure is fail-closed."""
    from novel_agent.domain.ids import CommitId
    from novel_agent.domain.memory import GraphPathDereferenceStatus, GraphPathReceipt
    from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
    from novel_agent.domain.world import StoryTime

    commit = CommitId("sha256:" + "7" * 64)
    snapshot = StableId("snapshot.receipt")
    seed = StableId("entity.seed")
    mid = StableId("entity.mid")
    valid_time = (StoryTime(worldline="main", start_ordinal=1, end_ordinal=5),)
    evidence = EvidenceRef(
        evidence_id=StableId("evidence.path"),
        root_hash=ArtifactId("sha256:" + "8" * 64),
        object_hash=ArtifactId("sha256:" + "9" * 64),
        chapter_id=StableId("chapter.test.5"),
        scene_id=StableId("scene.test.5.0"),
        span=TextSpanRef(block_id=StableId("block.test.5.0"), start=0, end=3),
        quote_hash=QuoteHash("sha256:" + "a" * 64),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=commit,
    )

    def receipt(**updates: object) -> GraphPathReceipt:
        base: dict[str, object] = {
            "path_id": StableId("path.1"),
            "source_commit": commit,
            "snapshot_id": snapshot,
            "seed_entity_ids": (seed,),
            "relation_row_ids": (StableId("row.1"),),
            "relation_ids": (StableId("rel.1"),),
            "entity_path": (seed, mid),
            "predicates": ("knows",),
            "directions": ("forward",),
            "valid_time": valid_time,
            "edge_semantics": ("canonical",),
            "evidence_refs": (evidence,),
            "dereference_status": GraphPathDereferenceStatus.L0_VERIFIED,
        }
        base.update(updates)
        return GraphPathReceipt.model_validate(base)

    assert receipt().dereference_status is GraphPathDereferenceStatus.L0_VERIFIED
    with pytest.raises(ValidationError, match="edge metadata lengths"):
        receipt(predicates=("knows", "lives_in"))
    with pytest.raises(ValidationError, match="entity count must be edge count plus one"):
        receipt(entity_path=(seed, mid, mid))
    with pytest.raises(ValidationError, match="must start at one of its declared seeds"):
        receipt(entity_path=(mid, seed))
    with pytest.raises(ValidationError, match="must be forward or reverse"):
        receipt(directions=("sideways",))
    with pytest.raises(ValidationError, match="only permits canonical edge semantics"):
        receipt(edge_semantics=("inferred",))
