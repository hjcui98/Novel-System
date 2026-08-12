from __future__ import annotations

import pytest

from novel_agent.domain.artifacts import RootKind
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ChangeOperation,
    ChangeOperationType,
    ExtractionRule,
    ObservedChangeSet,
    StateTransitionEdge,
    StateTransitionPolicy,
    StateTransitionRule,
    ValidationFinding,
    ValidationStatus,
    WorldRecordKind,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
)
from novel_agent.domain.memory import ObligationStatus, WorldRootDocument
from novel_agent.domain.world import TruthClass
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.benchmark_importer import text_root_content_id, world_root_content_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.curation import Stage1Curator
from novel_agent.services.overlay import OverlayError, WorldOverlay, build_candidate_bundle
from novel_agent.services.validation import Stage1Validator
from tests.factories import make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def _roots() -> tuple[WorldRootDocument, TextRootDocument]:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    future = next(root for root in bundle.text_roots if len(root.chapters) == 3)
    return world, future


def _obligation_rule() -> ExtractionRule:
    return ExtractionRule(
        rule_id=StableId("rule.resolve.north-tower"),
        phrase="进入北塔",
        operation=ChangeOperationType.REPLACE,
        record_kind=WorldRecordKind.OBLIGATION,
        target_id=StableId("obligation.synthetic.north-tower"),
        record={
            "obligation_id": "obligation.synthetic.north-tower",
            "kind": "objective",
            "description": "林澈需要进入北塔。",
            "status": "resolved",
            "owner_ids": ["entity.synthetic.lin-che"],
            "due_chapter": 23,
            "evidence_refs": [],
        },
    )


def _changes(world: WorldRootDocument) -> ObservedChangeSet:
    _, future = _roots()
    return Stage1Curator().extract(future, 23, world.source_commit, (_obligation_rule(),))


def test_curator_overlay_and_validator_form_a_traceable_candidate() -> None:
    world, future = _roots()
    changes = Stage1Curator().extract(
        future,
        23,
        world.source_commit,
        (
            _obligation_rule(),
            _obligation_rule().model_copy(
                update={"rule_id": StableId("rule.not-matched"), "phrase": "不存在的短语"}
            ),
        ),
    )
    assert len(changes.operations) == 1
    evidence = changes.operations[0].evidence_refs[0]
    assert evidence.chapter_id == StableId("chapter.synthetic.23")
    assert evidence.span is not None and evidence.span.end - evidence.span.start == len("进入北塔")

    proposed = WorldOverlay().apply(world, changes)
    assert proposed.obligations[0].status is ObligationStatus.RESOLVED
    assert proposed.obligations[0].evidence_refs == (evidence,)
    manifest = make_manifest().model_copy(update={"project_id": ProjectId("project.synthetic")})
    candidate = build_candidate_bundle(
        project_id=ProjectId("project.synthetic"),
        run_id=RunId("run.synthetic.23"),
        current_manifest=manifest,
        changes=changes,
        proposed_world=proposed,
    )

    report = Stage1Validator().validate(candidate, world, proposed, future)
    assert report.status is ValidationStatus.PASSED
    serialized_world_id = sha256_id(canonical_json_bytes(proposed.model_dump(mode="json")))
    assert candidate.proposed_roots.world_root.artifact_id == serialized_world_id
    assert serialized_world_id != proposed.root_hash
    assert candidate.produced_artifacts == (candidate.proposed_roots.world_root,)
    assert "transition.stage1-structural-v1" in report.validation_profile


def test_curator_rejects_unknown_chapter() -> None:
    world, future = _roots()
    with pytest.raises(LookupError, match="chapter does not exist"):
        Stage1Curator().extract(future, 99, world.source_commit, ())


def test_curator_does_not_inject_evidence_field_into_entity_records() -> None:
    world, future = _roots()
    rule = ExtractionRule(
        rule_id=StableId("rule.entity.introduced"),
        phrase="林澈",
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.ENTITY,
        target_id=StableId("entity.synthetic.new"),
        record={
            "entity_id": "entity.synthetic.new",
            "entity_type": "character",
            "internal_label": "新角色",
            "aliases": [],
            "identity_invariants": [],
        },
    )
    changes = Stage1Curator().extract(future, 21, world.source_commit, (rule,))
    payload = changes.operations[0].payload
    assert isinstance(payload, dict)
    record = payload["record"]
    assert isinstance(record, dict) and "evidence_refs" not in record


def _operation(
    world: WorldRootDocument,
    *,
    operation: ChangeOperationType = ChangeOperationType.REPLACE,
    target: str = "obligation.synthetic.north-tower",
    record_type: object = "obligation",
    record: object | None = None,
    root_kind: RootKind = RootKind.WORLD,
) -> ChangeOperation:
    raw_record = world.obligations[0].model_dump(mode="json") if record is None else record
    return ChangeOperation(
        operation_id=StableId("change.test"),
        root_kind=root_kind,
        operation=operation,
        target_id=StableId(target),
        payload={"record_type": record_type, "record": raw_record},  # type: ignore[dict-item]
    )


