#!/usr/bin/env python3
"""Repeatable offline five-checkpoint evaluator rescore (plan §R4/R6).

Loads the frozen paired artifacts, the verified semantic verifier receipts and
the canonical commit chain from the source project, then runs the same
evaluator/proof/manifest/receipt construction used by the production score()
path — without calling any model or mutating the source stores.  All new
artifacts (proof, evaluator manifest, derived semantic receipt, case report,
report index) are written to the existing CAS and re-read with verified reads.

Usage:
  STAGE2M_FROZEN_DB_URL=<read-only db url> \\
    .conda-env/bin/python scripts/rescore_stage2m_frozen_checkpoints.py \
      --source-project /tmp/ns-stage2m-frozen-checkpoint-repair-project-20260811-v1 \
      --output-root /tmp/ns-stage2m-frozen-checkpoint-evaluator-rescore-20260811-v3 \
      --case P001 --case P002 --case P003 --case P004 --case P005
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import Stage1MemoryNeed
from novel_agent.domain.writer_context import EvidenceLedger, FreezeReceipt, WriterContextPackage
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatcher
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_evaluation import MemoryBenchmarkEvaluator
from novel_agent.services.memory_benchmark_metric_contracts import GoldMetricContractBuilder
from novel_agent.services.observed_text_ancestry import (
    ObservedTextAncestryProof,
    TextRootAncestryEntry,
)

CASES = {
    "P001": {
        "case_id": "ZTJ-P001",
        "chapter": 20,
        "commit": "sha256:9ad34064a1343e2e5ee89296e7cbafa8ba9dfcdf385cfbd74ff3b2ccfb7432d6",
        "paired": "sha256:d495e6b9f711f70ccfdaf5c278862ffb50ee82798bf07f123abbbe05970af018",
        "semantic": "sha256:7a3681e1ae7f8708cbb08b1787b62b89931e04c326ba42af80957ae0b97b1c4d",
    },
    "P002": {
        "case_id": "ZTJ-P002",
        "chapter": 40,
        "commit": "sha256:378d71e6cb211782dff5cde651ac96fa55d8e83d600a6e1e7e92228b6046a0d6",
        "paired": "sha256:41bfc516d7c3cffbec8eaa58b78f39d2a8e4db286b8c9ca53e584bdb5e9a3cfe",
        "semantic": "sha256:0a05f3f919e25159902d2f750c1b2d16da6b5a836928ad047fe1ed54452e6981",
    },
    "P003": {
        "case_id": "ZTJ-P003",
        "chapter": 60,
        "commit": "sha256:86c060c6f10b9cf4d7a47618f7e0f339ec9adc5e2d33c2461ecb3ad1286e4bd0",
        "paired": "sha256:569ad56b9f51110d40a5567ac96c85f96b9ef488c626eb90cf6b00d40357a3cd",
        "semantic": "sha256:566df8609400723f676d99bce6578f8d2b76d940eb513f83a094fbda2ad25851",
    },
    "P004": {
        "case_id": "ZTJ-P004",
        "chapter": 80,
        "commit": "sha256:ba7c17cd3f91c47f425f68b26cf77471c8029c757cfb64622fadf1ba22dca57d",
        "paired": "sha256:2f95d13bc8cdcd1243fcf0414bf8ade27427dd83e2b0781314dd7fb43de6a785",
        "semantic": "sha256:56bf21d0bf8b56c787d28e385c013b87bc8dcdbb7d9618bd522e72826bdadb1b",
    },
    "P005": {
        "case_id": "ZTJ-P005",
        "chapter": 95,
        "commit": "sha256:8bb66f7d10cef9b8859766b4bb4126a6791c506e6f936287608595527ff254fd",
        "paired": "sha256:0e135b907fd5e5bd8645342625477e18cf65c80e61a889403cc027fe18e0b3e0",
        "semantic": "sha256:0cc27b1247369062722ebb3f97d7fa23b4470e0fcd0c4e1f40cdb009331ef670",
    },
}
PROOF_MEDIA_TYPE = "application/vnd.novel-agent.observed-text-ancestry-proof+json"
DERIVED_RECEIPT_MEDIA_TYPE = "application/vnd.novel-agent.stage2m-evaluator-semantic-receipt+json"


def _read_object(objects_root: Path, artifact_id: str) -> bytes:
    key = artifact_id.removeprefix("sha256:")
    return (objects_root / "sha256" / key[:2] / key).read_bytes()


def _artifact_ref(artifact_id: str, media_type: str, byte_length: int) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId(artifact_id),
        media_type=media_type,
        byte_length=byte_length,
        schema_version=SchemaVersion("1.0.0"),
    )


def _build_proof(
    repository: ArtifactRepository,
    objects_root: Path,
    engine: object,
    bundle: object,
    case_spec: dict[str, str],
) -> tuple[ObservedTextAncestryProof, dict[ArtifactId, TextRootDocument]]:
    case = next(item for item in bundle.case_manifests if item.case_id.root == case_spec["case_id"])
    compiled_text = next(
        root for root in bundle.text_roots if root.root_hash == case.input_text_root
    )
    entries: list[TextRootAncestryEntry] = []
    text_roots: dict[ArtifactId, TextRootDocument] = {}
    seen: set[str] = set()
    cursor: str | None = case_spec["commit"]
    checkpoint_ref: ArtifactRef | None = None
    checkpoint_hash: ArtifactId | None = None
    with engine.connect() as conn:
        while cursor is not None:
            if cursor in seen:
                raise ValueError("commit ancestry cycle")
            seen.add(cursor)
            row = conn.execute(
                text("SELECT manifest_json FROM project_commit WHERE commit_id=:c"),
                {"c": cursor},
            ).fetchone()
            if row is None:
                raise ValueError(f"commit manifest missing: {cursor}")
            manifest = row[0]
            text_root_ref_dict = manifest["text_root"]
            artifact_id = text_root_ref_dict["artifact_id"]
            text_bytes = _read_object(objects_root, artifact_id)
            text_doc = TextRootDocument.model_validate_json(text_bytes.decode("utf-8"))
            ref = _artifact_ref(
                artifact_id,
                text_root_ref_dict.get("media_type", "application/json"),
                text_root_ref_dict.get("byte_length", len(text_bytes)),
            )
            if checkpoint_ref is None:
                checkpoint_ref = ref
                checkpoint_hash = text_doc.root_hash
            entries.append(
                TextRootAncestryEntry(
                    commit_id=CommitId(cursor),
                    text_root_ref=ref,
                    text_root_logical_hash=text_doc.root_hash,
                )
            )
            text_roots[text_doc.root_hash] = text_doc
            parents = manifest.get("parent_commit_ids") or []
            if len(parents) > 1:
                raise ValueError("commit ancestry is not single-parent")
            cursor = parents[0] if parents else None
    engine.dispose()
    if checkpoint_ref is None or checkpoint_hash is None:
        raise ValueError("checkpoint TextRoot unresolved")
    proof = ObservedTextAncestryProof.build(
        benchmark_content_hash=bundle.content_hash,
        case_id=case.case_id,
        profile="author_plan_conditioned",
        checkpoint_chapter=case.history_range[1],
        checkpoint_commit=CommitId(case_spec["commit"]),
        checkpoint_text_root_ref=checkpoint_ref,
        checkpoint_text_root_hash=checkpoint_hash,
        ancestry=tuple(entries),
        case_input_text_root_hash=compiled_text.root_hash,
    )
    text_roots[compiled_text.root_hash] = compiled_text
    return proof, text_roots


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-project", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--case", action="append", required=True)
    args = parser.parse_args()
    source_objects = args.source_project / "objects"
    if not source_objects.is_dir():
        print(f"source objects missing: {source_objects}", file=sys.stderr)
        return 2
    db_url = os.environ.get("STAGE2M_FROZEN_DB_URL")
    if not db_url:
        print("STAGE2M_FROZEN_DB_URL is required", file=sys.stderr)
        return 2
    repository = ArtifactRepository(FilesystemObjectStore(source_objects))
    bundle = HumanBenchmarkCompiler().compile(
        Path("benchmarks/private/ztj_memory_pilot_v0.1").resolve()
    )
    engine = create_engine(db_url)
    args.output_root.mkdir(parents=True, exist_ok=True)
    for short in args.case:
        case_spec = CASES[short]
        case = next(
            item for item in bundle.case_manifests if item.case_id.root == case_spec["case_id"]
        )
        paired = json.loads(_read_object(source_objects, case_spec["paired"]).decode("utf-8"))
        deterministic = paired.get("deterministic") or {}
        writer_context = deterministic.get("writer_context")
        evidence_ledger = deterministic.get("evidence_ledger")
        gold_items = (
            *case.observed_use_gold,
            *case.operational_constraint_gold,
            *case.plan_obligation_gold,
        )
        gold_items = tuple(
            item for item in gold_items if case.information_profile in item.applicable_profiles
        )
        if (
            writer_context is None
            or evidence_ledger is None
            or paired.get("freeze_receipt") is None
        ):
            print(f"{short}: no ready writer context; skipping")
            continue
        proof, text_roots = _build_proof(repository, source_objects, engine, bundle, case_spec)
        # 1. proof -> CAS (verified by construction; re-read to confirm)
        proof_ref = repository.put(
            json.dumps(proof.model_dump(mode="json"), ensure_ascii=False).encode("utf-8"),
            PROOF_MEDIA_TYPE,
            SchemaVersion("1.0.0"),
        )
        repository.read_verified(proof_ref)
        # 2. evaluator manifest with proof ref
        matcher = GoldEvidenceMatcher(ancestry_proof=proof, text_roots=text_roots)
        manifest_id = StableId(f"evaluator-manifest.{case_spec['case_id']}.author_plan_conditioned")
        builder = GoldMetricContractBuilder(
            repository,
            matcher=matcher,
            ancestry_proof_ref=proof_ref,
        )
        _manifest, manifest_ref = builder.build_manifest(
            gold_items=gold_items,
            evaluator_manifest_id=manifest_id,
        )
        repository.read_verified(manifest_ref)
        descriptors = builder.build(
            gold_items=gold_items,
            evaluator_manifest_id=manifest_id,
            evaluator_manifest_hash=manifest_ref.artifact_id,
        )
        # 3. verified read of the source semantic receipt; persist a derived
        #    evaluator-side receipt binding source + proof + manifest + matcher.
        semantic_artifact_id = case_spec["semantic"]
        semantic_bytes = _read_object(source_objects, semantic_artifact_id)
        semantic_payload = json.loads(semantic_bytes.decode("utf-8"))
        source_semantic_ref = _artifact_ref(
            semantic_artifact_id,
            "application/vnd.novel-agent.stage2m-semantic-verifier-receipt+json",
            len(semantic_bytes),
        )
        repository.read_verified(source_semantic_ref)
        judgments = semantic_payload.get("batch", {}).get("judgments") or []
        from novel_agent.services.memory_benchmark_evaluation import SemanticGoldJudgment

        semantic_judgments = {
            StableId(item["gold_id"]): SemanticGoldJudgment.model_validate_json(json.dumps(item))
            for item in judgments
        }
        derived_payload = {
            "schema_version": "1.0.0",
            "source_semantic_receipt_ref": semantic_artifact_id,
            "source_semantic_receipt_hash": semantic_artifact_id,
            "evaluator_manifest_ref": manifest_ref.artifact_id.root,
            "evaluator_manifest_hash": manifest_ref.artifact_id.root,
            "ancestry_proof_ref": proof_ref.artifact_id.root,
            "ancestry_proof_hash": proof.proof_hash.root,
            "matcher_version": matcher.version,
            "evaluator_version": MemoryBenchmarkEvaluator.version,
            "normalized_judgments": [
                {"gold_id": item["gold_id"], "all_claims_support": item["all_claims_support"]}
                for item in judgments
            ],
        }
        derived_ref = repository.put(
            json.dumps(derived_payload, ensure_ascii=False).encode("utf-8"),
            DERIVED_RECEIPT_MEDIA_TYPE,
            SchemaVersion("1.0.0"),
        )
        repository.read_verified(derived_ref)
        # 4. evaluate with the shared matcher and derived receipt identity
        package = WriterContextPackage.model_validate_json(json.dumps(writer_context))
        ledger = EvidenceLedger.model_validate_json(json.dumps(evidence_ledger))
        receipt = FreezeReceipt.model_validate_json(json.dumps(paired["freeze_receipt"]))
        evaluator = MemoryBenchmarkEvaluator(evidence_matcher=matcher)
        report = evaluator.evaluate(
            package=package,
            evidence_ledger=ledger,
            gold_items=gold_items,
            profile=case.information_profile,
            freeze_receipt=receipt,
            evaluator_manifest_id=manifest_id,
            evaluator_manifest_ref=manifest_ref,
            evaluator_manifest_hash=manifest_ref.artifact_id,
            gold_metric_descriptors=descriptors,
            semantic_judgments=semantic_judgments,
            verifier_receipt_ref=derived_ref,
            stage_loss_diagnostics=(),
        )
        five_segments = evaluator.evaluate_five_segments(
            needs=tuple(
                Stage1MemoryNeed.model_validate_json(json.dumps(item))
                for item in (paired.get("generated_needs") or ())
            ),
            gold_need_specs=case.gold_need_specs,
            plan_goals=tuple(
                goal
                for goal in (
                    next(
                        (p for p in bundle.plan_roots if p.root_hash == case.input_plan_root),
                        None,
                    ).chapter_goals
                    or ()
                )
                if case.target_range[0] <= goal.chapter_index <= case.target_range[1]
            ),
            gold_items=gold_items,
            evidence_ledger=ledger,
            completion_accuracy=report.weighted_coverage,
            per_gold_comparisons=report.comparisons,
            future_leakage_count=deterministic.get("future_leakage_count", 0),
            entity_id_by_label=None,
            planner_fallback_used=bool(paired.get("planner_fallback_used")),
            planner_fallback_reason=paired.get("planner_fallback_reason"),
            planner_artifact_ref=(
                ArtifactRef(
                    artifact_id=ArtifactId(paired["planner_artifact_ref"]["artifact_id"]),
                    media_type=paired["planner_artifact_ref"].get(
                        "media_type", "application/vnd.novel-agent.planner-invocation+json"
                    ),
                    byte_length=paired["planner_artifact_ref"].get("byte_length", 1),
                    schema_version=paired["planner_artifact_ref"].get("schema_version", "1.0.0"),
                )
                if paired.get("planner_artifact_ref")
                else None
            ),
            grounded_status_counts=tuple(paired.get("grounded_status_counts") or (0, 0, 0)),
            profile=case.information_profile,
        )
        report = report.model_copy(update={"five_segments": five_segments})
        # 5. persist the case report + index; all refs already verified
        report_ref = repository.put(
            report.model_dump_json(indent=2).encode("utf-8"),
            "application/vnd.novel-agent.stage2m-per-gold-score+json",
            SchemaVersion("1.0.0"),
        )
        repository.read_verified(report_ref)
        out_dir = args.output_root / short
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "proof.json").write_text(proof.model_dump_json(indent=2), encoding="utf-8")
        (out_dir / "evaluator_manifest.json").write_text(
            json.dumps(
                {
                    "manifest_ref": manifest_ref.artifact_id.root,
                    "ancestry_proof_ref": proof_ref.artifact_id.root,
                    "manifest_version": builder.evaluator_manifest_version,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (out_dir / "derived_semantic_receipt.json").write_text(
            json.dumps(derived_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "rescore_report.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        observed = tuple(c for c in report.comparisons if c.eligibility.value == "observed_claim")
        matched = sum(1 for c in observed if c.matched_evidence_ledger_ids)
        print(
            f"{short}/C{case_spec['chapter']}: observed={len(observed)} "
            f"plan_axis={report.plan_axis_only_count} matcher={matched}/{len(observed)} "
            f"chain_len={len(proof.ancestry)} proof={proof.proof_hash.root[:16]} "
            f"manifest_proof_ref={'set' if manifest_ref.artifact_id.root else 'null'}"
        )
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
