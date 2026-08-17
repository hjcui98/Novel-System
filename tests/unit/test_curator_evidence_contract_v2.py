"""WP4/WP5: candidate-id binding and support enforcement."""

# mypy: disable-error-code="no-untyped-def,arg-type,index"

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from pydantic import ValidationError

from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.domain.benchmark import ChapterDocument, SceneDocument, TextRootDocument
from novel_agent.domain.changes import (
    ChangeOperationType,
    ChapterChangeDraftV2,
    CuratedOperationDraftV2,
    CuratorEntityRecord,
    CuratorObligationRecord,
    CuratorStateRecord,
    CuratorStoryTime,
    CuratorV2EvidenceDraft,
    CuratorV2OperationDraft,
    EvidenceCandidate,
    EvidenceRepairAction,
    EvidenceRepairDraft,
    EvidenceRepairDraftArray,
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
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.evidence_candidates import EvidenceCandidateGenerator
from novel_agent.services.model_curation import (
    CuratorProposalSemanticRejected,
    EvidenceSemanticDecisionDraft,
    EvidenceSemanticVerificationDraft,
    EvidenceSemanticVerificationItem,
    ModelCurationContractError,
    ModelCurator,
    NoOpSemanticVerificationDraft,
)
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint


class _FakeGateway:
    def __init__(self, draft: ChapterChangeDraftV2) -> None:
        self._draft = draft
        self.requests: list[ModelRequest] = []

    async def generate_structured(self, request, model_type, **kwargs):
        self.requests.append(request)
        assert model_type is CuratorV2EvidenceDraft
        assert "json_object_framing" not in kwargs
        assert "EVIDENCE_CANDIDATES" in request.prompt
        assert "keep no_durable_delta_reason under 80 characters" in request.prompt
        assert "Never emit an empty no_op_evidence_quotes" in request.prompt
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

    async def generate_structured(self, request, model_type, **kwargs):
        self.requests.append(request)
        call = type(
            "Call",
            (),
            {
                "request_id": request.request_id,
                "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})(),
            },
        )()
        if model_type is CuratorV2EvidenceDraft:
            return self._draft, call
        assert issubclass(model_type, EvidenceSemanticDecisionDraft)
        assert "EVIDENCE_VERIFICATION_INPUT" in request.prompt
        if isinstance(self._verification, Exception):
            raise self._verification
        return self._verification, call


class _NoOpVerifierGateway:
    def __init__(
        self,
        draft: ChapterChangeDraftV2,
        verification: NoOpSemanticVerificationDraft | Exception,
    ) -> None:
        self._draft = draft
        self._verification = verification
        self.requests: list[ModelRequest] = []

    async def generate_structured(self, request, model_type, **kwargs):
        self.requests.append(request)
        call = type(
            "Call",
            (),
            {
                "request_id": request.request_id,
                "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})(),
            },
        )()
        if model_type is CuratorV2EvidenceDraft:
            return self._draft, call
        assert model_type is NoOpSemanticVerificationDraft
        assert "NO_OP_VERIFICATION_INPUT" in request.prompt
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
        entities=(
            Entity(
                entity_id=StableId("entity.chen"),
                entity_type="character",
                internal_label="陈长生",
            ),
        ),
    )


def test_v2_binds_candidate_and_rejects_unrelated() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    good = next(item for item in candidates if "confidence" in item.text)
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
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
                evidence_quotes=(good.text,),
            ),
        ),
    )
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=False)
    changes, _call, out = asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            CommitId("sha256:" + "3" * 64),
            _world(),
            _request("req.v2"),
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
    unrelated = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
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
                evidence_quotes=(weak.text,),
            ),
        ),
    )
    bad_curator = ModelCurator(
        _FakeGateway(unrelated), evidence_generator=gen_all, enforce_support_gate=True
    )
    try:
        asyncio.run(
            bad_curator.extract_reported_v2(
                root,
                21,
                CommitId("sha256:" + "3" * 64),
                _world(),
                _request("req.v2.bad"),
            )
        )
        raise AssertionError("expected unresolved-evidence rejection")
    except CuratorProposalSemanticRejected as exc:
        assert exc.reason_code == "CURATOR_PROPOSAL_INVALID_EVIDENCE"
        assert exc.operation_indexes == (0,)
        assert exc.violation_rule == "evidence_quote_must_match_chapter_catalog"


def test_v2_schema_excludes_relation_and_materializes_exact_quote_span() -> None:
    schema = CuratorV2EvidenceDraft.model_json_schema()
    assert "CuratorRelationRecord" not in str(schema)

    text = "陈长生进入国教学院, 并在庭前停下。"
    root = _root_with(text)
    quote = "陈长生进入国教学院"
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.location"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.chen"),
                    predicate="location",
                    value="国教学院",
                    valid_time=CuratorStoryTime(worldline="main"),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
                evidence_quotes=(quote,),
            ),
        ),
    )
    changes, _call, resolved = asyncio.run(
        ModelCurator(_FakeGateway(draft), enforce_support_gate=False).extract_reported_v2(
            root,
            21,
            _COMMIT,
            _world(),
            _request("req.v2.exact-span"),
        )
    )

    evidence = changes.operations[0].evidence_refs[0]
    assert evidence.span is not None
    block = root.chapters[0].scenes[0].blocks[0]
    assert block.text[evidence.span.start : evidence.span.end] == quote
    exact = next(
        item
        for item in ModelCurator(
            _FakeGateway(draft), enforce_support_gate=False
        )._evidence_generator.resolve_exact_evidence_quotes(
            (quote,),
            EvidenceCandidateGenerator().generate(root, 21),
            root.chapters[0],
        )
    )
    assert resolved.operations[0].evidence_candidate_ids == (exact.candidate_id,)


def test_v2_places_mandatory_repair_contract_at_absolute_prompt_tail() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    generator = EvidenceCandidateGenerator()
    candidate = next(item for item in generator.generate(root, 21) if "confidence" in item.text)
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.attitude.repaired"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.chen"),
                    predicate="cultivation-attitude",
                    value="extreme_confidence",
                    valid_time=CuratorStoryTime(worldline="main"),
                    truth_class=TruthClass.ASSERTION,
                ),
                evidence_quotes=(candidate.text,),
            ),
        ),
    )
    gateway = _FakeGateway(draft)
    curator = ModelCurator(
        gateway,
        evidence_generator=generator,
        enforce_support_gate=False,
    )
    feedback = (
        '{"reason_code":"CURATOR_PROPOSAL_DANGLING_ENTITY_REFERENCE",'
        '"json_pointers":["/operations/1/record/subject_id"],'
        '"violation_rule":"referenced_entity_must_exist_or_be_created_in_same_proposal",'
        '"invalid_ids":["entity.guojiao-academy"]}'
    )

    asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            _COMMIT,
            _world(),
            _request("req.v2.repair-tail"),
            contract_prompt="SYSTEM CONTRACT",
            repair_feedback=feedback,
        )
    )

    prompt = gateway.requests[0].prompt
    assert prompt.endswith("</MANDATORY_PROPOSAL_REPAIR_CONTRACT>")
    assert prompt.rfind("<MANDATORY_PROPOSAL_REPAIR_CONTRACT") > prompt.rfind(
        "</CURATOR_OUTPUT_CONTRACT>"
    )
    assert "Return a complete replacement Draft, not a patch" in prompt
    assert "Before responding, self-check the complete replacement Draft" in prompt
    assert "/operations/1/record/subject_id" in prompt
    assert "entity.guojiao-academy" in prompt
    assert "prepend one CREATE operation for that exact entity ID" in prompt
    assert "Never reference a new entity from a state, event, or obligation" in prompt


def test_v2_contract_requires_subject_bearing_evidence_quotes() -> None:
    """2026-08-14 repair §28.8: state quotes must name their subject.

    ch1 smoke showed state operations rejected by the support gate because the
    model quoted subject-less fragments (e.g. only "十四岁。").  The gate is
    correct; the ordinary Curator contract must demand subject-bearing full
    sentences so exact evidence alone identifies who the fact is about.
    """
    text = "陈长生今年十四岁 是御东神将府的客人。"
    root = _root_with(text)
    generator = EvidenceCandidateGenerator()
    candidate = next(item for item in generator.generate(root, 21) if "十四岁" in item.text)
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.age"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.chen"),
                    predicate="age",
                    value="fourteen",
                    valid_time=CuratorStoryTime(worldline="main"),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
                evidence_quotes=(candidate.text,),
            ),
        ),
    )
    gateway = _FakeGateway(draft)
    curator = ModelCurator(
        gateway,
        evidence_generator=generator,
        enforce_support_gate=False,
    )
    asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            _COMMIT,
            _world(),
            _request("req.v2.subject-bearing"),
        )
    )
    prompt = gateway.requests[0].prompt
    assert "subject-bearing full sentence" in prompt
    assert "names the record's subject entity" in prompt
    assert "bare value fragment such as a lone number" in prompt
    assert "propose every durable delta the chapter establishes" in prompt
    assert "cannot support the record" in prompt


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
    """Fake gateway for evidence_repair_v2 returning EvidenceRepairDraftArray."""

    def __init__(self, drafts: list[EvidenceRepairDraft]) -> None:
        self._drafts = drafts
        self.requests: list[ModelRequest] = []

    async def generate_structured(
        self, request: ModelRequest, model_type: object
    ) -> tuple[EvidenceRepairDraftArray, object]:
        self.requests.append(request)
        call = type(
            "Call",
            (),
            {
                "request_id": StableId("model.repair"),
                "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})(),
            },
        )()
        return EvidenceRepairDraftArray(tuple(self._drafts)), call