def _change_set(world: WorldRootDocument, operation: ChangeOperation) -> ObservedChangeSet:
    return _changes(world).model_copy(update={"operations": (operation,)})


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (lambda world: _operation(world, root_kind=RootKind.TEXT), "structured WorldRoot"),
        (lambda world: _operation(world, record_type=7), "payload is invalid"),
        (lambda world: _operation(world, record="bad"), "payload is invalid"),
        (
            lambda world: _operation(world, record={"obligation_id": "bad"}),
            "payload is invalid",
        ),
        (
            lambda world: _operation(world, target="obligation.synthetic.other"),
            "target does not match",
        ),
        (
            lambda world: _operation(world, operation=ChangeOperationType.CREATE),
            "already exists",
        ),
        (
            lambda world: _operation(
                world,
                operation=ChangeOperationType.REPLACE,
                target="obligation.synthetic.other",
                record=world.obligations[0]
                .model_copy(update={"obligation_id": StableId("obligation.synthetic.other")})
                .model_dump(mode="json"),
            ),
            "does not exist",
        ),
        (
            lambda world: _operation(
                world,
                operation=ChangeOperationType.RETIRE,
                target="obligation.synthetic.other",
                record=world.obligations[0]
                .model_copy(update={"obligation_id": StableId("obligation.synthetic.other")})
                .model_dump(mode="json"),
            ),
            "does not exist",
        ),
    ],
)
def test_overlay_fails_closed_for_invalid_operations(operation: object, message: str) -> None:
    world, _ = _roots()
    factory = operation
    assert callable(factory)
    with pytest.raises(OverlayError, match=message):
        WorldOverlay().apply(world, _change_set(world, factory(world)))


def test_overlay_create_and_retire_paths() -> None:
    world, _ = _roots()
    new_obligation = world.obligations[0].model_copy(
        update={"obligation_id": StableId("obligation.synthetic.second")}
    )
    create = _operation(
        world,
        operation=ChangeOperationType.CREATE,
        target="obligation.synthetic.second",
        record=new_obligation.model_dump(mode="json"),
    )
    created = WorldOverlay().apply(world, _change_set(world, create))
    assert len(created.obligations) == 2
    retire = _operation(world, operation=ChangeOperationType.RETIRE)
    retired = WorldOverlay().apply(world, _change_set(world, retire))
    assert retired.obligations == ()
    wrong_base = _change_set(world, retire).model_copy(
        update={"base_commit": CommitId("sha256:" + "7" * 64)}
    )
    with pytest.raises(OverlayError, match="base commit"):
        WorldOverlay().apply(world, wrong_base)


def test_validator_reports_all_high_risk_failures() -> None:
    world, future = _roots()
    rule = ExtractionRule(
        rule_id=StableId("rule.false-promotion"),
        phrase="据说",
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.EVENT,
        target_id=StableId("event.synthetic.rumor"),
        record={
            "event_id": "event.synthetic.rumor",
            "event_type": "rumored_event",
            "participant_ids": ["entity.synthetic.lin-che"],
            "effect_refs": [],
            "evidence_refs": [],
            "truth_class": TruthClass.ACCEPTED_WORLD_FACT.value,
        },
    )
    block = future.chapters[0].scenes[0].blocks[0]
    modified_block = block.model_copy(update={"text": "据说林澈已经进入北塔。"})
    modified_scene = future.chapters[0].scenes[0].model_copy(update={"blocks": (modified_block,)})
    modified_chapter = future.chapters[0].model_copy(update={"scenes": (modified_scene,)})
    provisional_root = future.model_copy(
        update={"root_hash": ArtifactId("sha256:" + "0" * 64), "chapters": (modified_chapter,)}
    )
    evidence_root = provisional_root.model_copy(
        update={"root_hash": text_root_content_id(provisional_root)}
    )
    changes = Stage1Curator().extract(evidence_root, 21, world.source_commit, (rule,))
    proposed = WorldOverlay().apply(world, changes)
    candidate = build_candidate_bundle(
        project_id=ProjectId("project.synthetic"),
        run_id=RunId("run.validation.failures"),
        current_manifest=make_manifest(),
        changes=changes,
        proposed_world=proposed,
    )

    report = Stage1Validator().validate(candidate, world, proposed, evidence_root)
    assert report.status is ValidationStatus.PASSED

    bad_operation = changes.operations[0].model_copy(update={"evidence_refs": ()})
    duplicate = bad_operation.model_copy(update={"operation_id": StableId("change.duplicate")})
    corrupted_changes = changes.model_copy(update={"operations": (bad_operation, duplicate)})
    corrupted_candidate = candidate.model_copy(
        update={
            "base_commit": CommitId("sha256:" + "3" * 64),
            "observed_changes": corrupted_changes,
        }
    )
    corrupted_world = proposed.model_copy(update={"root_hash": ArtifactId("sha256:" + "4" * 64)})
    report = Stage1Validator().validate(corrupted_candidate, world, corrupted_world, evidence_root)
    codes = {finding.code for finding in report.findings}
    assert {
        "BASE_MISMATCH",
        "ROOT_HASH_MISMATCH",
        "WRITE_CONFLICT",
        "MISSING_EVIDENCE",
        "INVALID_OVERLAY",
    }.issubset(codes)


