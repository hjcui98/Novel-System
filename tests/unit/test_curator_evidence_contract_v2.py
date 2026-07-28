"""WP4/WP5: candidate-id binding and support enforcement."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from pydantic import ValidationError

from novel_agent.domain.benchmark import ChapterDocument, SceneDocument, TextRootDocument
from novel_agent.domain.changes import (
    ChangeOperationType,
    ChapterChangeDraftV2,
    CuratedOperationDraftV2,
    CuratorEntityRecord,
    CuratorStateRecord,
    CuratorStoryTime,
    EvidenceRepairAction,
    EvidenceRepairDraft,
    EvidenceSupportDisposition,
    WorldRecordKind,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.text import TextBlock
from novel_agent.domain.world import Entity, StateRecord, StoryTime, TruthClass
from novel_agent.services.evidence_candidates import EvidenceCandidateGenerator
from novel_agent.services.model_curation import (
    CuratorProposalSemanticRejected,
    EvidenceSemanticVerificationDraft,
    EvidenceSemanticVerificationItem,
    ModelCurationContractError,
    ModelCurator,
)


class _FakeGateway:
    def __init__(self, draft: ChapterChangeDraftV2) -> None:
        self._draft = draft
        self.requests: list[ModelRequest] = []

    async def generate_structured(self, request, model_type):
        self.requests.append(request)
        assert model_type is ChapterChangeDraftV2
        assert "EVIDENCE_CANDIDATES" in request.prompt
        call = type(
            "Call",
            (),
            {
                "request_id": StableId("model.1"),
                "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})(),
            },
        )()
        return self._draft, call


class _ModelVerifierGateway:
    def __init__(
        self,
        draft: ChapterChangeDraftV2,
        verification: EvidenceSemanticVerificationDraft | Exception,
    ) -> None:
        self._draft = draft
        self._verification = verification
        self.requests: list[ModelRequest] = []

    async def generate_structured(self, request, model_type):
        self.requests.append(request)
        call = type(
            "Call",
            (),
            {
                "request_id": request.request_id,
                "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})(),
            },
        )()
        if model_type is ChapterChangeDraftV2:
            return self._draft, call
        assert model_type is EvidenceSemanticVerificationDraft
        assert "EVIDENCE_VERIFICATION_INPUT" in request.prompt
        if isinstance(self._verification, Exception):
            raise self._verification
        return self._verification, call


def _root_with(text: str) -> TextRootDocument:
    block = TextBlock(
        block_id=StableId("block.c21"),
        chapter_id=StableId("chapter.21"),
        scene_id=StableId("scene.21"),
        narrative_index=0,
        text=text,
    )
    scene = SceneDocument(
        scene_id=StableId("scene.21"),
        scene_index=0,
        blocks=(block,),
    )
    chapter = ChapterDocument(
        chapter_id=StableId("chapter.21"),
        chapter_index=21,
        scenes=(scene,),
    )
    return TextRootDocument(
        root_hash=ArtifactId("sha256:" + "f" * 64),
        schema_version=SchemaVersion("0.1.0"),
        chapters=(chapter,),
    )


def _request(request_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=StableId(request_id),
        run_id=RunId("run.v2"),
        task_id=TaskId("task.v2"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace-v2",
        prompt="unused",
    )


def _world() -> WorldRootDocument:
    return WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "1" * 64),
        schema_version=SchemaVersion("0.1.0"),
        source_commit=CommitId("sha256:" + "2" * 64),
    )


def test_v2_binds_candidate_and_rejects_unrelated() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    good = next(item for item in candidates if "confidence" in item.text)
    draft = ChapterChangeDraftV2(
        chapter_index=21,
        operations=(
            CuratedOperationDraftV2(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.attitude"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.chen"),
                    predicate="cultivation-attitude",
                    value="extreme_confidence",
                    valid_time=CuratorStoryTime(worldline="main"),
                    truth_class=TruthClass.ASSERTION,
                ),
                evidence_candidate_ids=(good.candidate_id,),
            ),
        ),
    )
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=False)
    changes, _call, out = asyncio.run(
        curator.extract_reported_v2(
            root, 21, CommitId("sha256:" + "3" * 64), _world(), _request("req.v2"),
        )
    )
    assert out.chapter_index == 21
    assert changes.operations
    assert changes.operations[0].evidence_refs[0].span is not None
    assert changes.operations[0].evidence_refs[0].span.start == good.start

    from novel_agent.domain.changes import EvidenceCandidate
    from novel_agent.services.artifacts import sha256_id

    weak_text = "shuang-er appears at the door and starts mocking."
    weak = EvidenceCandidate(
        candidate_id=StableId("evidence-candidate.weak"),
        block_id=StableId("block.c21"),
        chapter_index=21,
        scene_index=0,
        text=weak_text,
        start=0,
        end=len(weak_text),
        content_hash=sha256_id(weak_text.encode("utf-8")),
    )
    # Inject weak candidate into generator catalog path via monkeypatched generate.
    gen_all = EvidenceCandidateGenerator()
    original_generate = gen_all.generate

    def _generate_with_weak(text_root, chapter_index):
        base = original_generate(text_root, chapter_index)
        return (*base, weak)

    gen_all.generate = _generate_with_weak  # type: ignore[method-assign]
    unrelated = ChapterChangeDraftV2(
        chapter_index=21,
        operations=(
            CuratedOperationDraftV2(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.attitude"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.chen"),
                    predicate="cultivation-attitude",
                    value="extreme_confidence",
                    valid_time=CuratorStoryTime(worldline="main"),
                    truth_class=TruthClass.ASSERTION,
                ),
                evidence_candidate_ids=(weak.candidate_id,),
            ),
        ),
    )
    bad_curator = ModelCurator(
        _FakeGateway(unrelated), evidence_generator=gen_all, enforce_support_gate=True
    )
    try:
        asyncio.run(
            bad_curator.extract_reported_v2(
                root, 21, CommitId("sha256:" + "3" * 64), _world(), _request("req.v2.bad"),
            )
        )
        raise AssertionError("expected unresolved-evidence rejection")
    except CuratorProposalSemanticRejected as exc:
        assert "UNRESOLVED" in exc.reason_code
        assert exc.operation_indexes == (0,)
        assert exc.violation_rule == "partial_evidence_unresolved_no_verifier"


def test_replay_agent_uses_candidate_v2() -> None:
    """CuratorReplayAgent must default to the CANDIDATE_ID_V2 evidence contract."""
    from novel_agent.agents.curator import CuratorReplayAgent
    from novel_agent.domain.stage2 import CuratorEvidenceContract
    from novel_agent.services.model_curation import ModelCurator

    agent = CuratorReplayAgent(
        cast(Any, ModelCurator(cast(Any, None))),
        cast(Any, None),
    )
    assert agent.evidence_contract is CuratorEvidenceContract.CANDIDATE_ID_V2

    legacy = CuratorReplayAgent(
        cast(Any, ModelCurator(cast(Any, None))),
        cast(Any, None),
        evidence_contract=CuratorEvidenceContract.LEGACY_OFFSET_V1,
    )
    assert legacy.evidence_contract is CuratorEvidenceContract.LEGACY_OFFSET_V1


class _RepairGateway:
    """Fake gateway for evidence_repair_v2 returning EvidenceRepairDraft lists."""

    def __init__(self, drafts: list[EvidenceRepairDraft]) -> None:
        self._drafts = drafts
        self.requests: list[ModelRequest] = []

    async def generate_structured(
        self, request: ModelRequest, model_type: object
    ) -> tuple[list[EvidenceRepairDraft], object]:
        self.requests.append(request)
        call = type(
            "Call",
            (),
            {
                "request_id": StableId("model.repair"),
                "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})(),
            },
        )()
        return list(self._drafts), call


_COMMIT = CommitId("sha256:" + "3" * 64)


def _v2_state_draft(candidate_id: StableId, chapter_index: int = 21) -> ChapterChangeDraftV2:
    return ChapterChangeDraftV2(
        chapter_index=chapter_index,
        operations=(
            CuratedOperationDraftV2(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.attitude"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.chen"),
                    predicate="cultivation-attitude",
                    value="extreme_confidence",
                    valid_time=CuratorStoryTime(worldline="main"),
                    truth_class=TruthClass.ASSERTION,
                ),
                evidence_candidate_ids=(candidate_id,),
            ),
        ),
    )


def _v2_no_op_draft(candidate_id: StableId) -> ChapterChangeDraftV2:
    return ChapterChangeDraftV2(
        chapter_index=21,
        operations=(),
        coverage=1.0,
        unresolved=(),
        declared_vs_observed_diff=(),
        no_durable_delta_reason="chapter only repeats accepted durable facts",
        no_op_evidence_candidate_ids=(candidate_id,),
    )


def test_v2_requires_operations_key() -> None:
    with pytest.raises(ValidationError, match="operations"):
        ChapterChangeDraftV2.model_validate({"chapter_index": 21})
    assert "operations" in ChapterChangeDraftV2.model_json_schema()["required"]


@pytest.mark.parametrize(
    "update",
    (
        {"no_durable_delta_reason": "not applicable with operations"},
        {"no_op_evidence_candidate_ids": (StableId("evidence-candidate.noop"),)},
    ),
)
def test_v2_rejects_no_op_proof_when_operations_exist(
    update: dict[str, object],
) -> None:
    draft = _v2_state_draft(StableId("evidence-candidate.support"))
    with pytest.raises(ValidationError, match="non-empty Curator draft"):
        ChapterChangeDraftV2.model_validate(
            {
                **draft.model_dump(),
                **update,
            }
        )


def test_v2_explicit_no_op_fails_closed_without_trusted_verifier() -> None:
    text = "chen repeats an already accepted durable fact."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    curator = ModelCurator(
        _FakeGateway(_v2_no_op_draft(candidate.candidate_id)),
        evidence_generator=gen,
        enforce_support_gate=True,
    )
    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_EMPTY_DELTA_UNVERIFIED",
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                _COMMIT,
                _world(),
                _request("req.v2.noop.no-verifier"),
            )
        )
    assert exc.value.violation_rule == "empty_delta_requires_trusted_verification"
    assert exc.value.operation_indexes == ()
    assert exc.value.json_pointers == (
        "/operations",
        "/no_durable_delta_reason",
        "/no_op_evidence_candidate_ids",
    )
    assert exc.value.safe_feedback == ("trusted no-op verifier is unavailable",)
    assert curator.last_no_op_verification is None


@pytest.mark.parametrize(
    ("update", "feedback"),
    (
        ({"coverage": 0.85}, "coverage must equal 1"),
        ({"unresolved": ("durable candidate omitted",)}, "unresolved must be empty"),
        (
            {"declared_vs_observed_diff": ("durable state differs",)},
            "declared_vs_observed_diff must be empty",
        ),
        ({"no_durable_delta_reason": None}, "no_durable_delta_reason is required"),
        (
            {"no_op_evidence_candidate_ids": ()},
            "no_op_evidence_candidate_ids are required",
        ),
    ),
)
def test_v2_incomplete_empty_draft_fails_closed_at_support_gate(
    update: dict[str, object],
    feedback: str,
) -> None:
    text = "chen repeats an already accepted durable fact."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    draft = _v2_no_op_draft(candidate.candidate_id).model_copy(update=update)
    curator = ModelCurator(
        _FakeGateway(draft),
        evidence_generator=gen,
        enforce_support_gate=True,
    )
    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_EMPTY_DELTA_UNVERIFIED",
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                _COMMIT,
                _world(),
                _request("req.v2.noop.incomplete"),
            )
        )
    assert feedback in exc.value.safe_feedback[0]
    assert exc.value.violation_rule == "empty_delta_requires_complete_proof"
    assert exc.value.json_pointers == (
        "/operations",
        "/coverage",
        "/unresolved",
        "/declared_vs_observed_diff",
        "/no_durable_delta_reason",
        "/no_op_evidence_candidate_ids",
    )


def test_v2_explicit_no_op_passes_when_trusted_verifier_accepts() -> None:
    text = "chen repeats an already accepted durable fact."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    verifier_calls: list[tuple[str, tuple[object, ...], object]] = []

    def verifier(reason, selected, world) -> tuple[bool, str]:
        verifier_calls.append((reason, selected, world))
        return (True, "NO_DURABLE_DELTA_CONFIRMED")

    curator = ModelCurator(
        _FakeGateway(_v2_no_op_draft(candidate.candidate_id)),
        evidence_generator=gen,
        enforce_support_gate=True,
        no_op_verifier=verifier,
    )
    changes, _call, draft = asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            _COMMIT,
            _world(),
            _request("req.v2.noop.verified"),
        )
    )
    assert changes.operations == ()
    assert draft.no_durable_delta_reason is not None
    assert verifier_calls
    assert verifier_calls[0][1][0] == candidate
    assert curator.last_no_op_verification == (True, "NO_DURABLE_DELTA_CONFIRMED")


@pytest.mark.parametrize("verifier_mode", ("reject", "raise"))
def test_v2_explicit_no_op_rejects_failed_verification(verifier_mode: str) -> None:
    text = "chen repeats an already accepted durable fact."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]

    def verifier(reason, selected, world) -> tuple[bool, str]:
        if verifier_mode == "raise":
            raise RuntimeError("verifier unavailable")
        return (False, "NEW_DURABLE_FACT_PRESENT")

    curator = ModelCurator(
        _FakeGateway(_v2_no_op_draft(candidate.candidate_id)),
        evidence_generator=gen,
        enforce_support_gate=True,
        no_op_verifier=verifier,
    )
    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_EMPTY_DELTA_UNVERIFIED",
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                _COMMIT,
                _world(),
                _request(f"req.v2.noop.{verifier_mode}"),
            )
        )
    if verifier_mode == "raise":
        assert exc.value.safe_feedback == ("trusted no-op verifier is unavailable",)
        assert curator.last_no_op_verification is None
    else:
        assert "NEW_DURABLE_FACT_PRESENT" in exc.value.safe_feedback[0]
        assert curator.last_no_op_verification == (
            False,
            "NEW_DURABLE_FACT_PRESENT",
        )


def test_v2_explicit_no_op_can_pass_when_support_gate_disabled() -> None:
    text = "chen repeats an already accepted durable fact."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    curator = ModelCurator(
        _FakeGateway(_v2_no_op_draft(candidate.candidate_id)),
        evidence_generator=gen,
        enforce_support_gate=False,
    )
    changes, _call, _draft = asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            _COMMIT,
            _world(),
            _request("req.v2.noop.gate-disabled"),
        )
    )
    assert changes.operations == ()
    assert curator.last_no_op_verification is None


def test_v2_rejects_unknown_no_op_evidence_candidate_id() -> None:
    text = "chen repeats an already accepted durable fact."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    draft = _v2_no_op_draft(StableId("evidence-candidate.missing"))
    curator = ModelCurator(
        _FakeGateway(draft),
        evidence_generator=gen,
        enforce_support_gate=True,
    )
    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_INFORMATION_BOUNDARY",
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                _COMMIT,
                _world(),
                _request("req.v2.noop.unknown-candidate"),
            )
        )
    assert exc.value.json_pointers == ("/no_op_evidence_candidate_ids/0",)
    assert exc.value.safe_feedback == (
        "evidence-candidate.missing: unknown evidence candidate",
    )


def _v2_parent_changes(
    root: TextRootDocument,
    gen: EvidenceCandidateGenerator,
    candidate_id: StableId,
    request_id: str = "req.parent",
) -> Any:
    draft = _v2_state_draft(candidate_id)
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=False)
    changes, _call, _out = asyncio.run(
        curator.extract_reported_v2(root, 21, _COMMIT, _world(), _request(request_id))
    )
    return changes


def test_v2_rejects_draft_with_mismatched_chapter_index() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    good = next(item for item in candidates if "confidence" in item.text)
    draft = _v2_state_draft(good.candidate_id, chapter_index=99)
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=True)
    with pytest.raises(ModelCurationContractError, match="draft chapter"):
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.chapter-mismatch")
            )
        )


def test_v2_rejects_unknown_evidence_candidate_id() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    draft = ChapterChangeDraftV2(
        chapter_index=21,
        operations=(
            CuratedOperationDraftV2(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.attitude"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.chen"),
                    predicate="cultivation-attitude",
                    value="extreme_confidence",
                    valid_time=CuratorStoryTime(worldline="main"),
                    truth_class=TruthClass.ASSERTION,
                ),
                evidence_candidate_ids=(StableId("evidence-candidate.missing"),),
            ),
        ),
    )
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=True)
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_INFORMATION_BOUNDARY"
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.unknown-candidate")
            )
        )
    assert exc.value.violation_rule == "candidate_id_must_belong_to_chapter"
    assert exc.value.information_boundary is True
    assert exc.value.safe_feedback == ("evidence-candidate.missing: unknown evidence candidate",)
    assert exc.value.json_pointers == ("/operations/0/evidence_candidate_ids/0",)


def test_v2_skips_support_gate_enforcement_when_disabled() -> None:
    text = (
        "chen holds extreme_confidence and cultivation-attitude firmly! "
        "The weather is sunny and calm today!"
    )
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    unrelated = next(
        item for item in candidates if "confidence" not in item.text and "attitude" not in item.text
    )
    draft = _v2_state_draft(unrelated.candidate_id)
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=False)
    changes, _call, _out = asyncio.run(
        curator.extract_reported_v2(root, 21, _COMMIT, _world(), _request("req.v2.gate-disabled"))
    )
    # Unrelated evidence is bound despite not supporting the record.
    assert changes.operations
    assert changes.operations[0].evidence_refs[0].span is not None
    assert curator.last_support_decisions
    assert curator.last_partial_support_decisions == ()


def test_v2_entity_record_omits_evidence_refs_from_record_payload() -> None:
    text = "chen the character arrives with cultivation-attitude."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    good = next(item for item in candidates if "chen" in item.text)
    draft = ChapterChangeDraftV2(
        chapter_index=21,
        operations=(
            CuratedOperationDraftV2(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.ENTITY,
                target_id=StableId("entity.chen"),
                record=CuratorEntityRecord(
                    entity_type="character",
                    internal_label="chen",
                ),
                evidence_candidate_ids=(good.candidate_id,),
            ),
        ),
    )
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=False)
    changes, _call, _out = asyncio.run(
        curator.extract_reported_v2(root, 21, _COMMIT, _world(), _request("req.v2.entity"))
    )
    payload = changes.operations[0].payload
    assert isinstance(payload, dict) and isinstance(payload["record"], dict)
    assert "evidence_refs" not in payload["record"]


def test_evidence_repair_v2_replaces_evidence_with_new_candidate_ids() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    good = next(item for item in candidates if "confidence" in item.text)
    other = next(item for item in candidates if item.candidate_id != good.candidate_id)
    parent_changes = _v2_parent_changes(root, gen, good.candidate_id)

    repair = EvidenceRepairDraft(
        operation_index=0,
        replacement_candidate_ids=(other.candidate_id,),
        action=EvidenceRepairAction.REPLACE_EVIDENCE,
    )
    gateway = _RepairGateway([repair])
    curator = ModelCurator(gateway, evidence_generator=gen, enforce_support_gate=True)
    changes, _call, drafts = asyncio.run(
        curator.evidence_repair_v2(
            root, 21, _COMMIT, parent_changes, _request("req.repair.replace")
        )
    )

    assert drafts == (repair,)
    assert len(changes.operations) == 1
    new_evidence = changes.operations[0].evidence_refs[0]
    assert new_evidence.span is not None
    assert new_evidence.span.start == other.start
    assert new_evidence.span.end == other.end
    assert changes.change_set_id.root.startswith("changes.model.repair.")
    # Operation identity and payload are preserved; only evidence_refs change.
    assert changes.operations[0].operation_id == parent_changes.operations[0].operation_id
    assert changes.operations[0].target_id == parent_changes.operations[0].target_id
    assert changes.operations[0].operation == parent_changes.operations[0].operation
    assert changes.operations[0].payload == parent_changes.operations[0].payload
    assert "EVIDENCE_REPAIR_INPUT" in gateway.requests[0].prompt
    assert curator.last_evidence_candidates


def test_evidence_repair_v2_drops_operation_on_drop_action() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    good = next(item for item in candidates if "confidence" in item.text)
    parent_changes = _v2_parent_changes(root, gen, good.candidate_id)

    repair = EvidenceRepairDraft(
        operation_index=0,
        replacement_candidate_ids=(),
        action=EvidenceRepairAction.DROP_OPERATION,
    )
    curator = ModelCurator(
        _RepairGateway([repair]), evidence_generator=gen, enforce_support_gate=True
    )
    changes, _call, drafts = asyncio.run(
        curator.evidence_repair_v2(root, 21, _COMMIT, parent_changes, _request("req.repair.drop"))
    )

    assert drafts == (repair,)
    assert changes.operations == ()


def test_evidence_repair_v2_keeps_operation_on_mark_unresolved() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    good = next(item for item in candidates if "confidence" in item.text)
    parent_changes = _v2_parent_changes(root, gen, good.candidate_id)

    repair = EvidenceRepairDraft(
        operation_index=0,
        replacement_candidate_ids=(),
        action=EvidenceRepairAction.MARK_UNRESOLVED,
    )
    curator = ModelCurator(
        _RepairGateway([repair]), evidence_generator=gen, enforce_support_gate=True
    )
    changes, _call, drafts = asyncio.run(
        curator.evidence_repair_v2(
            root, 21, _COMMIT, parent_changes, _request("req.repair.unresolved")
        )
    )

    assert drafts == (repair,)
    assert changes.operations == parent_changes.operations


def test_evidence_repair_v2_passes_through_operations_without_repair_draft() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    good = next(item for item in candidates if "confidence" in item.text)
    parent_changes = _v2_parent_changes(root, gen, good.candidate_id)

    curator = ModelCurator(_RepairGateway([]), evidence_generator=gen, enforce_support_gate=True)
    changes, _call, drafts = asyncio.run(
        curator.evidence_repair_v2(root, 21, _COMMIT, parent_changes, _request("req.repair.noop"))
    )

    assert drafts == ()
    assert changes.operations == parent_changes.operations


def test_evidence_repair_v2_rejects_unknown_replacement_candidate() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    good = next(item for item in candidates if "confidence" in item.text)
    parent_changes = _v2_parent_changes(root, gen, good.candidate_id)

    repair = EvidenceRepairDraft(
        operation_index=0,
        replacement_candidate_ids=(StableId("evidence-candidate.missing"),),
        action=EvidenceRepairAction.REPLACE_EVIDENCE,
    )
    curator = ModelCurator(
        _RepairGateway([repair]), evidence_generator=gen, enforce_support_gate=True
    )
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_INFORMATION_BOUNDARY"
    ) as exc:
        asyncio.run(
            curator.evidence_repair_v2(
                root, 21, _COMMIT, parent_changes, _request("req.repair.unknown")
            )
        )
    assert exc.value.violation_rule == "candidate_id_must_belong_to_chapter"
    assert exc.value.information_boundary is True
    assert exc.value.json_pointers == ("/operations/0/evidence_candidate_ids/0",)


def test_evidence_repair_v2_filters_parent_ops_by_repair_operation_indexes() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    good = next(item for item in candidates if "confidence" in item.text)
    parent_changes = _v2_parent_changes(root, gen, good.candidate_id)

    repair = EvidenceRepairDraft(
        operation_index=0,
        replacement_candidate_ids=(good.candidate_id,),
        action=EvidenceRepairAction.REPLACE_EVIDENCE,
    )
    gateway = _RepairGateway([repair])
    curator = ModelCurator(gateway, evidence_generator=gen, enforce_support_gate=True)
    asyncio.run(
        curator.evidence_repair_v2(
            root,
            21,
            _COMMIT,
            parent_changes,
            _request("req.repair.scoped"),
            repair_operation_indexes=(0,),
        )
    )

    sent = gateway.requests[0].prompt
    assert "REPAIR_OPERATIONS=" in sent
    assert parent_changes.operations[0].target_id.root in sent


# -- Evidence Support Gate: disposition-aware enforcement (P0 repair) --


def test_v2_rejects_contradicts_evidence_without_verifier() -> None:
    """A CONTRADICTS candidate (negation near primary token) is hard-rejected."""
    text = "chen does not have cultivation-attitude anymore."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    bad = candidates[0]
    draft = _v2_state_draft(bad.candidate_id)
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=True)
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_EVIDENCE_UNSUPPORTED"
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.contradicts")
            )
        )
    assert exc.value.violation_rule == "candidate_text_contradicts_or_unrelated"
    assert exc.value.operation_indexes == (0,)


def test_v2_partial_evidence_passes_when_verifier_supports() -> None:
    """A PARTIAL candidate is admitted when the semantic verifier returns SUPPORTS."""
    text = "the weather is sunny and calm today."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    weak = candidates[0]
    draft = _v2_state_draft(weak.candidate_id)

    verifier_calls: list[tuple[object, object]] = []

    def verifier(record, candidate) -> tuple[EvidenceSupportDisposition, str]:
        verifier_calls.append((record, candidate))
        return (EvidenceSupportDisposition.SUPPORTS, "VERIFIER_OK")

    curator = ModelCurator(
        _FakeGateway(draft),
        evidence_generator=gen,
        enforce_support_gate=True,
        semantic_verifier=verifier,
    )
    changes, _call, _out = asyncio.run(
        curator.extract_reported_v2(root, 21, _COMMIT, _world(), _request("req.v2.verifier-ok"))
    )
    assert changes.operations
    assert verifier_calls
    assert curator.last_partial_support_decisions


def test_v2_partial_evidence_rejected_when_verifier_contradicts() -> None:
    """A PARTIAL candidate is rejected when the verifier downgrades it."""
    text = "the weather is sunny and calm today."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    weak = candidates[0]
    draft = _v2_state_draft(weak.candidate_id)

    def verifier(record, candidate) -> tuple[EvidenceSupportDisposition, str]:
        return (EvidenceSupportDisposition.CONTRADICTS, "VERIFIER_CONTRADICTS")

    curator = ModelCurator(
        _FakeGateway(draft),
        evidence_generator=gen,
        enforce_support_gate=True,
        semantic_verifier=verifier,
    )
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_EVIDENCE_UNSUPPORTED"
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.verifier-no")
            )
        )
    assert exc.value.violation_rule == "semantic_verifier_rejected_partial"
    assert exc.value.operation_indexes == (0,)


def test_v2_partial_evidence_fails_closed_without_verifier() -> None:
    """A PARTIAL candidate with no verifier fails closed (UNRESOLVED)."""
    text = "the weather is sunny and calm today."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    weak = candidates[0]
    draft = _v2_state_draft(weak.candidate_id)
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=True)
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_EVIDENCE_UNRESOLVED"
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.no-verifier")
            )
        )
    assert exc.value.violation_rule == "partial_evidence_unresolved_no_verifier"
    assert exc.value.operation_indexes == (0,)


def test_v2_partial_evidence_fails_closed_when_verifier_raises() -> None:
    """A verifier that raises is treated as unresolved and fails closed."""
    text = "the weather is sunny and calm today."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    weak = candidates[0]
    draft = _v2_state_draft(weak.candidate_id)

    def verifier(record, candidate) -> tuple[EvidenceSupportDisposition, str]:
        raise RuntimeError("verifier crashed")

    curator = ModelCurator(
        _FakeGateway(draft),
        evidence_generator=gen,
        enforce_support_gate=True,
        semantic_verifier=verifier,
    )
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_EVIDENCE_UNRESOLVED"
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.verifier-crash")
            )
        )
    assert exc.value.violation_rule == "partial_evidence_unresolved_no_verifier"


def test_v2_model_semantic_verifier_batches_partial_evidence_once() -> None:
    text = "陈长生已经开始阅读道藏并正式开始学习修行"
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    draft = _v2_state_draft(candidate.candidate_id)
    verification = EvidenceSemanticVerificationDraft(
        decisions=(
            EvidenceSemanticVerificationItem(
                operation_index=0,
                candidate_ids=(candidate.candidate_id,),
                disposition=EvidenceSupportDisposition.SUPPORTS,
                reason_code="DIRECT_SEMANTIC_SUPPORT",
            ),
        )
    )
    gateway = _ModelVerifierGateway(draft, verification)
    curator = ModelCurator(
        cast(Any, gateway),
        evidence_generator=gen,
        enforce_support_gate=True,
        enable_model_semantic_verifier=True,
    )

    request = _request("req.v2.model-verifier").model_copy(
        update={"timeout_seconds": 120}
    )
    changes, _call, _out = asyncio.run(
        curator.extract_reported_v2(
            root, 21, _COMMIT, _world(), request
        )
    )

    assert changes.operations
    assert len(gateway.requests) == 2
    assert gateway.requests[1].request_id.root.endswith(".semantic-verifier")
    assert gateway.requests[1].timeout_seconds == 90
    assert "半个时辰 is one hour and never half_hour" in gateway.requests[1].prompt
    assert "supports only a record that explicitly encodes belief" in (
        gateway.requests[1].prompt
    )
    assert "Evaluate all excerpts for one operation collectively" in (
        gateway.requests[1].prompt
    )
    assert "accepted records must be durable World state" in gateway.requests[1].prompt
    assert "Emit only durable world-state deltas" in gateway.requests[0].prompt
    assert "one-scene encounters" in gateway.requests[0].prompt
    assert "general rules, hypotheticals, maxima" in gateway.requests[0].prompt
    prompt = gateway.requests[0].prompt
    assert prompt.index("</CURATOR_INPUT>") < prompt.index("<CURATOR_OUTPUT_CONTRACT")
    assert "MUST be copied verbatim" in prompt
    assert "Do not restate facts already present in WORLD" in prompt
    assert "composite method or process MUST cite" in prompt
    assert "half_shichen is not half_hour" in prompt
    assert "encoded as a belief/estimate/claim" in prompt
    assert "every semantic component" in prompt
    assert "no_durable_delta_reason MUST be null" in prompt
    assert "no_op_evidence_candidate_ids MUST be an empty array" in prompt
    assert curator.last_prompt_fingerprint is not None


def test_v2_model_semantic_verifier_schema_bounds_generation() -> None:
    schema = EvidenceSemanticVerificationDraft.model_json_schema()

    assert schema["properties"]["decisions"]["maxItems"] == 4
    item_schema = schema["$defs"]["EvidenceSemanticVerificationItem"]
    assert item_schema["properties"]["reason_code"]["minLength"] == 1
    assert item_schema["properties"]["reason_code"]["maxLength"] == 160


def test_v2_model_semantic_verifier_evaluates_combined_operation_evidence() -> None:
    root = _root_with("陈长生先反复阅读道藏。随后摘录要点整理笔记。")
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert len(candidates) >= 2
    candidate_ids = (candidates[0].candidate_id, candidates[1].candidate_id)
    base = _v2_state_draft(candidate_ids[0])
    draft = base.model_copy(
        update={
            "operations": (
                base.operations[0].model_copy(
                    update={"evidence_candidate_ids": candidate_ids}
                ),
            )
        }
    )
    verification = EvidenceSemanticVerificationDraft(
        decisions=(
            EvidenceSemanticVerificationItem(
                operation_index=0,
                candidate_ids=candidate_ids,
                disposition=EvidenceSupportDisposition.SUPPORTS,
                reason_code="COMBINED_EVIDENCE_SUPPORT",
            ),
        )
    )
    gateway = _ModelVerifierGateway(draft, verification)
    curator = ModelCurator(
        cast(Any, gateway),
        evidence_generator=gen,
        enforce_support_gate=True,
        enable_model_semantic_verifier=True,
    )

    changes, _call, _out = asyncio.run(
        curator.extract_reported_v2(
            root, 21, _COMMIT, _world(), _request("req.v2.combined-verifier")
        )
    )

    assert changes.operations
    assert len(gateway.requests) == 2
    verifier_prompt = gateway.requests[1].prompt
    assert all(candidate_id.root in verifier_prompt for candidate_id in candidate_ids)
    assert '"evidence":[' in verifier_prompt


def test_v2_model_semantic_verifier_drops_only_rejected_operations() -> None:
    root = _root_with("陈长生的读书方法十分特殊。天气晴朗。")
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert len(candidates) >= 2
    supported = candidates[0]
    rejected = candidates[-1]
    first = _v2_state_draft(supported.candidate_id).operations[0]
    draft = ChapterChangeDraftV2(
        chapter_index=21,
        operations=(
            first,
            first.model_copy(
                update={
                    "target_id": StableId("state.unsupported"),
                    "record": CuratorStateRecord(
                        subject_id=StableId("entity.chen"),
                        predicate="has_unrelated_fact",
                        value="not_in_evidence",
                        valid_time=CuratorStoryTime(worldline="current"),
                        truth_class=TruthClass.ASSERTION,
                    ),
                    "evidence_candidate_ids": (rejected.candidate_id,),
                }
            ),
        ),
    )
    verification = EvidenceSemanticVerificationDraft(
        decisions=(
            EvidenceSemanticVerificationItem(
                operation_index=0,
                candidate_ids=(supported.candidate_id,),
                disposition=EvidenceSupportDisposition.SUPPORTS,
                reason_code="DIRECT_SUPPORT",
            ),
            EvidenceSemanticVerificationItem(
                operation_index=1,
                candidate_ids=(rejected.candidate_id,),
                disposition=EvidenceSupportDisposition.UNRELATED,
                reason_code="NO_MATERIAL_SUPPORT",
            ),
        )
    )
    gateway = _ModelVerifierGateway(draft, verification)
    curator = ModelCurator(
        cast(Any, gateway),
        evidence_generator=gen,
        enforce_support_gate=True,
        enable_model_semantic_verifier=True,
    )

    changes, _call, filtered = asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            _COMMIT,
            _world(),
            _request("req.v2.model-verifier-partial-accept"),
        )
    )

    assert len(changes.operations) == 1
    assert len(filtered.operations) == 1
    assert filtered.operations[0].target_id == first.target_id
    assert len(gateway.requests) == 2
    assert len(curator.last_operation_filter_receipts) == 1
    receipt = curator.last_operation_filter_receipts[0]
    assert receipt.reason == "evidence_support_rejected"
    assert receipt.support_disposition is EvidenceSupportDisposition.UNRELATED
    assert receipt.support_reason_code == "NO_MATERIAL_SUPPORT"


def test_v2_compact_world_omits_historical_evidence_identifiers() -> None:
    world = WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "1" * 64),
        schema_version=SchemaVersion("0.1.0"),
        source_commit=_COMMIT,
        entities=(
            Entity(
                entity_id=StableId("entity.chen"),
                entity_type="character",
                internal_label="陈长生",
            ),
        ),
        states=(
            StateRecord(
                state_id=StableId("state.existing"),
                subject_id=StableId("entity.chen"),
                predicate="has_status",
                value="existing",
                valid_time=StoryTime(worldline="current", start_ordinal=20),
                truth_class=TruthClass.ASSERTION,
            ),
        ),
    )

    view = ModelCurator._world_model_view(world)

    assert "root_hash" not in view
    assert "source_commit" not in view
    assert "schema_version" not in view
    assert "evidence_refs" not in view["states"][0]


def test_v2_filters_existing_semantic_duplicate_before_candidate_scope_check() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = next(
        item for item in gen.generate(root, 21) if "confidence" in item.text
    )
    existing_record = CuratorStateRecord(
        subject_id=StableId("entity.chen"),
        predicate="has_cultivation_status",
        value="commenced_study",
        valid_time=CuratorStoryTime(worldline="current", start_ordinal=20),
        truth_class=TruthClass.ASSERTION,
    )
    draft = ChapterChangeDraftV2(
        chapter_index=21,
        operations=(
            CuratedOperationDraftV2(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.duplicate-under-new-id"),
                record=existing_record,
                evidence_candidate_ids=(
                    StableId("evidence-candidate.from-old-chapter"),
                ),
            ),
            _v2_state_draft(candidate.candidate_id).operations[0],
            CuratedOperationDraftV2(
                operation=ChangeOperationType.REPLACE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.replace-missing"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.chen"),
                    predicate="has_new_fact",
                    value="new_value",
                    valid_time=CuratorStoryTime(
                        worldline="current",
                        start_ordinal=21,
                    ),
                    truth_class=TruthClass.ASSERTION,
                ),
                evidence_candidate_ids=(candidate.candidate_id,),
            ),
        ),
    )
    world = WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "1" * 64),
        schema_version=SchemaVersion("0.1.0"),
        source_commit=_COMMIT,
        entities=(
            Entity(
                entity_id=StableId("entity.chen"),
                entity_type="character",
                internal_label="陈长生",
            ),
        ),
        states=(
            StateRecord(
                state_id=StableId("state.cultivation-start"),
                subject_id=existing_record.subject_id,
                predicate=existing_record.predicate,
                value=existing_record.value,
                valid_time=StoryTime.model_validate(
                    existing_record.valid_time.model_dump()
                ),
                truth_class=existing_record.truth_class,
            ),
        ),
    )
    gateway = _FakeGateway(draft)
    curator = ModelCurator(
        cast(Any, gateway),
        evidence_generator=gen,
        enforce_support_gate=False,
    )

    changes, _call, normalized = asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            _COMMIT,
            world,
            _request("req.v2.filter-existing"),
        )
    )

    assert len(changes.operations) == 2
    assert len(normalized.operations) == 2
    assert normalized.operations[0].target_id == StableId("state.attitude")
    assert normalized.operations[1].target_id == StableId("state.replace-missing")
    assert normalized.operations[1].operation is ChangeOperationType.CREATE
    assert len(curator.last_operation_filter_receipts) == 1
    receipt = curator.last_operation_filter_receipts[0]
    assert receipt.proposed_target_id == StableId("state.duplicate-under-new-id")
    assert receipt.existing_target_id == StableId("state.cultivation-start")
    assert receipt.reason == "existing_semantic_duplicate"


def test_v2_normalizes_fully_duplicate_proposal_to_verified_no_op() -> None:
    text = "chen repeats an already accepted durable fact."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    existing_record = CuratorStateRecord(
        subject_id=StableId("entity.chen"),
        predicate="has_cultivation_status",
        value="commenced_study",
        valid_time=CuratorStoryTime(worldline="current", start_ordinal=22),
        truth_class=TruthClass.ASSERTION,
    )
    draft = ChapterChangeDraftV2(
        chapter_index=21,
        operations=(
            CuratedOperationDraftV2(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.duplicate-under-new-id"),
                record=existing_record,
                evidence_candidate_ids=(
                    StableId("evidence-candidate.from-old-chapter"),
                ),
            ),
        ),
        coverage=0.8,
        unresolved=("state.unresolved",),
    )
    world = WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "1" * 64),
        schema_version=SchemaVersion("0.1.0"),
        source_commit=_COMMIT,
        entities=(
            Entity(
                entity_id=StableId("entity.chen"),
                entity_type="character",
                internal_label="陈长生",
            ),
        ),
        states=(
            StateRecord(
                state_id=StableId("state.cultivation-start"),
                subject_id=existing_record.subject_id,
                predicate=existing_record.predicate,
                value=existing_record.value,
                valid_time=StoryTime.model_validate(
                    existing_record.valid_time.model_dump()
                ),
                truth_class=existing_record.truth_class,
            ),
        ),
    )
    curator = ModelCurator(
        _FakeGateway(draft),
        evidence_generator=gen,
        enforce_support_gate=True,
    )

    changes, _call, normalized = asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            _COMMIT,
            world,
            _request("req.v2.all-duplicates"),
        )
    )

    assert changes.operations == ()
    assert normalized.operations == ()
    assert normalized.coverage == 1.0
    assert normalized.unresolved == ()
    assert normalized.declared_vs_observed_diff == ()
    assert normalized.no_durable_delta_reason == (
        "all proposed operations already exist in Canonical World"
    )
    assert normalized.no_op_evidence_candidate_ids == ()
    assert curator.last_no_op_verification == (
        True,
        "ALL_OPERATIONS_ALREADY_CANONICAL",
    )
    assert len(curator.last_operation_filter_receipts) == 1
    assert (
        curator.last_operation_filter_receipts[0].reason
        == "existing_semantic_duplicate"
    )


@pytest.mark.parametrize(
    "verification",
    [
        EvidenceSemanticVerificationDraft(decisions=()),
        EvidenceSemanticVerificationDraft(
            decisions=(
                EvidenceSemanticVerificationItem(
                    operation_index=0,
                    candidate_ids=(StableId("evidence-candidate.wrong"),),
                    disposition=EvidenceSupportDisposition.SUPPORTS,
                    reason_code="WRONG_ID",
                ),
            )
        ),
        EvidenceSemanticVerificationDraft(
            decisions=(
                EvidenceSemanticVerificationItem(
                    operation_index=0,
                    candidate_ids=(StableId("evidence-candidate.duplicate"),),
                    disposition=EvidenceSupportDisposition.SUPPORTS,
                    reason_code="WRONG_ID",
                ),
                EvidenceSemanticVerificationItem(
                    operation_index=0,
                    candidate_ids=(StableId("evidence-candidate.duplicate"),),
                    disposition=EvidenceSupportDisposition.SUPPORTS,
                    reason_code="DUPLICATE",
                ),
            )
        ),
        RuntimeError("model verifier unavailable"),
    ],
)
def test_v2_model_semantic_verifier_fails_closed_on_incomplete_batch(
    verification: EvidenceSemanticVerificationDraft | Exception,
) -> None:
    root = _root_with("中文证据需要语义核验。")
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    if isinstance(verification, EvidenceSemanticVerificationDraft) and len(
        verification.decisions
    ) == 2:
        verification = verification.model_copy(
            update={
                "decisions": tuple(
                    item.model_copy(
                        update={"candidate_ids": (candidate.candidate_id,)}
                    )
                    for item in verification.decisions
                )
            }
        )
    gateway = _ModelVerifierGateway(
        _v2_state_draft(candidate.candidate_id),
        verification,
    )
    curator = ModelCurator(
        cast(Any, gateway),
        evidence_generator=gen,
        enforce_support_gate=True,
        enable_model_semantic_verifier=True,
    )

    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_EVIDENCE_UNRESOLVED",
    ):
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.bad-model-verifier")
            )
        )


# -- Production wiring (P0 repair) --


def test_production_default_flags_enforce_support_gate_and_fail_closed_on_partial() -> None:
    """The E2E runner wires ModelCurator with enforce_support_gate derived from
    QualityRepairFeatureFlags defaults; with no semantic_verifier injected (smoke),
    PARTIAL must fail closed and CONTRADICTS must be hard-rejected.
    """
    from novel_agent.domain.stage2 import (
        EvidenceSupportGateMode,
        QualityRepairFeatureFlags,
    )

    flags = QualityRepairFeatureFlags()
    assert flags.evidence_support_gate is EvidenceSupportGateMode.ENFORCE_PRE_CANDIDATE
    enforce = (
        flags.evidence_support_gate is EvidenceSupportGateMode.ENFORCE_PRE_CANDIDATE
    )
    assert enforce is True

    # PARTIAL fails closed (no verifier wired in production smoke).
    text = "the weather is sunny and calm today."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    weak = candidates[0]
    draft = _v2_state_draft(weak.candidate_id)
    curator = ModelCurator(
        _FakeGateway(draft),
        evidence_generator=gen,
        enforce_support_gate=enforce,
    )
    assert curator._semantic_verifier is None
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_EVIDENCE_UNRESOLVED"
    ):
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.prod-partial")
            )
        )

    # CONTRADICTS is hard-rejected.
    bad_text = "chen does not have cultivation-attitude anymore."
    bad_root = _root_with(bad_text)
    bad_candidates = gen.generate(bad_root, 21)
    assert bad_candidates
    bad_draft = _v2_state_draft(bad_candidates[0].candidate_id)
    bad_curator = ModelCurator(
        _FakeGateway(bad_draft),
        evidence_generator=gen,
        enforce_support_gate=enforce,
    )
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_EVIDENCE_UNSUPPORTED"
    ):
        asyncio.run(
            bad_curator.extract_reported_v2(
                bad_root, 21, _COMMIT, _world(), _request("req.v2.prod-contradicts")
            )
        )
