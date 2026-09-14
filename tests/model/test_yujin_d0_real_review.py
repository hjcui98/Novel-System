"""D0: one real review of the frozen candidate, and what the host does with it.

The deterministic chain (``test_yujin_d0_frozen_candidate_chain.py``) proves the
mechanism.  This module makes the same point with a *real* review: one model call
against the registered ``qwen38_27b_nvfp4_8003`` profile, reviewing the frozen
candidate, with the frozen author locks as the trusted catalogue.

It is marked ``model_required`` and is off the deterministic path.  It spends at
most one initial call plus one bounded citation repair per logical review and
writes only into the diagnostic's own object store.  Nothing here advances the
frozen v23 run, writes its Canon, or calls a production CommitService.

What the run establishes, recorded in the delivery note:

* whether the reviewer's blocking findings carry citations that resolve against
  the candidate it named -- which is what the fixed verification checks;
* whether a refuted demand is demoted instead of forwarded, so no planner rewrites
  a volume over a sentence that is not there;
* whether the legitimate early-planting rule survives, so a legal `setup` is not
  pushed past a boundary it never crossed.

The diagnostic budget is two logical reviews, one bounded planner revision, and at
most one citation repair for each review or planner revision; it does not let the
planner regenerate eight volumes.

What the revision case does and does not establish is worth stating precisely.  It
requires the real Reviewer to discover the known repeated-climax defect first, then
passes those model-owned findings to the production Planner assembly.  The host
composes the returned proposal inside that reviewed scope and a real re-review must
accept it.  The composition's *restoration* path is covered deterministically
(``test_plan_composition_source_proof.py`` and the D0 chain's unauthorised-rewrite
case); a run in which the model wanders is recorded as ``out_of_scope_items`` rather
than trusted as extra authority.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.runtime.materializers import PLAN_PROPOSAL_MEDIA_TYPE
from novel_agent.agents.plan_reviewer import PlanReviewerAgent, PlanReviewerInvocationError
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
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelCallRecord,
    ModelRequest,
    ModelRole,
)
from novel_agent.domain.plan_composition import revision_scope
from novel_agent.domain.planning import (
    PlanReview,
    PlanReviewDraft,
    PlanReviewIssue,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import AgentMode, AgentType, PlanProposal
from novel_agent.runtime.production_bootstrap import (
    PACKAGE_ROOT,
    QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE,
    resolve_registered_model_endpoints,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from tests.integration.test_yujin_d0_frozen_candidate_chain import (
    FROZEN_CANDIDATE,
    FROZEN_LOCKS,
    FROZEN_RUN,
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
KNOWN_REPEATED_REVEAL_PHRASE = "正式揭露门被从对面推开"
KNOWN_REPEATED_REVEAL_IDS = frozenset({"vol-5", "vol-7", "vol-8"})
KNOWN_REPEATED_CONTENT = {
    "midpoint_reversal.description": "意识到门被从对面推开",
    "volume_climax.description": KNOWN_REPEATED_REVEAL_PHRASE,
    "ending_state.description": "但对其具体状态仍存疑问",
}
D0_CONTENT_REVIEW_FOCUS = (
    "仅审查 ARC_VOLUME 的 midpoint_reversal.description、volume_climax.description 和 "
    "ending_state.description 三个同名槽位的跨卷重复。先比较这些字段是否复现同一揭示、"
    "事件结果或叙事后果且没有新进展, 再决定是否报告 blocking; 不要在本次诊断中报告其"
    "他槽位的模板重复。字段名只是审查范围, 不是预置问题; 只有候选原文逐字证据成立才报告。"
    "每个重复观察都要遍历比较投影的全部条目, 把该 quote 在同一 field_path 中逐字命中的"
    "每个条目都列入 affected_item_ids, 不能只停在一对而漏掉第三个匹配条目。"
)


def _endpoint() -> RegisteredModelEndpoint:
    endpoints = resolve_registered_model_endpoints(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE)
    assert endpoints, "the registered profile resolved to no endpoint"
    return endpoints[0]


def _reviewer(tmp_path: Path) -> tuple[PlanReviewerAgent, ArtifactRepository]:
    repo = ArtifactRepository(FilesystemObjectStore(tmp_path / "d0-real-objects"))
    bundle = build_planner_contract_bundle(package_root=PACKAGE_ROOT, version=VERSION)
    gateway = ModelGateway(
        (_endpoint(),),
        forbid_external_calls=True,
        structured_max_retries=0,
        raw_artifacts=repo,
    )
    runner = StructuredAgentRunner(gateway, bundle.agents, bundle.prompts, bundle.skills)
    return PlanReviewerAgent(runner, repo), repo


def _frozen_world_root() -> WorldRootDocument:
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
        author_constraints=constraints,
        verified_citations=True,
    )
    if not [issue for issue in overlaid.issues if issue.blocking]:
        assert overlaid.decision is ReviewDecision.ACCEPT
        assert overlaid.revision_instruction is None


def _run_review(
    reviewer: PlanReviewerAgent,
    candidate: PlanProposal,
    target_ref: ArtifactRef,
    lock_ref: ArtifactRef,
    world_ref: ArtifactRef,
    *,
    phase: str = "plan-review",
    review_focus: str | None = None,
) -> tuple[PlanReview, ArtifactRef, ModelCallRecord]:
    import asyncio

    try:
        return asyncio.run(
            reviewer.review(
                version=VERSION,
                mode=AgentMode.ARC_VOLUME,
                target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                target_payload=candidate.model_dump_json(),
                target_artifact=target_ref,
                trusted_source_artifacts=(lock_ref, world_ref),
                request=_request(phase),
                base_commit=COMMIT,
                review_focus=review_focus,
            )
        )
    except PlanReviewerInvocationError as error:
        # One changed-input repair is part of the bounded D0 diagnostic.  The first
        # provider response and failed host draft remain durable in this repository.
        return asyncio.run(
            reviewer.review(
                version=VERSION,
                mode=AgentMode.ARC_VOLUME,
                target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                target_payload=candidate.model_dump_json(),
                target_artifact=target_ref,
                trusted_source_artifacts=(lock_ref, world_ref),
                request=_request(f"{phase}-repair"),
                base_commit=COMMIT,
                review_feedback=str(error),
                review_focus=review_focus,
            )
        )


def _record(tmp_path: Path, name: str, payload: dict[str, object]) -> None:
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
    assert DIAGNOSTIC_TIMEOUT_SECONDS <= 900.0


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


def _independent_known_defect_findings(
    candidate: PlanProposal, review: PlanReview
) -> tuple[PlanReviewIssue, ...]:
    """Find model findings that independently cite frozen repeated content.

    Host-generated structural findings are useful for the mechanical preflight, but
    they cannot establish that the real Reviewer understood a content defect.  D0
    therefore requires model-owned, blocking, field-level citations over the frozen
    repeated narrative phrases.  The host has already checked the returned citations;
    this second check keeps the diagnostic oracle tied to the immutable candidate
    rather than accepting any unrelated model issue.
    """

    descriptions: dict[str, dict[str, str]] = {}
    for item in candidate.items:
        fields: dict[str, str] = {}
        for field_path in KNOWN_REPEATED_CONTENT:
            slot, leaf = field_path.split(".", maxsplit=1)
            raw_slot = item.payload.get(slot)
            if isinstance(raw_slot, dict) and isinstance(raw_slot.get(leaf), str):
                fields[field_path] = raw_slot[leaf]
        descriptions[item.item_id.root] = fields
    findings: list[PlanReviewIssue] = []
    for issue in review.issues:
        phrase = KNOWN_REPEATED_CONTENT.get(issue.field_path or "")
        affected = {item_id.root for item_id in issue.affected_item_ids}
        quote = issue.quote
        if (
            issue.blocking
            and not issue.host_issued
            and phrase is not None
            and issue.unmet_condition
            and quote
            and len(affected) >= 2
            and phrase in quote
            and all(
                item_id in descriptions
                and issue.field_path in descriptions[item_id]
                and quote in descriptions[item_id][issue.field_path]
                for item_id in affected
            )
        ):
            findings.append(issue)
    return tuple(findings)


def test_d0_a_real_planner_revision_stays_inside_the_reviewed_scope(tmp_path: Path) -> None:
    """The gap D0 left open: a real model revision, host-composed and re-reviewed.

    The real review findings authorise one field on each frozen volume carrying the
    defect.  The model, asked to revise a proposal it did not write, may rewrite more
    than that; the host composes the candidate from the parent plus exactly the
    authorised fields, and records the rest as out of scope.  The composed candidate
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
    from novel_agent.domain.planning import ReviewDecision
    from novel_agent.domain.stage2 import PlanningTask

    candidate = _proposal_from_frozen()
    _constraints, root, _profile_ref = author_constraint_root()
    reviewer, repo = _reviewer(tmp_path)
    bundle = build_planner_contract_bundle(package_root=PACKAGE_ROOT, version=VERSION)
    gateway = ModelGateway(
        (_endpoint(),),
        forbid_external_calls=True,
        structured_max_retries=0,
        raw_artifacts=repo,
    )
    planner = PlannerAgent(
        StructuredAgentRunner(gateway, bundle.agents, bundle.prompts, bundle.skills), repo
    )

    parent_ref = repo.put(candidate.model_dump_json().encode(), PLAN_PROPOSAL_MEDIA_TYPE, VERSION)
    lock_ref = repo.put(
        root.model_dump_json().encode(),
        "application/vnd.novel-agent.author-constraint-root+json",
        VERSION,
    )
    world_ref = repo.put(
        _frozen_world_root().model_dump_json().encode(),
        "application/vnd.novel-agent.world-root+json",
        VERSION,
    )
    real_review, _review_ref, _review_call = _run_review(
        reviewer,
        candidate,
        parent_ref,
        lock_ref,
        world_ref,
        review_focus=D0_CONTENT_REVIEW_FOCUS,
    )
    known_findings = _independent_known_defect_findings(candidate, real_review)
    assert known_findings, "the real Reviewer did not discover the known repeated climax defect"
    volume_findings = tuple(
        issue for issue in known_findings if issue.field_path == "volume_climax.description"
    )
    volume_ids = {item_id.root for issue in volume_findings for item_id in issue.affected_item_ids}
    assert volume_ids >= KNOWN_REPEATED_REVEAL_IDS, (
        "the real Reviewer did not cover every frozen volume carrying the repeated climax"
    )
    expected_fields: dict[str, set[str]] = {}
    for issue in known_findings:
        assert issue.field_path is not None
        top_level_field = issue.field_path.split(".", maxsplit=1)[0]
        for item_id in issue.affected_item_ids:
            expected_fields.setdefault(item_id.root, set()).add(top_level_field)
    reviewed_fields = sorted({field for fields in expected_fields.values() for field in fields})
    review = real_review.model_copy(
        update={
            "decision": ReviewDecision.REVISE,
            "issues": known_findings,
            "revision_instruction": (
                "只修改 REVIEW 点名条目的 " + "、".join(reviewed_fields) + "；"  # noqa: RUF001
                "其余条目的 payload 必须与 PARENT_PROPOSAL 逐字一致。"
            ),
        }
    )
    scope = revision_scope(review)
    assert set(scope.targeted_item_ids) == set(expected_fields)
    for item_id, fields in expected_fields.items():
        target = scope.target_for(item_id)
        assert target is not None
        assert set(target.field_paths) == fields

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
        f"REVISION_SCOPE_DATA={scope.model_dump_json()}\n"
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

    def missing_revision_fields(proposal: PlanProposal) -> tuple[str, ...]:
        produced = {item.item_id.root: item.payload for item in proposal.items}
        missing: list[str] = []
        for item_id, fields in expected_fields.items():
            payload = produced.get(item_id)
            parent = next(item.payload for item in candidate.items if item.item_id.root == item_id)
            if payload is None:
                missing.extend(f"{item_id}.{field}" for field in fields)
                continue
            missing.extend(
                f"{item_id}.{field}" for field in fields if payload.get(field) == parent.get(field)
            )
        return tuple(missing)

    planner_repair: dict[str, object] | None = None
    missing = missing_revision_fields(raw)
    if missing:
        planner_repair = {
            "missing_fields_after_first_revision": list(missing),
            "request_phase": "plan-revision-repair",
        }
        repair_payload = (
            "PLANNING_PHASE=plan\n"
            f"PLANNING_TASK={task.model_dump_json()}\n"
            f"SOURCE_DATA={source_payload}\n"
            "PLANNER_REPAIR_FEEDBACK=上一份有界修订没有实际改变以下宿主点名字段: "
            f"{', '.join(missing)}. 这是唯一一次 repair; 必须输出完整 plan_items, "
            "逐一改变这些字段, 并逐字保留所有未点名 item/字段。"
        )
        repair_prepared = planner._runner.prepare(
            AgentType.PLANNER,
            AgentMode.ARC_VOLUME,
            VERSION.root,
            _request("plan-revision-repair"),
            repair_payload,
            source_hashes=(brief_ref.artifact_id,),
            input_artifacts=(brief_ref, parent_ref),
            base_commit=COMMIT,
            allowed_skill_ids=planner_skill_ids_for_mode(AgentMode.ARC_VOLUME),
        )
        repair_execution = asyncio.run(
            planner._runner.execute(repair_prepared, _proposal_output_type(task))
        )
        repair_result = planner._materialize_plan(
            version=VERSION,
            task=task,
            draft=repair_execution.output,
            prepared=repair_prepared,
            model_call=repair_execution.model_call,
            reviewed_inquiry_ref=None,
            memory_need_ids=(),
            evidence_refs=(),
            graph_path_receipt_refs=(),
            parent_proposal_id=candidate.proposal_id,
        )
        execution = repair_execution
        raw = repair_result.plan_proposal
        planner_repair["missing_fields_after_repair"] = list(missing_revision_fields(raw))

    composed = compose_scoped_revision(candidate, raw, scope)
    out_of_scope = out_of_scope_items(candidate, raw, scope)

    # The composed candidate carries the parent everywhere the review did not reach.
    targets = set(scope.targeted_item_ids)
    for original, produced in zip(candidate.items, composed.items, strict=True):
        if original.item_id.root in targets:
            continue
        assert produced.payload == original.payload, original.item_id.root

    _record(
        tmp_path,
        "d0.revision",
        {
            "target_items": sorted(targets),
            "scope_field_paths": sorted(
                {field for fields in expected_fields.values() for field in fields}
            ),
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
                if item.item_id.root in volume_ids
            ),
            "usage": None
            if getattr(execution.model_call, "usage", None) is None
            else execution.model_call.usage.model_dump(mode="json"),
            "planner_repair": planner_repair,
        },
    )

    # A revision that changed nothing is not convergence either; the diagnostic
    # reports it rather than asserting the model behaved.
    for item_id, fields in expected_fields.items():
        target_item = next(item for item in composed.items if item.item_id.root == item_id)
        parent_item = next(item for item in candidate.items if item.item_id.root == item_id)
        for field in fields:
            assert target_item.payload[field] != parent_item.payload[field], (
                f"the real revision did not move the field the review named: {item_id}.{field}"
            )

    # Re-review the composed candidate for real.  The same bounded citation-repair
    # rule applies, with a distinct request id so it cannot hide a duplicate call.
    rereview, _ref, _call = _run_review(
        reviewer,
        composed,
        repo.put(composed.model_dump_json().encode(), PLAN_PROPOSAL_MEDIA_TYPE, VERSION),
        lock_ref,
        world_ref,
        phase="plan-rereview",
        review_focus=D0_CONTENT_REVIEW_FOCUS,
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
    known_defects = _independent_known_defect_findings(candidate, review)
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
            "known_content_defect_findings": len(known_defects),
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
        assert issue.quote is not None
        assert issue.quote in candidate.model_dump_json()

    # D0 is specifically a content-defect exercise.  ACCEPT/zero findings, a
    # host-only structural finding, or a model finding with a loose citation does
    # not prove independent discovery and must fail the real validation.
    assert known_defects, (
        "the real Reviewer did not independently cite the known repeated climax "
        "defect across at least two frozen volumes"
    )
    assert blocking and structured
