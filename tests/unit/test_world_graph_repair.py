from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.adapters.model.scripted import ScriptedModelEndpoint
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import (
    CuratedOperationDraftV2,
    EvidenceCandidate,
    EvidenceSupportDecision,
    EvidenceSupportDisposition,
)
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    GraphPathDereferenceStatus,
    RetrievalUnitKind,
    WorldRootDocument,
)
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
    ModelUsage,
    ProviderModelResult,
)
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
from novel_agent.domain.world import (
    Entity,
    EntityResolutionStatus,
    RelationBackfillStatus,
    StateRecord,
    StoryTime,
    TruthClass,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import (
    canonical_json_bytes,
    quote_hash,
    world_root_content_id,
)
from novel_agent.services.evidence_candidates import EvidenceCandidateGenerator
from novel_agent.services.evidence_support import EvidenceSupportGate
from novel_agent.services.memory_pipeline import AnchorBuilder
from novel_agent.services.model_curation import ModelCurator
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.overlay import build_candidate_bundle
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.validation import Stage1Validator
from novel_agent.services.world_graph import (
    EntityAliasRepairPolicy,
    PredicateRegistry,
    WorldGraphExtractionPass,
)
from tests.factories import make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def _teacher_world() -> tuple[WorldRootDocument, TextRootDocument, StableId, StableId]:
    bundle = make_synthetic_bundle()
    text = bundle.text_roots[0]
    base = bundle.world_roots[0]
    student_id = base.entities[0].entity_id
    teacher_id = StableId("entity.synthetic.old-vow")
    teacher = Entity(
        entity_id=teacher_id,
        entity_type="character",
        internal_label="旧誓言",
        aliases=("誓言",),
    )
    block = text.chapters[4].scenes[0].blocks[0]
    evidence = EvidenceRef(
        evidence_id=StableId("evidence.synthetic.teacher"),
        root_hash=text.root_hash,
        object_hash=sha256_id(block.text.encode("utf-8")),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=TextSpanRef(block_id=block.block_id, start=0, end=len(block.text)),
        quote_hash=quote_hash(block.text),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=base.source_commit,
    )
    state = StateRecord(
        state_id=StableId("state.synthetic.teacher"),
        subject_id=student_id,
        predicate="teacher",
        value="旧誓言",
        valid_time=StoryTime(worldline="main", start_ordinal=5),
        evidence_refs=(evidence,),
        truth_class=TruthClass.ASSERTION,
    )
    provisional = base.model_copy(
        update={
            "root_hash": ArtifactId("sha256:" + "0" * 64),
            "entities": (*base.entities, teacher),
            "states": (state,),
            "relations": (),
        }
    )
    world = provisional.model_copy(update={"root_hash": world_root_content_id(provisional)})
    return world, text, teacher_id, student_id


def _world_with_state_truth(
    world: WorldRootDocument,
    truth_class: TruthClass,
) -> WorldRootDocument:
    provisional = world.model_copy(
        update={
            "root_hash": ArtifactId("sha256:" + "0" * 64),
            "states": tuple(
                state.model_copy(update={"truth_class": truth_class}) for state in world.states
            ),
        }
    )
    return provisional.model_copy(update={"root_hash": world_root_content_id(provisional)})


def test_predicate_registry_is_bounded_and_records_ownership_metadata() -> None:
    registry = PredicateRegistry()

    assert len(registry.definitions) == 18
    assert registry.require("teacher_of").current_caller == "WorldGraphExtractionPass"
    assert registry.require("teacher_of").inverse_predicate == "student_of"
    assert registry.require("teacher_of").example_evidence_ref


def test_alias_policy_prefers_unique_canonical_label_and_reports_alias_collision() -> None:
    world, _, _, student_id = _teacher_world()
    first = Entity(
        entity_id=StableId("entity.synthetic.alias-one"),
        entity_type="character",
        internal_label="甲",
        aliases=("林澈", "殿下"),
    )
    second = Entity(
        entity_id=StableId("entity.synthetic.alias-two"),
        entity_type="character",
        internal_label="乙",
        aliases=("殿下",),
    )
    collided = world.model_copy(update={"entities": (*world.entities, first, second)})
    policy = EntityAliasRepairPolicy()

    canonical = policy.resolve(collided, "林澈")
    ambiguous = policy.resolve(collided, "殿下")

    assert canonical.status is EntityResolutionStatus.UNIQUE_LABEL
    assert canonical.resolved_entity_id == student_id
    assert ambiguous.status is EntityResolutionStatus.AMBIGUOUS
    assert set(ambiguous.matched_entity_ids) == {first.entity_id, second.entity_id}


def test_world_graph_pass_backfills_evidence_relation_without_removing_state() -> None:
    world, text, teacher_id, student_id = _teacher_world()

    result = WorldGraphExtractionPass().run(world, text)

    assert world.relations == ()
    assert result.repaired_world.root_hash != world.root_hash
    assert result.repaired_world.states == world.states
    assert len(result.repaired_world.relations) == 1
    relation = result.repaired_world.relations[0]
    assert (relation.subject_id, relation.predicate, relation.object_id) == (
        teacher_id,
        "teacher_of",
        student_id,
    )
    assert relation.truth_class is TruthClass.ASSERTION
    assert result.receipt.candidates[0].status is RelationBackfillStatus.ACCEPTED


def test_world_graph_state_audit_uses_unique_aliases_present_in_evidence() -> None:
    world, text, teacher_id, student_id = _teacher_world()
    entities = tuple(
        entity.model_copy(
            update={
                "internal_label": f"canonical-{index}",
                "aliases": (*entity.aliases, entity.internal_label),
            }
        )
        if entity.entity_id in {teacher_id, student_id}
        else entity
        for index, entity in enumerate(world.entities)
    )
    alias_world = world.model_copy(update={"entities": entities})

    result = WorldGraphExtractionPass().run(alias_world, text)

    assert len(result.repaired_world.relations) == 1
    relation = result.repaired_world.relations[0]
    assert (relation.subject_id, relation.object_id) == (teacher_id, student_id)
    receipt = result.receipt.candidates[0]
    assert receipt.subject_resolution is not None
    assert receipt.object_resolution is not None
    assert receipt.subject_resolution.status is EntityResolutionStatus.UNIQUE_ALIAS
    assert receipt.object_resolution.status is EntityResolutionStatus.UNIQUE_ALIAS


def test_frozen_checkpoint_readiness_marks_mechanical_failures() -> None:
    from scripts.run_evidence_first_frozen_checkpoints import _readiness_status

    assert _readiness_status("READY", {}, 0) == "READY"
    assert _readiness_status("READY", {"dereference": 1}, 0) == "MECHANICAL_FAILURE"
    assert _readiness_status("READY", {}, 1) == "MECHANICAL_FAILURE"
    assert _readiness_status("EVIDENCE_INSUFFICIENT", {}, 0) == "EVIDENCE_INSUFFICIENT"


def test_stage1_validator_independently_rejects_unregistered_relation() -> None:
    world, text, _, _ = _teacher_world()
    extraction = WorldGraphExtractionPass().run(world, text)
    relation = extraction.repaired_world.relations[0].model_copy(
        update={"predicate": "invented_relation"}
    )
    provisional = extraction.repaired_world.model_copy(update={"relations": (relation,)})
    proposed = provisional.model_copy(update={"root_hash": world_root_content_id(provisional)})
    operation = extraction.change_set.operations[0]
    assert isinstance(operation.payload, dict)
    payload = dict(operation.payload)
    assert isinstance(payload["record"], dict)
    record = dict(payload["record"])
    record["predicate"] = relation.predicate
    payload["record"] = record
    changes = extraction.change_set.model_copy(
        update={"operations": (operation.model_copy(update={"payload": payload}),)}
    )
    bundle = build_candidate_bundle(
        project_id=ProjectId("project.graph-validator"),
        run_id=RunId("run.graph-validator"),
        current_manifest=make_manifest(),
        changes=changes,
        proposed_world=proposed,
    )

    report = Stage1Validator().validate(bundle, world, proposed, text)

    assert report.status.value == "failed"
    assert "RELATION_PREDICATE_INVALID" in {finding.code for finding in report.findings}
    assert "OVERLAY_MISMATCH" not in {finding.code for finding in report.findings}


@pytest.mark.parametrize(
    ("variant", "expected_code"),
    [
        ("missing_relation", "RELATION_WRITE_MISSING"),
        ("missing_endpoint", "RELATION_ENDPOINT_MISSING"),
        ("non_concrete_truth", "RELATION_TRUTH_NOT_CONCRETE"),
    ],
)
def test_stage1_validator_reports_each_relation_invariant(variant: str, expected_code: str) -> None:
    world, text, _, _ = _teacher_world()
    extraction = WorldGraphExtractionPass().run(world, text)
    operation = extraction.change_set.operations[0]
    relation = extraction.repaired_world.relations[0]
    if variant == "missing_relation":
        provisional = extraction.repaired_world.model_copy(update={"relations": ()})
        changes = extraction.change_set
    else:
        update = (
            {"subject_id": StableId("entity.missing")}
            if variant == "missing_endpoint"
            else {"truth_class": TruthClass.UNKNOWN}
        )
        relation = relation.model_copy(update=update)
        provisional = extraction.repaired_world.model_copy(update={"relations": (relation,)})
        assert isinstance(operation.payload, dict)
        payload = dict(operation.payload)
        payload["record"] = relation.model_dump(mode="json")
        changes = extraction.change_set.model_copy(
            update={"operations": (operation.model_copy(update={"payload": payload}),)}
        )
    proposed = provisional.model_copy(update={"root_hash": world_root_content_id(provisional)})
    bundle = build_candidate_bundle(
        project_id=ProjectId(f"project.graph-validator.{variant}"),
        run_id=RunId(f"run.graph-validator.{variant}"),
        current_manifest=make_manifest(),
        changes=changes,
        proposed_world=proposed,
    )

    report = Stage1Validator().validate(bundle, world, proposed, text)

    assert expected_code in {finding.code for finding in report.findings}


def test_model_curator_graph_profile_binds_quotes_and_host_admits_missing_entity() -> None:
    world, text, _, student_id = _teacher_world()
    quote = text.chapters[4].scenes[0].blocks[0].text
    response = json.dumps(
        {
            "status": "complete",
            "candidates": [
                {
                    "kind": "entity",
                    "surface": "北塔",
                    "entity_type": "location",
                    "evidence_quotes": [quote],
                },
                {
                    "kind": "relation",
                    "subject_surface": "林澈",
                    "predicate": "located_at",
                    "object_surface": "北塔",
                    "valid_time": {"worldline": "main", "start_ordinal": 5},
                    "source_truth_class": "accepted_world_fact",
                    "evidence_quotes": [quote],
                },
            ],
        },
        ensure_ascii=False,
    )
    endpoint = FakeModelEndpoint(response)
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="fake.graph",
                model_name="fake.graph",
                adapter=endpoint,
            ),
        )
    )
    request = ModelRequest(
        request_id=StableId("request.graph-profile"),
        run_id=RunId("run.graph-profile"),
        task_id=TaskId("task.graph-profile"),
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id="trace.graph-profile",
        prompt="",
        # Base e2e requests keep thinking disabled; the graph page sub-request
        # must override this with bounded thinking to satisfy the
        # relation-endpoint contract.
        enable_thinking=False,
        thinking_token_budget=None,
    )

    batches, _ = asyncio.run(
        ModelCurator(
            gateway,
            semantic_verifier=lambda _record, _candidate: (
                EvidenceSupportDisposition.SUPPORTS,
                "test_trusted_semantic_verifier",
            ),
        ).extract_graph_candidates(
            text,
            5,
            world.source_commit,
            world,
            request,
        )
    )
    batch = batches[0]
    result = WorldGraphExtractionPass().run(world, text, candidate_batches=batches)

    created = next(item for item in result.receipt.entity_admissions if item.surface == "北塔")
    accepted = next(
        item
        for item in result.receipt.candidates
        if item.candidate_id == batch.relations[0].candidate_id
    )
    assert created.entity_id is not None
    assert created.entity_id.root.startswith("entity.graph.")
    assert created.entity_id != student_id
    assert accepted.status is RelationBackfillStatus.ACCEPTED
    assert accepted.object_id == created.entity_id
    assert batch.model_request_id is not None
    assert batch.model_request_id.root.endswith(".u000.p00")
    assert batch.relations[0].evidence_refs[0].span is not None
    assert endpoint.requests[0].enable_thinking is True
    assert endpoint.requests[0].thinking_token_budget == 2048
    # Round-16 repair: graph pages carry a request-local repetition penalty
    # against runaway generation; the base request stays unchanged.
    assert endpoint.requests[0].repetition_penalty == 1.10
    assert request.repetition_penalty is None
    assert "every evidence_quote" in endpoint.requests[0].prompt
    assert "at most 12 candidates" in endpoint.requests[0].prompt
    assert (
        "Prioritize relation candidates over entity-only discovery" in endpoint.requests[0].prompt
    )
    assert "Never emit a standalone entity candidate" in endpoint.requests[0].prompt