_COMMIT = CommitId("sha256:" + "3" * 64)


def _v2_state_draft(evidence: object, chapter_index: int = 21) -> CuratorV2EvidenceDraft:
    quote = evidence.text if isinstance(evidence, EvidenceCandidate) else str(evidence)
    return CuratorV2EvidenceDraft(
        chapter_index=chapter_index,
        operations=(
            CuratorV2OperationDraft(
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
                evidence_quotes=(quote,),
            ),
        ),
    )


def _v2_no_op_draft(evidence: object) -> CuratorV2EvidenceDraft:
    quote = evidence.text if isinstance(evidence, EvidenceCandidate) else str(evidence)
    return CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(),
        coverage=1.0,
        unresolved=(),
        declared_vs_observed_diff=(),
        no_durable_delta_reason="chapter only repeats accepted durable facts",
        no_op_evidence_quotes=(quote,),
    )


def test_v2_quote_operation_domain_edges() -> None:
    from novel_agent.domain.changes import CuratorV2OperationDraft

    base = dict(
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
    )
    with pytest.raises(ValidationError, match="must not be blank"):
        CuratorV2OperationDraft.model_validate(base | {"evidence_quotes": ("  ",)})
    with pytest.raises(ValidationError, match="must be unique"):
        CuratorV2OperationDraft.model_validate(
            base | {"evidence_quotes": ("重复引用内容足够长", "重复引用内容足够长")}
        )
    with pytest.raises(ValidationError, match="does not match typed record"):
        CuratorV2OperationDraft.model_validate(
            base
            | {
                "record_kind": WorldRecordKind.ENTITY,
                "evidence_quotes": ("有效引用",),
            }
        )


def test_v2_evidence_draft_domain_edges() -> None:
    base = dict(
        chapter_index=21,
        operations=(),
        coverage=1.0,
        unresolved=(),
        declared_vs_observed_diff=(),
        no_durable_delta_reason="chapter only repeats accepted durable facts",
        no_op_evidence_quotes=("有效引用",),
    )
    with pytest.raises(ValidationError, match="non-empty Curator draft"):
        CuratorV2EvidenceDraft.model_validate(
            base | {"operations": (_v2_state_draft("有效引用").operations[0],)}
        )
    with pytest.raises(ValidationError, match="requires a no-durable-delta reason"):
        CuratorV2EvidenceDraft.model_validate(base | {"no_durable_delta_reason": None})
    with pytest.raises(ValidationError, match="must not be blank"):
        CuratorV2EvidenceDraft.model_validate(base | {"no_op_evidence_quotes": ("  ",)})
    with pytest.raises(ValidationError, match="at most 240 characters"):
        CuratorV2EvidenceDraft.model_validate(base | {"no_op_evidence_quotes": ("证" * 241,)})
    operation = _v2_state_draft("有效引用").operations[0]
    with pytest.raises(ValidationError, match="at most 240 characters"):
        CuratorV2OperationDraft.model_validate(
            operation.model_dump() | {"evidence_quotes": ("证" * 241,)}
        )


def test_evidence_quote_resolver_rejects_single_char_and_unresolved() -> None:
    gen = EvidenceCandidateGenerator()
    with pytest.raises(ValueError, match="too short"):
        gen.resolve_evidence_quotes(("妖",), ())
    with pytest.raises(ValueError, match="unresolved"):
        gen.resolve_evidence_quotes(("有效引用内容足够长",), ())
    # short but semantically complete quotes bind when unique
    cand = EvidenceCandidate(
        candidate_id=StableId("evidence-candidate.oppose"),
        block_id=StableId("block.oppose"),
        chapter_index=21,
        scene_index=0,
        text="\u201c我反对。",
        start=0,
        end=6,
        content_hash=ArtifactId("sha256:" + "f" * 64),
    )
    assert gen.resolve_evidence_quotes(("我反对",), (cand,)) == (cand,)


def test_closest_quote_hint_advertises_copyable_literal_when_resolvable() -> None:
    text = "陈长生在客栈整理道藏笔记。"
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    draft = _v2_state_draft("短")
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=True)
    with pytest.raises(CuratorProposalSemanticRejected, match="INVALID_EVIDENCE") as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.too-short-hint")
            )
        )
    feedback = exc.value.safe_feedback[0]
    assert "copy this exact catalog text verbatim" in feedback
    literal = feedback.split("copy this exact catalog text verbatim as the evidence quote: ", 1)[1]
    resolved = gen.resolve_evidence_quotes((literal,), gen.generate(root, 21))
    assert len(resolved) == 1


def test_too_short_quote_without_copyable_literal_uses_generic_guidance() -> None:
    """A too-short quote with no bounded resolver-valid literal must get
    truthful longer-fragment guidance, never an unverified nearest literal."""

    text = "\u9648\u957f\u751f\u62ac\u8d77\u5934\u3002\u9648\u957f\u751f\u62ac\u8d77\u5934\u3002"
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert gen.copyable_literal_for("\u77ed", candidates, max_chars=160) is None
    draft = _v2_state_draft("\u77ed")
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=True)
    with pytest.raises(CuratorProposalSemanticRejected, match="INVALID_EVIDENCE") as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.too-short-generic")
            )
        )
    feedback = exc.value.safe_feedback[0]
    assert "copy this exact catalog text verbatim" not in feedback
    assert "too short" in feedback
    assert "longer verbatim fragment" in feedback


def test_closest_candidate_skips_punctuation_only_spans() -> None:
    gen = EvidenceCandidateGenerator()
    punct = EvidenceCandidate(
        candidate_id=StableId("evidence-candidate.punct"),
        block_id=StableId("block.punct"),
        chapter_index=21,
        scene_index=0,
        text="\u2026\u2026\uff01\uff1f",
        start=0,
        end=4,
        content_hash=ArtifactId("sha256:" + "e" * 64),
    )
    content = punct.model_copy(
        update={
            "candidate_id": StableId("evidence-candidate.content"),
            "text": "我只能进国教学院",
        }
    )
    assert gen.closest_candidate("我只能进国教学院", (punct, content)) is content
    assert gen.closest_candidate("完全无关的内容文本", (punct, content)) is None


def test_evidence_quote_resolver_ambiguous_joined_and_pair_branches() -> None:
    gen = EvidenceCandidateGenerator()
    cue = EvidenceCandidate(
        candidate_id=StableId("evidence-candidate.cue"),
        block_id=StableId("block.dialogue"),
        chapter_index=21,
        scene_index=0,
        text="宁婆婆看着他面无表情说道\uff1a",
        start=0,
        end=12,
        content_hash=ArtifactId("sha256:" + "d" * 64),
    )
    content = cue.model_copy(
        update={
            "candidate_id": StableId("evidence-candidate.content"),
            "text": "\u201c但你只能进国教学院。",
            "start": 12,
            "end": 25,
        }
    )
    cue2 = cue.model_copy(
        update={
            "candidate_id": StableId("evidence-candidate.cue2"),
            "text": "宁婆婆看着他面无表情说道\uff1a",
        }
    )
    content2 = cue2.model_copy(
        update={
            "candidate_id": StableId("evidence-candidate.content2"),
            "text": "\u201c但你只能进国教学院。",
        }
    )
    quote = "宁婆婆看着他面无表情说道\uff1a\u201c但你只能进国教学院。"
    with pytest.raises(ValueError, match="candidate pairs"):
        gen.resolve_evidence_quotes((quote,), (cue, content, cue2, content2))
    # pair_bound only: quote is a strict substring of the joined adjacent
    # pair but contains no complete candidate
    a = cue.model_copy(
        update={"candidate_id": StableId("evidence-candidate.pair-a"), "text": "AAAAAAAAAA"}
    )
    b = cue.model_copy(
        update={"candidate_id": StableId("evidence-candidate.pair-b"), "text": "BBBBBBBB"}
    )
    assert gen.resolve_evidence_quotes(("AAAABBBB",), (a, b)) == (b,)
    # len(pair_bound) == 0 -> unresolved
    with pytest.raises(ValueError, match="unresolved"):
        gen.resolve_evidence_quotes(("完全不在目录中的引用内容足够长",), (cue, content))
    # len(pair_bound) == 0 when a longer candidate contains the quote: quote
    # exactly in a single candidate still resolves via exact
    assert gen.resolve_evidence_quotes(("AAAAAAAAAA",), (a, b)) == (a,)


