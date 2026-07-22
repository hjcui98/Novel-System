"""Deterministic write-side Gold evaluation for Stage 1B replay."""

from __future__ import annotations

from novel_agent.domain.benchmark import (
    ReplayCaseManifest,
    ReplayGoldChange,
    ReplayMetricSet,
    ReplayStateCategory,
)
from novel_agent.domain.changes import ChangeOperation, ChangeOperationType, WorldRecordKind
from novel_agent.domain.replay import ContinuousReplayResult, ReplayChapterStatus
from novel_agent.domain.text import EvidenceRef


class ReplayEvaluator:
    def evaluate(
        self, manifest: ReplayCaseManifest, result: ContinuousReplayResult
    ) -> ReplayMetricSet:
        predicted = tuple(
            (chapter.chapter_index, operation)
            for chapter in result.chapter_results
            for operation in chapter.observed_changes.operations
        )
        gold_by_key = {self._gold_key(item): item for item in manifest.gold_changes}
        predicted_by_key = {
            key: operation
            for chapter, operation in predicted
            if (key := self._operation_key(chapter, operation)) is not None
        }
        matched = {
            key
            for key in set(gold_by_key).intersection(predicted_by_key)
            if self._record_matches(predicted_by_key[key], gold_by_key[key])
        }
        state_p, state_r, state_f1 = self._kind_scores(
            WorldRecordKind.STATE, gold_by_key, predicted_by_key, matched
        )
        event_f1 = self._kind_scores(WorldRecordKind.EVENT, gold_by_key, predicted_by_key, matched)[
            2
        ]
        relation_f1 = self._kind_scores(
            WorldRecordKind.RELATION, gold_by_key, predicted_by_key, matched
        )[2]
        obligation_f1 = self._kind_scores(
            WorldRecordKind.OBLIGATION, gold_by_key, predicted_by_key, matched
        )[2]
        wrong_targets = sum(
            key not in gold_by_key and any(key[:3] == gold_key[:3] for gold_key in gold_by_key)
            for key in predicted_by_key
        )
        accepted_predictions = tuple(
            (key, operation)
            for key, operation in predicted_by_key.items()
            if self._truth(operation) == "accepted_world_fact"
        )
        false_promotions = sum(
            key not in gold_by_key
            or key not in matched
            or gold_by_key[key].expected_record.get("truth_class")
            not in (None, "accepted_world_fact")
            for key, _ in accepted_predictions
        )
        critical = {self._gold_key(item) for item in manifest.gold_changes if item.critical}
        evidence_correct = sum(
            self._evidence_bound(predicted_by_key[key], gold_by_key[key]) for key in matched
        )
        state_replacements = {
            key: operation
            for key, operation in predicted_by_key.items()
            if key[1] == WorldRecordKind.STATE.value
            and operation.operation is ChangeOperationType.REPLACE
        }
        invalid_state_overwrites = sum(
            key in gold_by_key and key not in matched for key in state_replacements
        )
        checkpoint_metrics = self._checkpoint_metrics(manifest, result)
        orphan_evidence = sum(
            evidence.span is None
            for _, operation in predicted
            for evidence in operation.evidence_refs
        )
        first_pollution = result.first_pollution_chapter
        replay_end = max(
            (chapter.chapter_index for chapter in result.chapter_results), default=None
        )
        return ReplayMetricSet(
            state_delta_precision=state_p,
            state_delta_recall=state_r,
            state_delta_f1=state_f1,
            event_extraction_f1=event_f1,
            relation_delta_f1=relation_f1,
            plan_obligation_update_f1=obligation_f1,
            wrong_target_binding_rate=self._ratio(wrong_targets, len(predicted_by_key)),
            false_world_fact_promotion_rate=self._ratio(
                false_promotions, len(accepted_predictions)
            ),
            missed_critical_change_rate=self._ratio(len(critical - matched), len(critical)),
            invalid_state_overwrite_rate=self._ratio(
                invalid_state_overwrites, len(state_replacements)
            ),
            evidence_binding_accuracy=self._ratio(evidence_correct, len(matched)),
            commit_reject_rate=self._ratio(
                sum(
                    chapter.status is ReplayChapterStatus.BLOCKED_BY_VALIDATION
                    for chapter in result.chapter_results
                ),
                len(result.chapter_results),
            ),
            current_state_accuracy_by_chapter=checkpoint_metrics[0],
            cumulative_state_drift=checkpoint_metrics[1],
            wrong_item_ownership_count=checkpoint_metrics[2].get(ReplayStateCategory.OWNERSHIP),
            wrong_character_location_count=checkpoint_metrics[2].get(ReplayStateCategory.LOCATION),
            wrong_vital_or_injury_state_count=checkpoint_metrics[2].get(
                ReplayStateCategory.VITAL_OR_INJURY
            ),
            wrong_obligation_debt_count=checkpoint_metrics[2].get(ReplayStateCategory.OBLIGATION),
            orphan_evidence_ref_count=orphan_evidence,
            manual_repair_commit_count=sum(
                chapter.manual_repair for chapter in result.chapter_results
            ),
            first_pollution_chapter=first_pollution,
            pollution_propagation_depth=(
                replay_end - first_pollution + 1
                if first_pollution is not None
                and replay_end is not None
                and replay_end >= first_pollution
                else None
            ),
        )

    @classmethod
    def _checkpoint_metrics(
        cls,
        manifest: ReplayCaseManifest,
        result: ContinuousReplayResult,
    ) -> tuple[
        dict[int, float],
        tuple[float, ...],
        dict[ReplayStateCategory, int],
    ]:
        snapshots = {
            chapter.chapter_index: {
                (record.record_kind, record.target_id): record.record
                for record in chapter.materialized_records
            }
            for chapter in result.chapter_results
        }
        accuracies: dict[int, float] = {}
        wrong_by_category = {category: 0 for category in ReplayStateCategory}
        for checkpoint in manifest.state_checkpoints:
            snapshot = snapshots.get(checkpoint.chapter_index, {})
            correct = 0
            for expected in checkpoint.expected_records:
                actual = snapshot.get((expected.record_kind, expected.target_id))
                matches = actual is not None and cls._contains_expected(
                    actual, expected.expected_record
                )
                correct += matches
                if not matches:
                    wrong_by_category[expected.category] += 1
            accuracies[checkpoint.chapter_index] = cls._ratio(
                correct, len(checkpoint.expected_records)
            )
        if not manifest.state_checkpoints:
            return {}, (), {}
        return (
            accuracies,
            tuple(1.0 - accuracies[chapter] for chapter in sorted(accuracies)),
            wrong_by_category,
        )

    @staticmethod
    def _gold_key(item: ReplayGoldChange) -> tuple[int, str, str, str]:
        return (
            item.chapter_index,
            item.record_kind.value,
            item.operation.value,
            item.target_id.root,
        )

    @staticmethod
    def _operation_key(
        chapter: int, operation: ChangeOperation
    ) -> tuple[int, str, str, str] | None:
        payload = operation.payload
        if not isinstance(payload, dict):
            return None
        record_type = payload.get("record_type")
        if not isinstance(record_type, str):
            return None
        return (
            chapter,
            record_type,
            operation.operation.value,
            operation.target_id.root,
        )

    @staticmethod
    def _truth(operation: ChangeOperation) -> object:
        payload = operation.payload
        if not isinstance(payload, dict):
            return None
        record = payload.get("record")
        return record.get("truth_class") if isinstance(record, dict) else None

    @classmethod
    def _kind_scores(
        cls,
        kind: WorldRecordKind,
        gold: dict[tuple[int, str, str, str], ReplayGoldChange],
        predicted: dict[tuple[int, str, str, str], ChangeOperation],
        matched: set[tuple[int, str, str, str]],
    ) -> tuple[float, float, float]:
        gold_keys = {key for key in gold if key[1] == kind.value}
        predicted_keys = {key for key in predicted if key[1] == kind.value}
        correct = len(gold_keys.intersection(predicted_keys).intersection(matched))
        precision = cls._ratio(correct, len(predicted_keys))
        recall = cls._ratio(correct, len(gold_keys))
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return precision, recall, f1

    @classmethod
    def _record_matches(cls, operation: ChangeOperation, gold: ReplayGoldChange) -> bool:
        payload = operation.payload
        if not isinstance(payload, dict):
            return False
        record = payload.get("record")
        return isinstance(record, dict) and cls._contains_expected(record, gold.expected_record)

    @classmethod
    def _contains_expected(cls, actual: object, expected: object) -> bool:
        """Match an annotation subset without rewarding a correct target with wrong values."""
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(
                key in actual and cls._contains_expected(actual[key], value)
                for key, value in expected.items()
            )
        if isinstance(expected, list):
            return (
                isinstance(actual, list)
                and len(actual) == len(expected)
                and all(
                    cls._contains_expected(actual_item, expected_item)
                    for actual_item, expected_item in zip(actual, expected, strict=True)
                )
            )
        return actual == expected

    @staticmethod
    def _evidence_bound(operation: ChangeOperation, gold: ReplayGoldChange) -> bool:
        return any(
            ReplayEvaluator._overlaps(candidate, expected)
            for candidate in operation.evidence_refs
            for expected in gold.evidence_refs
        )

    @staticmethod
    def _overlaps(candidate: EvidenceRef, expected: EvidenceRef) -> bool:
        return (
            candidate.root_hash == expected.root_hash
            and candidate.span is not None
            and expected.span is not None
            and candidate.span.block_id == expected.span.block_id
            and candidate.span.start < expected.span.end
            and expected.span.start < candidate.span.end
        )

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 1.0
