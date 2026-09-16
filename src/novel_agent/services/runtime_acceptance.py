"""Versioned candidate acceptance without Canon mutation authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    DRAFT_REVISION_DIRECTIVE_MEDIA_TYPE,
    OPERATOR_PLAN_REVIEW_MEDIA_TYPE,
    AcceptanceCommand,
    AcceptanceDecision,
    AcceptanceReceipt,
    AcceptedCandidateBinding,
    ActorKind,
    AutomationMode,
    CandidateBinding,
    CandidateKind,
    CreativeRunPolicy,
    OperatorReviewEvidence,
    commit_task_from_acceptance,
)
from novel_agent.domain.ids import SchemaVersion, StableId, TaskId
from novel_agent.domain.runtime import TaskKind, TaskPurpose, TaskRecord, TaskStatus
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.runtime_commands import (
    RuntimeCommandConflictError,
    RuntimeCommandService,
    bounded_runtime_identity,
)

ACCEPTANCE_MEDIA_TYPE = "application/vnd.novel-agent.stage5-acceptance-receipt+json"
ACCEPTANCE_SCHEMA_VERSION = SchemaVersion("1.0.0")
AUTHOR_REVISION_DIRECTIVE_MEDIA_TYPE = "application/vnd.novel-agent.author-revision-directive+json"
OPERATOR_REVISION_DIRECTIVE_MEDIA_TYPE = (
    "application/vnd.novel-agent.operator-revision-directive+json"
)
PLAN_REVIEW_MEDIA_TYPE = "application/vnd.novel-agent.plan-review+json"

# A rejected escalated review has to come back as a *structured* revision: prose in
# the author's reason never fills a candidate field, so the directive names the
# exact field each affected item must declare before the next review.
_ISSUE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "long_range_payoff_without_time_window": ("not_before_chapter",),
    "unresolved_scope_missing": ("affected_chapters",),
    "early_resolution_of_future_locked_obligation": ("target_chapter_start",),
    # Memory may only retrieve facts that exist at the declared source cutoff.
    # Keep this as a field-scoped repair: the rejected ChapterSet remains the
    # immutable parent and unrelated narrative fields are restored byte-for-byte.
    "history_need_targets_future_event": ("history_retrieval",),
}


def _issue_item_ids(issue: Mapping[str, object]) -> tuple[str, ...]:
    """The plan items an escalated review issue is bound to."""

    affected = issue.get("affected_item_ids")
    if not isinstance(affected, (list, tuple)):
        return ()
    return tuple(str(item) for item in affected)


_DIRECTIVE_ISSUE_FIELDS = (
    "issue_id",
    "kind",
    "summary",
    "blocking",
    "host_issued",
    "affected_item_ids",
    "proposed_target_item_ids",
    "authorized_target_item_ids",
    "authorized_operations",
    "field_path",
    "constraint_id",
    "actual",
    "expected",
    "quote",
    "citations",
    "unmet_condition",
    "evidence_refs",
    "memory_gap_questions",
)


def _directive_issue(issue: Mapping[str, object]) -> dict[str, object]:
    """Carry the immutable structured finding into the revision handoff.

    The directive is a control artifact, not a second review receipt.  It must
    nevertheless preserve the finding's field/constraint identity and its
    comparison evidence so the next Stage 4 invocation can audit why a field is
    required.  Only JSON-shaped values arrive here: model reviews are decoded
    from JSON and operator findings use ``model_dump(mode="json")``.
    """

    projected = {key: issue[key] for key in _DIRECTIVE_ISSUE_FIELDS if key in issue}
    projected["affected_item_ids"] = list(_issue_item_ids(issue))
    return projected


class RuntimeAcceptanceService:
    def __init__(
        self,
        commands: RuntimeCommandService,
        commits: CommitService,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion = ACCEPTANCE_SCHEMA_VERSION,
    ) -> None:
        self._commands = commands
        self._commits = commits
        self._artifacts = artifacts
        self._schema_version = schema_version

    def submit(
        self,
        command: AcceptanceCommand,
        *,
        policy: CreativeRunPolicy,
    ) -> AcceptanceReceipt:
        command_bytes = canonical_json_bytes(command.model_dump(mode="json"))
        command_hash = f"sha256:{hashlib.sha256(command_bytes).hexdigest()}"
        task = self._commands.get_task(command.task_id)
        expected_kind = {
            CandidateKind.PLAN: TaskKind.PLAN_ACCEPTANCE,
            CandidateKind.DRAFT: TaskKind.DRAFT_ACCEPTANCE,
        }[command.candidate.kind]
        if task.kind is not expected_kind or task.run_id != command.run_id:
            raise RuntimeCommandConflictError("candidate is bound to another acceptance task")
        if task.project_id != command.project_id:
            raise RuntimeCommandConflictError("acceptance project mismatch")
        if task.candidate_binding_ref is None:
            raise RuntimeCommandConflictError("acceptance task is missing its candidate binding")
        bound_candidate = CandidateBinding.model_validate_json(
            self._artifacts.read_verified(task.candidate_binding_ref)
        )
        if command.candidate != bound_candidate:
            raise RuntimeCommandConflictError(
                "acceptance command differs from the immutable candidate binding"
            )
        if command.acceptance_policy_hash != policy.policy_hash:
            raise RuntimeCommandConflictError("acceptance policy hash mismatch")
        if command.expected_project_commit != self._commits.current_commit(command.project_id):
            raise RuntimeCommandConflictError("acceptance expected commit is stale")
        if command.actor_kind is ActorKind.POLICY:
            allowed = policy.automation_mode is AutomationMode.AUTO and (
                policy.auto_accept_plan
                if command.candidate.kind is CandidateKind.PLAN
                else policy.auto_accept_draft
            )
            if not allowed:
                raise RuntimeCommandConflictError("automatic acceptance is not profile-pinned")
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.CANCELLED}:
            if len(task.terminal_artifact_refs) != 1:
                raise RuntimeCommandConflictError("settled acceptance has invalid receipt lineage")
            prior = AcceptanceReceipt.model_validate_json(
                self._artifacts.read_verified(task.terminal_artifact_refs[0])
            )
            if prior.command_hash != command_hash:
                raise RuntimeCommandConflictError(
                    "acceptance identity was reused with another payload"
                )
            if prior.decision is AcceptanceDecision.REJECT:
                try:
                    self.rejection_successor(command)
                except LookupError:
                    if command.candidate.kind is CandidateKind.PLAN:
                        directive_ref = self._record_revision(task, command)
                        replayed_revision = self._revised_plan_task(
                            task,
                            command.candidate.artifact_ref,
                            command.review_artifact_refs,
                            directive_ref,
                        )
                    else:
                        directive_ref = self._record_draft_revision(task, command)
                        replayed_revision = self._revised_draft_task(
                            task, command.candidate.artifact_ref, directive_ref
                        )
                    self._commands.create_task(replayed_revision)
            return prior
        if task.status is not TaskStatus.WAITING_INPUT:
            raise RuntimeCommandConflictError("acceptance task is not waiting for input")
        if (
            command.actor_kind is ActorKind.OPERATOR
            and command.decision is AcceptanceDecision.REJECT
            and command.candidate.kind is CandidateKind.PLAN
            and not command.review_artifact_refs
        ):
            raise RuntimeCommandConflictError(
                "operator plan rejection requires immutable review_artifact_refs"
            )
        if command.actor_kind is ActorKind.OPERATOR and any(
            ref.media_type != OPERATOR_PLAN_REVIEW_MEDIA_TYPE
            for ref in command.review_artifact_refs
        ):
            raise RuntimeCommandConflictError(
                "operator review_artifact_refs must use the operator review media type"
            )

        accepted = None
        if command.decision is AcceptanceDecision.ACCEPT:
            accepted = AcceptedCandidateBinding(
                acceptance_id=StableId("acceptance." + command_hash.removeprefix("sha256:")[:48]),
                command_id=command.command_id,
                project_id=command.project_id,
                run_id=command.run_id,
                task_id=command.task_id,
                candidate=command.candidate,
                actor_kind=command.actor_kind,
                actor_id=command.actor_id,
                accepted_at=command.issued_at,
                expected_project_commit=command.expected_project_commit,
            )
        receipt = AcceptanceReceipt(
            receipt_id=StableId("receipt." + command_hash.removeprefix("sha256:")[:48]),
            command_id=command.command_id,
            idempotency_identity=command.idempotency_identity,
            command_hash=command_hash,
            decision=command.decision,
            candidate=command.candidate,
            accepted_binding=accepted,
            reason=command.reason,
            recorded_at=command.issued_at,
        )
        receipt_ref = self._artifacts.put(
            canonical_json_bytes(receipt.model_dump(mode="json")),
            ACCEPTANCE_MEDIA_TYPE,
            self._schema_version,
        )
        successors: tuple[TaskRecord, ...] = ()
        revised: TaskRecord | None = None
        if receipt.accepted_binding is not None:
            settled = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": TaskStatus.SUCCEEDED,
                    "terminal_artifact_refs": (receipt_ref,),
                }
            )
            successors = (commit_task_from_acceptance(settled, receipt),)
        elif (
            command.decision is AcceptanceDecision.REJECT and task.kind is TaskKind.PLAN_ACCEPTANCE
        ):
            # The author refused the candidate.  A rejection that produced no
            # successor used to end the plan branch silently, so an escalated review
            # could never become a revised, re-reviewed candidate.  The rejected
            # acceptance task settles as CANCELLED and only a succeeded task may
            # create successors, so the revised generation is created explicitly
            # afterwards with its dependency on the rejected candidate recorded.
            directive_ref = self._record_revision(task, command)
            revised = self._revised_plan_task(
                task, command.candidate.artifact_ref, command.review_artifact_refs, directive_ref
            )
        elif (
            command.decision is AcceptanceDecision.REJECT and task.kind is TaskKind.DRAFT_ACCEPTANCE
        ):
            directive_ref = self._record_draft_revision(task, command)
            revised = self._revised_draft_task(task, command.candidate.artifact_ref, directive_ref)
        self._commands.complete_waiting_task(
            command.task_id,
            receipt=receipt,
            receipt_ref=receipt_ref,
            successor_tasks=successors,
        )
        if revised is not None:
            self._commands.create_task(revised)
        return receipt

    def rejection_successor(self, command: AcceptanceCommand) -> TaskRecord:
        """Return the durable repair task created by a rejected acceptance."""

        if command.decision is not AcceptanceDecision.REJECT:
            raise RuntimeCommandConflictError("only a rejected acceptance has a repair successor")
        task = self._commands.get_task(command.task_id)
        if command.candidate.kind is CandidateKind.DRAFT:
            producer = self._commands.get_task(task.dependency_task_ids[0])
            generation = max(task.writer_generation, producer.writer_generation) + 1
            suffix = f"draft.{task.chapter_index}.g{generation}"
            task_id = TaskId(
                bounded_runtime_identity(
                    f"{task.run_id.root}.{suffix}",
                    suffix,
                    f"draft.{task.run_id.root}.{generation}",
                ).root
            )
        else:
            generation = task.planning_generation + 1
            level = (
                "chapter-set"
                if task.plan_level is None
                else task.plan_level.value.replace("_", "-")
            )
            suffix = (
                f"plan.{level}.{task.horizon_start}-{task.horizon_end}.g{generation}"
                if task.horizon_start is not None and task.horizon_end is not None
                else f"plan.{level}.g{generation}"
            )
            task_id = TaskId(
                bounded_runtime_identity(
                    f"{task.run_id.root}.{suffix}",
                    suffix,
                    f"plan.{task.run_id.root}.{generation}",
                ).root
            )
        return self._commands.get_task(task_id)

    def _record_draft_revision(self, task: TaskRecord, command: AcceptanceCommand) -> ArtifactRef:
        directive = {
            "directive_id": bounded_runtime_identity(
                f"draft-revision.{task.task_id.root}",
                f"draft-revision.{command.command_id.root}",
            ).root,
            "kind": "draft_revision",
            "actor_kind": command.actor_kind.value,
            "actor_id": command.actor_id,
            "run_id": task.run_id.root,
            "project_id": task.project_id.root,
            "rejected_task_id": task.task_id.root,
            "target_chapter": task.chapter_index,
            "rejected_candidate_ref": command.candidate.artifact_ref.model_dump(mode="json"),
            "reason": command.reason,
            "required_action": "rewrite_complete_chapter_and_re_review",
        }
        return self._artifacts.put(
            canonical_json_bytes(directive),
            DRAFT_REVISION_DIRECTIVE_MEDIA_TYPE,
            self._schema_version,
        )

    def _revised_draft_task(
        self,
        task: TaskRecord,
        parent_ref: ArtifactRef,
        directive_ref: ArtifactRef,
    ) -> TaskRecord:
        producer = next(
            (
                self._commands.get_task(dependency_id)
                for dependency_id in task.dependency_task_ids
                if self._commands.get_task(dependency_id).kind is TaskKind.DRAFT_CANDIDATE
            ),
            None,
        )
        if producer is None or producer.status is not TaskStatus.SUCCEEDED:
            raise RuntimeCommandConflictError(
                "rejected Draft acceptance has no succeeded Writer producer"
            )
        generation = max(task.writer_generation, producer.writer_generation) + 1
        suffix = f"draft.{task.chapter_index}.g{generation}"
        return task.model_copy(
            update={
                "task_id": TaskId(
                    bounded_runtime_identity(
                        f"{task.run_id.root}.{suffix}",
                        suffix,
                        f"draft.{task.run_id.root}.{generation}",
                    ).root
                ),
                "kind": TaskKind.DRAFT_CANDIDATE,
                "task_revision": 0,
                "status": TaskStatus.READY,
                "candidate_binding_ref": None,
                "terminal_artifact_refs": (),
                "block_cause": None,
                "dependency_task_ids": (producer.task_id,),
                "input_artifact_refs": (parent_ref, directive_ref),
                "writer_generation": generation,
                "affects_future_plan": None,
            }
        )

    def _record_revision(self, task: TaskRecord, command: AcceptanceCommand) -> ArtifactRef:
        """Turn a reviewed rejection into a structured, actor-labelled revision."""

        issues = self._escalated_issues(
            task,
            command.review_artifact_refs,
            reviewer_id=command.actor_id if command.actor_kind is ActorKind.OPERATOR else None,
        )
        required: list[dict[str, str]] = []
        for issue in issues:
            fields = _ISSUE_REQUIRED_FIELDS.get(str(issue.get("kind")))
            if fields is None:
                continue
            required.extend(
                {"item_id": item_id, "field": field}
                for item_id in _issue_item_ids(issue)
                for field in fields
            )
        operator = command.actor_kind is ActorKind.OPERATOR
        directive = {
            "directive_id": bounded_runtime_identity(
                f"{'operator' if operator else 'author'}-revision.{task.task_id.root}",
                f"{'operator' if operator else 'author'}-revision.{command.command_id.root}",
                f"{task.run_id.root}.{task.task_revision}",
            ).root,
            "kind": "operator_revision" if operator else "author_revision",
            "actor_kind": command.actor_kind.value,
            "actor_id": command.actor_id,
            "run_id": task.run_id.root,
            "project_id": task.project_id.root,
            "rejected_task_id": task.task_id.root,
            "plan_level": None if task.plan_level is None else task.plan_level.value,
            "horizon_start": task.horizon_start,
            "horizon_end": task.horizon_end,
            "author_reason": command.reason if not operator else None,
            "operator_reason": command.reason if operator else None,
            "required_fields": required,
            "source_review_artifact_refs": [
                ref.artifact_id.root for ref in command.review_artifact_refs
            ],
            "escalated_issues": [_directive_issue(issue) for issue in issues],
        }
        return self._artifacts.put(
            canonical_json_bytes(directive),
            OPERATOR_REVISION_DIRECTIVE_MEDIA_TYPE
            if operator
            else AUTHOR_REVISION_DIRECTIVE_MEDIA_TYPE,
            self._schema_version,
        )

    def _escalated_issues(
        self,
        task: TaskRecord,
        review_artifact_refs: tuple[ArtifactRef, ...] = (),
        *,
        reviewer_id: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Read blocking issues from the candidate and cited immutable reviews."""

        if task.candidate_binding_ref is None:
            return ()
        try:
            candidate = CandidateBinding.model_validate_json(
                self._artifacts.read_verified(task.candidate_binding_ref)
            )
        except (UnicodeDecodeError, ValueError):
            return ()
        refs: list[ArtifactRef] = [
            ref
            for ref in candidate.lineage_artifact_refs
            if ref.media_type == PLAN_REVIEW_MEDIA_TYPE
        ]
        refs.extend(review_artifact_refs)
        collected: list[dict[str, object]] = []
        seen_refs: set[str] = set()
        for ref in refs:
            if ref.media_type == OPERATOR_PLAN_REVIEW_MEDIA_TYPE:
                try:
                    evidence = OperatorReviewEvidence.model_validate_json(
                        self._artifacts.read_verified(ref)
                    )
                except (UnicodeDecodeError, ValueError) as error:
                    raise RuntimeCommandConflictError(
                        "operator review artifact is not valid immutable evidence"
                    ) from error
                if reviewer_id is not None and evidence.reviewer_id != reviewer_id:
                    raise RuntimeCommandConflictError(
                        "operator review artifact reviewer does not match command actor"
                    )
                if evidence.target_artifact_ref.artifact_id != candidate.artifact_ref.artifact_id:
                    raise RuntimeCommandConflictError("operator review targets another candidate")
                collected.extend(finding.model_dump(mode="json") for finding in evidence.issues)
                continue
            if ref.media_type != PLAN_REVIEW_MEDIA_TYPE:
                continue
            if ref.artifact_id.root in seen_refs:
                continue
            seen_refs.add(ref.artifact_id.root)
            try:
                review = json.loads(self._artifacts.read_verified(ref).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(review, dict):
                continue
            if ref in review_artifact_refs:
                target_ref = review.get("target_artifact_ref")
                if isinstance(target_ref, dict) and target_ref.get("artifact_id") != (
                    candidate.artifact_ref.artifact_id.root
                ):
                    raise RuntimeCommandConflictError(
                        "cited operator review targets another candidate"
                    )
            for issue in review.get("issues") or ():
                if isinstance(issue, dict):
                    collected.append(issue)
        return tuple(collected)

    def _revised_plan_task(
        self,
        task: TaskRecord,
        parent_ref: ArtifactRef,
        review_refs: tuple[ArtifactRef, ...],
        directive_ref: ArtifactRef,
    ) -> TaskRecord:
        """One new planning generation that carries the author's revision directive."""

        control_media = {
            PLAN_REVIEW_MEDIA_TYPE,
            OPERATOR_PLAN_REVIEW_MEDIA_TYPE,
            AUTHOR_REVISION_DIRECTIVE_MEDIA_TYPE,
            OPERATOR_REVISION_DIRECTIVE_MEDIA_TYPE,
            "application/vnd.novel-agent.plan-proposal+json",
            "application/vnd.novel-agent.stage5-candidate-binding+json",
            "application/vnd.novel-agent.runtime-rebind-evidence+json",
            "application/vnd.novel-agent.planning-inquiry+json",
            "application/vnd.novel-agent.context-package+json",
            "application/vnd.novel-agent.planner-context-package+json",
            "application/vnd.novel-agent.planning-loop-event+json",
            "application/vnd.novel-agent.planning-loop-checkpoint+json",
            ACCEPTANCE_MEDIA_TYPE,
        }
        author_refs = tuple(
            ref for ref in task.input_artifact_refs if ref.media_type not in control_media
        )
        pending = list(task.dependency_task_ids)
        seen: set[TaskId] = set()
        while not author_refs and pending:
            dependency_id = pending.pop(0)
            if dependency_id in seen:
                continue
            seen.add(dependency_id)
            ancestor = self._commands.get_task(dependency_id)
            if ancestor.run_id == task.run_id and ancestor.project_id == task.project_id:
                author_refs = tuple(
                    ref
                    for ref in ancestor.input_artifact_refs
                    if ref.media_type not in control_media
                )
                pending.extend(ancestor.dependency_task_ids)
        if not author_refs:
            raise RuntimeCommandConflictError(
                "rejected plan has no verifiable upstream author-intent artifact"
            )
        if len(review_refs) > 1:
            raise RuntimeCommandConflictError("a revision requires one current operator review")
        producing_tasks = tuple(
            self._commands.get_task(dependency_id) for dependency_id in task.dependency_task_ids
        )
        producer = next(
            (
                item
                for item in producing_tasks
                if item.kind is TaskKind.PLAN_CANDIDATE and item.status is TaskStatus.SUCCEEDED
            ),
            None,
        )
        if producer is None:
            raise RuntimeCommandConflictError(
                "rejected acceptance has no succeeded planning producer"
            )

        generation = task.planning_generation + 1
        level = (
            "chapter-set" if task.plan_level is None else task.plan_level.value.replace("_", "-")
        )
        if task.horizon_start is not None and task.horizon_end is not None:
            suffix = f"plan.{level}.{task.horizon_start}-{task.horizon_end}.g{generation}"
        else:
            suffix = f"plan.{level}.g{generation}"
        return task.model_copy(
            update={
                "task_id": TaskId(
                    bounded_runtime_identity(
                        f"{task.run_id.root}.{suffix}",
                        suffix,
                        f"plan.{task.run_id.root}.{generation}",
                    ).root
                ),
                "kind": TaskKind.PLAN_CANDIDATE,
                "purpose": TaskPurpose.NORMAL,
                "task_revision": 0,
                "status": TaskStatus.READY,
                "candidate_binding_ref": None,
                "terminal_artifact_refs": (),
                "block_cause": None,
                "dependency_task_ids": (producer.task_id,),
                "input_artifact_refs": (*author_refs, parent_ref, *review_refs, directive_ref),
                "planning_generation": generation,
                "projection_after": None,
                "affects_future_plan": None,
            }
        )


__all__ = ["RuntimeAcceptanceService"]