def test_validator_detects_invalid_evidence_and_overlay_mismatch() -> None:
    world, future = _roots()
    changes = _changes(world)
    proposed = WorldOverlay().apply(world, changes)
    candidate = build_candidate_bundle(
        project_id=ProjectId("project.synthetic"),
        run_id=RunId("run.validation.mismatch"),
        current_manifest=make_manifest(),
        changes=changes,
        proposed_world=proposed,
    )
    operation = changes.operations[0]
    evidence = operation.evidence_refs[0].model_copy(
        update={"object_hash": ArtifactId("sha256:" + "5" * 64)}
    )
    bad_changes = changes.model_copy(
        update={"operations": (operation.model_copy(update={"evidence_refs": (evidence,)}),)}
    )
    bad_candidate = candidate.model_copy(update={"observed_changes": bad_changes})
    report = Stage1Validator().validate(bad_candidate, world, world, future)
    assert report.status is ValidationStatus.FAILED
    assert {finding.code for finding in report.findings} == {
        "INVALID_EVIDENCE",
        "OVERLAY_MISMATCH",
        "RECORD_EVIDENCE_MISMATCH",
    }


def _candidate_for_rule(
    world: WorldRootDocument,
    future: TextRootDocument,
    chapter_index: int,
    rule: ExtractionRule,
) -> tuple[CandidateChangeBundle, WorldRootDocument]:
    changes = Stage1Curator().extract(future, chapter_index, world.source_commit, (rule,))
    proposed = WorldOverlay().apply(world, changes)
    candidate = build_candidate_bundle(
        project_id=ProjectId("project.synthetic"),
        run_id=RunId(f"run.transition.{chapter_index}"),
        current_manifest=make_manifest(),
        changes=changes,
        proposed_world=proposed,
    )
    return candidate, proposed


def _state_rule(*, value: str, predicate: str = "injury") -> ExtractionRule:
    return ExtractionRule(
        rule_id=StableId(f"rule.state.{predicate}.{value}"),
        phrase="受伤仍未痊愈",
        operation=ChangeOperationType.REPLACE,
        record_kind=WorldRecordKind.STATE,
        target_id=StableId("state.synthetic.injury"),
        record={
            "state_id": "state.synthetic.injury",
            "subject_id": "entity.synthetic.lin-che",
            "predicate": predicate,
            "value": value,
            "valid_time": {"worldline": "main", "start_ordinal": 22},
            "evidence_refs": [],
            "truth_class": "accepted_world_fact",
        },
    )


def test_versioned_state_transition_policy_allows_and_rejects_exact_edges() -> None:
    world, future = _roots()
    policy = StateTransitionPolicy(
        policy_id=StableId("transition.synthetic-v1"),
        schema_version=SchemaVersion("0.1.0"),
        rules=(
            StateTransitionRule(
                predicate="injury",
                allowed=(
                    StateTransitionEdge(
                        from_value="not_healed",
                        to_value="worsened",
                    ),
                ),
            ),
        ),
        allow_unlisted_predicates=False,
    )
    allowed_candidate, allowed_world = _candidate_for_rule(
        world, future, 22, _state_rule(value="worsened")
    )
    allowed = Stage1Validator(policy).validate(allowed_candidate, world, allowed_world, future)
    assert allowed.status is ValidationStatus.PASSED
    assert "transition.synthetic-v1" in allowed.validation_profile

    denied_candidate, denied_world = _candidate_for_rule(
        world, future, 22, _state_rule(value="healed")
    )
    denied = Stage1Validator(policy).validate(denied_candidate, world, denied_world, future)
    assert "ILLEGAL_STATE_TRANSITION" in {finding.code for finding in denied.findings}

    unlisted_policy = policy.model_copy(update={"rules": ()})
    unlisted = Stage1Validator(unlisted_policy).validate(
        denied_candidate, world, denied_world, future
    )
    assert "UNLISTED_STATE_TRANSITION" in {finding.code for finding in unlisted.findings}


