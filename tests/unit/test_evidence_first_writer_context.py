"""Evidence-first Writer Context: L0 exact slices, v2 package/ledger, gaps."""

from __future__ import annotations

import json
from typing import Any

import pytest

from novel_agent.domain.benchmark import ChapterDocument, SceneDocument, TextRootDocument
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextBlock, TextSpanRef
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    ContextAssemblyStatus,
    EvidenceFirstPackageManifest,
    EvidenceGapKind,
    EvidenceSliceKind,
    EvidenceSliceSourceRole,
    WriterContextEvidenceItem,
    WriterContextSection,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes, quote_hash
from novel_agent.services.evidence_first_writer_context_assembler import (
    EvidenceFirstWriterContextAssembler,
    NeedEvidenceSelection,
    SliceSelectionTrace,
)
from novel_agent.services.evidence_slice_resolver import (
    DEFAULT_PARAGRAPH_CHAR_LIMIT,
    EvidenceSliceResolutionError,
    EvidenceSliceResolver,
)
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.evidence-first.test")


def _block(text: str, *, chapter: int = 5) -> TextBlock:
    return TextBlock(
        block_id=StableId(f"block.test.{chapter}.0"),
        chapter_id=StableId(f"chapter.test.{chapter}"),
        scene_id=StableId(f"scene.test.{chapter}.0"),
        narrative_index=chapter,
        text=text,
    )


def _evidence(block: TextBlock, start: int, end: int) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=StableId(f"evidence.test.{start}"),
        root_hash=ArtifactId("sha256:" + "b" * 64),
        object_hash=sha256_id(block.text.encode("utf-8")),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=TextSpanRef(block_id=block.block_id, start=start, end=end),
        quote_hash=quote_hash(block.text[start:end]),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=COMMIT,
    )


def _text_root(blocks: tuple[TextBlock, ...]) -> TextRootDocument:
    scene_id = blocks[0].scene_id
    assert scene_id is not None
    scene = SceneDocument(
        scene_id=scene_id,
        scene_index=0,
        blocks=blocks,
    )
    chapter = ChapterDocument(
        chapter_id=blocks[0].chapter_id,
        chapter_index=5,
        title="第五章",
        scenes=(scene,),
    )
    return TextRootDocument(
        root_hash=ArtifactId("sha256:" + "c" * 64),
        schema_version="1.0.0",
        chapters=(chapter,),
    )


def _task() -> BenchmarkTaskContract:
    return build_safe_task_contract(
        case_id=StableId("case.evidence-first"),
        checkpoint_chapter=5,
        target_range=(6, 8),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="test",
    )


def _need(
    need_id: str = "need.test.1",
    *,
    query: str = "陈长生当前的伤势状态是什么?",
    mandatory: bool = True,
    section: WriterContextSection = WriterContextSection.CURRENT_WORLD_STATE,
) -> Stage1MemoryNeed:
    return Stage1MemoryNeed(
        need_id=StableId(need_id),
        run_id=RunId(f"run.{need_id}"),
        task_id=TaskId("task.test"),
        base_commit=COMMIT,
        horizon_target=(6, 8),
        need_type="current_state",
        query_intent=Stage1QueryIntent.CURRENT_STATE,
        query_text=query,
        semantic_question=query,
        why_needed="writer needs the current state",
        planner_artifact_ref=ArtifactId("sha256:" + "f" * 64),
        planned_draft_id="draft.test",
        validated_need_set_hash=ArtifactId("sha256:" + "f" * 64),
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY if mandatory else RequirementLevel.OPTIONAL,
        preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
        allowed_candidate_pools=(CandidatePool.R1, CandidatePool.ANCHOR, CandidatePool.GROUNDED),
        expected_evidence_types=("text_span",),
        stop_condition="evidence ready",
        expected_section=section,
        priority=90,
        need_facets=(),
    )


def _selection(need: Stage1MemoryNeed, slice_: Any, rank: int = 1) -> NeedEvidenceSelection:
    return NeedEvidenceSelection(
        need=need,
        selections=(
            SliceSelectionTrace(
                slice_id=slice_.slice_id,
                unit_id=StableId("unit.test.1"),
                route_channel="anchor_bm25",
                fused_rank=rank,
                rerank_score=0.9,
                selection_reason="test",
                evidence_ref=None,
            ),
        ),
        slices=(slice_,),
    )