def test_graph_profile_embeds_repair_feedback_in_page_prompt() -> None:
    """2026-08-14 corridor repair: graph pages must receive rejection feedback.

    ch2 receipts (§28.14) showed the graph profile repeated the missing-`kind`
    discriminator defect on every attempt because the retry sent an identical
    prompt with no feedback.  The graph page prompt must carry the repair
    contract with the precise validation paths when feedback is present.
    """
    world, text, _, _ = _teacher_world()
    response = json.dumps(
        {
            "status": "complete",
            "candidates": [],
            "no_graph_candidate_reason": "no durable relation in unit",
        },
        ensure_ascii=False,
    )
    endpoint = FakeModelEndpoint(response)
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="fake.graph",
                model_name="fake.graph",
                adapter=endpoint,
            ),
        )
    )
    request = ModelRequest(
        request_id=StableId("request.graph-repair"),
        run_id=RunId("run.graph-repair"),
        task_id=TaskId("task.graph-repair"),
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id="trace.graph-repair",
        prompt="",
        # The feedback/repair retry shares the graph page path and must keep
        # the bounded-thinking override on top of a thinking-disabled base.
        enable_thinking=False,
        thinking_token_budget=None,
    )
    feedback = (
        '{"reason_code":"CURATOR_PROPOSAL_SCHEMA_REJECTED",'
        '"validation_error_paths":["candidates.0","candidates.1"],'
        '"json_pointers":["/candidates/0","/candidates/1"],'
        '"violation_rule":"kind_discriminator_required"}'
    )
    asyncio.run(
        ModelCurator(gateway).extract_graph_candidates(
            text,
            5,
            world.source_commit,
            world,
            request,
            repair_feedback=feedback,
        )
    )
    prompt = endpoint.requests[0].prompt
    assert endpoint.requests[0].enable_thinking is True
    assert endpoint.requests[0].thinking_token_budget == 2048
    assert endpoint.requests[0].repetition_penalty == 1.10
    assert "MANDATORY_GRAPH_REPAIR_CONTRACT" in prompt
    assert prompt.rfind("<MANDATORY_GRAPH_REPAIR_CONTRACT") > prompt.rfind("</GRAPH_REPAIR_INPUT>")
    assert "kind=entity or kind=relation" in prompt
    assert "worldline=main" in prompt
    assert "CURATOR_PROPOSAL_SCHEMA_REJECTED" in prompt
    assert "/candidates/0" in prompt
    assert "kind_discriminator_required" in prompt