def test_evidence_quote_resolver_binds_dialogue_span_across_split_boundary() -> None:
    gen = EvidenceCandidateGenerator()
    cue = EvidenceCandidate(
        candidate_id=StableId("evidence-candidate.cue"),
        block_id=StableId("block.dialogue"),
        chapter_index=21,
        scene_index=0,
        text="宁婆婆看着他面无表情说道\uff1a",
        start=0,
        end=12,
        content_hash=ArtifactId("sha256:" + "c" * 64),
    )
    content = cue.model_copy(
        update={
            "candidate_id": StableId("evidence-candidate.content"),
            "text": "\u201c但你只能进国教学院。",
            "start": 12,
            "end": 25,
        }
    )
    other = cue.model_copy(
        update={
            "candidate_id": StableId("evidence-candidate.other"),
            "text": "另一个完全不同的原文片段",
        }
    )
    quote = "宁婆婆看着他面无表情说道\uff1a\u201c但你只能进国教学院。"
    resolved = gen.resolve_evidence_quotes((quote,), (cue, content, other))
    assert resolved == (content,)
    with pytest.raises(ValueError, match="ambiguous"):
        gen.resolve_evidence_quotes(
            ("宁婆婆看着他面无表情说道\uff1a\u201c但你只能进国教学院。另一个完全不同的原文片段",),
            (cue, content, other),
        )


def test_evidence_quote_resolver_binds_quote_containing_candidate() -> None:
    gen = EvidenceCandidateGenerator()
    candidate = EvidenceCandidate(
        candidate_id=StableId("evidence-candidate.span"),
        block_id=StableId("block.span"),
        chapter_index=21,
        scene_index=0,
        text="我不能拿第二或者第三",
        start=0,
        end=12,
        content_hash=ArtifactId("sha256:" + "b" * 64),
    )
    other = candidate.model_copy(
        update={
            "candidate_id": StableId("evidence-candidate.other"),
            "text": "另一个完全不同的原文片段",
        }
    )
    quote = "陈长生看着他说道\uff1a\u201c我不能拿第二或者第三\uff0c我只能拿第一。"
    resolved = gen.resolve_evidence_quotes((quote,), (candidate, other))
    assert resolved == (candidate,)
    assert gen.resolve_evidence_quotes((candidate.text,), (candidate, other)) == (candidate,)
    with pytest.raises(ValueError, match="ambiguous"):
        gen.resolve_evidence_quotes(
            ("包含两个候选的原文\u3002我不能拿第二或者第三\uff0c另一个完全不同的原文片段",),
            (candidate, other),
        )


def test_evidence_quote_resolver_ambiguity_fails_closed() -> None:
    first = EvidenceCandidate(
        candidate_id=StableId("evidence-candidate.ambig-1"),
        block_id=StableId("block.ambig"),
        chapter_index=21,
        scene_index=0,
        text="同一个足够长的原文片段出现在两个候选里",
        start=0,
        end=20,
        content_hash=ArtifactId("sha256:" + "a" * 64),
    )
    second = first.model_copy(
        update={
            "candidate_id": StableId("evidence-candidate.ambig-2"),
            "text": "另一个不同的足够长的原文片段",
        }
    )
    gen = EvidenceCandidateGenerator()
    with pytest.raises(ValueError, match="ambiguous"):
        gen.resolve_evidence_quotes(("足够长的原文片段",), (first, second))
    resolved = gen.resolve_evidence_quotes((first.text,), (first, second))
    assert resolved == (first,)
    assert gen.resolve_evidence_quotes((second.text,), (first, second)) == (second,)


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
                evidence_candidate_ids=(StableId("evidence-candidate.v2"),),
            ),
        ),
    )
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
        _FakeGateway(_v2_no_op_draft(candidate)),
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
            {"no_op_evidence_quotes": ()},
            "no_op_evidence_quotes are required",
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
    draft = _v2_no_op_draft(candidate).model_copy(update=update)
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
        "/no_op_evidence_quotes",
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
        _FakeGateway(_v2_no_op_draft(candidate)),
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


def test_v2_explicit_no_op_can_use_narrow_model_verifier() -> None:
    text = "chen repeats an already accepted durable fact."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    draft = _v2_no_op_draft(candidate)
    gateway = _NoOpVerifierGateway(
        draft,
        NoOpSemanticVerificationDraft(
            selected_candidate_ids=(candidate.candidate_id,),
            verified_no_durable_delta=True,
            reason_code="ONLY_ACCEPTED_FACT_REPEATED",
        ),
    )
    curator = ModelCurator(
        cast(Any, gateway),
        evidence_generator=gen,
        enforce_support_gate=True,
        enable_model_semantic_verifier=True,
    )

    changes, _call, _draft = asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            _COMMIT,
            _world(),
            _request("req.v2.noop.model-verified"),
        )
    )

    assert changes.operations == ()
    assert len(gateway.requests) == 2
    assert curator.last_no_op_verification == (True, "ONLY_ACCEPTED_FACT_REPEATED")


def test_v2_no_op_model_verifier_fails_closed_on_candidate_binding_mismatch() -> None:
    text = "chen repeats an already accepted durable fact."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    gateway = _NoOpVerifierGateway(
        _v2_no_op_draft(candidate),
        NoOpSemanticVerificationDraft(
            selected_candidate_ids=(StableId("evidence-candidate.wrong"),),
            verified_no_durable_delta=True,
            reason_code="BINDING_MISMATCH",
        ),
    )
    curator = ModelCurator(
        cast(Any, gateway),
        evidence_generator=gen,
        enforce_support_gate=True,
        enable_model_semantic_verifier=True,
    )

    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_EMPTY_DELTA_UNVERIFIED",
    ):
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                _COMMIT,
                _world(),
                _request("req.v2.noop.model-binding-mismatch"),
            )
        )

    assert curator.last_no_op_verification is None


def test_v2_no_op_model_verifier_fails_closed_when_gateway_raises() -> None:
    text = "chen repeats an already accepted durable fact."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    gateway = _NoOpVerifierGateway(
        _v2_no_op_draft(candidate),
        RuntimeError("verifier unavailable"),
    )
    curator = ModelCurator(
        cast(Any, gateway),
        evidence_generator=gen,
        enforce_support_gate=True,
        enable_model_semantic_verifier=True,
    )

    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_EMPTY_DELTA_UNVERIFIED",
    ):
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                _COMMIT,
                _world(),
                _request("req.v2.noop.model-error"),
            )
        )

    assert curator.last_no_op_verification is None


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
        _FakeGateway(_v2_no_op_draft(candidate)),
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
        _FakeGateway(_v2_no_op_draft(candidate)),
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


def test_v2_rejection_feedback_advertises_resolver_valid_copyable_literal() -> None:
    text = "\u5b81\u5a46\u5a46\u9762\u65e0\u8868\u60c5\u8bf4\u9053\uff1a"
    text += "\u6211\u53ea\u80fd\u8fdb\u56fd\u6559\u5b66\u9662\u3002"
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    failing_quote = (
        "\u5b81\u5a46\u5a46\u770b\u7740\u4ed6\u9762\u65e0\u8868\u60c5\u8bf4\u9053\uff1a"
        "\u4f46\u4f60\u53ea\u80fd\u8fdb\u56fd\u6559\u5b66\u9662\u3002"
    )
    draft = _v2_state_draft(failing_quote)
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=True)
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_INVALID_EVIDENCE"
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.closest-hint")
            )
        )
    feedback = exc.value.safe_feedback[0]
    assert "copy this exact catalog text verbatim" in feedback
    literal = feedback.split("copy this exact catalog text verbatim as the evidence quote: ", 1)[1]
    resolved = gen.resolve_evidence_quotes((literal,), candidates)
    assert len(resolved) == 1


def test_v2_rejects_unknown_no_op_evidence_candidate_id() -> None:
    text = "chen repeats an already accepted durable fact."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    draft = _v2_no_op_draft("这段伪造引用不在本章目录中")
    curator = ModelCurator(
        _FakeGateway(draft),
        evidence_generator=gen,
        enforce_support_gate=True,
    )
    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_INVALID_EVIDENCE",
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
    assert exc.value.json_pointers == ("/no_op_evidence_quotes/0",)
    assert "evidence quote unresolved" in exc.value.safe_feedback[0]


