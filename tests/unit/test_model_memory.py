from __future__ import annotations

import asyncio

import pytest

from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.domain.benchmark import BenchmarkBundle, WorldConstructionDraft
from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.memory import HorizonNeedSet, Stage1MemoryNeed, WorldRootDocument
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.world import RelationRecord, StoryTime, TruthClass
from novel_agent.services.benchmark_importer import BenchmarkImportError
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.model_memory import (
    ModelMemoryConstructor,
    ModelMemoryContractError,
    ModelMemoryNeedGenerator,
)
from novel_agent.services.stage1_benchmark import Stage1NeedGenerator
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def _request(run_id: RunId, task_id: TaskId) -> ModelRequest:
    return ModelRequest(
        request_id=StableId("request.model-memory"),
        run_id=run_id,
        task_id=task_id,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace-model-memory",
        prompt="caller prompt must be replaced",
        timeout_seconds=1.0,
    )


def _gateway(response: str) -> tuple[ModelGateway, FakeModelEndpoint]:
    fake = FakeModelEndpoint(response)
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="batch-test-fake",
                model_name="fake-memory-model",
                adapter=fake,
            ),
        )
    )
    return gateway, fake


def test_model_world_constructor_uses_history_only_and_validates_all_evidence() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    history = next(root for root in bundle.text_roots if len(root.chapters) == 20)
    world = bundle.world_roots[0]
    relation = RelationRecord(
        relation_id=StableId("relation.synthetic.self"),
        predicate="knows_self",
        subject_id=world.entities[0].entity_id,
        object_id=world.entities[0].entity_id,
        valid_time=StoryTime(worldline="main", start_ordinal=20),
        evidence_refs=world.states[0].evidence_refs,
        truth_class=TruthClass.ACCEPTED_WORLD_FACT,
    )
    draft = WorldConstructionDraft(
        entities=world.entities,
        events=world.events,
        states=world.states,
        relations=(relation,),
        obligations=world.obligations,
    )
    gateway, fake = _gateway(draft.model_dump_json())
    request = _request(RunId("run.model-world"), TaskId("task.model-world"))

    constructed, call = asyncio.run(
        ModelMemoryConstructor(gateway).construct(history, case, world.source_commit, request)
    )

    assert constructed.entities == world.entities
    assert constructed.relations == (relation,)
    assert constructed.source_commit == world.source_commit
    assert call.model_role is ModelRole.BATCH_TEST
    prompt = fake.requests[0].prompt
    assert "caller prompt must be replaced" not in prompt
    assert "future_text_root_private" not in prompt
    assert "observed_use_gold" not in prompt
    assert "林澈终于进入北塔" not in prompt


def test_model_world_constructor_rejects_unresolvable_evidence() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    history = next(root for root in bundle.text_roots if len(root.chapters) == 20)
    world = bundle.world_roots[0]
    bad_evidence = (
        world.states[0]
        .evidence_refs[0]
        .model_copy(update={"root_hash": bundle.text_roots[1].root_hash})
    )
    bad_state = world.states[0].model_copy(update={"evidence_refs": (bad_evidence,)})
    draft = WorldConstructionDraft(
        entities=world.entities,
        events=world.events,
        states=(bad_state,),
        obligations=world.obligations,
    )
    gateway, _ = _gateway(draft.model_dump_json())
    with pytest.raises(BenchmarkImportError, match="declared text root"):
        asyncio.run(
            ModelMemoryConstructor(gateway).construct(
                history,
                case,
                world.source_commit,
                _request(RunId("run.bad-world"), TaskId("task.bad-world")),
            )
        )


def _generated_needs() -> tuple[BenchmarkBundle, WorldRootDocument, tuple[Stage1MemoryNeed, ...]]:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    world = bundle.world_roots[0]
    needs = Stage1NeedGenerator().generate(world, case)
    return bundle, world, needs


def test_model_need_generator_preserves_audit_identities_and_public_boundary() -> None:
    bundle, world, needs = _generated_needs()
    case = bundle.case_manifests[0]
    fourth = needs[0].model_copy(update={"need_id": StableId("need.generated.fourth")})
    generated = HorizonNeedSet(
        horizon_start=21,
        horizon_end=23,
        shared_constraints=(needs[0],),
        chapter_needs=(needs[1],),
        progressive_needs=(needs[2],),
        volume_obligations=(fourth,),
    )
    gateway, fake = _gateway(generated.model_dump_json())
    request = _request(needs[0].run_id, needs[0].task_id)

    restored, call = asyncio.run(
        ModelMemoryNeedGenerator(gateway).generate(world, bundle.plan_roots[0], case, request)
    )

    assert restored == generated
    assert call.purpose is ModelCallPurpose.BATCH_TEST
    prompt = fake.requests[0].prompt
    assert request.run_id.root in prompt and world.source_commit.root in prompt
    assert "future_text_root_private" not in prompt
    assert "plan_obligation_gold" not in prompt


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("horizon", "horizon differs"),
        ("duplicate", "not unique"),
        ("identity", "identity mismatch"),
        ("chapter", "chapter is outside"),
        ("need_horizon", "horizon is outside"),
    ],
)
def test_model_need_generator_rejects_contract_violations(mutation: str, message: str) -> None:
    bundle, world, needs = _generated_needs()
    case = bundle.case_manifests[0]
    selected = needs[0]
    if mutation == "identity":
        selected = selected.model_copy(update={"run_id": RunId("run.wrong")})
    elif mutation == "chapter":
        selected = selected.model_copy(update={"chapter_target": 24, "horizon_target": None})
    elif mutation == "need_horizon":
        selected = selected.model_copy(update={"horizon_target": (21, 22)})
    shared = (selected, selected) if mutation == "duplicate" else (selected,)
    generated = HorizonNeedSet(
        horizon_start=20 if mutation == "horizon" else 21,
        horizon_end=23,
        shared_constraints=shared,
    )
    gateway, _ = _gateway(generated.model_dump_json())
    with pytest.raises(ModelMemoryContractError, match=message):
        asyncio.run(
            ModelMemoryNeedGenerator(gateway).generate(
                world,
                None,
                case,
                _request(needs[0].run_id, needs[0].task_id),
            )
        )