def test_model_graph_profile_support_gate_and_audit_paths() -> None:
    world, text, _, _ = _teacher_world()
    quote = text.chapters[4].scenes[0].blocks[0].text
    draft_response = json.dumps(
        {
            "status": "complete",
            "candidates": [
                {
                    "kind": "relation",
                    "subject_surface": "旧誓言",
                    "predicate": "teacher_of",
                    "object_surface": "林澈",
                    "valid_time": {"worldline": "main", "start_ordinal": 5},
                    "source_truth_class": "accepted_world_fact",
                    "evidence_quotes": [quote],
                }
            ],
        },
        ensure_ascii=False,
    )
    verifier_response = json.dumps(
        {
            "decisions": [
                {
                    "operation_index": 0,
                    "disposition": "supports",
                    "reason_code": "MODEL_GRAPH_DIRECT_SUPPORT",
                }
            ]
        }
    )

    def request() -> ModelRequest:
        return ModelRequest(
            request_id=StableId("request.graph-support"),
            run_id=RunId("run.graph-support"),
            task_id=TaskId("task.graph-support"),
            model_role=ModelRole.IMPLEMENTATION,
            purpose=ModelCallPurpose.DEVELOPMENT,
            trace_id="trace.graph-support",
            prompt="",
        )

    def gateway(endpoint: object) -> ModelGateway:
        return ModelGateway(
            (
                RegisteredModelEndpoint(
                    role=ModelRole.IMPLEMENTATION,
                    endpoint_name="scripted.graph",
                    model_name="scripted.graph",
                    adapter=endpoint,  # type: ignore[arg-type]
                ),
            )
        )

    unresolved_gateway = gateway(FakeModelEndpoint(draft_response))
    unresolved_curator = ModelCurator(unresolved_gateway)
    unresolved_batches, _ = asyncio.run(
        unresolved_curator.extract_graph_candidates(text, 5, world.source_commit, world, request())
    )
    unresolved = unresolved_batches[0]
    assert unresolved.relations[0].support_status.value == "rejected"

    scripted = ScriptedModelEndpoint(
        lambda model_request: (
            verifier_response
            if "EVIDENCE_VERIFICATION_INPUT" in model_request.prompt
            else draft_response
        )
    )
    verified_gateway = gateway(scripted)
    verified_curator = ModelCurator(
        verified_gateway,
        enable_model_semantic_verifier=True,
    )
    verified_batches, _ = asyncio.run(
        verified_curator.extract_graph_candidates(text, 5, world.source_commit, world, request())
    )
    verified = verified_batches[0]
    assert verified.relations[0].support_status.value == "supported"
    assert len(verified_gateway.call_records) == 2

    class HardRejectGate(EvidenceSupportGate):
        def evaluate_draft(
            self,
            operations: Sequence[CuratedOperationDraftV2],
            catalog: dict[StableId, EvidenceCandidate],
        ) -> tuple[EvidenceSupportDecision, ...]:
            return tuple(
                EvidenceSupportDecision(
                    operation_index=index,
                    candidate_id=operation.evidence_candidate_ids[0],
                    disposition=EvidenceSupportDisposition.CONTRADICTS,
                    reason_code="HARD_GRAPH_CONTRADICTION",
                )
                for index, operation in enumerate(operations)
            )

    hard_gateway = gateway(FakeModelEndpoint(draft_response))
    hard_batches, _ = asyncio.run(
        ModelCurator(hard_gateway, support_gate=HardRejectGate()).extract_graph_candidates(
            text, 5, world.source_commit, world, request()
        )
    )
    hard = hard_batches[0]
    assert hard.relations[0].support_reason == "HARD_GRAPH_CONTRADICTION"

    verifier_reject_gateway = gateway(FakeModelEndpoint(draft_response))
    verifier_rejected_batches, _ = asyncio.run(
        ModelCurator(
            verifier_reject_gateway,
            semantic_verifier=lambda _record, _candidate: (
                EvidenceSupportDisposition.CONTRADICTS,
                "TRUSTED_VERIFIER_REJECTED",
            ),
        ).extract_graph_candidates(text, 5, world.source_commit, world, request())
    )
    verifier_rejected = verifier_rejected_batches[0]
    assert verifier_rejected.relations[0].support_status.value == "rejected"

    candidate = verified_curator.last_evidence_candidates[0].model_copy(update={"text": "wrong"})
    with pytest.raises(ValueError, match="does not round-trip"):
        ModelCurator._evidence_ref(text, 5, world.source_commit, candidate)