def test_v2_multi_quote_rejection_points_at_failing_quote_only() -> None:
    """A multi-quote operation must bind feedback and the JSON pointer to the
    actual failing quote/index, not to an unrelated quote with a similarity
    candidate."""

    text = "\u5b81\u5a46\u5a46\u9762\u65e0\u8868\u60c5\u8bf4\u9053\uff1a"
    text += "\u6211\u53ea\u80fd\u8fdb\u56fd\u6559\u5b66\u9662\u3002"
    text += "\u53e6\u4e00\u4e2a\u5b8c\u5168\u4e0d\u540c\u7684\u539f\u6587\u7247\u6bb5\u3002"
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    good = next(item for item in candidates if "\u56fd\u6559\u5b66\u9662" in item.text)
    fake_quote = (
        "\u5b8c\u5168\u4e0d\u5728\u76ee\u5f55\u4e2d\u7684\u4f2a\u9020\u5f15\u7528\u5185\u5bb9"
    )
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
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
                evidence_quotes=(good.text, fake_quote),
            ),
        ),
    )
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=True)
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_INVALID_EVIDENCE"
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.multi-quote-fail")
            )
        )
    assert exc.value.json_pointers == ("/operations/0/evidence_quotes/1",)
    assert fake_quote in exc.value.safe_feedback[0]


def test_v2_no_resolvable_literal_falls_back_to_generic_guidance() -> None:
    """When no bounded catalog literal is resolver-valid, feedback must not
    name an unverified nearest literal and must not claim copying works."""

    text = (
        "\u540c\u4e00\u4e2a\u8db3\u591f\u957f\u7684\u539f\u6587\u7247\u6bb5\u51fa\u73b0\u4e24\u6b21\u3002"
        "\u540c\u4e00\u4e2a\u8db3\u591f\u957f\u7684\u539f\u6587\u7247\u6bb5\u51fa\u73b0\u4e24\u6b21\u3002"
    )
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)

    def _resolves_uniquely(candidate_text: str) -> bool:
        try:
            return len(gen.resolve_evidence_quotes((candidate_text,), candidates)) == 1
        except ValueError:
            return False

    # Ensure every candidate text is ambiguous so no copyable literal exists.
    assert all(not _resolves_uniquely(candidate.text) for candidate in candidates)
    draft = _v2_state_draft("\u540c\u4e00\u4e2a\u8db3\u591f\u957f\u7684\u539f\u6587\u7247\u6bb5")
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=True)
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_INVALID_EVIDENCE"
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.no-copyable-literal")
            )
        )
    feedback = exc.value.safe_feedback[0]
    assert "copy this exact catalog text verbatim" not in feedback
    assert "longer verbatim/full-sentence fragment" in feedback


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
    draft = _v2_state_draft("这段伪造引用不存在于任何本章候选的原文中")
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=True)
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_INVALID_EVIDENCE"
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(
                root, 21, _COMMIT, _world(), _request("req.v2.unknown-candidate")
            )
        )
    assert exc.value.violation_rule == "evidence_quote_must_match_chapter_catalog"
    assert exc.value.information_boundary is False
    assert exc.value.operation_indexes == (0,)
    assert "evidence quote unresolved" in exc.value.safe_feedback[0]
    assert exc.value.json_pointers == ("/operations/0/evidence_quotes/0",)


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
    draft = _v2_state_draft(unrelated)
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
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.ENTITY,
                target_id=StableId("entity.chen"),
                record=CuratorEntityRecord(
                    entity_type="character",
                    internal_label="chen",
                ),
                evidence_quotes=(good.text,),
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
    parent_changes = _v2_parent_changes(root, gen, good)

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
    parent_changes = _v2_parent_changes(root, gen, good)

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
    parent_changes = _v2_parent_changes(root, gen, good)

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
    parent_changes = _v2_parent_changes(root, gen, good)

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
    parent_changes = _v2_parent_changes(root, gen, good)

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
    parent_changes = _v2_parent_changes(root, gen, good)

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


def test_v2_filters_contradicts_evidence_to_audited_no_op() -> None:
    """A CONTRADICTS operation is filtered without mutating Canonical World."""
    text = "chen does not have cultivation-attitude anymore."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    bad = candidates[0]
    draft = _v2_state_draft(bad)
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=True)
    changes, _call, filtered = asyncio.run(
        curator.extract_reported_v2(root, 21, _COMMIT, _world(), _request("req.v2.contradicts"))
    )
    assert changes.operations == ()
    assert filtered.operations == ()
    assert curator.last_no_op_verification == (
        True,
        "ALL_OPERATIONS_REJECTED_BY_SUPPORT_GATE",
    )
    assert len(curator.last_operation_filter_receipts) == 1
    assert (
        curator.last_operation_filter_receipts[0].support_disposition
        is EvidenceSupportDisposition.CONTRADICTS
    )


def test_v2_partial_evidence_passes_when_verifier_supports() -> None:
    """A PARTIAL candidate is admitted when the semantic verifier returns SUPPORTS."""
    text = "the weather is sunny and calm today."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    weak = candidates[0]
    draft = _v2_state_draft(weak)

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


def test_v2_partial_evidence_verifier_rejection_becomes_audited_no_op() -> None:
    """A verifier-rejected operation is filtered without Canonical mutation."""
    text = "the weather is sunny and calm today."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    weak = candidates[0]
    draft = _v2_state_draft(weak)

    def verifier(record, candidate) -> tuple[EvidenceSupportDisposition, str]:
        return (EvidenceSupportDisposition.CONTRADICTS, "VERIFIER_CONTRADICTS")

    curator = ModelCurator(
        _FakeGateway(draft),
        evidence_generator=gen,
        enforce_support_gate=True,
        semantic_verifier=verifier,
    )
    changes, _call, filtered = asyncio.run(
        curator.extract_reported_v2(root, 21, _COMMIT, _world(), _request("req.v2.verifier-no"))
    )
    assert changes.operations == ()
    assert filtered.operations == ()
    assert curator.last_no_op_verification == (
        True,
        "ALL_OPERATIONS_REJECTED_BY_SUPPORT_GATE",
    )
    assert len(curator.last_operation_filter_receipts) == 1
    receipt = curator.last_operation_filter_receipts[0]
    assert receipt.support_disposition is EvidenceSupportDisposition.CONTRADICTS
    assert receipt.support_reason_code == "VERIFIER_CONTRADICTS"


