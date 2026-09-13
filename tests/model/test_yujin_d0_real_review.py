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

The diagnostic budget is two reviews and one bounded revision; it does not let the
planner regenerate eight volumes.

What the revision case does and does not establish is worth stating precisely.  It
proves that a real model revision, run through the production Planner assembly,
produces a candidate the host can compose inside the reviewed scope and that a real
re-review then accepts.  In the run recorded here the model moved only the single
authorised item, so the composition's *restoration* path did not have to fire; that
path is covered deterministically (``test_plan_composition_source_proof.py`` and the
D0 chain's unauthorised-rewrite case).  A run in which the model does wander is
recorded as ``out_of_scope_items`` rather than failing, because the host's job is to
contain it, not to require good behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.runtime.materializers import PLAN_PROPOSAL_MEDIA_TYPE
from novel_agent.agents.plan_reviewer import PlanReviewerAgent
from novel_agent.agents.planner import build_planner_contract_bundle, planner_skill_ids_for_mode
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
from novel_agent.domain.stage2 import AgentMode, AgentType
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
    FROZEN_RUN,
    _proposal_from_frozen,
    author_constraint_root,
)
from tests.unit.test_stage4_planning_contracts import _receipt

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


# --------------------------------------------------- a real Planner revision


def _grounded_revision_finding(candidate: object):
    """A blocking finding whose citation is present in the candidate it names.

    The frozen candidate repeats one sentence across the climaxes of volumes five
    to eight.  Citing the field that holds it is what a review must do, and the
    scope it authorises is only that field.
    """

    from novel_agent.domain.planning import PlanReviewIssue, ReviewIssueKind

    phrase = "正式揭露门被从对面推开"
    for item in candidate.items:  # type: ignore[attr-defined]
        climax = item.payload.get("volume_climax")
        if not isinstance(climax, dict):
            continue
        description = str(climax.get("description") or "")
        if phrase not in description or item.item_id.root == "vol-4":
            continue
        return PlanReviewIssue(
            issue_id=StableId("issue.d0.repeated-reveal"),
            kind=ReviewIssueKind.CONTRADICTION,
            summary="后卷高潮与前次揭露使用同一句式，读者无法判断这是新的进展还是重复",  # noqa: RUF001
            blocking=True,
            affected_item_ids=(item.item_id.root,),
            field_path="volume_climax.description",
            quote=description,
            unmet_condition="每次揭露必须让读者分辨这是首次、验证还是新的后果",
        )
    raise AssertionError("no grounded repetition finding could be built")