def test_model_graph_profile_rejects_non_roundtripping_evidence_without_blocking_batch() -> None:
    world, text, _, _ = _teacher_world()
    block = text.chapters[4].scenes[0].blocks[0]
    candidates = tuple(
        EvidenceCandidate(
            candidate_id=StableId(f"evidence.graph.ambiguous.{index}"),
            block_id=block.block_id,
            chapter_index=5,
            scene_index=0,
            text="国教学院",
            start=index,
            end=index + 4,
            content_hash=sha256_id(block.text.encode("utf-8")),
        )
        for index in (0, 5, 10)
    )

    class AmbiguousGenerator(EvidenceCandidateGenerator):
        def generate(
            self, _text_root: TextRootDocument, _chapter_index: int
        ) -> tuple[EvidenceCandidate, ...]:
            return candidates

    response = json.dumps(
        {
            "status": "complete",
            "candidates": [
                {
                    "kind": "relation",
                    "subject_surface": "林澈",
                    "predicate": "member_of",
                    "object_surface": "国教学院",
                    "valid_time": {"worldline": "main", "start_ordinal": 5},
                    "source_truth_class": "accepted_world_fact",
                    "evidence_quotes": ["国教学院"],
                }
            ],
        },
        ensure_ascii=False,
    )
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="fake.graph.ambiguous",
                model_name="fake.graph.ambiguous",
                adapter=FakeModelEndpoint(response),
            ),
        )
    )
    request = ModelRequest(
        request_id=StableId("request.graph.ambiguous"),
        run_id=RunId("run.graph.ambiguous"),
        task_id=TaskId("task.graph.ambiguous"),
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id="trace.graph.ambiguous",
        prompt="",
    )

    batches, _ = asyncio.run(
        ModelCurator(
            gateway,
            evidence_generator=AmbiguousGenerator(),
        ).extract_graph_candidates(text, 5, world.source_commit, world, request)
    )
    batch = batches[0]

    assert batch.relations[0].support_status.value == "rejected"
    assert batch.relations[0].support_reason == "graph_candidate_evidence_unresolved"
    assert batch.relations[0].evidence_refs == ()
    extraction = WorldGraphExtractionPass().run(world, text, candidate_batches=batches)
    assert extraction.receipt.candidates[-1].status is RelationBackfillStatus.REJECTED


def test_model_graph_profile_fails_closed_on_invalid_semantic_verifier_response() -> None:
    world, text, _, _ = _teacher_world()
    world = _world_with_state_truth(world, TruthClass.ACCEPTED_WORLD_FACT)
    quote = text.chapters[4].scenes[0].blocks[0].text
    draft_response = json.dumps(
        {
            "status": "complete",
            "candidates": [
                {
                    "kind": "relation",
                    "subject_surface": "旧誓言",
                    "predicate": "teacher_of",
                    "object_surface": "林澈",
                    "valid_time": {"worldline": "main", "start_ordinal": 5},
                    "source_truth_class": "accepted_world_fact",
                    "evidence_quotes": [quote],
                }
            ],
        },
        ensure_ascii=False,
    )
    invalid_verifier_response = json.dumps(
        {
            "decisions": [
                {
                    "operation_index": 0,
                    "disposition": "supports",
                    "reason_code": "x" * 161,
                }
            ]
        }
    )
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="fake.graph.invalid-verifier",
                model_name="fake.graph.invalid-verifier",
                adapter=ScriptedModelEndpoint(
                    lambda request: (
                        invalid_verifier_response
                        if "EVIDENCE_VERIFICATION_INPUT" in request.prompt
                        else draft_response
                    )
                ),
            ),
        )
    )
    request = ModelRequest(
        request_id=StableId("request.graph.invalid-verifier"),
        run_id=RunId("run.graph.invalid-verifier"),
        task_id=TaskId("task.graph.invalid-verifier"),
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id="trace.graph.invalid-verifier",
        prompt="",
    )

    batches, _ = asyncio.run(
        ModelCurator(
            gateway,
            enable_model_semantic_verifier=True,
        ).extract_graph_candidates(text, 5, world.source_commit, world, request)
    )
    batch = batches[0]

    assert batch.relations[0].support_status.value == "rejected"
    assert batch.relations[0].support_reason == (
        "graph_candidate_semantic_verifier_schema_rejected"
    )


def test_graph_profile_continuation_no_progress_is_typed_incomplete() -> None:
    world, text, _, _ = _teacher_world()
    quote = text.chapters[4].scenes[0].blocks[0].text
    response = json.dumps(
        {
            "status": "has_more",
            "candidates": [
                {
                    "kind": "relation",
                    "subject_surface": "旧誓言",
                    "predicate": "teacher_of",
                    "object_surface": "林澈",
                    "valid_time": {"worldline": "main", "start_ordinal": 5},
                    "source_truth_class": "assertion",
                    "evidence_quotes": [quote],
                }
            ],
        },
        ensure_ascii=False,
    )
    endpoint = FakeModelEndpoint(response)
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="fake.graph.continuation",
                model_name="fake.graph.continuation",
                adapter=endpoint,
            ),
        )
    )

    batches, _ = asyncio.run(
        ModelCurator(gateway).extract_graph_candidates(
            text,
            5,
            world.source_commit,
            world,
            request=ModelRequest(
                request_id=StableId("request.graph.continuation"),
                run_id=RunId("run.graph.continuation"),
                task_id=TaskId("task.graph.continuation"),
                model_role=ModelRole.IMPLEMENTATION,
                purpose=ModelCallPurpose.DEVELOPMENT,
                trace_id="trace.graph.continuation",
                prompt="",
            ),
        )
    )

    assert len(endpoint.requests) == 2
    assert batches[-1].unit_status.value == "incomplete"
    assert batches[-1].incomplete_reason == "duplicate_only_no_progress"
    assert batches[-1].deduped_candidate_keys == batches[0].candidate_keys


