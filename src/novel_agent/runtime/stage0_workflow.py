"""Minimal Stage 0 LangGraph workflow over trusted deterministic services."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from novel_agent.domain.artifacts import ArtifactRef, RootManifest, TextRootRef
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    CommitRequest,
    ObservedChangeSet,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.ids import CommitId, ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.runtime import (
    EvaluationDecision,
    EvaluationEntry,
    EvaluationMetric,
    ResumabilityStatus,
    RunCheckpoint,
    RunEvent,
    RunEventType,
)
from novel_agent.ports.telemetry import TelemetryPort
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository


class StageZeroState(TypedDict, total=False):
    project_id: str
    run_id: str
    task_id: str
    trace_id: str
    trace_carrier: dict[str, str]
    started_at: str
    input_artifact: dict[str, Any]
    base_commit: str
    root_manifest: dict[str, Any]
    sequence_no: int
    resolved_context_artifact: dict[str, Any]
    compiled_context_artifact: dict[str, Any]
    checkpoint_artifact: dict[str, Any]
    resumed: bool
    validation_status: str
    commit_result: dict[str, Any]
    evaluation_artifact: dict[str, Any]
    status: str


class StageZeroWorkflow:
    def __init__(
        self,
        artifact_repository: ArtifactRepository,
        commit_service: CommitService,
        event_log: RunEventLogRepository,
        checkpoints: RunCheckpointRepository,
        checkpointer: Any,
        telemetry: TelemetryPort,
    ) -> None:
        self._artifacts = artifact_repository
        self._commits = commit_service
        self._events = event_log
        self._checkpoints = checkpoints
        self._telemetry = telemetry
        builder = StateGraph(StageZeroState)
        builder.add_node("load_snapshot", self._load_snapshot)
        builder.add_node("resolve_context", self._resolve_context)
        builder.add_node("compile_context", self._compile_context)
        builder.add_node("process_chapter", self._process_chapter)
        builder.add_node("pause_after_process", self._pause_after_process)
        builder.add_node("extract_changes", self._extract_changes)
        builder.add_node("validate", self._validate)
        builder.add_node("atomic_commit", self._atomic_commit)
        builder.add_node("evaluate", self._evaluate)
        builder.add_node("finish", self._finish)
        builder.add_edge(START, "load_snapshot")
        builder.add_edge("load_snapshot", "resolve_context")
        builder.add_edge("resolve_context", "compile_context")
        builder.add_edge("compile_context", "process_chapter")
        builder.add_edge("process_chapter", "pause_after_process")
        builder.add_edge("pause_after_process", "extract_changes")
        builder.add_edge("extract_changes", "validate")
        builder.add_edge("validate", "atomic_commit")
        builder.add_edge("atomic_commit", "evaluate")
        builder.add_edge("evaluate", "finish")
        builder.add_edge("finish", END)
        self.graph = builder.compile(checkpointer=checkpointer)

    def initial_state(
        self,
        project_id: ProjectId,
        run_id: RunId,
        task_id: TaskId,
        input_artifact: dict[str, Any],
        *,
        trace_id: str,
    ) -> StageZeroState:
        return StageZeroState(
            project_id=project_id.root,
            run_id=run_id.root,
            task_id=task_id.root,
            trace_id=trace_id,
            trace_carrier={},
            started_at=datetime.now(UTC).isoformat(),
            input_artifact=input_artifact,
            sequence_no=0,
        )

    def resume(self, thread_id: str) -> StageZeroState:
        return cast(
            StageZeroState,
            self.graph.invoke(Command(resume=True), {"configurable": {"thread_id": thread_id}}),
        )

    def _load_snapshot(self, state: StageZeroState) -> StageZeroState:
        project_id = ProjectId(state["project_id"])
        commit_id = self._commits.current_commit(project_id)
        manifest = self._commits.load_manifest(commit_id)
        update = self._record_event(state, RunEventType.RUN_CREATED, "load_snapshot")
        return {
            **update,
            "base_commit": commit_id.root,
            "root_manifest": manifest.model_dump(mode="json"),
        }

    def _resolve_context(self, state: StageZeroState) -> StageZeroState:
        artifact = self._put_json(
            {"base_commit": state["base_commit"], "resolver": "deterministic_fake"}
        )
        return {
            **self._record_event(
                state, RunEventType.ARTIFACT_PRODUCED, "resolve_context", (artifact,)
            ),
            "resolved_context_artifact": artifact.model_dump(mode="json"),
        }

    def _compile_context(self, state: StageZeroState) -> StageZeroState:
        artifact = self._put_json(
            {
                "base_commit": state["base_commit"],
                "source": state["resolved_context_artifact"]["artifact_id"],
                "compiler": "deterministic_fake",
            }
        )
        return {
            **self._record_event(
                state, RunEventType.ARTIFACT_PRODUCED, "compile_context", (artifact,)
            ),
            "compiled_context_artifact": artifact.model_dump(mode="json"),
        }

    def _process_chapter(self, state: StageZeroState) -> StageZeroState:
        started_update = self._record_event(state, RunEventType.TASK_STARTED, "process_chapter")
        started_state: StageZeroState = {**state, **started_update}
        return self._record_event(started_state, RunEventType.TASK_COMPLETED, "process_chapter")

    def _pause_after_process(self, state: StageZeroState) -> StageZeroState:
        suspended_update = self._record_event(
            state, RunEventType.TASK_SUSPENDED, "pause_after_process"
        )
        suspended_state: StageZeroState = {**state, **suspended_update}
        checkpoint_artifact = self._put_json(
            {
                "base_commit": state["base_commit"],
                "input_artifact": state["input_artifact"],
                "compiled_context_artifact": state["compiled_context_artifact"],
                "sequence_no": suspended_state["sequence_no"],
            }
        )
        event_update = self._record_event(
            suspended_state,
            RunEventType.CHECKPOINT_CREATED,
            "pause_after_process",
            (checkpoint_artifact,),
        )
        checkpoint = RunCheckpoint(
            checkpoint_id=StableId(f"checkpoint.{state['run_id']}.after_process"),
            run_id=RunId(state["run_id"]),
            event_position=event_update["sequence_no"],
            logical_stage="after_process_chapter",
            state_artifact_ref=checkpoint_artifact,
            resumability_status=ResumabilityStatus.RESUMABLE,
        )
        self._checkpoints.save(checkpoint)
        resumed = interrupt(
            {
                "run_id": state["run_id"],
                "checkpoint_id": checkpoint.checkpoint_id.root,
                "stage": checkpoint.logical_stage,
            }
        )
        resumed_state: StageZeroState = {**state, **event_update}
        return {
            **self._record_event(resumed_state, RunEventType.RUN_RESUMED, "resume"),
            "checkpoint_artifact": checkpoint_artifact.model_dump(mode="json"),
            "resumed": bool(resumed),
        }

    def _extract_changes(self, state: StageZeroState) -> StageZeroState:
        return self._record_event(state, RunEventType.TASK_COMPLETED, "extract_changes")

    def _validate(self, state: StageZeroState) -> StageZeroState:
        return {
            **self._record_event(state, RunEventType.TASK_COMPLETED, "validate"),
            "validation_status": ValidationStatus.PASSED.value,
        }

    def _atomic_commit(self, state: StageZeroState) -> StageZeroState:
        requested_update = self._record_event(state, RunEventType.COMMIT_REQUESTED, "atomic_commit")
        requested_state: StageZeroState = {**state, **requested_update}
        schema_version = SchemaVersion("0.1.0")
        base_manifest = self._manifest_from_state(state)
        input_artifact = self._artifact_from_state(state["input_artifact"])
        proposed_manifest = RootManifest(
            project_id=base_manifest.project_id,
            schema_version=base_manifest.schema_version,
            text_root=TextRootRef(**input_artifact.model_dump()),
            plan_root=base_manifest.plan_root,
            world_root=base_manifest.world_root,
            reference_root=base_manifest.reference_root,
            project_profile_root=base_manifest.project_profile_root,
            parent_commit_ids=(self._commit_id(state),),
        )
        bundle_id = StableId(f"bundle.{state['run_id']}")
        observed = ObservedChangeSet(
            change_set_id=StableId(f"changes.{state['run_id']}"),
            base_commit=self._commit_id(state),
            source_artifact=input_artifact,
        )
        bundle = CandidateChangeBundle(
            bundle_id=bundle_id,
            project_id=base_manifest.project_id,
            run_id=RunId(state["run_id"]),
            base_commit=self._commit_id(state),
            observed_changes=observed,
            proposed_roots=proposed_manifest,
            produced_artifacts=(input_artifact,),
        )
        report = ValidationReport(
            report_id=StableId(f"validation.{state['run_id']}"),
            bundle_id=bundle_id,
            status=ValidationStatus.PASSED,
            schema_version=schema_version,
            validated_at=datetime.fromisoformat(state["started_at"]),
        )
        request = CommitRequest(
            request_id=StableId(f"commit-request.{state['run_id']}"),
            project_id=base_manifest.project_id,
            base_commit=self._commit_id(state),
            idempotency_key=StableId(f"commit.{state['run_id']}"),
            bundle=bundle,
            validation_report=report,
        )
        result = self._commits.commit(request)
        return {
            **self._record_event(
                requested_state,
                RunEventType.COMMIT_ACCEPTED,
                "atomic_commit",
                payload={"commit_id": result.commit_id.root if result.commit_id else None},
            ),
            "commit_result": result.model_dump(mode="json"),
        }

    def _evaluate(self, state: StageZeroState) -> StageZeroState:
        entry = EvaluationEntry(
            evaluation_id=StableId(f"evaluation.{state['run_id']}"),
            run_id=RunId(state["run_id"]),
            commit_id=self._commit_result_id(state),
            evaluator="stage0_deterministic",
            evaluator_version="0.1.0",
            rubric_version="0.1.0",
            metrics=(EvaluationMetric(name="workflow_completed", value=1.0),),
            decision=EvaluationDecision.SELECTED,
            created_at=datetime.fromisoformat(state["started_at"]),
        )
        artifact = self._put_json(entry.model_dump(mode="json"))
        return {
            **self._record_event(state, RunEventType.ARTIFACT_PRODUCED, "evaluate", (artifact,)),
            "evaluation_artifact": artifact.model_dump(mode="json"),
        }

    def _finish(self, state: StageZeroState) -> StageZeroState:
        return {
            **self._record_event(state, RunEventType.RUN_COMPLETED, "finish"),
            "status": "completed",
        }

    def _record_event(
        self,
        state: StageZeroState,
        event_type: RunEventType,
        node: str,
        artifact_refs: tuple[ArtifactRef, ...] = (),
        *,
        payload: dict[str, Any] | None = None,
    ) -> StageZeroState:
        sequence_no = state.get("sequence_no", 0) + 1
        with self._telemetry.span(
            f"stage0.{node}",
            state.get("trace_carrier", {}),
            {
                "novel_agent.run_id": state["run_id"],
                "novel_agent.correlation_id": state["trace_id"],
                "novel_agent.event_type": event_type.value,
                "novel_agent.sequence_no": sequence_no,
            },
        ) as telemetry_span:
            event = RunEvent(
                event_id=StableId(f"event.{state['run_id']}.{sequence_no}"),
                run_id=RunId(state["run_id"]),
                task_id=TaskId(state["task_id"]),
                sequence_no=sequence_no,
                event_type=event_type,
                occurred_at=datetime.now(UTC),
                idempotency_identity=StableId(f"effect.{state['run_id']}.{sequence_no}"),
                payload_schema_version=SchemaVersion("0.1.0"),
                trace_id=telemetry_span.trace_id,
                span_id=telemetry_span.span_id,
                payload=payload or {"node": node},
                artifact_refs=artifact_refs,
            )
            persisted = self._events.append(event)
            return {
                "sequence_no": persisted.sequence_no,
                "trace_carrier": telemetry_span.carrier,
            }

    def _put_json(self, value: Any) -> ArtifactRef:
        payload = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return self._artifacts.put(payload, "application/json", SchemaVersion("0.1.0"))

    @staticmethod
    def _artifact_from_state(value: dict[str, Any]) -> ArtifactRef:
        return ArtifactRef.model_validate_json(json.dumps(value))

    @staticmethod
    def _manifest_from_state(state: StageZeroState) -> RootManifest:
        return RootManifest.model_validate_json(json.dumps(state["root_manifest"]))

    @staticmethod
    def _commit_id(state: StageZeroState) -> CommitId:
        return CommitId(state["base_commit"])

    @staticmethod
    def _commit_result_id(state: StageZeroState) -> CommitId:
        return CommitId(cast(str, state["commit_result"]["commit_id"]))
