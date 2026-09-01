"""Compile the U6-A Writer and C/D lifecycle plan from frozen identities."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.u6_continuous_replay import (
    U6A_READOUT_LIFECYCLE,
    U6A_READOUT_PLAN_MEDIA_TYPE,
    U6ACanaryJob,
    U6AReadoutPlan,
    U6AReadoutTask,
    U6AReadoutTrack,
    U6BasisKind,
    U6BasisStatus,
    U6CheckpointBasisManifest,
)
from novel_agent.domain.v05_readout import (
    V05CampaignPhase,
    V05ReadoutCampaignManifest,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes

SCHEMA_VERSION = SchemaVersion("1.0.0")


class U6AReadoutPlanError(ValueError):
    """The frozen U6-A identities cannot form one safe lifecycle plan."""


@dataclass(frozen=True, slots=True)
class U6AReadoutPlanArtifacts:
    plan: U6AReadoutPlan
    plan_path: Path
    plan_ref: ArtifactRef
    object_root: Path


class U6AReadoutPlanCompiler:
    """Compile only identity metadata; no Gold, future text, or model call is read."""

    def __init__(
        self,
        *,
        basis_manifest_path: Path,
        source_readout_manifest_path: Path,
        output_root: Path,
        experiment_id: str,
    ) -> None:
        self.basis_manifest_path = basis_manifest_path.resolve()
        self.source_readout_manifest_path = source_readout_manifest_path.resolve()
        self.output_root = output_root.resolve()
        self.experiment_id = StableId(experiment_id).root

    def run(self) -> U6AReadoutPlanArtifacts:
        self._validate_paths()
        basis = U6CheckpointBasisManifest.model_validate_json(
            self.basis_manifest_path.read_bytes(), strict=True
        )
        if basis.status is not U6BasisStatus.FROZEN:
            raise U6AReadoutPlanError("U6-A readout plan requires a frozen basis manifest")
        source = V05ReadoutCampaignManifest.model_validate_json(
            self.source_readout_manifest_path.read_bytes(), strict=True
        )
        if source.phase is not V05CampaignPhase.SEED:
            raise U6AReadoutPlanError("U6-A seed compiler accepts only a frozen seed identity set")
        by_chapter = {node.checkpoint_chapter: node for node in basis.basis_nodes}
        if len(by_chapter) != len(basis.basis_nodes):
            raise U6AReadoutPlanError("basis manifest repeats a checkpoint declaration")

        tasks: list[U6AReadoutTask] = []
        for identity in source.readout_manifest.tasks:
            if identity.track.value not in {
                U6AReadoutTrack.QA.value,
                U6AReadoutTrack.CONTEXT.value,
            }:
                raise U6AReadoutPlanError(
                    f"unsupported public readout track: {identity.track.value}"
                )
            basis_node = by_chapter.get(identity.checkpoint_chapter)
            if basis_node is None or basis_node.basis_id is None:
                raise U6AReadoutPlanError(
                    f"readout {identity.task_id.root} has no frozen basis at "
                    f"C{identity.checkpoint_chapter}"
                )
            tasks.append(
                U6AReadoutTask(
                    task_id=identity.task_id,
                    track=identity.track.value,
                    checkpoint_chapter=identity.checkpoint_chapter,
                    basis_id=basis_node.basis_id,
                    source_task_id=identity.task_id,
                    lifecycle=U6A_READOUT_LIFECYCLE,
                )
            )

        canary_jobs: list[U6ACanaryJob] = []
        for basis_node in basis.basis_nodes:
            if basis_node.kind is U6BasisKind.PUBLIC_DECLARED and basis_node.jobs:
                # Public basis jobs are valid only when the canary explicitly
                # attaches to this same frozen declaration.
                pass
            for job_id in basis_node.jobs:
                track = self._job_track(job_id)
                canary_jobs.append(
                    U6ACanaryJob(
                        job_id=job_id,
                        track=track,
                        checkpoint_chapter=basis_node.checkpoint_chapter,
                        basis_id=basis_node.basis_id,
                        lifecycle=U6A_READOUT_LIFECYCLE,
                    )
                )

        object_root = self.output_root / "objects"
        object_root.mkdir(parents=True)
        repository = ArtifactRepository(FilesystemObjectStore(object_root))
        basis_ref = repository.put(
            canonical_json_bytes(basis.model_dump(mode="json")),
            "application/vnd.novel-agent.evaluation.u6-checkpoint-basis-manifest+json",
            SCHEMA_VERSION,
        )
        source_ref = repository.put(
            canonical_json_bytes(source.model_dump(mode="json", by_alias=True)),
            "application/vnd.novel-agent.evaluation.v05-readout-campaign-manifest+json",
            SCHEMA_VERSION,
        )
        plan = U6AReadoutPlan(
            campaign_id=StableId(f"campaign.u6a.readout.{self.experiment_id}"[:128]),
            basis_manifest_ref=basis_ref,
            source_readout_manifest_ref=source_ref,
            tasks=tuple(tasks),
            canary_jobs=tuple(canary_jobs),
            qa_task_count=sum(task.track == U6AReadoutTrack.QA.value for task in tasks),
            context_task_count=sum(task.track == U6AReadoutTrack.CONTEXT.value for task in tasks),
            canary_job_count=len(canary_jobs),
            status="READY",
        )
        plan_payload = canonical_json_bytes(plan.model_dump(mode="json", by_alias=True))
        plan_ref = repository.put(plan_payload, U6A_READOUT_PLAN_MEDIA_TYPE, SCHEMA_VERSION)
        plan_path = self.output_root / "u6a_readout_plan.json"
        plan_path.write_bytes(
            json.dumps(
                plan.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        return U6AReadoutPlanArtifacts(
            plan=plan,
            plan_path=plan_path,
            plan_ref=plan_ref,
            object_root=object_root,
        )

    def _validate_paths(self) -> None:
        if self.output_root.exists():
            raise U6AReadoutPlanError(
                f"U6-A readout plan identity already exists: {self.output_root}"
            )
        for path, label in (
            (self.basis_manifest_path, "basis manifest"),
            (self.source_readout_manifest_path, "source readout manifest"),
        ):
            if not path.is_file():
                raise U6AReadoutPlanError(f"{label} is missing: {path}")

    @staticmethod
    def _job_track(job_id: StableId) -> U6AReadoutTrack:
        value = job_id.root
        if value.startswith("croll-"):
            return U6AReadoutTrack.C_ROLL
        if value.startswith("dshort-"):
            return U6AReadoutTrack.D_SHORT
        if value.startswith("freerun-"):
            return U6AReadoutTrack.FREE_RUN
        raise U6AReadoutPlanError(f"unknown U6-A canary job identity: {value}")


__all__ = [
    "U6AReadoutPlanArtifacts",
    "U6AReadoutPlanCompiler",
    "U6AReadoutPlanError",
]
