"""Prepare a bounded V27 successor from the real reviewer evidence."""

# ruff: noqa: E501, RUF001

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    OPERATOR_PLAN_REVIEW_MEDIA_TYPE,
    CreativeRunRequest,
    OperatorReviewEvidence,
    OperatorReviewFinding,
)
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId
from novel_agent.services.artifacts import ArtifactRepository, object_key
from novel_agent.services.content_addressing import canonical_json_bytes

VERSION = SchemaVersion("1.0.0")
REQUEST_SOURCE_RUN = Path("/home/cuihengjia/agent/novel/NS/yujin-jiuxu-v27-repair-02")
OBJECT_SOURCE_RUN = Path(
    os.environ.get(
        "YUJIN_V27_REVISION_OBJECT_SOURCE",
        "/home/cuihengjia/agent/novel/NS/yujin-jiuxu-v27-repair-02",
    )
)
DEST_RUN = Path(
    os.environ.get(
        "YUJIN_V27_REVISION_DEST",
        "/home/cuihengjia/agent/novel/NS/yujin-jiuxu-v27-revision-01",
    )
)
RUN_ID = os.environ.get("YUJIN_V27_REVISION_RUN_ID", "run.yujin-jiuxu.v27.revision.01")
DIRECTIVE_VERSION = os.environ.get(
    "YUJIN_V27_REVISION_DIRECTIVE_VERSION", "v27-bounded-revision-01"
)
PARENT_ID = ArtifactId(
    os.environ.get(
        "YUJIN_V27_REVISION_PARENT_ID",
        "sha256:8d2678e3f0cc37ec9c915911050c69f5742114e86d6f3d8d6be6f9ee85cbb485",
    )
)
REVIEW_DRAFT_ID = ArtifactId(
    os.environ.get(
        "YUJIN_V27_REVISION_REVIEW_ID",
        "sha256:b00b9a8a8c273f166a4ac9c9f285368fee2fa2a069433854a9621a9ee20ccfdf",
    )
)
FOLLOWUP = os.environ.get("YUJIN_V27_FOLLOWUP_FROM_REVIEW") == "1"
FIX_VOL2_SERVES = os.environ.get("YUJIN_V27_FIX_VOL2_SERVES") == "1"
OPERATOR_REVIEW_ID = os.environ.get(
    "YUJIN_V27_OPERATOR_REVIEW_ID", "review.operator.v27.revision.01"
)


def existing_ref(store: FilesystemObjectStore, artifact_id: ArtifactId) -> ArtifactRef:
    stat = store.stat(object_key(artifact_id))
    return ArtifactRef(
        artifact_id=artifact_id,
        media_type=stat.media_type,
        byte_length=stat.byte_length,
        schema_version=VERSION,
    )


