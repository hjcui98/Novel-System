from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.agents import AgentRegistry, CuratorReplayAgent, StructuredAgentRunner
from novel_agent.domain.changes import (
    ChangeOperationType,
    ChapterChangeDraft,
    ChapterChangeDraftV2,
    CuratedOperationDraftV2,
    CuratorObligationRecord,
    WorldRecordKind,
)
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentSpec,
    AgentType,
    ContractRef,
    CuratorEvidenceContract,
    CuratorReplayResult,
    PromptContractRef,
    SkillContractRef,
    ToolPolicy,
)
from novel_agent.prompts import PromptRegistry, PromptTemplate
from novel_agent.prompts.registry import content_hash
from novel_agent.services.evidence_candidates import EvidenceCandidateGenerator
from novel_agent.services.model_curation import ModelCurator
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.skills import SkillRegistry, SkillTemplate
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.unit.test_model_curation import _draft

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "a" * 64)
ROOT = Path(__file__).parents[2]


def request() -> ModelRequest:
    return ModelRequest(
        request_id=StableId("request.curator-agent"),
        run_id=RunId("run.curator-agent"),
        task_id=TaskId("task.curator-agent"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace-curator-agent",
        prompt="untrusted caller prompt",
    )


def ref(path: Path, contract_id: str) -> PromptContractRef:
    digest = content_hash(path.read_bytes())
    return PromptContractRef(
        contract_id=StableId(contract_id),
        version=VERSION,
        content_hash=digest,
        render_fingerprint=digest,
    )


def harness(
    draft: ChapterChangeDraft | ChapterChangeDraftV2,
    *,
    evidence_contract: CuratorEvidenceContract = CuratorEvidenceContract.LEGACY_OFFSET_V1,
    enforce_support_gate: bool = True,
) -> tuple[CuratorReplayAgent, FakeModelEndpoint]:
    system_path = ROOT / "src/novel_agent/prompts/system_policy_v1.md"
    task_path = ROOT / "src/novel_agent/prompts/curator_replay_v1.md"
    skill_path = ROOT / "src/novel_agent/skills/memory_delta_extraction_v1.md"
    system_ref = ref(system_path, "prompt.system-policy")
    task_ref = ref(task_path, "prompt.curator-replay")
    skill_ref = SkillContractRef(
        contract_id=StableId("skill.memory-delta-extraction"),
        version=VERSION,
        content_hash=content_hash(skill_path.read_bytes()),
    )
    schema = ContractRef(
        contract_id=StableId("schema.chapter-change-draft"),
        version=VERSION,
        content_hash=HASH,
    )
    policy = ToolPolicy(
        policy_id=StableId("policy.curator-replay"),
        version=VERSION,
        content_hash=HASH,
        allowed_tools=(),
        max_tool_calls=0,
    )
    spec = AgentSpec(
        agent_id=StableId("agent.memory-curator.replay"),
        agent_type=AgentType.MEMORY_CURATOR,
        mode=AgentMode.REPLAY,
        version=VERSION,
        content_hash=HASH,
        input_schema=schema,
        output_schema=schema,
        system_prompt=system_ref,
        task_prompt=task_ref,
        skills=(skill_ref,),
        tool_policy=policy,
    )
    endpoint = FakeModelEndpoint(draft.model_dump_json())
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="curator-test",
                model_name="fake-curator",
                adapter=endpoint,
            ),
        )
    )
    runner = StructuredAgentRunner(
        gateway,
        AgentRegistry((spec,)),
        PromptRegistry(
            (
                PromptTemplate(
                    system_ref.contract_id, VERSION, system_path, system_ref.content_hash
                ),
                PromptTemplate(task_ref.contract_id, VERSION, task_path, task_ref.content_hash),
            )
        ),
        SkillRegistry(
            (SkillTemplate(skill_ref.contract_id, VERSION, skill_path, skill_ref.content_hash),)
        ),
    )
    return CuratorReplayAgent(
        ModelCurator(gateway, enforce_support_gate=enforce_support_gate),
        runner,
        evidence_contract=evidence_contract,
    ), endpoint


def _v2_draft() -> ChapterChangeDraftV2:
    bundle = make_synthetic_bundle()
    text_root = bundle.text_roots[1]
    candidates = EvidenceCandidateGenerator().generate(text_root, 23)
    assert candidates
    candidate = candidates[0]
    return ChapterChangeDraftV2(
        chapter_index=23,
        operations=(
            CuratedOperationDraftV2(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.OBLIGATION,
                target_id=StableId("obligation.synthetic.north-tower"),
                record=CuratorObligationRecord(
                    kind="objective",
                    description="林澈需要进入北塔。",
                    status="resolved",
                    owner_ids=(StableId("entity.synthetic.lin-che"),),
                    due_chapter=23,
                ),
                evidence_candidate_ids=(candidate.candidate_id,),
            ),
        ),
    )


