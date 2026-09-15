"""Run one bounded, source-bound D0 content-revision round.

This entry is deliberately separate from the historical D0 pytest cases.  It
starts from the preserved e16 candidate, binds the corrected three-field scope
under scoped-revision.v3, calls the registered project-local 8003 endpoint for
one Planner revision (plus one format-only repair if needed), and performs one
independent Reviewer rereview.  Every raw response and decision is written to a
new locked diagnostic directory; an existing directory is never reused.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from tests.integration.test_yujin_d0_frozen_candidate_chain import author_constraint_root
from tests.model.test_yujin_d0_real_review import (
    FROZEN_RUN,
    PRESERVED_D0_OBJECTS,
    PRESERVED_E16_DIGEST,
    PRESERVED_FINAL_REVIEW_DIGEST,
    PRESERVED_ORIGINAL_REVIEW_DIGEST,
    _frozen_world_root,
)

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.runtime.materializers import PLAN_PROPOSAL_MEDIA_TYPE
from novel_agent.agents.plan_reviewer import PlanReviewerAgent, PlanReviewerInvocationError
from novel_agent.agents.planner import (
    PlannerAgent,
    _proposal_output_type,
    build_planner_contract_bundle,
    planner_skill_ids_for_mode,
)
from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    OPERATOR_PLAN_REVIEW_MEDIA_TYPE,
    OperatorReviewEvidence,
    OperatorReviewFinding,
)
from novel_agent.domain.ids import (
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
    bounded_stable_id,
)
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.plan_composition import (
    compose_scoped_revision,
    operator_revision_scope,
    out_of_scope_items,
)
from novel_agent.domain.planning import PlanReview, ReviewDecision, ReviewTargetKind
from novel_agent.domain.stage2 import AgentMode, AgentType, PlanningTask, PlanProposal
from novel_agent.runtime.production_bootstrap import (
    PACKAGE_ROOT,
    QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE,
    resolve_registered_model_endpoints,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint

VERSION = SchemaVersion("1.0.0")
PROJECT = ProjectId("project.yujin-jiuxu.v23.d0")
RUN = RunId("run.d0.targeted.v3")
COMMIT = CommitId("sha256:" + "d" * 64)
OUTPUT_TOKENS = 16_000
THINKING_TOKENS = 2_048
TIMEOUT_SECONDS = 900.0
ROUND = "r1"

TARGETS: tuple[tuple[str, str], ...] = (
    ("vol-8", "midpoint_reversal.description"),
    ("vol-6", "ending_state.description"),
    ("vol-7", "ending_state.description"),
)

REVIEW_FOCUS = (
    "这是一次有界的 ARC_VOLUME 内容复审。只把以下三个授权候选叶字段作为修改范围："
    "vol-8.midpoint_reversal.description、vol-6.ending_state.description、"
    "vol-7.ending_state.description。必须阅读完整候选及比较字段后独立判断，不能把字段名"
    "当作预置结论；只有逐条引用能够证明真实重复、身份重复或剩余未知表达不清，且没有"
    "新的因果、代价、信息或状态推进时，才报告 blocking。第八卷 midpoint 必须建立在"
    "第七卷已获得的真相之上并推动本卷选择/限制/代价；第六、七卷 ending 必须保留其"
    "已有阶位、信息差异和因果后果，不能再次声称首次取得已经拥有的黑铭，同时要明确"
    "下一步仍未知的对象。阶位或信息差异本身不是重复证据。每个 finding 必须逐条提供"
    "对应 item 的同一 field_path citation、quote、unmet_condition、affected_item_ids"
    "和独立 proposed_target_item_ids；比较基准不因此获得写权限。可以判定 ACCEPT，"
    "也可以报告其他有完整来源的阻断，但不得将未授权字段加入修改范围。"
)


def _preserved_object(digest: str) -> Path:
    return PRESERVED_D0_OBJECTS / "sha256" / digest[:2] / digest


def _endpoint() -> RegisteredModelEndpoint:
    endpoints = resolve_registered_model_endpoints(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE)
    if not endpoints:
        raise RuntimeError("qwen38_27b_nvfp4_8003 resolved to no endpoint")
    return endpoints[0]


def _request(phase: str) -> ModelRequest:
    return ModelRequest(
        request_id=bounded_stable_id(f"model-request.{RUN.root}.{phase}", f"model-request.{phase}"),
        run_id=RUN,
        task_id=TaskId(f"task.d0.targeted.{ROUND}"),
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id=f"trace.{RUN.root}.{phase}",
        prompt="",
        agent_mode=AgentMode.ARC_VOLUME.value,
        max_output_tokens=OUTPUT_TOKENS,
        timeout_seconds=TIMEOUT_SECONDS,
        enable_thinking=True,
        thinking_token_budget=THINKING_TOKENS,
    )


def _write_json(root: Path, name: str, payload: dict[str, Any]) -> None:
    path = root / f"{name}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"[d0] {name}: {path}")


def _field_value(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for segment in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return current


def _target_values(candidate: PlanProposal) -> dict[str, str]:
    values: dict[str, str] = {}
    for item_id, field_path in TARGETS:
        item = next(item for item in candidate.items if item.item_id.root == item_id)
        value = _field_value(item.payload, field_path)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"preserved candidate lacks {item_id}.{field_path}")
        values[f"{item_id}.{field_path}"] = value
    return values


def _known_support_refs(repo: ArtifactRepository) -> tuple[ArtifactRef, ...]:
    refs: list[ArtifactRef] = []
    for digest in (PRESERVED_ORIGINAL_REVIEW_DIGEST, PRESERVED_FINAL_REVIEW_DIGEST):
        refs.append(
            repo.put(
                _preserved_object(digest).read_bytes(),
                "application/vnd.novel-agent.plan-review+json",
                VERSION,
            )
        )
    r6_path = Path(__file__).parents[1] / "tmp/yujin-d0-rereview-20260914-r6/d0-real-objects"
    r6_digest = "659b9e9bd46b0299950673035c2e7fc6329dc9687c69fc3fd1aae7f9a3e7f08b"
    r6_object = r6_path / "sha256" / r6_digest[:2] / r6_digest
    if r6_object.exists():
        refs.append(
            repo.put(
                r6_object.read_bytes(),
                "application/vnd.novel-agent.plan-review+json",
                VERSION,
            )
        )
    if not refs:
        raise FileNotFoundError("no preserved review source available")
    return tuple(refs)


def _reviewer(repo: ArtifactRepository) -> PlanReviewerAgent:
    bundle = build_planner_contract_bundle(package_root=PACKAGE_ROOT, version=VERSION)
    gateway = ModelGateway(
        (_endpoint(),),
        forbid_external_calls=True,
        structured_max_retries=0,
        raw_artifacts=repo,
    )
    return PlanReviewerAgent(
        StructuredAgentRunner(gateway, bundle.agents, bundle.prompts, bundle.skills), repo
    )


def _planner(repo: ArtifactRepository) -> PlannerAgent:
    bundle = build_planner_contract_bundle(package_root=PACKAGE_ROOT, version=VERSION)
    gateway = ModelGateway(
        (_endpoint(),),
        forbid_external_calls=True,
        structured_max_retries=0,
        raw_artifacts=repo,
    )
    return PlannerAgent(
        StructuredAgentRunner(gateway, bundle.agents, bundle.prompts, bundle.skills), repo
    )


def _review(
    reviewer: PlanReviewerAgent,
    candidate: PlanProposal,
    target_ref: ArtifactRef,
    lock_ref: ArtifactRef,
    world_ref: ArtifactRef,
    phase: str,
) -> tuple[PlanReview, ArtifactRef, Any]:
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
            review_focus=REVIEW_FOCUS,
        )
    )


def main() -> int:
    default_root = Path(__file__).parents[1] / "tmp/yujin-d0-targeted-r1"
    diagnostic_root = Path(os.environ.get("YUJIN_D0_TARGETED_ROOT", str(default_root)))
    if diagnostic_root.exists():
        raise RuntimeError(
            f"diagnostic directory already exists; refusing rerun: {diagnostic_root}"
        )
    diagnostic_root.mkdir(parents=True, exist_ok=False)
    (diagnostic_root / "round.lock").write_text(
        "r1: one Planner revision plus one real Reviewer rereview; do not rerun\n",
        encoding="utf-8",
    )
    endpoint = _endpoint()
    _write_json(
        diagnostic_root,
        "configuration",
        {
            "round": ROUND,
            "code_sha": "ba4ce056463d1c67adb670d994e2d037253cbeed",
            "profile": QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE,
            "endpoint_name": endpoint.endpoint_name,
            "model_name": endpoint.model_name,
            "revision": endpoint.revision,
            "sequence_limit": endpoint.sequence_limit,
            "parent_digest": f"sha256:{PRESERVED_E16_DIGEST}",
            "scope_rule": "scoped-revision.v3",
            "targets": [f"{item}.{field}" for item, field in TARGETS],
            "max_content_rounds": 3,
        },
    )

    candidate_bytes = _preserved_object(PRESERVED_E16_DIGEST).read_bytes()
    candidate = PlanProposal.model_validate_json(candidate_bytes, strict=True)
    repo = ArtifactRepository(FilesystemObjectStore(diagnostic_root / "d0-real-objects"))
    parent_ref = repo.put(candidate_bytes, PLAN_PROPOSAL_MEDIA_TYPE, VERSION)
    support_refs = _known_support_refs(repo)
    _constraints, constraint_root, _profile_ref = author_constraint_root()
    lock_ref = repo.put(
        constraint_root.model_dump_json().encode(),
        "application/vnd.novel-agent.author-constraint-root+json",
        VERSION,
    )
    world_ref = repo.put(
        _frozen_world_root().model_dump_json().encode(),
        "application/vnd.novel-agent.world-root+json",
        VERSION,
    )
    actual = _target_values(candidate)
    operator_review = OperatorReviewEvidence(
        review_id=StableId("operator-review.d0.targeted.v3.r1"),
        target_artifact_ref=parent_ref,
        reviewer_id="codex.operator",
        reason=(
            "纠偏 r6 第二条概括；三卷存在阶位和信息差异，当前只处理第八卷 midpoint "
            "与第六/七卷 ending 的具体重复取得黑铭和剩余未知不清问题。"
        ),
        supporting_review_artifact_refs=support_refs,
        issues=(
            OperatorReviewFinding(
                issue_id=StableId("operator-issue.d0.vol8-midpoint"),
                kind="targeted_midpoint_reversal",
                summary="第八卷 midpoint 必须基于已知真相推进本卷新选择/限制/代价，不重演旧真相。",
                affected_item_ids=(StableId("vol-8"),),
                field_path="midpoint_reversal.description",
                constraint_id="d0.v3.vol8.midpoint_reversal.progression",
                actual=actual["vol-8.midpoint_reversal.description"],
                expected="第八卷新发现必须改变选择、限制或代价，并明确接续本卷 climax。",
            ),
            OperatorReviewFinding(
                issue_id=StableId("operator-issue.d0.vol6-ending"),
                kind="targeted_ending_state",
                summary="第六卷 ending 要保留已有阶位/黑铭状态，明确取得的信息、代价与剩余未知。",
                affected_item_ids=(StableId("vol-6"),),
                field_path="ending_state.description",
                constraint_id="d0.v3.vol6.ending_state.continuity",
                actual=actual["vol-6.ending_state.description"],
                expected="不得再次声称首次取得已有黑铭；剩余未知必须具体可区分。",
            ),
            OperatorReviewFinding(
                issue_id=StableId("operator-issue.d0.vol7-ending"),
                kind="targeted_ending_state",
                summary="第七卷 ending 要承接既有因果链和防御缺口，区分本卷新信息与剩余未知。",
                affected_item_ids=(StableId("vol-7"),),
                field_path="ending_state.description",
                constraint_id="d0.v3.vol7.ending_state.continuity",
                actual=actual["vol-7.ending_state.description"],
                expected="不得回到笼统初始悬念或重复取得已有黑铭，必须保留本卷后果。",
            ),
        ),
    )
    operator_ref = repo.put(
        operator_review.model_dump_json().encode(), OPERATOR_PLAN_REVIEW_MEDIA_TYPE, VERSION
    )
    scope = operator_revision_scope(operator_review)
    _write_json(
        diagnostic_root,
        "binding",
        {
            "parent": parent_ref.artifact_id.root,
            "operator_review": operator_ref.artifact_id.root,
            "supporting_reviews": [ref.artifact_id.root for ref in support_refs],
            "scope_rule": "scoped-revision.v3",
            "targets": sorted(scope.targeted_item_ids),
            "fields": sorted(
                f"{target.item_id.root}.{field}"
                for target in scope.targets
                for field in target.field_paths
            ),
            "actual": actual,
            "is_model_receipt": False,
        },
    )

    brief = (FROZEN_RUN / "input/brief.md").read_bytes()
    brief_ref = repo.put(brief, "text/plain", VERSION)
    task = PlanningTask(
        planning_task_id=StableId(f"task.d0.targeted.{ROUND}.revision"),
        project_id=PROJECT,
        mode=AgentMode.ARC_VOLUME,
        base_commit=COMMIT,
        source_ids=(StableId("source.d0.brief"),),
        strategy=None,
    )
    source_payload = (
        f"<AUTHOR_BRIEF>\n{brief.decode('utf-8')}\n</AUTHOR_BRIEF>\n"
        "OPERATOR_REVIEW_KIND=host_scope_binding\n"
        f"OPERATOR_REVIEW={operator_review.model_dump_json()}\n"
        f"REVISION_SCOPE={scope.model_dump_json()}\n"
        "REVISION_SCOPE_RULE=只修改三个授权叶字段；保留候选其余所有 item 与字段，"
        "不得把比较基准变成修改目标。\n"
        f"PARENT_CANDIDATE_HASH={candidate.proposal_id.root}\n"
        f"PARENT_PROPOSAL={candidate.model_dump_json()}"
    )
    planner = _planner(repo)

    def run_planner(phase: str, payload: str) -> tuple[PlanProposal, Any]:
        prepared = planner._runner.prepare(
            AgentType.PLANNER,
            AgentMode.ARC_VOLUME,
            VERSION.root,
            _request(phase),
            f"PLANNING_PHASE=plan\nPLANNING_TASK={task.model_dump_json()}\nSOURCE_DATA={payload}",
            source_hashes=(brief_ref.artifact_id, parent_ref.artifact_id, operator_ref.artifact_id),
            input_artifacts=(brief_ref, parent_ref, operator_ref),
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
        return result.plan_proposal, execution

    revised, execution = run_planner("targeted-r1-revision", source_payload)

    def missing_fields(proposal: PlanProposal) -> tuple[str, ...]:
        missing: list[str] = []
        for item_id, field_path in TARGETS:
            produced = next((item for item in proposal.items if item.item_id.root == item_id), None)
            parent = next(item for item in candidate.items if item.item_id.root == item_id)
            if produced is None or _field_value(produced.payload, field_path) == _field_value(
                parent.payload, field_path
            ):
                missing.append(f"{item_id}.{field_path}")
        return tuple(missing)

    repair: dict[str, Any] | None = None
    missing = missing_fields(revised)
    if missing:
        repair = {"missing_after_first": list(missing)}
        revised, execution = run_planner(
            "targeted-r1-format-repair",
            source_payload
            + "\nPLANNER_REPAIR_FEEDBACK=仅做一次格式修复；必须实际改变以下授权叶字段："
            + ", ".join(missing),
        )
        repair["missing_after_repair"] = list(missing_fields(revised))
    remaining = missing_fields(revised)
    if remaining:
        _write_json(
            diagnostic_root,
            "blocked",
            {"stage": "planner_revision", "missing_fields": list(remaining)},
        )
        return 2

    composed = compose_scoped_revision(candidate, revised, scope)
    composed_ref = repo.put(composed.model_dump_json().encode(), PLAN_PROPOSAL_MEDIA_TYPE, VERSION)
    out_of_scope = out_of_scope_items(candidate, revised, scope)
    _write_json(
        diagnostic_root,
        "revision",
        {
            "parent_candidate": parent_ref.artifact_id.root,
            "composed_candidate": composed_ref.artifact_id.root,
            "target_fields": [f"{item}.{field}" for item, field in TARGETS],
            "out_of_scope_items": list(out_of_scope),
            "planner_request_id": execution.model_call.request_id.root,
            "planner_usage": None
            if execution.model_call.usage is None
            else execution.model_call.usage.model_dump(mode="json"),
            "format_repair": repair,
        },
    )

    reviewer = _reviewer(repo)
    try:
        review, review_ref, review_call = _review(
            reviewer,
            composed,
            composed_ref,
            lock_ref,
            world_ref,
            "targeted-r1-rereview",
        )
    except PlanReviewerInvocationError as error:
        # Keep the first raw response and host draft durable, then allow exactly
        # one changed-input citation/format repair for this bounded round.
        review, review_ref, review_call = asyncio.run(
            reviewer.review(
                version=VERSION,
                mode=AgentMode.ARC_VOLUME,
                target_kind=ReviewTargetKind.PLAN_PROPOSAL,
                target_payload=composed.model_dump_json(),
                target_artifact=composed_ref,
                trusted_source_artifacts=(lock_ref, world_ref),
                request=_request("targeted-r1-rereview-repair"),
                base_commit=COMMIT,
                review_feedback=str(error),
                review_focus=REVIEW_FOCUS,
            )
        )
    blocking = [issue.summary for issue in review.issues if issue.blocking]
    _write_json(
        diagnostic_root,
        "rereview",
        {
            "candidate": composed_ref.artifact_id.root,
            "review_artifact": review_ref.artifact_id.root,
            "decision": review.decision.value,
            "blocking": blocking,
            "verification_failures": list(review.verification_failures),
            "review_usage": None
            if review_call.usage is None
            else review_call.usage.model_dump(mode="json"),
            "real_reviewer": True,
        },
    )
    if blocking or review.decision is not ReviewDecision.ACCEPT:
        _write_json(
            diagnostic_root,
            "blocked",
            {"stage": "real_rereview", "decision": review.decision.value, "blocking": blocking},
        )
        return 2
    _write_json(
        diagnostic_root,
        "accepted",
        {
            "candidate": composed_ref.artifact_id.root,
            "review_artifact": review_ref.artifact_id.root,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