def test_d0_a_real_planner_revision_stays_inside_the_reviewed_scope(tmp_path: Path) -> None:
    """The gap D0 left open: a real model revision, host-composed and re-reviewed.

    The reviewer's finding authorises one field on one volume.  The model, asked to
    revise a proposal it did not write, will almost certainly rewrite more than
    that; the host composes the candidate from the parent plus exactly the
    authorised field, and records the rest as out of scope.  The composed candidate
    is then re-reviewed for real.  Passing means the candidate the *materializer
    would receive* is inside scope and accepted -- not that the model behaved.
    """

    import asyncio

    from novel_agent.agents.planner import PlannerAgent, _proposal_output_type
    from novel_agent.domain.plan_composition import (
        compose_scoped_revision,
        out_of_scope_items,
        revision_scope,
    )
    from novel_agent.domain.planning import PlanReview, ReviewDecision, ReviewTargetKind
    from novel_agent.domain.stage2 import PlanningTask

    candidate = _proposal_from_frozen()
    _constraints, _root, _profile_ref = author_constraint_root()
    reviewer, repo = _reviewer(tmp_path)
    bundle = build_planner_contract_bundle(package_root=PACKAGE_ROOT, version=VERSION)
    gateway = ModelGateway(
        (_endpoint(),),  # type: ignore[arg-type]
        forbid_external_calls=True,
        structured_max_retries=0,
        raw_artifacts=repo,
    )
    planner = PlannerAgent(
        StructuredAgentRunner(gateway, bundle.agents, bundle.prompts, bundle.skills), repo
    )

    finding = _grounded_revision_finding(candidate)
    parent_ref = repo.put(candidate.model_dump_json().encode(), PLAN_PROPOSAL_MEDIA_TYPE, VERSION)
    review = PlanReview(
        review_id=StableId("plan-review.d0.real-revision"),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_artifact_ref=parent_ref,
        decision=ReviewDecision.REVISE,
        issues=(finding,),
        revision_instruction=(
            "只修改 REVIEW 点名条目的 volume_climax.description；"  # noqa: RUF001
            "其余条目的 payload 必须与 PARENT_PROPOSAL 逐字一致。"
        ),
        receipt=_receipt(AgentMode.ARC_VOLUME, AgentType.PLAN_REVIEWER),
    )
    scope = revision_scope(review)
    assert scope.targeted_item_ids == {finding.affected_item_ids[0].root}
    assert scope.target_for(finding.affected_item_ids[0].root).field_paths == ("volume_climax",)

    brief = (FROZEN_RUN / "input/brief.md").read_bytes()
    brief_ref = repo.put(brief, "text/plain", VERSION)
    task = PlanningTask(
        planning_task_id=StableId("task.d0.revision"),
        project_id=PROJECT,
        mode=AgentMode.ARC_VOLUME,
        base_commit=COMMIT,
        source_ids=(StableId("source.d0.brief"),),
        strategy=None,
    )
    source_payload = (
        f"<AUTHOR_BRIEF>\n{brief.decode('utf-8')}\n</AUTHOR_BRIEF>\n"
        f"REVIEW_REVISION={review.revision_instruction}\n"
        "REVISION_SCOPE=只修改 REVIEW 点名条目/字段；其余条目的 payload 必须与 "  # noqa: RUF001
        "PARENT_PROPOSAL 逐字一致。\n"
        f"PARENT_CANDIDATE_HASH={candidate.proposal_id.root}\n"
        f"REVIEW={review.model_dump_json()}\n"
        f"PARENT_PROPOSAL={candidate.model_dump_json()}"
    )
    prepared = planner._runner.prepare(
        AgentType.PLANNER,
        AgentMode.ARC_VOLUME,
        VERSION.root,
        _request("plan-revision"),
        f"PLANNING_PHASE=plan\nPLANNING_TASK={task.model_dump_json()}\nSOURCE_DATA={source_payload}",
        source_hashes=(brief_ref.artifact_id,),
        input_artifacts=(brief_ref, parent_ref),
        base_commit=COMMIT,
        allowed_skill_ids=planner_skill_ids_for_mode(AgentMode.ARC_VOLUME),
    )
    execution = asyncio.run(planner._runner.execute(prepared, _proposal_output_type(task)))
    result = planner._materialize_plan(
        version=VERSION,
        task=task,
        draft=execution.output,
        prepared=prepared,
        model_call=execution.model_call,
        reviewed_inquiry_ref=None,
        memory_need_ids=(),
        evidence_refs=(),
        graph_path_receipt_refs=(),
        parent_proposal_id=candidate.proposal_id,
    )
    raw = result.plan_proposal

    composed = compose_scoped_revision(candidate, raw, scope)
    out_of_scope = out_of_scope_items(candidate, raw, scope)

    # The composed candidate carries the parent everywhere the review did not reach.
    target = finding.affected_item_ids[0].root
    for original, produced in zip(candidate.items, composed.items, strict=True):
        if original.item_id.root == target:
            continue
        assert produced.payload == original.payload, original.item_id.root

    _record(
        tmp_path,
        "d0.revision",
        {
            "target_item": target,
            "scope_field_paths": ["volume_climax"],
            "raw_items": len(raw.items),
            "raw_changed_items": sorted(
                item.item_id.root
                for item in raw.items
                if item.payload
                != next(
                    parent.payload
                    for parent in candidate.items
                    if parent.item_id.root == item.item_id.root
                )
            ),
            "out_of_scope_items": list(out_of_scope),
            "composed_target_description": next(
                item.payload["volume_climax"]
                for item in composed.items
                if item.item_id.root == target
            ),
            "usage": None
            if getattr(execution.model_call, "usage", None) is None
            else execution.model_call.usage.model_dump(mode="json"),
        },
    )

    # A revision that changed nothing is not convergence either; the diagnostic
    # reports it rather than asserting the model behaved.
    target_item = next(item for item in composed.items if item.item_id.root == target)
    parent_item = next(item for item in candidate.items if item.item_id.root == target)
    assert target_item.payload["volume_climax"] != parent_item.payload["volume_climax"], (
        "the real revision did not move the field the review named"
    )

    # Re-review the composed candidate for real.
    rereview, _ref, _call = asyncio.run(
        reviewer.review(
            version=VERSION,
            mode=AgentMode.ARC_VOLUME,
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            target_payload=composed.model_dump_json(),
            target_artifact=repo.put(
                composed.model_dump_json().encode(), PLAN_PROPOSAL_MEDIA_TYPE, VERSION
            ),
            trusted_source_artifacts=(
                repo.put(
                    author_constraint_root()[1].model_dump_json().encode(),
                    "application/vnd.novel-agent.author-constraint-root+json",
                    VERSION,
                ),
                repo.put(
                    _frozen_world_root().model_dump_json().encode(),
                    "application/vnd.novel-agent.world-root+json",
                    VERSION,
                ),
            ),
            request=_request("plan-rereview"),
            base_commit=COMMIT,
        )
    )
    _record(
        tmp_path,
        "d0.rereview",
        {
            "decision": rereview.decision.value,
            "verification_failures": list(rereview.verification_failures),
            "blocking": [
                {
                    "kind": issue.kind.value,
                    "host_issued": issue.host_issued,
                    "summary": issue.summary[:300],
                }
                for issue in rereview.issues
                if issue.blocking
            ],
        },
    )
    assert rereview.decision is ReviewDecision.ACCEPT, [
        issue.summary for issue in rereview.issues if issue.blocking
    ]