def test_v2_partial_evidence_fails_closed_without_verifier() -> None:
    """A PARTIAL candidate with no verifier fails closed (UNRESOLVED)."""
    text = "the weather is sunny and calm today."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    weak = candidates[0]
    draft = _v2_state_draft(weak)
    curator = ModelCurator(_FakeGateway(draft), evidence_generator=gen, enforce_support_gate=True)
    with pytest.raises(
        CuratorProposalSemanticRejected, match="CURATOR_PROPOSAL_EVIDENCE_UNRESOLVED"
    ) as exc:
        asyncio.run(
            curator.extract_reported_v2(root, 21, _COMMIT, _world(), _request("req.v2.no-verifier"))
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
    draft = _v2_state_draft(weak)

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


def test_v2_model_semantic_verifier_inherits_parent_transport_ceiling() -> None:
    # Round-11 repair: the partial-batch semantic verifier must inherit the parent
    # request's 900s provider ceiling instead of being capped at 300s (the cap made
    # chapter-48 Graph verification time out repeatedly under endpoint concurrency 4).
    text = "陈长生已经开始阅读道藏并正式开始学习修行"
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    draft = _v2_state_draft(candidate)
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

    request = _request("req.v2.model-verifier").model_copy(update={"timeout_seconds": 900})
    changes, _call, _out = asyncio.run(
        curator.extract_reported_v2(root, 21, _COMMIT, _world(), request)
    )

    assert changes.operations
    assert len(gateway.requests) == 2
    verifier = gateway.requests[1]
    assert verifier.request_id.root.endswith(".semantic-verifier")
    assert verifier.timeout_seconds == 900  # inherits the parent ceiling, no 300s cap
    assert verifier.enable_thinking is False  # verifier stays deterministic/non-thinking
    assert verifier.thinking_token_budget is None
    # Round-12/18 repairs: the verifier carries its own repetition penalty
    # against runaway generation; the parent ordinary-Curator request carries
    # the round-18 request-local penalty too (both 1.10, per-owner policies).
    assert verifier.repetition_penalty == 1.10
    assert gateway.requests[0].repetition_penalty == 1.10


def test_v2_model_semantic_verifier_batches_partial_evidence_once() -> None:
    text = "陈长生已经开始阅读道藏并正式开始学习修行"
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    draft = _v2_state_draft(candidate)
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

    request = _request("req.v2.model-verifier").model_copy(update={"timeout_seconds": 120})
    changes, _call, _out = asyncio.run(
        curator.extract_reported_v2(root, 21, _COMMIT, _world(), request)
    )

    assert changes.operations
    assert len(gateway.requests) == 2
    assert gateway.requests[1].request_id.root.endswith(".semantic-verifier")
    assert gateway.requests[1].timeout_seconds == 120
    assert "半个时辰 is one hour and never half_hour" in gateway.requests[1].prompt
    assert "supports only a record that explicitly encodes belief" in (gateway.requests[1].prompt)
    assert "Evaluate all excerpts for one operation collectively" in (gateway.requests[1].prompt)
    assert "accepted records must be durable World state" in gateway.requests[1].prompt
    assert "Emit only durable world-state deltas" in gateway.requests[0].prompt
    assert "one-scene encounters" in gateway.requests[0].prompt
    assert "general rules, hypotheticals, maxima" in gateway.requests[0].prompt
    prompt = gateway.requests[0].prompt
    assert prompt.index("</CURATOR_INPUT>") < prompt.index("<CURATOR_OUTPUT_CONTRACT")
    assert "MUST be copied verbatim" in prompt
    assert "Every operation MUST carry a non-empty evidence_quotes array" in prompt
    assert "For record_kind=relation" not in prompt
    assert "record_kind=state" in prompt
    assert "composite method or process MUST cite" in prompt
    assert "half_shichen is not half_hour" in prompt
    assert "encoded as a belief/estimate/claim" in prompt
    assert "every semantic component" in prompt
    assert "no_durable_delta_reason MUST be null" in prompt
    assert "no_op_evidence_quotes MUST be an empty array" in prompt
    assert curator.last_prompt_fingerprint is not None


def test_v2_model_semantic_verifier_schema_bounds_generation() -> None:
    schema = EvidenceSemanticVerificationDraft.model_json_schema()

    assert schema["properties"]["decisions"]["maxItems"] == 4
    item_schema = schema["$defs"]["EvidenceSemanticVerificationItem"]
    assert item_schema["properties"]["reason_code"]["minLength"] == 1
    assert item_schema["properties"]["reason_code"]["maxLength"] == 160
    batch_schema = ModelCurator._semantic_verification_batch_type(2).model_json_schema()
    assert batch_schema["properties"]["decisions"]["minItems"] == 2
    assert batch_schema["properties"]["decisions"]["maxItems"] == 2


@pytest.mark.parametrize("decision_count", (0, 5))
def test_v2_model_semantic_verifier_rejects_invalid_batch_size(
    decision_count: int,
) -> None:
    with pytest.raises(ValueError, match="one to four decisions"):
        ModelCurator._semantic_verification_batch_type(decision_count)


def test_v2_model_semantic_verifier_evaluates_combined_operation_evidence() -> None:
    root = _root_with("陈长生先反复阅读道藏。随后摘录要点整理笔记。")
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert len(candidates) >= 2
    base = _v2_state_draft(candidates[0])
    draft = base.model_copy(
        update={
            "operations": (
                base.operations[0].model_copy(
                    update={"evidence_quotes": (candidates[0].text, candidates[1].text)}
                ),
            )
        }
    )
    verification = EvidenceSemanticVerificationDraft(
        decisions=(
            EvidenceSemanticVerificationItem(
                operation_index=0,
                candidate_ids=(candidates[0].candidate_id, candidates[1].candidate_id),
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
    assert all(
        candidate_id.root in verifier_prompt
        for candidate_id in (candidates[0].candidate_id, candidates[1].candidate_id)
    )
    assert '"evidence":[' in verifier_prompt


def test_v2_model_semantic_verifier_drops_only_rejected_operations() -> None:
    root = _root_with(
        "陈长生的读书方法十分特殊。他反复阅读道藏并整理出重要笔记。当天的天气晴朗无云。"
    )
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert len(candidates) >= 2
    supported = candidates[0]
    rejected = candidates[-1]
    first = _v2_state_draft(supported).operations[0]
    draft = CuratorV2EvidenceDraft(
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
                    "evidence_quotes": (rejected.text,),
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


def test_v2_model_semantic_verifier_drops_only_missing_decisions() -> None:
    root = _root_with(
        "陈长生的读书方法十分特殊。他反复阅读道藏并整理出重要笔记。当天的天气晴朗无云。"
    )
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert len(candidates) >= 2
    supported = candidates[0]
    unresolved = candidates[-1]
    first = _v2_state_draft(supported).operations[0]
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            first,
            first.model_copy(
                update={
                    "target_id": StableId("state.unresolved"),
                    "record": CuratorStateRecord(
                        subject_id=StableId("entity.chen"),
                        predicate="has_unresolved_fact",
                        value="not_in_evidence",
                        valid_time=CuratorStoryTime(worldline="current"),
                        truth_class=TruthClass.ASSERTION,
                    ),
                    "evidence_quotes": (unresolved.text,),
                }
            ),
        ),
    )
    gateway = _ModelVerifierGateway(
        draft,
        EvidenceSemanticVerificationDraft(
            decisions=(
                EvidenceSemanticVerificationItem(
                    operation_index=0,
                    candidate_ids=(supported.candidate_id,),
                    disposition=EvidenceSupportDisposition.SUPPORTS,
                    reason_code="DIRECT_SUPPORT",
                ),
            )
        ),
    )
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
            _request("req.v2.model-verifier-missing-decision"),
        )
    )

    assert len(changes.operations) == 1
    assert len(filtered.operations) == 1
    assert filtered.operations[0].target_id == first.target_id
    assert len(curator.last_operation_filter_receipts) == 1
    receipt = curator.last_operation_filter_receipts[0]
    assert receipt.proposed_target_id == StableId("state.unresolved")
    assert receipt.support_disposition is EvidenceSupportDisposition.PARTIAL
    assert receipt.support_reason_code is not None
    assert receipt.support_reason_code.endswith("_UNRESOLVED")


def test_v2_model_semantic_verifier_binds_decisions_by_operation_index() -> None:
    root = _root_with(
        "陈长生的读书方法十分特殊。他反复阅读道藏并整理出重要笔记。当天的天气晴朗无云。"
    )
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert len(candidates) >= 2
    mismatched = candidates[0]
    supported = candidates[-1]
    first = _v2_state_draft(mismatched).operations[0]
    second = first.model_copy(
        update={
            "target_id": StableId("state.supported"),
            "record": CuratorStateRecord(
                subject_id=StableId("entity.chen"),
                predicate="has_supported_fact",
                value="supported_value",
                valid_time=CuratorStoryTime(worldline="current"),
                truth_class=TruthClass.ASSERTION,
            ),
            "evidence_quotes": (supported.text,),
        }
    )
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(first, second),
    )
    gateway = _ModelVerifierGateway(
        draft,
        EvidenceSemanticVerificationDraft(
            decisions=(
                EvidenceSemanticVerificationItem(
                    operation_index=0,
                    candidate_ids=(
                        mismatched.candidate_id,
                        supported.candidate_id,
                    ),
                    disposition=EvidenceSupportDisposition.SUPPORTS,
                    reason_code="USED_UNSELECTED_EVIDENCE",
                ),
                EvidenceSemanticVerificationItem(
                    operation_index=1,
                    candidate_ids=(supported.candidate_id,),
                    disposition=EvidenceSupportDisposition.SUPPORTS,
                    reason_code="DIRECT_SUPPORT",
                ),
            )
        ),
    )
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
            _request("req.v2.model-verifier-candidate-mismatch"),
        )
    )

    assert len(changes.operations) == 2
    assert len(filtered.operations) == 2
    assert {item.target_id for item in filtered.operations} == {
        first.target_id,
        second.target_id,
    }
    assert not curator.last_operation_filter_receipts


def test_v2_model_semantic_verifier_ignores_unknown_echoed_candidate_id() -> None:
    root = _root_with("陈长生的读书方法十分特殊。")
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    draft = _v2_state_draft(candidate)
    gateway = _ModelVerifierGateway(
        draft,
        EvidenceSemanticVerificationDraft(
            decisions=(
                EvidenceSemanticVerificationItem(
                    operation_index=0,
                    candidate_ids=(
                        candidate.candidate_id,
                        StableId("decision_type_schema_description"),
                    ),
                    disposition=EvidenceSupportDisposition.SUPPORTS,
                    reason_code="DIRECT_SUPPORT",
                ),
            )
        ),
    )
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
            _request("req.v2.model-verifier-unknown-echo"),
        )
    )

    assert len(changes.operations) == 1
    assert len(filtered.operations) == 1
    assert not curator.last_operation_filter_receipts


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
    candidate = next(item for item in gen.generate(root, 21) if "confidence" in item.text)
    existing_record = CuratorStateRecord(
        subject_id=StableId("entity.chen"),
        predicate="has_cultivation_status",
        value="commenced_study",
        valid_time=CuratorStoryTime(worldline="current", start_ordinal=20),
        truth_class=TruthClass.ASSERTION,
    )
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.duplicate-under-new-id"),
                record=existing_record,
                evidence_quotes=(candidate.text,),
            ),
            _v2_state_draft(candidate).operations[0],
            CuratorV2OperationDraft(
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
                evidence_quotes=(candidate.text,),
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
                valid_time=StoryTime.model_validate(existing_record.valid_time.model_dump()),
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


def test_v2_filters_state_target_identity_mismatch_before_candidate() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = next(item for item in gen.generate(root, 21) if "confidence" in item.text)
    first = _v2_state_draft(candidate).operations[0]
    mismatched = CuratorV2OperationDraft(
        operation=ChangeOperationType.REPLACE,
        record_kind=WorldRecordKind.STATE,
        target_id=StableId("state.reading-method"),
        record=CuratorStateRecord(
            subject_id=StableId("entity.chen"),
            predicate="has_method",
            value="comparative_study",
            valid_time=CuratorStoryTime(worldline="current", start_ordinal=21),
            truth_class=TruthClass.ASSERTION,
        ),
        evidence_quotes=(candidate.text,),
    )
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(first, mismatched),
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
                state_id=StableId("state.reading-method"),
                subject_id=StableId("entity.chen"),
                predicate="uses_reading_method",
                value="recitation",
                valid_time=StoryTime(worldline="current", start_ordinal=20),
                truth_class=TruthClass.ASSERTION,
            ),
        ),
    )
    curator = ModelCurator(
        _FakeGateway(draft),
        evidence_generator=gen,
        enforce_support_gate=False,
    )

    # Round-21 repair: an identity-mismatched operation is a typed rejection
    # of the COMPLETE proposal, never a silent filter that could commit a
    # partial delta or feed the empty-delta gate an emptied draft.
    with pytest.raises(CuratorProposalSemanticRejected) as raised:
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                _COMMIT,
                world,
                _request("req.v2.target-identity-mismatch"),
            )
        )
    error = raised.value
    assert error.reason_code == "CURATOR_PROPOSAL_TARGET_IDENTITY_MISMATCH"
    assert error.operation_indexes == (1,)
    assert "/operations/1/target_id" in error.json_pointers
    assert "/operations/1/record/subject_id" in error.json_pointers
    assert "/operations/1/record/predicate" in error.json_pointers
    feedback = error.safe_feedback[0]
    assert "state.reading-method" in feedback
    assert "uses_reading_method" in feedback
    assert "new non-colliding state id" in feedback
    assert error.violation_rule == "existing_target_identity_immutable"


