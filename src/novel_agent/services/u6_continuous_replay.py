"""U6-A one-pass V0.5 checkpoint basis preparation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.benchmark import PlanRootDocument
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
)
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.u6_continuous_replay import (
    U6_BASIS_MANIFEST_MEDIA_TYPE,
    U6BasisKind,
    U6BasisStatus,
    U6CheckpointBasis,
    U6CheckpointBasisManifest,
    U6CheckpointLineage,
    U6ContinuousReplayReport,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import manifest_commit_id
from novel_agent.services.content_addressing import (
    canonical_json_bytes,
    content_id,
    plan_root_content_id,
    world_root_content_id,
)
from novel_agent.services.u4s_seed_readout import U4SPublicCorpus

SCHEMA_VERSION = SchemaVersion("1.0.0")
ZERO_ARTIFACT = ArtifactId("sha256:" + "0" * 64)
ZERO_COMMIT = CommitId("sha256:" + "0" * 64)
INDEX_LINEAGE_MEDIA_TYPE = "application/vnd.novel-agent.u6-index-lineage+json"


class U6ContinuousReplayError(ValueError):
    """The U6-A basis protocol cannot be proven safely."""


@dataclass(frozen=True, slots=True)
class U6ContinuousReplayArtifacts:
    """Paths and typed outputs of one immutable basis preparation."""

    manifest: U6CheckpointBasisManifest
    report: U6ContinuousReplayReport
    manifest_path: Path
    report_path: Path
    object_root: Path


class U6ContinuousReplayService:
    """Build one sequential basis chain without running a second checkpoint replay."""

    def __init__(
        self,
        *,
        bundle_root: Path,
        output_root: Path,
        experiment_id: str,
        project_id: ProjectId | None = None,
    ) -> None:
        self.bundle_root = bundle_root.resolve()
        self.output_root = output_root.resolve()
        self.experiment_id = StableId(experiment_id).root
        self.project_id = project_id or ProjectId(f"project.u6a.{self.experiment_id}"[:128])

    def run(self) -> U6ContinuousReplayArtifacts:
        self._validate_paths()
        template = self._load_basis_template()
        public_chapters = self._load_public_chapters()
        internal_chapters = tuple(
            node.checkpoint_chapter
            for node in template.basis_nodes
            if node.kind is U6BasisKind.INTERNAL_N_MINUS_1
        )
        template.validate_shape(
            public_chapters=public_chapters,
            internal_chapters=internal_chapters,
        )
        self._validate_d_short_attachments(template)

        object_root = self.output_root / "objects"
        object_root.mkdir(parents=True)
        repository = ArtifactRepository(FilesystemObjectStore(object_root))
        requested_chapters = tuple(node.checkpoint_chapter for node in template.basis_nodes)
        corpus = U4SPublicCorpus(self.bundle_root)
        if corpus.chapter_count != 300:
            raise U6ContinuousReplayError(
                f"U6-A requires C1..C300, received C1..C{corpus.chapter_count}"
            )

        roots_by_chapter = dict(corpus.continuous_text_roots(requested_chapters))
        if tuple(sorted(roots_by_chapter)) != tuple(sorted(requested_chapters)):
            raise U6ContinuousReplayError("continuous replay did not freeze every basis chapter")

        frozen_nodes: list[U6CheckpointBasis] = []
        lineage: list[U6CheckpointLineage] = []
        previous_commit = ZERO_COMMIT
        final_text = roots_by_chapter[max(roots_by_chapter)]
        for pending in template.basis_nodes:
            text = roots_by_chapter[pending.checkpoint_chapter]
            text_ref = self._put_root(
                repository,
                text.model_dump(mode="json"),
                "application/vnd.novel-agent.text-root+json",
                TextRootRef,
            )
            plan = PlanRootDocument(root_hash=ZERO_ARTIFACT, schema_version=SCHEMA_VERSION)
            plan = plan.model_copy(update={"root_hash": plan_root_content_id(plan)})
            plan_ref = self._put_root(
                repository,
                plan.model_dump(mode="json"),
                "application/vnd.novel-agent.plan-root+json",
                PlanRootRef,
            )
            world = WorldRootDocument(
                root_hash=ZERO_ARTIFACT,
                schema_version=SCHEMA_VERSION,
                source_commit=previous_commit,
            )
            world = world.model_copy(update={"root_hash": world_root_content_id(world)})
            world_ref = self._put_root(
                repository,
                world.model_dump(mode="json"),
                "application/vnd.novel-agent.world-root+json",
                WorldRootRef,
            )
            profile_ref = self._put_root(
                repository,
                {
                    "benchmark_id": template.benchmark_id,
                    "benchmark_version": template.version,
                    "checkpoint_chapter": pending.checkpoint_chapter,
                    "information_profile": "visible_at_cutoff",
                    "source": "u6a-continuous-replay",
                },
                "application/vnd.novel-agent.project-profile-root+json",
                ProjectProfileRootRef,
            )
            reference_ref = self._put_root(
                repository,
                {
                    "benchmark_id": template.benchmark_id,
                    "benchmark_version": template.version,
                    "kind": "u6a-reference-root",
                },
                "application/vnd.novel-agent.reference-root+json",
                ReferenceRootRef,
            )
            manifest = RootManifest(
                project_id=self.project_id,
                schema_version=SCHEMA_VERSION,
                text_root=text_ref,
                plan_root=plan_ref,
                world_root=world_ref,
                reference_root=reference_ref,
                project_profile_root=profile_ref,
                parent_commit_ids=() if previous_commit == ZERO_COMMIT else (previous_commit,),
            )
            commit_id = manifest_commit_id(manifest)
            snapshot_id = StableId(f"snapshot.u6a.{commit_id.root.removeprefix('sha256:')[:48]}")
            index_lineage_ref = repository.put(
                canonical_json_bytes(
                    {
                        "backend_profile": "benchmark-text-replay",
                        "checkpoint_chapter": pending.checkpoint_chapter,
                        "commit_id": commit_id.root,
                        "snapshot_id": snapshot_id.root,
                        "source_text_root": text.root_hash.root,
                        "unit_count": sum(
                            len(blocks)
                            for chapter in text.chapters
                            for scene in chapter.scenes
                            for blocks in (scene.blocks,)
                        ),
                    }
                ),
                INDEX_LINEAGE_MEDIA_TYPE,
                SCHEMA_VERSION,
            )
            memory_identity = content_id(
                {
                    "commit_id": commit_id.root,
                    "plan_root": plan_ref.artifact_id.root,
                    "profile_root": profile_ref.artifact_id.root,
                    "text_root": text_ref.artifact_id.root,
                    "world_root": world_ref.artifact_id.root,
                }
            )
            control_identity = content_id(
                {
                    "control": "u6a-identity-only-control",
                    "final_stream_root": final_text.root_hash.root,
                    "checkpoint": pending.checkpoint_chapter,
                    "memory_identity": memory_identity.root,
                }
            )
            frozen_nodes.append(
                pending.model_copy(
                    update={
                        "status": U6BasisStatus.FROZEN,
                        "commit_id": commit_id,
                        "snapshot_id": snapshot_id,
                        "plan_root_ref": plan_ref,
                        "text_root_ref": text_ref,
                        "world_root_ref": world_ref,
                        "profile_root_ref": profile_ref,
                    }
                )
            )
            lineage.append(
                U6CheckpointLineage(
                    basis_id=pending.basis_id,
                    checkpoint_chapter=pending.checkpoint_chapter,
                    commit_id=commit_id,
                    snapshot_id=snapshot_id,
                    plan_root_ref=plan_ref,
                    text_root_ref=text_ref,
                    world_root_ref=world_ref,
                    profile_root_ref=profile_ref,
                    index_lineage_ref=index_lineage_ref,
                    memory_identity_before=memory_identity,
                    memory_identity_after=memory_identity,
                    control_replay_identity=control_identity,
                    evaluation_namespace="PENDING_READOUT",
                    identity_match=True,
                )
            )
            previous_commit = commit_id

        frozen_manifest = template.model_copy(
            update={
                "status": U6BasisStatus.FROZEN,
                "status_note": (
                    "U6-A basis frozen after one sequential public-stream ingest; "
                    "Writer readout and evaluator discard remain pending."
                ),
                "basis_nodes": tuple(frozen_nodes),
            }
        )
        manifest_ref = repository.put(
            canonical_json_bytes(frozen_manifest.model_dump(mode="json")),
            U6_BASIS_MANIFEST_MEDIA_TYPE,
            SCHEMA_VERSION,
        )
        control_replay_identity = content_id(
            {
                "algorithm": "u6a-identity-only-control-v1",
                "basis_commits": [item.commit_id.root for item in frozen_nodes if item.commit_id],
                "final_stream_root": final_text.root_hash.root,
            }
        )
        report = U6ContinuousReplayReport(
            campaign_id=StableId(f"campaign.u6a.{self.experiment_id}"[:128]),
            run_id=RunId(f"run.u6a.{self.experiment_id}"[:128]),
            project_id=self.project_id,
            benchmark_id=frozen_manifest.benchmark_id,
            benchmark_version=frozen_manifest.version,
            basis_manifest_ref=manifest_ref,
            chapters_declared=300,
            chapters_ingested=300,
            ingest_passes=1,
            public_basis_count=sum(
                node.kind is U6BasisKind.PUBLIC_DECLARED for node in frozen_nodes
            ),
            internal_basis_count=sum(
                node.kind is U6BasisKind.INTERNAL_N_MINUS_1 for node in frozen_nodes
            ),
            basis_count=len(frozen_nodes),
            canary_job_count=sum(len(node.jobs) for node in frozen_nodes),
            expected_readout_task_count=81,
            completed_readout_task_count=0,
            evaluation_discard_count=0,
            future_leakage_count=0,
            duplicate_checkpoint_declarations=0,
            control_replay_identity=control_replay_identity,
            lineage=tuple(lineage),
            status="BASIS_FROZEN",
        )
        manifest_path = self.output_root / "checkpoint_basis_manifest.json"
        report_path = self.output_root / "u6_continuous_replay_report.json"
        manifest_path.write_bytes(
            json.dumps(
                frozen_manifest.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        report_path.write_bytes(
            json.dumps(
                report.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        return U6ContinuousReplayArtifacts(
            manifest=frozen_manifest,
            report=report,
            manifest_path=manifest_path,
            report_path=report_path,
            object_root=object_root,
        )

    def _validate_paths(self) -> None:
        if self.output_root.exists():
            raise U6ContinuousReplayError(
                f"U6-A output identity already exists: {self.output_root}"
            )
        if not self.bundle_root.is_dir():
            raise U6ContinuousReplayError(f"benchmark bundle is missing: {self.bundle_root}")
        if not (self.bundle_root / "public" / "checkpoints.json").is_file():
            raise U6ContinuousReplayError("public checkpoint protocol is missing")
        if not (
            self.bundle_root / "private" / "basis" / "checkpoint_basis_manifest.json"
        ).is_file():
            raise U6ContinuousReplayError("pending checkpoint basis manifest is missing")

    def _load_basis_template(self) -> U6CheckpointBasisManifest:
        path = self.bundle_root / "private" / "basis" / "checkpoint_basis_manifest.json"
        try:
            result = U6CheckpointBasisManifest.model_validate_json(path.read_bytes(), strict=True)
        except Exception as error:
            raise U6ContinuousReplayError(
                "checkpoint basis manifest is not schema-valid"
            ) from error
        if result.status is not U6BasisStatus.PENDING_REPLAY:
            raise U6ContinuousReplayError("U6-A refuses to reuse an already frozen basis")
        return result

    def _load_public_chapters(self) -> tuple[int, ...]:
        path = self.bundle_root / "public" / "checkpoints.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            checkpoints = payload["checkpoints"]
            chapters = tuple(int(item["after_chapter"]) for item in checkpoints)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise U6ContinuousReplayError("public checkpoint protocol is malformed") from error
        if len(chapters) != 16 or len(chapters) != len(set(chapters)):
            raise U6ContinuousReplayError("U6-A requires 16 unique public checkpoints")
        if any(chapter < 1 or chapter > 300 for chapter in chapters):
            raise U6ContinuousReplayError("public checkpoint is outside C1..C300")
        by_chapter = {int(item["after_chapter"]): tuple(item["tracks"]) for item in checkpoints}
        if by_chapter.get(100) != ("novelmem_context",):
            raise U6ContinuousReplayError("C100 must carry Context only")
        if by_chapter.get(300) != ("novelmem_qa",):
            raise U6ContinuousReplayError("C300 must carry QA only")
        return chapters

    def _validate_d_short_attachments(self, manifest: U6CheckpointBasisManifest) -> None:
        import yaml

        path = self.bundle_root / "annotations" / "track_d_short_selection.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        cases = payload.get("cases") if isinstance(payload, dict) else None
        if not isinstance(cases, list):
            raise U6ContinuousReplayError("D-SHORT selection is malformed")
        by_chapter = {node.checkpoint_chapter: node for node in manifest.basis_nodes}
        for case in cases:
            if not isinstance(case, dict):
                raise U6ContinuousReplayError("D-SHORT selection contains a non-object case")
            target = int(case["target_chapter"])
            basis_chapter = target - 1
            node = by_chapter.get(basis_chapter)
            if node is None:
                raise U6ContinuousReplayError(
                    f"D-SHORT target C{target} has no C{basis_chapter} basis"
                )
            expected_job = StableId(f"dshort-{target}")
            if expected_job not in node.jobs:
                raise U6ContinuousReplayError(
                    f"C{basis_chapter} basis is missing {expected_job.root} attachment"
                )

    @staticmethod
    def _put_root(
        repository: ArtifactRepository,
        payload: dict[str, Any],
        media_type: str,
        root_type: type[ArtifactRef],
    ) -> Any:
        ref = repository.put(canonical_json_bytes(payload), media_type, SCHEMA_VERSION)
        return root_type.model_validate(ref.model_dump(mode="python"))


__all__ = [
    "U6ContinuousReplayArtifacts",
    "U6ContinuousReplayError",
    "U6ContinuousReplayService",
]