class TestEvidenceSliceResolver:
    def test_paragraph_slicing_is_exact_and_stable(self) -> None:
        resolver = EvidenceSliceResolver()
        text = "第一段。\n第二段,讨论伤势。\n第三段。"
        block = _block(text)
        slices = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert len(slices) == 3
        for slice_ in slices:
            assert block.text[slice_.start : slice_.end] == slice_.text
            assert slice_.slice_kind is EvidenceSliceKind.PARAGRAPH
        again = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert tuple(item.slice_id for item in slices) == tuple(item.slice_id for item in again)

    def test_offset_round_trip_against_parent_block(self) -> None:
        resolver = EvidenceSliceResolver()
        text = "　　首段内容。\n　　第二段内容较长,包含具体事实。"
        block = _block(text)
        for slice_ in resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        ):
            assert block.text[slice_.start : slice_.end] == slice_.text

    def test_evidence_resolution_uses_covering_paragraph(self) -> None:
        resolver = EvidenceSliceResolver()
        text = "第一段。\n　　陈长生的伤势已经痊愈,他可以继续修行。\n第三段。"
        block = _block(text)
        paragraph_start = text.index("　　陈长生的伤势")
        paragraph_end = text.index("\n第三段")
        evidence = _evidence(block, paragraph_start + 2, paragraph_end)
        slices = resolver.resolve_evidence(
            evidence,
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert len(slices) == 1
        assert "陈长生的伤势" in slices[0].text
        assert slices[0].text == text[paragraph_start:paragraph_end].strip()

    def test_long_paragraph_falls_back_to_sentence_window(self) -> None:
        resolver = EvidenceSliceResolver(paragraph_char_limit=120, sentence_window_char_limit=120)
        long_sentence = "陈长生站在梧桐树下,看着远处的山。" * 20
        text = f"第一段。\n{long_sentence}\n第三段。"
        block = _block(text)
        slices = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert len(slices) == 3
        window = slices[1]
        assert window.slice_kind is EvidenceSliceKind.SENTENCE_WINDOW
        assert len(window.text) <= 120
        assert block.text[window.start : window.end] == window.text

    def test_oversized_sentence_paragraph_fails_closed(self) -> None:
        resolver = EvidenceSliceResolver(paragraph_char_limit=40, sentence_window_char_limit=20)
        text = "超长句没有句末标点可以切割" * 20
        block = _block(text)
        slices = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert slices == ()
        evidence = _evidence(block, 0, len(text))
        with pytest.raises(EvidenceSliceResolutionError):
            resolver.resolve_evidence(
                evidence,
                block,
                source_commit=COMMIT,
                snapshot_id=SNAPSHOT,
                access_scope="writer_safe",
            )

    def test_heading_source_role_filter_and_short_sentence_negative_control(self) -> None:
        resolver = EvidenceSliceResolver()
        text = "第五章 旧誓言\n　　正文内容。"
        block = _block(text)
        heading = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
            source_role=EvidenceSliceSourceRole.HEADING,
        )
        assert heading == ()
        heading_allowed = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
            source_role=EvidenceSliceSourceRole.HEADING,
            allow_heading_evidence=True,
        )
        assert len(heading_allowed) == 2
        narrative = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
            source_role=EvidenceSliceSourceRole.NARRATIVE,
        )
        # a short narrative sentence is never filtered by the heading rule
        assert len(narrative) == 2

    def test_forged_evidence_fails_closed(self) -> None:
        resolver = EvidenceSliceResolver()
        block = _block("第一段。\n第二段。")
        forged = _evidence(block, 0, len(block.text)).model_copy(
            update={"object_hash": ArtifactId("sha256:" + "d" * 64)}
        )
        with pytest.raises(EvidenceSliceResolutionError, match="object hash"):
            resolver.resolve_evidence(
                forged,
                block,
                source_commit=COMMIT,
                snapshot_id=SNAPSHOT,
                access_scope="writer_safe",
            )
        bad_span = _evidence(block, 0, len(block.text) + 10)
        with pytest.raises(EvidenceSliceResolutionError, match="exceeds"):
            resolver.resolve_evidence(
                bad_span,
                block,
                source_commit=COMMIT,
                snapshot_id=SNAPSHOT,
                access_scope="writer_safe",
            )
        with pytest.raises(EvidenceSliceResolutionError, match="no precise span"):
            resolver.resolve_evidence(
                bad_span.model_copy(update={"span": None}),
                block,
                source_commit=COMMIT,
                snapshot_id=SNAPSHOT,
                access_scope="writer_safe",
            )

    def test_constructor_validation(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            EvidenceSliceResolver(paragraph_char_limit=0)
        with pytest.raises(ValueError, match="cannot exceed"):
            EvidenceSliceResolver(paragraph_char_limit=10, sentence_window_char_limit=20)


class TestEvidenceFirstWriterContextAssembler:
    def _assembly(
        self,
        *selections: NeedEvidenceSelection,
        writer_budget: int = 4000,
        text_root: TextRootDocument | None = None,
    ) -> Any:
        assembler = EvidenceFirstWriterContextAssembler()
        text_root = text_root or _text_root(
            (_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),)
        )
        return assembler.assemble(
            task=_task(),
            selections=tuple(selections),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
            writer_token_budget=writer_budget,
            evidence_ledger_token_budget=12_000,
        )

    def _slice(self, text_root: TextRootDocument, index: int = 1) -> Any:
        block = next(block for scene in text_root.chapters[0].scenes for block in scene.blocks)
        return EvidenceSliceResolver().resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )[index]

    def test_ready_package_requires_no_claims(self) -> None:
        need = _need()
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        result = self._assembly(_selection(need, self._slice(text_root)))
        assert result.status is ContextAssemblyStatus.READY
        package = result.package
        assert package.contract_version == "writer_context.v2"
        assert len(package.items) == 1
        item = package.items[0]
        assert item.evidence_ledger_ids
        assert item.gap is None
        assert "claim" not in item.model_dump(mode="json")
        assert result.evidence_ledger.contract_version == "evidence_ledger.v2"
        assert all(
            entry.dereference_receipt == "verified_read" for entry in result.evidence_ledger.entries
        )

    def test_unselected_need_becomes_typed_gap(self) -> None:
        mandatory = _need()
        result = self._assembly(NeedEvidenceSelection(need=mandatory, selections=(), slices=()))
        # Typed gaps do not block the ADR-0008 mechanical package READY; they
        # mark the mandatory-facet closure INCOMPLETE (the repair-campaign gate).
        assert result.status is ContextAssemblyStatus.READY
        assert result.mandatory_facet_closure == "INCOMPLETE"
        assert len(result.package.items) == 1
        item = result.package.items[0]
        assert item.gap is not None
        assert item.gap.kind is EvidenceGapKind.NO_SELECTED_EVIDENCE
        assert item.evidence_ledger_ids == ()
        assert result.evidence_ledger.entries == ()
        optional = _need("need.test.opt", mandatory=False)
        result = self._assembly(NeedEvidenceSelection(need=optional, selections=(), slices=()))
        assert result.status is ContextAssemblyStatus.READY
        assert result.mandatory_facet_closure == "COMPLETE"
        assert result.package.items[0].gap is not None

    def test_same_slice_serves_multiple_needs_with_one_ledger_entry(self) -> None:
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = self._slice(text_root)
        need_a = _need("need.test.a")
        need_b = _need("need.test.b")
        result = self._assembly(
            _selection(need_a, slice_, rank=1),
            _selection(need_b, slice_, rank=2),
        )
        assert len(result.evidence_ledger.entries) == 1
        entry = result.evidence_ledger.entries[0]
        assert set(entry.need_ids) == {need_a.need_id, need_b.need_id}
        assert len(result.package.items) == 2

    def test_shared_ledger_span_is_charged_once_in_budget(self) -> None:
        """Round 2 packer: a span shared by several Needs is rendered once in
        the ledger, so the ledger budget must charge it once, not per item."""
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = self._slice(text_root)
        shared_cost = EvidenceFirstWriterContextAssembler().count_tokens(slice_.text)
        need_a = _need("need.test.a")
        need_b = _need("need.test.b")
        need_c = _need("need.test.c")
        assembler = EvidenceFirstWriterContextAssembler()
        result = assembler.assemble(
            task=_task(),
            selections=(
                _selection(need_a, slice_, rank=1),
                _selection(need_b, slice_, rank=2),
                _selection(need_c, slice_, rank=3),
            ),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
            writer_token_budget=4000,
            # Only the shared span fits in the ledger; a per-item double
            # charge would drop every item after the first.
            evidence_ledger_token_budget=shared_cost,
        )
        assert result.status is ContextAssemblyStatus.READY
        assert len(result.evidence_ledger.entries) == 1
        assert len(result.package.items) == 3
        assert all(item.gap is None for item in result.package.items)

    def test_dangling_ledger_ref_rejected_by_domain(self) -> None:
        with pytest.raises(ValueError, match="requires ledger refs or a typed gap"):
            WriterContextEvidenceItem(
                item_id=StableId("item.test"),
                section=WriterContextSection.CURRENT_WORLD_STATE,
                need_ids=(StableId("need.test.1"),),
                need_facet_ids=(),
                purpose="purpose",
                evidence_ledger_ids=(),
                raw_preview="",
            )

    def test_preview_truncation_is_explicit(self) -> None:
        need = _need()
        text_root = _text_root((_block("第一段。\n第二段," + "伤势细节" * 200 + "。\n第三段。"),))
        block = next(block for scene in text_root.chapters[0].scenes for block in scene.blocks)
        resolver = EvidenceSliceResolver(paragraph_char_limit=DEFAULT_PARAGRAPH_CHAR_LIMIT * 4)
        slice_ = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
            paragraph_budget=DEFAULT_PARAGRAPH_CHAR_LIMIT * 4,
        )[1]
        result = self._assembly(_selection(need, slice_), text_root=text_root)
        item = result.package.items[0]
        assert item.preview_truncated is True
        assert item.raw_preview.endswith("…")
        entry_text = result.evidence_ledger.entries[0].evidence_text
        assert item.raw_preview.rstrip("…") == entry_text[: len(item.raw_preview) - 1]

    def test_writer_budget_overflow_becomes_budget_gap(self) -> None:
        needs = tuple(_need(f"need.test.{index}") for index in range(12))
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = self._slice(text_root)
        result = self._assembly(
            *(_selection(need, slice_) for need in needs),
            writer_budget=200,
        )
        assert result.status is not ContextAssemblyStatus.READY
        assert any(
            item.gap is not None and item.gap.kind is EvidenceGapKind.BUDGET_EXCEEDED
            for item in result.package.items
        )
        assert any(item.gap is None for item in result.package.items)

    def test_tainted_or_future_slice_fails_closed(self) -> None:
        need = _need()
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = self._slice(text_root)
        future = slice_.model_copy(update={"chapter_id": StableId("chapter.test.99")})
        result = self._assembly(_selection(need, future))
        assert result.package.items[0].gap is not None
        assert result.package.items[0].gap.kind is EvidenceGapKind.NO_SELECTED_EVIDENCE
        scoped = slice_.model_copy(update={"access_scope": "evaluator"})
        result = self._assembly(_selection(need, scoped))
        assert result.package.items[0].gap is not None

    def test_gold_future_fields_cannot_enter_package(self) -> None:
        from novel_agent.services.memory_benchmark_contract import assert_safe_public_payload

        need = _need()
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = self._slice(text_root)
        result = self._assembly(_selection(need, slice_))
        payload = result.package.model_dump(mode="json")
        # the package itself must pass the public-payload guard...
        assert_safe_public_payload(payload)
        # ...and no private-named key may enter the package, manifest or query
        for item in result.package.items:
            assert "gold" not in item.purpose.casefold()
            assert "future" not in item.purpose.casefold()
        for key in payload:
            assert "gold" not in key.casefold()
            assert "future" not in key.casefold()
        # the guard itself rejects private-named keys (defense in depth)
        with pytest.raises(ValueError, match="forbidden in public payload"):
            assert_safe_public_payload({"gold_answer": "secret"})

    def test_markdown_projection_is_deterministic_projection_of_json(self) -> None:
        from scripts.run_evidence_first_frozen_checkpoints import _render_markdown

        need = _need()
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = self._slice(text_root)
        assembler = EvidenceFirstWriterContextAssembler()
        result = assembler.assemble(
            task=_task(),
            selections=(_selection(need, slice_),),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
        )
        manifest = EvidenceFirstPackageManifest(
            manifest_id=StableId("manifest.test"),
            experiment_id="test",
            case_id=StableId("case.evidence-first"),
            checkpoint_chapter=5,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            assembler_version=assembler.version,
            run_config_hash=ArtifactId("sha256:" + "f" * 64),
            package_artifact_ref=result.package.evidence_ledger_ref,
            evidence_ledger_ref=result.package.evidence_ledger_ref,
            package_hash=result.package.evidence_ledger_ref.artifact_id,
            evidence_ledger_hash=result.package.evidence_ledger_ref.artifact_id,
            generated_at="2026-08-11T00:00:00+00:00",
            writer_token_budget=4000,
            evidence_ledger_token_budget=12000,
            need_count=1,
            item_count=1,
            gap_count=0,
            ledger_entry_count=1,
            ledger_tokens=1,
            future_leakage_count=0,
            budget_status="READY",
            assembly_status="READY",
            mandatory_facet_closure=result.mandatory_facet_closure,
        )
        first = _render_markdown(result.package, result.evidence_ledger, manifest)
        second = _render_markdown(result.package, result.evidence_ledger, manifest)
        assert first == second
        assert "writer_context.v2" in first
        assert "第二段" in first

    def test_legacy_v1_package_remains_readable_but_v2_is_default(self) -> None:
        from novel_agent.domain.writer_context import (
            EvidenceLedger,
            EvidenceLedgerEntry,
            WriterContextPackage,
        )

        task = _task()
        ledger_entry = EvidenceLedgerEntry(
            ledger_id=StableId("ledger.legacy"),
            evidence_refs=(_evidence(_block("段落。"), 0, 3),),
            claim_excerpt="legacy excerpt",
            source_commit=COMMIT,
            information_scope="writer_safe",
        )
        ledger = EvidenceLedger(
            contract_version="evidence_ledger.v1",
            entries=(ledger_entry,),
            rendered_tokens=10,
        )
        package = WriterContextPackage(
            contract_version="writer_context.v1",
            task_contract=task,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
            current_world_state=(),
            gaps=(),
            budget_report=package_budget_legacy(),
            evidence_ledger_ref=ArtifactRef_legacy(),
            lineage=lineage_legacy(),
            rendered_context="",
        )
        assert package.contract_version == "writer_context.v1"
        assert ledger.contract_version == "evidence_ledger.v1"
        # the default assembler always emits v2
        need = _need()
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        result = self._assembly(_selection(need, self._slice(text_root)))
        assert result.package.contract_version == "writer_context.v2"
        assert result.evidence_ledger.contract_version == "evidence_ledger.v2"