def test_v2_rejects_dangling_entity_reference_with_field_level_feedback() -> None:
    text = "xu-shi-ji now appreciates chen changsheng."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = next(item for item in gen.generate(root, 21) if "appreciates" in item.text)
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.xu-shi-ji-attitude"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.xu-shi-ji"),
                    predicate="has_attitude_towards",
                    value="appreciation_for_chen_changsheng",
                    valid_time=CuratorStoryTime(
                        worldline="current",
                        start_ordinal=21,
                    ),
                    truth_class=TruthClass.ASSERTION,
                ),
                evidence_quotes=(candidate.text,),
            ),
        ),
    )
    curator = ModelCurator(
        _FakeGateway(draft),
        evidence_generator=gen,
        enforce_support_gate=True,
    )

    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_DANGLING_ENTITY_REFERENCE",
    ) as caught:
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                _COMMIT,
                _world(),
                _request("req.v2.dangling-entity"),
            )
        )

    error = caught.value
    assert error.operation_indexes == (0,)
    assert error.json_pointers == ("/operations/0/record/subject_id",)
    assert error.violation_rule == ("referenced_entity_must_exist_or_be_created_in_same_proposal")
    assert "entity.xu-shi-ji" in error.safe_feedback[0]
    assert error.safe_feedback[1].startswith("REQUIRED_REPAIR:")
    assert "Listing IDs in unresolved does not repair references" in error.safe_feedback[1]
    assert error.safe_feedback[2] == "Known WORLD entity_ids: entity.chen"


def test_v2_normalizes_unique_short_entity_reference_to_canon_id() -> None:
    text = "chen now has a durable preference."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    canon_id = StableId("entity.bootstrap.chen")
    world = _world().model_copy(
        update={"entities": (_world().entities[0].model_copy(update={"entity_id": canon_id}),)}
    )
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.chen.preference"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.chen"),
                    predicate="preference",
                    value="durable",
                    valid_time=CuratorStoryTime(worldline="main", start_ordinal=21),
                    truth_class=TruthClass.ASSERTION,
                ),
                evidence_quotes=(candidate.text,),
            ),
        ),
    )

    changes, _call, normalized = asyncio.run(
        ModelCurator(
            _FakeGateway(draft),
            evidence_generator=gen,
            enforce_support_gate=False,
        ).extract_reported_v2(
            root,
            21,
            _COMMIT,
            world,
            _request("req.v2.unique-entity-alias"),
        )
    )

    assert isinstance(normalized.operations[0].record, CuratorStateRecord)
    assert normalized.operations[0].record.subject_id == canon_id
    payload = changes.operations[0].payload
    assert isinstance(payload, dict)
    record = payload["record"]
    assert isinstance(record, dict)
    assert record["subject_id"] == canon_id.root


def test_v2_does_not_guess_ambiguous_short_entity_reference() -> None:
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.chen.preference"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.chen"),
                    predicate="preference",
                    value="durable",
                    valid_time=CuratorStoryTime(worldline="main", start_ordinal=21),
                    truth_class=TruthClass.ASSERTION,
                ),
                evidence_quotes=("evidence-candidate.placeholder",),
            ),
        ),
    )
    world = _world().model_copy(
        update={
            "entities": (
                _world()
                .entities[0]
                .model_copy(update={"entity_id": StableId("entity.alpha.chen")}),
                _world().entities[0].model_copy(update={"entity_id": StableId("entity.beta.chen")}),
            )
        }
    )

    normalized = ModelCurator._normalize_entity_reference_aliases(draft, world)

    assert isinstance(normalized.operations[0].record, CuratorStateRecord)
    assert normalized.operations[0].record.subject_id == StableId("entity.chen")


def test_v2_allows_reference_to_entity_created_in_same_proposal() -> None:
    text = "xu-shi-ji now appreciates chen changsheng."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = next(item for item in gen.generate(root, 21) if "appreciates" in item.text)
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.ENTITY,
                target_id=StableId("entity.xu-shi-ji"),
                record=CuratorEntityRecord(
                    entity_type="character",
                    internal_label="徐世绩",
                ),
                evidence_quotes=(candidate.text,),
            ),
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.xu-shi-ji-attitude"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.xu-shi-ji"),
                    predicate="has_attitude_towards",
                    value="appreciation_for_chen_changsheng",
                    valid_time=CuratorStoryTime(
                        worldline="current",
                        start_ordinal=21,
                    ),
                    truth_class=TruthClass.ASSERTION,
                ),
                evidence_quotes=(candidate.text,),
            ),
        ),
    )
    curator = ModelCurator(
        _FakeGateway(draft),
        evidence_generator=gen,
        enforce_support_gate=False,
    )

    changes, _call, accepted = asyncio.run(
        curator.extract_reported_v2(
            root,
            21,
            _COMMIT,
            _world(),
            _request("req.v2.create-then-reference"),
        )
    )

    assert len(changes.operations) == 2
    assert all(
        accepted.record_kind == draft_operation.record_kind
        and accepted.target_id == draft_operation.target_id
        and candidate.candidate_id in accepted.evidence_candidate_ids
        for accepted, draft_operation in zip(accepted.operations, draft.operations, strict=True)
        for _quote in draft_operation.evidence_quotes
    )


def test_v2_rejects_reference_when_support_gate_drops_entity_create() -> None:
    text = "xu-shi-ji now appreciates chen changsheng."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = next(item for item in gen.generate(root, 21) if "appreciates" in item.text)
    entity = CuratorV2OperationDraft(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.ENTITY,
        target_id=StableId("entity.xu-shi-ji"),
        record=CuratorEntityRecord(
            entity_type="character",
            internal_label="徐世绩",
        ),
        evidence_quotes=(candidate.text,),
    )
    state = CuratorV2OperationDraft(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.STATE,
        target_id=StableId("state.xu-shi-ji-attitude"),
        record=CuratorStateRecord(
            subject_id=StableId("entity.xu-shi-ji"),
            predicate="has_attitude_towards",
            value="appreciation_for_chen_changsheng",
            valid_time=CuratorStoryTime(worldline="current", start_ordinal=21),
            truth_class=TruthClass.ASSERTION,
        ),
        evidence_quotes=(candidate.text,),
    )
    draft = CuratorV2EvidenceDraft(chapter_index=21, operations=(entity, state))
    verification = EvidenceSemanticVerificationDraft(
        decisions=(
            EvidenceSemanticVerificationItem(
                operation_index=0,
                candidate_ids=(candidate.candidate_id,),
                disposition=EvidenceSupportDisposition.UNRELATED,
                reason_code="ENTITY_NAME_NOT_SUPPORTED",
            ),
            EvidenceSemanticVerificationItem(
                operation_index=1,
                candidate_ids=(candidate.candidate_id,),
                disposition=EvidenceSupportDisposition.SUPPORTS,
                reason_code="DIRECT_SUPPORT",
            ),
        )
    )
    curator = ModelCurator(
        cast(Any, _ModelVerifierGateway(draft, verification)),
        evidence_generator=gen,
        enforce_support_gate=True,
        enable_model_semantic_verifier=True,
    )

    with pytest.raises(
        CuratorProposalSemanticRejected,
        match="CURATOR_PROPOSAL_DANGLING_ENTITY_REFERENCE",
    ) as caught:
        asyncio.run(
            curator.extract_reported_v2(
                root,
                21,
                _COMMIT,
                _world(),
                _request("req.v2.post-filter-dangling-entity"),
            )
        )

    assert caught.value.operation_indexes == (0,)
    assert caught.value.json_pointers == ("/operations/0/record/subject_id",)
    assert curator.last_operation_filter_receipts[0].reason == "evidence_support_rejected"


