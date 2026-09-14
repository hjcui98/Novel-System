from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.agents.plan_reviewer import PlanReviewerAgent, PlanReviewerInvocationError
from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import SchemaVersion
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.planning import (
    PlanReviewDraft,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import AgentMode
from novel_agent.services.artifacts import ArtifactRepository

VERSION = SchemaVersion("1.0.0")


class _NeverCalledRunner:
    def prepare(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("mechanical preflight must run before Reviewer.prepare")


def _target(repo: ArtifactRepository) -> ArtifactRef:
    payload = {
        "items": [
            {
                "item_id": "story.reveal.obligations",
                "kind": "obligation",
                "payload": {
                    "title": "缺少类型的义务",
                    "constraints": ["lock.example"],
                },
            }
        ]
    }
    return repo.put(
        json.dumps(payload, ensure_ascii=False).encode(),
        "application/vnd.novel-agent.plan-proposal+json",
        VERSION,
    )


def test_formal_reviewer_preflight_persists_host_draft_without_model_call(tmp_path) -> None:
    repo = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    target = _target(repo)
    agent = PlanReviewerAgent(cast(StructuredAgentRunner, _NeverCalledRunner()), repo)

    with pytest.raises(PlanReviewerInvocationError) as caught:
        asyncio.run(
            agent.review(
                version=VERSION,
                mode=AgentMode.STORY,
                target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                target_payload=repo.read_verified(target).decode(),
                target_artifact=target,
                trusted_source_artifacts=(),
                request=cast(ModelRequest, object()),
                base_commit=None,
            )
        )

    error = caught.value
    assert error.mechanical_preflight
    assert not error.citation_repairable
    assert error.model_call is None
    assert error.review_draft_ref is not None
    draft = PlanReviewDraft.model_validate_json(repo.read_verified(error.review_draft_ref))
    assert draft.decision is ReviewDecision.REVISE
    assert any(
        issue.kind is ReviewIssueKind.OBLIGATION_CONTRACT and issue.blocking
        for issue in draft.issues
    )
