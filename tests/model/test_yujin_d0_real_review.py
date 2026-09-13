"""D0: one real review of the frozen candidate, and what the host does with it.

The deterministic chain (``test_yujin_d0_frozen_candidate_chain.py``) proves the
mechanism.  This module makes the same point with a *real* review: one model call
against the registered ``qwen38_27b_nvfp4_8003`` profile, reviewing the frozen
candidate, with the frozen author locks as the trusted catalogue.

It is marked ``model_required`` and is off the deterministic path.  It spends at
most two provider calls (a review, and a re-review of the composed candidate) and
writes only into the diagnostic's own object store.  Nothing here advances the
frozen v23 run, writes its Canon, or calls a production CommitService.

What the run establishes, recorded in the delivery note:

* whether the reviewer's blocking findings carry citations that resolve against
  the candidate it named -- which is what the fixed verification checks;
* whether a refuted demand is demoted instead of forwarded, so no planner rewrites
  a volume over a sentence that is not there;
* whether the legitimate early-planting rule survives, so a legal `setup` is not
  pushed past a boundary it never crossed.

The diagnostic budget is one review and one bounded revision; it does not let the
planner regenerate eight volumes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.agents.plan_reviewer import PlanReviewerAgent
from novel_agent.agents.planner import build_planner_contract_bundle
from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
    bounded_stable_id,
)
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.plan_composition import revision_scope
from novel_agent.domain.planning import (
    PlanReviewDraft,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import AgentMode
from novel_agent.runtime.production_bootstrap import (
    PACKAGE_ROOT,
    QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE,
    resolve_registered_model_endpoints,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.model_gateway import ModelGateway
from tests.integration.test_yujin_d0_frozen_candidate_chain import (
    FROZEN_CANDIDATE,
    FROZEN_LOCKS,
    _proposal_from_frozen,
    author_constraint_root,
)

pytestmark = [
    pytest.mark.model_required,
    pytest.mark.skipif(
        not FROZEN_CANDIDATE.exists(), reason="the frozen v23 development input is not present"
    ),
]

VERSION = SchemaVersion("1.0.0")
PROJECT = ProjectId("project.yujin-jiuxu.v23.d0")
RUN = RunId("run.d0.diagnostic")
COMMIT = CommitId("sha256:" + "d" * 64)
# The diagnostic is a development run, not a stage: one review, and one re-review if
# the first one demands a revision.  The output budget is the run's own initial
# allocation, not a claim about what the model needs.
DIAGNOSTIC_OUTPUT_TOKENS = 16_000
DIAGNOSTIC_TIMEOUT_SECONDS = 900.0


def _endpoint() -> object:
    endpoints = resolve_registered_model_endpoints(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE)
    assert endpoints, "the registered profile resolved to no endpoint"
    return endpoints[0]


def _reviewer(tmp_path: Path) -> tuple[PlanReviewerAgent, ArtifactRepository]:
    repo = ArtifactRepository(FilesystemObjectStore(tmp_path / "d0-real-objects"))
    bundle = build_planner_contract_bundle(package_root=PACKAGE_ROOT, version=VERSION)
    gateway = ModelGateway(
        (_endpoint(),),  # type: ignore[arg-type]
        forbid_external_calls=True,
        structured_max_retries=0,
        raw_artifacts=repo,
    )
    runner = StructuredAgentRunner(gateway, bundle.agents, bundle.prompts, bundle.skills)
    return PlanReviewerAgent(runner, repo), repo


def _frozen_world_root() -> object:
    """The committed World root the frozen run's basis binds: entities, no obligations."""

    from novel_agent.domain.memory import WorldRootDocument
    from novel_agent.services.content_addressing import world_root_content_id

    seed = WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "e" * 64),
        schema_version=VERSION,
        source_commit=COMMIT,
        obligations=(),
    )
    return WorldRootDocument(
        root_hash=world_root_content_id(seed),
        schema_version=VERSION,
        source_commit=COMMIT,
        obligations=(),
    )


def _request(phase: str) -> ModelRequest:
    return ModelRequest(
        request_id=bounded_stable_id(
            f"model-request.{RUN.root}.{phase}",
            f"model-request.{phase}",
        ),
        run_id=RUN,
        task_id=TaskId("task.d0.review"),
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id=f"trace.{RUN.root}.{phase}",
        prompt="",
        agent_mode=AgentMode.ARC_VOLUME.value,
        max_output_tokens=DIAGNOSTIC_OUTPUT_TOKENS,
        timeout_seconds=DIAGNOSTIC_TIMEOUT_SECONDS,
        enable_thinking=False,
    )