def test_graph_profile_runs_eight_independent_source_units_concurrently() -> None:
    world, text, _, _ = _teacher_world()
    block = text.chapters[4].scenes[0].blocks[0]
    candidates = tuple(
        EvidenceCandidate(
            candidate_id=StableId(f"evidence.concurrent.{index}"),
            block_id=block.block_id,
            chapter_index=5,
            scene_index=0,
            text=block.text,
            start=0,
            end=len(block.text),
            content_hash=sha256_id(block.text.encode("utf-8")),
        )
        for index in range(8)
    )

    class EightUnitGenerator(EvidenceCandidateGenerator):
        def generate(
            self, _text_root: TextRootDocument, _chapter_index: int
        ) -> tuple[EvidenceCandidate, ...]:
            return candidates

    class ConcurrentEndpoint:
        is_external = False

        def __init__(self) -> None:
            self.ready = asyncio.Event()
            self.inflight = 0
            self.max_inflight = 0

        async def generate(self, _request: ModelRequest) -> ProviderModelResult:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            if self.inflight == 8:
                self.ready.set()
            await asyncio.wait_for(self.ready.wait(), timeout=1)
            self.inflight -= 1
            return ProviderModelResult(
                text=json.dumps(
                    {
                        "status": "complete",
                        "candidates": [],
                        "no_graph_candidate_reason": "none found",
                    }
                ),
                model_version="concurrent-v1",
                usage=ModelUsage(
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=Decimal("0"),
                ),
            )

    endpoint = ConcurrentEndpoint()
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="fake.graph.concurrent",
                model_name="fake.graph.concurrent",
                adapter=endpoint,
            ),
        )
    )
    batches, _ = asyncio.run(
        ModelCurator(
            gateway,
            evidence_generator=EightUnitGenerator(),
            graph_source_unit_tokens=1,
        ).extract_graph_candidates(
            text,
            5,
            world.source_commit,
            world,
            ModelRequest(
                request_id=StableId("request.graph.concurrent"),
                run_id=RunId("run.graph.concurrent"),
                task_id=TaskId("task.graph.concurrent"),
                model_role=ModelRole.IMPLEMENTATION,
                purpose=ModelCallPurpose.DEVELOPMENT,
                trace_id="trace.graph.concurrent",
                prompt="",
            ),
        )
    )

    assert len(batches) == 8
    assert endpoint.max_inflight == 8


def test_backfill_runner_uses_validation_commit_projection_and_l0_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, text, _, _ = _teacher_world()
    world = _world_with_state_truth(world, TruthClass.ACCEPTED_WORLD_FACT)
    root = tmp_path
    world_path = root / "world.json"
    text_path = root / "text.json"
    output = root / "repair-output"
    world_path.write_bytes(canonical_json_bytes(world.model_dump(mode="json")))
    text_path.write_bytes(canonical_json_bytes(text.model_dump(mode="json")))
    from scripts import backfill_world_graph

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backfill_world_graph.py",
            "--world-root",
            str(world_path),
            "--text-root",
            str(text_path),
            "--output-dir",
            str(output),
            "--repair-id",
            "unit",
        ],
    )

    assert backfill_world_graph.main() == 0
    manifest = json.loads((output / "repair_manifest.json").read_text("utf-8"))
    receipts = json.loads((output / "graph_path_receipts.json").read_text("utf-8"))
    assert manifest["status"] == "world_graph_repair_completed"
    assert manifest["source_roots_unchanged"] is True
    assert manifest["graph_edge_count"] == manifest["r1_relation_row_count"] == 1
    assert manifest["graph_paths_l0_verified"] is True
    assert receipts[0]["dereference_status"] == "l0_verified"