def test_curator_replay_agent_layers_contract_and_preserves_report_and_receipt() -> None:
    bundle = make_synthetic_bundle()
    text_root = bundle.text_roots[1]
    world = bundle.world_roots[0]
    draft = _draft().model_copy(
        update={
            "coverage": 0.75,
            "unresolved": ("ambiguous owner",),
            "declared_vs_observed_diff": ("planned arrival was delayed",),
        }
    )
    agent, endpoint = harness(draft)

    result, call = asyncio.run(
        agent.run(
            version=VERSION,
            text_root=text_root,
            chapter_index=23,
            base_commit=world.source_commit,
            current_world=world,
            request=request(),
        )
    )

    assert call.request_id == request().request_id
    assert result.coverage == 0.75
    assert result.unresolved == ("ambiguous owner",)
    assert result.declared_vs_observed_diff == ("planned arrival was delayed",)
    assert result.receipt.agent_type is AgentType.MEMORY_CURATOR
    assert result.receipt.agent_mode is AgentMode.REPLAY
    assert result.receipt.base_commit == world.source_commit
    assert result.receipt.unresolved == result.unresolved
    assert result.receipt.output_artifacts[0].media_type.endswith("observed-change-set+json")
    assert result.receipt.skill_receipts[0].output_artifacts == result.receipt.output_artifacts
    sent = endpoint.requests[0]
    assert sent.agent_id == StableId("agent.memory-curator.replay")
    assert sent.render_fingerprint == result.receipt.prompt_fingerprint
    assert "Memory Curator REPLAY v1" in sent.prompt
    assert "memory_delta_extraction" in sent.prompt
    assert '<CURATOR_INPUT trusted="false">' in sent.prompt
    assert "终于进入北塔" in sent.prompt
    assert "重申旧誓言" not in sent.prompt


def test_curator_replay_agent_renders_trusted_proposal_feedback_into_effective_prompt() -> None:
    bundle = make_synthetic_bundle()
    text_root = bundle.text_roots[1]
    world = bundle.world_roots[0]
    agent, endpoint = harness(_draft())
    feedback = '{"reason_code":"CURATOR_PROPOSAL_DUPLICATE_TARGET"}'

    asyncio.run(
        agent.run(
            version=VERSION,
            text_root=text_root,
            chapter_index=23,
            base_commit=world.source_commit,
            current_world=world,
            request=request(),
            proposal_feedback=feedback,
        )
    )

    sent = endpoint.requests[0]
    assert '<PROPOSAL_REPAIR_FEEDBACK trusted="true">' in sent.prompt
    assert feedback in sent.prompt


@pytest.mark.parametrize(
    ("receipt_update", "message"),
    (
        ({"agent_type": AgentType.PLANNER}, "Memory Curator"),
        ({"agent_mode": AgentMode.BOOTSTRAP}, "REPLAY"),
        ({"base_commit": None}, "share a base commit"),
        ({"unresolved": ()}, "preserve unresolved"),
    ),
)
def test_curator_replay_result_rejects_mismatched_receipt(
    receipt_update: dict[str, object], message: str
) -> None:
    bundle = make_synthetic_bundle()
    text_root = bundle.text_roots[1]
    world = bundle.world_roots[0]
    draft = _draft().model_copy(update={"unresolved": ("gap",)})
    result, _ = asyncio.run(
        harness(draft)[0].run(
            version=VERSION,
            text_root=text_root,
            chapter_index=23,
            base_commit=world.source_commit,
            current_world=world,
            request=request(),
        )
    )
    payload = result.model_dump()
    payload["receipt"] = result.receipt.model_copy(update=receipt_update).model_dump()
    with pytest.raises(ValidationError, match=message):
        CuratorReplayResult.model_validate(payload)


def test_curator_replay_agent_v2_renders_trusted_proposal_feedback() -> None:
    """V2 contract path must inject proposal_feedback into the task prompt."""

    bundle = make_synthetic_bundle()
    text_root = bundle.text_roots[1]
    world = bundle.world_roots[0]
    agent, endpoint = harness(
        _v2_draft(),
        evidence_contract=CuratorEvidenceContract.CANDIDATE_ID_V2,
        enforce_support_gate=False,
    )
    feedback = '{"reason_code":"CURATOR_PROPOSAL_DUPLICATE_TARGET"}'

    asyncio.run(
        agent.run(
            version=VERSION,
            text_root=text_root,
            chapter_index=23,
            base_commit=world.source_commit,
            current_world=world,
            request=request(),
            proposal_feedback=feedback,
        )
    )

    sent = endpoint.requests[0]
    assert '<PROPOSAL_REPAIR_FEEDBACK trusted="true">' in sent.prompt
    assert feedback in sent.prompt
    assert "ChapterChangeDraftV2" in sent.prompt or "evidence_candidate_ids" in sent.prompt
    assert "Always emit the operations key" in sent.prompt
    assert "no_durable_delta_reason" in sent.prompt


def test_curator_replay_agent_v2_without_proposal_feedback() -> None:
    """V2 contract path without proposal_feedback must omit the repair block."""

    bundle = make_synthetic_bundle()
    text_root = bundle.text_roots[1]
    world = bundle.world_roots[0]
    agent, endpoint = harness(
        _v2_draft(),
        evidence_contract=CuratorEvidenceContract.CANDIDATE_ID_V2,
        enforce_support_gate=False,
    )

    result, call = asyncio.run(
        agent.run(
            version=VERSION,
            text_root=text_root,
            chapter_index=23,
            base_commit=world.source_commit,
            current_world=world,
            request=request(),
        )
    )

    sent = endpoint.requests[0]
    assert '<PROPOSAL_REPAIR_FEEDBACK' not in sent.prompt
    assert result.receipt.agent_type is AgentType.MEMORY_CURATOR
    assert call.request_id == request().request_id