def package_budget_legacy() -> Any:
    from novel_agent.domain.writer_context import ContextAssemblyStatus, WriterContextBudgetReport

    return WriterContextBudgetReport(
        tokenizer="deterministic_unicode",
        tokenizer_version="v1",
        configured_writer_token_budget=4000,
        actual_rendered_writer_tokens=0,
        evidence_ledger_tokens=10,
        mandatory_conclusion_tokens=0,
        optional_conclusion_tokens=0,
        header_citation_gap_tokens=0,
        deduplicated_item_count=0,
        superseded_item_count=0,
        dropped_optional_ids=(),
        dropped_optional_reasons={},
        reduction_rounds=0,
        final_status=ContextAssemblyStatus.READY,
    )


def ArtifactRef_legacy() -> Any:
    from novel_agent.domain.artifacts import ArtifactRef
    from novel_agent.domain.ids import SchemaVersion

    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "e" * 64),
        media_type="application/vnd.novel-agent.evidence-ledger+json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )


def lineage_legacy() -> Any:
    from novel_agent.domain.writer_context import ContextLineage

    return ContextLineage(
        need_ids=(),
        retrieval_unit_ids=(),
        assembler_version="writer_context_assembler.v18",
        normalized_unit_count=0,
    )


class TestEvidenceSliceResolverCoverage:
    def test_evidence_verification_rejects_chapter_scene_and_quote_mismatch(self) -> None:
        resolver = EvidenceSliceResolver()
        block = _block("第一段。\n第二段。")
        evidence = _evidence(block, 0, 3)
        with pytest.raises(EvidenceSliceResolutionError, match="chapter"):
            resolver.resolve_evidence(
                evidence.model_copy(update={"chapter_id": StableId("chapter.test.9")}),
                block,
                source_commit=COMMIT,
                snapshot_id=SNAPSHOT,
                access_scope="writer_safe",
            )
        other_scene = _block("第一段。\n第二段。").model_copy(
            update={"scene_id": StableId("scene.test.5.9")}
        )
        with pytest.raises(EvidenceSliceResolutionError, match="scene"):
            resolver.resolve_evidence(
                evidence,
                other_scene,
                source_commit=COMMIT,
                snapshot_id=SNAPSHOT,
                access_scope="writer_safe",
            )
        with pytest.raises(EvidenceSliceResolutionError, match="quote hash"):
            resolver.resolve_evidence(
                evidence.model_copy(update={"quote_hash": quote_hash("其他")}),
                block,
                source_commit=COMMIT,
                snapshot_id=SNAPSHOT,
                access_scope="writer_safe",
            )
        whitespace = _block("   \n\u3000\u3000\n  ")
        with pytest.raises(EvidenceSliceResolutionError, match="outside the parent block"):
            resolver.resolve_evidence(
                _evidence(whitespace, 0, 1),
                whitespace,
                source_commit=COMMIT,
                snapshot_id=SNAPSHOT,
                access_scope="writer_safe",
            )

    def test_whitespace_only_paragraphs_are_skipped(self) -> None:
        resolver = EvidenceSliceResolver()
        block = _block("第一段。\n   \n　　\n第三段。")
        slices = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert len(slices) == 2

    def test_sentence_window_without_sentence_boundaries_fails_closed(self) -> None:
        resolver = EvidenceSliceResolver(paragraph_char_limit=8, sentence_window_char_limit=8)
        block = _block("无标点句子" * 30)
        assert (
            resolver.resolve_block(
                block,
                source_commit=COMMIT,
                snapshot_id=SNAPSHOT,
                access_scope="writer_safe",
            )
            == ()
        )

    def test_sentence_window_prefers_smaller_adjacent_sentence(self) -> None:
        resolver = EvidenceSliceResolver(paragraph_char_limit=12, sentence_window_char_limit=12)
        block = _block("短句。这是一个非常长的句子。又短句。")
        slices = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert len(slices) == 1
        window = slices[0]
        assert window.slice_kind is EvidenceSliceKind.SENTENCE_WINDOW
        assert len(window.text) <= 12
        assert block.text[window.start : window.end] == window.text