def test_backfill_runner_imports_source_commit_without_mutating_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import backfill_world_graph

    from novel_agent.adapters.filesystem import FilesystemObjectStore
    from novel_agent.adapters.postgres.database import build_session_factory
    from novel_agent.domain.artifacts import (
        ArtifactRef,
        PlanRootRef,
        ProjectProfileRootRef,
        ReferenceRootRef,
        RootManifest,
        TextRootRef,
        WorldRootRef,
    )
    from novel_agent.domain.base import DomainModel
    from novel_agent.domain.ids import SchemaVersion
    from novel_agent.services.artifacts import ArtifactRepository
    from novel_agent.services.commits import CommitService
    from novel_agent.services.projection import DerivedSnapshotRepository

    world, text, _, _ = _teacher_world()
    world = _world_with_state_truth(world, TruthClass.ACCEPTED_WORLD_FACT)
    plan = make_synthetic_bundle().plan_roots[0]
    checkpoint_plan = plan.model_copy(update={"root_hash": ArtifactId("sha256:" + "a" * 64)})
    checkpoint_plan_path = tmp_path / "checkpoint-plan.json"
    checkpoint_plan_path.write_bytes(canonical_json_bytes(checkpoint_plan.model_dump(mode="json")))
    source = tmp_path / "source"
    objects = source / "objects"
    repository = ArtifactRepository(FilesystemObjectStore(objects))

    def put(value: DomainModel, media_type: str) -> ArtifactRef:
        return repository.put(
            canonical_json_bytes(value.model_dump(mode="json")),
            media_type,
            SchemaVersion("1.0.0"),
        )

    text_ref = put(text, "application/json")
    plan_ref = put(plan, "application/json")
    world_ref = put(world, "application/json")
    reference_ref = repository.put(
        b'{"reference":null}', "application/json", SchemaVersion("1.0.0")
    )
    profile_ref = repository.put(b'{"profile":null}', "application/json", SchemaVersion("1.0.0"))
    manifest = RootManifest(
        project_id=ProjectId("project.graph-source"),
        schema_version=SchemaVersion("1.0.0"),
        text_root=TextRootRef.model_validate(text_ref.model_dump()),
        plan_root=PlanRootRef.model_validate(plan_ref.model_dump()),
        world_root=WorldRootRef.model_validate(world_ref.model_dump()),
        reference_root=ReferenceRootRef.model_validate(reference_ref.model_dump()),
        project_profile_root=ProjectProfileRootRef.model_validate(profile_ref.model_dump()),
    )
    database = source / "source.sqlite3"
    engine = create_engine(f"sqlite+pysqlite:///{database}")
    Base.metadata.create_all(engine)
    source_commits = CommitService(build_session_factory(engine))
    source_commit = source_commits.initialize_project(manifest)
    output = tmp_path / "imported-repair"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backfill_world_graph.py",
            "--source-project",
            str(source),
            "--source-database-url",
            f"sqlite+pysqlite:///{database}",
            "--source-commit",
            source_commit.root,
            "--checkpoint-plan-root",
            str(checkpoint_plan_path),
            "--output-dir",
            str(output),
            "--repair-id",
            "source-unit",
        ],
    )

    assert backfill_world_graph.main() == 0
    assert source_commits.current_commit(manifest.project_id) == source_commit
    assert repository.read_verified(manifest.world_root) == canonical_json_bytes(
        world.model_dump(mode="json")
    )
    assert repository.read_verified(manifest.plan_root) == canonical_json_bytes(
        plan.model_dump(mode="json")
    )
    repair = json.loads((output / "repair_manifest.json").read_text("utf-8"))
    assert repair["source_roots_unchanged"] is True
    assert repair["selected_source_commit"] == source_commit.root
    assert repair["repair_commit"] != source_commit.root
    assert repair["source_plan_root"] == plan.root_hash.root
    assert repair["checkpoint_plan_root"] == checkpoint_plan.root_hash.root
    from scripts.run_evidence_first_frozen_checkpoints import (
        _immutable_roots,
        _load_repair_checkpoint,
        _select_case_basis,
    )

    repair_checkpoint = _load_repair_checkpoint(output)
    try:
        assert _immutable_roots(repair_checkpoint.engine)["db_commit_count"] == "2"
        snapshots = DerivedSnapshotRepository(build_session_factory(repair_checkpoint.engine))
        assert snapshots.get_for_commit(repair_checkpoint.repair_commit) is not None
        assert snapshots.get_attestation_for_commit(repair_checkpoint.repair_commit) is None
        source_r1 = R1WorldRepository(build_session_factory(engine))
        source_basis = _select_case_basis(
            short_case="P004",
            repair_case="P005",
            repair=repair_checkpoint,
            source_engine=engine,
            source_r1=source_r1,
            source_project_id=manifest.project_id,
            source_commit=source_commit,
            source_world=world,
            source_text=text,
            source_plan=checkpoint_plan,
        )
        assert source_basis.joint_repair is False
        joint_basis = _select_case_basis(
            short_case="P005",
            repair_case="P005",
            repair=repair_checkpoint,
            source_engine=engine,
            source_r1=source_r1,
            source_project_id=manifest.project_id,
            source_commit=source_commit,
            source_world=world,
            source_text=text,
            source_plan=checkpoint_plan,
        )
        assert joint_basis.joint_repair is True
        assert joint_basis.commit == repair_checkpoint.repair_commit
        assert joint_basis.project_id == repair_checkpoint.project_id
        with pytest.raises(ValueError, match="selected source commit"):
            _select_case_basis(
                short_case="P005",
                repair_case="P005",
                repair=repair_checkpoint,
                source_engine=engine,
                source_r1=source_r1,
                source_project_id=manifest.project_id,
                source_commit=CommitId("sha256:" + "f" * 64),
                source_world=world,
                source_text=text,
                source_plan=plan,
            )
    finally:
        repair_checkpoint.engine.dispose()
    engine.dispose()


def test_repaired_relation_materializes_paths_receipts_and_l1_anchor() -> None:
    world, text, teacher_id, _ = _teacher_world()
    world = _world_with_state_truth(world, TruthClass.ACCEPTED_WORLD_FACT)
    repaired = WorldGraphExtractionPass().run(world, text).repaired_world
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    repository = R1WorldRepository(build_session_factory(engine))
    try:
        repository.materialize(ProjectId("project.graph-repair"), world.source_commit, repaired)
        _, _, graph_edges = repository.counts(world.source_commit)
        paths = repository.typed_graph_paths(
            world.source_commit,
            (teacher_id,),
            max_depth=2,
            limit=10,
            snapshot_id=StableId("snapshot.graph-repair"),
        )
        verified = repository.validate_graph_path_receipts(paths, text)
        with pytest.raises(ValueError, match="identity does not match"):
            repository.validate_graph_path_receipts(
                (paths[0].model_copy(update={"path_id": StableId("graph-path.forged")}),),
                text,
            )
    finally:
        engine.dispose()

    units = AnchorBuilder().build(
        repaired,
        text,
        None,
        snapshot_id=StableId("snapshot.graph-repair"),
    )
    assert graph_edges == 1
    assert paths[0].dereference_status is GraphPathDereferenceStatus.RELATION_ROWS_VERIFIED
    assert verified[0].dereference_status is GraphPathDereferenceStatus.L0_VERIFIED
    assert any(unit.unit_kind is RetrievalUnitKind.RELATION_ANCHOR for unit in units)
    assert sum(unit.unit_kind is RetrievalUnitKind.FACT_ANCHOR for unit in units) == len(
        repaired.entities
    )


def test_world_graph_pass_audits_unresolvable_evidence_without_creating_edge() -> None:
    world, text, _, _ = _teacher_world()
    state = world.states[0]
    bad_evidence = state.evidence_refs[0].model_copy(
        update={"object_hash": ArtifactId("sha256:" + "f" * 64)}
    )
    invalid = world.model_copy(
        update={"states": (state.model_copy(update={"evidence_refs": (bad_evidence,)}),)}
    )

    result = WorldGraphExtractionPass().run(invalid, text)

    assert result.repaired_world.relations == ()
    assert result.receipt.candidates[0].status is RelationBackfillStatus.REJECTED
    reason = result.receipt.candidates[0].rejection_reason
    assert reason is not None and "evidence does not resolve" in reason

    rumor = world.model_copy(
        update={"states": (state.model_copy(update={"truth_class": TruthClass.RUMOR}),)}
    )
    rumor_result = WorldGraphExtractionPass().run(rumor, text)
    assert rumor_result.repaired_world.relations[0].truth_class is TruthClass.RUMOR
    assert rumor_result.receipt.candidates[0].status is RelationBackfillStatus.ACCEPTED


