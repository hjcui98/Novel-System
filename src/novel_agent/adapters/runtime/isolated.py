"""Strict deterministic leaves admitted only by the isolated Stage 5 runner."""

from __future__ import annotations

from dataclasses import dataclass

from novel_agent.domain.artifacts import PlanRootRef, RootKind, RootManifest, TextRootRef
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ObservedChangeSet,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.creative_runtime import (
    AcceptedCandidateBinding,
    CandidateBinding,
    CandidateKind,
    PlanningLoopRequest,
    PlanningLoopResult,
    PlanningTerminalStatus,
)
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.runtime import EffectReceipt, EffectStatus
from novel_agent.domain.writing_loop import WritingLoopResult
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes

PLAN_CANDIDATE_MEDIA_TYPE = "application/vnd.novel-agent.stage5-plan-candidate+json"


class StrictFakePlanningLeaf:
    is_fixture = True

    def __init__(
        self,
        artifacts: ArtifactRepository,
        *,
        terminal: PlanningTerminalStatus = PlanningTerminalStatus.PLAN_CANDIDATE_READY,
    ) -> None:
        self._artifacts = artifacts
        self._terminal = terminal

    async def run(self, request: PlanningLoopRequest) -> PlanningLoopResult:
        if self._terminal is not PlanningTerminalStatus.PLAN_CANDIDATE_READY:
            return PlanningLoopResult(
                result_id=StableId(f"{request.task_id.root}.planner-result"),
                run_id=request.run_id,
                task_id=request.task_id,
                status=self._terminal,
                failure_code=f"planner_{self._terminal.value.lower()}",
                failure_detail="strict injected Planner terminal",
            )
        payload = {
            "contract": "stage5.strict-plan-candidate.v1",
            "run_id": request.run_id.root,
            "task_id": request.task_id.root,
            "project_id": request.project_id.root,
            "basis_commit": request.basis_commit.root,
            "basis_snapshot": (
                None if request.basis_snapshot is None else request.basis_snapshot.root
            ),
            "input_artifacts": [
                item.model_dump(mode="json") for item in request.input_artifact_refs
            ],
        }
        artifact = self._artifacts.put(
            canonical_json_bytes(payload), PLAN_CANDIDATE_MEDIA_TYPE, SchemaVersion("1.0.0")
        )
        candidate = CandidateBinding(
            candidate_id=StableId(
                "plan-candidate." + artifact.artifact_id.root.removeprefix("sha256:")[:48]
            ),
            kind=CandidateKind.PLAN,
            artifact_ref=artifact,
            candidate_hash=artifact.artifact_id.root,
            basis_commit=request.basis_commit,
            basis_snapshot=request.basis_snapshot,
            lineage_artifact_refs=request.input_artifact_refs,
        )
        return PlanningLoopResult(
            result_id=StableId(f"{request.task_id.root}.planner-result"),
            run_id=request.run_id,
            task_id=request.task_id,
            status=PlanningTerminalStatus.PLAN_CANDIDATE_READY,
            candidate=candidate,
            artifact_refs=(artifact,),
        )


class FaultInjectionWritingLeaf:
    is_fixture = True

    def __init__(self, result: WritingLoopResult) -> None:
        self._result = result

    async def run(self, request: object) -> WritingLoopResult:
        run_id = getattr(request, "run_id", None)
        task_id = getattr(request, "task_id", None)
        if self._result.run_id != run_id or self._result.task_id != task_id:
            raise ValueError("fault Writer result must match the injected request")
        return self._result


@dataclass(frozen=True)
class DeterministicEffectResolution:
    receipt: EffectReceipt


class FaultInjectionEffectStatusResolver:
    """Isolated-only requested→terminal/uncertain effect status script."""

    is_fixture = True

    def __init__(self, status: EffectStatus) -> None:
        if status is EffectStatus.REQUESTED:
            raise ValueError("effect resolver must inject a post-request observation")
        self._status = status

    def resolve(self, receipt: EffectReceipt) -> DeterministicEffectResolution:
        if receipt.status not in {EffectStatus.REQUESTED, EffectStatus.UNCERTAIN}:
            raise ValueError("effect resolver only accepts an unresolved effect")
        return DeterministicEffectResolution(
            receipt=receipt.model_copy(update={"status": self._status})
        )


class StrictDeterministicCandidateMaterializer:
    """Isolated-only trusted fixture that still uses legal five-Root Commit contracts."""

    is_fixture = True

    def __init__(self, commits: CommitService, *, candidate_kind: CandidateKind) -> None:
        self._commits = commits
        self._candidate_kind = candidate_kind

    def materialize(
        self, accepted: AcceptedCandidateBinding
    ) -> tuple[CandidateChangeBundle, ValidationReport]:
        if accepted.candidate.kind is not self._candidate_kind:
            raise ValueError("materializer received the wrong candidate kind")
        base = self._commits.load_manifest(accepted.expected_project_commit)
        candidate = accepted.candidate.artifact_ref
        if self._candidate_kind is CandidateKind.PLAN:
            proposed = base.model_copy(
                update={
                    "plan_root": PlanRootRef(
                        **candidate.model_dump(mode="python"), root_kind=RootKind.PLAN
                    ),
                    "parent_commit_ids": (accepted.expected_project_commit,),
                }
            )
        else:
            proposed = base.model_copy(
                update={
                    "text_root": TextRootRef(
                        **candidate.model_dump(mode="python"), root_kind=RootKind.TEXT
                    ),
                    "parent_commit_ids": (accepted.expected_project_commit,),
                }
            )
        bundle_id = StableId(f"bundle.{accepted.acceptance_id.root}"[:128])
        bundle = CandidateChangeBundle(
            bundle_id=bundle_id,
            project_id=accepted.project_id,
            run_id=accepted.run_id,
            base_commit=accepted.expected_project_commit,
            observed_changes=ObservedChangeSet(
                change_set_id=StableId(f"changes.{accepted.acceptance_id.root}"[:128]),
                base_commit=accepted.expected_project_commit,
                source_artifact=candidate,
            ),
            proposed_roots=RootManifest.model_validate(proposed),
            produced_artifacts=(candidate,),
        )
        report = ValidationReport(
            report_id=StableId(f"validation.{accepted.acceptance_id.root}"[:128]),
            bundle_id=bundle.bundle_id,
            status=ValidationStatus.PASSED,
            schema_version=SchemaVersion("1.0.0"),
            validation_profile="stage5-isolated-deterministic-v1",
            validated_at=accepted.accepted_at,
        )
        return bundle, report


__all__ = [
    "FaultInjectionEffectStatusResolver",
    "FaultInjectionWritingLeaf",
    "StrictDeterministicCandidateMaterializer",
    "StrictFakePlanningLeaf",
]