class TestEvidenceFirstAssemblerCoverage:
    def test_assembler_argument_validation(self) -> None:
        assembler = EvidenceFirstWriterContextAssembler()
        need = _need()
        with pytest.raises(ValueError, match="arm must be A, B, or C"):
            assembler.assemble(
                task=_task(),
                selections=(NeedEvidenceSelection(need=need, selections=(), slices=()),),
                text_root=_text_root((_block("段落。"),)),
                basis_commit_id=COMMIT,
                basis_snapshot_id=SNAPSHOT,
                arm="X",
            )
        with pytest.raises(ValueError, match="budgets must be positive"):
            assembler.assemble(
                task=_task(),
                selections=(NeedEvidenceSelection(need=need, selections=(), slices=()),),
                text_root=_text_root((_block("段落。"),)),
                basis_commit_id=COMMIT,
                basis_snapshot_id=SNAPSHOT,
                arm="A",
                writer_token_budget=0,
            )
        with pytest.raises(ValueError, match="at least one Need selection"):
            assembler.assemble(
                task=_task(),
                selections=(),
                text_root=_text_root((_block("段落。"),)),
                basis_commit_id=COMMIT,
                basis_snapshot_id=SNAPSHOT,
                arm="A",
            )

    def test_duplicate_need_selections_fail_closed(self) -> None:
        need = _need()
        assembler = EvidenceFirstWriterContextAssembler()
        with pytest.raises(ValueError, match="unique by Need"):
            assembler.assemble(
                task=_task(),
                selections=(
                    NeedEvidenceSelection(need=need, selections=(), slices=()),
                    NeedEvidenceSelection(need=need, selections=(), slices=()),
                ),
                text_root=_text_root((_block("段落。"),)),
                basis_commit_id=COMMIT,
                basis_snapshot_id=SNAPSHOT,
                arm="A",
            )

    def test_missing_and_dangling_slice_traces_become_gaps(self) -> None:
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = TestEvidenceFirstWriterContextAssembler()._slice(text_root)
        need = _need()
        missing = NeedEvidenceSelection(
            need=need,
            selections=(
                SliceSelectionTrace(
                    slice_id=StableId("slice.ghost"),
                    unit_id=StableId("unit.test.1"),
                    route_channel="anchor_bm25",
                    fused_rank=1,
                    selection_reason="test",
                    evidence_ref=None,
                ),
            ),
            slices=(slice_,),
        )
        assembler = EvidenceFirstWriterContextAssembler()
        result = assembler.assemble(
            task=_task(),
            selections=(missing,),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
        )
        assert result.package.items[0].gap is not None
        assert any(code.startswith("SELECTED_SLICE_MISSING") for code in result.diagnostic_codes)
        dangling = NeedEvidenceSelection(
            need=need,
            selections=(
                SliceSelectionTrace(
                    slice_id=slice_.slice_id,
                    unit_id=StableId("unit.test.1"),
                    route_channel="anchor_bm25",
                    fused_rank=1,
                    selection_reason="test",
                    evidence_ref=None,
                ),
            ),
            slices=(slice_,),
        )
        text_root = _text_root((_block("完全不同的段落内容。"),))
        result = assembler.assemble(
            task=_task(),
            selections=(dangling,),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
        )
        assert result.package.items[0].gap is not None
        assert any(
            code.startswith("SLICE_REJECTED:dereference_failed") for code in result.diagnostic_codes
        )
        assert result.mechanical_failure_counts == {"dereference": 1, "scope": 0, "cutoff": 0}

    def test_ledger_budget_overflow_status(self) -> None:
        text_root = _text_root((_block("第一段。\n第二段," + "伤势" * 2000 + "。\n第三段。"),))
        block = next(block for scene in text_root.chapters[0].scenes for block in scene.blocks)
        resolver = EvidenceSliceResolver(paragraph_char_limit=9000, sentence_window_char_limit=9000)
        slice_ = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
            paragraph_budget=9000,
            sentence_window_budget=9000,
        )[1]
        assembler = EvidenceFirstWriterContextAssembler()
        result = assembler.assemble(
            task=_task(),
            selections=(_selection(_need(), slice_),),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
            evidence_ledger_token_budget=100,
        )
        # A ledger budget too small for the mandatory evidence produces a typed
        # gap: mechanical READY with mandatory-facet closure INCOMPLETE.
        assert result.status is ContextAssemblyStatus.READY
        assert result.mandatory_facet_closure == "INCOMPLETE"
        assert any(item.gap is not None for item in result.package.items)
        assert any(
            code.startswith(
                ("EVIDENCE_LEDGER_BUDGET_EXCEEDED", "MANDATORY_FACET_CLOSURE_INCOMPLETE")
            )
            for code in result.diagnostic_codes
        )

    def test_two_pass_packing_emits_per_facet_gap_for_unserved_mandatory_facet(
        self,
    ) -> None:
        """2026-08-13 repair D: fair two-pass packing with per-facet typed gaps."""
        from novel_agent.domain.memory import (
            ExpectedClaimScope,
            FacetEvidenceRequirement,
            NeedCompletionSpec,
            NeedFacet,
            NeedFacetKind,
            NeedGapPolicy,
            NeedUncertaintyPolicy,
        )

        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = TestEvidenceFirstWriterContextAssembler()._slice(text_root)
        need = _need("need.test.facets")
        facet_a = NeedFacet(
            need_facet_id=StableId("need-facet.a"),
            need_id=need.need_id,
            facet_kind=NeedFacetKind.CURRENT_STATE,
            expected_claim_scope=ExpectedClaimScope.CURRENT,
            derivation_refs=(need.need_id,),
            producer="test",
            producer_version="v1",
            information_scope="cutoff_safe",
        )
        facet_b = NeedFacet(
            need_facet_id=StableId("need-facet.b"),
            need_id=need.need_id,
            facet_kind=NeedFacetKind.RELATION_STATE,
            expected_claim_scope=ExpectedClaimScope.CURRENT,
            derivation_refs=(need.need_id,),
            producer="test",
            producer_version="v1",
            information_scope="cutoff_safe",
        )
        spec = NeedCompletionSpec(
            need_id=need.need_id,
            required_need_facet_ids=(facet_a.need_facet_id, facet_b.need_facet_id),
            irreducible_need_facet_ids=(facet_a.need_facet_id, facet_b.need_facet_id),
            evidence_requirement_by_facet={
                facet_a.need_facet_id.root: FacetEvidenceRequirement.TRACEABLE_CUTOFF_SOURCE,
                facet_b.need_facet_id.root: FacetEvidenceRequirement.TRACEABLE_CUTOFF_SOURCE,
            },
            min_distinct_evidence_sources=1,
            min_distinct_chapters=1,
            uncertainty_policy=NeedUncertaintyPolicy.ALLOW_GAP_ONLY,
            gap_policy=NeedGapPolicy.FAIL_MANDATORY,
            producer="test",
            producer_version="v1",
        )
        need = need.model_copy(update={"need_facets": (facet_a, facet_b), "completion_spec": spec})
        selection = NeedEvidenceSelection(
            need=need,
            selections=(
                SliceSelectionTrace(
                    slice_id=slice_.slice_id,
                    unit_id=StableId("unit.test.facets"),
                    route_channel="r1_exact",
                    fused_rank=1,
                    rerank_score=0.9,
                    selection_reason="test",
                    evidence_ref=None,
                    supported_facet_ids=(facet_a.need_facet_id,),
                ),
            ),
            slices=(slice_,),
            facet_receipts=(),
        )
        result = EvidenceFirstWriterContextAssembler().assemble(
            task=_task(),
            selections=(selection,),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
        )
        # Facet A is closed by exact evidence; facet B has no serving slice and
        # becomes a per-facet typed gap: mechanical READY, closure INCOMPLETE.
        assert result.status is ContextAssemblyStatus.READY
        assert result.mandatory_facet_closure == "INCOMPLETE"
        evidence_items = tuple(item for item in result.package.items if item.gap is None)
        gap_items = tuple(item for item in result.package.items if item.gap is not None)
        assert len(evidence_items) == 1
        assert evidence_items[0].need_facet_ids == (facet_a.need_facet_id, facet_b.need_facet_id)
        assert len(gap_items) == 1
        assert gap_items[0].need_facet_ids == (facet_b.need_facet_id,)
        assert gap_items[0].mandatory is True
        assert any(code.startswith("MANDATORY_FACET_GAP") for code in result.diagnostic_codes)

    def test_validity_mapping_and_prelude_chapter(self) -> None:
        from novel_agent.domain.memory import (
            ExpectedClaimScope,
            FacetEvidenceRequirement,
            NeedCompletionSpec,
            NeedFacet,
            NeedFacetKind,
            NeedGapPolicy,
            NeedUncertaintyPolicy,
        )

        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = TestEvidenceFirstWriterContextAssembler()._slice(text_root)
        facet = NeedFacet(
            need_facet_id=StableId("facet.historical"),
            need_id=StableId("need.test.1"),
            facet_kind=NeedFacetKind.CAUSAL_HISTORY,
            expected_claim_scope=ExpectedClaimScope.HISTORICAL,
            derivation_refs=(StableId("need.test.1"),),
            producer="test",
            producer_version="v1",
            information_scope="cutoff_safe",
        )
        need = _need().model_copy(
            update={
                "need_facets": (facet,),
                "completion_spec": NeedCompletionSpec(
                    need_id=StableId("need.test.1"),
                    required_need_facet_ids=(facet.need_facet_id,),
                    irreducible_need_facet_ids=(facet.need_facet_id,),
                    evidence_requirement_by_facet={
                        facet.need_facet_id.root: (
                            FacetEvidenceRequirement.DISTINCT_HISTORICAL_SOURCE
                        )
                    },
                    uncertainty_policy=NeedUncertaintyPolicy.ALLOW_GAP_ONLY,
                    gap_policy=NeedGapPolicy.FAIL_MANDATORY,
                    producer="test",
                    producer_version="v1",
                ),
            }
        )
        assembler = EvidenceFirstWriterContextAssembler()
        result = assembler.assemble(
            task=_task(),
            selections=(_selection(need, slice_),),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
        )
        assert result.package.items[0].validity.value == "historical"
        assert assembler._chapter_number(StableId("chapter.test.prelude")) == 0
        assert assembler._chapter_number(StableId("prelude.test")) == 0
        assert assembler._chapter_number(StableId("chapter.test.5")) == 5
        assert assembler._chapter_number(StableId("chapter.without.number")) is None

    def test_purpose_falls_back_to_query_text(self) -> None:
        need = _need().model_copy(
            update={
                "semantic_question": "",
                "why_needed": "",
                "planner_artifact_ref": None,
                "planned_draft_id": None,
                "validated_need_set_hash": None,
            }
        )
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = TestEvidenceFirstWriterContextAssembler()._slice(text_root)
        assembler = EvidenceFirstWriterContextAssembler()
        result = assembler.assemble(
            task=_task(),
            selections=(_selection(need, slice_),),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
        )
        assert result.package.items[0].purpose == need.query_text

    def test_domain_negative_validators(self) -> None:
        from novel_agent.domain.writer_context import (
            EvidenceLedgerEntryV2,
            EvidenceLedgerV2,
            EvidenceSlice,
        )

        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = TestEvidenceFirstWriterContextAssembler()._slice(text_root)
        with pytest.raises(ValueError, match="end must be greater than or equal to start"):
            EvidenceSlice(
                slice_id=slice_.slice_id,
                parent_block_id=slice_.parent_block_id,
                chapter_id=slice_.chapter_id,
                scene_id=slice_.scene_id,
                start=5,
                end=0,
                text=slice_.text,
                object_hash=slice_.object_hash,
                quote_hash=slice_.quote_hash,
                source_commit=slice_.source_commit,
                snapshot_id=slice_.snapshot_id,
                access_scope=slice_.access_scope,
                slice_kind=slice_.slice_kind,
                source_role=slice_.source_role,
            )
        with pytest.raises(ValueError, match="at least one exact slice"):
            EvidenceLedgerEntryV2(
                ledger_id=StableId("ledger.x"),
                evidence_slices=(),
                evidence_text="x",
                evidence_refs=(),
                basis_commit_id=COMMIT,
                basis_snapshot_id=SNAPSHOT,
                cutoff_chapter=5,
                information_scope="writer_safe",
                text_hash=ArtifactId("sha256:" + "f" * 64),
                span_hash=ArtifactId("sha256:" + "f" * 64),
                quote_hash=quote_hash("x"),
                need_ids=(),
                need_facet_ids=(),
            )
        with pytest.raises(ValueError, match="text must match its exact slices"):
            EvidenceLedgerEntryV2(
                ledger_id=StableId("ledger.x"),
                evidence_slices=(slice_,),
                evidence_text="different",
                evidence_refs=(),
                basis_commit_id=COMMIT,
                basis_snapshot_id=SNAPSHOT,
                cutoff_chapter=5,
                information_scope="writer_safe",
                text_hash=ArtifactId("sha256:" + "f" * 64),
                span_hash=ArtifactId("sha256:" + "f" * 64),
                quote_hash=quote_hash("x"),
                need_ids=(StableId("need.test.1"),),
                need_facet_ids=(),
            )
        with pytest.raises(ValueError, match="unique"):
            EvidenceLedgerEntryV2(
                ledger_id=StableId("ledger.x"),
                evidence_slices=(slice_, slice_),
                evidence_text=slice_.text * 2,
                evidence_refs=(),
                basis_commit_id=COMMIT,
                basis_snapshot_id=SNAPSHOT,
                cutoff_chapter=5,
                information_scope="writer_safe",
                text_hash=ArtifactId("sha256:" + "f" * 64),
                span_hash=ArtifactId("sha256:" + "f" * 64),
                quote_hash=quote_hash("x"),
                need_ids=(StableId("need.test.1"),),
                need_facet_ids=(),
            )
        entry = EvidenceLedgerEntryV2(
            ledger_id=StableId("ledger.x"),
            evidence_slices=(slice_,),
            evidence_text=slice_.text,
            evidence_refs=(),
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            cutoff_chapter=5,
            information_scope="writer_safe",
            text_hash=sha256_id(slice_.text.encode()),
            span_hash=sha256_id(b"x"),
            quote_hash=slice_.quote_hash,
            need_ids=(StableId("need.test.1"),),
            need_facet_ids=(),
        )
        with pytest.raises(ValueError, match="must be unique"):
            EvidenceLedgerV2(entries=(entry, entry), rendered_tokens=0)

    def test_manifest_readiness_validator_and_gap_codes(self) -> None:
        from novel_agent.domain.artifacts import ArtifactRef
        from novel_agent.domain.ids import SchemaVersion

        payload = canonical_json_bytes({"m": 2})
        ref = ArtifactRef(
            artifact_id=sha256_id(payload),
            media_type="application/json",
            byte_length=len(payload),
            schema_version=SchemaVersion("1.0.0"),
        )

        def manifest(**updates: Any) -> EvidenceFirstPackageManifest:
            kwargs: dict[str, Any] = {
                "budget_status": "READY",
                "assembly_status": "READY",
                "mandatory_facet_closure": "COMPLETE",
            }
            kwargs.update(updates)
            return EvidenceFirstPackageManifest(
                manifest_id=StableId("manifest.ready"),
                experiment_id="e",
                case_id=StableId("case.x"),
                checkpoint_chapter=5,
                basis_commit_id=COMMIT,
                basis_snapshot_id=SNAPSHOT,
                assembler_version="v",
                run_config_hash=ArtifactId("sha256:" + "f" * 64),
                package_artifact_ref=ref,
                evidence_ledger_ref=ref,
                package_hash=ref.artifact_id,
                evidence_ledger_hash=ref.artifact_id,
                generated_at="2026-08-11T00:00:00+00:00",
                writer_token_budget=4000,
                evidence_ledger_token_budget=12000,
                need_count=0,
                item_count=0,
                gap_count=0,
                ledger_entry_count=0,
                ledger_tokens=0,
                **kwargs,
            )

        # A READY manifest cannot carry mechanical failure counts.
        with pytest.raises(ValueError, match="cannot carry mechanical failure counts"):
            manifest(future_leakage_count=1, leakage_failure_count=1)
        with pytest.raises(ValueError, match="cannot carry mechanical failure counts"):
            manifest(dereference_failure_count=1)
        # ...nor an unchanged-root violation, nor claim-path model calls.
        with pytest.raises(ValueError, match="unchanged immutable roots"):
            manifest(root_hashes_unchanged=False)
        with pytest.raises(ValueError, match="zero claim-path model calls"):
            manifest(call_counts={"claim_support_calls": 1})
        # leakage count must mirror the future-leakage count.
        with pytest.raises(ValueError, match="must equal the future leakage count"):
            manifest(future_leakage_count=2, leakage_failure_count=1)
        # gap codes must be unique and ordered.
        with pytest.raises(ValueError, match="gap codes must be unique and ordered"):
            manifest(gap_codes=("a", "a"))
        with pytest.raises(ValueError, match="unknown graph readiness"):
            manifest(graph_readiness_by_need={"need.x": "silently_ready"})
        with pytest.raises(ValueError, match="graph READY requires"):
            manifest(
                graph_readiness_by_need={"need.x": "ready"},
                graph_edge_count=1,
            )
        with pytest.raises(ValueError, match="zero-edge projection"):
            manifest(
                graph_edge_count=0,
                verified_graph_path_receipt_ids=(StableId("graph-path.x"),),
            )
        graph_ready = manifest(
            graph_readiness_by_need={"need.x": "ready"},
            graph_edge_count=1,
            verified_graph_path_receipt_ids=(StableId("graph-path.x"),),
        )
        assert graph_ready.graph_readiness_by_need == {"need.x": "ready"}
        # mandatory-facet closure is persisted on the manifest and defaults to
        # COMPLETE; INCOMPLETE must be explicit when any mandatory facet gap
        # exists (review 2026-08-14 P1-2).
        assert manifest().mandatory_facet_closure == "COMPLETE"
        assert manifest(mandatory_facet_closure="INCOMPLETE").mandatory_facet_closure == (
            "INCOMPLETE"
        )
        # non-READY manifests may carry typed failures.
        failed = manifest(
            assembly_status="EVIDENCE_INSUFFICIENT",
            budget_status="EVIDENCE_INSUFFICIENT",
            dereference_failure_count=2,
            scope_failure_count=1,
            gap_codes=("dereference_failed",),
            root_hashes_unchanged=False,
        )
        assert failed.dereference_failure_count == 2
        # a clean READY manifest validates.
        assert manifest().assembly_status == "READY"

    def test_shared_ledger_entry_merges_evidence_refs(self) -> None:
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        block = next(block for scene in text_root.chapters[0].scenes for block in scene.blocks)
        slice_ = EvidenceSliceResolver().resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )[1]
        ref_a = _evidence(block, 0, 3)
        ref_b = _evidence(block, 3, 5)
        need_a = _need("need.test.a")
        need_b = _need("need.test.b")

        def selection(need: Any, ref: Any) -> NeedEvidenceSelection:
            return NeedEvidenceSelection(
                need=need,
                selections=(
                    SliceSelectionTrace(
                        slice_id=slice_.slice_id,
                        unit_id=StableId("unit.test.1"),
                        route_channel="anchor_bm25",
                        fused_rank=1,
                        selection_reason="test",
                        evidence_ref=ref,
                    ),
                ),
                slices=(slice_,),
            )

        result = EvidenceFirstWriterContextAssembler().assemble(
            task=_task(),
            selections=(selection(need_a, ref_a), selection(need_b, ref_b)),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
        )
        assert len(result.evidence_ledger.entries) == 1
        entry = result.evidence_ledger.entries[0]
        assert {item.evidence_id for item in entry.evidence_refs} == {
            ref_a.evidence_id,
            ref_b.evidence_id,
        }

    def test_budget_report_inconsistent_counts_and_manifest_refs(self) -> None:
        import json as _json

        from novel_agent.domain.artifacts import ArtifactRef
        from novel_agent.domain.ids import SchemaVersion
        from novel_agent.domain.writer_context import (
            EvidenceFirstPackageManifest,
            WriterContextBudgetReportV2,
            WriterContextPackageV2,
        )
        from novel_agent.services.content_addressing import canonical_json_bytes

        payload = canonical_json_bytes({"x": 1})
        ref = ArtifactRef(
            artifact_id=sha256_id(payload),
            media_type="application/json",
            byte_length=len(payload),
            schema_version=SchemaVersion("1.0.0"),
        )
        with pytest.raises(ValueError, match="item counts are inconsistent"):
            WriterContextBudgetReportV2(
                tokenizer="t",
                tokenizer_version="v",
                configured_writer_token_budget=4000,
                actual_rendered_writer_tokens=0,
                configured_ledger_token_budget=12000,
                actual_rendered_ledger_tokens=0,
                item_count=3,
                evidence_item_count=1,
                gap_item_count=1,
                ledger_entry_count=0,
                final_status=ContextAssemblyStatus.READY,
            )
        with pytest.raises(ValueError, match="ref must match its retained artifact hash"):
            EvidenceFirstPackageManifest(
                manifest_id=StableId("manifest.x"),
                experiment_id="e",
                case_id=StableId("case.x"),
                checkpoint_chapter=5,
                basis_commit_id=COMMIT,
                basis_snapshot_id=SNAPSHOT,
                assembler_version="v",
                run_config_hash=ArtifactId("sha256:" + "f" * 64),
                package_artifact_ref=ref,
                evidence_ledger_ref=ref,
                package_hash=ArtifactId("sha256:" + "e" * 64),
                evidence_ledger_hash=ref.artifact_id,
                generated_at="2026-08-11T00:00:00+00:00",
                writer_token_budget=4000,
                evidence_ledger_token_budget=12000,
                need_count=0,
                item_count=0,
                gap_count=0,
                ledger_entry_count=0,
                ledger_tokens=0,
                future_leakage_count=0,
                budget_status="READY",
                assembly_status="READY",
                mandatory_facet_closure="COMPLETE",
            )
        with pytest.raises(ValueError, match="item gap bindings"):
            gapped = (
                EvidenceFirstWriterContextAssembler()
                .assemble(
                    task=_task(),
                    selections=(NeedEvidenceSelection(need=_need(), selections=(), slices=()),),
                    text_root=text_root_default(),
                    basis_commit_id=COMMIT,
                    basis_snapshot_id=SNAPSHOT,
                    arm="A",
                )
                .package
            )
            WriterContextPackageV2.model_validate_json(
                _json.dumps({**gapped.model_dump(mode="json"), "gaps": []})
            )