def test_repair_receipt_domain_validators_fail_closed() -> None:
    """Round 1 graph repair receipts: every resolution/backfill shape is validated."""
    from pydantic import ValidationError

    from novel_agent.domain.world import (
        EntityAliasResolutionReceipt,
        RelationBackfillReceipt,
        WorldGraphExtractionReceipt,
    )

    world, _, teacher_id, student_id = _teacher_world()
    student_entity = world.entities[0]
    evidence = world.states[0].evidence_refs[0]

    with pytest.raises(ValidationError, match="one matching resolved entity"):
        EntityAliasResolutionReceipt(
            receipt_id=StableId("alias.unique-broken"),
            mention="林澈",
            status=EntityResolutionStatus.UNIQUE_LABEL,
            matched_entity_ids=(student_entity.entity_id,),
            resolved_entity_id=None,
            match_basis="canonical_label",
        )
    with pytest.raises(ValidationError, match="one matching resolved entity"):
        EntityAliasResolutionReceipt(
            receipt_id=StableId("alias.unique-multi"),
            mention="林澈",
            status=EntityResolutionStatus.UNIQUE_LABEL,
            matched_entity_ids=(
                student_entity.entity_id,
                StableId("entity.other"),
            ),
            resolved_entity_id=student_entity.entity_id,
            match_basis="canonical_label",
        )
    with pytest.raises(ValidationError, match="cannot expose a resolved entity"):
        EntityAliasResolutionReceipt(
            receipt_id=StableId("alias.ambiguous-resolved"),
            mention="殿下",
            status=EntityResolutionStatus.AMBIGUOUS,
            matched_entity_ids=(student_entity.entity_id,),
            resolved_entity_id=student_entity.entity_id,
            match_basis="alias_collision",
        )
    with pytest.raises(ValidationError, match="endpoints, identity, and evidence"):
        RelationBackfillReceipt(
            candidate_id=StableId("candidate.incomplete"),
            source_batch_id=StableId("batch.incomplete"),
            source_state_id=world.states[0].state_id,
            source_truth_class=TruthClass.ASSERTION,
            status=RelationBackfillStatus.ACCEPTED,
            predicate="teacher_of",
            subject_surface="旧誓言",
            object_surface="林澈",
            subject_id=teacher_id,
            object_id=None,
            relation_id=None,
            evidence_refs=(evidence,),
            rejection_reason=None,
        )
    with pytest.raises(ValidationError, match="cannot have a rejection reason"):
        RelationBackfillReceipt(
            candidate_id=StableId("candidate.rejected-accepted"),
            source_batch_id=StableId("batch.rejected-accepted"),
            source_state_id=world.states[0].state_id,
            source_truth_class=TruthClass.ASSERTION,
            status=RelationBackfillStatus.ACCEPTED,
            predicate="teacher_of",
            subject_surface="旧誓言",
            object_surface="林澈",
            subject_id=teacher_id,
            object_id=student_id,
            relation_id=StableId("relation.synthetic.teacher"),
            evidence_refs=(evidence,),
            rejection_reason="rejected anyway",
        )
    with pytest.raises(ValidationError, match="requires a rejection reason"):
        RelationBackfillReceipt(
            candidate_id=StableId("candidate.rejected-unknown"),
            source_batch_id=StableId("batch.rejected-unknown"),
            source_state_id=world.states[0].state_id,
            source_truth_class=TruthClass.ASSERTION,
            status=RelationBackfillStatus.REJECTED,
            predicate="teacher_of",
            subject_surface="旧誓言",
            object_surface="林澈",
            subject_id=teacher_id,
            object_id=student_id,
            relation_id=None,
            evidence_refs=(),
            rejection_reason=None,
        )
    accepted = RelationBackfillReceipt(
        candidate_id=StableId("candidate.accepted"),
        source_batch_id=StableId("batch.accepted"),
        source_state_id=world.states[0].state_id,
        source_truth_class=TruthClass.ASSERTION,
        status=RelationBackfillStatus.ACCEPTED,
        predicate="teacher_of",
        subject_surface="旧誓言",
        object_surface="林澈",
        subject_id=teacher_id,
        object_id=student_id,
        relation_id=StableId("relation.synthetic.teacher"),
        evidence_refs=(evidence,),
        rejection_reason=None,
    )
    with pytest.raises(ValidationError, match="accepted relation ids must match"):
        WorldGraphExtractionReceipt(
            receipt_id=StableId("extraction.mismatch"),
            source_world_root=world.root_hash,
            repaired_world_root=ArtifactId("sha256:" + "1" * 64),
            predicate_registry_version="v1",
            alias_policy_version="v1",
            candidates=(accepted,),
            accepted_relation_ids=(),
            retained_state_ids=(),
            accepted_count=1,
            rejected_count=0,
            deduped_count=0,
        )
    assert accepted.relation_id is not None
    extraction = WorldGraphExtractionReceipt(
        receipt_id=StableId("extraction.ok"),
        source_world_root=world.root_hash,
        repaired_world_root=ArtifactId("sha256:" + "1" * 64),
        predicate_registry_version="v1",
        alias_policy_version="v1",
        candidates=(accepted,),
        accepted_relation_ids=(accepted.relation_id,),
        retained_state_ids=(),
        accepted_count=1,
        rejected_count=0,
        deduped_count=0,
    )
    assert extraction.accepted_relation_ids == (accepted.relation_id,)


# --- Round-19: GraphCandidatePageDraft canonicalization of empty no-op reasons ---


