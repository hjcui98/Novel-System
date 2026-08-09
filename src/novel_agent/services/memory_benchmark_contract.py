"""Safe public-task construction and taint checks for Stage 2M."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novel_agent.domain.artifacts import PlanRootRef
from novel_agent.domain.ids import ArtifactId, ProjectId, StableId
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
)
from novel_agent.services.benchmark_importer import content_id

TASK_TEMPLATE_VERSION = "memory_context_task.v1"
OUTPUT_CONTRACT_VERSION = "writer_context.v1"

_PRIVATE_FIELD_FRAGMENTS = (
    "gold",
    "future",
    "forbidden",
    "target_plan",
    "preparation",
    "accepted_evidence",
)
_FIXED_COUNT_PHRASES = (
    "不超过 18 项",
    "不超过18项",
    "不超过 20 项",
    "不超过20项",
    "不超过 22 项",
    "不超过22项",
    "最多 18",
    "最多18",
    "最多 20",
    "最多20",
    "最多 22",
    "最多22",
)


class PublicBenchmarkTaintError(ValueError):
    pass


def build_safe_task_contract(
    *,
    case_id: StableId,
    checkpoint_chapter: int,
    target_range: tuple[int, int],
    information_profile: BenchmarkInformationProfile,
    task_intent: str = "",
    planning_context_ref: ArtifactId | None = None,
    planning_context_hash: ArtifactId | None = None,
) -> BenchmarkTaskContract:
    """Build the only task text accepted by formal Stage 2M execution.

    The fixed safety contract is combined with (never replaced by) the
    normalized task intent derived from the case AuthorPlanningContext.
    Blind profiles must not receive any task intent.
    """

    if information_profile is BenchmarkInformationProfile.VISIBLE_AT_CUTOFF and task_intent:
        raise PublicBenchmarkTaintError("blind profile cannot receive a task intent")
    start, end = target_range
    profile_rule = (
        "可以使用经过验证的作者粗粒度计划, 但必须把计划标为意图或义务, 不能当作已发生事实。"
        if information_profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
        else "不得使用作者计划节点或任何未来材料; 但可以使用任务意图作为检索方向。"
        if information_profile is BenchmarkInformationProfile.TASK_INTENT_ONLY
        else "不得使用截止点之后的作者粗纲、计划节点或任何未来材料。"
    )
    task_text = (
        f"为目标章节 {start}-{end} 准备必要的历史记忆 ContextPackage。"
        "恢复与截止点相符的当前状态、关系与情绪、因果历史、角色知识边界、"
        "未决义务和长程回收提醒。不要续写; 不要猜测或泄漏截止点之后的正文、"
        "评测 Gold、精确目标计划或准备材料; 每条确定性结论必须引用合法历史证据。"
        f"{profile_rule}"
    )
    assert_safe_public_payload({"task_text": task_text, "task_intent": task_intent})
    return BenchmarkTaskContract(
        task_id=StableId(
            f"task.stage2m.{case_id.root}.{information_profile.value.replace('_', '-')}"
        ),
        task_text=task_text,
        checkpoint_chapter=checkpoint_chapter,
        target_chapter_start=start,
        target_chapter_end=end,
        information_profile=information_profile,
        task_template_version=TASK_TEMPLATE_VERSION,
        output_contract_version=OUTPUT_CONTRACT_VERSION,
        task_intent=task_intent,
        planning_context_ref=planning_context_ref,
        planning_context_hash=planning_context_hash,
    )


def public_input_hash(payload: Mapping[str, Any]) -> ArtifactId:
    """Hash a public payload after recursively proving it contains no private fields."""

    scrubbed = dict(payload)
    scrubbed.pop("public_input_hash", None)
    assert_safe_public_payload(scrubbed)
    return content_id(scrubbed)


def build_public_checkpoint_case(
    *,
    case_id: StableId,
    project_id: ProjectId,
    history_range: tuple[int, int],
    target_range: tuple[int, int],
    information_profile: BenchmarkInformationProfile,
    plan_root_ref: PlanRootRef | None = None,
    task_intent: str = "",
    planning_context_ref: ArtifactId | None = None,
    planning_context_hash: ArtifactId | None = None,
) -> Any:
    """Construct a hash-bound ``PublicCheckpointCase`` without importing private data."""

    from novel_agent.domain.stage2 import PublicCheckpointCase

    task = build_safe_task_contract(
        case_id=case_id,
        checkpoint_chapter=history_range[1],
        target_range=target_range,
        information_profile=information_profile,
        task_intent=task_intent,
        planning_context_ref=planning_context_ref,
        planning_context_hash=planning_context_hash,
    )
    if (
        information_profile
        in {
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            BenchmarkInformationProfile.TASK_INTENT_ONLY,
        }
        and plan_root_ref is not None
    ):
        raise ValueError(f"{information_profile.value} public case rejects PlanRoot")
    provisional = {
        "case_id": case_id,
        "project_id": project_id,
        "target_range": target_range,
        "history_range": history_range,
        "task_contract": task,
        "plan_root_ref": plan_root_ref,
    }
    digest = public_input_hash(
        {
            key: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
            for key, value in provisional.items()
        }
    )
    return PublicCheckpointCase(
        case_id=case_id,
        project_id=project_id,
        target_range=target_range,
        history_range=history_range,
        task_contract=task,
        plan_root_ref=plan_root_ref,
        public_input_hash=digest,
    )


def verify_public_checkpoint_case(case: Any) -> None:
    """Verify the hash binding before any retrieval or model call."""

    payload = case.model_dump(mode="json")
    expected = public_input_hash(payload)
    if expected != case.public_input_hash:
        raise PublicBenchmarkTaintError("public checkpoint input hash mismatch")


def assert_safe_public_payload(payload: Any, *, path: str = "$") -> None:
    """Fail closed when private/evaluator-only names enter a public payload."""

    if isinstance(payload, Mapping):
        for raw_key, value in payload.items():
            key = str(raw_key).casefold()
            if any(fragment in key for fragment in _PRIVATE_FIELD_FRAGMENTS):
                raise PublicBenchmarkTaintError(
                    f"evaluator-only field is forbidden in public payload: {path}.{raw_key}"
                )
            assert_safe_public_payload(value, path=f"{path}.{raw_key}")
        return
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        for index, value in enumerate(payload):
            assert_safe_public_payload(value, path=f"{path}[{index}]")
        return
    if isinstance(payload, str) and any(phrase in payload for phrase in _FIXED_COUNT_PHRASES):
        raise PublicBenchmarkTaintError(
            f"fixed answer-count hint is forbidden in public task: {path}"
        )


def profile_namespace(
    project_id: ProjectId,
    information_profile: BenchmarkInformationProfile,
    experiment_id: str,
) -> str:
    """Return an explicit profile-isolated namespace for stateful services."""

    if not experiment_id.strip():
        raise ValueError("experiment id must be non-empty")
    return f"{project_id.root}:{information_profile.value}:{experiment_id}"