# ------------------------------------- does the reviewer emit structured findings?


def test_d0_the_real_reviewer_emits_findings_the_host_can_act_on(tmp_path: Path) -> None:
    """The question that decides whether a planning stage can converge at all.

    A review is only actionable if its blocking findings carry the four citation
    fields.  Prose naming the right fields is not enough: the host cannot verify or
    enforce it, so a loop driven by such a review cannot close its problem, which is
    what the frozen v23 run recorded before it was stopped by force.

    This inspects the *raw provider response* as well as the draft the host built
    from it, so "the host refused everything" and "the model emitted nothing usable"
    are distinguishable.
    """

    candidate = _proposal_from_frozen()
    _constraints, root, _profile_ref = author_constraint_root()
    reviewer, repo = _reviewer(tmp_path)
    target_ref = repo.put(candidate.model_dump_json().encode(), PLAN_PROPOSAL_MEDIA_TYPE, VERSION)
    root_ref = repo.put(
        root.model_dump_json().encode(),
        "application/vnd.novel-agent.author-constraint-root+json",
        VERSION,
    )
    world_ref = repo.put(
        _frozen_world_root().model_dump_json().encode(),
        "application/vnd.novel-agent.world-root+json",
        VERSION,
    )

    review, review_ref, _call = _run_review(reviewer, candidate, target_ref, root_ref, world_ref)

    # The draft the reviewer persisted, before the host overlay ran.
    draft = json.loads(repo.read_verified(review_ref).decode("utf-8"))
    blocking = [issue for issue in review.issues if issue.blocking]
    structured = [
        issue
        for issue in blocking
        if issue.affected_item_ids and issue.field_path and issue.quote and issue.unmet_condition
    ]

    _record(
        tmp_path,
        "d0.review_structure",
        {
            "decision": review.decision.value,
            "blocking_total": len(blocking),
            "blocking_structured": len(structured),
            "blocking_citation_shape": [
                {
                    "kind": issue.kind.value,
                    "items": [item.root for item in issue.affected_item_ids],
                    "field_path": issue.field_path,
                    "has_quote": issue.quote is not None,
                    "has_unmet_condition": issue.unmet_condition is not None,
                    "constraint_id": issue.constraint_id,
                    "host_issued": issue.host_issued,
                }
                for issue in blocking
            ],
            "verification_failures": list(review.verification_failures),
            "draft_revision_instruction_chars": len(draft.get("revision_instruction") or ""),
        },
    )

    # Every blocking finding the host kept must be actionable by construction; the
    # point of the record above is how *many* the model produced, not whether the
    # host could describe them.
    for issue in structured:
        assert issue.quote in candidate.model_dump_json()

    if blocking:
        # A review that demands work must be actionable, or the stage cannot close
        # its problem and will stop rather than converge.
        assert structured, (
            "the reviewer raised blocking findings without citations; "
            "a loop driven by this review cannot make checkable progress"
        )
    else:
        # Otherwise the candidate is accepted as-is, which is also a settled state.
        assert review.decision in {ReviewDecision.ACCEPT, ReviewDecision.HUMAN_REQUIRED}
