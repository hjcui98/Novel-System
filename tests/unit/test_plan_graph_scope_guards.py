"""R2: plan-graph guards for cycles, scope overlap and parent scope.

A rolling plan replaces declared ranges.  Without these guards a later proposal could
re-own an already covered chapter, extend past its parent, or form a parent cycle that
makes ancestor projection undefined.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_agent.domain.benchmark import PlanRootDocument
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.world import PlanLevel, PlanNode

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "1" * 64)


def _node(
    node_id: str,
    *,
    level: PlanLevel,
    start: int | None = None,
    end: int | None = None,
    parent: str | None = None,
) -> PlanNode:
    return PlanNode(
        plan_node_id=StableId(node_id),
        node_type=level.value,
        title=node_id,
        summary="node",
        parent_id=StableId(parent) if parent else None,
        plan_level=level,
        chapter_start=start,
        chapter_end=end,
    )


def _plan(*nodes: PlanNode) -> PlanRootDocument:
    return PlanRootDocument(root_hash=HASH, schema_version=VERSION, nodes=nodes)


def test_parent_cycle_is_rejected() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        _plan(
            _node("node.a", level=PlanLevel.ARC_VOLUME, start=1, end=100, parent="node.b"),
            _node("node.b", level=PlanLevel.ARC_VOLUME, start=1, end=100, parent="node.a"),
        )


def test_self_parent_is_rejected() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        _plan(_node("node.a", level=PlanLevel.ARC_VOLUME, start=1, end=100, parent="node.a"))


def test_overlapping_siblings_at_one_level_are_rejected() -> None:
    with pytest.raises(ValidationError, match="scope overlap"):
        _plan(
            _node("node.v1", level=PlanLevel.ARC_VOLUME, start=1, end=150),
            _node("node.v2", level=PlanLevel.ARC_VOLUME, start=101, end=200),
        )


def test_adjacent_siblings_are_allowed() -> None:
    plan = _plan(
        _node("node.v1", level=PlanLevel.ARC_VOLUME, start=1, end=100),
        _node("node.v2", level=PlanLevel.ARC_VOLUME, start=101, end=200),
    )

    assert len(plan.nodes) == 2


def test_child_outside_parent_scope_is_rejected() -> None:
    with pytest.raises(ValidationError, match="outside its parent"):
        _plan(
            _node("node.v1", level=PlanLevel.ARC_VOLUME, start=1, end=100),
            _node(
                "node.v1.set",
                level=PlanLevel.CHAPTER_SET,
                start=90,
                end=120,
                parent="node.v1",
            ),
        )


def test_child_inside_parent_scope_is_allowed() -> None:
    plan = _plan(
        _node("node.v1", level=PlanLevel.ARC_VOLUME, start=1, end=100),
        _node(
            "node.v1.set",
            level=PlanLevel.CHAPTER_SET,
            start=1,
            end=5,
            parent="node.v1",
        ),
    )

    assert len(plan.nodes) == 2


def test_same_range_at_different_levels_is_not_an_overlap() -> None:
    plan = _plan(
        _node("node.v1", level=PlanLevel.ARC_VOLUME, start=1, end=100),
        _node("node.v1.set", level=PlanLevel.CHAPTER_SET, start=1, end=100, parent="node.v1"),
    )

    assert len(plan.nodes) == 2