def test_graph_page_draft_canonicalizes_empty_no_op_reason() -> None:
    from novel_agent.domain.world import (
        GraphCandidatePageDraft,
        GraphCandidatePageStatus,
        GraphRelationCandidateDraft,
        StoryTime,
        TruthClass,
    )

    relation = GraphRelationCandidateDraft(
        kind="relation",
        subject_surface="陈长生",
        predicate="discloses_to",
        object_surface="唐三十六",
        valid_time=StoryTime(worldline="main", start_ordinal=None, end_ordinal=None),
        source_truth_class=TruthClass.ASSERTION,
        evidence_quotes=("陈长生向他解释。",),
    )
    # 1) non-empty page + empty-string reason -> reason None, candidates retained.
    page = GraphCandidatePageDraft.model_validate(
        {
            "status": GraphCandidatePageStatus.COMPLETE,
            "candidates": (relation,),
            "no_graph_candidate_reason": "",
        }
    )
    assert page.no_graph_candidate_reason is None
    assert len(page.candidates) == 1
    assert page.candidates[0].predicate == "discloses_to"  # type: ignore[union-attr]
    # 3) same shape with whitespace-only reason.
    page3 = GraphCandidatePageDraft.model_validate(
        {
            "status": GraphCandidatePageStatus.COMPLETE,
            "candidates": (relation,),
            "no_graph_candidate_reason": "   ",
        }
    )
    assert page3.no_graph_candidate_reason is None
    # 2) empty COMPLETE page + missing reason -> stable operational reason.
    empty = GraphCandidatePageDraft.model_validate(
        {
            "status": GraphCandidatePageStatus.COMPLETE,
            "candidates": (),
            "no_graph_candidate_reason": None,
        }
    )
    assert empty.no_graph_candidate_reason == "model_returned_no_graph_candidates"
    # An existing substantive empty-page reason is preserved.
    substantive = GraphCandidatePageDraft.model_validate(
        {
            "status": GraphCandidatePageStatus.COMPLETE,
            "candidates": (),
            "no_graph_candidate_reason": "no durable relation in unit",
        }
    )
    assert substantive.no_graph_candidate_reason == "no durable relation in unit"
    # Non-empty page with a substantive reason still fails.
    with pytest.raises(ValidationError):
        GraphCandidatePageDraft.model_validate(
            {
                "status": GraphCandidatePageStatus.COMPLETE,
                "candidates": (relation,),
                "no_graph_candidate_reason": "why not",
            }
        )
    # Empty HAS_MORE page still fails even with the operational reason.
    with pytest.raises(ValidationError):
        GraphCandidatePageDraft.model_validate(
            {
                "status": GraphCandidatePageStatus.HAS_MORE,
                "candidates": (),
                "no_graph_candidate_reason": "model_returned_no_graph_candidates",
            }
        )
    # Candidate mutation (entity without a relation endpoint) still fails.
    from novel_agent.domain.world import GraphEntityCandidateDraft

    entity = GraphEntityCandidateDraft(
        kind="entity",
        surface="陈长生",
        entity_type="person",
        evidence_quotes=("陈长生出现了。",),
    )
    with pytest.raises(ValidationError):
        GraphCandidatePageDraft.model_validate(
            {
                "status": GraphCandidatePageStatus.COMPLETE,
                "candidates": (entity,),
                "no_graph_candidate_reason": "",
            }
        )
    # Unrelated invalid fields still fail (strict domain, extra fields forbidden).
    with pytest.raises(ValidationError):
        GraphCandidatePageDraft.model_validate(
            {
                "status": GraphCandidatePageStatus.COMPLETE,
                "candidates": (relation,),
                "unrelated_field": "x",
            }
        )


# The three exact Graph candidate-page raw responses quarantined by the
# chapter-12 diagnostic (package sha256:1d35f044...): the first and third are
# complete pages WITH candidates carrying `no_graph_candidate_reason: ""`; the
# second is a complete page with no candidates that omits the reason entirely.
QUARANTINED_CH12_GRAPH_PAGE_1 = """{
  "status": "complete",
  "candidates": [
    {
      "kind": "relation",
      "subject_surface": "陈长生",
      "predicate": "discloses_to",
      "object_surface": "唐三十六",
      "valid_time": {
        "worldline": "main",
        "start_ordinal": null,
        "end_ordinal": null
      },
      "source_truth_class": "not_applicable",
      "evidence_quotes": [
        "陈长生想了想先前的场景\uff0c明白了些什么\uff0c有些不好意思\uff0c解释道\uff1a“因为……你点的飞雀黄精汤\uff0c名为温补\uff0c实则燥意极大\uff0c在秋冬服用是极好的\uff0c现在是春天\uff0c那汤喝了容易生虚火\uff0c对身体不大好。”"
      ]
    }
  ],
  "no_graph_candidate_reason": ""
}"""
QUARANTINED_CH12_GRAPH_PAGE_2 = """{
  "status": "complete",
  "candidates": []
}"""
QUARANTINED_CH12_GRAPH_PAGE_3 = """{
  "status": "complete",
  "candidates": [
    {
      "kind": "relation",
      "subject_surface": "陈长生",
      "predicate": "owns",
      "object_surface": "剑",
      "valid_time": {
        "worldline": "main",
        "start_ordinal": 1,
        "end_ordinal": null
      },
      "source_truth_class": "not_applicable",
      "evidence_quotes": [
        "”\\n　　陈长生看着他\uff0c说道\uff1a“这是我的。"
      ]
    },
    {
      "kind": "entity",
      "surface": "剑",
      "entity_type": "artifact",
      "evidence_quotes": [
        "走到厅室过博物架的时候\uff0c唐三十六的目光下意识落到架上\uff0c便再也无法离开——那里有一把剑。"
      ]
    }
  ],
  "no_graph_candidate_reason": ""
}"""


def test_graph_page_draft_canonicalizes_exact_quarantined_ch12_pages() -> None:
    # Round-19: the three exact quarantined raw pages must now validate, with
    # the canonicalization confined to the no-candidate-reason field.
    from novel_agent.domain.world import (
        GraphCandidatePageDraft,
        GraphEntityCandidateDraft,
        GraphRelationCandidateDraft,
    )

    page1 = GraphCandidatePageDraft.model_validate_json(QUARANTINED_CH12_GRAPH_PAGE_1)
    assert page1.no_graph_candidate_reason is None
    relation1 = page1.candidates[0]
    assert isinstance(relation1, GraphRelationCandidateDraft)
    assert len(page1.candidates) == 1
    assert relation1.predicate == "discloses_to"
    assert relation1.subject_surface == "陈长生"
    assert relation1.object_surface == "唐三十六"
    assert relation1.valid_time.start_ordinal is None
    assert len(relation1.evidence_quotes) == 1
    assert relation1.source_truth_class.value == "not_applicable"

    page2 = GraphCandidatePageDraft.model_validate_json(QUARANTINED_CH12_GRAPH_PAGE_2)
    assert page2.candidates == ()
    assert page2.no_graph_candidate_reason == "model_returned_no_graph_candidates"

    page3 = GraphCandidatePageDraft.model_validate_json(QUARANTINED_CH12_GRAPH_PAGE_3)
    assert page3.no_graph_candidate_reason is None
    assert len(page3.candidates) == 2
    relation3 = page3.candidates[0]
    assert isinstance(relation3, GraphRelationCandidateDraft)
    assert relation3.predicate == "owns"
    assert relation3.object_surface == "剑"
    entity3 = page3.candidates[1]
    assert isinstance(entity3, GraphEntityCandidateDraft)
    assert entity3.surface == "剑"
    assert entity3.entity_type == "artifact"
    # The canonicalized page differs from the raw page only in the reason field.
    raw3 = json.loads(QUARANTINED_CH12_GRAPH_PAGE_3)
    raw3["no_graph_candidate_reason"] = None
    dumped = page3.model_dump(mode="json")
    for candidate in dumped["candidates"]:
        if candidate.get("valid_time") is not None:
            candidate["valid_time"].pop("label", None)
    assert dumped == raw3