def main() -> None:
    if DEST_RUN.exists():
        raise RuntimeError(f"refusing to overwrite existing run directory: {DEST_RUN}")
    DEST_RUN.mkdir(parents=True, exist_ok=False)
    shutil.copytree(OBJECT_SOURCE_RUN / "objects", DEST_RUN / "objects")
    destination_store = FilesystemObjectStore(DEST_RUN / "objects")
    repository = ArtifactRepository(destination_store)

    source_request = CreativeRunRequest.model_validate_json(
        (REQUEST_SOURCE_RUN / "state/request.json").read_bytes(), strict=True
    )
    parent_ref = existing_ref(destination_store, PARENT_ID)
    review_draft_ref = existing_ref(destination_store, REVIEW_DRAFT_ID)
    parent_document = json.loads(destination_store.get(object_key(PARENT_ID)))
    parent_fields = {
        "vol-1": {"volume_climax": None},
        "vol-2": {"volume_climax": None},
        "vol-6": {"volume_climax": None},
        "vol-7": {"volume_climax": None},
        "vol-8": {"volume_climax": None, "next_volume_hook": None},
    }
    for item in parent_document["items"]:
        item_id = item["item_id"]
        if item_id in parent_fields:
            for field_name in parent_fields[item_id]:
                parent_fields[item_id][field_name] = item["payload"].get(field_name)

    followup_draft = None
    if FOLLOWUP:
        followup_draft = json.loads(destination_store.get(object_key(REVIEW_DRAFT_ID)))

    legacy_operator_findings = (
        OperatorReviewFinding(
            issue_id=StableId(
                "issue.volume_stage_window_violation.ff39d41ff9a1e0500075b8e1dc937f57"
            ),
            kind="volume_stage_window_violation",
            summary="vol-1 的 volume_climax.window 早于其服务责任的 not_before_chapter 90。",
            affected_item_ids=(StableId("vol-1"),),
            field_path="volume_climax.window",
            constraint_id="host.volume_stage_window",
            actual="volume_climax.window starts at 81",
            expected="served responsibility starts no earlier than chapter 90",
        ),
        OperatorReviewFinding(
            issue_id=StableId(
                "issue.volume_stage_window_violation.bdc4c6457061df0bff2ff34ca58a9ca2"
            ),
            kind="volume_stage_window_violation",
            summary="vol-2 的 volume_climax.serves 未命名其兑现的宿主责任。",
            affected_item_ids=(StableId("vol-2"),),
            field_path="volume_climax.serves",
            constraint_id="host.volume_stage_window",
            actual="volume_climax.serves is empty",
            expected="name the host-accepted responsibility disclosed by the payoff stage",
        ),
        OperatorReviewFinding(
            issue_id=StableId(
                "issue.volume_stage_window_violation.d8afd53fc899e4bb66f11195726cece3"
            ),
            kind="volume_stage_window_violation",
            summary="vol-6 的 volume_climax.serves 未命名其兑现的宿主责任。",
            affected_item_ids=(StableId("vol-6"),),
            field_path="volume_climax.serves",
            constraint_id="host.volume_stage_window",
            actual="volume_climax.serves is empty",
            expected="name the host-accepted responsibility disclosed by the payoff stage",
        ),
        OperatorReviewFinding(
            issue_id=StableId(
                "issue.volume_stage_window_violation.3ac009649a2c8e080f266b597148c20f"
            ),
            kind="volume_stage_window_violation",
            summary="vol-7 的 volume_climax.serves 未命名其兑现的宿主责任。",
            affected_item_ids=(StableId("vol-7"),),
            field_path="volume_climax.serves",
            constraint_id="host.volume_stage_window",
            actual="volume_climax.serves is empty",
            expected="name the host-accepted responsibility disclosed by the payoff stage",
        ),
        OperatorReviewFinding(
            issue_id=StableId("issue.volume_structure_incomplete.be17b7b32762efc7afa56f0dc386b806"),
            kind="volume_structure_incomplete",
            summary="vol-8 缺少必需的 next_volume_hook 结构槽位。",
            affected_item_ids=(StableId("vol-8"),),
            field_path="next_volume_hook",
            constraint_id="host.volume_structure_incomplete",
            actual="next_volume_hook is missing",
            expected="provide a non-empty next_volume_hook stage entry",
        ),
        OperatorReviewFinding(
            issue_id=StableId(
                "issue.volume_stage_window_violation.afd59d536e75df48fe1827b64fb88f98"
            ),
            kind="volume_stage_window_violation",
            summary="vol-8 的 volume_climax.serves 未命名其兑现的宿主责任。",
            affected_item_ids=(StableId("vol-8"),),
            field_path="volume_climax.serves",
            constraint_id="host.volume_stage_window",
            actual="volume_climax.serves is empty",
            expected="name the host-accepted responsibility disclosed by the payoff stage",
        ),
    )
    if FOLLOWUP:
        operator_findings = tuple(
            OperatorReviewFinding(
                issue_id=StableId(issue["issue_id"]),
                kind=str(issue["kind"]),
                summary=str(issue["summary"]),
                affected_item_ids=tuple(
                    StableId(item_id) for item_id in issue.get("affected_item_ids", ())
                ),
                field_path=issue.get("field_path"),
                constraint_id=issue.get("constraint_id"),
                actual=issue.get("actual"),
                expected=issue.get("expected"),
            )
            for issue in followup_draft.get("issues", ())
            if issue.get("blocking")
        )
        if FIX_VOL2_SERVES:
            operator_findings = (
                *operator_findings,
                OperatorReviewFinding(
                    issue_id=StableId("issue.volume_stage_window_violation.vol2-serves-boundary"),
                    kind="volume_stage_window_violation",
                    summary=(
                        "卷二高潮当前绑定第三碎片责任，但该责任最早在第 201 章开放；"
                        "为使卷二高潮留在 101-200 章，必须改绑卷二可用的宿主责任。"
                    ),
                    affected_item_ids=(StableId("vol-2"),),
                    field_path="volume_climax.serves",
                    constraint_id="host.volume_stage_window",
                    actual="volume_climax.serves=lock.third-shard-er07.vol3",
                    expected=(
                        "volume_climax.serves=lock.inner-court.vol2，且高潮窗口保持在 101-200"
                    ),
                ),
            )
    else:
        operator_findings = legacy_operator_findings

    operator_review = OperatorReviewEvidence(
        review_id=StableId(OPERATOR_REVIEW_ID),
        target_artifact_ref=parent_ref,
        reviewer_id="codex.operator",
        reason=(
            "将真实 8003 Planner 候选的最新宿主审查结果转换为一次有界 revision 输入；"
            "只修复审查明确指出的叶字段，修订后仍须经过独立 Reviewer。"
        ),
        supporting_review_artifact_refs=(review_draft_ref,),
        issues=operator_findings,
    )
    operator_review_ref = repository.put(
        canonical_json_bytes(operator_review.model_dump(mode="json")),
        OPERATOR_PLAN_REVIEW_MEDIA_TYPE,
        VERSION,
    )

    revision_scope = tuple(
        {
            "item_id": item_id.root,
            "field_path": finding.field_path,
            "operation": "modify",
        }
        for finding in operator_findings
        for item_id in finding.affected_item_ids
        if finding.field_path
    )
    if FOLLOWUP:
        revision_instructions = [
            "只修改宿主审查点列出的字段；未授权字段、卷身份、卷范围、obligation_plan 和 unresolved 必须继承父候选。",
            "这是叶字段 revision；PLAN_READY 时 unresolved 必须为空数组或省略，禁止为宿主审查项生成 unresolved MODIFY/ADD/CLOSE。",
            "父候选相关字段的只读快照已在 parent_field_snapshot 中；不要为了读取这些字段向 Memory 提问。",
            str(
                followup_draft.get("revision_instruction")
                or "按宿主审查逐项修复并保持其他字段不变。"
            ),
            *(
                (
                    "卷二的 volume_climax.serves 必须从 lock.third-shard-er07.vol3 改为 "
                    "lock.inner-court.vol2；volume_climax.window 必须是合法的、位于 101-200 "
                    "范围内的非反向窗口（例如 196-200）。不要把第三碎片正式获取提前到卷二。",
                )
                if FIX_VOL2_SERVES
                else ()
            ),
            "不要把宿主修订意见当成作者事实；完成后接受独立 Reviewer 的再次检查。",
        ]
    else:
        revision_instructions = [
            "只修改上述六个字段；未授权字段、卷身份、卷范围、obligation_plan 和 unresolved 必须继承父候选。",
            "这是叶字段 revision；PLAN_READY 时 unresolved 必须为空数组或省略，禁止为宿主审查项生成 unresolved MODIFY/ADD/CLOSE。",
            "父候选相关字段的只读快照已在 parent_field_snapshot 中；不要为了读取这些字段向 Memory 提问。",
            "vol-1 的 volume_climax.window 起始章必须不早于 90，并仍位于 vol-1 范围内。",
            "vol-2、vol-6、vol-7、vol-8 的 volume_climax.serves 必须填写对应的宿主责任句柄。",
            "vol-8 必须补齐非空的 next_volume_hook 阶段条目。",
            "不要把宿主修订意见当成作者事实；完成后接受独立 Reviewer 的再次检查。",
        ]

    directive = {
        "directive_version": DIRECTIVE_VERSION,
        "kind": "operator_revision",
        "operator": "codex.operator",
        "decision": "revise",
        "source_review_artifact_ref": review_draft_ref.model_dump(mode="json"),
        "target_parent_proposal_ref": parent_ref.model_dump(mode="json"),
        "parent_field_snapshot": parent_fields,
        "scope": revision_scope,
        "instructions": revision_instructions,
        "preserve_unmentioned_fields": True,
    }
    directive_ref = repository.put(
        canonical_json_bytes(directive),
        "application/vnd.novel-agent.operator-revision-directive+json",
        VERSION,
    )

    request = source_request.model_copy(
        update={
            "run_id": RunId(RUN_ID),
            "input_artifact_refs": (
                *source_request.input_artifact_refs,
                directive_ref,
                parent_ref,
                operator_review_ref,
            ),
        }
    )
    (DEST_RUN / "state").mkdir(parents=True, exist_ok=False)
    (DEST_RUN / "receipts").mkdir(parents=True, exist_ok=False)
    (DEST_RUN / "state/request.json").write_text(
        request.model_dump_json(indent=2), encoding="utf-8"
    )
    (DEST_RUN / "state/policy.json").write_text(
        request.policy.model_dump_json(indent=2), encoding="utf-8"
    )
    (DEST_RUN / "state/lineage.json").write_text(
        json.dumps(
            {
                "source_run": OBJECT_SOURCE_RUN.name,
                "parent_proposal": parent_ref.model_dump(mode="json"),
                "source_review_draft": review_draft_ref.model_dump(mode="json"),
                "operator_review": operator_review_ref.model_dump(mode="json"),
                "revision_directive": directive_ref.model_dump(mode="json"),
                "bounded_scope": directive["scope"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"request": str(DEST_RUN / "state/request.json"), "run_id": request.run_id.root},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