def test_validator_rejects_state_identity_record_evidence_and_narrative_mismatch() -> None:
    world, future = _roots()
    identity_candidate, identity_world = _candidate_for_rule(
        world, future, 22, _state_rule(value="not_healed", predicate="health")
    )
    identity = Stage1Validator().validate(identity_candidate, world, identity_world, future)
    assert "STATE_IDENTITY_MUTATION" in {finding.code for finding in identity.findings}

    changes = _changes(world)
    operation = changes.operations[0]
    assert isinstance(operation.payload, dict)
    raw_record = dict(operation.payload["record"])  # type: ignore[arg-type]
    raw_record["evidence_refs"] = []
    mismatched_operation = operation.model_copy(
        update={
            "payload": {
                "record_type": "obligation",
                "record": raw_record,
            }
        }
    )
    mismatched_changes = changes.model_copy(update={"operations": (mismatched_operation,)})
    mismatched_world = WorldOverlay().apply(world, mismatched_changes)
    mismatched_candidate = build_candidate_bundle(
        project_id=ProjectId("project.synthetic"),
        run_id=RunId("run.evidence-mismatch"),
        current_manifest=make_manifest(),
        changes=mismatched_changes,
        proposed_world=mismatched_world,
    )
    mismatch = Stage1Validator().validate(mismatched_candidate, world, mismatched_world, future)
    assert "RECORD_EVIDENCE_MISMATCH" in {finding.code for finding in mismatch.findings}

    event_rule = ExtractionRule(
        rule_id=StableId("rule.event.narrative-mismatch"),
        phrase="重申旧誓言",
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.EVENT,
        target_id=StableId("event.synthetic.narrative-mismatch"),
        record={
            "event_id": "event.synthetic.narrative-mismatch",
            "event_type": "promise_reaffirmed",
            "participant_ids": ["entity.synthetic.lin-che"],
            "narrative_order": {"chapter_index": 22},
            "effect_refs": [],
            "evidence_refs": [],
            "truth_class": "accepted_world_fact",
        },
    )
    event_candidate, event_world = _candidate_for_rule(world, future, 21, event_rule)
    event_report = Stage1Validator().validate(event_candidate, world, event_world, future)
    assert "NARRATIVE_ORDER_EVIDENCE_MISMATCH" in {
        finding.code for finding in event_report.findings
    }


def test_validator_rejects_reopening_terminal_obligation() -> None:
    world, future = _roots()
    resolved_obligation = world.obligations[0].model_copy(
        update={"status": ObligationStatus.RESOLVED}
    )
    provisional = world.model_copy(
        update={
            "root_hash": ArtifactId("sha256:" + "0" * 64),
            "obligations": (resolved_obligation,),
        }
    )
    resolved_world = provisional.model_copy(
        update={"root_hash": world_root_content_id(provisional)}
    )
    reopen_rule = _obligation_rule()
    reopen_record = dict(reopen_rule.record)
    reopen_record["status"] = "open"
    reopen_rule = reopen_rule.model_copy(update={"record": reopen_record})
    candidate, proposed = _candidate_for_rule(resolved_world, future, 23, reopen_rule)
    report = Stage1Validator().validate(candidate, resolved_world, proposed, future)
    assert "ILLEGAL_OBLIGATION_TRANSITION" in {finding.code for finding in report.findings}


def test_transition_checker_defers_malformed_and_unmaterialized_operations_to_overlay() -> None:
    world, future = _roots()
    changes = _changes(world)
    operation = changes.operations[0]
    malformed = operation.model_copy(update={"payload": "invalid"})
    non_string_kind = operation.model_copy(update={"payload": {"record_type": 7}})
    missing_state = operation.model_copy(
        update={
            "target_id": StableId("state.synthetic.missing"),
            "payload": {
                "record_type": "state",
                "record": {
                    "evidence_refs": [
                        evidence.model_dump(mode="json") for evidence in operation.evidence_refs
                    ]
                },
            },
        }
    )
    entity_replace = operation.model_copy(
        update={
            "target_id": world.entities[0].entity_id,
            "payload": {
                "record_type": "entity",
                "record": world.entities[0].model_dump(mode="json"),
            },
        }
    )
    candidate = build_candidate_bundle(
        project_id=ProjectId("project.synthetic"),
        run_id=RunId("run.transition.deferred"),
        current_manifest=make_manifest(),
        changes=changes.model_copy(
            update={
                "operations": (
                    malformed,
                    non_string_kind,
                    missing_state,
                    entity_replace,
                )
            }
        ),
        proposed_world=world,
    )
    findings: list[ValidationFinding] = []
    Stage1Validator()._check_transitions_and_order(candidate, world, world, future, findings)
    assert findings == []