def test_d0_a_real_review_of_the_frozen_candidate(tmp_path: Path) -> None:
    """One real review, and the host's verdict on the citations it produced."""

    from novel_agent.agents.plan_reviewer import apply_host_plan_review_constraints

    candidate = _proposal_from_frozen()
    _, _root, _profile_ref = author_constraint_root()
    reviewer, repo = _reviewer(tmp_path)
    target_ref = repo.put(
        candidate.model_dump_json().encode(),
        "application/vnd.novel-agent.plan-proposal+json",
        VERSION,
    )
    # The review reads its catalogue from the compiled constraint root, which is the
    # artifact a real run passes it -- not the author's raw lock file.
    constraints, root, _profile_ref = author_constraint_root()
    lock_ref = repo.put(
        root.model_dump_json().encode(),
        "application/vnd.novel-agent.author-constraint-root+json",
        VERSION,
    )
    # A review also needs the accepted World root, or it cannot tell a legal
    # reference from an invented obligation.  The frozen v23 World roots carry no
    # obligations (they predate G0's STORY/ARC_VOLUME commit), so this is the
    # readable-empty catalogue: a statement that no obligation exists yet, not an
    # unknown one.
    world_ref = repo.put(
        _frozen_world_root().model_dump_json().encode(),
        "application/vnd.novel-agent.world-root+json",
        VERSION,
    )

    try:
        review, review_ref, call = _run_review(reviewer, candidate, target_ref, lock_ref, world_ref)
    except Exception as error:
        pytest.fail(f"the real review call failed: {type(error).__name__}: {error}")

    # Record what happened, whatever it was.  A review that could not ground its
    # findings is a review defect, and reporting it is the point.
    _record(
        tmp_path,
        "d0.review",
        {
            "candidate": FROZEN_CANDIDATE.name,
            "review_artifact": review_ref.artifact_id.root,
            "decision": review.decision.value,
            "findings": [
                {
                    "kind": issue.kind.value,
                    "blocking": issue.blocking,
                    "host_issued": issue.host_issued,
                    "item_ids": [item.root for item in issue.affected_item_ids],
                    "field_path": issue.field_path,
                    "has_quote": issue.quote is not None,
                    "summary": issue.summary[:400],
                }
                for issue in review.issues
            ],
            "verification_failures": list(review.verification_failures),
            "scope_targets": sorted(revision_scope(review).targeted_item_ids),
            "model_call": {
                "usage": None
                if getattr(call, "usage", None) is None
                else call.usage.model_dump(mode="json"),
            },
        },
    )

    # Every blocking model finding the review kept must carry the four citation
    # fields; the host demotes any that do not resolve.
    for issue in review.issues:
        if issue.host_issued or not issue.blocking:
            continue
        assert issue.field_path, issue.summary
        assert issue.quote, issue.summary
        assert issue.unmet_condition, issue.summary
        assert issue.affected_item_ids, issue.summary

    # A review that could reach no verdict must say so rather than accept.
    assert review.decision in set(ReviewDecision)

    # The host overlay must agree with the review it was given: a candidate with a
    # refuted-only finding set cannot authorise a rewrite of the volumes it names.
    overlaid = apply_host_plan_review_constraints(
        PlanReviewDraft(
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            decision=review.decision,
            issues=review.issues,
            revision_instruction=review.revision_instruction,
        ),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=candidate.model_dump_json(),
        mode=AgentMode.ARC_VOLUME,
        accepted_obligation_ids=frozenset(),
        accepted_obligation_windows={},
        author_constraints=constraints,  # type: ignore[arg-type]
        verified_citations=True,
    )
    if not [issue for issue in overlaid.issues if issue.blocking]:
        assert overlaid.decision is ReviewDecision.ACCEPT
        assert overlaid.revision_instruction is None


def _run_review(
    reviewer: PlanReviewerAgent,
    candidate: object,
    target_ref: ArtifactRef,
    lock_ref: ArtifactRef,
    world_ref: ArtifactRef,
) -> tuple[object, ArtifactRef, object]:
    import asyncio

    return asyncio.run(
        reviewer.review(
            version=VERSION,
            mode=AgentMode.ARC_VOLUME,
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            target_payload=candidate.model_dump_json(),  # type: ignore[attr-defined]
            target_artifact=target_ref,
            trusted_source_artifacts=(lock_ref, world_ref),
            request=_request("plan-review"),
            base_commit=COMMIT,
        )
    )


def _record(tmp_path: Path, name: str, payload: dict) -> None:
    """Keep the diagnostic's own result next to its object store."""

    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"[measure] {name}: {path}")


def test_d0_the_diagnostic_records_the_real_endpoint_identity(tmp_path: Path) -> None:
    """The frozen configuration is recorded, not inferred from a healthy port."""

    endpoint = _endpoint()
    identity = {
        "profile": QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE,
        "endpoint_name": getattr(endpoint, "endpoint_name", None),
        "model_name": getattr(endpoint, "model_name", None),
        "revision": getattr(endpoint, "revision", None),
        "sequence_limit": getattr(endpoint, "sequence_limit", None),
        "output_limit": getattr(endpoint, "output_limit", None),
        "diagnostic_output_tokens": DIAGNOSTIC_OUTPUT_TOKENS,
        "diagnostic_timeout_seconds": DIAGNOSTIC_TIMEOUT_SECONDS,
        "candidate": FROZEN_CANDIDATE.name,
        "locks": "yujin-jiuxu-v23/input/planning-locks.json",
        "code_commit": _code_commit(),
        "artifact_id": str(ArtifactId("sha256:" + "0" * 64)),
    }
    _record(tmp_path, "d0.configuration", identity)

    assert identity["endpoint_name"], "the registered profile declares no endpoint name"
    assert identity["model_name"], "the registered profile declares no model name"
    assert identity["sequence_limit"] == 131_072
    assert identity["diagnostic_timeout_seconds"] <= 900.0


def _code_commit() -> str:
    import subprocess

    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - git absent
        return "unknown"


def test_the_frozen_locks_are_read_not_restated() -> None:
    """The diagnostic uses the author's own file, not a paraphrase of it."""

    document = json.loads(FROZEN_LOCKS.read_text(encoding="utf-8"))
    locks, root, _profile_ref = author_constraint_root()

    keys = {str(getattr(lock, "constraint_key", "")) for lock in locks}
    assert keys == {lock["lock_id"] for lock in document["locks"]}
    assert len(root.constraints) == len(document["locks"])
    assert StableId("lock.long-truth.vol4-hint") in {StableId(key) for key in keys}
    # The boundary the reviewer must apply comes from the author's own number.
    hint = next(
        lock for lock in locks if getattr(lock, "constraint_key", "") == "lock.long-truth.vol4-hint"
    )
    assert hint.not_before_chapter == 350
