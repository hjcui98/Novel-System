"""Prepare the bounded ARC owner repair required before Writer execution."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue
from sqlalchemy.engine import URL

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    OPERATOR_PLAN_REVIEW_MEDIA_TYPE,
    CreativeRunRequest,
    OperatorReviewEvidence,
    OperatorReviewFinding,
)
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.memory import OBLIGATION_OWNER_SEMANTICS, WorldRootDocument
from novel_agent.domain.planning import VOLUME_NARRATIVE_STAGE_KEYS
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    ExecutionStatus,
    PlanProposal,
    ProposedItem,
)
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.domain.world import PlanLevel
from novel_agent.runtime.creative_assembly import (
    ProductionAssemblyContext,
    build_production_assembly,
)
from novel_agent.runtime.production_bootstrap import (
    QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE,
    resolve_registered_model_endpoints,
)
from novel_agent.services.artifacts import ArtifactRepository, object_key
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.projection import DerivedSnapshotRepository

VERSION = SchemaVersion("1.0.0")
DIRECTIVE_MEDIA_TYPE = "application/vnd.novel-agent.operator-revision-directive+json"
PLAN_PROPOSAL_MEDIA_TYPE = "application/vnd.novel-agent.plan-proposal+json"


def database_url_from_env_file(path: Path) -> str:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key] = value
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_PORT", "POSTGRES_DB")
    missing = tuple(key for key in required if not values.get(key))
    if missing:
        raise SystemExit("database env file is missing: " + ", ".join(missing))
    return URL.create(
        "postgresql+psycopg",
        username=values["POSTGRES_USER"],
        password=values["POSTGRES_PASSWORD"],
        host="127.0.0.1",
        port=int(values["POSTGRES_PORT"]),
        database=values["POSTGRES_DB"],
    ).render_as_string(hide_password=False)


def existing_ref(store: FilesystemObjectStore, artifact_id: ArtifactId) -> ArtifactRef:
    stat = store.stat(object_key(artifact_id))
    return ArtifactRef(
        artifact_id=artifact_id,
        media_type=stat.media_type,
        byte_length=stat.byte_length,
        schema_version=VERSION,
    )


def rebase_parent(
    parent: PlanProposal,
    *,
    current_commit: object,
    request: CreativeRunRequest,
    run_id: RunId,
) -> PlanProposal:
    now = datetime.now(UTC)
    receipt: AgentExecutionReceipt = parent.receipt.model_copy(
        update={
            "receipt_id": StableId(f"operator.rebase.{run_id.root}"[:128]),
            "run_id": run_id,
            "task_id": TaskId(f"{run_id.root}.plan"),
            "base_commit": current_commit,
            "input_artifacts": tuple(
                ref for ref in request.input_artifact_refs if ref.media_type == "text/plain"
            ),
            "output_artifacts": (),
            "model_call_ids": (),
            "tool_call_ids": (),
            "started_at": now,
            "completed_at": now,
            "latency_ms": 0,
            "status": ExecutionStatus.SUCCEEDED,
            "escalations": ("operator_rebased_from_current_canon",),
        }
    )
    return parent.model_copy(
        update={
            "proposal_id": StableId(f"plan-proposal.rebased.{run_id.root}"[:128]),
            "base_commit": current_commit,
            "receipt": receipt,
            "parent_proposal_id": None,
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.environ.get("NOVEL_DATABASE_URL"))
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--source-request", type=Path, required=True)
    parser.add_argument("--source-object-store-root", type=Path, required=True)
    parser.add_argument("--parent-proposal-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument(
        "--endpoint-profile",
        default=QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE,
    )
    parser.add_argument("--endpoint-request-limit", type=int, default=1)
    parser.add_argument("--scheduling-timeout-seconds", type=float, default=300.0)
    args = parser.parse_args()
    database_url = args.database_url
    if database_url is None and args.env_file is not None:
        database_url = database_url_from_env_file(args.env_file)
    if not database_url:
        raise SystemExit("--database-url, --env-file, or NOVEL_DATABASE_URL is required")
    if args.destination_root.exists():
        raise SystemExit(f"refusing to overwrite existing run directory: {args.destination_root}")

    source = CreativeRunRequest.model_validate_json(args.source_request.read_bytes(), strict=True)
    run_id = RunId(args.run_id)
    factory = build_session_factory(build_engine(database_url))
    commits = CommitService(factory)
    current_commit = commits.current_commit(source.project_id)
    snapshot = DerivedSnapshotRepository(factory).get_for_commit(current_commit)
    if snapshot is None:
        raise SystemExit(f"no exact projection snapshot for current commit of {source.project_id}")
    manifest = commits.load_manifest(current_commit)

    args.destination_root.mkdir(parents=True)
    shutil.copytree(
        args.source_object_store_root,
        args.destination_root / "objects",
        dirs_exist_ok=False,
    )
    store = FilesystemObjectStore(args.destination_root / "objects")
    artifacts = ArtifactRepository(store)
    parent_ref = existing_ref(store, ArtifactId(args.parent_proposal_id))
    if parent_ref.media_type != PLAN_PROPOSAL_MEDIA_TYPE:
        raise SystemExit("parent artifact is not a Plan proposal")
    parent = PlanProposal.model_validate_json(artifacts.read_verified(parent_ref), strict=True)
    world = WorldRootDocument.model_validate_json(
        artifacts.read_verified(manifest.world_root), strict=False
    )
    rebased = rebase_parent(
        parent,
        current_commit=current_commit,
        request=source,
        run_id=run_id,
    )
    rebased_ref = artifacts.put(
        canonical_json_bytes(rebased.model_dump(mode="json")),
        PLAN_PROPOSAL_MEDIA_TYPE,
        VERSION,
    )

    canonical_entity_ids = tuple(entity.entity_id.root for entity in world.entities)
    owner_target_rows: list[tuple[ProposedItem, int, dict[str, JsonValue], str]] = []
    for item in rebased.items:
        responsibilities = item.payload.get("obligation_plan")
        if not isinstance(responsibilities, list):
            continue
        owner_target_rows.extend(
            (item, index, responsibility, f"obligation_plan[{index}].owner_ids")
            for index, responsibility in enumerate(responsibilities)
            if isinstance(responsibility, dict)
        )
    owner_targets = tuple(owner_target_rows)
    if len(owner_targets) != len(world.obligations):
        raise SystemExit("ARC parent obligation count does not match the current World catalogue")
    owner_findings = tuple(
        OperatorReviewFinding(
            issue_id=StableId(f"operator.{run_id.root}.owner.{item.item_id.root}.{index}"[:128]),
            kind="OBLIGATION_OWNER_MISSING",
            summary=(
                f"{item.item_id.root} 的第 {index + 1} 条长期责任缺少叙事主体/检索锚点; "
                "必须绑定至少一个需持续追踪该责任状态的 canonical World entity ID。"
            ),
            affected_item_ids=(item.item_id,),
            field_path=field_path,
            constraint_id="host.obligation_owner.v1",
            actual="owner_ids=[]",
            expected=OBLIGATION_OWNER_SEMANTICS,
        )
        for item, index, _responsibility, field_path in owner_targets
    )
    accepted_obligation_ids = {item.obligation_id.root for item in world.obligations}
    stale_serves_targets: tuple[tuple[ProposedItem, str, str], ...] = tuple(
        (item, f"{stage_key}.serves", serves)
        for item in rebased.items
        for stage_key in VOLUME_NARRATIVE_STAGE_KEYS
        if isinstance((stage := item.payload.get(stage_key)), dict)
        and isinstance((serves := stage.get("serves")), str)
        and serves.strip()
        and serves.strip() not in accepted_obligation_ids
    )
    serves_findings = tuple(
        OperatorReviewFinding(
            issue_id=StableId(
                f"operator.{run_id.root}.serves.{item.item_id.root}.{field_path}"[:128]
            ),
            kind="STAGE_SERVES_HANDLE_STALE",
            summary=(
                f"{item.item_id.root}.{field_path} 使用旧显示层责任句柄 {actual!r}; "
                "必须绑定语义对应的 accepted World obligation_id。"
            ),
            affected_item_ids=(item.item_id,),
            field_path=field_path,
            constraint_id="host.volume_stage_window",
            actual=actual,
            expected=(
                "one accepted World obligation_id whose description and window match the stage"
            ),
        )
        for item, field_path, actual in stale_serves_targets
    )
    findings = (*owner_findings, *serves_findings)
    operator_review = OperatorReviewEvidence(
        review_id=StableId(f"review.operator.{run_id.root}.obligation-rebind"[:128]),
        target_artifact_ref=rebased_ref,
        reviewer_id="codex.operator",
        reason=(
            "补齐已提交 ARC 责任的叙事主体/检索锚点, 并把旧显示层 serves 句柄重绑到"
            " accepted World obligation_id; 不改卷结构、窗口或章节动作。"
        ),
        issues=findings,
    )
    operator_review_ref = artifacts.put(
        canonical_json_bytes(operator_review.model_dump(mode="json")),
        OPERATOR_PLAN_REVIEW_MEDIA_TYPE,
        VERSION,
    )
    directive = {
        "directive_version": "v27-arc-obligation-owner-and-serves-rebind-03",
        "kind": "operator_revision",
        "operator": "codex.operator",
        "decision": "revise",
        "target_parent_proposal_ref": rebased_ref.model_dump(mode="json"),
        "parent_field_snapshot": {
            item.item_id.root: {
                "obligation_plan": [
                    {
                        "index": index,
                        "kind": responsibility.get("kind"),
                        "summary": responsibility.get("summary"),
                        "setup_window": responsibility.get("setup_window"),
                        "progress_windows": responsibility.get("progress_windows"),
                        "payoff_window": responsibility.get("payoff_window"),
                        "owner_ids": responsibility.get("owner_ids", []),
                    }
                    for candidate, index, responsibility, _field_path in owner_targets
                    if candidate.item_id == item.item_id
                ],
                "stage_serves": {
                    field_path: actual
                    for candidate, field_path, actual in stale_serves_targets
                    if candidate.item_id == item.item_id
                },
            }
            for item in rebased.items
        },
        "scope": [
            {
                "item_id": item.item_id.root,
                "field_path": field_path,
                "operation": "modify",
            }
            for item, _index, _responsibility, field_path in owner_targets
        ]
        + [
            {
                "item_id": item.item_id.root,
                "field_path": field_path,
                "operation": "modify",
            }
            for item, field_path, _actual in stale_serves_targets
        ],
        "canonical_world_entity_ids": list(canonical_entity_ids),
        "canonical_world_entities": [
            {
                "entity_id": entity.entity_id.root,
                "entity_type": entity.entity_type,
                "internal_label": entity.internal_label,
                "aliases": list(entity.aliases),
            }
            for entity in world.entities
        ],
        "owner_ids_semantics": OBLIGATION_OWNER_SEMANTICS,
        "accepted_world_obligations": [
            {
                "obligation_id": obligation.obligation_id.root,
                "description": obligation.description,
                "kind": obligation.kind.value,
                "not_before_chapter": obligation.not_before_chapter,
                "due_chapter": obligation.due_chapter,
            }
            for obligation in world.obligations
        ],
        "instructions": [
            (
                "只修改 scope 中逐项列出的 obligation_plan[index].owner_ids; 卷结构、"
                "责任 summary/kind/windows/not_before_chapter 和其他字段由宿主继承父候选。"
            ),
            (
                "每个 owner_ids 至少一个值, 且只能逐字取自 canonical_world_entity_ids;"
                "owner 是需要持续追踪该责任设立、推进和兑现状态的叙事主体/检索锚点, "
                "不是奖励授予者、导师、信息持有者、地点或偶然参与者; 除非后者自身的"
                "持续状态就是该责任的一部分。多条责任可以基于事实绑定同一个 owner, "
                "不得为了制造多样性改绑其他实体。"
            ),
            (
                "只返回被授权 volume item 的最小 payload patch; obligation_plan 数组保留原索引,"
                "每个目标索引的对象只需给出 owner_ids。"
            ),
            (
                "对 scope 中的 *.serves, 只能从 accepted_world_obligations 逐字复制"
                " obligation_id; 按同卷 obligation description 与 stage description/role/window"
                " 选择语义对应项。不要输出旧 lock.* 句柄, 也不要修改 stage 的其他字段。"
            ),
            "PLAN_READY 的 unresolved 和 unresolved_operations 必须保持父候选的空集。",
        ],
        "preserve_unmentioned_fields": True,
    }
    directive_ref = artifacts.put(
        canonical_json_bytes(directive),
        DIRECTIVE_MEDIA_TYPE,
        VERSION,
    )

    author_inputs = tuple(
        ref for ref in source.input_artifact_refs if ref.media_type == "text/plain"
    )
    if not author_inputs:
        raise SystemExit("source request has no author text inputs")
    request = source.model_copy(
        update={
            "run_id": run_id,
            "basis_commit": current_commit,
            "basis_snapshot": snapshot.snapshot_id,
            "input_artifact_refs": (
                *author_inputs,
                rebased_ref,
                operator_review_ref,
                directive_ref,
            ),
            "continuation_artifact_refs": (),
            "plan_level": PlanLevel.ARC_VOLUME,
        }
    )
    manifest_path = (
        Path(__file__).resolve().parents[1]
        / "src/novel_agent/runtime/stage5_development_manifest.json"
    )
    assembly = build_production_assembly(
        ProductionAssemblyContext(
            database_url=database_url,
            object_store_root=args.destination_root / "objects",
            project_id=request.project_id,
            run_id=request.run_id,
            policy=request.policy,
            manifest=load_stage5_manifest(manifest_path),
            model_endpoints=resolve_registered_model_endpoints(args.endpoint_profile),
            endpoint_request_limit=args.endpoint_request_limit,
            scheduling_timeout_seconds=args.scheduling_timeout_seconds,
            retrieval_backend_profile="memory",
        )
    )
    configuration_fingerprint = assembly.attestation.configuration_fingerprint
    frozen_policy = request.policy.model_copy(
        update={
            "policy_hash": configuration_fingerprint.root,
            "permission_hash": configuration_fingerprint.root,
        }
    )
    request = request.model_copy(update={"policy": frozen_policy})
    (args.destination_root / "state").mkdir()
    (args.destination_root / "receipts").mkdir()
    (args.destination_root / "state/request.json").write_text(
        request.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (args.destination_root / "state/policy.json").write_text(
        frozen_policy.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (args.destination_root / "state/lineage.json").write_text(
        json.dumps(
            {
                "source_run": source.run_id.root,
                "current_basis": current_commit.root,
                "parent_proposal": rebased_ref.model_dump(mode="json"),
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
                "mode": request.plan_level.value if request.plan_level else None,
                "owner_target_count": len(owner_targets),
                "stale_serves_target_count": len(stale_serves_targets),
                "configuration_fingerprint": configuration_fingerprint.root,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
