#!/usr/bin/env python3
# ruff: noqa: RUF001
"""Render frozen Stage 2M checkpoint outputs as human-readable Markdown."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, TypedDict

import yaml

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    MemoryBenchmarkCaseArmReport,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.memory_benchmark_reporting import MemoryBenchmarkReporter

CHECKPOINTS = (20, 40, 60, 80, 95)
PROFILES = (
    BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
)
PROFILE_LABELS = {
    BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED: "APC（author_plan_conditioned）",
    BenchmarkInformationProfile.VISIBLE_AT_CUTOFF: "VAC（visible_at_cutoff）",
}
SECTION_LABELS = {
    "current_world_state": "当前世界状态",
    "relationship_and_emotion": "关系与情绪",
    "causal_history": "因果历史",
    "knowledge_and_disclosure": "知识与披露边界",
    "continuity_constraints": "连续性约束",
    "plan_and_obligations": "计划与义务",
    "long_range_callbacks": "长程回调",
    "gaps": "明确缺口",
}


class CheckpointSummary(TypedDict):
    case_id: str
    arms: str
    coverage: float
    mandatory: float
    untraceable: float
    contradiction: float
    ready: str
    scenario_completed: bool
    formal_identity_complete: bool


FORMAL_CASE_FIELDS = (
    "code_version",
    "run_config_hash",
    "benchmark_contract_hash",
    "matcher_version",
    "writer_token_budget",
    "evidence_ledger_token_budget",
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    value.add_argument(
        "--output-directory",
        type=Path,
        default=None,
    )
    return value


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _load_diagnostic_case(
    path: Path,
    *,
    manifest: dict[str, Any],
    scenario: dict[str, Any],
) -> tuple[MemoryBenchmarkCaseArmReport, bool]:
    """Parse a legacy case without mutating or promoting its frozen JSON."""

    payload = _load_json(path)
    identity_complete = all(field in payload for field in FORMAL_CASE_FIELDS)
    if identity_complete:
        return MemoryBenchmarkCaseArmReport.model_validate_json(json.dumps(payload)), True

    freezes = scenario.get("freezes")
    if not isinstance(freezes, list) or len(freezes) != 1 or not isinstance(freezes[0], dict):
        raise ValueError(f"cannot derive diagnostic-only identity: {path}")
    freeze = freezes[0]
    # These values only let the current domain model read a legacy diagnostic artifact. They are
    # never persisted and the reporter below runs with formal validation disabled.
    payload.update(
        {
            "code_version": f"legacy-diagnostic:{manifest['code_commit']}",
            "run_config_hash": freeze["configuration_fingerprint"],
            "benchmark_contract_hash": manifest["benchmark_content_hash"],
            "matcher_version": manifest["gold_evidence_matcher_version"],
            "writer_token_budget": manifest["writer_token_budget"],
            "evidence_ledger_token_budget": 12_000,
        }
    )
    return MemoryBenchmarkCaseArmReport.model_validate_json(json.dumps(payload)), False


def _checkpoint_directory(run: Path, profile: BenchmarkInformationProfile, cp: int) -> Path:
    if profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED and cp == 20:
        return run
    return run / "checkpoints" / f"C{cp}"


def _verify_and_load_frozen(checkpoint_directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    scenario = _load_json(checkpoint_directory / "scenario_run.json")
    manifest = _load_json(checkpoint_directory / "experiment_manifest.json")
    freezes = scenario.get("freezes")
    if not isinstance(freezes, list) or len(freezes) != 1 or not isinstance(freezes[0], dict):
        raise ValueError(f"expected exactly one checkpoint freeze: {checkpoint_directory}")
    ref = freezes[0].get("context_artifact")
    if not isinstance(ref, dict):
        raise ValueError(f"freeze has no context artifact: {checkpoint_directory}")
    artifact_id = str(ref["artifact_id"])
    digest = artifact_id.removeprefix("sha256:")
    project_directory = Path(str(manifest["project_directory"]))
    object_path = project_directory / "objects" / "sha256" / digest[:2] / digest
    data = object_path.read_bytes()
    if len(data) != int(ref["byte_length"]):
        raise ValueError(f"frozen artifact length mismatch: {object_path}")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"frozen artifact hash mismatch: {object_path}")
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise ValueError(f"expected frozen object: {object_path}")
    return payload, ref


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def _short_hash(value: object) -> str:
    text = str(value)
    if text.startswith("sha256:") and len(text) > 23:
        return f"{text[:15]}…{text[-8:]}"
    return text


def _arm_payload(frozen: dict[str, Any], arm: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if arm == "A":
        result = frozen["deterministic"]
        assert isinstance(result, dict)
        writer = result.get("writer_context")
        ledger = result.get("evidence_ledger")
    elif arm == "B":
        result = frozen["agentic"]
        assert isinstance(result, dict)
        writer = result.get("writer_context")
        ledger = result.get("evidence_ledger")
    else:
        writer = frozen.get("arm_c_writer_context")
        ledger = frozen.get("arm_c_evidence_ledger")
    if not isinstance(writer, dict) or not isinstance(ledger, dict):
        raise ValueError(f"published Arm {arm} has no readable Writer Context or ledger")
    return writer, ledger


def _load_gold(repository: Path, case_id: str) -> dict[str, dict[str, Any]]:
    path = repository / "benchmarks/private/ztj_memory_pilot_v0.1/cases" / case_id / "gold.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {str(item["id"]): item for item in payload["items"]}


def _render_evidence_index(ledger: dict[str, Any]) -> list[str]:
    entries = ledger.get("entries", [])
    lines = [
        f"<details><summary>证据索引（{len(entries)} 条，点击展开）</summary>",
        "",
        "| Ledger ID | Writer 可见结论 | 证据章/Span | 支持状态 | Source commit |",
        "|---|---|---|---|---|",
    ]
    for entry in entries:
        refs = entry.get("evidence_refs", [])
        locations = []
        statuses = []
        for ref in refs:
            span = ref.get("span") or {}
            chapter = str(ref.get("chapter_id", "-"))
            if span:
                chapter += f" @{span.get('start', '?')}–{span.get('end', '?')}"
            locations.append(chapter)
            statuses.append(str(ref.get("support_status", "-")))
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(entry.get("ledger_id")),
                    _cell(entry.get("claim_excerpt")),
                    _cell("; ".join(locations) or "PlanNode/no text ref"),
                    _cell(", ".join(sorted(set(statuses))) or "-"),
                    _cell(_short_hash(entry.get("source_commit"))),
                )
            )
            + " |"
        )
    lines.extend(("", "</details>", ""))
    return lines


def _render_gold_table(
    report: MemoryBenchmarkCaseArmReport,
    gold: dict[str, dict[str, Any]],
) -> list[str]:
    diagnostics = {item.gold_id.root: item for item in report.evaluation.stage_loss_diagnostics}
    lines = [
        "### 逐 Gold 评测（冻结后 evaluator 信息）",
        "",
        "| Gold | 必须 | 权重 | 状态 | 目标事实 | 为什么需要 | 缺失 component | 主失败层 |",
        "|---|---:|---:|---|---|---|---|---|",
    ]
    for comparison in report.evaluation.comparisons:
        item = gold.get(comparison.gold_id.root, {})
        diagnostic = diagnostics.get(comparison.gold_id.root)
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(comparison.gold_id.root),
                    "是" if comparison.mandatory else "否",
                    _cell(comparison.weight),
                    _cell(comparison.status.value),
                    _cell(item.get("fact", "Gold 文本未找到")),
                    _cell(item.get("why_needed", "-")),
                    _cell(", ".join(comparison.missing_components) or "-"),
                    _cell(diagnostic.primary_failure.value if diagnostic else "-"),
                )
            )
            + " |"
        )
    lines.append("")
    return lines


def _render_quick_summary(
    report: MemoryBenchmarkCaseArmReport,
    gold: dict[str, dict[str, Any]],
) -> list[str]:
    delivered = []
    missing = []
    for comparison in report.evaluation.comparisons:
        item = gold.get(comparison.gold_id.root, {})
        fact = str(item.get("fact", comparison.gold_id.root))
        label = f"{comparison.gold_id.root} / {comparison.status.value}: {fact}"
        if comparison.status.value in {"HIT", "PARTIAL"}:
            delivered.append(label)
        else:
            prefix = "[必须] " if comparison.mandatory else ""
            missing.append(prefix + label)
    lines = [
        "### 人类快速摘要",
        "",
        "这不是新的模型判断，而是把冻结 evaluator 的逐 Gold 结果改写成阅读清单。",
        "",
        f"已交付或部分交付：{len(delivered)} 项。",
        "",
    ]
    lines.extend(f"- {item}" for item in delivered)
    if not delivered:
        lines.append("- 无。")
    lines.extend(("", f"未交付、不可追溯或矛盾：{len(missing)} 项。", ""))
    lines.extend(f"- {item}" for item in missing)
    if not missing:
        lines.append("- 无。")
    lines.append("")
    return lines


def _render_arm(
    arm: str,
    report: MemoryBenchmarkCaseArmReport,
    frozen: dict[str, Any],
    gold: dict[str, dict[str, Any]],
) -> list[str]:
    writer, ledger = _arm_payload(frozen, arm)
    budget = writer["budget_report"]
    sections = ", ".join(
        f"{label} {len(writer.get(key, []))}" for key, label in SECTION_LABELS.items()
    )
    statuses = Counter(item.status.value for item in report.evaluation.comparisons)
    rendered = str(writer.get("rendered_context", "")).replace("```", "``\u200b`")
    lines = [
        f"## Arm {arm} 最终产物",
        "",
        "| 项目 | 值 |",
        "|---|---|",
        f"| Assembly | `{report.assembly_status.value}` |",
        f"| Comparable | `{str(report.comparable).lower()}` |",
        f"| Writer tokens | {report.writer_tokens} / {budget['configured_writer_token_budget']} |",
        f"| Evidence tokens | {report.evidence_tokens} |",
        f"| Selected units | {report.selected_unit_count} |",
        f"| Weighted coverage | {report.evaluation.weighted_coverage:.3f} |",
        f"| Mandatory hit | {report.evaluation.mandatory_hit_rate:.3f} |",
        f"| Untraceable | {report.evaluation.untraceable_rate:.3f} |",
        f"| Contradiction | {report.evaluation.contradiction_rate:.3f} |",
        f"| Gold 状态分布 | `{dict(sorted(statuses.items()))}` |",
        f"| Writer Ledger hash | `{report.writer_evidence_ledger_ref.artifact_id.root}` |",
        f"| 内容分区 | {sections} |",
        "",
    ]
    lines.extend(_render_quick_summary(report, gold))
    lines.extend(
        (
            "### Writer 实际收到的完整 Context",
            "",
            "以下内容逐字来自冻结对象的 `rendered_context`；"
            "它是本 checkpoint 最接近“最终产品”的部分。",
            "",
            "```text",
            rendered,
            "```",
            "",
        )
    )
    lines.extend(_render_gold_table(report, gold))
    lines.extend(_render_evidence_index(ledger))
    return lines


def _render_checkpoint(
    repository: Path,
    run: Path,
    profile: BenchmarkInformationProfile,
    cp: int,
) -> tuple[str, CheckpointSummary]:
    checkpoint_directory = _checkpoint_directory(run, profile, cp)
    frozen, frozen_ref = _verify_and_load_frozen(checkpoint_directory)
    scenario = _load_json(checkpoint_directory / "scenario_run.json")
    manifest = _load_json(checkpoint_directory / "experiment_manifest.json")
    reports: dict[str, MemoryBenchmarkCaseArmReport] = {}
    formal_identity_complete = True
    for path in sorted(checkpoint_directory.glob(f"stage2m_case_C{cp}_*.json")):
        report, report_identity_complete = _load_diagnostic_case(
            path,
            manifest=manifest,
            scenario=scenario,
        )
        formal_identity_complete = formal_identity_complete and report_identity_complete
        reports[report.arm] = report
    if "A" not in reports:
        raise ValueError(f"checkpoint has no published Arm A: {checkpoint_directory}")
    case_id = reports["A"].case_id.root
    gold = _load_gold(repository, case_id)
    blockers = frozen.get("blockers") or []
    scenario_blockers = scenario.get("blockers") or []
    lines = [
        f"# {PROFILE_LABELS[profile]} / C{cp} 诊断可读产物",
        "",
        "> 生成自冻结 Writer Context、Evidence Ledger 和 evaluator report。",
        "> 本文件是只读展示层，不改变原始 artifact、hash 或 Gate 结论。",
        "",
        "## Checkpoint 概览",
        "",
        "| 项目 | 值 |",
        "|---|---|",
        f"| Case | `{case_id}` |",
        f"| Profile | `{profile.value}` |",
        f"| Checkpoint | `C{cp}` |",
        f"| 请求 arms | `{manifest['arms']}` |",
        f"| 实际发布 arms | `{''.join(sorted(reports))}` |",
        f"| Paired comparable | `{str(bool(frozen.get('comparable'))).lower()}` |",
        f"| Pair blockers | `{blockers or '无'}` |",
        f"| Scenario completed | `{str(bool(scenario.get('completed'))).lower()}` |",
        f"| Formal case identity complete | `{str(formal_identity_complete).lower()}` |",
        f"| Scenario blockers | `{scenario_blockers or '无'}` |",
        f"| Freeze-before-reveal | `{frozen['freeze_receipt']['frozen_before_reveal']}` |",
        f"| Frozen pair artifact | `{frozen_ref['artifact_id']}` |",
        f"| Code fingerprint | `{manifest['code_source_fingerprint']}` |",
        "| Gate formula | "
        f"`{manifest['gate_metric_formula_version']}` / "
        f"`{manifest['gate_metric_formula_hash']}` |",
        "",
        "## 先看什么",
        "",
        "1. 每个 Arm 的“Writer 实际收到的完整 Context”是最终可消费内容。",
        "2. “逐 Gold 评测”说明该 Context 对目标事实交付得怎么样。",
        "3. “证据索引”把 Context 中的 ledger ID 映射回章节、span 和 commit。",
        "",
    ]
    if set(reports) == {"A"}:
        lines.extend(
            (
                "本 checkpoint 只有 Arm A 已发布 case artifact；B/C 不存在，"
                "不能把 paired fallback 指标当作 B/C 输出。",
                "",
            )
        )
    for arm in sorted(reports):
        lines.extend(_render_arm(arm, reports[arm], frozen, gold))
    summary: CheckpointSummary = {
        "case_id": case_id,
        "arms": "".join(sorted(reports)),
        "coverage": reports["A"].evaluation.weighted_coverage,
        "mandatory": reports["A"].evaluation.mandatory_hit_rate,
        "untraceable": reports["A"].evaluation.untraceable_rate,
        "contradiction": reports["A"].evaluation.contradiction_rate,
        "ready": reports["A"].assembly_status.value,
        "scenario_completed": bool(scenario.get("completed")),
        "formal_identity_complete": formal_identity_complete,
    }
    return "\n".join(lines).rstrip() + "\n", summary


def _aggregate_profile(
    run: Path,
    profile: BenchmarkInformationProfile,
) -> dict[str, float | int | bool]:
    root_manifest = _load_json(_checkpoint_directory(run, profile, 20) / "experiment_manifest.json")
    project = Path(str(root_manifest["project_directory"]))
    repository = ArtifactRepository(FilesystemObjectStore(project / "objects"))
    cases = []
    for cp in CHECKPOINTS:
        checkpoint_directory = _checkpoint_directory(run, profile, cp)
        manifest = _load_json(checkpoint_directory / "experiment_manifest.json")
        scenario = _load_json(checkpoint_directory / "scenario_run.json")
        case_report, _ = _load_diagnostic_case(
            checkpoint_directory / f"stage2m_case_C{cp}_A.json",
            manifest=manifest,
            scenario=scenario,
        )
        cases.append(case_report)
    aggregate_report = MemoryBenchmarkReporter(
        artifact_reader=repository.read_verified,
        enforce_formal_contract=False,
    ).aggregate(
        profile=profile,
        cases=tuple(cases),
    )
    return {
        "case_count": aggregate_report.case_count,
        "current": aggregate_report.current_state.value,
        "operational": aggregate_report.operational_plan.value,
        "historical": aggregate_report.historical.value,
        "untraceable": aggregate_report.untraceable_rate,
        "contradiction": aggregate_report.contradiction_rate,
        "trace_complete": aggregate_report.trace_complete,
        "gate_passed": aggregate_report.gate_passed,
    }


def main() -> int:
    args = parser().parse_args()
    repository = args.repository.resolve()
    base = repository / "reports/stage2m/writer_context_benchmark"
    output = args.output_directory or base / "human_readable_wp8_20260731"
    output.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[BenchmarkInformationProfile, int, CheckpointSummary, Path]] = []
    aggregates: dict[BenchmarkInformationProfile, dict[str, float | int | bool]] = {}
    for profile in PROFILES:
        run = base / profile.value / "qwen36_wp8_v1_20260731"
        profile_output = output / profile.value
        profile_output.mkdir(parents=True, exist_ok=True)
        for cp in CHECKPOINTS:
            markdown, summary = _render_checkpoint(repository, run, profile, cp)
            path = profile_output / f"C{cp}.md"
            path.write_text(markdown, encoding="utf-8")
            rows.append((profile, cp, summary, path))
        aggregates[profile] = _aggregate_profile(run, profile)

    lines = [
        "# Stage 2M WP8 诊断产物（人类可读版）",
        "",
        "> 生成日期：2026-07-31",
        "> 数据来源：`qwen36_wp8_v1_20260731` 冻结产物。",
        "> 这是展示层，不替代冻结 JSON，不改变 Gate M4 HOLD / Gate M5 incomplete。",
        "",
        "## 推荐阅读顺序",
        "",
        "1. 先按下表打开目标 profile/checkpoint。",
        "2. 直接读“Writer 实际收到的完整 Context”。",
        "3. 再看“逐 Gold 评测”，判断 Context 哪些内容有用、哪些缺失。",
        "4. 需要核查来源时展开“证据索引”。",
        "",
        "## 十个 checkpoint",
        "",
        "| Profile | Checkpoint | Case | Arms | A coverage | A mandatory | "
        "A untraceable | Context 状态 | Scenario | Formal identity |",
        "|---|---:|---|---|---:|---:|---:|---|---|---|",
    ]
    for profile, cp, summary, path in rows:
        relative = path.relative_to(output)
        lines.append(
            "| "
            + " | ".join(
                (
                    PROFILE_LABELS[profile],
                    f"[C{cp}]({relative.as_posix()})",
                    _cell(summary["case_id"]),
                    _cell(summary["arms"]),
                    f"{summary['coverage']:.3f}",
                    f"{summary['mandatory']:.3f}",
                    f"{summary['untraceable']:.3f}",
                    _cell(summary["ready"]),
                    "完成" if summary["scenario_completed"] else "生命周期未闭合",
                    "完整" if summary["formal_identity_complete"] else "缺六字段",
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## Gate M4 诊断五点复算",
            "",
            "下表由五个 Arm A 的冻结 evaluator bundle 以 diagnostic mode 重新聚合；"
            "不是 checkpoint macro coverage，也不是 formal Gate report。",
            "",
            "| Profile | Current-state | Operational/plan | Historical | "
            "Untraceable | Contradiction | Gate |",
            "|---|---:|---:|---:|---:|---:|---|",
        )
    )
    for profile in PROFILES:
        aggregate = aggregates[profile]
        lines.append(
            "| "
            + " | ".join(
                (
                    PROFILE_LABELS[profile],
                    f"{float(aggregate['current']):.3%}",
                    f"{float(aggregate['operational']):.3%}",
                    f"{float(aggregate['historical']):.3%}",
                    f"{float(aggregate['untraceable']):.3%}",
                    f"{float(aggregate['contradiction']):.3%}",
                    "PASS" if aggregate["gate_passed"] else "FAIL / HOLD",
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## 人类读者需要知道的限制",
            "",
            "- 十个 checkpoint 都有可读 Arm A；只有 VAC C20/C40 有落盘且可读的 B/C case 文件。",
            "- APC C20 虽请求 ABC，但 B 未 READY，C fallback，因此只发布 A。",
            "- 所有 checkpoint 的 `scenario_run.completed=false`；"
            "本目录解决可读性，不宣称流程闭环。",
            "- 旧 case JSON 均缺少当前 formal schema 的六个 identity/budget 字段；"
            "展示脚本只在内存中构造 diagnostic-only 占位值以读取旧文件，"
            "不回写、不 backfill、不促进 P3。",
            "- Context 中保留少量英文 canonical value 和内部 entity ID，"
            "这是当前产品质量问题，不是展示转换错误。",
            "- 每个文件记录 frozen pair artifact、Writer Ledger hash 和代码 fingerprint，"
            "可回到原始对象复核。",
            "",
        )
    )
    (output / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(output / "README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