def test_v2_normalizes_fully_duplicate_proposal_to_verified_no_op() -> None:
    text = "chen repeats an already accepted durable fact."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidate = gen.generate(root, 21)[0]
    existing_record = CuratorStateRecord(
        subject_id=StableId("entity.chen"),
        predicate="has_cultivation_status",
        value="commenced_study",
        valid_time=CuratorStoryTime(worldline="current", start_ordinal=22),
        truth_class=TruthClass.ASSERTION,
    )
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.duplicate-under-new-id"),
                record=existing_record,
                evidence_quotes=(candidate.text,),
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
                valid_time=StoryTime.model_validate(existing_record.valid_time.model_dump()),
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
    assert curator.last_operation_filter_receipts[0].reason == "existing_semantic_duplicate"


@pytest.mark.parametrize(
    "verification",
    [
        EvidenceSemanticVerificationDraft(decisions=()),
        EvidenceSemanticVerificationDraft(
            decisions=(
                EvidenceSemanticVerificationItem(
                    operation_index=99,
                    candidate_ids=(StableId("evidence-candidate.wrong"),),
                    disposition=EvidenceSupportDisposition.SUPPORTS,
                    reason_code="WRONG_OPERATION_INDEX",
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
                EvidenceSemanticVerificationItem(
                    operation_index=0,
                    candidate_ids=(StableId("evidence-candidate.duplicate"),),
                    disposition=EvidenceSupportDisposition.SUPPORTS,
                    reason_code="DUPLICATE_AGAIN",
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
    if (
        isinstance(verification, EvidenceSemanticVerificationDraft)
        and len(verification.decisions) >= 2
    ):
        verification = verification.model_copy(
            update={
                "decisions": tuple(
                    item.model_copy(update={"candidate_ids": (candidate.candidate_id,)})
                    for item in verification.decisions
                )
            }
        )
    gateway = _ModelVerifierGateway(
        _v2_state_draft(candidate),
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
    enforce = flags.evidence_support_gate is EvidenceSupportGateMode.ENFORCE_PRE_CANDIDATE
    assert enforce is True

    # PARTIAL fails closed (no verifier wired in production smoke).
    text = "the weather is sunny and calm today."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    assert candidates
    weak = candidates[0]
    draft = _v2_state_draft(weak)
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

    # CONTRADICTS is hard-filtered and cannot mutate Canonical World.
    bad_text = "chen does not have cultivation-attitude anymore."
    bad_root = _root_with(bad_text)
    bad_candidates = gen.generate(bad_root, 21)
    assert bad_candidates
    bad_draft = _v2_state_draft(bad_candidates[0])
    bad_curator = ModelCurator(
        _FakeGateway(bad_draft),
        evidence_generator=gen,
        enforce_support_gate=enforce,
    )
    bad_changes, _call, _filtered = asyncio.run(
        bad_curator.extract_reported_v2(
            bad_root, 21, _COMMIT, _world(), _request("req.v2.prod-contradicts")
        )
    )
    assert bad_changes.operations == ()
    assert bad_curator.last_no_op_verification == (
        True,
        "ALL_OPERATIONS_REJECTED_BY_SUPPORT_GATE",
    )


# --- Round-18: ordinary-Curator request penalty + narrow layout-equivalence binding ---

_CANONICAL_CH12 = (
    "”\n　　他纠正道\uff0c忽然又想起秋山君\uff0c如果那人参加今次的大朝试……\n　　"
    "“好吧\uff0c我的目标是大朝试第三。"
)
_VARIANT_NO_LEADING_MARK = (
    "他纠正道\uff0c忽然又想起秋山君\uff0c如果那人参加今次的大朝试……"
    "“好吧\uff0c我的目标是大朝试第三。"
)
_VARIANT_KEPT_LEADING_MARK = (
    "”他纠正道\uff0c忽然又想起秋山君\uff0c如果那人参加今次的大朝试……"
    "“好吧\uff0c我的目标是大朝试第三。"
)


def _layout_candidate(text: str, candidate_id: str, start: int = 0) -> EvidenceCandidate:
    return EvidenceCandidate(
        candidate_id=StableId(candidate_id),
        block_id=StableId("block.c21"),
        chapter_index=21,
        scene_index=0,
        text=text,
        start=start,
        end=start + len(text),
        content_hash=ArtifactId("sha256:" + "a" * 64),
    )


def test_v2_curator_request_carries_request_local_penalty() -> None:
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    generator = EvidenceCandidateGenerator()
    candidate = next(item for item in generator.generate(root, 21) if "confidence" in item.text)
    draft = _v2_state_draft(candidate)
    gateway = _FakeGateway(draft)
    request = _request("req.v2.curator-penalty")
    curator = ModelCurator(
        cast(Any, gateway), evidence_generator=generator, enforce_support_gate=False
    )
    changes, _call, _out = asyncio.run(
        curator.extract_reported_v2(root, 21, _COMMIT, _world(), request)
    )
    assert changes.operations
    # Round-18: the ordinary Curator request carries 1.10 against runaway
    # generation; the base request stays unchanged (thinking policy inherited).
    assert gateway.requests[0].repetition_penalty == 1.10
    assert gateway.requests[0].enable_thinking is request.enable_thinking
    assert gateway.requests[0].thinking_token_budget is request.thinking_token_budget
    assert request.repetition_penalty is None


def test_layout_equivalent_quote_binds_canonical_ch12_variants() -> None:
    canonical = _layout_candidate(_CANONICAL_CH12, "cand.ch12")
    chapter = _root_with(_CANONICAL_CH12).chapters[0]
    for variant in (_VARIANT_NO_LEADING_MARK, _VARIANT_KEPT_LEADING_MARK):
        bound = EvidenceCandidateGenerator.resolve_layout_equivalent_quote(
            variant, (canonical,), chapter
        )
        assert bound is not None
        assert bound.candidate_id == canonical.candidate_id
        # Canonical source text and physical span, never the model string.
        assert bound.text == _CANONICAL_CH12
        assert bound.start == 0
        assert bound.end == len(_CANONICAL_CH12)
        assert bound.content_hash == canonical.content_hash


def test_layout_equivalent_quote_fails_closed() -> None:
    chapter = _root_with(_CANONICAL_CH12).chapters[0]
    canonical = _layout_candidate(_CANONICAL_CH12, "cand.ch12")
    resolver = EvidenceCandidateGenerator.resolve_layout_equivalent_quote
    # Changed character.
    assert (
        resolver(
            "他纠正到\uff0c忽然又想起秋山君\uff0c如果那人参加今次的大朝试……“好吧\uff0c我的目标是大朝试第三。",
            (canonical,),
            chapter,
        )
        is None
    )
    # Changed internal punctuation.
    assert (
        resolver(
            "他纠正道\uff0c忽然又想起秋山君\uff1b如果那人参加今次的大朝试……“好吧\uff0c我的目标是大朝试第三。",
            (canonical,),
            chapter,
        )
        is None
    )
    # Ordinary word-space change.
    assert (
        resolver(
            (
                "他 纠正道\uff0c忽然又想起秋山君\uff0c如果那人参加今次的大朝试……"
                "“好吧\uff0c我的目标是大朝试第三。"
            ),
            (canonical,),
            chapter,
        )
        is None
    )
    # Duplicate layout-equivalent candidates (both real physical spans of the
    # chapter text) -> ambiguous.
    doubled_text = _CANONICAL_CH12 + "间隔" + _CANONICAL_CH12
    doubled_chapter = _root_with(doubled_text).chapters[0]
    first = _layout_candidate(_CANONICAL_CH12, "cand.ch12.first", start=0)
    second = _layout_candidate(
        _CANONICAL_CH12,
        "cand.ch12.second",
        start=len(_CANONICAL_CH12) + len("间隔"),
    )
    assert resolver(_VARIANT_NO_LEADING_MARK, (first, second), doubled_chapter) is None
    # Too-short quote stays unresolved.
    assert resolver("第三。", (canonical,), chapter) is None


def test_v2_ch12_style_draft_binds_canonical_span_through_binding() -> None:
    # The chapter-12 quarantine: the model's quote differs from the catalog
    # only by the dropped/restored leading closing mark and the CR/LF+indent
    # layout. The ordinary-Curator binding must produce the canonical ref.
    canonical = _layout_candidate(_CANONICAL_CH12, "cand.ch12")

    class _FixedCatalogGenerator(EvidenceCandidateGenerator):
        def generate(self, text_root, chapter_index):
            return (canonical,)

    root = _root_with(_CANONICAL_CH12)
    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.OBLIGATION,
                target_id=StableId("entity.tang36"),
                record=CuratorObligationRecord(
                    kind="objective",
                    description="参加今次的大朝试并取得第三名",
                    status="open",
                ),
                evidence_quotes=(_VARIANT_KEPT_LEADING_MARK,),
            ),
        ),
    )
    gateway = _FakeGateway(draft)
    curator = ModelCurator(
        cast(Any, gateway),
        evidence_generator=_FixedCatalogGenerator(),
        enforce_support_gate=True,
        semantic_verifier=lambda _record, _candidate: (
            EvidenceSupportDisposition.SUPPORTS,
            "test_trusted_semantic_verifier",
        ),
    )
    changes, _call, _out = asyncio.run(
        curator.extract_reported_v2(root, 21, _COMMIT, _world(), _request("req.v2.ch12"))
    )
    assert changes.operations
    # The canonical evidence ref is produced by the layout-equivalence binding.
    assert _out.operations[0].evidence_candidate_ids == (canonical.candidate_id,)
    assert gateway.requests[0].repetition_penalty == 1.10


# --- Round-21: immutable state target identity (v7 chapter-22 typed stop) ---


def _ch21_world_with_cultivation_start() -> WorldRootDocument:
    """Frozen chapter-21 Canonical World fixture: the state id the v7 ch22
    drafts reused with a different predicate (review-21 evidence fixture)."""
    return WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "a" * 64),
        schema_version=SchemaVersion("0.1.0"),
        source_commit=_COMMIT,
        entities=(
            Entity(
                entity_id=StableId("entity.bootstrap.chen-changsheng"),
                entity_type="character",
                internal_label="陈长生",
            ),
        ),
        states=(
            StateRecord(
                state_id=StableId("state.chen-changsheng.cultivation-start"),
                subject_id=StableId("entity.bootstrap.chen-changsheng"),
                predicate="cultivation_start_status",
                value="completed_xisui_lun_memorization_and_note_taking",
                valid_time=StoryTime(worldline="main", start_ordinal=1),
                truth_class=TruthClass.ASSERTION,
            ),
        ),
    )


def _ch22_identity_mismatch_draft(value: str) -> CuratorV2EvidenceDraft:
    """The exact v7 chapter-22 ordinary-Curator draft shape: it reuses
    state.chen-changsheng.cultivation-start for predicate cultivation_realm."""
    return CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.REPLACE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.chen-changsheng.cultivation-start"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.bootstrap.chen-changsheng"),
                    predicate="cultivation_realm",
                    value=value,
                    valid_time=CuratorStoryTime(worldline="main", start_ordinal=21),
                    truth_class=TruthClass.ASSERTION,
                ),
                evidence_quotes=("一天一夜时间\uff0c他凝结神识成功。",),
            ),
        ),
        coverage=0.8,
    )


