"""Fail-closed Stage 1 BenchmarkBundle import and reference validation."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from novel_agent.domain.benchmark import (
    BenchmarkBundle,
    BenchmarkCaseManifest,
    ChapterSummaryRootDocument,
    PlanRootDocument,
    ReplayCaseManifest,
    TextRootDocument,
)
from novel_agent.domain.ids import ArtifactId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.text import EvidenceRef, TextBlock
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import (
    canonical_json_bytes,
    content_id,
    plan_root_content_id,
    quote_hash,
    summary_root_content_id,
    text_root_content_id,
    world_root_content_id,
)

__all__ = [
    "BenchmarkBundleImporter",
    "BenchmarkImportError",
    "bundle_content_id",
    "canonical_json_bytes",
    "content_id",
    "plan_root_content_id",
    "quote_hash",
    "summary_root_content_id",
    "text_root_content_id",
    "validate_evidence_ref",
    "world_root_content_id",
]


class BenchmarkImportError(ValueError):
    """Bundle is malformed, incomplete, inconsistent, or leaks future data."""


def bundle_content_id(bundle: BenchmarkBundle) -> ArtifactId:
    return content_id(bundle.model_dump(mode="json", exclude={"content_hash"}))


class BenchmarkBundleImporter:
    def load(self, path: Path) -> BenchmarkBundle:
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise BenchmarkImportError(f"cannot read benchmark bundle: {path}") from error
        try:
            bundle = BenchmarkBundle.model_validate_json(raw, strict=True)
        except ValidationError as error:
            raise BenchmarkImportError("benchmark bundle schema validation failed") from error
        self.validate(bundle)
        return bundle

    def validate(self, bundle: BenchmarkBundle) -> None:
        if bundle_content_id(bundle) != bundle.content_hash:
            raise BenchmarkImportError("benchmark bundle content hash mismatch")
        text_roots = {root.root_hash: root for root in bundle.text_roots}
        summaries = {root.root_hash: root for root in bundle.summary_roots}
        plans = {root.root_hash: root for root in bundle.plan_roots}
        worlds = {root.root_hash: root for root in bundle.world_roots}
        for text_root in bundle.text_roots:
            if text_root_content_id(text_root) != text_root.root_hash:
                raise BenchmarkImportError(
                    f"text root content hash mismatch: {text_root.root_hash.root}"
                )
        for plan_root in bundle.plan_roots:
            if plan_root_content_id(plan_root) != plan_root.root_hash:
                raise BenchmarkImportError(
                    f"plan root content hash mismatch: {plan_root.root_hash.root}"
                )
        for summary_root in bundle.summary_roots:
            if summary_root_content_id(summary_root) != summary_root.root_hash:
                raise BenchmarkImportError(
                    f"summary root content hash mismatch: {summary_root.root_hash.root}"
                )
            source = text_roots.get(summary_root.source_text_root)
            if source is None:
                raise BenchmarkImportError("summary root references a missing text root")
            blocks = self._blocks_by_id(source)
            chapters = {chapter.chapter_index: chapter for chapter in source.chapters}
            for summary in summary_root.summaries:
                chapter = chapters.get(summary.chapter_index)
                if chapter is None or chapter.chapter_id != summary.chapter_id:
                    raise BenchmarkImportError("chapter summary does not match its source chapter")
                for evidence in summary.evidence_refs:
                    self._validate_evidence(evidence, source, blocks)
        for world_root in bundle.world_roots:
            if world_root_content_id(world_root) != world_root.root_hash:
                raise BenchmarkImportError(
                    f"world root content hash mismatch: {world_root.root_hash.root}"
                )
        for case in bundle.case_manifests:
            self._validate_case(case, text_roots, summaries, plans, worlds)
        for replay in bundle.replay_manifests:
            self._validate_replay(replay, text_roots, worlds)

    def _validate_replay(
        self,
        replay: ReplayCaseManifest,
        text_roots: dict[ArtifactId, TextRootDocument],
        worlds: dict[ArtifactId, WorldRootDocument],
    ) -> None:
        root = text_roots.get(replay.target_text_root)
        if root is None or replay.initial_world_root not in worlds:
            raise BenchmarkImportError(
                f"replay {replay.replay_case_id.root} references a missing root"
            )
        available = {chapter.chapter_index for chapter in root.chapters}
        expected = set(range(replay.chapter_range[0], replay.chapter_range[1] + 1))
        if not expected.issubset(available):
            raise BenchmarkImportError(
                f"replay {replay.replay_case_id.root} chapters are incomplete"
            )
        blocks = self._blocks_by_id(root)
        for change in replay.gold_changes:
            for evidence in change.evidence_refs:
                self._validate_evidence(evidence, root, blocks)

    def _validate_case(
        self,
        case: BenchmarkCaseManifest,
        text_roots: dict[ArtifactId, TextRootDocument],
        summaries: dict[ArtifactId, ChapterSummaryRootDocument],
        plans: dict[ArtifactId, PlanRootDocument],
        worlds: dict[ArtifactId, WorldRootDocument],
    ) -> None:
        history = text_roots.get(case.input_text_root)
        future = text_roots.get(case.future_text_root_private)
        if history is None or future is None:
            raise BenchmarkImportError(f"case {case.case_id.root} references a missing text root")
        summary_root = (
            summaries.get(case.input_summary_root) if case.input_summary_root is not None else None
        )
        if case.input_summary_root is not None and summary_root is None:
            raise BenchmarkImportError(
                f"case {case.case_id.root} references a missing summary root"
            )
        if summary_root is not None and summary_root.source_text_root != history.root_hash:
            raise BenchmarkImportError("case summary root does not describe its history root")
        history_indexes = {chapter.chapter_index for chapter in history.chapters}
        future_indexes = {chapter.chapter_index for chapter in future.chapters}
        expected_history = set(range(case.history_range[0], case.history_range[1] + 1))
        expected_targets = set(range(case.target_range[0], case.target_range[1] + 1))
        if not expected_history.issubset(history_indexes):
            raise BenchmarkImportError(f"case {case.case_id.root} history chapters are incomplete")
        required_summary_indexes = set(
            range(case.history_range[0], max(case.history_range[0], case.history_range[1] - 2))
        )
        available_summary_indexes = (
            {summary.chapter_index for summary in summary_root.summaries}
            if summary_root is not None
            else set()
        )
        if case.gate_eligible and not required_summary_indexes.issubset(available_summary_indexes):
            raise BenchmarkImportError(
                f"case {case.case_id.root} lacks chapter summaries required by B1"
            )
        if any(index not in expected_history for index in available_summary_indexes):
            raise BenchmarkImportError("case summary root includes a chapter outside history")
        if history_indexes.intersection(expected_targets):
            raise BenchmarkImportError(
                f"case {case.case_id.root} history root leaks target chapters"
            )
        if not expected_targets.issubset(future_indexes):
            raise BenchmarkImportError(f"case {case.case_id.root} future chapters are incomplete")
        if case.input_plan_root is not None:
            plan = plans.get(case.input_plan_root)
            if plan is None:
                raise BenchmarkImportError(
                    f"case {case.case_id.root} references a missing plan root"
                )
            goal_ids = {goal.goal_id for goal in plan.chapter_goals}
            if not set(case.chapter_goal_ids).issubset(goal_ids):
                raise BenchmarkImportError(
                    f"case {case.case_id.root} references a missing chapter goal"
                )
            goals = {goal.goal_id: goal for goal in plan.chapter_goals}
            for item in case.plan_obligation_gold:
                for evidence in item.plan_evidence_refs:
                    if evidence.plan_root_hash != plan.root_hash:
                        raise BenchmarkImportError("plan Gold evidence references another PlanRoot")
                    goal = goals.get(evidence.goal_id)
                    if goal is None:
                        raise BenchmarkImportError("plan Gold evidence references a missing goal")
                    if evidence.object_hash != content_id(goal.model_dump(mode="json")):
                        raise BenchmarkImportError("plan Gold evidence goal hash mismatch")
        elif any(item.plan_evidence_refs for item in case.plan_obligation_gold):
            raise BenchmarkImportError("plan Gold evidence requires an input PlanRoot")
        if (
            case.input_world_root_verified is not None
            and case.input_world_root_verified not in worlds
        ):
            raise BenchmarkImportError(f"case {case.case_id.root} references a missing world root")
        history_blocks = self._blocks_by_id(history)
        future_blocks = self._blocks_by_id(future)
        for item in (
            *case.observed_use_gold,
            *case.operational_constraint_gold,
            *case.plan_obligation_gold,
        ):
            for history_evidence in item.evidence_refs:
                self._validate_evidence(history_evidence, history, history_blocks)
            for future_evidence in item.future_evidence_refs:
                self._validate_evidence(future_evidence, future, future_blocks)

    @staticmethod
    def _blocks_by_id(root: TextRootDocument) -> dict[str, TextBlock]:
        return {
            block.block_id.root: block
            for scene in (
                *(root.prelude.scenes if root.prelude is not None else ()),
                *(scene for chapter in root.chapters for scene in chapter.scenes),
            )
            for block in scene.blocks
        }

    @staticmethod
    def _validate_evidence(
        evidence: EvidenceRef,
        root: TextRootDocument,
        blocks: dict[str, TextBlock],
    ) -> None:
        if evidence.root_hash != root.root_hash or evidence.span is None:
            raise BenchmarkImportError("gold evidence must resolve to its declared text root")
        block = blocks.get(evidence.span.block_id.root)
        if block is None:
            raise BenchmarkImportError("gold evidence references a missing block")
        if evidence.chapter_id != block.chapter_id or evidence.scene_id != block.scene_id:
            raise BenchmarkImportError("gold evidence chapter or scene does not match its block")
        if evidence.object_hash != sha256_id(block.text.encode("utf-8")):
            raise BenchmarkImportError("gold evidence object hash does not match block text")
        if evidence.span.end > len(block.text):
            raise BenchmarkImportError("gold evidence codepoint span exceeds block length")
        selected = block.text[evidence.span.start : evidence.span.end]
        if evidence.quote_hash != quote_hash(selected):
            raise BenchmarkImportError("gold evidence quote hash mismatch")


def validate_evidence_ref(evidence: EvidenceRef, root: TextRootDocument) -> None:
    """Validate one EvidenceRef against a concrete immutable TextRoot."""
    BenchmarkBundleImporter._validate_evidence(
        evidence,
        root,
        BenchmarkBundleImporter._blocks_by_id(root),
    )
