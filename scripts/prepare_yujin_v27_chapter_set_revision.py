"""Prepare a bounded chapter-set revision from an immutable reviewer draft."""

from __future__ import annotations

import argparse
import json
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
from novel_agent.domain.stage2 import AgentMode, PlanProposal
from novel_agent.services.artifacts import ArtifactRepository, object_key
from novel_agent.services.content_addressing import canonical_json_bytes

VERSION = SchemaVersion("1.0.0")
DIRECTIVE_MEDIA_TYPE = "application/vnd.novel-agent.operator-revision-directive+json"


def existing_ref(store: FilesystemObjectStore, artifact_id: ArtifactId) -> ArtifactRef:
    stat = store.stat(object_key(artifact_id))
    return ArtifactRef(
        artifact_id=artifact_id,
        media_type=stat.media_type,
        byte_length=stat.byte_length,
        schema_version=VERSION,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-request", type=Path, required=True)
    parser.add_argument("--source-object-store-root", type=Path, required=True)
    parser.add_argument("--parent-proposal-id", required=True)
    parser.add_argument("--review-draft-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    args = parser.parse_args()

    if args.destination_root.exists():
        raise SystemExit(f"refusing to overwrite existing run directory: {args.destination_root}")
    args.destination_root.mkdir(parents=True)
    shutil.copytree(
        args.source_object_store_root,
        args.destination_root / "objects",
        dirs_exist_ok=False,
    )

    store = FilesystemObjectStore(args.destination_root / "objects")
    artifacts = ArtifactRepository(store)
    parent_id = ArtifactId(args.parent_proposal_id)
    review_id = ArtifactId(args.review_draft_id)
    parent_ref = existing_ref(store, parent_id)
    review_ref = existing_ref(store, review_id)
    if parent_ref.media_type != "application/vnd.novel-agent.plan-proposal+json":
        raise SystemExit("parent artifact is not a Plan proposal")
    if review_ref.media_type != "application/vnd.novel-agent.plan-review-draft+json":
        raise SystemExit("review artifact is not a plan-review draft")

    parent = PlanProposal.model_validate_json(artifacts.read_verified(parent_ref), strict=True)
    review = json.loads(artifacts.read_verified(review_ref))
    issues = tuple(issue for issue in review.get("issues", ()) if issue.get("blocking"))
    expected_ids = {f"plan.chapter.{chapter}" for chapter in range(1, 6)}
    affected_ids = {
        item_id
        for issue in issues
        for item_id in issue.get("affected_item_ids", ())
        if item_id in expected_ids
    }
    if affected_ids != expected_ids:
        raise SystemExit(
            "review draft does not contain exactly the chapter-1..5 history-waiver findings"
        )

    parent_fields: dict[str, object] = {}
    for item in parent.items:
        if item.item_id.root in expected_ids:
            parent_fields[item.item_id.root] = {
                "history_retrieval": item.payload.get("history_retrieval")
            }

    findings: list[OperatorReviewFinding] = []
    for chapter in range(1, 6):
        item_id = f"plan.chapter.{chapter}"
        source_issue = next(
            issue for issue in issues if item_id in issue.get("affected_item_ids", ())
        )
        findings.append(
            OperatorReviewFinding(
                issue_id=StableId(f"operator.v27.r07.history-waiver.chapter-{chapter}"),
                kind=str(source_issue["kind"]),
                summary=str(source_issue["summary"]),
                affected_item_ids=(StableId(item_id),),
                field_path="history_retrieval",
                constraint_id=source_issue.get("constraint_id"),
                actual=source_issue.get("actual"),
                expected=source_issue.get("expected"),
            )
        )

    operator_review = OperatorReviewEvidence(
        review_id=StableId("review.operator.v27.r07.chapter-set-history"),
        target_artifact_ref=parent_ref,
        reviewer_id="codex.operator",
        reason=(
            "将真实 chapter-set reviewer 的宿主 waiver 阻断转换为一次有界叶字段修订; "
            "只开放每章 history_retrieval, 修订后仍须经过独立 Reviewer。"
        ),
        issues=tuple(findings),
        supporting_review_artifact_refs=(review_ref,),
    )
    operator_review_ref = artifacts.put(
        canonical_json_bytes(operator_review.model_dump(mode="json")),
        OPERATOR_PLAN_REVIEW_MEDIA_TYPE,
        VERSION,
    )

    directive = {
        "directive_version": "v27-chapter-set-history-repair-01",
        "kind": "operator_revision",
        "operator": "codex.operator",
        "decision": "revise",
        "source_review_artifact_ref": review_ref.model_dump(mode="json"),
        "target_parent_proposal_ref": parent_ref.model_dump(mode="json"),
        "parent_field_snapshot": parent_fields,
        "scope": [
            {
                "item_id": f"plan.chapter.{chapter}",
                "field_path": "history_retrieval",
                "operation": "modify",
            }
            for chapter in range(1, 6)
        ],
        "instructions": [
            (
                "只修改各章节的 history_retrieval; beats、entities、state_changes、"
                "obligation_actions、summary 和其他字段必须继承父候选。"
            ),
            (
                "第 1 章若使用 NOT_REQUIRED, reason_code 必须是 first_chapter, "
                "waiver_ref 必须精确为 waiver.history.first_chapter; 禁止 "
                "first_chapter_waiver 或自行发明 waiver。"
            ),
            (
                "第 2—5 章不得使用 first-chapter waiver; 在没有宿主签发的其他 waiver 时, "
                "必须使用 REQUIRED, 并提供 1—3 个 grounded history needs。"
            ),
            (
                "HistoryRetrievalNeed.entity_ids 只能填写上下文中真实存在的稳定 ID; "
                "中文实体标签不是稳定 ID, "
                "不要放进 entity_ids, 必要时省略该字段并在 query 中保留精确中文标签。"
            ),
            (
                "PLAN_READY 的 unresolved 和 unresolved_operations 必须保持父候选的空集; "
                "不要把 reviewer 或 operator 的 waiver 问题复制成 unresolved MODIFY/ADD/CLOSE。"
            ),
            "不要把宿主审查意见当成作者事实; 完成后接受独立 Reviewer 的再次检查。",
        ],
        "preserve_unmentioned_fields": True,
    }
    directive_ref = artifacts.put(
        canonical_json_bytes(directive),
        DIRECTIVE_MEDIA_TYPE,
        VERSION,
    )

    source = CreativeRunRequest.model_validate_json(args.source_request.read_bytes(), strict=True)
    author_inputs = tuple(
        ref for ref in source.input_artifact_refs if ref.media_type == "text/plain"
    )
    if not author_inputs:
        raise SystemExit("source request has no author text inputs")
    request = source.model_copy(
        update={
            "run_id": RunId(args.run_id),
            "input_artifact_refs": (
                *author_inputs,
                parent_ref,
                review_ref,
                operator_review_ref,
                directive_ref,
            ),
            "continuation_artifact_refs": (),
            "plan_level": source.plan_level,
        }
    )
    (args.destination_root / "state").mkdir()
    (args.destination_root / "receipts").mkdir()
    (args.destination_root / "state/request.json").write_text(
        request.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (args.destination_root / "state/policy.json").write_text(
        request.policy.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (args.destination_root / "state/lineage.json").write_text(
        json.dumps(
            {
                "source_run": source.run_id.root,
                "parent_proposal": parent_ref.model_dump(mode="json"),
                "source_review_draft": review_ref.model_dump(mode="json"),
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
            {
                "request": str(args.destination_root / "state/request.json"),
                "run_id": request.run_id.root,
                "mode": AgentMode.CHAPTER_SET.value,
                "operator_review": operator_review.review_id.root,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
