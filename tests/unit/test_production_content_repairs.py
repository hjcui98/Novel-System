"""Regressions for information loss between production planning, memory and review."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest

from novel_agent.domain.benchmark import ChapterGoal, PlanRootDocument
from novel_agent.domain.editorial import (
    CuratorChangeObservation,
    CuratorObservation,
    EditorialReviewInput,
    EditorialVerdict,
    EditorReviewPayload,
    PlanRequirementAssessment,
)
from novel_agent.domain.generation import DeclaredMemoryHint, MemoryHintChangeKind
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.memory import ObligationStatus, WorldRootDocument
from novel_agent.domain.stage2 import ProposalProvenance, ProposedItem
from novel_agent.domain.world import PlanLevel, PlanNode
from novel_agent.services.content_addressing import content_id, plan_root_content_id
from novel_agent.services.editorial import EditorialReviewError, _enforce_content_contract
from novel_agent.services.planning_contracts import (
    chapter_plan_nodes,
    declared_obligations,
    effective_obligations,
)
from novel_agent.services.writer_change_reconciliation import WriterChangeReconciliationService
from tests.unit.test_stage2_paired_controller import COMMIT, CONFIG


def test_scoped_plan_keeps_all_ancestors_and_excludes_future_shared_promise() -> None:
    promise = StableId("promise.identity")
    story = PlanNode(
        plan_node_id=StableId("story"),
        node_type="story",
        title="Story",
        summary="The whole journey",
        plan_level=PlanLevel.STORY,
    )
    volume = PlanNode(
        plan_node_id=StableId("volume"),
        node_type="volume",
        title="Volume",
        summary="Earn trust",
        parent_id=story.plan_node_id,
        plan_level=PlanLevel.ARC_VOLUME,
        chapter_start=1,
        chapter_end=12,
        forbidden_outcomes=("Reveal the hidden identity",),
    )
    current = PlanNode(
        plan_node_id=StableId("chapter.2"),
        node_type="goal",
        title="Two",
        summary="Refuse the bargain",
        parent_id=volume.plan_node_id,
        chapter_start=2,
        chapter_end=2,
        obligation_ids=(promise,),
    )
    future = current.model_copy(
        update={
            "plan_node_id": StableId("chapter.10"),
            "chapter_start": 10,
            "chapter_end": 10,
            "summary": "Reveal the hidden identity",
        }
    )
    plan = PlanRootDocument(
        root_hash=CONFIG,
        schema_version=SchemaVersion("1.0.0"),
        nodes=(story, volume, current, future),
        chapter_goals=(
            ChapterGoal(
                goal_id=current.plan_node_id,
                chapter_index=2,
                summary=current.summary,
                obligation_ids=(promise,),
            ),
        ),
    )
    assert chapter_plan_nodes(plan, 2) == (story, volume, current)


def test_optional_contract_fields_preserve_legacy_plan_identity() -> None:
    raw = {
        "schema_version": "1.0.0",
        "nodes": [],
        "chapter_goals": [
            {
                "goal_id": "goal.1",
                "chapter_index": 1,
                "summary": "Refuse the bargain",
                "obligation_ids": [],
            }
        ],
    }
    plan = PlanRootDocument.model_validate_json(json.dumps({"root_hash": CONFIG.root, **raw}))
    assert plan_root_content_id(plan) == content_id(raw)
    changed = plan.model_copy(
        update={
            "chapter_goals": (
                plan.chapter_goals[0].model_copy(
                    update={"required_outcomes": ("Burn the contract",)}
                ),
            )
        }
    )
    assert plan_root_content_id(changed) != plan_root_content_id(plan)


def test_declared_promise_survives_without_becoming_an_observed_fact() -> None:
    item = ProposedItem(
        item_id=StableId("promise.identity"),
        kind="promise",
        provenance=ProposalProvenance.PLANNER_PROPOSED,
        payload={
            "summary": "Discover the mentor's identity",
            "not_before_chapter": 10,
            "due_chapter": 12,
        },
    )
    obligations = declared_obligations((item,))
    plan = PlanRootDocument(
        root_hash=CONFIG, schema_version=SchemaVersion("1.0.0"), obligations=obligations
    )
    world = WorldRootDocument(
        root_hash=CONFIG, schema_version=SchemaVersion("1.0.0"), source_commit=COMMIT
    )
    assert not world.obligations
    effective = effective_obligations(plan, world)[0]
    assert effective.status is ObligationStatus.OPEN
    assert effective.forbids_resolution(9)
    assert not effective.forbids_resolution(10)
    assert not effective.evidence_refs


def test_multiple_exact_matches_do_not_use_stale_observation_indexes() -> None:
    changes = tuple(
        CuratorChangeObservation(
            observation_id=StableId(f"observation.{i}"),
            subject_hint=f"person {i}",
            change_kind=MemoryHintChangeKind.CHANGE,
            predicate_hint="location",
            value_hint="tower",
        )
        for i in range(4)
    )
    hints = tuple(
        DeclaredMemoryHint(
            subject_hint=item.subject_hint,
            change_kind=item.change_kind,
            predicate_hint=item.predicate_hint,
            value_hint=item.value_hint,
            evidence_quote="entered the tower",
            confidence=0.9,
        )
        for item in reversed(changes)
    )
    observation = CuratorObservation(draft_id=ArtifactId("sha256:" + "a" * 64), changes=changes)
    result = WriterChangeReconciliationService().reconcile(observation.draft_id, hints, observation)
    assert len(result.matched) == 4
    assert not result.observed_only


def _review_input() -> EditorialReviewInput:
    return cast(
        EditorialReviewInput,
        SimpleNamespace(
            writing_task=SimpleNamespace(
                required_beats=("Burn the contract",),
                entry_conditions=(),
                acceptance_criteria=(),
                forbidden_reveals=(),
                mandatory_constraints=(),
                length_policy=SimpleNamespace(minimum_characters=10, maximum_characters=100),
            ),
            context=SimpleNamespace(unresolved_gaps=(), items=()),
            draft=SimpleNamespace(writer_receipt=SimpleNamespace(skill_receipts=())),
        ),
    )


def test_editor_cannot_pass_without_outcome_evidence() -> None:
    with pytest.raises(EditorialReviewError, match="every plan criterion"):
        _enforce_content_contract(
            _review_input(),
            "He burned the contract.",
            EditorReviewPayload(verdict=EditorialVerdict.PASS),
        )
    with pytest.raises(EditorialReviewError, match="absent"):
        _enforce_content_contract(
            _review_input(),
            "He read the contract.",
            EditorReviewPayload(
                verdict=EditorialVerdict.PASS,
                plan_assessments=(
                    PlanRequirementAssessment(
                        criterion_id="outcome.0",
                        satisfied=True,
                        rationale="The bargain is rejected.",
                        evidence_quotes=("He burned the contract.",),
                    ),
                ),
            ),
        )


def test_length_failure_routes_to_rewrite_before_acceptance() -> None:
    payload = EditorReviewPayload(
        verdict=EditorialVerdict.PASS,
        plan_assessments=(
            PlanRequirementAssessment(
                criterion_id="outcome.0",
                satisfied=True,
                rationale="The contract burns.",
                evidence_quotes=("Burnt.",),
            ),
        ),
    )
    result = _enforce_content_contract(_review_input(), "Burnt.", payload)
    assert result.verdict is EditorialVerdict.MAJOR_REWRITE
    assert result.rewrite_targets
    assert any("6 characters" in issue.description for issue in result.issues)


def test_grounded_history_is_delivered_in_frozen_writer_selections(tmp_path):
    from novel_agent.domain.benchmark import ChapterDocument, SceneDocument, TextRootDocument
    from novel_agent.domain.memory import CandidatePool, RetrievalUnitKind, Stage1QueryIntent
    from novel_agent.domain.stage2 import MemoryGatewayMode
    from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextBlock, TextSpanRef
    from novel_agent.services.artifacts import sha256_id
    from novel_agent.services.content_addressing import quote_hash
    from novel_agent.services.evidence_first_writer_context_assembler import (
        FrozenNeedEvidenceSelections,
    )
    from tests.unit.test_stage2_memory_gateway import gateway
    from tests.unit.test_stage2_paired_controller import VERSION, need, request, runner, unit

    block = TextBlock(
        block_id=StableId("block.history"),
        chapter_id=StableId("chapter.history"),
        scene_id=StableId("scene.history"),
        narrative_index=0,
        text="hero injury remains after the duel",
    )
    evidence = EvidenceRef(
        evidence_id=StableId("evidence.history"),
        root_hash=CONFIG,
        object_hash=sha256_id(block.text.encode()),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        quote_hash=quote_hash(block.text),
        span=TextSpanRef(block_id=block.block_id, start=0, end=len(block.text)),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=COMMIT,
    )
    historical = unit().model_copy(
        update={
            "unit_kind": RetrievalUnitKind.GROUNDED_BLOCK,
            "text": block.text,
            "evidence_refs": (evidence,),
        }
    )
    memory_need = need(intent=Stage1QueryIntent.EXACT_QUOTE).model_copy(
        update={"allowed_candidate_pools": (CandidatePool.GROUNDED,)}
    )
    service, artifacts = gateway(tmp_path, MemoryGatewayMode.DETERMINISTIC)
    service._paired = runner(memory_need, historical)
    text = TextRootDocument(
        root_hash=CONFIG,
        schema_version=VERSION,
        chapters=(
            ChapterDocument(
                chapter_id=block.chapter_id,
                chapter_index=1,
                scenes=(SceneDocument(scene_id=block.scene_id, scene_index=0, blocks=(block,)),),
            ),
        ),
    )
    result = service.resolve(request(memory_need), text, thread_id="grounded-history")
    frozen = FrozenNeedEvidenceSelections.model_validate_json(
        artifacts.read_verified(result.frozen_evidence_selections_artifact)
    )
    assert any(
        slice_.text == block.text for selection in frozen.selections for slice_ in selection.slices
    )