def _ch22_catalog(root: TextRootDocument) -> tuple[EvidenceCandidate, ...]:
    return tuple(EvidenceCandidateGenerator().generate(root, 21))


def test_v2_state_target_identity_mismatch_rejects_before_noop_branch() -> None:
    # Both exact v7 ch22 raw draft shapes (values condensed_divine_spirit and
    # 凝神之境) must raise the typed target-identity rejection BEFORE the
    # empty-delta gate, with operation index zero, exact field pointers, and
    # actionable feedback naming the immutable existing identity.
    world = _ch21_world_with_cultivation_start()
    root = _root_with("一天一夜时间\uff0c他凝结神识成功。")
    candidates = _ch22_catalog(root)

    class _FixedCatalogGenerator(EvidenceCandidateGenerator):
        def generate(self, text_root, chapter_index):
            return candidates

    for value in ("condensed_divine_spirit", "凝神之境"):
        gateway = _FakeGateway(_ch22_identity_mismatch_draft(value))
        curator = ModelCurator(
            cast(Any, gateway),
            evidence_generator=_FixedCatalogGenerator(),
            enforce_support_gate=True,
            semantic_verifier=lambda _record, _candidate: (
                EvidenceSupportDisposition.SUPPORTS,
                "test_trusted_semantic_verifier",
            ),
        )
        with pytest.raises(CuratorProposalSemanticRejected) as raised:
            asyncio.run(
                curator.extract_reported_v2(
                    root, 21, _COMMIT, world, _request(f"req.v2.ch22.{len(value)}")
                )
            )
        error = raised.value
        assert error.reason_code == "CURATOR_PROPOSAL_TARGET_IDENTITY_MISMATCH"
        assert error.operation_indexes == (0,)
        assert "/operations/0/target_id" in error.json_pointers
        assert "/operations/0/record/subject_id" in error.json_pointers
        assert "/operations/0/record/predicate" in error.json_pointers
        feedback = error.safe_feedback[0]
        assert "state.chen-changsheng.cultivation-start" in feedback
        assert "cultivation_start_status" in feedback
        assert "new non-colliding state id" in feedback
        assert error.violation_rule == "existing_target_identity_immutable"


def test_v2_corrected_fresh_state_id_proceeds_through_binding_and_verifier() -> None:
    # A corrected draft that uses a NEW state id for cultivation_realm stays
    # non-empty and proceeds through exact evidence binding and the support
    # verifier; the output contract names the immutable-target rule.
    world = _ch21_world_with_cultivation_start()
    root = _root_with("一天一夜时间\uff0c他凝结神识成功。")
    candidates = _ch22_catalog(root)

    class _FixedCatalogGenerator(EvidenceCandidateGenerator):
        def generate(self, text_root, chapter_index):
            return candidates

    draft = CuratorV2EvidenceDraft(
        chapter_index=21,
        operations=(
            CuratorV2OperationDraft(
                operation=ChangeOperationType.CREATE,
                record_kind=WorldRecordKind.STATE,
                target_id=StableId("state.chen-changsheng.cultivation-realm"),
                record=CuratorStateRecord(
                    subject_id=StableId("entity.bootstrap.chen-changsheng"),
                    predicate="cultivation_realm",
                    value="condensed_divine_spirit",
                    valid_time=CuratorStoryTime(worldline="main", start_ordinal=21),
                    truth_class=TruthClass.ASSERTION,
                ),
                evidence_quotes=("一天一夜时间\uff0c他凝结神识成功。",),
            ),
        ),
    )
    gateway = _FakeGateway(draft)
    curator = ModelCurator(
        cast(Any, gateway),
        evidence_generator=_FixedCatalogGenerator(),
        enforce_support_gate=True,
        semantic_verifier=lambda _record, _candidate: (
            EvidenceSupportDisposition.SUPPORTS,
            "test_trusted_semantic_verifier",
        ),
    )
    changes, _call, out = asyncio.run(
        curator.extract_reported_v2(root, 21, _COMMIT, world, _request("req.v2.ch22.corrected"))
    )
    assert len(changes.operations) == 1
    assert changes.operations[0].target_id.root == "state.chen-changsheng.cultivation-realm"
    assert out.operations[0].evidence_candidate_ids == (candidates[0].candidate_id,)
    assert "A state target id is immutable" in gateway.requests[0].prompt


def test_evidence_repair_v2_array_contract_with_real_gateway() -> None:
    # Round-23 regression: the evidence-repair corridor must pass a REAL
    # Pydantic contract to the gateway. The formal v8 run crashed at chapter
    # 28 with `AttributeError: type object 'list' has no attribute
    # 'model_json_schema'` because the call passed `list[EvidenceRepairDraft]`;
    # a duck-typed fake gateway never computed the schema, so the defect was
    # invisible to the deterministic suites. A real ModelGateway plus the
    # RootModel array contract must complete end-to-end.
    text = "chen holds extreme_confidence firmly! cultivation-attitude is strong."
    root = _root_with(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 21)
    good = next(item for item in candidates if "confidence" in item.text)
    other = next(item for item in candidates if item.candidate_id != good.candidate_id)
    parent_changes = _v2_parent_changes(root, gen, good)

    repair_json = canonical_json_bytes(
        [
            {
                "operation_index": 0,
                "replacement_candidate_ids": [other.candidate_id.root],
                "action": "replace_evidence",
            }
        ]
    ).decode("utf-8")
    endpoint = FakeModelEndpoint(repair_json)
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="repair-test",
                model_name="fake",
                adapter=endpoint,
            ),
        )
    )
    curator = ModelCurator(gateway, evidence_generator=gen, enforce_support_gate=True)
    changes, _call, drafts = asyncio.run(
        curator.evidence_repair_v2(
            root, 21, _COMMIT, parent_changes, _request("req.repair.real-gateway")
        )
    )

    assert len(drafts) == 1
    assert drafts[0].operation_index == 0
    assert drafts[0].action is EvidenceRepairAction.REPLACE_EVIDENCE
    assert drafts[0].replacement_candidate_ids == (other.candidate_id,)
    assert len(changes.operations) == 1
    new_evidence = changes.operations[0].evidence_refs[0]
    assert new_evidence.span is not None
    assert new_evidence.span.start == other.start
    assert new_evidence.span.end == other.end
    # The strict gateway carried the RootModel JSON schema on the request.
    assert endpoint.requests[0].response_schema is not None
