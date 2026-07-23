from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from novel_agent.agents.curator_bootstrap import (
    CuratorBootstrapAgent,
    CuratorBootstrapInvocationError,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, ProjectId, StableId
from novel_agent.domain.stage2 import (
    CuratorBootstrapDraft,
    ProposalProvenance,
    ProposedItem,
)
from tests.unit.test_stage2_curator_agent import VERSION, request

HASH = ArtifactId("sha256:" + "a" * 64)


def artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=HASH,
        media_type="text/plain",
        byte_length=1,
        schema_version=VERSION,
    )


def invoke(
    agent: CuratorBootstrapAgent,
    source_ids: tuple[StableId, ...],
    source_artifacts: tuple[ArtifactRef, ...],
) -> None:
    asyncio.run(
        agent.run(
            version=VERSION,
            project_id=ProjectId("project.bootstrap"),
            source_ids=source_ids,
            source_payload="source",
            source_artifacts=source_artifacts,
            request=request(),
        )
    )


def test_curator_bootstrap_requires_one_unique_artifact_per_source() -> None:
    agent = CuratorBootstrapAgent(cast(Any, object()), cast(Any, object()))
    with pytest.raises(CuratorBootstrapInvocationError, match="unique artifact bindings"):
        invoke(agent, (StableId("source.one"),), ())
    with pytest.raises(CuratorBootstrapInvocationError, match="unique artifact bindings"):
        invoke(
            agent,
            (StableId("source.one"), StableId("source.two")),
            (artifact(), artifact()),
        )


def test_curator_bootstrap_rejects_author_citation_outside_task() -> None:
    class Runner:
        def prepare(self, *_: Any, **__: Any) -> object:
            return object()

        async def execute(self, *_: Any, **__: Any) -> Any:
            return SimpleNamespace(
                output=CuratorBootstrapDraft(
                    items=(
                        ProposedItem(
                            item_id=StableId("item.foreign"),
                            kind="fact",
                            payload={"value": "foreign"},
                            provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                            source_ids=(StableId("source.foreign"),),
                        ),
                    ),
                    extraction_coverage=1.0,
                )
            )

    agent = CuratorBootstrapAgent(cast(Any, Runner()), cast(Any, object()))
    with pytest.raises(CuratorBootstrapInvocationError, match="outside the task"):
        invoke(agent, (StableId("source.one"),), (artifact(),))
