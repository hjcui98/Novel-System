#!/usr/bin/env python3
"""Create one audited Canonical commit that repairs an omitted benchmark prelude."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.artifacts import RootKind, TextRootRef, WorldRootRef
from novel_agent.domain.benchmark import PreludeDocument, TextRootDocument
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ChangeOperation,
    ChangeOperationType,
    CommitRequest,
    CommitStatus,
    ObservedChangeSet,
    ValidationReport,
    ValidationStatus,
    WorldRecordKind,
)
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.text import (
    EvidenceRef,
    EvidenceSupportStatus,
    TextSpanRef,
)
from novel_agent.domain.world import StateRecord, StoryTime, TruthClass
from novel_agent.services.artifacts import ArtifactRepository, sha256_id
from novel_agent.services.benchmark_importer import quote_hash, validate_evidence_ref
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.overlay import WorldOverlay
from novel_agent.services.text_timeline import SequentialTextRootService

_MERIDIAN_EVIDENCE = (
    "陈长生的病是因为先天体虚，身体里的九段经脉不能相连"  # noqa: RUF001
)
_MERIDIAN_STATE_ID = StableId("state.chen-changsheng-meridian-condition")
_CHEN_CHANGSHENG_ID = StableId("entity.bootstrap.chen-changsheng")


class PreludeBackfillError(RuntimeError):
    """The requested historical repair is not safe to commit."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--project-directory", required=True, type=Path)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-base-commit", required=True)
    parser.add_argument("--expected-benchmark-hash", required=True)
    parser.add_argument("--expected-prelude-document-hash", required=True)
    parser.add_argument(
        "--receipt",
        type=Path,
        help="defaults to PROJECT_DIRECTORY/prelude_backfill_receipt.json",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    project_directory = args.project_directory.resolve()
    object_directory = project_directory / "objects"
    progress_path = project_directory / "progress_manifest.json"
    if not object_directory.is_dir():
        raise PreludeBackfillError(f"missing project artifact directory: {object_directory}")
    if not progress_path.is_file():
        raise PreludeBackfillError(f"missing progress manifest: {progress_path}")
    progress = json.loads(progress_path.read_text("utf-8"))
    accepted_chapter = progress.get("last_accepted_chapter")
    if not isinstance(accepted_chapter, int) or accepted_chapter < 1:
        raise PreludeBackfillError("progress manifest has no accepted chapter history")

    expected_base = CommitId(args.expected_base_commit)
    if progress.get("last_accepted_commit") != expected_base.root:
        raise PreludeBackfillError("progress manifest does not match the expected base")
    expected_benchmark_hash = ArtifactId(args.expected_benchmark_hash)
    expected_prelude_hash = ArtifactId(args.expected_prelude_document_hash)
    bundle = HumanBenchmarkCompiler().compile(args.source)
    if bundle.content_hash != expected_benchmark_hash:
        raise PreludeBackfillError("compiled benchmark hash differs from the pinned source")
    latest = max(bundle.case_manifests, key=lambda item: item.history_range[1])
    reference = next(item for item in bundle.text_roots if item.root_hash == latest.input_text_root)
    if reference.prelude is None:
        raise PreludeBackfillError("pinned benchmark reference has no prelude")
    actual_prelude_hash = content_id(reference.prelude.model_dump(mode="json"))
    if actual_prelude_hash != expected_prelude_hash:
        raise PreludeBackfillError("benchmark prelude hash differs from the pinned document")

    engine = build_engine(args.database_url)
    try:
        factory = build_session_factory(engine)
        commits = CommitService(factory)
        if commits.current_commit(latest.project_id) != expected_base:
            raise PreludeBackfillError("current project commit differs from expected base")
        manifest = commits.load_manifest(expected_base)
        if manifest.project_id != latest.project_id:
            raise PreludeBackfillError("benchmark and Canonical project ids differ")
        artifacts = ArtifactRepository(FilesystemObjectStore(object_directory))
        current = TextRootDocument.model_validate_json(
            artifacts.read_verified(manifest.text_root),
            strict=True,
        )
        current_world = WorldRootDocument.model_validate_json(
            artifacts.read_verified(manifest.world_root),
            strict=True,
        )
        expected_chapters = tuple(
            chapter for chapter in reference.chapters if chapter.chapter_index <= accepted_chapter
        )
        if len(expected_chapters) != accepted_chapter:
            raise PreludeBackfillError("benchmark reference lacks the accepted chapter prefix")
        if current.chapters != expected_chapters:
            raise PreludeBackfillError(
                "Canonical chapters differ from the pinned benchmark reference"
            )

        repaired, advance = SequentialTextRootService().backfill_missing_prelude(
            current,
            StableId("source.chapter.prelude"),
            reference.prelude,
        )
        prelude_ref = artifacts.put(
            canonical_json_bytes(reference.prelude.model_dump(mode="json")),
            "application/vnd.novel-agent.prelude+json",
            manifest.schema_version,
        )
        text_artifact = artifacts.put(
            canonical_json_bytes(repaired.model_dump(mode="json")),
            "application/vnd.novel-agent.text-root+json",
            manifest.schema_version,
        )
        advance_ref = artifacts.put(
            canonical_json_bytes(advance.model_dump(mode="json")),
            "application/vnd.novel-agent.text-root-advance-receipt+json",
            manifest.schema_version,
        )
        state, world_operation = _prelude_world_state(
            repaired,
            expected_base,
            reference.prelude,
        )
        if _CHEN_CHANGSHENG_ID not in {entity.entity_id for entity in current_world.entities}:
            raise PreludeBackfillError("Canonical World lacks the prelude state subject")
        if _MERIDIAN_STATE_ID in {item.state_id for item in current_world.states}:
            raise PreludeBackfillError("Canonical World already contains the prelude state")
        world_changes = ObservedChangeSet(
            change_set_id=StableId("changes.prelude-backfill.world"),
            base_commit=expected_base,
            source_artifact=prelude_ref,
            operations=(world_operation,),
        )
        repaired_world = WorldOverlay().apply(
            current_world,
            world_changes,
            canonical_commit=expected_base,
        )
        world_artifact = artifacts.put(
            canonical_json_bytes(repaired_world.model_dump(mode="json")),
            "application/vnd.novel-agent.world-root+json",
            manifest.schema_version,
        )
        proposed = manifest.model_copy(
            update={
                "text_root": TextRootRef(**text_artifact.model_dump()),
                "world_root": WorldRootRef(**world_artifact.model_dump()),
                "parent_commit_ids": (expected_base,),
            }
        )
        digest = actual_prelude_hash.root.removeprefix("sha256:")[:24]
        run_id = RunId(f"run.prelude-backfill.{digest}")
        bundle_id = StableId(f"bundle.prelude-backfill.{digest}")
        text_operation = ChangeOperation(
            operation_id=StableId(f"operation.prelude-backfill.{digest}"),
            root_kind=RootKind.TEXT,
            operation=ChangeOperationType.REPLACE,
            target_id=StableId("text-root.prelude"),
            payload={
                "operation": "backfill_missing_prelude",
                "previous_text_root": current.root_hash.root,
                "resulting_text_root": repaired.root_hash.root,
                "prelude_document_hash": actual_prelude_hash.root,
                "chapter_count_preserved": len(current.chapters),
            },
        )
        observed = ObservedChangeSet(
            change_set_id=StableId(f"changes.prelude-backfill.{digest}"),
            base_commit=expected_base,
            source_artifact=prelude_ref,
            operations=(text_operation, world_operation),
        )
        candidate = CandidateChangeBundle(
            bundle_id=bundle_id,
            project_id=manifest.project_id,
            run_id=run_id,
            base_commit=expected_base,
            observed_changes=observed,
            proposed_roots=proposed,
            produced_artifacts=(
                prelude_ref,
                text_artifact,
                world_artifact,
                advance_ref,
            ),
        )
        now = datetime.now(UTC)
        validation = ValidationReport(
            report_id=StableId(f"validation.prelude-backfill.{digest}"),
            bundle_id=bundle_id,
            status=ValidationStatus.PASSED,
            schema_version=manifest.schema_version,
            validation_profile="historical-source-backfill-v2",
            validated_at=now,
        )
        result = commits.commit(
            CommitRequest(
                request_id=StableId(f"request.prelude-backfill.{digest}"),
                project_id=manifest.project_id,
                base_commit=expected_base,
                idempotency_key=StableId(f"commit.prelude-backfill.{digest}"),
                bundle=candidate,
                validation_report=validation,
            )
        )
        if result.status is not CommitStatus.ACCEPTED or result.commit_id is None:
            raise PreludeBackfillError(
                f"Canonical backfill was not accepted: {result.reason or result.status.value}"
            )
        if commits.current_commit(manifest.project_id) != result.commit_id:
            raise PreludeBackfillError("accepted backfill is not the current project commit")

        progress["last_accepted_commit"] = result.commit_id.root
        progress.pop("workflow_pause", None)
        _atomic_json_write(progress_path, progress)
        receipt = {
            "status": "prelude_backfill_committed",
            "validation_profile": validation.validation_profile,
            "project_id": manifest.project_id.root,
            "base_commit": expected_base.root,
            "commit_id": result.commit_id.root,
            "benchmark_hash": bundle.content_hash.root,
            "source_id": advance.source_id.root,
            "prelude_document_hash": actual_prelude_hash.root,
            "previous_text_root": current.root_hash.root,
            "resulting_text_root": repaired.root_hash.root,
            "chapter_count_preserved": len(current.chapters),
            "world_state": state.model_dump(mode="json"),
            "previous_world_root": current_world.root_hash.root,
            "resulting_world_root": repaired_world.root_hash.root,
            "text_root_advance_receipt": advance.model_dump(mode="json"),
            "produced_artifacts": [
                item.model_dump(mode="json") for item in candidate.produced_artifacts
            ],
            "committed_at": (
                None if result.committed_at is None else result.committed_at.isoformat()
            ),
        }
        receipt_path = (
            args.receipt.resolve()
            if args.receipt is not None
            else project_directory / "prelude_backfill_receipt.json"
        )
        _write_immutable_json(receipt_path, receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        engine.dispose()


def _prelude_world_state(
    text_root: TextRootDocument,
    base_commit: CommitId,
    prelude: PreludeDocument,
) -> tuple[StateRecord, ChangeOperation]:
    scenes = getattr(prelude, "scenes", ())
    matches = [
        (block, block.text.index(_MERIDIAN_EVIDENCE))
        for scene in scenes
        for block in scene.blocks
        if _MERIDIAN_EVIDENCE in block.text
    ]
    if len(matches) != 1:
        raise PreludeBackfillError("pinned meridian evidence must occur exactly once")
    block, start = matches[0]
    end = start + len(_MERIDIAN_EVIDENCE)
    evidence = EvidenceRef(
        evidence_id=StableId(
            "evidence.prelude-backfill."
            + content_id(
                {
                    "block_id": block.block_id.root,
                    "start": start,
                    "end": end,
                    "base_commit": base_commit.root,
                }
            ).root.removeprefix("sha256:")[:24]
        ),
        root_hash=text_root.root_hash,
        object_hash=sha256_id(block.text.encode("utf-8")),
        chapter_id=block.chapter_id,
        scene_id=block.scene_id,
        span=TextSpanRef(block_id=block.block_id, start=start, end=end),
        quote_hash=quote_hash(_MERIDIAN_EVIDENCE),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=base_commit,
    )
    validate_evidence_ref(evidence, text_root)
    state = StateRecord(
        state_id=_MERIDIAN_STATE_ID,
        subject_id=_CHEN_CHANGSHENG_ID,
        predicate="has_meridian_condition",
        value="nine_meridians_unconnected",
        valid_time=StoryTime(worldline="main", start_ordinal=0),
        evidence_refs=(evidence,),
        truth_class=TruthClass.ASSERTION,
    )
    operation = ChangeOperation(
        operation_id=StableId("operation.prelude-backfill.meridian-condition"),
        root_kind=RootKind.WORLD,
        operation=ChangeOperationType.CREATE,
        target_id=state.state_id,
        payload={
            "record_type": WorldRecordKind.STATE.value,
            "record": state.model_dump(mode="json"),
        },
        evidence_refs=(evidence,),
    )
    return state, operation


def _atomic_json_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
        encoding="utf-8",
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)


def _write_immutable_json(path: Path, payload: dict[str, object]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if path.exists():
        if path.read_bytes() != encoded:
            raise PreludeBackfillError(f"refusing to overwrite an existing receipt: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
