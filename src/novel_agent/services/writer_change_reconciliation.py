"""Deterministic reconciliation of Writer declarations and Curator observations."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping

from novel_agent.domain.changes import ChangeOperationType, ObservedChangeSet
from novel_agent.domain.editorial import (
    CuratorChangeObservation,
    CuratorObservation,
    ReconciliationClass,
    ReconciliationComparison,
    ReconciliationResult,
)
from novel_agent.domain.generation import DeclaredMemoryHint, MemoryHintChangeKind
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.services.content_addressing import content_id


class ReconciliationError(ValueError):
    """The independent observation cannot be safely reconciled."""


class WriterChangeReconciliationService:
    """Match weak Writer hints to an independently bound Curator observation set."""

    def reconcile(
        self,
        draft_id: ArtifactId,
        writer_hints: Iterable[DeclaredMemoryHint],
        observation: CuratorObservation,
    ) -> ReconciliationResult:
        if observation.draft_id != draft_id:
            raise ReconciliationError("Curator observation belongs to another Draft")
        hints = tuple(writer_hints)
        comparisons: list[ReconciliationComparison] = []
        remaining = list(enumerate(observation.changes))
        matched_hint_indexes: set[int] = set()

        # First consume exact identity/value matches so a partial mismatch cannot steal an
        # observation that is an exact match for a later declaration.
        for hint_index, hint in enumerate(hints):
            exact_index = next(
                (index for index, item in remaining if _exact_match(hint, item)),
                None,
            )
            if exact_index is None:
                continue
            _, item = remaining.pop(exact_index)
            matched_hint_indexes.add(hint_index)
            comparisons.append(
                _comparison(
                    ReconciliationClass.MATCHED,
                    hint_index,
                    hint,
                    item,
                    "subject, change type, predicate, and value agree",
                )
            )

        for hint_index, hint in enumerate(hints):
            if hint_index in matched_hint_indexes:
                continue
            partial_index = next(
                (
                    index
                    for index, item in remaining
                    if _same_subject(hint, item) and _partial_identity(hint, item)
                ),
                None,
            )
            if partial_index is None:
                comparisons.append(
                    _comparison(
                        ReconciliationClass.DECLARED_ONLY,
                        hint_index,
                        hint,
                        None,
                        "Writer declared a change without a supporting observation",
                    )
                )
                continue
            _, item = remaining.pop(partial_index)
            comparisons.append(
                _comparison(
                    ReconciliationClass.MISMATCHED,
                    hint_index,
                    hint,
                    item,
                    _mismatch_reason(hint, item),
                )
            )

        for _, item in remaining:
            comparisons.append(
                _comparison(
                    ReconciliationClass.OBSERVED_ONLY,
                    None,
                    None,
                    item,
                    "Curator observed a change that Writer did not declare",
                )
            )

        comparisons.sort(key=_comparison_sort_key)
        return ReconciliationResult(
            result_id=_stable_id(
                "reconciliation",
                {
                    "draft_id": draft_id.root,
                    "writer_hints": [hint.model_dump(mode="json") for hint in hints],
                    "observation": observation.model_dump(mode="json"),
                },
            ),
            draft_id=draft_id,
            writer_hints=hints,
            curator_observation=observation,
            comparisons=tuple(comparisons),
        )

    @staticmethod
    def observation_from_change_set(
        draft_id: ArtifactId,
        changes: ObservedChangeSet,
    ) -> CuratorObservation:
        """Adapt the existing independent Curator ChangeSet without writing it anywhere."""

        observations = tuple(
            CuratorChangeObservation(
                observation_id=operation.operation_id,
                subject_hint=_subject_from_payload(operation.target_id, operation.payload),
                change_kind=_change_kind(operation.operation),
                predicate_hint=_optional_text(operation.payload, "predicate"),
                value_hint=_optional_value(operation.payload),
                target_id=operation.target_id,
            )
            for operation in changes.operations
        )
        return CuratorObservation(draft_id=draft_id, changes=observations)


def _exact_match(hint: DeclaredMemoryHint, observation: CuratorChangeObservation) -> bool:
    return (
        _same_subject(hint, observation)
        and _same_text(hint.predicate_hint, observation.predicate_hint)
        and hint.change_kind is observation.change_kind
        and _same_text(hint.value_hint, observation.value_hint)
    )


def _partial_identity(hint: DeclaredMemoryHint, observation: CuratorChangeObservation) -> bool:
    # Once the subject/object is the same, any disagreement in type, predicate, or result is a
    # mismatch rather than two unrelated changes.
    return True


def _same_subject(hint: DeclaredMemoryHint, observation: CuratorChangeObservation) -> bool:
    return _normalise(hint.subject_hint) == _normalise(observation.subject_hint)


def _same_text(left: str | None, right: str | None) -> bool:
    return (left is not None and right is not None and _normalise(left) == _normalise(right)) or (
        left is None and right is None
    )


def _normalise(value: str) -> str:
    normalised = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalised if character.isalnum())


def _mismatch_reason(
    hint: DeclaredMemoryHint,
    observation: CuratorChangeObservation,
) -> str:
    differences: list[str] = []
    if hint.change_kind is not observation.change_kind:
        differences.append("change type")
    if not _same_text(hint.predicate_hint, observation.predicate_hint):
        differences.append("predicate")
    if not _same_text(hint.value_hint, observation.value_hint):
        differences.append("value/object")
    return "Writer and Curator disagree on " + ", ".join(differences or ("the change",))


def _comparison(
    classification: ReconciliationClass,
    hint_index: int | None,
    hint: DeclaredMemoryHint | None,
    observation: CuratorChangeObservation | None,
    reason: str,
) -> ReconciliationComparison:
    return ReconciliationComparison(
        comparison_id=_stable_id(
            "reconciliation-comparison",
            {
                "classification": classification.value,
                "writer_hint_index": hint_index,
                "observation_id": (
                    observation.observation_id.root if observation is not None else None
                ),
                "reason": reason,
            },
        ),
        classification=classification,
        writer_hint_index=hint_index,
        observation_id=observation.observation_id if observation is not None else None,
        writer_hint=hint,
        observation=observation,
        reason=reason,
    )


def _comparison_sort_key(item: ReconciliationComparison) -> tuple[int, int, str]:
    order = {
        ReconciliationClass.MATCHED: 0,
        ReconciliationClass.MISMATCHED: 1,
        ReconciliationClass.DECLARED_ONLY: 2,
        ReconciliationClass.OBSERVED_ONLY: 3,
    }
    return (
        order[item.classification],
        item.writer_hint_index if item.writer_hint_index is not None else 10_000,
        item.observation_id.root if item.observation_id is not None else "",
    )


def _change_kind(operation: ChangeOperationType) -> MemoryHintChangeKind:
    return {
        ChangeOperationType.CREATE: MemoryHintChangeKind.ADD,
        ChangeOperationType.REPLACE: MemoryHintChangeKind.CHANGE,
        ChangeOperationType.RETIRE: MemoryHintChangeKind.END,
    }[operation]


def _subject_from_payload(target_id: StableId, payload: object) -> str:
    if isinstance(payload, Mapping):
        for key in ("subject_hint", "subject", "entity", "name"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return target_id.root


def _optional_text(payload: object, key: str) -> str | None:
    if isinstance(payload, Mapping):
        value = payload.get(key)
        return value if isinstance(value, str) and value else None
    return None


def _optional_value(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("value_hint", "value", "object", "state"):
        value = payload.get(key)
        if value is None:
            continue
        return str(value)
    return None


def _stable_id(prefix: str, value: object) -> StableId:
    digest = content_id(value).root.removeprefix("sha256:")
    return StableId(f"{prefix}.{digest}")


__all__ = [
    "ReconciliationError",
    "WriterChangeReconciliationService",
]
