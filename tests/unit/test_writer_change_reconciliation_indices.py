"""R4.6: Writer/Curator reconciliation must not mis-match after a removal.

``reconcile`` pops matched observations out of its working list.  The previous
implementation enumerated the list once up front and then popped by position, so after
the first removal every stored position was stale and a later declaration could consume
the wrong observation - or fail to find its own exact match.
"""

from __future__ import annotations

from novel_agent.domain.editorial import (
    CuratorChangeObservation,
    CuratorObservation,
    ReconciliationClass,
)
from novel_agent.domain.generation import DeclaredMemoryHint, MemoryHintChangeKind
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.services.writer_change_reconciliation import (
    WriterChangeReconciliationService,
)

DRAFT = ArtifactId("sha256:" + "a" * 64)


def _hint(subject: str, predicate: str, value: str) -> DeclaredMemoryHint:
    return DeclaredMemoryHint(
        subject_hint=subject,
        change_kind=MemoryHintChangeKind.ADD,
        predicate_hint=predicate,
        value_hint=value,
        evidence_quote=f"{subject}{predicate}{value}",
        confidence=0.9,
    )


def _observation(index: int, subject: str, predicate: str, value: str):
    return CuratorChangeObservation(
        observation_id=StableId(f"observation.{index}"),
        subject_hint=subject,
        change_kind=MemoryHintChangeKind.ADD,
        predicate_hint=predicate,
        value_hint=value,
    )


def test_reverse_order_exact_matches_stay_bound_to_their_declaration() -> None:
    """Declarations are the reverse of the observation order, so every pop shifts."""

    observations = tuple(
        _observation(index, f"subject{index}", f"predicate{index}", f"value{index}")
        for index in range(4)
    )
    hints = tuple(
        _hint(f"subject{index}", f"predicate{index}", f"value{index}")
        for index in reversed(range(4))
    )

    result = WriterChangeReconciliationService().reconcile(
        DRAFT,
        hints,
        CuratorObservation(draft_id=DRAFT, changes=observations),
    )

    assert len(result.comparisons) == 4
    assert all(
        comparison.classification is ReconciliationClass.MATCHED
        for comparison in result.comparisons
    )
    subjects = {comparison.observation.subject_hint for comparison in result.comparisons}
    assert subjects == {f"subject{index}" for index in range(4)}


def test_partial_match_after_an_exact_pop_keeps_a_distinct_observation() -> None:
    """A partial mismatch must consume its own subject, not the shifted neighbour."""

    observations = (
        _observation(0, "alpha", "holds", "铜铭"),
        _observation(1, "beta", "holds", "银铭"),
    )
    hints = (
        _hint("beta", "holds", "铜铭"),
        _hint("alpha", "holds", "铜铭"),
    )

    result = WriterChangeReconciliationService().reconcile(
        DRAFT,
        hints,
        CuratorObservation(draft_id=DRAFT, changes=observations),
    )

    by_subject = {
        comparison.observation.subject_hint: comparison.classification
        for comparison in result.comparisons
    }
    assert by_subject["alpha"] is ReconciliationClass.MATCHED
    assert by_subject["beta"] is ReconciliationClass.MISMATCHED


def test_unmatched_observations_remain_reported_after_pops() -> None:
    observations = tuple(
        _observation(index, f"subject{index}", f"predicate{index}", f"value{index}")
        for index in range(3)
    )
    hints = (_hint("subject2", "predicate2", "value2"),)

    result = WriterChangeReconciliationService().reconcile(
        DRAFT,
        hints,
        CuratorObservation(draft_id=DRAFT, changes=observations),
    )

    observed_only = [
        comparison
        for comparison in result.comparisons
        if comparison.classification is ReconciliationClass.OBSERVED_ONLY
    ]
    assert {item.observation.subject_hint for item in observed_only} == {
        "subject0",
        "subject1",
    }
