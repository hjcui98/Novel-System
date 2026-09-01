"""Contract coverage for the durable Planner-gap handoff."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory_write import (
    InformationBoundary,
    MemoryGapClassification,
    MemoryRepairFinding,
    MemoryRepairOwner,
    NarrativePosition,
    RepairScope,
)
from novel_agent.domain.runtime import TaskKind, TaskPurpose, TaskRecord, TaskStatus
from novel_agent.domain.stage2 import AccessScope, ContractRef
from novel_agent.domain.text import SourceBoundEvidenceRequirement, TextSpanRef

ROOT = Path(__file__).parents[2]
PROJECT = ProjectId("project.u8b.contract")
BASE = CommitId("sha256:" + "1" * 64)
VERSION = SchemaVersion("1.0.0")


def _artifact(digit: str = "2") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + digit * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=VERSION,
    )


def _boundary(*, base_commit: CommitId = BASE, maximum: int = 4) -> InformationBoundary:
    return InformationBoundary(
        boundary_id=StableId("boundary.u8b.contract"),
        base_commit=base_commit,
        maximum_visible_position=NarrativePosition(chapter_index=maximum),
        evaluator_sources_forbidden=True,
        policy_ref=ContractRef(
            contract_id=StableId("policy.u8b.boundary"),
            version=VERSION,
            content_hash=ArtifactId("sha256:" + "3" * 64),
        ),
    )


def _finding(**updates: object) -> MemoryRepairFinding:
    values: dict[str, object] = {
        "finding_id": StableId("finding.u8b.contract"),
        "incident_id": StableId("incident.u8b.contract"),
        "planner_run_id": RunId("run.u8b.contract"),
        "planner_task_id": TaskId("task.u8b.planner"),
        "planner_attempt_id": StableId("attempt.u8b.planner"),
        "planner_request_id": StableId("request.u8b.planner"),
        "planner_intent_ref": _artifact("4"),
        "planner_checkpoint_ref": _artifact("5"),
        "project_id": PROJECT,
        "base_commit": BASE,
        "information_boundary": _boundary(),
        "cutoff": NarrativePosition(chapter_index=4),
        "access_scope": AccessScope.WRITER_SAFE,
        "need_id": StableId("need.u8b.contract"),
        "need_query": "which current relation is missing?",
        "semantic_question": "which relation is supported by the visible source?",
        "classification": MemoryGapClassification.CANON_EXTRACTION_GAP,
        "repair_owner": MemoryRepairOwner.GRAPH_CURATOR,
        "target_root_kind": RootKind.WORLD,
        "repair_scope": RepairScope(field_paths=("relations",)),
        "no_progress_key": StableId("noprogress.u8b.contract"),
    }
    values.update(updates)
    return MemoryRepairFinding.model_validate(values)


def test_memory_repair_finding_round_trips_only_refs_and_binds_basis() -> None:
    finding = _finding(
        source_artifact_refs=(_artifact("6"),),
        source_visibility_receipt_refs=(_artifact("7"),),
        graph_receipt_refs=(_artifact("8"),),
        l0_receipt_refs=(_artifact("9"),),
        mandatory_facet_ids=(StableId("facet.relation"),),
    )

    rebuilt = MemoryRepairFinding.model_validate_json(finding.model_dump_json())
    assert rebuilt == finding
    assert {
        "gold",
        "future_text",
        "expected_answer",
        "target_realization",
    }.isdisjoint(finding.model_dump())


def test_source_bound_requirement_is_immutable_and_source_aligned() -> None:
    source = _artifact("6")
    requirement = SourceBoundEvidenceRequirement(
        source_artifact_id=source.artifact_id,
        source_chapter_index=4,
        source_chapter_id=StableId("chapter.u8b.contract.4"),
        required_span=TextSpanRef(
            block_id=StableId("block.u8b.contract.4.0"),
            start=10,
            end=20,
        ),
        required_consequence_markers=("consequence",),
    )
    finding = _finding(
        source_artifact_refs=(source,),
        source_visibility_receipt_refs=(_artifact("7"),),
        source_chapter_indices=(4,),
        source_evidence_requirement=requirement,
    )

    assert finding.source_evidence_requirement == requirement
    assert MemoryRepairFinding.model_validate_json(finding.model_dump_json()) == finding

    with pytest.raises(ValidationError, match="finding source"):
        _finding(
            source_artifact_refs=(source,),
            source_visibility_receipt_refs=(_artifact("7"),),
            source_chapter_indices=(4,),
            source_evidence_requirement=requirement.model_copy(
                update={"source_artifact_id": _artifact("8").artifact_id}
            ),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        (
            "repair_owner",
            MemoryRepairOwner.OPERATOR,
            "owner does not match",
        ),
        (
            "cutoff",
            NarrativePosition(chapter_index=5),
            "cutoff exceeds",
        ),
        (
            "information_boundary",
            _boundary(base_commit=CommitId("sha256:" + "a" * 64)),
            "base commit",
        ),
        (
            "source_visibility_receipt_refs",
            (_artifact("a"),),
            "one visibility receipt",
        ),
        (
            "source_chapter_indices",
            (5,),
            "source chapter exceeds",
        ),
    ),
)
def test_memory_repair_finding_rejects_unbound_inputs(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _finding(**{field: value})


@pytest.mark.parametrize(
    ("facet_id", "owner"),
    (
        (StableId("facet.relation_state"), MemoryRepairOwner.ORDINARY_CURATOR),
        (StableId("facet.causal_history"), MemoryRepairOwner.GRAPH_CURATOR),
        (StableId("facet.current_state"), MemoryRepairOwner.GRAPH_CURATOR),
    ),
)
def test_populated_finding_binds_owner_to_unresolved_facet_kind(
    facet_id: StableId, owner: MemoryRepairOwner
) -> None:
    with pytest.raises(ValidationError, match="unresolved facet kinds"):
        _finding(mandatory_facet_ids=(facet_id,), repair_owner=owner)


def test_derived_maintenance_task_requires_a_finding_artifact() -> None:
    values = {
        "task_id": TaskId("task.u8b.maintenance"),
        "run_id": RunId("run.u8b.contract"),
        "project_id": PROJECT,
        "kind": TaskKind.MAINTENANCE,
        "purpose": TaskPurpose.DERIVED_MAINTENANCE,
        "task_revision": 0,
        "status": TaskStatus.READY,
        "basis_commit": BASE,
        "policy_hash": "sha256:" + "b" * 64,
        "permission_hash": "sha256:" + "c" * 64,
    }
    with pytest.raises(ValidationError, match="finding artifact"):
        TaskRecord.model_validate(values)

    task = TaskRecord.model_validate({**values, "input_artifact_refs": (_artifact("d"),)})
    assert task.purpose is TaskPurpose.DERIVED_MAINTENANCE


def test_memory_repair_finding_schema_is_exported() -> None:
    path = ROOT / "schemas" / "stage2" / "MemoryRepairFinding.schema.json"
    assert json.loads(path.read_text(encoding="utf-8")) == MemoryRepairFinding.model_json_schema()