def text_root_default() -> Any:
    return _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))


class TestEvidenceSliceResolverBranches:
    def test_punctuation_only_paragraph_has_no_sentences(self) -> None:
        resolver = EvidenceSliceResolver(paragraph_char_limit=40, sentence_window_char_limit=40)
        block = _block("第一段。\n" + "。" * 120 + "\n第三段。")
        slices = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert len(slices) == 2

    def test_sentence_window_condition_exit_when_center_fills_limit(self) -> None:
        resolver = EvidenceSliceResolver(paragraph_char_limit=16, sentence_window_char_limit=16)
        block = _block("一二三四五六七八九零一二三。\n第二段。")
        slices = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert len(slices) == 2

    def test_sentence_window_picks_both_neighbors_symmetrically(self) -> None:
        resolver = EvidenceSliceResolver(paragraph_char_limit=20, sentence_window_char_limit=18)
        block = _block("九字句子甲。四字。七字句子丙。九字句子乙。")
        slices = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert len(slices) == 1
        window = slices[0]
        assert window.slice_kind is EvidenceSliceKind.SENTENCE_WINDOW
        assert window.text == block.text[window.start : window.end]

    def test_resolve_evidence_heading_filter_and_long_paragraph_window(self) -> None:
        resolver = EvidenceSliceResolver(paragraph_char_limit=20, sentence_window_char_limit=18)
        block = _block("第一段。\n短句。句子很长很长很长很长很长很长很长。又短。")
        # heading-role evidence is filtered before verification
        assert (
            resolver.resolve_evidence(
                _evidence(block, 0, 3),
                block,
                source_commit=COMMIT,
                snapshot_id=SNAPSHOT,
                access_scope="writer_safe",
                source_role=EvidenceSliceSourceRole.HEADING,
            )
            == ()
        )
        paragraph_start = block.text.index("短句。")
        paragraph_end = len(block.text)
        slices = resolver.resolve_evidence(
            _evidence(block, paragraph_start, paragraph_end),
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert len(slices) == 1
        assert slices[0].slice_kind is EvidenceSliceKind.SENTENCE_WINDOW


class TestEvidenceFirstCoverageEdges:
    def test_assembler_count_tokens_and_custom_counter(self) -> None:
        assembler = EvidenceFirstWriterContextAssembler()
        assert assembler.count_tokens("一二三四五六") >= 1
        custom = EvidenceFirstWriterContextAssembler(token_counter=lambda text: len(text))
        assert custom.count_tokens("abcd") == 4

    def test_seen_span_dedupe_keeps_best_rank(self) -> None:
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = TestEvidenceFirstWriterContextAssembler()._slice(text_root)
        need = _need()
        low = SliceSelectionTrace(
            slice_id=slice_.slice_id,
            unit_id=StableId("unit.low"),
            route_channel="anchor_bm25",
            fused_rank=5,
            selection_reason="low",
            evidence_ref=None,
        )
        high = SliceSelectionTrace(
            slice_id=slice_.slice_id,
            unit_id=StableId("unit.high"),
            route_channel="anchor_dense",
            fused_rank=2,
            selection_reason="high",
            evidence_ref=None,
        )
        assembler = EvidenceFirstWriterContextAssembler()
        result = assembler.assemble(
            task=_task(),
            selections=(
                NeedEvidenceSelection(need=need, selections=(low, high), slices=(slice_,)),
            ),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
        )
        assert "anchor_dense" in result.package.items[0].selection_reason

    def test_reverify_slice_branch_coverage(self) -> None:
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        block = next(block for scene in text_root.chapters[0].scenes for block in scene.blocks)
        resolver = EvidenceSliceResolver()
        slice_ = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )[1]
        assembler = EvidenceFirstWriterContextAssembler()
        blocks = {b.block_id: b for scene in text_root.chapters[0].scenes for b in scene.blocks}
        need = _need()
        # source commit mismatch
        assert (
            assembler._reverify_slice(
                slice_.model_copy(update={"source_commit": CommitId("sha256:" + "d" * 64)}),
                blocks,
                _task(),
                need,
            )[1]
            == "dereference_failed"
        )
        # parent block absent from the text root
        assert (
            assembler._reverify_slice(
                slice_.model_copy(update={"parent_block_id": StableId("block.ghost")}),
                blocks,
                _task(),
                need,
            )[1]
            == "dereference_failed"
        )
        # access scope mismatch is a typed scope failure
        assert (
            assembler._reverify_slice(
                slice_.model_copy(update={"access_scope": "evaluator"}),
                blocks,
                _task(),
                need,
            )[1]
            == "scope_failed"
        )
        # future chapter within the text root is a typed cutoff failure
        future_root = _text_root(
            (_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。", chapter=99),)
        )
        future_block = next(
            block for scene in future_root.chapters[0].scenes for block in scene.blocks
        )
        future_slice = resolver.resolve_block(
            future_block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )[1]
        future_blocks = {
            b.block_id: b for scene in future_root.chapters[0].scenes for b in scene.blocks
        }
        assert (
            assembler._reverify_slice(future_slice, future_blocks, _task(), need)[1]
            == "cutoff_failed"
        )
        # a valid slice passes
        verified, rejection = assembler._reverify_slice(slice_, blocks, _task(), need)
        assert verified is not None
        assert rejection is None

    def test_mechanical_failure_counts_are_typed(self) -> None:
        base_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        future_root = _text_root(
            (_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。", chapter=99),)
        )
        base_block = next(block for scene in base_root.chapters[0].scenes for block in scene.blocks)
        resolver = EvidenceSliceResolver()
        base_slice = resolver.resolve_block(
            base_block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )[1]
        future_block = next(
            block for scene in future_root.chapters[0].scenes for block in scene.blocks
        )
        future_slice = resolver.resolve_block(
            future_block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )[1]

        def assemble(root: TextRootDocument, need: Any, slice_: Any) -> Any:
            return EvidenceFirstWriterContextAssembler().assemble(
                task=_task(),
                selections=(_selection(need, slice_),),
                text_root=root,
                basis_commit_id=COMMIT,
                basis_snapshot_id=SNAPSHOT,
                arm="A",
            )

        scoped = base_slice.model_copy(update={"access_scope": "evaluator"})
        forged = base_slice.model_copy(update={"text": "篡改文本"})
        result_cutoff = assemble(future_root, _need("need.test.future"), future_slice)
        result_scope = assemble(base_root, _need("need.test.scope"), scoped)
        result_forged = assemble(base_root, _need("need.test.forged"), forged)
        assert result_cutoff.mechanical_failure_counts == {
            "dereference": 0,
            "scope": 0,
            "cutoff": 1,
        }
        assert result_scope.mechanical_failure_counts == {
            "dereference": 0,
            "scope": 1,
            "cutoff": 0,
        }
        assert result_forged.mechanical_failure_counts == {
            "dereference": 1,
            "scope": 0,
            "cutoff": 0,
        }
        assert set(result_cutoff.package.budget_report.dropped_slice_reasons.values()) == {
            "cutoff_failed"
        }
        assert set(result_scope.package.budget_report.dropped_slice_reasons.values()) == {
            "scope_failed"
        }
        assert set(result_forged.package.budget_report.dropped_slice_reasons.values()) == {
            "dereference_failed"
        }

    def test_purpose_with_why_needed_and_validity_planned(self) -> None:
        from novel_agent.domain.memory import (
            ExpectedClaimScope,
            FacetEvidenceRequirement,
            NeedCompletionSpec,
            NeedFacet,
            NeedFacetKind,
            NeedGapPolicy,
            NeedUncertaintyPolicy,
        )

        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = TestEvidenceFirstWriterContextAssembler()._slice(text_root)
        need = _need()
        facet = NeedFacet(
            need_facet_id=StableId("facet.planned"),
            need_id=need.need_id,
            facet_kind=NeedFacetKind.PLAN_NODE,
            expected_claim_scope=ExpectedClaimScope.PLANNED,
            derivation_refs=(need.need_id,),
            producer="test",
            producer_version="v1",
            information_scope="author_plan",
        )
        planned = need.model_copy(
            update={
                "claim_may_cite_plan": True,
                "need_facets": (facet,),
                "completion_spec": NeedCompletionSpec(
                    need_id=need.need_id,
                    required_need_facet_ids=(facet.need_facet_id,),
                    irreducible_need_facet_ids=(facet.need_facet_id,),
                    evidence_requirement_by_facet={
                        facet.need_facet_id.root: (FacetEvidenceRequirement.PLAN_PROVENANCE)
                    },
                    uncertainty_policy=NeedUncertaintyPolicy.ALLOW_GAP_ONLY,
                    gap_policy=NeedGapPolicy.FAIL_MANDATORY,
                    producer="test",
                    producer_version="v1",
                ),
            }
        )
        assembler = EvidenceFirstWriterContextAssembler()
        result = assembler.assemble(
            task=_task(),
            selections=(_selection(planned, slice_),),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
        )
        item = result.package.items[0]
        assert item.purpose.startswith(planned.query_text)
        assert item.validity.value == "planned"

    def test_domain_v2_validator_edges(self) -> None:
        from novel_agent.domain.planning_memory import PlannerTargetStateCoverage
        from novel_agent.domain.writer_context import (
            EvidenceFirstGap,
            EvidenceLedgerEntryV2,
            WriterContextBudgetReportV2,
            WriterContextEvidenceItem,
        )

        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = TestEvidenceFirstWriterContextAssembler()._slice(text_root)
        need = _need()
        with pytest.raises(ValueError, match="counts are inconsistent"):
            PlannerTargetStateCoverage(label="x", available=3, selected=1, truncated=1)
        with pytest.raises(ValueError, match="at least one public Need"):
            EvidenceLedgerEntryV2(
                ledger_id=StableId("ledger.x"),
                evidence_slices=(slice_,),
                evidence_text=slice_.text,
                evidence_refs=(),
                basis_commit_id=COMMIT,
                basis_snapshot_id=SNAPSHOT,
                cutoff_chapter=5,
                information_scope="writer_safe",
                text_hash=sha256_id(slice_.text.encode()),
                span_hash=sha256_id(b"x"),
                quote_hash=slice_.quote_hash,
                need_ids=(),
                need_facet_ids=(),
            )
        with pytest.raises(ValueError, match="cannot carry evidence"):
            WriterContextEvidenceItem(
                item_id=StableId("item.gap-ev"),
                section=WriterContextSection.CURRENT_WORLD_STATE,
                need_ids=(need.need_id,),
                need_facet_ids=(),
                purpose="p",
                evidence_ledger_ids=(StableId("ledger.x"),),
                raw_preview="x",
                gap=EvidenceFirstGap(
                    gap_id=StableId("gap.x"),
                    need_ids=(need.need_id,),
                    need_facet_ids=(),
                    kind=EvidenceGapKind.NO_SELECTED_EVIDENCE,
                    reason="r",
                ),
            )
        with pytest.raises(ValueError, match="requires a raw preview"):
            WriterContextEvidenceItem(
                item_id=StableId("item.no-preview"),
                section=WriterContextSection.CURRENT_WORLD_STATE,
                need_ids=(need.need_id,),
                need_facet_ids=(),
                purpose="p",
                evidence_ledger_ids=(StableId("ledger.x"),),
                raw_preview="",
            )
        with pytest.raises(ValueError, match="must be unique"):
            WriterContextEvidenceItem(
                item_id=StableId("item.dup"),
                section=WriterContextSection.CURRENT_WORLD_STATE,
                need_ids=(need.need_id,),
                need_facet_ids=(),
                purpose="p",
                evidence_ledger_ids=(StableId("ledger.x"), StableId("ledger.x")),
                raw_preview="x",
            )
        with pytest.raises(ValueError, match="cannot exceed its writer budget"):
            WriterContextBudgetReportV2(
                tokenizer="t",
                tokenizer_version="v",
                configured_writer_token_budget=10,
                actual_rendered_writer_tokens=11,
                configured_ledger_token_budget=100,
                actual_rendered_ledger_tokens=0,
                item_count=1,
                evidence_item_count=1,
                gap_item_count=0,
                ledger_entry_count=1,
                final_status=ContextAssemblyStatus.READY,
            )
        with pytest.raises(ValueError, match="cannot exceed its ledger budget"):
            WriterContextBudgetReportV2(
                tokenizer="t",
                tokenizer_version="v",
                configured_writer_token_budget=100,
                actual_rendered_writer_tokens=0,
                configured_ledger_token_budget=10,
                actual_rendered_ledger_tokens=11,
                item_count=1,
                evidence_item_count=1,
                gap_item_count=0,
                ledger_entry_count=1,
                final_status=ContextAssemblyStatus.READY,
            )
        with pytest.raises(ValueError, match="ledger manifest ref must match"):
            manifest = package_manifest_ok()
            EvidenceFirstPackageManifest.model_validate_json(
                json.dumps(
                    {
                        **manifest.model_dump(mode="json"),
                        "evidence_ledger_hash": ArtifactId("sha256:" + "d" * 64).root,
                    }
                )
            )


def package_manifest_ok() -> Any:
    from novel_agent.domain.artifacts import ArtifactRef
    from novel_agent.domain.ids import SchemaVersion
    from novel_agent.domain.writer_context import EvidenceFirstPackageManifest

    payload = canonical_json_bytes({"m": 1})
    ref = ArtifactRef(
        artifact_id=sha256_id(payload),
        media_type="application/json",
        byte_length=len(payload),
        schema_version=SchemaVersion("1.0.0"),
    )
    return EvidenceFirstPackageManifest(
        manifest_id=StableId("manifest.ok"),
        experiment_id="e",
        case_id=StableId("case.x"),
        checkpoint_chapter=5,
        basis_commit_id=COMMIT,
        basis_snapshot_id=SNAPSHOT,
        assembler_version="v",
        run_config_hash=ArtifactId("sha256:" + "f" * 64),
        package_artifact_ref=ref,
        evidence_ledger_ref=ref,
        package_hash=ref.artifact_id,
        evidence_ledger_hash=ref.artifact_id,
        generated_at="2026-08-11T00:00:00+00:00",
        writer_token_budget=4000,
        evidence_ledger_token_budget=12000,
        need_count=0,
        item_count=0,
        gap_count=0,
        ledger_entry_count=0,
        ledger_tokens=0,
        future_leakage_count=0,
        budget_status="READY",
        assembly_status="READY",
        mandatory_facet_closure="COMPLETE",
    )


class TestEvidenceFirstBranches2:
    def test_seen_span_dedupe_skips_worse_rank(self) -> None:
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = TestEvidenceFirstWriterContextAssembler()._slice(text_root)
        need = _need()
        good = SliceSelectionTrace(
            slice_id=slice_.slice_id,
            unit_id=StableId("unit.good"),
            route_channel="anchor_dense",
            fused_rank=1,
            selection_reason="good",
            evidence_ref=None,
        )
        worse = SliceSelectionTrace(
            slice_id=slice_.slice_id,
            unit_id=StableId("unit.worse"),
            route_channel="anchor_bm25",
            fused_rank=3,
            selection_reason="worse",
            evidence_ref=None,
        )
        assembler = EvidenceFirstWriterContextAssembler()
        result = assembler.assemble(
            task=_task(),
            selections=(
                NeedEvidenceSelection(need=need, selections=(good, worse), slices=(slice_,)),
            ),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
        )
        assert "anchor_dense" in result.package.items[0].selection_reason

    def test_writer_budget_overflow_status_branch(self) -> None:
        text_root = _text_root((_block("第一段。\n第二段,陈长生伤势未愈。\n第三段。"),))
        slice_ = TestEvidenceFirstWriterContextAssembler()._slice(text_root)
        assembler = EvidenceFirstWriterContextAssembler()
        result = assembler.assemble(
            task=_task(),
            selections=(
                _selection(_need("need.test.overflow", query="非常非常长的问题" * 40), slice_),
            ),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
            writer_token_budget=20,
        )
        assert result.status is ContextAssemblyStatus.CONTEXT_BUDGET_INSUFFICIENT
        assert any(
            code.startswith("EVIDENCE_ITEMS_EXCEED_WRITER_BUDGET")
            for code in result.diagnostic_codes
        )


class TestEvidenceSliceWindowBranches:
    def test_window_condition_exit_and_right_exhausted(self) -> None:
        # the window fills exactly to the limit, then the while condition
        # exits naturally
        resolver = EvidenceSliceResolver(paragraph_char_limit=12, sentence_window_char_limit=12)
        block = _block("一二三四五六七八九十。七字句子。尾。再加一句。")
        slices = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert len(slices) == 1
        assert slices[0].slice_kind is EvidenceSliceKind.SENTENCE_WINDOW
        assert len(slices[0].text) <= 12

        # right side exhausts while the left side still has candidates
        resolver = EvidenceSliceResolver(paragraph_char_limit=19, sentence_window_char_limit=19)
        block = _block("一二三四。一二三。中心很长很长。一二三。")
        slices = resolver.resolve_block(
            block,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
        )
        assert len(slices) == 1
        window = slices[0]
        assert window.slice_kind is EvidenceSliceKind.SENTENCE_WINDOW
        assert block.text[window.start : window.end] == window.text

    def test_two_pass_packing_budget_exhaustion_in_pass_one(self) -> None:
        """A ledger budget that fits only the first Need exhausts pass 1 mid-cycle."""
        from novel_agent.domain.memory import (
            ExpectedClaimScope,
            FacetEvidenceRequirement,
            NeedCompletionSpec,
            NeedFacet,
            NeedFacetKind,
            NeedGapPolicy,
            NeedUncertaintyPolicy,
        )
        from novel_agent.domain.writer_context import EvidenceSlice

        text = "短句。" + "长" * 100 + "。"
        block = _block(text)
        text_root = _text_root((block,))
        # Build two exact slices of the same block with different lengths
        # directly: pass-1 round-robin must fit the short one and exhaust on
        # the long one.
        object_hash = sha256_id(block.text.encode("utf-8"))
        slice_a = EvidenceSlice(
            slice_id=StableId("slice.budget.a"),
            parent_block_id=block.block_id,
            chapter_id=block.chapter_id,
            start=0,
            end=len("短句。"),
            text="短句。",
            object_hash=object_hash,
            quote_hash=quote_hash("短句。"),
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
            slice_kind=EvidenceSliceKind.SENTENCE_WINDOW,
        )
        slice_b = EvidenceSlice(
            slice_id=StableId("slice.budget.b"),
            parent_block_id=block.block_id,
            chapter_id=block.chapter_id,
            start=len("短句。"),
            end=len(text),
            text=text[len("短句。") :],
            object_hash=object_hash,
            quote_hash=quote_hash(text[len("短句。") :]),
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scope="writer_safe",
            slice_kind=EvidenceSliceKind.SENTENCE_WINDOW,
        )
        assert EvidenceFirstWriterContextAssembler().count_tokens(slice_a.text) < (
            EvidenceFirstWriterContextAssembler().count_tokens(slice_b.text)
        )
        # Budget exactly one short slice: Need A fits, Need B exhausts pass 1.
        budget = EvidenceFirstWriterContextAssembler().count_tokens(slice_a.text)
        selections = []
        for index, need_id in enumerate(("need.test.budget.a", "need.test.budget.b")):
            need = _need(need_id)
            facet = NeedFacet(
                need_facet_id=StableId(f"need-facet.budget.{index}"),
                need_id=need.need_id,
                facet_kind=NeedFacetKind.CURRENT_STATE,
                expected_claim_scope=ExpectedClaimScope.CURRENT,
                derivation_refs=(need.need_id,),
                producer="test",
                producer_version="v1",
                information_scope="cutoff_safe",
            )
            spec = NeedCompletionSpec(
                need_id=need.need_id,
                required_need_facet_ids=(facet.need_facet_id,),
                irreducible_need_facet_ids=(facet.need_facet_id,),
                evidence_requirement_by_facet={
                    facet.need_facet_id.root: FacetEvidenceRequirement.TRACEABLE_CUTOFF_SOURCE
                },
                min_distinct_evidence_sources=1,
                min_distinct_chapters=1,
                uncertainty_policy=NeedUncertaintyPolicy.ALLOW_GAP_ONLY,
                gap_policy=NeedGapPolicy.FAIL_MANDATORY,
                producer="test",
                producer_version="v1",
            )
            need = need.model_copy(update={"need_facets": (facet,), "completion_spec": spec})
            slice_ = slice_a if index == 0 else slice_b
            selections.append(
                NeedEvidenceSelection(
                    need=need,
                    selections=(
                        SliceSelectionTrace(
                            slice_id=slice_.slice_id,
                            unit_id=StableId(f"unit.budget.{index}"),
                            route_channel="r1_exact",
                            fused_rank=1,
                            rerank_score=0.9,
                            selection_reason="test",
                            evidence_ref=None,
                            supported_facet_ids=(facet.need_facet_id,),
                        ),
                    ),
                    slices=(slice_,),
                    facet_receipts=(),
                )
            )
        result = EvidenceFirstWriterContextAssembler().assemble(
            task=_task(),
            selections=tuple(selections),
            text_root=text_root,
            basis_commit_id=COMMIT,
            basis_snapshot_id=SNAPSHOT,
            arm="A",
            evidence_ledger_token_budget=budget,
        )
        assert result.status is ContextAssemblyStatus.READY
        assert result.mandatory_facet_closure == "INCOMPLETE"
        assert len(result.package.items) == 2
        assert sum(item.gap is None for item in result.package.items) == 1
        assert sum(item.gap is not None for item in result.package.items) == 1
